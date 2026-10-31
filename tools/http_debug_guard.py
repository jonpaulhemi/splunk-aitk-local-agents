#!/usr/bin/env python3
"""Workaround for AI Toolkit 6.2.0's "unexpected data received (73)". Standard library only.

AI Toolkit's bin/agent_manager/agent_manager.py turns on Python's HTTP debug
output for its whole REST handler process (http.client debuglevel = 1). Each
HTTP call then prints "send: ...", "reply: ..." and "header: ..." lines,
including the Authorization header, to stdout. In a splunkd persistent REST
handler, stdout is the pipe splunkd reads replies from, so some Launchpad chats
and agent create/delete calls fail with "unexpected data received (73)" (0x73
is the "s" of "send:").

This installs a small startup hook into Splunk's own Python 3.13: a .pth file
in its site-packages that, in persistent REST workers only
(splunk/persistconn/appserver.py), throws away http.client's debug prints.
AI Toolkit's files aren't touched. Run it on the search head as root or as the
user splunkd runs as, then restart splunkd so every worker picks it up:

    sudo python3 http_debug_guard.py install
    sudo systemctl restart Splunkd      # or: $SPLUNK_HOME/bin/splunk restart
    sudo python3 http_debug_guard.py status

    sudo python3 http_debug_guard.py uninstall   # then restart again

The hook lives in Splunk's install, not in an app, so a Splunk upgrade removes
it: run install again after upgrading Splunk. Uninstall it once AI Toolkit
ships a version without the debug switch.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

NAME = "aitk_http_debug_guard"

GUARD = '''"""Installed by splunk_cloud_connect's tools/http_debug_guard.py.

AI Toolkit 6.2.0 turns on http.client's debug output, which prints into the
reply pipe of splunkd persistent REST handlers and corrupts replies
("unexpected data received (73)"). In those workers only, throw the debug
prints away. Remove with: python3 http_debug_guard.py uninstall
"""
import sys as _sys

if _sys.argv and _sys.argv[0].replace("\\\\", "/").endswith("splunk/persistconn/appserver.py"):
    import http.client as _http_client

    def _discard(*args, **kwargs):
        pass

    _http_client.print = _discard
'''

PTH = f"import {NAME}\n"

# Runs under Splunk's Python as if it were a persistent REST worker (its path
# ends in splunk/persistconn/appserver.py), so the hook should be active.
CHECK = f'''import builtins, contextlib, io, site, sys
site.addsitedir(sys.argv[1])
import http.client
out = io.StringIO()
with contextlib.redirect_stdout(out):
    http.client.print("send:", "check")
hooked = getattr(http.client.print, "__module__", None) == "{NAME}"
print("active" if hooked and not out.getvalue() else "inactive")
'''


def splunk_python(splunk_home):
    for name in ("python3.13", "python3"):
        path = Path(splunk_home) / "bin" / name
        if path.exists():
            return str(path)
    sys.exit(f"no Python found under {splunk_home}/bin; pass --splunk-home or --python")


def site_packages(python):
    out = subprocess.run(
        [python, "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True, text=True, check=True,
    )
    return Path(out.stdout.strip())


def paths(site_dir):
    return site_dir / f"{NAME}.py", site_dir / f"{NAME}.pth"


def install(site_dir):
    stat = site_dir.stat()
    module, pth = paths(site_dir)
    # Module first: a .pth naming a missing module would print an error at
    # every Python startup.
    for path, text in ((module, GUARD), (pth, PTH)):
        path.write_text(text)
        path.chmod(0o644)
        if os.geteuid() == 0:
            os.chown(path, stat.st_uid, stat.st_gid)
    print(f"installed {pth} and {module.name}; restart splunkd so every REST worker loads it")


def uninstall(site_dir):
    module, pth = paths(site_dir)
    # .pth first, for the same reason as above.
    removed = [p for p in (pth, module) if p.exists()]
    for path in removed:
        path.unlink()
    for cached in (site_dir / "__pycache__").glob(f"{NAME}.*.pyc"):
        cached.unlink()
    if removed:
        print(f"removed {', '.join(p.name for p in removed)}; restart splunkd to unload it from running workers")
    else:
        print("not installed")


def status(site_dir, python):
    module, pth = paths(site_dir)
    if not (module.exists() and pth.exists()):
        print(f"not installed in {site_dir}")
        return 1
    if module.read_text() != GUARD or pth.read_text() != PTH:
        print(f"installed in {site_dir}, but the files differ from this version; run install again")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        worker = Path(tmp, "splunk", "persistconn", "appserver.py")
        worker.parent.mkdir(parents=True)
        worker.write_text(CHECK)
        out = subprocess.run([python, str(worker), str(site_dir)], capture_output=True, text=True)
    if out.stdout.strip() != "active":
        print(f"installed in {site_dir}, but not active in a REST worker: {(out.stdout + out.stderr).strip()}")
        return 1
    print(f"installed in {site_dir} and active in REST workers started from now on")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("install", "uninstall", "status"))
    parser.add_argument("--splunk-home", default=os.environ.get("SPLUNK_HOME", "/opt/splunk"))
    parser.add_argument("--python", help="Splunk's Python 3.13 (default: <splunk-home>/bin/python3.13)")
    parser.add_argument("--site-packages", type=Path, help="default: that Python's site-packages")
    args = parser.parse_args()

    python = args.python or splunk_python(args.splunk_home)
    site_dir = args.site_packages or site_packages(python)
    if args.action == "install":
        install(site_dir)
    elif args.action == "uninstall":
        uninstall(site_dir)
    else:
        sys.exit(status(site_dir, python))


if __name__ == "__main__":
    main()
