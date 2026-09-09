"""``tests/helpers/tree_gate.py`` is the whole-tree gates' one file walker.

Each test here exercises a real failure mode from the shared checkout, not a
stub of one: a file that genuinely vanishes between enumeration and read, a
file git genuinely ignores, and a file that genuinely does not parse.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.helpers import tree_gate
from tests.helpers.tree_gate import (
    GateSourceError,
    TreeNotFrozenError,
    iter_gate_files,
    iter_gate_sources,
)

from elspeth_lints.core.ast_walker import ParsedPythonFile

REPO_ROOT = Path(__file__).resolve().parents[3]


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def _write(path: Path, source: str = "x = 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_sources_are_parsed_and_sorted(tmp_path: Path) -> None:
    b = _write(tmp_path / "pkg" / "b.py", "b = 2\n")
    a = _write(tmp_path / "pkg" / "a.py", "a = 1\n")

    parsed = list(iter_gate_sources(tmp_path))

    assert [p.path for p in parsed] == [a, b]
    assert all(isinstance(p, ParsedPythonFile) for p in parsed)
    assert parsed[0].source == "a = 1\n"


def test_vanished_file_raises_tree_not_frozen_rather_than_skipping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sibling deleting a file mid-walk voids the gate; it does not narrow it."""
    _write(tmp_path / "keep.py")
    doomed = _write(tmp_path / "test_zz_scratch_repro.py")

    real_iter = tree_gate.iter_gate_files

    def enumerate_then_delete(root: Path):
        for path in list(real_iter(root)):
            if path == doomed:
                doomed.unlink()  # the race: enumerated, then gone before the read
            yield path

    monkeypatch.setattr(tree_gate, "iter_gate_files", enumerate_then_delete)

    with pytest.raises(TreeNotFrozenError, match=r"test_zz_scratch_repro\.py vanished.*worktree.*tests/_scratch/"):
        list(iter_gate_sources(tmp_path))


def test_gitignored_files_are_invisible_to_gates(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    (repo / ".gitignore").write_text("/tests/_scratch/\n", encoding="utf-8")
    visible = _write(repo / "tests" / "unit" / "test_real.py")
    _write(repo / "tests" / "_scratch" / "test_repro.py", "assert hasattr(object(), 'x')\n")

    assert list(iter_gate_files(repo / "tests")) == [visible]


def test_outside_a_git_checkout_nothing_is_subtracted(tmp_path: Path) -> None:
    """Scope only ever widens when git cannot answer — never narrows silently."""
    assert not (tmp_path / ".git").exists()
    scratch = _write(tmp_path / "tests" / "_scratch" / "test_repro.py")
    visible = _write(tmp_path / "tests" / "unit" / "test_real.py")

    assert list(iter_gate_files(tmp_path / "tests")) == [scratch, visible]


def test_unparseable_file_raises_rather_than_being_skipped(tmp_path: Path) -> None:
    _write(tmp_path / "ok.py")
    _write(tmp_path / "broken.py", "def (:\n")

    with pytest.raises(GateSourceError, match=r"broken\.py:1:\d+"):
        list(iter_gate_sources(tmp_path))


def test_the_real_scratch_directory_is_gitignored_and_gate_invisible(tmp_path: Path) -> None:
    """Pins the .gitignore rule the helper's docstring promises."""
    probe = REPO_ROOT / "tests" / "_scratch" / "test_tree_gate_probe.py"
    probe.parent.mkdir(exist_ok=True)
    probe.write_text("x = 1\n", encoding="utf-8")
    try:
        ignored = subprocess.run(["git", "check-ignore", "-q", str(probe)], cwd=REPO_ROOT, check=False, capture_output=True)
        assert ignored.returncode == 0, "tests/_scratch/ must be gitignored"
        assert probe not in set(iter_gate_files(REPO_ROOT / "tests"))
    finally:
        probe.unlink()
