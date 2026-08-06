"""Discard-mode sink diversions must disclose the sink's own reason.

``RunDiagnosticDiscard`` (web/execution/schemas.py) documents the contract:
source validation is the one discard class with no token trail, and every
other stage — including sink discard — "leaves a token whose failed node
state already discloses the reason through ``tokens``".  A discard node
state carrying only ``effect-diversion:<hash>`` breaks that promise, which
is how a 0-byte output became undiagnosable in battery round 6
(elspeth-9595abb7b0).
"""

from __future__ import annotations

import json

from sqlalchemy import select

from elspeth.contracts.enums import RunStatus
from elspeth.contracts.hashing import stable_hash
from elspeth.core.landscape.schema import node_states_table
from tests.integration._helpers import build_test_pipeline_with_discard_sink, run_pipeline

# The effect-path reason DivertingSink records via ``_diversion_log``
# (tests/fixtures/plugins.py, CollectSink.prepare_effect).
DIVERTING_SINK_REASON = "collect sink rejected configured row"


def _discard_state_errors(db, run_id: str) -> list[dict[str, object]]:
    with db.read_only_connection() as conn:
        return [
            json.loads(row.error_json)
            for row in conn.execute(
                select(node_states_table.c.error_json)
                .where(node_states_table.c.run_id == run_id)
                .where(node_states_table.c.status == "failed")
                .where(node_states_table.c.error_json.isnot(None))
            )
            if json.loads(row.error_json)["type"] == "SinkDiscard"
        ]


class TestDiscardStateDisclosesSinkReason:
    def test_discard_node_state_carries_the_sinks_human_readable_reason(self, tmp_path, monkeypatch) -> None:
        """The whole run failed on discards; the node state must say why."""
        config, graph, db, store = build_test_pipeline_with_discard_sink(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            success_row_count=0,
            discard_row_count=2,
        )
        result = run_pipeline(config, graph, db, store)
        assert result.status == RunStatus.FAILED

        errors = _discard_state_errors(db, result.run_id)
        assert len(errors) == 2
        for error in errors:
            assert error["exception"] == DIVERTING_SINK_REASON
            assert error["type"] == "SinkDiscard"
            assert error["phase"] == "write"

    def test_discard_state_error_stays_bound_to_the_durable_attribution(self, tmp_path, monkeypatch) -> None:
        """The disclosed reason is verifiable against the durable reason hash."""
        config, graph, db, store = build_test_pipeline_with_discard_sink(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            success_row_count=1,
            discard_row_count=1,
        )
        result = run_pipeline(config, graph, db, store)

        errors = _discard_state_errors(db, result.run_id)
        assert len(errors) == 1
        context = errors[0]["context"]
        assert context["diversion_reason_hash"] == stable_hash({"diversion_reason": DIVERTING_SINK_REASON})
