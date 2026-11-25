"""Exercise the configured Responses backend using generated speech and one in-memory light."""
import asyncio
import json
import re
import time

from .assist_delegation import AssistDelegation
from .audio import RATE
from .conversation_trial import ASSIST_STANDBY_INSTRUCTIONS
from .live_bench import ASSIST_CANCEL_PROMPTS, ASSIST_SAMPLE_PROMPTS, SAMPLE_LIGHT_NAME, synthesize_fixture
from .session_bench import DigitalConversation


STATE_CHECKS = ("verified_change", "spoken_on", "fresh_read", "spoken_off")
CANCELLATION_CHECKS = ("pending_at_cancellation", "readback_cancelled", "action_not_retried", "spoken_uncertain_outcome")


class SampleLight:
    def __init__(self, *, hold_readback=False):
        self.state = "off"
        self.writes = self.readbacks = self.reads = 0
        self.rejected_targets = 0
        self.last_rejection = None
        self.last_lookup_at = None
        self.hold_readback = hold_readback
        self.readback_waiting = False
        self.readback_cancelled = False

    async def call_tool(self, call_id, name, arguments):
        name_arg = arguments.get("name")
        problem = None
        if not isinstance(name_arg, str) or not name_arg:
            problem = "missing_name"
        elif name_arg.casefold() != SAMPLE_LIGHT_NAME.casefold():
            problem = "different_name"
        elif arguments.get("domain") not in (None, "light", ["light"]):
            problem = "different_domain"
        elif set(arguments) - {"name", "domain"}:
            problem = "unexpected_filter"
        if problem:
            self.rejected_targets += 1
            self.last_rejection = problem
            return {"outcome": "failed", "reason": "The named sample device was not found"}
        if name == "HassTurnOn":
            self.writes += 1
            self.state = "on"
            payload = {"response_type": "action_done", "data": {"failed": [], "success": [{
                "type": "entity", "id": "light.reading_lamp", "name": SAMPLE_LIGHT_NAME}]}}
        elif name == "GetLiveContext":
            verification = call_id.startswith("verify:")
            if verification and self.hold_readback:
                self.readback_waiting = True
                try:
                    await asyncio.sleep(8)
                except asyncio.CancelledError:
                    self.readback_cancelled = True
                    raise
                finally:
                    self.readback_waiting = False
            self.last_lookup_at = time.monotonic()
            if verification:
                self.readbacks += 1
            else:
                self.reads += 1
            payload = {"result": f"Live Context: sample\n- names: {SAMPLE_LIGHT_NAME}\n  domain: light\n  state: '{self.state}'"}
        else:
            return {"outcome": "rejected", "reason": "Unknown sample tool"}
        return {"outcome": "reported", "text": json.dumps(payload)}


def spoken_state(text):
    """Recognize an affirmative answer about this fixture. Leave other wording ungraded."""
    if not text.strip():
        return "missing_transcript"
    if "?" in text or len(text) >= 640:
        return "unclear"
    words = " ".join(re.findall(r"[a-z']+", text.casefold().replace("’", "'")))
    states = set(re.findall(r"\b(?:on|off)\b", words))
    if len(states) != 1:
        return "both_states" if states else "no_state_words"
    state = states.pop()
    target = rf"(?:(?:the |your )?{re.escape(SAMPLE_LIGHT_NAME.casefold())}|the lamp|it)"
    subject = rf"(?:{target}(?: is|'s) )?"
    predicate = rf"(?:now |currently )?(?:(?:switched|turned) )?{state}"
    action = rf"(?:i've |i have |i )(?:switched|turned) (?:{target} {state}|{state} {target})"
    phrase = rf"(?:(?:done|confirmed|certainly|yes|sir) )?(?:{subject}{predicate}|{action})(?: sir)?"
    return state if re.fullmatch(phrase, words) else "unclear"


async def cancel_during_readback(owner, lamp, fixtures, report, phase):
    request, cancellation = (pcm for pcm, _ in fixtures)
    checks = report["checks"]
    phase("waiting for verification of the sample light")
    await owner.send(request, observe_seconds=0)
    deadline = time.monotonic() + 10
    while not lamp.readback_waiting and time.monotonic() < deadline:
        await owner.send(b"", observe_seconds=.04)
    checks["pending_at_cancellation"] = lamp.readback_waiting
    if not lamp.readback_waiting:
        return
    phase("sending generated cancellation during verification")
    try:
        await owner.send(cancellation)
    finally:
        bridge = owner.session.assist_delegation
        checks["readback_cancelled"] = lamp.readback_cancelled and bridge.activity["spoken_cancellations"] == 1
        checks["action_not_retried"] = lamp.writes == 1 and lamp.readbacks == 0 and bool(bridge.uncertain_actions)
        heard = owner.signoff.text.casefold()
        report["recognized_cancel_phrase"] = bool(re.search(r"\bcancel\s+that\b", heard))
        offset = len(owner.output_text) if owner.result_text_offset is None else owner.result_text_offset
        reply = owner.output_text[offset:].casefold().replace("’", "'")
        uncertainty = re.search(
            r"\bunconfirmed\b|\b(?:can't|cannot|couldn't|could not|unable to) confirm\b"
            r"|\b(?:may|might) (?:have|already)\b|\bnot (?:sure|certain)\b", reply)
        checks["spoken_uncertain_outcome"] = bool(owner.first_result_voice_at is not None and uncertainty)


async def check_assist_sample(settings, model, voice, phase, *, cancel_pending=False):
    lamp = SampleLight(hold_readback=cancel_pending)
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "domain": {"type": "string"}},
              "required": ["name"], "additionalProperties": False}
    catalog = [{"name": "HassTurnOn", "description": "Turn on a named device.", "inputSchema": schema},
               {"name": "GetLiveContext", "description": "Read the current state of a named device.", "inputSchema": schema}]
    bridge = AssistDelegation(lamp, catalog, model=model, uncertain_actions=set())
    prompts = ASSIST_CANCEL_PROMPTS if cancel_pending else ASSIST_SAMPLE_PROMPTS
    fixtures = [await synthesize_fixture(prompt) for prompt in prompts]
    owner = DigitalConversation(settings, voice, ASSIST_STANDBY_INSTRUCTIONS, assist_delegation=bridge)
    checks, timings = {}, []
    # A reported tool result alone does not prove that the caller got the right answer.
    report = {"checks": checks, "generated_speech_end_to_returned_voice_ms": timings, "model": model,
              "recognized_fixture_target": []}
    before_spend = settings.spent_usd
    try:
        await owner.session.start()
        if cancel_pending:
            await cancel_during_readback(owner, lamp, fixtures, report, phase)
        else:
            await check_sample_turns(owner, lamp, fixtures, report, phase)
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        report["failure"] = owner.session.metrics.error or type(exc).__name__
    finally:
        report["session"] = await owner.close()
        if report["session"]["finalization_confirmed"]:
            report["ledger_cost_usd"] = round(settings.spent_usd - before_spend, 6)
        report["tool_calls"] = {"writes": lamp.writes, "readbacks": lamp.readbacks, "reads": lamp.reads}
        report["assist_activity"] = dict(bridge.activity)
        if lamp.rejected_targets:
            report["assist_activity"]["rejected_sample_target"] = lamp.rejected_targets
            report["sample_rejection"] = lamp.last_rejection
    return report


async def check_sample_turns(owner, lamp, fixtures, report, phase):
    checks = report["checks"]
    timings = report["generated_speech_end_to_returned_voice_ms"]
    for (pcm, voiced_end), expected in zip(fixtures, ("on", "off")):
        phase("checking a sample light command" if expected == "on" else "checking a fresh sample light state")
        owner.next_turn()
        owner.session.begin_turn()
        before = lamp.reads
        # An external change between requests makes a cached or guessed read detectable.
        lamp.state = "off"
        lamp.last_lookup_at = None
        end_of_input = (owner.next_input_at or time.monotonic()) + voiced_end / RATE
        await owner.send(pcm)
        report["recognized_fixture_target"].append(
            bool(re.search(r"\b" + re.escape(SAMPLE_LIGHT_NAME) + r"\b", owner.signoff.text, re.IGNORECASE)))
        first = owner.first_voice_at
        timings.append(round(1000 * (first - end_of_input)) if first is not None and first >= end_of_input else None)
        if expected == "on":
            checks["verified_change"] = lamp.state == "on" and lamp.writes == 1 and lamp.readbacks == 1
        else:
            checks["fresh_read"] = lamp.state == "off" and lamp.writes == 1 and lamp.reads > before
        voice_after_lookup = (owner.first_result_voice_at is not None and lamp.last_lookup_at is not None
                              and owner.first_result_voice_at >= lamp.last_lookup_at)
        offset = len(owner.output_text) if owner.result_text_offset is None else owner.result_text_offset
        heard = spoken_state(owner.output_text[offset:])
        early_state = spoken_state(owner.output_text[:offset])
        voice_before_lookup = (first is not None and lamp.last_lookup_at is not None
                               and first < lamp.last_lookup_at)
        # Transcript deltas can describe audio the App discarded. Only a
        # state claim with forwarded voice before verification is suspect.
        early_confirmed_state_possible = voice_before_lookup and early_state in ("on", "off")
        checks["spoken_" + expected] = voice_after_lookup and heard == expected and not early_confirmed_state_possible
        report.setdefault("reply_evidence", []).append({
            "reported_state": heard,
            "voice_after_lookup": voice_after_lookup,
            "voice_before_lookup": voice_before_lookup,
            "transcript_before_result": early_state,
        })
        if expected == "on" and not checks["verified_change"]:
            break
