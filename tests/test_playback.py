import unittest
from types import SimpleNamespace

import numpy as np

from jarvis.playback import HEADER, JackPlayback, stereo_pcm


class FakeSocket:
    closed = False

    def __init__(self):
        self.commands = []
        self.packets = []

    async def send_json(self, message):
        kind = message["type"]
        self.commands.append(kind)
        self.player.receive({**message, "type": {
            "play_start": "play_started", "play_stop": "play_stopped",
            "play_drain": "play_drained"}[kind], "reason": "requested"})

    async def send_bytes(self, packet):
        magic, version, channels, count, generation, seq, first = HEADER.unpack_from(packet)
        self.packets.append((magic, version, channels, count, generation, seq, first))
        self.player.receive({"type": "play_render", "playback_id": self.player.id, "generation": generation,
                             "first_sample": first, "samples": count, "device_time_ms": 1000})


class JackPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_stereo_packets_and_drain(self):
        ws = FakeSocket()
        ws.player = player = JackPlayback(SimpleNamespace(ws=ws), "trial")
        await player.send(bytes(960 * 4))
        self.assertEqual(ws.commands, ["play_start", "play_drain"])
        self.assertEqual(ws.packets, [
            (b"JPLY", 1, 2, 480, player.generation, 0, 0),
            (b"JPLY", 1, 2, 480, player.generation, 1, 480)])
        self.assertEqual(player.snapshot()["frames_accepted"], 960)
        self.assertAlmostEqual(player.accepted_at(481, 5), 1.005 + 1 / 48000)

    async def test_incremental_playback_does_not_wait_for_later_audio(self):
        ws = FakeSocket()
        ws.player = player = JackPlayback(SimpleNamespace(ws=ws), "incremental")

        async def chunks():
            yield bytes(480 * 4)
            self.assertEqual(len(ws.packets), 1)
            self.assertEqual(ws.commands, ["play_start"])
            yield bytes(721 * 4)

        await player.send_stream(chunks())
        self.assertEqual([packet[3] for packet in ws.packets], [480, 480, 241])
        self.assertEqual(ws.commands, ["play_start", "play_drain"])

    async def test_playback_prime_waits_for_a_bounded_jitter_reserve(self):
        ws = FakeSocket()
        ws.player = player = JackPlayback(SimpleNamespace(ws=ws), "primed")

        async def chunks():
            yield bytes(480 * 4)
            self.assertEqual(ws.commands, [])
            yield bytes(480 * 4)
            self.assertEqual(len(ws.packets), 2)
            yield bytes(480 * 4)

        await player.send_stream(chunks(), prime_packets=2)
        self.assertEqual([packet[6] for packet in ws.packets], [0, 480, 960])

    async def test_stop_does_not_address_a_replacement_socket(self):
        old, new = FakeSocket(), FakeSocket()
        device = SimpleNamespace(ws=old)
        player = JackPlayback(device, "trial")
        device.ws = new
        await player.stop()
        self.assertEqual(old.commands, [])
        self.assertEqual(new.commands, [])

    def test_stereo_conversion_preserves_exact_duration(self):
        samples = np.full(1600, 123, dtype="<i2")
        stereo = np.frombuffer(stereo_pcm(samples), dtype="<i2").reshape(-1, 2)
        self.assertEqual(len(stereo), 4800)
        np.testing.assert_array_equal(stereo[:, 0], stereo[:, 1])
