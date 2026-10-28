"""The release .tgz passes the AppInspect checks that look at the package itself.

Needs the libraries built first: python tools/bundle.py build
"""

import shutil
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import bundle  # noqa: E402


@pytest.fixture
def package(tmp_path, monkeypatch):
    inventory = tmp_path / "bundled-libraries.md"
    shutil.copy(bundle.INVENTORY, inventory)
    monkeypatch.setattr(bundle, "DIST", tmp_path)
    monkeypatch.setattr(bundle, "INVENTORY", inventory)
    bundle.package()
    with tarfile.open(next(tmp_path.glob("*.tgz"))) as tar:
        yield tar.getmembers()


def test_nothing_in_lib_is_executable(package):
    shared = [m for m in package if m.isfile() and m.name.startswith("splunk_cloud_connect/lib/")]

    assert any(m.name.endswith(".so") for m in shared)
    assert [m.name for m in shared if m.mode & 0o111] == []


def test_no_bundled_code_opens_audio_files(package):
    names = {m.name for m in package}

    assert not any("/openai/helpers/" in n or "/langsmith/_internal/voice/" in n for n in names)

