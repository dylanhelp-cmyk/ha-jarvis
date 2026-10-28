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
