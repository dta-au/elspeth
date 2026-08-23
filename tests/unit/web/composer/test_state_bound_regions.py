"""Stage-1 mirrors for whole-roster fork closure (spec §7 rule 2, ruling 23)
and the E2 unbound consumer-fed fork-branch destination (spec §7 E2).

NEW FILE — test_state.py is under active maintainer edit; do not add to it
(see task-6-brief.md). Construction idiom copied from
``TestStage1Validation`` in test_state.py (``_empty_state``/``_fork_gate``/
coalesce ``NodeSpec`` shapes), not imported from it, since these are the
same small builders repeated file-locally throughout that suite.
"""

from __future__ import annotations

from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _make_source(on_success: str) -> SourceSpec:
    return SourceSpec(plugin="csv", on_success=on_success, options={}, on_validation_failure="discard")


def _make_output(name: str) -> OutputSpec:
    return OutputSpec(name=name, plugin="csv", options={}, on_write_failure="discard")


def _fork_gate(node_id: str, input_name: str, fork_to: tuple[str, ...]) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="gate",
        plugin=None,
        input=input_name,
        on_success=None,
        on_error=None,
        options={},
        condition="True",
        routes={"true": "fork", "false": "fork"},
        fork_to=fork_to,
        branches=None,
        policy=None,
        merge=None,
    )


def _transform(node_id: str, input_name: str, on_success: str) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="passthrough",
        input=input_name,
        on_success=on_success,
        on_error="discard",
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _routing_gate(node_id: str, input_name: str, routes: dict[str, str]) -> NodeSpec:
    """A plain (non-forking) routing gate — no ``fork_to``."""
    return NodeSpec(
        id=node_id,
        node_type="gate",
        plugin=None,
        input=input_name,
        on_success=None,
        on_error=None,
        options={},
        condition="True",
        routes=routes,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _aggregation(node_id: str, input_name: str, on_success: str, *, output_mode: str = "transform") -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="aggregation",
        plugin="batch_stats",
        input=input_name,
        on_success=on_success,
        on_error="discard",
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        trigger={},
        output_mode=output_mode,
    )


def _row_union(node_id: str, *, branches: dict[str, str], on_success: str | None) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="row_union",
        plugin=None,
        # NodeSpec.input is only a serialization placeholder for a
        # row_union; it must equal the first declared branch connection
        # (confirmed by reproduction against the real validate() error
        # message before writing this helper).
        input=next(iter(branches.values())),
        on_success=on_success,
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=branches,
        policy=None,
        merge=None,
    )


def _coalesce(node_id: str, *, branches: dict[str, str], on_success: str | None) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="coalesce",
        plugin=None,
        input="join",
        on_success=on_success,
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=branches,
        policy="require_all",
        merge="union",
    )


class TestWholeRosterForkClosureStage1Mirror:
    """Spec §7 rule 2 (ruling 23): a fork closes entirely at ONE closer or not at all.

    Mirrors ``TestWholeRosterForkClosure`` in
    ``tests/unit/core/dag/test_builder_validation.py`` — same four shapes,
    checked against the composer's Stage-1 ``validate()`` codes rather than
    the DAG builder's ``GraphValidationError``.
    """

    def test_mixed_fork_closure_is_rejected_with_code(self) -> None:
        # fork_to [failing, survivor]: 'failing' is a coalesce branch alias,
        # 'survivor' matches a sink name — mixed closure.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("failing", "survivor")))
        state = state.with_node(_coalesce("c", branches={"failing": "failing", "second": "second"}, on_success="out"))
        state = state.with_output(_make_output("survivor"))
        state = state.with_output(_make_output("out"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "fork_mixed_closure_invalid" in codes, summary.errors

    def test_pure_fan_out_fork_validates_green(self) -> None:
        # BOTH branches direct to sinks, no closer anywhere: "fully unbound
        # (pure fan-out)" — LEGAL under rule 2, positive control.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("left", "right")))
        state = state.with_output(_make_output("left"))
        state = state.with_output(_make_output("right"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "fork_mixed_closure_invalid" not in codes, summary.errors
        assert "fork_multiple_closers_invalid" not in codes, summary.errors
        assert "fork_roster_mismatch" not in codes, summary.errors

    def test_fork_split_across_two_closers_is_rejected_with_code(self) -> None:
        # The parallel-coalesces shape: ONE fork closing at TWO sibling coalesces.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("left_a", "left_b", "right_a", "right_b")))
        state = state.with_node(_coalesce("merge_left", branches={"left_a": "left_a", "left_b": "left_b"}, on_success="out"))
        state = state.with_node(_coalesce("merge_right", branches={"right_a": "right_a", "right_b": "right_b"}, on_success="out"))
        state = state.with_output(_make_output("out"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "fork_multiple_closers_invalid" in codes, summary.errors

    def test_closer_roster_mismatch_is_rejected_with_code(self) -> None:
        # Closer declares a strict SUPERSET of the fork's branches -> mismatch.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("path_a", "path_b")))
        state = state.with_node(_coalesce("c", branches={"path_a": "path_a", "path_b": "path_b", "path_c": "path_c"}, on_success="out"))
        state = state.with_output(_make_output("out"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "fork_roster_mismatch" in codes, summary.errors

    def test_fully_bound_matching_roster_validates_green(self) -> None:
        # Positive control: fully bound, roster-equal — LEGAL under rule 2.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("path_a", "path_b")))
        state = state.with_node(_coalesce("c", branches={"path_a": "path_a", "path_b": "path_b"}, on_success="out"))
        state = state.with_output(_make_output("out"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "fork_mixed_closure_invalid" not in codes, summary.errors
        assert "fork_multiple_closers_invalid" not in codes, summary.errors
        assert "fork_roster_mismatch" not in codes, summary.errors


class TestUnboundConsumerFedForkBranchStage1Mirror:
    """E2: a fork branch consumed by exactly one downstream transform/gate is
    a legal unbound (pure-fan-out) destination — mirrors the DAG builder's
    fourth destination path in ``_validate_runtime_route_destinations`` and
    the WHOLE-ROSTER FORK CLOSURE block's unbound classification."""

    def test_branch_feeding_a_transform_with_no_barrier_or_sink_validates_green(self) -> None:
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("explode_path", "control")))
        state = state.with_node(_transform("consumer", "explode_path", "exploded"))
        state = state.with_output(_make_output("control"))
        state = state.with_output(_make_output("exploded"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "fork_branch_no_destination" not in codes, summary.errors

    def test_branch_feeding_two_consumers_reports_duplicate_consumer_not_no_destination(self) -> None:
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("shared", "other")))
        state = state.with_node(_transform("t1", "shared", "out"))
        state = state.with_node(_transform("t2", "shared", "out"))
        state = state.with_output(_make_output("out"))
        state = state.with_output(_make_output("other"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "duplicate_connection_consumer" in codes, summary.errors
        assert "fork_branch_no_destination" not in codes, summary.errors

    def test_mixed_bound_and_consumer_fed_stays_mixed_closure_rejected(self) -> None:
        # rule 2 interplay: a consumer-fed (unbound) branch mixed with a
        # closer-bound branch on the SAME gate is still mixed closure.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("free_path", "bound_path")))
        state = state.with_node(_transform("consumer", "free_path", "out"))
        state = state.with_node(_coalesce("c", branches={"bound_path": "bound_path", "second": "second"}, on_success="out"))
        state = state.with_output(_make_output("out"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "fork_mixed_closure_invalid" in codes, summary.errors

    def test_orphan_fork_branch_still_has_no_destination(self) -> None:
        # E2 adds a fourth legal destination (an ordinary consumer); it does
        # NOT make every branch legal. "nowhere" matches no coalesce/row_union
        # alias, no sink, and is consumed by nothing — still a genuine
        # no-destination defect (the original incident this code protects,
        # live session d4ea3d8a). "high" matches a sink directly, so both
        # branches are unbound under rule 2 — no mixed-closure noise, an
        # isolated fork_branch_no_destination pin.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("high", "nowhere")))
        state = state.with_output(_make_output("high"))

        summary = state.validate()

        offending = [e for e in summary.errors if e.error_code == "fork_branch_no_destination"]
        assert len(offending) == 1, summary.errors
        assert "nowhere" in offending[0].message
        assert "fork_mixed_closure_invalid" not in {e.error_code for e in summary.errors}, summary.errors


class TestBoundRegionSinkInsideStage1Mirror:
    """Spec §7 rule 4, SINK-INSIDE limb only (the backward walk and no-path
    limb are Stage-1 abstentions — see generation.py's parity note).

    A whole-roster-bound fork (rule 2 passes) whose branch chain contains a
    plain routing gate with its OWN route straight to a sink is still a leak
    out of the bound region: rule 2 only checks branch ALIAS membership, not
    what an inner gate's other routes do.
    """

    def test_inner_gate_route_to_sink_is_rejected_with_code(self) -> None:
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("path_a", "path_b")))
        state = state.with_node(_routing_gate("screen", "path_a", {"pass": "path_a_out", "fail": "leak"}))
        state = state.with_node(_transform("t_b", "path_b", "path_b_out"))
        state = state.with_node(_coalesce("c", branches={"path_a": "path_a_out", "path_b": "path_b_out"}, on_success="out"))
        state = state.with_output(_make_output("out"))
        state = state.with_output(_make_output("leak"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "bound_region_sink_inside" in codes, summary.errors
        offending = next(e for e in summary.errors if e.error_code == "bound_region_sink_inside")
        assert "leak" in offending.message

    def test_fully_in_region_chain_validates_green(self) -> None:
        # Positive control: same shape, but the inner gate's other route
        # stays in-region (converges on the same connection) — no leak.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("path_a", "path_b")))
        state = state.with_node(_routing_gate("screen", "path_a", {"pass": "path_a_out", "fail": "path_a_out"}))
        state = state.with_node(_transform("t_b", "path_b", "path_b_out"))
        state = state.with_node(_coalesce("c", branches={"path_a": "path_a_out", "path_b": "path_b_out"}, on_success="out"))
        state = state.with_output(_make_output("out"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "bound_region_sink_inside" not in codes, summary.errors

    def test_leak_via_opener_non_roster_route_is_rejected(self) -> None:
        # Review F1 (2026-08-23 fix round): the FORK gate's own non-roster
        # route ('false': 'side_conn') feeds an intermediate non-fork gate
        # that re-enters the region before leaking to a sink. The
        # roster-only seed missed this entirely (the edge fork_gate ->
        # side_screen carries label='false', not a roster branch name) —
        # both the runtime and the mirror shared this blind spot.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(
            NodeSpec(
                id="g",
                node_type="gate",
                plugin=None,
                input="g_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "side_conn"},
                fork_to=("path_a", "path_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(_routing_gate("side_screen", "side_conn", {"pass": "path_a_out", "fail": "leak"}))
        state = state.with_node(_transform("t_b", "path_b", "path_b_out"))
        state = state.with_node(_coalesce("c", branches={"path_a": "path_a_out", "path_b": "path_b_out"}, on_success="out"))
        state = state.with_output(_make_output("out"))
        state = state.with_output(_make_output("leak"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "bound_region_sink_inside" in codes, summary.errors
        offending = next(e for e in summary.errors if e.error_code == "bound_region_sink_inside")
        assert "leak" in offending.message


class TestBoundRegionAggregationInvalidStage1Mirror:
    """Spec §7 rule 6 (ruling 25): aggregators banned inside all bound
    regions, both output modes — mirrors
    ``TestRule6AggregatorBan`` in ``tests/unit/core/dag/test_bound_regions.py``,
    checked against the composer's Stage-1 ``validate()`` codes. Keyed to
    coalesce-bound branches only: scope/collector regions are not yet
    composer-authorable (Task 12). The row_union twin
    (``row_union_branch_aggregation_invalid``) stays for its narrower
    transform-mode-only shape and is not superseded by this rule.
    """

    def test_aggregation_inside_coalesce_branch_rejected_both_modes(self) -> None:
        for output_mode in ("transform", "passthrough"):
            state = _empty_state()
            state = state.with_source(_make_source("g_in"))
            state = state.with_node(_fork_gate("g", "g_in", ("path_a", "path_b")))
            state = state.with_node(_aggregation("agg", "path_a", "path_a_out", output_mode=output_mode))
            state = state.with_node(_transform("t_b", "path_b", "path_b_out"))
            state = state.with_node(_coalesce("c", branches={"path_a": "path_a_out", "path_b": "path_b_out"}, on_success="out"))
            state = state.with_output(_make_output("out"))

            summary = state.validate()

            codes = {e.error_code for e in summary.errors}
            assert "bound_region_aggregation_invalid" in codes, (output_mode, summary.errors)
            offending = next(e for e in summary.errors if e.error_code == "bound_region_aggregation_invalid")
            assert "agg" in offending.message

    def test_aggregation_after_coalesce_release_stays_legal(self) -> None:
        # Positive control: the aggregation consumes the coalesce's OWN
        # release connection — strictly after the group settles. No roster
        # is watching past the closer (ADR-020 posture) — unchanged.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("path_a", "path_b")))
        state = state.with_node(_transform("t_a", "path_a", "path_a_out"))
        state = state.with_node(_transform("t_b", "path_b", "path_b_out"))
        state = state.with_node(_coalesce("c", branches={"path_a": "path_a_out", "path_b": "path_b_out"}, on_success="c_out"))
        state = state.with_node(_aggregation("agg", "c_out", "out"))
        state = state.with_output(_make_output("out"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "bound_region_aggregation_invalid" not in codes, summary.errors

    def test_top_level_aggregation_stays_legal(self) -> None:
        # Positive control: no fork/coalesce anywhere — no roster is watching.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_aggregation("agg", "g_in", "out"))
        state = state.with_output(_make_output("out"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "bound_region_aggregation_invalid" not in codes, summary.errors


class TestBoundRegionAggregationInvalidRowUnionStage1Mirror:
    """Extends the coalesce-side mirror to row_union regions too (maintainer
    ruling 2026-08-23, WS2 controller): the A3 divergence check for Task 9
    found that the pre-existing row_union twin
    (``row_union_branch_aggregation_invalid``) only flags output_mode:
    transform, so a PASSTHROUGH-mode aggregation feeding a row_union
    validated GREEN in Stage 1 while the runtime rule (ruling 25) rejected
    it. This closes that gap rather than shipping it as a known divergence
    — the twin stays untouched and fires FIRST for the transform-mode
    overlap shape (state.py's own code comment states the ordering
    guarantee this class pins).
    """

    def test_passthrough_mode_aggregation_in_row_union_branch_is_now_mirrored(self) -> None:
        # The exact shape the A3 divergence check reproduced as validate-green
        # before this fix — now closed.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("path_a", "path_b")))
        state = state.with_node(_aggregation("agg", "path_a", "path_a_out", output_mode="passthrough"))
        state = state.with_node(_transform("t_b", "path_b", "path_b_out"))
        state = state.with_node(_row_union("u", branches={"path_a": "path_a_out", "path_b": "path_b_out"}, on_success="union_out"))
        state = state.with_node(_transform("after", "union_out", "out"))
        state = state.with_output(_make_output("out"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "bound_region_aggregation_invalid" in codes, summary.errors
        assert "row_union_branch_aggregation_invalid" not in codes, summary.errors

    def test_transform_mode_overlap_reports_the_specific_twin_message_first(self) -> None:
        # A transform-mode aggregation in a row_union branch trips BOTH
        # checks (both rejections are correct for this node) — pin that the
        # twin's specific, more actionable message is FIRST in errors, not a
        # coin flip.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("path_a", "path_b")))
        state = state.with_node(_aggregation("agg", "path_a", "path_a_out", output_mode="transform"))
        state = state.with_node(_transform("t_b", "path_b", "path_b_out"))
        state = state.with_node(_row_union("u", branches={"path_a": "path_a_out", "path_b": "path_b_out"}, on_success="union_out"))
        state = state.with_node(_transform("after", "union_out", "out"))
        state = state.with_output(_make_output("out"))

        summary = state.validate()

        codes = [
            e.error_code
            for e in summary.errors
            if e.error_code in ("row_union_branch_aggregation_invalid", "bound_region_aggregation_invalid")
        ]
        assert codes == ["row_union_branch_aggregation_invalid", "bound_region_aggregation_invalid"], summary.errors

    def test_aggregation_after_row_union_release_stays_legal(self) -> None:
        # Positive control: the aggregation consumes the union's OWN release
        # connection — strictly after the group settles.
        state = _empty_state()
        state = state.with_source(_make_source("g_in"))
        state = state.with_node(_fork_gate("g", "g_in", ("path_a", "path_b")))
        state = state.with_node(_transform("t_a", "path_a", "path_a_out"))
        state = state.with_node(_transform("t_b", "path_b", "path_b_out"))
        state = state.with_node(_row_union("u", branches={"path_a": "path_a_out", "path_b": "path_b_out"}, on_success="union_out"))
        state = state.with_node(_aggregation("agg", "union_out", "out"))
        state = state.with_output(_make_output("out"))

        summary = state.validate()

        codes = {e.error_code for e in summary.errors}
        assert "bound_region_aggregation_invalid" not in codes, summary.errors
