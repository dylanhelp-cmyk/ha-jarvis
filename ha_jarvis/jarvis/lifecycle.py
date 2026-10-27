"""Offline-testable conversation policy for the later voice milestone.

No model is called and no cloud session is opened by this module.
"""
from dataclasses import dataclass


@dataclass
class ConversationPolicy:
    idle_seconds: float = 10.0
    grace_seconds: float = 2.0
    last_activity: float = 0.0
    pending_work: int = 0
    audible_until: float = 0.0
    ending_requested: bool = False
    grace_started: float | None = None

    def user_speech(self, now: float, *, meaningful: bool = True) -> None:
        if meaningful:
            self.last_activity = now
            self.ending_requested = False
            self.grace_started = None

    def request_end(self, *, clear_closing_intent: bool) -> None:
        # A tool completion or an ambiguous thanks must never enter this branch.
        if clear_closing_intent:
            self.ending_requested = True

    def should_close(self, now: float) -> bool:
        if self.pending_work or now < self.audible_until:
            self.grace_started = None
            return False
        if self.ending_requested:
            if self.grace_started is None:
                self.grace_started = now
            return now - self.grace_started >= self.grace_seconds
        return now - max(self.last_activity, self.audible_until) >= self.idle_seconds
