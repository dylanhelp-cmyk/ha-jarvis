"""Bounded continuous PCM source for an already connected RAOP session."""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from fractions import Fraction

import av
import numpy as np
from pyatv.protocols.raop.audio_source import AudioSource
from pyatv.support.metadata import EMPTY_METADATA


class StaleAudioGeneration(RuntimeError):
    """A cancelled reply attempted to enqueue more audio."""


class LiveAudioSource(AudioSource):
    """16 kHz mono PCM16LE in; clocked 44.1 kHz stereo PCM16BE out.

    RAOP owns pacing. Temporary starvation produces silence, never EOF or a
    reconnect. Methods mutate state without yielding, so interruption cannot
    race a partly completed write/read on the same event loop. Producers can
    fence delayed reply chunks by passing the generation captured at reply start.
    """

    INPUT_RATE = 16000
    MAX_PENDING_SECONDS = 2

    def __init__(self, sample_rate=44100, channels=2, sample_size=2, *, clock=time.monotonic,
                 on_render=None):
        if (any(type(value) is not int for value in (sample_rate, channels, sample_size))
                or (sample_rate, channels, sample_size) != (44100, 2, 2)):
            raise ValueError('Live AirPlay requires 44100 Hz stereo PCM16')
        self._sample_rate, self._channels, self._sample_size = sample_rate, channels, sample_size
        self._clock = clock
        self._on_render = on_render
        self._capacity = sample_rate * self.MAX_PENDING_SECONDS
        self._queue = deque()
        self._timestamps = deque()
        self._head_offset = 0
        self._queued_frames = 0
        self._input_frames = self._generated_frames = 0
        self._resampler = self._new_resampler()
        self.closed = False
        self.first_read = asyncio.Event()
        self.generation = 0
        self.telemetry = {'queued_frames': 0, 'resampler_pending_frames': 0,
                          'max_queue_residence_ms': None, 'silence_frames': 0,
                          'overflow_count': 0}

    def _new_resampler(self):
        return av.AudioResampler(format='s16', layout='stereo', rate=self.sample_rate)

    def _update_pending(self):
        self.telemetry['queued_frames'] = self._queued_frames
        self.telemetry['resampler_pending_frames'] = max(
            0, math.ceil(self._input_frames * self.sample_rate / self.INPUT_RATE) - self._generated_frames)

    async def write(self, pcm: bytes, *, generation=None):
        if self.closed:
            raise RuntimeError('Live AirPlay source is closed')
        if generation is not None and generation != self.generation:
            raise StaleAudioGeneration('Audio belongs to an interrupted reply')
        if not isinstance(pcm, bytes) or len(pcm) % 2:
            raise ValueError('Live audio must be complete mono PCM16LE samples')
        if not pcm:
            return
        count = len(pcm) // 2
        # Include samples held in the resampler, not just visible queue bytes.
        pending_after = math.ceil((self._input_frames + count) * self.sample_rate / self.INPUT_RATE) - self._generated_frames
        if self._queued_frames + pending_after > self._capacity:
            self.telemetry['overflow_count'] += 1
            raise BufferError('Live AirPlay pending audio exceeds the 2 second capacity')
        # Duplicate mono explicitly, as the bench's miniaudio decoder does.
        # FFmpeg's default mono-to-stereo rematrix would attenuate by 3 dB.
        samples = np.repeat(np.frombuffer(pcm, dtype='<i2').astype(np.int16), 2).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(samples, format='s16', layout='stereo')
        frame.sample_rate = self.INPUT_RATE
        frame.time_base = Fraction(1, self.INPUT_RATE)
        frame.pts = self._input_frames
        self._input_frames += count
        self._timestamps.append((self._input_frames, self._clock()))
        for output in self._resampler.resample(frame):
            # PyAV s16 is native-endian; the RAOP AudioSource contract is BE.
            data = output.to_ndarray().astype('>i2').tobytes()
            self._queue.append((data, self._timestamps[0][1]))
            self._queued_frames += output.samples
            self._generated_frames += output.samples
            consumed_input = self._generated_frames * self.INPUT_RATE // self.sample_rate
            while self._timestamps and self._timestamps[0][0] <= consumed_input:
                self._timestamps.popleft()
        self._update_pending()

    async def readframes(self, nframes):
        if self.closed:
            return b''
        if isinstance(nframes, bool) or not isinstance(nframes, int) or not 0 < nframes <= self._capacity:
            raise ValueError('Live AirPlay reads must request 1 to 88200 frames')
        frame_bytes = self.channels * self.sample_size
        result = bytearray(nframes * frame_bytes)
        written = 0
        now = self._clock()
        while self._queue and written < len(result):
            data, queued_at = self._queue[0]
            count = min(len(data) - self._head_offset, len(result) - written)
            result[written:written + count] = data[self._head_offset:self._head_offset + count]
            self._head_offset += count
            written += count
            self._queued_frames -= count // frame_bytes
            residence = max(0.0, (now - queued_at) * 1000)
            previous = self.telemetry['max_queue_residence_ms']
            self.telemetry['max_queue_residence_ms'] = residence if previous is None else max(previous, residence)
            if self._head_offset == len(data):
                self._queue.popleft()
                self._head_offset = 0
        self.telemetry['silence_frames'] += nframes - written // frame_bytes
        self._update_pending()
        pcm = bytes(result)
        if self._on_render is not None:
            self._on_render(pcm, now)
        self.first_read.set()
        return pcm

    async def interrupt(self):
        self.generation += 1
        self._queue.clear()
        self._timestamps.clear()
        self._head_offset = self._queued_frames = 0
        self._input_frames = self._generated_frames = 0
        self._resampler = self._new_resampler()
        self._update_pending()

    async def close(self):
        if not self.closed:
            await self.interrupt()
            self.closed = True

    async def get_metadata(self):
        return EMPTY_METADATA

    sample_rate = property(lambda self: self._sample_rate)
    channels = property(lambda self: self._channels)
    sample_size = property(lambda self: self._sample_size)
    queued_frames = property(lambda self: self._queued_frames)
    duration = property(lambda self: 0)
