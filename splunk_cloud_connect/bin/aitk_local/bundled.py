"""The third-party libraries shipped in this app's lib/ (built by tools/bundle.py)."""

import os
import sys

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib")


def activate():
    """Put lib/ first on sys.path so bundled versions win over Splunk's own.

    Call it before anything else is imported: a module already loaded from
    Splunk's site-packages stays loaded (see shadowed()).
    """
    if not os.path.isdir(LIB):
        raise RuntimeError(f"bundled libraries missing at {LIB}; build with tools/bundle.py build")
    if sys.path[:1] != [LIB]:
        sys.path.insert(0, LIB)


def top_level_modules():
    """Importable top-level names in lib/, e.g. 'langgraph', 'six', '_cffi_backend'.

    Read from each package's RECORD, since some (langgraph) are namespace
    packages with no __init__.py.
    """
    names = set()
    for info in os.listdir(LIB):
        if not info.endswith(".dist-info"):
            continue
        with open(os.path.join(LIB, info, "RECORD")) as record:
            for line in record:
                first = line.split(",", 1)[0].split("/", 1)[0]
                if first.endswith(".py"):
                    names.add(first[:-3])
                elif first.endswith(".so"):
                    names.add(first.split(".", 1)[0])
                elif "/" in line.split(",", 1)[0] and not first.endswith((".dist-info", ".libs", ".data")) \
                        and first not in ("..", "bin", "__pycache__") and first.isidentifier():
                    names.add(first)
    return sorted(names)


def shadowed():
    """Loaded modules that lib/ provides but that came from somewhere else."""
    provided = set(top_level_modules())
    found = []
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if name.split(".")[0] in provided and path and not os.path.abspath(path).startswith(LIB + os.sep):
            found.append(name)
    return sorted(found)
