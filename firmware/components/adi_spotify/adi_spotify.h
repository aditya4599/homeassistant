#pragma once

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/core/preferences.h"
#include "esphome/components/ring_buffer/ring_buffer.h"
#include "esphome/components/speaker/speaker.h"

#include "adi_ring.h"

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>

#include <atomic>
#include <memory>
#include <string>
#include <vector>

#include "adi_cspot.h"

namespace esphome {
namespace adi_spotify {

// Spotify Connect receiver. Audio: cspot (44.1 kHz stereo) -> PSRAM ring buffer -> speaker.
// Volume is normalized 0.0..1.0 internally (Spotify Connect uses 0..65535) and only scales
// the music, never the assistant's voice.
class AdiSpotify : public Component {
 public:
  void set_speaker(speaker::Speaker *s) { this->speaker_ = s; }
  void set_device_name(const std::string &v) { this->name_ = v; }
  void set_client_id(const std::string &v) { this->client_id_ = v; }
  void set_client_secret(const std::string &v) { this->client_secret_ = v; }
  void set_port(uint16_t v) { this->port_ = v; }
  void set_bitrate(int v) { this->bitrate_ = v; }
  void set_initial_volume(float v) { this->initial_volume_ = v; }
  void set_buffer_ms(uint32_t v) { this->buffer_ms_ = v; }

  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  /// "disconnected", "idle", "playing" or "paused".
  const std::string &get_state() const { return this->state_; }
  bool is_playing() const { return this->state_ == "playing"; }
  /// Normalized Spotify volume 0.0..1.0.
  float get_volume() const { return this->volume_ / float(ADI_CSPOT_VOLUME_MAX); }
  /// Smoothed loudness of the Spotify PCM, 0.0..1.0 (drives the status ring). Safe to read
  /// from any task; written only by the ESPHome main loop.
  float get_level() const { return this->level_.load(std::memory_order_relaxed); }
  /// Set the Spotify volume (0.0..1.0); reported back to the Spotify apps.
  void set_volume(float volume);

  void add_on_state_callback(std::function<void(const std::string &)> &&cb) {
    this->state_callbacks_.add(std::move(cb));
  }

 protected:
  struct Event {
    adi_cspot_event_t type;
    int value;
  };

  static size_t on_pcm_(uint8_t *pcm, size_t bytes, void *ctx);
  static void on_event_(adi_cspot_event_t event, int value, void *ctx);
  static uint32_t buffered_ms_(void *ctx);
  static void on_credentials_(const char *json, void *ctx);

  void apply_volume_(int volume);
  void publish_state_(const char *state);
  void feed_speaker_();
  void update_level_(const uint8_t *data, size_t bytes);
  void decay_level_();
  void load_credentials_();
  void store_credentials_(const std::string &json);

  speaker::Speaker *speaker_{nullptr};
  std::string name_, client_id_, client_secret_;
  uint16_t port_{8090};
  int bitrate_{160};
  float initial_volume_{0.5f};
  uint32_t buffer_ms_{1000};

  bool started_{false};
  std::string stored_credentials_;
  SemaphoreHandle_t cred_lock_{nullptr};
  std::string pending_credentials_;
  bool credentials_pending_{false};

  std::unique_ptr<ring_buffer::RingBuffer> rb_;
  QueueHandle_t events_{nullptr};
  std::atomic<int> volume_{0};        // 0..65535
  std::atomic<int32_t> gain_q15_{0};  // 32768 = unity
  std::atomic<bool> paused_{true};
  ESPPreferenceObject volume_pref_;
  uint32_t volume_dirty_since_{0};

  std::vector<uint8_t> tmp_;
  size_t tmp_off_{0};
  std::string state_{"disconnected"};
  AdiLevelMeter meter_;
  std::atomic<float> level_{0.0f};
  uint32_t level_ms_{0};  // when a chunk was last measured
  uint32_t decay_ms_{0};
  CallbackManager<void(const std::string &)> state_callbacks_;
  HighFrequencyLoopRequester high_freq_;
};

class StateTrigger : public Trigger<std::string> {
 public:
  explicit StateTrigger(AdiSpotify *parent) {
    parent->add_on_state_callback([this](const std::string &s) { this->trigger(s); });
  }
};

}  // namespace adi_spotify
}  // namespace esphome
