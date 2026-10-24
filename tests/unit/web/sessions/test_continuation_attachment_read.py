"""Actual current inventory, SQL custody, original-byte inspection and discovery."""

import asyncio
import io
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import insert, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_attachment_read as reader
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.redaction import READ_ATTACHMENT_MAX_UTF8_BYTES
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import blobs
from elspeth.web.config import WebSettings
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, WebPluginPolicy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.continuation_owner_results import decode_read_owner_result, re_admit_read_owner_result
from elspeth.web.sessions.models import blobs_table
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_selection import selected_case

operation_store = test_composer_operation_authority.operation_store


async def read_case(store, raw=b"Complete customer prose", *, secret_service=None):
    engine, _repository, authority, sessions, sid = store
    running, stored, _chunk = selected_case(store)
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=inventory_advisor(sessions, secret_service=secret_service),
        authority=authority,
        selected=selected,
        required_work=work,
    )
    settings = WebSettings(
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=100,
        shareable_link_signing_key=b"\x00" * 32,
        data_dir=sessions._data_dir,
    )
    catalog = create_catalog_service()
    policy = WebPluginPolicy.create(
        required=frozenset(), configured_optional=frozenset(), preferences=(), control_modes=(), plugin_code_identities=()
    )
    profiles = OperatorProfileRegistry(policy=policy, settings=RuntimeWebPluginConfig.from_settings(settings))
    actors = []

    def current_snapshot(actor):
        actors.append(actor)
        return PluginAvailabilitySnapshot.create(
            policy_hash=policy.policy_hash,
            principal_scope=f"local:{actor}",
            available=frozenset(),
            unavailable=(),
            selected=(),
            usable_profile_aliases=(),
            selected_profile_aliases=(),
            binding_generation_fingerprint="attachment-current-test",
        )

    service = ComposerServiceImpl(
        catalog,
        settings,
        sessions_service=sessions,
        session_engine=engine,
        plugin_snapshot_factory=current_snapshot,
        operator_profile_registry=profiles,
    )
    blob_id = str(uuid4())
    path = sessions._data_dir / "blobs" / str(sid) / f"{blob_id}_notes.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with engine.begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id=blob_id,
                session_id=str(sid),
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
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    kwargs = {
        "service": service,
        "authority": authority,
        "selected": selected,
        "inventory": inventory,
        "tool_name": "read_attachment",
        "arguments": {"blob_id": blob_id},
        "state": state,
        "required_work": work,
    }
    return kwargs, path, actors


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ("read_attachment", "get_blob_content"))
async def test_current_read_captures_actual_locked_original_and_preserves_result_alias(operation_store, monkeypatch, tool_name):
    raw = ("é customer field " + "x" * 3000).encode()
    kwargs, _path, actors = await read_case(operation_store, raw)
    kwargs["tool_name"] = tool_name
    if tool_name == "get_blob_content":
        kwargs["arguments"] = {**kwargs["arguments"], "offset_chars": 1, "expected_content_hash": sha256(raw).hexdigest()}
    actual_inspect = blobs.inspect_attachment_original
    observations = []

    def observe(**kw):
        value = actual_inspect(**kw)
        observations.append((kw["raw"], value))
        return value

    monkeypatch.setattr(blobs, "inspect_attachment_original", observe)
    actual = await reader.read_current_attachment(**kwargs)
    assert actual.result.success and actual.result.updated_state is kwargs["state"]
    assert actors == ["alice"]
    assert len(observations) == 1 and observations[0][0] == raw
    inspection = actual.retained.attachment_inspections[0]
    assert inspection.content_hash == sha256(raw).hexdigest() and inspection.size_bytes == len(raw)
    assert inspection.session_id == str(operation_store[4])
    assert inspection.policy_fingerprint == observations[0][1].policy_fingerprint
    assert len(actual.retained.blob_versions) == 1
    restored = decode_read_owner_result(canonical_json(actual.retained.model_dump(mode="json")))
    assert restored == actual.retained
    assert re_admit_read_owner_result(restored)["success"] is True
    repeated = await reader.read_current_attachment(**kwargs)
    assert repeated.result.success and len(observations) == 2
    assert repeated.retained.attachment_inspections == actual.retained.attachment_inspections
    assert actors == ["alice", "alice"]
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw", (b"prefix " + b"x" * 70000 + b" https://name:private-value@example.test/", b'{"api_key":"opaque-private-value"}', b"\xff")
)
async def test_original_tail_structured_secret_and_utf8_refuse_without_receipt(operation_store, raw):
    kwargs, _path, _actors = await read_case(operation_store, raw)
    actual = await reader.read_current_attachment(**kwargs)
    assert not actual.result.success and actual.result.updated_state is kwargs["state"]
    assert actual.retained.attachment_inspections == ()
    assert len(actual.retained.blob_versions) == 1
    assert re_admit_read_owner_result(actual.retained)["success"] is False
    kwargs["required_work"].coordinator.assert_completed()


class _CountingReader(io.BufferedReader):
    """Records every byte count a read takes from the spied blob file."""

    def __init__(self, raw, reads):
        super().__init__(raw)
        self._reads = reads

    def read(self, size=-1):
        data = super().read(size)
        self._reads.append(len(data))
        return data


def _spy_blob_reads(monkeypatch, path):
    """Count bytes read from ``path`` through ``Path.open``, which ``read_bytes`` also uses."""
    reads = []
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        if self != path:
            return real_open(self, *args, **kwargs)
        return _CountingReader(io.FileIO(self, "r"), reads)

    monkeypatch.setattr(Path, "open", counting_open)
    return reads


@pytest.mark.asyncio
async def test_in_range_attachment_reads_its_complete_recorded_original(operation_store, monkeypatch):
    raw = b"x" * (READ_ATTACHMENT_MAX_UTF8_BYTES // 2)
    kwargs, path, _actors = await read_case(operation_store, raw)
    reads = _spy_blob_reads(monkeypatch, path)
    actual = await reader.read_current_attachment(**kwargs)
    assert actual.result.success
    assert sum(reads) == len(raw)
    assert actual.retained.attachment_inspections[0].size_bytes == len(raw)
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_oversized_recorded_attachment_refuses_before_any_file_read(operation_store, monkeypatch):
    raw = b"x" * (READ_ATTACHMENT_MAX_UTF8_BYTES + 1)
    kwargs, path, _actors = await read_case(operation_store, raw)
    reads = _spy_blob_reads(monkeypatch, path)
    actual = await reader.read_current_attachment(**kwargs)
    assert not actual.result.success
    assert actual.result.validation.errors[0].message == (
        "The complete attachment was refused by the current inspection policy; no content was returned."
    )
    assert reads == []
    assert actual.retained.attachment_inspections == ()
    assert len(actual.retained.blob_versions) == 1
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_disk_bytes_beyond_in_range_recorded_size_are_an_integrity_failure(operation_store, monkeypatch):
    limit = READ_ATTACHMENT_MAX_UTF8_BYTES
    raw = b"x" * (limit * 2)
    kwargs, path, _actors = await read_case(operation_store, raw)
    engine = operation_store[0]
    with engine.begin() as conn:
        conn.execute(
            update(blobs_table)
            .where(blobs_table.c.id == kwargs["arguments"]["blob_id"])
            .values(size_bytes=limit, content_hash=sha256(raw[:limit]).hexdigest())
        )
    reads = _spy_blob_reads(monkeypatch, path)
    with pytest.raises(AuditIntegrityError, match="recorded size"):
        await reader.read_current_attachment(**kwargs)
    assert sum(reads) <= limit + 1
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("receipt", "version", "call", "session"))
async def test_complete_read_history_refuses_independent_binding_changes_after_positive(operation_store, change):
    kwargs, _path, _actors = await read_case(operation_store)
    actual = await reader.read_current_attachment(**kwargs)
    retained = actual.retained
    assert re_admit_read_owner_result(retained)["success"] is True
    if change == "receipt":
        changed = retained.model_copy(update={"attachment_inspections": ()})
    elif change == "version":
        changed = retained.model_copy(update={"blob_versions": (retained.blob_versions[0].model_copy(update={"size_bytes": 999}),)})
    elif change == "call":
        changed = retained.model_copy(update={"arguments_json": canonical_json({"blob_id": str(uuid4())})})
    else:
        from elspeth.web.sessions.continuation_owner_results import require_read_versions_on_connection

        with operation_store[0].connect() as conn, pytest.raises(AuditIntegrityError, match="another session"):
            require_read_versions_on_connection(conn, session_id=str(uuid4()), result=retained)
        return
    with pytest.raises(AuditIntegrityError):
        re_admit_read_owner_result(changed)


@pytest.mark.asyncio
async def test_actual_selection_recheck_refuses_cancel_after_successful_read(operation_store, monkeypatch):
    kwargs, _path, _actors = await read_case(operation_store)
    positive = await reader.read_current_attachment(**kwargs)
    assert positive.result.success
    original = reader.execute_discovery_tool_with_context

    def cancel_after(*args):
        value = original(*args)
        assert value.success
        with operation_store[0].begin() as conn:
            conn.execute(update(jobs).values(cancel_requested_at=database_now(conn)))
        return value

    monkeypatch.setattr(reader, "execute_discovery_tool_with_context", cancel_after)
    with pytest.raises(ComposerOperationCancelledDuringTurn):
        await reader.read_current_attachment(**kwargs)
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_valid_same_checkpoint_changed_root_refuses_before_file_read(operation_store, monkeypatch):
    kwargs, _path, _actors = await read_case(operation_store)
    assert (await reader.read_current_attachment(**kwargs)).result.success
    selected = kwargs["selected"]
    logical = selected.stack.logical.model_copy(update={"request_hash": "f" * 64})
    kwargs["selected"] = replace(selected, stack=selected.stack.model_copy(update={"logical": logical}))
    seen = []
    original = blobs._locked_read_ready_blob

    def observed(*args, **kw):
        seen.append(True)
        return original(*args, **kw)

    monkeypatch.setattr(blobs, "_locked_read_ready_blob", observed)
    with pytest.raises(AuditIntegrityError, match="complete selected root"):
        await reader.read_current_attachment(**kwargs)
    assert seen == []
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_started_read_cancellation_joins_exact_original_before_delivery(operation_store, monkeypatch):
    kwargs, _path, _actors = await read_case(operation_store)
    entered, release = Event(), Event()
    original = reader._execute_current_attachment_read
    completed = []

    def held(**kw):
        entered.set()
        assert release.wait(5)
        value = original(**kw)
        completed.append(value)
        return value

    monkeypatch.setattr(reader, "_execute_current_attachment_read", held)
    task = asyncio.create_task(reader.read_current_attachment(**kwargs))
    try:
        await test_composer_operation_authority._wait_for_thread_event(entered)
        task.cancel("attachment read cancellation")
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError, match="attachment read cancellation"):
        await task
    assert len(completed) == 1 and completed[0].result.success
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_admission_wait_keeps_single_original_cancellation_without_read(operation_store, monkeypatch):
    from elspeth.web import async_workers

    kwargs, _path, _actors = await read_case(operation_store)
    assert (await reader.read_current_attachment(**kwargs)).result.success
    kwargs["required_work"] = inventory_work(kwargs["inventory"].running)
    entered = asyncio.Event()
    originals = []
    calls = []
    admission = async_workers._acquire_admission
    execute = reader._execute_current_attachment_read

    async def observe_wait():
        entered.set()
        try:
            await admission()
        except asyncio.CancelledError as error:
            originals.append(error)
            raise

    def observe_read(**kw):
        calls.append(True)
        return execute(**kw)

    monkeypatch.setattr(async_workers, "ADMISSION_CAPACITY", 0)
    monkeypatch.setattr(async_workers, "ADMISSION_WAIT_SECONDS", 30.0)
    monkeypatch.setattr(async_workers, "_acquire_admission", observe_wait)
    monkeypatch.setattr(reader, "_execute_current_attachment_read", observe_read)
    task = asyncio.create_task(reader.read_current_attachment(**kwargs))
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        task.cancel("attachment admission cancellation")
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert len(originals) == 1 and caught.value is originals[0]
    assert calls == []
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_reader_integrity_failure_retains_exact_exception_and_settles_projection(operation_store, monkeypatch):
    kwargs, _path, _actors = await read_case(operation_store)
    assert (await reader.read_current_attachment(**kwargs)).result.success
    failure = AuditIntegrityError("original controlled attachment integrity failure")

    def fail(*args, **kw):
        raise failure

    monkeypatch.setattr(blobs, "_verify_blob_content_integrity", fail)
    with pytest.raises(AuditIntegrityError) as caught:
        await reader.read_current_attachment(**kwargs)
    assert caught.value is failure
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", (False, True))
async def test_policy_uses_actual_names_inventory_without_resolving_values(operation_store, monkeypatch, configured):
    from elspeth.web.secrets.server_store import ServerSecretStore
    from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
    from elspeth.web.secrets.user_store import UserSecretStore

    resolver = None
    if configured:
        monkeypatch.setenv("ATTACHMENT_TEST_REF", "offline-test-value")
        resolver = ScopedSecretResolver(
            WebSecretService(
                user_store=UserSecretStore(engine=operation_store[0], master_key="inventory-test-master-key-32chars!"),
                server_store=ServerSecretStore(allowlist=("ATTACHMENT_TEST_REF",)),
            ),
            "local",
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("attachment inventory resolved a secret value")

        monkeypatch.setattr(resolver, "resolve", forbidden)
        monkeypatch.setattr(resolver, "resolve_scoped", forbidden)
    raw = b'{"api_key":"${ATTACHMENT_TEST_REF}"}'
    kwargs, _path, _actors = await read_case(operation_store, raw, secret_service=resolver)
    actual = await reader.read_current_attachment(**kwargs)
    assert actual.result.success is configured
    assert bool(actual.retained.attachment_inspections) is configured
    assert kwargs["inventory"].names == (frozenset({"ATTACHMENT_TEST_REF"}) if configured else frozenset())
    kwargs["required_work"].coordinator.assert_completed()
