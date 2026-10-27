"""Spike handler for issue #2.

GET or POST /services/aitkspike/<anything>?seconds=N&lines=M

Sleeps N seconds spread across M NDJSON lines, then returns them all, ending
with a {"status": "completed"} line shaped like AgentCore's final chunk. It
echoes the path and whether an Authorization header reached the handler.
"""

import json
import os
import sys
import time

from splunk.persistconn.application import PersistentServerConnectionApplication

EXPECTED_TOKEN = "spike-token"


class SpikeStream(PersistentServerConnectionApplication):
    def __init__(self, command_line, command_arg):
        super().__init__()

    def handle(self, in_string):
        req = json.loads(in_string)
        query = dict(req.get("query") or [])
        headers = {k.lower(): v for k, v in (req.get("headers") or [])}
        auth = headers.get("authorization", "")

        if auth != f"Bearer {EXPECTED_TOKEN}":
            return {
                "status": 401,
                "payload": {"error": "bad or missing bearer", "auth_header_seen": bool(auth)},
            }

        seconds = float(query.get("seconds", "5"))
        lines = max(1, int(query.get("lines", "5")))
        started = time.time()
        out = []
        for i in range(lines):
            time.sleep(seconds / lines)
            out.append(json.dumps({"status": "thinking", "i": i, "t": round(time.time() - started, 2)}))
        out.append(
            json.dumps(
                {
                    "status": "completed",
                    "success": True,
                    "response": "spike ok",
                    "elapsed": round(time.time() - started, 2),
                    "pid": os.getpid(),
                    "path_info": req.get("path_info"),
                    "rest_path": req.get("rest_path"),
                    "method": req.get("method"),
                    "payload_bytes": len(req.get("payload") or ""),
                    "python": sys.version.split()[0],
                }
            )
        )
        return {
            "status": 200,
            "headers": {"Content-Type": "application/x-ndjson"},
            "payload": "\n".join(out) + "\n",
        }
