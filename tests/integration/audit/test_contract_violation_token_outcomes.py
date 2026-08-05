"""Terminal token outcomes for schema-validation contract violations (elspeth-82d4c5146c).

A bare PluginContractViolation raised by input-schema validation crashes the
run — that is correct and stays. But the audit trail must still describe the
token's fate: without a terminal outcome the run accounting reports a pending
token on a finished run (failed=0, closure='open'), a contradiction the audit
evidence should never contain.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from elspeth.contracts import PluginSchema, RunStatus
from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.contracts.errors import PluginContractViolation
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import runs_table, token_outcomes_table, tokens_table
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from tests.fixtures.base_classes import as_sink, as_source
from tests.fixtures.pipeline import build_linear_pipeline
from tests.fixtures.plugins import PassTransform
from tests.fixtures.stores import MockPayloadStore


class _RequiresMissingField(PluginSchema):
    field_the_row_never_has: str


def _assert_failed_token_outcome(db: LandscapeDB) -> None:
    with db.connection() as conn:
        run_rows = conn.execute(select(runs_table)).fetchall()
        assert len(run_rows) == 1
        run_row = run_rows[0]
        token_rows = conn.execute(select(tokens_table).where(tokens_table.c.run_id == run_row.run_id)).fetchall()
        outcome_rows = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == run_row.run_id)).fetchall()

    assert run_row.status == RunStatus.FAILED
    assert len(token_rows) == 1
    assert len(outcome_rows) == 1
    assert outcome_rows[0].token_id == token_rows[0].token_id
    assert outcome_rows[0].outcome == TerminalOutcome.FAILURE.value
    assert outcome_rows[0].path == TerminalPath.UNROUTED.value


def test_transform_input_validation_violation_records_terminal_outcome() -> None:
    """The g11/g08/g04 battery shape: transform input validation crashes the run."""
    db = LandscapeDB.in_memory()
    payload_store = MockPayloadStore()

    failing_transform = PassTransform()
    failing_transform.input_schema = _RequiresMissingField
    source, transforms, sinks, graph = build_linear_pipeline([{"value": 42}], transforms=[failing_transform])
    sink = sinks["default"]

    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=transforms,
        sinks={"default": as_sink(sink)},
    )

    with pytest.raises(PluginContractViolation, match="input validation failed"):
        Orchestrator(db).run(config, graph=graph, payload_store=payload_store)

    assert sink.results == []
    _assert_failed_token_outcome(db)


def test_sink_input_validation_violation_records_terminal_outcome() -> None:
    """Sink-boundary schema validation must also terminalize the tokens it fails."""
    db = LandscapeDB.in_memory()
    payload_store = MockPayloadStore()

    source, _tx_list, sinks, graph = build_linear_pipeline([{"value": 42}], transforms=[])
    sink = sinks["default"]
    sink.input_schema = _RequiresMissingField

    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[],
        sinks={"default": as_sink(sink)},
    )

    with pytest.raises(PluginContractViolation, match="input validation failed"):
        Orchestrator(db).run(config, graph=graph, payload_store=payload_store)

    assert sink.results == []
    _assert_failed_token_outcome(db)
