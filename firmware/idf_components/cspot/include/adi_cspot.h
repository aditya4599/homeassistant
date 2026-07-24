#pragma once
// Minimal C API around cspot (Spotify Connect) for the ADI ESPHome component.
// All callbacks run on cspot's own tasks (stacks in PSRAM): they must be quick,
// must not block for long and must NOT write to flash/NVS.
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ADI_CSPOT_VOLUME_MAX 65535  // Spotify Connect volume range is 0..65535

typedef enum {
  ADI_CSPOT_EV_LINKED,      // a Spotify account is connected to this speaker
  ADI_CSPOT_EV_START,       // playback (re)starts; audio for a new position follows
  ADI_CSPOT_EV_PLAY,        // resumed
  ADI_CSPOT_EV_PAUSE,       // paused
  ADI_CSPOT_EV_FLUSH,       // drop buffered audio (seek / next / previous)
  ADI_CSPOT_EV_STOP,        // playlist finished
  ADI_CSPOT_EV_DISC,        // another device took over / disconnected
  ADI_CSPOT_EV_VOLUME,      // value = 0..65535
} adi_cspot_event_t;

typedef struct {
  // 44.1 kHz, 16-bit, stereo, interleaved PCM. Return bytes accepted (0 = full, cspot retries).
  size_t (*on_pcm)(uint8_t *pcm, size_t bytes, void *ctx);
  void (*on_event)(adi_cspot_event_t event, int value, void *ctx);
  // Milliseconds of audio still buffered locally (used to detect the end of the last track).
  uint32_t (*buffered_ms)(void *ctx);
  // Reusable login JSON after a successful login. Copy it; persist it from another task.
  void (*on_credentials)(const char *json, void *ctx);
  void *ctx;
} adi_cspot_callbacks_t;

typedef struct {
  const char *name;           // shown in the Spotify app, e.g. "ADI Speaker"
  int port;                   // ZeroConf HTTP port (advertised over mDNS)
  int bitrate;                // 96, 160 or 320
  int initial_volume;         // 0..65535
  const char *client_id;      // Spotify developer app credentials (from secrets.yaml)
  const char *client_secret;
  const char *credentials;    // stored reusable login JSON, or NULL / "" for ZeroConf only
} adi_cspot_config_t;

// Starts the HTTP ZeroConf endpoint, mDNS advertisement and the player task. Call once,
// after the network is up. Returns false if the HTTP server could not start.
bool adi_cspot_start(const adi_cspot_config_t *config, const adi_cspot_callbacks_t *callbacks);
// Local volume change (0..65535): applied locally and reported to Spotify.
void adi_cspot_set_volume(int volume);
// Local pause/resume (reported to Spotify).
void adi_cspot_set_pause(bool pause);

#ifdef __cplusplus
}
#endif
