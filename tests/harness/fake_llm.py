"""A scripted OpenAI-compatible chat-completions server: the LLM edge in tests.

Each request is answered by `script(request_json)`, which returns either the
assistant's text, or a (status, body) pair for errors. `delay` seconds are
slept first. Requests are recorded, and `max_in_flight` counts overlap.
"""

import http.server
import json
import socketserver
import threading
import time


class FakeLLM:
    def __init__(self):
        self.script = lambda request: "ok"
        self.delay = 0.0
        self.requests = []
        self.in_flight = 0
        self.max_in_flight = 0
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

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                with fake._lock:
                    fake.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                    fake.in_flight += 1
                    fake.max_in_flight = max(fake.max_in_flight, fake.in_flight)
                try:
                    time.sleep(fake.delay)
                    answer = fake.script(body)
                finally:
                    with fake._lock:
                        fake.in_flight -= 1
                if isinstance(answer, tuple):
                    status, payload = answer
                else:
                    status, payload = 200, _completion(body.get("model"), answer)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
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


def _completion(model, text):
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def last_user_message(request):
    return [m for m in request["messages"] if m["role"] == "user"][-1]["content"]
