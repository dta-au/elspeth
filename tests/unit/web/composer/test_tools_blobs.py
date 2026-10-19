"""Composer blob-tool storage, custody, quota and integrity contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from elspeth.contracts.enums import CreationModality
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolResult
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.models import blobs_table
from elspeth.web.sessions.state_envelope import envelope_state_column
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.composer.test_tools import (
    _STUB_SHA256,
    _attached_notes,
    _blob_operation,
    _default_source,
    _empty_state,
    _insert_session_fork_operation,
    _insert_user_message,
    _mock_catalog,
    _session_engine_with_session,
    _verbatim_blob_context,
    execute_tool,
)


class TestBlobTools:
    """Blob composition tools: session context enforcement, storage_path exclusion,
    status guards, and source plugin wiring.

    Security contracts tested:
    - Blob tools fail without session context (no ambient access)
    - get_blob_metadata never exposes storage_path to the LLM
    - Wrong session_id returns failure (IDOR at the tool layer)
    - set_source_from_blob rejects non-ready blobs
    - set_source_from_blob wires the correct source plugin from MIME type
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        """Create an in-memory SQLite engine with tables and seed data."""
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")
            ensure_test_identity(conn, identity_id="other-user")

        self.session_id = str(uuid4())
        self.other_session_id = str(uuid4())
        self.blob_id = str(uuid4())
        self.pending_blob_id = str(uuid4())

        now = datetime.now(UTC)

        with self.engine.begin() as conn:
            # Two sessions
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test Session",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                sessions_table.insert().values(
                    id=self.other_session_id,
                    user_id="other-user",
                    auth_provider_type="local",
                    title="Other Session",
                    created_at=now,
                    updated_at=now,
                )
            )
            # Ready blob in session
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=100,
                    content_hash=_STUB_SHA256,
                    storage_path="/tmp/fake/data.csv",
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )
            # Pending blob in session
            conn.execute(
                blobs_table.insert().values(
                    id=self.pending_blob_id,
                    session_id=self.session_id,
                    filename="output.csv",
                    mime_type="text/csv",
                    size_bytes=0,
                    content_hash=None,
                    storage_path="/tmp/fake/output.csv",
                    created_at=now,
                    created_by="pipeline",
                    source_description=None,
                    status="pending",
                )
            )

    def test_list_blobs_without_session_context_returns_failure(self) -> None:
        """Blob tools with no session context must fail — no ambient data access."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("list_blobs", {}, state, catalog)
        assert result.success is False

    def test_get_blob_metadata_excludes_storage_path(self) -> None:
        """storage_path must never be exposed to the LLM — it leaks filesystem layout."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_blob_metadata",
            {"blob_id": self.blob_id},
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
            **_verbatim_blob_context(self.engine, self.session_id, "new"),
        )
        assert result.success is True
        assert "storage_path" not in result.data

    def test_get_blob_metadata_uses_allowlist_response_contract(self) -> None:
        """Metadata response must not default-expose prose or provenance fields."""
        from elspeth.web.sessions.models import blobs_table

        with self.engine.begin() as conn:
            conn.execute(
                blobs_table.update().where(blobs_table.c.id == self.blob_id).values(source_description="customer export sk-test-secret")
            )

        result = execute_tool(
            "get_blob_metadata",
            {"blob_id": self.blob_id},
            _empty_state(),
            _mock_catalog(),
            session_engine=self.engine,
            session_id=self.session_id,
        )

        assert result.success is True
        assert set(result.data) == {"id", "filename", "mime_type", "size_bytes", "content_hash", "status"}
        assert "source_description" not in result.data
        assert "creation_modality" not in result.data
        assert "created_from_message_id" not in result.data
        assert "sk-test-secret" not in str(result.data)

    def test_get_blob_metadata_rejects_invalid_blob_id_without_echoing_value(self) -> None:
        sentinel = "sensitive-value-not-a-uuid"

        result = execute_tool(
            "get_blob_metadata",
            {"blob_id": sentinel},
            _empty_state(),
            _mock_catalog(),
            session_engine=self.engine,
            session_id=self.session_id,
        )

        assert result.success is False
        assert "not a valid UUID" in result.validation.errors[0].message
        assert sentinel not in result.validation.errors[0].message

    def test_get_blob_metadata_not_found_does_not_echo_blob_id(self) -> None:
        from uuid import uuid4

        missing_blob_id = str(uuid4())

        result = execute_tool(
            "get_blob_metadata",
            {"blob_id": missing_blob_id},
            _empty_state(),
            _mock_catalog(),
            session_engine=self.engine,
            session_id=self.session_id,
        )

        assert result.success is False
        assert "not found" in result.validation.errors[0].message.lower()
        assert missing_blob_id not in result.validation.errors[0].message

    def test_get_blob_metadata_wrong_session_returns_failure(self) -> None:
        """IDOR at tool layer: blob belongs to session A, caller is session B."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_blob_metadata",
            {"blob_id": self.blob_id},
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.other_session_id,
        )
        assert result.success is False

    def _tamper_blob_row(self, **values: Any) -> None:
        """Bypass CHECK constraints so defensive read guards can be tested."""
        from sqlalchemy import text

        from elspeth.web.sessions.models import blobs_table

        with self.engine.begin() as conn:
            conn.execute(text("PRAGMA ignore_check_constraints = 1"))
            conn.execute(blobs_table.update().where(blobs_table.c.id == self.blob_id).values(**values))
            conn.execute(text("PRAGMA ignore_check_constraints = 0"))

    def test_get_blob_metadata_tampered_status_raises_audit_integrity_error(self) -> None:
        """Corrupted blob status must crash instead of leaking to the LLM."""
        from elspeth.contracts.errors import AuditIntegrityError

        self._tamper_blob_row(status="corrupted")

        with pytest.raises(AuditIntegrityError, match=r"blobs\.status"):
            execute_tool(
                "get_blob_metadata",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
            )

    def test_list_blobs_tampered_mime_type_raises_audit_integrity_error(self) -> None:
        """Corrupted MIME allowlist values must crash instead of being listed."""
        from elspeth.contracts.errors import AuditIntegrityError

        self._tamper_blob_row(mime_type="TEXT/CSV")

        with pytest.raises(AuditIntegrityError, match=r"blobs\.mime_type"):
            execute_tool(
                "list_blobs",
                {},
                _empty_state(),
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
            )

    def test_set_source_from_blob_rejects_non_ready(self) -> None:
        """Cannot wire a pending blob as source — content doesn't exist yet."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source_from_blob",
            {"blob_id": self.pending_blob_id, "on_success": "out"},
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
            **_verbatim_blob_context(self.engine, self.session_id, "new"),
        )
        assert result.success is False

    def test_set_source_from_blob_wires_correct_plugin(self) -> None:
        """text/csv blob should auto-resolve to the 'csv' source plugin."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source_from_blob",
            {"blob_id": self.blob_id, "on_success": "out", "on_validation_failure": "discard", "options": {"schema": {"mode": "observed"}}},
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
            **_verbatim_blob_context(self.engine, self.session_id, "post,run\n1,1"),
        )
        assert result.success is True
        assert _default_source(result.updated_state) is not None
        assert _default_source(result.updated_state).plugin == "csv"
        assert _default_source(result.updated_state).on_validation_failure == "discard"

    def test_set_source_from_blob_wires_named_source(self) -> None:
        """Blob-backed sources must be reachable as explicit named roots."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": self.blob_id,
                "source_name": "orders",
                "on_success": "orders_rows",
                "on_validation_failure": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
        )

        assert result.success is True
        assert result.updated_state.sources["orders"].plugin == "csv"
        assert result.updated_state.sources["orders"].options["blob_ref"] == self.blob_id
        assert result.affected_nodes == ("source:orders",)

    @pytest.mark.parametrize(
        "source_name",
        ("Orders", "on_success", " "),
    )
    def test_set_source_from_blob_invalid_source_name_raises_arg_error(self, source_name: str) -> None:
        """Invalid blob source names are LLM argument errors, not plugin crashes."""
        from elspeth.web.composer.protocol import ToolArgumentError

        state = _empty_state()
        catalog = _mock_catalog()

        with pytest.raises(ToolArgumentError) as exc_info:
            execute_tool(
                "set_source_from_blob",
                {
                    "blob_id": self.blob_id,
                    "source_name": source_name,
                    "on_success": "orders_rows",
                    "options": {"schema": {"mode": "observed"}},
                },
                state,
                catalog,
                session_engine=self.engine,
                session_id=self.session_id,
            )

        assert exc_info.value.argument == "source_name"
        assert isinstance(exc_info.value.__cause__, ValueError)

    def test_set_source_from_plain_text_blob_uses_text_source(self) -> None:
        """text/plain blob should auto-resolve to the 'text' source plugin."""
        from elspeth.web.sessions.models import blobs_table

        state = _empty_state()
        catalog = _mock_catalog()

        with self.engine.begin() as conn:
            conn.execute(blobs_table.update().where(blobs_table.c.id == self.blob_id).values(mime_type="text/plain"))

        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": self.blob_id,
                "on_success": "out",
                "on_validation_failure": "discard",
                "options": {"schema": {"mode": "observed"}, "column": "line"},
            },
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
            **_verbatim_blob_context(self.engine, self.session_id, "new"),
        )

        assert result.success is True
        assert _default_source(result.updated_state) is not None
        assert _default_source(result.updated_state).plugin == "text"

    def test_set_source_from_jsonl_blob_uses_json_plugin_with_format(self) -> None:
        """Regression: JSONL MIME types must resolve to 'json' plugin with format='jsonl'."""
        from elspeth.web.sessions.models import blobs_table

        state = _empty_state()
        catalog = _mock_catalog()

        with self.engine.begin() as conn:
            conn.execute(blobs_table.update().where(blobs_table.c.id == self.blob_id).values(mime_type="application/x-jsonlines"))

        result = execute_tool(
            "set_source_from_blob",
            {"blob_id": self.blob_id, "on_success": "out", "on_validation_failure": "discard", "options": {"schema": {"mode": "observed"}}},
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
            **_verbatim_blob_context(self.engine, self.session_id, "new"),
        )

        assert result.success is True
        assert _default_source(result.updated_state) is not None
        assert _default_source(result.updated_state).plugin == "json"
        assert _default_source(result.updated_state).options["format"] == "jsonl"

    def test_set_source_from_blob_merges_caller_options(self) -> None:
        """Caller-provided options are merged with blob-derived options.

        Plugin-specific config like schema and column must flow through,
        while path and blob_ref remain authoritative from the blob.
        """
        from elspeth.web.sessions.models import blobs_table

        state = _empty_state()
        catalog = _mock_catalog()

        # Update the test blob to text/plain so we get the text plugin
        with self.engine.begin() as conn:
            conn.execute(blobs_table.update().where(blobs_table.c.id == self.blob_id).values(mime_type="text/plain"))

        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": self.blob_id,
                "on_success": "out",
                "on_validation_failure": "discard",
                "options": {
                    "column": "line",
                    "schema": {"mode": "observed"},
                },
            },
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
            **_verbatim_blob_context(self.engine, self.session_id, "should,proceed\n1,1"),
        )

        assert result.success is True
        assert _default_source(result.updated_state) is not None
        assert _default_source(result.updated_state).plugin == "text"
        # Caller options merged in
        assert _default_source(result.updated_state).options["column"] == "line"
        assert _default_source(result.updated_state).options["schema"] == {"mode": "observed"}
        # Blob-derived options still present (path is internal, blob_ref is visible)
        assert "blob_ref" in _default_source(result.updated_state).options
        assert _default_source(result.updated_state).options["blob_ref"] == self.blob_id

    def test_set_source_from_blob_path_overrides_caller(self) -> None:
        """The blob-derived path cannot be overridden by caller.

        This is a security constraint: the blob's storage path is authoritative.
        Callers cannot inject an arbitrary path via the options parameter.
        """
        state = _empty_state()
        catalog = _mock_catalog()

        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": self.blob_id,
                "on_success": "out",
                "on_validation_failure": "discard",
                "options": {
                    "path": "/etc/passwd",  # Attempted path injection
                    "schema": {"mode": "observed"},
                },
            },
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
            **_verbatim_blob_context(self.engine, self.session_id, "would,corrupt,mid-run\n"),
        )

        assert result.success is True
        assert _default_source(result.updated_state) is not None
        # Blob's path and ref are authoritative — caller cannot override path.
        assert _default_source(result.updated_state).options["blob_ref"] == self.blob_id
        assert _default_source(result.updated_state).options["path"] != "/etc/passwd"

    def test_set_source_from_blob_rejects_caller_blob_ref(self) -> None:
        """blob_ref is an authoritative resolver output, not a caller option."""
        state = _empty_state()

        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": self.blob_id,
                "on_success": "out",
                "on_validation_failure": "discard",
                "options": {
                    "blob_ref": "malicious-ref",
                    "schema": {"mode": "observed"},
                },
            },
            state,
            _mock_catalog(),
            session_engine=self.engine,
            session_id=self.session_id,
        )

        assert result.success is False
        assert result.updated_state is state
        assert "blob_ref" in result.validation.errors[0].message
        assert "set_source_from_blob" in result.validation.errors[0].message

    def test_set_source_from_blob_gets_prior_validation(self) -> None:
        """Blob mutation tools must populate prior_validation for validation delta."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source_from_blob",
            {"blob_id": self.blob_id, "on_success": "out", "on_validation_failure": "discard", "options": {"schema": {"mode": "observed"}}},
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
        )
        assert result.success is True
        assert result.prior_validation is not None
        d = result.to_dict()
        assert "validation_delta" in d

    def test_set_source_from_blob_threaded_prior_used(self) -> None:
        """Threaded prior_validation is reused by blob mutation tools (identity check).

        execute_tool dispatches blob mutations through a separate branch from
        standard mutations. Both branches must honour the prior_validation kwarg.
        """
        state = _empty_state()
        catalog = _mock_catalog()
        threaded = state.validate()
        result = execute_tool(
            "set_source_from_blob",
            {"blob_id": self.blob_id, "on_success": "out", "on_validation_failure": "discard", "options": {"schema": {"mode": "observed"}}},
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
            prior_validation=threaded,
        )
        assert result.success is True
        assert result.prior_validation is threaded

    def test_set_source_from_blob_threaded_prior_produces_correct_delta(self) -> None:
        """Threaded and fresh prior_validation produce identical deltas for blob tools."""
        state = _empty_state()
        catalog = _mock_catalog()
        blob_args: dict[str, Any] = {
            "blob_id": self.blob_id,
            "on_success": "out",
            "on_validation_failure": "discard",
            "options": {"schema": {"mode": "observed"}},
        }
        # Fresh (no threading)
        result_fresh = execute_tool(
            "set_source_from_blob",
            blob_args,
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
        )
        # Threaded
        threaded = state.validate()
        result_threaded = execute_tool(
            "set_source_from_blob",
            blob_args,
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
            prior_validation=threaded,
        )
        delta_fresh = result_fresh.to_dict()["validation_delta"]
        delta_threaded = result_threaded.to_dict()["validation_delta"]
        assert delta_fresh == delta_threaded

    def test_blob_source_unknown_vf_sink_has_only_structured_error(self) -> None:
        """The blob adapter retains the error and its blob metadata."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": self.blob_id,
                "on_success": "out",
                "options": {"schema": {"mode": "observed"}},
                "on_validation_failure": "nonexistent",
            },
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
        )
        assert result.success is True
        assert result.data is not None
        assert "note" not in result.data
        assert "source_blob" in result.data
        assert [entry.error_code for entry in result.validation.errors].count("quarantine_unknown_output") == 1

    def test_create_blob_cleans_file_on_interrupted_atomic_write(self, tmp_path: Path) -> None:
        """A failed create_blob publication must delete its orphaned storage file."""
        from unittest.mock import patch

        from elspeth.web.blobs import service as blob_service

        state = _empty_state()
        catalog = _mock_catalog()
        data_dir = str(tmp_path)
        original_replace = blob_service.os.replace

        def _replace_then_fail(source, target, *, src_dir_fd=None, dst_dir_fd=None) -> None:
            original_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            raise RuntimeError("simulated interruption after file publication")

        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "a,b\n1,2")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            with (
                patch(
                    "elspeth.web.blobs.service.os.replace",
                    side_effect=_replace_then_fail,
                ),
                pytest.raises(RuntimeError, match="simulated interruption after file publication"),
            ):
                execute_tool(
                    "create_blob",
                    {"filename": "test.csv", "mime_type": "text/csv", "content": "a,b\n1,2"},
                    state,
                    catalog,
                    data_dir=data_dir,
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    **blob_provenance,
                )

            # Storage file must have been cleaned up
            blob_dir = tmp_path / "blobs" / self.session_id
            remaining = list(blob_dir.glob("*")) if blob_dir.exists() else []
            assert remaining == [], f"Orphaned files after interrupted publication: {remaining}"

    def test_update_blob_restores_old_content_on_db_failure(self, tmp_path: Path) -> None:
        """DB failure during update_blob must restore the original file content."""
        from datetime import UTC, datetime
        from unittest.mock import patch
        from uuid import uuid4

        from elspeth.web.blobs.service import content_hash
        from elspeth.web.sessions.models import blobs_table

        state = _empty_state()
        catalog = _mock_catalog()

        # Create a real blob on disk with known content
        blob_id = str(uuid4())
        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        storage_path = storage_dir / f"{blob_id}_test.csv"
        original_content = b"original,content\n1,2"
        storage_path.write_bytes(original_content)

        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=blob_id,
                    session_id=self.session_id,
                    filename="test.csv",
                    mime_type="text/csv",
                    size_bytes=len(original_content),
                    content_hash=content_hash(original_content),
                    storage_path=str(storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

        # Patch session_engine.begin() to raise AFTER the file is overwritten.
        # The update function reads old content, writes new content, THEN enters
        # the DB transaction.  We need the DB part to fail.
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "new,content\n3,4")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            with (
                patch.object(
                    self.engine,
                    "begin",
                    side_effect=RuntimeError("simulated DB failure"),
                ),
                pytest.raises(RuntimeError, match="simulated DB failure"),
            ):
                execute_tool(
                    "update_blob",
                    {"blob_id": blob_id, "content": "new,content\n3,4"},
                    state,
                    catalog,
                    data_dir=str(tmp_path),
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    **blob_provenance,
                )

            # File must contain the ORIGINAL content after rollback
            assert storage_path.read_bytes() == original_content

    def test_blob_rollback_does_not_catch_keyboard_interrupt(self, tmp_path: Path) -> None:
        """Blob exception handlers must catch Exception, not BaseException.

        Catching BaseException intercepts KeyboardInterrupt/SystemExit.
        Under KeyboardInterrupt, write_bytes() rollback (update_blob) could
        truncate the file, leaving it inconsistent with DB state.
        """
        import ast
        import inspect

        from elspeth.web.composer.tools import _execute_create_blob, _execute_update_blob

        for func in (_execute_create_blob, _execute_update_blob):
            source = inspect.getsource(func)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.type is not None and isinstance(node.type, ast.Name):
                    assert node.type.id != "BaseException", (
                        f"{func.__name__} catches BaseException — must use Exception to avoid intercepting KeyboardInterrupt/SystemExit"
                    )


class TestDeleteBlobActiveRunGuard:
    """delete_blob must refuse to delete blobs linked to active (pending/running) runs.

    Mirrors BlobServiceImpl.delete_blob() active-run guard — the composer tool
    layer must enforce the same invariant.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.session_id = str(uuid4())
        self.data_dir = str(tmp_path)
        self.blob_id = str(uuid4())
        self.run_id = str(uuid4())
        now = datetime.now(UTC)

        # Create blob on disk so unlink has a real target
        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_data.csv"
        self.storage_path.write_bytes(b"a,b\n1,2")

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(b"a,b\n1,2"),
                    content_hash=content_hash(b"a,b\n1,2"),
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def _insert_run_and_link(self, status: str) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from elspeth.web.sessions.models import (
            blob_run_links_table,
            composition_states_table,
            runs_table,
        )

        now = datetime.now(UTC)
        state_id = str(uuid4())
        with self.engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=self.session_id,
                    version=1,
                    source=None,
                    nodes=None,
                    edges=None,
                    outputs=None,
                    metadata_=None,
                    is_valid=False,
                    validation_errors=None,
                    # Plan §2294: composer-tools test fixture; provenance
                    # required for setup row supporting subsequent runs/
                    # blob_run_links FKs.
                    provenance="session_seed",
                    created_at=now,
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=self.run_id,
                    session_id=self.session_id,
                    state_id=state_id,
                    status=status,
                    started_at=now,
                    rows_processed=0,
                    rows_failed=0,
                )
            )
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=self.blob_id,
                    run_id=self.run_id,
                    direction="input",
                )
            )

    def _insert_run_without_link(self, status: str, *, source: dict[str, Any] | None = None) -> None:
        """Insert a run in the blob's session but omit the blob_run_links row.

        Simulates the pre-link window: _execute_locked() has called
        create_run() but link_blob_to_run() hasn't fired yet.

        Args:
            source: Composition state source dict.  Defaults to a source
                that references self.blob_id via blob_ref (the typical
                pre-link scenario).  Pass a different dict to simulate
                runs that use file-path sources with no blob_ref.
        """
        from datetime import UTC, datetime
        from uuid import uuid4

        from elspeth.web.sessions.models import (
            composition_states_table,
            runs_table,
        )

        if source is None:
            source = {
                "plugin": "csv",
                "on_success": "output",
                "on_validation_failure": "quarantine",
                "options": {"blob_ref": self.blob_id, "path": str(self.storage_path)},
            }
        else:
            source = {
                "on_success": "output",
                "on_validation_failure": "quarantine",
                **source,
            }

        now = datetime.now(UTC)
        state_id = str(uuid4())
        with self.engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=self.session_id,
                    version=1,
                    # Production shape, not the pre-2026-05 one. Since
                    # f0fd36087 every writer folds `source` into
                    # `sources={"source": ...}` and wraps each JSON column
                    # through the one envelope rule, so the active-run guard
                    # this fixture pins must be exercised against that shape --
                    # seeding a populated, bare `source` is what let
                    # elspeth-3db5745ba7 stay green while production 500'd.
                    source=None,
                    sources=envelope_state_column({"source": source}),
                    nodes=envelope_state_column([]),
                    edges=envelope_state_column([]),
                    outputs=envelope_state_column([]),
                    metadata_=envelope_state_column({"name": "Test", "description": ""}),
                    is_valid=False,
                    validation_errors=None,
                    # Plan §2294: composer-tools test fixture; provenance
                    # required for setup row supporting subsequent runs/
                    # blob_run_links FKs.
                    provenance="session_seed",
                    created_at=now,
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=self.run_id,
                    session_id=self.session_id,
                    state_id=state_id,
                    status=status,
                    started_at=now,
                    rows_processed=0,
                    rows_failed=0,
                )
            )

    def test_delete_succeeds_when_no_runs_linked(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
            assert result.success is True
            assert not self.storage_path.exists()

    def test_delete_rejected_while_session_fork_is_in_progress(self) -> None:
        operation_id = _insert_session_fork_operation(self.engine, self.session_id, status="in_progress")

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

            assert result.success is False
            assert operation_id in result.validation.errors[0].message
            assert "session fork" in result.validation.errors[0].message.lower()
            assert self.storage_path.read_bytes() == b"a,b\n1,2"

    @pytest.mark.parametrize("fork_status", ["completed", "failed"])
    def test_delete_allowed_after_session_fork_is_terminal(self, fork_status: str) -> None:
        _insert_session_fork_operation(self.engine, self.session_id, status=fork_status)

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

            assert result.success is True
            assert not self.storage_path.exists()

    def test_delete_rejected_when_pending_run_exists_without_link(self) -> None:
        """Pre-link window: run exists but blob_run_links row hasn't been created yet.

        _execute_locked() creates the run record before link_blob_to_run() inserts
        the link row.  During that gap, the explicit-link guard sees nothing.
        The composition-state guard must block deletion because the run's source
        references this blob via blob_ref.
        """
        self._insert_run_without_link("pending")
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
            assert result.success is False
            assert "active run" in result.validation.errors[0].message.lower()
            assert self.storage_path.exists(), "File must not be deleted when guard blocks"

    def test_delete_rejected_when_running_run_exists_without_link(self) -> None:
        """Same as pending — a running run without a link row must also block."""
        self._insert_run_without_link("running")
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
            assert result.success is False
            assert "active run" in result.validation.errors[0].message.lower()
            assert self.storage_path.exists(), "File must not be deleted when guard blocks"

    def test_delete_succeeds_when_active_run_uses_different_source(self) -> None:
        """Active run using source.path (no blob_ref) must not block unrelated blob deletion.

        Regression test: the original session-level guard blocked ALL blobs
        when ANY run was active, even if that run used a file-path source.
        The scoped guard checks source.options.blob_ref and only blocks
        if it matches this blob.
        """
        self._insert_run_without_link(
            "pending",
            source={"plugin": "csv", "options": {"path": "/data/external/other.csv"}},
        )
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
            assert result.success is True
            assert not self.storage_path.exists()

    def test_delete_rejected_when_active_run_path_matches_storage(self) -> None:
        """Active run using source.path matching this blob's storage_path must block.

        A run can read a blob's backing file via plain set_source with
        options.path (no blob_ref).  The guard must check path/file matches.
        """
        self._insert_run_without_link(
            "pending",
            source={"plugin": "csv", "options": {"path": str(self.storage_path)}},
        )
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
            assert result.success is False
            assert "active run" in result.validation.errors[0].message.lower()
            assert self.storage_path.exists(), "File must not be deleted when guard blocks"

    def test_delete_succeeds_when_completed_run_exists_without_link(self) -> None:
        """Completed runs (no link row) must not block deletion."""
        self._insert_run_without_link("completed")
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
            assert result.success is True
            assert not self.storage_path.exists()

    def test_delete_rejected_when_pending_run_linked(self) -> None:
        self._insert_run_and_link("pending")
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
            assert result.success is False
            assert "active run" in result.validation.errors[0].message.lower()
            assert self.storage_path.exists(), "File must not be deleted when guard blocks"

    def test_delete_rejected_when_running_run_linked(self) -> None:
        self._insert_run_and_link("running")
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
            assert result.success is False
            assert self.storage_path.exists(), "File must not be deleted when guard blocks"

    def test_delete_succeeds_when_completed_run_linked(self) -> None:
        self._insert_run_and_link("completed")
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
            assert result.success is True
            assert not self.storage_path.exists()

    def test_delete_restores_file_when_db_delete_fails_after_filesystem_mutation(self) -> None:
        """DB failure after the filesystem step must not leave a stale row/missing-file split."""
        from sqlalchemy import event, select

        from elspeth.web.sessions.models import blobs_table

        # The blob-row DELETE fails at the driver seam, not behind a forwarding
        # connection proxy. The code under test therefore keeps the REAL
        # SQLAlchemy Connection — which matters, because the custody lock
        # registers commit/rollback event listeners on that exact object — and
        # the fault lands inside the real transaction where a database failure
        # would. Only ``blobs`` is targeted: the tombstone journal writes to
        # ``blob_deletion_cleanups``, which this prefix does not match.
        def _fail_blob_row_delete(
            _conn: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            if statement.lstrip().upper().startswith("DELETE FROM BLOBS"):
                raise RuntimeError("simulated delete failure")

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            event.listen(self.engine, "before_cursor_execute", _fail_blob_row_delete)
            try:
                with pytest.raises(RuntimeError, match="simulated delete failure"):
                    execute_tool(
                        "delete_blob",
                        {"blob_id": self.blob_id},
                        _empty_state(),
                        _mock_catalog(),
                        data_dir=self.data_dir,
                        session_engine=self.engine,
                        session_id=self.session_id,
                        session_operation_authority=blob_authority,
                        session_operation_context=blob_operation_context,
                    )
            finally:
                event.remove(self.engine, "before_cursor_execute", _fail_blob_row_delete)

            assert self.storage_path.exists(), "Rollback must restore the backing file"
            with self.engine.connect() as conn:
                row = conn.execute(select(blobs_table.c.id).where(blobs_table.c.id == self.blob_id)).first()
            assert row is not None, "The blob row should still exist after the failed delete"


class TestUpdateBlobQuota:
    """update_blob must enforce per-session quota when the blob grows.

    Mirrors _execute_create_blob quota enforcement — the update path must
    also call _check_blob_quota atomically inside the same transaction as
    the DB UPDATE.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.session_id = str(uuid4())
        self.blob_id = str(uuid4())
        self.data_dir = str(tmp_path)
        now = datetime.now(UTC)

        # Create blob on disk with known content
        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_data.csv"
        self.original_content = b"small"
        self.storage_path.write_bytes(self.original_content)

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(self.original_content),
                    content_hash=content_hash(self.original_content),
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def test_check_blob_quota_locks_session_before_sum(self, monkeypatch) -> None:
        """Composer blob writers use the same session row serialization guard."""
        from elspeth.web.composer.tools import blobs as composer_blob_tools

        locked_sessions: list[str] = []
        original_lock = composer_blob_tools._lock_session_for_blob_quota

        def recording_lock(conn, session_id: str) -> None:
            locked_sessions.append(session_id)
            original_lock(conn, session_id)

        monkeypatch.setattr(composer_blob_tools, "_lock_session_for_blob_quota", recording_lock)

        with self.engine.begin() as conn:
            quota_error = composer_blob_tools._check_blob_quota(
                conn,
                self.session_id,
                additional_bytes=1,
                quota_bytes=100,
            )

        assert quota_error is None
        assert locked_sessions == [self.session_id]

    def test_update_locks_session_before_quota_sum(self) -> None:
        """Replacement quota reads follow a session-row lock on the same connection."""
        from sqlalchemy.dialects import postgresql

        statements: list[tuple[object, str]] = []
        quota_sums = 0

        def record_quota_read(conn, statement, multiparams, params, execution_options) -> None:
            nonlocal quota_sums
            compiled = str(statement.compile(dialect=postgresql.dialect()))
            if "sum(blobs.size_bytes)" in compiled:
                quota_sums += 1
                assert any(prior_conn is conn and "FROM sessions" in sql and "FOR UPDATE" in sql for prior_conn, sql in statements)
            statements.append((conn, compiled))

        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "larger content")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            event.listen(self.engine, "before_execute", record_quota_read)
            try:
                result = execute_tool(
                    "update_blob",
                    {"blob_id": self.blob_id, "content": "larger content"},
                    _empty_state(),
                    _mock_catalog(),
                    data_dir=self.data_dir,
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    **blob_provenance,
                )
            finally:
                event.remove(self.engine, "before_execute", record_quota_read)

            assert result.success is True
            assert quota_sums > 0, "the ordering assertion must observe a real quota SUM"

    def test_update_within_quota_succeeds(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "slightly larger content")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "slightly larger content"},
                state,
                catalog,
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
            assert result.success is True

    def test_update_exceeding_quota_rejected(self) -> None:
        from unittest.mock import patch

        state = _empty_state()
        catalog = _mock_catalog()
        # Set quota to a tiny value so any growth exceeds it
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "x" * 100)
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            with patch("elspeth.web.composer.tools.blobs._BLOB_QUOTA_BYTES", 10):
                result = execute_tool(
                    "update_blob",
                    {"blob_id": self.blob_id, "content": "x" * 100},
                    state,
                    catalog,
                    data_dir=self.data_dir,
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    **blob_provenance,
                )
            assert result.success is False
            assert "quota" in result.validation.errors[0].message.lower()

    def test_update_exceeding_quota_preserves_old_content(self) -> None:
        from unittest.mock import patch

        state = _empty_state()
        catalog = _mock_catalog()
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "x" * 100)
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            with patch("elspeth.web.composer.tools.blobs._BLOB_QUOTA_BYTES", 10):
                execute_tool(
                    "update_blob",
                    {"blob_id": self.blob_id, "content": "x" * 100},
                    state,
                    catalog,
                    data_dir=self.data_dir,
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    **blob_provenance,
                )
            assert self.storage_path.read_bytes() == self.original_content

    def test_shrink_always_succeeds(self) -> None:
        from unittest.mock import patch

        # First grow the blob so we have something to shrink
        state = _empty_state()
        catalog = _mock_catalog()
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "a" * 200)
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "a" * 200},
                state,
                catalog,
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
            assert result.success is True

        # Now set quota very low — shrinking should still succeed
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "tiny")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            with patch("elspeth.web.composer.tools.blobs._BLOB_QUOTA_BYTES", 10):
                result = execute_tool(
                    "update_blob",
                    {"blob_id": self.blob_id, "content": "tiny"},
                    state,
                    catalog,
                    data_dir=self.data_dir,
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    **blob_provenance,
                )
            assert result.success is True

    def test_delta_boundary_case(self) -> None:
        """Update that fits when measured by delta but not by absolute new size.

        Session total = 490 (including current blob at old_size=5).
        New content = 15 bytes. Delta = 10. Total after = 490 + 10 = 500.
        Must succeed at quota=500 because 500 <= 500.
        Would fail if check incorrectly used full len(content_bytes)=15.
        """
        from datetime import UTC, datetime
        from unittest.mock import patch
        from uuid import uuid4

        from elspeth.web.blobs.service import content_hash
        from elspeth.web.sessions.models import blobs_table

        # Add a second blob to bring session total to 490
        filler_id = str(uuid4())
        now = datetime.now(UTC)
        filler_path = Path(self.data_dir) / "blobs" / self.session_id / f"{filler_id}_filler.bin"
        filler_path.write_bytes(b"x" * 485)

        with self.engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=filler_id,
                    session_id=self.session_id,
                    filename="filler.bin",
                    mime_type="application/octet-stream",
                    size_bytes=485,
                    content_hash=content_hash(b"x" * 485),
                    storage_path=str(filler_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )
        # Session total is now 5 (original) + 485 (filler) = 490

        state = _empty_state()
        catalog = _mock_catalog()
        # New content: 15 bytes. Delta = 15 - 5 = 10. Total after = 490 + 10 = 500.
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "x" * 15)
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            with patch("elspeth.web.composer.tools.blobs._BLOB_QUOTA_BYTES", 500):
                result = execute_tool(
                    "update_blob",
                    {"blob_id": self.blob_id, "content": "x" * 15},
                    state,
                    catalog,
                    data_dir=self.data_dir,
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    **blob_provenance,
                )
            assert result.success is True, f"Delta-based quota check should pass at boundary: {result.data}"

    def test_shrink_on_at_quota_session_succeeds(self) -> None:
        """Shrinking a blob on a session exactly at quota must succeed."""
        from unittest.mock import patch

        state = _empty_state()
        catalog = _mock_catalog()
        # Quota exactly matches current total (5 bytes)
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "x")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            with patch("elspeth.web.composer.tools.blobs._BLOB_QUOTA_BYTES", len(self.original_content)):
                result = execute_tool(
                    "update_blob",
                    {"blob_id": self.blob_id, "content": "x"},  # 1 byte < 5 bytes
                    state,
                    catalog,
                    data_dir=self.data_dir,
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    **blob_provenance,
                )
            assert result.success is True

    def test_replacement_refuses_stale_size_snapshot(self) -> None:
        """A competing committed replacement invalidates the entire expected snapshot."""
        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.web.blobs.replacement import BlobReplacementCoordinator
        from elspeth.web.blobs.service import content_hash

        original_replace = BlobReplacementCoordinator.replace_blob
        competing_content = b"y" * 50
        competing_writes = 0

        def replace_after_competitor(driver, **kwargs):
            nonlocal competing_writes
            expected = kwargs["expected"]
            competitor = replace(expected, size_bytes=len(competing_content), content_hash=content_hash(competing_content))
            original_replace(
                driver,
                expected=expected,
                replacement=competitor,
                content=competing_content,
                context=kwargs["context"],
                max_storage_per_session=70,
                accepting_proposal_id=None,
            )
            competing_writes += 1
            return original_replace(driver, **kwargs)

        provenance = _verbatim_blob_context(self.engine, self.session_id, "x" * 60)
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            with (
                patch.object(BlobReplacementCoordinator, "replace_blob", replace_after_competitor),
                pytest.raises(AuditIntegrityError, match="metadata changed before durable replacement"),
            ):
                execute_tool(
                    "update_blob",
                    {"blob_id": self.blob_id, "content": "x" * 60},
                    _empty_state(),
                    _mock_catalog(),
                    data_dir=self.data_dir,
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=authority,
                    session_operation_context=context,
                    max_blob_storage_per_session_bytes=70,
                    **provenance,
                )
            assert competing_writes == 1
            assert self.storage_path.read_bytes() == competing_content
            with self.engine.connect() as conn:
                row = conn.execute(select(blobs_table).where(blobs_table.c.id == self.blob_id)).one()
            assert row.size_bytes == len(competing_content)
            assert row.content_hash == content_hash(competing_content)


class TestUpdateBlobRollbackPreservesPrimaryException:
    """Composer replacement preserves primary faults and durable cleanup obligations."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.session_id = str(uuid4())
        self.blob_id = str(uuid4())
        self.data_dir = str(tmp_path)
        now = datetime.now(UTC)

        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_data.csv"
        self.original_content = b"original-bytes"
        self.storage_path.write_bytes(self.original_content)

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(self.original_content),
                    content_hash=content_hash(self.original_content),
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def _update(self, authority, context, provenance, *, quota: int = 1000) -> ToolResult:
        return execute_tool(
            "update_blob",
            {"blob_id": self.blob_id, "content": "new"},
            _empty_state(),
            _mock_catalog(),
            data_dir=self.data_dir,
            session_engine=self.engine,
            session_id=self.session_id,
            session_operation_authority=authority,
            session_operation_context=context,
            max_blob_storage_per_session_bytes=quota,
            **provenance,
        )

    def _assert_settled(self, expected: bytes) -> None:
        from elspeth.web.blobs.service import content_hash
        from elspeth.web.sessions.models import blob_replacement_cleanups_table

        assert self.storage_path.read_bytes() == expected
        with self.engine.connect() as conn:
            record = conn.execute(select(blobs_table).where(blobs_table.c.id == self.blob_id)).one()
            obligations = conn.execute(select(blob_replacement_cleanups_table)).all()
        assert record.size_bytes == len(expected)
        assert record.content_hash == content_hash(expected)
        assert obligations == []
        assert list(self.storage_path.parent.iterdir()) == [self.storage_path]

    def test_preparation_failure_preserves_primary_error_without_writing_bytes(self) -> None:
        from elspeth.web.blobs.replacement import BlobReplacementCoordinator

        provenance = _verbatim_blob_context(self.engine, self.session_id, "new")
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            with (
                patch(
                    "elspeth.web.coordination.repository._RepositoryBlobMutations.prepare_blob_replacement",
                    side_effect=RuntimeError("primary-db-fault"),
                ),
                patch.object(BlobReplacementCoordinator, "_remove", side_effect=OSError("cleanup-fault")) as cleanup,
                pytest.raises(RuntimeError, match="primary-db-fault") as error,
            ):
                self._update(authority, context, provenance)
            assert type(error.value) is RuntimeError
            assert _attached_notes(error.value) == ()
            cleanup.assert_not_called()
            self._assert_settled(self.original_content)

    @pytest.mark.parametrize("rejection", ["quota", "retention"])
    def test_admission_rejection_creates_no_bytes_requiring_cleanup(self, rejection: str) -> None:
        from elspeth.web.blobs.replacement import BlobReplacementCoordinator

        if rejection == "retention":
            _insert_session_fork_operation(self.engine, self.session_id, status="in_progress")
        provenance = _verbatim_blob_context(self.engine, self.session_id, "new")
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            with patch.object(BlobReplacementCoordinator, "_remove", side_effect=OSError("cleanup-fault")) as cleanup:
                result = self._update(authority, context, provenance, quota=1 if rejection == "quota" else 1000)
            assert result.success is False
            assert ("quota" if rejection == "quota" else "fork") in result.validation.errors[0].message
            cleanup.assert_not_called()
            self._assert_settled(self.original_content)

    def test_recovery_failure_preserves_primary_commit_error_and_durable_obligation(self) -> None:
        from elspeth.web.blobs.replacement import BlobReplacementCoordinator
        from elspeth.web.sessions.models import blob_replacement_cleanups_table

        provenance = _verbatim_blob_context(self.engine, self.session_id, "new")
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            with (
                patch(
                    "elspeth.web.coordination.repository._RepositoryBlobMutations.commit_blob_replacement",
                    side_effect=RuntimeError("primary-commit-fault"),
                ),
                patch.object(BlobReplacementCoordinator, "_remove", side_effect=OSError("cleanup-fault")),
                pytest.raises(RuntimeError, match="primary-commit-fault") as error,
            ):
                self._update(authority, context, provenance)
            assert type(error.value) is RuntimeError
            assert any("Durable replacement recovery remains pending: OSError" in note for note in error.value.__notes__)
            assert self.storage_path.read_bytes() == self.original_content
            with self.engine.connect() as conn:
                assert conn.execute(select(blob_replacement_cleanups_table)).one().phase == "swap_pending"
            BlobReplacementCoordinator(engine=self.engine, data_dir=Path(self.data_dir), session_operation_authority=authority).reconcile(
                context=context
            )
            self._assert_settled(self.original_content)

    def test_cleanup_failure_preserves_committed_update_for_recovery(self) -> None:
        from elspeth.web.blobs.replacement import BlobReplacementCoordinator
        from elspeth.web.blobs.service import content_hash
        from elspeth.web.sessions.models import blob_replacement_cleanups_table

        provenance = _verbatim_blob_context(self.engine, self.session_id, "new")
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            with (
                patch.object(BlobReplacementCoordinator, "_remove", side_effect=OSError("cleanup-fault")),
                pytest.raises(OSError, match="cleanup-fault"),
            ):
                self._update(authority, context, provenance)
            assert self.storage_path.read_bytes() == b"new"
            with self.engine.connect() as conn:
                assert conn.execute(select(blob_replacement_cleanups_table)).one().phase == "purge_pending"
                assert conn.execute(
                    select(blobs_table.c.content_hash).where(blobs_table.c.id == self.blob_id)
                ).scalar_one() == content_hash(b"new")
            BlobReplacementCoordinator(engine=self.engine, data_dir=Path(self.data_dir), session_operation_authority=authority).reconcile(
                context=context
            )
            self._assert_settled(b"new")


class TestBlobCustodySessionIsolation:
    """Shared custody serializes one session while unrelated sessions progress."""

    @pytest.mark.parametrize("same_session", [True, False], ids=["same-session-blocks", "other-session-progresses"])
    def test_acquisition_obeys_session_scope(self, same_session: bool) -> None:
        from threading import Event, Thread

        from elspeth.web.blobs.service import _blob_custody_session_lock

        engine, session_id = _session_engine_with_session()
        contender_session = session_id if same_session else str(uuid4())
        started = Event()
        entered = Event()
        errors: list[Exception] = []

        def acquire_custody() -> None:
            started.set()
            try:
                with _blob_custody_session_lock(engine, contender_session):
                    entered.set()
            except Exception as exc:
                errors.append(exc)

        worker = Thread(target=acquire_custody, daemon=True)
        try:
            with _blob_custody_session_lock(engine, session_id):
                worker.start()
                assert started.wait(2), "contender never started"
                if same_session:
                    assert not entered.wait(0.2), "same-session contender bypassed shared custody"
                else:
                    assert entered.wait(2), "unrelated session was blocked by shared custody"
            worker.join(2)
            assert not worker.is_alive(), "contender did not finish after custody release"
            assert errors == []
            assert entered.is_set()
        finally:
            worker.join(2)
            engine.dispose()


class TestUpdateBlobSessionLockSerialisation:
    """Blob update/delete mutations must acquire the shared session lock.

    The read→filesystem mutation→commit critical section must be atomic
    across concurrent composer-tool callers on the same session.
    Holding the session lock externally from the test must block the
    tool call entirely — if the tool bypasses the lock, the worker
    thread completes while the main thread still holds the mutex,
    revealing the race.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.session_id = str(uuid4())
        self.blob_id = str(uuid4())
        self.data_dir = str(tmp_path)
        now = datetime.now(UTC)

        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_data.csv"
        self.original_content = b"orig"
        self.storage_path.write_bytes(self.original_content)

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(self.original_content),
                    content_hash=content_hash(self.original_content),
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def test_update_blob_blocks_when_session_lock_is_held(self) -> None:
        """Worker must NOT complete while the main thread holds the lock.

        Strategy: acquire the session lock externally, spawn a worker
        that calls update_blob, assert the worker is still alive after
        a short wait.  Releasing the lock unblocks the worker, which
        must then complete and produce a successful update.  A regression
        that skipped the lock would make the worker complete before the
        lock is released — the short-wait assertion catches that.

        Inverted-flake guard: a ``started`` event set at the very top
        of the worker body distinguishes "blocked by the lock as
        intended" from "worker thread never ran" (scheduler stall,
        import failure, etc.).  Without the probe, both cases look
        identical (completed.wait returns False) and a broken test
        would falsely pass.
        """
        import threading as stdlib_threading

        from elspeth.web.blobs.service import _blob_custody_session_lock

        lock = _blob_custody_session_lock(self.engine, self.session_id)
        started = stdlib_threading.Event()
        completed = stdlib_threading.Event()
        result_holder: list[Any] = []

        def worker() -> None:
            started.set()
            try:
                result = execute_tool(
                    "update_blob",
                    {"blob_id": self.blob_id, "content": "new-content-from-worker"},
                    _empty_state(),
                    _mock_catalog(),
                    data_dir=self.data_dir,
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    **blob_provenance,
                )
                result_holder.append(result)
            finally:
                completed.set()

        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "new-content-from-worker")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            lock.__enter__()
            try:
                t = stdlib_threading.Thread(target=worker, daemon=True)
                t.start()
                # Probe first: the worker must actually enter its body
                # before we interpret ``completed.wait`` returning False as
                # "lock-blocked."  A thread that never scheduled would
                # satisfy the completed-wait check for the wrong reason.
                assert started.wait(timeout=2.0), "worker thread never entered its body"
                # While we hold the lock, the worker MUST NOT complete — if
                # it does, the tool bypassed the session lock. Keep a full
                # second of slack here: under xdist load the worker thread may
                # take longer than a few hundred milliseconds to reach the
                # lock acquisition point even though the locking contract is
                # correct.
                blocked = not completed.wait(timeout=1.0)
                assert blocked, (
                    "update_blob completed while the session lock was held externally; "
                    "the tool did not acquire shared blob custody before reading metadata, "
                    "reopening the I4 file/DB rollback race."
                )
            finally:
                lock.__exit__(None, None, None)

            assert completed.wait(timeout=2.0), "update_blob did not complete within 2s after session lock was released"
            t.join(timeout=2.0)
            assert not t.is_alive(), "worker thread failed to exit after update completed"
            assert result_holder, "worker did not produce a result"
            assert result_holder[0].success is True, f"Update failed after lock release: {result_holder[0].data}"
            assert self.storage_path.read_bytes() == b"new-content-from-worker"

    def test_delete_blob_blocks_when_session_lock_is_held(self) -> None:
        """Delete must share update's lock for its read->tombstone->commit sequence."""
        import threading as stdlib_threading

        from elspeth.web.blobs.service import _blob_custody_session_lock

        lock = _blob_custody_session_lock(self.engine, self.session_id)
        started = stdlib_threading.Event()
        completed = stdlib_threading.Event()
        result_holder: list[Any] = []

        def worker() -> None:
            started.set()
            try:
                result_holder.append(
                    execute_tool(
                        "delete_blob",
                        {"blob_id": self.blob_id},
                        _empty_state(),
                        _mock_catalog(),
                        data_dir=self.data_dir,
                        session_engine=self.engine,
                        session_id=self.session_id,
                        session_operation_authority=blob_authority,
                        session_operation_context=blob_operation_context,
                    )
                )
            finally:
                completed.set()

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            lock.__enter__()
            try:
                thread = stdlib_threading.Thread(target=worker, daemon=True)
                thread.start()
                assert started.wait(timeout=2.0), "worker thread never entered its body"
                assert not completed.wait(timeout=1.0), (
                    "delete_blob completed while update_blob's shared session lock was held; "
                    "delete and update can race their filesystem/DB mutations"
                )
            finally:
                lock.__exit__(None, None, None)

            assert completed.wait(timeout=2.0), "delete_blob did not complete after the shared lock was released"
            thread.join(timeout=2.0)
            assert not thread.is_alive(), "delete_blob worker failed to exit"
            assert result_holder and result_holder[0].success is True
            assert not self.storage_path.exists()


class TestUpdateBlobQuotaRollbackDivergence:
    """Quota breach must return ``_failure_result`` without any file mutation.

    Pre-atomic-rename, ``_execute_update_blob`` overwrote
    ``storage_path`` BEFORE the DB transaction; a quota breach inside
    the transaction therefore required a rollback write, and a
    rollback-write OSError was surfaced via a RuntimeError with
    add_note divergence discipline (the I5 fix).

    Post atomic-rename (bug_004), the file is written to a sibling
    tempfile and swapped in via ``os.replace`` only AFTER the quota
    check has passed.  A quota breach therefore happens before any
    file mutation — no rollback, no RuntimeError, no add_note; the
    caller simply sees a ``ToolResult(success=False, ...)`` carrying
    the quota message.  The divergence-on-rollback-OSError discipline
    remains in the code as a defensive guardrail for the narrow
    post-replace commit-failure window, but it is no longer reachable
    via the quota path.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.session_id = str(uuid4())
        self.blob_id = str(uuid4())
        self.data_dir = str(tmp_path)
        now = datetime.now(UTC)

        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_data.csv"
        self.original_content = b"pre-quota-bytes"
        self.storage_path.write_bytes(self.original_content)

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(self.original_content),
                    content_hash=content_hash(self.original_content),
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def test_quota_breach_returns_failure_without_touching_storage(self) -> None:
        """Quota failure returns ToolResult(success=False) with storage intact.

        Under the atomic-rename design the quota check runs BEFORE
        ``os.replace``, so a quota breach leaves storage_path exactly
        as it was.  No rollback write is needed, no RuntimeError is
        raised, and no add_note is attached — the LLM simply sees a
        failure result describing the quota exhaustion.

        Tripwire: patches ``Path.write_bytes`` to fail on any write to
        storage_path so an accidental regression to "write-first then
        rollback" would surface as an ENOSPC-like error instead of a
        clean quota failure.
        """
        from unittest.mock import patch

        real_write_bytes = Path.write_bytes
        target_path_str = str(self.storage_path)
        tripwire_hits: list[str] = []

        def _tripwire_write_bytes(path_self: Path, data: bytes) -> int:
            if str(path_self) == target_path_str:
                tripwire_hits.append("storage_path was written pre-replace")
                raise OSError(28, "Tripwire: pre-replace write to storage_path not allowed")
            return real_write_bytes(path_self, data)

        state = _empty_state()
        catalog = _mock_catalog()

        # Quota 10 bytes; new content 100 bytes → delta 85 exceeds quota.
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "x" * 100)
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            with (
                patch("elspeth.web.composer.tools.blobs._BLOB_QUOTA_BYTES", 10),
                patch.object(Path, "write_bytes", _tripwire_write_bytes),
            ):
                result = execute_tool(
                    "update_blob",
                    {"blob_id": self.blob_id, "content": "x" * 100},
                    state,
                    catalog,
                    data_dir=self.data_dir,
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    **blob_provenance,
                )

            # Clean failure result — no exception, no divergence.
            assert result.success is False, f"Expected quota failure result, got {result!r}"
            assert "quota" in result.validation.errors[0].message.lower(), (
                f"Quota failure message missing from error: {result.validation.errors[0].message!r}"
            )
            # Tripwire must not have fired — no write to storage_path.
            assert tripwire_hits == [], f"Pre-replace write to storage_path detected (atomic-rename regression): {tripwire_hits}"
            # Storage unchanged.
            assert self.storage_path.read_bytes() == self.original_content
            # Tempfile cleaned up in finally.
            leftovers = [p for p in self.storage_path.parent.iterdir() if p != self.storage_path]
            assert leftovers == [], f"Tempfile leaked after quota breach: {leftovers}"

    def test_quota_rollback_success_returns_failure_result_not_exception(self) -> None:
        """When rollback succeeds on quota path, callers still get a ToolResult.

        Regression guard: the quota-exceeded contract is that callers
        receive a ``ToolResult(success=False, ...)`` — NOT a raised
        exception — when the quota is breached AND the rollback
        succeeds.  The I5 fix adds the divergence-on-rollback-failure
        path without changing the happy-path shape.
        """
        from unittest.mock import patch

        state = _empty_state()
        catalog = _mock_catalog()

        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "x" * 100)
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            with patch("elspeth.web.composer.tools.blobs._BLOB_QUOTA_BYTES", 10):
                result = execute_tool(
                    "update_blob",
                    {"blob_id": self.blob_id, "content": "x" * 100},
                    state,
                    catalog,
                    data_dir=self.data_dir,
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    **blob_provenance,
                )

            assert result.success is False, "Quota-exceeded must return failure, not success"
            assert "quota" in result.validation.errors[0].message.lower()
            # File must be restored to original content.
            assert self.storage_path.read_bytes() == self.original_content, (
                "File was not rolled back after quota-exceeded with successful rollback"
            )


class TestCreateBlobTypeGuard:
    """The Tier-3 content-type guard must raise ToolArgumentError, not TypeError.

    Mocked service-level tests (test_wrong_type_tool_arg_returns_error in
    test_service.py) patch execute_tool at the seam and cannot prove the
    real handler raises the right class. This test drives the handler
    end-to-end through execute_tool() dispatch.

    Post Task 13 (Wave 2 — ``create_blob`` manifest promotion): the
    Tier-3 type guard now lives in :class:`CreateBlobArgumentsModel`
    via Pydantic's strict ``content: str`` validation.  The handler
    catches :class:`pydantic.ValidationError` and re-raises as
    :class:`ToolArgumentError` (pattern at ``tools.py:2320-2327`` /
    Task 4); the LLM-facing message names the argument-bundle and the
    Pydantic model, not the offending value (rev-2 BLOCKER_A leak
    discipline).  Memory:
    ``feedback_locked_in_buggy_expectations`` — the previous
    ``"'content' must be a string, got int"`` assertion pinned the
    legacy ``_prepare_blob_create`` message; the new boundary fires
    earlier with a leak-safe shell.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.session_id = str(uuid4())
        self.data_dir = tmp_path
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )

    def test_non_string_content_raises_tool_argument_error(self) -> None:
        from pydantic import ValidationError as PydanticValidationError

        from elspeth.web.composer.protocol import ToolArgumentError

        catalog = _mock_catalog()
        state = _empty_state()

        # Post Task 13 the LLM-facing message names the argument-bundle and
        # the Pydantic model; the raw offending value is not echoed (the
        # full Pydantic detail survives on __cause__ for auditors).
        with (
            pytest.raises(
                ToolArgumentError,
                match=(
                    r"^'create_blob arguments' must be object conforming to CreateBlobArgumentsModel\. "
                    r"Match the tool's declared JSON types\. Supply object and array fields as actual JSON objects and arrays, "
                    r"not strings containing JSON\., got ValidationError$"
                ),
            ) as exc_info,
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
        ):
            execute_tool(
                "create_blob",
                {
                    "filename": "notes.txt",
                    "mime_type": "text/plain",
                    "content": 42,  # wrong type — LLM sent int where str required
                },
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        # __cause__ chain MUST preserve the structured Pydantic detail
        # (rev-2 BLOCKER_A leak discipline: full detail audit-side,
        # leak-safe shell LLM-side).
        assert isinstance(exc_info.value.__cause__, PydanticValidationError)


class TestUpdateBlobTypeGuard:
    """Parallels TestCreateBlobTypeGuard for _execute_update_blob.

    The fixture is deliberately copy-pasted from TestCreateBlobTypeGuard
    rather than factored into a shared helper: the two guards are
    independent raise sites and one should be moveable without the other.

    Post Task 13 (Wave 2 — ``update_blob`` manifest promotion): the
    Tier-3 type guard now lives in :class:`UpdateBlobArgumentsModel`
    via Pydantic's strict ``content: str`` validation.  Identical
    discipline to :class:`TestCreateBlobTypeGuard` — the same locked-in
    legacy assertion (``"'content' must be a string, got int"``) is
    updated to the new ToolArgumentError shape; the file-mutation
    critical section is never entered on a pure argument-validation
    failure (handler docstring pins this precedence).
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.session_id = str(uuid4())
        self.data_dir = tmp_path
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )

    def test_non_string_content_raises_tool_argument_error(self) -> None:
        from pydantic import ValidationError as PydanticValidationError

        from elspeth.web.composer.protocol import ToolArgumentError

        catalog = _mock_catalog()
        state = _empty_state()

        # Seed a real blob so the handler reaches the content guard before
        # the "blob not found" check.  Use the create path end-to-end so
        # the row is persisted by the same code path production uses.
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "initial")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            create_result = execute_tool(
                "create_blob",
                {"filename": "a.txt", "mime_type": "text/plain", "content": "initial"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
        blob_id = create_result.data["blob_id"]
        state = create_result.updated_state

        # Post Task 13 the LLM-facing message names the argument-bundle and
        # the Pydantic model; the raw offending value is not echoed (the
        # full Pydantic detail survives on __cause__ for auditors).
        with (
            pytest.raises(
                ToolArgumentError,
                match=(
                    r"^'update_blob arguments' must be object conforming to UpdateBlobArgumentsModel\. "
                    r"Match the tool's declared JSON types\. Supply object and array fields as actual JSON objects and arrays, "
                    r"not strings containing JSON\., got ValidationError$"
                ),
            ) as exc_info,
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
        ):
            execute_tool(
                "update_blob",
                {"blob_id": blob_id, "content": 42},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        # __cause__ chain MUST preserve the structured Pydantic detail.
        assert isinstance(exc_info.value.__cause__, PydanticValidationError)

    def test_unpaired_surrogate_content_raises_tool_argument_error(self) -> None:
        from elspeth.web.composer.protocol import ToolArgumentError

        catalog = _mock_catalog()
        state = _empty_state()

        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "initial")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            create_result = execute_tool(
                "create_blob",
                {"filename": "a.txt", "mime_type": "text/plain", "content": "initial"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
        blob_id = create_result.data["blob_id"]
        user_message_id = _insert_user_message(self.engine, self.session_id, "Please update the blob.")

        with (
            pytest.raises(ToolArgumentError, match="valid UTF-8"),
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
        ):
            execute_tool(
                "update_blob",
                {"blob_id": blob_id, "content": "bad\ud800"},
                create_result.updated_state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                user_message_id=user_message_id,
                composer_model_identifier="test-model",
                composer_model_version="test-model-2026-06-28",
                composer_provider="test-provider",
                composer_skill_hash="a" * 64,
                tool_arguments_hash="b" * 64,
            )


class TestSetSourceFromBlobTypeGuard:
    """Malformed `options` must stay a retryable tool-argument failure.

    Post Task 13 (Wave 2 — ``set_source_from_blob`` manifest promotion):
    the Tier-3 type guard now lives in
    :class:`SetSourceFromBlobArgumentsModel` via Pydantic's strict
    ``options: dict[str, Any]`` validation.  The handler catches
    :class:`pydantic.ValidationError` and re-raises as
    :class:`ToolArgumentError` (pattern at ``tools.py:2320-2327`` /
    Task 4); the LLM-facing message names the argument-bundle and the
    Pydantic model, not the offending value (rev-2 BLOCKER_A leak
    discipline).  Memory:
    ``feedback_locked_in_buggy_expectations`` — the previous
    ``"'options' must be an object, got str"`` assertion pinned the
    legacy in-handler isinstance guard; the new boundary fires earlier
    with a leak-safe shell.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.session_id = str(uuid4())
        self.data_dir = tmp_path
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )

    def test_non_object_options_raises_tool_argument_error(self) -> None:
        from pydantic import ValidationError as PydanticValidationError

        from elspeth.web.composer.protocol import ToolArgumentError

        catalog = _mock_catalog()
        state = _empty_state()

        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "hello")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            create_result = execute_tool(
                "create_blob",
                {"filename": "seed.txt", "mime_type": "text/plain", "content": "hello"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
        blob_id = create_result.data["blob_id"]
        state = create_result.updated_state

        # Post Task 13 the LLM-facing message names the argument-bundle and
        # the Pydantic model; the raw offending value (the string
        # "column=text") and its type ("got str") are not echoed.
        with pytest.raises(
            ToolArgumentError,
            match=(
                r"^'set_source_from_blob arguments' must be object conforming to SetSourceFromBlobArgumentsModel\. "
                r"Match the tool's declared JSON types\. Supply object and array fields as actual JSON objects and arrays, "
                r"not strings containing JSON\., got ValidationError$"
            ),
        ) as exc_info:
            execute_tool(
                "set_source_from_blob",
                {
                    "blob_id": blob_id,
                    "on_success": "out",
                    "options": "column=text",
                },
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
            )
        # __cause__ chain MUST preserve the structured Pydantic detail.
        assert isinstance(exc_info.value.__cause__, PydanticValidationError)


class TestGetBlobContentGuards:
    """``get_blob_content`` must mirror BlobServiceImpl.read_blob_content guards.

    The composer tool returns blob bytes to an LLM composing a pipeline.
    Without these guards the LLM can:

    * observe a partially-written blob (status=pending) and treat it as
      authoritative;
    * observe a blob whose on-disk bytes have drifted from the stored
      content_hash (corruption, tampering, or a write-path bug) without
      the Tier-1 BlobIntegrityError firing;
    * crash the tool dispatcher with an unhandled UnicodeDecodeError on
      non-UTF-8 bytes that the MIME allowlist admits (``text/csv`` is
      frequently latin-1 in the wild).

    The canonical read path — ``BlobServiceImpl.read_blob_content`` —
    is async and engine-bound, so the fix mirrors its three guards
    inline.  These tests pin the guard semantics so future drift is
    caught at CI time.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.data_dir = tmp_path
        self.session_id = str(uuid4())
        self.blob_id = str(uuid4())
        now = datetime.now(UTC)

        # Real content with a real SHA-256 so hash verification can
        # succeed on the happy path and be perturbed deterministically
        # on the mismatch path.
        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_data.csv"
        self.content_bytes = b"col_a,col_b\n1,2\n3,4\n"
        self.content_hash_hex = _content_hash(self.content_bytes)
        self.storage_path.write_bytes(self.content_bytes)

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(self.content_bytes),
                    content_hash=self.content_hash_hex,
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def _set_status(self, status: str) -> None:
        from elspeth.web.sessions.models import blobs_table

        with self.engine.begin() as conn:
            conn.execute(blobs_table.update().where(blobs_table.c.id == self.blob_id).values(status=status))

    def test_ready_blob_with_matching_hash_returns_content(self) -> None:
        """Happy path — status=ready, hash matches, bytes are UTF-8."""
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert result.success is True
        assert result.data["content"] == self.content_bytes.decode("utf-8")

    def test_explicit_null_identity_at_zero_reads_the_verified_current_blob(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id, "offset_chars": 0, "expected_content_hash": None},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=context,
            )
        assert result.success
        assert result.data["content"] == self.content_bytes.decode("utf-8")
        assert result.data["content_hash"] == self.content_hash_hex

    def _replace_content(self, content: str) -> str:
        from elspeth.web.blobs.service import content_hash

        data = content.encode("utf-8")
        digest = content_hash(data)
        self.storage_path.write_bytes(data)
        with self.engine.begin() as conn:
            conn.execute(blobs_table.update().where(blobs_table.c.id == self.blob_id).values(size_bytes=len(data), content_hash=digest))
        return digest

    @pytest.mark.parametrize(
        "content",
        ["", "x" * 50_000, "x" * 50_001, "\x00" * 50_001, "🙂e\u0301" * 20_000],
        ids=["empty", "ascii-cap", "ascii-over", "escaped-ascii", "non-bmp-combining"],
    )
    def test_pages_reconstruct_exact_unicode_content_with_verified_identity(self, content: str) -> None:
        import json

        from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
        from elspeth.web.composer.prompt_guard import _request
        from elspeth.web.composer.prompt_guard_rendering import json_content_parts

        digest = self._replace_content(content)
        offset = 0
        chunks = []
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            for _ in range(256):
                arguments = {"blob_id": self.blob_id, "offset_chars": offset, "expected_content_hash": digest}
                result = execute_tool(
                    "get_blob_content",
                    arguments,
                    _empty_state(),
                    _mock_catalog(),
                    data_dir=str(self.data_dir),
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=authority,
                    session_operation_context=context,
                )
                assert result.success, result.validation.errors
                assert result.data["content_hash"] == digest
                assert result.data["size_bytes"] == len(content.encode("utf-8"))
                assert result.data["total_chars"] == len(content)
                assert result.data["start_char"] == offset
                end = result.data["end_char"]
                assert result.data["content"] == content[offset:end]
                assert end - offset <= 50_000
                assert result.data["truncated"] is (end < len(content))
                serialized = serialize_admitted_discovery_result(admit_discovery_result("get_blob_content", result))
                assert len(serialized.encode("utf-8")) <= 4_096
                assert json.loads(serialized)["data"]["content"] == content[offset:end]
                guard_request, supported = _request(
                    policy_fingerprint="a" * 64,
                    role="composer",
                    messages=[{"role": "tool", "content": serialized, "tool_call_id": "inspect-page"}],
                    tools=(),
                    user_prompt="Inspect the uploaded file.",
                    trusted_message_indexes=frozenset(),
                    content_parts=(json_content_parts(serialized),),
                )
                assert supported
                assert all(len(document) <= 10_000 for document in guard_request.documents)
                chunks.append(result.data["content"])
                if result.data["next_offset_chars"] is None:
                    assert end == len(content)
                    break
                assert result.data["next_offset_chars"] == end > offset
                offset = end
            else:
                pytest.fail("Continuation failed to advance within the synthetic page bound")
        assert "".join(chunks) == content

    @pytest.mark.parametrize("offset", [0, 2, 4, 5])
    def test_explicit_offsets_and_eof(self, offset: int) -> None:
        content = "a🙂e\u0301"
        digest = self._replace_content(content)
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id, "offset_chars": offset, "expected_content_hash": digest},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=context,
            )
        assert result.success is (offset <= len(content))
        if result.success:
            assert result.data["content"] == content[offset:]
            assert result.data["start_char"] == offset
            assert result.data["end_char"] == len(content)
            assert result.data["next_offset_chars"] is None

    @pytest.mark.parametrize("offset", [0, 1])
    def test_expected_identity_mismatch_discloses_no_content(self, offset: int) -> None:
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id, "offset_chars": offset, "expected_content_hash": "0" * 64},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=context,
            )
        assert not result.success
        assert result.data is None

    def test_replaced_blob_cannot_continue_the_previous_verified_series(self) -> None:
        from elspeth.web.composer.tools import blobs
        from tests.unit.web.composer.test_set_pipeline_candidate import _trained_context

        digest = self._replace_content("before")
        with _blob_operation(self.engine, self.session_id) as (authority, operation):
            context = _trained_context(
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=operation,
            )
            initial = blobs._execute_get_blob_content({"blob_id": self.blob_id}, _empty_state(), context)
        assert initial.success
        assert initial.data["content_hash"] == digest
        replacement_digest = self._replace_content("after")
        with _blob_operation(self.engine, self.session_id) as (authority, operation):
            context = _trained_context(
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=operation,
            )
            stale = blobs._execute_get_blob_content(
                {"blob_id": self.blob_id, "offset_chars": 1, "expected_content_hash": digest},
                _empty_state(),
                context,
            )
            fresh = blobs._execute_get_blob_content(
                {"blob_id": self.blob_id, "offset_chars": 1, "expected_content_hash": replacement_digest},
                _empty_state(),
                context,
            )
        assert not stale.success
        assert stale.data is None
        assert fresh.success
        assert fresh.data["content"] == "fter"

    def test_credential_after_page_boundary_refuses_the_first_page(self) -> None:
        content = "x" * 49_996 + "\nsk-" + "A" * 48
        self._replace_content(content)
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=context,
            )
        assert not result.success
        assert result.data is None
        assert result.validation.errors[0].error_code == "credential_material_rejected"

    def test_escaped_page_must_fit_complete_provider_envelope(self) -> None:
        from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result

        self._replace_content("\x00" * 50_000)
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=context,
            )
        assert result.success
        encoded = serialize_admitted_discovery_result(admit_discovery_result("get_blob_content", result))
        assert len(encoded.encode("utf-8")) <= 4_096

    @pytest.mark.parametrize("position", ["across", "after"])
    def test_credentials_across_or_after_actual_page_boundary_refuse_without_prefix(self, position: str) -> None:
        self._replace_content("x" * 50_001)
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            initial = execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=context,
            )
        assert initial.success and initial.data["truncated"]
        boundary = initial.data["end_char"]
        offset = boundary - 2 if position == "across" else boundary + 20
        self._replace_content("x" * offset + "\nsk-" + "A" * 48)
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            refused = execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=context,
            )
        assert not refused.success and refused.data is None
        assert refused.validation.errors[0].error_code == "credential_material_rejected"

    def test_pending_blob_refused(self) -> None:
        """Status guard — pending blobs may be partial writes."""
        self._set_status("pending")
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert result.success is False
        assert "pending" in result.validation.errors[0].message.lower() or "not readable" in result.validation.errors[0].message.lower()

    def test_error_blob_refused(self) -> None:
        """Status guard — error blobs belong to failed runs and are not trustworthy."""
        # The blobs CHECK constraint disallows reading ready→error without a hash,
        # but error is a valid status value; flip it directly.
        self._set_status("error")
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert result.success is False
        assert "error" in result.validation.errors[0].message.lower() or "not readable" in result.validation.errors[0].message.lower()

    def test_hash_mismatch_raises_blob_integrity_error(self) -> None:
        """Integrity guard — corruption/tampering must ESCALATE, not return failure.

        Tier-1 policy: a mismatch between on-disk bytes and stored
        content_hash is a Tier-1 anomaly (our hash, our file — a
        mismatch means corruption, tampering, or a write-path bug).
        Downgrading to a tool-failure result tells the LLM "retry",
        masking a live audit-integrity event.
        """
        from elspeth.web.blobs.protocol import BlobIntegrityError

        # Mutate the on-disk bytes without touching the DB — simulates
        # filesystem corruption / tampering.
        self.storage_path.write_bytes(b"col_a,col_b\n9,9\n9,9\n")

        state = _empty_state()
        catalog = _mock_catalog()
        with (
            pytest.raises(BlobIntegrityError) as exc_info,
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
        ):
            execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert exc_info.value.blob_id == self.blob_id

    def test_null_content_hash_on_ready_blob_raises_audit_integrity_error(self) -> None:
        """A ready blob with NULL content_hash is a DB-integrity anomaly.

        The blobs table has CHECK constraints forbidding this state;
        reaching it means the invariant was breached out-of-band.
        Must escalate (Tier-1), not return a tool-failure.
        """
        from sqlalchemy import text

        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.web.sessions.models import blobs_table

        # Bypass the CHECK constraint by dropping then re-inserting via
        # raw SQL — the test exercises the defensive read guard, not
        # the write-side invariant.  Use PRAGMA to disable the
        # constraint temporarily.

        with self.engine.begin() as conn:
            conn.execute(text("PRAGMA ignore_check_constraints = 1"))
            conn.execute(blobs_table.update().where(blobs_table.c.id == self.blob_id).values(content_hash=None))
            conn.execute(text("PRAGMA ignore_check_constraints = 0"))

        state = _empty_state()
        catalog = _mock_catalog()
        with (
            pytest.raises(AuditIntegrityError, match="NULL content_hash"),
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
        ):
            execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

    def test_non_utf8_bytes_return_failure_not_crash(self) -> None:
        """Decode safety — UnicodeDecodeError must not escape the tool.

        The MIME allowlist admits ``text/csv``, ``text/plain`` etc.
        without constraining encoding.  A latin-1 CSV (common in the
        wild) raises UnicodeDecodeError on ``read_text(encoding='utf-8')``;
        without a decode guard this crashes the tool dispatcher with
        an unhandled exception.  The correct behaviour is a structured
        failure so the compose loop can surface a helpful message.
        """
        from elspeth.web.blobs.service import content_hash as _content_hash

        # Bytes that are valid latin-1 but invalid UTF-8 (0xFE is an
        # invalid leading byte in UTF-8).
        non_utf8_bytes = b"na\xefve,col_b\n1,2\n"
        self.storage_path.write_bytes(non_utf8_bytes)

        # Update the DB hash so integrity check passes — the test
        # targets the decode step, not the integrity step.
        from elspeth.web.sessions.models import blobs_table

        with self.engine.begin() as conn:
            conn.execute(
                blobs_table.update()
                .where(blobs_table.c.id == self.blob_id)
                .values(
                    size_bytes=len(non_utf8_bytes),
                    content_hash=_content_hash(non_utf8_bytes),
                )
            )

        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert result.success is False
        assert "utf-8" in result.validation.errors[0].message.lower()


class TestGetBlobContentOrigin:
    """The read-back result must name who authored the bytes.

    ``created_by`` and ``creation_modality`` are read as a PAIR, and each
    test below pins one origin class that the pair separates but that either
    field alone conflates:

    * ``created_by`` alone cannot tell composer-generated bytes from bytes
      the composer copied verbatim out of the user's own message — both are
      written ``created_by="assistant"``.
    * ``creation_modality`` alone cannot tell an operator upload from a
      pipeline run artifact from that verbatim copy — all three are stored
      ``verbatim``.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        self.data_dir = str(tmp_path)
        self.engine, self.session_id = _session_engine_with_session()

    def _insert_blob_row(self, *, created_by: str, content: bytes) -> str:
        """Insert one ready blob row directly, as a non-composer writer would.

        The upload route and run-output capture both persist through
        ``BlobServiceImpl``, which is async and engine-bound; inserting the
        row is the synchronous equivalent of what they commit, including
        leaving ``creation_modality`` to the column's ``verbatim`` default.
        """
        from datetime import UTC, datetime

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        blob_id = str(uuid4())
        storage_dir = Path(self.data_dir) / "blobs" / self.session_id
        storage_dir.mkdir(parents=True, exist_ok=True)
        storage_path = storage_dir / f"{blob_id}_data.csv"
        storage_path.write_bytes(content)
        with self.engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(content),
                    content_hash=_content_hash(content),
                    storage_path=str(storage_path),
                    created_at=datetime.now(UTC),
                    created_by=created_by,
                    source_description=None,
                    status="ready",
                )
            )
        return blob_id

    def _read_back(
        self,
        blob_id: str,
        *,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_operation_context: SessionOperationContext,
    ) -> Any:
        result = execute_tool(
            "get_blob_content",
            {"blob_id": blob_id},
            _empty_state(),
            _mock_catalog(),
            data_dir=str(self.data_dir),
            session_engine=self.engine,
            session_id=self.session_id,
            session_operation_authority=session_operation_authority,
            session_operation_context=session_operation_context,
        )
        assert result.success is True
        return result.data

    def test_llm_authored_blob_reads_back_as_assistant_generated(self) -> None:
        """The 891b7b1e path: fabricate, write, read back — origin survives.

        Also pins the exact key set, so a future field added to the handler
        payload must be declared on ``BlobContentPayload`` and on
        ``GetBlobContentDataModel`` rather than smuggled through.
        """
        invented = "complaint_id,text\n1,late delivery\n2,wrong item\n"
        user_message_content = "Make up a couple of sample complaints for me."
        user_message_id = _insert_user_message(self.engine, self.session_id, user_message_content)

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            created = execute_tool(
                "create_blob",
                {"filename": "complaints.csv", "mime_type": "text/csv", "content": invented},
                _empty_state(),
                _mock_catalog(),
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                user_message_id=user_message_id,
                user_message_content=user_message_content,
                composer_model_identifier="openai/gpt-5-mini",
                composer_model_version="gpt-5-mini-2026-05-01",
                composer_provider="openai",
                composer_skill_hash="a" * 64,
                tool_arguments_hash="b" * 64,
            )
        assert created.success is True

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            data = self._read_back(
                created.data["blob_id"],
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

        assert data["content"] == invented
        assert data["created_by"] == "assistant"
        assert data["creation_modality"] == CreationModality.LLM_GENERATED.value
        assert set(data) == {
            "blob_id",
            "filename",
            "mime_type",
            "content",
            "truncated",
            "size_bytes",
            "created_by",
            "creation_modality",
            "content_hash",
            "total_chars",
            "start_char",
            "end_char",
            "next_offset_chars",
        }

    def test_composer_written_user_content_is_not_reported_as_assistant_authored(self) -> None:
        """created_by alone would misattribute the user's own words.

        The composer writes ``created_by="assistant"`` whenever it makes the
        create_blob call — including for content copied verbatim out of the
        user's message.  Only the modality separates that from invention, so
        a planner reading this back must not claim it generated the rows.
        """
        dictated = "sku,on_hand\nAX-100,12\n"

        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, dictated)
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            created = execute_tool(
                "create_blob",
                {"filename": "stock.csv", "mime_type": "text/csv", "content": dictated},
                _empty_state(),
                _mock_catalog(),
                data_dir=self.data_dir,
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
        assert created.success is True

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            data = self._read_back(
                created.data["blob_id"],
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

        assert data["created_by"] == "assistant"
        assert data["creation_modality"] == CreationModality.VERBATIM.value

    def test_uploaded_blob_reads_back_as_user_supplied(self) -> None:
        """An operator upload: the discovery case the defect was mimicking."""
        blob_id = self._insert_blob_row(created_by="user", content=b"col_a,col_b\n1,2\n")

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            data = self._read_back(
                blob_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

        assert data["created_by"] == "user"
        assert data["creation_modality"] == CreationModality.VERBATIM.value

    def test_pipeline_run_output_blob_reads_back_as_pipeline_authored(self) -> None:
        """creation_modality alone would read a run artifact as user-supplied.

        Run-output capture stores ``created_by="pipeline"`` and leaves the
        modality at its ``verbatim`` default, which is byte-identical to what
        an operator upload stores.  ``created_by`` is the only field that
        keeps the two apart — the reason the origin is surfaced as a pair.
        """
        blob_id = self._insert_blob_row(created_by="pipeline", content=b"row_id,score\n1,0.9\n")

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            data = self._read_back(
                blob_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

        assert data["created_by"] == "pipeline"
        assert data["creation_modality"] == CreationModality.VERBATIM.value

    def test_list_blobs_reports_the_same_origin_depth_as_the_read_path(self) -> None:
        """Parity: a planner must not get shallower provenance from the survey."""
        blob_id = self._insert_blob_row(created_by="pipeline", content=b"row_id,score\n1,0.9\n")

        listed = execute_tool(
            "list_blobs",
            {},
            _empty_state(),
            _mock_catalog(),
            session_engine=self.engine,
            session_id=self.session_id,
        )
        assert listed.success is True
        entry = next(blob for blob in listed.data if blob["id"] == blob_id)

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            read_back = self._read_back(
                blob_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert entry["created_by"] == read_back["created_by"]
        assert entry["creation_modality"] == read_back["creation_modality"]


class TestUpdateBlobActiveRunGuard:
    """update_blob must refuse to mutate blobs referenced by active runs.

    Two corruption modes without the guard:

    * Path-based sources: the pipeline reads the new bytes but records
      them under the old content_hash — silent Tier-1 audit corruption.
    * blob_ref sources: a mid-run BlobIntegrityError fires as a
      false-positive tamper event because the recomputed hash no
      longer matches the stored hash.

    Both modes are closed by refusing the update while any active
    (pending/running) run in the blob's session references the blob.
    Mirrors the pattern in _execute_delete_blob so the two mutating
    tools enforce the same invariant.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.data_dir = tmp_path
        self.session_id = str(uuid4())
        self.blob_id = str(uuid4())
        self.run_id = str(uuid4())
        now = datetime.now(UTC)

        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_data.csv"
        self.original_content = b"a,b\n1,2"
        self.storage_path.write_bytes(self.original_content)
        self.original_hash = content_hash(self.original_content)

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(self.original_content),
                    content_hash=self.original_hash,
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def _insert_run_and_link(self, status: str) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from elspeth.web.sessions.models import (
            blob_run_links_table,
            composition_states_table,
            runs_table,
        )

        now = datetime.now(UTC)
        state_id = str(uuid4())
        with self.engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=self.session_id,
                    version=1,
                    source=None,
                    nodes=None,
                    edges=None,
                    outputs=None,
                    metadata_=None,
                    is_valid=False,
                    validation_errors=None,
                    # Plan §2294: composer-tools test fixture; provenance
                    # required for setup row supporting subsequent runs/
                    # blob_run_links FKs.
                    provenance="session_seed",
                    created_at=now,
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=self.run_id,
                    session_id=self.session_id,
                    state_id=state_id,
                    status=status,
                    started_at=now,
                    rows_processed=0,
                    rows_failed=0,
                )
            )
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=self.blob_id,
                    run_id=self.run_id,
                    direction="input",
                )
            )

    def _insert_run_without_link(self, status: str, *, source: dict[str, Any] | None = None) -> None:
        """Simulate the pre-link window (run exists, blob_run_links row not yet inserted)."""
        from datetime import UTC, datetime
        from uuid import uuid4

        from elspeth.web.sessions.models import (
            composition_states_table,
            runs_table,
        )

        if source is None:
            source = {
                "plugin": "csv",
                "on_success": "output",
                "on_validation_failure": "quarantine",
                "options": {"blob_ref": self.blob_id, "path": str(self.storage_path)},
            }
        else:
            source = {
                "on_success": "output",
                "on_validation_failure": "quarantine",
                **source,
            }

        now = datetime.now(UTC)
        state_id = str(uuid4())
        with self.engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=self.session_id,
                    version=1,
                    # Production shape, not the pre-2026-05 one. Since
                    # f0fd36087 every writer folds `source` into
                    # `sources={"source": ...}` and wraps each JSON column
                    # through the one envelope rule, so the active-run guard
                    # this fixture pins must be exercised against that shape --
                    # seeding a populated, bare `source` is what let
                    # elspeth-3db5745ba7 stay green while production 500'd.
                    source=None,
                    sources=envelope_state_column({"source": source}),
                    nodes=envelope_state_column([]),
                    edges=envelope_state_column([]),
                    outputs=envelope_state_column([]),
                    metadata_=envelope_state_column({"name": "Test", "description": ""}),
                    is_valid=False,
                    validation_errors=None,
                    # Plan §2294: composer-tools test fixture; provenance
                    # required for setup row supporting subsequent runs/
                    # blob_run_links FKs.
                    provenance="session_seed",
                    created_at=now,
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=self.run_id,
                    session_id=self.session_id,
                    state_id=state_id,
                    status=status,
                    started_at=now,
                    rows_processed=0,
                    rows_failed=0,
                )
            )

    def test_update_succeeds_when_no_runs_exist(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "new,content\n9,9")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "new,content\n9,9"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
        assert result.success is True
        assert self.storage_path.read_bytes() == b"new,content\n9,9"

    def test_update_rejected_while_session_fork_is_in_progress(self) -> None:
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "new,content\n9,9")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            operation_id = _insert_session_fork_operation(self.engine, self.session_id, status="in_progress")
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "new,content\n9,9"},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )

        assert result.success is False
        assert operation_id in result.validation.errors[0].message
        assert "session fork" in result.validation.errors[0].message.lower()
        assert self.storage_path.read_bytes() == self.original_content

    @pytest.mark.parametrize("fork_status", ["completed", "failed"])
    def test_update_allowed_after_session_fork_is_terminal(self, fork_status: str) -> None:
        _insert_session_fork_operation(self.engine, self.session_id, status=fork_status)

        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "new,content\n9,9")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "new,content\n9,9"},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )

        assert result.success is True
        assert self.storage_path.read_bytes() == b"new,content\n9,9"

    def test_update_without_authoring_context_fails_closed(self) -> None:
        from elspeth.contracts.errors import AuditIntegrityError

        state = _empty_state()
        catalog = _mock_catalog()

        with (
            pytest.raises(AuditIntegrityError, match="missing: user_message_id"),
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
        ):
            execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "new,content\n9,9"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

        assert self.storage_path.read_bytes() == self.original_content
        with self.engine.begin() as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == self.blob_id)).one()
        assert row.content_hash == self.original_hash

    def test_update_rejected_when_blob_is_current_source_blob_ref(self) -> None:
        """A blob-backed source locks the blob content hash stamped in source_authoring."""
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="output",
                options={
                    "blob_ref": self.blob_id,
                    "path": str(self.storage_path),
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="quarantine",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        catalog = _mock_catalog()

        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "new,content\n9,9")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "new,content\n9,9"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )

        assert result.success is False
        assert "referenced by the current composition" in result.validation.errors[0].message
        assert self.storage_path.read_bytes() == self.original_content
        with self.engine.begin() as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == self.blob_id)).one()
        assert row.content_hash == self.original_hash

    def test_unbound_update_recomputes_composer_provenance(self) -> None:
        """Unbound blob updates that author new bytes refresh blob provenance."""
        from elspeth.web.blobs.service import content_hash

        state = _empty_state()
        catalog = _mock_catalog()
        user_message_content = "Generate a replacement CSV for the current scratch blob."
        user_message_id = _insert_user_message(self.engine, self.session_id, user_message_content)
        new_content = "name,score\nada,42\n"

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": new_content},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                user_message_id=user_message_id,
                user_message_content=user_message_content,
                composer_model_identifier="openai/gpt-5-mini",
                composer_model_version="gpt-5-mini-2026-05-01",
                composer_provider="openai",
                composer_skill_hash="a" * 64,
                tool_arguments_hash="b" * 64,
            )

        assert result.success is True
        assert self.storage_path.read_bytes() == new_content.encode("utf-8")
        with self.engine.begin() as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == self.blob_id)).one()
        assert row.content_hash == content_hash(new_content.encode("utf-8"))
        assert row.creation_modality == CreationModality.LLM_GENERATED.value
        assert row.created_from_message_id == user_message_id
        assert row.creating_model_identifier == "openai/gpt-5-mini"
        assert row.creating_model_version == "gpt-5-mini-2026-05-01"
        assert row.creating_provider == "openai"
        assert row.creating_composer_skill_hash == "a" * 64
        assert row.creating_arguments_hash == "b" * 64

    def test_update_rejected_when_pending_run_linked(self) -> None:
        self._insert_run_and_link("pending")
        state = _empty_state()
        catalog = _mock_catalog()
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "new")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "new"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
        assert result.success is False
        assert "active run" in result.validation.errors[0].message.lower()
        assert self.storage_path.read_bytes() == self.original_content, "File must not change when the active-run guard blocks the update"

    def test_update_rejected_when_running_run_linked(self) -> None:
        self._insert_run_and_link("running")
        state = _empty_state()
        catalog = _mock_catalog()
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "new")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "new"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
        assert result.success is False
        assert "active run" in result.validation.errors[0].message.lower()
        assert self.storage_path.read_bytes() == self.original_content

    def test_update_succeeds_when_completed_run_linked(self) -> None:
        """Completed runs have released the blob — update must proceed."""
        self._insert_run_and_link("completed")
        state = _empty_state()
        catalog = _mock_catalog()
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "post,run\n1,1")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "post,run\n1,1"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
        assert result.success is True
        assert self.storage_path.read_bytes() == b"post,run\n1,1"

    def test_update_rejected_pre_link_window_blob_ref_source(self) -> None:
        """Pre-link window: run exists, blob_run_links not yet inserted, source uses blob_ref."""
        self._insert_run_without_link("pending")
        state = _empty_state()
        catalog = _mock_catalog()
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "new")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "new"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
        assert result.success is False
        assert "active run" in result.validation.errors[0].message.lower()
        assert self.storage_path.read_bytes() == self.original_content

    def test_update_rejected_pre_link_window_path_source(self) -> None:
        """Pre-link window: run exists with source.path matching storage_path (no blob_ref)."""
        self._insert_run_without_link(
            "running",
            source={"plugin": "csv", "options": {"path": str(self.storage_path)}},
        )
        state = _empty_state()
        catalog = _mock_catalog()
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "new")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "new"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
        assert result.success is False
        assert "active run" in result.validation.errors[0].message.lower()
        assert self.storage_path.read_bytes() == self.original_content

    def test_update_succeeds_when_active_run_references_different_source(self) -> None:
        """Unrelated active run (different source) must NOT block update — scoped guard."""
        self._insert_run_without_link(
            "pending",
            source={"plugin": "csv", "options": {"path": "/data/external/unrelated.csv"}},
        )
        state = _empty_state()
        catalog = _mock_catalog()
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "should,proceed\n1,1")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "should,proceed\n1,1"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
        assert result.success is True
        assert self.storage_path.read_bytes() == b"should,proceed\n1,1"


class TestUpdateBlobAtomicWrite:
    """update_blob must not modify storage_path until DB guards have passed.

    Before the fix, _execute_update_blob called ``write_bytes`` before
    ``session_engine.begin()`` — so any subsequent guard failure (active
    run, quota) forced a rollback-write, and any concurrent reader saw
    new bytes against the stale DB hash in the intervening window.

    The fix serialises the file swap to AFTER guard + quota + UPDATE,
    via ``os.replace(tmp, storage_path)`` inside the transaction.  These
    tests pin that ordering by asserting the storage file is unchanged
    on every guard-rejection path and by exercising a simulated DB
    failure to confirm no orphaned tempfiles remain.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.data_dir = tmp_path
        self.session_id = str(uuid4())
        self.blob_id = str(uuid4())
        self.storage_dir = tmp_path / "blobs" / self.session_id
        self.storage_dir.mkdir(parents=True)
        self.storage_path = self.storage_dir / f"{self.blob_id}_data.csv"
        self.original_content = b"ORIGINAL-BYTES"
        self.storage_path.write_bytes(self.original_content)
        self.original_hash = content_hash(self.original_content)

        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(self.original_content),
                    content_hash=self.original_hash,
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def test_guard_rejection_leaves_storage_untouched_and_no_tempfile(self) -> None:
        """When active-run guard fires, storage_path bytes are unchanged and no tempfile leaks."""
        from datetime import UTC, datetime
        from uuid import uuid4

        from elspeth.web.sessions.models import (
            blob_run_links_table,
            composition_states_table,
            runs_table,
        )

        # Insert a pending run linked to our blob to force the guard.
        now = datetime.now(UTC)
        run_id = str(uuid4())
        state_id = str(uuid4())
        with self.engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=self.session_id,
                    version=1,
                    source=None,
                    nodes=None,
                    edges=None,
                    outputs=None,
                    metadata_=None,
                    is_valid=False,
                    validation_errors=None,
                    # Plan §2294: composer-tools test fixture; provenance
                    # required for setup row supporting subsequent runs/
                    # blob_run_links FKs.
                    provenance="session_seed",
                    created_at=now,
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=self.session_id,
                    state_id=state_id,
                    status="pending",
                    started_at=now,
                    rows_processed=0,
                    rows_failed=0,
                )
            )
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=self.blob_id,
                    run_id=run_id,
                    direction="input",
                )
            )

        state = _empty_state()
        catalog = _mock_catalog()
        blob_provenance = _verbatim_blob_context(self.engine, self.session_id, "would,corrupt,mid-run\n")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "would,corrupt,mid-run\n"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )
        assert result.success is False
        assert self.storage_path.read_bytes() == self.original_content

        # No sibling tempfiles must remain (stale tempfile accumulation
        # would exhaust inodes and leak uncommitted content).
        leftovers = [p for p in self.storage_dir.iterdir() if p != self.storage_path]
        assert leftovers == [], f"Tempfiles leaked after guard rejection: {leftovers}"

    def test_db_failure_leaves_storage_untouched_and_no_tempfile(self) -> None:
        """Simulated DB failure: storage unchanged, no tempfiles remain.

        After the fix, the file is not written to storage_path until
        ``os.replace`` runs inside the transaction — so a DB failure
        that happens before ``os.replace`` leaves the original bytes
        intact by construction (no rollback-write needed).  The
        tempfile cleanup runs unconditionally in a finally block.
        """
        from unittest.mock import patch

        state = _empty_state()
        catalog = _mock_catalog()
        provenance_context = _verbatim_blob_context(self.engine, self.session_id, "new")

        # Force a DB failure by making begin() raise.  This fires
        # BEFORE any UPDATE / os.replace, so no file mutation can
        # have occurred.
        with (
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
            patch.object(
                self.engine,
                "begin",
                side_effect=RuntimeError("simulated DB failure"),
            ),
            pytest.raises(RuntimeError, match="simulated DB failure"),
        ):
            execute_tool(
                "update_blob",
                {"blob_id": self.blob_id, "content": "new"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **provenance_context,
            )

        assert self.storage_path.read_bytes() == self.original_content
        leftovers = [p for p in self.storage_dir.iterdir() if p != self.storage_path]
        assert leftovers == [], f"Tempfiles leaked after DB failure: {leftovers}"


class TestGetBlobContentReaderMixedVersion:
    """Readers must never pair one blob version's row with another's bytes.

    ``_execute_update_blob`` swaps the storage file (``os.replace``) before
    committing the new metadata. A reader that fetches the blob row
    and the storage bytes without entering the same-session custody lock can
    therefore pair the old committed row (old content_hash) with the new
    bytes — escalating a false-positive ``BlobIntegrityError`` for a blob
    that was never corrupted (elspeth-3d1d1fcb6c).
    """

    def test_reader_between_swap_and_commit_sees_one_complete_version(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import hashlib
        import os as stdlib_os
        import threading as stdlib_threading
        import time
        from datetime import UTC, datetime
        from uuid import uuid4

        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
        initialize_session_schema(engine)
        with engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        session_id = str(uuid4())
        blob_id = str(uuid4())
        now = datetime.now(UTC)
        storage_dir = tmp_path / "blobs" / session_id
        storage_dir.mkdir(parents=True)
        storage_path = storage_dir / f"{blob_id}_data.csv"
        old_content = b"old-content"
        storage_path.write_bytes(old_content)

        with engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Reader serialisation",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=blob_id,
                    session_id=session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(old_content),
                    content_hash=hashlib.sha256(old_content).hexdigest(),
                    storage_path=str(storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

        update_context = _verbatim_blob_context(engine, session_id, "new-content")

        with _blob_operation(engine, session_id) as (authority, context):
            swapped = stdlib_threading.Event()
            release_update = stdlib_threading.Event()
            real_replace = stdlib_os.replace

            def pausing_replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
                real_replace(src, dst, *args, **kwargs)
                if Path(dst).name == storage_path.name and Path(src).name.endswith(".stage"):
                    # The storage file now holds the NEW bytes while the update
                    # transaction (old row) has not committed yet.  Hold the
                    # window open so the reader thread can attempt its read.
                    swapped.set()
                    if not release_update.wait(timeout=10):
                        raise TimeoutError("update thread was never released from the swap window")

            monkeypatch.setattr("os.replace", pausing_replace)

            update_results: list[Any] = []
            update_failures: list[BaseException] = []

            def updater() -> None:
                try:
                    update_results.append(
                        execute_tool(
                            "update_blob",
                            {"blob_id": blob_id, "content": "new-content"},
                            _empty_state(),
                            _mock_catalog(),
                            session_engine=engine,
                            session_id=session_id,
                            data_dir=str(tmp_path),
                            session_operation_authority=authority,
                            session_operation_context=context,
                            **update_context,
                        )
                    )
                except BaseException as exc:  # pragma: no cover - failure diagnostics
                    update_failures.append(exc)

            reader_results: list[Any] = []
            reader_failures: list[BaseException] = []

            def reader() -> None:
                try:
                    reader_results.append(
                        execute_tool(
                            "get_blob_content",
                            {"blob_id": blob_id},
                            _empty_state(),
                            _mock_catalog(),
                            session_engine=engine,
                            session_id=session_id,
                            data_dir=str(tmp_path),
                            session_operation_authority=authority,
                            session_operation_context=context,
                        )
                    )
                except BaseException as exc:
                    reader_failures.append(exc)

            update_thread = stdlib_threading.Thread(target=updater, name="blob-updater", daemon=True)
            update_thread.start()
            assert swapped.wait(timeout=5), "update never reached its file swap"

            reader_thread = stdlib_threading.Thread(target=reader, name="blob-reader", daemon=True)
            reader_thread.start()
            # Give the reader time to attempt its read inside the swap window.
            # A correctly-serialised reader blocks on the session custody lock
            # here; an unserialised reader pairs the old row with the new bytes.
            time.sleep(0.5)
            release_update.set()
            update_thread.join(timeout=10)
            reader_thread.join(timeout=10)
            assert not update_thread.is_alive() and not reader_thread.is_alive()

            assert update_failures == [], f"update_blob raised: {update_failures}"
            assert update_results and update_results[0].success is True
            assert reader_failures == [], (
                f"get_blob_content escalated during the update swap/commit window: {reader_failures}; "
                "the reader paired one version's row with another version's bytes"
            )
            assert reader_results and reader_results[0].success is True
            assert reader_results[0].data["content"] == "new-content"


class TestBlobCrashStateReconciliation:
    """Crash leftovers must reconcile to ONE authoritative blob version.

    ``_execute_update_blob`` preserves the prior bytes at a deterministic
    ``.{name}.pre-update`` sidecar across its swap+commit window, and
    ``_execute_delete_blob`` stages bytes at a ``.{blob_id}.delete-<hex>``
    tombstone before its DELETE commits.  A crash inside either window
    leaves the filesystem one rename away from the committed row state;
    the committed ``content_hash`` is the arbiter.  Readers reconcile
    under the custody lock instead of escalating a false
    ``BlobIntegrityError`` / missing-file failure (elspeth-3d1d1fcb6c).
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        import hashlib
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.session_id = str(uuid4())
        self.blob_id = str(uuid4())
        self.data_dir = str(tmp_path)
        now = datetime.now(UTC)

        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_data.csv"
        self.old_content = b"authoritative-old"
        self.old_hash = hashlib.sha256(self.old_content).hexdigest()
        self.new_content = b"uncommitted-new"
        self.storage_path.write_bytes(self.old_content)
        self.sidecar_path = self.storage_path.with_name(f".{self.storage_path.name}.pre-update")

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Crash reconcile",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(self.old_content),
                    content_hash=self.old_hash,
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def _read_content(
        self,
        authority: SQLiteLocalSessionOperationAuthority,
        context: SessionOperationContext,
    ) -> Any:
        return execute_tool(
            "get_blob_content",
            {"blob_id": self.blob_id},
            _empty_state(),
            _mock_catalog(),
            session_engine=self.engine,
            session_id=self.session_id,
            data_dir=str(self.data_dir),
            session_operation_authority=authority,
            session_operation_context=context,
        )

    def test_update_crash_before_commit_restores_sidecar_version(self) -> None:
        """storage=new bytes, sidecar=old bytes, row=old hash → old wins."""
        self.storage_path.write_bytes(self.new_content)
        self.sidecar_path.write_bytes(self.old_content)

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = self._read_content(authority, context)

        assert result.success is True, f"reader escalated on a recoverable crash state: {result.data}"
        assert result.data["content"] == self.old_content.decode()
        assert self.storage_path.read_bytes() == self.old_content
        assert not self.sidecar_path.exists(), "sidecar journal must be consumed by reconciliation"

    def test_update_crash_between_renames_restores_sidecar_version(self) -> None:
        """storage missing, sidecar=old bytes, row=old hash → old restored."""
        self.storage_path.unlink()
        self.sidecar_path.write_bytes(self.old_content)

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = self._read_content(authority, context)

        assert result.success is True, f"reader escalated on a recoverable crash state: {result.data}"
        assert result.data["content"] == self.old_content.decode()
        assert self.storage_path.read_bytes() == self.old_content
        assert not self.sidecar_path.exists()

    def test_update_crash_after_commit_purges_stale_sidecar(self) -> None:
        """storage matches the committed row → stale sidecar is purged."""
        import hashlib

        committed = b"committed-new"
        self.storage_path.write_bytes(committed)
        self.sidecar_path.write_bytes(self.old_content)
        with self.engine.begin() as conn:
            from elspeth.web.sessions.models import blobs_table

            conn.execute(
                blobs_table.update()
                .where(blobs_table.c.id == self.blob_id)
                .values(content_hash=hashlib.sha256(committed).hexdigest(), size_bytes=len(committed))
            )

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = self._read_content(authority, context)

        assert result.success is True, f"reader escalated on a recoverable crash state: {result.data}"
        assert result.data["content"] == committed.decode()
        assert self.storage_path.read_bytes() == committed
        assert not self.sidecar_path.exists(), "stale sidecar must be purged once the committed bytes verify"

    def test_delete_crash_before_commit_restores_tombstone_version(self) -> None:
        """row live, storage missing, one matching tombstone → restored."""
        tombstone = self.storage_path.with_name(f".{self.blob_id}.delete-{'0' * 32}")
        self.storage_path.rename(tombstone)

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = self._read_content(authority, context)

        assert result.success is True, f"reader escalated on a recoverable crash state: {result.data}"
        assert result.data["content"] == self.old_content.decode()
        assert self.storage_path.read_bytes() == self.old_content
        assert not tombstone.exists(), "delete tombstone must be consumed by reconciliation"

    @pytest.mark.parametrize("artifact_kind", ["inline_stage", "sidecar", "tombstone"])
    def test_crash_reconciliation_hashes_every_candidate_in_bounded_chunks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        artifact_kind: str,
    ) -> None:
        """Crash recovery must not materialize any candidate file in memory."""
        import hashlib
        import os

        from elspeth.web.blobs import service as blob_service_module

        monkeypatch.setattr(blob_service_module, "_STREAM_CHUNK_BYTES", 4096)
        authoritative = bytes(range(256)) * 80
        expected_hash = hashlib.sha256(authoritative).hexdigest()
        with self.engine.begin() as conn:
            from elspeth.web.sessions.models import blobs_table

            conn.execute(
                blobs_table.update()
                .where(blobs_table.c.id == self.blob_id)
                .values(content_hash=expected_hash, size_bytes=len(authoritative))
            )

        candidates: tuple[Path, ...]
        if artifact_kind == "inline_stage":
            self.storage_path.unlink()
            inline_stage = blob_service_module.inline_custody_staging_path(self.storage_path)
            inline_stage.write_bytes(authoritative)
            candidates = (inline_stage,)
        elif artifact_kind == "sidecar":
            self.storage_path.write_bytes(b"x" * len(authoritative))
            self.sidecar_path.write_bytes(authoritative)
            candidates = (self.storage_path, self.sidecar_path)
        else:
            self.storage_path.unlink()
            tombstone = self.storage_path.with_name(f".{self.blob_id}.delete-{'0' * 32}")
            tombstone.write_bytes(authoritative)
            candidates = (tombstone,)

        requested_sizes: dict[Path, list[int]] = {}
        real_read = blob_service_module.os.read

        def spy_read(descriptor: int, size: int) -> bytes:
            opened_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if opened_path in candidates:
                if opened_path not in requested_sizes:
                    requested_sizes[opened_path] = []
                requested_sizes[opened_path].append(size)
            return real_read(descriptor, size)

        monkeypatch.setattr(blob_service_module.os, "read", spy_read)

        blob_service_module.reconcile_blob_storage_versions(
            self.storage_path,
            expected_hash=expected_hash,
        )

        assert set(requested_sizes) == set(candidates)
        for sizes in requested_sizes.values():
            assert len(sizes) > 1
            assert all(0 < size <= 4096 for size in sizes)
        assert self.storage_path.read_bytes() == authoritative

    def test_genuine_corruption_still_escalates(self) -> None:
        """No recoverable version matching the committed hash → escalate."""
        from elspeth.web.blobs.protocol import BlobIntegrityError

        self.storage_path.write_bytes(b"garbage-bytes")
        self.sidecar_path.write_bytes(b"other-garbage")

        with _blob_operation(self.engine, self.session_id) as (authority, context), pytest.raises(BlobIntegrityError):
            self._read_content(authority, context)
        assert self.sidecar_path.exists(), "corrupt sidecar must be left in place for manual reconciliation"


class TestDeleteBlobDurableJournal:
    """Composer delete must journal its staged deletion like the HTTP path.

    ``BlobServiceImpl.delete_blob`` records the tombstone in
    ``blob_deletion_cleanups_table`` inside the delete transaction so a
    crash after commit is recoverable; the composer tool previously
    tombstoned without any journal, stranding bytes at an unrecoverable
    random-suffix path (elspeth-3d1d1fcb6c).
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        import hashlib
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.session_id = str(uuid4())
        self.blob_id = str(uuid4())
        self.data_dir = str(tmp_path)
        now = datetime.now(UTC)

        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_data.csv"
        self.content = b"delete-journal-bytes"
        self.storage_path.write_bytes(self.content)

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Delete journal",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(self.content),
                    content_hash=hashlib.sha256(self.content).hexdigest(),
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def test_successful_delete_leaves_no_journal_or_tombstone_debris(self) -> None:
        from elspeth.web.sessions.models import blob_deletion_cleanups_table

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=context,
                data_dir=str(self.data_dir),
            )

        assert result.success is True, result.data
        assert not self.storage_path.exists()
        assert list(self.storage_path.parent.iterdir()) == [], "tombstone/journal debris left after successful delete"
        with self.engine.begin() as conn:
            remaining = conn.execute(select(blob_deletion_cleanups_table)).fetchall()
        assert remaining == [], "journal row must be purged after the staged bytes are gone"

    def test_delete_retry_finalizes_crash_after_commit_leftovers(self) -> None:
        """Journal row + tombstone without a blobs row → retry completes the delete."""
        from datetime import UTC, datetime

        from elspeth.web.sessions.models import blob_deletion_cleanups_table, blobs_table

        tombstone = self.storage_path.with_name(f".{self.blob_id}.delete-{'a' * 32}")
        self.storage_path.rename(tombstone)
        with self.engine.begin() as conn:
            journal_registered_at = datetime.now(UTC)
            conn.execute(
                blob_deletion_cleanups_table.insert().values(
                    blob_id=self.blob_id,
                    session_id=self.session_id,
                    storage_path=str(self.storage_path),
                    tombstone_path=str(tombstone),
                    updated_at=journal_registered_at,
                    created_at=journal_registered_at,
                )
            )
            conn.execute(blobs_table.delete().where(blobs_table.c.id == self.blob_id))

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = execute_tool(
                "delete_blob",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=context,
                data_dir=str(self.data_dir),
            )

        assert result.success is True, f"retry after a crash-after-commit must finalize the deletion: {result.data}"
        assert result.data == {"blob_id": self.blob_id, "deleted": True}
        assert not tombstone.exists(), "staged bytes must be purged by the retry"
        with self.engine.begin() as conn:
            remaining = conn.execute(select(blob_deletion_cleanups_table)).fetchall()
        assert remaining == [], "journal row must be retired by the retry"


class TestUpdateBlobSidecarCommitFailure:
    """A commit failure after the file swap must restore via the sidecar.

    The update sequence parks prior bytes at the operation-qualified backup,
    swaps the new bytes in, then commits.
    When the transaction fails after the swap, the except-arm restores the
    sidecar over storage_path in one atomic rename — storage must hold the
    original bytes, and neither sidecar nor tempfile may remain.
    """

    def test_commit_failure_after_swap_restores_sidecar(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import hashlib
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(engine)
        with engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        session_id = str(uuid4())
        blob_id = str(uuid4())
        now = datetime.now(UTC)
        storage_dir = tmp_path / "blobs" / session_id
        storage_dir.mkdir(parents=True)
        storage_path = storage_dir / f"{blob_id}_data.csv"
        original = b"pre-commit-original"
        storage_path.write_bytes(original)
        with engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Sidecar commit failure",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=blob_id,
                    session_id=session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(original),
                    content_hash=hashlib.sha256(original).hexdigest(),
                    storage_path=str(storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

        provenance_context = _verbatim_blob_context(engine, session_id, "replacement-bytes")
        with _blob_operation(engine, session_id) as (authority, context):
            original_commit = engine.dialect.do_commit
            fault_pending = False
            injected = False

            def arm_metadata_commit(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
                nonlocal fault_pending
                if statement.lstrip().upper().startswith("UPDATE BLOBS SET "):
                    fault_pending = True

            def fail_metadata_commit(connection) -> None:
                nonlocal fault_pending, injected
                if fault_pending:
                    fault_pending = False
                    injected = True
                    assert storage_path.read_bytes() == b"replacement-bytes"
                    backups = list(storage_path.parent.glob("*.backup"))
                    assert len(backups) == 1 and backups[0].read_bytes() == original
                    raise RuntimeError("simulated commit failure")
                original_commit(connection)

            monkeypatch.setattr(engine.dialect, "do_commit", fail_metadata_commit)
            event.listen(engine, "before_cursor_execute", arm_metadata_commit)
            try:
                with pytest.raises(RuntimeError, match="simulated commit failure"):
                    execute_tool(
                        "update_blob",
                        {"blob_id": blob_id, "content": "replacement-bytes"},
                        _empty_state(),
                        _mock_catalog(),
                        session_engine=engine,
                        session_id=session_id,
                        data_dir=str(tmp_path),
                        session_operation_authority=authority,
                        session_operation_context=context,
                        **provenance_context,
                    )
            finally:
                event.remove(engine, "before_cursor_execute", arm_metadata_commit)
            assert injected

            assert storage_path.read_bytes() == original, "storage must be restored from the sidecar after a commit failure"
            assert list(storage_path.parent.iterdir()) == [storage_path], (
                f"sidecar/tempfile debris left after rollback: {list(storage_path.parent.iterdir())}"
            )


class TestBlobCompositionReferenceGuards:
    """update/delete must refuse any blob the composition references anywhere.

    The update guard previously consulted only top-level source ``blob_ref``
    values (``_state_source_blob_refs``) and delete had no current-composition
    guard at all, so nested inline markers (``wire_blob_inline_ref``), node and
    output references, raw-path bindings, and ``blob:<uuid>`` sentinels could
    all be mutated or deleted behind an accepted composition
    (elspeth-b3feba9a7c).
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        import hashlib
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.session_id = str(uuid4())
        self.blob_id = str(uuid4())
        now = datetime.now(UTC)

        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_data.csv"
        self.content = b"reference-guard-bytes"
        self.content_sha = hashlib.sha256(self.content).hexdigest()
        self.data_dir = str(tmp_path)
        self.storage_path.write_bytes(self.content)

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Reference guards",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=len(self.content),
                    content_hash=self.content_sha,
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def _marker(self) -> dict[str, Any]:
        return {"blob_ref": self.blob_id, "mode": "inline_content", "sha256": self.content_sha}

    def _node(self, options: Mapping[str, Any]) -> NodeSpec:
        return NodeSpec(
            id="enrich",
            node_type="transform",
            plugin="passthrough",
            input="rows",
            on_success="result",
            on_error="discard",
            options=options,
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _state_with_source_options(self, options: Mapping[str, Any]) -> CompositionState:
        return CompositionState(
            source=SourceSpec(plugin="csv", on_success="rows", options=options, on_validation_failure="discard"),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _update(
        self,
        state: CompositionState,
        authority: SQLiteLocalSessionOperationAuthority,
        context: SessionOperationContext,
        provenance_context: dict[str, str],
    ) -> Any:
        return execute_tool(
            "update_blob",
            {"blob_id": self.blob_id, "content": "mutated-content"},
            state,
            _mock_catalog(),
            session_engine=self.engine,
            session_id=self.session_id,
            data_dir=str(self.data_dir),
            session_operation_authority=authority,
            session_operation_context=context,
            **provenance_context,
        )

    def _delete(
        self,
        state: CompositionState,
        authority: SQLiteLocalSessionOperationAuthority,
        context: SessionOperationContext,
    ) -> Any:
        return execute_tool(
            "delete_blob",
            {"blob_id": self.blob_id},
            state,
            _mock_catalog(),
            session_engine=self.engine,
            session_id=self.session_id,
            data_dir=str(self.data_dir),
            session_operation_authority=authority,
            session_operation_context=context,
        )

    def _assert_denied(self, result: Any, operation: str) -> None:
        assert result.success is False, f"{operation} succeeded against a composition-referenced blob: {result.data}"
        assert self.storage_path.read_bytes() == self.content, f"{operation} mutated the referenced blob's bytes"

    def test_update_denied_for_nested_source_inline_marker(self) -> None:
        state = self._state_with_source_options({"path": "rows.csv", "queries": self._marker()})
        provenance_context = _verbatim_blob_context(self.engine, self.session_id, "mutated-content")
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            self._assert_denied(self._update(state, authority, context, provenance_context), "update_blob")

    def test_update_denied_for_nested_node_inline_marker_in_list(self) -> None:
        state = replace(_empty_state(), nodes=(self._node({"prompts": [self._marker()]}),))
        provenance_context = _verbatim_blob_context(self.engine, self.session_id, "mutated-content")
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            self._assert_denied(self._update(state, authority, context, provenance_context), "update_blob")

    def test_update_denied_for_nested_output_inline_marker(self) -> None:
        state = replace(
            _empty_state(),
            outputs=(OutputSpec(name="result", plugin="json", options={"template": self._marker()}, on_write_failure="discard"),),
        )
        provenance_context = _verbatim_blob_context(self.engine, self.session_id, "mutated-content")
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            self._assert_denied(self._update(state, authority, context, provenance_context), "update_blob")

    def test_update_denied_for_raw_storage_path_binding(self) -> None:
        state = self._state_with_source_options({"path": str(self.storage_path)})
        provenance_context = _verbatim_blob_context(self.engine, self.session_id, "mutated-content")
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            self._assert_denied(self._update(state, authority, context, provenance_context), "update_blob")

    def test_update_denied_for_blob_path_sentinel(self) -> None:
        state = self._state_with_source_options({"path": f"blob:{self.blob_id}"})
        provenance_context = _verbatim_blob_context(self.engine, self.session_id, "mutated-content")
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            self._assert_denied(self._update(state, authority, context, provenance_context), "update_blob")

    def test_update_allowed_for_unreferenced_blob(self) -> None:
        state = self._state_with_source_options({"path": "unrelated.csv"})
        provenance_context = _verbatim_blob_context(self.engine, self.session_id, "mutated-content")
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = self._update(state, authority, context, provenance_context)
        assert result.success is True, f"update of an unreferenced blob must succeed: {result.data}"

    def test_update_denied_while_pending_proposal_references_blob(self) -> None:
        """update must honour the same pending-proposal retention edge delete does."""
        from datetime import UTC, datetime
        from uuid import uuid4 as _uuid4

        from elspeth.web.sessions.models import composition_proposals_table

        with self.engine.begin() as conn:
            arguments = {"source": {"plugin": "csv", "options": {"path": f"blob:{self.blob_id}"}}}
            conn.execute(
                composition_proposals_table.insert().values(
                    id=str(_uuid4()),
                    session_id=self.session_id,
                    tool_call_id=f"call-{_uuid4()}",
                    tool_name="set_pipeline",
                    status="pending",
                    summary="pending sentinel proposal",
                    rationale="retention guard test",
                    affects=["source"],
                    arguments_json=arguments,
                    arguments_redacted_json=arguments,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        provenance_context = _verbatim_blob_context(self.engine, self.session_id, "mutated-content")
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            self._assert_denied(self._update(_empty_state(), authority, context, provenance_context), "update_blob")

    def test_delete_denied_for_nested_node_inline_marker(self) -> None:
        state = replace(_empty_state(), nodes=(self._node({"prompts": [self._marker()]}),))
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = self._delete(state, authority, context)
        assert result.success is False, f"delete_blob succeeded against a composition-referenced blob: {result.data}"
        assert self.storage_path.exists(), "delete_blob removed the referenced blob's bytes"

    def test_delete_denied_for_top_level_source_blob_ref(self) -> None:
        state = self._state_with_source_options({"path": str(self.storage_path), "blob_ref": self.blob_id})
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = self._delete(state, authority, context)
        assert result.success is False, f"delete_blob succeeded against the current source's backing blob: {result.data}"
        assert self.storage_path.exists()

    def test_delete_allowed_once_unreferenced(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            result = self._delete(_empty_state(), authority, context)
        assert result.success is True, f"delete of an unreferenced blob must succeed: {result.data}"
        assert not self.storage_path.exists()
