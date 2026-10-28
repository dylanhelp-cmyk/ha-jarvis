"""Real playback transports. A control acknowledgement is never an audio measurement.

WiiM API: https://www.wiimhome.com/pdf/HTTP%20API%20for%20WiiM%20Products.pdf
SlimProto: music-assistant/aioslimproto commit f133feab2d94bc9be7e4704548e18be4bbc53727
AirPlay: https://pyatv.dev/development/stream/ (tested API: 0.18.0)
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit


class TransportError(RuntimeError):
    """Playback command failed or a required capability is unavailable."""


MIME_TYPES = {"wav": "audio/wav", "flac": "audio/flac"}
SLIMPROTO_REQUIREMENT = "aioslimproto @ https://github.com/music-assistant/aioslimproto/archive/f133feab2d94bc9be7e4704548e18be4bbc53727.zip"
SLIM_PCM_RATES = {8000, 11000, 12000, 16000, 22000, 24000, 44100, 48000,
                  88200, 96000, 176400, 192000, 352800, 384000}


def _codec(config: Mapping[str, Any]) -> str:
    codec = str(config.get("codec", "wav")).lower()
    if codec not in MIME_TYPES:
        raise TransportError(f"Unsupported stream codec {codec!r}; choose wav or flac")
    return codec


def _url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise TransportError("Playback requires an absolute HTTP(S) stream URL")
    if any(character in url for character in "\r\n"):
        raise TransportError("Stream URL contains a newline")


class Transport:
    """Shared lifecycle telemetry, explicitly separate from audible onset."""

    name = "base"

    def __init__(self) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self.active = False
        self.last_error: str | None = None

    def _event(self, name: str, **data: Any) -> None:
        self.events.append({"event": name, "monotonic_ns": time.monotonic_ns(), **data})

    def snapshot(self) -> dict[str, Any]:
        return {"transport": self.name, "active": self.active,
                "last_error": self.last_error, "events": list(self.events),
                "audible_onset_measured": False}

    async def close(self) -> None:
        await self.stop()


class NativeHTTPTransport(Transport):
    """WiiM's URL playback command; the player pulls the progressive HTTP body."""

    name = "native_http"

    def __init__(self, device_url: str, *,
                 verify_ssl: bool = True, timeout: float = 8.0,
                 session: Any = None) -> None:
        super().__init__()
        _url(device_url)
        self.device_url = device_url.rstrip("/")
        parsed = urlsplit(self.device_url)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise TransportError("Device URL must contain only scheme, host and optional port")
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = session
        self._owns_session = session is None
        self._command_lock = asyncio.Lock()

    async def _command(self, command: str) -> None:
        try:
            import aiohttp
        except ImportError as exc:
            raise TransportError("Native HTTP requires aiohttp; install project dependencies") from exc
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        # Encode the entire command once. In particular, an embedded URL's &, +,
        # # and percent escapes must not become parameters of httpapi.asp.
        request_url = self.device_url + "/httpapi.asp?" + urlencode({"command": command})
        async with self._command_lock:
            self._event("command_sent", command=command.split(":", 2)[:2])
            try:
                async with self._session.get(request_url, ssl=None if self.verify_ssl else False,
                                             allow_redirects=False,
                                             timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                    raw_body = bytearray()
                    async for part in response.content.iter_chunked(4096):
                        raw_body.extend(part)
                        if len(raw_body) > 65536:
                            raise TransportError("WiiM command response exceeded 64KB")
                    body = raw_body.decode("utf-8", errors="replace").strip()
                    if response.status < 200 or response.status >= 300:
                        raise TransportError(f"WiiM returned HTTP {response.status}: {body[:160]}")
                    if body.strip('"').upper() != "OK":
                        raise TransportError(f"WiiM rejected command: {body[:160]}")
            except (aiohttp.ClientError, asyncio.TimeoutError, TransportError) as exc:
                self.last_error = str(exc)
                self._event("command_failed", error=self.last_error)
                raise TransportError(self.last_error) from exc
            self._event("command_acknowledged", note="Control response only; audio onset unknown")

    async def play(self, stream_url: str, config: Mapping[str, Any]) -> None:
        _url(stream_url)
        _codec(config)
        self.last_error = None
        # Keep the command ordered ahead of stop even if the requesting HTTP
        # handler is cancelled after the player has received the request.
        pending = asyncio.create_task(self._command("setPlayerCmd:play:" + stream_url))
        try:
            await asyncio.shield(pending)
            self.active = True
        except asyncio.CancelledError:
            try:
                await pending
                self.active = True
            except Exception:
                pass
            await self.stop()
            raise

    async def stop(self) -> None:
        self._event("stop_requested")
        await self._command("setPlayerCmd:stop")
        self.active = False
        self._event("stop_acknowledged", note="Device output drain time unknown")

    async def close(self) -> None:
        try:
            if self.active:
                await self.stop()
        finally:
            if self._owns_session and self._session is not None:
                await self._session.close()
                self._session = None


class SlimProtoTransport(Transport):
    """An LMS-compatible local server using aioslimproto, without rewriting packets.

    start() is explicit: it binds TCP and UDP discovery on control_port. Players
    must connect to this server; there is no outbound connection to the WiiM.
    aioslimproto binds TCP to all interfaces; server_host is its advertised LAN IP.
    """

    name = "slimproto"

    def __init__(self, server_host: str, control_port: int = 3483, *,
                 player_id: str | None = None, server_factory: Callable[..., Any] | None = None,
                 callback: Callable[[dict[str, Any]], None] | None = None,
                 timeout: float = 8.0) -> None:
        super().__init__()
        if server_host in ("", "0.0.0.0", "::"):
            raise TransportError("SlimProto requires an advertised LAN interface IP")
        self.server_host = server_host
        self.control_port = control_port
        self.player_id = player_id
        self.timeout = timeout
        self._factory = server_factory
        self._callback = callback
        self._server: Any = None
        self._unsubscribe: Any = None
        self._player: Any = None
        self._operation_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._server is not None:
            return
        factory = self._factory
        if factory is None:
            try:
                from aioslimproto import SlimServer
            except ImportError as exc:
                raise TransportError("SlimProto unavailable: install the pinned aioslimproto source dependency") from exc
            factory = SlimServer
        server = factory(ip_address=self.server_host, control_port=self.control_port,
                         name="Jarvis audio testbench", cli_port=0, cli_port_json=0)
        self._unsubscribe = server.subscribe(self._on_event)
        try:
            await server.start()
        except BaseException as exc:
            self._unsubscribe()
            self._unsubscribe = None
            try:
                await server.stop()
            except Exception as cleanup_error:
                self._event("server_cleanup_failed", error=str(cleanup_error))
            if isinstance(exc, asyncio.CancelledError):
                raise
            self.last_error = (f"SlimProto server failed on TCP/UDP port {self.control_port}: {exc}. "
                               "Check whether another LMS/Music Assistant server already owns the port.")
            self._event("server_start_failed", error=self.last_error)
            raise TransportError(self.last_error) from exc
        self._server = server
        self._event("server_started", advertised_host=self.server_host, control_port=self.control_port)

    def _on_event(self, event: Any) -> None:
        event_type = getattr(event.type, "value", str(event.type))
        self._event("player_event", type=event_type, player_id=event.player_id)
        if self._player is not None and event.player_id == self._player.player_id:
            if event_type in ("player_disconnected", "player_decoder_error"):
                self.last_error = event_type
                self.active = False
        if self._callback is not None:
            self._callback(self.events[-1])

    def players(self) -> list[dict[str, Any]]:
        if self._server is None:
            return []
        return [{"player_id": player.player_id, "name": getattr(player, "name", player.player_id),
                 "connected": player.connected, "supported_codecs": list(player.supported_codecs),
                 "max_sample_rate": player.max_sample_rate}
                for player in self._server.players]

    async def play(self, stream_url: str, config: Mapping[str, Any]) -> None:
        _url(stream_url)
        codec = _codec(config)
        if urlsplit(stream_url).scheme != "http":
            raise TransportError("SlimProto testbench requires HTTP, avoiding silent library HTTPS fallback")
        if self._server is None:
            raise TransportError("Start SlimProto server and connect the player first")
        requested = config.get("player_id") or self.player_id
        players = [p for p in self._server.players if p.connected and (not requested or p.player_id == requested)]
        if len(players) != 1:
            raise TransportError("Select one connected SlimProto player by player_id")
        player = players[0]
        required = "flc" if codec == "flac" else "pcm"
        if required not in player.supported_codecs:
            raise TransportError(f"Player does not advertise {required} support")
        if int(config.get("sample_rate", 44100)) > int(player.max_sample_rate):
            raise TransportError("Sample rate exceeds the player's advertised maximum")
        stream_threshold = int(config.get("stream_threshold_kb", 1))
        output_threshold = int(config.get("output_threshold_tenths", 1))
        if not (0 <= stream_threshold <= 255 and 0 <= output_threshold <= 255):
            raise TransportError("SlimProto thresholds must be 0..255")
        threshold_support = "stream_threshold" in inspect.signature(player.play_url).parameters
        if not threshold_support:
            raise TransportError("Installed aioslimproto lacks buffer controls; install the pinned source dependency")
        mime_type = MIME_TYPES[codec]
        if codec == "wav":
            # SlimProto uses headerless PCM. The HTTP response must repeat these
            # parameters: the library sends another decoder command on RESP.
            sample_rate = int(config.get("sample_rate", 44100))
            sample_size = int(config.get("sample_size", config.get("bit_depth", 16)))
            channels = int(config.get("channels", 2))
            if channels not in (1, 2) or sample_size not in (8, 16, 24, 32):
                raise TransportError("SlimProto WAV requires 1/2 channels and 8/16/24/32 bit PCM")
            if sample_rate not in SLIM_PCM_RATES:
                raise TransportError("Sample rate has no SlimProto PCM format code")
            mime_type = f"audio/pcm;rate={sample_rate};bitrate={sample_size};channels={channels}"
        self._player = player
        self.last_error = None
        async def send() -> None:
            async with self._operation_lock:
                self._event("play_command_sent", player_id=player.player_id,
                            stream_threshold_kb=stream_threshold, output_threshold_tenths=output_threshold)
                options = {"mime_type": mime_type, "autostart": True, "send_flush": True}
                if threshold_support:
                    options.update(stream_threshold=stream_threshold, output_threshold=output_threshold)
                await asyncio.wait_for(player.play_url(stream_url, **options), self.timeout)
                if not player.connected:
                    raise TransportError("SlimProto player disconnected during play command")
        pending = asyncio.create_task(send())
        try:
            await asyncio.shield(pending)
            self.active = True
            self._event("play_command_completed", note="No acoustic measurement")
        except asyncio.CancelledError:
            try:
                await pending
                self.active = True
            except Exception:
                pass
            await self.stop()
            raise
        except Exception as exc:
            self.last_error = str(exc)
            raise

    async def stop(self) -> None:
        self._event("stop_requested")
        async with self._operation_lock:
            if self._player is not None:
                if not self._player.connected:
                    raise TransportError("Cannot send stop: SlimProto player disconnected")
                await asyncio.wait_for(self._player.stop(), self.timeout)
                if not self._player.connected:
                    raise TransportError("SlimProto disconnected while sending stop")
        self.active = False
        self._event("stop_command_completed", note="No acoustic measurement")

    async def close(self) -> None:
        try:
            if self.active:
                await self.stop()
        finally:
            if self._unsubscribe is not None:
                self._unsubscribe()
                self._unsubscribe = None
            if self._server is not None:
                await self._server.stop()
                self._server = None


class AirPlayTransport(Transport):
    """Real RAOP baseline via pyatv, retaining the connection for same-session stop.

    pyatv may buffer/decode/resample HTTP audio. This adapter makes no low-latency
    or progressive-decoder guarantee. An unpaired receiver can require setup.
    """

    name = "airplay"

    def __init__(self, device_host: str, *,
                 connection: Any = None, scan_timeout: float = 5.0,
                 stop_timeout: float = 5.0) -> None:
        super().__init__()
        self.device_host = device_host
        self.scan_timeout = scan_timeout
        self.stop_timeout = stop_timeout
        self._connection = connection
        self._stream_task: asyncio.Task | None = None

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("pyatv") is not None

    async def _connect(self) -> None:
        if self._connection is not None:
            return
        try:
            import pyatv
            from pyatv.const import Protocol
        except ImportError as exc:
            raise TransportError("AirPlay unavailable: install pyatv==0.18.0") from exc
        loop = asyncio.get_running_loop()
        configs = await pyatv.scan(loop, hosts=[self.device_host], protocol=Protocol.RAOP,
                                   timeout=self.scan_timeout)
        if len(configs) != 1:
            raise TransportError("No unique RAOP receiver found at the configured address")
        service = configs[0].get_service(Protocol.RAOP)
        if service is not None:
            from pyatv.const import PairingRequirement
            properties = {key: str(value)[:160] for key, value in service.properties.items()
                          if key in ("cn", "et", "sr", "ss", "ch", "tp", "sf", "ft", "am", "vn", "vs")}
            self._event("raop_discovered", properties=properties, pairing=str(service.pairing))
            if service.pairing == PairingRequirement.Mandatory:
                raise TransportError("RAOP receiver requires pairing; pairing is not implemented in this bench")
        self._connection = await pyatv.connect(configs[0], loop, protocol=Protocol.RAOP)
        self._event("raop_connected")

    async def play(self, stream_url: str, config: Mapping[str, Any]) -> None:
        _url(stream_url)
        _codec(config)
        if self._stream_task is not None:
            await self.stop()
        self.last_error = None
        try:
            try:
                await asyncio.wait_for(self._connect(), timeout=self.scan_timeout + 10)
            except asyncio.TimeoutError as exc:
                raise TransportError("AirPlay discovery/control connection timed out before streaming") from exc
            self._event("stream_task_started", note="pyatv handles decoding and receiver buffering")
            self._stream_task = asyncio.create_task(self._connection.stream.stream_file(stream_url))
            self._stream_task.add_done_callback(self._completed)
            self.active = True
            # Surface synchronous setup errors without waiting for the whole stream.
            await asyncio.sleep(0)
            if self._stream_task.done():
                self._stream_task.result()
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception as exc:
            self.active = False
            self.last_error = str(exc) or type(exc).__name__
            self._event("play_failed", error=self.last_error, exception_type=type(exc).__name__)
            raise TransportError(f"AirPlay playback failed: {self.last_error}") from exc

    def pending_stage(self) -> list[str]:
        """Awaited function names only: never inspect coroutine locals or credentials."""
        current = self._stream_task.get_coro() if self._stream_task is not None else None
        stages = []
        for _ in range(16):
            code = getattr(current, "cr_code", None) or getattr(current, "gi_code", None)
            if code is None:
                break
            stages.append(code.co_qualname)
            current = getattr(current, "cr_await", None) or getattr(current, "gi_yieldfrom", None)
        return stages

    def check_failure(self) -> None:
        task = self._stream_task
        if task is not None and task.done():
            if task.cancelled():
                raise TransportError("AirPlay sender was unexpectedly cancelled")
            error = task.exception()
            if error is not None:
                message = str(error) or type(error).__name__
                self.last_error = message
                raise TransportError(f"AirPlay sender failed ({type(error).__name__}): {message}") from error

    async def wait_for_stream(self, connected: asyncio.Event, timeout: float) -> None:
        """Surface a background sender failure instead of hiding it behind timeout."""
        if self._stream_task is None:
            raise TransportError("AirPlay sender was not started")
        waiter = asyncio.create_task(connected.wait())
        try:
            await asyncio.wait({waiter, self._stream_task}, timeout=timeout,
                               return_when=asyncio.FIRST_COMPLETED)
            self.check_failure()
            if connected.is_set():
                return
            stages = self.pending_stage()
            self._event("source_not_requested", pending_stage=stages)
            if self._stream_task.done():
                raise TransportError("AirPlay sender ended before requesting test audio")
            raise TransportError(f"AirPlay sender did not request test audio within {timeout:g} seconds; receiver/session setup may be stalled")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    def _completed(self, task: asyncio.Task) -> None:
        self.active = False
        if task.cancelled():
            self._event("stream_task_cancelled")
            return
        error = task.exception()
        if error is not None:
            self.last_error = str(error) or type(error).__name__
            self._event("stream_failed", error=self.last_error, exception_type=type(error).__name__)
        else:
            self._event("stream_finished")

    async def stop(self) -> None:
        self._event("stop_requested")
        task = self._stream_task
        try:
            if self._connection is not None and task is not None and not task.done():
                # The control belongs to this active RAOP session, not a fresh
                # atvremote process that cannot see the original sender session.
                await asyncio.wait_for(self._connection.remote_control.stop(), self.stop_timeout)
                try:
                    await asyncio.wait_for(asyncio.shield(task), self.stop_timeout)
                except asyncio.TimeoutError:
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, self.stop_timeout)
                    except asyncio.CancelledError:
                        pass
            self.active = False
            self._event("session_stopped", note="Device output drain time unknown")
        except Exception as exc:
            self.last_error = str(exc)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise TransportError(f"AirPlay stop failed: {exc}") from exc
        finally:
            self._stream_task = None

    async def close(self) -> None:
        try:
            await self.stop()
        finally:
            if self._connection is not None:
                tasks = self._connection.close()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                self._connection = None
