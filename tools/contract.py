#!/usr/bin/env python3
"""Run the contract tests against an AI Toolkit release, in one command.

    python tools/contract.py ~/Downloads/splunk-ai-toolkit_630.tgz
    python tools/contract.py /path/to/unpacked/Splunk_ML_Toolkit
    python tools/contract.py <package> -- -k approvals     # extra pytest arguments

The tests drive AI Toolkit's own client code (agent create/invoke/delete,
`| aiagent`, approvals, Launchpad's result handling) against this app's
handlers, so a pass means the new version still speaks the contract in
docs/research/aitk-6.2.0-agentcore-contract.md. A package is unpacked into a
temporary folder and removed afterwards; AI Toolkit is never copied into the
repo. Needs the dev requirements (requirements-dev.txt) and the bundled
libraries (python tools/bundle.py build).
"""

import argparse
import configparser
import contextlib
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_ID = "Splunk_ML_Toolkit"


@contextlib.contextmanager
def toolkit(source):
    """(unpacked Splunk_ML_Toolkit folder, its version) for a .tgz/.spl or a folder."""
    source = Path(source).expanduser()
    if source.is_dir():
        yield source, _version(source)
        return
    if not tarfile.is_tarfile(source):
        sys.exit(f"{source} is neither a folder nor an app package")
    with tempfile.TemporaryDirectory(prefix="aitk_contract_") as tmp:
        with tarfile.open(source) as tar:
            tar.extractall(tmp, filter="data")
        app = Path(tmp) / APP_ID
        yield app, _version(app)


def _version(app):
    conf = configparser.ConfigParser(strict=False, interpolation=None)
    conf.read(app / "default" / "app.conf")
    package_id = conf.get("package", "id", fallback=None) or conf.get("id", "name", fallback=None)
    if package_id != APP_ID:
        sys.exit(f"{app} isn't an AI Toolkit app ({APP_ID}); point at the package or its unpacked folder")
    return conf.get("launcher", "version", fallback="unknown")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("toolkit", help="AI Toolkit .tgz/.spl, or its unpacked Splunk_ML_Toolkit folder")
    parser.add_argument("pytest_args", nargs="*", help="extra arguments for pytest, after --")
    args = parser.parse_args()
    if not (REPO / "splunk_cloud_connect" / "lib").is_dir():
        sys.exit("splunk_cloud_connect/lib is missing; run `python tools/bundle.py build` first")
    with toolkit(args.toolkit) as (app, version):
        print(f"contract tests against AI Toolkit {version}", file=sys.stderr)
        env = {**os.environ, "AITK_APP": str(app)}
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", *args.pytest_args], cwd=REPO, env=env)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
