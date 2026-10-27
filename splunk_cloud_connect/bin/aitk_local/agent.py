"""One agent run: AI Toolkit's decrypted invoke payload in, AgentCore's final chunk out."""

import asyncio
import time

from aitk_local import llm, log

# Blocks AI Toolkit always sends that later tickets will use; for now only
# their key names are logged.
NOT_YET_USED = ("mcps", "knowledge_bases", "skills")


def run(payload):
    """The `completed` chunk: success, response, and error_message on failure."""
    logger = log.get_logger()
    started = time.monotonic()
    block = payload.get("llm") or {}
    ids = f"runtime_id={payload.get('runtime_id')} session_id={payload.get('session_id')}"
    ignored = sorted(k for k in NOT_YET_USED if payload.get(k))
    if ignored:
        logger.info("invoke %s: not yet supported, ignoring %s", ids, ignored)
    timeout = int(payload.get("agent_timeout") or 300)
    try:
        response = asyncio.run(
            asyncio.wait_for(_answer(llm.chat_model(block), payload), timeout=timeout)
        )
        outcome = _completed(True, response)
    except llm.UnsupportedProvider as e:
        outcome = _completed(False, error=str(e))
    except (asyncio.TimeoutError, TimeoutError):
        outcome = _completed(False, error=f"Agent timed out after {timeout}s")
    except Exception as e:
        # The exception text can echo the request; log only its type.
        outcome = _completed(False, error=f"LLM call failed: {type(e).__name__}")
    logger.info(
        "invoke completed %s model=%s provider=%s success=%s duration=%.1fs%s",
        ids, block.get("model"), block.get("provider"), outcome["success"], time.monotonic() - started,
        f" error={outcome['error_message']}" if not outcome["success"] else "",
    )
    return outcome


async def _answer(model, payload):
    from langgraph.prebuilt import create_react_agent

    graph = create_react_agent(model, tools=[], prompt=payload.get("system_prompt") or None)
    state = await graph.ainvoke({"messages": [("user", payload.get("task_prompt") or "")]})
    return state["messages"][-1].content


def failed(error):
    log.get_logger().info("invoke failed before running: %s", error)
    return _completed(False, error=error)


def _completed(success, response=None, error=None):
    chunk = {
        "status": "completed",
        "success": success,
        "response": response,
        "tool_invocations": [],
        "hitl_status": None,
        "hitl_pending": [],
        "skills": [],
    }
    if not success:
        chunk["error_message"] = error
    return chunk
