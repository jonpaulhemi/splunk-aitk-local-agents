"""An agent with structured output configured answers in that shape (#21).

AI Toolkit 6.2.0 sets `structured_output` through its agents REST API (there
is no Launchpad form for it yet), sends it in the invoke payload, and writes a
successful answer to the result column as JSON.
"""

import json

import pytest

from harness.aitk import create_agent, run_aiagent
from harness.fake_bedrock import FakeBedrock, tool_use
from harness.fake_bedrock import offered_tools as bedrock_tools
from harness.fake_llm import FakeLLM, offered_tools, tool_call

ADMIN_CAPS = ["admin_all_objects", "apply_ai_commander_command"]
TRIAGE = {"fields": {
    "severity": {"type": "string", "required": True, "allowed_values": ["low", "medium", "high"],
                 "description": "How bad it is"},
    "hosts": {"type": "string", "required": True, "is_array": True, "array_max_items": 5},
    "event_count": {"type": "integer", "required": False, "minimum": 0},
    "details": {"type": "object", "required": False},
}}
ANSWER = {"severity": "high", "hosts": ["web01", "web02"], "event_count": 42}


@pytest.fixture
def admin(splunkd):
    return splunkd.add_user("admin", ADMIN_CAPS, roles=["admin"])


@pytest.fixture
def llm():
    server = FakeLLM().start()
    yield server
    server.stop()


def _llm_block(llm, model="qwen3:4b"):
    return {"llm": {"provider": "Custom", "model": model, "api_key": "sk-fake", "api_base": f"{llm.url}/v1",
                    "request_timeout": 30, "reasoning_effort": "NONE"}}


def _answers_with(structured):
    """A model that answers in prose, then fills the structured output tool when it's offered."""
    def script(request):
        if "structured_output" in offered_tools(request):
            return tool_call("structured_output", structured)
        return "Two web hosts are failing logins; it looks serious."
    return script


def _run(aitk, splunkd, admin, llm_block, structured_output=TRIAGE):
    agent = create_agent(aitk, splunkd, admin, structured_output=structured_output)
    return run_aiagent(aitk, splunkd, admin, agent, llm_block, "Triage the alerts", [{"x": 1}])


@pytest.mark.parametrize("model", ["qwen3:4b", "gpt-6-luna"], ids=["chat-completions", "responses-api"])
def test_answer_follows_the_schema_and_lands_in_the_result_as_json(aitk, splunkd, admin, llm, model):
    llm.script = _answers_with(ANSWER)

    df = _run(aitk, splunkd, admin, _llm_block(llm, model))

    assert df.iloc[0]["status"] == "success", df.iloc[0]["error"]
    assert json.loads(df.iloc[0]["result_1"]) == ANSWER
    schema = next(t["function"]["parameters"] for t in llm.requests[-1]["body"]["tools"]
                  if t["function"]["name"] == "structured_output")
    assert sorted(schema["required"]) == ["hosts", "severity"]
    assert schema["properties"]["severity"]["enum"] == ["low", "medium", "high"]
    assert schema["properties"]["severity"]["description"] == "How bad it is"
    assert schema["properties"]["hosts"] == {"type": "array", "items": {"type": "string"}, "maxItems": 5}
    assert schema["properties"]["event_count"] == {"type": "integer", "minimum": 0}


def test_agent_without_structured_output_answers_in_prose(aitk, splunkd, admin, llm):
    llm.script = _answers_with(ANSWER)

    df = _run(aitk, splunkd, admin, _llm_block(llm), structured_output=None)

    assert df.iloc[0]["result_1"] == "Two web hosts are failing logins; it looks serious."
    assert all(offered_tools(r["body"]) == [] for r in llm.requests)


@pytest.mark.parametrize("structured, problem", [
    ({"severity": "catastrophic", "hosts": ["web01"]}, "'severity' isn't one of the allowed values"),
    ({"severity": "high"}, "'hosts' is missing"),
    ({"severity": "high", "hosts": "web01"}, "'hosts' has the wrong type"),
    ({"severity": "high", "hosts": [f"web{i}" for i in range(9)]}, "'hosts' fails maxItems"),
], ids=["not-allowed", "missing", "wrong-type", "too-many"])
def test_output_that_breaks_the_schema_fails_clearly(aitk, splunkd, admin, llm, splunk_home, structured, problem):
    llm.script = _answers_with(structured)

    df = _run(aitk, splunkd, admin, _llm_block(llm))

    assert df.iloc[0]["status"] == "error"
    assert df.iloc[0]["error"] == f"The model's answer didn't match the agent's structured output: {problem}"
    log = (splunk_home / "var" / "log" / "splunk" / "aitk_local.log").read_text()
    assert "catastrophic" not in log and "web01" not in log


def test_a_model_that_wont_fill_the_schema_fails_clearly(aitk, splunkd, admin, llm):
    llm.script = lambda request: "I'd rather just talk."

    df = _run(aitk, splunkd, admin, _llm_block(llm))

    assert df.iloc[0]["status"] == "error"
    assert df.iloc[0]["error"] == "The model didn't return the agent's structured output"


def test_structured_output_on_bedrock(aitk, splunkd, admin, monkeypatch):
    bedrock = FakeBedrock().start()
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", bedrock.url)
    bedrock.script = lambda request: (tool_use("structured_output", ANSWER)
                                      if "structured_output" in bedrock_tools(request) else "prose")
    block = {"llm": {"provider": "Bedrock", "model": "us.anthropic.claude-sonnet", "request_timeout": 30,
                     "aws_access_key_id": "AKIAFAKE0000000001", "aws_access_token": "fake-secret",
                     "aws_region": "us-gov-west-1"}}
    try:
        df = _run(aitk, splunkd, admin, block)
    finally:
        bedrock.stop()

    assert df.iloc[0]["status"] == "success", df.iloc[0]["error"]
    assert json.loads(df.iloc[0]["result_1"]) == ANSWER
