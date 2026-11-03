"""Paced HTTP -> continuous decoder -> real encrypted RAOP packets, offline."""
import asyncio
from contextlib import suppress
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
import miniaudio
import numpy as np
from pyatv.protocols.raop import RaopStream
from pyatv.protocols.raop.protocols import StreamContext
from pyatv.protocols.raop.protocols.airplayv2 import AirPlayV2
from pyatv.protocols.raop.packets import SyncPacket
from pyatv.protocols.raop.stream_client import ControlClient, StreamClient
from pyatv.settings import Settings
from pyatv.support.chacha20 import Chacha20Cipher8byteNonce

from jarvis.airplay_source import ProgressiveAudioSource, progressive_stream_file, sender_telemetry
from jarvis.audio import RATE, detect_probe, make_probe, wav_header
from jarvis.server import Stream, create_audio


class PacketCollector:
    def __init__(self):
        self.packets = []
        self.first_audio_at = None
        self.decryptor = Chacha20Cipher8byteNonce(bytes(range(32)), bytes(range(32)))

    def is_closing(self):
        return False

    def sendto(self, packet):
        data = self.decryptor.decrypt(packet[12:-8], nonce=packet[-8:], aad=packet[4:12])
        if self.first_audio_at is None and any(data):
            self.first_audio_at = time.monotonic()
        self.packets.append(packet)


class AirPlayDecodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_paced_http_reaches_encrypted_packets_before_eof_with_all_probes(self):
        stream = Stream('latency', 3)
        bench = SimpleNamespace(stream=stream, config={'route': 'airplay', 'codec': 'wav'})
        runner = web.AppRunner(create_audio(bench))
        source = None
        telemetry = sender_telemetry()
        ticks = []
        async def heartbeat():
            previous = time.monotonic()
            while True:
                await asyncio.sleep(.01)
                now = time.monotonic()
                ticks.append(now - previous)
                previous = now
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        pulse = asyncio.create_task(heartbeat())
        try:
            source = await asyncio.wait_for(ProgressiveAudioSource.open(
                f'http://127.0.0.1:{port}/stream/{stream.id}.wav', 44100, 2, 2, telemetry), 3)
            self.assertFalse(stream.done.is_set(), telemetry)
            context = StreamContext()
            context.reset()
            rtsp = SimpleNamespace(session_id=123)
            protocol = AirPlayV2(context, rtsp)
            key = bytes(range(32))
            protocol._cipher = Chacha20Cipher8byteNonce(key, key)
            client = StreamClient(rtsp, context, protocol, Settings())
            collector = PacketCollector()
            # Both producer deadlines and production sender pacing are real.
            await asyncio.wait_for(client._stream_data(source, collector), 18)
            self.assertEqual(stream.bytes_sent, 12 * RATE * 2)
            self.assertIsNone(stream.error)
            self.assertLess(collector.first_audio_at - stream.started_at, 2, telemetry)
            self.assertLess(telemetry['first_pcm_ready_ms'], 2000, telemetry)
            self.assertGreater(telemetry['http_bytes_read'], 0)
            self.assertLessEqual(telemetry['max_http_read_request_bytes'], 4096, telemetry)
            self.assertLess(max(ticks), .3, ticks)
            self.assertLess(sum(tick > .1 for tick in ticks), 3)
            self.assertLess(stream.schedule_lag_ms, 300)
            decryptor = Chacha20Cipher8byteNonce(key, key)
            payload = b''.join(decryptor.decrypt(packet[12:-8],
                nonce=packet[-8:], aad=packet[4:12]) for packet in collector.packets)
            pcm = np.frombuffer(payload, dtype='>i2').reshape(-1, 2)
            self.assertTrue(np.array_equal(pcm[:, 0], pcm[:, 1]))
            self.assertGreaterEqual(len(pcm), 12 * 44100)
            self.assertLess(len(pcm), 13.6 * 44100)
            self.assertGreater(np.max(np.abs(pcm.astype(int))), 3800)
            self.assertLess(np.max(np.abs(pcm.astype(int))), 4100)
            mono = np.frombuffer(miniaudio.convert_frames(
                miniaudio.SampleFormat.SIGNED16, 2, 44100, pcm.astype('<i2').tobytes(),
                miniaudio.SampleFormat.SIGNED16, 1, RATE), dtype='<i2')
            for index in range(3):
                match = detect_probe(mono, make_probe(513 + index))
                self.assertEqual(match['status'], 'detected', match)
                self.assertGreater(match['confidence'], .99)
                self.assertAlmostEqual(match['offset_seconds'], .1 + index * 2, delta=.008)
        finally:
            if source is not None:
                await source.close()
            pulse.cancel()
            with suppress(asyncio.CancelledError):
                await pulse
            await runner.cleanup()

    async def test_cancel_pending_decoder_open_or_read_releases_worker_promptly(self):
        for during_open in (True, False):
            connected, release = asyncio.Event(), asyncio.Event()
            async def stalled(request):
                response = web.StreamResponse(headers={'Content-Type': 'audio/wav'})
                await response.prepare(request)
                await response.write(wav_header(data_bytes=320000))
                if not during_open:
                    await response.write(bytes(32000))
                connected.set()
                await release.wait()
                return response
            app = web.Application()
            app.router.add_get('/test.wav', stalled)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            telemetry = sender_telemetry()
            source = None
            async def consume():
                nonlocal source
                try:
                    source = await ProgressiveAudioSource.open(
                        f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/test.wav',
                        44100, 2, 2, telemetry)
                    while await source.readframes(352):
                        pass
                finally:
                    if source is not None:
                        await source.close()
            task = asyncio.create_task(consume())
            try:
                await connected.wait()
                await asyncio.sleep(.1)
                started = time.monotonic()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, .5)
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(telemetry['closed'])
                self.assertTrue(telemetry['cancelled'])
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await runner.cleanup()


class AirPlaySessionAdapterTests(unittest.IsolatedAsyncioTestCase):
    def connection(self, initialize=None):
        context = StreamContext()
        manager = SimpleNamespace(acquire=Mock(), context=context,
            setup=AsyncMock(return_value=(SimpleNamespace(initialize=initialize or AsyncMock()), context)),
            teardown=AsyncMock())
        release = Mock()
        core = SimpleNamespace(takeover=Mock(return_value=release),
            service=SimpleNamespace(credentials=None, password=None, properties={}))
        stream = RaopStream(core, None, None, manager)
        connection = SimpleNamespace(stream=SimpleNamespace(get=lambda protocol: stream))
        return connection, stream, manager, release

    async def test_failure_keeps_upstream_acquire_release_and_teardown_and_globals(self):
        from pyatv.protocols import raop
        original_open = raop.open_source
        connection, stream, manager, release = self.connection(
            AsyncMock(side_effect=RuntimeError('setup failed')))
        with self.assertRaisesRegex(RuntimeError, 'setup failed'):
            await progressive_stream_file(connection, 'http://127.0.0.1/test.wav', sender_telemetry())
        manager.acquire.assert_called_once()
        manager.teardown.assert_awaited_once()
        release.assert_called_once()
        self.assertIs(raop.open_source, original_open)
        self.assertNotIn('stream_file', vars(stream))
        self.assertNotIn('reset', vars(manager.context))

    async def test_cancel_keeps_upstream_release_teardown_and_globals(self):
        from pyatv.protocols import raop
        original_open = raop.open_source
        initialized = asyncio.Event()
        async def initialize(properties):
            initialized.set()
            await asyncio.Event().wait()
        connection, stream, manager, release = self.connection(initialize)
        task = asyncio.create_task(progressive_stream_file(
            connection, 'http://127.0.0.1/test.wav', sender_telemetry()))
        await initialized.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        manager.teardown.assert_awaited_once()
        release.assert_called_once()
        self.assertIs(raop.open_source, original_open)
        self.assertNotIn('reset', vars(manager.context))

    async def test_latency_controls_real_sender_sync_and_restores_context(self):
        from pyatv.protocols import raop
        original_open = raop.open_source
        original_class_reset = StreamContext.reset
        for latency_ms in (1500, 500):
            connection, stream, manager, release = self.connection()
            context = manager.context
            original_latency = context.latency
            audio = SimpleNamespace(has_changed_volume=True, volume=47, set_volume=AsyncMock())
            stream.audio = audio
            rtsp = SimpleNamespace(session_id=123, connection=SimpleNamespace(remote_ip='127.0.0.1'),
                record=AsyncMock(), flush=AsyncMock(), teardown=AsyncMock())
            protocol = AirPlayV2(context, rtsp)
            key = bytes(range(32))
            protocol._cipher = Chacha20Cipher8byteNonce(key, key)
            protocol.start_feedback = AsyncMock()
            client = StreamClient(rtsp, context, protocol, Settings())
            client.initialize = AsyncMock()
            client.control_client = ControlClient(context, client._packet_backlog)
            sync_transport = SimpleNamespace(sendto=Mock(), close=Mock())
            client.control_client.connection_made(sync_transport)
            client.timing_server = SimpleNamespace(close=Mock())
            collector = PacketCollector()
            collector.close = Mock()
            client.loop = SimpleNamespace(create_datagram_endpoint=AsyncMock(return_value=(collector, None)))
            manager.setup = AsyncMock(return_value=(client, context))
            # Exercise real send_audio/reset, real pacing, encrypted audio and sync.
            # Only the HTTP source and network sockets are replaced.
            remaining = 2
            async def readframes(count):
                nonlocal remaining
                if remaining:
                    remaining -= 1
                    return b'\x00\x01' * (count * 2)
                return b''
            source = SimpleNamespace(duration=0, readframes=readframes, close=AsyncMock())
            telemetry = sender_telemetry()
            with patch('jarvis.airplay_source.ProgressiveAudioSource.open', AsyncMock(return_value=source)):
                options = {} if latency_ms == 1500 else {'latency_ms': latency_ms}
                await progressive_stream_file(connection, 'http://127.0.0.1/test.wav', telemetry,
                                              **options)
            sync_transport.sendto.assert_called()
            packet = SyncPacket.decode(sync_transport.sendto.call_args_list[0].args[0])
            expected_samples = 44100 * latency_ms // 1000
            self.assertEqual(packet.now - packet.now_without_latency, expected_samples)
            self.assertEqual(telemetry['requested_airplay_latency_ms'], latency_ms)
            self.assertEqual(telemetry['selected_airplay_latency_samples'], expected_samples)
            self.assertEqual(telemetry['selected_airplay_sample_rate_hz'], 44100)
            self.assertGreater(len(collector.packets), 2)
            self.assertNotIn('reset', vars(context))
            self.assertIs(StreamContext.reset, original_class_reset)
            self.assertIs(raop.open_source, original_open)
            self.assertEqual(context.latency, original_latency)
            context.reset()
            self.assertEqual(context.latency, 66150)
            source.close.assert_awaited_once()
            manager.teardown.assert_awaited_once()
            release.assert_called_once()

    async def test_existing_context_reset_is_restored_after_cancel_or_error(self):
        for cancelled in (False, True):
            entered = asyncio.Event()
            connection, stream, manager, release = self.connection()
            context = manager.context
            original_reset = Mock(wraps=context.reset)
            context.reset = original_reset
            async def initialize(properties):
                context.sample_rate = 48000
                context.reset()
                self.assertEqual(context.latency, 24000)
                entered.set()
                if cancelled:
                    await asyncio.Event().wait()
                raise RuntimeError('after context reset')
            manager.setup.return_value[0].initialize = initialize
            telemetry = sender_telemetry()
            task = asyncio.create_task(progressive_stream_file(
                connection, 'http://127.0.0.1/test.wav', telemetry, latency_ms=500))
            await entered.wait()
            if cancelled:
                task.cancel()
            with self.assertRaises(asyncio.CancelledError if cancelled else RuntimeError):
                await task
            self.assertIs(context.reset, original_reset)
            self.assertEqual(telemetry['selected_airplay_latency_samples'], 24000)
            self.assertEqual(telemetry['selected_airplay_sample_rate_hz'], 48000)

    async def test_invalid_latency_fails_before_session_acquire(self):
        connection, stream, manager, release = self.connection()
        for latency_ms in (249, 2001, True, 500.5, '500'):
            with self.assertRaisesRegex(ValueError, '250 to 2000'):
                progressive_stream_file(connection, 'http://127.0.0.1/test.wav',
                                        sender_telemetry(), latency_ms=latency_ms)
        manager.acquire.assert_not_called()

    async def test_version_drift_fails_before_session_acquire(self):
        connection, stream, manager, release = self.connection()
        with patch('jarvis.airplay_source.__version__', '0.19.0'):
            with self.assertRaisesRegex(RuntimeError, 'tested pyatv'):
                progressive_stream_file(connection, 'http://127.0.0.1/test.wav', sender_telemetry())
        manager.acquire.assert_not_called()
