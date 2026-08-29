"""Public-integrity checks for active architecture decision records."""

from __future__ import annotations

import re
from pathlib import Path

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

_AUTHORITY_FIELD = re.compile(r"^\*\*(?:Deciders|Decision Makers):\*\*\s*(?P<value>.+)$", re.MULTILINE)
_NON_AUTHORITY_ACTOR = re.compile(
    r"\b(?:architecture (?:review )?board|architecture team|core maintainers|code reviewers|"
    r"claude(?: opus| fable \d+)?|codex|SME agent|reviewer panel|review panel|panel-reviewed)\b",
    re.IGNORECASE,
)
_PRIVATE_HOME = re.compile(r"/(?:home|Users)/(?!user(?:/|\b))[^/\s`]+/")
_EPHEMERAL_EVIDENCE = re.compile(
    r"^(?=.*\b(?:evidence|provenance|review|plan|artefacts?|artifacts?)\b).*?/tmp/",
    re.IGNORECASE | re.MULTILINE,
)
_OPAQUE_MEMORY_AUTHORITY = re.compile(
    r"MEMORY\.md::|project memory|(?:project|feedback)_[a-z0-9_]+",
    re.IGNORECASE,
)


def _active_adrs() -> tuple[Path, ...]:
    return tuple(sorted(path for path in ADR_DIRECTORY.glob("[0-9][0-9][0-9]-*.md") if path.name != "000-template.md"))


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
        assert "**Status:** Accepted" in text, filename


def test_active_adr_authority_metadata_does_not_delegate_to_tools_or_panels() -> None:
    failures: list[str] = []

    for path in _active_adrs():
        text = path.read_text(encoding="utf-8")
        for match in _AUTHORITY_FIELD.finditer(text):
            authority = match.group("value").strip()
            if _NON_AUTHORITY_ACTOR.search(authority):
                failures.append(f"{path.name}: {authority}")

    assert not failures, "ADR authority metadata names a tool, agent, panel, or fictional body:\n" + "\n".join(failures)


def test_active_adrs_do_not_use_private_or_ephemeral_provenance() -> None:
    failures: list[str] = []

    for path in _active_adrs():
        text = path.read_text(encoding="utf-8")
        if _PRIVATE_HOME.search(text):
            failures.append(f"{path.name}: private absolute path")
        if _EPHEMERAL_EVIDENCE.search(text):
            failures.append(f"{path.name}: ephemeral evidence path")
        if _OPAQUE_MEMORY_AUTHORITY.search(text):
            failures.append(f"{path.name}: opaque project-memory authority")

    assert not failures, "Active ADRs contain non-public provenance:\n" + "\n".join(failures)
