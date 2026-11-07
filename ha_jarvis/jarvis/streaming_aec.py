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
