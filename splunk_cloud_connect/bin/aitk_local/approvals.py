"""Approvals (human in the loop): tool calls that wait for someone's decision (#23).

A tool listed in its MCP connection's approval_required_tools doesn't run
until someone approves it. The run pauses with LangGraph's interrupt() right
after the model asks for the call, and the session checkpointer (memory.py)
keeps it paused. AI Toolkit shows the pending calls in Launchpad and its
email, then resumes the session with `hitl_response`: one
{tool_call_id, approved} decision per call and an optional correction.
"""

WAITING = "waiting_for_approval"
# Tool call statuses AI Toolkit's Launchpad recognises.
WAITING_FOR_HUMAN = "waiting_for_human_approval"
REJECTED_BY_HUMAN = "rejected_by_human"

REJECTED = "The user rejected this tool call, so it didn't run."
UNDECIDED = "No one approved this tool call, so it didn't run."
NOT_RUN = "This tool call didn't run."


class NothingWaiting(Exception):
    def __init__(self):
        super().__init__("There's no run waiting for approval in this session; it may have finished or expired. "
                         "Ask again to start a new run.")


class Gate:
    """The graph's post_model_hook: pause before any tool call in `gated` runs,
    and on resume pass the decisions on."""

    def __init__(self, gated, trace):
        self.gated = set(gated)
        self.trace = trace
        # A correction given while approving every call: shown to the model on
        # its next call, since the approved calls' results must come first.
        # Not kept in the session.
        self.note = None

    def __call__(self, state):
        from langgraph.types import interrupt

        asking = state["messages"][-1]
        renamed = _unique_ids(asking, state["messages"])
        calls = [c for c in (renamed or asking).tool_calls if c["name"] in self.gated]
        if not calls:
            return {"messages": [renamed]} if renamed else {}
        decision = interrupt([self._pending(c) for c in calls]) or {}
        approved = {d.get("tool_call_id") for d in decision.get("decisions") or [] if d.get("approved") is True}
        correction = (decision.get("correction") or "").strip()
        # A call left without a decision doesn't run either.
        rejected = [c for c in calls if c["id"] not in approved]
        result = REJECTED + (f" Their note: {correction}" if correction else "")
        for c in rejected:
            self.trace.rejected(c["id"], c["name"], c["args"], result)
        if correction and not rejected:
            self.note = f"Note from the approver: {correction}"
        return {"messages": ([renamed] if renamed else []) + [_result(c["id"], c["name"], result) for c in rejected]}

    def _pending(self, call):
        # The field names AI Toolkit's Launchpad and its approval email read.
        return {"tool_call_id": call["id"], "name": call["name"], "tool_name": call["name"],
                "mcp": self.trace.servers.get(call["name"]), "arguments": call["args"],
                "tool_arguments": call["args"], "tool_call_status": WAITING_FOR_HUMAN}

    def take_note(self):
        note, self.note = self.note, None
        return note


def unanswered(messages, pending):
    """Results for the last model message's calls that never got one (a run
    paused, or stopped partway), for when a new prompt arrives; models reject a
    conversation with calls left unanswered."""
    from langchain_core.messages import AIMessage, ToolMessage

    last = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], AIMessage)), None)
    if last is None:
        return []
    answered = {m.tool_call_id for m in messages[last + 1:] if isinstance(m, ToolMessage)}
    waiting = {p["tool_call_id"] for p in pending}
    return [_result(c["id"], c["name"], UNDECIDED if c["id"] in waiting else NOT_RUN)
            for c in messages[last].tool_calls if c["id"] not in answered]


def _unique_ids(asking, messages):
    """`asking` with tool call ids made unique in the session, or None if they are.

    LangGraph sends a call to the tools only if no earlier result has its id,
    and some OpenAI-compatible servers number calls per response (call_1 each
    turn). The new ids depend only on the conversation, so they come out the
    same when the step is replayed on resume.
    """
    from langchain_core.messages import ToolMessage

    used = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    calls = getattr(asking, "tool_calls", None) or []
    if not any(c["id"] in used for c in calls):
        return None
    suffix = f"_{len(messages)}"
    ids = {c["id"]: c["id"] + suffix if c["id"] in used else c["id"] for c in calls}
    raw = [{**t, "id": ids.get(t.get("id"), t.get("id"))} for t in asking.additional_kwargs.get("tool_calls") or []]
    return asking.model_copy(update={
        "tool_calls": [{**c, "id": ids[c["id"]]} for c in calls],
        "additional_kwargs": {**asking.additional_kwargs, **({"tool_calls": raw} if raw else {})},
    })


def _result(tool_call_id, name, text):
    from langchain_core.messages import ToolMessage

    return ToolMessage(text, tool_call_id=tool_call_id, name=name, status="error")
