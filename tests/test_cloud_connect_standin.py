"""AI Toolkit finds 'Cloud Connect' activated and pointing at our local runtime."""

from conftest import searchinfo


def test_aitk_system_token_lookup_points_scs_at_local_runtime(aitk, splunkd):
    admin = splunkd.add_user("admin", ["admin_all_objects", "apply_ai_commander_command"])
    si = searchinfo(splunkd, admin)

    base_url = aitk.util.get_scs_api_base_url_with_system_token(si)

    assert base_url == f"https://localhost:{splunkd.port}/services/aitk_local/slim-api/v1alpha1"


def test_aitk_user_relay_lookup_gets_same_runtime_and_token(aitk, splunkd):
    admin = splunkd.add_user("admin", ["admin_all_objects", "apply_ai_commander_command"])
    analyst = splunkd.add_user("analyst", ["apply_ai_commander_command"])
    system_view = aitk.util.fetch_cloud_connect_details_with_system_token(searchinfo(splunkd, admin))

    si = searchinfo(splunkd, analyst, username="analyst", with_system_token=False)
    base_url = aitk.util.get_scs_api_base_url(si)
    token, _ = aitk.util.get_cached_scs_token(None, None, si)

    assert base_url == f"https://localhost:{splunkd.port}/services/aitk_local/slim-api/v1alpha1"
    assert token == system_view["scs_token"]
    assert len(token) >= 32


def test_runtime_token_is_generated_once_and_kept_in_the_credential_store(aitk, splunkd):
    admin = splunkd.add_user("admin", ["admin_all_objects", "apply_ai_commander_command"])
    si = searchinfo(splunkd, admin)

    first = aitk.util.fetch_cloud_connect_details_with_system_token(si)["scs_token"]
    splunkd.load_routes()  # fresh handler instances, as after a splunkd restart
    second = aitk.util.fetch_cloud_connect_details_with_system_token(si)["scs_token"]

    assert first == second
    assert list(splunkd.passwords.values()) == [first]


def test_concurrent_first_lookups_agree_on_one_token(aitk, splunkd):
    from concurrent.futures import ThreadPoolExecutor

    admin = splunkd.add_user("admin", ["admin_all_objects", "apply_ai_commander_command"])
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
    from splunk import rest

    analyst = splunkd.add_user("analyst", ["apply_ai_commander_command"])

    response, content = rest.simpleRequest(
        STANDIN, sessionKey=analyst, getargs={"output_mode": "json"}
    )

    assert response.status == 403
    assert b"token" not in content
    assert splunkd.passwords == {}


def _set_runtime_conf(splunk_home, text):
    conf = splunk_home / "etc" / "apps" / "splunk_cloud_connect" / "local" / "aitk_local.conf"
    conf.write_text("[runtime]\n" + text)


def test_advertised_host_defaults_to_this_machines_fqdn_and_splunkd_port(aitk, splunkd, splunk_home):
    import socket

    _set_runtime_conf(splunk_home, "")
    admin = splunkd.add_user("admin", ["admin_all_objects"])

    details = aitk.util.fetch_cloud_connect_details_with_system_token(searchinfo(splunkd, admin))

    assert details["tenantApiHostname"] == f"{socket.getfqdn()}:{splunkd.port}"


def test_advertised_host_and_port_can_be_overridden(aitk, splunkd, splunk_home):
    _set_runtime_conf(splunk_home, "advertised_host = sh01.example.internal\nadvertised_port = 9089\n")
    admin = splunkd.add_user("admin", ["admin_all_objects"])

    details = aitk.util.fetch_cloud_connect_details_with_system_token(searchinfo(splunkd, admin))

    assert details["tenantApiHostname"] == "sh01.example.internal:9089"


def test_token_is_never_written_to_disk(aitk, splunkd, splunk_home):
    admin = splunkd.add_user("admin", ["admin_all_objects", "apply_ai_commander_command"])
    analyst = splunkd.add_user("analyst", ["apply_ai_commander_command"])
    token = aitk.util.fetch_cloud_connect_details_with_system_token(searchinfo(splunkd, admin))["scs_token"]
    aitk.util.get_cached_scs_token(None, None, searchinfo(splunkd, analyst, "analyst", False))

    leaks = [
        p
        for p in splunk_home.rglob("*")
        if p.is_file() and not p.is_symlink() and token.encode() in p.read_bytes()
    ]

    assert leaks == []
