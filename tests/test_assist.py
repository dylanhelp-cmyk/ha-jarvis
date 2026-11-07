import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from jarvis.assist import AssistClient, AssistDiscoveryError, PROTOCOL, discover_tools
from jarvis.assist_delegation import AssistDelegation


class AssistDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_only_assist_tools_and_negotiates_older_protocol(self):
        methods = []

        async def assist(request):
            self.assertEqual(request.headers["Authorization"], "Bearer local-test-token")
            message = await request.json()
            methods.append(message["method"])
            if message["method"] == "initialize":
                self.assertEqual(request.headers["MCP-Protocol-Version"], PROTOCOL)
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
            else:
                self.assertEqual(request.headers["MCP-Protocol-Version"], "2025-06-18")
                if message["method"] == "notifications/initialized":
                    self.assertNotIn("id", message)
                    return web.Response(status=202)
                self.assertEqual(message["method"], "tools/list")
                schema = {"type": "object"}
                if message["params"] == {}:
                    result = {"tools": [{"name": "HassTurnOn", "inputSchema": schema}], "nextCursor": "next"}
                else:
                    self.assertEqual(message["params"], {"cursor": "next"})
                    result = {"tools": [{"name": "GetLiveContext", "inputSchema": schema}]}
            return web.json_response({"jsonrpc": "2.0", "id": message["id"], "result": result})

        app = web.Application()
        app.router.add_post("/api/mcp/assist", assist)
        async with TestServer(app) as server:
            tools = await discover_tools(str(server.make_url("/")), "local-test-token")
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/list", "tools/list"])
        self.assertEqual([tool["name"] for tool in tools], ["HassTurnOn", "GetLiveContext"])

    async def test_only_catalogued_actions_dispatch_and_timeout_is_never_replayed(self):
        calls = []

        async def assist(request):
            message = await request.json()
            method = message["method"]
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}}}
            elif method == "notifications/initialized":
                return web.Response(status=202)
            elif method == "tools/list":
                result = {"tools": [{"name": "HassTurnOn", "inputSchema": {"type": "object"}}]}
            else:
                self.assertEqual(method, "tools/call")
                self.assertEqual(message["params"]["name"], "HassTurnOn")
                args = message["params"]["arguments"]
                calls.append(args)
                if args["name"] == "timeout":
                    await asyncio.sleep(.08)
                result = {"isError": args["name"] == "denied",
                          "content": [{"type": "text", "text": '{"targets": []}'}]}
            return web.json_response({"jsonrpc": "2.0", "id": message["id"], "result": result})

        app = web.Application()
        app.router.add_post("/api/mcp/assist", assist)
        async with TestServer(app) as server:
            async with AssistClient(str(server.make_url("/")), "local-test-token") as client:
                not_ready = await client.call_tool("before-list", "HassTurnOn", {"name": "lamp"})
                self.assertEqual(not_ready["outcome"], "rejected")
                await client.list_tools()
                blocked = await client.call_tool("blocked", "RunAdministrativeAction", {})
                self.assertEqual(blocked["outcome"], "rejected")
                result = await client.call_tool("action", "HassTurnOn", {"name": "lamp"})
                self.assertEqual(result["outcome"], "reported")
                self.assertEqual(result, await client.call_tool("action", "HassTurnOn", {"name": "lamp"}))
                reused = await client.call_tool("action", "HassTurnOn", {"name": "other lamp"})
                self.assertEqual(reused["outcome"], "rejected")
                denied = await client.call_tool("denied", "HassTurnOn", {"name": "denied"})
                self.assertEqual(denied["outcome"], "failed")
                with patch("jarvis.assist.CALL_TIMEOUT_SECONDS", .025):
                    unknown = await client.call_tool("slow", "HassTurnOn", {"name": "timeout"})
                self.assertEqual(unknown["outcome"], "unknown")
                self.assertEqual(unknown, await client.call_tool("slow", "HassTurnOn", {"name": "timeout"}))
        self.assertEqual(calls, [{"name": "lamp"}, {"name": "denied"}, {"name": "timeout"}])

    async def test_power_outcome_needs_a_unique_matching_assist_state(self):
        on = "assist__intent__HassTurnOn"
        read = "assist__homeassistant__GetLiveContext"
        catalog = [{"name": on, "inputSchema": {"type": "object"}},
                   {"name": read, "inputSchema": {"type": "object", "properties": {"name": {}, "domain": {}}}}]
        action = {"outcome": "reported", "text": json.dumps({"response_type": "action_done", "data": {
            "success": [{"type": "entity", "name": "Desk Lamp", "id": "light.desk_lamp"}], "failed": []}})}
        row = "- names: Desk Lamp, Reading Lamp\n  domain: light\n  state: 'on'\n"
        for content, expected in [(row, "verified"), (row * 2, "unverified"),
                                  (row.replace("'on'", "'off'"), "unverified"),
                                  (row.replace("Desk Lamp", "Other Lamp"), "unverified")]:
            with self.subTest(expected=expected, context=content):
                check = {"outcome": "reported", "text": json.dumps({"result": "Live Context: example\n" + content})}
                client = SimpleNamespace(call_tool=AsyncMock(side_effect=[action, check]))
                bridge = AssistDelegation(client, catalog)
                result = await bridge.execute("call_on", on, '{"name":"Desk Lamp"}')
                self.assertEqual(result["outcome"], expected)
                self.assertNotIn("Desk Lamp", str(result))
                verify = client.call_tool.await_args_list[1].args
                self.assertTrue(verify[0].startswith("verify:"))
                self.assertEqual(verify[1:], (read, {"name": "Desk Lamp", "domain": "light"}))

        client = SimpleNamespace(call_tool=AsyncMock(return_value={"outcome": "unknown"}))
        self.assertEqual((await AssistDelegation(client, catalog).execute("unknown", on, "{}"))["outcome"], "unknown")
        self.assertEqual(client.call_tool.await_count, 1)

    async def test_preview_limits_mutation_to_one_verified_named_device(self):
        on = "assist__intent__HassTurnOn"
        read = "assist__homeassistant__GetLiveContext"
        schema = {"type": "object", "properties": {"name": {}, "domain": {}}}
        catalog = [{"name": name, "inputSchema": schema} for name in (on, read)]

        def state(value):
            return {"outcome": "reported", "text": json.dumps({
                "result": f"Live Context: example\n- names: Desk Lamp\n  domain: light\n  state: '{value}'\n",
            })}

        action = {"outcome": "reported", "text": json.dumps({"response_type": "action_done", "data": {
            "success": [{"type": "entity", "name": "Desk Lamp", "id": "light.desk_lamp"}], "failed": []}})}
        client = SimpleNamespace(call_tool=AsyncMock(side_effect=[state("off"), action, state("on")]))
        bridge = AssistDelegation(client, catalog, preview_target=("light", "Desk Lamp"))
        for encoded in ('{"name":"Other Lamp"}', '{"name":"Desk Lamp","area":"Kitchen"}'):
            self.assertEqual((await bridge.execute("wrong", on, encoded))["outcome"], "rejected")
        client.call_tool.assert_not_awaited()
        self.assertEqual(await bridge.execute("valid", on, '{"name":"desk lamp"}'),
                         {"outcome": "verified", "state": "on"})
        self.assertEqual(client.call_tool.await_args_list[1].args,
                         ("valid", on, {"name": "Desk Lamp", "domain": ["light"]}))
        self.assertEqual((await bridge.execute("repeat", on, '{"name":"Desk Lamp"}'))["outcome"], "rejected")
        self.assertEqual(client.call_tool.await_count, 3)

    async def test_never_follows_a_redirect_to_a_different_api(self):
        followed = False

        async def assist(_request):
            raise web.HTTPTemporaryRedirect(location="/api/mcp")

        async def other(_request):
            nonlocal followed
            followed = True
            return web.Response()

        app = web.Application()
        app.router.add_post("/api/mcp/assist", assist)
        app.router.add_post("/api/mcp", other)
        async with TestServer(app) as server:
            with self.assertRaisesRegex(AssistDiscoveryError, "unexpected HTTP response"):
                await discover_tools(str(server.make_url("/")), "local-test-token")
        self.assertFalse(followed)
