"""Frozen shared-library publication and curation against a real sessions store."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, insert, select, update
from sqlalchemy.exc import IntegrityError
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.web.composer import yaml_generator as yaml_module
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.yaml_generator import generate_public_yaml, public_projection_digest, sources_reading_uploaded_blobs
from elspeth.web.coordination import library_authority as library_module
from elspeth.web.coordination.library_authority import (
    CuratorAuthorityRequired,
    LibraryCompartmentNotConfigured,
    LibraryCurated,
    LibraryCuratorIsPublisher,
    LibraryEntryAlreadyCurated,
    LibraryEntryNeedsProfileBoundSource,
    LibraryEntryNotForkable,
    LibraryEntryNotFound,
    LibraryForkerNotActive,
    LibraryNoteTooLong,
    LibraryPublished,
    LibraryPublisherNotActive,
    LibraryRejectionNoteRequired,
    RepositoryLibraryAuthority,
)
from elspeth.web.sessions.models import identities_table, identity_roles_table, library_entries_table


def _state(*, blob_ref: str | None = None) -> CompositionState:
    options: dict[str, Any] = {"path": "/private/input.csv", "schema": {"fields": ["name"]}}
    if blob_ref is not None:
        options["blob_ref"] = blob_ref
    return CompositionState(
        source=SourceSpec(plugin="csv", on_success="src_out", options=options, on_validation_failure="discard"),
        nodes=(
            NodeSpec(
                id="pass",
                node_type="transform",
                plugin="passthrough",
                input="src_out",
                on_success="out",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(OutputSpec(name="out", plugin="csv", options={"path": "/private/out.csv"}, on_write_failure="discard"),),
        metadata=PipelineMetadata(name="library fixture", description="publishable"),
        version=1,
    )


def _authority(engine: Engine, tmp_path: Path) -> RepositoryLibraryAuthority:
    with engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol"):
            ensure_test_identity(conn, identity_id=identity_id)
    _grant(engine, "alice", role="user")
    _grant(engine, "bob", role="user")
    return RepositoryLibraryAuthority(engine, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))


def _grant(
    engine: Engine,
    identity_id: str,
    *,
    role: str = "curator",
    expires_at: datetime | None = None,
    scope: str | None = None,
) -> str:
    role_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            insert(identity_roles_table).values(
                role_id=role_id,
                identity_id=identity_id,
                role=role,
                expires_at=expires_at,
                note=None,
                scope=scope,
                granted_by_identity_id=identity_id,
                granted_at=datetime.now(UTC),
                revoked_at=None,
            )
        )
    return role_id


def _publish(
    authority: RepositoryLibraryAuthority,
    *,
    published_by: str = "alice",
    state: CompositionState | None = None,
    title: str = "classify",
    compartment_id: str | None = "alpha",
    record: Any = lambda event: None,
):
    return authority.publish(
        session_id="origin-session",
        state=_state() if state is None else state,
        title=title,
        published_by=published_by,
        compartment_id=compartment_id,
        record=record,
    )


def test_projection_is_exact_public_yaml_and_blob_binding_is_refused(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    events: list[LibraryPublished] = []
    entry = _publish(authority, record=events.append)
    assert entry.payload_digest == public_projection_digest(_state())
    assert authority._payload_store.retrieve(entry.payload_digest) == generate_public_yaml(_state()).encode()
    assert entry.published_from_session_id == "origin-session"
    assert entry.compartment_id == "alpha"
    assert entry.state == "pending"
    assert events == [LibraryPublished(entry=entry, actor_identity_id="alice")]
    assert "/private/input.csv" not in authority._payload_store.retrieve(entry.payload_digest).decode()
    assert sources_reading_uploaded_blobs(_state(blob_ref="blob-1")) == ("source",)
    with pytest.raises(LibraryEntryNeedsProfileBoundSource) as refused:
        _publish(authority, state=_state(blob_ref="blob-1"))
    assert refused.value.source_names == ("source",)
    with engine.connect() as conn:
        assert len(conn.execute(select(library_entries_table)).all()) == 1


def test_publish_requires_compartment_and_active_publisher(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    with pytest.raises(LibraryCompartmentNotConfigured):
        _publish(authority, compartment_id=None)
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))
    with pytest.raises(LibraryPublisherNotActive):
        _publish(authority)
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="active"))
    assert _publish(authority).published_by_identity_id == "alice"


def test_publish_requires_live_deployment_user_grant(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    with pytest.raises(LibraryPublisherNotActive):
        _publish(authority, published_by="carol")
    expired = _grant(engine, "carol", role="user", expires_at=datetime.now(UTC) - timedelta(days=1))
    with pytest.raises(LibraryPublisherNotActive):
        _publish(authority, published_by="carol")
    with engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == expired).values(revoked_at=datetime.now(UTC)))
    scoped = _grant(engine, "carol", role="user", scope="another-library")
    with pytest.raises(LibraryPublisherNotActive):
        _publish(authority, published_by="carol")
    with engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == scoped).values(revoked_at=datetime.now(UTC)))
    live = _grant(engine, "carol", role="user")
    assert _publish(authority, published_by="carol").published_by_identity_id == "carol"
    with engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == live).values(revoked_at=datetime.now(UTC)))
    with pytest.raises(LibraryPublisherNotActive):
        _publish(authority, published_by="carol")


def test_service_identity_cannot_publish_or_curate_even_if_active(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    entry = _publish(authority)
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "bob").values(kind="service"))
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "carol").values(kind="service"))
    _grant(engine, "carol")
    with pytest.raises(LibraryPublisherNotActive):
        _publish(authority, published_by="bob")
    with pytest.raises(CuratorAuthorityRequired):
        authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)


def test_historical_service_provider_row_cannot_publish_or_curate_when_kind_is_human(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    entry = _publish(authority)
    _grant(engine, "carol")
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "bob").values(provider="service"))
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "carol").values(provider="service"))
    with pytest.raises(LibraryPublisherNotActive):
        _publish(authority, published_by="bob")
    with pytest.raises(CuratorAuthorityRequired):
        authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)


def test_versions_are_per_publisher_and_title_and_audit_failure_rolls_back(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    assert [_publish(authority).version, _publish(authority).version] == [1, 2]
    assert _publish(authority, title="other").version == 1
    assert _publish(authority, published_by="bob").version == 1

    def fail_audit(event: LibraryPublished) -> None:
        raise RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        _publish(authority, title="rollback", record=fail_audit)
    assert _publish(authority, title="rollback").version == 1


def test_publish_hashes_the_bytes_it_actually_stores(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority = _authority(engine, tmp_path)
    calls = 0
    original_render = generate_public_yaml

    def changing_render(state: CompositionState) -> str:
        nonlocal calls
        calls += 1
        rendered = original_render(state)
        return rendered if calls == 1 else rendered + "# changed on another render\n"

    monkeypatch.setattr(library_module, "generate_public_yaml", changing_render)
    monkeypatch.setattr(yaml_module, "generate_public_yaml", changing_render)
    entry = _publish(authority)
    assert calls == 1
    assert authority._payload_store.retrieve(entry.payload_digest) == original_render(_state()).encode("utf-8")


def test_curator_grant_is_live_and_publisher_cannot_curate(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    entry = _publish(authority)
    with pytest.raises(CuratorAuthorityRequired):
        authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    expired_role_id = _grant(engine, "carol", expires_at=datetime.now(UTC) - timedelta(days=1))
    with pytest.raises(CuratorAuthorityRequired):
        authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    with engine.begin() as conn:
        conn.execute(
            update(identity_roles_table).where(identity_roles_table.c.role_id == expired_role_id).values(revoked_at=datetime.now(UTC))
        )
    scoped_role_id = _grant(engine, "carol", scope="another-library")
    with pytest.raises(CuratorAuthorityRequired):
        authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    with engine.begin() as conn:
        conn.execute(
            update(identity_roles_table).where(identity_roles_table.c.role_id == scoped_role_id).values(revoked_at=datetime.now(UTC))
        )
    role_id = _grant(engine, "carol")
    events: list[LibraryCurated] = []
    accepted = authority.accept(entry_id=entry.entry_id, curator="carol", note="good", record=events.append)
    assert accepted.state == "accepted"
    assert authority.browse() == (accepted,)
    assert events == [LibraryCurated(entry=accepted, action="accepted", actor_identity_id="carol", note="good")]
    second = _publish(authority, title="second")
    with engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == role_id).values(revoked_at=datetime.now(UTC)))
    with pytest.raises(CuratorAuthorityRequired):
        authority.accept(entry_id=second.entry_id, curator="carol", note=None, record=lambda event: None)
    _grant(engine, "alice")
    with pytest.raises(LibraryCuratorIsPublisher):
        authority.accept(entry_id=second.entry_id, curator="alice", note=None, record=lambda event: None)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            update(library_entries_table)
            .where(library_entries_table.c.entry_id == second.entry_id)
            .values(
                curated_by_identity_id="alice",
                accepted_at=datetime.now(UTC),
            )
        )


def test_curation_queue_checks_live_identity_provider_and_role(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    entry = _publish(authority)
    with pytest.raises(CuratorAuthorityRequired):
        authority.curation_queue(curator_identity_id="carol", provider="local")
    role_id = _grant(engine, "carol")
    assert authority.curation_queue(curator_identity_id="carol", provider="local") == (entry,)
    with pytest.raises(CuratorAuthorityRequired):
        authority.curation_queue(curator_identity_id="carol", provider="oidc")
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "carol").values(access_state="disabled"))
    with pytest.raises(CuratorAuthorityRequired):
        authority.curation_queue(curator_identity_id="carol", provider="local")
    with engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == role_id).values(revoked_at=None))
        conn.execute(
            update(identities_table).where(identities_table.c.identity_id == "carol").values(access_state="active", kind="service")
        )
    with pytest.raises(CuratorAuthorityRequired):
        authority.curation_queue(curator_identity_id="carol", provider="local")
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "carol").values(kind="human", provider="service"))
    with pytest.raises(CuratorAuthorityRequired):
        authority.curation_queue(curator_identity_id="carol", provider="service")
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "carol").values(access_state="active"))
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == role_id).values(revoked_at=datetime.now(UTC)))
    with pytest.raises(CuratorAuthorityRequired):
        authority.curation_queue(curator_identity_id="carol", provider="local")


def test_reject_deprecate_recall_and_fork_state(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    _grant(engine, "carol")
    pending = _publish(authority)
    assert authority.curation_queue(curator_identity_id="carol", provider="local") == (pending,)
    with pytest.raises(LibraryEntryNotForkable):
        authority.authorize_fork_source(entry_id=pending.entry_id, forker_identity_id="bob", provider="local")
    with pytest.raises(LibraryRejectionNoteRequired):
        authority.reject(entry_id=pending.entry_id, curator="carol", note=" ", record=lambda event: None)
    with pytest.raises(LibraryNoteTooLong):
        authority.reject(entry_id=pending.entry_id, curator="carol", note="x" * 4097, record=lambda event: None)
    rejected = authority.reject(entry_id=pending.entry_id, curator="carol", note="wrong", record=lambda event: None)
    assert rejected.state == "rejected"
    assert rejected.rejection_note == "wrong"
    with pytest.raises(LibraryEntryAlreadyCurated) as refused:
        authority.accept(entry_id=pending.entry_id, curator="carol", note=None, record=lambda event: None)
    assert refused.value.current_state == "rejected"
    accepted = _publish(authority, title="accepted")
    accepted = authority.accept(entry_id=accepted.entry_id, curator="carol", note=None, record=lambda event: None)
    assert authority.authorize_fork_source(
        entry_id=accepted.entry_id, forker_identity_id="bob", provider="local"
    ).payload_yaml == generate_public_yaml(_state())
    deprecated = authority.deprecate(entry_id=accepted.entry_id, curator="carol", note="superseded", record=lambda event: None)
    assert deprecated.state == "deprecated"
    assert deprecated in authority.browse()
    recalled = authority.recall(entry_id=accepted.entry_id, curator="carol", note="unsafe", record=lambda event: None)
    assert recalled.state == "recalled"
    assert authority.browse() == ()
    with pytest.raises(LibraryEntryNotForkable):
        authority.authorize_fork_source(entry_id=accepted.entry_id, forker_identity_id="bob", provider="local")
    assert authority.read(entry_id=accepted.entry_id) == recalled
    assert {entry.entry_id for entry in authority.published_by(identity_id="alice")} == {pending.entry_id, recalled.entry_id}


def test_library_note_limit_is_utf8_bytes(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    _grant(engine, "carol")
    entry = _publish(authority)
    with pytest.raises(LibraryNoteTooLong):
        authority.reject(entry_id=entry.entry_id, curator="carol", note="é" * 2049, record=lambda event: None)
    rejected = authority.reject(entry_id=entry.entry_id, curator="carol", note="é" * 2048, record=lambda event: None)
    assert rejected.rejection_note == "é" * 2048
    second = _publish(authority, title="second")
    with pytest.raises(LibraryNoteTooLong):
        authority.accept(entry_id=second.entry_id, curator="carol", note="é" * 2049, record=lambda event: None)


def test_missing_or_corrupt_projection_is_integrity_failure(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    _grant(engine, "carol")
    entry = _publish(authority)
    authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    authority._payload_store.delete(entry.payload_digest)
    with pytest.raises(AuditIntegrityError, match="payload"):
        authority.authorize_fork_source(entry_id=entry.entry_id, forker_identity_id="bob", provider="local")


def test_fork_authorization_returns_frozen_source_and_recall_refuses_new_authorization(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    _grant(engine, "carol")
    entry = _publish(authority)
    authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    authorized = authority.authorize_fork_source(entry_id=entry.entry_id, forker_identity_id="bob", provider="local")
    assert authorized.entry.compartment_id == "alpha"
    assert authorized.payload_yaml == generate_public_yaml(_state())
    authority.recall(entry_id=entry.entry_id, curator="carol", note="withdrawn", record=lambda event: None)
    assert authorized.payload_yaml == generate_public_yaml(_state())
    with pytest.raises(LibraryEntryNotForkable) as refused:
        authority.authorize_fork_source(entry_id=entry.entry_id, forker_identity_id="bob", provider="local")
    assert refused.value.current_state == "recalled"


def test_fork_authorization_requires_active_same_provider_user(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    _grant(engine, "carol")
    entry = _publish(authority)
    authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    with pytest.raises(LibraryForkerNotActive):
        authority.authorize_fork_source(entry_id=entry.entry_id, forker_identity_id="carol", provider="local")
    with pytest.raises(LibraryForkerNotActive):
        authority.authorize_fork_source(entry_id=entry.entry_id, forker_identity_id="bob", provider="oidc")
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "bob").values(access_state="disabled"))
    with pytest.raises(LibraryForkerNotActive):
        authority.authorize_fork_source(entry_id=entry.entry_id, forker_identity_id="bob", provider="local")


def test_projection_helpers_use_exact_public_bytes_and_both_upload_keys() -> None:
    base = _state()
    assert public_projection_digest(base) == hashlib.sha256(generate_public_yaml(base).encode("utf-8")).hexdigest()
    alternate_path = replace(
        base,
        sources={
            "source": replace(base.sources["source"], options={**base.sources["source"].options, "path": "/another/private/path"}),
        },
    )
    assert public_projection_digest(alternate_path) == public_projection_digest(base)
    for key in ("blob_ref", "blob_id"):
        bound = replace(
            base,
            sources={
                "source": replace(base.sources["source"], options={**base.sources["source"].options, key: "blob-1"}),
            },
        )
        assert sources_reading_uploaded_blobs(bound) == ("source",)
        unbound = replace(
            base,
            sources={
                "source": replace(base.sources["source"], options={**base.sources["source"].options, key: None}),
            },
        )
        assert sources_reading_uploaded_blobs(unbound) == ()


def test_curator_audit_failure_rolls_back_and_missing_entry_is_distinct(engine: Engine, tmp_path: Path) -> None:
    authority = _authority(engine, tmp_path)
    _grant(engine, "carol")
    entry = _publish(authority)

    def fail_audit(event: LibraryCurated) -> None:
        raise RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=fail_audit)
    assert authority.read(entry_id=entry.entry_id).state == "pending"
    with pytest.raises(LibraryEntryNotFound):
        authority.read(entry_id="missing")
