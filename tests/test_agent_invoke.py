"""An agent answers from AI Toolkit's custom OpenAI-compatible connection (#18).

AI Toolkit's own AiAgentProcessor encrypts and sends the run; the runtime
decrypts it and runs a LangGraph agent against a scripted fake LLM server.
"""

import base64
import json

import httpx
import pytest

from harness.aitk import create_agent, run_aiagent, runtime_base_url, searchinfo
from harness.fake_llm import FakeLLM, last_user_message

ADMIN_CAPS = ["admin_all_objects", "apply_ai_commander_command", "list_storage_passwords"]
API_KEY = "sk-fake-llm-secret-123"


@pytest.fixture
def admin(splunkd):
    return splunkd.add_user("admin", ADMIN_CAPS, roles=["admin"])


@pytest.fixture
def llm():
    server = FakeLLM().start()
    yield server
    server.stop()


def _llm_block(llm, **overrides):
    """The `llm` block AI Toolkit's aiagent builds for a custom OpenAI-compatible connection."""
    block = {
        "provider": "Custom",
        "model": "qwen3:4b",
        "connection_name": "qwen-local",
        "response_variability": 0.2,
        "max_tokens": 512,
        "request_timeout": 30,
        "max_rows": 100,
        "reasoning_effort": "NONE",
        "api_key": API_KEY,
        "api_base": f"{llm.url}/v1",
    }
    return {"llm": {**block, **overrides}}


_agent = create_agent
_run = run_aiagent


def test_aiagent_gets_the_models_answer(aitk, splunkd, admin, llm):
    llm.script = lambda request: "Three failed logins from 10.0.0.7."
    agent = _agent(aitk, splunkd, admin)

    df = _run(aitk, splunkd, admin, agent, _llm_block(llm), "Summarise the alerts", [{"x": 1}])

    assert df.iloc[0]["result_1"] == "Three failed logins from 10.0.0.7."
    assert df.iloc[0]["status"] == "success"
    sent = llm.requests[-1]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["body"]["model"] == "qwen3:4b"
    assert sent["headers"]["authorization"] == f"Bearer {API_KEY}"
    assert sent["body"]["messages"][0] == {"role": "system", "content": "You are terse."}
    assert last_user_message(sent["body"]) == "Summarise the alerts"


def test_rows_run_in_parallel_and_each_gets_its_own_answer(aitk, splunkd, admin, llm):
    llm.delay = 1.0
    llm.script = lambda request: f"echo: {last_user_message(request)}"
    agent = _agent(aitk, splunkd, admin)

    df = _run(aitk, splunkd, admin, agent, _llm_block(llm), "Check {host}", [{"host": h} for h in ("a", "b", "c")])

    assert list(df["result_1"]) == ["echo: Check a", "echo: Check b", "echo: Check c"]
    assert llm.max_in_flight > 1


def test_a_stale_ssl_cert_file_in_splunkds_environment_does_not_break_runs(aitk, splunkd, admin, llm, monkeypatch, splunk_home):
    # Live on 0.4.0: splunkd's environment can carry SSL_CERT_FILE pointing at a
    # missing file. AI Toolkit drops it when its modules load, so only some
    # handler processes still had it, and their rows failed with FileNotFoundError.
    llm.script = lambda request: "fine"
    agent = _agent(aitk, splunkd, admin)
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/cacert.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent/certs")

    df = _run(aitk, splunkd, admin, agent, _llm_block(llm), "hi", [{"x": 1}])

    assert df.iloc[0]["status"] == "success", df.iloc[0].get("error")
    assert df.iloc[0]["result_1"] == "fine"
    log = (splunk_home / "var" / "log" / "splunk" / "aitk_local.log").read_text()
    assert "ignoring SSL_CERT_FILE: /nonexistent/cacert.pem does not exist" in log


def test_model_error_comes_back_as_a_failed_row(aitk, splunkd, admin, llm, splunk_home):
    llm.script = lambda request: (500, {"error": {"message": "model exploded"}})
    agent = _agent(aitk, splunkd, admin)

    df = _run(aitk, splunkd, admin, agent, _llm_block(llm), "hi", [{"x": 1}])

    assert df.iloc[0]["status"] == "error"
    assert "LLM call failed" in df.iloc[0]["error"]
    assert "HTTP 500" in df.iloc[0]["error"]
    assert "model exploded" not in df.iloc[0]["error"]
    log = (splunk_home / "var" / "log" / "splunk" / "aitk_local.log").read_text()
    assert "failed at:" in log and "HTTPStatusError" in log
    assert "model exploded" not in log


def test_agent_timeout_bounds_the_whole_run(aitk, splunkd, admin, llm):
    import time

    llm.delay = 20
    agent = _agent(aitk, splunkd, admin, agent_timeout=2)

    started = time.monotonic()
    df = _run(aitk, splunkd, admin, agent, _llm_block(llm), "hi", [{"x": 1}])

    assert df.iloc[0]["status"] == "error"
    assert "timed out after 2s" in df.iloc[0]["error"]
    assert time.monotonic() - started < 15


def test_unsupported_provider_is_a_clear_failure(aitk, splunkd, admin, llm):
    agent = _agent(aitk, splunkd, admin)

    df = _run(aitk, splunkd, admin, agent, {"llm": {"provider": "Ollama", "model": "qwen3:4b"}}, "hi", [{"x": 1}])

    assert df.iloc[0]["status"] == "error"
    assert "Ollama" in df.iloc[0]["error"]
    assert llm.requests == []


def test_connection_without_a_model_is_a_clear_failure(aitk, splunkd, admin, llm):
    agent = _agent(aitk, splunkd, admin)

    df = _run(aitk, splunkd, admin, agent, _llm_block(llm, model=None), "hi", [{"x": 1}])

    assert df.iloc[0]["status"] == "error"
    assert "no model" in df.iloc[0]["error"]
    assert llm.requests == []


def test_approval_resume_fails_clearly_instead_of_answering_nothing(aitk, splunkd, admin, llm):
    agent = _agent(aitk, splunkd, admin)

    df = _run(aitk, splunkd, admin, agent, _llm_block(llm), "Tool approval decisions submitted.", [{"x": 1}],
              hitl_input=json.dumps({"decisions": [{"tool_call_id": "t1", "approved": True}]}))

    assert df.iloc[0]["status"] == "error"
    assert "Approvals" in df.iloc[0]["error"]
    assert llm.requests == []


def _token(aitk, splunkd, admin):
    return aitk.util.fetch_cloud_connect_details_with_system_token(searchinfo(splunkd, admin))["scs_token"]


def test_invoke_needs_the_runtime_token(aitk, splunkd, admin):
    _token(aitk, splunkd, admin)

    response = httpx.post(f"{runtime_base_url(splunkd)}/agent/aws_agentcore/invoke",
                          headers={"Authorization": "Bearer wrong"}, json={"payload": "x", "salt": "y"})

    assert response.status_code == 401


def test_undecryptable_payload_is_a_failed_run_not_a_hang(aitk, splunkd, admin):
    token = _token(aitk, splunkd, admin)

    response = httpx.post(
        f"{runtime_base_url(splunkd)}/agent/aws_agentcore/invoke",
        headers={"Authorization": f"Bearer {token}"},
        json={"payload": base64.b64encode(b"junk").decode(), "is_new_encryption": True, "salt": "bm9wZQ=="},
        timeout=30,
    )

    final = json.loads(response.text.strip().splitlines()[-1])
    assert response.status_code == 200
    assert final["status"] == "completed"
    assert final["success"] is False
    assert "decrypt" in final["error_message"]


def test_logs_hold_no_secrets_or_payload_values(aitk, splunkd, admin, llm, splunk_home):
    agent = _agent(aitk, splunkd, admin)
    llm.script = lambda request: "the secret answer is 42"
    _run(aitk, splunkd, admin, agent, _llm_block(llm), "my private question", [{"x": 1}])
    llm.script = lambda request: (400, {"error": {"message": f"bad request: {last_user_message(request)}"}})
    _run(aitk, splunkd, admin, agent, _llm_block(llm), "my failing private question", [{"x": 1}])
    log = (splunk_home / "var" / "log" / "splunk" / "aitk_local.log").read_text()

    for secret in (API_KEY, _token(aitk, splunkd, admin), "my private question", "my failing private question",
                   "the secret answer", "You are terse"):
        assert secret not in log
    assert "success=True" in log and "success=False" in log
    assert "not yet supported" not in log  # empty knowledge bases aren't "ignored"


def test_custom_connection_maps_onto_chatopenai(splunk_home):
    import sys

    sys.path.insert(0, str(splunk_home / "etc" / "apps" / "splunk_cloud_connect" / "bin"))
    from aitk_local import bundled

    bundled.activate()
    from aitk_local import llm as llm_mapping

    model = llm_mapping.chat_model({
        "provider": "Custom", "model": "qwen3:4b", "api_key": API_KEY,
        "api_base": "https://ollama.internal:11434/v1", "response_variability": 0.2,
        "max_tokens": 512, "request_timeout": 45, "reasoning_effort": "NONE",
    })

    assert type(model).__name__ == "ChatOpenAI"
    assert model.model_name == "qwen3:4b"
    assert model.openai_api_base == "https://ollama.internal:11434/v1"
    assert model.openai_api_key.get_secret_value() == API_KEY
    assert model.temperature == 0.2
    assert model.max_tokens == 512
    assert model.request_timeout == 45
    assert model.reasoning_effort is None
