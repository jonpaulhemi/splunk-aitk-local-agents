"""A scripted AWS edge for tests: Bedrock Runtime's Converse API, Bedrock Agent
Runtime's Retrieve (knowledge base search), plus STS AssumeRole.

Point botocore at it with AWS_ENDPOINT_URL_BEDROCK_RUNTIME,
AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME and AWS_ENDPOINT_URL_STS. Converse
requests are answered by `script(request_json)`, which returns the assistant's
text, a `tool_use(name, input)` reply, or an (status, error_type, message)
triple. Retrieve requests are answered by `retrieve_script(kb_id, request_json)`,
which returns a list of `chunk(...)` results or such a triple.
Signatures aren't checked, but each request's SigV4 credential scope is
recorded so tests can see which key, region and session token were used.
"""

import http.server
import json
import re
import socketserver
import threading
import time
import urllib.parse

ROLE_KEY_ID = "ASIAFAKEROLEKEY00001"
ROLE_SECRET = "fake-role-secret"
ROLE_SESSION_TOKEN = "fake-role-session-token"


class FakeBedrock:
    def __init__(self):
        self.script = lambda request: "ok"
        self.converse_calls = []  # {"model", "key_id", "region", "session_token", "body"}
        self.assume_role_calls = []  # {"key_id", "region", "form", "session_token"}
        self.retrieve_script = lambda kb_id, request: []
        self.retrieve_calls = []  # {"kb_id", "key_id", "region", "session_token", "body"}
        self.reject_key_ids = set()  # answered like an invalid security token
        # Bedrock's control plane (ListFoundationModels, ListInferenceProfiles), at AWS_ENDPOINT_URL_BEDROCK
        self.foundation_models = [{"modelId": "anthropic.claude-3-5-sonnet-20240620-v1:0",
                                   "modelName": "Claude 3.5 Sonnet", "providerName": "Anthropic"}]
        self.inference_profiles = [{"inferenceProfileId": "us.anthropic.claude-3-5-sonnet-20240620-v1:0",
                                    "inferenceProfileName": "US Claude 3.5 Sonnet"}]
        self.list_error = None  # (status, error_type, message) to refuse listing
        self.list_calls = []  # {"path", "key_id", "region", "session_token"}
        self.delay = 0.0  # seconds before each Converse answer
        self._lock = threading.Lock()
        self._server = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                key_id, region, _ = _scope(self.headers.get("Authorization", ""))
                path = urllib.parse.urlsplit(self.path).path
                with fake._lock:
                    fake.list_calls.append({"path": path, "key_id": key_id, "region": region,
                                            "session_token": self.headers.get("X-Amz-Security-Token")})
                if fake.list_error:
                    return self._error(*fake.list_error)
                if path == "/foundation-models":
                    return self._send(200, "application/json",
                                      json.dumps({"modelSummaries": fake.foundation_models}))
                if path == "/inference-profiles":
                    return self._send(200, "application/json",
                                      json.dumps({"inferenceProfileSummaries": fake.inference_profiles}))
                return self._error(404, "UnknownOperationException", "unknown operation")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                key_id, region, service = _scope(self.headers.get("Authorization", ""))
                if key_id in fake.reject_key_ids and service == "sts":
                    return self._send(403, "text/xml", _sts_error("InvalidClientTokenId",
                                                                  "The security token included in the request is invalid."))
                if key_id in fake.reject_key_ids:
                    return self._error(403, "UnrecognizedClientException",
                                       "The security token included in the request is invalid.")
                if service == "sts":
                    form = dict(urllib.parse.parse_qsl(raw.decode()))
                    with fake._lock:
                        fake.assume_role_calls.append({"key_id": key_id, "region": region, "form": form,
                                                       "session_token": self.headers.get("X-Amz-Security-Token")})
                    return self._send(200, "text/xml", _assume_role_xml())
                retrieve = re.fullmatch(r"/knowledgebases/([^/]+)/retrieve", self.path)
                if retrieve:
                    return self._retrieve(urllib.parse.unquote(retrieve.group(1)), key_id, region, json.loads(raw))
                match = re.fullmatch(r"/model/([^/]+)/converse", self.path)
                if not match:
                    return self._error(404, "UnknownOperationException", "unknown operation")
                body = json.loads(raw)
                with fake._lock:
                    fake.converse_calls.append({
                        "model": urllib.parse.unquote(match.group(1)), "key_id": key_id, "region": region,
                        "session_token": self.headers.get("X-Amz-Security-Token"), "body": body,
                    })
                time.sleep(fake.delay)
                answer = fake.script(body)
                if isinstance(answer, tuple):
                    return self._error(*answer)
                try:
                    return self._send(200, "application/json", json.dumps(_converse(answer)))
                except BrokenPipeError:
                    pass  # the client gave up waiting (timeout tests)

            def _retrieve(self, kb_id, key_id, region, body):
                with fake._lock:
                    fake.retrieve_calls.append({"kb_id": kb_id, "key_id": key_id, "region": region,
                                                "session_token": self.headers.get("X-Amz-Security-Token"),
                                                "body": body})
                time.sleep(fake.delay)
                answer = fake.retrieve_script(kb_id, body)
                if isinstance(answer, tuple):
                    return self._error(*answer)
                try:
                    return self._send(200, "application/json", json.dumps({"retrievalResults": answer}))
                except BrokenPipeError:
                    pass

            def _error(self, status, error_type, message):
                self.send_response(status)
                self.send_header("x-amzn-ErrorType", error_type)
                data = json.dumps({"message": message}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send(self, status, content_type, text):
                data = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def _scope(authorization):
    """(key id, region, service) from a SigV4 Authorization header."""
    match = re.search(r"Credential=([^/]+)/\d{8}/([^/]+)/([^/]+)/aws4_request", authorization)
    return match.groups() if match else (None, None, None)


def tool_use(name, tool_input, tool_use_id="tooluse_1"):
    """A Converse reply asking to call one tool."""
    return {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": tool_input}}


def chunk(text, uri, score):
    """One Retrieve result: a chunk from an S3 document (or, for an http(s) `uri`, a web page)."""
    location = ({"type": "WEB", "webLocation": {"url": uri}} if uri.startswith("http")
                else {"type": "S3", "s3Location": {"uri": uri}})
    return {"content": {"type": "TEXT", "text": text}, "location": location, "score": score}


def offered_tools(request):
    return sorted(t["toolSpec"]["name"] for t in (request.get("toolConfig") or {}).get("tools", []))


def _converse(answer):
    content = [answer] if isinstance(answer, dict) else [{"text": answer}]
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use" if isinstance(answer, dict) else "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "metrics": {"latencyMs": 1},
    }


def _sts_error(code, message):
    return f"""<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <Error><Type>Sender</Type><Code>{code}</Code><Message>{message}</Message></Error>
  <RequestId>fake</RequestId>
</ErrorResponse>"""


def _assume_role_xml():
    return f"""<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <AssumeRoleResult>
    <Credentials>
      <AccessKeyId>{ROLE_KEY_ID}</AccessKeyId>
      <SecretAccessKey>{ROLE_SECRET}</SecretAccessKey>
      <SessionToken>{ROLE_SESSION_TOKEN}</SessionToken>
      <Expiration>2099-01-01T00:00:00Z</Expiration>
    </Credentials>
    <AssumedRoleUser>
      <AssumedRoleId>AROAFAKE:aitk-local</AssumedRoleId>
      <Arn>arn:aws-us-gov:sts::123456789012:assumed-role/bedrock/aitk-local</Arn>
    </AssumedRoleUser>
  </AssumeRoleResult>
  <ResponseMetadata><RequestId>fake</RequestId></ResponseMetadata>
</AssumeRoleResponse>"""
