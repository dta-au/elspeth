"""Handle-free authority for exact skill Markdown history writes."""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, final, runtime_checkable

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.models import skill_markdown_history_table


@runtime_checkable
class SkillMarkdownHistoryAuthority(Protocol):
    """Handle-free capability for exact skill Markdown history writes."""

    def upsert_exact(self, *, skill_hash: str, filename: str, content: str) -> bool:
        """Insert exact content once, returning whether this call inserted it."""
        ...


@final
class RepositorySkillMarkdownHistoryAuthority:
    """Own every skill Markdown history write and verify hash collisions."""

    __slots__ = ("_dialect", "_engine")

    def __init__(self, engine: Engine) -> None:
        dialect = engine.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise NotImplementedError(
                "SkillMarkdownHistoryAuthority requires INSERT ON CONFLICT DO NOTHING RETURNING; "
                f"unsupported session database dialect {dialect!r}; supported dialects: sqlite, postgresql"
            )
        self._engine = engine
        self._dialect = dialect

    def upsert_exact(self, *, skill_hash: str, filename: str, content: str) -> bool:
        """Commit an exact content-addressed row or verify the existing row."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        if skill_hash != content_hash:
            raise AuditIntegrityError(f"skill markdown hash mismatch: supplied {skill_hash!r}, exact content hashes to {content_hash!r}")

        values = {
            "hash": skill_hash,
            "filename": filename,
            "content": content,
            "first_seen_at": func.current_timestamp(),
        }
        stmt: Any
        if self._dialect == "sqlite":
            stmt = sqlite_insert(skill_markdown_history_table).values(**values)
        else:
            stmt = postgresql_insert(skill_markdown_history_table).values(**values)
        stmt = stmt.on_conflict_do_nothing(index_elements=[skill_markdown_history_table.c.hash]).returning(
            skill_markdown_history_table.c.hash
        )

        with self._engine.begin() as conn:
            inserted_hash = conn.execute(stmt).scalar_one_or_none()
            if inserted_hash is not None:
                if inserted_hash != skill_hash:
                    raise AuditIntegrityError("skill markdown insert returned an unexpected hash")
                return True
            existing = conn.execute(
                select(skill_markdown_history_table.c.filename, skill_markdown_history_table.c.content).where(
                    skill_markdown_history_table.c.hash == skill_hash
                )
            ).one_or_none()
            if existing is None:
                raise AuditIntegrityError("skill markdown conflict did not resolve to an existing row")
            if existing.filename != filename or existing.content != content:
                raise AuditIntegrityError("skill markdown hash collision resolved to different filename or content")
            return False
