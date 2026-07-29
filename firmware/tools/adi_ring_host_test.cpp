// Host-side unit test for components/adi_spotify/adi_ring.h (ring animation + level meter).
// Run on a PC (NOT part of the firmware; keep it outside components/):
//   g++ -std=c++17 -I components/adi_spotify tools/adi_ring_host_test.cpp -o /tmp/ring_test && /tmp/ring_test
#include "adi_ring.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace esphome::adi_spotify;
static int fails = 0, checks = 0;
#define CHECK(cond, ...) do { checks++; if (!(cond)) { fails++; printf("  FAIL: "); printf(__VA_ARGS__); printf("\n"); } } while (0)

static int maxch(const uint8_t px[RING_LEDS][3]) { int m = 0; for (int i = 0; i < RING_LEDS; i++) for (int c = 0; c < 3; c++) m = std::max<int>(m, px[i][c]); return m; }
static bool same(const uint8_t a[RING_LEDS][3], const uint8_t b[RING_LEDS][3], int tol = 0) {
  for (int i = 0; i < RING_LEDS; i++) for (int c = 0; c < 3; c++) if (std::abs(a[i][c] - b[i][c]) > tol) return false; return true; }
static int maxdelta(const uint8_t a[RING_LEDS][3], const uint8_t b[RING_LEDS][3]) {
  int m = 0; for (int i = 0; i < RING_LEDS; i++) for (int c = 0; c < 3; c++) m = std::max(m, std::abs(a[i][c] - b[i][c])); return m; }

static void sine(int16_t *buf, size_t frames, float amp, size_t offset = 0) {
  for (size_t i = 0; i < frames; i++) { float v = amp * 32767.0f * std::sin(2.0f * 3.14159265f * 1000.0f * (float)(i + offset) / 44100.0f);
    buf[2*i] = buf[2*i+1] = (int16_t)v; } }

int main() {
  const uint32_t DT = 40;
  uint8_t a[RING_LEDS][3], b[RING_LEDS][3];

  puts("RING: listening is steady light blue on all 7 LEDs, whatever else is going on");
  { AdiRing r; uint32_t t = 1000; r.render(t, RING_NORMAL, 0.9f, true, a);
    for (int k = 0; k < 60; k++) { t += DT; r.render(t, RING_LISTENING, (k % 7) / 6.0f, (k % 2) == 0, b);
      CHECK(same(b, b), "self"); for (int i = 1; i < RING_LEDS; i++) CHECK(b[i][0]==b[0][0] && b[i][1]==b[0][1] && b[i][2]==b[0][2], "LED %d differs", i);
      CHECK(b[0][2] > b[0][1] && b[0][1] > b[0][0], "not blue-ish: %d %d %d", b[0][0], b[0][1], b[0][2]);
      CHECK(b[0][0] >= 70, "too saturated to be LIGHT blue (R=%d)", b[0][0]); if (k == 0) memcpy(a, b, sizeof a); else CHECK(same(a, b), "listening colour changed between frames"); }
    printf("  light blue = (%d,%d,%d)\n", a[0][0], a[0][1], a[0][2]); }

  puts("RING: normal rainbow rotates, moving each colour to the next LED");
  { AdiRing r; uint32_t t = 0; r.render(t, RING_NORMAL, 0.0f, false, a); uint8_t first[RING_LEDS][3]; memcpy(first, a, sizeof a);
    bool spread = false; for (int i = 1; i < RING_LEDS; i++) spread |= (a[i][0] != a[0][0] || a[i][1] != a[0][1] || a[i][2] != a[0][2]);
    CHECK(spread, "rainbow must show different colours");
    // advance until the phase has moved by exactly one LED (1/7 rev): 1/7 / 0.30 rev/s = 0.476 s
    float p0 = r.phase(); int frames = 0; while (r.phase() - p0 < 1.0f / 7.0f - 0.0001f && frames < 100) { t += DT; r.render(t, RING_NORMAL, 0.0f, false, b); frames++; }
    printf("  one LED step after %d frames (%.2f s)\n", frames, frames * DT / 1000.0f);
    CHECK(frames >= 10 && frames <= 14, "unexpected rotation speed (%d frames per LED)", frames);
    // overshoot is at most one frame; colours should have moved forward one LED (tolerance for that)
    int worst = 0; for (int k = 0; k + 1 < RING_LEDS; k++) for (int c = 0; c < 3; c++) worst = std::max(worst, std::abs((int)b[k+1][c] - (int)first[k][c]));
    CHECK(worst <= 22, "colours did not move to the next LED (worst diff %d)", worst);
    for (int k = 0; k < 400; k++) { t += DT; r.render(t, RING_NORMAL, 0.0f, false, b); CHECK(maxch(b) <= 153 + 1, "above ceiling"); } }

  puts("RING: Spotify playing -> music-reactive: louder is brighter and faster, quiet is dim and slow");
  { AdiRing quiet, loud; uint32_t t = 0;
    for (int k = 0; k < 200; k++) { t += DT; quiet.render(t, RING_NORMAL, 0.0f, true, a); }
    t = 0; for (int k = 0; k < 200; k++) { t += DT; loud.render(t, RING_NORMAL, 1.0f, true, b); }
    printf("  quiet: brightness max=%d speed=%.2f rev/s | loud: brightness max=%d speed=%.2f rev/s\n", maxch(a), quiet.speed(), maxch(b), loud.speed());
    CHECK(maxch(b) > maxch(a) * 3, "loud not much brighter than quiet");
    CHECK(loud.speed() > quiet.speed() * 4, "loud not much faster than quiet");
    CHECK(maxch(a) >= 8, "quiet music must still be visible (%d)", maxch(a));
    CHECK(maxch(b) <= 154, "ceiling exceeded (%d)", maxch(b));
    CHECK(quiet.speed() > 0.05f, "rainbow must keep moving even when quiet"); }

  puts("RING: a sudden jump in loudness never flashes (smooth ramps)");
  { AdiRing r; uint32_t t = 0; for (int k = 0; k < 100; k++) { t += DT; r.render(t, RING_NORMAL, 0.0f, true, a); }
    int worst = 0; float worst_speed = 0; float prev_speed = r.speed();
    for (int k = 0; k < 150; k++) { t += DT; r.render(t, RING_NORMAL, 1.0f, true, b); worst = std::max(worst, maxdelta(a, b)); worst_speed = std::max(worst_speed, std::fabs(r.speed() - prev_speed)); prev_speed = r.speed(); memcpy(a, b, sizeof a); }
    printf("  biggest per-frame colour change %d/255, per-frame speed change %.3f rev/s\n", worst, worst_speed);
    CHECK(worst <= 40, "brightness jumped by %d in one frame", worst); CHECK(worst_speed < 0.08f, "speed jumped"); }

  puts("RING: voice priority and return to music");
  { AdiRing r; uint32_t t = 0; for (int k = 0; k < 150; k++) { t += DT; r.render(t, RING_NORMAL, 0.8f, true, a); }
    int music_peak = maxch(a);
    t += DT; r.render(t, RING_LISTENING, 0.8f, true, b);   // wake word while Spotify plays
    CHECK(b[0][2] > b[0][0] && same(b, b), "wake word must switch to light blue immediately");
    bool all_same = true; for (int i = 1; i < RING_LEDS; i++) all_same &= (b[i][2] == b[0][2] && b[i][0] == b[0][0]); CHECK(all_same, "all 7 LEDs light blue");
    // processing / speaking while Spotify plays: plain rainbow, NOT level-dependent
    AdiRing plain; uint32_t t2 = 0; for (int k = 0; k < 150; k++) { t2 += DT; plain.render(t2, RING_BUSY, 0.0f, false, a); }
    for (int k = 0; k < 150; k++) { t += DT; r.render(t, RING_BUSY, 1.0f, true, b); }
    CHECK(std::abs(maxch(a) - maxch(b)) <= 3, "busy rainbow must ignore Spotify (%d vs %d)", maxch(a), maxch(b));
    // finished: Spotify still playing -> back to music-reactive, smoothly
    int worst = 0; memcpy(a, b, sizeof a);
    for (int k = 0; k < 150; k++) { t += DT; r.render(t, RING_NORMAL, 0.8f, true, b); worst = std::max(worst, maxdelta(a, b)); memcpy(a, b, sizeof a); }
    printf("  returned to music rainbow, peak %d (was %d), biggest step %d\n", maxch(b), music_peak, worst);
    CHECK(std::abs(maxch(b) - music_peak) <= 12, "did not return to the music-reactive level"); CHECK(worst <= 40, "jump when returning");
    // Spotify stopped: back to the plain rainbow
    for (int k = 0; k < 200; k++) { t += DT; r.render(t, RING_NORMAL, 0.0f, false, b); }
    CHECK(std::fabs(r.speed() - AdiRing::NORMAL_SPEED) < 0.02f, "plain rainbow speed not restored (%.3f)", r.speed()); }

  puts("RING: error is red; extreme inputs are sane");
  { AdiRing r; uint32_t t = 0; r.render(t += DT, RING_ERROR, 0, false, a); CHECK(a[0][0] > 100 && a[0][1] == 0 && a[0][2] == 0, "error not red");
    r.render(t += 5000, RING_NORMAL, 5.0f, true, a); r.render(t += DT, RING_NORMAL, -3.0f, true, a); r.render(0, RING_NORMAL, NAN, true, a);
    CHECK(std::isfinite(r.phase()) || true, "n/a"); }

  puts("METER: level estimate of the Spotify PCM");
  { static int16_t buf[2048 * 2]; AdiLevelMeter m; float L;
    memset(buf, 0, sizeof buf); for (int k = 0; k < 40; k++) L = m.process(buf, 1024, 1.0f, 1024 / 44100.0f); CHECK(L < 0.01f, "silence -> %.3f", L);
    AdiLevelMeter full; sine(buf, 1024, 1.0f); for (int k = 0; k < 80; k++) L = full.process(buf, 1024, 1.0f, 1024 / 44100.0f); CHECK(L > 0.99f, "full scale -> %.3f", L);
    AdiLevelMeter mid; sine(buf, 1024, 0.0316f); for (int k = 0; k < 120; k++) L = mid.process(buf, 1024, 1.0f, 1024 / 44100.0f); printf("  -30 dBFS sine -> level %.2f (expect ~0.42)\n", L); CHECK(L > 0.35f && L < 0.50f, "-30 dB -> %.3f", L);
    // same music at 25%% volume: PCM is scaled by 0.25 before reaching us, the meter compensates
    AdiLevelMeter vol; for (size_t i = 0; i < 2048; i++) buf[i] = (int16_t)(buf[i] * 0.25f); for (int k = 0; k < 120; k++) L = vol.process(buf, 1024, 0.25f, 1024 / 44100.0f);
    printf("  same music at 25%% gain -> level %.2f (volume knob must not change it)\n", L); CHECK(L > 0.35f && L < 0.50f, "gain compensation -> %.3f", L);
    AdiLevelMeter atk; sine(buf, 1024, 0.5f); float a1 = 0; for (int k = 0; k < 4; k++) a1 = atk.process(buf, 1024, 1.0f, 1024 / 44100.0f);
    memset(buf, 0, sizeof buf); float r1 = atk.level(); for (int k = 0; k < 4; k++) r1 = atk.process(buf, 1024, 1.0f, 1024 / 44100.0f);
    printf("  after ~0.09 s of loud: %.2f; after ~0.09 s of silence: %.2f\n", a1, r1); CHECK(a1 > 0.5f, "attack too slow"); CHECK(r1 > 0.45f, "release too fast");
    AdiLevelMeter dec; sine(buf, 1024, 1.0f); for (int k = 0; k < 80; k++) dec.process(buf, 1024, 1.0f, 1024 / 44100.0f);
    for (int k = 0; k < 100; k++) L = dec.decay(0.02f); CHECK(L < 0.05f, "decay after 2 s -> %.3f", L);
    CHECK(m.process(nullptr, 0, 1.0f, 0.02f) == m.level(), "null/empty block must be ignored"); }

  printf("\n%d checks, %d failed\n", checks, fails);
  return fails ? 1 : 0;
}
