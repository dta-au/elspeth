#!/usr/bin/env python3
"""Keyless HEAD/working-tree measurement for ``trust_tier.tier_model``.

The local hook uses explicit ``--report-only``: exit zero means both scans and
their comparison completed, never adjudication or permission to land. Added
occurrences require review under release A4. Default/``--strict`` CLI behavior
still refuses surplus non-note ``(path, rule, message)`` multiplicities.

Full raw occurrence additions/removals, including suppression notes and identity
fields, are reported separately. Identical normalized multiplicity can conceal
site replacement or coordinate churn; that summary is not occurrence clearance.
This single-rule measurement does not replace A4's complete ``--rules all``
base/tip review. No signatures, baseline advancement or approval store is used.

HEAD's rule, source and allowlists are read together from a temporary archive
inside the worktree cache. Working-tree scans include unstaged bytes. Both runs
verify their lint import root and pin keyless shape verification; an operator
key in the environment is refused rather than forwarded.
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
from dataclasses import dataclass, field
from pathlib import Path

RULE_ID = "trust_tier.tier_model"
SCAN_ROOT = "src/elspeth"
LINTS_SOURCE_ROOT = "elspeth-lints/src"
VERIFY_MODE_ENV = "ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE"
VERIFY_MODE = "shape-only-when-key-missing"
NON_GATING_SEVERITY = "note"
OPERATOR_KEY_ENV = "ELSPETH_JUDGE_METADATA_HMAC_KEY"

FindingKey = tuple[str, str, str]


class RatchetError(Exception):
    """The ratchet could not produce a verdict (usage or environment failure)."""


@dataclass(frozen=True, slots=True)
class FindingRecord:
    """Owned summary plus the complete CLI occurrence, with a tree-relative path."""

    path: str
    rule_id: str
    message: str
    line: int
    column: int
    severity: str
    raw_json: str = field(default="", compare=False)

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

    @property
    def occurrence_json(self) -> str:
        """Complete normalized CLI row, or the explicit synthetic record fields."""
        return self.raw_json or json.dumps(
            {
                "file_path": self.path,
                "rule_id": self.rule_id,
                "message": self.message,
                "line": self.line,
                "column": self.column,
                "severity": self.severity,
            },
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class AddedKey:
    """A surplus working-tree key; staged_* names are historical compatibility."""

    head_multiplicity: int
    staged_multiplicity: int
    occurrences: tuple[FindingRecord, ...]


@dataclass(frozen=True, slots=True)
class RatchetResult:
    """Working-tree measurement; historical staged_* field names remain compatible."""

    head_count: int
    staged_count: int
    head_notes: int
    staged_notes: int
    added: tuple[AddedKey, ...]
    removed_count: int
    raw_added: tuple[FindingRecord, ...]
    raw_removed: tuple[FindingRecord, ...]

    @property
    def added_count(self) -> int:
        return sum(entry.staged_multiplicity - entry.head_multiplicity for entry in self.added)

    @property
    def exit_code(self) -> int:
        return 1 if self.added else 0


def compare_corpora(head: Sequence[FindingRecord], staged: Sequence[FindingRecord]) -> RatchetResult:
    """Compute the strict normalized summary and independent full occurrence churn."""
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
        raw_added=_occurrence_surplus(staged, head),
        raw_removed=_occurrence_surplus(head, staged),
    )


def _occurrence_surplus(current: Sequence[FindingRecord], previous: Sequence[FindingRecord]) -> tuple[FindingRecord, ...]:
    remaining = Counter(finding.occurrence_json for finding in current) - Counter(finding.occurrence_json for finding in previous)
    surplus: list[FindingRecord] = []
    for finding in current:
        if remaining[finding.occurrence_json]:
            surplus.append(finding)
            remaining[finding.occurrence_json] -= 1
    return tuple(surplus)


def format_report(result: RatchetResult, *, head_sha: str, report_only: bool = False) -> str:
    """Report normalized churn and every changed raw occurrence; never adjudicate."""
    lines = [
        f"trust-tier {'delta report' if report_only else 'ratchet'}: working tree vs HEAD {head_sha}",
        (
            f"  gating findings: head {result.head_count}, working {result.staged_count}, "
            f"added {result.added_count}, removed {result.removed_count}"
        ),
        f"  note findings (never strict-gate): head {result.head_notes}, working {result.staged_notes}",
        f"  raw occurrence changes: added {len(result.raw_added)}, removed {len(result.raw_removed)}",
    ]
    for entry in result.added:
        if entry.head_multiplicity:
            lines.append(f"  (key already present {entry.head_multiplicity}x in HEAD, {entry.staged_multiplicity}x working)")
    for marker, findings in (("-", result.raw_removed), ("+", result.raw_added)):
        for finding in findings:
            lines.extend((f"{marker} {finding.render()}", f"  raw: {finding.occurrence_json}"))
    if result.raw_added or result.added:
        lines.append("REVIEW REQUIRED: added occurrences are unadjudicated; Root must fix or individually review additions before landing.")
    if report_only:
        lines.append("MEASURED: comparison completed; exit zero is not finding clearance or permission to land.")
    elif result.added:
        lines.append(f"FAIL: strict normalized ratchet found {result.added_count} added gating occurrence(s).")
    else:
        lines.append("OK: strict normalized multiplicities did not grow; raw occurrence changes still require review.")
    lines.append(
        "Scope: trust_tier.tier_model only, not A4's --rules all comparison. Review removals for accidental suppression; signing is suspended."
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
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RatchetError(f"lint JSON finding field {name!r} is not an integer: {value!r}")
    return value


def _invalid_json_constant(value: str) -> None:
    raise RatchetError(f"lint JSON contains a non-JSON numeric constant: {value}")


def parse_findings(payload: str, *, tree_root: Path) -> list[FindingRecord]:
    """Parse the CLI's ``--format json`` document into owned records."""
    try:
        document = json.loads(payload, parse_constant=_invalid_json_constant)
    except json.JSONDecodeError as exc:
        raise RatchetError(f"lint output is not JSON: {exc}") from exc
    if not isinstance(document, list):
        raise RatchetError(f"lint JSON document is not a list: {type(document).__name__}")
    findings: list[FindingRecord] = []
    for record in document:
        if not isinstance(record, dict):
            raise RatchetError(f"lint JSON finding is not an object: {record!r}")
        _string_field(record, "fingerprint")
        severity = _string_field(record, "severity")
        if severity not in {"error", "warning", "note"}:
            raise RatchetError(f"lint JSON finding has an invalid severity: {severity!r}")
        if _string_field(record, "rule_id") in {"parse-error", "read-error"}:
            raise RatchetError(f"lint scan did not analyze an input: {record!r}")
        try:
            raw_json = json.dumps(
                {**record, "file_path": normalise_path(_string_field(record, "file_path"), tree_root=tree_root)},
                sort_keys=True,
                allow_nan=False,
            )
        except ValueError as exc:
            raise RatchetError("lint JSON occurrence contains a non-finite value") from exc
        findings.append(
            FindingRecord(
                path=normalise_path(_string_field(record, "file_path"), tree_root=tree_root),
                rule_id=_string_field(record, "rule_id"),
                message=_string_field(record, "message"),
                line=_int_field(record, "line"),
                column=_int_field(record, "column"),
                severity=severity,
                raw_json=raw_json,
            )
        )
    return findings


def lint_environment() -> dict[str, str]:
    """Pinned keyless mode; never forward an operator key into either scan."""
    if OPERATOR_KEY_ENV in os.environ:
        raise RatchetError("keyless trust-tier measurement refuses an operator-key environment")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(("src", LINTS_SOURCE_ROOT))
    env[VERIFY_MODE_ENV] = VERIFY_MODE
    return env


def verify_lints_import_path(tree_root: Path, *, python: str) -> Path:
    """Prove the rule code the run will import lives inside ``tree_root``."""
    probe = subprocess.run(
        [
            python,
            "-c",
            "import json, os, elspeth, elspeth_lints; "
            "print(json.dumps([os.path.realpath(elspeth.__file__), os.path.realpath(elspeth_lints.__file__)]))",
        ],
        cwd=tree_root,
        env=lint_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise RatchetError(f"could not import elspeth_lints from {tree_root}:\n{probe.stderr}")
    try:
        paths = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise RatchetError("import-root probe did not return JSON evidence") from exc
    if not isinstance(paths, list) or len(paths) != 2 or not all(isinstance(path, str) for path in paths):
        raise RatchetError("import-root probe did not return both module paths")
    imported_source, imported = map(Path, paths)
    if not imported_source.is_relative_to(Path(os.path.realpath(tree_root)) / "src"):
        raise RatchetError(f"elspeth imported from {imported_source}, expected source under {tree_root / 'src'}")
    expected = Path(os.path.realpath(tree_root)) / LINTS_SOURCE_ROOT
    if not imported.is_relative_to(expected):
        raise RatchetError(f"elspeth_lints imported from {imported}, expected a module under {expected}")
    return imported


def run_tier_model_check(tree_root: Path, *, python: str) -> tuple[list[FindingRecord], str]:
    """Run the rule exactly as the previous hook entry did; return findings and the run's stderr."""
    if not (tree_root / SCAN_ROOT).is_dir():
        raise RatchetError(f"lint scan root is missing: {tree_root / SCAN_ROOT}")
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
    findings = parse_findings(completed.stdout, tree_root=tree_root)
    if completed.returncode != int(any(finding.gates for finding in findings)):
        raise RatchetError("lint exit status disagrees with the measured JSON finding corpus")
    return findings, completed.stderr


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
    lint_environment()
    cache = repo_root / ".pytest_cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="trust-tier-ratchet-head-", dir=cache) as scratch:
        head_tree = Path(scratch) / "head"
        head_tree.mkdir()
        head_sha = export_head(repo_root, head_tree)
        verify_lints_import_path(head_tree, python=python)
        head_findings, head_stderr = run_tier_model_check(head_tree, python=python)
        sys.stderr.write(head_stderr)
    verify_lints_import_path(repo_root, python=python)
    staged_findings, staged_stderr = run_tier_model_check(repo_root, python=python)
    sys.stderr.write(staged_stderr)
    return compare_corpora(head_findings, staged_findings), head_sha


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure keyless HEAD/working-tree trust-tier deltas; default strict CLI rejects normalized additions."
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--report-only", action="store_true", help="Exit zero after successful measurement; additions still require Root A4 review."
    )
    modes.add_argument("--strict", action="store_true", help="Explicit default: reject added normalized gating multiplicities.")
    parser.add_argument("--repo-root", type=Path, default=None, help="Repository root (default: git rev-parse --show-toplevel)")
    parser.add_argument("--python", default=sys.executable, help="Interpreter for both lint runs (default: this one)")
    args = parser.parse_args(argv)
    try:
        lint_environment()  # Refuse a key-bearing environment before any child, including Git.
        repo_root = args.repo_root if args.repo_root is not None else _repo_root_from_cwd()
        result, head_sha = ratchet(repo_root, python=args.python)
    except RatchetError as exc:
        sys.stderr.write(f"trust-tier ratchet: {exc}\n")
        return 2
    sys.stdout.write(format_report(result, head_sha=head_sha, report_only=args.report_only))
    return 0 if args.report_only else result.exit_code


if __name__ == "__main__":
    sys.exit(main())
