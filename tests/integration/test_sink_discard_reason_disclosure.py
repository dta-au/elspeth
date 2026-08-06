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

from elspeth.contracts import ExecutionError
from elspeth.contracts.enums import RunStatus
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.secret_scrub import scrub_text_for_audit
from elspeth.core.landscape.schema import node_states_table
from elspeth.engine.executors.sink import SinkExecutor
from elspeth.web.execution.diagnostics import load_run_diagnostics_from_db
from tests.integration._helpers import (
    build_test_pipeline_with_discard_sink,
    build_test_pipeline_with_failsink_diversion,
    run_pipeline,
)

# The effect-path reason DivertingSink records via ``_diversion_log``
# (tests/fixtures/plugins.py, CollectSink.prepare_effect).
DIVERTING_SINK_REASON = "collect sink rejected configured row"


def _state_errors_of_type(db, run_id: str, exception_type: str) -> list[dict[str, object]]:
    with db.read_only_connection() as conn:
        return [
            json.loads(row.error_json)
            for row in conn.execute(
                select(node_states_table.c.error_json)
                .where(node_states_table.c.run_id == run_id)
                .where(node_states_table.c.status == "failed")
                .where(node_states_table.c.error_json.isnot(None))
            )
            if json.loads(row.error_json)["type"] == exception_type
        ]


def _discard_state_errors(db, run_id: str) -> list[dict[str, object]]:
    return _state_errors_of_type(db, run_id, "SinkDiscard")


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


class TestFailsinkAnchorDisclosesSinkReason:
    def test_failsink_primary_anchor_carries_the_reason_not_the_hash(self, tmp_path, monkeypatch) -> None:
        """Quarantining a row must not leave its primary anchor mute.

        A failsink-mode diversion writes the row to a quarantine sink, but the
        PRIMARY anchor's failed state is the same audit column, and the same
        operator surface, as the discard anchor's.
        """
        config, graph, db, store = build_test_pipeline_with_failsink_diversion(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            diverted_row_count=2,
            success_row_count=1,
        )
        result = run_pipeline(config, graph, db, store)

        errors = _state_errors_of_type(db, result.run_id, "SinkDiversion")
        assert len(errors) == 2
        for error in errors:
            assert error["exception"] == DIVERTING_SINK_REASON
            assert error["context"]["diversion_reason_hash"] == stable_hash({"diversion_reason": DIVERTING_SINK_REASON})


class TestOperatorSurfaceDisclosesSinkReason:
    def test_diagnostics_response_carries_the_discard_reason(self, tmp_path, monkeypatch) -> None:
        """Close the loop the ticket actually asked about: the read path.

        Asserting on the DB proves the reason was written; only the diagnostics
        projection proves an operator can reach it. ``RunDiagnosticDiscard``
        names ``tokens`` as the carrier for sink discards, so that is the field
        under test.
        """
        config, graph, db, store = build_test_pipeline_with_discard_sink(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            success_row_count=0,
            discard_row_count=2,
        )
        result = run_pipeline(config, graph, db, store)

        response = load_run_diagnostics_from_db(
            db,
            run_id=result.run_id,
            landscape_run_id=result.run_id,
            run_status="failed",
        )

        assert response.failure_detail is None, "no operation failed; the reason must not need one"
        discard_states = [
            state for token in response.tokens for state in token.states if state.status == "failed" and state.error is not None
        ]
        assert len(discard_states) == 2
        for state in discard_states:
            assert state.error["exception"] == DIVERTING_SINK_REASON
            assert state.error["context"]["diversion_reason_hash"] == stable_hash({"diversion_reason": DIVERTING_SINK_REASON})


class TestDisclosureNeverContradictsItsOwnHash:
    """The recorded text is always checkable against the hash beside it.

    ``ExecutionError`` scrubs ``exception`` by replacing the WHOLE string, so a
    reason quoting a driver error could otherwise persist as
    ``<redacted-secret>`` next to the hash of the unredacted original — the one
    shape that would make ``context.diversion_reason_hash`` a lie. The fallback
    keeps exactly two legal values, and both check out.
    """

    def test_a_scrubbable_reason_falls_back_to_the_hash_form(self) -> None:
        secret_bearing = "Constraint violation: password=hunter2 already exists"  # secret-scan: allow-this-line
        assert scrub_text_for_audit(secret_bearing) != secret_bearing

        reason_hash = stable_hash({"diversion_reason": secret_bearing})
        disclosed = SinkExecutor._disclosable_diversion_reason(reason=secret_bearing, reason_hash=reason_hash)

        assert disclosed == f"effect-diversion:{reason_hash}"
        # The scrubber is a no-op on what we chose, so ExecutionError persists it
        # verbatim rather than redacting a second time into a third value.
        assert ExecutionError(exception=disclosed, exception_type="SinkDiscard").exception == disclosed

    def test_a_clean_reason_is_disclosed_verbatim_and_hashes_to_its_attribution(self) -> None:
        reason = "Text values cannot contain CR or LF record separators"
        reason_hash = stable_hash({"diversion_reason": reason})

        disclosed = SinkExecutor._disclosable_diversion_reason(reason=reason, reason_hash=reason_hash)

        assert disclosed == reason
        assert stable_hash({"diversion_reason": disclosed}) == reason_hash

    def test_an_already_recovered_reason_stays_the_hash_form(self) -> None:
        """A recovered batch's RowDiversion.reason is already the hash form."""
        reason_hash = "d4" * 32
        hash_form = f"effect-diversion:{reason_hash}"

        assert SinkExecutor._disclosable_diversion_reason(reason=hash_form, reason_hash=reason_hash) == hash_form
