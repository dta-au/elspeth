"""Red-team trigger: classify commits against security seams, select attack
angles, spawn red-team agents in parallel, and route their findings.

Deterministic core (unit-tested in tests/unit/scripts/test_red_team_trigger.py):

- ``classify_paths``     — which security-seam categories a commit touches
- ``select_attack_angles`` — 2-3 adversarial angles for those categories
- ``parse_findings``     — fail-closed extraction of agent JSON findings
- ``route_finding``      — auto-file vs review-log severity routing
- ``file_issue_argv`` / ``build_agent_argv`` — argv construction (no shell)

Orchestration (thin, subprocess-based): ``main`` with ``classify`` and ``run``
subcommands. Exit codes are deliberately distinct — 0 = seam matched /
run completed, 3 = no seam matched — so callers never conflate "nothing to
do" with "failed" (the exact conflation bug the red-team agent hunts).

Findings routing: severity in {critical, high} AND confidence == confirmed
auto-files a Filigree bug; everything else — including any finding with an
unrecognised severity or confidence — appends to the review log. Unknown
vocabulary fails closed to the *quiet* side because the pipeline's contract
is zero-noise auto-filing.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SEAM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("auth", re.compile(r"^src/elspeth/web/auth/")),
    (
        "secrets",
        re.compile(r"^src/elspeth/core/secrets\.py$|^src/elspeth/web/secrets/"),
    ),
    (
        "security",
        re.compile(r"^src/elspeth/core/security/|^src/elspeth/core/url_validation\.py$"),
    ),
    (
        "policy_gate",
        re.compile(
            r"^src/elspeth/web/plugin_policy/"
            r"|^src/elspeth/web/composer/no_tool_policy\.py$"
        ),
    ),
    (
        "state_machine",
        re.compile(
            r"^src/elspeth/engine/orchestrator/"
            r"|^src/elspeth/web/sessions/"
            r"|^src/elspeth/core/checkpoint/"
        ),
    ),
    ("cicd_gate", re.compile(r"^config/cicd/|^scripts/cicd/")),
)

_ANGLES: dict[str, AttackAngle] = {}


@dataclass(frozen=True)
class Finding:
    title: str
    severity: str
    confidence: str
    angle: str
    commit: str
    files: tuple[str, ...]
    repro: str
    detail: str


@dataclass(frozen=True)
class AttackAngle:
    name: str
    charter: str


def _register(name: str, charter: str) -> AttackAngle:
    angle = AttackAngle(name=name, charter=charter)
    _ANGLES[name] = angle
    return angle


WRONG_REASON_TESTS = _register(
    "wrong-reason-tests",
    "Prove the new/changed tests pass for the wrong reason: assert on mocks "
    "instead of behavior, assert a value the buggy code also produces, never "
    "reach the guarded branch, or tolerate the defect via a broad except / "
    "default. Then mutation-test the changed production lines by hand: for "
    "each guard, describe the mutant (invert the condition, drop the raise, "
    "swap the boundary) and name the test that kills it — or report the "
    "survivor.",
)
REVERTED_GUARD = _register(
    "reverted-guard",
    "Prove the fix is no longer present at HEAD even though its tests are: "
    "trace each guard added by the commit through later merges and file-level "
    "restores (git log --follow / git blame the guarded lines). A test that "
    "survives while its guard is gone is the finding. Also check the guard "
    "was not weakened: same lines present but condition loosened, or moved "
    "somewhere the tested entry point no longer reaches.",
)
ESCAPE_ARTIST = _register(
    "escape-artist",
    "Prove the boundary can be escaped: path traversal, symlinks, case or "
    "Unicode normalization mismatches, TOCTOU between check and use, prefix "
    "matching where exact matching was intended, and gates that fail OPEN on "
    "parse errors, empty configs, missing files, or unexpected exceptions. "
    "Attack the error paths first — the happy path is the decoy.",
)
STATE_CONFLATION = _register(
    "state-conflation",
    "Prove states or exit codes are conflated: success/no-op/error collapsed "
    "into one code, a state machine transition that silently absorbs an "
    "illegal edge, retries that mask a fatal state, idempotency violated "
    "under replay, or a lease/claim released on the error path it should "
    "hold through. Enumerate the transition table and probe every edge the "
    "tests do not.",
)

_BASE_ANGLES: tuple[AttackAngle, ...] = (WRONG_REASON_TESTS, REVERTED_GUARD)
_ESCAPE_CATEGORIES = frozenset({"auth", "secrets", "security", "policy_gate", "cicd_gate"})
_STATE_CATEGORIES = frozenset({"state_machine"})
_MAX_ANGLES = 3

_AUTO_FILE_SEVERITIES = frozenset({"critical", "high"})
_KNOWN_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
_KNOWN_CONFIDENCES = frozenset({"confirmed", "probable", "speculative"})
_PRIORITY_BY_SEVERITY = {"critical": "0", "high": "1"}

_REQUIRED_FINDING_FIELDS = ("title", "severity", "confidence", "files", "repro", "detail")

_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

_AGENT_ALLOWED_TOOLS = (
    "Read",
    "Grep",
    "Glob",
    "Bash(git:*)",
    "Bash(rg:*)",
    "Bash(.venv/bin/python:*)",
)


def classify_paths(paths: list[str]) -> set[str]:
    """Return the seam categories touched by ``paths`` (empty set = no seam)."""
    categories: set[str] = set()
    for path in paths:
        for category, pattern in SEAM_PATTERNS:
            if pattern.search(path):
                categories.add(category)
    return categories


def select_attack_angles(categories: set[str]) -> tuple[AttackAngle, ...]:
    """Pick 2-3 angles for the matched seams; empty input selects nothing.

    The two base angles always run; specialists join by category, capped at
    ``_MAX_ANGLES`` in a fixed order so a given commit always gets the same
    fleet.
    """
    if not categories:
        return ()
    selected = list(_BASE_ANGLES)
    if categories & _ESCAPE_CATEGORIES:
        selected.append(ESCAPE_ARTIST)
    if categories & _STATE_CATEGORIES:
        selected.append(STATE_CONFLATION)
    return tuple(selected[:_MAX_ANGLES])


def parse_findings(text: str, angle: str, commit: str) -> tuple[list[Finding], list[str]]:
    """Extract findings from an agent transcript, fail-closed.

    Returns ``(findings, errors)``. Anything that does not parse into a
    fully-populated finding lands in ``errors`` and never in ``findings``.
    """
    blocks = _JSON_BLOCK_RE.findall(text)
    if not blocks:
        return [], [f"[{angle}@{commit}] no fenced JSON findings block in output"]

    findings: list[Finding] = []
    errors: list[str] = []
    for block in blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError as exc:
            errors.append(f"[{angle}@{commit}] malformed JSON block: {exc}")
            continue
        raw_findings = payload.get("findings") if isinstance(payload, dict) else None
        if not isinstance(raw_findings, list):
            errors.append(f"[{angle}@{commit}] JSON block has no 'findings' list")
            continue
        for index, raw in enumerate(raw_findings):
            if not isinstance(raw, dict):
                errors.append(f"[{angle}@{commit}] finding #{index} is not an object")
                continue
            missing = [f for f in _REQUIRED_FINDING_FIELDS if f not in raw]
            if missing:
                errors.append(f"[{angle}@{commit}] finding #{index} missing {missing}")
                continue
            files_raw = raw["files"]
            if not isinstance(files_raw, list) or not all(isinstance(f, str) for f in files_raw):
                errors.append(f"[{angle}@{commit}] finding #{index} 'files' is not a list of strings")
                continue
            findings.append(
                Finding(
                    title=str(raw["title"]),
                    severity=str(raw["severity"]),
                    confidence=str(raw["confidence"]),
                    angle=angle,
                    commit=commit,
                    files=tuple(files_raw),
                    repro=str(raw["repro"]),
                    detail=str(raw["detail"]),
                )
            )
    return findings, errors


def route_finding(finding: Finding) -> str:
    """Return ``"file"`` to auto-file a tracker issue, ``"log"`` otherwise.

    Unknown severity or confidence vocabulary routes to the log: the
    auto-file path only fires on values it positively recognises.
    """
    if finding.severity not in _KNOWN_SEVERITIES:
        return "log"
    if finding.confidence not in _KNOWN_CONFIDENCES:
        return "log"
    if finding.severity in _AUTO_FILE_SEVERITIES and finding.confidence == "confirmed":
        return "file"
    return "log"


def file_issue_argv(finding: Finding) -> list[str]:
    """Build the ``filigree create`` argv for an auto-filed finding."""
    description = (
        f"Red-team finding (angle: {finding.angle}, commit: {finding.commit}, "
        f"confidence: {finding.confidence}).\n\n"
        f"{finding.detail}\n\n"
        f"Files: {', '.join(finding.files)}\n\n"
        f"Reproduction:\n{finding.repro}\n"
    )
    return [
        "filigree",
        "create",
        finding.title,
        "--type",
        "bug",
        "-p",
        _PRIORITY_BY_SEVERITY[finding.severity],
        "-d",
        description,
        "--label",
        "red-team",
        "--label",
        f"red-team-angle:{finding.angle}",
        "--actor",
        "red-team",
        "--json",
    ]


def build_agent_prompt(commit: str, angle: AttackAngle) -> str:
    return (
        f"You are running the '{angle.name}' attack angle of the adversarial "
        f"review pipeline against commit {commit}.\n\n"
        f"Angle charter: {angle.charter}\n\n"
        f"Start from `git show {commit}` and the files it touches. Your job "
        "is to DISPROVE that the change works as claimed — assume it is "
        "broken and hunt for the break. Follow your agent definition's "
        "output contract: finish with exactly one fenced ```json block "
        'containing {"findings": [...]}, each finding with title, severity '
        "(critical|high|medium|low), confidence "
        "(confirmed|probable|speculative), files, repro, detail. Report "
        "only findings you have evidence for; an empty findings list is a "
        "valid and respected result."
    )


def build_agent_argv(angle: AttackAngle, prompt: str) -> list[str]:
    return [
        "claude",
        "--agent",
        "red-team",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--allowedTools",
        *_AGENT_ALLOWED_TOOLS,
    ]


# --------------------------------------------------------------------------
# Orchestration (thin subprocess shell around the tested core).
# --------------------------------------------------------------------------

_EXIT_SEAM_MATCHED = 0
_EXIT_NO_SEAM = 3


def _git_output(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def changed_paths(commit: str, repo_root: Path) -> list[str]:
    """Paths changed by ``commit`` (merge commits: vs first parent)."""
    output = _git_output(repo_root, "show", "-m", "--first-parent", "--format=", "--name-only", commit)
    return [line for line in output.splitlines() if line.strip()]


def _review_log_path(repo_root: Path) -> Path:
    return repo_root / ".claude" / "red-team" / "review-log.md"


def append_review_log(repo_root: Path, findings: list[Finding], errors: list[str]) -> Path:
    log_path = _review_log_path(repo_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
    lines = [f"\n## Run {stamp}\n"]
    for finding in findings:
        lines.append(
            f"- **[{finding.severity}/{finding.confidence}]** {finding.title} "
            f"(angle: {finding.angle}, commit: {finding.commit})\n"
            f"  - files: {', '.join(finding.files)}\n"
            f"  - repro: {finding.repro}\n"
            f"  - {finding.detail}\n"
        )
    for error in errors:
        lines.append(f"- parse error: {error}\n")
    if not findings and not errors:
        lines.append("- no findings\n")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.writelines(lines)
    return log_path


def run_red_team(commit: str, repo_root: Path, dry_run: bool) -> int:
    sha = _git_output(repo_root, "rev-parse", commit).strip()
    categories = classify_paths(changed_paths(sha, repo_root))
    if not categories:
        print(f"{sha[:12]}: no security seam touched; nothing to do")
        return _EXIT_NO_SEAM
    angles = select_attack_angles(categories)
    print(f"{sha[:12]}: seams {sorted(categories)} -> angles {[angle.name for angle in angles]}")
    commands = [(angle, build_agent_argv(angle, build_agent_prompt(sha, angle))) for angle in angles]
    if dry_run:
        for angle, argv in commands:
            print(f"[dry-run] {angle.name}: {argv[:4]} ... ({len(argv)} args)")
        return _EXIT_SEAM_MATCHED

    runs_dir = repo_root / ".claude" / "red-team" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%SZ")

    processes = [
        (
            angle,
            subprocess.Popen(
                argv,
                cwd=repo_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ),
        )
        for angle, argv in commands
    ]

    all_findings: list[Finding] = []
    all_errors: list[str] = []
    for angle, process in processes:
        stdout, stderr = process.communicate()
        raw_path = runs_dir / f"{stamp}-{sha[:12]}-{angle.name}.json"
        raw_path.write_text(stdout or "", encoding="utf-8")
        if process.returncode != 0:
            all_errors.append(f"[{angle.name}@{sha[:12]}] agent exited {process.returncode}: {(stderr or '')[-500:]}")
            continue
        result_text = stdout or ""
        try:
            envelope = json.loads(result_text)
            if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
                result_text = envelope["result"]
        except json.JSONDecodeError:
            pass
        findings, errors = parse_findings(result_text, angle=angle.name, commit=sha)
        all_findings.extend(findings)
        all_errors.extend(errors)

    filed: list[str] = []
    logged: list[Finding] = []
    for finding in all_findings:
        if route_finding(finding) == "file":
            result = subprocess.run(
                file_issue_argv(finding),
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                filed.append(finding.title)
            else:
                all_errors.append(f"filigree create failed for '{finding.title}': {result.stderr[-300:]}")
                logged.append(finding)
        else:
            logged.append(finding)

    log_path = append_review_log(repo_root, logged, all_errors)
    print(f"{sha[:12]}: {len(filed)} issue(s) filed, {len(logged)} finding(s) logged to {log_path}, {len(all_errors)} error(s)")
    for title in filed:
        print(f"  filed: {title}")
    return _EXIT_SEAM_MATCHED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="red-team-trigger", description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify_parser = subparsers.add_parser("classify", help="exit 0 if the commit touches a security seam, 3 if not")
    classify_parser.add_argument("--commit", default="HEAD")

    run_parser = subparsers.add_parser("run", help="classify, then spawn red-team agents and route findings")
    run_parser.add_argument("--commit", default="HEAD")
    run_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "classify":
        sha = _git_output(args.repo_root, "rev-parse", args.commit).strip()
        categories = classify_paths(changed_paths(sha, args.repo_root))
        for category in sorted(categories):
            print(category)
        return _EXIT_SEAM_MATCHED if categories else _EXIT_NO_SEAM
    return run_red_team(args.commit, args.repo_root, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
