"""Local credential storage; saving a key never opens a paid session."""
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import secrets


VOICE_USD_PER_SECOND = 0.05 / 60
BACKEND_OUTPUT_TOKENS = 512
BACKEND_RESPONSE_LIMIT = 2
BACKEND_CONTEXT_TOKENS = 1_050_000
BACKEND_RATES = {"gpt-6-luna": (.10, .01, .50), "gpt-6-sol": (2, .20, 10)}


def _backend_cost(model, input_tokens, cached_tokens, output_tokens):
    input_rate, cached_rate, output_rate = BACKEND_RATES[model]
    if input_tokens > 272_000:
        input_rate *= 2
        cached_rate *= 2
        output_rate *= 1.5
    # Charge uncached input at the higher 12-hour cache-write rate and allow
    # for the regional uplift. The ledger is a conservative estimate.
    return 1.1 * ((input_tokens - cached_tokens) * input_rate * 2.5
                  + cached_tokens * cached_rate + output_tokens * output_rate) / 1_000_000


def _backend_allowance(model):
    return _backend_cost(model, BACKEND_CONTEXT_TOKENS, 0, BACKEND_OUTPUT_TOKENS)


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

    def reserve_attempt(self, *, transport, backend_model=None):
        """Reserve voice and optional backend usage before any network operation.

        This is a single-App ledger. Never create parallel paid session owners.
        A process restart leaves pending attempts blocked until accounted for.
        """
        if transport not in ("webrtc", "websocket", "realtime"):
            raise ValueError("Unknown cloud transport")
        if backend_model is not None and (transport != "websocket" or backend_model not in BACKEND_RATES):
            raise CloudBudgetError("Unsupported budgeted delegation configuration")
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
        if backend_model is not None:
            # Allow for one extra response that Live can start before the
            # client receives the event that triggers shutdown.
            reserved += (BACKEND_RESPONSE_LIMIT + 1) * _backend_allowance(backend_model)
        if self.spent_usd + reserved > self.budget_usd + 1e-12:
            raise CloudBudgetError("Cloud test would exceed the US$5 budget")
        self.pending = {"id": secrets.token_hex(12), "transport": transport,
                        "reserved_usd": reserved, "usage_seconds": 0.0,
                        "status": "reserved"}
        if backend_model is not None:
            self.pending["backend"] = {"model": backend_model, "responses": {}}
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
        backend = pending.get("backend")
        reserved_backend = (BACKEND_RESPONSE_LIMIT + 1) * _backend_allowance(backend["model"]) if backend else 0
        pending["reserved_usd"] = max(pending["reserved_usd"], reserved_backend + seconds * VOICE_USD_PER_SECOND)
        self._save()

    def observe_backend_response(self, attempt_id, response_id, *, usage=None, model=None):
        pending = self._attempt(attempt_id)
        backend = pending.get("backend")
        if backend is None or not isinstance(response_id, str) or not 0 < len(response_id) <= 128:
            raise CloudBudgetError("Invalid delegated response identity")
        if model is not None and model != backend["model"]:
            raise CloudBudgetError("Delegated response used an unbudgeted model")
        responses = backend["responses"]
        key = sha256(response_id.encode()).hexdigest()
        if usage is None:
            if key not in responses:
                responses[key] = None
                self._save()
            return len(responses)
        if not isinstance(usage, dict):
            raise CloudBudgetError("Invalid delegated token usage")
        details = usage.get("input_tokens_details") or {}
        if not isinstance(details, dict):
            raise CloudBudgetError("Invalid delegated token usage")
        tokens = (usage.get("input_tokens"), details.get("cached_tokens", 0), usage.get("output_tokens"))
        if (any(type(count) is not int or count < 0 for count in tokens)
                or tokens[1] > tokens[0] or tokens[0] > BACKEND_CONTEXT_TOKENS
                or tokens[2] > BACKEND_OUTPUT_TOKENS):
            raise CloudBudgetError("Invalid delegated token usage")
        record = list(tokens)
        if key in responses and responses[key] is not None:
            if responses[key] != record:
                raise CloudBudgetError("Conflicting delegated token usage")
        else:
            responses[key] = record
            self._save()
        return len(responses)

    def mark_backend_unconfirmed(self, attempt_id):
        backend = self._attempt(attempt_id).get("backend")
        if backend is not None:
            backend["unconfirmed"] = True
            self._save()

    def finalize(self, attempt_id, seconds):
        self.observe_usage(attempt_id, seconds)
        pending = self._attempt(attempt_id)
        # Initialization is a floor/credit, never another 15 seconds added.
        billed = max(pending["usage_seconds"], 15 if pending["transport"] == "webrtc" else 0)
        backend = pending.get("backend")
        backend_cost = 0
        if backend:
            model = backend["model"]
            allowance = _backend_allowance(model)
            responses = backend["responses"]
            backend_cost = sum(allowance if counts is None else _backend_cost(model, *counts)
                               for counts in responses.values())
            if backend.get("unconfirmed"):
                backend_cost = max(backend_cost, (BACKEND_RESPONSE_LIMIT + 1) * allowance)
        self._settle(billed * VOICE_USD_PER_SECOND + backend_cost)

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
