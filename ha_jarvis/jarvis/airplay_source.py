"""Progressive bench audio for the pinned pyatv RAOP sender.

pyatv 0.18.0 has no public AudioSource injection API. Its URL opener parses
optional metadata before playback and its InternetSource reads synchronously.
Keep its complete session implementation, with a function-local source opener.
No pyatv module, class, instance or global is modified, even during cancellation.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import sys
import time
from types import FunctionType
from typing import Any

import aiohttp
import miniaudio
from pyatv.const import Protocol, __version__
from pyatv.protocols.raop import RaopStream
from pyatv.protocols.raop.audio_source import AudioSource
from pyatv.support.metadata import EMPTY_METADATA


class _HTTPReader(miniaudio.StreamableSource):
    """Decoder callbacks run in a worker; only asyncio performs network reads."""

    MAX_READ_BYTES = 65536

    def __init__(self, response, telemetry):
        self.response = response
        self.telemetry = telemetry
        self.loop = asyncio.get_running_loop()
        self.closed = False
        self.pending = None
        self.error = None

    async def _read(self, size):
        started = time.monotonic()
        self.telemetry['max_http_read_request_bytes'] = max(
            self.telemetry['max_http_read_request_bytes'], size)
        try:
            try:
                data = await self.response.content.readexactly(size)
            except asyncio.IncompleteReadError as exc:
                data = exc.partial
                self.telemetry['http_eof'] = True
            self.telemetry['http_bytes_read'] += len(data)
            return data
        except Exception as exc:
            if not self.closed:
                self.error = exc
            raise
        finally:
            elapsed = (time.monotonic() - started) * 1000
            self.telemetry['max_http_read_wait_ms'] = max(
                self.telemetry['max_http_read_wait_ms'], elapsed)

    def read(self, num_bytes):
        if self.closed:
            return b''
        if not 0 <= num_bytes <= self.MAX_READ_BYTES:
            self.error = ValueError('Decoder requested more than bounded HTTP read capacity')
            raise self.error
        self.pending = asyncio.run_coroutine_threadsafe(self._read(num_bytes), self.loop)
        # close() can race this worker between the first closed check and
        # publishing the newly scheduled read. Never leave that read waiting.
        if self.closed:
            self.pending.cancel()
        try:
            return self.pending.result()
        except concurrent.futures.CancelledError:
            if self.closed:
                return b''
            raise

    def abort(self):
        self.closed = True
        self.response.close()
        if self.pending is not None:
            self.pending.cancel()


class ProgressiveAudioSource(AudioSource):
    """One continuous miniaudio decoder, without metadata or PCM read-ahead queue.

    aiohttp flow control bounds HTTP buffering; decoder callbacks have a 64KiB
    per-read ceiling. PCM is produced only when RAOP requests the next packet.
    """

    def __init__(self, telemetry, sample_rate, channels, sample_size):
        if sample_size != 2:
            raise ValueError('Progressive AirPlay bench requires 16-bit receiver PCM')
        self.telemetry = telemetry
        self._sample_rate, self._channels = sample_rate, channels
        self._sample_size = sample_size
        self.session = None
        self.reader = None
        self.decoder = None
        self.worker = None
        self.closed = False

    async def _work(self, function):
        self.worker = asyncio.create_task(asyncio.to_thread(function))
        try:
            return await asyncio.shield(self.worker)
        except asyncio.CancelledError:
            self.telemetry['cancelled'] = True
            raise
        finally:
            if self.worker.done():
                self.worker = None

    @classmethod
    async def open(cls, url, sample_rate, channels, sample_size, telemetry, codec='wav'):
        source = cls(telemetry, sample_rate, channels, sample_size)
        started = time.monotonic()
        telemetry['session_setup_ms'] = (started - telemetry['_started']) * 1000
        telemetry['stage'] = 'opening_http_decoder'
        try:
            source.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=10),
                read_bufsize=4096)
            response = await source.session.get(url, allow_redirects=False)
            response.raise_for_status()
            source.reader = _HTTPReader(response, telemetry)
            def initialize():
                source.decoder = miniaudio.stream_any(
                    source.reader,
                    source_format=miniaudio.FileFormat.WAV if codec == 'wav' else miniaudio.FileFormat.FLAC,
                    output_format=miniaudio.SampleFormat.SIGNED16,
                    nchannels=channels, sample_rate=sample_rate, frames_to_read=352)
            await source._work(initialize)
            if source.reader.error is not None:
                raise source.reader.error
            telemetry['source_open_ms'] = (time.monotonic() - started) * 1000
            telemetry['stage'] = 'awaiting_pcm_request'
            return source
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                telemetry['cancelled'] = True
            await source.close()
            raise

    async def readframes(self, nframes):
        if self.closed:
            return b''
        started = time.monotonic()
        def read():
            samples = self.decoder.send(nframes) if self.decoder else None
            if samples is None:
                return b''
            # Match pyatv's network-byte-order PCM representation.
            if sys.byteorder == 'little':
                samples.byteswap()
            return samples.tobytes()
        # StopIteration cannot pass through an asyncio Future.
        def next_packet():
            try:
                return read()
            except StopIteration:
                return b''
        data = await self._work(next_packet)
        if self.reader.error is not None:
            raise self.reader.error
        elapsed = (time.monotonic() - started) * 1000
        self.telemetry['pcm_read_wait_total_ms'] += elapsed
        self.telemetry['max_pcm_read_wait_ms'] = max(self.telemetry['max_pcm_read_wait_ms'], elapsed)
        if data:
            ready = (time.monotonic() - self.telemetry['_started']) * 1000
            if self.telemetry['first_pcm_ready_ms'] is None:
                self.telemetry['first_pcm_ready_ms'] = ready
                self.telemetry['first_pcm_handed_to_raop_ms'] = ready
            self.telemetry['pcm_frames_handed_to_raop'] += len(data) // (self.channels * self.sample_size)
            self.telemetry['stage'] = 'handing_pcm_to_raop'
        else:
            self.telemetry['stage'] = 'pcm_eof'
        return data

    async def get_metadata(self):
        return EMPTY_METADATA

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.reader is not None:
            self.reader.abort()
        try:
            if self.worker is not None:
                await asyncio.gather(self.worker, return_exceptions=True)
                self.worker = None
            if self.decoder is not None:
                await asyncio.to_thread(self.decoder.close)
                self.decoder = None
        finally:
            if self.session is not None:
                await self.session.close()
            self.telemetry['closed'] = True

    sample_rate = property(lambda self: self._sample_rate)
    channels = property(lambda self: self._channels)
    sample_size = property(lambda self: self._sample_size)
    duration = property(lambda self: 0)  # Content duration is not inferred metadata.


def sender_telemetry():
    return {'_started': time.monotonic(), 'stage': 'session_setup',
            'session_setup_ms': None, 'source_open_ms': None,
            'first_pcm_ready_ms': None, 'first_pcm_handed_to_raop_ms': None,
            'pcm_read_wait_total_ms': 0.0, 'max_pcm_read_wait_ms': 0.0,
            'max_http_read_wait_ms': 0.0, 'http_bytes_read': 0,
            'http_reader_high_water_bytes': 8192, 'max_http_read_request_bytes': 0,
            'pcm_frames_handed_to_raop': 0, 'http_eof': False, 'closed': False,
            'cancelled': False, 'audible_onset_measured': False}


def progressive_stream_file(connection, url, telemetry, codec='wav'):
    """Return the original RAOP session coroutine with a local source override.

    The supported public metadata arguments cannot prevent eager source metadata.
    Fail closed on dependency/API drift. Exact URL matching confines this adapter
    to its generated bench stream. All upstream session logic remains unchanged.
    """
    original = RaopStream.stream_file
    expected = ['self', 'file', 'metadata', 'override_missing_metadata', 'kwargs']
    if (__version__ != '0.18.0' or list(inspect.signature(original).parameters) != expected
            or original.__module__ != 'pyatv.protocols.raop'
            or 'open_source' not in original.__code__.co_names):
        raise RuntimeError('Progressive AirPlay adapter requires the tested pyatv==0.18.0 session API')
    stream = connection.stream.get(Protocol.RAOP)
    if type(stream) is not RaopStream:
        raise RuntimeError('Progressive AirPlay adapter requires a pyatv RAOP stream instance')
    async def open_bench_source(file, sample_rate, channels, sample_size):
        if file != url or not isinstance(file, str):
            raise ValueError('Progressive AirPlay source URL does not match this session')
        return await ProgressiveAudioSource.open(
            file, sample_rate, channels, sample_size, telemetry, codec)
    namespace = dict(original.__globals__, open_source=open_bench_source)
    local = FunctionType(original.__code__, namespace, original.__name__,
                         original.__defaults__, original.__closure__)
    local.__kwdefaults__ = original.__kwdefaults__
    return local(stream, url, metadata=EMPTY_METADATA)
