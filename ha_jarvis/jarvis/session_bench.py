"""Bounded generated-speech checks for session policy and short-term context."""
import asyncio
import re
import time

import numpy as np

from .conversation_trial import PERSONALITIES, STANDBY_INSTRUCTIONS
from .lifecycle import ConversationPolicy, ExchangeTranscript, SignOffRequest, StandbyContext
from .live_bench import synthesize_fixture, voiced_bounds
from .live_session import LiveSession, RATE


SESSION_PROMPTS = ("What is the capital of France?", "Which river flows through that city?", "Thanks.", "Goodbye.")


class DigitalConversation:
    """Feed generated audio at real-time speed and keep only bounded text in RAM."""

    def __init__(self, settings, voice, instructions, prior_exchanges=()):
        self.signoff = SignOffRequest()
        self.transcript = ExchangeTranscript()
        self.output_text = ""
        self.last_audio_at = self.last_voice_at = None
        self.next_input_at = None
        self.session = LiveSession(settings, transport="websocket", voice=voice, instructions=instructions,
            on_audio=self.audio, on_transcript=self.output_transcript, on_input_transcript=self.signoff.append,
            on_signoff_request=self.request_signoff, on_timed_transcript=self.transcript.append,
            prior_exchanges=prior_exchanges)

    async def audio(self, pcm, rate):
        if rate != RATE:
            raise ValueError("Unexpected generated conversation audio format")
        self.last_audio_at = time.monotonic()
        if voiced_bounds(np.frombuffer(pcm, dtype="<i2")) is not None:
            self.last_voice_at = self.last_audio_at

    def output_transcript(self, delta):
        self.output_text = (self.output_text + delta)[:640]

    def request_signoff(self):
        self.signoff.requested = True

    async def send(self, pcm, *, observe_seconds=None):
        """Keep silence flowing until a bounded observation or reply ending."""
        started = time.monotonic()
        count = 0
        while time.monotonic() < min(started + len(pcm) / (2 * RATE) + 12,
                                     self.session.media_deadline() - 1):
            due = self.next_input_at if self.next_input_at is not None else started
            await asyncio.sleep(max(0., due - time.monotonic()))
            if time.monotonic() - due > .1:
                raise RuntimeError("Generated conversation input missed its pacing bound")
            if self.session.metrics.error:
                raise RuntimeError("Generated conversation session failed")
            packet = pcm[count * 640:(count + 1) * 640].ljust(640, b"\0")
            await self.session.send_audio(packet)
            self.next_input_at = due + .02
            count += 1
            input_done = count * 640 >= len(pcm)
            now = time.monotonic()
            if observe_seconds is not None:
                if now - started >= len(pcm) / (2 * RATE) + observe_seconds:
                    return
            elif input_done and self.last_voice_at is not None and (
                    now - self.last_audio_at >= .8 or now - self.last_voice_at >= 1.2):
                return
        raise TimeoutError("Generated conversation did not complete within its observation limit")

    async def wait_for_close(self, policy):
        started = time.monotonic()
        policy.start_listening(started)
        while not policy.should_close(time.monotonic()):
            await self.send(b"", observe_seconds=.02)
        return round((time.monotonic() - started) * 1000, 1)

    def next_turn(self):
        self.signoff.finish()
        self.transcript.finish()
        self.output_text = ""
        self.last_audio_at = self.last_voice_at = None

    async def close(self):
        try:
            metrics = await self.session.close()
            return {"finalization_confirmed": metrics.finalization_confirmed,
                    "usage_seconds": metrics.usage_seconds, "close_reason": metrics.close_reason,
                    "error": metrics.error}
        finally:
            self.output_text = ""
            self.signoff.finish()
            self.transcript.finish()


async def check_sessions(settings, voice, personality, stage):
    """Exercise two sessions without microphone capture or speaker playback."""
    fixtures = [(await synthesize_fixture(prompt))[0] for prompt in SESSION_PROMPTS]
    report = {"scope": "generated speech and provider transcript events; no acoustic or room microphone check",
              "checks": {}, "sessions": []}
    checks = report["checks"]
    memory = StandbyContext()
    instructions = STANDBY_INSTRUCTIONS + " " + PERSONALITIES[personality]
    owner = None
    try:
        stage("checking a generated question and inactivity close")
        owner = DigitalConversation(settings, voice, instructions)
        await owner.session.start()
        await owner.send(fixtures[0])
        exchange = owner.transcript.finish()
        checks["first_answer"] = "paris" in re.findall(r"[a-z]+", owner.output_text.casefold())
        checks["complete_exchange_available"] = exchange is not None
        if not all(checks.values()):
            return report
        idle = ConversationPolicy()
        report["inactivity_wait_ms"] = await owner.wait_for_close(idle)
        metrics = await owner.close()
        report["sessions"].append(metrics)
        owner = None
        if metrics["error"] or not metrics["finalization_confirmed"]:
            return report
        memory.save((exchange,))

        stage("checking context in a new generated conversation")
        prior = memory.take()
        report["resumed_exchanges"] = len(prior)
        owner = DigitalConversation(settings, voice, instructions, prior)
        await owner.session.start()
        await owner.send(fixtures[1])
        checks["resumed_answer"] = "seine" in re.findall(r"[a-z]+", owner.output_text.casefold())
        owner.next_turn()
        stage("checking ambiguous thanks")
        await owner.send(fixtures[2], observe_seconds=3)
        words = re.findall(r"[a-z]+", owner.signoff.text.casefold())
        checks["thanks_recognized"] = words in (["thanks"], ["thank", "you"])
        checks["thanks_stays_open"] = not owner.signoff.finish()
        owner.next_turn()

        stage("checking a clear sign-off and the two-second grace")
        await owner.send(fixtures[3])
        checks["clear_signoff"] = owner.signoff.finish()
        policy = ConversationPolicy()
        policy.request_end(clear_closing_intent=checks["clear_signoff"])
        report["signoff_wait_ms"] = await owner.wait_for_close(policy)
    except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
        report["failure"] = str(exc) or type(exc).__name__
    finally:
        if owner is not None:
            report["sessions"].append(await owner.close())
        memory.clear()
    return report
