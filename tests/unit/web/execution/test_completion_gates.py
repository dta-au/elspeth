"""Tests for durable composer completion-gate facts.

Covers the four surfaces of ``web/execution/completion_gates.py``: the graph
fingerprint, the writer-side envelope derivation, the Tier-1 parse of the
persisted envelope, and the read-side merge into a recomputed
``ValidationResult``. Spec:
docs-archive/specs/2026-08-01-composer-completion-gate-persistence-design.md.
"""

from __future__ import annotations

from dataclasses import replace

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
    advisor_block_covers_unchanged_graph,
    completion_gate_fingerprint,
    completion_gates_meta_from_facts,
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
                    suggestion=None,
                    note=None,
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


def test_stale_graph_completion_advisory_detail_is_evidence_scoped() -> None:
    assert ADVISOR_SIGNOFF_PENDING_DETAIL == (
        "The evidence-scoped completion advisory review has not covered this pipeline version. "
        "Composer completion remains withheld. Re-run the composer to obtain a current review."
    )
    assert "sign-off" not in ADVISOR_SIGNOFF_PENDING_DETAIL


_BLOCKED_DETAIL = "The advisor sign-off could not be obtained; the pipeline cannot complete."


def test_advisor_suggestion_survives_reload_and_clears_on_graph_change() -> None:
    from elspeth.web.composer.service import _advisor_signoff_pending_validation

    state = _make_state()
    result = _advisor_signoff_pending_validation(
        _green_result(), reason="unavailable", findings="Model unavailable.", category="other", step_ids=(), note=None
    )
    suggestion = (
        "The advisor model was unavailable after retry; check the advisor model configuration. "
        "Validation and the advisory review run again after your next pipeline change."
    )
    assert result.readiness.blockers[0].suggestion == suggestion
    facts = parse_completion_gates({COMPLETION_GATES_META_KEY: completion_gates_meta_value(result, state)})
    reloaded = merge_completion_gates(_green_result(), facts, state)
    assert reloaded.readiness.blockers[0].suggestion == suggestion
    changed = _make_state(node_options={"operations": [{"target": "y", "expression": "2"}]})
    assert merge_completion_gates(_green_result(), facts, changed).readiness.blockers[0].suggestion is None
    clean = parse_completion_gates({COMPLETION_GATES_META_KEY: completion_gates_meta_value(_green_result(), state)})
    assert merge_completion_gates(_green_result(), clean, state).readiness.blockers == []


@pytest.mark.parametrize("value", [7, {}, [], True])
def test_persisted_advisor_suggestion_rejects_malformed_values(value: object) -> None:
    with pytest.raises(ValueError, match="suggestion"):
        parse_completion_gates(
            {
                COMPLETION_GATES_META_KEY: {
                    "advisor_signoff": {
                        "status": "blocked",
                        "detail": "Review blocked",
                        "for_graph": "graph",
                        "note": None,
                        "suggestion": value,
                    }
                }
            }
        )


def test_persisted_advisor_suggestion_is_required() -> None:
    with pytest.raises(ValueError, match="suggestion"):
        parse_completion_gates(
            {
                COMPLETION_GATES_META_KEY: {
                    "advisor_signoff": {
                        "status": "blocked",
                        "detail": "Review blocked",
                        "for_graph": "graph",
                        "note": None,
                    }
                }
            }
        )


@pytest.mark.parametrize("reason", ["flagged_no_repair", "flagged_final_pass", "flagged_unrepairable"])
def test_advisor_readiness_never_publishes_model_findings(reason: str) -> None:
    from elspeth.web.composer.service import (
        _advisor_signoff_blocked_validation,
        _advisor_signoff_pending_validation,
        _advisor_signoff_unverified_validation,
    )

    findings = "PRIVATE_PROVIDER_FINDING credential-shaped-content"
    results = [
        _advisor_signoff_blocked_validation(reason=reason, findings=findings, category="other", step_ids=(), note=None),
        _advisor_signoff_unverified_validation(reason=reason, findings=findings, category="other", step_ids=(), note=None),
        _advisor_signoff_pending_validation(_green_result(), reason=reason, findings=findings, category="other", step_ids=(), note=None),
    ]
    for result in results:
        blocker = result.readiness.blockers[0]
        assert blocker.suggestion
        assert findings not in result.model_dump_json()
        if result.errors:
            assert blocker.suggestion == result.errors[0].suggestion


@pytest.mark.parametrize("reason", ["flagged_no_repair", "flagged_final_pass", "flagged_unrepairable"])
def test_advisor_note_reaches_only_the_blocker_note_field(reason: str) -> None:
    """Ruling 2026-09-22 (elspeth-032ec69c41) narrows R2-F13: the advisor's
    bounded note IS published — in ``blockers[].note`` and nowhere else. The
    raw ``findings_text`` stays off every surface, note included: the note is
    the parser's sanitised extract, not the fenced reply."""
    from elspeth.web.composer.service import (
        _advisor_signoff_blocked_validation,
        _advisor_signoff_pending_validation,
        _advisor_signoff_unverified_validation,
    )

    findings = "PRIVATE_PROVIDER_FINDING credential-shaped-content"
    note = "The merge step cannot route failures; pick per-branch sinks or a partial-arrival policy."
    results = [
        _advisor_signoff_blocked_validation(reason=reason, findings=findings, category="error_handling", step_ids=(), note=note),
        _advisor_signoff_unverified_validation(reason=reason, findings=findings, category="error_handling", step_ids=(), note=note),
        _advisor_signoff_pending_validation(
            _green_result(), reason=reason, findings=findings, category="error_handling", step_ids=(), note=note
        ),
    ]
    for result in results:
        (blocker,) = [b for b in result.readiness.blockers if b.code == ADVISOR_SIGNOFF_BLOCKED_CODE]
        assert blocker.note == note
        assert findings not in result.model_dump_json()
        # Every surface other than the note field is still free of the words.
        assert note not in blocker.detail
        assert note not in (blocker.suggestion or "")
        assert all(note not in check.detail for check in result.checks)
        assert all(note not in error.message and note not in (error.suggestion or "") for error in result.errors)


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
                    suggestion=None,
                    note=None,
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
                "suggestion": None,
                "for_graph": completion_gate_fingerprint(state),
                "note": None,
            }
        }

    def test_clean_preflight_produces_empty(self) -> None:
        assert completion_gates_meta_value(_green_result(), _make_state()) == {}

    def test_none_preflight_produces_empty(self) -> None:
        assert completion_gates_meta_value(None, _make_state()) == {}


# ── Tier-1 parse ────────────────────────────────────────────────────────


class TestParse:
    def test_noncanonical_mapping_cannot_supply_a_persisted_signoff(self) -> None:
        from collections import UserDict

        signoff = UserDict({"status": "blocked", "detail": "d", "for_graph": "f", "note": None})
        with pytest.raises(ValueError, match="expected a dict"):
            parse_completion_gates({COMPLETION_GATES_META_KEY: {"advisor_signoff": signoff}})

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
            suggestion=None,
            for_graph=completion_gate_fingerprint(state),
            note=None,
        )

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "not-a-mapping",
            {"unknown_gate": {}},
            {"advisor_signoff": None},
            {"advisor_signoff": "not-a-mapping"},
            {"advisor_signoff": {"status": "cleared", "detail": "d", "for_graph": "f", "note": None}},
            {"advisor_signoff": {"status": "blocked", "detail": "", "for_graph": "f", "note": None}},
            {"advisor_signoff": {"status": "blocked", "detail": "d", "for_graph": "", "note": None}},
            {"advisor_signoff": {"status": "blocked", "detail": 7, "for_graph": "f", "note": None}},
            {"advisor_signoff": {"status": "blocked", "detail": "d"}},
        ],
    )
    def test_malformed_raises(self, raw: object) -> None:
        with pytest.raises(ValueError, match="Tier 1"):
            parse_completion_gates({COMPLETION_GATES_META_KEY: raw})


# ── Carry-forward serialization ─────────────────────────────────────────


class TestMetaFromFacts:
    def test_round_trips_a_blocked_fact_verbatim(self) -> None:
        """parse → serialize → parse is the identity for a persisted fact —
        the property the recovery-save carry-forward relies on."""
        envelope = {
            COMPLETION_GATES_META_KEY: {
                "advisor_signoff": {
                    "status": "blocked",
                    "detail": "The advisor sign-off could not be obtained.",
                    "suggestion": "Retry advisory review.",
                    "for_graph": "fingerprint-abc",
                    "note": None,
                }
            }
        }
        facts = parse_completion_gates(envelope)
        serialized = completion_gates_meta_from_facts(facts)
        assert serialized == envelope[COMPLETION_GATES_META_KEY]
        assert parse_completion_gates({COMPLETION_GATES_META_KEY: serialized}) == facts

    def test_none_facts_serialize_empty(self) -> None:
        assert completion_gates_meta_from_facts(None) == {}

    def test_no_signoff_fact_serializes_empty(self) -> None:
        assert completion_gates_meta_from_facts(CompletionGateFacts(advisor_signoff=None)) == {}


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
                suggestion=None,
                for_graph=completion_gate_fingerprint(state),
                note=None,
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
                suggestion=None,
                for_graph=completion_gate_fingerprint(blocked_for),
                note=None,
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
                        suggestion=None,
                        note=None,
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
                suggestion=None,
                for_graph=completion_gate_fingerprint(state),
                note=None,
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
                suggestion=None,
                for_graph=completion_gate_fingerprint(state),
                note=None,
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
                suggestion=None,
                for_graph=completion_gate_fingerprint(state),
                note=None,
            )
        )

        once = merge_completion_gates(_green_result(), facts, state)
        twice = merge_completion_gates(once, facts, state)

        assert twice == once
        assert len(_advisor_checks(twice)) == 1
        assert sum(blocker.code == ADVISOR_SIGNOFF_BLOCKED_CODE for blocker in twice.readiness.blockers) == 1
        _assert_canonical_check_order(twice)


def _blocked_facts_for(state: CompositionState, *, note: str | None = None) -> CompletionGateFacts:
    return CompletionGateFacts(
        advisor_signoff=AdvisorSignoffGateFact(
            detail=_BLOCKED_DETAIL,
            suggestion=None,
            for_graph=completion_gate_fingerprint(state),
            note=note,
        )
    )


def test_note_survives_reload_and_reaches_validate() -> None:
    """Ruling 2026-09-22: the note the blocking turn showed is the note /validate shows on the same graph."""
    from elspeth.web.composer.service import _advisor_signoff_pending_validation

    state = _make_state()
    result = _advisor_signoff_pending_validation(
        _green_result(),
        reason="flagged_final_pass",
        findings="FLAGGED: choose per-branch sinks",
        category="error_handling",
        step_ids=(),
        note="choose per-branch sinks",
    )
    assert result.readiness.blockers[0].note == "choose per-branch sinks"
    facts = parse_completion_gates({COMPLETION_GATES_META_KEY: completion_gates_meta_value(result, state)})
    assert facts is not None and facts.advisor_signoff is not None
    assert facts.advisor_signoff.note == "choose per-branch sinks"
    reloaded = merge_completion_gates(_green_result(), facts, state)
    assert reloaded.readiness.blockers[0].note == "choose per-branch sinks"
    # The carry-forward writer keeps it too.
    carried = parse_completion_gates({COMPLETION_GATES_META_KEY: completion_gates_meta_from_facts(facts)})
    assert carried is not None and carried.advisor_signoff is not None
    assert carried.advisor_signoff.note == "choose per-branch sinks"
    # A changed graph gets the pending wording and no note: the words applied
    # to a graph that no longer exists.
    changed = _make_state(node_options={"operations": [{"target": "y", "expression": "2"}]})
    assert merge_completion_gates(_green_result(), facts, changed).readiness.blockers[0].note is None


def test_gate_fact_without_note_key_is_rejected() -> None:
    """Tier 1: an envelope missing the key is writer drift or corruption, never a default."""
    legacy = {
        COMPLETION_GATES_META_KEY: {
            "advisor_signoff": {"status": "blocked", "detail": "d", "suggestion": None, "for_graph": "f"},
        }
    }
    with pytest.raises(ValueError, match="note is required"):
        parse_completion_gates(legacy)


def test_gate_fact_note_null_round_trips() -> None:
    state = _make_state()
    facts = _blocked_facts_for(state, note=None)
    parsed = parse_completion_gates({COMPLETION_GATES_META_KEY: completion_gates_meta_from_facts(facts)})
    assert parsed is not None and parsed.advisor_signoff is not None
    assert parsed.advisor_signoff.note is None
    assert merge_completion_gates(_green_result(), parsed, state).readiness.blockers[0].note is None


def test_writer_normalises_an_empty_note_to_null() -> None:
    """Final review M-2: the reader rejects ``note=""`` but nothing on the
    write path forbade it, so a builder that passed the empty string would
    persist a row every later read rejects — and ``parse_completion_gates`` is
    called UNCAUGHT from /validate, execute, compose, messages, audit readiness
    and shareable reviews. That is a bricked session row, not a degraded one.
    Unreachable today; closed at the writer so it stays that way."""
    state = _make_state()
    result = _advisor_signoff_pending_validation_with_note("")
    assert result.readiness.blockers[0].note == ""
    envelope = completion_gates_meta_value(result, state)
    assert envelope["advisor_signoff"]["note"] is None
    parsed = parse_completion_gates({COMPLETION_GATES_META_KEY: envelope})
    assert parsed is not None and parsed.advisor_signoff is not None
    assert parsed.advisor_signoff.note is None


def _advisor_signoff_pending_validation_with_note(note: str | None) -> ValidationResult:
    from elspeth.web.composer.service import _advisor_signoff_pending_validation

    return _advisor_signoff_pending_validation(
        _green_result(), reason="flagged_final_pass", findings="FLAGGED: x", category="other", step_ids=(), note=note
    )


@pytest.mark.parametrize("bad", ["", 7, b"x"], ids=["empty", "int", "bytes"])
def test_gate_fact_note_must_be_a_non_empty_string_or_null(bad: object) -> None:
    envelope = {
        COMPLETION_GATES_META_KEY: {
            "advisor_signoff": {"status": "blocked", "detail": "d", "suggestion": None, "for_graph": "f", "note": bad},
        }
    }
    with pytest.raises(ValueError, match="note must be"):
        parse_completion_gates(envelope)


def test_block_covers_an_unchanged_graph_it_was_recorded_for() -> None:
    state = _make_state(version=3)
    facts = _blocked_facts_for(state)
    assert advisor_block_covers_unchanged_graph(facts, state, initial_version=state.version) is True


def test_block_does_not_cover_a_turn_that_changed_the_version() -> None:
    state = _make_state(version=3)
    facts = _blocked_facts_for(state)
    assert advisor_block_covers_unchanged_graph(facts, state, initial_version=state.version - 1) is False


def test_block_covers_is_false_without_a_fact() -> None:
    state = _make_state(version=3)
    assert advisor_block_covers_unchanged_graph(None, state, initial_version=state.version) is False
    no_gate = CompletionGateFacts(advisor_signoff=None)
    assert advisor_block_covers_unchanged_graph(no_gate, state, initial_version=state.version) is False


def test_block_does_not_cover_a_different_graph() -> None:
    state = _make_state(version=3)
    blocked = _blocked_facts_for(state).advisor_signoff
    assert blocked is not None
    stale = CompletionGateFacts(advisor_signoff=replace(blocked, for_graph="0" * 64))
    assert advisor_block_covers_unchanged_graph(stale, state, initial_version=state.version) is False
