"""Run a handler in its own Python process, away from AI Toolkit's libraries.

splunkd hosts our persistent handlers in the same Python process as AI
Toolkit's. AI Toolkit puts the Scientific Computing add-on's site-packages
first on sys.path, so a library we import after it (seen live: cryptography)
comes from PSC, built against another OpenSSL, and fails to load. Each call
here gets a fresh interpreter with only this app's lib/ in front.
"""

import json
import os
import subprocess
import sys

from aitk_local import log

WORKER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aitk_local_worker.py")
TIMEOUT_SECONDS = 900
# Prompts and answers must stay on this host: no LangSmith tracing, whatever
# splunkd's environment says.
SCRUBBED_ENV = ("LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING", "LANGSMITH_TRACING", "LANGCHAIN_API_KEY",
                "LANGSMITH_API_KEY", "LANGCHAIN_ENDPOINT", "LANGSMITH_ENDPOINT",
                # A Bedrock API key in splunkd's environment would override the
                # connection's own credentials.
                "AWS_BEARER_TOKEN_BEDROCK")


def run(handler_name, in_string):
    """The reply of `handler_name` (a key of aitk_local_worker.HANDLERS) for one request."""
    data = in_string if isinstance(in_string, bytes) else in_string.encode()
    try:
        # The request carries session tokens: pass it on stdin, never argv or env.
        done = subprocess.run(
            [sys.executable, WORKER, handler_name],
            input=data,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            env=_worker_env(),
        )
    except subprocess.TimeoutExpired:
        log.get_logger().error("%s worker timed out after %ss", handler_name, TIMEOUT_SECONDS)
        return {"status": 504, "payload": {"message": "Local agent runtime timed out"}}
    try:
        if done.returncode != 0:
            raise ValueError(f"exit code {done.returncode}")
        return json.loads(done.stdout)
    except ValueError as e:
        log.get_logger().error(
            "%s worker failed (%s); stderr tail:\n%s", handler_name, e, done.stderr.decode(errors="replace")[-4000:]
        )
        return {"status": 500, "payload": {"message": "Local agent runtime error; see aitk_local.log"}}


def _worker_env():
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    env["LANGSMITH_TRACING"] = "false"
    # splunkd's environment can name a CA file or dir that doesn't exist. AI
    # Toolkit drops these when its modules load, so whether a handler process
    # still has them is luck; with them, every TLS client in the worker fails
    # with FileNotFoundError (seen live on 0.4.0).
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"):
        path = env.get(name)
        if path is not None and not os.path.exists(path):
            log.get_logger().warning("ignoring %s: %s does not exist", name, path)
            del env[name]
    return env
