import unittest
from jarvis.lifecycle import ConversationPolicy


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


if __name__ == "__main__": unittest.main()
