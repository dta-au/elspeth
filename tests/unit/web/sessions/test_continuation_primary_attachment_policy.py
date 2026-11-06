"""PRIMARY refuses unsupported attachment dispatch; semantic Read inspects originals."""

import time
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import insert

from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.attachment_admission import AttachmentInspectionPolicy
from elspeth.web.composer.continuation_semantic_advisor_read import execute_semantic_advisor_read
from elspeth.web.composer.tools import blobs
from elspeth.web.sessions.continuation_semantic_actions import install_semantic_action_on_connection, prepare_semantic_read_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import blobs_table
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_attachment_read import _set_current_inspection_limit
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case
from tests.unit.web.sessions.test_continuation_semantic_actions import _read_answer
from tests.unit.web.sessions.test_continuation_semantic_advisor import _run_paid_semantic, _semantic_case

operation_store = _operation_store


def _install_attachment(store, service, running, blob_id, raw, limit):
    _set_current_inspection_limit(service, limit)
    sessions = service._require_sessions_service()
    assert sessions._data_dir is not None
    path = sessions._data_dir / "blobs" / str(running.claim.session_id) / f"{blob_id}_notes.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with store[0].begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id=blob_id,
                session_id=str(running.claim.session_id),
                filename="notes.txt",
                mime_type="text/plain",
                size_bytes=len(raw),
                content_hash=sha256(raw).hexdigest(),
                status="ready",
                created_by="user",
                creation_modality="verbatim",
                storage_path=str(path),
                created_at=datetime.now(UTC),
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ("read_attachment", "get_blob_content"))
@pytest.mark.parametrize("over_limit", (False, True))
async def test_primary_attachment_refuses_unbounded_tail_before_original_inspection(
    operation_store, tmp_path, monkeypatch, tool_name, over_limit
):
    raw = ("é" * 16).encode("utf-8")
    limit = len(raw) - int(over_limit)
    blob_id = str(uuid4())
    actual_inspect = blobs.inspect_attachment_original
    inspections = []

    def inspect(**kwargs):
        value = actual_inspect(**kwargs)
        inspections.append(value)
        return value

    monkeypatch.setattr(blobs, "inspect_attachment_original", inspect)

    def install(owner, running):
        _install_attachment(operation_store, owner.service, running, blob_id, raw, limit)

    async with _phase_case(
        operation_store,
        tmp_path,
        monkeypatch,
        tool_call=("original_attachment_call", tool_name, {"blob_id": blob_id}),
        before_batch_initialization=install,
    ) as (owner, _running, primary, sdk):
        original = primary.outcome.completion.tool_batch.calls[0]
        await _advance(owner)
        refused = owner.tool_refusal
        assert refused is not None
        assert refused.receipt.tool_call_id == original.id
        assert refused.receipt.response_digest == primary.response.reference.digest
        assert refused.receipt.reason == "retained_tail_unbounded"
        assert refused.receipt.tool_index == 0
        assert inspections == []
        assert len(sdk) == 1
    owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ("read_attachment", "get_blob_content"))
@pytest.mark.parametrize("over_limit", (False, True))
async def test_paid_semantic_read_has_the_same_configured_attachment_boundary(
    operation_store, tmp_path, monkeypatch, tool_name, over_limit
):
    raw = ("é" * 16).encode("utf-8")
    limit = len(raw) - int(over_limit)
    blob_id = str(uuid4())
    async with _semantic_case(operation_store, tmp_path, monkeypatch, timed_read=True) as case:
        runtime, running = case["runtime"], case["running"]
        service = runtime.materialization.service
        _install_attachment(operation_store, service, running, blob_id, raw, limit)
        physical = []

        async def completion(**kwargs):
            physical.append(kwargs)
            answer = _read_answer(case["request"])
            answer["prescribed_action"].update(tool_name=tool_name, arguments_json=canonical_json({"blob_id": blob_id}))
            return _reply(canonical_json(answer))

        monkeypatch.setattr(litellm, "acompletion", completion)
        prepared, retained = await _run_paid_semantic(case, monkeypatch)
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            installed = install_semantic_action_on_connection(
                conn, running, selected=retained.snapshot, effect_key=prepared.binding.effect_key
            )
            selected = prepare_semantic_read_on_connection(conn, running, selected=installed, effect_key=prepared.binding.effect_key)
        deadline = case["sql_deadline"]
        assert deadline is not None
        deadline.begin_action(deadline.policy.actions.read, started_at=time.monotonic())
        actual = await execute_semantic_advisor_read(
            service=service,
            authority=runtime.materialization.authority,
            running=running,
            selected=selected,
            effect_key=prepared.binding.effect_key,
            runtime=runtime,
            required_work=case["work"],
        )
        assert actual.result.success is (not over_limit)
        original = actual.committed.original_result
        assert original.prescribing_provider_call_id is not None
        if over_limit:
            assert original.attachment_inspections == ()
            assert actual.result.validation.errors[0].message == (
                "The complete attachment was refused by the current inspection policy; no content was returned."
            )
        else:
            assert len(original.attachment_inspections) == 1
            inspection = original.attachment_inspections[0]
            assert inspection.content_hash == sha256(raw).hexdigest() and inspection.size_bytes == len(raw)
            assert inspection.policy_fingerprint == AttachmentInspectionPolicy(limit, env_ref_names=runtime.env_ref_names).fingerprint
        assert len(physical) == 1
        case["work"].coordinator.assert_completed()
