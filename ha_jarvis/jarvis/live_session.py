"""Bounded GPT-Live transport with optional App-owned function delegation.

Caller supplies real-time paced PCM16LE mono at 16 kHz. Audio callback receives
the same format. start() is the only paid entry point; constructing is inert.
Protocol: https://developers.openai.com/api/docs/guides/voice-webrtc
and https://developers.openai.com/api/docs/guides/voice-websockets
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from fractions import Fraction
import json
import re
import time
import uuid

import aiohttp
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame, AudioResampler

from .assist_delegation import END_CONVERSATION
from .cloud_settings import BACKEND_RESPONSE_LIMIT, STANDBY_BACKEND_RESPONSE_LIMIT, CloudBudgetError


RATE = 16000
MODEL = "gpt-live-1"
VOICE = "marin"
LIVE_VOICES = (
    "marin", "cedar", "alloy", "ash", "ballad", "beacon", "bossa", "brise", "calandria", "cinder",
    "compas", "coral", "delta", "destello", "echo", "flitz", "gleam", "halo", "harema", "hornero",
    "juni", "luciernaga", "meridian", "nira", "noeul", "nuri", "pearl", "quartz", "ripple", "sage",
    "shida", "shimmer", "sillage", "stone", "tempo", "verse", "vesper", "willow",
)
INSTRUCTIONS = "Wait silently for a spoken question, then answer briefly in one short sentence. Do not greet, delegate work or use tools."
ASSIST_CANCELLATION = re.compile(
    r"(?:actually,? )?(?:please )?(?:cancel that|never ?mind|forget that|don['’]t do that)(?:,? please)?[.!]",
    re.IGNORECASE,
)


class LiveSessionError(RuntimeError):
    pass


@dataclass
class LiveMetrics:
    transport: str
    model: str = "gpt-live-1"
    session_id: str | None = None
    ready_ms: float | None = None
    websocket_connect_ms: float | None = None
    first_output_ms: float | None = None
    input_samples: int = 0
    output_samples: int = 0
    usage_seconds: float = 0.0
    finalization_confirmed: bool = False
    close_reason: str | None = None
    local_close_reason: str | None = None
    error: str | None = None
    input_transcript_events: int = 0
    output_transcript_events: int = 0
    timed_input_transcript_events: int = 0
    timed_output_transcript_events: int = 0
    discarded_output_samples: int = 0
    http_status: int | None = None
    acoustic_measurement: bool = False

    def websocket_startup_breakdown(self):
        if self.websocket_connect_ms is None:
            return {}
        return {"websocket_connect_ms": round(self.websocket_connect_ms, 1),
                "session_ready_wait_ms": round(self.ready_ms - self.websocket_connect_ms, 1)
                if self.ready_ms is not None else None}


class _PCMTrack(AudioStreamTrack):
    """20 ms frames on a continuous media clock, with bounded caller buffering."""
    def __init__(self, on_failure=None):
        super().__init__()
        self.pending = bytearray()
        self.started = None
        self.samples = 0
        self.on_failure = on_failure

    def append(self, pcm):
        if len(self.pending) + len(pcm) > RATE * 2:
            raise LiveSessionError("Cloud microphone queue exceeded one second")
        self.pending.extend(pcm)

    async def recv(self):
        if self.readyState != "live":
            from aiortc.mediastreams import MediaStreamError
            raise MediaStreamError
        if self.started is None:
            self.started = time.monotonic()
        if time.monotonic() - (self.started + self.samples / RATE) > .1:
            if self.on_failure:
                self.on_failure("Cloud media clock fell behind; stopping instead of accelerating speech")
            from aiortc.mediastreams import MediaStreamError
            raise MediaStreamError
        await asyncio.sleep(max(0, self.started + self.samples / RATE - time.monotonic()))
        count = min(640, len(self.pending))
        data = bytes(self.pending[:count]) + bytes(640 - count)
        del self.pending[:count]
        frame = AudioFrame(format="s16", layout="mono", samples=320)
        frame.planes[0].update(data)
        frame.sample_rate, frame.pts, frame.time_base = RATE, self.samples, Fraction(1, RATE)
        self.samples += 320
        return frame


class LiveSession:
    def __init__(self, settings, *, on_audio, transport="webrtc", max_seconds=60,
                 startup_timeout=15, close_timeout=10, greeting=None, on_transcript=None, instructions=INSTRUCTIONS,
                 output_buffer_seconds=1, assist_delegation=None,
                 on_input_transcript=None, on_signoff_request=None, on_timed_transcript=None, prior_exchanges=(),
                 allow_assist_action=None, voice=VOICE):
        if transport not in ("webrtc", "websocket"):
            raise ValueError("Choose webrtc or websocket")
        if voice not in LIVE_VOICES:
            raise ValueError("Choose a built-in GPT-Live voice")
        if not 0 < max_seconds <= 60 or not 0 < close_timeout < max_seconds or startup_timeout <= 0:
            raise ValueError("Cloud attempt must fit within 60 seconds including finalization")
        if assist_delegation is not None and (transport != "websocket" or greeting):
            raise ValueError("Assist delegation requires a conversational WebSocket session")
        if on_signoff_request is not None and (transport != "websocket" or greeting):
            raise ValueError("Sign-off requires a conversational WebSocket session")
        if prior_exchanges and (greeting or (assist_delegation is not None and allow_assist_action is None)):
            raise ValueError("Resumed conversations with Assist require a fresh spoken request")
        self.assist_delegation = assist_delegation
        self.allow_assist_action = allow_assist_action
        self._assist_response_limit = (STANDBY_BACKEND_RESPONSE_LIMIT if on_signoff_request is not None
                                       else BACKEND_RESPONSE_LIMIT)
        self._assist_active = set()
        self._assist_finished = set()
        self._assist_continuing = False
        self.assist_used_this_turn = False
        self._assist_unknown_this_turn = False
        self._delegation_task = None
        self._assist_call = None
        self._assist_cancel_after_ms = None
        self._assist_cancel_text = ""
        self._assist_cancelled = False
        self.assist_result_at = None
        self._assist_result_submitted = False
        self._assist_result_words = False
        self.settings, self.on_audio, self.transport, self.voice = settings, on_audio, transport, voice
        self.greeting, self.on_transcript = greeting, on_transcript
        self.on_input_transcript, self.on_signoff_request = on_input_transcript, on_signoff_request
        self.on_timed_transcript = on_timed_transcript
        self._prior_exchanges = prior_exchanges
        self._signoff_delegation_ids = set()
        self.instructions = instructions
        self.max_seconds, self.startup_timeout, self.close_timeout = max_seconds, startup_timeout, close_timeout
        self.metrics = LiveMetrics(transport)
        self._http = self._ws = self._pc = self._channel = self._track = None
        self._attempt = self._started_at = None
        self._ready = asyncio.Event()
        self._final = asyncio.Event()
        self._failed = asyncio.Event()
        self._tasks = set()
        self._close_task = None
        self._closing = self._disposed = False
        self._output = asyncio.Queue(maxsize=50 * output_buffer_seconds)
        self._output_limit_bytes = RATE * 2 * output_buffer_seconds
        self._output_bytes = 0
        self._output_blocked = False
        self._output_generation = 0
        self._input_paused = False
        self._input_lock = asyncio.Lock()
        self._interrupt_id = None
        self._interrupt_ready_generation = None
        self._fixture_playing = asyncio.Event()
        self._fixture_played = asyncio.Event()
        self._fixture_played_at = None
        self._last_output_activity = None

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _fail(self, message):
        # Never retain raw API exceptions/events, which can contain user content.
        if self.metrics.error is None:
            self.metrics.error = message
        self._failed.set()
        self.stop_interaction()
        if self._attempt and self._close_task is None:
            self._close_task = asyncio.create_task(self._close())

    def _config(self):
        config = {"model": MODEL, "instructions": self.greeting or self.instructions,
                  "store": False, "delegation": {"type": "client"},
                  "audio": {"output": {"voice": self.voice}}}
        if self.assist_delegation is not None:
            config["delegation"] = self.assist_delegation.config(signoff=self.on_signoff_request is not None)
        elif self.on_signoff_request is not None:
            config["delegation"]["acknowledgement_delay_ms"] = 800
        if self.greeting:
            config["input"] = [{"role": "developer", "content": [{"type": "input_text", "text": self.greeting}]}]
        elif self._prior_exchanges:
            config["input"] = [
                {"type": "message", "role": role, "content": [{"type": content_type, "text": text}]}
                for user, assistant in self._prior_exchanges
                for role, content_type, text in (("user", "input_text", user), ("assistant", "output_text", assistant))
            ]
        if self.transport == "websocket":
            config["audio"]["format"] = {"type": "audio/pcm", "rate": RATE}
        return config

    async def start(self):
        if self._attempt or self._closing:
            raise LiveSessionError("A LiveSession can start only once")
        backend_model = self.assist_delegation.model if self.assist_delegation is not None else None
        self._attempt = self.settings.reserve_attempt(transport=self.metrics.transport, backend_model=backend_model,
            backend_response_limit=self._assist_response_limit if backend_model is not None else BACKEND_RESPONSE_LIMIT)
        self._started_at = time.monotonic()
        try:
            self._http = aiohttp.ClientSession(
                headers={"Authorization": "Bearer " + self.settings.api_key},
                timeout=aiohttp.ClientTimeout(total=self.startup_timeout))
            self._spawn(self._watchdog())
            self._spawn(self._deliver_output())
            async with asyncio.timeout(min(self.startup_timeout, self.max_seconds - self.close_timeout)):
                if self.transport == "webrtc":
                    await self._start_webrtc()
                else:
                    await self._start_websocket()
                ready = asyncio.create_task(self._ready.wait())
                failed = asyncio.create_task(self._failed.wait())
                try:
                    await asyncio.wait((ready, failed), return_when=asyncio.FIRST_COMPLETED)
                    if self._failed.is_set():
                        raise LiveSessionError("Cloud session startup failed")
                finally:
                    ready.cancel()
                    failed.cancel()
                    await asyncio.gather(ready, failed, return_exceptions=True)
            return self.metrics
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                self.metrics.error = self.metrics.error or "Cloud session startup failed"
            await self.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise LiveSessionError(self.metrics.error) from None

    async def _start_websocket(self):
        self._ws = await self._http.ws_connect("wss://api.openai.com/v1/live/sessions", max_msg_size=262144)
        self.metrics.websocket_connect_ms = (time.monotonic() - self._started_at) * 1000
        self._spawn(self._receive_ws())
        await self._ws.send_json({"type": "session.start", "session": self._config()})

    async def _start_webrtc(self):
        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self._track = _PCMTrack(self._fail)
        self._pc.addTrack(self._track)
        self._channel = self._pc.createDataChannel("oai-events")

        @self._channel.on("message")
        def message(data):
            try:
                event = json.loads(data)
                self._event(event)
            except Exception:
                self._fail("Invalid cloud event")

        @self._channel.on("close")
        def channel_closed():
            if not self._final.is_set():
                self._fail("Cloud event channel closed without final usage")

        @self._pc.on("connectionstatechange")
        def state_changed():
            if self._pc.connectionState in ("failed", "closed") and not self._final.is_set():
                self._fail("Cloud media connection failed")

        @self._pc.on("track")
        def track_received(track):
            if track.kind == "audio":
                self._spawn(self._receive_track(track))

        await self._pc.setLocalDescription(await self._pc.createOffer())
        async with self._http.post("https://api.openai.com/v1/live/sessions",
                                   json={"session": self._config(), "transport": {
                                       "type": "webrtc", "sdp": self._pc.localDescription.sdp}},
                                   allow_redirects=False) as response:
            self.metrics.http_status = response.status
            if response.status not in (200, 201):
                raise LiveSessionError(f"Cloud session creation rejected (HTTP {response.status})")
            payload = await response.json()
        self.metrics.session_id = payload["session"]["id"]
        await self._pc.setRemoteDescription(RTCSessionDescription(payload["transport"]["sdp"], "answer"))

    async def _receive_ws(self):
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    self._event(json.loads(message.data))
                elif message.type == aiohttp.WSMsgType.ERROR:
                    self._fail("Cloud WebSocket receive failed")
                    break
        except Exception:
            self._fail("Invalid cloud WebSocket event")
        finally:
            if not self._final.is_set():
                self._fail("Cloud connection closed without final usage")

    async def _receive_track(self, track):
        resampler = AudioResampler(format="s16", layout="mono", rate=RATE)
        try:
            while not self._disposed:
                frame = await track.recv()
                for converted in resampler.resample(frame):
                    self._queue_audio(converted.to_ndarray().astype("<i2", copy=False).tobytes())
        except Exception:
            if not self._closing and not self._final.is_set():
                self._fail("Cloud audio track ended unexpectedly")

    @property
    def assist_work_pending(self):
        return bool(self._assist_active or self._assist_continuing
                    or (self._delegation_task is not None and not self._delegation_task.done()))

    def begin_turn(self):
        self.assist_result_at = None
        self._assist_result_submitted = False
        self._assist_result_words = False
        self.assist_used_this_turn = False
        self._assist_unknown_this_turn = False
        self._assist_cancel_after_ms = None
        self._assist_cancel_text = ""
        self._assist_cancelled = False

    def assist_reply_pending(self, first_voice_at, last_voice_at, *, require_words=False):
        if self.assist_work_pending:
            return True
        if self.assist_result_at is None:
            return False
        # The last PCM of a progress acknowledgment can arrive after the lookup.
        earlier_voice = first_voice_at is not None and first_voice_at < self.assist_result_at
        return (last_voice_at is None or last_voice_at < self.assist_result_at
                or (earlier_voice or require_words) and not self._assist_result_words)

    def _hold_assist_audio(self):
        return (self.assist_delegation is not None and not self._assist_result_submitted
                and (self.assist_delegation.preview_target is not None or self.assist_work_pending))

    def suppress_output(self):
        """Invalidate queued speech immediately; no server acknowledgment unmutes it."""
        self._output_blocked = True
        self._output_generation += 1
        self._interrupt_ready_generation = None
        while not self._output.empty():
            self.metrics.discarded_output_samples += len(self._output.get_nowait()) // 2
        self._output_bytes = 0
        return self._output_generation

    def stop_interaction(self):
        """Stop media and revoke pending Assist work before yielding to cleanup."""
        if self._closing:
            return
        self._closing = True
        self._assist_cancel_text = ""
        self.suppress_output()
        if self._delegation_task is not None and not self._delegation_task.done():
            self._delegation_task.cancel()

    async def interrupt_speech(self):
        """Probe a silent mixer interruption while caller-owned capture keeps queuing.

        This does not stop a physical player or cancel a Home Assistant action.
        The caller must seal the old reply and stop its player separately.
        """
        if self.transport != "websocket" or not self._ready.is_set() or self._closing or self._final.is_set():
            raise LiveSessionError("Speech interruption requires an active WebSocket session")
        started = time.monotonic()
        async with self._input_lock:
            if self._closing or self._final.is_set() or self._failed.is_set():
                raise LiveSessionError("Cloud session stopped before the interruption")
            if self._input_paused:
                raise LiveSessionError("A speech interruption is already pending")
            generation = self.suppress_output()
            self._input_paused = True
            self._fixture_playing.clear()
            self._fixture_played.clear()
            self._fixture_played_at = None
            self._interrupt_id = uuid.uuid4().hex
            # Fixtures always use 24 kHz PCM16, even on our 16 kHz session.
            try:
                await asyncio.wait_for(self._ws.send_json({"type": "session.output_audio.fixture.play",
                    "event_id": self._interrupt_id, "audio": base64.b64encode(bytes(480 * 2)).decode("ascii")}), .5)
            except Exception:
                self._interrupt_id = None
                self._fail("Cloud speech interruption command failed")
                raise LiveSessionError("Cloud speech interruption command failed") from None
        try:
            async with asyncio.timeout(2):
                await self._fixture_played.wait()
                if not self._fixture_playing.is_set():
                    raise LiveSessionError("The interrupt fixture had no acceptance acknowledgment")
                # Mixer completion is not an exact media boundary. Keep late
                # transport audio muted and observe a separate quiet interval.
                while time.monotonic() - max(self._fixture_played_at, self._last_output_activity or started) < .25:
                    if self.metrics.error or self._closing:
                        raise LiveSessionError("Cloud interruption stopped before its quiet interval")
                    await asyncio.sleep(.02)
            if self._closing or generation != self._output_generation:
                raise LiveSessionError("Speech interruption was superseded")
            self._interrupt_ready_generation = generation
            return {"generation": generation,
                    "mixer_ack_ms": round((self._fixture_played_at - started) * 1000, 1),
                    "quiet_observed_ms": round((time.monotonic() - max(self._fixture_played_at,
                                                                      self._last_output_activity or started)) * 1000, 1),
                    "input_pause_ms": round((time.monotonic() - started) * 1000, 1),
                    "scope": "server mixer and received audio; physical playback is separate"}
        except (OSError, RuntimeError, TimeoutError):
            self._fail("Cloud speech interruption could not establish a quiet interval")
            raise
        finally:
            self._interrupt_id = None

    def resume_after_interrupt(self, generation):
        if (type(generation) is not int or self._closing or self._final.is_set() or generation != self._output_generation
                or generation != self._interrupt_ready_generation or not self._input_paused):
            raise LiveSessionError("No matching completed speech interruption is available")
        # Audio received while muted is discarded, never saved for later play.
        while not self._output.empty():
            self.metrics.discarded_output_samples += len(self._output.get_nowait()) // 2
        self._output_bytes = 0
        self._interrupt_ready_generation = None
        self._input_paused = False
        self._output_blocked = False

    def _queue_audio(self, pcm):
        if self._closing or not pcm:
            return
        if len(pcm) % 2 or len(pcm) > self._output_limit_bytes:
            self._fail("Cloud output queue exceeded its limit or contained invalid PCM")
            return
        if max(abs(sample) for sample in memoryview(pcm).cast("h")) >= 16:
            self._last_output_activity = time.monotonic()
        if self._output_blocked or self._hold_assist_audio():
            self.metrics.discarded_output_samples += len(pcm) // 2
            return
        if self._output_bytes + len(pcm) > self._output_limit_bytes:
            self._fail("Cloud output queue exceeded its limit or contained invalid PCM")
            return
        try:
            self._output.put_nowait(pcm)
        except asyncio.QueueFull:
            self._fail("Cloud output queue overflow")
            return
        self._output_bytes += len(pcm)
        self.metrics.output_samples += len(pcm) // 2
        if self.metrics.first_output_ms is None:
            self.metrics.first_output_ms = (time.monotonic() - self._started_at) * 1000

    async def _deliver_output(self):
        try:
            while True:
                pcm = await self._output.get()
                self._output_bytes -= len(pcm)
                if not self._output_blocked and not self._hold_assist_audio():
                    await self.on_audio(pcm, RATE)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("Cloud output callback failed")

    def _event(self, event):
        if self._final.is_set():
            return
        kind = event.get("type")
        if kind == "session.started":
            self.metrics.session_id = event["session"]["id"]
            if not self._ready.is_set():
                self.metrics.ready_ms = (time.monotonic() - self._started_at) * 1000
            self._ready.set()
        elif kind in ("session.usage.updated", "session.closed"):
            seconds = event["usage"]["seconds"]
            self.settings.observe_usage(self._attempt, seconds)
            self.metrics.usage_seconds = max(self.metrics.usage_seconds, seconds)
            if kind == "session.closed":
                self.settings.finalize(self._attempt, seconds)
                self.metrics.finalization_confirmed = True
                reason = event.get("reason")
                self.metrics.close_reason = reason if reason in (
                    "close_requested", "expired", "content", "remote_hangup", "connection_lost") else "unknown"
                self._final.set()
                self.stop_interaction()
                if not self._ready.is_set():
                    self._fail("Cloud session closed before startup completed")
                if self._close_task is None:
                    self._close_task = asyncio.create_task(self._close())
        elif kind == "session.output_audio.delta" and self.transport == "websocket":
            self._queue_audio(base64.b64decode(event["delta"], validate=True))
        elif (kind in ("session.output_audio.fixture.playing", "session.output_audio.fixture.played")
              and self._interrupt_id is not None and event.get("client_event_id") == self._interrupt_id):
            if kind.endswith(".playing"):
                self._fixture_playing.set()
            elif not self._fixture_played.is_set():
                self._fixture_played_at = time.monotonic()
                self._fixture_played.set()
        elif kind in ("session.input_transcript.delta", "session.output_transcript.delta"):
            self._transcript_event(kind, event)
        elif kind == "error":
            self._fail("Cloud session reported an error")
        elif kind == "session.delegation.created":
            delegation = event.get("delegation")
            if self._closing or not self._ready.is_set() or not isinstance(delegation, dict):
                return
            if self.assist_delegation is not None:
                response_id = delegation.get("response_id")
                if (delegation.get("target") == "responses" and isinstance(response_id, str)
                        and 0 < len(response_id) <= 128 and response_id not in self._assist_finished):
                    self._assist_active.add(response_id)
                    self.assist_used_this_turn = True
                    offset = event.get("offset_ms")
                    if self._assist_cancel_after_ms is None and type(offset) is int and offset >= 0:
                        self._assist_cancel_after_ms = offset
            elif self.on_signoff_request is None:
                self._fail("Unexpected delegation in voice-only benchmark")
            else:
                if delegation.get("target") != "client":
                    return
                ident = delegation.get("id")
                if (not isinstance(ident, str) or not 0 < len(ident) <= 128
                        or ident in self._signoff_delegation_ids or len(self._signoff_delegation_ids) >= 32):
                    return
                self._signoff_delegation_ids.add(ident)
                self.on_signoff_request()
                self._spawn(self._acknowledge_signoff_request(ident))
        elif kind == "response.event" and self.assist_delegation is not None:
            try:
                nested = event.get("event")
                if isinstance(nested, dict) and nested.get("type") in (
                        "response.created", "response.completed", "response.failed",
                        "response.incomplete", "response.cancelled"):
                    response = nested.get("response") or {}
                    response_id = response.get("id")
                    count = self.settings.observe_backend_response(self._attempt, response_id,
                        usage=response.get("usage"), model=response.get("model"))
                    if count > self._assist_response_limit:
                        self._fail("Delegated response budget limit reached")
                        return
                    if nested["type"] == "response.created":
                        if response_id not in self._assist_finished:
                            self._assist_active.add(response_id)
                            self.assist_used_this_turn = True
                            self._assist_continuing = False
                    else:
                        self._assist_active.discard(response_id)
                        self._assist_finished.add(response_id)
                if self._closing or self._failed.is_set():
                    return
                batch = self.assist_delegation.observe(event)
                if batch:
                    if self._delegation_task is not None and not self._delegation_task.done():
                        raise ValueError("Overlapping Assist execution")
                    if count >= self._assist_response_limit:
                        self._fail("Delegated response budget cannot cover the continuation")
                        return
                    self._assist_result_submitted = False
                    self._delegation_task = self._spawn(self._return_assist_results(batch))
            except (CloudBudgetError, TypeError, AttributeError):
                self.settings.mark_backend_unconfirmed(self._attempt)
                self._fail("Could not confirm delegated usage")
            except ValueError:
                self._fail("Invalid Assist delegation event")

    async def _acknowledge_signoff_request(self, ident):
        if self._closing or self._final.is_set():
            return
        try:
            await self._ws.send_json({"type": "session.thinking.append", "event_id": uuid.uuid4().hex,
                                      "delegation_id": ident, "content": ""})
        except (OSError, RuntimeError):
            self._fail("Could not acknowledge the sign-off request")

    async def _return_assist_results(self, batch):
        try:
            for call_id, (name, arguments) in batch.calls.items():
                if self._closing or self._final.is_set():
                    return
                if self._assist_cancelled:
                    result = self._cancelled_assist_result()
                else:
                    self._assist_call = asyncio.create_task(self._execute_assist_call(call_id, name, arguments))
                    try:
                        result = await self._assist_call
                    except asyncio.CancelledError:
                        if self._closing or not self._assist_cancelled:
                            raise
                        result = self._cancelled_assist_result()
                if self._closing or self._final.is_set():
                    return
                await self._ws.send_json({"type": "response.item.create", "event_id": uuid.uuid4().hex,
                    "item": {"type": "function_call_output", "call_id": call_id,
                             "output": json.dumps(result, allow_nan=False)}})
            if not self._closing and not self._final.is_set():
                self._assist_continuing = True
                self._assist_result_submitted = True
                if any(name != END_CONVERSATION for name, _ in batch.calls.values()):
                    self.assist_result_at = time.monotonic()
                    self._assist_result_words = False
                await self._ws.send_json({"type": "response.create", "event_id": uuid.uuid4().hex})
        except Exception:
            self._fail("Could not return Assist delegation results")
        finally:
            self._assist_call = None

    def _cancelled_assist_result(self):
        if self._assist_unknown_this_turn:
            return {"outcome": "unknown",
                    "reason": "You cancelled while a command was in flight. Check the device state before retrying."}
        return {"outcome": "cancelled", "reason": "You cancelled the pending request."}

    def _check_assist_cancellation(self, delta, start, end):
        if (self._assist_cancel_after_ms is None or self._assist_cancelled or self._assist_result_submitted
                or not self.assist_work_pending or self._assist_call is not None and self._assist_call.done()
                or type(start) is not int or type(end) is not int or not self._assist_cancel_after_ms <= start < end):
            return
        self._assist_cancel_text = (self._assist_cancel_text + delta)[-96:]
        phrase = " ".join(self._assist_cancel_text.split())
        if ASSIST_CANCELLATION.fullmatch(phrase):
            self._assist_cancelled = True
            self._assist_unknown_this_turn |= self.assist_delegation.action_unconfirmed
            self.assist_delegation.activity["spoken_cancellations"] += 1
            if self._assist_call is not None:
                self._assist_call.cancel()
        if phrase.endswith((".", "!", "?")):
            self._assist_cancel_text = ""

    async def _execute_assist_call(self, call_id, name, arguments):
        if name == END_CONVERSATION and self.on_signoff_request is not None:
            try:
                empty = json.loads(arguments) == {}
            except (ValueError, RecursionError):
                empty = False
            if empty and self.on_signoff_request():
                return {"outcome": "accepted"}
            return {"outcome": "rejected", "reason": "The current speech does not confirm a sign-off"}
        if self.allow_assist_action is not None and not self.allow_assist_action():
            self.assist_delegation.activity["missing_current_request"] += 1
            return {"outcome": "rejected", "reason": "An explicit current spoken request is required"}
        if self._assist_unknown_this_turn and not self.assist_delegation.is_state_query(name):
            return {"outcome": "unknown", "reason": "An earlier attempt in this turn is unconfirmed. Check device state."}
        result = await self.assist_delegation.execute(call_id, name, arguments)
        self._assist_unknown_this_turn |= result["outcome"] == "unknown"
        return result

    def _transcript_event(self, kind, event):
        delta = event.get("delta")
        if not isinstance(delta, str):
            return
        is_input = kind == "session.input_transcript.delta"
        start, end = event.get("start_ms"), event.get("end_ms")
        field = "input_transcript_events" if is_input else "output_transcript_events"
        setattr(self.metrics, field, getattr(self.metrics, field) + 1)
        if type(start) is int and start == end:
            # A zero-length transcript interval can describe seeded text, not speech.
            return
        if is_input and self.assist_delegation is not None and not self._closing:
            self._check_assist_cancellation(delta, start, end)
        if not is_input and self.assist_result_at is not None and any(ch.isalpha() for ch in delta):
            self._assist_result_words = True
        callback = self.on_input_transcript if is_input else self.on_transcript
        if callback is not None and (not is_input or not self._closing):
            callback(delta)
        if (self.on_timed_transcript is not None and not self._closing
                and type(start) is int and type(end) is int and 0 <= start < end):
            field = "timed_" + field
            setattr(self.metrics, field, getattr(self.metrics, field) + 1)
            self.on_timed_transcript("user" if is_input else "assistant", delta, start, end)

    async def send_audio(self, pcm):
        if not self._ready.is_set() or self._closing or self._final.is_set() or self._failed.is_set():
            if self.metrics.error:
                raise LiveSessionError(self.metrics.error)
            if self.metrics.local_close_reason == "preview_limit":
                raise LiveSessionError("The GPT-Live preview reached its time limit")
            if self.metrics.close_reason is not None:
                raise LiveSessionError(f"GPT-Live closed during input ({self.metrics.close_reason})")
            raise LiveSessionError("Cloud session is not accepting audio")
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > 3200:
            raise ValueError("Supply up to 100 ms of complete PCM16LE mono samples at 16 kHz")
        async with self._input_lock:
            if self._closing or self._final.is_set() or self._failed.is_set():
                raise LiveSessionError("Cloud session stopped before the queued microphone send")
            if self._input_paused:
                raise LiveSessionError("Keep microphone input queued locally until the interruption resumes")
            try:
                if self.transport == "webrtc":
                    self._track.append(pcm)
                else:
                    await self._ws.send_json({"type": "session.input_audio.append",
                                              "audio": base64.b64encode(pcm).decode("ascii")})
                self.metrics.input_samples += len(pcm) // 2
            except Exception:
                self._fail("Cloud microphone send failed")
                raise LiveSessionError("Cloud microphone send failed") from None

    async def request_greeting(self):
        if not self.greeting or self.transport != "websocket" or not self._ready.is_set() or self._closing:
            raise LiveSessionError("Greeting session is not ready")
        await self._ws.send_json({"type": "session.commentary.append", "event_id": "cached_ack",
                                  "delegation_id": None, "content": self.greeting})

    def media_deadline(self):
        return self._started_at + self.max_seconds - self.close_timeout

    async def _watchdog(self):
        await asyncio.sleep(max(0, self.media_deadline() - time.monotonic()))
        if self._close_task is None:
            self.metrics.local_close_reason = "preview_limit"
            await self.close()

    async def close(self):
        self.stop_interaction()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        return await asyncio.shield(self._close_task)

    async def _close(self):
        if self._disposed:
            return self.metrics
        try:
            if self._attempt and not self._final.is_set():
                remaining = max(0, self.max_seconds - (time.monotonic() - self._started_at))
                try:
                    async with asyncio.timeout(min(self.close_timeout, remaining)):
                        if self._ws is not None and not self._ws.closed:
                            await self._ws.send_json({"type": "session.close"})
                        elif self._channel is not None and self._channel.readyState == "open":
                            self._channel.send(json.dumps({"type": "session.close"}))
                        else:
                            raise LiveSessionError("No control connection available for finalization")
                        await self._final.wait()
                except Exception:
                    self.metrics.error = self.metrics.error or "Cloud finalization unconfirmed; paid retries blocked"
                if not self._final.is_set():
                    self.settings.mark_unconfirmed(self._attempt)
        finally:
            self._disposed = True
            self._prior_exchanges = ()
            current = asyncio.current_task()
            tasks = [task for task in self._tasks if task is not current]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._track is not None:
                self._track.stop()
                self._track.pending.clear()
            try:
                async with asyncio.timeout(2):
                    if self._pc is not None:
                        await self._pc.close()
                    if self._ws is not None:
                        await self._ws.close()
            except Exception:
                self.metrics.error = self.metrics.error or "Cloud transport cleanup timed out"
            finally:
                if self._http is not None:
                    await self._http.close()
            while not self._output.empty():
                self._output.get_nowait()
            self._output_bytes = 0
        return self.metrics
