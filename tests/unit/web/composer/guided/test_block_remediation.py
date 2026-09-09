"""Trust-domain regression proofs for guided coverage and wire observations."""

from __future__ import annotations

import ast
from dataclasses import replace

import pytest

from elspeth.contracts.freeze import deep_freeze
from elspeth.web.composer.guided.deferred_intents import _coverage_context, _row_column
from elspeth.web.composer.guided.protocol import TurnType, validate_payload
from tests.unit.web.composer.guided.test_deferred_intent_coverage import SOURCE_ID, _candidate, _guided, _node
from tests.unit.web.composer.guided.test_emitters import _schema_output_state, _wire_turn


@pytest.mark.parametrize("walk", ("success", "branch"))
def test_coverage_walk_rejects_missing_owned_component(walk: str) -> None:
    candidate = replace(
        _candidate(),
        nodes=(
            _node(
                node_id="gate",
                plugin=None,
                node_type="gate",
                routes={"true": "rows", "false": "rows"},
            ),
        ),
    )
    context = _coverage_context(candidate, _guided())
    components = dict(context.exact_components)
    output_identity = context.consumers["rows"][0]
    del components[output_identity]
    corrupt = replace(context, exact_components=components)

    with pytest.raises(KeyError):
        if walk == "success":
            corrupt.exclusively_reached_gate(context.exact_components[("source", SOURCE_ID)])
        else:
            corrupt.route_output_name(context.exact_components[("node", "gate")], "true")


def test_branch_without_a_consumer_is_unproven() -> None:
    candidate = replace(
        _candidate(),
        nodes=(_node(node_id="gate", plugin=None, node_type="gate", routes={"true": "unwired", "false": "rows"}),),
    )
    context = _coverage_context(candidate, _guided())
    gate = context.exact_components[("node", "gate")]

    assert context.route_output_name(gate, "false") == "rows"
    assert context.route_output_name(gate, "true") is None


@pytest.mark.parametrize(
    ("expression", "expected"),
    (
        ("row['amount']", "amount"),
        ("row.get('amount')", "amount"),
        ("row[other]", None),
        ("row.get('amount', 0)", None),
        ("other.get('amount')", None),
        ("context.row['amount']", None),
        ("row['']", None),
        ("row[1]", None),
        ("get('amount')", None),
    ),
)
def test_row_column_observes_only_literal_row_access(expression: str, expected: str | None) -> None:
    assert _row_column(ast.parse(expression, mode="eval").body) == expected


@pytest.mark.parametrize("frozen", (False, True))
@pytest.mark.parametrize("sample", ("scalar", [1], 3, None))
def test_inspect_payload_rejects_non_row_samples(sample: object, frozen: bool) -> None:
    payload = {"observed": {"columns": [], "samples": [sample], "warnings": []}}
    assert validate_payload(TurnType.INSPECT_AND_CONFIRM, deep_freeze(payload) if frozen else payload) == (
        "payload.observed.samples[0] must be a mapping"
    )


def test_inspect_payload_accepts_frozen_rows_and_rejects_non_json_contents() -> None:
    payload = {"observed": {"columns": ["amount"], "samples": [{"amount": 3}], "warnings": []}}
    assert validate_payload(TurnType.INSPECT_AND_CONFIRM, deep_freeze(payload)) is None
    invalid = {"observed": {"columns": ["amount"], "samples": [{"amount": float("nan")}], "warnings": []}}
    assert validate_payload(TurnType.INSPECT_AND_CONFIRM, deep_freeze(invalid)) is not None


def test_malformed_wire_field_cannot_make_a_proposal_confirmable() -> None:
    turn = _wire_turn(_schema_output_state(["id: int", "bad: invalid_type"]))
    payload = turn["payload"]

    assert payload["outputs"][0]["business_schema"]["fields"] == []
    assert payload["can_confirm"] is False
    assert [blocker["error_code"] for blocker in payload["blockers"]] == ["contract_config_invalid"]


@pytest.mark.parametrize(
    "fields",
    (
        [{"id": "int"}, {"email": "str"}],
        [{"name": "id", "field_type": "int"}, {"name": "email", "field_type": "str"}],
    ),
)
def test_wire_projection_preserves_all_valid_schema_field_spellings(fields: list[object]) -> None:
    turn = _wire_turn(_schema_output_state(fields))
    assert turn["payload"]["can_confirm"] is True
    assert turn["payload"]["blockers"] == []
    assert turn["payload"]["outputs"][0]["business_schema"]["fields"] == [
        {"name": "id", "type": "int", "required": True, "nullable": False},
        {"name": "email", "type": "str", "required": True, "nullable": False},
    ]
