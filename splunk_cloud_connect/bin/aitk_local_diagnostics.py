"""splunkd entry point for the runtime self-check."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aitk_local import bundled

bundled.activate()  # before anything else imports a library lib/ also ships

from splunk.persistconn.application import PersistentServerConnectionApplication

from aitk_local import diagnostics, entry


class Diagnostics(PersistentServerConnectionApplication):
    def __init__(self, command_line, command_arg):
        super().__init__()

    def handle(self, in_string):
        return entry.reply(diagnostics.handle, in_string, "Diagnostics")
