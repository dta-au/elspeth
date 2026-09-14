### Task I4: Review requests and reviewer attestations

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: I3. Runs before: I6. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered after I3 (it registers its router beside I3's in `web/app.py` and
lives in the `sessions/routes/workflow/` package I3 creates) and in parallel
with I5; I6 follows both. Spec: D16 (sso-design.md:75), D26 (:82), R11
(:1115-1124), §Frontend → Mailbox (:1203-1220) and → Reviewer attestation
(:1358-1361), §Testing → Workflow governance (:1290-1311, "attestation has no
refusal to mutate, by its own design as a ledger"), §Workflow tables rows
`review_attestations` (:1418) and `review_requests` (:1419).

Measured on HEAD 072141b75: both tables already exist —
`review_requests_table` (sessions/models.py:3701-3735: `request_id`,
`session_id` FK RESTRICT, `state_id`, `requested_by_identity_id`,
`reviewer_identity_id` NULL = "any active reviewer", `requested_at`,
`cancelled_at`, `request_note`) and `review_attestations_table`
(:3737-3776: `attestation_id`, `session_id`, `state_id`, `payload_digest`,
`reviewer_identity_id`, `author_identity_id` snapshot, `attested_at`,
`verdict`, `note`, CHECK `ck_review_attestations_verdict` over
`_REVIEW_VERDICT_CHECK` :3583 and CHECK
`ck_review_attestations_reviewer_is_not_author` :3772-3775). I0 changes
nothing here. The mutation-authority manifest already names the owner:
`TablePolicy("review_attestations", "session", "ReviewAuthority")` and
`TablePolicy("review_requests", "session", "ReviewAuthority")`
(test_session_db_mutation_authority.py:118-119); no writer exists yet, so
`grep -rn "review_requests_table\|review_attestations_table" src/elspeth/web
--include=*.py` hits only models.py. `AuthAuditEventType`
(core/landscape/auth_audit_repository.py:20-45) and the Landscape CHECK
(core/landscape/schema.py:2566-2578) already carry `review_requested`,
`review_request_cancelled` and `review_attested`; no Landscape change.

Decisions this task owns:

1. **The authority owns its transaction, like the identity authority, not a
   fence token.** A reviewer is not the session's owner, so the
   session-operation fence (owner-instance leases, `coordination/repository.py`)
   is the wrong lock: an attestation must not wait on, or evict, the author's
   compose lease. `RepositoryReviewAuthority(engine)` opens
   `engine.begin()`, locks the participating `identities` rows `FOR UPDATE`
   and writes both tables inside that transaction, exactly the shape
   `RepositoryIdentityAuthority` uses (identity_authority.py:1-40, its
   "THE CONNECTION NEVER LEAVES THE METHOD THAT OPENED IT" rule and
   module-level statement constants). Lock order follows
   approval_lifecycle_authority.py:3-8: if the requester holds deployment
   `admin` (possible only for the requester — R8 bars an admin reviewer),
   take `_ADMIN_HOLDER_ROWS_FOR_UPDATE` (identity_authority.py:797) first,
   then the participant rows in stable identity-id order; otherwise stable
   id order alone.
2. **"Closed" is derived, never written.** Spec :1419: a request is closed
   "by an attestation on the same `(session_id, state_id)`, or by
   `cancelled_at`". The authority never updates a request when an
   attestation lands; `open` is `cancelled_at IS NULL AND NOT EXISTS
   (attestation on the pair with attested_at >= requested_at)`, evaluated by
   one predicate every read uses. Attestation therefore has exactly one
   writer (an insert) and refuses on nothing about the request's state — a
   ledger, per :1418.
3. **The digest is the state's content, computed server-side.**
   `payload_digest` = `sha256:` + SHA-256 of `canonical_json`
   (contracts/hashing.py:66) over the state record's `version`, `sources`,
   `source`, `nodes`, `edges`, `outputs` and `metadata`. The client never
   supplies it. (I5's `public_projection_digest` hashes the public YAML
   projection for a different purpose — library identity — and is not
   consumed here because I4 ∥ I5.)
4. **Governance switch.** The three mutation routes refuse with HTTP 409
   `error_type="workflow_governance_off"` unless
   `settings.workflow_governance == "on"` (I8); the inbox returns an empty
   list when the switch is off, so I9's mailbox renders empty rather than
   red on a deployment that has not enabled governance.
5. **Audit rows are anchored on the ACTOR** (`identity_id` = the requester
   for `review_requested`/`review_request_cancelled`, the reviewer for
   `review_attested`); the other participant travels in `metadata_json`,
   the way `record_relationship_changed` anchors on `to` and carries `from`
   (audit.py:1140-1180). Metadata keeps the L0 provenance keys via
   `_admin_provenance` (audit.py:1206) with both console keys `None`.
6. **Notes are bounded in BYTES at the write boundary** (4096, the
   models.py:3577-3581 rule that a CHECK cannot express a byte length
   portably); the route bounds characters first, the authority bounds bytes.
7. **Copy rename.** "Save for review" becomes "Share inspect link" on the
   completion bar and everywhere that string is pinned;
   `data-testid` values and the e2e page-object method name stay, only the
   accessible name changes.

**Files:**
- Create: `src/elspeth/web/coordination/review_authority.py`
- Create: `src/elspeth/web/sessions/routes/workflow/reviews.py` (the package `__init__.py` is I3's; I4 adds nothing to it)
- Modify: `src/elspeth/web/app.py:1521-1522` (`identity_authority = RepositoryIdentityAuthority(session_engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)` / `app.state.identity_authority = identity_authority`; the review authority is built directly after) and `:1782-1783` (`app.include_router(create_identity_admin_router())` / `app.include_router(create_session_router())`; the reviews router is registered directly after I3's approvals-router line, which on your branch follows :1782)
- Modify: `src/elspeth/web/auth/audit.py:238-252` (`AuthAuditWriter.record_relationship_changed`, the Protocol's last member; three members appended after :252), `:286-287` (`ROLE_CHANGED` / `RELATIONSHIP_CHANGED`, the last `AuthAuditOperation` members), `:1140-1180` (`AuthAuditRecorder.record_relationship_changed`, the recorder's last method; three methods appended before `class _AdminProvenanceMetadata` at :1183)
- Modify: `tests/unit/web/auth/test_audit.py` (append after `test_the_two_dormancy_rows_are_distinguishable_from_each_other_and_from_a_rebound` :1013-1050, end of file; helpers `_request` :131, `_durable_recorder` :629, `_durable_rows` :634, `_metadata` :643, `_PROVENANCE` :650)
- Modify: `tests/unit/web/auth/test_identity_admin_routes.py:96-97` (`_RecordingAuditWriter.record_relationship_changed`, the fake's last recorded member; three inert members appended so the docstring at :50 "Every `AuthAuditWriter` member, explicit" stays true)
- Modify: `tests/unit/architecture/test_session_db_mutation_authority.py:806-852` (`_NAMED_AUTHORITY_SYMBOLS` identity block; three `AuthoritySymbol` entries appended after the last `RepositoryIdentityAuthority.*` entry) and `:1198` (`_REVIEWED_WRITERS`; three `WriterIdentity` rows with the fingerprints the gate prints)
- Modify: `src/elspeth/web/frontend/src/components/composer/CompletionBar.tsx:6` (comment), `:26` (comment), `:55-56` (`SAVE_FOR_REVIEW_DISABLED_TITLE`), `:101` (button text)
- Modify: `src/elspeth/web/frontend/src/components/composer/CompletionBar.test.tsx:112,133,146,155,168,191,235,251` (the literal `"Save for review"` in two `toEqual` arrays and six `it` test titles)
- Modify: `src/elspeth/web/frontend/src/components/composer/CompletionFlow.integration.test.tsx:6,122,149,204` (a comment, two `it` test titles, one comment)
- Modify: `src/elspeth/web/frontend/src/components/workspace/workspaceChrome.test.ts:174` (comment)
- Modify: `src/elspeth/web/frontend/tests/e2e/page-objects/composer-page.ts:124` (`getByRole("button", { name: "Save for review" })`; the `saveForReview()` method name at :123 stays — five e2e call sites use it)
- Modify: `docs/release/composer-guide.md:35,106`, `docs/guides/composer-training-one-hour.md:35,147,414,451-452`
- Modify: `CHANGELOG.md:35-38` (the `**Authentication events in signed exports.**` bullet under `## 0.8.1 - 2026-09-10`; the new bullet goes directly after it, after any I8/I3 bullets already there)
- Test: `tests/unit/web/coordination/test_review_authority.py` (new), `tests/unit/web/workflow/test_review_routes.py` (new; the package `tests/unit/web/workflow/__init__.py` is I3's, I4 adds nothing to it), `tests/testcontainer/web/test_review_authority_postgres.py` (new)

**Interfaces:**
- Consumes:
  - I8: `WebSettings.workflow_governance: Literal["off", "on"] = "off"`; fixtures `closed_local_settings` and `closed_local_app` (tests/unit/web/conftest.py; `client.app.state.phase3_engine`, `client.app.state.settings`, `get_current_user` overridden to `alice`).
  - I3: packages `src/elspeth/web/sessions/routes/workflow/__init__.py` and `tests/unit/web/workflow/__init__.py`, and the `app.include_router` line I3 adds for its approvals router in `web/app.py`.
  - HEAD: `_ADMIN_HOLDER_ROWS_FOR_UPDATE` (identity_authority.py:797), `RepositoryIdentityAuthority.holds_active_role(*, identity_id, role) -> bool` (:1285), `database_now(conn) -> datetime` (coordination/database_clock.py), `_admin_provenance(request, *, actor_identity_id, on_behalf_of, console_request_id)` and `_bounded_text` (audit.py:1206, :290), `AuthAuditOperation` (:268), `RecorderFactory(db).auth_audit.record_auth_event(*, event_type, outcome, provider, user_id, username, failure_category, request_id, client_host, user_agent, metadata, identity_id=None) -> str` (core/landscape/auth_audit_repository.py:156), `_verify_session_ownership(session_id: UUID, user, request) -> SessionRecord` (sessions/routes/_helpers.py:2527), `run_sync_in_worker` (async_workers.py:179), `get_current_user` (auth/middleware.py), `UserIdentity(user_id, username)` (auth/models.py:20), `MAX_AUTH_AUDIT_TEXT_LENGTH` (audit.py:41), `SessionServiceProtocol.get_state(state_id: UUID) -> CompositionStateRecord` (sessions/protocol.py:4618; impl service.py:10332), `canonical_json` (contracts/hashing.py:66), `ensure_test_identity` (tests/fixtures/identities.py:10), fixture `engine` (tests/unit/web/conftest.py:63), fixture `external_deployment_postgres_url` (tests/testcontainer/web/conftest.py:39).
- Produces:
  - `src/elspeth/web/coordination/review_authority.py`:
    - `ReviewVerdict = Literal["signed_off", "changes_requested", "withdrawn"]`, `MAX_REVIEW_NOTE_BYTES: Final = 4096`
    - `ReviewRequestRecord(request_id, session_id, state_id, requested_by_identity_id, reviewer_identity_id: str | None, requested_at, cancelled_at: datetime | None, request_note: str | None, open: bool)` (frozen)
    - `ReviewAttestationRecord(attestation_id, session_id, state_id, payload_digest, reviewer_identity_id, author_identity_id, attested_at, verdict: ReviewVerdict, note: str | None)` (frozen)
    - `ReviewAuthorityRefusal(RuntimeError)` and its closed subclasses `ReviewRequestNotFound`, `SessionNotOwnedByRequester`, `StateNotInSession`, `ReviewParticipantNotActive`, `ReviewerRoleRequired`, `ReviewerIsAuthor`, `OpenReviewRequestExists`, `ReviewRequestAlreadyClosed`, `ChangesRequestedNeedsNote`, `ReviewNoteTooLong`
    - `RepositoryReviewAuthority(engine: Engine)` with
      `request(*, session_id: str, state_id: str, requested_by: str, reviewer: str | None, note: str | None, record: Callable[[ReviewRequestRecord], None]) -> ReviewRequestRecord`,
      `cancel(*, request_id: str, requested_by: str, record: Callable[[ReviewRequestRecord], None]) -> ReviewRequestRecord`,
      `attest(*, session_id: str, state_id: str, payload_digest: str, reviewer: str, verdict: ReviewVerdict, note: str | None, record: Callable[[ReviewAttestationRecord], None]) -> ReviewAttestationRecord`,
      `read_request(*, request_id: str) -> ReviewRequestRecord | None`,
      `open_for(*, reviewer: str) -> tuple[ReviewRequestRecord, ...]`,
      `attestations_for(*, session_id: str, state_id: str) -> tuple[ReviewAttestationRecord, ...]` (I7's inspect view and audit view read attestations through this; I9 renders `open_for`).
  - `src/elspeth/web/sessions/routes/workflow/reviews.py`: `create_reviews_router() -> APIRouter` and `review_payload_digest(record: CompositionStateRecord) -> str`; routes `POST /api/sessions/{session_id}/reviews` (201, `ReviewRequestView`), `GET /api/reviews/inbox` (200, `ReviewInboxResponse`), `POST /api/reviews/{request_id}/attest` (201, `ReviewAttestationView`), `POST /api/reviews/{request_id}/cancel` (200, `ReviewRequestView`). Error envelope `{"error_type": <snake_case refusal>, "detail": <str>}`: 404 for `review_request_not_found`, `session_not_owned_by_requester`, `state_not_in_session`; 409 for every other refusal and for `workflow_governance_off`.
  - `app.state.review_authority: RepositoryReviewAuthority`.
  - `AuthAuditWriter` / `AuthAuditRecorder` gain `record_review_requested(request, *, provider, request_id, session_id, state_id, requested_by_identity_id, reviewer_identity_id, note)`, `record_review_request_cancelled(request, *, provider, request_id, session_id, state_id, requested_by_identity_id)`, `record_review_attested(request, *, provider, attestation_id, session_id, state_id, payload_digest, reviewer_identity_id, author_identity_id, verdict, note)`; `AuthAuditOperation.REVIEW_REQUESTED`, `.REVIEW_REQUEST_CANCELLED`, `.REVIEW_ATTESTED`.
  - Frontend accessible name `Share inspect link` (button `data-testid="completion-bar-save-for-review"` unchanged); e2e page object `composer.saveForReview()` locates it by the new name.

- [ ] **Step 1: Write the failing authority tests.**

Create `tests/unit/web/coordination/test_review_authority.py`:

```python
"""RepositoryReviewAuthority: review requests are a control, attestations are a ledger (spec :1418-1419)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event, insert, select, update
from sqlalchemy.exc import IntegrityError
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.review_authority import (
    MAX_REVIEW_NOTE_BYTES,
    ChangesRequestedNeedsNote,
    OpenReviewRequestExists,
    RepositoryReviewAuthority,
    ReviewAttestationRecord,
    ReviewerIsAuthor,
    ReviewerRoleRequired,
    ReviewNoteTooLong,
    ReviewParticipantNotActive,
    ReviewRequestAlreadyClosed,
    ReviewRequestNotFound,
    ReviewRequestRecord,
    SessionNotOwnedByRequester,
    StateNotInSession,
)
from elspeth.web.sessions.models import (
    composition_states_table,
    identities_table,
    identity_roles_table,
    review_attestations_table,
    review_requests_table,
    sessions_table,
)

SESSION = "sess-1"
STATE = "state-1"
DIGEST = "sha256:" + "ab" * 32


def _seed(engine: Engine) -> None:
    """alice owns SESSION/STATE; bob and carol hold reviewer; dave holds only user; root holds admin."""
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol", "dave", "root"):
            ensure_test_identity(conn, identity_id=identity_id)
        for role_id, identity_id, role in (
            ("role-bob", "bob", "reviewer"),
            ("role-carol", "carol", "reviewer"),
            ("role-dave", "dave", "user"),
            ("role-root", "root", "admin"),
        ):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=role_id, identity_id=identity_id, role=role, granted_at=now, granted_by_identity_id="root"
                )
            )
        conn.execute(
            insert(sessions_table).values(
                id=SESSION, user_id="alice", auth_provider_type="local", title="review me", created_at=now, updated_at=now
            )
        )
        conn.execute(
            insert(composition_states_table).values(
                id=STATE, session_id=SESSION, version=1, provenance="session_seed", created_at=now
            )
        )


@pytest.fixture
def authority(engine: Engine) -> RepositoryReviewAuthority:
    _seed(engine)
    return RepositoryReviewAuthority(engine)


def _noop(_record: object) -> None:
    pass


def _request(authority: RepositoryReviewAuthority, *, reviewer: str | None = "bob", note: str | None = "please look") -> ReviewRequestRecord:
    return authority.request(session_id=SESSION, state_id=STATE, requested_by="alice", reviewer=reviewer, note=note, record=_noop)


def _attest(
    authority: RepositoryReviewAuthority, *, reviewer: str = "bob", verdict: str = "signed_off", note: str | None = None
) -> ReviewAttestationRecord:
    return authority.attest(
        session_id=SESSION, state_id=STATE, payload_digest=DIGEST, reviewer=reviewer, verdict=verdict, note=note, record=_noop
    )


def _revoke_role(engine: Engine, role_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == role_id).values(revoked_at=datetime.now(UTC)))


# ── request ──────────────────────────────────────────────────────────────


def test_request_records_the_note_and_the_named_reviewer_and_audits_before_commit(engine: Engine, authority: RepositoryReviewAuthority) -> None:
    seen: list[ReviewRequestRecord] = []
    created = authority.request(session_id=SESSION, state_id=STATE, requested_by="alice", reviewer="bob", note="  please look  ", record=seen.append)
    assert seen == [created]
    assert (created.session_id, created.state_id, created.requested_by_identity_id, created.reviewer_identity_id) == (SESSION, STATE, "alice", "bob")
    assert created.request_note == "please look" and created.cancelled_at is None and created.open is True
    with engine.connect() as conn:
        row = conn.execute(select(review_requests_table).where(review_requests_table.c.request_id == created.request_id)).one()
    assert row.request_note == "please look" and row.reviewer_identity_id == "bob"


def test_request_audit_failure_rolls_the_row_back(engine: Engine, authority: RepositoryReviewAuthority) -> None:
    def fail(_record: ReviewRequestRecord) -> None:
        raise RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        authority.request(session_id=SESSION, state_id=STATE, requested_by="alice", reviewer="bob", note=None, record=fail)
    with engine.connect() as conn:
        assert conn.execute(select(review_requests_table)).all() == []


def test_request_refuses_a_second_open_request_for_the_state(authority: RepositoryReviewAuthority) -> None:
    first = _request(authority)
    with pytest.raises(OpenReviewRequestExists, match=STATE):
        _request(authority, reviewer="carol")
    # Derivation: closing the first (cancel) is what admits the second.
    authority.cancel(request_id=first.request_id, requested_by="alice", record=_noop)
    second = _request(authority, reviewer="carol")
    assert second.request_id != first.request_id and second.open is True


def test_request_refuses_a_reviewer_without_the_active_reviewer_role(engine: Engine, authority: RepositoryReviewAuthority) -> None:
    with pytest.raises(ReviewerRoleRequired, match="dave"):
        _request(authority, reviewer="dave")
    # Derivation on the authority row: revoke bob's grant and the same call that succeeds today refuses.
    _revoke_role(engine, "role-bob")
    with pytest.raises(ReviewerRoleRequired, match="bob"):
        _request(authority, reviewer="bob")
    assert _request(authority, reviewer="carol").reviewer_identity_id == "carol"


def test_request_refuses_an_expired_reviewer_grant(engine: Engine, authority: RepositoryReviewAuthority) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(identity_roles_table)
            .where(identity_roles_table.c.role_id == "role-bob")
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )
    with pytest.raises(ReviewerRoleRequired, match="bob"):
        _request(authority, reviewer="bob")


def test_request_refuses_the_author_as_reviewer(authority: RepositoryReviewAuthority) -> None:
    with pytest.raises(ReviewerIsAuthor, match="alice"):
        _request(authority, reviewer="alice")


def test_request_refuses_an_inactive_participant(engine: Engine, authority: RepositoryReviewAuthority) -> None:
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "bob").values(access_state="disabled"))
    with pytest.raises(ReviewParticipantNotActive, match="bob"):
        _request(authority, reviewer="bob")


def test_request_refuses_a_session_the_requester_does_not_own(authority: RepositoryReviewAuthority) -> None:
    with pytest.raises(SessionNotOwnedByRequester, match=SESSION):
        authority.request(session_id=SESSION, state_id=STATE, requested_by="bob", reviewer="carol", note=None, record=_noop)


def test_request_refuses_a_state_outside_the_session(authority: RepositoryReviewAuthority) -> None:
    with pytest.raises(StateNotInSession, match="state-elsewhere"):
        authority.request(session_id=SESSION, state_id="state-elsewhere", requested_by="alice", reviewer="bob", note=None, record=_noop)


def test_request_note_is_bounded_in_bytes_not_characters(authority: RepositoryReviewAuthority) -> None:
    assert _request(authority, note="a" * MAX_REVIEW_NOTE_BYTES).request_note == "a" * MAX_REVIEW_NOTE_BYTES
    authority.cancel(request_id=authority.open_for(reviewer="bob")[0].request_id, requested_by="alice", record=_noop)
    # 1366 euro signs are 1366 characters but 4098 bytes.
    with pytest.raises(ReviewNoteTooLong, match=str(MAX_REVIEW_NOTE_BYTES)):
        _request(authority, note="€" * 1366)


def _statements(engine: Engine) -> list[str]:
    seen: list[str] = []

    def capture(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    return seen


def test_request_by_an_admin_requester_takes_the_population_lock_first(engine: Engine, authority: RepositoryReviewAuthority) -> None:
    """approval_lifecycle_authority.py:3-8: admin-population-before-target when an admin can participate."""
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(insert(sessions_table).values(id="sess-root", user_id="root", auth_provider_type="local", title="t", created_at=now, updated_at=now))
        conn.execute(insert(composition_states_table).values(id="state-root", session_id="sess-root", version=1, provenance="session_seed", created_at=now))
    seen = _statements(engine)
    authority.request(session_id="sess-root", state_id="state-root", requested_by="root", reviewer="bob", note=None, record=_noop)
    population = next(i for i, s in enumerate(seen) if "FROM identity_roles JOIN identities" in s)
    target = next(i for i, s in enumerate(seen) if "FROM identities" in s and "JOIN" not in s)
    assert population < target
    # Derivation: a non-admin requester never touches the admin population.
    seen.clear()
    _request(authority)
    assert not any("FROM identity_roles JOIN identities" in s for s in seen)


# ── inbox ────────────────────────────────────────────────────────────────


def test_unaddressed_request_is_visible_to_every_reviewer_and_a_named_one_only_to_its_reviewer(authority: RepositoryReviewAuthority) -> None:
    unaddressed = _request(authority, reviewer=None)
    assert [r.request_id for r in authority.open_for(reviewer="bob")] == [unaddressed.request_id]
    assert [r.request_id for r in authority.open_for(reviewer="carol")] == [unaddressed.request_id]
    authority.cancel(request_id=unaddressed.request_id, requested_by="alice", record=_noop)
    named = _request(authority, reviewer="bob")
    assert [r.request_id for r in authority.open_for(reviewer="bob")] == [named.request_id]
    assert authority.open_for(reviewer="carol") == ()


# ── attest ───────────────────────────────────────────────────────────────


def test_attest_snapshots_the_author_and_closes_the_open_request(engine: Engine, authority: RepositoryReviewAuthority) -> None:
    requested = _request(authority)
    seen: list[ReviewAttestationRecord] = []
    attested = authority.attest(
        session_id=SESSION, state_id=STATE, payload_digest=DIGEST, reviewer="bob", verdict="signed_off", note=None, record=seen.append
    )
    assert seen == [attested]
    assert (attested.reviewer_identity_id, attested.author_identity_id, attested.payload_digest, attested.verdict) == ("bob", "alice", DIGEST, "signed_off")
    assert authority.read_request(request_id=requested.request_id).open is False
    assert authority.open_for(reviewer="bob") == ()
    with engine.connect() as conn:
        row = conn.execute(select(review_requests_table).where(review_requests_table.c.request_id == requested.request_id)).one()
    assert row.cancelled_at is None, "closure is derived from the attestation, never written onto the request"


def test_attest_refuses_the_author_and_the_check_backs_it(engine: Engine, authority: RepositoryReviewAuthority) -> None:
    with engine.begin() as conn:
        conn.execute(insert(identity_roles_table).values(role_id="role-alice", identity_id="alice", role="reviewer", granted_at=datetime.now(UTC), granted_by_identity_id="root"))
    with pytest.raises(ReviewerIsAuthor, match="alice"):
        _attest(authority, reviewer="alice")
    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_review_attestations_reviewer_is_not_author"):
        conn.execute(
            insert(review_attestations_table).values(
                attestation_id=str(uuid4()), session_id=SESSION, state_id=STATE, payload_digest=DIGEST,
                reviewer_identity_id="alice", author_identity_id="alice", attested_at=datetime.now(UTC), verdict="signed_off",
            )
        )


def test_attest_requires_the_active_reviewer_role(engine: Engine, authority: RepositoryReviewAuthority) -> None:
    with pytest.raises(ReviewerRoleRequired, match="dave"):
        _attest(authority, reviewer="dave")
    _revoke_role(engine, "role-bob")
    with pytest.raises(ReviewerRoleRequired, match="bob"):
        _attest(authority, reviewer="bob")
    assert _attest(authority, reviewer="carol").reviewer_identity_id == "carol"


def test_changes_requested_needs_a_nonblank_note(authority: RepositoryReviewAuthority) -> None:
    with pytest.raises(ChangesRequestedNeedsNote, match="changes_requested"):
        _attest(authority, verdict="changes_requested", note="   ")
    assert _attest(authority, verdict="changes_requested", note="rename the sink").note == "rename the sink"
    # Derivation: the other two verdicts leave the note optional.
    assert _attest(authority, reviewer="carol", verdict="signed_off", note=None).note is None
    assert _attest(authority, reviewer="carol", verdict="withdrawn", note=None).verdict == "withdrawn"


def test_attest_refuses_an_unknown_verdict(authority: RepositoryReviewAuthority) -> None:
    with pytest.raises(ValueError, match="verdict"):
        _attest(authority, verdict="approved")


def test_two_attestations_on_one_digest_are_both_recorded(engine: Engine, authority: RepositoryReviewAuthority) -> None:
    _request(authority, reviewer=None)
    _attest(authority, reviewer="bob")
    _attest(authority, reviewer="carol", verdict="changes_requested", note="one more pass")
    rows = authority.attestations_for(session_id=SESSION, state_id=STATE)
    assert [(r.reviewer_identity_id, r.verdict) for r in rows] == [("bob", "signed_off"), ("carol", "changes_requested")]
    with engine.connect() as conn:
        assert len(conn.execute(select(review_attestations_table)).all()) == 2, "a ledger keeps both rows; never collapse them to a boolean"


def test_attest_without_any_request_is_recorded(authority: RepositoryReviewAuthority) -> None:
    """A ledger: nothing about the request's state refuses an attestation (spec :1418)."""
    assert _attest(authority).verdict == "signed_off"


# ── cancel ───────────────────────────────────────────────────────────────


def test_cancel_is_requester_only_and_closes_once(authority: RepositoryReviewAuthority) -> None:
    requested = _request(authority)
    with pytest.raises(ReviewRequestNotFound, match=requested.request_id):
        authority.cancel(request_id=requested.request_id, requested_by="bob", record=_noop)
    seen: list[ReviewRequestRecord] = []
    cancelled = authority.cancel(request_id=requested.request_id, requested_by="alice", record=seen.append)
    assert seen == [cancelled] and cancelled.cancelled_at is not None and cancelled.open is False
    with pytest.raises(ReviewRequestAlreadyClosed, match=requested.request_id):
        authority.cancel(request_id=requested.request_id, requested_by="alice", record=_noop)


def test_cancel_refuses_a_request_already_closed_by_an_attestation(authority: RepositoryReviewAuthority) -> None:
    requested = _request(authority)
    _attest(authority)
    with pytest.raises(ReviewRequestAlreadyClosed, match=requested.request_id):
        authority.cancel(request_id=requested.request_id, requested_by="alice", record=_noop)


def test_cancel_of_an_unknown_request_is_not_found(authority: RepositoryReviewAuthority) -> None:
    with pytest.raises(ReviewRequestNotFound, match="nope"):
        authority.cancel(request_id="nope", requested_by="alice", record=_noop)
```

- [ ] **Step 2: Run the authority tests to verify they fail on the missing module.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_review_authority.py -n 0 > /tmp/i4-authority-red.log 2>&1; echo exit=$?
```

Expected: `exit=2` (collection error) with
`ModuleNotFoundError: No module named 'elspeth.web.coordination.review_authority'`
in the log.

- [ ] **Step 3: Write the review authority.**

Create `src/elspeth/web/coordination/review_authority.py`:

```python
"""Sole writer of ``review_requests`` and ``review_attestations``.

A review REQUEST is a control (one open request per ``(session_id,
state_id)``, requester-only cancel, a named reviewer must hold the role);
a reviewer ATTESTATION is a ledger (spec §Workflow tables, ``review_attestations``:
"nothing refuses on it"). Both are written here and nowhere else — the
mutation-authority manifest binds every ``RepositoryReviewAuthority.*``
writer to ``ReviewAuthority``.

Lock order (approval_lifecycle_authority.py:3-8): every mutation locks the
participating ``identities`` rows ``FOR UPDATE`` before touching a review
row and rejects a participant that is not ``active``. If the requester holds
deployment ``admin`` — possible only for the requester, R8 bars an admin
reviewer — the admin population is locked FIRST (``_ADMIN_HOLDER_ROWS_FOR_UPDATE``),
then the participants in stable identity-id order; otherwise stable id order
alone. No session-operation fence is taken: a reviewer is not the session's
owner and an attestation must neither wait on nor evict the author's lease.

"Closed" is DERIVED, never written: a request is open while ``cancelled_at``
is NULL and no attestation on the same pair carries ``attested_at >=
requested_at`` (spec :1419). Attesting therefore writes exactly one row.

Audit is the caller's. Each mutation takes a required ``record`` callback
and invokes it after the rows are written and BEFORE commit, so a failed
audit rolls the mutation back — the identity authority's rule
(identity_authority.py:15-38). This module never opens the Landscape.

THE CONNECTION NEVER LEAVES THE METHOD THAT OPENED IT. Every SELECT is a
module-level constant with bound parameters, and the three writes are inline
``insert(<table>)`` / ``update(<table>)`` calls on the models table objects —
the two forms the writer manifest already resolves for the identity and
cancellation authorities.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast, final, get_args

from sqlalchemy import bindparam, exists, insert, or_, select, update
from sqlalchemy.engine import Connection, Engine, Row

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.identity_authority import _ADMIN_HOLDER_ROWS_FOR_UPDATE
from elspeth.web.sessions.models import (
    composition_states_table,
    identities_table,
    identity_roles_table,
    review_attestations_table,
    review_requests_table,
    sessions_table,
)

ReviewVerdict = Literal["signed_off", "changes_requested", "withdrawn"]
"""Closed set; the same three values as ``_REVIEW_VERDICT_CHECK`` (sessions/models.py:3583)."""

_VERDICT_VALUES: Final = frozenset(get_args(ReviewVerdict))

MAX_REVIEW_NOTE_BYTES: Final = 4096
"""Spec :1418-1419: notes are bounded plain text, 4 KiB. Bytes, at the write boundary (models.py:3577-3581)."""


# ── refusals (closed set; routes translate exact types) ───────────────────


class ReviewAuthorityRefusal(RuntimeError):
    """Base of the closed refusal set. Routes switch on exact type, never ``isinstance``."""


class ReviewRequestNotFound(ReviewAuthorityRefusal):
    """No open-or-closed request with that id visible to this requester."""


class SessionNotOwnedByRequester(ReviewAuthorityRefusal):
    """The session is missing, archived, or owned by someone else."""


class StateNotInSession(ReviewAuthorityRefusal):
    """The state id is not a composition state of that session."""


class ReviewParticipantNotActive(ReviewAuthorityRefusal):
    """A participant's ``identities.access_state`` is not ``active``."""


class ReviewerRoleRequired(ReviewAuthorityRefusal):
    """The reviewer holds no unrevoked, unexpired, deployment-wide ``reviewer`` grant."""


class ReviewerIsAuthor(ReviewAuthorityRefusal):
    """Reviewer and author are the same identity (the CHECK at models.py:3772 backs this)."""


class OpenReviewRequestExists(ReviewAuthorityRefusal):
    """One open request per ``(session_id, state_id)``."""


class ReviewRequestAlreadyClosed(ReviewAuthorityRefusal):
    """Cancelled already, or closed by an attestation."""


class ChangesRequestedNeedsNote(ReviewAuthorityRefusal):
    """``changes_requested`` must carry a non-blank note (spec :1418, rev2.8)."""


class ReviewNoteTooLong(ReviewAuthorityRefusal):
    """The note exceeds ``MAX_REVIEW_NOTE_BYTES`` bytes."""


# ── records ──────────────────────────────────────────────────────────────


@final
@dataclass(frozen=True, slots=True)
class ReviewRequestRecord:
    request_id: str
    session_id: str
    state_id: str
    requested_by_identity_id: str
    reviewer_identity_id: str | None
    requested_at: datetime
    cancelled_at: datetime | None
    request_note: str | None
    open: bool


@final
@dataclass(frozen=True, slots=True)
class ReviewAttestationRecord:
    attestation_id: str
    session_id: str
    state_id: str
    payload_digest: str
    reviewer_identity_id: str
    author_identity_id: str
    attested_at: datetime
    verdict: ReviewVerdict
    note: str | None


# ── statements (module-level constants; the manifest follows these names) ─

_ATTESTED_SINCE_REQUEST: Final = exists().where(
    review_attestations_table.c.session_id == review_requests_table.c.session_id,
    review_attestations_table.c.state_id == review_requests_table.c.state_id,
    review_attestations_table.c.attested_at >= review_requests_table.c.requested_at,
)
_REQUEST_ROWS: Final = select(review_requests_table, _ATTESTED_SINCE_REQUEST.label("attested"))
_REQUEST_BY_ID: Final = _REQUEST_ROWS.where(review_requests_table.c.request_id == bindparam("request_id"))
_OPEN_REQUESTS_FOR_PAIR: Final = _REQUEST_ROWS.where(
    review_requests_table.c.session_id == bindparam("session_id"),
    review_requests_table.c.state_id == bindparam("state_id"),
    review_requests_table.c.cancelled_at.is_(None),
    ~_ATTESTED_SINCE_REQUEST,
)
_OPEN_REQUESTS_FOR_REVIEWER: Final = (
    _REQUEST_ROWS.where(
        review_requests_table.c.cancelled_at.is_(None),
        ~_ATTESTED_SINCE_REQUEST,
        or_(
            review_requests_table.c.reviewer_identity_id == bindparam("reviewer"),
            review_requests_table.c.reviewer_identity_id.is_(None),
        ),
    )
    .order_by(review_requests_table.c.requested_at, review_requests_table.c.request_id)
)
_ATTESTATIONS_FOR_PAIR: Final = (
    select(review_attestations_table)
    .where(
        review_attestations_table.c.session_id == bindparam("session_id"),
        review_attestations_table.c.state_id == bindparam("state_id"),
    )
    .order_by(review_attestations_table.c.attested_at, review_attestations_table.c.attestation_id)
)
_IDENTITY_FOR_UPDATE: Final = (
    select(identities_table.c.identity_id, identities_table.c.access_state)
    .where(identities_table.c.identity_id == bindparam("identity_id"))
    .with_for_update()
)
_LIVE_ROLE_ROWS: Final = select(identity_roles_table.c.role, identity_roles_table.c.expires_at).where(
    identity_roles_table.c.identity_id == bindparam("identity_id"),
    identity_roles_table.c.scope.is_(None),
    identity_roles_table.c.revoked_at.is_(None),
)
_SESSION_BY_ID: Final = select(sessions_table.c.id, sessions_table.c.user_id, sessions_table.c.archived_at).where(
    sessions_table.c.id == bindparam("session_id")
)
_STATE_IN_SESSION: Final = select(composition_states_table.c.id).where(
    composition_states_table.c.id == bindparam("state_id"),
    composition_states_table.c.session_id == bindparam("session_id"),
)


# ── helpers (no connection, no statement construction) ───────────────────


def _require_nonblank(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank exact string")


def _require_verdict(value: object) -> ReviewVerdict:
    if type(value) is not str or value not in _VERDICT_VALUES:
        raise ValueError("verdict must be one of signed_off, changes_requested or withdrawn")
    return cast(ReviewVerdict, value)


def _bounded_note(value: str | None) -> str | None:
    """Blank collapses to ``None``; longer than 4 KiB in UTF-8 bytes refuses."""
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("note must be a string or None")
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped.encode("utf-8")) > MAX_REVIEW_NOTE_BYTES:
        raise ReviewNoteTooLong(f"note exceeds {MAX_REVIEW_NOTE_BYTES} bytes")
    return stripped


def _ensure_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _is_live(expires_at: datetime | None, now: datetime) -> bool:
    return expires_at is None or _ensure_utc(expires_at) > now


def _request_record(row: Row[Any]) -> ReviewRequestRecord:
    cancelled_at = None if row.cancelled_at is None else _ensure_utc(row.cancelled_at)
    return ReviewRequestRecord(
        request_id=row.request_id,
        session_id=row.session_id,
        state_id=row.state_id,
        requested_by_identity_id=row.requested_by_identity_id,
        reviewer_identity_id=row.reviewer_identity_id,
        requested_at=_ensure_utc(row.requested_at),
        cancelled_at=cancelled_at,
        request_note=row.request_note,
        open=cancelled_at is None and not bool(row.attested),
    )


def _attestation_record(row: Row[Any]) -> ReviewAttestationRecord:
    return ReviewAttestationRecord(
        attestation_id=row.attestation_id,
        session_id=row.session_id,
        state_id=row.state_id,
        payload_digest=row.payload_digest,
        reviewer_identity_id=row.reviewer_identity_id,
        author_identity_id=row.author_identity_id,
        attested_at=_ensure_utc(row.attested_at),
        verdict=_require_verdict(row.verdict),
        note=row.note,
    )


@final
class RepositoryReviewAuthority:
    """One engine, no settings, no clock, no recorder — the identity authority's shape."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ── reads ────────────────────────────────────────────────────────

    def read_request(self, *, request_id: str) -> ReviewRequestRecord | None:
        _require_nonblank(request_id, "request_id")
        with self._engine.connect() as conn:
            row = conn.execute(_REQUEST_BY_ID, {"request_id": request_id}).one_or_none()
        return None if row is None else _request_record(row)

    def open_for(self, *, reviewer: str) -> tuple[ReviewRequestRecord, ...]:
        """Open rows addressed to ``reviewer`` plus open unaddressed rows (spec :1419, the badge's count)."""
        _require_nonblank(reviewer, "reviewer")
        with self._engine.connect() as conn:
            rows = conn.execute(_OPEN_REQUESTS_FOR_REVIEWER, {"reviewer": reviewer}).all()
        return tuple(_request_record(row) for row in rows)

    def attestations_for(self, *, session_id: str, state_id: str) -> tuple[ReviewAttestationRecord, ...]:
        _require_nonblank(session_id, "session_id")
        _require_nonblank(state_id, "state_id")
        with self._engine.connect() as conn:
            rows = conn.execute(_ATTESTATIONS_FOR_PAIR, {"session_id": session_id, "state_id": state_id}).all()
        return tuple(_attestation_record(row) for row in rows)

    # ── writes ───────────────────────────────────────────────────────

    def request(
        self,
        *,
        session_id: str,
        state_id: str,
        requested_by: str,
        reviewer: str | None,
        note: str | None,
        record: Callable[[ReviewRequestRecord], None],
    ) -> ReviewRequestRecord:
        _require_nonblank(session_id, "session_id")
        _require_nonblank(state_id, "state_id")
        _require_nonblank(requested_by, "requested_by")
        if reviewer is not None:
            _require_nonblank(reviewer, "reviewer")
            if reviewer == requested_by:
                raise ReviewerIsAuthor(f"{requested_by} cannot review their own session")
        bounded_note = _bounded_note(note)
        participants = (requested_by,) if reviewer is None else (requested_by, reviewer)
        with self._engine.begin() as conn:
            now = database_now(conn)
            self._lock_participants(conn, participants, now=now, admin_candidate=requested_by)
            if reviewer is not None and not self._holds_live_role(conn, reviewer, "reviewer", now=now):
                raise ReviewerRoleRequired(f"{reviewer} holds no active reviewer role")
            session = conn.execute(_SESSION_BY_ID, {"session_id": session_id}).one_or_none()
            if session is None or session.archived_at is not None or session.user_id != requested_by:
                raise SessionNotOwnedByRequester(f"session {session_id} is not owned by {requested_by}")
            if conn.execute(_STATE_IN_SESSION, {"state_id": state_id, "session_id": session_id}).first() is None:
                raise StateNotInSession(f"state {state_id} is not a state of session {session_id}")
            if conn.execute(_OPEN_REQUESTS_FOR_PAIR, {"session_id": session_id, "state_id": state_id}).first() is not None:
                raise OpenReviewRequestExists(f"an open review request already exists for state {state_id}")
            request_id = str(uuid.uuid4())
            conn.execute(
                insert(review_requests_table).values(
                    request_id=request_id,
                    session_id=session_id,
                    state_id=state_id,
                    requested_by_identity_id=requested_by,
                    reviewer_identity_id=reviewer,
                    requested_at=now,
                    cancelled_at=None,
                    request_note=bounded_note,
                )
            )
            created = _request_record(conn.execute(_REQUEST_BY_ID, {"request_id": request_id}).one())
            # BEFORE commit: an audit row this trail cannot hold rolls the request back.
            record(created)
            return created

    def cancel(
        self,
        *,
        request_id: str,
        requested_by: str,
        record: Callable[[ReviewRequestRecord], None],
    ) -> ReviewRequestRecord:
        _require_nonblank(request_id, "request_id")
        _require_nonblank(requested_by, "requested_by")
        with self._engine.begin() as conn:
            now = database_now(conn)
            self._lock_participants(conn, (requested_by,), now=now, admin_candidate=requested_by)
            result = conn.execute(
                update(review_requests_table)
                .where(
                    review_requests_table.c.request_id == request_id,
                    review_requests_table.c.requested_by_identity_id == requested_by,
                    review_requests_table.c.cancelled_at.is_(None),
                    ~_ATTESTED_SINCE_REQUEST,
                )
                .values(cancelled_at=now)
            )
            if result.rowcount != 1:
                current = conn.execute(_REQUEST_BY_ID, {"request_id": request_id}).one_or_none()
                # Not found and not-yours read the same: a requester learns nothing about others' requests.
                if current is None or current.requested_by_identity_id != requested_by:
                    raise ReviewRequestNotFound(f"review request {request_id} not found")
                raise ReviewRequestAlreadyClosed(f"review request {request_id} is already closed")
            cancelled = _request_record(conn.execute(_REQUEST_BY_ID, {"request_id": request_id}).one())
            record(cancelled)
            return cancelled

    def attest(
        self,
        *,
        session_id: str,
        state_id: str,
        payload_digest: str,
        reviewer: str,
        verdict: ReviewVerdict,
        note: str | None,
        record: Callable[[ReviewAttestationRecord], None],
    ) -> ReviewAttestationRecord:
        _require_nonblank(session_id, "session_id")
        _require_nonblank(state_id, "state_id")
        _require_nonblank(payload_digest, "payload_digest")
        _require_nonblank(reviewer, "reviewer")
        verdict = _require_verdict(verdict)
        bounded_note = _bounded_note(note)
        if verdict == "changes_requested" and bounded_note is None:
            raise ChangesRequestedNeedsNote("changes_requested must carry a non-blank note")
        with self._engine.begin() as conn:
            now = database_now(conn)
            session = conn.execute(_SESSION_BY_ID, {"session_id": session_id}).one_or_none()
            if session is None or session.archived_at is not None:
                raise StateNotInSession(f"session {session_id} is not reviewable")
            if conn.execute(_STATE_IN_SESSION, {"state_id": state_id, "session_id": session_id}).first() is None:
                raise StateNotInSession(f"state {state_id} is not a state of session {session_id}")
            # The author is a SNAPSHOT of sessions.user_id at attestation time (models.py:3757-3762).
            author: str = session.user_id
            if reviewer == author:
                raise ReviewerIsAuthor(f"{reviewer} cannot attest their own session")
            # Author may hold admin (an admin can own a session); the reviewer cannot (R8).
            self._lock_participants(conn, (reviewer, author), now=now, admin_candidate=author)
            if not self._holds_live_role(conn, reviewer, "reviewer", now=now):
                raise ReviewerRoleRequired(f"{reviewer} holds no active reviewer role")
            attestation_id = str(uuid.uuid4())
            conn.execute(
                insert(review_attestations_table).values(
                    attestation_id=attestation_id,
                    session_id=session_id,
                    state_id=state_id,
                    payload_digest=payload_digest,
                    reviewer_identity_id=reviewer,
                    author_identity_id=author,
                    attested_at=now,
                    verdict=verdict,
                    note=bounded_note,
                )
            )
            rows = conn.execute(_ATTESTATIONS_FOR_PAIR, {"session_id": session_id, "state_id": state_id}).all()
            attested = next(_attestation_record(row) for row in rows if row.attestation_id == attestation_id)
            record(attested)
            return attested

    # ── locks and role reads (take a connection the public method opened) ─

    def _lock_participants(self, conn: Connection, participants: Sequence[str], *, now: datetime, admin_candidate: str) -> None:
        """Admin population first when the candidate holds admin, then participants in stable id order."""
        if self._holds_live_role(conn, admin_candidate, "admin", now=now):
            conn.execute(_ADMIN_HOLDER_ROWS_FOR_UPDATE).all()
        for identity_id in sorted(set(participants)):
            row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": identity_id}).one_or_none()
            if row is None or row.access_state != "active":
                raise ReviewParticipantNotActive(f"{identity_id} is not an active identity")

    @staticmethod
    def _holds_live_role(conn: Connection, identity_id: str, role: str, *, now: datetime) -> bool:
        rows = conn.execute(_LIVE_ROLE_ROWS, {"identity_id": identity_id}).all()
        return any(row.role == role and _is_live(row.expires_at, now) for row in rows)
```

- [ ] **Step 4: Run the authority tests to verify they pass.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_review_authority.py -n 0 > /tmp/i4-authority-green.log 2>&1; echo exit=$?
```

Expected: `exit=0`, summary `22 passed`. If
`test_request_by_an_admin_requester_takes_the_population_lock_first` fails
on the `population` lookup, print `seen` — the admin-holder statement is the
one compiled from `_ADMIN_HOLDER_ROWS` (identity_authority.py:777-784) and
contains `FROM identity_roles JOIN identities`; fix the assertion's substring
to the compiled text, never the lock order.

- [ ] **Step 5: Write the failing audit-writer test.**

Append to `tests/unit/web/auth/test_audit.py` (after :1050, end of file):

```python


def test_review_rows_are_anchored_on_the_actor_and_carry_the_pair(tmp_path: Any) -> None:
    """I4: the three ``review_*`` event types (auth_audit_repository.py:20-45), anchored on who acted."""
    recorder, url = _durable_recorder(tmp_path)
    recorder.record_review_requested(
        _request(),
        provider="local",
        request_id="req-1",
        session_id="sess-1",
        state_id="state-1",
        requested_by_identity_id="alice",
        reviewer_identity_id="bob",
        note="please look",
    )
    recorder.record_review_request_cancelled(
        None,
        provider="local",
        request_id="req-1",
        session_id="sess-1",
        state_id="state-1",
        requested_by_identity_id="alice",
    )
    recorder.record_review_attested(
        _request(),
        provider="local",
        attestation_id="att-1",
        session_id="sess-1",
        state_id="state-1",
        payload_digest="sha256:" + "ab" * 32,
        reviewer_identity_id="bob",
        author_identity_id="alice",
        verdict="changes_requested",
        note="rename the sink",
    )
    rows = _durable_rows(url)
    assert [row.event_type for row in rows] == ["review_requested", "review_request_cancelled", "review_attested"]
    assert [row.identity_id for row in rows] == ["alice", "alice", "bob"]
    assert all((row.outcome, row.provider, row.user_id, row.username) == ("success", "local", None, None) for row in rows)
    requested = _metadata(rows[0])
    assert (requested["actor"], requested["request_id"], requested["session_id"], requested["state_id"]) == ("alice", "req-1", "sess-1", "state-1")
    assert (requested["reviewer_identity_id"], requested["note"]) == ("bob", "please look")
    assert (rows[0].request_id, rows[0].client_host, rows[0].user_agent) == ("request-id", "127.0.0.1", "bounded-agent")
    assert (rows[1].request_id, rows[1].client_host, rows[1].user_agent) == (None, None, None)
    assert _metadata(rows[1])["request_id"] == "req-1"
    attested = _metadata(rows[2])
    assert (attested["actor"], attested["attestation_id"], attested["author_identity_id"], attested["verdict"], attested["note"]) == (
        "bob",
        "att-1",
        "alice",
        "changes_requested",
        "rename the sink",
    )
    assert attested["payload_digest"] == "sha256:" + "ab" * 32
    assert all({key: _metadata(row)[key] for key in _PROVENANCE} == _PROVENANCE for row in rows)
```

- [ ] **Step 6: Run it to verify it fails on the missing writer.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/auth/test_audit.py -n 0 -k review_rows > /tmp/i4-audit-red.log 2>&1; echo exit=$?
```

Expected: `exit=1`, `AttributeError: 'AuthAuditRecorder' object has no attribute 'record_review_requested'`.

- [ ] **Step 7: Add the three writers to the Protocol, the operation enum and the recorder.**

In `src/elspeth/web/auth/audit.py`, after the Protocol member
`record_relationship_changed` (:238-252, ends `) -> None: ...`), append:

```python
    # I4 review workflow. No console provenance: a human requester or
    # reviewer acts for themselves, so both L0 keys are recorded as None.
    def record_review_requested(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        request_id: str,
        session_id: str,
        state_id: str,
        requested_by_identity_id: str,
        reviewer_identity_id: str | None,
        note: str | None,
    ) -> None: ...

    def record_review_request_cancelled(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        request_id: str,
        session_id: str,
        state_id: str,
        requested_by_identity_id: str,
    ) -> None: ...

    def record_review_attested(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        attestation_id: str,
        session_id: str,
        state_id: str,
        payload_digest: str,
        reviewer_identity_id: str,
        author_identity_id: str,
        verdict: str,
        note: str | None,
    ) -> None: ...
```

After `RELATIONSHIP_CHANGED = "relationship_changed"` (:287) in
`class AuthAuditOperation`, append:

```python
    REVIEW_REQUESTED = "review_requested"
    REVIEW_REQUEST_CANCELLED = "review_request_cancelled"
    REVIEW_ATTESTED = "review_attested"
```

After the recorder's `record_relationship_changed` (:1140-1180), before
`class _AdminProvenanceMetadata` (:1183), append:

```python
    # ── I4 review workflow: anchored on the ACTOR, the other participant in metadata ──

    def record_review_requested(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        request_id: str,
        session_id: str,
        state_id: str,
        requested_by_identity_id: str,
        reviewer_identity_id: str | None,
        note: str | None,
    ) -> None:
        provenance = _admin_provenance(request, actor_identity_id=requested_by_identity_id, on_behalf_of=None, console_request_id=None)
        with self._open_landscape(AuthAuditOperation.REVIEW_REQUESTED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="review_requested",
                outcome="success",
                provider=provider,
                identity_id=requested_by_identity_id,
                user_id=None,
                username=None,
                failure_category=None,
                metadata={
                    **provenance.metadata,
                    "request_id": request_id,
                    "session_id": session_id,
                    "state_id": state_id,
                    "reviewer_identity_id": reviewer_identity_id,
                    "note": _bounded_text(note),
                },
                **provenance.request_columns,
            )

    def record_review_request_cancelled(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        request_id: str,
        session_id: str,
        state_id: str,
        requested_by_identity_id: str,
    ) -> None:
        provenance = _admin_provenance(request, actor_identity_id=requested_by_identity_id, on_behalf_of=None, console_request_id=None)
        with self._open_landscape(AuthAuditOperation.REVIEW_REQUEST_CANCELLED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="review_request_cancelled",
                outcome="success",
                provider=provider,
                identity_id=requested_by_identity_id,
                user_id=None,
                username=None,
                failure_category=None,
                metadata={
                    **provenance.metadata,
                    "request_id": request_id,
                    "session_id": session_id,
                    "state_id": state_id,
                },
                **provenance.request_columns,
            )

    def record_review_attested(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        attestation_id: str,
        session_id: str,
        state_id: str,
        payload_digest: str,
        reviewer_identity_id: str,
        author_identity_id: str,
        verdict: str,
        note: str | None,
    ) -> None:
        provenance = _admin_provenance(request, actor_identity_id=reviewer_identity_id, on_behalf_of=None, console_request_id=None)
        with self._open_landscape(AuthAuditOperation.REVIEW_ATTESTED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="review_attested",
                outcome="success",
                provider=provider,
                identity_id=reviewer_identity_id,
                user_id=None,
                username=None,
                failure_category=None,
                metadata={
                    **provenance.metadata,
                    "attestation_id": attestation_id,
                    "session_id": session_id,
                    "state_id": state_id,
                    "payload_digest": payload_digest,
                    "author_identity_id": author_identity_id,
                    "verdict": verdict,
                    "note": _bounded_text(note),
                },
                **provenance.request_columns,
            )
```

In `tests/unit/web/auth/test_identity_admin_routes.py`, after
`record_relationship_changed` (:96-97) inside `_RecordingAuditWriter`, append
the three members so the fake stays complete (they are inert here; the
review routes have their own recording fake in Step 12):

```python
    def record_review_requested(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_review_requested", request, kwargs)

    def record_review_request_cancelled(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_review_request_cancelled", request, kwargs)

    def record_review_attested(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_review_attested", request, kwargs)
```

- [ ] **Step 8: Run the audit tests to verify they pass, and that nothing else in the auth suite moved.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py -n 0 > /tmp/i4-audit-green.log 2>&1; echo exit=$?
```

Expected: `exit=0`; the summary line shows every test passed and
`test_review_rows_are_anchored_on_the_actor_and_carry_the_pair` is among them.

- [ ] **Step 9: Bind the writers in the mutation-authority manifest and run the gate.**

Run the gate first to obtain the fingerprints (it XFAILs on drift rather than
failing, so the exit code is 0 either way — read the summary word):

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities -n 0 > /tmp/i4-manifest-drift.log 2>&1; echo exit=$?
```

Expected: `exit=0` and `1 xfailed`; the reason text lists
`Unexpected/unreviewed (3):` with one line each of the form
`src/elspeth/web/coordination/review_authority.py:<line> RepositoryReviewAuthority.request insert review_requests fp=<16 hex>#1 authority=UNCLASSIFIED connection_escape=False`
(and the same for `RepositoryReviewAuthority.cancel update review_requests`
and `RepositoryReviewAuthority.attest insert review_attestations`), plus
`Writers without a named authority (3):` repeating them. A line reading
`Unresolved write executions` with a non-zero count means a write is not an
inline `insert(<table>)` / `update(<table>)` on the models table object, the
form the scanner resolves for identity_authority.py:2625-2635 — fix the
module, not the manifest.

Then, in `tests/unit/architecture/test_session_db_mutation_authority.py`,
append after the last `RepositoryIdentityAuthority.*` entry of the identity
block in `_NAMED_AUTHORITY_SYMBOLS` (the block starts at :806):

```python
    # ── I4 review workflow: RepositoryReviewAuthority, method-exact; every
    # acquisition stays inside its method; the record callback receives a
    # frozen record, never a connection ──────────────────────────────────
    AuthoritySymbol(
        "src/elspeth/web/coordination/review_authority.py",
        "RepositoryReviewAuthority.request",
        "ReviewAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/review_authority.py",
        "RepositoryReviewAuthority.cancel",
        "ReviewAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/review_authority.py",
        "RepositoryReviewAuthority.attest",
        "ReviewAuthority",
    ),
```

and append to `_REVIEWED_WRITERS` (:1198), copying the fingerprint, the
`#ordinal` and the `line` VERBATIM from the drift log into the three
`FP_FROM_LOG` / `LINE_FROM_LOG` slots below (the scanner hashes the module
as written on your branch, so these three values are measured there, never
typed from memory; the ordinal is `1` when each symbol writes its table
once, which the log confirms):

```python
    # I4: one open request per state, requester-only cancel, append-only
    # attestation. "Closed" is derived from attestations, never written, so
    # attest has exactly one writer.
    WriterIdentity(
        "src/elspeth/web/coordination/review_authority.py",
        "RepositoryReviewAuthority.request",
        "review_requests",
        "insert",
        "FP_FROM_LOG",
        1,
        "ReviewAuthority",
        line=LINE_FROM_LOG,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/review_authority.py",
        "RepositoryReviewAuthority.cancel",
        "review_requests",
        "update",
        "FP_FROM_LOG",
        1,
        "ReviewAuthority",
        line=LINE_FROM_LOG,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/review_authority.py",
        "RepositoryReviewAuthority.attest",
        "review_attestations",
        "insert",
        "FP_FROM_LOG",
        1,
        "ReviewAuthority",
        line=LINE_FROM_LOG,
    ),
```

Never widen a `TablePolicy`: :118-119 already name `ReviewAuthority` for
both tables. Re-run the gate:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py -n 0 > /tmp/i4-manifest-green.log 2>&1; echo exit=$?
```

Expected: `exit=0` and the summary contains `passed` with NO `xfailed`
(`grep -c xfailed /tmp/i4-manifest-green.log` prints `0`). Control the
instrument: temporarily change one `fp` character, re-run, confirm
`1 xfailed` returns with `Stale reviewed (1)`, then restore it.

- [ ] **Step 10: Write the failing route tests.**

Create `tests/unit/web/workflow/test_review_routes.py` (the package
`tests/unit/web/workflow/__init__.py` already exists from I3):

```python
"""Review routes over a REAL review authority on the closed local app (I8's ``closed_local_app``).

The audit writer is a recording fake: what these tests pin is that every
mutation hands the authority a record callback that fires with the fields
the trail needs; that the row is written INSIDE the transaction is the
authority's contract, pinned in test_review_authority.py.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import Request
from sqlalchemy import insert, select
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.hashing import canonical_json
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.review_authority import RepositoryReviewAuthority
from elspeth.web.sessions.models import composition_states_table, identity_roles_table, review_attestations_table, sessions_table
from elspeth.web.sessions.routes.workflow.reviews import create_reviews_router, review_payload_digest

SESSION = "11111111-1111-4111-8111-111111111111"
STATE = "22222222-2222-4222-8222-222222222222"
_STATE_CONTENT: dict[str, Any] = {
    "sources": {"source": {"plugin": "csv", "options": {"path": "in.csv"}}},
    "nodes": [{"id": "n1", "plugin": "passthrough", "options": {}}],
    "edges": [],
    "outputs": [],
    "metadata_": {"name": "demo", "description": ""},
}


@dataclass
class _AuditCall:
    method: str
    request_bound: bool
    kwargs: dict[str, Any]


@dataclass
class _RecordingAuditWriter:
    calls: list[_AuditCall] = field(default_factory=list)

    def _note(self, method: str, request: Request | None, kwargs: dict[str, Any]) -> None:
        self.calls.append(_AuditCall(method=method, request_bound=request is not None, kwargs=kwargs))

    def record_review_requested(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_review_requested", request, kwargs)

    def record_review_request_cancelled(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_review_request_cancelled", request, kwargs)

    def record_review_attested(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_review_attested", request, kwargs)

    def only(self, method: str) -> _AuditCall:
        matches = [call for call in self.calls if call.method == method]
        assert len(matches) == 1, [call.method for call in self.calls]
        return matches[0]


@pytest.fixture
def app(closed_local_app: Any) -> Any:
    """closed_local_app + the review router, a real review authority, a real identity authority and a recording audit fake."""
    engine = closed_local_app.app.state.phase3_engine
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol", "dave"):
            ensure_test_identity(conn, identity_id=identity_id)
        for role_id, identity_id, role in (("role-bob", "bob", "reviewer"), ("role-carol", "carol", "reviewer"), ("role-dave", "dave", "user")):
            conn.execute(insert(identity_roles_table).values(role_id=role_id, identity_id=identity_id, role=role, granted_at=now, granted_by_identity_id="alice"))
        conn.execute(insert(sessions_table).values(id=SESSION, user_id="alice", auth_provider_type="local", title="review me", created_at=now, updated_at=now))
        conn.execute(insert(composition_states_table).values(id=STATE, session_id=SESSION, version=1, provenance="session_seed", created_at=now, **_STATE_CONTENT))
    closed_local_app.app.state.review_authority = RepositoryReviewAuthority(engine)
    closed_local_app.app.state.identity_authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
    closed_local_app.app.state.auth_audit_recorder = _RecordingAuditWriter()
    closed_local_app.app.include_router(create_reviews_router())
    return closed_local_app


def _as(client: Any, identity_id: str) -> None:
    identity = UserIdentity(user_id=identity_id, username=identity_id)

    async def user() -> UserIdentity:
        return identity

    client.app.dependency_overrides[get_current_user] = user


def _governance(client: Any, value: str) -> None:
    client.app.state.settings = client.app.state.settings.model_copy(update={"workflow_governance": value})


def _request(client: Any, *, reviewer: str | None = "bob", note: str | None = "please look") -> Any:
    _as(client, "alice")
    return client.post(f"/api/sessions/{SESSION}/reviews", json={"state_id": STATE, "reviewer_identity_id": reviewer, "note": note})


# ── governance switch (R11's consumer; I8 owns the readiness refusal) ──────


def test_mutations_refuse_when_governance_is_off_and_the_inbox_is_empty(app: Any) -> None:
    _governance(app, "off")
    refused = _request(app)
    assert refused.status_code == 409 and refused.json()["detail"]["error_type"] == "workflow_governance_off"
    _as(app, "bob")
    assert app.get("/api/reviews/inbox").json() == {"requests": []}
    assert app.post("/api/reviews/any/attest", json={"verdict": "signed_off", "note": None}).json()["detail"]["error_type"] == "workflow_governance_off"
    assert app.post("/api/reviews/any/cancel").json()["detail"]["error_type"] == "workflow_governance_off"
    # Derivation: the switch, not the fixture, is what admits the request.
    _governance(app, "on")
    assert _request(app).status_code == 201
    # The refused attempts never reached the recorder: exactly one audit call, from the admitted request.
    assert [call.method for call in app.app.state.auth_audit_recorder.calls] == ["record_review_requested"]


# ── request ──────────────────────────────────────────────────────────────


def test_request_writes_the_row_and_the_audit_callback_fires_request_bound(app: Any) -> None:
    response = _request(app)
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["session_id"], body["state_id"], body["requested_by_identity_id"], body["reviewer_identity_id"], body["open"]) == (SESSION, STATE, "alice", "bob", True)
    assert body["request_note"] == "please look" and body["cancelled_at"] is None
    assert response.headers["cache-control"] == "no-store"
    call = app.app.state.auth_audit_recorder.only("record_review_requested")
    assert call.request_bound is True
    assert (call.kwargs["provider"], call.kwargs["request_id"], call.kwargs["reviewer_identity_id"], call.kwargs["note"]) == ("local", body["request_id"], "bob", "please look")


def test_request_on_a_session_the_caller_does_not_own_is_404(app: Any) -> None:
    _as(app, "bob")
    response = app.post(f"/api/sessions/{SESSION}/reviews", json={"state_id": STATE, "reviewer_identity_id": None, "note": None})
    assert response.status_code == 404
    assert app.app.state.auth_audit_recorder.calls == []


def test_request_refusals_map_to_409_with_the_refusal_code(app: Any) -> None:
    assert _request(app, reviewer="dave").json()["detail"]["error_type"] == "reviewer_role_required"
    assert _request(app, reviewer="alice").json()["detail"]["error_type"] == "reviewer_is_author"
    assert _request(app).status_code == 201
    second = _request(app, reviewer="carol")
    assert second.status_code == 409 and second.json()["detail"]["error_type"] == "open_review_request_exists"


def test_request_with_an_unknown_state_is_404(app: Any) -> None:
    _as(app, "alice")
    response = app.post(f"/api/sessions/{SESSION}/reviews", json={"state_id": "33333333-3333-4333-8333-333333333333", "reviewer_identity_id": None, "note": None})
    assert response.status_code == 404 and response.json()["detail"]["error_type"] == "state_not_in_session"


def test_request_note_longer_than_the_bound_is_422_at_the_boundary(app: Any) -> None:
    assert _request(app, note="a" * 4097).status_code == 422


# ── inbox ────────────────────────────────────────────────────────────────


def test_inbox_shows_named_and_unaddressed_open_requests_to_reviewers_only(app: Any) -> None:
    request_id = _request(app, reviewer=None).json()["request_id"]
    _as(app, "bob")
    assert [r["request_id"] for r in app.get("/api/reviews/inbox").json()["requests"]] == [request_id]
    _as(app, "carol")
    assert [r["request_id"] for r in app.get("/api/reviews/inbox").json()["requests"]] == [request_id]
    _as(app, "dave")
    assert app.get("/api/reviews/inbox").json() == {"requests": []}


# ── attest ───────────────────────────────────────────────────────────────


def test_attest_computes_the_digest_server_side_snapshots_the_author_and_closes_the_request(app: Any) -> None:
    request_id = _request(app).json()["request_id"]
    _as(app, "bob")
    response = app.post(f"/api/reviews/{request_id}/attest", json={"verdict": "signed_off", "note": None})
    assert response.status_code == 201, response.text
    body = response.json()
    expected = "sha256:" + hashlib.sha256(
        canonical_json(
            {
                "version": 1,
                "sources": _STATE_CONTENT["sources"],
                "source": None,
                "nodes": _STATE_CONTENT["nodes"],
                "edges": [],
                "outputs": [],
                "metadata": _STATE_CONTENT["metadata_"],
            }
        ).encode("utf-8")
    ).hexdigest()
    assert (body["payload_digest"], body["reviewer_identity_id"], body["author_identity_id"], body["verdict"]) == (expected, "bob", "alice", "signed_off")
    engine = app.app.state.phase3_engine
    with engine.connect() as conn:
        row = conn.execute(select(review_attestations_table)).one()
    assert row.payload_digest == expected and row.author_identity_id == "alice"
    assert app.get("/api/reviews/inbox").json() == {"requests": []}
    call = app.app.state.auth_audit_recorder.only("record_review_attested")
    assert call.request_bound is True and call.kwargs["attestation_id"] == body["attestation_id"] and call.kwargs["payload_digest"] == expected


def test_review_payload_digest_is_over_the_state_content_only() -> None:
    """Mutation: the same content under a different state id or time yields the same digest; changed content does not."""
    from types import SimpleNamespace

    def record(**overrides: Any) -> Any:
        values = {"id": "x", "session_id": "y", "version": 1, "sources": None, "source": None, "nodes": [], "edges": [], "outputs": [], "metadata_": {"name": "n", "description": ""}, "created_at": datetime.now(UTC)}
        values.update(overrides)
        return SimpleNamespace(**values)

    baseline = review_payload_digest(record())
    assert baseline.startswith("sha256:") and len(baseline) == len("sha256:") + 64
    assert review_payload_digest(record(id="other", session_id="other")) == baseline
    assert review_payload_digest(record(nodes=[{"id": "n1"}])) != baseline


def test_attest_refusals_and_not_found(app: Any) -> None:
    request_id = _request(app).json()["request_id"]
    _as(app, "alice")
    own = app.post(f"/api/reviews/{request_id}/attest", json={"verdict": "signed_off", "note": None})
    assert own.status_code == 409 and own.json()["detail"]["error_type"] == "reviewer_is_author"
    _as(app, "dave")
    unroled = app.post(f"/api/reviews/{request_id}/attest", json={"verdict": "signed_off", "note": None})
    assert unroled.status_code == 409 and unroled.json()["detail"]["error_type"] == "reviewer_role_required"
    _as(app, "bob")
    blank = app.post(f"/api/reviews/{request_id}/attest", json={"verdict": "changes_requested", "note": " "})
    assert blank.status_code == 409 and blank.json()["detail"]["error_type"] == "changes_requested_needs_note"
    assert app.post(f"/api/reviews/{request_id}/attest", json={"verdict": "approved", "note": None}).status_code == 422
    missing = app.post("/api/reviews/no-such-request/attest", json={"verdict": "signed_off", "note": None})
    assert missing.status_code == 404 and missing.json()["detail"]["error_type"] == "review_request_not_found"
    assert app.app.state.auth_audit_recorder.calls[-1].method == "record_review_requested"


def test_two_reviewers_both_attest_the_same_request(app: Any) -> None:
    request_id = _request(app, reviewer=None).json()["request_id"]
    _as(app, "bob")
    assert app.post(f"/api/reviews/{request_id}/attest", json={"verdict": "signed_off", "note": None}).status_code == 201
    _as(app, "carol")
    assert app.post(f"/api/reviews/{request_id}/attest", json={"verdict": "changes_requested", "note": "one more pass"}).status_code == 201
    assert [c.method for c in app.app.state.auth_audit_recorder.calls] == ["record_review_requested", "record_review_attested", "record_review_attested"]


# ── cancel ───────────────────────────────────────────────────────────────


def test_cancel_is_requester_only(app: Any) -> None:
    request_id = _request(app).json()["request_id"]
    _as(app, "bob")
    assert app.post(f"/api/reviews/{request_id}/cancel").status_code == 404
    _as(app, "alice")
    cancelled = app.post(f"/api/reviews/{request_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["open"] is False and cancelled.json()["cancelled_at"] is not None
    again = app.post(f"/api/reviews/{request_id}/cancel")
    assert again.status_code == 409 and again.json()["detail"]["error_type"] == "review_request_already_closed"
    call = app.app.state.auth_audit_recorder.only("record_review_request_cancelled")
    assert call.kwargs["request_id"] == request_id and call.request_bound is True
```

- [ ] **Step 11: Run the route tests to verify they fail on the missing module.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_review_routes.py -n 0 > /tmp/i4-routes-red.log 2>&1; echo exit=$?
```

Expected: `exit=2`,
`ModuleNotFoundError: No module named 'elspeth.web.sessions.routes.workflow.reviews'`.

- [ ] **Step 12: Write the routes, register them, and build the authority on app state.**

Create `src/elspeth/web/sessions/routes/workflow/reviews.py`:

```python
"""Review requests and reviewer attestations -- the I4 half of the workflow mailbox.

``POST /api/sessions/{session_id}/reviews`` (requester = owner),
``GET /api/reviews/inbox`` (reviewers), ``POST /api/reviews/{request_id}/attest``
(reviewers), ``POST /api/reviews/{request_id}/cancel`` (requester).

The authority is the arbiter: role, activity, author-is-not-reviewer,
one-open-request and the note rules are enforced inside
``RepositoryReviewAuthority``'s transaction. These routes parse the body,
prove session ownership for the request route, compute the digest, hand
the authority a record callback and translate its closed refusal set. They
add no rule of their own except the governance switch: every mutation
refuses with ``workflow_governance_off`` unless ``WebSettings.workflow_governance``
is ``"on"`` (I8), and the inbox is empty while it is off.

The digest is COMPUTED HERE from the state record, never accepted from the
client: an attestation is a statement about bytes the reviewer saw.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.hashing import canonical_json
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.audit import MAX_AUTH_AUDIT_TEXT_LENGTH, AuthAuditWriter
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.review_authority import (
    MAX_REVIEW_NOTE_BYTES,
    RepositoryReviewAuthority,
    ReviewAttestationRecord,
    ReviewAuthorityRefusal,
    ReviewRequestNotFound,
    ReviewRequestRecord,
    ReviewVerdict,
    SessionNotOwnedByRequester,
    StateNotInSession,
)
from elspeth.web.sessions.protocol import CompositionStateRecord, SessionServiceProtocol
from elspeth.web.sessions.routes._helpers import _verify_session_ownership

# ── wire shapes ──────────────────────────────────────────────────────────


class _StrictModel(BaseModel):
    """Tier 1 base: no coercion, no extras."""

    model_config = ConfigDict(strict=True, extra="forbid")


class RequestReviewBody(_StrictModel):
    state_id: str = Field(min_length=1, max_length=64)
    reviewer_identity_id: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)
    # Characters here; the authority bounds BYTES (MAX_REVIEW_NOTE_BYTES).
    note: str | None = Field(default=None, max_length=MAX_REVIEW_NOTE_BYTES)


class AttestBody(_StrictModel):
    verdict: ReviewVerdict
    note: str | None = Field(default=None, max_length=MAX_REVIEW_NOTE_BYTES)


class ReviewRequestView(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    session_id: str
    state_id: str
    requested_by_identity_id: str
    reviewer_identity_id: str | None
    requested_at: AwareDatetime
    cancelled_at: AwareDatetime | None
    request_note: str | None
    open: bool


class ReviewAttestationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    attestation_id: str
    session_id: str
    state_id: str
    payload_digest: str
    reviewer_identity_id: str
    author_identity_id: str
    attested_at: AwareDatetime
    verdict: ReviewVerdict
    note: str | None


class ReviewInboxResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    requests: list[ReviewRequestView]


# ── helpers ──────────────────────────────────────────────────────────────


def review_payload_digest(record: CompositionStateRecord) -> str:
    """``sha256:<hex>`` over the state's CONTENT — ids, times and provenance excluded.

    Two states with the same composition carry the same digest; that is the
    property an attestation needs ("I looked at these bytes"), and it is what
    lets I7's inspect view show which attestations apply to what it renders.
    """
    content = {
        "version": record.version,
        "sources": record.sources,
        "source": record.source,
        "nodes": record.nodes,
        "edges": record.edges,
        "outputs": record.outputs,
        "metadata": record.metadata_,
    }
    return "sha256:" + hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def _authority(request: Request) -> RepositoryReviewAuthority:
    authority: RepositoryReviewAuthority = request.app.state.review_authority
    return authority


def _identity_authority(request: Request) -> RepositoryIdentityAuthority:
    authority: RepositoryIdentityAuthority = request.app.state.identity_authority
    return authority


def _recorder(request: Request) -> AuthAuditWriter:
    recorder: AuthAuditWriter = request.app.state.auth_audit_recorder
    return recorder


def _settings(request: Request) -> WebSettings:
    settings: WebSettings = request.app.state.settings
    return settings


def _provider(request: Request) -> AuthProviderType:
    return _settings(request).auth_provider


def _governance_on(request: Request) -> bool:
    return _settings(request).workflow_governance == "on"


def _require_governance(request: Request) -> None:
    if not _governance_on(request):
        raise HTTPException(
            status_code=409,
            detail={"error_type": "workflow_governance_off", "detail": "workflow governance is off on this deployment (set ELSPETH_WEB__WORKFLOW_GOVERNANCE=on)"},
        )


def _refusal_code(exc: ReviewAuthorityRefusal) -> str:
    """``ReviewerRoleRequired`` -> ``reviewer_role_required``: the closed code a client can switch on."""
    name = type(exc).__name__
    return "".join(f"_{char.lower()}" if char.isupper() else char for char in name).lstrip("_")


_NOT_FOUND_REFUSALS: frozenset[type[ReviewAuthorityRefusal]] = frozenset({ReviewRequestNotFound, SessionNotOwnedByRequester, StateNotInSession})


def _refused(exc: ReviewAuthorityRefusal) -> HTTPException:
    """Exact types, not ``isinstance``: a new refusal class lands in the 409 arm by default."""
    status = 404 if type(exc) in _NOT_FOUND_REFUSALS else 409
    return HTTPException(status_code=status, detail={"error_type": _refusal_code(exc), "detail": str(exc)})


def _uncacheable(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _request_view(record: ReviewRequestRecord) -> ReviewRequestView:
    return ReviewRequestView(
        request_id=record.request_id,
        session_id=record.session_id,
        state_id=record.state_id,
        requested_by_identity_id=record.requested_by_identity_id,
        reviewer_identity_id=record.reviewer_identity_id,
        requested_at=record.requested_at,
        cancelled_at=record.cancelled_at,
        request_note=record.request_note,
        open=record.open,
    )


def _attestation_view(record: ReviewAttestationRecord) -> ReviewAttestationView:
    return ReviewAttestationView(
        attestation_id=record.attestation_id,
        session_id=record.session_id,
        state_id=record.state_id,
        payload_digest=record.payload_digest,
        reviewer_identity_id=record.reviewer_identity_id,
        author_identity_id=record.author_identity_id,
        attested_at=record.attested_at,
        verdict=record.verdict,
        note=record.note,
    )


# ── router ───────────────────────────────────────────────────────────────


def create_reviews_router() -> APIRouter:
    router = APIRouter(tags=["workflow-reviews"])

    @router.post("/api/sessions/{session_id}/reviews", response_model=ReviewRequestView, status_code=201)
    async def request_review(
        session_id: UUID,
        request: Request,
        response: Response,
        body: RequestReviewBody,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ReviewRequestView:
        _require_governance(request)
        # 404 on a session the caller does not own (IDOR rule, _helpers.py:2527); the authority re-proves ownership in its transaction.
        session = await _verify_session_ownership(session_id, user, request)
        recorder = _recorder(request)
        provider = _provider(request)

        def record(created: ReviewRequestRecord) -> None:
            recorder.record_review_requested(
                request,
                provider=provider,
                request_id=created.request_id,
                session_id=created.session_id,
                state_id=created.state_id,
                requested_by_identity_id=created.requested_by_identity_id,
                reviewer_identity_id=created.reviewer_identity_id,
                note=created.request_note,
            )

        try:
            created = await run_sync_in_worker(
                _authority(request).request,
                session_id=str(session.id),
                state_id=body.state_id,
                requested_by=user.user_id,
                reviewer=body.reviewer_identity_id,
                note=body.note,
                record=record,
            )
        except ReviewAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _request_view(created)

    @router.get("/api/reviews/inbox", response_model=ReviewInboxResponse)
    async def review_inbox(
        request: Request,
        response: Response,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ReviewInboxResponse:
        _uncacheable(response)
        if not _governance_on(request):
            return ReviewInboxResponse(requests=[])
        # Unaddressed rows are "any active reviewer" (spec :1419): only a live reviewer sees the inbox at all.
        if not await run_sync_in_worker(_identity_authority(request).holds_active_role, identity_id=user.user_id, role="reviewer"):
            return ReviewInboxResponse(requests=[])
        rows = await run_sync_in_worker(_authority(request).open_for, reviewer=user.user_id)
        return ReviewInboxResponse(requests=[_request_view(row) for row in rows])

    @router.post("/api/reviews/{request_id}/attest", response_model=ReviewAttestationView, status_code=201)
    async def attest_review(
        request_id: str,
        request: Request,
        response: Response,
        body: AttestBody,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ReviewAttestationView:
        _require_governance(request)
        authority = _authority(request)
        pointer = await run_sync_in_worker(authority.read_request, request_id=request_id)
        if pointer is None:
            raise _refused(ReviewRequestNotFound(f"review request {request_id} not found"))
        # The request row is only a POINTER to the pair; its open/closed state
        # refuses nothing (a ledger). The digest comes from the state itself.
        service: SessionServiceProtocol = request.app.state.session_service
        state = await service.get_state(UUID(pointer.state_id))
        digest = review_payload_digest(state)
        recorder = _recorder(request)
        provider = _provider(request)

        def record(attested: ReviewAttestationRecord) -> None:
            recorder.record_review_attested(
                request,
                provider=provider,
                attestation_id=attested.attestation_id,
                session_id=attested.session_id,
                state_id=attested.state_id,
                payload_digest=attested.payload_digest,
                reviewer_identity_id=attested.reviewer_identity_id,
                author_identity_id=attested.author_identity_id,
                verdict=attested.verdict,
                note=attested.note,
            )

        try:
            attested = await run_sync_in_worker(
                authority.attest,
                session_id=pointer.session_id,
                state_id=pointer.state_id,
                payload_digest=digest,
                reviewer=user.user_id,
                verdict=body.verdict,
                note=body.note,
                record=record,
            )
        except ReviewAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _attestation_view(attested)

    @router.post("/api/reviews/{request_id}/cancel", response_model=ReviewRequestView)
    async def cancel_review(
        request_id: str,
        request: Request,
        response: Response,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ReviewRequestView:
        _require_governance(request)
        recorder = _recorder(request)
        provider = _provider(request)

        def record(cancelled: ReviewRequestRecord) -> None:
            recorder.record_review_request_cancelled(
                request,
                provider=provider,
                request_id=cancelled.request_id,
                session_id=cancelled.session_id,
                state_id=cancelled.state_id,
                requested_by_identity_id=cancelled.requested_by_identity_id,
            )

        try:
            cancelled = await run_sync_in_worker(_authority(request).cancel, request_id=request_id, requested_by=user.user_id, record=record)
        except ReviewAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _request_view(cancelled)

    return router
```

In `src/elspeth/web/app.py`, directly after
`app.state.identity_authority = identity_authority` (:1522), add:

```python
    # --- Review authority (I4) ---
    # Sole writer of review_requests / review_attestations; engine-owned
    # transactions, identity locks first, no session fence (a reviewer is
    # not the session's owner).
    app.state.review_authority = RepositoryReviewAuthority(session_engine)
```

with the import next to the identity-authority imports (:87 is
`from elspeth.web.coordination.identity_authority import (`; add
`from elspeth.web.coordination.review_authority import RepositoryReviewAuthority`
in alphabetical position among the `elspeth.web.coordination` imports).
Directly after I3's approvals-router registration (the line following
`app.include_router(create_identity_admin_router())` at :1782 on your
branch) add `app.include_router(create_reviews_router())`, importing
`from elspeth.web.sessions.routes.workflow.reviews import create_reviews_router`
beside I3's workflow import.

- [ ] **Step 13: Run the route tests, then the authority, audit and app-wiring suites together.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_review_routes.py tests/unit/web/coordination/test_review_authority.py tests/unit/web/auth/test_audit.py tests/unit/web/test_app.py tests/unit/web/test_readiness.py -n 0 > /tmp/i4-unit-green.log 2>&1; echo exit=$?
```

Expected: `exit=0`. `test_review_routes.py` reports `12 passed`. If
`test_attest_computes_the_digest_server_side_snapshots_the_author_and_closes_the_request` fails on
`sources`/`source`, the seeded row's JSON columns are read back through
`SessionServiceImpl._row_to_state_record` (service.py:9989) — compare the
record's `sources` against the seeded dict and adjust the seed, never the
digest function.

- [ ] **Step 14: Write and run the PostgreSQL proof (real `FOR UPDATE`).**

SQLite drops `FOR UPDATE`, so the one-open-request rule under two concurrent
requesters is only proven on PostgreSQL. Create
`tests/testcontainer/web/test_review_authority_postgres.py`:

```python
"""Real PostgreSQL identity locks serialise two review requests for one state."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Engine, insert, select
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.review_authority import OpenReviewRequestExists, RepositoryReviewAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composition_states_table, identity_roles_table, review_requests_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def review_engine(external_deployment_postgres_url: str) -> Iterator[Engine]:
    database = f"review_authority_{uuid4().hex}"
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


def _noop(_record: object) -> None:
    pass


def test_two_concurrent_requests_for_one_state_yield_exactly_one_open_row(review_engine: Engine) -> None:
    now = datetime.now(UTC)
    with review_engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol"):
            ensure_test_identity(conn, identity_id=identity_id)
        for role_id, identity_id in (("role-bob", "bob"), ("role-carol", "carol")):
            conn.execute(insert(identity_roles_table).values(role_id=role_id, identity_id=identity_id, role="reviewer", granted_at=now, granted_by_identity_id="alice"))
        conn.execute(insert(sessions_table).values(id="sess-1", user_id="alice", auth_provider_type="local", title="t", created_at=now, updated_at=now))
        conn.execute(insert(composition_states_table).values(id="state-1", session_id="sess-1", version=1, provenance="session_seed", created_at=now))
    authority = RepositoryReviewAuthority(review_engine)

    def attempt(reviewer: str) -> str:
        try:
            return authority.request(session_id="sess-1", state_id="state-1", requested_by="alice", reviewer=reviewer, note=None, record=_noop).request_id
        except OpenReviewRequestExists:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, ["bob", "carol"]))
    assert outcomes.count("refused") == 1, outcomes
    with review_engine.connect() as conn:
        assert len(conn.execute(select(review_requests_table)).all()) == 1
```

Run (needs Docker; serial, shares the acceptance container):

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/testcontainer/web/test_review_authority_postgres.py -m testcontainer -n 0 > /tmp/i4-postgres.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `1 passed`. Also run the neighbouring identity-lock proof
to confirm the shared container is healthy:
`pytest tests/testcontainer/web/test_chargeable_admission_postgres.py -m testcontainer -n 0 > /tmp/i4-postgres-neighbour.log 2>&1; echo exit=$?` → `exit=0`.

- [ ] **Step 15: Rename the copy — frontend, e2e page object, docs — and run vitest.**

`src/elspeth/web/frontend/src/components/composer/CompletionBar.tsx`:

- :6 ` *   * Save for review  → POSTs mark-ready-for-review, opens dialog with the` → ` *   * Share inspect link → POSTs mark-ready-for-review, opens dialog with the`
- :26 ` * The "Save for review" button follows the backend-owned` → ` * The "Share inspect link" button follows the backend-owned`
- :55-56:

```ts
const SAVE_FOR_REVIEW_DISABLED_TITLE =
  "Fix validation or completion blockers before sharing an inspect link.";
```

- :101 `        Save for review` → `        Share inspect link`

`CompletionBar.test.tsx`: :112 and :133 `"Save for review",` →
`"Share inspect link",`; :146, :155, :168, :191, :235, :251 replace
`Save for review` inside each `it` test title with `Share inspect link`.
`CompletionFlow.integration.test.tsx`: :6, :122, :149, :204 — same
replacement. `workspaceChrome.test.ts:174` comment — same replacement.
`tests/e2e/page-objects/composer-page.ts:124`:

```ts
    return this.page.getByRole("button", { name: "Share inspect link" });
```

Docs: `docs/release/composer-guide.md:35` → `| Finish in the right way | Share an inspect link, run the pipeline, or export YAML depending on the user's workflow. |`;
`:106` → `| Share inspect link | Composer marks the current composition as ready for another person to inspect, creates a signed share link, and shows the reviewer the same readiness and YAML evidence. |`.
`docs/guides/composer-training-one-hour.md:35` `Save for review` → `Share inspect link`; `:147` `` `Save for review` `` → `` `Share inspect link` ``; `:414` `` `Save for review` is stricter still `` → `` `Share inspect link` is stricter still ``; `:451` `**Slide 33 — Save for review.**` → `**Slide 33 — Share inspect link.**`; `:452` `` - `Save for review` → `Share for review` → a `Share URL` `` → `` - `Share inspect link` → `Share for review` → a `Share URL` ``.

Prove no pin was missed, then run vitest:

```bash
cd "$(git rev-parse --show-toplevel)" && grep -rn "Save for review" src/elspeth/web/frontend/src src/elspeth/web/frontend/tests docs/release/composer-guide.md docs/guides/composer-training-one-hour.md; echo grep_exit=$?
cd "$(git rev-parse --show-toplevel)"/src/elspeth/web/frontend && npm test -- --run > /tmp/i4-vitest.log 2>&1; echo exit=$?
```

Expected: the grep prints nothing and `grep_exit=1` (control: run the same
grep with `"Share inspect link"` — it must list CompletionBar.tsx:101 and
composer-page.ts:124 among others); vitest `exit=0`. The PRD
(docs/project-control/2026-09-03-elspeth-prd.md:390) describes the rename as
a decision and keeps the old name in quotes deliberately.

- [ ] **Step 16: Add the changelog line.**

In `CHANGELOG.md`, directly after the `**Authentication events in signed
exports.**` bullet (:35-38; after any I8/I3 bullets already inserted there),
add:

```markdown
- **Review requests and reviewer attestations.** A session owner can send a
  state for review (to a named reviewer or any active reviewer), cancel it,
  and reviewers see open requests in their inbox and attest `signed_off`,
  `changes_requested` (note required) or `withdrawn` against a server-computed
  content digest. Attestation is a ledger, not a control: nothing refuses on
  it. The "Save for review" gesture is renamed "Share inspect link".
```

Confirm the section with the operator before the first commit (see Global Constraints).

- [ ] **Step 17: Lint, type-check and run the scoped suites once more.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff check src/elspeth/web/coordination/review_authority.py src/elspeth/web/sessions/routes/workflow/reviews.py src/elspeth/web/auth/audit.py src/elspeth/web/app.py tests/unit/web/coordination/test_review_authority.py tests/unit/web/workflow tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py tests/testcontainer/web/test_review_authority_postgres.py tests/unit/architecture/test_session_db_mutation_authority.py > /tmp/i4-ruff.log 2>&1; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && mypy src/elspeth/web/coordination/review_authority.py src/elspeth/web/sessions/routes/workflow/reviews.py src/elspeth/web/auth/audit.py > /tmp/i4-mypy.log 2>&1; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow tests/unit/web/coordination/test_review_authority.py tests/unit/web/auth tests/unit/architecture/test_session_db_mutation_authority.py tests/unit/web/test_app.py -n 0 > /tmp/i4-final.log 2>&1; echo exit=$?
```

Expected: all three `exit=0`; the pytest summary has no `failed` and no
`xfailed`.

- [ ] **Step 18: Commit by file pathspec.**

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N src/elspeth/web/coordination/review_authority.py src/elspeth/web/sessions/routes/workflow/reviews.py tests/unit/web/coordination/test_review_authority.py tests/unit/web/workflow/test_review_routes.py tests/testcontainer/web/test_review_authority_postgres.py; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && scripts/branch-safety-check.sh --intent commit
cd "$(git rev-parse --show-toplevel)" && git commit -m "feat(identity): review requests and reviewer attestations" -- src/elspeth/web/coordination/review_authority.py src/elspeth/web/sessions/routes/workflow/reviews.py src/elspeth/web/app.py src/elspeth/web/auth/audit.py tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/architecture/test_session_db_mutation_authority.py tests/unit/web/coordination/test_review_authority.py tests/unit/web/workflow/test_review_routes.py tests/testcontainer/web/test_review_authority_postgres.py src/elspeth/web/frontend/src/components/composer/CompletionBar.tsx src/elspeth/web/frontend/src/components/composer/CompletionBar.test.tsx src/elspeth/web/frontend/src/components/composer/CompletionFlow.integration.test.tsx src/elspeth/web/frontend/src/components/workspace/workspaceChrome.test.ts src/elspeth/web/frontend/tests/e2e/page-objects/composer-page.ts docs/release/composer-guide.md docs/guides/composer-training-one-hour.md CHANGELOG.md
```

`git add -N` records the five new files as intent-to-add so the pathspec
commit can find them (without it `git commit -- <new file>` aborts with
`pathspec ... did not match any file(s) known to git`); expected `exit=0`.
Check `git show --stat HEAD` lists exactly those 18 files. `web/app.py`,
`test_identity_admin_routes.py` and `CHANGELOG.md` are also touched by I3/I5:
the second lane to land rebases; every collision is an adjacent-line append.
