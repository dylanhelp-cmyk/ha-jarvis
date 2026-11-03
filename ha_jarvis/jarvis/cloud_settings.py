"""Local credential storage; saving a key never opens a paid session."""
import json
from pathlib import Path


class CloudSettings:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "openai-private.json"
        self.api_key = ""
        self.spent_usd = 0.0
        self.budget_usd = 5.0
        if self.path.exists():
            value = json.loads(self.path.read_text())
            self.api_key = value.get("api_key", "")
            self.spent_usd = float(value.get("spent_usd", 0))

    def public(self):
        return {"api_key_configured": bool(self.api_key), "budget_usd": self.budget_usd,
                "spent_usd": self.spent_usd, "enabled": False,
                "reason": "Credential preparation only; this revision cannot open cloud sessions"}

    def set_key(self, key):
        if not isinstance(key, str) or not 20 <= len(key) <= 512 or not key.isascii() or any(c.isspace() for c in key):
            raise ValueError("Enter a valid API key locally; its value is never returned")
        temporary = self.path.with_suffix(".tmp")
        temporary.touch(mode=0o600, exist_ok=True)
        temporary.chmod(0o600)
        temporary.write_text(json.dumps({"api_key": key, "spent_usd": self.spent_usd}))
        temporary.replace(self.path)
        self.path.chmod(0o600)
        self.api_key = key
