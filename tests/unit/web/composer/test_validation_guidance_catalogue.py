"""Direct code authority and the historical ordered prose compatibility fence."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import generation
from elspeth.web.plugin_policy.models import PluginUnavailableReason
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, execute_tool


def _historical_fixture():
    return json.loads(Path(__file__).with_name("validation_guidance_legacy_patterns.json").read_text())


def _assert_surviving_order(baseline: list[str], current: list[str]) -> None:
    """Consume occurrences, so duplicate spellings cannot manufacture membership."""
    remaining = iter(baseline)
    for pattern in current:
        assert any(previous == pattern for previous in remaining), f"new, replaced or reordered legacy pattern: {pattern}"


def test_legacy_patterns_preserve_historical_occurrences_and_order() -> None:
    fixture = _historical_fixture()
    occurrences: Counter[str] = Counter()
    for ordinal, row in enumerate(fixture["rows"]):
        assert row["ordinal"] == ordinal
        assert row["occurrence"] == occurrences[row["pattern"]]
        occurrences[row["pattern"]] += 1
    _assert_surviving_order(
        [row["pattern"] for row in fixture["rows"]],
        [pattern for pattern, _explanation, _fix in generation._VALIDATION_ERROR_PATTERNS],
    )


@pytest.mark.parametrize("current", [["a", "x", "b"], ["b", "a"], ["a", "a", "a", "b"]])
def test_legacy_freeze_rejects_replacement_order_and_duplicate_growth(current: list[str]) -> None:
    with pytest.raises(AssertionError, match="new, replaced or reordered"):
        _assert_surviving_order(["a", "a", "b"], current)


def test_legacy_freeze_permits_retirement_without_reordering_survivors() -> None:
    _assert_surviving_order(["a", "a", "b"], ["a", "b"])


@pytest.mark.parametrize("mutation", ["replace", "reorder", "duplicate"])
def test_actual_catalogue_freeze_detects_mutations(monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    rows = list(generation._VALIDATION_ERROR_PATTERNS)
    if mutation == "replace":
        rows[0] = ("brand_new_pattern", rows[0][1], rows[0][2])
    elif mutation == "reorder":
        rows[0], rows[1] = rows[1], rows[0]
    else:
        rows.append(rows[0])
    monkeypatch.setattr(generation, "_VALIDATION_ERROR_PATTERNS", tuple(rows))
    with pytest.raises(AssertionError, match="new, replaced or reordered"):
        test_legacy_patterns_preserve_historical_occurrences_and_order()


def test_every_historical_code_keeps_its_first_legacy_record() -> None:
    codes = _historical_fixture()["legacy_codes"]
    assert list(generation._CLOSED_VALIDATION_ERROR_CODES[: len(codes)]) == codes
    for code in codes:
        record = next(row for row in generation._VALIDATION_ERROR_PATTERNS if re.search(row[0], code))
        assert generation._VALIDATION_GUIDANCE_BY_CODE[code] is record
        assert generation.explain_validation_code(code) == record[1:]


@pytest.mark.parametrize("value", [None, 1, "", "prefix unknown_node_type suffix", "UNKNOWN_NODE_TYPE"])
def test_internal_accessor_only_recognizes_exact_codes(value: object) -> None:
    assert generation.explain_validation_code(value) is None


def _quarantine_rejection_state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(plugin="csv", on_success="rows", options={}, on_validation_failure="missing_sink"),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def test_real_quarantine_producer_has_direct_repair_guidance() -> None:
    from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback

    state = _quarantine_rejection_state()
    entry = next(error for error in state.validate().errors if error.component == "source" and "on_validation_failure" in error.message)
    code = entry.error_code
    assert code is not None
    guidance = generation.explain_validation_code(code)
    assert guidance is not None
    direct = {record.code: record for record in generation._DIRECT_VALIDATION_GUIDANCE}
    assert code in direct
    assert "on_validation_failure" in guidance[0]
    assert "discard" in guidance[1]
    feedback = _allowlisted_candidate_feedback(
        generation.ToolResult(success=False, updated_state=state, validation=state.validate(), affected_nodes=())
    )
    reported = next(error for error in feedback["validation"]["errors"] if error["error_code"] == code)
    assert reported["explanation"] == guidance[0]
    assert reported["suggested_fix"] == guidance[1]


def test_direct_records_are_immutable_and_index_refuses_duplicates() -> None:
    direct = generation.DirectValidationGuidance("new_code", "explanation", "repair")
    with pytest.raises(FrozenInstanceError):
        direct.code = "changed"
    with pytest.raises(AssertionError, match="duplicate"):
        generation._build_validation_guidance_index((), (), (direct, direct))
    legacy = ("new_code", "legacy", "legacy repair")
    with pytest.raises(AssertionError, match="collision"):
        generation._build_validation_guidance_index((legacy,), ("new_code",), (direct,))
    with pytest.raises(AssertionError, match="unresolved"):
        generation._build_validation_guidance_index((), ("unknown",), ())


def test_real_producer_witness_detects_missing_direct_index_record(monkeypatch: pytest.MonkeyPatch) -> None:
    index = dict(generation._VALIDATION_GUIDANCE_BY_CODE)
    del index["quarantine_unknown_output"]
    monkeypatch.setattr(generation, "_VALIDATION_GUIDANCE_BY_CODE", index)
    with pytest.raises(AssertionError):
        test_real_quarantine_producer_has_direct_repair_guidance()


def test_real_count_rejection_reaches_planner_with_mode_choice_guidance() -> None:
    from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback
    from tests.unit.web.composer.test_set_pipeline_candidate import _trained_context

    context = _trained_context()
    state = _empty_state()
    result = execute_tool(
        "upsert_node",
        {
            "id": "batch",
            "node_type": "aggregation",
            "plugin": "batch_stats",
            "input": "rows",
            "on_success": "out",
            "on_error": "discard",
            "output_mode": "passthrough",
            "expected_output_count": 1,
        },
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
    )
    assert not result.success
    assert result.updated_state is state
    (entry,) = result.validation.errors
    assert entry.error_code == "aggregation_expected_output_count_mode_invalid"
    (reported,) = _allowlisted_candidate_feedback(result)["validation"]["errors"]
    assert "suggested_fix" in reported
    assert "omit expected_output_count" in reported["suggested_fix"]
    assert "output_mode='transform'" in reported["suggested_fix"]
    assert "different row semantics" in reported["suggested_fix"]


def test_real_diff_without_baseline_retains_planner_guidance() -> None:
    from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback

    result = execute_tool("diff_pipeline", {}, _empty_state(), _mock_catalog())
    assert not result.success
    (reported,) = _allowlisted_candidate_feedback(result)["validation"]["errors"]
    assert reported["error_code"] == "diff_baseline_unavailable"
    assert "baseline" in reported["explanation"].lower()
    assert reported["suggested_fix"]


@pytest.mark.parametrize(
    ("policy", "merge", "code"),
    [
        ("quorum", "union", "coalesce_policy_quorum_unsupported"),
        ("best_effort", "union", "coalesce_best_effort_requires_timeout"),
        ("require_all", "select", "coalesce_merge_select_unsupported"),
    ],
)
def test_real_coalesce_rejections_retain_planner_guidance(policy: str, merge: str, code: str) -> None:
    from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback

    node = NodeSpec(
        id="merge",
        node_type="coalesce",
        branches=("a", "b"),
        policy=policy,
        merge=merge,
        plugin=None,
        input=None,
        on_success="out",
        on_error=None,
        options={},
        condition=None,
        routes={},
        fork_to=(),
    )
    state = replace(_empty_state(), nodes=(node,))
    validation = state.validate()
    assert any(entry.error_code == code for entry in validation.errors)
    result = generation.ToolResult(success=False, updated_state=state, validation=validation, affected_nodes=())
    reported = next(entry for entry in _allowlisted_candidate_feedback(result)["validation"]["errors"] if entry["error_code"] == code)
    assert reported["explanation"]
    assert reported["suggested_fix"]


def test_legacy_code_duplicates_cannot_disappear_in_index_construction() -> None:
    with pytest.raises(AssertionError, match="duplicate"):
        generation._build_validation_guidance_index((("old", "explanation", "fix"),), ("old", "old"), ())


def test_new_policy_guidance_does_not_extend_legacy_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat an actual policy reason as newly introduced, preserving owned prose."""
    reason = PluginUnavailableReason.WEB_SURFACE_PROHIBITED
    historical = generation._LEGACY_PLUGIN_UNAVAILABLE_REASONS
    patterns = generation._VALIDATION_ERROR_PATTERNS
    monkeypatch.setattr(generation, "_LEGACY_PLUGIN_UNAVAILABLE_REASONS", tuple(item for item in historical if item != reason))
    direct = generation._direct_plugin_policy_guidance()
    assert [record.code for record in direct] == [reason.value]
    assert generation._PLUGIN_UNAVAILABLE_EXPLANATIONS[reason] in direct[0].explanation
    assert direct[0].suggested_fix == generation._PLUGIN_UNAVAILABLE_FIXES[reason]
    assert generation._VALIDATION_ERROR_PATTERNS is patterns


def test_direct_exact_and_noisy_lookup_and_help_derive_from_records() -> None:
    for record in generation._DIRECT_VALIDATION_GUIDANCE:
        exact = execute_tool("explain_validation_error", {"error_text": record.code}, _empty_state(), _mock_catalog())
        assert list(exact.data) == ["error_text", "explanation", "suggested_fix"]
        assert exact.data["explanation"] == record.explanation
        assert exact.data["suggested_fix"] == record.suggested_fix
        noisy = execute_tool("explain_validation_error", {"error_text": f"LOG {record.code.upper()} END"}, _empty_state(), _mock_catalog())
        assert noisy.data["error_code"] == record.code
        assert noisy.data["suggested_fix"] == record.suggested_fix
        help_result = execute_tool("explain_validation_error", {"error_text": "ZZZ_UNRECOGNIZED_QQQ"}, _empty_state(), _mock_catalog())
        assert record.code in help_result.data["suggested_fix"]
        assert "known_codes" not in help_result.data


def test_public_prose_retains_first_match_and_expected_hint() -> None:
    text = "unknown_node_type coalesce_policy_invalid. Expected a declared node kind."
    matching = [row for row in generation._VALIDATION_ERROR_PATTERNS if re.search(row[0], text)]
    assert len(matching) == 2
    assert matching[0][0] == "unknown[ _]node_type"
    result = execute_tool("explain_validation_error", {"error_text": text}, _empty_state(), _mock_catalog())
    assert result.data["explanation"] == matching[0][1]
    assert result.data["suggested_fix"] == matching[0][2] + " Expected a declared node kind."


def test_public_noisy_code_preserves_legacy_seed_precedence() -> None:
    first, second = _historical_fixture()["legacy_codes"][:2]
    result = execute_tool(
        "explain_validation_error",
        {"error_text": f"LOG {second.upper()} {first.upper()} END"},
        _empty_state(),
        _mock_catalog(),
    )
    assert result.data["error_code"] == first


def test_direct_exact_lookup_precedes_any_legacy_prose_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    record = generation._DIRECT_VALIDATION_GUIDANCE[0]
    monkeypatch.setattr(generation, "_VALIDATION_ERROR_PATTERNS", ((".*", "wrong", "wrong"),))
    result = execute_tool("explain_validation_error", {"error_text": record.code}, _empty_state(), _mock_catalog())
    assert result.data["explanation"] == record.explanation
    assert result.data["suggested_fix"] == record.suggested_fix
