# tests/integration/pipeline/test_row_type_violation_routing.py
"""Operator-level proof that a wrong-typed row value ROUTES (elspeth-5887fb7928).

The per-plugin unit tests assert the RETURNED shape. This file asserts the
half that shape exists for: that the engine actually routes it, counts the row,
and leaves no token undecided — the operator-visible triple the defect broke.

Before the fix each plugin raised a bare ``TypeError``. ``TypeError`` matches no
clause in ``RowProcessor._execute_transform_with_retry`` and nothing in
``engine/`` converts it, so a ROW-level failure aborted the RUN: ``on_error``
never fired, the row was counted as neither succeeded nor failed, and the
operator got a raw traceback at exit 4. All three symptoms are asserted against
here — the row reaches its configured sink, the run reports it failed, and
``Orchestrator.run`` returns a result instead of raising.

Companion to ``test_retry.py::test_tier_2_contract_violation_routes_to_named_error_sink``,
which pins the same triple for the engine-raised Tier-2 violation. This one
pins it for the plugin-returned row-level error, across every per-row site the
ticket covers.
"""

from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import RunStatus, TerminalOutcome, TerminalPath
from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.wiring import WiredTransform
from elspeth.core.landscape.schema import token_outcomes_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.testing import make_pipeline_row
from tests.fixtures.base_classes import as_sink, as_source, as_transform
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import make_landscape_db
from tests.fixtures.plugins import CollectSink, ListSource

DYNAMIC_SCHEMA = {"mode": "observed"}


def _build_transform(plugin_id: str) -> BaseTransform:
    """Construct the plugin under test with the minimum viable config."""
    if plugin_id == "json_explode":
        from elspeth.plugins.transforms.json_explode import JSONExplode

        return JSONExplode({"schema": DYNAMIC_SCHEMA, "array_field": "items"})
    if plugin_id == "line_explode":
        from elspeth.plugins.transforms.line_explode import LineExplode

        return LineExplode({"schema": DYNAMIC_SCHEMA, "source_field": "html", "output_field": "html_line"})
    if plugin_id == "blob_csv_expand":
        from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand

        return BlobCSVExpand({"schema": DYNAMIC_SCHEMA, "blob_ref_field": "blob_ref"})
    if plugin_id == "blob_json_expand":
        from elspeth.plugins.transforms.blob_json_expand import BlobJSONExpand

        return BlobJSONExpand({"schema": DYNAMIC_SCHEMA, "blob_ref_field": "blob_ref", "fields": ["value"]})
    if plugin_id == "blob_text_expand":
        from elspeth.plugins.transforms.blob_text_expand import BlobTextExpand

        return BlobTextExpand({"schema": DYNAMIC_SCHEMA, "blob_ref_field": "blob_ref"})
    raise AssertionError(f"unknown plugin id {plugin_id!r}")


# (plugin id, the row whose value has the WRONG TYPE, the error_type it reports).
#
# The rows differ on purpose: there is no uniform "bad value" for this
# population. json_explode wants a list and is handed a str; line_explode wants
# a str and is handed a list (deep-frozen to a tuple); blob_csv_expand wants a
# str payload hash and is handed an int. A single fixture shape would arm none
# of the others — the same trap recorded on the ticket for the batch half.
# ``reported_type`` is the name the OPERATOR sees, which is not always
# ``type(value).__name__``: PipelineRow deep-freezes the row, so a list arrives
# as a tuple and a dict as a mappingproxy. Carried explicitly so the freeze
# wrapper changing surfaces here rather than in a quarantine record.
_CASES = [
    pytest.param("json_explode", {"id": 1, "items": "abc"}, "wrong_type", "str", id="json_explode-str-for-list"),
    pytest.param("line_explode", {"id": 1, "html": ["not", "a", "string"]}, "wrong_type", "tuple", id="line_explode-list-for-str"),
    pytest.param("blob_csv_expand", {"id": 1, "blob_ref": 12}, "non_string_ref", "int", id="blob_csv_expand-int-for-ref"),
    # blob_json_expand declares `blob_content_type` alongside the ref, so the
    # row carries it: the engine's declared-required-fields gate fires BEFORE
    # the plugin and would abort on the missing column instead of exercising
    # the routing this file exists to prove.
    pytest.param(
        "blob_json_expand",
        {"id": 1, "blob_ref": 12, "blob_content_type": "application/json"},
        "non_string_ref",
        "int",
        id="blob_json_expand-int-for-ref",
    ),
    pytest.param("blob_text_expand", {"id": 1, "blob_ref": 12}, "non_string_ref", "int", id="blob_text_expand-int-for-ref"),
]


def _build_pipeline(
    transform: BaseTransform,
    row: dict[str, Any],
    *,
    on_error: str,
) -> tuple[dict[str, CollectSink], ExecutionGraph, ElspethSettings, PipelineConfig]:
    source_name = "typed_source"
    connection = "typed_source_out"
    source = ListSource([row], name="typed_source_plugin", on_success=connection)
    transform.on_success = "output"
    transform.on_error = on_error

    transform_settings = TransformSettings(
        name="type_violation",
        plugin=transform.name,
        input=connection,
        on_success="output",
        on_error=on_error,
        options={},
    )
    source_settings = SourceSettings(plugin=source.name, on_success=connection, options={})
    sinks = {"output": CollectSink("output"), "quarantine": CollectSink("quarantine")}

    graph = ExecutionGraph.from_plugin_instances(
        sources={source_name: as_source(source)},
        source_settings_map={source_name: source_settings},
        transforms=[WiredTransform(plugin=as_transform(transform), settings=transform_settings)],
        sinks={name: as_sink(sink) for name, sink in sinks.items()},
        aggregations={},
        gates=[],
    )
    settings = ElspethSettings(
        sources={source_name: source_settings},
        transforms=[transform_settings],
        sinks={name: SinkSettings(plugin=sink.name, options={}, on_write_failure="discard") for name, sink in sinks.items()},
    )
    config = PipelineConfig(
        sources={source_name: as_source(source)},
        transforms=[as_transform(transform)],
        sinks={name: as_sink(sink) for name, sink in sinks.items()},
    )
    return sinks, graph, settings, config


@pytest.mark.parametrize(("plugin_id", "row", "expected_error_type", "reported_type"), _CASES)
def test_wrong_typed_row_value_routes_to_the_named_error_sink(
    plugin_id: str,
    row: dict[str, Any],
    expected_error_type: str,
    reported_type: str,
    tmp_path: Any,
) -> None:
    """The row reaches on_error, is COUNTED failed, and leaves no pending token."""
    db = make_landscape_db()
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    sinks, graph, settings, config = _build_pipeline(_build_transform(plugin_id), row, on_error="quarantine")

    # Symptom 3: this returns a result. Before the fix it raised past every
    # catch site and the operator saw a traceback instead of a run outcome.
    result = Orchestrator(db).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=payload_store,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )

    # Symptom 2: the row is COUNTED. The defect reported it as neither
    # succeeded nor failed — the ticket's "0 failed" on a run where the only
    # row failed.
    assert result.status is RunStatus.FAILED
    assert result.rows_processed == 1
    assert result.rows_failed == 1

    # Symptom 1: on_error FIRES. The defect never wrote the error sink at all.
    assert sinks["output"].results == []
    assert sinks["quarantine"].results == [row]

    # And the token is DECIDED. The defect left it pending forever, which is
    # what made the failed rows unattributable in the audit trail.
    with db.engine.connect() as conn:
        outcomes = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == result.run_id)).all()

    assert len(outcomes) == 1
    [outcome] = outcomes
    assert (outcome.outcome, outcome.path, outcome.sink_name, outcome.completed) == (
        TerminalOutcome.FAILURE.value,
        TerminalPath.ON_ERROR_ROUTED.value,
        "quarantine",
        1,
    )
    assert not [o for o in outcomes if o.completed == 0], "a finished run must leave zero pending outcomes"


@pytest.mark.parametrize(("plugin_id", "row", "expected_error_type", "reported_type"), _CASES)
def test_wrong_typed_row_value_is_reported_with_its_type_and_never_its_value(
    plugin_id: str,
    row: dict[str, Any],
    expected_error_type: str,
    reported_type: str,
    tmp_path: Any,
) -> None:
    """The routed reason names the field and the TYPE, and omits the value.

    The value is Tier-2/3 row content. Reporting it would leak row data into
    the audit trail, the bug family `batch_replicate` documents at its own
    quarantine site ("record the row INDEX for traceability, never the row
    body"). `llm/image_inputs.py` established this shape for payload refs and
    the two blob plugins follow it.
    """
    transform = _build_transform(plugin_id)
    if plugin_id == "blob_csv_expand":
        transform._payload_store = FilesystemPayloadStore(tmp_path / "payloads")

    result = transform.process(make_pipeline_row(row), make_context())

    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == expected_error_type
    assert result.rows is None

    bad_field = next(k for k in row if k != "id")
    assert result.reason["field"] == bad_field
    # The TYPE is named; the VALUE never appears anywhere in the reason.
    rendered = repr(sorted(result.reason.items()))
    assert reported_type in rendered
    assert repr(row[bad_field]) not in rendered


# ---------------------------------------------------------------------------
# The batch half: a wrongly-typed row at an AGGREGATION fails the whole batch,
# and the batch goes to the aggregation's on_error (elspeth-d2e3f29d10).
# ---------------------------------------------------------------------------

# Observed CSV: every value arrives as ``str``, so batch_stats' numeric guard
# fires on the first row of the batch (BATCH index 0). The aggregation's own
# schema is observed as well — a fixed one would re-coerce the strings or trip
# the engine's batch-input validation, and this test would arm nothing.
_CSV_ROWS = (("1", "12.5"), ("2", "not-a-number"), ("3", "31.75"))
_CSV_VALUES = tuple(amount for _id, amount in _CSV_ROWS)


def _read_jsonl(path: Any) -> list[dict[str, Any]]:
    import json

    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _run_csv_batch_stats_pipeline(tmp_path: Any, *, on_error: str, trigger: str) -> tuple[Any, Any, Any, Any]:
    """Real CSV source (observed) -> batch_stats aggregation -> JSON sinks.

    Built through the production assembly path (settings YAML ->
    instantiate_plugins_from_config -> ExecutionGraph.from_plugin_instances ->
    assemble_and_validate_pipeline_config -> Orchestrator), so the DAG builder
    wires (or refuses) the aggregation error edge exactly as ``elspeth run``.
    """
    from elspeth.cli_helpers import instantiate_plugins_from_config
    from elspeth.config_loading import load_settings_from_yaml_string
    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config

    input_path = tmp_path / "amounts.csv"
    input_path.write_text("id,amount\n" + "".join(f"{row_id},{amount}\n" for row_id, amount in _CSV_ROWS))
    output_path = tmp_path / "stats.jsonl"
    quarantine_path = tmp_path / "quarantine.jsonl"
    trigger_line = "    trigger:\n      count: 3\n" if trigger == "count" else ""
    quarantine_sink = (
        f"""
  quarantine:
    plugin: json
    on_write_failure: discard
    options:
      path: {quarantine_path}
      format: jsonl
      schema:
        mode: observed
"""
        if on_error == "quarantine"
        else ""
    )
    settings = load_settings_from_yaml_string(
        f"""
sources:
  amounts:
    plugin: csv
    on_success: stats_in
    options:
      path: {input_path}
      on_validation_failure: discard
      schema:
        mode: observed
aggregations:
  - name: stats
    plugin: batch_stats
    input: stats_in
    on_success: output
    on_error: {on_error}
{trigger_line}    options:
      schema:
        mode: observed
      value_field: amount
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema:
        mode: observed
{quarantine_sink}
"""
    )
    bundle = instantiate_plugins_from_config(settings)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
    )
    # The structural check `elspeth run` / `elspeth validate` apply: before the
    # aggregation error edge existed, a named on_error sink failed here as an
    # unreachable node.
    graph.validate()
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
    )
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    result = Orchestrator(db).run(config, graph=graph, settings=settings, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))
    return result, db, _read_jsonl(output_path), _read_jsonl(quarantine_path)


def _expected_batch_reason() -> dict[str, Any]:
    """The reason batch_stats returns for this input: row 0's str amount."""
    from elspeth.plugins.transforms._batch_row_types import BatchRowTypeError

    return dict(BatchRowTypeError(field="amount", row_index=0, expected="numeric (int or float)", found="str").as_reason())


def _assert_value_free(text: str) -> None:
    for value in _CSV_VALUES:
        assert value not in text, f"row value {value!r} leaked into the audit record: {text}"


def _failed_flush_audit(db: Any, run_id: str) -> dict[str, Any]:
    """Every audit row the failed flush wrote, keyed for assertions."""
    from elspeth.core.landscape.schema import (
        batches_table,
        edges_table,
        node_states_table,
        nodes_table,
        routing_events_table,
        tokens_table,
        transform_errors_table,
    )

    with db.engine.connect() as conn:
        agg_node_ids = (
            conn.execute(
                select(nodes_table.c.node_id).where(nodes_table.c.run_id == run_id).where(nodes_table.c.node_type == "aggregation")
            )
            .scalars()
            .all()
        )
        [agg_node_id] = agg_node_ids
        return {
            "agg_node_id": agg_node_id,
            "outcomes": conn.execute(
                select(token_outcomes_table).where(token_outcomes_table.c.run_id == run_id).where(token_outcomes_table.c.completed == 1)
            ).all(),
            "row_token_ids": set(conn.execute(select(tokens_table.c.token_id).where(tokens_table.c.run_id == run_id)).scalars()),
            "failed_states": conn.execute(
                select(node_states_table)
                .where(node_states_table.c.run_id == run_id)
                .where(node_states_table.c.node_id == agg_node_id)
                .where(node_states_table.c.status == "failed")
            ).all(),
            "routing": conn.execute(
                select(routing_events_table, edges_table.c.label, edges_table.c.from_node_id, edges_table.c.to_node_id)
                .join(edges_table, edges_table.c.edge_id == routing_events_table.c.edge_id)
                .where(routing_events_table.c.run_id == run_id)
            ).all(),
            "batches": conn.execute(select(batches_table).where(batches_table.c.run_id == run_id)).all(),
            "transform_errors": conn.execute(select(transform_errors_table).where(transform_errors_table.c.run_id == run_id)).all(),
        }


@pytest.mark.parametrize("trigger", ["count", "end_of_source"])
def test_wrong_typed_row_at_an_aggregation_routes_the_whole_batch_to_on_error(trigger: str, tmp_path: Any) -> None:
    """The batch is reassembled and quarantined: every buffered row reaches the
    on_error sink with its ORIGINAL values, each token is decided once as
    ``(failure, on_error_routed, 'quarantine')``, and the record says which
    row, which field, and the expected vs found type — never the value.

    ``count`` flushes at intake (barrier coordination consumes the results);
    ``end_of_source`` flushes from the orchestrator. Both reach the one
    failed-flush seam, and both must route.

    Before elspeth-d2e3f29d10 this pipeline could not be built at all: no
    aggregation error edge existed, so the quarantine sink was an
    unreachable node and ``from_plugin_instances``/``validate`` refused it.
    """
    import json

    from elspeth.engine._error_hash import compute_error_hash

    result, db, output_rows, quarantine_rows = _run_csv_batch_stats_pipeline(tmp_path, on_error="quarantine", trigger=trigger)

    assert result.status is RunStatus.FAILED
    assert (result.rows_processed, result.rows_succeeded, result.rows_failed) == (3, 0, 3)
    assert result.rows_routed_failure == 3
    assert result.rows_quarantined == 0

    # Reassembled best effort: every buffered row, original values, in order.
    assert quarantine_rows == [{"id": row_id, "amount": amount} for row_id, amount in _CSV_ROWS]
    assert output_rows == []

    audit = _failed_flush_audit(db, result.run_id)
    reason = _expected_batch_reason()
    reason_text = str(reason)

    # One terminal per token, written by the sink after durability; batch_id
    # stays on batch_members, not on the routed outcome.
    assert len(audit["outcomes"]) == 3
    assert {outcome.token_id for outcome in audit["outcomes"]} == audit["row_token_ids"]
    for outcome in audit["outcomes"]:
        assert (outcome.outcome, outcome.path, outcome.sink_name, outcome.completed) == (
            TerminalOutcome.FAILURE.value,
            TerminalPath.ON_ERROR_ROUTED.value,
            "quarantine",
            1,
        )
        assert outcome.batch_id is None
        assert outcome.error_hash == compute_error_hash(reason_text, exception_type="TransformError")

    # The journal handoff carried the same scrubbed reason to the sink.
    from elspeth.core.landscape.schema import token_work_items_table

    with db.engine.connect() as conn:
        handoffs = conn.execute(
            select(token_work_items_table.c.token_id, token_work_items_table.c.pending_error_message)
            .where(token_work_items_table.c.run_id == result.run_id)
            .where(token_work_items_table.c.pending_path == TerminalPath.ON_ERROR_ROUTED.value)
        ).all()
    assert {handoff.token_id for handoff in handoffs} == audit["row_token_ids"]
    for handoff in handoffs:
        assert handoff.pending_error_message == reason_text
        _assert_value_free(handoff.pending_error_message)

    # The flush node_state failed with the scrubbed reason DICT (not a repr).
    [failed_state] = audit["failed_states"]
    stored_reason = json.loads(failed_state.error_json)
    assert stored_reason == reason
    assert stored_reason["field"] == "amount"
    assert (stored_reason["expected"], stored_reason["actual_type"]) == ("numeric (int or float)", "str")
    assert "in row 0" in stored_reason["error"]
    _assert_value_free(failed_state.error_json)

    # Exactly one DIVERT routing_event, on the flush state, along __error_stats__.
    [routing] = audit["routing"]
    assert routing.mode == "divert"
    assert routing.label == "__error_stats__"
    assert routing.from_node_id == audit["agg_node_id"]
    assert routing.state_id == failed_state.state_id

    [batch] = audit["batches"]
    assert batch.status == "failed"
    assert batch.trigger_type == trigger

    # One transform_errors row per buffered token, same reason, own row.
    assert len(audit["transform_errors"]) == 3
    assert {row.token_id for row in audit["transform_errors"]} == audit["row_token_ids"]
    for error_row in audit["transform_errors"]:
        assert error_row.transform_id == audit["agg_node_id"]
        assert error_row.destination == "quarantine"
        assert json.loads(error_row.error_details_json) == reason
        _assert_value_free(error_row.error_details_json)
    assert sorted(json.loads(row.row_data_json)["amount"] for row in audit["transform_errors"]) == sorted(_CSV_VALUES)


@pytest.mark.parametrize("trigger", ["count", "end_of_source"])
def test_wrong_typed_row_at_a_discard_aggregation_records_every_member_without_routing(trigger: str, tmp_path: Any) -> None:
    """Negative control for the route above: ``on_error: discard`` writes no
    sink and no DIVERT, but still decides every member once and records, per
    member, that the batch failed and why (transform_errors, destination
    'discard'), with an error_hash that binds to the reason — not to a
    constant shared by every failed batch."""
    import json

    from elspeth.engine._error_hash import compute_error_hash

    result, db, output_rows, quarantine_rows = _run_csv_batch_stats_pipeline(tmp_path, on_error="discard", trigger=trigger)

    assert result.status is RunStatus.FAILED
    assert (result.rows_processed, result.rows_succeeded, result.rows_failed) == (3, 0, 3)
    assert result.rows_routed_failure == 0
    assert result.rows_quarantined == 0
    assert output_rows == []
    assert quarantine_rows == []

    audit = _failed_flush_audit(db, result.run_id)
    reason = _expected_batch_reason()

    assert len(audit["outcomes"]) == 3
    assert {outcome.token_id for outcome in audit["outcomes"]} == audit["row_token_ids"]
    for outcome in audit["outcomes"]:
        assert (outcome.outcome, outcome.path, outcome.sink_name, outcome.completed) == (
            TerminalOutcome.FAILURE.value,
            TerminalPath.UNROUTED.value,
            None,
            1,
        )
        assert outcome.error_hash == compute_error_hash(str(reason), exception_type="TransformError")

    assert audit["routing"] == []
    [failed_state] = audit["failed_states"]
    assert json.loads(failed_state.error_json) == reason
    [batch] = audit["batches"]
    assert batch.status == "failed"

    assert len(audit["transform_errors"]) == 3
    assert {row.token_id for row in audit["transform_errors"]} == audit["row_token_ids"]
    assert {row.destination for row in audit["transform_errors"]} == {"discard"}
