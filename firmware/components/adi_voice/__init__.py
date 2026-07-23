"""ADI voice: streams mic audio to the ADI backend over WebSocket and plays the reply."""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import automation
from esphome.components import esp32, micro_wake_word, microphone, speaker
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_PORT, CONF_SPEAKER, CONF_TRIGGER_ID

CODEOWNERS = ["@adi"]
DEPENDENCIES = ["esp32", "network", "micro_wake_word", "microphone", "speaker"]
AUTO_LOAD = ["json", "ring_buffer"]

adi_voice_ns = cg.esphome_ns.namespace("adi_voice")
AdiVoice = adi_voice_ns.class_("AdiVoice", cg.Component)
StartAction = adi_voice_ns.class_("StartAction", automation.Action)
StopAction = adi_voice_ns.class_("StopAction", automation.Action)
StateTrigger = adi_voice_ns.class_("StateTrigger", automation.Trigger.template(cg.std_string))
ConnectionTrigger = adi_voice_ns.class_("ConnectionTrigger", automation.Trigger.template())

CONF_MICRO_WAKE_WORD = "micro_wake_word"
CONF_HOST = "host"
CONF_PATH = "path"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_NAME = "device_name"
CONF_TOKEN = "token"
CONF_MAX_RECORD = "max_record_seconds"
CONF_SILENCE = "silence_ms"
CONF_NO_SPEECH = "no_speech_timeout"
CONF_ON_STATE = "on_state"
CONF_ON_CONNECTED = "on_connected"
CONF_ON_DISCONNECTED = "on_disconnected"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(AdiVoice),
        cv.Required(CONF_MICROPHONE): cv.use_id(microphone.Microphone),
        cv.Required(CONF_SPEAKER): cv.use_id(speaker.Speaker),
        cv.Required(CONF_MICRO_WAKE_WORD): cv.use_id(micro_wake_word.MicroWakeWord),
        cv.Required(CONF_HOST): cv.string,
        cv.Optional(CONF_PORT, default=8000): cv.port,
        cv.Optional(CONF_PATH, default="/api/v1/device/ws"): cv.string,
        cv.Required(CONF_DEVICE_ID): cv.string,
        cv.Optional(CONF_DEVICE_NAME, default="ADI Speaker"): cv.string,
        cv.Optional(CONF_TOKEN, default=""): cv.string,
        cv.Optional(CONF_MAX_RECORD, default=12): cv.int_range(min=2, max=30),
        cv.Optional(CONF_SILENCE, default=1200): cv.int_range(min=300, max=5000),
        cv.Optional(CONF_NO_SPEECH, default=5): cv.int_range(min=1, max=20),
        cv.Optional(CONF_ON_STATE): automation.validate_automation(
            {cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(StateTrigger)}
        ),
        cv.Optional(CONF_ON_CONNECTED): automation.validate_automation(
            {cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(ConnectionTrigger)}
        ),
        cv.Optional(CONF_ON_DISCONNECTED): automation.validate_automation(
            {cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(ConnectionTrigger)}
        ),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    esp32.add_idf_component(name="espressif/esp_websocket_client", ref="1.5.0")
    # By default the library writes the auto-PONG (reply to the server's 20 s PING) from its own
    # task with NO lock, while a data frame - which is two socket writes, header then payload - may
    # be half-written by another task. A separate TX lock makes sends, PING and PONG take turns.
    # The lock timeout must exceed adi_voice's 5 s write timeout, otherwise a slow write would make
    # the PONG give up and the library abort the connection.
    esp32.add_idf_sdkconfig_option("CONFIG_ESP_WS_CLIENT_SEPARATE_TX_LOCK", True)
    esp32.add_idf_sdkconfig_option("CONFIG_ESP_WS_CLIENT_TX_LOCK_TIMEOUT_MS", 7000)

    cg.add(var.set_microphone(await cg.get_variable(config[CONF_MICROPHONE])))
    cg.add(var.set_speaker(await cg.get_variable(config[CONF_SPEAKER])))
    cg.add(var.set_wake_word(await cg.get_variable(config[CONF_MICRO_WAKE_WORD])))
    cg.add(var.set_uri(f"ws://{config[CONF_HOST]}:{config[CONF_PORT]}{config[CONF_PATH]}"))
    cg.add(var.set_device_id(config[CONF_DEVICE_ID]))
    cg.add(var.set_device_name(config[CONF_DEVICE_NAME]))
    cg.add(var.set_token(config[CONF_TOKEN]))
    cg.add(var.set_max_record_ms(config[CONF_MAX_RECORD] * 1000))
    cg.add(var.set_silence_ms(config[CONF_SILENCE]))
    cg.add(var.set_no_speech_ms(config[CONF_NO_SPEECH] * 1000))

    for conf in config.get(CONF_ON_STATE, []):
        trig = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
        await automation.build_automation(trig, [(cg.std_string, "state")], conf)
    for conf in config.get(CONF_ON_CONNECTED, []):
        trig = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var, True)
        await automation.build_automation(trig, [], conf)
    for conf in config.get(CONF_ON_DISCONNECTED, []):
        trig = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var, False)
        await automation.build_automation(trig, [], conf)


ACTION_SCHEMA = automation.maybe_simple_id({cv.GenerateID(): cv.use_id(AdiVoice)})


@automation.register_action("adi_voice.start", StartAction, ACTION_SCHEMA)
@automation.register_action("adi_voice.stop", StopAction, ACTION_SCHEMA)
async def action_to_code(config, action_id, template_arg, args):
    parent = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_arg, parent)
