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
