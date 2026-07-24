#include "SpircHandler.h"

#include <cstdint>      // for uint8_t
#include <memory>       // for shared_ptr, make_unique, unique_ptr
#include <type_traits>  // for remove_extent_t
#include <utility>      // for move

#include "BellLogger.h"      // for AbstractLogger
#include "CSpotContext.h"    // for Context::ConfigState, Context (ptr only)
#include "Logger.h"          // for CSPOT_LOG
#include "MercurySession.h"  // for MercurySession, MercurySession::Response
#include "NanoPBHelper.h"    // for pbDecode
#include "Packet.h"          // for cspot
#include "PlaybackState.h"   // for PlaybackState, PlaybackState::State
#include "TrackPlayer.h"     // for TrackPlayer
#include "TrackQueue.h"
#include "TrackReference.h"     // for TrackReference
#include "Utils.h"              // for stringHexToBytes
#include "pb_decode.h"          // for pb_release
#include "protobuf/spirc.pb.h"  // for Frame, State, Frame_fields, MessageTy...

using namespace cspot;

SpircHandler::SpircHandler(std::shared_ptr<cspot::Context> ctx) {
  this->playbackState = std::make_shared<PlaybackState>(ctx);
  this->trackQueue = std::make_shared<cspot::TrackQueue>(ctx, playbackState);

  auto EOFCallback = [this]() {
    if (trackQueue->isFinished()) {
      sendEvent(EventType::DEPLETED);
    }
  };

  auto trackLoadedCallback = [this](std::shared_ptr<QueuedTrack> track,
                                    bool paused = false) {
    playbackState->setPlaybackState(paused ? PlaybackState::State::Paused
                                           : PlaybackState::State::Playing);
    playbackState->updatePositionMs(track->requestedPosition);

    this->notify();

    // Send playback start event, pause/unpause per request
    sendEvent(EventType::PLAYBACK_START, (int)track->requestedPosition);
    sendEvent(EventType::PLAY_PAUSE, paused);
  };

  this->ctx = ctx;
  this->trackPlayer = std::make_shared<TrackPlayer>(
      ctx, trackQueue, EOFCallback, trackLoadedCallback);

  // Subscribe to mercury on session ready
  ctx->session->setConnectedHandler([this]() { this->subscribeToMercury(); });
}

void SpircHandler::subscribeToMercury() {
  auto responseLambda = [this](MercurySession::Response& res) {
    if (res.fail) {
      // ADI PATCH: used to be silent. The subscription is what delivers every remote command.
      CSPOT_LOG(error, "Control channel subscription failed (Spotify connection dropped)");
      return;
    }

    sendCmd(MessageType_kMessageTypeHello);
    CSPOT_LOG(debug, "Sent kMessageTypeHello!");
    // ADI PATCH: a number above 1 means the Spotify connection was re-established and the
    // control channel was re-created (commands only arrive through this subscription).
    static int subscriptions = 0;
    CSPOT_LOG(info, "Control channel active (subscription #%d)", ++subscriptions);

    // Assign country code
    this->ctx->config.countryCode = this->ctx->session->getCountryCode();
  };
  auto subscriptionLambda = [this](MercurySession::Response& res) {
    if (res.fail) {
      CSPOT_LOG(info, "Control channel lost (Spotify connection dropped)");  // ADI PATCH
      return;
    }
    CSPOT_LOG(debug, "Received subscription response");

    this->handleFrame(res.parts[0]);
  };

  ctx->session->executeSubscription(
      MercurySession::RequestType::SUB,
      "hm://remote/user/" + ctx->config.username + "/", responseLambda,
      subscriptionLambda);
}

void SpircHandler::loadTrackFromURI(const std::string& uri) {}

void SpircHandler::notifyAudioEnded() {
  playbackState->updatePositionMs(0);
  notify();
  trackPlayer->resetState(true);
}

void SpircHandler::notifyAudioReachedPlayback(std::string_view identifier) {
  std::shared_ptr<QueuedTrack> currentTrack;

  /* Validation and queue advancement happen as one atomic operation inside
   * TrackQueue: a Load/Replace frame can rebuild the queue from the mercury
   * thread at any moment, and a stale notification (e.g. from a stream that
   * predates the rebuild) must neither consume the pending notify nor pop the
   * queue one track early - that desyncs playback with no way to resync.
   * Callers that cannot identify the playing track (flow mode) pass an empty
   * identifier and keep the legacy behavior. */
  switch (trackQueue->notifyTrackReached(identifier, currentTrack)) {
    case TrackQueue::Reached::IGNORED:
      return;
    case TrackQueue::Reached::CONSUMED_PENDING:
      playbackState->updatePositionMs(currentTrack->requestedPosition);

      // Reset position in queued track
      currentTrack->requestedPosition = 0;
      break;
    case TrackQueue::Reached::ADVANCED:
      playbackState->updatePositionMs(0);
      break;
  }

  this->notify();

  sendEvent(EventType::TRACK_INFO, currentTrack->trackInfo);
}

void SpircHandler::updatePositionMs(uint32_t position) {
  playbackState->updatePositionMs(position);
  notify();
}

void SpircHandler::disconnect() {
  this->ctx->session->disable();
  this->trackQueue->stopTask();
  this->trackPlayer->stop();
  this->ctx->session->disconnect();
}

// ADI PATCH ---------------------------------------------------------------------------
// Diagnostics: show every command Spotify sends to this speaker (except the frequent Notify
// frames), including which tracks it contains, so a voice/Web-API command can be matched
// against what the backend asked for (the backend logs the same track as a GID).
static const char* remoteFrameName(int type) {
  switch (type) {
    case MessageType_kMessageTypeHello:
      return "Hello";
    case MessageType_kMessageTypeGoodbye:
      return "Goodbye";
    case MessageType_kMessageTypeProbe:
      return "Probe";
    case MessageType_kMessageTypeNotify:
      return "Notify";
    case MessageType_kMessageTypeLoad:
      return "Load";
    case MessageType_kMessageTypePlay:
      return "Play";
    case MessageType_kMessageTypePause:
      return "Pause";
    case MessageType_kMessageTypePlayPause:
      return "PlayPause";
    case MessageType_kMessageTypeSeek:
      return "Seek";
    case MessageType_kMessageTypePrev:
      return "Prev";
    case MessageType_kMessageTypeNext:
      return "Next";
    case MessageType_kMessageTypeVolume:
      return "Volume";
    case MessageType_kMessageTypeShuffle:
      return "Shuffle";
    case MessageType_kMessageTypeRepeat:
      return "Repeat";
    case MessageType_kMessageTypeVolumeDown:
      return "VolumeDown";
    case MessageType_kMessageTypeVolumeUp:
      return "VolumeUp";
    case MessageType_kMessageTypeReplace:
      return "Replace";
    case MessageType_kMessageTypeLogout:
      return "Logout";
    case MessageType_kMessageTypeAction:
      return "Action";
    default:
      return "unknown";
  }
}

static void logRemoteFrame(PlaybackState& state) {
  const auto& frame = state.remoteFrame;
  if (frame.typ == MessageType_kMessageTypeNotify) {
    return;
  }

  const char* context =
      frame.state.context_uri != nullptr ? frame.state.context_uri : "<none>";
  CSPOT_LOG(info,
            "Remote command: %s (type %d), tracks=%d, playing_index=%d, "
            "position=%u ms, context=%s",
            remoteFrameName((int)frame.typ), (int)frame.typ,
            (int)state.remoteTracks.size(),
            (int)frame.state.playing_track_index, (unsigned)frame.position,
            context);

  int shown = 0;
  for (auto& ref : state.remoteTracks) {
    if (shown >= 3) {
      CSPOT_LOG(info, "  ... and %d more tracks",
                (int)state.remoteTracks.size() - shown);
      break;
    }
    CSPOT_LOG(info, "  remote track[%d] gid=%s uri=%s", shown,
              bytesToHexString(ref.gid).c_str(),
              ref.uri.empty() ? "-" : ref.uri.c_str());
    shown++;
  }
}

void SpircHandler::handleFrame(std::vector<uint8_t>& data) {
  // Decode received spirc frame
  playbackState->decodeRemoteFrame(data);

  logRemoteFrame(*playbackState);  // ADI PATCH

  switch (playbackState->remoteFrame.typ) {
    case MessageType_kMessageTypeNotify: {
      CSPOT_LOG(debug, "Notify frame");

      // Pause the playback if another player took control
      if (playbackState->isActive() &&
          playbackState->remoteFrame.device_state.is_active) {
        CSPOT_LOG(debug, "Another player took control, pausing playback");
        playbackState->setActive(false);

        this->trackPlayer->stop();
        sendEvent(EventType::DISC);
      }
      break;
    }
    case MessageType_kMessageTypeSeek: {
      this->trackPlayer->seekMs(playbackState->remoteFrame.position);

      playbackState->updatePositionMs(playbackState->remoteFrame.position);

      notify();

      sendEvent(EventType::SEEK, (int)playbackState->remoteFrame.position);
      break;
    }
    case MessageType_kMessageTypeVolume:
      playbackState->setVolume(playbackState->remoteFrame.volume);
      this->notify();
      sendEvent(EventType::VOLUME, (int)playbackState->remoteFrame.volume);
      break;
    case MessageType_kMessageTypePause:
      setPause(true);
      break;
    case MessageType_kMessageTypePlay:
      setPause(false);
      break;
    case MessageType_kMessageTypePlayPause:
      // ADI PATCH: the toggle form of play/pause was silently dropped by the default branch.
      setPause(playbackState->innerFrame.state.status == PlayStatus_kPlayStatusPlay);
      break;
    case MessageType_kMessageTypeNext:
      if (nextSong()) {
        sendEvent(EventType::NEXT);
      }
      break;
    case MessageType_kMessageTypePrev:
      if (previousSong()) {
        sendEvent(EventType::PREV);
      }
      break;
    case MessageType_kMessageTypeLoad: {
      this->trackPlayer->start();

      CSPOT_LOG(debug, "Load frame %d!", playbackState->remoteTracks.size());

      if (playbackState->remoteTracks.size() == 0) {
        CSPOT_LOG(info, "No tracks in frame, stopping playback");
        break;
      }

      playbackState->setActive(true);

      playbackState->updatePositionMs(playbackState->remoteFrame.position);
      playbackState->setPlaybackState(PlaybackState::State::Playing);

      playbackState->syncWithRemote();

      // Update track list in case we have a new one
      trackQueue->updateTracks(playbackState->remoteFrame.state.position_ms,
                               true);

      this->notify();

      // Stop the current track, if any
      trackPlayer->resetState();
      break;
    }
    case MessageType_kMessageTypeReplace: {
      CSPOT_LOG(debug, "Got replace frame %d",
                playbackState->remoteTracks.size());
      playbackState->syncWithRemote();

      // 1st track is the current one, but update the position
      bool cleared = trackQueue->updateTracks(
          playbackState->remoteFrame.state.position_ms +
              ctx->timeProvider->getSyncedTimestamp() -
              playbackState->innerFrame.state.position_measured_at,
          false);

      this->notify();

      // need to re-load all if streaming track is completed
      if (cleared) {
        sendEvent(EventType::FLUSH);
        trackPlayer->resetState();
      }
      break;
    }
    case MessageType_kMessageTypeShuffle: {
      CSPOT_LOG(debug, "Got shuffle frame");
      this->notify();
      break;
    }
    case MessageType_kMessageTypeRepeat: {
      CSPOT_LOG(debug, "Got repeat frame");
      this->notify();
      break;
    }
    default:
      // ADI PATCH: commands cspot does not implement used to vanish without a trace.
      CSPOT_LOG(info, "Remote command type %d is not handled",
                (int)playbackState->remoteFrame.typ);
      break;
  }
}

void SpircHandler::setRemoteVolume(int volume) {
  playbackState->setVolume(volume);
  notify();
}

void SpircHandler::notify() {
  this->sendCmd(MessageType_kMessageTypeNotify);
}

bool SpircHandler::skipSong(TrackQueue::SkipDirection dir) {
  bool skipped = trackQueue->skipTrack(dir);

  // Reset track state
  trackPlayer->resetState(!skipped);

  // send NEXT or PREV event only when successful
  return skipped;
}

bool SpircHandler::nextSong() {
  return skipSong(TrackQueue::SkipDirection::NEXT);
}

bool SpircHandler::previousSong() {
  return skipSong(TrackQueue::SkipDirection::PREV);
}

std::shared_ptr<TrackPlayer> SpircHandler::getTrackPlayer() {
  return this->trackPlayer;
}

void SpircHandler::sendCmd(MessageType typ) {
  // Serialize current player state
  auto encodedFrame = playbackState->encodeCurrentFrame(typ);

  auto responseLambda = [=](MercurySession::Response& res) {
  };
  auto parts = MercurySession::DataParts({encodedFrame});
  ctx->session->execute(MercurySession::RequestType::SEND,
                        "hm://remote/user/" + ctx->config.username + "/",
                        responseLambda, parts);
}
void SpircHandler::setEventHandler(EventHandler handler) {
  this->eventHandler = handler;
}

void SpircHandler::setPause(bool isPaused) {
  CSPOT_LOG(info, "Playback %s by remote command",
            isPaused ? "paused" : "resumed");  // ADI PATCH
  if (isPaused) {
    CSPOT_LOG(debug, "External pause command");
    playbackState->setPlaybackState(PlaybackState::State::Paused);
  } else {
    CSPOT_LOG(debug, "External play command");

    playbackState->setPlaybackState(PlaybackState::State::Playing);
  }
  notify();
  sendEvent(EventType::PLAY_PAUSE, isPaused);
}

void SpircHandler::sendEvent(EventType type) {
  auto event = std::make_unique<Event>();
  event->eventType = type;
  event->data = {};
  eventHandler(std::move(event));
}

void SpircHandler::sendEvent(EventType type, EventData data) {
  auto event = std::make_unique<Event>();
  event->eventType = type;
  event->data = data;
  eventHandler(std::move(event));
}
