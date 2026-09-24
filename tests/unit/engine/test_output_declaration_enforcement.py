"""The engine enforces a transform's output declaration on what it emitted (ADR-050).

Two checks in ``TransformExecutor._verify_output_declarations``, proved through
the production ``Orchestrator`` so the recorded outcomes are the real ones:

1. COMPLETENESS (Tier 1): a created field that bypassed the declaration stamp
   ends the run with ``UndeclaredOutputFieldsViolation`` after the token's
   terminal is recorded.
2. DECLARED TYPES AGAINST VALUES (Tier 2): a value breaking a concrete
   declared type routes the row through ``on_error`` with a value-free reason
   that records who DECLARED the broken type (``declared_by``: operator,
   plugin or upstream) and whether the transform created the field or
   rewrote an input field (``authorship``: computed or carried); a multi-row
   emission fails its parent token once.

Before ADR-050 ``validate_output_against_contract`` had no production caller
and the strict ``output_schema`` check typed nothing for a field-adding
transform, so a declared ``page: int`` delivered a str to the sink.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import Determinism, PluginSchema, RunStatus, TerminalOutcome, TerminalPath, TransformResult
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.errors import UndeclaredOutputFieldsViolation
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.schema import FieldDefinition
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.wiring import WiredTransform
from elspeth.core.landscape.schema import token_outcomes_table, transform_errors_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from tests.fixtures.base_classes import as_sink, as_source, as_transform
from tests.fixtures.landscape import make_landscape_db
from tests.fixtures.plugins import CollectSink, ListSource

SENTINEL = "SENTINEL_ADR050_ENGINE"


class _Emitting(BaseTransform):
    """A transform whose emission and declaration are chosen per test."""

    name = "emitting_probe"
    determinism = Determinism.DETERMINISTIC
    input_schema = PluginSchema
    output_schema = PluginSchema
    plugin_version = "1.0.0"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        emit: list[dict[str, Any]],
        created: tuple[FieldDefinition, ...] = (),
        declared: frozenset[str] = frozenset(),
        stamp: bool = True,
    ) -> None:
        super().__init__(config)
        cfg = TransformDataConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._emit = emit
        self._created = created
        self._stamp = stamp
        self.declared_output_fields = declared
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)

    def created_output_fields(self) -> tuple[FieldDefinition, ...]:
        return self._created

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        from elspeth.contracts.contract_propagation import propagate_contract

        rows: list[PipelineRow] = []
        shared_contract = None
        for payload in self._emit:
            output = {**row.to_dict(), **payload}
            if shared_contract is None:
                contract = propagate_contract(row.contract, output, transform_adds_fields=True)
                if self._stamp:
                    contract = self._apply_declared_output_field_contracts(contract)
                shared_contract = self._align_output_contract(contract)
            rows.append(PipelineRow(output, shared_contract))
        if len(rows) == 1:
            return TransformResult.success(rows[0], success_reason={"action": "probe"})
        return TransformResult.success_multi(rows, success_reason={"action": "probe"})

    def close(self) -> None:
        pass


def _run(tmp_path: Any, transform: BaseTransform, row: dict[str, Any], *, db: Any | None = None) -> tuple[Any, dict[str, CollectSink], Any]:
    source_connection = "probe_source_out"
    source = ListSource([row], name="probe_source_plugin", on_success=source_connection)
    transform.on_success = "output"
    transform.on_error = "quarantine"
    transform_settings = TransformSettings(
        name="probe", plugin=transform.name, input=source_connection, on_success="output", on_error="quarantine", options={}
    )
    source_settings = SourceSettings(plugin=source.name, on_success=source_connection, options={})
    sinks = {"output": CollectSink("output"), "quarantine": CollectSink("quarantine")}
    graph = ExecutionGraph.from_plugin_instances(
        sources={"probe_source": as_source(source)},
        source_settings_map={"probe_source": source_settings},
        transforms=[WiredTransform(plugin=as_transform(transform), settings=transform_settings)],
        sinks={name: as_sink(sink) for name, sink in sinks.items()},
        aggregations={},
        gates=[],
    )
    settings = ElspethSettings(
        sources={"probe_source": source_settings},
        transforms=[transform_settings],
        sinks={name: SinkSettings(plugin=sink.name, options={}, on_write_failure="discard") for name, sink in sinks.items()},
    )
    config = PipelineConfig(
        sources={"probe_source": as_source(source)},
        transforms=[as_transform(transform)],
        sinks={name: as_sink(sink) for name, sink in sinks.items()},
    )
    db = db if db is not None else make_landscape_db()
    result = Orchestrator(db).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    return result, sinks, db


def _transform_error_reasons(db: Any, run_id: str) -> list[dict[str, Any]]:
    with db.engine.connect() as conn:
        rows = (
            conn.execute(select(transform_errors_table.c.error_details_json).where(transform_errors_table.c.run_id == run_id))
            .scalars()
            .all()
        )
    return [json.loads(text) for text in rows]


def _outcomes(db: Any, run_id: str) -> list[tuple[str, str, str | None]]:
    with db.engine.connect() as conn:
        return [
            (row.outcome, row.path, row.sink_name)
            for row in conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == run_id)).all()
        ]


class TestCompleteness:
    def test_a_created_field_that_bypassed_the_stamp_ends_the_run_after_recording_the_terminal(self, tmp_path: Any) -> None:
        transform = _Emitting({"schema": {"mode": "observed"}}, emit=[{"score": 1}], declared=frozenset({"score"}), stamp=False)
        db = make_landscape_db()
        with pytest.raises(UndeclaredOutputFieldsViolation) as raised:
            _run(tmp_path, transform, {"id": 1}, db=db)
        assert deep_thaw(raised.value.payload["violations"]) == [{"emitted_index": 0, "undeclared": ["score"]}]
        assert deep_thaw(raised.value.payload["stamped"]) == []
        assert raised.value.contract_name == "output_declaration_completeness"
        assert "score" in str(raised.value)
        # Tier 1: the run ended, and the token's terminal was recorded before it did.
        with db.engine.connect() as conn:
            [outcome] = conn.execute(select(token_outcomes_table)).all()
        assert (outcome.outcome, outcome.path) == (TerminalOutcome.FAILURE.value, TerminalPath.UNROUTED.value)

    def test_a_created_field_the_plugin_never_named_is_reported_by_name_only(self, tmp_path: Any) -> None:
        transform = _Emitting({"schema": {"mode": "observed"}}, emit=[{"surprise": SENTINEL}])
        with pytest.raises(UndeclaredOutputFieldsViolation) as raised:
            _run(tmp_path, transform, {"id": 1})
        assert deep_thaw(raised.value.payload["violations"]) == [{"emitted_index": 0, "undeclared": ["surprise"]}]
        assert SENTINEL not in json.dumps(raised.value.to_audit_dict())

    def test_a_declared_created_field_passes(self, tmp_path: Any) -> None:
        transform = _Emitting({"schema": {"mode": "observed"}}, emit=[{"score": 1}], declared=frozenset({"score"}))
        result, sinks, _ = _run(tmp_path, transform, {"id": 1})
        assert result.status is RunStatus.COMPLETED
        assert sinks["output"].results == [{"id": 1, "score": 1}]


class TestDeclaredTypesAgainstValues:
    def test_a_created_value_breaking_the_plugin_declared_type_routes_as_plugin_declared(self, tmp_path: Any) -> None:
        """The RC-2 case: the plugin's own code fixes ``score: int`` and its own value breaks it."""
        transform = _Emitting(
            {"schema": {"mode": "observed"}},
            emit=[{"score": SENTINEL}],
            created=(FieldDefinition(name="score", field_type="int", required=True),),
        )
        result, sinks, db = _run(tmp_path, transform, {"id": 1})

        assert result.status is RunStatus.FAILED
        assert result.rows_failed == 1
        assert sinks["output"].results == []
        assert sinks["quarantine"].results == [{"id": 1}]
        assert _outcomes(db, result.run_id) == [(TerminalOutcome.FAILURE.value, TerminalPath.ON_ERROR_ROUTED.value, "quarantine")]
        [reason] = _transform_error_reasons(db, result.run_id)
        assert reason == {
            "reason": "contract_violation",
            "error": reason["error"],
            "field": "score",
            "expected": "int",
            "actual": "str",
            "emitted_index": 0,
            "authorship": "computed",
            "declared_by": "plugin",
        }
        assert (
            "'score' of type str, but the field is declared int by the transform itself (the transform created the field)"
            in reason["error"]
        )
        assert "fix the transform" in reason["error"]
        assert SENTINEL not in json.dumps(reason)

    def test_an_untouched_carried_value_the_input_check_admitted_is_not_re_adjudicated(self, tmp_path: Any) -> None:
        """A Decimal under ``float`` passes pydantic's strict input check; passed through unchanged, it is not faulted here."""
        from decimal import Decimal

        transform = _Emitting({"schema": {"mode": "flexible", "fields": ["amount: float"]}}, emit=[{}])
        result, sinks, db = _run(tmp_path, transform, {"amount": Decimal("123.456789")})
        assert result.status is RunStatus.COMPLETED
        # The collecting sink canonicalises the Decimal to its text; the point is that the row ARRIVED, unrouted.
        assert [row["amount"] for row in sinks["output"].results] == ["123.456789"]
        assert sinks["quarantine"].results == []
        assert _transform_error_reasons(db, result.run_id) == []

    def test_an_equal_valued_type_change_on_an_input_field_is_a_rewrite(self, tmp_path: Any) -> None:
        """``1 == True`` in Python: an input ``bool`` rewritten to ``1`` under ``flag: bool`` is still routed."""
        transform = _Emitting({"schema": {"mode": "flexible", "fields": ["flag: bool"]}}, emit=[{"flag": 1}])
        result, sinks, db = _run(tmp_path, transform, {"flag": True})
        assert result.status is RunStatus.FAILED
        assert sinks["output"].results == []
        [reason] = _transform_error_reasons(db, result.run_id)
        assert (reason["field"], reason["expected"], reason["actual"], reason["authorship"], reason["declared_by"]) == (
            "flag",
            "bool",
            "int",
            "carried",
            "operator",
        )

    def test_a_rewritten_input_value_breaking_the_operator_declared_type_routes_as_operator_declared(self, tmp_path: Any) -> None:
        """The transform REWROTE an input field under the operator's ``amount: int``: authorship carried, declared_by operator."""
        transform = _Emitting({"schema": {"mode": "flexible", "fields": ["amount: int"]}}, emit=[{"amount": SENTINEL}])
        result, sinks, db = _run(tmp_path, transform, {"amount": 250})

        assert result.status is RunStatus.FAILED
        assert sinks["quarantine"].results == [{"amount": 250}]
        [reason] = _transform_error_reasons(db, result.run_id)
        assert (reason["field"], reason["expected"], reason["actual"], reason["authorship"], reason["declared_by"]) == (
            "amount",
            "int",
            "str",
            "carried",
            "operator",
        )
        assert "declared int by the pipeline's schema (the transform rewrote the field)" in reason["error"]
        assert "correct the data or the declaration" in reason["error"]
        assert SENTINEL not in json.dumps(reason)

    def test_a_multi_row_emission_fails_its_parent_once(self, tmp_path: Any) -> None:
        transform = _Emitting(
            {"schema": {"mode": "observed"}},
            emit=[{"page": 1}, {"page": SENTINEL}, {"page": 3}],
            created=(FieldDefinition(name="page", field_type="int", required=True),),
        )
        result, sinks, db = _run(tmp_path, transform, {"id": 1})

        assert result.status is RunStatus.FAILED
        assert sinks["output"].results == []
        assert sinks["quarantine"].results == [{"id": 1}]
        [reason] = _transform_error_reasons(db, result.run_id)
        assert (reason["emitted_index"], reason["field"]) == (1, "page")
        assert len(_outcomes(db, result.run_id)) == 1

    def test_an_any_declaration_accepts_every_value(self, tmp_path: Any) -> None:
        transform = _Emitting({"schema": {"mode": "observed"}}, emit=[{"score": SENTINEL}], declared=frozenset({"score"}))
        result, sinks, _ = _run(tmp_path, transform, {"id": 1})
        assert result.status is RunStatus.COMPLETED
        assert sinks["output"].results == [{"id": 1, "score": SENTINEL}]

    def test_a_null_passes_a_nullable_declaration_and_fails_a_non_nullable_one(self, tmp_path: Any) -> None:
        nullable = _Emitting(
            {"schema": {"mode": "observed"}},
            emit=[{"score": None}],
            created=(FieldDefinition(name="score", field_type="int", required=True, nullable=True),),
        )
        result, sinks, _ = _run(tmp_path, nullable, {"id": 1})
        assert result.status is RunStatus.COMPLETED
        assert sinks["output"].results == [{"id": 1, "score": None}]

        strict = _Emitting(
            {"schema": {"mode": "observed"}},
            emit=[{"score": None}],
            created=(FieldDefinition(name="score", field_type="int", required=True),),
        )
        result, sinks, db = _run(tmp_path / "strict", strict, {"id": 1})
        assert result.status is RunStatus.FAILED
        [reason] = _transform_error_reasons(db, result.run_id)
        assert (reason["expected"], reason["actual"]) == ("int", "NoneType")


def _declared_row(values: dict[str, Any], types: dict[str, type]) -> PipelineRow:
    from elspeth.contracts.schema_contract import FieldContract, SchemaContract

    return PipelineRow(
        values,
        SchemaContract(
            mode="OBSERVED",
            fields=tuple(
                FieldContract(normalized_name=name, original_name=name, python_type=python_type, required=True, source="declared")
                for name, python_type in types.items()
            ),
            locked=True,
        ),
    )


class TestTheDeclarerOfAFieldOutsideTheStampTable:
    """``declared_by`` for a checked field this transform's stamp table does not hold (``declared_output_types._declarer``)."""

    def test_an_input_field_declared_upstream_and_rewritten_records_upstream(self) -> None:
        from elspeth.contracts.errors import DeclaredOutputTypeViolation
        from elspeth.engine.executors.declared_output_types import verify_produced_output_types

        transform = _Emitting({"schema": {"mode": "observed"}}, emit=[])
        assert "amount" not in transform.output_field_declared_by()
        input_row = _declared_row({"amount": 250}, {"amount": int})
        emitted = _declared_row({"amount": SENTINEL}, {"amount": int})
        with pytest.raises(DeclaredOutputTypeViolation) as raised:
            verify_produced_output_types(transform=transform, input_row=input_row, emitted_rows=[emitted])
        reason = raised.value.to_transform_error_reason()
        assert (reason["field"], reason["authorship"], reason["declared_by"]) == ("amount", "carried", "upstream")
        assert "declared int upstream of this transform (the transform rewrote the field)" in reason["error"]
        assert SENTINEL not in json.dumps(reason)

    def test_a_created_field_stamped_per_emission_records_plugin(self) -> None:
        """blob_csv_expand's ``dynamic_created_fields`` shape: created, declared by the plugin, outside the static table."""
        from elspeth.contracts.errors import DeclaredOutputTypeViolation
        from elspeth.engine.executors.declared_output_types import verify_produced_output_types

        transform = _Emitting({"schema": {"mode": "observed"}}, emit=[])
        assert "column" not in transform.output_field_declared_by()
        input_row = _declared_row({"id": 1}, {"id": int})
        emitted = _declared_row({"id": 1, "column": 5}, {"id": int, "column": str})
        with pytest.raises(DeclaredOutputTypeViolation) as raised:
            verify_produced_output_types(transform=transform, input_row=input_row, emitted_rows=[emitted])
        reason = raised.value.to_transform_error_reason()
        assert (reason["field"], reason["authorship"], reason["declared_by"]) == ("column", "computed", "plugin")
