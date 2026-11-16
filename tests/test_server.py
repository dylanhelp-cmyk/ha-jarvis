import asyncio
import hashlib
import json
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import wave
import zipfile

from aiohttp.test_utils import TestClient, TestServer
from jarvis.server import Bench, bench_signal, create_ui, create_audio, validate_config
from jarvis.device import Device
from jarvis.audio import FRAME, RATE, make_probe
from jarvis.playback import HEADER
import numpy as np


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.bench = Bench(self.folder.name)
        self.ui = TestClient(TestServer(create_ui(self.bench)))
        self.audio = TestClient(TestServer(create_audio(self.bench)))
        await self.ui.start_server()
        await self.audio.start_server()

    async def asyncTearDown(self):
        await self.bench.cancel()
        await self.audio.close()
        await self.ui.close()
        self.folder.cleanup()

    async def test_first_load_does_not_capture_or_open_cloud(self):
        response = await self.ui.get('/api/status')
        body = await response.json()
        self.assertEqual(response.status, 200)
        self.assertFalse(body['cloud']['enabled'])
        self.assertFalse(body['test']['running'])
        self.assertNotIn('device_token', body['config'])

    async def test_wake_match_reports_greeting_end_only_with_timed_render(self):
        device = self.bench.device
        device.clock_offset_ms = 0
        device.clock_rtt_ms = 4
        wake = {"device_time_ms": 1000, "ack_cache_id": "clip"}
        terminal = {"drained": True, "rendered_samples": 24_000, "first_rendered_ms": 1030,
                    "last_rendered_ms": 1530, "max_render_discontinuity_us": 0}
        pcm = bytes(24_000 * 4)
        capture = np.zeros(3 * RATE, dtype=np.int16)
        reference = np.zeros(RATE // 2, dtype=np.int16)
        with (patch.object(self.bench, "alignment_delay", return_value=32),
              patch("jarvis.live_bench.render_reference", return_value=reference),
              patch("jarvis.live_bench.voiced_bounds", return_value=(RATE // 10, 4 * RATE // 10)),
              patch("jarvis.live_bench.match_response",
                    side_effect=lambda *_args, **_kwargs: {"status": "detected", "latency_ms": 162}) as match):
            result, detected_at, _ = await self.bench.match_wake_audio(wake, terminal, "clip", pcm, capture, 1)
            self.assertEqual((detected_at, result["estimated_end_ms"]), (1, 462))
            self.assertLess(len(match.call_args.args[0]), len(capture))
            terminal["max_render_discontinuity_us"] = 20_000
            result, _, _ = await self.bench.match_wake_audio(wake, terminal, "clip", pcm, capture, 1)
            self.assertNotIn("estimated_end_ms", result)

    async def test_realtime_benchmark_uses_neither_microphone_nor_speaker(self):
        from types import SimpleNamespace

        async def benchmark(_device, _settings, _transport, _id, _fixtures, timings, _stage, _profile):
            player = SimpleNamespace(snapshot=lambda: {})
            timings.append((1.0, SimpleNamespace(first_voice_at=1.4, player=player), True))
            return {"playback_enabled": False, "failure": None}

        self.bench.test = {"id": "digital", "running": True}
        with (patch.object(self.bench.device, "begin") as begin,
              patch("jarvis.live_bench.synthesize_fixture", return_value=(bytes(640), 320)),
              patch("jarvis.live_bench.benchmark_replies", side_effect=benchmark)):
            await self.bench.run("synthetic_replies", 2, "realtime")
        begin.assert_not_called()
        result = self.bench.results[0]
        self.assertEqual(result["route"], "digital")
        self.assertNotIn("capture", result)
        self.assertEqual(result["trials"][0]["digital_end_to_cloud_voice_ms"], 400)
        self.assertIsNone(result["trials"][0]["latency_ms"])

    async def test_standby_rearms_locally_and_waits_for_a_real_wake_before_cloud(self):
        from unittest.mock import AsyncMock

        device = self.bench.device
        second_arm = asyncio.Event()
        third_arm = asyncio.Event()
        arms = []
        device.info["wake_capture_protocol"] = 1

        async def begin(identity, _duration, *, arm_timeout_ms):
            self.assertEqual(arm_timeout_ms, 120_000)
            arms.append(identity)
            device.awaiting_wake = True
            device.stopped.clear()
            if len(arms) == 1:
                device.arm_stop_reason = "arm_timeout"
                device.stopped.set()
            elif len(arms) == 2:
                second_arm.set()
            elif len(arms) == 3:
                third_arm.set()

        context = None
        token = "test.assist.standby"
        assist = AsyncMock()
        assist.__aenter__.return_value = assist
        assist.list_tools.return_value = [{"name": "HassTurnOn", "inputSchema": {"type": "object"}}]

        async def voice_turn(_device, _settings, result, *_args, **kwargs):
            nonlocal context
            self.assertTrue(device.wake_event.is_set())
            self.assertTrue(kwargs["follow_up"])
            self.assertTrue(kwargs["after_greeting"])
            self.assertEqual(kwargs["idle_seconds"], 6)
            self.assertEqual(result["standby_idle_seconds"], 6)
            self.assertIsNone(kwargs["wait_for_preconnected_wake"])
            self.assertIs(kwargs["assist_delegation"].client, assist)
            self.assertEqual(result["assist_standby"], True)
            context = kwargs["standby_context"]
            self.assertIsNotNone(context)
            context.save((("Private question", "Private reply"),))
            result["cloud"] = {"finalization_confirmed": True}
            result["status"] = "unavailable"
            return []

        ready = {"connected": True, "muted": False, "raw_ready": True,
                 "jack_plugged": True, "wake_capture_protocol": 1, "playback_protocol": 1}
        self.bench.cloud_settings.set_key("sk-local-standby-test-key")
        response = await self.ui.put("/api/config", json={"standby_idle_seconds": 2})
        self.assertEqual(response.status, 400)
        response = await self.ui.put("/api/config", json={"standby_idle_seconds": 6})
        self.assertEqual(response.status, 200)
        self.assertEqual(Bench(self.folder.name).config["standby_idle_seconds"], 6)
        with (patch.object(device, "snapshot", return_value=ready),
              patch.object(device, "begin", side_effect=begin),
              patch.object(device, "end", new_callable=AsyncMock),
              patch.object(self.bench, "alignment_delay", return_value=35),
              patch.object(self.bench, "acknowledgment_status", return_value={"cached_on_device": True}),
              patch("jarvis.server.AssistClient", return_value=assist) as assist_client,
              patch("jarvis.server.verified_render", return_value=("clip", b"pcm")),
              patch("jarvis.conversation_trial.run_voice_turn", side_effect=voice_turn) as cloud):
            body = {"kind": "voice_turn", "standby": True, "after_greeting": True, "confirm_paid": True,
                    "allow_room_audio_to_openai": True, "comfortable_volume_confirmed": True}
            response = await self.ui.post("/api/tests", json=body)
            self.assertEqual(response.status, 400)
            self.assertIsNone(self.bench.task)
            body["allow_repeated_wake_audio"] = True
            body["remember_context"] = True
            response = await self.ui.post("/api/tests", json={**body, "standby": False})
            self.assertEqual(response.status, 400)
            body["assist_standby_token"] = token
            response = await self.ui.post("/api/tests", json=body)
            self.assertEqual(response.status, 400)
            assist_client.assert_not_called()
            body["allow_assist_standby"] = True
            response = await self.ui.post("/api/tests", json=body)
            self.assertEqual(response.status, 202)
            await asyncio.wait_for(second_arm.wait(), 1)
            cloud.assert_not_called()
            self.assertEqual(assist.list_tools.await_count, 2)
            self.assertEqual(len(self.bench.results), 1)
            device.wake_details = {"ack_cache_id": "clip"}
            device.wake_event.set()
            await asyncio.wait_for(third_arm.wait(), 1)
            cloud.assert_awaited_once()
            await self.bench.cancel()
            self.assertFalse(self.bench.test["running"])
            self.assertEqual(len(set(arms)), 3)
            self.assertEqual(context.take(), ())
            self.assertEqual(assist.list_tools.await_count, 3)
            assist.call_tool.assert_not_awaited()
            for path in ("/api/status", "/api/results", "/api/diagnostics"):
                response = await self.ui.get(path)
                self.assertNotIn(token, await response.text())

    async def test_alignment_retries_a_discontinuous_render_and_requires_two_matches(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        device = self.bench.device
        device.clock_offset_ms = 0
        device.clock_rtt_ms = 2
        device.info.update(ack_cache_id="clip", diagnostics={"boot_id": "boot"})
        reference = np.zeros(RATE, dtype=np.int16)
        players = [SimpleNamespace(send=AsyncMock(), stop=AsyncMock(), confirmed_span=lambda: (1000, 2000),
                                   max_render_discontinuity_us=gap, snapshot=lambda: {})
                   for gap in (20_000, 0, 0)]
        result = {"id": "alignment", "route": "voice_pe_internal", "trials": []}

        def match(_capture, _capture_start, _reference, _voice_start, started, *_args, **_kwargs):
            return {"status": "detected", "audible_onset_at": started + .033}

        with (patch.object(device, "end", new_callable=AsyncMock),
              patch.object(device, "recording", return_value=(np.zeros(3 * RATE, dtype=np.int16), 0)),
              patch("jarvis.server.verified_render", return_value=("clip", b"pcm")),
              patch("jarvis.server.JackPlayback", side_effect=players) as playback,
              patch("jarvis.server.asyncio.sleep", new_callable=AsyncMock),
              patch("jarvis.live_bench.render_reference", return_value=reference),
              patch("jarvis.live_bench.voiced_bounds", return_value=(0, len(reference))),
              patch("jarvis.live_bench.match_response", side_effect=match) as acoustic,
              patch("jarvis.streaming_aec.check_confirmed_references",
                    return_value={"status": "processed", "estimated_delay_ms": 33}) as alignment):
            await self.bench.measure_render_alignment(result)

        self.assertEqual(playback.call_count, 3)
        self.assertEqual(acoustic.call_count, 2)
        self.assertEqual(len(alignment.call_args.args[1]), 2)
        self.assertEqual([trial["status"] for trial in result["trials"]], ["unavailable", "detected", "detected"])
        self.assertEqual(self.bench.echo_alignment, ("boot", "clip", "voice_pe_internal", 33))

    async def test_interruption_probe_records_early_candidate_and_still_measures_stop(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from jarvis.audio import PCMFrame

        device = self.bench.device
        start_ms = int(time.monotonic() * 1000)
        samples = np.full(256, 100, dtype="<i2")
        device.frames.append(PCMFrame(0, 0, start_ms, samples))
        reference = np.tile(samples, 4)
        result = {"id": "interrupt", "trials": []}
        control = {"id": "interrupt-control", "trials": []}
        playback_only = False

        async def send(_pcm, **_options):
            loud_start = 1520 if playback_only else 720
            for index in range(1, 115):
                observe = device.pcm_consumer
                frame_samples = samples * 3 if loud_start <= index * 16 < loud_start + 160 else samples
                observe(PCMFrame(index, index * 256, start_ms + index * 16, frame_samples), 0)
            if not playback_only:
                await asyncio.Future()

        player = SimpleNamespace(first_rendered_ms=start_ms, last_rendered_ms=start_ms + 1730, reason="requested",
                                 send=send, stop=AsyncMock(), snapshot=lambda: {})
        echo = SimpleNamespace(error=None, finished=True, rendered_frames=192000,
                               prime_capture=lambda *_: samples.tobytes(),
                               offer=lambda frame, _: frame.samples.tobytes())
        self.bench.echo_band_profile = SimpleNamespace(third_band_ratio=lambda mic, _: np.mean(abs(mic)) / 175)
        detector = SimpleNamespace(observe=lambda *_: True)
        with (patch.object(self.bench, "alignment_delay", return_value=35),
              patch.object(device, "end", new_callable=AsyncMock),
              patch.object(device, "recording", return_value=(reference, 0)),
              patch("jarvis.server.JackPlayback", return_value=player),
              patch("jarvis.server.detect_stop") as pilot_stop,
              patch("jarvis.server.asyncio.sleep", new_callable=AsyncMock),
              patch("jarvis.conversation_trial.SpeechEndpoint", return_value=detector),
              patch("jarvis.live_bench.synthesize_fixture", return_value=(reference.tobytes(), 0)) as synthesize,
              patch("jarvis.live_bench.voiced_bounds", return_value=(0, len(reference))),
              patch("jarvis.live_bench.render_reference", return_value=np.zeros(4 * RATE)),
              patch("jarvis.live_bench.match_response", return_value={"status": "detected", "latency_ms": 40}),
              patch("jarvis.streaming_aec.ConfirmedRenderInput", return_value=echo)):
            device.clock_offset_ms = 0
            await self.bench.measure_interrupt(result, spoken_reply=True)
            synthesize.reset_mock()
            playback_only = True
            player.reason = "drained"
            await self.bench.measure_interrupt(control, spoken_reply=True, playback_only=True)

        self.assertEqual(result["status"], "unavailable")
        self.assertGreater(result["interruption_probe"]["band_candidates_rejected"], 0)
        self.assertLess(result["interruption_probe"]["early_candidate_relative_to_injection_ms"], 0)
        self.assertGreater(result["interruption_probe"]["trigger_relative_to_injection_ms"], 0)
        self.assertEqual(result["trials"][0]["latency_ms"], 40)
        self.assertIsNone(result["interruption_probe"]["speaker_stop_acoustic_ms"])
        pilot_stop.assert_not_called()
        synthesize.assert_awaited_once()
        self.assertEqual(control["status"], "failed")
        self.assertGreater(control["interruption_probe"]["first_candidate_after_render_ms"], 1400)
        self.assertNotIn("injection_to_stop_request_ms", control["interruption_probe"])

    async def test_acoustic_interruption_requires_match_and_checks_opening_independently(self):
        from types import SimpleNamespace
        from jarvis.interruption import InterruptionTimeline

        self.bench.device.clock_offset_ms = 5_000
        player = SimpleNamespace(first_rendered_ms=1_000, last_rendered_ms=5_000)
        phrase = np.full(RATE, 800, dtype="<i2")
        trace = InterruptionTimeline()
        trace.record(1020, 1140, True, False, False)
        trace.record(1080, 1190, True, True, True)
        found = {"status": "detected", "audible_onset_at": 7, "window_spread_ms": 2}
        for match, prefix, expected in (({"status":"unavailable"}, None, "unavailable"),
                                        (found, {"status":"unavailable"}, "unavailable"),
                                        (found, {"status":"detected", "confidence":.9}, "measured")):
            with self.subTest(expected=expected, match=match, prefix=prefix):
                result, report = {"status": "unavailable"}, {}
                with (patch("jarvis.live_bench.match_response", return_value=match),
                      patch("jarvis.server.detect_probe", return_value=prefix) as cleaned):
                    await self.bench.measure_acoustic_interruption(result, report, trace, phrase, 6.7,
                                                                   player, phrase, 6, 31, phrase)
                self.assertEqual(result["status"], expected)
                if prefix is None:
                    cleaned.assert_not_called()
                    self.assertNotIn("near_end_source_after_render_ms", report)
                else:
                    self.assertEqual(report["evidence_timing"]["first_joint_decision_ms"], 190)
                    self.assertEqual(report["cleaned_opening"]["status"], prefix["status"])

    async def test_browser_fixture_endpoint_serves_generated_pcm_only(self):
        pcm = bytes(2 * RATE)
        with patch("jarvis.live_bench.synthesize_fixture", return_value=(pcm, RATE)) as synthesize:
            response = await self.ui.get("/api/interruption-source/1.wav")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.content_type, "audio/wav")
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            with wave.open(io.BytesIO(await response.read()), "rb") as clip:
                self.assertEqual((clip.getframerate(), clip.getnframes(), clip.getnchannels()), (RATE, RATE, 1))
            self.assertEqual((await self.ui.get("/api/interruption-source/9.wav")).status, 404)
            synthesize.assert_awaited_once_with("Hold on, can you tell me the time?")
        self.assertFalse(self.bench.device.frames)

    async def test_local_speech_recovery_requires_acoustic_evidence_after_the_pause(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        fixture = (np.full(RATE, 1000, dtype="<i2").tobytes(), RATE - 1)
        sleep = AsyncMock()
        captured = np.zeros(4 * RATE, dtype=np.int16)
        self.bench.device.clock_offset_ms = 0

        async def send(chunks, **options):
            self.assertEqual(options, {"prime_packets": 5, "source_prime_packets": 12, "recover_reply_stalls": True})
            sizes = []
            async for chunk in chunks:
                sizes.append(len(chunk))
                if len(sizes) in (10, 11):
                    self.assertEqual(sleep.await_count, 1 if len(sizes) == 10 else 2)
            self.assertEqual(sizes, [19200] * 20)

        player = SimpleNamespace(send_stream=send, stop=AsyncMock(), frames_rendered=96000,
                                 schedule_rebases=1, snapshot=lambda: {"reason": "drained"},
                                 accepted_at=lambda frame, _: frame / 48000)
        calls = []

        def match(*_, handoff_at, **__):
            calls.append(handoff_at(0))
            return {"status": "detected", "windows": [
                {"status": "detected"}, {"status": "detected"},
                {"status": "unavailable" if len(calls) == 2 else "detected"}]}

        result = {"id": "local-recovery", "trials": []}
        with (patch("jarvis.server.JackPlayback", return_value=player),
              patch("jarvis.server.asyncio.sleep", sleep),
              patch.object(self.bench.device, "end", new_callable=AsyncMock),
              patch.object(self.bench.device, "recording", return_value=(captured, 0)),
              patch("jarvis.live_bench.synthesize_fixture", return_value=fixture),
              patch("jarvis.live_bench.match_response", side_effect=match)):
            await self.bench.measure_reply_stall(result)
        self.assertEqual(calls, [0, 1])
        self.assertEqual(sleep.await_args_list[1].args, (.25,))
        self.assertEqual([trial["status"] for trial in result["trials"]], ["detected", "unavailable"])
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(self.bench.device.playback)

    async def test_cloud_key_stays_local_and_never_enters_read_or_diagnostic_responses(self):
        key = 'sk-test-local-only-' + 'x' * 32
        response = await self.ui.put('/api/cloud', json={'api_key':key})
        value = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(value['api_key_configured'])
        self.assertFalse(value['enabled'])
        self.assertEqual(value['budget_usd'], 5)
        self.assertEqual(Bench(self.folder.name).cloud_settings.api_key, key)
        for path in ('/api/cloud', '/api/status', '/api/config', '/api/diagnostics'):
            response = await self.ui.get(path)
            self.assertNotIn(key, await response.text())
        response = await self.ui.put('/api/cloud', json={'api_key':'too short'})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.bench.cloud_settings.api_key, key)

    async def test_assist_check_uses_a_fixed_origin_and_discards_the_token(self):
        token = 'test.home-assistant.token'
        origin = 'https://home-assistant.test:8123'
        with patch.dict('os.environ', {'JARVIS_ASSIST_ORIGIN': origin}), \
             patch('jarvis.server.discover_tools', return_value=[{'name': 'HassTurnOn'}]) as discover:
            response = await self.ui.post('/api/assist/check', json={'token': token})
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {'tool_count': 1})
            discover.assert_awaited_once_with(origin, token)
            discover.reset_mock()
            response = await self.ui.post('/api/assist/check', json={'token': token, 'origin': 'http://other'})
            self.assertEqual(response.status, 400)
            discover.assert_not_awaited()
        diagnostics = await self.ui.get('/api/diagnostics')
        self.assertNotIn(token, await diagnostics.text())
        for path in Path(self.folder.name).glob('*.json'):
            self.assertNotIn(token, path.read_text())

    async def test_synthetic_fixture_is_memory_only_and_paid_start_is_gated(self):
        import base64
        response = await self.ui.put('/api/cloud/fixture', json={'pcm_b64':base64.b64encode(bytes(RATE * 2)).decode()})
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())['retained_on_disk'])
        self.assertEqual(len(self.bench.cloud_fixture), RATE * 2)
        self.assertFalse(Bench(self.folder.name).cloud_fixture)
        response = await self.ui.post('/api/cloud/tests', json={'transport':'webrtc', 'comfortable_volume_confirmed':True})
        self.assertEqual(response.status, 503)
        self.assertIsNone(self.bench.task)
        self.assertIsNone(self.bench.cloud_settings.pending)
        response = await self.ui.put('/api/cloud/fixture', json={'pcm_b64':'invalid!'})
        self.assertEqual(response.status, 400)

    async def test_test_requires_user_confirmation_and_paired_mic(self):
        response = await self.ui.post('/api/tests', json={'kind':'latency'})
        self.assertEqual(response.status, 400)
        response = await self.ui.post('/api/tests', json={'kind':'latency', 'comfortable_volume_confirmed':True})
        self.assertEqual(response.status, 400)
        self.assertIsNone(self.bench.task)

    async def test_legacy_settings_migrate_without_resetting_pairing(self):
        original = self.bench.config['device_token']
        self.bench.path.write_text(json.dumps({
            'device_token': original, 'stream_base_url': 'http://192.168.1.10:8099',
            'route': 'airplay', 'wiim_host': '192.168.1.20', 'repetitions': 5}))
        restarted = Bench(self.folder.name)
        self.assertEqual(restarted.config, {
            'device_token': original, 'ha_lan_url': 'http://192.168.1.10:8099', 'repetitions': 5,
            'standby_idle_seconds': 10})
        self.assertEqual(restarted.firmware_secrets, self.bench.firmware_secrets)

    async def test_playback_requires_a_connected_jack_and_compatible_firmware(self):
        ws = await self.audio.ws_connect('/device')
        await ws.send_json({'type':'hello','protocol':1,'token':self.bench.config['device_token'],
                            'sample_rate':RATE,'channels':1,'raw_ready':True,
                            'playback_protocol':1,'jack_plugged':False})
        await ws.receive_json()
        response = await self.ui.post('/api/tests', json={'kind':'latency', 'comfortable_volume_confirmed':True})
        self.assertEqual(response.status, 400)
        self.assertIsNone(self.bench.task)
        await ws.close()

    async def test_internal_speaker_bench_uses_paired_socket_for_pcm_and_drain(self):
        ws = await self.audio.ws_connect('/device')
        await ws.send_json({'type':'hello','protocol':1,'token':self.bench.config['device_token'],
                            'sample_rate':RATE,'channels':1,'raw_ready':True,'muted':False,
                            'playback_protocol':1,'jack_plugged':False,'output_route':'voice_pe_internal'})
        await ws.receive_json()
        generation = None
        sent = 0

        async def peer():
            nonlocal generation, sent
            async for frame in ws:
                if isinstance(frame.data, bytes):
                    magic, _, _, count, packet_generation, _, first = HEADER.unpack_from(frame.data)
                    self.assertEqual((magic, packet_generation, first), (b'JPLY', generation, sent))
                    sent += count
                    await ws.send_json({'type':'play_render','playback_id':playback_id,
                        'generation':generation,'first_sample':first,'samples':count,
                        'device_time_ms':int(time.monotonic() * 1000)})
                    continue
                command = json.loads(frame.data)
                kind = command['type']
                if kind == 'clock_sync':
                    await ws.send_json({'type':kind,'id':command['id'],
                        'device_time_ms':int(time.monotonic() * 1000)})
                elif kind == 'start_capture':
                    capture_id = command['capture_id']
                    await ws.send_json({'type':'capture_started','capture_id':capture_id})
                    await ws.send_bytes(FRAME.pack(b'JPCM',1,1,320,0,0,int(time.monotonic()*1000)) + bytes(640))
                elif kind == 'play_start':
                    playback_id, generation = command['playback_id'], command['generation']
                    await ws.send_json({**command,'type':'play_started'})
                elif kind == 'play_drain':
                    await ws.send_json({**command,'type':'play_drained','rendered_samples':sent,
                                        'last_rendered_ms':int(time.monotonic()*1000)})
                elif kind == 'stop_capture':
                    await ws.send_json({'type':'capture_stopped','capture_id':capture_id,
                        'reason':'requested','dropped_frames':0})
                    return

        simulator = asyncio.create_task(peer())
        try:
            with patch('jarvis.server.bench_signal', return_value=(np.zeros(320, dtype='<i2'), {})):
                response = await self.ui.post('/api/tests', json={'kind':'latency', 'comfortable_volume_confirmed':True})
                self.assertEqual(response.status, 202)
                await asyncio.wait_for(self.bench.task, 4)
                await asyncio.wait_for(simulator, 2)
            result = self.bench.results[0]
            self.assertEqual(result['route'], 'voice_pe_internal')
            self.assertEqual(result['playback']['frames_rendered'], 960)
            self.assertEqual(result['status'], 'unavailable')
            self.assertIsNone(self.bench.device.playback)
        finally:
            simulator.cancel()
            await asyncio.gather(simulator, return_exceptions=True)
            await ws.close()

    async def test_room_voice_preview_requires_separate_explicit_authorization(self):
        ws = await self.audio.ws_connect("/device")
        try:
            await ws.send_json({"type": "hello", "protocol": 1, "token": self.bench.config["device_token"],
                                "sample_rate": RATE, "channels": 1, "raw_ready": True, "muted": False,
                                "jack_plugged": True, "playback_protocol": 1, "wake_capture_protocol": 1})
            await ws.receive_json(timeout=2)
            response = await self.ui.post("/api/tests", json={"kind": "voice_turn", "confirm_paid": True,
                                          "comfortable_volume_confirmed": True})
            self.assertEqual(response.status, 400)
            self.assertIn("Explicitly authorize", await response.text())
            self.assertIsNone(self.bench.task)
            self.assertIsNone(self.bench.device.capture_id)
        finally:
            await ws.close()

    async def test_armed_capture_preserves_first_samples_and_disarms(self):
        from jarvis.opening_audio import OpeningAudio
        ws = await self.audio.ws_connect("/device")
        device = self.bench.device
        try:
            await ws.send_json({"type": "hello", "protocol": 1, "token": self.bench.config["device_token"],
                                "sample_rate": RATE, "channels": 1, "raw_ready": True, "muted": False,
                                "wake_capture_protocol": 1})
            await ws.receive_json(timeout=2)
            device.clock_offset_ms = 0
            opening = OpeningAudio()
            device.pcm_consumer = opening.offer
            arm = asyncio.create_task(device.begin("wake-1", 5000, clock_synced=True, arm_timeout_ms=32000))
            command = await ws.receive_json(timeout=2)
            self.assertEqual(command["type"], "arm_wake_capture")
            await ws.send_json({"type": "wake_capture_armed", "capture_id": "wake-1"})
            await arm
            self.assertTrue(device.snapshot()["wake_capture_armed"])
            self.assertFalse(device.snapshot()["capturing"])
            await ws.send_json({"type": "wake_word", "capture_id": "different", "wake_word": "Hey Jarvis"})
            stamp = int(time.monotonic() * 1000)
            await ws.send_json({"type": "wake_word", "capture_id": "wake-1",
                                "wake_word": "Hey Jarvis", "device_time_ms": stamp})
            await asyncio.wait_for(device.wake_event.wait(), 2)
            self.assertEqual(device.wake_details["capture_id"], "wake-1")
            await ws.send_json({"type": "capture_started", "capture_id": "wake-1"})
            await asyncio.wait_for(device.started.wait(), 2)
            pcm = np.arange(320, dtype="<i2").tobytes()
            await ws.send_bytes(FRAME.pack(b"JPCM", 1, 1, 320, 0, 0, stamp) + pcm)
            ending = asyncio.create_task(device.end())
            self.assertEqual((await ws.receive_json(timeout=2))["type"], "stop_capture")
            await ws.send_json({"type": "capture_stopped", "capture_id": "wake-1",
                                "reason": "requested", "dropped_frames": 0})
            await ending
            frames, result = opening.release(time.monotonic())
            self.assertEqual(result["status"], "ready")
            self.assertEqual(frames[0].samples.tobytes(), pcm)

            arm = asyncio.create_task(device.begin("wake-2", 5000, clock_synced=True, arm_timeout_ms=32000))
            await ws.receive_json(timeout=2)
            await ws.send_json({"type": "wake_capture_armed", "capture_id": "wake-2"})
            await arm
            ending = asyncio.create_task(device.end())
            self.assertEqual((await ws.receive_json(timeout=2))["capture_id"], "wake-2")
            await ws.send_json({"type": "wake_capture_disarmed", "capture_id": "wake-2", "reason": "requested"})
            await ending
            self.assertFalse(device.snapshot()["wake_capture_armed"])
            self.assertEqual(device.frames, [])
        finally:
            await ws.close()

    async def test_verified_greeting_retries_a_timed_out_transfer_without_playing_it(self):
        from jarvis.acknowledgment import cache_path, candidate_identity, greeting_playback_identity
        pcm = np.full(RATE * 2, 900, dtype="<i2").tobytes()
        cache_path(self.bench.data_dir).write_bytes(pcm)
        identity = candidate_identity(pcm)
        playback_id = greeting_playback_identity(identity)
        previous_playback_id = hashlib.sha256(f"{identity}:gain:-6".encode("ascii")).hexdigest()
        preview = await self.ui.get("/api/acknowledgment/preview", params={"clip": identity})
        self.assertEqual(preview.status, 200)
        with wave.open(io.BytesIO(await preview.read()), "rb") as wav:
            self.assertEqual(wav.getnframes(), RATE * 2)
            self.assertEqual(int.from_bytes(wav.readframes(1), "little", signed=True), 285)
        bad = await self.ui.post("/api/acknowledgment/verify",
                                 json={"clip": "stale", "heard_exact_phrase": True})
        self.assertEqual(bad.status, 400)
        approved = await self.ui.post("/api/acknowledgment/verify",
                                      json={"clip": identity, "heard_exact_phrase": True})
        self.assertTrue((await approved.json())["verified"])
        ws = await self.audio.ws_connect("/device")
        try:
            await ws.send_json({"type": "hello", "protocol": 1, "token": self.bench.config["device_token"],
                                "sample_rate": RATE, "channels": 1, "ack_cache_protocol": 1,
                                "ack_cache_id": previous_playback_id, "ack_cache_max_frames": 144000})
            self.assertEqual((await ws.receive_json(timeout=2))["type"], "hello_ok")
            first_start = await ws.receive_json(timeout=2)
            self.assertEqual(first_start["type"], "ack_cache_start")
            self.assertEqual(first_start["samples"], 96000)
            abort = await ws.receive_json(timeout=4)
            self.assertEqual(abort, {"type": "ack_cache_abort", "generation": first_start["generation"]})
            self.assertFalse(self.bench.acknowledgment_status()["cached_on_device"])
            start = await ws.receive_json(timeout=2)
            self.assertEqual(start["type"], "ack_cache_start")
            self.assertNotEqual(start["cache_id"], previous_playback_id)
            self.assertEqual(start["cache_id"], playback_id)
            generation = start["generation"]
            self.assertNotEqual(generation, first_start["generation"])
            await ws.send_json({"type": "ack_cache_ready", "generation": first_start["generation"]})
            await ws.send_json({"type": "ack_cache_ready", "generation": generation})
            sent = sequence = 0
            while sent < start["samples"]:
                frame = await ws.receive_bytes(timeout=2)
                magic, _, _, count, packet_generation, packet_sequence, first = HEADER.unpack_from(frame)
                self.assertEqual((magic, packet_generation, packet_sequence, first),
                                 (b"JPLY", generation, sequence, sent))
                if sequence == 0:
                    self.assertEqual(int(np.median(np.frombuffer(frame[HEADER.size:], dtype="<i2"))), 285)
                sent += count
                sequence += 1
                await ws.send_json({"type": "ack_cache_progress", "generation": generation, "samples": sent})
            self.assertEqual((await ws.receive_json(timeout=2))["type"], "ack_cache_commit")
            await ws.send_json({"type": "ack_cache_committed", "generation": generation, "cache_id": playback_id})
            await asyncio.wait_for(self.bench.task, 2)
            self.assertTrue(self.bench.acknowledgment_status()["cached_on_device"])
            self.assertIsNone(self.bench.device.capture_id)
            self.assertIsNone(self.bench.device.playback)
        finally:
            await ws.close()

    async def test_cross_origin_and_non_json_control_are_rejected(self):
        response = await self.ui.put('/api/config', json={}, headers={'Origin':'https://untrusted.example'})
        self.assertEqual(response.status, 403)
        response = await self.ui.put('/api/config', data='{}', headers={'Content-Type':'text/plain'})
        self.assertEqual(response.status, 415)

    async def test_diagnostic_export_does_not_include_pairing_secret(self):
        self.bench.results = [{'transport': {'events':[{'player_id':'aa:bb:cc:dd:ee:ff', 'advertised_host':'192.168.1.77'}]}}]
        response = await self.ui.get('/api/diagnostics')
        content = await response.text()
        self.assertNotIn(self.bench.config['device_token'], content)
        self.assertNotIn('aa:bb:cc:dd:ee:ff', content)
        self.assertNotIn('192.168.1.77', content)

    async def test_firmware_setup_uses_repository_and_persistent_credentials(self):
        self.bench.config['ha_lan_url'] = 'http://192.168.1.10:8099'
        first = await self.ui.get('/api/firmware')
        second = await self.ui.get('/api/firmware')
        self.assertEqual(first.status, 200)
        a = zipfile.ZipFile(io.BytesIO(await first.read()))
        b = zipfile.ZipFile(io.BytesIO(await second.read()))
        self.assertEqual(a.read('jarvis-secrets-to-merge.yaml'), b.read('jarvis-secrets-to-merge.yaml'))
        self.assertIn(b'https://github.com/dylanhelp-cmyk/ha-jarvis', a.read('voice-pe-repository.yaml'))
        self.assertFalse(any('components/' in name for name in a.namelist()))
        restarted = Bench(self.folder.name)
        self.assertEqual(restarted.firmware_secrets, self.bench.firmware_secrets)

    async def test_firmware_setup_requires_the_saved_lan_url(self):
        response = await self.ui.get('/api/firmware')
        self.assertEqual(response.status, 400)

    async def test_unpaired_device_cannot_connect(self):
        ws = await self.audio.ws_connect('/device')
        await ws.send_json({'type':'hello','protocol':1,'token':'wrong','sample_rate':16000,'channels':1})
        await ws.receive()
        self.assertFalse(self.bench.device.snapshot()['connected'])
        await ws.close()

    async def test_authenticated_connection_is_idle_and_exclusive(self):
        message = {'type':'hello','protocol':1,'token':self.bench.config['device_token'],'sample_rate':16000,'channels':1,'device_id':'test-device'}
        ws = await self.audio.ws_connect('/device')
        await ws.send_json(message)
        self.assertEqual((await ws.receive_json())['type'], 'hello_ok')
        self.assertIsNone(self.bench.device.capture_id)
        second = await self.audio.ws_connect('/device')
        await second.send_json(message)
        await second.receive()
        self.assertTrue(self.bench.device.snapshot()['connected'])
        await second.close()
        await ws.close()

    async def test_authenticated_reconnect_telemetry_is_exported(self):
        from test_device_diagnostics import diagnostics, pcm_summary
        ws = await self.audio.ws_connect('/device')
        await ws.send_json({'type':'hello', 'protocol':1, 'token':self.bench.config['device_token'],
                            'sample_rate':16000, 'channels':1,
                            'diagnostics':{**diagnostics(), 'pcm_send':pcm_summary()}})
        await ws.receive_json()
        response = await self.ui.get('/api/diagnostics')
        body = await response.json()
        self.assertEqual(body['firmware_observations'][0]['diagnostics']['root_fault']['reason'], 'pcm_send_failed')
        self.assertEqual(body['firmware_observations'][0]['diagnostics']['pcm_send'], pcm_summary())
        self.assertNotIn(self.bench.config['device_token'], json.dumps(body))
        await ws.close()

    async def test_pairing_rotation_closes_old_connection(self):
        ws = await self.audio.ws_connect('/device')
        await ws.send_json({'type':'hello','protocol':1,'token':self.bench.config['device_token'],'sample_rate':16000,'channels':1})
        await ws.receive_json()
        response = await self.ui.put('/api/config', json={'device_token':'a'*40})
        self.assertEqual(response.status, 200)
        await ws.receive()
        await ws.close()


class DeviceTests(unittest.TestCase):
    def test_wake_render_progress_rejects_stale_generation_and_invalid_sample_clock(self):
        device = Device()
        device.wake_details = {"ack_playback_id": "wake", "ack_generation": 4,
                               "ack_cache_id": "yes-sir", "ack_samples": 4800}
        report = {"type": "play_clock", "source": "wake_ack", "playback_id": "wake", "generation": 4,
                  "ack_cache_id": "yes-sir",
                  "rendered_samples": 3840, "first_rendered_ms": 1000, "last_rendered_ms": 1080}
        device.receive_json({**report, "generation": 3})
        device.receive_json({**report, "ack_cache_id": "old-clip"})
        device.receive_json({**report, "last_rendered_ms": 1140})
        device.receive_json({**report, "rendered_samples": 960, "last_rendered_ms": 1020})
        self.assertIsNone(device.wake_render_progress)
        device.receive_json(report)
        self.assertEqual(device.wake_render_progress[:2], (3840, 1000))
        self.assertFalse(device.wake_terminal_event.is_set())
        device.receive_json({**report, "type": "play_drained", "rendered_samples": 4800, "last_rendered_ms": 1100})
        self.assertTrue(device.wake_terminal_event.is_set())
        self.assertGreaterEqual(device.wake_render_terminal_at, device.wake_render_progress[2])

    def test_sample_gap_invalidates_capture(self):
        device = Device()
        device.capture_id = 'test'
        device.receive_pcm(FRAME.pack(b'JPCM',1,1,2,0,0,10) + b'\0'*4)
        device.receive_pcm(FRAME.pack(b'JPCM',1,1,2,1,3,11) + b'\0'*4)
        self.assertIn('Capture sample gap or reordering', device.errors)

    def test_device_mute_or_drops_invalidates_capture(self):
        device = Device()
        device.capture_id = 'test'
        device.receive_json({'type':'status','muted':True})
        device.receive_json({'type':'capture_stopped','capture_id':'test','reason':'requested','dropped_frames':2})
        self.assertEqual(len(device.errors), 2)

    def test_missing_opening_frame_is_detected(self):
        device = Device()
        device.capture_id = 'test'
        device.receive_pcm(FRAME.pack(b'JPCM',1,1,2,1,2,10) + b'\0'*4)
        self.assertIn('Capture beginning was lost', device.errors)

    def test_bad_addresses_and_short_pairing_token_are_rejected(self):
        for config in [{'device_token':'short'}, {'device_token':'a'*40,'wiim_host':'8.8.8.8'},
                       {'device_token':'a'*40,'ha_lan_url':'http://127.0.0.1:8099'},
                       {'device_token':'a'*40,'ha_lan_url':'http://192.168.1.4:8099/private'}]:
            with self.assertRaises(ValueError): validate_config(config)


class StreamSignalTests(unittest.TestCase):
    def test_stability_preserves_every_probe_before_continuous_pilot(self):
        pcm, probes = bench_signal("stability", 3)
        self.assertEqual(len(probes), 3)
        for i, (offset, probe) in enumerate(probes.items()):
            self.assertEqual(offset, 2 * i * RATE)
            np.testing.assert_array_equal(pcm[offset:offset + len(probe)], make_probe(513 + i))
        self.assertTrue(np.any(pcm[-2 * RATE:-RATE]))
        self.assertFalse(np.any(pcm[-RATE:]))
