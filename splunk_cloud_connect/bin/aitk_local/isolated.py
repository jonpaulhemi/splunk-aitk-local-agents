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
