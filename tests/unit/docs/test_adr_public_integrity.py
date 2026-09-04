"""Public-integrity checks for active architecture decision records."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[3]
ADR_DIRECTORY = REPOSITORY_ROOT / "docs/architecture/adr"

AUDITED_ADRS = frozenset(
    {
        "001-plugin-level-concurrency.md",
        "002-routing-copy-mode-limitation.md",
        "004-adr-explicit-sink-routing.md",
        "005-adr-declarative-dag-wiring.md",
        "006-layer-dependency-remediation.md",
        "007-pass-through-contract-propagation.md",
        "008-runtime-contract-cross-check.md",
        "009-pass-through-pathway-fusion.md",
        "010-declaration-trust-framework.md",
        "011-declared-output-fields-contract.md",
        "012-can-drop-rows-contract.md",
        "013-declared-required-fields-contract.md",
        "014-schema-config-mode-contract.md",
        "015-creates-tokens-contract.md",
        "019-two-axis-terminal-model.md",
        "021-sources-and-sinks-uniformly-boundary.md",
        "022-shareable-reviews.md",
        "023-custom-python-ci-analyzer.md",
        "024-delivery-governance-for-single-maintainer-mode.md",
        "025-multi-source-ingestion.md",
        "026-durable-token-scheduler.md",
        "029-journal-is-barrier-buffer-truth.md",
        "030-multi-worker-deployment-shape.md",
        "031-tutorial-is-a-fixed-script-canary.md",
        "032-validate-by-trust-domain.md",
        "033-deferred-intent-admission-contract.md",
        "036-textract-profile-bound-bucket.md",
    }
)

_PRIVATE_HOME = re.compile(r"/(?:home|Users)/(?!user(?:/|\b))[^/\s`),]+(?:/[^\s`),]*)?")
_TMP_PATH_PATTERN = r"/tmp(?:/[^\s`),]*)?"
_TMP_PATH = re.compile(_TMP_PATH_PATTERN)
_PROVENANCE_CONTEXT = re.compile(
    r"\b(?:accountable|authority|deciders?|decision makers?|evidence|provenance|review|plan|reference|source|"
    r"artefacts?|artifacts?|policy)\b",
    re.IGNORECASE,
)
_OPAQUE_MEMORY_REFERENCE = re.compile(
    r"MEMORY\.md::|\bproject memory\b|"
    r"\bmemory(?:\s+(?:authority|evidence|entry|key|policy|provenance|record|reference))?"
    r"\s*[:=]?\s*`?(?:project|feedback)_[a-z0-9_]+\b",
    re.IGNORECASE,
)
_QUOTED_REVISION = re.compile(r"`(?P<revision>[0-9a-f]{7,40})`", re.IGNORECASE)
_COMMIT_CITATION_GROUP = re.compile(
    r"\bcommits?\s*:?\s*"
    r"(?P<revisions>`[0-9a-f]{7,40}`"
    r"(?:\s*(?:,\s*|,?\s+and\s+)`[0-9a-f]{7,40}`)*)",
    re.IGNORECASE,
)
_REPOSITORY_FILE_REFERENCE = re.compile(r"`(?P<path>(?:src|tests)/[^`\n]+)`")


def _active_adrs() -> tuple[Path, ...]:
    return tuple(sorted(path for path in ADR_DIRECTORY.glob("[0-9][0-9][0-9]-*.md") if path.name != "000-template.md"))


def _tmp_path_is_rejected_as_provenance(paragraph: str, path: str) -> bool:
    quoted_path = rf"`?{re.escape(path)}`?"
    provenance_kind = r"(?:evidence|provenance|reference|source)"
    rejection_patterns = (
        rf"\b(?:do not|must not|never)\s+(?:use|cite|accept|treat)\s+{quoted_path}\s+as\s+{provenance_kind}\b",
        rf"\b(?:reject|forbid)\s+{quoted_path}\s+as\s+{provenance_kind}\b",
        rf"{quoted_path}\s+(?:is|are)\s+"
        rf"(?:rejected|forbidden|not\s+(?:valid|acceptable|durable|public))\s+as\s+{provenance_kind}\b",
    )
    return any(re.search(pattern, paragraph, re.IGNORECASE) for pattern in rejection_patterns)


def _public_provenance_violations(text: str) -> tuple[str, ...]:
    violations: list[str] = []

    for paragraph in re.split(r"\n\s*\n", text):
        if _PRIVATE_HOME.search(paragraph):
            violations.append("private absolute path")

        has_provenance_context = bool(_PROVENANCE_CONTEXT.search(paragraph))
        tmp_paths = tuple(match.group(0) for match in _TMP_PATH.finditer(paragraph))
        if has_provenance_context and any(not _tmp_path_is_rejected_as_provenance(paragraph, path) for path in tmp_paths):
            violations.append("ephemeral evidence path")

        if _OPAQUE_MEMORY_REFERENCE.search(paragraph):
            violations.append("opaque project-memory authority")

    return tuple(dict.fromkeys(violations))


def _public_commit_citations(text: str) -> tuple[str, ...]:
    citations: list[str] = []
    for group in _COMMIT_CITATION_GROUP.finditer(text):
        citations.extend(match.group("revision") for match in _QUOTED_REVISION.finditer(group.group("revisions")))
    return tuple(dict.fromkeys(citations))


def _reachable_git_commits() -> frozenset[str]:
    result = subprocess.run(
        ("git", "rev-list", "--all"),
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return frozenset(result.stdout.splitlines())


def _resolve_git_commit(revision: str) -> str | None:
    result = subprocess.run(
        ("git", "rev-parse", "--verify", f"{revision}^{{commit}}"),
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def test_active_adrs_do_not_use_private_or_ephemeral_provenance() -> None:
    failures: list[str] = []

    for path in _active_adrs():
        text = path.read_text(encoding="utf-8")
        failures.extend(f"{path.name}: {violation}" for violation in _public_provenance_violations(text))

    assert not failures, "Active ADRs contain non-public provenance:\n" + "\n".join(failures)


def test_audited_adr_commit_citations_resolve_to_reachable_history() -> None:
    reachable_commits = _reachable_git_commits()
    failures: list[str] = []

    for filename in sorted(AUDITED_ADRS):
        text = (ADR_DIRECTORY / filename).read_text(encoding="utf-8")
        for revision in _public_commit_citations(text):
            resolved = _resolve_git_commit(revision)
            if resolved is None:
                failures.append(f"{filename}: {revision} does not resolve as a commit")
            elif resolved not in reachable_commits:
                failures.append(f"{filename}: {revision} is not reachable from a Git ref")

    assert not failures, "Audited ADR commit citations must remain publicly reachable:\n" + "\n".join(failures)


def test_adr_031_live_control_references_exist() -> None:
    text = (ADR_DIRECTORY / "031-tutorial-is-a-fixed-script-canary.md").read_text(encoding="utf-8")
    references = tuple(match.group("path") for match in _REPOSITORY_FILE_REFERENCE.finditer(text))

    assert references, "ADR-031 must cite its live compensating controls"
    missing = tuple(path for path in references if not (REPOSITORY_ROOT / path).is_file())
    assert not missing, "ADR-031 cites missing live controls:\n" + "\n".join(missing)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Evidence: commit `deadbeef`.", ("deadbeef",)),
        ("Evidence: commits `deadbeef`, `cafebabe` and `0123456789`.", ("deadbeef", "cafebabe", "0123456789")),
        ("The content hash is `deadbeef`.", ()),
        ("Filigree issue `elspeth-deadbeef` records the decision.", ()),
        ("The word commit appears after unrelated hash `deadbeef`.", ()),
    ],
)
def test_public_commit_citation_boundary_cases(text: str, expected: tuple[str, ...]) -> None:
    assert _public_commit_citations(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("**Review evidence:**\n  `/tmp/review.json`", True),
        ("**Reference:** `/home/john`", True),
        ("**Reference:** `/Users/alice`", True),
        ("**Decision evidence:** `MEMORY.md::project_db_migration_policy`", True),
        ("**Decision evidence:** project memory `project_db_migration_policy`", True),
        ("**Decision evidence:** memory project_db_migration_policy", True),
        ("**Decision evidence:** memory key feedback_review_policy", True),
        ("Runtime uses `/tmp/cache` for spill files.", False),
        ("The `project_id` field identifies the project.", False),
        ("The `feedback_channel` field routes messages.", False),
        ("Review code validates the `project_name` field.", False),
        ("The source schema includes `feedback_text`.", False),
        ("Policy code reads the `project_slug` setting.", False),
        ("Evidence rows may include `feedback_score`.", False),
        ("**Decision evidence:** `project_db_migration_policy` is tracked in the repository.", False),
        ("**Evidence:** /tmp/review.json is the reference; do not delete it.", True),
        ("**Evidence:** /tmp/review.json is the reference; /tmp/old.json is rejected as evidence.", True),
        ("References to `/tmp/review.json` are rejected as evidence.", False),
    ],
)
def test_public_provenance_boundary_cases(text: str, expected: bool) -> None:
    assert bool(_public_provenance_violations(text)) is expected
