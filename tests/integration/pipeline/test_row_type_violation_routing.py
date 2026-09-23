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

from elspeth.contracts import Determinism, PluginSchema, RunStatus, TerminalOutcome, TerminalPath, TransformResult
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.canonical import canonical_json
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


def _run_csv_batch_stats_pipeline(tmp_path: Any, *, on_error: str, trigger: str, db: Any | None = None) -> tuple[Any, Any, Any, Any]:
    """Real CSV source (observed) -> batch_stats aggregation -> JSON sinks.

    Built through the production assembly path (settings YAML ->
    instantiate_plugins_from_config -> ExecutionGraph.from_plugin_instances ->
    assemble_and_validate_pipeline_config -> Orchestrator), so the DAG builder
    wires (or refuses) the aggregation error edge exactly as ``elspeth run``.
    ``db`` defaults to a file SQLite Landscape under ``tmp_path``; the
    PostgreSQL proof passes its own.
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
    if db is None:
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
    # Rendered as the recorded reason: its canonical JSON, the text a resume reads back.
    reason_text = canonical_json(reason)

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
def test_wrong_typed_row_at_a_discard_aggregation_quarantines_every_member_without_routing(trigger: str, tmp_path: Any) -> None:
    """Negative control for the route above, and operator ruling B3.

    ``on_error: discard`` writes no sink and no DIVERT, and matches the
    per-row discard: every member is ``(failure, quarantined_at_source)`` —
    counted quarantined, so a run whose every row was discarded is
    COMPLETED_WITH_FAILURES exactly as for a per-row discard — with an
    error_hash that binds to the batch reason (B4), not to a constant shared
    by every failed batch. Each member still records that the batch failed and
    why (transform_errors, destination 'discard'; B5).
    """
    import json

    from elspeth.engine._error_hash import compute_error_hash

    result, db, output_rows, quarantine_rows = _run_csv_batch_stats_pipeline(tmp_path, on_error="discard", trigger=trigger)

    assert result.status is RunStatus.COMPLETED_WITH_FAILURES
    assert (result.rows_processed, result.rows_succeeded, result.rows_failed) == (3, 0, 3)
    assert result.rows_quarantined == 3
    assert result.rows_routed_failure == 0
    assert output_rows == []
    assert quarantine_rows == []

    audit = _failed_flush_audit(db, result.run_id)
    reason = _expected_batch_reason()

    assert len(audit["outcomes"]) == 3
    assert {outcome.token_id for outcome in audit["outcomes"]} == audit["row_token_ids"]
    for outcome in audit["outcomes"]:
        assert (outcome.outcome, outcome.path, outcome.sink_name, outcome.completed) == (
            TerminalOutcome.FAILURE.value,
            TerminalPath.QUARANTINED_AT_SOURCE.value,
            None,
            1,
        )
        assert outcome.error_hash == compute_error_hash(canonical_json(reason))

    assert audit["routing"] == []
    [failed_state] = audit["failed_states"]
    assert json.loads(failed_state.error_json) == reason
    [batch] = audit["batches"]
    assert batch.status == "failed"

    assert len(audit["transform_errors"]) == 3
    assert {row.token_id for row in audit["transform_errors"]} == audit["row_token_ids"]
    assert {row.destination for row in audit["transform_errors"]} == {"discard"}


# ---------------------------------------------------------------------------
# The engine-raised half: a row that fails a transform's DECLARED input schema.
# The engine raises a Tier-2 ``PluginContractViolation`` and the processor
# routes it, recording the violation's message as the routed reason. Pydantic's
# ``str(ValidationError)`` echoes ``input_value=...``, so the message must be
# rendered value-free at the raise site (C4).
# ---------------------------------------------------------------------------

_SENTINEL = "SENTINEL-value-7f3a91"


class _StrictAmountSchema(PluginSchema):
    """A declared input contract the sentinel row cannot meet: ``amount`` is an int."""

    amount: int


class _StrictInputTransform(BaseTransform):
    """Declares ``amount: int``; the engine's input preflight rejects a str amount."""

    name = "strict_input_transform"
    determinism = Determinism.DETERMINISTIC
    input_schema = _StrictAmountSchema
    output_schema = PluginSchema
    plugin_version = "1.0.0"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__({"schema": {"mode": "observed"}, **config})
        self.process_count = 0

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        self.process_count += 1
        return TransformResult.success(row, success_reason={"action": "never reached"})


def _audit_cells_containing(db: Any, needle: str) -> list[tuple[str, str]]:
    """Every (table, column) text cell of the Landscape that contains ``needle``."""
    from sqlalchemy import inspect, text

    hits: list[tuple[str, str]] = []
    with db.engine.connect() as conn:
        inspector = inspect(conn)
        for table in inspector.get_table_names():
            columns = [column["name"] for column in inspector.get_columns(table)]
            for row in conn.execute(text(f'SELECT * FROM "{table}"')).mappings():
                hits.extend((table, column) for column in columns if type(row[column]) is str and needle in row[column])
    return hits


def test_a_row_failing_a_declared_input_schema_is_routed_without_its_value_in_the_audit_trail(tmp_path: Any) -> None:
    """The routed contract-violation reason names the field and error type, never the value.

    The scan covers every text cell of every Landscape table. The row VALUE is
    allowed in exactly one place, ``transform_errors.row_data_json`` — the row
    itself, kept by design, as the per-row plugin-returned error keeps it. That
    hit is also the scan's positive control: a scan that found nothing could not
    tell a clean trail from a scan that never looks.
    """
    import json

    from elspeth.core.landscape.schema import routing_events_table, transform_errors_table

    db = make_landscape_db()
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    row = {"id": 7, "amount": _SENTINEL}
    transform = _StrictInputTransform({})
    sinks, graph, settings, config = _build_pipeline(transform, row, on_error="quarantine")

    result = Orchestrator(db).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=payload_store,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )

    assert result.status is RunStatus.FAILED
    assert result.rows_failed == 1
    assert transform.process_count == 0, "the engine's input preflight rejects before the plugin body runs"
    assert sinks["quarantine"].results == [row]

    with db.engine.connect() as conn:
        [transform_error] = conn.execute(select(transform_errors_table).where(transform_errors_table.c.run_id == result.run_id)).all()
        [routing_event] = conn.execute(select(routing_events_table).where(routing_events_table.c.run_id == result.run_id)).all()

    reason = json.loads(transform_error.error_details_json)
    assert reason["reason"] == "contract_violation"
    assert reason["error"].startswith("Transform 'strict_input_transform' input validation failed: 1 validation error: amount: ")
    assert "[int_type]" in reason["error"], "the error type code survives for triage"
    assert _SENTINEL not in transform_error.error_details_json
    # The DIVERT reason is held in the payload store, not in a Landscape column.
    assert routing_event.reason_ref is not None
    routed_reason = payload_store.retrieve(routing_event.reason_ref).decode()
    assert json.loads(routed_reason) == reason
    assert _SENTINEL not in routed_reason

    assert _audit_cells_containing(db, _SENTINEL) == [("transform_errors", "row_data_json")]


# ---------------------------------------------------------------------------
# The batch seams' own contract check (operator ruling 2026-09-23, B2): a
# buffered row that fails a TYPED batch node's declared input schema fails the
# whole batch — routed to the aggregation's on_error, or failing the
# collector's group — instead of aborting the run. Driven through the CLI so
# the operator-visible exit code and the absence of a traceback are measured,
# not inferred.
#
# Arming (measured, engine-seams ``aggpre3``): an OBSERVED JSON source locks a
# field's type on its first row, so the one wrongly-typed row must come first,
# and ``null`` is the value that lets the later ints through. A typed schema
# over an observed CSV would fail every row, and an observed upstream transform
# whose output type varies row to row aborts earlier with ContractMergeError.
# ---------------------------------------------------------------------------

_BATCH_ROWS = (
    {"id": 1, "name": "A", "copies": None, "category": "s"},
    {"id": 2, "name": "B", "copies": 2, "category": "s"},
    {"id": 3, "name": "C", "copies": 1, "category": "s"},
)


def _write_jsonl(path: Any, rows: Any) -> None:
    import json

    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _run_cli(tmp_path: Any, settings_yaml: str) -> Any:
    """``elspeth validate`` then ``elspeth run --execute`` on ``settings_yaml``."""
    from typer.testing import CliRunner

    from elspeth.cli import app

    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(settings_yaml)
    runner = CliRunner()
    validated = runner.invoke(app, ["validate", "-s", str(settings_path)])
    assert validated.exit_code == 0, validated.output
    return runner.invoke(app, ["run", "-s", str(settings_path), "--execute"])


def _typed_aggregation_settings(tmp_path: Any, *, on_error: str, trigger: str) -> str:
    _write_jsonl(tmp_path / "input.jsonl", _BATCH_ROWS)
    trigger_block = "  trigger:\n    count: 3\n" if trigger == "count" else ""
    quarantine_sink = (
        f"""  quarantine:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "quarantine.jsonl"}
      format: jsonl
      schema:
        mode: observed
"""
        if on_error == "quarantine"
        else ""
    )
    return f"""sources:
  primary:
    plugin: json
    on_success: batch_in
    options:
      path: {tmp_path / "input.jsonl"}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
aggregations:
- name: replicate_batch
  plugin: batch_replicate
  input: batch_in
  on_success: output
  on_error: {on_error}
{trigger_block}  output_mode: transform
  options:
    schema:
      mode: fixed
      fields:
      - 'id: int'
      - 'name: str'
      - 'copies: int'
      - 'category: str'
    copies_field: copies
    default_copies: 1
    include_copy_index: true
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "output.jsonl"}
      format: jsonl
      schema:
        mode: observed
{quarantine_sink}landscape:
  url: sqlite:///{tmp_path / "audit.db"}
payload_store:
  backend: filesystem
  base_path: {tmp_path / "payloads"}
"""


_BUFFERED_ROW_0_VIOLATION = (
    "Aggregation transform 'batch_replicate' input validation failed for buffered row 0: 1 validation error: copies: "
)


@pytest.mark.parametrize("trigger", ["count", "end_of_source"])
def test_a_row_failing_a_typed_aggregation_schema_routes_the_whole_batch_to_on_error(trigger: str, tmp_path: Any) -> None:
    """Before B2 this aborted the run (exit 4, a traceback, 3 tokens abandoned)."""
    import json

    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.engine.orchestrator.run_status import cli_completion_for

    cli = _run_cli(tmp_path, _typed_aggregation_settings(tmp_path, on_error="quarantine", trigger=trigger))

    # Every row failed and was routed: FAILED, which the CLI maps to exit 2.
    assert cli.exit_code == cli_completion_for(RunStatus.FAILED)[1] == 2, cli.output
    assert "Traceback" not in cli.output
    assert "PluginContractViolation" not in cli.output

    assert _read_jsonl(tmp_path / "quarantine.jsonl") == list(_BATCH_ROWS)
    assert _read_jsonl(tmp_path / "output.jsonl") == []

    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    with db.engine.connect() as conn:
        [run_id] = conn.execute(select(token_outcomes_table.c.run_id).distinct()).scalars().all()
    audit = _failed_flush_audit(db, run_id)

    assert len(audit["outcomes"]) == 3
    assert {outcome.token_id for outcome in audit["outcomes"]} == audit["row_token_ids"]
    for outcome in audit["outcomes"]:
        assert (outcome.outcome, outcome.path, outcome.sink_name) == (
            TerminalOutcome.FAILURE.value,
            TerminalPath.ON_ERROR_ROUTED.value,
            "quarantine",
        )

    [failed_state] = audit["failed_states"]
    reason = json.loads(failed_state.error_json)
    assert reason["reason"] == "contract_violation"
    assert reason["error"].startswith(_BUFFERED_ROW_0_VIOLATION)
    assert "[int_type]" in reason["error"]

    [routing] = audit["routing"]
    assert (routing.mode, routing.label, routing.state_id) == ("divert", "__error_replicate_batch__", failed_state.state_id)
    [batch] = audit["batches"]
    assert (batch.status, batch.trigger_type) == ("failed", trigger)
    assert len(audit["transform_errors"]) == 3
    for error_row in audit["transform_errors"]:
        assert error_row.destination == "quarantine"
        assert json.loads(error_row.error_details_json) == reason


def test_a_row_failing_a_typed_aggregation_schema_under_discard_quarantines_the_batch(tmp_path: Any) -> None:
    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.engine.orchestrator.run_status import cli_completion_for

    cli = _run_cli(tmp_path, _typed_aggregation_settings(tmp_path, on_error="discard", trigger="count"))

    # Every row discarded: COMPLETED_WITH_FAILURES, exit 1 (the per-row discard's disposition).
    assert cli.exit_code == cli_completion_for(RunStatus.COMPLETED_WITH_FAILURES)[1] == 1, cli.output
    assert "Traceback" not in cli.output

    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    with db.engine.connect() as conn:
        [run_id] = conn.execute(select(token_outcomes_table.c.run_id).distinct()).scalars().all()
    audit = _failed_flush_audit(db, run_id)
    assert {(outcome.outcome, outcome.path) for outcome in audit["outcomes"]} == {
        (TerminalOutcome.FAILURE.value, TerminalPath.QUARANTINED_AT_SOURCE.value)
    }
    assert len(audit["outcomes"]) == 3
    assert audit["routing"] == []
    assert {row.destination for row in audit["transform_errors"]} == {"discard"}


def _typed_collector_settings(tmp_path: Any) -> str:
    # The document-level ``copies`` is copied onto every exploded page. DOC-2 is
    # first (an observed JSON source locks types on row 0, and ``null`` lets
    # DOC-1's int through) and carries copies=null under a collector schema
    # that declares ``copies: int``; DOC-1 is well typed. Lifting a per-page
    # value through an observed transform instead would abort earlier on
    # ContractMergeError (row-to-row type variance), which is not this seam.
    _write_jsonl(
        tmp_path / "docs.jsonl",
        (
            {"doc_id": "DOC-2", "copies": None, "pages": [1, 2]},
            {"doc_id": "DOC-1", "copies": 2, "pages": [1, 2]},
        ),
    )
    return f"""sources:
  docs:
    plugin: json
    on_success: rows
    options:
      path: {tmp_path / "docs.jsonl"}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
concurrency:
  max_workers: 1
transforms:
- name: explode_pages
  plugin: json_explode
  input: rows
  on_success: pages
  on_error: discard
  options:
    array_field: pages
    output_field: page
    schema:
      mode: observed
collectors:
- name: page_rep
  plugin: batch_replicate
  input: pages
  on_success: out
  options:
    copies_field: copies
    default_copies: 1
    include_copy_index: true
    schema:
      mode: flexible
      fields:
      - 'copies: int'
scopes:
- name: document_pages
  opener: explode_pages
  closer: page_rep
  policy: require_all
sinks:
  out:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "out.jsonl"}
      format: jsonl
      schema:
        mode: observed
landscape:
  url: sqlite:///{tmp_path / "audit.db"}
payload_store:
  backend: filesystem
  base_path: {tmp_path / "payloads"}
"""


def test_a_row_failing_a_typed_collector_schema_fails_its_group_and_the_run_goes_on(tmp_path: Any) -> None:
    """Before B2 this aborted the run and left every member hold OPEN."""
    import json

    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.core.landscape.schema import node_states_table, nodes_table
    from elspeth.engine.orchestrator.run_status import cli_completion_for

    cli = _run_cli(tmp_path, _typed_collector_settings(tmp_path))

    # DOC-1's group released; DOC-2's group failed: COMPLETED_WITH_FAILURES, exit 1.
    assert cli.exit_code == cli_completion_for(RunStatus.COMPLETED_WITH_FAILURES)[1] == 1, cli.output
    assert "Traceback" not in cli.output

    released = _read_jsonl(tmp_path / "out.jsonl")
    assert {row["doc_id"] for row in released} == {"DOC-1"}

    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    with db.engine.connect() as conn:
        [collector_node_id] = (
            conn.execute(select(nodes_table.c.node_id).where(nodes_table.c.plugin_name == "batch_replicate")).scalars().all()
        )
        states = conn.execute(select(node_states_table).where(node_states_table.c.node_id == collector_node_id)).all()

    assert not [state for state in states if state.status == "open"], "no member hold may be left OPEN"
    failed = [json.loads(state.error_json) for state in states if state.status == "failed"]
    [flush_error] = [error for error in failed if error["type"] == "PluginContractViolation"]
    assert flush_error["phase"] == "collector_flush"
    assert "input validation failed for buffered row 0: 1 validation error: copies: " in flush_error["exception"]
    member_errors = [error for error in failed if error["type"] == "CollectorGroupFailure"]
    assert len(member_errors) == 2
    assert {error["context"]["failure_reason"] for error in member_errors} == {"collector_contract_violation"}


# ---------------------------------------------------------------------------
# A buffered row that OMITS a field the batch transform declares required
# (elspeth-5887fb7928 R1). The observed input model has no fields, so pydantic
# never saw ``value_field``, and the row reached the plugin's ``row[field]`` as
# a raw KeyError: exit 4, a traceback, every buffered token undecided. The flush
# preflight now enforces the declared set and routes the batch like any other
# contract violation. The omitting row carries a sentinel in ANOTHER column,
# which must reach the on_error sink (the row, intact) and nothing in the audit
# trail's text except the row itself.
# ---------------------------------------------------------------------------

_MISSING_FIELD_SENTINEL = "SENTINEL-R1-note-0b7e"
_MISSING_FIELD_ROWS = (
    {"id": 1, "v": 1.5},
    {"id": 2, "v": 2.5},
    {"id": 3, "v": 0.5},
    {"id": 4, "note": _MISSING_FIELD_SENTINEL},
)


def _missing_field_aggregation_settings(tmp_path: Any, *, trigger: str) -> str:
    _write_jsonl(tmp_path / "input.jsonl", _MISSING_FIELD_ROWS)
    trigger_block = "  trigger:\n    count: 2\n" if trigger == "count" else ""
    return f"""sources:
  primary:
    plugin: json
    on_success: batch_in
    options:
      path: {tmp_path / "input.jsonl"}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
aggregations:
- name: thresholds
  plugin: batch_threshold_summary
  input: batch_in
  on_success: output
  on_error: quarantine
{trigger_block}  output_mode: transform
  options:
    value_field: v
    thresholds:
    - name: hi
      operator: '>='
      value: 1
    schema:
      mode: observed
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "output.jsonl"}
      format: jsonl
      schema:
        mode: observed
  quarantine:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "quarantine.jsonl"}
      format: jsonl
      schema:
        mode: observed
landscape:
  url: sqlite:///{tmp_path / "audit.db"}
payload_store:
  backend: filesystem
  base_path: {tmp_path / "payloads"}
"""


@pytest.mark.parametrize(
    ("trigger", "failed_rows", "offending_index", "expected_status"),
    [
        # count: 2 -> batch (1, 2) succeeds, batch (3, 4) fails on its row 1.
        pytest.param("count", _MISSING_FIELD_ROWS[2:], 1, RunStatus.COMPLETED_WITH_FAILURES, id="count"),
        # end of source -> one batch of four fails on its row 3.
        pytest.param("end_of_source", _MISSING_FIELD_ROWS, 3, RunStatus.FAILED, id="end_of_source"),
    ],
)
def test_a_buffered_row_missing_a_declared_field_routes_the_whole_batch_to_on_error(
    trigger: str,
    failed_rows: tuple[dict[str, Any], ...],
    offending_index: int,
    expected_status: RunStatus,
    tmp_path: Any,
) -> None:
    """Before R1 this aborted the run: exit 4, ``KeyError: 'v'`` from inside the plugin."""
    import json

    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.engine.orchestrator.run_status import cli_completion_for

    cli = _run_cli(tmp_path, _missing_field_aggregation_settings(tmp_path, trigger=trigger))

    assert cli.exit_code == cli_completion_for(expected_status)[1], cli.output
    assert "Traceback" not in cli.output
    assert "KeyError" not in cli.output

    # The whole failed batch reaches on_error with its values intact.
    assert _read_jsonl(tmp_path / "quarantine.jsonl") == list(failed_rows)
    released = _read_jsonl(tmp_path / "output.jsonl")
    if trigger == "count":
        [summary] = released
        assert (summary["batch_size"], summary["valid_count"]) == (2, 2)
    else:
        assert released == []

    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    with db.engine.connect() as conn:
        [run_id] = conn.execute(select(token_outcomes_table.c.run_id).distinct()).scalars().all()
    audit = _failed_flush_audit(db, run_id)

    routed = [
        outcome
        for outcome in audit["outcomes"]
        if (outcome.outcome, outcome.path) == (TerminalOutcome.FAILURE.value, TerminalPath.ON_ERROR_ROUTED.value)
    ]
    assert len(routed) == len(failed_rows)
    assert {outcome.sink_name for outcome in routed} == {"quarantine"}

    [failed_state] = audit["failed_states"]
    reason = json.loads(failed_state.error_json)
    assert reason == {
        "reason": "contract_violation",
        "error": (
            f"Aggregation transform 'batch_threshold_summary' input validation failed for buffered row {offending_index}: "
            "required input field(s) ['v'] absent from the row. The transform's schema declares them required."
        ),
    }
    [routing] = audit["routing"]
    assert (routing.mode, routing.label, routing.state_id) == ("divert", "__error_thresholds__", failed_state.state_id)
    assert [batch.status for batch in audit["batches"] if batch.status == "failed"] == ["failed"]
    assert len(audit["transform_errors"]) == len(failed_rows)
    for error_row in audit["transform_errors"]:
        assert error_row.destination == "quarantine"
        assert json.loads(error_row.error_details_json) == reason

    # The omitting row's own value is kept only as the row itself (positive control).
    assert _audit_cells_containing(db, _MISSING_FIELD_SENTINEL) == [("transform_errors", "row_data_json")]


def _missing_field_collector_settings(tmp_path: Any) -> str:
    # The document-level ``score`` is copied onto every exploded page. DOC-2
    # omits it and carries a sentinel note instead.
    _write_jsonl(
        tmp_path / "docs.jsonl",
        (
            {"doc_id": "DOC-1", "score": 0.9, "pages": [1, 2, 3]},
            {"doc_id": "DOC-2", "note": _MISSING_FIELD_SENTINEL, "pages": [4, 5]},
        ),
    )
    return f"""sources:
  docs:
    plugin: json
    on_success: rows
    options:
      path: {tmp_path / "docs.jsonl"}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
concurrency:
  max_workers: 1
transforms:
- name: explode_pages
  plugin: json_explode
  input: rows
  on_success: pages
  on_error: discard
  options:
    array_field: pages
    output_field: page
    schema:
      mode: observed
collectors:
- name: page_scores
  plugin: batch_threshold_summary
  input: pages
  on_success: out
  options:
    value_field: score
    thresholds:
    - name: hi
      operator: '>='
      value: 0.5
    schema:
      mode: observed
scopes:
- name: document_pages
  opener: explode_pages
  closer: page_scores
  policy: require_all
sinks:
  out:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "out.jsonl"}
      format: jsonl
      schema:
        mode: observed
landscape:
  url: sqlite:///{tmp_path / "audit.db"}
payload_store:
  backend: filesystem
  base_path: {tmp_path / "payloads"}
"""


def test_a_buffered_row_missing_a_declared_field_fails_its_collector_group_and_the_run_goes_on(tmp_path: Any) -> None:
    """Before R1 this aborted the run: exit 4, ``KeyError: "'score' not found in schema contract"``."""
    import json

    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.core.landscape.schema import node_states_table, nodes_table
    from elspeth.engine.orchestrator.run_status import cli_completion_for

    cli = _run_cli(tmp_path, _missing_field_collector_settings(tmp_path))

    # DOC-1's group released; DOC-2's group failed: COMPLETED_WITH_FAILURES, exit 1.
    assert cli.exit_code == cli_completion_for(RunStatus.COMPLETED_WITH_FAILURES)[1] == 1, cli.output
    assert "Traceback" not in cli.output
    assert "KeyError" not in cli.output

    [released] = _read_jsonl(tmp_path / "out.jsonl")
    assert (released["value_field"], released["batch_size"]) == ("score", 3)

    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    with db.engine.connect() as conn:
        [collector_node_id] = (
            conn.execute(select(nodes_table.c.node_id).where(nodes_table.c.plugin_name == "batch_threshold_summary")).scalars().all()
        )
        states = conn.execute(select(node_states_table).where(node_states_table.c.node_id == collector_node_id)).all()
        outcomes = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.completed == 1)).all()

    assert not [state for state in states if state.status == "open"], "no member hold may be left OPEN"
    failed = [json.loads(state.error_json) for state in states if state.status == "failed"]
    [flush_error] = [error for error in failed if error["type"] == "PluginContractViolation"]
    assert flush_error["phase"] == "collector_flush"
    assert flush_error["exception"] == (
        "Collector transform 'batch_threshold_summary' input validation failed for buffered row 0: "
        "required input field(s) ['score'] absent from the row. The transform's schema declares them required."
    )
    member_errors = [error for error in failed if error["type"] == "CollectorGroupFailure"]
    assert len(member_errors) == 2
    assert {error["context"]["failure_reason"] for error in member_errors} == {"collector_contract_violation"}
    # Both DOC-2 pages are decided (failed), not left pending.
    failed_pages = [o for o in outcomes if (o.outcome, o.path) == (TerminalOutcome.FAILURE.value, TerminalPath.UNROUTED.value)]
    assert len(failed_pages) == 2

    # Positive control for the scan: it finds the reason it must find.
    assert ("node_states", "error_json") in _audit_cells_containing(db, "required input field(s) ['score']")
    assert _audit_cells_containing(db, _MISSING_FIELD_SENTINEL) == []


# Each page value is inside the JSON safe integer range (2**53 - 1 =
# 9007199254740991), so the source and the explode hash them cleanly; the
# collector's batch_stats keeps an int sum an int, and the two of them add up
# past that range. The sum is a value the collector EMITTED, not one it
# received, so only the collector's own output check can stop it.
_BIG_PAGE_VALUE = 5_000_000_000_000_000
_NON_CANONICAL_SUM = 2 * _BIG_PAGE_VALUE


def _summing_collector_settings(tmp_path: Any) -> str:
    _write_jsonl(
        tmp_path / "docs.jsonl",
        (
            {"doc_id": "DOC-2", "pages": [_BIG_PAGE_VALUE, _BIG_PAGE_VALUE]},
            {"doc_id": "DOC-1", "pages": [1, 2]},
        ),
    )
    return f"""sources:
  docs:
    plugin: json
    on_success: rows
    options:
      path: {tmp_path / "docs.jsonl"}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
concurrency:
  max_workers: 1
transforms:
- name: explode_pages
  plugin: json_explode
  input: rows
  on_success: pages
  on_error: discard
  options:
    array_field: pages
    output_field: page
    schema:
      mode: observed
collectors:
- name: page_totals
  plugin: batch_stats
  input: pages
  on_success: out
  options:
    value_field: page
    compute_mean: false
    schema:
      mode: observed
scopes:
- name: document_pages
  opener: explode_pages
  closer: page_totals
  policy: require_all
sinks:
  out:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "out.jsonl"}
      format: jsonl
      schema:
        mode: observed
landscape:
  url: sqlite:///{tmp_path / "audit.db"}
payload_store:
  backend: filesystem
  base_path: {tmp_path / "payloads"}
"""


def test_a_collector_emitting_non_canonical_output_fails_its_group_without_the_value(tmp_path: Any) -> None:
    """The collector flush checks what it RELEASES, as the aggregation and per-row seams do.

    Before the check, the collector released the out-of-range sum and the run
    died at the next node's canonical hash: one row's data ended the run. Now
    the group fails as a Tier-2 contract violation (whole group, value-free),
    DOC-1's group still releases, and no audit cell holds the emitted value.
    """
    import json

    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.core.landscape.schema import node_states_table, nodes_table
    from elspeth.engine.orchestrator.run_status import cli_completion_for

    cli = _run_cli(tmp_path, _summing_collector_settings(tmp_path))

    assert cli.exit_code == cli_completion_for(RunStatus.COMPLETED_WITH_FAILURES)[1] == 1, cli.output
    assert "Traceback" not in cli.output
    assert str(_NON_CANONICAL_SUM) not in cli.output

    released = _read_jsonl(tmp_path / "out.jsonl")
    assert [(row["count"], row["sum"]) for row in released] == [(2, 3)]

    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    with db.engine.connect() as conn:
        [collector_node_id] = conn.execute(select(nodes_table.c.node_id).where(nodes_table.c.plugin_name == "batch_stats")).scalars().all()
        states = conn.execute(select(node_states_table).where(node_states_table.c.node_id == collector_node_id)).all()

    assert not [state for state in states if state.status == "open"], "no member hold may be left OPEN"
    failed = [json.loads(state.error_json) for state in states if state.status == "failed"]
    [flush_error] = [error for error in failed if error["type"] == "PluginContractViolation"]
    assert flush_error["phase"] == "collector_flush"
    # batch_stats under an observed schema declares no output field, so the
    # field name is withheld too (a field name can itself be row data).
    assert flush_error["exception"].startswith(
        "Collector transform 'batch_stats' emitted non-canonical data at emitted row 0, "
        "in a field its output schema does not declare (IntegerDomainError). "
    )
    member_errors = [error for error in failed if error["type"] == "CollectorGroupFailure"]
    assert len(member_errors) == 2
    assert {error["context"]["failure_reason"] for error in member_errors} == {"collector_contract_violation"}

    # The emitted sum is persisted nowhere: no Landscape text cell (the reasons
    # live there) and no payload (the row values live there; nothing was
    # released, so no child row was stored). Each scan has a positive control
    # in the same run: the DB scan finds the violation text in the very cell
    # a leak would reach, and the payload scan finds the page values the
    # source and the explode legitimately stored.
    assert ("node_states", "error_json") in _audit_cells_containing(db, "emitted non-canonical data")
    assert _audit_cells_containing(db, str(_NON_CANONICAL_SUM)) == []
    payloads = [path.read_bytes() for path in (tmp_path / "payloads").rglob("*") if path.is_file()]
    assert any(str(_BIG_PAGE_VALUE).encode() in payload for payload in payloads)
    assert not any(str(_NON_CANONICAL_SUM).encode() in payload for payload in payloads)


# ---------------------------------------------------------------------------
# The batch plugins' own row checks (elspeth-5887fb7928 plugin half, and the
# collision ruling on elspeth-d90495084c): a wrong-typed value, or a row that
# already carries a field the plugin writes, fails the WHOLE batch with a
# RETURNED error, which the aggregation routes to its on_error sink.
#
# Arming (measured per plugin, lane evidence impl-P-*.md): the source AND the
# aggregation schema are observed — a typed aggregation schema trips the
# engine's batch-input validation first and the plugin body never runs. An
# observed CSV source makes every value a ``str``, so any row arms a numeric
# guard. An observed JSON source locks a field's type on row 0, so the
# offending value is in row 0 and every row carries the same type; that keeps
# all N rows buffered in one batch. The sentinel is the offending value (or the
# value already held under the colliding field): it must reach the quarantine
# sink with the row, and must appear in no audit reason.
#
# Before the fix the wrong-type cases raised a bare ``TypeError`` out of
# ``Orchestrator.run`` (exit 4, every token abandoned). The two collision cases
# already ROUTED before this fix, through the engine's flush contract-violation
# arm, with the plugin's free-text message; what they pin is the structured,
# value-free ``field_collision`` reason, so their assertion is the reason shape.
# ---------------------------------------------------------------------------

_BATCH_NODE = "batch_node"


def _csv_rows(header: tuple[str, ...], *rows: tuple[str, ...]) -> list[dict[str, Any]]:
    return [dict(zip(header, row, strict=True)) for row in rows]


def _wrong_type(field: str, expected: str, found: str, row_index: int) -> dict[str, Any]:
    """The reason ``BatchRowTypeError.as_reason()`` renders, written out literally
    so this file collects on a tree that predates the plugin fixes."""
    return {
        "reason": "invalid_input",
        "error_type": "wrong_type",
        "field": field,
        "expected": expected,
        "actual_type": found,
        "error": f"must be {expected}, got {found} in row {row_index}",
    }


def _collision(field: str, row_index: int) -> dict[str, Any]:
    return {
        "reason": "field_collision",
        "collisions": [field],
        "error": f"would overwrite existing input fields {[field]} in row {row_index}",
    }


_NUMERIC = "numeric (int or float)"

# (case id, plugin, source kind, input rows, plugin options, sentinel, expected reason)
_BATCH_PLUGIN_CASES = [
    pytest.param(
        "batch_distribution_profile",
        "csv",
        _csv_rows(("id", "v"), ("1", "SENTINEL-dist-4b2e"), ("2", "77.5"), ("3", "12.0")),
        {"value_field": "v"},
        "SENTINEL-dist-4b2e",
        _wrong_type("v", _NUMERIC, "str", 0),
        id="distribution_profile-str-value",
    ),
    pytest.param(
        "batch_drift_compare",
        "csv",
        _csv_rows(("c", "v"), ("base", "SENTINEL-drift-91c4"), ("cur", "2"), ("base", "3"), ("cur", "4")),
        {"cohort_field": "c", "value_field": "v", "value_type": "numeric"},
        "SENTINEL-drift-91c4",
        _wrong_type("v", _NUMERIC, "str", 0),
        id="drift_compare-numeric-str-value",
    ),
    pytest.param(
        "batch_drift_compare",
        "json",
        [{"c": "base", "v": 6173.375}, {"c": "cur", "v": 2.5}, {"c": "base", "v": 3.5}, {"c": "cur", "v": 4.5}],
        {"cohort_field": "c", "value_field": "v", "value_type": "categorical"},
        "6173.375",
        _wrong_type("v", "a scalar category (str, int, or bool)", "float", 0),
        id="drift_compare-categorical-float-value",
    ),
    pytest.param(
        "batch_effect_size",
        "csv",
        _csv_rows(
            ("id", "variant", "score"),
            ("1", "control", "SENTINEL-effect-5d71"),
            ("2", "control", "2.0"),
            ("3", "treatment", "3.5"),
            ("4", "treatment", "4.25"),
        ),
        {"variant_field": "variant", "score_field": "score", "baseline_variant": "control"},
        "SENTINEL-effect-5d71",
        _wrong_type("score", _NUMERIC, "str", 0),
        id="effect_size-str-score",
    ),
    pytest.param(
        "batch_experiment_compare",
        "csv",
        _csv_rows(
            ("variant", "score", "id"),
            ("control", "SENTINEL-exp-7ab3", "1"),
            ("treat", "0.7713", "2"),
            ("control", "0.6121", "3"),
        ),
        {"variant_field": "variant", "score_field": "score"},
        "SENTINEL-exp-7ab3",
        _wrong_type("score", _NUMERIC, "str", 0),
        id="experiment_compare-str-score",
    ),
    pytest.param(
        "batch_paired_preference",
        "csv",
        _csv_rows(
            ("case_id", "variant", "score"),
            ("CASE-1", "A", "SENTINEL-score-7f3a"),
            ("CASE-1", "B", "0.9"),
            ("CASE-2", "A", "0.4"),
            ("CASE-2", "B", "0.6"),
        ),
        {"pair_field": "case_id", "variant_field": "variant", "score_field": "score"},
        "SENTINEL-score-7f3a",
        _wrong_type("score", _NUMERIC, "str", 0),
        id="paired_preference-str-score",
    ),
    pytest.param(
        "batch_threshold_summary",
        "csv",
        _csv_rows(("id", "v"), ("1", "SENTINEL-thresh-2e9a"), ("2", "0.9"), ("3", "0.4")),
        {"value_field": "v", "thresholds": [{"name": "hi", "operator": ">=", "value": 0.5}]},
        "SENTINEL-thresh-2e9a",
        _wrong_type("v", _NUMERIC, "str", 0),
        id="threshold_summary-str-value",
    ),
    pytest.param(
        "batch_classifier_metrics",
        "json",
        [{"id": 1, "a": 7331.625, "p": "cat"}, {"id": 2, "a": 2.25, "p": "dog"}, {"id": 3, "a": 3.125, "p": "cat"}],
        {"actual_field": "a", "predicted_field": "p"},
        "7331.625",
        _wrong_type("a", "a scalar label (str, int, or bool)", "float", 0),
        id="classifier_metrics-float-label",
    ),
    pytest.param(
        "batch_top_k",
        "json",
        [{"id": 1, "v": ["SENTINEL-topk-81af"]}, {"id": 2, "v": ["b"]}, {"id": 3, "v": ["c"]}],
        {"field": "v", "k": 2},
        "SENTINEL-topk-81af",
        _wrong_type("v", "a scalar top-k value (str, int, float, bool, or None)", "tuple", 0),
        id="top_k-array-value",
    ),
    pytest.param(
        "report_assemble",
        "json",
        [{"id": 1, "t": 734129}, {"id": 2, "t": 59317}, {"id": 3, "t": 60421}],
        {"text_field": "t"},
        "734129",
        _wrong_type("t", "a string", "int", 0),
        id="report_assemble-int-text",
    ),
    pytest.param(
        "batch_stats",
        "json",
        [
            {"id": 1, "v": 10, "g": {"tenant": "SENTINEL-group-6b0e"}},
            {"id": 2, "v": 20, "g": {"tenant": "b"}},
            {"id": 3, "v": 30, "g": {"tenant": "c"}},
        ],
        {"value_field": "v", "group_by": "g"},
        "SENTINEL-group-6b0e",
        _wrong_type("g", "a scalar group key", "mappingproxy", 0),
        id="stats-object-group-key",
    ),
    pytest.param(
        "batch_outlier_annotator",
        "csv",
        _csv_rows(("id", "v"), ("1", "SENTINEL-outlier-5e6d"), ("2", "2.5"), ("3", "3.5")),
        {"value_field": "v"},
        "SENTINEL-outlier-5e6d",
        _wrong_type("v", _NUMERIC, "str", 0),
        id="outlier_annotator-str-value",
    ),
    pytest.param(
        "batch_replicate",
        "csv",
        _csv_rows(("id", "copies"), ("1", "SENTINEL-copies-3c8d"), ("2", "2"), ("3", "1")),
        {"copies_field": "copies"},
        "SENTINEL-copies-3c8d",
        _wrong_type("copies", "int", "str", 0),
        id="replicate-str-copies",
    ),
    pytest.param(
        "batch_replicate",
        "json",
        [
            {"id": 1, "copies": 2, "copy_index": "SENTINEL-collide-9f2b"},
            {"id": 2, "copies": 1, "copy_index": "x"},
            {"id": 3, "copies": 1, "copy_index": "y"},
        ],
        {"copies_field": "copies"},
        "SENTINEL-collide-9f2b",
        _collision("copy_index", 0),
        id="replicate-copy_index-collision",
    ),
    pytest.param(
        "batch_outlier_annotator",
        "json",
        [
            {"id": 1, "v": 1.0, "outlier_z_score": "SENTINEL-collide-7a1b"},
            {"id": 2, "v": 2.0, "outlier_z_score": "x"},
            {"id": 3, "v": 3.0, "outlier_z_score": "y"},
        ],
        {"value_field": "v"},
        "SENTINEL-collide-7a1b",
        _collision("outlier_z_score", 0),
        id="outlier_annotator-outlier_z_score-collision",
    ),
]


def _run_batch_plugin_pipeline(
    tmp_path: Any, *, plugin: str, source: str, rows: list[dict[str, Any]], options: dict[str, Any]
) -> tuple[Any, Any, Any, list[dict[str, Any]], list[dict[str, Any]]]:
    """Observed source -> ``plugin`` under ``aggregations:`` (observed schema,
    one count-triggered batch holding every row, ``on_error: quarantine``) ->
    JSON sinks, through the same production assembly path as the batch_stats
    harness above (settings -> instantiate -> graph.validate -> preflight ->
    Orchestrator)."""
    import json

    from elspeth.cli_helpers import instantiate_plugins_from_config
    from elspeth.config_loading import load_settings_from_yaml_string
    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config

    if source == "csv":
        input_path = tmp_path / "input.csv"
        header = list(rows[0])
        input_path.write_text(",".join(header) + "\n" + "".join(",".join(row[name] for name in header) + "\n" for row in rows))
        source_options: dict[str, Any] = {}
    else:
        input_path = tmp_path / "input.jsonl"
        _write_jsonl(input_path, rows)
        source_options = {"format": "jsonl"}

    def _sink(name: str) -> dict[str, Any]:
        return {
            "plugin": "json",
            "on_write_failure": "discard",
            "options": {"path": str(tmp_path / f"{name}.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
        }

    settings = load_settings_from_yaml_string(
        # JSON is YAML; building the document as data keeps list/dict options exact.
        json.dumps(
            {
                "sources": {
                    "primary": {
                        "plugin": source,
                        "on_success": "batch_in",
                        "options": {
                            "path": str(input_path),
                            "on_validation_failure": "discard",
                            "schema": {"mode": "observed"},
                            **source_options,
                        },
                    }
                },
                "aggregations": [
                    {
                        "name": _BATCH_NODE,
                        "plugin": plugin,
                        "input": "batch_in",
                        "on_success": "output",
                        "on_error": "quarantine",
                        "trigger": {"count": len(rows)},
                        "output_mode": "transform",
                        "options": {**options, "schema": {"mode": "observed"}},
                    }
                ],
                "sinks": {"output": _sink("output"), "quarantine": _sink("quarantine")},
            }
        )
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
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    result = Orchestrator(db).run(config, graph=graph, settings=settings, payload_store=payload_store)
    return result, db, payload_store, _read_jsonl(tmp_path / "output.jsonl"), _read_jsonl(tmp_path / "quarantine.jsonl")


@pytest.mark.parametrize(("plugin", "source", "rows", "options", "sentinel", "expected_reason"), _BATCH_PLUGIN_CASES)
def test_a_batch_plugin_row_fault_routes_the_whole_batch_with_a_value_free_reason(
    plugin: str,
    source: str,
    rows: list[dict[str, Any]],
    options: dict[str, Any],
    sentinel: str,
    expected_reason: dict[str, Any],
    tmp_path: Any,
) -> None:
    """The plugin RETURNS the failure; the aggregation routes every buffered
    row to on_error with its original values; the record names the field,
    expected and found type (or colliding field) and the BATCH row index —
    never the value."""
    import json

    from elspeth.core.landscape.schema import token_work_items_table

    # Returning at all is the first assertion: before the fix the plugin's
    # TypeError escaped Orchestrator.run.
    result, db, payload_store, output_rows, quarantine_rows = _run_batch_plugin_pipeline(
        tmp_path, plugin=plugin, source=source, rows=rows, options=options
    )
    n = len(rows)

    assert result.status is RunStatus.FAILED
    assert (result.rows_processed, result.rows_succeeded, result.rows_failed) == (n, 0, n)
    assert (result.rows_routed_failure, result.rows_quarantined) == (n, 0)

    # Every buffered row reaches the on_error sink with its ORIGINAL values.
    assert quarantine_rows == rows
    assert output_rows == []

    audit = _failed_flush_audit(db, result.run_id)
    assert len(audit["outcomes"]) == n
    assert {outcome.token_id for outcome in audit["outcomes"]} == audit["row_token_ids"]
    for outcome in audit["outcomes"]:
        assert (outcome.outcome, outcome.path, outcome.sink_name) == (
            TerminalOutcome.FAILURE.value,
            TerminalPath.ON_ERROR_ROUTED.value,
            "quarantine",
        )

    # The flush state records the plugin's structured reason: field, expected,
    # found type (or the colliding field names) and the batch row index.
    [failed_state] = audit["failed_states"]
    assert json.loads(failed_state.error_json) == expected_reason

    [routing] = audit["routing"]
    assert (routing.mode, routing.label, routing.state_id) == ("divert", f"__error_{_BATCH_NODE}__", failed_state.state_id)
    # The DIVERT reason lives in the payload store, not a Landscape column.
    routed_reason = payload_store.retrieve(routing.reason_ref).decode()
    assert json.loads(routed_reason) == expected_reason
    assert sentinel not in routed_reason

    [batch] = audit["batches"]
    assert batch.status == "failed"

    assert len(audit["transform_errors"]) == n
    for error_row in audit["transform_errors"]:
        assert error_row.destination == "quarantine"
        assert json.loads(error_row.error_details_json) == expected_reason

    with db.engine.connect() as conn:
        pending = (
            conn.execute(
                select(token_work_items_table.c.pending_error_message)
                .where(token_work_items_table.c.run_id == result.run_id)
                .where(token_work_items_table.c.pending_path == TerminalPath.ON_ERROR_ROUTED.value)
            )
            .scalars()
            .all()
        )
    assert pending == [canonical_json(expected_reason)] * n

    # The value is in exactly one audit text surface: the failed row itself,
    # kept by design. That hit is the scan's positive control; the reason
    # columns (error_details_json, error_json, pending_error_message) are clean.
    assert set(_audit_cells_containing(db, sentinel)) == {("transform_errors", "row_data_json")}
