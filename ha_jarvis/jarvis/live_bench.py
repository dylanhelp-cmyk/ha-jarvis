"""Injected question to acoustic reply benchmark; microphone audio stays local."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime, timezone
import time

import numpy as np

from .audio import RATE, detect_probe, summarize
from .live_audio import LiveAudioSource
from .live_session import LiveSession
from .transports import AirPlayTransport, NativeHTTPTransport


QUIET_SECONDS = 1.0
POST_INPUT_SECONDS = 15.0
PHYSICAL_TAIL_SECONDS = 2.0
MAX_REFERENCE_SAMPLES = 20 * RATE


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


def match_response(captured, capture_start, reference, voiced_start_sample,
                   fixture_end_at, earliest_output_at):
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
    search_start = max(0, int((earliest_output_at - capture_start - .1) * RATE))
    search_end = min(len(captured), int((fixture_end_at - capture_start + POST_INPUT_SECONDS + 4) * RATE))
    candidates, windows = [], []
    for offset in starts:
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


async def run_cloud_benchmark(bench, transport: str, fixture: bytes) -> None:
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
    session = source = None
    source_ready = asyncio.Event()
    startup_tasks = []
    reference = bytearray()
    reference_voiced_start = None
    first_voiced_at = last_voiced_at = None
    previous_callback_at = previous_chunk_seconds = None
    reference_gap = False
    fixture_end_at = None
    max_lag_ms = 0.0
    capture_ended = playback_stopped = False

    async def source_factory(rate, channels, width):
        nonlocal source
        if source is not None:
            raise RuntimeError("Live speaker source was opened more than once")
        source = LiveAudioSource(rate, channels, width)
        source_ready.set()
        return source

    async def output_audio(pcm, rate):
        nonlocal reference_voiced_start, first_voiced_at, last_voiced_at
        nonlocal previous_callback_at, previous_chunk_seconds, reference_gap
        now = time.monotonic()
        if rate != RATE or len(pcm) % 2:
            raise ValueError("Cloud reply audio format changed")
        samples = np.frombuffer(pcm, dtype="<i2")
        bounds = voiced_bounds(samples)
        if bounds is not None:
            if first_voiced_at is None:
                first_voiced_at = now
                reference_voiced_start = bounds[0]
            if previous_callback_at is not None and reference and now - previous_callback_at > previous_chunk_seconds + .1:
                reference_gap = True
            last_voiced_at = now
        if reference_voiced_start is not None:
            if len(reference) + len(pcm) > MAX_REFERENCE_SAMPLES * 2:
                raise BufferError("Reply reference exceeded the 20 second RAM limit")
            reference.extend(pcm)
        previous_callback_at, previous_chunk_seconds = now, len(samples) / RATE
        await asyncio.wait_for(source_ready.wait(), 15)
        await source.write(pcm)

    def healthy():
        if bench.device.errors:
            raise RuntimeError("Microphone capture failed during cloud test")
        bench.transport.check_failure()
        if session.metrics.error:
            raise RuntimeError(session.metrics.error)

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
        result["fixture"] = {"input_samples": len(fixture_samples), "trimmed_samples": len(question) // 2,
                             "trimmed_head_samples": bounds[0], "trimmed_tail_samples": len(fixture_samples) - bounds[1]}
        bench.test["phase"] = "capturing local microphone for cloud playback measurement"
        await bench.device.begin(result["id"], 75000)
        result["device_session"] = dict(bench.device.capture_session)

        async def read_output_state():
            reader = NativeHTTPTransport("https://" + bench.config["wiim_host"],
                                         verify_ssl=bench.config["verify_tls"])
            try:
                return await reader.output_state()
            finally:
                await reader.close()

        bench.transport = AirPlayTransport(bench.config["wiim_host"], state_reader=read_output_state,
                                           discovery_cache=bench.airplay_discovery_cache)
        session = LiveSession(bench.cloud_settings, transport=transport, on_audio=output_audio, max_seconds=60)
        bench.test["phase"] = "connecting cloud and continuous AirPlay stream"
        async with asyncio.timeout(45):
            startup_tasks = [asyncio.create_task(session.start()), asyncio.create_task(bench.transport.play(
                "http://127.0.0.1:8099/live-source-unused.wav",
                {"codec": "wav", "airplay_latency_ms": bench.config["airplay_latency_ms"],
                 "source_factory": source_factory}))]
            await asyncio.gather(*startup_tasks)
            await asyncio.wait_for(source_ready.wait(), 15)
            result["cold_ready_ms"] = round((time.monotonic() - started) * 1000, 1)
            bench.test["phase"] = "sending paced canned question; microphone is not uploaded"
            paced_at = time.monotonic()
            sent_samples = 0
            for offset in range(0, len(question), 640):
                due = paced_at + sent_samples / RATE
                await asyncio.sleep(max(0, due - time.monotonic()))
                lag_ms = max(0, (time.monotonic() - due) * 1000)
                max_lag_ms = max(max_lag_ms, lag_ms)
                if lag_ms > 100:
                    raise RuntimeError("Canned question pacing fell behind; trial stopped without acceleration")
                healthy()
                block = question[offset:offset + 640]
                await session.send_audio(block)
                sent_samples += len(block) // 2
            # Endpoint is the submit time of the chunk containing the final
            # voiced sample, not the fixture file length or cloud readiness.
            fixture_end_at = time.monotonic()
            result["fixture_last_voiced_submitted_at"] = fixture_end_at
            bench.test["phase"] = "waiting for generated reply while streaming silence"
            deadline = fixture_end_at + POST_INPUT_SECONDS
            while time.monotonic() < deadline:
                due = paced_at + sent_samples / RATE
                await asyncio.sleep(max(0, due - time.monotonic()))
                lag_ms = max(0, (time.monotonic() - due) * 1000)
                max_lag_ms = max(max_lag_ms, lag_ms)
                if lag_ms > 100:
                    raise RuntimeError("Silence stream pacing fell behind; trial stopped without acceleration")
                healthy()
                await session.send_audio(bytes(640))
                sent_samples += 320
                if last_voiced_at is not None and time.monotonic() - last_voiced_at >= QUIET_SECONDS and source.queued_frames == 0:
                    break
            else:
                raise TimeoutError("No completed generated reply within the 15 second observation limit")

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
        if first_voiced_at is None or reference_voiced_start is None:
            detection = {"status": "unavailable", "latency_ms": None, "reason": "No generated voiced PCM detected"}
        elif reference_gap:
            detection = {"status": "unavailable", "latency_ms": None, "reason": "Returned audio timing gaps make concatenated reference alignment uncertain"}
        else:
            detection = await asyncio.to_thread(match_response, captured, capture_start,
                np.frombuffer(reference, dtype="<i2"), reference_voiced_start, fixture_end_at, first_voiced_at)
        trial = {"trial": 1, **detection, "metric": "fixture_end_to_audible",
                 "first_voiced_pcm_ms": round((first_voiced_at - fixture_end_at) * 1000, 1) if first_voiced_at is not None else None,
                 "cold_ready_ms": result["cold_ready_ms"],
                 "clock_uncertainty_ms": round(bench.device.clock_rtt_ms / 2 + 4, 2),
                 "digital_energy_resolution_ms": 10, "input_chunk_ms": 20}
        result["trials"].append(trial)
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
        result["reference_samples"] = len(reference) // 2
        result["reference_timing_gap"] = reference_gap
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["summary"] = summarize(result["trials"])
        if hasattr(bench, "observe_firmware"):
            bench.observe_firmware()
        bench.device.frames.clear()
        # Rebind rather than resize: a completed worker may briefly retain its
        # NumPy view, but no recording/reference is attached to the result.
        reference = bytearray()
        bench.stream = None
        bench.test = {"running": False, "phase": "idle"}
