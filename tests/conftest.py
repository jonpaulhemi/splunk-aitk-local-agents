"""Contract-test harness (spec #13, seam 1).

Builds a fake $SPLUNK_HOME with this app and AI Toolkit installed side by side,
serves both apps' REST handlers from a fake splunkd over HTTPS, and lets tests
drive AI Toolkit's own client code against our handlers.

AI Toolkit is proprietary and never copied into this repo. Point AITK_APP at an
unpacked `Splunk_ML_Toolkit` directory (from splunk-ai-toolkit_620.tgz):

    AITK_APP=/path/to/Splunk_ML_Toolkit pytest
"""

import importlib
import os
import types
import shutil
import sys
from pathlib import Path

import pytest

from harness.fake_splunkd import FakeSplunkd
from harness.tls import make_ca_and_server_cert

REPO = Path(__file__).resolve().parent.parent
APP_SRC = REPO / "splunk_cloud_connect"
FAKES = Path(__file__).resolve().parent / "fakes"


def pytest_configure(config):
    aitk = os.environ.get("AITK_APP")
    if not aitk or not (Path(aitk) / "default" / "app.conf").exists():
        raise pytest.UsageError(
            "Set AITK_APP to an unpacked AI Toolkit 6.2.0 app directory (Splunk_ML_Toolkit)."
        )
    sys.path.insert(0, str(FAKES))


@pytest.fixture(scope="session")
def splunk_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("splunk_home")
    apps = home / "etc" / "apps"
    apps.mkdir(parents=True)
    (home / "var" / "log" / "splunk").mkdir(parents=True)
    os.symlink(Path(os.environ["AITK_APP"]).resolve(), apps / "Splunk_ML_Toolkit")
    shutil.copytree(APP_SRC, apps / "splunk_cloud_connect", ignore=shutil.ignore_patterns("local"))
    # AI Toolkit refuses to import unless the Python for Scientific Computing
    # add-on directory exists; nothing in it is used by these tests.
    (apps / "Splunk_SA_Scientific_Python_linux_x86_64").mkdir()
    os.environ["SPLUNK_HOME"] = str(home)
    tls = make_ca_and_server_cert(home, ["localhost", "127.0.0.1"])
    os.environ["FAKE_SPLUNKD_CA"] = str(tls["ca"])
    # AI Toolkit verifies the agent runtime with certifi plus the OS trust
    # store; the test CA plays the org CA the real search head's cert chains to.
    os.environ["SSL_CERT_FILE"] = str(tls["ca"])
    return home


@pytest.fixture
def runtime_conf(splunk_home):
    """This app's local/aitk_local.conf in the fake Splunk home."""
    return splunk_home / "etc" / "apps" / "splunk_cloud_connect" / "local" / "aitk_local.conf"


@pytest.fixture
def splunkd(splunk_home, runtime_conf, monkeypatch):
    local = runtime_conf.parent
    shutil.rmtree(local, ignore_errors=True)
    local.mkdir()
    # Tests reach the fake by its name on the cert, as AI Toolkit must reach the
    # real search head by its FQDN.
    runtime_conf.write_text("[runtime]\nadvertised_host = localhost\n")
    server = FakeSplunkd(splunk_home, {
        "cert": splunk_home / "server.pem",
        "key": splunk_home / "server.key",
    }).start()
    monkeypatch.setenv("FAKE_SPLUNKD_URI", server.uri)
    yield server
    server.stop()


@pytest.fixture(scope="session")
def aitk(splunk_home):
    """AI Toolkit's own modules, imported from the installed app."""
    bin_dir = str(splunk_home / "etc" / "apps" / "Splunk_ML_Toolkit" / "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    bouncer = importlib.import_module("util.rest_bouncer")
    # AI Toolkit shells out to $SPLUNK_HOME/bin/python3 unless it is already
    # running under it; the test interpreter stands in for Splunk's Python.
    bouncer.SPLUNK_PYTHON_PATH = sys.executable

    return types.SimpleNamespace(
        util=importlib.import_module("util.ai_commander_util"),
        agent_manager=importlib.import_module("agent_manager.agent_manager"),
        agent_status=importlib.import_module("processors.AgentStatusProcessor"),
        ai_agent=importlib.import_module("processors.AiAgentProcessor"),
    )

