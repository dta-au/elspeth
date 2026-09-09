"""The ONE way a whole-tree test gate enumerates and reads Python files.

Whole-tree gates (``tests/unit/test_no_hasattr_branching.py`` and its ~25
siblings) assert exact expected sets over every ``*.py`` under a root. Each
once did its own ``root.rglob("*.py")`` + ``read_text`` + ``ast.parse``,
which exposed every one of them to the same race on the shared checkout: a
sibling agent session drops a scratch file into ``tests/`` and deletes it
between the glob and the read, and the gate dies with a bare
``FileNotFoundError`` that names a file nobody committed (2026-08-28,
``test_zz_scratch_repro_39578c6f.py``).

This module fixes that in two ways, and both are deliberate:

* **Enumeration derives from the exclusion authority.** Paths come from
  :func:`elspeth_lints.core.ast_walker.iter_python_files`, so a gate can
  never see a worktree, a venv, or a ``__pycache__`` — and there is no
  second walker to keep in sync (``test_python_file_walker_authority.py``
  pins ``tests/`` against a fresh ``rglob("*.py")``).
* **Gitignored files are invisible to gates.** Anything ``git`` reports as
  ignored (``tests/_scratch/``, an editor's swap file, a generated
  fixture) is subtracted, so local gates measure exactly the tree CI will
  measure. Scratch repro files therefore belong in ``tests/_scratch/``:
  pytest still collects and runs them, but no gate counts them. Outside a
  git checkout the subtraction is skipped — scope only ever widens, never
  narrows silently.

A file that vanishes mid-walk is NOT skipped. Silently dropping a file from
an allowlist gate converts a race into an invisible scope narrowing, which
is worse than the crash it replaces. Instead the gate raises
:class:`TreeNotFrozenError` naming the file and the remedy (run long suites
in a worktree — AGENTS.md). Decode and syntax failures raise
:class:`GateSourceError`: every ``*.py`` a gate is pointed at is expected to
parse, and a gate that quietly ``continue``s past one has stopped measuring
the tree it claims to.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

from elspeth_lints.core.ast_walker import (
    ParsedPythonFile,
    PythonFileReadError,
    PythonSyntaxError,
    iter_python_files,
    parse_python_file,
)

__all__ = [
    "GateSourceError",
    "ParsedPythonFile",
    "TreeNotFrozenError",
    "iter_gate_files",
    "iter_gate_sources",
]


class TreeNotFrozenError(RuntimeError):
    """A file enumerated by a whole-tree gate vanished before it was read.

    The tree changed under the gate — almost always a sibling session
    writing to the shared checkout while a long suite run was in flight.
    The gate's result is void, not merely incomplete.
    """


class GateSourceError(RuntimeError):
    """A file enumerated by a whole-tree gate could not be decoded or parsed."""


def _gitignored_under(root: Path) -> frozenset[Path]:
    """Every ignored, untracked file under ``root`` as ``git`` sees it.

    Empty when ``root`` is not inside a git work tree (or ``git`` is
    unavailable): a gate then measures the whole filesystem tree, which is
    the conservative direction.
    """
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--full-name", "--others", "--ignored", "--exclude-standard", "--", str(root)],
            cwd=root if root.is_dir() else root.parent,
            capture_output=True,
            check=False,
        )
    except OSError:
        return frozenset()
    if completed.returncode != 0:
        return frozenset()
    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=root if root.is_dir() else root.parent,
        capture_output=True,
        check=False,
        text=True,
    )
    if toplevel.returncode != 0:
        return frozenset()
    repo_root = Path(toplevel.stdout.strip())
    return frozenset((repo_root / entry.decode("utf-8")).resolve() for entry in completed.stdout.split(b"\0") if entry)


def iter_gate_files(root: Path) -> Iterator[Path]:
    """Yield, sorted, every gate-visible ``*.py`` under ``root``.

    Derived from the exclusion authority and minus gitignored files. Use
    this when a gate needs paths only (a text probe); prefer
    :func:`iter_gate_sources` when it needs the parsed module.
    """
    ignored = _gitignored_under(root)
    for path in iter_python_files(root):
        if ignored and path.resolve() in ignored:
            continue
        yield path


def iter_gate_sources(root: Path) -> Iterator[ParsedPythonFile]:
    """Yield every gate-visible file under ``root`` read and parsed.

    Raises:
        TreeNotFrozenError: a file vanished between enumeration and read.
        GateSourceError: a file could not be decoded or does not parse.
    """
    for path in iter_gate_files(root):
        parsed = parse_python_file(path)
        if isinstance(parsed, ParsedPythonFile):
            yield parsed
            continue
        if isinstance(parsed, PythonFileReadError):
            if parsed.error_type == "FileNotFoundError":
                raise TreeNotFrozenError(
                    f"{path} vanished while the gate was walking {root}: the tree is not frozen "
                    "(a sibling session is writing to this checkout). The gate's result is void — "
                    "run long suites in a worktree, and keep scratch files in tests/_scratch/ (gitignored)."
                )
            raise GateSourceError(f"{path}: {parsed.message}")
        if isinstance(parsed, PythonSyntaxError):
            raise GateSourceError(f"{path}:{parsed.line}:{parsed.column}: {parsed.message}")
        raise AssertionError(f"unhandled parse result {parsed!r}")
