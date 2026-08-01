"""Trust-tier regressions for the execution-validation refactor surface."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from elspeth_lints.rules.trust_tier.tier_model.rule import scan_file_with_observations

# Discovered from the package directory rather than hand-listed so a future
# `_validation_*.py` sibling cannot be extracted with zero trust-tier pin
# coverage and no test failure. The count floor guards the glob itself.
_KNOWN_PRODUCTION_FILE_COUNT = 8


def _production_files(execution_root: Path) -> list[str]:
    discovered = sorted(path.name for path in execution_root.glob("_validation_*.py"))
    discovered.append("validation.py")
    assert len(discovered) >= _KNOWN_PRODUCTION_FILE_COUNT, "validation package glob found fewer modules than the split created"
    return discovered


# These R5s are policy-wrong, adjudication-ready nominal checks over owned
# concrete classes: one admits the real scoped-resolver implementation instead
# of a runtime Protocol impostor; the other two discriminate owned closed
# result/exception unions. The `_reframe_settings_missing_parts` R1/R5 trio
# ran ACTIVE at base under three judge-signed allowlist entries whose
# rationales explicitly ruled the decorator mechanism inapplicable; the move
# to `_validation_diagnostics.py` staled those path-keyed signatures, so the
# findings run active here as re-adjudication candidates for the release-end
# signing ceremony. No filename/name exemption may hide any check.
_ADJUDICATION_CANDIDATES = {
    "_validation_authoring.py": ["R5:_secret_ref_exists", "R5:review_interpretations"],
    "_validation_diagnostics.py": [
        "R1:_reframe_settings_missing_parts",
        "R1:_reframe_settings_missing_parts",
        "R5:_reframe_settings_missing_parts",
        "R5:_infer_component_type_from_plugin_error",
    ],
}

_EXPECTED_SUPPRESSION_OBSERVATIONS = {
    "_validation_authoring.py": Counter(
        {
            "R1:lower_plugin_policy": 1,
            "R5:lower_plugin_policy": 1,
            "R1:validate_path_policy": 2,
            "R5:validate_path_policy": 1,
            "R5:validate_web_network_policy": 3,
            "R5:validate_web_resource_policy": 1,
            "R5:_collect_secret_refs": 2,
        }
    ),
    "_validation_diagnostics.py": Counter(
        {
            "R1:_find_identity_node_advisories": 3,
            "R5:_find_identity_node_advisories": 4,
            "R1:_find_static_llm_prompt_advisories": 2,
            "R5:_find_static_llm_prompt_advisories": 1,
        }
    ),
}

# This parser is a fail-closed admission point for our JSON-round-tripped
# completion-gate envelope. Two membership checks distinguish absent optional
# envelope keys from explicit null, while the three required-field probes and
# two Mapping shape checks remain explicit release-signing candidates: every
# malformed present value raises before constructing the owned
# CompletionGateFacts type.
_COMPLETION_GATE_ADJUDICATION_CANDIDATES = Counter(
    {
        "R1:parse_completion_gates": 3,
        "R5:parse_completion_gates": 2,
    }
)


def _suppression_key(message: str, symbol_context: tuple[str, ...]) -> str:
    marker = "@trust_boundary suppressed "
    assert message.startswith(marker)
    original_rule = message.removeprefix(marker).split(maxsplit=1)[0]
    return f"{original_rule}:{':'.join(symbol_context)}"


def test_touched_validation_files_have_only_explicit_adjudication_candidates() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    source_root = repository_root / "src" / "elspeth"
    execution_root = source_root / "web" / "execution"

    findings_by_file: dict[str, list[str]] = {}
    suppressions_by_file: dict[str, Counter[str]] = {}
    for filename in _production_files(execution_root):
        findings, suppressed = scan_file_with_observations(execution_root / filename, source_root)
        if findings:
            findings_by_file[filename] = [f"{finding.rule_id}:{':'.join(finding.symbol_context)}" for finding in findings]
        if suppressed:
            suppressions_by_file[filename] = Counter(_suppression_key(finding.message, finding.symbol_context) for finding in suppressed)

    assert findings_by_file == _ADJUDICATION_CANDIDATES
    assert suppressions_by_file == _EXPECTED_SUPPRESSION_OBSERVATIONS


def test_completion_gate_parser_has_only_explicit_adjudication_candidates() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    source_root = repository_root / "src" / "elspeth"
    findings, suppressed = scan_file_with_observations(
        source_root / "web" / "execution" / "completion_gates.py",
        source_root,
    )

    assert Counter(f"{finding.rule_id}:{':'.join(finding.symbol_context)}" for finding in findings) == (
        _COMPLETION_GATE_ADJUDICATION_CANDIDATES
    )
    assert suppressed == []
