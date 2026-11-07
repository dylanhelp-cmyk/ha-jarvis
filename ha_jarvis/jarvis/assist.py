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


async def discover_tools(origin: str, token: str) -> list[dict]:
    """List tools without executing them or fetching household state."""
    try:
        base = URL(origin)
    except ValueError:
        raise AssistDiscoveryError("Enter a valid Home Assistant origin") from None
    if (base.scheme not in {"http", "https"} or not base.host or base.user is not None
            or base.path != "/" or base.query_string or base.fragment or not token.strip()):
        raise AssistDiscoveryError("Enter a Home Assistant origin and access token")
    endpoint = base.with_path("/api/mcp/assist")
    protocol = PROTOCOL
    request_id = 0
    async with ClientSession(headers={"Authorization": "Bearer " + token,
                                      "Accept": "application/json, text/event-stream"}) as client:
        async def post(method, params, *, notification=False):
            nonlocal request_id
            request_id += 1
            message = {"jsonrpc": "2.0", "method": method, "params": params}
            if not notification:
                message["id"] = request_id
            async with client.post(endpoint, json=message, allow_redirects=False,
                                   headers={"MCP-Protocol-Version": protocol}) as response:
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
                    or payload.get("id") != request_id or "error" in payload
                    or not isinstance(payload.get("result"), dict)):
                raise AssistDiscoveryError("Assist MCP returned an invalid result")
            return payload["result"]

        try:
            async with asyncio.timeout(5):
                initialized = await post("initialize", {"protocolVersion": protocol, "capabilities": {},
                                                        "clientInfo": {"name": "HA Jarvis", "version": "0.1"}})
                protocol = initialized.get("protocolVersion")
                capabilities = initialized.get("capabilities")
                if (not isinstance(protocol, str) or protocol not in SUPPORTED_PROTOCOLS
                        or not isinstance(capabilities, dict) or "tools" not in capabilities):
                    raise AssistDiscoveryError("Assist MCP did not negotiate tool discovery")
                await post("notifications/initialized", {}, notification=True)
                tools = {}
                params = {}
                while True:
                    page = await post("tools/list", params)
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
                        return list(tools.values())
                    if not isinstance(cursor, str) or not cursor or not items:
                        raise AssistDiscoveryError("Assist MCP returned an invalid tool page")
                    params = {"cursor": cursor}
        except (ClientError, TimeoutError, ValueError):
            raise AssistDiscoveryError("Could not complete Assist MCP discovery") from None


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
