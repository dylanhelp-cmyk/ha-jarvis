import asyncio
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

import numpy as np

from jarvis.audio import PCMFrame
from jarvis.conversation_trial import (CleanInput, OpeningEchoInput, SpeechEndpoint, gain_packet,
                                       run_voice_turn, stream_reply)
from jarvis.opening_audio import OpeningAudio


class LocalEcho:
    def __init__(self, delay):
        self.delay = delay

    def render(self, *args, **kwargs):
        pass

    def finish_render(self, *args):
        pass

    def capture(self, frame, offset):
        return frame.samples.tobytes()


class SimulatedSession:
    def __init__(self, settings, *, on_audio, **kwargs):
        self.on_audio = on_audio
        self.metrics = SimpleNamespace(error=None, ready_ms=5, finalization_confirmed=True, usage_seconds=0)
        self.sent = settings

    async def start(self):
        self.sent.append("ready")

    async def send_audio(self, pcm):
        self.sent.append(pcm)
        if len(self.sent) in (3, 5):
            await self.on_audio(bytes(640), 16000)

    async def close(self):
        self.sent.append("closed")
        return self.metrics


class SimulatedReply:
    def __init__(self, device, identity, *, output_gain_db=0):
        self.first_voice_at = self.last_voice_at = None
        self.player = self
        self.frames_sent = self.frames_rendered = 480
        self.draining = asyncio.Event()

    async def receive(self, audio, rate):
        self.first_voice_at = time.monotonic()
        self.last_voice_at = self.first_voice_at - .7

    async def send_stream(self, unused, *, prime_packets, source_prime_packets):
        await self.draining.wait()

    def chunks(self):
        return None

    def ready_to_drain(self, now):
        return self.last_voice_at is not None and now - self.last_voice_at >= .6

    async def finish(self):
        self.draining.set()

    def snapshot(self):
        return {"frames_rendered": self.frames_rendered}

    async def stop(self):
        self.draining.set()


class VoiceTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_warm_speech_near_idle_deadline_keeps_listening(self):
        for speaking in (False, True):
            with self.subTest(speaking=speaking):
                now = 100.0
                sent = 0
                output = None

                def set_output(reply):
                    nonlocal output
                    output = reply

                async def packet():
                    nonlocal now
                    now = 100 + sent * .02
                    pcm = bytes([1, 0]) * 320 if speaking and 495 <= sent < 499 else bytes(640)
                    return pcm, now

                async def send_audio(_):
                    nonlocal sent
                    sent += 1
                    if sent == 503:
                        output.first_voice_at = now
                        output.last_voice_at = now - 1

                device = SimpleNamespace(errors=[], info={"jack_plugged": True})
                session = SimpleNamespace(metrics=SimpleNamespace(error=None), send_audio=send_audio)
                stream = SimpleNamespace(packet=packet, offer=lambda *_: None, passthrough_samples=0)
                detector = SimpleNamespace(is_speech=lambda pcm, rate: bool(pcm[0]))
                result = {"id": "warm"}
                with patch("jarvis.conversation_trial.BenchReply", SimulatedReply), \
                     patch("jarvis.conversation_trial.SpeechEndpoint", lambda: SpeechEndpoint(detector)), \
                     patch("jarvis.conversation_trial.time", SimpleNamespace(monotonic=lambda: now)):
                    run = stream_reply(device, session, result, stream, set_output, lambda _: None, "listening",
                                       gain_db=0, max_age_ms=250, warm_follow_up=True)
                    if speaking:
                        self.assertIsNotNone(await run)
                        self.assertEqual(sent, 503)
                    else:
                        with self.assertRaisesRegex(TimeoutError, "without detected speech"):
                            await run
                        self.assertLess(sent, 503)

    def test_echo_guard_keeps_sample_order_across_partial_aec_blocks(self):
        buffered = 0
        def aec_capture(frame, _offset):
            nonlocal buffered
            buffered += len(frame.samples)
            count = buffered // 160 * 160
            buffered -= count
            return bytes(count * 2)
        with patch("jarvis.conversation_trial.StreamingEcho",
                   return_value=SimpleNamespace(capture=aec_capture)):
            stream = CleanInput(31, passthrough_after=1.015625)
        pcm = np.arange(512, dtype="<i2")
        stream.offer(PCMFrame(0, 0, 1000, pcm[:256]), 0)
        stream.offer(PCMFrame(1, 256, 1016, pcm[256:]), 0)
        expected = np.concatenate((np.zeros(250, dtype="<i2"), pcm[250:480]))
        np.testing.assert_array_equal(np.frombuffer(stream.pending, dtype="<i2"), expected)
        self.assertEqual(stream.passthrough_samples, 230)

    def test_input_gain_preserves_zero_and_reports_overload(self):
        pcm = np.array([0, -100, 100, 32767, -32768], dtype="<i2").tobytes()
        self.assertEqual(gain_packet(pcm, 0), (pcm, 32768, 32768, 0))
        output, before, after, overload = gain_packet(pcm, 24)
        self.assertEqual((before, after, overload), (32768, 32768, 2))
        self.assertEqual(np.frombuffer(output, dtype="<i2").tolist(), [0, -1585, 1585, 32767, -32768])

    def test_high_gain_lifts_quiet_speech_and_compresses_loud_peaks(self):
        pcm = np.array([0, -117, 117, 800, 2000, 32767, -32768], dtype="<i2").tobytes()
        output, _, peak, overload = gain_packet(pcm, 36)
        samples = np.frombuffer(output, dtype="<i2")
        self.assertEqual(samples[:3].tolist(), [0, -7382, 7382])
        self.assertTrue(24576 < samples[3] < samples[4] < samples[5] < 32767)
        self.assertLess(peak, 32767)
        self.assertEqual(overload, 0)

    def test_speech_timing_requires_a_burst_then_quiet(self):
        detector = SimpleNamespace(is_speech=lambda pcm, rate: bool(pcm[0]))
        endpoint = SpeechEndpoint(detector)
        for i, voiced in enumerate([True, False, False, False, True, True, True] + [False] * 7):
            endpoint.observe(bytes([voiced]), 1 + i * .02, 1.3 + i * .02)
        self.assertEqual(endpoint.timing(1.8)["status"], "unavailable")
        endpoint.observe(bytes(1), 1.28, 1.58)
        timing = endpoint.timing(1.8)
        self.assertEqual(timing["status"], "estimated")
        self.assertEqual(timing["speech_end_to_send_ms"], 300)
        self.assertEqual(timing["speech_end_send_to_cloud_voice_ms"], 380)
        self.assertEqual(timing["speech_end_to_cloud_voice_ms"], 680)
        self.assertEqual(endpoint.timing(4)["status"], "unavailable")

    async def test_confirmed_wake_sends_ordered_opening_then_drains_and_closes(self):
        endpoint = SpeechEndpoint(SimpleNamespace(is_speech=lambda *_: False))
        with (patch("jarvis.conversation_trial.LiveSession", SimulatedSession),
              patch("jarvis.conversation_trial.BenchReply", SimulatedReply),
              patch("jarvis.conversation_trial.StreamingEcho", LocalEcho),
              patch("jarvis.conversation_trial.SpeechEndpoint", return_value=endpoint),
              patch.object(endpoint, "observe", wraps=endpoint.observe) as observe):
            event = asyncio.Event()
            event.set()
            device = SimpleNamespace(ws=None, wake_details={}, wake_terminal_event=event,
                                     wake_playback_terminal={"drained": True, "rendered_samples": 480,
                                                             "first_rendered_ms": 10, "last_rendered_ms": 20},
                                     clock_offset_ms=0, errors=[], info={"jack_plugged": True})
            opening = OpeningAudio()
            samples = np.arange(768, dtype="<i2")
            stamp = int(time.monotonic() * 1000)
            for i in range(3):
                opening.offer(PCMFrame(i, i * 256, stamp + i * 16, samples[i * 256:(i + 1) * 256]), 0)
            messages, result = [], {"id": "test"}
            await run_voice_turn(device, messages, result, opening, bytes(480 * 4), 31, lambda phase: None,
                                 gain_db=36)
            packets = [samples[:320].tobytes(), samples[320:640].tobytes()]
            self.assertEqual(messages, ["ready", *(gain_packet(pcm, 36)[0] for pcm in packets), "closed"])
            self.assertEqual([call.args[0] for call in observe.call_args_list],
                             [gain_packet(pcm, 24)[0] for pcm in packets])
            self.assertEqual(result["input_signal"]["vad_gain_db"], 24)
            self.assertEqual(result["input_samples_sent"], 640)
            self.assertIsNotNone(result["input_queue"]["first_packet_age_ms"])
            self.assertEqual(result["playback"]["frames_rendered"], 480)
            self.assertIsNone(device.pcm_consumer)
            self.assertIsNone(device.playback)

    async def test_warm_reply_reuses_session_with_fresh_capture_and_no_cold_queue(self):
        with patch("jarvis.conversation_trial.LiveSession", SimulatedSession),              patch("jarvis.conversation_trial.BenchReply", SimulatedReply),              patch("jarvis.conversation_trial.StreamingEcho", LocalEcho):
            event = asyncio.Event()
            event.set()
            device = SimpleNamespace(ws=None, wake_details={}, wake_terminal_event=event,
                                     wake_playback_terminal={"drained": True, "rendered_samples": 480,
                                                             "first_rendered_ms": 10, "last_rendered_ms": 20},
                                     clock_offset_ms=0, errors=[], info={"jack_plugged": True})
            opening = OpeningAudio()
            cold = np.arange(768, dtype="<i2")
            warm = np.arange(768, dtype="<i2") + 1000
            stamp = int(time.monotonic() * 1000)
            for index in range(3):
                opening.offer(PCMFrame(index, index * 256, stamp + index * 16,
                                       cold[index * 256:(index + 1) * 256]), 0)

            def phase(label):
                if label == "listening for your warm follow-up":
                    stamp = int(time.monotonic() * 1000) + 1
                    # A late frame from the previous reply must not enter warm input.
                    device.pcm_consumer(PCMFrame(100, 25600, stamp - 100, cold[:256]), 0)
                    for index in range(3):
                        device.pcm_consumer(PCMFrame(101 + index, 25856 + index * 256, stamp + index * 16,
                                                       warm[index * 256:(index + 1) * 256]), 0)

            messages, result = [], {"id": "test"}
            replies = await run_voice_turn(device, messages, result, opening, bytes(480 * 4), 31, phase,
                                           follow_up=True)
            self.assertEqual(len(replies), 2)
            self.assertEqual(messages, ["ready", cold[:320].tobytes(), cold[320:640].tobytes(),
                                        warm[:320].tobytes(), warm[320:640].tobytes(), "closed"])
            self.assertEqual(result["warm_reply"]["input_samples_sent"], 640)

    async def test_preconnected_input_can_start_before_ack_drain_but_speaker_cannot(self):
        input_sent = asyncio.Event()
        output_started = asyncio.Event()

        class EarlySession(SimulatedSession):
            async def send_audio(self, pcm):
                await super().send_audio(pcm)
                input_sent.set()

        class GatedReply(SimulatedReply):
            async def chunks(self):
                yield bytes(640)

            async def send_stream(self, chunks, **_):
                async for _ in chunks:
                    output_started.set()
                await self.draining.wait()

        stamp = int(time.monotonic() * 1000) - 50
        report = (4800, stamp, time.monotonic())
        device = SimpleNamespace(ws=None, wake_details={}, wake_terminal_event=asyncio.Event(),
                                 wake_render_changed=asyncio.Event(), wake_render_progress=report,
                                 wake_render_latest=report, clock_offset_ms=0, errors=[],
                                 info={"jack_plugged": True})
        opening = OpeningAudio()
        samples = np.arange(768, dtype="<i2")

        async def wake():
            for i in range(3):
                opening.offer(PCMFrame(i, i * 256, stamp + i * 16, samples[i * 256:(i + 1) * 256]), 0)
            device.wake_render_changed.set()
            return True

        messages, result = [], {"id": "early"}
        with (patch("jarvis.conversation_trial.LiveSession", EarlySession),
              patch("jarvis.conversation_trial.BenchReply", GatedReply),
              patch("jarvis.conversation_trial.StreamingEcho", LocalEcho)):
            task = asyncio.create_task(run_voice_turn(device, messages, result, opening, bytes(22080 * 4), 31,
                                                       lambda _: None, wait_for_preconnected_wake=wake))
            try:
                await asyncio.wait_for(input_sent.wait(), 1)
                self.assertFalse(output_started.is_set())
                self.assertFalse(device.wake_terminal_event.is_set())
                device.wake_playback_terminal = {"drained": True, "first_rendered_ms": stamp,
                                                 "last_rendered_ms": stamp + 460, "rendered_samples": 22080}
                device.wake_render_terminal_at = time.monotonic()
                device.wake_terminal_event.set()
                device.wake_render_changed.set()
                await asyncio.wait_for(task, 1)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(output_started.is_set())
        self.assertEqual(messages, ["ready", samples[:320].tobytes(), samples[320:640].tobytes(), "closed"])
        self.assertEqual(result["opening_reference_mode"], "confirmed_prefix")
        self.assertGreaterEqual(result["first_input_before_ack_terminal_ms"], 0)

    async def test_live_opening_holds_capture_past_confirmed_render(self):
        device = SimpleNamespace(clock_offset_ms=0, errors=[], wake_render_changed=asyncio.Event(),
                                 wake_terminal_event=asyncio.Event(), wake_render_latest=(4800, 1000, 0))
        with patch("jarvis.conversation_trial.StreamingEcho", LocalEcho):
            stream = OpeningEchoInput(device, bytes(22080 * 4), 31, 1000)
        samples = np.arange(7 * 256, dtype="<i2")
        for i in range(7):
            stream.offer(PCMFrame(i, i * 256, 1000 + i * 16, samples[i * 256:(i + 1) * 256]), 0)
        for i in range(4):
            pcm, _ = await stream.packet()
            self.assertEqual(pcm, samples[i * 320:(i + 1) * 320].tobytes())
        next_packet = asyncio.create_task(stream.packet())
        try:
            await asyncio.sleep(0)
            self.assertFalse(next_packet.done())
            device.wake_render_latest = (7200, 1000, 0)
            device.wake_render_changed.set()
            pcm, _ = await asyncio.wait_for(next_packet, 1)
            self.assertEqual(pcm, samples[1280:1600].tobytes())
        finally:
            if not next_packet.done():
                next_packet.cancel()
                await asyncio.gather(next_packet, return_exceptions=True)

    async def test_preconnection_timeout_finalizes_without_sending_room_audio(self):
        messages, result = [], {"id": "preconnected"}
        device = SimpleNamespace(ws=None, wake_details={}, wake_terminal_event=asyncio.Event())

        async def wait_for_wake():
            self.assertEqual(messages, ["ready"])
            return False

        with patch("jarvis.conversation_trial.LiveSession", SimulatedSession):
            replies = await run_voice_turn(device, messages, result, OpeningAudio(), b"", 31, lambda _: None,
                                           wait_for_preconnected_wake=wait_for_wake)

        self.assertEqual(replies, [])
        self.assertEqual(messages, ["ready", "closed"])
        self.assertEqual(result["connection_start"], "before_wake")
        self.assertTrue(result["cloud"]["finalization_confirmed"])
