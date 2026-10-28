"""An agent answers from AI Toolkit's Bedrock connection, including GovCloud (#19).

botocore is pointed at a scripted fake of Bedrock Runtime's Converse API and
STS, so these runs make no AWS calls.
"""

import pytest

from harness.aitk import create_agent, run_aiagent
from harness.fake_bedrock import ROLE_KEY_ID, ROLE_SESSION_TOKEN, FakeBedrock

KEY_ID = "AKIAFAKEGOVCLOUD0001"
SECRET = "fake-govcloud-secret-key"
MODEL = "us-gov.anthropic.claude-3-5-sonnet-20240620-v1:0"
ROLE = "arn:aws-us-gov:iam::123456789012:role/bedrock-agents"


@pytest.fixture
def admin(splunkd):
    return splunkd.add_user("admin", ["admin_all_objects", "apply_ai_commander_command"], roles=["admin"])


@pytest.fixture
def bedrock(monkeypatch):
    server = FakeBedrock().start()
    # botocore's own endpoint overrides; the worker inherits them from splunkd.
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", server.url)
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", server.url)
    yield server
    server.stop()


def _bedrock_block(**overrides):
    """The `llm` block AI Toolkit's aiagent builds for a Bedrock connection."""
    block = {
        "provider": "Bedrock",
        "model": MODEL,
        "response_variability": 0.3,
        "max_tokens": 400,
        "request_timeout": 30,
        "max_rows": 100,
        "reasoning_effort": "NONE",
        "aws_access_key_id": KEY_ID,
        "aws_access_token": SECRET,
        "aws_region": "us-gov-west-1",
        "aws_role_arn": None,
    }
    return {"llm": {**block, **overrides}}


def test_aiagent_answers_from_govcloud_bedrock_with_the_connections_keys(aitk, splunkd, admin, bedrock):
    bedrock.script = lambda request: "No anomalies in the last hour."
    agent = create_agent(aitk, splunkd, admin)

    df = run_aiagent(aitk, splunkd, admin, agent, _bedrock_block(), "Check the alerts", [{"x": 1}])

    assert df.iloc[0]["status"] == "success", df.iloc[0].get("error")
    assert df.iloc[0]["result_1"] == "No anomalies in the last hour."
    call = bedrock.converse_calls[-1]
    assert (call["model"], call["key_id"], call["region"], call["session_token"]) == (
        MODEL, KEY_ID, "us-gov-west-1", None)
    assert call["body"]["system"] == [{"text": "You are terse."}]
    assert call["body"]["messages"][-1] == {"role": "user", "content": [{"text": "Check the alerts"}]}
    assert call["body"]["inferenceConfig"] == {"maxTokens": 400, "temperature": 0.3}
    assert bedrock.assume_role_calls == []


def test_a_role_arn_is_assumed_first_like_ai_toolkit_does(aitk, splunkd, admin, bedrock):
    agent = create_agent(aitk, splunkd, admin)

    df = run_aiagent(aitk, splunkd, admin, agent, _bedrock_block(aws_role_arn=ROLE), "hi", [{"x": 1}])

    assert df.iloc[0]["status"] == "success", df.iloc[0].get("error")
    [assumed] = bedrock.assume_role_calls
    assert (assumed["key_id"], assumed["region"], assumed["form"]["RoleArn"]) == (KEY_ID, "us-gov-west-1", ROLE)
    call = bedrock.converse_calls[-1]
    assert (call["key_id"], call["session_token"]) == (ROLE_KEY_ID, ROLE_SESSION_TOKEN)


def test_bad_credentials_are_a_clear_failure_and_never_logged(aitk, splunkd, admin, bedrock, splunk_home):
    bedrock.reject_key_ids.add(KEY_ID)
    agent = create_agent(aitk, splunkd, admin)

    df = run_aiagent(aitk, splunkd, admin, agent, _bedrock_block(), "my private question", [{"x": 1}])

    assert df.iloc[0]["status"] == "error"
    assert "UnrecognizedClientException" in df.iloc[0]["error"]
    assert "HTTP 403" in df.iloc[0]["error"]
    log = (splunk_home / "var" / "log" / "splunk" / "aitk_local.log").read_text()
    for secret in (SECRET, "my private question", "security token included"):
        assert secret not in log


def test_model_refusal_reports_the_aws_error_code_not_its_text(aitk, splunkd, admin, bedrock):
    bedrock.script = lambda request: (400, "ValidationException", "The provided model identifier is invalid.")
    agent = create_agent(aitk, splunkd, admin)

    df = run_aiagent(aitk, splunkd, admin, agent, _bedrock_block(), "hi", [{"x": 1}])

    assert df.iloc[0]["status"] == "error"
    assert "ValidationException" in df.iloc[0]["error"] and "HTTP 400" in df.iloc[0]["error"]
    assert "identifier is invalid" not in df.iloc[0]["error"]


def test_a_connection_without_region_or_keys_fails_before_calling_aws(aitk, splunkd, admin, bedrock):
    agent = create_agent(aitk, splunkd, admin)

    df = run_aiagent(aitk, splunkd, admin, agent, _bedrock_block(aws_region=""), "hi", [{"x": 1}])

    assert df.iloc[0]["status"] == "error"
    assert "region" in df.iloc[0]["error"]
    assert bedrock.converse_calls == [] and bedrock.assume_role_calls == []


def test_bedrock_connection_maps_onto_chatbedrockconverse(splunk_home):
    import sys

    sys.path.insert(0, str(splunk_home / "etc" / "apps" / "splunk_cloud_connect" / "bin"))
    from aitk_local import bundled

    bundled.activate()
    from aitk_local import llm as llm_mapping

    model = llm_mapping.chat_model(_bedrock_block()["llm"])

    assert type(model).__name__ == "ChatBedrockConverse"
    assert model.model_id == MODEL
    assert model.region_name == "us-gov-west-1"
    assert model.aws_access_key_id.get_secret_value() == KEY_ID
    assert model.aws_secret_access_key.get_secret_value() == SECRET
    assert model.aws_session_token is None
    assert model.temperature == 0.3
    assert model.max_tokens == 400
    assert model.client.meta.region_name == "us-gov-west-1"
