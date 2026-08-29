"""Public-integrity checks for active architecture decision records."""

from __future__ import annotations

import re
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

_AUTHORITY_FIELD = re.compile(
    r"^\*\*(?:Amendment )?(?:Deciders|Decision Makers):\*\*\s*(?P<value>.+)$",
    re.MULTILINE,
)
_STATUS_FIELD = re.compile(r"^\*\*Status:\*\*\s*(?P<value>.+)$", re.MULTILINE)
_ACCOUNTABLE_AUTHORITIES = frozenset({"ELSPETH maintainer", "ELSPETH maintainers"})
_PRIVATE_HOME = re.compile(r"/(?:home|Users)/(?!user(?:/|\b))[^/\s`),]+(?:/[^\s`),]*)?")
_TMP_PATH = re.compile(r"/tmp(?:/[^\s`),]*)?")
_PROVENANCE_CONTEXT = re.compile(
    r"\b(?:accountable|authority|deciders?|decision makers?|evidence|provenance|review|plan|reference|source|"
    r"artefacts?|artifacts?|policy)\b",
    re.IGNORECASE,
)
_PROVENANCE_REJECTION = re.compile(
    r"\b(?:do not|must not|never|reject(?:ed)?|forbid(?:den)?|not (?:a )?(?:durable|valid|acceptable|public))\b",
    re.IGNORECASE,
)
_OPAQUE_MEMORY_REFERENCE = re.compile(
    r"MEMORY\.md::|\bproject memory\b|"
    r"\bmemory(?:\s+(?:authority|evidence|entry|key|policy|provenance|record|reference))?"
    r"\s*[:=]?\s*`?(?:project|feedback)_[a-z0-9_]+\b",
    re.IGNORECASE,
)


def _active_adrs() -> tuple[Path, ...]:
    return tuple(sorted(path for path in ADR_DIRECTORY.glob("[0-9][0-9][0-9]-*.md") if path.name != "000-template.md"))


def _top_level_metadata(text: str, pattern: re.Pattern[str]) -> str | None:
    preamble = text.split("\n## ", maxsplit=1)[0]
    match = pattern.search(preamble)
    return match.group("value").strip() if match else None


def _public_provenance_violations(text: str) -> tuple[str, ...]:
    violations: list[str] = []

    for paragraph in re.split(r"\n\s*\n", text):
        if _PRIVATE_HOME.search(paragraph):
            violations.append("private absolute path")

        rejects_provenance = bool(_PROVENANCE_REJECTION.search(paragraph))
        has_provenance_context = bool(_PROVENANCE_CONTEXT.search(paragraph))
        if _TMP_PATH.search(paragraph) and has_provenance_context and not rejects_provenance:
            violations.append("ephemeral evidence path")

        if _OPAQUE_MEMORY_REFERENCE.search(paragraph):
            violations.append("opaque project-memory authority")

    return tuple(dict.fromkeys(violations))


def test_audited_adrs_name_the_maintainer_as_accountable_authority() -> None:
    failures: list[str] = []

    for filename in sorted(AUDITED_ADRS):
        text = (ADR_DIRECTORY / filename).read_text(encoding="utf-8")
        authorities = tuple(match.group("value").strip() for match in _AUTHORITY_FIELD.finditer(text))
        if not authorities or any(authority != "ELSPETH maintainer" for authority in authorities):
            failures.append(f"{filename}: {authorities or ('missing',)}")

    assert not failures, "Audited ADRs must name only the accountable authority:\n" + "\n".join(failures)


def test_accepted_index_state_is_reflected_in_adr_status() -> None:
    for filename in (
        "004-adr-explicit-sink-routing.md",
        "005-adr-declarative-dag-wiring.md",
    ):
        text = (ADR_DIRECTORY / filename).read_text(encoding="utf-8")
        assert _top_level_metadata(text, _STATUS_FIELD) == "Accepted", filename


@pytest.mark.parametrize(
    "field",
    ("Deciders", "Decision Makers", "Amendment Deciders", "Amendment Decision Makers"),
)
def test_authority_field_variants_use_the_exact_positive_allowlist(field: str) -> None:
    match = _AUTHORITY_FIELD.fullmatch(f"**{field}:** ELSPETH maintainers")

    assert match is not None
    assert match.group("value") in _ACCOUNTABLE_AUTHORITIES


def test_top_level_status_parser_does_not_accept_an_amendment_status() -> None:
    text = "# ADR\n\n**Status:** Proposed\n\n## Amendment\n\n**Status:** Accepted\n"

    assert _top_level_metadata(text, _STATUS_FIELD) == "Proposed"


def test_active_adr_authority_metadata_names_an_accountable_maintainer() -> None:
    failures: list[str] = []

    for path in _active_adrs():
        text = path.read_text(encoding="utf-8")
        authorities = tuple(match.group("value").strip() for match in _AUTHORITY_FIELD.finditer(text))
        for authority in authorities:
            if authority not in _ACCOUNTABLE_AUTHORITIES:
                failures.append(f"{path.name}: {authority}")

    assert not failures, "ADR authority metadata must name an accountable ELSPETH maintainer:\n" + "\n".join(failures)


def test_active_adrs_do_not_use_private_or_ephemeral_provenance() -> None:
    failures: list[str] = []

    for path in _active_adrs():
        text = path.read_text(encoding="utf-8")
        failures.extend(f"{path.name}: {violation}" for violation in _public_provenance_violations(text))

    assert not failures, "Active ADRs contain non-public provenance:\n" + "\n".join(failures)


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
        ("References to `/tmp/review.json` are rejected as evidence.", False),
    ],
)
def test_public_provenance_boundary_cases(text: str, expected: bool) -> None:
    assert bool(_public_provenance_violations(text)) is expected
