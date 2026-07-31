"""Composer tool coverage for widened blob_ref inline-content authoring."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.blobs import BlobIntegrityError
from elspeth.contracts.blobs_inline import is_widened_blob_ref
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.protocol import CatalogService, PluginKind
from elspeth.web.catalog.schemas import PluginSchemaInfo, PluginSummary
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.composer.tools import ToolResult, get_tool_definitions
from elspeth.web.composer.tools import execute_tool as _execute_tool
from elspeth.web.composer.yaml_generator import generate_pipeline_dict
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.provider_config_policy import AWS_S3_ENDPOINT_URL_POLICY_ERROR
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blob_replacement_cleanups_table, blobs_table, chat_messages_table
from elspeth.web.sessions.schema import initialize_session_schema


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _inline_ref_state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": "/tmp/input.csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="classify",
                node_type="transform",
                plugin="llm",
                input="rows",
                on_success="classified",
                on_error="discard",
                options={
                    "provider": "openrouter",
                    "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                    "model": "openai/gpt-4o",
                    "prompt_template": "Placeholder",
                    "required_input_fields": [],
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="classified",
                plugin="json",
                options={"path": "/tmp/output.jsonl", "format": "jsonl", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _named_sources_state() -> CompositionState:
    return CompositionState(
        sources={
            "orders": SourceSpec(
                plugin="csv",
                on_success="orders_rows",
                options={"path": "/tmp/orders.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            "refunds": SourceSpec(
                plugin="json",
                on_success="refund_rows",
                options={"path": "/tmp/refunds.jsonl", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _aws_s3_endpoint_state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="aws_s3",
            on_success="main",
            options={},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="main",
                plugin="aws_s3",
                options={},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=5,
    )


class _Catalog:
    def list_sources(self) -> list[PluginSummary]:
        return [
            PluginSummary(name="csv", description="CSV source", plugin_type="source", config_fields=[]),
            PluginSummary(name="json", description="JSON source", plugin_type="source", config_fields=[]),
            PluginSummary(name="text", description="Text source", plugin_type="source", config_fields=[]),
        ]

    def list_sinks(self) -> list[PluginSummary]:
        return [PluginSummary(name="json", description="JSON sink", plugin_type="sink", config_fields=[])]

    def list_transforms(self) -> list[PluginSummary]:
        return [PluginSummary(name="llm", description="LLM transform", plugin_type="transform", config_fields=[])]

    def get_schema(self, plugin_type: PluginKind, plugin_name: str) -> PluginSchemaInfo:
        return PluginSchemaInfo(
            name=plugin_name,
            plugin_type=plugin_type,
            description=f"{plugin_name} {plugin_type}",
            json_schema={"title": plugin_name, "properties": {"path": {"type": "string"}}},
            knob_schema={"fields": []},
        )

    def post_call_hints(
        self,
        *,
        plugin_type: PluginKind,
        plugin_name: str,
        tool_name: str,
        config_snapshot: Mapping[str, object],
    ) -> tuple[str, ...]:
        return ()


def _catalog() -> CatalogService:
    return cast(CatalogService, _Catalog())


def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    state: CompositionState,
    catalog: CatalogService,
    **kwargs: Any,
) -> ToolResult:
    """Invoke the strict dispatcher with an explicit trained-operator snapshot."""
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return _execute_tool(
        tool_name,
        arguments,
        state,
        PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
        **kwargs,
    )


@pytest.fixture()
def blob_env(tmp_path: Path) -> dict[str, Any]:
    engine = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(engine)
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = authority.create_session_with_initial_fence(
        user_id="test-user",
        title="Inline Blob Test",
        auth_provider_type="local",
        owner_instance_id="composer-blob-test",
        lease_seconds=30,
    )
    session_id = str(session.id)
    operation_context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="composer-blob-test",
        lease_seconds=30,
    )
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            chat_messages_table.insert().values(
                id="user-message-1",
                session_id=session_id,
                role="user",
                content="Use this exact content.",
                raw_content=None,
                tool_calls=None,
                tool_call_id=None,
                sequence_no=1,
                writer_principal="route_user_message",
                created_at=now,
                composition_state_id=None,
                parent_assistant_id=None,
            )
        )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "blobs").mkdir()
    return {
        "engine": engine,
        "session_id": session_id,
        "data_dir": str(data_dir),
        "session_operation_authority": authority,
        "session_operation_context": operation_context,
    }


def _create_blob(
    blob_env: dict[str, Any],
    *,
    filename: str = "prompt.txt",
    mime_type: str = "text/plain",
    content: str = "System prompt",
    llm_authored: bool = False,
) -> ToolResult:
    provenance_kwargs = (
        {
            "composer_model_identifier": "test-model",
            "composer_model_version": "test-model-v1",
            "composer_provider": "test-provider",
            "composer_skill_hash": "a" * 64,
            "tool_arguments_hash": "b" * 64,
        }
        if llm_authored
        else {}
    )
    return execute_tool(
        "create_blob",
        {"filename": filename, "mime_type": mime_type, "content": content},
        _empty_state(),
        _catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_authority=blob_env["session_operation_authority"],
        session_operation_context=blob_env["session_operation_context"],
        user_message_id="user-message-1",
        user_message_content="Generate a source for me." if llm_authored else f"Use this exact content:\n{content}",
        **provenance_kwargs,
    )


def _mark_blob_pending(blob_env: dict[str, Any], blob_id: str) -> None:
    with blob_env["engine"].begin() as conn:
        conn.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(status="pending"))


def _crash_blob_replacement_staging_write(
    data_dir: str,
    context: Any,
    plan: Any,
    content: bytes,
    crash_cut: str,
) -> None:
    from elspeth.web.blobs import service as service_module

    class _CompareAndSwapOnlyAuthority:
        @staticmethod
        def compare_and_swap(_context: Any) -> None:
            return None

    staging = Path(plan.staging_path)
    if crash_cut == "partial_temp":
        real_fdopen = service_module.os.fdopen

        class _PartialWriter:
            def __init__(self, descriptor: int, mode: str) -> None:
                self._file = real_fdopen(descriptor, mode)

            def __enter__(self) -> _PartialWriter:
                return self

            def __exit__(self, *_args: Any) -> None:
                self._file.close()

            def write(self, value: bytes) -> None:
                self._file.write(value[: max(1, len(value) // 2)])
                self._file.flush()
                os.fsync(self._file.fileno())
                os._exit(91)

            def flush(self) -> None:
                self._file.flush()

            def fileno(self) -> int:
                return self._file.fileno()

        service_module.os.fdopen = _PartialWriter
    elif crash_cut == "prepublish_temp":
        real_replace = service_module.os.replace

        def crash_before_stage_publish(src: Any, dst: Any) -> None:
            if Path(dst) == staging:
                os._exit(92)
            real_replace(src, dst)

        service_module.os.replace = crash_before_stage_publish
    else:  # pragma: no cover - caller owns the closed crash-cut vocabulary
        raise AssertionError(f"unknown crash cut: {crash_cut}")

    service_module._BlobReplacementCoordinator(
        data_dir=Path(data_dir),
        session_operation_authority=_CompareAndSwapOnlyAuthority(),
    )._write_staging_file(context=context, plan=plan, content=content)


def test_create_blob_fails_closed_without_paired_session_operation_authority(blob_env: dict[str, Any]) -> None:
    result = execute_tool(
        "create_blob",
        {"filename": "unauthorized.txt", "mime_type": "text/plain", "content": "must not persist"},
        _empty_state(),
        _catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        user_message_id="user-message-1",
        user_message_content="must not persist",
    )

    assert result.success is False
    assert "session operation authority" in str(result.data or result.validation.errors).lower()
    with blob_env["engine"].connect() as conn:
        assert conn.execute(blobs_table.select()).all() == []


@pytest.mark.parametrize("tool_name", ["update_blob", "delete_blob"])
def test_blob_replacement_and_deletion_reject_stale_compose_context_before_filesystem_mutation(
    blob_env: dict[str, Any],
    tool_name: str,
) -> None:
    created = _create_blob(blob_env, filename="authority.txt", content="original")
    assert created.success is True
    blob_id = created.data["blob_id"]
    with blob_env["engine"].connect() as conn:
        row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
    storage = Path(row.storage_path)
    before = storage.read_bytes()

    authority = blob_env["session_operation_authority"]
    stale = blob_env["session_operation_context"]
    authority.release(stale)
    authority.acquire(
        session_id=UUID(blob_env["session_id"]),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="composer-blob-successor",
        lease_seconds=30,
    )
    arguments = {"blob_id": blob_id, **({"content": "replacement"} if tool_name == "update_blob" else {})}

    result = execute_tool(
        tool_name,
        arguments,
        created.updated_state,
        _catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_authority=authority,
        session_operation_context=stale,
        user_message_id="user-message-1",
        user_message_content="replacement",
    )

    assert result.success is False
    assert storage.read_bytes() == before
    with blob_env["engine"].connect() as conn:
        after = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
    assert after.size_bytes == row.size_bytes
    assert after.content_hash == row.content_hash


def test_update_blob_rejects_recorded_hash_drift_before_swap(blob_env: dict[str, Any]) -> None:
    created = _create_blob(blob_env, filename="drift.txt", content="original")
    blob_id = created.data["blob_id"]
    with blob_env["engine"].begin() as conn:
        row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
        conn.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(content_hash="c" * 64))
    storage = Path(row.storage_path)
    before = storage.read_bytes()
    before_inventory = tuple(sorted(path.name for path in storage.parent.iterdir()))

    with pytest.raises(BlobIntegrityError, match="content integrity failure"):
        execute_tool(
            "update_blob",
            {"blob_id": blob_id, "content": "replacement"},
            created.updated_state,
            _catalog(),
            data_dir=blob_env["data_dir"],
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
            session_operation_authority=blob_env["session_operation_authority"],
            session_operation_context=blob_env["session_operation_context"],
            user_message_id="user-message-1",
            user_message_content="replacement",
        )

    assert storage.read_bytes() == before
    assert tuple(sorted(path.name for path in storage.parent.iterdir())) == before_inventory
    with blob_env["engine"].connect() as conn:
        after = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
    assert after.size_bytes == row.size_bytes
    assert after.content_hash == "c" * 64


def test_update_blob_delegates_publish_to_durable_replacement_coordinator(
    blob_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.composer.tools import blobs as blob_tools_module

    coordinator_type = getattr(blob_tools_module, "_BlobReplacementCoordinator", None)
    assert coordinator_type is not None, "update_blob has no durable replacement coordinator"
    created = _create_blob(blob_env, filename="coordinated.txt", content="original")
    calls: list[dict[str, Any]] = []

    def replace_blob(_self: Any, **kwargs: Any):
        calls.append(kwargs)
        return kwargs["replacement"]

    monkeypatch.setattr(coordinator_type, "replace_blob", replace_blob)
    result = execute_tool(
        "update_blob",
        {"blob_id": created.data["blob_id"], "content": "replacement"},
        created.updated_state,
        _catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_authority=blob_env["session_operation_authority"],
        session_operation_context=blob_env["session_operation_context"],
        user_message_id="user-message-1",
        user_message_content="replacement",
    )

    assert result.success is True
    assert len(calls) == 1
    assert calls[0]["content"] == b"replacement"
    assert calls[0]["expected"].id == calls[0]["replacement"].id


def test_update_blob_commit_then_wrapper_raise_is_recovered_as_committed_by_successor(
    blob_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.blobs.service import _BlobReplacementCoordinator

    created = _create_blob(blob_env, filename="commit.txt", content="original")
    blob_id = created.data["blob_id"]
    with blob_env["engine"].connect() as conn:
        before_row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
    storage = Path(before_row.storage_path)

    authority = blob_env["session_operation_authority"]
    original_mutate = authority.mutate
    successor_contexts: list[Any] = []
    injected = False

    def commit_then_raise(context: Any, mutation: Any):
        nonlocal injected
        result = original_mutate(context, mutation)
        if mutation.__name__ == "commit_replacement" and not injected:
            injected = True
            authority.release(context)
            successor_contexts.append(
                authority.acquire(
                    session_id=UUID(blob_env["session_id"]),
                    operation_kind=SessionOperationKind.COMPOSE,
                    owner_instance_id="replacement-successor",
                    lease_seconds=30,
                )
            )
            raise RuntimeError("simulated wrapper failure after committed replacement")
        return result

    monkeypatch.setattr(authority, "mutate", commit_then_raise)
    with pytest.raises(RuntimeError, match="wrapper failure after committed replacement"):
        execute_tool(
            "update_blob",
            {"blob_id": blob_id, "content": "replacement"},
            created.updated_state,
            _catalog(),
            data_dir=blob_env["data_dir"],
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
            session_operation_authority=authority,
            session_operation_context=blob_env["session_operation_context"],
            user_message_id="user-message-1",
            user_message_content="replacement",
        )

    assert injected is True
    assert len(successor_contexts) == 1
    _BlobReplacementCoordinator(
        data_dir=Path(blob_env["data_dir"]),
        session_operation_authority=authority,
    ).reconcile(context=successor_contexts[0])

    assert storage.read_bytes() == b"replacement"
    assert tuple(sorted(path.name for path in storage.parent.iterdir())) == (storage.name,)
    with blob_env["engine"].connect() as conn:
        after_row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
        assert (
            conn.execute(blob_replacement_cleanups_table.select().where(blob_replacement_cleanups_table.c.blob_id == blob_id)).one_or_none()
            is None
        )
    assert after_row.size_bytes == len(b"replacement")
    assert after_row.content_hash == hashlib.sha256(b"replacement").hexdigest()


def test_update_blob_definite_precommit_failure_restores_old_and_aborts(
    blob_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.coordination import repository as repository_module

    created = _create_blob(blob_env, filename="precommit.txt", content="original")
    blob_id = created.data["blob_id"]
    with blob_env["engine"].connect() as conn:
        before_row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
    storage = Path(before_row.storage_path)

    def fail_before_commit(*_args: Any, **_kwargs: Any):
        raise RuntimeError("definite precommit failure")

    monkeypatch.setattr(repository_module._RepositoryBlobMutations, "commit_blob_replacement", fail_before_commit)
    with pytest.raises(RuntimeError, match="definite precommit failure"):
        execute_tool(
            "update_blob",
            {"blob_id": blob_id, "content": "replacement"},
            created.updated_state,
            _catalog(),
            data_dir=blob_env["data_dir"],
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
            session_operation_authority=blob_env["session_operation_authority"],
            session_operation_context=blob_env["session_operation_context"],
            user_message_id="user-message-1",
            user_message_content="replacement",
        )

    assert storage.read_bytes() == b"original"
    assert tuple(sorted(path.name for path in storage.parent.iterdir())) == (storage.name,)
    with blob_env["engine"].connect() as conn:
        after_row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
        assert (
            conn.execute(blob_replacement_cleanups_table.select().where(blob_replacement_cleanups_table.c.blob_id == blob_id)).one_or_none()
            is None
        )
    assert after_row.size_bytes == before_row.size_bytes
    assert after_row.content_hash == before_row.content_hash


def test_update_blob_recovery_failure_preserves_primary_and_scrubs_note(
    blob_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.blobs import service as service_module
    from elspeth.web.coordination import repository as repository_module

    created = _create_blob(blob_env, filename="recovery-note.txt", content="original secret")
    blob_id = created.data["blob_id"]
    with blob_env["engine"].connect() as conn:
        before_row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
    storage = Path(before_row.storage_path)
    real_replace = service_module.os.replace

    def fail_before_commit(*_args: Any, **_kwargs: Any):
        raise RuntimeError("primary commit failure")

    def fail_backup_restore(src: Any, dst: Any) -> None:
        if Path(src).name.endswith(".backup") and Path(dst) == storage:
            raise OSError("CANARY-private-path-and-provider-detail")
        real_replace(src, dst)

    monkeypatch.setattr(repository_module._RepositoryBlobMutations, "commit_blob_replacement", fail_before_commit)
    monkeypatch.setattr(service_module.os, "replace", fail_backup_restore)
    with pytest.raises(RuntimeError, match="primary commit failure") as exc_info:
        execute_tool(
            "update_blob",
            {"blob_id": blob_id, "content": "replacement secret"},
            created.updated_state,
            _catalog(),
            data_dir=blob_env["data_dir"],
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
            session_operation_authority=blob_env["session_operation_authority"],
            session_operation_context=blob_env["session_operation_context"],
            user_message_id="user-message-1",
            user_message_content="replacement secret",
        )

    notes = tuple(getattr(exc_info.value, "__notes__", ()))
    assert notes == ("Replacement recovery failed: OSError. Durable replacement obligation remains.",)
    assert str(storage) not in notes[0]
    assert "CANARY" not in notes[0]
    assert "secret" not in notes[0]
    with blob_env["engine"].connect() as conn:
        cleanup = conn.execute(blob_replacement_cleanups_table.select().where(blob_replacement_cleanups_table.c.blob_id == blob_id)).one()
    assert cleanup.phase == "swap_pending"
    assert storage.read_bytes() == b"replacement secret"
    assert Path(cleanup.backup_path).read_bytes() == b"original secret"


def test_update_blob_fsyncs_stage_both_renames_and_cleanup(
    blob_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.blobs import service as service_module

    created = _create_blob(blob_env, filename="fsync.txt", content="original")
    blob_id = created.data["blob_id"]
    with blob_env["engine"].connect() as conn:
        before_row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
    storage = Path(before_row.storage_path)
    real_fsync_parent = service_module._fsync_parent_directory
    fsynced_parents: list[Path] = []

    def record_fsync(parent: Path) -> None:
        fsynced_parents.append(parent)
        real_fsync_parent(parent)

    monkeypatch.setattr(service_module, "_fsync_parent_directory", record_fsync)
    result = execute_tool(
        "update_blob",
        {"blob_id": blob_id, "content": "replacement"},
        created.updated_state,
        _catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_authority=blob_env["session_operation_authority"],
        session_operation_context=blob_env["session_operation_context"],
        user_message_id="user-message-1",
        user_message_content="replacement",
    )

    assert result.success is True
    assert fsynced_parents == [storage.parent, storage.parent, storage.parent, storage.parent]


@pytest.mark.parametrize(
    ("crash_cut", "expected_exitcode"),
    [("partial_temp", 91), ("prepublish_temp", 92)],
)
def test_update_blob_restart_recovers_unpublished_staging_temp_without_residue(
    blob_env: dict[str, Any],
    crash_cut: str,
    expected_exitcode: int,
) -> None:
    from elspeth.web.blobs import service as service_module

    created = _create_blob(blob_env, filename=f"{crash_cut}.txt", content="original")
    blob_id = UUID(created.data["blob_id"])
    authority = blob_env["session_operation_authority"]
    stale = blob_env["session_operation_context"]
    expected = authority.mutate(stale, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id))
    replacement_content = b"replacement durable bytes"
    replacement = replace(
        expected,
        size_bytes=len(replacement_content),
        content_hash=hashlib.sha256(replacement_content).hexdigest(),
    )
    replacement_id = uuid4()
    storage = Path(expected.storage_path)
    operation_token = service_module._blob_operation_path_token(
        operation_id=stale.fence.operation_id,
        operation_epoch=stale.fence.operation_epoch,
        operation_kind=stale.operation_kind,
    )
    stem = f".{storage.name}.replace-{operation_token}-{replacement_id}"
    staging = storage.with_name(f"{stem}.stage")
    temporary = staging.with_name(f"{staging.name}.tmp")
    backup = storage.with_name(f"{stem}.backup")
    plan = authority.mutate(
        stale,
        lambda transaction: transaction.blobs.prepare_blob_replacement(
            replacement_id=replacement_id,
            expected=expected,
            replacement=replacement,
            staging_path=str(staging),
            backup_path=str(backup),
            max_storage_per_session=1024,
            accepting_proposal_id=None,
        ),
    )
    process_context = multiprocessing.get_context("spawn")
    process = process_context.Process(
        target=_crash_blob_replacement_staging_write,
        args=(blob_env["data_dir"], stale, plan, replacement_content, crash_cut),
    )

    process.start()
    process.join(timeout=15)
    assert process.exitcode == expected_exitcode
    assert staging.exists() is False
    assert temporary.exists() is True
    if crash_cut == "partial_temp":
        assert 0 < temporary.stat().st_size < len(replacement_content)
    else:
        assert temporary.read_bytes() == replacement_content

    authority.release(stale)
    successor = authority.acquire(
        session_id=UUID(blob_env["session_id"]),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=f"successor-{crash_cut}",
        lease_seconds=30,
    )
    service_module._BlobReplacementCoordinator(
        data_dir=Path(blob_env["data_dir"]),
        session_operation_authority=authority,
    ).reconcile(context=successor)

    assert storage.read_bytes() == b"original"
    assert tuple(sorted(path.name for path in storage.parent.iterdir())) == (storage.name,)
    with blob_env["engine"].connect() as conn:
        assert (
            conn.execute(
                blob_replacement_cleanups_table.select().where(blob_replacement_cleanups_table.c.blob_id == str(blob_id))
            ).one_or_none()
            is None
        )


def test_blob_service_reconciles_replacements_before_deletions(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.blobs import service as service_module

    calls: list[str] = []

    def reconcile_replacements(_self: Any, _context: Any) -> None:
        calls.append("replacement")

    def reconcile_deletions(_self: Any, _context: Any) -> None:
        calls.append("deletion")

    monkeypatch.setattr(
        service_module._BlobReplacementCoordinator,
        "_reconcile_blob_replacements_locked",
        reconcile_replacements,
    )
    monkeypatch.setattr(
        service_module.BlobServiceImpl,
        "_reconcile_blob_deletions_only_locked",
        reconcile_deletions,
    )
    service = object.__new__(service_module.BlobServiceImpl)
    service._data_dir = Path("/tmp/blob-reconcile-order")
    service._session_operation_authority = object()

    service._reconcile_blob_deletions_locked(object())

    assert calls == ["replacement", "deletion"]


@pytest.mark.parametrize(
    "crash_cut",
    ["intent", "staging_fsynced", "swap_pending", "old_renamed_to_backup", "new_renamed_to_canonical"],
)
def test_update_blob_successor_restores_old_at_every_precommit_restart_cut(
    blob_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    crash_cut: str,
) -> None:
    from elspeth.web.blobs import service as service_module

    created = _create_blob(blob_env, filename=f"{crash_cut}.txt", content="original")
    blob_id = created.data["blob_id"]
    with blob_env["engine"].connect() as conn:
        before_row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
    storage = Path(before_row.storage_path)
    authority = blob_env["session_operation_authority"]
    stale = blob_env["session_operation_context"]
    successor_contexts: list[Any] = []

    def crash_now() -> None:
        if successor_contexts:
            return
        authority.release(stale)
        successor_contexts.append(
            authority.acquire(
                session_id=UUID(blob_env["session_id"]),
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=f"successor-{crash_cut}",
                lease_seconds=30,
            )
        )
        raise RuntimeError(f"crash at {crash_cut}")

    coordinator_type = service_module._BlobReplacementCoordinator
    if crash_cut in {"intent", "staging_fsynced"}:
        original_write = coordinator_type._write_staging_file

        def crash_during_staging(self: Any, **kwargs: Any) -> None:
            if crash_cut == "staging_fsynced":
                original_write(self, **kwargs)
            crash_now()

        monkeypatch.setattr(coordinator_type, "_write_staging_file", crash_during_staging)
    elif crash_cut == "swap_pending":

        def crash_before_publish(_self: Any, **_kwargs: Any) -> None:
            crash_now()

        monkeypatch.setattr(coordinator_type, "_publish_new_bytes", crash_before_publish)
    else:
        real_replace = service_module.os.replace

        def crash_during_publish(src: Any, dst: Any) -> None:
            real_replace(src, dst)
            src_path = Path(src)
            dst_path = Path(dst)
            if crash_cut == "old_renamed_to_backup" and src_path == storage and dst_path.name.endswith(".backup"):
                crash_now()
            if crash_cut == "new_renamed_to_canonical" and dst_path == storage and src_path.name.endswith(".stage"):
                crash_now()

        monkeypatch.setattr(service_module.os, "replace", crash_during_publish)

    with pytest.raises(RuntimeError, match=f"crash at {crash_cut}"):
        execute_tool(
            "update_blob",
            {"blob_id": blob_id, "content": "replacement"},
            created.updated_state,
            _catalog(),
            data_dir=blob_env["data_dir"],
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
            session_operation_authority=authority,
            session_operation_context=stale,
            user_message_id="user-message-1",
            user_message_content="replacement",
        )

    assert len(successor_contexts) == 1
    coordinator_type(
        data_dir=Path(blob_env["data_dir"]),
        session_operation_authority=authority,
    ).reconcile(context=successor_contexts[0])
    assert storage.read_bytes() == b"original"
    assert tuple(sorted(path.name for path in storage.parent.iterdir())) == (storage.name,)
    with blob_env["engine"].connect() as conn:
        after_row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
        assert (
            conn.execute(blob_replacement_cleanups_table.select().where(blob_replacement_cleanups_table.c.blob_id == blob_id)).one_or_none()
            is None
        )
    assert after_row.size_bytes == before_row.size_bytes
    assert after_row.content_hash == before_row.content_hash


@pytest.mark.parametrize("crash_cut", ["cleanup_dir_fsynced", "ledger_retired"])
def test_update_blob_successor_finishes_new_at_postcommit_restart_cuts(
    blob_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    crash_cut: str,
) -> None:
    from elspeth.web.blobs.service import _BlobReplacementCoordinator

    created = _create_blob(blob_env, filename=f"{crash_cut}.txt", content="original")
    blob_id = created.data["blob_id"]
    with blob_env["engine"].connect() as conn:
        before_row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
    storage = Path(before_row.storage_path)
    authority = blob_env["session_operation_authority"]
    stale = blob_env["session_operation_context"]
    original_mutate = authority.mutate
    successor_contexts: list[Any] = []
    injected = False

    def takeover_and_raise() -> None:
        nonlocal injected
        injected = True
        authority.release(stale)
        successor_contexts.append(
            authority.acquire(
                session_id=UUID(blob_env["session_id"]),
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=f"successor-{crash_cut}",
                lease_seconds=30,
            )
        )
        raise RuntimeError(f"crash at {crash_cut}")

    def crash_around_retirement(context: Any, mutation: Any):
        is_retirement = "retire_blob_replacement" in mutation.__code__.co_names
        if is_retirement and crash_cut == "cleanup_dir_fsynced" and not injected:
            takeover_and_raise()
        result = original_mutate(context, mutation)
        if is_retirement and crash_cut == "ledger_retired" and not injected:
            takeover_and_raise()
        return result

    monkeypatch.setattr(authority, "mutate", crash_around_retirement)
    with pytest.raises(RuntimeError, match=f"crash at {crash_cut}"):
        execute_tool(
            "update_blob",
            {"blob_id": blob_id, "content": "replacement"},
            created.updated_state,
            _catalog(),
            data_dir=blob_env["data_dir"],
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
            session_operation_authority=authority,
            session_operation_context=stale,
            user_message_id="user-message-1",
            user_message_content="replacement",
        )

    assert injected is True
    _BlobReplacementCoordinator(
        data_dir=Path(blob_env["data_dir"]),
        session_operation_authority=authority,
    ).reconcile(context=successor_contexts[0])
    assert storage.read_bytes() == b"replacement"
    assert tuple(sorted(path.name for path in storage.parent.iterdir())) == (storage.name,)
    with blob_env["engine"].connect() as conn:
        after_row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
        assert (
            conn.execute(blob_replacement_cleanups_table.select().where(blob_replacement_cleanups_table.c.blob_id == blob_id)).one_or_none()
            is None
        )
    assert after_row.size_bytes == len(b"replacement")
    assert after_row.content_hash == hashlib.sha256(b"replacement").hexdigest()


class TestListComposerBlobs:
    def test_returns_h4_visibility_shape_without_free_text_or_content(self, blob_env: dict[str, Any]) -> None:
        create = _create_blob(blob_env, filename="prompt.txt", content="Do not leak this prompt.")
        assert create.success is True

        result = execute_tool(
            "list_composer_blobs",
            {},
            _empty_state(),
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is True
        assert list(result.data) == ["blobs"]
        descriptor = result.data["blobs"][0]
        assert set(descriptor) == {"blob_id", "mime_type", "size_bytes", "content_hash", "filename"}
        assert descriptor["blob_id"] == create.data["blob_id"]
        assert descriptor["content_hash"] == create.data["content_hash"]
        assert "source_description" not in descriptor
        assert "content" not in descriptor
        assert "preview" not in descriptor

    def test_only_ready_blobs_are_returned(self, blob_env: dict[str, Any]) -> None:
        ready = _create_blob(blob_env, filename="ready.txt", content="ready")
        pending = _create_blob(blob_env, filename="pending.txt", content="pending")
        _mark_blob_pending(blob_env, pending.data["blob_id"])

        result = execute_tool(
            "list_composer_blobs",
            {},
            _empty_state(),
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is True
        assert [blob["blob_id"] for blob in result.data["blobs"]] == [ready.data["blob_id"]]


class TestWireBlobInlineRef:
    def test_candidate_runs_canonical_review_invariant_before_publication(
        self,
        blob_env: dict[str, Any],
    ) -> None:
        blob = _create_blob(blob_env, content="replacement prompt")
        base = _inline_ref_state()
        node = base.nodes[0]
        state = replace(
            base,
            nodes=(
                replace(
                    node,
                    options={
                        **node.options,
                        INTERPRETATION_REQUIREMENTS_KEY: [
                            {
                                "id": "duplicate",
                                "kind": "vague_term",
                                "user_term": "alpha",
                                "draft": "first",
                                "status": "pending",
                                "event_id": None,
                                "accepted_value": None,
                                "accepted_artifact_hash": None,
                                "resolved_prompt_template_hash": None,
                            },
                            {
                                "id": "duplicate",
                                "kind": "pipeline_decision",
                                "user_term": "beta",
                                "draft": "sk-sensitive-wire-review",
                                "status": "pending",
                                "event_id": None,
                                "accepted_value": None,
                                "accepted_artifact_hash": None,
                                "resolved_prompt_template_hash": None,
                            },
                        ],
                    },
                ),
            ),
        )

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "node:classify.options.prompt_template",
                "blob_id": blob.data["blob_id"],
            },
            state,
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.data["error_code"] == "interpretation_requirements_invalid"
        assert "sk-sensitive-wire-review" not in result.data["error"]

    @pytest.mark.parametrize("field_path", ["source.options.endpoint_url", "output:main.options.endpoint_url"])
    def test_aws_s3_endpoint_url_field_is_rejected_without_mutating_state(
        self,
        blob_env: dict[str, Any],
        field_path: str,
    ) -> None:
        blob = _create_blob(blob_env, content="inline endpoint canary")
        state = _aws_s3_endpoint_state()

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": field_path,
                "blob_id": blob.data["blob_id"],
            },
            state,
            _catalog(),
            data_dir=blob_env["data_dir"],
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.updated_state.version == 5
        assert result.data["error"] == AWS_S3_ENDPOINT_URL_POLICY_ERROR
        assert blob.data["blob_id"] not in result.data["error"]

    def test_authors_marker_with_authoritative_pinned_hash(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, content="Pinned prompt")
        state = _inline_ref_state()

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "node:classify.options.prompt_template",
                "blob_id": blob.data["blob_id"],
            },
            state,
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is True
        marker = result.updated_state.nodes[0].options["prompt_template"]
        assert marker == {
            "blob_ref": blob.data["blob_id"],
            "mode": "inline_content",
            "sha256": blob.data["content_hash"],
        }

    def test_writes_source_and_output_paths_by_identity(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, content="inline replacement")
        state = _inline_ref_state()

        source_result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "source.options.schema.description",
                "blob_id": blob.data["blob_id"],
                "encoding": "latin-1",
            },
            state,
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )
        output_result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "output:classified.options.header",
                "blob_id": blob.data["blob_id"],
            },
            source_result.updated_state,
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert source_result.success is True
        assert output_result.success is True
        assert "source" in source_result.updated_state.sources
        source_marker = source_result.updated_state.sources["source"].options["schema"]["description"]
        assert source_marker["encoding"] == "latin-1"
        assert output_result.updated_state.outputs[0].options["header"]["mode"] == "inline_content"

    def test_writes_named_source_paths_for_plural_source_yaml(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, content="orders prompt")

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "source:orders.options.schema.description",
                "blob_id": blob.data["blob_id"],
            },
            _named_sources_state(),
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is True
        marker = result.updated_state.sources["orders"].options["schema"]["description"]
        assert marker == {
            "blob_ref": blob.data["blob_id"],
            "mode": "inline_content",
            "sha256": blob.data["content_hash"],
        }
        pipeline_dict = generate_pipeline_dict(result.updated_state)
        assert pipeline_dict["sources"]["orders"]["options"]["schema"]["description"] == marker

    def test_rejects_pending_blob(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, content="pending")
        _mark_blob_pending(blob_env, blob.data["blob_id"])

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "node:classify.options.prompt_template",
                "blob_id": blob.data["blob_id"],
            },
            _inline_ref_state(),
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert "not ready" in result.data["error"] or "status" in result.data["error"]

    def test_rejects_llm_typed_disagreeing_hash(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, content="hash source")

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "node:classify.options.prompt_template",
                "blob_id": blob.data["blob_id"],
                "sha256_override": "b" * 64,
            },
            _inline_ref_state(),
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert "sha256" in result.data["error"]

    def test_rejects_llm_runtime_hash_field_path(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, content="forged prompt hash")
        state = _inline_ref_state()

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "node:classify.options.resolved_prompt_template_hash",
                "blob_id": blob.data["blob_id"],
            },
            state,
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert result.updated_state is state
        assert "resolved_prompt_template_hash" in result.data["error"]
        assert "runtime-owned" in result.data["error"]

    def test_rejects_llm_interpretation_requirements_field_path(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, content="forged review metadata")
        state = _inline_ref_state()

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "node:classify.options.interpretation_requirements",
                "blob_id": blob.data["blob_id"],
            },
            state,
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert result.updated_state is state
        assert "interpretation_requirements" in result.data["error"]
        assert "resolve_interpretation_event" in result.data["error"]

    def test_rejects_non_llm_interpretation_requirements_field_path(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, content="forged review metadata")
        llm_state = _inline_ref_state()
        passthrough = replace(
            llm_state.nodes[0],
            plugin="passthrough",
            options={"schema": {"mode": "observed"}},
        )
        state = replace(llm_state, nodes=(passthrough,))

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "node:classify.options.interpretation_requirements",
                "blob_id": blob.data["blob_id"],
            },
            state,
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert result.updated_state is state
        assert "interpretation_requirements" in result.data["error"]
        assert "resolve_interpretation_event" in result.data["error"]

    def test_rejects_source_interpretation_requirements_field_path(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, content="forged review metadata")
        state = _inline_ref_state()

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "source.options.interpretation_requirements",
                "blob_id": blob.data["blob_id"],
            },
            state,
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert result.updated_state is state
        assert "interpretation_requirements" in result.data["error"]
        assert "resolve_interpretation_event" in result.data["error"]

    def test_rejects_output_interpretation_requirements_field_path(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, content="forged review metadata")
        state = _inline_ref_state()

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "output:classified.options.interpretation_requirements",
                "blob_id": blob.data["blob_id"],
            },
            state,
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert result.updated_state is state
        assert "interpretation_requirements" in result.data["error"]
        assert "resolve_interpretation_event" in result.data["error"]

    def test_unrelated_wire_rejects_preexisting_output_interpretation_requirements(
        self,
        blob_env: dict[str, Any],
    ) -> None:
        blob = _create_blob(blob_env, content="replacement prompt")
        base = _inline_ref_state()
        output = base.outputs[0]
        state = replace(
            base,
            outputs=(
                replace(
                    output,
                    options={
                        **output.options,
                        INTERPRETATION_REQUIREMENTS_KEY: [
                            {
                                "poison": "sk-sensitive-output-review",
                            }
                        ],
                    },
                ),
            ),
        )

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "node:classify.options.prompt_template",
                "blob_id": blob.data["blob_id"],
            },
            state,
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.data["error_code"] == "interpretation_requirements_invalid"
        assert "sk-sensitive-output-review" not in result.data["error"]

    def test_rejects_invalid_field_path(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, content="prompt")

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "transforms[0].options.prompt_template",
                "blob_id": blob.data["blob_id"],
            },
            _inline_ref_state(),
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert "field_path" in result.data["error"]

    def test_rejects_unknown_encoding(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, content="prompt")

        result = execute_tool(
            "wire_blob_inline_ref",
            {
                "field_path": "node:classify.options.prompt_template",
                "blob_id": blob.data["blob_id"],
                "encoding": "ascii",
            },
            _inline_ref_state(),
            _catalog(),
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert "encoding" in result.data["error"]


class TestSetSourceFromBlobMode:
    def test_rebind_preserves_trusted_existing_source_requirement_id(
        self,
        blob_env: dict[str, Any],
    ) -> None:
        blob = _create_blob(
            blob_env,
            filename="input.csv",
            mime_type="text/csv",
            content="name\nAda",
        )
        trusted_id = "trusted-source-review-id"
        requirement = {
            "id": trusted_id,
            "kind": "invented_source",
            "user_term": "trusted_source_assumption",
            "draft": "Review the source assumption.",
            "status": "pending",
            "event_id": None,
            "accepted_value": None,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": None,
        }
        base = _inline_ref_state()
        source = base.sources["source"]
        state = base.with_named_source(
            "source",
            replace(
                source,
                options={
                    **source.options,
                    INTERPRETATION_REQUIREMENTS_KEY: [requirement],
                },
            ),
        )

        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": blob.data["blob_id"],
                "on_success": "rows",
                "options": {
                    "schema": {"mode": "observed"},
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "kind": requirement["kind"],
                            "user_term": requirement["user_term"],
                            "draft": requirement["draft"],
                        }
                    ],
                },
            },
            state,
            _catalog(),
            data_dir=blob_env["data_dir"],
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success, result.to_dict()
        retained = result.updated_state.sources["source"].options[INTERPRETATION_REQUIREMENTS_KEY]
        assert retained[0]["id"] == trusted_id

    def test_source_blob_stager_rejects_different_kind_projecting_same_source_review_id(
        self,
        blob_env: dict[str, Any],
    ) -> None:
        blob = _create_blob(
            blob_env,
            filename="input.csv",
            mime_type="text/csv",
            content="name\nAda",
            llm_authored=True,
        )
        state = _empty_state()

        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": blob.data["blob_id"],
                "on_success": "rows",
                "options": {
                    "schema": {"mode": "observed"},
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "kind": "pipeline_decision",
                            "user_term": "inline_source_data",
                            "draft": "sk-sensitive-source-review",
                        }
                    ],
                },
            },
            state,
            _catalog(),
            data_dir=blob_env["data_dir"],
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is False
        assert result.updated_state is state
        assert "interpretation_requirements_invalid" in result.data["error"]
        assert "sk-sensitive-source-review" not in result.data["error"]

    def test_set_source_from_blob_emits_explicit_bind_source_mode(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, filename="input.csv", mime_type="text/csv", content="name\nAda")

        result = execute_tool(
            "set_source_from_blob",
            {"blob_id": blob.data["blob_id"], "on_success": "rows", "options": {"schema": {"mode": "observed"}}},
            _empty_state(),
            _catalog(),
            data_dir=blob_env["data_dir"],
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        assert result.success is True
        assert "source" in result.updated_state.sources
        marker = {key: result.updated_state.sources["source"].options[key] for key in ("blob_ref", "mode", "path")}
        assert marker["mode"] == "bind_source"
        shape = is_widened_blob_ref(marker)
        assert shape is not None
        assert shape.mode == "bind_source"
        assert shape.blob_id == UUID(blob.data["blob_id"])

    def test_bind_source_mode_is_stripped_from_engine_yaml(self, blob_env: dict[str, Any]) -> None:
        blob = _create_blob(blob_env, filename="input.csv", mime_type="text/csv", content="name\nAda")
        result = execute_tool(
            "set_source_from_blob",
            {"blob_id": blob.data["blob_id"], "on_success": "rows", "options": {"schema": {"mode": "observed"}}},
            _empty_state(),
            _catalog(),
            data_dir=blob_env["data_dir"],
            session_engine=blob_env["engine"],
            session_id=blob_env["session_id"],
        )

        pipeline = generate_pipeline_dict(result.updated_state)

        assert "blob_ref" not in pipeline["sources"]["source"]["options"]
        assert "mode" not in pipeline["sources"]["source"]["options"]


def test_tool_definitions_include_inline_blob_authoring_tools() -> None:
    definitions = {definition["name"]: definition for definition in get_tool_definitions()}

    assert "list_composer_blobs" in definitions
    assert definitions["list_composer_blobs"]["parameters"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    assert "wire_blob_inline_ref" in definitions
    assert definitions["wire_blob_inline_ref"]["parameters"]["required"] == ["field_path", "blob_id"]
    assert definitions["wire_blob_inline_ref"]["parameters"]["additionalProperties"] is False
    assert definitions["delete_blob"]["parameters"]["additionalProperties"] is False
