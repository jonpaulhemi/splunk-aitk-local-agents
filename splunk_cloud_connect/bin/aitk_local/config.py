"""This app's settings from aitk_local.conf (local/ overrides default/)."""

import configparser
import os
import socket
from urllib.parse import urlsplit

from aitk_local import splunkd


def _conf():
    app_dir = os.path.join(os.environ["SPLUNK_HOME"], "etc", "apps", splunkd.APP)
    parser = configparser.RawConfigParser(interpolation=None)
    parser.read([os.path.join(app_dir, layer, "aitk_local.conf") for layer in ("default", "local")])
    return parser


def advertised_host_and_port(rest_uri):
    """'host:port' AI Toolkit should use to reach the local runtime."""
    conf = _conf()
    runtime = conf["runtime"] if conf.has_section("runtime") else {}
    host = (runtime.get("advertised_host") or "").strip() or socket.getfqdn()
    port = (runtime.get("advertised_port") or "").strip() or str(urlsplit(rest_uri).port)
    return f"{host}:{port}"


DEFAULT_MAX_TOOL_RESULT_CHARS = 16000


def max_tool_result_chars():
    """How much of each tool result the model sees ([agent] max_tool_result_chars)."""
    conf = _conf()
    value = conf.get("agent", "max_tool_result_chars", fallback="").strip()
    return int(value) if value.isdigit() and int(value) > 0 else DEFAULT_MAX_TOOL_RESULT_CHARS
