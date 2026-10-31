"""The HTTP debug guard keeps http.client's debug output out of persistent REST workers' stdout.

AI Toolkit 6.2.0 sets http.client.HTTPConnection.debuglevel = 1, so every HTTP
call in its REST handler prints "send: ..." lines into the reply pipe and
splunkd fails some requests with "unexpected data received (73)".
tools/http_debug_guard.py installs a .pth hook into Splunk's Python that
discards those prints in splunk/persistconn/appserver.py workers only.
"""

import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import http_debug_guard  # noqa: E402

# Mimics AI Toolkit: turn on debug output, then make one HTTP call.
CALL = '''import site, sys
site.addsitedir(sys.argv[1])
import http.client
http.client.HTTPConnection.debuglevel = 1
conn = http.client.HTTPConnection("127.0.0.1", int(sys.argv[2]))
conn.request("GET", "/services/server/info", headers={"Authorization": "Splunk not-a-real-key"})
conn.getresponse().read()
print("reply received")
'''


class _Splunkd(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def splunkd_port():
    server = HTTPServer(("127.0.0.1", 0), _Splunkd)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port
    server.shutdown()


@pytest.fixture
def site_dir(tmp_path):
    path = tmp_path / "site-packages"
    path.mkdir()
    return path


def _run_as(script_path, site_dir, port):
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(CALL)
    return subprocess.run(
        [sys.executable, str(script_path), str(site_dir), str(port)],
        capture_output=True, text=True, check=True,
    ).stdout


def test_rest_worker_stdout_has_no_debug_output(tmp_path, site_dir, splunkd_port):
    http_debug_guard.install(site_dir)

    out = _run_as(tmp_path / "splunk" / "persistconn" / "appserver.py", site_dir, splunkd_port)

    assert out == "reply received\n"


def test_without_the_guard_the_worker_prints_the_authorization_header(tmp_path, site_dir, splunkd_port):
    out = _run_as(tmp_path / "splunk" / "persistconn" / "appserver.py", site_dir, splunkd_port)

    assert out.startswith("send: b'GET /services/server/info")
    assert "Authorization: Splunk not-a-real-key" in out


def test_other_python_processes_are_left_alone(tmp_path, site_dir, splunkd_port):
    http_debug_guard.install(site_dir)

    out = _run_as(tmp_path / "bin" / "some_modular_input.py", site_dir, splunkd_port)

    assert out.startswith("send: ")


def test_status_reports_active_after_install(site_dir, capsys):
    http_debug_guard.install(site_dir)

    assert http_debug_guard.status(site_dir, sys.executable) == 0
    assert "active in REST workers" in capsys.readouterr().out


def test_status_flags_files_from_another_version(site_dir, capsys):
    http_debug_guard.install(site_dir)
    (site_dir / "aitk_http_debug_guard.py").write_text("# edited\n")

    assert http_debug_guard.status(site_dir, sys.executable) == 1
    assert "differ" in capsys.readouterr().out


def test_install_twice_then_uninstall_leaves_nothing(site_dir):
    http_debug_guard.install(site_dir)
    http_debug_guard.install(site_dir)
    cache = site_dir / "__pycache__"
    cache.mkdir()
    (cache / "aitk_http_debug_guard.cpython-313.pyc").write_bytes(b"")

    http_debug_guard.uninstall(site_dir)

    assert list(site_dir.rglob("aitk_http_debug_guard*")) == []
    assert http_debug_guard.status(site_dir, sys.executable) == 1
