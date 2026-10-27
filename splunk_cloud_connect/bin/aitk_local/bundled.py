"""The third-party libraries shipped in this app's lib/ (built by tools/bundle.py)."""

import os
import sys

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib")


def activate():
    """Put lib/ first on sys.path so bundled versions win over Splunk's own."""
    if not os.path.isdir(LIB):
        raise RuntimeError(f"bundled libraries missing at {LIB}; build with tools/bundle.py build")
    if sys.path[:1] != [LIB]:
        sys.path.insert(0, LIB)


def shadowed():
    """Loaded modules that lib/ provides but that came from somewhere else."""
    provided = {name.split(".")[0] for name in os.listdir(LIB)}
    out = []
    for name, module in list(sys.modules.items()):
        top = name.split(".")[0]
        path = getattr(module, "__file__", None)
        if top in provided and path and not os.path.abspath(path).startswith(LIB + os.sep):
            out.append(name)
    return sorted(out)
