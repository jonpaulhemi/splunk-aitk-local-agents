"""Shared splunkd entry-point behaviour for this app's persistent handlers."""

import json

from aitk_local import log


def reply(handle, in_string, what):
    """Run `handle` on the request; on any exception still reply, and log why.

    splunkd turns an escaped exception into an opaque 500 with nothing logged.
    """
    try:
        return handle(json.loads(in_string))
    except Exception:
        log.get_logger().exception("%s failed", what)
        return {"status": 500, "payload": {"message": "Local agent runtime error; see aitk_local.log"}}
