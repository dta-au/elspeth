"""Actual post-read monotonic observation, retained before a later finisher."""

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_attachment_result as owner
from elspeth.web.composer.continuation_batch_data import decode_prepared_batch_data, readmit_prepared_batch_audit
from elspeth.web.composer.continuation_dispatch_audit import capture_dispatch_audit, encode_dispatch_audit, re_admit_dispatch_audit
from elspeth.web.sessions.continuation_attachment_result import require_attachment_read_audit
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_attachment_result import refreshed, result_case, without_service

operation_store = test_composer_operation_authority.operation_store


def current_audit(prepared):
    call = prepared.batch.current_call
    return readmit_prepared_batch_audit(
        prepared,
        selected_call=call.tool_call,
        selected_pre_state=call.pre_state,
        current_dialect=prepared.dialect,
        actor=prepared.audit.actor,
        strict_sent=call.wire.strict_sent,
        wire_conformant=call.wire.wire_conformant,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("success", (False, True))
async def test_actual_post_read_observation_retains_original_start_and_completed_elapsed(operation_store, monkeypatch, success):
    kwargs, _path, _actors, encoded = await result_case(
        operation_store, monkeypatch, raw=b"ordinary inspected bytes" if success else b"\xff"
    )
    prepared = decode_prepared_batch_data(encoded)
    audit = current_audit(prepared)
    before = capture_dispatch_audit(audit)
    completed = []
    actual_reader = owner.read_current_attachment

    async def observe(**kw):
        value = await actual_reader(**kw)
        completed.append(capture_dispatch_audit(audit))
        return value

    monkeypatch.setattr(owner, "read_current_attachment", observe)
    await owner.run_attachment_result(**kwargs, audit=audit)
    kwargs = await refreshed(kwargs)
    result = await owner.read_attachment_result(**without_service(kwargs))
    assert result.result.success is success
    assert len(completed) == 1 and result.read_audit is not None
    assert result.read_audit.started_at == audit.started_at == prepared.audit.started_at
    assert result.read_audit.elapsed_active_ns >= completed[0].elapsed_active_ns > before.elapsed_active_ns
    original_timer = audit.started_ns
    await owner.apply_attachment_history(**without_service(kwargs))
    kwargs = await refreshed(kwargs)
    retained = await owner.read_attachment_result(**without_service(kwargs))
    assert encode_dispatch_audit(retained.read_audit) == encode_dispatch_audit(result.read_audit)
    assert audit.started_ns == original_timer
    call = retained.prepared.batch.current_call
    fresh = re_admit_dispatch_audit(
        retained.read_audit,
        selected_call=call.tool_call,
        pre_state=call.pre_state,
        audit_arguments=call.audit_arguments,
        actor=retained.read_audit.actor,
        strict_sent=call.wire.strict_sent,
        wire_conformant=call.wire.wire_conformant,
    )
    assert fresh is not audit and fresh.started_at == audit.started_at
    assert capture_dispatch_audit(fresh).elapsed_active_ns >= retained.read_audit.elapsed_active_ns
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_legacy_history_only_result_does_not_invent_completed_read_timing(operation_store, monkeypatch):
    kwargs, _path, _actors, _encoded = await result_case(operation_store, monkeypatch)
    await owner.run_attachment_result(**kwargs)
    kwargs = await refreshed(kwargs)
    value = await owner.read_attachment_result(**without_service(kwargs))
    assert value.read_audit is None
    await owner.apply_attachment_history(**without_service(kwargs))
    kwargs = await refreshed(kwargs)
    assert (await owner.read_attachment_result(**without_service(kwargs))).read_audit is None
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("elapsed", "started_at", "actor"))
async def test_completed_read_timing_refuses_changed_original_binding_after_positive(operation_store, monkeypatch, change):
    kwargs, _path, _actors, encoded = await result_case(operation_store, monkeypatch)
    prepared = decode_prepared_batch_data(encoded)
    audit = current_audit(prepared)
    actual = capture_dispatch_audit(audit)
    require_attachment_read_audit(prepared, actual, names=frozenset())
    if change == "elapsed":
        assert prepared.audit.elapsed_active_ns > 0
        altered = actual.model_copy(update={"elapsed_active_ns": prepared.audit.elapsed_active_ns - 1})
    elif change == "started_at":
        from datetime import timedelta

        altered = actual.model_copy(update={"started_at": actual.started_at + timedelta(seconds=1)})
    else:
        altered = actual.model_copy(update={"actor": "different-actual-actor"})
    with pytest.raises(AuditIntegrityError, match="original dispatch binding or elapsed"):
        require_attachment_read_audit(prepared, altered, names=frozenset())
    kwargs["required_work"].coordinator.assert_completed()
