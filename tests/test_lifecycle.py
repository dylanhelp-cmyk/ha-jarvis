import unittest
from jarvis.lifecycle import ConversationPolicy, SignOffRequest


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


if __name__ == "__main__": unittest.main()
