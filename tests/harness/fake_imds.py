"""A scripted EC2 instance metadata service (IMDSv2) handing out an instance
profile's credentials.

Point botocore at it with AWS_EC2_METADATA_SERVICE_ENDPOINT. It answers the
IMDSv2 token PUT and the two credential GETs, and refuses GETs without a token,
as an instance that requires IMDSv2 does. `role` None means no instance
profile is attached.
"""

import http.server
import json
import socketserver
import threading

INSTANCE_KEY_ID = "ASIAFAKEINSTANCE0001"
INSTANCE_SECRET = "fake-instance-secret"
INSTANCE_SESSION_TOKEN = "fake-instance-session-token"
TOKEN = "fake-imds-token"
CREDENTIALS = "/latest/meta-data/iam/security-credentials/"


class FakeIMDS:
    def __init__(self, role="splunk-search-head"):
        self.role = role
        self.token_puts = 0
        self.tokenless_gets = 0
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

            def do_PUT(self):
                if self.path != "/latest/api/token":
                    return self._send(404, "")
                fake.token_puts += 1
                self._send(200, TOKEN)

            def do_GET(self):
                if self.headers.get("X-aws-ec2-metadata-token") != TOKEN:
                    fake.tokenless_gets += 1
                    return self._send(401, "")
                if fake.role is None or not self.path.startswith(CREDENTIALS):
                    return self._send(404, "")
                name = self.path[len(CREDENTIALS):]
                if not name:
                    return self._send(200, fake.role)
                if name != fake.role:
                    return self._send(404, "")
                self._send(200, json.dumps({
                    "Code": "Success", "Type": "AWS-HMAC", "LastUpdated": "2026-10-31T00:00:00Z",
                    "AccessKeyId": INSTANCE_KEY_ID, "SecretAccessKey": INSTANCE_SECRET,
                    "Token": INSTANCE_SESSION_TOKEN, "Expiration": "2099-01-01T00:00:00Z",
                }))

            def _send(self, status, text):
                data = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
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
