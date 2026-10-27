# Voice PE acoustic test firmware

Copy this directory into ESPHome Builder's configuration directory, preserving `components/jarvis_stream/`. Open `voice-pe-testbench.yaml`. Tested baseline: **ESPHome 2026.10.0 / ESP-IDF 5.5.6**, for Home Assistant Voice Preview Edition hardware.

Add the keys in `secrets.example.yaml` to Builder's `secrets.yaml`: Wi-Fi, fresh API encryption key, unique OTA password, App LAN URL (`ws://HA_LAN_IP:8099/device`) and App-generated pairing token. Replace the intentionally invalid all-zero API example. This milestone authenticates over **plain LAN WebSocket**; keep port 8099 private.

Run **Validate**, then **Install → Manual download** for a review candidate. No firmware was flashed or installed. Flashing/acoustic tests remain user-controlled. Native API, OTA, mute, LEDs and local Hey Jarvis are supported; this input-test configuration replaces the stock pipeline/player and keeps the speaker amp off.

Hardware derives from [official Voice PE YAML](https://github.com/esphome/home-assistant-voice-pe/blob/d4e6fa43d6a1a7d342b7d4d403567e06516719de/home-assistant-voice.yaml) at `d4e6fa43d6a1a7d342b7d4d403567e06516719de`, including its license. The same commit supplies `voice_kit`. [XMOS v1.3.1](https://github.com/esphome/voice-kit-xmos-firmware/releases/tag/v1.3.1) implements `NONE/AEC/IC/NS/AGC` selection. **On future first boot, upstream automatically updates XMOS if its version differs**, with its checksum-verified official image.

PCM channel 0 selects `NONE` before AEC/IC/NS/AGC, with unity gain and no ESPHome DC correction. Supported `MicrophoneSource` converts 32-bit stereo I2S to mono PCM16LE. Capture requires actual XMOS 1.3.1 and `NONE` readback. Channel 1 uses `NS`/gain 4 for Hey Jarvis, pinned to official model commit `40ff33f57f8fc6ad71a75ef085abbf742495b225` (upstream Apache-2.0). Wake never starts upload.

Only explicit App tests upload PCM, bounded to 120 seconds. Mute, center button, disconnect, fault or timeout stop capture; reconnect never resumes it. LEDs: red muted, blue capturing, dim white paired/raw verified, amber not ready, brief green wake. Clear center-button mute through Home Assistant's software mute switch.

Espressif `esp_websocket_client` 1.5.0 is pinned at `29f1dec4083cdb9a051fcbb1ca5908e9d4141fa0`. The App's JSON controls and 24-byte little-endian `<4sBBHIQI` PCM header are implemented. Controls must be single text frames under 768 bytes. Counters expose drops; stale audio is never replayed across captures.

Timestamps estimate first-sample time from callback minus block duration; **XMOS/I2S delay remains uncalibrated**. Clock sync uses the same 32-bit monotonic millisecond epoch (49-day wrap). Raw PCM is not calibrated SPL. Missing frames/poor sync invalidate measurements. Physical mute, audio quality, wake accuracy and wireless reliability remain untested.

Local validation passed schema, actual C++ compilation, linking and image generation (1,458,815-byte image; 38.6% static RAM, 82% app partition free). Initial Windows linker-generator batch handling needed its original Python command invoked directly; the final ordinary ESPHome compile then reported success. The image uses temporary development credentials and is **not an installation artifact**; compile your own in Builder.
