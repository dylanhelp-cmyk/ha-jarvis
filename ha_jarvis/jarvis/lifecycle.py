"""Local rules for ending and resuming conversations."""
import asyncio
from dataclasses import dataclass
import re

DEFAULT_IDLE_SECONDS = 10
CONTEXT_SECONDS = 60
CONTEXT_EXCHANGES = 2
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

    def clear_closing_phrase(self) -> bool:
        words = " ".join(re.findall(r"[a-z0-9']+", self.text.casefold().replace("’", "'")))
        words = words.removeprefix("jarvis ").removesuffix(" jarvis")
        for courtesy in ("thanks", "thank you"):
            words = words.removeprefix(courtesy + " ").removesuffix(" " + courtesy)
        return not self.overflow and words in SIGN_OFFS

    def finish(self) -> bool:
        confirmed = self.requested and self.clear_closing_phrase()
        self.text = ""
        self.requested = self.overflow = False
        return confirmed


class StandbyContext:
    """Keep only the previous session's recent exchanges until the next wake."""

    def __init__(self):
        self._exchanges = ()
        self._expiry = None

    def save(self, exchanges):
        self.clear()
        if exchanges:
            self._exchanges = tuple(exchanges)[-CONTEXT_EXCHANGES:]
            self._expiry = asyncio.get_running_loop().call_later(CONTEXT_SECONDS, self.clear)

    def take(self):
        fresh = self._expiry is not None and asyncio.get_running_loop().time() < self._expiry.when()
        exchanges = self._exchanges if fresh else ()
        self.clear()
        return exchanges

    def clear(self):
        self._exchanges = ()
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None


class ExchangeTranscript:
    """Pair bounded transcript fragments using their position in the Live session."""

    def __init__(self):
        self._previous_reply_end = 0
        self._parts = {}

    def append(self, speaker, delta, start_ms, end_ms):
        if start_ms < self._previous_reply_end:
            return
        text, first, last = self._parts.get(speaker, ("", start_ms, end_ms))
        limit = 160 if speaker == "user" else 640
        if text is not None:
            text = text + delta if len(text) + len(delta) <= limit else None
        self._parts[speaker] = (text, min(first, start_ms), max(last, end_ms))

    def finish(self):
        user, _, user_end = self._parts.get("user", (None, 0, 0))
        assistant, assistant_start, assistant_end = self._parts.get("assistant", (None, 0, 0))
        self._parts.clear()
        self._previous_reply_end = max(self._previous_reply_end, assistant_end)
        if user and assistant and user_end <= assistant_start and user.strip() and assistant.strip():
            return user.strip(), assistant.strip()
        return None


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
