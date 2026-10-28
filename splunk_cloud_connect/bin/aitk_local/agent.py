"""One agent run: AI Toolkit's decrypted invoke payload in, AgentCore's final chunk out."""

import asyncio
import time
import traceback

from aitk_local import llm, log, mcp_tools, structured

from aitk_local.isolated import TIMEOUT_SECONDS

# Blocks AI Toolkit sends that later tickets will use; for now only their key
# names are logged, and only when they hold something.
NOT_YET_USED = ("knowledge_bases", "skills")
DEFAULT_TIMEOUT = 300
# Leave the worker room to report the timeout before isolated.run kills it.
MAX_TIMEOUT = TIMEOUT_SECONDS - 60


class NotSupported(Exception):
    pass


def run(payload):
    """The `completed` chunk: success, response, and error_message on failure."""
    logger = log.get_logger()
    started = time.monotonic()
    block = payload.get("llm") or {}
    ids = f"runtime_id={payload.get('runtime_id')} session_id={payload.get('session_id')}"
    ignored = sorted(k for k in NOT_YET_USED if _has_content(payload.get(k)))
    if ignored:
        logger.info("invoke %s: not yet supported, ignoring %s", ids, ignored)
    timeout = DEFAULT_TIMEOUT
    try:
        timeout = _timeout(payload.get("agent_timeout"))
        if "hitl_response" in payload:
            raise NotSupported("Approvals (human in the loop) aren't supported by the local agent runtime yet")
        response, trace = _within(timeout, _answer(block, payload))
        outcome = _completed(True, response, tool_invocations=trace)
    except (llm.UnusableConnection, mcp_tools.Unusable, structured.Invalid, NotSupported) as e:
        outcome = _completed(False, error=str(e))
    except TimeoutError:
        outcome = _completed(False, error=f"Agent timed out after {timeout}s")
    except Exception as e:
        outcome = _completed(False, error=_llm_error(e))
        logger.info("invoke %s failed at:\n%s", ids, _where(e))
    logger.info(
        "invoke completed %s model=%s provider=%s success=%s tool_calls=%d duration=%.1fs%s",
        ids, block.get("model"), block.get("provider"), outcome["success"], len(outcome["tool_invocations"]),
        time.monotonic() - started,
        f" error={outcome['error_message']}" if not outcome["success"] else "",
    )
    return outcome


def _timeout(value):
    seconds = int(value) if value else DEFAULT_TIMEOUT
    return max(1, min(seconds, MAX_TIMEOUT))


def _has_content(value):
    # knowledge_bases arrives as {"vector_dbs": []} even when the agent has none.
    if isinstance(value, dict):
        return any(_has_content(v) for v in value.values())
    return bool(value)


def _where(e):
    """Where the root cause was raised: exception types and frames, without the
    exception text, which can echo the request."""
    chain = [e]
    while (cause := chain[-1].__cause__ or chain[-1].__context__) and len(chain) < 10:
        chain.append(cause)
    frames = traceback.extract_tb(chain[-1].__traceback__)[-6:]
    causes = " <- ".join(type(c).__name__ for c in chain)
    return causes + "\n" + "".join(f"  {f.filename}:{f.lineno} in {f.name}\n" for f in frames)


def _llm_error(e):
    # The exception text can echo the request, so only its type, any AWS error
    # code, and the HTTP status go out.
    name = type(e).__name__
    status = getattr(e, "status_code", None)
    aws = getattr(e, "response", None)
    if isinstance(aws, dict):  # botocore ClientError
        name = f"{name} {aws.get('Error', {}).get('Code', '')}".strip()
        status = aws.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return f"LLM call failed: {name}" + (f" (HTTP {status})" if status else "")


def _within(seconds, coroutine):
    """Run `coroutine`, raising TimeoutError after `seconds` even if it left
    threads behind (Bedrock calls run in threads asyncio can't cancel).

    asyncio.run would wait for those threads at shutdown; closing the loop
    doesn't, and the worker process exits right after replying.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(coroutine, timeout=seconds))
    finally:
        loop.close()


async def _answer(block, payload):
    """The answer (text, or a dict for structured output) and the tool calls made on the way."""
    from langchain_core.messages import SystemMessage
    from langgraph.prebuilt import create_react_agent

    tools, servers = await mcp_tools.load(payload.get("mcps") or [])
    trace = mcp_tools.Trace(servers)
    # Building a Bedrock model may call STS, so it counts against the timeout.
    model = await asyncio.to_thread(llm.chat_model, block)
    system_prompt = payload.get("system_prompt") or None
    graph = create_react_agent(llm.with_tools(model, tools), tools=mcp_tools.tool_node(tools) if tools else [],
                               prompt=system_prompt)
    state = await graph.ainvoke({"messages": [("user", payload.get("task_prompt") or "")]},
                                config={"callbacks": [trace.recorder]})
    if payload.get("structured_output") is None:
        return state["messages"][-1].text, trace.calls
    # One more call, as LangGraph's own response_format does, so the agent
    # can use its tools first and then put the outcome into the shape.
    messages = ([SystemMessage(system_prompt)] if system_prompt else []) + state["messages"]
    return await structured.answer(model, messages, structured.schema(payload["structured_output"])), trace.calls


def failed(error):
    log.get_logger().info("invoke failed before running: %s", error)
    return _completed(False, error=error)


def _completed(success, response=None, error=None, tool_invocations=()):
    chunk = {
        "status": "completed",
        "success": success,
        "response": response,
        "tool_invocations": list(tool_invocations),
        "hitl_status": None,
        "hitl_pending": [],
        "skills": [],
    }
    if not success:
        chunk["error_message"] = error
    return chunk
