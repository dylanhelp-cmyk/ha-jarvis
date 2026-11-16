import unittest

import numpy as np

from jarvis.audio import PCMFrame, RATE
from jarvis.interruption import HOP, WINDOW, InterruptionEvidence, RenderBands, calibrate_echo_bands


class InterruptionTests(unittest.TestCase):
    def test_calibrated_bounds_reject_delayed_echo_and_keep_broadband_interruption(self):
        clock = np.arange(2 * RATE) / RATE
        render = np.rint((.7 + .3 * np.sin(2 * np.pi * 5 * clock)) * sum(
            440 * np.sin(2 * np.pi * hz * clock) for hz in (220, 410, 610, 930, 1430, 2210, 3230)
        )).astype("<i2")
        delayed = np.r_[np.zeros(110), render[:-110]]
        echo = np.rint(render * .08 + delayed * .02).astype("<i2")
        ambient = np.random.default_rng(42).integers(-3, 4, RATE // 2, dtype=np.int16)
        known = np.concatenate((ambient, echo, ambient, echo))
        profile = calibrate_echo_bands(known, 0, render, (.5, 3.0))
        self.assertIsNotNone(profile)

        start = RATE
        speech = sum(95 * np.sin(2 * np.pi * hz * clock[:RATE // 5])
                     for hz in (280, 485, 735, 1090, 1650, 2510, 3420))
        generated = echo.copy()
        generated[start:start + len(speech)] += np.rint(speech).astype("<i2")

        def triggers(track):
            evidence = InterruptionEvidence(profile, render, 0)
            detected = []
            for first in range(0, len(track), HOP):
                end = first + HOP
                evidence.offer_capture(PCMFrame(first // HOP, first, first // 16, track[first:end]))
                _, sustained = evidence.observe(end, end, end)
                if sustained >= 1:
                    detected.append(end)
            return detected

        self.assertEqual(triggers(echo), [])
        self.assertIn(triggers(generated)[0], range(start + 2 * HOP, start + WINDOW + HOP + 1))

    def test_unconfirmed_render_and_inaudible_calibration(self):
        reference = np.zeros(8 * WINDOW, dtype="<i2")
        reference[2 * WINDOW:3 * WINDOW] = 500
        bands = RenderBands(reference)
        np.testing.assert_array_equal(bands.envelope(3 * WINDOW, WINDOW), 0)
        self.assertGreater(bands.envelope(3 * WINDOW, 3 * WINDOW).sum(), 0)
        silent = np.zeros(RATE, dtype="<i2")
        self.assertIsNone(calibrate_echo_bands(silent, 0, reference, (.2,)))
