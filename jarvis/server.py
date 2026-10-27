"""Ingress-only control UI; separately authenticated LAN audio endpoints."""
from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from datetime import datetime, timezone
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import struct
import time
from urllib.parse import urlparse
import uuid
import zipfile

from aiohttp import web, WSMsgType
import numpy as np

from . import __version__
from .audio import RATE, make_probe, detect_probe, detect_stop, detect_stability, summarize, wav_header
from .device import Device

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = {"wiim_host": "", "stream_base_url": "", "route": "http", "codec": "wav",
            "repetitions": 3, "verify_tls": True, "slimproto_player_id": "",
            "slimproto_port": 3483}


def validate_config(value):
    allowed = set(DEFAULTS) | {"device_token"}
    if set(value) - allowed:
        raise ValueError("Unknown configuration field")
    config = {**DEFAULTS, **value}
    if config["wiim_host"]:
        address = ipaddress.ip_address(config["wiim_host"])
        if not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError("WiiM address must be a private LAN IP address")
    if config["stream_base_url"]:
        parsed = urlparse(config["stream_base_url"])
        address = ipaddress.ip_address(parsed.hostname or "")
        if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("Stream address must be http://HA-LAN-IP:8099, without a path")
        if not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast or parsed.port != 8099:
            raise ValueError("Stream address must use the HA LAN IP and port 8099")
    if config["route"] not in ("http", "slimproto", "airplay") or config["codec"] not in ("wav", "flac"):
        raise ValueError("Unsupported route or codec")
    if type(config["repetitions"]) is not int or not 1 <= config["repetitions"] <= 20:
        raise ValueError("Choose 1 to 20 repetitions")
    if type(config["verify_tls"]) is not bool:
        raise ValueError("verify_tls must be a boolean")
    if type(config["slimproto_port"]) is not int or not 1024 <= config["slimproto_port"] <= 65535:
        raise ValueError("SlimProto port must be between 1024 and 65535")
    token = config.get("device_token", "")
    if len(token) < 32 or len(token) > 128 or not all(c.isalnum() or c in "-_" for c in token):
        raise ValueError("Device token must contain 32–128 URL-safe characters")
    return config


class Stream:
    def __init__(self, kind, repetitions):
        self.id = secrets.token_urlsafe(24)
        self.kind, self.repetitions = kind, repetitions
        self.connected = asyncio.Event()
        self.done = asyncio.Event()
        self.stop_requested = asyncio.Event()
        self.markers = []
        self.started_at = None
        self.bytes_sent = 0
        self.consumed = False
        self.schedule_lag_ms = 0
        self.error = None
        self.expected_disconnect = False
        self.duration = 0

    async def blocks(self):
        self.started_at = time.monotonic()
        self.connected.set()
        self.duration = 30 if self.kind == "stop" else 1 + self.repetitions * 2.0 + 5
        total = int(self.duration * RATE)
        pcm = np.zeros(total, dtype="<i2")
        for i in range(self.repetitions):
            probe = make_probe(i + 513)
            offset = int((.1 + i * 2) * RATE)
            pcm[offset:offset + len(probe)] = probe
            self.markers.append((self.started_at + offset / RATE, probe))
        if self.kind == "stability":
            t = np.arange(total) / RATE
            pilot = ((np.sin(2*np.pi*937*t) + np.sin(2*np.pi*1579*t)) * .025 * 32767).astype("<i2")
            pilot[:RATE] = 0
            pilot[-RATE:] = 0
            pcm += pilot
        if self.kind == "stop":
            t = np.arange(total - 2 * RATE) / RATE
            pcm[2 * RATE:] = ((np.sin(2 * np.pi * 937 * t) + np.sin(2 * np.pi * 1579 * t)) * .05 * 32767).astype("<i2")
            self.markers.clear()
        for start in range(0, total, 320):
            if self.stop_requested.is_set():
                break
            deadline = self.started_at + start / RATE
            await asyncio.sleep(max(0, deadline - time.monotonic()))
            self.schedule_lag_ms = max(self.schedule_lag_ms, (time.monotonic() - deadline) * 1000)
            chunk = pcm[start:start + 320].tobytes()
            self.bytes_sent += len(chunk)
            yield chunk


class Bench:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "settings.json"
        self.config = {**DEFAULTS, "device_token": secrets.token_urlsafe(32)}
        if self.path.exists():
            self.config = validate_config(json.loads(self.path.read_text()))
        self.save()
        self.device = Device()
        self.test = {"running": False, "phase": "idle"}
        self.results = []
        self.task = None
        self.stream = None
        self.transport = None
        self.slim = None
        self.slim_error = None

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.config, indent=2))
        with suppress(OSError):
            temporary.chmod(0o600)
        temporary.replace(self.path)

    async def init_slim(self):
        if self.slim:
            await self.slim.close()
            self.slim = None
        self.slim_error = None
        if not self.config["stream_base_url"]:
            return
        try:
            from .transports import SlimProtoTransport
            self.slim = SlimProtoTransport(server_host=urlparse(self.config["stream_base_url"]).hostname,
                                          control_port=self.config["slimproto_port"],
                                          player_id=self.config["slimproto_player_id"] or None)
            await self.slim.start()
        except Exception as exc:
            self.slim_error = str(exc)
            if self.slim:
                with suppress(Exception):
                    await self.slim.close()
            self.slim = None

    def status(self):
        return {"version": __version__, "milestone": "audio_bench", "device": self.device.snapshot(),
                "test": self.test, "config": {k:v for k,v in self.config.items() if k != "device_token"},
                "transports": {"http": {"available": True},
                    "slimproto": {"available": self.slim is not None,
                                  "clients": self.slim.players() if self.slim else [], "reason": self.slim_error},
                    "airplay": {"available": True, "reason": "Baseline: requires an unpaired RAOP receiver; no automatic pairing"}},
                "codecs": {"wav": True, "flac": shutil.which("ffmpeg") is not None},
                "cloud": {"enabled": False, "reason": "Wireless audio validation comes first; this build has no OpenAI client"}}

    async def cancel(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def run(self, kind, repetitions):
        from .transports import NativeHTTPTransport, AirPlayTransport
        result = {"id": self.test["id"], "kind": kind, "route": self.config["route"],
                  "codec": self.config["codec"], "started_at": datetime.now(timezone.utc).isoformat(),
                  "status": "running", "trials": [], "summary": summarize([]), "warnings": [
                      "Acoustic estimates include microphone processing delay; clock uncertainty is shown separately.",
                      "This is playback-path latency, not end-of-speech-to-assistant-response latency.",
                      "Test playback interrupts the current source. Automatic music restoration is not in this bench."]}
        self.results.insert(0, result)
        del self.results[30:]
        try:
            self.test["phase"] = "synchronizing microphone"
            await self.device.begin(result["id"], min(120000, (repetitions * 2 + 20) * 1000))
            await asyncio.sleep(.4)
            if self.config["route"] == "http":
                self.transport = NativeHTTPTransport(device_url="https://" + self.config["wiim_host"],
                                                     verify_ssl=self.config["verify_tls"])
            elif self.config["route"] == "airplay":
                self.transport = AirPlayTransport(device_host=self.config["wiim_host"])
            else:
                if not self.slim:
                    raise ValueError(self.slim_error or "SlimProto server is not ready")
                self.transport = self.slim
            self.stream = Stream(kind, repetitions)
            extension = self.config["codec"]
            url = self.config["stream_base_url"].rstrip("/") + "/stream/" + self.stream.id + "." + extension
            self.test["phase"] = "waiting for speaker stream"
            play_requested = time.monotonic()
            await self.transport.play(url, {"codec": extension, "sample_rate": RATE, "channels": 1, "sample_size": 16})
            await asyncio.wait_for(self.stream.connected.wait(), timeout=12)
            self.test["phase"] = "playing acoustic probes — remain quiet"
            if kind == "stop":
                await asyncio.sleep(5)
                self.test["phase"] = "stopping playback"
                if self.stream.done.is_set():
                    raise ValueError("Stream ended before stop; no valid stop measurement is possible")
                # A real stop is issued while the network stream remains open.
                result["stop_command_at"] = time.monotonic()
                self.stream.expected_disconnect = True
                await self.transport.stop()
                await asyncio.sleep(5)
            else:
                await asyncio.wait_for(self.stream.done.wait(), timeout=repetitions * 2 + 20)
                await asyncio.sleep(3)
            self.test["phase"] = "analysing microphone capture"
            await self.device.end()
            pcm, capture_start = self.device.recording()
            if kind == "stop":
                detection = await asyncio.to_thread(detect_stop, pcm, capture_start, result["stop_command_at"])
                result["trials"].append({"trial": 1, **detection,
                    "clock_uncertainty_ms": round(self.device.clock_rtt_ms / 2 + 32, 2)})
                result["metric"] = "stop_to_acoustic_silence"
            else:
                result["metric"] = "stream_marker_to_acoustic_onset"
            for i, (emitted, probe) in enumerate(self.stream.markers):
                # Each marker gets its own bounded search window, avoiding pairing
                # unrelated microphone bursts with a different output trial.
                search_start = max(0, int((emitted - capture_start - .1) * RATE))
                search_end = min(len(pcm), int((emitted - capture_start + 4.5) * RATE))
                detection = await asyncio.to_thread(detect_probe, pcm[search_start:search_end], probe)
                trial = {"trial": i + 1, **detection, "latency_ms": None,
                         "clock_uncertainty_ms": round(self.device.clock_rtt_ms / 2 + 4, 2)}
                if detection["status"] == "detected":
                    audible = capture_start + search_start / RATE + detection["offset_seconds"]
                    latency = (audible - emitted) * 1000
                    if latency < 0 or self.stream.schedule_lag_ms > 50:
                        trial.update(status="unavailable", reason="Clock or stream scheduling uncertainty prevents a reliable measurement")
                    else:
                        trial["latency_ms"] = round(latency, 1)
                        if i == 0:
                            trial["cold_startup_ms"] = round((audible - play_requested) * 1000, 1)
                        trial["detected_after_stop"] = kind == "stop" and audible > result.get("stop_command_at", float("inf"))
                result["trials"].append(trial)
            result["summary"] = summarize(result["trials"])
            if kind == "stability" and result["summary"]["median_ms"] is not None:
                result["stability"] = await asyncio.to_thread(detect_stability, pcm, capture_start,
                    self.stream.started_at, self.stream.duration, result["summary"]["median_ms"], self.stream.markers)
            result["stream"] = {"bytes_sent": self.stream.bytes_sent,
                                "max_schedule_lag_ms": round(self.stream.schedule_lag_ms, 2)}
            if self.stream.error:
                raise ValueError(self.stream.error)
            result["status"] = "measured" if result["summary"]["valid_trials"] else "unavailable"
        except asyncio.CancelledError:
            result["status"] = "cancelled"
            raise
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
        finally:
            if self.transport:
                try:
                    await asyncio.wait_for(self.transport.stop(), timeout=8)
                except Exception as exc:
                    result["warnings"].append("Player stop could not be confirmed: " + str(exc))
                if self.transport is not self.slim:
                    with suppress(Exception):
                        await self.transport.close()
                self.transport = None
            if self.stream:
                self.stream.stop_requested.set()
            with suppress(Exception):
                await self.device.end()
            self.device.frames.clear()
            result["summary"] = summarize(result["trials"])
            self.test = {"running": False, "phase": "idle"}
            self.stream = None


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


@web.middleware
async def ingress_only(request, handler):
    if request.remote not in ("127.0.0.1", "::1", "172.30.32.2"):
        raise web.HTTPForbidden(text="Open Jarvis through Home Assistant ingress")
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("Origin")
        # Normal same-origin browsers supply Origin; reject third-party origins.
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden(text="Cross-site control is not allowed")
        if origin and request.remote != "172.30.32.2" and urlparse(origin).netloc != request.host:
            raise web.HTTPForbidden(text="Cross-origin control is not allowed")
        if request.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise web.HTTPUnsupportedMediaType(text="JSON control requests are required")
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'self'"
    return response


def create_ui(bench):
    app = web.Application(middlewares=[errors, ingress_only], client_max_size=8192)
    async def index(request):
        return web.FileResponse(ROOT / "jarvis/static/index.html")
    async def status(request): return web.json_response(bench.status())
    async def config(request):
        if request.method == "PUT":
            if bench.test["running"]:
                raise web.HTTPConflict(text="Cancel the current test before changing settings")
            new = validate_config({**bench.config, **await request.json()})
            if new["device_token"] != bench.config["device_token"] and bench.device.ws:
                await bench.device.ws.close(code=1008, message=b"Pairing changed")
            bench.config = new
            bench.save()
            await bench.init_slim()
        return web.json_response(bench.config)
    async def start(request):
        data = await request.json()
        if bench.test["running"]:
            raise web.HTTPConflict(text="A test is already running")
        if data.get("comfortable_volume_confirmed") is not True:
            raise ValueError("Confirm a comfortable amplifier volume before starting")
        if not bench.device.snapshot()["connected"] or bench.device.info.get("muted"):
            raise ValueError("Pair and unmute Voice PE first")
        kind, repetitions = data.get("kind"), data.get("repetitions", 3)
        if kind not in ("latency", "stop", "stability") or type(repetitions) is not int or not 1 <= repetitions <= 20:
            raise ValueError("Invalid test type or repetition count")
        if not bench.config["wiim_host"] or not bench.config["stream_base_url"]:
            raise ValueError("Save the WiiM and speaker-reachable stream addresses first")
        if bench.config["codec"] == "flac" and not shutil.which("ffmpeg"):
            raise ValueError("FLAC requires ffmpeg; it is included in the HA App image")
        ident = uuid.uuid4().hex
        bench.test = {"running": True, "id": ident, "phase": "preparing"}
        bench.task = asyncio.create_task(bench.run(kind, repetitions))
        return web.json_response({"test_id": ident}, status=202)
    async def cancel(request):
        await bench.cancel()
        return web.json_response({"cancelled": True})
    async def results(request): return web.json_response({"results": bench.results})
    async def diagnostics(request):
        config = {k: v for k, v in bench.config.items() if k not in ("device_token", "wiim_host", "stream_base_url", "slimproto_player_id")}
        status = bench.status()
        status.pop("config")
        status["device"] = {k:v for k,v in status["device"].items() if k not in ("device_id", "ip", "mac")}
        status["transports"]["slimproto"]["clients"] = []
        body = {"version": __version__, "config": config, "status": status,
                "results": bench.results, "audio_retained": False, "cloud_calls": 0}
        # Exception strings can contain URLs, therefore redact all configured
        # secrets/addresses recursively from the export as a final boundary.
        content = json.dumps(body, indent=2)
        for key in ("device_token", "wiim_host", "stream_base_url", "slimproto_player_id"):
            if bench.config.get(key): content = content.replace(bench.config[key], "[redacted]")
        return web.Response(text=content, content_type="application/json", headers={"Content-Disposition": 'attachment; filename="jarvis-diagnostics.json"'})
    async def firmware(request):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in (ROOT / "firmware").rglob("*"):
                if file.is_file() and not any(p.startswith(".") for p in file.relative_to(ROOT / "firmware").parts) and (file.suffix in (".yaml", ".py", ".h", ".cpp", ".md") or file.name == "UPSTREAM-LICENSE.txt") and file.name != "secrets.yaml":
                    archive.write(file, str(file.relative_to(ROOT)))
            values = {"jarvis_pairing_token": bench.config["device_token"],
                      "jarvis_server_url": "ws://" + (urlparse(bench.config["stream_base_url"]).netloc or "YOUR_HA_IP:8099") + "/device",
                      "jarvis_api_key": base64.b64encode(secrets.token_bytes(32)).decode(),
                      "jarvis_ota_password": secrets.token_urlsafe(32)}
            archive.writestr("firmware/jarvis-secrets-to-merge.yaml", "# Merge into Builder secrets.yaml; preserve your Wi-Fi and other device secrets.\n# These local credentials are generated for this download; keep them private.\n" + "".join(key + ": " + json.dumps(value) + "\n" for key,value in values.items()))
        return web.Response(body=buffer.getvalue(), content_type="application/zip", headers={"Content-Disposition": 'attachment; filename="jarvis-firmware.zip"'})
    app.router.add_get("/", index)
    app.router.add_static("/static/", ROOT / "jarvis/static")
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/config", config)
    app.router.add_put("/api/config", config)
    app.router.add_post("/api/tests", start)
    app.router.add_delete("/api/tests/current", cancel)
    app.router.add_get("/api/results", results)
    app.router.add_get("/api/diagnostics", diagnostics)
    app.router.add_get("/api/firmware", firmware)
    return app


def create_audio(bench):
    app = web.Application(client_max_size=16384)
    async def device(request):
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=16384)
        await ws.prepare(request)
        accepted = False
        try:
            message = await asyncio.wait_for(ws.receive_json(), timeout=5)
            token = str(message.get("token", ""))
            if not secrets.compare_digest(token, bench.config["device_token"]) or message.get("type") != "hello" or message.get("protocol") != 1 or message.get("sample_rate") != RATE or message.get("channels") != 1:
                await ws.close(code=1008, message=b"Invalid pairing or format")
                return ws
            if bench.device.ws is not None and not bench.device.ws.closed:
                await ws.close(code=1008, message=b"A device is already paired")
                return ws
            bench.device.ws = ws
            accepted = True
            bench.device.info = {k:message[k] for k in ("device_id", "sample_rate", "muted", "firmware", "xmos_version") if k in message}
            await ws.send_json({"type": "hello_ok", "protocol": 1})
            async for frame in ws:
                if frame.type == WSMsgType.BINARY:
                    bench.device.receive_pcm(frame.data)
                elif frame.type == WSMsgType.TEXT:
                    bench.device.receive_json(json.loads(frame.data))
                    if bench.device.info.get("muted") and bench.test["running"]:
                        asyncio.create_task(bench.cancel())
        except (ValueError, asyncio.TimeoutError, KeyError, TypeError):
            await ws.close(code=1008, message=b"Invalid device protocol")
        finally:
            if accepted and bench.device.ws is ws:
                bench.device.ws = None
                await bench.cancel()
                bench.device.info = {}
        return ws
    async def stream(request):
        active = bench.stream
        if not active or request.match_info["name"] != active.id + "." + bench.config["codec"] or active.stop_requested.is_set():
            raise web.HTTPNotFound()
        content_type = "audio/wav" if bench.config["codec"] == "wav" else "audio/flac"
        if request.method == "HEAD":
            return web.Response(headers={"Content-Type": content_type, "Cache-Control": "no-store"})
        if active.consumed:
            raise web.HTTPGone(text="A measurement stream cannot be replayed")
        active.consumed = True
        response = web.StreamResponse(headers={"Content-Type": content_type, "Cache-Control": "no-store"})
        await response.prepare(request)
        process = None
        encoder_task = None
        try:
            if bench.config["codec"] == "wav":
                await response.write(wav_header())
                async for chunk in active.blocks(): await response.write(chunk)
            else:
                process = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0", "-c:a", "flac", "-compression_level", "0", "-flush_packets", "1", "-f", "flac", "pipe:1", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                async def encode():
                    async for chunk in active.blocks():
                        process.stdin.write(chunk)
                        await process.stdin.drain()
                    process.stdin.close()
                encoder_task = asyncio.create_task(encode())
                while chunk := await process.stdout.read(4096): await response.write(chunk)
                await encoder_task
                if await process.wait() != 0:
                    raise ValueError("FLAC encoder failed")
        except (ConnectionError, ValueError) as exc:
            if not active.stop_requested.is_set() and not active.expected_disconnect: active.error = str(exc)
        finally:
            if encoder_task and not encoder_task.done():
                encoder_task.cancel()
                with suppress(asyncio.CancelledError): await encoder_task
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            active.done.set()
        return response
    app.router.add_get("/device", device)
    app.router.add_get("/stream/{name}", stream)
    return app


async def serve():
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, shutdown.set)
    bench = Bench(os.environ.get("JARVIS_DATA_DIR", "/data"))
    ui_runner, audio_runner = web.AppRunner(create_ui(bench)), web.AppRunner(create_audio(bench))
    await ui_runner.setup()
    await audio_runner.setup()
    await web.TCPSite(ui_runner, os.environ.get("JARVIS_UI_HOST", "0.0.0.0"), 8098).start()
    await web.TCPSite(audio_runner, "0.0.0.0", 8099).start()
    await bench.init_slim()
    try:
        await shutdown.wait()
    finally:
        await bench.cancel()
        if bench.slim: await bench.slim.close()
        if bench.device.ws is not None: await bench.device.ws.close()
        await audio_runner.cleanup()
        await ui_runner.cleanup()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt): asyncio.run(serve())
