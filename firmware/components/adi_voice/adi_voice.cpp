#include "adi_voice.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/components/network/util.h"
#include "esphome/components/json/json_util.h"

#include <esp_timer.h>

#include <algorithm>
#include <cstring>
#include <new>

namespace esphome::adi_voice {

static const char *const TAG = "adi_voice";
// 50 ms of 16 kHz mono 16-bit. Kept below lwIP's "socket is writable" threshold (TCP_SNDLOWAT =
// 2881 bytes with this build's 5760-byte send buffer) so one frame never has to wait for ACKs
// part-way through; the old 3200 bytes always did.
static constexpr size_t SEND_CHUNK = 1600;
// Per-write timeout used by the websocket library BOTH to wait for the socket to become
// writable and (on timeout) to declare the connection dead. It only matters to the sender task
// now, so it no longer freezes anything. 5 s is the library's own network_timeout_ms; it must
// stay below CONFIG_ESP_WS_CLIENT_TX_LOCK_TIMEOUT_MS (7 s, see __init__.py) so a slow write
// never makes the library's auto-PONG give up and abort the connection.
static constexpr uint32_t WS_WRITE_TIMEOUT_MS = 5000;
static constexpr uint32_t WS_SLOW_WRITE_MS = 50;           // anything slower is logged
static constexpr size_t MAX_QUEUED_AUDIO_BYTES = 24 * 1024;  // ~0.75 s; the rest stays in rec_buf_
static constexpr size_t OUT_QUEUE_LEN = 32;
static constexpr uint32_t WS_STATE_PERIOD_MS = 30000;
static inline uint32_t now_ms() { return static_cast<uint32_t>(esp_timer_get_time() / 1000); }
static constexpr size_t PLAY_CHUNK = 2048;
static constexpr uint32_t WAIT_TIMEOUT_MS = 30000;
static constexpr size_t PLAY_RB_SIZE = 512 * 1024;  // ~16 s of 16 kHz mono 16-bit, in PSRAM

// "a12" -> 12; anything else -> 0 (never matches an active recording).
static uint32_t seq_of(const char *id) {
  if (id == nullptr || id[0] != 'a') return 0;
  return static_cast<uint32_t>(strtoul(id + 1, nullptr, 10));
}
// 1.0 = send raw mic audio. Raw speech already peaks around 4700-9400 on this board;
// 8x clipped it. The backend (DEVICE_AUDIO_NORMALIZE=true) boosts quiet audio without clipping.
static constexpr float MIC_INPUT_GAIN = 1.0f;

static std::string json_escape(const std::string &in) {
  std::string out;
  for (char c : in) {
    if (c == '"' || c == '\\') { out += '\\'; out += c; }
    else if (static_cast<unsigned char>(c) < 0x20) { out += ' '; }
    else out += c;
  }
  return out;
}

static void amplify_pcm_s16le_(std::vector<uint8_t> &data, float gain) {
  if (data.size() < sizeof(int16_t) || gain <= 0.0f) return;

  const size_t samples = data.size() / sizeof(int16_t);
  int16_t *p = reinterpret_cast<int16_t *>(data.data());

  for (size_t i = 0; i < samples; ++i) {
    const float amplified = static_cast<float>(p[i]) * gain;
    if (amplified > 32767.0f) {
      p[i] = 32767;
    } else if (amplified < -32768.0f) {
      p[i] = -32768;
    } else {
      p[i] = static_cast<int16_t>(amplified);
    }
  }
}

static void update_audio_stats_(std::vector<uint8_t> const &data, uint64_t *abs_sum,
                                uint64_t *sq_sum, uint32_t *count, int16_t *peak) {
  const size_t samples = data.size() / sizeof(int16_t);
  const int16_t *p = reinterpret_cast<const int16_t *>(data.data());
  for (size_t i = 0; i < samples; i++) {
    const int32_t v = p[i];
    const uint32_t a = static_cast<uint32_t>(v < 0 ? -v : v);
    *abs_sum += a;
    *sq_sum += static_cast<uint64_t>(v) * static_cast<int64_t>(v);
    *count += 1;
    if (a > static_cast<uint32_t>(*peak < 0 ? -*peak : *peak)) *peak = p[i];
  }
}

void AdiVoice::setup() {
  this->lock_ = xSemaphoreCreateMutex();
  this->play_rb_ = ring_buffer::RingBuffer::create(PLAY_RB_SIZE);
  if (this->play_rb_ == nullptr) {
    ESP_LOGE(TAG, "could not allocate the playback buffer (is PSRAM enabled?)");
    this->mark_failed();
    return;
  }
  this->out_q_ = xQueueCreate(OUT_QUEUE_LEN, sizeof(OutMsg *));
  if (this->out_q_ == nullptr ||
      xTaskCreate(&AdiVoice::ws_sender_task_, "adi_ws_tx", 6144, this, 4, &this->sender_task_) != pdPASS) {
    ESP_LOGE(TAG, "could not start the websocket sender task");
    this->mark_failed();
    return;
  }
  // The backend is asked for 16 kHz mono 16-bit PCM in hello; tell the speaker explicitly.
  this->speaker_->set_audio_stream_info(audio::AudioStreamInfo(16, 1, 16000));
  // Passive listener: the wake-word engine owns the mic; we only copy audio while recording.
  this->mic_->add_data_callback([this](const std::vector<uint8_t> &data) {
    if (!this->recording_) return;
    if (xSemaphoreTake(this->lock_, pdMS_TO_TICKS(5)) == pdTRUE) {
      // Cap the buffer (~10 s) so a stalled network can't exhaust RAM.
      if (this->rec_buf_.size() < 320000) {
        this->rec_buf_.insert(this->rec_buf_.end(), data.begin(), data.end());
        if (this->mic_channels_ == 1) {
          update_audio_stats_(data, &this->audio_abs_sum_, &this->audio_sq_sum_, &this->audio_sample_count_,
                              &this->audio_peak_);
        }
      }
      xSemaphoreGive(this->lock_);
    }
  });
  this->publish_state_("connecting");
}

void AdiVoice::dump_config() {
  ESP_LOGCONFIG(TAG, "ADI voice:");
  ESP_LOGCONFIG(TAG, "  Backend: %s", this->uri_.c_str());
  ESP_LOGCONFIG(TAG, "  Device ID: %s", this->device_id_.c_str());
  ESP_LOGCONFIG(TAG, "  Silence to end: %u ms, max recording: %u ms", (unsigned) this->silence_ms_,
                (unsigned) this->max_record_ms_);
}

void AdiVoice::publish_state_(const char *s) {
  if (this->last_state_ == s) return;
  this->last_state_ = s;
  ESP_LOGD(TAG, "state -> %s", s);
  for (auto &cb : this->state_cbs_) cb(this->last_state_);
}

void AdiVoice::set_phase_(Phase p) { this->phase_ = p; }

// Hold "error" on the ring briefly (publishing error+idle back to back hid it).
void AdiVoice::show_error_() {
  this->publish_state_("error");
  this->set_timeout("adi_err", 1500, [this]() {
    if (this->phase_ == Phase::IDLE && this->last_state_ == "error") this->publish_state_("idle");
  });
}

// ------------------------------------------------------------------ websocket plumbing
void AdiVoice::ws_event_(void *arg, esp_event_base_t base, int32_t id, void *data) {
  static_cast<AdiVoice *>(arg)->on_ws_event_(id, static_cast<esp_websocket_event_data_t *>(data));
}

void AdiVoice::on_ws_event_(int32_t id, esp_websocket_event_data_t *d) {
  switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
      this->ws_gen_++;
      this->last_rx_ms_ = this->last_tx_ms_ = now_ms();
      this->connected_ = true;
      this->pending_hello_ = true;
      this->pending_conn_event_ = 1;
      this->log_ws_state_("connected", 1);
      break;
    case WEBSOCKET_EVENT_DISCONNECTED:
    case WEBSOCKET_EVENT_CLOSED:
      // Logged first: shows whether a write was in flight when the connection died.
      this->log_ws_state_(id == WEBSOCKET_EVENT_CLOSED ? "closed" : "disconnected", 2);
      this->connected_ = false;
      this->welcomed_ = false;
      this->ws_gen_++;
      this->pending_conn_event_ = 0;
      break;
    case WEBSOCKET_EVENT_DATA: {
      this->last_rx_ms_ = now_ms();
      if (d->op_code == 0x09) {
        ESP_LOGD(TAG, "[WS-PING] server PING received (the library answers with a PONG)");
        break;
      }
      if (d->op_code == 0x0A) {
        ESP_LOGD(TAG, "[WS-PING] PONG received (answer to our keepalive PING)");
        break;
      }
      if (d->op_code == 0x08) {
        ESP_LOGW(TAG, "[WS-STATE] server sent a CLOSE frame");
        break;
      }
      uint8_t op = d->op_code;
      if (op == 0x00) op = this->last_op_;  // continuation frame
      else this->last_op_ = op;
      if (op == 0x01) {  // text: accumulate until the message is complete
        this->text_acc_.append(static_cast<const char *>(d->data_ptr), d->data_len);
        bool complete = (d->payload_offset + d->data_len >= d->payload_len) && d->fin;
        if (complete) {
          // Must run here, in arrival order: the binary audio that follows this message
          // is delivered by this same task before loop() ever sees the text.
          this->on_text_frame_ws_(this->text_acc_);
          if (xSemaphoreTake(this->lock_, pdMS_TO_TICKS(50)) == pdTRUE) {
            if (this->text_q_.size() < 32) this->text_q_.push_back(std::move(this->text_acc_));
            xSemaphoreGive(this->lock_);
          }
          this->text_acc_.clear();
        }
      } else if (op == 0x02 && d->data_len > 0 && this->accept_audio_) {  // binary: reply audio
        const uint8_t *p = reinterpret_cast<const uint8_t *>(d->data_ptr);
        size_t n = d->data_len;
        size_t skip = std::min(this->wav_skip_, n);  // strip a WAV header if one was announced
        p += skip;
        n -= skip;
        this->wav_skip_ -= skip;
        // Blocking briefly here is intentional back-pressure while the speaker drains. It does
        // stall THIS task (no reads, no PONGs) while it waits, so time it.
        const uint32_t t0 = now_ms();
        const size_t w = n > 0 ? this->play_rb_->write_without_replacement(p, n, pdMS_TO_TICKS(2000)) : 0;
        const uint32_t dt = now_ms() - t0;
        if (dt > WS_SLOW_WRITE_MS && t0 - this->last_rx_block_log_ms_ > 1000) {
          this->last_rx_block_log_ms_ = t0;
          ESP_LOGW(TAG,
                   "[WS-TIMING] rx stalled %u ms: playback buffer full, the websocket task could not read "
                   "or answer pings meanwhile",
                   (unsigned) dt);
        }
        if (n > 0 && w < n) ESP_LOGW(TAG, "playback buffer full, dropped audio");
      }
      break;
    }
    case WEBSOCKET_EVENT_ERROR:
      ESP_LOGW(TAG, "websocket error: type=%d sock_errno=%d tls_err=%s handshake_status=%d",
               (int) d->error_handle.error_type, d->error_handle.esp_transport_sock_errno,
               esp_err_to_name(d->error_handle.esp_tls_last_esp_err), d->error_handle.esp_ws_handshake_status_code);
      this->log_ws_state_("websocket error", 2);
      break;
    default:
      break;
  }
}

// Runs in the websocket task. Only arms/disarms the audio path; loop() does the rest.
void AdiVoice::on_text_frame_ws_(const std::string &msg) {
  if (msg.find("\"audio_output_start\"") == std::string::npos) return;
  auto doc = json::parse_json(msg);
  const char *type = doc["type"] | "";
  if (strcmp(type, "audio_output_start") != 0) return;
  uint32_t seq = seq_of(doc["reply_to"] | "");
  if (seq == 0 || seq != this->active_seq_.load()) {
    this->accept_audio_ = false;  // reply to an interrupted request: drop its audio
    return;
  }
  const char *enc = doc["encoding"] | "pcm_s16le";
  this->play_rb_->reset();
  this->wav_skip_ = (strcmp(enc, "wav") == 0) ? 44 : 0;  // canonical WAV header
  this->accept_audio_ = true;
}

const char *AdiVoice::out_kind_name_(OutKind k) {
  switch (k) {
    case OutKind::HELLO: return "hello";
    case OutKind::PONG: return "pong";
    case OutKind::AUDIO_START: return "audio_start";
    case OutKind::AUDIO_CHUNK: return "audio_chunk";
    case OutKind::AUDIO_END: return "audio_end";
    case OutKind::AUDIO_CANCEL: return "audio_cancel";
  }
  return "?";
}

// [WS-STATE] last_rx_ms / last_tx_ms are AGES: milliseconds since the last frame was received /
// written (-1 = never on this connection). Callable from any task.
void AdiVoice::log_ws_state_(const char *why, int level) {
  const uint32_t now = now_ms();
  const uint32_t rx = this->last_rx_ms_.load(), tx = this->last_tx_ms_.load();
  const long rx_age = rx ? static_cast<long>(now - rx) : -1, tx_age = tx ? static_cast<long>(now - tx) : -1;
  const unsigned queued = this->out_q_ ? (unsigned) uxQueueMessagesWaiting(this->out_q_) : 0;
  const unsigned bytes = (unsigned) this->out_bytes_.load();
  const int connected = this->connected_ ? 1 : 0, writing = this->write_in_progress_.load() ? 1 : 0;
  if (level >= 2) {
    ESP_LOGW(TAG, "[WS-STATE] %s: connected=%d write_in_progress=%d last_rx_ms=%ld last_tx_ms=%ld queued=%u/%uB",
             why, connected, writing, rx_age, tx_age, queued, bytes);
  } else if (level == 1) {
    ESP_LOGI(TAG, "[WS-STATE] %s: connected=%d write_in_progress=%d last_rx_ms=%ld last_tx_ms=%ld queued=%u/%uB",
             why, connected, writing, rx_age, tx_age, queued, bytes);
  } else {
    ESP_LOGD(TAG, "[WS-STATE] %s: connected=%d write_in_progress=%d last_rx_ms=%ld last_tx_ms=%ld queued=%u/%uB",
             why, connected, writing, rx_age, tx_age, queued, bytes);
  }
}

bool AdiVoice::enqueue_(OutKind kind, std::string text, std::vector<uint8_t> bin) {
  auto *m = new (std::nothrow) OutMsg();
  if (m == nullptr) return false;
  m->kind = kind;
  m->gen = this->ws_gen_.load();
  m->text = std::move(text);
  m->bin = std::move(bin);
  const size_t n = m->bin.size();
  this->out_bytes_ += n;
  if (xQueueSend(this->out_q_, &m, 0) != pdTRUE) {  // never block the main loop
    this->out_bytes_ -= n;
    delete m;
    return false;
  }
  return true;
}

// Replaces the old blocking send_text_(): only queues, returns immediately.
void AdiVoice::queue_text_(OutKind kind, const std::string &s) {
  if (this->client_ == nullptr || !this->connected_) return;
  if (!this->enqueue_(kind, s, {})) {
    ESP_LOGW(TAG, "[WS-TIMING] could not queue %s (send queue full)", out_kind_name_(kind));
  }
}

void AdiVoice::drain_out_queue_() {
  OutMsg *m = nullptr;
  uint32_t n = 0;
  while (xQueueReceive(this->out_q_, &m, 0) == pdTRUE) {
    this->out_bytes_ -= m->bin.size();
    delete m;
    n++;
  }
  if (n > 0) ESP_LOGW(TAG, "[WS-TIMING] discarded %u queued message(s) after the disconnect", (unsigned) n);
}

void AdiVoice::ws_sender_task_(void *arg) { static_cast<AdiVoice *>(arg)->ws_sender_loop_(); }

void AdiVoice::ws_sender_loop_() {
  OutMsg *raw = nullptr;
  for (;;) {
    if (xQueueReceive(this->out_q_, &raw, portMAX_DELAY) != pdTRUE || raw == nullptr) continue;
    std::unique_ptr<OutMsg> m(raw);
    const size_t payload = m->bin.size();
    this->write_out_(*m);
    this->out_bytes_ -= payload;
  }
}

// Runs in the sender task: the ONLY place the application writes to the websocket.
void AdiVoice::write_out_(OutMsg &m) {
  const char *name = out_kind_name_(m.kind);
  const bool is_audio = m.kind == OutKind::AUDIO_CHUNK;
  const size_t total = is_audio ? m.bin.size() : m.text.size();

  if (!this->connected_ || m.gen != this->ws_gen_.load()) {  // the connection changed since queuing
    this->dropped_++;
    if (!is_audio) ESP_LOGW(TAG, "[WS-TIMING] dropped %s: connection changed before it was sent", name);
    return;
  }
  const uint32_t start = now_ms();
  if (m.kind == OutKind::AUDIO_START) {
    this->up_ = UploadStats{};
    this->up_.started_ms = start;
    ESP_LOGI(TAG, "[WS-AUDIO] upload begin (%u-byte frames)", (unsigned) SEND_CHUNK);
  }

  this->write_in_progress_ = true;
  bool ok = true;
  int last_r = 0;
  uint32_t slowest = 0;
  if (is_audio) {
    for (size_t off = 0; off < total; off += SEND_CHUNK) {
      const size_t n = std::min(SEND_CHUNK, total - off);
      const uint32_t t0 = now_ms();
      last_r = esp_websocket_client_send_bin(this->client_, reinterpret_cast<const char *>(m.bin.data()) + off,
                                             n, pdMS_TO_TICKS(WS_WRITE_TIMEOUT_MS));
      const uint32_t dt = now_ms() - t0;
      slowest = std::max(slowest, dt);
      this->up_.frames++;
      this->up_.max_ms = std::max(this->up_.max_ms, dt);
      if (dt > WS_SLOW_WRITE_MS) this->up_.slow++;
      if (last_r != static_cast<int>(n)) {
        ok = false;
        break;
      }
      this->up_.bytes += n;
      this->last_tx_ms_ = now_ms();
      if (!this->connected_) {  // aborted right after this frame: don't write into a dead socket
        ok = false;
        break;
      }
    }
  } else {
    for (int attempt = 0; attempt < 2; attempt++) {  // control frames matter: one retry if still connected
      const uint32_t t0 = now_ms();
      last_r = esp_websocket_client_send_text(this->client_, m.text.c_str(), m.text.size(),
                                              pdMS_TO_TICKS(WS_WRITE_TIMEOUT_MS));
      slowest = now_ms() - t0;
      ok = last_r == static_cast<int>(m.text.size());
      if (ok || !this->connected_ || m.gen != this->ws_gen_.load()) break;
      vTaskDelay(pdMS_TO_TICKS(100));
    }
    if (ok) this->last_tx_ms_ = now_ms();
  }
  this->write_in_progress_ = false;

  const uint32_t end = now_ms();
  const bool slow = slowest > WS_SLOW_WRITE_MS;
  if (!ok) {
    ESP_LOGW(TAG, "[WS-TIMING] send type=%s bytes=%u start=%u duration_ms=%u result=FAILED(%d) %s", name,
             (unsigned) total, (unsigned) start, (unsigned) (end - start), last_r,
             this->connected_ ? "(still connected)" : "(connection aborted by the websocket library)");
    this->log_ws_state_("write failed", 2);
  } else if (!is_audio) {
    if (slow) {
      ESP_LOGW(TAG, "[WS-TIMING] send type=%s bytes=%u start=%u duration_ms=%u result=ok (slow)", name,
               (unsigned) total, (unsigned) start, (unsigned) (end - start));
      this->log_ws_state_("slow write", 2);
    } else {
      ESP_LOGI(TAG, "[WS-TIMING] send type=%s bytes=%u start=%u duration_ms=%u result=ok", name, (unsigned) total,
               (unsigned) start, (unsigned) (end - start));
    }
    if (m.kind == OutKind::PONG) {
      ESP_LOGI(TAG, "[WS-PING] pong written %u ms after the backend ping arrived", (unsigned) (end - this->ping_rx_ms_));
    }
  } else if (slow && end - this->last_slow_log_ms_ > 500) {  // audio: only slow ones, rate-limited
    this->last_slow_log_ms_ = end;
    ESP_LOGW(TAG, "[WS-TIMING] send type=audio_chunk bytes=%u start=%u duration_ms=%u result=ok (slow, slowest frame %u ms)",
             (unsigned) total, (unsigned) start, (unsigned) (end - start), (unsigned) slowest);
    this->log_ws_state_("slow write", 2);
  }

  if (m.kind == OutKind::AUDIO_END || m.kind == OutKind::AUDIO_CANCEL) {
    ESP_LOGI(TAG,
             "[WS-AUDIO] upload %s: frames=%u bytes=%u max_write_ms=%u slow_writes(>%ums)=%u took=%u ms dropped_msgs=%u",
             m.kind == OutKind::AUDIO_END ? "end" : "cancelled", (unsigned) this->up_.frames,
             (unsigned) this->up_.bytes, (unsigned) this->up_.max_ms, (unsigned) WS_SLOW_WRITE_MS,
             (unsigned) this->up_.slow, (unsigned) (end - this->up_.started_ms), (unsigned) this->dropped_.load());
  }
}

void AdiVoice::send_hello_() {
  std::string m = "{\"type\":\"hello\",\"protocol\":1,\"device_id\":\"" + json_escape(this->device_id_) +
                  "\",\"name\":\"" + json_escape(this->device_name_) + "\",\"firmware\":\"adi-esp32-0.1\"";
  if (!this->token_.empty()) m += ",\"token\":\"" + json_escape(this->token_) + "\"";
  m += ",\"capabilities\":{\"audio_input\":true,\"audio_output\":true,\"wake_word_on_device\":true,"
       "\"binary_frames\":true},\"audio_output\":{\"encoding\":\"pcm_s16le\",\"sample_rate\":16000,\"channels\":1}}";
  ESP_LOGI(TAG, "sending hello");
  this->queue_text_(OutKind::HELLO, m);
}

// ------------------------------------------------------------------ incoming messages (main thread)
void AdiVoice::handle_text_(const std::string &msg) {
  auto doc = json::parse_json(msg);
  const char *type_c = doc["type"] | "";
  std::string type = type_c;
  const char *rt = doc["reply_to"] | "";
  std::string reply_to = rt;
  ESP_LOGD(TAG, "<- %s", type.c_str());

  if (type == "welcome") {
    this->welcomed_ = true;
    ESP_LOGI(TAG, "connected to backend (session %s)", (const char *) (doc["session_id"] | "?"));
    if (this->phase_ == Phase::IDLE) this->publish_state_("idle");
  } else if (type == "ping") {
    this->ping_rx_ms_ = now_ms();
    ESP_LOGI(TAG, "[WS-PING] backend ping received at=%u", (unsigned) this->ping_rx_ms_);
    this->queue_text_(OutKind::PONG, "{\"type\":\"pong\"}");
  } else if (type == "error") {
    ESP_LOGW(TAG, "backend error %s: %s", (const char *) (doc["code"] | "?"), (const char *) (doc["message"] | ""));
    bool ours = !reply_to.empty() && reply_to == this->current_id_;
    if (this->phase_ == Phase::RECORDING && ours) {
      // e.g. "busy" / "payload_too_large": the backend rejected this recording,
      // so stop streaming audio it will only reject again.
      this->recording_ = false;
      xSemaphoreTake(this->lock_, portMAX_DELAY);
      this->rec_buf_.clear();
      xSemaphoreGive(this->lock_);
      this->current_id_.clear();
      this->active_seq_ = 0;
      this->set_phase_(Phase::IDLE);
      this->show_error_();
    } else if (this->phase_ == Phase::WAITING || this->phase_ == Phase::SPEAKING) {
      this->reset_playback_();
      this->set_phase_(Phase::IDLE);
      this->show_error_();
    }
  } else if (type == "response") {
    if (!reply_to.empty() && reply_to != this->current_id_) return;  // stale
    ESP_LOGI(TAG, "you said: \"%s\"", (const char *) (doc["transcript"] | ""));
    ESP_LOGI(TAG, "reply: \"%s\"", (const char *) (doc["text"] | ""));
    const char *st = doc["state"] | "idle";
    this->keep_listening_ = (std::string(st) == "listening");
    bool has_audio = doc["audio"] | false;
    if (!has_audio && this->phase_ == Phase::WAITING) {
      this->set_phase_(Phase::IDLE);
      if (this->keep_listening_) this->begin_recording_();
      else this->publish_state_("idle");
    }
  } else if (type == "audio_output_start") {
    if (!reply_to.empty() && reply_to != this->current_id_) return;
    if (this->phase_ != Phase::WAITING) return;
    const char *enc = doc["encoding"] | "pcm_s16le";
    uint32_t rate = doc["sample_rate"] | 16000;
    if (rate != 16000) ESP_LOGW(TAG, "reply is %u Hz but the speaker runs at 16000 Hz", (unsigned) rate);
    (void) enc;  // the ring buffer was already reset and armed in the websocket task
    this->tmp_.clear();
    this->tmp_off_ = 0;
    this->audio_in_progress_ = true;
    this->audio_end_seen_ = false;
    this->audio_done_at_ = 0;
    this->speak_started_ = millis();
    this->set_phase_(Phase::SPEAKING);
    this->publish_state_("speaking");
    this->speaker_->start();
  } else if (type == "audio_output_end") {
    if (this->phase_ == Phase::SPEAKING) this->audio_end_seen_ = true;
  }
}

// ------------------------------------------------------------------ recording
void AdiVoice::start_listening() {
  if (!this->welcomed_) {
    ESP_LOGW(TAG, "wake word heard but backend is not connected yet");
    return;
  }
  if (this->phase_ == Phase::RECORDING) return;
  if (this->phase_ != Phase::IDLE) this->stop();  // barge-in
  this->begin_recording_();
}

void AdiVoice::begin_recording_() {
  auto info = this->mic_->get_audio_stream_info();
  this->mic_rate_ = info.get_sample_rate();
  this->mic_channels_ = info.get_channels();
  if (info.get_bits_per_sample() != 16) ESP_LOGW(TAG, "microphone is not 16-bit; audio will be garbled");
  if (!this->mic_->is_running()) ESP_LOGW(TAG, "microphone is not running");

  xSemaphoreTake(this->lock_, portMAX_DELAY);
  this->rec_buf_.clear();
  this->audio_abs_sum_ = 0;
  this->audio_sq_sum_ = 0;
  this->audio_sample_count_ = 0;
  this->audio_peak_ = 0;
  xSemaphoreGive(this->lock_);

  this->current_id_ = "a" + std::to_string(++this->seq_);
  this->active_seq_ = this->seq_;
  this->accept_audio_ = false;
  char buf[256];
  snprintf(buf, sizeof(buf),
           "{\"type\":\"audio_start\",\"id\":\"%s\",\"format\":{\"encoding\":\"pcm_s16le\",\"sample_rate\":%u,"
           "\"channels\":%u},\"want_audio\":true}",
           this->current_id_.c_str(), (unsigned) this->mic_rate_, (unsigned) this->mic_channels_);
  this->queue_text_(OutKind::AUDIO_START, buf);

  this->rec_start_ = millis();
  this->last_speech_ = this->rec_start_;
  this->speech_seen_ = false;
  this->recording_ = true;
  this->set_phase_(Phase::RECORDING);
  this->publish_state_("listening");
}

void AdiVoice::finish_recording_(bool cancel) {
  this->recording_ = false;
  // flush what is left
  std::vector<uint8_t> rest;
  xSemaphoreTake(this->lock_, portMAX_DELAY);
  rest.swap(this->rec_buf_);
  xSemaphoreGive(this->lock_);
  if (!cancel) {
    // The microphone signal is very quiet (previously observed peak ~= 538).
    // Amplify the PCM before sending it to the speech-to-text backend.
    amplify_pcm_s16le_(rest, MIC_INPUT_GAIN);

    // Whatever was not streamed yet goes out as ONE queued message (the sender task splits it
    // into frames), so this call returns immediately instead of writing up to 10 s of audio
    // on the main loop.
    const size_t tail = rest.size();
    if (tail > 0 && !this->connected_) {
      ESP_LOGW(TAG, "[WS-TIMING] recording tail (%u bytes) not sent: not connected", (unsigned) tail);
    } else if (tail > 0 && !this->enqueue_(OutKind::AUDIO_CHUNK, {}, std::move(rest))) {
      ESP_LOGW(TAG, "[WS-TIMING] could not queue the recording tail (%u bytes): send queue full", (unsigned) tail);
    }
    ESP_LOGI(TAG, "recorded %u bytes; mic=%u Hz/%u ch/16-bit; abs_sum=%llu samples=%u peak=%d",
             (unsigned) tail, (unsigned)this->mic_rate_, (unsigned)this->mic_channels_,
             (unsigned long long)this->audio_abs_sum_, (unsigned)this->audio_sample_count_,
             (int)this->audio_peak_);
    this->queue_text_(OutKind::AUDIO_END, "{\"type\":\"audio_end\"}");
    this->wait_started_ = millis();
    this->set_phase_(Phase::WAITING);
    this->publish_state_("processing");
  } else {
    this->queue_text_(OutKind::AUDIO_CANCEL, "{\"type\":\"audio_cancel\"}");
    this->current_id_.clear();
    this->set_phase_(Phase::IDLE);
    this->publish_state_("idle");
  }
}

void AdiVoice::reset_playback_() {
  this->accept_audio_ = false;  // stop the websocket task writing first
  this->play_rb_->reset();
  this->tmp_.clear();
  this->tmp_off_ = 0;
  this->audio_in_progress_ = false;
  this->audio_end_seen_ = false;
  if (this->speaker_->is_running()) this->speaker_->stop();
}

void AdiVoice::stop() {
  if (this->recording_) {
    this->finish_recording_(true);
  }
  this->reset_playback_();
  this->current_id_.clear();
  this->active_seq_ = 0;
  this->keep_listening_ = false;
  this->set_phase_(Phase::IDLE);
  this->publish_state_("idle");
}

// ------------------------------------------------------------------ playback
void AdiVoice::feed_speaker_() {
  if (this->tmp_.empty() || this->tmp_off_ >= this->tmp_.size()) {
    this->tmp_.clear();
    this->tmp_off_ = 0;
    size_t n = std::min(PLAY_CHUNK, this->play_rb_->available());
    n &= ~static_cast<size_t>(1);  // whole 16-bit samples only
    this->tmp_.resize(n);
    if (n > 0) this->tmp_.resize(this->play_rb_->read(this->tmp_.data(), n, 0));
  }
  if (!this->tmp_.empty() && this->tmp_off_ < this->tmp_.size()) {
    size_t w = this->speaker_->play(this->tmp_.data() + this->tmp_off_, this->tmp_.size() - this->tmp_off_, 0);
    this->tmp_off_ += w;
  }
}

// ------------------------------------------------------------------ main loop
void AdiVoice::loop() {
  // Start the websocket once Wi-Fi is up.
  if (!this->ws_started_ && network::is_connected()) {
    esp_websocket_client_config_t cfg = {};
    cfg.uri = this->uri_.c_str();
    cfg.buffer_size = 4096;
    cfg.task_stack = 8192;  // audio_output_start is JSON-parsed in this task now
    cfg.reconnect_timeout_ms = 3000;
    cfg.network_timeout_ms = 5000;
    cfg.ping_interval_sec = 10;
    this->client_ = esp_websocket_client_init(&cfg);
    if (this->client_ == nullptr) {
      ESP_LOGE(TAG, "websocket init failed");
      this->mark_failed();
      return;
    }
    esp_websocket_register_events(this->client_, WEBSOCKET_EVENT_ANY, &AdiVoice::ws_event_, this);
    esp_websocket_client_start(this->client_);
    this->ws_started_ = true;
    ESP_LOGI(TAG, "connecting to %s", this->uri_.c_str());
  }
  if (!this->ws_started_) return;

  // connection events
  int ev = this->pending_conn_event_;
  if (ev >= 0) {
    this->pending_conn_event_ = -1;
    for (auto &cb : this->conn_cbs_) cb(ev == 1);
    if (ev == 0) {
      ESP_LOGW(TAG, "disconnected from backend, retrying");
      this->drain_out_queue_();
      this->recording_ = false;
      this->reset_playback_();
      this->set_phase_(Phase::IDLE);
      this->publish_state_("connecting");
    }
  }
  if (this->pending_hello_) {
    this->pending_hello_ = false;
    this->send_hello_();
  }

  // incoming text
  for (int i = 0; i < 8; i++) {
    std::string msg;
    xSemaphoreTake(this->lock_, portMAX_DELAY);
    if (!this->text_q_.empty()) {
      msg = std::move(this->text_q_.front());
      this->text_q_.pop_front();
    }
    xSemaphoreGive(this->lock_);
    if (msg.empty()) break;
    this->handle_text_(msg);
  }

  uint32_t now = millis();
  if (now - this->ws_state_log_ms_ > WS_STATE_PERIOD_MS) {
    this->ws_state_log_ms_ = now;
    this->log_ws_state_("periodic", 0);
  }
  switch (this->phase_) {
    case Phase::RECORDING: {
      // ship buffered microphone audio
      std::vector<uint8_t> chunk;
      // Only hand over a chunk while the uplink has room. If the network is slow the audio
      // simply waits in rec_buf_ (PSRAM, capped) instead of piling up in internal RAM.
      const bool uplink_ready =
          this->out_bytes_.load() < MAX_QUEUED_AUDIO_BYTES && uxQueueSpacesAvailable(this->out_q_) > 2;
      xSemaphoreTake(this->lock_, portMAX_DELAY);
      if (uplink_ready && this->rec_buf_.size() >= SEND_CHUNK) {
        chunk.assign(this->rec_buf_.begin(), this->rec_buf_.begin() + SEND_CHUNK);
        this->rec_buf_.erase(this->rec_buf_.begin(), this->rec_buf_.begin() + SEND_CHUNK);
      }
      xSemaphoreGive(this->lock_);
      if (!chunk.empty()) {
        // Amplify the live PCM chunk before it reaches the STT backend.
        amplify_pcm_s16le_(chunk, MIC_INPUT_GAIN);
        if (!this->enqueue_(OutKind::AUDIO_CHUNK, {}, std::move(chunk))) {
          ESP_LOGW(TAG, "[WS-TIMING] could not queue an audio chunk: send queue full");
        }
      }

      if (this->mww_->get_vad_state()) {
        this->speech_seen_ = true;
        this->last_speech_ = now;
      }
      if (now - this->rec_start_ > this->max_record_ms_) {
        ESP_LOGI(TAG, "max recording length reached");
        this->finish_recording_(false);
      } else if (this->speech_seen_ && now - this->last_speech_ > this->silence_ms_) {
        ESP_LOGI(TAG, "end of speech");
        this->finish_recording_(false);
      } else if (!this->speech_seen_ && now - this->rec_start_ > this->no_speech_ms_) {
        ESP_LOGI(TAG, "no speech heard, cancelling");
        this->finish_recording_(true);
      }
      break;
    }
    case Phase::WAITING:
      if (now - this->wait_started_ > WAIT_TIMEOUT_MS) {
        ESP_LOGW(TAG, "backend did not answer in time");
        this->stop();
        this->publish_state_("error");
        this->publish_state_("idle");
      }
      break;
    case Phase::SPEAKING: {
      this->feed_speaker_();
      bool drained = this->audio_end_seen_ && this->tmp_.empty() && !this->speaker_->has_buffered_data();
      drained = drained && this->play_rb_->available() == 0;
      if (drained) {
        if (this->audio_done_at_ == 0) this->audio_done_at_ = now;
        if (now - this->audio_done_at_ > 300) {  // let the DMA tail play out
          this->speaker_->finish();
          this->audio_in_progress_ = false;
          this->accept_audio_ = false;
          this->set_phase_(Phase::IDLE);
          if (this->keep_listening_) {
            this->keep_listening_ = false;
            this->begin_recording_();  // "Adi" -> "Yes?" -> listen for the command
          } else {
            this->publish_state_("idle");
          }
        }
      } else {
        this->audio_done_at_ = 0;
        if (now - this->speak_started_ > 60000) this->stop();
      }
      break;
    }
    default:
      break;
  }
}

}  // namespace esphome::adi_voice
