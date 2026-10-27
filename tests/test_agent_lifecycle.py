"""Agent Launchpad lifecycle against the local runtime (#16).

AI Toolkit's own AgentManager and AgentStatusProcessor create, poll and delete
an agent; the runtime answers the AgentCore lifecycle calls itself.
"""

import json

import httpx
import pytest

from harness.aitk import runtime_base_url, searchinfo

ADMIN_CAPS = ["admin_all_objects", "apply_ai_commander_command", "list_storage_passwords"]
AGENTS = "aitk_agent_collection"


@pytest.fixture
def admin(splunkd):
    return splunkd.add_user("admin", ADMIN_CAPS, roles=["admin"])


@pytest.fixture
def manager(aitk, splunkd, admin):
    return aitk.agent_manager.AgentManager(searchinfo(splunkd, admin))


def _status_processor(aitk, splunkd, admin, agent_name, action):
    from ai_commander.constants import PARAMS

    options = {PARAMS: {"agent_name": agent_name, "action": action, "user_name": "admin"}}
    processor = aitk.agent_status.AgentStatusProcessor(options, searchinfo(splunkd, admin))
    processor.poll_interval_secs = 0
    processor.max_polls = 3
    processor.df = None
    return processor


def _stored(splunkd, name):
    docs = [d for d in splunkd.kvstore.get(("Splunk_ML_Toolkit", AGENTS), {}).values() if d["name"] == name]
    return docs[0] if docs else None


def _runtime_token(aitk, splunkd, admin):
    return aitk.util.fetch_cloud_connect_details_with_system_token(searchinfo(splunkd, admin))["scs_token"]


def test_launchpad_create_gets_runtime_and_memory_ids(manager, splunkd):
    assert manager.create_agent({"name": "triage", "system_prompt": "Be brief."}) is True

    params = _stored(splunkd, "triage")["details"]["versions"][0]
    assert params["runtime_params"]["runtime_id"]
    assert params["runtime_params"]["version"]
    assert params["memory_params"]["memory_id"]


def test_status_polling_marks_a_new_agent_available(aitk, splunkd, admin, manager):
    manager.create_agent({"name": "triage"})

    processor = _status_processor(aitk, splunkd, admin, "triage", "create")
    processor.process()

    assert processor.df.iloc[0]["status"] == "created"
    assert _stored(splunkd, "triage")["details"]["versions"][0]["state"] == "Available"


def test_delete_then_status_polling_removes_the_agent(aitk, splunkd, admin, manager):
    manager.create_agent({"name": "triage"})

    ok, _ = manager.delete_agent("triage", notify_runtime=True)
    processor = _status_processor(aitk, splunkd, admin, "triage", "delete")
    processor.process()

    assert ok is True
    assert processor.df.iloc[0]["status"] == "deleted"
    assert _stored(splunkd, "triage") is None


def test_runtimes_survive_a_restart_of_the_handler_processes(aitk, splunkd, admin, manager):
    manager.create_agent({"name": "triage"})
    splunkd.load_routes()  # new handler instances, as after a splunkd restart

    processor = _status_processor(aitk, splunkd, admin, "triage", "create")
    processor.process()

    assert processor.df.iloc[0]["status"] == "created"


def test_unknown_runtime_reports_not_found(aitk, splunkd, admin):
    token = _runtime_token(aitk, splunkd, admin)

    response = httpx.get(
        f"{runtime_base_url(splunkd)}/agent/aws_agentcore?runtime_ids=nope&memory_ids=nope",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["agents"][0]["status"] == "not_found"
    assert response.json()["memories"][0]["status"] == "not_found"


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "Splunk {admin}"])
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "agent/aws_agentcore"),
        ("GET", "agent/aws_agentcore?runtime_ids=x"),
        ("DELETE", "agent/aws_agentcore/x"),
        ("GET", "encryption/public-key"),
    ],
)
def test_every_call_needs_the_runtime_token(aitk, splunkd, admin, auth, method, path):
    _runtime_token(aitk, splunkd, admin)  # make sure a token exists
    headers = {"Authorization": auth.format(admin=admin)} if auth else {}

    response = httpx.request(
        method, f"{runtime_base_url(splunkd)}/{path}", headers=headers,
        json={"agent_name": "x", "app_version": "6.2.0", "agent_type": "aitk"} if method == "POST" else None,
    )

    assert response.status_code == 401


def test_public_key_is_accepted_by_aitk_salt_encryption_and_is_stable(aitk, splunkd, admin):
    processor = aitk.ai_agent.AiAgentProcessor.__new__(aitk.ai_agent.AiAgentProcessor)
    processor.searchinfo = searchinfo(splunkd, admin)
    processor.scs_token = processor.scs_token_expiry = None

    processor._populate_scs_calling_params()
    first = processor.encryption_public_key
    encrypted = processor._encrypt_salt("0f5c1b9e-salt")
    splunkd.load_routes()
    processor._populate_scs_calling_params()

    assert encrypted
    assert processor.encryption_public_key == first
    assert "PRIVATE" not in first
