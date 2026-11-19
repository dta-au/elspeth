"""The file review snapshot binds both bytes and blob identity."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from elspeth.web.sessions.prompt_file_review import PromptFileReviewStale, resolve_prompt_review_snapshot
from tests.unit.web.composer import test_blob_inline_tools as blob_tools

blob_env = blob_tools.blob_env


def _record_and_bytes(env: dict[str, Any], blob_id: UUID) -> tuple[Any, bytes]:
    record = env["authority"].mutate(env["operation"], lambda transaction: transaction.blobs.read_blob(blob_id=blob_id))
    return record, Path(record.storage_path).read_bytes()


def test_snapshot_records_full_text_and_distinguishes_identical_blob_bytes(blob_env: dict[str, Any]) -> None:
    first = blob_tools._create_blob(blob_env, content="Line one\n  line two\n")
    second = blob_tools._create_blob(blob_env, content="Line one\n  line two\n")
    assert first.success and second.success
    ids = [UUID(first.data["blob_id"]), UUID(second.data["blob_id"])]

    def snapshot(blob_id: UUID) -> Any:
        record, _ = _record_and_bytes(blob_env, blob_id)
        return resolve_prompt_review_snapshot(
            session_id=UUID(blob_env["session_id"]),
            node_id="classify",
            options={"prompt_template": {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": record.content_hash}},
            read_blob=lambda requested: _record_and_bytes(blob_env, requested),
        )

    left = snapshot(ids[0])
    right = snapshot(ids[1])
    assert left is not None and right is not None
    assert left.bindings[0].text == "Line one\n  line two\n"
    assert left.bindings[0].content_sha256 == right.bindings[0].content_sha256
    assert left.anchor != right.anchor
    assert left.effective_artifact_hash == right.effective_artifact_hash


def test_snapshot_refuses_changed_bytes_even_when_metadata_is_unchanged(blob_env: dict[str, Any]) -> None:
    blob = blob_tools._create_blob(blob_env, content="Reviewed")
    assert blob.success
    blob_id = UUID(blob.data["blob_id"])
    record, content = _record_and_bytes(blob_env, blob_id)
    with pytest.raises(PromptFileReviewStale):
        resolve_prompt_review_snapshot(
            session_id=UUID(blob_env["session_id"]),
            node_id="classify",
            options={"prompt_template": {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": record.content_hash}},
            read_blob=lambda _requested: (replace(record, size_bytes=len(content)), b"Tampered"),
        )


def test_landscape_records_admitted_prompt_identity(blob_env: dict[str, Any], tmp_path: Path) -> None:
    from sqlalchemy import select

    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.core.landscape.factory import RecorderFactory
    from elspeth.core.landscape.schema import run_prompt_inputs_table
    from elspeth.web.execution.service import _run_prompt_input_evidence

    blob = blob_tools._create_blob(blob_env, content="First row\nSecond row")
    assert blob.success
    blob_id = UUID(blob.data["blob_id"])
    record, _content = _record_and_bytes(blob_env, blob_id)
    snapshot = resolve_prompt_review_snapshot(
        session_id=UUID(blob_env["session_id"]),
        node_id="classify",
        options={"prompt_template": {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": record.content_hash}},
        read_blob=lambda requested: _record_and_bytes(blob_env, requested),
    )
    assert snapshot is not None
    with LandscapeDB(f"sqlite:///{tmp_path / 'landscape.db'}") as db:
        RecorderFactory(db).run_lifecycle.begin_run({}, "v1", run_id="run-1", run_prompt_inputs=_run_prompt_input_evidence((snapshot,)))
        with db.read_only_connection() as conn:
            row = conn.execute(select(run_prompt_inputs_table)).one()
        assert row.run_id == "run-1"
        assert row.blob_id == str(blob_id)
        assert row.field_path == "prompt_template"
        assert row.content_sha256 == record.content_hash
        assert row.review_anchor == snapshot.anchor


def test_run_start_uses_admitted_hash_after_blob_bytes_change(blob_env: dict[str, Any], tmp_path: Path) -> None:
    from hashlib import sha256

    from sqlalchemy import select

    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.core.landscape.factory import RecorderFactory
    from elspeth.core.landscape.schema import run_prompt_inputs_table
    from elspeth.web.execution.service import _run_prompt_input_evidence

    blob = blob_tools._create_blob(blob_env, content="Reviewed")
    assert blob.success
    blob_id = UUID(blob.data["blob_id"])
    record, _content = _record_and_bytes(blob_env, blob_id)
    snapshot = resolve_prompt_review_snapshot(
        session_id=UUID(blob_env["session_id"]),
        node_id="classify",
        options={"prompt_template": {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": record.content_hash}},
        read_blob=lambda requested: _record_and_bytes(blob_env, requested),
    )
    assert snapshot is not None
    Path(record.storage_path).write_bytes(b"Changed after admission")
    with LandscapeDB(f"sqlite:///{tmp_path / 'landscape.db'}") as db:
        RecorderFactory(db).run_lifecycle.begin_run(
            {}, "v1", run_id="admitted-run", run_prompt_inputs=_run_prompt_input_evidence((snapshot,))
        )
        with db.read_only_connection() as conn:
            row = conn.execute(select(run_prompt_inputs_table)).one()
    assert row.content_sha256 == record.content_hash
    assert row.content_sha256 != sha256(b"Changed after admission").hexdigest()


def test_run_prompt_inputs_update_refused(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import insert, update
    from sqlalchemy.exc import IntegrityError

    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.core.landscape.schema import run_prompt_inputs_table

    with LandscapeDB(f"sqlite:///{tmp_path / 'landscape.db'}") as db:
        from elspeth.core.landscape.factory import RecorderFactory

        RecorderFactory(db).run_lifecycle.begin_run({}, "v1", run_id="run-1")
        with db.engine.begin() as conn:
            conn.execute(
                insert(run_prompt_inputs_table).values(
                    run_id="run-1",
                    node_id="classify",
                    field_path="prompt_template",
                    blob_id="b1",
                    content_sha256="a" * 64,
                    review_anchor="a" * 64,
                    admitted_at=datetime.now(UTC),
                )
            )
        with pytest.raises(IntegrityError, match="run_prompt_inputs is append-only"), db.engine.begin() as conn:
            conn.execute(update(run_prompt_inputs_table).values(content_sha256="b" * 64))
