"""Real PyAV conversion and bounded continuous RAOP source checks, offline."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import av
import numpy as np
from pyatv.protocols.raop import RaopStream
from pyatv.protocols.raop.protocols import StreamContext

from jarvis.airplay_source import progressive_stream_file, sender_telemetry
from jarvis.live_audio import LiveAudioSource, StaleAudioGeneration, StartupPCMSource
from jarvis.server import Stream


class LiveAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_acknowledgment_source_keeps_separate_word_windows(self):
        slots = np.zeros((2, 12000), dtype="<i2")
        slots[0, 320:5120] = 1200
        slots[1, 640:5440] = -1800
        stream = Stream("startup_ack", 2, acknowledgment=slots.tobytes())
        source = StartupPCMSource(stream)
        await source.readframes(44100)
        self.assertEqual(len(stream.markers), 2)
        self.assertTrue(np.all(stream.markers[0][1] == 1200))
        self.assertTrue(np.all(stream.markers[1][1] == -1800))

    async def test_startup_pcm_marks_probes_only_when_requested(self):
        stream = Stream("startup_pcm", 2)
        source = StartupPCMSource(stream)
        self.assertFalse(stream.connected.is_set())
        self.assertEqual(await source.readframes(4300), bytes(4300 * 4))
        self.assertTrue(stream.connected.is_set())
        self.assertEqual(stream.markers, [])
        await source.readframes(352)
        self.assertEqual(len(stream.markers), 1)
        self.assertGreaterEqual(stream.markers[0][0], stream.started_at)
        await source.readframes(4 * 44100)
        self.assertEqual(len(stream.markers), 2)
        self.assertEqual(await source.readframes(352), b"")
        self.assertTrue(stream.done.is_set())

    async def test_render_tap_receives_exact_packet_with_inserted_silence(self):
        tap = Mock()
        source = LiveAudioSource(clock=lambda: 10.0, on_render=tap)
        silence = await source.readframes(352)
        await source.write(np.full(160, 4000, dtype='<i2').tobytes())
        mixed = await source.readframes(source.queued_frames + 25)
        self.assertEqual(tap.call_args_list[0].args, (silence, 10.0))
        self.assertEqual(tap.call_args_list[1].args, (mixed, 10.0))
        self.assertEqual(mixed[-100:], bytes(100))

    async def test_real_resampler_retains_rate_and_samples_across_irregular_chunks(self):
        samples = (np.sin(np.arange(16000) * .071) * 9000).astype('<i2')
        source = LiveAudioSource()
        sizes = (1, 17, 53, 320, 779, 1600, 720, 12510)
        offset = 0
        for count in sizes:
            await source.write(samples[offset:offset + count].tobytes())
            offset += count
        self.assertEqual(offset, len(samples))
        # Compare against a real single uninterrupted resampling operation.
        reference = av.AudioResampler(format='s16', layout='stereo', rate=44100)
        frame = av.AudioFrame.from_ndarray(np.repeat(samples, 2).reshape(1, -1), format='s16', layout='stereo')
        frame.sample_rate = 16000
        expected = b''.join(output.to_ndarray().astype('>i2').tobytes()
                            for output in reference.resample(frame))
        self.assertEqual(source.queued_frames, len(expected) // 4)
        self.assertEqual(source.queued_frames + source.telemetry['resampler_pending_frames'], 44100)
        self.assertGreater(source.queued_frames, 44000)
        actual = await source.readframes(source.queued_frames)
        self.assertEqual(actual, expected)
        self.assertEqual(source.telemetry['silence_frames'], 0)
        stereo = np.frombuffer(actual, dtype='>i2').reshape(-1, 2)
        np.testing.assert_array_equal(stereo[:, 0], stereo[:, 1])

    async def test_asymmetric_signed_samples_are_output_in_network_byte_order(self):
        # Long plateaux avoid depending on filter transients. Mono duplication
        # preserves the bench's gain, with identical left and right samples.
        samples = np.concatenate((np.full(1000, 0x1234), np.full(1000, -0x2345))).astype('<i2')
        source = LiveAudioSource()
        await source.write(samples.tobytes())
        data = await source.readframes(source.queued_frames)
        stereo = np.frombuffer(data, dtype='>i2').reshape(-1, 2)
        positive, negative = int(stereo[500, 0]), int(stereo[4000, 0])
        self.assertAlmostEqual(positive, 0x1234, delta=2)
        self.assertAlmostEqual(negative, -0x2345, delta=2)
        self.assertEqual(data[500 * 4:500 * 4 + 2], positive.to_bytes(2, 'big', signed=True))
        self.assertNotEqual(data[500 * 4:500 * 4 + 2], positive.to_bytes(2, 'little', signed=True))

    async def test_starvation_returns_exact_silence_until_idempotent_close(self):
        source = LiveAudioSource()
        self.assertEqual(await source.readframes(352), bytes(352 * 4))
        self.assertEqual(source.telemetry['silence_frames'], 352)
        self.assertIsNone(source.telemetry['max_queue_residence_ms'])
        await source.write(np.full(160, 4000, dtype='<i2').tobytes())
        available = source.queued_frames
        self.assertGreater(available, 0)
        data = await source.readframes(available + 25)
        self.assertEqual(len(data), (available + 25) * 4)
        self.assertTrue(any(data[:-100]))
        self.assertEqual(data[-100:], bytes(100))
        self.assertEqual(source.telemetry['silence_frames'], 377)
        await source.close()
        await source.close()
        self.assertEqual(await source.readframes(352), b'')
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            await source.write(bytes(2))

    async def test_two_second_bound_includes_resampler_delay_and_rejects_atomically(self):
        source = LiveAudioSource()
        with self.assertRaisesRegex(BufferError, '2 second'):
            await source.write(bytes(32001 * 2))
        self.assertEqual(source.queued_frames, 0)
        self.assertEqual(source.telemetry['resampler_pending_frames'], 0)
        await source.write(np.full(32000, 1234, dtype='<i2').tobytes())
        queued = source.queued_frames
        self.assertEqual(queued + source.telemetry['resampler_pending_frames'], 88200)
        with self.assertRaises(BufferError):
            await source.write(bytes(2))
        self.assertEqual(source.queued_frames, queued)
        self.assertEqual(source.telemetry['overflow_count'], 2)
        self.assertTrue(any(await source.readframes(1000)))
        await source.write(bytes(300 * 2))
        self.assertLessEqual(source.queued_frames + source.telemetry['resampler_pending_frames'], 88200)

    async def test_interrupt_clears_queue_filter_tail_and_fences_cancelled_generation(self):
        source, clean = LiveAudioSource(), LiveAudioSource()
        old_generation = source.generation
        await source.write(np.full(1600, 20000, dtype='<i2').tobytes(), generation=old_generation)
        await source.readframes(7)  # Leave a partly consumed queue entry too.
        await source.interrupt()
        self.assertEqual(source.queued_frames, 0)
        self.assertEqual(source.telemetry['resampler_pending_frames'], 0)
        self.assertEqual(await source.readframes(352), bytes(352 * 4))
        with self.assertRaises(StaleAudioGeneration):
            await source.write(bytes(100), generation=old_generation)
        replacement = np.full(1600, -2000, dtype='<i2').tobytes()
        await source.write(replacement, generation=source.generation)
        await clean.write(replacement)
        self.assertEqual(source.queued_frames, clean.queued_frames)
        self.assertEqual(await source.readframes(source.queued_frames), await clean.readframes(clean.queued_frames))

    async def test_queue_residence_is_measured_only_after_consuming_audio(self):
        now = [10.0]
        source = LiveAudioSource(clock=lambda: now[0])
        await source.readframes(352)
        self.assertIsNone(source.telemetry['max_queue_residence_ms'])
        await source.write(bytes(1600 * 2))
        self.assertIsNone(source.telemetry['max_queue_residence_ms'])
        now[0] += .25
        await source.readframes(352)
        self.assertAlmostEqual(source.telemetry['max_queue_residence_ms'], 250)
        now[0] += .5
        await source.readframes(352)
        self.assertAlmostEqual(source.telemetry['max_queue_residence_ms'], 750)

    async def test_invalid_format_sample_alignment_and_read_count_fail_explicitly(self):
        for arguments in ((48000, 2, 2), (44100, 1, 2), (44100, 2, 4)):
            with self.assertRaises(ValueError):
                LiveAudioSource(*arguments)
        source = LiveAudioSource()
        with self.assertRaises(ValueError):
            await source.write(b'x')
        for count in (0, -1, True, 88201):
            with self.assertRaises(ValueError):
                await source.readframes(count)


class LiveAudioFactoryTests(unittest.IsolatedAsyncioTestCase):
    def connection(self):
        context = StreamContext()
        client = SimpleNamespace(initialize=AsyncMock(), send_audio=AsyncMock())
        manager = SimpleNamespace(acquire=Mock(), context=context,
            setup=AsyncMock(return_value=(client, context)), teardown=AsyncMock())
        core = SimpleNamespace(takeover=Mock(return_value=Mock()),
            service=SimpleNamespace(credentials=None, password=None, properties={}))
        stream = RaopStream(core, None, None, manager)
        stream.audio = SimpleNamespace(has_changed_volume=True, volume=47, set_volume=AsyncMock())
        return SimpleNamespace(stream=SimpleNamespace(get=lambda protocol: stream)), client

    async def test_factory_is_awaited_with_negotiated_format_without_opening_http(self):
        connection, client = self.connection()
        source = LiveAudioSource()
        factory = AsyncMock(return_value=source)
        with patch('jarvis.airplay_source.ProgressiveAudioSource.open', AsyncMock()) as http:
            await progressive_stream_file(connection, 'http://127.0.0.1/live.wav', sender_telemetry(), source_factory=factory)
        factory.assert_awaited_once_with(44100, 2, 2)
        http.assert_not_awaited()
        self.assertIs(client.send_audio.await_args.args[0], source)
        self.assertTrue(source.closed)

    async def test_factory_format_mismatch_closes_source_and_never_sends(self):
        connection, client = self.connection()
        source = LiveAudioSource()
        source._channels = 1
        with self.assertRaisesRegex(ValueError, 'negotiated PCM'):
            await progressive_stream_file(connection, 'http://127.0.0.1/live.wav', sender_telemetry(),
                                          source_factory=AsyncMock(return_value=source))
        self.assertTrue(source.closed)
        client.send_audio.assert_not_awaited()
