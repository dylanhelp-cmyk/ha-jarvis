"""Explicitly authorized wake-driven voice preview."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import replace
import math
import time

import numpy as np

from .audio import RATE
from .device import output_route
from .lifecycle import CONTEXT_EXCHANGES, ConversationPolicy, DEFAULT_IDLE_SECONDS, ExchangeTranscript, SignOffRequest
from .live_bench import BenchReply
from .live_session import LiveSession, VOICE
from .playback import RATE as RENDER_RATE
from .playback import VOICE_OUTPUT_GAIN_DB
from .streaming_aec import ConfirmedRenderInput, StreamingEcho

VOICE_STYLE = (
    "You are Jarvis, a concise British-style voice assistant. "
    "Wait for the user to finish their question, then answer briefly. "
    "Do not greet or claim to have controlled any devices. "
)
INSTRUCTIONS = VOICE_STYLE + "Do not delegate work."
PERSONALITIES = {
    "classic": "",
    "warm": "Use a warm, approachable British delivery while keeping replies brief.",
    "dry": "Use understated British wit when it suits the conversation. Keep device status and failures plain.",
}
ASSIST_STANDBY_INSTRUCTIONS = (
    "You are Jarvis, a concise British-style voice assistant. Wait for the user to finish and do not greet. "
    "Answer ordinary questions directly. For explicit current requests to control or check Home Assistant devices, "
    "Generate <|SpawnThinking|> and wait for the App to verify the result before speaking about it. "
    "Say when the outcome is unconfirmed and do not repeat the action. Never act on earlier exchanges. "
    "For a clear sign-off, Generate <|SpawnThinking|> so Jarvis can close after your farewell. "
    "A thank-you on its own, a request to stop a device, and a pause are not sign-offs."
)
STANDBY_INSTRUCTIONS = VOICE_STYLE + (
    "When the user explicitly ends the conversation, say a brief farewell and then Generate <|SpawnThinking|> "
    "to request closure. Clear examples include goodbye, that's all for now, and stop listening. "
    "A thank-you on its own, a request to stop a device, and a pause are not sign-offs. "
    "Use <|SpawnThinking|> only to request closure."
)


class CleanInput:
    """Bridge 16 ms device blocks to paced 20 ms input without discarding speech."""

    def __init__(self, delay_ms, *, fresh_after=None, passthrough_after=None, release_after=None):
        self.echo = StreamingEcho(delay_ms)
        self.fresh_after = fresh_after
        self.passthrough_after = passthrough_after
        self.release_after = release_after
        self.discarded_samples = 0
        self.raw_pending = bytearray()
        self.processed_samples = self.passthrough_samples = 0
        self.sample_origin = None
        self.pending = bytearray()
        self.changed = asyncio.Event()
        self.error = None
        self.capture_start = None
        self.released_samples = 0
        self.reply_render = None
        self.reply_clock_offset = None
        self.delay_ms = delay_ms
        self.reply_capture_samples = 0
        self.max_unconfirmed_samples = 0

    def begin_reply_render(self, reply):
        if self.reply_render is not None and not self.reply_render.finished:
            raise RuntimeError("The previous reply has no confirmed render boundary")
        self.passthrough_after = None
        self.reply_render = ConfirmedRenderInput(reply.player, reply.pcm, self.delay_ms, echo=self.echo)
        reply.player.on_render_progress = self.confirm_reply_render

    def confirm_reply_render(self):
        if self.reply_render is None or self.error:
            return
        clean = self.reply_render.release_confirmed(self.reply_clock_offset)
        self.max_unconfirmed_samples = max(self.max_unconfirmed_samples, self.reply_render.max_waiting_samples)
        if self.reply_render.error:
            self.error = "reply_echo_reference_failed: " + self.reply_render.error
            self.pending.clear()
        else:
            self.append_clean(clean)
        self.changed.set()

    def finish_reply_render(self):
        self.confirm_reply_render()
        if self.error or self.reply_render is None or not self.reply_render.finished:
            raise RuntimeError(self.error or "The reply echo reference did not finish")
        player = self.reply_render.player
        player.on_render_progress = None
        self.passthrough_after = (player.last_rendered_ms + self.reply_clock_offset + self.delay_ms) / 1000 + .18

    def offer(self, frame, clock_offset_ms):
        if self.error:
            return
        try:
            captured_at = (frame.capture_ms + clock_offset_ms) / 1000
            if self.fresh_after is not None:
                if captured_at < self.fresh_after:
                    return
                if self.sample_origin is None:
                    self.sample_origin = frame.first_sample
                frame = replace(frame, first_sample=frame.first_sample - self.sample_origin)
            if self.capture_start is None:
                self.capture_start = captured_at
            self.raw_pending.extend(frame.samples.tobytes())
            self.reply_clock_offset = clock_offset_ms
            if self.reply_render is not None:
                clean = self.reply_render.offer(frame, clock_offset_ms)
                self.reply_capture_samples += len(frame.samples)
                self.max_unconfirmed_samples = max(self.max_unconfirmed_samples, self.reply_render.max_waiting_samples)
                if self.reply_render.error:
                    raise RuntimeError(self.reply_render.error)
            else:
                clean = self.echo.capture(frame, clock_offset_ms)
            self.append_clean(clean)
        except (RuntimeError, ValueError):
            self.error = "opening_echo_or_queue_failed"
            self.pending.clear()
        self.changed.set()

    def append_clean(self, clean):
        try:
            # Track the unfiltered counterpart even while AEC is active, so
            # partial 10 ms blocks stay aligned across render generations.
            if len(clean) > len(self.raw_pending):
                raise RuntimeError("Echo processor output lost its capture alignment")
            raw = bytes(self.raw_pending[:len(clean)])
            del self.raw_pending[:len(clean)]
            if self.passthrough_after is not None:
                cutoff = math.ceil((self.passthrough_after - self.capture_start) * RATE)
                boundary = min(len(clean) // 2, max(0, cutoff - self.processed_samples))
                clean = clean[:2 * boundary] + raw[2 * boundary:]
                self.passthrough_samples += len(raw) // 2 - boundary
            first_sample = self.processed_samples
            self.processed_samples += len(clean) // 2
            if self.release_after is not None:
                cutoff = math.ceil((self.release_after - self.capture_start) * RATE)
                discard = min(len(clean) // 2, max(0, cutoff - first_sample))
                clean = clean[2 * discard:]
                self.discarded_samples += discard
            if len(self.pending) + len(clean) > 3 * RATE * 2:
                raise RuntimeError("clean_opening_queue_overflow")
            self.pending.extend(clean)
        except (RuntimeError, ValueError):
            self.error = "opening_echo_or_queue_failed"
            self.pending.clear()
        self.changed.set()

    async def packet(self):
        while len(self.pending) < 640:
            if self.error:
                raise RuntimeError(self.error)
            self.changed.clear()
            await asyncio.wait_for(self.changed.wait(), .35)
        return self.take_packet()

    def take_packet(self):
        if self.error:
            raise RuntimeError(self.error)
        if len(self.pending) < 640:
            raise RuntimeError("A complete microphone packet is not available")
        packet = bytes(self.pending[:640])
        del self.pending[:640]
        self.released_samples += 320
        return packet, self.capture_start + (self.discarded_samples + self.released_samples) / RATE


class OpeningEchoInput(CleanInput):
    """Hold capture until the matching speaker report covers its echo reference."""

    def __init__(self, device, pcm, delay_ms, first_ms, *, after_greeting=False):
        offset = device.clock_offset_ms
        super().__init__(delay_ms, passthrough_after=(first_ms + offset) / 1000 + 3)
        self.device = device
        self.delay_ms = delay_ms
        self.first_ms = first_ms
        self.after_greeting = after_greeting
        self.render_frames = len(pcm) // 4
        self.echo.render(pcm, (first_ms + offset) / 1000, first_frame=0)
        if not device.wake_terminal_event.is_set():
            self.changed = device.wake_render_changed
        self.waiting_frames = deque()
        self.waiting_samples = 0
        self.confirmed = False
        self.terminal_deadline = time.monotonic() + 3

    def offer(self, frame, clock_offset_ms):
        if self.error:
            return
        if self.waiting_samples + len(frame.samples) + len(self.pending) // 2 > 3 * RATE:
            self.error = "clean_opening_queue_overflow"
        else:
            self.waiting_frames.append((frame, clock_offset_ms))
            self.waiting_samples += len(frame.samples)
        self.changed.set()

    def confirm(self):
        if self.confirmed:
            return
        terminal = self.device.wake_playback_terminal
        last_ms = terminal.get("last_rendered_ms")
        if (terminal.get("drained") is not True or terminal.get("first_rendered_ms") != self.first_ms
                or terminal.get("rendered_samples") != self.render_frames or type(last_ms) is not int):
            raise RuntimeError("wake_echo_reference_incomplete")
        offset = self.device.clock_offset_ms
        if self.after_greeting:
            gap = terminal.get("max_render_discontinuity_us")
            if (type(gap) is not int or not 0 <= gap <= 15_000
                    or abs(last_ms - self.first_ms - self.render_frames / 48) > 15):
                raise RuntimeError("The greeting playback clock had a gap. Retry or allow speech during the greeting.")
            self.release_after = (last_ms + offset + self.delay_ms) / 1000
        self.echo.finish_render(self.render_frames, (last_ms + offset) / 1000)
        self.passthrough_after = (last_ms + offset + self.delay_ms) / 1000 + .18
        self.confirmed = True

    async def packet(self):
        while True:
            if self.error:
                raise RuntimeError(self.error)
            if self.device.wake_terminal_event.is_set():
                self.confirm()
            elif time.monotonic() >= self.terminal_deadline or self.device.errors:
                raise RuntimeError("wake_echo_reference_incomplete")
            horizon = math.inf if self.confirmed else self.first_ms + self.device.wake_render_latest[0] / 48 - 20
            while self.waiting_frames and len(self.pending) < 640:
                frame, offset = self.waiting_frames[0]
                if frame.capture_ms + len(frame.samples) * 1000 / RATE > horizon:
                    break
                self.waiting_frames.popleft()
                self.waiting_samples -= len(frame.samples)
                super().offer(frame, offset)
                if self.error:
                    raise RuntimeError(self.error)
            if len(self.pending) >= 640:
                return await super().packet()
            self.changed.clear()
            await asyncio.wait_for(self.changed.wait(), .35)

    async def reply_chunks(self, reply):
        async with asyncio.timeout(3):
            await self.device.wake_terminal_event.wait()
        self.confirm()
        async for chunk in reply.chunks():
            yield chunk


def warm_input_after_render(device, reply, delay_ms):
    """Warm AEC with the measured render tail, then release only new speech."""
    span = reply.player.confirmed_span()
    offset = device.clock_offset_ms
    if (span is None or offset is None or reply.player.schedule_rebases or not device.frames
            or (reply.player.max_render_discontinuity_us or 0) > 15_000):
        return None
    total_frames = len(reply.pcm) // 4
    if total_frames != reply.player.frames_rendered or abs(span[1] - span[0] - total_frames / 48) > 15:
        return None

    # Retain enough of the known reply to warm the echo filter. Nothing from
    # before the estimated end of audible speech goes to the next turn.
    tail_frames = min(total_frames, RENDER_RATE // 2)
    render_end = (span[1] + offset) / 1000
    render_start = render_end - tail_frames / RENDER_RATE
    acoustic_end = render_end + delay_ms / 1000
    if time.monotonic() - acoustic_end > .2:
        return None
    stream = CleanInput(delay_ms, fresh_after=render_start + .025, release_after=acoustic_end,
                        passthrough_after=acoustic_end + .18)
    stream.echo.render(bytes(reply.pcm[-4 * tail_frames:]), render_start, first_frame=0)
    stream.echo.finish_render(tail_frames, render_end)

    # Capture is bounded by the current device generation. Replay only the
    # end of the capture while preserving the device's original sample order.
    frames = []
    for frame in reversed(device.frames):
        if (frame.capture_ms + offset) / 1000 < stream.fresh_after:
            break
        frames.append(frame)
    if not frames or (frames[-1].capture_ms + offset) / 1000 >= acoustic_end - .1:
        return None
    for frame in reversed(frames):
        stream.offer(frame, offset)
    return None if stream.error else stream


class SpeechEndpoint:
    """Estimate a local speech boundary from consecutive cleaned 20 ms packets."""

    def __init__(self, detector=None):
        if detector is None:
            import webrtcvad
            detector = webrtcvad.Vad(2)
        self.detector = detector
        self.recent = deque(maxlen=5)
        self.quiet_frames = 0
        self.active = False
        self.last_voice = None
        self.voice_packets = 0

    def observe(self, pcm, captured_at, sent_at):
        voiced = self.detector.is_speech(pcm, RATE)
        self.recent.append(voiced)
        if voiced:
            self.voice_packets += 1
            self.quiet_frames = 0
            if self.active or sum(self.recent) >= 3:
                self.active = True
                self.last_voice = captured_at, sent_at
                return True
        else:
            self.quiet_frames += 1
            if self.quiet_frames >= 8:
                self.active = False
        return False

    def timing(self, first_voice_at):
        scope = "local WebRTC VAD estimate on echo-processed capture"
        if self.last_voice is None or self.quiet_frames < 8:
            return {"status": "unavailable", "scope": scope, "reason": "No qualified speech followed by 160 ms of quiet"}
        captured_at, sent_at = self.last_voice
        if not 0 <= first_voice_at - sent_at <= 2:
            return {"status": "unavailable", "scope": scope, "reason": "The speech estimate could not be paired with this reply"}
        return {"status": "estimated", "scope": scope,
                "speech_end_to_send_ms": round((sent_at - captured_at) * 1000, 1),
                "speech_end_send_to_cloud_voice_ms": round((first_voice_at - sent_at) * 1000, 1),
                "speech_end_to_cloud_voice_ms": round((first_voice_at - captured_at) * 1000, 1)}


def gain_packet(pcm, gain_db):
    """Apply fixed gain after AEC, with peak protection at the highest setting."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
    scaled = np.rint(samples * 10 ** (gain_db / 20)).astype(np.int32)
    clipped = int(np.count_nonzero((scaled < -32768) | (scaled > 32767)))
    if gain_db == 36:
        knee, headroom = 24576, 8191
        magnitude = np.abs(scaled)
        above_knee = magnitude > knee
        excess = magnitude[above_knee] - knee
        scaled[above_knee] = np.rint(
            np.sign(scaled[above_knee]) * (knee + headroom * (excess / (headroom + excess)))
        ).astype(np.int32)
        clipped = 0
    output = np.clip(scaled, -32768, 32767).astype("<i2")
    return output.tobytes(), int(np.max(np.abs(samples))), int(np.max(np.abs(output.astype(np.int32)))), clipped


REPLY_DRAIN_SECONDS = 8


class FollowUpEnded(TimeoutError):
    """The local policy ended the follow-up window."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


async def stream_reply(device, session, result, stream, set_output, phase, listening_phase, *, gain_db, max_age_ms,
                       warm_follow_up=False, source_prime_packets=12, input_deadline=None,
                       idle_seconds=DEFAULT_IDLE_SECONDS, closure_policy=None, assist=False,
                       output_gain_db=VOICE_OUTPUT_GAIN_DB, duplex=False):
    reply = BenchReply(device, result["id"], output_gain_db=output_gain_db)
    early_echo = isinstance(stream, OpeningEchoInput) and not stream.confirmed
    sender = None
    endpoint = SpeechEndpoint()
    vad_gain_db = min(gain_db, 24)
    sent = peak_before = peak_after = clipped = 0
    first_sent_at = None
    first_age = max_age = None
    completed = False

    async def send_packet():
        nonlocal origin, sent, first_sent_at, first_age, max_age, peak_before, peak_after, clipped
        packet, captured_at = await stream.packet()
        now = time.monotonic()
        if sent == 0:
            origin = now
        elif (early_echo or duplex) and now > origin + sent / RATE:
            origin = now - sent / RATE
        due = origin + sent / RATE
        await asyncio.sleep(max(0, due - time.monotonic()))
        if time.monotonic() - due > .1:
            raise RuntimeError("Microphone input pacing exceeded 100 ms")
        if reply.first_voice_at is not None and not duplex:
            return
        sent_at = time.monotonic()
        age = (sent_at - captured_at) * 1000
        if not -100 <= age <= max_age_ms:
            raise RuntimeError("Microphone input exceeded its capture-age bound; retry the turn")
        first_age = age if first_age is None else first_age
        max_age = age if max_age is None else max(max_age, age)
        transmitted, before, after, overload = gain_packet(packet, gain_db)
        await session.send_audio(transmitted)
        if first_sent_at is None:
            first_sent_at = sent_at
        peak_before = max(peak_before, before)
        peak_after = max(peak_after, after)
        clipped += overload
        vad_pcm = transmitted if vad_gain_db == gain_db else gain_packet(packet, vad_gain_db)[0]
        if endpoint.observe(vad_pcm, captured_at, sent_at):
            policy.user_speech(sent_at)
        sent += 320

    async def track_render(chunks):
        bound = False
        async for chunk in chunks:
            if not bound:
                stream.begin_reply_render(reply)
                bound = True
            yield chunk

    try:
        device.pcm_consumer = stream.offer
        device.playback = reply.player
        # The extra source reserve remains on the App. The device window is
        # still 50 ms and playback keeps the original sample clock.
        result["reply_output_gain_db"] = output_gain_db
        result["reply_source_reserve_ms"] = source_prime_packets * 10
        chunks = stream.reply_chunks(reply) if early_echo else reply.chunks()
        if duplex:
            chunks = track_render(chunks)
        prime_packets = 10 if duplex else 5
        result["reply_playback_prime_ms"] = prime_packets * 10
        sender = asyncio.create_task(reply.player.send_stream(chunks, prime_packets=prime_packets,
                                                               source_prime_packets=source_prime_packets,
                                                               recover_reply_stalls=True))
        phase(listening_phase)
        set_output(reply)
        origin = listening_started = time.monotonic()
        policy = closure_policy if closure_policy is not None else ConversationPolicy(idle_seconds=idle_seconds)
        policy.start_listening(listening_started)
        # Bound the paid preview separately from the conversational idle timer.
        deadline = listening_started + (18 if warm_follow_up else 10)
        while reply.first_voice_at is None:
            now = time.monotonic()
            if session.metrics.error:
                raise RuntimeError(session.metrics.error)
            if device.errors or sender.done() or output_route(device.info) is None:
                raise RuntimeError("The microphone or speaker playback failed")
            if input_deadline is not None and now >= input_deadline:
                raise FollowUpEnded("session_limit", "The conversation reached the preview session limit")
            pending_work = assist and session.assist_work_pending
            if policy.pending_work and not pending_work:
                policy.last_activity = now
            policy.pending_work = int(pending_work)
            if warm_follow_up and policy.should_close(now):
                if policy.ending_requested:
                    raise FollowUpEnded("signoff", "The sign-off grace period finished")
                raise FollowUpEnded("inactivity", f"No detected speech for {idle_seconds:g} seconds")
            if input_deadline is None and now >= deadline and not policy.pending_work:
                raise TimeoutError("No spoken reply before the preview time limit")
            await send_packet()
        if not duplex:
            device.pcm_consumer = None
        result["speech_endpoint"] = endpoint.timing(reply.first_voice_at)
        sent_before_reply = sent
        result["digital_first_voice_ms"] = round((reply.first_voice_at - (first_sent_at or origin)) * 1000, 1)
        phase("playing the GPT-Live reply")
        async with asyncio.timeout(REPLY_DRAIN_SECONDS):
            while not reply.ready_to_drain(time.monotonic()) or (assist and session.assist_work_pending):
                if sender.done():
                    sender.result()
                    raise RuntimeError("Reply playback stopped early")
                if session.metrics.error:
                    raise RuntimeError("GPT-Live stopped before the reply finished")
                if device.errors or output_route(device.info) is None:
                    raise RuntimeError("The microphone or speaker playback failed")
                if duplex:
                    await send_packet()
                else:
                    await asyncio.sleep(.025)
            set_output(None)
            await reply.finish()
            await sender
        if reply.player.frames_rendered != reply.player.frames_sent:
            raise RuntimeError("Voice PE did not confirm the complete reply")
        if duplex:
            stream.finish_reply_render()
            result["duplex_input"] = {"forwarded_during_reply": sent > sent_before_reply,
                "samples_forwarded_during_reply": sent - sent_before_reply,
                "max_unconfirmed_capture_ms": round(stream.max_unconfirmed_samples * 1000 / RATE, 1),
                "render_reference_confirmed": True}
        completed = True
        return reply
    finally:
        set_output(None)
        if not completed:
            reply.discard()
        terminal_at = getattr(device, "wake_render_terminal_at", None)
        if early_echo and first_sent_at is not None and terminal_at is not None:
            result["first_input_before_ack_terminal_ms"] = round((terminal_at - first_sent_at) * 1000, 1)
        result["input_samples_sent"] = sent
        if isinstance(stream, OpeningEchoInput) and stream.after_greeting:
            result["greeting_capture_omitted_ms"] = round(stream.discarded_samples * 1000 / RATE, 1)
        result["input_queue"] = {"first_packet_age_ms": round(first_age, 1) if first_age is not None else None,
                                 "max_packet_age_ms": round(max_age, 1) if max_age is not None else None}
        result["input_signal"] = {"gain_db": gain_db, "peak_before_gain_pcm": peak_before,
                                  "peak_transmitted_pcm": peak_after, "clipped_samples": clipped,
                                  "vad_voice_packets": endpoint.voice_packets,
                                  "vad_gain_db": vad_gain_db,
                                  "capture_samples_after_echo_guard": stream.passthrough_samples}
        device.pcm_consumer = None
        reply.player.on_render_progress = None
        if sender is not None:
            if not sender.done():
                sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            with suppress(RuntimeError, OSError, TimeoutError):
                await reply.player.stop()
            result["playback"] = reply.player.snapshot()
        result["reply_output_signal"] = reply.signal_report()
        device.playback = None


async def run_voice_turn(device, settings, result, opening, pcm, delay_ms, phase, *, gain_db=0,
                         follow_up=False, wait_for_preconnected_wake=None, standby=False,
                         assist_delegation=None, after_greeting=False, idle_seconds=DEFAULT_IDLE_SECONDS,
                         standby_context=None, voice=VOICE, personality="classic",
                         output_gain_db=VOICE_OUTPUT_GAIN_DB, duplex=False):
    """Reuse one connection for the opening and any enabled warm turns."""
    ws = device.ws
    preconnect = wait_for_preconnected_wake is not None
    startup = None
    output = None
    heard_request = False
    assist = assist_delegation is not None
    closure_policy = ConversationPolicy(idle_seconds=idle_seconds) if standby else None
    signoff = SignOffRequest() if standby else None
    transcript = ExchangeTranscript() if standby_context is not None else None
    recent = deque(maxlen=CONTEXT_EXCHANGES)
    prior_exchanges = standby_context.take() if standby_context is not None else ()

    def request_signoff():
        signoff.requested = True
        return output is not None and signoff.clear_closing_phrase()

    def input_transcript(delta):
        nonlocal heard_request
        if output is not None:
            signoff.append(delta)
            heard_request |= bool(delta.strip())

    def allow_assist_action():
        return output is not None and heard_request and not signoff.clear_closing_phrase()

    def collect_transcript(speaker, delta, start_ms, end_ms):
        if output is not None:
            transcript.append(speaker, delta, start_ms, end_ms)

    def finish_turn():
        nonlocal heard_request
        heard_request = False
        signed_off = signoff is not None and signoff.finish()
        if signed_off:
            closure_policy.request_end(clear_closing_intent=True)
        if transcript is not None:
            exchange = transcript.finish()
            if exchange is not None and not signed_off and not (assist and session.assist_used_this_turn):
                recent.append(exchange)

    def set_output(reply):
        nonlocal output
        output = reply

    async def on_audio(audio, rate):
        if output is not None:
            await output.receive(audio, rate)

    result["connection_start"] = "before_wake" if preconnect else "on_wake"
    instructions = STANDBY_INSTRUCTIONS if standby else INSTRUCTIONS
    if assist:
        if standby:
            instructions = ASSIST_STANDBY_INSTRUCTIONS
        else:
            instructions = ("You are Jarvis, a concise British-style voice assistant. Do not greet. "
                            "For an explicit request to turn a named light or switch on or off, Generate <|SpawnThinking|>. "
                            "Wait for the tool result before saying the device changed. Report only verified outcomes.")
    if style := PERSONALITIES[personality]:
        instructions += " " + style
    if standby_context is not None:
        result["resumed_context_exchanges"] = len(prior_exchanges)
        instructions += (" Use any earlier exchanges only as background for the next spoken request. "
                         "Wait for new speech before responding.")
    session = LiveSession(settings, on_audio=on_audio, transport="websocket",
                          max_seconds=60 if follow_up or preconnect else 30,
                          startup_timeout=3, instructions=instructions, assist_delegation=assist_delegation,
                          on_input_transcript=input_transcript if signoff is not None else None,
                          on_signoff_request=request_signoff if signoff is not None else None,
                          on_timed_transcript=collect_transcript if transcript is not None else None,
                          prior_exchanges=prior_exchanges, allow_assist_action=allow_assist_action if standby and assist else None,
                          voice=voice)
    try:
        phase("preconnecting to GPT-Live" if preconnect else
              "connecting to GPT-Live while the cached acknowledgment plays")
        started = time.monotonic()
        startup = asyncio.create_task(session.start())
        async with asyncio.timeout(3):
            await startup
        ready_at = time.monotonic()
        if preconnect and not await wait_for_preconnected_wake():
            return []
        wake_received_at = getattr(device, "last_wake_at", None) or started
        result["wake_receipt_to_ready_ms"] = round((ready_at - wake_received_at) * 1000, 1)
        wake_stamp = device.wake_details.get("device_time_ms")
        if type(wake_stamp) is int and 0 <= wake_stamp <= 0xFFFFFFFF and device.clock_offset_ms is not None:
            detected_at = (wake_stamp + device.clock_offset_ms) / 1000
            result["wake_detection_to_ready_ms"] = round((ready_at - detected_at) * 1000, 1)
        if not device.wake_terminal_event.is_set():
            async with asyncio.timeout(3):
                await device.wake_render_changed.wait()
        progress = getattr(device, "wake_render_progress", None)
        if not after_greeting and progress and opening.frames and not device.wake_terminal_event.is_set():
            stream = OpeningEchoInput(device, pcm, delay_ms, progress[1])
            result["opening_reference_mode"] = "confirmed_prefix"
        else:
            async with asyncio.timeout(3):
                await device.wake_terminal_event.wait()
            first = device.wake_playback_terminal.get("first_rendered_ms")
            if type(first) is not int:
                raise RuntimeError("wake_echo_reference_incomplete")
            stream = OpeningEchoInput(device, pcm, delay_ms, first, after_greeting=after_greeting)
            stream.confirm()
            result["opening_reference_mode"] = "after_greeting" if after_greeting else "terminal"
        buffered, result["opening_audio"] = opening.release(time.monotonic())
        if not buffered or device.errors:
            raise RuntimeError(result["opening_audio"]["reason"] or "opening_capture_failed")
        offset = device.clock_offset_ms
        for frame in buffered:
            stream.offer(frame, offset)
        input_deadline = session.media_deadline() - REPLY_DRAIN_SECONDS if standby else None
        if assist:
            session.begin_turn()
        replies = [await stream_reply(device, session, result, stream, set_output, phase,
                                      "streaming the cleaned opening speech",
                                      gain_db=gain_db, max_age_ms=opening.max_ms,
                                      source_prime_packets=20 if preconnect else 12,
                                      closure_policy=closure_policy,
                                      input_deadline=input_deadline if assist else None, assist=assist,
                                      output_gain_db=output_gain_db, duplex=duplex)]
        finish_turn()
        terminal = device.wake_playback_terminal
        terminal_at = getattr(device, "wake_render_terminal_at", None)
        if progress and terminal_at is not None and progress[1] == terminal.get("first_rendered_ms"):
            samples, _, received_at = progress
            result["wake_render_report"] = {
                "first_report_rendered_samples": samples,
                "first_report_to_terminal_ms": round((terminal_at - received_at) * 1000, 1),
            }
        while follow_up:
            if input_deadline is not None and time.monotonic() >= input_deadline:
                result["conversation_end"] = "session_limit"
                break
            if duplex:
                reference = "continuous_capture_and_render"
            else:
                stream = warm_input_after_render(device, replies[-1], delay_ms)
                reference = "confirmed_render_tail" if stream is not None else "quiet_tail_guard"
            if stream is None:
                phase("clearing the speaker echo tail")
                await asyncio.sleep(.18)
                fresh_at = time.monotonic()
                stream = CleanInput(delay_ms, fresh_after=fresh_at, passthrough_after=fresh_at)
            turn_number = len(replies) + 1
            warm = {"id": f'{result["id"]}-warm-{turn_number}', "opening_reference_mode": reference}
            try:
                if assist:
                    session.begin_turn()
                replies.append(await stream_reply(device, session, warm, stream, set_output, phase,
                                                  "listening for your warm follow-up", gain_db=gain_db,
                                                  max_age_ms=opening.max_ms if duplex else 250,
                                                  warm_follow_up=True, input_deadline=input_deadline,
                                                  idle_seconds=idle_seconds, closure_policy=closure_policy, assist=assist,
                                                  output_gain_db=output_gain_db, duplex=duplex))
                finish_turn()
            except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
                if standby and isinstance(exc, FollowUpEnded):
                    result["conversation_end"] = exc.reason
                    break
                warm.update(status="unavailable", error=str(exc) or type(exc).__name__)
                if standby:
                    result["conversation_end"] = "error"
            if turn_number == 2:
                result["warm_reply"] = warm
            else:
                result.setdefault("additional_warm_replies", []).append(warm)
            if not standby or "error" in warm:
                break
        return replies
    finally:
        device.pcm_consumer = None
        session.suppress_output()
        if startup is not None and not startup.done():
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
        wake = device.wake_details
        if (device.ws is ws and ws is not None and not ws.closed and wake.get("ack_playback_id")
                and not device.wake_terminal_event.is_set()):
            with suppress(OSError, RuntimeError, TimeoutError):
                await asyncio.wait_for(ws.send_json({"type": "play_stop",
                    "playback_id": wake["ack_playback_id"], "generation": wake["ack_generation"]}), 1)
        metrics = await session.close()
        if (standby_context is not None and result.get("conversation_end") in ("inactivity", "session_limit")
                and not closure_policy.ending_requested and metrics.finalization_confirmed
                and metrics.close_reason == "close_requested" and not metrics.error):
            standby_context.save(recent)
            result["remembered_context_exchanges"] = len(recent)
        profile = "wake standby" if standby else ("warm follow-up preview" if follow_up else "one-turn preview")
        if assist:
            profile = "Assist wake standby" if standby else "single-device Assist preview"
        result["cloud"] = {"transport": "websocket", "profile": profile, "voice": voice, "personality": personality,
                           "startup_ms": round(metrics.ready_ms, 1) if metrics.ready_ms is not None else None,
                           **metrics.websocket_startup_breakdown(),
                           "finalization_confirmed": metrics.finalization_confirmed,
                           "usage_seconds": metrics.usage_seconds,
                           "close_reason": metrics.close_reason,
                           "local_close_reason": metrics.local_close_reason}
