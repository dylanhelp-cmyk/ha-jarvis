# 0.1.3

- Reserve “cancelled” for the App Stop button. Microphone disconnect/mute and App shutdown now report a reason, phase and elapsed time.
- Preserve partial microphone health, capture completion/drop reports and stream progress when a test fails. No audio is retained.
- Pair with firmware 0.1.3, which separates WebSocket receive/send locks to remove a confirmed contention hazard during capture. The cause of the reported hardware failures remains unconfirmed until retesting.
- Update both App and firmware, then run one HTTP/WAV playback latency test at comfortable volume with the existing certificate setting. Allow completion and export diagnostics; if it fails, also save the ESPHome logs.

# 0.1.2

- Extend acoustic playback search from 4.5 to 15 seconds and listen longer after streaming, to measure heavily buffered routes.
- Export aggregate microphone levels, clipping, frame count and sample/timestamp spans without retaining audio.
- Clearly label completed tests whose measurement is unavailable; distinguish weak and ambiguous matches without lowering acceptance thresholds.
- App-only update; keep Voice PE firmware 0.1.2. Test once at comfortable volume with HTTP/WAV, export diagnostics after completion (about 30–45 seconds). Keep certificate verification disabled for the receiver reporting a self-signed certificate.

# 0.1.1

- Install/update from a Home Assistant custom repository.
- One-time ESPHome setup fetches firmware packages and components from GitHub.
- Preserve generated API/OTA credentials across setup downloads and App updates.

# 0.1.0

- Initial user-operated local audio bench, wireless transports and raw-microphone test firmware.
