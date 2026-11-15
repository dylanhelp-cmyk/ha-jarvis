# Install and test HA Jarvis

1. Add the [HA Jarvis repository](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fdylanhelp-cmyk%2Fha-jarvis) in Home Assistant and open the App through its ingress panel.
2. Reuse the existing ESPHome Builder entry and local secrets. Follow the [firmware instructions](ha_jarvis/firmware/INSTALL.md) to validate, compile and update the paired Voice PE. Keep port 8099 private on the LAN.
3. Connect powered speakers to the 3.5 mm jack or leave the jack unplugged to use the Voice PE's internal speaker with App 0.2.46 and firmware 0.2.7. Check the active output in Jarvis and run one local playback test. Run Echo reference alignment on the selected output before a voice preview.

Local tests make no OpenAI calls. API credentials stay in the App's local data and audio and transcript retention default off.

For the new greeting, use App 0.2.43 and Voice PE firmware 0.2.6. In **Jarvis → Wake acknowledgment**, generate the new cached clip, play the preview and confirm it says “Yes sir. How can I help?” Wait for the Voice PE cache to show ready. Run **Echo reference alignment** before trying **Voice preview**.

To try a voice reply, run **Echo reference alignment** with the speakers in their usual position. Then select **Voice preview**, review and confirm the per-run room microphone consent, and start. When armed, say “Hey Jarvis” followed by a short question. To measure a warm turn on the same connection, enable the optional follow-up and wait for “listening for your warm follow-up” before speaking again. The preview sends cleaned audio to OpenAI GPT-Live and uses the API test budget. Repeat alignment after moving the speakers or restarting the App or Voice PE. Interruptions during a reply remain a future milestone.

To keep the preview ready between uses, select **Stay ready for “Hey Jarvis”**, review the repeated audio consent and choose **Enable wake standby**. Each wake can start a budgeted conversation with multiple follow-ups. Select **I'll wait for the greeting to finish before speaking** before enabling standby if you prefer to ask your opening question after the greeting. After a reply finishes, standby listens for 10 seconds of inactivity and then returns to local wake detection. The preview session time limit still applies. **Stop wake standby** disarms it. An App restart also turns standby off.

For an Assist test, use a non-admin Home Assistant access token and choose an exposed light or switch by its exact name. In **Voice preview**, enable the one-command option, fill in the device and token, review the microphone consent, and start. When armed, say “Hey Jarvis, turn on [device name]” or ask to turn it off. The token is held in memory for this run.

## Recovery

Keep a Supervisor backup of the previous App and its data, and a matching firmware build or fixed source checkout, before upgrading. The rollback pair is App **0.1.36** and firmware **0.1.6**, from commit [ae6d932](https://github.com/dylanhelp-cmyk/ha-jarvis/tree/ae6d932). Restore the App and firmware together. Use the fixed source from that commit if Builder's usual repository entry now points to the wired firmware. Keep the existing device name and secrets. If OTA recovery is unavailable, install the fixed firmware through USB.
