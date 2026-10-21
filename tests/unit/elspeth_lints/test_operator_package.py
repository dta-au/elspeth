"""Offline operator artifact assembly must never import input wheel code."""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest


def _assemble(tmp_path: Path, wheel: Path) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parents[3] / "elspeth-lints/operator-runtime/package_runtime.py"
    return subprocess.run(
        [
            "/usr/bin/python3.12",
            "-I",
            "-S",
            str(script),
            "--output",
            str(tmp_path / "assembled"),
            "--operator-uid",
            "123",
            "--candidate-uid",
            "456",
            "--wheel",
            str(wheel),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_assembles_manifest_without_importing_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "synthetic-1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("elspeth_lints/__init__.py", "raise RuntimeError('must never import input')\n")
        archive.writestr("elspeth_lints/core/operator_runtime.py", "# synthetic wheel bytes\n")
    result = _assemble(tmp_path, wheel)
    assert result.returncode == 0, result.stderr
    artifact = tmp_path / "assembled"
    manifest = json.loads((artifact / "runtime-manifest.json").read_text(encoding="utf-8"))
    assert manifest["operator_uid"] == 123
    assert manifest["candidate_uid"] == 456
    assert set(manifest["files"]) == {
        "bootstrap.py",
        "lib/elspeth_lints/__init__.py",
        "lib/elspeth_lints/core/operator_runtime.py",
    }


@pytest.mark.parametrize("member", ["../escaped.py", "injected.pth", "sitecustomize.py", "package.egg-link"])
def test_assembly_rejects_editable_and_traversal_members(tmp_path: Path, member: str) -> None:
    wheel = tmp_path / "hostile-1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(member, "import hostile\n")
    result = _assemble(tmp_path, wheel)
    assert result.returncode == 2
    assert "operator artifact assembly refused" in result.stderr
    assert not (tmp_path / "assembled").exists()
    assert not (tmp_path / "escaped.py").exists()
