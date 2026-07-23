"""ADI Spotify Connect: makes the speaker selectable in Spotify (via cspot)."""

from pathlib import Path

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import automation
from esphome.components import esp32, speaker
from esphome.const import CONF_ID, CONF_NAME, CONF_PORT, CONF_TRIGGER_ID
from esphome.core import CORE

CODEOWNERS = []
DEPENDENCIES = ["esp32", "network", "psram"]
AUTO_LOAD = ["ring_buffer", "mdns"]

CONF_SPEAKER = "speaker"
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_BITRATE = "bitrate"
CONF_INITIAL_VOLUME = "initial_volume"
CONF_BUFFER_DURATION = "buffer_duration"
CONF_ON_STATE = "on_state"

adi_spotify_ns = cg.esphome_ns.namespace("adi_spotify")
AdiSpotify = adi_spotify_ns.class_("AdiSpotify", cg.Component)
StateTrigger = adi_spotify_ns.class_("StateTrigger", automation.Trigger.template(cg.std_string))

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(AdiSpotify),
        cv.Required(CONF_SPEAKER): cv.use_id(speaker.Speaker),
        # Name shown in the Spotify app. Defaults to the device's friendly_name.
        cv.Optional(CONF_NAME): cv.string_strict,
        # Your Spotify developer app (same one the backend uses). Put them in secrets.yaml.
        cv.Required(CONF_CLIENT_ID): cv.string_strict,
        cv.Required(CONF_CLIENT_SECRET): cv.string_strict,
        cv.Optional(CONF_PORT, default=8090): cv.port,
        cv.Optional(CONF_BITRATE, default=160): cv.one_of(96, 160, 320, int=True),
        cv.Optional(CONF_INITIAL_VOLUME, default=0.5): cv.percentage,
        cv.Optional(CONF_BUFFER_DURATION, default="1000ms"): cv.All(
            cv.positive_time_period_milliseconds,
            cv.Range(min=cv.TimePeriod(milliseconds=300), max=cv.TimePeriod(milliseconds=5000)),
        ),
        cv.Optional(CONF_ON_STATE): automation.validate_automation(
            {cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(StateTrigger)}
        ),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    spk = await cg.get_variable(config[CONF_SPEAKER])
    cg.add(var.set_speaker(spk))
    cg.add(var.set_device_name(config.get(CONF_NAME) or CORE.friendly_name or CORE.name))
    cg.add(var.set_client_id(config[CONF_CLIENT_ID]))
    cg.add(var.set_client_secret(config[CONF_CLIENT_SECRET]))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_bitrate(config[CONF_BITRATE]))
    cg.add(var.set_initial_volume(config[CONF_INITIAL_VOLUME]))
    cg.add(var.set_buffer_ms(config[CONF_BUFFER_DURATION].total_milliseconds))

    for conf in config.get(CONF_ON_STATE, []):
        trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
        await automation.build_automation(trigger, [(cg.std_string, "state")], conf)

    # cspot + bell, packaged as a local ESP-IDF component (outside components/ so ESPHome
    # does not try to compile its sources itself).
    cspot_dir = Path(__file__).resolve().parent.parent.parent / "idf_components" / "cspot"
    esp32.add_idf_component(name="cspot", path=str(cspot_dir))
    esp32.include_builtin_idf_component("esp_http_server")  # ZeroConf endpoint
    # cspot/bell use C++ exceptions for network/stream errors (as in Squeezelite). Without
    # this, a throw would abort() and reboot the speaker instead of being handled.
    esp32.add_idf_sdkconfig_option("CONFIG_COMPILER_CXX_EXCEPTIONS", True)
    # cspot tasks keep their (large) stacks in PSRAM.
    esp32.add_idf_sdkconfig_option("CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY", True)
    # cspot (AP + CDN + token sockets) + ZeroConf HTTP + ADI WebSocket + OTA + mDNS.
    esp32.add_idf_sdkconfig_option("CONFIG_LWIP_MAX_SOCKETS", 16)
