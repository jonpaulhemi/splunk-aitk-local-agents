"""Agents search the knowledge bases attached to them (#40).

AI Toolkit sends each attached AWS_KB knowledge base as a `vector_dbs` entry
with its description, kb_id and credentials. The runtime offers the model one
search tool per knowledge base; a knowledge base search calls Bedrock Agent
Runtime's Retrieve (a scripted fake here) and returns the best-matching chunks
with where they came from.
"""

import json

import pytest

from harness.aitk import create_agent, run_aiagent
from harness.fake_bedrock import ROLE_KEY_ID, ROLE_SESSION_TOKEN, FakeBedrock, chunk
from harness.fake_llm import FakeLLM, offered_tools, tool_call, tool_results
from harness.fake_mcp import FakeMCP

KEY_ID = "AKIAFAKEKBUSER000001"
SECRET = "fake-kb-secret-key"
RUNBOOKS = {"type": "AWS_KB", "kb_id": "EJGJR4SKJL", "description": "On-call runbooks and the escalation rota.",
            "connection_details": {"region": "us-west-2", "aws_access_key_id": KEY_ID,
                                   "aws_access_key_token": SECRET, "role_arn": ""}}
TOOL = "search_knowledge_base_EJGJR4SKJL"
ROLE = "arn:aws:iam::123456789012:role/aitk-kb-search"
WITH_ROLE = {**RUNBOOKS, "connection_details": {**RUNBOOKS["connection_details"], "role_arn": ROLE}}


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


@pytest.fixture
def aws(monkeypatch):
    server = FakeBedrock().start()
    # botocore's own endpoint overrides; the worker inherits them from splunkd.
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME", server.url)
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", server.url)
    yield server
    server.stop()


def _llm_block(llm):
    return {"llm": {"provider": "Custom", "model": "qwen3:4b", "api_key": "sk-fake", "api_base": f"{llm.url}/v1",
                    "request_timeout": 30, "reasoning_effort": "NONE"}}


def _run(aitk, splunkd, admin, agent, llm, vector_dbs, prompt="Who do I page for a disk alert?", **params):
    df = run_aiagent(aitk, splunkd, admin, agent, _llm_block(llm), prompt, [{"x": 1}], vector_dbs=vector_dbs,
                     conversation_tool="true", **params)
    return df.iloc[0]


def _searches_then_answers(tool=TOOL, query="disk alert escalation"):
    def script(request):
        results = tool_results(request)
        if not results:
            return tool_call(tool, {"query": query})
        return f"From the runbooks: {results[-1]}"
    return script


def _tool_description(request, name):
    return next(t["function"]["description"] for t in request["tools"] if t["function"]["name"] == name)


def test_the_agent_searches_a_knowledge_base_and_gets_chunks_with_their_sources(aitk, splunkd, admin, agent, llm,
                                                                               aws):
    aws.retrieve_script = lambda kb_id, request: [
        chunk("Disk alerts page the storage on-call first.", "s3://runbooks/disk.md", 0.82),
        chunk("Escalate to the infra lead after 30 minutes.", "s3://runbooks/escalation.md", 0.61)]
    llm.script = _searches_then_answers()

    row = _run(aitk, splunkd, admin, agent, llm, [RUNBOOKS])

    assert row["status"] == "success", row["error"]
    first = llm.requests[0]["body"]
    assert offered_tools(first) == [TOOL]
    assert _tool_description(first, TOOL) == RUNBOOKS["description"]
    [call] = aws.retrieve_calls
    assert (call["kb_id"], call["key_id"], call["region"]) == ("EJGJR4SKJL", KEY_ID, "us-west-2")
    assert call["body"]["retrievalQuery"] == {"text": "disk alert escalation"}
    assert call["body"]["retrievalConfiguration"] == {"vectorSearchConfiguration": {"numberOfResults": 5}}
    answer = row["result_1"]
    for text, source in [("Disk alerts page the storage on-call first.", "s3://runbooks/disk.md"),
                         ("Escalate to the infra lead after 30 minutes.", "s3://runbooks/escalation.md")]:
        assert text in answer and source in answer
    assert answer.index("disk.md") < answer.index("escalation.md")
    [traced] = json.loads(row["trace"])
    assert (traced["name"], traced["arguments"], traced["tool_call_status"]) == (
        TOOL, {"query": "disk alert escalation"}, "success")


def test_the_chunks_per_search_come_from_this_apps_config(aitk, splunkd, admin, agent, llm, aws, runtime_conf):
    runtime_conf.write_text(runtime_conf.read_text() + "\n[knowledge_base]\nchunks_per_search = 2\n")
    llm.script = _searches_then_answers()

    _run(aitk, splunkd, admin, agent, llm, [RUNBOOKS])

    [call] = aws.retrieve_calls
    assert call["body"]["retrievalConfiguration"] == {"vectorSearchConfiguration": {"numberOfResults": 2}}


def test_a_large_result_is_cut_and_the_model_is_told(aitk, splunkd, admin, agent, llm, aws):
    aws.retrieve_script = lambda kb_id, request: [chunk("x" * 5000, f"s3://runbooks/{i}.md", 0.5) for i in range(3)]
    llm.script = _searches_then_answers()

    row = _run(aitk, splunkd, admin, agent, llm, [RUNBOOKS])

    result = json.loads(row["trace"])[0]["result"]
    body, note = result.rsplit("\n", 1)
    assert len(body) == 8000
    assert note.startswith("[Result cut to 8000 of ") and "narrower query" in note


def test_no_matching_chunks_says_so(aitk, splunkd, admin, agent, llm, aws):
    llm.script = _searches_then_answers()

    row = _run(aitk, splunkd, admin, agent, llm, [RUNBOOKS])

    assert row["result_1"] == "From the runbooks: No chunks matched the query."


@pytest.mark.parametrize("status, code", [(400, "ThrottlingException"), (404, "ResourceNotFoundException"),
                                          (403, "AccessDeniedException")])
def test_a_failed_search_is_a_tool_error_naming_only_the_aws_error_code(aitk, splunkd, admin, agent, llm, aws,
                                                                       status, code):
    aws.retrieve_script = lambda kb_id, request: (status, code, f"denied for {KEY_ID} on {request}")
    llm.script = _searches_then_answers()

    row = _run(aitk, splunkd, admin, agent, llm, [RUNBOOKS])

    assert row["status"] == "success", row["error"]
    assert row["result_1"] == f"From the runbooks: The knowledge base search failed: {code}"
    [traced] = json.loads(row["trace"])
    assert traced["tool_call_status"] == "error"
    assert KEY_ID not in row["trace"] and SECRET not in row["trace"]


def test_a_slow_search_times_out_as_a_tool_error(aitk, splunkd, admin, agent, llm, aws, runtime_conf):
    runtime_conf.write_text(runtime_conf.read_text() + "\n[knowledge_base]\nsearch_timeout_seconds = 1\n")
    aws.delay = 3
    llm.script = _searches_then_answers()

    row = _run(aitk, splunkd, admin, agent, llm, [RUNBOOKS])

    assert row["status"] == "success", row["error"]
    assert row["result_1"] == "From the runbooks: The knowledge base search timed out after 1s"


def test_each_knowledge_base_gets_its_own_tool_and_region(aitk, splunkd, admin, agent, llm, aws):
    policies = {**RUNBOOKS, "kb_id": "POLICY0001", "description": "Security policies.",
                "connection_details": {**RUNBOOKS["connection_details"], "region": "eu-west-1"}}

    def script(request):
        done = tool_results(request)
        steps = [tool_call(TOOL, {"query": "disk"}, call_id="call_1"),
                 tool_call("search_knowledge_base_POLICY0001", {"query": "passwords"}, call_id="call_2")]
        return steps[len(done)] if len(done) < len(steps) else "done"
    llm.script = script

    row = _run(aitk, splunkd, admin, agent, llm, [RUNBOOKS, policies, dict(RUNBOOKS)])

    assert row["status"] == "success", row["error"]
    assert offered_tools(llm.requests[0]["body"]) == ["search_knowledge_base_EJGJR4SKJL",
                                                      "search_knowledge_base_POLICY0001"]
    assert [(c["kb_id"], c["region"]) for c in aws.retrieve_calls] == [("EJGJR4SKJL", "us-west-2"),
                                                                       ("POLICY0001", "eu-west-1")]


def test_other_vector_db_types_and_no_knowledge_bases_offer_no_search(aitk, splunkd, admin, agent, llm, aws):
    llm.script = lambda request: "plain"

    row = _run(aitk, splunkd, admin, agent, llm, [{"type": "PINECONE", "description": "?", "kb_id": "x"}])

    assert row["result_1"] == "plain"
    assert offered_tools(llm.requests[0]["body"]) == []
    assert aws.retrieve_calls == []


def test_a_connection_without_keys_never_falls_back_to_other_aws_credentials(aitk, splunkd, admin, agent, llm, aws,
                                                                            monkeypatch):
    # Credentials in splunkd's environment (or an instance role) must not stand in for missing ones.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAAMBIENTKEY000001")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    keyless = {**RUNBOOKS, "connection_details": {"region": "us-west-2", "aws_access_key_id": "",
                                                  "aws_access_key_token": "", "role_arn": ""}}
    llm.script = _searches_then_answers()

    row = _run(aitk, splunkd, admin, agent, llm, [keyless])

    assert row["result_1"] == ("From the runbooks: The knowledge base connection needs a region, "
                               "an access key ID and a secret key")
    assert aws.retrieve_calls == []


def test_a_search_never_waits_for_approval_even_beside_gated_tools(aitk, splunkd, admin, agent, llm, aws,
                                                                  splunk_home):
    mcp = FakeMCP(splunk_home / "server.pem", splunk_home / "server.key").start()
    try:
        gated = {"name": "splunk", "type": "SPLUNK", "tools": ["delete_index"],
                 "approval_required_tools": ["delete_index"], "require_approval": True,
                 "connection_details": {"url": mcp.url, "auth_token": "splunk-mcp-token"}}
        llm.script = _searches_then_answers()

        row = _run(aitk, splunkd, admin, agent, llm, [RUNBOOKS], mcps=[gated], session_id="kb-approvals")
    finally:
        mcp.stop()

    assert row["status"] == "success", row["error"]
    assert not row["hitl_status"]
    assert len(aws.retrieve_calls) == 1


# Assuming the IAM role (#41)

def test_a_role_arn_is_assumed_once_and_searches_use_its_credentials(aitk, splunkd, admin, agent, llm, aws):
    def script(request):
        done = tool_results(request)
        if len(done) < 2:
            return tool_call(TOOL, {"query": f"q{len(done)}"}, call_id=f"call_{len(done)}")
        return "done"
    llm.script = script

    row = _run(aitk, splunkd, admin, agent, llm, [WITH_ROLE])

    assert row["status"] == "success", row["error"]
    [assumed] = aws.assume_role_calls
    assert (assumed["key_id"], assumed["region"], assumed["form"]["RoleArn"]) == (KEY_ID, "us-west-2", ROLE)
    assert [(c["key_id"], c["session_token"]) for c in aws.retrieve_calls] == [(ROLE_KEY_ID, ROLE_SESSION_TOKEN)] * 2


def test_a_role_that_cant_be_assumed_fails_the_run_naming_the_role_and_code(aitk, splunkd, admin, agent, llm, aws,
                                                                           splunk_home):
    aws.reject_key_ids.add(KEY_ID)

    row = _run(aitk, splunkd, admin, agent, llm, [WITH_ROLE])

    assert row["status"] == "error"
    assert row["error"] == (f"Knowledge base EJGJR4SKJL couldn't be used: assuming role {ROLE} failed "
                            "(InvalidClientTokenId)")
    assert llm.requests == [] and aws.retrieve_calls == []
    log = (splunk_home / "var" / "log" / "splunk" / "aitk_local.log").read_text()
    assert SECRET not in log and "security token included" not in log


def test_knowledge_bases_in_different_regions_with_and_without_a_role_work_together(aitk, splunkd, admin, agent,
                                                                                   llm, aws):
    policies = {**RUNBOOKS, "kb_id": "POLICY0001", "description": "Security policies.",
                "connection_details": {**RUNBOOKS["connection_details"], "region": "eu-west-1"}}

    def script(request):
        done = tool_results(request)
        steps = [tool_call(TOOL, {"query": "disk"}, call_id="call_1"),
                 tool_call("search_knowledge_base_POLICY0001", {"query": "passwords"}, call_id="call_2")]
        return steps[len(done)] if len(done) < len(steps) else "done"
    llm.script = script

    row = _run(aitk, splunkd, admin, agent, llm, [WITH_ROLE, policies])

    assert row["status"] == "success", row["error"]
    assert [(c["kb_id"], c["region"], c["key_id"]) for c in aws.retrieve_calls] == [
        ("EJGJR4SKJL", "us-west-2", ROLE_KEY_ID), ("POLICY0001", "eu-west-1", KEY_ID)]


MANAGED_ONLY = ("Incompatible configuration: vectorSearchConfiguration is not supported for managed knowledge "
                "bases. Use managedSearchConfiguration instead.")


def test_a_managed_knowledge_base_is_searched_with_managed_search(aitk, splunkd, admin, agent, llm, aws):
    # Bedrock's managed knowledge bases refuse vector search settings (live on 0.12.0).
    def managed(kb_id, request):
        if "vectorSearchConfiguration" in request["retrievalConfiguration"]:
            return 400, "ValidationException", MANAGED_ONLY
        return [chunk("Disk alerts page the storage on-call first.", "s3://runbooks/disk.md", 0.82)]
    aws.retrieve_script = managed

    def script(request):
        done = tool_results(request)
        if len(done) < 2:
            return tool_call(TOOL, {"query": f"q{len(done)}"}, call_id=f"call_{len(done)}")
        return f"From the runbooks: {done[-1]}"
    llm.script = script

    row = _run(aitk, splunkd, admin, agent, llm, [RUNBOOKS])

    assert row["status"] == "success", row["error"]
    assert "Disk alerts page the storage on-call first." in row["result_1"]
    # Found out once per run, then searched the managed way straight away.
    assert [c["body"]["retrievalConfiguration"] for c in aws.retrieve_calls] == [
        {"vectorSearchConfiguration": {"numberOfResults": 5}},
        {"managedSearchConfiguration": {"numberOfResults": 5}},
        {"managedSearchConfiguration": {"numberOfResults": 5}}]
