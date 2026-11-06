import asyncio
import json
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from aiohttp.test_utils import TestClient, TestServer
from jarvis.server import Bench, bench_signal, create_ui, create_audio, validate_config
from jarvis.device import Device
from jarvis.audio import FRAME, RATE, make_probe
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
