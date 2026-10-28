"""An agent calls the MCP tools attached to it, and each call shows in run history (#20).

AI Toolkit's own AiAgentProcessor sends the run with the `mcps` blocks its
ai_agent builds; the runtime opens them over streamable HTTP against a fake
MCP server, and a scripted fake LLM decides which tool to call.
"""

import base64
import json

import pytest

from harness.aitk import create_agent, run_aiagent
from harness.fake_bedrock import FakeBedrock, tool_use
from harness.fake_llm import FakeLLM, last_user_message, offered_tools, tool_call, tool_results
from harness.fake_mcp import FakeMCP
from harness.tls import make_ca_and_server_cert

ADMIN_CAPS = ["admin_all_objects", "apply_ai_commander_command", "list_storage_passwords"]
MCP_TOKEN = "splunk-mcp-encrypted-token-123"


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
    # Signed by the test CA, which plays the org CA in the OS trust store.
    server = FakeMCP(splunk_home / "server.pem", splunk_home / "server.key").start()
    yield server
    server.stop()


def _llm_block(llm, model="qwen3:4b"):
    return {"llm": {"provider": "Custom", "model": model, "api_key": "sk-fake", "api_base": f"{llm.url}/v1",
                    "request_timeout": 30, "reasoning_effort": "NONE"}}


def _mcp_block(url, tools=("search",), name="splunk", type_="SPLUNK", **connection):
    """One entry of the `mcps` list AI Toolkit's ai_agent builds for an agent."""
    return {
        "name": name, "type": type_, "tools": list(tools), "approval_required_tools": [], "require_approval": False,
        "connection_details": {"url": url, **(connection or {"auth_token": MCP_TOKEN})},
    }


def _search_then_answer(request):
    """Call `search` first; once its result is back, answer with it."""
    results = tool_results(request)
    if not results:
        return tool_call("search", {"query": "index=main error"})
    return f"Found: {results[-1]}"


def _run(aitk, splunkd, admin, llm, mcps, model="qwen3:4b", **params):
    agent = create_agent(aitk, splunkd, admin)
    return run_aiagent(aitk, splunkd, admin, agent, _llm_block(llm, model), "Find errors", [{"x": 1}], mcps=mcps,
                       **params)


def test_agent_calls_a_selected_tool_and_its_result_reaches_the_answer(aitk, splunkd, admin, llm, mcp):
    llm.script = _search_then_answer

    df = _run(aitk, splunkd, admin, llm, [_mcp_block(mcp.url, tools=["search"])])

    assert df.iloc[0]["status"] == "success", df.iloc[0]["error"]
    assert df.iloc[0]["result_1"] == "Found: 3 events for index=main error"
    assert mcp.calls == [("search", {"query": "index=main error"})]
    # The server also has delete_index and broken; only the selected tool is offered.
    assert offered_tools(llm.requests[0]["body"]) == ["search"]


def test_tools_with_optional_arguments_work_on_openais_responses_api(aitk, splunkd, admin, llm, mcp):
    # Live on 0.7.0: LangChain sends gpt-6 models with tools to the Responses
    # API, where function tools are strict unless they say otherwise, and
    # splunk_run_query's optional arguments made OpenAI reject the schema.
    llm.script = _search_then_answer

    df = _run(aitk, splunkd, admin, llm, [_mcp_block(mcp.url)], model="gpt-6-luna")

    assert df.iloc[0]["status"] == "success", df.iloc[0]["error"]
    assert df.iloc[0]["result_1"] == "Found: 3 events for index=main error"
    assert llm.requests[0]["path"] == "/v1/responses"
    assert mcp.calls == [("search", {"query": "index=main error"})]


def test_agent_without_mcps_offers_no_tools(aitk, splunkd, admin, llm):
    llm.script = lambda request: "no tools here"

    df = _run(aitk, splunkd, admin, llm, [])

    assert df.iloc[0]["result_1"] == "no tools here"
    assert offered_tools(llm.requests[0]["body"]) == []


BASIC = base64.b64encode(b"svc_mcp:hunter2").decode()


@pytest.mark.parametrize("type_, connection, expected", [
    ("SPLUNK", {"auth_token": MCP_TOKEN}, {"Authorization": f"Bearer {MCP_TOKEN}"}),
    ("PAGERDUTY", {"auth_token": MCP_TOKEN}, {"Authorization": f"Token {MCP_TOKEN}"}),
    ("CUSTOM", {"headers": {"Authorization": "Bearer custom-bearer-1"}}, {"Authorization": "Bearer custom-bearer-1"}),
    ("CUSTOM", {"headers": {"X-API-Key": "api-key-1"}}, {"X-API-Key": "api-key-1"}),
    ("CUSTOM", {"headers": {"Authorization": f"Basic {BASIC}"}}, {"Authorization": f"Basic {BASIC}"}),
    ("CUSTOM", {"headers": {"X-Org-Token": "org-1", "X-Tenant": "soc"}}, {"X-Org-Token": "org-1", "X-Tenant": "soc"}),
], ids=["splunk-bearer", "pagerduty-token", "bearer", "api-key", "basic", "custom-headers"])
def test_each_auth_type_reaches_the_mcp_server(aitk, splunkd, admin, llm, mcp, type_, connection, expected):
    mcp.required_headers = expected
    llm.script = _search_then_answer

    df = _run(aitk, splunkd, admin, llm, [_mcp_block(mcp.url, type_=type_, **connection)])

    assert df.iloc[0]["status"] == "success", df.iloc[0]["error"]
    assert mcp.calls == [("search", {"query": "index=main error"})]


def test_rejected_credentials_fail_clearly(aitk, splunkd, admin, llm, mcp):
    mcp.required_headers = {"Authorization": "Bearer the-right-token"}

    df = _run(aitk, splunkd, admin, llm, [_mcp_block(mcp.url)])

    assert df.iloc[0]["status"] == "error"
    assert df.iloc[0]["error"] == "MCP server 'splunk' couldn't be used: it answered HTTP 401"
    assert llm.requests == []


def test_redirects_are_not_followed_so_credentials_stay_with_the_configured_server(aitk, splunkd, admin, llm,
                                                                                  splunk_home):
    elsewhere = FakeMCP(splunk_home / "server.pem", splunk_home / "server.key").start()
    configured = FakeMCP(splunk_home / "server.pem", splunk_home / "server.key").start()
    configured.redirect_to = elsewhere.url
    try:
        df = _run(aitk, splunkd, admin, llm,
                  [_mcp_block(configured.url, type_="CUSTOM", headers={"X-API-Key": "api-key-1"})])
    finally:
        configured.stop()
        elsewhere.stop()

    assert df.iloc[0]["error"] == "MCP server 'splunk' couldn't be used: it answered HTTP 307"
    assert elsewhere.requests == []


def test_the_same_tool_selected_from_two_servers_fails_clearly(aitk, splunkd, admin, llm, mcp):
    df = _run(aitk, splunkd, admin, llm, [_mcp_block(mcp.url, name="prod"), _mcp_block(mcp.url, name="test")])

    assert df.iloc[0]["status"] == "error"
    assert df.iloc[0]["error"] == ("Tool 'search' is selected from more than one MCP server ('prod', 'test'); "
                                   "deselect it on all but one")
    assert llm.requests == []


def test_tools_that_need_approval_are_refused_until_approvals_are_supported(aitk, splunkd, admin, llm, mcp):
    block = {**_mcp_block(mcp.url, tools=["search", "delete_index"]),
             "approval_required_tools": ["delete_index"], "require_approval": True}

    df = _run(aitk, splunkd, admin, llm, [block])

    assert df.iloc[0]["status"] == "error"
    assert df.iloc[0]["error"] == ("MCP server 'splunk' has tools that need approval (['delete_index']), "
                                   "and approvals aren't supported by the local agent runtime yet")
    assert mcp.calls == [] and llm.requests == []


def test_untrusted_mcp_certificate_fails_clearly(aitk, splunkd, admin, llm, tmp_path):
    rogue = make_ca_and_server_cert(tmp_path, ["localhost", "127.0.0.1"])  # its CA isn't trusted
    server = FakeMCP(rogue["cert"], rogue["key"]).start()
    try:
        df = _run(aitk, splunkd, admin, llm, [_mcp_block(server.url)])
    finally:
        server.stop()

    assert df.iloc[0]["status"] == "error"
    assert df.iloc[0]["error"] == (
        "MCP server 'splunk' couldn't be used: its TLS certificate isn't trusted by this search head; "
        "add its CA to the operating system's trust store"
    )
    assert server.calls == [] and llm.requests == []


def test_unreachable_mcp_server_fails_clearly(aitk, splunkd, admin, llm):
    df = _run(aitk, splunkd, admin, llm, [_mcp_block("https://localhost:1/mcp")])

    assert df.iloc[0]["status"] == "error"
    assert df.iloc[0]["error"] == "MCP server 'splunk' couldn't be used: ConnectError"


def test_selected_tool_the_server_lacks_is_logged_and_the_rest_still_offered(aitk, splunkd, admin, llm, mcp,
                                                                            splunk_home):
    llm.script = lambda request: "ok"

    df = _run(aitk, splunkd, admin, llm, [_mcp_block(mcp.url, tools=["search", "retired_tool"])])

    assert df.iloc[0]["status"] == "success"
    assert offered_tools(llm.requests[0]["body"]) == ["search"]
    log = (splunk_home / "var" / "log" / "splunk" / "aitk_local.log").read_text()
    assert "MCP server 'splunk' doesn't offer selected tools ['retired_tool']" in log


def test_tool_calls_show_in_run_history(aitk, splunkd, admin, llm, mcp):
    def script(request):
        done = tool_results(request)
        if not done:
            return tool_call("search", {"query": "index=main error"}, call_id="call_a")
        if len(done) == 1:
            return tool_call("broken", {}, call_id="call_b")
        return "done"

    llm.script = script

    df = _run(aitk, splunkd, admin, llm, [_mcp_block(mcp.url, tools=["search", "broken"])], conversation_tool="true")

    assert df.iloc[0]["status"] == "success", df.iloc[0]["error"]
    trace = json.loads(df.iloc[0]["trace"])
    assert [(t["tool_call_id"], t["name"], t["mcp_server"], t["arguments"], t["tool_call_status"]) for t in trace] == [
        ("call_a", "search", "splunk", {"query": "index=main error"}, "success"),
        ("call_b", "broken", "splunk", {}, "error"),
    ]
    assert trace[0]["result"] == "3 events for index=main error"
    assert "the tool broke" in trace[1]["result"]  # the server's own error, sent to the model
    for t in trace:
        assert t["tool_call_start_time"] <= t["tool_call_end_time"]


def test_logs_hold_no_mcp_credentials_arguments_or_results(aitk, splunkd, admin, llm, mcp, splunk_home):
    llm.script = _search_then_answer

    _run(aitk, splunkd, admin, llm, [
        _mcp_block(mcp.url),
        _mcp_block(mcp.url, tools=["broken"], name="custom", type_="CUSTOM", headers={"X-API-Key": "api-key-secret-9"}),
    ])

    log = (splunk_home / "var" / "log" / "splunk" / "aitk_local.log").read_text()
    for secret in (MCP_TOKEN, "api-key-secret-9", "index=main error", "3 events"):
        assert secret not in log
    assert last_user_message(llm.requests[0]["body"]) == "Find errors"


OVERFLOW = "The run outgrew the model's context window after 1 tool call; ask for less data, or use a model with a larger context"


def test_outgrowing_the_context_window_fails_clearly_and_keeps_the_trace(aitk, splunkd, admin, llm, mcp,
                                                                         splunk_home):
    def script(request):
        if not tool_results(request):
            return tool_call("search", {"query": "index=main error"})
        return 400, {"error": {"message": "This model's maximum context length is 8192 tokens",
                               "type": "invalid_request_error", "code": "context_length_exceeded"}}

    llm.script = script

    df = _run(aitk, splunkd, admin, llm, [_mcp_block(mcp.url)], conversation_tool="true")

    assert df.iloc[0]["status"] == "error"
    assert df.iloc[0]["error"] == OVERFLOW
    # A failed run's trace goes to AI Toolkit's run-failure event (run history),
    # which the fake splunkd doesn't index; our log shows it was sent.
    log = (splunk_home / "var" / "log" / "splunk" / "aitk_local.log").read_text()
    assert "success=False tool_calls=1" in log.splitlines()[-1]


def test_outgrowing_bedrocks_context_window_fails_clearly(aitk, splunkd, admin, mcp, monkeypatch):
    # Live on 0.8.0: a long Bedrock investigation ended in "ValidationException ValidationException (HTTP 400)".
    bedrock = FakeBedrock().start()
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", bedrock.url)
    bedrock.script = lambda request: (
        tool_use("search", {"query": "index=main error"}) if len(request["messages"]) == 1
        else (400, "ValidationException", "Input is too long for requested model."))
    block = {"llm": {"provider": "Bedrock", "model": "zai.glm-5", "request_timeout": 30,
                     "aws_access_key_id": "AKIAFAKE0000000001", "aws_access_token": "fake-secret",
                     "aws_region": "us-east-1"}}
    agent = create_agent(aitk, splunkd, admin)
    try:
        df = run_aiagent(aitk, splunkd, admin, agent, block, "Find errors", [{"x": 1}], mcps=[_mcp_block(mcp.url)])
    finally:
        bedrock.stop()

    assert df.iloc[0]["error"] == OVERFLOW


def _search_big(size):
    def script(request):
        if not tool_results(request):
            return tool_call("search", {"query": f"big {size}"})
        return f"got {len(tool_results(request)[-1])} characters"
    return script


def test_large_tool_results_are_cut_before_they_reach_the_model(aitk, splunkd, admin, llm, mcp):
    llm.script = _search_big(50000)

    df = _run(aitk, splunkd, admin, llm, [_mcp_block(mcp.url)], conversation_tool="true")

    sent = tool_results(llm.requests[-1]["body"])[-1]
    assert sent.startswith("e" * 16000) and not sent.startswith("e" * 16001)
    assert sent.endswith("[Result cut to 16000 of 50000 characters to fit the model's context. "
                         "Narrow the search or ask for fewer rows to see more.]")
    assert json.loads(df.iloc[0]["trace"])[0]["result"] == sent


def test_small_tool_results_are_untouched(aitk, splunkd, admin, llm, mcp):
    llm.script = _search_big(16000)

    _run(aitk, splunkd, admin, llm, [_mcp_block(mcp.url)])

    assert tool_results(llm.requests[-1]["body"])[-1] == "e" * 16000


def test_the_cut_is_configurable(aitk, splunkd, admin, llm, mcp, runtime_conf):
    runtime_conf.write_text(runtime_conf.read_text() + "\n[agent]\nmax_tool_result_chars = 100\n")
    llm.script = _search_big(500)

    _run(aitk, splunkd, admin, llm, [_mcp_block(mcp.url)])

    assert tool_results(llm.requests[-1]["body"])[-1].startswith("e" * 100 + "\n[Result cut to 100 of 500")


def test_earlier_turns_tool_results_are_cut_shorter_on_later_turns(aitk, splunkd, admin, llm, mcp):
    # Every earlier result is resent on each later turn of the session (#22).
    llm.script = _search_big(5000)
    agent = create_agent(aitk, splunkd, admin)
    session = "investigation-1"
    for prompt in ("Find errors", "Anything else?"):
        df = run_aiagent(aitk, splunkd, admin, agent, _llm_block(llm), prompt, [{"x": 1}],
                         mcps=[_mcp_block(mcp.url)], session_id=session)
        assert df.iloc[0]["status"] == "success", df.iloc[0]["error"]

    assert last_user_message(llm.requests[-1]["body"]) == "Anything else?"
    assert tool_results(llm.requests[-1]["body"]) == [
        "e" * 2000 + "\n[Result from an earlier turn, cut to 2000 of 5000 characters. Run the tool again to see more.]"]


def test_a_turn_that_times_out_during_a_tool_call_is_not_remembered(aitk, splunkd, admin, llm, mcp):
    # Remembering it would leave the session ending in a tool call with no result,
    # which models reject on the next turn.
    def script(request):
        if last_user_message(request) == "Find errors":
            return tool_call("search", {"query": "slow 6"})
        return "Hello."
    llm.script = script
    agent = create_agent(aitk, splunkd, admin, agent_timeout=2)
    df = run_aiagent(aitk, splunkd, admin, agent, _llm_block(llm), "Find errors", [{"x": 1}],
                     mcps=[_mcp_block(mcp.url)], session_id="investigation-2")
    assert "timed out" in df.iloc[0]["error"]

    df = run_aiagent(aitk, splunkd, admin, agent, _llm_block(llm), "Hello?", [{"x": 1}],
                     mcps=[_mcp_block(mcp.url)], session_id="investigation-2")

    assert df.iloc[0]["status"] == "success", df.iloc[0]["error"]
    assert [m["role"] for m in llm.requests[-1]["body"]["messages"]] == ["system", "user"]
