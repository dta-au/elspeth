"""Trust-tier regressions for the execution-validation refactor surface."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from elspeth_lints.rules.trust_tier.tier_model.rule import scan_file_with_observations

_PRODUCTION_FILES = (
    "_validation_authoring.py",
    "_validation_diagnostics.py",
    "_validation_ledger.py",
    "_validation_materialization.py",
    "_validation_model.py",
    "_validation_pipeline.py",
    "_validation_runtime.py",
    "validation.py",
)

# These R5s are policy-wrong, adjudication-ready nominal checks over owned
# concrete classes: one admits the real scoped-resolver implementation instead
# of a runtime Protocol impostor; the other two discriminate owned closed
# result/exception unions. The static-prompt advisory finder's
# `isinstance(template, str)` skip guard is carried over verbatim from its
# pre-refactor home in validation.py (63d713dc4) and is an explicit candidate
# like its siblings. No filename/name exemption may hide any check.
_ADJUDICATION_CANDIDATES = {
    "_validation_authoring.py": ["R5:_secret_ref_exists", "R5:review_interpretations"],
    "_validation_diagnostics.py": [
        "R5:_infer_component_type_from_plugin_error",
        "R5:_find_static_llm_prompt_advisories",
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
            "R1:_reframe_settings_missing_parts": 2,
            "R5:_reframe_settings_missing_parts": 1,
            "R1:_find_identity_node_advisories": 3,
            "R5:_find_identity_node_advisories": 4,
            "R1:_find_static_llm_prompt_advisories": 2,
        }
    ),
}

# This parser is a fail-closed admission point for our JSON-round-tripped
# completion-gate envelope. The five sentinel probes and two Mapping shape
# checks are explicit release-signing candidates, not source defects or hidden
# exemptions: every malformed present value raises before constructing the
# owned CompletionGateFacts type.
_COMPLETION_GATE_ADJUDICATION_CANDIDATES = Counter(
    {
        "R1:parse_completion_gates": 5,
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
    for filename in _PRODUCTION_FILES:
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
