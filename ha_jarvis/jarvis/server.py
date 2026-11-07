"""Ingress-only control UI; separately authenticated LAN audio endpoints."""
from __future__ import annotations

import asyncio
import base64
from contextlib import AsyncExitStack, suppress
from collections import deque
from dataclasses import replace
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
from .assist import AssistClient, AssistDiscoveryError, discover_tools
from .assist_delegation import AssistDelegation
from .audio import RATE, STABILITY_PILOT_GAP_SECONDS, make_probe, detect_probe, detect_stop, detect_stability, summarize
from .device import Device
from .opening_audio import OpeningAudio
from .cloud_settings import CloudSettings
from .playback import JackPlayback, WakeCacheUpload, stereo_pcm

ROOT = Path(__file__).resolve().parent.parent
CONNECTION_TEST_SECONDS = 60
STANDBY_ARM_MS = 120_000
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
        self.opening_audio = None
        self.echo_alignment = None
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
        ws = self.device.ws
        try:
            cached = await asyncio.to_thread(verified_render, self.data_dir)
            if cached is None:
                return
            ident, pcm = cached
            if self.device.info.get("ack_cache_id") != ident:
                for attempt in range(2):
                    if self.device.ws is not ws or ws is None or ws.closed:
                        raise RuntimeError("Voice PE disconnected during wake cache transfer")
                    upload = WakeCacheUpload(self.device)
                    self.device.wake_cache_upload = upload
                    try:
                        await upload.send(ident, pcm)
                    except TimeoutError:
                        if attempt:
                            raise
                    else:
                        break
                if self.device.ws is not ws or ws.closed:
                    raise RuntimeError("Voice PE disconnected during wake cache transfer")
                self.device.info["ack_cache_id"] = ident
            self.acknowledgment = {"phase": "preloaded"}
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            self.acknowledgment = {"phase": "preload_failed", "error_type": type(exc).__name__}
        finally:
            self.device.wake_cache_upload = None

    def status(self):
        return {"version": __version__, "milestone": "wired_audio_bench", "device": self.device.snapshot(),
                "test": self.test, "echo_alignment_ready": self.alignment_delay() is not None,
                "acknowledgment": self.acknowledgment_status(), "config": {k: v for k, v in self.config.items() if k != "device_token"},
                "cloud": {**self.cloud_settings.public(), "fixture_loaded": bool(self.cloud_fixture),
                          "reason": "The voice preview requires separate per-run room microphone consent"}}

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

    def alignment_delay(self):
        if self.echo_alignment is None:
            return None
        boot, ident, delay = self.echo_alignment
        if (self.device.info.get("jack_plugged") is True
                and self.device.info.get("diagnostics", {}).get("boot_id") == boot
                and self.device.info.get("ack_cache_id") == ident):
            return delay
        return None

    async def wait_for_wake(self, result, *, timeout=30):
        device = self.device
        self.test["phase"] = "armed for Hey Jarvis"
        waiting = [asyncio.create_task(device.wake_event.wait())]
        if device.awaiting_wake:
            waiting.append(asyncio.create_task(device.stopped.wait()))
        try:
            await asyncio.wait(waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiting:
                task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)
        if not device.wake_event.is_set():
            result.update(status="unavailable", reason=device.arm_stop_reason or
                          f"No Hey Jarvis wake detected during the {timeout}-second window")
            return False
        return True

    async def measure_wake_ack(self, result, startup_transport):
        device = self.device
        cached = await asyncio.to_thread(verified_render, self.data_dir)
        if cached is None or device.info.get("ack_cache_id") != cached[0]:
            raise ValueError("Verify and preload the wake acknowledgment first")
        ident, pcm = cached
        if not await self.wait_for_wake(result):
            return
        self.test["phase"] = "capturing the acknowledgment"
        wake = dict(device.wake_details)
        startup = None
        opening_ready = asyncio.Event()
        opening = self.opening_audio

        def handoff(at):
            if opening is not None and self.device.pcm_consumer is not None:
                self.device.pcm_consumer = None
                _, result["opening_audio"] = opening.release(at)
            opening_ready.set()

        stamp = wake.get("device_time_ms")
        if startup_transport and wake.get("ack_cache_id") == ident and type(stamp) is int and 0 <= stamp <= 0xFFFFFFFF:
            detected_at = (stamp + device.clock_offset_ms) / 1000
            startup = asyncio.create_task(self.measure_cloud_startup(startup_transport, detected_at, handoff))
        try:
            await self.measure_wake_audio(result, wake, ident, pcm, opening_ready if startup else None, handoff)
            if startup is not None:
                self.test["phase"] = "finalizing the GPT-Live startup test"
                result["cloud_readiness"] = await startup
                trials = result["trials"]
                audible = trials[0].get("latency_ms") if trials else None
                ready = result["cloud_readiness"].get("wake_to_ready_ms")
                if audible is not None and ready is not None:
                    result["cloud_readiness"]["ready_minus_audible_ack_ms"] = round(ready - audible, 1)
        finally:
            if startup is not None and not startup.done():
                startup.cancel()
                await asyncio.gather(startup, return_exceptions=True)

    async def measure_cloud_startup(self, transport, detected_at, handoff):
        from .live_session import LiveSession

        async def discard_audio(pcm, rate):
            pass

        session = LiveSession(self.cloud_settings, on_audio=discard_audio, transport=transport, max_seconds=30)
        ready = None
        try:
            await session.start()
            now = time.monotonic()
            ready = round((now - detected_at) * 1000, 1)
            handoff(now)
        except (OSError, RuntimeError, ValueError):
            opening = self.opening_audio
            if opening is not None:
                opening.error = "cloud_startup_failed"
            handoff(time.monotonic())
        finally:
            metrics = await session.close()
        return {"transport": transport, "wake_to_ready_ms": ready,
                "provider_startup_ms": round(metrics.ready_ms, 1) if metrics.ready_ms is not None else None,
                "finalization_confirmed": metrics.finalization_confirmed,
                "scope": "startup only; no room microphone samples connected to GPT-Live"}

    async def measure_wake_audio(self, result, wake, ident, pcm, opening_ready, handoff):
        from .live_bench import match_response, render_reference, voiced_bounds

        device = self.device
        ws = device.ws
        try:
            await asyncio.sleep(1.35)
            if opening_ready is not None and self.opening_audio is not None:
                try:
                    await asyncio.wait_for(opening_ready.wait(), 1.6)
                except TimeoutError:
                    self.opening_audio.error = "cloud_startup_timeout"
            if opening_ready is None or not opening_ready.is_set():
                handoff(time.monotonic())
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
        if result.get("wake_capture_mode") == "device_armed":
            result["wake_to_first_capture_sample_ms"] = round((capture_start - detected_at) * 1000, 1)
        if not capture_start - .08 <= detected_at <= capture_start + len(captured) / RATE:
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
        if result.get("wake_capture_mode") == "device_armed":
            from .streaming_aec import check_opening_reference, compare_opening_prefix

            frames = tuple(device.frames)
            result["opening_echo"] = await asyncio.to_thread(
                check_opening_reference, frames, pcm, device.clock_offset_ms,
                terminal.get("first_rendered_ms"), terminal.get("last_rendered_ms"),
                match.get("audible_onset_at"), bounds[0])
            progress = device.wake_render_progress
            if (progress and progress[1] == terminal.get("first_rendered_ms")
                    and result["opening_echo"]["status"] == "processed"):
                count, first, received_at = progress
                comparison = await asyncio.to_thread(
                    compare_opening_prefix, frames, pcm, device.clock_offset_ms, first,
                    terminal["last_rendered_ms"], count, result["opening_echo"]["estimated_delay_ms"])
                comparison["first_report_to_terminal_ms"] = round(
                    (device.wake_render_terminal_at - received_at) * 1000, 1)
                result["opening_prefix_echo"] = comparison
            else:
                result["opening_prefix_echo"] = {"status": "unavailable",
                    "reason": "No usable partial render report matched the completed acknowledgment"}

    async def measure_voice_turn(self, result, gain_db, follow_up, preconnect, *, standby=False, assist_delegation=None):
        from .conversation_trial import run_voice_turn
        from .live_bench import match_streamed_reply

        device = self.device
        cached = await asyncio.to_thread(verified_render, self.data_dir)
        delay = self.alignment_delay()
        if cached is None or delay is None:
            raise ValueError("Run a successful echo reference alignment first")
        ident, pcm = cached
        async def ready_wake():
            timeout = STANDBY_ARM_MS / 1000 + 2 if standby else (15 if preconnect else 30)
            if not await self.wait_for_wake(result, timeout=timeout):
                return False
            if device.wake_details.get("ack_cache_id") != ident:
                raise RuntimeError("The verified wake acknowledgment did not start")
            return True

        if not preconnect and not await ready_wake():
            return
        def stage(phase):
            self.test["phase"] = phase
        result["metric"] = "returned_voice_to_audible_reply"
        replies = await run_voice_turn(device, self.cloud_settings, result, self.opening_audio, pcm, delay, stage,
                                       gain_db=gain_db, follow_up=follow_up,
                                       wait_for_preconnected_wake=ready_wake if preconnect else None, standby=standby,
                                       assist_delegation=assist_delegation)
        if not replies:
            return
        await asyncio.sleep(.25)
        await device.end()
        captured, capture_start = device.recording()
        result["clock_uncertainty_ms"] = round(device.clock_rtt_ms / 2 + 4, 1)
        turns = [result, result.get("warm_reply"), *result.get("additional_warm_replies", [])]
        for index, (reply, turn) in enumerate(zip(replies, turns)):
            output = np.frombuffer(reply.pcm, dtype="<i2")[::2]
            turn["reply_output_signal"] = {
                "peak_pcm": int(np.max(np.abs(output.astype(np.int32)), initial=0)),
                "full_scale_samples": int(np.count_nonzero((output == -32768) | (output == 32767))),
            }
            match = await asyncio.to_thread(
                match_streamed_reply, captured, capture_start, reply, reply.first_voice_at, device.clock_offset_ms,
                alignment_ms=delay, clock_uncertainty_ms=result["clock_uncertainty_ms"])
            match.pop("fixture_end_to_audible_ms", None)
            trial = {"trial": index + 1, "turn": "warm_followup" if index else "first", **match}
            result["trials"].append(trial)
            turn["status"] = "measured" if match["status"] == "detected" else "unavailable"
            endpoint = turn["speech_endpoint"]
            if endpoint["status"] == "estimated" and match["status"] == "detected":
                endpoint["speech_end_to_audible_ms"] = round(
                    endpoint["speech_end_to_cloud_voice_ms"] + match["latency_ms"], 1)
        if len(turns) > len(replies):
            failed = turns[len(replies)]
            if failed and "error" in failed:
                result["trials"].append({"trial": len(replies) + 1, "turn": "warm_followup",
                                         "status": "unavailable", "reason": failed["error"]})
        if follow_up and any(t["status"] == "detected" for t in result["trials"]):
            result["status"] = "measured"

    async def measure_render_alignment(self, result):
        from .live_bench import match_response, render_reference, voiced_bounds
        from .streaming_aec import check_confirmed_references

        device = self.device
        cached = await asyncio.to_thread(verified_render, self.data_dir)
        if cached is None:
            raise ValueError("Verify the wake acknowledgment before checking render alignment")
        ident, pcm = cached
        self.echo_alignment = None
        reference = await asyncio.to_thread(render_reference, pcm)
        bounds = voiced_bounds(reference)
        if bounds is None:
            raise ValueError("The approved clip contains no usable reference")
        plays = []
        await asyncio.sleep(.4)
        for index in range(2):
            self.test["phase"] = f"playing reference clip {index + 1} of 2"
            player = JackPlayback(device, f'{result["id"]}-{index + 1}')
            device.playback = player
            try:
                await player.send(pcm, prime_packets=5)
                plays.append(player)
            finally:
                with suppress(RuntimeError, OSError, TimeoutError):
                    await player.stop()
                device.playback = None
            await asyncio.sleep(.55)
        await device.end()
        captured, capture_start = device.recording()
        offset = device.clock_offset_ms
        renders, delays = [], []
        result["metric"] = "dma_start_to_acoustic_reference"
        result["playback_prime_ms"] = 50
        for index, player in enumerate(plays, 1):
            span = player.confirmed_span()
            match = {"status": "unavailable", "reason": "Voice PE did not confirm a DMA span"}
            if span is not None:
                started = (span[0] + offset) / 1000
                lo = max(0, round((started - .06 - capture_start) * RATE))
                hi = min(len(captured), round((started + .9 - capture_start) * RATE))
                match = await asyncio.to_thread(
                    match_response, captured[lo:hi], capture_start + lo / RATE, reference,
                    bounds[0], started, started, window_seconds=.14, allow_late_outlier=True)
                if match["status"] == "detected":
                    delays.append((match["audible_onset_at"] - started - bounds[0] / RATE) * 1000)
                    renders.append((pcm, *span))
            result["trials"].append({"trial": index, **match, "playback": player.snapshot()})
        if len(delays) != 2 or min(delays) < 0 or abs(delays[1] - delays[0]) > 35:
            outcome = {"status": "unavailable", "reason": "The two acoustic alignments did not agree"}
        else:
            outcome = await asyncio.to_thread(check_confirmed_references, tuple(device.frames),
                                                renders, offset, sum(delays) / 2)
        result["render_alignment"] = outcome
        boot = device.info.get("diagnostics", {}).get("boot_id")
        if outcome["status"] == "processed" and boot and device.info.get("ack_cache_id") == ident:
            self.echo_alignment = (boot, ident, outcome["estimated_delay_ms"])
        result["status"] = "measured" if outcome["status"] == "processed" else "unavailable"
        result["clock_uncertainty_ms"] = round(device.clock_rtt_ms / 2 + 4, 1)

    async def measure_interrupt(self, result):
        from .conversation_trial import SpeechEndpoint, gain_packet
        from .live_bench import SYNTHETIC_PROMPTS, render_reference, synthesize_fixture, voiced_bounds
        from .streaming_aec import ConfirmedRenderInput, residual_echo_similarity

        alignment = self.alignment_delay()
        if alignment is None:
            raise ValueError("Run Echo reference alignment before the interruption check")
        device = self.device
        pcm, _ = await synthesize_fixture(SYNTHETIC_PROMPTS[0])
        speech = np.frombuffer(pcm, dtype="<i2")
        first, end = voiced_bounds(speech)
        speech = speech[first:end].astype(np.float64)
        speech = np.rint(speech * (400 / np.max(np.abs(speech)))).astype("<i2")
        pilot_pcm = await asyncio.to_thread(stereo_pcm, pilot(np.arange(4 * RATE) / RATE))
        reference = await asyncio.to_thread(render_reference, pilot_pcm)
        player = JackPlayback(device, result["id"])
        echo = ConfirmedRenderInput(player, pilot_pcm, alignment)
        detector = SpeechEndpoint()
        trigger = asyncio.get_running_loop().create_future()
        cleaned = bytearray()
        recent = deque(maxlen=5)
        rejected = 0
        monitor_start_ms, inject_at_ms = 500, 1400

        await asyncio.sleep(.5)
        capture_start_ms = device.frames[0].capture_ms if device.frames else None
        if capture_start_ms is None:
            raise RuntimeError("No microphone pre-roll for the interruption check")
        processed = len(echo.prime_capture(device.frames, device.clock_offset_ms)) // 2

        def observe(frame, offset):
            nonlocal processed, rejected
            first_ms = player.first_rendered_ms
            if first_ms is not None:
                # Inject only into the AEC input. Leave the original microphone
                # recording untouched for independent acoustic stop detection.
                start = (frame.capture_ms - first_ms - inject_at_ms) * (RATE // 1000)
                lo, hi = max(0, -start), min(len(frame.samples), len(speech) - start)
                if hi > lo:
                    samples = frame.samples.copy()
                    mixed = samples[lo:hi].astype(np.int32) + speech[start + lo:start + hi]
                    samples[lo:hi] = np.clip(mixed, -32768, 32767)
                    frame = replace(frame, samples=samples)
            cleaned.extend(echo.offer(frame, offset))
            while len(cleaned) >= 640 and not trigger.done():
                packet = bytes(cleaned[:640])
                del cleaned[:640]
                processed += 320
                end_ms = capture_start_ms + processed * 1000 / RATE
                recent.append(packet)
                if first_ms is not None and end_ms >= first_ms + monitor_start_ms:
                    voiced = detector.observe(gain_packet(packet, 24)[0], (end_ms + offset) / 1000, time.monotonic())
                    if not voiced or len(recent) < recent.maxlen:
                        continue
                    expected_end = round((end_ms - first_ms - alignment) * RATE / 1000)
                    search_margin = RATE * 45 // 1000
                    lo = max(0, expected_end - 1600 - search_margin)
                    hi = min(echo.rendered_frames // 3, expected_end + search_margin)
                    similarity = residual_echo_similarity(b"".join(recent), reference[lo:hi])
                    if similarity is None or similarity >= .65:
                        rejected += 1
                    else:
                        trigger.set_result((end_ms - first_ms, similarity))

        self.test["phase"] = "testing a local speech-triggered stop"
        device.pcm_consumer = observe
        device.playback = player
        sender = asyncio.create_task(player.send(pilot_pcm, prime_packets=5))
        stop_at = None
        try:
            await asyncio.wait((sender, trigger), return_when=asyncio.FIRST_COMPLETED)
            if trigger.done():
                # Stop observing before issuing the intentional abort. Its
                # terminal report is not an echo-reference failure.
                device.pcm_consumer = None
                stop_at = time.monotonic()
                sender.cancel()
                with suppress(asyncio.CancelledError):
                    await sender
                await player.stop()
            else:
                await sender
        finally:
            device.pcm_consumer = None
            trigger.cancel()
            if not sender.done():
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            with suppress(RuntimeError, OSError, TimeoutError):
                await player.stop()
            device.playback = None
            result["playback"] = player.snapshot()

        report = result["interruption_probe"] = {"input": "generated speech injected before local AEC",
                                                "injection_after_render_ms": inject_at_ms,
                                                "echo_like_candidates_rejected": rejected}
        if stop_at is None or not trigger.done() or trigger.cancelled():
            result.update(status="unavailable", reason=echo.error or "No speech-triggered stop was detected")
            return
        trigger_ms, similarity = trigger.result()
        report["trigger_relative_to_injection_ms"] = round(trigger_ms - inject_at_ms, 1)
        report["trigger_render_similarity"] = round(similarity, 3)
        if trigger_ms < inject_at_ms or echo.error or player.reason != "requested":
            result.update(status="unavailable", reason=echo.error or "Stop could not be attributed to the injected speech")
            return
        report["injection_to_stop_request_ms"] = round(
            (stop_at - (player.first_rendered_ms + inject_at_ms + device.clock_offset_ms) / 1000) * 1000, 1)
        await asyncio.sleep(.75)
        await device.end()
        captured, capture_start = device.recording()
        acoustic = await asyncio.to_thread(detect_stop, captured, capture_start, stop_at)
        result["trials"].append({"trial": 1, **acoustic})
        result["status"] = "measured" if acoustic["status"] == "detected" else "unavailable"

    async def measure_reply_stall(self, result):
        from .live_bench import SYNTHETIC_PROMPTS, match_response, render_reference, synthesize_fixture

        phrases = []
        for prompt in SYNTHETIC_PROMPTS:
            pcm, end = await synthesize_fixture(prompt)
            samples = np.frombuffer(pcm, dtype="<i2")[:end]
            # End the first phrase on a 10 ms boundary so the pause is
            # injected between complete wire packets.
            samples = np.pad(samples, (0, (-len(samples)) % (RATE // 100)))
            phrases.append(await asyncio.to_thread(stereo_pcm, samples // 2))

        async def chunks():
            for index, pcm in enumerate(phrases):
                if index:
                    await asyncio.sleep(.25)
                for offset in range(0, len(pcm), 19200):
                    yield pcm[offset:offset + 19200]

        device = self.device
        await asyncio.sleep(.4)
        self.test["phase"] = "playing two local phrases with a deliberate delivery pause"
        player = JackPlayback(device, result["id"])
        device.playback = player
        requested_at = time.monotonic()
        try:
            await player.send_stream(chunks(), prime_packets=5, source_prime_packets=12, recover_reply_stalls=True)
        finally:
            with suppress(RuntimeError, OSError, TimeoutError):
                await player.stop()
            device.playback = None
            result["playback"] = player.snapshot()
        if player.frames_rendered != sum(map(len, phrases)) // 4 or player.schedule_rebases != 1:
            raise RuntimeError("Voice PE did not confirm the entire fixture and one sender recovery")

        await asyncio.sleep(.4)
        await device.end()
        captured, capture_start = device.recording()
        first_frame = 0
        for index, pcm in enumerate(phrases, 1):
            reference = await asyncio.to_thread(render_reference, pcm)

            def handoff(sample):
                return player.accepted_at(first_frame + sample * 3, device.clock_offset_ms)

            match = await asyncio.to_thread(
                match_response, captured, capture_start, reference, 0, requested_at, requested_at,
                handoff_at=handoff, acoustic_delay_range=(-.03, .35), window_seconds=.24)
            windows = match["windows"]
            heard_end = match["status"] == "detected" and windows[-1]["status"] == "detected"
            result["trials"].append({"trial": index, "status": "detected" if heard_end else "unavailable",
                                     "latency_ms": None, "windows": windows,
                                     "reason": "Speech and its final window matched" if heard_end else
                                     match.get("reason", "Final speech window could not be confirmed")})
            first_frame += len(pcm) // 4
        result["status"] = "measured" if all(t["status"] == "detected" for t in result["trials"]) else "unavailable"

    async def measure_synthetic_replies(self, result, transport, profile):
        from .live_bench import SYNTHETIC_PROMPTS, benchmark_replies, match_streamed_reply, synthesize_fixture

        self.test["phase"] = "synthesizing fixed test prompts locally"
        fixtures = [await synthesize_fixture(prompt) for prompt in SYNTHETIC_PROMPTS]
        timings = []
        def stage(phase):
            self.test["phase"] = phase
        result["metric"] = "synthetic_digital_speech_end_to_audible_reply"
        result["cloud"] = await benchmark_replies(self.device, self.cloud_settings, transport,
                                                   result["id"], fixtures, timings, stage, profile)
        if result["cloud"]["playback_enabled"]:
            await asyncio.sleep(.35)
            await self.device.end()
            captured, capture_start = self.device.recording()
            result["clock_uncertainty_ms"] = round(self.device.clock_rtt_ms / 2 + 4, 1)
            offset = self.device.clock_offset_ms
            delay = self.alignment_delay()
        for index, (endpoint, reply, answer_check) in enumerate(timings, 1):
            if result["cloud"]["playback_enabled"]:
                match = await asyncio.to_thread(
                    match_streamed_reply, captured, capture_start, reply, endpoint, offset,
                    alignment_ms=delay, clock_uncertainty_ms=result["clock_uncertainty_ms"])
            else:
                match = {"status": "digital_only", "latency_ms": None,
                         "reason": "Provider timing only. Speaker playback was disabled."}
            audible_at = match.get("audible_onset_at")
            match.update(trial=index, turn="first" if index == 1 else "warm_followup",
                         digital_end_to_cloud_voice_ms=round((reply.first_voice_at - endpoint) * 1000, 1),
                         cloud_voice_to_audible_ms=round((audible_at - reply.first_voice_at) * 1000, 1)
                         if audible_at is not None else None,
                         expected_answer_mentioned=answer_check, playback=reply.player.snapshot())
            result["trials"].append(match)
        if result["cloud"]["failure"]:
            result["error"] = result["cloud"]["failure"]
        for index in range(len(timings) + 1, len(fixtures) + 1):
            result["trials"].append({"trial": index, "turn": "first" if index == 1 else "warm_followup",
                                     "status": "unavailable", "latency_ms": None,
                                     "reason": "The synthetic turn did not complete"})
        result["status"] = "measured" if any(x["status"] in ("detected", "digital_only")
                                             for x in result["trials"]) else "unavailable"

    async def run_standby(self, gain_db):
        """Keep local wake available until explicitly stopped or the setup changes."""
        while True:
            await self.run("voice_turn", 1, gain_db=gain_db, follow_up=True, standby=True)
            result = self.results[0]
            if result.get("reason") == "arm_timeout" and result["status"] == "unavailable":
                self.results.pop(0)
            elif (not result.get("cloud", {}).get("finalization_confirmed")
                    or result["status"] == "failed" or result.get("conversation_end") == "error"):
                return
            device = self.device.snapshot()
            if (not device["connected"] or device.get("muted") or not device.get("raw_ready")
                    or self.alignment_delay() is None or not self.acknowledgment_status()["cached_on_device"]
                    or self.cloud_settings.public()["paid_retry_blocked"]):
                return
            self.test = {"running": True, "id": uuid.uuid4().hex, "phase": "rearming for Hey Jarvis", "standby": True}

    async def run(self, kind, repetitions, startup_transport=None, reply_profile="baseline", gain_db=0,
                  follow_up=False, preconnect=False, *, standby=False, assist_preview=None):
        started = time.monotonic()
        result = {"id": self.test["id"], "kind": kind, "route": "voice_pe_jack",
                  "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
                  "trials": [], "warnings": [
                      "Speaker API acceptance is a software milestone. Acoustic estimates include microphone processing delay."]}
        self.results.insert(0, result)
        del self.results[30:]
        player = sender = assist_delegation = None
        resources = AsyncExitStack()
        digital_only = kind == "synthetic_replies" and startup_transport == "realtime"
        if standby:
            result["standby"] = True
        try:
            if digital_only:
                result["route"] = "digital"
                result["warnings"].clear()
                await self.measure_synthetic_replies(result, startup_transport, reply_profile)
                return
            if assist_preview is not None:
                token, domain, name = assist_preview
                result["assist_preview"] = True
                self.test["phase"] = "checking the selected Assist device"
                origin = os.environ.get("JARVIS_ASSIST_ORIGIN", "http://127.0.0.1:8123")
                client = await resources.enter_async_context(AssistClient(origin, token))
                assist_delegation = AssistDelegation(client, await client.list_tools(), preview_target=(domain, name))
                if await assist_delegation.preview_state("setup") not in ("on", "off"):
                    raise ValueError("Assist could not uniquely resolve the selected light or switch")
            self.test["phase"] = "synchronizing Voice PE and starting capture"
            wake_kind = kind in ("wake_ack", "voice_turn")
            armed = wake_kind and self.device.info.get("wake_capture_protocol") == 1
            if wake_kind:
                self.device.wake_event.clear()
                self.device.wake_details = {}
                self.device.wake_playback_terminal = {}
                self.device.wake_render_progress = None
                self.device.wake_render_latest = None
                self.device.wake_render_changed.clear()
                self.device.wake_render_terminal_at = None
                self.device.wake_terminal_event.clear()
                result["wake_capture_mode"] = "device_armed" if armed else "prestarted_bench_capture"
            if armed:
                self.opening_audio = OpeningAudio()
                self.device.pcm_consumer = self.opening_audio.offer
                duration_ms = 5_000
                if kind == "voice_turn":
                    duration_ms = 65_000 if standby else (55_000 if follow_up else 30_000)
                await self.device.begin(result["id"], duration_ms,
                                        arm_timeout_ms=STANDBY_ARM_MS if standby else (19000 if preconnect else 32000))
            else:
                await self.device.begin(result["id"], 100000 if kind == "connection" else 65000)
            result["device_session"] = dict(self.device.capture_session)
            self.test["phase"] = "capturing a quiet baseline"
            if kind == "connection":
                await asyncio.sleep(CONNECTION_TEST_SECONDS)
                await self.device.end()
                self.device.recording()
                result.update(status="passed", metric="microphone_connection")
                return
            if kind == "voice_turn":
                await self.measure_voice_turn(result, gain_db, follow_up, preconnect, standby=standby,
                                              assist_delegation=assist_delegation)
                return
            if kind == "render_alignment":
                await self.measure_render_alignment(result)
                return
            if kind == "interrupt":
                await self.measure_interrupt(result)
                return
            if kind == "reply_stall":
                await self.measure_reply_stall(result)
                return
            if kind == "synthetic_replies":
                await self.measure_synthetic_replies(result, startup_transport, reply_profile)
                return
            if kind == "wake_ack":
                await self.measure_wake_ack(result, startup_transport)
                return
            signal_pcm, probes = bench_signal(kind, repetitions)
            pcm = await asyncio.to_thread(stereo_pcm, signal_pcm)
            player = JackPlayback(self.device, result["id"])
            live_echo = None
            alignment = self.alignment_delay() if kind == "echo" else None
            if alignment is not None:
                from .streaming_aec import ConfirmedRenderInput

                cleaned = bytearray()
                try:
                    live_echo = ConfirmedRenderInput(player, pcm, alignment)
                except (ImportError, RuntimeError) as exc:
                    result["live_echo"] = {"status": "unavailable", "reason": f"AEC3 unavailable ({type(exc).__name__})"}
                else:
                    def observe_echo(frame, offset_ms):
                        cleaned.extend(live_echo.offer(frame, offset_ms))

            await asyncio.sleep(.5)
            if live_echo:
                # Playback has not been requested. Prime AEC with known silence
                # instead of filling the bounded queue waiting for a DMA report.
                cleaned.extend(live_echo.prime_capture(self.device.frames, self.device.clock_offset_ms))
                self.device.pcm_consumer = observe_echo
            self.device.playback = player
            self.test["phase"] = "playing through the Voice PE jack"
            requested_at = time.monotonic()
            sender = asyncio.create_task(player.send(pcm, prime_packets=5 if live_echo else 1))
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
                        if live_echo and live_echo.finished and not live_echo.error:
                            render_start = (player.first_rendered_ms + offset_ms) / 1000
                            result["live_echo"] = await asyncio.to_thread(
                                measure_echo_reduction, captured, capture_start, reference,
                                render_start, alignment, precleaned=np.frombuffer(cleaned, dtype="<i2"))
                if live_echo:
                    live = result.setdefault("live_echo", {"status": "unavailable",
                        "reason": live_echo.error or "Live echo analysis needs a measured pilot and a complete render"})
                    live["processed_during_playback_samples"] = live_echo.processed_during_playback
                    live["maximum_waiting_ms"] = round(live_echo.max_waiting_samples * 1000 / RATE, 1)
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
                if not digital_only:
                    with suppress(RuntimeError, OSError, TimeoutError):
                        await self.device.end()
                    result["capture"] = self.device.capture_diagnostics()
                    result["device_session"] = dict(self.device.capture_session)
                    self.observe_firmware()
                result["summary"] = summarize(result["trials"])
                result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            finally:
                await resources.aclose()
                self.device.playback = None
                self.device.pcm_consumer = None
                self.opening_audio = None
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

    async def check_assist(request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {"token"}:
            raise ValueError("Enter a Home Assistant access token")
        token = data["token"]
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,2048}", token):
            raise ValueError("Enter a valid Home Assistant access token")
        # This App uses host networking. Keep the destination out of the
        # request so the ingress route cannot send credentials to other hosts.
        origin = os.environ.get("JARVIS_ASSIST_ORIGIN", "http://127.0.0.1:8123")
        try:
            tools = await discover_tools(origin, token)
        except AssistDiscoveryError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        return web.json_response({"tool_count": len(tools)})

    async def cloud_settings(request):
        if request.method == "PUT":
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {"api_key"}:
                raise ValueError("Only local API key entry is supported")
            if bench.test.get("standby"):
                raise web.HTTPConflict(text="Stop wake standby before changing the OpenAI key")
            bench.cloud_settings.set_key(data["api_key"])
        return web.json_response({**bench.cloud_settings.public(), "fixture_loaded": bool(bench.cloud_fixture),
                                  "benchmark_available": True,
                                  "reason": "The voice preview requires separate per-run room microphone consent"})
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
        data = await request.json()
        required = {"transport", "confirm_paid", "comfortable_volume_confirmed"}
        if not isinstance(data, dict) or not required <= set(data) <= required | {"profile"}:
            raise web.HTTPServiceUnavailable(text="Choose the built-in synthetic reply benchmark")
        from .live_bench import REPLY_PROFILES
        transport = data["transport"]
        profile = data.get("profile", "baseline")
        if not isinstance(profile, str) or profile not in REPLY_PROFILES:
            raise ValueError("Choose a supported synthetic reply profile")
        if transport not in ("websocket", "webrtc", "realtime") or data["confirm_paid"] is not True:
            raise ValueError("Explicitly confirm a paid synthetic reply test and choose the transport")
        if transport != "realtime" and data["comfortable_volume_confirmed"] is not True:
            raise ValueError("Confirm comfortable speaker volume")
        if bench.test["running"]:
            raise web.HTTPConflict(text="A test is already running")
        device = bench.device.snapshot()
        if transport != "realtime" and (not device["connected"] or device.get("muted")
                or device.get("raw_ready") is not True or device.get("playback_protocol") != 1
                or device.get("jack_plugged") is not True):
            raise ValueError("Connect the paired Voice PE and speakers before starting")
        settings = bench.cloud_settings.public()
        if not settings["api_key_configured"] or settings["paid_retry_blocked"]:
            raise ValueError("A configured key and a settled budget ledger are required")
        ident = uuid.uuid4().hex
        bench.test = {"running": True, "id": ident, "phase": "preparing synthetic reply benchmark"}
        bench.task = asyncio.create_task(bench.run("synthetic_replies", 2, transport, profile))
        return web.json_response({"test_id": ident}, status=202)

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
        if kind not in ("latency", "stop", "stability", "echo", "connection", "wake_ack", "render_alignment", "reply_stall", "interrupt", "voice_turn"):
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
        follow_up = data.get("follow_up", False)
        if type(follow_up) is not bool or (follow_up and kind != "voice_turn"):
            raise ValueError("A warm follow-up is available only in the voice preview")
        preconnect = data.get("preconnect", False)
        if type(preconnect) is not bool or (preconnect and kind != "voice_turn"):
            raise ValueError("Preconnection is available only in the consent-gated voice preview")
        standby = data.get("standby", False)
        if type(standby) is not bool or (standby and (kind != "voice_turn" or preconnect)):
            raise ValueError("Wake standby uses the voice preview and connects only after wake")
        if standby and data.get("allow_repeated_wake_audio") is not True:
            raise ValueError("Explicitly authorize cleaned microphone audio after each wake while standby is enabled")
        assist_preview = data.get("assist_preview")
        if assist_preview is not None:
            if (kind != "voice_turn" or standby or follow_up or preconnect
                    or data.get("allow_assist_power_action") is not True
                    or not isinstance(assist_preview, dict) or set(assist_preview) != {"token", "domain", "name"}):
                raise ValueError("Assist power control needs separate consent for a single voice preview")
            token, domain, name = (assist_preview[key] for key in ("token", "domain", "name"))
            if (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,2048}", token)
                    or domain not in ("light", "switch") or not isinstance(name, str)
                    or not 0 < len(name.strip()) <= 128 or not name.isprintable()):
                raise ValueError("Enter a Home Assistant token and the exact name of an exposed light or switch")
            assist_preview = (token, domain, name.strip())
        gain_db = data.get("input_gain_db", 24 if kind == "voice_turn" else 0)
        if type(gain_db) is not int or gain_db not in (0, 12, 24, 36):
            raise ValueError("Choose 0, 12, 24 or 36 dB of preview microphone gain")
        if kind == "voice_turn":
            if data.get("confirm_paid") is not True or data.get("allow_room_audio_to_openai") is not True:
                raise ValueError("Explicitly authorize sending cleaned microphone audio to OpenAI for this turn")
            settings = bench.cloud_settings.public()
            if not settings["api_key_configured"] or settings["paid_retry_blocked"]:
                raise ValueError("A configured key and a settled budget ledger are required")
            if device.get("wake_capture_protocol") != 1 or bench.alignment_delay() is None:
                raise ValueError("Run Echo reference alignment successfully on the current Voice PE first")
        if kind in ("wake_ack", "voice_turn") and not bench.acknowledgment_status()["cached_on_device"]:
            raise ValueError("Verify and preload the wake acknowledgment before testing it")
        if kind == "interrupt" and bench.alignment_delay() is None:
            raise ValueError("Run Echo reference alignment before the interruption check")
        if kind == "render_alignment" and not bench.acknowledgment_status()["verified"]:
            raise ValueError("Verify the wake clip before testing its render alignment")
        startup_transport = data.get("startup_transport")
        if startup_transport is not None:
            if (kind != "wake_ack" or startup_transport not in ("websocket", "webrtc")
                    or data.get("confirm_paid") is not True):
                raise ValueError("Explicitly confirm the paid wake startup test and choose a valid transport")
            settings = bench.cloud_settings.public()
            if not settings["api_key_configured"] or settings["paid_retry_blocked"]:
                raise ValueError("A configured key and a settled budget ledger are required")
        ident = uuid.uuid4().hex
        bench.test = {"running": True, "id": ident, "phase": "preparing"}
        if kind == "voice_turn":
            bench.test["input_gain_db"] = gain_db
        if standby:
            bench.test["standby"] = True
            bench.task = asyncio.create_task(bench.run_standby(gain_db))
        else:
            bench.task = asyncio.create_task(bench.run(kind, repetitions, startup_transport, gain_db=gain_db,
                                                      follow_up=follow_up, preconnect=preconnect,
                                                      assist_preview=assist_preview))
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
    app.router.add_post("/api/assist/check", check_assist)
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
                                "ack_cache_protocol", "ack_cache_id", "wake_capture_protocol", "capture_gain") if k in message}
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
                    if bench.device.info.get("jack_plugged") is False:
                        bench.echo_alignment = None
                    if message.get("type") == "status":
                        bench.maybe_preload_wake_cache()
                    bench.observe_firmware()
                    if bench.device.info.get("muted") and bench.test.get("id") and bench.test["running"]:
                        asyncio.create_task(bench.cancel("device_muted"))
                    elif (bench.device.playback or bench.test.get("standby")) and bench.device.info.get("jack_plugged") is False:
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
