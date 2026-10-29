"""tools/contract.py runs the contract tests against an AI Toolkit package or folder (#24)."""

import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import contract  # noqa: E402


def _toolkit(root, version="6.3.0"):
    app = root / "Splunk_ML_Toolkit"
    (app / "default").mkdir(parents=True)
    (app / "default" / "app.conf").write_text(f"[launcher]\nversion = {version}\n\n[package]\nid = Splunk_ML_Toolkit\n")
    return app


def test_a_downloaded_package_is_unpacked_and_its_version_read(tmp_path):
    app = _toolkit(tmp_path / "src")
    package = tmp_path / "splunk-ai-toolkit_630.tgz"
    with tarfile.open(package, "w:gz") as tar:
        tar.add(app, arcname="Splunk_ML_Toolkit")

    with contract.toolkit(package) as (path, version):
        assert (path / "default" / "app.conf").exists()
        assert version == "6.3.0"
    assert not path.exists()


def test_an_unpacked_folder_is_used_where_it_is(tmp_path):
    app = _toolkit(tmp_path)

    with contract.toolkit(app) as (path, version):
        assert path == app
        assert version == "6.3.0"
    assert app.exists()


def test_anything_else_is_refused_with_a_clear_message(tmp_path):
    (tmp_path / "default").mkdir()

    with pytest.raises(SystemExit, match="isn't an AI Toolkit app"):
        with contract.toolkit(tmp_path):
            pass
