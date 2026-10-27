"""Secrets this app generates for itself, kept in Splunk's credential store."""

import base64
import secrets

from aitk_local import splunkd

REALM = "aitk_local"
TOKEN_NAME = "runtime_token"
RSA_KEY_NAME = "rsa_private_key"
_PASSWORDS = f"/servicesNS/nobody/{splunkd.APP}/storage/passwords"


def runtime_token(rest_uri, system_token):
    """The bearer token AI Toolkit must present to the local runtime."""
    return _get_or_create(rest_uri, system_token, TOKEN_NAME, lambda: secrets.token_urlsafe(32))


def existing_runtime_token(rest_uri, system_token):
    """The runtime token if one has been generated, else None. Never creates it."""
    return _read(rest_uri, system_token, TOKEN_NAME)


def rsa_private_key_der(rest_uri, system_token):
    """DER bytes of the runtime's RSA key; AI Toolkit encrypts invoke salts to it."""
    stored = _get_or_create(rest_uri, system_token, RSA_KEY_NAME, _new_rsa_key)
    return base64.b64decode(stored)


def _new_rsa_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    # One line: the credential store keeps passwords as single-line strings.
    return base64.b64encode(der).decode()


def _get_or_create(rest_uri, system_token, name, make):
    """Read secret `name`, generating it on first use.

    Concurrent first calls (splunkd runs handlers in several processes)
    converge on whichever value was stored first.
    """
    existing = _read(rest_uri, system_token, name)
    if existing is not None:
        return existing
    status, _ = splunkd.call(
        rest_uri,
        system_token,
        "POST",
        _PASSWORDS,
        postargs={"name": name, "realm": REALM, "password": make()},
    )
    if status not in (200, 201, 409):
        raise splunkd.SplunkdError(status, f"could not store {name}")
    stored = _read(rest_uri, system_token, name)
    if stored is None:
        raise splunkd.SplunkdError(404, f"{name} missing after create")
    return stored


def _read(rest_uri, system_token, name):
    status, body = splunkd.call(rest_uri, system_token, "GET", f"{_PASSWORDS}/{REALM}:{name}:")
    if status == 404:
        return None
    if status != 200:
        raise splunkd.SplunkdError(status, "could not read the credential store")
    return body["entry"][0]["content"]["clear_password"]
