# Spotify Connect + voice volume — implementation notes

## Design
- **cspot as a local ESP-IDF component** (`idf_components/cspot/`), pulled in by the ESPHome
  component `components/adi_spotify/` via `esp32.add_idf_component(path=...)`. It lives outside
  `components/` so ESPHome doesn't try to compile cspot's sources itself.
- **Shim** `idf_components/cspot/adi_cspot.cpp` is adapted from Squeezelite's `Shim.cpp`
  (player loop, ZeroConf `GET/POST /spotify_info`, mDNS `_spotify-connect._tcp`, Spirc events).
  Squeezelite's config/NVS/sink plumbing is replaced by a small C API (`include/adi_cspot.h`).
- **Name**: `${friendly_name}` = "ADI Speaker". ZeroConf is always on (any account can select it).
- **Credentials**: Spotify developer app ID/secret come from `secrets.yaml`
  (`spotify_client_id`, `spotify_client_secret`). After a successful login the reusable login
  blob is stored in NVS (`adi_spotify`/`creds`) from the ESPHome loop (cspot tasks have PSRAM
  stacks and must not write flash) and used to reconnect automatically after reboot.
- **Audio path (lowest risk)**: the I2S speaker + microphone clocking are unchanged (16 kHz).
  `cspot 44.1 kHz stereo -> PSRAM ring buffer -> resampler (16 kHz) -> mixer source
  music_input -> I2S speaker`; `adi_voice` TTS uses mixer source `tts_input`. `adi_voice` code
  is unchanged (only its `speaker:` points at `tts_input`).
- **Volume**: Spotify Connect 0..65535 -> normalized 0.0–1.0 -> gain n² (perceptual), applied
  to music PCM only (TTS untouched). Persisted across reboots (ESPHome preferences).
- **Voice volume**: backend sets volume through the Spotify Web API
  (`PUT /me/player/volume`), Spotify forwards it to the speaker (cspot VOLUME event).
  "volume N" = N/10 (5 -> 50%), up/down = ±10%, mute stores the previous level, unmute restores.
- **Amplifier**: on while ADI speaks OR Spotify plays (`amp_update` script).
- **Ducking**: music drops 30 dB while ADI listens/thinks/talks, restored when idle.
- **Backend default device**: `SPOTIFY_DEFAULT_DEVICE=ADI Speaker`. Requests from the ADI
  device (session `device:<id>`) with no device named play there. If Spotify doesn't list it
  yet, ADI says to open Spotify and select "ADI Speaker" once.

## Known limitations
- Music is resampled to 16 kHz and mixed to the (mono) speaker output: reduced fidelity, by
  design, to keep the proven mic/I2S setup.
- cspot obtains its CDN token with the client-credentials grant using your developer app;
  compatibility with Spotify's 2026 development-mode restrictions is unverified on hardware.
- Position reporting to Spotify is approximate (± local buffer, ~1 s).
- Memory/CPU headroom with wake word + mic + WebSocket + Vorbis + mixer + resampler running
  together is untested on the board.
- Seeking/next flushes the local buffer; a short gap is expected.
