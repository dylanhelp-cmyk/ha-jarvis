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

## First wired deployment

App and firmware 0.2.0 paired on the actual Voice PE, with the 3.5 mm jack detected. The first local playback stopped at 101.9 ms sender lag after submitting 12,000 frames and receiving acceptance for 10,080 frames. Audible timing is unavailable. Verify the firmware packet-drain fix on hardware before tuning buffers or restoring cloud playback.

App and firmware 0.2.1 (f25b66e) installed and verified on the Voice PE. The revised firmware drains up to four packets per ESPHome loop and requests faster scheduling during playback. The first valid local run measured 146.9 ms from play request to audible onset, with speaker-buffer acceptance to acoustic onset of 86, 122 and 122 ms. All 336,000 frames drained, with maximum sender lag 12.3 ms. One stop run measured 132.7 ms to acoustic silence at 32 ms resolution. These measurements cover wired playback, not wake-to-acknowledgment or cloud reply latency. Next repeat cold starts, run stability and echo calibration, and verify the generated acknowledgment path independently.

Generate the exact “Yes sir” clip in the configured GPT-Live voice once, verify the spoken wording, and key the cache by model, voice and phrase. Persist only the generated acknowledgment, not room capture. Preload the clip on Voice PE, play it once at wake while establishing the cloud session, and replace the cache when the voice changes. Measure local wake-to-sound, cloud readiness and end-of-speech-to-reply independently. Treat the existing 1 s warm median as a ceiling to beat rather than a stopping point when wired measurements show credible room to improve.

Additional wired runs measured 164.6 and 201.5 ms from playback request to audible onset. The stability run detected zero gaps over a 4535 ms pilot (45.2 dB SNR), drained all 528,000 frames and reached 33.7 ms maximum sender lag. The 70 ms DMA output clock span deviation remains unexplained. Echo suppression was unavailable because the resampling filter crossed the exact one-second silence boundary. App 0.2.2 adds a 20 ms guard for the repeat test.

App 0.2.2 (269521c) installed with firmware 0.2.1. The repeat wired echo run measured 182.8 ms request to audible onset, 118 ms median speaker acceptance to sound over three probes, and zero gaps across the 4530 ms pilot. Offline AEC3 reduced the two pilot bands by 43.35 dB to the noise floor (118 ms assumed delay, 1.465 ms peak processing per 10 ms frame). This establishes far-end quiet-room suppression only. Live reference alignment, drift and near-end speech preservation remain open.

One GPT-Live WebSocket greeting generation sent digital silence and produced a 460 ms marin candidate on the App host. The generated transcript matched Yes sir. The final usage ledger is US$0.31833 of US$5 with no pending reservation. No room audio was sent. Next verify the spoken clip and preload it on the Voice PE, then measure wake-to-audible timing alongside concurrent capture.

App 0.2.3 (d777af9) and firmware 0.2.2 (2c775c5) were deployed and paired. The user listened to the 460 ms candidate and confirmed it says only Yes sir. The App confirmed transfer to device RAM. On the first armed physical Hey Jarvis trial, the user heard the clip once, and all 22,080 output frames drained. Four acoustic matching windows measured 102.7 ms from firmware wake detection to audible onset (33.8 ms timing uncertainty). The end of the spoken wake phrase and cloud readiness were not measured in this trial. No room audio was sent to OpenAI.
