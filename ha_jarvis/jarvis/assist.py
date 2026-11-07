"""Read-only discovery against Home Assistant's stateless Assist MCP endpoint."""
import argparse
import asyncio
from getpass import getpass
import json

from aiohttp import ClientError, ClientSession
from yarl import URL


PROTOCOL = "2025-11-25"
SUPPORTED_PROTOCOLS = {PROTOCOL, "2025-06-18", "2025-03-26"}


class AssistDiscoveryError(RuntimeError):
    """A discovery failure safe to show without a URL, token or response body."""


CALL_TIMEOUT_SECONDS = 3
MAX_ARGUMENT_BYTES = 8192
MAX_RESULT_BYTES = 32768
MAX_CALLS = 32


class AssistClient:
    """One local Assist catalog and a bounded set of tool invocations."""

    def __init__(self, origin: str, token: str):
        try:
            base = URL(origin)
        except ValueError:
            raise AssistDiscoveryError("Enter a valid Home Assistant origin") from None
        if (base.scheme not in {"http", "https"} or not base.host or base.user is not None
                or base.path != "/" or base.query_string or base.fragment or not token.strip()):
            raise AssistDiscoveryError("Enter a Home Assistant origin and access token")
        self._endpoint = base.with_path("/api/mcp/assist")
        self._token = token
        self._protocol = PROTOCOL
        self._request_id = 0
        self._tool_names = frozenset()
        self._attempts = {}
        self._call_lock = asyncio.Lock()
        self._client = None

    async def __aenter__(self):
        self._client = ClientSession(headers={"Authorization": "Bearer " + self._token,
                                              "Accept": "application/json, text/event-stream"})
        return self

    async def __aexit__(self, *_):
        await self._client.close()
        self._client = None
        self._tool_names = frozenset()

    async def _post(self, method, params, *, notification=False):
        if self._client is None:
            raise AssistDiscoveryError("Assist connection is closed")
        self._request_id += 1
        request_id = self._request_id
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = request_id
        async with self._client.post(self._endpoint, json=message, allow_redirects=False,
                                     headers={"MCP-Protocol-Version": self._protocol}) as response:
            if response.status in {401, 403}:
                raise AssistDiscoveryError("Home Assistant denied Assist access")
            if response.status == 404:
                raise AssistDiscoveryError("Home Assistant did not expose the Assist MCP endpoint")
            if notification and response.status == 202:
                return None
            if response.status != 200 or response.content_type != "application/json":
                raise AssistDiscoveryError("Assist MCP returned an unexpected HTTP response")
            payload = await response.json()
        if (not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0"
                or type(payload.get("id")) is not int or payload["id"] != request_id
                or "error" in payload or not isinstance(payload.get("result"), dict)):
            raise AssistDiscoveryError("Assist MCP returned an invalid result")
        return payload["result"]

    async def list_tools(self) -> list[dict]:
        """Discover tools without executing them or fetching household state."""
        self._tool_names = frozenset()
        self._protocol = PROTOCOL
        try:
            async with asyncio.timeout(5):
                initialized = await self._post("initialize", {"protocolVersion": self._protocol,
                    "capabilities": {}, "clientInfo": {"name": "HA Jarvis", "version": "0.1"}})
                protocol = initialized.get("protocolVersion")
                capabilities = initialized.get("capabilities")
                if (not isinstance(protocol, str) or protocol not in SUPPORTED_PROTOCOLS
                        or not isinstance(capabilities, dict) or "tools" not in capabilities):
                    raise AssistDiscoveryError("Assist MCP did not negotiate tool discovery")
                self._protocol = protocol
                await self._post("notifications/initialized", {}, notification=True)
                tools, cursors = {}, set()
                params = {}
                while True:
                    page = await self._post("tools/list", params)
                    items = page.get("tools")
                    if not isinstance(items, list):
                        raise AssistDiscoveryError("Assist MCP returned an invalid tool list")
                    for tool in items:
                        if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                                or not tool["name"] or tool["name"] in tools
                                or not isinstance(tool.get("inputSchema"), dict)):
                            raise AssistDiscoveryError("Assist MCP returned an invalid tool definition")
                        tools[tool["name"]] = tool
                    cursor = page.get("nextCursor")
                    if cursor is None:
                        self._tool_names = frozenset(tools)
                        return list(tools.values())
                    if not isinstance(cursor, str) or not cursor or cursor in cursors or not items:
                        raise AssistDiscoveryError("Assist MCP returned an invalid tool page")
                    cursors.add(cursor)
                    params = {"cursor": cursor}
        except (ClientError, TimeoutError, ValueError):
            raise AssistDiscoveryError("Could not complete Assist MCP discovery") from None

    async def call_tool(self, call_id, name, arguments) -> dict:
        """Report the Assist result. A reported completion still needs state verification."""
        if (not isinstance(call_id, str) or not 0 < len(call_id) <= 128
                or not isinstance(name, str) or not isinstance(arguments, dict)):
            return {"outcome": "rejected", "reason": "Invalid Assist invocation"}
        try:
            encoded = json.dumps(arguments, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError, RecursionError):
            return {"outcome": "rejected", "reason": "Invalid Assist arguments"}
        if len(encoded) > MAX_ARGUMENT_BYTES:
            return {"outcome": "rejected", "reason": "Assist arguments exceed the limit"}
        async with self._call_lock:
            if name not in self._tool_names:
                return {"outcome": "rejected", "reason": "Tool is outside the current Assist catalog"}
            if call_id in self._attempts:
                old_name, old_arguments, previous = self._attempts[call_id]
                if (old_name, old_arguments) != (name, encoded):
                    return {"outcome": "rejected", "reason": "Invocation ID was already used"}
                return dict(previous)
            if len(self._attempts) >= MAX_CALLS:
                return {"outcome": "rejected", "reason": "Assist invocation limit reached"}
            unknown = {"outcome": "unknown", "reason": "Assist outcome unconfirmed. Check state before retrying."}
            # Record before the first await. A timeout or cancellation may
            # leave an action executing in Home Assistant.
            self._attempts[call_id] = (name, encoded, unknown)
            try:
                async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
                    reply = await self._post("tools/call", {"name": name, "arguments": json.loads(encoded)})
            except (AssistDiscoveryError, ClientError, TimeoutError, ValueError):
                return dict(unknown)
            content = reply.get("content")
            error = reply.get("isError", False)
            if (type(error) is not bool or not isinstance(content, list) or not content
                    or any(not isinstance(item, dict) or item.get("type") != "text"
                           or not isinstance(item.get("text"), str) for item in content)):
                return dict(unknown)
            text = "\n".join(item["text"] for item in content)
            if not text or len(text.encode("utf-8", errors="replace")) > MAX_RESULT_BYTES:
                return dict(unknown)
            result = {"outcome": "failed" if error else "reported", "text": text}
            self._attempts[call_id] = (name, encoded, result)
            return dict(result)


async def discover_tools(origin: str, token: str) -> list[dict]:
    """Retain the read-only discovery entry point for ingress and the CLI."""
    async with AssistClient(origin, token) as assist:
        return await assist.list_tools()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin", help="Home Assistant URL with scheme and port, without a path")
    args = parser.parse_args()
    token = getpass("Home Assistant access token: ")
    try:
        tools = asyncio.run(discover_tools(args.origin, token))
    except AssistDiscoveryError as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps([tool["name"] for tool in tools], indent=2))


if __name__ == "__main__":
    main()
