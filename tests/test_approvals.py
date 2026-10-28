"""Tools that need approval pause the run until someone decides (#23).

A run that calls a tool listed in its MCP connection's approval_required_tools
pauses before the tool runs. AI Toolkit shows the pending calls in Launchpad
(and in its email), then resumes the session with an invoke carrying
`hitl_response`: one {tool_call_id, approved} decision per call, plus an
optional correction, and no task prompt. Each invoke runs in a fresh worker
process, so every resume here is also a resume after a handler restart.
"""

import json
import uuid

import pytest

from harness.aitk import create_agent, run_aiagent
from harness.fake_bedrock import FakeBedrock, tool_use
from harness.fake_llm import FakeLLM, tool_call, tool_results
from harness.fake_mcp import FakeMCP

ADMIN_CAPS = ["admin_all_objects", "apply_ai_commander_command", "list_storage_passwords"]
RESUME_PROMPT = "Tool approval decisions submitted."  # what Launchpad sends as the prompt of a resume


@pytest.fixture
def admin(splunkd):
    return splunkd.add_user("admin", ADMIN_CAPS, roles=["admin"])


@pytest.fixture
def llm():
    server = FakeLLM().start()
    yield server
    server.stop()


@pytest.fixture
def mcp(splunk_home):
    server = FakeMCP(splunk_home / "server.pem", splunk_home / "server.key").start()
    yield server
    server.stop()


@pytest.fixture
def agent(aitk, splunkd, admin):
    return create_agent(aitk, splunkd, admin)


def _llm_block(llm):
    return {"llm": {"provider": "Custom", "model": "qwen3:4b", "api_key": "sk-fake", "api_base": f"{llm.url}/v1",
                    "request_timeout": 30, "reasoning_effort": "NONE"}}


def _mcp_block(url, approval_required_tools=("delete_index",)):
    """A Splunk MCP connection whose delete_index tool needs approval, as AI Toolkit's ai_agent builds it."""
    return {"name": "splunk", "type": "SPLUNK", "tools": ["search", "delete_index"],
            "approval_required_tools": list(approval_required_tools), "require_approval": bool(approval_required_tools),
            "connection_details": {"url": url, "auth_token": "splunk-mcp-token"}}


def _deletes_then_reports(request):
    """Ask to delete index `old`; once the call has a result, report it."""
    results = tool_results(request)
    if not results:
        return {**tool_call("delete_index", {"name": "old"}), "content": "I'll delete the old index."}
    return f"Outcome: {results[-1]}"


def _run(aitk, splunkd, admin, agent, llm, mcp, prompt, session, llm_block=None, mcp_block=None, **params):
    df = run_aiagent(aitk, splunkd, admin, agent, llm_block or _llm_block(llm), prompt, [{"x": 1}],
                     mcps=[mcp_block or _mcp_block(mcp.url)], session_id=session, conversation_tool="true", **params)
    return df.iloc[0]


def _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved, prompt=RESUME_PROMPT, tool_call_id="call_1",
            **params):
    decisions = {"decisions": [{"tool_call_id": tool_call_id, "approved": approved}]}
    return _run(aitk, splunkd, admin, agent, llm, mcp, prompt, session, hitl_input=json.dumps(decisions), **params)


def _paused(aitk, splunkd, admin, agent, llm, mcp):
    llm.script = _deletes_then_reports
    session = f"approval-{uuid.uuid4().hex}"
    row = _run(aitk, splunkd, admin, agent, llm, mcp, "Clean up the old index", session)
    assert row["status"] == "success", row["error"]
    assert row["hitl_status"] == "waiting_for_approval"
    return session, row


def test_a_tool_that_needs_approval_pauses_the_run_before_it_runs(aitk, splunkd, admin, agent, llm, mcp,
                                                                   splunk_home):
    _, row = _paused(aitk, splunkd, admin, agent, llm, mcp)

    assert mcp.calls == []
    assert row["result_1"] == "I'll delete the old index."  # the approver's context, in Launchpad and the email
    pending = json.loads(row["hitl_pending"])
    assert pending == [{
        "tool_call_id": "call_1", "name": "delete_index", "tool_name": "delete_index", "mcp": "splunk",
        "arguments": {"name": "old"}, "tool_arguments": {"name": "old"},
        "tool_call_status": "waiting_for_human_approval",
    }]
    # AI Toolkit's own email formatting finds the tool in it.
    assert aitk.util.format_pending_tools(pending) == "• delete_index (splunk)"
    log = (splunk_home / "var" / "log" / "splunk" / "aitk_local.log").read_text()
    assert "waiting_for_approval" in log and '"old"' not in log


def test_tools_that_dont_need_approval_run_without_pausing(aitk, splunkd, admin, agent, llm, mcp):
    llm.script = lambda request: (tool_call("search", {"query": "index=main"}) if not tool_results(request)
                                  else "found")

    row = _run(aitk, splunkd, admin, agent, llm, mcp, "Search", f"approval-{uuid.uuid4().hex}")

    assert row["status"] == "success" and not row["hitl_status"]
    assert row["result_1"] == "found"
    assert mcp.calls == [("search", {"query": "index=main"})]


def test_approving_runs_the_tool_once_and_the_run_carries_on(aitk, splunkd, admin, agent, llm, mcp):
    session, _ = _paused(aitk, splunkd, admin, agent, llm, mcp)
    asked = len(llm.requests)

    row = _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=True)

    assert row["status"] == "success", row["error"]
    assert not row["hitl_status"]
    assert mcp.calls == [("delete_index", {"name": "old"})]
    assert row["result_1"] == "Outcome: deleted old"
    assert len(llm.requests) == asked + 1  # the model isn't asked again for the call it already made
    trace = json.loads(row["trace"])
    assert [(t["tool_call_id"], t["name"], t["tool_call_status"]) for t in trace] == [
        ("call_1", "delete_index", "success")]


def test_rejecting_skips_the_tool_and_tells_the_model(aitk, splunkd, admin, agent, llm, mcp):
    session, _ = _paused(aitk, splunkd, admin, agent, llm, mcp)

    row = _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=False)

    assert row["status"] == "success", row["error"]
    assert mcp.calls == []
    assert tool_results(llm.requests[-1]["body"]) == ["The user rejected this tool call, so it didn't run."]
    trace = json.loads(row["trace"])
    assert [(t["tool_call_id"], t["name"], t["tool_call_status"]) for t in trace] == [
        ("call_1", "delete_index", "rejected_by_human")]


def test_a_correction_reaches_the_model(aitk, splunkd, admin, agent, llm, mcp):
    session, _ = _paused(aitk, splunkd, admin, agent, llm, mcp)

    _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=False, prompt="Archive it instead.")

    assert tool_results(llm.requests[-1]["body"]) == [
        "The user rejected this tool call, so it didn't run. Their note: Archive it instead."]


def test_a_correction_with_everything_approved_reaches_the_model_too(aitk, splunkd, admin, agent, llm, mcp):
    session, _ = _paused(aitk, splunkd, admin, agent, llm, mcp)

    _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=True, prompt="Tell me how much space it freed.")

    sent = llm.requests[-1]["body"]["messages"]
    assert sent[-1] == {"role": "user", "content": "Note from the approver: Tell me how much space it freed."}
    assert mcp.calls == [("delete_index", {"name": "old"})]


def test_a_call_without_a_decision_is_treated_as_rejected(aitk, splunkd, admin, agent, llm, mcp):
    session, _ = _paused(aitk, splunkd, admin, agent, llm, mcp)

    row = _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=True, tool_call_id="call_other")

    assert row["status"] == "success", row["error"]
    assert mcp.calls == []


def test_resuming_a_session_with_nothing_waiting_fails_clearly(aitk, splunkd, admin, agent, llm, mcp):
    row = _resume(aitk, splunkd, admin, agent, llm, mcp, f"approval-{uuid.uuid4().hex}", approved=True)

    assert row["status"] == "error"
    assert row["error"] == ("There's no run waiting for approval in this session; it may have finished or "
                            "expired. Ask again to start a new run.")
    assert mcp.calls == [] and llm.requests == []


def test_a_paused_run_can_only_be_resumed_once(aitk, splunkd, admin, agent, llm, mcp):
    session, _ = _paused(aitk, splunkd, admin, agent, llm, mcp)
    _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=True)

    row = _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=True)

    assert row["status"] == "error" and "no run waiting for approval" in row["error"]
    assert mcp.calls == [("delete_index", {"name": "old"})]


def test_the_session_remembers_the_approved_run(aitk, splunkd, admin, agent, llm, mcp):
    session, _ = _paused(aitk, splunkd, admin, agent, llm, mcp)
    _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=True)
    llm.script = lambda request: "ok"

    _run(aitk, splunkd, admin, agent, llm, mcp, "What did you do?", session)

    assert tool_results(llm.requests[-1]["body"]) == ["deleted old"]


def test_a_rejection_holds_even_if_the_tool_stopped_needing_approval_meanwhile(aitk, splunkd, admin, agent, llm,
                                                                                mcp):
    session, _ = _paused(aitk, splunkd, admin, agent, llm, mcp)

    row = _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=False,
                  mcp_block=_mcp_block(mcp.url, approval_required_tools=()))

    assert row["status"] == "success", row["error"]
    assert mcp.calls == []


def test_approvals_on_bedrock(aitk, splunkd, admin, agent, llm, mcp, monkeypatch):
    bedrock = FakeBedrock().start()
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", bedrock.url)

    def script(request):
        results = [part["toolResult"] for m in request["messages"] for part in m["content"] if "toolResult" in part]
        return tool_use("delete_index", {"name": "old"}) if not results else "done"
    bedrock.script = script
    block = {"llm": {"provider": "Bedrock", "model": "zai.glm-5", "request_timeout": 30,
                     "aws_access_key_id": "AKIAFAKE0000000001", "aws_access_token": "fake-secret",
                     "aws_region": "us-gov-west-1"}}
    session = f"approval-{uuid.uuid4().hex}"
    try:
        paused = _run(aitk, splunkd, admin, agent, llm, mcp, "Clean up", session, llm_block=block)
        row = _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=False, tool_call_id="tooluse_1",
                      llm_block=block)
    finally:
        bedrock.stop()

    assert paused["hitl_status"] == "waiting_for_approval"
    assert row["status"] == "success", row["error"]
    assert row["result_1"] == "done"
    assert mcp.calls == []
    sent = bedrock.converse_calls[-1]["body"]["messages"]
    result = next(part["toolResult"] for m in sent for part in m["content"] if "toolResult" in part)
    assert result["toolUseId"] == "tooluse_1" and result["status"] == "error"


def test_a_new_prompt_instead_of_a_decision_leaves_the_calls_unrun(aitk, splunkd, admin, agent, llm, mcp):
    # In Launchpad, typing a new message instead of approving or denying.
    session, _ = _paused(aitk, splunkd, admin, agent, llm, mcp)
    llm.script = lambda request: "hi"

    row = _run(aitk, splunkd, admin, agent, llm, mcp, "Never mind, just say hi.", session)

    assert row["status"] == "success", row["error"]
    assert row["result_1"] == "hi" and not row["hitl_status"]
    assert mcp.calls == []
    assert tool_results(llm.requests[-1]["body"]) == ["No one approved this tool call, so it didn't run."]
    late = _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=True)
    assert late["status"] == "error" and mcp.calls == []


def _deletes_and_searches(request):
    """Ask for an ungated search and a gated delete together; then report."""
    results = tool_results(request)
    if not results:
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_s", "type": "function", "function": {"name": "search", "arguments": '{"query": "x"}'}},
            {"id": "call_d", "type": "function", "function": {"name": "delete_index", "arguments": '{"name": "old"}'}},
        ]}
    return f"Outcome: {sorted(results)}"


def test_calls_asked_for_alongside_a_gated_one_wait_with_it_and_run_on_approval(aitk, splunkd, admin, agent, llm,
                                                                                 mcp):
    llm.script = _deletes_and_searches
    session = f"approval-{uuid.uuid4().hex}"
    paused = _run(aitk, splunkd, admin, agent, llm, mcp, "Search and clean up", session)
    assert paused["hitl_status"] == "waiting_for_approval" and mcp.calls == []

    row = _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=True, tool_call_id="call_d")

    assert row["status"] == "success", row["error"]
    assert sorted(mcp.calls) == [("delete_index", {"name": "old"}), ("search", {"query": "x"})]


def test_a_new_prompt_after_a_mixed_pause_answers_every_waiting_call(aitk, splunkd, admin, agent, llm, mcp):
    llm.script = _deletes_and_searches
    session = f"approval-{uuid.uuid4().hex}"
    _run(aitk, splunkd, admin, agent, llm, mcp, "Search and clean up", session)
    llm.script = lambda request: "hi"

    row = _run(aitk, splunkd, admin, agent, llm, mcp, "Never mind.", session)

    assert row["status"] == "success", row["error"]
    assert mcp.calls == []
    assert sorted(tool_results(llm.requests[-1]["body"])) == [
        "No one approved this tool call, so it didn't run.", "This tool call didn't run."]


def test_an_approved_tool_never_runs_twice_even_if_the_resumed_run_fails(aitk, splunkd, admin, agent, llm, mcp):
    session, _ = _paused(aitk, splunkd, admin, agent, llm, mcp)
    llm.script = lambda request: (500, {"error": {"message": "model exploded"}})
    failed = _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=True)
    assert failed["status"] == "error"

    again = _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=True)
    llm.script = lambda request: "ok"
    later = _run(aitk, splunkd, admin, agent, llm, mcp, "What happened?", session)

    assert again["status"] == "error" and "no run waiting for approval" in again["error"]
    assert mcp.calls == [("delete_index", {"name": "old"})]
    assert later["status"] == "success", later["error"]
    assert tool_results(llm.requests[-1]["body"]) == ["deleted old"]


def test_a_model_reusing_tool_call_ids_across_turns_still_gets_asked(aitk, splunkd, admin, agent, llm, mcp):
    # Some OpenAI-compatible servers number tool calls per response, so call_1 comes back every turn.
    session, _ = _paused(aitk, splunkd, admin, agent, llm, mcp)
    _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=True)
    llm.script = lambda request: (tool_call("delete_index", {"name": "older"})
                                  if tool_results(request) == ["deleted old"] else "done")

    paused = _run(aitk, splunkd, admin, agent, llm, mcp, "And the older one", session)
    pending = json.loads(paused["hitl_pending"])
    row = _resume(aitk, splunkd, admin, agent, llm, mcp, session, approved=True,
                  tool_call_id=pending[0]["tool_call_id"])

    assert paused["hitl_status"] == "waiting_for_approval"
    assert row["status"] == "success", row["error"]
    assert mcp.calls == [("delete_index", {"name": "old"}), ("delete_index", {"name": "older"})]
