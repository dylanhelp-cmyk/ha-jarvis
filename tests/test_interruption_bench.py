import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from jarvis.interruption_bench import check_interruption, INTERRUPTING_QUESTION
from jarvis.session_bench import INTERRUPTION_PROMPT


class InterruptionCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_recovery_reuses_one_connection_and_excludes_speech_from_results(self):
        now = 100.
        owners, sent = [], []

        async def sleep(seconds):
            nonlocal now
            now += seconds

        async def fixture(prompt):
            marker = 1 if prompt == INTERRUPTION_PROMPT else 2
            self.assertIn(prompt, (INTERRUPTION_PROMPT, INTERRUPTING_QUESTION))
            return np.full(320, marker, dtype="<i2").tobytes(), 320

        class Provider:
            assist_work_pending = False

            def __init__(self, _, **callbacks):
                owners.append(self)
                self.callbacks = callbacks
                self.seen = set()
                self.metrics = SimpleNamespace(error=None, discarded_output_samples=640,
                    finalization_confirmed=True, usage_seconds=5, close_reason="close_requested")

            async def start(self):
                pass

            def media_deadline(self):
                return 150.

            async def send_audio(self, pcm):
                sent.append(now)
                marker = int.from_bytes(pcm[:2], "little")
                if marker not in self.seen and (marker or 2 in self.seen):
                    self.seen.add(marker)
                    if marker == 2:
                        self.callbacks["on_input_transcript"](INTERRUPTING_QUESTION)
                    else:
                        self.callbacks["on_transcript"]("One." if marker == 1 else answer)
                        await self.callbacks["on_audio"](np.full(320, 1000, dtype="<i2").tobytes(), 16000)

            async def close(self):
                return self.metrics

        clock = SimpleNamespace(monotonic=lambda: now)
        with (patch("jarvis.session_bench.LiveSession", Provider),
              patch("jarvis.interruption_bench.synthesize_fixture", side_effect=fixture),
              patch("jarvis.interruption_bench.time", clock),
              patch("jarvis.session_bench.time", clock),
              patch("jarvis.session_bench.asyncio.sleep", side_effect=sleep)):
            for answer in ("Paris.", "Paris. Three, four.", "Paris. Paris.", "Three, four."):
                with self.subTest(answer=answer):
                    sent.clear()
                    owners.clear()
                    now = 100.
                    report = await check_interruption(None, "marin", "classic", lambda _: None)
                    self.assertEqual(len(owners), 1)
                    self.assertEqual(owners[0].seen, {0, 1, 2})
                    self.assertEqual(report["checks"]["count_stayed_stopped"], answer == "Paris.")
                    self.assertEqual(all(report["checks"].values()), answer == "Paris.")
                    self.assertTrue(report["sessions"][0]["finalization_confirmed"])
                    self.assertTrue(all(after - before >= .0199 for before, after in zip(sent, sent[1:])))
                    self.assertNotIn("Paris", repr(report))
