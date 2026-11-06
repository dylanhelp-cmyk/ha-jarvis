# HA Jarvis wired restart

The Voice PE 3.5 mm output is the sole Jarvis playback route. Previous wireless measurements are preserved in Git history and cannot establish wired performance. The prior rollback pair is App 0.1.36 / firmware 0.1.6 at ae6d932. The earlier 200 ms wireless lead-in failed both acknowledgment trials. One of two 300 ms controls verified both words at 1536 ms after wake receipt.

## Milestones

1. Remove the retired playback routes and migrate old settings. Keep the App identity, repository, ESPHome entry, pairing, credentials and durable test budget.
2. Bring up authenticated PCM playback through the jack. Preserve ESPHome native API, OTA, local wake, mute and LEDs. Measure audible cold and warm onset, stop-to-silence, stale playback, continuity, queue age, drift and simultaneous capture. Recalibrate host AEC3 using the actual device render reference and verify near-end speech preservation. Report unreliable measurements as unavailable.
3. Cache the verified selected-voice “Yes sir” on the device and play it exactly once after local “Hey Jarvis.” Capture immediately with a bounded timestamped buffer, connect GPT-Live in parallel and expose readiness separately. Give an explicit retry on overflow or startup failure. Compare WebRTC and WebSocket under equivalent first-turn, follow-up and interruption trials. Preserve concise British-style speech and full-duplex conversation during delegated work.
4. Allow only Assist-exposed tools from local /api/mcp/assist. Verify action outcomes before announcing success, treat timeouts as unknown where appropriate, and avoid blind repeats. Interrupt and restore supported music sources on the Voice PE output. Keep the ingress settings, Start/Cancel controls and diagnostic export. Proactive monitoring is deferred.

## Session and acceptance

Clear sign-off can request closure after pending work and audible playback finish, with a two-second grace period cancelled by meaningful speech. Otherwise use an adjustable ten-second inactivity fallback. Ambiguous thanks, device completion and noise alone are insufficient. Keep recent context briefly in memory and finalize usage on close.

Measure wake phrase end to audible acknowledgment separately from readiness. Ordinary warm replies must reach median at most 1.0 s and estimated p95 at most 1.5 s from end of speech to sound. Report sample count, missing measurements, cold startup and device-action latency separately. Exercise opening words, interruptions, follow-ups, thinking pauses, sign-offs, background noise, pending tools, exposure, cancel/reconnect and music restoration. Stop tuning after two credible changes yield less than 100 ms repeatable gain or compromise reliability.

The agent handles routine installation and hardware testing under existing delegation. Local tests come first. The US$5 initial API-test cap includes US$0.31667 previously spent. Sending room microphone audio to OpenAI for testing requires separate explicit permission. Credentials stay local and audio and transcript retention are off by default. Use GPT-6.1 Sol medium for the main chat, with high recommended for firmware timing and echo alignment.
