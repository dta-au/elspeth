"""A stored composition state can always be read back.

``state_from_record`` refuses a state without pipeline metadata (Tier 1), so
every writer must refuse to store one. One rule, ``require_stored_state_metadata``,
serves the DTO and the record at construction, the reader, and each writer.
"""

import ast
import asyncio
import inspect
import textwrap
from dataclasses import replace

import pytest
import structlog
from sqlalchemy import func, select

from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.coordination import repository
from elspeth.web.sessions import converters, service
from elspeth.web.sessions._persist_payload import RedactedToolRow, StatePayload
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.session_fences import FencedComposeTurnHarness
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness


@pytest.fixture
def store(tmp_path):
    engine = create_session_engine(f"sqlite:///{tmp_path / 'metadata.db'}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
    sessions = FencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    yield sessions, engine
    engine.dispose()


def _valid() -> CompositionStateData:
    raw = CompositionState(nodes=(), edges=(), outputs=(), version=1, metadata=PipelineMetadata(description="kept")).to_dict()
    return CompositionStateData(
        sources=raw["sources"], nodes=raw["nodes"], edges=raw["edges"], outputs=raw["outputs"], metadata_=raw["metadata"]
    )


def _without_metadata() -> CompositionStateData:
    """An object that bypassed construction, as an untyped caller could hand a writer."""
    invalid = _valid()
    object.__setattr__(invalid, "metadata_", None)
    return invalid


def _stored_states(engine) -> int:
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(composition_states_table))


@pytest.mark.asyncio
async def test_valid_state_round_trips_through_the_reader(store):
    sessions, _engine = store
    session = await sessions.create_session(user_id="alice", title="Metadata", auth_provider_type="local")
    saved = await sessions.save_composition_state(session.id, _valid(), provenance="session_seed")
    assert state_from_record(await sessions.get_state(saved.id)).metadata.description == "kept"


def test_state_data_cannot_be_built_without_metadata():
    with pytest.raises(ValueError, match="metadata_"):
        replace(_valid(), metadata_=None)


@pytest.mark.asyncio
async def test_stored_record_without_metadata_is_refused_at_decode(store):
    sessions, _engine = store
    session = await sessions.create_session(user_id="alice", title="Metadata", auth_provider_type="local")
    saved = await sessions.save_composition_state(session.id, _valid(), provenance="session_seed")
    with pytest.raises(ValueError, match="metadata_"):
        replace(saved, metadata_=None)


@pytest.mark.asyncio
async def test_saved_state_without_metadata_is_refused_before_any_write(store):
    sessions, engine = store
    session = await sessions.create_session(user_id="alice", title="Metadata", auth_provider_type="local")
    with pytest.raises(ValueError, match="metadata_"):
        await sessions.save_composition_state(session.id, _without_metadata(), provenance="session_seed")
    assert _stored_states(engine) == 0


def test_compose_checkpoint_without_metadata_is_refused_before_any_write(tmp_path):
    engine = create_session_engine(f"sqlite:///{tmp_path / 'compose.db'}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
    sessions = FencedComposeTurnHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    session = asyncio.run(sessions.create_session(user_id="alice", title="Metadata", auth_provider_type="local"))
    with pytest.raises(ValueError, match="metadata_"):
        sessions.persist_compose_turn(
            session_id=str(session.id),
            assistant_content="checkpoint",
            redacted_assistant_tool_calls=({"id": "metadata_call", "function": {"name": "set_pipeline"}},),
            redacted_tool_rows=(
                RedactedToolRow("metadata_call", '{"ok": true}', StatePayload(data=_without_metadata(), derived_from_state_id=None)),
            ),
            parent_composition_state_id=None,
            expected_current_state_id=None,
            writer_principal="compose_loop",
            plugin_crash_pending=False,
        )
    assert _stored_states(engine) == 0
    engine.dispose()


def _calls_named(function, name: str) -> int:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name)


@pytest.mark.parametrize(
    "function",
    [
        converters.state_from_record,
        CompositionStateData.__post_init__,
        CompositionStateRecord.__post_init__,
        service.SessionServiceImpl._insert_composition_state,
        repository._RepositoryCompositionStateMutations.append_state,
        repository._RepositoryInterpretationMutations._create_pending,
        repository._ForkChildSessionMutations.insert_child_state,
    ],
    ids=lambda function: function.__qualname__,
)
def test_reader_and_every_writer_share_one_metadata_rule(function):
    assert _calls_named(function, "require_stored_state_metadata") == 1
