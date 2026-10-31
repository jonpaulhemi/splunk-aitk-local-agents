"""Instance-role connections: Bedrock models and knowledge bases with no access
keys, only a role ARN in the Bedrock account (spec #45).

The runtime takes the EC2 instance profile's credentials from IMDSv2 (a
scripted fake here), assumes the connection's role with them in the
connection's region, and calls Bedrock as that role.
"""

import pytest

from harness.aitk import create_agent, run_aiagent
from harness.fake_bedrock import ROLE_KEY_ID, ROLE_SESSION_TOKEN, FakeBedrock
from harness.fake_imds import INSTANCE_KEY_ID, INSTANCE_SESSION_TOKEN, FakeIMDS

MODEL = "anthropic.claude-3-5-sonnet-20240620-v1:0"
ROLE = "arn:aws:iam::210987654321:role/bedrock-invoke"
KEY_ID = "AKIAFAKEKEYPAIR00001"


@pytest.fixture
def admin(splunkd):
    return splunkd.add_user("admin", ["admin_all_objects", "apply_ai_commander_command"], roles=["admin"])


@pytest.fixture
def aws(monkeypatch):
    server = FakeBedrock().start()
    # botocore's own endpoint overrides; the worker inherits them from splunkd.
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", server.url)
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME", server.url)
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", server.url)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
                 "AWS_EC2_METADATA_DISABLED"):
        monkeypatch.delenv(name, raising=False)
    yield server
    server.stop()


@pytest.fixture
def imds(monkeypatch):
    server = FakeIMDS().start()
    monkeypatch.setenv("AWS_EC2_METADATA_SERVICE_ENDPOINT", server.url)
    yield server
    server.stop()


@pytest.fixture
def agent(aitk, splunkd, admin):
    return create_agent(aitk, splunkd, admin)


def _model(**overrides):
    """The `llm` block AI Toolkit's aiagent builds for an instance-role Bedrock connection:
    its stored key fields are null."""
    block = {"provider": "Bedrock", "model": MODEL, "response_variability": 0.3, "max_tokens": 400,
             "request_timeout": 30, "max_rows": 100, "reasoning_effort": "NONE",
             "aws_access_key_id": None, "aws_access_token": None, "aws_region": "us-east-1",
             "aws_role_arn": ROLE}
    return {"llm": {**block, **overrides}}


def _run(aitk, splunkd, admin, agent, llm_block):
    return run_aiagent(aitk, splunkd, admin, agent, llm_block, "hi", [{"x": 1}]).iloc[0]


# Bedrock models (#47)

@pytest.mark.parametrize("missing", [None, ""])
def test_a_keyless_model_connection_assumes_its_role_with_the_instance_credentials(aitk, splunkd, admin, agent,
                                                                                   aws, imds, missing):
    aws.script = lambda request: "All quiet."

    row = _run(aitk, splunkd, admin, agent, _model(aws_access_key_id=missing, aws_access_token=missing))

    assert row["status"] == "success", row.get("error")
    assert row["result_1"] == "All quiet."
    [assumed] = aws.assume_role_calls
    assert (assumed["key_id"], assumed["session_token"], assumed["region"], assumed["form"]["RoleArn"]) == (
        INSTANCE_KEY_ID, INSTANCE_SESSION_TOKEN, "us-east-1", ROLE)
    [call] = aws.converse_calls
    assert (call["key_id"], call["session_token"], call["region"]) == (ROLE_KEY_ID, ROLE_SESSION_TOKEN, "us-east-1")
    assert imds.token_puts >= 1 and imds.tokenless_gets == 0  # IMDSv2 only


def test_aws_keys_in_splunkds_environment_never_stand_in_for_the_instance_role(aitk, splunkd, admin, agent, aws,
                                                                               imds, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAAMBIENTKEY000001")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")

    row = _run(aitk, splunkd, admin, agent, _model())

    assert row["status"] == "success", row.get("error")
    [assumed] = aws.assume_role_calls
    assert assumed["key_id"] == INSTANCE_KEY_ID


def test_a_keyless_model_connection_without_a_role_arn_fails_before_calling_aws(aitk, splunkd, admin, agent, aws,
                                                                                imds):
    row = _run(aitk, splunkd, admin, agent, _model(aws_role_arn=None))

    assert row["status"] == "error"
    assert row["error"] == ("The Bedrock connection has no access keys, so it uses the instance role "
                            "and needs a role ARN")
    assert aws.assume_role_calls == [] and aws.converse_calls == [] and imds.token_puts == 0


def test_no_instance_profile_is_a_clear_failure(aitk, splunkd, admin, agent, aws, imds):
    imds.role = None

    row = _run(aitk, splunkd, admin, agent, _model())

    assert row["status"] == "error"
    assert row["error"] == ("The Bedrock connection uses the instance role, but no instance credentials were "
                            "found: attach an instance profile and allow IMDSv2")
    assert aws.assume_role_calls == [] and aws.converse_calls == []


def test_a_refused_assume_role_names_the_role_and_the_aws_error_code(aitk, splunkd, admin, agent, aws, imds,
                                                                     splunk_home):
    aws.reject_key_ids.add(INSTANCE_KEY_ID)

    row = _run(aitk, splunkd, admin, agent, _model())

    assert row["status"] == "error"
    assert row["error"] == (f"The Bedrock connection couldn't be used: assuming role {ROLE} failed "
                            "(InvalidClientTokenId)")
    assert aws.converse_calls == []
    log = (splunk_home / "var" / "log" / "splunk" / "aitk_local.log").read_text()
    assert "fake-instance-secret" not in log and INSTANCE_SESSION_TOKEN not in log


def test_the_log_names_the_role_each_run_assumed(aitk, splunkd, admin, agent, aws, imds, splunk_home):
    log = splunk_home / "var" / "log" / "splunk" / "aitk_local.log"
    before = log.read_text() if log.exists() else ""

    _run(aitk, splunkd, admin, agent, _model())
    _run(aitk, splunkd, admin, agent, _model())

    assert len(aws.assume_role_calls) == 2  # afresh on every run
    added = log.read_text()[len(before):]
    assert added.count(f"assumed role {ROLE} with the instance credentials") == 2


def test_a_connection_with_keys_uses_them_even_on_an_instance_with_a_profile(aitk, splunkd, admin, agent, aws, imds):
    row = _run(aitk, splunkd, admin, agent, _model(aws_access_key_id=KEY_ID, aws_access_token="fake-secret"))

    assert row["status"] == "success", row.get("error")
    [assumed] = aws.assume_role_calls
    assert (assumed["key_id"], assumed["session_token"]) == (KEY_ID, None)
    assert imds.token_puts == 0
