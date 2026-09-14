### Task I5: Shared library — publish, curate, browse, fork

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: I3. Runs before: I6. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

**Files:**
- Create: `src/elspeth/web/coordination/library_authority.py` (sole writer of `library_entries`; engine-owning, global scope)
- Create: `src/elspeth/web/sessions/routes/workflow/library.py` (`create_library_router()`; the package `src/elspeth/web/sessions/routes/workflow/__init__.py` is created by Task I3)
- Modify: `src/elspeth/web/composer/yaml_generator.py:779-804` (`_PUBLIC_SOURCE_LINKAGE_KEYS` at :779 and `public_export_redaction` at :782; add `sources_reading_uploaded_blobs` beside them) and `:852-881` (`generate_public_yaml`; add `public_projection_digest` after it; add `import hashlib` to the imports at :30-37, which today carry `yaml` at :37 and no `hashlib`)
- Modify: `src/elspeth/web/sessions/routes/composer/state.py:805-917` (`import_state_yaml`: decorator :805, `def` :809, `finally: await lease.close()` :916-917; extract its body into `seed_state_from_runtime_yaml` so the fork route re-imports through the same validators)
- Modify: `src/elspeth/web/auth/audit.py:238-252` (`AuthAuditWriter.record_relationship_changed`, the Protocol's last method) and `:268-287` (`AuthAuditOperation`, last member `RELATIONSHIP_CHANGED` at :287) and `:1140-1183` (`AuthAuditRecorder.record_relationship_changed`, the recorder's last method): five new writers `record_library_published` / `_accepted` / `_rejected` / `_deprecated` / `_recalled`
- Modify: `src/elspeth/web/app.py:640` (`app.state.payload_store = payload_store`, inside `_service_lifespan` :527 — the store does not exist in `_create_app`, so the library authority is built here, not beside `identity_authority` at :1522) and `:1791` (`app.include_router(create_shareable_reviews_router())`, the last registration)
- Modify: `tests/unit/architecture/test_session_db_mutation_authority.py:262-908` (`_NAMED_AUTHORITY_SYMBOLS`), `:914-1193` (`_CONTAINED_CONNECTION_AUTHORITIES`), `:1198` (`_REVIEWED_WRITERS`), `:3954` (`_REVIEWED_READ_CONNECTIONS`)
- Test: `tests/unit/web/coordination/test_library_authority.py` (new), `tests/unit/web/composer/test_yaml_generator.py` (append two tests), `tests/unit/web/auth/test_audit.py` (append one test; helpers `_durable_recorder` :631, `_durable_rows` :634, `_metadata` :644, `_request` :131 already exist), `tests/integration/web/workflow/test_library.py` (new), `tests/testcontainer/web/test_library_postgres.py` (new)

**Interfaces:**
- Consumes:
  - Task I0: `library_entries_table` exactly as on HEAD (`sessions/models.py:3785-3825`; I0 changes nothing in it): `entry_id`, `published_from_session_id` (provenance, not an FK), `payload_digest`, `compartment_id` (NOT NULL, :3796), `title`, `version`, `published_by_identity_id`, `curated_by_identity_id`, `published_at`, `accepted_at`, `rejected_at`, `rejection_note`, `deprecated_at`, `recalled_at`, `note`, CHECK `ck_library_entries_curator_is_not_publisher` (:3820-3823).
  - Task I8: `WebSettings.workflow_governance: Literal["off", "on"] = "off"` (config.py) and the `_check_auth_mode` rule that `on` requires `compartment_id` (readiness.py:315).
  - Task I3: the router package `src/elspeth/web/sessions/routes/workflow/__init__.py`.
  - HEAD: `RepositoryIdentityAuthority.holds_active_role(*, identity_id, role)` (identity_authority.py:1285) and `app.state.identity_authority` (app.py:1522); `FilesystemPayloadStore.store(content) -> sha256 hex` / `.retrieve(content_hash)` (core/payload_store.py:211/265) and `app.state.payload_store` (app.py:640); `generate_public_yaml` (yaml_generator.py:852), `reattach_guided_blob_refs_for_public_export` (:486), `_PUBLIC_SOURCE_LINKAGE_KEYS = frozenset({"blob_ref", "blob_id"})` (:779); `state_from_record` (sessions/converters.py:29); `merge_composer_meta_updates` (routes/_helpers.py:958); `_verify_session_ownership` (routes/_helpers.py:2527); `SessionServiceProtocol.create_session(user_id, title, auth_provider_type)` (service.py:6871), `.get_current_state(session_id)` (:9940), `.archive_session(session_id)` (:7012); `database_now(conn)` (coordination/database_clock.py); `ensure_test_identity` (tests/fixtures/identities.py:10); the `engine` fixture (tests/unit/web/conftest.py:63); `_lifespan_test_client` (tests/integration/web/conftest.py:291), `_seed_session_with_state` (:439), `_save_composition_state_with_compose_authority` (:86), `_passthrough_composition_state` (:360); `external_deployment_postgres_url` (tests/testcontainer/web/conftest.py:40).
- Produces:
  - `RepositoryLibraryAuthority(engine: Engine, *, payload_store: PayloadStore)` in `src/elspeth/web/coordination/library_authority.py`, engine-owning (opens its own transaction per method) because `library_entries` is `global` scope in `_TABLE_POLICIES` (:113; comment :87-90) and the session-operation fence does not apply — the identity_authority.py precedent, not I3's `connection_token` pattern:
    - `.publish(*, session_id: str, state: CompositionState, title: str, published_by: str, compartment_id: str | None, record: Callable[[LibraryPublished], None]) -> LibraryEntryRecord`
    - `.accept(*, entry_id: str, curator: str, note: str | None, record: Callable[[LibraryCurated], None]) -> LibraryEntryRecord`; `.reject(*, entry_id, curator, note, record)` (`note` required non-blank); `.deprecate(*, entry_id, curator, note, record)`; `.recall(*, entry_id, curator, note, record)` (all four are curator acts; the last curator act wins `curated_by_identity_id`, the audit trail carries each)
    - reads: `.browse() -> tuple[LibraryEntryRecord, ...]` (predicate `accepted_at IS NOT NULL AND recalled_at IS NULL`, deprecated entries stay visible with `deprecated_at` set), `.curation_queue()` (no `accepted_at`/`rejected_at`/`recalled_at`), `.published_by(*, identity_id)`, `.read(*, entry_id)`, `.fork_source(*, entry_id) -> LibraryForkSource(entry, payload_yaml)` (same predicate as browse; a live row whose payload is missing from the store raises `AuditIntegrityError`, never 404).
  - `LibraryEntryRecord` (frozen dataclass, field order): `entry_id, published_from_session_id, payload_digest, compartment_id, title, version, published_by_identity_id, curated_by_identity_id, published_at, accepted_at, rejected_at, rejection_note, deprecated_at, recalled_at, note`; property `state: LibraryEntryState = Literal["pending", "accepted", "rejected", "deprecated", "recalled"]`. Task I6 edits this module to stamp `compartment_id` from settings; Task I9 renders these fields.
  - Events handed to `record`: `LibraryPublished(entry, actor_identity_id)`, `LibraryCurated(entry, action: Literal["accepted", "rejected", "deprecated", "recalled"], actor_identity_id, note)`.
  - Refusals (closed set, base `LibraryAuthorityRefusal(RuntimeError)`): `LibraryCompartmentNotConfigured`, `LibraryEntryNeedsProfileBoundSource(source_names)`, `LibraryPublisherNotActive`, `CuratorAuthorityRequired`, `LibraryCuratorIsPublisher`, `LibraryEntryNotFound(entry_id)`, `LibraryEntryAlreadyCurated(current_state)`, `LibraryRejectionNoteRequired`, `LibraryNoteTooLong`, `LibraryEntryNotForkable(current_state)`. `MAX_LIBRARY_NOTE_LENGTH = 4096`.
  - `sources_reading_uploaded_blobs(state: CompositionState) -> tuple[str, ...]` and `public_projection_digest(state: CompositionState) -> str` (sha256 hex of the UTF-8 bytes `generate_public_yaml` returns — the row's `payload_digest` and the payload store's content address are the same string) in `yaml_generator.py`.
  - `seed_state_from_runtime_yaml(*, session: SessionRecord, body: ImportStateYamlRequest, request: Request, user: UserIdentity, composer_meta_updates: Mapping[str, Any] | None = None) -> CompositionStateResponse` in `routes/composer/state.py` — the body of `import_state_yaml` unchanged, plus the optional `composer_meta` merge.
  - Routes (byte-exact), factory `create_library_router() -> APIRouter` in `routes/workflow/library.py`, registered in `web/app.py`: `POST /api/sessions/{session_id}/library/publish` (201), `GET /api/library?view=accepted|queue|mine` (`queue` is curator-only and hidden as 404 otherwise; `accepted` omits `published_from_session_id` unless the viewer is the publisher), `POST /api/library/{entry_id}/accept`, `/reject`, `/deprecate`, `/recall` (curator-only, hidden 404 otherwise), `POST /api/library/{entry_id}/fork` (201 `{session_id, state_id}`).
  - HTTP error envelope `{"error_type": <type>, "detail": <text>}`: 409 `workflow_governance_off` (every library route, whenever `settings.workflow_governance != "on"`; the default is `off`, so Task I9's `LibraryBrowser` must render it), 409 `compartment_not_configured`, 409 `library_entry_needs_profile_bound_source` (+ `sources`), 409 `library_publisher_not_active`, 409 `library_curator_is_publisher`, 409 `library_entry_already_curated` (+ `current_state`), 409 `library_rejection_note_required`, 409 `library_note_too_long`, 409 `library_entry_not_forkable` (+ `current_state`), 404 `library_entry_not_found`.
  - Fork provenance: the forked session's seeded composition state carries `composer_meta["library_fork"] = {"entry_id", "payload_digest", "published_from_session_id", "compartment_id", "version"}` with `provenance="session_seed"`; `sessions.forked_from_session_id` stays NULL (see Step 7 for why).
  - `AuthAuditWriter` / `AuthAuditRecorder` (audit.py) gain `record_library_published(request, *, provider, entry_id, publisher_identity_id, actor_identity_id, payload_digest, compartment_id, title, version, published_from_session_id)` and `record_library_accepted` / `record_library_rejected` / `record_library_deprecated` / `record_library_recalled` `(request, *, provider, entry_id, publisher_identity_id, actor_identity_id, payload_digest, compartment_id, note)`. Rows anchor on the publisher (`identity_id`); the curator is `metadata.actor`. The rejection note is 4 KiB on the row and a 512-character `_bounded_text` copy in `metadata_json.note`, the treatment `record_role_changed` gives `note` (:1132).
  - `app.state.library_authority` (built in `_service_lifespan` after `app.state.payload_store`).

- [ ] **Step 1: Write the failing authority tests.**

Create `tests/unit/web/coordination/test_library_authority.py`:

```python
"""``RepositoryLibraryAuthority`` against a real sessions engine.

Every refusal has a fire test and a derivation test: the verdict must flip
when the AUTHORITY ROW changes (the curator grant, the compartment, the
blob binding, the entry state), never when a guard is edited.  The
``curator != publisher`` rule is proven twice — the named refusal before
the UPDATE, and the schema CHECK itself under a direct SQL write.
"""

from __future__ import annotations

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
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.yaml_generator import generate_public_yaml, public_projection_digest
from elspeth.web.coordination.library_authority import (
    CuratorAuthorityRequired,
    LibraryCompartmentNotConfigured,
    LibraryCurated,
    LibraryCuratorIsPublisher,
    LibraryEntryAlreadyCurated,
    LibraryEntryNeedsProfileBoundSource,
    LibraryEntryNotForkable,
    LibraryEntryRecord,
    LibraryNoteTooLong,
    LibraryPublished,
    LibraryPublisherNotActive,
    LibraryRejectionNoteRequired,
    RepositoryLibraryAuthority,
)
from elspeth.web.sessions.models import identities_table, identity_roles_table, library_entries_table

_BLOB_REF = "20b944e3-fd46-434f-b9a2-4fb508db30f0"
_SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _state(*, blob_ref: str | None = None) -> CompositionState:
    options: dict[str, Any] = {"path": "/data/input.csv", "schema": {"fields": ["name"]}}
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
        outputs=(OutputSpec(name="out", plugin="csv", options={"path": "/data/out.csv"}, on_write_failure="discard"),),
        metadata=PipelineMetadata(name="library fixture", description="publishable"),
        version=1,
    )


def _grant_role(engine: Engine, identity_id: str, role: str, *, expires_at: datetime | None = None) -> str:
    role_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            insert(identity_roles_table).values(
                role_id=role_id,
                identity_id=identity_id,
                role=role,
                expires_at=expires_at,
                note=None,
                scope=None,
                granted_by_identity_id=identity_id,
                granted_at=datetime.now(UTC),
                revoked_at=None,
            )
        )
    return role_id


def _revoke_role(engine: Engine, role_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == role_id).values(revoked_at=datetime.now(UTC)))


def _authority(engine: Engine, tmp_path: Path) -> RepositoryLibraryAuthority:
    with engine.begin() as conn:
        for identity_id in ("alice", "carol", "bob"):
            ensure_test_identity(conn, identity_id=identity_id)
    return RepositoryLibraryAuthority(engine, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))


def _publish(
    authority: RepositoryLibraryAuthority,
    *,
    published_by: str = "alice",
    state: CompositionState | None = None,
    compartment_id: str | None = "alpha",
    title: str = "classify tickets",
    events: list[LibraryPublished] | None = None,
) -> LibraryEntryRecord:
    sink: list[LibraryPublished] = [] if events is None else events
    return authority.publish(
        session_id=_SESSION_ID,
        state=_state() if state is None else state,
        title=title,
        published_by=published_by,
        compartment_id=compartment_id,
        record=sink.append,
    )


def _rows(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(select(library_entries_table)).all())


def test_publish_writes_the_projection_under_its_digest_and_records_the_event(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    events: list[LibraryPublished] = []
    entry = _publish(authority, events=events)
    assert entry.payload_digest == public_projection_digest(_state())
    assert authority._payload_store.retrieve(entry.payload_digest) == generate_public_yaml(_state()).encode("utf-8")
    assert (entry.version, entry.state, entry.compartment_id, entry.published_from_session_id) == (1, "pending", "alpha", _SESSION_ID)
    assert entry.curated_by_identity_id is None
    assert events == [LibraryPublished(entry=entry, actor_identity_id="alice")]


def test_publish_version_counts_per_publisher_and_title(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    first = _publish(authority)
    second = _publish(authority)
    other_title = _publish(authority, title="other")
    bob = _publish(authority, published_by="bob")
    assert (first.version, second.version, other_title.version, bob.version) == (1, 2, 1, 1)


def test_publish_refuses_without_a_configured_compartment(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    events: list[LibraryPublished] = []
    with pytest.raises(LibraryCompartmentNotConfigured, match="compartment_id is not configured"):
        _publish(authority, compartment_id=None, events=events)
    assert _rows(engine) == [] and events == []
    # derivation: the same call with the compartment configured is admitted
    assert _publish(authority, compartment_id="alpha", events=events).compartment_id == "alpha"


def test_publish_refuses_a_source_that_reads_an_uploaded_blob(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    with pytest.raises(LibraryEntryNeedsProfileBoundSource, match="publish a profile-bound source instead") as refused:
        _publish(authority, state=_state(blob_ref=_BLOB_REF))
    assert refused.value.source_names == ("source",)
    assert _rows(engine) == []
    # derivation: drop the binding from the SAME state and the publish is admitted
    assert _publish(authority, state=_state(blob_ref=None)).state == "pending"


def test_publish_refuses_an_inactive_publisher(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))
    with pytest.raises(LibraryPublisherNotActive, match="publisher identity is not active"):
        _publish(authority)
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="active"))
    assert _publish(authority).published_by_identity_id == "alice"


def test_a_failed_audit_write_rolls_the_publish_back(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)

    def _boom(event: LibraryPublished) -> None:
        raise RuntimeError("landscape unavailable")

    with pytest.raises(RuntimeError, match="landscape unavailable"):
        authority.publish(session_id=_SESSION_ID, state=_state(), title="t", published_by="alice", compartment_id="alpha", record=_boom)
    assert _rows(engine) == []


def test_accept_makes_the_entry_browseable_and_records_the_curator(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    _grant_role(engine, "carol", "curator")
    entry = _publish(authority)
    assert authority.browse() == ()
    assert [row.entry_id for row in authority.curation_queue()] == [entry.entry_id]
    events: list[LibraryCurated] = []
    accepted = authority.accept(entry_id=entry.entry_id, curator="carol", note="looks right", record=events.append)
    assert (accepted.state, accepted.curated_by_identity_id, accepted.note) == ("accepted", "carol", "looks right")
    assert accepted.accepted_at is not None
    assert authority.browse() == (accepted,)
    assert authority.curation_queue() == ()
    assert events == [LibraryCurated(entry=accepted, action="accepted", actor_identity_id="carol", note="looks right")]


def test_curator_authority_is_derived_from_the_live_grant(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    entry = _publish(authority)
    # no grant at all
    with pytest.raises(CuratorAuthorityRequired, match="active deployment-wide curator role"):
        authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    # an expired grant is not a grant
    _grant_role(engine, "carol", "curator", expires_at=datetime.now(UTC) - timedelta(days=1))
    with pytest.raises(CuratorAuthorityRequired, match="active deployment-wide curator role"):
        authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    # a live grant admits; revoking it refuses again on the next call
    role_id = _grant_role(engine, "carol", "curator")
    second = _publish(authority, title="second")
    assert authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None).state == "accepted"
    _revoke_role(engine, role_id)
    with pytest.raises(CuratorAuthorityRequired, match="active deployment-wide curator role"):
        authority.accept(entry_id=second.entry_id, curator="carol", note=None, record=lambda event: None)


def test_the_publisher_cannot_curate_their_own_entry(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    _grant_role(engine, "alice", "curator")
    entry = _publish(authority)
    with pytest.raises(LibraryCuratorIsPublisher, match="cannot be curated by its publisher"):
        authority.accept(entry_id=entry.entry_id, curator="alice", note=None, record=lambda event: None)
    assert authority.read(entry_id=entry.entry_id).state == "pending"


def test_the_curator_is_not_publisher_rule_holds_in_the_schema(engine, tmp_path) -> None:
    """Change the ROW, not the guard: a direct UPDATE must hit the CHECK."""
    authority = _authority(engine, tmp_path)
    entry = _publish(authority)
    with pytest.raises(IntegrityError, match="ck_library_entries_curator_is_not_publisher"), engine.begin() as conn:
        conn.execute(
            update(library_entries_table)
            .where(library_entries_table.c.entry_id == entry.entry_id)
            .values(curated_by_identity_id="alice", accepted_at=datetime.now(UTC))
        )


def test_reject_requires_a_nonblank_note_bounded_at_4_kib(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    _grant_role(engine, "carol", "curator")
    entry = _publish(authority)
    with pytest.raises(LibraryRejectionNoteRequired, match="rejection needs a non-blank note"):
        authority.reject(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    with pytest.raises(LibraryRejectionNoteRequired, match="rejection needs a non-blank note"):
        authority.reject(entry_id=entry.entry_id, curator="carol", note="   ", record=lambda event: None)
    with pytest.raises(LibraryNoteTooLong, match="exceeds 4096 characters"):
        authority.reject(entry_id=entry.entry_id, curator="carol", note="x" * 4097, record=lambda event: None)
    assert authority.read(entry_id=entry.entry_id).state == "pending"
    events: list[LibraryCurated] = []
    rejected = authority.reject(entry_id=entry.entry_id, curator="carol", note="x" * 4096, record=events.append)
    assert (rejected.state, rejected.rejection_note, rejected.curated_by_identity_id) == ("rejected", "x" * 4096, "carol")
    assert authority.browse() == () and authority.curation_queue() == ()
    assert events[0].action == "rejected" and events[0].note == "x" * 4096


def test_a_decided_entry_reports_its_current_state_instead_of_a_second_decision(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    _grant_role(engine, "carol", "curator")
    entry = _publish(authority)
    authority.reject(entry_id=entry.entry_id, curator="carol", note="not yet", record=lambda event: None)
    with pytest.raises(LibraryEntryAlreadyCurated, match="library entry is rejected") as refused:
        authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    assert refused.value.current_state == "rejected"
    # deprecate applies only to an accepted entry
    pending = _publish(authority, title="pending one")
    with pytest.raises(LibraryEntryAlreadyCurated, match="library entry is pending"):
        authority.deprecate(entry_id=pending.entry_id, curator="carol", note=None, record=lambda event: None)


def test_deprecate_keeps_the_entry_visible_and_flagged(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    _grant_role(engine, "carol", "curator")
    entry = _publish(authority)
    authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    deprecated = authority.deprecate(entry_id=entry.entry_id, curator="carol", note="superseded by v2", record=lambda event: None)
    assert deprecated.state == "deprecated" and deprecated.deprecated_at is not None
    assert authority.browse() == (deprecated,)
    assert authority.fork_source(entry_id=entry.entry_id).entry == deprecated


def test_recall_flags_and_keeps_the_row(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    _grant_role(engine, "carol", "curator")
    entry = _publish(authority)
    authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    recalled = authority.recall(entry_id=entry.entry_id, curator="carol", note="wrong compartment", record=lambda event: None)
    assert recalled.state == "recalled" and recalled.recalled_at is not None and recalled.accepted_at is not None
    assert len(_rows(engine)) == 1
    assert authority.browse() == ()
    with pytest.raises(LibraryEntryNotForkable, match="library entry is recalled"):
        authority.fork_source(entry_id=entry.entry_id)
    with pytest.raises(LibraryEntryAlreadyCurated, match="library entry is recalled"):
        authority.recall(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)


def test_fork_source_serves_the_projection_and_refuses_a_pending_entry(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    _grant_role(engine, "carol", "curator")
    entry = _publish(authority)
    with pytest.raises(LibraryEntryNotForkable, match="library entry is pending"):
        authority.fork_source(entry_id=entry.entry_id)
    authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    source = authority.fork_source(entry_id=entry.entry_id)
    assert source.payload_yaml == generate_public_yaml(_state())
    assert "blob_ref" not in source.payload_yaml and "/data/input.csv" not in source.payload_yaml


def test_a_live_row_whose_payload_is_missing_is_an_integrity_failure(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    _grant_role(engine, "carol", "curator")
    entry = _publish(authority)
    authority.accept(entry_id=entry.entry_id, curator="carol", note=None, record=lambda event: None)
    assert authority._payload_store.delete(entry.payload_digest) is True
    with pytest.raises(AuditIntegrityError, match="payload"):
        authority.fork_source(entry_id=entry.entry_id)


def test_published_by_lists_only_the_publishers_entries(engine, tmp_path) -> None:
    authority = _authority(engine, tmp_path)
    alice_entry = _publish(authority)
    bob_entry = _publish(authority, published_by="bob", title="bobs")
    assert [row.entry_id for row in authority.published_by(identity_id="alice")] == [alice_entry.entry_id]
    assert [row.entry_id for row in authority.published_by(identity_id="bob")] == [bob_entry.entry_id]
```

Append to `tests/unit/web/composer/test_yaml_generator.py` (imports: add `sources_reading_uploaded_blobs`, `public_projection_digest` to the existing `from elspeth.web.composer.yaml_generator import (` block at :21-35; `hashlib` and `replace` are needed — `replace` is already imported at :6):

```python
class TestLibraryProjectionHelpers:
    def test_sources_reading_uploaded_blobs_names_only_bound_sources(self) -> None:
        base = _make_linear_pipeline()
        assert sources_reading_uploaded_blobs(base) == ()
        bound = replace(base, sources={"source": replace(base.sources["source"], options={**base.sources["source"].options, "blob_ref": "20b944e3-fd46-434f-b9a2-4fb508db30f0"})})
        assert sources_reading_uploaded_blobs(bound) == ("source",)
        # ``blob_ref: None`` is the unbound spelling (mirrors ``_has_blob_binding`` at :84)
        unbound = replace(base, sources={"source": replace(base.sources["source"], options={**base.sources["source"].options, "blob_ref": None})})
        assert sources_reading_uploaded_blobs(unbound) == ()

    def test_public_projection_digest_is_the_sha256_of_the_public_yaml_bytes(self) -> None:
        import hashlib

        state = _make_linear_pipeline()
        assert public_projection_digest(state) == hashlib.sha256(generate_public_yaml(state).encode("utf-8")).hexdigest()
        # the digest is over the PROJECTION: stripped custody does not change it
        with_path = replace(state, sources={"source": replace(state.sources["source"], options={**state.sources["source"].options, "path": "/elsewhere/input.csv"})})
        assert public_projection_digest(with_path) == public_projection_digest(state)
```

Append to `tests/unit/web/auth/test_audit.py`:

```python
def test_library_rows_anchor_on_the_publisher_and_carry_digest_and_compartment(tmp_path: Any) -> None:
    recorder, url = _durable_recorder(tmp_path)
    recorder.record_library_published(
        _request(),
        provider="oidc",
        entry_id="entry-1",
        publisher_identity_id="identity-alice",
        actor_identity_id="identity-alice",
        payload_digest="a" * 64,
        compartment_id="alpha",
        title="classify tickets",
        version=1,
        published_from_session_id="session-1",
    )
    curation = {
        "entry_id": "entry-1",
        "publisher_identity_id": "identity-alice",
        "actor_identity_id": "identity-carol",
        "payload_digest": "a" * 64,
        "compartment_id": "alpha",
    }
    recorder.record_library_accepted(_request(), provider="oidc", note=None, **curation)
    recorder.record_library_rejected(None, provider="oidc", note="x" * 4096, **curation)
    recorder.record_library_deprecated(_request(), provider="oidc", note="superseded", **curation)
    recorder.record_library_recalled(_request(), provider="oidc", note=None, **curation)
    rows = _durable_rows(url)
    assert [row.event_type for row in rows] == [
        "library_published",
        "library_accepted",
        "library_rejected",
        "library_deprecated",
        "library_recalled",
    ]
    assert {row.identity_id for row in rows} == {"identity-alice"}, "anchored on the publisher"
    assert all(row.user_id is None and row.username is None for row in rows)
    published = _metadata(rows[0])
    assert (published["actor"], published["entry_id"], published["payload_digest"], published["compartment_id"]) == (
        "identity-alice",
        "entry-1",
        "a" * 64,
        "alpha",
    )
    assert (published["title"], published["version"], published["published_from_session_id"]) == ("classify tickets", 1, "session-1")
    accepted = _metadata(rows[1])
    assert (accepted["actor"], accepted["payload_digest"], accepted["compartment_id"], accepted["note"]) == ("identity-carol", "a" * 64, "alpha", None)
    rejected = _metadata(rows[2])
    assert len(rejected["note"]) == 512, "the 4 KiB row note is bounded to 512 in audit metadata"
    assert rows[2].request_id is None, "a request-less write carries no request columns"
    assert _metadata(rows[3])["note"] == "superseded"
```

- [ ] **Step 2: Run to verify the tests fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/coordination/test_library_authority.py tests/unit/web/composer/test_yaml_generator.py::TestLibraryProjectionHelpers tests/unit/web/auth/test_audit.py::test_library_rows_anchor_on_the_publisher_and_carry_digest_and_compartment -n 0 > /tmp/i5-red.log 2>&1; echo exit=$?`
Expected: `exit=2` (collection error) — `ImportError: cannot import name 'sources_reading_uploaded_blobs' from 'elspeth.web.composer.yaml_generator'` for the generator test, `ModuleNotFoundError: No module named 'elspeth.web.coordination.library_authority'` for the authority file, and `AttributeError: 'AuthAuditRecorder' object has no attribute 'record_library_published'` once the other two import.

- [ ] **Step 3: Add the two projection helpers to `yaml_generator.py`.**

Add `import hashlib` to the stdlib imports (the block at :30-37 has no `hashlib`). Insert after `public_export_redaction` (ends :804):

```python
def sources_reading_uploaded_blobs(state: CompositionState) -> tuple[str, ...]:
    """Names of sources bound to an uploaded blob, in declaration order.

    Decided over the SAME reattached export state and the SAME linkage keys
    :func:`public_export_redaction` scrubs (``_PUBLIC_SOURCE_LINKAGE_KEYS``),
    so the shared library's config-only refusal cannot drift from the scrub.
    A ``None`` value is the unbound spelling (``_has_blob_binding``), not a
    binding. A source whose ``path`` merely points into managed blob storage
    without a linkage key is NOT counted: the projection strips ``path``, so
    the entry never carries it and a fork must re-bind — no custody moves.
    """
    export_state = reattach_guided_blob_refs_for_public_export(state)
    return tuple(
        source_name
        for source_name, source in export_state.sources.items()
        if any(source.options.get(key) is not None for key in _PUBLIC_SOURCE_LINKAGE_KEYS)
    )
```

Insert after `generate_public_yaml` (ends :881):

```python
def public_projection_digest(state: CompositionState) -> str:
    """sha256 of the exact UTF-8 bytes :func:`generate_public_yaml` returns.

    The shared library stores those bytes in the content-addressed payload
    store, whose address is the same sha256 (``FilesystemPayloadStore.store``),
    so ``library_entries.payload_digest`` and the store key are one string.
    """
    return hashlib.sha256(generate_public_yaml(state).encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Write the library authority.**

Create `src/elspeth/web/coordination/library_authority.py`:

```python
"""Sole writer of ``library_entries``: publish, curate, and the library reads.

``library_entries`` is ``global`` scope in the mutation-authority manifest
(no ``session_id``; ``published_from_session_id`` is provenance), so, exactly
like the identity substrate, the session-operation fence does not apply and
this authority owns its own transaction: every mutation opens
``self._engine.begin()``, locks the participating ``identities`` row(s)
``FOR UPDATE`` (identity before entry, the order
``approval_lifecycle_authority.py`` documents), reads the DATABASE clock,
writes, invokes the caller's ``record`` callback, and commits.  A failed
audit write rolls the mutation back (R4).  A caller that audits nothing
passes an explicit no-op and owns that decision; there is no default.

THE CONNECTION NEVER LEAVES THE METHOD THAT OPENED IT (the manifest treats
a connection handed to any callable as an escaped handle), and every
statement is a module-level constant with bound parameters so the manifest
can resolve it.  Helpers below take rows and values, never a connection.

An entry is the public projection (``generate_public_yaml``) and nothing
else: the bytes live in the content-addressed payload store under
``payload_digest`` and the row references its session only as provenance.
A state whose source reads an uploaded blob is refused by name (spec
§Workflow tables): blob custody proves same-principal on fork, and a
cross-identity fork of a blob-backed source cannot copy the blob without
becoming an intra-container exfiltration path.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import bindparam, func, insert, select, update
from sqlalchemy.engine import Engine

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.payload_store import PayloadNotFoundError, PayloadStore
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.yaml_generator import (
    generate_public_yaml,
    public_projection_digest,
    reattach_guided_blob_refs_for_public_export,
    sources_reading_uploaded_blobs,
)
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.models import identities_table, identity_roles_table, library_entries_table

MAX_LIBRARY_NOTE_LENGTH: Final = 4096
"""Spec §Workflow tables: notes are bounded plain text, 4 KiB."""
MAX_LIBRARY_TITLE_LENGTH: Final = 200

LibraryCurationAction = Literal["accepted", "rejected", "deprecated", "recalled"]
LibraryEntryState = Literal["pending", "accepted", "rejected", "deprecated", "recalled"]


# ── refusals (closed set) ────────────────────────────────────────────────


class LibraryAuthorityRefusal(RuntimeError):
    """Base of the closed refusal set; each subclass carries a stable message."""


class LibraryCompartmentNotConfigured(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__("compartment_id is not configured on this deployment; publishing is refused")


class LibraryEntryNeedsProfileBoundSource(LibraryAuthorityRefusal):
    def __init__(self, source_names: tuple[str, ...]) -> None:
        self.source_names = source_names
        super().__init__(f"source(s) {', '.join(source_names)} read an uploaded blob; publish a profile-bound source instead")


class LibraryPublisherNotActive(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__("publisher identity is not active")


class CuratorAuthorityRequired(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__("curator does not hold an active deployment-wide curator role")


class LibraryCuratorIsPublisher(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__("an entry cannot be curated by its publisher")


class LibraryEntryNotFound(LibraryAuthorityRefusal):
    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        super().__init__(f"library entry {entry_id} not found")


class LibraryEntryAlreadyCurated(LibraryAuthorityRefusal):
    def __init__(self, current_state: LibraryEntryState) -> None:
        self.current_state = current_state
        super().__init__(f"library entry is {current_state}; this curation does not apply")


class LibraryRejectionNoteRequired(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__("a rejection needs a non-blank note")


class LibraryNoteTooLong(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__(f"note exceeds {MAX_LIBRARY_NOTE_LENGTH} characters")


class LibraryEntryNotForkable(LibraryAuthorityRefusal):
    def __init__(self, current_state: LibraryEntryState) -> None:
        self.current_state = current_state
        super().__init__(f"library entry is {current_state}, not accepted")


# ── records and events ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LibraryEntryRecord:
    entry_id: str
    published_from_session_id: str | None
    payload_digest: str
    compartment_id: str
    title: str
    version: int
    published_by_identity_id: str
    curated_by_identity_id: str | None
    published_at: datetime
    accepted_at: datetime | None
    rejected_at: datetime | None
    rejection_note: str | None
    deprecated_at: datetime | None
    recalled_at: datetime | None
    note: str | None

    @property
    def state(self) -> LibraryEntryState:
        if self.recalled_at is not None:
            return "recalled"
        if self.deprecated_at is not None:
            return "deprecated"
        if self.rejected_at is not None:
            return "rejected"
        if self.accepted_at is not None:
            return "accepted"
        return "pending"


@dataclass(frozen=True, slots=True)
class LibraryPublished:
    entry: LibraryEntryRecord
    actor_identity_id: str


@dataclass(frozen=True, slots=True)
class LibraryCurated:
    entry: LibraryEntryRecord
    action: LibraryCurationAction
    actor_identity_id: str
    note: str | None


@dataclass(frozen=True, slots=True)
class LibraryForkSource:
    entry: LibraryEntryRecord
    payload_yaml: str


# ── statements (module-level so the manifest can resolve every write) ────
#
# UPDATE statements name their parameters ``target_entry_id`` / ``curated_at``
# / ``curator_identity_id`` / ``note_text`` / ``rejection_text``: SQLAlchemy
# reserves the target table's column names for its own VALUES binds.

_IDENTITY_FOR_UPDATE: Final = (
    select(identities_table.c.identity_id, identities_table.c.access_state)
    .where(identities_table.c.identity_id == bindparam("identity_id"))
    .with_for_update()
)
_ROLE_GRANTS_OF_IDENTITY: Final = select(
    identity_roles_table.c.role,
    identity_roles_table.c.scope,
    identity_roles_table.c.expires_at,
    identity_roles_table.c.revoked_at,
).where(identity_roles_table.c.identity_id == bindparam("identity_id"))
_ENTRY_BY_ID: Final = select(library_entries_table).where(library_entries_table.c.entry_id == bindparam("entry_id"))
_ENTRY_BY_ID_FOR_UPDATE: Final = _ENTRY_BY_ID.with_for_update()
_NEXT_VERSION: Final = select(func.coalesce(func.max(library_entries_table.c.version), 0) + 1).where(
    library_entries_table.c.published_by_identity_id == bindparam("publisher"),
    library_entries_table.c.title == bindparam("title"),
)
_BROWSE: Final = (
    select(library_entries_table)
    .where(library_entries_table.c.accepted_at.is_not(None), library_entries_table.c.recalled_at.is_(None))
    .order_by(library_entries_table.c.accepted_at.desc(), library_entries_table.c.entry_id)
)
_CURATION_QUEUE: Final = (
    select(library_entries_table)
    .where(
        library_entries_table.c.accepted_at.is_(None),
        library_entries_table.c.rejected_at.is_(None),
        library_entries_table.c.recalled_at.is_(None),
    )
    .order_by(library_entries_table.c.published_at, library_entries_table.c.entry_id)
)
_PUBLISHED_BY: Final = (
    select(library_entries_table)
    .where(library_entries_table.c.published_by_identity_id == bindparam("publisher"))
    .order_by(library_entries_table.c.published_at.desc(), library_entries_table.c.entry_id)
)
_UNDECIDED: Final = (
    library_entries_table.c.accepted_at.is_(None),
    library_entries_table.c.rejected_at.is_(None),
    library_entries_table.c.recalled_at.is_(None),
)
_ACCEPT: Final = (
    update(library_entries_table)
    .where(library_entries_table.c.entry_id == bindparam("target_entry_id"), *_UNDECIDED)
    .values(accepted_at=bindparam("curated_at"), curated_by_identity_id=bindparam("curator_identity_id"), note=bindparam("note_text"))
)
_REJECT: Final = (
    update(library_entries_table)
    .where(library_entries_table.c.entry_id == bindparam("target_entry_id"), *_UNDECIDED)
    .values(
        rejected_at=bindparam("curated_at"),
        curated_by_identity_id=bindparam("curator_identity_id"),
        rejection_note=bindparam("rejection_text"),
    )
)
_DEPRECATE: Final = (
    update(library_entries_table)
    .where(
        library_entries_table.c.entry_id == bindparam("target_entry_id"),
        library_entries_table.c.accepted_at.is_not(None),
        library_entries_table.c.deprecated_at.is_(None),
        library_entries_table.c.recalled_at.is_(None),
    )
    .values(deprecated_at=bindparam("curated_at"), curated_by_identity_id=bindparam("curator_identity_id"), note=bindparam("note_text"))
)
_RECALL: Final = (
    update(library_entries_table)
    .where(library_entries_table.c.entry_id == bindparam("target_entry_id"), library_entries_table.c.recalled_at.is_(None))
    .values(recalled_at=bindparam("curated_at"), curated_by_identity_id=bindparam("curator_identity_id"), note=bindparam("note_text"))
)


# ── helpers: rows and values in, never a connection ──────────────────────


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _optional_utc(value: Any) -> datetime | None:
    return None if value is None else _ensure_utc(value)


def _entry_from_row(row: Any) -> LibraryEntryRecord:
    return LibraryEntryRecord(
        entry_id=row.entry_id,
        published_from_session_id=row.published_from_session_id,
        payload_digest=row.payload_digest,
        compartment_id=row.compartment_id,
        title=row.title,
        version=row.version,
        published_by_identity_id=row.published_by_identity_id,
        curated_by_identity_id=row.curated_by_identity_id,
        published_at=_ensure_utc(row.published_at),
        accepted_at=_optional_utc(row.accepted_at),
        rejected_at=_optional_utc(row.rejected_at),
        rejection_note=row.rejection_note,
        deprecated_at=_optional_utc(row.deprecated_at),
        recalled_at=_optional_utc(row.recalled_at),
        note=row.note,
    )


def _holds_active_curator_grant(identity_row: Any, grant_rows: list[Any], now: datetime) -> bool:
    """Unrevoked, unexpired, deployment-wide ``curator`` on an ACTIVE row, at database time."""
    if identity_row is None or identity_row.access_state != "active":
        return False
    return any(
        grant.role == "curator"
        and grant.scope is None
        and grant.revoked_at is None
        and (grant.expires_at is None or _ensure_utc(grant.expires_at) > now)
        for grant in grant_rows
    )


def _require_nonblank(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-blank string")


def _bounded_optional_note(note: object) -> str | None:
    if note is None:
        return None
    if type(note) is not str:
        raise TypeError("note must be a string or None")
    if len(note) > MAX_LIBRARY_NOTE_LENGTH:
        raise LibraryNoteTooLong()
    return note


def _required_rejection_note(note: object) -> str:
    if note is None or type(note) is not str or not note.strip():
        raise LibraryRejectionNoteRequired()
    if len(note) > MAX_LIBRARY_NOTE_LENGTH:
        raise LibraryNoteTooLong()
    return note


# ── the authority ────────────────────────────────────────────────────────


class RepositoryLibraryAuthority:
    __slots__ = ("_engine", "_payload_store")

    def __init__(self, engine: Engine, *, payload_store: PayloadStore) -> None:
        self._engine = engine
        self._payload_store = payload_store

    # -- reads --------------------------------------------------------------

    def read(self, *, entry_id: str) -> LibraryEntryRecord:
        _require_nonblank(entry_id, "entry_id")
        with self._engine.connect() as conn:
            row = conn.execute(_ENTRY_BY_ID, {"entry_id": entry_id}).one_or_none()
        if row is None:
            raise LibraryEntryNotFound(entry_id)
        return _entry_from_row(row)

    def browse(self) -> tuple[LibraryEntryRecord, ...]:
        """Deployment-wide read: accepted and not recalled; deprecated stays visible, flagged."""
        with self._engine.connect() as conn:
            rows = conn.execute(_BROWSE).all()
        return tuple(_entry_from_row(row) for row in rows)

    def curation_queue(self) -> tuple[LibraryEntryRecord, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(_CURATION_QUEUE).all()
        return tuple(_entry_from_row(row) for row in rows)

    def published_by(self, *, identity_id: str) -> tuple[LibraryEntryRecord, ...]:
        _require_nonblank(identity_id, "identity_id")
        with self._engine.connect() as conn:
            rows = conn.execute(_PUBLISHED_BY, {"publisher": identity_id}).all()
        return tuple(_entry_from_row(row) for row in rows)

    def fork_source(self, *, entry_id: str) -> LibraryForkSource:
        """The projection bytes of a browseable entry; a live row with no payload is an integrity failure."""
        entry = self.read(entry_id=entry_id)
        if entry.accepted_at is None or entry.recalled_at is not None:
            raise LibraryEntryNotForkable(entry.state)
        try:
            payload = self._payload_store.retrieve(entry.payload_digest)
        except PayloadNotFoundError as exc:
            raise AuditIntegrityError(f"library entry {entry_id} has no payload under digest {entry.payload_digest}") from exc
        return LibraryForkSource(entry=entry, payload_yaml=payload.decode("utf-8"))

    # -- publish ------------------------------------------------------------

    def publish(
        self,
        *,
        session_id: str,
        state: CompositionState,
        title: str,
        published_by: str,
        compartment_id: str | None,
        record: Callable[[LibraryPublished], None],
    ) -> LibraryEntryRecord:
        _require_nonblank(session_id, "session_id")
        _require_nonblank(title, "title")
        _require_nonblank(published_by, "published_by")
        if len(title) > MAX_LIBRARY_TITLE_LENGTH:
            raise ValueError(f"title exceeds {MAX_LIBRARY_TITLE_LENGTH} characters")
        if type(state) is not CompositionState:
            raise TypeError("state must be an exact CompositionState")
        if compartment_id is None or not compartment_id.strip():
            # Defence in depth beside I8's readiness rule: the column is NOT
            # NULL (models.py:3796) and a bare IntegrityError is not a refusal.
            raise LibraryCompartmentNotConfigured()
        blob_backed = sources_reading_uploaded_blobs(state)
        if blob_backed:
            raise LibraryEntryNeedsProfileBoundSource(blob_backed)
        export_state = reattach_guided_blob_refs_for_public_export(state)
        payload_yaml = generate_public_yaml(export_state)
        payload_digest = public_projection_digest(export_state)
        entry_id = str(uuid.uuid4())
        with self._engine.begin() as conn:
            publisher = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": published_by}).one_or_none()
            if publisher is None or publisher.access_state != "active":
                raise LibraryPublisherNotActive()
            now = database_now(conn)
            version = conn.execute(_NEXT_VERSION, {"publisher": published_by, "title": title}).scalar_one()
            # Content-addressed and idempotent: storing before the INSERT means a
            # committed row always has a retrievable payload; a rolled-back row
            # leaves at most an orphan file under a hash nothing references.
            stored = self._payload_store.store(payload_yaml.encode("utf-8"))
            if stored != payload_digest:
                raise AuditIntegrityError("payload store address differs from the projection digest")
            conn.execute(
                insert(library_entries_table).values(
                    entry_id=entry_id,
                    published_from_session_id=session_id,
                    payload_digest=payload_digest,
                    compartment_id=compartment_id,
                    title=title,
                    version=version,
                    published_by_identity_id=published_by,
                    curated_by_identity_id=None,
                    published_at=now,
                    accepted_at=None,
                    rejected_at=None,
                    rejection_note=None,
                    deprecated_at=None,
                    recalled_at=None,
                    note=None,
                )
            )
            entry = _entry_from_row(conn.execute(_ENTRY_BY_ID, {"entry_id": entry_id}).one())
            record(LibraryPublished(entry=entry, actor_identity_id=published_by))
        return entry

    # -- curation (one method per verb: each statement is executed in its own
    #    public method, which is what the writer manifest can see) ----------

    def accept(self, *, entry_id: str, curator: str, note: str | None, record: Callable[[LibraryCurated], None]) -> LibraryEntryRecord:
        _require_nonblank(entry_id, "entry_id")
        _require_nonblank(curator, "curator")
        note_text = _bounded_optional_note(note)
        with self._engine.begin() as conn:
            curator_row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": curator}).one_or_none()
            grants = conn.execute(_ROLE_GRANTS_OF_IDENTITY, {"identity_id": curator}).all()
            now = database_now(conn)
            if not _holds_active_curator_grant(curator_row, grants, now):
                raise CuratorAuthorityRequired()
            current = conn.execute(_ENTRY_BY_ID_FOR_UPDATE, {"entry_id": entry_id}).one_or_none()
            if current is None:
                raise LibraryEntryNotFound(entry_id)
            if current.published_by_identity_id == curator:
                raise LibraryCuratorIsPublisher()
            result = conn.execute(
                _ACCEPT, {"target_entry_id": entry_id, "curated_at": now, "curator_identity_id": curator, "note_text": note_text}
            )
            if result.rowcount != 1:
                raise LibraryEntryAlreadyCurated(_entry_from_row(current).state)
            entry = _entry_from_row(conn.execute(_ENTRY_BY_ID, {"entry_id": entry_id}).one())
            record(LibraryCurated(entry=entry, action="accepted", actor_identity_id=curator, note=note_text))
        return entry

    def reject(self, *, entry_id: str, curator: str, note: str | None, record: Callable[[LibraryCurated], None]) -> LibraryEntryRecord:
        _require_nonblank(entry_id, "entry_id")
        _require_nonblank(curator, "curator")
        rejection_text = _required_rejection_note(note)
        with self._engine.begin() as conn:
            curator_row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": curator}).one_or_none()
            grants = conn.execute(_ROLE_GRANTS_OF_IDENTITY, {"identity_id": curator}).all()
            now = database_now(conn)
            if not _holds_active_curator_grant(curator_row, grants, now):
                raise CuratorAuthorityRequired()
            current = conn.execute(_ENTRY_BY_ID_FOR_UPDATE, {"entry_id": entry_id}).one_or_none()
            if current is None:
                raise LibraryEntryNotFound(entry_id)
            if current.published_by_identity_id == curator:
                raise LibraryCuratorIsPublisher()
            result = conn.execute(
                _REJECT,
                {"target_entry_id": entry_id, "curated_at": now, "curator_identity_id": curator, "rejection_text": rejection_text},
            )
            if result.rowcount != 1:
                raise LibraryEntryAlreadyCurated(_entry_from_row(current).state)
            entry = _entry_from_row(conn.execute(_ENTRY_BY_ID, {"entry_id": entry_id}).one())
            record(LibraryCurated(entry=entry, action="rejected", actor_identity_id=curator, note=rejection_text))
        return entry

    def deprecate(self, *, entry_id: str, curator: str, note: str | None, record: Callable[[LibraryCurated], None]) -> LibraryEntryRecord:
        _require_nonblank(entry_id, "entry_id")
        _require_nonblank(curator, "curator")
        note_text = _bounded_optional_note(note)
        with self._engine.begin() as conn:
            curator_row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": curator}).one_or_none()
            grants = conn.execute(_ROLE_GRANTS_OF_IDENTITY, {"identity_id": curator}).all()
            now = database_now(conn)
            if not _holds_active_curator_grant(curator_row, grants, now):
                raise CuratorAuthorityRequired()
            current = conn.execute(_ENTRY_BY_ID_FOR_UPDATE, {"entry_id": entry_id}).one_or_none()
            if current is None:
                raise LibraryEntryNotFound(entry_id)
            if current.published_by_identity_id == curator:
                raise LibraryCuratorIsPublisher()
            result = conn.execute(
                _DEPRECATE, {"target_entry_id": entry_id, "curated_at": now, "curator_identity_id": curator, "note_text": note_text}
            )
            if result.rowcount != 1:
                raise LibraryEntryAlreadyCurated(_entry_from_row(current).state)
            entry = _entry_from_row(conn.execute(_ENTRY_BY_ID, {"entry_id": entry_id}).one())
            record(LibraryCurated(entry=entry, action="deprecated", actor_identity_id=curator, note=note_text))
        return entry

    def recall(self, *, entry_id: str, curator: str, note: str | None, record: Callable[[LibraryCurated], None]) -> LibraryEntryRecord:
        """Recall FLAGS, never deletes: the row and its payload stay; browse and fork stop serving it."""
        _require_nonblank(entry_id, "entry_id")
        _require_nonblank(curator, "curator")
        note_text = _bounded_optional_note(note)
        with self._engine.begin() as conn:
            curator_row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": curator}).one_or_none()
            grants = conn.execute(_ROLE_GRANTS_OF_IDENTITY, {"identity_id": curator}).all()
            now = database_now(conn)
            if not _holds_active_curator_grant(curator_row, grants, now):
                raise CuratorAuthorityRequired()
            current = conn.execute(_ENTRY_BY_ID_FOR_UPDATE, {"entry_id": entry_id}).one_or_none()
            if current is None:
                raise LibraryEntryNotFound(entry_id)
            if current.published_by_identity_id == curator:
                raise LibraryCuratorIsPublisher()
            result = conn.execute(
                _RECALL, {"target_entry_id": entry_id, "curated_at": now, "curator_identity_id": curator, "note_text": note_text}
            )
            if result.rowcount != 1:
                raise LibraryEntryAlreadyCurated(_entry_from_row(current).state)
            entry = _entry_from_row(conn.execute(_ENTRY_BY_ID, {"entry_id": entry_id}).one())
            record(LibraryCurated(entry=entry, action="recalled", actor_identity_id=curator, note=note_text))
        return entry
```

Note on SQLite: `with_for_update()` is dropped by the SQLite dialect and `engine.begin()` is `BEGIN IMMEDIATE`, so the read-check-write already runs under the single writer lock; on PostgreSQL the curator's `identities` row and the entry row are locked in that order, and the conditional `UPDATE` makes a concurrent second curator's write return `rowcount == 0` (Step 12 proves it).

- [ ] **Step 5: Add the five audit writers to `audit.py`.**

In `AuthAuditWriter` (Protocol), after `record_relationship_changed` (:238-252):

```python
    def record_library_published(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        entry_id: str,
        publisher_identity_id: str,
        actor_identity_id: str,
        payload_digest: str,
        compartment_id: str,
        title: str,
        version: int,
        published_from_session_id: str | None,
    ) -> None:
        """A ``library_published`` row anchored on the publisher."""

    def record_library_accepted(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        entry_id: str,
        publisher_identity_id: str,
        actor_identity_id: str,
        payload_digest: str,
        compartment_id: str,
        note: str | None,
    ) -> None:
        """A ``library_accepted`` row anchored on the publisher; the curator is ``metadata.actor``."""

    def record_library_rejected(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        entry_id: str,
        publisher_identity_id: str,
        actor_identity_id: str,
        payload_digest: str,
        compartment_id: str,
        note: str | None,
    ) -> None:
        """A ``library_rejected`` row; ``note`` is the rejection note, bounded to 512 here."""

    def record_library_deprecated(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        entry_id: str,
        publisher_identity_id: str,
        actor_identity_id: str,
        payload_digest: str,
        compartment_id: str,
        note: str | None,
    ) -> None:
        """A ``library_deprecated`` row anchored on the publisher."""

    def record_library_recalled(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        entry_id: str,
        publisher_identity_id: str,
        actor_identity_id: str,
        payload_digest: str,
        compartment_id: str,
        note: str | None,
    ) -> None:
        """A ``library_recalled`` row anchored on the publisher."""
```

In `AuthAuditOperation` (:268-287), after `RELATIONSHIP_CHANGED = "relationship_changed"`:

```python
    LIBRARY_PUBLISHED = "library_published"
    LIBRARY_CURATED = "library_curated"
```

Add the alias beside `AdminActivationCause` (:255):

```python
LibraryCurationEventType = Literal["library_accepted", "library_rejected", "library_deprecated", "library_recalled"]
```

In `AuthAuditRecorder`, after `record_relationship_changed` (:1140-1183):

```python
    def record_library_published(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        entry_id: str,
        publisher_identity_id: str,
        actor_identity_id: str,
        payload_digest: str,
        compartment_id: str,
        title: str,
        version: int,
        published_from_session_id: str | None,
    ) -> None:
        """Anchored on the publisher; ``payload_digest`` + ``compartment_id`` make the same artifact detectable across containers (spec §Workflow tables)."""
        provenance = _admin_provenance(request, actor_identity_id=actor_identity_id, on_behalf_of=None, console_request_id=None)
        with self._open_landscape(AuthAuditOperation.LIBRARY_PUBLISHED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="library_published",
                outcome="success",
                provider=provider,
                identity_id=publisher_identity_id,
                user_id=None,
                username=None,
                failure_category=None,
                metadata={
                    **provenance.metadata,
                    "entry_id": entry_id,
                    "payload_digest": payload_digest,
                    "compartment_id": compartment_id,
                    "title": _bounded_text(title),
                    "version": version,
                    "published_from_session_id": published_from_session_id,
                },
                **provenance.request_columns,
            )

    def _record_library_curation(
        self,
        request: Request | None,
        *,
        event_type: LibraryCurationEventType,
        provider: AuthProviderType,
        entry_id: str,
        publisher_identity_id: str,
        actor_identity_id: str,
        payload_digest: str,
        compartment_id: str,
        note: str | None,
    ) -> None:
        provenance = _admin_provenance(request, actor_identity_id=actor_identity_id, on_behalf_of=None, console_request_id=None)
        with self._open_landscape(AuthAuditOperation.LIBRARY_CURATED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type=event_type,
                outcome="success",
                provider=provider,
                identity_id=publisher_identity_id,
                user_id=None,
                username=None,
                failure_category=None,
                metadata={
                    **provenance.metadata,
                    "entry_id": entry_id,
                    "payload_digest": payload_digest,
                    "compartment_id": compartment_id,
                    "note": _bounded_text(note),
                },
                **provenance.request_columns,
            )

    def record_library_accepted(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        entry_id: str,
        publisher_identity_id: str,
        actor_identity_id: str,
        payload_digest: str,
        compartment_id: str,
        note: str | None,
    ) -> None:
        self._record_library_curation(
            request,
            event_type="library_accepted",
            provider=provider,
            entry_id=entry_id,
            publisher_identity_id=publisher_identity_id,
            actor_identity_id=actor_identity_id,
            payload_digest=payload_digest,
            compartment_id=compartment_id,
            note=note,
        )

    def record_library_rejected(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        entry_id: str,
        publisher_identity_id: str,
        actor_identity_id: str,
        payload_digest: str,
        compartment_id: str,
        note: str | None,
    ) -> None:
        self._record_library_curation(
            request,
            event_type="library_rejected",
            provider=provider,
            entry_id=entry_id,
            publisher_identity_id=publisher_identity_id,
            actor_identity_id=actor_identity_id,
            payload_digest=payload_digest,
            compartment_id=compartment_id,
            note=note,
        )

    def record_library_deprecated(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        entry_id: str,
        publisher_identity_id: str,
        actor_identity_id: str,
        payload_digest: str,
        compartment_id: str,
        note: str | None,
    ) -> None:
        self._record_library_curation(
            request,
            event_type="library_deprecated",
            provider=provider,
            entry_id=entry_id,
            publisher_identity_id=publisher_identity_id,
            actor_identity_id=actor_identity_id,
            payload_digest=payload_digest,
            compartment_id=compartment_id,
            note=note,
        )

    def record_library_recalled(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        entry_id: str,
        publisher_identity_id: str,
        actor_identity_id: str,
        payload_digest: str,
        compartment_id: str,
        note: str | None,
    ) -> None:
        self._record_library_curation(
            request,
            event_type="library_recalled",
            provider=provider,
            entry_id=entry_id,
            publisher_identity_id=publisher_identity_id,
            actor_identity_id=actor_identity_id,
            payload_digest=payload_digest,
            compartment_id=compartment_id,
            note=note,
        )
```

All five event types already exist in `AuthAuditEventType` (core/landscape/auth_audit_repository.py:41-45) and in `ck_auth_events_event_type` (core/landscape/schema.py:2573-2574): no Landscape change.

- [ ] **Step 6: Run the unit tests to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/coordination/test_library_authority.py tests/unit/web/composer/test_yaml_generator.py tests/unit/web/auth/test_audit.py -n 0 > /tmp/i5-unit.log 2>&1; echo exit=$?`
Expected: `exit=0`. If `test_the_curator_is_not_publisher_rule_holds_in_the_schema` fails with a message that does not name the constraint, SQLite's `PRAGMA foreign_keys`/CHECK reporting is unchanged and the `match=` is the defect to fix, not the test.

- [ ] **Step 7: Extract the YAML-seed helper and write the routes.**

In `src/elspeth/web/sessions/routes/composer/state.py`, rename the body of `import_state_yaml` (:809-917) into a module-level coroutine and make the route a two-line caller. The helper keeps every line of the current body verbatim from `service: SessionServiceProtocol = request.app.state.session_service` (:816) through `await lease.close()` (:917); the only additions are the `composer_meta_updates` parameter and the three-line merge immediately before `service.save_composition_state_with_interpretations(` (:905):

```python
async def seed_state_from_runtime_yaml(
    *,
    session: SessionRecord,
    body: ImportStateYamlRequest,
    request: Request,
    user: UserIdentity,
    composer_meta_updates: Mapping[str, Any] | None = None,
) -> CompositionStateResponse:
    """Seed ``session`` from runtime YAML through every import validator.

    The body of ``import_state_yaml`` unchanged (elspeth-06f92da0d9 and the
    ``_reject_*`` guards it names).  The library fork re-imports a published
    projection through this same path so a forked pipeline is admitted by
    exactly the rules a pasted one is; ``composer_meta_updates`` lets the
    fork stamp its provenance without a second state write.
    """
    service: SessionServiceProtocol = request.app.state.session_service
    # lines :817-904 of import_state_yaml on HEAD 072141b75 (the lease
    # acquire through the ``interpretation_drafts =`` assignment) are MOVED here
    # unchanged; the three lines below are the only addition
            if composer_meta_updates is not None:
                state_data = replace(
                    state_data,
                    composer_meta=merge_composer_meta_updates(state_data.composer_meta, composer_meta_updates),
                )
            response_state = await service.save_composition_state_with_interpretations(
                session.id,
                state_data,
                provenance="session_seed",
                interpretations=interpretation_drafts,
                session_operation_context=lease.context,
            )
            with _named_guided_custody_projection():
                return _state_response(response_state, policy_catalog=catalog)
    finally:
        await lease.close()


@router.post(
    "/{session_id}/state/yaml",
    response_model=CompositionStateResponse,
)
async def import_state_yaml(
    session_id: UUID,
    body: ImportStateYamlRequest,
    request: Request,
    user: UserIdentity = Depends(get_current_user),  # noqa: B008
) -> CompositionStateResponse:
    """Seed a session's composition state from exported runtime YAML."""
    session = await _verify_session_ownership(session_id, user, request)
    return await seed_state_from_runtime_yaml(session=session, body=body, request=request, user=user)
```

Imports to add at the top of state.py: `from collections.abc import Mapping`, `from dataclasses import replace`, `from typing import Any` (extend the existing `from typing import NotRequired, TypedDict` at :6), `SessionRecord` in the existing `from elspeth.web.sessions.protocol import (` block at :38, and `merge_composer_meta_updates` in the `from .._helpers import (` block at :52. The comment inside the helper stands in for the existing :817-904 text, which is moved into the helper unchanged, not retyped.

Create `src/elspeth/web/sessions/routes/workflow/library.py`:

```python
"""Shared library routes: publish, curate, browse, fork.

Every route refuses 409 ``workflow_governance_off`` unless
``settings.workflow_governance == "on"`` (R11: under open local registration
one human holds many identities and every ``<>`` CHECK is defeatable; Task
I8's readiness check is what makes ``on`` safe).  Curator routes hide
themselves (404) from non-curators exactly as ``identity_admin_routes``
hides admin routes; the authority re-proves the role inside its transaction.

Fork provenance lives in the seeded state's ``composer_meta["library_fork"]``
and ``sessions.forked_from_session_id`` stays NULL: ``list_sessions``
(service.py:6979) hides any session with that column set unless a completed
``session_fork`` guided operation exists, and ``create_session`` (:6871) does
not accept it — setting it would hide the fork from its own owner.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.audit import AuthAuditWriter
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.library_authority import (
    MAX_LIBRARY_NOTE_LENGTH,
    MAX_LIBRARY_TITLE_LENGTH,
    CuratorAuthorityRequired,
    LibraryAuthorityRefusal,
    LibraryCompartmentNotConfigured,
    LibraryCurated,
    LibraryCuratorIsPublisher,
    LibraryEntryAlreadyCurated,
    LibraryEntryNeedsProfileBoundSource,
    LibraryEntryNotForkable,
    LibraryEntryNotFound,
    LibraryEntryRecord,
    LibraryEntryState,
    LibraryNoteTooLong,
    LibraryPublished,
    LibraryPublisherNotActive,
    LibraryRejectionNoteRequired,
    RepositoryLibraryAuthority,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import SessionServiceProtocol
from elspeth.web.sessions.routes._helpers import _verify_session_ownership
from elspeth.web.sessions.routes.composer.state import ImportStateYamlRequest, seed_state_from_runtime_yaml

WORKFLOW_GOVERNANCE_OFF = "workflow_governance_off"
LIBRARY_ENTRY_NOT_FOUND = "library_entry_not_found"

_ERROR_TYPES: dict[type[LibraryAuthorityRefusal], str] = {
    LibraryCompartmentNotConfigured: "compartment_not_configured",
    LibraryEntryNeedsProfileBoundSource: "library_entry_needs_profile_bound_source",
    LibraryPublisherNotActive: "library_publisher_not_active",
    LibraryCuratorIsPublisher: "library_curator_is_publisher",
    LibraryEntryAlreadyCurated: "library_entry_already_curated",
    LibraryRejectionNoteRequired: "library_rejection_note_required",
    LibraryNoteTooLong: "library_note_too_long",
    LibraryEntryNotForkable: "library_entry_not_forkable",
}

LibraryView = Literal["accepted", "queue", "mine"]


class PublishLibraryEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_LIBRARY_TITLE_LENGTH)


class CurateLibraryEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=MAX_LIBRARY_NOTE_LENGTH)


class LibraryEntryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str
    # Only the publisher sees their own staging session id; a browser is
    # handed the projection, never a session reference.
    published_from_session_id: str | None
    payload_digest: str
    compartment_id: str
    title: str
    version: int
    published_by_identity_id: str
    curated_by_identity_id: str | None
    published_at: datetime
    accepted_at: datetime | None
    rejected_at: datetime | None
    rejection_note: str | None
    deprecated_at: datetime | None
    recalled_at: datetime | None
    note: str | None
    state: LibraryEntryState


class LibraryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: LibraryView
    entries: list[LibraryEntryView]


class LibraryForkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    state_id: str


# ── request plumbing ─────────────────────────────────────────────────────


def _settings(request: Request) -> WebSettings:
    settings: WebSettings = request.app.state.settings
    return settings


def _authority(request: Request) -> RepositoryLibraryAuthority:
    authority: RepositoryLibraryAuthority = request.app.state.library_authority
    return authority


def _identity_authority(request: Request) -> RepositoryIdentityAuthority:
    authority: RepositoryIdentityAuthority = request.app.state.identity_authority
    return authority


def _recorder(request: Request) -> AuthAuditWriter:
    recorder: AuthAuditWriter = request.app.state.auth_audit_recorder
    return recorder


def _hidden() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


def _require_workflow_governance(request: Request) -> None:
    if _settings(request).workflow_governance != "on":
        raise HTTPException(
            status_code=409,
            detail={
                "error_type": WORKFLOW_GOVERNANCE_OFF,
                "detail": "workflow governance is off on this deployment (settings.workflow_governance)",
            },
        )


async def _require_governed_user(request: Request, user: UserIdentity = Depends(get_current_user)) -> UserIdentity:  # noqa: B008
    _require_workflow_governance(request)
    return user


async def _require_curator(request: Request, user: UserIdentity = Depends(get_current_user)) -> UserIdentity:  # noqa: B008
    """Admit only a live holder of a deployment-wide ``curator`` role (checked per request, never cached)."""
    _require_workflow_governance(request)
    if not await run_sync_in_worker(_identity_authority(request).holds_active_role, identity_id=user.user_id, role="curator"):
        raise _hidden()
    return user


def _refused(exc: LibraryAuthorityRefusal) -> HTTPException:
    """Exact types: the set is closed, and an unmapped subclass is a defect (KeyError), not a 500 in disguise."""
    if type(exc) is CuratorAuthorityRequired:
        return _hidden()
    if type(exc) is LibraryEntryNotFound:
        return HTTPException(status_code=404, detail={"error_type": LIBRARY_ENTRY_NOT_FOUND, "detail": str(exc)})
    detail: dict[str, object] = {"error_type": _ERROR_TYPES[type(exc)], "detail": str(exc)}
    if type(exc) is LibraryEntryNeedsProfileBoundSource:
        detail["sources"] = list(exc.source_names)
    if type(exc) is LibraryEntryAlreadyCurated or type(exc) is LibraryEntryNotForkable:
        detail["current_state"] = exc.current_state
    return HTTPException(status_code=409, detail=detail)


def _uncacheable(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _view(entry: LibraryEntryRecord, *, viewer: str) -> LibraryEntryView:
    return LibraryEntryView(
        entry_id=entry.entry_id,
        published_from_session_id=entry.published_from_session_id if entry.published_by_identity_id == viewer else None,
        payload_digest=entry.payload_digest,
        compartment_id=entry.compartment_id,
        title=entry.title,
        version=entry.version,
        published_by_identity_id=entry.published_by_identity_id,
        curated_by_identity_id=entry.curated_by_identity_id,
        published_at=entry.published_at,
        accepted_at=entry.accepted_at,
        rejected_at=entry.rejected_at,
        rejection_note=entry.rejection_note,
        deprecated_at=entry.deprecated_at,
        recalled_at=entry.recalled_at,
        note=entry.note,
        state=entry.state,
    )


async def _curate(
    request: Request,
    response: Response,
    *,
    entry_id: str,
    curator: UserIdentity,
    note: str | None,
    action: Literal["accepted", "rejected", "deprecated", "recalled"],
) -> LibraryEntryView:
    settings = _settings(request)
    recorder = _recorder(request)
    authority = _authority(request)
    writers = {
        "accepted": recorder.record_library_accepted,
        "rejected": recorder.record_library_rejected,
        "deprecated": recorder.record_library_deprecated,
        "recalled": recorder.record_library_recalled,
    }
    mutations = {
        "accepted": authority.accept,
        "rejected": authority.reject,
        "deprecated": authority.deprecate,
        "recalled": authority.recall,
    }

    def record(event: LibraryCurated) -> None:
        writers[event.action](
            request,
            provider=settings.auth_provider,
            entry_id=event.entry.entry_id,
            publisher_identity_id=event.entry.published_by_identity_id,
            actor_identity_id=event.actor_identity_id,
            payload_digest=event.entry.payload_digest,
            compartment_id=event.entry.compartment_id,
            note=event.note,
        )

    try:
        entry = await run_sync_in_worker(mutations[action], entry_id=entry_id, curator=curator.user_id, note=note, record=record)
    except LibraryAuthorityRefusal as exc:
        raise _refused(exc) from exc
    _uncacheable(response)
    return _view(entry, viewer=curator.user_id)


# ── router ───────────────────────────────────────────────────────────────


def create_library_router() -> APIRouter:
    """Library routes; paths are absolute because publish hangs off /api/sessions and the rest off /api/library."""
    router = APIRouter(tags=["library"])

    @router.post("/api/sessions/{session_id}/library/publish", status_code=201, response_model=LibraryEntryView)
    async def publish_entry(
        session_id: UUID,
        body: PublishLibraryEntryRequest,
        request: Request,
        response: Response,
        user: UserIdentity = Depends(_require_governed_user),  # noqa: B008
    ) -> LibraryEntryView:
        session = await _verify_session_ownership(session_id, user, request)
        service: SessionServiceProtocol = request.app.state.session_service
        state_record = await service.get_current_state(session.id)
        if state_record is None:
            raise HTTPException(status_code=404, detail="No composition state exists")
        state = state_from_record(state_record)
        settings = _settings(request)
        recorder = _recorder(request)

        def record(event: LibraryPublished) -> None:
            recorder.record_library_published(
                request,
                provider=settings.auth_provider,
                entry_id=event.entry.entry_id,
                publisher_identity_id=event.entry.published_by_identity_id,
                actor_identity_id=event.actor_identity_id,
                payload_digest=event.entry.payload_digest,
                compartment_id=event.entry.compartment_id,
                title=event.entry.title,
                version=event.entry.version,
                published_from_session_id=event.entry.published_from_session_id,
            )

        try:
            entry = await run_sync_in_worker(
                _authority(request).publish,
                session_id=str(session.id),
                state=state,
                title=body.title,
                published_by=user.user_id,
                compartment_id=settings.compartment_id,
                record=record,
            )
        except LibraryAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _view(entry, viewer=user.user_id)

    @router.get("/api/library", response_model=LibraryListResponse)
    async def list_entries(
        request: Request,
        response: Response,
        view: LibraryView = Query("accepted"),  # noqa: B008
        user: UserIdentity = Depends(_require_governed_user),  # noqa: B008
    ) -> LibraryListResponse:
        authority = _authority(request)
        if view == "queue":
            if not await run_sync_in_worker(_identity_authority(request).holds_active_role, identity_id=user.user_id, role="curator"):
                raise _hidden()
            entries = await run_sync_in_worker(authority.curation_queue)
        elif view == "mine":
            entries = await run_sync_in_worker(authority.published_by, identity_id=user.user_id)
        else:
            entries = await run_sync_in_worker(authority.browse)
        _uncacheable(response)
        return LibraryListResponse(view=view, entries=[_view(entry, viewer=user.user_id) for entry in entries])

    @router.post("/api/library/{entry_id}/accept", response_model=LibraryEntryView)
    async def accept_entry(
        entry_id: str,
        body: CurateLibraryEntryRequest,
        request: Request,
        response: Response,
        curator: UserIdentity = Depends(_require_curator),  # noqa: B008
    ) -> LibraryEntryView:
        return await _curate(request, response, entry_id=entry_id, curator=curator, note=body.note, action="accepted")

    @router.post("/api/library/{entry_id}/reject", response_model=LibraryEntryView)
    async def reject_entry(
        entry_id: str,
        body: CurateLibraryEntryRequest,
        request: Request,
        response: Response,
        curator: UserIdentity = Depends(_require_curator),  # noqa: B008
    ) -> LibraryEntryView:
        return await _curate(request, response, entry_id=entry_id, curator=curator, note=body.note, action="rejected")

    @router.post("/api/library/{entry_id}/deprecate", response_model=LibraryEntryView)
    async def deprecate_entry(
        entry_id: str,
        body: CurateLibraryEntryRequest,
        request: Request,
        response: Response,
        curator: UserIdentity = Depends(_require_curator),  # noqa: B008
    ) -> LibraryEntryView:
        return await _curate(request, response, entry_id=entry_id, curator=curator, note=body.note, action="deprecated")

    @router.post("/api/library/{entry_id}/recall", response_model=LibraryEntryView)
    async def recall_entry(
        entry_id: str,
        body: CurateLibraryEntryRequest,
        request: Request,
        response: Response,
        curator: UserIdentity = Depends(_require_curator),  # noqa: B008
    ) -> LibraryEntryView:
        return await _curate(request, response, entry_id=entry_id, curator=curator, note=body.note, action="recalled")

    @router.post("/api/library/{entry_id}/fork", status_code=201, response_model=LibraryForkResponse)
    async def fork_entry(
        entry_id: str,
        request: Request,
        response: Response,
        user: UserIdentity = Depends(_require_governed_user),  # noqa: B008
    ) -> LibraryForkResponse:
        """Instantiate the projection into a NEW session owned by the forker.

        The projection is re-imported through ``seed_state_from_runtime_yaml``
        — the same validators a pasted YAML meets — so nothing here authors
        pipeline structure (composer invariant 1).  A projection whose source
        was custody-stripped seeds a state the importer marks invalid until
        the forker binds their own source; that is the intended shape.
        """
        settings = _settings(request)
        try:
            source = await run_sync_in_worker(_authority(request).fork_source, entry_id=entry_id)
        except LibraryAuthorityRefusal as exc:
            raise _refused(exc) from exc
        service: SessionServiceProtocol = request.app.state.session_service
        session = await service.create_session(user.user_id, f"Fork of {source.entry.title}", settings.auth_provider)
        try:
            seeded = await seed_state_from_runtime_yaml(
                session=session,
                body=ImportStateYamlRequest(yaml=source.payload_yaml, source_blob_ids=None),
                request=request,
                user=user,
                composer_meta_updates={
                    "library_fork": {
                        "entry_id": source.entry.entry_id,
                        "payload_digest": source.entry.payload_digest,
                        "published_from_session_id": source.entry.published_from_session_id,
                        "compartment_id": source.entry.compartment_id,
                        "version": source.entry.version,
                    }
                },
            )
        except HTTPException:
            # The importer refused the projection (a plugin no longer allowed
            # on this deployment, for example): do not leave an empty session
            # behind.  An empty session has no durable history, so archive
            # physically deletes it (repository.py:731-775).
            await service.archive_session(session.id)
            raise
        _uncacheable(response)
        return LibraryForkResponse(session_id=session.id, state_id=seeded.id)

    return router
```

Wire it in `src/elspeth/web/app.py`. After `app.state.payload_store = payload_store` (:640, inside `_service_lifespan`):

```python
    # --- Library authority (Task I5) ---
    # Built here, not beside identity_authority in _create_app, because it
    # owns the payload store, which exists only once the state mode above
    # is resolved.  The store is shared with the guided payload writers
    # (guided_payloads.py:49/52); no web caller deletes from it, so a
    # content-addressed library payload cannot be purged from under a row.
    app.state.library_authority = RepositoryLibraryAuthority(app.state.session_engine, payload_store=payload_store)
```

After `app.include_router(create_shareable_reviews_router())` (:1791):

```python
    app.include_router(create_library_router())
```

with the imports `from elspeth.web.coordination.library_authority import RepositoryLibraryAuthority` and `from elspeth.web.sessions.routes.workflow.library import create_library_router` added to app.py's import block.

- [ ] **Step 8: Write the integration test.**

Create `tests/integration/web/workflow/test_library.py` (the package `__init__.py` and the directory are created by Task I3, which runs before this task):

```python
"""Library routes on the full app: publish → curate → browse → fork.

The app boots a closed local deployment with governance on and a
compartment configured (R11; Task I8).  Auth is bypassed through a
switchable ``get_current_user`` override so one client can act as the
publisher, a curator, and a third identity.  ``auth_events`` assertions
read the app's own Landscape (``settings.landscape_url``), because
``auth_events`` is a Landscape table (core/landscape/schema.py:2542).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import insert, select
from tests.fixtures.identities import ensure_test_identity
from tests.integration.web.conftest import (
    _lifespan_test_client,
    _passthrough_composition_state,
    _save_composition_state_with_compose_authority,
    _seed_session_with_state,
)
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient

from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import auth_events_table
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.sessions.models import identity_roles_table
from elspeth.web.sessions.protocol import CompositionStateData

_BLOB_REF = "20b944e3-fd46-434f-b9a2-4fb508db30f0"


@dataclass
class _Actor:
    identity: UserIdentity


def _build_app(tmp_path: Path, *, overrides: dict[str, Any]) -> tuple[FastAPI, _Actor]:
    from elspeth.web.app import create_app

    settings = WebSettings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        plugin_allowlist=("transform:passthrough",),
        registration_mode="closed",
        workflow_governance="on",
        compartment_id="alpha",
        **overrides,
    )
    app = create_app(settings=settings)
    actor = _Actor(UserIdentity(user_id="alice", username="alice"))

    async def _current_user() -> UserIdentity:
        return actor.identity

    app.dependency_overrides[get_current_user] = _current_user
    return app, actor


@pytest.fixture
def library_client(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[TestClient]:
    overrides: dict[str, Any] = getattr(request, "param", {})
    app, actor = _build_app(tmp_path, overrides=overrides)
    with _lifespan_test_client(app) as client:
        client.app.state.library_test_actor = actor
        with client.app.state.session_engine.begin() as conn:
            for identity_id in ("alice", "carol", "bob"):
                ensure_test_identity(conn, identity_id=identity_id)
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=str(uuid4()),
                    identity_id="carol",
                    role="curator",
                    expires_at=None,
                    note="fixture",
                    scope=None,
                    granted_by_identity_id="carol",
                    granted_at=datetime.now(UTC),
                    revoked_at=None,
                )
            )
        yield client


def _act_as(client: TestClient, identity_id: str) -> None:
    client.app.state.library_test_actor.identity = UserIdentity(user_id=identity_id, username=identity_id)


def _auth_events(client: TestClient) -> list[Any]:
    settings: WebSettings = client.app.state.settings
    with LandscapeDB.from_url(settings.landscape_url) as db, db.read_only_connection() as conn:
        return list(conn.execute(select(auth_events_table).order_by(auth_events_table.c.occurred_at)).fetchall())


def _seed_blob_backed_session(client: TestClient, *, user_id: str) -> UUID:
    session_service = client.app.state.session_service
    settings: WebSettings = client.app.state.settings

    async def _seed() -> UUID:
        record = await session_service.create_session(user_id=user_id, title="blob-backed", auth_provider_type=settings.auth_provider)
        (settings.data_dir / "blobs" / str(record.id)).mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "outputs" / str(record.id)).mkdir(parents=True, exist_ok=True)
        state_d = _passthrough_composition_state(settings.data_dir, record.id).to_dict()
        state_d["sources"]["source"]["options"]["blob_ref"] = _BLOB_REF
        await _save_composition_state_with_compose_authority(
            session_service,
            record.id,
            CompositionStateData(
                sources=state_d["sources"],
                nodes=state_d["nodes"],
                edges=state_d["edges"],
                outputs=state_d["outputs"],
                metadata_=state_d["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
        )
        return record.id

    return asyncio.run(_seed())


def test_publish_curate_browse_and_fork_round_trip(library_client: TestClient) -> None:
    client = library_client
    alice_session = _seed_session_with_state(client, user_id="alice")

    published = client.post(f"/api/sessions/{alice_session}/library/publish", json={"title": "passthrough"})
    assert published.status_code == 201, published.text
    entry = published.json()
    assert (entry["state"], entry["version"], entry["compartment_id"]) == ("pending", 1, "alpha")
    assert entry["published_from_session_id"] == str(alice_session), "the publisher sees their own staging session"

    _act_as(client, "bob")
    assert client.get("/api/library").json()["entries"] == [], "pending entries are not browseable"

    _act_as(client, "carol")
    queue = client.get("/api/library", params={"view": "queue"})
    assert [row["entry_id"] for row in queue.json()["entries"]] == [entry["entry_id"]]
    accepted = client.post(f"/api/library/{entry['entry_id']}/accept", json={"note": "fine"})
    assert accepted.status_code == 200, accepted.text
    assert (accepted.json()["state"], accepted.json()["curated_by_identity_id"]) == ("accepted", "carol")

    _act_as(client, "bob")
    browse = client.get("/api/library").json()
    assert [row["entry_id"] for row in browse["entries"]] == [entry["entry_id"]]
    assert browse["entries"][0]["published_from_session_id"] is None, "a browser is handed the projection, never a session reference"
    assert browse["entries"][0]["payload_digest"] == entry["payload_digest"]
    # the shared read never weakened session ownership
    assert client.get(f"/api/sessions/{alice_session}").status_code == 404

    forked = client.post(f"/api/library/{entry['entry_id']}/fork")
    assert forked.status_code == 201, forked.text
    new_session_id = UUID(forked.json()["session_id"])
    session_service = client.app.state.session_service
    new_session = asyncio.run(session_service.get_session(new_session_id))
    assert new_session.user_id == "bob" and new_session.forked_from_session_id is None
    listed = client.get("/api/sessions").json()
    assert str(new_session_id) in {row["id"] for row in listed}, "the fork is visible in its owner's session list"
    seeded = asyncio.run(session_service.get_current_state(new_session_id))
    assert seeded is not None and str(seeded.id) == forked.json()["state_id"]
    assert seeded.composer_meta is not None
    assert dict(seeded.composer_meta["library_fork"]) == {
        "entry_id": entry["entry_id"],
        "payload_digest": entry["payload_digest"],
        "published_from_session_id": str(alice_session),
        "compartment_id": "alpha",
        "version": 1,
    }
    assert set(seeded.sources or {}) == {"source"}

    events = _auth_events(client)
    library_rows = [row for row in events if row.event_type.startswith("library_")]
    assert [row.event_type for row in library_rows] == ["library_published", "library_accepted"]
    assert {row.identity_id for row in library_rows} == {"alice"}, "anchored on the publisher"
    published_meta = json.loads(library_rows[0].metadata_json)
    accepted_meta = json.loads(library_rows[1].metadata_json)
    assert (published_meta["payload_digest"], published_meta["compartment_id"]) == (entry["payload_digest"], "alpha")
    assert (accepted_meta["actor"], accepted_meta["note"]) == ("carol", "fine")


def test_publish_refuses_a_blob_backed_source_by_name(library_client: TestClient) -> None:
    client = library_client
    session_id = _seed_blob_backed_session(client, user_id="alice")
    refused = client.post(f"/api/sessions/{session_id}/library/publish", json={"title": "blob"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error_type"] == "library_entry_needs_profile_bound_source"
    assert refused.json()["detail"]["sources"] == ["source"]
    _act_as(client, "carol")
    assert client.get("/api/library", params={"view": "queue"}).json()["entries"] == []


@pytest.mark.parametrize("library_client", [{"compartment_id": None}], indirect=True)
def test_publish_refuses_without_a_configured_compartment(library_client: TestClient) -> None:
    client = library_client
    session_id = _seed_session_with_state(client, user_id="alice")
    refused = client.post(f"/api/sessions/{session_id}/library/publish", json={"title": "no compartment"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error_type"] == "compartment_not_configured"


@pytest.mark.parametrize("library_client", [{"workflow_governance": "off"}], indirect=True)
def test_governance_off_refuses_every_library_route(library_client: TestClient) -> None:
    client = library_client
    session_id = _seed_session_with_state(client, user_id="alice")
    calls = [
        client.post(f"/api/sessions/{session_id}/library/publish", json={"title": "t"}),
        client.get("/api/library"),
        client.post("/api/library/nope/accept", json={}),
        client.post("/api/library/nope/fork"),
    ]
    assert [(call.status_code, call.json()["detail"]["error_type"]) for call in calls] == [(409, "workflow_governance_off")] * 4


def test_reject_needs_a_note_and_curator_routes_are_hidden_from_non_curators(library_client: TestClient) -> None:
    client = library_client
    session_id = _seed_session_with_state(client, user_id="alice")
    entry_id = client.post(f"/api/sessions/{session_id}/library/publish", json={"title": "t"}).json()["entry_id"]

    _act_as(client, "bob")
    assert client.post(f"/api/library/{entry_id}/accept", json={}).status_code == 404
    assert client.get("/api/library", params={"view": "queue"}).status_code == 404

    _act_as(client, "carol")
    blank = client.post(f"/api/library/{entry_id}/reject", json={})
    assert (blank.status_code, blank.json()["detail"]["error_type"]) == (409, "library_rejection_note_required")
    rejected = client.post(f"/api/library/{entry_id}/reject", json={"note": "needs a bound source"})
    assert rejected.status_code == 200, rejected.text
    assert (rejected.json()["state"], rejected.json()["rejection_note"]) == ("rejected", "needs a bound source")
    again = client.post(f"/api/library/{entry_id}/accept", json={})
    assert (again.status_code, again.json()["detail"]["error_type"], again.json()["detail"]["current_state"]) == (
        409,
        "library_entry_already_curated",
        "rejected",
    )
    fork = client.post(f"/api/library/{entry_id}/fork")
    assert (fork.status_code, fork.json()["detail"]["error_type"]) == (409, "library_entry_not_forkable")
```

- [ ] **Step 9: Write the PostgreSQL concurrent-curation test.**

Create `tests/testcontainer/web/test_library_postgres.py`:

```python
"""Two curators accept the same entry at once on real PostgreSQL.

The conditional UPDATE (``WHERE accepted_at IS NULL AND rejected_at IS NULL
AND recalled_at IS NULL``) under the entry's ``FOR UPDATE`` lock lets exactly
one curator win; the loser is told the current state, and the row names one
curator.  SQLite serialises writers and cannot express this race.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, insert, select
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity

from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.coordination.library_authority import LibraryEntryAlreadyCurated, RepositoryLibraryAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identity_roles_table, library_entries_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def library_engine(external_deployment_postgres_url: str) -> Iterator[Engine]:
    database = f"library_{uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    engine = create_session_engine(make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False))
    try:
        initialize_session_schema(engine)
        yield engine
    finally:
        engine.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()


def _state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(plugin="csv", on_success="src_out", options={"path": "/data/in.csv", "schema": {"fields": ["a"]}}, on_validation_failure="discard"),
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
        outputs=(OutputSpec(name="out", plugin="csv", options={"path": "/data/out.csv"}, on_write_failure="discard"),),
        metadata=PipelineMetadata(name="race", description="concurrent accept"),
        version=1,
    )


def _grant_curator(engine: Engine, identity_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(identity_roles_table).values(
                role_id=str(uuid4()),
                identity_id=identity_id,
                role="curator",
                expires_at=None,
                note=None,
                scope=None,
                granted_by_identity_id=identity_id,
                granted_at=datetime.now(UTC),
                revoked_at=None,
            )
        )


def test_concurrent_accept_has_exactly_one_winner(library_engine: Engine, tmp_path: Path) -> None:
    with library_engine.begin() as conn:
        for identity_id in ("alice", "carol", "dave"):
            ensure_test_identity(conn, identity_id=identity_id)
    _grant_curator(library_engine, "carol")
    _grant_curator(library_engine, "dave")
    authority = RepositoryLibraryAuthority(library_engine, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))
    entry = authority.publish(session_id=str(uuid4()), state=_state(), title="race", published_by="alice", compartment_id="alpha", record=lambda event: None)
    gate = Barrier(2)

    def _accept(curator: str) -> str | BaseException:
        gate.wait(timeout=30)
        try:
            return authority.accept(entry_id=entry.entry_id, curator=curator, note=None, record=lambda event: None).curated_by_identity_id
        except LibraryEntryAlreadyCurated as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_accept, ("carol", "dave")))

    winners = [outcome for outcome in outcomes if isinstance(outcome, str)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, LibraryEntryAlreadyCurated)]
    assert len(winners) == 1 and len(losers) == 1, outcomes
    assert losers[0].current_state == "accepted"
    with library_engine.connect() as conn:
        row = conn.execute(select(library_entries_table).where(library_entries_table.c.entry_id == entry.entry_id)).one()
    assert row.curated_by_identity_id == winners[0] and row.accepted_at is not None
```

- [ ] **Step 10: Admit the new writers and connections to the Sessions mutation-authority manifest.**

The manifest is fail-closed, but the gate reports drift through `pytest.xfail` (`tests/unit/architecture/test_session_db_mutation_authority.py:18351`, HEAD) and already XFAILs on a clean HEAD, so its exit code is `0` with `1 xfailed` both before and after this step. The signal is the XFAIL reason text, which `-rx` prints. First read the drift report:

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities -n 0 -v -rx > /tmp/i5-manifest-red.log 2>&1; echo exit=$?`
Expected: `exit=0` and `1 xfailed`. The reason text begins `Sessions mutation authority inventory drift.` and lists one `describe()` line per site, in the form `<path>:<line> <symbol> <operation> <table> fp=<16 hex>#<ordinal> authority=<name or UNCLASSIFIED> connection_escape=False`. Compared with the counts the same gate printed on the tree before this task, exactly three sections grow:
- `Unexpected/unreviewed`: 14 above, the 14 sites listed below.
- `Connections outside exact contained authority`: 9 above, the five `engine.begin()` connections of `publish`/`accept`/`reject`/`deprecate`/`recall` and the four `engine.connect()` reads of `read`/`browse`/`curation_queue`/`published_by`.
- `Writers without a named authority`: 5 above, the five `library_entries` writes.

Every other section keeps its pre-task count. `Unresolved write executions` names no `library_authority.py` line, because every `conn.execute` in the module runs a statement the scanner resolves. `Stale reviewed` and `Stale reviewed read connections` stay `(0)`: no `_REVIEWED_WRITERS` or `_REVIEWED_READ_CONNECTIONS` row names `yaml_generator.py`, `sessions/routes/composer/state.py`, `auth/audit.py` or `app.py` (measured on HEAD: `grep -c` of each quoted path in the gate file prints `0`, while `identity_authority.py` prints `81`), so this task moves no reviewed row. The pre-task counts are I1 Step 15's baseline when I5 lands directly after I3, and I4 Step 9's green-run counts when I4 landed first.

Measured 2026-09-15 on a `git archive` export of 46219b2b7 with Step 4's module pasted verbatim: `1 xfailed in 103.29s`. The nine section counts went from I1 Step 15's baseline (67, 0, 16, 44, 7, 0, 0, 0, 0) to (81, 0, 25, 44, 12, 0, 0, 0, 0), and the 14 new sites were:

```text
src/elspeth/web/coordination/library_authority.py:222 <module> update library_entries fp=9e5f9796f7291412#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:227 <module> update library_entries fp=a344eef1e9ad568c#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:236 <module> update library_entries fp=8e247ffde92273d1#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:246 <module> update library_entries fp=3963f4a1b9b21ae0#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:410 RepositoryLibraryAuthority.publish insert library_entries fp=a7fe5fe90f1113ff#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:397 RepositoryLibraryAuthority.publish write_connection <sessions-write-connection> fp=b99e4775a7964aca#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:439 RepositoryLibraryAuthority.accept write_connection <sessions-write-connection> fp=7869c3bf021e648f#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:463 RepositoryLibraryAuthority.reject write_connection <sessions-write-connection> fp=4ac067ac8b49654a#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:488 RepositoryLibraryAuthority.deprecate write_connection <sessions-write-connection> fp=d7c0ff9f68292f97#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:513 RepositoryLibraryAuthority.recall write_connection <sessions-write-connection> fp=c4174561aded5211#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:333 RepositoryLibraryAuthority.read write_connection <sessions-write-connection> fp=77be48d4595cea35#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:341 RepositoryLibraryAuthority.browse write_connection <sessions-write-connection> fp=4920d07b588aa7ca#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:346 RepositoryLibraryAuthority.curation_queue write_connection <sessions-write-connection> fp=312dd68e5ad28ba2#1 authority=UNCLASSIFIED connection_escape=False
src/elspeth/web/coordination/library_authority.py:352 RepositoryLibraryAuthority.published_by write_connection <sessions-write-connection> fp=5df578dd6aecec07#1 authority=UNCLASSIFIED connection_escape=False
```

The four curation `UPDATE` statements are the module-level constants `_ACCEPT`, `_REJECT`, `_DEPRECATE` and `_RECALL`. The scanner attributes them to the symbol `<module>` at the line of each constant, not to the method that executes them. That is why the manifest binds `<module>` below: a `_REVIEWED_WRITERS` row that names `RepositoryLibraryAuthority.accept` for an `update` never matches a live site. `Unexpected/unreviewed` prints at most 80 lines (`unexpected[:80]`, :18356), so when the section count is above 80 a new site can be cut from it. Read the five writes from `Writers without a named authority` and the nine connections from `Connections outside exact contained authority`, which list every one of them.

Then edit `tests/unit/architecture/test_session_db_mutation_authority.py`. Each fingerprint and `line=` below is the value measured on Step 4's module as written. If `/tmp/i5-manifest-red.log` prints a different value for a site, the module differs from Step 4's fence. Diff the two and fix the module; copy the printed value only when the difference is a deliberate change to Step 4.

1. In `_NAMED_AUTHORITY_SYMBOLS` (:262-908), before the closing `)` at :908:

```python
    # ── shared library (Task I5): RepositoryLibraryAuthority is the sole writer
    # of library_entries (TablePolicy :113, global scope, engine-owning). The
    # five mutations bind method-exact, so the four read methods and any future
    # method stay unbound. `<module>` binds the four curation UPDATE constants,
    # which the scanner attributes to module scope; `_authority_for` (:5632)
    # matches `<module>` exactly, so it covers module-scope sites of this one
    # file and nothing else ──────────────────────────────────────────────────────
    AuthoritySymbol("src/elspeth/web/coordination/library_authority.py", "RepositoryLibraryAuthority.publish", "LibraryAuthority"),
    AuthoritySymbol("src/elspeth/web/coordination/library_authority.py", "RepositoryLibraryAuthority.accept", "LibraryAuthority"),
    AuthoritySymbol("src/elspeth/web/coordination/library_authority.py", "RepositoryLibraryAuthority.reject", "LibraryAuthority"),
    AuthoritySymbol("src/elspeth/web/coordination/library_authority.py", "RepositoryLibraryAuthority.deprecate", "LibraryAuthority"),
    AuthoritySymbol("src/elspeth/web/coordination/library_authority.py", "RepositoryLibraryAuthority.recall", "LibraryAuthority"),
    AuthoritySymbol("src/elspeth/web/coordination/library_authority.py", "<module>", "LibraryAuthority"),
```

2. In `_CONTAINED_CONNECTION_AUTHORITIES` (:914-1193), directly above its closing `)` at :1193 (two lines above the comment `# Literal identities for writers that sit behind an exact named authority.`, :1195). `connection_authority_violations` (:10608) requires every `write_connection` site's contained authority (`_contained_connection_authority_for`, :5639, exact symbol match) to equal the authority its symbol binds:

```python
    # Task I5: each library mutation opens, uses and closes its own
    # engine.begin() connection inside the method.
    AuthoritySymbol("src/elspeth/web/coordination/library_authority.py", "RepositoryLibraryAuthority.publish", "LibraryAuthority"),
    AuthoritySymbol("src/elspeth/web/coordination/library_authority.py", "RepositoryLibraryAuthority.accept", "LibraryAuthority"),
    AuthoritySymbol("src/elspeth/web/coordination/library_authority.py", "RepositoryLibraryAuthority.reject", "LibraryAuthority"),
    AuthoritySymbol("src/elspeth/web/coordination/library_authority.py", "RepositoryLibraryAuthority.deprecate", "LibraryAuthority"),
    AuthoritySymbol("src/elspeth/web/coordination/library_authority.py", "RepositoryLibraryAuthority.recall", "LibraryAuthority"),
```

3. In `_REVIEWED_WRITERS` (:1198), directly after its opening line `_REVIEWED_WRITERS: tuple[WriterIdentity, ...] = (`:

```python
    # ── shared library (Task I5): publish's insert, the four module-level
    # curation UPDATE constants, and the five contained write connections ────
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "<module>",
        "library_entries",
        "update",
        "9e5f9796f7291412",
        1,
        "LibraryAuthority",
        line=222,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "<module>",
        "library_entries",
        "update",
        "a344eef1e9ad568c",
        1,
        "LibraryAuthority",
        line=227,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "<module>",
        "library_entries",
        "update",
        "8e247ffde92273d1",
        1,
        "LibraryAuthority",
        line=236,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "<module>",
        "library_entries",
        "update",
        "3963f4a1b9b21ae0",
        1,
        "LibraryAuthority",
        line=246,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.publish",
        "library_entries",
        "insert",
        "a7fe5fe90f1113ff",
        1,
        "LibraryAuthority",
        line=410,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.publish",
        "<sessions-write-connection>",
        "write_connection",
        "b99e4775a7964aca",
        1,
        "LibraryAuthority",
        line=397,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.accept",
        "<sessions-write-connection>",
        "write_connection",
        "7869c3bf021e648f",
        1,
        "LibraryAuthority",
        line=439,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.reject",
        "<sessions-write-connection>",
        "write_connection",
        "4ac067ac8b49654a",
        1,
        "LibraryAuthority",
        line=463,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.deprecate",
        "<sessions-write-connection>",
        "write_connection",
        "d7c0ff9f68292f97",
        1,
        "LibraryAuthority",
        line=488,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.recall",
        "<sessions-write-connection>",
        "write_connection",
        "c4174561aded5211",
        1,
        "LibraryAuthority",
        line=513,
    ),
```

4. In `_REVIEWED_READ_CONNECTIONS` (:3954), directly after its opening line `_REVIEWED_READ_CONNECTIONS: tuple[WriterIdentity, ...] = (`. These are the shape of the `RepositoryIdentityAuthority.configured_admin_seed_consumed` row at :3957-3966: authority `None`, because the read methods are not bound.

```python
    # Task I5: the library reads are SELECT-only engine.connect() blocks; the
    # connection never leaves the method.
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.read",
        "<sessions-write-connection>",
        "write_connection",
        "77be48d4595cea35",
        1,
        None,
        line=333,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.browse",
        "<sessions-write-connection>",
        "write_connection",
        "4920d07b588aa7ca",
        1,
        None,
        line=341,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.curation_queue",
        "<sessions-write-connection>",
        "write_connection",
        "312dd68e5ad28ba2",
        1,
        None,
        line=346,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.published_by",
        "<sessions-write-connection>",
        "write_connection",
        "5df578dd6aecec07",
        1,
        None,
        line=352,
    ),
```

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py -n 0 -v -rx > /tmp/i5-manifest-green.log 2>&1; echo exit=$?`
Expected: `exit=0`, no `failed`, and exactly `1 xfailed`, `test_all_production_sessions_writers_are_reviewed_typed_authorities`. The gate already XFAILs on a clean HEAD (I1 Step 15 records the baseline counts), so this task cannot bring it to a pass. Read the XFAIL text instead. Its nine section counts equal the red run's counts, minus 14 for `Unexpected/unreviewed`, 9 for `Connections outside exact contained authority` and 5 for `Writers without a named authority`, which is back to the pre-task counts. Measured on the same export with this step applied: `265 passed, 1 xfailed in 197.62s`, counts (67, 0, 16, 44, 7, 0, 0, 0, 0). If any other test in the file fails, a row above was typed with the wrong authority or table; compare it against the red log.

Run: `grep -c 'library_authority.py' /tmp/i5-manifest-green.log; grep -c 'library_authority.py' /tmp/i5-manifest-red.log; grep -c 'Stale reviewed (0):' /tmp/i5-manifest-green.log; grep -c 'Stale reviewed read connections (0):' /tmp/i5-manifest-green.log`
Expected: `0`, then at least `1`, then at least `1`, then at least `1` (measured: `0`, `28`, `1`, `1`). The first count proves every one of the 14 sites is reviewed, `<module>` rows included: every `describe()` line carries the path, and no test function or file-level text in the gate file names `library_authority.py` (measured `0` on the HEAD file), so a `-v` progress line cannot match. The second is the positive control: the same grep matches the red log, so the `0` does not come from a pattern that matches nothing. The third proves no writer row was mistyped. The fourth proves no read-connection row was mistyped. A read row with a wrong `line=` prints under `Stale reviewed read connections`, which the third grep cannot see, because `Stale reviewed (0):` is not a substring of `Stale reviewed read connections (0):`.

Control the instrument twice. The exit code does not discriminate here: it is `exit=0` with `1 xfailed` before and after each mutation.

First, change the last hex character of the `RepositoryLibraryAuthority.publish` `insert` row's fingerprint from `f` to `e` (`"a7fe5fe90f1113ff"` becomes `"a7fe5fe90f1113fe"`).

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities -n 0 -v -rx > /tmp/i5-manifest-control-fp.log 2>&1; echo exit=$?; grep -c 'Stale reviewed (1):' /tmp/i5-manifest-control-fp.log; grep -c 'Stale reviewed (0):' /tmp/i5-manifest-control-fp.log; grep -c 'library_authority.py' /tmp/i5-manifest-control-fp.log`
Expected: `exit=0` and `1 xfailed`, then `1`, `0`, `2`. `Unexpected/unreviewed` is 1 above the green count and names the live `publish insert library_entries fp=a7fe5fe90f1113ff` site; `Stale reviewed (1):` names the mutated `fp=a7fe5fe90f1113fe` row. All other counts equal the green run. Restore the `f`.

Second, change the `RepositoryLibraryAuthority.browse` read row's `line=341` to `line=342`.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities -n 0 -v -rx > /tmp/i5-manifest-control-read.log 2>&1; echo exit=$?; grep -c 'Stale reviewed (0):' /tmp/i5-manifest-control-read.log; grep -c 'Stale reviewed read connections (0):' /tmp/i5-manifest-control-read.log; grep -c 'library_authority.py' /tmp/i5-manifest-control-read.log`
Expected: `exit=0` and `1 xfailed`, then `1`, `0`, `3`. `Stale reviewed read connections (1):` names the `browse` row at `:342`, and the live `:341` site appears under both `Unexpected/unreviewed` and `Connections outside exact contained authority`, each 1 above the green count. `Stale reviewed (0):` still matches, which is the reason the fourth grep above exists. Measured on the export: both controls gave exactly these values (`Unexpected/unreviewed (68)`, and `Connections outside exact contained authority (17)` for the second). Restore `line=341`.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py -n 0 -v -rx > /tmp/i5-manifest-green.log 2>&1; echo exit=$?; grep -c 'library_authority.py' /tmp/i5-manifest-green.log; grep -c 'Stale reviewed (0):' /tmp/i5-manifest-green.log; grep -c 'Stale reviewed read connections (0):' /tmp/i5-manifest-green.log`
Expected: `exit=0`, no `failed`, exactly `1 xfailed`, then `0`, at least `1`, at least `1`: both mutations are restored.

- [ ] **Step 11: Run the integration and route suites.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/integration/web/workflow/test_library.py tests/unit/web/sessions/routes -n 0 > /tmp/i5-integration.log 2>&1; echo exit=$?`
Expected: `exit=0`. `tests/unit/web/sessions/routes` covers the `/state/yaml` extraction (the route's own tests must be unchanged by the refactor).

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/composer tests/unit/web/auth tests/unit/web/coordination tests/integration/web/test_yaml_export_audit_event.py -n 0 > /tmp/i5-neighbours.log 2>&1; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 12: Run the PostgreSQL race proof.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/testcontainer/web/test_library_postgres.py -m testcontainer -n 0 > /tmp/i5-pg.log 2>&1; echo exit=$?`
Expected: `exit=0` (needs Docker; `-m testcontainer` is required or the selection is empty and pytest exits 5). Also run the schema probe, since this task writes a sessions table on both dialects: `pytest tests/testcontainer/web/test_schema_probe_postgres.py -m testcontainer -n 0 > /tmp/i5-pg-schema.log 2>&1; echo exit=$?` → `exit=0`.

- [ ] **Step 13: Lint and commit.**

Run: `cd "$(git rev-parse --show-toplevel)" && ruff check src/elspeth/web/coordination/library_authority.py src/elspeth/web/sessions/routes/workflow/library.py src/elspeth/web/composer/yaml_generator.py src/elspeth/web/sessions/routes/composer/state.py src/elspeth/web/auth/audit.py src/elspeth/web/app.py tests/unit/web/coordination/test_library_authority.py tests/integration/web/workflow/test_library.py tests/testcontainer/web/test_library_postgres.py > /tmp/i5-ruff.log 2>&1; echo exit=$?` → `exit=0`.

This task creates five files, which are untracked. A commit pathspec that names an untracked path is refused, so mark exactly those five as intent-to-add first. This records only that the paths exist; nothing else enters the index:

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N src/elspeth/web/coordination/library_authority.py src/elspeth/web/sessions/routes/workflow/library.py tests/unit/web/coordination/test_library_authority.py tests/integration/web/workflow/test_library.py tests/testcontainer/web/test_library_postgres.py; echo exit=$?
```

Expected: `exit=0`.

Run: `cd "$(git rev-parse --show-toplevel)" && git status --short > /tmp/i5-lane-status.log 2>&1; scripts/branch-safety-check.sh --intent commit > /tmp/i5-lane-branch-safety.log 2>&1; echo exit=$?`
Expected: `exit=0` with no `[FAIL]` line in the safety log (exit 1 on any FAIL: stop before the commit and fix the reported check). The status log shows the five created paths above as ` A` (intent-to-add) and the seven modified paths named in the commit below as ` M`. Any other path it lists belongs to a sibling lane and must not be named in the commit.

```bash
cd "$(git rev-parse --show-toplevel)" && git commit -m "feat(identity): shared library publish, curate, browse and fork" -- src/elspeth/web/coordination/library_authority.py src/elspeth/web/sessions/routes/workflow/library.py src/elspeth/web/composer/yaml_generator.py src/elspeth/web/sessions/routes/composer/state.py src/elspeth/web/auth/audit.py src/elspeth/web/app.py tests/unit/architecture/test_session_db_mutation_authority.py tests/unit/web/coordination/test_library_authority.py tests/unit/web/composer/test_yaml_generator.py tests/unit/web/auth/test_audit.py tests/integration/web/workflow/test_library.py tests/testcontainer/web/test_library_postgres.py; echo exit=$?
```

Expected: `exit=0` (the pre-commit hooks pass).

Run: `cd "$(git rev-parse --show-toplevel)" && git show --stat HEAD > /tmp/i5-lane-commit-stat.log 2>&1; echo exit=$?`
Expected: `exit=0`; the stat lists exactly those 12 files (5 created: `library_authority.py`, `routes/workflow/library.py`, `test_library_authority.py`, `integration/web/workflow/test_library.py`, `test_library_postgres.py`; 7 modified: `yaml_generator.py`, `routes/composer/state.py`, `auth/audit.py`, `web/app.py`, `test_session_db_mutation_authority.py`, `test_yaml_generator.py`, `test_audit.py`), and the summary line reads `12 files changed`. Any other count means a sibling lane staged into the shared index: undo with `git reset --mixed HEAD~1` (never `checkout`, `restore` or `clean`). That reset also drops the five intent-to-add entries, so re-run the `git add -N` block above, then commit again with the same 12-path pathspec. `web/app.py`, `auth/audit.py`, `tests/unit/web/auth/test_audit.py` and `tests/unit/architecture/test_session_db_mutation_authority.py` are also touched by I3 and I4. The second lane to land rebases, and every collision is an adjacent-line append.
