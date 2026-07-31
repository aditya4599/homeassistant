# Security notes

## What is kept out of git

| File | Holds | Template in the repo |
|---|---|---|
| `firmware/secrets.yaml` | Wi-Fi SSID/password, backend IP, device token, OTA password, Spotify client ID/secret | `firmware/secrets.example.yaml` |
| `backend/.env` | API keys (Gemini, Groq, OpenRouter, ElevenLabs, Brave), Spotify client secret, `API_KEY`, device tokens | `backend/.env.example` |
| `backend/.spotify_token.json` | Spotify OAuth access + refresh token | none (created by `/api/v1/spotify/login`) |
| `backend/debug_audio/` | WAV recordings of everything said to the speaker (when `DEVICE_DEBUG_AUDIO_DIR` is set) | none |

All of these are listed in the root `.gitignore` and in the component `.gitignore` files.

## If credentials were ever shared

`.gitignore` only stops *new* commits from picking a file up. It does not remove a file from
existing Git history, from ZIP archives, or from anywhere a copy was sent. If any of the files
above left your machine (a pushed commit, a shared ZIP, an upload), treat their contents as
exposed and rotate them:

- **Spotify:** reset the client secret in the Spotify developer dashboard, and remove the app
  under your Spotify account's *Manage apps* page to revoke the stored refresh token. Then
  update `.env` and `secrets.yaml` and run `/api/v1/spotify/login` again.
- **Gemini / Groq / OpenRouter / ElevenLabs / Brave:** delete the key in each provider's
  console and create a new one.
- **`API_KEY`, `DEVICE_TOKENS`, `DEVICE_SHARED_TOKEN`, OTA password:** generate new random
  values (for example `python -c "import secrets; print(secrets.token_urlsafe(32))"`), update
  both `.env` and `secrets.yaml`, and reflash the device.
- **Wi-Fi password:** change it on the router if the network matters to you.

If a secret was committed, rewriting history (`git filter-repo`) and force-pushing is also
needed, and it still does not help anyone who already cloned it. Rotation is the fix.

## Things to know about the design

- **The Spotify client secret is compiled into the firmware.** cspot needs it on the device.
  Anyone holding a built `.bin` or a flash dump can extract it. Don't publish firmware
  binaries; `*.bin` and `*.elf` are git-ignored.
- **The device WebSocket is unencrypted (`ws://`).** The device token is visible to anyone
  who can sniff the LAN. Keep the backend on a trusted network.
- **Set `API_KEY` on anything but a throwaway dev setup.** With it empty, `/api/v1/chat`,
  `/api/v1/voice`, the Spotify control routes and `/api/v1/device/clients` are open to anyone
  who can reach the port, and device auth falls back to disabled when no device token is set.
- **`/api/v1/spotify/login` and `/callback` are not behind `API_KEY`** (a browser has to open
  them). Anyone on the network can start the OAuth flow and link a different Spotify account,
  which replaces the stored token.
- **`DEVICE_DEBUG_AUDIO_DIR`** saves every recording to disk. Leave it empty in normal use.
- Computer control is off by default and only touches allowlisted apps, sites and paths.
