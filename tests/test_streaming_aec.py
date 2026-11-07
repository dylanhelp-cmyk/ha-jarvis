import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from jarvis.audio import PCMFrame, RATE
from jarvis.streaming_aec import (ConfirmedRenderInput, StreamingEcho, check_opening_reference,
                                 compare_opening_prefix, residual_echo_similarity)


class ReferenceRecorder:
    def __init__(self):
        self.references = []

    def process(self, capture, render):
        self.references.append(np.frombuffer(render, dtype="<i2"))
        return capture


class StreamingEchoTests(unittest.TestCase):
    def test_residual_echo_similarity_distinguishes_delayed_echo_from_independent_input(self):
        rng = np.random.default_rng(11)
        reference = rng.integers(-700, 700, size=3000, dtype=np.int16)
        echo = (reference[570:2170] // 5 + 35).astype("<i2")
        independent = rng.integers(-1500, 1500, size=len(echo), dtype=np.int16)
        self.assertGreater(residual_echo_similarity(echo.tobytes(), reference), .99)
        self.assertLess(residual_echo_similarity((echo + independent).tobytes(), reference), .3)
        self.assertIsNone(residual_echo_similarity(echo.tobytes(), reference[:800]))

    def test_live_dma_prefix_holds_capture_and_stops_on_a_discontinuity(self):
        player = SimpleNamespace(first_rendered_ms=None, last_rendered_ms=None, frames_rendered=None,
                                 render_clock_invalid=False, schedule_rebases=0,
                                 max_render_discontinuity_us=0, terminal_confirmed=False)
        pcm = np.full(9600 * 2, 500, dtype="<i2").tobytes()
        samples = np.arange(256 * 11, dtype="<i2")
        frames = [PCMFrame(i, i * 256, 1000 + 16 * i, samples[i * 256:(i + 1) * 256])
                  for i in range(11)]
        with patch("jarvis.streaming_aec.HostAEC3", return_value=ReferenceRecorder()):
            stream = ConfirmedRenderInput(player, pcm, 35)
        pre_roll = stream.prime_capture(frames[:3], 0)
        self.assertEqual(len(pre_roll), 640 * 2)
        self.assertTrue(all(not stream.offer(frame, 0) for frame in frames[3:6]))
        player.first_rendered_ms, player.last_rendered_ms, player.frames_rendered = 1040, 1140, 4800
        clean = b"".join(stream.offer(frame, 0) for frame in frames[6:10])
        self.assertEqual(pre_roll + clean, samples[:1760].tobytes())
        self.assertEqual(stream.processed_during_playback, 1120)
        self.assertEqual(stream.waiting_samples, 768)

        player.max_render_discontinuity_us = 20_000
        player.last_rendered_ms, player.frames_rendered = 1260, 9600
        self.assertEqual(stream.offer(frames[-1], 0), b"")
        self.assertIn("Discontinuous speaker output", stream.error)

    def test_live_dma_terminal_releases_the_remaining_capture(self):
        player = SimpleNamespace(first_rendered_ms=1000, last_rendered_ms=1100, frames_rendered=4800,
                                 render_clock_invalid=False, schedule_rebases=0,
                                 max_render_discontinuity_us=0, terminal_confirmed=False,
                                 frames_sent=4800, reason=None)
        pcm = np.full(4800 * 2, 500, dtype="<i2").tobytes()
        samples = np.arange(256 * 8, dtype="<i2")
        frames = [PCMFrame(i, i * 256, 1000 + 16 * i, samples[i * 256:(i + 1) * 256])
                  for i in range(8)]
        with patch("jarvis.streaming_aec.HostAEC3", return_value=ReferenceRecorder()):
            stream = ConfirmedRenderInput(player, pcm, 35)
        clean = b"".join(stream.offer(frame, 0) for frame in frames[:7])
        self.assertEqual(len(clean), 1280 * 2)
        player.terminal_confirmed, player.reason = True, "drained"
        clean += stream.offer(frames[7], 0)
        self.assertTrue(stream.finished)
        self.assertEqual(clean, samples[:1920].tobytes())
        self.assertEqual(stream.waiting_samples, 0)

    def test_first_report_prefix_excludes_capture_beyond_proven_render(self):
        pcm = np.tile((3000 * np.sin(2 * np.pi * 350 * np.arange(22080) / 48000))[:, None], (1, 2))
        frames = [PCMFrame(i, i * 256, 1000 + i * 16, np.zeros(256, dtype="<i2")) for i in range(40)]
        with patch("jarvis.streaming_aec.HostAEC3",
                   side_effect=lambda **_: SimpleNamespace(process=lambda _capture, render: render)):
            compared = compare_opening_prefix(frames, pcm.astype("<i2").tobytes(), 0, 1040, 1500, 4800, 30)
            short = compare_opening_prefix(frames, pcm.astype("<i2").tobytes(), 0, 1040, 1500, 960, 30)
        self.assertEqual(compared["status"], "compared", compared)
        self.assertEqual(compared["capture_samples"], 1760)
        self.assertEqual(compared["overlapping_capture_ms"], 72)
        self.assertLess(compared["rms_difference_pcm"], 1)
        self.assertEqual(short["status"], "unavailable")

    def test_render_delay_and_partial_capture_frames_preserve_samples(self):
        recorder = ReferenceRecorder()
        echo = StreamingEcho(500, processor=recorder)
        pcm = np.full((48000, 2), 2345, dtype="<i2")
        for start in range(0, len(pcm), 480):
            echo.render(pcm[start:start + 480].tobytes(), 10 + start / 48000, first_frame=start)
        captured = np.arange(512, dtype="<i2")
        first = echo.capture(PCMFrame(0, 0, 10600, captured[:256]), 0)
        second = echo.capture(PCMFrame(1, 256, 10616, captured[256:]), 0)
        self.assertEqual(first + second, captured[:480].tobytes())
        self.assertEqual(len(recorder.references), 3)
        np.testing.assert_array_equal(recorder.references[0], np.full(160, 2345))

    def test_cached_acknowledgment_and_tail_fit_the_render_history(self):
        frames = [PCMFrame(i, i * 256, 1000 + i * 16, np.zeros(256, dtype="<i2"))
                  for i in range(80)]
        with patch("jarvis.streaming_aec.HostAEC3", return_value=ReferenceRecorder()):
            result = check_opening_reference(frames, bytes(22080 * 4), 0, 1040, 1500, 1.08, 0)
        self.assertEqual(result["status"], "processed", result)
        self.assertEqual(result["processed_samples"], 80 * 256)

    def test_confirmed_render_bursts_keep_silence_and_capture_order_between_replies(self):
        recorder = ReferenceRecorder()
        echo = StreamingEcho(0, processor=recorder)
        first = np.full((960, 2), 1000, dtype="<i2").tobytes()
        second = np.full((960, 2), 2000, dtype="<i2").tobytes()
        echo.render(first, 1, first_frame=0)
        echo.finish_render(960, 1.02)
        echo.render(second, 1.05, first_frame=0)
        echo.finish_render(960, 1.07)
        for index in range(8):
            pcm = np.full(160, index, dtype="<i2")
            self.assertEqual(echo.capture(PCMFrame(index, index * 160, 1000 + index * 10, pcm), 0),
                             pcm.tobytes())
        self.assertEqual(int(np.median(recorder.references[0])), 1000)
        self.assertFalse(any(np.any(ref) for ref in recorder.references[2:5]))
        self.assertEqual(int(np.median(recorder.references[5])), 2000)

    def test_missing_render_frames_invalidate_the_reference(self):
        echo = StreamingEcho(100, processor=ReferenceRecorder())
        with self.assertRaisesRegex(RuntimeError, "Speaker discontinuity"):
            echo.render(bytes(480 * 4), 10, first_frame=480)

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
