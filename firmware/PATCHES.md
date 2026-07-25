# cspot / bell compatibility patches

Source: `squeezelite-esp32/components/spotify/cspot` (cspot + bell submodule), copied into
`idf_components/cspot/cspot/`. Only these changes were made; each is marked `ADI PATCH` in code.

1. **Pre-generated protobuf (no protoc at build time)**
   - `cspot/CMakeLists.txt`: replaced `nanopb_generate_cpp(...)` with a glob of
     `protobuf_gen/protobuf/*.c`; include dir `protobuf_gen` instead of the binary dir.
   - `cspot/bell/CMakeLists.txt`: replaced `find_package(Nanopb REQUIRED)` with
     `set(NANOPB_INCLUDE_DIRS .../external/nanopb)`.
   - Why: ESPHome's ESP-IDF Python env has no `protoc` / `protobuf`.
   - Regenerate (only if the .proto files change), from `idf_components/cspot/cspot/`:
     `pip install "protobuf<5" grpcio-tools "setuptools<70"` then
     `python bell/external/nanopb/generator/nanopb_generator.py -I . -D protobuf_gen protobuf/*.proto`

2. **No `idf::driver` link** — `cspot/bell/CMakeLists.txt`: removed `idf::driver` from the ESP
   `EXTRA_LIBS`. ESPHome excludes the `driver` component; bell only needs it for its audio
   sinks, which are disabled.

3. **`#include <cstring>`** — `cspot/bell/main/io/URLParser.cpp` uses `strstr`; GCC 14 (IDF 5.5)
   no longer pulls `<cstring>` in transitively.

4. **Trimmed bell externals** — removed unused `alac, civetweb, fmt, libhelix-mp3, mdnssvc,
   nlohmann_json, opencore-aacdec, opus, portaudio` and `bell/example` (~35 MB). Kept
   `nanopb, cJSON, tremor`. All removed ones are behind bell options that are OFF.

Build-level settings (not source patches), set by `components/adi_spotify/__init__.py`:
`CONFIG_COMPILER_CXX_EXCEPTIONS=y` (cspot/bell use try/catch for network errors; without it a
throw aborts), `CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY=y` (cspot task stacks in PSRAM),
`CONFIG_LWIP_MAX_SOCKETS=16`, and re-including ESP-IDF's `esp_http_server` (ZeroConf).

## Robustness patches (also marked `ADI PATCH`)

Beyond the build fixes above, these cspot/bell sources carry runtime fixes. Each change is
commented in place with `ADI PATCH:` explaining the old behavior:

| File (under `idf_components/cspot/cspot/`) | What changed |
|---|---|
| `src/AccessKeyFetcher.cpp` | Token-fetch failures (network/TLS exceptions, bad responses) are reported and retried instead of being fatal or silent. |
| `src/TrackQueue.cpp` | Licensing/country data passed through; token and track-metadata failures logged and retried; queue task no longer dies on a network exception. |
| `src/TrackPlayer.cpp` | Tracks progress per track, waits while a slow track is still progressing, catches network errors in the player task, checks decoder open results, distinguishes natural end of track from interruptions. |
| `src/SpircHandler.cpp` | Logs subscription failures, detects reconnects, handles the toggle form of play/pause, logs unsupported commands. |
| `src/PlainConnection.cpp` | Stops on connection failure instead of continuing with an uninitialized socket; frees `addrinfo` on failed attempts. |
| `src/MercurySession.cpp` | Also catches `std::bad_alloc`, not only `std::runtime_error`. |
| `src/CDNAudioFile.cpp` | CDN HTTP errors (403/404/416…) are no longer treated as audio data. |
| `src/PlaybackState.cpp` | Handles a NULL optional `context_uri` from nanopb. |
| `bell/main/io/HTTPClient.cpp`, `bell/main/io/include/HTTPClient.h` | Exposes the HTTP status of the last response. |

Find every change with `grep -rn "ADI PATCH" idf_components/cspot`.
cspot is GPL-3.0 (see `idf_components/cspot/cspot/LICENSE`); these modified files stay under
that license.
