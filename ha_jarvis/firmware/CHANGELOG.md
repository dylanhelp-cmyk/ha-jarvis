# 0.1.2 — non-blocking network startup and reconnect

- Move WebSocket networking and teardown off ESPHome's main loop.
- Wait for network readiness before connecting; keep reconnects in the network worker.
- Pin [Espressif's fix](https://github.com/espressif/esp-protocols/commit/b2b86d7388de0d7335ff21df1cc15e6ea278caf1) for the WebSocket task completion race.
- Firmware only: the HA App remains 0.1.1; the audio protocol and local credentials are unchanged.

Rebuild your existing `voice-pe-repository.yaml` in Builder after its five-minute Git cache expires, and confirm project version 0.1.2 in the compile output before installing. A boot-looping device may require USB installation. After installing, first verify stable Wi-Fi, native API and Jarvis raw-ready status; then resume acoustic tests. Hardware confirmation remains user-run.
