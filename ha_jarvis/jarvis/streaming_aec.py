"""Align microphone samples to a bounded, live AirPlay render history."""
from __future__ import annotations

import math

import av
import numpy as np

from .aec import FRAME_SAMPLES, HostAEC3, SAMPLE_RATE


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
        self._converter = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        self._history = np.zeros(4 * SAMPLE_RATE, dtype="<i2")
        self._render_start = None
        self._render_end = 0
        self._capture_start = self._capture_stamp = None
        self._next_capture = self._processed = 0
        self._pending = np.empty(0, dtype="<i2")
        self.capture_drift_ppm = 0.0

    def render(self, pcm, handed_off_at):
        """Accept exact stereo PCM16BE handed to the 44.1 kHz AirPlay sender."""
        stereo = np.frombuffer(pcm, dtype=">i2").reshape(-1, 2)
        if self._render_start is None:
            self._render_start = handed_off_at
        mono = stereo[:, 0].astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(mono, format="s16", layout="mono")
        frame.sample_rate = 44100
        for output in self._converter.resample(frame):
            samples = output.to_ndarray().reshape(-1)
            indices = np.arange(self._render_end, self._render_end + len(samples))
            self._history[indices % len(self._history)] = samples
            self._render_end += len(samples)

    def _reference(self, capture_at, period):
        if self._render_start is None:
            return bytes(FRAME_SAMPLES * 2)
        start = (capture_at - self.delay_seconds - self._render_start) * SAMPLE_RATE
        positions = start + np.arange(FRAME_SAMPLES) * period * SAMPLE_RATE
        left = np.floor(positions).astype(np.int64)
        audible = left >= 0
        if not np.any(audible):
            return bytes(FRAME_SAMPLES * 2)
        indices = left[audible]
        if indices[0] < max(0, self._render_end - len(self._history)) or indices[-1] + 1 >= self._render_end:
            raise RuntimeError("Live echo reference is outside the retained render timeline")
        fraction = positions[audible] - indices
        values = np.zeros(FRAME_SAMPLES, dtype="<i2")
        values[audible] = np.rint(
            self._history[indices % len(self._history)] * (1 - fraction)
            + self._history[(indices + 1) % len(self._history)] * fraction
        ).astype("<i2")
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
