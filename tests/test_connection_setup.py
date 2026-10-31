"""Cloud Connect's setup endpoint for instance-role connections (#49, #50).

An admin creates, lists, edits and deletes Bedrock model and knowledge base
connections that have a role ARN and no access keys. The endpoint writes them
into AI Toolkit's own store through AI Toolkit's REST API, so these tests read
them back with AI Toolkit's own code. AWS (STS, Bedrock) and IMDS are fakes.
"""

import json
from urllib.parse import quote

import httpx
import pytest

from harness.aitk import searchinfo
from harness.fake_bedrock import ROLE_KEY_ID, FakeBedrock
from harness.fake_imds import INSTANCE_KEY_ID, FakeIMDS

ROLE = "arn:aws:iam::210987654321:role/bedrock-invoke"
MODEL = "anthropic.claude-3-5-sonnet-20240620-v1:0"
AITK_CAPS = ["apply_ai_commander_command", "edit_ai_commander_config", "list_ai_commander_config",
             "edit_agent_connections"]


@pytest.fixture
def admin(splunkd):
    return splunkd.add_user("admin", ["admin_all_objects", "list_storage_passwords", *AITK_CAPS], roles=["admin"])


@pytest.fixture
def power(splunkd):
    return splunkd.add_user("power", AITK_CAPS, roles=["power"])


@pytest.fixture
def aws(monkeypatch):
    server = FakeBedrock().start()
    for service in ("BEDROCK", "BEDROCK_RUNTIME", "BEDROCK_AGENT_RUNTIME", "STS"):
        monkeypatch.setenv(f"AWS_ENDPOINT_URL_{service}", server.url)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    yield server
    server.stop()


@pytest.fixture
def imds(monkeypatch):
    server = FakeIMDS().start()
    monkeypatch.setenv("AWS_EC2_METADATA_SERVICE_ENDPOINT", server.url)
    yield server
    server.stop()


def _call(splunkd, token, method, path="", body=None):
    return httpx.request(method, f"{splunkd.uri}/services/aitk_local/connections{path}",
                         headers={"Authorization": f"Splunk {token}"}, params={"output_mode": "json"},
                         content=None if body is None else json.dumps(body), verify=False, timeout=120)


def _aitk_model(aitk, splunkd, token, name):
    """The connection as AI Toolkit resolves it for a run, secrets filled in."""
    manager = aitk.llm_config_manager.ConfigManager(search_info=searchinfo(splunkd, token))
    return manager.get_llm_config(connection_name=name)


def _aitk_models(aitk, splunkd, token):
    manager = aitk.llm_config_manager.ConfigManager(search_info=searchinfo(splunkd, token))
    return [c["name"] for c in manager.list_llm_configs()]


GLM = {"name": "glm", "region": "us-east-1", "role_arn": ROLE, "model": MODEL, "temperature": 0.2,
       "max_tokens": 500, "request_timeout": 90, "description": "Bedrock in the shared account"}


# Model connections (#49)

def test_only_admins_can_use_the_endpoint(splunkd, power, aws, imds):
    for method, path, body in [("GET", "", None), ("POST", "/models", GLM),
                               ("DELETE", f"/models/{quote('glm_instance_role')}", None),
                               ("POST", "/models/available", {"region": "us-east-1", "role_arn": ROLE})]:
        assert _call(splunkd, power, method, path, body).status_code == 403
    assert aws.assume_role_calls == []


def test_an_admin_creates_a_model_connection_that_ai_toolkit_stores_without_keys(aitk, splunkd, admin, aws, imds):
    response = _call(splunkd, admin, "POST", "/models", GLM)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["connection"]["name"] == "glm_instance_role"
    assert body["test"] == {"ok": True, "message": f"Assumed role {ROLE} and Bedrock answered"}
    stored = _aitk_model(aitk, splunkd, admin, "glm_instance_role")
    assert (stored["provider"], stored["model"]) == ("Bedrock", MODEL)
    details = stored["connection_details"]
    assert details["aws_access_key_id"] is None and details["aws_secret_access_key"] is None
    assert (details["aws_region_name"], details["aws_role_name"]) == ("us-east-1", ROLE)
    assert details["request_timeout"] == 90
    assert stored["llm_params"]["response_variability"] == 0.2 and stored["llm_params"]["max_tokens"] == 500
    assert stored["description"] == "Bedrock in the shared account"
    # The test assumed the role with the instance credentials, then made a one-token call as the role.
    [assumed] = aws.assume_role_calls
    assert (assumed["key_id"], assumed["region"], assumed["form"]["RoleArn"]) == (INSTANCE_KEY_ID, "us-east-1", ROLE)
    [call] = aws.converse_calls
    assert (call["model"], call["key_id"]) == (MODEL, ROLE_KEY_ID)
    assert call["body"]["inferenceConfig"]["maxTokens"] == 1


def test_a_name_that_already_has_the_suffix_isnt_suffixed_twice(aitk, splunkd, admin, aws, imds):
    response = _call(splunkd, admin, "POST", "/models", {**GLM, "name": "glm_instance_role"})

    assert response.status_code == 201, response.text
    assert "glm_instance_role" in _aitk_models(aitk, splunkd, admin)


@pytest.mark.parametrize("arn", ["", "bedrock-invoke", "arn:aws:iam::12345:role/x",
                                 "arn:aws:iam::210987654321:user/x", "arn:aws:sts::210987654321:role/x"])
def test_a_role_arn_that_isnt_an_iam_role_arn_is_rejected(aitk, splunkd, admin, aws, imds, arn):
    response = _call(splunkd, admin, "POST", "/models", {**GLM, "role_arn": arn})

    assert response.status_code == 400
    assert response.json()["message"] == ("The role ARN must look like arn:<partition>:iam::<12-digit account>:"
                                          "role/<name>")
    assert aws.assume_role_calls == [] and _aitk_models(aitk, splunkd, admin) == []


def test_a_govcloud_or_c2s_role_arn_is_accepted(splunkd, admin, aws, imds):
    for name, partition, region in [("govcloud", "aws-us-gov", "us-gov-west-1"), ("c2s", "aws-iso", "us-iso-east-1")]:
        arn = f"arn:{partition}:iam::123456789012:role/path/bedrock"
        response = _call(splunkd, admin, "POST", "/models",
                         {**GLM, "name": name, "region": region, "role_arn": arn})
        assert response.status_code == 201, response.text


@pytest.mark.parametrize("name", ["glm 5", "5glm", "glm-5", "glm(5)"])
def test_a_name_ai_toolkit_wouldnt_accept_is_rejected(aitk, splunkd, admin, aws, imds, name):
    # AI Toolkit's own forms allow only these names, and its secret lookups break on others.
    response = _call(splunkd, admin, "POST", "/models", {**GLM, "name": name})

    assert response.status_code == 400
    assert response.json()["message"] == ("The name must start with a letter or underscore and contain only "
                                          "letters, digits and underscores")


@pytest.mark.parametrize("field", ["name", "region", "model"])
def test_a_missing_field_is_rejected(aitk, splunkd, admin, aws, imds, field):
    response = _call(splunkd, admin, "POST", "/models", {**GLM, field: ""})

    assert response.status_code == 400
    assert field.replace("_", " ") in response.json()["message"]
    assert _aitk_models(aitk, splunkd, admin) == []


def test_a_failed_assume_role_is_reported_as_such_and_nothing_is_saved(aitk, splunkd, admin, aws, imds):
    aws.reject_key_ids.add(INSTANCE_KEY_ID)

    response = _call(splunkd, admin, "POST", "/models", GLM)

    assert response.status_code == 422
    assert response.json()["test"] == {
        "ok": False, "step": "assume_role", "code": "InvalidClientTokenId",
        "message": f"Couldn't assume role {ROLE} (InvalidClientTokenId). Check the role's trust policy and "
                   "the instance profile's sts:AssumeRole permission."}
    assert aws.converse_calls == [] and _aitk_models(aitk, splunkd, admin) == []


def test_a_bedrock_refusal_after_assuming_the_role_is_reported_as_such(aitk, splunkd, admin, aws, imds):
    aws.script = lambda request: (403, "AccessDeniedException", "not authorized")

    response = _call(splunkd, admin, "POST", "/models", GLM)

    assert response.status_code == 422
    assert response.json()["test"] == {
        "ok": False, "step": "bedrock", "code": "AccessDeniedException",
        "message": f"Assumed role {ROLE}, but Bedrock refused the call (AccessDeniedException). Check the "
                   "role's Bedrock permissions and model access."}
    assert _aitk_models(aitk, splunkd, admin) == []


def test_no_instance_credentials_is_reported_as_such(splunkd, admin, aws, imds):
    imds.role = None

    response = _call(splunkd, admin, "POST", "/models", GLM)

    assert response.status_code == 422
    assert response.json()["test"]["step"] == "instance_credentials"
    assert response.json()["test"]["message"] == ("No instance credentials were found. Attach an instance profile "
                                                  "to this search head and allow IMDSv2.")


def test_save_anyway_saves_after_a_failed_test(aitk, splunkd, admin, aws, imds):
    aws.reject_key_ids.add(INSTANCE_KEY_ID)

    response = _call(splunkd, admin, "POST", "/models", {**GLM, "save_anyway": True})

    assert response.status_code == 201, response.text
    assert response.json()["test"]["ok"] is False
    assert "glm_instance_role" in _aitk_models(aitk, splunkd, admin)


def test_testing_without_saving(aitk, splunkd, admin, aws, imds):
    response = _call(splunkd, admin, "POST", "/models/test", GLM)

    assert response.status_code == 200
    assert response.json()["test"]["ok"] is True
    assert _aitk_models(aitk, splunkd, admin) == []


def test_a_duplicate_name_is_refused(splunkd, admin, aws, imds):
    assert _call(splunkd, admin, "POST", "/models", GLM).status_code == 201

    response = _call(splunkd, admin, "POST", "/models", GLM)

    assert response.status_code == 409


def _key_based_connection(splunkd, admin, name="keys"):
    body = {"name": name, "provider": "Bedrock", "model": MODEL,
            "connection_details": {"region": "us-east-1", "aws_access_key_id": "AKIAFAKE000000000001",
                                   "aws_access_token": "fake-secret", "role_arn": ""}}
    response = httpx.post(f"{splunkd.uri}/servicesNS/admin/Splunk_ML_Toolkit/mltk/aicommander",
                          headers={"Authorization": f"Splunk {admin}"}, json=body, verify=False)
    assert response.status_code == 200, response.text


def test_the_list_shows_only_instance_role_connections_with_their_settings(splunkd, admin, aws, imds):
    _key_based_connection(splunkd, admin)
    _call(splunkd, admin, "POST", "/models", GLM)

    response = _call(splunkd, admin, "GET")

    assert response.status_code == 200
    assert response.json()["models"] == [{
        "name": "glm_instance_role", "region": "us-east-1", "role_arn": ROLE, "model": MODEL,
        "temperature": 0.2, "max_tokens": 500, "request_timeout": 90,
        "description": "Bedrock in the shared account"}]


def test_an_admin_edits_a_model_connection_and_its_keys_stay_empty(aitk, splunkd, admin, aws, imds):
    _call(splunkd, admin, "POST", "/models", GLM)
    other_role = "arn:aws:iam::210987654321:role/bedrock-invoke-2"

    response = _call(splunkd, admin, "POST", f"/models/{quote('glm_instance_role')}",
                     {**GLM, "region": "us-west-2", "role_arn": other_role, "max_tokens": 900})

    assert response.status_code == 200, response.text
    stored = _aitk_model(aitk, splunkd, admin, "glm_instance_role")
    details = stored["connection_details"]
    assert details["aws_access_key_id"] is None and details["aws_secret_access_key"] is None
    assert (details["aws_region_name"], details["aws_role_name"]) == ("us-west-2", other_role)
    assert stored["llm_params"]["max_tokens"] == 900
    [listed] = _call(splunkd, admin, "GET").json()["models"]
    assert (listed["region"], listed["role_arn"]) == ("us-west-2", other_role)


def test_an_admin_deletes_a_model_connection(aitk, splunkd, admin, aws, imds):
    _call(splunkd, admin, "POST", "/models", GLM)

    response = _call(splunkd, admin, "DELETE", f"/models/{quote('glm_instance_role')}")

    assert response.status_code == 200, response.text
    assert _aitk_models(aitk, splunkd, admin) == []
    assert _call(splunkd, admin, "GET").json()["models"] == []
    assert not [k for k in splunkd.passwords if k[2] == "glm_instance_role"]


def test_key_based_connections_cant_be_edited_or_deleted_here(aitk, splunkd, admin, aws, imds):
    _key_based_connection(splunkd, admin)

    for method, body in [("POST", {**GLM, "name": "keys"}), ("DELETE", None)]:
        response = _call(splunkd, admin, method, "/models/keys", body)
        assert response.status_code == 404
        assert response.json()["message"] == "keys isn't an instance-role connection made in Cloud Connect"
    assert "keys" in _aitk_models(aitk, splunkd, admin)


def test_the_models_the_assumed_role_can_see_are_listed(splunkd, admin, aws, imds):
    response = _call(splunkd, admin, "POST", "/models/available", {"region": "us-east-1", "role_arn": ROLE})

    assert response.status_code == 200, response.text
    assert response.json() == {"models": [
        {"id": "anthropic.claude-3-5-sonnet-20240620-v1:0", "name": "Claude 3.5 Sonnet (Anthropic)"},
        {"id": "us.anthropic.claude-3-5-sonnet-20240620-v1:0", "name": "US Claude 3.5 Sonnet (inference profile)"}]}
    assert {(c["path"], c["key_id"], c["region"]) for c in aws.list_calls} == {
        ("/foundation-models", ROLE_KEY_ID, "us-east-1"), ("/inference-profiles", ROLE_KEY_ID, "us-east-1")}


def test_when_listing_is_refused_the_admin_types_the_model_id(splunkd, admin, aws, imds):
    aws.list_error = (403, "AccessDeniedException", "not authorized")

    response = _call(splunkd, admin, "POST", "/models/available", {"region": "us-east-1", "role_arn": ROLE})

    assert response.status_code == 200
    assert response.json() == {"models": [], "message": f"Couldn't list models as {ROLE} "
                                                        "(AccessDeniedException). Type the model ID instead."}


# Knowledge base connections (#50)

KB_ROLE = "arn:aws:iam::210987654321:role/kb-search"
RUNBOOKS = {"name": "runbooks", "region": "us-west-2", "role_arn": KB_ROLE, "kb_id": "EJGJR4SKJL",
            "description": "On-call runbooks and the escalation rota."}


def _aitk_kbs(aitk, splunkd, token):
    manager = aitk.vector_db_config_manager.ConfigManager(searchinfo(splunkd, token))
    return {c["name"]: c for c in manager.get_config()}


def _kb_secret(splunkd, name):
    return json.loads(splunkd.passwords[("Splunk_ML_Toolkit", "aitk_vector_db_creds", name)])


def test_an_admin_creates_a_knowledge_base_that_ai_toolkit_stores_without_keys(aitk, splunkd, admin, aws, imds):
    response = _call(splunkd, admin, "POST", "/knowledge_bases", RUNBOOKS)

    assert response.status_code == 201, response.text
    assert response.json()["test"] == {"ok": True, "message": f"Assumed role {KB_ROLE} and searched knowledge base "
                                                              "EJGJR4SKJL"}
    stored = _aitk_kbs(aitk, splunkd, admin)["runbooks_instance_role"]
    assert stored["type"] == "AWS_KB" and stored["description"] == RUNBOOKS["description"]
    # AI Toolkit's aiagent reads the region from details.aws_region and the rest from the secret.
    assert (stored["details"]["aws_region"], stored["details"]["kb_id"]) == ("us-west-2", "EJGJR4SKJL")
    assert _kb_secret(splunkd, "runbooks_instance_role") == {
        "aws_access_key_id": None, "aws_access_key_token": None, "role_arn": KB_ROLE}
    [assumed] = aws.assume_role_calls
    assert (assumed["key_id"], assumed["region"], assumed["form"]["RoleArn"]) == (INSTANCE_KEY_ID, "us-west-2",
                                                                                 KB_ROLE)
    [search] = aws.retrieve_calls
    assert (search["kb_id"], search["key_id"]) == ("EJGJR4SKJL", ROLE_KEY_ID)
    assert search["body"]["retrievalConfiguration"] == {"vectorSearchConfiguration": {"numberOfResults": 1}}


@pytest.mark.parametrize("change, message", [
    ({"kb_id": "EJGJR4"}, "The knowledge base ID is 10 letters and digits, like EJGJR4SKJL"),
    ({"description": ""}, "The knowledge base needs a description: it's all the agent learns about it"),
    ({"role_arn": "kb-search"}, "The role ARN must look like arn:<partition>:iam::<12-digit account>:role/<name>"),
])
def test_an_incomplete_knowledge_base_is_rejected(aitk, splunkd, admin, aws, imds, change, message):
    response = _call(splunkd, admin, "POST", "/knowledge_bases", {**RUNBOOKS, **change})

    assert response.status_code == 400
    assert response.json()["message"] == message
    assert _aitk_kbs(aitk, splunkd, admin) == {}


def test_a_failed_knowledge_base_test_says_which_step_failed(aitk, splunkd, admin, aws, imds):
    aws.retrieve_script = lambda kb_id, request: (403, "AccessDeniedException", "not authorized")

    response = _call(splunkd, admin, "POST", "/knowledge_bases", RUNBOOKS)

    assert response.status_code == 422
    assert response.json()["test"] == {
        "ok": False, "step": "bedrock", "code": "AccessDeniedException",
        "message": f"Assumed role {KB_ROLE}, but the knowledge base search failed (AccessDeniedException). "
                   "Check the role's Bedrock permissions."}
    assert _aitk_kbs(aitk, splunkd, admin) == {}

    aws.reject_key_ids.add(INSTANCE_KEY_ID)
    response = _call(splunkd, admin, "POST", "/knowledge_bases", {**RUNBOOKS, "save_anyway": True})

    assert response.status_code == 201
    assert response.json()["test"]["step"] == "assume_role"
    assert "runbooks_instance_role" in _aitk_kbs(aitk, splunkd, admin)


def test_knowledge_bases_are_listed_beside_models(splunkd, admin, aws, imds):
    _call(splunkd, admin, "POST", "/knowledge_bases", RUNBOOKS)
    _call(splunkd, admin, "POST", "/models", GLM)

    listing = _call(splunkd, admin, "GET").json()

    assert listing["knowledge_bases"] == [{"name": "runbooks_instance_role", "region": "us-west-2",
                                           "role_arn": KB_ROLE, "kb_id": "EJGJR4SKJL",
                                           "description": RUNBOOKS["description"]}]
    assert [m["name"] for m in listing["models"]] == ["glm_instance_role"]


def test_an_admin_edits_a_knowledge_base_and_its_keys_stay_empty(aitk, splunkd, admin, aws, imds):
    _call(splunkd, admin, "POST", "/knowledge_bases", RUNBOOKS)
    other_role = "arn:aws:iam::210987654321:role/kb-search-2"

    response = _call(splunkd, admin, "POST", "/knowledge_bases/runbooks_instance_role",
                     {**RUNBOOKS, "region": "eu-west-1", "role_arn": other_role, "description": "Runbooks v2."})

    assert response.status_code == 200, response.text
    stored = _aitk_kbs(aitk, splunkd, admin)["runbooks_instance_role"]
    assert (stored["details"]["aws_region"], stored["description"]) == ("eu-west-1", "Runbooks v2.")
    assert _kb_secret(splunkd, "runbooks_instance_role") == {
        "aws_access_key_id": None, "aws_access_key_token": None, "role_arn": other_role}


def test_an_admin_deletes_a_knowledge_base(aitk, splunkd, admin, aws, imds):
    _call(splunkd, admin, "POST", "/knowledge_bases", RUNBOOKS)

    response = _call(splunkd, admin, "DELETE", "/knowledge_bases/runbooks_instance_role")

    assert response.status_code == 200, response.text
    assert _aitk_kbs(aitk, splunkd, admin) == {}
    assert ("Splunk_ML_Toolkit", "aitk_vector_db_creds", "runbooks_instance_role") not in splunkd.passwords
    assert _call(splunkd, admin, "GET").json()["knowledge_bases"] == []


def test_a_model_connection_cant_be_edited_as_a_knowledge_base(splunkd, admin, aws, imds):
    _call(splunkd, admin, "POST", "/models", GLM)

    response = _call(splunkd, admin, "DELETE", "/knowledge_bases/glm_instance_role")

    assert response.status_code == 404
    assert response.json()["message"] == "glm_instance_role isn't an instance-role connection made in Cloud Connect"


def test_key_based_knowledge_bases_cant_be_edited_or_deleted_here(aitk, splunkd, admin, aws, imds):
    body = {"name": "keys_kb", "type": "AWS_KB", "description": "d",
            "details": {"aws_region": "us-west-2", "kb_id": "EJGJR4SKJL", "aws_access_key_id": "AKIAFAKE000000000001",
                        "aws_access_key_token": "fake-secret", "role_arn": KB_ROLE}}
    created = httpx.post(f"{splunkd.uri}/servicesNS/admin/Splunk_ML_Toolkit/mltk/vector_stores",
                         headers={"Authorization": f"Splunk {admin}"}, json=body, verify=False)
    assert created.status_code == 200, created.text

    response = _call(splunkd, admin, "DELETE", "/knowledge_bases/keys_kb")

    assert response.status_code == 404
    assert "keys_kb" in _aitk_kbs(aitk, splunkd, admin)
