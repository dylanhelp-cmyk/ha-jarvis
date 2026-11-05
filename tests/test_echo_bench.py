"""Real AEC3 processing of synthetic room echo; no hardware acceptance claims."""
import importlib.util
import unittest
from unittest.mock import patch

import numpy as np

from jarvis.echo_bench import SCOPE, measure_echo_reduction


class EchoBenchTests(unittest.TestCase):
    def fixture(self, noise=8):
        rate = 16000
        rng = np.random.default_rng(73)
        t = np.arange(rate * 10) / rate
        rendered = np.zeros(rate * 10, dtype='<i2')
        rendered[rate * 3:-rate] = ((np.sin(2 * np.pi * 937 * t[rate * 3:-rate]) +
            np.sin(2 * np.pi * 1579 * t[rate * 3:-rate])) * 900).astype('<i2')
        # One second capture pre-roll, 600 ms direct echo, plus room reflections.
        capture = rng.normal(0, noise, rate * 13)
        for delay, gain in ((25600, .55), (25927, .18), (26751, .1)):
            capture[delay:delay + len(rendered)] += rendered.astype(float) * gain
        return capture.astype('<i2'), rendered

    @unittest.skipUnless(importlib.util.find_spec('pywebrtc_audio'), 'pywebrtc-audio wheel not installed')
    def test_native_room_echo_reports_finite_noise_bounded_band_reduction(self):
        capture, render = self.fixture()
        result = measure_echo_reduction(capture, 100, render, 101, 600)
        self.assertEqual(result['status'], 'measured', result)
        self.assertGreater(result['pilot_band_reduction_db'], 8, result)
        self.assertTrue(np.isfinite(result['pilot_band_reduction_db']))
        self.assertLessEqual(result['pilot_band_reduction_db'], result['raw_pilot_snr_db'] + .01)
        self.assertEqual(result['delay_assumed_ms'], 600)
        self.assertEqual(result['scope'], SCOPE)
        self.assertGreater(result['max_processor_call_ms'], 0)
        self.assertTrue(all(not isinstance(v, (np.ndarray, bytes)) for v in result.values()))

    def test_low_snr_is_unavailable_instead_of_infinite_reduction(self):
        capture, render = self.fixture(noise=12000)
        # Avoid the clipping guard so this exercises the pilot/noise evidence.
        capture = np.clip(capture.astype(float), -20000, 20000).astype('<i2')
        result = measure_echo_reduction(capture, 100, render, 101, 600)
        self.assertEqual(result['status'], 'unavailable', result)
        self.assertIn('too weak', result['reason'])
        self.assertIsNone(result['pilot_band_reduction_db'])

    def test_short_capture_cannot_report_partial_final_pilot(self):
        capture, render = self.fixture()
        result = measure_echo_reduction(capture[:7 * 16000], 100, render, 101, 600)
        self.assertEqual(result['status'], 'unavailable', result)
        self.assertIn('full final pilot', result['reason'])

    def test_missing_native_backend_returns_unavailable(self):
        capture, render = self.fixture()
        with patch('jarvis.echo_bench.HostAEC3', side_effect=RuntimeError('dependency absent')):
            result = measure_echo_reduction(capture, 100, render, 101, 600)
        self.assertEqual(result['status'], 'unavailable', result)
        self.assertIn('backend is unavailable', result['reason'])

    def test_no_quiet_preroll_is_unavailable(self):
        capture, render = self.fixture()
        result = measure_echo_reduction(capture, 102, render, 101, 600)
        self.assertEqual(result['status'], 'unavailable', result)
        self.assertIn('quiet pre-roll', result['reason'])

    def test_missing_reference_pilot_and_invalid_delay_are_unavailable(self):
        capture, render = self.fixture()
        for delay in (None, float('nan'), -1, True):
            self.assertEqual(measure_echo_reduction(capture, 100, render, 101, delay)['status'], 'unavailable')
        result = measure_echo_reduction(capture, 100, np.zeros_like(render), 101, 600)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('both pilot bands', result['reason'])
