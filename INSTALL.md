# Install and test HA Jarvis

1. Add the [HA Jarvis repository](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fdylanhelp-cmyk%2Fha-jarvis) in Home Assistant and open the App through its ingress panel.
2. Reuse the existing ESPHome Builder entry and local secrets. Follow the [firmware instructions](ha_jarvis/firmware/INSTALL.md) to validate, compile and update the paired Voice PE. Keep port 8099 private on the LAN.
3. Connect powered speakers to the Voice PE's 3.5 mm jack. Start at a low, comfortable speaker level. Confirm the App reports the device and output ready, then run one local playback test. Review the acoustic result separately from transport timing and export diagnostics if a measurement is unavailable.

The wired bench is the first milestone. Conversational playback, echo acceptance and Assist control require later gates. Local tests make no OpenAI calls. API credentials stay in the App's local data and audio and transcript retention default off.

## Recovery

Keep a Supervisor backup of the previous App and its data, and a matching firmware build or fixed source checkout, before upgrading. The rollback pair is App **0.1.36** and firmware **0.1.6**, from commit [ae6d932](https://github.com/dylanhelp-cmyk/ha-jarvis/tree/ae6d932). Restore the App and firmware together. Use the fixed source from that commit if Builder's usual repository entry now points to the wired firmware. Keep the existing device name and secrets. If OTA recovery is unavailable, install the fixed firmware through USB.
