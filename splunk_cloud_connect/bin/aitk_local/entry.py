"""Shared splunkd entry-point behaviour for this app's persistent handlers."""

import json

from aitk_local import log, splunkd


def reply(handle, in_string, handler_name):
    """Run `handle` on the request; on any exception still reply, and log why.

    splunkd turns an escaped exception into an opaque 500 with nothing logged.
    """
    try:
        return handle(json.loads(in_string))
    except splunkd.SplunkdError as e:
        log.get_logger().error("%s: splunkd call failed: %s", handler_name, e)
        return {
            "status": 503,
            "payload": {"message": "Local agent runtime unavailable: credential store or splunkd error"},
        }
    except Exception:
        log.get_logger().exception("%s failed", handler_name)
        return {"status": 500, "payload": {"message": "Local agent runtime error; see aitk_local.log"}}
