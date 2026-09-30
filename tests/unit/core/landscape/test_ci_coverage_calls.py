"""Audit calls and export readers fail closed on damaged durable evidence."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.enums import FrameKind, RunMode, RunStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.payload_store import IntegrityError as PayloadIntegrityError
from elspeth.core.landscape.errors import LandscapePostCommitError
from elspeth.core.landscape.export_read_model import open_export_read_transaction
from elspeth.core.landscape.exporter import LandscapeExporter, RecorderFactoryExportReadModel
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.row_data import CallDataState
from elspeth.core.landscape.schema import (
    call_verifications_table,
    calls_table,
    run_attributions_table,
    runs_table,
    token_lineage_frames_table,
)
from tests.fixtures.landscape import (
    RecorderSetup,
    claim_test_work_item,
    leader_coordination_token,
    make_recorder_with_run,
    register_test_node,
)
from tests.fixtures.stores import MockPayloadStore


@pytest.fixture
def setup() -> Iterator[RecorderSetup]:
    prepared = make_recorder_with_run(run_id="source", source_node_id="source-node")
    try:
        yield prepared
    finally:
        prepared.db.close()


def _operation_call(setup: RecorderSetup, *, request_ref: str | None = None):
    operation = setup.execution.begin_operation(setup.source_node_id, "source_load", coordination_token=setup.coordination_token)
    return setup.execution.record_operation_call(
        operation.operation_id,
        CallType.HTTP,
        CallStatus.SUCCESS,
        request_data=RawCallPayload({"url": "https://example.test/data"}),
        response_data=RawCallPayload({"status": 200}),
        request_ref=request_ref,
        coordination_token=setup.coordination_token,
    )


def test_request_archive_distinguishes_unknown_hash_only_and_missing_store(setup: RecorderSetup) -> None:
    assert setup.execution.get_call_request_data("missing").state is CallDataState.CALL_NOT_FOUND
    call = _operation_call(setup)
    assert setup.execution.get_call_request_data(call.call_id).data == {"url": "https://example.test/data"}
    without_store = RecorderFactory(setup.db)
    assert without_store.execution.get_call_request_data(call.call_id).state is CallDataState.STORE_NOT_CONFIGURED
    assert without_store.execution.get_call_response_data(call.call_id).state is CallDataState.STORE_NOT_CONFIGURED
    with setup.db.engine.begin() as connection:
        connection.execute(calls_table.update().where(calls_table.c.call_id == call.call_id).values(request_ref=None))
    assert setup.execution.get_call_request_data(call.call_id).state is CallDataState.HASH_ONLY


def test_operation_call_refuses_untyped_token_usage_before_persisting(setup: RecorderSetup) -> None:
    operation = setup.execution.begin_operation(setup.source_node_id, "source_load", coordination_token=setup.coordination_token)
    with pytest.raises(TypeError, match="token_usage must be TokenUsage"):
        setup.execution.record_operation_call(
            operation.operation_id,
            CallType.HTTP,
            CallStatus.SUCCESS,
            request_data=RawCallPayload({"url": "https://example.test/data"}),
            coordination_token=setup.coordination_token,
            token_usage=object(),
        )
    assert setup.execution.get_operation_calls(operation.operation_id) == []
    valid = setup.execution.record_operation_call(
        operation.operation_id,
        CallType.HTTP,
        CallStatus.SUCCESS,
        request_data=RawCallPayload({"url": "https://example.test/data"}),
        coordination_token=setup.coordination_token,
    )
    assert valid.call_index == 0


def test_request_archive_reports_retention_purge_without_inventing_data() -> None:
    store = MockPayloadStore()
    setup = make_recorder_with_run(payload_store=store)
    try:
        call = _operation_call(setup)
        assert call.request_ref is not None
        assert store.delete(call.request_ref)
        result = setup.execution.get_call_request_data(call.call_id)
        assert result.state is CallDataState.PURGED
        assert result.data is None
    finally:
        setup.db.close()


@pytest.mark.parametrize("failure", [OSError("backend unavailable"), PayloadIntegrityError("damaged archive")])
def test_request_archive_retrieval_failure_is_audit_corruption(failure: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    store = MockPayloadStore()
    setup = make_recorder_with_run(payload_store=store)

    def refuse_retrieve(_reference: str) -> bytes:
        raise failure

    try:
        call = _operation_call(setup)
        monkeypatch.setattr(store, "retrieve", refuse_retrieve)
        with pytest.raises(AuditIntegrityError, match="payload retrieval failed") as caught:
            setup.execution.get_call_request_data(call.call_id)
        assert caught.value.__cause__ is failure
    finally:
        setup.db.close()


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        pytest.param(b"\xff", "Corrupt call request", id="invalid-utf8"),
        pytest.param(b"{broken", "Corrupt call request", id="invalid-json"),
        pytest.param(b"[1, 2]", "not a JSON object", id="array"),
        pytest.param(b'{"url":"https://example.test/tampered"}', "hash mismatch", id="different-object"),
    ],
)
def test_request_archive_rejects_valid_store_objects_that_do_not_match_call_evidence(replacement: bytes, message: str) -> None:
    store = MockPayloadStore()
    setup = make_recorder_with_run(payload_store=store)
    try:
        call = _operation_call(setup)
        replacement_ref = store.store(replacement)
        with setup.db.engine.begin() as connection:
            connection.execute(calls_table.update().where(calls_table.c.call_id == call.call_id).values(request_ref=replacement_ref))
        with pytest.raises(AuditIntegrityError, match=message):
            setup.execution.get_call_request_data(call.call_id)
    finally:
        setup.db.close()


@pytest.mark.parametrize("failure", [OSError("backend unavailable"), ValueError("failed to archive")])
def test_payload_materialization_failure_preserves_committed_call(failure: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    store = MockPayloadStore()
    setup = make_recorder_with_run(payload_store=store)

    def refuse_store(_content: bytes) -> str:
        raise failure

    try:
        monkeypatch.setattr(store, "store", refuse_store)
        with pytest.raises(LandscapePostCommitError, match="materialization failed") as caught:
            _operation_call(setup)
        assert caught.value.__cause__ is failure
        calls = setup.execution.get_all_calls_for_run(setup.run_id)
        assert len(calls) == 1
        assert calls[0].request_ref is None
        assert calls[0].request_hash is not None
        assert setup.execution.get_call_request_data(calls[0].call_id).state is CallDataState.HASH_ONLY
    finally:
        setup.db.close()


@pytest.mark.parametrize("parent", ["state", "operation"])
def test_reference_update_failure_preserves_call_after_external_effect(setup: RecorderSetup, parent: str) -> None:
    def refuse_reference_update(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if statement.startswith("UPDATE calls SET request_ref="):
            raise OperationalError(statement, {}, OSError("database unavailable"))

    event.listen(setup.db.engine, "before_cursor_execute", refuse_reference_update)
    try:
        with pytest.raises(LandscapePostCommitError, match="payload reference update failed"):
            if parent == "operation":
                _operation_call(setup)
            else:
                node_id = register_test_node(setup.data_flow, setup.run_id, "transform-node")
                _, token = setup.data_flow.create_row_with_token(
                    coordination_token=setup.coordination_token,
                    source_node_id=setup.source_node_id,
                    row_index=0,
                    source_row_index=0,
                    ingest_sequence=0,
                    data={"value": 1},
                )
                state = setup.execution.begin_node_state(
                    token_id=token.token_id,
                    node_id=node_id,
                    member_token=setup.coordination_token.membership,
                    step_index=0,
                    input_data={"value": 1},
                )
                claim = claim_test_work_item(
                    setup.factory,
                    member_token=setup.coordination_token.membership,
                    token_id=token.token_id,
                    node_id=node_id,
                )
                setup.execution.record_call(
                    state_id=state.state_id,
                    member_token=setup.coordination_token.membership,
                    work_item=claim,
                    call_index=0,
                    call_type=CallType.HTTP,
                    status=CallStatus.SUCCESS,
                    request_data=RawCallPayload({"url": "https://example.test/data"}),
                )
    finally:
        event.remove(setup.db.engine, "before_cursor_execute", refuse_reference_update)
    calls = setup.execution.get_all_calls_for_run(setup.run_id)
    assert len(calls) == 1
    assert calls[0].request_hash is not None
    assert calls[0].request_ref is None


@pytest.mark.parametrize(
    ("differences", "is_match", "source_call_id", "error_type", "message"),
    [
        pytest.param("{}", 1, "source-call", TypeError, "is_match must be bool", id="integer-match"),
        pytest.param("{broken", False, None, ValueError, "valid finite JSON", id="malformed-json"),
        pytest.param('{"value": NaN}', False, None, ValueError, "valid finite JSON", id="nonfinite-json"),
        pytest.param("[]", False, None, ValueError, "encode an object", id="array-differences"),
        pytest.param("{}", True, None, ValueError, "requires a source call", id="match-without-source"),
        pytest.param('{"status": 500}', True, "source-call", ValueError, "no differences", id="match-with-differences"),
    ],
)
def test_verification_invalid_claims_leave_no_durable_decision(
    setup: RecorderSetup, differences, is_match, source_call_id, error_type, message
) -> None:
    with pytest.raises(error_type, match=message):
        setup.execution.record_verification_decision(
            current_run_id=setup.run_id,
            current_call_id="current-call",
            source_run_id="other-run",
            source_call_id=source_call_id,
            is_match=is_match,
            differences_json=differences,
            coordination_token=setup.coordination_token,
        )
    with setup.db.engine.connect() as connection:
        assert connection.execute(select(call_verifications_table)).all() == []


def test_verification_cannot_compare_a_run_against_itself(setup: RecorderSetup) -> None:
    with pytest.raises(AuditIntegrityError, match="source and current runs must differ"):
        setup.execution.record_verification_decision(
            current_run_id=setup.run_id,
            current_call_id="current-call",
            source_run_id=setup.run_id,
            source_call_id=None,
            is_match=None,
            differences_json="{}",
            coordination_token=setup.coordination_token,
        )
    assert setup.execution.get_verification_decisions_for_run(setup.run_id) == []


@pytest.mark.parametrize("current_call", ["missing", "source"])
def test_verification_rejects_missing_or_wrong_run_current_call(setup: RecorderSetup, current_call: str) -> None:
    source_call = _operation_call(setup)
    setup.run_lifecycle.begin_run(
        config={}, canonical_version="v1", run_id="current", run_mode=RunMode.VERIFY, replay_from_run_id=setup.run_id
    )
    with pytest.raises(AuditIntegrityError, match="current call is missing or belongs to another run"):
        setup.execution.record_verification_decision(
            current_run_id="current",
            current_call_id="missing" if current_call == "missing" else source_call.call_id,
            source_run_id=setup.run_id,
            source_call_id=None,
            is_match=None,
            differences_json="{}",
            coordination_token=leader_coordination_token(setup.factory, "current"),
        )
    assert setup.execution.get_verification_decisions_for_run("current") == []


@pytest.mark.parametrize(
    ("state_id", "operation_id", "message"),
    [
        (None, None, "exactly one"),
        ("state", "op", "exactly one"),
        ("missing", None, "current state is missing"),
        (None, "missing", "current operation is missing"),
    ],
)
def test_replay_lookup_refuses_invalid_or_missing_parents(setup: RecorderSetup, state_id, operation_id, message) -> None:
    error_type = ValueError if message == "exactly one" else AuditIntegrityError
    with pytest.raises(error_type, match=message):
        setup.execution.list_source_calls_for_current_parent(
            source_run_id=setup.run_id, call_type=CallType.HTTP, current_state_id=state_id, current_operation_id=operation_id
        )


@pytest.mark.parametrize("index", [-1, True, 0.5])
def test_replay_lookup_refuses_noninteger_or_negative_occurrence(setup: RecorderSetup, index) -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
        setup.execution.find_call_for_current_parent(
            source_run_id=setup.run_id,
            call_type=CallType.HTTP,
            request_hash=None,
            current_state_id=None,
            current_operation_id="missing",
            current_call_index=index,
        )


@pytest.mark.parametrize("batch_size", [0, -1, True, 1.5])
def test_snapshot_readers_refuse_invalid_batch_sizes(setup: RecorderSetup, batch_size) -> None:
    with open_export_read_transaction(setup.db.engine) as model:
        with pytest.raises(ValueError, match="positive exact integer"):
            list(model.iter_rows_for_run(setup.run_id, batch_size=batch_size))
        with pytest.raises(ValueError, match="positive exact integer"):
            list(model.iter_auth_events(datetime.now(UTC), batch_size=batch_size))


@pytest.mark.parametrize(
    ("user_id", "provider", "message"),
    [("", "local", "user ID is corrupt"), ("user", "", "provider type is corrupt"), ("user", "local", None)],
)
def test_snapshot_attribution_is_preserved_or_rejected_as_corrupt(setup: RecorderSetup, user_id, provider, message) -> None:
    with setup.db.engine.begin() as connection:
        # Emulate damaged persisted evidence, including a value refused by
        # the normal writer's CHECK, to exercise the reader independently.
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        try:
            connection.execute(
                run_attributions_table.insert().values(
                    run_id=setup.run_id, initiated_by_user_id=user_id, auth_provider_type=provider, recorded_at=datetime.now(UTC)
                )
            )
        finally:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    with open_export_read_transaction(setup.db.engine) as model:
        if message is None:
            assert model.get_run_attribution(setup.run_id) == ("user", "local")
        else:
            with pytest.raises(AuditIntegrityError, match=message):
                model.get_run_attribution(setup.run_id)


def test_snapshot_terminal_witness_distinguishes_missing_run(setup: RecorderSetup) -> None:
    with open_export_read_transaction(setup.db.engine) as model, pytest.raises(ValueError, match="Run not found"):
        model.get_export_terminal_witness("missing")


def test_snapshot_terminal_witness_rejects_unrecognized_persisted_status(setup: RecorderSetup) -> None:
    with setup.db.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        try:
            connection.execute(runs_table.update().where(runs_table.c.run_id == setup.run_id).values(status="invented-terminal"))
        finally:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    with (
        open_export_read_transaction(setup.db.engine) as model,
        pytest.raises(AuditIntegrityError, match="invalid persisted status"),
    ):
        model.get_export_terminal_witness(setup.run_id)


@pytest.mark.parametrize("depth", [0, 1])
def test_snapshot_lineage_requires_dense_depths(setup: RecorderSetup, depth: int) -> None:
    _, token = setup.data_flow.create_row_with_token(
        coordination_token=setup.coordination_token,
        source_node_id=setup.source_node_id,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        data={"value": 1},
    )
    with setup.db.engine.begin() as connection:
        connection.execute(
            token_lineage_frames_table.insert().values(
                token_id=token.token_id,
                run_id=setup.run_id,
                depth=depth,
                kind=FrameKind.FORK,
                group_id="fork-group",
                member_key="left",
            )
        )
    with open_export_read_transaction(setup.db.engine) as model:
        if depth == 1:
            with pytest.raises(AuditIntegrityError, match="non-dense depths"):
                model.get_lineage_paths_for_tokens(setup.run_id, [token.token_id])
        else:
            paths = model.get_lineage_paths_for_tokens(setup.run_id, [token.token_id])
            assert len(paths[token.token_id]) == 1
            frame = paths[token.token_id][0]
            assert (frame.kind, frame.group_id, frame.member_key) == (FrameKind.FORK, "fork-group", "left")


def test_export_refuses_unknown_auth_mode_and_signing_without_key(setup: RecorderSetup) -> None:
    with pytest.raises(ValueError, match="auth_events must be"):
        LandscapeExporter(setup.db, auth_events="unknown")
    with pytest.raises(ValueError, match="no signing_key"):
        LandscapeExporter(setup.db).derive_run_bundle(setup.run_id, sign=True)


def test_caller_owned_read_model_still_requires_terminal_run_and_signer_identity(setup: RecorderSetup) -> None:
    adapter = RecorderFactoryExportReadModel(setup.factory)
    with pytest.raises(AuditIntegrityError, match="snapshot-bound"):
        list(adapter.iter_auth_events(datetime.now(UTC), batch_size=1))
    with pytest.raises(ValueError, match="immutable export-terminal"):
        LandscapeExporter(setup.db, read_model=adapter).derive_run_bundle(setup.run_id)
    with setup.db.engine.begin() as connection:
        connection.execute(
            runs_table.update()
            .where(runs_table.c.run_id == setup.run_id)
            .values(status=RunStatus.COMPLETED, completed_at=datetime.now(UTC))
        )
    with pytest.raises(ValueError, match="explicit signer_key_id"):
        LandscapeExporter(setup.db, read_model=adapter, signing_key=b"test-export-key").derive_run_bundle(setup.run_id, sign=True)


def test_export_refuses_corrupt_settings_json_instead_of_sanitizing_it(setup: RecorderSetup) -> None:
    with setup.db.engine.begin() as connection:
        connection.execute(
            runs_table.update()
            .where(runs_table.c.run_id == setup.run_id)
            .values(status=RunStatus.COMPLETED, completed_at=datetime.now(UTC), settings_json="{broken")
        )
    with pytest.raises(AuditIntegrityError, match="Corrupt settings_json"):
        list(LandscapeExporter(setup.db, compartment_id="test-compartment").export_run(setup.run_id))
