# tests/integration/pipeline/test_validation_error_redaction.py
"""No row content in a rendered validation failure, at any seam (elspeth-5887fb7928 engine review).

``contracts.safe_validation_errors`` renders every Pydantic failure the engine
and the sources turn into audit text. Dropping pydantic's ``input`` echo was not
enough: a custom validator writes the value into ``msg``, and a ``dict[str, X]``
field puts the row's own dict KEY into ``loc``. Both shapes are driven through
each seam that renders a row-data validation — the per-row transform input
check, the aggregation flush, the collector flush, and a source quarantine —
and the Landscape (every text cell of every table) plus the payload-stored
DIVERT reasons are scanned for the sentinel.

The only cells allowed to hold it are the ones that store the ROW itself by
design (``transform_errors.row_data_json``, ``validation_errors.row_data_json``);
their hit is each scan's positive control.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError, field_validator
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
    """A declared ``dict[str, int]`` field: a row's dict KEY lands in pydantic's ``loc``."""

    value: dict[str, int]


class _EchoingSchema(PluginSchema):
    """A custom validator that writes the value into pydantic's ``msg``."""

    value: str

    @field_validator("value")
    @classmethod
    def _reject(cls, value: str) -> str:
        raise ValueError(f"Invalid customer {value}")


# (schema, the row value that fails it with the sentinel in pydantic's own rendering)
_SHAPES = [
    pytest.param(_MapSchema, {SENTINEL: "not-an-int"}, id="dict-key-in-loc"),
    pytest.param(_EchoingSchema, SENTINEL, id="custom-validator-msg"),
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


def _assert_pydantic_itself_leaks(schema: type[PluginSchema], value: object) -> None:
    """Positive control: without the renderer the sentinel WOULD reach the text."""
    try:
        schema.model_validate({"value": value})
    except ValidationError as exc:
        assert SENTINEL in str(exc)
        assert SENTINEL not in safe_validation_error_text(exc)
        return
    raise AssertionError("the shape must fail its schema")


# ---------------------------------------------------------------------------
# Per-row transform input check (TransformExecutor), routed to on_error.
# ---------------------------------------------------------------------------


class _MapInputTransform(BaseTransform):
    name = "map_input_transform"
    determinism = Determinism.DETERMINISTIC
    input_schema = _MapSchema
    output_schema = PluginSchema
    plugin_version = "1.0.0"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__({"schema": {"mode": "observed"}, **config})

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        raise AssertionError("the engine's input preflight rejects before the plugin body runs")


class _EchoingInputTransform(_MapInputTransform):
    name = "echoing_input_transform"
    determinism = Determinism.DETERMINISTIC
    input_schema = _EchoingSchema


@pytest.mark.parametrize(
    ("transform_cls", "schema", "value"),
    [
        pytest.param(_MapInputTransform, _MapSchema, {SENTINEL: "not-an-int"}, id="dict-key-in-loc"),
        pytest.param(_EchoingInputTransform, _EchoingSchema, SENTINEL, id="custom-validator-msg"),
    ],
)
def test_per_row_input_violation_is_routed_without_row_content(
    tmp_path: Any, transform_cls: type[BaseTransform], schema: type[PluginSchema], value: object
) -> None:
    from tests.integration.pipeline.test_row_type_violation_routing import _build_pipeline

    _assert_pydantic_itself_leaks(schema, value)
    db = make_landscape_db()
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    row = {"id": 7, "value": value}
    sinks, graph, settings, config = _build_pipeline(transform_cls({}), row, on_error="quarantine")

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
    assert "input validation failed: 1 validation error: value" in reason
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


class _MapBatchTransform(BaseTransform):
    name = "map_batch"
    determinism = Determinism.DETERMINISTIC
    input_schema = _MapSchema
    output_schema = _TestSchema
    is_batch_aware = True
    on_success = "output"
    on_error = "discard"

    def __init__(self) -> None:
        super().__init__({"schema": {"mode": "observed"}})

    def process(self, row: PipelineRow | list[PipelineRow], ctx: Any) -> TransformResult:
        if isinstance(row, list):
            raise AssertionError("the flush preflight rejects before the plugin body runs")
        return TransformResult.success(row, success_reason={"action": "buffer"})


class _EchoingBatchTransform(_MapBatchTransform):
    name = "echoing_batch"
    determinism = Determinism.DETERMINISTIC
    input_schema = _EchoingSchema


@pytest.mark.parametrize(
    ("transform_cls", "schema", "value"),
    [
        pytest.param(_MapBatchTransform, _MapSchema, {SENTINEL: "not-an-int"}, id="dict-key-in-loc"),
        pytest.param(_EchoingBatchTransform, _EchoingSchema, SENTINEL, id="custom-validator-msg"),
    ],
)
def test_aggregation_flush_violation_is_routed_without_row_content(
    tmp_path: Any, transform_cls: type[BaseTransform], schema: type[PluginSchema], value: object
) -> None:
    from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
    from elspeth.core.checkpoint import CheckpointManager
    from tests.integration.pipeline.test_aggregation_recovery import _build_eof_aggregation_pipeline

    _assert_pydantic_itself_leaks(schema, value)
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    error_sink = CollectSink("quarantine")
    source = _ObservedSource([{"id": index, "value": value} for index in range(3)], on_success="batch_in")
    transform = transform_cls()
    config, graph = _build_eof_aggregation_pipeline(source, transform, CollectSink("output"), error_sink=error_sink)
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
    assert json.loads(reason)["reason"] == "contract_violation"
    assert SENTINEL not in reason


# ---------------------------------------------------------------------------
# Collector flush input check: the group fails; its flush state records the violation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("schema", "value"), _SHAPES)
def test_collector_flush_violation_is_recorded_without_row_content(schema: type[PluginSchema], value: object) -> None:
    from elspeth.contracts import TokenInfo
    from elspeth.contracts.audit import TokenRef
    from elspeth.core.landscape.schema import node_states_table
    from tests.unit.engine.test_collector_executor import _CollectorEnv

    _assert_pydantic_itself_leaks(schema, value)
    env = _CollectorEnv(policy="require_all")
    env.transform.input_schema = schema
    opener = env._seed_opener()
    payload = {"value": value}
    children, _ = env.factory.data_flow.expand_token(
        member_token=env.setup.coordination_token.membership,
        parent_ref=TokenRef(token_id=opener.token_id, run_id=env.run_id),
        row_id=opener.row_id,
        child_payloads=[payload],
        output_contract=env.contract,
    )
    child = children[0]
    member = TokenInfo(
        row_id=child.row_id, token_id=child.token_id, row_data=PipelineRow(payload, env.contract), lineage_path=child.lineage_path
    )

    outcome = env.executor.accept(member, "stitch", ctx=env.ctx)

    assert outcome.failure_reason == "collector_contract_violation"
    with env.db.read_only_connection() as conn:
        errors = [error for error in conn.execute(select(node_states_table.c.error_json)).scalars() if error is not None]
    violation = [error for error in errors if "PluginContractViolation" in error]
    assert len(violation) == 1, "positive control: the flush state recorded the violation"
    assert "input validation failed for buffered row 0" in violation[0]
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
                error_text = safe_validation_error_text(exc)
                ctx.record_validation_error(row=row, error=error_text, schema_mode="fixed", destination="quarantine")
                yield SourceRow.quarantined(row, error=error_text, destination="quarantine", source_row_index=index)
                continue
            raise AssertionError("every row in this fixture fails its schema")


@pytest.mark.parametrize(("schema", "value"), _SHAPES)
def test_source_quarantine_is_recorded_and_routed_without_row_content(tmp_path: Any, schema: type[PluginSchema], value: object) -> None:
    from elspeth.core.config import SourceSettings
    from elspeth.core.dag import ExecutionGraph
    from elspeth.engine.orchestrator import PipelineConfig
    from tests.fixtures.base_classes import as_sink, as_source

    _assert_pydantic_itself_leaks(schema, value)
    db = make_landscape_db()
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    source = _ValidatingSource([{"id": 1, "value": value}], schema, on_success="output")
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
    assert recorded.startswith("1 validation error: value")
    [reason] = _routing_reasons(db, payload_store)
    assert SENTINEL not in reason
