"""Fake of `splunk.rest.simpleRequest` that calls the fake splunkd for real."""

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request


class _Response:
    def __init__(self, status, headers):
        self.status = status
        self.headers = headers

    def __getitem__(self, key):
        return self.headers.get(key)


def _context():
    cafile = os.environ.get("FAKE_SPLUNKD_CA")
    return ssl.create_default_context(cafile=cafile)


def simpleRequest(
    path,
    sessionKey=None,
    getargs=None,
    postargs=None,
    method="GET",
    raiseAllErrors=False,
    jsonargs=None,
    rawResult=False,
    timeout=None,
    **_ignored,
):
    url = path
    if not url.startswith("http"):
        url = os.environ["FAKE_SPLUNKD_URI"] + url
    if getargs:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(getargs)
    data = None
    headers = {}
    if sessionKey:
        headers["Authorization"] = f"Splunk {sessionKey}"
    if jsonargs is not None:
        data = (jsonargs if isinstance(jsonargs, str) else json.dumps(jsonargs)).encode()
        headers["Content-Type"] = "application/json"
    elif postargs is not None:
        data = urllib.parse.urlencode(postargs, doseq=True).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        if method == "GET":
            method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=_context(), timeout=timeout or 60) as r:
            return _Response(r.status, dict(r.headers)), r.read()
    except urllib.error.HTTPError as e:
        return _Response(e.code, dict(e.headers)), e.read()
