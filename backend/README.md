# Adi – Personal Voice Assistant Backend

A FastAPI backend for a personal voice assistant (default name **Adi**, configurable
with `ASSISTANT_NAME`). It turns speech or text into answers and actions:

```
microphone ─► STT ─► wake-word gate ─► Gemini (LLM) ─► tools (Spotify, time, math, weather, …)
                                             │
speaker    ◄─ TTS ◄──────── spoken reply ◄───┘
```

Everything runs offline with **mock** providers by default (no API keys needed).
Switch to **Google Gemini**, or **Groq** (STT) / **OpenRouter** (LLM) / **ElevenLabs** (TTS),
for real speech and answers, connect **Spotify** for music, and connect an **ESP32** (or any
device) over a WebSocket.

This is the `backend/` half of the ADI repository. The ESP32 firmware that talks to it is in
`../firmware/`; how the two connect is described in `../docs/firmware-backend-protocol.md`.

---

## 1. Installation

**Python 3.12+** is required (3.12 is what the Docker image uses).

```bash
cd backend
python3.12 -m venv .venv            # macOS: brew install python@3.12 if needed
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-dev.txt  # runtime + test dependencies
cp .env.example .env
```

## 2. Environment variables

All configuration lives in `.env` (never commit it). **`.env.example` documents
every setting** with comments; the most important ones:

| Area | Variables |
|---|---|
| Providers | `STT_PROVIDER` = `mock`/`gemini`/`groq`, `LLM_PROVIDER` = `mock`/`gemini`/`openrouter`, `TTS_PROVIDER` = `mock`/`gemini`/`elevenlabs` |
| Other providers | `GROQ_*`, `OPENROUTER_*`, `ELEVENLABS_*` (key + model + timeout for each) |
| Identity | `ASSISTANT_NAME`, `WAKE_WORD`, `WAKE_WORD_MODE`, `WAKE_WORD_ALIASES`, `WAKE_ACK_TEXT` |
| Behaviour | `ASSISTANT_TIMEZONE`, `CONVERSATION_MAX_TURNS`, `CONVERSATION_TTL_SECONDS`, `CONVERSATION_DEFAULT_SESSION`, `FAST_INTENTS_ENABLED`, `TOOL_DIRECT_REPLIES` |
| Gemini | `GEMINI_API_KEY`, `GEMINI_STT_MODEL`, `GEMINI_LLM_MODEL`, `GEMINI_TTS_MODEL`, `GEMINI_TTS_VOICE`, `GEMINI_*` tuning, `GEMINI_TIMEOUT_SECONDS` |
| Spotify | `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI`, `SPOTIFY_SCOPES`, `SPOTIFY_TOKEN_FILE`, `SPOTIFY_TIMEOUT_SECONDS`, `SPOTIFY_DEFAULT_DEVICE` |
| Tools | `TOOLS_TIMEOUT_SECONDS`, `WEATHER_PROVIDER`, `WEATHER_DEFAULT_LOCATION`, `WEATHER_UNITS`, `WEB_SEARCH_PROVIDER`, `WEB_SEARCH_GEMINI_MODEL`, `BRAVE_SEARCH_API_KEY` |
| Computer control | `COMPUTER_CONTROL_ENABLED`, `COMPUTER_CONTROL_APPS`, `COMPUTER_CONTROL_WEBSITES`, `COMPUTER_CONTROL_PROJECTS`, `COMPUTER_CONTROL_PROJECT_APP` |
| Devices / audio | `DEVICE_WS_ENABLED`, `DEVICE_TOKENS`, `DEVICE_SHARED_TOKEN`, `DEVICE_*_SECONDS`, `DEVICE_AUDIO_INPUT_*`, `DEVICE_AUDIO_OUTPUT_*`, `DEVICE_AUDIO_CHUNK_BYTES`, `DEVICE_MAX_AUDIO_SECONDS`, `DEVICE_AUDIO_NORMALIZE`, `DEVICE_DEBUG_AUDIO_DIR` |
| Security | `API_KEY` (protects chat/voice/Spotify controls/device REST), `MAX_AUDIO_BYTES` |

Invalid values (unknown provider, bad timezone, …) stop the app at startup with a
clear message. Selecting `gemini` without `GEMINI_API_KEY` also fails fast.

## 3. Gemini setup

1. Create a key at <https://aistudio.google.com/apikey>.
2. In `.env`:

```bash
GEMINI_API_KEY=your-key
STT_PROVIDER=gemini
LLM_PROVIDER=gemini
TTS_PROVIDER=gemini
```

Defaults: `gemini-3.5-transcribe` (STT), `gemini-3.5-flash-lite` (LLM),
`gemini-3.8-flash-lite-tts` (TTS, voice `Kore`). As of 2026-09-30 Google's pricing
page listed all three as free on the free tier. Free-tier rate limits still apply,
and on the free tier Google may use your content to improve its products.

First real check (makes 4 API calls; writes `question.wav` and `reply.wav`):

```bash
python scripts/gemini_smoke_test.py
```

The SDK is `google-genai` (the old `google-generativeai` package is not used).

## 4. Spotify setup

Playback control needs **Spotify Premium** on the app owner's account.
Development-mode apps are limited to 5 users, and since February 2026 search
returns at most 10 results.

1. Open <https://developer.spotify.com/dashboard> → **Create app** → select **Web API**.
2. Add this **Redirect URI** exactly (Spotify rejects `localhost`; use `127.0.0.1`):
   `http://127.0.0.1:8000/api/v1/spotify/callback`
3. Put the credentials in `.env`:

```bash
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8000/api/v1/spotify/callback
```

## 5. Spotify OAuth

Start the backend, then open in a browser:

```text
http://127.0.0.1:8000/api/v1/spotify/login
```

Approve access. The callback stores a refresh token in `.spotify_token.json`
(permissions 600, git-ignored). Access tokens refresh automatically. **Spotify
refresh tokens expire about six months after you authorize.** When that happens,
the assistant says *"Spotify needs to be reconnected"* and `/spotify/status` shows
`connected: false`. Just open `/spotify/login` again.

Open Spotify on at least one device (phone, laptop, web player) so it appears
as a playback device.

## 6. Starting the backend

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Or with Docker: `docker compose up --build` (from this folder; needs Docker Compose
v2.24+ for the optional `env_file`). Compose passes `.env` into the container and keeps the
Spotify refresh token on the `adi-data` volume (`/srv/data/.spotify_token.json`), so it
survives rebuilds. Computer control only works when running directly on your Mac, not in Docker.

Checks:

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/v1/health/providers   # stt/llm/tts in use
curl -s http://127.0.0.1:8000/api/v1/health/tools       # enabled tools, Spotify, devices
```

## 7. API

Interactive docs: <http://127.0.0.1:8000/docs> (hidden when `APP_ENV=production`).
If `API_KEY` is set, send `X-API-Key: <key>` on protected routes.

| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness |
| `GET /api/v1/health/providers` | Active STT/LLM/TTS |
| `GET /api/v1/health/tools` | Enabled tools, Spotify connection, connected devices |
| `POST /api/v1/chat` | `{"message": "...", "session_id": "optional"}` → `{"response": "...", "tools_used": [...]}` |
| `POST /api/v1/voice` | Raw audio body (`audio/wav` or raw PCM) → spoken reply `audio/wav`; optional `X-Session-ID` header |
| `GET /api/v1/spotify/login` · `/callback` | OAuth |
| `GET /api/v1/spotify/status` · `/devices` · `/current` | Connection, devices, what's playing |
| `POST /api/v1/spotify/play?query=&artist=&device=` | Play a song/artist, optionally on a device |
| `POST /api/v1/spotify/pause` · `/resume` · `/next` · `/previous` · `/transfer?device=` | Controls |
| `WS /api/v1/device/ws` | ESP32 / device protocol (section 9) |
| `GET /api/v1/device/clients` | Connected devices |
| `POST /api/v1/device/clients/{id}/say` | Speak text on a device (announcements/tests) |

`/voice` response headers: `X-Transcript`, `X-Response-Text` (URL-encoded),
`X-Assistant-State` (`idle`, or `listening` when only the wake word was heard),
`X-Wake-Word`, `X-Tools-Used`. It returns **204** when `WAKE_WORD_MODE=require`
and no wake word was spoken.

### Errors

Every error uses one envelope, never a stack trace:
`{"error": {"code": "...", "message": "...", "request_id": "..."}}`. Technical
details go to the JSON logs with the same `request_id`. Secrets are never logged.

| Status | Code | Meaning |
|---|---|---|
| 400 | `invalid_audio` · `invalid_request` | Bad audio / request |
| 401 | `unauthorized` | Missing/wrong `X-API-Key` |
| 401 | `spotify_not_connected` · `spotify_auth_expired` | Run `/spotify/login` (again) |
| 404 | `spotify_not_found` · `spotify_device_not_found` · `device_not_connected` | Nothing matched |
| 409 | `spotify_device_ambiguous` · `spotify_no_device` · `spotify_nothing_playing` | Ask which device / open Spotify / nothing playing |
| 413 / 415 / 422 | `payload_too_large` · `unsupported_audio_format` · `no_speech_detected` | Audio issues |
| 502 | `provider_error` · `provider_auth_error` · `spotify_error` | Upstream failed |
| 503 | `provider_rate_limited` · `provider_unavailable` · `spotify_rate_limited` · `spotify_not_configured` | Try later / configure |
| 504 | `provider_timeout` | Upstream too slow |

In conversation, tool failures become natural sentences instead: *"Spotify isn't
connected yet."*, *"I couldn't find your T2 Pro."*, *"I found two phones: vivo T2
Pro 5G or Pixel 8. Which one should I use?"*, *"I couldn't find that song."*

## 8. How the assistant works

**Tools** (provider-neutral, `app/services/tools/`). The LLM chooses them through
function calling; the backend executes them. The LLM never sees credentials.

| Tool | Enabled when | Examples |
|---|---|---|
| `play_music` (song / artist / device), `play_last_searched`, `pause_music`, `resume_music`, `next_track`, `previous_track`, `current_playback`, `transfer_playback`, `list_spotify_devices`, `set_volume`, `volume_up`, `volume_down`, `mute_music`, `unmute_music` | Spotify configured | "Play Love Dose by Diljit on my T2 Pro", "Play something by Arijit Singh", "Play this on my phone" |
| `get_time`, `get_date` | always | uses `ASSISTANT_TIMEZONE` |
| `calculate` | always | safe AST evaluator. No `eval`, bounded exponents. "15% of 840" |
| `get_weather` | `WEATHER_PROVIDER=open_meteo` (default, free, no key) | city from the request or `WEATHER_DEFAULT_LOCATION` |
| `web_search` | `WEB_SEARCH_PROVIDER=gemini` (Grounding with Google Search) or `brave` | "Look up the latest on…" |
| `open_app`, `close_app`, `open_website`, `open_project` | `COMPUTER_CONTROL_ENABLED=true` (macOS) | allowlisted targets only |

To add a tool, write a function returning `list[Tool]` in `app/services/tools/`,
then register it in `app/services/tools/setup.py`.

**Speed and quota.** Exact short commands ("pause", "next song", "what's
playing?", "what time is it?") are matched locally and never hit the LLM
(`FAST_INTENTS_ENABLED`). Action tools return their own confirmation ("Paused.",
"Playing Believer by Imagine Dragons on your MacBook."), which is spoken directly instead of
making a second LLM call (`TOOL_DIRECT_REPLIES`). A typical "play X on Y" costs
one LLM call. Information tools like web search go back to the LLM to be phrased.

**Device matching** (`app/services/spotify/devices.py`) tries, in order: an exact
name match, then all spoken words appearing in the device name ("T2 Pro" → "vivo
T2 Pro 5G"), then category aliases (phone/mobile → smartphone; laptop, computer,
Mac, MacBook → computer, preferring names containing "mac"). If several devices
match at the best level, it asks which one; it never picks randomly. With no device
named, it uses the active device, or the only device, or asks. Requests that come from an
ADI device session (`device:<id>`) with no device named go to `SPOTIFY_DEFAULT_DEVICE`
(the speaker's Spotify Connect name, e.g. `ADI Speaker`).

**Multi-turn conversation.** Recent turns are kept in memory per session
(`CONVERSATION_MAX_TURNS`, idle expiry `CONVERSATION_TTL_SECONDS`). Chat uses
`session_id` (or `CONVERSATION_DEFAULT_SESSION`), voice uses `X-Session-ID`, and
devices use their device ID. So "Play Love Dose on my T2 Pro" → "Pause" → "Resume"
→ "Play it on my MacBook instead" works. Memory is not persisted across restarts.

**Personality.** The system prompt (`app/services/prompts.py`) uses
`ASSISTANT_NAME`. It asks for short spoken replies, confirmation of actions,
short clarifying questions, and no markdown. It never mentions tools or internals.

## 9. Voice pipeline and wake word

`POST /api/v1/voice` and device audio run the same steps: validate audio → STT →
**wake-word gate** → chat (fast intent, or LLM plus tools) → TTS → WAV.

Wake-word detection *on audio* belongs on the device (low power, private). The
backend only sees transcripts and applies `WAKE_WORD_MODE`:

| Mode | Behaviour |
|---|---|
| `strip` (default) | "Adi, play Believer" → command "Play Believer". Speech without the wake word is still processed. Use this when the device already detected the wake word. |
| `require` | Speech that doesn't start with the wake word (or a `WAKE_WORD_ALIASES` spelling) is ignored: HTTP 204, or `response.ignored` on the WebSocket. Use this for always-listening devices. |
| `off` | No handling |

A bare "Adi" returns `WAKE_ACK_TEXT` ("Yes?") with state `listening`, so the
device keeps recording for the command. That gives the flow wake word → listen →
command → response.

Test with real speech (after `scripts/gemini_smoke_test.py` created `question.wav`):

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/voice -H "Content-Type: audio/wav" \
  --data-binary @question.wav -D - -o reply.wav && afplay reply.wav
# your own voice (macOS):
say --file-format=WAVE --data-format=LEI16@16000 -o mine.wav "Adi, what time is it?"
```

## 10. ESP32 communication architecture

One WebSocket per device: `ws://<backend>:8000/api/v1/device/ws`. Text frames are
JSON with a `type`. Binary frames carry audio. Any client message may include an
`id`; replies echo it as `reply_to`. Unknown fields are ignored, so both sides can
evolve. Protocol version: **1**.

**Device → backend**

| type | Fields | Purpose |
|---|---|---|
| `hello` | `device_id`, `token`, `firmware`, `name`, `capabilities{audio_input, audio_output, wake_word_on_device, binary_frames}`, optional `audio_output{encoding, sample_rate, channels}` | Must be the first message (within `DEVICE_HELLO_TIMEOUT_SECONDS`). A token may instead be sent as `Authorization: Bearer`. |
| `command` | `text`, `want_audio` | Typed or forwarded text command |
| `audio_start` | optional `format{encoding: pcm_s16le\|wav, sample_rate, channels}`, `want_audio` | Begin a recording. Defaults come from `DEVICE_AUDIO_INPUT_*`. |
| *binary frames* | raw audio | Microphone data |
| `audio_chunk` | `data` (base64) | Alternative to binary frames |
| `audio_end` / `audio_cancel` | – | Process / discard the recording |
| `status` | `state`, `battery`, `rssi`, `volume`, `extra` | Device telemetry |
| `ping` / `pong` | `ts` | Heartbeat |
| `bye` | `reason` | Clean disconnect |

**Backend → device**

| type | Purpose |
|---|---|
| `welcome` | `session_id`, heartbeat/timeout, negotiated `audio_input`/`audio_output`, wake word and mode |
| `status` | `processing` → `speaking` → `idle`/`listening` (also `recording`) |
| `response` | `text`, `transcript`, `state`, `audio` (bool), `tools_used`, `ignored` |
| `audio_output_start` | `encoding`, `sample_rate`, `channels`, `sample_width`, `bytes`, `chunks`; then **binary chunks**; then `audio_output_end` |
| `error` | `code`, `message`, `reply_to`. Safe text only. |
| `ping` / `pong` | Heartbeat (server pings every `DEVICE_HEARTBEAT_SECONDS`) |

**Example voice turn**

```text
→ {"type":"hello","device_id":"esp32-desk","token":"…","capabilities":{"audio_input":true,"audio_output":true,"wake_word_on_device":true}}
← {"type":"welcome","session_id":"device:esp32-desk",...}
→ {"type":"audio_start","id":"a1","format":{"encoding":"pcm_s16le","sample_rate":16000,"channels":1}}
→ <binary PCM> <binary PCM> …
→ {"type":"audio_end"}
← {"type":"status","state":"processing","reply_to":"a1"}
← {"type":"response","reply_to":"a1","text":"Playing Love Dose by Yo Yo Honey Singh on your T2 Pro.","transcript":"Play Love Dose on my T2 Pro","state":"idle","audio":true}
← {"type":"audio_output_start","encoding":"wav","sample_rate":24000,"bytes":…}
← <binary audio chunks>
← {"type":"audio_output_end","reply_to":"a1"}
← {"type":"status","state":"idle"}
```

**Connection management.** Any number of devices can connect, keyed by
`device_id`. Reconnecting with the same ID replaces the old socket (close code
4000) and keeps its conversation memory. If nothing is received for
`DEVICE_TIMEOUT_SECONDS`, the socket closes with 4008. Other close codes: 4001
auth failed, 4002 protocol error, 4003 disabled. One request per device runs at
a time; a second gets `error busy`.

**Authentication.** Use `DEVICE_TOKENS="esp32-desk:token1,kitchen:token2"` (per
device, recommended) or `DEVICE_SHARED_TOKEN`. If neither is set, `API_KEY` is
used. If nothing is set, auth is off in development and devices are refused in
production.

**Audio formats.** Microphone input may be headerless `pcm_s16le` (any rate from
8 to 48 kHz, 1 or 2 channels) or WAV. Speaker output is `wav` or `pcm_s16le`,
resampled to `DEVICE_AUDIO_OUTPUT_SAMPLE_RATE` or to the rate the device requests
in `hello`. 0 means the TTS native rate (24 kHz for Gemini). Chunk size is set by
`DEVICE_AUDIO_CHUNK_BYTES`. The ADI firmware asks for `pcm_s16le` at 16 kHz in its
`hello`, which overrides the `DEVICE_AUDIO_OUTPUT_*` defaults.

Quick test without hardware: `pip install websockets`, then:

```python
import asyncio, json, websockets
async def main():
    async with websockets.connect("ws://127.0.0.1:8000/api/v1/device/ws") as ws:
        await ws.send(json.dumps({"type": "hello", "device_id": "test-pc"}))
        print(await ws.recv())
        await ws.send(json.dumps({"type": "command", "text": "what time is it?", "want_audio": False}))
        for _ in range(3): print(await ws.recv())
asyncio.run(main())
```

## 11. Example commands

Via `POST /api/v1/chat` (`{"message": "..."}`) or spoken:

| You say | Adi |
|---|---|
| "Play Love Dose." | "Playing Love Dose by Yo Yo Honey Singh." |
| "Play Love Dose on my T2 Pro." | "Playing Love Dose by Yo Yo Honey Singh on your T2 Pro." |
| "Pause." / "Resume." | "Paused." / "Playing again." |
| "Next song." / "Go back." | "Skipping." / "Going back." |
| "What's playing?" | "Love Dose by Yo Yo Honey Singh." |
| "Play Believer on my MacBook." | "Playing Believer by Imagine Dragons on your MacBook." |
| "Play this on my phone." | "Playing it on your phone." (or asks which phone, if you have two) |
| "Play something by Arijit Singh." | "Playing songs by Arijit Singh." |
| "What time is it?" / "What's today's date?" | "It's 4:35 PM." / "Today is Wednesday, September 30, 2026." |
| "Calculate 25 times 36." / "What's 15% of 840?" | "That's 900." / "That's 126." |
| "What's the weather in Delhi?" | "It's 31 degrees and partly cloudy in Delhi, …" |
| "Explain recursion." / "Tell me a joke." | Short spoken answer from Gemini |
| "Open Spotify." / "Open YouTube." | With computer control enabled |

In mock mode, the fast intents, calculator, clock, and simple
"play X on Y" / "calculate …" phrasings work without any API key. Other text is
echoed back.

## 12. Tests

```bash
pip install -r requirements-dev.txt
pytest            # no network, no API keys: Gemini, Spotify and devices are faked
```

`scripts/gemini_smoke_test.py` is the only check that calls a real provider (needs
`GEMINI_API_KEY` and network access).

## 13. Troubleshooting

| Symptom | Fix |
|---|---|
| "Spotify isn't connected" / `spotify_not_connected` | Open `/api/v1/spotify/login`. |
| "Spotify needs to be reconnected" | The refresh token expired (~6 months) or was revoked. Log in again. |
| `INVALID_CLIENT: Invalid redirect URI` | Dashboard redirect URI must exactly match `SPOTIFY_REDIRECT_URI` (use `127.0.0.1`). |
| "No Spotify device is available" | Open Spotify on the phone or computer (play and pause once), then retry. `GET /spotify/devices` shows what Spotify sees. |
| "I couldn't find your T2 Pro" | Check the exact name in `/spotify/devices`; any word from it works ("T2 Pro"). |
| "Spotify Premium is required" | Spotify only allows playback control for Premium accounts. |
| 403 errors from Spotify for a new user | Development-mode apps need the user added under the app's User Management (max 5). |
| `provider_rate_limited` | Gemini free-tier limit. Wait, or check limits in AI Studio. Fast intents and direct replies reduce calls. |
| App won't start: `GEMINI_API_KEY is required` | A provider or `WEB_SEARCH_PROVIDER=gemini` needs the key. |
| `422 no_speech_detected` | Nothing intelligible (the tone from `make_test_wav.py` is not speech). |
| Voice returns 204 | `WAKE_WORD_MODE=require` and the wake word wasn't heard. Add spellings to `WAKE_WORD_ALIASES`. |
| Wrong time | Set `ASSISTANT_TIMEZONE`, e.g. `Asia/Kolkata` or `America/Indiana/Indianapolis`. |
| Weather asks "Which city?" | Set `WEATHER_DEFAULT_LOCATION`. |
| Device WebSocket closes 4001 | Token doesn't match `DEVICE_TOKENS` / `DEVICE_SHARED_TOKEN` / `API_KEY`. |
| Device closes with 4008 | No messages within `DEVICE_TIMEOUT_SECONDS`. Answer the server's `ping` with `pong`. |
| "Computer control isn't supported on this machine" | Only macOS is implemented; run the backend on the Mac, not in Docker. |

## 14. Project layout

```
app/
  api/            chat, voice, spotify, device (WebSocket + REST), health
  core/           config, errors (AppError hierarchy), logging, middleware, security
  services/
    chat.py           conversation: fast intents → LLM ↔ tools, memory
    conversation.py   short-term per-session memory
    intents.py        deterministic fast path for exact short commands
    prompts.py        voice personality prompt
    wake.py           wake-word gate
    stt/ llm/ tts/    provider interfaces + mock + gemini (+ groq / openrouter / elevenlabs)
    gemini/           shared Gemini client and error mapping
    spotify/          OAuth client, device resolver, Spotify tools
    tools/            registry, clock, calculator, weather, web search, computer, setup
    computer/         allowlisted, shell-free platform controllers (macOS)
    device/           WebSocket protocol, connection manager, session
    voice/            audio validation + pipeline
    wav.py            WAV/PCM helpers, resampling
scripts/            make_test_wav.py, gemini_smoke_test.py
alembic/            migration environment (no tables or migrations yet)
tests/              unit + integration (no network)
```

Security notes: secrets come only from `.env`/environment. `.env`,
`.spotify_token.json`, `debug_audio/`, audio files, and caches are git-ignored. See
`../docs/security.md`. Computer control
never runs a shell and only touches allowlisted targets.
