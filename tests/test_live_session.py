import asyncio
import base64
import json
from fractions import Fraction
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import numpy as np
from av import AudioFrame

from jarvis.assist_delegation import AssistDelegation, END_CONVERSATION
from jarvis.cloud_settings import CloudSettings, CloudBudgetError
from jarvis.live_session import LiveSession, LiveSessionError, _PCMTrack
from jarvis.realtime_bench import MODEL as REALTIME_MODEL, RealtimeBenchmarkSession


class FakeWS:
    def __init__(self, finalize=True):
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.finalize = finalize

    def event(self, value):
        self.queue.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(value)))

    async def send_json(self, value):
        self.sent.append(value)
        if value["type"] == "session.start":
            self.event({"type": "session.started", "session": {"id": "live_offline"}})
        if value["type"] == "session.close" and self.finalize:
            self.event({"type": "session.closed", "usage": {"seconds": 20}, "reason": "close_requested"})

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.queue.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def close(self):
        self.closed = True
        self.queue.put_nowait(None)


class FakeHTTP:
    def __init__(self, ws):
        self.ws = ws
        self.closed = False
        self.request = None

    async def ws_connect(self, url, **kwargs):
        self.url = url
        return self.ws

    def post(self, url, **kwargs):
        self.url, self.request = url, kwargs
        return self

    async def __aenter__(self):
        self.status = getattr(self, 'status', 201)
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return {"session": {"id": "live_rtc_offline"}, "transport": {"sdp": "offline-answer"}}

    async def close(self):
        self.closed = True


class FakeChannel:
    def __init__(self):
        self.handlers = {}
        self.readyState = "open"
        self.sent = []

    def on(self, name):
        def register(function):
            self.handlers[name] = function
            return function
        return register

    def send(self, message):
        self.sent.append(json.loads(message))
        if self.sent[-1]["type"] == "session.close":
            self.handlers["message"](json.dumps({"type": "session.closed", "usage": {"seconds": 20},
                                                 "reason": "close_requested"}))


class FakePC:
    def __init__(self, configuration):
        self.channel = FakeChannel()
        self.handlers = {}
        self.connectionState = "new"
        self.closed = False

    def addTrack(self, track):
        self.track = track

    def createDataChannel(self, label):
        self.label = label
        return self.channel

    def on(self, name):
        def register(function):
            self.handlers[name] = function
            return function
        return register

    async def createOffer(self):
        return SimpleNamespace(sdp="offline-offer")

    async def setLocalDescription(self, offer):
        self.localDescription = offer

    async def setRemoteDescription(self, answer):
        self.answer = answer
        self.channel.handlers["message"](json.dumps({"type": "session.started",
                                                      "session": {"id": "live_rtc_offline"}}))

    async def close(self):
        self.closed = True


class LiveSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.settings = CloudSettings(self.folder.name)
        self.settings.set_key("sk-offline-only-" + "x" * 32)
        self.output = AsyncMock()
        self.sessions = []

    async def asyncTearDown(self):
        for session in self.sessions:
            await session.close()
        self.folder.cleanup()

    def session(self, **kwargs):
        result = LiveSession(self.settings, on_audio=self.output, close_timeout=.05, **kwargs)
        self.sessions.append(result)
        return result

    async def test_interrupt_ack_never_unmutes_or_discards_input_and_pending_work(self):
        ws = FakeWS()
        fixture_seen = asyncio.Event()
        send = ws.send_json

        async def capture_fixture(value):
            await send(value)
            if value["type"] == "session.output_audio.fixture.play":
                fixture_seen.set()

        ws.send_json = capture_fixture
        session = self.session(transport="websocket")
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()
        session._assist_active.add("pending-response")
        attempt = asyncio.create_task(session.interrupt_speech())
        await asyncio.wait_for(fixture_seen.wait(), 1)
        command = ws.sent[-1]
        self.assertEqual(base64.b64decode(command["audio"]), bytes(960))
        with self.assertRaises(LiveSessionError):
            await session.send_audio(bytes(640))
        self.assertFalse(any(item["type"] == "session.input_audio.append" for item in ws.sent))
        for event in ("playing", "played"):
            session._event({"type": "session.output_audio.fixture." + event, "client_event_id": "stale"})
        self.assertFalse(session._fixture_played.is_set())
        for event in ("playing", "played"):
            session._event({"type": "session.output_audio.fixture." + event,
                            "client_event_id": command["event_id"]})
        with self.assertRaises(LiveSessionError):
            session.resume_after_interrupt(session._output_generation)
        await asyncio.sleep(.08)
        stale = np.full(320, 600, dtype="<i2").tobytes()
        session._queue_audio(stale)
        result = await asyncio.wait_for(attempt, 1)
        self.assertGreaterEqual(result["quiet_observed_ms"], 250)
        self.assertTrue(session._output_blocked)
        self.assertTrue(session.assist_work_pending)
        self.output.assert_not_awaited()
        with self.assertRaises(LiveSessionError):
            session.resume_after_interrupt(result["generation"] - 1)
        session.resume_after_interrupt(result["generation"])
        fresh = np.full(320, 900, dtype="<i2").tobytes()
        await session.send_audio(fresh)
        session._queue_audio(fresh)
        await asyncio.sleep(0)
        self.output.assert_awaited_once_with(fresh, 16000)
        self.assertEqual(session.metrics.discarded_output_samples, 320)
        session.suppress_output()
        with self.assertRaises(LiveSessionError):
            session.resume_after_interrupt(result["generation"])

    async def test_missing_interrupt_ack_keeps_output_blocked_and_settles_usage(self):
        ws = FakeWS()
        session = self.session(transport="websocket")
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()
        with self.assertRaises(TimeoutError):
            await session.interrupt_speech()
        self.assertTrue(session._output_blocked)
        self.assertTrue((await session.close()).finalization_confirmed)
        self.assertIsNone(self.settings.pending)

    async def test_close_rejects_a_microphone_send_already_waiting_for_the_input_lock(self):
        ws = FakeWS()
        session = self.session(transport="websocket")
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()
        await session._input_lock.acquire()
        waiting = asyncio.create_task(session.send_audio(bytes(640)))
        await asyncio.sleep(0)
        await session.close()
        session._input_lock.release()
        with self.assertRaises(LiveSessionError):
            await waiting
        self.assertFalse(any(item["type"] == "session.input_audio.append" for item in ws.sent))

    async def test_responses_delegation_waits_for_a_complete_batch_and_keeps_audio_running(self):
        ws = FakeWS()
        tool_started = asyncio.Event()
        finish_tool = asyncio.Event()

        async def call_tool(*_):
            tool_started.set()
            await finish_tool.wait()
            return {"outcome": "reported", "text": "private Home Assistant response"}

        client = SimpleNamespace(call_tool=AsyncMock(side_effect=call_tool))
        catalog = [{"name": "HassTurnOn", "description": "Turn on an exposed device",
                    "inputSchema": {"type": "object"}}]
        bridge = AssistDelegation(client, catalog)
        request_signoff = Mock(return_value=False)
        current_speech = Mock(return_value=True)
        session = self.session(transport="websocket", assist_delegation=bridge,
                               on_signoff_request=request_signoff, allow_assist_action=current_speech)
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()

        config = ws.sent[0]["session"]["delegation"]
        self.assertEqual(config["responses"]["model"], "gpt-6-luna")
        self.assertEqual([tool["name"] for tool in config["responses"]["tools"]], ["HassTurnOn", END_CONVERSATION])
        self.assertEqual(self.settings.pending["backend"]["response_limit"], 6)

        def emit(kind, delegation="task", **data):
            session._event({"type": "response.event", "delegation_id": delegation,
                            "event": {"type": kind, **data}})

        def item(call_id, name="HassTurnOn"):
            return {"type": "function_call", "call_id": call_id, "name": name,
                    "arguments": '{"name":"Example lamp"}'}

        emit("response.created", response={"id": "failed"})
        emit("response.output_item.done", item=item("cancelled"))
        emit("response.failed", response={"id": "failed", "status": "failed"})
        emit("response.created", response={"id": "ok"})
        emit("response.output_item.done", delegation="foreign", item=item("foreign"))
        emit("response.output_item.done", item=item("denied", "AdministrativeTool"))
        emit("response.output_item.done", item=item("permitted"))
        emit("response.output_item.done", item=item("permitted"))
        client.call_tool.assert_not_called()

        emit("response.completed", response={"id": "ok", "status": "completed"})
        emit("response.completed", response={"id": "ok", "status": "completed"})
        await asyncio.wait_for(tool_started.wait(), 1)
        self.assertTrue(session.assist_work_pending)
        session._queue_audio(bytes(640))
        self.output.assert_not_awaited()
        self.assertEqual(len([m for m in ws.sent if m["type"] == "response.create"]), 0)
        await session.send_audio(bytes(640))
        finish_tool.set()
        await asyncio.wait_for(session._delegation_task, 1)
        client.call_tool.assert_awaited_once_with("permitted", "HassTurnOn", {"name": "Example lamp"})
        outputs = [m["item"] for m in ws.sent if m["type"] == "response.item.create"]
        self.assertEqual([m["call_id"] for m in outputs], ["denied", "permitted"])
        self.assertEqual([json.loads(m["output"])["outcome"] for m in outputs], ["rejected", "unverified"])
        self.assertNotIn("private Home Assistant response", str(ws.sent))
        self.assertEqual([m["type"] for m in ws.sent][-2:], ["response.item.create", "response.create"])
        self.assertTrue(session.assist_work_pending)
        emit("response.created", delegation=None, response={"id": "continued"})
        emit("response.completed", delegation=None, response={"id": "continued", "status": "completed"})
        self.assertFalse(session.assist_work_pending)
        before, after = session.assist_result_at - .01, session.assist_result_at + .01
        self.assertTrue(session.assist_reply_pending(before, before))
        self.assertTrue(session.assist_reply_pending(before, after))
        self.assertFalse(session.assist_reply_pending(after, after))
        session._event({"type": "session.output_transcript.delta", "delta": "On.", "start_ms": 100, "end_ms": 200})
        self.assertFalse(session.assist_reply_pending(before, after))
        session.begin_turn()
        self.assertFalse(session.assist_reply_pending(None, None))
        self.assertFalse(session.assist_used_this_turn)
        session._queue_audio(bytes(640))
        await asyncio.sleep(0)
        self.output.assert_awaited_once()
        request_signoff.return_value = True
        self.assertEqual(await session._execute_assist_call("signoff", END_CONVERSATION, "{}"), {"outcome": "accepted"})
        current_speech.return_value = False
        self.assertEqual((await session._execute_assist_call("seeded", "HassTurnOn", "{}"))["outcome"], "rejected")
        self.assertEqual(dict(bridge.activity), {"responses_started": 3, "function_calls_collected": 3,
                         "failed": 1, "completed": 2, "missing_current_request": 1})
        client.call_tool.assert_awaited_once()

    async def test_two_assist_requests_and_signoff_fit_one_standby_session(self):
        ws = FakeWS()
        client = SimpleNamespace(call_tool=AsyncMock(return_value={"outcome": "reported", "text": "{}"}))
        catalog = [{"name": "HassTurnOn", "inputSchema": {"type": "object"}},
                   {"name": "HassTurnOff", "inputSchema": {"type": "object"}}]
        signoff = Mock(return_value=True)
        session = self.session(transport="websocket", assist_delegation=AssistDelegation(client, catalog),
                               on_signoff_request=signoff, allow_assist_action=lambda: True)
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()

        def event(kind, delegation, **kwargs):
            session._event({"type": "response.event", "delegation_id": delegation, "event": {"type": kind, **kwargs}})

        async def tool_turn(index, name, args):
            session.begin_turn()
            delegation = f"turn-{index}"
            response = {"id": f"{delegation}-tool", "model": "gpt-6-luna", "status": "completed"}
            session._event({"type": "session.delegation.created", "delegation": {
                "target": "responses", "id": delegation, "response_id": response["id"]}})
            self.assertTrue(session.assist_work_pending)
            event("response.created", delegation, response=response)
            event("response.output_item.done", delegation, item={
                "type": "function_call", "call_id": f"call-{index}", "name": name, "arguments": args})
            event("response.completed", delegation, response=response)
            await session._delegation_task
            continuation = {**response, "id": f"{delegation}-speak",
                            "usage": {"input_tokens": 200, "output_tokens": 20}}
            event("response.created", None, response=continuation)
            event("response.completed", None, response=continuation)
            self.assertFalse(session.assist_work_pending)

        await tool_turn(1, "HassTurnOn", '{"name":"Reading light"}')
        await tool_turn(2, "HassTurnOff", '{"name":"Reading light"}')
        await tool_turn(3, END_CONVERSATION, "{}")
        self.assertFalse(session.assist_reply_pending(10, 10))
        self.assertEqual(client.call_tool.await_count, 2)
        signoff.assert_called_once_with()
        self.assertIsNone(session.metrics.error)
        self.assertTrue((await session.close()).finalization_confirmed)
        self.assertIsNone(self.settings.pending)

    async def test_spoken_or_terminal_stop_during_tool_work_revokes_the_whole_batch(self):
        schema = {"type": "object", "properties": {"name": {}, "domain": {}}}
        catalog = [{"name": name, "inputSchema": schema} for name in ("HassTurnOn", "GetLiveContext")]
        arguments = '{"name":"Desk Lamp"}'
        for preflight, spoken in ((True, False), (False, False), (True, True), (False, True)):
            with self.subTest(preflight=preflight, spoken=spoken):
                waiting, release = asyncio.Event(), asyncio.Event()

                async def call_tool(_call_id, name, _arguments):
                    if name == "HassTurnOn":
                        return {"outcome": "reported", "text": json.dumps({"response_type": "action_done",
                            "data": {"success": [{"type": "entity", "id": "light.desk", "name": "Desk Lamp"}],
                                     "failed": []}})}
                    waiting.set()
                    await release.wait()
                    state = "off" if preflight else "on"
                    return {"outcome": "reported", "text": json.dumps({
                        "result": f"Live Context: example\n- names: Desk Lamp\n  domain: light\n  state: '{state}'"})}

                client = SimpleNamespace(call_tool=AsyncMock(side_effect=call_tool))
                uncertain = set()
                bridge = AssistDelegation(client, catalog, uncertain_actions=uncertain,
                                          preview_target=("light", "Desk Lamp") if preflight else None)
                ws = FakeWS()
                session = self.session(transport="websocket", assist_delegation=bridge)
                with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
                    await session.start()

                def emit(kind, **data):
                    session._event({"type": "response.event", "delegation_id": "work",
                                    "event": {"type": kind, **data}})

                def hear(delta, start, end):
                    session._event({"type": "session.input_transcript.delta", "delta": delta,
                                    "start_ms": start, "end_ms": end})

                response = {"id": "work", "status": "completed", "usage": {"input_tokens": 100, "output_tokens": 8}}
                session._event({"type": "session.delegation.created", "offset_ms": 1200, "delegation": {
                    "target": "responses", "id": "work", "response_id": "work"}})
                emit("response.created", response={"id": "work"})
                for call_id in ("first", "second"):
                    emit("response.output_item.done", item={"type": "function_call",
                         "call_id": call_id, "name": "HassTurnOn", "arguments": arguments})
                emit("response.completed", response=response)
                await asyncio.wait_for(waiting.wait(), 1)
                if spoken:
                    hear("Cancel that.", 100, 500)
                    hear("Cancel that.", 1250, 1250)
                    hear("Cancel that timer.", 1400, 1750)
                    hear("Cancel", 1900, 2050)
                    self.assertFalse(session._assist_call.cancelled())
                    release.set()  # The HTTP result and the final word can arrive together.
                    hear(" that.", 2075, 2250)
                    await session._delegation_task
                    self.assertEqual(bridge.activity["spoken_cancellations"], 1)
                    messages = [json.loads(m["item"]["output"]) for m in ws.sent
                                if m["type"] == "response.item.create"]
                    expected = "cancelled" if preflight else "unknown"
                    self.assertEqual([m["outcome"] for m in messages], [expected, expected])
                    self.assertEqual(ws.sent[-1]["type"], "response.create")
                elif preflight:
                    release.set()
                    session.stop_interaction()
                else:
                    release.set()
                    session._event({"type": "error"})
                if not spoken:
                    with self.assertRaises(asyncio.CancelledError):
                        await session._delegation_task
                self.assertEqual([call.args[1] for call in client.call_tool.await_args_list],
                                 ["GetLiveContext"] if preflight else ["HassTurnOn", "GetLiveContext"])
                self.assertEqual(bridge.action_unconfirmed, not preflight)
                self.assertEqual(bool(uncertain), not preflight)
                self.assertTrue((await session.close()).finalization_confirmed)
                self.assertIsNone(self.settings.pending)
                if not spoken:
                    self.assertEqual([m["type"] for m in ws.sent], ["session.start", "session.close"])

    async def test_spoken_cancellation_before_tool_dispatch_preserves_the_connection(self):
        client = SimpleNamespace(call_tool=AsyncMock())
        bridge = AssistDelegation(client, [{"name": "HassTurnOn", "inputSchema": {"type": "object"}}])
        ws = FakeWS()
        session = self.session(transport="websocket", assist_delegation=bridge)
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()

        def emit(kind, **data):
            session._event({"type": "response.event", "delegation_id": "first",
                            "event": {"type": kind, **data}})

        response = {"id": "pending", "status": "completed", "usage": {"input_tokens": 100, "output_tokens": 8}}
        session._event({"type": "session.delegation.created", "offset_ms": 900, "delegation": {
            "target": "responses", "id": "first", "response_id": "pending"}})
        emit("response.created", response=response)
        session._event({"type": "session.input_transcript.delta", "delta": "Actually, never mind.",
                        "start_ms": 1100, "end_ms": 1700})
        emit("response.output_item.done", item={"type": "function_call", "call_id": "call1",
                                               "name": "HassTurnOn", "arguments": '{"name":"Desk Lamp"}'})
        emit("response.completed", response=response)
        await session._delegation_task
        client.call_tool.assert_not_awaited()
        output = next(m["item"]["output"] for m in ws.sent if m["type"] == "response.item.create")
        self.assertEqual(json.loads(output)["outcome"], "cancelled")
        await session.send_audio(bytes(640))
        self.assertTrue((await session.close()).finalization_confirmed)

    async def test_delegation_closes_at_the_reserved_response_limit(self):
        ws = FakeWS()
        catalog = [{"name": "HassTurnOn", "inputSchema": {"type": "object"}}]
        client = SimpleNamespace(call_tool=AsyncMock())
        session = self.session(transport="websocket", assist_delegation=AssistDelegation(client, catalog))
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()
        reserved = self.settings.pending["reserved_usd"]
        for i in range(3):
            session._event({"type": "response.event", "delegation_id": f"task_{i}", "event": {
                "type": "response.created", "response": {"id": f"response_{i}", "model": "gpt-6-luna"}}})
            if i < 2:
                session._event({"type": "response.event", "delegation_id": f"task_{i}", "event": {
                    "type": "response.completed", "response": {"id": f"response_{i}", "status": "completed",
                        "usage": {"input_tokens": 200, "output_tokens": 20}}}})
        self.assertEqual(session.metrics.error, "Delegated response budget limit reached")
        self.assertTrue((await session.close()).finalization_confirmed)
        self.assertIsNone(self.settings.pending)
        self.assertGreater(self.settings.spent_usd, .05)
        self.assertLess(self.settings.spent_usd, reserved)
        client.call_tool.assert_not_called()

    async def test_realtime_bench_limits_replies_and_settles_response_usage(self):
        class RealtimeWS(FakeWS):
            async def send_json(self, value):
                await super().send_json(value)
                if value["type"] == "session.update":
                    self.event({"type": "session.updated", "session": {"id": "realtime_offline"}})
                elif value["type"] == "response.create":
                    self.event({"type": "response.output_audio.delta",
                                "delta": base64.b64encode(bytes(960)).decode()})
                    self.event({"type": "response.done", "response": {"status": "completed",
                                "usage": {"input_tokens": 100, "output_tokens": 80}}})

        ws = RealtimeWS()
        http = FakeHTTP(ws)
        session = RealtimeBenchmarkSession(self.settings, on_audio=self.output, instructions="Answer briefly.",
                                           on_transcript=None)
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=http):
            await session.start()
        self.sessions.append(session)
        self.assertTrue(http.url.endswith("?model=" + REALTIME_MODEL))
        config = ws.sent[0]["session"]
        self.assertFalse(config["audio"]["input"]["turn_detection"]["create_response"])
        self.assertEqual(config["max_output_tokens"], 512)
        for turn in (1, 2):
            session.arm_turn()
            await session.send_audio(bytes(640))
            ws.event({"type": "input_audio_buffer.committed"})
            ws.event({"type": "input_audio_buffer.committed"})
            async with asyncio.timeout(1):
                while session.completed < turn:
                    await asyncio.sleep(.001)
        with self.assertRaises(LiveSessionError):
            session.arm_turn()
        self.assertEqual(sum(event["type"] == "response.create" for event in ws.sent), 2)
        self.assertTrue((await session.close()).finalization_confirmed)
        self.assertAlmostEqual(self.settings.spent_usd, .0052)
        self.assertIsNone(self.settings.pending)

    async def test_websocket_order_format_output_and_final_cumulative_usage(self):
        ws = FakeWS()
        http = FakeHTTP(ws)
        session = self.session(transport="websocket")
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=http):
            startup = await session.start()
        timing = startup.websocket_startup_breakdown()
        self.assertGreaterEqual(timing["websocket_connect_ms"], 0)
        self.assertGreaterEqual(timing["session_ready_wait_ms"], 0)
        self.assertAlmostEqual(sum(timing.values()), startup.ready_ms, delta=.11)
        self.assertEqual(http.url, "wss://api.openai.com/v1/live/sessions")
        config = ws.sent[0]["session"]
        self.assertEqual(config["model"], "gpt-live-1")
        self.assertEqual(config["audio"]["format"]["rate"], 16000)
        self.assertEqual(config["delegation"], {"type": "client"})
        self.assertFalse(config["store"])
        pcm = np.array([1, -300, 700, -1], dtype="<i2").tobytes()
        await session.send_audio(pcm)
        self.assertEqual(base64.b64decode(ws.sent[1]["audio"]), pcm)
        ws.event({"type": "session.output_audio.delta", "delta": base64.b64encode(pcm).decode()})
        ws.event({"type": "session.usage.updated", "usage": {"seconds": 12}})
        ws.event({"type": "session.usage.updated", "usage": {"seconds": 15}})
        for _ in range(20):
            if self.output.await_count:
                break
            await asyncio.sleep(0)
        self.output.assert_awaited_once_with(pcm, 16000)
        metrics = await session.close()
        self.assertTrue(metrics.finalization_confirmed)
        self.assertEqual(metrics.usage_seconds, 20)
        self.assertAlmostEqual(self.settings.spent_usd, 20 / 1200)
        self.assertFalse(metrics.acoustic_measurement)
        self.assertTrue(ws.closed and http.closed)
        await session.close()
        self.assertEqual(sum(event["type"] == "session.close" for event in ws.sent), 1)

    async def test_webrtc_sdp_media_no_start_or_audio_append_data_events(self):
        http = FakeHTTP(None)
        session = self.session()
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=http), \
             patch("jarvis.live_session.RTCPeerConnection", FakePC):
            startup = await session.start()
        self.assertEqual(startup.websocket_startup_breakdown(), {})
        self.assertEqual(http.request["json"]["transport"], {"type": "webrtc", "sdp": "offline-offer"})
        self.assertNotIn("format", http.request["json"]["session"]["audio"])
        self.assertEqual(session._pc.label, "oai-events")
        await session.send_audio(b"\x01\x00" * 320)
        frame = await session._track.recv()
        self.assertEqual(frame.sample_rate, 16000)
        self.assertEqual(frame.pts, 0)
        self.assertEqual(frame.to_ndarray()[0, 0], 1)
        self.assertEqual(session._channel.sent, [])
        result = await session.close()
        self.assertTrue(result.finalization_confirmed)
        self.assertTrue(session._pc.closed)

    async def test_remote_close_during_input_preserves_reason_and_final_usage(self):
        ws = FakeWS()
        session = self.session(transport="websocket")
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()
        ws.event({"type": "session.closed", "reason": "remote_hangup", "usage": {"seconds": 8}})
        await asyncio.wait_for(session._final.wait(), 1)
        with self.assertRaisesRegex(LiveSessionError, "closed during input \\(remote_hangup\\)"):
            await session.send_audio(bytes(640))
        metrics = await session.close()
        self.assertEqual(metrics.close_reason, "remote_hangup")
        self.assertEqual(metrics.usage_seconds, 8)
        self.assertFalse(any(event["type"] == "session.close" for event in ws.sent))

    async def test_no_terminal_event_blocks_retry_after_restart(self):
        ws = FakeWS(finalize=False)
        session = self.session(transport="websocket")
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()
        result = await session.close()
        self.assertFalse(result.finalization_confirmed)
        self.assertAlmostEqual(self.settings.pending["reserved_usd"], .05)
        with self.assertRaises(CloudBudgetError):
            CloudSettings(self.folder.name).reserve_attempt(transport="webrtc")

    async def test_webrtc_accepts_successful_200_and_records_http_status(self):
        http = FakeHTTP(None)
        http.status = 200
        session = self.session()
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=http), \
             patch("jarvis.live_session.RTCPeerConnection", FakePC):
            await session.start()
        self.assertEqual(session.metrics.http_status, 200)
        self.assertTrue((await session.close()).finalization_confirmed)

    async def test_failure_before_network_completion_preserves_reservation_and_hides_exception(self):
        session = self.session()
        with patch("jarvis.live_session.aiohttp.ClientSession", side_effect=RuntimeError(self.settings.api_key)):
            with self.assertRaises(LiveSessionError) as raised:
                await session.start()
        self.assertNotIn(self.settings.api_key, str(raised.exception))
        self.assertEqual(self.settings.pending["status"], "unconfirmed")

    async def test_watchdog_closes_without_usage_updates(self):
        ws = FakeWS()
        session = self.session(transport="websocket", max_seconds=.15)
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()
        await asyncio.sleep(.2)
        self.assertTrue(session.metrics.finalization_confirmed)
        self.assertTrue(ws.closed)

    async def test_cancel_start_finalizes_or_blocks_without_leaking_tasks(self):
        ws = FakeWS()
        async def delayed_send(value):
            ws.sent.append(value)
            if value["type"] == "session.close":
                ws.event({"type": "session.closed", "usage": {"seconds": 2}, "reason": "close_requested"})
        ws.send_json = delayed_send
        session = self.session(transport="websocket")
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            task = asyncio.create_task(session.start())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(session.metrics.finalization_confirmed)
        self.assertTrue(ws.closed)

    async def test_transcript_and_error_payloads_are_not_retained(self):
        ws = FakeWS()
        session = self.session(transport="websocket")
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()
        session._event({"type": "session.input_transcript.delta", "delta": "private household speech"})
        session._event({"type": "error", "error": {"message": "private household speech"}})
        await session.close()
        self.assertNotIn("private household speech", repr(session.metrics))

    async def test_standby_accepts_one_client_signoff_request_and_finalizes_usage(self):
        ws = FakeWS()
        on_transcript = Mock()
        on_signoff = Mock()
        session = self.session(transport="websocket", on_input_transcript=on_transcript,
                               on_signoff_request=on_signoff)
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()
        session._event({"type": "session.input_transcript.delta", "delta": "Goodbye"})
        requested = {"type": "session.delegation.created", "delegation": {"target": "client", "id": "turn-end"}}
        session._event({**requested, "delegation": {"target": "responses", "id": "other"}})
        session._event(requested)
        session._event(requested)
        await asyncio.sleep(0)
        on_transcript.assert_called_once_with("Goodbye")
        on_signoff.assert_called_once_with()
        acknowledgement = ws.sent[1].copy()
        self.assertTrue(acknowledgement.pop("event_id"))
        self.assertEqual(acknowledgement, {"type": "session.thinking.append", "delegation_id": "turn-end", "content": ""})
        metrics = await session.close()
        self.assertTrue(metrics.finalization_confirmed)
        self.assertNotIn("Goodbye", repr(metrics))

    async def test_seeded_dialogue_keeps_storage_off_and_captures_only_timed_speech(self):
        ws = FakeWS()
        callback = Mock()
        session = self.session(transport="websocket", voice="cedar", prior_exchanges=(("What city?", "Paris."),),
                               on_timed_transcript=callback)
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()
        config = ws.sent[0]["session"]
        self.assertEqual(config["audio"]["output"]["voice"], "cedar")
        self.assertFalse(config["store"])
        self.assertEqual(config["input"], [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "What city?"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Paris."}]},
        ])
        session._event({"type": "session.input_transcript.delta", "delta": "What city?",
                        "start_ms": 0, "end_ms": 0})
        session._event({"type": "session.input_transcript.delta", "delta": "Which river?",
                        "start_ms": 1000, "end_ms": 1300})
        callback.assert_called_once_with("user", "Which river?", 1000, 1300)
        metrics = await session.close()
        self.assertNotIn("What city?", repr(metrics))
        self.assertEqual(session._prior_exchanges, ())

    async def test_real_pyav_output_resampling_and_pcm_queue_bound(self):
        session = self.session()
        session._started_at = 0
        frame = AudioFrame.from_ndarray(np.ones((1, 960), dtype=np.int16), format="s16", layout="mono")
        frame.sample_rate = 48000
        frame.pts = 0
        frame.time_base = Fraction(1, 48000)
        track = SimpleNamespace(recv=AsyncMock(side_effect=[frame, RuntimeError("end")]))
        with patch.object(session, "_queue_audio", wraps=session._queue_audio) as queued:
            await session._receive_track(track)
        self.assertGreater(session.metrics.output_samples, 0)
        self.assertLess(session.metrics.output_samples, 960)
        pcm = queued.call_args.args[0]
        self.assertEqual(len(pcm) % 2, 0)
        self.assertEqual(len(pcm) // 2, session.metrics.output_samples)
        self.assertTrue(session._output.empty())
        self.assertEqual(session.metrics.discarded_output_samples, session.metrics.output_samples)
        source = _PCMTrack()
        source.append(bytes(32000))
        with self.assertRaises(LiveSessionError):
            source.append(b"\x00\x00")
        source.stop()

    async def test_late_media_clock_does_not_accelerate_buffered_speech(self):
        from aiortc.mediastreams import MediaStreamError
        fail = Mock()
        source = _PCMTrack(fail)
        source.started = 0
        source.append(b"\x01\x00" * 320)
        with self.assertRaises(MediaStreamError):
            await source.recv()
        fail.assert_called_once()
        self.assertEqual(source.samples, 0)
        source.stop()

    async def test_bounded_output_callback_failure_closes_and_clears_buffers(self):
        ws = FakeWS()
        self.output.side_effect = RuntimeError("must not be retained")
        session = self.session(transport="websocket")
        with patch("jarvis.live_session.aiohttp.ClientSession", return_value=FakeHTTP(ws)):
            await session.start()
        ws.event({"type": "session.output_audio.delta", "delta": base64.b64encode(b"\x01\x00" * 320).decode()})
        await asyncio.wait_for(session._final.wait(), 1)
        await session.close()
        self.assertEqual(session.metrics.error, "Cloud output callback failed")
        self.assertEqual(session._output_bytes, 0)
        self.assertTrue(ws.closed)
