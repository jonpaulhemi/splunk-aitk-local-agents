"""The bundled agent libraries load and run inside a splunkd handler (#15).

Needs the libraries built first: python tools/bundle.py build
"""

import json

import pytest

DIAGNOSTICS = "/services/aitk_local/diagnostics"


@pytest.fixture
def admin(splunkd):
    return splunkd.add_user("admin", ["admin_all_objects"])


def _get(token):
    from splunk import rest

    response, content = rest.simpleRequest(DIAGNOSTICS, sessionKey=token, getargs={"output_mode": "json"})
    return response.status, json.loads(content)


def test_every_bundled_library_imports_and_a_checkpointed_graph_runs(splunkd, admin):
    status, report = _get(admin)

    assert status == 200, report
    assert report["python"].startswith("3.13.")
    assert report["errors"] == {}
    assert report["graph_run"] == "ok"
    assert set(report["imported"]) >= {
        "langgraph",
        "langgraph.checkpoint.sqlite",
        "langchain_core",
        "langchain_aws",
        "langchain_openai",
        "langchain_mcp_adapters",
    }


def test_bundled_libraries_win_over_anything_already_on_the_path(splunkd, admin):
    import sys

    # The fake splunkd runs handlers in this test process, which has already
    # loaded some of the same packages (cryptography, httpx...). Only those may
    # be reported as coming from outside lib/; the live run expects none.
    preloaded = {name.split(".")[0] for name in sys.modules}

    _, report = _get(admin)

    assert {name.split(".")[0] for name in report["shadowed"]} <= preloaded


def test_diagnostics_are_admin_only(splunkd):
    from splunk import RESTException

    analyst = splunkd.add_user("analyst", ["apply_ai_commander_command"])

    with pytest.raises(RESTException) as refused:
        _get(analyst)

    assert refused.value.statusCode == 403


def test_aliases_of_splunks_own_packages_are_not_reported_as_shadowing(splunkd, admin, monkeypatch):
    # Seen live: requests registers Splunk's chardet as requests.packages.chardet.
    import sys
    import types

    chardet = types.ModuleType("chardet")
    chardet.__file__ = "/opt/splunk/lib/python3.13/site-packages/chardet/__init__.py"
    monkeypatch.setitem(sys.modules, "requests.packages.chardet", chardet)

    _, report = _get(admin)

    assert "requests.packages.chardet" not in report["shadowed"]
