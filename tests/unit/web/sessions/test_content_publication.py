"""Content identity is published with the exact immutable graph version."""

from dataclasses import replace

import pytest
import structlog
from sqlalchemy import event, func, select, update

from elspeth.contracts.composer_content import ContentPublication
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.coordination.repository import _active_run_state_record, _RepositoryInterpretationMutations
from elspeth.web.sessions.content_publication import read_content_publication, storage_content_hash
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness


@pytest.fixture
def store(tmp_path):
    engine = create_session_engine(f"sqlite:///{tmp_path / 'content.db'}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
    service = FencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    yield service, engine
    engine.dispose()


def _data(description="original", *, metadata=None):
    raw = CompositionState(nodes=(), edges=(), outputs=(), version=1, metadata=PipelineMetadata(description=description)).to_dict()
    return CompositionStateData(
        sources=raw["sources"],
        nodes=raw["nodes"],
        edges=raw["edges"],
        outputs=raw["outputs"],
        metadata_=raw["metadata"],
        composer_meta=metadata,
    )


@pytest.mark.asyncio
async def test_real_checkpoint_publishes_version_and_uninspected_applicability_together(store):
    service, engine = store
    session = await service.create_session(user_id="alice", title="Content", auth_provider_type="local")
    saved = await service.save_composition_state(session.id, _data(metadata={"opaque": {"keep": [1]}}), provenance="session_seed")
    payload = deep_thaw(saved.composer_meta)["content_publication"]
    assert payload["session_id"] == str(session.id)
    assert payload["state_id"] == str(saved.id)
    assert payload["state_version"] == saved.version
    assert payload["applicability"] == "requires_current_inspection"
    assert deep_thaw(saved.composer_meta)["opaque"] == {"keep": [1]}
    with engine.connect() as connection:
        row = connection.execute(select(composition_states_table).where(composition_states_table.c.id == str(saved.id))).one()
    assert row.composer_meta["data"]["content_publication"] == payload
    assert row.metadata_["data"]["description"] == "original"
    assert read_content_publication(saved) == ContentPublication(session.id, saved.id, saved.version, storage_content_hash(saved))


@pytest.mark.asyncio
async def test_edit_and_identical_revert_receive_fresh_version_bindings_without_reusing_clearance(store):
    service, _engine = store
    session = await service.create_session(user_id="alice", title="Content", auth_provider_type="local")
    original = await service.save_composition_state(session.id, _data(), provenance="session_seed")
    edited = await service.save_composition_state(session.id, _data("edited", metadata=original.composer_meta), provenance="session_seed")
    restored = await service.save_composition_state(
        session.id, replace(_data(), composer_meta=edited.composer_meta), provenance="session_seed"
    )
    first, second, third = [deep_thaw(row.composer_meta)["content_publication"] for row in (original, edited, restored)]
    assert first["storage_content_hash"] != second["storage_content_hash"]
    assert first["storage_content_hash"] == third["storage_content_hash"]
    assert [item["state_version"] for item in (first, second, third)] == [1, 2, 3]
    assert len({item["state_id"] for item in (first, second, third)}) == 3
    assert all(item["applicability"] == "requires_current_inspection" for item in (first, second, third))
    assert deep_thaw((await service.get_state(original.id)).composer_meta)["content_publication"] == first


@pytest.mark.asyncio
async def test_failed_insert_rolls_back_graph_and_content_evidence(store):
    service, engine = store
    session = await service.create_session(user_id="alice", title="Content", auth_provider_type="local")
    attempted = []

    def fail_after_insert(connection, cursor, statement, parameters, context, executemany):
        if context.compiled is not None and context.compiled.isinsert and context.compiled.statement.table.name == "composition_states":
            attempted.append(context.compiled_parameters[0]["composer_meta"])
            raise RuntimeError("controlled post-insert failure")

    event.listen(engine, "after_cursor_execute", fail_after_insert)
    try:
        with pytest.raises(RuntimeError, match="controlled post-insert failure"):
            await service.save_composition_state(session.id, _data(), provenance="session_seed")
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_insert)
    assert len(attempted) == 1
    assert attempted[0]["data"]["content_publication"]["applicability"] == "requires_current_inspection"
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(composition_states_table)) == 0


@pytest.mark.asyncio
async def test_malformed_inherited_metadata_refuses_before_any_graph_write(store):
    service, engine = store
    session = await service.create_session(user_id="alice", title="Content", auth_provider_type="local")
    saved = await service.save_composition_state(session.id, _data(), provenance="session_seed")
    with pytest.raises(AuditIntegrityError):
        await service.save_composition_state(
            session.id, _data("changed", metadata={"content_publication": {"applicability": "clean"}}), provenance="session_seed"
        )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(composition_states_table)) == 1
    assert (await service.get_state(saved.id)) == saved


@pytest.mark.asyncio
async def test_read_refuses_graph_tampering_instead_of_returning_stale_applicability(store):
    service, engine = store
    session = await service.create_session(user_id="alice", title="Content", auth_provider_type="local")
    saved = await service.save_composition_state(session.id, _data(), provenance="session_seed")
    with engine.begin() as connection:
        connection.execute(
            update(composition_states_table)
            .where(composition_states_table.c.id == str(saved.id))
            .values(metadata_={"_version": 1, "data": {"name": None, "description": "tampered"}})
        )
    with pytest.raises(AuditIntegrityError, match="immutable graph version"):
        await service.get_state(saved.id)


@pytest.mark.asyncio
async def test_repository_readers_reject_stale_publication_before_derived_mutation(store):
    service, engine = store
    session = await service.create_session(user_id="alice", title="Content", auth_provider_type="local")
    saved = await service.save_composition_state(session.id, _data(), provenance="session_seed")
    table = composition_states_table
    active_projection = select(
        table.c.id.label("state_id"),
        table.c.session_id.label("state_session_id"),
        table.c.version.label("state_version"),
        table.c.source,
        table.c.sources,
        table.c.nodes,
        table.c.edges,
        table.c.outputs,
        table.c.metadata_,
        table.c.is_valid,
        table.c.validation_errors,
        table.c.created_at,
        table.c.derived_from_state_id,
        table.c.composer_meta,
    ).where(table.c.id == str(saved.id))
    with engine.begin() as connection:
        row = connection.execute(select(table).where(table.c.id == str(saved.id))).one()
        assert read_content_publication(_RepositoryInterpretationMutations._state_record(row)) == read_content_publication(saved)
        assert read_content_publication(_active_run_state_record(connection.execute(active_projection).one())) == read_content_publication(
            saved
        )
        connection.execute(
            update(table).where(table.c.id == str(saved.id)).values(metadata_={"_version": 1, "data": {"description": "changed"}})
        )
        row = connection.execute(select(table).where(table.c.id == str(saved.id))).one()
        with pytest.raises(AuditIntegrityError, match="immutable graph version"):
            _RepositoryInterpretationMutations._state_record(row)
        with pytest.raises(AuditIntegrityError, match="immutable graph version"):
            _active_run_state_record(connection.execute(active_projection).one())


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("state_version", True),
        ("state_version", 0),
        ("applicability", "clean"),
        ("allow", True),
        ("storage_content_hash", "0" * 63),
        ("state_id", "not-a-uuid"),
    ],
)
def test_publication_is_closed_versioned_identity_and_cannot_grant_permission(field, value):
    from uuid import UUID

    publication = ContentPublication(UUID(int=1), UUID(int=2), 1, "a" * 64)
    assert ContentPublication.from_payload(publication.to_payload()) == publication
    with pytest.raises(AuditIntegrityError):
        ContentPublication.from_payload({**publication.to_payload(), field: value})


def test_storage_digest_preserves_json_number_types_nulls_and_authored_order():
    def digest(value):
        return storage_content_hash(CompositionStateData(nodes=[{"id": "number", "options": {"number": value}}]))

    assert len({digest(1), digest(1.0), digest(True)}) == 3
    assert digest(2**60) != digest(2**60 + 1)
    assert storage_content_hash(CompositionStateData(nodes=None)) != storage_content_hash(CompositionStateData(nodes=[]))
    first = {"first": {"plugin": "csv"}, "second": {"plugin": "csv"}}
    assert storage_content_hash(CompositionStateData(sources=first)) != storage_content_hash(
        CompositionStateData(sources=dict(reversed(list(first.items()))))
    )


@pytest.mark.asyncio
async def test_storage_number_type_binding_survives_database_roundtrip(store):
    service, _engine = store
    session = await service.create_session(user_id="alice", title="Numbers", auth_provider_type="local")
    saved = []
    for value in (1, 1.0, 2**60 + 1):
        row = await service.save_composition_state(
            session.id, CompositionStateData(nodes=[{"id": "number", "options": {"number": value}}]), provenance="session_seed"
        )
        saved.append(await service.get_state(row.id))
    assert len({read_content_publication(row).storage_content_hash for row in saved}) == 3
    assert [type(row.nodes[0]["options"]["number"]) for row in saved] == [int, float, int]
