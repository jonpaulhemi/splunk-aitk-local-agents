"""Calls to this search head's own splunkd REST API."""

import json

from splunk import RESTException, rest

APP = "splunk_cloud_connect"


class SplunkdError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"splunkd returned {status}: {message}")
        self.status = status


def call(rest_uri, token, method, path, postargs=None):
    """Make a REST call as `token` and return (status, parsed JSON or None)."""
    try:
        response, content = rest.simpleRequest(
            f"{rest_uri}{path}",
            sessionKey=token,
            method=method,
            postargs=postargs,
            getargs={"output_mode": "json"},
            raiseAllErrors=False,
        )
    except RESTException as e:
        # simpleRequest raises on 401/403/404 even with raiseAllErrors=False.
        return e.statusCode, None
    try:
        body = json.loads(content) if content else None
    except ValueError:
        body = None
    return response.status, body


def call_json(rest_uri, token, method, path, body=None):
    """Make a REST call with a JSON body (as AI Toolkit's own REST API takes) and
    return (status, parsed JSON or None)."""
    try:
        response, content = rest.simpleRequest(
            f"{rest_uri}{path}",
            sessionKey=token,
            method=method,
            jsonargs=None if body is None else json.dumps(body),
            getargs={"output_mode": "json"},
            raiseAllErrors=False,
        )
    except RESTException as e:
        return e.statusCode, None
    try:
        parsed = json.loads(content) if content else None
    except ValueError:
        parsed = None
    return response.status, parsed


def capabilities(rest_uri, token):
    status, body = call(rest_uri, token, "GET", "/services/authentication/current-context")
    if status != 200:
        raise SplunkdError(status, "current-context lookup failed")
    return set(body["entry"][0]["content"].get("capabilities", []))


def is_admin(request):
    """Whether the calling user holds admin_all_objects."""
    caps = capabilities(request["server"]["rest_uri"], request["session"]["authtoken"])
    return "admin_all_objects" in caps
