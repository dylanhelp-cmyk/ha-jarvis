import unittest

import numpy as np

from jarvis.audio import PCMFrame
from jarvis.opening_audio import OpeningAudio


class OpeningAudioTests(unittest.TestCase):
    def test_expired_or_oversized_opening_is_never_released_partially(self):
        for max_ms, now, expected in ((10, 1.001, "opening_audio_overflow"),
                                      (100, 1.101, "opening_audio_expired")):
            with self.subTest(expected=expected):
                buffer = OpeningAudio(max_ms)
                buffer.offer(PCMFrame(0, 0, 1000, np.zeros(320, dtype="<i2")), 0)
                frames, status = buffer.release(now)
                self.assertEqual(frames, ())
                self.assertEqual(status["status"], "retry")
                self.assertEqual(status["reason"], expected)
