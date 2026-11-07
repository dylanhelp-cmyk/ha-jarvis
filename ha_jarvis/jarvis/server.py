"""Ingress-only control UI; separately authenticated LAN audio endpoints."""
from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from collections import deque
from datetime import datetime, timezone
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import re
import signal
import time
from urllib.parse import urlparse
import uuid
import zipfile

from aiohttp import web, WSMsgType
import numpy as np

from . import __version__
from .acknowledgment import (approval_path, cache_path, candidate_identity, candidate_wav,
                             generate_acknowledgment, read_candidate, verified_render)
from .audio import RATE, STABILITY_PILOT_GAP_SECONDS, make_probe, detect_probe, detect_stop, detect_stability, summarize
from .device import Device
from .cloud_settings import CloudSettings
from .playback import JackPlayback, WakeCacheUpload, stereo_pcm

ROOT = Path(__file__).resolve().parent.parent
CONNECTION_TEST_SECONDS = 60
DEFAULTS = {"ha_lan_url": "", "repetitions": 3}
LEGACY_FIELDS = {"wiim_host", "stream_base_url", "route", "codec", "verify_tls",
                 "slimproto_player_id", "slimproto_port", "airplay_latency_ms"}


def validate_config(value):
    if set(value) - set(DEFAULTS) - {"device_token"}:
        raise ValueError("Unknown configuration field")
    config = {**DEFAULTS, **value}
    if config["ha_lan_url"]:
        parsed = urlparse(config["ha_lan_url"])
        address = ipaddress.ip_address(parsed.hostname or "")
        if (parsed.scheme != "http" or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in ("", "/") or parsed.port != 8099
                or address.version != 4 or not address.is_private or address.is_loopback
                or address.is_unspecified or address.is_multicast):
            raise ValueError("Enter http://HA-LAN-IP:8099 without a path")
    if type(config["repetitions"]) is not int or not 1 <= config["repetitions"] <= 20:
        raise ValueError("Choose 1 to 20 repetitions")
    token = config.get("device_token", "")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("Device token must contain 32 to 128 URL-safe characters")
    return config


def migrate_config(value):
    value = dict(value)
    value.setdefault("ha_lan_url", value.get("stream_base_url", ""))
    return validate_config({key: entry for key, entry in value.items() if key not in LEGACY_FIELDS})


def bench_signal(kind, repetitions):
    if kind == "stop":
        t = np.arange(10 * RATE) / RATE
        return pilot(t), {}
    duration = (2 * repetitions + 5) if kind in ("stability", "echo") else 2 * repetitions + 1
    pcm = np.zeros(duration * RATE, dtype="<i2")
    probes = {}
    for i in range(repetitions):
        offset = (2 * i) * RATE
        probe = make_probe(513 + i)
        probes[offset] = probe
        pcm[offset:offset + len(probe)] = probe
    if kind in ("stability", "echo"):
        last = max(probes)
        start = last + len(probes[last]) + int(STABILITY_PILOT_GAP_SECONDS * RATE)
        # Leave a guard for resampling filter ringing before the analysis tail.
        end = len(pcm) - RATE - (RATE // 50 if kind == "echo" else 0)
        pcm[start:end] = pilot(np.arange(start, end) / RATE)
    return pcm, probes


def pilot(t):
    return ((np.sin(2 * np.pi * 937 * t) + np.sin(2 * np.pi * 1579 * t)) * .025 * 32767).astype("<i2")


class Bench:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cloud_settings = CloudSettings(self.data_dir)
        self.cloud_fixture = b""
        self.acknowledgment = {"phase": "idle"}
        self.ack_cache_attempted_on = None
        self.path = self.data_dir / "settings.json"
        self.firmware_secrets_path = self.data_dir / "firmware-secrets.json"
        self.config = {**DEFAULTS, "device_token": secrets.token_urlsafe(32)}
        if self.path.exists():
            self.config = migrate_config(json.loads(self.path.read_text()))
        self.save()
        if self.firmware_secrets_path.exists():
            self.firmware_secrets = json.loads(self.firmware_secrets_path.read_text())
        else:
            self.firmware_secrets = {
                "jarvis_api_key": base64.b64encode(secrets.token_bytes(32)).decode(),
                "jarvis_ota_password": secrets.token_urlsafe(32)}
            self.firmware_secrets_path.write_text(json.dumps(self.firmware_secrets))
            with suppress(OSError): self.firmware_secrets_path.chmod(0o600)
        self.device = Device()
        self.test = {"running": False, "phase": "idle"}
        self.results = []
        self.task = None
        self.firmware_observations = deque(maxlen=20)

    def observe_firmware(self):
        diagnostics = self.device.info.get("diagnostics")
        if diagnostics is None:
            return
        identity = tuple(diagnostics[key] for key in ("boot_id", "connection_generation", "fault_sequence"))
        duplicate = False
        if self.firmware_observations:
            previous = self.firmware_observations[-1]["diagnostics"]
            if (identity == tuple(previous[key] for key in ("boot_id", "connection_generation", "fault_sequence"))
                    and previous.get("pcm_send") == diagnostics.get("pcm_send")):
                duplicate = True
        observed = datetime.now(timezone.utc)
        observation = {"observed_at": observed.isoformat(), "diagnostics": diagnostics}
        if not duplicate:
            self.firmware_observations.append(observation)
        # Per-capture measurements can arrive with a clean stop or after reconnect.
        # Fault sequence alone does not identify a capture, nor does the current
        # connection generation on a reconnected hello.
        pcm = diagnostics.get("pcm_send")
        if pcm is not None:
            for result in self.results:
                session = result.get("device_session", {})
                if diagnostics["boot_id"] == session.get("boot_id") and all(
                    pcm.get(key) == session.get(key) for key in ("connection_generation", "capture_generation")):
                    result["firmware_pcm"] = pcm
                    break
        # A reconnect may describe a failed capture, but only an exact boot and
        # generation match establishes that the root event belongs to that test.
        for result in self.results:
            if result.get("interruption", {}).get("reason") != "device_disconnected":
                continue
            if result.get("firmware_followup", {}).get("linked_root_fault") is not None:
                continue
            ended = datetime.fromisoformat(result["started_at"]).timestamp() + result.get("elapsed_seconds", 0)
            if not 0 <= observed.timestamp() - ended <= 120:
                continue
            session = result.get("device_session", {})
            same_boot = diagnostics["boot_id"] == session.get("boot_id")
            root = diagnostics.get("root_fault")
            linked = root if same_boot and root and all(root.get(key) == session.get(key)
                for key in ("connection_generation", "capture_generation")) else None
            result["firmware_followup"] = {**observation, "same_boot": same_boot if "boot_id" in session else None,
                                          "linked_root_fault": linked}
            break

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.config, indent=2))
        with suppress(OSError):
            temporary.chmod(0o600)
        temporary.replace(self.path)

    def acknowledgment_status(self):
        pcm = read_candidate(self.data_dir) if cache_path(self.data_dir).is_file() else None
        ident = candidate_identity(pcm) if pcm else None
        approval = approval_path(self.data_dir)
        verified = bool(ident and approval.is_file() and approval.read_text(encoding="ascii").strip() == ident)
        return {**self.acknowledgment, "cached_on_host": pcm is not None, "candidate_id": ident,
                "verified": verified, "cached_on_device": bool(verified and self.device.info.get("ack_cache_id") == ident)}

    def maybe_preload_wake_cache(self):
        device = self.device
        if (self.test["running"] or device.ws is None or self.ack_cache_attempted_on is device.ws
                or device.info.get("ack_cache_protocol") != 1 or not approval_path(self.data_dir).is_file()):
            return
        self.ack_cache_attempted_on = device.ws
        self.test = {"running": True, "phase": "preloading the verified wake clip"}
        self.task = asyncio.create_task(self.preload_wake_cache())
        self.task.add_done_callback(self.finish_auxiliary_test)

    def finish_auxiliary_test(self, task):
        if task.cancelled():
            self.acknowledgment = {"phase": "cancelled"}
        self.test = {"running": False, "phase": "idle"}

    async def preload_wake_cache(self):
        upload = WakeCacheUpload(self.device)
        self.device.wake_cache_upload = upload
        try:
            cached = await asyncio.to_thread(verified_render, self.data_dir)
            if cached is None:
                return
            ident, pcm = cached
            if self.device.info.get("ack_cache_id") != ident:
                await upload.send(ident, pcm)
                self.device.info["ack_cache_id"] = ident
            self.acknowledgment = {"phase": "preloaded"}
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            self.acknowledgment = {"phase": "preload_failed", "error_type": type(exc).__name__}
        finally:
            self.device.wake_cache_upload = None

    def status(self):
        return {"version": __version__, "milestone": "wired_audio_bench", "device": self.device.snapshot(),
                "test": self.test, "acknowledgment": self.acknowledgment_status(), "config": {k: v for k, v in self.config.items() if k != "device_token"},
                "cloud": {**self.cloud_settings.public(), "fixture_loaded": bool(self.cloud_fixture),
                          "reason": "Cloud playback trials are gated on wired bench verification"}}

    async def cancel(self, reason="user_stop", details=None):
        if self.task and not self.task.done():
            if not self.test.get("cancelling") and not self.test.get("finishing"):
                self.test["interruption"] = {"reason": reason, "phase": self.test.get("phase"), **(details or {})}
                self.test["cancelling"] = True
                self.test["phase"] = "stopping playback and microphone capture"
                self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def prepare_acknowledgment(self):
        try:
            metadata = await generate_acknowledgment(self.cloud_settings, self.data_dir)
            self.acknowledgment = {"phase": "generated", **metadata}
        except asyncio.CancelledError:
            self.acknowledgment = {"phase": "cancelled"}
            raise
        except Exception as exc:
            self.acknowledgment = {"phase": "failed", "error_type": type(exc).__name__}

    async def measure_wake_ack(self, result):
        from .live_bench import match_response, render_reference, voiced_bounds

        device = self.device
        cached = await asyncio.to_thread(verified_render, self.data_dir)
        if cached is None or device.info.get("ack_cache_id") != cached[0]:
            raise ValueError("Verify and preload the wake acknowledgment first")
        ident, pcm = cached
        device.wake_event.clear()
        device.wake_details = {}
        device.wake_playback_terminal = {}
        self.test["phase"] = "armed for Hey Jarvis"
        try:
            await asyncio.wait_for(device.wake_event.wait(), 30)
        except TimeoutError:
            result.update(status="unavailable", reason="No Hey Jarvis wake detected during the 30-second window")
            return
        self.test["phase"] = "capturing the acknowledgment"
        wake = dict(device.wake_details)
        ws = device.ws
        try:
            await asyncio.sleep(1.35)
        except asyncio.CancelledError:
            if (ws is device.ws and ws is not None and not ws.closed and wake.get("ack_playback_id")
                    and not device.wake_playback_terminal):
                with suppress(OSError, RuntimeError, TimeoutError):
                    await asyncio.wait_for(ws.send_json({"type": "play_stop",
                        "playback_id": wake["ack_playback_id"], "generation": wake["ack_generation"]}), 1)
            raise
        await device.end()
        captured, capture_start = device.recording()
        terminal = dict(device.wake_playback_terminal)
        result["wake_playback"] = terminal
        result["metric"] = "firmware_wake_detection_to_audible_acknowledgment"
        stamp = wake.get("device_time_ms")
        if (wake.get("ack_cache_id") != ident or terminal.get("drained") is not True
                or terminal.get("rendered_samples") != len(pcm) // 4
                or type(stamp) is not int or not 0 <= stamp <= 0xFFFFFFFF):
            result.update(status="unavailable", reason="Voice PE did not confirm a complete playback for this wake")
            return
        detected_at = (stamp + device.clock_offset_ms) / 1000
        if not capture_start <= detected_at <= capture_start + len(captured) / RATE:
            result.update(status="unavailable", reason="Wake timing is outside the captured interval")
            return
        reference = await asyncio.to_thread(render_reference, pcm)
        bounds = voiced_bounds(reference)
        if bounds is None:
            result.update(status="unavailable", reason="The wake clip reference contains no usable speech")
            return
        index = max(0, round((detected_at - .04 - capture_start) * RATE))
        match = await asyncio.to_thread(
            match_response, captured[index:], capture_start + index / RATE,
            reference, bounds[0], detected_at, detected_at, window_seconds=.14)
        match.pop("fixture_end_to_audible_ms", None)
        result["trials"] = [{"trial": 1, **match}]
        result["status"] = "measured" if match["status"] == "detected" else "unavailable"
        result["clock_uncertainty_ms"] = round(device.clock_rtt_ms / 2 + 4, 1)

    async def run(self, kind, repetitions):
        started = time.monotonic()
        result = {"id": self.test["id"], "kind": kind, "route": "voice_pe_jack",
                  "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
                  "trials": [], "warnings": [
                      "Speaker API acceptance is a software milestone. Acoustic estimates include microphone processing delay."]}
        self.results.insert(0, result)
        del self.results[30:]
        player = sender = None
        try:
            self.test["phase"] = "synchronizing Voice PE and starting capture"
            await self.device.begin(result["id"], 100000 if kind == "connection" else 65000)
            result["device_session"] = dict(self.device.capture_session)
            self.test["phase"] = "capturing a quiet baseline"
            if kind == "connection":
                await asyncio.sleep(CONNECTION_TEST_SECONDS)
                await self.device.end()
                self.device.recording()
                result.update(status="passed", metric="microphone_connection")
                return
            if kind == "wake_ack":
                await self.measure_wake_ack(result)
                return
            signal_pcm, probes = bench_signal(kind, repetitions)
            pcm = await asyncio.to_thread(stereo_pcm, signal_pcm)
            await asyncio.sleep(.5)
            player = JackPlayback(self.device, result["id"])
            self.device.playback = player
            self.test["phase"] = "playing through the Voice PE jack"
            requested_at = time.monotonic()
            sender = asyncio.create_task(player.send(pcm))
            if kind == "stop":
                done, _ = await asyncio.wait({sender}, timeout=2)
                if done:
                    sender.result()
                    raise RuntimeError("Playback ended before the stop check")
                sender.cancel()
                with suppress(asyncio.CancelledError):
                    await sender
                stop_at = time.monotonic()
                await player.stop()
            else:
                await sender
            self.test["phase"] = "capturing the playback tail"
            await asyncio.sleep(.9)
            await self.device.end()
            captured, capture_start = self.device.recording()
            self.test["phase"] = "analyzing acoustic evidence"
            offset_ms = self.device.clock_offset_ms
            if kind == "stop":
                detection = await asyncio.to_thread(detect_stop, captured, capture_start, stop_at)
                result.update(metric="stop_to_acoustic_silence", trials=[{"trial": 1, **detection}])
            else:
                result["metric"] = "speaker_acceptance_to_acoustic_onset"
                for index, (offset, probe) in enumerate(probes.items()):
                    accepted = player.accepted_at(offset * 3, offset_ms)
                    trial = {"trial": index + 1, "status": "unavailable", "latency_ms": None}
                    if accepted is not None:
                        lo = max(0, int((accepted - capture_start - .15) * RATE))
                        hi = max(lo, int((accepted - capture_start + .75) * RATE) + len(probe))
                        match = await asyncio.to_thread(detect_probe, captured[lo:hi], probe)
                        trial.update(match)
                        if match["status"] == "detected":
                            audible = capture_start + lo / RATE + match["offset_seconds"]
                            latency = (audible - accepted) * 1000
                            if latency >= 0:
                                trial["latency_ms"] = round(latency, 1)
                                if index == 0:
                                    trial["cold_startup_ms"] = round((audible - requested_at) * 1000, 1)
                            else:
                                trial.update(status="unavailable", reason="Acoustic onset predates the estimated device timestamp")
                    else:
                        trial["reason"] = "Speaker did not confirm the probe frames"
                    result["trials"].append(trial)
            summary = summarize(result["trials"])
            if kind in ("stability", "echo"):
                accepted_start = player.accepted_at(0, offset_ms)
                if summary["median_ms"] is not None and accepted_start is not None:
                    markers = [(accepted_start + offset / RATE, probe) for offset, probe in probes.items()]
                    result["stability"] = await asyncio.to_thread(
                        detect_stability, captured, capture_start, accepted_start,
                        len(signal_pcm) / RATE, summary["median_ms"], markers)
                    if (kind == "echo" and result["stability"]["status"] == "measured"
                            and player.frames_rendered == player.frames_sent):
                        from .echo_bench import measure_echo_reduction
                        from .live_bench import render_reference

                        reference = await asyncio.to_thread(render_reference, pcm)
                        result["echo"] = await asyncio.to_thread(
                            measure_echo_reduction, captured, capture_start, reference,
                            accepted_start, summary["median_ms"])
                if kind == "echo" and "echo" not in result:
                    result["echo"] = {"status": "unavailable",
                                      "reason": "Echo analysis needs a measured pilot and confirmed output of every reference frame"}
                outcome = result.get("echo" if kind == "echo" else "stability", {})
                result["status"] = "measured" if outcome.get("status") == "measured" else "unavailable"
            else:
                result["status"] = "measured" if summary["valid_trials"] else "unavailable"
            result["clock_uncertainty_ms"] = round(self.device.clock_rtt_ms / 2 + 4, 1)
        except asyncio.CancelledError:
            interruption = self.test.get("interruption", {"reason": "unexpected_task_cancellation"})
            result.update(interruption=interruption,
                          status="cancelled" if interruption["reason"] == "user_stop" else "failed")
            raise
        except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
            result.update(status="failed", error=str(exc) or type(exc).__name__)
        finally:
            self.test["finishing"] = True
            try:
                if sender is not None:
                    if not sender.done():
                        sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                if player is not None:
                    try:
                        await player.stop()
                    except (RuntimeError, OSError, TimeoutError):
                        result["warnings"].append("Voice PE did not confirm the playback stop")
                    result["playback"] = player.snapshot()
                with suppress(RuntimeError, OSError, TimeoutError):
                    await self.device.end()
                result["capture"] = self.device.capture_diagnostics()
                result["device_session"] = dict(self.device.capture_session)
                self.observe_firmware()
                result["summary"] = summarize(result["trials"])
                result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            finally:
                self.device.playback = None
                self.device.frames.clear()
                self.test = {"running": False, "phase": "idle"}


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
    app = web.Application(middlewares=[errors, ingress_only], client_max_size=262144)
    async def index(request):
        return web.FileResponse(ROOT / "jarvis/static/index.html")
    async def status(request): return web.json_response(bench.status())
    async def cloud_settings(request):
        if request.method == "PUT":
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"api_key"}:
                raise ValueError("Only local API key entry is supported")
            bench.cloud_settings.set_key(data["api_key"])
        return web.json_response({**bench.cloud_settings.public(), "fixture_loaded": bool(bench.cloud_fixture),
                                  "benchmark_available": False,
                                  "reason": "Cloud playback trials are gated on wired bench verification"})
    async def cloud_acknowledgment(request):
        if request.method == "POST":
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"confirm_paid"} or data["confirm_paid"] is not True:
                raise ValueError("Explicitly confirm the paid greeting generation")
            if bench.test["running"] or (bench.task and not bench.task.done()):
                raise web.HTTPConflict(text="Wait for the current test to finish")
            if approval_path(bench.data_dir).is_file():
                raise web.HTTPConflict(text="The current voice already has a verified greeting")
            public = bench.cloud_settings.public()
            if not public["api_key_configured"] or public["paid_retry_blocked"]:
                raise ValueError("A configured key and a settled budget ledger are required")
            bench.acknowledgment = {"phase": "generating"}
            bench.test = {"running": True, "phase": "generating greeting with digital silence"}
            bench.task = asyncio.create_task(bench.prepare_acknowledgment())
            bench.task.add_done_callback(bench.finish_auxiliary_test)
        return web.json_response(bench.acknowledgment_status(), status=202 if request.method == "POST" else 200)

    async def preview_acknowledgment(request):
        if not cache_path(bench.data_dir).is_file():
            raise web.HTTPNotFound(text="No generated greeting is available")
        pcm = read_candidate(bench.data_dir)
        if request.query.get("clip") != candidate_identity(pcm):
            raise web.HTTPConflict(text="The greeting changed. Reload the preview.")
        return web.Response(body=candidate_wav(pcm), content_type="audio/wav")

    async def verify_acknowledgment(request):
        data = await request.json()
        if bench.test["running"]:
            raise web.HTTPConflict(text="Wait for the current test before verifying its greeting")
        if not cache_path(bench.data_dir).is_file():
            raise web.HTTPNotFound(text="No generated greeting is available")
        pcm = read_candidate(bench.data_dir)
        ident = candidate_identity(pcm)
        if (not isinstance(data, dict) or data.get("clip") != ident
                or data.get("heard_exact_phrase") is not True):
            raise ValueError("Listen to the current clip and confirm that it says only Yes sir")
        if len(pcm) > RATE * 2:
            raise ValueError("The wake clip must be at most one second")
        path = approval_path(bench.data_dir)
        temporary = path.with_suffix(".verified.tmp")
        temporary.write_text(ident + "\n", encoding="ascii")
        temporary.replace(path)
        bench.ack_cache_attempted_on = None
        bench.acknowledgment = {"phase": "verified"}
        bench.maybe_preload_wake_cache()
        return web.json_response(bench.acknowledgment_status())

    async def cloud_fixture(request):
        if bench.test["running"]:
            raise web.HTTPConflict(text="Wait for the current test before replacing its fixture")
        data = await request.json()
        try:
            pcm = base64.b64decode(data["pcm_b64"], validate=True)
        except (ValueError, KeyError, TypeError):
            raise ValueError("Supply a base64 PCM16LE mono 16 kHz test fixture") from None
        if len(pcm) % 2 or not RATE // 2 <= len(pcm) // 2 <= RATE * 5:
            raise ValueError("The synthetic fixture must contain 0.5 to 5 seconds of audio")
        bench.cloud_fixture = pcm
        return web.json_response({"fixture_loaded": True, "duration_seconds": len(pcm) / (RATE * 2), "retained_on_disk": False})
    async def audio_capabilities(request):
        try:
            from .aec import HostAEC3
            usable = len(HostAEC3().process(bytes(320), bytes(320))) == 320
            return web.json_response({"native_aec3_usable": usable, "frame_ms": 10, "sample_rate": RATE,
                                      "acoustically_verified": False, "clock_alignment_integrated": False})
        except Exception as exc:
            return web.json_response({"native_aec3_usable": False, "reason": type(exc).__name__})
    async def cloud_start(request):
        raise web.HTTPServiceUnavailable(text="Paid trials will resume after wired playback is verified")

    async def config(request):
        if request.method == "PUT":
            if bench.test["running"]:
                raise web.HTTPConflict(text="Cancel the current test before changing settings")
            new = validate_config({**bench.config, **await request.json()})
            if new["device_token"] != bench.config["device_token"] and bench.device.ws:
                await bench.device.ws.close(code=1008, message=b"Pairing changed")
            bench.config = new
            bench.save()
        return web.json_response(bench.config)
    async def start(request):
        data = await request.json()
        if bench.test["running"]:
            raise web.HTTPConflict(text="A test is already running")
        kind, repetitions = data.get("kind"), data.get("repetitions", bench.config["repetitions"])
        if kind not in ("latency", "stop", "stability", "echo", "connection", "wake_ack"):
            raise ValueError("Unsupported local test")
        if type(repetitions) is not int or not 1 <= repetitions <= 20:
            raise ValueError("Choose 1 to 20 repetitions")
        device = bench.device.snapshot()
        if not device["connected"] or device.get("muted") or device.get("raw_ready") is not True:
            raise ValueError("Pair and unmute the Voice PE with verified raw microphone capture")
        if kind != "connection":
            if data.get("comfortable_volume_confirmed") is not True:
                raise ValueError("Confirm comfortable speaker volume before starting")
            if device.get("playback_protocol") != 1 or device.get("jack_plugged") is not True:
                raise ValueError("Install compatible playback firmware and connect the 3.5 mm jack")
        if kind == "wake_ack" and not bench.acknowledgment_status()["cached_on_device"]:
            raise ValueError("Verify and preload the wake acknowledgment before testing it")
        ident = uuid.uuid4().hex
        bench.test = {"running": True, "id": ident, "phase": "preparing"}
        bench.task = asyncio.create_task(bench.run(kind, repetitions))
        return web.json_response({"test_id": ident}, status=202)

    async def cancel(request):
        asyncio.create_task(bench.cancel())
        return web.json_response({"stop_requested": True}, status=202)
    async def results(request): return web.json_response({"results": bench.results})
    async def diagnostics(request):
        config = {k: v for k, v in bench.config.items() if k not in ("device_token", "ha_lan_url")}
        status = bench.status()
        status.pop("config")
        status["device"] = {k:v for k,v in status["device"].items() if k not in ("device_id", "ip", "mac")}
        body = {"version": __version__, "config": config, "status": status,
                "results": bench.results, "firmware_observations": list(bench.firmware_observations),
                "audio_retained": False, "cloud_calls": bench.cloud_settings.attempt_count,
                "cloud_call_count_scope": "session creation attempts; audio messages and close events are not counted"}
        def redact_identifiers(value):
            if isinstance(value, dict):
                return {key: "[redacted]" if key in ("player_id", "advertised_host", "device_id") else redact_identifiers(item)
                        for key, item in value.items()}
            if isinstance(value, list):
                return [redact_identifiers(item) for item in value]
            return value
        # Exception strings can contain URLs, therefore redact all configured
        # secrets/addresses recursively from the export as a final boundary.
        content = json.dumps(redact_identifiers(body), indent=2)
        for key in ("device_token", "ha_lan_url"):
            if bench.config.get(key): content = content.replace(bench.config[key], "[redacted]")
        return web.Response(text=content, content_type="application/json", headers={"Content-Disposition": 'attachment; filename="jarvis-diagnostics.json"'})
    async def firmware(request):
        if not bench.config["ha_lan_url"]:
            raise ValueError("Save the HA LAN URL before downloading ESPHome setup")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            # A one-time local entry fetches versioned source from GitHub on
            # subsequent Builder builds; no repeated source-directory copying.
            archive.write(ROOT / "firmware/voice-pe-repository.yaml", "voice-pe-repository.yaml")
            archive.writestr("SETUP.txt", "Copy voice-pe-repository.yaml into ESPHome Builder. Merge jarvis-secrets-to-merge.yaml into existing secrets.yaml without replacing other values. Keep Wi-Fi secrets local. Validate and install when you choose. Later builds fetch the public GitHub firmware package/components (5-minute cache); no new bundle is required for routine updates. App updates are separate through Home Assistant Apps.\n")
            values = {"jarvis_pairing_token": bench.config["device_token"],
                      "jarvis_server_url": "ws://" + (urlparse(bench.config["ha_lan_url"]).netloc or "YOUR_HA_IP:8099") + "/device",
                      **bench.firmware_secrets}
            archive.writestr("jarvis-secrets-to-merge.yaml", "# Merge into Builder secrets.yaml; preserve your Wi-Fi and other device secrets.\n# These local credentials persist across App updates and repeated downloads.\n" + "".join(key + ": " + json.dumps(value) + "\n" for key,value in values.items()))
        return web.Response(body=buffer.getvalue(), content_type="application/zip", headers={"Content-Disposition": 'attachment; filename="jarvis-esphome-setup.zip"'})
    app.router.add_get("/", index)
    app.router.add_static("/static/", ROOT / "jarvis/static")
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/cloud", cloud_settings)
    app.router.add_put("/api/cloud", cloud_settings)
    app.router.add_put("/api/cloud/fixture", cloud_fixture)
    app.router.add_get("/api/cloud/acknowledgment", cloud_acknowledgment)
    app.router.add_post("/api/cloud/acknowledgment", cloud_acknowledgment)
    app.router.add_get("/api/acknowledgment/preview", preview_acknowledgment)
    app.router.add_post("/api/acknowledgment/verify", verify_acknowledgment)
    app.router.add_get("/api/audio/capabilities", audio_capabilities)
    app.router.add_post("/api/cloud/tests", cloud_start)
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
        disconnect_details = {}
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
            bench.device.info = {k: message[k] for k in ("device_id", "sample_rate", "muted", "raw_ready",
                                "firmware", "xmos_version", "playback_protocol", "jack_plugged",
                                "ack_cache_protocol", "ack_cache_id") if k in message}
            bench.device.receive_json(message)
            bench.observe_firmware()
            await ws.send_json({"type": "hello_ok", "protocol": 1})
            bench.maybe_preload_wake_cache()
            async for frame in ws:
                if frame.type == WSMsgType.BINARY:
                    bench.device.receive_pcm(frame.data)
                elif frame.type == WSMsgType.TEXT:
                    message = json.loads(frame.data)
                    bench.device.receive_json(message)
                    if message.get("type") == "status":
                        bench.maybe_preload_wake_cache()
                    bench.observe_firmware()
                    if bench.device.info.get("muted") and bench.test.get("id") and bench.test["running"]:
                        asyncio.create_task(bench.cancel("device_muted"))
                    elif bench.device.playback and bench.device.info.get("jack_plugged") is False:
                        asyncio.create_task(bench.cancel("jack_unplugged"))
        except (ValueError, asyncio.TimeoutError, KeyError, TypeError) as exc:
            disconnect_details = {"protocol_error": str(exc), "exception_type": type(exc).__name__, "server_close_code": 1008}
            await ws.close(code=1008, message=b"Invalid device protocol")
        finally:
            if accepted and bench.device.ws is ws:
                disconnect_details["close_code"] = ws.close_code
                if ws.exception() is not None:
                    disconnect_details["socket_error"] = str(ws.exception())
                bench.device.ws = None
                bench.device.info = {}
                await bench.cancel("device_disconnected", disconnect_details)
        return ws
    app.router.add_get("/device", device)
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
    try:
        await shutdown.wait()
    finally:
        await bench.cancel("app_shutdown")
        if bench.device.ws is not None: await bench.device.ws.close()
        await audio_runner.cleanup()
        await ui_runner.cleanup()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt): asyncio.run(serve())
