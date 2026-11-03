import asyncio
from contextlib import ExitStack
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

from jarvis.audio import RATE, make_probe
from jarvis.live_bench import match_response, run_cloud_benchmark, voiced_bounds
from jarvis.live_session import LiveMetrics


class ReplyMatchingTests(unittest.TestCase):
    def test_generated_reference_matches_acoustic_copy_with_window_offset_compensation(self):
        reply = np.concatenate((np.zeros(1600, dtype="<i2"), make_probe(513), make_probe(514)))
        captured = np.zeros(6 * RATE, dtype="<i2")
        offset = int(2.1 * RATE)
        captured[offset:offset + len(reply)] = (reply * .35).astype("<i2")
        result = match_response(captured, 100, reply, 1600, 101, 101.2)
        self.assertEqual(result["status"], "detected", result)
        self.assertAlmostEqual(result["fixture_end_to_audible_ms"], 1200, delta=12)
        self.assertGreaterEqual(sum(window["status"] == "detected" for window in result["windows"]), 2)

    def test_silence_and_short_reply_cannot_be_reported_as_zero_latency(self):
        capture = np.zeros(4 * RATE, dtype="<i2")
        for reply in (np.zeros(RATE, dtype="<i2"), make_probe(513)[:4000]):
            result = match_response(capture, 100, reply, 0, 101, 101.2)
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["latency_ms"])

    def test_contradictory_window_locations_are_rejected(self):
        reply = np.concatenate((make_probe(513), make_probe(514)))
        fake = [{"status": "detected", "offset_seconds": value, "confidence": .99}
                for value in (1, 2, 3, 4)]
        with patch("jarvis.live_bench.detect_probe", side_effect=fake):
            result = match_response(np.zeros(8 * RATE), 100, reply, 0, 101, 101.1)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("contradict", result["reason"])

    def test_known_fixture_energy_trims_head_and_tail(self):
        samples = np.concatenate((np.zeros(320), np.full(640, 2000), np.zeros(480))).astype("<i2")
        self.assertEqual(voiced_bounds(samples), (320, 960))


class FakeSource:
    instances = []

    def __init__(self, *args):
        self.written = 0
        self.queued_frames = 0
        self.closed = False
        self.telemetry = {"queued_frames": 0, "overflow_count": 0}
        self.instances.append(self)

    async def write(self, pcm):
        self.written += len(pcm)

    async def close(self):
        self.closed = True


class FakeSpeaker:
    instances = []

    def __init__(self, *args, **kwargs):
        self.stopped = self.closed = False
        self.instances.append(self)

    async def play(self, url, config):
        self.url = url
        self.source = await config["source_factory"](44100, 2, 2)

    def check_failure(self):
        pass

    def snapshot(self):
        return {"transport": "airplay", "audible_onset_measured": False}

    async def stop(self):
        self.stopped = True

    async def close(self):
        self.closed = True


class FakeLive:
    instances = []
    final_confirmed = True
    wait_forever = False
    first_send = None

    def __init__(self, settings, *, transport, on_audio, **kwargs):
        self.output = on_audio
        self.metrics = LiveMetrics(transport, ready_ms=5)
        self.sent = []
        self.replied = False
        self.closed = False
        self.instances.append(self)

    async def start(self):
        await asyncio.sleep(0)

    async def send_audio(self, pcm):
        self.sent.append(pcm)
        if self.first_send:
            self.first_send.set()
        if self.wait_forever:
            await asyncio.Event().wait()
        if not any(pcm) and not self.replied:
            self.replied = True
            await self.output(make_probe(513).tobytes(), RATE)

    async def close(self):
        self.closed = True
        self.metrics.finalization_confirmed = self.final_confirmed
        self.metrics.usage_seconds = 1
        return self.metrics


class CloudBenchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("LiveSession", FakeLive), ("AirPlayTransport", FakeSpeaker),
                            ("LiveAudioSource", FakeSource), ("QUIET_SECONDS", .001),
                            ("PHYSICAL_TAIL_SECONDS", 0), ("POST_INPUT_SECONDS", .3)):
            self.stack.enter_context(patch("jarvis.live_bench." + name, value))
        FakeLive.instances.clear()
        FakeSpeaker.instances.clear()
        FakeSource.instances.clear()
        FakeLive.wait_forever = False
        FakeLive.final_confirmed = True
        FakeLive.first_send = asyncio.Event()
        device = SimpleNamespace(frames=["local-only-frame"], errors=[], capture_session={"boot_id": "offline"},
                                 clock_rtt_ms=8, begin=AsyncMock(), end=AsyncMock(),
                                 recording=lambda: (np.full(3 * RATE, 17000, dtype="<i2"), 0),
                                 capture_diagnostics=lambda: {"frames": 1, "errors": []})
        self.bench = SimpleNamespace(test={"id": "offline-run", "running": True, "phase": "starting"},
                                     results=[], device=device, transport=None, stream=None,
                                     config={"wiim_host": "offline.invalid", "verify_tls": True, "airplay_latency_ms": 500},
                                     airplay_discovery_cache={}, cloud_settings=object())
        self.fixture = np.concatenate((np.zeros(320), np.full(640, 3000), np.zeros(320))).astype("<i2").tobytes()

    async def test_fixture_only_upload_and_continuous_silence_with_honest_unavailable_measurement(self):
        unavailable = {"status": "unavailable", "latency_ms": None, "reason": "weak room match"}
        with patch("jarvis.live_bench.match_response", return_value=unavailable) as match:
            await run_cloud_benchmark(self.bench, "websocket", self.fixture)
        result = self.bench.results[0]
        self.assertEqual(result["kind"], "cloud_latency")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["summary"]["valid_trials"], 0)
        self.assertEqual(result["trials"][0]["metric"], "fixture_end_to_audible")
        sent = FakeLive.instances[0].sent
        self.assertEqual(b"".join(block for block in sent if any(block)), np.full(640, 3000, dtype="<i2").tobytes())
        self.assertGreaterEqual(sum(not any(block) for block in sent), 2)
        match.assert_called_once()
        self.assertFalse(self.bench.device.frames)
        self.assertIsNone(self.bench.transport)
        self.assertEqual(self.bench.test, {"running": False, "phase": "idle"})
        self.assertTrue(FakeSource.instances[0].closed)
        self.assertTrue(FakeSpeaker.instances[0].closed and FakeSpeaker.instances[0].stopped)
        self.assertTrue(FakeLive.instances[0].closed)
        json.dumps(result)  # Results retain scalar diagnostics, never arrays/PCM.

    async def test_unconfirmed_cloud_finalization_cannot_pass_acoustic_result(self):
        FakeLive.final_confirmed = False
        with patch("jarvis.live_bench.match_response") as match:
            await run_cloud_benchmark(self.bench, "webrtc", self.fixture)
        self.assertEqual(self.bench.results[0]["status"], "failed")
        self.assertFalse(self.bench.results[0]["cloud"]["finalization_confirmed"])
        match.assert_not_called()
        self.assertFalse(self.bench.device.frames)

    async def test_disconnect_cancellation_keeps_failed_status_partial_health_and_cleans_audio(self):
        FakeLive.wait_forever = True
        task = asyncio.create_task(run_cloud_benchmark(self.bench, "webrtc", self.fixture))
        await asyncio.wait_for(FakeLive.first_send.wait(), 1)
        self.bench.test["interruption"] = {"reason": "device_disconnected", "phase": "sending fixture"}
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = self.bench.results[0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["capture_at_end"]["frames"], 1)
        self.assertEqual(result["interruption"]["reason"], "device_disconnected")
        self.assertTrue(FakeSpeaker.instances[0].stopped)
        self.assertTrue(FakeLive.instances[0].closed)
        self.assertFalse(self.bench.device.frames)

    async def test_silent_fixture_fails_before_cloud_or_speaker_start(self):
        await run_cloud_benchmark(self.bench, "websocket", bytes(3200))
        self.assertEqual(self.bench.results[0]["status"], "failed")
        self.assertFalse(FakeLive.instances)
        self.assertFalse(FakeSpeaker.instances)
