"""Tests for durable composer completion-gate facts.

Covers the four surfaces of ``web/execution/completion_gates.py``: the graph
fingerprint, the writer-side envelope derivation, the Tier-1 parse of the
persisted envelope, and the read-side merge into a recomputed
``ValidationResult``. Spec:
docs-archive/specs/2026-08-01-composer-completion-gate-persistence-design.md.
"""

from __future__ import annotations

import pytest

from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.execution._validation_ledger import CORE_VALIDATION_CHECK_NAMES, ValidationLedger
from elspeth.web.execution.completion_gates import (
    ADVISOR_SIGNOFF_PENDING_DETAIL,
    COMPLETION_GATES_META_KEY,
    AdvisorSignoffGateFact,
    CompletionGateFacts,
    completion_gate_fingerprint,
    completion_gates_meta_value,
    merge_completion_gates,
    parse_completion_gates,
)
from elspeth.web.execution.schemas import (
    ADVISOR_SIGNOFF_BLOCKED_CODE,
    CHECK_ADVISOR_SIGNOFF,
    CHECK_GATE_FAN_OUT_ADVISORY,
    CHECK_IDENTITY_NODE_ADVISORY,
    CHECK_OUTCOME_SECRET_REFS_NO_REFS,
    CHECK_PLUGIN_ENABLEMENT,
    CHECK_SECRET_REFS,
    VALIDATION_CHECK_NAMES,
    ValidationCheck,
    ValidationCheckName,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
)

# ── Fixture builders ────────────────────────────────────────────────────


def _make_node(node_id: str = "map_fields", options: dict | None = None) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="value_transform",
        input="rows_in",
        on_success="rows_out",
        on_error="discard",
        options=options or {"operations": [{"target": "x", "expression": "1"}]},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _make_state(
    *,
    metadata: PipelineMetadata | None = None,
    node_options: dict | None = None,
    version: int = 1,
) -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows_in",
            options={"path": "in.csv"},
            on_validation_failure="discard",
        ),
        nodes=(_make_node(options=node_options),),
        edges=(),
        outputs=(
            OutputSpec(
                name="json_out",
                plugin="json",
                options={"path": "out.json", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=metadata or PipelineMetadata(),
        version=version,
    )


def _green_result() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[
            *(_passed_check(name) for name in CORE_VALIDATION_CHECK_NAMES),
            _passed_check(CHECK_IDENTITY_NODE_ADVISORY),
            _passed_check(CHECK_GATE_FAN_OUT_ADVISORY),
        ],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=True,
            completion_ready=True,
            blockers=[],
        ),
    )


def _passed_check(name: ValidationCheckName) -> ValidationCheck:
    return ValidationCheck(
        name=name,
        passed=True,
        detail=f"{name} passed",
        affected_nodes=(),
        outcome_code=CHECK_OUTCOME_SECRET_REFS_NO_REFS if name == CHECK_SECRET_REFS else None,
    )


def _failed_ledger_result() -> ValidationResult:
    ledger = ValidationLedger()
    return ledger.finish_failure(
        ValidationCheck(
            name=CHECK_PLUGIN_ENABLEMENT,
            passed=False,
            detail="Plugin enablement failed",
            affected_nodes=("source",),
            outcome_code=None,
        ),
        errors=(
            ValidationError(
                component_id="source",
                component_type="source",
                message="Plugin is unavailable",
                suggestion="Enable the plugin.",
                error_code="plugin_unavailable",
            ),
        ),
        readiness=ValidationReadiness(
            authoring_valid=False,
            execution_ready=False,
            completion_ready=False,
            blockers=[
                ValidationReadinessBlocker(
                    code=CHECK_PLUGIN_ENABLEMENT,
                    component_id="source",
                    component_type="source",
                    detail="Plugin enablement failed.",
                )
            ],
        ),
    )


def _advisor_checks(result: ValidationResult) -> list[ValidationCheck]:
    return [check for check in result.checks if check.name == CHECK_ADVISOR_SIGNOFF]


def _assert_canonical_check_order(result: ValidationResult) -> None:
    ranks = [VALIDATION_CHECK_NAMES.index(check.name) for check in result.checks]
    assert ranks == sorted(ranks)


_BLOCKED_DETAIL = "The advisor sign-off could not be obtained; the pipeline cannot complete."


def _signoff_blocked_result() -> ValidationResult:
    """A green build whose completion is withheld by the advisor gate (R2-F14 shape)."""
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=True,
            completion_ready=False,
            blockers=[
                ValidationReadinessBlocker(
                    code=ADVISOR_SIGNOFF_BLOCKED_CODE,
                    component_id="pipeline",
                    component_type="pipeline",
                    detail=_BLOCKED_DETAIL,
                )
            ],
        ),
    )


# ── Fingerprint ─────────────────────────────────────────────────────────


class TestFingerprint:
    def test_stable_across_identical_graphs(self) -> None:
        assert completion_gate_fingerprint(_make_state()) == completion_gate_fingerprint(_make_state())

    def test_metadata_change_does_not_rotate(self) -> None:
        renamed = _make_state(metadata=PipelineMetadata(name="Renamed", description="new words"))
        assert completion_gate_fingerprint(_make_state()) == completion_gate_fingerprint(renamed)

    def test_version_change_does_not_rotate(self) -> None:
        assert completion_gate_fingerprint(_make_state()) == completion_gate_fingerprint(_make_state(version=5))

    def test_node_change_rotates(self) -> None:
        changed = _make_state(node_options={"operations": [{"target": "y", "expression": "2"}]})
        assert completion_gate_fingerprint(_make_state()) != completion_gate_fingerprint(changed)


# ── Writer derivation ───────────────────────────────────────────────────


class TestWriter:
    def test_blocked_preflight_produces_fact(self) -> None:
        state = _make_state()
        value = completion_gates_meta_value(_signoff_blocked_result(), state)
        assert value == {
            "advisor_signoff": {
                "status": "blocked",
                "detail": _BLOCKED_DETAIL,
                "for_graph": completion_gate_fingerprint(state),
            }
        }

    def test_clean_preflight_produces_empty(self) -> None:
        assert completion_gates_meta_value(_green_result(), _make_state()) == {}

    def test_none_preflight_produces_empty(self) -> None:
        assert completion_gates_meta_value(None, _make_state()) == {}


# ── Tier-1 parse ────────────────────────────────────────────────────────


class TestParse:
    def test_absent_meta_is_none(self) -> None:
        assert parse_completion_gates(None) is None
        assert parse_completion_gates({"repair_turns_used": 0}) is None

    def test_empty_mapping_is_no_gates(self) -> None:
        facts = parse_completion_gates({COMPLETION_GATES_META_KEY: {}})
        assert facts == CompletionGateFacts(advisor_signoff=None)

    def test_roundtrip(self) -> None:
        state = _make_state()
        meta = {COMPLETION_GATES_META_KEY: completion_gates_meta_value(_signoff_blocked_result(), state)}
        facts = parse_completion_gates(meta)
        assert facts is not None
        assert facts.advisor_signoff == AdvisorSignoffGateFact(
            detail=_BLOCKED_DETAIL,
            for_graph=completion_gate_fingerprint(state),
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "not-a-mapping",
            {"unknown_gate": {}},
            {"advisor_signoff": "not-a-mapping"},
            {"advisor_signoff": {"status": "cleared", "detail": "d", "for_graph": "f"}},
            {"advisor_signoff": {"status": "blocked", "detail": "", "for_graph": "f"}},
            {"advisor_signoff": {"status": "blocked", "detail": "d", "for_graph": ""}},
            {"advisor_signoff": {"status": "blocked", "detail": 7, "for_graph": "f"}},
            {"advisor_signoff": {"status": "blocked", "detail": "d"}},
        ],
    )
    def test_malformed_raises(self, raw: object) -> None:
        with pytest.raises(ValueError, match="Tier 1"):
            parse_completion_gates({COMPLETION_GATES_META_KEY: raw})


# ── Read-side merge ─────────────────────────────────────────────────────


class TestMerge:
    def test_none_facts_is_identity(self) -> None:
        result = _green_result()
        assert merge_completion_gates(result, None, _make_state()) is result

    def test_no_signoff_fact_is_identity(self) -> None:
        result = _green_result()
        facts = CompletionGateFacts(advisor_signoff=None)
        assert merge_completion_gates(result, facts, _make_state()) is result

    def test_matching_fingerprint_appends_blocker(self) -> None:
        state = _make_state()
        facts = CompletionGateFacts(
            advisor_signoff=AdvisorSignoffGateFact(
                detail=_BLOCKED_DETAIL,
                for_graph=completion_gate_fingerprint(state),
            )
        )
        merged = merge_completion_gates(_green_result(), facts, state)
        assert merged.is_valid is True
        assert merged.readiness.authoring_valid is True
        assert merged.readiness.execution_ready is True
        assert merged.readiness.completion_ready is False
        (blocker,) = merged.readiness.blockers
        assert blocker.code == ADVISOR_SIGNOFF_BLOCKED_CODE
        assert blocker.detail == _BLOCKED_DETAIL
        (check,) = _advisor_checks(merged)
        assert check.name == CHECK_ADVISOR_SIGNOFF
        assert check.passed is False
        assert check.detail == _BLOCKED_DETAIL
        _assert_canonical_check_order(merged)

    def test_stale_fingerprint_uses_pending_wording(self) -> None:
        blocked_for = _make_state()
        current = _make_state(node_options={"operations": [{"target": "y", "expression": "2"}]})
        facts = CompletionGateFacts(
            advisor_signoff=AdvisorSignoffGateFact(
                detail=_BLOCKED_DETAIL,
                for_graph=completion_gate_fingerprint(blocked_for),
            )
        )
        merged = merge_completion_gates(_green_result(), facts, current)
        assert merged.readiness.completion_ready is False
        (blocker,) = merged.readiness.blockers
        assert blocker.code == ADVISOR_SIGNOFF_BLOCKED_CODE
        assert blocker.detail == ADVISOR_SIGNOFF_PENDING_DETAIL
        (check,) = _advisor_checks(merged)
        assert check.detail == ADVISOR_SIGNOFF_PENDING_DETAIL
        _assert_canonical_check_order(merged)

    def test_merge_preserves_recomputed_defects(self) -> None:
        """A stale gate on a now-broken graph must not mask the real defects."""
        state = _make_state()
        base = ValidationResult(
            is_valid=False,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=False,
                execution_ready=False,
                completion_ready=False,
                blockers=[
                    ValidationReadinessBlocker(
                        code="state_exists",
                        component_id=None,
                        component_type=None,
                        detail="No composition state exists for this session.",
                    )
                ],
            ),
        )
        facts = CompletionGateFacts(
            advisor_signoff=AdvisorSignoffGateFact(
                detail=_BLOCKED_DETAIL,
                for_graph=completion_gate_fingerprint(state),
            )
        )
        merged = merge_completion_gates(base, facts, state)
        assert merged.is_valid is False
        assert merged.readiness.authoring_valid is False
        codes = [blocker.code for blocker in merged.readiness.blockers]
        assert codes == ["state_exists", ADVISOR_SIGNOFF_BLOCKED_CODE]

    def test_failed_ledger_reconciles_skipped_advisor_slot(self) -> None:
        state = _make_state()
        base = _failed_ledger_result()
        facts = CompletionGateFacts(
            advisor_signoff=AdvisorSignoffGateFact(
                detail=_BLOCKED_DETAIL,
                for_graph=completion_gate_fingerprint(state),
            )
        )

        merged = merge_completion_gates(base, facts, state)

        assert merged.is_valid is base.is_valid
        assert merged.readiness.authoring_valid is base.readiness.authoring_valid
        assert merged.readiness.execution_ready is base.readiness.execution_ready
        assert merged.readiness.completion_ready is False
        assert merged.errors == base.errors
        assert merged.readiness.blockers[0] == base.readiness.blockers[0]
        assert [blocker.code for blocker in merged.readiness.blockers] == [
            CHECK_PLUGIN_ENABLEMENT,
            ADVISOR_SIGNOFF_BLOCKED_CODE,
        ]
        (advisor_check,) = _advisor_checks(merged)
        assert advisor_check.passed is False
        assert advisor_check.detail == _BLOCKED_DETAIL
        assert advisor_check.outcome_code is None
        _assert_canonical_check_order(merged)

    def test_merge_is_idempotent_for_already_merged_result(self) -> None:
        state = _make_state()
        facts = CompletionGateFacts(
            advisor_signoff=AdvisorSignoffGateFact(
                detail=_BLOCKED_DETAIL,
                for_graph=completion_gate_fingerprint(state),
            )
        )

        once = merge_completion_gates(_green_result(), facts, state)
        twice = merge_completion_gates(once, facts, state)

        assert twice == once
        assert len(_advisor_checks(twice)) == 1
        assert sum(blocker.code == ADVISOR_SIGNOFF_BLOCKED_CODE for blocker in twice.readiness.blockers) == 1
        _assert_canonical_check_order(twice)
