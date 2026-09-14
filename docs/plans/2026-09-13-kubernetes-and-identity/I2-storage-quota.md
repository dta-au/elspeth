### Task I2: Storage quota R13 at every byte-admitting site

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: I1. Runs before: I3. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered after I1 (I0 → I8 → I1 → I2 → I3). I2 consumes I1's `QuotaExceeded`, `ActiveQuotaPolicies`, `QuotaPolicyRow`, the policy lookup behind `RepositoryQuotaAuthority.active_policy`, `AuthAuditRecorder.record_quota_exceeded` and the `fenced_session` / `pg_fenced` fixtures. I8's `workflow_governance` switch is not read: R13 enforces whenever an active storage policy row exists, the posture I1 gives R14. Spec: R13 (`docs/specs/2026-09-02-pluggable-sso-design.md:1127-1156`), D18 (:77), D24 (:81), the `quota_exceeded` metadata rule (:834-835) and §Testing → Workflow governance (:1283-1298: a fire and a mutation-derivation test per refusal, and a storage refusal at each R13 site asserting the row's dimension, cap, ceiling and usage).

Every `path:line` below was measured on HEAD 072141b75. Six files are also edited by I1: `src/elspeth/web/coordination/quota_authority.py` (created by I1), `src/elspeth/web/sessions/service.py`, `src/elspeth/web/app.py`, `tests/unit/architecture/test_session_db_mutation_authority.py`, `tests/unit/web/auth/test_audit.py` and `tests/testcontainer/web/test_quota_authority_postgres.py`. Their line numbers are those of the tree after I1's commit and are labelled "after I1". Where a file gets several hunks, each hunk's quoted text is the Edit anchor.

This block was executed end to end in a scratch export of HEAD with I0's two source edits and all of I1 applied. Every expected failure, pass count, AST digest, fingerprint and re-pin below is the measured output of that run. The export did not carry I0's epoch-pin test edits, so its only failures outside this task were I0's pins: `tests/integration/web/composer/guided/test_schema9_epoch.py`, `tests/unit/contracts/test_web_blob_fencing.py:3196`, `tests/unit/web/sessions/test_interpretation_events_table.py`, `tests/unit/web/sessions/test_blob_inline_resolutions_schema.py`, `tests/unit/web/sessions/test_proposal_blob_effect_receipts_schema.py` and `tests/unit/web/sessions/test_schema.py`. The counts below assume I0 has landed.

Decisions this task owns:

1. **The spec's four sites are five entry points and seven admission points, plus replacement.** The per-session check exists in two families, and R13 rides directly behind every member. An earlier working call graph routed upload and run-output finalize through `_reserve_pending_blob` and admitted only after the three `_enforce_session_blob_quota` calls in `_reserve_pending_blob`, `persist_inline_custody_blob_on_connection` and `copy_blobs_for_fork`; measurement corrects it as follows.
   - **Upload.** `create_blob` (`blobs/service.py:2644`; the multipart route `blobs/routes.py:245` and the inline route `:308`) → `_persist_fenced_blob_record` (:2715) → facet `reserve_blob` (`coordination/repository.py:2575`, per-session check :2658). Composer inline custody `reserve_inline_custody` (:2833) and the composer `create_blob` tool (`composer/tools/blobs.py:1342`) reach the same facet, because `_persist_blob_content` delegates to `_persist_fenced_blob_record` whenever a session-operation context is given (:1616-1633). None of them reaches `_reserve_pending_blob`.
   - **Guided-full inline custody.** `SessionServiceImpl` (after I1 :11712) → `finalize_pipeline_custody_on_connection` (`composer/pipeline_custody.py:278`) → `persist_inline_custody_blob_on_connection` (`blobs/service.py:2018`, check :2070).
   - **Run-output finalize.** `finalize_run_output_blobs` (:3554) → `_finalize_one_output_blob` (:3687) → `_mark_run_output_ready` (:3803) → facet `mark_run_output_blob_ready` (`repository.py:3580`, check :3612). The facet `finalize_pending_output_blob` (:2497, check :2544) has no production caller on HEAD; the only hit is a docstring at `composer/tools/blobs.py:404`. It is admitted as well, so the facet protocol keeps no unguarded byte path, and it is not deleted.
   - **Fork.** `copy_blobs_for_fork` (:3945): the pre-copy check in `_verify_plan_and_quota` (:4009) refuses before the loop writes any child row, and each copy's reservation (`_persist_copy` :4058 → `_reserve_pending_blob` :1371, check :1395) admits again.
   - **Blob replacement.** A replacement grows a live row's `size_bytes`: facets `prepare_blob_replacement` (:2143, check :2215) and `commit_blob_replacement` (:2329, check :2367), driven from `blobs/replacement.py:277` and `:312`. This is a path the spec's list of four omits; it gets its own fire and mutation tests.
   - **Not admitted.** `reserve_pending_output_blob` (:2456) inserts a zero-byte row and refuses any other size, so it adds no bytes.
2. **One admission function, counting net bytes, taking no lock.** `admit_storage_bytes_on_connection` (in I1's `quota_authority.py`) works as follows.
   - **Owner and usage.** It reads the session's owner and provider from `sessions` (`models.py:433-434`), then sums `blobs.size_bytes` over `blobs JOIN sessions` for that owner. Archived sessions and `pending`/`error` rows count (spec :1143-1145).
   - **The bound.** It refuses when `usage + additional_bytes` exceeds the lower of the identity row's and the container row's `storage_bytes`. That is the same `>` the per-session bound uses.
   - **Net bytes.** `additional_bytes` is the net increase: a replacement passes new size minus old, and run-output finalize passes the ready size minus the pending row's current size. A call adding no net bytes is exempt and measures nothing, which is the fork's `missing_bytes == 0` replay exemption (spec :1141-1143).
   - **No policy.** With no active policy row there is no bound (D18: the storage regime is optional).
   - **No lock.** Policy rows are read without `FOR UPDATE`. I1's `active_policy` locks both rows for R14; doing that here would serialise every blob write in the deployment on the one container row, the serialisation D24 rejects. I1's lookup is therefore split into `_active_policy_rows(connection, *, identity_id, for_update)`, and `active_policy` keeps `for_update=True` and its behaviour.
   - **Accounting failure.** A `SQLAlchemyError` from the policy read or the usage SUM refuses with `StorageAccountingUnavailableError` and records nothing, because nothing was measured (spec :1131).
3. **The refusal is a subclass of the per-session error.**
   - **Why a subclass.** `IdentityStorageQuotaExceededError(BlobQuotaExceededError)` lives beside its parent in `contracts/blobs.py` (:339). Eight production handlers catch `BlobQuotaExceededError`: `blobs/service.py:3734`, `composer/tool_batch.py:336`, `composer/tools/blobs.py:1364` and `:1590`, `sessions/routes/composer/guided_chat_atomic.py:1763`, `sessions/routes/composer/guided_plan.py:272`, `sessions/routes/sessions.py:1092` and `execution/service.py:3948`. A sibling type would escape `_finalize_one_output_blob` and abort a whole finalize batch.
   - **Fields.** The subclass sets `current_bytes` to the identity's usage and `limit_bytes` to the bound in force, so every inherited field stays true. It adds `identity_id`, `cap`, `ceiling`, `usage` and `additional_bytes`. `BlobQuotaExceededError` itself is unchanged (spec: "the existing per-session error keeps its shape").
   - **HTTP.** The two upload routes answer 413 with `detail = {"error_type": "storage_quota_exceeded", "detail": <message>, "dimension": "storage", "cap": <int or null>, "ceiling": <int or null>, "usage": <int>}`, a dict detail the frontend client already unwraps (`frontend/src/api/client.ts:261`). They answer 503 for `StorageAccountingUnavailableError`. The existing per-session `detail=str(exc)` arm stays last.
   - **Composer wording.** The composer `create_blob` tool message (`composer/tools/blobs.py:1364-1368`) still says "Session blob quota exceeded", with correct byte counts; its wording is an open question for the operator.
4. **The audit seam.** `auth_events` is a Landscape table and nothing on the sessions side opens it.
   - **Record before raise.** `admit_storage_bytes_on_connection` takes a required `record: Callable[[QuotaExceeded], None]` and calls it with `dimension="storage"` before raising, inside the caller's transaction. Under R4, an audit failure is therefore the error the caller sees.
   - **Fenced sites.** The session-operation authorities (`_SessionOperationAuthorityRepository`, `SQLiteLocalSessionOperationAuthority`, `PostgresSessionOperationRepository`) gain a keyword `quota_exceeded_recorder`. `mutate` hands it to `_RepositoryMutationTransaction` and `_RepositoryMutationState`, and the five facets pass `state._quota_exceeded_recorder`. That covers the upload routes, composer tools, pipeline planner, tool batch, replacement driver and run finalize without changing a facet-protocol signature.
   - **Raw-connection sites.** These take the recorder explicitly:
     - `BlobServiceImpl` gains `quota_exceeded_recorder`, used for the fork pre-check, the per-copy check and the authority it builds by default.
     - `_persist_blob_content`, `persist_inline_custody_blob_on_connection` and `finalize_pipeline_custody_on_connection` each gain the keyword.
     - The guided-full settlement passes I1's `self._quota_exceeded_recorder`.
   - **App wiring.** `web/app.py` passes `audit_recorder.record_quota_exceeded` (bound at `app.py:1536`, before the authorities at :1580/:1582 and the blob service at :1593) to both.
   - **Fail closed.** Every default is `refuse_unrecorded_quota_exceeded`, which raises `AuditIntegrityError`, so an unwired refusal fails closed.
   - **No `web/auth/audit.py` change.** I1's `AuthAuditRecorder.record_quota_exceeded` (after I1 :1186) already writes `failure_category=f"quota_exceeded_{dimension}"` and the metadata keys. Step 10 pins the storage row on the Landscape engine.
5. **"Before any byte reaches disk."** Upload, `reserve_inline_custody`, fork and replacement refuse in the reservation transaction, before `_atomic_write_blob` or the staging write; Step 6 asserts that no file exists under the session directory. Two sites cannot, by construction:
   - The guided-full settlement's bytes are staged before its cohort transaction (`staged_pipeline_custody`, which discards the stage when the cohort raises).
   - Run outputs are written by the run before finalize. There the refusal marks the row `error` and removes the file through the existing `_mark_output_blob_error_and_remove_bytes`.
6. **Churn is re-pinned, never avoided.** No sessions table gains a writer, so `_TABLE_POLICIES` and `_NAMED_AUTHORITY_SYMBOLS` are untouched. The new lines shift 114 reviewed rows. Three of them also change fingerprint, because their enclosing function body changed: `_reserve_pending_blob`, `_persist_blob_content` and `copy_blobs_for_fork._verify_plan_and_quota`. Step 16 tables every move, and the gate's XFAIL counts return to I1's baseline (67 / 0 / 16 / 44 / 7). The blob route gate pins three AST digests that the new except arms change; Step 14 re-pins them.

**Files:**
- Modify: `src/elspeth/contracts/blobs.py:348-354` (the tail of `BlobQuotaExceededError` and the `class BlobStateError` line after it)
- Modify: `src/elspeth/web/blobs/protocol.py:42-44` (re-exports)
- Modify: `src/elspeth/web/coordination/quota_authority.py` after I1 `:32-43` (imports) and `:268-292` (`RepositoryQuotaAuthority.active_policy`, the last method in the file)
- Modify: `src/elspeth/web/coordination/repository.py:73-76, :499-512, :2215-2218, :2367-2370, :2544-2552, :2658-2666, :3612-3620, :3753-3762, :4495-4496, :5139-5144, :5620-5623`
- Modify: `src/elspeth/web/coordination/sqlite_authority.py:11, :14-15, :27-30`
- Modify: `src/elspeth/web/blobs/service.py:67-68, :1310-1318, :1327-1335, :1380-1382, :1395-1401, :1513-1515, :1619-1624, :1646-1649, :2022-2024, :2070-2076, :2431-2436, :2444, :2448, :4009-4014, :4076-4079`
- Modify: `src/elspeth/web/composer/pipeline_custody.py:13, :55, :283-285, :305-310`
- Modify: `src/elspeth/web/sessions/service.py` after I1 `:4450, :4452, :11715-11716`
- Modify: `src/elspeth/web/app.py:1580, :1582, :1596-1598`
- Modify: `src/elspeth/web/blobs/routes.py:37-39, :254-255, :317-318`
- Create: `tests/unit/web/coordination/test_storage_quota_authority.py`
- Create: `tests/unit/web/blobs/test_storage_quota_sites.py`
- Modify: `tests/unit/web/test_app.py:411-415` (end of `TestCreateApp.test_settings_stored_on_app_state`)
- Modify: `tests/unit/web/auth/test_audit.py` after I1 `:1091-1093` (end of I1's `test_quota_exceeded_row_carries_dimension_cap_ceiling_and_usage`, which starts at :1053)
- Modify: `tests/unit/contracts/test_web_blob_fencing.py:315, :325-327`
- Modify: `tests/unit/architecture/test_session_db_mutation_authority.py` after I1: the 114 `WriterIdentity` rows tabled in Step 16
- Modify: `tests/testcontainer/web/test_quota_authority_postgres.py` after I1 `:10-20` (imports) and append after its last line (:71)
- Test: the two created modules and the five modified test modules above

**Interfaces:**
- Consumes:
  - I0: `SESSION_SCHEMA_EPOCH == 57`; no storage column changes. `quota_policies.storage_bytes` (`sessions/models.py:3849`, NOT NULL), `quota_policies.revoked_at` (:3863), `sessions.user_id` / `sessions.auth_provider_type` (:433-434), `sessions.archived_at` (:515), `blobs.size_bytes` (:2713, NOT NULL).
  - I8: nothing is read.
  - I1:
    - `QuotaExceeded(identity_id: str, provider: AuthProviderType, operation: str, dimension: Literal["tokens", "storage"], cap: int | None, ceiling: int | None, usage: int, identity_policy_id: str | None, container_policy_id: str | None)` (`quota_authority.py` after I1 :92).
    - `ActiveQuotaPolicies(identity: QuotaPolicyRow | None, container: QuotaPolicyRow | None)` (:83) and `QuotaPolicyRow(policy_id: str, tokens_per_day: int, storage_bytes: int)` (:75).
    - `RepositoryQuotaAuthority` (:217) and its `active_policy(connection_token: str, *, identity_id: str) -> ActiveQuotaPolicies` (:269).
    - `AuthAuditRecorder.record_quota_exceeded(self, outcome: QuotaExceeded) -> None` (`web/auth/audit.py` after I1 :1186; Protocol member :258).
    - Fixtures and helpers: `fenced_session` (`tests/unit/web/coordination/conftest.py:23`) yielding `FencedSession(engine, connection_token, identity_id, session_id)` (`tests/helpers/fenced_session.py:26`), `pg_fenced` (`tests/testcontainer/web/conftest.py:226`), and `_durable_recorder` / `_durable_rows` / `_metadata` (`tests/unit/web/auth/test_audit.py:629`, `:634`, `:644`).
    - I1's test module `tests/unit/web/coordination/test_quota_authority.py`, re-run as a regression for the `active_policy` split.
  - HEAD:
    - Source: `BlobQuotaExceededError` (`contracts/blobs.py:339`), `_guard_frozen_attr` (:236), `_enforce_session_blob_quota` (`blobs/service.py:1306`), `_resolve_mutation_connection` (`coordination/mutation_connection_registry.py:22`).
    - Blob test helpers in `tests/unit/web/blobs/test_service.py`: `_seed_active_run` (:140), `_RUN_SOURCE` (:207), `_pending_output_record` (:215), `reserve_output_blob` (:242), `_custody_request` (:1713), and `TestCopyBlobsForFork._plan` (:3193) / `._authorize_copy` (:3227), reached as attributes of the module so pytest does not collect the class twice.
    - Shared test helpers: `seed_live_operation_context` (`tests/helpers/session_fences.py:285`), `seed_live_compose_context` (:344), `ensure_test_identity` (`tests/fixtures/identities.py:10`), `DualFencedSessionServiceHarness` (`tests/unit/web/sessions/guided_test_authority.py:22`), `SyncASGITestClient` (`tests/unit/web/_sync_asgi_client.py:14`), and `_settings` (`tests/unit/web/test_app.py:187`).
- Produces:
  - `src/elspeth/contracts/blobs.py` (re-exported from `elspeth.web.blobs.protocol`):
    - `IdentityStorageQuotaExceededError(BlobQuotaExceededError)` with `__init__(self, session_id: str, *, identity_id: str, cap: int | None, ceiling: int | None, usage: int, additional_bytes: int)`. Frozen attributes: `session_id`, `current_bytes` (= usage), `limit_bytes` (= min of the non-None bounds), `identity_id`, `cap`, `ceiling`, `usage`, `additional_bytes`. Message: `Identity <identity_id> blob storage (<usage> bytes) plus <additional_bytes> bytes would exceed its storage quota (<limit> bytes)`.
    - `StorageAccountingUnavailableError(BlobError)` with `__init__(self, session_id: str)`. Message: `Storage accounting for session <session_id> is unavailable; the blob write is refused`.
  - `src/elspeth/web/coordination/quota_authority.py`:
    - `StorageAdmissionOperation = Literal["blob_create", "inline_custody", "run_output_finalize", "blob_replacement", "session_fork"]`
    - `StorageAdmission(identity_id: str, additional_bytes: int, usage: int | None, cap: int | None, ceiling: int | None)`, a frozen slotted dataclass.
    - `refuse_unrecorded_quota_exceeded(outcome: QuotaExceeded) -> None`, which raises `AuditIntegrityError("quota_exceeded refusal for identity <id> has no auth audit writer wired")`.
    - `admit_storage_bytes_on_connection(connection: Connection, *, session_id: str, additional_bytes: int, operation: StorageAdmissionOperation, record: Callable[[QuotaExceeded], None]) -> StorageAdmission`
    - `RepositoryQuotaAuthority.admit_storage_bytes(connection_token: str, *, session_id: str, additional_bytes: int, operation: StorageAdmissionOperation, record: Callable[[QuotaExceeded], None]) -> StorageAdmission`
  - Constructors: `SQLiteLocalSessionOperationAuthority(engine: Engine, *, quota_exceeded_recorder: Callable[[QuotaExceeded], None] = refuse_unrecorded_quota_exceeded)`; `PostgresSessionOperationRepository` and `_SessionOperationAuthorityRepository` take the same keyword. `BlobServiceImpl(engine, data_dir, max_storage_per_session=500 * 1024 * 1024, *, session_operation_authority=None, quota_exceeded_recorder=refuse_unrecorded_quota_exceeded)`. `persist_inline_custody_blob_on_connection`, `finalize_pipeline_custody_on_connection` and `_persist_blob_content` each gain the keyword `quota_exceeded_recorder` with the same default.
  - HTTP: `POST /api/sessions/{session_id}/blobs` and `POST /api/sessions/{session_id}/blobs/inline` answer an R13 refusal with 413 and `{"detail": {"error_type": "storage_quota_exceeded", "detail": str, "dimension": "storage", "cap": int | None, "ceiling": int | None, "usage": int}}` (I9 renders it), and an accounting failure with 503.
  - Landscape `auth_events` row per refusal: `event_type="quota_exceeded"`, `failure_category="quota_exceeded_storage"`, metadata `operation` ∈ `StorageAdmissionOperation`.

- [ ] **Step 1: Write the failing authority tests.** They drive `RepositoryQuotaAuthority.admit_storage_bytes` on I1's `fenced_session` token and observe the `record` callback; the Landscape row is pinned in Step 10.

Create `tests/unit/web/coordination/test_storage_quota_authority.py`:

```python
"""R13 storage admission at the authority: the identity's live blob total against its cap and the ceiling (Task I2).

Every refusal has a fire test and a mutation-derivation test: the authority row
(a policy's ``storage_bytes``, a ``blobs`` row, a session's owner) is changed,
never the guard. ``auth_events`` is a Landscape table, so these tests observe
the ``record`` callback the authority invokes; the Landscape row itself is
pinned in ``tests/unit/web/auth/test_audit.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, event, insert, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.fenced_session import FencedSession

from elspeth.contracts.blobs import IdentityStorageQuotaExceededError, StorageAccountingUnavailableError
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.coordination.quota_authority import (
    QuotaExceeded,
    RepositoryQuotaAuthority,
    StorageAdmission,
    refuse_unrecorded_quota_exceeded,
)
from elspeth.web.sessions.models import blobs_table, quota_policies_table, sessions_table

SET_AT = datetime(2026, 9, 1, tzinfo=UTC)


def _conn(fenced: FencedSession) -> Connection:
    return _resolve_mutation_connection(fenced.connection_token)


def _seed_storage_policies(conn: Connection, *, identity_id: str, identity_bytes: int | None, container_bytes: int | None) -> None:
    if identity_bytes is not None:
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="storage-identity",
                identity_id=identity_id,
                tokens_per_day=1000,
                storage_bytes=identity_bytes,
                set_by_actor="operator",
                set_by_identity_id=None,
                set_at=SET_AT,
            )
        )
    if container_bytes is not None:
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="storage-container",
                identity_id=None,
                tokens_per_day=5000,
                storage_bytes=container_bytes,
                set_by_actor="config",
                set_by_identity_id=None,
                set_at=SET_AT,
            )
        )


def _session(conn: Connection, *, user_id: str, archived: bool = False) -> str:
    session_id = str(uuid.uuid4())
    conn.execute(
        insert(sessions_table).values(
            id=session_id,
            user_id=user_id,
            auth_provider_type="local",
            title="another session",
            created_at=SET_AT,
            updated_at=SET_AT,
            archived_at=SET_AT if archived else None,
        )
    )
    return session_id


def _blob(conn: Connection, *, session_id: str, size_bytes: int, status: str = "ready") -> str:
    blob_id = str(uuid.uuid4())
    conn.execute(
        insert(blobs_table).values(
            id=blob_id,
            session_id=session_id,
            filename=f"{blob_id}.csv",
            mime_type="text/csv",
            size_bytes=size_bytes,
            content_hash="a" * 64 if status == "ready" else None,
            storage_path=f"/nonexistent/{blob_id}.csv",
            created_at=SET_AT,
            created_by="user",
            source_description=None,
            status=status,
        )
    )
    return blob_id


def _admit(fenced: FencedSession, additional_bytes: int, recorded: list[QuotaExceeded]) -> StorageAdmission:
    return RepositoryQuotaAuthority.admit_storage_bytes(
        fenced.connection_token,
        session_id=fenced.session_id,
        additional_bytes=additional_bytes,
        operation="blob_create",
        record=recorded.append,
    )


def _ninety_bytes_across_two_sessions(fenced: FencedSession) -> None:
    conn = _conn(fenced)
    _blob(conn, session_id=_session(conn, user_id=fenced.identity_id), size_bytes=60)
    _blob(conn, session_id=fenced.session_id, size_bytes=30)


def test_r13_refuses_when_the_identity_total_across_sessions_would_exceed_the_cap(fenced_session: FencedSession) -> None:
    """Fire test. sso-design.md:1127: refuse a path that would take the identity's live blob total over storage_bytes."""
    _seed_storage_policies(_conn(fenced_session), identity_id=fenced_session.identity_id, identity_bytes=100, container_bytes=1000)
    _ninety_bytes_across_two_sessions(fenced_session)
    recorded: list[QuotaExceeded] = []
    with pytest.raises(
        IdentityStorageQuotaExceededError,
        match=r"alice blob storage \(90 bytes\) plus 11 bytes would exceed its storage quota \(100 bytes\)",
    ) as caught:
        _admit(fenced_session, 11, recorded)
    assert (caught.value.identity_id, caught.value.cap, caught.value.ceiling, caught.value.usage, caught.value.additional_bytes) == (
        "alice",
        100,
        1000,
        90,
        11,
    )
    assert (caught.value.session_id, caught.value.current_bytes, caught.value.limit_bytes) == (fenced_session.session_id, 90, 100)
    assert recorded == [
        QuotaExceeded(
            identity_id="alice",
            provider="local",
            operation="blob_create",
            dimension="storage",
            cap=100,
            ceiling=1000,
            usage=90,
            identity_policy_id="storage-identity",
            container_policy_id="storage-container",
        )
    ]


@pytest.mark.parametrize(("storage_bytes", "refused"), [(102, False), (101, False), (100, True)])
def test_r13_derives_from_the_identity_policy_row_not_a_constant(fenced_session: FencedSession, storage_bytes: int, refused: bool) -> None:
    """Mutation-derivation: the same 90 + 11 bytes flips on the identity policy row's storage_bytes alone."""
    conn = _conn(fenced_session)
    _seed_storage_policies(conn, identity_id=fenced_session.identity_id, identity_bytes=100, container_bytes=1000)
    _ninety_bytes_across_two_sessions(fenced_session)
    conn.execute(
        update(quota_policies_table).where(quota_policies_table.c.policy_id == "storage-identity").values(storage_bytes=storage_bytes)
    )
    recorded: list[QuotaExceeded] = []
    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError, match=r"would exceed its storage quota \(100 bytes\)"):
            _admit(fenced_session, 11, recorded)
        assert [outcome.cap for outcome in recorded] == [storage_bytes]
    else:
        assert _admit(fenced_session, 11, recorded) == StorageAdmission(
            identity_id="alice", additional_bytes=11, usage=90, cap=storage_bytes, ceiling=1000
        )
        assert recorded == []


def test_r13_container_ceiling_binds_when_it_is_the_lower_bound(fenced_session: FencedSession) -> None:
    """Mutation-derivation for the ceiling: lower the container row below usage + additional and the identity cap no longer admits."""
    conn = _conn(fenced_session)
    _seed_storage_policies(conn, identity_id=fenced_session.identity_id, identity_bytes=1000, container_bytes=5000)
    _ninety_bytes_across_two_sessions(fenced_session)
    recorded: list[QuotaExceeded] = []
    assert _admit(fenced_session, 11, recorded).usage == 90
    conn.execute(update(quota_policies_table).where(quota_policies_table.c.policy_id == "storage-container").values(storage_bytes=100))
    with pytest.raises(IdentityStorageQuotaExceededError, match=r"would exceed its storage quota \(100 bytes\)") as caught:
        _admit(fenced_session, 11, recorded)
    assert (caught.value.cap, caught.value.ceiling, caught.value.limit_bytes) == (1000, 100, 100)
    assert [(outcome.cap, outcome.ceiling) for outcome in recorded] == [(1000, 100)]


def test_r13_counts_archived_sessions_and_pending_and_error_rows(fenced_session: FencedSession) -> None:
    """Spec :1143-1145: blobs in archived sessions count, and pending or error rows count, because they occupy disk."""
    conn = _conn(fenced_session)
    _seed_storage_policies(conn, identity_id=fenced_session.identity_id, identity_bytes=100, container_bytes=None)
    archived = _blob(conn, session_id=_session(conn, user_id=fenced_session.identity_id, archived=True), size_bytes=30)
    pending = _blob(conn, session_id=fenced_session.session_id, size_bytes=30, status="pending")
    errored = _blob(conn, session_id=fenced_session.session_id, size_bytes=30, status="error")
    recorded: list[QuotaExceeded] = []
    with pytest.raises(IdentityStorageQuotaExceededError, match=r"\(90 bytes\) plus 11 bytes"):
        _admit(fenced_session, 11, recorded)
    conn.execute(delete(blobs_table).where(blobs_table.c.id.in_([archived, pending, errored])))
    assert _admit(fenced_session, 11, recorded) == StorageAdmission(
        identity_id="alice", additional_bytes=11, usage=0, cap=100, ceiling=None
    )


def test_r13_measures_the_session_owner_not_every_identity(fenced_session: FencedSession) -> None:
    """Mutation-derivation for attribution: bob's bytes count only once his session becomes alice's."""
    conn = _conn(fenced_session)
    ensure_test_identity(conn, identity_id="bob")
    _seed_storage_policies(conn, identity_id=fenced_session.identity_id, identity_bytes=100, container_bytes=None)
    bobs_session = _session(conn, user_id="bob")
    _blob(conn, session_id=bobs_session, size_bytes=500)
    recorded: list[QuotaExceeded] = []
    assert _admit(fenced_session, 11, recorded).usage == 0
    conn.execute(update(sessions_table).where(sessions_table.c.id == bobs_session).values(user_id="alice"))
    with pytest.raises(IdentityStorageQuotaExceededError, match=r"\(500 bytes\) plus 11 bytes"):
        _admit(fenced_session, 11, recorded)


def test_r13_admits_without_measuring_when_no_policy_row_is_active(fenced_session: FencedSession) -> None:
    """A storage regime is optional (D18): with no active identity or container row there is no bound to enforce."""
    conn = _conn(fenced_session)
    _ninety_bytes_across_two_sessions(fenced_session)
    recorded: list[QuotaExceeded] = []
    assert _admit(fenced_session, 10**9, recorded) == StorageAdmission(
        identity_id="alice", additional_bytes=10**9, usage=None, cap=None, ceiling=None
    )
    _seed_storage_policies(conn, identity_id=fenced_session.identity_id, identity_bytes=100, container_bytes=None)
    with pytest.raises(IdentityStorageQuotaExceededError, match=r"would exceed its storage quota \(100 bytes\)"):
        _admit(fenced_session, 11, recorded)
    conn.execute(update(quota_policies_table).where(quota_policies_table.c.policy_id == "storage-identity").values(revoked_at=SET_AT))
    assert _admit(fenced_session, 11, recorded).usage is None


@pytest.mark.parametrize(("additional_bytes", "refused"), [(-5, False), (0, False), (1, True)])
def test_r13_exempts_a_write_that_adds_no_net_bytes(fenced_session: FencedSession, additional_bytes: int, refused: bool) -> None:
    """Spec :1141-1143: a path that admits no new bytes (fork replay, a shrinking replacement) stays exempt even over the cap."""
    conn = _conn(fenced_session)
    _seed_storage_policies(conn, identity_id=fenced_session.identity_id, identity_bytes=100, container_bytes=None)
    _blob(conn, session_id=fenced_session.session_id, size_bytes=200)
    recorded: list[QuotaExceeded] = []
    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError, match=r"\(200 bytes\) plus 1 bytes"):
            _admit(fenced_session, additional_bytes, recorded)
    else:
        assert _admit(fenced_session, additional_bytes, recorded) == StorageAdmission(
            identity_id="alice", additional_bytes=additional_bytes, usage=None, cap=None, ceiling=None
        )
        assert recorded == []


def test_r13_refuses_when_the_accounting_query_fails(fenced_session: FencedSession) -> None:
    """Spec :1131: refuse when the accounting query fails; nothing was measured, so no quota_exceeded row is written."""
    _seed_storage_policies(_conn(fenced_session), identity_id=fenced_session.identity_id, identity_bytes=100, container_bytes=None)
    failures: list[str] = []

    def _fail_the_usage_sum(conn, cursor, statement, parameters, context, executemany) -> None:
        if "sum(blobs.size_bytes)" in statement:
            failures.append(statement)
            raise OperationalError(statement, parameters, Exception("accounting store unavailable"))

    event.listen(fenced_session.engine, "before_cursor_execute", _fail_the_usage_sum)
    recorded: list[QuotaExceeded] = []
    try:
        with pytest.raises(StorageAccountingUnavailableError, match=r"Storage accounting for session .* is unavailable") as caught:
            _admit(fenced_session, 11, recorded)
    finally:
        event.remove(fenced_session.engine, "before_cursor_execute", _fail_the_usage_sum)
    assert len(failures) == 1, "the instrument must have failed the real usage SUM"
    assert isinstance(caught.value.__cause__, OperationalError)
    assert recorded == []
    assert _admit(fenced_session, 11, recorded).usage == 0


def test_r13_a_failed_audit_write_refuses_the_mutation(fenced_session: FencedSession) -> None:
    """R4: the record callback runs before the refusal reaches the caller, so an audit failure is what the caller sees."""
    _seed_storage_policies(_conn(fenced_session), identity_id=fenced_session.identity_id, identity_bytes=10, container_bytes=None)

    def _landscape_down(outcome: QuotaExceeded) -> None:
        raise AuditIntegrityError(f"landscape unavailable for {outcome.identity_id}")

    with pytest.raises(AuditIntegrityError, match="landscape unavailable for alice"):
        RepositoryQuotaAuthority.admit_storage_bytes(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            additional_bytes=11,
            operation="session_fork",
            record=_landscape_down,
        )


def test_r13_an_unwired_recorder_fails_closed(fenced_session: FencedSession) -> None:
    _seed_storage_policies(_conn(fenced_session), identity_id=fenced_session.identity_id, identity_bytes=10, container_bytes=None)
    with pytest.raises(AuditIntegrityError, match="quota_exceeded refusal for identity alice has no auth audit writer wired"):
        RepositoryQuotaAuthority.admit_storage_bytes(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            additional_bytes=11,
            operation="inline_custody",
            record=refuse_unrecorded_quota_exceeded,
        )


def test_r13_refuses_an_operation_outside_the_closed_vocabulary(fenced_session: FencedSession) -> None:
    invalid_operation: Any = "upload"  # outside StorageAdmissionOperation on purpose: the runtime check is under test
    with pytest.raises(ValueError, match="storage admission operation 'upload' is not one of"):
        RepositoryQuotaAuthority.admit_storage_bytes(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            additional_bytes=1,
            operation=invalid_operation,
            record=lambda outcome: None,
        )
```

- [ ] **Step 2: Run the authority tests to verify they fail.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_storage_quota_authority.py -n 0 > /tmp/i-lane-i2-step2.log 2>&1; echo exit=$?
```

Expected: `exit=2`. Collection stops with `ImportError: cannot import name 'IdentityStorageQuotaExceededError' from 'elspeth.contracts.blobs'` and `Interrupted: 1 error during collection`.

- [ ] **Step 3: Add the two storage errors to the blob contract and re-export them.**

`src/elspeth/contracts/blobs.py` HEAD lines 348-354 read:

```python
        self.limit_bytes = limit_bytes

    def __setattr__(self, name: str, value: object) -> None:
        _guard_frozen_attr(self, name, value)


class BlobStateError(BlobError):
```

Replace them with:

```python
        self.limit_bytes = limit_bytes

    def __setattr__(self, name: str, value: object) -> None:
        _guard_frozen_attr(self, name, value)


class IdentityStorageQuotaExceededError(BlobQuotaExceededError):
    """R13: the write would take the session owner's live blob total over its storage cap or the container ceiling.

    A subclass, so every existing ``except BlobQuotaExceededError`` arm (run-output
    finalize, the composer blob tools, guided plan, session fork) refuses it the
    way it refuses the per-session bound. ``current_bytes`` is the identity's
    measured usage and ``limit_bytes`` the bound in force, so the inherited
    fields stay true; the per-session error keeps its own shape.
    """

    _FROZEN_ATTRS: ClassVar[frozenset[str]] = frozenset(
        {"session_id", "current_bytes", "limit_bytes", "identity_id", "cap", "ceiling", "usage", "additional_bytes"}
    )

    def __init__(
        self,
        session_id: str,
        *,
        identity_id: str,
        cap: int | None,
        ceiling: int | None,
        usage: int,
        additional_bytes: int,
    ) -> None:
        bounds = [bound for bound in (cap, ceiling) if bound is not None]
        if not bounds:
            raise ValueError("IdentityStorageQuotaExceededError requires a cap or a ceiling")
        limit = min(bounds)
        BlobError.__init__(
            self,
            f"Identity {identity_id} blob storage ({usage} bytes) plus {additional_bytes} bytes would exceed its storage quota ({limit} bytes)",
        )
        self.session_id = session_id
        self.current_bytes = usage
        self.limit_bytes = limit
        self.identity_id = identity_id
        self.cap = cap
        self.ceiling = ceiling
        self.usage = usage
        self.additional_bytes = additional_bytes


class StorageAccountingUnavailableError(BlobError):
    """R13: the identity storage accounting query failed, so the byte-admitting write is refused (sso-design.md:1131)."""

    _FROZEN_ATTRS: ClassVar[frozenset[str]] = frozenset({"session_id"})

    def __init__(self, session_id: str) -> None:
        super().__init__(f"Storage accounting for session {session_id} is unavailable; the blob write is refused")
        self.session_id = session_id

    def __setattr__(self, name: str, value: object) -> None:
        _guard_frozen_attr(self, name, value)


class BlobStateError(BlobError):
```

`IdentityStorageQuotaExceededError` inherits `BlobQuotaExceededError.__setattr__`, which reads `type(self)._FROZEN_ATTRS`, so the eight attributes are frozen after construction.

`src/elspeth/web/blobs/protocol.py` HEAD lines 42-44 read:

```python
from elspeth.contracts.blobs import FinalizeBlobStatus as FinalizeBlobStatus
from elspeth.contracts.blobs import InlineCustodyRequest as InlineCustodyRequest
from elspeth.contracts.blobs import StorageMimeType as StorageMimeType
```

Replace them with:

```python
from elspeth.contracts.blobs import FinalizeBlobStatus as FinalizeBlobStatus
from elspeth.contracts.blobs import IdentityStorageQuotaExceededError as IdentityStorageQuotaExceededError
from elspeth.contracts.blobs import InlineCustodyRequest as InlineCustodyRequest
from elspeth.contracts.blobs import StorageAccountingUnavailableError as StorageAccountingUnavailableError
from elspeth.contracts.blobs import StorageMimeType as StorageMimeType
```

- [ ] **Step 4: Add storage admission to the QuotaAuthority.**

`src/elspeth/web/coordination/quota_authority.py` after I1, lines 32-43 read:

```python
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, final

from sqlalchemy import case, func, insert, or_, select
from sqlalchemy.engine import Connection

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.sessions.models import quota_policies_table, sessions_table, token_usage_ledger_table
```

Replace them with:

```python
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, final

from sqlalchemy import case, func, insert, or_, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.blobs import IdentityStorageQuotaExceededError, StorageAccountingUnavailableError
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.sessions.models import blobs_table, quota_policies_table, sessions_table, token_usage_ledger_table
```

`src/elspeth/web/coordination/quota_authority.py` after I1, lines 268-292 (the last lines of the file) read:

```python
    @staticmethod
    def active_policy(connection_token: str, *, identity_id: str) -> ActiveQuotaPolicies:
        """Lock and return the identity's unrevoked policy and the unrevoked container ceiling."""
        conn = _resolve_mutation_connection(connection_token)
        policy = quota_policies_table.c
        identity = conn.execute(
            select(policy.policy_id, policy.tokens_per_day, policy.storage_bytes)
            .where(policy.identity_id == identity_id, policy.revoked_at.is_(None))
            .with_for_update()
        ).one_or_none()
        container = conn.execute(
            select(policy.policy_id, policy.tokens_per_day, policy.storage_bytes)
            .where(policy.identity_id.is_(None), policy.revoked_at.is_(None))
            .with_for_update()
        ).one_or_none()
        return ActiveQuotaPolicies(
            identity=None
            if identity is None
            else QuotaPolicyRow(policy_id=identity.policy_id, tokens_per_day=identity.tokens_per_day, storage_bytes=identity.storage_bytes),
            container=None
            if container is None
            else QuotaPolicyRow(
                policy_id=container.policy_id, tokens_per_day=container.tokens_per_day, storage_bytes=container.storage_bytes
            ),
        )
```

Replace them with:

```python
    @staticmethod
    def active_policy(connection_token: str, *, identity_id: str) -> ActiveQuotaPolicies:
        """Lock and return the identity's unrevoked policy and the unrevoked container ceiling."""
        return _active_policy_rows(_resolve_mutation_connection(connection_token), identity_id=identity_id, for_update=True)

    @staticmethod
    def admit_storage_bytes(
        connection_token: str,
        *,
        session_id: str,
        additional_bytes: int,
        operation: StorageAdmissionOperation,
        record: Callable[[QuotaExceeded], None],
    ) -> StorageAdmission:
        """R13 on a token-bound fenced transaction: see :func:`admit_storage_bytes_on_connection`."""
        return admit_storage_bytes_on_connection(
            _resolve_mutation_connection(connection_token),
            session_id=session_id,
            additional_bytes=additional_bytes,
            operation=operation,
            record=record,
        )


StorageAdmissionOperation = Literal["blob_create", "inline_custody", "run_output_finalize", "blob_replacement", "session_fork"]
_STORAGE_ADMISSION_OPERATIONS: frozenset[str] = frozenset(
    {"blob_create", "inline_custody", "run_output_finalize", "blob_replacement", "session_fork"}
)


@final
@dataclass(frozen=True, slots=True)
class StorageAdmission:
    """An admitted byte-admitting write. ``usage`` is ``None`` when nothing was measured: no active policy, or no net new bytes."""

    identity_id: str
    additional_bytes: int
    usage: int | None
    cap: int | None
    ceiling: int | None


def refuse_unrecorded_quota_exceeded(outcome: QuotaExceeded) -> None:
    """The recorder a blob authority gets when none is wired: a storage refusal nobody audits fails closed (R4)."""
    raise AuditIntegrityError(f"quota_exceeded refusal for identity {outcome.identity_id} has no auth audit writer wired")


def _active_policy_rows(connection: Connection, *, identity_id: str, for_update: bool) -> ActiveQuotaPolicies:
    """The identity's unrevoked policy row and the unrevoked container ceiling row.

    R14 locks both rows (``for_update=True``) because token admission is a
    decision over one row set. R13 reads them without a lock: every blob write
    in the deployment would otherwise serialise on the one container row, and
    D24 rules storage eventually consistent precisely to avoid a lock broader
    than the session.
    """
    policy = quota_policies_table.c
    identity_query = select(policy.policy_id, policy.tokens_per_day, policy.storage_bytes).where(
        policy.identity_id == identity_id, policy.revoked_at.is_(None)
    )
    container_query = select(policy.policy_id, policy.tokens_per_day, policy.storage_bytes).where(
        policy.identity_id.is_(None), policy.revoked_at.is_(None)
    )
    if for_update:
        identity_query = identity_query.with_for_update()
        container_query = container_query.with_for_update()
    identity = connection.execute(identity_query).one_or_none()
    container = connection.execute(container_query).one_or_none()
    return ActiveQuotaPolicies(
        identity=None
        if identity is None
        else QuotaPolicyRow(policy_id=identity.policy_id, tokens_per_day=identity.tokens_per_day, storage_bytes=identity.storage_bytes),
        container=None
        if container is None
        else QuotaPolicyRow(policy_id=container.policy_id, tokens_per_day=container.tokens_per_day, storage_bytes=container.storage_bytes),
    )


def admit_storage_bytes_on_connection(
    connection: Connection,
    *,
    session_id: str,
    additional_bytes: int,
    operation: StorageAdmissionOperation,
    record: Callable[[QuotaExceeded], None],
) -> StorageAdmission:
    """R13: refuse a write that would take the session owner's live blob total over its cap or the ceiling.

    Usage is ``SUM(blobs.size_bytes)`` over every row of every session the
    owner holds: archived sessions and ``pending``/``error`` rows count because
    they occupy disk (spec :1143-1145). ``additional_bytes`` is the NET increase
    the write makes; a write adding no net bytes (the fork's
    ``missing_bytes == 0`` replay, a shrinking replacement) is exempt and
    measures nothing. With no active policy row there is no bound (D18).

    On refusal the typed ``QuotaExceeded`` goes to ``record`` BEFORE the error
    reaches the caller and before the caller's transaction ends (R4), so a
    failed audit write is the error the caller sees. A failed accounting query
    refuses with ``StorageAccountingUnavailableError`` and records nothing,
    because nothing was measured (spec :1131). The bound is eventually
    consistent (D24): the caller's session lock does not serialise two sessions
    of one identity, and this function takes no lock of its own.
    """
    if operation not in _STORAGE_ADMISSION_OPERATIONS:
        raise ValueError(f"storage admission operation {operation!r} is not one of {sorted(_STORAGE_ADMISSION_OPERATIONS)}")
    if type(additional_bytes) is not int:
        raise TypeError("additional_bytes must be an exact int")
    if not callable(record):
        raise TypeError("record must be callable")
    owner = connection.execute(
        select(sessions_table.c.user_id, sessions_table.c.auth_provider_type).where(sessions_table.c.id == session_id)
    ).one_or_none()
    if owner is None:
        raise AuditIntegrityError("Storage admission names a session that does not exist")
    if additional_bytes <= 0:
        return StorageAdmission(identity_id=owner.user_id, additional_bytes=additional_bytes, usage=None, cap=None, ceiling=None)
    try:
        policies = _active_policy_rows(connection, identity_id=owner.user_id, for_update=False)
        if policies.identity is None and policies.container is None:
            return StorageAdmission(identity_id=owner.user_id, additional_bytes=additional_bytes, usage=None, cap=None, ceiling=None)
        usage = connection.execute(
            select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0))
            .select_from(blobs_table.join(sessions_table, sessions_table.c.id == blobs_table.c.session_id))
            .where(sessions_table.c.user_id == owner.user_id)
        ).scalar_one()
    except SQLAlchemyError as exc:
        raise StorageAccountingUnavailableError(session_id) from exc
    # COALESCE guarantees an exact int; anything else is a Tier 1 anomaly.
    if type(usage) is not int:
        raise AuditIntegrityError(f"Tier 1: identity storage COALESCE(SUM) returned {type(usage).__name__}, expected int")
    cap = policies.identity.storage_bytes if policies.identity is not None else None
    ceiling = policies.container.storage_bytes if policies.container is not None else None
    limit = min(bound for bound in (cap, ceiling) if bound is not None)
    if usage + additional_bytes > limit:
        record(
            QuotaExceeded(
                identity_id=owner.user_id,
                provider=owner.auth_provider_type,
                operation=operation,
                dimension="storage",
                cap=cap,
                ceiling=ceiling,
                usage=usage,
                identity_policy_id=policies.identity.policy_id if policies.identity is not None else None,
                container_policy_id=policies.container.policy_id if policies.container is not None else None,
            )
        )
        raise IdentityStorageQuotaExceededError(
            session_id,
            identity_id=owner.user_id,
            cap=cap,
            ceiling=ceiling,
            usage=usage,
            additional_bytes=additional_bytes,
        )
    return StorageAdmission(identity_id=owner.user_id, additional_bytes=additional_bytes, usage=usage, cap=cap, ceiling=ceiling)
```

- [ ] **Step 5: Run the authority tests and I1's quota tests.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_storage_quota_authority.py tests/unit/web/coordination/test_quota_authority.py -n 0 > /tmp/i-lane-i2-step5.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `38 passed`: the 15 storage tests and I1's 23 quota tests, which prove that the `_active_policy_rows` split left R14's locked lookup unchanged.

- [ ] **Step 6: Write the failing site tests.** One fire test and one mutation-derivation test per admission point, plus the 413 body on both upload routes. The run-output test seeds its run through the real composition-state writer (`_seed_active_run`), because the whole-tree static direct-writer gate (`tests/unit/web/sessions/test_static_direct_writers.py`) refuses a test that inserts `composition_states` rows directly.

Create `tests/unit/web/blobs/test_storage_quota_sites.py`:

```python
"""R13 at every byte-admitting site (Task I2).

Spec :1133-1145 names four sites; they reach seven admission points, and each
one has a fire test and a mutation-derivation test here. The mutation is
always the identity's ``quota_policies.storage_bytes`` row: the same write
flips between admitted and refused on that row alone.

* multipart and inline upload: ``create_blob`` -> ``transaction.blobs.reserve_blob``
  (the route test below also pins the 413 body);
* composer inline custody: ``reserve_inline_custody`` -> ``reserve_blob``, and
  the guided-full settlement ``persist_inline_custody_blob_on_connection``;
* run-output finalize: ``finalize_run_output_blobs`` -> ``mark_run_output_blob_ready``,
  plus the unlinked-output facet ``finalize_pending_output_blob``;
* ``copy_blobs_for_fork``: the pre-copy check (refuses before any child row)
  and the per-copy reservation;
* blob replacement, which grows a live row: ``prepare_blob_replacement`` and
  ``commit_blob_replacement``.

The identity already holds 90 bytes in another session, so the per-session
bound (500 MiB by default) never fires and every refusal is R13's.
"""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi import FastAPI
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from elspeth.contracts.blobs import IdentityStorageQuotaExceededError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.blobs import service as blob_service_module
from elspeth.web.blobs.routes import create_blobs_router
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.config import WebSettings
from elspeth.web.coordination.quota_authority import QuotaExceeded
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.middleware.rate_limit import ComposerRateLimiter
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blobs_table, quota_policies_table, sessions_table
from elspeth.web.sessions.routes import create_session_router
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.session_fences import seed_live_compose_context, seed_live_operation_context
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.blobs import test_service as blob_service_tests
from tests.unit.web.sessions.guided_test_authority import DualFencedSessionServiceHarness

IDENTITY = "test-user"
NOW = datetime(2026, 9, 13, tzinfo=UTC)
CAP_CASES = [(101, False), (100, True)]
REFUSED_11_OVER_90 = r"Identity test-user blob storage \(90 bytes\) plus 11 bytes would exceed its storage quota \(100 bytes\)"


@pytest.fixture()
def db_engine() -> Engine:
    engine = create_session_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id=IDENTITY)
    return engine


def _insert_session(engine: Engine, *, archived: bool = False, forked_from: UUID | None = None) -> UUID:
    session_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(sessions_table).values(
                id=str(session_id),
                user_id=IDENTITY,
                auth_provider_type="local",
                title="quota session",
                created_at=NOW,
                updated_at=NOW,
                archived_at=NOW if archived else None,
                forked_from_session_id=str(forked_from) if forked_from is not None else None,
            )
        )
    return session_id


@pytest.fixture()
def session_id(db_engine: Engine) -> UUID:
    return _insert_session(db_engine)


@pytest.fixture()
def recorded() -> list[QuotaExceeded]:
    return []


@pytest.fixture()
def blob_service(db_engine: Engine, tmp_path: Path, recorded: list[QuotaExceeded]) -> BlobServiceImpl:
    """The production wiring in miniature: one recorder shared by the session authority and the blob service."""
    authority = SQLiteLocalSessionOperationAuthority(db_engine, quota_exceeded_recorder=recorded.append)
    return BlobServiceImpl(db_engine, tmp_path, session_operation_authority=authority, quota_exceeded_recorder=recorded.append)


def _seed_identity_cap(engine: Engine, *, storage_bytes: int, container_bytes: int | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="storage-identity",
                identity_id=IDENTITY,
                tokens_per_day=1000,
                storage_bytes=storage_bytes,
                set_by_actor="operator",
                set_by_identity_id=None,
                set_at=NOW,
            )
        )
        if container_bytes is not None:
            conn.execute(
                insert(quota_policies_table).values(
                    policy_id="storage-container",
                    identity_id=None,
                    tokens_per_day=5000,
                    storage_bytes=container_bytes,
                    set_by_actor="config",
                    set_by_identity_id=None,
                    set_at=NOW,
                )
            )


def _occupy(engine: Engine, *, size_bytes: int) -> None:
    """Put ``size_bytes`` of ready blob in ANOTHER session the identity owns."""
    other_session = _insert_session(engine)
    blob_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id=str(blob_id),
                session_id=str(other_session),
                filename="elsewhere.csv",
                mime_type="text/csv",
                size_bytes=size_bytes,
                content_hash="e" * 64,
                storage_path=f"/nonexistent/{blob_id}.csv",
                created_at=NOW,
                created_by="user",
                source_description=None,
                status="ready",
            )
        )


def _session_rows(engine: Engine, session_id: UUID) -> list[tuple[str, int]]:
    with engine.connect() as conn:
        return [
            (row.status, row.size_bytes)
            for row in conn.execute(
                select(blobs_table.c.status, blobs_table.c.size_bytes).where(blobs_table.c.session_id == str(session_id))
            )
        ]


def _files_under(data_dir: Path, session_id: UUID) -> list[Path]:
    session_dir = data_dir.resolve() / "blobs" / str(session_id)
    return sorted(path for path in session_dir.rglob("*") if path.is_file()) if session_dir.exists() else []


def _recorded(recorded: list[QuotaExceeded]) -> list[tuple[str, str, str, int, int | None, int | None]]:
    return [
        (outcome.identity_id, outcome.operation, outcome.dimension, outcome.usage, outcome.cap, outcome.ceiling) for outcome in recorded
    ]


# ── site 1: multipart and inline upload ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), CAP_CASES)
async def test_upload_create_blob_is_refused_before_any_byte_reaches_disk(
    db_engine: Engine,
    session_id: UUID,
    blob_service: BlobServiceImpl,
    recorded: list[QuotaExceeded],
    tmp_path: Path,
    cap: int,
    refused: bool,
) -> None:
    _seed_identity_cap(db_engine, storage_bytes=cap)
    _occupy(db_engine, size_bytes=90)
    context = seed_live_compose_context(db_engine, session_id)
    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError, match=REFUSED_11_OVER_90):
            await blob_service.create_blob(session_id, "upload.csv", b"x" * 11, "text/csv", session_operation_context=context)
        assert _session_rows(db_engine, session_id) == []
        assert _files_under(tmp_path, session_id) == []
        assert _recorded(recorded) == [(IDENTITY, "blob_create", "storage", 90, 100, None)]
    else:
        record = await blob_service.create_blob(session_id, "upload.csv", b"x" * 11, "text/csv", session_operation_context=context)
        assert record.status == "ready"
        assert recorded == []


def _quota_app(engine: Engine, tmp_path: Path, recorded: list[QuotaExceeded]) -> FastAPI:
    session_service = DualFencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    authority = SQLiteLocalSessionOperationAuthority(engine, quota_exceeded_recorder=recorded.append)
    app = FastAPI()

    async def _user() -> UserIdentity:
        return UserIdentity(user_id=IDENTITY, username=IDENTITY)

    app.dependency_overrides[get_current_user] = _user
    app.state.settings = WebSettings(
        data_dir=tmp_path,
        max_upload_bytes=10 * 1024 * 1024,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )
    app.state.session_service = session_service
    app.state.blob_service = BlobServiceImpl(
        engine, tmp_path, session_operation_authority=authority, quota_exceeded_recorder=recorded.append
    )
    app.state.rate_limiter = ComposerRateLimiter(limit=100)
    app.include_router(create_session_router())
    app.include_router(create_blobs_router())
    return app


@pytest.mark.parametrize(("cap", "refused"), CAP_CASES)
@pytest.mark.parametrize("route", ["multipart", "inline"])
def test_upload_routes_answer_413_naming_cap_ceiling_and_usage(
    db_engine: Engine, tmp_path: Path, recorded: list[QuotaExceeded], route: str, cap: int, refused: bool
) -> None:
    """Spec :1131-1132: the response names the cap and the current usage so the user can delete blobs to recover."""
    client = SyncASGITestClient(_quota_app(db_engine, tmp_path, recorded))
    created = client.post("/api/sessions", json={"title": "quota"})
    assert created.status_code == 201
    session_id = created.json()["id"]
    _seed_identity_cap(db_engine, storage_bytes=cap, container_bytes=1000)
    _occupy(db_engine, size_bytes=90)
    if route == "multipart":
        response = client.post(f"/api/sessions/{session_id}/blobs", files={"file": ("note.txt", io.BytesIO(b"hello world"), "text/plain")})
    else:
        response = client.post(
            f"/api/sessions/{session_id}/blobs/inline",
            json={"filename": "note.txt", "content": "hello world", "mime_type": "text/plain"},
        )
    if refused:
        assert response.status_code == 413
        assert response.json() == {
            "detail": {
                "error_type": "storage_quota_exceeded",
                "detail": "Identity test-user blob storage (90 bytes) plus 11 bytes would exceed its storage quota (100 bytes)",
                "dimension": "storage",
                "cap": 100,
                "ceiling": 1000,
                "usage": 90,
            }
        }
        assert _recorded(recorded) == [(IDENTITY, "blob_create", "storage", 90, 100, 1000)]
    else:
        assert response.status_code == 201
        assert recorded == []


# ── site 2: composer inline custody ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), CAP_CASES)
async def test_reserve_inline_custody_admits_through_r13(
    db_engine: Engine,
    session_id: UUID,
    blob_service: BlobServiceImpl,
    recorded: list[QuotaExceeded],
    tmp_path: Path,
    cap: int,
    refused: bool,
) -> None:
    context = seed_live_compose_context(db_engine, session_id)
    request = blob_service_tests._custody_request(db_engine, session_id, content=b"x" * 11)
    _seed_identity_cap(db_engine, storage_bytes=cap)
    _occupy(db_engine, size_bytes=90)
    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError, match=REFUSED_11_OVER_90):
            await blob_service.reserve_inline_custody(request, session_operation_context=context)
        assert _session_rows(db_engine, session_id) == []
        assert _files_under(tmp_path, session_id) == []
        assert _recorded(recorded) == [(IDENTITY, "blob_create", "storage", 90, 100, None)]
    else:
        assert (await blob_service.reserve_inline_custody(request, session_operation_context=context)).status == "ready"
        assert recorded == []


@pytest.mark.parametrize(("cap", "refused"), CAP_CASES)
def test_guided_settlement_inline_custody_admits_through_r13(
    db_engine: Engine, session_id: UUID, recorded: list[QuotaExceeded], tmp_path: Path, cap: int, refused: bool
) -> None:
    """``persist_inline_custody_blob_on_connection`` runs inside the guided-full cohort transaction (pipeline_custody.py)."""
    request = blob_service_tests._custody_request(db_engine, session_id, content=b"x" * 11)
    staged = blob_service_module.prepare_inline_custody_blob(data_dir=tmp_path, request=request, write_guard=lambda: None)
    _seed_identity_cap(db_engine, storage_bytes=cap)
    _occupy(db_engine, size_bytes=90)
    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError, match=REFUSED_11_OVER_90), db_engine.begin() as conn:
            blob_service_module.persist_inline_custody_blob_on_connection(
                conn,
                staged=staged,
                max_storage_per_session=10_000,
                write_fence=None,
                quota_exceeded_recorder=recorded.append,
            )
        assert _session_rows(db_engine, session_id) == []
        assert _recorded(recorded) == [(IDENTITY, "inline_custody", "storage", 90, 100, None)]
    else:
        with db_engine.begin() as conn:
            row, _publication = blob_service_module.persist_inline_custody_blob_on_connection(
                conn,
                staged=staged,
                max_storage_per_session=10_000,
                write_fence=None,
                quota_exceeded_recorder=recorded.append,
            )
        assert row.status == "ready"
        assert recorded == []


# ── site 3: run-output finalize ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), CAP_CASES)
async def test_run_output_finalize_marks_the_over_quota_output_error_and_removes_its_bytes(
    db_engine: Engine,
    session_id: UUID,
    blob_service: BlobServiceImpl,
    recorded: list[QuotaExceeded],
    cap: int,
    refused: bool,
) -> None:
    """The subclass reaches the existing ``except BlobQuotaExceededError`` arm (blobs/service.py ``_finalize_one_output_blob``)."""
    compose = seed_live_compose_context(db_engine, session_id)
    run_id = UUID(
        await blob_service_tests._seed_active_run(
            db_engine, session_id, session_operation_context=compose, source=blob_service_tests._RUN_SOURCE, status="running"
        )
    )
    execute = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
    pending = blob_service_tests.reserve_output_blob(blob_service, session_id, run_id, execute, filename="result.csv")
    Path(pending.storage_path).write_bytes(b"x" * 11)
    _seed_identity_cap(db_engine, storage_bytes=cap)
    _occupy(db_engine, size_bytes=90)
    result = await blob_service.finalize_run_output_blobs(run_id, success=True, session_operation_context=execute)
    if refused:
        assert result.finalized == ()
        assert [(error.blob_id, error.exc_type) for error in result.errors] == [(pending.id, "IdentityStorageQuotaExceededError")]
        assert _session_rows(db_engine, session_id) == [("error", 0)]
        assert not Path(pending.storage_path).exists()
        assert _recorded(recorded) == [(IDENTITY, "run_output_finalize", "storage", 90, 100, None)]
    else:
        assert [record.id for record in result.finalized] == [pending.id]
        assert _session_rows(db_engine, session_id) == [("ready", 11)]
        assert recorded == []


@pytest.mark.parametrize(("cap", "refused"), CAP_CASES)
def test_unlinked_output_finalize_facet_admits_through_r13(
    db_engine: Engine,
    session_id: UUID,
    blob_service: BlobServiceImpl,
    recorded: list[QuotaExceeded],
    cap: int,
    refused: bool,
) -> None:
    execute = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
    record = blob_service_tests._pending_output_record(blob_service, session_id, "unlinked.csv")
    authority = blob_service._session_operation_authority
    authority.mutate(execute, lambda transaction: transaction.blobs.reserve_pending_output_blob(record=record))
    _seed_identity_cap(db_engine, storage_bytes=cap)
    _occupy(db_engine, size_bytes=90)

    def _finalize(transaction):
        return transaction.blobs.finalize_pending_output_blob(
            blob_id=record.id, status="ready", size_bytes=11, content_hash="c" * 64, max_storage_per_session=10_000
        )

    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError, match=REFUSED_11_OVER_90):
            authority.mutate(execute, _finalize)
        assert _session_rows(db_engine, session_id) == [("pending", 0)]
        assert _recorded(recorded) == [(IDENTITY, "run_output_finalize", "storage", 90, 100, None)]
    else:
        assert authority.mutate(execute, _finalize).status == "ready"
        assert recorded == []


# ── site 4: copy_blobs_for_fork ────────────────────────────────────────────


async def _fork_fixture(db_engine: Engine, session_id: UUID, blob_service: BlobServiceImpl):
    target = _insert_session(db_engine, archived=True, forked_from=session_id)
    compose = seed_live_compose_context(db_engine, session_id)
    await blob_service.create_blob(session_id, "source.csv", b"x" * 11, "text/csv", session_operation_context=compose)
    fork_harness = blob_service_tests.TestCopyBlobsForFork
    plan = await fork_harness._plan(blob_service, session_id, target)
    fence = await fork_harness._authorize_copy(blob_service, session_id, target, plan)
    return target, plan, fence


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), CAP_CASES)
async def test_fork_refuses_before_the_copy_loop_leaves_a_child_row(
    db_engine: Engine,
    session_id: UUID,
    blob_service: BlobServiceImpl,
    recorded: list[QuotaExceeded],
    cap: int,
    refused: bool,
) -> None:
    """Spec :1140-1142: site 4 refuses BEFORE the copy loop starts, so a refused fork leaves no half-populated child."""
    target, plan, fence = await _fork_fixture(db_engine, session_id, blob_service)
    _seed_identity_cap(db_engine, storage_bytes=cap)
    _occupy(db_engine, size_bytes=79)
    copies_started = 0

    async def _checkpoint() -> None:
        nonlocal copies_started
        copies_started += 1

    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError, match=REFUSED_11_OVER_90):
            await blob_service.copy_blobs_for_fork(session_id, target, plan, fence, checkpoint=_checkpoint)
        assert copies_started == 1, "the refusal must come from the pre-copy check, before the loop's first checkpoint"
        assert _session_rows(db_engine, target) == []
        assert _recorded(recorded) == [(IDENTITY, "session_fork", "storage", 90, 100, None)]
    else:
        copied = await blob_service.copy_blobs_for_fork(session_id, target, plan, fence, checkpoint=_checkpoint)
        assert [record.status for record in copied.values()] == ["ready"]
        assert recorded == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), [(151, False), (150, True)])
async def test_fork_copy_reservation_admits_through_r13_when_usage_grows_after_the_pre_check(
    db_engine: Engine,
    session_id: UUID,
    blob_service: BlobServiceImpl,
    recorded: list[QuotaExceeded],
    cap: int,
    refused: bool,
) -> None:
    """The per-copy reservation (``_reserve_pending_blob``) re-admits: usage another session added after the pre-check counts."""
    target, plan, fence = await _fork_fixture(db_engine, session_id, blob_service)
    _seed_identity_cap(db_engine, storage_bytes=cap)
    _occupy(db_engine, size_bytes=79)
    calls = 0

    async def _checkpoint() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            _occupy(db_engine, size_bytes=50)

    if refused:
        with pytest.raises(
            IdentityStorageQuotaExceededError, match=r"\(140 bytes\) plus 11 bytes would exceed its storage quota \(150 bytes\)"
        ):
            await blob_service.copy_blobs_for_fork(session_id, target, plan, fence, checkpoint=_checkpoint)
        assert _session_rows(db_engine, target) == []
        assert _recorded(recorded) == [(IDENTITY, "session_fork", "storage", 140, 150, None)]
    else:
        copied = await blob_service.copy_blobs_for_fork(session_id, target, plan, fence, checkpoint=_checkpoint)
        assert [record.status for record in copied.values()] == ["ready"]
        assert recorded == []


# ── blob replacement grows a live row ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), CAP_CASES)
async def test_replacement_prepare_admits_the_net_growth_through_r13(
    db_engine: Engine,
    session_id: UUID,
    blob_service: BlobServiceImpl,
    recorded: list[QuotaExceeded],
    cap: int,
    refused: bool,
) -> None:
    compose = seed_live_compose_context(db_engine, session_id)
    expected = await blob_service.create_blob(session_id, "grow.csv", b"abc", "text/csv", session_operation_context=compose)
    replacement = replace(expected, size_bytes=14, content_hash="b" * 64)
    _seed_identity_cap(db_engine, storage_bytes=cap)
    _occupy(db_engine, size_bytes=87)
    authority = blob_service._session_operation_authority

    def _prepare(transaction):
        return transaction.blobs.prepare_blob_replacement(
            replacement_id=uuid4(),
            expected=expected,
            replacement=replacement,
            staging_path=f"{expected.storage_path}.quota.stage",
            backup_path=f"{expected.storage_path}.quota.backup",
            max_storage_per_session=10_000,
            accepting_proposal_id=None,
        )

    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError, match=REFUSED_11_OVER_90):
            authority.mutate(compose, _prepare)
        assert authority.mutate(compose, lambda transaction: transaction.blobs.read_blob_replacement(blob_id=expected.id)) is None
        assert _recorded(recorded) == [(IDENTITY, "blob_replacement", "storage", 90, 100, None)]
    else:
        assert authority.mutate(compose, _prepare).phase == "intent"
        assert recorded == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), [(102, False), (101, True)])
async def test_replacement_commit_re_admits_the_net_growth_through_r13(
    db_engine: Engine,
    session_id: UUID,
    blob_service: BlobServiceImpl,
    recorded: list[QuotaExceeded],
    cap: int,
    refused: bool,
) -> None:
    """Usage that grew between prepare and commit counts at commit, where the metadata swap makes the growth live."""
    compose = seed_live_compose_context(db_engine, session_id)
    expected = await blob_service.create_blob(session_id, "grow.csv", b"abc", "text/csv", session_operation_context=compose)
    replacement = replace(expected, size_bytes=14, content_hash="b" * 64)
    _seed_identity_cap(db_engine, storage_bytes=cap)
    _occupy(db_engine, size_bytes=87)
    authority = blob_service._session_operation_authority
    plan = authority.mutate(
        compose,
        lambda transaction: transaction.blobs.prepare_blob_replacement(
            replacement_id=uuid4(),
            expected=expected,
            replacement=replacement,
            staging_path=f"{expected.storage_path}.quota.stage",
            backup_path=f"{expected.storage_path}.quota.backup",
            max_storage_per_session=10_000,
            accepting_proposal_id=None,
        ),
    )
    plan = authority.mutate(compose, lambda transaction: transaction.blobs.mark_blob_replacement_staged(plan=plan))
    _occupy(db_engine, size_bytes=1)

    def _commit(transaction):
        return transaction.blobs.commit_blob_replacement(plan=plan, max_storage_per_session=10_000, accepting_proposal_id=None)

    if refused:
        with pytest.raises(
            IdentityStorageQuotaExceededError, match=r"\(91 bytes\) plus 11 bytes would exceed its storage quota \(101 bytes\)"
        ):
            authority.mutate(compose, _commit)
        assert _session_rows(db_engine, session_id) == [("ready", 3)]
        assert _recorded(recorded) == [(IDENTITY, "blob_replacement", "storage", 91, 101, None)]
    else:
        assert authority.mutate(compose, _commit).phase == "purge_pending"
        assert _session_rows(db_engine, session_id) == [("ready", 14)]
        assert recorded == []


def test_blob_service_default_authority_carries_the_recorder(db_engine: Engine, tmp_path: Path, recorded: list[QuotaExceeded]) -> None:
    """A service built without an explicit authority must not build one that silently fails closed on every refusal."""
    service = BlobServiceImpl(db_engine, tmp_path, quota_exceeded_recorder=recorded.append)
    assert service._session_operation_authority._quota_exceeded_recorder == recorded.append
    assert service._quota_exceeded_recorder == recorded.append


def test_count_of_session_rows_helper_sees_rows(db_engine: Engine, session_id: UUID) -> None:
    """Positive control for ``_session_rows``: an instrument returning [] for every session would pass every refusal above."""
    with db_engine.begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id=str(uuid4()),
                session_id=str(session_id),
                filename="control.csv",
                mime_type="text/csv",
                size_bytes=5,
                content_hash="f" * 64,
                storage_path="/nonexistent/control.csv",
                created_at=NOW,
                created_by="user",
                source_description=None,
                status="ready",
            )
        )
        assert conn.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 1
        conn.execute(update(blobs_table).values(status="error"))
    assert _session_rows(db_engine, session_id) == [("error", 5)]
```

- [ ] **Step 7: Run the site tests to verify they fail.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/blobs/test_storage_quota_sites.py -n 0 > /tmp/i-lane-i2-step7.log 2>&1; echo exit=$?
```

Expected: `exit=1`, `7 failed, 1 passed, 16 errors`. The only pass is the instrument control `test_count_of_session_rows_helper_sees_rows`. Every other test stops on one of three messages: `TypeError: SQLiteLocalSessionOperationAuthority.__init__() got an unexpected keyword argument 'quota_exceeded_recorder'` (20 tests), `TypeError: persist_inline_custody_blob_on_connection() got an unexpected keyword argument 'quota_exceeded_recorder'` (2) or `TypeError: BlobServiceImpl.__init__() got an unexpected keyword argument 'quota_exceeded_recorder'` (1).

- [ ] **Step 8: Wire the recorder and admit R13 at every site.**

`src/elspeth/web/coordination/repository.py` HEAD lines 73-76 read:

```python
    _resolve_mutation_connection,
    _unregister_mutation_connection,
)
from elspeth.web.coordination.run_start_permit_authority import RepositoryRunStartPermitAuthority
```

Replace them with:

```python
    _resolve_mutation_connection,
    _unregister_mutation_connection,
)
from elspeth.web.coordination.quota_authority import QuotaExceeded, RepositoryQuotaAuthority, refuse_unrecorded_quota_exceeded
from elspeth.web.coordination.run_start_permit_authority import RepositoryRunStartPermitAuthority
```

This adds no import cycle: `repository.py:59` already imports `chargeable_admission_authority`, which imports `quota_authority` after I1.

`src/elspeth/web/coordination/repository.py` HEAD lines 499-512 read:

```python
    __slots__ = ("_connection_token", "_database_now", "_operation_context", "_session_id")

    def __init__(
        self,
        connection: Connection,
        *,
        session_id: str,
        database_now: datetime,
        operation_context: SessionOperationContext | None = None,
    ) -> None:
        self._connection_token = _register_mutation_connection(connection)
        self._session_id = session_id
        self._database_now = database_now
        self._operation_context = operation_context
```

Replace them with:

```python
    __slots__ = ("_connection_token", "_database_now", "_operation_context", "_quota_exceeded_recorder", "_session_id")

    def __init__(
        self,
        connection: Connection,
        *,
        session_id: str,
        database_now: datetime,
        operation_context: SessionOperationContext | None = None,
        quota_exceeded_recorder: Callable[[QuotaExceeded], None] = refuse_unrecorded_quota_exceeded,
    ) -> None:
        self._connection_token = _register_mutation_connection(connection)
        self._session_id = session_id
        self._database_now = database_now
        self._operation_context = operation_context
        # R13 (Task I2): the blob facets hand a storage refusal to this recorder
        # before it leaves the transaction, so it is audited before any caller sees it.
        self._quota_exceeded_recorder = quota_exceeded_recorder
```

`src/elspeth/web/coordination/repository.py` HEAD lines 2215-2218 (`prepare_blob_replacement`) read:

```python
        if current_total - actual.size_bytes + replacement.size_bytes > max_storage_per_session:
            from elspeth.contracts.blobs import BlobQuotaExceededError

            raise BlobQuotaExceededError(state._session_id, current_bytes=current_total, limit_bytes=max_storage_per_session)
```

Replace them with:

```python
        if current_total - actual.size_bytes + replacement.size_bytes > max_storage_per_session:
            from elspeth.contracts.blobs import BlobQuotaExceededError

            raise BlobQuotaExceededError(state._session_id, current_bytes=current_total, limit_bytes=max_storage_per_session)
        RepositoryQuotaAuthority.admit_storage_bytes(
            state._connection_token,
            session_id=state._session_id,
            additional_bytes=replacement.size_bytes - actual.size_bytes,
            operation="blob_replacement",
            record=state._quota_exceeded_recorder,
        )
```

`src/elspeth/web/coordination/repository.py` HEAD lines 2367-2370 (`commit_blob_replacement`) read:

```python
        if current_total - actual.size_bytes + exact.replacement_blob.size_bytes > max_storage_per_session:
            from elspeth.contracts.blobs import BlobQuotaExceededError

            raise BlobQuotaExceededError(state._session_id, current_bytes=current_total, limit_bytes=max_storage_per_session)
```

Replace them with:

```python
        if current_total - actual.size_bytes + exact.replacement_blob.size_bytes > max_storage_per_session:
            from elspeth.contracts.blobs import BlobQuotaExceededError

            raise BlobQuotaExceededError(state._session_id, current_bytes=current_total, limit_bytes=max_storage_per_session)
        RepositoryQuotaAuthority.admit_storage_bytes(
            state._connection_token,
            session_id=state._session_id,
            additional_bytes=exact.replacement_blob.size_bytes - actual.size_bytes,
            operation="blob_replacement",
            record=state._quota_exceeded_recorder,
        )
```

`src/elspeth/web/coordination/repository.py` HEAD lines 2544-2552 (`finalize_pending_output_blob`, the `if` is indented 12 spaces; `row` is the facet's `state._require_blob(blob_id)` read at its top) read:

```python
            if current_total + size_bytes > max_storage_per_session:
                from elspeth.contracts.blobs import BlobQuotaExceededError

                raise BlobQuotaExceededError(
                    state._session_id,
                    current_bytes=current_total,
                    limit_bytes=max_storage_per_session,
                )
        result = connection.execute(
```

Replace them with:

```python
            if current_total + size_bytes > max_storage_per_session:
                from elspeth.contracts.blobs import BlobQuotaExceededError

                raise BlobQuotaExceededError(
                    state._session_id,
                    current_bytes=current_total,
                    limit_bytes=max_storage_per_session,
                )
            RepositoryQuotaAuthority.admit_storage_bytes(
                state._connection_token,
                session_id=state._session_id,
                additional_bytes=size_bytes - row.size_bytes,
                operation="run_output_finalize",
                record=state._quota_exceeded_recorder,
            )
        result = connection.execute(
```

`src/elspeth/web/coordination/repository.py` HEAD lines 2658-2666 (`reserve_blob`) read:

```python
        if current_total + record.size_bytes > max_storage_per_session:
            from elspeth.contracts.blobs import BlobQuotaExceededError

            raise BlobQuotaExceededError(
                state._session_id,
                current_bytes=current_total,
                limit_bytes=max_storage_per_session,
            )
        connection.execute(
```

Replace them with:

```python
        if current_total + record.size_bytes > max_storage_per_session:
            from elspeth.contracts.blobs import BlobQuotaExceededError

            raise BlobQuotaExceededError(
                state._session_id,
                current_bytes=current_total,
                limit_bytes=max_storage_per_session,
            )
        RepositoryQuotaAuthority.admit_storage_bytes(
            state._connection_token,
            session_id=state._session_id,
            additional_bytes=record.size_bytes,
            operation="blob_create",
            record=state._quota_exceeded_recorder,
        )
        connection.execute(
```

`src/elspeth/web/coordination/repository.py` HEAD lines 3612-3620 (`mark_run_output_blob_ready`, the `if` is indented 8 spaces) read:

```python
        if current_total + size_bytes > max_storage_per_session:
            from elspeth.contracts.blobs import BlobQuotaExceededError

            raise BlobQuotaExceededError(
                state._session_id,
                current_bytes=current_total,
                limit_bytes=max_storage_per_session,
            )
        result = connection.execute(
```

Replace them with:

```python
        if current_total + size_bytes > max_storage_per_session:
            from elspeth.contracts.blobs import BlobQuotaExceededError

            raise BlobQuotaExceededError(
                state._session_id,
                current_bytes=current_total,
                limit_bytes=max_storage_per_session,
            )
        RepositoryQuotaAuthority.admit_storage_bytes(
            state._connection_token,
            session_id=state._session_id,
            additional_bytes=size_bytes - state._require_blob(blob_id).size_bytes,
            operation="run_output_finalize",
            record=state._quota_exceeded_recorder,
        )
        result = connection.execute(
```

`src/elspeth/web/coordination/repository.py` HEAD lines 3753-3762 (`_RepositoryMutationTransaction.__init__`) read:

```python
        session_id: str,
        database_now: datetime,
        operation_context: SessionOperationContext | None = None,
    ) -> None:
        state = _RepositoryMutationState(
            connection,
            session_id=session_id,
            database_now=database_now,
            operation_context=operation_context,
        )
```

Replace them with:

```python
        session_id: str,
        database_now: datetime,
        operation_context: SessionOperationContext | None = None,
        quota_exceeded_recorder: Callable[[QuotaExceeded], None] = refuse_unrecorded_quota_exceeded,
    ) -> None:
        state = _RepositoryMutationState(
            connection,
            session_id=session_id,
            database_now=database_now,
            operation_context=operation_context,
            quota_exceeded_recorder=quota_exceeded_recorder,
        )
```

`src/elspeth/web/coordination/repository.py` HEAD lines 4495-4496 (`_SessionOperationAuthorityRepository.__init__`) read:

```python
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
```

Replace them with:

```python
    def __init__(
        self,
        engine: Engine,
        *,
        quota_exceeded_recorder: Callable[[QuotaExceeded], None] = refuse_unrecorded_quota_exceeded,
    ) -> None:
        self._engine = engine
        self._quota_exceeded_recorder = quota_exceeded_recorder
```

`src/elspeth/web/coordination/repository.py` HEAD lines 5139-5144 (`mutate`) read:

```python
            transaction = _RepositoryMutationTransaction(
                conn,
                session_id=fence.session_id,
                database_now=database_now,
                operation_context=context,
            )
```

Replace them with:

```python
            transaction = _RepositoryMutationTransaction(
                conn,
                session_id=fence.session_id,
                database_now=database_now,
                operation_context=context,
                quota_exceeded_recorder=self._quota_exceeded_recorder,
            )
```

`src/elspeth/web/coordination/repository.py` HEAD lines 5620-5623 (`PostgresSessionOperationRepository.__init__`) read:

```python
    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgresSessionOperationRepository requires PostgreSQL")
        super().__init__(engine)
```

Replace them with:

```python
    def __init__(
        self,
        engine: Engine,
        *,
        quota_exceeded_recorder: Callable[[QuotaExceeded], None] = refuse_unrecorded_quota_exceeded,
    ) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgresSessionOperationRepository requires PostgreSQL")
        super().__init__(engine, quota_exceeded_recorder=quota_exceeded_recorder)
```

`src/elspeth/web/coordination/sqlite_authority.py` HEAD line 11 reads `from elspeth.web.coordination.repository import _SessionOperationAuthorityRepository`. Insert this line directly above it:

```python
from elspeth.web.coordination.quota_authority import QuotaExceeded, refuse_unrecorded_quota_exceeded
```

`src/elspeth/web/coordination/sqlite_authority.py` HEAD lines 14-15 read:

```python
if TYPE_CHECKING:
    from collections.abc import Iterator
```

Replace them with:

```python
if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
```

`src/elspeth/web/coordination/sqlite_authority.py` HEAD lines 27-30 read:

```python
    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "sqlite":
            raise ValueError("SQLiteLocalSessionOperationAuthority requires SQLite")
        super().__init__(engine)
```

Replace them with:

```python
    def __init__(
        self,
        engine: Engine,
        *,
        quota_exceeded_recorder: Callable[[QuotaExceeded], None] = refuse_unrecorded_quota_exceeded,
    ) -> None:
        if engine.dialect.name != "sqlite":
            raise ValueError("SQLiteLocalSessionOperationAuthority requires SQLite")
        super().__init__(engine, quota_exceeded_recorder=quota_exceeded_recorder)
```

`src/elspeth/web/blobs/service.py` HEAD lines 67-68 read:

```python
from elspeth.web.composer.yaml_generator import LoweredPipelineDocument
from elspeth.web.sessions.converters import pipeline_dict_from_record
```

Replace them with:

```python
from elspeth.web.composer.yaml_generator import LoweredPipelineDocument
from elspeth.web.coordination.quota_authority import (
    QuotaExceeded,
    StorageAdmissionOperation,
    admit_storage_bytes_on_connection,
    refuse_unrecorded_quota_exceeded,
)
from elspeth.web.sessions.converters import pipeline_dict_from_record
```

`src/elspeth/web/blobs/service.py` HEAD lines 1310-1318 (`_enforce_session_blob_quota` signature and docstring) read:

```python
    additional_bytes: int,
    max_storage_per_session: int,
    exclude_blob_id: str | None = None,
) -> None:
    """Raise ``BlobQuotaExceededError`` if adding bytes would exceed the session ceiling.

    Runs against the caller's connection inside the caller's transaction and
    quota lock; it owns no locking or commit protocol of its own.
    """
```

Replace them with:

```python
    additional_bytes: int,
    max_storage_per_session: int,
    operation: StorageAdmissionOperation,
    quota_exceeded_recorder: Callable[[QuotaExceeded], None],
    exclude_blob_id: str | None = None,
) -> None:
    """Raise ``BlobQuotaExceededError`` if adding bytes would exceed the session ceiling, then admit R13.

    Runs against the caller's connection inside the caller's transaction and
    quota lock; it owns no locking or commit protocol of its own. After the
    per-session bound, the session owner's identity bound (R13, Task I2) is
    admitted on the same connection.
    """
```

`src/elspeth/web/blobs/service.py` HEAD lines 1327-1335 read:

```python
    if current_total + additional_bytes > max_storage_per_session:
        raise BlobQuotaExceededError(
            session_id,
            current_bytes=current_total,
            limit_bytes=max_storage_per_session,
        )


def _insert_pending_blob_row(
```

Replace them with:

```python
    if current_total + additional_bytes > max_storage_per_session:
        raise BlobQuotaExceededError(
            session_id,
            current_bytes=current_total,
            limit_bytes=max_storage_per_session,
        )
    admit_storage_bytes_on_connection(
        conn,
        session_id=session_id,
        additional_bytes=additional_bytes,
        operation=operation,
        record=quota_exceeded_recorder,
    )


def _insert_pending_blob_row(
```

`src/elspeth/web/blobs/service.py` HEAD lines 1380-1382 (`_reserve_pending_blob` signature) read:

```python
    fork_write_fence: BlobForkWriteFence | None,
    guided_operation_write_fence: BlobGuidedOperationWriteFence | None,
) -> tuple[Row[Any], bool]:
```

Replace them with:

```python
    fork_write_fence: BlobForkWriteFence | None,
    guided_operation_write_fence: BlobGuidedOperationWriteFence | None,
    quota_exceeded_recorder: Callable[[QuotaExceeded], None],
) -> tuple[Row[Any], bool]:
```

`src/elspeth/web/blobs/service.py` HEAD lines 1395-1401 read (`_reserve_pending_blob` is reached only on the composite fork path; the session-operation path returns at :1633):

```python
            _enforce_session_blob_quota(
                conn,
                session_id,
                additional_bytes=expected["size_bytes"],
                max_storage_per_session=max_storage_per_session,
            )
            try:
```

Replace them with:

```python
            _enforce_session_blob_quota(
                conn,
                session_id,
                additional_bytes=expected["size_bytes"],
                max_storage_per_session=max_storage_per_session,
                operation="session_fork",
                quota_exceeded_recorder=quota_exceeded_recorder,
            )
            try:
```

`src/elspeth/web/blobs/service.py` HEAD lines 1513-1515 (`_persist_blob_content` signature) read:

```python
    session_operation_authority: SessionOperationAuthority | None = None,
    session_operation_context: SessionOperationContext | None = None,
) -> Row[Any] | BlobRecord:
```

Replace them with:

```python
    session_operation_authority: SessionOperationAuthority | None = None,
    session_operation_context: SessionOperationContext | None = None,
    quota_exceeded_recorder: Callable[[QuotaExceeded], None] = refuse_unrecorded_quota_exceeded,
) -> Row[Any] | BlobRecord:
```

`src/elspeth/web/blobs/service.py` HEAD lines 1619-1624 read:

```python
        service = BlobServiceImpl(
            engine,
            data_dir,
            max_storage_per_session,
            session_operation_authority=session_operation_authority,
        )
```

Replace them with:

```python
        service = BlobServiceImpl(
            engine,
            data_dir,
            max_storage_per_session,
            session_operation_authority=session_operation_authority,
            quota_exceeded_recorder=quota_exceeded_recorder,
        )
```

`src/elspeth/web/blobs/service.py` HEAD lines 1646-1649 read:

```python
                fork_write_fence=fork_write_fence,
                guided_operation_write_fence=guided_operation_write_fence,
            )
            storage_existed_before_write = storage.exists()
```

Replace them with:

```python
                fork_write_fence=fork_write_fence,
                guided_operation_write_fence=guided_operation_write_fence,
                quota_exceeded_recorder=quota_exceeded_recorder,
            )
            storage_existed_before_write = storage.exists()
```

`src/elspeth/web/blobs/service.py` HEAD lines 2022-2024 (`persist_inline_custody_blob_on_connection` signature) read:

```python
    max_storage_per_session: int,
    write_fence: BlobGuidedOperationWriteFence | None,
) -> tuple[Row[Any], InlineCustodyPublication]:
```

Replace them with:

```python
    max_storage_per_session: int,
    write_fence: BlobGuidedOperationWriteFence | None,
    quota_exceeded_recorder: Callable[[QuotaExceeded], None] = refuse_unrecorded_quota_exceeded,
) -> tuple[Row[Any], InlineCustodyPublication]:
```

`src/elspeth/web/blobs/service.py` HEAD lines 2070-2076 read:

```python
        _enforce_session_blob_quota(
            conn,
            session_id,
            additional_bytes=expected["size_bytes"],
            max_storage_per_session=max_storage_per_session,
        )
        _insert_pending_blob_row(conn, blob_id=blob_id, storage=storage, expected=expected)
```

Replace them with:

```python
        _enforce_session_blob_quota(
            conn,
            session_id,
            additional_bytes=expected["size_bytes"],
            max_storage_per_session=max_storage_per_session,
            operation="inline_custody",
            quota_exceeded_recorder=quota_exceeded_recorder,
        )
        _insert_pending_blob_row(conn, blob_id=blob_id, storage=storage, expected=expected)
```

`src/elspeth/web/blobs/service.py` HEAD lines 2431-2436 (`BlobServiceImpl.__init__`) read:

```python
        *,
        session_operation_authority: SessionOperationAuthority | None = None,
    ) -> None:
        self._engine = engine
        self._data_dir = data_dir.expanduser().resolve()
        self._max_storage_per_session = max_storage_per_session
```

Replace them with:

```python
        *,
        session_operation_authority: SessionOperationAuthority | None = None,
        quota_exceeded_recorder: Callable[[QuotaExceeded], None] = refuse_unrecorded_quota_exceeded,
    ) -> None:
        self._engine = engine
        self._data_dir = data_dir.expanduser().resolve()
        self._max_storage_per_session = max_storage_per_session
        # R13 (Task I2): the fork's pre-copy and per-copy checks run on raw
        # connections, so they audit through this recorder; every other site
        # records through the session authority's own recorder.
        self._quota_exceeded_recorder = quota_exceeded_recorder
```

`src/elspeth/web/blobs/service.py` HEAD line 2444 reads `                session_operation_authority = SQLiteLocalSessionOperationAuthority(engine)`. Replace it with:

```python
                session_operation_authority = SQLiteLocalSessionOperationAuthority(engine, quota_exceeded_recorder=quota_exceeded_recorder)
```

`src/elspeth/web/blobs/service.py` HEAD line 2448 reads `                session_operation_authority = PostgresSessionOperationRepository(engine)`. Replace it with:

```python
                session_operation_authority = PostgresSessionOperationRepository(engine, quota_exceeded_recorder=quota_exceeded_recorder)
```

`src/elspeth/web/blobs/service.py` HEAD lines 4009-4014 (the fork pre-copy check) read:

```python
                    _enforce_session_blob_quota(
                        conn,
                        target_session_id_str,
                        additional_bytes=missing_bytes,
                        max_storage_per_session=self._max_storage_per_session,
                    )
```

Replace them with:

```python
                    _enforce_session_blob_quota(
                        conn,
                        target_session_id_str,
                        additional_bytes=missing_bytes,
                        max_storage_per_session=self._max_storage_per_session,
                        operation="session_fork",
                        quota_exceeded_recorder=self._quota_exceeded_recorder,
                    )
```

`src/elspeth/web/blobs/service.py` HEAD lines 4076-4079 (`_persist_copy`) read:

```python
                    idempotent=True,
                    fork_write_fence=write_fence,
                    write_guard=authority.require,
                )
```

Replace them with:

```python
                    idempotent=True,
                    fork_write_fence=write_fence,
                    write_guard=authority.require,
                    quota_exceeded_recorder=self._quota_exceeded_recorder,
                )
```

`src/elspeth/web/composer/pipeline_custody.py` HEAD line 13 reads `from collections.abc import Iterator, Mapping`. Replace it with:

```python
from collections.abc import Callable, Iterator, Mapping
```

`src/elspeth/web/composer/pipeline_custody.py` HEAD line 55 reads `from elspeth.web.sessions.locking import _run_lock_cleanup`. Insert this line directly above it:

```python
from elspeth.web.coordination.quota_authority import QuotaExceeded, refuse_unrecorded_quota_exceeded
```

`src/elspeth/web/composer/pipeline_custody.py` HEAD lines 283-285 (`finalize_pipeline_custody_on_connection` signature) read:

```python
    max_storage_per_session: int,
    write_fence: BlobGuidedOperationWriteFence | None,
) -> InlineCustodyPublication:
```

Replace them with:

```python
    max_storage_per_session: int,
    write_fence: BlobGuidedOperationWriteFence | None,
    quota_exceeded_recorder: Callable[[QuotaExceeded], None] = refuse_unrecorded_quota_exceeded,
) -> InlineCustodyPublication:
```

`src/elspeth/web/composer/pipeline_custody.py` HEAD lines 305-310 read:

```python
    row, publication = persist_inline_custody_blob_on_connection(
        conn,
        staged=staged,
        max_storage_per_session=max_storage_per_session,
        write_fence=write_fence,
    )
```

Replace them with:

```python
    row, publication = persist_inline_custody_blob_on_connection(
        conn,
        staged=staged,
        max_storage_per_session=max_storage_per_session,
        write_fence=write_fence,
        quota_exceeded_recorder=quota_exceeded_recorder,
    )
```

`src/elspeth/web/sessions/service.py` after I1, line 4450 reads `                session_operation_authority = SQLiteLocalSessionOperationAuthority(engine)`. Replace it with:

```python
                session_operation_authority = SQLiteLocalSessionOperationAuthority(engine, quota_exceeded_recorder=quota_exceeded_recorder)
```

`src/elspeth/web/sessions/service.py` after I1, line 4452 reads `                session_operation_authority = PostgresSessionOperationRepository(engine)`. Replace it with:

```python
                session_operation_authority = PostgresSessionOperationRepository(engine, quota_exceeded_recorder=quota_exceeded_recorder)
```

`quota_exceeded_recorder` there is I1's `SessionServiceImpl.__init__` parameter (after I1 :4422).

`src/elspeth/web/sessions/service.py` after I1, lines 11715-11716 (inside the `finalize_pipeline_custody_on_connection(` call of the guided-full settlement) read:

```python
                        staged=staged_custody,
                        max_storage_per_session=command.custody_max_storage_per_session,
```

Replace them with:

```python
                        staged=staged_custody,
                        max_storage_per_session=command.custody_max_storage_per_session,
                        quota_exceeded_recorder=self._quota_exceeded_recorder,
```

`src/elspeth/web/blobs/routes.py` HEAD lines 37-39 read:

```python
    BlobStateError,
    StorageMimeType,
)
```

Replace them with:

```python
    BlobStateError,
    IdentityStorageQuotaExceededError,
    StorageAccountingUnavailableError,
    StorageMimeType,
)
```

`src/elspeth/web/blobs/routes.py` HEAD lines 254-255 (`create_blob_upload`) and 317-318 (`create_blob_inline`) both read:

```python
            except BlobQuotaExceededError as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from None
```

Replace both occurrences with:

```python
            except IdentityStorageQuotaExceededError as exc:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "error_type": "storage_quota_exceeded",
                        "detail": str(exc),
                        "dimension": "storage",
                        "cap": exc.cap,
                        "ceiling": exc.ceiling,
                        "usage": exc.usage,
                    },
                ) from None
            except StorageAccountingUnavailableError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from None
            except BlobQuotaExceededError as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from None
```

The subclass arm must precede the base arm, or the base arm catches the identity refusal with the per-session string body.

- [ ] **Step 9: Run the site and authority tests.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/blobs/test_storage_quota_sites.py tests/unit/web/coordination/test_storage_quota_authority.py -n 0 > /tmp/i-lane-i2-step9.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `39 passed`.

- [ ] **Step 10: Write the failing app-wiring test and the storage audit-row pin.** The app test proves `create_app` wires one recorder into the shared session authority and the blob service. The audit test reads the LANDSCAPE engine, because `auth_events` is a Landscape table (`core/landscape/schema.py:2542`), and pins the storage row I1's writer already produces.

`tests/unit/web/test_app.py` HEAD lines 411-415 read:

```python
    def test_settings_stored_on_app_state(self, tmp_path) -> None:
        settings = _settings(tmp_path, port=9999)
        app = create_app(settings)
        assert app.state.settings is settings
        assert app.state.settings.port == 9999
```

Replace them with:

```python
    def test_settings_stored_on_app_state(self, tmp_path) -> None:
        settings = _settings(tmp_path, port=9999)
        app = create_app(settings)
        assert app.state.settings is settings
        assert app.state.settings.port == 9999

    def test_storage_quota_refusals_record_through_the_auth_audit_recorder(self, tmp_path) -> None:
        """R13 (Task I2): the shared session authority and the blob service audit a storage refusal, never fail closed unwired."""
        app = create_app(_settings(tmp_path))
        record = app.state.auth_audit_recorder.record_quota_exceeded
        assert app.state.blob_service._quota_exceeded_recorder == record
        assert app.state.blob_service._session_operation_authority._quota_exceeded_recorder == record
        assert app.state.session_service.session_operation_authority is app.state.blob_service._session_operation_authority
```

`tests/unit/web/auth/test_audit.py` after I1, lines 1091-1093 (the end of `test_quota_exceeded_row_carries_dimension_cap_ceiling_and_usage`) read:

```python
        "identity_policy_id": "quota-identity",
        "container_policy_id": "quota-container",
    }
```

Replace them with:

```python
        "identity_policy_id": "quota-identity",
        "container_policy_id": "quota-container",
    }


def test_storage_quota_exceeded_row_names_the_storage_dimension(tmp_path: Any) -> None:
    """R13 (spec :834, :1295-1297): a storage refusal's row carries dimension=storage, the cap, the ceiling and the usage.

    Read from the LANDSCAPE engine: ``auth_events`` is a Landscape table
    (core/landscape/schema.py:2542), never a sessions table.
    """
    from elspeth.web.coordination.quota_authority import QuotaExceeded

    recorder, url = _durable_recorder(tmp_path)
    recorder.record_quota_exceeded(
        QuotaExceeded(
            identity_id="identity-1",
            provider="oidc",
            operation="session_fork",
            dimension="storage",
            cap=100,
            ceiling=None,
            usage=90,
            identity_policy_id="storage-identity",
            container_policy_id=None,
        )
    )
    (row,) = _durable_rows(url)
    assert (row.event_type, row.outcome, row.provider, row.identity_id, row.failure_category) == (
        "quota_exceeded",
        "failure",
        "oidc",
        "identity-1",
        "quota_exceeded_storage",
    )
    assert _metadata(row) == {
        "actor": "system",
        "operation": "session_fork",
        "dimension": "storage",
        "cap": 100,
        "ceiling": None,
        "usage": 90,
        "identity_policy_id": "storage-identity",
        "container_policy_id": None,
    }
```

- [ ] **Step 11: Run the two tests; the app test must fail.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest "tests/unit/web/test_app.py::TestCreateApp::test_storage_quota_refusals_record_through_the_auth_audit_recorder" tests/unit/web/auth/test_audit.py::test_storage_quota_exceeded_row_names_the_storage_dimension -n 0 > /tmp/i-lane-i2-step11.log 2>&1; echo exit=$?
```

Expected: `exit=1`, `1 failed, 1 passed`. The app test fails with `AssertionError: assert <function refuse_unrecorded_quota_exceeded at 0x744c13b84ae0> == record_quota_exceeded`; the address differs per run, and the right-hand side is the recorder's bound method. `web/app.py` still builds the blob service with the fail-closed default. The audit pin passes on this first run because I1's `record_quota_exceeded` is dimension-generic. It exists so that a later change to that writer cannot silently drop the storage metadata.

- [ ] **Step 12: Wire the auth audit recorder in `create_app`.**

`src/elspeth/web/app.py` HEAD line 1580 reads `        session_operation_authority = SQLiteLocalSessionOperationAuthority(session_engine)`. Replace it with:

```python
        session_operation_authority = SQLiteLocalSessionOperationAuthority(
            session_engine, quota_exceeded_recorder=audit_recorder.record_quota_exceeded
        )
```

`src/elspeth/web/app.py` HEAD line 1582 reads `        session_operation_authority = PostgresSessionOperationRepository(session_engine)`. Replace it with:

```python
        session_operation_authority = PostgresSessionOperationRepository(
            session_engine, quota_exceeded_recorder=audit_recorder.record_quota_exceeded
        )
```

`src/elspeth/web/app.py` HEAD lines 1596-1598 (the `app.state.blob_service = BlobServiceImpl(` call that opens at :1593) read:

```python
        settings.max_blob_storage_per_session_bytes,
        session_operation_authority=session_operation_authority,
    )
```

Replace them with:

```python
        settings.max_blob_storage_per_session_bytes,
        session_operation_authority=session_operation_authority,
        # R13 storage refusals write their Landscape quota_exceeded row through
        # the same auth audit engine as R14 (Task I2).
        quota_exceeded_recorder=audit_recorder.record_quota_exceeded,
    )
```

`audit_recorder` is bound at `app.py:1536` (`AuthAuditRecorder.from_settings(settings, resolved_state_mode)`), before both uses. I1's hunk at `app.py:1671-1676` lies below this edit and is unaffected.

- [ ] **Step 13: Run the two tests again.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest "tests/unit/web/test_app.py::TestCreateApp::test_storage_quota_refusals_record_through_the_auth_audit_recorder" tests/unit/web/auth/test_audit.py::test_storage_quota_exceeded_row_names_the_storage_dimension -n 0 > /tmp/i-lane-i2-step13.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `2 passed`.

- [ ] **Step 14: Run the blob route gate; it must report the changed routes, then re-pin its three digests.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/contracts/test_web_blob_fencing.py -n 0 > /tmp/i-lane-i2-step14.log 2>&1; echo exit=$?
```

Expected: `exit=1`, `4 failed, 200 passed`. The failing tests are:
- `test_production_gate_is_fillable_by_canonical_functional_delete_tail`
- `test_route_gate_rejects_open_import_and_factory_identity_surfaces[renamed_factory-require exactly one top-level create_blobs_router factory]`
- `test_route_gate_rejects_open_import_and_factory_identity_surfaces[changed_factory_signature-signature must match exact canonical factory identity]`
- `test_standalone_blob_routes_bind_the_exact_renewable_lease`

The violations name `standalone blob routes require an exact closed canonical import inventory`, `create_blob_upload: post-acquisition statements must match the exact approved fenced tail` and `create_blob_inline: post-acquisition statements must match the exact approved fenced tail`.

The gate hashes `stable_ast_dump` of the module's import statements (`_import_surface_violations`, `test_web_blob_fencing.py:798-806`) and of each endpoint's statements after the lease acquisition (:1060-1066). The new import names and the two except arms change exactly those three digests. The preamble digests, `_ALLOWED_ROUTE_CALLABLES` (the arms call only `HTTPException` and `str`) and the top-level definition inventory are unchanged. The new values were computed from the Step 8 `routes.py` with the gate's own `stable_ast_dump` and `_is_acquisition_like_call`.

`tests/unit/contracts/test_web_blob_fencing.py` HEAD line 315 reads:

```python
_PRODUCTION_IMPORTS_AST_SHA256 = "ddba86cd52908d2d48352875cce8e6391dd9e343bc362998ac6059461cb177d8"
```

Replace it with:

```python
_PRODUCTION_IMPORTS_AST_SHA256 = "7edde4d3577c43fa2859b5284663a6a27b17ba98d5a4f4a9e6a6c68ea862908d"
```

`tests/unit/contracts/test_web_blob_fencing.py` HEAD lines 325-327 read:

```python
_PRODUCTION_ENDPOINT_POST_ACQUIRE_AST_SHA256 = {
    "create_blob_upload": "f0e2e38eef879ccb4cf32ce6ae44e0d55d987f61e5ba652dc26a356dca915b84",
    "create_blob_inline": "06259ae65e2a1df9b94e5335d754fec157a722be17c7056715817104c0db2c0f",
```

Replace them with:

```python
_PRODUCTION_ENDPOINT_POST_ACQUIRE_AST_SHA256 = {
    "create_blob_upload": "048d344a7d6939fc9ba4163f40b3cbc8356dabea3489180fd06322c36e95a035",
    "create_blob_inline": "815f01692e5ac0dd77fae4ca3dcfb7caeb0c33995dfb2797c7e1e49ff5ebad8a",
```

These two hunks do not overlap I0's edit to the same file (`:3196`, the epoch assertion).

- [ ] **Step 15: Run the route gate again.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/contracts/test_web_blob_fencing.py -n 0 > /tmp/i-lane-i2-step15.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `204 passed`.

- [ ] **Step 16: Re-pin the mutation-authority manifest.** The manifest is fail-closed and measured. On the tree after I1 the gate XFAILs with this baseline: `Unexpected/unreviewed (67)`, `Stale reviewed (0)`, `Connections outside exact contained authority (16)`, `Unresolved write executions (44)`, `Writers without a named authority (7)`, `Writers under the wrong table authority (0)`, `Stale reviewed read connections (0)`, `Stale non-Sessions connection classifications (0)`, `Invalid reviewed read connections (0)`.

Run the gate before re-pinning:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py -n 0 -rx > /tmp/i-lane-i2-step16-before.log 2>&1; echo exit=$?
```

Expected: `exit=1`, `18 failed, 247 passed, 1 xfailed`. The XFAIL text reports `Unexpected/unreviewed (181)`, `Stale reviewed (97)`, `Connections outside exact contained authority (33)` and `Stale reviewed read connections (17)`; the remaining counts equal the baseline. That XFAIL text prints only the first 80 unexpected and 40 stale rows (`test_session_db_mutation_authority.py`, the `pytest.xfail` call in `test_all_production_sessions_writers_are_reviewed_typed_authorities`), so the table below is the complete measured list, not the log.

No table policy, authority symbol or new writer row is added: `admit_storage_bytes_on_connection` executes only `SELECT`s, and it runs on connections the reviewed writers already hold. Each row below is one `WriterIdentity` call in `_REVIEWED_WRITERS` or `_REVIEWED_READ_CONNECTIONS`. A row is identified uniquely by its path, symbol, table, operation, ordinal, after-I1 fingerprint and after-I1 `line=`. Change its `line=` to the new value, and where the two fingerprint columns differ, replace the fingerprint string literal as well. Rows are written positionally or with `fingerprint=` / `ordinal=` / `line=` keywords; edit the value in whichever form the row uses.

| Collection | Path | Symbol | Table | Operation | Ordinal | After-I1 fingerprint | New fingerprint | After-I1 `line=` | New `line=` |
|---|---|---|---|---|---|---|---|---|---|
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/blobs/service.py` | `publish_inline_custody_publication` | `<sessions-write-connection>` | write_connection | 1 | `be04b66aa0fe7683` | `be04b66aa0fe7683` | 1811 | 1834 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/blobs/service.py` | `publish_inline_custody_publication_locked` | `<sessions-write-connection>` | write_connection | 1 | `ef84064b21eb8128` | `ef84064b21eb8128` | 1824 | 1847 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._persist_fenced_blob_record._guard` | `<sessions-write-connection>` | write_connection | 1 | `5c59fb0d4c71542d` | `5c59fb0d4c71542d` | 2735 | 2766 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._persist_fenced_blob_record._sync` | `<sessions-write-connection>` | write_connection | 1 | `2aa8c0978abe6388` | `2aa8c0978abe6388` | 2739 | 2770 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl.list_blobs._sync` | `<sessions-write-connection>` | write_connection | 1 | `4fa518a6c8ccdf63` | `4fa518a6c8ccdf63` | 2894 | 2925 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl.delete_blob._sync` | `<sessions-write-connection>` | write_connection | 1 | `ef5b2a4c7344b12b` | `ef5b2a4c7344b12b` | 3267 | 3298 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._locked_blob_row_for_read` | `<sessions-write-connection>` | write_connection | 1 | `cb5f0515f16e5953` | `cb5f0515f16e5953` | 3294 | 3325 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl.get_blob_run_links._sync` | `<sessions-write-connection>` | write_connection | 1 | `793b2531e21a4ec2` | `793b2531e21a4ec2` | 3548 | 3579 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl.finalize_run_output_blobs._sync` | `<sessions-write-connection>` | write_connection | 1 | `5fb1fac9efd87d60` | `5fb1fac9efd87d60` | 3592 | 3623 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._stage_output_blob_error_and_remove_bytes` | `<sessions-write-connection>` | write_connection | 1 | `990aa332b8773eb8` | `990aa332b8773eb8` | 3895 | 3926 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl.copy_blobs_for_fork._verify_plan_and_quota` | `<sessions-write-connection>` | write_connection | 1 | `175d222f809ec084` | `2bcac139e1737da2` | 3979 | 4010 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl.copy_blobs_for_fork._verify_exact_target` | `<sessions-write-connection>` | write_connection | 1 | `4536b6d6104a3575` | `4536b6d6104a3575` | 4096 | 4130 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/composer/pipeline_custody.py` | `staged_pipeline_custody._guard` | `<sessions-write-connection>` | write_connection | 1 | `82e1a7d77b34d3eb` | `82e1a7d77b34d3eb` | 347 | 350 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository._session_exists` | `<sessions-write-connection>` | write_connection | 1 | `f98bc6d74193a045` | `f98bc6d74193a045` | 4620 | 4668 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/sessions/service.py` | `SessionServiceImpl.list_pending_landscape_reconciliations._sync` | `<sessions-write-connection>` | write_connection | 1 | `af0aa9fb126f5b07` | `af0aa9fb126f5b07` | 13434 | 13435 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/sessions/service.py` | `SessionServiceImpl._session_principal_context._sync` | `<sessions-write-connection>` | write_connection | 1 | `3a0d4f58a95545b6` | `3a0d4f58a95545b6` | 14223 | 14224 |
| _REVIEWED_READ_CONNECTIONS | `src/elspeth/web/sessions/service.py` | `SessionServiceImpl.get_state_version_numbers._sync` | `<sessions-write-connection>` | write_connection | 1 | `79cbcb06fa877308` | `79cbcb06fa877308` | 14488 | 14489 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `_insert_pending_blob_row` | `blobs` | insert | 1 | `bb9d60d5e991d99d` | `bb9d60d5e991d99d` | 1348 | 1365 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `_reserve_pending_blob` | `<sessions-write-connection>` | write_connection | 1 | `ed8bc9f6e94ae399` | `bccea31ca8ab0074` | 1384 | 1402 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `_finalize_reserved_blob` | `<sessions-write-connection>` | write_connection | 1 | `5fcbd10db9a8bb6a` | `5fcbd10db9a8bb6a` | 1457 | 1477 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `_finalize_reserved_blob` | `blobs` | update | 1 | `38de196d17740a2c` | `38de196d17740a2c` | 1468 | 1488 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `_discard_nonidempotent_reservation` | `<sessions-write-connection>` | write_connection | 1 | `726caad0e48b0bcc` | `726caad0e48b0bcc` | 1485 | 1505 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `_discard_nonidempotent_reservation` | `blobs` | delete | 1 | `dfb9e7995e5fc483` | `dfb9e7995e5fc483` | 1487 | 1507 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `_persist_blob_content` | `<sessions-write-connection>` | write_connection | 1 | `af22910b21dfc633` | `e2b652e5b65abab9` | 1634 | 1656 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `reconcile_inline_custody_publications` | `<sessions-write-connection>` | write_connection | 1 | `c29cebfe705db1ba` | `c29cebfe705db1ba` | 1885 | 1908 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `reconcile_inline_custody_publications` | `<sessions-write-connection>` | write_connection | 2 | `c29cebfe705db1ba` | `c29cebfe705db1ba` | 1921 | 1944 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `persist_inline_custody_blob_on_connection` | `blobs` | update | 1 | `8269e201d89d7e63` | `8269e201d89d7e63` | 2081 | 2107 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._delete_fork_blob_row_locked` | `blob_deletion_cleanups` | insert | 1 | `16da07b191192600` | `16da07b191192600` | 4177 | 4211 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._delete_fork_blob_row_locked` | `blobs` | delete | 1 | `16da07b191192600` | `16da07b191192600` | 4187 | 4221 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._finalize_registered_fork_blob_deletion` | `<sessions-write-connection>` | write_connection | 1 | `1039c549bf5d962c` | `1039c549bf5d962c` | 4206 | 4240 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._finalize_registered_fork_blob_deletion` | `blob_deletion_cleanups` | delete | 1 | `9adc75e8d72e71ad` | `9adc75e8d72e71ad` | 4209 | 4243 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._cleanup_blobs_for_fork_sync` | `<sessions-write-connection>` | write_connection | 1 | `4ffe9086cd5b6d8f` | `4ffe9086cd5b6d8f` | 4233 | 4267 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._cleanup_blobs_for_fork_sync` | `<sessions-write-connection>` | write_connection | 2 | `4ffe9086cd5b6d8f` | `4ffe9086cd5b6d8f` | 4234 | 4268 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._cleanup_blobs_for_fork_sync` | `<sessions-write-connection>` | write_connection | 3 | `4ffe9086cd5b6d8f` | `4ffe9086cd5b6d8f` | 4277 | 4311 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._cleanup_blobs_for_fork_sync` | `<sessions-write-connection>` | write_connection | 5 | `4ffe9086cd5b6d8f` | `4ffe9086cd5b6d8f` | 4314 | 4348 |
| _REVIEWED_WRITERS | `src/elspeth/web/blobs/service.py` | `BlobServiceImpl._cleanup_blobs_for_fork_sync` | `<sessions-write-connection>` | write_connection | 4 | `4ffe9086cd5b6d8f` | `4ffe9086cd5b6d8f` | 4390 | 4424 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/quota_authority.py` | `record_token_usage_on_connection` | `token_usage_ledger` | insert | 1 | `311925fabbc6ca39` | `311925fabbc6ca39` | 198 | 200 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositorySessionMutations.record_plugin_crash_breadcrumb` | `sessions` | update | 1 | `016ef4c50b5e5390` | `016ef4c50b5e5390` | 590 | 595 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositorySessionMutations.mark_session_updated` | `sessions` | update | 1 | `44e58336446946af` | `44e58336446946af` | 616 | 621 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositorySessionMutations.record_composition_rejection` | `composition_rejection_events` | insert | 1 | `e0386cbdb277f0b0` | `e0386cbdb277f0b0` | 651 | 656 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositorySessionMutations.set_title` | `sessions` | update | 1 | `feed562da5394634` | `feed562da5394634` | 685 | 690 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositorySessionMutations.decide_and_soft_archive` | `sessions` | update | 1 | `2aec308084effcf4` | `2aec308084effcf4` | 777 | 782 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryCompositionStateMutations.append_state` | `composition_states` | insert | 1 | `8290255ca5f496f1` | `8290255ca5f496f1` | 837 | 842 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryInterpretationMutations.create_or_reconcile_pending` | `interpretation_events` | update | 1 | `b62dec99662793d2` | `b62dec99662793d2` | 1092 | 1097 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryInterpretationMutations.create_or_reconcile_pending` | `interpretation_events` | insert | 1 | `83a9e49f7b465443` | `83a9e49f7b465443` | 1169 | 1174 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryInterpretationMutations.create_or_reconcile_pending` | `interpretation_events` | insert | 1 | `c969a753999273dd` | `c969a753999273dd` | 1196 | 1201 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryInterpretationMutations.create_or_reconcile_pending` | `composition_states` | insert | 1 | `4e9d29a674a21658` | `4e9d29a674a21658` | 1246 | 1251 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryInterpretationMutations.record_session_opt_out` | `interpretation_events` | insert | 1 | `600ab798ca133699` | `600ab798ca133699` | 1316 | 1321 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryInterpretationMutations.record_session_opt_out` | `sessions` | update | 1 | `3966641511f4795d` | `3966641511f4795d` | 1343 | 1348 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryInterpretationMutations.record_auto_interpreted_no_surfaces_event` | `interpretation_events` | insert | 1 | `33b3cdac54fa6f56` | `33b3cdac54fa6f56` | 1374 | 1379 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryInterpretationMutations.resolve_pending_event` | `interpretation_events` | update | 1 | `9f581942e7ea4c5a` | `9f581942e7ea4c5a` | 1431 | 1436 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryRunMutations.create_pending_run` | `runs` | insert | 1 | `6e3fcabf86bb6ffa` | `6e3fcabf86bb6ffa` | 1511 | 1516 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryRunMutations.create_pending_run` | `run_execution_inputs` | insert | 1 | `46b7dd19a3e250fc` | `46b7dd19a3e250fc` | 1528 | 1533 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryRunMutations.create_pending_run` | `runs` | update | 1 | `46b7dd19a3e250fc` | `46b7dd19a3e250fc` | 1532 | 1537 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryRunMutations.complete_admission_refusal` | `runs` | update | 1 | `ab417310ef40b484` | `ab417310ef40b484` | 1602 | 1607 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryRunMutations.rebind_run_ownership` | `runs` | update | 1 | `911ff034cb757589` | `911ff034cb757589` | 1629 | 1634 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryRunMutations.mark_recovery_outputs_finalized` | `runs` | update | 1 | `1822dba79eb9ed3d` | `1822dba79eb9ed3d` | 1645 | 1650 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryRunMutations.mark_recovery_required` | `runs` | update | 1 | `6613984de3a26bff` | `6613984de3a26bff` | 1664 | 1669 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryRunMutations.transition_run_status` | `runs` | update | 1 | `f926157e24accee8` | `f926157e24accee8` | 1724 | 1729 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryRunMutations.append_run_event` | `run_events` | insert | 1 | `5009526a773c2d16` | `5009526a773c2d16` | 1823 | 1828 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.prepare_blob_replacement` | `blob_replacement_cleanups` | insert | 1 | `bca2ee74a8a93022` | `bca2ee74a8a93022` | 2255 | 2267 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.mark_blob_replacement_staged` | `blob_replacement_cleanups` | update | 1 | `6128417bd9a69f02` | `6128417bd9a69f02` | 2318 | 2330 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.commit_blob_replacement` | `blob_replacement_cleanups` | update | 1 | `2ecd759927d60392` | `2ecd759927d60392` | 2372 | 2391 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.commit_blob_replacement` | `blobs` | update | 1 | `d2ea73ee0e8472e4` | `d2ea73ee0e8472e4` | 2380 | 2399 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.retire_blob_replacement` | `blob_replacement_cleanups` | delete | 1 | `75415f0d12b7a661` | `75415f0d12b7a661` | 2432 | 2451 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.abort_blob_replacement` | `blob_replacement_cleanups` | delete | 1 | `75415f0d12b7a661` | `75415f0d12b7a661` | 2452 | 2471 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.reserve_pending_output_blob` | `blobs` | insert | 1 | `c997a216a1a51351` | `c997a216a1a51351` | 2471 | 2490 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.finalize_pending_output_blob` | `blobs` | update | 1 | `d2eb84d546dbcdd2` | `d2eb84d546dbcdd2` | 2553 | 2579 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.reserve_blob` | `blobs` | update | 1 | `d4eba14bc84728e8` | `d4eba14bc84728e8` | 2625 | 2651 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.reserve_blob` | `blobs` | insert | 1 | `d112eae374c9b904` | `d112eae374c9b904` | 2667 | 2700 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.mark_blob_ready` | `blobs` | update | 1 | `3fd8ace829dbb08b` | `3fd8ace829dbb08b` | 2747 | 2780 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.discard_pending_blob` | `blobs` | delete | 1 | `0e357be387ddc260` | `0e357be387ddc260` | 2778 | 2811 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.retire_abandoned_blob_reservation` | `blobs` | delete | 1 | `d22b3792b721f8ac` | `d22b3792b721f8ac` | 2845 | 2878 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations._record_applied_blob_proposal_effect` | `proposal_blob_effect_receipts` | insert | 1 | `8c66fbb679cf7fa3` | `8c66fbb679cf7fa3` | 3076 | 3109 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.prepare_blob_deletion` | `blob_deletion_cleanups` | insert | 1 | `850298970c19565f` | `850298970c19565f` | 3153 | 3186 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.retire_atomic_blob_deletion` | `blob_deletion_cleanups` | delete | 1 | `2f1a2d64948bf4b7` | `2f1a2d64948bf4b7` | 3251 | 3284 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.mark_blob_deletion_staged` | `blob_deletion_cleanups` | update | 1 | `afe03ed89b5a16c7` | `afe03ed89b5a16c7` | 3300 | 3333 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.commit_blob_deletion` | `blob_deletion_cleanups` | update | 1 | `32bd4fca1428b085` | `32bd4fca1428b085` | 3341 | 3374 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.commit_blob_deletion` | `blobs` | delete | 1 | `87f41b9adb5c8772` | `87f41b9adb5c8772` | 3348 | 3381 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.retire_blob_deletion` | `blob_deletion_cleanups` | delete | 1 | `4f9e61b1cf7247b0` | `4f9e61b1cf7247b0` | 3382 | 3415 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.abort_blob_deletion` | `blob_deletion_cleanups` | delete | 1 | `4f9e61b1cf7247b0` | `4f9e61b1cf7247b0` | 3400 | 3433 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.insert_blob_run_link` | `blob_run_links` | insert | 1 | `05fd90838bf9a030` | `05fd90838bf9a030` | 3425 | 3458 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations._adopt_pending_run_outputs` | `blobs` | update | 1 | `d8e2ea36cf4bd35c` | `d8e2ea36cf4bd35c` | 3501 | 3534 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.mark_run_output_blob_ready` | `blobs` | update | 1 | `a2e525368ada9268` | `a2e525368ada9268` | 3621 | 3661 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.mark_run_output_blob_error` | `blobs` | update | 1 | `f7b3307ef1e90c89` | `f7b3307ef1e90c89` | 3649 | 3689 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryBlobMutations.insert_blob_inline_resolutions` | `blob_inline_resolutions` | insert | 1 | `4c42110eaa7c5994` | `4c42110eaa7c5994` | 3732 | 3772 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_ForkChildSessionMutations.insert_child_state` | `composition_states` | insert | 1 | `a20e7856361bd56b` | `a20e7856361bd56b` | 3887 | 3929 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_ForkChildSessionMutations.append_child_messages` | `chat_messages` | insert | 1 | `b3fcd50e04854888` | `b3fcd50e04854888` | 3940 | 3982 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_ForkParentGuidedMutations.bind_guided_fork` | `guided_operations` | update | 1 | `16cc2a98abfd1f5e` | `16cc2a98abfd1f5e` | 4073 | 4115 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository.create_session_with_initial_fence` | `sessions` | insert | 1 | `3d19f2b10e4f6a34` | `3d19f2b10e4f6a34` | 4565 | 4613 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository.create_session_with_initial_fence` | `session_operation_fences` | insert | 1 | `3d19f2b10e4f6a34` | `3d19f2b10e4f6a34` | 4575 | 4623 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository.create_session_with_initial_fence` | `session_operation_fences` | update | 1 | `3d19f2b10e4f6a34` | `3d19f2b10e4f6a34` | 4589 | 4637 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository.acquire` | `session_operation_fences` | update | 1 | `83981192adddd83f` | `83981192adddd83f` | 4682 | 4730 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository._admit_blob_read` | `session_read_admissions` | delete | 1 | `8cf4bc5cc87457bd` | `8cf4bc5cc87457bd` | 4760 | 4808 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository._admit_blob_read` | `session_read_admissions` | insert | 1 | `8cf4bc5cc87457bd` | `8cf4bc5cc87457bd` | 4768 | 4816 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository._compare_and_swap_on_connection` | `session_operation_fences` | update | 1 | `ff923dcba8b3e8e7` | `ff923dcba8b3e8e7` | 4883 | 4931 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository.renew` | `session_read_admissions` | update | 1 | `d41bbaef242667cd` | `d41bbaef242667cd` | 4949 | 4997 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository.renew` | `session_operation_fences` | update | 1 | `d41bbaef242667cd` | `d41bbaef242667cd` | 4962 | 5010 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository.renew_fork_child_lease` | `session_operation_fences` | update | 1 | `b268c9591db479c5` | `b268c9591db479c5` | 5066 | 5114 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository._insert_fork_child` | `sessions` | insert | 1 | `b964b22650d92b97` | `b964b22650d92b97` | 5267 | 5316 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository._insert_fork_child` | `session_operation_fences` | insert | 1 | `d5ab9aa3497d191a` | `d5ab9aa3497d191a` | 5282 | 5331 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository._insert_fork_child` | `session_operation_fences` | update | 1 | `7326f9c03db2a1c7` | `7326f9c03db2a1c7` | 5296 | 5345 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository._resume_or_take_over_fork_child` | `session_operation_fences` | update | 1 | `988ac755147ef9bc` | `988ac755147ef9bc` | 5357 | 5406 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository.mutate_fork_creation` | `<sessions-write-connection>` | write_connection | 1 | `2a24f2fb856584b3` | `2a24f2fb856584b3` | 5403 | 5452 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository.release` | `session_read_admissions` | delete | 1 | `6be0e794bca1608d` | `6be0e794bca1608d` | 5498 | 5547 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository.release` | `session_operation_fences` | update | 1 | `6be0e794bca1608d` | `6be0e794bca1608d` | 5508 | 5557 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_SessionOperationAuthorityRepository.archive_delete` | `sessions` | delete | 1 | `66cc108182d86009` | `66cc108182d86009` | 5528 | 5577 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryComposerCompletionMutations.mark_ready_for_review` | `composer_completion_events` | insert | 1 | `b0eb63d6e0d9027b` | `b0eb63d6e0d9027b` | 5721 | 5775 |
| _REVIEWED_WRITERS | `src/elspeth/web/coordination/repository.py` | `_RepositoryComposerCompletionMutations.record_yaml_export` | `composer_completion_events` | insert | 1 | `2e42be4b632cb581` | `2e42be4b632cb581` | 5745 | 5799 |
| _REVIEWED_WRITERS | `src/elspeth/web/sessions/service.py` | `SessionServiceImpl.settle_guided_fork_operation._sync` | `chat_messages` | update | 1 | `77fdbd32e4f8a672` | `77fdbd32e4f8a672` | 14019 | 14020 |
| _REVIEWED_WRITERS | `src/elspeth/web/sessions/service.py` | `SessionServiceImpl.settle_guided_fork_operation._sync` | `composition_states` | delete | 1 | `77fdbd32e4f8a672` | `77fdbd32e4f8a672` | 14030 | 14031 |
| _REVIEWED_WRITERS | `src/elspeth/web/sessions/service.py` | `SessionServiceImpl.settle_guided_fork_operation._sync` | `chat_messages` | update | 2 | `77fdbd32e4f8a672` | `77fdbd32e4f8a672` | 14050 | 14051 |
| _REVIEWED_WRITERS | `src/elspeth/web/sessions/service.py` | `SessionServiceImpl.settle_guided_fork_operation._sync` | `guided_operations` | insert | 1 | `77fdbd32e4f8a672` | `77fdbd32e4f8a672` | 14098 | 14099 |
| _REVIEWED_WRITERS | `src/elspeth/web/sessions/service.py` | `SessionServiceImpl.settle_guided_fork_operation._sync` | `sessions` | update | 1 | `77fdbd32e4f8a672` | `77fdbd32e4f8a672` | 14152 | 14153 |

Why the three fingerprints move: each fingerprint hashes its enclosing function's body. `_reserve_pending_blob` and `_persist_blob_content` gained the recorder argument, and `copy_blobs_for_fork._verify_plan_and_quota` gained the operation and recorder arguments. The other 111 rows keep their fingerprint; only their `line=` moves, by the lines Steps 4, 8 and 12 insert above them.

Run the gate after re-pinning:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py -n 0 -rx > /tmp/i-lane-i2-step16-after.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `265 passed, 1 xfailed`. Read the XFAIL text in the log: it must show exactly the baseline counts above, with `Stale reviewed (0)` and `Stale reviewed read connections (0)`. A remaining stale row means a copied line or fingerprint is wrong. An `Unexpected` row naming `quota_authority.py`, or a `Connections outside exact contained authority` count above 16, means the admission function wrote or took a connection of its own, which this task forbids.

- [ ] **Step 17: Run every suite that constructs a session authority or a blob service, or calls a changed signature.** These are the parallel default runs (`addopts` carries `-n 12`), because the selections are thousands of tests; `-n 0` is for the single-test steps above.

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/blobs tests/unit/web/coordination tests/unit/contracts/test_web_blob_fencing.py tests/unit/contracts/test_blobs.py tests/unit/web/sessions/test_fork.py tests/unit/web/sessions/test_fork_custody_settlement.py tests/unit/web/sessions/test_guided_operation_fork_service.py tests/unit/web/test_app.py tests/unit/web/auth/test_audit.py tests/integration/web/composer/test_pipeline_custody.py tests/integration/web/composer/guided > /tmp/i-lane-i2-step17a.log 2>&1; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/sessions tests/unit/web/execution > /tmp/i-lane-i2-step17b.log 2>&1; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/composer tests/integration/web > /tmp/i-lane-i2-step17c.log 2>&1; echo exit=$?
```

Expected: `exit=0` three times. The export's measured results, where every failure was an I0 epoch pin that I0's block updates:
- **First selection:** 2514 passed and 7 skipped, measured before Step 10 added its two tests; the only failure was `test_schema9_epoch.py::test_current_schema_epoch_pair_is_deliberately_pinned`.
- **Second selection:** 3952 passed and 12 skipped. The failures were four I0 pins and `test_static_direct_writers.py::test_static_direct_writers_match_reviewed_allowlist`. That gate fired on a draft of the Step 6 run-output test that inserted `composition_states` directly; the Step 6 text seeds the run through `_seed_active_run`, and with it the gate passed (`tests/unit/web/sessions/test_static_direct_writers.py` together with the site file: `40 passed`).
- **Third selection:** 11393 passed; the only failure was the same `test_schema9_epoch.py` pin.

- [ ] **Step 18: Prove the admission SQL and its lock posture on PostgreSQL (F7).** The task adds a `blobs JOIN sessions` aggregate. Its claim of "no lock on `quota_policies`" is a lock property that SQLite cannot express. Docker is required.

`tests/testcontainer/web/test_quota_authority_postgres.py` after I1, lines 10-20 read:

```python
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.fenced_session import CONTAINER_TOKENS_PER_DAY, IDENTITY_TOKENS_PER_DAY, FencedSession, seed_token_policies

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, QuotaDisposition
from elspeth.web.coordination import chargeable_admission_authority
from elspeth.web.coordination.chargeable_admission_authority import RepositoryChargeableAdmissionAuthority
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.coordination.quota_authority import RepositoryQuotaAuthority, TokenUsageEntry
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
```

Replace them with:

```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from tests.helpers.fenced_session import CONTAINER_TOKENS_PER_DAY, IDENTITY_TOKENS_PER_DAY, FencedSession, seed_token_policies

from elspeth.contracts.blobs import IdentityStorageQuotaExceededError
from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, QuotaDisposition
from elspeth.web.coordination import chargeable_admission_authority
from elspeth.web.coordination.chargeable_admission_authority import RepositoryChargeableAdmissionAuthority
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.coordination.quota_authority import QuotaExceeded, RepositoryQuotaAuthority, StorageAdmission, TokenUsageEntry
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.models import blobs_table, quota_policies_table, sessions_table
```

Append to the end of the same file, after I1's `test_r14_refuses_and_derives_from_the_usage_on_postgres`:

```python
# ── R13 storage admission (Task I2) ────────────────────────────────────────


def _storage_policy(conn: Connection, *, identity_id: str | None, storage_bytes: int) -> None:
    conn.execute(
        insert(quota_policies_table).values(
            policy_id=f"storage-{identity_id or 'container'}",
            identity_id=identity_id,
            tokens_per_day=1000,
            storage_bytes=storage_bytes,
            set_by_actor="operator" if identity_id is not None else "config",
            set_by_identity_id=None,
            set_at=DAY,
        )
    )


def _storage_blob(conn: Connection, *, session_id: str, size_bytes: int, status: str) -> None:
    blob_id = str(uuid.uuid4())
    conn.execute(
        insert(blobs_table).values(
            id=blob_id,
            session_id=session_id,
            filename=f"{blob_id}.csv",
            mime_type="text/csv",
            size_bytes=size_bytes,
            content_hash=None,
            storage_path=f"/nonexistent/{blob_id}.csv",
            created_at=DAY,
            created_by="user",
            source_description=None,
            status=status,
        )
    )


def _admit_storage(fenced: FencedSession, additional_bytes: int, recorded: list[QuotaExceeded]) -> StorageAdmission:
    return RepositoryQuotaAuthority.admit_storage_bytes(
        fenced.connection_token,
        session_id=fenced.session_id,
        additional_bytes=additional_bytes,
        operation="blob_create",
        record=recorded.append,
    )


def test_r13_storage_usage_spans_archived_sessions_and_unready_rows_on_postgres(pg_fenced: FencedSession) -> None:
    """The blobs JOIN sessions aggregate on the production dialect: archived, pending and error rows count."""
    conn = _resolve_mutation_connection(pg_fenced.connection_token)
    _storage_policy(conn, identity_id=pg_fenced.identity_id, storage_bytes=100)
    archived_session = str(uuid.uuid4())
    conn.execute(
        insert(sessions_table).values(
            id=archived_session,
            user_id=pg_fenced.identity_id,
            auth_provider_type="local",
            title="archived",
            created_at=DAY,
            updated_at=DAY,
            archived_at=DAY,
        )
    )
    _storage_blob(conn, session_id=archived_session, size_bytes=60, status="pending")
    _storage_blob(conn, session_id=pg_fenced.session_id, size_bytes=30, status="error")
    recorded: list[QuotaExceeded] = []
    with pytest.raises(IdentityStorageQuotaExceededError, match=r"\(90 bytes\) plus 11 bytes would exceed its storage quota \(100 bytes\)"):
        _admit_storage(pg_fenced, 11, recorded)
    assert [(outcome.dimension, outcome.usage, outcome.cap, outcome.ceiling) for outcome in recorded] == [("storage", 90, 100, None)]
    conn.execute(update(quota_policies_table).where(quota_policies_table.c.identity_id == pg_fenced.identity_id).values(storage_bytes=101))
    assert _admit_storage(pg_fenced, 11, recorded).usage == 90


def test_r13_storage_admission_takes_no_policy_row_lock_on_postgres(pg_fenced: FencedSession) -> None:
    """D24: a blob write never serialises on the container ceiling row that R14 locks FOR UPDATE.

    Positive control: the same connection asking for that row FOR UPDATE times
    out, so the instrument detects a lock when one is taken.
    """
    with pg_fenced.engine.begin() as setup:
        _storage_policy(setup, identity_id=None, storage_bytes=1000)
    conn = _resolve_mutation_connection(pg_fenced.connection_token)
    conn.exec_driver_sql("SET LOCAL lock_timeout = '1s'")
    container_row = select(quota_policies_table.c.policy_id).where(quota_policies_table.c.identity_id.is_(None)).with_for_update()
    with pg_fenced.engine.connect() as holder, holder.begin():
        holder.execute(container_row).one()
        assert _admit_storage(pg_fenced, 11, []).ceiling == 1000
        with pytest.raises(OperationalError, match="lock timeout"), conn.begin_nested():
            conn.execute(container_row).one()
```

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/testcontainer/web/test_quota_authority_postgres.py tests/testcontainer/web/test_session_mutation_fencing_postgres.py tests/testcontainer/web/test_chargeable_admission_postgres.py -m testcontainer -n 0 > /tmp/i-lane-i2-step18.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `27 passed`. The three files are I1's and I2's quota proofs, the fork-copy fencing proofs that drive `copy_blobs_for_fork` on PostgreSQL, and I1's chargeable-admission lock-order proofs, which run `active_policy` through the new `_active_policy_rows` with `for_update=True`. Measured on the tree after I1, `tests/testcontainer/web/test_session_derived_mutations_postgres.py` fails all 9 of its tests both before and after this task, with `psycopg.errors.ForeignKeyViolation: insert or update on table "sessions" violates foreign key constraint "sessions_user_id_fkey"` (`Key (user_id)=(alice) is not present in table "identities"`). Its sessions are created without an identity row. That is a pre-existing fixture defect this task neither causes nor fixes, so it is not part of this step's gate.

- [ ] **Step 19: Static gates.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff check src/elspeth/contracts/blobs.py src/elspeth/web/blobs/protocol.py src/elspeth/web/blobs/routes.py src/elspeth/web/blobs/service.py src/elspeth/web/coordination/quota_authority.py src/elspeth/web/coordination/repository.py src/elspeth/web/coordination/sqlite_authority.py src/elspeth/web/composer/pipeline_custody.py src/elspeth/web/sessions/service.py src/elspeth/web/app.py tests/unit/web/coordination/test_storage_quota_authority.py tests/unit/web/blobs/test_storage_quota_sites.py tests/unit/web/test_app.py tests/unit/web/auth/test_audit.py tests/unit/contracts/test_web_blob_fencing.py tests/unit/architecture/test_session_db_mutation_authority.py tests/testcontainer/web/test_quota_authority_postgres.py > /tmp/i-lane-i2-ruff.log 2>&1; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff format --check src/elspeth/contracts/blobs.py src/elspeth/web/blobs/protocol.py src/elspeth/web/blobs/routes.py src/elspeth/web/blobs/service.py src/elspeth/web/coordination/quota_authority.py src/elspeth/web/coordination/repository.py src/elspeth/web/coordination/sqlite_authority.py src/elspeth/web/composer/pipeline_custody.py src/elspeth/web/sessions/service.py src/elspeth/web/app.py tests/unit/web/coordination/test_storage_quota_authority.py tests/unit/web/blobs/test_storage_quota_sites.py tests/unit/web/test_app.py tests/unit/web/auth/test_audit.py tests/unit/contracts/test_web_blob_fencing.py tests/unit/architecture/test_session_db_mutation_authority.py tests/testcontainer/web/test_quota_authority_postgres.py > /tmp/i-lane-i2-format.log 2>&1; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && mypy src/elspeth/contracts/blobs.py src/elspeth/web/blobs/protocol.py src/elspeth/web/blobs/routes.py src/elspeth/web/blobs/service.py src/elspeth/web/coordination/quota_authority.py src/elspeth/web/coordination/repository.py src/elspeth/web/coordination/sqlite_authority.py src/elspeth/web/composer/pipeline_custody.py src/elspeth/web/sessions/service.py src/elspeth/web/app.py > /tmp/i-lane-i2-mypy.log 2>&1; echo exit=$?
```

Expected: ruff `exit=0` twice; mypy `exit=0` with `Success: no issues found in 10 source files`.

The trust-tier lint gate exits `1` on every tree (the documented fail-closed state), so compare finding corpora, not exit codes. Measure the base from a detached worktree of the commit this task starts from (I1's commit, which is still `HEAD` while this task is uncommitted). Export `PYTHONPATH` so the console script lints that tree, not the main checkout:

```bash
cd "$(git rev-parse --show-toplevel)" && git worktree add --detach .claude/worktrees/i2-lints-base HEAD > /tmp/i-lane-i2-lints-worktree.log 2>&1; echo exit=$?
cd "$(git rev-parse --show-toplevel)/.claude/worktrees/i2-lints-base" && export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing "$(git -C "$PWD" rev-parse --path-format=absolute --git-common-dir)/../.venv/bin/elspeth-lints" check --rules all --root src/elspeth > /tmp/i-lane-i2-lints-base.log 2>&1; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/i-lane-i2-lints-after.log 2>&1; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && for side in base after; do grep -v '^WARNING' /tmp/i-lane-i2-lints-$side.log | sed -E -e 's#^[^ ]*/config/cicd/#config/cicd/#' -e 's/^([^:]+):[0-9]+:[0-9]+: /\1: /' | sort > /tmp/i-lane-i2-lints-$side.reduced; wc -l < /tmp/i-lane-i2-lints-$side.reduced; done; diff /tmp/i-lane-i2-lints-base.reduced /tmp/i-lane-i2-lints-after.reduced; echo diff_exit=$?
cd "$(git rev-parse --show-toplevel)" && scripts/worktree-cleanup.sh --path '*i2-lints-base' --execute > /tmp/i-lane-i2-lints-cleanup.log 2>&1; echo exit=$?
```

Expected: both lint runs `exit=1`; each reduced corpus holds 2038 lines (the positive control: an instrument that matched nothing would print 0), and `diff_exit=0`. The reduction strips the line and column from each `path:line:col:` finding and drops the tree's absolute prefix from the allowlist lines, which name their `config/cicd/<file>.yaml` by absolute path. That the corpora are identical was measured in the export. The cleanup script classifies the worktree REMOVABLE, since its HEAD is an ancestor of the branch, and removes it.

- [ ] **Step 20: Check branch safety and commit by file pathspec.**

```bash
cd "$(git rev-parse --show-toplevel)" && scripts/branch-safety-check.sh --intent commit > /tmp/i-lane-i2-safety.log 2>&1; echo exit=$?
```

Expected: `exit=0` with no `[FAIL]` line. The two created test modules are still untracked, and `git commit -- <pathspec>` aborts with `error: pathspec ... did not match any file(s) known to git` on an untracked path, so mark them intent-to-add first (`exit=0`). Then commit exactly the 17 files:

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N tests/unit/web/coordination/test_storage_quota_authority.py tests/unit/web/blobs/test_storage_quota_sites.py; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && git commit -m "feat(identity): R13 storage quota at every byte-admitting site" -- src/elspeth/contracts/blobs.py src/elspeth/web/blobs/protocol.py src/elspeth/web/blobs/routes.py src/elspeth/web/blobs/service.py src/elspeth/web/coordination/quota_authority.py src/elspeth/web/coordination/repository.py src/elspeth/web/coordination/sqlite_authority.py src/elspeth/web/composer/pipeline_custody.py src/elspeth/web/sessions/service.py src/elspeth/web/app.py tests/unit/web/coordination/test_storage_quota_authority.py tests/unit/web/blobs/test_storage_quota_sites.py tests/unit/web/test_app.py tests/unit/web/auth/test_audit.py tests/unit/contracts/test_web_blob_fencing.py tests/unit/architecture/test_session_db_mutation_authority.py tests/testcontainer/web/test_quota_authority_postgres.py
```

Check that `git show --stat HEAD` lists exactly those 17 files (2 created, 15 modified). Three of them are also edited by I3: `coordination/repository.py`, `sessions/service.py` and `web/app.py`, together with `test_session_db_mutation_authority.py` and `test_audit.py`. I3 lands after this task and rebases onto it; every collision is an adjacent-line change or a manifest `line=` re-pin.
