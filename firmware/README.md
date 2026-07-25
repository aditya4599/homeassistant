# ADI speaker firmware — Waveshare ESP32-S3-AUDIO-Board

ESPHome firmware that turns the Waveshare ESP32-S3-AUDIO-Board into ADI's speaker. It does
**not** need Home Assistant.

```text
wake word (on device) → record 16 kHz mono PCM → WebSocket → ADI backend
                                                  (STT → LLM / tools → TTS)
speaker ← 16 kHz PCM reply ← WebSocket ←──────────────────────┘
```

It also runs a Spotify Connect receiver (cspot), so the board shows up in Spotify apps as
**ADI Speaker** and plays music through the same speaker.

## Contents

| Path | What it is |
|---|---|
| `adi-speaker.yaml` | ESPHome config: pins, I2S, codecs, mixer, wake word, LED ring, scripts |
| `components/adi_voice/` | Custom component: wake word → WebSocket upload → reply playback |
| `components/adi_spotify/` | Custom component: Spotify Connect via cspot, music volume, music level for the ring |
| `components/adi_spotify/adi_ring.h` | Dependency-free LED ring renderer and level meter (host-testable) |
| `idf_components/cspot/` | cspot + bell packaged as an ESP-IDF component (GPL-3.0, see `PATCHES.md`) |
| `tools/adi_ring_host_test.cpp` | PC-side unit test for `adi_ring.h` |
| `validate_firmware.py` | Static checks for regressions fixed in earlier builds |
| `NOTES.md` | Spotify Connect design notes and known limitations |
| `PATCHES.md` | Every change made to cspot/bell |
| `secrets.example.yaml` | Template for `secrets.yaml` |

`components/adi_spotify/__init__.py` finds cspot at `../../idf_components/cspot` relative to
itself, and `adi-speaker.yaml` loads `components/` as a local external component. Keep the
three directories side by side and run ESPHome from this folder.

## Hardware

- Waveshare ESP32-S3-AUDIO-Board (ESP32-S3, 16 MB flash, octal PSRAM)
- ES7210 microphone ADC, ES8311 DAC, amplifier enabled through a TCA9555 I/O expander (EXIO8)
- 7 × WS2812 LEDs on GPIO38
- USB-C cable for the first flash

Pin assignments are the `substitutions` at the top of `adi-speaker.yaml`
(I2S MCLK 12, BCLK 13, LRCLK 14, DIN 15, DOUT 16; I2C SDA 11, SCL 10).

## 1. Install ESPHome

`adi-speaker.yaml` requires ESPHome **2025.8.0 or newer** (`min_version`). It was validated
with 2026.9.1.

```bash
cd firmware
python3 -m venv .venv
source .venv/bin/activate
pip install esphome
```

## 2. Secrets

```bash
cp secrets.example.yaml secrets.yaml
```

Fill in `secrets.yaml`:

| Key | Value |
|---|---|
| `wifi_ssid`, `wifi_password` | Your 2.4 GHz Wi-Fi |
| `backend_host` | LAN IP of the computer running the backend (not `localhost`) |
| `device_token` | Same token as `DEVICE_TOKENS=esp32-desk:<token>` in `backend/.env` |
| `ota_password` | Any password for over-the-air updates |
| `spotify_client_id`, `spotify_client_secret` | Your Spotify developer app (the same one the backend uses) |

`device_id` (`esp32-desk`), `backend_port` (`8000`) and `friendly_name` (`ADI Speaker`) are
`substitutions` in `adi-speaker.yaml`, not secrets. `secrets.yaml` is git-ignored; never
commit it.

## 3. Validate

These run without hardware:

```bash
python validate_firmware.py
g++ -std=c++17 -I components/adi_spotify tools/adi_ring_host_test.cpp -o /tmp/ring_test && /tmp/ring_test
esphome config adi-speaker.yaml
```

`esphome config` downloads the microWakeWord models from GitHub, so it needs internet access.

## 4. Build and flash

```bash
esphome compile adi-speaker.yaml   # first build downloads ESP-IDF and takes a while
esphome run adi-speaker.yaml       # first time over USB; later updates can use OTA
esphome logs adi-speaker.yaml
```

## 5. What to expect at startup

1. Wi-Fi connects.
2. `adi_voice` connects to `ws://<backend_host>:8000/api/v1/device/ws`, sends `hello`,
   and gets `welcome`.
3. The ring shows the rotating rainbow and the wake words are armed.

```text
[adi_voice] sending hello
[adi_voice] connected to backend (session device:esp32-desk)
```

The backend must be listening on the LAN (`--host 0.0.0.0`) with a matching device token.

## Voice

Wake words are the stock ESPHome microWakeWord models **"Alexa"** and **"Okay Nabu"**.
A custom "Adi" wake word would need a separately trained model. After the wake word, speak
normally; 1.2 s of silence (`silence_ms`) ends the recording, and recordings are capped at
12 s (`max_record_seconds`). The device sends one 16-bit 16 kHz channel (ES7210 right
channel) and logs the byte count and amplitude stats of each recording, which helps tell
a microphone problem from an STT problem.

## LED ring

| State | Ring |
|---|---|
| Listening (after wake word) | Steady light blue |
| Processing / speaking | Rotating rainbow |
| Idle, Spotify playing | Rainbow whose speed and brightness follow the music level |
| Idle | Rotating rainbow |
| Error | Red, briefly |

The amplifier is on while ADI speaks or Spotify plays (`amp_update` script). Spotify is ducked
by 30 dB while ADI listens, thinks or talks.

## Spotify Connect

Open Spotify on a phone or computer on the same network and pick **ADI Speaker** from the
device list once. The login is saved in flash and reused after reboots. After that the
backend can start music on the speaker by voice (`SPOTIFY_DEFAULT_DEVICE=ADI Speaker` in
`backend/.env`). Volume changes from Spotify or by voice only affect music, not ADI's voice.
See `NOTES.md` for the audio path and limitations.

## Troubleshooting

| Symptom | Check |
|---|---|
| Log shows repeated WebSocket connect failures | `backend_host` is the backend machine's LAN IP; backend started with `--host 0.0.0.0`; firewall allows port 8000 |
| Backend closes the socket with 4001 | `device_token` doesn't match `DEVICE_TOKENS` for `esp32-desk` |
| Wake word never triggers | Say "Alexa" or "Okay Nabu"; check the mic stats in the log |
| Reply never plays | Backend logs; `response` with `audio: false` means TTS was skipped or failed |
| "ADI Speaker" missing in Spotify | Same Wi-Fi as the phone; mDNS not blocked by the router |
| `esphome config` fails downloading models | Needs internet access to GitHub |

## Status

From this repository layout, the firmware passes `validate_firmware.py`, the ring host test
and `esphome config`, and `esphome compile` succeeds with ESPHome 2026.9.1 (RAM 41.8%,
flash 22.1% of the app partition). Whether it behaves correctly on the physical board hasn't
been checked as part of the consolidation; test it on hardware before relying on it.
