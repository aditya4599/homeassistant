#include "adi_spotify.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/components/network/util.h"

#include <nvs.h>

#include <algorithm>
#include <cmath>
#include <cstring>

namespace esphome {
namespace adi_spotify {

static const char *const TAG = "adi_spotify";

static constexpr uint32_t BYTES_PER_MS = 44100 * 2 * 2 / 1000;  // 44.1 kHz, stereo, 16-bit
static constexpr size_t FEED_CHUNK = 4096;
static constexpr const char *NVS_NAMESPACE = "adi_spotify";
static constexpr const char *NVS_KEY_CREDS = "creds";
static constexpr uint32_t VOLUME_SAVE_DELAY_MS = 5000;

// Perceptual curve: Spotify's slider is linear; squaring it sounds much more even.
static int32_t gain_for(int volume) {
  float n = std::clamp(volume / float(ADI_CSPOT_VOLUME_MAX), 0.0f, 1.0f);
  return static_cast<int32_t>(n * n * 32768.0f + 0.5f);
}

void AdiSpotify::setup() {
  this->cred_lock_ = xSemaphoreCreateMutex();
  this->events_ = xQueueCreate(32, sizeof(Event));
  // ring_buffer::RingBuffer::create prefers PSRAM.
  this->rb_ = ring_buffer::RingBuffer::create(this->buffer_ms_ * BYTES_PER_MS);
  if (this->cred_lock_ == nullptr || this->events_ == nullptr || this->rb_ == nullptr) {
    ESP_LOGE(TAG, "out of memory (is PSRAM enabled?)");
    this->mark_failed();
    return;
  }
  this->tmp_.reserve(FEED_CHUNK);

  this->volume_pref_ = global_preferences->make_preference<float>(fnv1_hash("adi_spotify_volume"));
  float saved;
  float initial = this->volume_pref_.load(&saved) ? saved : this->initial_volume_;
  this->apply_volume_(static_cast<int>(std::clamp(initial, 0.0f, 1.0f) * ADI_CSPOT_VOLUME_MAX + 0.5f));

  this->load_credentials_();
  this->speaker_->set_audio_stream_info(audio::AudioStreamInfo(16, 2, 44100));
}

void AdiSpotify::dump_config() {
  ESP_LOGCONFIG(TAG, "ADI Spotify Connect:");
  ESP_LOGCONFIG(TAG, "  Name: %s", this->name_.c_str());
  ESP_LOGCONFIG(TAG, "  ZeroConf port: %u", this->port_);
  ESP_LOGCONFIG(TAG, "  Bitrate: %d kbps, buffer: %u ms", this->bitrate_, (unsigned) this->buffer_ms_);
  ESP_LOGCONFIG(TAG, "  Stored login: %s", this->stored_credentials_.empty() ? "no (select it in Spotify)" : "yes");
}

// ---------------------------------------------------------------- cspot callbacks (cspot tasks)
size_t AdiSpotify::on_pcm_(uint8_t *pcm, size_t bytes, void *ctx) {
  auto *self = static_cast<AdiSpotify *>(ctx);
  size_t n = std::min(bytes, self->rb_->free()) & ~static_cast<size_t>(3);  // whole stereo frames
  if (n == 0) return 0;  // full: cspot sleeps 50 ms and retries (back-pressure)
  // Scale exactly the bytes we accept; cspot re-offers the rest unscaled next time.
  int32_t g = self->gain_q15_.load();
  if (g < 32768) {
    auto *s = reinterpret_cast<int16_t *>(pcm);
    for (size_t i = 0; i < n / 2; i++) s[i] = static_cast<int16_t>((s[i] * g) >> 15);
  }
  return self->rb_->write_without_replacement(pcm, n, 0, false);
}

void AdiSpotify::on_event_(adi_cspot_event_t event, int value, void *ctx) {
  auto *self = static_cast<AdiSpotify *>(ctx);
  switch (event) {
    // Flushes must happen here, in order with the audio cspot writes next.
    case ADI_CSPOT_EV_START:
    case ADI_CSPOT_EV_FLUSH:
    case ADI_CSPOT_EV_DISC:
      self->rb_->reset();
      break;
    case ADI_CSPOT_EV_VOLUME:
      self->volume_ = value;
      self->gain_q15_ = gain_for(value);
      break;
    default:
      break;
  }
  Event ev{event, value};
  xQueueSend(self->events_, &ev, 0);
}

uint32_t AdiSpotify::buffered_ms_(void *ctx) {
  auto *self = static_cast<AdiSpotify *>(ctx);
  uint32_t ms = self->rb_->available() / BYTES_PER_MS;
  if (self->speaker_->has_buffered_data()) ms += 1;
  return ms;
}

void AdiSpotify::on_credentials_(const char *json, void *ctx) {
  auto *self = static_cast<AdiSpotify *>(ctx);
  // NVS writes are not allowed from cspot's PSRAM-stack task; loop() persists it.
  if (xSemaphoreTake(self->cred_lock_, pdMS_TO_TICKS(100)) == pdTRUE) {
    self->pending_credentials_ = json ? json : "";
    self->credentials_pending_ = true;
    xSemaphoreGive(self->cred_lock_);
  }
}

// ---------------------------------------------------------------- main loop
void AdiSpotify::loop() {
  if (!this->started_ && network::is_connected()) {
    adi_cspot_config_t cfg{};
    cfg.name = this->name_.c_str();
    cfg.port = this->port_;
    cfg.bitrate = this->bitrate_;
    cfg.initial_volume = this->volume_;
    cfg.client_id = this->client_id_.c_str();
    cfg.client_secret = this->client_secret_.c_str();
    cfg.credentials = this->stored_credentials_.c_str();
    adi_cspot_callbacks_t cb{};
    cb.on_pcm = &AdiSpotify::on_pcm_;
    cb.on_event = &AdiSpotify::on_event_;
    cb.buffered_ms = &AdiSpotify::buffered_ms_;
    cb.on_credentials = &AdiSpotify::on_credentials_;
    cb.ctx = this;
    this->started_ = true;  // never retry in a tight loop
    if (!adi_cspot_start(&cfg, &cb)) {
      ESP_LOGE(TAG, "could not start Spotify Connect");
      this->mark_failed();
      return;
    }
  }

  Event ev;
  while (xQueueReceive(this->events_, &ev, 0) == pdTRUE) {
    switch (ev.type) {
      case ADI_CSPOT_EV_LINKED:
        ESP_LOGI(TAG, "Spotify account connected");
        this->publish_state_("idle");
        break;
      case ADI_CSPOT_EV_START:
      case ADI_CSPOT_EV_PLAY:
        this->paused_ = false;
        this->publish_state_("playing");
        break;
      case ADI_CSPOT_EV_PAUSE:
        this->paused_ = true;
        this->publish_state_("paused");
        break;
      case ADI_CSPOT_EV_STOP:
      case ADI_CSPOT_EV_DISC:
        this->paused_ = true;
        this->tmp_.clear();
        this->tmp_off_ = 0;
        if (this->speaker_->is_running()) this->speaker_->stop();
        this->publish_state_("idle");
        break;
      case ADI_CSPOT_EV_FLUSH:
        this->tmp_.clear();
        this->tmp_off_ = 0;
        break;
      case ADI_CSPOT_EV_VOLUME:
        ESP_LOGD(TAG, "volume %.0f%%", this->get_volume() * 100.0f);
        this->volume_dirty_since_ = millis() | 1;
        break;
    }
  }

  if (!this->paused_) this->feed_speaker_();
  this->decay_level_();

  if (this->volume_dirty_since_ && millis() - this->volume_dirty_since_ > VOLUME_SAVE_DELAY_MS) {
    this->volume_dirty_since_ = 0;
    float v = this->get_volume();
    this->volume_pref_.save(&v);
  }

  bool pending = false;
  std::string creds;
  if (xSemaphoreTake(this->cred_lock_, 0) == pdTRUE) {
    if (this->credentials_pending_) {
      pending = true;
      creds.swap(this->pending_credentials_);
      this->credentials_pending_ = false;
    }
    xSemaphoreGive(this->cred_lock_);
  }
  if (pending && creds != this->stored_credentials_) this->store_credentials_(creds);
}

void AdiSpotify::feed_speaker_() {
  if (this->tmp_off_ >= this->tmp_.size()) {
    size_t n = std::min(FEED_CHUNK, this->rb_->available()) & ~static_cast<size_t>(3);
    this->tmp_.resize(n);
    this->tmp_off_ = 0;
    if (n > 0) {
      this->tmp_.resize(this->rb_->read(this->tmp_.data(), n, 0));
      this->update_level_(this->tmp_.data(), this->tmp_.size());  // loudness for the ring
    }
  }
  if (this->tmp_off_ >= this->tmp_.size()) return;
  if (!this->speaker_->is_running()) {
    this->speaker_->set_audio_stream_info(audio::AudioStreamInfo(16, 2, 44100));
    this->speaker_->start();
  }
  this->tmp_off_ += this->speaker_->play(this->tmp_.data() + this->tmp_off_, this->tmp_.size() - this->tmp_off_, 0);
}

// Loudness of the chunk that is about to be played (main loop, ~every 23 ms while playing).
// Measured here rather than in cspot's PCM callback: it costs cspot nothing, needs no
// cross-task buffering, and is ~1 s closer to what is actually audible than the ring buffer
// input. ~128 samples per 4 KB chunk, no allocation.
void AdiSpotify::update_level_(const uint8_t *data, size_t bytes) {
  const float gain = this->gain_q15_.load() / 32768.0f;
  const float dt = static_cast<float>(bytes) / 176400.0f;  // 44.1 kHz * 2 ch * 2 bytes
  const float level = this->meter_.process(reinterpret_cast<const int16_t *>(data), bytes / 4, gain, dt);
  this->level_.store(level, std::memory_order_relaxed);
  this->level_ms_ = millis();
}

// No fresh audio (paused, stopped, starved): let the level fall back to 0.
void AdiSpotify::decay_level_() {
  const uint32_t now = millis();
  if (now - this->level_ms_ > 120 && this->meter_.level() > 0.001f) {
    const float dt = std::min(static_cast<float>(now - this->decay_ms_) * 0.001f, 0.25f);
    this->level_.store(this->meter_.decay(dt), std::memory_order_relaxed);
  }
  this->decay_ms_ = now;
}

void AdiSpotify::publish_state_(const char *state) {
  if (this->state_ == state) return;
  this->state_ = state;
  ESP_LOGI(TAG, "state -> %s", state);
  if (this->state_ == "playing") this->high_freq_.start();
  else this->high_freq_.stop();
  this->state_callbacks_.call(this->state_);
}

void AdiSpotify::apply_volume_(int volume) {
  this->volume_ = std::clamp(volume, 0, ADI_CSPOT_VOLUME_MAX);
  this->gain_q15_ = gain_for(this->volume_);
}

void AdiSpotify::set_volume(float volume) {
  int v = static_cast<int>(std::clamp(volume, 0.0f, 1.0f) * ADI_CSPOT_VOLUME_MAX + 0.5f);
  this->apply_volume_(v);
  if (this->started_) adi_cspot_set_volume(v);  // also tells the Spotify apps
  this->volume_dirty_since_ = millis() | 1;
}

// ---------------------------------------------------------------- credential storage (NVS)
void AdiSpotify::load_credentials_() {
  nvs_handle_t h;
  if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return;
  size_t len = 0;
  if (nvs_get_str(h, NVS_KEY_CREDS, nullptr, &len) == ESP_OK && len > 1 && len < 4096) {
    std::string buf(len, '\0');
    if (nvs_get_str(h, NVS_KEY_CREDS, buf.data(), &len) == ESP_OK) {
      buf.resize(len - 1);
      this->stored_credentials_ = std::move(buf);
    }
  }
  nvs_close(h);
}

void AdiSpotify::store_credentials_(const std::string &json) {
  nvs_handle_t h;
  if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) {
    ESP_LOGW(TAG, "could not open NVS to store the Spotify login");
    return;
  }
  if (nvs_set_str(h, NVS_KEY_CREDS, json.c_str()) == ESP_OK && nvs_commit(h) == ESP_OK) {
    this->stored_credentials_ = json;
    ESP_LOGI(TAG, "Spotify login stored; it reconnects automatically after reboot");
  }
  nvs_close(h);
}

}  // namespace adi_spotify
}  // namespace esphome
