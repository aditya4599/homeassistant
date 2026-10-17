# ADI — personal voice assistant

ADI is a self-hosted voice assistant made of two parts:

- **`firmware/`** — ESPHome firmware for a Waveshare ESP32-S3-AUDIO-Board. It listens for a
  wake word on the device, streams your speech to the backend, plays the spoken reply, drives
  a 7-LED status ring, and doubles as a Spotify Connect speaker ("ADI Speaker").
- **`backend/`** — a FastAPI service that turns speech into an answer or an action:
  speech-to-text → LLM with tools (Spotify, time, calculator, weather, web search, optional
  macOS app control) → text-to-speech.

No Home Assistant is involved. The two parts talk over one WebSocket on your LAN.

```text
┌──────────── ESP32-S3 (firmware/) ────────────┐        ┌───────── backend/ (FastAPI) ─────────┐
│ mic → microWakeWord ("Alexa"/"Okay Nabu")    │  ws:// │ /api/v1/device/ws                     │
│     → adi_voice ── 16 kHz PCM ───────────────┼───────►│ STT → wake-word gate → chat + tools   │
│ speaker ◄─ mixer ◄─ adi_voice ◄─ reply PCM ──┼◄───────┤ TTS → 16 kHz PCM                      │
│              ▲                               │        │            │ Spotify Web API (OAuth) │
│              └─ resampler ◄─ adi_spotify     │        └────────────┼─────────────────────────┘
│                              (cspot) ◄───────┼── Spotify Connect ──┘ via Spotify's cloud
│ LED ring ◄─ voice state + music level        │
└──────────────────────────────────────────────┘
```

## Features

- On-device wake word (stock "Alexa" and "Okay Nabu" models), silence-based end of speech.
- Swappable providers: STT `mock`/`gemini`/`groq`, LLM `mock`/`gemini`/`openrouter`,
  TTS `mock`/`gemini`/`elevenlabs`. Mock mode runs with no API keys.
- Tools: Spotify (play song/artist, pause, resume, skip, what's playing, move playback to a
  named device, volume/mute), time and date, calculator, weather (Open-Meteo), web search
  (Gemini grounding or Brave), allowlisted macOS app/website control (off by default).
- Short-term conversation memory per device, fast local handling of exact short commands.
- Spotify Connect receiver on the speaker; music is ducked while ADI listens or talks.
- LED ring: light blue while listening, rainbow while thinking/speaking, music-reactive
  rainbow while Spotify plays, red on errors.
- REST endpoints for text chat, voice upload, Spotify control and device announcements.

## Repository layout

```text
adi-assistant/
├── firmware/                 ESPHome config + custom components (see firmware/README.md)
│   ├── adi-speaker.yaml
│   ├── components/adi_voice/       wake word → WebSocket → reply playback
│   ├── components/adi_spotify/     Spotify Connect, music volume, LED ring renderer
│   ├── idf_components/cspot/       cspot + bell as an ESP-IDF component (GPL-3.0)
│   ├── tools/adi_ring_host_test.cpp
│   ├── validate_firmware.py
│   ├── NOTES.md  PATCHES.md
│   └── secrets.example.yaml
├── backend/                  FastAPI service (see backend/README.md)
│   ├── app/  tests/  scripts/  alembic/
│   ├── requirements*.txt  pyproject.toml  alembic.ini
│   ├── Dockerfile  docker-compose.yml
│   └── .env.example
├── docs/
│   ├── firmware-backend-protocol.md
│   └── security.md
└── THIRD_PARTY_NOTICES.md
```

## Hardware

- Waveshare ESP32-S3-AUDIO-Board (ESP32-S3, 16 MB flash, octal PSRAM, ES7210 mic ADC,
  ES8311 DAC, TCA9555 expander for the amplifier, 7 × WS2812 LEDs)
- USB-C cable for the first flash
- A computer on the same LAN to run the backend (macOS, Linux or Docker)
- Optional: Spotify Premium (required by Spotify for playback control)

## Quick start

### 1. Backend

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
curl -s http://127.0.0.1:8000/health          # {"status":"ok"}
```

That runs with mock providers. To talk to the speaker, set at least these in `backend/.env`:

```dotenv
DEVICE_TOKENS=esp32-desk:<long-random-token>
WAKE_WORD=Alexa
STT_PROVIDER=gemini          # or groq
LLM_PROVIDER=gemini          # or openrouter
TTS_PROVIDER=gemini          # or elevenlabs
GEMINI_API_KEY=<your key>
```

Every setting is documented in `backend/.env.example`; `backend/README.md` covers each
provider.

### 2. Spotify (optional)

1. Create an app at <https://developer.spotify.com/dashboard> (Web API) and add the redirect
   URI `http://127.0.0.1:8000/api/v1/spotify/callback` exactly.
2. In `backend/.env` set `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` and
   `SPOTIFY_DEFAULT_DEVICE=ADI Speaker`.
3. Start the backend and open `http://127.0.0.1:8000/api/v1/spotify/login` in a browser on
   the same machine. The refresh token is saved to `SPOTIFY_TOKEN_FILE`
   (`.spotify_token.json`, git-ignored) and access tokens refresh automatically.
4. Put the same client ID/secret in `firmware/secrets.yaml` (cspot needs them on the device).
5. After flashing, select **ADI Speaker** once in a Spotify app on the same Wi-Fi.

Scopes used: `user-read-playback-state user-modify-playback-state`. Refresh tokens expire
after roughly six months; log in again when ADI says Spotify needs reconnecting.

### 3. Firmware

```bash
cd firmware
python3 -m venv .venv && source .venv/bin/activate
pip install esphome
cp secrets.example.yaml secrets.yaml     # Wi-Fi, backend LAN IP, device token, OTA, Spotify app
python validate_firmware.py
esphome config adi-speaker.yaml
esphome run adi-speaker.yaml             # first flash over USB
```

`device_token` in `secrets.yaml` must equal the token in `DEVICE_TOKENS=esp32-desk:<token>`,
and `backend_host` must be the backend computer's LAN IP. Details in `firmware/README.md`.

## How the firmware talks to the backend

The speaker opens `ws://<backend_host>:8000/api/v1/device/ws`, sends `hello` (device id,
token, capabilities, wants 16 kHz `pcm_s16le` back) and waits for `welcome`. On a wake word
it sends `audio_start`, binary PCM frames and `audio_end`. The backend replies with
`response` (transcript, reply text) and streams the spoken reply between
`audio_output_start` and `audio_output_end`. The backend pings every 20 s and the device
answers `pong`.

Spotify playback and Spotify control are separate: the speaker is a Spotify Connect device,
and the backend controls it through Spotify's Web API, not over the WebSocket. Full message
tables and the list of values that must match in both configs:
[`docs/firmware-backend-protocol.md`](docs/firmware-backend-protocol.md).

## Configuration files

| File | Committed? | Purpose |
|---|---|---|
| `backend/.env.example` | yes | Every backend setting with comments |
| `backend/.env` | **no** | Your real backend config and API keys |
| `firmware/secrets.example.yaml` | yes | Firmware secrets template |
| `firmware/secrets.yaml` | **no** | Wi-Fi, device token, OTA password, Spotify app credentials |
| `backend/.spotify_token.json` | **no** | Spotify OAuth tokens, written by the login flow |

## Tests

```bash
# Backend: offline, all providers and Spotify faked
cd backend && pytest

# Firmware static checks and LED ring unit test (no hardware)
cd firmware
python validate_firmware.py
g++ -std=c++17 -I components/adi_spotify tools/adi_ring_host_test.cpp -o /tmp/ring_test && /tmp/ring_test
esphome config adi-speaker.yaml          # needs internet (downloads wake word models)
esphome compile adi-speaker.yaml         # full build; first run downloads ESP-IDF
```

Not covered by automated tests: real provider calls (`backend/scripts/gemini_smoke_test.py`
does that manually), real Spotify, and anything on the physical board.

## Docker (backend only)

```bash
cd backend
cp .env.example .env                     # then edit
docker compose up --build
```

The container listens on `APP_PORT` (8000) on all interfaces, so the speaker can reach it.
The Spotify token is stored on the `adi-data` volume. `docker compose --profile db up` also
starts PostgreSQL, but nothing uses the database yet. Computer control does not work inside
Docker.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Speaker log shows WebSocket connect failures | Backend must run with `--host 0.0.0.0`; `backend_host` must be its LAN IP; open port 8000 in the firewall |
| WebSocket closed with 4001 | `device_token` ≠ token in `DEVICE_TOKENS` for `esp32-desk` |
| WebSocket closed with 4008 | No traffic for `DEVICE_TIMEOUT_SECONDS`; usually Wi-Fi trouble |
| Wake word heard but the command includes "Alexa" or is ignored | Set `WAKE_WORD=Alexa`, add misspellings to `WAKE_WORD_ALIASES`, keep `WAKE_WORD_MODE=strip` |
| "Spotify isn't connected" | Open `/api/v1/spotify/login` again |
| `INVALID_CLIENT: Invalid redirect URI` | Dashboard redirect URI must match `SPOTIFY_REDIRECT_URI` exactly, with `127.0.0.1` |
| Music doesn't start on the speaker | Select "ADI Speaker" once in a Spotify app; check `GET /api/v1/spotify/devices` |
| App won't start: `GEMINI_API_KEY is required` | A `gemini` provider or `WEB_SEARCH_PROVIDER=gemini` is selected without a key |

More in `backend/README.md` and `firmware/README.md`.

## Current limitations

- The firmware passes `esphome config` and `esphome compile` (ESPHome 2026.9.1), the static
  checks and the ring host test, but nothing here verifies behavior on the board. Test on hardware.
- The device WebSocket is unencrypted `ws://`, meant for a trusted LAN.
- The Spotify client secret is compiled into the firmware image (cspot needs it). Don't share
  built binaries.
- Music on the speaker is resampled to 16 kHz mono so it can share the voice audio path.
- cspot's compatibility with Spotify's current development-mode restrictions is unverified
  (see `firmware/NOTES.md`).
- The custom wake word "Adi" doesn't exist yet; the stock "Alexa"/"Okay Nabu" models are used.
- Conversation memory is in-process and lost on restart. The database layer and Alembic are
  set up but no tables or migrations exist.
- Computer control is macOS-only and only when the backend runs directly on the Mac.
- `/api/v1/spotify/login` is reachable without `API_KEY` by design (a browser opens it).
- ESPHome 2026.9 prints deprecation warnings for `rgb_order` (removal planned for 2027.3)
  and for the `adi_voice` actions' missing `synchronous=` flag. They don't block the build.

## Security

Never commit `backend/.env`, `firmware/secrets.yaml` or `.spotify_token.json`. If any of
them has left your machine, rotate the credentials — `.gitignore` doesn't remove files from
history or from copies already shared. See [`docs/security.md`](docs/security.md).

## Third-party code and licenses

The firmware includes **cspot** (GPL-3.0), its support library bell and bell's bundled
nanopb (zlib), cJSON (MIT) and Tremor (BSD-style); the cspot glue is adapted from
squeezelite-esp32 (MIT), and the board's audio setup from
esphome-waveshare-esp32-s3-audio-va (MIT). Built firmware images therefore fall under
GPL-3.0. The backend's dependencies are installed from PyPI. Details and obligations:
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

This repository does not yet declare a license for ADI's own code.
