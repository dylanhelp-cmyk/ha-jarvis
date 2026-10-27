# First test build — 0.1.0

This is the **local wireless audio bench**, not the conversational assistant yet. It makes **no OpenAI calls**. You control installation, firmware flashing and every audible test. Keep the previous GPT-Live App stopped.

## Install the Home Assistant OS App

1. Download this private repository through GitHub **Code → Download ZIP**, or use the supplied `ha-jarvis-0.1.0.zip`.
2. Copy the project contents into `/addons/ha_jarvis/` on Home Assistant OS, using the Samba `addons` share or your existing file-transfer method. `config.yaml`, `Dockerfile`, `requirements.txt`, `jarvis/` and `firmware/` must sit directly inside that folder. Do not copy `.venv`, `data`, or ESPHome build caches.
3. Open **Settings → Apps → Install app**, refresh/check for updates, then find **HA Jarvis Audio Bench** under **Local apps**. Install it. The first local image build downloads dependencies and can take several minutes.
4. Start the App and select **Open Web UI**. The interface is available through authenticated HA ingress, not a public web port.

In **Connection**, enter the WiiM's LAN IP and `http://YOUR_HA_LAN_IP:8099` as the stream base URL. Begin with **Native HTTP / WAV**. Save settings before downloading firmware.

Native control uses HTTPS with certificate verification enabled. If the receiver reports a self-signed certificate error, the advanced setting lets you explicitly disable verification for this configured LAN receiver. It is never disabled automatically.

The App does not adjust amplifier volume. Test signals use a modest digital level, but set a comfortable listening volume yourself. This bench interrupts the active speaker source and does **not** restore music automatically yet.

## Prepare Voice PE in ESPHome Builder

1. Preserve your current device YAML and secrets for recovery before replacing firmware.
2. In the Jarvis interface, select **Download firmware bundle**. Copy the bundle's `firmware/` contents into `/config/esphome/`, preserving `components/jarvis_stream/` and the two YAML files.
3. Merge `jarvis-secrets-to-merge.yaml` into Builder's existing `secrets.yaml`; keep existing secrets. Ensure `wifi_ssid` and `wifi_password` are correct. The downloaded bundle supplies the App pairing token, URL and fresh native-API/OTA credentials. Keep it private.
4. Open `voice-pe-testbench.yaml` in Builder 2026.10.0 or later, **Validate**, then **Install** using your normal method. Changed native-API/OTA credentials may require a first USB installation; do not assume the previous firmware accepts the new password.
5. The official `voice_kit` component updates XMOS to pinned v1.3.1 on first boot if needed. Wait for completion and for Jarvis to report a paired, raw-ready microphone. Add the new ESPHome device to HA using its generated API key if you want its mute/status controls.

The test firmware keeps local Hey Jarvis detection, native API, OTA and LEDs, but replaces the old voice pipeline/player. Waking it only emits an event in this milestone: it does not start a conversation, upload audio or say “Yes sir” yet. The Voice PE speaker amplifier stays off. Only a test started in the App uploads microphone audio.

Physical mute and the center button stop capture. The center button enables software mute; clear it using the new ESPHome device's **Microphone mute** switch. Blue LEDs mean test capture; red means muted; dim white means paired/raw-ready; amber means not ready.

Recovery: restore your saved configuration through Builder/USB, or use the [official Voice PE installer](https://esphome.github.io/home-assistant-voice-pe/). The firmware bundle contains source; rebuild in Builder with your own secrets. The development-validation `.bin` is deliberately not distributed.

## Run the first test

1. Leave Voice PE in its normal listening position. Keep the room quiet and the WiiM idle. Confirm volume and speaker readiness in the interface.
2. Start **Playback latency**. Three different local probes play. Repeat the batch once from idle and once with the stream/path warmed. Do not change route settings mid-batch.
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

Offline checks use synthetic audio, fake receivers and loopback APIs. Run `python -m unittest discover -s tests -v` after installing requirements in a development environment. Firmware schema and a real ESP32-S3 compile/link passed on ESPHome 2026.10.0. The Home Assistant container image still needs its first build in your HA installation; Docker Desktop's Linux engine was not running here.
