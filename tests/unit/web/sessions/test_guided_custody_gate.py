"""Pre-persist guided custody gate at the composition_states write boundary.

elspeth-4c442aaaa8 EXPECTED-1: an active guided session whose reviewed
custody cannot bind to the live sources must be refused BEFORE the row is
inserted — every later read of a persisted unbindable tip re-raises, so the
only place the refusal can protect the session is inside the write lock.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import func, insert, select
from sqlalchemy.pool import StaticPool

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions import service as service_module
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

_PRIVATE = "/srv/elspeth/data/blobs/s1/50f5b3e9-f52f-4c5f-98df-a20ec7b2627b_colours.csv"
_LIVE_BLOB_REF = "50f5b3e9-f52f-4c5f-98df-a20ec7b2627b"
_REVIEWED_SENTINEL = "blob:360e1583-ae3c-4135-9240-0a26a14cf22f"
_EXITED = {"kind": "exited_to_freeform", "reason": "user_pressed_exit", "pipeline_yaml": None}


@pytest.fixture
def engine():
    eng = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(eng)
    return eng


@pytest.fixture
def service(engine):
    return SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))


def _unbindable_pair(*, terminal: dict[str, Any] | None, transition_consumed: bool = False) -> CompositionStateData:
    """Incident v13 (elspeth-201903a286): a retained sentinel review of ``source``
    re-attached to a live ``source`` bound to a different blob."""
    return CompositionStateData(
        sources={"source": {"plugin": "csv", "options": {"path": _PRIVATE, "blob_ref": _LIVE_BLOB_REF}}},
        nodes=[],
        edges=[],
        outputs=[],
        metadata_={"name": "Guided", "description": ""},
        is_valid=False,
        composer_meta={
            "guided_session": {
                "reviewed_sources": {
                    "11111111-1111-4111-8111-111111111111": {"name": "source", "plugin": "csv", "options": {"path": _REVIEWED_SENTINEL}}
                },
                "pending_source_intents": {},
                "terminal": terminal,
                "transition_consumed": transition_consumed,
            }
        },
    )


def _version_count(engine, session_id: uuid.UUID) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == str(session_id))
        ).scalar_one()


class TestWriteBoundaryGate:
    @pytest.mark.asyncio
    async def test_save_composition_state_refuses_an_active_unbindable_pair(self, service, engine) -> None:
        session = await service.create_session("alice", "Guided", "local")
        with pytest.raises(AuditIntegrityError, match="guided blob"):
            await service.save_composition_state(session.id, _unbindable_pair(terminal=None), provenance="post_compose")
        assert _version_count(engine, session.id) == 0
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_save_composition_state_persists_a_terminal_unbindable_pair(self, service, engine) -> None:
        session = await service.create_session("alice", "Guided", "local")
        record = await service.save_composition_state(session.id, _unbindable_pair(terminal=_EXITED), provenance="post_compose")
        assert record.version == 1
        assert _version_count(engine, session.id) == 1

    @pytest.mark.asyncio
    async def test_commit_transition_response_refuses_an_active_unbindable_pair(self, service, engine) -> None:
        session = await service.create_session("alice", "Guided", "local")
        seed = await service.save_composition_state(
            session.id,
            CompositionStateData(metadata_={"name": "Guided", "description": ""}, is_valid=True),
            provenance="session_seed",
        )
        with pytest.raises(AuditIntegrityError, match="guided blob"):
            await service.commit_transition_response(
                session_id=session.id,
                expected_current_state_id=seed.id,
                state=_unbindable_pair(terminal=None, transition_consumed=True),
                assistant_content="reply",
                raw_content=None,
            )
        assert _version_count(engine, session.id) == 1
        messages = await service.get_messages(session.id)
        assert [m.role for m in messages] == []

    @pytest.mark.asyncio
    async def test_set_active_state_refuses_to_copy_a_legacy_unbindable_active_row(self, service, engine) -> None:
        """A row persisted before the gate existed must not be re-tipped by revert:
        the copy would brick the session exactly as the original insert did."""
        session = await service.create_session("alice", "Guided", "local")
        good = await service.save_composition_state(
            session.id,
            CompositionStateData(metadata_={"name": "Guided", "description": ""}, is_valid=True),
            provenance="session_seed",
        )
        legacy = _unbindable_pair(terminal=None)
        legacy_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                insert(composition_states_table).values(
                    id=str(legacy_id),
                    session_id=str(session.id),
                    version=2,
                    source=None,
                    sources=service_module._enveloped_state_column(legacy.sources),
                    nodes=service_module._enveloped_state_column(legacy.nodes),
                    edges=service_module._enveloped_state_column(legacy.edges),
                    outputs=service_module._enveloped_state_column(legacy.outputs),
                    metadata_=service_module._enveloped_state_column(legacy.metadata_),
                    is_valid=False,
                    validation_errors=None,
                    composer_meta=service_module._enveloped_state_column(legacy.composer_meta),
                    derived_from_state_id=None,
                    provenance="post_compose",
                    created_at=datetime.now(UTC),
                )
            )
        with pytest.raises(AuditIntegrityError, match="guided blob"):
            await service.set_active_state(session.id, legacy_id)
        assert _version_count(engine, session.id) == 2
        reverted = await service.set_active_state(session.id, good.id)
        assert reverted.version == 3


def _custody_census_package_root() -> Path:
    """Return ``src/elspeth`` resolved from THIS test file, not from the import.

    ``tests/unit/web/sessions/test_guided_custody_gate.py``'s parents chain is
    ``[sessions/, web/, unit/, tests/, <repo>]``, so ``parents[4]`` is the repo
    root — the same derivation ``test_static_direct_writers.py`` uses. Reading
    ``elspeth.__file__`` instead would let a worktree run that forgot the
    worktree on ``PYTHONPATH`` census the MAIN checkout's package and report a
    confidently wrong answer rather than an error.
    """

    repo_root = Path(__file__).resolve().parents[4]
    package_root = repo_root / "src" / "elspeth"
    if not package_root.is_dir() or not (repo_root / "tests").is_dir():
        raise RuntimeError(f"could not resolve the elspeth package root from {Path(__file__)}: {package_root} is not a package tree")
    return package_root


def test_every_composition_states_insert_site_is_preceded_by_the_custody_gate() -> None:
    """Census (modeled on the elspeth-3b45cdb41e proof-requirement census): every
    ``insert(composition_states_table)`` in the package sits inside a method that
    calls ``assert_guided_custody_persistable`` before the insert. A new insert
    site must either route through a gated writer or carry its own gate call.

    The scan is WHOLE-PACKAGE, not sessions-service-only. The session-operation
    authority in ``web/coordination/repository.py`` owns three of the four insert
    sites, and a census that read only ``service.py`` would be structurally blind
    to every one of them — it would go green over an ungated writer in the very
    file the multi-replica authority lives in.

    The pin is keyed on a COUNT per writer, not on a set of writer names: a
    SECOND insert added inside an already-gated method would otherwise be
    invisible, and the per-site body check below would wave it through on the
    FIRST insert's gate call without proving that call covers the second
    insert's data.
    """
    package_root = _custody_census_package_root()
    def_pattern = re.compile(r"^    (async )?def (\w+)\(")
    sites: dict[str, int] = {}
    for source_path in sorted(package_root.rglob("*.py")):
        source_lines = source_path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(source_lines):
            if "insert(composition_states_table)" not in line:
                continue
            display = f"{source_path.relative_to(package_root).as_posix()}:{index + 1}"
            owner_line = next((i for i in range(index, -1, -1) if def_pattern.match(source_lines[i])), None)
            # Fail with the site, not with a bare StopIteration: an insert at
            # module scope (or under a def the pattern cannot see) is an
            # unattributable writer, which is a census failure, not a crash.
            assert owner_line is not None, f"{display} inserts a composition_states row outside any method"
            owner_match = def_pattern.match(source_lines[owner_line])
            assert owner_match is not None
            owner = owner_match.group(2)
            body = "\n".join(source_lines[owner_line:index])
            site = f"{source_path.relative_to(package_root).as_posix()}::{owner}"
            assert "assert_guided_custody_persistable(" in body, f"{site} inserts a composition_states row without the custody gate"
            sites[site] = sites.get(site, 0) + 1
    assert sites == {
        "web/coordination/repository.py::append_state": 1,
        "web/coordination/repository.py::create_or_reconcile_pending": 1,
        "web/coordination/repository.py::insert_child_state": 1,
        "web/sessions/service.py::_insert_composition_state": 1,
    }, sites
