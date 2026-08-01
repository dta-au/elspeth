"""Repo-wide ratchet for permanent multi-rule per-file suppressions.

The existing corpus is transitional debt.  A baseline entry may grandfather
one HEAD entry only while the suppression's behaviour stays the same or gets
narrower.  New or broadened permanent multi-rule blankets fail the check;
deleting one, expiring it, reducing it to one rule, or tightening ``max_hits``
is monotonic cleanup and passes. A grandfathered blanket also fails whenever
the baseline-to-HEAD diff touches a source file it matches, forcing that file's
cleanup to replace broad coverage with precise dispositions.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from elspeth_lints.core.allowlist_io import AllowlistIOError, load_yaml_mapping_text, parse_per_file_rules


class PerFileBlanketRatchetError(RuntimeError):
    """The blanket ratchet could not measure HEAD against its baseline."""


@dataclass(frozen=True, slots=True)
class PerFileBlanket:
    """One permanent per-file suppression covering multiple rule IDs."""

    source_file: str
    index: int
    pattern: str
    rules: tuple[str, ...]
    max_hits: int | None


@dataclass(frozen=True, slots=True)
class PerFileBlanketViolation:
    """One HEAD blanket that was neither legacy-equivalent nor narrower."""

    source_file: str
    index: int
    pattern: str
    rules: tuple[str, ...]
    max_hits: int | None
    reason: str
    touched_file: str | None = None


@dataclass(frozen=True, slots=True)
class PerFileBlanketRatchetReport:
    """Result of the semantic baseline comparison."""

    head_blanket_count: int
    baseline_blanket_count: int
    grandfathered_count: int
    violations: tuple[PerFileBlanketViolation, ...]

    @property
    def passes(self) -> bool:
        return not self.violations


def check_per_file_blanket_ratchet(
    *,
    allowlist_root: Path,
    baseline_ref: str,
    repo_root: Path,
) -> PerFileBlanketRatchetReport:
    """Compare every ``config/cicd`` YAML blanket with ``baseline_ref``."""
    allowlist_root = Path(allowlist_root).resolve()
    repo_root = Path(repo_root).resolve()
    if not allowlist_root.is_dir():
        raise PerFileBlanketRatchetError(f"--allowlist-root {allowlist_root} is not a directory")
    if not repo_root.is_dir():
        raise PerFileBlanketRatchetError(f"--repo-root {repo_root} is not a directory")
    try:
        relative_root = allowlist_root.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise PerFileBlanketRatchetError(f"{allowlist_root} is not inside repo root {repo_root}") from exc

    head = _load_head_blankets(allowlist_root=allowlist_root, repo_root=repo_root)
    baseline = _load_baseline_blankets(
        baseline_ref=baseline_ref,
        relative_root=relative_root,
        repo_root=repo_root,
    )
    remaining = list(baseline)
    grandfathered = 0
    grandfathered_entries: list[PerFileBlanket] = []
    violations: list[PerFileBlanketViolation] = []
    for entry in head:
        match_index = next((index for index, old in enumerate(remaining) if _grandfathers(old, entry)), None)
        if match_index is not None:
            remaining.pop(match_index)
            grandfathered += 1
            grandfathered_entries.append(entry)
            continue
        same_target_exists = any(old.source_file == entry.source_file and old.pattern == entry.pattern for old in baseline)
        violations.append(
            PerFileBlanketViolation(
                source_file=entry.source_file,
                index=entry.index,
                pattern=entry.pattern,
                rules=entry.rules,
                max_hits=entry.max_hits,
                reason=("broadened permanent multi-rule blanket" if same_target_exists else "new permanent multi-rule blanket"),
            )
        )

    changed_paths = _load_changed_paths(baseline_ref=baseline_ref, repo_root=repo_root)
    for entry in grandfathered_entries:
        touched_file = next(
            (path for path in changed_paths if _repo_path_matches_pattern(path, entry.pattern)),
            None,
        )
        if touched_file is None:
            continue
        violations.append(
            PerFileBlanketViolation(
                source_file=entry.source_file,
                index=entry.index,
                pattern=entry.pattern,
                rules=entry.rules,
                max_hits=entry.max_hits,
                reason="touched source file remains under permanent multi-rule blanket",
                touched_file=touched_file,
            )
        )

    return PerFileBlanketRatchetReport(
        head_blanket_count=len(head),
        baseline_blanket_count=len(baseline),
        grandfathered_count=grandfathered,
        violations=tuple(violations),
    )


def _grandfathers(baseline: PerFileBlanket, head: PerFileBlanket) -> bool:
    return (
        baseline.source_file == head.source_file
        and baseline.pattern == head.pattern
        and set(head.rules).issubset(baseline.rules)
        and _cap_is_same_or_tighter(head.max_hits, baseline.max_hits)
    )


def _cap_is_same_or_tighter(head: int | None, baseline: int | None) -> bool:
    if baseline is None:
        return True
    return head is not None and head <= baseline


def _load_head_blankets(*, allowlist_root: Path, repo_root: Path) -> list[PerFileBlanket]:
    entries: list[PerFileBlanket] = []
    paths = sorted({*allowlist_root.rglob("*.yaml"), *allowlist_root.rglob("*.yml")})
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PerFileBlanketRatchetError(f"could not read {path}: {exc}") from exc
        source_file = path.relative_to(repo_root).as_posix()
        entries.extend(_parse_blankets(text, source_file=source_file))
    return entries


def _load_baseline_blankets(
    *,
    baseline_ref: str,
    relative_root: str,
    repo_root: Path,
) -> list[PerFileBlanket]:
    listed = _run_git(
        ["ls-tree", "-r", "--name-only", baseline_ref, "--", relative_root],
        repo_root=repo_root,
    )
    if listed.returncode != 0:
        raise PerFileBlanketRatchetError(f"git ls-tree could not inspect baseline-ref {baseline_ref!r}: {_git_failure_detail(listed)}")
    entries: list[PerFileBlanket] = []
    for source_file in sorted(listed.stdout.splitlines()):
        if not source_file.endswith((".yaml", ".yml")):
            continue
        shown = _run_git(["show", f"{baseline_ref}:{source_file}"], repo_root=repo_root)
        if shown.returncode != 0:
            raise PerFileBlanketRatchetError(f"git show failed for baseline {baseline_ref}:{source_file}: {_git_failure_detail(shown)}")
        entries.extend(_parse_blankets(shown.stdout, source_file=source_file, baseline_ref=baseline_ref))
    return entries


def _load_changed_paths(*, baseline_ref: str, repo_root: Path) -> tuple[str, ...]:
    changed = _run_git(
        ["diff", "--name-only", "--diff-filter=ACMR", "-z", baseline_ref, "--"],
        repo_root=repo_root,
    )
    if changed.returncode != 0:
        raise PerFileBlanketRatchetError(
            f"git diff could not inspect changed paths from baseline-ref {baseline_ref!r}: {_git_failure_detail(changed)}"
        )
    return tuple(path for path in changed.stdout.split("\0") if path)


def _repo_path_matches_pattern(repo_path: str, pattern: str) -> bool:
    candidates = {repo_path}
    production_prefix = "src/elspeth/"
    if repo_path.startswith(production_prefix):
        candidates.add(repo_path.removeprefix(production_prefix))
    return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates)


def _parse_blankets(
    text: str,
    *,
    source_file: str,
    baseline_ref: str | None = None,
) -> list[PerFileBlanket]:
    label = source_file if baseline_ref is None else f"baseline {baseline_ref}:{source_file}"
    try:
        data = load_yaml_mapping_text(text, source_label=label)
        entries: list[PerFileBlanket] = []
        for record in parse_per_file_rules(data, source_file=label):
            if record.expires is None and len(record.rules) > 1:
                entries.append(
                    PerFileBlanket(
                        source_file=source_file,
                        index=record.index,
                        pattern=record.pattern,
                        rules=tuple(sorted(record.rules)),
                        max_hits=record.max_hits,
                    )
                )
        return entries
    except AllowlistIOError as exc:
        raise PerFileBlanketRatchetError(str(exc)) from exc


def _run_git(args: list[str], *, repo_root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
    except OSError as exc:
        raise PerFileBlanketRatchetError(f"git command failed to start: {exc}") from exc


def _git_failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or f"git exited with status {result.returncode}").strip()
