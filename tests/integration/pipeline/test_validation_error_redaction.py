# tests/integration/pipeline/test_validation_error_redaction.py
"""No row content in a rendered validation failure, at any seam (elspeth-5887fb7928 engine review).

``contracts.safe_validation_errors`` renders every Pydantic failure the engine
and the sources turn into audit text. Dropping pydantic's ``input`` echo was not
enough: a custom validator writes the value into ``msg``, a ``dict[str, X]``
field puts the row's own dict KEY into a nested ``loc``, and a model validator
that re-raises an inner ``ValidationError`` puts such a key at ``loc[0]``, where
a declared field name normally sits. Each shape is driven through each seam
that renders a row-data validation — the per-row transform input check, the
aggregation flush, the collector flush, and a source quarantine — and the
Landscape (every text cell of every table) plus the payload-stored DIVERT
reasons are scanned for the sentinel.

The only cells allowed to hold it are the ones that store the ROW itself by
design (``transform_errors.row_data_json``, ``validation_errors.row_data_json``);
their hit is each scan's positive control.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Self

import pytest
from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator
from sqlalchemy import inspect, select, text

from elspeth.contracts import Determinism, PipelineRow, PluginSchema, RunStatus, TransformResult
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.results import SourceRow
from elspeth.contracts.safe_validation_errors import safe_validation_error_text
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.core.config import CheckpointSettings
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import routing_events_table, validation_errors_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.plugins.infrastructure.base import BaseTransform
from tests.fixtures.base_classes import _TestSchema, _TestSourceBase
from tests.fixtures.landscape import make_landscape_db
from tests.fixtures.plugins import CollectSink, ListSource

SENTINEL = "CUSTOMER-private-582971"


class _MapSchema(PluginSchema):
    """A declared ``dict[str, int]`` field: a row's dict KEY lands in pydantic's nested ``loc``."""

    value: dict[str, int]


class _EchoingSchema(PluginSchema):
    """A custom validator that writes the value into pydantic's ``msg``."""

    value: str

    @field_validator("value")
    @classmethod
    def _reject(cls, value: str) -> str:
        raise ValueError(f"Invalid customer {value}")


class _ReRaisingSchema(PluginSchema):
    """A model validator re-raises an inner ``ValidationError``: the row's dict KEY lands at ``loc[0]``."""

    value: dict[str, Any]

    @model_validator(mode="after")
    def _values_are_ints(self) -> Self:
        TypeAdapter(dict[str, int]).validate_python(self.value)
        return self


class _TypedExtrasSchema(PluginSchema):
    """Typed extras: an undeclared top-level row KEY lands at ``loc[0]`` under an ordinary type code."""

    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, int] = Field(init=False)


# (schema, the row fields that fail it with the sentinel in pydantic's own
# rendering, the rendered location of the failure)
_ENGINE_SHAPES = [
    pytest.param(_MapSchema, {"value": {SENTINEL: "not-an-int"}}, "value.[item]", id="dict-key-in-loc"),
    pytest.param(_EchoingSchema, {"value": SENTINEL}, "value", id="custom-validator-msg"),
    pytest.param(_ReRaisingSchema, {"value": {SENTINEL: "not-an-int"}}, "[undeclared]", id="model-validator-reraise"),
]
# A top-level row key reaching an engine seam is already a field name of the
# upstream schema contract, which the Landscape records by design; at a source
# it is Tier-3 content no contract has admitted, so typed extras are driven
# through the source seam only.
_SOURCE_SHAPES = [
    *_ENGINE_SHAPES,
    pytest.param(_TypedExtrasSchema, {SENTINEL: "not-an-int"}, "[undeclared]", id="typed-extras-key"),
]


def _audit_cells_containing(db: LandscapeDB, needle: str) -> set[tuple[str, str]]:
    """Every (table, column) text cell of the Landscape that contains ``needle``."""
    hits: set[tuple[str, str]] = set()
    with db.engine.connect() as conn:
        inspector = inspect(conn)
        for table in inspector.get_table_names():
            columns = [column["name"] for column in inspector.get_columns(table)]
            for row in conn.execute(text(f'SELECT * FROM "{table}"')).mappings():
                hits.update((table, column) for column in columns if type(row[column]) is str and needle in row[column])
    return hits


def _routing_reasons(db: LandscapeDB, payload_store: FilesystemPayloadStore) -> list[str]:
    """The DIVERT reasons, which live in the payload store rather than a Landscape column."""
    with db.engine.connect() as conn:
        refs = list(conn.execute(select(routing_events_table.c.reason_ref)).scalars())
    assert refs, "positive control: the failure was routed"
    return [payload_store.retrieve(ref).decode() for ref in refs]


def _assert_pydantic_itself_leaks(schema: type[PluginSchema], fields: dict[str, Any]) -> None:
    """Positive control: without the renderer the sentinel WOULD reach the text."""
    try:
        schema.model_validate(fields)
    except ValidationError as exc:
        assert SENTINEL in str(exc)
        assert SENTINEL not in safe_validation_error_text(exc, schema)
        return
    raise AssertionError("the shape must fail its schema")


# ---------------------------------------------------------------------------
# Per-row transform input check (TransformExecutor), routed to on_error.
# ---------------------------------------------------------------------------


class _SchemaInputTransform(BaseTransform):
    name = "schema_input_transform"
    determinism = Determinism.DETERMINISTIC
    output_schema = PluginSchema
    plugin_version = "1.0.0"

    def __init__(self, input_schema: type[PluginSchema]) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self.input_schema = input_schema

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        raise AssertionError("the engine's input preflight rejects before the plugin body runs")


@pytest.mark.parametrize(("schema", "fields", "location"), _ENGINE_SHAPES)
def test_per_row_input_violation_is_routed_without_row_content(
    tmp_path: Any, schema: type[PluginSchema], fields: dict[str, Any], location: str
) -> None:
    from tests.integration.pipeline.test_row_type_violation_routing import _build_pipeline

    _assert_pydantic_itself_leaks(schema, fields)
    db = make_landscape_db()
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    sinks, graph, settings, config = _build_pipeline(_SchemaInputTransform(schema), {"id": 7, **fields}, on_error="quarantine")

    result = Orchestrator(db).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=payload_store,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )

    assert result.status is RunStatus.FAILED
    assert result.rows_routed_failure == 1
    assert len(sinks["quarantine"].results) == 1
    assert _audit_cells_containing(db, SENTINEL) == {("transform_errors", "row_data_json")}
    [reason] = _routing_reasons(db, payload_store)
    assert f"input validation failed: 1 validation error: {location}: [" in reason
    assert SENTINEL not in reason


# ---------------------------------------------------------------------------
# Aggregation flush input check (batch_contract_validation), routed to on_error.
# ---------------------------------------------------------------------------


class _ObservedSource(_TestSourceBase):
    name = "observed_value_source"
    output_schema = ListSource.output_schema

    def __init__(self, rows: list[dict[str, Any]], *, on_success: str) -> None:
        super().__init__()
        self._rows = rows
        self.on_success = on_success

    def load(self, ctx: Any) -> Iterator[SourceRow]:
        for index, row in enumerate(self._rows):
            fields = tuple(
                FieldContract(normalized_name=key, original_name=key, python_type=object, required=False, source="inferred") for key in row
            )
            contract = SchemaContract(mode="OBSERVED", fields=fields, locked=True)
            self._schema_contract = contract
            yield SourceRow.valid(row, contract=contract, source_row_index=index)


class _SchemaBatchTransform(BaseTransform):
    name = "schema_batch"
    determinism = Determinism.DETERMINISTIC
    output_schema = _TestSchema
    is_batch_aware = True
    on_success = "output"
    on_error = "discard"

    def __init__(self, input_schema: type[PluginSchema]) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self.input_schema = input_schema

    def process(self, row: PipelineRow | list[PipelineRow], ctx: Any) -> TransformResult:
        raise AssertionError("the flush preflight rejects before the plugin body runs")


@pytest.mark.parametrize(("schema", "fields", "location"), _ENGINE_SHAPES)
def test_aggregation_flush_violation_is_routed_without_row_content(
    tmp_path: Any, schema: type[PluginSchema], fields: dict[str, Any], location: str
) -> None:
    from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
    from elspeth.core.checkpoint import CheckpointManager
    from tests.integration.pipeline.test_aggregation_recovery import _build_eof_aggregation_pipeline

    _assert_pydantic_itself_leaks(schema, fields)
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    error_sink = CollectSink("quarantine")
    source = _ObservedSource([{"id": index, **fields} for index in range(3)], on_success="batch_in")
    config, graph = _build_eof_aggregation_pipeline(source, _SchemaBatchTransform(schema), CollectSink("output"), error_sink=error_sink)
    orchestrator = Orchestrator(
        db=db,
        checkpoint_manager=CheckpointManager(db),
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    )

    result = orchestrator.run(config, graph=graph, payload_store=payload_store)

    assert result.status is RunStatus.FAILED
    assert result.rows_routed_failure == 3
    assert len(error_sink.results) == 3
    # node_states.error_json, transform_errors.error_details_json and
    # token_work_items.pending_error_message all carry the reason; none may
    # carry the sentinel.
    assert _audit_cells_containing(db, SENTINEL) == {("transform_errors", "row_data_json")}
    [reason] = _routing_reasons(db, payload_store)
    routed = json.loads(reason)
    assert routed["reason"] == "contract_violation"
    assert f"input validation failed for buffered row 0: 1 validation error: {location}: [" in routed["error"]
    assert SENTINEL not in reason


# ---------------------------------------------------------------------------
# Collector flush input check: the group fails; its flush state records the violation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("schema", "fields", "location"), _ENGINE_SHAPES)
def test_collector_flush_violation_is_recorded_without_row_content(
    schema: type[PluginSchema], fields: dict[str, Any], location: str
) -> None:
    from elspeth.contracts import TokenInfo
    from elspeth.contracts.audit import TokenRef
    from elspeth.core.landscape.schema import node_states_table
    from tests.unit.engine.test_collector_executor import _CollectorEnv

    _assert_pydantic_itself_leaks(schema, fields)
    env = _CollectorEnv(policy="require_all")
    env.transform.input_schema = schema
    opener = env._seed_opener()
    children, _ = env.factory.data_flow.expand_token(
        member_token=env.setup.coordination_token.membership,
        parent_ref=TokenRef(token_id=opener.token_id, run_id=env.run_id),
        row_id=opener.row_id,
        child_payloads=[fields],
        output_contract=env.contract,
    )
    child = children[0]
    member = TokenInfo(
        row_id=child.row_id, token_id=child.token_id, row_data=PipelineRow(fields, env.contract), lineage_path=child.lineage_path
    )

    outcome = env.executor.accept(member, "stitch", ctx=env.ctx)

    assert outcome.failure_reason == "collector_contract_violation"
    with env.db.read_only_connection() as conn:
        errors = [error for error in conn.execute(select(node_states_table.c.error_json)).scalars() if error is not None]
    violation = [error for error in errors if "PluginContractViolation" in error]
    assert len(violation) == 1, "positive control: the flush state recorded the violation"
    assert f"input validation failed for buffered row 0: 1 validation error: {location}: [" in violation[0]
    assert not [error for error in errors if SENTINEL in error]


# ---------------------------------------------------------------------------
# Source quarantine: a source renders its own validation failure the way every
# shipped source does, and the engine records and routes it.
# ---------------------------------------------------------------------------


class _ValidatingSource(_TestSourceBase):
    """Validates each row against a programmatic schema; quarantines failures as the shipped sources do."""

    name = "validating_source"
    output_schema = _TestSchema

    def __init__(self, rows: list[dict[str, Any]], schema: type[PluginSchema], *, on_success: str) -> None:
        super().__init__()
        self._rows = rows
        self._schema = schema
        self.on_success = on_success

    def load(self, ctx: Any) -> Iterator[SourceRow]:
        for index, row in enumerate(self._rows):
            try:
                self._schema.model_validate(row)
            except ValidationError as exc:
                error_text = safe_validation_error_text(exc, self._schema)
                ctx.record_validation_error(row=row, error=error_text, schema_mode="fixed", destination="quarantine")
                yield SourceRow.quarantined(row, error=error_text, destination="quarantine", source_row_index=index)
                continue
            raise AssertionError("every row in this fixture fails its schema")


@pytest.mark.parametrize(("schema", "fields", "location"), _SOURCE_SHAPES)
def test_source_quarantine_is_recorded_and_routed_without_row_content(
    tmp_path: Any, schema: type[PluginSchema], fields: dict[str, Any], location: str
) -> None:
    from elspeth.core.config import SourceSettings
    from elspeth.core.dag import ExecutionGraph
    from elspeth.engine.orchestrator import PipelineConfig
    from tests.fixtures.base_classes import as_sink, as_source

    _assert_pydantic_itself_leaks(schema, fields)
    db = make_landscape_db()
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    source = _ValidatingSource([{"id": 1, **fields}], schema, on_success="output")
    source._on_validation_failure = "quarantine"
    sinks = {"output": CollectSink("output"), "quarantine": CollectSink("quarantine")}
    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": as_source(source)},
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="output", options={})},
        transforms=[],
        sinks={name: as_sink(sink) for name, sink in sinks.items()},
        aggregations={},
        gates=[],
    )
    config = PipelineConfig(
        sources={"primary": as_source(source)}, transforms=[], sinks={name: as_sink(sink) for name, sink in sinks.items()}
    )

    result = Orchestrator(db).run(config, graph=graph, payload_store=payload_store)

    assert result.rows_quarantined == 1
    assert len(sinks["quarantine"].results) == 1
    # validation_errors.row_data_json stores the quarantined row by design (the
    # positive control); validation_errors.error, node_states.error_json and
    # the DIVERT reason carry the rendered text and must not hold it.
    assert _audit_cells_containing(db, SENTINEL) == {("validation_errors", "row_data_json")}
    with db.engine.connect() as conn:
        [recorded] = conn.execute(select(validation_errors_table.c.error)).scalars()
    assert recorded.startswith(f"1 validation error: {location}: [")
    [reason] = _routing_reasons(db, payload_store)
    assert SENTINEL not in reason


# ---------------------------------------------------------------------------
# Shipped source, shipped YAML schema: every source schema built from YAML
# (observed, flexible, fixed) sits on the observed base whose before-validator
# rejects a non-finite float and names its PATH, nested row keys included, in
# ``msg``. JSON ``1e999`` is valid JSON and parses to inf, so a jsonl line
# reaches that validator with a row key in the path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param({"mode": "observed"}, id="observed"),
        pytest.param({"mode": "flexible", "fields": ["id: int"]}, id="flexible-extra"),
        pytest.param({"mode": "fixed", "fields": ["id: int", "balances: any"]}, id="fixed-any-field"),
    ],
)
def test_shipped_json_source_quarantines_a_non_finite_value_without_its_row_key(tmp_path: Any, schema: dict[str, Any]) -> None:
    from elspeth.contracts.plugin_context import PluginContext
    from elspeth.plugins.sources.json_source import JSONSource
    from tests.fixtures.landscape import make_recorder_with_run

    data = tmp_path / "data.jsonl"
    data.write_text('{"id": 1, "balances": {"' + SENTINEL + '": 1e999}}\n{"id": 2, "balances": {"ok": 1.5}}\n')
    setup = make_recorder_with_run(run_id="shipped-source-non-finite", source_node_id="source", source_plugin_name="json")
    ctx = PluginContext(
        run_id=setup.run_id,
        node_id=setup.source_node_id,
        config={},
        landscape=setup.factory.plugin_audit_writer(),
        coordination_token=setup.coordination_token,
    )
    source = JSONSource({"path": str(data), "format": "jsonl", "schema": schema, "on_validation_failure": "quarantine"})

    rows = list(source.load(ctx))

    [quarantined] = [row for row in rows if row.is_quarantined]
    assert [row.row["id"] for row in rows if not row.is_quarantined] == [2]
    # validation_errors.row_data_json stores the quarantined row by design (the
    # positive control); the rendered text names the model-level failure only.
    assert _audit_cells_containing(setup.db, SENTINEL) == {("validation_errors", "row_data_json")}
    with setup.db.engine.connect() as conn:
        [recorded] = conn.execute(select(validation_errors_table.c.error)).scalars()
    assert recorded == "1 validation error: <root>: [value_error]"
    assert quarantined.quarantine_error == recorded
