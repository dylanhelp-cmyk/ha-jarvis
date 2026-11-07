"""Align microphone samples to a bounded Voice PE render history."""
from __future__ import annotations

from collections import deque
import math

import av
import numpy as np

from .aec import FRAME_SAMPLES, HostAEC3, SAMPLE_RATE


class RenderBurst:
    """One continuous I2S render generation with an explicit terminal report."""

    def __init__(self, start):
        self.start = start
        self.converter = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        self.history = np.zeros(4 * SAMPLE_RATE, dtype="<i2")
        self.end = self.frames = 0
        self.ended_at = None

    def store(self, output):
        samples = output.to_ndarray().reshape(-1)
        indices = np.arange(self.end, self.end + len(samples))
        self.history[indices % len(self.history)] = samples
        self.end += len(samples)

    def append(self, pcm):
        stereo = np.frombuffer(pcm, dtype="<i2").reshape(-1, 2)
        self.frames += len(stereo)
        mono = (stereo.astype(np.int32).sum(axis=1) // 2).astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(mono, format="s16", layout="mono")
        frame.sample_rate = 48000
        for output in self.converter.resample(frame):
            self.store(output)

    def finish(self, ended_at):
        for output in self.converter.resample(None):
            self.store(output)
        self.ended_at = ended_at

    def read(self, times, values):
        selected = times >= self.start
        if self.ended_at is not None:
            selected &= times < self.ended_at
        if not np.any(selected):
            return
        duration = (self.ended_at - self.start if self.ended_at is not None else None)
        rate = self.end / duration if duration else SAMPLE_RATE
        positions = (times[selected] - self.start) * rate
        indices = np.floor(positions).astype(np.int64)
        if (indices[0] < max(0, self.end - len(self.history))
                or (self.ended_at is None and indices[-1] + 1 >= self.end)):
            raise RuntimeError("Live echo reference is outside the retained render timeline")
        fraction = positions - indices
        left = self.history[indices % len(self.history)]
        right = self.history[(indices + 1) % len(self.history)]
        right = np.where(indices + 1 < self.end, right, 0)
        values[selected] = np.rint(left * (1 - fraction) + right * fraction).astype("<i2")


class StreamingEcho:
    """One owner, one continuous speaker and microphone clock epoch."""

    def __init__(self, delay_ms, *, processor=None):
        if not math.isfinite(delay_ms) or not 0 <= delay_ms <= 3000:
            raise ValueError("Live echo delay must be between 0 and 3000 ms")
        # Keep the supplied reference ahead of the acoustic return even when
        # the room/clock estimate is a few milliseconds too high.
        self.residual_delay_ms = min(40, int(delay_ms))
        self.delay_seconds = (delay_ms - self.residual_delay_ms) / 1000
        self.processor = processor if processor is not None else HostAEC3(stream_delay_ms=self.residual_delay_ms)
        self._bursts = deque()
        self._render_floor = None
        self._capture_start = self._capture_stamp = None
        self._next_capture = self._processed = 0
        self._pending = np.empty(0, dtype="<i2")
        self.capture_drift_ppm = 0.0

    def render(self, pcm, rendered_at, *, first_frame):
        """Accept ordered device-confirmed 48 kHz stereo PCM16LE."""
        if not pcm or len(pcm) % 4 or not math.isfinite(rendered_at):
            raise ValueError("Invalid wired render frame")
        burst = self._bursts[-1] if self._bursts else None
        if burst is None or burst.ended_at is not None:
            if first_frame or (burst is not None and rendered_at < burst.ended_at):
                raise RuntimeError("Speaker discontinuity invalidated live echo alignment")
            while self._bursts and (len(self._bursts) >= 8
                    or rendered_at - self._bursts[0].ended_at >= 4):
                self._render_floor = self._bursts.popleft().ended_at
            burst = RenderBurst(rendered_at)
            self._bursts.append(burst)
        if first_frame != burst.frames:
            raise RuntimeError("Speaker discontinuity invalidated live echo alignment")
        burst.append(pcm)

    def finish_render(self, rendered_frames, ended_at):
        """Confirm the boundary between this rendered generation and known silence."""
        burst = self._bursts[-1] if self._bursts else None
        if (burst is None or burst.ended_at is not None or rendered_frames != burst.frames
                or not math.isfinite(ended_at) or ended_at <= burst.start):
            raise RuntimeError("Incomplete speaker render cannot establish an echo boundary")
        if abs((ended_at - burst.start) - rendered_frames / 48000) > .015:
            raise RuntimeError("Speaker render clock span cannot establish an echo boundary")
        burst.finish(ended_at)

    def _reference(self, capture_at, period):
        start = capture_at - self.delay_seconds
        if self._render_floor is not None and start < self._render_floor:
            raise RuntimeError("Live echo reference is outside the retained render timeline")
        times = start + np.arange(FRAME_SAMPLES) * period
        values = np.zeros(FRAME_SAMPLES, dtype="<i2")
        for burst in self._bursts:
            burst.read(times, values)
        return values.tobytes()

    def capture(self, frame, clock_offset_ms):
        """Return cleaned 10 ms blocks without dropping or resampling speech."""
        if frame.first_sample != self._next_capture:
            raise RuntimeError("Microphone discontinuity invalidated live echo alignment")
        if self._capture_start is None:
            if clock_offset_ms is None:
                raise RuntimeError("Microphone clock has not been synchronized")
            self._capture_stamp = frame.capture_ms
            self._capture_start = (frame.capture_ms + clock_offset_ms) / 1000
        elif frame.first_sample >= 5 * SAMPLE_RATE:
            elapsed_ms = (frame.capture_ms - self._capture_stamp) % (2 ** 32)
            estimate_ppm = (elapsed_ms * SAMPLE_RATE / (1000 * frame.first_sample) - 1) * 1e6
            if abs(estimate_ppm) > 5000:
                raise RuntimeError("Microphone clock changed during live echo alignment")
            self.capture_drift_ppm += .02 * (estimate_ppm - self.capture_drift_ppm)
        period = (1 + self.capture_drift_ppm / 1e6) / SAMPLE_RATE
        self._next_capture += len(frame.samples)
        pcm = np.concatenate((self._pending, frame.samples))
        end = len(pcm) - len(pcm) % FRAME_SAMPLES
        cleaned = []
        for offset in range(0, end, FRAME_SAMPLES):
            capture_at = self._capture_start + self._processed * period
            reference = self._reference(capture_at, period)
            cleaned.append(self.processor.process(pcm[offset:offset + FRAME_SAMPLES].tobytes(), reference))
            self._processed += FRAME_SAMPLES
        self._pending = pcm[end:].copy()
        return b"".join(cleaned)


def residual_echo_similarity(clean_pcm, render):
    """Peak normalized correlation over the caller's bounded render window."""
    mic = np.frombuffer(clean_pcm, dtype="<i2").astype(float)
    reference = np.asarray(render, dtype=float)
    if not len(mic) or len(reference) < len(mic):
        return None
    mic -= mic.mean()
    power = np.dot(mic, mic)
    if power <= 0:
        return None
    count = len(mic)
    total = np.concatenate(([0.0], np.cumsum(reference)))
    square = np.concatenate(([0.0], np.cumsum(reference ** 2)))
    variance = square[count:] - square[:-count] - (total[count:] - total[:-count]) ** 2 / count
    if not np.any(variance > 0):
        return None
    correlation = np.correlate(reference, mic, mode="valid")
    denominator = np.sqrt(power * np.maximum(variance, 0))
    scores = np.divide(np.abs(correlation), denominator, out=np.zeros_like(correlation), where=denominator > 0)
    return float(np.clip(np.max(scores), 0, 1))


class ConfirmedRenderInput:
    """Release microphone samples only when the matching DMA reference is known."""

    def __init__(self, player, pcm, delay_ms):
        self.player, self.pcm = player, pcm
        self.echo = StreamingEcho(delay_ms)
        self.waiting = deque()
        self.waiting_samples = self.max_waiting_samples = 0
        self.rendered_frames = self.processed_during_playback = 0
        self.finished = False
        self.error = None

    def prime_capture(self, frames, offset_ms):
        """Process quiet pre-roll before the playback command is sent."""
        try:
            return b"".join(self.echo.capture(frame, offset_ms) for frame in frames)
        except (RuntimeError, ValueError) as exc:
            self.error = str(exc)
            return b""

    def offer(self, frame, offset_ms):
        if self.error:
            return b""
        self.waiting.append(frame)
        self.waiting_samples += len(frame.samples)
        self.max_waiting_samples = max(self.max_waiting_samples, self.waiting_samples)
        try:
            if self.waiting_samples > SAMPLE_RATE // 2:
                raise RuntimeError("No reliable render report within the half-second microphone window")
            return self._release(offset_ms)
        except (RuntimeError, ValueError) as exc:
            self.error = str(exc)
            self.waiting.clear()
            return b""

    def _release(self, offset_ms):
        player = self.player
        first, last = player.first_rendered_ms, player.last_rendered_ms
        count = player.frames_rendered or 0
        if (player.render_clock_invalid or player.schedule_rebases
                or (player.max_render_discontinuity_us or 0) > 15_000):
            raise RuntimeError("Discontinuous speaker output invalidated the live echo reference")
        if first is None or offset_ms is None:
            if player.terminal_confirmed:
                raise RuntimeError("Playback ended without a timed DMA reference")
            return b""
        if abs(last - first - count / 48) > 15 or count * 4 > len(self.pcm):
            raise RuntimeError("DMA timestamps do not match the available reference samples")
        if count > self.rendered_frames:
            self.echo.render(bytes(self.pcm[self.rendered_frames * 4:count * 4]),
                             (first + offset_ms) / 1000, first_frame=self.rendered_frames)
            self.rendered_frames = count
        if player.terminal_confirmed and not self.finished:
            if player.reason != "drained" or count != player.frames_sent:
                raise RuntimeError("Playback stopped before the reference could be confirmed")
            self.echo.finish_render(count, (last + offset_ms) / 1000)
            self.finished = True

        # Leave room for millisecond quantization and the resampler's filter tail.
        horizon = math.inf if self.finished else first + count / 48 - 20
        clean = bytearray()
        while self.waiting:
            frame = self.waiting[0]
            if frame.capture_ms + len(frame.samples) * 1000 / SAMPLE_RATE > horizon:
                break
            self.waiting.popleft()
            self.waiting_samples -= len(frame.samples)
            clean.extend(self.echo.capture(frame, offset_ms))
        if not self.finished:
            self.processed_during_playback += len(clean) // 2
        return bytes(clean)


def check_confirmed_references(frames, renders, clock_offset_ms, delay_ms):
    """Process a short local capture using confirmed render bursts and known gaps."""
    scope = "offline render alignment and sample accounting"
    if not frames or not renders or not 0 <= delay_ms <= 300:
        return {"status": "unavailable", "scope": scope, "reason": "Unreliable render-to-microphone alignment"}
    try:
        echo = StreamingEcho(delay_ms)
        for pcm, first_ms, last_ms in renders:
            echo.render(pcm, (first_ms + clock_offset_ms) / 1000, first_frame=0)
            echo.finish_render(len(pcm) // 4, (last_ms + clock_offset_ms) / 1000)
        processed = sum(len(echo.capture(frame, clock_offset_ms)) // 2 for frame in frames)
    except (ImportError, RuntimeError, ValueError) as exc:
        return {"status": "unavailable", "scope": scope, "reason": str(exc) or type(exc).__name__}
    total = sum(len(frame.samples) for frame in frames)
    return {"status": "processed", "scope": scope, "estimated_delay_ms": round(delay_ms, 1),
            "processed_samples": processed, "partial_final_block_samples": total - processed,
            "confirmed_bursts": len(renders), "near_end_preservation_verified": False}


def check_opening_reference(frames, pcm, clock_offset_ms, first_rendered_ms, last_rendered_ms,
                            acoustic_onset, voiced_start):
    """Exercise the acknowledged clip as an AEC3 reference on an actual wake capture."""
    if (type(first_rendered_ms) is not int or type(last_rendered_ms) is not int
            or acoustic_onset is None):
        return {"status": "unavailable", "reason": "Missing rendered or acoustic onset"}
    rendered_at = (first_rendered_ms + clock_offset_ms) / 1000
    delay_ms = (acoustic_onset - rendered_at - voiced_start / SAMPLE_RATE) * 1000
    return check_confirmed_references(frames, [(pcm, first_rendered_ms, last_rendered_ms)],
                                      clock_offset_ms, delay_ms)


def compare_opening_prefix(frames, pcm, clock_offset_ms, first_ms, last_ms, confirmed_frames, delay_ms):
    """Replay the first proven portion locally and compare it with the full reference."""
    if not 0 < confirmed_frames <= len(pcm) // 4:
        return {"status": "unavailable", "reason": "Invalid confirmed render length"}
    # Leave room for resampler lookahead and quantized device timestamps.
    horizon_ms = first_ms + confirmed_frames / 48 - 20
    eligible = []
    for frame in frames:
        if frame.capture_ms + len(frame.samples) * 1000 / SAMPLE_RATE > horizon_ms:
            break
        eligible.append(frame)
    if not eligible:
        return {"status": "unavailable", "reason": "The first report covered no overlapping microphone frames"}
    last_frame = eligible[-1]
    covered_until_ms = last_frame.capture_ms + len(last_frame.samples) * 1000 / SAMPLE_RATE
    if covered_until_ms <= first_ms:
        return {"status": "unavailable", "reason": "The first report covered no overlapping microphone frames"}

    def clean(reference, terminal_ms=None):
        echo = StreamingEcho(delay_ms)
        echo.render(reference, (first_ms + clock_offset_ms) / 1000, first_frame=0)
        if terminal_ms is not None:
            echo.finish_render(len(reference) // 4, (terminal_ms + clock_offset_ms) / 1000)
        return np.frombuffer(b"".join(echo.capture(frame, clock_offset_ms) for frame in eligible), dtype="<i2")

    try:
        early = clean(pcm[:confirmed_frames * 4])
        complete = clean(pcm, last_ms)
    except (ImportError, RuntimeError, ValueError) as exc:
        return {"status": "unavailable", "reason": str(exc) or type(exc).__name__}
    difference = early.astype(np.float64) - complete
    return {"status": "compared", "capture_samples": len(early),
            "overlapping_capture_ms": round(covered_until_ms - first_ms, 1),
            "rms_difference_pcm": round(float(np.sqrt(np.mean(difference ** 2))), 2)}
