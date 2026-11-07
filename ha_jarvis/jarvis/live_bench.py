"""Reusable digital voice and acoustic matching helpers."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import io
import re
import time
import wave

import av
import numpy as np

from .audio import RATE, detect_probe
from .playback import JackPlayback, RATE as RENDER_RATE
from .live_session import INSTRUCTIONS, LiveSession


POST_INPUT_SECONDS = 15.0


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
    """Reconstruct the mono reference from the stereo PCM sent to the Voice PE jack."""
    stereo = np.frombuffer(pcm, dtype="<i2").reshape(-1, 2)
    # Jarvis duplicates its mono input into both channels.
    mono = stereo[:, 0].astype(np.int16).reshape(1, -1)
    frame = av.AudioFrame.from_ndarray(mono, format="s16", layout="mono")
    frame.sample_rate = RENDER_RATE
    converter = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    converted = converter.resample(frame) + converter.resample(None)
    return np.concatenate([block.to_ndarray().reshape(-1) for block in converted])


def match_response(captured, capture_start, reference, voiced_start_sample,
                   fixture_end_at, earliest_output_at, *, handoff_at=None,
                   acoustic_delay_range=(0.0, 2.5), window_seconds=.32, onset_horizon_seconds=4, allow_late_outlier=False):
    """Require distinct reply windows to agree before reporting onset."""
    unavailable = {"status": "unavailable", "latency_ms": None,
                   "fixture_end_to_audible_ms": None, "windows": []}
    width = int(window_seconds * RATE)
    bounds = voiced_bounds(reference)
    if bounds is None or bounds[1] - voiced_start_sample < width + int(min(.2, window_seconds) * RATE):
        return {**unavailable, "reason": "Reply is too short or weak for two distinct acoustic windows"}
    # Select distinct windows from the configured onset region.
    last = min(bounds[1] - width, voiced_start_sample + int(onset_horizon_seconds * RATE))
    starts = sorted(set(int(value) for value in np.linspace(voiced_start_sample, last, 4)))
    starts = [value for index, value in enumerate(starts)
              if index == 0 or value - starts[index - 1] >= int(.06 * RATE)]
    candidates, windows = [], []
    for offset in starts:
        start_at = earliest_output_at - .1
        end_at = fixture_end_at + POST_INPUT_SECONDS + 4
        if handoff_at is not None:
            handoff = handoff_at(offset)
            if handoff is None:
                windows.append({"reference_offset_ms": round(offset * 1000 / RATE, 2),
                                "status": "unavailable", "reason": "No speaker acceptance timestamp"})
                continue
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
    if (spread > 40 and allow_late_outlier and len(candidates) == len(windows) == 4
            and min(w["confidence"] for w in windows[:3]) >= .8
            and max(candidates[:3]) - min(candidates[:3]) <= .012
            and candidates[-1] > max(candidates[:3])):
        windows[-1]["late_outlier"] = True
        candidates = candidates[:3]
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


def match_streamed_reply(captured, capture_start, reply, endpoint_at, clock_offset_ms,
                         *, alignment_ms=None, clock_uncertainty_ms=0):
    """Search actual room audio near the device's confirmed render timeline."""
    reference = render_reference(bytes(reply.pcm))
    onset = reply.first_voice_sample
    if onset is None or onset >= len(reference):
        return {"status": "unavailable", "reason": "Reply reference had no usable speech", "latency_ms": None}

    span = reply.player.confirmed_span()
    span_error_ms = abs(span[1] - span[0] - reply.player.frames_sent / 48) if span else None
    if alignment_ms is not None and span_error_ms is not None and span_error_ms <= 40:
        dma_start = (span[0] + clock_offset_ms) / 1000
        margin_ms = clock_uncertainty_ms + span_error_ms + 25
        delay_range = ((alignment_ms - margin_ms) / 1000, (alignment_ms + margin_ms) / 1000)

        def handoff(sample):
            return dma_start + sample / RATE

        scope = "confirmed DMA span and current echo alignment"
    else:
        delay_range = (-.04, .45)

        def handoff(sample):
            return reply.player.accepted_at(sample * 3, clock_offset_ms)

        scope = "speaker acceptance timestamps"

    match = match_response(captured, capture_start, reference, onset, endpoint_at, reply.first_voice_at,
                           handoff_at=handoff, acoustic_delay_range=delay_range,
                           window_seconds=.14, onset_horizon_seconds=.28)
    match["acoustic_search_clock"] = scope
    return match


SYNTHETIC_PROMPTS = (
    "What is the capital of France?",
    "And which river flows through that city?",
)


async def synthesize_fixture(prompt):
    """Build a digital test asset on the App. No recording or upload endpoint is used."""
    if prompt not in SYNTHETIC_PROMPTS:
        raise ValueError("Unknown built-in synthetic prompt")
    process = await asyncio.create_subprocess_exec(
        "espeak-ng", "--stdout", "-v", "en-gb", "--stdin",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(prompt.encode("ascii")), 5)
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    if process.returncode or len(output) > 512000:
        raise RuntimeError("Local synthetic speech generation failed")
    with wave.open(io.BytesIO(output)) as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("Unexpected synthetic speech format")
        samples = np.frombuffer(source.readframes(512000), dtype="<i2")
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = source.getframerate()
    converter = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    blocks = converter.resample(frame) + converter.resample(None)
    pcm = np.concatenate([block.to_ndarray().ravel() for block in blocks])
    bounds = voiced_bounds(pcm)
    if bounds is None or not RATE // 2 <= len(pcm) <= 6 * RATE:
        raise ValueError("Synthetic speech has no usable endpoint or exceeds six seconds")
    return pcm.astype("<i2", copy=False).tobytes(), bounds[1]


class BenchReply:
    def __init__(self, device, identity, *, output_gain_db=0, output_buffer_seconds=1):
        self.player = JackPlayback(device, identity)
        self.queue = asyncio.Queue(maxsize=65 * output_buffer_seconds)
        self.queue_limit_bytes = output_buffer_seconds * RENDER_RATE * 4
        self.pending_bytes = 0
        self.pcm = bytearray()
        self.transcript = ""
        self.first_voice_at = self.last_voice_at = None
        self.last_audio_at = None
        self.first_voice_sample = None
        self.leading_pcm = b""
        self.output_scale = 10 ** (output_gain_db / 20)
        self.converter = av.AudioResampler(format="s16", layout="mono", rate=RENDER_RATE)

    def _render(self, frame):
        for block in self.converter.resample(frame):
            mono = block.to_ndarray().ravel()
            if self.output_scale != 1:
                mono = np.rint(mono * self.output_scale)
            stereo = np.repeat(mono, 2).astype("<i2").tobytes()
            if self.pending_bytes + len(stereo) > self.queue_limit_bytes or len(self.pcm) + len(stereo) > 8 * RENDER_RATE * 4:
                raise RuntimeError("Synthetic reply exceeded its playback queue or duration bound")
            self.queue.put_nowait(stereo)
            self.pending_bytes += len(stereo)
            self.pcm.extend(stereo)

    async def receive(self, pcm, rate):
        self.last_audio_at = time.monotonic()
        samples = np.frombuffer(pcm, dtype="<i2")
        bounds = voiced_bounds(samples)
        if bounds is not None:
            self.last_voice_at = time.monotonic()
            if self.first_voice_at is None:
                self.first_voice_at = self.last_voice_at
                # Preserve quiet consonants that precede the first strong
                # block, including when they arrived in an earlier packet.
                prefix = (self.leading_pcm + pcm[:2 * bounds[0]])[-2 * (rate // 25):]
                self.first_voice_sample = len(prefix) // 2
                samples = np.frombuffer(prefix + pcm[2 * bounds[0]:], dtype="<i2")
                self.leading_pcm = b""
        elif self.first_voice_at is None:
            self.leading_pcm = (self.leading_pcm + pcm)[-2 * (rate // 25):]
            return
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = rate
        self._render(frame)

    def ready_to_drain(self, now):
        return self.last_voice_at is not None and (
            now - self.last_audio_at >= .8 or now - self.last_voice_at >= 1.2
        )

    async def chunks(self):
        while (chunk := await self.queue.get()) is not None:
            self.pending_bytes -= len(chunk)
            yield chunk

    async def finish(self):
        self._render(None)
        await self.queue.put(None)


REPLY_PROFILES = {
    "baseline": INSTRUCTIONS,
    "direct": (
        "Listen until the user finishes a clear question, then start the answer promptly. "
        "Lead with the answer itself in a complete short sentence. "
        "Wait through thinking pauses or incomplete questions. Do not greet, delegate work or use tools."
    ),
}


async def benchmark_replies(device, settings, transport, identity, fixtures, timings, stage, profile):
    """Stream two digital fixtures and two incremental replies on one Live session."""
    current = None

    async def on_audio(pcm, rate):
        if current is not None:
            await current.receive(pcm, rate)

    def on_transcript(delta):
        if current is not None and isinstance(delta, str):
            current.transcript = (current.transcript + delta)[:512]

    if transport == "realtime":
        from .realtime_bench import RealtimeBenchmarkSession

        session = RealtimeBenchmarkSession(settings, on_audio=on_audio,
                                           instructions=REPLY_PROFILES[profile], on_transcript=on_transcript)
    else:
        session = LiveSession(settings, on_audio=on_audio, transport=transport,
                              instructions=REPLY_PROFILES[profile], on_transcript=on_transcript)
    failure = None
    try:
        stage("connecting to OpenAI Realtime" if transport == "realtime" else "connecting to GPT-Live")
        await session.start()
        for index, (fixture, endpoint) in enumerate(fixtures, 1):
            reply = BenchReply(device, f"{identity}-{index}", output_gain_db=-6,
                               output_buffer_seconds=8 if transport == "realtime" else 1)
            current = reply
            sender = None
            if transport != "realtime":
                device.playback = reply.player
                sender = asyncio.create_task(reply.player.send_stream(reply.chunks(), prime_packets=5,
                                                                       source_prime_packets=12))
            stage(f"streaming synthetic turn {index}")
            origin = time.monotonic()
            if transport == "realtime":
                session.arm_turn()
            input_samples = 0
            fixture_end_at = None
            try:
                while time.monotonic() < origin + len(fixture) / (2 * RATE) + 9:
                    if sender is not None and sender.done():
                        sender.result()
                        raise RuntimeError("Reply playback ended before the turn finished")
                    if session.metrics.error:
                        raise RuntimeError("GPT-Live failed during the synthetic turn")
                    due = origin + input_samples / RATE
                    await asyncio.sleep(max(0, due - time.monotonic()))
                    if time.monotonic() - due > .1:
                        raise RuntimeError("Synthetic input pacing exceeded 100 ms")
                    pcm = fixture[input_samples * 2:(input_samples + 320) * 2].ljust(640, b"\0")
                    sent_at = time.monotonic()
                    await session.send_audio(pcm)
                    if input_samples < endpoint <= input_samples + 320:
                        fixture_end_at = sent_at + (endpoint - input_samples) / RATE
                    input_samples += 320
                    complete = (session.reply_finished(index) if transport == "realtime"
                                else reply.ready_to_drain(time.monotonic()))
                    if fixture_end_at and complete:
                        break
                else:
                    raise TimeoutError("No complete spoken reply arrived within the trial window")
                current = None
                if sender is not None:
                    async with asyncio.timeout(3):
                        await reply.finish()
                        await sender
                    if reply.player.frames_rendered != reply.player.frames_sent:
                        raise RuntimeError("Voice PE did not confirm rendering the complete synthetic reply")
                if reply.first_voice_at is None:
                    raise RuntimeError("The synthetic reply contained no voiced audio")
                expected = ("paris", "seine")[index - 1]
                answer_check = (expected in re.findall(r"[a-z]+", reply.transcript.casefold())
                                if reply.transcript.strip() else None)
                reply.transcript = ""
                timings.append((fixture_end_at, reply, answer_check))
            except (OSError, RuntimeError, ValueError, TimeoutError):
                current = None
                if sender is not None and reply.first_voice_at is not None and not sender.done():
                    with suppress(OSError, RuntimeError, ValueError, TimeoutError):
                        async with asyncio.timeout(8):
                            await reply.finish()
                            await sender
                raise
            finally:
                current = None
                if sender is not None:
                    if not sender.done():
                        sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                    with suppress(OSError, RuntimeError, TimeoutError):
                        await reply.player.stop()
                device.playback = None
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        failure = session.metrics.error or str(exc) or type(exc).__name__
    finally:
        metrics = await session.close()
    return {"transport": transport, "model": metrics.model, "profile": profile,
            "playback_enabled": transport != "realtime", "playback_prime_ms": 50, "source_reserve_ms": 120,
            "output_gain_db": -6, "startup_ms": round(metrics.ready_ms, 1) if metrics.ready_ms is not None else None,
            "finalization_confirmed": metrics.finalization_confirmed, "usage_seconds": metrics.usage_seconds,
            "failure": failure}
