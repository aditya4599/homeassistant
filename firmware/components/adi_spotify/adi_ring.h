#pragma once
// Pure, dependency-free helpers for the status ring (no ESPHome / FreeRTOS includes, so the
// maths can be unit-tested on a PC).
//
//  * AdiLevelMeter - cheap loudness estimate of the Spotify PCM (0..1, smoothed).
//  * AdiRing       - turns (mode, music level, "Spotify playing") into 7 RGB pixels.
//
// Nothing here allocates or blocks; one render() is a few dozen float operations.

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>

namespace esphome {
namespace adi_spotify {

constexpr int RING_LEDS = 7;

// Chosen by the YAML "ring" script from the voice state.
enum RingMode : int {
  RING_NORMAL = 0,     // idle / connecting / anything else: rainbow (music-reactive if Spotify plays)
  RING_LISTENING = 1,  // after the wake word: steady light blue (highest priority)
  RING_BUSY = 2,       // processing / speaking: plain rainbow
  RING_ERROR = 3,      // brief red
};

inline float clamp01(float v) { return v < 0.0f ? 0.0f : (v > 1.0f ? 1.0f : v); }
inline float lerp(float a, float b, float t) { return a + (b - a) * t; }
// One-pole smoothing factor for a time constant `tau` over `dt` seconds.
inline float smooth_alpha(float dt, float tau) { return dt <= 0.0f ? 0.0f : 1.0f - std::exp(-dt / tau); }

class AdiLevelMeter {
 public:
  // `stereo`: interleaved 16-bit L/R frames. `applied_gain`: the amplitude factor already
  // applied to this data (volume), so the lights follow the music rather than the volume knob.
  // `dt_s`: duration of this block in seconds. Returns the smoothed level 0..1.
  float process(const int16_t *stereo, size_t frames, float applied_gain, float dt_s) {
    if (stereo == nullptr || frames == 0) return level_;
    // Every 8th frame, left channel only: ~128 samples per 4 KB chunk.
    uint64_t acc = 0;
    uint32_t count = 0;
    for (size_t i = 0; i < frames; i += 8) {
      const int32_t v = stereo[2 * i];
      acc += static_cast<uint64_t>(static_cast<int64_t>(v) * v);
      count++;
    }
    float rms = std::sqrt(static_cast<float>(acc) / static_cast<float>(count)) / 32768.0f;
    rms /= std::max(applied_gain, 0.03f);  // undo the volume gain (floored: no noise boost at ~0 volume)
    const float db = 20.0f * std::log10(std::max(rms, 1e-5f));
    const float target = clamp01((db + 48.0f) / 36.0f);  // -48 dBFS -> 0, -12 dBFS -> 1
    const float tau = target > level_ ? ATTACK_S : RELEASE_S;
    level_ += (target - level_) * smooth_alpha(dt_s, tau);
    return level_;
  }

  // No audio is arriving: fall back toward 0.
  float decay(float dt_s) {
    level_ += (0.0f - level_) * smooth_alpha(dt_s, RELEASE_S);
    return level_;
  }

  float level() const { return level_; }

  static constexpr float ATTACK_S = 0.06f;
  static constexpr float RELEASE_S = 0.45f;

 private:
  float level_ = 0.0f;
};

class AdiRing {
 public:
  // ---- tuning knobs ------------------------------------------------------------------
  static constexpr float MAX_VALUE = 0.60f;          // overall brightness ceiling (WS2812 are very bright)
  static constexpr float NORMAL_BRIGHTNESS = 0.85f;  // relative, plain rainbow
  static constexpr float NORMAL_SPEED = 0.30f;       // revolutions per second, plain rainbow
  static constexpr float MUSIC_MIN_BRIGHTNESS = 0.30f;
  static constexpr float MUSIC_MAX_BRIGHTNESS = 1.00f;
  static constexpr float MUSIC_MIN_SPEED = 0.10f;
  static constexpr float MUSIC_MAX_SPEED = 0.85f;
  static constexpr float LISTEN_VALUE = 0.80f;       // light blue 🩵, steady
  static constexpr float LISTEN_R = 0.45f, LISTEN_G = 0.82f, LISTEN_B = 1.00f;

  // Fill `out` with the pixels for this frame. Call at a steady interval (e.g. every 40 ms).
  void render(uint32_t now_ms, int mode, float music_level, bool spotify_playing, uint8_t out[RING_LEDS][3]) {
    float dt = 0.0f;
    if (started_) {
      dt = static_cast<float>(static_cast<uint32_t>(now_ms - last_ms_)) * 0.001f;
      dt = std::min(dt, 0.25f);  // after a stall, don't jump
    }
    started_ = true;
    last_ms_ = now_ms;

    // Music reaction only applies to the normal ring; voice states always win.
    const bool music = spotify_playing && mode == RING_NORMAL;
    level_ += ((music ? clamp01(music_level) : 0.0f) - level_) * smooth_alpha(dt, 0.35f);

    if (mode == RING_LISTENING) {  // immediate, steady, no animation
      fill(out, LISTEN_R * LISTEN_VALUE, LISTEN_G * LISTEN_VALUE, LISTEN_B * LISTEN_VALUE);
      return;
    }
    if (mode == RING_ERROR) {
      fill(out, 1.0f * MAX_VALUE, 0.0f, 0.0f);
      return;
    }

    const float target_speed = music ? lerp(MUSIC_MIN_SPEED, MUSIC_MAX_SPEED, level_) : NORMAL_SPEED;
    const float target_bright = music ? lerp(MUSIC_MIN_BRIGHTNESS, MUSIC_MAX_BRIGHTNESS, level_) : NORMAL_BRIGHTNESS;
    speed_ += (target_speed - speed_) * smooth_alpha(dt, 0.40f);
    bright_ += (target_bright - bright_) * smooth_alpha(dt, 0.30f);

    phase_ += speed_ * dt;
    phase_ -= std::floor(phase_);

    const float value = MAX_VALUE * bright_ * bright_;  // squared: perceptually even steps
    for (int i = 0; i < RING_LEDS; i++) {
      // Colours advance to the next LED each step: LED i shows the hue LED i-1 showed before.
      float hue = static_cast<float>(i) / RING_LEDS - phase_;
      hue -= std::floor(hue);
      hsv_to_rgb(hue, 1.0f, value, out[i]);
    }
  }

  float phase() const { return phase_; }
  float speed() const { return speed_; }
  float brightness() const { return bright_; }
  float music_level() const { return level_; }

 private:
  static uint8_t to_byte(float v) { return static_cast<uint8_t>(clamp01(v) * 255.0f + 0.5f); }

  static void fill(uint8_t out[RING_LEDS][3], float r, float g, float b) {
    for (int i = 0; i < RING_LEDS; i++) {
      out[i][0] = to_byte(r);
      out[i][1] = to_byte(g);
      out[i][2] = to_byte(b);
    }
  }

  static void hsv_to_rgb(float h, float s, float v, uint8_t px[3]) {
    const float x = h * 6.0f;
    const int sector = static_cast<int>(x) % 6;
    const float f = x - std::floor(x);
    const float p = v * (1.0f - s), q = v * (1.0f - s * f), t = v * (1.0f - s * (1.0f - f));
    float r, g, b;
    switch (sector) {
      case 0: r = v; g = t; b = p; break;
      case 1: r = q; g = v; b = p; break;
      case 2: r = p; g = v; b = t; break;
      case 3: r = p; g = q; b = v; break;
      case 4: r = t; g = p; b = v; break;
      default: r = v; g = p; b = q; break;
    }
    px[0] = to_byte(r);
    px[1] = to_byte(g);
    px[2] = to_byte(b);
  }

  bool started_ = false;
  uint32_t last_ms_ = 0;
  float phase_ = 0.0f;
  float level_ = 0.0f;
  float speed_ = NORMAL_SPEED;
  float bright_ = NORMAL_BRIGHTNESS;
};

}  // namespace adi_spotify
}  // namespace esphome
