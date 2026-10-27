"""Logging to $SPLUNK_HOME/var/log/splunk/aitk_local.log (indexed into _internal).

Never log tokens, keys, credentials or decrypted payloads.
"""

import logging
import logging.handlers
import os

_NAME = "aitk_local"


def get_logger():
    logger = logging.getLogger(_NAME)
    if not logger.handlers:
        path = os.path.join(os.environ["SPLUNK_HOME"], "var", "log", "splunk", f"{_NAME}.log")
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=25_000_000, backupCount=5)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s pid=%(process)d %(module)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
