"""A scripted OpenAI-compatible server: the LLM edge in tests.

Serves chat completions and, for models LangChain sends there (gpt-6 with
tools, for one), the Responses API. Responses requests are recorded and
scripted in chat-completions shape, so scripts needn't care which API was
used; like OpenAI, the Responses API treats function tools as strict unless
they say `"strict": false`, and rejects schemas strict mode can't take.

Each request is answered by `script(request_json)`, which returns the
assistant's text, an assistant message dict (see `tool_call`), or a
(status, body) pair for errors. `delay` seconds are
slept first. Requests are recorded, and `max_in_flight` counts overlap.
"""

import http.server
import json
import socketserver
import threading
import time
import uuid


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
                raw = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                responses_api = self.path.endswith("/responses")
                body = _as_chat(raw) if responses_api else raw
                with fake._lock:
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    fake.requests.append({"path": self.path, "headers": headers, "body": body})
                    fake.in_flight += 1
                    fake.max_in_flight = max(fake.max_in_flight, fake.in_flight)
                try:
                    time.sleep(fake.delay)
                    rejected = _strict_schema_error(raw) if responses_api else None
                    answer = rejected or fake.script(body)
                finally:
                    with fake._lock:
                        fake.in_flight -= 1
                if isinstance(answer, tuple):
                    status, payload = answer
                elif responses_api:
                    status, payload = 200, _response(body.get("model"), answer)
                else:
                    status, payload = 200, _completion(body.get("model"), answer)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass  # the client gave up waiting (timeout tests)

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def _completion(model, answer):
    message = answer if isinstance(answer, dict) else {"role": "assistant", "content": answer}
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def last_user_message(request):
    return [m for m in request["messages"] if m["role"] == "user"][-1]["content"]


def _as_chat(body):
    """A Responses API request in chat-completions shape."""
    messages = [{"role": "system", "content": body["instructions"]}] if body.get("instructions") else []
    for item in body.get("input") or []:
        kind = item.get("type", "message")
        if kind == "function_call_output":
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        elif kind == "function_call":
            messages.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": item["call_id"], "type": "function",
                 "function": {"name": item["name"], "arguments": item["arguments"]}},
            ]})
        elif kind == "message":
            content = item["content"]
            if not isinstance(content, str):
                content = "".join(part.get("text", "") for part in content)
            messages.append({"role": "developer" if item["role"] == "system" else item["role"], "content": content})
    tools = [{"type": "function", "function": {"name": t["name"], "parameters": t.get("parameters")}}
             for t in body.get("tools") or [] if t.get("type") == "function"]
    return {"model": body.get("model"), "messages": messages, "tools": tools, "responses_api": True}


def _strict_schema_error(body):
    """OpenAI's 400 for a strict function tool whose schema strict mode can't
    take: every property required, and no additional properties."""
    for tool in body.get("tools") or []:
        if tool.get("type") != "function" or tool.get("strict", True) is False:
            continue
        schema = tool.get("parameters") or {}
        if (schema.get("additionalProperties") is not False
                or set(schema.get("required") or []) != set(schema.get("properties") or {})):
            return 400, {"error": {"type": "invalid_request_error", "code": "invalid_json_schema", "param": "tools",
                                   "message": f"Invalid schema for function '{tool['name']}'"}}
    return None


def _response(model, answer):
    message = answer if isinstance(answer, dict) else {"role": "assistant", "content": answer}
    if message.get("tool_calls"):
        output = [{"type": "function_call", "id": f"fc_{c['id']}", "call_id": c["id"], "status": "completed",
                   "name": c["function"]["name"], "arguments": c["function"]["arguments"]}
                  for c in message["tool_calls"]]
    else:
        output = [{"type": "message", "id": f"msg_{uuid.uuid4().hex}", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": message["content"], "annotations": []}]}]
    return {
        # Unique, as OpenAI's are: LangChain uses it as the message id, and
        # LangGraph replaces earlier messages with the same id.
        "id": f"resp_{uuid.uuid4().hex}", "object": "response", "created_at": int(time.time()), "model": model,
        "status": "completed", "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
    }


def tool_call(name, arguments, call_id="call_1"):
    """An assistant message asking to call one tool."""
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},
    ]}


def tool_results(request):
    """The text of each tool message sent back to the model so far."""
    return [_text(m["content"]) for m in request["messages"] if m["role"] == "tool"]


def _text(content):
    # Tool results arrive as a string or as a list of text parts.
    return content if isinstance(content, str) else "".join(p.get("text", "") for p in content)


def offered_tools(request):
    return sorted(t["function"]["name"] for t in request.get("tools") or [])
