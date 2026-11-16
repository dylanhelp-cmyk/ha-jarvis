"""Local rules for closing a conversation."""
from dataclasses import dataclass
import re

DEFAULT_IDLE_SECONDS = 10
SIGN_OFFS = frozenset({"bye", "goodbye", "good bye", "good night", "that's all", "that's all for now",
                       "that's it for now", "that will be all", "that'll be all", "we're done", "end the conversation",
                       "stop listening", "you can stop listening", "you can go now", "talk to you later"})


@dataclass
class SignOffRequest:
    """Corroborate a model closure request with the current input transcript."""

    text: str = ""
    requested: bool = False
    overflow: bool = False

    def append(self, delta: str) -> None:
        if self.overflow:
            return
        if len(self.text) + len(delta) > 160:
            self.text = ""
            self.overflow = True
        else:
            self.text += delta

    def finish(self) -> bool:
        words = " ".join(re.findall(r"[a-z0-9']+", self.text.casefold().replace("’", "'")))
        words = words.removeprefix("jarvis ").removesuffix(" jarvis")
        for courtesy in ("thanks", "thank you"):
            words = words.removeprefix(courtesy + " ").removesuffix(" " + courtesy)
        confirmed = self.requested and not self.overflow and words in SIGN_OFFS
        self.text = ""
        self.requested = self.overflow = False
        return confirmed


@dataclass
class ConversationPolicy:
    idle_seconds: float = DEFAULT_IDLE_SECONDS
    grace_seconds: float = 2.0
    last_activity: float = 0.0
    pending_work: int = 0
    audible_until: float = 0.0
    ending_requested: bool = False
    grace_started: float | None = None

    def start_listening(self, now: float) -> None:
        self.last_activity = now
        self.grace_started = None

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
