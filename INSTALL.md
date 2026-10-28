# Install and update — 0.1.2

This is the **local wireless audio bench**, not the conversational assistant yet. It makes **no OpenAI calls**. You control installation, firmware flashing and every audible test. Keep the previous GPT-Live App stopped.

**App 0.1.2:** extends delayed-playback measurements and adds microphone health diagnostics. Update the App; keep working **firmware 0.1.2** (no reflash for this App update). Firmware 0.1.2 addresses the earlier startup/reconnect watchdog path. In HA's ESPHome pairing dialog, use the **Voice PE IP and port 6053**; the firmware's separate Jarvis server URL uses the **HA host IP and port 8099**.

## Install the Home Assistant OS App

1. [Add the HA Jarvis repository](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fdylanhelp-cmyk%2Fha-jarvis). Alternatively, open **Settings → Apps → Install app → ⋮ → Repositories** and add `https://github.com/dylanhelp-cmyk/ha-jarvis`.
2. Find **HA Jarvis Audio Bench** under **HA Jarvis**, and install version **0.1.2** or later. Refresh/check for updates if it has not appeared yet. If experimental Apps are hidden, enable Advanced mode in your HA profile.
3. Start the App and select **Open Web UI**. The interface is available through authenticated HA ingress, not a public web port. No ZIP/file copying or GitHub token is needed. The first image build downloads dependencies and can take several minutes.

In **Connection**, enter the WiiM's LAN IP and `http://YOUR_HA_LAN_IP:8099` as the stream base URL. Begin with **Native HTTP / WAV**. Save settings before downloading firmware.

Native control uses HTTPS with certificate verification enabled. If the receiver reports a self-signed certificate error, the advanced setting lets you explicitly disable verification for this configured LAN receiver. It is never disabled automatically.

The App does not adjust amplifier volume. Test signals use a modest digital level, but set a comfortable listening volume yourself. This bench interrupts the active speaker source and does **not** restore music automatically yet.

## Prepare Voice PE in ESPHome Builder

1. Preserve your current device YAML and secrets for recovery before replacing firmware.
2. Save the App connection settings, then select **Download one-time ESPHome setup**. Copy only `voice-pe-repository.yaml` into `/config/esphome/` or create that file using Builder's editor. It pulls its shared firmware package and components directly from this public GitHub repository.
3. Merge the setup's `jarvis-secrets-to-merge.yaml` into Builder's existing `secrets.yaml`; keep existing secrets. Ensure `wifi_ssid` and `wifi_password` are correct. The setup supplies the App pairing token, URL and native-API/OTA credentials. They remain local, persist across App updates and do not change on repeated setup downloads.
4. Open `voice-pe-repository.yaml` in Builder 2026.10.0 or later, **Validate**, then **Install** using your normal method. The first replacement of the previous experimental firmware may require USB or its existing OTA credentials. Keep the new credentials and device name unchanged for subsequent OTA updates.
5. The official `voice_kit` component updates XMOS to pinned v1.3.1 on first boot if needed. Wait for completion and for Jarvis to report a paired, raw-ready microphone. Add the new ESPHome device to HA using its generated API key if you want its mute/status controls.

The test firmware keeps local Hey Jarvis detection, native API, OTA and LEDs, but replaces the old voice pipeline/player. Waking it only emits an event in this milestone: it does not start a conversation, upload audio or say “Yes sir” yet. The Voice PE speaker amplifier stays off. Only a test started in the App uploads microphone audio.

Physical mute and the center button stop capture. The center button enables software mute; clear it using the new ESPHome device's **Microphone mute** switch. Blue LEDs mean test capture; red means muted; dim white means paired/raw-ready; amber means not ready.

Recovery: restore your saved configuration through Builder/USB, or use the [official Voice PE installer](https://esphome.github.io/home-assistant-voice-pe/). Rebuild in Builder with your own secrets; no development-validation `.bin` is distributed.

## Update later revisions

- **App:** Home Assistant checks this repository for new App versions. Use the App's **Update** button when one is available; **Check for updates** refreshes the store. Settings and pairing credentials in `/data` are retained. Do not uninstall/reinstall for routine updates.
- **Voice PE:** keep your local `voice-pe-repository.yaml` and secrets. Builder checks the GitHub `main` package and component when validating/compiling after a five-minute cache expires. Choose **Install** to build and flash an update. No source folders need copying, and nothing automatically flashes the device. The project version is shown in device metadata.
- App and firmware updates remain separate. Each revision's short change note will say which one needs updating. This project is still the audio-test milestone.

## Run the first test

1. Leave Voice PE in its normal listening position. Keep the room quiet and the WiiM idle. Confirm volume and speaker readiness in the interface.
2. Start **Playback latency**. Three different local probes play. The bench listens for a further 15 seconds after streaming to detect buffered playback; a batch takes about 30–45 seconds. Repeat the batch once from idle and once with the stream/path warmed. Do not change route settings mid-batch. If the WiiM rejects HTTPS verification with a self-signed certificate error, disable **Verify WiiM HTTPS certificate** for that LAN receiver and save.
3. Read the **emitted-marker → microphone-detected sound** measurements separately from the first **control request → sound** startup estimate, which includes a 100 ms probe lead-in. These are playback-path measurements, not assistant end-of-speech latency.
4. Try **Stop response**, one trial per run. It issues a real stop while a 30-second pilot is still available, then listens for acoustic silence. Use **Stream stability** for a continuous pilot and repeated probes. Noise can invalidate acoustic measurements; missing signals are unavailable, never zero.
5. Export diagnostics and share the result. Export contains numeric results and redacts configured addresses/tokens; no microphone recording is retained.

The uncertainty shown covers clock synchronization and detector resolution. **XMOS/I2S microphone pipeline delay is not calibrated** and remains included in the acoustic estimate. Hardware accuracy, actual playback buffering and echo cancellation are not yet established.

## Compare other wireless routes

**SlimProto:** enable the WiiM's LMS/Squeezelite support and select this HA host as its server. Save the App connection to start its listener, then select the connected player. Default TCP/UDP port is 3483. If Music Assistant already owns that port, use a distinct port supported by the WiiM's server configuration or temporarily disable the competing provider yourself. The App never stops it for you. Requested buffers are 1 KB / 100 ms, not measured speaker latency.

**FLAC:** select it after WAV; the App image includes ffmpeg. A receiver may accept one framing and reject another; report the route/codec combination.

**AirPlay baseline:** uses a maintained sender with same-session cancellation, aimed only at the configured receiver. Pairing-required receivers are reported unsupported in this build; no credentials are collected. Chromecast remains deferred unless the main routes fail.

Do not expose LAN audio port 8099 or the SlimProto listener to the internet. There are no cloud credentials or cloud sessions in this build.

## Development validation

Offline checks use synthetic audio, fake receivers and loopback APIs. Install `ha_jarvis/requirements.txt` in a development environment and run `python tools/check.py`. The original firmware passed a real ESP32-S3 compile/link; repository packaging was validated/code-generated on ESPHome 2026.10.0. The Home Assistant container image still needs its first build in your HA installation; Docker Desktop's Linux engine was not running here.
