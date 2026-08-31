"""Tests for the source data-contract demand backtrace (elspeth-da68332faf).

The demand set is DERIVED by delta-running Stage-1 validation's own
edge-contract ledger, so these tests pin the derivation's guarantees:
minimality (a field no consumer requires never appears), transparency (the
demand reaches the source through pass-through nodes), and honest abstention
(a source that cannot carry a guarantee stamp yields no demand).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.source_demand import (
    SOURCE_DATA_CONTRACT_DRAFT_VERSION,
    SOURCE_DATA_CONTRACT_USER_TERM,
    backtraced_source_demand,
    build_source_data_contract_draft,
    parse_source_data_contract_accepted_fields,
    sample_header_for_source,
    source_data_contract_artifact_hash,
    stamp_source_options_with_guarantees,
)
from elspeth.web.composer.state import SOURCE_AUTHORING_KEY, CompositionState, NodeSpec, PipelineMetadata, SourceSpec


def _llm_node(
    *,
    node_id: str = "rate",
    input_name: str = "source",
    on_success: str = "rated",
    required: list[str] | None = None,
    extra_options: dict[str, Any] | None = None,
) -> NodeSpec:
    options: dict[str, Any] = {
        "prompt_template": "Rate {{ row.colour }}",
        "model": "gpt-test",
        "schema": {"mode": "observed"},
    }
    if required is not None:
        options["required_input_fields"] = required
    if extra_options:
        options.update(extra_options)
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="llm",
        input=input_name,
        on_success=on_success,
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state(
    source_options: dict[str, Any],
    nodes: tuple[NodeSpec, ...],
) -> CompositionState:
    return CompositionState(
        source=None,
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="source",
                options=source_options,
                on_validation_failure="discard",
            )
        },
        nodes=nodes,
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


class TestBacktracedSourceDemand:
    def test_consumer_required_field_is_demanded_of_the_source(self) -> None:
        state = _state({"path": "/tmp/upload.csv"}, (_llm_node(required=["colour"]),))
        assert backtraced_source_demand(state, "source") == ("colour",)

    def test_field_no_consumer_requires_never_appears(self) -> None:
        # Only 'colour' is required anywhere; nothing else may be demanded,
        # whatever the file's header happens to carry.
        state = _state({"path": "/tmp/upload.csv"}, (_llm_node(required=["colour"]),))
        demand = backtraced_source_demand(state, "source")
        assert demand == ("colour",)
        assert "extra" not in demand

    def test_no_demand_when_no_consumer_requires_fields(self) -> None:
        state = _state({"path": "/tmp/upload.csv"}, (_llm_node(required=None),))
        assert backtraced_source_demand(state, "source") == ()

    def test_demand_flows_through_a_pass_through_chain(self) -> None:
        # source -> llm (pass-through) -> llm requiring 'colour': the demand
        # resolves through the transparent walk back to the source. The
        # intermediate prompt references no row fields (with the explicit
        # empty opt-out) so the only requirement anywhere is the consumer's.
        first = _llm_node(
            node_id="summarise",
            input_name="source",
            on_success="summaries",
            required=[],
            extra_options={"prompt_template": "Summarise this."},
        )
        second = _llm_node(node_id="rate", input_name="summaries", on_success="rated", required=["colour"])
        state = _state({"path": "/tmp/upload.csv"}, (first, second))
        assert backtraced_source_demand(state, "source") == ("colour",)

    def test_missing_source_yields_no_demand(self) -> None:
        state = _state({"path": "/tmp/upload.csv"}, (_llm_node(required=["colour"]),))
        assert backtraced_source_demand(state, "no_such_source") == ()

    def test_unstampable_source_schema_yields_no_demand(self) -> None:
        # An explicit schema.fields declaration is the author's own complete
        # claim; the ask flow never rewrites it, so no demand may be staged.
        options = {
            "path": "/tmp/upload.csv",
            "schema": {"mode": "fixed", "fields": ["colour: str"]},
        }
        state = _state(options, (_llm_node(required=["colour"]),))
        assert backtraced_source_demand(state, "source") == ()

    def test_disregard_fields_reopens_demand_behind_a_stamp(self) -> None:
        # With the stamp in place the edge is satisfied; stripping the
        # acknowledged field recomputes the true graph demand.
        options = {
            "path": "/tmp/upload.csv",
            "schema": {"mode": "observed", "guaranteed_fields": ["colour"]},
        }
        state = _state(options, (_llm_node(required=["colour"]),))
        assert backtraced_source_demand(state, "source") == ()
        assert backtraced_source_demand(state, "source", disregard_fields=frozenset({"colour"})) == ("colour",)


def _queue_node(node_id: str = "q") -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="queue",
        plugin=None,
        input=node_id,
        on_success=None,
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _csv_source(options: dict[str, Any], *, on_success: str = "q") -> SourceSpec:
    return SourceSpec(plugin="csv", on_success=on_success, options=options, on_validation_failure="discard")


def _observed(*guaranteed: str) -> dict[str, Any]:
    return {"path": "/tmp/upload.csv", "schema": {"mode": "observed", "guaranteed_fields": list(guaranteed)}}


def _fan_in_state(
    sources: dict[str, SourceSpec],
    nodes: tuple[NodeSpec, ...] = (),
    *,
    required: list[str] | None = None,
) -> CompositionState:
    return CompositionState(
        source=None,
        sources=sources,
        nodes=(
            _queue_node(),
            _llm_node(input_name="q", required=required if required is not None else ["colour"]),
            *nodes,
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


class TestFanInDemandAttribution:
    """Queue fan-in is an AND over N independent per-source promises.

    Ruling on elspeth-da68332faf: every released row comes from exactly one
    arm, so a consumer requirement must be promised by EVERY feeding source,
    each for its own rows (Stage-1 already intersects arm votes). The demand
    walk attributes a field to a source iff the miss clears when every
    card-eligible source is stamped (sufficiency) and does NOT clear when
    only the others are (necessity). The row_union vote is the same
    intersection arm of ``_producer_entry_propagation_vote``; the queue is
    the sanctioned multi-source fan-in point and carries these pins.
    """

    def test_fan_in_requirement_is_demanded_of_every_feeding_source(self) -> None:
        state = _fan_in_state({"src_a": _csv_source(_observed("id")), "src_b": _csv_source(_observed("id"))})
        assert backtraced_source_demand(state, "src_a") == ("colour",)
        assert backtraced_source_demand(state, "src_b") == ("colour",)

    def test_stamping_every_source_clears_validation_but_one_alone_does_not(self) -> None:
        # The demand is honest: acknowledging BOTH cards satisfies the edge,
        # acknowledging one alone leaves the intersection short — the AND the
        # attribution promises the user is the AND validation enforces.
        state = _fan_in_state({"src_a": _csv_source(_observed("id")), "src_b": _csv_source(_observed("id"))})

        def _stamped(state: CompositionState, names: tuple[str, ...]) -> CompositionState:
            sources = dict(state.sources)
            for name in names:
                stamped_options = stamp_source_options_with_guarantees(sources[name].options, ["colour"])
                assert stamped_options is not None
                sources[name] = SourceSpec(
                    plugin=sources[name].plugin,
                    on_success=sources[name].on_success,
                    options=stamped_options,
                    on_validation_failure=sources[name].on_validation_failure,
                )
            return CompositionState(
                source=None,
                sources=sources,
                nodes=state.nodes,
                edges=state.edges,
                outputs=state.outputs,
                metadata=state.metadata,
                version=state.version,
            )

        def _unsatisfied(state: CompositionState) -> list[tuple[str, str, tuple[str, ...]]]:
            return [(c.from_id, c.to_id, c.missing_fields) for c in state.validate().edge_contracts if not c.satisfied]

        assert _unsatisfied(state) == [("q", "rate", ("colour",))]
        assert _unsatisfied(_stamped(state, ("src_a",))) == [("q", "rate", ("colour",))]
        assert _unsatisfied(_stamped(state, ("src_a", "src_b"))) == []

    def test_source_not_feeding_the_edge_is_never_asked(self) -> None:
        # Necessity: src_c is card-eligible but feeds its own edge; the fan-in
        # miss clears without its promise, so nothing lands on its card.
        side_consumer = _llm_node(
            node_id="side_note",
            input_name="side",
            on_success="noted",
            required=[],
            extra_options={"prompt_template": "Summarise this."},
        )
        state = _fan_in_state(
            {
                "src_a": _csv_source(_observed("id")),
                "src_b": _csv_source(_observed("id")),
                "src_c": _csv_source(_observed("id"), on_success="side"),
            },
            nodes=(side_consumer,),
        )
        assert backtraced_source_demand(state, "src_a") == ("colour",)
        assert backtraced_source_demand(state, "src_c") == ()

    def test_ineligible_source_in_the_intersection_fails_closed_for_the_sibling_too(self) -> None:
        # src_b's declared schema.fields is the author's complete claim — it
        # cannot be stamped, so the fan-in miss can never clear and NO card
        # demands the field: the shape stays fail-closed at validation with
        # the ordinary edge-contract advice instead of staging a card whose
        # acknowledgement could not resolve the miss.
        declared = {"path": "/tmp/upload.csv", "schema": {"mode": "fixed", "fields": ["id: str"]}}
        state = _fan_in_state({"src_a": _csv_source(_observed("id")), "src_b": _csv_source(declared)})
        assert [c.missing_fields for c in state.validate().edge_contracts if not c.satisfied] == [("colour",)]
        assert backtraced_source_demand(state, "src_a") == ()
        assert backtraced_source_demand(state, "src_b") == ()

    def test_resolved_source_recomputes_its_own_demand_while_a_sibling_is_pending(self) -> None:
        # After src_a's card resolves (stamp in place), the disregard-strip
        # recompute must return src_a's OWN demand again — necessity holds
        # because src_a's rows still need the promise even though src_b is
        # unstamped — so the accepted artifact hash keeps matching and the
        # site enumerator keeps src_a's card closed while src_b's stays open.
        state = _fan_in_state({"src_a": _csv_source(_observed("colour", "id")), "src_b": _csv_source(_observed("id"))})
        assert backtraced_source_demand(state, "src_a", disregard_fields=frozenset({"colour"})) == ("colour",)
        assert backtraced_source_demand(state, "src_b") == ("colour",)

    def test_disregarding_the_only_guarantee_preserves_fan_in_participation(self) -> None:
        # A resolved card's accepted field is stripped before demand is
        # recomputed.  Stripping the last field must leave an EXPLICIT empty
        # vote, not turn the source into an abstainer: otherwise one abstaining
        # arm makes the queue abstain wholesale, erases the Stage-1 miss, and
        # hides both the accepted field and a newly required field.
        state = _fan_in_state(
            {
                "src_a": _csv_source(_observed("colour")),
                "src_b": _csv_source(_observed("colour")),
            },
            required=["colour", "size"],
        )

        assert backtraced_source_demand(
            state,
            "src_a",
            disregard_fields=frozenset({"colour"}),
        ) == ("colour", "size")

    def test_composer_authored_sibling_is_never_stamped_even_when_stampable(self) -> None:
        # src_b carries the composer-authored content marker: its guarantee
        # is content-derived (invented_source flow), never a card promise, so
        # the hypothesis must not stamp it — src_b gets no demand, and the
        # AND over the fan-in cannot clear without it, so src_a fails closed
        # exactly like the unstampable-sibling shape.
        authored = _observed("id")
        authored[SOURCE_AUTHORING_KEY] = {
            "modality": "llm_generated",
            "content_hash": "0" * 64,
            "review_event_id": None,
            "resolved_kind": None,
        }
        state = _fan_in_state({"src_a": _csv_source(_observed("id")), "src_b": _csv_source(authored)})
        assert backtraced_source_demand(state, "src_b") == ()
        assert backtraced_source_demand(state, "src_a") == ()

    def test_bare_observed_arms_leave_no_ledger_miss_and_no_demand(self) -> None:
        # When every arm abstains (no effective guarantees) Stage-1 skips the
        # edge with the honest "not yet checked" warning and records NO miss,
        # so there is no existence-precondition to attribute: the demand walk
        # derives from the ledger and stays empty. Runtime per-row
        # enforcement owns the shape — this pins the derivation's scope, not
        # a gap in it.
        state = _fan_in_state({"src_a": _csv_source({"path": "/tmp/a.csv"}), "src_b": _csv_source({"path": "/tmp/b.csv"})})
        result = state.validate()
        assert result.edge_contracts == ()
        assert any("Contract check skipped" in warning.message for warning in result.warnings)
        assert backtraced_source_demand(state, "src_a") == ()
        assert backtraced_source_demand(state, "src_b") == ()


class TestStampSourceOptions:
    def test_stamps_observed_schema_and_unions_existing(self) -> None:
        options = {"path": "/x.csv", "schema": {"mode": "observed", "guaranteed_fields": ["colour"]}}
        stamped = stamp_source_options_with_guarantees(options, ["size"])
        assert stamped is not None
        assert stamped["schema"]["guaranteed_fields"] == ["colour", "size"]
        assert stamped["schema"]["mode"] == "observed"

    def test_creates_observed_schema_when_absent(self) -> None:
        stamped = stamp_source_options_with_guarantees({"path": "/x.csv"}, ["colour"])
        assert stamped is not None
        assert stamped["schema"] == {"mode": "observed", "guaranteed_fields": ["colour"]}

    def test_abstains_on_declared_fields(self) -> None:
        options = {"schema": {"mode": "observed", "fields": ["colour: str"]}}
        assert stamp_source_options_with_guarantees(options, ["colour"]) is None

    def test_abstains_on_non_observed_mode(self) -> None:
        options = {"schema": {"mode": "flexible"}}
        assert stamp_source_options_with_guarantees(options, ["colour"]) is None

    def test_abstains_on_malformed_schema_block(self) -> None:
        assert stamp_source_options_with_guarantees({"schema": "nope"}, ["colour"]) is None


class TestDraftAndArtifact:
    def test_draft_round_trips_the_demand_set(self) -> None:
        draft = build_source_data_contract_draft(["size", "colour"], ("colour", "extra"))
        assert parse_source_data_contract_accepted_fields(draft) == ("colour", "size")

        payload = json.loads(draft)
        assert payload["contract_version"] == SOURCE_DATA_CONTRACT_DRAFT_VERSION == 2
        assert payload["kind"] == SOURCE_DATA_CONTRACT_USER_TERM

    def test_draft_warns_on_demanded_fields_the_sample_lacks(self) -> None:
        draft = build_source_data_contract_draft(["colour", "size"], ("colour", "extra"))
        assert '"missing_from_sample":["size"]' in draft

    def test_draft_without_sample_has_no_sample_warnings(self) -> None:
        draft = build_source_data_contract_draft(["colour"], None)
        assert '"sample_header":null' in draft
        assert '"missing_from_sample":[]' in draft

    def test_artifact_hash_binds_current_contract_semantics_and_field_set(self) -> None:
        # Order-insensitive over fields; sample evidence never participates.
        assert source_data_contract_artifact_hash(["b", "a"]) == source_data_contract_artifact_hash(["a", "b"])
        assert source_data_contract_artifact_hash(["a"]) != source_data_contract_artifact_hash(["a", "b"])
        legacy_v1_hash = stable_hash({"review_kind": SOURCE_DATA_CONTRACT_USER_TERM, "demanded_fields": ["a"]})
        assert source_data_contract_artifact_hash(["a"]) != legacy_v1_hash

    @pytest.mark.parametrize(
        "payload",
        (
            "not json",
            {"demanded_fields": "colour"},
            {"demanded_fields": [1]},
            {
                "contract_version": 1,
                "kind": SOURCE_DATA_CONTRACT_USER_TERM,
                "demanded_fields": ["colour"],
                "sample_header": None,
                "missing_from_sample": [],
            },
            {
                "contract_version": 2,
                "kind": "invented_source",
                "demanded_fields": ["colour"],
                "sample_header": None,
                "missing_from_sample": [],
            },
            {
                "contract_version": 2,
                "kind": SOURCE_DATA_CONTRACT_USER_TERM,
                "demanded_fields": ["colour"],
                "sample_header": 42,
                "missing_from_sample": [],
            },
            {
                "contract_version": 2,
                "kind": SOURCE_DATA_CONTRACT_USER_TERM,
                "demanded_fields": ["colour"],
                "sample_header": None,
                "missing_from_sample": [1],
            },
            {
                "contract_version": 2,
                "kind": SOURCE_DATA_CONTRACT_USER_TERM,
                "demanded_fields": ["colour"],
                "sample_header": None,
                "missing_from_sample": ["size"],
            },
            {
                "contract_version": 2,
                "kind": SOURCE_DATA_CONTRACT_USER_TERM,
                "demanded_fields": ["colour"],
                "sample_header": None,
                "missing_from_sample": [],
                "unexpected": True,
            },
        ),
        ids=(
            "not-json",
            "missing-shape",
            "non-string-demand",
            "legacy-version",
            "wrong-kind",
            "malformed-sample",
            "malformed-missing",
            "missing-not-demanded",
            "extra-key",
        ),
    )
    def test_parse_abstains_on_malformed_or_non_current_payloads(self, payload: object) -> None:
        value = payload if isinstance(payload, str) else json.dumps(payload)
        assert parse_source_data_contract_accepted_fields(value) is None

    def test_user_term_constant(self) -> None:
        assert SOURCE_DATA_CONTRACT_USER_TERM == "source_data_contract"


class TestSampleHeader:
    def test_reads_header_from_bound_csv(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "upload.csv"
        csv_path.write_text("colour,size\nred,10\n", encoding="utf-8")
        source = SourceSpec(
            plugin="csv",
            on_success="source",
            options={"path": str(csv_path)},
            on_validation_failure="discard",
        )
        assert sample_header_for_source(source) == ("colour", "size")

    def test_honours_declared_delimiter(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "upload.tsv"
        csv_path.write_text("colour\tsize\nred\t10\n", encoding="utf-8")
        source = SourceSpec(
            plugin="csv",
            on_success="source",
            options={"path": str(csv_path), "delimiter": "\t"},
            on_validation_failure="discard",
        )
        assert sample_header_for_source(source) == ("colour", "size")

    def test_abstains_on_missing_file(self) -> None:
        source = SourceSpec(
            plugin="csv",
            on_success="source",
            options={"path": "/nonexistent/upload.csv"},
            on_validation_failure="discard",
        )
        assert sample_header_for_source(source) is None

    def test_abstains_on_non_csv_plugin(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "upload.csv"
        csv_path.write_text("colour\nred\n", encoding="utf-8")
        source = SourceSpec(
            plugin="json",
            on_success="source",
            options={"path": str(csv_path)},
            on_validation_failure="discard",
        )
        assert sample_header_for_source(source) is None

    def test_abstains_on_undecodable_bytes(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "upload.csv"
        csv_path.write_bytes(b"\xff\xfe\x00broken")
        source = SourceSpec(
            plugin="csv",
            on_success="source",
            options={"path": str(csv_path)},
            on_validation_failure="discard",
        )
        assert sample_header_for_source(source) is None
