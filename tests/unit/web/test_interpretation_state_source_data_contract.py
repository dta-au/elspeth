"""Site enumeration + readiness blocking for source_data_contract reviews.

Covers the staging trigger matrix from elspeth-da68332faf work item 2:
uploaded source + demand stages a card; a composer-authored bound blob does
NOT; no demand does NOT; a demand-set change after acknowledgement re-opens
the card; an unacknowledged contract blocks handoff exactly like
invented_source (materialize_state_for_execution returns the pending site).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.web.composer.source_demand import (
    SOURCE_DATA_CONTRACT_USER_TERM,
    build_source_data_contract_draft,
    source_data_contract_artifact_hash,
)
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    SOURCE_AUTHORING_KEY,
    InterpretationReviewPending,
    current_source_data_contract_demand,
    interpretation_sites,
    materialize_state_for_execution,
    reconcile_authoritative_reviews,
)


def _llm_node(*, required: list[str] | None, node_id: str = "rate") -> NodeSpec:
    options: dict[str, Any] = {
        "prompt_template": "Rate {{ row.colour }}",
        "model": "gpt-test",
        "schema": {"mode": "observed"},
    }
    if required is not None:
        options["required_input_fields"] = required
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="llm",
        input="source",
        on_success="rated",
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state(source_options: dict[str, Any], *, required: list[str] | None = None) -> CompositionState:
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
        nodes=(_llm_node(required=required),),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _contract_sites(state: CompositionState):
    return [site for site in interpretation_sites(state) if site.kind is InterpretationKind.SOURCE_DATA_CONTRACT]


def _resolved_contract_options(acknowledged: list[str], *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    draft = build_source_data_contract_draft(acknowledged, None)
    options: dict[str, Any] = {
        "path": "/tmp/upload.csv",
        "schema": {"mode": "observed", "guaranteed_fields": list(acknowledged)},
        INTERPRETATION_REQUIREMENTS_KEY: [
            {
                "id": "source-data-contract-source",
                "kind": InterpretationKind.SOURCE_DATA_CONTRACT.value,
                "user_term": SOURCE_DATA_CONTRACT_USER_TERM,
                "status": "resolved",
                "draft": draft,
                "event_id": "11111111-1111-1111-1111-111111111111",
                "accepted_value": draft,
                "accepted_artifact_hash": source_data_contract_artifact_hash(acknowledged),
                "resolved_prompt_template_hash": None,
            }
        ],
    }
    if extra:
        options.update(extra)
    return options


class TestSiteStaging:
    def test_uploaded_source_with_demand_stages_a_site(self) -> None:
        sites = _contract_sites(_state({"path": "/tmp/upload.csv"}, required=["colour"]))
        assert len(sites) == 1
        site = sites[0]
        assert site.component_id == "source"
        assert site.component_type == "source"
        assert site.user_term == SOURCE_DATA_CONTRACT_USER_TERM

    def test_composer_authored_bound_blob_never_stages(self) -> None:
        options = {
            "path": "/tmp/generated.csv",
            SOURCE_AUTHORING_KEY: {
                "modality": "llm_generated",
                "content_hash": "0" * 64,
                "review_event_id": None,
                "resolved_kind": None,
            },
        }
        assert _contract_sites(_state(options, required=["colour"])) == []

    def test_no_demand_never_stages(self) -> None:
        assert _contract_sites(_state({"path": "/tmp/upload.csv"}, required=None)) == []

    def test_acknowledged_matching_demand_is_clean(self) -> None:
        state = _state(_resolved_contract_options(["colour"]), required=["colour"])
        assert _contract_sites(state) == []

    def test_demand_growth_reopens_the_card(self) -> None:
        # Acknowledged {colour}; the pipeline now also requires 'size' from
        # the source — the acknowledged FIELD SET no longer matches the
        # demand, so the card re-opens (accepted artifact binds the set).
        state = _state(_resolved_contract_options(["colour"]), required=["colour", "size"])
        sites = _contract_sites(state)
        assert len(sites) == 1
        assert current_source_data_contract_demand(state, "source") == ("colour", "size")

    def test_demand_shrunk_to_empty_closes_without_reasking(self) -> None:
        # Every consumer requirement is gone: nothing left to acknowledge, no
        # card — the standing stamp remains the user's own recorded promise.
        state = _state(_resolved_contract_options(["colour"]), required=None)
        assert _contract_sites(state) == []


def _queue_node() -> NodeSpec:
    return NodeSpec(
        id="q",
        node_type="queue",
        plugin=None,
        input="q",
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


def _fan_in_state(
    sources: dict[str, SourceSpec],
    *,
    required: list[str] | None = None,
) -> CompositionState:
    node = _llm_node(required=required if required is not None else ["colour"])
    return CompositionState(
        source=None,
        sources=sources,
        nodes=(_queue_node(), replace(node, input="q")),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _fan_in_source(options: dict[str, Any]) -> SourceSpec:
    return SourceSpec(plugin="csv", on_success="q", options=options, on_validation_failure="discard")


def _fork_fan_in_state(node_type: Literal["coalesce", "row_union"]) -> CompositionState:
    is_coalesce = node_type == "coalesce"
    source = SourceSpec(
        plugin="csv",
        on_success="input",
        options=_resolved_contract_options(["colour"]),
        on_validation_failure="discard",
    )
    gate = NodeSpec(
        id="split",
        node_type="gate",
        plugin=None,
        input="input",
        on_success=None,
        on_error="discard",
        options={},
        condition="True",
        routes={"true": "fork", "false": "fork"},
        fork_to=("a", "b"),
        branches=None,
        policy=None,
        merge=None,
    )
    fan_in = NodeSpec(
        id="joined" if is_coalesce else "u",
        node_type=node_type,
        plugin=None,
        input="a",
        on_success=None if is_coalesce else "joined",
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches={"a": "a", "b": "b"},
        policy="require_all" if is_coalesce else None,
        merge="union" if is_coalesce else None,
    )
    consumer = replace(_llm_node(required=["colour", "size"]), input="joined", on_success="output")
    return CompositionState(
        source=None,
        sources={"source": source},
        nodes=(gate, fan_in, consumer),
        edges=(),
        outputs=(
            OutputSpec(
                name="output",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


class TestFanInSiteLifecycle:
    """AND-attributed fan-in demand: one card per feeding source, no re-ask loop.

    Ruling on elspeth-da68332faf: a queue fan-in requirement is an AND over N
    independent per-source promises, so each eligible feeding source gets its
    own card for the same field. Resolving one card must keep that source's
    site CLOSED while a sibling's card is still pending — the disregard-strip
    recompute returns the resolved source's own demand again (necessity holds
    for its rows regardless of the sibling), so the accepted artifact hash
    keeps matching and the enumerator never re-opens a settled card.
    """

    def test_each_pending_feeding_source_gets_its_own_site(self) -> None:
        state = _fan_in_state(
            {
                "src_a": _fan_in_source({"path": "/tmp/a.csv", "schema": {"mode": "observed", "guaranteed_fields": ["id"]}}),
                "src_b": _fan_in_source({"path": "/tmp/b.csv", "schema": {"mode": "observed", "guaranteed_fields": ["id"]}}),
            }
        )
        sites = _contract_sites(state)
        assert sorted(site.component_id for site in sites) == ["source:src_a", "source:src_b"]

    def test_resolved_source_stays_closed_while_a_sibling_is_pending(self) -> None:
        draft = build_source_data_contract_draft(["colour"], None)
        resolved_options: dict[str, Any] = {
            "path": "/tmp/a.csv",
            "schema": {"mode": "observed", "guaranteed_fields": ["colour", "id"]},
            INTERPRETATION_REQUIREMENTS_KEY: [
                {
                    "id": "source-data-contract-src_a",
                    "kind": InterpretationKind.SOURCE_DATA_CONTRACT.value,
                    "user_term": SOURCE_DATA_CONTRACT_USER_TERM,
                    "status": "resolved",
                    "draft": draft,
                    "event_id": "11111111-1111-1111-1111-111111111111",
                    "accepted_value": draft,
                    "accepted_artifact_hash": source_data_contract_artifact_hash(["colour"]),
                    "resolved_prompt_template_hash": None,
                }
            ],
        }
        state = _fan_in_state(
            {
                "src_a": _fan_in_source(resolved_options),
                "src_b": _fan_in_source({"path": "/tmp/b.csv", "schema": {"mode": "observed", "guaranteed_fields": ["id"]}}),
            }
        )
        # The disregard-strip recompute re-derives src_a's own demand even
        # though src_b's arm is unstamped — that identity with the accepted
        # hash is exactly what keeps src_a's site closed.
        assert current_source_data_contract_demand(state, "src_a") == ("colour",)
        sites = _contract_sites(state)
        assert [site.component_id for site in sites] == ["source:src_b"]
        assert current_source_data_contract_demand(state, "src_b") == ("colour",)

    def test_growth_after_sole_guarantees_reopens_every_source_card(self) -> None:
        # Both sources previously promised their sole guaranteed field.  A
        # later consumer grows the contract, so stripping each accepted field
        # for recomputation must preserve that arm's explicit-empty vote and
        # surface the full current demand on both resolvable cards.
        state = _fan_in_state(
            {
                "src_a": _fan_in_source(_resolved_contract_options(["colour"], extra={"path": "/tmp/a.csv"})),
                "src_b": _fan_in_source(_resolved_contract_options(["colour"], extra={"path": "/tmp/b.csv"})),
            },
            required=["colour", "size"],
        )

        assert current_source_data_contract_demand(state, "src_a") == ("colour", "size")
        assert current_source_data_contract_demand(state, "src_b") == ("colour", "size")
        assert sorted(site.component_id for site in _contract_sites(state)) == ["source:src_a", "source:src_b"]

    def test_row_union_growth_after_sole_guarantee_reopens_source_card(self) -> None:
        state = _fork_fan_in_state("row_union")
        result = state.validate()
        assert [error for error in result.errors if error.error_code != "schema_contract_violation"] == []
        assert [(contract.from_id, contract.to_id, contract.missing_fields) for contract in result.edge_contracts] == [
            ("u", "rate", ("size",)),
        ]
        assert current_source_data_contract_demand(state, "source") == ("colour", "size")
        assert [site.component_id for site in _contract_sites(state)] == ["source"]

    def test_coalesce_growth_after_sole_guarantee_reopens_source_card(self) -> None:
        state = _fork_fan_in_state("coalesce")

        result = state.validate()
        assert [error for error in result.errors if error.error_code != "schema_contract_violation"] == []
        assert [(contract.from_id, contract.to_id, contract.missing_fields) for contract in result.edge_contracts] == [
            ("joined", "rate", ("size",)),
        ]
        assert current_source_data_contract_demand(state, "source") == ("colour", "size")
        assert [site.component_id for site in _contract_sites(state)] == ["source"]


class TestReadinessBlocking:
    def test_unacknowledged_contract_blocks_execution_materialization(self) -> None:
        state = _state({"path": "/tmp/upload.csv"}, required=["colour"])
        result = materialize_state_for_execution(state)
        assert isinstance(result, InterpretationReviewPending)
        assert any(site.kind is InterpretationKind.SOURCE_DATA_CONTRACT for site in result.sites)

    def test_acknowledged_contract_does_not_block(self) -> None:
        state = _state(_resolved_contract_options(["colour"]), required=["colour"])
        result = materialize_state_for_execution(state)
        if isinstance(result, InterpretationReviewPending):
            assert not any(site.kind is InterpretationKind.SOURCE_DATA_CONTRACT for site in result.sites)


class TestReconcile:
    def test_resolved_row_carries_forward_verbatim(self) -> None:
        previous = _state(_resolved_contract_options(["colour"]), required=["colour"])
        proposed = _state(_resolved_contract_options(["colour"]), required=["colour"])
        reconciled = reconcile_authoritative_reviews(previous, proposed)
        rows = reconciled.sources["source"].options[INTERPRETATION_REQUIREMENTS_KEY]
        assert len(rows) == 1
        assert rows[0]["status"] == "resolved"
        assert rows[0]["accepted_artifact_hash"] == source_data_contract_artifact_hash(["colour"])

    def test_unknown_previous_row_reduces_to_pending_shell(self) -> None:
        previous = _state({"path": "/tmp/upload.csv"}, required=["colour"])
        proposed = _state(_resolved_contract_options(["colour"]), required=["colour"])
        # previous state has no matching resolved identity -> shell, not carry.
        reconciled = reconcile_authoritative_reviews(previous, proposed)
        rows = reconciled.sources["source"].options[INTERPRETATION_REQUIREMENTS_KEY]
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"
