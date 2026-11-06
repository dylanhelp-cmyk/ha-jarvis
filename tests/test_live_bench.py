import unittest
from unittest.mock import patch

import numpy as np

from jarvis.audio import PCMFrame, RATE, make_probe
from jarvis.live_bench import match_response, render_reference, voiced_bounds


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
