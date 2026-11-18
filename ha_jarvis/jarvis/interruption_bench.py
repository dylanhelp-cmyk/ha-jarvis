"""One generated-speech check of cloud interruption and recovery, without devices."""
import time

from .conversation_trial import PERSONALITIES
from .live_bench import synthesize_fixture
from .session_bench import DigitalConversation, INTERRUPTION_PROMPT, SESSION_PROMPTS


async def check_interruption(settings, voice, personality, stage):
    fixtures = [(await synthesize_fixture(prompt))[0]
                for prompt in (INTERRUPTION_PROMPT, SESSION_PROMPTS[0])]
    report = {"scope": "generated input and provider audio only; no microphone or speaker playback",
              "checks": {}, "sessions": []}
    owner = DigitalConversation(settings, voice,
        "Listen for spoken requests. When asked to count, count slowly up to the requested number. "
        "Otherwise answer in one short sentence. Do not greet or delegate work. " + PERSONALITIES[personality])
    try:
        stage("starting generated speech before the interruption")
        await owner.session.start()
        await owner.send(fixtures[0], until_first_voice=True)
        report["checks"]["first_speech_started"] = owner.last_voice_at is not None

        stage("interrupting the cloud mixer while local output stays muted")
        control = await owner.session.interrupt_speech()
        report["control"] = control
        report["checks"]["interruption_completed"] = control["quiet_observed_ms"] >= 250
        # The command acknowledgment alone never releases model audio.
        owner.next_turn()
        owner.session.resume_after_interrupt(control["generation"])
        owner.next_input_at = time.monotonic()
        await owner.send(b"", observe_seconds=.4)
        report["checks"]["no_speech_without_new_input"] = owner.last_voice_at is None
        if owner.last_voice_at is not None:
            return report

        stage("checking the next generated question on the same connection")
        owner.next_turn()
        await owner.send(fixtures[1])
        report["checks"]["next_answer"] = "paris" in owner.output_text.casefold()
        report["discarded_cloud_output_ms"] = round(owner.session.metrics.discarded_output_samples / 16, 1)
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        report["failure"] = type(exc).__name__
    finally:
        report["sessions"].append(await owner.close())
    return report
