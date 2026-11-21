"""A node whose contract sits in a file the authoring check does not open: what authoring says, and who decides.

``reference_join`` reads its joined fields' values, and so their types and
nullability, from the table it joins; under a valid ``inline_content`` marker
the resolver-free authoring probe cannot construct it and raises
``DeferredBlobContractProbe``. Before stage 2a every such node voted
"participates, guarantees nothing", so any consumer requiring a field by
name was refused with ``Producer (...) guarantees: [(none)]`` (upstream
fields included), while every check that reads a TYPE or NULLABILITY
abstained without saying so.

Now:

- a deferring known pass-through votes with exactly the guarantees it
  forwards; its own authored ``guaranteed_fields`` are never promoted; a
  deferring non-pass-through abstains;
- the consumer side drops the fields the node's config shape says it creates
  (``reference_join`` ``output`` keys) from its own input requirement;
- every check that meets the node abstains on what the file decides, and ONE
  ``contract_probe_deferred`` notice per node names the node, the fields it
  adds (when its config fixes their names) and the fact: they are checked
  before the run starts;
- the runtime preflight (the web save path) and run admission decide every
  abstained fact on the real bytes, before any run record exists.

Known limit, pinned by ``test_2B_BOUNDARY_...``: behind a FIXED upstream that
lacks a field the deferring node adds, a consumer requiring that field by
name is still refused at authoring (stage 2b).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic_core import ErrorDetails

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.freeze import deep_thaw
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec, ValidationSummary
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from tests.unit.web.composer import test_file_inject_file_content_probe as chain_fixtures
from tests.unit.web.composer import test_file_inject_upload_only_tool_parity as upload_tools
from tests.unit.web.execution.test_validate_blob_inline import _ready_blob_record

# Reuse the session/blob fixture the blob-tool suite already owns.
blob_env = upload_tools.blob_env

SESSION_ID = UUID("2c6e8a40-5b1d-4f3e-9c7a-0d1e2f3a4b5c")
TABLE_BLOB = UUID("3d7f9b51-6c2e-4a4f-8d8b-1e2f3a4b5c6d")

FIXED_SOURCE: dict[str, Any] = {"mode": "fixed", "fields": ["id: str", "k: str"]}
OBSERVED: dict[str, Any] = {"mode": "observed"}

INT_TABLE = '[{"sku": "p", "description": 3}]'
STR_TABLE = '[{"sku": "p", "description": "v"}]'
NULL_CELL_TABLE = '[{"sku": "p", "description": "v"}, {"sku": "q", "description": null}]'
CSV_TABLE = "sku,description\np,v\n"
CSV_TABLE_WITHOUT_COLUMN = "sku,label\np,v\n"

DEFERRED = "contract_probe_deferred"
# Every code an authoring check emits for a fact the table's bytes decide.
_BYTES_DECIDED_CODES = frozenset(
    {
        "row_union_schema_incompatible",
        "coalesce_union_type_incompatible",
        "edge_field_type_incompatible",
        "value_transform_result_type_incompatible",
        "sink_contract_violation",
        "schema_contract_violation",
    }
)


def _marker(text: str) -> dict[str, str]:
    return {"blob_ref": str(TABLE_BLOB), "mode": "inline_content", "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _node(**fields: Any) -> NodeSpec:
    spec: dict[str, Any] = {
        "plugin": None,
        "on_error": None,
        "options": {},
        "condition": None,
        "routes": None,
        "fork_to": None,
        "branches": None,
        "policy": None,
        "merge": None,
    }
    spec.update(fields)
    return NodeSpec(**spec)


def _transform(node_id: str, plugin: str, input: str, on_success: str, options: dict[str, Any]) -> NodeSpec:
    return _node(id=node_id, node_type="transform", plugin=plugin, input=input, on_success=on_success, on_error="discard", options=options)


def _join_node(node_id: str, input: str, on_success: str, join: tuple[str, dict[str, Any]]) -> NodeSpec:
    return _transform(node_id, join[0], input, on_success, join[1])


def _join(content: object, fmt: str = "json", schema: dict[str, Any] | None = None, **extra: Any) -> tuple[str, dict[str, Any]]:
    return (
        "reference_join",
        {
            "schema": schema if schema is not None else FIXED_SOURCE,
            "reference_content": content,
            "reference_format": fmt,
            "key_field": "k",
            "reference_key_name": "sku",
            "output": {"added": "ref['description']"},
            **extra,
        },
    )


def _source(tmp_path: Path, schema: dict[str, Any]) -> SourceSpec:
    blobs = tmp_path / "blobs" / str(SESSION_ID)
    blobs.mkdir(parents=True, exist_ok=True)
    (blobs / "input.csv").write_text("id,k\nw1,p\n", encoding="utf-8")
    return SourceSpec(
        plugin="csv", on_success="src_out", options={"path": str(blobs / "input.csv"), "schema": schema}, on_validation_failure="discard"
    )


def _sink(tmp_path: Path, schema: dict[str, Any] | None = None) -> OutputSpec:
    outputs = tmp_path / "outputs" / str(SESSION_ID)
    outputs.mkdir(parents=True, exist_ok=True)
    return OutputSpec(
        name="output",
        plugin="json",
        options={"path": str(outputs / "o.jsonl"), "format": "jsonl", "schema": schema if schema is not None else OBSERVED},
        on_write_failure="discard",
    )


def _graph(tmp_path: Path, *nodes: NodeSpec, source_schema: dict[str, Any], sink_schema: dict[str, Any] | None = None) -> CompositionState:
    return CompositionState(
        source=_source(tmp_path, source_schema),
        nodes=nodes,
        edges=(),
        outputs=(_sink(tmp_path, sink_schema),),
        metadata=PipelineMetadata(),
        version=1,
    )


def _fan(tmp_path: Path, control: tuple[str, dict[str, Any]], treatment: tuple[str, dict[str, Any]], *, merge: str) -> CompositionState:
    """fork -> two joins -> row_union (then a passthrough) or a require_all union coalesce."""
    fork = _node(
        id="fork_rows",
        node_type="gate",
        input="src_out",
        on_success=None,
        condition="True",
        routes={"true": "fork", "false": "fork"},
        fork_to=("control_branch", "treatment_branch"),
    )
    branches = {"control_branch": "control_done", "treatment_branch": "treatment_done"}
    joins = (
        _transform("control", control[0], "control_branch", "control_done", control[1]),
        _transform("treatment", treatment[0], "treatment_branch", "treatment_done", treatment[1]),
    )
    if merge == "row_union":
        tail: tuple[NodeSpec, ...] = (
            _node(id="variant_union", node_type="row_union", input="control_done", on_success="union_out", branches=branches),
            _transform("after_union", "passthrough", "union_out", "output", {"schema": OBSERVED}),
        )
    else:
        tail = (
            _node(
                id="merge_rows",
                node_type="coalesce",
                input="control_done",
                on_success="output",
                branches=branches,
                policy="require_all",
                merge="union",
            ),
        )
    return _graph(tmp_path, fork, *joins, *tail, source_schema=FIXED_SOURCE)


def _record(text: str, *, mime_type: str = "application/json") -> BlobRecord:
    raw = text.encode()
    return replace(
        _ready_blob_record(session_id=SESSION_ID, blob_id=TABLE_BLOB, size_bytes=len(raw)),
        filename="reference.json" if mime_type == "application/json" else "reference.csv",
        mime_type=mime_type,
        content_hash=hashlib.sha256(raw).hexdigest(),
    )


def _execution_validate(tmp_path: Path, state: CompositionState, text: str, *, mime_type: str = "application/json") -> ValidationResult:
    """Execution validation with the table's real bytes behind its marker."""
    record = _record(text, mime_type=mime_type)
    return validate_pipeline_for_trained_operator(
        state,
        SimpleNamespace(data_dir=tmp_path),
        yaml_generator,
        blob_get_metadata=lambda blob_id: record if blob_id == TABLE_BLOB else None,
        blob_get_content=lambda _blob_id: (record, text.encode()),
        session_id=str(SESSION_ID),
    )


def _notices(summary: ValidationSummary) -> list[str]:
    return [entry.message for entry in summary.warnings if entry.error_code == DEFERRED]


def _codes(summary: ValidationSummary) -> list[str | None]:
    return [entry.error_code for entry in summary.errors]


def _join_notice(node_id: str, fields: str = "added") -> str:
    return (
        f"Node '{node_id}' (reference_join) reads an attached file that this check does not open, so the fields it adds ({fields}), "
        "their types and whether they can be empty are checked before the run starts. No change is needed."
    )


# ---------------------------------------------------------------------------
# The vote: (a) a deferring pass-through forwards, (c) a non-pass-through abstains
# ---------------------------------------------------------------------------


def test_a_deferring_pass_through_votes_with_the_upstream_guarantees_it_forwards(tmp_path: Path) -> None:
    """A consumer requiring an UPSTREAM field is satisfied; before 2a it was refused with ``guarantees: [(none)]``."""
    state = _graph(
        tmp_path,
        _join_node("enrich", "src_out", "output", _join(_marker(CSV_TABLE), "csv")),
        source_schema=FIXED_SOURCE,
        sink_schema={"mode": "observed", "required_fields": ["id", "k"]},
    )

    summary = state.validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _notices(summary) == [_join_notice("enrich")]
    [contract] = [contract for contract in summary.edge_contracts if contract.from_id == "enrich"]
    assert contract.producer_guarantees == ("id", "k")
    assert contract.satisfied


def test_its_own_authored_guarantees_are_never_promoted(tmp_path: Path) -> None:
    """Condition (i): the node's authored ``guaranteed_fields`` reach no guarantee and no edge contract, at any consumer.

    ``certified`` is authored by the node and written by nothing: unknown
    bytes cannot prove it. ``added`` is the field the join writes; it is not
    promoted either (stage 2b decides what a FIXED upstream means for it).
    """
    authored = {"mode": "observed", "guaranteed_fields": ["added", "certified"]}
    state = _graph(
        tmp_path,
        _join_node("enrich", "src_out", "mid", _join(_marker(CSV_TABLE), "csv", schema=authored)),
        _transform("relay", "passthrough", "mid", "output", {"schema": {"mode": "observed", "required_fields": ["certified"]}}),
        source_schema=FIXED_SOURCE,
        sink_schema={"mode": "observed", "required_fields": ["id", "certified"]},
    )

    summary = state.validate()

    assert not summary.is_valid
    assert {entry.error_code for entry in summary.errors} == {"schema_contract_violation", "sink_contract_violation"}
    for error in summary.errors:
        assert "guarantees: [id, k]. Missing fields: [certified]." in error.message, error.message
    for contract in summary.edge_contracts:
        assert not {"added", "certified"} & set(contract.producer_guarantees), contract.to_dict()
    assert _notices(summary) == [_join_notice("enrich")]


def test_a_deferring_non_pass_through_abstains(tmp_path: Path) -> None:
    """(c): ``field_mapper`` replaces its input; with its mapping in a file nothing about its output is known.

    Abstaining means a consumer requiring a field the step may write is not
    refused at authoring; the runtime preflight decides it. The notice cannot
    name the fields: the config shape does not fix them.
    """
    mapper = _transform("rename", "field_mapper", "src_out", "output", {"schema": OBSERVED, "mapping": {"id": _marker("x")}})
    state = _graph(tmp_path, mapper, source_schema=FIXED_SOURCE, sink_schema={"mode": "observed", "required_fields": ["renamed_id"]})

    summary = state.validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _notices(summary) == [
        "Node 'rename' (field_mapper) reads an attached file that this check does not open, so the fields this step reads and adds, "
        "their types and whether they can be empty are checked before the run starts. No change is needed."
    ]
    assert not [contract for contract in summary.edge_contracts if contract.from_id == "rename"]


# ---------------------------------------------------------------------------
# The consumer side (audit item 4): a field the join creates is not required of its upstream
# ---------------------------------------------------------------------------


def test_a_joined_field_the_node_declares_is_not_required_of_its_upstream(tmp_path: Path) -> None:
    """Declaring ``added`` in the join's own ``schema.fields`` used to refuse the INBOUND edge: the source lacks it.

    The constructed model demotes every field the node creates; without the
    table, the ``output`` mapping's keys are the fields it is certain to
    create. The runtime preflight then decides the declaration against the
    real table, and refuses a table without the column.
    """
    declared = {"mode": "fixed", "fields": ["id: str", "k: str", "added: str"]}

    def build(table: str) -> CompositionState:
        return _graph(
            tmp_path, _join_node("enrich", "src_out", "output", _join(_marker(table), "csv", schema=declared)), source_schema=FIXED_SOURCE
        )

    state = build(CSV_TABLE)
    summary = state.validate()
    admitted = _execution_validate(tmp_path, state, CSV_TABLE, mime_type="text/csv")
    refused = _execution_validate(tmp_path, build(CSV_TABLE_WITHOUT_COLUMN), CSV_TABLE_WITHOUT_COLUMN, mime_type="text/csv")

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _notices(summary) == [_join_notice("enrich")]
    assert admitted.is_valid, [error.message for error in admitted.errors]
    assert not refused.is_valid
    assert any("output fields ['added'] resolved against none of the 1 reference entries" in error.message for error in refused.errors), [
        error.message for error in refused.errors
    ]


def test_a_declared_field_the_join_does_not_create_is_still_required_of_its_upstream(tmp_path: Path) -> None:
    """Control: only the ``output`` keys are demoted; any other declared field still needs an upstream."""
    declared = {"mode": "fixed", "fields": ["id: str", "k: str", "added: str", "region: str"]}
    state = _graph(
        tmp_path, _join_node("enrich", "src_out", "output", _join(_marker(CSV_TABLE), "csv", schema=declared)), source_schema=FIXED_SOURCE
    )

    summary = state.validate()

    assert not summary.is_valid
    [error] = summary.errors
    assert error.error_code == "schema_contract_violation"
    assert "Consumer (reference_join) requires fields: [id, k, region]" in error.message
    assert "Missing fields: [region]" in error.message


# ---------------------------------------------------------------------------
# Every check that reads what the bytes decide abstains, says so, and the runtime decides
# ---------------------------------------------------------------------------


def _edge_to_node(tmp_path: Path, content: object) -> CompositionState:
    return _graph(
        tmp_path,
        _join_node("enrich", "src_out", "mid", _join(content, schema=OBSERVED)),
        _transform("consumer", "passthrough", "mid", "output", {"schema": {"mode": "flexible", "fields": ["added: str"]}}),
        source_schema=OBSERVED,
    )


def _edge_to_sink(tmp_path: Path, content: object) -> CompositionState:
    return _graph(
        tmp_path,
        _join_node("enrich", "src_out", "output", _join(content, schema=OBSERVED)),
        source_schema=OBSERVED,
        sink_schema={"mode": "flexible", "fields": ["added: str"]},
    )


def _value_binding(tmp_path: Path, content: object) -> CompositionState:
    copy = {"schema": {"mode": "flexible", "fields": ["copied: str"]}, "operations": [{"target": "copied", "expression": "row['added']"}]}
    return _graph(
        tmp_path,
        _join_node("enrich", "src_out", "mid", _join(content, schema=OBSERVED)),
        _transform("copy_value", "value_transform", "mid", "output", copy),
        source_schema=OBSERVED,
    )


def _declared_narrow(tmp_path: Path, content: object) -> CompositionState:
    declared = {"mode": "fixed", "fields": ["id: str", "k: str", "added: str"]}
    return _graph(tmp_path, _join_node("enrich", "src_out", "output", _join(content, schema=declared)), source_schema=FIXED_SOURCE)


def _row_union(tmp_path: Path, content: object, **extra: Any) -> CompositionState:
    return _fan(tmp_path, _join(content, **extra), _join(CSV_TABLE, "csv"), merge="row_union")


def _row_union_against_nullable(tmp_path: Path, content: object) -> CompositionState:
    """The literal branch is nullable (``on_miss: null``); the file-held one is nullable only if its table holds a null."""
    return _fan(tmp_path, _join(content), _join(CSV_TABLE, "csv", on_miss="null"), merge="row_union")


def _coalesce(tmp_path: Path, content: object) -> CompositionState:
    return _fan(tmp_path, _join(content), _join(CSV_TABLE, "csv"), merge="coalesce")


# (graph, deferring node, conflicting table, the runtime's refusal, admitted table)
_ABSTAINED_FACTS = {
    "edge_to_node_type": (_edge_to_node, "enrich", INT_TABLE, "field 'added': consumer requires 'str', producer emits", STR_TABLE),
    "edge_to_node_json_null": (
        _edge_to_node,
        "enrich",
        NULL_CELL_TABLE,
        "field 'added': consumer requires 'str', producer emits",
        STR_TABLE,
    ),
    "edge_to_sink_type": (_edge_to_sink, "enrich", INT_TABLE, "field 'added': consumer requires 'str', producer emits 'int'", STR_TABLE),
    "value_transform_binding": (
        _value_binding,
        "enrich",
        INT_TABLE,
        "target 'copied' declares str, but its expression computes int",
        STR_TABLE,
    ),
    "declared_type_refuted": (_declared_narrow, "enrich", INT_TABLE, "schema declares 'added' as str", STR_TABLE),
    "declared_non_null_refuted": (_declared_narrow, "enrich", NULL_CELL_TABLE, "schema declares 'added' as never null", STR_TABLE),
    "row_union_type": (_row_union, "control", INT_TABLE, "receives incompatible schemas from multiple branches", STR_TABLE),
    "row_union_nullability_bytes": (
        _row_union,
        "control",
        NULL_CELL_TABLE,
        "receives incompatible schemas from multiple branches",
        STR_TABLE,
    ),
    "coalesce_union_type": (_coalesce, "control", INT_TABLE, "receives incompatible types for field 'added' in union merge", STR_TABLE),
    # An unknown is not "never null": here the NON-null table is the conflicting one.
    "row_union_unknown_is_not_non_null": (
        _row_union_against_nullable,
        "control",
        STR_TABLE,
        "receives incompatible schemas from multiple branches",
        NULL_CELL_TABLE,
    ),
}


@pytest.mark.parametrize("fact", sorted(_ABSTAINED_FACTS))
def test_authoring_abstains_with_a_notice_and_execution_validation_decides(tmp_path: Path, fact: str) -> None:
    """Audit items 5 and 6: no error on the fact, no zero-guarantee rejection, the named notice; the bytes decide.

    The control half: the same graph with an agreeing table is admitted, so
    the refusal is about the bytes and not the shape.
    """
    build, node_id, conflicting, refusal, agreeing = _ABSTAINED_FACTS[fact]
    state = build(tmp_path, _marker(conflicting))

    summary = state.validate()
    refused = _execution_validate(tmp_path, state, conflicting)
    admitted = _execution_validate(tmp_path, build(tmp_path, _marker(agreeing)), agreeing)

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert not _BYTES_DECIDED_CODES & {entry.error_code for entry in summary.warnings}
    assert _notices(summary) == [_join_notice(node_id)]
    assert not refused.is_valid
    assert any(refusal in error.message for error in refused.errors), [error.message for error in refused.errors]
    assert admitted.is_valid, [error.message for error in admitted.errors]


@pytest.mark.parametrize(
    ("label", "build"),
    [
        pytest.param("row_union", _row_union, id="row_union"),
        pytest.param("coalesce", _coalesce, id="coalesce"),
    ],
)
def test_the_same_fan_in_with_the_table_inline_is_refused_at_authoring(tmp_path: Path, label: str, build: Any) -> None:
    """Twin control: the abstention is the marker's, not the check's. With the table's text inline the check fires."""
    summary = build(tmp_path, INT_TABLE).validate()

    assert not summary.is_valid
    assert {entry.error_code for entry in summary.errors} == {
        f"{label}_schema_incompatible" if label == "row_union" else "coalesce_union_type_incompatible"
    }
    assert _notices(summary) == []


def test_a_row_union_of_config_decided_nullability_carries_the_notice(tmp_path: Path) -> None:
    """``on_miss: null`` against ``on_miss: fail``: the audit's fifth silent row_union graph.

    Nullability here is config, but the check reads it from the constructed
    branch schema, which the marker withholds; it abstains, now with the
    notice, and the runtime refuses.
    """
    state = _row_union(tmp_path, _marker(STR_TABLE), on_miss="null")

    summary = state.validate()
    refused = _execution_validate(tmp_path, state, STR_TABLE)

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _notices(summary) == [_join_notice("control")]
    assert not refused.is_valid
    assert any("receives incompatible schemas from multiple branches" in error.message for error in refused.errors)


# ---------------------------------------------------------------------------
# What the notice says
# ---------------------------------------------------------------------------


def test_one_notice_per_deferring_node_naming_node_fields_and_fact_and_nothing_else(tmp_path: Path) -> None:
    """Two consumers ask about the join; it is still one notice, in the node's own words, with no content and no repair."""
    table = "sku,description\nsecret-sku-value,secret-description-value\n"
    state = _graph(
        tmp_path,
        _join_node("enrich", "src_out", "mid", _join(_marker(table), "csv")),
        _transform("relay", "passthrough", "mid", "output", {"schema": {"mode": "observed", "required_fields": ["k"]}}),
        source_schema=FIXED_SOURCE,
        sink_schema={"mode": "observed", "required_fields": ["id"]},
    )

    summary = state.validate()

    [notice] = [entry for entry in summary.warnings if entry.error_code == DEFERRED]
    assert notice.component == "node:enrich"
    assert notice.severity == "medium"
    assert notice.message == _join_notice("enrich")
    rendered = json.dumps(notice.to_dict())
    for withheld in ("secret-sku-value", "secret-description-value", "ref['description']", str(TABLE_BLOB), "sha256", "inline_content"):
        assert withheld not in rendered
    for repair in ("repair", "Repair", "suggest", "replace", "remove"):
        assert repair not in notice.message


def test_the_notice_names_every_lookup_output_of_a_multi_table_join(tmp_path: Path) -> None:
    """The multi-table form writes the keys of each ``lookups[].output``; the notice names all of them, sorted."""
    options = {
        "schema": OBSERVED,
        "tables": {
            "regions": {"reference_content": _marker(CSV_TABLE), "reference_format": "csv"},
            "owners": {"reference_content": _marker(CSV_TABLE), "reference_format": "csv"},
        },
        "lookups": [
            {"table": "regions", "key_field": "k", "reference_key_name": "sku", "output": {"region_name": "ref['description']"}},
            {
                "table": "owners",
                "key_field": "k",
                "reference_key_name": "sku",
                "output": {"owner_name": "ref['description']", "owner_code": "ref['sku']"},
            },
        ],
    }
    state = _graph(tmp_path, _transform("enrich", "reference_join", "src_out", "output", options), source_schema=FIXED_SOURCE)

    summary = state.validate()

    assert _notices(summary) == [_join_notice("enrich", "owner_code, owner_name, region_name")]


def test_a_node_with_its_contract_in_hand_gets_no_notice(tmp_path: Path) -> None:
    """Control: the same join with the table inline constructs, so nothing is deferred and nothing is said."""
    state = _graph(tmp_path, _join_node("enrich", "src_out", "output", _join(CSV_TABLE, "csv")), source_schema=FIXED_SOURCE)

    assert _notices(state.validate()) == []


# ---------------------------------------------------------------------------
# Real errors stay errors
# ---------------------------------------------------------------------------


def test_a_field_neither_upstream_nor_the_join_could_supply_is_still_refused(tmp_path: Path) -> None:
    """Condition (v), the typo case: ``descriptoin`` is not upstream and not a key of the join's ``output``."""
    state = _graph(
        tmp_path,
        _join_node("enrich", "src_out", "output", _join(_marker(CSV_TABLE), "csv")),
        source_schema=FIXED_SOURCE,
        sink_schema={"mode": "observed", "required_fields": ["descriptoin"]},
    )

    summary = state.validate()

    assert not summary.is_valid
    [error] = summary.errors
    assert error.error_code == "sink_contract_violation"
    assert error.message == (
        "Schema contract violation: 'enrich' -> 'output:output'. Sink 'output' requires fields: [descriptoin]. "
        "Producer (reference_join) guarantees: [id, k]. Missing fields: [descriptoin]."
    )
    assert _notices(summary) == [_join_notice("enrich")]


# Formerly ``test_2B_BOUNDARY_a_fixed_upstream_lacking_a_joined_field_still_refuses_a_consumer_requiring_it``,
# the known 2a limit; stage 2b closes it and this is its rewrite.
def test_a_consumer_requiring_a_field_the_join_adds_behind_a_fixed_upstream_is_left_to_the_preflight(tmp_path: Path) -> None:
    """``added`` is an ``output`` key: the join adds it once its table is read, so it is not refused here.

    The fixed upstream guarantees ``id, k``; the join's own additions stay
    unverified (never a guarantee, no edge contract), and the notice names
    them. Execution validation admits the graph over a table with the column
    and refuses it over one without; per-site and per-shape tests are in
    ``test_deferred_possible_additions.py``.
    """

    def build(table: str) -> CompositionState:
        return _graph(
            tmp_path / table.split("\n")[0],
            _join_node("enrich", "src_out", "output", _join(_marker(table), "csv")),
            source_schema=FIXED_SOURCE,
            sink_schema={"mode": "observed", "required_fields": ["added"]},
        )

    summary = build(CSV_TABLE).validate()
    admitted = _execution_validate(tmp_path / "sku,description", build(CSV_TABLE), CSV_TABLE, mime_type="text/csv")
    refused = _execution_validate(tmp_path / "sku,label", build(CSV_TABLE_WITHOUT_COLUMN), CSV_TABLE_WITHOUT_COLUMN, mime_type="text/csv")

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _notices(summary) == [_join_notice("enrich")]
    assert not [contract for contract in summary.edge_contracts if contract.from_id == "enrich"]
    assert admitted.is_valid, [error.message for error in admitted.errors]
    assert not refused.is_valid
    assert any("resolved against none of the 1 reference entries" in error.message for error in refused.errors)


# ---------------------------------------------------------------------------
# (d) An error at or under a valid marker is the marker's, not the config's
# ---------------------------------------------------------------------------

_MARKER = {"blob_ref": str(TABLE_BLOB), "mode": "inline_content", "sha256": "a" * 64}

# MEASURED (stage 2a scan over every transform's ``probe_config()`` string
# leaf): the positions where a marker's ONLY errors were consequences of the
# marker, yet the string-type rule alone called it a config failure.
_CONSEQUENCE_POSITIONS = [
    ("keyword_filter", ("fields", 0)),
    ("azure_content_safety", ("fields", 0)),
    ("azure_prompt_shield", ("fields", 0)),
    ("batch_duplicate_annotator", ("rules", 0, "name")),
    ("batch_duplicate_annotator", ("rules", 0, "cells", 0, "field")),
    ("blob_fetch", ("http", "allowed_hosts", 0)),
    ("web_scrape", ("http", "allowed_hosts", 0)),
]


def _probe_options_with_marker(plugin: str, path: tuple[str | int, ...]) -> dict[str, Any]:
    [cls] = [cls for cls in get_shared_plugin_manager().get_transforms() if cls.name == plugin]
    options: dict[str, Any] = cls.probe_config()
    container: Any = options
    for segment in path[:-1]:
        container = container[segment]
    assert type(container[path[-1]]) is str, "the scan measured a string leaf here"
    container[path[-1]] = dict(_MARKER)
    return options


def _probe_warnings(tmp_path: Path, plugin: str, options: dict[str, Any]) -> set[str | None]:
    """Warnings of a one-node graph whose sink asks the node's vote, so a failed probe is reported."""
    state = _graph(
        tmp_path,
        _transform("probe_me", plugin, "src_out", "output", options),
        source_schema=FIXED_SOURCE,
        sink_schema={"mode": "observed", "required_fields": ["id"]},
    )
    return {entry.error_code for entry in state.validate().warnings}


@pytest.mark.parametrize(
    ("plugin", "path"), _CONSEQUENCE_POSITIONS, ids=lambda value: ".".join(map(str, value)) if type(value) is tuple else value
)
def test_a_union_branch_or_min_length_error_caused_by_a_marker_defers(tmp_path: Path, plugin: str, path: tuple[str | int, ...]) -> None:
    warnings = _probe_warnings(tmp_path, plugin, _probe_options_with_marker(plugin, path))

    assert DEFERRED in warnings
    assert "contract_probe_failed" not in warnings


@pytest.mark.parametrize(
    ("plugin", "path"), _CONSEQUENCE_POSITIONS[:4], ids=lambda value: ".".join(map(str, value)) if type(value) is tuple else value
)
def test_a_real_second_error_beside_the_marker_is_still_a_config_failure(tmp_path: Path, plugin: str, path: tuple[str | int, ...]) -> None:
    options = _probe_options_with_marker(plugin, path)
    options["not_an_option"] = "x"

    warnings = _probe_warnings(tmp_path, plugin, options)

    assert "contract_probe_failed" in warnings
    assert DEFERRED not in warnings


@pytest.mark.parametrize(
    ("label", "errors", "options", "expected"),
    [
        pytest.param(
            "union sibling",
            [
                {"type": "string_type", "loc": ("fields", "str"), "input": [_MARKER]},
                {"type": "string_type", "loc": ("fields", "list[str]", 0), "input": _MARKER},
            ],
            {"fields": [_MARKER]},
            True,
            id="union-sibling",
        ),
        pytest.param(
            "min length",
            [
                {"type": "string_type", "loc": ("rules", 0, "name"), "input": _MARKER},
                {"type": "too_short", "loc": ("rules",), "input": [{"name": _MARKER}], "ctx": {"min_length": 1, "actual_length": 0}},
            ],
            {"rules": [{"name": _MARKER}]},
            True,
            id="min-length-ancestor",
        ),
        # Short whatever the file says: one item, two required. Reading the
        # marker cannot make the list long enough, so this is a config error.
        pytest.param(
            "min length the marker cannot meet",
            [
                {"type": "string_type", "loc": ("rules", 0, "name"), "input": _MARKER},
                {"type": "too_short", "loc": ("rules",), "input": [{"name": _MARKER}], "ctx": {"min_length": 2, "actual_length": 0}},
            ],
            {"rules": [{"name": _MARKER}]},
            False,
            id="too-short-whatever-the-file-says",
        ),
        pytest.param(
            "too_short without its context",
            [
                {"type": "string_type", "loc": ("rules", 0, "name"), "input": _MARKER},
                {"type": "too_short", "loc": ("rules",), "input": [{"name": _MARKER}]},
            ],
            {"rules": [{"name": _MARKER}]},
            False,
            id="too-short-without-min-length-is-not-assumed",
        ),
        # A union branch error is explained only from a DIFFERENT branch of
        # the marker's union: a union passes as soon as one branch does. A real
        # error in the marker's own branch survives the read and stays real.
        pytest.param(
            "literal error in the marker's own branch",
            [
                {
                    "type": "literal_error",
                    "loc": ("http", "allowed_hosts", "literal['public_only','allow_private']"),
                    "input": [_MARKER, "x"],
                },
                {"type": "string_type", "loc": ("http", "allowed_hosts", "list[str]", 0), "input": _MARKER},
                {"type": "literal_error", "loc": ("http", "allowed_hosts", "list[str]", 1), "input": "x"},
            ],
            {"http": {"allowed_hosts": [_MARKER, "x"]}},
            False,
            id="union-same-branch-literal-error",
        ),
        pytest.param(
            "missing key in the marker's own branch",
            [
                {"type": "string_type", "loc": ("source", "FileSource", "path"), "input": _MARKER},
                {"type": "missing", "loc": ("source", "FileSource", "format"), "input": {"path": _MARKER}},
                {"type": "missing", "loc": ("source", "UrlSource", "url"), "input": {"path": _MARKER}},
            ],
            {"source": {"path": _MARKER}},
            False,
            id="union-same-branch-missing",
        ),
        pytest.param(
            "missing key only in the other branch",
            [
                {"type": "string_type", "loc": ("source", "FileSource", "path"), "input": _MARKER},
                {"type": "missing", "loc": ("source", "UrlSource", "url"), "input": {"path": _MARKER}},
            ],
            {"source": {"path": _MARKER}},
            True,
            id="union-other-branch-missing",
        ),
        pytest.param(
            "missing sibling key",
            [
                {"type": "string_type", "loc": ("files", "table", "content"), "input": _MARKER},
                {"type": "missing", "loc": ("files", "table", "format"), "input": {"content": _MARKER}},
            ],
            {"files": {"table": {"content": _MARKER}}},
            False,
            id="missing-sibling-is-not-a-union-branch",
        ),
        pytest.param(
            "too_short elsewhere",
            [
                {"type": "string_type", "loc": ("fields", 0), "input": _MARKER},
                {"type": "too_short", "loc": ("rules",), "input": []},
            ],
            {"fields": [_MARKER], "rules": []},
            False,
            id="too-short-on-a-container-without-the-marker",
        ),
        pytest.param(
            "second error",
            [
                {"type": "string_type", "loc": ("fields", 0), "input": _MARKER},
                {"type": "int_parsing", "loc": ("limit",), "input": "many"},
            ],
            {"fields": [_MARKER], "limit": "many"},
            False,
            id="second-real-error",
        ),
        pytest.param(
            "marker in a literal",
            [{"type": "literal_error", "loc": ("reference_format",), "input": _MARKER}],
            {"reference_format": _MARKER},
            False,
            id="literal-position",
        ),
        pytest.param(
            "schema mode",
            [{"type": "value_error", "loc": ("schema",), "input": {"mode": _MARKER}}],
            {"schema": {"mode": _MARKER}},
            False,
            id="validator-error-on-the-container",
        ),
        pytest.param("no errors", [], {"fields": [_MARKER]}, False, id="no-errors"),
    ],
)
def test_only_marker_errors_counts_consequences_of_a_marker_and_nothing_else(
    label: str, errors: list[dict[str, Any]], options: dict[str, Any], expected: bool
) -> None:
    from elspeth.web.composer._validation_probe import only_marker_errors

    assert only_marker_errors(cast("list[ErrorDetails]", errors), options) is expected, label


# ---------------------------------------------------------------------------
# The names a deferring node's config fixes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plugin", "options", "expected"),
    [
        pytest.param("reference_join", {"output": {"b": "x", "a": "y"}}, frozenset({"a", "b"}), id="join-single"),
        pytest.param(
            "reference_join",
            {"lookups": [{"output": {"a": "x"}}, {"output": {"b": "y"}}, "not-a-lookup"]},
            frozenset({"a", "b"}),
            id="join-multi",
        ),
        pytest.param("reference_join", {"output": {}}, None, id="join-empty"),
        pytest.param("reference_join", {"output": "not-a-mapping"}, None, id="join-malformed"),
        pytest.param("file_inject", {"mode": "select", "output_field": "guide"}, frozenset({"guide"}), id="inject-select"),
        pytest.param(
            "file_inject",
            {"mode": "all", "files": {"x": {"output_field": "a"}, "y": {"output_field": "b"}}},
            frozenset({"a", "b"}),
            id="inject-all",
        ),
        pytest.param("file_inject", {"mode": "select"}, None, id="inject-select-without-field"),
        pytest.param("field_mapper", {"mapping": {"a": "b"}}, None, id="other-plugin"),
        pytest.param("reference_join", ["not", "a", "mapping"], None, id="not-options"),
    ],
)
def test_deferred_created_fields_reads_only_names_the_config_fixes(plugin: str, options: object, expected: frozenset[str] | None) -> None:
    from elspeth.web.composer._validation_probe import deferred_created_fields

    assert deferred_created_fields(plugin, options) == expected


# ---------------------------------------------------------------------------
# The tool result carries the notice beside is_valid: true
# ---------------------------------------------------------------------------


def test_the_notice_rides_in_the_same_tool_result_as_is_valid(tmp_path: Path, blob_env: dict[str, Any]) -> None:
    """Required: the planner reads the tool result, not the state object (``ToolResult.to_dict``)."""
    state = _graph(
        tmp_path,
        _join_node("enrich", "src_out", "output", _join(CSV_TABLE, "csv")),
        source_schema=FIXED_SOURCE,
        sink_schema={"mode": "observed", "required_fields": ["k"]},
    )
    uploaded = upload_tools._upload(blob_env, content=CSV_TABLE)

    result = upload_tools._execute(
        "wire_blob_inline_ref",
        {"field_path": "node:enrich.options.reference_content", "blob_id": uploaded["blob_ref"]},
        state,
        blob_env,
    )
    payload = result.to_dict()

    assert result.success is True, payload
    [node] = [node for node in result.updated_state.nodes if node.id == "enrich"]
    assert deep_thaw(node.options)["reference_content"] == uploaded
    assert payload["validation"]["is_valid"] is True, payload["validation"]["errors"]
    notices = [warning for warning in payload["validation"]["warnings"] if warning["error_code"] == DEFERRED]
    assert [(notice["component"], notice["message"], notice["severity"]) for notice in notices] == [
        ("node:enrich", _join_notice("enrich"), "medium")
    ]


# ---------------------------------------------------------------------------
# Required-control wiring over an honestly deferring node
# ---------------------------------------------------------------------------


def _join_fed_chain(tmp_path: Path) -> CompositionState:
    """The stage-1 llm chain with its ``file_inject`` replaced by a ``reference_join`` whose table is a file.

    The llm reads only upstream fields by name (``remark_one``); requiring the
    joined field too is left to the preflight since stage 2b
    (``test_deferred_possible_additions.py``).
    """
    state = chain_fixtures._profiled(chain_fixtures._chain(tmp_path, inject_file=False, prompt_files=True, query_file=True))
    table = "kind,rubric\nround,Grade round gadgets gently.\n"
    join = _transform(
        "join_rubric",
        "reference_join",
        "inject_in",
        "assess_in",
        {
            "schema": OBSERVED,
            "reference_content": _marker(table),
            "reference_format": "csv",
            "key_field": "gadget_kind",
            "reference_key_name": "kind",
            "output": {"rubric_text": "ref['rubric']"},
        },
    )
    nodes = []
    for node in state.nodes:
        if node.id == "inject_rubric":
            nodes.append(join)
        elif node.id == "assess_gadget":
            nodes.append(replace(node, options={**node.options, "required_input_fields": ["remark_one"]}))
        else:
            nodes.append(node)
    return replace(state, nodes=tuple(nodes))


def test_required_controls_wire_over_a_deferring_join_and_the_wired_graph_is_valid_with_the_notice(tmp_path: Path) -> None:
    """Wiring (b): every wiring call site re-validates and raises ``AuditIntegrityError`` on an invalid wired graph."""
    from elspeth.web.composer.required_controls import wire_required_controls_state
    from tests.unit.web.composer.test_planner_authoring_aids import _guardrail_profile_view

    view, snapshot = _guardrail_profile_view(tmp_path)
    state = _join_fed_chain(tmp_path)

    before = view.validate_composition_state(state).validation
    wired = wire_required_controls_state(state, snapshot, view)
    after = view.validate_composition_state(wired).validation

    assert before.is_valid, [entry.to_dict() for entry in before.errors]
    inserted = [node.plugin for node in wired.nodes if node.id not in {existing.id for existing in state.nodes}]
    assert inserted, "the REQUIRED controls were wired in"
    assert after.is_valid, [entry.to_dict() for entry in after.errors]
    assert _notices(after) == [_join_notice("join_rubric", "rubric_text")]


def test_a_real_missing_field_still_leaves_the_wired_deferring_chain_invalid(tmp_path: Path) -> None:
    """Wiring (c): the deferral repairs nothing; a field nobody writes is still refused after wiring."""
    from elspeth.web.composer.required_controls import wire_required_controls_state
    from tests.unit.web.composer.test_planner_authoring_aids import _guardrail_profile_view

    view, snapshot = _guardrail_profile_view(tmp_path)
    state = _join_fed_chain(tmp_path)
    sink = state.outputs[0]
    fields = [*sink.options["schema"]["fields"], "never_written: str"]
    state = replace(state, outputs=(replace(sink, options={**sink.options, "schema": {"mode": "flexible", "fields": fields}}),))

    wired = wire_required_controls_state(state, snapshot, view)
    after = view.validate_composition_state(wired).validation

    assert not after.is_valid
    assert any("never_written" in entry.message for entry in after.errors)
    assert _notices(after) == [_join_notice("join_rubric", "rubric_text")]
