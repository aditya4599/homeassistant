# cspot (Spotify Connect) packaged as an ESP-IDF component for the ADI firmware.
# Adapted from squeezelite-esp32/components/spotify/CMakeLists.txt without the
# Squeezelite-only dependencies (platform_config, services, tools, codecs).

# this must be set *before* idf_component_register
set(CMAKE_CXX_STANDARD 20)

idf_component_register(
    SRCS "adi_cspot.cpp"
    INCLUDE_DIRS "include"
    PRIV_REQUIRES mbedtls nvs_flash esp_http_server esp_timer pthread lwip
)

# Same bell feature set Squeezelite uses: only what cspot needs (no codecs/sinks/webserver).
set(BELL_DISABLE_CODECS ON)
set(BELL_DISABLE_SINKS ON)
set(BELL_DISABLE_FMT ON)
set(BELL_DISABLE_REGEX ON)
set(BELL_ONLY_CJSON ON)
set(BELL_DISABLE_MQTT ON)
set(BELL_DISABLE_WEBSERVER ON)
set(CSPOT_TARGET_ESP32 ON)
# Use bell's bundled Tremor (Vorbis) and cJSON instead of Squeezelite's codecs component.
set(BELL_EXTERNAL_VORBIS "" CACHE STRING "use bell's bundled tremor" FORCE)
set(BELL_EXTERNAL_CJSON "" CACHE STRING "use bell's bundled cJSON" FORCE)

add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/cspot ${CMAKE_CURRENT_BINARY_DIR}/cspot)
target_compile_options(bell PRIVATE -Wno-error -w)
target_compile_options(cspot PRIVATE -Wno-error -w)
target_link_libraries(${COMPONENT_LIB} PRIVATE cspot)
