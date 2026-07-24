#include "TrackPlayer.h"
#include "Utils.h"  // bytesToHexString (ADI PATCH)

#include <chrono>  // ADI PATCH
#include <exception>    // for exception (ADI PATCH)
#include <mutex>        // for mutex, scoped_lock
#include <string>       // for string
#include <type_traits>  // for remove_extent_t
#include <vector>       // for vector, vector<>::value_type

#include "BellLogger.h"        // for AbstractLogger
#include "BellUtils.h"         // for BELL_SLEEP_MS
#include "Logger.h"            // for CSPOT_LOG
#include "Packet.h"            // for cspot
#include "TrackQueue.h"        // for CDNTrackStream, CDNTrackStream::TrackInfo
#include "WrappedSemaphore.h"  // for WrappedSemaphore

#ifdef BELL_VORBIS_FLOAT
#define VORBIS_SEEK(file, position) \
  (ov_time_seek(file, (double)position / 1000))
#define VORBIS_READ(file, buffer, bufferSize, section) \
  (ov_read(file, buffer, bufferSize, 0, 2, 1, section))
#else
#define VORBIS_SEEK(file, position) (ov_time_seek(file, position))
#define VORBIS_READ(file, buffer, bufferSize, section) \
  (ov_read(file, buffer, bufferSize, section))
#endif

namespace cspot {
struct Context;
struct TrackReference;
}  // namespace cspot

using namespace cspot;

static size_t vorbisReadCb(void* ptr, size_t size, size_t nmemb,
                           TrackPlayer* self) {
  return self->_vorbisRead(ptr, size, nmemb);
}

static int vorbisCloseCb(TrackPlayer* self) {
  return self->_vorbisClose();
}

static int vorbisSeekCb(TrackPlayer* self, int64_t offset, int whence) {

  return self->_vorbisSeek(offset, whence);
}

static long vorbisTellCb(TrackPlayer* self) {
  return self->_vorbisTell();
}

TrackPlayer::TrackPlayer(std::shared_ptr<cspot::Context> ctx,
                         std::shared_ptr<cspot::TrackQueue> trackQueue,
                         EOFCallback eof, TrackLoadedCallback trackLoaded)
    : bell::Task("cspot_player", 48 * 1024, 5, 1) {
  this->ctx = ctx;
  this->eofCallback = eof;
  this->trackLoaded = trackLoaded;
  this->trackQueue = trackQueue;
  this->playbackSemaphore = std::make_unique<bell::WrappedSemaphore>(5);

  // Initialize vorbis callbacks
  vorbisFile = {};
  vorbisCallbacks = {
      (decltype(ov_callbacks::read_func))&vorbisReadCb,
      (decltype(ov_callbacks::seek_func))&vorbisSeekCb,
      (decltype(ov_callbacks::close_func))&vorbisCloseCb,
      (decltype(ov_callbacks::tell_func))&vorbisTellCb,
  };
}

TrackPlayer::~TrackPlayer() {
  isRunning = false;
  resetState();
  std::scoped_lock lock(runningMutex);
}

void TrackPlayer::start() {
  if (!isRunning) {
    isRunning = true;
    startTask();
  }
}

void TrackPlayer::stop() {
  isRunning = false;
  resetState();
  std::scoped_lock lock(runningMutex);
}

void TrackPlayer::resetState(bool paused) {
  // Mark for reset
  this->pendingReset = true;
  this->currentSongPlaying = false;
  this->startPaused = paused;

  std::scoped_lock lock(dataOutMutex);

  CSPOT_LOG(info, "Resetting state (paused=%d)", (int)paused);  // ADI PATCH
}

void TrackPlayer::seekMs(size_t ms) {
  if (inFuture) {
    // We're in the middle of the next track, so we need to reset the player in order to seek
    resetState();
  }

  CSPOT_LOG(info, "Seeking...");
  this->pendingSeekPositionMs = ms;
}

// ADI PATCH ---------------------------------------------------------------------------
// Optional hook implemented by the ADI shim (weak: resolves to null elsewhere). Logs the
// internal/PSRAM heap state so memory problems are visible next to the failure.
extern "C" void adi_cspot_log_heap(const char* where) __attribute__((weak));

// How long a track may take to become READY (metadata + audio key + CDN URL, each a
// network round trip incl. a TLS handshake). The original fixed 5 s was shorter than the
// chain takes on this device when the CPU is busy with wake word / Wi-Fi, so tracks were
// abandoned (and then finished loading a moment later, unused).
static constexpr uint32_t kTrackLoadTimeoutMs = 30000;

static const char* trackStateName(QueuedTrack::State state) {
  switch (state) {
    case QueuedTrack::State::QUEUED:
      return "QUEUED";
    case QueuedTrack::State::PENDING_META:
      return "PENDING_META (waiting for track metadata)";
    case QueuedTrack::State::KEY_REQUIRED:
      return "KEY_REQUIRED";
    case QueuedTrack::State::PENDING_KEY:
      return "PENDING_KEY (waiting for audio key)";
    case QueuedTrack::State::CDN_REQUIRED:
      return "CDN_REQUIRED (resolving CDN URL)";
    case QueuedTrack::State::READY:
      return "READY";
    case QueuedTrack::State::FAILED:
      return "FAILED";
  }
  return "?";
}

static bool trackStillLoading(QueuedTrack::State state) {
  return state != QueuedTrack::State::READY &&
         state != QueuedTrack::State::FAILED;
}

void TrackPlayer::runTask() {
  std::scoped_lock lock(runningMutex);

  std::shared_ptr<QueuedTrack> track, newTrack = nullptr;

  int trackOffset = 0;
  bool eof = false;
  bool endOfQueueReached = false;

  while (isRunning) {
    // Ensure we even have any tracks to play
    if (!this->trackQueue->hasTracks() ||
        (!pendingReset && endOfQueueReached && trackQueue->isFinished())) {
      this->trackQueue->playableSemaphore->twait(300);
      continue;
    }

    // Last track was interrupted, reset to default
    if (pendingReset) {
      track = nullptr;
      pendingReset = false;
      inFuture = false;
    }

    endOfQueueReached = false;

    // Wait 800ms. If next reset is requested in meantime, restart the queue.
    // Gets rid of excess actions during rapid queueing
    BELL_SLEEP_MS(50);

    if (pendingReset) {
      continue;
    }

    newTrack = trackQueue->consumeTrack(track, trackOffset);

    if (newTrack == nullptr) {
      if (trackOffset == -1) {
        // Reset required
        track = nullptr;
      }

      BELL_SLEEP_MS(100);
      continue;
    }

    track = newTrack;

    inFuture = trackOffset > 0;

    // ADI PATCH: which track the player is working on, and how far along it is.
    CSPOT_LOG(info, "Player picked track gid=%s (queue offset %d, state=%s)",
              bytesToHexString(track->ref.gid).c_str(), trackOffset,
              trackStateName(track->state));

    if (track->state != QueuedTrack::State::READY) {
      // ADI PATCH: wait while the track is still making progress instead of giving up
      // after a fixed 5 s, and stop waiting right away on failure or when a new load /
      // seek / stop request arrives.
      const auto waitStart = std::chrono::steady_clock::now();
      auto waitedMs = [&waitStart]() {
        return (long)std::chrono::duration_cast<std::chrono::milliseconds>(
                   std::chrono::steady_clock::now() - waitStart)
            .count();
      };

      while (isRunning && !pendingReset && trackStillLoading(track->state) &&
             waitedMs() < (long)kTrackLoadTimeoutMs) {
        track->loadedSemaphore->twait(250);
      }

      if (pendingReset || !isRunning) {
        // A new request replaced this one while it was loading: not a failure.
        continue;
      }

      if (track->state != QueuedTrack::State::READY) {
        CSPOT_LOG(error,
                  "Track failed to load, skipping it (state=%s, waited %ld ms)",
                  trackStateName(track->state), waitedMs());
        if (adi_cspot_log_heap) {
          adi_cspot_log_heap("track failed to load");
        }
        this->eofCallback();
        continue;
      }

      if (waitedMs() >= 2000) {
        CSPOT_LOG(info, "Track took %ld ms to become ready", waitedMs());
      }
    }

    CSPOT_LOG(info, "Got track ID=%s", track->identifier.c_str());

    currentSongPlaying = true;

    {
      std::scoped_lock lock(playbackMutex);

      // ADI PATCH: this runs in a task with no exception handler, so a network error or
      // std::bad_alloc here used to abort() the device. Skip the track instead.
      try {
        currentTrackStream = track->getAudioFile();

        // Open the stream
        currentTrackStream->openStream();
      } catch (const std::exception& e) {
        CSPOT_LOG(error, "Cannot open track stream (%s), skipping it", e.what());
        if (adi_cspot_log_heap) {
          adi_cspot_log_heap("cannot open track stream");
        }
        currentTrackStream = nullptr;
        this->eofCallback();
        continue;
      } catch (...) {
        CSPOT_LOG(error, "Cannot open track stream (unknown exception)");
        currentTrackStream = nullptr;
        this->eofCallback();
        continue;
      }

      if (pendingReset || !currentSongPlaying) {
        continue;
      }

      if (trackOffset == 0 && pendingSeekPositionMs == 0) {
        this->trackLoaded(track, startPaused);
        startPaused = false;
      }

      int32_t r =
          ov_open_callbacks(this, &vorbisFile, NULL, 0, vorbisCallbacks);

      if (r != 0) {
        // ADI PATCH: the result was ignored, so a decoder that could not open the stream
        // only showed up later as a confusing read error.
        CSPOT_LOG(error,
                  "Vorbis decoder could not open the stream (ov_open_callbacks "
                  "= %d), skipping it",
                  (int)r);
        currentTrackStream = nullptr;
        this->eofCallback();
        continue;
      }

      if (pendingSeekPositionMs > 0) {
        track->requestedPosition = pendingSeekPositionMs;
      }

      if (track->requestedPosition > 0) {
        VORBIS_SEEK(&vorbisFile, track->requestedPosition);
      }

      eof = false;
      track->loading = true;

      CSPOT_LOG(info, "Playing");

      while (!eof && currentSongPlaying) {
        // Execute seek if needed
        if (pendingSeekPositionMs > 0) {
          uint32_t seekPosition = pendingSeekPositionMs;

          // Reset the pending seek position
          pendingSeekPositionMs = 0;

          // Seek to the new position
          VORBIS_SEEK(&vorbisFile, seekPosition);
        }

        long ret = VORBIS_READ(&vorbisFile, (char*)&pcmBuffer[0],
                               pcmBuffer.size(), &currentSection);

        if (ret == 0) {
          CSPOT_LOG(info, "EOF");
          // and done :)
          eof = true;
        } else if (ret < 0) {
          CSPOT_LOG(error, "An error has occured in the stream %d", ret);
          currentSongPlaying = false;
        } else {
          if (this->dataCallback != nullptr) {
            auto toWrite = ret;

            while (!eof && currentSongPlaying && !pendingReset && toWrite > 0) {
              int written = 0;
              {
                std::scoped_lock dataOutLock(dataOutMutex);
                // If reset happened during playback, return
                if (!currentSongPlaying || pendingReset)
                  break;

                written = dataCallback(pcmBuffer.data() + (ret - toWrite),
                                       toWrite, track->identifier);
              }
              if (written == 0) {
                BELL_SLEEP_MS(50);
              }
              toWrite -= written;
            }
          }
        }
      }
      ov_clear(&vorbisFile);

      // ADI PATCH: tell a natural end of track from an interruption (Load/seek/stop).
      CSPOT_LOG(info, "Playing done (eof=%d, interrupted=%d)", (int)eof,
                (int)(!currentSongPlaying || pendingReset));

      // always move back to LOADING (ensure proper seeking after last track has been loaded)
      currentTrackStream = nullptr;
      track->loading = false;
    }

    if (eof) {
      if (trackQueue->isFinished()) {
        endOfQueueReached = true;
      }

      this->eofCallback();
    }
  }
}

size_t TrackPlayer::_vorbisRead(void* ptr, size_t size, size_t nmemb) {
  if (this->currentTrackStream == nullptr) {
    return 0;
  }
  return this->currentTrackStream->readBytes((uint8_t*)ptr, nmemb * size);
}

size_t TrackPlayer::_vorbisClose() {
  return 0;
}

int TrackPlayer::_vorbisSeek(int64_t offset, int whence) {
  if (this->currentTrackStream == nullptr) {
    return 0;
  }
  switch (whence) {
    case 0:
      this->currentTrackStream->seek(offset);  // Spotify header offset
      break;
    case 1:
      this->currentTrackStream->seek(this->currentTrackStream->getPosition() +
                                     offset);
      break;
    case 2:
      this->currentTrackStream->seek(this->currentTrackStream->getSize() +
                                     offset);
      break;
  }

  return 0;
}

long TrackPlayer::_vorbisTell() {
  if (this->currentTrackStream == nullptr) {
    return 0;
  }
  return this->currentTrackStream->getPosition();
}

void TrackPlayer::setDataCallback(DataCallback callback) {
  this->dataCallback = callback;
}
