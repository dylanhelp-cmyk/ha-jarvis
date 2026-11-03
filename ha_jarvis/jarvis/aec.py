"""Bounded 16 kHz host AEC3 core; capture/render clock alignment belongs to caller.

Each call pairs 10 ms of capture with 10 ms of current outgoing render on the
same timeline. The configured reference delay postpones only the AEC reference,
never microphone delivery or speaker playback. It is an explicit estimate, not
automatic acoustic calibration. Independent device clock drift is not corrected
here; the caller must monitor alignment and reset after any discontinuity.

Uses pywebrtc-audio==0.2.0's direct WebRTC EchoCanceller3Factory binding. Instances
have one sequential owner; the native processor is not thread-safe.
"""
from __future__ import annotations

from collections import deque
from importlib.metadata import PackageNotFoundError, version
import time

import numpy as np


SAMPLE_RATE = 16000
FRAME_SAMPLES = 160
FRAME_BYTES = FRAME_SAMPLES * 2
DEPENDENCY = 'pywebrtc-audio'
DEPENDENCY_VERSION = '0.2.0'
_SILENCE = bytes(FRAME_BYTES)


class HostAEC3:
    """Process exact PCM16LE mono frames without silently padding/skipping audio.

    For an explicitly estimated 600 ms render-to-capture path, an initial trial
    can use reference_delay_ms=500 and stream_delay_ms=100. The latter is the
    residual native AEC delay hint, limited to 500 ms. No setting establishes
    actual cancellation or preservation of simultaneous user speech.
    """

    def __init__(self, *, reference_delay_ms: int = 0, stream_delay_ms: int = 0):
        if (isinstance(reference_delay_ms, bool) or not isinstance(reference_delay_ms, int)
                or not 0 <= reference_delay_ms <= 2000 or reference_delay_ms % 10):
            raise ValueError('AEC reference delay must be 0..2000 ms in 10 ms steps')
        if (isinstance(stream_delay_ms, bool) or not isinstance(stream_delay_ms, int)
                or not 0 <= stream_delay_ms <= 500):
            raise ValueError('AEC stream delay hint must be an integer from 0 to 500 ms')
        try:
            installed = version(DEPENDENCY)
        except PackageNotFoundError as exc:
            raise RuntimeError('Host AEC3 requires pywebrtc-audio==0.2.0') from exc
        if installed != DEPENDENCY_VERSION:
            raise RuntimeError('Host AEC3 requires the tested pywebrtc-audio==0.2.0')
        from pywebrtc_audio import EchoCanceller
        self._processor = EchoCanceller(sample_rate=SAMPLE_RATE, num_channels=1,
                                        stream_delay_ms=stream_delay_ms)
        self._reference_delay_ms = reference_delay_ms
        self._stream_delay_ms = stream_delay_ms
        self._delay_frames = reference_delay_ms // 10
        self._reference: deque[bytes] = deque(maxlen=self._delay_frames + 1)
        self.generation = 0
        self.frames_processed = 0
        self.max_processing_ms = 0.0
        self._prime_reference()

    reference_delay_ms = property(lambda self: self._reference_delay_ms)
    stream_delay_ms = property(lambda self: self._stream_delay_ms)

    def _prime_reference(self):
        self._reference.clear()
        self._reference.extend([_SILENCE] * self._delay_frames)

    def process(self, capture_pcm: bytes, render_pcm: bytes) -> bytes:
        """Return one cleaned frame; both inputs must be exactly 160 samples.

        Supply explicit zero render frames during silence, including after stop
        while the existing acoustic echo drains. Reset on a route/clock epoch
        change rather than combining references from unrelated sessions.
        """
        for name, pcm in (('capture', capture_pcm), ('render', render_pcm)):
            if not isinstance(pcm, bytes) or len(pcm) != FRAME_BYTES:
                raise ValueError(f'AEC {name} requires exactly 320 bytes of PCM16LE mono')
        self._reference.append(render_pcm)
        reference_pcm = self._reference.popleft()
        started = time.perf_counter()
        clean = self._processor.process(np.frombuffer(capture_pcm, dtype='<i2'),
                                        np.frombuffer(reference_pcm, dtype='<i2'))
        if clean.dtype != np.dtype('int16') or clean.shape != (FRAME_SAMPLES,):
            raise RuntimeError('AEC3 returned an unexpected audio frame')
        result = clean.astype('<i2', copy=False).tobytes()
        self.max_processing_ms = max(self.max_processing_ms,
                                     (time.perf_counter() - started) * 1000)
        self.frames_processed += 1
        return result

    def reset(self):
        """Discard old reference/adaptation after an explicit discontinuity."""
        self._processor.reset()
        self._prime_reference()
        self.generation += 1
        self.frames_processed = 0
        self.max_processing_ms = 0.0

    def snapshot(self) -> dict:
        return {'backend': 'WebRTC AEC3', 'dependency_version': DEPENDENCY_VERSION,
                'sample_rate_hz': SAMPLE_RATE, 'frame_samples': FRAME_SAMPLES,
                'reference_delay_ms': self.reference_delay_ms,
                'stream_delay_hint_ms': self.stream_delay_ms,
                'configured_total_delay_ms': self.reference_delay_ms + self.stream_delay_ms,
                'reference_buffer_frames': len(self._reference),
                'frames_processed': self.frames_processed, 'generation': self.generation,
                'max_processing_ms': self.max_processing_ms,
                'automatic_calibration': False, 'clock_drift_compensation': False,
                'acoustic_cancellation_verified': False}
