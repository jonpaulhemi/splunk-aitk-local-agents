"""Stand-in for Splunk's private `splunk` Python package, used only in tests.

Splunk ships this package inside its own Python. The harness needs the two
pieces our handlers and AI Toolkit touch: the persistent-handler base class and
`splunk.rest.simpleRequest`, which here talks to the fake splunkd over HTTPS.
"""


class RESTException(Exception):
    def __init__(self, statusCode, msg=""):
        super().__init__(msg)
        self.statusCode = statusCode
