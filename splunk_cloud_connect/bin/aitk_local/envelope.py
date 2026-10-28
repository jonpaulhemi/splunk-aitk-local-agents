"""Decrypting the invoke request AI Toolkit sends.

AI Toolkit (AiAgentProcessor) encrypts the run payload with Fernet, keyed by
PBKDF2-HMAC-SHA256(runtime token, salt, 100,000 iterations), and sends the
salt RSA-OAEP-SHA256 encrypted to our public key:

    {"payload": b64(fernet token), "is_new_encryption": true, "salt": b64(rsa)}
"""

import base64
import json


class DecryptError(ValueError):
    pass


def open_envelope(body, token, private_key):
    """The decrypted payload dict. Raises DecryptError, never revealing contents."""
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    try:
        salt = private_key.decrypt(
            base64.b64decode(body["salt"]),
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100_000)
        key = base64.urlsafe_b64encode(kdf.derive(token.encode()))
        payload = json.loads(Fernet(key).decrypt(base64.b64decode(body["payload"])))
    except Exception as e:
        raise DecryptError(f"could not decrypt the invoke payload ({type(e).__name__})") from None
    if not isinstance(payload, dict):
        raise DecryptError("decrypted invoke payload is not a JSON object")
    return payload
