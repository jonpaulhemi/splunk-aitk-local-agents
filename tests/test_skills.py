"""Agents load the skills attached to them on demand (#39).

AI Toolkit resolves an agent's skills to {name, description, skill_text} and
sends them with every invoke. The model sees each skill's name and
description, and reads a skill's full instructions with `load_skill`; the
skills it loads come back for the "Loaded skills" panel on that reply.
"""

import json
import uuid

import pytest

from harness.aitk import create_agent, run_aiagent
from harness.fake_llm import FakeLLM, offered_tools, tool_call, tool_results
from harness.fake_mcp import FakeMCP

RESUME_PROMPT = "Tool approval decisions submitted."  # what Launchpad sends as the prompt of a resume

TRIAGE = {"name": "phishing_triage", "description": "Use when a user reports a suspicious email.",
          "skill_text": "1. Pull the message headers.\n2. Check the sender domain's age.\n3. Rate it 1-5."}
FORECAST = {"name": "capacity_forecast", "description": "Use when asked how long disk space will last.",
            "skill_text": "Fit a linear trend over 30 days of df output."}


@pytest.fixture
def admin(splunkd):
    return splunkd.add_user("admin", ["admin_all_objects", "apply_ai_commander_command"], roles=["admin"])


@pytest.fixture
def llm():
    server = FakeLLM().start()
    yield server
    server.stop()


@pytest.fixture
def agent(aitk, splunkd, admin):
    return create_agent(aitk, splunkd, admin)


def _llm_block(llm):
    return {"llm": {"provider": "Custom", "model": "qwen3:4b", "api_key": "sk-fake", "api_base": f"{llm.url}/v1",
                    "request_timeout": 30, "reasoning_effort": "NONE"}}


def _run(aitk, splunkd, admin, agent, llm, skills, prompt="Is this email phishing?", **params):
    df = run_aiagent(aitk, splunkd, admin, agent, _llm_block(llm), prompt, [{"x": 1}], skills=skills,
                     conversation_tool="true", **params)
    return df.iloc[0]


def _system_prompt(request):
    return next(m["content"] for m in request["messages"] if m["role"] == "system")


def _skills(row):
    # AI Toolkit stores an empty list as no value.
    return json.loads(row["skills"] or "[]")


def _loads_then_answers(name):
    def script(request):
        results = tool_results(request)
        if not results:
            return tool_call("load_skill", {"name": name})
        return f"Following: {results[-1]}"
    return script


def test_the_model_sees_each_skill_and_loads_one_when_it_applies(aitk, splunkd, admin, agent, llm):
    llm.script = _loads_then_answers("phishing_triage")

    row = _run(aitk, splunkd, admin, agent, llm, [TRIAGE, FORECAST])

    assert row["status"] == "success", row["error"]
    first = llm.requests[0]["body"]
    assert "load_skill" in offered_tools(first)
    prompt = _system_prompt(first)
    assert "You are terse." in prompt
    for skill in (TRIAGE, FORECAST):
        assert skill["name"] in prompt and skill["description"] in prompt
        assert skill["skill_text"] not in prompt
    # The full instructions reach the model, untouched.
    assert row["result_1"] == f"Following: {TRIAGE['skill_text']}"
    assert _skills(row) == [{"name": "phishing_triage"}]


def test_a_skill_load_shows_in_the_trace_as_a_skill(aitk, splunkd, admin, agent, llm):
    llm.script = _loads_then_answers("phishing_triage")

    row = _run(aitk, splunkd, admin, agent, llm, [TRIAGE])

    [call] = json.loads(row["trace"])
    assert (call["type"], call["name"], call["arguments"], call["tool_call_status"]) == (
        "skill", "load_skill", {"name": "phishing_triage"}, "success")


def test_an_unknown_skill_gets_a_tool_error_naming_the_skills_and_the_run_carries_on(aitk, splunkd, admin, agent,
                                                                                     llm):
    llm.script = _loads_then_answers("phish_triage")

    row = _run(aitk, splunkd, admin, agent, llm, [TRIAGE, FORECAST])

    assert row["status"] == "success", row["error"]
    assert row["result_1"] == ("Following: There is no skill named 'phish_triage'. "
                               "Skills: phishing_triage, capacity_forecast")
    [call] = json.loads(row["trace"])
    assert call["tool_call_status"] == "error"
    assert _skills(row) == []


def test_a_skill_loaded_twice_is_reported_once(aitk, splunkd, admin, agent, llm):
    def script(request):
        done = tool_results(request)
        if len(done) < 2:
            return tool_call("load_skill", {"name": "phishing_triage"}, call_id=f"call_{len(done)}")
        return "done"
    llm.script = script

    row = _run(aitk, splunkd, admin, agent, llm, [TRIAGE])

    assert _skills(row) == [{"name": "phishing_triage"}]


def test_loaded_skills_are_reported_on_the_turn_that_loaded_them(aitk, splunkd, admin, agent, llm):
    session = f"skills-{uuid.uuid4().hex}"
    llm.script = _loads_then_answers("phishing_triage")
    first = _run(aitk, splunkd, admin, agent, llm, [TRIAGE], session_id=session)
    llm.script = lambda request: "Still rated 4."

    second = _run(aitk, splunkd, admin, agent, llm, [TRIAGE], prompt="And the rating?", session_id=session)

    assert _skills(first) == [{"name": "phishing_triage"}]
    assert second["status"] == "success", second["error"]
    assert _skills(second) == []
    # The instructions loaded earlier are still in the conversation.
    messages = llm.requests[-1]["body"]["messages"]
    assert any(m["role"] == "tool" and m["content"] == TRIAGE["skill_text"] for m in messages)


def test_an_agent_without_skills_is_unchanged(aitk, splunkd, admin, agent, llm):
    llm.script = lambda request: "plain"

    row = _run(aitk, splunkd, admin, agent, llm, [])

    assert row["result_1"] == "plain"
    assert offered_tools(llm.requests[0]["body"]) == []
    assert _system_prompt(llm.requests[0]["body"]) == "You are terse."


@pytest.fixture
def mcp(splunk_home):
    server = FakeMCP(splunk_home / "server.pem", splunk_home / "server.key").start()
    yield server
    server.stop()


def test_a_run_paused_for_approval_and_its_resumption_each_report_their_own_loads(aitk, splunkd, admin, agent,
                                                                                 llm, mcp):
    # Load a skill, then ask to delete an index (which needs approval); once
    # approved, load another skill and answer.
    def script(request):
        done = tool_results(request)
        steps = [tool_call("load_skill", {"name": "phishing_triage"}, call_id="call_1"),
                 tool_call("delete_index", {"name": "old"}, call_id="call_2"),
                 tool_call("load_skill", {"name": "capacity_forecast"}, call_id="call_3")]
        return steps[len(done)] if len(done) < len(steps) else "Done."
    llm.script = script
    session = f"skills-{uuid.uuid4().hex}"
    gated = {"name": "splunk", "type": "SPLUNK", "tools": ["delete_index"], "approval_required_tools": ["delete_index"],
             "require_approval": True, "connection_details": {"url": mcp.url, "auth_token": "splunk-mcp-token"}}
    both = [TRIAGE, FORECAST]

    paused = _run(aitk, splunkd, admin, agent, llm, both, session_id=session, mcps=[gated])
    decisions = {"decisions": [{"tool_call_id": "call_2", "approved": True}]}
    resumed = _run(aitk, splunkd, admin, agent, llm, both, prompt=RESUME_PROMPT, session_id=session, mcps=[gated],
                   hitl_input=json.dumps(decisions))

    assert paused["hitl_status"] == "waiting_for_approval", paused["error"]
    assert _skills(paused) == [{"name": "phishing_triage"}]
    assert resumed["status"] == "success", resumed["error"]
    assert mcp.calls == [("delete_index", {"name": "old"})]
    assert _skills(resumed) == [{"name": "capacity_forecast"}]
