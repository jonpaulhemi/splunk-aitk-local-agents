"""One request to one of this app's handlers, in a fresh interpreter.

    python aitk_local_worker.py <handler>   < request JSON   > reply JSON

Started by aitk_local.isolated; see there for why.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aitk_local import bundled

bundled.activate()  # before anything else imports a library lib/ also ships

from aitk_local import diagnostics, entry, runtime

HANDLERS = {"runtime": runtime.handle, "diagnostics": diagnostics.handle}


def main():
    name = sys.argv[1]
    in_string = sys.stdin.buffer.read()
    reply_to = sys.stdout
    sys.stdout = sys.stderr  # stray prints must not corrupt the reply
    reply = entry.reply(HANDLERS[name], in_string, name)
    reply_to.write(json.dumps(reply))
    reply_to.flush()


if __name__ == "__main__":
    main()
