"""Real native AEC3 synthetic checks; no speaker/microphone acceptance claims."""
import importlib.util
import unittest
from unittest.mock import patch

import numpy as np

from jarvis.aec import FRAME_BYTES, HostAEC3


@unittest.skipUnless(importlib.util.find_spec('pywebrtc_audio'), 'pywebrtc-audio wheel not installed')
class HostAEC3Tests(unittest.TestCase):
    def test_real_aec3_reduces_delayed_echo_with_bounded_reference(self):
        rng = np.random.default_rng(725)
        render = rng.integers(-6000, 6001, 16000 * 8, dtype=np.int16)
        capture = np.zeros_like(render)
        delay = 9600  # 600 ms synthetic echo path, not a hardware measurement.
        capture[delay:] = (render[:-delay].astype(np.float64) * .6).astype(np.int16)
        processor = HostAEC3(reference_delay_ms=500, stream_delay_ms=100)
        output = []
        for offset in range(0, len(render), 160):
            output.append(np.frombuffer(processor.process(
                capture[offset:offset + 160].astype('<i2').tobytes(),
                render[offset:offset + 160].astype('<i2').tobytes()), dtype='<i2'))
            self.assertEqual(processor.snapshot()['reference_buffer_frames'], 50)
        clean = np.concatenate(output).astype(np.float64)
        original_power = np.mean(capture[-32000:].astype(np.float64) ** 2)
        residual_power = np.mean(clean[-32000:] ** 2)
        self.assertLess(residual_power, original_power / 10)  # >=10 dB synthetic reduction
        self.assertEqual(len(clean), len(render))
        self.assertEqual(processor.frames_processed, 800)
        self.assertFalse(processor.snapshot()['acoustic_cancellation_verified'])

    def test_exact_frame_contract_rejects_padding_and_partial_audio(self):
        processor = HostAEC3()
        for invalid in (b'', bytes(FRAME_BYTES - 2), bytes(FRAME_BYTES + 2), bytearray(FRAME_BYTES)):
            with self.assertRaises(ValueError):
                processor.process(invalid, bytes(FRAME_BYTES))
            with self.assertRaises(ValueError):
                processor.process(bytes(FRAME_BYTES), invalid)
        self.assertEqual(processor.frames_processed, 0)
        self.assertEqual(len(processor.process(bytes(FRAME_BYTES), bytes(FRAME_BYTES))), FRAME_BYTES)

    def test_independent_desired_signal_survives_synthetic_double_talk(self):
        rng = np.random.default_rng(42)
        length = 16000 * 10
        render = rng.integers(-5000, 5001, length, dtype=np.int16)
        target = np.zeros(length, dtype=np.float64)
        t = np.arange(16000 * 3) / 16000
        target[-len(t):] = 1800 * np.sin(2 * np.pi * 311 * t) + 900 * np.sin(2 * np.pi * 733 * t)
        capture = target.copy()
        capture[9600:] += render[:-9600].astype(np.float64) * .5
        capture = capture.astype('<i2')
        processor = HostAEC3(reference_delay_ms=500, stream_delay_ms=100)
        output = b''.join(processor.process(capture[i:i + 160].tobytes(),
            render[i:i + 160].astype('<i2').tobytes()) for i in range(0, length, 160))
        # These tones are a desired-signal regression, not proof of user speech
        # preservation. Allow the AEC/HP filter's small algorithmic offset.
        cleaned = np.frombuffer(output, dtype='<i2').astype(np.float64)[-16000:]
        desired = target[-16000:]
        correlations = [abs(np.corrcoef(cleaned[64:-64],
            desired[64 + lag:len(desired) - 64 + lag])[0, 1]) for lag in range(-48, 49)]
        self.assertGreater(max(correlations), .75)
        self.assertGreater(np.sqrt(np.mean(cleaned ** 2)), np.sqrt(np.mean(desired ** 2)) * .4)

    def test_reset_clears_stale_reference_and_native_adaptation(self):
        processor = HostAEC3(reference_delay_ms=20, stream_delay_ms=0)
        marker = np.full(160, 4000, dtype='<i2').tobytes()
        processor.process(bytes(FRAME_BYTES), marker)
        processor.reset()
        self.assertEqual(processor.frames_processed, 0)
        self.assertEqual(processor.generation, 1)
        self.assertEqual(list(processor._reference), [bytes(FRAME_BYTES)] * 2)
        for _ in range(5):
            result = processor.process(bytes(FRAME_BYTES), bytes(FRAME_BYTES))
            self.assertFalse(np.any(np.frombuffer(result, dtype='<i2')))

    def test_dependency_drift_fails_closed(self):
        with patch('jarvis.aec.version', return_value='99.0'):
            with self.assertRaisesRegex(RuntimeError, 'tested pywebrtc-audio'):
                HostAEC3()

    def test_delay_bounds_are_explicit(self):
        for invalid in (-10, 2001, 15, True, 500.0):
            with self.assertRaises(ValueError):
                HostAEC3(reference_delay_ms=invalid)
        for invalid in (-1, 501, True, 100.0):
            with self.assertRaises(ValueError):
                HostAEC3(stream_delay_ms=invalid)
