import asyncio
from contextlib import asynccontextmanager, suppress
import json
import io
from pathlib import Path
import tempfile
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

from aiohttp.test_utils import TestClient, TestServer
from jarvis.server import Bench, Stream, create_ui, create_audio, validate_config
from jarvis.device import Device
from jarvis.audio import FRAME, wav_header


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

    @asynccontextmanager
    async def running_test(self):
        """Exercise actual test task and WS handling with a fake speaker only."""
        ws = await self.audio.ws_connect('/device')
        await ws.send_json({'type':'hello', 'protocol':1, 'token':self.bench.config['device_token'],
                            'sample_rate':16000, 'channels':1})
        await ws.receive_json()
        self.bench.config.update(wiim_host='192.168.1.20', stream_base_url='http://192.168.1.10:8099')
        entered = asyncio.Event()
        async def begin(capture_id, duration_ms):
            self.bench.device.capture_id = capture_id
            self.bench.device.started.set()
            self.bench.device.receive_pcm(FRAME.pack(b'JPCM', 1, 1, 2, 0, 0, 10) + b'\x01\x00'*2)
        async def play(*args):
            entered.set()
        transport = Mock(play=AsyncMock(side_effect=play), stop=AsyncMock(), close=AsyncMock())
        with patch('jarvis.transports.NativeHTTPTransport', return_value=transport), patch.object(self.bench.device, 'begin', begin):
            response = await self.ui.post('/api/tests', json={'kind':'latency', 'comfortable_volume_confirmed':True})
            self.assertEqual(response.status, 202)
            await asyncio.wait_for(entered.wait(), 2)
            try:
                yield ws
            finally:
                await self.bench.cancel()
                await ws.close()

    async def test_connection_loss_is_failed_and_preserves_partial_capture(self):
        async with self.running_test() as ws:
            await ws.close()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(self.bench.task, 2)
            result = self.bench.results[0]
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['interruption']['reason'], 'device_disconnected')
            self.assertIn('close_code', result['interruption'])
            self.assertEqual(result['capture']['samples'], 2)
            self.assertEqual(result['trials'], [])
            self.assertFalse(self.bench.test['running'])

    async def test_explicit_stop_is_cancelled_and_preserves_reason(self):
        async with self.running_test():
            response = await self.ui.delete('/api/tests/current', json={})
            self.assertEqual(response.status, 202)
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(self.bench.task, 4)
            self.assertEqual(self.bench.results[0]['status'], 'cancelled')
            self.assertEqual(self.bench.results[0]['interruption']['reason'], 'user_stop')

    async def test_mute_is_failed_and_preserves_reason(self):
        async with self.running_test() as ws:
            await ws.send_json({'type':'status', 'muted':True})
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(self.bench.task, 4)
            result = self.bench.results[0]
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['interruption']['reason'], 'device_muted')

    async def test_invalid_pcm_records_protocol_failure(self):
        async with self.running_test() as ws:
            await ws.send_bytes(b'bad')
            await ws.receive()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(self.bench.task, 2)
            result = self.bench.results[0]
            self.assertEqual(result['status'], 'failed')
            self.assertIn('header', result['interruption']['protocol_error'])
            self.assertEqual(result['interruption']['server_close_code'], 1008)

    async def test_late_disconnect_cannot_interrupt_failure_cleanup(self):
        async with self.running_test():
            stopping, release = asyncio.Event(), asyncio.Event()
            async def stop():
                stopping.set()
                await release.wait()
            self.bench.transport.stop.side_effect = stop
            with patch('jarvis.server.MAX_PLAYBACK_DELAY_SECONDS', 0), patch.object(self.bench.device, 'end', AsyncMock()):
                self.bench.stream.connected.set()
                self.bench.stream.done.set()
                await asyncio.wait_for(stopping.wait(), 2)
                # recording() failed clock validation; cleanup is now in progress.
                late_disconnect = asyncio.create_task(self.bench.cancel('device_disconnected'))
                await asyncio.sleep(0)
                self.assertFalse(self.bench.task.cancelling())
                release.set()
                await asyncio.wait_for(late_disconnect, 2)
            self.assertFalse(self.bench.test['running'])
            self.assertEqual(self.bench.device.frames, [])
            self.assertEqual(self.bench.results[0]['status'], 'failed')

    async def test_test_requires_user_confirmation_and_paired_mic(self):
        response = await self.ui.post('/api/tests', json={'kind':'latency'})
        self.assertEqual(response.status, 400)
        response = await self.ui.post('/api/tests', json={'kind':'latency', 'comfortable_volume_confirmed':True})
        self.assertEqual(response.status, 400)
        self.assertIsNone(self.bench.task)

    async def test_cross_origin_and_non_json_control_are_rejected(self):
        response = await self.ui.put('/api/config', json={}, headers={'Origin':'https://untrusted.example'})
        self.assertEqual(response.status, 403)
        response = await self.ui.put('/api/config', data='{}', headers={'Content-Type':'text/plain'})
        self.assertEqual(response.status, 415)

    async def test_diagnostic_export_does_not_include_pairing_secret(self):
        response = await self.ui.get('/api/diagnostics')
        self.assertNotIn(self.bench.config['device_token'], await response.text())

    async def test_firmware_setup_uses_repository_and_persistent_credentials(self):
        self.bench.config['stream_base_url'] = 'http://192.168.1.10:8099'
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

    async def test_stale_stream_url_cannot_be_replayed(self):
        response = await self.audio.get('/stream/made-up.wav')
        self.assertEqual(response.status, 404)

    async def test_slim_pcm_http_body_and_both_decoder_commands_agree(self):
        from aioslimproto.client import SlimClient
        self.bench.config.update(route='slimproto', codec='wav')
        stream = self.bench.stream = Stream('latency', 1)
        samples = struct.pack('<hhhh', -2000, -1, 0, 2000)
        async def blocks():
            yield samples
        reader = asyncio.StreamReader()
        writer = SimpleNamespace(is_closing=lambda:False, can_write_eof=lambda:False,
                                 close=lambda:None, wait_closed=AsyncMock())
        player = SlimClient(reader, writer, lambda *args:None)
        player._connected = player._powered = True
        player._capabilities = {'SupportedCodecs':['pcm', 'flc']}
        player.send_frame = AsyncMock()
        try:
            # Use the actual transport, pinned library, endpoint and response
            # parser: an assertion on the initial MIME alone missed this bug.
            from jarvis.transports import SlimProtoTransport
            transport = SlimProtoTransport('127.0.0.1')
            transport._server = SimpleNamespace(players=[player])
            with patch.object(stream, 'blocks', blocks):
                url = str(self.audio.make_url('/stream/' + stream.id + '.pcm'))
                await transport.play(url, {'codec':'wav', 'sample_rate':16000, 'sample_size':16, 'channels':1})
                command, packet = player.send_frame.await_args.args
                self.assertEqual(command, b'strm')
                self.assertEqual(struct.unpack('!cc5sBcBcBBBLHL', packet[:24])[2], b'p1711')
                response = await self.audio.get('/stream/' + stream.id + '.pcm')
                self.assertEqual(await response.read(), samples)  # no RIFF header or byte swaps
                content_type = response.headers['Content-Type']
                self.assertEqual(content_type, 'audio/pcm;rate=16000;bitrate=16;channels=1')
                player._awaiting_stmc = False  # simulated connection acknowledgment
                await player._process_resp(('HTTP/1.0 200 OK\r\nContent-Type: ' + content_type + '\r\n\r\n').encode())
                decoder_commands = [call.args[1] for call in player.send_frame.await_args_list if call.args[0] == b'codc']
                self.assertEqual(decoder_commands, [b'p1711'])
        finally:
            player.disconnect()
            await player._async_cleanup()

    async def test_http_wav_keeps_its_container_header(self):
        self.bench.config.update(route='http', codec='wav')
        stream = self.bench.stream = Stream('latency', 1)
        async def blocks():
            yield b'\x01\x00' * 20
        with patch.object(stream, 'blocks', blocks):
            response = await self.audio.get('/stream/' + stream.id + '.wav')
            self.assertEqual(response.headers['Content-Type'], 'audio/wav')
            self.assertEqual(await response.read(), wav_header() + b'\x01\x00' * 20)

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
                       {'device_token':'a'*40,'stream_base_url':'http://127.0.0.1:8099'},
                       {'device_token':'a'*40,'stream_base_url':'http://192.168.1.4:8099/private'}]:
            with self.assertRaises(ValueError): validate_config(config)


if __name__ == '__main__': unittest.main()
