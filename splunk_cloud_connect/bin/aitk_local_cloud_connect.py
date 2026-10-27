"""splunkd entry point for the Cloud Connect stand-in."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from splunk.persistconn.application import PersistentServerConnectionApplication

from aitk_local import log, standin


class CloudConnectStandIn(PersistentServerConnectionApplication):
    def __init__(self, command_line, command_arg):
        super().__init__()

    def handle(self, in_string):
        # splunkd turns an escaped exception into an opaque 500 with nothing
        # logged, so always reply and leave a traceback in aitk_local.log.
        try:
            return standin.handle(json.loads(in_string))
        except Exception:
            log.get_logger().exception("Cloud Connect stand-in failed")
            return {"status": 500, "payload": {"message": "Local agent runtime error; see aitk_local.log"}}
