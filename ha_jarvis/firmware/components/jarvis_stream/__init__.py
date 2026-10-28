"""Explicit-test PCM transport for Home Assistant Voice PE."""
import esphome.codegen as cg
import esphome.config_validation as cv
import json
from urllib.parse import urlsplit
from esphome.components import esp32, i2c, microphone
from esphome.const import CONF_ID, CONF_MICROPHONE

DEPENDENCIES = ["esp32", "wifi", "i2c"]
AUTO_LOAD = ["json"]
ns = cg.esphome_ns.namespace("jarvis_stream")
JarvisStream = ns.class_("JarvisStream", cg.Component, i2c.I2CDevice)

def server_url(value):
    value = cv.string_strict(value)
    parsed = urlsplit(value)
    if parsed.scheme != "ws" or not parsed.hostname or parsed.path != "/device" or parsed.query or parsed.fragment or parsed.username:
        raise cv.Invalid("Use ws://LOCAL_APP_HOST:8099/device with no credentials or query")
    return value

def raw_microphone(value):
    value = microphone.microphone_source_schema()(value)
    if value["channels"] != [0] or value["bits_per_sample"] != 16 or value["gain_factor"] != 1:
        raise cv.Invalid("Acoustic PCM requires channel 0, 16-bit output and unity gain")
    return value

def hello_identity_budget(value):
    # Reserve the remainder of the fixed 2048-byte outgoing slot for bounded
    # diagnostics and hello metadata, including worst-case numeric values.
    identity = {key: value[key] for key in ("device_id", "token")}
    if len(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 512:
        raise cv.Invalid("JSON-encoded device_id and token must fit 512 bytes including field names")
    return value

CONFIG_SCHEMA = cv.All(
    cv.Schema({
        cv.GenerateID(): cv.declare_id(JarvisStream),
        cv.Required(CONF_MICROPHONE): raw_microphone,
        cv.Required("server_url"): server_url,
        cv.Required("device_id"): cv.All(cv.string_strict, cv.Length(min=1, max=64)),
        cv.Required("token"): cv.All(cv.string_strict, cv.Length(min=32, max=256)),
    }).extend(cv.COMPONENT_SCHEMA).extend(i2c.i2c_device_schema(0x42)),
    hello_identity_budget,
    cv.only_with_framework("esp-idf"),
)
FINAL_VALIDATE_SCHEMA = cv.Schema({
    cv.Required(CONF_MICROPHONE): microphone.final_validate_microphone_source_schema("jarvis_stream", sample_rate=16000),
}, extra=cv.ALLOW_EXTRA)

async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await i2c.register_i2c_device(var, config)
    # Passive: microWakeWord owns the microphone lifecycle. Idle PCM stays local.
    source = await microphone.microphone_source_to_code(config[CONF_MICROPHONE], passive=True)
    cg.add(var.set_microphone(source))
    for key in ("server_url", "device_id", "token"):
        cg.add(getattr(var, "set_" + key)(config[key]))
    # RX and heartbeat work can wait up to network_timeout_ms. Keep it from
    # consuming the PCM sender's short mutex deadline on a shared RX/TX lock.
    esp32.add_idf_sdkconfig_option("CONFIG_ESP_WS_CLIENT_SEPARATE_TX_LOCK", True)
    esp32.add_idf_component(
        name="esp_websocket_client",
        repo="https://github.com/espressif/esp-protocols.git",
        # 1.7.0: fixes start/stop completion-bit race; supports ESP-IDF >=5.0.
        ref="b2b86d7388de0d7335ff21df1cc15e6ea278caf1",
        path="components/esp_websocket_client",
    )
