import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
    def test_render_observer_runs_once_for_new_clock_information(self):
        player = JackPlayback(SimpleNamespace(ws=FakeSocket()), "observer")
        player.frames_sent = 960
        reports = []
        player.on_render_progress = lambda: reports.append((player.frames_rendered, player.first_rendered_ms))
        message = {"type": "play_clock", "playback_id": player.id, "generation": player.generation,
                   "rendered_samples": 480, "first_rendered_ms": 1000, "last_rendered_ms": 1010,
                   "max_render_discontinuity_us": 0}
        player.receive({**message, "generation": player.generation + 1})
        player.receive(message)
        player.receive(message)
        player.receive({**message, "rendered_samples": 1440})
        player.receive({**message, "type": "play_drained", "rendered_samples": 960, "last_rendered_ms": 1020})
        self.assertEqual(reports, [(480, 1000)])

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

    async def test_host_source_reserve_is_independent_of_wire_prime(self):
        ws = FakeSocket()
        ws.player = player = JackPlayback(SimpleNamespace(ws=ws), "source-reserve")

        async def chunks():
            self.assertFalse(ws.commands)
            yield bytes(480 * 4)
            for _ in range(4):
                self.assertEqual(ws.commands, ["play_start"])
                self.assertEqual(ws.packets, [])
                yield bytes(480 * 4)

        await player.send_stream(chunks(), prime_packets=2, source_prime_packets=5)
        self.assertEqual([packet[6] for packet in ws.packets], [0, 480, 960, 1440, 1920])
        self.assertEqual(ws.commands, ["play_start", "play_drain"])
        self.assertIn("source_prime_wait_ms", player.snapshot())
        self.assertIn("play_start_wait_after_prime_ms", player.snapshot())

    async def test_100_ms_playback_reserve_keeps_the_six_packet_transport_window(self):
        full = asyncio.Event()

        class Socket(FakeSocket):
            accepting = False

            async def send_bytes(self, packet):
                if self.accepting:
                    return await super().send_bytes(packet)
                self.packets.append(HEADER.unpack_from(packet))
                if len(self.packets) == 6:
                    full.set()

        ws = Socket()
        ws.player = player = JackPlayback(SimpleNamespace(ws=ws), "larger-reserve")

        async def chunks():
            for _ in range(24):
                yield bytes(480 * 4)

        task = asyncio.create_task(player.send_stream(chunks(), prime_packets=10, source_prime_packets=12))
        try:
            await asyncio.wait_for(full.wait(), .5)
            await asyncio.sleep(.01)
            self.assertEqual(len(ws.packets), 6)
            ws.accepting = True
            for packet in ws.packets[:6]:
                player.receive({"type": "play_render", "playback_id": player.id, "generation": player.generation,
                    "first_sample": packet[6], "samples": packet[3], "device_time_ms": 1000 + packet[6] // 48})
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(player.max_in_flight, 6 * 480)
        self.assertEqual([packet[6] for packet in ws.packets], [i * 480 for i in range(24)])

    async def test_reply_stall_preserves_order_and_resumes_at_normal_cadence(self):
        for recover, delay, should_drain in [(True, .25, True), (False, .25, False), (True, .6, False)]:
            with self.subTest(recover=recover, delay=delay):
                clock = 10.0
                sent = []
                ws = FakeSocket()
                ws.player = player = JackPlayback(SimpleNamespace(ws=ws), "stall")
                payloads = [bytes([value]) * (480 * 4) for value in (1, 2, 3)]

                async def sleep(seconds):
                    nonlocal clock
                    clock += seconds

                async def record(packet):
                    sent.append((clock, packet[HEADER.size:]))
                    await FakeSocket.send_bytes(ws, packet)

                async def chunks():
                    nonlocal clock
                    yield payloads[0]
                    clock += delay
                    yield b"".join(payloads[1:])

                ws.send_bytes = record
                with (patch("jarvis.playback.time", SimpleNamespace(monotonic=lambda: clock)),
                      patch("jarvis.playback.asyncio.sleep", sleep)):
                    if should_drain:
                        await player.send_stream(chunks(), recover_reply_stalls=recover)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "sender fell behind"):
                            await player.send_stream(chunks(), recover_reply_stalls=recover)

                if should_drain:
                    self.assertEqual(ws.commands, ["play_start", "play_drain"])
                    self.assertEqual([packet[5:] for packet in ws.packets], [(0, 0), (1, 480), (2, 960)])
                    self.assertEqual([payload for _, payload in sent], payloads)
                    self.assertAlmostEqual(sent[2][0] - sent[1][0], .01)
                    self.assertEqual(player.snapshot()["schedule_rebases"], 1)
                    self.assertEqual(player.snapshot()["schedule_shift_ms"], 240.0)
                else:
                    self.assertEqual(ws.commands, ["play_start"])
                    self.assertEqual(len(sent), 1)

    async def test_stall_refills_the_device_prime_then_resumes_pacing(self):
        clock = 10.0
        sent = []
        ws = FakeSocket()
        ws.player = player = JackPlayback(SimpleNamespace(ws=ws), "refill")
        payloads = [bytes([value]) * (480 * 4) for value in range(12)]

        async def sleep(seconds):
            nonlocal clock
            clock += seconds

        async def record(packet):
            sent.append((clock, packet[HEADER.size:]))
            await FakeSocket.send_bytes(ws, packet)

        async def chunks():
            nonlocal clock
            yield b"".join(payloads[:6])
            clock += .25
            yield b"".join(payloads[6:])

        ws.send_bytes = record
        with (patch("jarvis.playback.time", SimpleNamespace(monotonic=lambda: clock)),
              patch("jarvis.playback.asyncio.sleep", sleep)):
            await player.send_stream(chunks(), prime_packets=5, recover_reply_stalls=True)

        self.assertEqual([pcm for _, pcm in sent], payloads)
        self.assertEqual([packet[6] for packet in ws.packets], [480 * i for i in range(12)])
        self.assertTrue(all(abs(stamp - sent[6][0]) < 1e-8 for stamp, _ in sent[6:11]))
        self.assertAlmostEqual(sent[11][0] - sent[10][0], .01)
        self.assertEqual(player.snapshot()["schedule_rebases"], 1)
        self.assertEqual(player.snapshot()["schedule_shift_ms"], 200.0)
        self.assertEqual(ws.commands, ["play_start", "play_drain"])

    def test_device_clock_uses_exact_origin_and_rejects_conflicting_reports(self):
        ws = FakeSocket()
        player = JackPlayback(SimpleNamespace(ws=ws), "clock")
        player.frames_sent = 4_800
        report = {"type": "play_clock", "playback_id": player.id, "generation": player.generation,
                  "rendered_samples": 2_400, "first_rendered_ms": 1000, "last_rendered_ms": 1070,
                  "max_render_discontinuity_us": 20_000}
        player.receive(report)
        report.update(type="play_drained", rendered_samples=4_800, last_rendered_ms=1120)
        player.receive(report)
        self.assertEqual(player.confirmed_span(), (1000, 1120))
        self.assertEqual(player.snapshot()["max_render_discontinuity_us"], 20_000)
        player.receive({**report, "first_rendered_ms": 1001})
        self.assertIsNone(player.confirmed_span())
        self.assertFalse(player.snapshot()["render_clock_valid"])

    async def test_stop_does_not_address_a_replacement_socket(self):
        old, new = FakeSocket(), FakeSocket()
        device = SimpleNamespace(ws=old)
        player = JackPlayback(device, "trial")
        device.ws = new
        await player.stop()
        self.assertEqual(old.commands, [])
        self.assertEqual(new.commands, [])

    def test_first_dma_gap_uses_only_overlapping_packets_and_device_clock(self):
        player = JackPlayback(SimpleNamespace(ws=FakeSocket()), "gap")
        player.frames_sent = 5 * 480
        base = {"playback_id": player.id, "generation": player.generation}
        for i, stamp in enumerate([880, 890, 990, 1044, 1045]):
            player.receive({**base, "type": "play_render", "first_sample": i * 480,
                            "samples": 480, "device_time_ms": stamp})
        player.recent_sends.extend([(0, 100., 80., 80.), (480, 90., 90., 90.),
                                    (960, 11., 5., 0.), (1440, 37., 22., 15.), (1920, 1., 0., 0.)])
        report = {**base, "type": "play_clock", "first_rendered_ms": 1000,
                  "rendered_samples": 960, "last_rendered_ms": 1020, "max_render_discontinuity_us": 0}
        player.receive(report)
        self.assertNotIn("first_clock_gap", player.snapshot())
        player.receive({**report, "rendered_samples": 1920, "last_rendered_ms": 1060,
                        "max_render_discontinuity_us": 20000})
        player.receive({**report, "type": "play_drained", "rendered_samples": 2400,
                        "last_rendered_ms": 1080, "max_render_discontinuity_us": 20000})
        self.assertEqual(player.snapshot()["first_clock_gap"], {
            "first_frame": 960, "end_frame": 1920, "interval_excess_ms": 20,
            "accepted_packets_observed": 2, "least_acceptance_lead_frame": 1440,
            "least_acceptance_lead_ms": -14, "max_acceptance_interval_ms": 100,
            "max_acceptance_interval_frame": 960, "max_send_interval_ms": 37,
            "max_send_interval_frame": 1440, "max_credit_wait_ms": 22, "max_source_wait_ms": 15})

    def test_first_dma_report_with_cumulative_gap_and_expired_host_timing(self):
        player = JackPlayback(SimpleNamespace(ws=FakeSocket()), "old")
        player.frames_sent = 960
        player.accepted = [(0, 480, 995), (480, 480, 1005)]
        player.recent_sends.append((480, 10., 0., 0.))
        player.receive({"playback_id": player.id, "generation": player.generation, "type": "play_drained",
                        "rendered_samples": 960, "first_rendered_ms": 1000, "last_rendered_ms": 1040,
                        "max_render_discontinuity_us": 10000})
        gap = player.snapshot()["first_clock_gap"]
        self.assertEqual((gap["first_frame"], gap["interval_excess_ms"]), (0, 20))
        self.assertNotIn("max_send_interval_ms", gap)

    def test_stereo_conversion_preserves_exact_duration(self):
        samples = np.full(1600, 123, dtype="<i2")
        stereo = np.frombuffer(stereo_pcm(samples), dtype="<i2").reshape(-1, 2)
        self.assertEqual(len(stereo), 4800)
        np.testing.assert_array_equal(stereo[:, 0], stereo[:, 1])
