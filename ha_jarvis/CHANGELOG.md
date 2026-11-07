# 0.2.29

- Add a local Speech recovery check. It measures two generated phrases through the jack with an intentional delivery pause. Use it with the speakers at a comfortable level and idle.

# 0.2.28

- Keep streamed GPT-Live reply samples in order through brief delivery stalls. Resume at normal speed and report any sender schedule shifts in diagnostics. Longer stalls still stop the reply.

# 0.2.27

- Run the isolated Realtime comparison silently and report the time to returned voice audio. This experiment uses generated input and opens neither the room microphone nor the speaker.

# 0.2.26

- Drain queued reply audio after a failed synthetic comparison. Preserve the failure reason in the result.
- Let the isolated Realtime benchmark buffer bursts of returned audio and finish at the provider's response completion event.
- Allow brief quiet gaps in GPT-Live output before ending playback.

# 0.2.25

- Add a separate synthetic comparison for GPT-Realtime-2.1 Mini over WebSocket with 220 ms of silence to detect the end of speech.
- Keep the comparison limited to two generated prompts and two replies. Settle its token usage against the existing test budget.

# 0.2.24

- Narrow acoustic reply matching with the Voice PE's confirmed playback clock and current echo alignment. Require independent microphone windows to agree before reporting latency.
- App-only update. Run Echo reference alignment to make calibrated reply timing available.

# 0.2.23

- Retry a timed-out wake clip transfer once on the same paired connection. Ignore late confirmations from the failed transfer.
- Measure synthetic reply onset from the streaming speech boundary. Report the provider and speaker intervals separately, and use the same reply level and reserve on both transports.
- App-only update. Compare the synthetic WebSocket and WebRTC benchmarks using the same reply profile.

# 0.2.22

- In the preconnected voice preview, begin sending cleaned microphone audio as the Voice PE confirms the corresponding wake clip samples. Hold reply playback until the complete wake clip is confirmed.
- Show the timing of the local echo comparison and label the provider and playback intervals separately.
- App-only update. Re-run Echo reference alignment and try one normal-volume preconnected voice preview with warm follow-up at +36 dB.

# 0.2.21

- Compare echo cancellation using the first confirmed portion of the cached wake clip with the full playback reference in the local Wake acknowledgment test. Keep this comparison on the App.
- App-only update. Run Wake acknowledgment with cloud startup disabled. After the test is armed, say “Hey Jarvis” and immediately speak a short question.

# 0.2.20

- Lower live preview reply output by 6 dB before sending audio to the jack.
- Increase the first preconnected reply's source reserve from 120 to 200 ms. Warm replies and wake-start previews keep 120 ms.
- App-only update. Run Echo reference alignment, then try one normal-volume preconnected preview at +36 dB. Listen for harshness and interruptions.

# 0.2.19

- Match audible onset from the same speech boundary detected during streaming. Use Voice PE sample acceptance times to constrain the acoustic search.
- Cap local speech timing gain at +24 dB when +36 dB is selected for microphone transmission. Report both gains in diagnostics.
- App-only update. Re-run Echo reference alignment, then compare normal-volume two-turn previews with and without preconnection at +36 dB.

# 0.2.18

- Add a +36 dB microphone option with peak protection for quiet capture.
- Preserve up to 40 ms of quiet reply audio before the first strong sound and record digital output peak and full-scale sample count.
- Start the Voice PE speaker while the App fills the reply reserve. Record the reserve fill and remaining handshake wait separately. The playback reserves and sample pacing are unchanged.
- Give warm follow-ups 10 seconds of inactivity. Detected speech resets that timer within the paid preview's 18-second input limit.
- Add a read-only Assist connection check. It reports the discovered tool count and uses the access token once.
- App-only update. Run Echo reference alignment, then try Voice preview with +36 dB, a warm follow-up, normal speaking volume, and preconnection off.

# 0.2.17

- Keep echo processing through the confirmed wake clip and a 180 ms acoustic tail guard. Preserve microphone levels when the jack is quiet.
- Add an explicit paid preconnection comparison to Voice preview. GPT-Live opens before wake and closes if no wake arrives within the 15-second wait. The existing wake-triggered connection remains the default.

# 0.2.16

- Default the opt-in voice preview to +24 dB App input gain and show active gain while testing. Pair firmware 0.2.4 to add fixed 4x capture gain before the 16-bit conversion.

# 0.2.15

- Allow the known-clip alignment to identify a later acoustic outlier when three strong early windows agree within 12 ms. Report the outlier. Ordinary reply timing still requires its windows to agree. App-only update.

# 0.2.14

- Add an optional warm follow-up to the consent-gated Voice preview. After the first answer, wait for the listening status before speaking again. The test keeps the same connection and measures a fresh microphone queue. App-only update.

# 0.2.13

- Add selectable preview microphone gain after echo cancellation, preserve input diagnostics even when no reply arrives, and estimate acoustic onset independently of later reply gaps. App-only update.

# 0.2.12

- Allow a 120 ms host-side source reserve for live preview replies and retain playback diagnostics if they stall.
- Estimate the end of speech locally and measure microphone queue age independently from the provider and wired playback. The estimate is diagnostic and does not change turn-taking. App-only update.

# 0.2.11

- Add a one-turn voice preview after a successful Echo reference alignment. It requires per-run consent to send cleaned microphone audio to OpenAI GPT-Live. Say Hey Jarvis, then ask a short question. Firmware 0.2.3 is sufficient.

# 0.2.10

- Run the two-clip echo alignment test using the same 50 ms playback reserve as streamed replies. App-only update.

# 0.2.9

- Keep the echo reference aligned across confirmed playback bursts and known gaps. Run Echo reference alignment to check two copies of the approved clip with firmware 0.2.3. App-only update.

# 0.2.8

- With firmware 0.2.3, arm the wake bench without capturing audio until the Voice PE detects Hey Jarvis. Report the first capture timestamp, bounded opening buffer and offline wake-clip echo alignment. Room audio stays local.

# 0.2.7

- Prime streamed synthetic replies with up to 50 ms of PCM to absorb uneven cloud packet delivery. Compare one WebSocket reply session with the previous timing windows. App-only update.

# 0.2.6

- Compare direct-answer prompting with the baseline using the same synthetic audio. The diagnostics record whether a returned transcript mentions the expected answer. App-only update.

# 0.2.5

- Add the synthetic reply timing bench. Select WebSocket or WebRTC and run one session with two digital prompts to measure first and warm audible replies through the jack. App-only update.

# 0.2.4

- Optionally measure GPT-Live startup during the local wake acknowledgment test. Run one local or budgeted startup trial using the existing 0.2.2 firmware. Room microphone audio stays local.

# 0.2.3

- Preview the generated wake clip and approve the exact phrase before it can play on the device.
- Preload the approved clip onto a paired Voice PE with cache-capable firmware. Run the local Wake acknowledgment test and say Hey Jarvis when armed to measure firmware wake detection to audible playback.

# 0.2.2

- Add a 20 ms guard before the required silent tail in the local echo reference. Repeat the Echo test with firmware 0.2.1.
- Add a paid greeting generation endpoint using digital silence and local candidate storage.

# 0.2.1

- Report missing echo prerequisites explicitly and add a paired playback regression test.
- Pair with firmware 0.2.1 for the first sustained wired playback trial.

# 0.2.0

- Replace the wireless playback bench with authenticated 48 kHz stereo playback through the Voice PE 3.5 mm jack. Requires playback-capable Voice PE firmware.
- Preserve pairing and API-test spending while retiring the old playback settings. Keep paid playback gated pending hardware verification of the wired route.

# 0.1.36

- Add a 200 ms acknowledgment lead-in option for comparison with the verified 300 ms setting. Cold timing continues to include the selected silence.

# 0.1.35

- Give the synthetic talker's AirPlay stream a 300 ms silent lead-in before the microphone capture window. The lead precedes the question and its measured endpoint.
- Use verified acknowledgment timing for the raw scripted microphone test as well as local preflight. Room microphone upload still requires separate authorization.
- App-only update. Repeat the local preflight and inspect the opening acoustic window and endpoint coverage.

# 0.1.34

- Match the acoustic question against the exact rendered AirPlay PCM. Include any inserted silence and anchor the search to sender handoff.
- App-only update. Repeat the local microphone preflight with the same synthetic question.

# 0.1.33

- Add a local microphone preflight. Play the synthetic question and check that its verified acoustic endpoints fit in the bounded Voice PE sample window without opening a cloud session.
- App-only update. Verify a local acknowledgment at the chosen WiiM settings, load the synthetic question and run Local microphone preflight.

# 0.1.32

- Use a recent verified acknowledgment at the same receiver level and timing to bound reply matching when a room echo calibration is unavailable.
- App-only update. Verify both acknowledgment words, then compare the synthetic follow-up under the same playback settings.

# 0.1.31

- Allow a 150 ms AirPlay timing request for a controlled comparison. Compare wake acknowledgment and stability with 250 ms before selecting a setting.

# 0.1.30

- Use one second without voiced cloud or AirPlay PCM to pace the scripted follow-up. Allow up to 100 ms queued audio so a continuing stream of silence does not prevent a turn boundary. Record the two voice ages and queue size.
- App-only update. Compare both transports with the same synthetic fixtures and inspect the reply-boundary diagnostics.

# 0.1.29

- Allow an agent-supplied synthetic follow-up in the same paid voice session and AirPlay stream. Report the first and warm follow-up turns separately. Room audio stays local.
- App-only update. Compare the same two synthetic questions across transports.

# 0.1.28

- Allow the local armed test to play a synthetic wake stimulus supplied by the test runner, close that session and measure the acknowledgment on a fresh session.
- App-only update. The scripted test reports wake stimulus teardown separately from acknowledgment playback. Manual arming remains available.

# 0.1.27

- Add an optional one-shot, 30-second wake arm to the local acknowledgment test. Synchronize clocks while arming, start capture on a new wake and report wake receipt to acoustic onset.
- App-only update. Load the acknowledgment clip, select 500 ms and arm the test. It returns to idle after one wake, timeout or cancellation.

# 0.1.26

- Use the second word's measured arrival to restrict the first-word search. Withhold cold timing when the two matches disagree.
- App-only update. Repeat the local acknowledgment test and compare both word delays.

# 0.1.25

- Add a local acknowledgment playback check. Load 1.5 seconds of mono 16 kHz PCM with one synthetic word in each 750 ms slot. A complete match requires consistent acoustic timing for both slots.
- App-only update. Compare the same test clip at 100 and 500 ms lead-in. No OpenAI call.

# 0.1.24

- Compare 100, 300 and 500 ms of silence before prepared PCM probes. Cold audible timing includes the chosen lead-in.
- App-only update. Select prepared PCM startup and compare lead-ins at the same AirPlay timing and volume.

# 0.1.23

- Add a local startup comparison that hands prepared PCM to AirPlay without HTTP decoding. Match the same probe signals using their sender handoff times.
- Count authenticated, unmuted Hey Jarvis events locally. Track captured samples without rescanning the recording on every packet.
- App-only update. Compare prepared PCM with the existing AirPlay startup check at the same timing and volume.

# 0.1.22

- Add a local AirPlay startup check with the two known probes reversed to isolate loss of the first probe.
- Verify that a scripted acoustic question falls entirely inside the transmitted sample interval before reporting microphone-to-reply latency.
- App-only update. Run AirPlay startup check twice with the same volume and compare first and second probe results.

# 0.1.21

- Warm AirPlay discovery metadata while idle. Open a fresh playback connection on demand.
- Add a separately confirmed acoustic microphone benchmark. Play a synthetic question, forward only the bounded Voice PE microphone window, and verify the question endpoint acoustically before reporting latency.
- App-only update. Run quiet-room AirPlay calibration, then one paid Voice PE microphone trial with the room quiet and speakers idle.

# 0.1.20

- Process a local copy of capture through streaming AEC3 during the injected voice benchmark after fresh room calibration. Use bounded AirPlay history and adjust reference sampling for estimated capture-clock drift. Report processing and timing separately from acoustic suppression.
- App-only update. Run the quiet-room echo test, then the injected voice benchmark and export the local streaming AEC metrics. Only the synthetic question is sent to OpenAI.

# 0.1.19

- Bound acoustic searches from each PCM handoff. Narrow the range after a recent quiet-room echo measurement at the same AirPlay timing and receiver level. Retain the agreement requirement.
- App-only update. Run a quiet-room echo test, then repeat the injected WebSocket question and inspect the reported acoustic search range and outcome.

# 0.1.18

- Match audible replies against the PCM handed to AirPlay, including silence inserted by the output queue. Keep the reference in bounded RAM for the trial.
- App-only update. Repeat the same WebSocket fixture at the same WiiM level and check whether the acoustic windows agree.

# 0.1.17

- Start microphone capture, cloud connection and AirPlay setup concurrently. Gate fixture input on microphone and cloud readiness and the first AirPlay PCM pull after volume verification.
- Report capture-ready and speaker-pull times separately. Drop pre-fixture cloud audio so unsolicited startup output cannot be counted as a reply.
- App-only update. Run the same injected question twice on each transport and compare the startup breakdown separately from audible onset.

# 0.1.16

- Use a frequency-band noise floor for quiet-room AEC3 pilot measurements.
- Preserve provisional audible onset evidence if a reply outlasts the observation window. Keep the trial marked incomplete.
- App-only update. Run one quiet-room echo test with AirPlay selected and check the measured pilot SNR and suppression or the reason the result is unavailable.

# 0.1.15

- If AirPlay startup raises the receiver level, reduce it to the pre-switch ceiling using WiiM's native control and verify readback before allowing PCM. Apply the same guard to local and cloud tests. Repeat the silent switch check before audible testing.

# 0.1.14

- Leave volume under receiver control. Check WiiM readback after AirPlay source selection and before sending PCM. Abort if the level rises, mute is active, or readback fails.
- Add a silent AirPlay source-switch check, acoustic probe handoff timestamps, and an offline quiet-room AEC3 pilot test. The echo test requires stable playback and consistent acoustic delays.
- App-only update. With AirPlay selected and the speaker idle, run Silent AirPlay volume check once and compare receiver levels before playback, before PCM, and after stop.

# 0.1.13

- Cache receiver discovery for five minutes while creating fresh authenticated sessions. Preserve provisional acoustic observations from failed captures without marking them successful.
- Add a continuous, bounded AirPlay source and explicitly started WebRTC/WebSocket GPT-Live synthetic-input benchmarks. Microphone audio stays local in this mode. This is not a conversational assistant or acoustic AEC acceptance.
- Enforce durable US$0.05 reservations within the US$5 initial voice-test budget. Unconfirmed finalization blocks further paid attempts. Credentials and usage stay local; no cloud session starts on App startup.
- Bundle verified native AEC3 processing for subsequent acoustic integration. Synthetic checks do not establish real-room echo suppression or drift handling.
- App-only update; firmware stays 0.1.6. The agent operates comparisons directly under the current testing authorization.

# 0.1.12

- Add a bounded per-session AirPlay timing request (250–2000 ms; unchanged 1500 ms default), restored after playback. Preserve upstream authentication, encryption, volume and cancellation; actual delay still needs acoustic measurement.
- Normalize probe matching per frequency band to tolerate speaker/room EQ. Probe identities, clipping rejection, acceptance threshold and ambiguity margin remain unchanged.
- Add local write-only OpenAI API-key entry and show the agreed US$5 initial API-test cap. Saving credentials opens no connection; cloud sessions remain disabled in this revision.
- App-only update; firmware stays 0.1.6. The agent now operates controlled acoustic comparisons directly under the user's authorization.

# 0.1.11

- Pair with firmware 0.1.6's PCM send-timeout experiment (30 → 100 ms). Audio queue capacity and failure/reset behavior remain unchanged; the SDK timeout is not a hard whole-call duration limit.
- Display/export bounded per-capture successful/failed send durations, maximum firmware queue residence and failed-send byte counts. Match only exact boot/connection/capture identities, including clean completion and reconnect. Missing or invalid optional telemetry remains unavailable.
- Update both App and firmware, then run one AirPlay/WAV Stream stability test at comfortable audible volume. Keep the normal observation period; export after completion or reconnect. This tests connection behavior and possible added queue delay without claiming the earlier fault is fixed.

# 0.1.10

- Play the continuous stability pilot after all three coded probes so it cannot weaken their detection. Calibrate continuity against quiet-room noise and a clear pilot reference; weak or interrupted reference audio reports unavailable, without misleading gap counts.
- Require a measured continuity result before marking a stability test measured. Show pilot signal-to-noise and reference coverage in the interface. Volume remains under your control.
- Pair with firmware 0.1.5: disable Wi-Fi power saving and retain the SDK's intermediate TCP error details across reconnect. The earlier disconnect's cause remains unconfirmed; PCM deadlines and buffering are unchanged.
- Update both App and firmware, then run one AirPlay/WAV Stream stability test at comfortable audible volume. Export after completion or reconnection; no further repeats needed for this revision.

# 0.1.9

- Export firmware 0.1.4's bounded RAM fault history after reconnect, including boot/connection/capture identities. Match a reported first fault to a failed test only when all identities agree; distinguish a changed boot ID without inventing a restart cause.
- Add a user-started **Microphone connection** test: 60 seconds of silent capture, no speaker control or cloud calls, aggregate diagnostics only.
- Update both App and firmware for this diagnostic revision. This does not change network timing or claim to fix the intermittent disconnect.
- Run one Microphone connection test. If it disconnects, wait until Voice PE reconnects before exporting diagnostics. If it passes, export once; no repeated playback tests or log watching needed.

# 0.1.8

- Replace AirPlay's generated-audio handoff with a bounded streaming source that skips tag scanning. Decode/resample in worker threads so network and microphone handling stay responsive.
- Preserve pyatv's session authentication, encryption, volume handling and teardown. Keep HTTP marker timestamps tied to actual paced production; do not cache a completed recording or infer audible latency from software readiness.
- Export source setup, first PCM handoff and decoder read-wait timings separately from acoustic measurements. Retain WiiM volume preservation and firmware 0.1.3.
- App-only update. Run one AirPlay/WAV playback latency test at comfortable volume, allow completion, and export diagnostics. Speaker latency and stable playback still require your measurement.

# 0.1.7

- Fix reproduced silent AirPlay decoding by serving this finite test's actual WAV lengths and Content-Length. HTTP/WAV and SlimProto framing are unchanged.
- Read the current WiiM volume/mute before AirPlay, set the sender context to that same level instead of pyatv's 33% default, and export before/after output state. Muted/zero/unknown volume fails clearly without guessing or unmuting.
- The current pyatv URL source preloads during metadata parsing; the baseline includes that software delay. Allow a 30-second acoustic search and label this limitation; do not treat its result as receiver-only latency.
- App-only update; keep firmware 0.1.3. Set your comfortable WiiM level yourself, run one AirPlay/WAV test, allow up to 60 seconds, and export diagnostics. Confirm whether tones are audible and volume remains at your chosen level.

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
