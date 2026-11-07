import asyncio
import json
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
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
            'device_token': original, 'ha_lan_url': 'http://192.168.1.10:8099', 'repetitions': 5})
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

    async def test_wired_bench_uses_paired_socket_for_pcm_and_drain(self):
        ws = await self.audio.ws_connect('/device')
        await ws.send_json({'type':'hello','protocol':1,'token':self.bench.config['device_token'],
                            'sample_rate':RATE,'channels':1,'raw_ready':True,'muted':False,
                            'playback_protocol':1,'jack_plugged':True})
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
        from jarvis.acknowledgment import cache_path, candidate_identity
        pcm = np.full(RATE // 4, 900, dtype="<i2").tobytes()
        cache_path(self.bench.data_dir).write_bytes(pcm)
        identity = candidate_identity(pcm)
        preview = await self.ui.get("/api/acknowledgment/preview", params={"clip": identity})
        self.assertEqual(preview.status, 200)
        self.assertTrue((await preview.read()).startswith(b"RIFF"))
        bad = await self.ui.post("/api/acknowledgment/verify",
                                 json={"clip": "stale", "heard_exact_phrase": True})
        self.assertEqual(bad.status, 400)
        approved = await self.ui.post("/api/acknowledgment/verify",
                                      json={"clip": identity, "heard_exact_phrase": True})
        self.assertTrue((await approved.json())["verified"])
        ws = await self.audio.ws_connect("/device")
        try:
            await ws.send_json({"type": "hello", "protocol": 1, "token": self.bench.config["device_token"],
                                "sample_rate": RATE, "channels": 1, "ack_cache_protocol": 1})
            self.assertEqual((await ws.receive_json(timeout=2))["type"], "hello_ok")
            first_start = await ws.receive_json(timeout=2)
            self.assertEqual(first_start["type"], "ack_cache_start")
            abort = await ws.receive_json(timeout=4)
            self.assertEqual(abort, {"type": "ack_cache_abort", "generation": first_start["generation"]})
            self.assertFalse(self.bench.acknowledgment_status()["cached_on_device"])
            start = await ws.receive_json(timeout=2)
            self.assertEqual(start["type"], "ack_cache_start")
            self.assertEqual(start["cache_id"], identity)
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
                sent += count
                sequence += 1
                await ws.send_json({"type": "ack_cache_progress", "generation": generation, "samples": sent})
            self.assertEqual((await ws.receive_json(timeout=2))["type"], "ack_cache_commit")
            await ws.send_json({"type": "ack_cache_committed", "generation": generation, "cache_id": identity})
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
