"""Actual pending/settled TITLE audit admission against owned SQL anomalies.

The only external doubles are raw SDK/guard transport. Malformed known-kind
JSON is inserted into the real selected session inside a rollback savepoint;
no append-only trigger, Row, current claim or result source is substituted.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import func, insert, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer import continuation_title_provider as driver
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.visible_tool_event import validate_visible_response_gap, visible_response_gap
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import token_usage_entry_from_llm_call_envelope
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions import continuation_title_provider as owner
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_continuation_ingress import recovery_rows
from tests.unit.web.sessions.test_continuation_title_application import complete_rows_on_connection, reached_or_failure
from tests.unit.web.sessions.test_continuation_title_provider import observer, raw_reply, title_case

operation_store = operations.operation_store


@asynccontextmanager
async def actual_title_cut(store, tmp_path, monkeypatch, *, settled=False):
    running, service, owner_work, prepared, guard_requests = await title_case(store, tmp_path, monkeypatch)
    calls = observer(monkeypatch)
    reached, resume = asyncio.Event(), asyncio.Event()
    real_finish = type(store[3]).finish_provider_attempt
    sdk = []
    task = None
    read_work = inventory_work(running)

    async def paused_finish(self, **kwargs):
        if kwargs["session_operation_context"] is running.session_operation_context:
            reached.set()
            await resume.wait()
        return await real_finish(self, **kwargs)

    async def completion(**kwargs):
        _assert_required_guard_admitted(
            store[0], running.session_operation_context, guard_requests, role="composer", user_prompt="Preserve café and original context"
        )
        sdk.append(kwargs)
        return raw_reply(kwargs["model"])

    with monkeypatch.context() as patch:
        patch.setattr(type(store[3]), "finish_provider_attempt", paused_finish)
        patch.setattr(litellm, "acompletion", completion)
        try:
            task = asyncio.create_task(
                driver.run_prepared_title_provider(
                    prepared=prepared, invocation_owner=ProviderInvocationOwner(service=store[3], required_work=owner_work)
                )
            )
            await reached_or_failure(task, reached)
            current = await driver.read_current_title_result(service=service, running=running, required_work=read_work)
            assert not current.settled and len(calls) == len(sdk) == 1
            assert current.settlement.audit_call == calls[0] and current.settlement.audit_call.call_id is not None
            root = decode_continuation_snapshot(current.selected.payload_json)
            assert root.pending_settlement_ids == (calls[0].call_id,)
            with store[0].connect() as conn:
                attempt = conn.execute(select(physical).where(physical.c.attempt_id == calls[0].call_id)).one()
                assert attempt.settled_at is None and attempt.ledger_entry_id is None
                assert conn.execute(select(usage)).all() == []
            read_work.coordinator.assert_completed()
            if settled:
                resume.set()
                original = await task
                assert original is not None and original.audit_call is calls[0]
                current = await driver.read_current_title_result(service=service, running=running, required_work=read_work)
                assert current.settled
                with store[0].connect() as conn:
                    attempt = conn.execute(select(physical).where(physical.c.attempt_id == calls[0].call_id)).one()
                    assert attempt.settled_at is not None and attempt.ledger_entry_id is not None
                    assert len(conn.execute(select(usage)).all()) == 1
                read_work.coordinator.assert_completed()
            yield running, service, read_work, current, sdk, calls
        finally:
            resume.set()
            try:
                if task is not None:
                    original = await task
                    assert original is not None and original.audit_call is calls[0]
                    assert len(calls) == len(sdk) == 1
            finally:
                read_work.coordinator.assert_completed()
                owner_work.coordinator.assert_completed()
                store[1].release(running.session_operation_context)


def legacy_data_companions(current, running):
    # A distinct, explicitly unbound legacy DATA fixture, passed through the
    # actual public producers. It is never used as the real TITLE audit/source,
    # charged, settled or installed into the retained physical response.
    original = current.settlement.audit_call
    assert original.call_id is not None
    legacy = replace(original, call_id=None)
    public = deep_thaw(llm_call_audit_envelope(legacy))
    gap = visible_response_gap(legacy, running.session_operation_context, public)
    entry = token_usage_entry_from_llm_call_envelope(public)
    assert entry is not None and entry.call_id is None
    assert public["call"]["call_id"] is None and gap["provider_call_id"] is None
    validate_visible_response_gap(gap, public)
    assert current.settlement.audit_call is original and original.call_id is not None
    return public, gap


def anomaly_envelopes(current, running, case):
    public, gap = legacy_data_companions(current, running)
    if case == "missing_call":
        del public["call"]
    elif case == "null_call":
        public["call"] = None
    elif case == "wrong_call":
        public["call"] = []
    elif case == "missing_call_id":
        del public["call"]["call_id"]
    elif case == "missing_accounting_field":
        del public["call"]["prompt_tokens"]
    elif case == "invalid_call_id":
        public["call"]["call_id"] = 17
    elif case == "gap_missing_id":
        del gap["provider_call_id"]
        return [public, gap]
    elif case == "gap_missing_companion":
        return [gap]
    else:
        raise AssertionError(case)
    return [public]


async def read_actual_sql_anomaly(store, running, work, current, envelopes, *, role="audit", refusal=None, cause=None):
    """Read one genuine current TITLE owner with real savepoint-visible rows.

    The raw INSERT is a deliberate owned-storage fault fixture, not an audited
    producer claim. The savepoint rolls it back even on the original RED.
    Complete same-connection snapshots observe reader writes rather than
    hiding/filtering them; after rollback the outer database stays exact.
    """
    engine, sid = store[0], str(store[4])

    def read():
        with locked_session_transaction(engine, sid) as conn:
            positive = owner.read_title_result_on_connection(
                conn, running, selected=current.selected, inventory=current.settlement.inventory
            )
            assert positive == current
            baseline = complete_rows_on_connection(conn)
            savepoint = conn.begin_nested()
            try:
                next_sequence = (
                    conn.execute(
                        select(func.coalesce(func.max(messages.c.sequence_no), 0)).where(messages.c.session_id == sid)
                    ).scalar_one()
                    + 1
                )
                row_id = str(uuid4())
                values = deepcopy(envelopes)
                conn.execute(
                    insert(messages).values(
                        id=row_id,
                        session_id=sid,
                        role=role,
                        content="Test-only owned-storage envelope anomaly",
                        raw_content=None,
                        tool_calls=values,
                        tool_call_id=None,
                        parent_assistant_id=None,
                        composition_state_id=None,
                        sequence_no=next_sequence,
                        writer_principal="compose_loop",
                        created_at=database_now(conn),
                    )
                )
                row = conn.execute(select(messages).where(messages.c.id == row_id, messages.c.session_id == sid)).one()
                assert row.tool_calls == values and row.sequence_no == next_sequence
                inserted = complete_rows_on_connection(conn)
                if refusal is None:
                    actual = owner.read_title_result_on_connection(
                        conn, running, selected=current.selected, inventory=current.settlement.inventory
                    )
                    assert actual == current
                else:
                    with pytest.raises(AuditIntegrityError, match=refusal) as caught:
                        owner.read_title_result_on_connection(
                            conn, running, selected=current.selected, inventory=current.settlement.inventory
                        )
                    if cause is not None:
                        assert type(caught.value.__cause__) is cause
                    else:
                        assert caught.value.__cause__ is None
                assert complete_rows_on_connection(conn) == inserted
            finally:
                savepoint.rollback()
                assert complete_rows_on_connection(conn) == baseline
            return owner.read_title_result_on_connection(conn, running, selected=current.selected, inventory=current.settlement.inventory)

    before = recovery_rows(engine)
    result = await _registered(
        work=work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=read, expected=owner.CurrentTitleResult
    )
    assert result == current and recovery_rows(engine) == before
    work.coordinator.assert_completed()
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,cause",
    [
        pytest.param("missing_call", KeyError, id="missing-call"),
        pytest.param("null_call", TypeError, id="null-call"),
        pytest.param("wrong_call", TypeError, id="wrong-call"),
        pytest.param("missing_call_id", KeyError, id="missing-call-id"),
        pytest.param("missing_accounting_field", KeyError, id="missing-accounting-field"),
        pytest.param("invalid_call_id", ValueError, id="invalid-call-id"),
    ],
)
async def test_genuine_pending_title_refuses_malformed_known_public_audit_without_writes(
    operation_store, tmp_path, monkeypatch, case, cause
):
    async with actual_title_cut(operation_store, tmp_path, monkeypatch) as (running, _service, work, current, sdk, calls):
        public, gap = legacy_data_companions(current, running)
        # Real pending empty cohort was admitted by the helper. A legitimate
        # unbound legacy pair is also admitted before its malformed sibling.
        await read_actual_sql_anomaly(operation_store, running, work, current, [public, gap])
        await read_actual_sql_anomaly(
            operation_store,
            running,
            work,
            current,
            anomaly_envelopes(current, running, case),
            refusal="TITLE stored public LLM-call audit violates its required accounting shape",
            cause=cause,
        )
        assert len(sdk) == len(calls) == 1 and not current.settled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,refusal",
    [
        pytest.param("gap_missing_id", "Stored visible-response gap lost its exact public call binding", id="missing-gap-id"),
        pytest.param(
            "gap_missing_companion", "TITLE stored response gap lacks its one actual public-call companion", id="missing-companion"
        ),
    ],
)
async def test_genuine_pending_title_refuses_malformed_known_gap_without_writes(operation_store, tmp_path, monkeypatch, case, refusal):
    async with actual_title_cut(operation_store, tmp_path, monkeypatch) as (running, _service, work, current, sdk, calls):
        public, gap = legacy_data_companions(current, running)
        await read_actual_sql_anomaly(operation_store, running, work, current, [public, gap])
        await read_actual_sql_anomaly(operation_store, running, work, current, anomaly_envelopes(current, running, case), refusal=refusal)
        assert len(sdk) == len(calls) == 1 and not current.settled


@pytest.mark.asyncio
@pytest.mark.parametrize("settled", (False, True))
async def test_actual_title_reader_preserves_heterogeneous_and_explicit_null_id_metadata(operation_store, tmp_path, monkeypatch, settled):
    async with actual_title_cut(operation_store, tmp_path, monkeypatch, settled=settled) as (running, _service, work, current, sdk, calls):
        public, gap = legacy_data_companions(current, running)
        await read_actual_sql_anomaly(operation_store, running, work, current, [public, gap])
        # Actual prompt-guard rows already remain present in the SQL baseline.
        # An assistant request has no public-call/gap fields and is unrelated.
        request = {
            "id": "ordinary-unrelated-request",
            "type": "function",
            "function": {"name": "get_expression_grammar", "arguments": "{}"},
        }
        await read_actual_sql_anomaly(operation_store, running, work, current, [request], role="assistant")
        assert current.settled is settled and len(sdk) == len(calls) == 1


@pytest.mark.asyncio
async def test_settled_title_complete_cohort_still_rejects_unrelated_malformed_known_audit(operation_store, tmp_path, monkeypatch):
    async with actual_title_cut(operation_store, tmp_path, monkeypatch, settled=True) as (running, _service, work, current, sdk, calls):
        assert current.settled
        await read_actual_sql_anomaly(
            operation_store,
            running,
            work,
            current,
            anomaly_envelopes(current, running, "missing_call_id"),
            refusal="TITLE stored public LLM-call audit violates its required accounting shape",
            cause=KeyError,
        )
        assert len(sdk) == len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settled,refusal",
    [
        pytest.param(False, "TITLE pending result has partial or foreign settled accounting", id="pending-partial-cohort"),
        pytest.param(True, "TITLE completed accounting lost its exact complete cohort/ledger", id="settled-extra-correlated-row"),
    ],
)
async def test_actual_title_reader_retains_exact_correlated_cohort_refusals(operation_store, tmp_path, monkeypatch, settled, refusal):
    async with actual_title_cut(operation_store, tmp_path, monkeypatch, settled=settled) as (running, _service, work, current, sdk, calls):
        # This is valid actual public metadata for the target physical call;
        # its unexpected row must stay related and fail existing exact checks.
        target_public = deep_thaw(llm_call_audit_envelope(current.settlement.audit_call))
        await read_actual_sql_anomaly(operation_store, running, work, current, [target_public], refusal=refusal)
        assert len(sdk) == len(calls) == 1
