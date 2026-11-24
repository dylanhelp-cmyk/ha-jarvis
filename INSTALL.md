# Install and test HA Jarvis

1. Add the [HA Jarvis repository](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fdylanhelp-cmyk%2Fha-jarvis) in Home Assistant and open the App through its ingress panel.
2. Reuse the existing ESPHome Builder entry and local secrets. Follow the [firmware instructions](ha_jarvis/firmware/INSTALL.md) to validate, compile and update the paired Voice PE. Keep port 8099 private on the LAN.
3. Connect powered speakers to the 3.5 mm jack or leave the jack unplugged to use the Voice PE's internal speaker with App 0.2.46 and firmware 0.2.7. Check the active output in Jarvis and run one local playback test. Run Echo reference alignment on the selected output before a voice preview.

Local tests make no OpenAI calls. API credentials stay in the App's local data and audio and transcript retention default off.

In **Jarvis settings**, choose a **GPT-Live voice**, **Personality** and **Voice volume**, then save. Voice changes require generating and approving a wake clip for that voice. After a voice or volume change, wait for the clip to cache on the Voice PE and run **Echo reference alignment**. Returning to an already approved voice reuses its clip. A personality change applies on the next conversation.

App **0.2.75** lowers the wake greeting by another 6 dB without changing live reply volume. No firmware update or new clip approval is needed. After the automatic cache refresh and local echo alignment, compare the greeting with the next live reply at your usual speaker level.

**Conversation checks** runs two GPT-Live sessions using generated questions, thanks and goodbye. It checks context across sessions and the closure policy, using up to US$0.10 of the existing cap. The test needs no Voice PE or room microphone audio.

**Generated interruption control** speaks a new question while GPT-Live counts. A pass requires one answer to the new question and no resumed count during the observation. It uses one connection and up to US$0.05, with no room microphone or speakers. For the hardware check, choose **Voice preview** and enable **Listen during replies (experimental)** before starting. Interrupt a longer answer with a different question at your usual volume. Check that the old answer stays stopped and the new answer plays once at the usual level.

To check interruption detection with real sound, run **Echo reference alignment**, then open **Acoustic interruption check** on a browser device playing through a separate speaker near the Voice PE. Confirm both speakers are at a comfortable level, select a test phrase and let both voices finish. Jarvis measures the time from the phrase reaching its microphone and checks whether the opening survives echo cancellation.

For the new greeting, use App 0.2.43 and Voice PE firmware 0.2.6. In **Jarvis → Wake acknowledgment**, generate the new cached clip, play the preview and confirm it says “Yes sir. How can I help?” Wait for the Voice PE cache to show ready. Run **Echo reference alignment** before trying **Voice preview**.

To try a voice reply, run **Echo reference alignment** with the speakers in their usual position. Then select **Voice preview**, review and confirm the per-run room microphone consent, and start. When armed, say “Hey Jarvis” followed by a short question. To measure a warm turn on the same connection, enable the optional follow-up and wait for “listening for your warm follow-up” before speaking again. The preview sends cleaned audio to OpenAI GPT-Live and uses the API test budget. Repeat alignment after moving the speakers or restarting the App or Voice PE.

To keep the preview ready between uses, select **Stay ready for “Hey Jarvis”**, review the repeated audio consent and choose **Enable wake standby**. Each wake can start a budgeted conversation with multiple follow-ups. Select **I'll wait for the greeting to finish before speaking** before enabling standby if you prefer to ask your opening question after the greeting. After a reply finishes, standby listens for 10 seconds of inactivity and then returns to local wake detection. To change that interval, set **Wake standby inactivity timeout (seconds)** and select **Save settings** before enabling standby. The preview session time limit still applies. **Stop wake standby** disarms it. An App restart also turns standby off.

In wake standby, try a clear sign-off such as “Goodbye” or “That's all for now.” Jarvis gives you two seconds after the farewell to speak again. If GPT-Live does not confirm the sign-off, the inactivity timeout still applies.

To try resuming a topic, select **Remember up to two recent exchanges** before enabling wake standby. Ask “What is the capital of France?” and let Jarvis return to **armed for Hey Jarvis** on its own. Within a minute, wake it again and ask “Which river runs through that city?” The option shares the recent text with GPT-Live on the next wake. Jarvis keeps it in memory and clears it when standby stops.

To check a device locally on App **0.2.84**, open **Home Assistant device control**, enter a non-admin token and the name or alias of an Assist-exposed device, and select **Check Assist**. Compare the returned state with Home Assistant. A light can also show brightness, and a thermostat can show its setpoint and current temperature. The check uses the token once and makes no OpenAI calls.

On App **0.2.86**, open **Jarvis settings → Advanced home control** to choose the Assist reasoning model. **Try model on a sample light** uses generated speech, an in-memory light and the displayed budget reservation. Compare the verified state, fresh read, timing and ledger cost in Results. **Save settings** applies your choice to future Assist conversations. No firmware update is needed.

To test Assist in standby, select **Stay ready for “Hey Jarvis”** and **Allow control of Assist-exposed devices** in **Voice preview**. Enter a non-admin Home Assistant access token locally, review the microphone consent and enable standby. Ask for an exposed test light by name, check that it changes state, then ask Jarvis to restore that light as a warm follow-up. Try “Goodbye” to close the conversation. Jarvis keeps the token in memory until standby stops. You can also select Assist without standby to try a single named light or switch.

App **0.2.76** adds **Listen during replies (experimental)** in Voice preview. Run **Echo reference alignment** first and check that the continuous microphone check passes. For one focused trial, enable the warm follow-up and this option, approve the room-audio consent and ask a question. While Jarvis answers, ask a short follow-up. This tests continuous capture and provider listening; reliable immediate playback cancellation is still being developed. Leave the option off to use the previous preview path.

App **0.2.78** increases the playback reserve only for **Listen during replies**. Run the local **Conversation playback stability** check first. It plays two longer references while microphone cleanup runs, with no OpenAI connection. A failed voice trial now keeps **Reply level measurements** in its result, so a volume change can be investigated without retaining audio.

With App **0.2.83** and firmware **0.2.8** installed, run **Conversation playback stability** once. A recovered sender stall can pass when the Voice PE confirms continuous playback within tolerance. If the speaker clock develops a larger gap, the trial's playback diagnostics include `first_clock_gap` for the affected samples. Continue to a separately consented voice trial once acoustic alignment and continuous microphone processing both pass.

## Recovery

Keep a Supervisor backup of the previous App and its data, and a matching firmware build or fixed source checkout, before upgrading. The rollback pair is App **0.1.36** and firmware **0.1.6**, from commit [ae6d932](https://github.com/dylanhelp-cmyk/ha-jarvis/tree/ae6d932). Restore the App and firmware together. Use the fixed source from that commit if Builder's usual repository entry now points to the wired firmware. Keep the existing device name and secrets. If OTA recovery is unavailable, install the fixed firmware through USB.
