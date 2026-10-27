"""splunkd entry point for the local agent runtime."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aitk_local import bundled

bundled.activate()  # before anything else imports a library lib/ also ships

from splunk.persistconn.application import PersistentServerConnectionApplication

from aitk_local import entry, runtime


class Runtime(PersistentServerConnectionApplication):
    def __init__(self, command_line, command_arg):
        super().__init__()

    def handle(self, in_string):
        return entry.reply(runtime.handle, in_string, "Local agent runtime")
