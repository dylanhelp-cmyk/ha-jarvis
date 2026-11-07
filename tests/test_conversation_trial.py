import asyncio
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

import numpy as np

from jarvis.audio import PCMFrame
from jarvis.conversation_trial import SpeechEndpoint, gain_packet, run_voice_turn
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
    def __init__(self, device, identity):
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

    async def finish(self):
        self.draining.set()

    def snapshot(self):
        return {"frames_rendered": self.frames_rendered}

    async def stop(self):
        self.draining.set()


class VoiceTurnTests(unittest.IsolatedAsyncioTestCase):
    def test_input_gain_preserves_zero_and_reports_overload(self):
        pcm = np.array([0, -100, 100, 32767, -32768], dtype="<i2").tobytes()
        self.assertEqual(gain_packet(pcm, 0), (pcm, 32768, 32768, 0))
        output, before, after, overload = gain_packet(pcm, 24)
        self.assertEqual((before, after, overload), (32768, 32768, 2))
        self.assertEqual(np.frombuffer(output, dtype="<i2").tolist(), [0, -1585, 1585, 32767, -32768])

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
        with patch("jarvis.conversation_trial.LiveSession", SimulatedSession),              patch("jarvis.conversation_trial.BenchReply", SimulatedReply),              patch("jarvis.conversation_trial.StreamingEcho", LocalEcho):
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
            await run_voice_turn(device, messages, result, opening, bytes(480 * 4), 31, lambda phase: None)
            self.assertEqual(messages, ["ready", samples[:320].tobytes(), samples[320:640].tobytes(), "closed"])
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
