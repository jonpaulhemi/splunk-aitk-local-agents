"""The agent's MCP tools: the invoke payload's `mcps` blocks as LangChain tools.

AI Toolkit's ai_agent resolves each connection's credentials before sending it:
- `auth_token` for its built-in types (Splunk, Atlassian, GitHub, ...), sent as
  a bearer token, or as `Token <t>` for PagerDuty, as its connection test does
- `headers` for Custom connections: bearer, API key, basic and custom-header
  auth (OAuth2 arrives already exchanged, as `auth_token`)

Servers' TLS certificates are verified against the OS trust store, unlike AI
Toolkit's own MCP client, which turns verification off.
"""

import asyncio
import datetime
import ssl

from aitk_local import log


class Unusable(Exception):
    """An MCP server can't be used; the message names it and says why, without secrets."""


async def load(mcps):
    """The tools selected on the agent, from every server in `mcps`, and a
    {tool name: server name} map for the trace."""
    per_server = await asyncio.gather(*(_server_tools(block) for block in mcps))
    tools = [t for server_tools in per_server for t in server_tools]
    return tools, {t.name: block["name"] for block, server_tools in zip(mcps, per_server) for t in server_tools}


async def _server_tools(block):
    from langchain_mcp_adapters.tools import load_mcp_tools

    name = block.get("name")
    selected = set(block.get("tools") or [])
    if not selected:
        return []
    try:
        offered = await load_mcp_tools(None, connection=_connection(block), server_name=name)
    except Exception as e:
        raise Unusable(f"MCP server '{name}' couldn't be used: {_reason(e)}") from e
    tools = [t for t in offered if t.name in selected]
    missing = sorted(selected - {t.name for t in tools})
    if missing:
        log.get_logger().warning("MCP server '%s' doesn't offer selected tools %s", name, missing)
    return tools


def _connection(block):
    details = block.get("connection_details") or {}
    headers = dict(details.get("headers") or {})
    if details.get("auth_token"):
        scheme = "Token" if block.get("type") == "PAGERDUTY" else "Bearer"
        headers["Authorization"] = f"{scheme} {details['auth_token']}"
    return {"transport": "streamable_http", "url": details.get("url"), "headers": headers,
            "httpx_client_factory": _http_client}


def _http_client(headers=None, timeout=None, auth=None):
    import httpx
    import truststore

    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, follow_redirects=True,
                             verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))


def _reason(e):
    """Why a server couldn't be used: the HTTP status or exception type, never
    the exception text, which can echo credentials."""
    import httpx

    causes = _causes(e)
    if any(isinstance(c, ssl.SSLCertVerificationError) for c in causes):
        return ("its TLS certificate isn't trusted by this search head; "
                "add its CA to the operating system's trust store")
    for cause in causes:
        if isinstance(cause, httpx.HTTPStatusError):
            return f"it answered HTTP {cause.response.status_code}"
        if isinstance(cause, httpx.HTTPError):  # ConnectError, ReadTimeout, ...
            return type(cause).__name__
    leaves = [c for c in causes if not isinstance(c, BaseExceptionGroup)]
    return type(leaves[0] if leaves else e).__name__


def _causes(e, depth=0):
    """`e`, its causes, and the members of any exception groups among them."""
    if e is None or depth > 10:
        return []
    found = [e]
    if isinstance(e, BaseExceptionGroup):
        for member in e.exceptions:
            found += _causes(member, depth + 1)
    return found + _causes(e.__cause__ or e.__context__, depth + 1)


class Trace:
    """Each tool call, in the fields AI Toolkit's run history shows: name,
    arguments, result, tool_call_status and start and end times."""

    def __init__(self, servers):
        from langchain_core.callbacks import BaseCallbackHandler

        trace = self
        self.servers = servers
        self.calls = []
        self._open = {}

        class Recorder(BaseCallbackHandler):
            def on_tool_start(self, serialized, input_str, *, run_id, inputs=None, tool_call_id=None, **kwargs):
                name = (serialized or {}).get("name")
                call = {"tool_call_id": tool_call_id, "name": name, "mcp_server": trace.servers.get(name),
                        "arguments": inputs or {}, "tool_call_start_time": _now()}
                trace._open[run_id] = call
                trace.calls.append(call)

            def on_tool_end(self, output, *, run_id, **kwargs):
                status = getattr(output, "status", "success")
                trace._finish(run_id, getattr(output, "text", None) or str(output), status)

            def on_tool_error(self, error, *, run_id, **kwargs):
                trace._finish(run_id, type(error).__name__, "error")

        self.recorder = Recorder()

    def _finish(self, run_id, result, status):
        call = self._open.pop(run_id, None)
        if call is not None:
            call.update(result=result, tool_call_status=status, tool_call_end_time=_now())


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
