# 0.1.6 - measure PCM sends with a modestly longer wait

- Increase the PCM send API timeout from 30 to 100 ms to test the observed send-failure hypothesis. Keep control sends at 100 ms. Underlying lock/poll/socket phases mean this is not a hard per-call deadline or a proven fix.
- Retain a bounded per-capture `diagnostics.pcm_send` snapshot under the boot/connection/capture identities. Report successful/failed send counts, separate maximum SDK call durations, maximum enqueue-to-dequeue residence, and the latest failed return/expected bytes/duration/uptime. Preserve zero/negative returns. Stop/reconnect retains the summary; a new accepted capture replaces it and ignores stale old send returns.
- Include diagnostics in `capture_stopped`, refreshing the PCM summary in the sending worker after its preceding PCM call returns. Preserve an older capture's snapshot if a newer capture already replaced the retained summary.
- Increase outgoing **text control** slots from 2048 to 3072 bytes for the combined fault/send envelope; keep the 350-byte encoded identity budget. The 16-slot audio queue, PCM protocol, callback's nonblocking enqueue, capture limits and failure/reset behavior are unchanged. Do not retry a failed frame on the same connection.
- Fifteen firmware regressions passed, including production numeric recording/serialization, capture reset/retention, zero/negative/short failures, concurrent snapshots, stale attribution, worker stop refresh and worst-case JSON bounds. Fully compiled/linked for ESP32-S3 with dummy secrets. These measurements do not establish acoustic latency or hardware reliability. No hardware was contacted/flashed/restarted.

Update the accompanying App revision, rebuild the existing Builder entry after its five-minute cache expires and confirm **0.1.6**. Run one **AirPlay / WAV - Stream stability** trial with the full existing observation tail, then export after completion or reconnection. Development images contain dummy credentials and are not installation artifacts.

# 0.1.5 — awake Wi-Fi and retain the SDK transport event

- Set Wi-Fi power saving to `NONE` in the shared repository package and enforce it through ESPHome's supported final-validation helper. Existing local repository entries inherit the policy without file copying. ESPHome's ESP32 default was `LIGHT`; [Espressif's ESP32-S3 / ESP-IDF 5.5.6 guidance](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32s3/api-guides/wifi.html#wi-fi-power-saving-mode) recommends disabling modem sleep to minimize real-time receive delay, at increased power consumption. This removes a source of latency; the reported drop is not proven to have been caused by power saving.
- Preserve the first numeric TCP SDK event as optional `diagnostics.transport_fault`. The pinned client can dispatch a generic `ERROR` before its abort path supplies TCP/TLS/socket diagnostics on `DISCONNECTED`; a later generic PCM-send failure previously displaced that middle event. Keep the original root reason/codes unchanged, and omit the supplemental record when root/last already contain it. Reconnect retention and exact boot/connection/capture identities remain intact.
- Keep outgoing slots at 2048 bytes and limit encoded identity fields to 350 bytes combined; maximum-length 64-byte ASCII IDs and 256-byte ASCII tokens still fit. Test the real serializer's largest valid three-record envelope and the SDK's generic-error → numeric-disconnect → PCM-failure ordering offline.
- Keep the 30 ms PCM timeout, 100 ms control timeout, audio protocol, capture limits and reconnect/lifecycle behavior unchanged. No firmware was flashed or device restarted during development.

Rebuild the existing local entry in ESPHome Builder after its five-minute repository cache expires and confirm **0.1.5** before installing. The policy and additional retained SDK evidence need a new device build; development images contain dummy credentials and are not installation artifacts.

# 0.1.4 — retain disconnect evidence in RAM

- Export bounded `diagnostics` in both `hello` and `status`, including after reconnect. A random boot ID, monotonic uptime and connection/capture generations identify the affected boot and capture. `capture_started` includes both generations for exact App correlation.
- Retain the first fault of the latest failed connection and the latest secondary fault, plus cumulative per-reason counters. Wi-Fi loss, control/PCM send failures, WebSocket errors/closure/disconnection/unexpected task finish, malformed controls and control/outgoing queue faults are recorded under a short critical section. Later teardown cannot overwrite the first cause; reconnect does not erase it. Intentional worker stop and OTA shutdown are excluded from unexpected-finish faults.
- Copy only numeric WebSocket/TLS/socket/HTTP diagnostics supplied by the pinned client's event. No audio, URLs, credentials or free-form library error text enters the RAM history. History resets at reboot and is never written to flash. Warning logs show bounded reason/generation summaries; the next App export retains evidence even if ESPHome logs were closed.
- Bound outgoing controls to 2048 bytes; validate the combined JSON-encoded device identity and token against a 512-byte budget before code generation. App-generated pairing tokens are unchanged. Receive controls, audio wire format, timeouts, reconnect policy and watchdog/lifecycle behavior are unchanged.
- Validate offline causal-ordering, reconnect retention, stale-send attribution, concurrent recording/snapshots and real pinned ArduinoJson serialization, then fully compile/link for ESP32-S3 using dummy secrets. No device was contacted, flashed or restarted. This adds diagnosis, not a claimed fix for the reported intermittent capture disconnect.

Rebuild your existing local entry in ESPHome Builder after its five-minute repository cache expires, confirm project version **0.1.4**, and install through Builder when ready. Reproduce a capture and export App diagnostics after the device reconnects. Development images contain dummy credentials and are not installation artifacts.

# 0.1.3 — isolate PCM sends from WebSocket receive work

- Enable the pinned WebSocket client's separate transmit mutex. Receive and heartbeat work previously shared the mutex used by the PCM sender, whose 30 ms lock deadline could trigger a transport reset during an otherwise healthy capture.
- Log whether a control or PCM send failed before requesting a transport reset. Logs contain byte counts only, with no credentials or audio.
- Keep send timeouts, non-blocking microphone callbacks, the wire protocol and capture limits unchanged.
- Validate configuration/code generation and fully compile/link on ESPHome 2026.10.0 / ESP-IDF 5.5.6; confirm the separate transmit mutex in generated SDK configuration.

This removes a confirmed contention hazard; the cause of the reported automatic cancellation is not proven without device logs. Rebuild your existing `voice-pe-repository.yaml` after its five-minute Git cache expires, confirm project version 0.1.3, then install using Builder. No device was flashed during development; acoustic confirmation remains user-run.

# 0.1.2 — non-blocking network startup and reconnect

- Move WebSocket networking and teardown off ESPHome's main loop.
- Wait for network readiness before connecting; keep reconnects in the network worker.
- Pin [Espressif's fix](https://github.com/espressif/esp-protocols/commit/b2b86d7388de0d7335ff21df1cc15e6ea278caf1) for the WebSocket task completion race.
- Firmware only: the HA App remains 0.1.1; the audio protocol and local credentials are unchanged.

Rebuild your existing `voice-pe-repository.yaml` in Builder after its five-minute Git cache expires, and confirm project version 0.1.2 in the compile output before installing. A boot-looping device may require USB installation. After installing, first verify stable Wi-Fi, native API and Jarvis raw-ready status; then resume acoustic tests. Hardware confirmation remains user-run.
