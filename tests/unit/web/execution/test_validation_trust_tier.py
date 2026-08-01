"""Trust-tier regressions for the execution-validation refactor surface."""

from __future__ import annotations

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
# result/exception unions. No filename/name exemption may hide any check.
_ADJUDICATION_CANDIDATES = {
    "_validation_authoring.py": ["R5:_secret_ref_exists", "R5:review_interpretations"],
    "_validation_diagnostics.py": ["R5:_infer_component_type_from_plugin_error"],
}


def test_touched_validation_files_have_only_explicit_adjudication_candidates() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    source_root = repository_root / "src" / "elspeth"
    execution_root = source_root / "web" / "execution"

    findings_by_file: dict[str, list[str]] = {}
    for filename in _PRODUCTION_FILES:
        findings, _suppressed = scan_file_with_observations(execution_root / filename, source_root)
        if findings:
            findings_by_file[filename] = [f"{finding.rule_id}:{':'.join(finding.symbol_context)}" for finding in findings]

    assert findings_by_file == _ADJUDICATION_CANDIDATES
