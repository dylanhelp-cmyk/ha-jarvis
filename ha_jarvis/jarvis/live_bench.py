"""Injected question to acoustic reply benchmark; microphone audio stays local."""
from __future__ import annotations

import asyncio
from bisect import bisect_right
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime, timezone
import time

import av
import numpy as np

from .audio import RATE, detect_probe, summarize
from .live_audio import LiveAudioSource
from .live_session import LiveSession
from .streaming_aec import StreamingEcho
from .transports import AirPlayTransport


QUIET_SECONDS = 1.0
POST_INPUT_SECONDS = 15.0
PHYSICAL_TAIL_SECONDS = 2.0
RENDER_RATE = 44100
MAX_RENDER_BYTES = 75 * RENDER_RATE * 4


def voiced_bounds(samples):
    """Conservative digital energy bounds, never a human speech endpoint."""
    samples = np.asarray(samples, dtype=np.int16)
    if not len(samples):
        return None
    padded = np.pad(samples.astype(float), (0, (-len(samples)) % 160))
    energy = np.sqrt(np.mean(padded.reshape(-1, 160) ** 2, axis=1))
    active = np.flatnonzero(energy >= max(100.0, float(np.max(energy)) * .04))
    if not len(active):
        return None
    return int(active[0] * 160), min(len(samples), int((active[-1] + 1) * 160))


def render_reference(pcm):
    """Reconstruct the mono reference from the stereo packets sent to RAOP."""
    stereo = np.frombuffer(pcm, dtype=">i2").reshape(-1, 2)
    # LiveAudioSource duplicates its mono input into both channels.
    mono = stereo[:, 0].astype(np.int16).reshape(1, -1)
    frame = av.AudioFrame.from_ndarray(mono, format="s16", layout="mono")
    frame.sample_rate = RENDER_RATE
    converter = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    converted = converter.resample(frame) + converter.resample(None)
    return np.concatenate([block.to_ndarray().reshape(-1) for block in converted])


def match_response(captured, capture_start, reference, voiced_start_sample,
                   fixture_end_at, earliest_output_at, *, handoff_at=None,
                   acoustic_delay_range=(0.0, 2.5)):
    """Require distinct reply windows to agree before reporting onset."""
    unavailable = {"status": "unavailable", "latency_ms": None,
                   "fixture_end_to_audible_ms": None, "windows": []}
    width = int(.32 * RATE)
    bounds = voiced_bounds(reference)
    if bounds is None or bounds[1] - voiced_start_sample < width + int(.2 * RATE):
        return {**unavailable, "reason": "Reply is too short or weak for two distinct acoustic windows"}
    # Start windows at distinct positions over at most the first four seconds.
    last = min(bounds[1] - width, voiced_start_sample + 4 * RATE)
    starts = sorted(set(int(value) for value in np.linspace(voiced_start_sample, last, 4)))
    starts = [value for index, value in enumerate(starts)
              if index == 0 or value - starts[index - 1] >= int(.06 * RATE)]
    candidates, windows = [], []
    for offset in starts:
        start_at = earliest_output_at - .1
        end_at = fixture_end_at + POST_INPUT_SECONDS + 4
        if handoff_at is not None:
            handoff = handoff_at(offset)
            start_at = max(start_at, handoff + acoustic_delay_range[0])
            end_at = min(end_at, handoff + acoustic_delay_range[1] + width / RATE)
        search_start = max(0, int((start_at - capture_start) * RATE))
        search_end = min(len(captured), int((end_at - capture_start) * RATE))
        detection = detect_probe(captured[search_start:search_end], reference[offset:offset + width])
        window = {"reference_offset_ms": round(offset * 1000 / RATE, 2), **detection}
        if detection["status"] == "detected":
            onset = capture_start + search_start / RATE + detection["offset_seconds"]
            onset -= (offset - voiced_start_sample) / RATE
            candidates.append(onset)
            window["estimated_voiced_onset_at"] = onset
        windows.append(window)
    result = {**unavailable, "windows": windows}
    if len(candidates) < 2:
        return {**result, "reason": "Fewer than two reliable acoustic reply windows"}
    spread = (max(candidates) - min(candidates)) * 1000
    if spread > 40:
        return {**result, "reason": "Acoustic reply windows contradict each other", "window_spread_ms": round(spread, 2)}
    onset = float(np.median(candidates))
    latency = (onset - fixture_end_at) * 1000
    if latency < 0 or onset < earliest_output_at - .04:
        return {**result, "reason": "Reply onset predates the injected endpoint or returned audio"}
    return {**result, "status": "detected", "latency_ms": round(latency, 1),
            "fixture_end_to_audible_ms": round(latency, 1), "audible_onset_at": onset,
            "window_spread_ms": round(spread, 2), "resolution_ms": 4.0,
            "method": "multiple generated-reply spectral windows with reference-offset compensation"}


async def run_cloud_benchmark(bench, transport: str, fixture: bytes, *, microphone_input=False, followup=None) -> None:
    """Run one explicitly started fixture trial and retain scalar evidence only."""
    started = time.monotonic()
    result = {"id": bench.test["id"], "kind": "cloud_latency", "route": "airplay",
              "cloud_transport": transport, "started_at": datetime.now(timezone.utc).isoformat(),
              "status": "running", "trials": [], "summary": summarize([]),
              "metric_scope": "injected input end to acoustically detected generated reply",
              "warnings": ["Only the canned question is uploaded. Microphone capture stays local and is cleared after analysis.",
                           "Injected-input latency is not measured human end-of-speech latency, full-duplex acceptance or echo cancellation.",
                           "One trial does not establish a latency percentile. Microphone processing delay remains uncalibrated.",
                           "Reply completion uses one second without digital voice and a drained software queue; it is not a server audio-done event.",
                           "The test interrupts the speaker source; automatic music restoration is unavailable."]}
    bench.results.insert(0, result)
    del bench.results[30:]
    if microphone_input:
        result["metric_scope"] = "acoustic fixture captured by Voice PE to audible reply"
        result["warnings"][:2] = [
            "This trial uploads a bounded room microphone window while the speaker plays the synthetic question.",
            "The scripted first turn uses raw microphone input. It does not verify conversational follow-ups or echo cancellation.",
        ]
    result["input_mode"] = "acoustic_microphone" if microphone_input else "injected"
    result["followup_requested"] = followup is not None
    session = source = None
    source_ready = asyncio.Event()
    startup_tasks = []
    rendered = bytearray()
    render_offsets = []
    render_handoffs = []
    first_voiced_at = last_voiced_at = None
    last_voiced_render_at = None
    previous_callback_at = previous_chunk_seconds = None
    reference_gap = False
    fixture_end_at = None
    fixture_started = False
    max_lag_ms = 0.0
    capture_ended = playback_stopped = False
    alignment = None
    alignment_error = None
    microphone = asyncio.Queue(maxsize=16)
    capture_forwarding = False
    early_reply = False
    microphone_error = None
    fixture_requested_at = None
    reply_start_frame = 0
    max_microphone_queue_ms = 0.0
    uploaded_samples = 0
    uploaded_capture_start = uploaded_capture_end = None
    first_turn = None

    def on_render(pcm, handed_off_at):
        nonlocal alignment_error, last_voiced_render_at
        if len(rendered) + len(pcm) > MAX_RENDER_BYTES:
            raise BufferError("AirPlay render reference exceeded the benchmark RAM limit")
        render_offsets.append(len(rendered) // 4)
        render_handoffs.append(handed_off_at)
        rendered.extend(pcm)
        mono = np.frombuffer(pcm, dtype=">i2")[::2].astype(float)
        if len(mono) and float(np.mean(mono * mono)) >= 100 ** 2:
            last_voiced_render_at = handed_off_at
        if alignment is not None and alignment_error is None:
            try:
                alignment.render(pcm, handed_off_at)
            except (RuntimeError, ValueError) as exc:
                alignment_error = str(exc)

    def on_capture(frame, clock_offset_ms):
        nonlocal alignment_error, microphone_error
        if capture_forwarding and microphone_error is None:
            try:
                if clock_offset_ms is None:
                    microphone_error = "Microphone clock unavailable during upload"
                else:
                    microphone.put_nowait((time.monotonic(), (frame.capture_ms + clock_offset_ms) / 1000,
                                           frame.samples.tobytes()))
            except asyncio.QueueFull:
                microphone_error = "Microphone upload queue overflowed"
        if alignment is not None and alignment_error is None:
            try:
                alignment.capture(frame, clock_offset_ms)
            except (RuntimeError, ValueError) as exc:
                alignment_error = str(exc)

    def handoff_at(reference_offset):
        render_frame = reference_offset * RENDER_RATE // RATE
        index = bisect_right(render_offsets, render_frame) - 1
        return render_handoffs[index]

    def recent_calibration(*, allow_ack=False):
        timing = bench.config["airplay_latency_ms"]
        for prior in bench.results[1:]:
            echo = prior.get("echo", {})
            receiver = prior.get("transport", {}).get("receiver_after") or {}
            if (prior.get("route") != "airplay" or prior.get("status") not in ("measured", "unavailable")
                    or prior.get("calibration_stale")
                    or prior.get("requested_airplay_latency_ms") != timing
                    or receiver.get("volume_percent") is None
                    or (datetime.now(timezone.utc) - datetime.fromisoformat(prior["started_at"])).total_seconds() > 900):
                continue
            delay = echo.get("delay_assumed_ms")
            if delay is not None:
                return delay, receiver["volume_percent"]
            if allow_ack and prior.get("acknowledgment_match") == "both_word_windows_agree":
                delays = [trial["latency_ms"] for trial in prior["trials"]]
                return float(np.median(delays)), receiver["volume_percent"]
        return None

    def calibration_matches_receiver(calibration):
        volume = next((event["volume_percent"] for event in bench.transport.snapshot().get("events", ())
                       if event["event"] == "receiver_before_pcm"), None)
        return calibration is not None and volume == calibration[1]

    def acoustic_delay_range(calibration):
        if calibration_matches_receiver(calibration):
            delay = calibration[0] / 1000
            return max(0, delay - .3), delay + .3
        # Bench supports up to 2 s of requested RAOP timing plus downstream delay.
        return 0.0, 3.0

    async def source_factory(rate, channels, width):
        nonlocal source
        if source is not None:
            raise RuntimeError("Live speaker source was opened more than once")
        source = LiveAudioSource(rate, channels, width, on_render=on_render)
        source_ready.set()
        return source

    async def output_audio(pcm, rate):
        nonlocal first_voiced_at, last_voiced_at
        nonlocal previous_callback_at, previous_chunk_seconds, reference_gap
        nonlocal early_reply
        now = time.monotonic()
        if rate != RATE or len(pcm) % 2:
            raise ValueError("Cloud reply audio format changed")
        samples = np.frombuffer(pcm, dtype="<i2")
        bounds = voiced_bounds(samples)
        if not fixture_started:
            if microphone_input and capture_forwarding and bounds is not None:
                early_reply = True
            return
        if bounds is not None:
            if first_voiced_at is None:
                first_voiced_at = now
            if previous_callback_at is not None and last_voiced_at is not None and now - previous_callback_at > previous_chunk_seconds + .1:
                reference_gap = True
            last_voiced_at = now
        previous_callback_at, previous_chunk_seconds = now, len(samples) / RATE
        await source.write(pcm)

    def healthy():
        if microphone_error is not None:
            raise RuntimeError(microphone_error)
        if bench.device.errors:
            raise RuntimeError("Microphone capture failed during cloud test")
        bench.transport.check_failure()
        if session.metrics.error:
            raise RuntimeError(session.metrics.error)

    async def start_capture():
        await bench.device.begin(result["id"], 75000)
        result["device_session"] = dict(bench.device.capture_session)
        result["capture_ready_ms"] = round((time.monotonic() - started) * 1000, 1)

    async def start_speaker():
        await bench.transport.play(
            "http://127.0.0.1:8099/live-source-unused.wav",
            {"codec": "wav", "airplay_latency_ms": bench.config["airplay_latency_ms"],
             "source_factory": source_factory})
        await bench.transport.wait_for_stream(source_ready, 15)
        # The first pull follows RECORD/FLUSH and the receiver volume guard.
        await bench.transport.wait_for_stream(source.first_read, 8)
        result["speaker_pull_ready_ms"] = round((time.monotonic() - started) * 1000, 1)

    async def play_acoustic_question(question, calibration):
        nonlocal fixture_requested_at, capture_forwarding, max_microphone_queue_ms, uploaded_samples
        nonlocal uploaded_capture_start, uploaded_capture_end
        if not calibration_matches_receiver(calibration) or not 0 < calibration[0] <= 3000:
            raise RuntimeError("Acoustic microphone test requires recent calibration at this receiver level")
        fixture_requested_at = time.monotonic()
        stop_at = fixture_requested_at + len(question) / (2 * RATE) + calibration[0] / 1000 + .25

        async def play_fixture():
            for offset in range(0, len(question), 640):
                due = fixture_requested_at + max(0, offset / (2 * RATE) - .1)
                await asyncio.sleep(max(0, due - time.monotonic()))
                if time.monotonic() - due > .1:
                    raise RuntimeError("Acoustic question pacing fell behind")
                await source.write(question[offset:offset + 640])

        capture_forwarding = True
        playback = asyncio.create_task(play_fixture())
        sample_limit = round((stop_at - fixture_requested_at) * RATE)
        try:
            while time.monotonic() < stop_at:
                if playback.done():
                    playback.result()
                healthy()
                remaining = stop_at - time.monotonic()
                try:
                    queued_at, captured_at, pcm = await asyncio.wait_for(microphone.get(), min(.15, remaining))
                except TimeoutError:
                    if remaining <= .15:
                        break
                    raise RuntimeError("Voice PE microphone stopped delivering PCM") from None
                if time.monotonic() >= stop_at or uploaded_samples + len(pcm) // 2 > sample_limit:
                    break
                age = (time.monotonic() - queued_at) * 1000
                max_microphone_queue_ms = max(max_microphone_queue_ms, age)
                if age > 150:
                    raise RuntimeError("Microphone upload fell behind; trial stopped without catching up")
                await session.send_audio(pcm)
                if uploaded_capture_start is None:
                    uploaded_capture_start = captured_at
                uploaded_capture_end = captured_at + len(pcm) / (2 * RATE)
                uploaded_samples += len(pcm) // 2
            await playback
            if uploaded_samples < len(question) // 2:
                raise RuntimeError("Microphone upload ended before the full question could be captured")
        finally:
            capture_forwarding = False
            if not playback.done():
                playback.cancel()
                await asyncio.gather(playback, return_exceptions=True)

    async def exchange(question):
        nonlocal fixture_started, max_lag_ms
        paced_at = time.monotonic()
        sent_samples = 0

        async def send(block):
            nonlocal sent_samples, max_lag_ms
            due = paced_at + sent_samples / RATE
            await asyncio.sleep(max(0, due - time.monotonic()))
            lag_ms = max(0, (time.monotonic() - due) * 1000)
            max_lag_ms = max(max_lag_ms, lag_ms)
            if lag_ms > 100:
                raise RuntimeError("Fixture input pacing fell behind; trial stopped without acceleration")
            healthy()
            await session.send_audio(block)
            sent_samples += len(block) // 2

        for offset in range(0, len(question), 640):
            fixture_started = True
            await send(question[offset:offset + 640])
        endpoint = time.monotonic()
        deadline = endpoint + POST_INPUT_SECONDS
        bench.test["phase"] = "waiting for generated reply while streaming silence"
        completion = "observation_timeout"
        while time.monotonic() < deadline:
            await send(bytes(640))
            now = time.monotonic()
            if (last_voiced_at is not None and last_voiced_render_at is not None
                    and now - max(last_voiced_at, last_voiced_render_at) >= QUIET_SECONDS
                    and source.queued_frames <= RENDER_RATE // 10):
                # Continuous cloud silence can keep the FIFO nonempty after
                # the audible reply has passed through it.
                completion = "quiet_and_drained"
                break
        now = time.monotonic()
        result.setdefault("reply_boundaries", []).append({
            "status": completion,
            "cloud_voiced_age_ms": round((now - last_voiced_at) * 1000, 1) if last_voiced_at is not None else None,
            "render_voiced_age_ms": round((now - last_voiced_render_at) * 1000, 1) if last_voiced_render_at is not None else None,
            "queued_ms": round(source.queued_frames / RENDER_RATE * 1000, 1),
        })
        return endpoint, completion

    try:
        if transport not in ("webrtc", "websocket"):
            raise ValueError("Choose webrtc or websocket")
        if not isinstance(fixture, bytes) or not fixture or len(fixture) % 2 or len(fixture) > 10 * RATE * 2:
            raise ValueError("Question fixture must be 1–10 seconds of mono PCM16LE at 16 kHz")
        fixture_samples = np.frombuffer(fixture, dtype="<i2")
        bounds = voiced_bounds(fixture_samples)
        if bounds is None:
            raise ValueError("Question fixture has no usable digital speech energy")
        question = fixture_samples[bounds[0]:bounds[1]].tobytes()
        next_question = None
        if followup is not None:
            if microphone_input or not isinstance(followup, bytes) or not 0 < len(followup) <= 10 * RATE or len(followup) % 2:
                raise ValueError("Follow-up requires a bounded synthetic PCM fixture")
            next_samples = np.frombuffer(followup, dtype="<i2")
            next_bounds = voiced_bounds(next_samples)
            if next_bounds is None:
                raise ValueError("Follow-up fixture has no usable digital speech energy")
            next_question = next_samples[next_bounds[0]:next_bounds[1]].tobytes()
        result["fixture"] = {"input_samples": len(fixture_samples), "trimmed_samples": len(question) // 2,
                             "trimmed_head_samples": bounds[0], "trimmed_tail_samples": len(fixture_samples) - bounds[1]}
        calibration = recent_calibration()
        match_calibration = calibration or recent_calibration(allow_ack=True)
        if microphone_input:
            if calibration is None or not 0 < calibration[0] <= 3000:
                raise RuntimeError("Run a quiet-room AirPlay calibration before the acoustic microphone test")
            bench.device.pcm_consumer = on_capture
        elif calibration is not None and calibration[0] <= 3000:
            try:
                alignment = StreamingEcho(calibration[0])
                bench.device.pcm_consumer = on_capture
            except (ImportError, RuntimeError, ValueError) as exc:
                alignment_error = str(exc)
        bench.transport = AirPlayTransport.for_wiim(bench.config["wiim_host"], bench.config["verify_tls"],
                                                    discovery_cache=bench.airplay_discovery_cache)
        session = LiveSession(bench.cloud_settings, transport=transport, on_audio=output_audio, max_seconds=60)
        bench.test["phase"] = "preparing capture, cloud and AirPlay in parallel"
        async with asyncio.timeout(45):
            startup_tasks = [asyncio.create_task(operation()) for operation in
                             (start_capture, start_speaker, session.start)]
            await asyncio.gather(*startup_tasks)
            healthy()
            result["cold_ready_ms"] = round((time.monotonic() - started) * 1000, 1)
            if microphone_input:
                bench.test["phase"] = "playing synthetic question and streaming bounded room microphone"
                await play_acoustic_question(question, calibration)
                reply_start_frame = len(rendered) // 4
                fixture_started = True
            else:
                bench.test["phase"] = "sending paced canned question; microphone is not uploaded"
            fixture_end_at, completion = await exchange(b"" if microphone_input else question)
            if next_question is not None and completion == "quiet_and_drained":
                reply_start_frame = len(rendered) // 4
                first_turn = (fixture_end_at, first_voiced_at, reply_start_frame)
                first_voiced_at = last_voiced_at = None
                last_voiced_render_at = None
                previous_callback_at = previous_chunk_seconds = None
                bench.test["phase"] = "sending a follow-up in the existing voice session"
                fixture_end_at, completion = await exchange(next_question)
            result["reply_completion"] = completion
            if not microphone_input:
                result["fixture_last_voiced_submitted_at"] = fixture_end_at
            if completion == "observation_timeout":
                result["warnings"].append("The reply did not finish within the observation window. Any confirmed onset is provisional.")

        bench.test["phase"] = "closing paid cloud session and collecting final usage"
        metrics = await session.close()
        result["cloud"] = asdict(metrics)
        if not metrics.finalization_confirmed:
            raise RuntimeError("Cloud finalization unconfirmed; paid retries remain blocked")
        if metrics.error:
            raise RuntimeError(metrics.error)
        bench.test["phase"] = "recording physical speaker playback tail"
        await asyncio.sleep(PHYSICAL_TAIL_SECONDS)
        await asyncio.wait_for(bench.transport.stop(), 8)
        playback_stopped = True
        await bench.device.end()
        capture_ended = True
        captured, capture_start = bench.device.recording()
        result["capture"] = bench.device.capture_diagnostics()
        bench.test["phase"] = "matching generated reply against local acoustic capture"
        if first_turn is not None:
            first_end, first_pcm, first_render_end = first_turn
            first_reference = await asyncio.to_thread(render_reference, rendered[:first_render_end * 4])
            first_bounds = voiced_bounds(first_reference)
            first_detection = {"status": "unavailable", "latency_ms": None}
            if first_pcm is not None and first_bounds is not None:
                first_detection = await asyncio.to_thread(
                    match_response, captured, capture_start, first_reference, first_bounds[0],
                    first_end, first_pcm, handoff_at=handoff_at,
                    acoustic_delay_range=acoustic_delay_range(match_calibration))
            result["trials"].append({"trial": 1, "turn": "first", **first_detection,
                                     "first_voiced_pcm_ms": round((first_pcm - first_end) * 1000, 1)
                                     if first_pcm is not None else None})
        measurement_end = fixture_end_at
        if microphone_input:
            question_reference = np.frombuffer(question, dtype="<i2")
            input_match = await asyncio.to_thread(
                match_response, captured, capture_start, question_reference, 0,
                fixture_requested_at, fixture_requested_at, handoff_at=lambda offset: fixture_requested_at + offset / RATE,
                acoustic_delay_range=acoustic_delay_range(calibration))
            result["acoustic_input"] = input_match
            measurement_end = (input_match["audible_onset_at"] + len(question_reference) / RATE
                               if input_match["status"] == "detected" else None)
            result["estimated_acoustic_question_end_at"] = measurement_end
            if measurement_end is not None and not (
                uploaded_capture_start <= input_match["audible_onset_at"] <= measurement_end <= uploaded_capture_end
            ):
                result["warnings"].append("The acoustic question exceeded the transmitted microphone sample interval.")
                measurement_end = None
        if microphone_input and (measurement_end is None or early_reply):
            detection = {"status": "unavailable", "latency_ms": None,
                         "reason": "Early model output overlapped microphone forwarding" if early_reply
                         else "Acoustic question endpoint was not independently verified"}
        elif first_voiced_at is None or not rendered:
            detection = {"status": "unavailable", "latency_ms": None, "reason": "No generated voiced PCM detected"}
        else:
            reference = await asyncio.to_thread(render_reference, rendered)
            if reply_start_frame:
                reference[:reply_start_frame * RATE // RENDER_RATE] = 0
            output_bounds = voiced_bounds(reference)
            if output_bounds is None:
                detection = {"status": "unavailable", "latency_ms": None, "reason": "No voiced PCM reached the AirPlay sender"}
            else:
                delay_range = acoustic_delay_range(match_calibration)
                detection = await asyncio.to_thread(match_response, captured, capture_start,
                    reference, output_bounds[0], measurement_end, first_voiced_at,
                    handoff_at=handoff_at, acoustic_delay_range=delay_range)
                detection["reference_source"] = "airplay_pcm_handoff"
                detection["acoustic_search_delay_range_ms"] = [round(value * 1000) for value in delay_range]
        if alignment is not None:
            result["live_echo"] = {
                "status": "processed" if alignment_error is None and calibration_matches_receiver(calibration) else "unavailable",
                "reason": alignment_error or (None if calibration_matches_receiver(calibration) else "Receiver volume changed since calibration"),
                "delay_assumed_ms": calibration[0],
                "residual_delay_hint_ms": alignment.residual_delay_ms,
                "processed_samples": alignment.processor.frames_processed * 160,
                "capture_drift_ppm": round(alignment.capture_drift_ppm, 1),
                "max_processing_ms": round(alignment.processor.max_processing_ms, 3),
                "scope": "Real-time reference alignment only; microphone audio stays local",
            }
        trial = {"trial": 2 if first_turn is not None else 1,
                 "turn": "warm_followup" if first_turn is not None else "first",
                 **detection, "metric": "microphone_end_to_audible" if microphone_input else "fixture_end_to_audible",
                 "first_voiced_pcm_ms": round((first_voiced_at - measurement_end) * 1000, 1) if first_voiced_at is not None and measurement_end is not None else None,
                 "cold_ready_ms": result["cold_ready_ms"],
                 "clock_uncertainty_ms": round(bench.device.clock_rtt_ms / 2 + 4, 2),
                 "digital_energy_resolution_ms": 10, "input_chunk_ms": 20}
        result["trials"].append(trial)
        if result["reply_completion"] == "observation_timeout":
            trial["provisional"] = True
            result["status"] = "incomplete"
        else:
            result["status"] = "measured" if detection["status"] == "detected" else "unavailable"
    except asyncio.CancelledError:
        interruption = bench.test.get("interruption", {"reason": "unexpected_task_cancellation"})
        result["interruption"] = interruption
        result["status"] = "cancelled" if interruption["reason"] == "user_stop" else "failed"
        result["error"] = "Cloud test interrupted: " + interruption["reason"]
        raise
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc) or type(exc).__name__
        result["exception_type"] = type(exc).__name__
    finally:
        if alignment is not None or microphone_input:
            bench.device.pcm_consumer = None
        if microphone_input:
            result["uploaded_microphone_samples"] = uploaded_samples
            result["uploaded_capture_start_at"] = uploaded_capture_start
            result["uploaded_capture_end_at"] = uploaded_capture_end
            result["max_microphone_queue_ms"] = round(max_microphone_queue_ms, 2)
        bench.test["finishing"] = True
        result["ended_phase"] = bench.test.get("phase")
        for task in startup_tasks:
            if not task.done():
                task.cancel()
        if startup_tasks:
            await asyncio.gather(*startup_tasks, return_exceptions=True)
        if session is not None:
            try:
                result["cloud"] = asdict(await session.close())
            except Exception:
                result["status"] = "failed"
                result["warnings"].append("Cloud close failed; final usage is unconfirmed")
            if not result.get("cloud", {}).get("finalization_confirmed", False):
                result["status"] = "failed"
        if bench.transport is not None:
            result["transport"] = bench.transport.snapshot()
            if not playback_stopped:
                try:
                    await asyncio.wait_for(bench.transport.stop(), 8)
                except Exception:
                    result["status"] = "failed"
                    result["warnings"].append("Speaker stop could not be confirmed")
            with suppress(Exception):
                await bench.transport.close()
            bench.transport = None
        if source is not None:
            result["output_queue"] = dict(source.telemetry)
            with suppress(Exception):
                await source.close()
        if not capture_ended:
            with suppress(Exception):
                await bench.device.end()
        result["capture_at_end"] = bench.device.capture_diagnostics()
        result["device_session"] = dict(bench.device.capture_session)
        result["max_input_schedule_lag_ms"] = round(max_lag_ms, 2)
        result["reference_render_frames"] = len(rendered) // 4
        result["first_render_handoff_at"] = render_handoffs[0] if render_handoffs else None
        result["cloud_callback_timing_gap"] = reference_gap
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["summary"] = summarize(result["trials"])
        if hasattr(bench, "observe_firmware"):
            bench.observe_firmware()
        bench.device.frames.clear()
        bench.stream = None
        bench.test = {"running": False, "phase": "idle"}
