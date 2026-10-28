# 0.1.6

- Pin srptools 1.0.1 for pyatv 0.18.0: srptools 2.0.0 makes the private-key argument keyword-only, causing the reported SRPClientSession TypeError before audio fetch.
- Reproduced the failure locally with 2.0.0; regression now exercises actual legacy AirPlay initialization and a complete in-memory HAP SRP key/proof exchange using dummy credentials with 1.0.1.
- App-only dependency fix; firmware stays 0.1.3. Update the App, run one AirPlay/WAV latency test at comfortable volume, and export diagnostics. Offline authentication checks do not establish receiver playback or acoustic latency.

# 0.1.5

- Surface background AirPlay sender errors immediately while waiting for source fetch, instead of hiding them behind a blank timeout.
- Record receiver capability/pairing flags, bounded transport events and pending function names (no coroutine locals or credentials). Redact device/player identifiers from diagnostic exports.
- Give timeouts nonempty error messages and bound AirPlay discovery/control preparation. Report mandatory pairing as unsupported without attempting it.
- App-only diagnostic update; firmware stays 0.1.3. This does not claim to fix the receiver connection. Run one AirPlay/WAV latency test and export diagnostics even if no sound plays; no additional repeats needed.

# 0.1.4

- Fix SlimProto format negotiation: the HTTP response now repeats 16 kHz/16-bit/mono parameters, preventing the library's response handler from replacing them with 44.1 kHz/stereo defaults.
- Send headerless PCM on SlimProto's uncompressed route so container bytes cannot become audio. HTTP/WAV and FLAC framing are unchanged.
- Offline regression exercises both actual library decoder commands against the served MIME/body; hardware playback needs confirmation.
- App-only update; keep firmware 0.1.3. Select SlimProto / PCM (uncompressed), lower amplifier volume, run one playback latency test and export diagnostics. Stop if distortion remains.

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
