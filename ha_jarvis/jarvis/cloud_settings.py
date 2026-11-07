"""Local credential storage; saving a key never opens a paid session."""
import json
import math
import os
from pathlib import Path
import secrets


VOICE_USD_PER_SECOND = 0.05 / 60


class CloudBudgetError(ValueError):
    """A paid attempt cannot safely be started."""


class CloudSettings:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "openai-private.json"
        self.api_key = ""
        self.spent_usd = 0.0
        self.budget_usd = 5.0
        self.pending = None
        self.attempt_count = 0
        if self.path.exists():
            value = json.loads(self.path.read_text())
            self.api_key = value.get("api_key", "")
            self.spent_usd = float(value.get("spent_usd", 0))
            self.pending = value.get("pending")
            self.attempt_count = int(value.get("attempt_count", 0))
            if not math.isfinite(self.spent_usd) or self.spent_usd < 0:
                raise CloudBudgetError("Invalid saved cloud spending; paid calls are blocked")

    def public(self):
        return {"api_key_configured": bool(self.api_key), "budget_usd": self.budget_usd,
                "spent_usd": self.spent_usd, "enabled": False,
                "session_attempts": self.attempt_count,
                "reserved_usd": self.pending["reserved_usd"] if self.pending else 0.0,
                "paid_retry_blocked": self.pending is not None,
                "reason": "Cloud transport is not enabled by the application"}

    def set_key(self, key):
        if not isinstance(key, str) or not 20 <= len(key) <= 512 or not key.isascii() or any(c.isspace() for c in key):
            raise ValueError("Enter a valid API key locally; its value is never returned")
        self._reload_budget()
        previous = self.api_key
        self.api_key = key
        try:
            self._save()
        except Exception:
            self.api_key = previous
            raise

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.touch(mode=0o600, exist_ok=True)
        temporary.chmod(0o600)
        with temporary.open("w") as handle:
            json.dump({"api_key": self.api_key, "spent_usd": self.spent_usd,
                       "pending": self.pending, "attempt_count": self.attempt_count}, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)
        self.path.chmod(0o600)
        if os.name == "posix":
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def _reload_budget(self):
        if self.path.exists():
            current = json.loads(self.path.read_text())
            self.spent_usd = float(current.get("spent_usd", 0))
            self.pending = current.get("pending")
            self.attempt_count = int(current.get("attempt_count", 0))
        if not math.isfinite(self.spent_usd) or self.spent_usd < 0:
            raise CloudBudgetError("Invalid saved cloud spending; paid calls are blocked")

    def reserve_attempt(self, *, transport):
        """Reserve up to US$0.05 per attempt before any network operation.

        This is a single-App ledger. Never create parallel paid session owners.
        A process restart leaves pending attempts blocked until accounted for.
        """
        if transport not in ("webrtc", "websocket", "realtime"):
            raise ValueError("Unknown cloud transport")
        # Other objects constructed before the current attempt must also see its
        # durable reservation. This method is synchronous on the App event loop.
        if self.path.exists():
            current = json.loads(self.path.read_text())
            self.api_key = current.get("api_key", "")
        self._reload_budget()
        if not self.api_key:
            raise CloudBudgetError("Enter the API key locally before starting a cloud test")
        if self.pending is not None:
            raise CloudBudgetError("Previous cloud attempt has unconfirmed finalization; paid retries are blocked")
        reserved = 60 * VOICE_USD_PER_SECOND
        if self.spent_usd + reserved > self.budget_usd + 1e-12:
            raise CloudBudgetError("Cloud test would exceed the US$5 budget")
        self.pending = {"id": secrets.token_hex(12), "transport": transport,
                        "reserved_usd": reserved, "usage_seconds": 0.0,
                        "status": "reserved"}
        self.attempt_count += 1
        try:
            self._save()
        except Exception:
            self.pending = None
            self.attempt_count -= 1
            raise
        return self.pending["id"]

    def _attempt(self, attempt_id):
        if not self.pending or self.pending.get("id") != attempt_id:
            raise CloudBudgetError("Cloud attempt does not match the pending reservation")
        return self.pending

    def observe_usage(self, attempt_id, seconds):
        pending = self._attempt(attempt_id)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise CloudBudgetError("Invalid cloud usage duration")
        pending["usage_seconds"] = max(pending["usage_seconds"], float(seconds))
        pending["reserved_usd"] = max(pending["reserved_usd"], seconds * VOICE_USD_PER_SECOND)
        self._save()

    def finalize(self, attempt_id, seconds):
        self.observe_usage(attempt_id, seconds)
        pending = self._attempt(attempt_id)
        # Initialization is a floor/credit, never another 15 seconds added.
        billed = max(pending["usage_seconds"], 15 if pending["transport"] == "webrtc" else 0)
        self._settle(billed * VOICE_USD_PER_SECOND)

    def finalize_realtime(self, attempt_id, estimated_usd):
        pending = self._attempt(attempt_id)
        if pending["transport"] != "realtime":
            raise CloudBudgetError("Only Realtime attempts use token billing")
        if estimated_usd is not None and (not math.isfinite(estimated_usd) or estimated_usd < 0):
            raise CloudBudgetError("Invalid Realtime cost estimate")
        # If response usage is incomplete, debit the entire reservation.
        self._settle(pending["reserved_usd"] if estimated_usd is None else estimated_usd)

    def _settle(self, amount):
        old_spent, old_pending = self.spent_usd, self.pending
        self.spent_usd += amount
        self.pending = None
        try:
            self._save()
        except Exception:
            self.spent_usd, self.pending = old_spent, old_pending
            raise

    def mark_unconfirmed(self, attempt_id):
        self._attempt(attempt_id)["status"] = "unconfirmed"
        self._save()
