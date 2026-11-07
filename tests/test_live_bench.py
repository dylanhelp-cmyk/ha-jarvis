import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from jarvis.audio import PCMFrame, RATE, make_probe
from jarvis.live_bench import BenchReply, match_response, render_reference, voiced_bounds


class ReplyOnsetTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_reply_attenuation_reaches_the_render_reference(self):
        source = (3000 * np.sin(2 * np.pi * 440 * np.arange(1600) / RATE)).astype("<i2").tobytes()
        pcm = []
        for gain in (0, -6):
            reply = BenchReply(SimpleNamespace(ws=None), "test", output_gain_db=gain)
            await reply.receive(source, RATE)
            await reply.finish()
            pcm.append(np.frombuffer(reply.pcm, dtype="<i2"))
        self.assertEqual(len(pcm[0]), len(pcm[1]))
        expected = np.rint(pcm[0] * 10 ** (-6 / 20))
        np.testing.assert_array_equal(pcm[1], expected)

    async def test_retains_quiet_lead_across_packet_boundary(self):
        reply = BenchReply(SimpleNamespace(ws=None), "test")
        quiet = np.arange(960, dtype="<i2") % 40 + 20
        strong = np.full(320, 1200, dtype="<i2")
        with patch.object(reply, "_render") as render:
            await reply.receive(quiet.tobytes(), RATE)
            self.assertIsNone(reply.first_voice_at)
            render.assert_not_called()
            await reply.receive(strong.tobytes(), RATE)
            frame, = render.call_args.args
        np.testing.assert_array_equal(frame.to_ndarray().ravel(), np.concatenate((quiet[-640:], strong)))
        self.assertEqual(reply.first_voice_sample, 640)


class ReplyMatchingTests(unittest.TestCase):
    def test_generated_reference_matches_acoustic_copy_with_window_offset_compensation(self):
        reply = np.concatenate((np.zeros(1600, dtype="<i2"), make_probe(513), make_probe(514)))
        captured = np.zeros(6 * RATE, dtype="<i2")
        offset = int(2.1 * RATE)
        captured[offset:offset + len(reply)] = (reply * .35).astype("<i2")
        result = match_response(captured, 100, reply, 1600, 101, 101.2)
        self.assertEqual(result["status"], "detected", result)
        self.assertAlmostEqual(result["fixture_end_to_audible_ms"], 1200, delta=12)
        self.assertGreaterEqual(sum(window["status"] == "detected" for window in result["windows"]), 2)

    def test_silence_and_short_reply_cannot_be_reported_as_zero_latency(self):
        capture = np.zeros(4 * RATE, dtype="<i2")
        for reply in (np.zeros(RATE, dtype="<i2"), make_probe(513)[:4000]):
            result = match_response(capture, 100, reply, 0, 101, 101.2)
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["latency_ms"])

    def test_handoff_bounds_exclude_a_later_copy_without_relaxing_agreement(self):
        reply = np.concatenate((make_probe(513), make_probe(514)))
        captured = np.zeros(8 * RATE, dtype="<i2")
        first = int(2.1 * RATE)
        later = 5 * RATE
        captured[first:first + len(reply)] = (reply * .35).astype("<i2")
        captured[later:later + len(reply)] = reply
        result = match_response(captured, 100, reply, 0, 101, 101.2,
                                handoff_at=lambda offset: 101.55 + offset / RATE,
                                acoustic_delay_range=(.25, .85))
        self.assertEqual(result["status"], "detected", result)
        self.assertAlmostEqual(result["latency_ms"], 1100, delta=12)

    def test_contradictory_window_locations_are_rejected(self):
        reply = np.concatenate((make_probe(513), make_probe(514)))
        fake = [{"status": "detected", "offset_seconds": value, "confidence": .99}
                for value in (1, 2, 3, 4)]
        with patch("jarvis.live_bench.detect_probe", side_effect=fake):
            result = match_response(np.zeros(8 * RATE), 100, reply, 0, 101, 101.1)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("contradict", result["reason"])

    def test_voice_onset_ignores_louder_late_copy_in_the_same_capture(self):
        reply = make_probe(513)[:int(.6 * RATE)]
        capture = np.zeros(2 * RATE, dtype="<i2")
        first, late = int(.22 * RATE), int(.91 * RATE)
        capture[first:first + len(reply)] = (reply * .3).astype("<i2")
        capture[late:late + len(reply)] = reply
        result = match_response(capture, 100, reply, 0, 100, 100,
                                handoff_at=lambda offset: 100.1 + offset / RATE,
                                acoustic_delay_range=(-.04, .45), window_seconds=.14, onset_horizon_seconds=.28)
        self.assertEqual(result["status"], "detected", result)
        self.assertAlmostEqual(result["latency_ms"], 220, delta=12)

    def test_known_clip_alignment_can_flag_one_late_window_without_relaxing_reply_timing(self):
        reply = np.full(RATE, 1200, dtype="<i2")
        starts = np.linspace(0, RATE - int(.14 * RATE), 4, dtype=int)
        fake = [{"status": "detected", "confidence": .95,
                 "offset_seconds": 1 + offset / RATE + (.047 if i == 3 else 0)}
                for i, offset in enumerate(starts)]
        with patch("jarvis.live_bench.detect_probe", side_effect=fake):
            strict = match_response(np.zeros(8 * RATE), 100, reply, 0, 101, 101.1, window_seconds=.14)
        self.assertEqual(strict["status"], "unavailable")
        with patch("jarvis.live_bench.detect_probe", side_effect=fake):
            calibrated = match_response(np.zeros(8 * RATE), 100, reply, 0, 101, 101.1,
                                        window_seconds=.14, allow_late_outlier=True)
        self.assertEqual(calibrated["status"], "detected", calibrated)
        self.assertTrue(calibrated["windows"][-1]["late_outlier"])

    def test_known_fixture_energy_trims_head_and_tail(self):
        samples = np.concatenate((np.zeros(320), np.full(640, 2000), np.zeros(480))).astype("<i2")
        self.assertEqual(voiced_bounds(samples), (320, 960))

    def test_render_reference_preserves_silence_and_packet_sample_clock(self):
        stereo = np.zeros((48000, 2), dtype="<i2")
        stereo[12000:24000] = 3000
        reference = render_reference(stereo.tobytes())
        self.assertEqual(len(reference), RATE)
        start, end = voiced_bounds(reference)
        self.assertAlmostEqual(start, 4000, delta=160)
        self.assertAlmostEqual(end, 8000, delta=160)
