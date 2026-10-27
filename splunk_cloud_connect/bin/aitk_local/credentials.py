"""Secrets this app generates for itself, kept in Splunk's credential store."""

import secrets

from aitk_local import splunkd

REALM = "aitk_local"
TOKEN_NAME = "runtime_token"
_PASSWORDS = f"/servicesNS/nobody/{splunkd.APP}/storage/passwords"


def runtime_token(rest_uri, system_token):
    """The bearer token AI Toolkit must present to the local runtime.

    Generated on first use. Concurrent first calls (splunkd runs handlers in
    several processes) converge on whichever value was stored first.
    """
    existing = _read(rest_uri, system_token, TOKEN_NAME)
    if existing is not None:
        return existing
    status, _ = splunkd.call(
        rest_uri,
        system_token,
        "POST",
        _PASSWORDS,
        postargs={"name": TOKEN_NAME, "realm": REALM, "password": secrets.token_urlsafe(32)},
    )
    if status not in (200, 201, 409):
        raise splunkd.SplunkdError(status, "could not store the runtime token")
    stored = _read(rest_uri, system_token, TOKEN_NAME)
    if stored is None:
        raise splunkd.SplunkdError(404, "runtime token missing after create")
    return stored


def _read(rest_uri, system_token, name):
    status, body = splunkd.call(rest_uri, system_token, "GET", f"{_PASSWORDS}/{REALM}:{name}:")
    if status == 404:
        return None
    if status != 200:
        raise splunkd.SplunkdError(status, "could not read the credential store")
    return body["entry"][0]["content"]["clear_password"]
