"""splunkd entry point for the offline setup action."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from splunk.persistconn.application import PersistentServerConnectionApplication

from aitk_local import entry, offline_setup


class OfflineSetup(PersistentServerConnectionApplication):
    def __init__(self, command_line, command_arg):
        super().__init__()

    def handle(self, in_string):
        return entry.reply(offline_setup.handle, in_string, "Offline setup")
