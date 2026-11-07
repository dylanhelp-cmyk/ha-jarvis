import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from jarvis.assist import AssistDiscoveryError, PROTOCOL, discover_tools


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
