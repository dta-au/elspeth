### Task I9: Frontend — mailbox, completion bar, readiness row, admin UI, library, quota status

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: I7. Runs before: I10. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered after I7 (I0 → I8 → I1 → I2 → I3 → {I4 ∥ I5} → I6 → I7 → I9); I10
follows. Spec: §Frontend (sso-design.md:1189-1234), the admin advisory
(:974-975), D15/D18 (:74, :77), the `approvals` row (:1416: `decision_seen_at`
"is a UI convenience, never a control"; a rejection must carry a note), the
`review_requests` row (:1419), the `quota_set` metadata rule (:833-835),
the exact routes I3 to I7 register, and the identity-row usage, blob-list total
and single-admin advisory rows (:1199-1203, :974-975). Every frontend path:line below was read with
`git show 072141b75:<path>` because the shared checkout is dirty in
`App.tsx`, `App.test.tsx`, `types/index.ts` and `sessionStore.ts`. I0–I7 insert
lines above several Python anchors; find each site by the quoted anchor text.

Decisions this task owns:

1. **The spec needs four backend pieces that no ancestor block produces.**
   I9 adds each one, with a route test on I8's `closed_local_app`:
   a. `RepositoryApprovalAuthority.mark_decision_seen` in I3's
      `approval_authority.py`: the `seen` route's conditional UPDATE. It
      writes no audit row, and an undecided or already-seen request is not a
      refusal (spec :1416).
   b. `GET /api/workflow/mailbox/approvers`: the approver picker, with the
      requester's active `approver` edges as the default suggestion (spec
      :1416). A requester cannot read `GET /api/auth/admin/roles`, which is
      admin-only (identity_admin_routes.py:568).
   c. `RepositoryQuotaPolicyAuthority` and `storage_bytes_used_on_connection`
      in I1's `quota_authority.py`, plus a quota router (spec :1199-1203).
      HEAD has no quota read route and no quota write route: `IdentityView`
      (identity_admin_routes.py:161-187) has no quota field, and
      `quota_policies` is written only by activation
      (identity_authority.py:1534).
   d. `AuthAuditWriter.record_quota_set`: the editor's R4 audit row. HEAD's
      two `quota_set` writes (audit.py:686 and :1021) belong to activation.
2. **The surfaces are dialogs, not hash routes.** `useHashRouter.ts:74`
   matches `^#\/([^/]+?)(?:\/([a-z]+))?$`, so a `#/mailbox` route would be read
   as a session id. The mailbox, identity administration and library mount as
   `app-dialog` modals (the `UserAdminDialog` shape, App.tsx:841), opened from
   the header badge and the account menu. `useHashRouter.ts` is not touched.
3. **One timer.** `useMailboxStore.getState().startPolling()` owns the only
   `setInterval` (`MAILBOX_POLL_INTERVAL_MS = 30_000`; spec :1232 "One timer
   for the badge, and no second one"). `startPolling` is idempotent. App starts
   it while authenticated outside the shared-inspect route and stops it on
   unmount.
4. **The header badge renders nothing when the count is zero or governance is
   off.** The default deployment (`workflow_governance="off"`) and the five
   visual baselines under
   `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots/`
   therefore render unchanged. With nothing unread, the way in is the account
   menu's "Mailbox" entry.
5. **Unread means decided, not yet seen, and not caused by the requester.**
   A `superseded` request is ended by the requester's own next state. A
   requester withdrawal is `revoked` with `revocation_actor_kind="identity"`
   and `revoked_by_identity_id` = the caller (I3's `withdraw`). Neither counts.
   Review requests have no seen column and I0 is closed, so a review outcome is
   never unread: `decisions_unseen` counts approvals only. The Sent folder still
   lists both kinds (spec :1228-1229, "my own requests with their state, the
   decider, the decision note, and when"). `/sent` returns the caller's
   approvals and, through I4's `RepositoryReviewAuthority.sent_for` (I4 Step 3,
   decision 8), the caller's review requests, each with its attributed
   attestations (reviewer, verdict, note, `attested_at`). A sent review row is
   display-only: there is no seen stamp to set, and I7's inspect route 404s
   the requester. I7's audit view is no substitute: it 404s a caller without
   a live `approver` grant, and `AuditViewAttestation` carries no note.
6. **The readiness approval row is a separate component.** `ReadinessRowId`
   is closed three times: in `types/api.ts`, in `READINESS_ROW_IDS`
   (api/auditReadiness.ts:28) and in the two `never` arms of
   AuditReadinessPanel.tsx. No backend row exists to widen it for.
   `ApprovalReadinessRow` renders after the six rows' `</ul>`
   (AuditReadinessPanel.tsx:633). The row shows the newest request for the
   exact state, chosen by a stated total order: the later `requested_at`
   instant (parsed; PostgreSQL sends microseconds and SQLite whole seconds, so
   the strings do not sort), then the greater `approval_id` (SQLite's
   one-second clock ties requests). The pick never depends on the order of
   Sent. One state can carry several decided requests (I3 decision 8). The row
   shows the newest REQUEST, not R2's verdict. R2 admits on any approved row
   whose binding equals the compiled one, so a newer open or rejected request
   after an approval shows as waiting or rejected while execute still admits:
   the row under-reports, never over-reports, what multiplicity authorises.
   The execute-time refusal (`pendingApproval`) still overrides the row.
7. **A pending row shows its subject and organisation only.**
   `_identity_view` (identity_admin_routes.py:253-287) blanks `username`,
   `display_name` and `email` on a pending row that was never admitted. The
   table renders `username ?? subject` plus `organisation_id`, and never
   renders `display_name` or `email` for any row.
8. **Error envelopes.** Identity-admin refusals use the key `refusal`
   (identity_admin_routes.py:371). `parseResponse`'s error-type key list
   (client.ts:267) gains `"refusal"`. It also lifts `current_state` and the
   storage-quota triple `cap` / `ceiling` / `usage` into
   `ApiError.storage_quota`.
9. **The 413 body belongs to I2; I9 pins the shape it consumes.** That shape is
   `detail: {"error_type": "storage_quota_exceeded", "detail": <str>, "dimension": "storage", "cap": int | null, "ceiling": int | null, "usage": int}`.
   The frontend keys on a numeric `usage`, never on the `error_type` string.
   The per-session `BlobQuotaExceededError` 413 carries a plain string detail
   (blobs/routes.py:254), so it keeps the copy "File exceeds the maximum
   upload size."
10. **A quota edit changes one dimension per request** ("one editor sets
    either", spec :1200). Both columns are NOT NULL (models.py:3843, :3849).
    The other dimension is copied from the identity's active row, or from the
    container default setting when the identity has no row; with neither,
    the write refuses with `quota_default_missing`. The editor sits behind
    the `admin` role (spec :1197).
11. **UI roles come from the mailbox summary.** `UserProfile` carries only
    `dev_admin` (types/index.ts:39-48). The summary reports the caller's
    live, deployment-wide roles through `active_roles`
    (identity_authority.py:1277). Every admin route re-proves the role on
    each request.
12. **The completion bar gains no member; the request actions live in the readiness approval row.**
    In the workspace action bar, `.completion-bar` is a one-row grid pinned
    to `grid-template-columns: auto auto 1fr auto`. Run is fixed to column 4
    and the gate reason to column 3 (workspace.css:457-516), and
    workspaceChrome.test.ts:242-349 pins that contract. A fifth direct child
    would auto-place into a wrapped second row. The bar's I-workstream copy
    change is I4's rename to "Share inspect link". I9's completion gesture is
    "Request approval" / "Request review" on `ApprovalReadinessRow`, beside the
    approval state it changes. The execute-time 409 surfaces twice: as
    `executionStore.error`, which `SideRailValidationBanner.tsx:190`
    renders, and as `executionStore.pendingApproval`, which the approval row
    renders.
13. **The inbox, inspect and decide admit the same approver, and a decision
    follows the inspection of that exact state.** The mailbox approval folder
    and the badge's `approvals_to_decide` are I3's
    `ApprovalTransactionAuthority.inbox` (`approver_identity_id = caller AND
    decision IS NULL`). It cannot list the caller's own request, because
    `ck_approvals_author_is_not_approver` (models.py:3647-3650) forbids a
    requester addressing themselves. That is the approver I7's inspect admits
    (`_OPEN_APPROVAL_ADDRESSED_TO_CALLER`) and the only approver I3's `decide`
    admits (I3 decision 7). Spec :1205-1207 lists "any other open request I am
    eligible to decide" after the addressed ones; under addressed-only
    eligibility that set is empty, so there is no second inbox query. If the
    operator rules eligibility role-based (the I7 item in the master's
    Self-review notes), the inbox, inspect and decide widen together. In the
    frontend, `InspectPane` tags each settled inspection with the request id,
    `session_id` and `state_id` it was fetched for. Approve, Reject, Sign off
    and Request changes are enabled only when a loaded inspection carries the
    shown request's tag. While it is loading, after it fails (including a
    404, a server error, or an echoed `session_id`/`state_id` that differs
    from the request's), and whenever the settled inspection belongs to a
    different target, every decision button is disabled.

**Files:**
- Modify: `src/elspeth/web/coordination/approval_authority.py` (I3's module): `mark_decision_seen` goes directly after `RepositoryApprovalAuthority.read`
- Modify: `src/elspeth/web/coordination/quota_authority.py` (I1's module): the import block, and the I9 section appended after `RepositoryQuotaAuthority.active_policy`
- Modify: `src/elspeth/web/auth/audit.py:255` (`AdminActivationCause = Literal[`, the Protocol member goes directly above it), `:290` (`def _bounded_text(`, the enum member goes directly above it), `:1180` (`class _AdminProvenanceMetadata(TypedDict):`, the recorder method goes directly above it), and the `if TYPE_CHECKING:` import I1 added
- Create: `src/elspeth/web/sessions/routes/workflow/mailbox.py`, `src/elspeth/web/sessions/routes/workflow/quota.py`
- Modify: `src/elspeth/web/app.py` (the `app.state.identity_authority = identity_authority` line, HEAD :1522; I7's `from elspeth.web.sessions.routes.workflow.audit_view import create_workflow_audit_view_router` import; I7's `app.include_router(create_workflow_audit_view_router())` line)
- Modify: `tests/unit/architecture/test_session_db_mutation_authority.py` (`_NAMED_AUTHORITY_SYMBOLS` :262, `_CONTAINED_CONNECTION_AUTHORITIES` :914 closing `)` above the comment at :1195, `_REVIEWED_WRITERS` :1198, `_REVIEWED_READ_CONNECTIONS` :3954)
- Modify: `tests/unit/web/auth/test_audit.py` (append at end of file), `tests/unit/web/auth/test_identity_admin_routes.py:97` (`def only(`; the new fake member goes directly above it)
- Create (Python tests): `tests/unit/web/coordination/test_approval_mailbox.py`, `tests/unit/web/coordination/test_quota_policy_authority.py`, `tests/unit/web/workflow/test_mailbox_routes.py`, `tests/unit/web/workflow/test_quota_routes.py`, `tests/testcontainer/web/test_workflow_mailbox_postgres.py`
- Create (frontend, under `src/elspeth/web/frontend/src/`): `types/workflow.ts`, `api/workflow.ts`, `stores/mailboxStore.ts`, `utils/bytes.ts`, `components/workflow/MailboxBadge.tsx`, `components/workflow/MailboxDialog.tsx`, `components/workflow/InboxList.tsx`, `components/workflow/SentList.tsx`, `components/workflow/InspectPane.tsx`, `components/workflow/WorkflowRequestDialog.tsx`, `components/workflow/ApprovalReadinessRow.tsx`, `components/workflow/workflow.css`, `components/admin/AdminDialog.tsx`, `components/admin/IdentitiesTable.tsx`, `components/admin/RolesEditor.tsx`, `components/admin/RelationshipsEditor.tsx`, `components/admin/QuotaEditor.tsx`, `components/library/LibraryDialog.tsx`, `components/blobs/IdentityStorageTotal.tsx`
- Modify (frontend): `api/client.ts:257` (`let partialStateSaveError`), `:267` (key list), `:270` (`requestId = firstStringField`), `:401` (`} catch {`), `:420` (`partial_state_save_error: partialStateSaveError,`); `types/index.ts:1212` (`snapshot_fingerprint?: string;`, the last `ApiError` field); `stores/executionStore.ts:101`, `:433`, `:540`, `:604`; `stores/blobStore.ts:4` and the `const detail =` block in `uploadBlob`; `components/blobs/BlobManager.tsx:8` and `:128`; `components/blobs/BlobManager.test.tsx:7`; `components/audit/AuditReadinessPanel.tsx:17` and `:633`; `components/common/AppHeader.tsx:21`, `:27`, `:48`; `components/common/UserMenu.tsx:18`, `:70`, `:131`, `:237`; `App.tsx:36`, `:125`, `:152`, `:742-748`, `:841`; `App.test.tsx:307` (the `vi.mock("./api/shareableReviews"` block; the new mock goes directly after it) and end of file; `styles/index.css:33`
- Create (frontend tests): `api/client.workflow-errors.test.ts`, `api/workflow.test.ts`, `utils/bytes.test.ts`, `stores/mailboxStore.test.ts`, `stores/executionStore.approval.test.ts`, `stores/blobStore.quota.test.ts`, `components/workflow/MailboxBadge.test.tsx`, `components/workflow/MailboxDialog.test.tsx`, `components/workflow/WorkflowRequestDialog.test.tsx`, `components/workflow/ApprovalReadinessRow.test.tsx`, `components/admin/AdminDialog.test.tsx`, `components/admin/IdentitiesTable.test.tsx`, `components/admin/RolesEditor.test.tsx`, `components/admin/RelationshipsEditor.test.tsx`, `components/admin/QuotaEditor.test.tsx`, `components/library/LibraryDialog.test.tsx`, `components/blobs/IdentityStorageTotal.test.tsx`, `components/common/UserMenu.workflow.test.tsx`
- Modify: `CHANGELOG.md` (the I9 bullet goes after the I8/I3/I4/I5/I6/I7 bullets under `## 0.8.1 - 2026-09-10`)

**Interfaces:**
- Consumes:
  - I8: `WebSettings.workflow_governance: Literal["off", "on"] = "off"`; fixtures `closed_local_settings` and `closed_local_app` in `tests/unit/web/conftest.py`, with `client.app.state.phase3_engine`, `client.app.state.phase3_sessions_service`, `client.app.state.settings`, `quota_default_tokens_per_day=100_000`, `quota_default_storage_bytes=1_000_000` and `get_current_user` overridden to `alice`.
  - I1: `QuotaPolicyRow(policy_id, tokens_per_day, storage_bytes)`; `RepositoryQuotaAuthority.daily_token_total(connection_token, *, identity_id, day_start_utc) -> int | None`; `utc_day_start(now) -> datetime`; the `if TYPE_CHECKING:` block in `audit.py` that imports `QuotaExceeded`; `fenced_session` (`engine`, `connection_token`, `identity_id` = `"alice"`, `session_id: str`) from `tests/unit/web/coordination/conftest.py`.
  - I3: `ApprovalRecord` (every field listed in I3's Interfaces), `ApprovalNotFound(approval_id)` (message `approval {approval_id} not found`), `ApprovalBinding` and `.as_json()`, `build_approval_binding(*, evidence, config_hash, canonical_version, openrouter_catalog_sha256, runtime_val_manifest_sha256)`, `RepositoryApprovalAuthority.request` / `.decide` / `.withdraw` (keyword signatures in I3's Interfaces), `_APPROVAL_BY_ID`, `_SENT`, `_record(conn, row)`; `ApprovalTransactionAuthority(engine)` with `.run(session_id, mutation)`, `.session_id_of(approval_id)`, `.sent(*, requested_by_identity_id)`, `.inbox(*, approver_identity_id)` (addressed-only: `approver_identity_id = caller AND decision IS NULL`; decision 13); `create_approvals_router() -> APIRouter`, which reads `app.state.approval_authority` and calls `app.state.auth_audit_recorder.record_approval_decided(request, *, provider, approval, actor_identity_id)`; `decide`'s addressed-only rule (I3 decision 7: a caller who is neither the addressed approver nor the requester gets `ApprovalNotFound` before any lock; the requester gets `ApprovalAuthorIsApprover`); `ApprovalView` and `_view(record)` in `routes/workflow/approvals.py`; `tests/unit/web/workflow/__init__.py`; the routes `POST /api/sessions/{session_id}/approvals`, `POST /api/approvals/{approval_id}/decide`, `POST /api/approvals/{approval_id}/withdraw`; the error codes `approval_already_decided` (+ `current_state`), `approval_author_is_approver`, `approval_not_found` (404), `approval_note_required`, `approval_open_request_exists`, `workflow_governance_off`.
  - I4: `RepositoryReviewAuthority(engine)` with `.request(*, session_id, state_id, requested_by, reviewer, note, record)`, `.open_for(*, reviewer)`, `.attest(*, session_id, state_id, payload_digest, reviewer, verdict, note, record)` and `.sent_for(*, requested_by) -> tuple[ReviewSentRecord, ...]`; `ReviewSentRecord(request: ReviewRequestRecord, attestations: tuple[ReviewAttestationRecord, ...])`; `ReviewRequestView`, `_request_view(record)`, `ReviewAttestationView` (its fields) and `_attestation_view(record)` in `routes/workflow/reviews.py`; `app.state.review_authority`; the routes `POST /api/sessions/{session_id}/reviews` (body `{state_id, reviewer_identity_id, note}`) and `POST /api/reviews/{request_id}/attest` (body `{verdict, note}`); the error code `changes_requested_needs_note`; the frontend accessible name `Share inspect link`.
  - I5: `GET /api/library?view=accepted`, returning `{view, entries: LibraryEntryView[]}` with the field list in I5's Interfaces; `POST /api/library/{entry_id}/fork`, returning 201 `{session_id, state_id}`; the error code `workflow_governance_off`.
  - I6: `AuthAuditRecorder._auth_audit(db)`. Every recorder method writes through `self._auth_audit(db).record_auth_event`, which stamps `compartment_id`.
  - I7: `GET /api/workflow/inspect/{session_id}/{state_id}`, returning `{session_id, state_id, access_log_id, composition_snapshot, yaml, attestations}`, with 404 on any denial and an approver arm that admits only the addressed approver; `create_workflow_inspect_router() -> APIRouter` in `sessions/routes/workflow/inspect.py` (reads `app.state.audit_access_log_authority`, `app.state.session_service` and `app.state.review_authority`); `RepositoryAuditAccessLogAuthority(engine)` in `coordination/audit_access_log_authority.py`, writing one `audit_access_log` row (`requesting_principal` = caller) per admitted read and none per denial; the `app.include_router(create_workflow_audit_view_router())` line and its import in `web/app.py`.
  - I2: the 413 body pinned in decision 9.
  - HEAD: `RepositoryIdentityAuthority.active_roles(*, identity_id) -> tuple[RoleGrant, ...]` (identity_authority.py:1277), `holds_active_role` (:1285), `list_roles(*, identity_id, include_revoked, limit, offset)` (:1305), `list_relationships(*, identity_id, include_revoked, limit, offset)` (:1320), `read_identity_summary(*, identity_id)` (:1258), `_require_limit` with a cap of `_LIST_LIMIT_MAX` (:668); `RoleGrant` and `IdentitySummary` (:299-331); `IdentityListResponse.active_human_admin_count` (identity_admin_routes.py:198); the identity-admin routes under `/api/auth/admin` (:402-701) with bodies `EnableIdentityRequest(note)`, `DisableIdentityRequest(reason)`, `GrantRoleRequest(identity_id, role, note)`, `RevokeRequest(note)` and `AssertRelationshipRequest(from_identity_id, to_identity_id, relationship_type, note)` (:110-158); `_admin_provenance(request, *, actor_identity_id, on_behalf_of, console_request_id)` (audit.py:1206); `database_now(conn)` (coordination/database_clock.py:54); `_register_mutation_connection` / `_unregister_mutation_connection` (mutation_connection_registry.py:22, :44); `run_sync_in_worker` (async_workers.py:179); `ensure_test_identity(conn, *, identity_id, provider="local")` (tests/fixtures/identities.py:10); `_make_session(conn, *, session_id, user_id="test_user", auth_provider_type="local", title="test session", created_at=None, updated_at=None)` (tests/unit/web/conftest.py:73, importable as `from tests.unit.web.conftest import _make_session`); `engine` (tests/unit/web/conftest.py:63); `external_deployment_postgres_url` (tests/testcontainer/web/conftest.py:40); frontend `authHeaders` / `parseResponse` (client.ts:96, :211), `Button` / `Input` (components/ui/index.ts), `useFocusTrap(ref, active)` (hooks/useFocusTrap.ts), `resetStore` (test/store-helpers.ts), `makeComposition(version, overrides)` (test/composerFixtures.ts:120), `useSessionStore` `loadSessions` / `selectSession` (sessionStore.ts:1236, :1238).
- Produces:
  - `approval_authority.py`: `RepositoryApprovalAuthority.mark_decision_seen(connection_token: str, *, approval_id: str, requested_by: str, now: datetime) -> ApprovalRecord` (raises `ApprovalNotFound` when the row is absent or the caller is not its requester).
  - `quota_authority.py`: `QuotaDimension = Literal["tokens", "storage"]`; `MAX_QUOTA_VALUE: Final = 2_147_483_647`; `storage_bytes_used_on_connection(connection: Connection, *, identity_id: str) -> int`; `IdentityQuotaStatus(identity_id: str, identity_policy: QuotaPolicyRow | None, container_policy: QuotaPolicyRow | None, tokens_used_today: int | None, storage_bytes_used: int)`; `QuotaPolicySet(identity_id: str, actor_identity_id: str, dimension: QuotaDimension, policy: QuotaPolicyRow, previous: QuotaPolicyRow | None)`; `QuotaPolicyRefusal(RuntimeError)` and its closed subclasses `QuotaSetterNotAdmin`, `QuotaTargetNotFound`, `QuotaTargetNotActive`, `QuotaDefaultMissing`, `QuotaValueOutOfRange`; `RepositoryQuotaPolicyAuthority(engine: Engine)` with `.status(*, identity_id: str) -> IdentityQuotaStatus` and `.set_identity_policy(*, actor_identity_id: str, identity_id: str, dimension: QuotaDimension, value: int, default_tokens_per_day: int | None, default_storage_bytes: int | None, record: Callable[[QuotaPolicySet], None]) -> QuotaPolicySet`. Mounted as `app.state.quota_policy_authority`.
  - `audit.py`: `AuthAuditWriter.record_quota_set(self, request: Request | None, *, provider: AuthProviderType, change: QuotaPolicySet) -> None`; `AuthAuditOperation.QUOTA_SET = "quota_set"`. Each row has `event_type="quota_set"`, `outcome="success"` and `identity_id=change.identity_id`. Its metadata is the admin provenance keys plus `source="admin"`, `dimension`, `cap`, `previous_cap`, `tokens_per_day`, `storage_bytes`, `policy_id` and `revoked_policy_id`.
  - `routes/workflow/mailbox.py`: `create_mailbox_router() -> APIRouter`; `MAILBOX_ROLES`; `decision_is_unseen(record: ApprovalRecord, *, caller: str) -> bool`; `ReviewSentView(request: ReviewRequestView, attestations: list[ReviewAttestationView])`; routes:
    - `GET /api/workflow/mailbox/summary` returns `MailboxSummaryResponse(governance: Literal["on","off"], roles: list[IdentityRole], approvals_to_decide: int, reviews_to_attest: int, decisions_unseen: int)`.
    - `GET /api/workflow/mailbox/inbox` returns `MailboxInboxResponse(approvals: list[ApprovalView], reviews: list[ReviewRequestView])`; `approvals` is I3's addressed-only `inbox` (decision 13), and `summary.approvals_to_decide` is its length.
    - `GET /api/workflow/mailbox/sent` returns `MailboxSentResponse(approvals: list[ApprovalView], reviews: list[ReviewSentView])`; both lists are empty while governance is off, and `reviews` never feeds `decisions_unseen`.
    - `POST /api/workflow/mailbox/{approval_id}/seen` returns `ApprovalView`, 404 `approval_not_found`, or 409 `workflow_governance_off`.
    - `GET /api/workflow/mailbox/approvers` returns `ApproverDirectoryResponse(approvers: list[ApproverEntry(identity_id, username)], suggested_identity_ids: list[str])`.
  - `routes/workflow/quota.py`: `create_quota_router() -> APIRouter`; `IdentityQuotaView(identity_id, tokens_per_day: int | None, storage_bytes: int | None, container_tokens_per_day: int | None, container_storage_bytes: int | None, tokens_used_today: int | None, storage_bytes_used: int)`; `SetQuotaBody(dimension: Literal["tokens","storage"], value: int)`; routes:
    - `GET /api/workflow/quota/me` (any signed-in identity).
    - `GET /api/workflow/quota/identities/{identity_id}` (admin; a hidden 404 for anyone else).
    - `POST /api/workflow/quota/identities/{identity_id}` (admin). Refusals: 404 `quota_target_not_found`; 409 `quota_target_not_active`, `quota_default_missing`, `quota_value_out_of_range`.
  - Frontend: `types/workflow.ts` (every wire type below); `api/workflow.ts` fetchers; `ApiError.current_state?: string` and `ApiError.storage_quota?: StorageQuotaRefusal`; `useMailboxStore` with `summary`, `inbox`, `sent`, `sentReviews`, `error`, `refreshSummary`, `loadInbox`, `loadSent`, `openSent`, `decide`, `attest`, `startPolling`, `reset`, plus the exports `MAILBOX_POLL_INTERVAL_MS`, `badgeCount(summary)` and `approvalForState(sent, sessionId, stateId)` (the newest request for the exact state by `requested_at` instant, then `approval_id`; decision 6); `ExecutionState.pendingApproval: PendingApproval | null`; `formatBytes(bytes: number): string` and `storageQuotaMessage(refusal: StorageQuotaRefusal): string` in `utils/bytes.ts`; the components listed under Files. I10 consumes the routes, not the components.

- [ ] **Step 1: Write the failing approval-authority tests (the seen stamp).**

Create `tests/unit/web/coordination/test_approval_mailbox.py`:

```python
"""Task I9 additions to I3's approval authority, through I1's ``fenced_session`` token.

``mark_decision_seen`` is a UI convenience (spec :1416): it never refuses an
undecided or already-seen request, and it hides a request the caller did not
raise. The mailbox approval folder is I3's addressed-only ``inbox`` (decision
13); its agreement with inspect and decide for an author, the addressed
approver and another approver is pinned in
tests/unit/web/workflow/test_mailbox_routes.py.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.web.coordination.approval_authority import (
    ApprovalBinding,
    ApprovalNotFound,
    ApprovalRecord,
    RepositoryApprovalAuthority,
    build_approval_binding,
)
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.sessions.models import identity_roles_table

NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


def _binding() -> ApprovalBinding:
    evidence = WebPluginPolicyEvidence(
        schema_version=1,
        policy_hash="a" * 64,
        snapshot_hash="b" * 64,
        authorized_plugin_ids=("sink:csv", "source:csv"),
        available_plugin_ids=("sink:csv", "source:csv"),
        control_modes=(),
        selected_implementations=(),
        selected_profile_aliases=(),
        plugin_code_identities=(),
        binding_generation_fingerprint="c" * 64,
        decision_codes=("policy_allowed",),
    )
    return build_approval_binding(
        evidence=evidence,
        config_hash="1" * 64,
        canonical_version="sha256-rfc8785-v1",
        openrouter_catalog_sha256="2" * 64,
        runtime_val_manifest_sha256="3" * 64,
    )


def _ignore(_record: Any) -> None:
    return None


def _seed(fenced_session: Any) -> None:
    """``bob`` approves."""
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    ensure_test_identity(conn, identity_id="bob")
    conn.execute(
        insert(identity_roles_table).values(
            role_id="bob-approver",
            identity_id="bob",
            role="approver",
            granted_at=NOW - timedelta(days=1),
            granted_by_identity_id="bob",
        )
    )


def _request(fenced_session: Any, *, session_id: str, requested_by: str, approver: str, at: datetime) -> ApprovalRecord:
    return RepositoryApprovalAuthority.request(
        fenced_session.connection_token,
        session_id=session_id,
        state_id=f"state-{session_id}",
        binding=_binding(),
        requested_by=requested_by,
        approver=approver,
        note="please",
        now=at,
        record=_ignore,
    )


def _decide(fenced_session: Any, approval_id: str, *, decided_by: str, at: datetime) -> ApprovalRecord:
    return RepositoryApprovalAuthority.decide(
        fenced_session.connection_token,
        approval_id=approval_id,
        decided_by=decided_by,
        decision="approved",
        note=None,
        now=at,
        record=_ignore,
    )


def test_mark_decision_seen_stamps_a_decided_request_exactly_once(fenced_session: Any) -> None:
    _seed(fenced_session)
    opened = _request(fenced_session, session_id=fenced_session.session_id, requested_by="alice", approver="bob", at=NOW)
    _decide(fenced_session, opened.approval_id, decided_by="bob", at=NOW + timedelta(minutes=1))
    first = RepositoryApprovalAuthority.mark_decision_seen(
        fenced_session.connection_token, approval_id=opened.approval_id, requested_by="alice", now=NOW + timedelta(minutes=2)
    )
    assert first.decision_seen_at == NOW + timedelta(minutes=2)
    # Mutation-derivation: the ``decision_seen_at IS NULL`` term, not the call, keeps the first stamp.
    again = RepositoryApprovalAuthority.mark_decision_seen(
        fenced_session.connection_token, approval_id=opened.approval_id, requested_by="alice", now=NOW + timedelta(minutes=9)
    )
    assert again.decision_seen_at == NOW + timedelta(minutes=2)


def test_mark_decision_seen_leaves_an_open_request_unstamped_until_it_is_decided(fenced_session: Any) -> None:
    _seed(fenced_session)
    opened = _request(fenced_session, session_id=fenced_session.session_id, requested_by="alice", approver="bob", at=NOW)
    still_open = RepositoryApprovalAuthority.mark_decision_seen(
        fenced_session.connection_token, approval_id=opened.approval_id, requested_by="alice", now=NOW + timedelta(minutes=1)
    )
    assert still_open.decision is None
    assert still_open.decision_seen_at is None
    # Mutation-derivation: a decision row, not a second call, is what admits the stamp.
    _decide(fenced_session, opened.approval_id, decided_by="bob", at=NOW + timedelta(minutes=2))
    seen = RepositoryApprovalAuthority.mark_decision_seen(
        fenced_session.connection_token, approval_id=opened.approval_id, requested_by="alice", now=NOW + timedelta(minutes=3)
    )
    assert seen.decision_seen_at == NOW + timedelta(minutes=3)


def test_mark_decision_seen_hides_a_request_the_caller_did_not_raise(fenced_session: Any) -> None:
    _seed(fenced_session)
    opened = _request(fenced_session, session_id=fenced_session.session_id, requested_by="alice", approver="bob", at=NOW)
    _decide(fenced_session, opened.approval_id, decided_by="bob", at=NOW + timedelta(minutes=1))
    with pytest.raises(ApprovalNotFound, match=f"approval {opened.approval_id} not found"):
        RepositoryApprovalAuthority.mark_decision_seen(
            fenced_session.connection_token, approval_id=opened.approval_id, requested_by="bob", now=NOW + timedelta(minutes=2)
        )
    with pytest.raises(ApprovalNotFound, match="approval no-such-approval not found"):
        RepositoryApprovalAuthority.mark_decision_seen(
            fenced_session.connection_token, approval_id="no-such-approval", requested_by="alice", now=NOW
        )
    unseen = RepositoryApprovalAuthority.read(fenced_session.connection_token, approval_id=opened.approval_id)
    assert unseen.decision_seen_at is None


def test_approval_record_field_set_is_the_one_the_mailbox_renders() -> None:
    assert "decision_seen_at" in {field.name for field in dataclasses.fields(ApprovalRecord)}
```

- [ ] **Step 2: Run the approval-authority tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_approval_mailbox.py -n 0 > /tmp/i9-lane-approval-red.log 2>&1; echo exit=$?`
Expected: `exit=1`. The log shows `AttributeError: type object 'RepositoryApprovalAuthority' has no attribute 'mark_decision_seen'` for the three seen tests; `test_approval_record_field_set_is_the_one_the_mailbox_renders` passes.

- [ ] **Step 3: Add the seen stamp to `approval_authority.py`.**

In `src/elspeth/web/coordination/approval_authority.py`, directly after `RepositoryApprovalAuthority.read` (the static method that ends `return _record(conn, row)`) and before `@final` / `class ApprovalTransactionAuthority:`, add:

```python

    @staticmethod
    def mark_decision_seen(connection_token: str, *, approval_id: str, requested_by: str, now: datetime) -> ApprovalRecord:
        """Stamp ``decision_seen_at`` when the requester opens a decided request (spec :1229).

        A UI convenience, never a control (spec :1416): no audit row, and an
        undecided or already-seen request is not a refusal. The conditional
        UPDATE matches nothing and the current record is returned. A request
        the caller did not raise is hidden as not found.
        """
        conn = _resolve_mutation_connection(connection_token)
        row = conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one_or_none()
        if row is None or row.requested_by_identity_id != requested_by:
            raise ApprovalNotFound(approval_id)
        conn.execute(
            update(approvals_table)
            .where(
                approvals_table.c.approval_id == approval_id,
                approvals_table.c.requested_by_identity_id == requested_by,
                approvals_table.c.decision.is_not(None),
                approvals_table.c.decision_seen_at.is_(None),
            )
            .values(decision_seen_at=now)
        )
        return _record(conn, conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one())
```

- [ ] **Step 4: Run the approval-authority tests and I3's authority suite to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_approval_mailbox.py tests/unit/web/coordination/test_approval_authority.py -n 0 > /tmp/i9-lane-approval-green.log 2>&1; echo exit=$?`
Expected: `exit=0`; `test_approval_mailbox.py` contributes `4 passed`.

- [ ] **Step 5: Write the failing quota-policy authority tests.**

Create `tests/unit/web/coordination/test_quota_policy_authority.py`:

```python
"""``RepositoryQuotaPolicyAuthority``: the admin identity row's quota status and the one-dimension editor write.

Real sessions engine (``engine``, tests/unit/web/conftest.py:63), committed rows.
Every refusal has a fire test and a mutation-derivation test that changes the
authority row (a role, an access state, a policy row), never the guard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select, update
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.conftest import _make_session

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import (
    MAX_QUOTA_VALUE,
    QuotaDefaultMissing,
    QuotaPolicySet,
    QuotaSetterNotAdmin,
    QuotaTargetNotActive,
    QuotaTargetNotFound,
    QuotaValueOutOfRange,
    RepositoryQuotaPolicyAuthority,
    storage_bytes_used_on_connection,
)
from elspeth.web.sessions.models import (
    blobs_table,
    identities_table,
    identity_roles_table,
    quota_policies_table,
    token_usage_ledger_table,
)


def _seed(engine: Engine) -> datetime:
    """``root`` is a live admin; ``alice`` and ``bob`` are active; alice holds a policy and a container ceiling exists."""
    with engine.begin() as conn:
        now = database_now(conn)
        for identity_id in ("root", "alice", "bob"):
            ensure_test_identity(conn, identity_id=identity_id)
        conn.execute(
            insert(identity_roles_table).values(
                role_id="root-admin", identity_id="root", role="admin", granted_at=now - timedelta(days=1), granted_by_identity_id="root"
            )
        )
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="alice-policy",
                identity_id="alice",
                tokens_per_day=500,
                storage_bytes=2000,
                set_by_actor="system",
                set_at=now - timedelta(days=1),
            )
        )
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="container-ceiling",
                identity_id=None,
                tokens_per_day=9000,
                storage_bytes=90000,
                set_by_actor="config",
                set_at=now - timedelta(days=1),
            )
        )
    return now


def _blob(conn: Any, *, blob_id: str, session_id: str, size_bytes: int, now: datetime) -> None:
    conn.execute(
        insert(blobs_table).values(
            id=blob_id,
            session_id=session_id,
            filename=f"{blob_id}.csv",
            mime_type="text/csv",
            size_bytes=size_bytes,
            storage_path=f"blobs/{blob_id}",
            created_at=now,
            created_by="user",
            status="ready",
        )
    )


def _ledger(conn: Any, *, entry_id: str, identity_id: str, session_id: str, prompt: int | None, completion: int | None, at: datetime) -> None:
    conn.execute(
        insert(token_usage_ledger_table).values(
            entry_id=entry_id,
            identity_id=identity_id,
            source="composer",
            session_id=session_id,
            run_id=None,
            model="test-model",
            prompt_tokens=prompt,
            completion_tokens=completion,
            recorded_at=at,
        )
    )


def _ignore(_change: QuotaPolicySet) -> None:
    return None


def _set(authority: RepositoryQuotaPolicyAuthority, **overrides: Any) -> QuotaPolicySet:
    values: dict[str, Any] = {
        "actor_identity_id": "root",
        "identity_id": "alice",
        "dimension": "storage",
        "value": 3000,
        "default_tokens_per_day": None,
        "default_storage_bytes": None,
        "record": _ignore,
    }
    values.update(overrides)
    return authority.set_identity_policy(**values)


def _active_rows(engine: Engine, identity_id: str) -> list[Any]:
    with engine.connect() as conn:
        return conn.execute(
            select(quota_policies_table).where(quota_policies_table.c.identity_id == identity_id, quota_policies_table.c.revoked_at.is_(None))
        ).all()


# ── status ───────────────────────────────────────────────────────────────


def test_status_reports_both_policies_todays_tokens_and_the_storage_level(engine: Engine) -> None:
    now = _seed(engine)
    with engine.begin() as conn:
        _make_session(conn, session_id="alice-1", user_id="alice")
        _make_session(conn, session_id="alice-2", user_id="alice")
        _make_session(conn, session_id="bob-1", user_id="bob")
        _blob(conn, blob_id="a1", session_id="alice-1", size_bytes=400, now=now)
        _blob(conn, blob_id="a2", session_id="alice-2", size_bytes=600, now=now)
        _blob(conn, blob_id="b1", session_id="bob-1", size_bytes=999, now=now)
        _ledger(conn, entry_id="today-1", identity_id="alice", session_id="alice-1", prompt=100, completion=50, at=now)
        _ledger(conn, entry_id="today-2", identity_id="alice", session_id="alice-2", prompt=7, completion=3, at=now)
        _ledger(conn, entry_id="yesterday", identity_id="alice", session_id="alice-1", prompt=5000, completion=0, at=now - timedelta(days=1))
    status = RepositoryQuotaPolicyAuthority(engine).status(identity_id="alice")
    assert status.identity_id == "alice"
    assert status.identity_policy is not None and (status.identity_policy.tokens_per_day, status.identity_policy.storage_bytes) == (500, 2000)
    assert status.container_policy is not None and status.container_policy.policy_id == "container-ceiling"
    assert status.tokens_used_today == 160
    assert status.storage_bytes_used == 1000
    with engine.connect() as conn:
        assert storage_bytes_used_on_connection(conn, identity_id="bob") == 999
        assert storage_bytes_used_on_connection(conn, identity_id="root") == 0


def test_status_reports_unknown_token_usage_as_none_never_zero(engine: Engine) -> None:
    now = _seed(engine)
    with engine.begin() as conn:
        _make_session(conn, session_id="alice-1", user_id="alice")
        _ledger(conn, entry_id="known", identity_id="alice", session_id="alice-1", prompt=10, completion=10, at=now)
    authority = RepositoryQuotaPolicyAuthority(engine)
    assert authority.status(identity_id="alice").tokens_used_today == 20
    # Mutation-derivation: one unreported measure on the ledger row makes the day unknown.
    with engine.begin() as conn:
        conn.execute(update(token_usage_ledger_table).where(token_usage_ledger_table.c.entry_id == "known").values(prompt_tokens=None))
    assert authority.status(identity_id="alice").tokens_used_today is None


def test_status_of_an_identity_without_a_policy_reports_none(engine: Engine) -> None:
    _seed(engine)
    status = RepositoryQuotaPolicyAuthority(engine).status(identity_id="bob")
    assert status.identity_policy is None
    assert status.tokens_used_today == 0
    assert status.storage_bytes_used == 0


# ── set ──────────────────────────────────────────────────────────────────


def test_set_one_dimension_revokes_the_old_row_copies_the_other_and_records_the_change(engine: Engine) -> None:
    _seed(engine)
    recorded: list[QuotaPolicySet] = []
    change = _set(RepositoryQuotaPolicyAuthority(engine), record=recorded.append)
    assert recorded == [change]
    assert change.dimension == "storage"
    assert (change.policy.tokens_per_day, change.policy.storage_bytes) == (500, 3000)
    assert change.previous is not None and change.previous.policy_id == "alice-policy"
    (active,) = _active_rows(engine, "alice")
    assert active.policy_id == change.policy.policy_id
    assert active.set_by_actor == "identity"
    assert active.set_by_identity_id == "root"
    with engine.connect() as conn:
        revoked = conn.execute(select(quota_policies_table.c.revoked_at).where(quota_policies_table.c.policy_id == "alice-policy")).one()
    assert revoked.revoked_at is not None


def test_a_failing_record_callback_rolls_the_whole_edit_back(engine: Engine) -> None:
    """R4: the audit row is written before commit; no audit, no edit."""
    _seed(engine)

    def fail(_change: QuotaPolicySet) -> None:
        raise RuntimeError("Auth audit recorder is closed")

    with pytest.raises(RuntimeError, match="Auth audit recorder is closed"):
        _set(RepositoryQuotaPolicyAuthority(engine), record=fail)
    (active,) = _active_rows(engine, "alice")
    assert active.policy_id == "alice-policy"


def test_set_without_a_policy_takes_the_other_dimension_from_the_container_default(engine: Engine) -> None:
    _seed(engine)
    authority = RepositoryQuotaPolicyAuthority(engine)
    with pytest.raises(QuotaDefaultMissing, match="identity bob has no quota policy and no container default for storage"):
        _set(authority, identity_id="bob", dimension="tokens", value=42)
    assert _active_rows(engine, "bob") == []
    # Mutation-derivation: supplying the container default admits the same edit.
    change = _set(authority, identity_id="bob", dimension="tokens", value=42, default_storage_bytes=1_000_000)
    assert (change.policy.tokens_per_day, change.policy.storage_bytes) == (42, 1_000_000)
    assert change.previous is None


def test_set_by_a_non_admin_is_refused_and_derives_from_the_role_row(engine: Engine) -> None:
    _seed(engine)
    authority = RepositoryQuotaPolicyAuthority(engine)
    with pytest.raises(QuotaSetterNotAdmin, match="identity bob does not hold a live admin role"):
        _set(authority, actor_identity_id="bob")
    with engine.begin() as conn:
        conn.execute(
            insert(identity_roles_table).values(
                role_id="bob-admin", identity_id="bob", role="admin", granted_at=datetime.now(UTC) - timedelta(hours=1), granted_by_identity_id="root"
            )
        )
    assert _set(authority, actor_identity_id="bob").actor_identity_id == "bob"
    with engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == "bob-admin").values(revoked_at=datetime.now(UTC)))
    with pytest.raises(QuotaSetterNotAdmin, match="identity bob does not hold a live admin role"):
        _set(authority, actor_identity_id="bob", value=4000)


def test_set_on_a_pending_identity_is_refused_and_an_unknown_one_is_not_found(engine: Engine) -> None:
    _seed(engine)
    authority = RepositoryQuotaPolicyAuthority(engine)
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="pending"))
    with pytest.raises(QuotaTargetNotActive, match="identity alice is pending, not active"):
        _set(authority)
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="active"))
    assert _set(authority).identity_id == "alice"
    with pytest.raises(QuotaTargetNotFound, match="identity nobody not found"):
        _set(authority, identity_id="nobody")


@pytest.mark.parametrize("value", [0, -1, MAX_QUOTA_VALUE + 1])
def test_set_refuses_a_value_outside_the_integer_column(engine: Engine, value: int) -> None:
    _seed(engine)
    with pytest.raises(QuotaValueOutOfRange, match=f"quota value must be an integer between 1 and {MAX_QUOTA_VALUE}"):
        _set(RepositoryQuotaPolicyAuthority(engine), value=value)
    (active,) = _active_rows(engine, "alice")
    assert active.policy_id == "alice-policy"
```

- [ ] **Step 6: Run the quota-policy authority tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_quota_policy_authority.py -n 0 > /tmp/i9-lane-quota-red.log 2>&1; echo exit=$?`
Expected: `exit=2` (collection error); the log shows `ImportError: cannot import name 'MAX_QUOTA_VALUE' from 'elspeth.web.coordination.quota_authority'`.

- [ ] **Step 7: Write the quota-policy authority.**

In `src/elspeth/web/coordination/quota_authority.py` (I1's module), make these import edits.

Replace `from collections.abc import Mapping, Sequence` with:

```python
from collections.abc import Callable, Mapping, Sequence
```

Replace `from typing import Any, Literal, final` with:

```python
from typing import Any, Final, Literal, final
```

Replace `from sqlalchemy import case, func, insert, or_, select` with:

```python
from sqlalchemy import case, func, insert, or_, select, update
```

Replace `from sqlalchemy.engine import Connection` with:

```python
from sqlalchemy.engine import Connection, Engine
```

Replace `from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection` with:

```python
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.mutation_connection_registry import (
    _register_mutation_connection,
    _resolve_mutation_connection,
    _unregister_mutation_connection,
)
```

Replace `from elspeth.web.sessions.models import quota_policies_table, sessions_table, token_usage_ledger_table` with:

```python
from elspeth.web.sessions.models import (
    blobs_table,
    identities_table,
    identity_roles_table,
    quota_policies_table,
    sessions_table,
    token_usage_ledger_table,
)
```

Append at the end of the module, after `RepositoryQuotaAuthority.active_policy`:

```python
# ---------------------------------------------------------------------------
# Task I9: quota status for the admin identity row and the blob list, and the
# admin editor's one-dimension policy write (spec :1199-1203, D15, D18).
# ---------------------------------------------------------------------------

QuotaDimension = Literal["tokens", "storage"]
MAX_QUOTA_VALUE: Final = 2_147_483_647
"""The largest value the ``Integer`` ``tokens_per_day`` / ``storage_bytes`` columns hold on PostgreSQL."""


def storage_bytes_used_on_connection(connection: Connection, *, identity_id: str) -> int:
    """``SUM(blobs.size_bytes)`` over every blob row of every session the identity owns (D18).

    A standing level, not a rate. Archived sessions and pending or error rows
    count because each still occupies the share. R13's admission (Task I2) and
    the I9 status reads must report the same number, so this is the one
    statement that computes it.
    """
    total = connection.execute(
        select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0))
        .select_from(blobs_table.join(sessions_table, sessions_table.c.id == blobs_table.c.session_id))
        .where(sessions_table.c.user_id == identity_id)
    ).scalar_one()
    return int(total)


@final
@dataclass(frozen=True, slots=True)
class IdentityQuotaStatus:
    """What the admin identity row and the blob list render (spec :1199-1203)."""

    identity_id: str
    identity_policy: QuotaPolicyRow | None
    container_policy: QuotaPolicyRow | None
    tokens_used_today: int | None
    storage_bytes_used: int


@final
@dataclass(frozen=True, slots=True)
class QuotaPolicySet:
    """One admin quota edit, handed to the ``record`` callback before commit (R4)."""

    identity_id: str
    actor_identity_id: str
    dimension: QuotaDimension
    policy: QuotaPolicyRow
    previous: QuotaPolicyRow | None


class QuotaPolicyRefusal(RuntimeError):
    """Base of the closed refusal set the quota routes translate."""


class QuotaSetterNotAdmin(QuotaPolicyRefusal):
    """The actor is not an active identity holding a live, deployment-wide ``admin`` grant."""


class QuotaTargetNotFound(QuotaPolicyRefusal):
    """No identity row carries the target id."""


class QuotaTargetNotActive(QuotaPolicyRefusal):
    """Quota rows are written for active identities only; activation writes the first one."""


class QuotaDefaultMissing(QuotaPolicyRefusal):
    """The other dimension has neither an active row to copy nor a container default."""


class QuotaValueOutOfRange(QuotaPolicyRefusal):
    """The new cap is not an integer in ``[1, MAX_QUOTA_VALUE]``."""


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _policy_row(conn: Connection, *, identity_id: str | None) -> QuotaPolicyRow | None:
    policy = quota_policies_table.c
    owner = policy.identity_id.is_(None) if identity_id is None else policy.identity_id == identity_id
    row = conn.execute(select(policy.policy_id, policy.tokens_per_day, policy.storage_bytes).where(owner, policy.revoked_at.is_(None))).one_or_none()
    if row is None:
        return None
    return QuotaPolicyRow(policy_id=row.policy_id, tokens_per_day=row.tokens_per_day, storage_bytes=row.storage_bytes)


def _holds_live_admin(conn: Connection, *, identity_id: str, now: datetime) -> bool:
    role = identity_roles_table.c
    rows = conn.execute(
        select(role.expires_at).where(
            role.identity_id == identity_id,
            role.role == "admin",
            role.scope.is_(None),
            role.revoked_at.is_(None),
        )
    ).all()
    return any(row.expires_at is None or _as_utc(row.expires_at) > now for row in rows)


@final
class RepositoryQuotaPolicyAuthority:
    """Engine-owning quota reads and the admin editor's write.

    ``quota_policies`` has global scope in the mutation-authority manifest, so
    no session fence applies. The identity rows are the lock (R4): both
    participating ``identities`` rows are taken ``FOR UPDATE`` in stable id
    order, the actor's admin grant is re-proved at database time, and the
    active policy row is locked, revoked and replaced.
    ``uq_quota_policies_active_per_identity`` admits one active row per
    identity, so the revoke precedes the insert.
    """

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def status(self, *, identity_id: str) -> IdentityQuotaStatus:
        if type(identity_id) is not str or not identity_id.strip():
            raise ValueError("identity_id must be a non-blank string")
        with self._engine.begin() as conn:
            now = database_now(conn)
            token = _register_mutation_connection(conn)
            try:
                tokens_used = RepositoryQuotaAuthority.daily_token_total(token, identity_id=identity_id, day_start_utc=utc_day_start(now))
            finally:
                _unregister_mutation_connection(token)
            identity_policy = _policy_row(conn, identity_id=identity_id)
            container_policy = _policy_row(conn, identity_id=None)
            storage_used = storage_bytes_used_on_connection(conn, identity_id=identity_id)
        return IdentityQuotaStatus(
            identity_id=identity_id,
            identity_policy=identity_policy,
            container_policy=container_policy,
            tokens_used_today=tokens_used,
            storage_bytes_used=storage_used,
        )

    def set_identity_policy(
        self,
        *,
        actor_identity_id: str,
        identity_id: str,
        dimension: QuotaDimension,
        value: int,
        default_tokens_per_day: int | None,
        default_storage_bytes: int | None,
        record: Callable[[QuotaPolicySet], None],
    ) -> QuotaPolicySet:
        if dimension not in ("tokens", "storage"):
            raise ValueError(f"dimension must be tokens or storage, not {dimension!r}")
        if type(value) is not int or value < 1 or value > MAX_QUOTA_VALUE:
            raise QuotaValueOutOfRange(f"quota value must be an integer between 1 and {MAX_QUOTA_VALUE}")
        if not callable(record):
            raise TypeError("record must be callable")
        identities = identities_table.c
        policy = quota_policies_table.c
        with self._engine.begin() as conn:
            now = database_now(conn)
            for locked_id in sorted({actor_identity_id, identity_id}):
                conn.execute(select(identities.identity_id).where(identities.identity_id == locked_id).with_for_update()).one_or_none()
            actor = conn.execute(select(identities.access_state).where(identities.identity_id == actor_identity_id)).one_or_none()
            if actor is None or actor.access_state != "active" or not _holds_live_admin(conn, identity_id=actor_identity_id, now=now):
                raise QuotaSetterNotAdmin(f"identity {actor_identity_id} does not hold a live admin role")
            target = conn.execute(select(identities.access_state).where(identities.identity_id == identity_id)).one_or_none()
            if target is None:
                raise QuotaTargetNotFound(f"identity {identity_id} not found")
            if target.access_state != "active":
                raise QuotaTargetNotActive(f"identity {identity_id} is {target.access_state}, not active")
            current = conn.execute(
                select(policy.policy_id, policy.tokens_per_day, policy.storage_bytes, policy.dual_control_above_tokens)
                .where(policy.identity_id == identity_id, policy.revoked_at.is_(None))
                .with_for_update()
            ).one_or_none()
            previous = (
                None
                if current is None
                else QuotaPolicyRow(policy_id=current.policy_id, tokens_per_day=current.tokens_per_day, storage_bytes=current.storage_bytes)
            )
            if dimension == "tokens":
                tokens_per_day: int | None = value
                storage_bytes = default_storage_bytes if previous is None else previous.storage_bytes
                other = "storage"
            else:
                tokens_per_day = default_tokens_per_day if previous is None else previous.tokens_per_day
                storage_bytes = value
                other = "tokens"
            if tokens_per_day is None or storage_bytes is None:
                raise QuotaDefaultMissing(f"identity {identity_id} has no quota policy and no container default for {other}")
            if current is not None:
                conn.execute(
                    update(quota_policies_table)
                    .where(policy.policy_id == current.policy_id, policy.revoked_at.is_(None))
                    .values(revoked_at=now)
                )
            policy_id = str(uuid.uuid4())
            conn.execute(
                insert(quota_policies_table).values(
                    policy_id=policy_id,
                    identity_id=identity_id,
                    tokens_per_day=tokens_per_day,
                    storage_bytes=storage_bytes,
                    dual_control_above_tokens=None if current is None else current.dual_control_above_tokens,
                    set_by_identity_id=actor_identity_id,
                    set_by_actor="identity",
                    set_at=now,
                    revoked_at=None,
                )
            )
            change = QuotaPolicySet(
                identity_id=identity_id,
                actor_identity_id=actor_identity_id,
                dimension=dimension,
                policy=QuotaPolicyRow(policy_id=policy_id, tokens_per_day=tokens_per_day, storage_bytes=storage_bytes),
                previous=previous,
            )
            record(change)
        return change
```

- [ ] **Step 8: Run the quota-policy authority tests and I1's quota suite to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_quota_policy_authority.py tests/unit/web/coordination/test_quota_authority.py -n 0 > /tmp/i9-lane-quota-green.log 2>&1; echo exit=$?`
Expected: `exit=0`; `test_quota_policy_authority.py` contributes `11 passed` (eight test functions, the range test parametrised three times).

- [ ] **Step 9: Write the failing `quota_set` audit-writer tests.**

Append to the end of `tests/unit/web/auth/test_audit.py` (the helpers `_request` :131, `_durable_recorder` :629, `_durable_rows` :634 and `_metadata` :643 already exist):

```python
# ── Task I9: the admin quota editor's quota_set row ──────────────────────


def test_quota_set_row_carries_dimension_cap_and_both_policy_ids(tmp_path: Any) -> None:
    """Spec :833-835: ``quota_set`` carries its dimension and the cap; the row anchors on the identity whose allowance changed."""
    from elspeth.web.coordination.quota_authority import QuotaPolicyRow, QuotaPolicySet

    recorder, landscape_url = _durable_recorder(tmp_path)
    change = QuotaPolicySet(
        identity_id="alice",
        actor_identity_id="root",
        dimension="storage",
        policy=QuotaPolicyRow(policy_id="policy-new", tokens_per_day=1000, storage_bytes=9000),
        previous=QuotaPolicyRow(policy_id="policy-old", tokens_per_day=1000, storage_bytes=5000),
    )
    recorder.record_quota_set(_request(), provider="local", change=change)
    (row,) = [row for row in _durable_rows(landscape_url) if row.event_type == "quota_set"]
    assert row.identity_id == "alice"
    assert row.outcome == "success"
    assert row.failure_category is None
    metadata = _metadata(row)
    assert metadata["actor"] == "root"
    assert metadata["on_behalf_of"] is None
    assert metadata["console_request_id"] is None
    assert metadata["source"] == "admin"
    assert metadata["dimension"] == "storage"
    assert metadata["cap"] == 9000
    assert metadata["previous_cap"] == 5000
    assert metadata["tokens_per_day"] == 1000
    assert metadata["storage_bytes"] == 9000
    assert metadata["policy_id"] == "policy-new"
    assert metadata["revoked_policy_id"] == "policy-old"


def test_quota_set_first_policy_has_no_previous_cap_and_the_tokens_cap_follows_the_dimension(tmp_path: Any) -> None:
    from elspeth.web.coordination.quota_authority import QuotaPolicyRow, QuotaPolicySet

    recorder, landscape_url = _durable_recorder(tmp_path)
    change = QuotaPolicySet(
        identity_id="bob",
        actor_identity_id="root",
        dimension="tokens",
        policy=QuotaPolicyRow(policy_id="policy-first", tokens_per_day=42, storage_bytes=1_000_000),
        previous=None,
    )
    recorder.record_quota_set(None, provider="local", change=change)
    (row,) = [row for row in _durable_rows(landscape_url) if row.event_type == "quota_set"]
    metadata = _metadata(row)
    assert metadata["cap"] == 42
    assert metadata["previous_cap"] is None
    assert metadata["revoked_policy_id"] is None
    assert row.request_id is None
    assert row.client_host is None
```

In `tests/unit/web/auth/test_identity_admin_routes.py`, directly above `    def only(self, method: str) -> _AuditCall:` (:97), add the fake member, so the class docstring "Every `AuthAuditWriter` member, explicit" stays true:

```python
    def record_quota_set(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_quota_set", request, kwargs)

```

- [ ] **Step 10: Run the audit tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/auth/test_audit.py -k quota_set -n 0 > /tmp/i9-lane-audit-red.log 2>&1; echo exit=$?`
Expected: `exit=1`; both tests fail with `AttributeError: 'AuthAuditRecorder' object has no attribute 'record_quota_set'`.

- [ ] **Step 11: Add `record_quota_set` to the writer Protocol, the operation enum and the recorder.**

In `src/elspeth/web/auth/audit.py`, inside the `if TYPE_CHECKING:` block I1 extended, replace

```python
    from elspeth.web.coordination.quota_authority import QuotaExceeded
```

with

```python
    from elspeth.web.coordination.quota_authority import QuotaExceeded, QuotaPolicySet
```

Directly above `AdminActivationCause = Literal["admin_activation", "pre_provision", "bootstrap"]` (HEAD :255), as the Protocol's last member, add:

```python
    # Task I9: the admin quota editor. Request-bound like every admin mutation;
    # the row is written inside the policy transaction, before commit (R4).
    def record_quota_set(self, request: Request | None, *, provider: AuthProviderType, change: QuotaPolicySet) -> None:
        """Write the Landscape ``quota_set`` row for one admin quota edit."""


```

Directly above `def _bounded_text(` (HEAD :290), as the last `AuthAuditOperation` member, add:

```python
    QUOTA_SET = "quota_set"


```

Directly above `class _AdminProvenanceMetadata(TypedDict):` (HEAD :1180), as the recorder's last method, add:

```python
    def record_quota_set(self, request: Request | None, *, provider: AuthProviderType, change: QuotaPolicySet) -> None:
        """One ``quota_set`` row per admin edit, anchored on the identity whose allowance changed (spec :833-835).

        ``cap`` is the new value of the edited dimension and ``previous_cap``
        the value it replaced (``None`` for a first policy). Both full column
        values travel too, because the editor copies the other dimension
        forward and an administrator reading the trail must see what was
        copied.
        """
        provenance = _admin_provenance(request, actor_identity_id=change.actor_identity_id, on_behalf_of=None, console_request_id=None)
        if change.dimension == "tokens":
            cap = change.policy.tokens_per_day
            previous_cap = None if change.previous is None else change.previous.tokens_per_day
        else:
            cap = change.policy.storage_bytes
            previous_cap = None if change.previous is None else change.previous.storage_bytes
        with self._open_landscape(AuthAuditOperation.QUOTA_SET) as db:
            self._auth_audit(db).record_auth_event(
                event_type="quota_set",
                outcome="success",
                provider=provider,
                identity_id=change.identity_id,
                user_id=None,
                username=None,
                failure_category=None,
                metadata={
                    **provenance.metadata,
                    "source": "admin",
                    "dimension": change.dimension,
                    "cap": cap,
                    "previous_cap": previous_cap,
                    "tokens_per_day": change.policy.tokens_per_day,
                    "storage_bytes": change.policy.storage_bytes,
                    "policy_id": change.policy.policy_id,
                    "revoked_policy_id": None if change.previous is None else change.previous.policy_id,
                },
                **provenance.request_columns,
            )

```

- [ ] **Step 12: Run the whole audit and identity-admin route suites to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py -n 0 > /tmp/i9-lane-audit-green.log 2>&1; echo exit=$?`
Expected: `exit=0`; the two `quota_set` tests are among the passes.

- [ ] **Step 13: Write the failing mailbox and quota route tests.**

Create `tests/unit/web/workflow/test_mailbox_routes.py`:

```python
"""Mailbox routes over REAL approval, review and identity authorities on I8's ``closed_local_app``.

Approvals and review requests are created through the authorities directly;
I3's and I4's route tests pin that path. What these tests pin is the folder
contents, the counts, the seen stamp and the approver picker, each derived from
the rows that authorise it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi import Request
from sqlalchemy import insert, select, update
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.approval_authority import (
    ApprovalBinding,
    ApprovalRecord,
    ApprovalTransactionAuthority,
    RepositoryApprovalAuthority,
)
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.review_authority import RepositoryReviewAuthority
from elspeth.web.sessions.models import approvals_table, audit_access_log_table, identity_relationships_table, identity_roles_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.routes.workflow.approvals import create_approvals_router
from elspeth.web.sessions.routes.workflow.inspect import create_workflow_inspect_router
from elspeth.web.sessions.routes.workflow.mailbox import create_mailbox_router, decision_is_unseen

BINDING = ApprovalBinding(
    config_hash="1" * 64,
    canonical_version="sha256-rfc8785-v1",
    runtime_val_manifest_sha256="3" * 64,
    openrouter_catalog_sha256="2" * 64,
    binding_generation_fingerprint="c" * 64,
    policy_hash="a" * 64,
)


@pytest.fixture
def app(closed_local_app: Any) -> Any:
    """bob and carol approve, erin reviews, dave is a plain user; carol holds an approver edge over alice."""
    engine = closed_local_app.app.state.phase3_engine
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("bob", "carol", "dave", "erin"):
            ensure_test_identity(conn, identity_id=identity_id)
        for role_id, identity_id, role in (
            ("role-bob", "bob", "approver"),
            ("role-carol", "carol", "approver"),
            ("role-erin", "erin", "reviewer"),
        ):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=role_id, identity_id=identity_id, role=role, granted_at=now, granted_by_identity_id="alice"
                )
            )
        conn.execute(
            insert(identity_relationships_table).values(
                relationship_id="edge-carol-alice",
                from_identity_id="carol",
                to_identity_id="alice",
                relationship_type="approver",
                asserted_by_identity_id="alice",
                asserted_at=now,
            )
        )
    state = closed_local_app.app.state
    state.approval_authority = ApprovalTransactionAuthority(engine)
    state.review_authority = RepositoryReviewAuthority(engine)
    state.identity_authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
    closed_local_app.app.include_router(create_mailbox_router())
    return closed_local_app


def _as(client: Any, identity_id: str) -> None:
    identity = UserIdentity(user_id=identity_id, username=identity_id)

    async def user() -> UserIdentity:
        return identity

    client.app.dependency_overrides[get_current_user] = user


def _governance(client: Any, value: str) -> None:
    client.app.state.settings = client.app.state.settings.model_copy(update={"workflow_governance": value})


def _save_state(client: Any, session_id: str) -> str:
    service = client.app.state.phase3_sessions_service

    async def save() -> str:
        lease = await SessionOperationLease.acquire(
            service.session_operation_authority,
            session_id=UUID(session_id),
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
        async with lease:
            record = await service.save_composition_state(
                UUID(session_id),
                CompositionStateData(
                    sources={},
                    nodes=[],
                    edges=[],
                    outputs=[],
                    metadata_={"name": "demo", "description": ""},
                    is_valid=False,
                    validation_errors=None,
                ),
                provenance="session_seed",
                session_operation_context=lease.context,
            )
        return str(record.id)

    return asyncio.run(save())


def _session(client: Any, owner: str) -> tuple[str, str]:
    _as(client, owner)
    created = client.post("/api/sessions", json={"title": f"{owner}'s pipeline"})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    return session_id, _save_state(client, session_id)


def _ignore(_record: Any) -> None:
    return None


def _approval(client: Any, session: tuple[str, str], *, requested_by: str, approver: str) -> str:
    session_id, state_id = session

    def mutation(token: str, now: datetime) -> ApprovalRecord:
        return RepositoryApprovalAuthority.request(
            token,
            session_id=session_id,
            state_id=state_id,
            binding=BINDING,
            requested_by=requested_by,
            approver=approver,
            note="please",
            now=now,
            record=_ignore,
        )

    return client.app.state.approval_authority.run(session_id, mutation).approval_id


def _decide(client: Any, approval_id: str, *, decided_by: str, decision: str, note: str | None) -> None:
    authority = client.app.state.approval_authority
    session_id = authority.session_id_of(approval_id)

    def mutation(token: str, now: datetime) -> ApprovalRecord:
        return RepositoryApprovalAuthority.decide(
            token, approval_id=approval_id, decided_by=decided_by, decision=decision, note=note, now=now, record=_ignore
        )

    authority.run(session_id, mutation)


def _summary(client: Any, identity_id: str) -> dict[str, Any]:
    _as(client, identity_id)
    response = client.get("/api/workflow/mailbox/summary")
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


def test_summary_counts_each_folder_and_reports_live_roles(app: Any) -> None:
    _approval(app, _session(app, "alice"), requested_by="alice", approver="bob")
    _approval(app, _session(app, "dave"), requested_by="dave", approver="carol")
    alice_session = _session(app, "alice")
    app.app.state.review_authority.request(
        session_id=alice_session[0], state_id=alice_session[1], requested_by="alice", reviewer=None, note="look", record=_ignore
    )
    # dave's request is addressed to carol, so bob counts only alice's (decision 13).
    assert _summary(app, "bob") == {
        "governance": "on",
        "roles": ["approver"],
        "approvals_to_decide": 1,
        "reviews_to_attest": 0,
        "decisions_unseen": 0,
    }
    assert _summary(app, "erin")["reviews_to_attest"] == 1
    assert _summary(app, "erin")["roles"] == ["reviewer"]
    assert _summary(app, "alice")["approvals_to_decide"] == 0, "alice holds no approver role"
    # Mutation-derivation: revoke bob's ROLE ROW and both his count and his roles empty.
    with app.app.state.phase3_engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == "role-bob").values(revoked_at=datetime.now(UTC)))
    assert _summary(app, "bob")["approvals_to_decide"] == 0
    assert _summary(app, "bob")["roles"] == []


def test_inbox_lists_only_requests_addressed_to_the_caller(app: Any) -> None:
    alice_to_bob = _approval(app, _session(app, "alice"), requested_by="alice", approver="bob")
    dave_to_carol = _approval(app, _session(app, "dave"), requested_by="dave", approver="carol")
    bob_to_carol = _approval(app, _session(app, "bob"), requested_by="bob", approver="carol")
    _as(app, "bob")
    bob_inbox = app.get("/api/workflow/mailbox/inbox").json()
    assert [row["approval_id"] for row in bob_inbox["approvals"]] == [alice_to_bob], "never dave's request, never his own"
    assert bob_inbox["reviews"] == []
    _as(app, "carol")
    carol_ids = [row["approval_id"] for row in app.get("/api/workflow/mailbox/inbox").json()["approvals"]]
    assert sorted(carol_ids) == sorted([dave_to_carol, bob_to_carol]), "never alice's request to bob"
    _as(app, "dave")
    assert app.get("/api/workflow/mailbox/inbox").json() == {"approvals": [], "reviews": []}, "dave holds no approver role"
    # Mutation-derivation: re-address alice's ROW to carol and it moves from bob's folder to carol's.
    with app.app.state.phase3_engine.begin() as conn:
        conn.execute(update(approvals_table).where(approvals_table.c.approval_id == alice_to_bob).values(approver_identity_id="carol"))
    _as(app, "bob")
    assert app.get("/api/workflow/mailbox/inbox").json()["approvals"] == []
    _as(app, "carol")
    moved = [row["approval_id"] for row in app.get("/api/workflow/mailbox/inbox").json()["approvals"]]
    assert sorted(moved) == sorted([alice_to_bob, dave_to_carol, bob_to_carol])


def test_inbox_reviews_are_listed_for_a_live_reviewer_only(app: Any) -> None:
    session_id, state_id = _session(app, "alice")
    request = app.app.state.review_authority.request(
        session_id=session_id, state_id=state_id, requested_by="alice", reviewer=None, note="look", record=_ignore
    )
    _as(app, "erin")
    reviews = app.get("/api/workflow/mailbox/inbox").json()["reviews"]
    assert [row["request_id"] for row in reviews] == [request.request_id]
    # Mutation-derivation: revoking erin's reviewer ROLE ROW empties her review folder.
    with app.app.state.phase3_engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == "role-erin").values(revoked_at=datetime.now(UTC)))
    assert app.get("/api/workflow/mailbox/inbox").json()["reviews"] == []


def test_sent_and_seen_round_trip_clears_the_unseen_count(app: Any) -> None:
    approval_id = _approval(app, _session(app, "alice"), requested_by="alice", approver="bob")
    _as(app, "alice")
    opened = app.post(f"/api/workflow/mailbox/{approval_id}/seen")
    assert opened.status_code == 200 and opened.json()["decision_seen_at"] is None, "an open request is not stamped"
    _decide(app, approval_id, decided_by="bob", decision="rejected", note="missing an owner")
    assert _summary(app, "alice")["decisions_unseen"] == 1
    (row,) = app.get("/api/workflow/mailbox/sent").json()["approvals"]
    assert row["decision"] == "rejected"
    assert row["decision_note"] == "missing an owner"
    assert row["decided_by_identity_id"] == "bob"
    # Another identity cannot clear it: the stamp is hidden, and alice's count stands.
    _as(app, "bob")
    hidden = app.post(f"/api/workflow/mailbox/{approval_id}/seen")
    assert hidden.status_code == 404
    assert hidden.json()["detail"]["error_type"] == "approval_not_found"
    assert _summary(app, "alice")["decisions_unseen"] == 1
    _as(app, "alice")
    seen = app.post(f"/api/workflow/mailbox/{approval_id}/seen")
    assert seen.status_code == 200
    assert seen.json()["decision_seen_at"] is not None
    assert _summary(app, "alice")["decisions_unseen"] == 0
    missing = app.post("/api/workflow/mailbox/no-such-approval/seen")
    assert missing.status_code == 404 and missing.json()["detail"]["error_type"] == "approval_not_found"


def test_withdrawn_and_superseded_requests_are_not_unseen_news(app: Any) -> None:
    session = _session(app, "alice")
    withdrawn_id = _approval(app, session, requested_by="alice", approver="bob")
    authority = app.app.state.approval_authority

    def withdraw(token: str, now: datetime) -> ApprovalRecord:
        return RepositoryApprovalAuthority.withdraw(token, approval_id=withdrawn_id, requested_by="alice", now=now, record=_ignore)

    authority.run(session[0], withdraw)
    _approval(app, (session[0], _save_state(app, session[0])), requested_by="alice", approver="bob")
    _save_state(app, session[0])
    _as(app, "alice")
    sent = app.get("/api/workflow/mailbox/sent").json()["approvals"]
    assert sorted(row["decision"] for row in sent) == ["revoked", "superseded"]
    assert _summary(app, "alice")["decisions_unseen"] == 0


def test_sent_lists_the_callers_review_requests_with_each_attestation_and_never_counts_them_unseen(app: Any) -> None:
    session_id, state_id = _session(app, "alice")
    reviews = app.app.state.review_authority
    requested = reviews.request(
        session_id=session_id, state_id=state_id, requested_by="alice", reviewer=None, note="check the joins", record=_ignore
    )
    _as(app, "alice")
    (waiting,) = app.get("/api/workflow/mailbox/sent").json()["reviews"]
    assert waiting["request"]["request_id"] == requested.request_id
    assert waiting["request"]["open"] is True
    assert waiting["attestations"] == []
    reviews.attest(
        session_id=session_id,
        state_id=state_id,
        payload_digest="sha256:" + "ab" * 32,
        reviewer="erin",
        verdict="changes_requested",
        note="rename the sink",
        record=_ignore,
    )
    _as(app, "alice")
    sent = app.get("/api/workflow/mailbox/sent")
    assert sent.status_code == 200, sent.text
    assert sent.headers["Cache-Control"] == "no-store"
    body = sent.json()
    assert body["approvals"] == []
    (reviewed,) = body["reviews"]
    assert reviewed["request"]["request_note"] == "check the joins"
    assert reviewed["request"]["open"] is False
    assert [(row["reviewer_identity_id"], row["verdict"], row["note"]) for row in reviewed["attestations"]] == [
        ("erin", "changes_requested", "rename the sink")
    ]
    # No seen column: a review outcome is shown, never counted as unread news.
    assert _summary(app, "alice")["decisions_unseen"] == 0
    # Requester-owned: the reviewer and a bystander read none of it.
    for other in ("erin", "bob"):
        _as(app, other)
        assert app.get("/api/workflow/mailbox/sent").json()["reviews"] == []
    # Mutation-derivation: the switch, not an empty table, empties the folder.
    _governance(app, "off")
    _as(app, "alice")
    assert app.get("/api/workflow/mailbox/sent").json() == {"approvals": [], "reviews": []}


def test_decision_is_unseen_counts_only_news_the_requester_did_not_cause() -> None:
    base = ApprovalRecord(
        approval_id="a",
        session_id="s",
        state_id="t",
        binding=BINDING,
        requested_by_identity_id="alice",
        approver_identity_id="bob",
        requested_at=datetime(2026, 9, 14, tzinfo=UTC),
        decided_at=datetime(2026, 9, 14, 1, tzinfo=UTC),
        decision="approved",
        request_note=None,
        decision_seen_at=None,
        decided_by_identity_id="bob",
        decision_note=None,
        revoked_by_identity_id=None,
        revocation_actor_kind=None,
        revocation_event_id=None,
    )
    assert decision_is_unseen(base, caller="alice") is True
    assert decision_is_unseen(replace(base, decision_seen_at=datetime(2026, 9, 14, 2, tzinfo=UTC)), caller="alice") is False
    assert decision_is_unseen(replace(base, decision=None, decided_at=None), caller="alice") is False
    assert decision_is_unseen(replace(base, decision="superseded"), caller="alice") is False
    own_withdrawal = replace(base, decision="revoked", revocation_actor_kind="identity", revoked_by_identity_id="alice")
    assert decision_is_unseen(own_withdrawal, caller="alice") is False
    # Mutation-derivation: the same revoked row written by the lifecycle (not the requester) IS news.
    assert decision_is_unseen(replace(own_withdrawal, revocation_actor_kind="system", revoked_by_identity_id=None), caller="alice") is True


def test_approvers_directory_lists_live_approvers_and_suggests_the_callers_edge(app: Any) -> None:
    _as(app, "alice")
    directory = app.get("/api/workflow/mailbox/approvers").json()
    assert directory == {
        "approvers": [{"identity_id": "bob", "username": "bob"}, {"identity_id": "carol", "username": "carol"}],
        "suggested_identity_ids": ["carol"],
    }
    _as(app, "bob")
    assert [entry["identity_id"] for entry in app.get("/api/workflow/mailbox/approvers").json()["approvers"]] == ["carol"]
    # Mutation-derivation: revoke carol's ROLE ROW; she leaves the picker and the suggestion.
    with app.app.state.phase3_engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == "role-carol").values(revoked_at=datetime.now(UTC)))
    _as(app, "alice")
    assert app.get("/api/workflow/mailbox/approvers").json() == {
        "approvers": [{"identity_id": "bob", "username": "bob"}],
        "suggested_identity_ids": [],
    }


def test_governance_off_zeroes_the_counts_empties_the_folders_and_refuses_seen(app: Any) -> None:
    approval_id = _approval(app, _session(app, "alice"), requested_by="alice", approver="bob")
    _governance(app, "off")
    assert _summary(app, "bob") == {
        "governance": "off",
        "roles": ["approver"],
        "approvals_to_decide": 0,
        "reviews_to_attest": 0,
        "decisions_unseen": 0,
    }
    _as(app, "bob")
    assert app.get("/api/workflow/mailbox/inbox").json() == {"approvals": [], "reviews": []}
    assert app.get("/api/workflow/mailbox/approvers").json() == {"approvers": [], "suggested_identity_ids": []}
    _as(app, "alice")
    assert app.get("/api/workflow/mailbox/sent").json() == {"approvals": [], "reviews": []}
    refused = app.post(f"/api/workflow/mailbox/{approval_id}/seen")
    assert refused.status_code == 409
    assert refused.json()["detail"]["error_type"] == "workflow_governance_off"
    # Mutation-derivation: the switch, not the fixture, zeroes the count.
    _governance(app, "on")
    assert _summary(app, "bob")["approvals_to_decide"] == 1


@dataclass
class _DecisionRecorder:
    """The approvals router's audit seam: records who decided, so a refused decide is provably silent."""

    actors: list[str] = field(default_factory=list)

    def record_approval_decided(self, request: Request | None, **kwargs: Any) -> None:
        assert request is not None, "the decide route records against the live request"
        self.actors.append(kwargs["actor_identity_id"])


@pytest.fixture
def three_party_app(app: Any) -> Any:
    """``app`` plus I7's inspect router and I3's approvals router: every surface an approver meets."""
    state = app.app.state
    state.audit_access_log_authority = RepositoryAuditAccessLogAuthority(state.phase3_engine)
    state.auth_audit_recorder = _DecisionRecorder()
    app.app.include_router(create_workflow_inspect_router())
    app.app.include_router(create_approvals_router())
    return app


def test_alice_bob_and_carol_meet_one_rule_at_inbox_inspect_and_decide(three_party_app: Any) -> None:
    """B2: the approver the inbox lists is the one inspect admits and the one decide admits; nobody else gets in."""
    app = three_party_app
    session = _session(app, "alice")
    approval_id = _approval(app, session, requested_by="alice", approver="bob")
    inspect_url = f"/api/workflow/inspect/{session[0]}/{session[1]}"
    decide_url = f"/api/approvals/{approval_id}/decide"
    approve = {"decision": "approved", "note": None}

    def inbox_ids(identity_id: str) -> list[str]:
        _as(app, identity_id)
        return [row["approval_id"] for row in app.get("/api/workflow/mailbox/inbox").json()["approvals"]]

    # Bob, the addressed approver: listed, and his inspect is admitted.
    assert inbox_ids("bob") == [approval_id]
    assert app.get(inspect_url).status_code == 200
    # Carol, a live approver the request is not addressed to: not listed, and hidden at inspect and decide.
    assert inbox_ids("carol") == []
    assert _summary(app, "carol")["approvals_to_decide"] == 0
    _as(app, "carol")
    assert app.get(inspect_url).status_code == 404
    carol = app.post(decide_url, json=approve)
    assert carol.status_code == 404, carol.text
    assert carol.json()["detail"] == {"error_type": "approval_not_found", "detail": f"approval {approval_id} not found"}
    # Alice, the author: not listed, hidden at inspect, refused as the author at decide.
    assert inbox_ids("alice") == []
    assert app.get(inspect_url).status_code == 404
    alice = app.post(decide_url, json=approve)
    assert alice.status_code == 409, alice.text
    assert alice.json()["detail"]["error_type"] == "approval_author_is_approver"
    assert app.app.state.auth_audit_recorder.actors == [], "no refused decide reached the audit seam"
    # Mutation-derivation: re-address the ROW to carol. All three surfaces follow the column, not the grant.
    with app.app.state.phase3_engine.begin() as conn:
        conn.execute(update(approvals_table).where(approvals_table.c.approval_id == approval_id).values(approver_identity_id="carol"))
    assert inbox_ids("bob") == []
    assert app.get(inspect_url).status_code == 404
    assert app.post(decide_url, json=approve).status_code == 404
    assert inbox_ids("carol") == [approval_id]
    assert app.get(inspect_url).status_code == 200
    decided = app.post(decide_url, json=approve)
    assert decided.status_code == 200, decided.text
    assert decided.json()["decided_by_identity_id"] == "carol"
    assert app.app.state.auth_audit_recorder.actors == ["carol"]
    # A decision ends access at every surface, the decider's included.
    assert inbox_ids("carol") == []
    assert app.get(inspect_url).status_code == 404
    with app.app.state.phase3_engine.connect() as conn:
        readers = [row.requesting_principal for row in conn.execute(select(audit_access_log_table)).all()]
    assert sorted(readers) == ["bob", "carol"], "only the two admitted inspects wrote an access row"
```

Create `tests/unit/web/workflow/test_quota_routes.py`:

```python
"""Quota status and editor routes over a REAL quota-policy authority on ``closed_local_app``.

The audit writer is a recording fake; that the Landscape row is written inside
the transaction is pinned in test_quota_policy_authority.py (a failing
callback rolls the edit back) and the row shape in tests/unit/web/auth/test_audit.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import Request
from sqlalchemy import insert, select, update
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.quota_authority import RepositoryQuotaPolicyAuthority
from elspeth.web.sessions.models import blobs_table, identities_table, identity_roles_table, quota_policies_table
from elspeth.web.sessions.routes.workflow.quota import create_quota_router


@dataclass
class _RecordingAuditWriter:
    calls: list[tuple[str, bool, dict[str, Any]]] = field(default_factory=list)

    def record_quota_set(self, request: Request | None, **kwargs: Any) -> None:
        self.calls.append(("record_quota_set", request is not None, kwargs))


@pytest.fixture
def app(closed_local_app: Any) -> Any:
    engine = closed_local_app.app.state.phase3_engine
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("root", "bob"):
            ensure_test_identity(conn, identity_id=identity_id)
        conn.execute(
            insert(identity_roles_table).values(
                role_id="root-admin", identity_id="root", role="admin", granted_at=now - timedelta(hours=1), granted_by_identity_id="root"
            )
        )
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="alice-policy", identity_id="alice", tokens_per_day=500, storage_bytes=2000, set_by_actor="system", set_at=now
            )
        )
    state = closed_local_app.app.state
    state.identity_authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
    state.quota_policy_authority = RepositoryQuotaPolicyAuthority(engine)
    state.auth_audit_recorder = _RecordingAuditWriter()
    closed_local_app.app.include_router(create_quota_router())
    return closed_local_app


def _as(client: Any, identity_id: str) -> None:
    identity = UserIdentity(user_id=identity_id, username=identity_id)

    async def user() -> UserIdentity:
        return identity

    client.app.dependency_overrides[get_current_user] = user


def test_me_reports_the_callers_policy_and_storage_level(app: Any) -> None:
    _as(app, "alice")
    session_id = app.post("/api/sessions", json={"title": "files"}).json()["id"]
    with app.app.state.phase3_engine.begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id="blob-1",
                session_id=session_id,
                filename="a.csv",
                mime_type="text/csv",
                size_bytes=700,
                storage_path="blobs/blob-1",
                created_at=datetime.now(UTC),
                created_by="user",
                status="ready",
            )
        )
    response = app.get("/api/workflow/quota/me")
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "identity_id": "alice",
        "tokens_per_day": 500,
        "storage_bytes": 2000,
        "container_tokens_per_day": None,
        "container_storage_bytes": None,
        "tokens_used_today": 0,
        "storage_bytes_used": 700,
    }


def test_identity_status_is_admin_only_and_hidden_otherwise(app: Any) -> None:
    _as(app, "alice")
    hidden = app.get("/api/workflow/quota/identities/alice")
    assert hidden.status_code == 404 and hidden.json() == {"detail": "Not found"}
    _as(app, "root")
    assert app.get("/api/workflow/quota/identities/alice").json()["tokens_per_day"] == 500
    # Mutation-derivation: revoke root's admin ROLE ROW and the route hides itself again.
    with app.app.state.phase3_engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == "root-admin").values(revoked_at=datetime.now(UTC)))
    assert app.get("/api/workflow/quota/identities/alice").status_code == 404


def test_set_one_dimension_keeps_the_other_and_audits_it(app: Any) -> None:
    _as(app, "root")
    response = app.post("/api/workflow/quota/identities/alice", json={"dimension": "storage", "value": 3000})
    assert response.status_code == 200, response.text
    assert (response.json()["tokens_per_day"], response.json()["storage_bytes"]) == (500, 3000)
    ((method, request_bound, kwargs),) = app.app.state.auth_audit_recorder.calls
    assert method == "record_quota_set" and request_bound is True
    assert kwargs["provider"] == "local"
    assert kwargs["change"].dimension == "storage"
    assert kwargs["change"].previous.policy_id == "alice-policy"
    with app.app.state.phase3_engine.connect() as conn:
        active = conn.execute(
            select(quota_policies_table.c.policy_id).where(quota_policies_table.c.identity_id == "alice", quota_policies_table.c.revoked_at.is_(None))
        ).all()
    assert [row.policy_id for row in active] == [kwargs["change"].policy.policy_id]


def test_set_without_a_policy_uses_the_container_default_for_the_other_dimension(app: Any) -> None:
    _as(app, "root")
    response = app.post("/api/workflow/quota/identities/bob", json={"dimension": "tokens", "value": 42})
    assert response.status_code == 200, response.text
    assert (response.json()["tokens_per_day"], response.json()["storage_bytes"]) == (42, 1_000_000)
    # Mutation-derivation: remove the container default (settings, not the route) and the same edit refuses.
    app.app.state.settings = app.app.state.settings.model_copy(update={"quota_default_tokens_per_day": None})
    with app.app.state.phase3_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="cara")
    refused = app.post("/api/workflow/quota/identities/cara", json={"dimension": "storage", "value": 5})
    assert refused.status_code == 409
    assert refused.json()["detail"]["error_type"] == "quota_default_missing"


def test_set_refuses_a_pending_identity_and_reports_an_unknown_one(app: Any) -> None:
    with app.app.state.phase3_engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "bob").values(access_state="pending"))
    _as(app, "root")
    pending = app.post("/api/workflow/quota/identities/bob", json={"dimension": "tokens", "value": 42})
    assert pending.status_code == 409
    assert pending.json()["detail"] == {"error_type": "quota_target_not_active", "detail": "identity bob is pending, not active"}
    unknown = app.post("/api/workflow/quota/identities/nobody", json={"dimension": "tokens", "value": 42})
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["error_type"] == "quota_target_not_found"
    assert app.app.state.auth_audit_recorder.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"dimension": "storage", "value": 0},
        {"dimension": "rows", "value": 10},
        {"dimension": "tokens", "value": "10"},
        {"dimension": "tokens", "value": 10, "note": "extra"},
    ],
)
def test_set_body_is_strict(app: Any, body: dict[str, Any]) -> None:
    _as(app, "root")
    assert app.post("/api/workflow/quota/identities/alice", json=body).status_code == 422
```

- [ ] **Step 14: Run the route tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_mailbox_routes.py tests/unit/web/workflow/test_quota_routes.py -n 0 > /tmp/i9-lane-routes-red.log 2>&1; echo exit=$?`
Expected: `exit=2` (collection errors); the log shows `ModuleNotFoundError: No module named 'elspeth.web.sessions.routes.workflow.mailbox'` and `ModuleNotFoundError: No module named 'elspeth.web.sessions.routes.workflow.quota'`.

- [ ] **Step 15: Write the mailbox and quota routers and register them.**

Create `src/elspeth/web/sessions/routes/workflow/mailbox.py`:

```python
"""The workflow mailbox: one surface, two folders, one badge (sso-design.md §Frontend → Mailbox, :1204-1234).

``GET /api/workflow/mailbox/summary`` feeds the navigation badge on the
frontend's single timer. ``/inbox`` lists approvals awaiting the caller's
decision and review requests the caller may attest. The approval folder is I3's
addressed-only ``inbox``: the approver I7's inspect admits and the only one I3's
``decide`` admits (I9 decision 13). ``/sent`` lists the caller's own
approval requests, and the caller's own review requests each with the
attestations I4's ``sent_for`` (I4 Step 3) attributes to it; a review outcome has no seen
column, so it is shown and never counted in ``decisions_unseen``.
``POST /{approval_id}/seen`` stamps ``decision_seen_at``, a
UI convenience that is never a control. ``/approvers`` is the approval picker:
live approvers other than the caller, with the caller's active ``approver``
edges as the default suggestion.

Every list is empty and every count is zero while ``workflow_governance`` is
not ``"on"``. ``roles`` is always reported, because the identity-administration
entry the frontend shows an ``admin`` does not depend on governance; every
admin route re-proves the role on each request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from elspeth.contracts.auth import IdentityRole
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.approval_authority import (
    ApprovalNotFound,
    ApprovalRecord,
    ApprovalTransactionAuthority,
    RepositoryApprovalAuthority,
)
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.review_authority import RepositoryReviewAuthority, ReviewSentRecord
from elspeth.web.sessions.routes.workflow.approvals import ApprovalView
from elspeth.web.sessions.routes.workflow.approvals import _view as _approval_view
from elspeth.web.sessions.routes.workflow.reviews import ReviewAttestationView, ReviewRequestView, _attestation_view, _request_view

MAILBOX_ROLES: Final[tuple[IdentityRole, ...]] = ("admin", "approver", "reviewer", "curator", "oversight")
"""The roles the frontend branches on, in the order the summary reports them."""

_DIRECTORY_PAGE: Final = 200
"""One page of ``list_roles`` / ``list_relationships``; equals the authority's ``_LIST_LIMIT_MAX``."""


class MailboxSummaryResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    governance: Literal["on", "off"]
    roles: list[IdentityRole]
    approvals_to_decide: int
    reviews_to_attest: int
    decisions_unseen: int


class MailboxInboxResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approvals: list[ApprovalView]
    reviews: list[ReviewRequestView]


class ReviewSentView(BaseModel):
    """One of the caller's review requests with the attestations I4's ``sent_for`` attributed to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ReviewRequestView
    attestations: list[ReviewAttestationView]


class MailboxSentResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approvals: list[ApprovalView]
    reviews: list[ReviewSentView]


class ApproverEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity_id: str
    username: str


class ApproverDirectoryResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approvers: list[ApproverEntry]
    suggested_identity_ids: list[str]


def decision_is_unseen(record: ApprovalRecord, *, caller: str) -> bool:
    """A decided request the requester has not opened, excluding the two outcomes the requester caused.

    ``superseded`` is written by the requester's own next state. A ``revoked``
    row whose revocation actor is the requester (``revocation_actor_kind ==
    "identity"``) is their own withdrawal. Neither is news. A lifecycle
    revocation, where the approver lost authority, is.
    """
    if record.decision is None or record.decision_seen_at is not None:
        return False
    if record.decision == "superseded":
        return False
    return not (record.decision == "revoked" and record.revocation_actor_kind == "identity" and record.revoked_by_identity_id == caller)


def _settings(request: Request) -> WebSettings:
    settings: WebSettings = request.app.state.settings
    return settings


def _governance_on(request: Request) -> bool:
    return _settings(request).workflow_governance == "on"


def _identity_authority(request: Request) -> RepositoryIdentityAuthority:
    authority: RepositoryIdentityAuthority = request.app.state.identity_authority
    return authority


def _approval_authority(request: Request) -> ApprovalTransactionAuthority:
    authority: ApprovalTransactionAuthority = request.app.state.approval_authority
    return authority


def _review_authority(request: Request) -> RepositoryReviewAuthority:
    authority: RepositoryReviewAuthority = request.app.state.review_authority
    return authority


def _uncacheable(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _not_found(approval_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"error_type": "approval_not_found", "detail": f"approval {approval_id} not found"})


def _review_sent_view(record: ReviewSentRecord) -> ReviewSentView:
    return ReviewSentView(
        request=_request_view(record.request),
        attestations=[_attestation_view(attestation) for attestation in record.attestations],
    )


def _held_roles(authority: RepositoryIdentityAuthority, identity_id: str) -> list[IdentityRole]:
    """Live, deployment-wide grants at database time (``active_roles`` filters revoked and expired rows)."""
    held = {grant.role for grant in authority.active_roles(identity_id=identity_id) if grant.scope is None}
    return [role for role in MAILBOX_ROLES if role in held]


def _approver_directory(authority: RepositoryIdentityAuthority, caller: str) -> ApproverDirectoryResponse:
    candidates: set[str] = set()
    offset = 0
    while True:
        grants = authority.list_roles(identity_id=None, include_revoked=False, limit=_DIRECTORY_PAGE, offset=offset)
        candidates.update(grant.identity_id for grant in grants if grant.role == "approver" and grant.scope is None and grant.identity_id != caller)
        if len(grants) < _DIRECTORY_PAGE:
            break
        offset += _DIRECTORY_PAGE
    approvers: list[ApproverEntry] = []
    for identity_id in sorted(candidates):
        # ``list_roles`` does not filter expiry; the live check is at database time.
        if not authority.holds_active_role(identity_id=identity_id, role="approver"):
            continue
        summary = authority.read_identity_summary(identity_id=identity_id)
        if summary is None or summary.access_state != "active":
            continue
        approvers.append(ApproverEntry(identity_id=identity_id, username=summary.username))
    eligible = {entry.identity_id for entry in approvers}
    suggested: set[str] = set()
    offset = 0
    while True:
        edges = authority.list_relationships(identity_id=caller, include_revoked=False, limit=_DIRECTORY_PAGE, offset=offset)
        suggested.update(
            edge.from_identity_id
            for edge in edges
            if edge.to_identity_id == caller and edge.relationship_type == "approver" and edge.from_identity_id in eligible
        )
        if len(edges) < _DIRECTORY_PAGE:
            break
        offset += _DIRECTORY_PAGE
    return ApproverDirectoryResponse(approvers=approvers, suggested_identity_ids=sorted(suggested))


def create_mailbox_router() -> APIRouter:
    router = APIRouter(tags=["workflow-mailbox"])

    @router.get("/api/workflow/mailbox/summary", response_model=MailboxSummaryResponse)
    async def mailbox_summary(
        request: Request,
        response: Response,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> MailboxSummaryResponse:
        _uncacheable(response)
        roles = await run_sync_in_worker(_held_roles, _identity_authority(request), user.user_id)
        if not _governance_on(request):
            return MailboxSummaryResponse(governance="off", roles=roles, approvals_to_decide=0, reviews_to_attest=0, decisions_unseen=0)
        approvals: tuple[ApprovalRecord, ...] = ()
        if "approver" in roles:
            approvals = await run_sync_in_worker(_approval_authority(request).inbox, approver_identity_id=user.user_id)
        reviews_to_attest = 0
        if "reviewer" in roles:
            reviews_to_attest = len(await run_sync_in_worker(_review_authority(request).open_for, reviewer=user.user_id))
        sent = await run_sync_in_worker(_approval_authority(request).sent, requested_by_identity_id=user.user_id)
        return MailboxSummaryResponse(
            governance="on",
            roles=roles,
            approvals_to_decide=len(approvals),
            reviews_to_attest=reviews_to_attest,
            decisions_unseen=sum(1 for record in sent if decision_is_unseen(record, caller=user.user_id)),
        )

    @router.get("/api/workflow/mailbox/inbox", response_model=MailboxInboxResponse)
    async def mailbox_inbox(
        request: Request,
        response: Response,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> MailboxInboxResponse:
        _uncacheable(response)
        if not _governance_on(request):
            return MailboxInboxResponse(approvals=[], reviews=[])
        roles = await run_sync_in_worker(_held_roles, _identity_authority(request), user.user_id)
        approvals: list[ApprovalView] = []
        if "approver" in roles:
            records = await run_sync_in_worker(_approval_authority(request).inbox, approver_identity_id=user.user_id)
            approvals = [_approval_view(record) for record in records]
        reviews: list[ReviewRequestView] = []
        if "reviewer" in roles:
            requests = await run_sync_in_worker(_review_authority(request).open_for, reviewer=user.user_id)
            reviews = [_request_view(record) for record in requests]
        return MailboxInboxResponse(approvals=approvals, reviews=reviews)

    @router.get("/api/workflow/mailbox/sent", response_model=MailboxSentResponse)
    async def mailbox_sent(
        request: Request,
        response: Response,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> MailboxSentResponse:
        _uncacheable(response)
        if not _governance_on(request):
            return MailboxSentResponse(approvals=[], reviews=[])
        records = await run_sync_in_worker(_approval_authority(request).sent, requested_by_identity_id=user.user_id)
        reviews = await run_sync_in_worker(_review_authority(request).sent_for, requested_by=user.user_id)
        return MailboxSentResponse(
            approvals=[_approval_view(record) for record in records],
            reviews=[_review_sent_view(record) for record in reviews],
        )

    @router.get("/api/workflow/mailbox/approvers", response_model=ApproverDirectoryResponse)
    async def mailbox_approvers(
        request: Request,
        response: Response,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ApproverDirectoryResponse:
        _uncacheable(response)
        if not _governance_on(request):
            return ApproverDirectoryResponse(approvers=[], suggested_identity_ids=[])
        return await run_sync_in_worker(_approver_directory, _identity_authority(request), user.user_id)

    @router.post("/api/workflow/mailbox/{approval_id}/seen", response_model=ApprovalView)
    async def mailbox_mark_seen(
        approval_id: str,
        request: Request,
        response: Response,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ApprovalView:
        if not _governance_on(request):
            raise HTTPException(
                status_code=409,
                detail={
                    "error_type": "workflow_governance_off",
                    "detail": "workflow governance is off on this deployment (set ELSPETH_WEB__WORKFLOW_GOVERNANCE=on)",
                },
            )
        authority = _approval_authority(request)
        session_id = await run_sync_in_worker(authority.session_id_of, approval_id)
        if session_id is None:
            raise _not_found(approval_id)

        def mutation(token: str, now: datetime) -> ApprovalRecord:
            return RepositoryApprovalAuthority.mark_decision_seen(token, approval_id=approval_id, requested_by=user.user_id, now=now)

        try:
            record = await run_sync_in_worker(authority.run, session_id, mutation)
        except ApprovalNotFound as exc:
            raise _not_found(approval_id) from exc
        _uncacheable(response)
        return _approval_view(record)

    return router
```

The three private imports (`_view`, `_request_view`, `_attestation_view`) are deliberate: the mailbox must render exactly the views I3 and I4 render, and a second projection would drift from them.

Create `src/elspeth/web/sessions/routes/workflow/quota.py`:

```python
"""Quota status and the admin quota editor (sso-design.md §Frontend :1197-1203, D15, D18).

``GET /api/workflow/quota/me`` gives any signed-in identity its own policy and
storage level; the blob list renders it so deletion is discoverable.
``GET`` and ``POST /api/workflow/quota/identities/{identity_id}`` sit behind a
live ``admin`` grant, checked per request and hidden as 404 otherwise, the
identity-admin router's rule. The authority re-proves the grant inside the
write transaction. One request edits one dimension; the other is copied forward
(``RepositoryQuotaPolicyAuthority.set_identity_policy``).

Quotas are enforced by R13/R14 whether or not ``workflow_governance`` is on,
so these routes do not read the switch.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.audit import AuthAuditWriter
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.quota_authority import (
    MAX_QUOTA_VALUE,
    IdentityQuotaStatus,
    QuotaPolicyRefusal,
    QuotaPolicySet,
    QuotaSetterNotAdmin,
    QuotaTargetNotFound,
    RepositoryQuotaPolicyAuthority,
)


class IdentityQuotaView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity_id: str
    tokens_per_day: int | None
    storage_bytes: int | None
    container_tokens_per_day: int | None
    container_storage_bytes: int | None
    tokens_used_today: int | None
    storage_bytes_used: int


class SetQuotaBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: Literal["tokens", "storage"]
    value: int = Field(ge=1, le=MAX_QUOTA_VALUE, strict=True)


def _authority(request: Request) -> RepositoryQuotaPolicyAuthority:
    authority: RepositoryQuotaPolicyAuthority = request.app.state.quota_policy_authority
    return authority


def _settings(request: Request) -> WebSettings:
    settings: WebSettings = request.app.state.settings
    return settings


def _hidden() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


def _uncacheable(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


async def _require_admin(request: Request) -> UserIdentity:
    user = await get_current_user(request)
    identity_authority: RepositoryIdentityAuthority = request.app.state.identity_authority
    if not await run_sync_in_worker(identity_authority.holds_active_role, identity_id=user.user_id, role="admin"):
        raise _hidden()
    return user


def _refusal_code(exc: QuotaPolicyRefusal) -> str:
    """``QuotaTargetNotActive`` becomes ``quota_target_not_active``."""
    name = type(exc).__name__
    return "".join(f"_{char.lower()}" if char.isupper() else char for char in name).lstrip("_")


def _refused(exc: QuotaPolicyRefusal) -> HTTPException:
    if type(exc) is QuotaSetterNotAdmin:
        return _hidden()
    status = 404 if type(exc) is QuotaTargetNotFound else 409
    return HTTPException(status_code=status, detail={"error_type": _refusal_code(exc), "detail": str(exc)})


def _view(status: IdentityQuotaStatus) -> IdentityQuotaView:
    return IdentityQuotaView(
        identity_id=status.identity_id,
        tokens_per_day=None if status.identity_policy is None else status.identity_policy.tokens_per_day,
        storage_bytes=None if status.identity_policy is None else status.identity_policy.storage_bytes,
        container_tokens_per_day=None if status.container_policy is None else status.container_policy.tokens_per_day,
        container_storage_bytes=None if status.container_policy is None else status.container_policy.storage_bytes,
        tokens_used_today=status.tokens_used_today,
        storage_bytes_used=status.storage_bytes_used,
    )


def create_quota_router() -> APIRouter:
    router = APIRouter(tags=["workflow-quota"])

    @router.get("/api/workflow/quota/me", response_model=IdentityQuotaView)
    async def my_quota(
        request: Request,
        response: Response,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> IdentityQuotaView:
        _uncacheable(response)
        return _view(await run_sync_in_worker(_authority(request).status, identity_id=user.user_id))

    @router.get("/api/workflow/quota/identities/{identity_id}", response_model=IdentityQuotaView)
    async def identity_quota(
        identity_id: str,
        request: Request,
        response: Response,
        admin: UserIdentity = Depends(_require_admin),  # noqa: B008
    ) -> IdentityQuotaView:
        _uncacheable(response)
        return _view(await run_sync_in_worker(_authority(request).status, identity_id=identity_id))

    @router.post("/api/workflow/quota/identities/{identity_id}", response_model=IdentityQuotaView)
    async def set_identity_quota(
        identity_id: str,
        body: SetQuotaBody,
        request: Request,
        response: Response,
        admin: UserIdentity = Depends(_require_admin),  # noqa: B008
    ) -> IdentityQuotaView:
        settings = _settings(request)
        recorder: AuthAuditWriter = request.app.state.auth_audit_recorder
        authority = _authority(request)

        def record(change: QuotaPolicySet) -> None:
            recorder.record_quota_set(request, provider=settings.auth_provider, change=change)

        try:
            await run_sync_in_worker(
                authority.set_identity_policy,
                actor_identity_id=admin.user_id,
                identity_id=identity_id,
                dimension=body.dimension,
                value=body.value,
                default_tokens_per_day=settings.quota_default_tokens_per_day,
                default_storage_bytes=settings.quota_default_storage_bytes,
                record=record,
            )
        except QuotaPolicyRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _view(await run_sync_in_worker(authority.status, identity_id=identity_id))

    return router
```

In `src/elspeth/web/app.py`:

1. Beside I7's `from elspeth.web.sessions.routes.workflow.audit_view import create_workflow_audit_view_router`, add

```python
from elspeth.web.sessions.routes.workflow.mailbox import create_mailbox_router
from elspeth.web.sessions.routes.workflow.quota import create_quota_router
```

   and beside `from elspeth.web.coordination.websocket_ticket_authority import RepositorySessionWebsocketTicketAuthority` (HEAD :107), add

```python
from elspeth.web.coordination.quota_authority import RepositoryQuotaPolicyAuthority
```

2. Directly after `app.state.identity_authority = identity_authority` (HEAD :1522), add

```python
    app.state.quota_policy_authority = RepositoryQuotaPolicyAuthority(session_engine)
```

3. Directly after I7's `app.include_router(create_workflow_audit_view_router())`, add

```python
    app.include_router(create_mailbox_router())
    app.include_router(create_quota_router())
```

- [ ] **Step 16: Run the route tests, the app-wiring suite and the neighbouring workflow suites to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow tests/unit/web/test_app.py tests/unit/web/coordination/test_approval_mailbox.py tests/unit/web/coordination/test_quota_policy_authority.py -n 0 > /tmp/i9-lane-routes-green.log 2>&1; echo exit=$?`
Expected: `exit=0`. `test_mailbox_routes.py` contributes `10 passed` and `test_quota_routes.py` `9 passed` (five functions, the strict-body test parametrised four times).

- [ ] **Step 17: Admit the new writers and connections to the Sessions mutation-authority manifest.**

The manifest is fail-closed. First read the drift report; the gate XFAILs on drift rather than failing:

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities -n 0 -v -rx > /tmp/i9-lane-manifest-drift.log 2>&1; echo exit=$?`
Expected: `exit=0` and `1 xfailed`. The reason text lists one `describe()` line per site, in the form `<path>:<line> <symbol> <operation> <table> fp=<16 hex>#<ordinal> authority=<name or UNCLASSIFIED> connection_escape=False`. The new sites are:
- under `Unexpected/unreviewed`: `src/elspeth/web/coordination/approval_authority.py:<line> RepositoryApprovalAuthority.mark_decision_seen update approvals` (authority `ApprovalAuthority`, through I3's class-prefix binding), `src/elspeth/web/coordination/quota_authority.py:<line> RepositoryQuotaPolicyAuthority.set_identity_policy update quota_policies` and `src/elspeth/web/coordination/quota_authority.py:<line> RepositoryQuotaPolicyAuthority.set_identity_policy insert quota_policies` (the scanner's own line text, with `UNCLASSIFIED` until the binding below lands);
- under `Connections outside exact contained authority`: `quota_authority.py:<line> RepositoryQuotaPolicyAuthority.set_identity_policy write_connection <sessions-write-connection>` and `quota_authority.py:<line> RepositoryQuotaPolicyAuthority.status write_connection <sessions-write-connection>`.

The log also lists pairs of a reviewed row and its live twin, because `line` is part of the identity key. Every reviewed `approval_authority.py` row below `mark_decision_seen` moves. I3 Step 10 places `class ApprovalTransactionAuthority` directly after `RepositoryApprovalAuthority.read`, and Step 3 of this task inserts `mark_decision_seen` between the two, so every `ApprovalTransactionAuthority` row sits below the insertion and moves. That is four rows: I3's four `_REVIEWED_READ_CONNECTIONS` rows `ApprovalTransactionAuthority.session_id_of`, `ApprovalTransactionAuthority.approved_bindings`, `ApprovalTransactionAuthority.inbox` and `ApprovalTransactionAuthority.sent` (each `write_connection <sessions-write-connection>`, authority `None`). I1's `record_token_usage_on_connection insert token_usage_ledger` row also moves, because Step 7 added import lines. A moved `_REVIEWED_WRITERS` row prints under `Stale reviewed`, with its twin under `Unexpected/unreviewed`. A moved read-connection row prints under `Stale reviewed read connections`, with its twin under both `Unexpected/unreviewed` and `Connections outside exact contained authority`: I3 Step 12 binds no `ApprovalTransactionAuthority` symbol in `_NAMED_AUTHORITY_SYMBOLS` and adds no `_CONTAINED_CONNECTION_AUTHORITIES` entry, so the four reads stay unbound and uncontained. For each pair, update only `line=`. If a twin's fingerprint differs, that method was edited by mistake: revert the edit rather than re-pin it. The `Unresolved write executions` section is not I1's baseline here. The count this plan gives for it, 45, is derived, not measured: it is I1 Step 15's measured 44 plus the one `src/elspeth/web/coordination/review_authority.py:<line> RepositoryReviewAuthority._lock_then_read_clock unknown_execute <unresolved-session-write>` line that I4 Step 9's drift read states it adds ("`Unresolved write executions` is 1 above the baseline (44)") and accepts as a module shape (its `conn.execute(_ADMIN_HOLDER_ROWS_FOR_UPDATE)` runs a constant imported from `identity_authority`). No tree after I4 existed when this plan was written, so re-measure it: read the count from `/tmp/i9-lane-manifest-drift.log` and confirm it equals the `Unresolved write executions` count in I4 Step 9's green log (`/tmp/i4-manifest-green.log`, or a gate re-run on the tree before this task's edits), and that the only line beyond I1 Step 15's 44 is that `_lock_then_read_clock` line. If I4 was changed to call I3's `lock_admin_population(conn)` instead of executing the imported constant, the measured count is 44, and 44 replaces 45 everywhere in this paragraph. This task adds no line to the section, so expect the re-measured count (45 on I4 as written) and no line naming `approval_authority.py` or `quota_authority.py`. A count above the re-measured one, or any line naming either module, means an execute is not a statement the scanner resolves: fix the module, not the manifest.

Then edit `tests/unit/architecture/test_session_db_mutation_authority.py`:

1. In `_NAMED_AUTHORITY_SYMBOLS` (:262), directly after I1's entry

```python
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_authority.py",
        "record_token_usage_on_connection",
        "QuotaAuthority",
    ),
```

add

```python
    # Task I9: the admin quota editor revokes and replaces an identity's
    # active quota_policies row under both identity locks; QuotaAuthority owns
    # the table (TablePolicy :117), so no policy is widened.
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_authority.py",
        "RepositoryQuotaPolicyAuthority",
        "QuotaAuthority",
    ),
```

2. In `_CONTAINED_CONNECTION_AUTHORITIES` (:914), directly above its closing `)` (the line above the comment `# Literal identities for writers that sit behind an exact named authority.`, HEAD :1195), add

```python
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_authority.py",
        "RepositoryQuotaPolicyAuthority.set_identity_policy",
        "QuotaAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_authority.py",
        "RepositoryQuotaPolicyAuthority.status",
        "QuotaAuthority",
    ),
```

3. In `_REVIEWED_WRITERS` (:1198), directly after I3's `RepositoryApprovalAuthority.withdraw` `update approvals` row, add

```python
    # Task I9: the requester's seen stamp (spec :1416, a UI convenience):
    # WHERE requested_by = caller AND decision IS NOT NULL AND decision_seen_at IS NULL.
    WriterIdentity(
        "src/elspeth/web/coordination/approval_authority.py",
        "RepositoryApprovalAuthority.mark_decision_seen",
        "approvals",
        "update",
        "FP_FROM_LOG",
        1,
        "ApprovalAuthority",
        line=LINE_FROM_LOG,
    ),
```

and directly after I1's `record_token_usage_on_connection` `insert token_usage_ledger` row, add

```python
    # Task I9: admin quota edit, one active row per identity.
    WriterIdentity(
        "src/elspeth/web/coordination/quota_authority.py",
        "RepositoryQuotaPolicyAuthority.set_identity_policy",
        "quota_policies",
        "update",
        "FP_FROM_LOG",
        1,
        "QuotaAuthority",
        line=LINE_FROM_LOG,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/quota_authority.py",
        "RepositoryQuotaPolicyAuthority.set_identity_policy",
        "quota_policies",
        "insert",
        "FP_FROM_LOG",
        1,
        "QuotaAuthority",
        line=LINE_FROM_LOG,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/quota_authority.py",
        "RepositoryQuotaPolicyAuthority.set_identity_policy",
        "<sessions-write-connection>",
        "write_connection",
        "FP_FROM_LOG",
        1,
        "QuotaAuthority",
        line=LINE_FROM_LOG,
    ),
```

4. In `_REVIEWED_READ_CONNECTIONS` (:3954), directly after the `RepositoryIdentityAuthority.configured_admin_seed_consumed` row (HEAD :3957-3966), add

```python
    # Task I9: quota status is SELECT-only; the begin() transaction exists only
    # to lend I1's token-bound daily_token_total one connection and one clock.
    WriterIdentity(
        "src/elspeth/web/coordination/quota_authority.py",
        "RepositoryQuotaPolicyAuthority.status",
        "<sessions-write-connection>",
        "write_connection",
        "FP_FROM_LOG",
        1,
        None,
        line=LINE_FROM_LOG,
    ),
```

In every `FP_FROM_LOG` / `LINE_FROM_LOG` slot, write the fingerprint, ordinal and line for that exact symbol and operation, copied from `/tmp/i9-lane-manifest-drift.log`. The fingerprint is an AST hash of the method as written on your branch; this plan cannot know it.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py -n 0 -v -rx > /tmp/i9-lane-manifest-green.log 2>&1; echo exit=$?`
Expected: `exit=0`, no `failed`, and exactly `1 xfailed`, `test_all_production_sessions_writers_are_reviewed_typed_authorities`: the gate already XFAILs on a clean HEAD (I1 Step 15 records the baseline counts; I7 Step 28 cites the measurement on a `git archive` export of 46219b2b7, `1 xfailed in 100.99s`), so this task cannot bring it to a pass. Read the XFAIL text instead. No section may name a site this task adds or moves.

Run: `grep -c 'mark_decision_seen\|set_identity_policy\|RepositoryQuotaPolicyAuthority' /tmp/i9-lane-manifest-green.log; grep -c 'Stale reviewed (0):' /tmp/i9-lane-manifest-green.log; grep -c 'Stale reviewed read connections (0):' /tmp/i9-lane-manifest-green.log`
Expected: `0`, then at least `1`, then at least `1`. The first count proves every new writer, write connection and read connection is reviewed: no test function in the gate file names any of the three symbols, so a `-v` progress line cannot match. The second proves every line-only re-pin above was applied (the `record_token_usage_on_connection` row), because a missed one prints as a `Stale reviewed` row. The third proves the `RepositoryQuotaPolicyAuthority.status` read-connection row matches the live site and that the four `ApprovalTransactionAuthority` read-connection re-pins above were applied, because a missed one prints as a `Stale reviewed read connections` row.

Control the instrument: change one hex character of the `mark_decision_seen` fingerprint and re-run the gate id with `-rx` to `/tmp/i9-lane-manifest-control.log`. Confirm the log shows `Stale reviewed (1):` naming that row, and that `grep -c 'mark_decision_seen' /tmp/i9-lane-manifest-control.log` prints at least `1`. Then restore the character, re-run the green command, and confirm the three counts above return to `0`, at least `1`, at least `1`. The exit code does not discriminate here: it is `exit=0` with `1 xfailed` both before and after the mutation.

- [ ] **Step 18: Write and run the PostgreSQL proofs (real `FOR UPDATE`, real partial unique index).**

F7 applies: the task adds a `quota_policies` revoke-and-insert under row locks and a conditional `approvals` UPDATE. Docker is required.

Create `tests/testcontainer/web/test_workflow_mailbox_postgres.py`:

```python
"""PostgreSQL proofs for Task I9's two sessions writes.

1. Two concurrent admin quota edits on one identity serialise on the identity
   row lock. Both succeed, exactly one active row survives
   (``uq_quota_policies_active_per_identity``), and the second edit copies the
   first edit's value forward instead of a stale one.
2. ``mark_decision_seen`` is one conditional UPDATE: a second call keeps the
   first stamp, and a non-requester is refused without writing.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, insert, select, text
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.approval_authority import (
    ApprovalBinding,
    ApprovalNotFound,
    ApprovalRecord,
    ApprovalTransactionAuthority,
    RepositoryApprovalAuthority,
)
from elspeth.web.coordination.quota_authority import QuotaPolicySet, RepositoryQuotaPolicyAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import approvals_table, identity_roles_table, quota_policies_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer

BINDING = ApprovalBinding(
    config_hash="1" * 64,
    canonical_version="sha256-rfc8785-v1",
    runtime_val_manifest_sha256="3" * 64,
    openrouter_catalog_sha256="2" * 64,
    binding_generation_fingerprint="c" * 64,
    policy_hash="a" * 64,
)


@pytest.fixture
def database_url(external_deployment_postgres_url: str) -> Iterator[str]:
    database = f"workflow_mailbox_{uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    url = make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False)
    schema_engine = create_session_engine(url)
    try:
        initialize_session_schema(schema_engine)
        now = datetime.now(UTC)
        with schema_engine.begin() as conn:
            for identity_id in ("root", "alice", "bob"):
                ensure_test_identity(conn, identity_id=identity_id)
            conn.execute(
                insert(identity_roles_table).values(
                    role_id="root-admin", identity_id="root", role="admin", granted_at=now - timedelta(hours=1), granted_by_identity_id="root"
                )
            )
            conn.execute(
                insert(quota_policies_table).values(
                    policy_id="alice-policy", identity_id="alice", tokens_per_day=500, storage_bytes=2000, set_by_actor="system", set_at=now
                )
            )
            conn.execute(
                insert(sessions_table).values(id="session-alice", user_id="alice", title="mailbox", created_at=now, updated_at=now)
            )
            conn.execute(
                insert(approvals_table).values(
                    approval_id="decided",
                    session_id="session-alice",
                    state_id="state-1",
                    binding_json=BINDING.as_json(),
                    requested_by_identity_id="alice",
                    approver_identity_id="bob",
                    requested_at=now - timedelta(minutes=5),
                    decided_at=now - timedelta(minutes=1),
                    decision="approved",
                )
            )
        yield url
    finally:
        schema_engine.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()


def _wait_for_lock_wait(observer: Engine, application: str) -> None:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        with observer.connect() as conn:
            waiting = conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :app AND wait_event_type = 'Lock'"),
                {"app": application},
            ).scalar_one()
        if waiting:
            return
        sleep(0.05)
    raise AssertionError(f"{application} never waited on a row lock")


def test_concurrent_quota_edits_serialise_and_leave_one_active_row(database_url: str) -> None:
    first_engine = create_session_engine(database_url, connect_args={"application_name": "quota_first"})
    second_engine = create_session_engine(database_url, connect_args={"application_name": "quota_second"})
    observer = create_session_engine(database_url)
    inside = Event()
    release = Event()

    def hold(_change: QuotaPolicySet) -> None:
        inside.set()
        assert release.wait(10), "the test never released the first edit"

    def first() -> QuotaPolicySet:
        return RepositoryQuotaPolicyAuthority(first_engine).set_identity_policy(
            actor_identity_id="root", identity_id="alice", dimension="tokens", value=111,
            default_tokens_per_day=None, default_storage_bytes=None, record=hold,
        )

    def second() -> QuotaPolicySet:
        return RepositoryQuotaPolicyAuthority(second_engine).set_identity_policy(
            actor_identity_id="root", identity_id="alice", dimension="storage", value=222,
            default_tokens_per_day=None, default_storage_bytes=None, record=lambda _change: None,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first)
            assert inside.wait(10), "the first edit never reached its record callback"
            second_future = pool.submit(second)
            _wait_for_lock_wait(observer, "quota_second")
            release.set()
            first_change = first_future.result(timeout=20)
            second_change = second_future.result(timeout=20)
        assert first_change.previous is not None and first_change.previous.policy_id == "alice-policy"
        # The second edit read the FIRST edit's committed row, not the original one.
        assert second_change.previous is not None and second_change.previous.policy_id == first_change.policy.policy_id
        assert (second_change.policy.tokens_per_day, second_change.policy.storage_bytes) == (111, 222)
        with observer.connect() as conn:
            active = conn.execute(
                select(func.count()).select_from(quota_policies_table).where(
                    quota_policies_table.c.identity_id == "alice", quota_policies_table.c.revoked_at.is_(None)
                )
            ).scalar_one()
            total = conn.execute(select(func.count()).select_from(quota_policies_table).where(quota_policies_table.c.identity_id == "alice")).scalar_one()
        assert (active, total) == (1, 3)
    finally:
        release.set()
        first_engine.dispose()
        second_engine.dispose()
        observer.dispose()


def test_mark_decision_seen_is_one_conditional_update(database_url: str) -> None:
    engine = create_session_engine(database_url)
    authority = ApprovalTransactionAuthority(engine)

    def seen_by(identity_id: str) -> ApprovalRecord:
        def mutation(token: str, now: datetime) -> ApprovalRecord:
            return RepositoryApprovalAuthority.mark_decision_seen(token, approval_id="decided", requested_by=identity_id, now=now)

        return authority.run("session-alice", mutation)

    try:
        with pytest.raises(ApprovalNotFound, match="approval decided not found"):
            seen_by("bob")
        with engine.connect() as conn:
            assert conn.execute(select(approvals_table.c.decision_seen_at).where(approvals_table.c.approval_id == "decided")).scalar_one() is None
        first = seen_by("alice")
        assert first.decision_seen_at is not None
        sleep(0.01)
        again = seen_by("alice")
        assert again.decision_seen_at == first.decision_seen_at
    finally:
        engine.dispose()
```

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/testcontainer/web/test_workflow_mailbox_postgres.py -m testcontainer -n 0 > /tmp/i9-lane-postgres.log 2>&1; echo exit=$?`
Expected: `exit=0`, `2 passed`.

Control the lock proof: temporarily delete the `for locked_id in sorted({actor_identity_id, identity_id}):` loop and the `.with_for_update()` on the `current` select in `set_identity_policy`, then re-run the same command to `/tmp/i9-lane-postgres-control.log`. Expected: `exit=1`, and `test_concurrent_quota_edits_serialise_and_leave_one_active_row` fails, either with `AssertionError: quota_second never waited on a row lock` or with `IntegrityError` naming `uq_quota_policies_active_per_identity`. Restore both lines and re-run the green command to `exit=0`.

Also run I3's and I1's PostgreSQL files, which share the modules this task edits:

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/testcontainer/web/test_approval_decide_race_postgres.py tests/testcontainer/web/test_quota_authority_postgres.py -m testcontainer -n 0 > /tmp/i9-lane-postgres-neighbours.log 2>&1; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 19: Write the failing frontend tests for the envelope parser, the fetchers, the byte formatter and the mailbox store.**

All frontend paths below are relative to `src/elspeth/web/frontend/`. The census gates this task must satisfy are live tree-wide: `src/components/ui/primitiveCensus.test.ts` (zero raw `<button>` / `<input>` outside `components/ui/`), `src/styles/classNames.test.ts` (every emitted className needs a rule in a stylesheet imported by `src/styles/index.css`) and `src/styles/tokenReferences.test.ts` (every `var(--x)` must be defined).

Create `src/api/client.workflow-errors.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { parseResponse } from "./client";
import type { ApiError } from "@/types/index";

function failed(status: number, body: unknown): Response {
  return {
    ok: false,
    status,
    statusText: "Error",
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

async function rejection(response: Response): Promise<ApiError> {
  try {
    await parseResponse<never>(response);
  } catch (error) {
    return error as ApiError;
  }
  throw new Error("parseResponse resolved a failed response");
}

describe("parseResponse workflow envelopes (Task I9)", () => {
  it("reads the identity-admin refusal key as the error type", async () => {
    const error = await rejection(
      failed(409, {
        detail: {
          refusal: "last_active_admin_protected",
          detail: "the last active admin cannot be disabled",
        },
      }),
    );
    expect(error.error_type).toBe("last_active_admin_protected");
    expect(error.detail).toBe("the last active admin cannot be disabled");
  });

  it("lifts current_state from an already-decided refusal", async () => {
    const error = await rejection(
      failed(409, {
        detail: {
          error_type: "approval_already_decided",
          detail: "approval is already approved",
          current_state: "approved",
        },
      }),
    );
    expect(error.error_type).toBe("approval_already_decided");
    expect(error.current_state).toBe("approved");
  });

  it("lifts the storage quota triple from a 413 body", async () => {
    const error = await rejection(
      failed(413, {
        detail: {
          error_type: "storage_quota_exceeded",
          detail: "identity storage quota exceeded",
          dimension: "storage",
          cap: 1000,
          ceiling: null,
          usage: 990,
        },
      }),
    );
    expect(error.status).toBe(413);
    expect(error.storage_quota).toEqual({ cap: 1000, ceiling: null, usage: 990 });
  });

  it("leaves storage_quota undefined for the per-session string 413", async () => {
    const error = await rejection(failed(413, { detail: "Session blob storage limit exceeded" }));
    expect(error.storage_quota).toBeUndefined();
    expect(error.error_type).toBeUndefined();
  });

  it("rejects a quota triple whose usage is not a number", async () => {
    const error = await rejection(
      failed(413, { detail: { cap: 1000, ceiling: null, usage: "990" } }),
    );
    expect(error.storage_quota).toBeUndefined();
  });
});
```

Create `src/api/workflow.test.ts`:

```ts
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  decideApproval,
  fetchIdentities,
  fetchMailboxSummary,
  forkLibraryEntry,
  markApprovalSeen,
  requestApproval,
  setIdentityQuota,
} from "./workflow";

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

function call(spy: ReturnType<typeof vi.spyOn>, index: number): [string, RequestInit] {
  return spy.mock.calls[index] as [string, RequestInit];
}

describe("api/workflow fetchers (Task I9)", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  it("reads the mailbox summary with the auth headers", async () => {
    const summary = {
      governance: "on",
      roles: ["approver"],
      approvals_to_decide: 2,
      reviews_to_attest: 0,
      decisions_unseen: 1,
    };
    fetchSpy.mockResolvedValue(ok(summary));
    await expect(fetchMailboxSummary()).resolves.toEqual(summary);
    const [url, init] = call(fetchSpy, 0);
    expect(url).toBe("/api/workflow/mailbox/summary");
    expect(init).toEqual(expect.objectContaining({ headers: expect.any(Object) }));
  });

  it("posts the seen stamp to the encoded approval id", async () => {
    fetchSpy.mockResolvedValue(ok({ approval_id: "a/1" }));
    await markApprovalSeen("a/1");
    const [url, init] = call(fetchSpy, 0);
    expect(url).toBe("/api/workflow/mailbox/a%2F1/seen");
    expect(init.method).toBe("POST");
  });

  it("sends the approval request body the I3 route declares", async () => {
    fetchSpy.mockResolvedValue(ok({ approval_id: "a-1" }));
    await requestApproval("session-1", { state_id: "state-1", approver_identity_id: "bob", note: "please" });
    const [url, init] = call(fetchSpy, 0);
    expect(url).toBe("/api/sessions/session-1/approvals");
    expect(JSON.parse(init.body as string)).toEqual({
      state_id: "state-1",
      approver_identity_id: "bob",
      note: "please",
    });
  });

  it("sends a decision with its note", async () => {
    fetchSpy.mockResolvedValue(ok({ approval_id: "a-1" }));
    await decideApproval("a-1", "rejected", "missing an owner");
    const [url, init] = call(fetchSpy, 0);
    expect(url).toBe("/api/approvals/a-1/decide");
    expect(JSON.parse(init.body as string)).toEqual({ decision: "rejected", note: "missing an owner" });
  });

  it("sets exactly one quota dimension", async () => {
    fetchSpy.mockResolvedValue(ok({ identity_id: "alice" }));
    await setIdentityQuota("alice", "storage", 3000);
    const [url, init] = call(fetchSpy, 0);
    expect(url).toBe("/api/workflow/quota/identities/alice");
    expect(JSON.parse(init.body as string)).toEqual({ dimension: "storage", value: 3000 });
  });

  it("forks a library entry by POST", async () => {
    fetchSpy.mockResolvedValue(ok({ session_id: "s-2", state_id: "t-2" }));
    await expect(forkLibraryEntry("entry-1")).resolves.toEqual({ session_id: "s-2", state_id: "t-2" });
    const [url, init] = call(fetchSpy, 0);
    expect(url).toBe("/api/library/entry-1/fork");
    expect(init.method).toBe("POST");
  });

  it("lists identities by access state at the route's page ceiling", async () => {
    fetchSpy.mockResolvedValue(ok({ identities: [], access_state: "pending", limit: 200, offset: 0, active_human_admin_count: 1 }));
    await fetchIdentities("pending");
    expect(call(fetchSpy, 0)[0]).toBe("/api/auth/admin/identities?access_state=pending&limit=200&offset=0");
  });
});
```

Create `src/utils/bytes.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { effectiveStorageLimit, formatBytes, storageQuotaMessage } from "./bytes";

describe("utils/bytes (Task I9)", () => {
  it("formats bytes with binary units", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1023)).toBe("1023 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(1_048_576)).toBe("1.0 MB");
  });

  it("takes the lower of the identity cap and the container ceiling", () => {
    expect(effectiveStorageLimit(2000, 1000)).toBe(1000);
    expect(effectiveStorageLimit(null, 1000)).toBe(1000);
    expect(effectiveStorageLimit(2000, null)).toBe(2000);
    expect(effectiveStorageLimit(null, null)).toBeNull();
  });

  it("names the usage and the effective limit so deletion is the obvious recovery", () => {
    expect(storageQuotaMessage({ cap: 2048, ceiling: null, usage: 1536 })).toBe(
      "Storage quota reached: 1.5 KB used of 2.0 KB. Delete files you no longer need, then try again.",
    );
    expect(storageQuotaMessage({ cap: null, ceiling: null, usage: 10 })).toBe(
      "Storage quota reached: 10 B used. Delete files you no longer need, then try again.",
    );
  });
});
```

Create `src/stores/mailboxStore.test.ts`:

```ts
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as workflow from "@/api/workflow";
import {
  MAILBOX_POLL_INTERVAL_MS,
  approvalForState,
  badgeCount,
  useMailboxStore,
} from "./mailboxStore";
import type { ApprovalView, MailboxSummary, ReviewSentView } from "@/types/workflow";

vi.mock("@/api/workflow", () => ({
  fetchMailboxSummary: vi.fn(),
  fetchMailboxInbox: vi.fn(),
  fetchMailboxSent: vi.fn(),
  markApprovalSeen: vi.fn(),
  decideApproval: vi.fn(),
  attestReview: vi.fn(),
}));

const fetchMailboxSummary = vi.mocked(workflow.fetchMailboxSummary);
const fetchMailboxInbox = vi.mocked(workflow.fetchMailboxInbox);
const markApprovalSeen = vi.mocked(workflow.markApprovalSeen);
const decideApproval = vi.mocked(workflow.decideApproval);
const fetchMailboxSent = vi.mocked(workflow.fetchMailboxSent);

const SUMMARY: MailboxSummary = {
  governance: "on",
  roles: ["approver"],
  approvals_to_decide: 2,
  reviews_to_attest: 1,
  decisions_unseen: 1,
};

function approval(overrides: Partial<ApprovalView> = {}): ApprovalView {
  return Object.assign(
    {
      approval_id: "a-1",
      session_id: "s-1",
      state_id: "t-1",
      binding: {},
      requested_by_identity_id: "alice",
      approver_identity_id: "bob",
      requested_at: "2026-09-14T09:00:00Z",
      decided_at: "2026-09-14T10:00:00Z",
      decision: "rejected",
      request_note: "please",
      decision_seen_at: null,
      decided_by_identity_id: "bob",
      decision_note: "missing an owner",
      revoked_by_identity_id: null,
      revocation_actor_kind: null,
      revocation_event_id: null,
    },
    overrides,
  ) as ApprovalView;
}

describe("mailboxStore (Task I9)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMailboxStore.getState().reset();
  });

  afterEach(() => {
    useMailboxStore.getState().reset();
    vi.useRealTimers();
  });

  it("counts the badge only while governance is on", () => {
    expect(badgeCount(SUMMARY)).toBe(4);
    expect(badgeCount(Object.assign({}, SUMMARY, { governance: "off" }))).toBe(0);
    expect(badgeCount(null)).toBe(0);
  });

  it("polls the summary on exactly one timer, however often polling is started", async () => {
    vi.useFakeTimers();
    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
    fetchMailboxSummary.mockResolvedValue(SUMMARY);
    const stop = useMailboxStore.getState().startPolling();
    useMailboxStore.getState().startPolling();
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);
    expect(fetchMailboxSummary).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(MAILBOX_POLL_INTERVAL_MS);
    expect(fetchMailboxSummary).toHaveBeenCalledTimes(2);
    expect(useMailboxStore.getState().summary).toEqual(SUMMARY);
    stop();
    await vi.advanceTimersByTimeAsync(MAILBOX_POLL_INTERVAL_MS * 3);
    expect(fetchMailboxSummary).toHaveBeenCalledTimes(2);
  });

  it("keeps the last summary when a refresh fails", async () => {
    fetchMailboxSummary.mockResolvedValueOnce(SUMMARY).mockRejectedValueOnce({ status: 503, detail: "down" });
    await useMailboxStore.getState().refreshSummary();
    await useMailboxStore.getState().refreshSummary();
    expect(useMailboxStore.getState().summary).toEqual(SUMMARY);
  });

  it("stamps a decided unseen request when it is opened, and only that one", async () => {
    const decided = approval();
    const open = approval({ approval_id: "a-2", decision: null, decided_at: null, decided_by_identity_id: null, decision_note: null });
    useMailboxStore.setState({ sent: [decided, open] });
    markApprovalSeen.mockResolvedValue(approval({ decision_seen_at: "2026-09-14T11:00:00Z" }));
    fetchMailboxSummary.mockResolvedValue(Object.assign({}, SUMMARY, { decisions_unseen: 0 }));
    await useMailboxStore.getState().openSent("a-2");
    expect(markApprovalSeen).not.toHaveBeenCalled();
    await useMailboxStore.getState().openSent("a-1");
    expect(markApprovalSeen).toHaveBeenCalledWith("a-1");
    expect(useMailboxStore.getState().sent?.[0].decision_seen_at).toBe("2026-09-14T11:00:00Z");
    expect(useMailboxStore.getState().summary?.decisions_unseen).toBe(0);
    // Mutation-derivation: the stored seen stamp, not the call, stops a second POST.
    await useMailboxStore.getState().openSent("a-1");
    expect(markApprovalSeen).toHaveBeenCalledTimes(1);
  });

  it("reloads the inbox and the badge after a decision, and names an already-decided loss", async () => {
    decideApproval.mockResolvedValueOnce(approval({ decision: "approved" }));
    fetchMailboxInbox.mockResolvedValue({ approvals: [], reviews: [] });
    fetchMailboxSummary.mockResolvedValue(SUMMARY);
    await expect(useMailboxStore.getState().decide("a-1", "approved", null)).resolves.toBe(true);
    expect(fetchMailboxInbox).toHaveBeenCalledTimes(1);
    expect(fetchMailboxSummary).toHaveBeenCalledTimes(1);
    decideApproval.mockRejectedValueOnce({
      status: 409,
      detail: "approval is already approved",
      error_type: "approval_already_decided",
      current_state: "approved",
    });
    await expect(useMailboxStore.getState().decide("a-1", "rejected", "late")).resolves.toBe(false);
    expect(useMailboxStore.getState().error).toBe("This request was already approved.");
  });

  it("picks the newest request for the exact session and state", () => {
    const older = approval({ approval_id: "old", requested_at: "2026-09-14T08:00:00Z" });
    const newer = approval({ approval_id: "new", requested_at: "2026-09-14T12:00:00Z" });
    const otherState = approval({ approval_id: "other", state_id: "t-2", requested_at: "2026-09-14T13:00:00Z" });
    expect(approvalForState([older, newer, otherState], "s-1", "t-1")?.approval_id).toBe("new");
    expect(approvalForState([older], "s-1", "t-9")).toBeNull();
    expect(approvalForState(null, "s-1", "t-1")).toBeNull();
  });

  it("loads the sent approvals and the sent review outcomes from one response", async () => {
    const outcome: ReviewSentView = {
      request: {
        request_id: "r-1",
        session_id: "s-2",
        state_id: "t-2",
        requested_by_identity_id: "alice",
        reviewer_identity_id: null,
        requested_at: "2026-09-14T09:30:00Z",
        cancelled_at: null,
        request_note: "have a look",
        open: false,
      },
      attestations: [
        {
          attestation_id: "at-1",
          session_id: "s-2",
          state_id: "t-2",
          payload_digest: "sha256:abc",
          reviewer_identity_id: "erin",
          author_identity_id: "alice",
          attested_at: "2026-09-14T10:00:00Z",
          verdict: "changes_requested",
          note: "rename the sink",
        },
      ],
    };
    fetchMailboxSent.mockResolvedValue({ approvals: [approval()], reviews: [outcome] });
    await useMailboxStore.getState().loadSent();
    expect(useMailboxStore.getState().sent).toEqual([approval()]);
    expect(useMailboxStore.getState().sentReviews).toEqual([outcome]);
    // A review outcome is never unread: loading it stamps nothing.
    expect(markApprovalSeen).not.toHaveBeenCalled();
    useMailboxStore.getState().reset();
    expect(useMailboxStore.getState().sentReviews).toBeNull();
  });
});
```

- [ ] **Step 20: Run the new frontend tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npx vitest run src/api/client.workflow-errors.test.ts src/api/workflow.test.ts src/utils/bytes.test.ts src/stores/mailboxStore.test.ts > /tmp/i9-lane-fe-core-red.log 2>&1; echo exit=$?`
Expected: `exit=1`. `workflow.test.ts`, `bytes.test.ts` and `mailboxStore.test.ts` fail to load with `Failed to resolve import "./workflow"`, `"./bytes"` and `"./mailboxStore"`. In `client.workflow-errors.test.ts`, the refusal test fails with `expected undefined to be 'last_active_admin_protected'`, and the `current_state` and storage-quota tests fail on `undefined`; the two `toBeUndefined` tests pass.

- [ ] **Step 21: Write the wire types, extend the envelope parser, and write the fetchers, the byte formatter and the mailbox store.**

In `src/types/index.ts`, directly after the last `ApiError` field `  snapshot_fingerprint?: string;` (:1212), add:

```ts
  /** The row's state when a curated or decided row refused a second act
   *  (I3 `approval_already_decided`, I5 `library_entry_already_curated`). */
  current_state?: string;
  /** Present only on the R13 identity storage refusal (Task I2's 413). The
   *  per-session 413 carries a plain string detail and leaves this absent. */
  storage_quota?: StorageQuotaRefusal;
```

and directly after the `ApiError` interface's closing `}`, add:

```ts
/** R13's identity storage refusal: the cap, the container ceiling and the current usage, in bytes. */
export interface StorageQuotaRefusal {
  cap: number | null;
  ceiling: number | null;
  usage: number;
}
```

In `src/api/client.ts`:

1. Replace `    let partialStateSaveError: ApiError["partial_state_save_error"];` (:257) with:

```ts
    let partialStateSaveError: ApiError["partial_state_save_error"];
    let currentState: string | undefined;
    let storageQuota: ApiError["storage_quota"];
```

2. Replace `        ["error_type", "error_code", "code", "kind"],` (:267) with:

```ts
        // `refusal` is the identity-admin router's key (identity_admin_routes.py _refused).
        ["error_type", "error_code", "code", "kind", "refusal"],
```

3. Replace `      requestId = firstStringField([body, nestedDetail], ["request_id"]);` (:270) with:

```ts
      requestId = firstStringField([body, nestedDetail], ["request_id"]);
      currentState = firstStringField([body, nestedDetail], ["current_state"]);

      // R13 identity storage refusal (Task I2). Keyed on a numeric usage, not
      // on the error_type string, so the per-session string 413 never matches.
      const rawUsage = firstDefined(ownField(body, "usage"), ownField(nestedDetail, "usage"));
      const rawCap = firstDefined(ownField(body, "cap"), ownField(nestedDetail, "cap"));
      const rawCeiling = firstDefined(ownField(body, "ceiling"), ownField(nestedDetail, "ceiling"));
      storageQuota =
        typeof rawUsage === "number" &&
        (typeof rawCap === "number" || rawCap === null) &&
        (typeof rawCeiling === "number" || rawCeiling === null)
          ? { usage: rawUsage, cap: rawCap, ceiling: rawCeiling }
          : undefined;
```

4. Replace `      partial_state_save_error: partialStateSaveError,` (:420) with:

```ts
      partial_state_save_error: partialStateSaveError,
      current_state: currentState,
      storage_quota: storageQuota,
```

Create `src/types/workflow.ts`:

```ts
// ============================================================================
// Workflow governance wire types (Task I9). Each interface mirrors one
// pydantic view: I3 approvals.py, I4 reviews.py, I5 library.py, I7 inspect.py,
// the I9 mailbox.py and quota.py, and auth/identity_admin_routes.py.
// ============================================================================

export type IdentityRole =
  | "admin"
  | "approver"
  | "reviewer"
  | "user"
  | "curator"
  | "auditor"
  | "oversight";

export type IdentityAccessState = "pending" | "active" | "disabled";

export type ApprovalDecision = "approved" | "rejected" | "revoked" | "superseded";

export interface ApprovalView {
  approval_id: string;
  session_id: string;
  state_id: string;
  binding: Record<string, string>;
  requested_by_identity_id: string;
  approver_identity_id: string;
  requested_at: string;
  decided_at: string | null;
  decision: ApprovalDecision | null;
  request_note: string | null;
  decision_seen_at: string | null;
  decided_by_identity_id: string | null;
  decision_note: string | null;
  revoked_by_identity_id: string | null;
  revocation_actor_kind: string | null;
  revocation_event_id: string | null;
}

export type ReviewVerdict = "signed_off" | "changes_requested" | "withdrawn";

export interface ReviewRequestView {
  request_id: string;
  session_id: string;
  state_id: string;
  requested_by_identity_id: string;
  reviewer_identity_id: string | null;
  requested_at: string;
  cancelled_at: string | null;
  request_note: string | null;
  open: boolean;
}

export interface ReviewAttestationView {
  attestation_id: string;
  session_id: string;
  state_id: string;
  payload_digest: string;
  reviewer_identity_id: string;
  author_identity_id: string;
  attested_at: string;
  verdict: ReviewVerdict;
  note: string | null;
}

export interface MailboxSummary {
  governance: "on" | "off";
  roles: IdentityRole[];
  approvals_to_decide: number;
  reviews_to_attest: number;
  decisions_unseen: number;
}

export interface MailboxInbox {
  approvals: ApprovalView[];
  reviews: ReviewRequestView[];
}

/** One of the caller's review requests with the attestations attributed to it (mailbox.py `ReviewSentView`). */
export interface ReviewSentView {
  request: ReviewRequestView;
  attestations: ReviewAttestationView[];
}

export interface MailboxSent {
  approvals: ApprovalView[];
  reviews: ReviewSentView[];
}

export interface ApproverEntry {
  identity_id: string;
  username: string;
}

export interface ApproverDirectory {
  approvers: ApproverEntry[];
  suggested_identity_ids: string[];
}

export interface WorkflowInspect {
  session_id: string;
  state_id: string;
  access_log_id: string;
  composition_snapshot: unknown;
  yaml: string;
  attestations: ReviewAttestationView[];
}

export interface IdentityView {
  identity_id: string;
  provider: string;
  kind: "human" | "service";
  subject: string;
  organisation_id: string | null;
  access_state: IdentityAccessState;
  username: string | null;
  display_name: string | null;
  email: string | null;
  first_seen_at: string;
  last_login_at: string | null;
  pre_provisioned_at: string | null;
  activated_at: string | null;
  activated_by_identity_id: string | null;
  disabled_at: string | null;
  disabled_by_identity_id: string | null;
  disable_reason: string | null;
}

export interface IdentityList {
  identities: IdentityView[];
  access_state: IdentityAccessState;
  limit: number;
  offset: number;
  active_human_admin_count: number;
}

export interface RoleView {
  role_id: string;
  identity_id: string;
  role: IdentityRole;
  scope: string | null;
  expires_at: string | null;
  note: string | null;
  granted_by_identity_id: string | null;
  granted_at: string;
  revoked_at: string | null;
}

export interface RoleList {
  roles: RoleView[];
  limit: number;
  offset: number;
}

export interface RelationshipView {
  relationship_id: string;
  from_identity_id: string;
  to_identity_id: string;
  relationship_type: "approver";
  asserted_by_identity_id: string;
  asserted_at: string;
  effective_from: string | null;
  effective_until: string | null;
  note: string | null;
  revoked_at: string | null;
  revoked_by_identity_id: string | null;
}

export interface RelationshipList {
  relationships: RelationshipView[];
  limit: number;
  offset: number;
}

export type QuotaDimension = "tokens" | "storage";

export interface IdentityQuota {
  identity_id: string;
  tokens_per_day: number | null;
  storage_bytes: number | null;
  container_tokens_per_day: number | null;
  container_storage_bytes: number | null;
  tokens_used_today: number | null;
  storage_bytes_used: number;
}

export type LibraryEntryState = "pending" | "accepted" | "rejected" | "deprecated" | "recalled";

export interface LibraryEntryView {
  entry_id: string;
  published_from_session_id: string | null;
  payload_digest: string;
  compartment_id: string;
  title: string;
  version: number;
  published_by_identity_id: string;
  curated_by_identity_id: string | null;
  published_at: string;
  accepted_at: string | null;
  rejected_at: string | null;
  rejection_note: string | null;
  deprecated_at: string | null;
  recalled_at: string | null;
  note: string | null;
  state: LibraryEntryState;
}

export interface LibraryList {
  view: "accepted" | "queue" | "mine";
  entries: LibraryEntryView[];
}

export interface LibraryFork {
  session_id: string;
  state_id: string;
}
```

Create `src/api/workflow.ts`:

```ts
// ============================================================================
// Workflow governance fetchers (Task I9): mailbox, approvals, reviews,
// inspect, identity administration, quota and library. Every call goes
// through parseResponse so the global 401 interceptor and the ApiError
// envelope apply.
// ============================================================================

import { authHeaders, parseResponse } from "./client";
import type {
  ApprovalView,
  ApproverDirectory,
  IdentityAccessState,
  IdentityList,
  IdentityQuota,
  IdentityRole,
  IdentityView,
  LibraryFork,
  LibraryList,
  MailboxInbox,
  MailboxSent,
  MailboxSummary,
  QuotaDimension,
  RelationshipList,
  RelationshipView,
  ReviewAttestationView,
  ReviewRequestView,
  ReviewVerdict,
  RoleList,
  RoleView,
  WorkflowInspect,
} from "@/types/workflow";

const ADMIN_PAGE_SIZE = 200;

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: authHeaders() });
  return parseResponse<T>(response);
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    headers: authHeaders("application/json"),
    body: JSON.stringify(body),
  });
  return parseResponse<T>(response);
}

function segment(value: string): string {
  return encodeURIComponent(value);
}

function blankToNull(note: string | null): string | null {
  return note === null || note.trim() === "" ? null : note;
}

// ── Mailbox ────────────────────────────────────────────────────────────────

export function fetchMailboxSummary(): Promise<MailboxSummary> {
  return getJson<MailboxSummary>("/api/workflow/mailbox/summary");
}

export function fetchMailboxInbox(): Promise<MailboxInbox> {
  return getJson<MailboxInbox>("/api/workflow/mailbox/inbox");
}

export function fetchMailboxSent(): Promise<MailboxSent> {
  return getJson<MailboxSent>("/api/workflow/mailbox/sent");
}

export async function markApprovalSeen(approvalId: string): Promise<ApprovalView> {
  const response = await fetch(`/api/workflow/mailbox/${segment(approvalId)}/seen`, {
    method: "POST",
    headers: authHeaders(),
  });
  return parseResponse<ApprovalView>(response);
}

export function fetchApproverDirectory(): Promise<ApproverDirectory> {
  return getJson<ApproverDirectory>("/api/workflow/mailbox/approvers");
}

// ── Approvals and reviews ─────────────────────────────────────────────────

export function requestApproval(
  sessionId: string,
  body: { state_id: string; approver_identity_id: string; note: string | null },
): Promise<ApprovalView> {
  return postJson<ApprovalView>(`/api/sessions/${segment(sessionId)}/approvals`, {
    state_id: body.state_id,
    approver_identity_id: body.approver_identity_id,
    note: blankToNull(body.note),
  });
}

export function decideApproval(
  approvalId: string,
  decision: "approved" | "rejected",
  note: string | null,
): Promise<ApprovalView> {
  return postJson<ApprovalView>(`/api/approvals/${segment(approvalId)}/decide`, {
    decision,
    note: blankToNull(note),
  });
}

export async function withdrawApproval(approvalId: string): Promise<ApprovalView> {
  const response = await fetch(`/api/approvals/${segment(approvalId)}/withdraw`, {
    method: "POST",
    headers: authHeaders(),
  });
  return parseResponse<ApprovalView>(response);
}

export function requestReview(
  sessionId: string,
  body: { state_id: string; note: string | null },
): Promise<ReviewRequestView> {
  return postJson<ReviewRequestView>(`/api/sessions/${segment(sessionId)}/reviews`, {
    state_id: body.state_id,
    reviewer_identity_id: null,
    note: blankToNull(body.note),
  });
}

export function attestReview(
  requestId: string,
  verdict: ReviewVerdict,
  note: string | null,
): Promise<ReviewAttestationView> {
  return postJson<ReviewAttestationView>(`/api/reviews/${segment(requestId)}/attest`, {
    verdict,
    note: blankToNull(note),
  });
}

export function fetchWorkflowInspect(sessionId: string, stateId: string): Promise<WorkflowInspect> {
  return getJson<WorkflowInspect>(`/api/workflow/inspect/${segment(sessionId)}/${segment(stateId)}`);
}

// ── Identity administration (auth/identity_admin_routes.py) ───────────────

export function fetchIdentities(accessState: IdentityAccessState): Promise<IdentityList> {
  return getJson<IdentityList>(
    `/api/auth/admin/identities?access_state=${accessState}&limit=${ADMIN_PAGE_SIZE}&offset=0`,
  );
}

export function enableIdentity(identityId: string, note: string): Promise<IdentityView> {
  return postJson<IdentityView>(`/api/auth/admin/identities/${segment(identityId)}/enable`, { note });
}

export function disableIdentity(identityId: string, reason: string): Promise<{ identity: IdentityView }> {
  return postJson<{ identity: IdentityView }>(`/api/auth/admin/identities/${segment(identityId)}/disable`, { reason });
}

export function fetchRoles(identityId: string): Promise<RoleList> {
  return getJson<RoleList>(`/api/auth/admin/roles?identity_id=${segment(identityId)}&limit=${ADMIN_PAGE_SIZE}&offset=0`);
}

export function grantRole(body: { identity_id: string; role: IdentityRole; note: string | null }): Promise<RoleView> {
  return postJson<RoleView>("/api/auth/admin/roles", {
    identity_id: body.identity_id,
    role: body.role,
    note: blankToNull(body.note),
  });
}

export function revokeRole(roleId: string, note: string | null): Promise<RoleView> {
  return postJson<RoleView>(`/api/auth/admin/roles/${segment(roleId)}/revoke`, { note: blankToNull(note) });
}

export function fetchRelationships(identityId: string): Promise<RelationshipList> {
  return getJson<RelationshipList>(
    `/api/auth/admin/relationships?identity_id=${segment(identityId)}&limit=${ADMIN_PAGE_SIZE}&offset=0`,
  );
}

export function assertApproverRelationship(body: {
  from_identity_id: string;
  to_identity_id: string;
  note: string | null;
}): Promise<RelationshipView> {
  return postJson<RelationshipView>("/api/auth/admin/relationships", {
    from_identity_id: body.from_identity_id,
    to_identity_id: body.to_identity_id,
    relationship_type: "approver",
    note: blankToNull(body.note),
  });
}

export function revokeRelationship(relationshipId: string, note: string | null): Promise<RelationshipView> {
  return postJson<RelationshipView>(`/api/auth/admin/relationships/${segment(relationshipId)}/revoke`, {
    note: blankToNull(note),
  });
}

// ── Quota ──────────────────────────────────────────────────────────────────

export function fetchMyQuota(): Promise<IdentityQuota> {
  return getJson<IdentityQuota>("/api/workflow/quota/me");
}

export function fetchIdentityQuota(identityId: string): Promise<IdentityQuota> {
  return getJson<IdentityQuota>(`/api/workflow/quota/identities/${segment(identityId)}`);
}

export function setIdentityQuota(identityId: string, dimension: QuotaDimension, value: number): Promise<IdentityQuota> {
  return postJson<IdentityQuota>(`/api/workflow/quota/identities/${segment(identityId)}`, { dimension, value });
}

// ── Library ────────────────────────────────────────────────────────────────

export function fetchLibrary(view: "accepted" | "queue" | "mine"): Promise<LibraryList> {
  return getJson<LibraryList>(`/api/library?view=${view}`);
}

export async function forkLibraryEntry(entryId: string): Promise<LibraryFork> {
  const response = await fetch(`/api/library/${segment(entryId)}/fork`, {
    method: "POST",
    headers: authHeaders(),
  });
  return parseResponse<LibraryFork>(response);
}
```

Create `src/utils/bytes.ts`:

```ts
import type { StorageQuotaRefusal } from "@/types/index";

const UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

/** Binary-unit byte count for quota copy; bytes below 1 KB stay exact. */
export function formatBytes(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${UNITS[unit]}`;
}

/** R13 refuses at the LOWER of the identity cap and the container ceiling. */
export function effectiveStorageLimit(cap: number | null, ceiling: number | null): number | null {
  if (cap === null) return ceiling;
  if (ceiling === null) return cap;
  return Math.min(cap, ceiling);
}

/** The upload refusal copy: usage and limit, so deleting files is the obvious recovery (spec :1131-1133). */
export function storageQuotaMessage(refusal: StorageQuotaRefusal): string {
  const limit = effectiveStorageLimit(refusal.cap, refusal.ceiling);
  const used = formatBytes(refusal.usage);
  const recovery = "Delete files you no longer need, then try again.";
  return limit === null
    ? `Storage quota reached: ${used} used. ${recovery}`
    : `Storage quota reached: ${used} used of ${formatBytes(limit)}. ${recovery}`;
}
```

Create `src/stores/mailboxStore.ts`:

```ts
/**
 * Mailbox store (Task I9, spec §Frontend → Mailbox, sso-design.md:1204-1234).
 *
 * ONE timer: startPolling() owns the only setInterval for the badge (spec
 * :1232, "One timer for the badge, and no second one") and is idempotent.
 * The folders load on demand when the mailbox dialog opens. A failed summary
 * refresh keeps the last summary, so a blip never flashes the badge away.
 */

import { create } from "zustand";

import * as workflow from "@/api/workflow";
import type { ApiError } from "@/types/index";
import type { ApprovalView, MailboxInbox, MailboxSummary, ReviewSentView, ReviewVerdict } from "@/types/workflow";

export const MAILBOX_POLL_INTERVAL_MS = 30_000;

let pollTimer: ReturnType<typeof setInterval> | null = null;

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

export function badgeCount(summary: MailboxSummary | null): number {
  if (summary === null || summary.governance !== "on") return 0;
  return summary.approvals_to_decide + summary.reviews_to_attest + summary.decisions_unseen;
}

/** A wire timestamp as an instant: PostgreSQL sends microseconds and SQLite whole seconds, so the strings do not sort. */
function requestedInstant(approval: ApprovalView): number {
  const parsed = Date.parse(approval.requested_at);
  if (Number.isNaN(parsed)) throw new Error(`unparseable approval requested_at: ${approval.requested_at}`);
  return parsed;
}

/**
 * The newest approval request for exactly this composition state, or null (decision 6).
 *
 * "Newest" is a total order: the later `requested_at` instant, then the greater
 * `approval_id`. One state can carry several decided requests (Task I3 decision 8)
 * and SQLite's one-second clock ties them, so the pick never depends on the order
 * of `sent`.
 */
export function approvalForState(
  sent: ApprovalView[] | null,
  sessionId: string,
  stateId: string,
): ApprovalView | null {
  if (sent === null) return null;
  let newest: ApprovalView | null = null;
  for (const candidate of sent) {
    if (candidate.session_id !== sessionId || candidate.state_id !== stateId) continue;
    if (newest === null) {
      newest = candidate;
      continue;
    }
    const difference = requestedInstant(candidate) - requestedInstant(newest);
    if (difference > 0 || (difference === 0 && candidate.approval_id > newest.approval_id)) newest = candidate;
  }
  return newest;
}

/** One sentence per closed workflow error code; the backend detail otherwise. */
export function workflowErrorMessage(error: unknown): string {
  const apiError = error as Partial<ApiError>;
  switch (apiError.error_type) {
    case "approval_already_decided":
      return `This request was already ${apiError.current_state ?? "decided"}.`;
    case "approval_note_required":
      return "A rejection needs a note explaining why.";
    case "changes_requested_needs_note":
      return "Requesting changes needs a note explaining what to change.";
    case "approval_open_request_exists":
      return "An approval request is already open for this version.";
    case "workflow_governance_off":
      return "Workflow governance is not enabled on this deployment.";
    default:
      return typeof apiError.detail === "string" ? apiError.detail : "The request failed. Please try again.";
  }
}

interface MailboxState {
  summary: MailboxSummary | null;
  inbox: MailboxInbox | null;
  sent: ApprovalView[] | null;
  /** The caller's review requests with their attested outcomes; shown, never unread (no seen column). */
  sentReviews: ReviewSentView[] | null;
  error: string | null;
  refreshSummary: () => Promise<void>;
  loadInbox: () => Promise<void>;
  loadSent: () => Promise<void>;
  openSent: (approvalId: string) => Promise<void>;
  decide: (approvalId: string, decision: "approved" | "rejected", note: string | null) => Promise<boolean>;
  attest: (requestId: string, verdict: ReviewVerdict, note: string | null) => Promise<boolean>;
  startPolling: () => () => void;
  reset: () => void;
}

export const useMailboxStore = create<MailboxState>((set, get) => ({
  summary: null,
  inbox: null,
  sent: null,
  sentReviews: null,
  error: null,

  async refreshSummary() {
    try {
      const summary = await workflow.fetchMailboxSummary();
      set({ summary });
    } catch {
      // Keep the last summary: the badge is a convenience, never an alarm.
    }
  },

  async loadInbox() {
    try {
      const inbox = await workflow.fetchMailboxInbox();
      set({ inbox, error: null });
    } catch (error) {
      set({ error: workflowErrorMessage(error) });
    }
  },

  async loadSent() {
    try {
      const sent = await workflow.fetchMailboxSent();
      set({ sent: sent.approvals, sentReviews: sent.reviews, error: null });
    } catch (error) {
      set({ error: workflowErrorMessage(error) });
    }
  },

  async openSent(approvalId: string) {
    const current = get().sent;
    const target = current?.find((candidate) => candidate.approval_id === approvalId);
    if (target === undefined || target.decision === null || target.decision_seen_at !== null) return;
    try {
      const updated = await workflow.markApprovalSeen(approvalId);
      set((state) => ({
        sent: (state.sent ?? []).map((candidate) => (candidate.approval_id === approvalId ? updated : candidate)),
      }));
      await get().refreshSummary();
    } catch (error) {
      set({ error: workflowErrorMessage(error) });
    }
  },

  async decide(approvalId, decision, note) {
    try {
      await workflow.decideApproval(approvalId, decision, note);
      set({ error: null });
      await Promise.all([get().loadInbox(), get().refreshSummary()]);
      return true;
    } catch (error) {
      set({ error: workflowErrorMessage(error) });
      return false;
    }
  },

  async attest(requestId, verdict, note) {
    try {
      await workflow.attestReview(requestId, verdict, note);
      set({ error: null });
      await Promise.all([get().loadInbox(), get().refreshSummary()]);
      return true;
    } catch (error) {
      set({ error: workflowErrorMessage(error) });
      return false;
    }
  },

  startPolling() {
    if (pollTimer === null) {
      void get().refreshSummary();
      pollTimer = setInterval(() => {
        void get().refreshSummary();
      }, MAILBOX_POLL_INTERVAL_MS);
    }
    return stopPolling;
  },

  reset() {
    stopPolling();
    set({ summary: null, inbox: null, sent: null, sentReviews: null, error: null });
  },
}));
```

- [ ] **Step 22: Run the core frontend tests and the existing client suites to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npx vitest run src/api/client.workflow-errors.test.ts src/api/workflow.test.ts src/utils/bytes.test.ts src/stores/mailboxStore.test.ts src/api/client.execution-errors.test.ts src/api/client.auth.test.ts > /tmp/i9-lane-fe-core-green.log 2>&1; echo exit=$?`
Expected: `exit=0`; the four new files contribute `5`, `7`, `3` and `7` passes.

- [ ] **Step 23: Write the failing mailbox UI tests.**

Create `src/components/workflow/MailboxBadge.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MailboxBadge } from "./MailboxBadge";
import { useMailboxStore } from "@/stores/mailboxStore";

vi.mock("@/api/workflow", () => ({ fetchMailboxSummary: vi.fn() }));

const ON = {
  governance: "on" as const,
  roles: [],
  approvals_to_decide: 1,
  reviews_to_attest: 2,
  decisions_unseen: 1,
};

describe("MailboxBadge (Task I9)", () => {
  beforeEach(() => {
    useMailboxStore.getState().reset();
  });

  it("shows the unread count from the summary and opens the mailbox", async () => {
    const onOpen = vi.fn();
    useMailboxStore.setState({ summary: ON });
    render(<MailboxBadge onOpen={onOpen} />);
    await userEvent.click(screen.getByRole("button", { name: "Mailbox, 4 unread" }));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("renders nothing at zero, so the default header is unchanged", () => {
    useMailboxStore.setState({
      summary: Object.assign({}, ON, { approvals_to_decide: 0, reviews_to_attest: 0, decisions_unseen: 0 }),
    });
    const { container } = render(<MailboxBadge onOpen={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing while governance is off, whatever the counts say", () => {
    useMailboxStore.setState({ summary: Object.assign({}, ON, { governance: "off" }) });
    const { container } = render(<MailboxBadge onOpen={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });
});
```

Create `src/components/workflow/MailboxDialog.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MailboxDialog } from "./MailboxDialog";
import type { InboxItem } from "./InboxList";
import { InspectPane } from "./InspectPane";
import * as workflow from "@/api/workflow";
import { useMailboxStore } from "@/stores/mailboxStore";
import type { ApprovalView, ReviewRequestView, WorkflowInspect } from "@/types/workflow";

vi.mock("@/api/workflow", () => ({
  fetchMailboxSummary: vi.fn(),
  fetchMailboxInbox: vi.fn(),
  fetchMailboxSent: vi.fn(),
  markApprovalSeen: vi.fn(),
  decideApproval: vi.fn(),
  attestReview: vi.fn(),
  fetchWorkflowInspect: vi.fn(),
}));

const api = vi.mocked(workflow);

function approval(overrides: Partial<ApprovalView> = {}): ApprovalView {
  return Object.assign(
    {
      approval_id: "a-1",
      session_id: "s-1",
      state_id: "t-1",
      binding: {},
      requested_by_identity_id: "alice",
      approver_identity_id: "bob",
      requested_at: "2026-09-14T09:00:00Z",
      decided_at: null,
      decision: null,
      request_note: "please approve",
      decision_seen_at: null,
      decided_by_identity_id: null,
      decision_note: null,
      revoked_by_identity_id: null,
      revocation_actor_kind: null,
      revocation_event_id: null,
    },
    overrides,
  ) as ApprovalView;
}

const REVIEW: ReviewRequestView = {
  request_id: "r-1",
  session_id: "s-2",
  state_id: "t-2",
  requested_by_identity_id: "alice",
  reviewer_identity_id: null,
  requested_at: "2026-09-14T09:30:00Z",
  cancelled_at: null,
  request_note: "have a look",
  open: true,
};

const INSPECT = {
  session_id: "s-1",
  state_id: "t-1",
  access_log_id: "log-1",
  composition_snapshot: {},
  yaml: "sources:\n  primary:\n    plugin: csv\n",
  attestations: [],
};

describe("MailboxDialog (Task I9)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMailboxStore.getState().reset();
    api.fetchMailboxInbox.mockResolvedValue({ approvals: [approval()], reviews: [REVIEW] });
    api.fetchMailboxSent.mockResolvedValue({ approvals: [], reviews: [] });
    api.fetchMailboxSummary.mockResolvedValue({
      governance: "on",
      roles: ["approver", "reviewer"],
      approvals_to_decide: 1,
      reviews_to_attest: 1,
      decisions_unseen: 0,
    });
    // The server echoes the ids it inspected; InspectPane enables a decision only for its own request's state.
    api.fetchWorkflowInspect.mockImplementation((sessionId: string, stateId: string) =>
      Promise.resolve(Object.assign({}, INSPECT, { session_id: sessionId, state_id: stateId })),
    );
  });

  it("opens an inbox approval into the frozen inspect view with the requester's note, and approves it", async () => {
    api.decideApproval.mockResolvedValue(approval({ decision: "approved" }));
    const onClose = vi.fn();
    render(<MailboxDialog onClose={onClose} />);
    await userEvent.click(await screen.findByRole("button", { name: /Approval requested by alice/ }));
    expect(await screen.findByTestId("mailbox-inspect-yaml")).toHaveTextContent("plugin: csv");
    expect(api.fetchWorkflowInspect).toHaveBeenCalledWith("s-1", "t-1");
    expect(screen.getByText("please approve")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(api.decideApproval).toHaveBeenCalledWith("a-1", "approved", ""));
    expect(await screen.findByRole("button", { name: /Approval requested by alice/ })).toBeInTheDocument();
  });

  it("keeps Reject disabled until a note is written, then sends the note", async () => {
    api.decideApproval.mockResolvedValue(approval({ decision: "rejected" }));
    render(<MailboxDialog onClose={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: /Approval requested by alice/ }));
    await screen.findByTestId("mailbox-inspect-yaml");
    const reject = screen.getByRole("button", { name: "Reject" });
    expect(reject).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Note"), "missing an owner");
    expect(reject).toBeEnabled();
    await userEvent.click(reject);
    await waitFor(() => expect(api.decideApproval).toHaveBeenCalledWith("a-1", "rejected", "missing an owner"));
  });

  it("renders the requester's note as text, never as markup", async () => {
    api.fetchMailboxInbox.mockResolvedValue({ approvals: [approval({ request_note: "<b>bold</b>" })], reviews: [] });
    const { baseElement } = render(<MailboxDialog onClose={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: /Approval requested by alice/ }));
    expect(await screen.findByText("<b>bold</b>")).toBeInTheDocument();
    expect(baseElement.querySelector("b")).toBeNull();
  });

  it("shows an already-decided loss as the named current state", async () => {
    api.decideApproval.mockRejectedValue({
      status: 409,
      detail: "approval is already approved",
      error_type: "approval_already_decided",
      current_state: "approved",
    });
    render(<MailboxDialog onClose={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: /Approval requested by alice/ }));
    await screen.findByTestId("mailbox-inspect-yaml");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This request was already approved.");
  });

  it("signs off a review request, and keeps Request changes disabled without a note", async () => {
    api.attestReview.mockResolvedValue({
      attestation_id: "at-1",
      session_id: "s-2",
      state_id: "t-2",
      payload_digest: "sha256:abc",
      reviewer_identity_id: "erin",
      author_identity_id: "alice",
      attested_at: "2026-09-14T10:00:00Z",
      verdict: "signed_off",
      note: null,
    });
    render(<MailboxDialog onClose={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: /Review requested by alice/ }));
    await screen.findByTestId("mailbox-inspect-yaml");
    expect(screen.getByRole("button", { name: "Request changes" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Sign off" }));
    await waitFor(() => expect(api.attestReview).toHaveBeenCalledWith("r-1", "signed_off", ""));
  });

  it("says an inspect 404 means the request is no longer open, and leaves nothing to decide", async () => {
    api.fetchWorkflowInspect.mockRejectedValue({ status: 404, detail: "Not found" });
    render(<MailboxDialog onClose={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: /Approval requested by alice/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This request is no longer open for inspection.");
    await userEvent.type(screen.getByLabelText("Note"), "missing an owner");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(api.decideApproval).not.toHaveBeenCalled();
  });

  it("lists sent requests with the decider and note, marks unread, and stamps one when opened", async () => {
    const decided = approval({
      decision: "rejected",
      decided_at: "2026-09-14T10:00:00Z",
      decided_by_identity_id: "bob",
      decision_note: "missing an owner",
    });
    api.fetchMailboxSent.mockResolvedValue({ approvals: [decided], reviews: [] });
    api.markApprovalSeen.mockResolvedValue(Object.assign({}, decided, { decision_seen_at: "2026-09-14T11:00:00Z" }));
    render(<MailboxDialog onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Sent" }));
    const row = await screen.findByRole("button", { name: /Approval from bob/ });
    expect(row).toHaveTextContent("Rejected by bob");
    expect(row).toHaveTextContent("New");
    expect(screen.getByText("missing an owner")).toBeInTheDocument();
    await userEvent.click(row);
    await waitFor(() => expect(api.markApprovalSeen).toHaveBeenCalledWith("a-1"));
    await waitFor(() => expect(screen.getByRole("button", { name: /Approval from bob/ })).not.toHaveTextContent("New"));
  });

  it("does not mark the requester's own withdrawal as unread", async () => {
    api.fetchMailboxSent.mockResolvedValue({
      approvals: [
        approval({
          decision: "revoked",
          decided_at: "2026-09-14T10:00:00Z",
          revoked_by_identity_id: "alice",
          revocation_actor_kind: "identity",
        }),
      ],
      reviews: [],
    });
    render(<MailboxDialog onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Sent" }));
    const row = await screen.findByRole("button", { name: /Approval from bob/ });
    expect(row).toHaveTextContent("Withdrawn");
    expect(row).not.toHaveTextContent("New");
  });

  it("lists sent review requests with each reviewer's verdict and note, display-only and never unread", async () => {
    api.fetchMailboxSent.mockResolvedValue({
      approvals: [],
      reviews: [
        {
          request: Object.assign({}, REVIEW, { request_note: "check the joins", open: false }),
          attestations: [
            {
              attestation_id: "at-9",
              session_id: "s-2",
              state_id: "t-2",
              payload_digest: "sha256:abc",
              reviewer_identity_id: "erin",
              author_identity_id: "alice",
              attested_at: "2026-09-14T10:00:00Z",
              verdict: "changes_requested",
              note: "rename the sink",
            },
          ],
        },
      ],
    });
    render(<MailboxDialog onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Sent" }));
    const row = await screen.findByTestId("mailbox-sent-review");
    expect(row).toHaveTextContent("Review request to any reviewer: Reviewed");
    expect(row).toHaveTextContent("Changes requested by erin");
    expect(row).toHaveTextContent("rename the sink");
    expect(row).not.toHaveTextContent("New");
    expect(row.querySelector("button")).toBeNull();
    expect(screen.queryByText("You have not sent a request.")).toBeNull();
    expect(api.markApprovalSeen).not.toHaveBeenCalled();
  });

  it("closes on Escape", async () => {
    const onClose = vi.fn();
    render(<MailboxDialog onClose={onClose} />);
    await screen.findByRole("button", { name: /Approval requested by alice/ });
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (reason: unknown) => void } {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason: unknown) => void = () => undefined;
  const promise = new Promise<T>((settle, fail) => {
    resolve = settle;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function inspectionOf(sessionId: string, stateId: string, yaml: string): WorkflowInspect {
  return Object.assign({}, INSPECT, { session_id: sessionId, state_id: stateId, yaml });
}

const FIRST_ITEM: InboxItem = { kind: "approval", approval: approval() };
const SECOND_ITEM: InboxItem = { kind: "approval", approval: approval({ approval_id: "a-2", session_id: "s-3", state_id: "t-3" }) };
const REVIEW_ITEM: InboxItem = { kind: "review", review: REVIEW };

describe("InspectPane decision gating (Task I9, decision 13)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMailboxStore.getState().reset();
    api.fetchMailboxInbox.mockResolvedValue({ approvals: [], reviews: [] });
    api.fetchMailboxSummary.mockResolvedValue({
      governance: "on",
      roles: ["approver", "reviewer"],
      approvals_to_decide: 0,
      reviews_to_attest: 0,
      decisions_unseen: 0,
    });
  });

  it("keeps Approve and Reject disabled while the frozen pipeline is loading, then enables them", async () => {
    const pending = deferred<WorkflowInspect>();
    api.fetchWorkflowInspect.mockReturnValueOnce(pending.promise);
    render(<InspectPane item={FIRST_ITEM} onBack={vi.fn()} onDone={vi.fn()} />);
    expect(screen.getByText("Loading the frozen pipeline")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Note"), "missing an owner");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    // Mutation-derivation: the loaded inspection, not the note or the passage of time, enables the decision.
    await act(async () => {
      pending.resolve(inspectionOf("s-1", "t-1", "first: pipeline\n"));
    });
    expect(screen.getByTestId("mailbox-inspect-yaml")).toHaveTextContent("first: pipeline");
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(api.decideApproval).not.toHaveBeenCalled();
  });

  it.each([
    ["a 404", { status: 404, detail: "Not found" }, "This request is no longer open for inspection."],
    ["a server error", { status: 500, detail: "inspect failed" }, "inspect failed"],
  ])("keeps the decision disabled after %s from the inspection", async (_label, failure, message) => {
    api.fetchWorkflowInspect.mockRejectedValueOnce(failure);
    render(<InspectPane item={FIRST_ITEM} onBack={vi.fn()} onDone={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    await userEvent.type(screen.getByLabelText("Note"), "missing an owner");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(screen.queryByTestId("mailbox-inspect-yaml")).toBeNull();
  });

  it("disables the decision the moment the target changes, until the new target's inspection loads", async () => {
    const second = deferred<WorkflowInspect>();
    api.fetchWorkflowInspect
      .mockResolvedValueOnce(inspectionOf("s-1", "t-1", "first: pipeline\n"))
      .mockReturnValueOnce(second.promise);
    const { rerender } = render(<InspectPane item={FIRST_ITEM} onBack={vi.fn()} onDone={vi.fn()} />);
    expect(await screen.findByTestId("mailbox-inspect-yaml")).toHaveTextContent("first: pipeline");
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    rerender(<InspectPane item={SECOND_ITEM} onBack={vi.fn()} onDone={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.queryByTestId("mailbox-inspect-yaml")).toBeNull();
    expect(api.fetchWorkflowInspect).toHaveBeenLastCalledWith("s-3", "t-3");
    await act(async () => {
      second.resolve(inspectionOf("s-3", "t-3", "second: pipeline\n"));
    });
    expect(screen.getByTestId("mailbox-inspect-yaml")).toHaveTextContent("second: pipeline");
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("ignores a late inspection of the previous target", async () => {
    const first = deferred<WorkflowInspect>();
    const second = deferred<WorkflowInspect>();
    api.fetchWorkflowInspect.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { rerender } = render(<InspectPane item={FIRST_ITEM} onBack={vi.fn()} onDone={vi.fn()} />);
    rerender(<InspectPane item={SECOND_ITEM} onBack={vi.fn()} onDone={vi.fn()} />);
    await act(async () => {
      first.resolve(inspectionOf("s-1", "t-1", "first: pipeline\n"));
    });
    expect(screen.getByText("Loading the frozen pipeline")).toBeInTheDocument();
    expect(screen.queryByTestId("mailbox-inspect-yaml")).toBeNull();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    await act(async () => {
      second.resolve(inspectionOf("s-3", "t-3", "second: pipeline\n"));
    });
    expect(screen.getByTestId("mailbox-inspect-yaml")).toHaveTextContent("second: pipeline");
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("never enables the decision on an inspection of a different state", async () => {
    api.fetchWorkflowInspect.mockResolvedValueOnce(inspectionOf("s-1", "t-other", "other: pipeline\n"));
    render(<InspectPane item={FIRST_ITEM} onBack={vi.fn()} onDone={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The loaded pipeline is not the version this request names.");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(screen.queryByTestId("mailbox-inspect-yaml")).toBeNull();
  });

  it("gates Sign off and Request changes on the review's inspection the same way", async () => {
    const pending = deferred<WorkflowInspect>();
    api.fetchWorkflowInspect.mockReturnValueOnce(pending.promise);
    render(<InspectPane item={REVIEW_ITEM} onBack={vi.fn()} onDone={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Note"), "one more pass");
    expect(screen.getByRole("button", { name: "Sign off" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Request changes" })).toBeDisabled();
    await act(async () => {
      pending.reject({ status: 404, detail: "Not found" });
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("This request is no longer open for inspection.");
    expect(screen.getByRole("button", { name: "Sign off" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Request changes" })).toBeDisabled();
    expect(api.attestReview).not.toHaveBeenCalled();
  });
});
```

Create `src/components/workflow/WorkflowRequestDialog.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WorkflowRequestDialog } from "./WorkflowRequestDialog";
import * as workflow from "@/api/workflow";
import { useMailboxStore } from "@/stores/mailboxStore";

vi.mock("@/api/workflow", () => ({
  fetchApproverDirectory: vi.fn(),
  requestApproval: vi.fn(),
  requestReview: vi.fn(),
  fetchMailboxSent: vi.fn(),
  fetchMailboxSummary: vi.fn(),
}));

const api = vi.mocked(workflow);

describe("WorkflowRequestDialog (Task I9)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMailboxStore.getState().reset();
    api.fetchMailboxSent.mockResolvedValue({ approvals: [], reviews: [] });
    api.fetchMailboxSummary.mockResolvedValue({
      governance: "on",
      roles: [],
      approvals_to_decide: 0,
      reviews_to_attest: 0,
      decisions_unseen: 0,
    });
    api.fetchApproverDirectory.mockResolvedValue({
      approvers: [
        { identity_id: "bob", username: "bob" },
        { identity_id: "carol", username: "carol" },
      ],
      suggested_identity_ids: ["carol"],
    });
  });

  it("preselects the suggested approver and sends the request with its note", async () => {
    api.requestApproval.mockResolvedValue({} as never);
    const onClose = vi.fn();
    render(<WorkflowRequestDialog kind="approval" sessionId="s-1" stateId="t-1" onClose={onClose} />);
    const picker = await screen.findByLabelText("Approver");
    await waitFor(() => expect(picker).toHaveValue("carol"));
    expect(screen.getByRole("option", { name: "carol (suggested)" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Note"), "for the quarterly run");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));
    await waitFor(() =>
      expect(api.requestApproval).toHaveBeenCalledWith("s-1", {
        state_id: "t-1",
        approver_identity_id: "carol",
        note: "for the quarterly run",
      }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(api.fetchMailboxSent).toHaveBeenCalledTimes(1);
  });

  it("says when no approver is available and keeps Send disabled", async () => {
    api.fetchApproverDirectory.mockResolvedValue({ approvers: [], suggested_identity_ids: [] });
    render(<WorkflowRequestDialog kind="approval" sessionId="s-1" stateId="t-1" onClose={vi.fn()} />);
    expect(await screen.findByText("No approver is available on this deployment.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send request" })).toBeDisabled();
  });

  it("names an already-open request instead of closing", async () => {
    api.requestApproval.mockRejectedValue({
      status: 409,
      detail: "an open request exists",
      error_type: "approval_open_request_exists",
    });
    const onClose = vi.fn();
    render(<WorkflowRequestDialog kind="approval" sessionId="s-1" stateId="t-1" onClose={onClose} />);
    await waitFor(() => expect(screen.getByLabelText("Approver")).toHaveValue("carol"));
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("An approval request is already open for this version.");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("asks any reviewer for a review, with no picker", async () => {
    api.requestReview.mockResolvedValue({} as never);
    const onClose = vi.fn();
    render(<WorkflowRequestDialog kind="review" sessionId="s-1" stateId="t-1" onClose={onClose} />);
    expect(screen.queryByLabelText("Approver")).toBeNull();
    expect(api.fetchApproverDirectory).not.toHaveBeenCalled();
    await userEvent.type(screen.getByLabelText("Note"), "please check the joins");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));
    await waitFor(() =>
      expect(api.requestReview).toHaveBeenCalledWith("s-1", { state_id: "t-1", note: "please check the joins" }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });
});
```

- [ ] **Step 24: Run the mailbox UI tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npx vitest run src/components/workflow/MailboxBadge.test.tsx src/components/workflow/MailboxDialog.test.tsx src/components/workflow/WorkflowRequestDialog.test.tsx > /tmp/i9-lane-fe-mailbox-red.log 2>&1; echo exit=$?`
Expected: `exit=1`; each file fails to load with `Failed to resolve import "./MailboxBadge"`, `"./MailboxDialog"` and `"./WorkflowRequestDialog"` respectively.

- [ ] **Step 25: Write the mailbox components and their stylesheet.**

Create `src/components/workflow/MailboxBadge.tsx`:

```tsx
import { Button } from "@/components/ui";
import { badgeCount, useMailboxStore } from "@/stores/mailboxStore";

interface MailboxBadgeProps {
  onOpen: () => void;
}

/**
 * Header badge (spec :1229-1233). Reads the summary the single poll keeps
 * fresh; it never fetches. Renders nothing at zero or with governance off, so
 * the default header is byte-identical to the pre-I9 header.
 */
export function MailboxBadge({ onOpen }: MailboxBadgeProps): JSX.Element | null {
  const summary = useMailboxStore((state) => state.summary);
  const count = badgeCount(summary);
  if (count === 0) return null;
  return (
    <Button variant="bare" className="mailbox-badge" onClick={onOpen} aria-label={`Mailbox, ${count} unread`}>
      Mailbox
      <span className="mailbox-badge-count" aria-hidden="true">
        {count}
      </span>
    </Button>
  );
}
```

Create `src/components/workflow/InboxList.tsx`:

```tsx
import { Button } from "@/components/ui";
import type { ApprovalView, MailboxInbox, ReviewRequestView } from "@/types/workflow";

export type InboxItem =
  | { kind: "approval"; approval: ApprovalView }
  | { kind: "review"; review: ReviewRequestView };

interface InboxListProps {
  inbox: MailboxInbox | null;
  onOpen: (item: InboxItem) => void;
}

export function InboxList({ inbox, onOpen }: InboxListProps): JSX.Element {
  if (inbox === null) {
    return <p className="mailbox-empty">Loading the inbox</p>;
  }
  if (inbox.approvals.length === 0 && inbox.reviews.length === 0) {
    return <p className="mailbox-empty">Nothing is waiting for you.</p>;
  }
  return (
    <ul className="mailbox-list">
      {inbox.approvals.map((approval) => (
        <li key={approval.approval_id} className="mailbox-row">
          <Button variant="bare" className="mailbox-row-open" onClick={() => onOpen({ kind: "approval", approval })}>
            Approval requested by {approval.requested_by_identity_id}
            <span className="mailbox-row-meta">addressed to {approval.approver_identity_id}</span>
          </Button>
        </li>
      ))}
      {inbox.reviews.map((review) => (
        <li key={review.request_id} className="mailbox-row">
          <Button variant="bare" className="mailbox-row-open" onClick={() => onOpen({ kind: "review", review })}>
            Review requested by {review.requested_by_identity_id}
            <span className="mailbox-row-meta">
              {review.reviewer_identity_id === null ? "any reviewer" : `addressed to ${review.reviewer_identity_id}`}
            </span>
          </Button>
        </li>
      ))}
    </ul>
  );
}
```

Create `src/components/workflow/SentList.tsx`:

```tsx
import { Button } from "@/components/ui";
import type { ApprovalView, ReviewAttestationView, ReviewSentView } from "@/types/workflow";

interface SentListProps {
  sent: ApprovalView[] | null;
  reviews: ReviewSentView[] | null;
  onOpen: (approvalId: string) => void;
}

/** The client twin of mailbox.py ``decision_is_unseen``: decided, unseen, and not caused by the requester. */
export function isUnseenDecision(approval: ApprovalView): boolean {
  if (approval.decision === null || approval.decision_seen_at !== null) return false;
  if (approval.decision === "superseded") return false;
  return !(
    approval.decision === "revoked" &&
    approval.revocation_actor_kind === "identity" &&
    approval.revoked_by_identity_id === approval.requested_by_identity_id
  );
}

function statusText(approval: ApprovalView): string {
  switch (approval.decision) {
    case null:
      return "Waiting for a decision";
    case "approved":
      return `Approved by ${approval.decided_by_identity_id ?? approval.approver_identity_id}`;
    case "rejected":
      return `Rejected by ${approval.decided_by_identity_id ?? approval.approver_identity_id}`;
    case "superseded":
      return "Superseded by a newer version";
    case "revoked":
      return approval.revocation_actor_kind === "identity" && approval.revoked_by_identity_id === approval.requested_by_identity_id
        ? "Withdrawn"
        : "Revoked: the approver can no longer decide";
    default: {
      const exhaustive: never = approval.decision;
      throw new Error(`unknown approval decision: ${String(exhaustive)}`);
    }
  }
}

function reviewStatusText(item: ReviewSentView): string {
  if (item.request.cancelled_at !== null) return "Cancelled";
  if (item.request.open) {
    return item.request.reviewer_identity_id === null
      ? "Waiting for any reviewer"
      : `Waiting for ${item.request.reviewer_identity_id}`;
  }
  return "Reviewed";
}

function verdictText(attestation: ReviewAttestationView): string {
  switch (attestation.verdict) {
    case "signed_off":
      return `Signed off by ${attestation.reviewer_identity_id}`;
    case "changes_requested":
      return `Changes requested by ${attestation.reviewer_identity_id}`;
    case "withdrawn":
      return `Withdrawn by ${attestation.reviewer_identity_id}`;
    default: {
      const exhaustive: never = attestation.verdict;
      throw new Error(`unknown review verdict: ${String(exhaustive)}`);
    }
  }
}

/**
 * Sent folder (spec :1228-1229): the caller's approval requests, then the
 * caller's review requests with every attestation I4's `sent_for` attributes
 * to each. A review row is display-only: review requests have no seen column,
 * so it is never "New", and I7's inspect route 404s the requester.
 */
export function SentList({ sent, reviews, onOpen }: SentListProps): JSX.Element {
  if (sent === null || reviews === null) {
    return <p className="mailbox-empty">Loading sent requests</p>;
  }
  if (sent.length === 0 && reviews.length === 0) {
    return <p className="mailbox-empty">You have not sent a request.</p>;
  }
  return (
    <ul className="mailbox-list">
      {sent.map((approval) => (
        <li key={approval.approval_id} className="mailbox-row">
          <Button variant="bare" className="mailbox-row-open" onClick={() => onOpen(approval.approval_id)}>
            Approval from {approval.approver_identity_id}
            <span className="mailbox-row-meta">{statusText(approval)}</span>
            {isUnseenDecision(approval) && <span className="mailbox-row-unread">New</span>}
          </Button>
          {approval.decision_note !== null && <p className="mailbox-note">{approval.decision_note}</p>}
          {approval.decided_at !== null && (
            <span className="mailbox-row-meta">{new Date(approval.decided_at).toLocaleString()}</span>
          )}
        </li>
      ))}
      {reviews.map((item) => (
        <li key={item.request.request_id} className="mailbox-row" data-testid="mailbox-sent-review">
          <span className="mailbox-row-meta">
            Review request to {item.request.reviewer_identity_id ?? "any reviewer"}: {reviewStatusText(item)}
          </span>
          {item.request.request_note !== null && <p className="mailbox-note">{item.request.request_note}</p>}
          {item.attestations.map((attestation) => (
            <div key={attestation.attestation_id}>
              <span className="mailbox-row-meta">
                {verdictText(attestation)}, {new Date(attestation.attested_at).toLocaleString()}
              </span>
              {attestation.note !== null && <p className="mailbox-note">{attestation.note}</p>}
            </div>
          ))}
        </li>
      ))}
    </ul>
  );
}
```

Create `src/components/workflow/InspectPane.tsx`:

```tsx
import { useEffect, useState } from "react";

import { Button } from "@/components/ui";
import * as workflow from "@/api/workflow";
import { useMailboxStore, workflowErrorMessage } from "@/stores/mailboxStore";
import type { ApiError } from "@/types/index";
import type { WorkflowInspect } from "@/types/workflow";
import type { InboxItem } from "./InboxList";

interface InspectPaneProps {
  item: InboxItem;
  onBack: () => void;
  onDone: () => void;
}

/** A settled inspection, tagged with the exact request and state it was fetched for. */
type SettledInspection =
  | { target: string; status: "loaded"; inspect: WorkflowInspect }
  | { target: string; status: "failed"; message: string };

const DECIDE_AFTER_INSPECTION = "Decide once the frozen pipeline for this request has loaded.";
const WRONG_STATE = "The loaded pipeline is not the version this request names.";

/** The request id and the (session, state) pair its decision binds: the one inspection a decision may follow. */
function inspectionTarget(item: InboxItem): string {
  return item.kind === "approval"
    ? `approval/${item.approval.approval_id}/${item.approval.session_id}/${item.approval.state_id}`
    : `review/${item.review.request_id}/${item.review.session_id}/${item.review.state_id}`;
}

/**
 * The frozen read-only inspect view (I7 route) with the requester's note and
 * the decision form. Every note is rendered as text: React escapes it, and no
 * markup path exists here (spec :1416, "rendered as text, never as markup").
 *
 * A decision follows the inspection of exactly this request's state
 * (decision 13). Every decision button stays disabled while that inspection is
 * loading, after it failed, and whenever the settled inspection was fetched
 * for a different target than the one shown now.
 */
export function InspectPane({ item, onBack, onDone }: InspectPaneProps): JSX.Element {
  const target = item.kind === "approval" ? item.approval : item.review;
  const targetKey = inspectionTarget(item);
  const decide = useMailboxStore((state) => state.decide);
  const attest = useMailboxStore((state) => state.attest);
  const [settled, setSettled] = useState<SettledInspection | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    const sessionId = target.session_id;
    const stateId = target.state_id;
    workflow
      .fetchWorkflowInspect(sessionId, stateId)
      .then((result) => {
        if (!live) return;
        setSettled(
          result.session_id === sessionId && result.state_id === stateId
            ? { target: targetKey, status: "loaded", inspect: result }
            : { target: targetKey, status: "failed", message: WRONG_STATE },
        );
      })
      .catch((error: unknown) => {
        if (!live) return;
        setSettled({
          target: targetKey,
          status: "failed",
          message:
            (error as Partial<ApiError>).status === 404
              ? "This request is no longer open for inspection."
              : workflowErrorMessage(error),
        });
      });
    return () => {
      live = false;
    };
  }, [targetKey, target.session_id, target.state_id]);

  // A settled inspection of another target is stale: for the target shown now it is still loading.
  const current = settled !== null && settled.target === targetKey ? settled : null;
  const inspected = current !== null && current.status === "loaded";
  const noteBlank = note.trim() === "";

  async function act(run: () => Promise<boolean>): Promise<void> {
    setBusy(true);
    const succeeded = await run();
    setBusy(false);
    if (succeeded) onDone();
  }

  return (
    <section className="mailbox-inspect" aria-label="Request detail">
      <Button variant="bare" className="mailbox-back" onClick={onBack}>
        Back to inbox
      </Button>
      <h3 className="mailbox-inspect-title">
        {item.kind === "approval" ? "Approval request" : "Review request"} from {target.requested_by_identity_id}
      </h3>
      {target.request_note !== null && <p className="mailbox-note">{target.request_note}</p>}
      {current === null ? (
        <p className="mailbox-empty">Loading the frozen pipeline</p>
      ) : current.status === "failed" ? (
        <p role="alert" className="mailbox-error">
          {current.message}
        </p>
      ) : (
        <pre className="mailbox-inspect-yaml" data-testid="mailbox-inspect-yaml">
          {current.inspect.yaml}
        </pre>
      )}
      <label className="field-label" htmlFor="mailbox-decision-note">
        Note
      </label>
      <textarea
        id="mailbox-decision-note"
        className="textarea"
        value={note}
        maxLength={4096}
        onChange={(event) => setNote(event.target.value)}
      />
      <div className="mailbox-decision-actions">
        {item.kind === "approval" ? (
          <>
            <Button
              variant="primary"
              disabled={busy || !inspected}
              title={inspected ? undefined : DECIDE_AFTER_INSPECTION}
              onClick={() => void act(() => decide(item.approval.approval_id, "approved", note))}
            >
              Approve
            </Button>
            <Button
              variant="danger"
              disabled={busy || !inspected || noteBlank}
              title={!inspected ? DECIDE_AFTER_INSPECTION : noteBlank ? "A rejection needs a note." : undefined}
              onClick={() => void act(() => decide(item.approval.approval_id, "rejected", note))}
            >
              Reject
            </Button>
          </>
        ) : (
          <>
            <Button
              variant="primary"
              disabled={busy || !inspected}
              title={inspected ? undefined : DECIDE_AFTER_INSPECTION}
              onClick={() => void act(() => attest(item.review.request_id, "signed_off", note))}
            >
              Sign off
            </Button>
            <Button
              variant="danger"
              disabled={busy || !inspected || noteBlank}
              title={!inspected ? DECIDE_AFTER_INSPECTION : noteBlank ? "Requesting changes needs a note." : undefined}
              onClick={() => void act(() => attest(item.review.request_id, "changes_requested", note))}
            >
              Request changes
            </Button>
          </>
        )}
      </div>
    </section>
  );
}
```

Create `src/components/workflow/MailboxDialog.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { useMailboxStore } from "@/stores/mailboxStore";
import { InboxList, type InboxItem } from "./InboxList";
import { InspectPane } from "./InspectPane";
import { SentList } from "./SentList";

interface MailboxDialogProps {
  onClose: () => void;
}

/**
 * The mailbox (spec :1204-1234): one surface, two folders. A dialog on the
 * app-dialog primitive, not a hash route: useHashRouter reads any `#/x` as a
 * session id.
 */
export function MailboxDialog({ onClose }: MailboxDialogProps): JSX.Element {
  const modalRef = useRef<HTMLDivElement>(null);
  useFocusTrap(modalRef, true);
  const inbox = useMailboxStore((state) => state.inbox);
  const sent = useMailboxStore((state) => state.sent);
  const sentReviews = useMailboxStore((state) => state.sentReviews);
  const error = useMailboxStore((state) => state.error);
  const loadInbox = useMailboxStore((state) => state.loadInbox);
  const loadSent = useMailboxStore((state) => state.loadSent);
  const openSent = useMailboxStore((state) => state.openSent);
  const [folder, setFolder] = useState<"inbox" | "sent">("inbox");
  const [selected, setSelected] = useState<InboxItem | null>(null);

  useEffect(() => {
    void loadInbox();
    void loadSent();
  }, [loadInbox, loadSent]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <>
      <div role="presentation" onClick={onClose} className="app-dialog-backdrop" />
      <div
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="mailbox-title"
        className="app-dialog settings-dialog settings-dialog-wide"
      >
        <div className="secrets-panel-header">
          <h2 id="mailbox-title" className="secrets-panel-title">
            Mailbox
          </h2>
          <Button variant="bare" onClick={onClose} aria-label="Close mailbox" className="dialog-close">
            ×
          </Button>
        </div>
        <div className="secrets-panel-body">
          <div className="mailbox-tabs">
            <Button
              variant="bare"
              className="mailbox-tab"
              aria-pressed={folder === "inbox"}
              onClick={() => {
                setFolder("inbox");
                setSelected(null);
              }}
            >
              Inbox
            </Button>
            <Button
              variant="bare"
              className="mailbox-tab"
              aria-pressed={folder === "sent"}
              onClick={() => {
                setFolder("sent");
                setSelected(null);
              }}
            >
              Sent
            </Button>
          </div>
          {error !== null && (
            <p role="alert" className="mailbox-error">
              {error}
            </p>
          )}
          {folder === "inbox" && selected !== null ? (
            <InspectPane item={selected} onBack={() => setSelected(null)} onDone={() => setSelected(null)} />
          ) : folder === "inbox" ? (
            <InboxList inbox={inbox} onOpen={setSelected} />
          ) : (
            <SentList sent={sent} reviews={sentReviews} onOpen={(approvalId) => void openSent(approvalId)} />
          )}
        </div>
      </div>
    </>
  );
}
```

Create `src/components/workflow/WorkflowRequestDialog.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui";
import * as workflow from "@/api/workflow";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { useMailboxStore, workflowErrorMessage } from "@/stores/mailboxStore";
import type { ApproverDirectory } from "@/types/workflow";

interface WorkflowRequestDialogProps {
  kind: "approval" | "review";
  sessionId: string;
  stateId: string;
  onClose: () => void;
}

/**
 * Ask for an approval (a picker of live approvers, the author's approver edge
 * preselected; spec :1416) or a review (any active reviewer; spec :1419). The
 * binding and the review digest are computed server-side from the state; the
 * client sends only the state id, the addressee and the note.
 */
export function WorkflowRequestDialog({ kind, sessionId, stateId, onClose }: WorkflowRequestDialogProps): JSX.Element {
  const modalRef = useRef<HTMLDivElement>(null);
  useFocusTrap(modalRef, true);
  const loadSent = useMailboxStore((state) => state.loadSent);
  const refreshSummary = useMailboxStore((state) => state.refreshSummary);
  const [directory, setDirectory] = useState<ApproverDirectory | null>(null);
  const [approver, setApprover] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (kind !== "approval") return;
    let live = true;
    workflow
      .fetchApproverDirectory()
      .then((result) => {
        if (!live) return;
        setDirectory(result);
        setApprover(result.suggested_identity_ids[0] ?? result.approvers[0]?.identity_id ?? "");
      })
      .catch((failure: unknown) => {
        if (live) setError(workflowErrorMessage(failure));
      });
    return () => {
      live = false;
    };
  }, [kind]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  async function submit(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      if (kind === "approval") {
        await workflow.requestApproval(sessionId, { state_id: stateId, approver_identity_id: approver, note });
      } else {
        await workflow.requestReview(sessionId, { state_id: stateId, note });
      }
      await Promise.all([loadSent(), refreshSummary()]);
      onClose();
    } catch (failure) {
      setError(workflowErrorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  const title = kind === "approval" ? "Request approval" : "Request review";
  const noApprover = kind === "approval" && directory !== null && directory.approvers.length === 0;
  const sendDisabled = busy || (kind === "approval" && approver === "");

  return (
    <>
      <div role="presentation" onClick={onClose} className="app-dialog-backdrop" />
      <div ref={modalRef} role="dialog" aria-modal="true" aria-labelledby="workflow-request-title" className="app-dialog settings-dialog">
        <div className="secrets-panel-header">
          <h2 id="workflow-request-title" className="secrets-panel-title">
            {title}
          </h2>
          <Button variant="bare" onClick={onClose} aria-label={`Close ${title.toLowerCase()} dialog`} className="dialog-close">
            ×
          </Button>
        </div>
        <div className="secrets-panel-body workflow-request-form">
          {error !== null && (
            <p role="alert" className="mailbox-error">
              {error}
            </p>
          )}
          {kind === "approval" &&
            (noApprover ? (
              <p className="mailbox-empty">No approver is available on this deployment.</p>
            ) : (
              <>
                <label className="field-label" htmlFor="workflow-request-approver">
                  Approver
                </label>
                <select
                  id="workflow-request-approver"
                  className="input"
                  value={approver}
                  onChange={(event) => setApprover(event.target.value)}
                >
                  {(directory?.approvers ?? []).map((entry) => (
                    <option key={entry.identity_id} value={entry.identity_id}>
                      {directory?.suggested_identity_ids.includes(entry.identity_id)
                        ? `${entry.username} (suggested)`
                        : entry.username}
                    </option>
                  ))}
                </select>
              </>
            ))}
          <label className="field-label" htmlFor="workflow-request-note">
            Note
          </label>
          <textarea
            id="workflow-request-note"
            className="textarea"
            value={note}
            maxLength={4096}
            onChange={(event) => setNote(event.target.value)}
          />
          <div className="workflow-request-actions">
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" disabled={sendDisabled} onClick={() => void submit()}>
              Send request
            </Button>
          </div>
        </div>
      </div>
    </>
  );
}
```

Create `src/components/workflow/workflow.css`. Every class the workflow components emit has a rule here; `src/styles/classNames.test.ts` gates that. Later steps append the admin, library and readiness sections.

```css
/* workflow.css — Task I9: mailbox, workflow request dialog, readiness approval
   row, identity administration, library and quota status. Every var() is a
   token defined in styles/tokens.css (tokenReferences.test.ts gates it). */

/* ── Header badge ─────────────────────────────────────────────────────── */

.mailbox-badge {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-2xs) var(--space-sm);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text);
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.mailbox-badge:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 2px;
}

.mailbox-badge-count {
  min-width: var(--space-lg);
  padding: 0 var(--space-xs);
  border-radius: var(--radius-sm);
  background: var(--color-accent);
  color: var(--color-text-inverse);
  font-size: var(--font-size-xs);
  text-align: center;
}

/* ── Mailbox dialog ───────────────────────────────────────────────────── */

.mailbox-tabs {
  display: flex;
  gap: var(--space-sm);
  margin-bottom: var(--space-md);
}

.mailbox-tab {
  padding: var(--space-xs) var(--space-md);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.mailbox-tab[aria-pressed="true"] {
  border-color: var(--color-accent);
  color: var(--color-text);
}

.mailbox-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  margin: 0;
  padding: 0;
  list-style: none;
}

.mailbox-row {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  padding: var(--space-sm) 0;
  border-bottom: 1px solid var(--color-border);
}

.mailbox-row-open {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-sm);
  padding: 0;
  border: none;
  background: transparent;
  color: var(--color-text);
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
}

.mailbox-row-open:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 2px;
}

.mailbox-row-meta {
  color: var(--color-text-muted);
  font-size: var(--font-size-xs);
}

.mailbox-row-unread {
  padding: 0 var(--space-xs);
  border-radius: var(--radius-sm);
  background: var(--color-accent);
  color: var(--color-text-inverse);
  font-size: var(--font-size-xs);
}

.mailbox-empty {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.mailbox-error {
  margin: 0 0 var(--space-sm);
  padding: var(--space-sm);
  border: 1px solid var(--color-error-border);
  border-radius: var(--radius-md);
  background: var(--color-error-bg);
  color: var(--color-error);
  font-size: var(--font-size-sm);
}

.mailbox-note {
  margin: 0;
  color: var(--color-text);
  font-size: var(--font-size-sm);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.mailbox-inspect {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}

.mailbox-back {
  align-self: flex-start;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--color-accent);
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.mailbox-inspect-title {
  margin: 0;
  color: var(--color-text);
  font-size: var(--font-size-md);
}

.mailbox-inspect-yaml {
  max-height: 40vh;
  margin: 0;
  padding: var(--space-sm);
  overflow: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-elevated);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  white-space: pre;
}

.mailbox-decision-actions,
.workflow-request-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-sm);
}

.workflow-request-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}
```

In `src/styles/index.css`, directly after `@import "../components/settings/settings.css";` (:33), add:

```css
@import "../components/workflow/workflow.css";
```

- [ ] **Step 26: Run the mailbox UI tests and the tree-wide census gates to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npx vitest run src/components/workflow/MailboxBadge.test.tsx src/components/workflow/MailboxDialog.test.tsx src/components/workflow/WorkflowRequestDialog.test.tsx src/styles/classNames.test.ts src/styles/tokenReferences.test.ts src/components/ui/primitiveCensus.test.ts > /tmp/i9-lane-fe-mailbox-green.log 2>&1; echo exit=$?`
Expected: `exit=0`; the three new files contribute `3`, `17` and `4` passes.

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npm run lint:css > /tmp/i9-lane-fe-stylelint-1.log 2>&1; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 27: Write the failing tests for the readiness approval row and the executionStore approval refusal.**

Create `src/components/workflow/ApprovalReadinessRow.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApprovalReadinessRow } from "./ApprovalReadinessRow";
import * as workflow from "@/api/workflow";
import { useMailboxStore } from "@/stores/mailboxStore";
import { useExecutionStore } from "@/stores/executionStore";
import type { ApprovalView, MailboxSummary } from "@/types/workflow";

vi.mock("@/api/workflow", () => ({
  fetchMailboxSent: vi.fn(),
  fetchMailboxSummary: vi.fn(),
  fetchApproverDirectory: vi.fn(),
  requestApproval: vi.fn(),
  requestReview: vi.fn(),
}));

const api = vi.mocked(workflow);

const ON: MailboxSummary = {
  governance: "on",
  roles: [],
  approvals_to_decide: 0,
  reviews_to_attest: 0,
  decisions_unseen: 0,
};

function approval(overrides: Partial<ApprovalView> = {}): ApprovalView {
  return Object.assign(
    {
      approval_id: "a-1",
      session_id: "s-1",
      state_id: "t-1",
      binding: {},
      requested_by_identity_id: "alice",
      approver_identity_id: "bob",
      requested_at: "2026-09-14T09:00:00Z",
      decided_at: null,
      decision: null,
      request_note: null,
      decision_seen_at: null,
      decided_by_identity_id: null,
      decision_note: null,
      revoked_by_identity_id: null,
      revocation_actor_kind: null,
      revocation_event_id: null,
    },
    overrides,
  ) as ApprovalView;
}

describe("ApprovalReadinessRow (Task I9)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMailboxStore.getState().reset();
    useExecutionStore.getState().reset();
    api.fetchMailboxSent.mockResolvedValue({ approvals: [], reviews: [] });
    api.fetchMailboxSummary.mockResolvedValue(ON);
    api.fetchApproverDirectory.mockResolvedValue({ approvers: [{ identity_id: "bob", username: "bob" }], suggested_identity_ids: [] });
  });

  it("renders nothing while governance is off and no run was refused", () => {
    useMailboxStore.setState({ summary: Object.assign({}, ON, { governance: "off" }) });
    const { container } = render(<ApprovalReadinessRow sessionId="s-1" stateId="t-1" />);
    expect(container).toBeEmptyDOMElement();
    expect(api.fetchMailboxSent).not.toHaveBeenCalled();
  });

  it("asks for an approval when this state has none, and opens the request dialog", async () => {
    useMailboxStore.setState({ summary: ON });
    render(<ApprovalReadinessRow sessionId="s-1" stateId="t-1" />);
    const row = await screen.findByTestId("audit-readiness-approval");
    await waitFor(() => expect(api.fetchMailboxSent).toHaveBeenCalledTimes(1));
    expect(row).toHaveAttribute("data-status", "warning");
    expect(row).toHaveTextContent("This pipeline needs an approval before it can run.");
    await userEvent.click(screen.getByRole("button", { name: "Request approval" }));
    expect(await screen.findByRole("heading", { name: "Request approval" })).toBeInTheDocument();
  });

  it("shows an open request as waiting, with no second request offered", () => {
    useMailboxStore.setState({ summary: ON, sent: [approval()] });
    render(<ApprovalReadinessRow sessionId="s-1" stateId="t-1" />);
    expect(screen.getByTestId("audit-readiness-approval")).toHaveTextContent("Waiting for bob to decide.");
    expect(screen.queryByRole("button", { name: "Request approval" })).toBeNull();
  });

  it("is OK only for an approval of exactly this state", () => {
    const approved = approval({ decision: "approved", decided_at: "2026-09-14T10:00:00Z", decided_by_identity_id: "bob" });
    useMailboxStore.setState({ summary: ON, sent: [approved] });
    const { rerender } = render(<ApprovalReadinessRow sessionId="s-1" stateId="t-1" />);
    expect(screen.getByTestId("audit-readiness-approval")).toHaveAttribute("data-status", "ok");
    expect(screen.getByTestId("audit-readiness-approval")).toHaveTextContent("Approved by bob.");
    // Mutation-derivation: the same approval row for a DIFFERENT state approves nothing here.
    rerender(<ApprovalReadinessRow sessionId="s-1" stateId="t-2" />);
    expect(screen.getByTestId("audit-readiness-approval")).toHaveAttribute("data-status", "warning");
  });

  it("shows a rejection with its note as text and offers a new request", () => {
    useMailboxStore.setState({
      summary: ON,
      sent: [approval({ decision: "rejected", decided_at: "2026-09-14T10:00:00Z", decided_by_identity_id: "bob", decision_note: "<i>no owner</i>" })],
    });
    const { container } = render(<ApprovalReadinessRow sessionId="s-1" stateId="t-1" />);
    expect(screen.getByTestId("audit-readiness-approval")).toHaveAttribute("data-status", "error");
    expect(screen.getByText("<i>no owner</i>")).toBeInTheDocument();
    expect(container.querySelector("i")).toBeNull();
    expect(screen.getByRole("button", { name: "Request approval" })).toBeInTheDocument();
  });

  it("renders the execute-time binding mismatch even before the summary has loaded", () => {
    useExecutionStore.setState({ pendingApproval: { sessionId: "s-1", errorType: "approval_binding_mismatch" } });
    render(<ApprovalReadinessRow sessionId="s-1" stateId="t-1" />);
    const row = screen.getByTestId("audit-readiness-approval");
    expect(row).toHaveAttribute("data-status", "error");
    expect(row).toHaveTextContent("The approval no longer matches this pipeline.");
    // Mutation-derivation: a refusal recorded for ANOTHER session renders nothing here.
    act(() => {
      useExecutionStore.setState({ pendingApproval: { sessionId: "s-9", errorType: "approval_binding_mismatch" } });
    });
    expect(screen.queryByTestId("audit-readiness-approval")).toBeNull();
  });

  // Task I3 decision 8: one state can carry several decided requests. The row keys on the newest by a
  // stated total order (requested_at as an instant, then approval_id), never on the order Sent arrives in.
  it.each([
    ["an equal binding", "2"],
    ["a changed binding", "9"],
  ])("shows the newest approval when one state was approved twice, with %s", (_label, olderCatalogDigit) => {
    const older = approval({
      approval_id: "a-1",
      binding: { openrouter_catalog_sha256: olderCatalogDigit.repeat(64) },
      requested_at: "2026-09-14T09:00:00Z",
      decision: "approved",
      decided_at: "2026-09-14T09:30:00Z",
      decided_by_identity_id: "bob",
    });
    const newer = approval({
      approval_id: "a-2",
      binding: { openrouter_catalog_sha256: "2".repeat(64) },
      requested_at: "2026-09-14T11:00:00Z",
      decision: "approved",
      decided_at: "2026-09-14T11:30:00Z",
      decided_by_identity_id: "carol",
    });
    for (const sent of [[older, newer], [newer, older]]) {
      useMailboxStore.setState({ summary: ON, sent });
      const { unmount } = render(<ApprovalReadinessRow sessionId="s-1" stateId="t-1" />);
      const row = screen.getByTestId("audit-readiness-approval");
      expect(row).toHaveAttribute("data-status", "ok");
      expect(row).toHaveTextContent("Approved by carol.");
      expect(screen.queryByRole("button", { name: "Request approval" })).toBeNull();
      unmount();
    }
  });

  it("breaks a tied requested_at on approval_id and compares instants, not strings", () => {
    const rejected = approval({ approval_id: "a-1", decision: "rejected", decided_at: "2026-09-14T09:00:05Z", decided_by_identity_id: "bob" });
    const approved = approval({ approval_id: "a-2", decision: "approved", decided_at: "2026-09-14T09:00:05Z", decided_by_identity_id: "carol" });
    // Both carry the helper's requested_at, so only approval_id can order them.
    for (const sent of [[rejected, approved], [approved, rejected]]) {
      useMailboxStore.setState({ summary: ON, sent });
      const { unmount } = render(<ApprovalReadinessRow sessionId="s-1" stateId="t-1" />);
      expect(screen.getByTestId("audit-readiness-approval")).toHaveTextContent("Approved by carol.");
      unmount();
    }
    // PostgreSQL sends microseconds and SQLite whole seconds: as strings "...09:00:00.500000Z" sorts BEFORE "...09:00:00Z".
    const later = approval({ approval_id: "a-0", requested_at: "2026-09-14T09:00:00.500000Z", approver_identity_id: "dave" });
    useMailboxStore.setState({ summary: ON, sent: [approved, later] });
    render(<ApprovalReadinessRow sessionId="s-1" stateId="t-1" />);
    expect(screen.getByTestId("audit-readiness-approval")).toHaveTextContent("Waiting for dave to decide.");
  });
});
```

Create `src/stores/executionStore.approval.test.ts`:

```ts
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  APPROVAL_BINDING_MISMATCH_ERROR,
  APPROVAL_REQUIRED_ERROR,
  useExecutionStore,
} from "./executionStore";
import { useInterpretationEventsStore } from "./interpretationEventsStore";
import { useSessionStore } from "./sessionStore";
import { resetStore } from "@/test/store-helpers";

vi.mock("@/api/client", () => ({
  validatePipeline: vi.fn(),
  listInterpretationEvents: vi.fn().mockResolvedValue([]),
  executePipeline: vi.fn(),
  cancelRun: vi.fn(),
  createRunWebSocketTicket: vi.fn(),
  fetchRuns: vi.fn().mockResolvedValue([]),
  fetchRunDiagnostics: vi.fn(),
  evaluateRunDiagnostics: vi.fn(),
}));

vi.mock("@/api/websocket", () => ({
  connectToRun: vi.fn(),
}));

describe("executionStore approval refusal (Task I9, R2)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useExecutionStore.getState().reset();
    resetStore(useInterpretationEventsStore);
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: { id: "state-1", version: 1, sources: {}, nodes: [], edges: [], outputs: [] },
    } as never);
  });

  it("holds an approval_required 409 as a pending approval, not as a run conflict", async () => {
    const { executePipeline } = await import("@/api/client");
    (executePipeline as ReturnType<typeof vi.fn>).mockRejectedValue({
      status: 409,
      detail: "Run admission refused: approval_required",
      error_type: "approval_required",
    });
    const runId = await useExecutionStore.getState().execute("session-1");
    const state = useExecutionStore.getState();
    expect(runId).toBeNull();
    expect(state.pendingApproval).toEqual({ sessionId: "session-1", errorType: "approval_required" });
    expect(state.error).toBe(APPROVAL_REQUIRED_ERROR);
    expect(state.isExecuting).toBe(false);
  });

  it("names a binding mismatch separately", async () => {
    const { executePipeline } = await import("@/api/client");
    (executePipeline as ReturnType<typeof vi.fn>).mockRejectedValue({
      status: 409,
      detail: "Run admission refused: approval_binding_mismatch",
      error_type: "approval_binding_mismatch",
    });
    await useExecutionStore.getState().execute("session-1");
    expect(useExecutionStore.getState().pendingApproval).toEqual({
      sessionId: "session-1",
      errorType: "approval_binding_mismatch",
    });
    expect(useExecutionStore.getState().error).toBe(APPROVAL_BINDING_MISMATCH_ERROR);
  });

  it("keeps a plain 409 as the run-in-progress conflict (the error_type, not the status, selects the arm)", async () => {
    const { executePipeline } = await import("@/api/client");
    (executePipeline as ReturnType<typeof vi.fn>).mockRejectedValue({ status: 409, detail: "busy" });
    await useExecutionStore.getState().execute("session-1");
    expect(useExecutionStore.getState().pendingApproval).toBeNull();
    expect(useExecutionStore.getState().error).toBe("A run is already in progress for this pipeline.");
  });

  it("clears the pending approval when a run launches and on reset", async () => {
    const { executePipeline } = await import("@/api/client");
    useExecutionStore.setState({ pendingApproval: { sessionId: "session-1", errorType: "approval_required" } });
    (executePipeline as ReturnType<typeof vi.fn>).mockResolvedValue({ run_id: "run-1" });
    await useExecutionStore.getState().execute("session-1");
    expect(useExecutionStore.getState().pendingApproval).toBeNull();
    useExecutionStore.setState({ pendingApproval: { sessionId: "session-1", errorType: "approval_required" } });
    useExecutionStore.getState().reset();
    expect(useExecutionStore.getState().pendingApproval).toBeNull();
  });
});
```

- [ ] **Step 28: Run the readiness-row and executionStore tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npx vitest run src/components/workflow/ApprovalReadinessRow.test.tsx src/stores/executionStore.approval.test.ts > /tmp/i9-lane-fe-approval-red.log 2>&1; echo exit=$?`
Expected: `exit=1`. `ApprovalReadinessRow.test.tsx` fails to load with `Failed to resolve import "./ApprovalReadinessRow"`. In `executionStore.approval.test.ts` the first test fails with `expected undefined to deeply equal { sessionId: 'session-1', errorType: 'approval_required' }`, the second fails the same way, and the third and fourth fail with `expected undefined to be null`.

- [ ] **Step 29: Write the approval row, the executionStore approval arm and the panel mount.**

Create `src/components/workflow/ApprovalReadinessRow.tsx`:

```tsx
import { useEffect, useState } from "react";

import { Button } from "@/components/ui";
import { approvalForState, useMailboxStore } from "@/stores/mailboxStore";
import { useExecutionStore, type PendingApproval } from "@/stores/executionStore";
import type { ApprovalView } from "@/types/workflow";
import { WorkflowRequestDialog } from "./WorkflowRequestDialog";

interface ApprovalReadinessRowProps {
  sessionId: string;
  stateId: string;
}

export interface ApprovalRowView {
  status: "ok" | "warning" | "error";
  summary: string;
  note: string | null;
  canRequestApproval: boolean;
}

const GLYPH: Record<ApprovalRowView["status"], { glyph: string; aria: string }> = {
  ok: { glyph: "✓", aria: "OK" },
  warning: { glyph: "⚠", aria: "Warning" },
  error: { glyph: "✗", aria: "Error" },
};

/** The row's state: the execute-time refusal first, then the newest request for this exact state. */
export function approvalRowView(approval: ApprovalView | null, pendingError: PendingApproval["errorType"] | null): ApprovalRowView {
  if (pendingError === "approval_binding_mismatch") {
    return { status: "error", summary: "The approval no longer matches this pipeline. Request a new approval.", note: null, canRequestApproval: true };
  }
  if (approval === null) {
    return { status: "warning", summary: "This pipeline needs an approval before it can run.", note: null, canRequestApproval: true };
  }
  switch (approval.decision) {
    case null:
      return { status: "warning", summary: `Waiting for ${approval.approver_identity_id} to decide.`, note: null, canRequestApproval: false };
    case "approved":
      return {
        status: "ok",
        summary: `Approved by ${approval.decided_by_identity_id ?? approval.approver_identity_id}.`,
        note: approval.decision_note,
        canRequestApproval: false,
      };
    case "rejected":
      return {
        status: "error",
        summary: `Rejected by ${approval.decided_by_identity_id ?? approval.approver_identity_id}.`,
        note: approval.decision_note,
        canRequestApproval: true,
      };
    case "superseded":
      return { status: "warning", summary: "The request was superseded by a newer version. Request a new approval.", note: null, canRequestApproval: true };
    case "revoked":
      return { status: "warning", summary: "The request was withdrawn or revoked. Request a new approval.", note: null, canRequestApproval: true };
    default: {
      const exhaustive: never = approval.decision;
      throw new Error(`unknown approval decision: ${String(exhaustive)}`);
    }
  }
}

/**
 * The authorization row (R2), rendered after the six audit-readiness rows and
 * deliberately NOT one of them: ReadinessRowId is a closed wire vocabulary with
 * no backend approval row. It carries the completion gestures "Request
 * approval" and "Request review" because the completion bar's grid is pinned.
 */
export function ApprovalReadinessRow({ sessionId, stateId }: ApprovalReadinessRowProps): JSX.Element | null {
  const summary = useMailboxStore((state) => state.summary);
  const sent = useMailboxStore((state) => state.sent);
  const loadSent = useMailboxStore((state) => state.loadSent);
  const pendingApproval = useExecutionStore((state) => state.pendingApproval);
  const [requestKind, setRequestKind] = useState<"approval" | "review" | null>(null);
  const governanceOn = summary?.governance === "on";
  const pendingHere = pendingApproval !== null && pendingApproval.sessionId === sessionId;

  useEffect(() => {
    if (governanceOn && sent === null) void loadSent();
  }, [governanceOn, sent, loadSent]);

  if (!governanceOn && !pendingHere) return null;

  const view = approvalRowView(approvalForState(sent, sessionId, stateId), pendingHere ? pendingApproval.errorType : null);
  const { glyph, aria } = GLYPH[view.status];

  return (
    <div className="audit-readiness-approval" data-testid="audit-readiness-approval" data-status={view.status}>
      <p className="audit-readiness-approval-heading">
        <span aria-hidden="true">{glyph}</span>
        <span className="sr-only">{aria}: </span>
        Approval
      </p>
      <p className="audit-readiness-approval-summary">{view.summary}</p>
      {view.note !== null && <p className="mailbox-note">{view.note}</p>}
      <div className="audit-readiness-approval-actions">
        {view.canRequestApproval && (
          <Button compact onClick={() => setRequestKind("approval")}>
            Request approval
          </Button>
        )}
        {governanceOn && (
          <Button compact onClick={() => setRequestKind("review")}>
            Request review
          </Button>
        )}
      </div>
      {requestKind !== null && (
        <WorkflowRequestDialog kind={requestKind} sessionId={sessionId} stateId={stateId} onClose={() => setRequestKind(null)} />
      )}
    </div>
  );
}
```

In `src/stores/executionStore.ts`:

1. Directly above `const STALE_FANOUT_READINESS_ERROR =` (:43), add:

```ts
/** R2 execute refusals (Task I3's 409 error types), as the pending-approval state names them. */
export interface PendingApproval {
  sessionId: string;
  errorType: "approval_required" | "approval_binding_mismatch";
}

export const APPROVAL_REQUIRED_ERROR =
  "This pipeline needs an approved request before it can run. Request one from the Approval row in the Audit panel.";
export const APPROVAL_BINDING_MISMATCH_ERROR =
  "The approval on this pipeline no longer matches it. Request a new approval from the Approval row in the Audit panel.";

```

2. Replace `  pendingSecretSessionId: string | null;` (:101, the interface member) with:

```ts
  pendingSecretSessionId: string | null;
  /** Set by an R2 409 from execute; cleared by a successful launch and by reset(). */
  pendingApproval: PendingApproval | null;
```

3. Replace `  pendingSecretSessionId: null as string | null,` (:433, the initial state) with:

```ts
  pendingSecretSessionId: null as string | null,
  pendingApproval: null as PendingApproval | null,
```

4. Replace the two lines at :539-540 in `execute`'s success `set({`:

```ts
        pendingSecretFanoutAck: null,
        // A fresh launch supersedes any unacknowledged prior outcome: the
```

with:

```ts
        pendingSecretFanoutAck: null,
        pendingApproval: null,
        // A fresh launch supersedes any unacknowledged prior outcome: the
```

5. Replace the generic conflict block that starts at :603:

```ts
      const message =
        apiErr.status === 409
          ? "A run is already in progress for this pipeline."
```

with:

```ts
      if (
        apiErr.status === 409 &&
        (apiErr.error_type === "approval_required" || apiErr.error_type === "approval_binding_mismatch")
      ) {
        set({
          isExecuting: false,
          pendingApproval: { sessionId, errorType: apiErr.error_type },
          error: apiErr.error_type === "approval_required" ? APPROVAL_REQUIRED_ERROR : APPROVAL_BINDING_MISMATCH_ERROR,
        });
        return null;
      }
      const message =
        apiErr.status === 409
          ? "A run is already in progress for this pipeline."
```

`reset()` (:1044) spreads `initialExecutionState`, so the new initial-state field clears there with no further edit.

In `src/components/audit/AuditReadinessPanel.tsx`:

1. Directly after `import { useAuditReadinessStore } from "../../stores/auditReadinessStore";` (:17), add:

```ts
import { ApprovalReadinessRow } from "../workflow/ApprovalReadinessRow";
```

2. Replace the rows list's closing tag and the section end (:633-634):

```tsx
        </ul>
      </section>
```

with:

```tsx
        </ul>
        <ApprovalReadinessRow sessionId={activeSessionId} stateId={compositionState.id} />
      </section>
```

Append to `src/components/workflow/workflow.css`:

```css
/* ── Readiness approval row ───────────────────────────────────────────── */

.audit-readiness-approval {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  margin-top: var(--space-md);
  padding: var(--space-sm) var(--space-md);
  border-left: 3px solid var(--color-border);
}

.audit-readiness-approval[data-status="ok"] {
  border-left-color: var(--color-success);
}

.audit-readiness-approval[data-status="warning"] {
  border-left-color: var(--color-warning);
}

.audit-readiness-approval[data-status="error"] {
  border-left-color: var(--color-error);
}

.audit-readiness-approval-heading {
  display: flex;
  gap: var(--space-xs);
  margin: 0;
  color: var(--color-text);
  font-size: var(--font-size-sm);
  font-weight: 600;
}

.audit-readiness-approval-summary {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.audit-readiness-approval-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
}
```

- [ ] **Step 30: Run the approval-row, executionStore and readiness-panel suites and the census gates to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npx vitest run src/components/workflow/ApprovalReadinessRow.test.tsx src/stores/executionStore.approval.test.ts src/stores/executionStore.test.ts src/components/audit/AuditReadinessPanel.test.tsx src/styles/classNames.test.ts src/styles/tokenReferences.test.ts > /tmp/i9-lane-fe-approval-green.log 2>&1; echo exit=$?`
Expected: `exit=0`; the two new files contribute `9` and `4` passes (`ApprovalReadinessRow.test.tsx` has eight test blocks, and the multiplicity `it.each` runs twice). AuditReadinessPanel.test.tsx is unchanged and green: it never seeds a mailbox summary, so the approval row renders nothing.

- [ ] **Step 31: Write the failing identity-administration UI tests.**

Create `src/components/admin/IdentitiesTable.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IdentitiesTable } from "./IdentitiesTable";
import * as workflow from "@/api/workflow";
import type { IdentityView } from "@/types/workflow";

vi.mock("@/api/workflow", () => ({
  fetchIdentityQuota: vi.fn(),
  enableIdentity: vi.fn(),
  disableIdentity: vi.fn(),
}));

const api = vi.mocked(workflow);

function identity(overrides: Partial<IdentityView> = {}): IdentityView {
  return Object.assign(
    {
      identity_id: "id-alice",
      provider: "vanguard",
      kind: "human",
      subject: "sub-alice",
      organisation_id: "org-1",
      access_state: "active",
      username: "alice",
      display_name: "Alice Author",
      email: "alice@example.com",
      first_seen_at: "2026-09-01T00:00:00Z",
      last_login_at: "2026-09-13T00:00:00Z",
      pre_provisioned_at: null,
      activated_at: "2026-09-02T00:00:00Z",
      activated_by_identity_id: "id-root",
      disabled_at: null,
      disabled_by_identity_id: null,
      disable_reason: null,
    },
    overrides,
  ) as IdentityView;
}

describe("IdentitiesTable (Task I9)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchIdentityQuota.mockResolvedValue({
      identity_id: "id-alice",
      tokens_per_day: 500,
      storage_bytes: 2048,
      container_tokens_per_day: null,
      container_storage_bytes: null,
      tokens_used_today: 160,
      storage_bytes_used: 1024,
    });
  });

  it("shows a pending row's subject and organisation only, and never a display name or email on any row", async () => {
    const pending = identity({
      identity_id: "id-pat",
      subject: "sub-pat",
      organisation_id: "org-9",
      access_state: "pending",
      username: null,
      activated_at: null,
      // Defence in depth: the route already blanks these for a never-admitted row.
      display_name: "Leaky Display",
      email: "leak@example.com",
    });
    render(<IdentitiesTable identities={[pending, identity()]} onSelect={vi.fn()} onChanged={vi.fn()} />);
    const pendingRow = screen.getByRole("row", { name: /sub-pat/ });
    expect(within(pendingRow).getByText("org-9")).toBeInTheDocument();
    expect(screen.queryByText("Leaky Display")).toBeNull();
    expect(screen.queryByText("leak@example.com")).toBeNull();
    expect(screen.queryByText("Alice Author")).toBeNull();
    expect(screen.queryByText("alice@example.com")).toBeNull();
    await waitFor(() => expect(api.fetchIdentityQuota).toHaveBeenCalledTimes(1));
    expect(api.fetchIdentityQuota).toHaveBeenCalledWith("id-alice");
  });

  it("shows both quotas with current usage on an active row", async () => {
    render(<IdentitiesTable identities={[identity()]} onSelect={vi.fn()} onChanged={vi.fn()} />);
    const row = screen.getByRole("row", { name: /alice/ });
    expect(await within(row).findByText("Tokens today: 160 of 500")).toBeInTheDocument();
    expect(within(row).getByText("Storage: 1.0 KB of 2.0 KB")).toBeInTheDocument();
  });

  it("says unknown, never zero, when a day's token usage is unmeasured", async () => {
    api.fetchIdentityQuota.mockResolvedValue({
      identity_id: "id-alice",
      tokens_per_day: 500,
      storage_bytes: 2048,
      container_tokens_per_day: null,
      container_storage_bytes: null,
      tokens_used_today: null,
      storage_bytes_used: 0,
    });
    render(<IdentitiesTable identities={[identity()]} onSelect={vi.fn()} onChanged={vi.fn()} />);
    expect(await screen.findByText("Tokens today: unknown of 500")).toBeInTheDocument();
  });

  it("disables an identity only with a reason, then reports the change", async () => {
    api.disableIdentity.mockResolvedValue({ identity: identity({ access_state: "disabled" }) });
    const onChanged = vi.fn();
    render(<IdentitiesTable identities={[identity()]} onSelect={vi.fn()} onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: "Disable alice" }));
    const confirm = screen.getByRole("button", { name: "Confirm disable" });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason for disabling alice"), "left the team");
    await userEvent.click(confirm);
    await waitFor(() => expect(api.disableIdentity).toHaveBeenCalledWith("id-alice", "left the team"));
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  });

  it("shows the authority's refusal when the last active admin would be disabled", async () => {
    api.disableIdentity.mockRejectedValue({
      status: 409,
      error_type: "last_active_admin_protected",
      detail: "the last active admin cannot be disabled",
    });
    render(<IdentitiesTable identities={[identity()]} onSelect={vi.fn()} onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Disable alice" }));
    await userEvent.type(screen.getByLabelText("Reason for disabling alice"), "rotation");
    await userEvent.click(screen.getByRole("button", { name: "Confirm disable" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("the last active admin cannot be disabled");
  });

  it("enables a disabled identity with a note, and opens an active identity's editors", async () => {
    api.enableIdentity.mockResolvedValue(identity());
    const onSelect = vi.fn();
    render(
      <IdentitiesTable
        identities={[identity({ identity_id: "id-dan", username: "dan", subject: "sub-dan", access_state: "disabled" }), identity()]}
        onSelect={onSelect}
        onChanged={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Enable dan" }));
    await userEvent.type(screen.getByLabelText("Note for enabling dan"), "back from leave");
    await userEvent.click(screen.getByRole("button", { name: "Confirm enable" }));
    await waitFor(() => expect(api.enableIdentity).toHaveBeenCalledWith("id-dan", "back from leave"));
    await userEvent.click(screen.getByRole("button", { name: "Manage alice" }));
    expect(onSelect).toHaveBeenCalledWith("id-alice");
  });
});
```

Create `src/components/admin/AdminDialog.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AdminDialog } from "./AdminDialog";
import * as workflow from "@/api/workflow";

vi.mock("@/api/workflow", () => ({
  fetchIdentities: vi.fn(),
  fetchIdentityQuota: vi.fn(),
  enableIdentity: vi.fn(),
  disableIdentity: vi.fn(),
  fetchRoles: vi.fn(),
  grantRole: vi.fn(),
  revokeRole: vi.fn(),
  fetchRelationships: vi.fn(),
  assertApproverRelationship: vi.fn(),
  revokeRelationship: vi.fn(),
  setIdentityQuota: vi.fn(),
}));

const api = vi.mocked(workflow);

function list(activeHumanAdmins: number, accessState: "pending" | "active" | "disabled" = "pending") {
  return { identities: [], access_state: accessState, limit: 200, offset: 0, active_human_admin_count: activeHumanAdmins };
}

describe("AdminDialog (Task I9)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows the single-admin advisory when exactly one active human admin holds the container", async () => {
    api.fetchIdentities.mockResolvedValue(list(1));
    render(<AdminDialog onClose={vi.fn()} />);
    expect(await screen.findByRole("note")).toHaveTextContent("Only one active human administrator holds this container.");
    expect(api.fetchIdentities).toHaveBeenCalledWith("pending");
  });

  it("omits the advisory when the count is not one (the count, not the list, decides)", async () => {
    api.fetchIdentities.mockResolvedValue(list(2));
    render(<AdminDialog onClose={vi.fn()} />);
    await waitFor(() => expect(api.fetchIdentities).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("No identities in this state.")).toBeInTheDocument();
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("refetches when the access-state filter changes", async () => {
    api.fetchIdentities.mockResolvedValueOnce(list(2)).mockResolvedValueOnce(list(2, "active"));
    render(<AdminDialog onClose={vi.fn()} />);
    await waitFor(() => expect(api.fetchIdentities).toHaveBeenCalledWith("pending"));
    await userEvent.selectOptions(screen.getByLabelText("Show"), "active");
    await waitFor(() => expect(api.fetchIdentities).toHaveBeenLastCalledWith("active"));
  });

  it("says the surface is for administrators when the route hides itself", async () => {
    api.fetchIdentities.mockRejectedValue({ status: 404, detail: "Not found" });
    render(<AdminDialog onClose={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Identity administration is available to administrators only.");
  });
});
```

Create `src/components/admin/RolesEditor.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RolesEditor } from "./RolesEditor";
import * as workflow from "@/api/workflow";
import type { RoleView } from "@/types/workflow";

vi.mock("@/api/workflow", () => ({
  fetchRoles: vi.fn(),
  grantRole: vi.fn(),
  revokeRole: vi.fn(),
}));

const api = vi.mocked(workflow);

const APPROVER: RoleView = {
  role_id: "role-1",
  identity_id: "id-alice",
  role: "approver",
  scope: null,
  expires_at: null,
  note: null,
  granted_by_identity_id: "id-root",
  granted_at: "2026-09-10T00:00:00Z",
  revoked_at: null,
};

describe("RolesEditor (Task I9)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchRoles.mockResolvedValue({ roles: [APPROVER], limit: 200, offset: 0 });
  });

  it("lists the identity's live roles and revokes one", async () => {
    api.revokeRole.mockResolvedValue(Object.assign({}, APPROVER, { revoked_at: "2026-09-14T00:00:00Z" }));
    render(<RolesEditor identityId="id-alice" />);
    await userEvent.click(await screen.findByRole("button", { name: "Revoke approver" }));
    await waitFor(() => expect(api.revokeRole).toHaveBeenCalledWith("role-1", null));
    await waitFor(() => expect(api.fetchRoles).toHaveBeenCalledTimes(2));
    expect(api.fetchRoles).toHaveBeenCalledWith("id-alice");
  });

  it("grants a chosen role with its note", async () => {
    api.grantRole.mockResolvedValue(Object.assign({}, APPROVER, { role_id: "role-2", role: "reviewer" }));
    render(<RolesEditor identityId="id-alice" />);
    await screen.findByRole("button", { name: "Revoke approver" });
    await userEvent.selectOptions(screen.getByLabelText("Role"), "reviewer");
    await userEvent.type(screen.getByLabelText("Grant note"), "covering leave");
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
    await waitFor(() =>
      expect(api.grantRole).toHaveBeenCalledWith({ identity_id: "id-alice", role: "reviewer", note: "covering leave" }),
    );
  });

  it("shows the authority's refusal detail", async () => {
    api.grantRole.mockRejectedValue({
      status: 409,
      error_type: "admin_workload_conflict",
      detail: "admin cannot be combined with a workload role",
    });
    render(<RolesEditor identityId="id-alice" />);
    await screen.findByRole("button", { name: "Revoke approver" });
    await userEvent.selectOptions(screen.getByLabelText("Role"), "admin");
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("admin cannot be combined with a workload role");
  });
});
```

Create `src/components/admin/RelationshipsEditor.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RelationshipsEditor } from "./RelationshipsEditor";
import * as workflow from "@/api/workflow";
import type { RelationshipView } from "@/types/workflow";

vi.mock("@/api/workflow", () => ({
  fetchRelationships: vi.fn(),
  assertApproverRelationship: vi.fn(),
  revokeRelationship: vi.fn(),
}));

const api = vi.mocked(workflow);

function edge(overrides: Partial<RelationshipView>): RelationshipView {
  return Object.assign(
    {
      relationship_id: "edge-1",
      from_identity_id: "id-carol",
      to_identity_id: "id-alice",
      relationship_type: "approver",
      asserted_by_identity_id: "id-root",
      asserted_at: "2026-09-10T00:00:00Z",
      effective_from: null,
      effective_until: null,
      note: null,
      revoked_at: null,
      revoked_by_identity_id: null,
    },
    overrides,
  ) as RelationshipView;
}

describe("RelationshipsEditor (Task I9)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchRelationships.mockResolvedValue({
      relationships: [edge({}), edge({ relationship_id: "edge-2", from_identity_id: "id-alice", to_identity_id: "id-dave" })],
      limit: 200,
      offset: 0,
    });
  });

  it("shows both directions of the org chart around the identity and revokes an edge", async () => {
    api.revokeRelationship.mockResolvedValue(edge({ revoked_at: "2026-09-14T00:00:00Z" }));
    render(<RelationshipsEditor identityId="id-alice" />);
    expect(await screen.findByText("Overseen by id-carol")).toBeInTheDocument();
    expect(screen.getByText("Oversees id-dave")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Revoke edge from id-carol" }));
    await waitFor(() => expect(api.revokeRelationship).toHaveBeenCalledWith("edge-1", null));
  });

  it("asserts an approver edge with the named overseer", async () => {
    api.assertApproverRelationship.mockResolvedValue(edge({ relationship_id: "edge-3", from_identity_id: "id-bob" }));
    render(<RelationshipsEditor identityId="id-alice" />);
    await screen.findByText("Overseen by id-carol");
    const add = screen.getByRole("button", { name: "Add overseer" });
    expect(add).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Overseer identity id"), "id-bob");
    await userEvent.click(add);
    await waitFor(() =>
      expect(api.assertApproverRelationship).toHaveBeenCalledWith({
        from_identity_id: "id-bob",
        to_identity_id: "id-alice",
        note: null,
      }),
    );
  });

  it("shows a cycle refusal from the authority", async () => {
    api.assertApproverRelationship.mockRejectedValue({
      status: 409,
      error_type: "relationship_cycle",
      detail: "the edge would create a cycle",
    });
    render(<RelationshipsEditor identityId="id-alice" />);
    await screen.findByText("Overseen by id-carol");
    await userEvent.type(screen.getByLabelText("Overseer identity id"), "id-dave");
    await userEvent.click(screen.getByRole("button", { name: "Add overseer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("the edge would create a cycle");
  });
});
```

Create `src/components/admin/QuotaEditor.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QuotaEditor } from "./QuotaEditor";
import * as workflow from "@/api/workflow";
import type { IdentityQuota } from "@/types/workflow";

vi.mock("@/api/workflow", () => ({
  fetchIdentityQuota: vi.fn(),
  setIdentityQuota: vi.fn(),
}));

const api = vi.mocked(workflow);

const QUOTA: IdentityQuota = {
  identity_id: "id-alice",
  tokens_per_day: 500,
  storage_bytes: 2048,
  container_tokens_per_day: 9000,
  container_storage_bytes: null,
  tokens_used_today: 160,
  storage_bytes_used: 1024,
};

describe("QuotaEditor (Task I9)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchIdentityQuota.mockResolvedValue(QUOTA);
  });

  it("shows both caps with usage and the container ceiling", async () => {
    render(<QuotaEditor identityId="id-alice" />);
    expect(await screen.findByText("Tokens per day: 500 (160 used today; container ceiling 9000)")).toBeInTheDocument();
    expect(screen.getByText("Storage: 2.0 KB (1.0 KB used; no container ceiling)")).toBeInTheDocument();
  });

  it("sets the token cap alone", async () => {
    api.setIdentityQuota.mockResolvedValue(Object.assign({}, QUOTA, { tokens_per_day: 42 }));
    render(<QuotaEditor identityId="id-alice" />);
    await screen.findByText(/Tokens per day: 500/);
    await userEvent.type(screen.getByLabelText("New cap"), "42");
    await userEvent.click(screen.getByRole("button", { name: "Save cap" }));
    await waitFor(() => expect(api.setIdentityQuota).toHaveBeenCalledWith("id-alice", "tokens", 42));
    expect(await screen.findByText(/Tokens per day: 42/)).toBeInTheDocument();
  });

  it("sets the storage cap alone", async () => {
    api.setIdentityQuota.mockResolvedValue(Object.assign({}, QUOTA, { storage_bytes: 3000 }));
    render(<QuotaEditor identityId="id-alice" />);
    await screen.findByText(/Tokens per day: 500/);
    await userEvent.selectOptions(screen.getByLabelText("Dimension"), "storage");
    await userEvent.type(screen.getByLabelText("New cap"), "3000");
    await userEvent.click(screen.getByRole("button", { name: "Save cap" }));
    await waitFor(() => expect(api.setIdentityQuota).toHaveBeenCalledWith("id-alice", "storage", 3000));
  });

  it("keeps Save disabled for a value the column cannot hold", async () => {
    render(<QuotaEditor identityId="id-alice" />);
    await screen.findByText(/Tokens per day: 500/);
    const save = screen.getByRole("button", { name: "Save cap" });
    await userEvent.type(screen.getByLabelText("New cap"), "0");
    expect(save).toBeDisabled();
    await userEvent.clear(screen.getByLabelText("New cap"));
    await userEvent.type(screen.getByLabelText("New cap"), "2147483648");
    expect(save).toBeDisabled();
    await userEvent.clear(screen.getByLabelText("New cap"));
    await userEvent.type(screen.getByLabelText("New cap"), "12");
    expect(save).toBeEnabled();
  });

  it("shows the refusal when the other dimension has no default", async () => {
    api.setIdentityQuota.mockRejectedValue({
      status: 409,
      error_type: "quota_default_missing",
      detail: "identity id-alice has no quota policy and no container default for storage",
    });
    render(<QuotaEditor identityId="id-alice" />);
    await screen.findByText(/Tokens per day: 500/);
    await userEvent.type(screen.getByLabelText("New cap"), "42");
    await userEvent.click(screen.getByRole("button", { name: "Save cap" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no container default for storage");
  });
});
```

- [ ] **Step 32: Run the admin UI tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npx vitest run src/components/admin > /tmp/i9-lane-fe-admin-red.log 2>&1; echo exit=$?`
Expected: `exit=1`; the five files fail to load with `Failed to resolve import "./IdentitiesTable"`, `"./AdminDialog"`, `"./RolesEditor"`, `"./RelationshipsEditor"` and `"./QuotaEditor"`.

- [ ] **Step 33: Write the identity-administration components.**

Create `src/components/admin/IdentitiesTable.tsx`:

```tsx
import { useEffect, useState } from "react";

import { Button, Input } from "@/components/ui";
import * as workflow from "@/api/workflow";
import { workflowErrorMessage } from "@/stores/mailboxStore";
import type { IdentityQuota, IdentityView } from "@/types/workflow";
import { formatBytes } from "@/utils/bytes";

interface IdentitiesTableProps {
  identities: IdentityView[];
  onSelect: (identityId: string) => void;
  onChanged: () => void;
}

type PendingAction = { identityId: string; kind: "disable" | "enable" };

/** The row label: the username, or the subject for a never-admitted pending row (its username is blanked server-side). */
function label(identity: IdentityView): string {
  return identity.username ?? identity.subject;
}

/**
 * Identities with disable/enable and both quotas with current usage (spec
 * :1196-1200). Never renders display_name or email: a never-admitted pending
 * row may show only its subject and organisation (spec :963-964), and the
 * admin's job here needs neither field for any row.
 */
export function IdentitiesTable({ identities, onSelect, onChanged }: IdentitiesTableProps): JSX.Element {
  const [quotas, setQuotas] = useState<Record<string, IdentityQuota>>({});
  const [action, setAction] = useState<PendingAction | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    for (const identity of identities) {
      if (identity.access_state !== "active") continue;
      workflow
        .fetchIdentityQuota(identity.identity_id)
        .then((quota) => {
          if (live) setQuotas((current) => Object.assign({}, current, { [identity.identity_id]: quota }));
        })
        .catch((failure: unknown) => {
          if (live) setError(workflowErrorMessage(failure));
        });
    }
    return () => {
      live = false;
    };
  }, [identities]);

  async function confirm(identity: IdentityView, pending: PendingAction): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      if (pending.kind === "disable") {
        await workflow.disableIdentity(identity.identity_id, text);
      } else {
        await workflow.enableIdentity(identity.identity_id, text);
      }
      setAction(null);
      setText("");
      onChanged();
    } catch (failure) {
      setError(workflowErrorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {error !== null && (
        <p role="alert" className="identity-admin-error">
          {error}
        </p>
      )}
      <table className="identity-admin-table">
        <thead>
          <tr>
            <th scope="col">Identity</th>
            <th scope="col">State</th>
            <th scope="col">Quota</th>
            <th scope="col">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {identities.map((identity) => {
            const name = label(identity);
            const quota = quotas[identity.identity_id];
            const pending = action !== null && action.identityId === identity.identity_id ? action : null;
            return (
              <tr key={identity.identity_id}>
                <td>
                  <span className="identity-admin-name">{name}</span>
                  {identity.organisation_id !== null && <span className="identity-admin-meta">{identity.organisation_id}</span>}
                </td>
                <td>{identity.access_state}</td>
                <td>
                  {identity.access_state !== "active" ? (
                    <span className="identity-admin-meta">No quota while {identity.access_state}</span>
                  ) : quota === undefined ? (
                    <span className="identity-admin-meta">Loading quota</span>
                  ) : (
                    <>
                      <span className="identity-admin-meta">
                        Tokens today: {quota.tokens_used_today ?? "unknown"} of {quota.tokens_per_day ?? "no cap"}
                      </span>
                      <span className="identity-admin-meta">
                        Storage: {formatBytes(quota.storage_bytes_used)} of{" "}
                        {quota.storage_bytes === null ? "no cap" : formatBytes(quota.storage_bytes)}
                      </span>
                    </>
                  )}
                </td>
                <td className="identity-admin-actions">
                  {identity.access_state === "active" && (
                    <>
                      <Button compact aria-label={`Manage ${name}`} onClick={() => onSelect(identity.identity_id)}>
                        Manage
                      </Button>
                      <Button compact aria-label={`Disable ${name}`} onClick={() => setAction({ identityId: identity.identity_id, kind: "disable" })}>
                        Disable
                      </Button>
                    </>
                  )}
                  {identity.access_state === "disabled" && (
                    <Button compact aria-label={`Enable ${name}`} onClick={() => setAction({ identityId: identity.identity_id, kind: "enable" })}>
                      Enable
                    </Button>
                  )}
                  {pending !== null && (
                    <div className="identity-admin-inline-form">
                      <Input
                        type="text"
                        aria-label={pending.kind === "disable" ? `Reason for disabling ${name}` : `Note for enabling ${name}`}
                        value={text}
                        maxLength={512}
                        onChange={(event) => setText(event.target.value)}
                      />
                      <Button
                        compact
                        variant={pending.kind === "disable" ? "danger" : "primary"}
                        disabled={busy || text.trim() === ""}
                        onClick={() => void confirm(identity, pending)}
                      >
                        {pending.kind === "disable" ? "Confirm disable" : "Confirm enable"}
                      </Button>
                      <Button
                        compact
                        onClick={() => {
                          setAction(null);
                          setText("");
                        }}
                      >
                        Cancel
                      </Button>
                    </div>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}
```

Create `src/components/admin/RolesEditor.tsx`:

```tsx
import { useCallback, useEffect, useState } from "react";

import { Button, Input } from "@/components/ui";
import * as workflow from "@/api/workflow";
import { workflowErrorMessage } from "@/stores/mailboxStore";
import type { IdentityRole, RoleView } from "@/types/workflow";

const GRANTABLE_ROLES: readonly IdentityRole[] = ["approver", "reviewer", "curator", "auditor", "oversight", "user", "admin"];

interface RolesEditorProps {
  identityId: string;
}

/** Roles grant and revoke. R8 and the rest are the authority's rules; this shows its refusal text. */
export function RolesEditor({ identityId }: RolesEditorProps): JSX.Element {
  const [roles, setRoles] = useState<RoleView[] | null>(null);
  const [role, setRole] = useState<IdentityRole>("approver");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      const list = await workflow.fetchRoles(identityId);
      setRoles(list.roles);
    } catch (failure) {
      setError(workflowErrorMessage(failure));
    }
  }, [identityId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function run(mutation: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await mutation();
      await reload();
    } catch (failure) {
      setError(workflowErrorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="identity-admin-editor" aria-label="Roles">
      <h3 className="identity-admin-editor-title">Roles</h3>
      {error !== null && (
        <p role="alert" className="identity-admin-error">
          {error}
        </p>
      )}
      <ul className="identity-admin-list">
        {(roles ?? []).map((grant) => (
          <li key={grant.role_id} className="identity-admin-list-item">
            <span>{grant.scope === null ? grant.role : `${grant.role} (${grant.scope})`}</span>
            <Button compact disabled={busy} aria-label={`Revoke ${grant.role}`} onClick={() => void run(() => workflow.revokeRole(grant.role_id, null))}>
              Revoke
            </Button>
          </li>
        ))}
      </ul>
      <div className="identity-admin-form">
        <label className="field-label" htmlFor="roles-editor-role">
          Role
        </label>
        <select id="roles-editor-role" className="input" value={role} onChange={(event) => setRole(event.target.value as IdentityRole)}>
          {GRANTABLE_ROLES.map((candidate) => (
            <option key={candidate} value={candidate}>
              {candidate}
            </option>
          ))}
        </select>
        <label className="field-label" htmlFor="roles-editor-note">
          Grant note
        </label>
        <Input id="roles-editor-note" type="text" value={note} maxLength={512} onChange={(event) => setNote(event.target.value)} />
        <Button disabled={busy} onClick={() => void run(() => workflow.grantRole({ identity_id: identityId, role, note }))}>
          Grant role
        </Button>
      </div>
    </section>
  );
}
```

Create `src/components/admin/RelationshipsEditor.tsx`:

```tsx
import { useCallback, useEffect, useState } from "react";

import { Button, Input } from "@/components/ui";
import * as workflow from "@/api/workflow";
import { workflowErrorMessage } from "@/stores/mailboxStore";
import type { RelationshipView } from "@/types/workflow";

interface RelationshipsEditorProps {
  identityId: string;
}

/** The org chart around one identity (D11): who oversees them, whom they oversee. Cycles are the authority's refusal. */
export function RelationshipsEditor({ identityId }: RelationshipsEditorProps): JSX.Element {
  const [edges, setEdges] = useState<RelationshipView[] | null>(null);
  const [overseer, setOverseer] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      const list = await workflow.fetchRelationships(identityId);
      setEdges(list.relationships);
    } catch (failure) {
      setError(workflowErrorMessage(failure));
    }
  }, [identityId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function run(mutation: () => Promise<unknown>): Promise<boolean> {
    setBusy(true);
    setError(null);
    try {
      await mutation();
      await reload();
      return true;
    } catch (failure) {
      setError(workflowErrorMessage(failure));
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="identity-admin-editor" aria-label="Relationships">
      <h3 className="identity-admin-editor-title">Relationships</h3>
      {error !== null && (
        <p role="alert" className="identity-admin-error">
          {error}
        </p>
      )}
      <ul className="identity-admin-list">
        {(edges ?? []).map((edge) =>
          edge.to_identity_id === identityId ? (
            <li key={edge.relationship_id} className="identity-admin-list-item">
              <span>Overseen by {edge.from_identity_id}</span>
              <Button
                compact
                disabled={busy}
                aria-label={`Revoke edge from ${edge.from_identity_id}`}
                onClick={() => void run(() => workflow.revokeRelationship(edge.relationship_id, null))}
              >
                Revoke
              </Button>
            </li>
          ) : (
            <li key={edge.relationship_id} className="identity-admin-list-item">
              <span>Oversees {edge.to_identity_id}</span>
              <Button
                compact
                disabled={busy}
                aria-label={`Revoke edge to ${edge.to_identity_id}`}
                onClick={() => void run(() => workflow.revokeRelationship(edge.relationship_id, null))}
              >
                Revoke
              </Button>
            </li>
          ),
        )}
      </ul>
      <div className="identity-admin-form">
        <label className="field-label" htmlFor="relationships-editor-overseer">
          Overseer identity id
        </label>
        <Input id="relationships-editor-overseer" type="text" value={overseer} maxLength={64} onChange={(event) => setOverseer(event.target.value)} />
        <Button
          disabled={busy || overseer.trim() === ""}
          onClick={() =>
            void run(() =>
              workflow.assertApproverRelationship({ from_identity_id: overseer.trim(), to_identity_id: identityId, note: null }),
            ).then((succeeded) => {
              if (succeeded) setOverseer("");
            })
          }
        >
          Add overseer
        </Button>
      </div>
    </section>
  );
}
```

Create `src/components/admin/QuotaEditor.tsx`:

```tsx
import { useEffect, useState } from "react";

import { Button, Input } from "@/components/ui";
import * as workflow from "@/api/workflow";
import { workflowErrorMessage } from "@/stores/mailboxStore";
import type { IdentityQuota, QuotaDimension } from "@/types/workflow";
import { formatBytes } from "@/utils/bytes";

const MAX_QUOTA_VALUE = 2_147_483_647;

interface QuotaEditorProps {
  identityId: string;
  onSaved?: (quota: IdentityQuota) => void;
}

function parseCap(value: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return parsed >= 1 && parsed <= MAX_QUOTA_VALUE ? parsed : null;
}

/** One editor sets either dimension (spec :1200); the backend copies the other forward. */
export function QuotaEditor({ identityId, onSaved }: QuotaEditorProps): JSX.Element {
  const [quota, setQuota] = useState<IdentityQuota | null>(null);
  const [dimension, setDimension] = useState<QuotaDimension>("tokens");
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    workflow
      .fetchIdentityQuota(identityId)
      .then((result) => {
        if (live) setQuota(result);
      })
      .catch((failure: unknown) => {
        if (live) setError(workflowErrorMessage(failure));
      });
    return () => {
      live = false;
    };
  }, [identityId]);

  const cap = parseCap(value);

  async function save(): Promise<void> {
    if (cap === null) return;
    setBusy(true);
    setError(null);
    try {
      const result = await workflow.setIdentityQuota(identityId, dimension, cap);
      setQuota(result);
      setValue("");
      onSaved?.(result);
    } catch (failure) {
      setError(workflowErrorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="identity-admin-editor" aria-label="Quota">
      <h3 className="identity-admin-editor-title">Quota</h3>
      {error !== null && (
        <p role="alert" className="identity-admin-error">
          {error}
        </p>
      )}
      {quota !== null && (
        <>
          <p className="identity-admin-meta">
            Tokens per day: {quota.tokens_per_day ?? "no cap"} ({quota.tokens_used_today ?? "unknown"} used today;{" "}
            {quota.container_tokens_per_day === null ? "no container ceiling" : `container ceiling ${quota.container_tokens_per_day}`})
          </p>
          <p className="identity-admin-meta">
            Storage: {quota.storage_bytes === null ? "no cap" : formatBytes(quota.storage_bytes)} ({formatBytes(quota.storage_bytes_used)} used;{" "}
            {quota.container_storage_bytes === null ? "no container ceiling" : `container ceiling ${formatBytes(quota.container_storage_bytes)}`})
          </p>
        </>
      )}
      <div className="identity-admin-form">
        <label className="field-label" htmlFor="quota-editor-dimension">
          Dimension
        </label>
        <select
          id="quota-editor-dimension"
          className="input"
          value={dimension}
          onChange={(event) => setDimension(event.target.value as QuotaDimension)}
        >
          <option value="tokens">Tokens per day</option>
          <option value="storage">Storage bytes</option>
        </select>
        <label className="field-label" htmlFor="quota-editor-value">
          New cap
        </label>
        <Input id="quota-editor-value" type="text" inputMode="numeric" value={value} onChange={(event) => setValue(event.target.value)} />
        <Button variant="primary" disabled={busy || cap === null} onClick={() => void save()}>
          Save cap
        </Button>
      </div>
    </section>
  );
}
```

Create `src/components/admin/AdminDialog.tsx`:

```tsx
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui";
import * as workflow from "@/api/workflow";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { workflowErrorMessage } from "@/stores/mailboxStore";
import type { ApiError } from "@/types/index";
import type { IdentityAccessState, IdentityList } from "@/types/workflow";
import { IdentitiesTable } from "./IdentitiesTable";
import { QuotaEditor } from "./QuotaEditor";
import { RelationshipsEditor } from "./RelationshipsEditor";
import { RolesEditor } from "./RolesEditor";

interface AdminDialogProps {
  onClose: () => void;
}

/**
 * Minimal admin UI (spec :1196-1200): identities with disable/enable, roles,
 * relationships and the quota editor, all behind the admin role that every
 * /api/auth/admin and /api/workflow/quota/identities route re-proves. The
 * pending queue is the default filter, matching the route.
 */
export function AdminDialog({ onClose }: AdminDialogProps): JSX.Element {
  const modalRef = useRef<HTMLDivElement>(null);
  useFocusTrap(modalRef, true);
  const [accessState, setAccessState] = useState<IdentityAccessState>("pending");
  const [list, setList] = useState<IdentityList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setList(await workflow.fetchIdentities(accessState));
      setError(null);
    } catch (failure) {
      setError(
        (failure as Partial<ApiError>).status === 404
          ? "Identity administration is available to administrators only."
          : workflowErrorMessage(failure),
      );
    }
  }, [accessState]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <>
      <div role="presentation" onClick={onClose} className="app-dialog-backdrop" />
      <div
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="identity-admin-title"
        className="app-dialog settings-dialog settings-dialog-wide"
      >
        <div className="secrets-panel-header">
          <h2 id="identity-admin-title" className="secrets-panel-title">
            Identity administration
          </h2>
          <Button variant="bare" onClick={onClose} aria-label="Close identity administration" className="dialog-close">
            ×
          </Button>
        </div>
        <div className="secrets-panel-body">
          {error !== null && (
            <p role="alert" className="identity-admin-error">
              {error}
            </p>
          )}
          {list !== null && list.active_human_admin_count === 1 && (
            <p role="note" className="identity-admin-advisory">
              Only one active human administrator holds this container. Grant the admin role to a second person
              before this one is disabled or leaves.
            </p>
          )}
          <div className="identity-admin-toolbar">
            <label className="field-label" htmlFor="identity-admin-filter">
              Show
            </label>
            <select
              id="identity-admin-filter"
              className="input"
              value={accessState}
              onChange={(event) => {
                setAccessState(event.target.value as IdentityAccessState);
                setSelectedId(null);
              }}
            >
              <option value="pending">Pending</option>
              <option value="active">Active</option>
              <option value="disabled">Disabled</option>
            </select>
          </div>
          {list !== null &&
            (list.identities.length === 0 ? (
              <p className="mailbox-empty">No identities in this state.</p>
            ) : (
              <IdentitiesTable identities={list.identities} onSelect={setSelectedId} onChanged={() => void reload()} />
            ))}
          {selectedId !== null && (
            <div className="identity-admin-editors">
              <RolesEditor identityId={selectedId} />
              <RelationshipsEditor identityId={selectedId} />
              <QuotaEditor identityId={selectedId} />
            </div>
          )}
        </div>
      </div>
    </>
  );
}
```

Append to `src/components/workflow/workflow.css`:

```css
/* ── Identity administration ──────────────────────────────────────────── */

.identity-admin-advisory {
  margin: 0 0 var(--space-md);
  padding: var(--space-sm) var(--space-md);
  border: 1px solid var(--color-warning-border);
  border-radius: var(--radius-md);
  background: var(--color-warning-bg);
  color: var(--color-text);
  font-size: var(--font-size-sm);
}

.identity-admin-error {
  margin: 0 0 var(--space-sm);
  padding: var(--space-sm);
  border: 1px solid var(--color-error-border);
  border-radius: var(--radius-md);
  background: var(--color-error-bg);
  color: var(--color-error);
  font-size: var(--font-size-sm);
}

.identity-admin-toolbar {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  margin-bottom: var(--space-md);
}

.identity-admin-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-size-sm);
}

.identity-admin-table th,
.identity-admin-table td {
  padding: var(--space-xs) var(--space-sm);
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text);
  text-align: left;
  vertical-align: top;
}

.identity-admin-name {
  display: block;
  color: var(--color-text);
}

.identity-admin-meta {
  display: block;
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-xs);
}

.identity-admin-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--space-xs);
}

.identity-admin-inline-form {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-xs);
  width: 100%;
}

.identity-admin-editors {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
  gap: var(--space-md);
  margin-top: var(--space-md);
}

.identity-admin-editor {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  padding: var(--space-sm);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.identity-admin-editor-title {
  margin: 0;
  color: var(--color-text);
  font-size: var(--font-size-md);
}

.identity-admin-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  margin: 0;
  padding: 0;
  list-style: none;
}

.identity-admin-list-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-sm);
  color: var(--color-text);
  font-size: var(--font-size-sm);
}

.identity-admin-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
}
```

- [ ] **Step 34: Run the admin UI tests and the census gates to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npx vitest run src/components/admin src/styles/classNames.test.ts src/styles/tokenReferences.test.ts src/components/ui/primitiveCensus.test.ts > /tmp/i9-lane-fe-admin-green.log 2>&1; echo exit=$?`
Expected: `exit=0`; `IdentitiesTable` `6`, `AdminDialog` `4`, `RolesEditor` `3`, `RelationshipsEditor` `3`, `QuotaEditor` `5` passes.

- [ ] **Step 35: Write the failing library, identity-storage-total and upload-quota tests.**

Create `src/components/library/LibraryDialog.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LibraryDialog } from "./LibraryDialog";
import * as workflow from "@/api/workflow";
import { useSessionStore } from "@/stores/sessionStore";
import type { LibraryEntryView } from "@/types/workflow";

vi.mock("@/api/workflow", () => ({
  fetchLibrary: vi.fn(),
  forkLibraryEntry: vi.fn(),
}));

const api = vi.mocked(workflow);

function entry(overrides: Partial<LibraryEntryView> = {}): LibraryEntryView {
  return Object.assign(
    {
      entry_id: "entry-1",
      published_from_session_id: null,
      payload_digest: "d".repeat(64),
      compartment_id: "test-compartment",
      title: "Invoice triage",
      version: 2,
      published_by_identity_id: "alice",
      curated_by_identity_id: "carol",
      published_at: "2026-09-10T00:00:00Z",
      accepted_at: "2026-09-11T00:00:00Z",
      rejected_at: null,
      rejection_note: null,
      deprecated_at: null,
      recalled_at: null,
      note: null,
      state: "accepted",
    },
    overrides,
  ) as LibraryEntryView;
}

describe("LibraryDialog (Task I9)", () => {
  const loadSessions = vi.fn().mockResolvedValue(undefined);
  const selectSession = vi.fn().mockResolvedValue(undefined);

  beforeEach(() => {
    vi.clearAllMocks();
    useSessionStore.setState({ loadSessions, selectSession } as never);
  });

  it("lists accepted entries with their version and compartment, and marks a deprecated one", async () => {
    api.fetchLibrary.mockResolvedValue({
      view: "accepted",
      entries: [entry(), entry({ entry_id: "entry-2", title: "Old triage", state: "deprecated", deprecated_at: "2026-09-12T00:00:00Z" })],
    });
    render(<LibraryDialog onClose={vi.fn()} />);
    expect(await screen.findByText("Invoice triage")).toBeInTheDocument();
    expect(screen.getByText("Version 2 · test-compartment")).toBeInTheDocument();
    expect(screen.getByText("Deprecated")).toBeInTheDocument();
    expect(api.fetchLibrary).toHaveBeenCalledWith("accepted");
  });

  it("forks an entry into a new session and opens it", async () => {
    api.fetchLibrary.mockResolvedValue({ view: "accepted", entries: [entry()] });
    api.forkLibraryEntry.mockResolvedValue({ session_id: "session-new", state_id: "state-new" });
    const onClose = vi.fn();
    render(<LibraryDialog onClose={onClose} />);
    await userEvent.click(await screen.findByRole("button", { name: "Fork Invoice triage" }));
    await waitFor(() => expect(api.forkLibraryEntry).toHaveBeenCalledWith("entry-1"));
    await waitFor(() => expect(selectSession).toHaveBeenCalledWith("session-new"));
    expect(loadSessions).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("renders governance off as a plain statement, not an error", async () => {
    api.fetchLibrary.mockRejectedValue({ status: 409, error_type: "workflow_governance_off", detail: "off" });
    render(<LibraryDialog onClose={vi.fn()} />);
    expect(await screen.findByText("The shared library is not enabled on this deployment.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows a not-forkable refusal with the entry's current state", async () => {
    api.fetchLibrary.mockResolvedValue({ view: "accepted", entries: [entry()] });
    api.forkLibraryEntry.mockRejectedValue({
      status: 409,
      error_type: "library_entry_not_forkable",
      detail: "library entry is recalled",
      current_state: "recalled",
    });
    render(<LibraryDialog onClose={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Fork Invoice triage" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("library entry is recalled");
    expect(selectSession).not.toHaveBeenCalled();
  });
});
```

Create `src/components/blobs/IdentityStorageTotal.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { IdentityStorageTotal } from "./IdentityStorageTotal";
import * as workflow from "@/api/workflow";

vi.mock("@/api/workflow", () => ({ fetchMyQuota: vi.fn() }));

const api = vi.mocked(workflow);

const QUOTA = {
  identity_id: "alice",
  tokens_per_day: 500,
  storage_bytes: 4096,
  container_tokens_per_day: null,
  container_storage_bytes: 2048,
  tokens_used_today: 0,
  storage_bytes_used: 1024,
};

describe("IdentityStorageTotal (Task I9)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows the identity's total against the effective limit", async () => {
    api.fetchMyQuota.mockResolvedValue(QUOTA);
    render(<IdentityStorageTotal refreshKey={0} />);
    expect(await screen.findByText("Your files use 1.0 KB of 2.0 KB")).toBeInTheDocument();
  });

  it("shows the total alone when no storage limit applies", async () => {
    api.fetchMyQuota.mockResolvedValue(Object.assign({}, QUOTA, { storage_bytes: null, container_storage_bytes: null }));
    render(<IdentityStorageTotal refreshKey={0} />);
    expect(await screen.findByText("Your files use 1.0 KB")).toBeInTheDocument();
  });

  it("refetches when the blob list changes, and renders nothing when the read fails", async () => {
    api.fetchMyQuota.mockResolvedValueOnce(QUOTA).mockRejectedValueOnce({ status: 503, detail: "down" });
    const { rerender, container } = render(<IdentityStorageTotal refreshKey={1} />);
    await screen.findByText("Your files use 1.0 KB of 2.0 KB");
    rerender(<IdentityStorageTotal refreshKey={2} />);
    await waitFor(() => expect(api.fetchMyQuota).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
```

Create `src/stores/blobStore.quota.test.ts`:

```ts
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useBlobStore } from "./blobStore";

vi.mock("@/api/client", () => ({
  listBlobs: vi.fn(),
  uploadBlob: vi.fn(),
  deleteBlob: vi.fn(),
  downloadBlobContent: vi.fn(),
}));

describe("blobStore storage-quota refusal (Task I9, R13)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useBlobStore.setState({ activeSessionId: "session-1", blobs: [], isLoading: false, error: null });
  });

  it("names the usage and the effective limit on an identity storage 413", async () => {
    const { uploadBlob } = await import("@/api/client");
    const refusal = {
      status: 413,
      detail: "identity storage quota exceeded",
      error_type: "storage_quota_exceeded",
      storage_quota: { cap: 2048, ceiling: null, usage: 1536 },
    };
    (uploadBlob as ReturnType<typeof vi.fn>).mockRejectedValue(refusal);
    await expect(useBlobStore.getState().uploadBlob("session-1", new File(["x"], "big.csv"))).rejects.toEqual(refusal);
    expect(useBlobStore.getState().error).toBe(
      "Storage quota reached: 1.5 KB used of 2.0 KB. Delete files you no longer need, then try again.",
    );
  });

  it("keeps the per-file size copy for a 413 without the quota triple", async () => {
    const { uploadBlob } = await import("@/api/client");
    (uploadBlob as ReturnType<typeof vi.fn>).mockRejectedValue({ status: 413 });
    await expect(useBlobStore.getState().uploadBlob("session-1", new File(["x"], "big.csv"))).rejects.toEqual({ status: 413 });
    expect(useBlobStore.getState().error).toBe("File exceeds the maximum upload size.");
  });
});
```

- [ ] **Step 36: Run the library, storage-total and upload-quota tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npx vitest run src/components/library/LibraryDialog.test.tsx src/components/blobs/IdentityStorageTotal.test.tsx src/stores/blobStore.quota.test.ts > /tmp/i9-lane-fe-library-red.log 2>&1; echo exit=$?`
Expected: `exit=1`. The two component files fail to load with `Failed to resolve import "./LibraryDialog"` and `"./IdentityStorageTotal"`. In `blobStore.quota.test.ts` the first test fails with `expected 'File exceeds the maximum upload size.' to be 'Storage quota reached: 1.5 KB used of 2.0 KB. Delete files you no longer need, then try again.'`; the second passes.

- [ ] **Step 37: Write the library dialog, the identity storage total and the upload-quota copy.**

Create `src/components/library/LibraryDialog.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui";
import * as workflow from "@/api/workflow";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { workflowErrorMessage } from "@/stores/mailboxStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { ApiError } from "@/types/index";
import type { LibraryEntryView } from "@/types/workflow";

interface LibraryDialogProps {
  onClose: () => void;
}

/**
 * The shared library browser (Task I5 routes): accepted entries, deprecated
 * ones still visible and marked, and fork into a new session. The fork
 * re-imports the public projection server-side (I5 `seed_state_from_runtime_yaml`);
 * the client only opens the session it returns.
 */
export function LibraryDialog({ onClose }: LibraryDialogProps): JSX.Element {
  const modalRef = useRef<HTMLDivElement>(null);
  useFocusTrap(modalRef, true);
  const [entries, setEntries] = useState<LibraryEntryView[] | null>(null);
  const [governanceOff, setGovernanceOff] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    workflow
      .fetchLibrary("accepted")
      .then((list) => {
        if (live) setEntries(list.entries);
      })
      .catch((failure: unknown) => {
        if (!live) return;
        if ((failure as Partial<ApiError>).error_type === "workflow_governance_off") {
          setGovernanceOff(true);
        } else {
          setError(workflowErrorMessage(failure));
        }
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  async function fork(target: LibraryEntryView): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const forked = await workflow.forkLibraryEntry(target.entry_id);
      const sessions = useSessionStore.getState();
      await sessions.loadSessions();
      await sessions.selectSession(forked.session_id);
      onClose();
    } catch (failure) {
      setError(workflowErrorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div role="presentation" onClick={onClose} className="app-dialog-backdrop" />
      <div ref={modalRef} role="dialog" aria-modal="true" aria-labelledby="library-title" className="app-dialog settings-dialog settings-dialog-wide">
        <div className="secrets-panel-header">
          <h2 id="library-title" className="secrets-panel-title">
            Shared library
          </h2>
          <Button variant="bare" onClick={onClose} aria-label="Close shared library" className="dialog-close">
            ×
          </Button>
        </div>
        <div className="secrets-panel-body">
          {error !== null && (
            <p role="alert" className="mailbox-error">
              {error}
            </p>
          )}
          {governanceOff ? (
            <p className="mailbox-empty">The shared library is not enabled on this deployment.</p>
          ) : entries === null ? (
            error === null && <p className="mailbox-empty">Loading the library</p>
          ) : entries.length === 0 ? (
            <p className="mailbox-empty">No entries have been accepted yet.</p>
          ) : (
            <ul className="library-list">
              {entries.map((candidate) => (
                <li key={candidate.entry_id} className="library-entry">
                  <span className="library-entry-title">{candidate.title}</span>
                  <span className="library-entry-meta">
                    Version {candidate.version} · {candidate.compartment_id}
                  </span>
                  {candidate.state === "deprecated" && <span className="library-entry-deprecated">Deprecated</span>}
                  <Button compact disabled={busy} aria-label={`Fork ${candidate.title}`} onClick={() => void fork(candidate)}>
                    Fork
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </>
  );
}
```

Create `src/components/blobs/IdentityStorageTotal.tsx`:

```tsx
import { useEffect, useState } from "react";

import * as workflow from "@/api/workflow";
import type { IdentityQuota } from "@/types/workflow";
import { effectiveStorageLimit, formatBytes } from "@/utils/bytes";

interface IdentityStorageTotalProps {
  /** Changes whenever the blob list changes, so the total follows uploads and deletions. */
  refreshKey: number;
}

/**
 * The identity's storage total beside the blob list, so deletion is
 * discoverable (spec :1201-1203). A failed read renders nothing: the total is
 * a convenience, and the upload refusal itself carries the numbers.
 */
export function IdentityStorageTotal({ refreshKey }: IdentityStorageTotalProps): JSX.Element | null {
  const [quota, setQuota] = useState<IdentityQuota | null>(null);

  useEffect(() => {
    let live = true;
    workflow
      .fetchMyQuota()
      .then((result) => {
        if (live) setQuota(result);
      })
      .catch(() => {
        if (live) setQuota(null);
      });
    return () => {
      live = false;
    };
  }, [refreshKey]);

  if (quota === null) return null;
  const limit = effectiveStorageLimit(quota.storage_bytes, quota.container_storage_bytes);
  return (
    <span className="blob-manager-identity-total">
      {limit === null
        ? `Your files use ${formatBytes(quota.storage_bytes_used)}`
        : `Your files use ${formatBytes(quota.storage_bytes_used)} of ${formatBytes(limit)}`}
    </span>
  );
}
```

In `src/stores/blobStore.ts`:

1. Directly after `import * as api from "@/api/client";` (:4), add:

```ts
import type { ApiError } from "@/types/index";
import { storageQuotaMessage } from "@/utils/bytes";
```

2. In `uploadBlob`'s `catch (err)`, replace

```ts
      const detail =
        (err as { status?: number }).status === 413
          ? "File exceeds the maximum upload size."
```

with

```ts
      const quotaRefusal = (err as Partial<ApiError>).storage_quota;
      const detail =
        (err as { status?: number }).status === 413 && quotaRefusal !== undefined
          ? storageQuotaMessage(quotaRefusal)
          : (err as { status?: number }).status === 413
          ? "File exceeds the maximum upload size."
```

In `src/components/blobs/BlobManager.tsx`:

1. Directly after `import type { BlobMetadata, BlobCategory } from "@/types/api";` (:8), add:

```ts
import { IdentityStorageTotal } from "./IdentityStorageTotal";
```

2. Replace the header title span (:127-129):

```tsx
        <span className="blob-manager-title">
          Files ({blobs.length})
        </span>
```

with

```tsx
        <span className="blob-manager-title">
          Files ({blobs.length})
        </span>
        <IdentityStorageTotal refreshKey={blobs.length} />
```

In `src/components/blobs/BlobManager.test.tsx`, directly after `import type { BlobMetadata } from "@/types/api";` (:7), add:

```ts

// The identity storage total fetches /api/workflow/quota/me; these tests pin the session list only.
vi.mock("@/api/workflow", () => ({ fetchMyQuota: vi.fn().mockReturnValue(new Promise(() => {})) }));
```

Append to `src/components/workflow/workflow.css`:

```css
/* ── Shared library and storage total ─────────────────────────────────── */

.library-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  margin: 0;
  padding: 0;
  list-style: none;
}

.library-entry {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) 0;
  border-bottom: 1px solid var(--color-border);
}

.library-entry-title {
  flex: 1 1 auto;
  color: var(--color-text);
  font-size: var(--font-size-sm);
}

.library-entry-meta {
  color: var(--color-text-muted);
  font-size: var(--font-size-xs);
}

.library-entry-deprecated {
  padding: 0 var(--space-xs);
  border: 1px solid var(--color-warning-border);
  border-radius: var(--radius-sm);
  color: var(--color-warning);
  font-size: var(--font-size-xs);
}

.blob-manager-identity-total {
  color: var(--color-text-muted);
  font-size: var(--font-size-xs);
}
```

- [ ] **Step 38: Run the library, storage and blob suites and the census gates to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npx vitest run src/components/library/LibraryDialog.test.tsx src/components/blobs src/stores/blobStore.quota.test.ts src/stores/blobStore.test.ts src/styles/classNames.test.ts src/styles/tokenReferences.test.ts > /tmp/i9-lane-fe-library-green.log 2>&1; echo exit=$?`
Expected: `exit=0`; the three new files contribute `4`, `3` and `2` passes, and `BlobManager.test.tsx` and `blobStore.test.ts` keep their existing counts.

- [ ] **Step 39: Wire the badge, the account-menu entries, the three dialogs and the single poll into the app.**

Create `src/components/common/UserMenu.workflow.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { UserMenu } from "./UserMenu";
import { AppHeader } from "./AppHeader";
import { useAuthStore } from "@/stores/authStore";
import { useMailboxStore } from "@/stores/mailboxStore";

vi.mock("@/api/workflow", () => ({ fetchMailboxSummary: vi.fn() }));

describe("UserMenu and AppHeader workflow entries (Task I9)", () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.setState({ user: null });
    useMailboxStore.getState().reset();
  });

  it("offers Mailbox, Shared library and Identity administration only when handed their openers", async () => {
    const onOpenMailbox = vi.fn();
    const onOpenLibrary = vi.fn();
    const onOpenIdentityAdmin = vi.fn();
    const { unmount } = render(
      <UserMenu
        onOpenSettings={vi.fn()}
        onSignOut={vi.fn()}
        onOpenMailbox={onOpenMailbox}
        onOpenLibrary={onOpenLibrary}
        onOpenIdentityAdmin={onOpenIdentityAdmin}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    await userEvent.click(screen.getByRole("button", { name: "Mailbox" }));
    expect(onOpenMailbox).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    await userEvent.click(screen.getByRole("button", { name: "Shared library" }));
    expect(onOpenLibrary).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    await userEvent.click(screen.getByRole("button", { name: "Identity administration" }));
    expect(onOpenIdentityAdmin).toHaveBeenCalledTimes(1);
    unmount();
    // Mutation-derivation: without the openers the entries do not exist.
    render(<UserMenu onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    expect(screen.queryByRole("button", { name: "Mailbox" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Shared library" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Identity administration" })).toBeNull();
  });

  it("puts the unread badge in the header only when the header is handed the mailbox opener", async () => {
    useMailboxStore.setState({
      summary: { governance: "on", roles: [], approvals_to_decide: 2, reviews_to_attest: 0, decisions_unseen: 0 },
    });
    const onOpenMailbox = vi.fn();
    const { unmount } = render(<AppHeader onOpenSettings={vi.fn()} onSignOut={vi.fn()} onOpenMailbox={onOpenMailbox} />);
    await userEvent.click(screen.getByRole("button", { name: "Mailbox, 2 unread" }));
    expect(onOpenMailbox).toHaveBeenCalledTimes(1);
    unmount();
    render(<AppHeader onOpenSettings={vi.fn()} onSignOut={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /Mailbox,/ })).toBeNull();
  });
});
```

In `src/components/common/UserMenu.tsx`:

1. Replace the props interface's last member (:18) and closing brace:

```tsx
  onOpenUserManagement?: () => void;
}
```

with

```tsx
  onOpenUserManagement?: () => void;
  /** Task I9: present while workflow governance is on; absent, the item is not rendered. */
  onOpenMailbox?: () => void;
  /** Task I9: present while workflow governance is on. */
  onOpenLibrary?: () => void;
  /** Task I9: present only when the mailbox summary reports a live admin grant. */
  onOpenIdentityAdmin?: () => void;
}
```

2. Replace the destructured parameter (:70):

```tsx
  onOpenUserManagement,
}: UserMenuProps): JSX.Element {
```

with

```tsx
  onOpenUserManagement,
  onOpenMailbox,
  onOpenLibrary,
  onOpenIdentityAdmin,
}: UserMenuProps): JSX.Element {
```

3. Directly after the `onUserManagement` callback, which ends at :131 with `  }, [onOpenUserManagement]);`, add:

```tsx

  // Same focus-return-before-unmount move for the three workflow dialogs.
  const onMailbox = useCallback(() => {
    triggerRef.current?.focus();
    setOpen(false);
    onOpenMailbox?.();
  }, [onOpenMailbox]);

  const onLibrary = useCallback(() => {
    triggerRef.current?.focus();
    setOpen(false);
    onOpenLibrary?.();
  }, [onOpenLibrary]);

  const onIdentityAdmin = useCallback(() => {
    triggerRef.current?.focus();
    setOpen(false);
    onOpenIdentityAdmin?.();
  }, [onOpenIdentityAdmin]);
```

4. Directly above `          {onOpenUserManagement !== undefined && (` (:237), add:

```tsx
          {onOpenMailbox !== undefined && (
            <li className="user-menu-item">
              <Button variant="bare" onClick={onMailbox} className="user-menu-action">
                Mailbox
              </Button>
            </li>
          )}
          {onOpenLibrary !== undefined && (
            <li className="user-menu-item">
              <Button variant="bare" onClick={onLibrary} className="user-menu-action">
                Shared library
              </Button>
            </li>
          )}
          {onOpenIdentityAdmin !== undefined && (
            <li className="user-menu-item">
              <Button variant="bare" onClick={onIdentityAdmin} className="user-menu-action">
                Identity administration
              </Button>
            </li>
          )}
```

In `src/components/common/AppHeader.tsx`:

1. Directly after `import { WordMark } from "@/components/ui";`, add:

```tsx
import { MailboxBadge } from "@/components/workflow/MailboxBadge";
```

2. Replace the props member at :21 and the interface's closing brace:

```tsx
  onOpenUserManagement?: () => void;
}
```

with

```tsx
  onOpenUserManagement?: () => void;
  /** Task I9 openers, forwarded to the badge and the account menu. */
  onOpenMailbox?: () => void;
  onOpenLibrary?: () => void;
  onOpenIdentityAdmin?: () => void;
}
```

3. Replace the destructured parameter at :27:

```tsx
  onOpenUserManagement,
}: AppHeaderProps): JSX.Element {
```

with

```tsx
  onOpenUserManagement,
  onOpenMailbox,
  onOpenLibrary,
  onOpenIdentityAdmin,
}: AppHeaderProps): JSX.Element {
```

4. Replace the right region (:46-50):

```tsx
      <div className="app-header-right">
        <UserMenu
          onOpenSettings={onOpenSettings}
          onSignOut={onSignOut}
          onOpenUserManagement={onOpenUserManagement}
        />
```

with

```tsx
      <div className="app-header-right">
        {onOpenMailbox !== undefined && <MailboxBadge onOpen={onOpenMailbox} />}
        <UserMenu
          onOpenSettings={onOpenSettings}
          onSignOut={onSignOut}
          onOpenUserManagement={onOpenUserManagement}
          onOpenMailbox={onOpenMailbox}
          onOpenLibrary={onOpenLibrary}
          onOpenIdentityAdmin={onOpenIdentityAdmin}
        />
```

In `src/App.tsx`:

1. Directly after `import { UserAdminDialog } from "./components/settings/UserAdminDialog";` (:36), add:

```tsx
import { MailboxDialog } from "./components/workflow/MailboxDialog";
import { AdminDialog } from "./components/admin/AdminDialog";
import { LibraryDialog } from "./components/library/LibraryDialog";
import { useMailboxStore } from "./stores/mailboxStore";
```

2. Directly after `  const closeUserAdmin = useCallback(() => setShowUserAdmin(false), []);` (:125), add:

```tsx
  // Task I9: the workflow dialogs. Dialogs, not hash routes: useHashRouter
  // reads any `#/x` as a session id.
  const [showMailbox, setShowMailbox] = useState(false);
  const [showIdentityAdmin, setShowIdentityAdmin] = useState(false);
  const [showLibrary, setShowLibrary] = useState(false);
  const openMailbox = useCallback(() => setShowMailbox(true), []);
  const closeMailbox = useCallback(() => setShowMailbox(false), []);
  const openIdentityAdmin = useCallback(() => setShowIdentityAdmin(true), []);
  const closeIdentityAdmin = useCallback(() => setShowIdentityAdmin(false), []);
  const openLibrary = useCallback(() => setShowLibrary(true), []);
  const closeLibrary = useCallback(() => setShowLibrary(false), []);
  const mailboxSummary = useMailboxStore((s) => s.summary);
  const workflowGovernanceOn = mailboxSummary?.governance === "on";
  const holdsAdmin = mailboxSummary?.roles.includes("admin") === true;
```

3. Directly after `  useSessionLifecycle();` (:152), add:

```tsx

  // The ONE mailbox timer (spec :1232). startPolling is idempotent and returns its stop.
  useEffect(() => {
    if (!isAuthenticated || sharedToken !== null) return undefined;
    return useMailboxStore.getState().startPolling();
  }, [isAuthenticated, sharedToken]);
```

4. Replace the `<AppHeader` element (:742-748):

```tsx
        <AppHeader
          onOpenSettings={openComposerSettings}
          onSignOut={logout}
          onOpenUserManagement={
            authUser?.dev_admin === true ? openUserAdmin : undefined
          }
        />
```

with

```tsx
        <AppHeader
          onOpenSettings={openComposerSettings}
          onSignOut={logout}
          onOpenUserManagement={
            authUser?.dev_admin === true ? openUserAdmin : undefined
          }
          onOpenMailbox={workflowGovernanceOn ? openMailbox : undefined}
          onOpenLibrary={workflowGovernanceOn ? openLibrary : undefined}
          onOpenIdentityAdmin={holdsAdmin ? openIdentityAdmin : undefined}
        />
```

5. Directly above `        {showUserAdmin && authUser !== null && (` (:841), add:

```tsx
        {showMailbox && <MailboxDialog onClose={closeMailbox} />}
        {showIdentityAdmin && <AdminDialog onClose={closeIdentityAdmin} />}
        {showLibrary && <LibraryDialog onClose={closeLibrary} />}
```

In `src/App.test.tsx`, directly after the `vi.mock("./api/shareableReviews", () => ({` block (:307-311, ending `}));`), add:

```tsx

// ── Workflow API stub (Task I9) ──────────────────────────────────────────────
// App starts the single mailbox poll when authenticated. The default summary is
// governance off, so no workflow entry renders unless a test overrides it.
vi.mock("./api/workflow", () => ({
  fetchMailboxSummary: vi.fn().mockResolvedValue({
    governance: "off",
    roles: [],
    approvals_to_decide: 0,
    reviews_to_attest: 0,
    decisions_unseen: 0,
  }),
  fetchMailboxInbox: vi.fn().mockResolvedValue({ approvals: [], reviews: [] }),
  fetchMailboxSent: vi.fn().mockResolvedValue({ approvals: [], reviews: [] }),
  fetchIdentities: vi.fn().mockResolvedValue({
    identities: [],
    access_state: "pending",
    limit: 200,
    offset: 0,
    active_human_admin_count: 2,
  }),
  fetchLibrary: vi.fn().mockResolvedValue({ view: "accepted", entries: [] }),
}));
```

and append at the end of `src/App.test.tsx`:

```tsx

describe("App workflow wiring (Task I9)", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    resetStore(useSessionStore);
    resetStore(usePreferencesStore);
    useExecutionStore.getState().reset();
    const { useMailboxStore } = await import("./stores/mailboxStore");
    useMailboxStore.getState().reset();
    useAuthStore.setState({
      token: "test-token",
      user: {
        user_id: "test-001",
        username: "test-operator",
        display_name: null,
        email: null,
        groups: [],
        dev_admin: false,
      } as never,
    } as never);
    localStorage.clear();
    window.history.replaceState(null, "", "/");
    vi.spyOn(api, "fetchSystemStatus").mockResolvedValue({
      composer_available: true,
      composer_model: "gpt-4o",
      composer_provider: "openai",
      composer_reason: null,
      composer_missing_keys: [],
    } satisfies SystemStatus);
    vi.spyOn(api, "fetchSessions").mockResolvedValue([]);
    vi.spyOn(api, "fetchRuns").mockResolvedValue([]);
  });

  it("starts the mailbox poll once and shows no workflow entry while governance is off", async () => {
    const workflow = await import("./api/workflow");
    render(<App />);
    await waitFor(() => expect(workflow.fetchMailboxSummary).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    expect(screen.queryByRole("button", { name: "Mailbox" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Identity administration" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Mailbox,/ })).toBeNull();
  });

  it("offers the mailbox, library and admin entries from the summary and opens the admin dialog", async () => {
    const workflow = await import("./api/workflow");
    vi.mocked(workflow.fetchMailboxSummary).mockResolvedValue({
      governance: "on",
      roles: ["admin"],
      approvals_to_decide: 0,
      reviews_to_attest: 0,
      decisions_unseen: 1,
    });
    render(<App />);
    expect(await screen.findByRole("button", { name: "Mailbox, 1 unread" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    expect(screen.getByRole("button", { name: "Shared library" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Identity administration" }));
    expect(await screen.findByRole("dialog", { name: "Identity administration" })).toBeInTheDocument();
    await waitFor(() => expect(workflow.fetchIdentities).toHaveBeenCalledWith("pending"));
  });
});
```

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npx vitest run src/components/common src/App.test.tsx > /tmp/i9-lane-fe-wiring.log 2>&1; echo exit=$?`
Expected: `exit=0`; `UserMenu.workflow.test.tsx` contributes `2 passed` and the new App describe `2 passed`; `AppHeader.test.tsx`, `UserMenu.test.tsx` and the existing App describes keep their counts. The existing App tests stay green because the mocked summary is governance off, so the header and menu render exactly as before.

- [ ] **Step 40: Run the whole frontend suite, the type check and both linters.**

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npm test > /tmp/i9-lane-fe-vitest.log 2>&1; echo exit=$?`
Expected: `exit=0`. This is the whole vitest run, so it includes the tree-wide gates `primitiveCensus.test.ts`, `classNames.test.ts`, `tokenReferences.test.ts`, `scrollOwners.test.ts` and `workspaceChrome.test.ts`. The last passes because I9 adds no member to `.completion-bar` (decision 12).

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npm run typecheck > /tmp/i9-lane-fe-typecheck.log 2>&1; echo exit=$?`
Expected: `exit=0`.

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npm run lint > /tmp/i9-lane-fe-eslint.log 2>&1; echo exit=$?`
Expected: `exit=0`.

Run: `cd "$(git rev-parse --show-toplevel)/src/elspeth/web/frontend" && npm run lint:css > /tmp/i9-lane-fe-stylelint.log 2>&1; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 41: Run the Python static gates, the scoped suites, and the trust-tier corpus diff.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff check src/elspeth/web/coordination/approval_authority.py src/elspeth/web/coordination/quota_authority.py src/elspeth/web/auth/audit.py src/elspeth/web/sessions/routes/workflow/mailbox.py src/elspeth/web/sessions/routes/workflow/quota.py src/elspeth/web/app.py tests/unit/architecture/test_session_db_mutation_authority.py tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/web/coordination/test_approval_mailbox.py tests/unit/web/coordination/test_quota_policy_authority.py tests/unit/web/workflow/test_mailbox_routes.py tests/unit/web/workflow/test_quota_routes.py tests/testcontainer/web/test_workflow_mailbox_postgres.py > /tmp/i9-lane-ruff.log 2>&1; echo exit=$?`
Expected: `exit=0`.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff format --check src/elspeth/web/coordination/approval_authority.py src/elspeth/web/coordination/quota_authority.py src/elspeth/web/auth/audit.py src/elspeth/web/sessions/routes/workflow/mailbox.py src/elspeth/web/sessions/routes/workflow/quota.py src/elspeth/web/app.py tests/unit/architecture/test_session_db_mutation_authority.py tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/web/coordination/test_approval_mailbox.py tests/unit/web/coordination/test_quota_policy_authority.py tests/unit/web/workflow/test_mailbox_routes.py tests/unit/web/workflow/test_quota_routes.py tests/testcontainer/web/test_workflow_mailbox_postgres.py > /tmp/i9-lane-format.log 2>&1; echo exit=$?`
Expected: `exit=0`. If it exits 1, run the same file list through `ruff format` (without `--check`), then re-run the check to `exit=0`.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && mypy src/elspeth/web/coordination/approval_authority.py src/elspeth/web/coordination/quota_authority.py src/elspeth/web/auth/audit.py src/elspeth/web/sessions/routes/workflow/mailbox.py src/elspeth/web/sessions/routes/workflow/quota.py src/elspeth/web/app.py > /tmp/i9-lane-mypy.log 2>&1; echo exit=$?`
Expected: `exit=0`.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination tests/unit/web/workflow tests/unit/web/auth tests/unit/web/test_app.py tests/unit/architecture/test_session_db_mutation_authority.py -n 0 > /tmp/i9-lane-python-scoped.log 2>&1; echo exit=$?`
Expected: `exit=0`.

The trust-tier gate exits 1 on every tree (the fail-closed signing state, AGENTS.md § Judge-signature stage), so compare its findings with the pre-I9 tree, never with zero. Build that tree from `HEAD` (I9's edits are still uncommitted) in a detached worktree:

Run: `cd "$(git rev-parse --show-toplevel)" && git worktree add --detach /tmp/i9-lane-base HEAD > /tmp/i9-lane-base-add.log 2>&1; echo exit=$?`
Expected: `exit=0`.

Run: `cd /tmp/i9-lane-base && PYTHONPATH=/tmp/i9-lane-base/src:/tmp/i9-lane-base/elspeth-lints/src ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing "$(git -C "$OLDPWD" rev-parse --show-toplevel)/.venv/bin/elspeth-lints" check --rules all --root src/elspeth > /tmp/i9-lane-lints-before.log 2>&1; echo exit=$?`
Expected: `exit=1`.

Run: `cd "$(git rev-parse --show-toplevel)" && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing .venv/bin/elspeth-lints check --rules all --root src/elspeth > /tmp/i9-lane-lints-after.log 2>&1; echo exit=$?`
Expected: `exit=1`.

Run: `cd "$(git rev-parse --show-toplevel)" && diff <(grep -E "approval_authority\.py|quota_authority\.py|auth/audit\.py|workflow/mailbox\.py|workflow/quota\.py|web/app\.py" /tmp/i9-lane-lints-before.log | sed -E 's/:[0-9]+(:[0-9]+)?/:LINE/g' | sort) <(grep -E "approval_authority\.py|quota_authority\.py|auth/audit\.py|workflow/mailbox\.py|workflow/quota\.py|web/app\.py" /tmp/i9-lane-lints-after.log | sed -E 's/:[0-9]+(:[0-9]+)?/:LINE/g' | sort) > /tmp/i9-lane-lints-diff.log 2>&1; echo exit=$?`
Expected: `exit=0` (no new finding in the six touched source files once line numbers are normalised). A `>` line in the diff is a new finding. Fix the code (trust-tier rules, AGENTS.md) and never add a suppression.

Control the instrument before trusting an empty diff. Add the line `_probe = getattr(object(), "x", None)` at the end of `src/elspeth/web/sessions/routes/workflow/quota.py`, re-run the "after" lint and the diff to `/tmp/i9-lane-lints-control.log`, and confirm the diff exits 1 with a `>` line naming `workflow/quota.py`. Delete the probe line and re-run the "after" lint and the diff to `exit=0`.

Remove the base worktree with the canonical script, dry run first:

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/worktree-cleanup.sh --path '/tmp/i9-lane-base' > /tmp/i9-lane-cleanup-dry.log 2>&1; echo exit=$?`
Expected: `exit=0`, and the log classifies `/tmp/i9-lane-base` as `REMOVABLE`.

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/worktree-cleanup.sh --execute --path '/tmp/i9-lane-base' > /tmp/i9-lane-cleanup.log 2>&1; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 42: Add the changelog line.**

In `CHANGELOG.md`, under `## 0.8.1 - 2026-09-10`, directly after I7's `- **Scoped workflow reads.**` bullet (it ends ``All three require `ELSPETH_WEB__WORKFLOW_GOVERNANCE=on`.``), add:

```markdown
- **Workflow mailbox, identity administration and quota status.** Every
  active identity has a mailbox. The Inbox holds the approvals and review
  requests awaiting them; Sent holds their own approval requests with the
  decision, the decider and the note, and their own review requests with each
  reviewer's verdict, note and time. One unread badge refreshes every 30
  seconds, and opening a decided approval request clears it; review outcomes
  are shown but never counted as unread. Administrators get an
  identities list with disable and enable, roles, the approver org chart and
  a quota editor that sets either daily tokens or storage bytes, recording a
  `quota_set` authentication event. The list warns when only one active human
  administrator remains. The blob list shows the identity's storage total,
  an identity storage refusal names the usage and the limit, and the Audit
  panel shows the approval state of the current version with the request
  actions. The mailbox, approval row and library require
  `ELSPETH_WEB__WORKFLOW_GOVERNANCE=on`.
```

- [ ] **Step 43: Run the full-suite gate, check branch safety, and commit by file pathspec.**

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/full-suite-gate.sh --execute --detach > /tmp/i9-lane-gate-launch.log 2>&1; echo exit=$?`
Expected: `exit=0`, and the log prints the run's `.done` path and log directory. Poll the `.done` path until it exists, then read that run's `summary.txt`. Expected: `frozen=YES`; the `ruff`, `mypy`, `contracts`, `pytest` and `testcontainer` stages `exit=0`. The `lints` stage exits 1, the fail-closed baseline Step 41 compared. If `contracts` fails on the soft-mapping census, run `python -m scripts.check_contracts --write-census`, add `config/cicd/soft-mapping-census.yaml` to the commit pathspec below, and re-run the gate.

Mark the 44 files this task created as intent-to-add, so the pathspec commit below can name them (an untracked path in `git commit -- <pathspec>` aborts with `error: pathspec ... did not match any file(s) known to git`):

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N -- src/elspeth/web/sessions/routes/workflow/mailbox.py src/elspeth/web/sessions/routes/workflow/quota.py tests/unit/web/coordination/test_approval_mailbox.py tests/unit/web/coordination/test_quota_policy_authority.py tests/unit/web/workflow/test_mailbox_routes.py tests/unit/web/workflow/test_quota_routes.py tests/testcontainer/web/test_workflow_mailbox_postgres.py src/elspeth/web/frontend/src/types/workflow.ts src/elspeth/web/frontend/src/api/workflow.ts src/elspeth/web/frontend/src/stores/mailboxStore.ts src/elspeth/web/frontend/src/utils/bytes.ts src/elspeth/web/frontend/src/components/workflow/MailboxBadge.tsx src/elspeth/web/frontend/src/components/workflow/MailboxDialog.tsx src/elspeth/web/frontend/src/components/workflow/InboxList.tsx src/elspeth/web/frontend/src/components/workflow/SentList.tsx src/elspeth/web/frontend/src/components/workflow/InspectPane.tsx src/elspeth/web/frontend/src/components/workflow/WorkflowRequestDialog.tsx src/elspeth/web/frontend/src/components/workflow/ApprovalReadinessRow.tsx src/elspeth/web/frontend/src/components/workflow/workflow.css src/elspeth/web/frontend/src/components/admin/AdminDialog.tsx src/elspeth/web/frontend/src/components/admin/IdentitiesTable.tsx src/elspeth/web/frontend/src/components/admin/RolesEditor.tsx src/elspeth/web/frontend/src/components/admin/RelationshipsEditor.tsx src/elspeth/web/frontend/src/components/admin/QuotaEditor.tsx src/elspeth/web/frontend/src/components/library/LibraryDialog.tsx src/elspeth/web/frontend/src/components/blobs/IdentityStorageTotal.tsx src/elspeth/web/frontend/src/api/client.workflow-errors.test.ts src/elspeth/web/frontend/src/api/workflow.test.ts src/elspeth/web/frontend/src/utils/bytes.test.ts src/elspeth/web/frontend/src/stores/mailboxStore.test.ts src/elspeth/web/frontend/src/stores/executionStore.approval.test.ts src/elspeth/web/frontend/src/stores/blobStore.quota.test.ts src/elspeth/web/frontend/src/components/workflow/MailboxBadge.test.tsx src/elspeth/web/frontend/src/components/workflow/MailboxDialog.test.tsx src/elspeth/web/frontend/src/components/workflow/WorkflowRequestDialog.test.tsx src/elspeth/web/frontend/src/components/workflow/ApprovalReadinessRow.test.tsx src/elspeth/web/frontend/src/components/admin/AdminDialog.test.tsx src/elspeth/web/frontend/src/components/admin/IdentitiesTable.test.tsx src/elspeth/web/frontend/src/components/admin/RolesEditor.test.tsx src/elspeth/web/frontend/src/components/admin/RelationshipsEditor.test.tsx src/elspeth/web/frontend/src/components/admin/QuotaEditor.test.tsx src/elspeth/web/frontend/src/components/library/LibraryDialog.test.tsx src/elspeth/web/frontend/src/components/blobs/IdentityStorageTotal.test.tsx src/elspeth/web/frontend/src/components/common/UserMenu.workflow.test.tsx > /tmp/i9-lane-add-n.log 2>&1; echo exit=$?
```

Expected: `exit=0`. A `fatal: pathspec ... did not match any files` line means a Create step did not write that file; go back to that step.

Run: `cd "$(git rev-parse --show-toplevel)" && git status --short > /tmp/i9-lane-status.log 2>&1; echo exit=$?`
Expected: `exit=0`. Every path this task changed appears, and nothing under `.claude/lanes/`, `dist/` or a scratch log is staged.

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/branch-safety-check.sh --intent commit > /tmp/i9-lane-branch-safety.log 2>&1; echo exit=$?`
Expected: `exit=0` and no `[FAIL]` line.

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && git commit -m "feat(identity): workflow mailbox, identity administration, library and quota status surfaces" -- src/elspeth/web/coordination/approval_authority.py src/elspeth/web/coordination/quota_authority.py src/elspeth/web/auth/audit.py src/elspeth/web/sessions/routes/workflow/mailbox.py src/elspeth/web/sessions/routes/workflow/quota.py src/elspeth/web/app.py tests/unit/architecture/test_session_db_mutation_authority.py tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/web/coordination/test_approval_mailbox.py tests/unit/web/coordination/test_quota_policy_authority.py tests/unit/web/workflow/test_mailbox_routes.py tests/unit/web/workflow/test_quota_routes.py tests/testcontainer/web/test_workflow_mailbox_postgres.py src/elspeth/web/frontend/src/types/workflow.ts src/elspeth/web/frontend/src/types/index.ts src/elspeth/web/frontend/src/api/workflow.ts src/elspeth/web/frontend/src/api/client.ts src/elspeth/web/frontend/src/stores/mailboxStore.ts src/elspeth/web/frontend/src/stores/executionStore.ts src/elspeth/web/frontend/src/stores/blobStore.ts src/elspeth/web/frontend/src/utils/bytes.ts src/elspeth/web/frontend/src/components/workflow/MailboxBadge.tsx src/elspeth/web/frontend/src/components/workflow/MailboxDialog.tsx src/elspeth/web/frontend/src/components/workflow/InboxList.tsx src/elspeth/web/frontend/src/components/workflow/SentList.tsx src/elspeth/web/frontend/src/components/workflow/InspectPane.tsx src/elspeth/web/frontend/src/components/workflow/WorkflowRequestDialog.tsx src/elspeth/web/frontend/src/components/workflow/ApprovalReadinessRow.tsx src/elspeth/web/frontend/src/components/workflow/workflow.css src/elspeth/web/frontend/src/components/admin/AdminDialog.tsx src/elspeth/web/frontend/src/components/admin/IdentitiesTable.tsx src/elspeth/web/frontend/src/components/admin/RolesEditor.tsx src/elspeth/web/frontend/src/components/admin/RelationshipsEditor.tsx src/elspeth/web/frontend/src/components/admin/QuotaEditor.tsx src/elspeth/web/frontend/src/components/library/LibraryDialog.tsx src/elspeth/web/frontend/src/components/blobs/IdentityStorageTotal.tsx src/elspeth/web/frontend/src/components/blobs/BlobManager.tsx src/elspeth/web/frontend/src/components/blobs/BlobManager.test.tsx src/elspeth/web/frontend/src/components/audit/AuditReadinessPanel.tsx src/elspeth/web/frontend/src/components/common/AppHeader.tsx src/elspeth/web/frontend/src/components/common/UserMenu.tsx src/elspeth/web/frontend/src/App.tsx src/elspeth/web/frontend/src/App.test.tsx src/elspeth/web/frontend/src/styles/index.css src/elspeth/web/frontend/src/api/client.workflow-errors.test.ts src/elspeth/web/frontend/src/api/workflow.test.ts src/elspeth/web/frontend/src/utils/bytes.test.ts src/elspeth/web/frontend/src/stores/mailboxStore.test.ts src/elspeth/web/frontend/src/stores/executionStore.approval.test.ts src/elspeth/web/frontend/src/stores/blobStore.quota.test.ts src/elspeth/web/frontend/src/components/workflow/MailboxBadge.test.tsx src/elspeth/web/frontend/src/components/workflow/MailboxDialog.test.tsx src/elspeth/web/frontend/src/components/workflow/WorkflowRequestDialog.test.tsx src/elspeth/web/frontend/src/components/workflow/ApprovalReadinessRow.test.tsx src/elspeth/web/frontend/src/components/admin/AdminDialog.test.tsx src/elspeth/web/frontend/src/components/admin/IdentitiesTable.test.tsx src/elspeth/web/frontend/src/components/admin/RolesEditor.test.tsx src/elspeth/web/frontend/src/components/admin/RelationshipsEditor.test.tsx src/elspeth/web/frontend/src/components/admin/QuotaEditor.test.tsx src/elspeth/web/frontend/src/components/library/LibraryDialog.test.tsx src/elspeth/web/frontend/src/components/blobs/IdentityStorageTotal.test.tsx src/elspeth/web/frontend/src/components/common/UserMenu.workflow.test.tsx CHANGELOG.md > /tmp/i9-lane-commit.log 2>&1; echo exit=$?
```

Expected: `exit=0`. Then run `cd "$(git rev-parse --show-toplevel)" && git show --stat HEAD > /tmp/i9-lane-commit-stat.log 2>&1; echo exit=$?` and confirm the stat lists exactly the 64 paths above, and no path another lane staged.
