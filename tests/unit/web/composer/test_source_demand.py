"""Tests for the source data-contract demand backtrace (elspeth-da68332faf).

The demand set is DERIVED by delta-running Stage-1 validation's own
edge-contract ledger, so these tests pin the derivation's guarantees:
minimality (a field no consumer requires never appears), transparency (the
demand reaches the source through pass-through nodes), and honest abstention
(a source that cannot carry a guarantee stamp yields no demand).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from elspeth.web.composer.source_demand import (
    SOURCE_DATA_CONTRACT_USER_TERM,
    backtraced_source_demand,
    build_source_data_contract_draft,
    parse_source_data_contract_accepted_fields,
    sample_header_for_source,
    source_data_contract_artifact_hash,
    stamp_source_options_with_guarantees,
)
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec


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

    def test_draft_warns_on_demanded_fields_the_sample_lacks(self) -> None:
        draft = build_source_data_contract_draft(["colour", "size"], ("colour", "extra"))
        assert '"missing_from_sample":["size"]' in draft

    def test_draft_without_sample_has_no_sample_warnings(self) -> None:
        draft = build_source_data_contract_draft(["colour"], None)
        assert '"sample_header":null' in draft
        assert '"missing_from_sample":[]' in draft

    def test_artifact_hash_binds_the_field_set_only(self) -> None:
        # Order-insensitive over fields; sample evidence never participates.
        assert source_data_contract_artifact_hash(["b", "a"]) == source_data_contract_artifact_hash(["a", "b"])
        assert source_data_contract_artifact_hash(["a"]) != source_data_contract_artifact_hash(["a", "b"])

    def test_parse_abstains_on_malformed_payloads(self) -> None:
        assert parse_source_data_contract_accepted_fields("not json") is None
        assert parse_source_data_contract_accepted_fields('{"demanded_fields": "colour"}') is None
        assert parse_source_data_contract_accepted_fields('{"demanded_fields": [1]}') is None

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
