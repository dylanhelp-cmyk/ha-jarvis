"""Check whether GPT-Live yields to a spoken question on its ordinary audio input."""
import re
import time

from .conversation_trial import DUPLEX_INSTRUCTIONS, INSTRUCTIONS, PERSONALITIES
from .live_bench import synthesize_fixture
from .session_bench import DigitalConversation, INTERRUPTION_PROMPT


INTERRUPTING_QUESTION = "Stop. What is the capital of France?"
COUNT = re.compile(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
                   r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|\d+)\b", re.I)


async def check_interruption(settings, voice, personality, stage, *, transport="websocket"):
    first, _ = await synthesize_fixture(INTERRUPTION_PROMPT)
    question, voiced_end = await synthesize_fixture(INTERRUPTING_QUESTION)
    checks = {}
    report = {"scope": "Generated caller speech and provider output on one connection. No room audio or speakers.",
              "transport": transport, "checks": checks, "sessions": []}
    owner = DigitalConversation(settings, voice,
        " ".join((INSTRUCTIONS, DUPLEX_INSTRUCTIONS, PERSONALITIES[personality])),
        allow_signoff=False, transport=transport)
    try:
        stage("waiting for GPT-Live to begin counting")
        await owner.session.start()
        await owner.send(first, until_first_voice=True)
        deadline = time.monotonic() + 2
        while not COUNT.search(owner.output_text) and time.monotonic() < deadline:
            await owner.send(b"", observe_seconds=.02)
        checks["interrupted_during_count"] = bool(
            COUNT.search(owner.output_text) and not re.search(r"\b(?:twenty|20)\b", owner.output_text, re.I)
            and owner.last_voice_at is not None and time.monotonic() - owner.last_voice_at <= .2)
        if not checks["interrupted_during_count"]:
            return report

        stage("speaking a new question over the count")
        owner.next_turn()
        await owner.send(question[:voiced_end * 2], observe_seconds=0)
        question_end = time.monotonic()
        count_at_end = len(COUNT.findall(owner.output_text))
        await owner.send(b"", observe_seconds=5)
        heard = set(re.findall(r"[a-z]+", owner.signoff.text.casefold()))
        checks["replacement_question_heard"] = {"capital", "france"} <= heard
        answers = list(re.finditer(r"\bparis\b", owner.output_text, re.I))
        checks["replacement_answer_once"] = (len(answers) == 1
            and owner.last_voice_at is not None and owner.last_voice_at >= question_end)
        checks["count_stayed_stopped"] = (checks["replacement_answer_once"]
            and len(COUNT.findall(owner.output_text)) == count_at_end
            and not COUNT.search(owner.output_text, answers[0].end())
            and not re.search(r"\b(?:twenty|20)\b", owner.output_text, re.I)
            and time.monotonic() - owner.last_voice_at >= 1.2)
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        report["failure"] = type(exc).__name__
    finally:
        report["sessions"].append(await owner.close())
    return report
