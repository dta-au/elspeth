"""Tests for the audit-story read service, including historical cache rows."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from elspeth.contracts import NodeType, RunStatus
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import rows_table, runs_table
from elspeth.web.sessions.audit_story_service import (
    AuditStoryIntegrityError,
    AuditStoryNotRecordedError,
    AuditStoryService,
)
from tests.fixtures.landscape import make_factory, make_landscape_db, register_test_node

_STARTED_AT = datetime(2026, 5, 15, tzinfo=UTC)


def _seed_historical_story(
    db: LandscapeDB,
    *,
    source_hashes: tuple[str, ...] = ("3" * 64,),
    seeded_from_cache: bool = False,
    cache_key: str | None = None,
) -> str:
    """Seed historical persisted facts to exercise the reader independently.

    The retired cache writer is not a production entry point. Raw fixture SQL
    preserves coverage of existing audit rows without recreating that writer.
    """
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(
        config={"pipeline": "historical-audit-story"},
        canonical_version="v1",
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    source_node_id = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE, plugin_name="inline_blob")
    register_test_node(factory.data_flow, run.run_id, "sink", node_type=NodeType.SINK, plugin_name="tutorial_summary")
    with db.write_connection() as conn:
        conn.execute(
            update(runs_table)
            .where(runs_table.c.run_id == run.run_id)
            .values(
                started_at=_STARTED_AT,
                completed_at=_STARTED_AT,
                status=RunStatus.COMPLETED.value,
                llm_call_count=0,
                seeded_from_cache=seeded_from_cache,
                cache_key=cache_key,
            )
        )
        for index, source_hash in enumerate(source_hashes):
            conn.execute(
                rows_table.insert().values(
                    row_id=f"row-{index}",
                    run_id=run.run_id,
                    source_node_id=source_node_id,
                    row_index=index,
                    source_row_index=index,
                    ingest_sequence=index,
                    source_data_hash=source_hash,
                    source_data_ref=None,
                    created_at=_STARTED_AT,
                )
            )
    return run.run_id


def test_audit_story_reads_real_landscape_rows() -> None:
    db = make_landscape_db()
    run_id = _seed_historical_story(db, source_hashes=("1" * 64,), seeded_from_cache=True, cache_key="b" * 64)
    story = AuditStoryService(db).get_run_audit_story(run_id, public_run_id="session-run-1", session_id="session-1")
    assert story.run_id == "session-run-1"
    assert story.session_id == "session-1"
    assert story.llm_call_count == 0
    assert story.source_data_hash == "1" * 64
    assert "output_file_hash" not in story.model_dump()
    assert story.started_at.replace(tzinfo=UTC) == _STARTED_AT
    assert story.plugin_versions == {"inline_blob": "1.0", "tutorial_summary": "1.0"}
    assert story.seeded_from_cache is True
    assert story.cache_key == "b" * 64


def test_audit_story_aggregates_multiple_row_source_hashes() -> None:
    db = make_landscape_db()
    hashes = ("a" * 64, "b" * 64)
    run_id = _seed_historical_story(db, source_hashes=hashes)
    story = AuditStoryService(db).get_run_audit_story(run_id, public_run_id="session-run-1", session_id="session-1")
    assert story.source_data_hash == stable_hash({"source_data_hashes": list(hashes)})


def test_audit_story_null_llm_call_count_raises_not_recorded_error() -> None:
    """An absent historical count maps to404 independently of corrupt facts."""
    db = make_landscape_db()
    run_id = _seed_historical_story(db)
    with db.write_connection() as conn:
        conn.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(llm_call_count=None))
    with pytest.raises(AuditStoryNotRecordedError):
        AuditStoryService(db).get_run_audit_story(run_id, public_run_id="session-run-1", session_id="session-1")


def test_audit_story_not_recorded_error_is_not_an_integrity_error() -> None:
    assert not issubclass(AuditStoryNotRecordedError, AuditStoryIntegrityError)
    assert not issubclass(AuditStoryIntegrityError, AuditStoryNotRecordedError)


def test_audit_story_corrupt_recorded_row_still_raises_integrity_error() -> None:
    """A claimed cache replay without its key remains an integrity violation."""
    db = make_landscape_db()
    run_id = _seed_historical_story(db)
    with db.write_connection() as conn:
        conn.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(seeded_from_cache=True, cache_key=None))
    with pytest.raises(AuditStoryIntegrityError, match="NULL cache_key"):
        AuditStoryService(db).get_run_audit_story(run_id, public_run_id="session-run-1", session_id="session-1")


def test_audit_story_missing_run_raises_named_error() -> None:
    with pytest.raises(AuditStoryIntegrityError, match="not found"):
        AuditStoryService(make_landscape_db()).get_run_audit_story("missing-run", public_run_id="session-run-1", session_id="session-1")
