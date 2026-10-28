"""Exercise the HTTP WAV -> decoder -> encrypted RAOP payload without hardware."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web
import miniaudio
import numpy as np
from pyatv.protocols.raop.audio_source import open_source
from pyatv.protocols.raop.protocols import StreamContext
from pyatv.protocols.raop.protocols.airplayv2 import AirPlayV2
from pyatv.protocols.raop.stream_client import StreamClient
from pyatv.settings import Settings
from pyatv.support.chacha20 import Chacha20Cipher8byteNonce

from jarvis.audio import RATE, detect_probe, make_probe
from jarvis.server import Stream, create_audio


class PacketCollector:
    def __init__(self):
        self.packets = []

    def is_closing(self):
        return False

    def sendto(self, packet):
        self.packets.append(packet)


class AirPlayDecodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_airplay_http_wav_reaches_encrypted_packets_with_all_three_probes(self):
        stream = Stream('latency', 3)
        bench = SimpleNamespace(stream=stream, config={'route': 'airplay', 'codec': 'wav'})
        runner = web.AppRunner(create_audio(bench))
        source = None
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        # Retain the actual generator and all 12 seconds of PCM, but make its
        # deadlines expire immediately. Only jarvis.server's clock is replaced.
        clock_calls = iter([0.0])
        clock = SimpleNamespace(monotonic=lambda: next(clock_calls, 100.0))
        try:
            # TinyTag attempts to seek beyond pyatv's protected HTTP headroom.
            # Bound its optional metadata timeout; retain real PCM decoding.
            with patch('jarvis.server.time', clock), \
                    patch('pyatv.protocols.raop.audio_source.DEFAULT_TIMEOUT', 1.0):
                source = await asyncio.wait_for(open_source(
                    f'http://127.0.0.1:{port}/stream/{stream.id}.wav', 44100, 2, 2), 10)
                context = StreamContext()
                context.reset()
                rtsp = SimpleNamespace(session_id=123)
                protocol = AirPlayV2(context, rtsp)
                key = bytes(range(32))
                protocol._cipher = Chacha20Cipher8byteNonce(key, key)
                client = StreamClient(rtsp, context, protocol, Settings())
                collector = PacketCollector()
                # Use production framing, final-packet padding, and encryption.
                # Skip only the sender's wall-clock pacing and stop after padding.
                async def collect():
                    for _ in range(2000):
                        if not await client._send_packet(source, not collector.packets, collector):
                            return
                    self.fail('RAOP stream did not reach EOF within 2000 packets')
                # InternetSource.readframes is synchronous internally. Keep the
                # producer's HTTP loop running while draining faster than realtime.
                await asyncio.wait_for(asyncio.to_thread(lambda: asyncio.run(collect())), 10)
            self.assertEqual(stream.bytes_sent, 12 * RATE * 2)
            self.assertIsNone(stream.error)
            decryptor = Chacha20Cipher8byteNonce(key, key)
            payload = b''.join(decryptor.decrypt(packet[12:-8],
                nonce=packet[-8:], aad=packet[4:12]) for packet in collector.packets)
            # pyatv serializes signed 16-bit PCM in network byte order.
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
            await runner.cleanup()
