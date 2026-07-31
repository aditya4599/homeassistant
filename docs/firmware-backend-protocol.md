# Firmware ↔ backend integration

This describes what the code in this repository actually does. The backend's protocol
definitions live in `backend/app/services/device/protocol.py` and
`backend/app/services/device/session.py`; the firmware side is
`firmware/components/adi_voice/adi_voice.cpp`.

## Transport

- One WebSocket per device: `ws://<backend_host>:<backend_port>/api/v1/device/ws`.
  The firmware builds this URL in `components/adi_voice/__init__.py` from `host`
  (`!secret backend_host`), `port` (`${backend_port}`, default `8000`) and `path`
  (default `/api/v1/device/ws`).
- Plain `ws://`. There is no TLS, so the device token travels in clear text on the LAN.
- Text frames are JSON with a `type` field. Binary frames are raw audio.
- Protocol version `1` (sent in `hello.protocol`, checked by the backend).

## Messages the firmware sends

| Message | When | Notes |
|---|---|---|
| `hello` | Right after the WebSocket connects | `device_id`, `name`, `firmware: "adi-esp32-0.1"`, `token` (if set), `capabilities{audio_input, audio_output, wake_word_on_device, binary_frames}` (all `true`), `audio_output{encoding: pcm_s16le, sample_rate: 16000, channels: 1}` |
| `audio_start` | Wake word detected | `id: "a<N>"`, `format{encoding: pcm_s16le, sample_rate, channels}` from the mic stream, `want_audio: true` |
| binary frames | While recording | 1600-byte frames (50 ms of 16 kHz mono 16-bit) |
| `audio_end` | Silence for `silence_ms`, or `max_record_seconds` reached | Backend processes the recording |
| `audio_cancel` | Recording aborted (e.g. no speech within `no_speech_timeout`) | Backend discards it |
| `pong` | In reply to the backend's JSON `ping` | |

The backend also accepts `command`, `audio_chunk` (base64), `status`, `ping` and `bye`
from devices. The ADI firmware does not send them.

## Messages the backend sends

| Message | Firmware behavior |
|---|---|
| `welcome` | Marks the connection ready; wake-word turns are only started after it. |
| `ping` (every `DEVICE_HEARTBEAT_SECONDS`) | Answers with `pong`. The backend closes with 4008 after `DEVICE_TIMEOUT_SECONDS` without traffic. |
| `status` | Ignored. The LED ring is driven by the firmware's own state machine. |
| `response` | Logs transcript and reply. `state: "listening"` makes the device record again (wake-word-only turns). `audio: false` returns it to idle. |
| `audio_output_start` → binary chunks → `audio_output_end` | Plays the reply. Only accepted when `reply_to` matches the current recording. A `wav` encoding has its 44-byte header skipped. |
| `error` | Stops the current turn and shows red on the ring briefly. |

## Audio formats

- Microphone → backend: 16-bit little-endian PCM, 16 kHz, mono (ES7210 right channel).
- Backend → speaker: the firmware asks for `pcm_s16le` at 16 kHz in `hello`. That
  request takes precedence over `DEVICE_AUDIO_OUTPUT_FORMAT` and
  `DEVICE_AUDIO_OUTPUT_SAMPLE_RATE`, which only apply to devices that don't declare a format.
  The backend resamples TTS output to 16 kHz.
- The backend normalizes quiet microphone audio before STT when
  `DEVICE_AUDIO_NORMALIZE=true` (default). The firmware sends raw, unamplified audio and
  relies on this.

## Authentication

The backend checks the `hello.token` against, in order: `DEVICE_TOKENS`
(`device_id:token,...`), `DEVICE_SHARED_TOKEN`, then `API_KEY`. If none is set, device auth
is disabled in development and refused when `APP_ENV=production`. A bad token closes the
socket with code 4001.

## Values that must match on both sides

| Firmware (`adi-speaker.yaml` / `secrets.yaml`) | Backend (`.env`) |
|---|---|
| `substitutions.device_id` (`esp32-desk`) | the id in `DEVICE_TOKENS=esp32-desk:<token>` |
| `device_token` | the token in `DEVICE_TOKENS` (or `DEVICE_SHARED_TOKEN`) |
| `backend_host` | LAN IP of the machine running the backend; it must listen on `0.0.0.0` (`APP_HOST`) |
| `substitutions.backend_port` (`8000`) | `APP_PORT` |
| Wake word models: `alexa`, `okay_nabu` | `WAKE_WORD=Alexa` (+ `WAKE_WORD_ALIASES`) so `WAKE_WORD_MODE=strip` removes it from the transcript |
| `substitutions.friendly_name` (`ADI Speaker`) — the Spotify Connect name | `SPOTIFY_DEFAULT_DEVICE=ADI Speaker` |
| `spotify_client_id` / `spotify_client_secret` | `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` (same developer app) |

Nothing synchronizes these automatically; they are set by hand in each file.

## Spotify: two independent paths

The firmware and backend never exchange Spotify messages with each other.

1. **Playback (firmware).** `adi_spotify` runs cspot, a Spotify Connect receiver. The
   speaker appears in Spotify apps as `ADI Speaker` (ZeroConf over mDNS on the LAN). After
   a Spotify app selects it once, the login blob is stored in NVS and reused after reboot.
   Audio is decoded on the device, resampled to 16 kHz and mixed with the voice output.
2. **Control (backend).** The backend uses the Spotify Web API with the user's OAuth token
   (`/api/v1/spotify/login`). Voice commands become tool calls (`play_music`,
   `pause_music`, `set_volume`, …). When a request comes from a device session
   (`device:<id>`) and names no device, the backend targets `SPOTIFY_DEFAULT_DEVICE`.
   Spotify's cloud then forwards play/pause/volume to cspot on the speaker.

So "Alexa, play Believer" goes: speaker → backend (STT, LLM, tool) → Spotify Web API →
Spotify cloud → cspot on the same speaker. If the speaker hasn't been selected in a Spotify
app since it last lost its session, Spotify doesn't list it and the backend tells the user to
select "ADI Speaker" once.

## LED ring

`adi_voice` publishes `connecting | idle | listening | processing | speaking | error`.
The `ring` script in `adi-speaker.yaml` maps that to `ring_mode`
(0 normal, 1 listening, 2 busy, 3 error), switches the amplifier (`amp_update`) and ducks
Spotify by 30 dB while ADI listens or talks. The `ADI Ring` light effect renders 7 WS2812
pixels every 40 ms with `AdiRing` from `components/adi_spotify/adi_ring.h`; the music level
comes from the Spotify PCM, never from the microphone.
