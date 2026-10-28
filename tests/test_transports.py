"""Offline command and lifecycle tests; no real receiver is contacted."""
import asyncio
import inspect
import struct
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, Mock, create_autospec

from jarvis.transports import AirPlayTransport, NativeHTTPTransport, SlimProtoTransport, TransportError


class NativeHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.commands = []
        self.status = 200
        self.body = b"OK"
        self.split_response = False
        self.play_received = asyncio.Event()
        self.release_play = asyncio.Event()
        self.release_play.set()
        self.connections = set()

        async def request(reader, writer):
            self.connections.add(writer)
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                target = head.split(b" ")[1].decode()
                command = parse_qs(urlsplit(target).query)["command"][0]
                self.commands.append(command)
                if command.startswith("setPlayerCmd:play:"):
                    self.play_received.set()
                    await self.release_play.wait()
                writer.write(f"HTTP/1.1 {self.status} Test\r\nContent-Length: {len(self.body)}\r\nConnection: close\r\n\r\n".encode())
                if self.split_response:
                    writer.write(self.body[:1])
                    await writer.drain()
                    await asyncio.sleep(0.01)
                    writer.write(self.body[1:])
                else:
                    writer.write(self.body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                self.connections.discard(writer)

        self.server = await asyncio.start_server(request, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.adapter = NativeHTTPTransport(f"http://127.0.0.1:{port}", timeout=0.5)

    async def asyncTearDown(self):
        self.release_play.set()
        self.adapter.active = False
        await self.adapter.close()
        self.server.close()
        await self.server.wait_closed()
        for writer in self.connections.copy():
            writer.close()
            await writer.wait_closed()

    async def test_play_escapes_embedded_url_and_stop_reaches_receiver(self):
        url = "http://127.0.0.1:9876/a%20b.wav?key=a+b&other=%26#fragment"
        await self.adapter.play(url, {"codec": "wav"})
        self.assertTrue(self.adapter.active)
        await self.adapter.stop()
        self.assertEqual(self.commands, ["setPlayerCmd:play:" + url, "setPlayerCmd:stop"])
        self.assertFalse(self.adapter.active)
        self.assertFalse(self.adapter.snapshot()["audible_onset_measured"])

    async def test_flac_is_a_real_url_playback_candidate(self):
        await self.adapter.play("http://127.0.0.1/test.flac", {"codec": "flac"})
        self.assertEqual(len(self.commands), 1)

    async def test_acknowledgement_may_arrive_in_multiple_reads(self):
        self.split_response = True
        await self.adapter.play("http://127.0.0.1/test.wav", {})
        self.assertTrue(self.adapter.active)

    async def test_http_error_fails_play(self):
        self.status = 503
        with self.assertRaisesRegex(TransportError, "HTTP 503"):
            await self.adapter.play("http://127.0.0.1/audio.wav", {})
        self.assertFalse(self.adapter.active)

    async def test_device_rejection_fails_play(self):
        self.body = b"FAIL"
        with self.assertRaisesRegex(TransportError, "rejected"):
            await self.adapter.play("http://127.0.0.1/audio.wav", {})

    async def test_stop_http_error_is_not_reported_as_stopped(self):
        await self.adapter.play("http://127.0.0.1/audio.wav", {})
        self.status = 500
        with self.assertRaises(TransportError):
            await self.adapter.stop()
        self.assertTrue(self.adapter.active)

    async def test_cancel_waits_for_play_then_sends_actual_stop(self):
        self.release_play.clear()
        task = asyncio.create_task(self.adapter.play("http://127.0.0.1/audio.wav", {}))
        await self.play_received.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.release_play.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.commands[-1], "setPlayerCmd:stop")
        self.assertFalse(self.adapter.active)

    async def test_output_state_is_read_only_and_validated(self):
        self.body = b'{"vol":"47","mute":"0","mode":"1","status":"stop"}'
        state = await self.adapter.output_state()
        self.assertEqual(state['volume_percent'], 47)
        self.assertFalse(state['muted'])
        self.assertEqual(self.commands, ['getPlayerStatus'])
        self.body = b'{"vol":"NaN","mute":"0"}'
        with self.assertRaisesRegex(TransportError, 'valid volume'):
            await self.adapter.output_state()

    async def test_unsupported_codec_does_not_send_a_command(self):
        with self.assertRaisesRegex(TransportError, "Unsupported"):
            await self.adapter.play("http://127.0.0.1/audio.ogg", {"codec": "ogg"})
        self.assertEqual(self.commands, [])


async def _play_url_signature(url, mime_type=None, autostart=True, send_flush=True,
                              stream_threshold=200, output_threshold=20):
    pass


class FakeSlimServer:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.player = SimpleNamespace(player_id="00:11:22:33:44:55", name="Fake receiver",
                                      connected=True, supported_codecs=["pcm", "flc"],
                                      max_sample_rate=96000, play_url=create_autospec(_play_url_signature),
                                      stop=AsyncMock())
        self.players = [self.player]
        self.unsubscribed = False

    def subscribe(self, callback):
        self.callback = callback
        def unsubscribe():
            self.unsubscribed = True
        return unsubscribe


class SlimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = SlimProtoTransport("127.0.0.1", server_factory=FakeSlimServer)
        await self.transport.start()
        self.server = self.transport._server

    async def asyncTearDown(self):
        self.server.player.connected = True
        await self.transport.close()

    async def test_server_advertisement_and_player_discovery(self):
        self.assertEqual(self.server.kwargs["ip_address"], "127.0.0.1")
        self.assertEqual(self.server.kwargs["control_port"], 3483)
        self.assertEqual(self.transport.players()[0]["supported_codecs"], ["pcm", "flc"])

    async def test_bind_conflict_reports_control_port_and_cleans_up(self):
        failed_server = FakeSlimServer()
        failed_server.start.side_effect = OSError("Address already in use")
        adapter = SlimProtoTransport("127.0.0.1", control_port=3483,
                                     server_factory=lambda **kwargs: failed_server)
        with self.assertRaisesRegex(TransportError, "TCP/UDP port 3483"):
            await adapter.start()
        self.assertIn("another LMS", adapter.snapshot()["last_error"])
        self.assertTrue(failed_server.unsubscribed)
        failed_server.stop.assert_awaited_once()

    async def test_play_forwards_mime_and_low_buffer_defaults(self):
        await self.transport.play("http://127.0.0.1/test.flac", {"codec": "flac"})
        self.server.player.play_url.assert_awaited_once_with(
            "http://127.0.0.1/test.flac", mime_type="audio/flac", autostart=True,
            send_flush=True, stream_threshold=1, output_threshold=1)
        await self.transport.stop()
        self.server.player.stop.assert_awaited_once()

    async def test_old_dependency_without_buffer_controls_rejected(self):
        self.server.player.play_url = AsyncMock()
        with self.assertRaisesRegex(TransportError, "lacks buffer controls"):
            await self.transport.play("http://127.0.0.1/test.wav", {"stream_threshold_kb": 10})
        self.server.player.play_url.assert_not_awaited()

    async def test_custom_buffer_configuration_forwarded(self):
        await self.transport.play("http://127.0.0.1/test.flac", {
            "codec": "flac", "stream_threshold_kb": 3, "output_threshold_tenths": 2})
        args = self.server.player.play_url.await_args.kwargs
        self.assertEqual((args["stream_threshold"], args["output_threshold"]), (3, 2))

    async def test_wav_mime_declares_actual_pcm_parameters(self):
        await self.transport.play("http://127.0.0.1/test.wav", {
            "sample_rate": 48000, "sample_size": 16, "channels": 1})
        self.assertEqual(self.server.player.play_url.await_args.kwargs["mime_type"],
                         "audio/pcm;rate=48000;bitrate=16;channels=1")

    async def test_rejects_unadvertised_codec(self):
        self.server.player.supported_codecs = ["pcm"]
        with self.assertRaisesRegex(TransportError, "does not advertise"):
            await self.transport.play("http://127.0.0.1/test.flac", {"codec": "flac"})
        self.server.player.play_url.assert_not_awaited()

    async def test_rejects_ambiguous_player_and_https(self):
        self.server.players.append(self.server.player)
        with self.assertRaisesRegex(TransportError, "Select one"):
            await self.transport.play("http://127.0.0.1/test.wav", {})
        with self.assertRaisesRegex(TransportError, "requires HTTP"):
            await self.transport.play("https://127.0.0.1/test.wav", {})

    async def test_cancel_sends_stop_to_same_player(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_play(*args, **kwargs):
            entered.set()
            await release.wait()
        self.server.player.play_url.side_effect = slow_play
        task = asyncio.create_task(self.transport.play("http://127.0.0.1/test.wav", {}))
        await entered.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.server.player.stop.assert_awaited_once()

    async def test_actual_dependency_serializes_requested_buffers(self):
        # Validate the real maintained library's packet construction, while
        # intercepting its final socket write. No server or receiver is contacted.
        from aioslimproto.client import SlimClient
        self.assertIn("stream_threshold", inspect.signature(SlimClient.play_url).parameters,
                      "Install the project's immutable aioslimproto source pin")
        reader = asyncio.StreamReader()
        writer = SimpleNamespace(is_closing=lambda: False, can_write_eof=lambda: False,
                                 close=lambda: None, wait_closed=AsyncMock())
        client = SlimClient(reader, writer, lambda *args: None)
        client._connected = True
        client._powered = True
        client._capabilities = {"SupportedCodecs": ["pcm", "flc"]}
        client.send_frame = AsyncMock()
        try:
            await client.play_url("http://127.0.0.1:8900/test.flac", mime_type="audio/flac",
                                  stream_threshold=1, output_threshold=1)
            command, data = client.send_frame.await_args.args
            self.assertEqual(command, b"strm")
            fields = struct.unpack("!cc5sBcBcBBBLHL", data[:24])
            self.assertEqual(fields[0], b"s")
            self.assertEqual(fields[3], 1)  # compressed stream KB
            self.assertEqual(fields[8], 1)  # decoded output tenths of a second
        finally:
            client.disconnect()
            await client._async_cleanup()


class AirPlayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.finished = asyncio.Event()
        self.entered = asyncio.Event()
        async def stream(url):
            self.entered.set()
            await self.finished.wait()
        async def stop():
            self.finished.set()
        self.connection = SimpleNamespace(stream=SimpleNamespace(stream_file=AsyncMock(side_effect=stream)),
                                          remote_control=SimpleNamespace(stop=AsyncMock(side_effect=stop)),
                                          close=lambda: set())
        self.adapter = AirPlayTransport("127.0.0.1", connection=self.connection, stop_timeout=0.05,
            streamer=lambda connection, url, telemetry, codec: connection.stream.stream_file(url))

    async def asyncTearDown(self):
        await self.adapter.close()

    async def test_stream_url_and_same_session_stop(self):
        url = "http://127.0.0.1/stream.wav"
        await self.adapter.play(url, {})
        await self.entered.wait()
        self.connection.stream.stream_file.assert_awaited_once_with(url)
        await self.adapter.stop()
        self.connection.remote_control.stop.assert_awaited_once()
        self.assertFalse(self.adapter.active)

    async def test_airplay_context_uses_receiver_volume_instead_of_default(self):
        from pyatv.protocols.raop import RaopAudio
        from pyatv.protocols.raop.protocols import StreamContext
        context = StreamContext()
        audio = RaopAudio(SimpleNamespace(context=context, stream_client=None), Mock())
        self.connection.audio = audio
        self.adapter._state_reader = AsyncMock(return_value={'volume_percent':47, 'muted':False})
        self.assertEqual(audio.volume, 33)  # actual library default before preparation
        await self.adapter.play('http://127.0.0.1/stream.wav', {})
        self.assertAlmostEqual(audio.volume, 47)
        self.assertTrue(audio.has_changed_volume)
        self.adapter._state_reader.assert_awaited_once()

    async def test_muted_receiver_is_not_unmuted_or_streamed(self):
        self.connection.audio = SimpleNamespace(set_volume=AsyncMock())
        self.adapter._state_reader = AsyncMock(return_value={'volume_percent':47, 'muted':True})
        with self.assertRaisesRegex(TransportError, 'muted'):
            await self.adapter.play('http://127.0.0.1/stream.wav', {})
        self.connection.audio.set_volume.assert_not_awaited()
        self.connection.stream.stream_file.assert_not_awaited()

    async def test_unreadable_volume_never_falls_back_to_library_default(self):
        self.adapter._state_reader = AsyncMock(side_effect=TransportError('cannot read volume'))
        with self.assertRaisesRegex(TransportError, 'cannot read volume'):
            await self.adapter.play('http://127.0.0.1/stream.wav', {})
        self.connection.stream.stream_file.assert_not_awaited()

    async def test_failed_stream_remains_visible(self):
        self.connection.stream.stream_file.side_effect = RuntimeError("decoder rejected source")
        with self.assertRaisesRegex(TransportError, "decoder rejected"):
            await self.adapter.play("http://127.0.0.1/stream.flac", {"codec": "flac"})
        self.assertIn("decoder rejected", self.adapter.snapshot()["last_error"])

    async def test_background_setup_failure_surfaces_before_stream_timeout(self):
        async def fail_later(url):
            await self.finished.wait()
            raise RuntimeError('receiver rejected session')
        self.connection.stream.stream_file.side_effect = fail_later
        await self.adapter.play('http://127.0.0.1/stream.wav', {})
        self.finished.set()
        with self.assertRaisesRegex(TransportError, 'receiver rejected session'):
            await self.adapter.wait_for_stream(asyncio.Event(), timeout=1)

    async def test_empty_background_error_has_an_exception_name(self):
        async def fail_later(url):
            await self.finished.wait()
            raise TimeoutError()
        self.connection.stream.stream_file.side_effect = fail_later
        await self.adapter.play('http://127.0.0.1/stream.wav', {})
        self.finished.set()
        with self.assertRaisesRegex(TransportError, 'TimeoutError'):
            await self.adapter.wait_for_stream(asyncio.Event(), timeout=1)

    async def test_stalled_setup_reports_stage_without_cancelling_sender(self):
        await self.adapter.play('http://127.0.0.1/stream.wav', {})
        with self.assertRaisesRegex(TransportError, 'did not request test audio'):
            await self.adapter.wait_for_stream(asyncio.Event(), timeout=.01)
        self.assertFalse(self.adapter._stream_task.done())
        self.assertTrue(self.adapter.events[-1]['pending_stage'])

    async def test_fetch_ready_does_not_claim_playback_complete(self):
        await self.adapter.play('http://127.0.0.1/stream.wav', {})
        ready = asyncio.Event()
        ready.set()
        await self.adapter.wait_for_stream(ready, timeout=.1)
        self.assertFalse(self.adapter._stream_task.done())

    async def test_cancelling_readiness_wait_leaves_sender_for_stop_cleanup(self):
        await self.adapter.play('http://127.0.0.1/stream.wav', {})
        readiness = asyncio.create_task(self.adapter.wait_for_stream(asyncio.Event(), timeout=10))
        await asyncio.sleep(0)
        readiness.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await readiness
        self.assertFalse(self.adapter._stream_task.done())
        await self.adapter.stop()

    async def test_stop_cancels_stalled_sender(self):
        self.connection.remote_control.stop.side_effect = None
        await self.adapter.play("http://127.0.0.1/stream.wav", {})
        task = self.adapter._stream_task
        await self.adapter.stop()
        self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main()
