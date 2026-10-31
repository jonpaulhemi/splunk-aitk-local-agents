"""Cloud Connect's setup for instance-role connections (#49, #50).

An instance-role connection is a Bedrock model or knowledge base connection
with a role ARN and no access keys: the runtime assumes the role with the EC2
instance profile's credentials (see aws_credentials). AI Toolkit's own forms
insist on access keys, so admins make these here instead. This endpoint
writes them into AI Toolkit's connection store through AI Toolkit's own REST
API, with the key fields JSON null (never ""; litellm, behind AI Toolkit's
`| ai`, treats "" as real keys), so they show up in Launchpad's pickers.

It also keeps a small registry of what it made (this app's
aitk_local_connections.conf, no secrets): AI Toolkit masks a Bedrock
connection's region and role ARN when listing, and the registry is how this
endpoint tells its own connections from key-based ones, which it never
touches.

    GET    /connections                                -> {"models": [...], "knowledge_bases": [...]}
    POST   /connections/<kind>              {fields}   -> create (tested first unless save_anyway)
    POST   /connections/<kind>/test         {fields}   -> test only
    POST   /connections/models/available    {region, role_arn} -> models the role can see
    POST   /connections/<kind>/<name>       {fields}   -> edit (tested first unless save_anyway)
    DELETE /connections/<kind>/<name>                  -> delete

<kind> is `models` or `knowledge_bases`. Admins only (admin_all_objects).
"""

import json
import re
from urllib.parse import quote, unquote

from aitk_local import aws_credentials, log, splunkd
from aitk_local.llm import REGION

SUFFIX = "_instance_role"
# AI Toolkit's own rule for connection names; its secret lookups put the name in a URL unquoted.
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ROLE_ARN = re.compile(r"arn:aws(-[a-z]+)*:iam::\d{12}:role/[\w+=,.@/-]{1,512}")
REGISTRY = f"/servicesNS/nobody/{splunkd.APP}/configs/conf-aitk_local_connections"
TEST_TIMEOUT = 30
MODELS, KNOWLEDGE_BASES = "models", "knowledge_bases"


class _Refused(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def handle(request):
    if not splunkd.is_admin(request):
        return {"status": 403, "payload": {"message": "Insufficient permissions"}}
    parts = [unquote(p) for p in (request.get("path_info") or "").split("/") if p]
    method = request["method"]
    ctx = _Context(request)
    try:
        if not parts and method == "GET":
            return _reply(200, ctx.listing())
        if not parts or parts[0] not in KINDS or len(parts) > 2:
            raise _Refused(404, "Unknown path")
        kind = KINDS[parts[0]]
        action = parts[1] if len(parts) == 2 else None
        if method == "POST" and action is None:
            return ctx.save(kind, _fields(request), existing=None)
        if method == "POST" and action == "test":
            return _reply(200, {"test": kind.test(kind.validated(_fields(request)))})
        if method == "POST" and action == "available" and kind is MODEL_KIND:
            return _reply(200, _available_models(_fields(request)))
        if method == "POST":
            return ctx.save(kind, _fields(request), existing=action)
        if method == "DELETE" and action:
            return ctx.delete(kind, action)
        raise _Refused(405, "Unsupported method")
    except _Refused as e:
        return _reply(e.status, {"message": e.message, **e.extra})


def _reply(status, payload):
    return {"status": status, "payload": payload}


def _fields(request):
    try:
        body = json.loads(request.get("payload") or "{}")
    except ValueError:
        raise _Refused(400, "The request body must be JSON") from None
    if not isinstance(body, dict):
        raise _Refused(400, "The request body must be a JSON object")
    return body


def _full_name(name):
    name = str(name or "").strip()
    return name if name.endswith(SUFFIX) else name + SUFFIX


def _common(fields):
    """The name, region and role ARN every instance-role connection needs, checked."""
    name = str(fields.get("name") or "").strip()
    if not name:
        raise _Refused(400, "The connection needs a name")
    if not NAME.fullmatch(name) or len(_full_name(name)) > 100:
        raise _Refused(400, "The name must start with a letter or underscore and contain only letters, digits "
                            "and underscores, up to 100 characters")
    region = str(fields.get("region") or "").strip()
    if not region:
        raise _Refused(400, "The connection needs a region")
    if not REGION.fullmatch(region):
        raise _Refused(400, f"'{region}' isn't an AWS region name such as us-east-1")
    return {"name": _full_name(name), "region": region, "role_arn": _role_arn(fields),
            "description": str(fields.get("description") or "").strip()}


def _role_arn(fields):
    role_arn = str(fields.get("role_arn") or "").strip()
    if not ROLE_ARN.fullmatch(role_arn):
        raise _Refused(400, "The role ARN must look like arn:<partition>:iam::<12-digit account>:role/<name>")
    return role_arn


def _number(fields, key, kind, default, low, high):
    value = fields.get(key)
    if value in (None, ""):
        return default
    try:
        number = kind(value)
    except (TypeError, ValueError):
        raise _Refused(400, f"{key.replace('_', ' ')} must be a number") from None
    if not low <= number <= high:
        raise _Refused(400, f"{key.replace('_', ' ')} must be between {low} and {high}")
    return number


def _boto_config():
    from botocore.config import Config

    return Config(connect_timeout=TEST_TIMEOUT, read_timeout=TEST_TIMEOUT, retries={"total_max_attempts": 1})


def _role_credentials(fields):
    """(credentials, None) for the connection's role, or (None, failed test result)."""
    try:
        return aws_credentials.for_connection(fields["region"], None, None, fields["role_arn"], _boto_config()), None
    except aws_credentials.NoInstanceCredentials:
        return None, {"ok": False, "step": "instance_credentials", "code": "NoInstanceCredentials",
                      "message": "No instance credentials were found. Attach an instance profile to this "
                                 "search head and allow IMDSv2."}
    except aws_credentials.AssumeRoleFailed as e:
        return None, {"ok": False, "step": "assume_role", "code": e.code,
                      "message": f"Couldn't assume role {e.role_arn} ({e.code}). Check the role's trust policy "
                                 "and the instance profile's sts:AssumeRole permission."}


def _bedrock_refused(fields, e, what, check):
    code = aws_credentials.reason(e)
    return {"ok": False, "step": "bedrock", "code": code,
            "message": f"Assumed role {fields['role_arn']}, but {what} ({code}). Check {check}."}


class _ModelKind:
    path = MODELS
    aitk = "aicommander"

    def validated(self, fields):
        checked = _common(fields)
        model = str(fields.get("model") or "").strip()
        if not model:
            raise _Refused(400, "The connection needs a model")
        checked.update(
            model=model,
            temperature=_number(fields, "temperature", float, 0.0, 0.0, 1.0),
            max_tokens=_number(fields, "max_tokens", int, 4000, 1, 200000),
            request_timeout=_number(fields, "request_timeout", int, 60, 1, 840),
        )
        return checked

    def test(self, fields):
        """Assume the role, then a one-token Converse call as it."""
        credentials, failed = _role_credentials(fields)
        if failed:
            return failed
        import boto3

        client = boto3.client("bedrock-runtime", region_name=fields["region"], config=_boto_config(), **credentials)
        try:
            client.converse(modelId=fields["model"], messages=[{"role": "user", "content": [{"text": "Hi"}]}],
                            inferenceConfig={"maxTokens": 1})
        except Exception as e:
            return _bedrock_refused(fields, e, "Bedrock refused the call", "the role's Bedrock permissions and model access")
        return {"ok": True, "message": f"Assumed role {fields['role_arn']} and Bedrock answered"}

    def aitk_body(self, fields):
        return {
            "name": fields["name"], "provider": "Bedrock", "model": fields["model"],
            "description": fields["description"],
            "connection_details": {"region": fields["region"], "aws_access_key_id": None,
                                   "aws_access_token": None, "role_arn": fields["role_arn"],
                                   "request_timeout": fields["request_timeout"]},
            "llm_params": {"response_variability": fields["temperature"], "max_tokens": fields["max_tokens"]},
        }

    def listed(self, stanza, content):
        return {"name": stanza, "region": content.get("region"), "role_arn": content.get("role_arn"),
                "model": content.get("model"), "temperature": float(content.get("temperature") or 0),
                "max_tokens": int(content.get("max_tokens") or 0),
                "request_timeout": int(content.get("request_timeout") or 0),
                "description": content.get("description") or ""}

    def registry_fields(self, fields):
        return {k: str(fields[k]) for k in ("region", "role_arn", "model", "temperature", "max_tokens",
                                             "request_timeout", "description")}


class _KnowledgeBaseKind:
    path = KNOWLEDGE_BASES
    aitk = "vector_stores"

    def validated(self, fields):
        checked = _common(fields)
        kb_id = str(fields.get("kb_id") or "").strip()
        if not re.fullmatch(r"[0-9A-Za-z]{10}", kb_id):
            raise _Refused(400, "The knowledge base ID is 10 letters and digits, like EJGJR4SKJL")
        if not checked["description"]:
            raise _Refused(400, "The knowledge base needs a description: it's all the agent learns about it")
        checked["kb_id"] = kb_id
        return checked

    def test(self, fields):
        """Assume the role, then one Retrieve as it."""
        credentials, failed = _role_credentials(fields)
        if failed:
            return failed
        import boto3

        client = boto3.client("bedrock-agent-runtime", region_name=fields["region"], config=_boto_config(),
                              **credentials)
        try:
            client.retrieve(knowledgeBaseId=fields["kb_id"], retrievalQuery={"text": "connection test"},
                            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 1}})
        except Exception as e:
            return _bedrock_refused(fields, e, "the knowledge base search failed", "the role's Bedrock permissions")
        return {"ok": True, "message": f"Assumed role {fields['role_arn']} and searched knowledge base "
                                       f"{fields['kb_id']}"}

    def aitk_body(self, fields):
        return {"name": fields["name"], "type": "AWS_KB", "description": fields["description"],
                "details": {"aws_region": fields["region"], "kb_id": fields["kb_id"], "aws_access_key_id": None,
                            "aws_access_key_token": None, "role_arn": fields["role_arn"]}}

    def listed(self, stanza, content):
        return {"name": stanza, "region": content.get("region"), "role_arn": content.get("role_arn"),
                "kb_id": content.get("kb_id"), "description": content.get("description") or ""}

    def registry_fields(self, fields):
        return {k: str(fields[k]) for k in ("region", "role_arn", "kb_id", "description")}


MODEL_KIND, KNOWLEDGE_BASE_KIND = _ModelKind(), _KnowledgeBaseKind()
KINDS = {MODELS: MODEL_KIND, KNOWLEDGE_BASES: KNOWLEDGE_BASE_KIND}


class _Context:
    def __init__(self, request):
        self.rest_uri = request["server"]["rest_uri"]
        self.token = request["session"]["authtoken"]
        self.user = request["session"]["user"]

    # -- the registry -----------------------------------------------------

    def registry(self):
        status, body = splunkd.call(self.rest_uri, self.token, "GET", REGISTRY, getargs={"count": 0})
        if status != 200:
            raise splunkd.SplunkdError(status, "reading the connection registry failed")
        return {e["name"]: e["content"] for e in body.get("entry", []) if e.get("content", {}).get("kind")}

    def registered(self, kind, name):
        content = self.registry().get(name)
        if not content or content.get("kind") != kind.path:
            raise _Refused(404, f"{name} isn't an instance-role connection made in Cloud Connect")
        return content

    def listing(self):
        found = {MODELS: [], KNOWLEDGE_BASES: []}
        for name, content in sorted(self.registry().items()):
            kind = KINDS.get(content.get("kind"))
            if kind:
                found[kind.path].append(kind.listed(name, content))
        return found

    # -- AI Toolkit ------------------------------------------------------------

    def aitk(self, method, handler, body=None, name=None):
        path = f"/servicesNS/{quote(self.user, safe='')}/Splunk_ML_Toolkit/mltk/{handler}"
        if name is not None:
            path += "/" + quote(name, safe="")
        status, reply = splunkd.call_json(self.rest_uri, self.token, method, path, body)
        if status not in (200, 201):
            message = (reply or {}).get("message") or (reply or {}).get("error_message") or f"HTTP {status}"
            raise _Refused(502 if status >= 500 else status, f"AI Toolkit refused the change: {message}")
        return reply

    # -- actions ---------------------------------------------------------------

    def save(self, kind, fields, existing):
        checked = kind.validated(fields)
        if existing is not None:
            self.registered(kind, existing)
            if checked["name"] != existing:
                raise _Refused(400, "Renaming isn't supported: delete the connection and make a new one")
        elif checked["name"] in self.registry():
            raise _Refused(409, f"A connection named {checked['name']} already exists")
        test = kind.test(checked)
        if not test["ok"] and not fields.get("save_anyway"):
            return _reply(422, {"message": test["message"], "test": test})
        body = kind.aitk_body(checked)
        if existing is None:
            self.aitk("POST", kind.aitk, body)
            try:
                self._registry_write(None, {"name": checked["name"], "kind": kind.path,
                                            **kind.registry_fields(checked)})
            except splunkd.SplunkdError:
                # Without its registry entry this endpoint could never edit or delete it: undo.
                self.aitk("DELETE", kind.aitk, name=checked["name"])
                raise
        else:
            self.aitk("PUT", kind.aitk, body)
            self._registry_write(checked["name"], kind.registry_fields(checked))
        log.get_logger().info("%s %s instance-role connection %s (role %s, test %s)", self.user,
                              "created" if existing is None else "updated", checked["name"], checked["role_arn"],
                              "passed" if test["ok"] else f"failed at {test['step']}")
        return _reply(201 if existing is None else 200,
                      {"connection": kind.listed(checked["name"], kind.registry_fields(checked)), "test": test})

    def delete(self, kind, name):
        self.registered(kind, name)
        self.aitk("DELETE", kind.aitk, name=name)
        status, _ = splunkd.call(self.rest_uri, self.token, "DELETE", f"{REGISTRY}/{quote(name, safe='')}")
        if status not in (200, 404):
            raise splunkd.SplunkdError(status, "removing the connection from the registry failed")
        log.get_logger().info("%s deleted instance-role connection %s", self.user, name)
        return _reply(200, {"message": f"Deleted {name}"})

    def _registry_write(self, stanza, form):
        path = REGISTRY if stanza is None else f"{REGISTRY}/{quote(stanza, safe='')}"
        status, _ = splunkd.call(self.rest_uri, self.token, "POST", path, postargs=form)
        if status not in (200, 201):
            raise splunkd.SplunkdError(status, "writing the connection registry failed")


def _available_models(fields):
    """Foundation models and inference profiles the role can see in the region."""
    checked = {"region": str(fields.get("region") or "").strip()}
    if not REGION.fullmatch(checked["region"]):
        raise _Refused(400, "Pick a region first")
    checked["role_arn"] = _role_arn(fields)
    credentials, failed = _role_credentials(checked)
    if failed:
        return {"models": [], "message": failed["message"] + " Type the model ID instead."}
    import boto3

    client = boto3.client("bedrock", region_name=checked["region"], config=_boto_config(), **credentials)
    try:
        found = [{"id": m["modelId"], "name": f"{m.get('modelName') or m['modelId']} ({m.get('providerName')})"}
                 for m in client.list_foundation_models(byOutputModality="TEXT").get("modelSummaries", [])]
        found += [{"id": p["inferenceProfileId"],
                   "name": f"{p.get('inferenceProfileName') or p['inferenceProfileId']} (inference profile)"}
                  for p in client.list_inference_profiles().get("inferenceProfileSummaries", [])]
    except Exception as e:
        return {"models": [], "message": f"Couldn't list models as {checked['role_arn']} "
                                         f"({aws_credentials.reason(e)}). Type the model ID instead."}
    return {"models": found}
