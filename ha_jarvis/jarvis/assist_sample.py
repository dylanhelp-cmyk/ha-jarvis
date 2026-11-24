"""Exercise the configured Responses backend using generated speech and one in-memory light."""
import json
import re
import time

from .assist_delegation import AssistDelegation
from .audio import RATE
from .conversation_trial import ASSIST_STANDBY_INSTRUCTIONS
from .live_bench import ASSIST_SAMPLE_PROMPTS, synthesize_fixture
from .session_bench import DigitalConversation


class SampleLight:
    def __init__(self):
        self.state = "off"
        self.writes = self.readbacks = self.reads = 0
        self.last_lookup_at = None

    async def call_tool(self, call_id, name, arguments):
        if (str(arguments.get("name", "")).casefold() != "test lamp"
                or arguments.get("domain") not in (None, "light", ["light"])
                or set(arguments) - {"name", "domain"}):
            return {"outcome": "failed", "reason": "The named sample device was not found"}
        if name == "HassTurnOn":
            self.writes += 1
            self.state = "on"
            payload = {"response_type": "action_done", "data": {"failed": [], "success": [{
                "type": "entity", "id": "light.test_lamp", "name": "Test Lamp"}]}}
        elif name == "GetLiveContext":
            self.last_lookup_at = time.monotonic()
            if call_id.startswith("verify:"):
                self.readbacks += 1
            else:
                self.reads += 1
            payload = {"result": f"Live Context: sample\n- names: Test Lamp\n  domain: light\n  state: '{self.state}'"}
        else:
            return {"outcome": "rejected", "reason": "Unknown sample tool"}
        return {"outcome": "reported", "text": json.dumps(payload)}


def said_state(text, expected):
    words = " ".join(re.findall(r"[a-z']+", text.lower().replace("’", "'")))
    return words.removesuffix(" sir") in (expected, f"it is {expected}", f"it's {expected}",
                                          f"the test lamp is {expected}", f"test lamp is {expected}")


async def check_assist_sample(settings, model, voice, phase):
    lamp = SampleLight()
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "domain": {"type": "string"}},
              "required": ["name"], "additionalProperties": False}
    catalog = [{"name": "HassTurnOn", "description": "Turn on a named device.", "inputSchema": schema},
               {"name": "GetLiveContext", "description": "Read the current state of a named device.", "inputSchema": schema}]
    bridge = AssistDelegation(lamp, catalog, model=model, uncertain_actions=set())
    fixtures = [await synthesize_fixture(prompt) for prompt in ASSIST_SAMPLE_PROMPTS]
    owner = DigitalConversation(settings, voice, ASSIST_STANDBY_INSTRUCTIONS, assist_delegation=bridge)
    checks, timings = {}, []
    # A reported tool result alone does not prove that the caller got the right answer.
    report = {"checks": checks, "generated_speech_end_to_returned_voice_ms": timings, "model": model}
    before_spend = settings.spent_usd
    try:
        await owner.session.start()
        for (pcm, voiced_end), expected in zip(fixtures, ("on", "off")):
            phase("checking a sample light command" if expected == "on" else "checking a fresh sample light state")
            owner.next_turn()
            owner.session.begin_turn()
            before = lamp.reads
            # An external change between requests makes a cached or guessed read detectable.
            lamp.state = "off"
            end_of_input = (owner.next_input_at or time.monotonic()) + voiced_end / RATE
            await owner.send(pcm)
            first = owner.first_voice_at
            timings.append(round(1000 * (first - end_of_input)) if first is not None and first >= end_of_input else None)
            if expected == "on":
                checks["verified_change"] = lamp.state == "on" and lamp.writes == 1 and lamp.readbacks == 1
            else:
                checks["fresh_read"] = lamp.state == "off" and lamp.writes == 1 and lamp.reads > before
            checks["spoken_" + expected] = (first is not None and lamp.last_lookup_at is not None
                                            and first >= lamp.last_lookup_at and said_state(owner.output_text, expected))
            if not all(checks.values()):
                break
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        report["failure"] = owner.session.metrics.error or type(exc).__name__
    finally:
        report["session"] = await owner.close()
        if report["session"]["finalization_confirmed"]:
            report["ledger_cost_usd"] = round(settings.spent_usd - before_spend, 6)
        report["tool_calls"] = {"writes": lamp.writes, "readbacks": lamp.readbacks, "reads": lamp.reads}
    return report
