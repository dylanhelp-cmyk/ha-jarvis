"""App-owned Assist functions for a GPT-Live Responses session."""
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re

import yaml

from .assist import MAX_ARGUMENT_BYTES, MAX_CALLS
from .cloud_settings import BACKEND_OUTPUT_TOKENS, BACKEND_RATES


POWER_STATE = {"HassTurnOn": "on", "HassTurnOff": "off"}
POWER_TOOL = re.compile(r"(?:assist__)?(?:intent__)?(HassTurnOn|HassTurnOff)")
POWER_DOMAINS = {"fan", "humidifier", "input_boolean", "light", "switch"}
UNVERIFIED = {"outcome": "unverified", "reason": "Assist replied but device state was not verified"}


def _power_target(text):
    """Resolve a single successful entity from an Assist intent response."""
    try:
        result = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(result, dict) or result.get("response_type") != "action_done":
        return None
    data = result.get("data")
    if not isinstance(data, dict) or data.get("failed") != []:
        return None
    targets = data.get("success")
    if not isinstance(targets, list) or len(targets) != 1:
        return None
    target = targets[0]
    if not isinstance(target, dict) or target.get("type") != "entity":
        return None
    identity, name = target.get("id"), target.get("name")
    if not isinstance(identity, str) or not isinstance(name, str) or not name.strip():
        return None
    match = re.fullmatch(r"([a-z_]+)[.][a-z0-9_]+", identity)
    if match and match[1] in POWER_DOMAINS:
        return match[1], name
    return None


def _live_context_state(text, domain, name):
    """Read the one row from a scoped, read-only Assist context query."""
    try:
        result = json.loads(text)
        payload = result.get("result") if isinstance(result, dict) else None
        if not isinstance(payload, str):
            return None
        header, _, body = payload.partition("\n")
        if not header.startswith("Live Context:"):
            return None
        rows = yaml.safe_load(body)
    except (TypeError, ValueError, yaml.YAMLError, RecursionError):
        return None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    names = row.get("names")
    if row.get("domain") != domain or not isinstance(names, str):
        return None
    if name.casefold() not in {names.casefold(), *(alias.casefold() for alias in names.split(", "))}:
        return None
    return row.get("state")


@dataclass
class ResponseBatch:
    delegation_id: str
    response_id: str
    calls: dict = field(default_factory=dict)


class AssistDelegation:
    def __init__(self, client, catalog, *, model="gpt-6-luna", preview_target=None):
        if model not in BACKEND_RATES:
            raise ValueError("Unsupported Assist reasoning model")
        self.client, self.model = client, model
        self.tools = {
            tool["name"]: {"type": "function", "name": tool["name"],
                           "description": str(tool.get("description") or "")[:1024],
                           "parameters": tool["inputSchema"], "strict": False}
            for tool in catalog if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool["name"])
        }
        if not self.tools:
            raise ValueError("No compatible Assist tools were discovered")
        state_tools = [name for name, tool in self.tools.items()
                       if re.fullmatch(r"(?:assist__)?(?:homeassistant__)?GetLiveContext", name)
                       and isinstance(tool["parameters"].get("properties"), dict)
                       and {"name", "domain"} <= tool["parameters"]["properties"].keys()]
        self._state_tool = state_tools[0] if len(state_tools) == 1 else None
        self.preview_target = preview_target
        self._preview_action_used = False
        if preview_target is not None:
            self.tools = {
                name: {"type": "function", "name": name, "strict": False,
                       "description": f'Turn the named preview device {POWER_STATE[match[1]]}.',
                       "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                                      "required": ["name"], "additionalProperties": False}}
                for name, tool in self.tools.items() if (match := POWER_TOOL.fullmatch(name))
                and isinstance(tool["parameters"].get("properties"), dict)
                and {"name", "domain"} <= tool["parameters"]["properties"].keys()
            }
            if not self.tools or self._state_tool is None:
                raise ValueError("Assist must offer named power control and filtered state lookup")
        self._current = None
        self._responses = set()
        self._calls = set()

    def config(self):
        return {"type": "responses", "responses": {
            "model": self.model, "reasoning": {"effort": "low"}, "max_output_tokens": BACKEND_OUTPUT_TOKENS,
            "service_tier": "default",
            "tool_choice": "auto", "parallel_tool_calls": False, "tools": list(self.tools.values()),
            "instructions": ("Use Assist functions only for an explicit user request. "
                             "Report success only when the App confirms the resulting state. "
                             "Never retry an unknown outcome. Ask before acting on an ambiguous target."),
        }}

    def observe(self, envelope):
        """Collect one completed, correlated response before any local side effect."""
        event = envelope.get("event")
        delegation_id = envelope.get("delegation_id")
        if not isinstance(event, dict) or not isinstance(delegation_id, str):
            return None
        kind = event.get("type")
        if kind == "response.created":
            response_id = (event.get("response") or {}).get("id")
            if not isinstance(response_id, str) or not response_id:
                raise ValueError("Missing delegated response ID")
            if response_id in self._responses:
                return None
            if self._current is not None or len(self._responses) >= MAX_CALLS:
                raise ValueError("Overlapping or excessive delegated responses")
            self._responses.add(response_id)
            self._current = ResponseBatch(delegation_id, response_id)
        elif self._current is not None and self._current.delegation_id == delegation_id:
            batch = self._current
            if kind == "response.output_item.done":
                item = event.get("item")
                if not isinstance(item, dict) or item.get("type") != "function_call":
                    return None
                call_id, name, arguments = (item.get(key) for key in ("call_id", "name", "arguments"))
                if (not isinstance(call_id, str) or not 0 < len(call_id) <= 128
                        or not isinstance(name, str) or not isinstance(arguments, str)
                        or len(arguments) > MAX_ARGUMENT_BYTES):
                    raise ValueError("Invalid delegated function item")
                invocation = (name, arguments)
                if call_id in batch.calls:
                    if batch.calls[call_id] != invocation:
                        raise ValueError("Conflicting duplicate function item")
                elif call_id in self._calls or len(self._calls) >= MAX_CALLS:
                    raise ValueError("Duplicate or excessive delegated invocations")
                else:
                    batch.calls[call_id] = invocation
                    self._calls.add(call_id)
            elif kind in ("response.completed", "response.failed", "response.incomplete", "response.cancelled"):
                response = event.get("response") or {}
                if response.get("id") != batch.response_id:
                    return None
                self._current = None
                if kind == "response.completed" and response.get("status") == "completed" and batch.calls:
                    return batch
        return None

    async def preview_state(self, call_id):
        domain, name = self.preview_target
        check = await self.client.call_tool(call_id, self._state_tool, {"name": name, "domain": domain})
        if check["outcome"] == "reported":
            return _live_context_state(check.get("text"), domain, name)
        return None

    async def execute(self, call_id, name, encoded):
        if name not in self.tools:
            return {"outcome": "rejected", "reason": "Function was not offered by Assist"}
        try:
            arguments = json.loads(encoded)
        except (ValueError, RecursionError):
            return {"outcome": "rejected", "reason": "Invalid function arguments"}
        match = POWER_TOOL.fullmatch(name)
        if self.preview_target is not None:
            domain, device_name = self.preview_target
            if (self._preview_action_used or not isinstance(arguments, dict) or set(arguments) != {"name"}
                    or not isinstance(arguments["name"], str)
                    or arguments["name"].strip().casefold() != device_name.casefold()):
                return {"outcome": "rejected", "reason": "Only one command for the selected preview device is allowed"}
            self._preview_action_used = True
            preflight_id = "before:" + sha256(call_id.encode()).hexdigest()
            state = await self.preview_state(preflight_id)
            if state not in ("on", "off"):
                return {"outcome": "rejected", "reason": "The selected device could not be uniquely resolved through Assist"}
            expected = POWER_STATE[match[1]]
            if state == expected:
                return {"outcome": "verified", "state": expected, "already_in_state": True}
            arguments = {"name": device_name, "domain": [domain]}

        result = await self.client.call_tool(call_id, name, arguments)
        outcome = result["outcome"]
        if outcome != "reported":
            return {"outcome": outcome, "reason": result.get("reason", "Home Assistant reported a failure")}

        target = _power_target(result.get("text")) if self._state_tool and match else None
        if target and (self.preview_target is None or target[0] == self.preview_target[0]
                       and target[1].casefold() == self.preview_target[1].casefold()):
            domain, device_name = target
            check_id = "verify:" + sha256(call_id.encode()).hexdigest()
            check = await self.client.call_tool(check_id, self._state_tool, {"name": device_name, "domain": domain})
            expected = POWER_STATE[match[1]]
            if (check["outcome"] == "reported"
                    and _live_context_state(check.get("text"), domain, device_name) == expected):
                return {"outcome": "verified", "state": expected}
        # No action result or room context is forwarded as free-form text.
        return dict(UNVERIFIED)
