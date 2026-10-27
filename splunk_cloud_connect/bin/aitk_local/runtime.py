"""The local agent runtime: the AgentCore calls AI Toolkit makes to SCS.

AI Toolkit sends them to https://{host}:8089/services/aitk_local/slim-api/v1alpha1/...
(see standin.py), with the stand-in's token as a bearer token. splunkd doesn't
authenticate these requests (the token isn't a Splunk session), so every call
checks the token here.

Lifecycle (#16):
  POST   agent/aws_agentcore                {agent_name, app_version, agent_type}
  GET    agent/aws_agentcore?runtime_ids=&memory_ids=
  DELETE agent/aws_agentcore/{runtime_id}
  GET    encryption/public-key
"""

import functools
import hmac
import json
import urllib.parse

from aitk_local import credentials, records

UNAUTHORIZED = {"status": 401, "payload": {"message": "Unauthorized"}}
NOT_FOUND = {"status": 404, "payload": {"message": "Not found"}}


def handle(request):
    rest_uri, system_token = request["server"]["rest_uri"], request["system_authtoken"]
    if not _authorized(request, credentials.runtime_token(rest_uri, system_token)):
        return UNAUTHORIZED

    method = request["method"]
    path = request.get("path_info", "").strip("/").split("/")
    query = _query(request)

    if path == ["agent", "aws_agentcore"] and method == "POST":
        return _create(json.loads(request.get("payload") or "{}"))
    if path == ["agent", "aws_agentcore"] and method == "GET":
        return _status(query)
    if len(path) == 3 and path[:2] == ["agent", "aws_agentcore"] and method == "DELETE":
        return _delete(path[2])
    if path == ["encryption", "public-key"] and method == "GET":
        return {"status": 200, "payload": {"public_key": _public_key_pem(rest_uri, system_token)}}
    return NOT_FOUND


def _authorized(request, token):
    headers = {k.lower(): v for k, v in request.get("headers") or []}
    scheme, _, presented = headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(presented.strip().encode(), token.encode())


def _query(request):
    query = dict(request.get("query") or [])
    if not query and "?" in request.get("rest_path", ""):
        query = dict(urllib.parse.parse_qsl(request["rest_path"].split("?", 1)[1]))
    return query


def _create(body):
    agent_name = body.get("agent_name")
    if not agent_name:
        return {"status": 400, "payload": {"message": "agent_name is required"}}
    runtime_id, version, memory_id = records.create(agent_name, body.get("agent_type"), body.get("app_version"))
    return {
        "status": 201,
        "payload": {"runtime_id": runtime_id, "version": version, "memory_id": memory_id, "status": records.READY},
    }


def _status(query):
    runtime_ids = [i for i in query.get("runtime_ids", "").split(",") if i]
    memory_ids = [i for i in query.get("memory_ids", "").split(",") if i]
    return {
        "status": 200,
        "payload": {
            "agents": [{"runtime_id": i, "status": records.runtime_status(i)} for i in runtime_ids],
            "memories": [{"memory_id": i, "status": records.memory_status(i)} for i in memory_ids],
        },
    }


def _delete(runtime_id):
    if not records.delete(runtime_id):
        return {"status": 404, "payload": {"runtime_id": runtime_id, "status": records.NOT_FOUND}}
    return {"status": 200, "payload": {"runtime_id": runtime_id, "status": "DELETED"}}


@functools.cache
def _private_key(der):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_der_private_key(der, password=None)


def _public_key_pem(rest_uri, system_token):
    from cryptography.hazmat.primitives import serialization

    key = _private_key(credentials.rsa_private_key_der(rest_uri, system_token))
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
