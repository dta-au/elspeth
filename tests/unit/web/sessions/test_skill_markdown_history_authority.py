"""Tests for the skill Markdown history write authority."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from sqlalchemy import event, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import skill_markdown_history_table
from elspeth.web.sessions.protocol import SessionServiceProtocol
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.skill_markdown_history import (
    RepositorySkillMarkdownHistoryAuthority,
    SkillMarkdownHistoryAuthority,
)
from elspeth.web.sessions.telemetry import build_sessions_telemetry


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


@pytest.fixture
def engine(tmp_path: Path):
    value = create_session_engine(f"sqlite:///{tmp_path / 'skill-history.db'}")
    initialize_session_schema(value)
    yield value
    value.dispose()


def test_skill_markdown_history_authority_protocol_is_runtime_checkable_and_handle_free() -> None:
    class ExactAuthority:
        def upsert_exact(self, *, skill_hash: str, filename: str, content: str) -> bool:
            return False

    assert isinstance(ExactAuthority(), SkillMarkdownHistoryAuthority)
    assert tuple(inspect.signature(SkillMarkdownHistoryAuthority.upsert_exact).parameters) == (
        "self",
        "skill_hash",
        "filename",
        "content",
    )
    assert "first_seen_at" not in inspect.signature(SkillMarkdownHistoryAuthority.upsert_exact).parameters
    assert not hasattr(SkillMarkdownHistoryAuthority, "engine")
    assert not hasattr(SkillMarkdownHistoryAuthority, "connection")
    assert "first_seen_at" not in inspect.signature(SessionServiceProtocol.upsert_skill_markdown_history).parameters
    assert "first_seen_at" not in inspect.signature(SessionServiceImpl.upsert_skill_markdown_history).parameters


def test_hash_mismatch_raises_before_any_dml(engine) -> None:
    statements: list[str] = []

    def record_statement(_conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        authority = RepositorySkillMarkdownHistoryAuthority(engine)
        with pytest.raises(AuditIntegrityError, match="hash mismatch"):
            authority.upsert_exact(skill_hash="0" * 64, filename="pipeline_composer.md", content="# exact")
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
    assert statements == []


def test_exact_insert_is_true_and_duplicate_is_false_with_database_clock(engine) -> None:
    authority = RepositorySkillMarkdownHistoryAuthority(engine)
    content = "# Composer skill"
    skill_hash = _hash(content)
    with engine.connect() as conn:
        before = conn.exec_driver_sql("SELECT CURRENT_TIMESTAMP").scalar_one()

    assert authority.upsert_exact(skill_hash=skill_hash, filename="pipeline_composer.md", content=content) is True
    assert authority.upsert_exact(skill_hash=skill_hash, filename="pipeline_composer.md", content=content) is False

    with engine.connect() as conn:
        row = conn.execute(select(skill_markdown_history_table)).mappings().one()
        after = conn.exec_driver_sql("SELECT CURRENT_TIMESTAMP").scalar_one()
    assert row["hash"] == skill_hash
    assert row["first_seen_at"] is not None
    assert before <= row["first_seen_at"].isoformat(sep=" ") <= after


def test_conflicting_existing_filename_raises_integrity_error(engine) -> None:
    authority = RepositorySkillMarkdownHistoryAuthority(engine)
    original = "# exact"
    skill_hash = _hash(original)
    assert authority.upsert_exact(skill_hash=skill_hash, filename="pipeline_composer.md", content=original) is True

    with pytest.raises(AuditIntegrityError):
        authority.upsert_exact(skill_hash=skill_hash, filename="renamed.md", content=original)


def test_conflicting_existing_content_raises_after_conflict_read(engine) -> None:
    authority = RepositorySkillMarkdownHistoryAuthority(engine)
    original = "# exact"
    skill_hash = _hash(original)
    assert authority.upsert_exact(skill_hash=skill_hash, filename="pipeline_composer.md", content=original) is True
    with engine.begin() as conn:
        conn.execute(
            skill_markdown_history_table.update().where(skill_markdown_history_table.c.hash == skill_hash).values(content="# corrupted")
        )

    with pytest.raises(AuditIntegrityError, match="different filename or content"):
        authority.upsert_exact(skill_hash=skill_hash, filename="pipeline_composer.md", content=original)


def test_concurrent_duplicate_returns_exactly_true_and_false(engine) -> None:
    authority = RepositorySkillMarkdownHistoryAuthority(engine)
    content = "# concurrent"
    barrier = threading.Barrier(2)

    def upsert() -> bool:
        barrier.wait()
        return authority.upsert_exact(skill_hash=_hash(content), filename="pipeline_composer.md", content=content)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(upsert), pool.submit(upsert))]
    assert set(results) == {True, False}


def test_commit_failure_propagates_and_does_not_report_success(engine) -> None:
    authority = RepositorySkillMarkdownHistoryAuthority(engine)
    skill_hash = _hash("# commit")

    def fail_commit(_conn: Any) -> None:
        raise RuntimeError("commit failed")

    event.listen(engine, "commit", fail_commit, once=True)
    with pytest.raises(RuntimeError, match="commit failed"):
        authority.upsert_exact(skill_hash=skill_hash, filename="pipeline_composer.md", content="# commit")
    with engine.connect() as conn:
        assert (
            conn.execute(select(skill_markdown_history_table.c.hash).where(skill_markdown_history_table.c.hash == skill_hash)).one_or_none()
            is None
        )


def test_session_service_delegates_through_run_sync(engine, tmp_path: Path) -> None:
    class RecordingAuthority:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        def upsert_exact(self, **kwargs: str) -> bool:
            self.calls.append(kwargs)
            return False

    authority = RecordingAuthority()
    service = SessionServiceImpl(
        engine,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test"),
        skill_markdown_history_authority=authority,
    )

    result = asyncio.run(service.upsert_skill_markdown_history(skill_hash="a" * 64, filename="pipeline_composer.md", content="# delegated"))

    assert result is False
    assert authority.calls == [{"skill_hash": "a" * 64, "filename": "pipeline_composer.md", "content": "# delegated"}]


def test_composer_retries_after_failure_and_sets_flag_only_after_success() -> None:
    class FlakySessions:
        def __init__(self) -> None:
            self.calls = 0

        async def upsert_skill_markdown_history(self, **_kwargs: str) -> bool:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("commit failed")
            return False

    sessions = FlakySessions()
    text = "# Composer"
    composer = SimpleNamespace(
        _skill_markdown_history_upserted=False,
        _sessions_service=sessions,
        _composer_skill_text=text,
        _composer_skill_hash=_hash(text),
        _composer_skill_name="pipeline_composer",
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        asyncio.run(ComposerServiceImpl._maybe_upsert_skill_markdown_history(composer))
    assert composer._skill_markdown_history_upserted is False
    asyncio.run(ComposerServiceImpl._maybe_upsert_skill_markdown_history(composer))
    assert composer._skill_markdown_history_upserted is True
    assert sessions.calls == 2
