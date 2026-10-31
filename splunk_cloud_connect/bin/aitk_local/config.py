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
    return _positive_int("agent", "max_tool_result_chars", DEFAULT_MAX_TOOL_RESULT_CHARS)


DEFAULT_SESSION_RETENTION_HOURS = 168.0


def session_retention_hours():
    """How long an unused session is remembered ([memory] session_retention_hours)."""
    value = _conf().get("memory", "session_retention_hours", fallback="").strip()
    try:
        hours = float(value)
    except ValueError:
        return DEFAULT_SESSION_RETENTION_HOURS
    return hours if hours > 0 else DEFAULT_SESSION_RETENTION_HOURS


DEFAULT_CHUNKS_PER_SEARCH = 5


def chunks_per_search():
    """How many chunks a knowledge base search asks for ([knowledge_base] chunks_per_search)."""
    return _positive_int("knowledge_base", "chunks_per_search", DEFAULT_CHUNKS_PER_SEARCH)


DEFAULT_SEARCH_TIMEOUT_SECONDS = 30


def search_timeout_seconds():
    """How long one knowledge base search may take ([knowledge_base] search_timeout_seconds)."""
    return _positive_int("knowledge_base", "search_timeout_seconds", DEFAULT_SEARCH_TIMEOUT_SECONDS)


def _positive_int(section, key, default):
    value = _conf().get(section, key, fallback="").strip()
    return int(value) if value.isdigit() and int(value) > 0 else default
