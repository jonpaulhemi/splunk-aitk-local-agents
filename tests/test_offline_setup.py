"""An admin turns off AI Toolkit's own traffic to Splunk's cloud in one action (#17)."""

import httpx
import pytest

from harness.aitk import searchinfo

AITK = "Splunk_ML_Toolkit"
SYNC_INPUTS = ["slim_agent_templates/managed_templates", "splunk_managed_skills/catalog"]
TELEMETRY_SEARCHES = [
    "Splunk_ML_Toolkit - ML-SPL - Telemetry Gen",
    "Splunk_ML_Toolkit - FineTuneCDTSM - Telemetry Gen",
    "Splunk_ML_Toolkit - FineTuneCDTSM - Lineage Telemetry Gen",
    "Splunk_ML_Toolkit - AI Agent - Telemetry Gen",
    "Splunk_ML_Toolkit - ML-SPL - Sourcetype Inference",
]


@pytest.fixture
def admin(splunkd):
    return splunkd.add_user("admin", ["admin_all_objects", "apply_ai_commander_command"], roles=["admin"])


def _setup(splunkd, token, method="POST", **form):
    return httpx.request(
        method, f"{splunkd.uri}/services/aitk_local/offline_setup",
        headers={"Authorization": f"Splunk {token}"}, data=form or None, params={"output_mode": "json"},
    )


def _get(splunkd, token, path):
    response = httpx.get(f"{splunkd.uri}/servicesNS/nobody/{AITK}/{path}",
                         headers={"Authorization": f"Splunk {token}"}, params={"output_mode": "json"})
    assert response.status_code == 200, path
    return response.json()["entry"][0]["content"]


def _state(splunkd, token):
    from urllib.parse import quote

    return {
        "inputs_disabled": [_get(splunkd, token, f"data/inputs/{i}")["disabled"] for i in SYNC_INPUTS],
        "searches_scheduled": [
            _get(splunkd, token, f"saved/searches/{quote(s)}")["is_scheduled"] for s in TELEMETRY_SEARCHES
        ],
        "allowlist": _get(splunkd, token, "configs/conf-mlspl/ai%3AAllowedDomains"),
    }


def test_first_run_turns_off_syncs_and_telemetry_and_a_rerun_changes_nothing(splunkd, admin):
    first = _setup(splunkd, admin)

    assert first.status_code == 200
    assert sorted(c["setting"] for c in first.json()["changed"]) == sorted(
        [f"input {i.replace('/', '://')}" for i in SYNC_INPUTS]
        + [f"scheduled search {s}" for s in TELEMETRY_SEARCHES]
    )
    state = _state(splunkd, admin)
    assert state["inputs_disabled"] == [True, True]
    assert state["searches_scheduled"] == [False] * 5

    second = _setup(splunkd, admin)

    assert second.status_code == 200
    assert second.json()["changed"] == []
    assert len(second.json()["unchanged"]) == 7


def test_without_hosts_the_allowlist_is_left_alone(splunkd, admin):
    response = _setup(splunkd, admin)

    assert not any("allow" in c["setting"] for c in response.json()["changed"])
    assert _state(splunkd, admin)["allowlist"]["allowed_domains"] == "*"


def test_allowlist_limits_aitk_mcp_urls_to_the_given_hosts(aitk, splunkd, admin):
    from connection_config_manager.utils.common_utils import CommonUtils

    response = _setup(splunkd, admin, allowed_domains=" mcp.corp.example , *.internal.example ,")

    assert {"setting": "allowed domains", "from": "*", "to": "mcp.corp.example,*.internal.example"} \
        in response.json()["changed"]
    allowlist = _state(splunkd, admin)["allowlist"]
    assert allowlist["allowed_domains"] == "mcp.corp.example,*.internal.example"
    assert allowlist["enforce_domain_validation"] == "true"
    checks = CommonUtils(searchinfo(splunkd, admin))
    assert checks.is_url_whitelisted("https://mcp.corp.example/sse")
    assert checks.is_url_whitelisted("https://splunk.internal.example:8089/services/mcp")
    assert not checks.is_url_whitelisted("https://mcp.atlassian.com/v1/sse")

    again = _setup(splunkd, admin, allowed_domains="mcp.corp.example,*.internal.example")

    assert again.json()["changed"] == []


def test_enforcement_switched_off_is_switched_back_on(splunkd, admin):
    splunkd.conf_writes[(AITK, "mlspl.conf", "ai:AllowedDomains")] = {
        "allowed_domains": "mcp.corp.example", "enforce_domain_validation": "false",
    }

    response = _setup(splunkd, admin, allowed_domains="mcp.corp.example")

    assert {"setting": "allowed domains enforcement", "from": "false", "to": "true"} in response.json()["changed"]


def test_only_admins_can_run_it(splunkd, admin):
    analyst = splunkd.add_user("analyst", ["apply_ai_commander_command"])

    response = _setup(splunkd, analyst, allowed_domains="mcp.corp.example")

    assert response.status_code == 403
    assert _state(splunkd, admin) == {
        "inputs_disabled": [False, False],
        "searches_scheduled": [True] * 5,
        "allowlist": {"allowed_domains": "*", "enforce_domain_validation": "true"},
    }


def test_get_is_not_a_way_to_run_it(splunkd, admin):
    response = _setup(splunkd, admin, method="GET")

    assert response.status_code == 405
    assert splunkd.conf_writes == {}
