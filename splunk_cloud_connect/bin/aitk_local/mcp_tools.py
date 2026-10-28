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

from aitk_local import config, log


class Unusable(Exception):
    """An MCP server can't be used; the message names it and says why, without secrets."""


async def load(mcps):
    """The tools selected on the agent, from every server in `mcps`, a
    {tool name: server name} map for the trace, and the names of the tools
    that need approval before they run."""
    per_server = await asyncio.gather(*(_server_tools(block) for block in mcps))
    servers = {}
    for block, tools in zip(mcps, per_server):
        for tool in tools:
            # The model calls tools by name alone, so a name can't mean two servers' tools.
            if tool.name in servers:
                raise Unusable(f"Tool '{tool.name}' is selected from more than one MCP server "
                               f"('{servers[tool.name]}', '{block.get('name')}'); deselect it on all but one")
            servers[tool.name] = block.get("name")
    gated = {name for block in mcps for name in block.get("approval_required_tools") or []
             if servers.get(name) == block.get("name")}
    return [t for tools in per_server for t in tools], servers, gated


def tool_node(tools):
    """The graph's tool step. A call that fails on our side (a dropped
    connection, say) goes back to the model as an error result naming only the
    exception type; MCP servers' own tool errors arrive as results already."""
    from langgraph.prebuilt import ToolNode
    from langgraph.prebuilt.tool_node import ToolInvocationError

    def error_result(e):
        # Invalid arguments are the model's own mistake, and the details help it retry.
        return e.message if isinstance(e, ToolInvocationError) else f"Error: {type(e).__name__}"

    return ToolNode(tools, handle_tool_errors=error_result)


async def _server_tools(block):
    from langchain_mcp_adapters.tools import load_mcp_tools

    name = block.get("name")
    selected = set(block.get("tools") or [])
    if not selected:
        return []
    try:
        offered = await load_mcp_tools(None, connection=_connection(block), server_name=name,
                                       tool_interceptors=[_capped(config.max_tool_result_chars())])
    except Exception as e:
        raise Unusable(f"MCP server '{name}' couldn't be used: {_reason(e)}") from e
    tools = [t for t in offered if t.name in selected]
    missing = sorted(selected - {t.name for t in tools})
    if missing:
        log.get_logger().warning("MCP server '%s' doesn't offer selected tools %s", name, missing)
    return tools


def _capped(limit):
    """A tool interceptor that cuts a result's text to `limit` characters.

    Every result stays in the conversation and is resent on each step, so a
    few large searches can outgrow the model's context window (live on 0.8.0:
    14 Splunk searches, about 400 KB, ended a Bedrock run). The model is told
    the result was cut, so it can narrow its search.
    """
    async def cap(request, handler):
        from mcp.types import CallToolResult, TextContent

        result = await handler(request)
        if not isinstance(result, CallToolResult):
            return result
        texts = [c for c in result.content if isinstance(c, TextContent)]
        total = sum(len(c.text) for c in texts)
        if total <= limit:
            return result
        content, room = [], limit
        for c in result.content:
            if isinstance(c, TextContent):
                c, room = c.model_copy(update={"text": c.text[:room]}), max(0, room - len(c.text))
            content.append(c)
        content.append(TextContent(type="text", text=(
            f"\n[Result cut to {limit} of {total} characters to fit the model's context. "
            "Narrow the search or ask for fewer rows to see more.]")))
        return result.model_copy(update={"content": content})

    return cap


def _connection(block):
    details = block.get("connection_details") or {}
    headers = dict(details.get("headers") or {})
    if details.get("auth_token"):
        scheme = "Token" if block.get("type") == "PAGERDUTY" else "Bearer"
        headers["Authorization"] = f"{scheme} {details['auth_token']}"
    return {"transport": "streamable_http", "url": details.get("url"), "headers": headers,
            "httpx_client_factory": _http_client}


def _http_client(headers=None, timeout=None, auth=None):
    # Redirects aren't followed: a custom header with a key in it would go
    # wherever the server pointed.
    import httpx
    import truststore

    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, follow_redirects=False,
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

    def __init__(self, servers=None):
        self.servers = servers or {}  # tool name -> MCP server name, set once tools are loaded
        self.calls = []
        self._open = {}  # LangChain run id -> call
        self.recorder = _recorder(self)

    def started(self, run_id, name, tool_call_id, arguments):
        call = {"tool_call_id": tool_call_id, "name": name, "mcp_server": self.servers.get(name),
                "arguments": arguments or {}, "tool_call_start_time": _now()}
        self._open[run_id] = call
        self.calls.append(call)

    def rejected(self, tool_call_id, name, arguments, result):
        now = _now()
        self.calls.append({"tool_call_id": tool_call_id, "name": name, "mcp_server": self.servers.get(name),
                           "arguments": arguments or {}, "tool_call_start_time": now, "result": result,
                           "tool_call_status": "rejected_by_human", "tool_call_end_time": now})

    def finished(self, run_id, result, status):
        call = self._open.pop(run_id, None)
        if call is not None:
            call.update(result=result, tool_call_status=status, tool_call_end_time=_now())


def _recorder(trace):
    """A LangChain callback handler feeding `trace` (LangChain is imported
    late, once the bundled libraries are on the path)."""
    from langchain_core.callbacks import BaseCallbackHandler

    class Recorder(BaseCallbackHandler):
        def on_tool_start(self, serialized, input_str, *, run_id, inputs=None, tool_call_id=None, **kwargs):
            trace.started(run_id, (serialized or {}).get("name"), tool_call_id, inputs)

        def on_tool_end(self, output, *, run_id, **kwargs):
            # A ToolMessage, whose status is "error" for an MCP tool error.
            trace.finished(run_id, getattr(output, "text", None) or str(output), getattr(output, "status", "success"))

        def on_tool_error(self, error, *, run_id, **kwargs):
            trace.finished(run_id, f"Error: {type(error).__name__}", "error")

    return Recorder()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
