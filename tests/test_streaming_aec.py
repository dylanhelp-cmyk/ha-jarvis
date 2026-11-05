import unittest

import numpy as np

from jarvis.audio import PCMFrame, RATE
from jarvis.streaming_aec import StreamingEcho


class ReferenceRecorder:
    def __init__(self):
        self.references = []

    def process(self, capture, render):
        self.references.append(np.frombuffer(render, dtype="<i2"))
        return capture


class StreamingEchoTests(unittest.TestCase):
    def test_render_delay_and_partial_capture_frames_preserve_samples(self):
        recorder = ReferenceRecorder()
        echo = StreamingEcho(500, processor=recorder)
        pcm = np.full((44100, 2), 2345, dtype=">i2")
        for start in range(0, len(pcm), 352):
            echo.render(pcm[start:start + 352].tobytes(), 10 + start / 44100)
        captured = np.arange(512, dtype="<i2")
        first = echo.capture(PCMFrame(0, 0, 10600, captured[:256]), 0)
        second = echo.capture(PCMFrame(1, 256, 10616, captured[256:]), 0)
        self.assertEqual(first + second, captured[:480].tobytes())
        self.assertEqual(len(recorder.references), 3)
        np.testing.assert_array_equal(recorder.references[0], np.full(160, 2345))

    def test_before_render_and_discontinuous_capture(self):
        recorder = ReferenceRecorder()
        echo = StreamingEcho(500, processor=recorder)
        samples = np.zeros(160, dtype="<i2")
        echo.capture(PCMFrame(0, 0, 10000, samples), 0)
        self.assertFalse(np.any(recorder.references[0]))
        with self.assertRaisesRegex(RuntimeError, "discontinuity"):
            echo.capture(PCMFrame(1, 320, 10020, samples), 0)

    def test_clock_drift_updates_reference_without_resampling_capture(self):
        recorder = ReferenceRecorder()
        echo = StreamingEcho(500, processor=recorder)
        samples = np.full(160, 300, dtype="<i2")
        for index in range(700):
            stamp = 10000 + round(index * 10 * 1.001)
            output = echo.capture(PCMFrame(index, index * RATE // 100, stamp, samples), 0)
            self.assertEqual(output, samples.tobytes())
        self.assertAlmostEqual(echo.capture_drift_ppm, 1000, delta=100)
