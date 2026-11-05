"""Finite-stream timestamps refer to producer block handoff, not planned time."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from jarvis.audio import RATE, make_probe
from jarvis.server import Stream


class StreamTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_pcm_generation_precedes_stream_clock_and_connection(self):
        stream = Stream('latency', 2)
        clock = SimpleNamespace(now=100.0)
        sleeps = []
        prepared = []

        def slow_probe(seed):
            self.assertIsNone(stream.started_at)
            self.assertFalse(stream.connected.is_set())
            self.assertEqual(stream.markers, [])
            clock.now += 3.0
            probe = make_probe(seed)
            prepared.append(probe)
            return probe

        async def sleep(delay):
            sleeps.append(delay)
            clock.now += delay

        observed = []
        with patch('jarvis.server.make_probe', side_effect=slow_probe), \
                patch('jarvis.server.time', SimpleNamespace(monotonic=lambda: clock.now)), \
                patch('jarvis.server.asyncio.sleep', side_effect=sleep):
            async for chunk in stream.blocks():
                self.assertTrue(stream.connected.is_set())
                observed.append((clock.now, chunk))

        self.assertEqual(stream.started_at, 106.0)
        self.assertAlmostEqual(observed[0][0], 106.0)
        self.assertLessEqual(max(sleeps), .020000001)
        self.assertEqual(stream.marker_timing, 'block_handoff_monotonic')
        self.assertAlmostEqual(stream.markers[0][0], 106.1)
        self.assertAlmostEqual(stream.markers[1][0], 108.1)
        self.assertAlmostEqual(stream.schedule_lag_ms, 0)
        pcm = np.frombuffer(b''.join(chunk for _, chunk in observed), dtype='<i2')
        self.assertEqual(len(pcm), int(stream.duration * RATE))
        for index, (_, probe) in enumerate(stream.markers):
            offset = int((.1 + index * 2) * RATE)
            np.testing.assert_array_equal(probe, prepared[index])
            np.testing.assert_array_equal(pcm[offset:offset + len(probe)], probe)
        self.assertEqual(stream.bytes_sent, len(pcm) * 2)
        np.testing.assert_array_equal(stream.reference_pcm, pcm)

    async def test_later_stall_records_actual_marker_without_changing_earlier_clock(self):
        stream = Stream('latency', 3)
        clock = SimpleNamespace(now=10.0)
        stalled = False
        observed = []

        async def sleep(delay):
            nonlocal stalled
            clock.now += delay
            if not stalled and clock.now >= 12.1 - 1e-9:
                clock.now += .12
                stalled = True

        with patch('jarvis.server.time', SimpleNamespace(monotonic=lambda: clock.now)), \
                patch('jarvis.server.asyncio.sleep', side_effect=sleep):
            async for chunk in stream.blocks():
                observed.append((clock.now, chunk))

        self.assertTrue(stalled)
        self.assertGreater(stream.schedule_lag_ms, 50)
        self.assertEqual(stream.marker_timing, 'block_handoff_monotonic')
        self.assertEqual(len(stream.markers), 3)
        for index, (emitted, _) in enumerate(stream.markers):
            marker_block = int((.1 + index * 2) * RATE) // 320
            self.assertEqual(emitted, observed[marker_block][0])
        self.assertAlmostEqual(stream.markers[0][0], 10.1)
        self.assertAlmostEqual(stream.markers[1][0], 12.22)
        self.assertAlmostEqual(stream.markers[2][0], 14.1)
        for actual, expected in zip(stream.marker_schedule_lag_ms, (0, 120, 0)):
            self.assertAlmostEqual(actual, expected)
        self.assertTrue(all(len(chunk) == 640 for _, chunk in observed))

    async def test_echo_reuses_stability_waveform_and_retains_ram_reference(self):
        sources = []
        for kind in ('stability', 'echo'):
            stream = Stream(kind, 2)
            clock = SimpleNamespace(now=100.0)

            async def sleep(delay):
                clock.now += delay

            with patch('jarvis.server.time', SimpleNamespace(monotonic=lambda: clock.now)), \
                    patch('jarvis.server.asyncio.sleep', side_effect=sleep):
                blocks = [chunk async for chunk in stream.blocks()]
            pcm = np.frombuffer(b''.join(blocks), dtype='<i2')
            np.testing.assert_array_equal(stream.reference_pcm, pcm)
            self.assertEqual(len(stream.markers), 2)
            sources.append(pcm)
        np.testing.assert_array_equal(*sources)
        self.assertGreater(np.count_nonzero(sources[1][4 * RATE:5 * RATE]), RATE // 2)

    async def test_unemitted_markers_never_claim_handoff_schema(self):
        stream = Stream('latency', 2)
        clock = SimpleNamespace(now=100.0)

        async def sleep(delay):
            clock.now += delay

        with patch('jarvis.server.time', SimpleNamespace(monotonic=lambda: clock.now)), \
                patch('jarvis.server.asyncio.sleep', side_effect=sleep):
            blocks = stream.blocks()
            await anext(blocks)
            self.assertEqual(stream.markers, [])
            self.assertIsNone(stream.marker_timing)
            for _ in range(5):
                await anext(blocks)
            self.assertEqual(len(stream.markers), 1)
            self.assertEqual(stream.marker_timing, 'block_handoff_monotonic')
            stream.stop_requested.set()
            self.assertEqual([chunk async for chunk in blocks], [])
        self.assertEqual(len(stream.markers), 1)


if __name__ == '__main__':
    unittest.main()
