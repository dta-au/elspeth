#!/usr/bin/env python3
"""Pre-commit ratchet for the trust-tier (``trust_tier.tier_model``) lint corpus.

``elspeth-lints check`` is deliberately fail-closed: it exits 1 whenever the
standing finding corpus is non-empty, and that corpus stays non-empty until the
operator signs the package (AGENTS.md, "Judge-signature stage"). Used directly
as a pre-commit entry it therefore refuses every commit that touches its
trigger paths, including commits that strictly shrink the corpus. This script
is the hook's entry instead. It runs the same rule with the same arguments and
environment twice -- over the tree pre-commit is about to commit and over
``git archive HEAD`` -- and fails only when the commit would ADD a failing
finding that HEAD does not already carry. The CLI gate itself is untouched and
CI still runs it fail-closed.

Findings are compared as a multiset of line-insensitive keys
``(path, rule id, message)``. Line and column numbers move under unrelated
edits and never count as new; a second occurrence of an identical key does.
Only findings the CLI would fail on take part (every severity except ``note``):
the ``R_TB_SUPPRESSED`` notes that record each ``@trust_boundary`` suppression
appear in the counts but never gate.

The HEAD run extracts ``git archive HEAD`` into a temporary directory and puts
that copy's ``elspeth-lints/src`` on ``PYTHONPATH``, so it sees HEAD's rule
code, HEAD's ``src/elspeth`` and HEAD's allowlists together; the import path is
verified before the rule runs. The working-tree side is whatever is on disk:
the repo-local dispatcher runs pre-commit without stashing, so unstaged edits
are visible here exactly as they are to every sibling whole-repo hook.

Project tooling, not product: no signatures and no key handling. The verify
mode is pinned to ``shape-only-when-key-missing`` for both runs, exactly as the
previous hook entry pinned it, and the operator's HMAC key, when present in the
shell, reaches the rule untouched.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

RULE_ID = "trust_tier.tier_model"
SCAN_ROOT = "src/elspeth"
LINTS_SOURCE_ROOT = "elspeth-lints/src"
VERIFY_MODE_ENV = "ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE"
VERIFY_MODE = "shape-only-when-key-missing"
NON_GATING_SEVERITY = "note"

FindingKey = tuple[str, str, str]


class RatchetError(Exception):
    """The ratchet could not produce a verdict (usage or environment failure)."""


@dataclass(frozen=True, slots=True)
class FindingRecord:
    """One lint finding as the ratchet sees it: tree-relative path, no signature fields."""

    path: str
    rule_id: str
    message: str
    line: int
    column: int
    severity: str

    @property
    def key(self) -> FindingKey:
        """Line-insensitive identity: an unrelated edit that shifts lines keeps the key."""
        return (self.path, self.rule_id, self.message)

    @property
    def gates(self) -> bool:
        """Mirror the CLI's exit rule: every severity except ``note`` fails the gate."""
        return self.severity != NON_GATING_SEVERITY

    def render(self) -> str:
        """Same shape as the CLI's text emitter, so an added line is recognisable."""
        return f"{self.path}:{self.line}:{self.column}: {self.rule_id}: {self.message}"


@dataclass(frozen=True, slots=True)
class AddedKey:
    """A key whose staged multiplicity exceeds HEAD's, with every staged occurrence."""

    head_multiplicity: int
    staged_multiplicity: int
    occurrences: tuple[FindingRecord, ...]


@dataclass(frozen=True, slots=True)
class RatchetResult:
    """Outcome of comparing the staged corpus against HEAD's."""

    head_count: int
    staged_count: int
    head_notes: int
    staged_notes: int
    added: tuple[AddedKey, ...]
    removed_count: int

    @property
    def added_count(self) -> int:
        return sum(entry.staged_multiplicity - entry.head_multiplicity for entry in self.added)

    @property
    def exit_code(self) -> int:
        return 1 if self.added else 0


def compare_corpora(head: Sequence[FindingRecord], staged: Sequence[FindingRecord]) -> RatchetResult:
    """Staged gating findings must be a sub-multiset of HEAD's; anything in surplus is added."""
    head_gating = [finding for finding in head if finding.gates]
    staged_gating = [finding for finding in staged if finding.gates]
    head_keys = Counter(finding.key for finding in head_gating)
    staged_keys = Counter(finding.key for finding in staged_gating)
    surplus = staged_keys - head_keys
    deficit = head_keys - staged_keys
    added = tuple(
        AddedKey(
            head_multiplicity=head_keys[key],
            staged_multiplicity=staged_keys[key],
            occurrences=tuple(finding for finding in staged_gating if finding.key == key),
        )
        for key in sorted(surplus)
    )
    return RatchetResult(
        head_count=len(head_gating),
        staged_count=len(staged_gating),
        head_notes=len(head) - len(head_gating),
        staged_notes=len(staged) - len(staged_gating),
        added=added,
        removed_count=sum(deficit.values()),
    )


def format_report(result: RatchetResult, *, head_sha: str) -> str:
    """Human-readable verdict: counts first, then every added finding verbatim."""
    lines = [
        f"trust-tier ratchet: working tree vs HEAD {head_sha}",
        (
            f"  gating findings: head {result.head_count}, staged {result.staged_count}, "
            f"added {result.added_count}, removed {result.removed_count}"
        ),
        f"  note findings (never gate): head {result.head_notes}, staged {result.staged_notes}",
    ]
    if not result.added:
        lines.append("OK: the staged tree adds no trust-tier finding relative to HEAD.")
        return "\n".join(lines) + "\n"
    lines.append(f"FAIL: the staged tree adds {result.added_count} trust-tier finding(s) relative to HEAD:")
    for entry in result.added:
        if entry.head_multiplicity:
            lines.append(f"  (key already present {entry.head_multiplicity}x in HEAD, {entry.staged_multiplicity}x staged)")
        lines.extend(f"+ {finding.render()}" for finding in entry.occurrences)
    lines.append(
        "Remove the new finding or, for an honest Tier-3 boundary, stage it for the judge; never hand-edit an allowlist signature."
    )
    return "\n".join(lines) + "\n"


def normalise_path(raw: str, *, tree_root: Path) -> str:
    """Make an absolute path under ``tree_root`` tree-relative; leave relative paths as emitted."""
    if not os.path.isabs(raw):
        return raw
    resolved = Path(os.path.realpath(raw))
    root = Path(os.path.realpath(tree_root))
    if resolved.is_relative_to(root):
        return resolved.relative_to(root).as_posix()
    return raw


def _field(record: dict[str, object], name: str) -> object:
    if name not in record:
        raise RatchetError(f"lint JSON finding lacks the {name!r} field: {record!r}")
    return record[name]


def _string_field(record: dict[str, object], name: str) -> str:
    value = _field(record, name)
    if not isinstance(value, str):
        raise RatchetError(f"lint JSON finding field {name!r} is not a string: {value!r}")
    return value


def _int_field(record: dict[str, object], name: str) -> int:
    value = _field(record, name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RatchetError(f"lint JSON finding field {name!r} is not an integer: {value!r}")
    return value


def parse_findings(payload: str, *, tree_root: Path) -> list[FindingRecord]:
    """Parse the CLI's ``--format json`` document into owned records."""
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RatchetError(f"lint output is not JSON: {exc}") from exc
    if not isinstance(document, list):
        raise RatchetError(f"lint JSON document is not a list: {type(document).__name__}")
    findings: list[FindingRecord] = []
    for record in document:
        if not isinstance(record, dict):
            raise RatchetError(f"lint JSON finding is not an object: {record!r}")
        findings.append(
            FindingRecord(
                path=normalise_path(_string_field(record, "file_path"), tree_root=tree_root),
                rule_id=_string_field(record, "rule_id"),
                message=_string_field(record, "message"),
                line=_int_field(record, "line"),
                column=_int_field(record, "column"),
                severity=_string_field(record, "severity"),
            )
        )
    return findings


def lint_environment() -> dict[str, str]:
    """The previous hook entry's environment: relative lints path, pinned verify mode, nothing else touched."""
    env = dict(os.environ)
    env["PYTHONPATH"] = LINTS_SOURCE_ROOT
    env[VERIFY_MODE_ENV] = VERIFY_MODE
    return env


def verify_lints_import_path(tree_root: Path, *, python: str) -> Path:
    """Prove the rule code the run will import lives inside ``tree_root``."""
    probe = subprocess.run(
        [python, "-c", "import os, elspeth_lints; print(os.path.realpath(elspeth_lints.__file__))"],
        cwd=tree_root,
        env=lint_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise RatchetError(f"could not import elspeth_lints from {tree_root}:\n{probe.stderr}")
    imported = Path(probe.stdout.strip())
    expected = Path(os.path.realpath(tree_root)) / LINTS_SOURCE_ROOT
    if not imported.is_relative_to(expected):
        raise RatchetError(f"elspeth_lints imported from {imported}, expected a module under {expected}")
    return imported


def run_tier_model_check(tree_root: Path, *, python: str) -> tuple[list[FindingRecord], str]:
    """Run the rule exactly as the previous hook entry did; return findings and the run's stderr."""
    completed = subprocess.run(
        [python, "-m", "elspeth_lints.core.cli", "check", "--rules", RULE_ID, "--root", SCAN_ROOT, "--format", "json"],
        cwd=tree_root,
        env=lint_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in (0, 1):
        raise RatchetError(f"elspeth-lints check exited {completed.returncode} in {tree_root}:\n{completed.stderr}")
    return parse_findings(completed.stdout, tree_root=tree_root), completed.stderr


def export_head(repo_root: Path, destination: Path) -> str:
    """Extract ``git archive HEAD`` into ``destination``; return HEAD's sha."""
    head = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    if head.returncode != 0:
        raise RatchetError(f"git rev-parse HEAD failed in {repo_root}:\n{head.stderr}")
    archive = subprocess.run(["git", "-C", str(repo_root), "archive", "--format=tar", "HEAD"], capture_output=True, check=False)
    if archive.returncode != 0:
        raise RatchetError(f"git archive HEAD failed in {repo_root}:\n{archive.stderr.decode(errors='replace')}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(destination, filter="data")
    return head.stdout.strip()


def _repo_root_from_cwd() -> Path:
    toplevel = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    if toplevel.returncode != 0:
        raise RatchetError(f"not inside a git repository:\n{toplevel.stderr}")
    return Path(toplevel.stdout.strip())


def ratchet(repo_root: Path, *, python: str) -> tuple[RatchetResult, str]:
    """Run both sides and compare; returns the result and HEAD's sha."""
    with tempfile.TemporaryDirectory(prefix="trust-tier-ratchet-head-") as scratch:
        head_tree = Path(scratch) / "head"
        head_tree.mkdir()
        head_sha = export_head(repo_root, head_tree)
        verify_lints_import_path(head_tree, python=python)
        head_findings, _head_stderr = run_tier_model_check(head_tree, python=python)
    verify_lints_import_path(repo_root, python=python)
    staged_findings, staged_stderr = run_tier_model_check(repo_root, python=python)
    sys.stderr.write(staged_stderr)
    return compare_corpora(head_findings, staged_findings), head_sha


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail only when the staged tree adds a trust-tier finding HEAD does not have.")
    parser.add_argument("--repo-root", type=Path, default=None, help="Repository root (default: git rev-parse --show-toplevel)")
    parser.add_argument("--python", default=sys.executable, help="Interpreter for both lint runs (default: this one)")
    args = parser.parse_args(argv)
    try:
        repo_root = args.repo_root if args.repo_root is not None else _repo_root_from_cwd()
        result, head_sha = ratchet(repo_root, python=args.python)
    except RatchetError as exc:
        sys.stderr.write(f"trust-tier ratchet: {exc}\n")
        return 2
    sys.stdout.write(format_report(result, head_sha=head_sha))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
