#pragma once

#include "esphome/core/component.h"
#include "esphome/core/automation.h"
#include "esphome/components/microphone/microphone.h"
#include "esphome/components/speaker/speaker.h"
#include "esphome/components/micro_wake_word/micro_wake_word.h"
#include "esphome/components/ring_buffer/ring_buffer.h"

#include <esp_websocket_client.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include <atomic>
#include <deque>
#include <memory>
#include <string>
#include <vector>

namespace esphome::adi_voice {

// Device states reported to on_state (used for the LED ring).
//  "connecting" | "idle" | "listening" | "processing" | "speaking" | "error"
class AdiVoice : public Component {
 public:
  void set_microphone(microphone::Microphone *m) { this->mic_ = m; }
  void set_speaker(speaker::Speaker *s) { this->speaker_ = s; }
  void set_wake_word(micro_wake_word::MicroWakeWord *w) { this->mww_ = w; }
  void set_uri(const std::string &v) { this->uri_ = v; }
  void set_device_id(const std::string &v) { this->device_id_ = v; }
  void set_device_name(const std::string &v) { this->device_name_ = v; }
  void set_token(const std::string &v) { this->token_ = v; }
  void set_max_record_ms(uint32_t v) { this->max_record_ms_ = v; }
  void set_silence_ms(uint32_t v) { this->silence_ms_ = v; }
  void set_no_speech_ms(uint32_t v) { this->no_speech_ms_ = v; }

  void add_state_callback(std::function<void(const std::string &)> &&cb) { this->state_cbs_.push_back(std::move(cb)); }
  void add_connection_callback(std::function<void(bool)> &&cb) { this->conn_cbs_.push_back(std::move(cb)); }

  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  /// Wake word / button: start a recording (or interrupt playback).
  void start_listening();
  /// Cancel whatever is going on and return to idle.
  void stop();

 protected:
  enum class Phase : uint8_t { IDLE, RECORDING, WAITING, SPEAKING };

  static void ws_event_(void *arg, esp_event_base_t base, int32_t id, void *data);
  void on_ws_event_(int32_t id, esp_websocket_event_data_t *d);
  void handle_text_(const std::string &msg);

  // ---- outgoing WebSocket traffic -----------------------------------------------------
  // Every frame to the backend goes through ONE sender task. A websocket write waits for the
  // TCP send buffer to drain (it needs ACKs from the backend), so it must never run on the
  // ESPHome main loop: it froze the wake word, LEDs and Spotify feed for up to 1.5 s and, on
  // timeout, made the library drop the connection.
  enum class OutKind : uint8_t { HELLO, PONG, AUDIO_START, AUDIO_CHUNK, AUDIO_END, AUDIO_CANCEL };
  struct OutMsg {
    OutKind kind{OutKind::HELLO};
    uint32_t gen{0};           // connection generation it was queued for (stale ones are dropped)
    std::string text;          // text frames
    std::vector<uint8_t> bin;  // binary frames (audio)
  };
  struct UploadStats {  // one recording, sender task only
    uint32_t started_ms{0}, frames{0}, bytes{0}, slow{0}, max_ms{0};
  };
  static const char *out_kind_name_(OutKind k);
  static void ws_sender_task_(void *arg);
  void ws_sender_loop_();
  void write_out_(OutMsg &m);
  bool enqueue_(OutKind kind, std::string text, std::vector<uint8_t> bin);
  void queue_text_(OutKind kind, const std::string &s);
  void drain_out_queue_();
  void log_ws_state_(const char *why, int level);  // 0 debug, 1 info, 2 warn
  void send_hello_();
  void begin_recording_();
  void finish_recording_(bool cancel);
  void show_error_();
  void on_text_frame_ws_(const std::string &msg);
  void feed_speaker_();
  void set_phase_(Phase p);
  void publish_state_(const char *s);
  void reset_playback_();

  microphone::Microphone *mic_{nullptr};
  speaker::Speaker *speaker_{nullptr};
  micro_wake_word::MicroWakeWord *mww_{nullptr};
  std::string uri_, device_id_, device_name_, token_;
  uint32_t max_record_ms_{12000}, silence_ms_{1200}, no_speech_ms_{5000};

  esp_websocket_client_handle_t client_{nullptr};
  QueueHandle_t out_q_{nullptr};
  TaskHandle_t sender_task_{nullptr};
  std::atomic<size_t> out_bytes_{0};          // audio bytes queued / being written
  std::atomic<bool> write_in_progress_{false};
  std::atomic<uint32_t> ws_gen_{0};
  std::atomic<uint32_t> last_rx_ms_{0}, last_tx_ms_{0}, dropped_{0};
  uint32_t ping_rx_ms_{0};
  UploadStats up_{};                          // sender task only
  uint32_t last_slow_log_ms_{0};              // sender task only
  uint32_t last_rx_block_log_ms_{0};          // websocket task only
  uint32_t ws_state_log_ms_{0};               // main loop only
  bool ws_started_{false};
  volatile bool connected_{false};
  volatile bool welcomed_{false};
  volatile bool pending_hello_{false};

  Phase phase_{Phase::IDLE};
  std::string last_state_;
  uint32_t seq_{0};
  std::string current_id_;

  // recording
  SemaphoreHandle_t lock_{nullptr};
  std::vector<uint8_t> rec_buf_;
  std::atomic<bool> recording_{false};
  uint32_t rec_start_{0};
  uint32_t last_speech_{0};
  bool speech_seen_{false};
  uint64_t audio_abs_sum_{0};
  uint64_t audio_sq_sum_{0};
  uint32_t audio_sample_count_{0};
  int16_t audio_peak_{0};
  uint32_t mic_rate_{16000};
  uint8_t mic_channels_{1};

  // incoming (written by the websocket task, consumed in loop())
  std::string text_acc_;
  std::deque<std::string> text_q_;
  uint8_t last_op_{1};
  // Reply audio lives in PSRAM. A std::deque<uint8_t> allocates hundreds of tiny blocks
  // in internal RAM and aborts (operator new) on a normal-length reply.
  std::unique_ptr<ring_buffer::RingBuffer> play_rb_;
  std::atomic<bool> accept_audio_{false};  // armed by the websocket task on a matching audio_output_start
  std::atomic<uint32_t> active_seq_{0};    // numeric part of current_id_ ("a12" -> 12), readable from any task
  std::vector<uint8_t> tmp_;
  size_t tmp_off_{0};
  size_t wav_skip_{0};
  uint32_t audio_done_at_{0};
  bool audio_in_progress_{false};
  bool audio_end_seen_{false};
  bool keep_listening_{false};
  uint32_t speak_started_{0};
  uint32_t wait_started_{0};

  std::vector<std::function<void(const std::string &)>> state_cbs_;
  std::vector<std::function<void(bool)>> conn_cbs_;
  volatile int pending_conn_event_{-1};
};

template<typename... Ts> class StartAction : public Action<Ts...>, public Parented<AdiVoice> {
 public:
  using Parented<AdiVoice>::Parented;
  void play(const Ts &...) override { this->parent_->start_listening(); }
};
template<typename... Ts> class StopAction : public Action<Ts...>, public Parented<AdiVoice> {
 public:
  using Parented<AdiVoice>::Parented;
  void play(const Ts &...) override { this->parent_->stop(); }
};

class StateTrigger : public Trigger<std::string> {
 public:
  explicit StateTrigger(AdiVoice *p) {
    p->add_state_callback([this](const std::string &s) { this->trigger(s); });
  }
};
class ConnectionTrigger : public Trigger<> {
 public:
  ConnectionTrigger(AdiVoice *p, bool on_connect) {
    p->add_connection_callback([this, on_connect](bool c) {
      if (c == on_connect) this->trigger();
    });
  }
};

}  // namespace esphome::adi_voice
