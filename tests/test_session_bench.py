import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from jarvis.session_bench import SESSION_PROMPTS, check_sessions
from jarvis.assist_sample import SampleLight, check_assist_sample, spoken_state
from jarvis.live_bench import SAMPLE_LIGHT_NAME


class SessionCheckTests(unittest.IsolatedAsyncioTestCase):
    def test_grade_only_affirmative_spoken_sample_state(self):
        for reply in ("It's now on, sir.", "The reading lamp is switched on.", "I've turned the reading lamp on."):
            self.assertEqual(spoken_state(reply), "on")
        for reply in ("Is it on?", "I cannot turn it on.", "If it's on", "Turn on the reading lamp.",
                      "The desk lamp is on.", "It was on.", "It was off, now it's on.", "Maybe on."):
            self.assertNotIn(spoken_state(reply), ("on", "off"))
        self.assertEqual(spoken_state(""), "missing_transcript")
    async def test_unknown_spoken_target_does_not_change_sample(self):
        lamp = SampleLight()
        result = await lamp.call_tool("1", "HassTurnOn", {"name": "Different Lamp"})
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual((lamp.state, lamp.writes, lamp.rejected_targets), ("off", 0, 1))

    async def test_sample_model_must_read_changed_state_before_speaking(self):
        import json
        from jarvis.live_bench import ASSIST_SAMPLE_PROMPTS
        from jarvis.live_session import LiveSession

        class Provider:
            assist_work_pending = False
            assist_result_at = None
            _assist_result_words = False
            assist_reply_pending = LiveSession.assist_reply_pending
            def __init__(self, _settings, **callbacks):
                self.callbacks = callbacks
                self.delayed_reply = None
                self.metrics = SimpleNamespace(error=None, finalization_confirmed=True, usage_seconds=10,
                                               close_reason="close_requested")
            async def start(self):
                pass
            async def close(self):
                return self.metrics
            def begin_turn(self):
                self.assist_result_at = None
            def media_deadline(self):
                return now + 50
            async def send_audio(self, packet):
                marker = int.from_bytes(packet[:2], "little")
                if not marker:
                    if self.delayed_reply is not None and now >= self.assist_result_at + 1.6:
                        self.callbacks["on_transcript"](self.delayed_reply)
                        self._assist_result_words = True
                        self.delayed_reply = None
                        await self.callbacks["on_audio"](np.full(320, 1000, dtype="<i2").tobytes(), 16000)
                    return
                self.callbacks["on_input_transcript"](ASSIST_SAMPLE_PROMPTS[marker - 1])
                bridge = self.callbacks["assist_delegation"]
                if marker == 1 or fresh:
                    name = "HassTurnOn" if marker == 1 else "GetLiveContext"
                    self.callbacks["on_transcript"](early_text if marker == 1 else "One moment. ")
                    if marker != 1 or early_voice:
                        await self.callbacks["on_audio"](np.full(320, 1000, dtype="<i2").tobytes(), 16000)
                    now_before_lookup = now
                    await sleep(.05)
                    await bridge.execute(str(marker), name, json.dumps({"name": SAMPLE_LIGHT_NAME}))
                    self.assist_result_at = now_before_lookup + .051
                    self._assist_result_words = False
                    self.delayed_reply = first_reply if marker == 1 else "Off."
                    return
                self.callbacks["on_transcript"]("Off.")
                await self.callbacks["on_audio"](np.full(320, 1000, dtype="<i2").tobytes(), 16000)

        async def fixture(prompt):
            return np.full(320, ASSIST_SAMPLE_PROMPTS.index(prompt) + 1, dtype="<i2").tobytes(), 320
        async def sleep(seconds):
            nonlocal now
            now += seconds
        now = 100.
        clock = SimpleNamespace(monotonic=lambda: now)
        with (patch("jarvis.session_bench.LiveSession", Provider),
              patch("jarvis.assist_sample.synthesize_fixture", side_effect=fixture),
              patch("jarvis.assist_sample.time", clock), patch("jarvis.session_bench.time", clock),
              patch("jarvis.session_bench.asyncio.sleep", side_effect=sleep)):
            for fresh, first_reply, early_text, early_voice in (
                (True, "On.", "One moment. ", True),
                (False, "On.", "One moment. ", True),
                (True, "Checking.", "One moment. ", True),
                (True, "On.", "On. ", False),
                (True, "On.", "On. ", True),
            ):
                report = await check_assist_sample(SimpleNamespace(spent_usd=0), "gpt-6-luna", "marin", lambda _: None)
                self.assertTrue(report["checks"]["verified_change"])
                early_claim = early_text == "On. " and early_voice
                self.assertEqual(all(report["checks"].values()), fresh and first_reply == "On." and not early_claim)
                self.assertEqual(report["checks"]["spoken_off"], fresh)
                self.assertEqual(report["tool_calls"]["reads"], int(fresh))
                self.assertEqual(report["recognized_fixture_target"], [True, True])
                self.assertNotIn(SAMPLE_LIGHT_NAME, repr(report))

    async def test_real_time_generated_input_resumes_context_and_settles_both_sessions(self):
        clock = 100.
        owners = []
        sent_at = []
        deadlines = {}

        async def sleep(seconds):
            nonlocal clock
            clock += seconds

        async def fixture(prompt):
            marker = SESSION_PROMPTS.index(prompt) + 1
            return np.full(320, marker, dtype="<i2").tobytes(), 320

        class Provider:
            assist_work_pending = False

            def __init__(self, _settings, **callbacks):
                owners.append(callbacks)
                self.callbacks = callbacks
                self.metrics = SimpleNamespace(error=None, finalization_confirmed=True,
                                               usage_seconds=24, close_reason="close_requested")
                deadlines[id(self)] = clock + 50
                self.seen = set()

            async def start(self):
                pass

            def media_deadline(self):
                return deadlines[id(self)]

            async def send_audio(self, pcm):
                marker = int.from_bytes(pcm[:2], "little")
                sent_at.append((id(self), clock))
                if not marker or marker in self.seen:
                    return
                self.seen.add(marker)
                question = SESSION_PROMPTS[marker - 1]
                answer = ("Paris.", "The Seine.", "You're welcome.", "Goodbye.")[marker - 1]
                self.callbacks["on_input_transcript"](question)
                collect = self.callbacks["on_timed_transcript"]
                collect("user", question, marker * 1000, marker * 1000 + 300)
                collect("assistant", answer, marker * 1000 + 500, marker * 1000 + 800)
                self.callbacks["on_transcript"](answer)
                await self.callbacks["on_audio"](np.full(320, 1000, dtype="<i2").tobytes(), 16000)
                if marker == 4:
                    self.callbacks["on_signoff_request"]()

            async def close(self):
                return self.metrics

        with (patch("jarvis.session_bench.LiveSession", Provider),
              patch("jarvis.session_bench.synthesize_fixture", side_effect=fixture),
              patch("jarvis.session_bench.time", SimpleNamespace(monotonic=lambda: clock)),
              patch("jarvis.session_bench.asyncio.sleep", side_effect=sleep)):
            result = await check_sessions(None, "marin", "classic", lambda _: None)

        self.assertEqual(len(result["sessions"]), 2)
        self.assertEqual(len(result["checks"]), 6)
        self.assertTrue(all(result["checks"].values()))
        self.assertGreaterEqual(result["inactivity_wait_ms"], 10_000)
        self.assertLess(result["inactivity_wait_ms"], 10_100)
        self.assertGreaterEqual(result["signoff_wait_ms"], 2000)
        self.assertEqual(owners[1]["prior_exchanges"], ((SESSION_PROMPTS[0], "Paris."),))
        for (before_owner, before), (after_owner, after) in zip(sent_at, sent_at[1:]):
            if before_owner == after_owner:
                self.assertGreaterEqual(after - before, .0199)
        self.assertNotIn("Paris.", repr(result))
        self.assertNotIn("What is the capital", repr(result))
