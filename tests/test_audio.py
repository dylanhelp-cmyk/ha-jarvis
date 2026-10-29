import struct
import unittest

import numpy as np

from jarvis.audio import RATE, FRAME, MAX_PLAYBACK_DELAY_SECONDS, capture_health, decode_frame, make_probe, detect_probe, detect_stop, detect_stability, summarize


class AudioTests(unittest.TestCase):
    def test_binary_contract_and_bad_lengths(self):
        packet = FRAME.pack(b"JPCM", 1, 1, 3, 8, 1234, 2300) + struct.pack("<hhh", -1, 0, 200)
        frame = decode_frame(packet)
        self.assertEqual(frame.first_sample, 1234)
        self.assertEqual(frame.samples.tolist(), [-1, 0, 200])
        for bad in [packet[:-1], b"x", packet.replace(b"JPCM", b"NOPE")]:
            with self.assertRaises(ValueError): decode_frame(bad)

    def test_acoustic_delay_with_phase_and_gain_change(self):
        probe = make_probe(17)
        captured = np.zeros(RATE * 3, dtype="<i2")
        # Inverting phase prevents a naive positive-waveform-only match.
        captured[8000:8000 + len(probe)] = -probe // 3
        noise = np.random.default_rng(2).normal(0, 6, len(captured)).astype("<i2")
        detected = detect_probe(captured + noise, probe)
        self.assertEqual(detected["status"], "detected", detected)
        self.assertAlmostEqual(detected["offset_seconds"], .5, delta=.012)

    def test_no_signal_is_unavailable_not_zero(self):
        result = detect_probe(np.zeros(RATE * 2, dtype="<i2"), make_probe())
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("offset_seconds", result)

    def test_capture_health_distinguishes_silence_low_level_and_clipping(self):
        silent = capture_health(np.zeros(RATE, dtype="<i2"))
        self.assertEqual(silent["signal"], "digital_silence")
        self.assertIsNone(silent["rms_dbfs"])
        quiet = capture_health(np.full(RATE, 10, dtype="<i2"))
        self.assertEqual(quiet["signal"], "nonzero")
        self.assertLess(quiet["rms_dbfs"], -60)
        clipped = capture_health(np.full(RATE, -32768, dtype="<i2"))
        self.assertEqual(clipped["clipped_fraction"], 1)
        self.assertEqual(clipped["peak_dbfs"], 0)

    def test_heavily_buffered_probe_detected_without_accepting_noise(self):
        probe = make_probe(513)
        captured = np.random.default_rng(7).normal(0, 5, RATE * 17).astype("<i2")
        self.assertEqual(detect_probe(captured, probe)["status"], "unavailable")
        captured[RATE * 12:RATE * 12 + len(probe)] += probe // 2
        found = detect_probe(captured, probe)
        self.assertEqual(found["status"], "detected", found)
        self.assertAlmostEqual(found["offset_seconds"], 12, delta=.012)

    def test_repeated_ambiguous_probe_is_rejected(self):
        probe = make_probe()
        captured = np.zeros(RATE * 4, dtype="<i2")
        captured[4000:4000 + len(probe)] = probe
        captured[32000:32000 + len(probe)] = probe
        self.assertEqual(detect_probe(captured, probe)["status"], "unavailable")

    def test_clipping_is_rejected(self):
        self.assertEqual(detect_probe(np.full(RATE * 2, 32767, dtype="<i2"), make_probe())["status"], "unavailable")

    def test_stop_is_acoustic_not_command_ack(self):
        t = np.arange(RATE * 5) / RATE
        pilot = ((np.sin(2*np.pi*937*t) + np.sin(2*np.pi*1579*t)) * 1700).astype("<i2")
        capture = np.zeros_like(pilot)
        capture[RATE: int(RATE * 3.2)] = pilot[RATE:int(RATE * 3.2)]
        result = detect_stop(capture, 100.0, 103.0)
        self.assertEqual(result["status"], "detected", result)
        self.assertAlmostEqual(result["latency_ms"], 200, delta=35)

    def test_still_playing_is_not_a_successful_stop(self):
        t = np.arange(RATE * 5) / RATE
        capture = ((np.sin(2*np.pi*937*t) + np.sin(2*np.pi*1579*t)) * 1700).astype("<i2")
        capture[:RATE] = 0
        self.assertEqual(detect_stop(capture, 100, 103)["status"], "unavailable")

    def test_invalid_trials_are_excluded_from_percentiles(self):
        self.assertIsNone(summarize([{"status": "unavailable", "latency_ms": None}])["median_ms"])
        result = summarize([{"status":"detected", "latency_ms":400}, {"status":"unavailable", "latency_ms":0}])
        self.assertEqual(result["median_ms"], 400)

    def test_overlapping_trial_windows_keep_unique_probe_pairings(self):
        probes = [make_probe(i + 513) for i in range(3)]
        for delay in (.1, .5, 1, 2, 3, 6, 12, 15):
            captured = np.zeros(RATE * 24, dtype="<i2")
            for i, probe in enumerate(probes):
                offset = int((.1 + i * 2 + delay) * RATE)
                captured[offset:offset + len(probe)] = probe
            for i, probe in enumerate(probes):
                start = max(0, int((.1 + i * 2 - .1) * RATE))
                end = int((.1 + i * 2 + MAX_PLAYBACK_DELAY_SECONDS + len(probe) / RATE) * RATE)
                match = detect_probe(captured[start:end], probe)
                self.assertEqual(match["status"], "detected", (delay, i, match))
                self.assertAlmostEqual(start / RATE + match["offset_seconds"] - (.1 + i * 2), delay, delta=.012)

    def test_continuous_pilot_distinguishes_gap_from_stable_playback(self):
        t = np.arange(RATE * 10) / RATE
        captured = ((np.sin(2*np.pi*937*t) + np.sin(2*np.pi*1579*t)) * 1000).astype("<i2")
        captured[:RATE * 2] = 0
        captured[RATE * 8:] = 0
        good = detect_stability(captured, 0, 1, 8, 0, [])
        self.assertEqual(good["dropout_count"], 0)
        captured[RATE*5:RATE*6] = 0
        bad = detect_stability(captured, 0, 1, 8, 0, [])
        self.assertEqual(bad["dropout_count"], 1)
        self.assertGreater(bad["dropout_ms"], 900)

    def test_clean_low_level_pilot_does_not_require_high_absolute_volume(self):
        t = np.arange(RATE * 10) / RATE
        for amplitude in (1000, 50, 3):
            captured = ((np.sin(2*np.pi*937*t) + np.sin(2*np.pi*1579*t)) * amplitude).astype('<i2')
            captured[:RATE * 2] = 0
            captured[RATE * 8:] = 0
            result = detect_stability(captured, 0, 1, 8, 0, [])
            self.assertEqual(result['status'], 'measured', result)
            self.assertEqual(result['dropout_count'], 0)

    def test_weak_pilot_is_unavailable_instead_of_false_dropout_counts(self):
        captured = np.random.default_rng(5).normal(0, 20, RATE * 10).astype('<i2')
        t = np.arange(RATE * 6) / RATE
        captured[RATE*2:RATE*8] += (np.sin(2*np.pi*937*t) + np.sin(2*np.pi*1579*t)).astype('<i2')
        result = detect_stability(captured, 0, 1, 8, 0, [])
        self.assertEqual(result['status'], 'unavailable', result)
        self.assertNotIn('dropout_count', result)

    def test_interrupted_reference_cannot_certify_continuity(self):
        t = np.arange(RATE * 10) / RATE
        captured = ((np.sin(2*np.pi*937*t) + np.sin(2*np.pi*1579*t)) * 1000).astype('<i2')
        captured[:RATE*2] = 0
        captured[int(RATE*2.2):int(RATE*2.7)] = 0
        result = detect_stability(captured, 0, 1, 8, 0, [])
        self.assertEqual(result['status'], 'unavailable', result)
        self.assertNotIn('dropout_count', result)


if __name__ == "__main__": unittest.main()
