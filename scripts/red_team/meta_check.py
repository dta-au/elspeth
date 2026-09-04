"""Reverted-guard meta-check: find commits whose production additions
vanished from HEAD while their test additions survived.

The target failure mode (it has happened on this repo — see
docs/agents/recent-code-hints.md on file-level restores): a commit lands a
guard plus its test; a later restore or merge reverts the guard file while
the test file survives. The test either fails only in selections nobody
runs, or passes for the wrong reason. Orphaned tests are the tell.

Detection: for each recent non-merge commit, take the *added* lines of its
diff, split production vs test paths, and measure how many of those lines
still exist (whitespace-normalized) in the same file at HEAD. A commit is
flagged only when ALL of:

- it added >= 3 substantive production lines,
- <= 20% of them survive at HEAD,
- it also added test lines, and >= 80% of those survive at HEAD,
- the production files still exist at HEAD (a deleted or renamed file is
  indistinguishable from a revert by line-matching, so it is skipped —
  precision over recall).

Exit codes: 0 = clean, 4 = findings (distinct from generic failure).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MIN_PROD_LINES = 3
PROD_SURVIVAL_MAX = 0.2
TEST_SURVIVAL_MIN = 0.8

_EXIT_CLEAN = 0
_EXIT_FINDINGS = 4

_TRIVIAL_WORDS = frozenset({"pass", "else:", "try:", "finally:", "break", "continue"})
_PUNCTUATION_ONLY = frozenset("()[]{},:\"'` ")


@dataclass(frozen=True)
class SurvivalStats:
    survived: int
    total: int

    @property
    def ratio(self) -> float:
        return self.survived / self.total if self.total else 1.0


@dataclass(frozen=True)
class RevertedFixFinding:
    commit: str
    subject: str
    dead_prod_files: tuple[str, ...]
    surviving_test_files: tuple[str, ...]
    prod: SurvivalStats
    tests: SurvivalStats


def normalize(line: str) -> str:
    """Collapse all whitespace so indentation changes do not hide a match."""
    return " ".join(line.split())


def is_substantive(line: str) -> bool:
    """True for lines that carry logic; blank, comment, and bare-punctuation
    lines are excluded from survival accounting."""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("#"):
        return False
    if stripped in _TRIVIAL_WORDS:
        return False
    return not all(ch in _PUNCTUATION_ONLY for ch in stripped)


def parse_added_lines(diff_text: str) -> dict[str, list[str]]:
    """Map each changed path to the lines its diff added.

    Understands unified diff output of ``git show --format= <sha>``; only
    ``+`` lines under a ``+++ b/<path>`` header are collected.
    """
    added: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            if target.startswith("b/"):
                current = target[2:]
            else:
                current = None  # /dev/null: file deleted in this commit
            continue
        if line.startswith("--- ") or line.startswith("diff --git "):
            if line.startswith("diff --git "):
                current = None
            continue
        if current is not None and line.startswith("+"):
            added.setdefault(current, []).append(line[1:])
    return added


def survival(added: list[str], current_content: str) -> tuple[int, int]:
    """Count how many substantive added lines still appear in the file."""
    current_lines = {normalize(line) for line in current_content.splitlines()}
    substantive = [line for line in added if is_substantive(line)]
    survived = sum(1 for line in substantive if normalize(line) in current_lines)
    return survived, len(substantive)


def assess(prod: SurvivalStats, tests: SurvivalStats) -> bool:
    """True when the commit's guard is dead but its tests live on."""
    if prod.total < MIN_PROD_LINES:
        return False
    if tests.total < 1:
        return False
    return prod.ratio <= PROD_SURVIVAL_MAX and tests.ratio >= TEST_SURVIVAL_MIN


def is_test_path(path: str) -> bool:
    return path.startswith("tests/")


def is_prod_path(path: str) -> bool:
    if not path.endswith(".py"):
        return False
    return path.startswith("src/") or path.startswith("scripts/") or path.startswith("elspeth-lints/src/")


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _head_content(repo_root: Path, path: str) -> str | None:
    """Content of ``path`` at HEAD, or None if it does not exist there."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"HEAD:{path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def check_commit(sha: str, repo_root: Path) -> RevertedFixFinding | None:
    diff = _git(repo_root, "show", "-m", "--first-parent", "--format=", "--unified=0", sha)
    added = parse_added_lines(diff)

    prod_survived = prod_total = 0
    test_survived = test_total = 0
    dead_prod_files: list[str] = []
    surviving_test_files: list[str] = []

    for path, lines in added.items():
        if is_prod_path(path):
            content = _head_content(repo_root, path)
            if content is None:
                continue  # deleted/renamed at HEAD: skip for precision
            survived, total = survival(lines, content)
            prod_survived += survived
            prod_total += total
            if total and survived == 0:
                dead_prod_files.append(path)
        elif is_test_path(path):
            content = _head_content(repo_root, path)
            if content is None:
                continue
            survived, total = survival(lines, content)
            test_survived += survived
            test_total += total
            if total and survived:
                surviving_test_files.append(path)

    prod = SurvivalStats(survived=prod_survived, total=prod_total)
    tests = SurvivalStats(survived=test_survived, total=test_total)
    if not assess(prod=prod, tests=tests):
        return None
    subject = _git(repo_root, "show", "-s", "--format=%s", sha).strip()
    return RevertedFixFinding(
        commit=sha,
        subject=subject,
        dead_prod_files=tuple(sorted(dead_prod_files)),
        surviving_test_files=tuple(sorted(surviving_test_files)),
        prod=prod,
        tests=tests,
    )


def scan(last: int, repo_root: Path) -> list[RevertedFixFinding]:
    """Check the last ``last`` non-merge commits, newest first."""
    output = _git(repo_root, "rev-list", "--no-merges", "-n", str(last), "HEAD")
    findings: list[RevertedFixFinding] = []
    for sha in output.split():
        finding = check_commit(sha, repo_root)
        if finding is not None:
            findings.append(finding)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="red-team-meta-check", description=__doc__)
    parser.add_argument("--last", type=int, default=50)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    findings = scan(last=args.last, repo_root=args.repo_root)
    if not findings:
        print(f"meta-check: no reverted-guard findings in the last {args.last} non-merge commits")
        return _EXIT_CLEAN
    for finding in findings:
        print(f"REVERTED-GUARD {finding.commit[:12]} {finding.subject}")
        print(f"  prod additions surviving at HEAD: {finding.prod.survived}/{finding.prod.total}")
        print(f"  test additions surviving at HEAD: {finding.tests.survived}/{finding.tests.total}")
        for path in finding.dead_prod_files:
            print(f"  dead guard file: {path}")
        for path in finding.surviving_test_files:
            print(f"  surviving test: {path}")
        print(f"  repro: git show {finding.commit[:12]} -- {' '.join(finding.dead_prod_files)}  # then grep the added guard lines at HEAD")
    return _EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
