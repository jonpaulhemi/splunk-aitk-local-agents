"""Approvals (human in the loop): tool calls that wait for someone's decision (#23).

A tool listed in its MCP connection's approval_required_tools doesn't run
until someone approves it. The run pauses with LangGraph's interrupt() right
after the model asks for the call, and the session checkpointer (memory.py)
keeps it paused. AI Toolkit shows the pending calls in Launchpad and its
email, then resumes the session with `hitl_response`: one
{tool_call_id, approved} decision per call and an optional correction.
"""

WAITING = "waiting_for_approval"
REJECTED = "The user rejected this tool call, so it didn't run."
UNDECIDED = "No one approved this tool call, so it didn't run."


class NothingWaiting(Exception):
    def __init__(self):
        super().__init__("There's no run waiting for approval in this session; it may have finished or expired. "
                         "Ask again to start a new run.")


class Gate:
    """The graph's post_model_hook: pause before any tool call in `gated` runs,
    and on resume pass the decisions on."""

    def __init__(self, gated, servers, trace):
        self.gated = gated
        self.servers = servers
        self.trace = trace
        # A correction given while approving every call: shown to the model on
        # its next call, since the approved calls' results must come first.
        self.note = None

    def __call__(self, state):
        from langchain_core.messages import ToolMessage
        from langgraph.types import interrupt

        calls = [c for c in getattr(state["messages"][-1], "tool_calls", None) or [] if c["name"] in self.gated]
        if not calls:
            return {}
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
        return {"messages": [ToolMessage(result, tool_call_id=c["id"], name=c["name"], status="error")
                             for c in rejected]}

    def _pending(self, call):
        # The field names AI Toolkit's Launchpad and its approval email read.
        return {"tool_call_id": call["id"], "name": call["name"], "tool_name": call["name"],
                "mcp": self.servers.get(call["name"]), "arguments": call["args"], "tool_arguments": call["args"],
                "tool_call_status": "waiting_for_human_approval"}

    def take_note(self):
        note, self.note = self.note, None
        return note


def unanswered(pending):
    """Results for paused calls that were never decided, for when a new prompt arrives instead."""
    from langchain_core.messages import ToolMessage

    return [ToolMessage(UNDECIDED, tool_call_id=p["tool_call_id"], name=p["name"], status="error") for p in pending]
