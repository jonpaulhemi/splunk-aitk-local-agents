"""AI Toolkit finds 'Cloud Connect' activated and pointing at our local runtime."""

import pytest

from harness.aitk import runtime_base_url, searchinfo

ADMIN_CAPS = ["admin_all_objects", "apply_ai_commander_command"]


@pytest.fixture
def admin(splunkd):
    return splunkd.add_user("admin", ADMIN_CAPS)


def test_aitk_system_token_lookup_points_scs_at_local_runtime(aitk, splunkd, admin):
    base_url = aitk.util.get_scs_api_base_url_with_system_token(searchinfo(splunkd, admin))

    assert base_url == runtime_base_url(splunkd)


def test_aitk_user_relay_lookup_gets_same_runtime_and_token(aitk, splunkd, admin):
    analyst = splunkd.add_user("analyst", ["apply_ai_commander_command"])
    system_view = aitk.util.fetch_cloud_connect_details_with_system_token(searchinfo(splunkd, admin))

    si = searchinfo(splunkd, analyst, username="analyst", with_system_token=False)
    base_url = aitk.util.get_scs_api_base_url(si)
    token, _ = aitk.util.get_cached_scs_token(None, None, si)

    assert base_url == runtime_base_url(splunkd)
    assert token == system_view["scs_token"]
    assert len(token) >= 32


def test_runtime_token_is_generated_once_and_kept_in_the_credential_store(aitk, splunkd, admin):
    si = searchinfo(splunkd, admin)

    first = aitk.util.fetch_cloud_connect_details_with_system_token(si)["scs_token"]
    splunkd.load_routes()  # fresh handler instances, as after a splunkd restart
    second = aitk.util.fetch_cloud_connect_details_with_system_token(si)["scs_token"]

    assert first == second
    assert list(splunkd.passwords.values()) == [first]


def test_concurrent_first_lookups_agree_on_one_token(aitk, splunkd, admin):
    from concurrent.futures import ThreadPoolExecutor

    si = searchinfo(splunkd, admin)

    with ThreadPoolExecutor(8) as pool:
        tokens = set(
            pool.map(
                lambda _: aitk.util.fetch_cloud_connect_details_with_system_token(si)["scs_token"],
                range(8),
            )
        )

    assert len(tokens) == 1


STANDIN = "/servicesNS/nobody/splunk_cloud_connect/v1alpha1/cloud-connection/splunk_ml_toolkit"


def test_non_admin_cannot_read_the_token_directly(aitk, splunkd):
    from splunk import RESTException, rest

    analyst = splunkd.add_user("analyst", ["apply_ai_commander_command"])

    with pytest.raises(RESTException) as refused:
        rest.simpleRequest(STANDIN, sessionKey=analyst, getargs={"output_mode": "json"})

    assert refused.value.statusCode == 403
    assert splunkd.passwords == {}


def test_advertised_host_defaults_to_this_machines_fqdn_and_splunkd_port(aitk, splunkd, admin, runtime_conf):
    import socket

    runtime_conf.write_text("[runtime]\n")

    details = aitk.util.fetch_cloud_connect_details_with_system_token(searchinfo(splunkd, admin))

    assert details["tenantApiHostname"] == f"{socket.getfqdn()}:{splunkd.port}"


def test_advertised_host_and_port_can_be_overridden(aitk, splunkd, admin, runtime_conf):
    runtime_conf.write_text("[runtime]\nadvertised_host = sh01.example.internal\nadvertised_port = 9089\n")

    details = aitk.util.fetch_cloud_connect_details_with_system_token(searchinfo(splunkd, admin))

    assert details["tenantApiHostname"] == "sh01.example.internal:9089"


def test_token_is_never_written_to_disk(aitk, splunkd, admin, splunk_home):
    analyst = splunkd.add_user("analyst", ["apply_ai_commander_command"])
    token = aitk.util.fetch_cloud_connect_details_with_system_token(searchinfo(splunkd, admin))["scs_token"]
    aitk.util.get_cached_scs_token(None, None, searchinfo(splunkd, analyst, "analyst", False))

    leaks = [
        p
        for p in splunk_home.rglob("*")
        if p.is_file() and not p.is_symlink() and token.encode() in p.read_bytes()
    ]

    assert leaks == []


def test_broken_credential_store_is_reported_not_crashed(aitk, splunkd, admin):
    from splunk import rest

    splunkd.fail_credential_store = True

    response, content = rest.simpleRequest(STANDIN, sessionKey=admin, getargs={"output_mode": "json"})

    assert response.status == 503
    assert b"credential store" in content


def test_credential_store_refusal_is_reported_not_crashed(aitk, splunkd, admin):
    # Real splunk.rest.simpleRequest raises on 401/403 even with raiseAllErrors=False.
    from splunk import rest

    splunkd.users["splunk-system-user"].capabilities = ["admin_all_objects"]

    response, content = rest.simpleRequest(STANDIN, sessionKey=admin, getargs={"output_mode": "json"})

    assert response.status == 503
    assert b"credential store" in content


def test_unexpected_failure_still_returns_a_reply_and_is_logged(aitk, splunkd, admin, runtime_conf, splunk_home):
    from splunk import rest

    runtime_conf.write_text("[runtime]\nadvertised_host = a\nadvertised_host = b\n")

    response, content = rest.simpleRequest(STANDIN, sessionKey=admin, getargs={"output_mode": "json"})

    assert response.status == 500
    assert b"Local agent runtime error" in content
    log = (splunk_home / "var" / "log" / "splunk" / "aitk_local.log").read_text()
    assert "Traceback" in log
