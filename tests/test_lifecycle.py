import asyncio
import unittest
from unittest.mock import patch
from jarvis.lifecycle import ConversationPolicy, ExchangeTranscript, SignOffRequest, StandbyContext


class LifecycleTests(unittest.TestCase):
    def test_followups_reset_idle_without_waiting(self):
        state = ConversationPolicy(last_activity=0)
        state.user_speech(9)
        self.assertFalse(state.should_close(10))
        self.assertTrue(state.should_close(19))

    def test_signoff_grace_is_cancelled_by_user(self):
        state = ConversationPolicy()
        state.request_end(clear_closing_intent=True)
        self.assertFalse(state.should_close(4))
        state.user_speech(5)
        self.assertFalse(state.should_close(6))

    def test_pending_work_and_actual_playback_prevent_close(self):
        state = ConversationPolicy(pending_work=1, audible_until=50)
        state.request_end(clear_closing_intent=True)
        self.assertFalse(state.should_close(100))
        state.pending_work = 0
        self.assertFalse(state.should_close(49))
        self.assertFalse(state.should_close(50))
        self.assertTrue(state.should_close(52))

    def test_noise_and_ambiguous_thanks_do_not_change_policy(self):
        state = ConversationPolicy()
        state.user_speech(9, meaningful=False)
        state.request_end(clear_closing_intent=False)
        self.assertFalse(state.ending_requested)
        self.assertTrue(state.should_close(10))

    def test_signoff_requires_a_model_request_and_a_clear_complete_phrase(self):
        request = SignOffRequest()
        for pieces, model_requested, expected in (
            (("Thanks, that’s", " all for now."), True, True),
            (("Goodbye.",), False, False),
            (("Thanks.",), True, False),
            (("That's all", " for the lights. What's the weather?"), True, False),
            (("Stop listening.", " " * 170), True, False),
            (("Jarvis, goodbye.",), True, True),
        ):
            with self.subTest(pieces=pieces, model_requested=model_requested):
                request.requested = model_requested
                for fragment in pieces:
                    request.append(fragment)
                self.assertEqual(request.finish(), expected)
                self.assertEqual(request.text, "")
                self.assertFalse(request.requested)


    def test_exchange_skips_stale_incomplete_and_oversized_transcript_fragments(self):
        transcript = ExchangeTranscript()
        transcript.append("user", "Which", 50, 500)
        transcript.append("assistant", "The Seine.", 1500, 1900)
        transcript.append("user", " river?", 510, 750)
        self.assertEqual(transcript.finish(), ("Which river?", "The Seine."))
        transcript.append("assistant", "Late fragment", 1800, 1910)
        transcript.append("user", "How long is it?", 2200, 2500)
        self.assertIsNone(transcript.finish())
        transcript.append("user", "How long is it?", 2700, 3000)
        transcript.append("assistant", "a" * 641, 3500, 3700)
        transcript.append("assistant", "Too late", 3710, 3730)
        self.assertIsNone(transcript.finish())


class StandbyContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_is_bounded_consumable_and_expires_without_a_wake(self):
        history = StandbyContext()
        pairs = (("First", "One"), ("Second", "Two"), ("Third", "Three"))
        history.save(pairs)
        self.assertEqual(history.take(), pairs[-2:])
        self.assertEqual(history.take(), ())
        with patch("jarvis.lifecycle.CONTEXT_SECONDS", 0):
            history.save(pairs)
            self.assertEqual(history.take(), ())
            history.save(pairs)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(history._exchanges, ())


if __name__ == "__main__": unittest.main()
