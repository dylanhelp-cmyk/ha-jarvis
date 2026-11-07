"""Bounded GPT-Live transport bench; no tools, recording, or acoustic claims.

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
import time

import aiohttp
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame, AudioResampler


RATE = 16000
MODEL = "gpt-live-1"
VOICE = "marin"
INSTRUCTIONS = "Wait silently for a spoken question, then answer briefly in one short sentence. Do not greet, delegate work or use tools."


class LiveSessionError(RuntimeError):
    pass


@dataclass
class LiveMetrics:
    transport: str
    model: str = "gpt-live-1"
    session_id: str | None = None
    ready_ms: float | None = None
    first_output_ms: float | None = None
    input_samples: int = 0
    output_samples: int = 0
    usage_seconds: float = 0.0
    finalization_confirmed: bool = False
    close_reason: str | None = None
    error: str | None = None
    http_status: int | None = None
    acoustic_measurement: bool = False


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
                 startup_timeout=15, close_timeout=10, greeting=None, on_transcript=None, instructions=INSTRUCTIONS):
        if transport not in ("webrtc", "websocket"):
            raise ValueError("Choose webrtc or websocket")
        if not 0 < max_seconds <= 60 or not 0 < close_timeout < max_seconds or startup_timeout <= 0:
            raise ValueError("Cloud attempt must fit within 60 seconds including finalization")
        self.settings, self.on_audio, self.transport = settings, on_audio, transport
        self.greeting, self.on_transcript = greeting, on_transcript
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
        self._output = asyncio.Queue(maxsize=50)
        self._output_bytes = 0

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
        if self._attempt and self._close_task is None:
            self._close_task = asyncio.create_task(self._close())

    def _config(self):
        config = {"model": MODEL, "instructions": self.greeting or self.instructions,
                  "store": False, "delegation": {"type": "client"},
                  "audio": {"output": {"voice": VOICE}}}
        if self.greeting:
            config["input"] = [{"role": "developer", "content": [{"type": "input_text", "text": self.greeting}]}]
        if self.transport == "websocket":
            config["audio"]["format"] = {"type": "audio/pcm", "rate": RATE}
        return config

    async def start(self):
        if self._attempt or self._disposed:
            raise LiveSessionError("A LiveSession can start only once")
        self._attempt = self.settings.reserve_attempt(transport=self.metrics.transport)
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

    def _queue_audio(self, pcm):
        if self._closing or not pcm:
            return
        if len(pcm) % 2 or self._output_bytes + len(pcm) > RATE * 2:
            self._fail("Cloud output queue exceeded one second or contained invalid PCM")
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
                if not self._ready.is_set():
                    self._fail("Cloud session closed before startup completed")
                if self._close_task is None:
                    self._close_task = asyncio.create_task(self._close())
        elif kind == "session.output_audio.delta" and self.transport == "websocket":
            self._queue_audio(base64.b64decode(event["delta"], validate=True))
        elif kind == "session.output_transcript.delta" and self.on_transcript is not None:
            self.on_transcript(event["delta"])
        elif kind == "error":
            self._fail("Cloud session reported an error")
        # Transcript text and delegated payloads are never retained or logged.
        elif kind == "session.delegation.created":
            self._fail("Unexpected delegation in voice-only benchmark")

    async def send_audio(self, pcm):
        if not self._ready.is_set() or self._closing or self._final.is_set() or self._failed.is_set():
            raise LiveSessionError("Cloud session is not accepting audio")
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > 3200:
            raise ValueError("Supply up to 100 ms of complete PCM16LE mono samples at 16 kHz")
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

    async def _watchdog(self):
        await asyncio.sleep(max(0, self.max_seconds - self.close_timeout - (time.monotonic() - self._started_at)))
        await self.close()

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        return await asyncio.shield(self._close_task)

    async def _close(self):
        if self._disposed:
            return self.metrics
        self._closing = True
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
