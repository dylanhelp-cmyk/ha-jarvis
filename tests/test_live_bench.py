import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

from jarvis.audio import PCMFrame, RATE, make_probe
from jarvis.live_bench import match_response, render_reference, run_cloud_benchmark, voiced_bounds
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

    def test_handoff_bounds_exclude_a_later_copy_without_relaxing_agreement(self):
        reply = np.concatenate((make_probe(513), make_probe(514)))
        captured = np.zeros(8 * RATE, dtype="<i2")
        first = int(2.1 * RATE)
        later = 5 * RATE
        captured[first:first + len(reply)] = (reply * .35).astype("<i2")
        captured[later:later + len(reply)] = reply
        result = match_response(captured, 100, reply, 0, 101, 101.2,
                                handoff_at=lambda offset: 101.55 + offset / RATE,
                                acoustic_delay_range=(.25, .85))
        self.assertEqual(result["status"], "detected", result)
        self.assertAlmostEqual(result["latency_ms"], 1100, delta=12)

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

    def test_render_reference_preserves_silence_and_packet_sample_clock(self):
        stereo = np.zeros((44100, 2), dtype=">i2")
        stereo[11025:22050] = 3000
        reference = render_reference(stereo.tobytes())
        self.assertEqual(len(reference), RATE)
        start, end = voiced_bounds(reference)
        self.assertAlmostEqual(start, 4000, delta=160)
        self.assertAlmostEqual(end, 8000, delta=160)


class FakeSource:
    instances = []

    def __init__(self, *args, on_render):
        self.written = 0
        self.on_render = on_render
        self.queued_frames = 0
        self.closed = False
        self.telemetry = {"queued_frames": 0, "overflow_count": 0}
        self.instances.append(self)

    async def write(self, pcm):
        self.written += len(pcm)
        samples = np.frombuffer(pcm, dtype="<i2")
        stereo = np.repeat(samples, 6).astype(">i2")
        self.on_render(stereo.tobytes(), asyncio.get_running_loop().time())

    async def close(self):
        self.closed = True


class FakeSpeaker:
    @classmethod
    def for_wiim(cls, *args, **kwargs):
        return cls()

    instances = []

    def __init__(self, *args, **kwargs):
        self.stopped = self.closed = False
        self.instances.append(self)

    async def play(self, url, config):
        self.url = url
        self.source = await config["source_factory"](44100, 2, 2)
        self.source.first_read = asyncio.Event()
        self.source.first_read.set()

    async def wait_for_stream(self, ready, timeout):
        await asyncio.wait_for(ready.wait(), timeout)

    def check_failure(self):
        pass

    def snapshot(self):
        return {"transport": "airplay", "audible_onset_measured": False,
                "events": [{"event": "receiver_before_pcm", "volume_percent": 40}]}

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

    async def test_reply_timeout_preserves_provisional_acoustic_onset(self):
        detected = {"status": "detected", "latency_ms": 1200.0}
        with patch("jarvis.live_bench.QUIET_SECONDS", 10), \
             patch("jarvis.live_bench.match_response", return_value=detected):
            await run_cloud_benchmark(self.bench, "websocket", self.fixture)
        result = self.bench.results[0]
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["reply_completion"], "observation_timeout")
        self.assertTrue(result["trials"][0]["provisional"])
        self.assertEqual(result["trials"][0]["latency_ms"], 1200.0)
        self.assertTrue(result["cloud"]["finalization_confirmed"])

    async def test_startup_overlaps_and_fixture_waits_for_speaker_pull(self):
        capture_entered = asyncio.Event()
        release_capture = asyncio.Event()
        speaker_opened = asyncio.Event()
        original_play = FakeSpeaker.play

        async def begin(*args):
            capture_entered.set()
            await release_capture.wait()

        async def play(speaker, *args):
            await original_play(speaker, *args)
            speaker.source.first_read.clear()
            speaker_opened.set()

        self.bench.device.begin.side_effect = begin
        with patch.object(FakeSpeaker, "play", play):
            task = asyncio.create_task(run_cloud_benchmark(self.bench, "webrtc", self.fixture))
            try:
                await asyncio.wait_for(asyncio.gather(capture_entered.wait(), speaker_opened.wait()), 1)
                self.assertTrue(FakeLive.instances)
                release_capture.set()
                await asyncio.sleep(0)
                self.assertFalse(FakeLive.instances[0].sent)
                FakeSpeaker.instances[0].source.first_read.set()
                await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        result = self.bench.results[0]
        self.assertGreaterEqual(result["cold_ready_ms"], result["speaker_pull_ready_ms"])
        self.assertGreaterEqual(result["cold_ready_ms"], result["capture_ready_ms"])

    async def test_acoustic_trial_uploads_only_bounded_microphone_window(self):
        self.bench.results.append({
            "route": "airplay", "status": "measured", "requested_airplay_latency_ms": 500,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "echo": {"delay_assumed_ms": 10},
            "transport": {"receiver_after": {"volume_percent": 40}},
        })
        samples = np.full(160, 1000, dtype="<i2")

        async def feed():
            due = time.monotonic()
            while True:
                await asyncio.sleep(max(.001, due - time.monotonic()))
                if time.monotonic() < due:
                    continue
                consumer = getattr(self.bench.device, "pcm_consumer", None)
                if consumer:
                    consumer(PCMFrame(0, 0, int(due * 1000), samples), 0)
                due += .01

        def match(captured, capture_start, reference, offset, endpoint, earliest, **kwargs):
            return {"status": "detected", "audible_onset_at": earliest + .1, "latency_ms": 100}

        producer = asyncio.create_task(feed())
        try:
            with patch("jarvis.live_bench.match_response", side_effect=match) as matcher:
                await run_cloud_benchmark(self.bench, "websocket", self.fixture, microphone_input=True)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        result = self.bench.results[0]
        self.assertEqual(result["status"], "measured", result)
        self.assertEqual(matcher.call_count, 2)
        self.assertEqual(result["trials"][0]["metric"], "microphone_end_to_audible")
        live = FakeLive.instances[0]
        voiced = [block for block in live.sent if any(block)]
        self.assertGreater(len(voiced), 1)
        self.assertTrue(all(block == samples.tobytes() for block in voiced))
        self.assertEqual(result["uploaded_microphone_samples"], len(voiced) * len(samples))
        self.assertIsNone(self.bench.device.pcm_consumer)

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
