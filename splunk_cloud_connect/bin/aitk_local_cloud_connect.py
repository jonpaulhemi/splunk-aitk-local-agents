"""splunkd entry point for the Cloud Connect stand-in."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from splunk.persistconn.application import PersistentServerConnectionApplication

from aitk_local import entry, standin


class CloudConnectStandIn(PersistentServerConnectionApplication):
    def __init__(self, command_line, command_arg):
        super().__init__()

    def handle(self, in_string):
        return entry.reply(standin.handle, in_string, "Cloud Connect stand-in")
