# tests/unit/web/composer/test_validation_error_codes.py
"""Every candidate-path rejection carries a closed error_code + structural facts.

Guided A/B session 5113b7ac (attempts 6/10/12, 2026-07-22) died
REPAIR_EXHAUSTED with ``rejection_codes=[]``: the planner's redacted repair
feedback (``_allowlisted_candidate_feedback``) strips raw validation messages
and keys its enrichment on the closed ``error_code`` — so a ``ValidationEntry``
emitted without one forwards NOTHING actionable. A rejection with no code and
no message is unrepairable by construction.

These tests pin the closure:

- the schema-contract family carries ``schema_contract_violation`` /
  ``sink_contract_violation`` / ``locked_input_extras`` / ``sink_locked_extras``
  plus a structured ``contract`` detail naming producer, consumer, and the
  missing/extra FIELD NAMES (pipeline identifiers and schema field names from
  validated config — never user row content, hence redaction-safe);
- representative structural rejections each carry their closed code;
- the closed-code catalogue stays containment-free (no code is a substring of
  another), which the explain tool's fuzzy route and the regex alternations
  in ``_VALIDATION_ERROR_PATTERNS`` both depend on;
- the planner feedback projection forwards code + static guidance + contract
  facts, and the per-attempt trail can never report an empty code list for a
  rejection that carried entries.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.web.composer.state import (
    _PROMPT_TEMPLATE_UNDECLARED_ROW_FIELDS_EXPLANATION,
    _PROMPT_TEMPLATE_UNDECLARED_ROW_FIELDS_FIX,
    _TRANSFORM_DECLARED_NOT_GUARANTEED_EXPLANATION,
    _TRANSFORM_DECLARED_NOT_GUARANTEED_FIX,
    _TRANSFORM_OUTPUT_COLLISION_EXPLANATION,
    _TRANSFORM_OUTPUT_COLLISION_FIX,
    CompositionState,
    EdgeSpec,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    ValidationEntry,
    ValidationSummary,
)
from elspeth.web.composer.tools._common import _PLUGIN_UNAVAILABLE_EXPLANATIONS
from elspeth.web.composer.tools.generation import (
    _CLOSED_VALIDATION_ERROR_CODES,
    _PLUGIN_UNAVAILABLE_FIXES,
    explain_validation_code,
)
from elspeth.web.plugin_policy.models import PluginUnavailableReason


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _make_source(
    on_success: str = "t1",
    options: dict[str, Any] | None = None,
) -> SourceSpec:
    return SourceSpec(
        plugin="csv",
        on_success=on_success,
        options={"path": "/data/input.csv", **(options or {})},
        on_validation_failure="discard",
    )


def _make_transform(
    id: str,
    input: str,
    on_success: str,
    options: dict[str, Any] | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=id,
        node_type="transform",
        plugin="value_transform",
        input=input,
        on_success=on_success,
        on_error="discard",
        options={
            "schema": {"mode": "observed"},
            "operations": [{"target": "_placeholder", "expression": "row['text']"}],
            **(options or {}),
        },
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _make_output(name: str = "main", options: dict[str, Any] | None = None) -> OutputSpec:
    return OutputSpec(
        name=name,
        plugin="csv",
        options={"path": f"outputs/{name}.csv", "schema": {"mode": "observed"}, **(options or {})},
        on_write_failure="discard",
    )


def _make_edge(id: str, from_node: str, to_node: str) -> EdgeSpec:
    return EdgeSpec(id=id, from_node=from_node, to_node=to_node, edge_type="on_success", label=None)


def _entries_with_code(result: ValidationSummary, code: str) -> list[ValidationEntry]:
    return [e for e in result.errors if e.error_code == code]


class TestSchemaContractFamilyCodes:
    """The five schema-contract emitters carry codes + structured facts."""

    def test_node_contract_violation_carries_code_and_facts(self) -> None:
        state = _empty_state()
        state = state.with_source(_make_source(options={"schema": {"mode": "observed"}}))
        state = state.with_node(_make_transform("t1", "t1", "main", options={"required_input_fields": ["text"]}))
        state = state.with_output(_make_output())
        state = state.with_edge(_make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        entries = _entries_with_code(result, "schema_contract_violation")
        assert entries, [e.to_dict() for e in result.errors]
        detail = entries[0].contract
        assert detail is not None
        assert detail.consumer == "t1"
        assert detail.producer  # source producer id
        assert detail.missing_fields == ("text",)

    def test_sink_contract_violation_carries_code_and_facts(self) -> None:
        state = _empty_state()
        state = state.with_source(_make_source(on_success="main", options={"schema": {"mode": "fixed", "fields": ["other: str"]}}))
        state = state.with_output(_make_output(options={"schema": {"mode": "observed", "required_fields": ["text"]}}))
        result = state.validate()
        assert not result.is_valid
        entries = _entries_with_code(result, "sink_contract_violation")
        assert entries, [e.to_dict() for e in result.errors]
        detail = entries[0].contract
        assert detail is not None
        assert detail.consumer == "output:main"
        assert detail.missing_fields == ("text",)

    def test_locked_input_extras_carries_code_and_facts(self) -> None:
        state = _empty_state()
        state = state.with_source(_make_source(options={"schema": {"mode": "fixed", "fields": ["text: str", "extra: str"]}}))
        state = state.with_node(
            _make_transform(
                "t1",
                "t1",
                "main",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_output(_make_output())
        result = state.validate()
        assert not result.is_valid
        entries = _entries_with_code(result, "locked_input_extras")
        assert entries, [e.to_dict() for e in result.errors]
        detail = entries[0].contract
        assert detail is not None
        assert detail.consumer == "t1"
        assert "extra" in detail.extra_fields

    def test_sink_locked_extras_carries_code_and_facts(self) -> None:
        state = _empty_state()
        state = state.with_source(
            _make_source(on_success="main", options={"schema": {"mode": "fixed", "fields": ["text: str", "extra: str"]}})
        )
        state = state.with_output(_make_output(options={"schema": {"mode": "fixed", "fields": ["text: str"]}}))
        result = state.validate()
        assert not result.is_valid
        entries = _entries_with_code(result, "sink_locked_extras")
        assert entries, [e.to_dict() for e in result.errors]
        detail = entries[0].contract
        assert detail is not None
        assert detail.consumer == "output:main"
        assert "extra" in detail.extra_fields

    def test_contract_config_parse_failure_carries_code(self) -> None:
        state = _empty_state()
        state = state.with_source(_make_source(options={"schema": {"mode": "observed"}}))
        state = state.with_node(_make_transform("t1", "t1", "main", options={"required_input_fields": "text"}))
        state = state.with_output(_make_output())
        result = state.validate()
        assert not result.is_valid
        assert _entries_with_code(result, "contract_config_invalid"), [e.to_dict() for e in result.errors]


class TestStructuralRejectionCodes:
    """Representative structural rejections each carry their closed code."""

    def test_empty_state_names_missing_source_and_sinks(self) -> None:
        result = _empty_state().validate()
        assert not result.is_valid
        assert _entries_with_code(result, "no_source_configured")
        assert _entries_with_code(result, "no_sinks_configured")

    def test_unreachable_input_carries_code(self) -> None:
        state = _empty_state()
        state = state.with_source(_make_source(on_success="rows"))
        state = state.with_node(_make_transform("t1", "nowhere", "main"))
        state = state.with_output(_make_output())
        result = state.validate()
        assert not result.is_valid
        assert _entries_with_code(result, "node_input_not_reachable"), [e.to_dict() for e in result.errors]

    def test_duplicate_connection_producer_carries_code(self) -> None:
        state = _empty_state()
        state = state.with_source(_make_source(on_success="shared"))
        state = state.with_node(_make_transform("t1", "shared", "shared"))
        state = state.with_node(_make_transform("t2", "shared", "main"))
        state = state.with_output(_make_output())
        result = state.validate()
        assert not result.is_valid
        assert _entries_with_code(result, "duplicate_connection_producer"), [e.to_dict() for e in result.errors]

    def test_aggregation_missing_on_error_carries_code(self) -> None:
        state = _empty_state()
        state = state.with_source(_make_source(on_success="agg_in"))
        state = state.with_node(
            NodeSpec(
                id="agg",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg_in",
                on_success="main",
                on_error=None,
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                trigger={"count": 10},
            )
        )
        state = state.with_output(_make_output())
        result = state.validate()
        assert not result.is_valid
        assert _entries_with_code(result, "aggregation_missing_on_error"), [e.to_dict() for e in result.errors]

    def test_battery_states_emit_no_codeless_errors(self) -> None:
        """Sweep: every error the battery produces carries a closed code.

        This is the campaign's boundary rule stated as an invariant: the
        planner feedback can only enrich what carries a code, so a codeless
        error is a blind (unrepairable) rejection by construction.
        """
        battery: list[CompositionState] = []

        battery.append(_empty_state())

        s = _empty_state()
        s = s.with_source(_make_source(options={"schema": {"mode": "observed"}}))
        s = s.with_node(_make_transform("t1", "t1", "main", options={"required_input_fields": ["text"]}))
        s = s.with_output(_make_output())
        battery.append(s)

        s = _empty_state()
        s = s.with_source(_make_source(on_success="rows"))
        s = s.with_node(_make_transform("t1", "nowhere", "main"))
        s = s.with_output(_make_output())
        battery.append(s)

        s = _empty_state()
        s = s.with_source(_make_source(on_success="shared"))
        s = s.with_node(_make_transform("t1", "shared", "shared"))
        s = s.with_node(_make_transform("t2", "shared", "main"))
        s = s.with_output(_make_output())
        battery.append(s)

        s = _empty_state()
        s = s.with_source(_make_source(on_success="main", options={"schema": {"mode": "fixed", "fields": ["other: str"]}}))
        s = s.with_output(_make_output(options={"schema": {"mode": "observed", "required_fields": ["text"]}}))
        battery.append(s)

        for state in battery:
            result = state.validate()
            assert not result.is_valid
            codeless = [e.to_dict() for e in result.errors if not e.error_code]
            assert not codeless, f"codeless rejection(s) escaped the closed-code sweep: {codeless}"


class TestClosedCodeCatalogueInvariants:
    def test_unknown_node_type_guidance_includes_row_union_n_to_n_reconvergence(self) -> None:
        guidance = explain_validation_code("unknown_node_type")

        assert guidance is not None
        explanation, fix = guidance
        assert "row_union" in explanation
        assert "row_union" in fix
        assert "N-to-N" in fix

    def test_schema_contract_codes_are_registered_and_explainable(self) -> None:
        for code in (
            "schema_contract_violation",
            "sink_contract_violation",
            "locked_input_extras",
            "sink_locked_extras",
            "transform_contract_violation",
            "transform_declared_output_not_guaranteed",
            "contract_config_invalid",
            "node_input_not_reachable",
            "duplicate_connection_producer",
            "duplicate_connection_consumer",
            "no_source_configured",
            "no_sinks_configured",
            "aggregation_missing_on_error",
            "coalesce_branch_unreachable",
            "coalesce_schema_mode_mixed",
            "row_union_config_invalid",
            "row_union_name_invalid",
            "row_union_branches_invalid",
            "row_union_branch_invalid",
            "row_union_input_mismatch",
            "row_union_on_success_invalid",
            "row_union_timeout_invalid",
            "row_union_branch_alias_unreachable",
            "row_union_branch_unreachable",
            "row_union_branch_not_downstream",
            "row_union_branch_aggregation_invalid",
            "row_union_nested_fork_invalid",
            "row_union_downstream_group_invalid",
            "row_union_schema_incompatible",
            "row_union_on_success_must_be_connection",
            "row_union_on_success_dangling",
            "fork_branch_multiple_barriers",
            "gate_duplicate_fork_branch",
        ):
            assert code in _CLOSED_VALIDATION_ERROR_CODES, code
            guidance = explain_validation_code(code)
            assert guidance is not None, f"{code} does not resolve to catalogue guidance"
            explanation, fix = guidance
            assert explanation and fix

    def test_prompt_template_undeclared_row_fields_resolves_to_its_own_guidance(self) -> None:
        """The single-prompt declaration guard must not fall through to either sibling.

        ``prompt_template_unbound_variables`` advises "rewrite each name as
        '{{ row.<field> }}'" — circular here, because the reference already IS
        ``row.<field>``; the defect is that the DECLARATION does not cover it.
        ``query_template_unbound_row_fields`` advises ``input_fields`` and
        ``row.source_row``, neither of which a single-prompt node has
        (elspeth-a9ba80cb0b).
        """
        assert "prompt_template_undeclared_row_fields" in _CLOSED_VALIDATION_ERROR_CODES

        guidance = explain_validation_code("prompt_template_undeclared_row_fields")
        assert guidance is not None, "prompt_template_undeclared_row_fields does not resolve to catalogue guidance"
        explanation, fix = guidance
        assert explanation and fix
        assert "required_input_fields" in explanation
        assert "required_input_fields" in fix

        assert guidance != explain_validation_code("prompt_template_unbound_variables")
        assert guidance != explain_validation_code("query_template_unbound_row_fields")

        # Neither sibling's vocabulary: a single-prompt node has no per-query
        # ``input_fields`` and no ``row.source_row``.
        assert "input_fields" not in fix.replace("options.required_input_fields", "").replace("required_input_fields", "")
        assert "source_row" not in fix

        # The advice must not steer the planner to the one repair that clears
        # this error by withdrawing the contract for every OTHER field too.
        assert "withdraws the contract for every field" in fix

        # Rewrite-the-reference must LEAD. ``verify_declared_required_fields``
        # is a plain set difference over row keys with no dual-name limb, so
        # declaring a read name the producer does not guarantee is accepted at
        # config time and then raises on every row — leading with it would hand
        # the planner a repair that clears this error and breaks the run
        # (elspeth-a9ba80cb0b). This is the claim the catalogue must carry, not
        # a property of whatever string happened to be written first.
        assert fix.index("Rewrite each reference") < fix.index("Add a name to options.required_input_fields")
        assert "ONLY if the upstream producer guarantees that exact name" in fix
        assert "fails every row at run time" in fix

    def test_transform_contract_advice_has_exactly_one_owner(self) -> None:
        """The catalogue must SERVE ``state``'s advice, never restate it.

        These two texts reach the planner on disjoint paths — the rendered
        message only ever on a tool call, this catalogue only ever on the
        one-shot planner's repair turn, which projects
        ``explanation``/``suggested_fix`` from the error_code and withholds the
        message. So a second copy here cannot be caught by reading either path:
        88137581b rewrote the message and left this catalogue on advice written
        a month earlier, and the planner alternated remedy sets depending on how
        it had learned of the error (elspeth-920bd88299).

        Identity, not equality: a copied string would satisfy ``==`` on the day
        it was copied and drift on the next edit, which is the failure being
        pinned.
        """
        for code, expected in (
            (
                "transform_declared_output_not_guaranteed",
                (_TRANSFORM_DECLARED_NOT_GUARANTEED_EXPLANATION, _TRANSFORM_DECLARED_NOT_GUARANTEED_FIX),
            ),
            (
                "transform_contract_violation",
                (_TRANSFORM_OUTPUT_COLLISION_EXPLANATION, _TRANSFORM_OUTPUT_COLLISION_FIX),
            ),
            (
                "prompt_template_undeclared_row_fields",
                (_PROMPT_TEMPLATE_UNDECLARED_ROW_FIELDS_EXPLANATION, _PROMPT_TEMPLATE_UNDECLARED_ROW_FIELDS_FIX),
            ),
        ):
            guidance = explain_validation_code(code)
            assert guidance is not None, code
            explanation, fix = guidance
            assert explanation is expected[0], f"{code} explanation is a copy, not the shared constant"
            assert fix is expected[1], f"{code} fix is a copy, not the shared constant"

    def test_the_two_transform_contract_rules_do_not_share_repair_advice(self) -> None:
        """Rule C and Rule D are different defects and must resolve differently.

        They shared one error_code until elspeth-920bd88299, and the catalogue
        is keyed on the code — so one entry was served to both. A Rule D
        collision on an ``llm`` node received field_mapper advice naming
        ``mapping`` and ``select_only``, options that node does not have.
        """
        declared = explain_validation_code("transform_declared_output_not_guaranteed")
        collision = explain_validation_code("transform_contract_violation")
        assert declared is not None and collision is not None
        assert declared != collision

        # Rule D's advice must not name field_mapper-only options: it fires for
        # any transform, most visibly an llm rewriting in place.
        collision_text = " ".join(collision)
        assert "select_only" not in collision_text, collision_text
        assert "response_field" in collision_text, collision_text

        # Rule C's advice must not claim the field cannot be EMITTED. It is
        # emitted whenever the source is present; what is missing is the
        # GUARANTEE, and conflating the two is what produced remedies telling
        # authors to map a field the mapping already targets.
        declared_text = " ".join(declared)
        assert "guarantee" in declared_text, declared_text

    def test_row_union_topology_codes_resolve_to_topology_guidance(self) -> None:
        """The new codes must not fall through to the intrinsic entry.

        ``explain_validation_code`` returns the first matching pattern, so a
        mis-ordered catalogue entry would silently route a topology code to
        the node-shape guidance ("give every branch a non-empty unique
        alias") — the exact mis-advice the code split removes.
        """
        intrinsic = explain_validation_code("row_union_branch_invalid")
        assert intrinsic is not None

        downstream = explain_validation_code("row_union_branch_not_downstream")
        assert downstream is not None
        assert downstream != intrinsic
        assert "downstream" in downstream[0] or "downstream" in downstream[1]

        branch_aggregation = explain_validation_code("row_union_branch_aggregation_invalid")
        assert branch_aggregation is not None
        assert branch_aggregation not in (intrinsic, downstream)
        # 2026-08-23 remedy enrichment (Task 9 ruling, F1 fix round): the
        # remedy no longer recommends output_mode: passthrough — rule 6
        # (ruling 25) bans aggregators inside every bound region regardless
        # of mode, so that advice would land the planner straight in a NEW
        # rejection (the exact wasted-turn defect this pin flip closes).
        assert "passthrough" not in branch_aggregation[1]
        assert "rule 6" in branch_aggregation[1]
        assert "trigger" not in branch_aggregation[1]

        nested_fork = explain_validation_code("row_union_nested_fork_invalid")
        assert nested_fork is not None
        assert nested_fork not in (intrinsic, downstream, branch_aggregation)
        assert "nested fork" in nested_fork[0].lower()
        assert "before" in nested_fork[1] or "terminate" in nested_fork[1]

        invalid_name = explain_validation_code("row_union_name_invalid")
        assert invalid_name is not None
        assert "name" in invalid_name[0].lower()
        assert "letters" in invalid_name[1].lower()

        downstream_group = explain_validation_code("row_union_downstream_group_invalid")
        assert downstream_group is not None
        assert downstream_group not in (intrinsic, downstream, branch_aggregation, nested_fork)
        assert "indivisible" in downstream_group[0] or "indivisible" in downstream_group[1]
        assert "end_of_source" in downstream_group[1]
        assert "branches" not in downstream_group[1]

        schema_incompatible = explain_validation_code("row_union_schema_incompatible")
        assert schema_incompatible is not None
        assert schema_incompatible not in (intrinsic, downstream, downstream_group)
        assert "long-format" in schema_incompatible[0]
        assert "row_union_schema" in schema_incompatible[1]

        # The cross-node barrier-ownership code sits in the same cluster and
        # must not fall through to any row_union node-shape entry either.
        multiple_barriers = explain_validation_code("fork_branch_multiple_barriers")
        assert multiple_barriers is not None
        assert multiple_barriers not in (intrinsic, downstream)
        assert "barrier" in multiple_barriers[0]

    def test_on_error_closer_out_of_region_resolves_both_the_code_and_the_runtime_text(self) -> None:
        """Task 11 (spec §7 rule 9): same shape as the scope_escalate pin
        above and for the same reason — the entry is outside
        ``_CLOSED_VALIDATION_ERROR_CODES`` (Stage 1 deliberately RELAXES
        instead of mirroring the out-of-region rejection, so it never emits
        this code), which means the runtime-message route (the alternation's
        second limb, surfaced via ``preview_pipeline``'s Stage-2 build error)
        is the only live route and would otherwise ship unpinned.
        """
        by_code = explain_validation_code("on_error_closer_out_of_region")
        assert by_code is not None
        assert "closer" in by_code[0]
        assert "sink name or 'discard'" in by_code[1]

        # Representative fragment of the real builder message
        # (core/dag/builder.py rule-9 resolution pass).
        by_runtime_text = explain_validation_code(
            "Transform 'cleanup' on_error 'merge_paths' names closer 'merge_paths' but 'cleanup' "
            "is not inside that closer's bound region. A closer is a legal on_error target only "
            "from inside its own region (spec §7 rule 9)."
        )
        assert by_runtime_text == by_code

    def test_query_template_unbound_row_fields_resolves_to_multi_query_guidance(self) -> None:
        """The multi-query row-binding code must not fall through to the
        single-prompt unbound-variables entry: the repair is different (bind
        the variable in that query's input_fields, or use row.source_row),
        and the single-prompt advice ("rewrite as row.<field>") would send
        the planner in a circle — the reference already IS row.<field>."""
        assert "query_template_unbound_row_fields" in _CLOSED_VALIDATION_ERROR_CODES

        guidance = explain_validation_code("query_template_unbound_row_fields")
        assert guidance is not None, "query_template_unbound_row_fields does not resolve to catalogue guidance"
        explanation, fix = guidance
        assert explanation and fix
        assert "input_fields" in explanation
        assert "input_fields" in fix
        assert "source_row" in fix

        single_prompt = explain_validation_code("prompt_template_unbound_variables")
        assert single_prompt is not None
        assert guidance != single_prompt

    @pytest.mark.parametrize(
        "code",
        ("guided_amend_contract_violation", "guided_revision_unchanged"),
    )
    def test_guided_prose_amend_codes_are_closed_and_actionable(self, code: str) -> None:
        assert code in _CLOSED_VALIDATION_ERROR_CODES
        guidance = explain_validation_code(code)
        assert guidance is not None
        explanation, fix = guidance
        assert explanation and fix
        assert "correction_target" not in explanation
        assert "correction_target" not in fix
        assert "private" not in explanation.lower()
        assert "private" not in fix.lower()

    @pytest.mark.parametrize(
        "code",
        (
            "guided_delta_unknown_stable_id",
            "guided_delta_duplicate_stable_id",
            "guided_delta_authority_violation",
            "guided_delta_nonincident_route",
            "guided_delta_unknown_reference",
            "guided_delta_reviewed_failure_route_required",
            "guided_collector_not_authorable",
        ),
    )
    def test_guided_delta_codes_are_closed_and_actionable(self, code: str) -> None:
        assert code in _CLOSED_VALIDATION_ERROR_CODES
        guidance = explain_validation_code(code)
        assert guidance is not None
        explanation, fix = guidance
        assert explanation and fix
        assert "tutorial" not in explanation.lower()
        assert "tutorial" not in fix.lower()
        assert "private" not in explanation.lower()
        assert "private" not in fix.lower()

    def test_reviewed_output_projection_conflict_is_closed_and_actionable(self) -> None:
        code = "reviewed_output_projection_conflict"
        assert code in _CLOSED_VALIDATION_ERROR_CODES

        guidance = explain_validation_code(code)
        assert guidance is not None
        explanation, fix = guidance
        assert "select-only field_mapper" in explanation
        assert "VALUES" in explanation
        assert "missing_fields" in explanation
        assert "options.mapping value" in fix
        assert "reviewed output form" in fix
        assert "tutorial" not in (explanation + fix).lower()
        assert "private" not in (explanation + fix).lower()

    def test_codes_are_containment_free(self) -> None:
        """No closed code may be a substring of another.

        The explain tool's fuzzy route scans codes by substring containment
        and the catalogue patterns embed codes as regex alternations — a
        contained code would mis-resolve to whichever entry scans first.
        """
        codes = _CLOSED_VALIDATION_ERROR_CODES
        offenders = [(a, b) for a in codes for b in codes if a != b and a in b]
        assert not offenders, offenders


class TestPlannerFeedbackCarriesStructuralFacts:
    def test_allowlisted_feedback_projects_contract_facts(self) -> None:
        from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback
        from elspeth.web.composer.state import SchemaContractDetail
        from elspeth.web.composer.tools import ToolResult

        entry = ValidationEntry(
            component="node:llm_tone",
            message="Schema contract violation: 'fork_ab' -> 'llm_tone'. (raw message must NOT be forwarded)",
            severity="high",
            error_code="schema_contract_violation",
            contract=SchemaContractDetail(
                producer="fork_ab",
                consumer="llm_tone",
                missing_fields=("color_name", "hex"),
            ),
        )
        result = ToolResult(
            success=False,
            updated_state=_empty_state(),
            validation=ValidationSummary(is_valid=False, errors=(entry,), warnings=(), suggestions=()),
            affected_nodes=(),
        )
        feedback = _allowlisted_candidate_feedback(result)
        projected = feedback["validation"]["errors"][0]
        assert projected["error_code"] == "schema_contract_violation"
        assert "message" not in projected
        assert projected["explanation"]
        assert projected["suggested_fix"]
        assert projected["contract"] == {
            "producer": "fork_ab",
            "consumer": "llm_tone",
            "missing_fields": ["color_name", "hex"],
        }

    def test_allowlisted_feedback_codeless_entry_still_names_a_code(self) -> None:
        from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback
        from elspeth.web.composer.tools import ToolResult

        entry = ValidationEntry(component="node:x", message="anything", severity="high")
        result = ToolResult(
            success=False,
            updated_state=_empty_state(),
            validation=ValidationSummary(is_valid=False, errors=(entry,), warnings=(), suggestions=()),
            affected_nodes=(),
        )
        feedback = _allowlisted_candidate_feedback(result)
        assert feedback["validation"]["errors"][0]["error_code"] == "validation_error"

    def test_review_contract_guidance_quotes_the_recognition_constants_verbatim(self) -> None:
        """The repair guidance must BE the literal minimal delta.

        Tutorial op 18b4cee7 (session c98e8561, 2026-07-22, post-356d839a8):
        four generations including the opus hatch each drew the single code
        ``interpretation_review_contract_unsatisfied`` WITH guidance live.
        The contract recognizes the cleanup row only when user_term equals
        RAW_HTML_CLEANUP_USER_TERM AND the draft's lowercase contains every
        _RAW_HTML_CLEANUP_DRAFT_MARKERS substring — guidance inviting a
        free-text draft steers the planner into an unrecognized-row loop
        where the identical code fires forever. The suggested_fix must quote
        the registered user_term and the canonical draft constant verbatim
        so a copy-paste repair is guaranteed to be recognized.
        """
        from elspeth.web.composer.tools.generation import explain_validation_code
        from elspeth.web.interpretation_state import (
            RAW_HTML_CLEANUP_REVIEW_DRAFT,
            RAW_HTML_CLEANUP_USER_TERM,
        )

        guidance = explain_validation_code("interpretation_review_contract_unsatisfied")
        assert guidance is not None
        _explanation, suggested_fix = guidance
        assert RAW_HTML_CLEANUP_USER_TERM in suggested_fix
        assert RAW_HTML_CLEANUP_REVIEW_DRAFT in suggested_fix

    def test_rejected_mutation_gates_stale_state_errors_out_of_feedback_and_trail(self) -> None:
        """A pre-application rejection must not carry the unchanged state's errors.

        Tutorial session 38e3e7f8 (op 1152d7e3, 2026-07-22): every semantic
        set_pipeline rejection on the empty-seed surface reached the planner
        as ``['no_sinks_configured', 'no_source_configured', 'validation_error']``
        — the real reason reduced to a bare placeholder and two red herrings
        describing a state the planner was not editing (it authors a full
        replacement pipeline). The planner "converged" by dropping every node.
        When a ``rejected_mutation`` entry is present, feedback and trail must
        carry ONLY the rejection entries.
        """
        from elspeth.web.composer.pipeline_planner import (
            _allowlisted_candidate_feedback,
            _candidate_rejection_codes,
        )
        from elspeth.web.composer.tools._common import _failure_result

        result = _failure_result(
            _empty_state(),
            "File sink 'json' must set mode explicitly. Use 'write' or 'append'.",
        )
        # The empty state contributes no_source_configured/no_sinks_configured
        # to validation.errors — stale-state noise for a full-replacement tool.
        assert {entry.error_code for entry in result.validation.errors} >= {"no_source_configured", "no_sinks_configured"}

        feedback = _allowlisted_candidate_feedback(result)
        assert [entry["component"] for entry in feedback["validation"]["errors"]] == ["rejected_mutation"]
        assert _candidate_rejection_codes(result) == ("validation_error",)

    def test_validated_candidate_rejections_pass_through_ungated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without a rejected_mutation entry, every real error must survive.

        Guards the instance-1 class (built candidate validated, real errors,
        e.g. coalesce_branch_unreachable) against over-gating.
        """
        import elspeth.web.composer.pipeline_planner as planner_module
        from elspeth.web.composer.pipeline_planner import (
            _allowlisted_candidate_feedback,
            _candidate_rejection_codes,
        )
        from elspeth.web.composer.tools import ToolResult

        entries = (
            ValidationEntry(component="node:merge", message="m", severity="high", error_code="coalesce_branch_unreachable"),
            ValidationEntry(component="node:copy", message="m", severity="high", error_code="node_input_not_reachable"),
        )
        result = ToolResult(
            success=False,
            updated_state=_empty_state(),
            validation=ValidationSummary(is_valid=False, errors=entries, warnings=(), suggestions=()),
            affected_nodes=(),
        )
        monkeypatch.setattr(
            planner_module,
            "coalesce_reachability_facts",
            lambda _state: {"merge": {"produced_connections": ["left", "right"]}},
        )
        feedback = _allowlisted_candidate_feedback(result)
        assert [entry["error_code"] for entry in feedback["validation"]["errors"]] == [
            "coalesce_branch_unreachable",
            "node_input_not_reachable",
        ]
        assert feedback["validation"]["errors"][0]["connectivity"] == {"produced_connections": ["left", "right"]}
        assert _candidate_rejection_codes(result) == ("coalesce_branch_unreachable", "node_input_not_reachable")

    def test_rejection_trail_codes_never_empty_when_entries_exist(self) -> None:
        """The per-attempt trail must name every rejection, coded or not.

        A codeless entry surfaces as the 'validation_error' placeholder in
        rejection_codes rather than silently vanishing — REPAIR_EXHAUSTED
        with rejection_codes=[] while entries existed is the exact blindness
        session 5113b7ac exposed.
        """
        from elspeth.web.composer.pipeline_planner import _candidate_rejection_codes
        from elspeth.web.composer.tools import ToolResult

        entries = (
            ValidationEntry(component="node:a", message="m", severity="high"),
            ValidationEntry(component="node:b", message="m", severity="high", error_code="schema_contract_violation"),
        )
        result = ToolResult(
            success=False,
            updated_state=_empty_state(),
            validation=ValidationSummary(is_valid=False, errors=entries, warnings=(), suggestions=()),
            affected_nodes=(),
        )
        codes = _candidate_rejection_codes(result)
        assert codes == ("validation_error", "schema_contract_violation")


class TestFullReplacementRejectionsWithholdStaleStateEntries:
    """set_pipeline rejections carry no stale pre-mutation state entries.

    elspeth-e89e6bf47a: the planner-side ``_rejection_entries`` gate protects
    only the planner surface. The freeform chat loop (``serialize_tool_result``)
    and the composer MCP server both serialize ``ToolResult.to_dict()``
    verbatim, so for the full-replacement tool the pre-mutation state's errors
    (``no_source_configured`` / ``no_sinks_configured`` on an empty session)
    must not exist at the producer — a repair loop reading error codes is
    otherwise told to fix a source/sink the rejected candidate configured
    correctly. Discovery tools and incremental mutations keep the default
    disclosure: there the standing state is what survives the rejection, and
    restricted surfaces read its errors through failure results (see the
    pipeline-state disclosure tests).
    """

    def test_failure_result_withholds_state_entries_only_when_asked(self) -> None:
        from elspeth.web.composer.tools._common import _failure_result

        withheld = _failure_result(
            _empty_state(),
            "Node 'enrich': Invalid options for transform 'llm': provider: Field required",
            error_code="plugin_options_invalid",
            with_state_validation=False,
        )

        assert not withheld.validation.is_valid
        assert [entry.component for entry in withheld.validation.errors] == ["rejected_mutation"]
        assert [entry.error_code for entry in withheld.validation.errors] == ["plugin_options_invalid"]
        assert withheld.validation.warnings == ()
        assert withheld.validation.suggestions == ()

        disclosed = _failure_result(
            _empty_state(),
            "Component 'missing-component' not found.",
        )
        disclosed_codes = {entry.error_code for entry in disclosed.validation.errors}
        assert {"no_source_configured", "no_sinks_configured"} <= disclosed_codes

    def test_normalization_does_not_reattach_withheld_stale_state_errors(self) -> None:
        from elspeth.web.catalog.policy_view import PolicyCatalogView
        from elspeth.web.composer.tools._common import _failure_result, normalize_tool_result_validation
        from elspeth.web.dependencies import create_catalog_service
        from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

        catalog_service = create_catalog_service()
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog_service)
        catalog = PolicyCatalogView.for_trained_operator(catalog_service, snapshot)

        result = _failure_result(
            _empty_state(),
            "Node 'enrich': Invalid options for transform 'llm': provider: Field required",
            error_code="plugin_options_invalid",
            with_state_validation=False,
        )
        normalized = normalize_tool_result_validation(result, catalog)

        assert [entry.component for entry in normalized.validation.errors] == ["rejected_mutation"]


def _make_gate(id: str, input: str, fork_to: tuple[str, ...]) -> NodeSpec:
    return NodeSpec(
        id=id,
        node_type="gate",
        plugin=None,
        input=input,
        on_success=None,
        on_error=None,
        options={},
        condition="'all'",
        routes={"all": "fork"},
        fork_to=fork_to,
        branches=None,
        policy=None,
        merge=None,
    )


def _make_coalesce(id: str, branches: Any) -> NodeSpec:
    return NodeSpec(
        id=id,
        node_type="coalesce",
        plugin=None,
        input="branches",
        on_success=None,
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=branches,
        policy="require_all",
        merge="union",
    )


def _orphaned_coalesce_state(branches: Any) -> CompositionState:
    """Fork/coalesce pipeline whose branch transforms bypass the coalesce.

    Reconstruction of guided session 277fb6c4 (attempts 3/6/9/10, 2026-07-22):
    the per-branch transforms publish straight to the sink — legal in
    isolation, so no companion code fires — leaving the coalesce's branches
    values naming connections nothing produces. The ONLY rejection is
    ``coalesce_branch_unreachable``, exactly matching the observed
    single-code per-attempt trail.
    """
    state = _empty_state()
    state = state.with_source(_make_source(on_success="rows"))
    state = state.with_node(_make_gate("fan_out", "rows", ("branch_a", "branch_b")))
    state = state.with_node(_make_transform("t_a", "branch_a", "main"))
    state = state.with_node(_make_transform("t_b", "branch_b", "main"))
    state = state.with_node(_make_coalesce("merge", branches))
    state = state.with_node(_make_transform("tidy", "merge", "main"))
    state = state.with_output(_make_output())
    return state


class TestCoalesceReachabilityFacts:
    """The coalesce reachability rejection carries instance wiring facts.

    Guided session 277fb6c4 died REPAIR_EXHAUSTED on four identical
    ``coalesce_branch_unreachable`` rejections: the static guidance directs
    the repair at the coalesce node, but the observed miswiring lives in the
    branch transforms' ``on_success`` — a repair the planner cannot find
    from a bare code. These facts name each unreachable branches value and
    the connections the pipeline actually produces (node ids and connection
    names the planner itself authored — never user row content).
    """

    def test_orphaned_coalesce_rejects_with_the_single_observed_code(self) -> None:
        state = _orphaned_coalesce_state({"branch_a": "a_done", "branch_b": "b_done"})
        result = state.validate()
        assert not result.is_valid
        assert [e.error_code for e in result.errors] == ["coalesce_branch_unreachable"]

    def test_reachability_facts_name_unreachable_pairs_and_produced_connections(self) -> None:
        from elspeth.web.composer.state import coalesce_reachability_facts

        state = _orphaned_coalesce_state({"branch_a": "a_done", "branch_b": "b_done"})
        facts = coalesce_reachability_facts(state)
        assert facts == {
            "merge": {
                "unreachable_branches": {"branch_a": "a_done", "branch_b": "b_done"},
                # Sink names and the coalesce's own published id are excluded:
                # both pass the membership walk today but are not connections a
                # branch value should be steered toward.
                "produced_connections": ["branch_a", "branch_b", "rows"],
                # The lure, named: each unreachable branch whose branch-side
                # transform publishes to a SINK instead of the expected
                # connection (guided attempt 14, session 04200b45 — the model
                # wired branch transforms to the reviewed sink 3x with the
                # bare facts live).
                "sink_targeting_branches": [
                    {"node_id": "t_a", "on_success_sink": "main", "expected_connection": "a_done"},
                    {"node_id": "t_b", "on_success_sink": "main", "expected_connection": "b_done"},
                ],
            }
        }

    def test_reachability_facts_handle_list_form_branches(self) -> None:
        from elspeth.web.composer.state import coalesce_reachability_facts

        state = _orphaned_coalesce_state(("a_done", "b_done"))
        facts = coalesce_reachability_facts(state)
        # List-form branch keys are the arriving connection names themselves —
        # nothing consumes them as an input, so no branch-side transform chain
        # exists to attribute a sink lure to.
        assert facts == {
            "merge": {
                "unreachable_branches": {"a_done": "a_done", "b_done": "b_done"},
                "produced_connections": ["branch_a", "branch_b", "rows"],
            }
        }

    def test_sink_lure_attribution_follows_transform_chains_and_skips_non_sink_dangles(self) -> None:
        """The lure walk follows a branch's transform CHAIN to the sink hop.

        branch_a: t_a -> x_mid -> t_mid -> main (sink): the transform to
        repair is t_mid, the chain's sink-publishing hop. branch_b: t_b
        publishes a dangling non-sink name — unreachable, but not the sink
        lure, so no attribution entry.
        """
        from elspeth.web.composer.state import coalesce_reachability_facts

        state = _empty_state()
        state = state.with_source(_make_source(on_success="rows"))
        state = state.with_node(_make_gate("fan_out", "rows", ("branch_a", "branch_b")))
        state = state.with_node(_make_transform("t_a", "branch_a", "x_mid"))
        state = state.with_node(_make_transform("t_mid", "x_mid", "main"))
        state = state.with_node(_make_transform("t_b", "branch_b", "b_dangle"))
        state = state.with_node(_make_coalesce("merge", {"branch_a": "a_done", "branch_b": "b_done"}))
        state = state.with_node(_make_transform("tidy", "merge", "main"))
        state = state.with_output(_make_output())
        facts = coalesce_reachability_facts(state)
        assert facts["merge"]["sink_targeting_branches"] == [
            {"node_id": "t_mid", "on_success_sink": "main", "expected_connection": "a_done"},
        ]

    def test_reachability_facts_empty_for_correctly_wired_coalesce(self) -> None:
        from elspeth.web.composer.state import coalesce_reachability_facts

        state = _empty_state()
        state = state.with_source(_make_source(on_success="rows"))
        state = state.with_node(_make_gate("fan_out", "rows", ("branch_a", "branch_b")))
        state = state.with_node(_make_transform("t_a", "branch_a", "a_done"))
        state = state.with_node(_make_transform("t_b", "branch_b", "b_done"))
        state = state.with_node(_make_coalesce("merge", {"branch_a": "a_done", "branch_b": "b_done"}))
        state = state.with_node(_make_transform("tidy", "merge", "main"))
        state = state.with_output(_make_output())
        assert state.validate().is_valid
        assert coalesce_reachability_facts(state) == {}

    def test_allowlisted_feedback_projects_connectivity_facts(self) -> None:
        from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback
        from elspeth.web.composer.tools import ToolResult

        state = _orphaned_coalesce_state({"branch_a": "a_done", "branch_b": "b_done"})
        result = ToolResult(
            success=False,
            updated_state=state,
            validation=state.validate(),
            affected_nodes=(),
        )
        feedback = _allowlisted_candidate_feedback(result)
        projected = feedback["validation"]["errors"][0]
        assert projected["error_code"] == "coalesce_branch_unreachable"
        assert "message" not in projected
        assert projected["connectivity"] == {
            "unreachable_branches": {"branch_a": "a_done", "branch_b": "b_done"},
            "produced_connections": ["branch_a", "branch_b", "rows"],
            "sink_targeting_branches": [
                {"node_id": "t_a", "on_success_sink": "main", "expected_connection": "a_done"},
                {"node_id": "t_b", "on_success_sink": "main", "expected_connection": "b_done"},
            ],
        }

    def test_allowlisted_feedback_omits_connectivity_for_other_codes(self) -> None:
        from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback
        from elspeth.web.composer.tools import ToolResult

        entry = ValidationEntry(
            component="node:merge",
            message="anything",
            severity="high",
            error_code="coalesce_missing_branches",
        )
        result = ToolResult(
            success=False,
            updated_state=_empty_state(),
            validation=ValidationSummary(is_valid=False, errors=(entry,), warnings=(), suggestions=()),
            affected_nodes=(),
        )
        feedback = _allowlisted_candidate_feedback(result)
        assert "connectivity" not in feedback["validation"]["errors"][0]


class TestPluginUnavailabilityFamilyIsExplainable:
    """Every plugin-unavailability reason must resolve to actionable guidance.

    ``_plugin_policy_failure`` emits each ``PluginUnavailableReason`` value as a
    tool ``error_code``, so any of them can reach the planner's redacted repair
    feedback — where the code is the ONLY surviving signal. Until this sweep the
    whole family resolved to nothing through ``explain_validation_code``: the
    model saw a bare token like ``credential_unavailable`` with no way to learn
    whether to pick a different plugin, wait for an operator, or stop trying, and
    so re-emitted the same rejected selection until its budget ran out. Same
    failure shape as the coded-but-unexplained rejections the rest of this module
    pins, one layer up.
    """

    def test_every_reason_is_in_the_closed_catalogue_and_resolves(self) -> None:
        for reason in PluginUnavailableReason:
            assert reason.value in _CLOSED_VALIDATION_ERROR_CODES, reason
            guidance = explain_validation_code(reason.value)
            assert guidance is not None, f"{reason.value} does not resolve to catalogue guidance"
            explanation, fix = guidance
            assert explanation and fix

    def test_explanations_are_reused_from_the_tool_copy_not_restated(self) -> None:
        """One source of truth: the tool failure and the explain entry cannot drift.

        The tool's own message already carries a plain-language cause per reason
        (``_PLUGIN_UNAVAILABLE_EXPLANATIONS``). Transcribing it into the explain
        catalogue would let the two answers to "why can't I use this plugin?"
        diverge silently, which is how a model ends up told to repair something
        an operator must fix (or vice versa).
        """
        for reason in PluginUnavailableReason:
            guidance = explain_validation_code(reason.value)
            assert guidance is not None
            explanation, _fix = guidance
            assert _PLUGIN_UNAVAILABLE_EXPLANATIONS[reason] in explanation, reason

    def test_fix_table_is_total_over_the_enum(self) -> None:
        """A new reason joins both halves or fails at import, never half-wired."""
        assert set(_PLUGIN_UNAVAILABLE_FIXES) == set(PluginUnavailableReason)
        assert all(_PLUGIN_UNAVAILABLE_FIXES[reason].strip() for reason in PluginUnavailableReason)

    def test_web_surface_prohibition_tells_the_planner_the_refusal_is_categorical(self) -> None:
        """The one reason with NO repair must say so, or the budget burns.

        Every other reason names something an operator could change.
        ``WEB_SURFACE_PROHIBITED`` names something nothing can change, so the fix
        must forbid re-emission outright rather than suggest another attempt.
        """
        guidance = explain_validation_code(PluginUnavailableReason.WEB_SURFACE_PROHIBITED.value)
        assert guidance is not None
        _explanation, fix = guidance
        lowered = fix.lower()

        assert "categorical" in lowered
        assert "do not re-emit" in lowered

    def test_reason_codes_do_not_shadow_an_unrelated_catalogue_entry(self) -> None:
        """Exact-code patterns only: these codes are short and generic.

        A loose alternation here would make an unrelated full validation message
        resolve to a plugin-policy explanation. Every other closed code must keep
        resolving to its own guidance with the family added.
        """
        family = {reason.value for reason in PluginUnavailableReason}
        for code in _CLOSED_VALIDATION_ERROR_CODES:
            if code in family:
                continue
            guidance = explain_validation_code(code)
            assert guidance is not None, code
            explanation, _fix = guidance
            assert "cannot be used in this deployment" not in explanation, code


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
