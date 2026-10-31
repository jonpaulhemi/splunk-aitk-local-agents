"""splunkd entry point for the instance-role connection setup endpoint."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from splunk.persistconn.application import PersistentServerConnectionApplication

from aitk_local import isolated


class Connections(PersistentServerConnectionApplication):
    def __init__(self, command_line, command_arg):
        super().__init__()

    def handle(self, in_string):
        # Testing a connection needs boto3 from lib/, so it runs in a worker.
        return isolated.run("connections", in_string)
