/*
 * Spotify Connect player for the ADI firmware.
 *
 * Adapted from squeezelite-esp32/components/spotify/Shim.cpp (MIT License). Kept: the
 * cspot player task, ZeroConf HTTP handlers, mDNS advertisement and Spirc event handling.
 * Removed: Squeezelite's platform_config / NVS helpers / sink command API, replaced by the
 * small callback interface in adi_cspot.h so the ESPHome component owns audio + storage.
 */

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <string>

#include "BellTask.h"
#include "CSpotContext.h"
#include "Logger.h"
#include "LoginBlob.h"
#include "MDNSService.h"
#include "SpircHandler.h"
#include "TrackPlayer.h"
#include "WrappedSemaphore.h"

#include <exception>

#include "esp_heap_caps.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "adi_cspot.h"

static const char *const TAG = "adi_cspot";

namespace {

// In-place application/x-www-form-urlencoded decoding ("%2B" -> '+', '+' -> ' ').
void url_decode(char *s) {
  char *out = s;
  for (char *in = s; *in; ++in) {
    if (*in == '%' && isxdigit((unsigned char) in[1]) && isxdigit((unsigned char) in[2])) {
      char hex[3] = {in[1], in[2], 0};
      *out++ = (char) strtol(hex, nullptr, 16);
      in += 2;
    } else if (*in == '+') {
      *out++ = ' ';
    } else {
      *out++ = *in;
    }
  }
  *out = 0;
}

// ---------------------------------------------------------------------------------------
// Memory policy.
//
// ESPHome's `psram:` component builds with CONFIG_SPIRAM_USE_CAPS_ALLOC=y. In that mode PSRAM
// is reachable ONLY through heap_caps_*(MALLOC_CAP_SPIRAM): plain malloc()/operator new use
// internal RAM exclusively and never fall back to PSRAM, however much of it is free. cspot and
// bell (inherited from Squeezelite, which runs with malloc allowed to use PSRAM) allocate
// everything with new/std::vector/std::string/malloc, so they compete with Wi-Fi, lwip and the
// wake word for the small internal heap. When it runs dry operator new throws std::bad_alloc,
// which nothing catches -> std::terminate -> abort() -> reboot loop.
//
// heap_caps_malloc_extmem_enable() gives malloc() the CONFIG_SPIRAM_USE_MALLOC behaviour at
// runtime: requests <= the limit still try internal RAM first, bigger ones try PSRAM first, and
// any request that fails in its preferred region is retried in the other one. The limit is kept
// above lwip's largest allocations (~1.6 KB pbufs) so networking behaves as before; the big
// cspot buffers (CDN header/footer/chunk buffers, Vorbis tables, TLS records) move to PSRAM.
// Code that needs internal/DMA memory already asks for it with explicit heap_caps flags.
constexpr size_t kInternalFirstLimit = 4096;

void log_heap(const char *where) {
  ESP_LOGI(TAG, "heap @ %s: internal free=%u largest=%u min-ever=%u | psram free=%u largest=%u", where,
           (unsigned) heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
           (unsigned) heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
           (unsigned) heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
           (unsigned) heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
           (unsigned) heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM));
}

void enable_psram_malloc() {
  if (heap_caps_get_total_size(MALLOC_CAP_SPIRAM) == 0) {
    ESP_LOGW(TAG, "no PSRAM heap found; cspot will use internal RAM only");
    return;
  }
  log_heap("before enabling PSRAM malloc");
  heap_caps_malloc_extmem_enable(kInternalFirstLimit);
  ESP_LOGI(TAG, "malloc()/new may now use PSRAM (internal first up to %u bytes, PSRAM fallback for all sizes)",
           (unsigned) kInternalFirstLimit);
}

class Player : public bell::Task {
 public:
  Player(const adi_cspot_config_t &cfg, const adi_cspot_callbacks_t &cb)
      : bell::Task("adi_cspot", 32 * 1024, 0, 1), cb_(cb) {
    name_ = cfg.name ? cfg.name : "ADI Speaker";
    port_ = cfg.port;
    bitrate_ = (cfg.bitrate == 96 || cfg.bitrate == 320) ? cfg.bitrate : 160;
    volume_ = cfg.initial_volume;
    client_id_ = cfg.client_id ? cfg.client_id : "";
    client_secret_ = cfg.client_secret ? cfg.client_secret : "";
    if (cfg.credentials) credentials_ = cfg.credentials;
  }

  bool start_http() {
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = port_;
    config.ctrl_port = port_ + 1;  // avoid clashing with any other httpd instance
    config.max_open_sockets = 3;   // keep LWIP sockets for cspot + the ADI WebSocket
    config.stack_size = 6144;
    if (httpd_start(&server_, &config) != ESP_OK) {
      ESP_LOGE(TAG, "could not start the ZeroConf HTTP server on port %d", port_);
      return false;
    }
    return true;
  }

  esp_err_t handle_get(httpd_req_t *request) {
    std::string body = blob_->buildZeroconfInfo();
    if (body.empty()) return ESP_ERR_HTTPD_INVALID_REQ;
    httpd_resp_set_hdr(request, "Content-type", "application/json");
    return httpd_resp_send(request, body.c_str(), body.size());
  }

  esp_err_t handle_post(httpd_req_t *request) {
    // https://developer.spotify.com/documentation/commercial-hardware/implementation/guides/zeroconf
    if (request->content_len > 0 && request->content_len < 4096) {
      char *body = (char *) calloc(1, request->content_len + 1);
      int got = body ? httpd_req_recv(request, body, request->content_len) : -1;
      if (got > 0) {
        body[got] = 0;
        std::map<std::string, std::string> query;
        char *save = nullptr;
        for (char *pair = strtok_r(body, "&", &save); pair; pair = strtok_r(nullptr, "&", &save)) {
          char *eq = strchr(pair, '=');
          if (!eq) continue;
          *eq++ = 0;
          url_decode(pair);
          url_decode(eq);
          query[pair] = eq;
        }
        blob_->loadZeroconfQuery(query);
        // A (new) user selected this speaker: (re)connect with the new credentials.
        if (state_ == LINKED) state_ = ABORT;
        client_connected_.give();
      }
      free(body);
    }
    static const char kOk[] = "{\"status\":101,\"statusString\":\"OK\",\"spotifyError\":0}";
    httpd_resp_set_hdr(request, "Content-type", "application/json");
    return httpd_resp_send(request, kOk, sizeof(kOk) - 1);
  }

  void set_volume(int volume) {
    volume_ = std::max(0, std::min(volume, ADI_CSPOT_VOLUME_MAX));
    emit(ADI_CSPOT_EV_VOLUME, volume_);
    std::lock_guard<std::recursive_mutex> lock(spirc_mutex_);
    if (spirc_) spirc_->setRemoteVolume(volume_);
  }

  void set_pause(bool pause) {
    std::lock_guard<std::recursive_mutex> lock(spirc_mutex_);
    if (spirc_) spirc_->setPause(pause);
  }

 private:
  enum State { ABORT, LINKED, DISCO };

  adi_cspot_callbacks_t cb_;
  std::string name_, client_id_, client_secret_, credentials_;
  int port_ = 0, bitrate_ = 160;
  std::atomic<int> volume_{0};
  std::atomic<State> state_{ABORT};
  httpd_handle_t server_ = nullptr;
  bell::WrappedSemaphore client_connected_;
  std::shared_ptr<cspot::LoginBlob> blob_;
  std::recursive_mutex spirc_mutex_;  // Spirc can emit events synchronously from calls made under this lock
  std::unique_ptr<cspot::SpircHandler> spirc_;
  std::unique_ptr<bell::MDNSService> mdns_;
  std::mutex track_mutex_;
  std::string stream_track_id_;  // guarded by track_mutex_
  std::atomic<bool> track_end_{false};
  std::atomic<bool> notify_started_{false};  // new track audio began; tell Spotify from runTask()

  void emit(adi_cspot_event_t ev, int value = 0) {
    if (cb_.on_event) cb_.on_event(ev, value, cb_.ctx);
  }

  static esp_err_t get_trampoline(httpd_req_t *r) { return static_cast<Player *>(r->user_ctx)->handle_get(r); }
  static esp_err_t post_trampoline(httpd_req_t *r) { return static_cast<Player *>(r->user_ctx)->handle_post(r); }

  void enable_zeroconf() {
    httpd_uri_t uri = {};
    uri.uri = "/spotify_info";
    uri.method = HTTP_GET;
    uri.handler = &Player::get_trampoline;
    uri.user_ctx = this;
    httpd_register_uri_handler(server_, &uri);
    uri.method = HTTP_POST;
    uri.handler = &Player::post_trampoline;
    httpd_register_uri_handler(server_, &uri);

    // Advertise for the Spotify apps (requires mdns_init(), done by ESPHome's mdns component).
    mdns_ = bell::MDNSService::registerService(blob_->getDeviceName(), "_spotify-connect", "_tcp", "", port_,
                                               {{"VERSION", "1.0"}, {"CPath", "/spotify_info"}, {"Stack", "SP"}});
    ESP_LOGI(TAG, "Spotify Connect ZeroConf ready as \"%s\" (port %d)", name_.c_str(), port_);
  }

  void handle_event(std::unique_ptr<cspot::SpircHandler::Event> event) {
    using cspot::SpircHandler;
    switch (event->eventType) {
      case SpircHandler::EventType::PLAYBACK_START: {
        std::lock_guard<std::mutex> tl(track_mutex_);
        stream_track_id_.clear();
      }
        track_end_ = false;
        // Spotify servers do not send the volume at connection.
        {
          std::lock_guard<std::recursive_mutex> lock(spirc_mutex_);
          if (spirc_) spirc_->setRemoteVolume(volume_);
        }
        emit(ADI_CSPOT_EV_START, std::get<int>(event->data));
        break;
      case SpircHandler::EventType::PLAY_PAUSE:
        emit(std::get<bool>(event->data) ? ADI_CSPOT_EV_PAUSE : ADI_CSPOT_EV_PLAY);
        break;
      case SpircHandler::EventType::FLUSH:
      case SpircHandler::EventType::NEXT:
      case SpircHandler::EventType::PREV:
        emit(ADI_CSPOT_EV_FLUSH);
        break;
      case SpircHandler::EventType::SEEK:
        emit(ADI_CSPOT_EV_FLUSH, std::get<int>(event->data));
        break;
      case SpircHandler::EventType::DISC:
        emit(ADI_CSPOT_EV_DISC);
        state_ = DISCO;
        break;
      case SpircHandler::EventType::DEPLETED:
        track_end_ = true;
        break;
      case SpircHandler::EventType::VOLUME:
        volume_ = std::get<int>(event->data);
        emit(ADI_CSPOT_EV_VOLUME, volume_);
        break;
      default:
        break;
    }
  }

  size_t pcm_write(uint8_t *pcm, size_t bytes, std::string_view track_id) {
    std::unique_lock<std::mutex> track_lock(track_mutex_);
    if (stream_track_id_ != track_id) {
      // First audio of a new track. Spirc must not be called from here (this runs under
      // TrackPlayer's output mutex), so the player loop reports it - as Squeezelite does.
      stream_track_id_ = track_id;
      notify_started_ = true;
    }
    track_lock.unlock();
    return cb_.on_pcm ? cb_.on_pcm(pcm, bytes, cb_.ctx) : bytes;
  }

  // A cspot/bell call threw (network error, std::bad_alloc, ...). Previously nothing caught it
  // and std::terminate() rebooted the device, so the reboot -> auto-reconnect -> throw cycle
  // repeated forever. Tear the session down, report the heap, back off and retry the login that
  // is already loaded (stored or ZeroConf); give up after a few tries and wait for the Spotify
  // app to select the speaker again.
  void recover_after_failure(std::shared_ptr<cspot::Context> &ctx, int &failures, bool &use_stored) {
    log_heap("after Spotify failure");
    try {
      {
        std::lock_guard<std::recursive_mutex> lock(spirc_mutex_);
        if (spirc_) {
          spirc_->disconnect();
          spirc_.reset();
        }
      }
      ctx.reset();
    } catch (...) {
      ESP_LOGW(TAG, "error while cleaning up the failed Spotify session");
    }
    state_ = ABORT;
    emit(ADI_CSPOT_EV_DISC);
    failures++;
    if (failures > kMaxRetries) {
      ESP_LOGW(TAG, "giving up after %d failures; select \"%s\" in the Spotify app to reconnect", failures,
               name_.c_str());
      failures = 0;
      return;  // use_stored stays false: wait for ZeroConf
    }
    uint32_t delay_ms = std::min<uint32_t>(5000u * failures, 30000u);
    ESP_LOGW(TAG, "retrying Spotify connection in %u ms (attempt %d/%d)", (unsigned) delay_ms, failures,
             kMaxRetries);
    vTaskDelay(pdMS_TO_TICKS(delay_ms));
    use_stored = true;
  }

  static constexpr int kMaxRetries = 6;

  void runTask() override {
    blob_ = std::make_shared<cspot::LoginBlob>(name_);
    enable_zeroconf();  // always on, so any account can select "ADI Speaker"

    bool use_stored = !credentials_.empty();
    if (use_stored) {
      blob_->loadJson(credentials_);
      ESP_LOGI(TAG, "reconnecting with stored Spotify credentials");
    }

    int failures = 0;
    while (true) {
      if (!use_stored) client_connected_.wait();
      use_stored = false;

      std::shared_ptr<cspot::Context> ctx;
      try {
        ctx = cspot::Context::createFromBlob(blob_);
        if (bitrate_ == 320) ctx->config.audioFormat = AudioFormat_OGG_VORBIS_320;
        else if (bitrate_ == 96) ctx->config.audioFormat = AudioFormat_OGG_VORBIS_96;
        else ctx->config.audioFormat = AudioFormat_OGG_VORBIS_160;

        log_heap("before Spotify connect");
        ctx->session->connectWithRandomAp();
        ctx->config.authData = ctx->session->authenticate(blob_);
        ctx->config.clientId = client_id_;
        ctx->config.clientSecret = client_secret_;

        if (ctx->config.authData.empty()) {
          ESP_LOGW(TAG, "Spotify login failed; select \"%s\" in the Spotify app again", name_.c_str());
          ctx.reset();
          continue;
        }

        if (cb_.on_credentials) cb_.on_credentials(ctx->getCredentialsJson().c_str(), cb_.ctx);

        {
          std::lock_guard<std::recursive_mutex> lock(spirc_mutex_);
          spirc_ = std::make_unique<cspot::SpircHandler>(ctx);
          spirc_->getTrackPlayer()->setDataCallback(
              [this](uint8_t *data, size_t bytes, std::string_view id) { return pcm_write(data, bytes, id); });
          spirc_->setEventHandler(
              [this](std::unique_ptr<cspot::SpircHandler::Event> ev) { handle_event(std::move(ev)); });
        }
        state_ = LINKED;
        failures = 0;
        ctx->session->startTask();
        emit(ADI_CSPOT_EV_LINKED);
        emit(ADI_CSPOT_EV_VOLUME, volume_);

        while (state_ == LINKED || state_ == DISCO) {
          ctx->session->handlePacket();
          if (notify_started_.exchange(false)) {
            // Accuracy ~ the local audio buffer length (about a second).
            std::string id;
            {
              std::lock_guard<std::mutex> tl(track_mutex_);
              id = stream_track_id_;
            }
            std::lock_guard<std::recursive_mutex> lock(spirc_mutex_);
            if (spirc_) spirc_->notifyAudioReachedPlayback(id);
          }
          if (track_end_ && cb_.buffered_ms && cb_.buffered_ms(cb_.ctx) == 0) {
            track_end_ = false;
            emit(ADI_CSPOT_EV_STOP);
            std::lock_guard<std::recursive_mutex> lock(spirc_mutex_);
            if (spirc_) spirc_->notifyAudioEnded();
          }
          // Another device took over: stay connected (ZeroConf) so it can come back.
          if (state_ == DISCO) state_ = LINKED;
        }

        {
          std::lock_guard<std::recursive_mutex> lock(spirc_mutex_);
          spirc_->disconnect();
          spirc_.reset();
        }
        ESP_LOGI(TAG, "Spotify session closed");
        ctx.reset();
      } catch (const std::exception &e) {
        ESP_LOGE(TAG, "Spotify session error: %s", e.what());
        recover_after_failure(ctx, failures, use_stored);
      } catch (...) {
        ESP_LOGE(TAG, "Spotify session error: unknown exception");
        recover_after_failure(ctx, failures, use_stored);
      }
    }
  }
};

Player *g_player = nullptr;

}  // namespace

// Called by cspot's TrackPlayer when a track cannot be loaded/opened (weak reference there):
// records the internal/PSRAM heap state at the moment of the failure.
extern "C" void adi_cspot_log_heap(const char *where) { log_heap(where); }

extern "C" bool adi_cspot_start(const adi_cspot_config_t *config, const adi_cspot_callbacks_t *callbacks) {
  if (g_player != nullptr || config == nullptr || callbacks == nullptr) return false;
  bell::setDefaultLogger();
  enable_psram_malloc();
  g_player = new Player(*config, *callbacks);
  if (!g_player->start_http()) {
    delete g_player;
    g_player = nullptr;
    return false;
  }
  return g_player->startTask();
}

extern "C" void adi_cspot_set_volume(int volume) {
  if (g_player) g_player->set_volume(volume);
}

extern "C" void adi_cspot_set_pause(bool pause) {
  if (g_player) g_player->set_pause(pause);
}
