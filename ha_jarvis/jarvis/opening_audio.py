"""Bounded microphone handoff while the cloud connection starts."""
from __future__ import annotations

from .audio import RATE


class OpeningAudio:
    def __init__(self, max_ms=3000):
        self.max_ms = max_ms
        self.frames = []
        self.samples = 0
        self.first_at = None
        self.error = None

    def offer(self, frame, clock_offset_ms):
        if self.error is not None:
            return
        if frame.sequence != len(self.frames) or frame.first_sample != self.samples or clock_offset_ms is None:
            self.error = "opening_audio_discontinuous"
        elif self.samples + len(frame.samples) > self.max_ms * RATE // 1000:
            self.error = "opening_audio_overflow"
        if self.error is not None:
            self.frames.clear()
            return
        if self.first_at is None:
            self.first_at = (frame.capture_ms + clock_offset_ms) / 1000
        self.frames.append(frame)
        self.samples += len(frame.samples)

    def release(self, now):
        age_ms = round((now - self.first_at) * 1000, 1) if self.first_at is not None else None
        reason = self.error
        if reason is None:
            if age_ms is None:
                reason = "opening_audio_missing"
            elif age_ms > self.max_ms or age_ms < -100:
                reason = "opening_audio_expired"
        frames = tuple(self.frames) if reason is None else ()
        self.frames.clear()
        return frames, {"status": "ready" if reason is None else "retry", "reason": reason,
                        "sample_count": self.samples, "oldest_sample_age_ms": age_ms,
                        "capacity_ms": self.max_ms}
