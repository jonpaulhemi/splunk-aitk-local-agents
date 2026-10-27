"""Calls to this search head's own splunkd REST API."""

import json

from splunk import rest

APP = "splunk_cloud_connect"


class SplunkdError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"splunkd returned {status}: {message}")
        self.status = status


def call(rest_uri, token, method, path, postargs=None):
    """Make a REST call as `token` and return (status, parsed JSON or None)."""
    response, content = rest.simpleRequest(
        f"{rest_uri}{path}",
        sessionKey=token,
        method=method,
        postargs=postargs,
        getargs={"output_mode": "json"},
        raiseAllErrors=False,
    )
    try:
        body = json.loads(content) if content else None
    except ValueError:
        body = None
    return response.status, body


def capabilities(rest_uri, token):
    status, body = call(rest_uri, token, "GET", "/services/authentication/current-context")
    if status != 200:
        raise SplunkdError(status, "current-context lookup failed")
    return set(body["entry"][0]["content"].get("capabilities", []))
