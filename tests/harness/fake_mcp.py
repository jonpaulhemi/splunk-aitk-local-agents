"""A fake MCP server over streamable HTTP and TLS: the MCP edge in tests.

Serves three tools: `search` (echoes the query with a result count, or for
`big <n>` returns n characters; like the Splunk MCP Server's splunk_run_query
it has optional arguments too),
`delete_index` (which agents in these tests never select) and `broken` (an MCP
tool error). Every HTTP request's headers are recorded, and requests missing
`required_headers` are answered 401 the way an MCP server rejects bad
credentials; with `redirect_to` set, every request is redirected there. Tool calls are recorded as (name, arguments).
"""

import asyncio
import socket
import threading
import time

import uvicorn
from mcp.server.fastmcp import FastMCP


class FakeMCP:
    def __init__(self, cert, key):
        self.cert = cert
        self.key = key
        self.requests = []  # lower-cased header dicts, one per HTTP request
        self.calls = []  # (tool name, arguments)
        self.required_headers = {}
        self.redirect_to = None  # answer every request with a 307 to this URL
        self._server = None
        self._port = None

    @property
    def url(self):
        return f"https://localhost:{self._port}/mcp"

    def start(self):
        app = self._app()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self._port = sock.getsockname()[1]
        config = uvicorn.Config(app, ssl_certfile=str(self.cert), ssl_keyfile=str(self.key),
                                log_level="warning", lifespan="on")
        self._server = uvicorn.Server(config)
        threading.Thread(target=lambda: asyncio.run(self._server.serve(sockets=[sock])), daemon=True).start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake MCP server didn't start")
            time.sleep(0.05)
        return self

    def stop(self):
        self._server.should_exit = True

    def _app(self):
        fake = self
        mcp = FastMCP("fake-splunk-mcp")

        @mcp.tool()
        def search(query: str, earliest_time: str = "-15m") -> str:
            """Run a Splunk search and return the matching events."""
            fake.calls.append(("search", {"query": query}))
            if query.startswith("big "):  # a search returning a lot of data
                return "e" * int(query.split()[1])
            return f"3 events for {query}"

        @mcp.tool()
        def delete_index(name: str) -> str:
            """Delete a Splunk index."""
            fake.calls.append(("delete_index", {"name": name}))
            return f"deleted {name}"

        @mcp.tool()
        def broken() -> str:
            """A tool that always fails."""
            fake.calls.append(("broken", {}))
            raise ValueError("the tool broke")

        inner = mcp.streamable_http_app()

        async def app(scope, receive, send):
            if scope["type"] == "http":
                headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
                fake.requests.append(headers)
                if fake.redirect_to:
                    await send({"type": "http.response.start", "status": 307,
                                "headers": [(b"location", fake.redirect_to.encode())]})
                    await send({"type": "http.response.body", "body": b""})
                    return
                if any(headers.get(k.lower()) != v for k, v in fake.required_headers.items()):
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"text/plain")]})
                    await send({"type": "http.response.body", "body": b"unauthorized"})
                    return
            await inner(scope, receive, send)

        return app
