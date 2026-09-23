# Identity / SSO programme — completion adjudication

Date: 2026-09-23. Tree: `release/0.8.1` at `64863ff15`, working tree as checked
out (modified: `AGENTS.md`, one spec; untracked docs only — no source changes).

**Question asked.** The maintainer believes the SSO / identity programme is
done. Is it?

**Answer in one line.** Substantially yes — the record is badly stale, not the
code. Nine of twelve deliverables are built, wired, enforced and tested through
production paths. Three specific things are missing, all of them UI/read-surface
placement rather than enforcement.

Every claim below carries the command that produced it.

## Read this before you read the word "enforced"

The whole governance stack is **opt-in and off by default**:

```
$ grep -n "workflow_governance" src/elspeth/web/config.py
602:    workflow_governance: Literal["off", "on"] = "off"
```

With it off, the R2 gate accepts a run with no approval input at all — that is a
named, tested behaviour, not an oversight
(`tests/unit/web/coordination/test_r2_execute_gate.py:201`
`test_governance_off_accepts_without_approval_input`, and `:208`
`test_governance_off_ignores_optional_gate_without_changing_decision`).

Turning it on has two prerequisites that readiness refuses without:

```
$ sed -n '315,327p' src/elspeth/web/readiness.py
315:def _workflow_governance_refusal(settings: WebSettings, provider: str) -> str | None:
321:            "workflow_governance=on is refused with auth_provider=local and registration_mode=open (R11): "
323:            "set registration_mode=closed or email_verified, or set workflow_governance=off"
326:        return "workflow_governance=on requires compartment_id: library rows and audit metadata carry the compartment marking"
```

So every "enforced" in this document means *enforced when
`workflow_governance=on`, `compartment_id` is set, and the deployment is not
local-plus-open-registration*. A deployment that upgrades to this code and
changes no settings gets none of it. That is the design (R11 exists precisely to
stop governance being switched on over an open front door), but it is the
difference between "the feature is built" and "the feature is protecting you",
and it should be stated when the programme is declared done.

---

## Why the README's status line is wrong

`docs/programmes/identity-workflow-governance/README.md:23` records a
**2026-09-13** state: "Phase 4 governance backend 1/9, Phase 5 frontend 0/3,
cutover pending. Approval, review, library, quotas, compartment marking and the
admin UI are NOT built."

That was true on 09-13. It was overtaken seven days later:

```
$ git show --stat --format="%h %ad %s" --date=short 44cf55a65 | tail -1
 218 files changed, 21344 insertions(+), 533 deletions(-)
$ git show --format="%h %ad %s" --date=short -s 44cf55a65
44cf55a65 2026-09-20 feat(identity): finalize governed workflow and cutover handoff
```

`44cf55a65` lands Phase 4, Phase 5 and I11 in one commit: `approval_authority.py`
(+704), `review_authority.py` (+522), `library_authority.py` (+549),
`quota_policy_authority.py` (+378), `workflow_scope_reader.py` (+186),
`quota_routes.py` (+205), the six `sessions/routes/workflow/` route modules, the
`compartments.py` module, 14 frontend components, and
`docs/runbooks/identity-workflow-cutover.md` (+271).

So the README is a measurement taken before the work landed, not a measurement
of the work. It should be corrected or dated explicitly.

---

## Verdicts per deliverable

| Deliverable | Verdict |
|---|---|
| Pluggable SSO | **DONE** |
| Identity substrate | **DONE** |
| Approval (send-for-approval, R2 run gate) | **DONE** |
| Review (attestation ledger) | **PARTIAL** — required rename not done |
| Shared library + personal lists | **DONE** |
| Per-person quotas (tokens/day + storage bytes) | **DONE** (closed `skipped` — misleading) |
| Compartment marking | **DONE** (one documented divergence) |
| Approver audit view | **PARTIAL** — backend only, no read surface |
| Mailbox (inbox, sent, badge, curator queue) | **DONE** |
| Admin UI | **DONE** via People & access (with orphaned components) |
| Composer completion bar (3 affordances) | **NOT BUILT as specified** |
| Identity cutover runbook + compatibility record | **DONE** (closed `skipped` — misleading) |
| Operator cutover execution | **NOT DONE — correctly**, operator-only |

---

### 1. Pluggable SSO — DONE

A profile registry, not a hardcoded provider list, with an import-time parity
assertion against a closed discriminator:

```
$ grep -n "PROFILE_REGISTRY\|AuthProviderType" src/elspeth/web/auth/providers/__init__.py
181:PROFILE_REGISTRY: Final[dict[AuthProviderType, IdPProfile]] = {profile.name: profile for profile in (_ENTRA, _GOOGLE, _OIDC, _VANGUARD)}
206:    declared = frozenset(get_args(AuthProviderType))
207:    registered = frozenset(PROFILE_REGISTRY) | {"local"}
208:    if registered != declared:
$ grep -n "AuthProviderType = " src/elspeth/contracts/auth.py
14:AuthProviderType = Literal["local", "oidc", "entra", "vanguard", "google"]
```

Registering a provider without a profile is a **boot failure**, not a test
failure (`providers/__init__.py:10-11`). The tracker row for this
(`elspeth-61b35227fa`) closed 2026-09-06 citing a landed commit; that claim
holds.

### 2. Identity substrate — DONE

`src/elspeth/web/coordination/identity_authority.py` owns identities, roles,
relationships and the one `quota_policies` row an activation writes. R7 (cycle),
R8 (admin/workload role conflict) and R9 (dormancy) all refuse inside the
transaction. Covered by the suite run in § Tests.

### 3. Approval and the R2 run gate — DONE

The gate is on the production execute path, and it compiles the binding from the
**frozen run settings**, not from a fixture:

```
$ grep -n "approval" src/elspeth/web/execution/service.py | sed -n '1,40p'
2185:            approval_inputs = await run_sync_in_worker(
2186:                self._approval_inputs_from_frozen,
2192:            reason = await self._session_service.check_approval_binding(
2199:                raise ExecutionApprovalRequired(reason=reason, binding=approval_inputs.binding)
```

The rev2.8 correction on the concurrent-decide guard — `WHERE decision IS NULL`,
**not** `decided_at IS NULL` — is what shipped:

```
$ grep -n "decision.is_(None)" src/elspeth/web/coordination/approval_authority.py
293:    .where(approvals_table.c.session_id == bindparam("session_id"), approvals_table.c.decision.is_(None))
432:            .where(approvals_table.c.approval_id == row.approval_id, approvals_table.c.decision.is_(None))
533:            .where(approvals_table.c.approval_id == approval_id, approvals_table.c.decision.is_(None))
589:            .where(approvals_table.c.approval_id == approval_id, approvals_table.c.decision.is_(None))
```

The losing half of that race is proven on real PostgreSQL, not SQLite:

```
$ pytest -m testcontainer -n 0 --collect-only -q tests/testcontainer/web/test_approval_authority_postgres.py
tests/testcontainer/web/test_approval_authority_postgres.py::test_two_eligible_approvers_race_to_decide_one_open_request
tests/testcontainer/web/test_approval_authority_postgres.py::test_approval_request_waits_for_session_without_blocking_token_settlement
tests/testcontainer/web/test_approval_authority_postgres.py::test_role_expiring_during_identity_lock_wait_refuses_decision
```

Role-based eligibility (rev2.2, not "manager edge to the author") and the
non-bearer-token approver read (D27) are both implemented — the read goes
through `workflow_scope_reader.py` and `routes/workflow/inspect.py`, which mint
no token.

### 4. Review — PARTIAL

The backend is complete: `review_authority.py` (522 lines), `review_requests` +
attestations, verdict-bound to an open request for the exact state, never a run
gate. Round-trip proven through HTTP (see § Tests).

**What is missing** is the one explicitly named UI obligation in the row:
*"rename existing 'Save for review' → 'Share inspect link'"*. It did not happen.

```
$ grep -rn "Save for review\|Share inspect link" src/elspeth/web/frontend/src/components/composer/CompletionBar.tsx
7: *   * Save for review  → POSTs mark-ready-for-review, opens dialog with the
27: * The "Save for review" button follows the backend-owned
102:        Save for review
```

And it is live in the served bundle (instrument control: the negative token
returns nothing on the same file, so a zero would have been a real zero):

```
$ grep -o 'index-[A-Za-z0-9_-]*\.js' src/elspeth/web/frontend/dist/index.html
index-C46mHGX7.js
$ grep -c "Save for review"   src/elspeth/web/frontend/dist/assets/index-C46mHGX7.js   → 2
$ grep -c "Share inspect link" src/elspeth/web/frontend/dist/assets/index-C46mHGX7.js  → 0
$ grep -c "ZZnotpresentZZ"     src/elspeth/web/frontend/dist/assets/index-C46mHGX7.js  → 0 (exit 1)
```

This matters beyond cosmetics: the row's reasoning was that the old bearer-token
"Save for review" surface is a *capability*, not an authorization, and keeping
the old label on it is exactly the confusion the rename was meant to end. Two
surfaces now both read as "review".

### 5. Shared library — DONE

`library_authority.py` (549 lines) with frozen content-addressed publication,
curator accept/reject/deprecate/recall, and curator-cannot-curate-own-publication
separation. Reachable: `App.tsx:898` renders `LibraryDialog`, opened from the
user menu (`UserMenu.tsx:258-259`, label "Shared library"). Publishing refuses
when `compartment_id` is unset (`library_authority.py:45`).

### 6. Per-person quotas — DONE (this row is closed `skipped`; that is wrong)

`elspeth-7c4b65bed3` was closed `skipped` by the 09-22 GitHub migration. The work
is **done**, and more completely than the row asked.

**Storage (R13) at all four byte-admitting sites.** Instrument controlled first
— the known-positive hits the definition, the nonsense token returns empty:

```
$ grep -rn "def admit_storage_bytes_on_connection" src/elspeth --include=*.py
src/elspeth/web/coordination/quota_authority.py:582:def admit_storage_bytes_on_connection(
$ grep -rn "admit_storage_bytes_on_connectionZZZ" src/elspeth --include=*.py   → exit 1, no output
```

Then every site, by its operation label:

```
$ grep -rn 'operation="blob_create"\|operation="inline_custody"\|operation="run_output_finalize"\|operation="blob_replacement"\|operation="session_fork"' src/elspeth --include=*.py
src/elspeth/web/blobs/service.py:1412:                operation="session_fork",
src/elspeth/web/blobs/service.py:2098:            operation="inline_custody",
src/elspeth/web/blobs/service.py:4049:                        operation="session_fork",
src/elspeth/web/coordination/repository.py:2309:            operation="blob_replacement",
src/elspeth/web/coordination/repository.py:2468:            operation="blob_replacement",
src/elspeth/web/coordination/repository.py:2656:                operation="run_output_finalize",
src/elspeth/web/coordination/repository.py:2777:            operation="blob_create",
src/elspeth/web/coordination/repository.py:3738:            operation="run_output_finalize",
```

All four required sites are present — upload (`blob_create`), inline custody,
run-output finalize, fork — plus a fifth the row did not name (`blob_replacement`).

The two subtleties the row spelled out are both honoured. Site 4 refuses
**before** the copy loop starts (`service.py:4008-4051`: the admission is inside
`_verify_plan_and_quota` at 4045; that function is defined at 4008 and awaited at
4055, and the copy loop begins at 4057), and the idempotent-replay path is exempt
with the reason in a comment:

```
src/elspeth/web/blobs/service.py:4036-4039
    # A plan that adds no new bytes deliberately skips the quota
    # check: an already-materialized child stays valid even if the
    # ceiling was lowered beneath its existing usage.
    if missing_bytes > 0:
```

**Tokens (R14)** fire at execute (via `run_start_permit_authority` /
`chargeable_admission_authority`) and at composer turn start — five call sites:

```
$ grep -n "_require_chargeable_admission" src/elspeth/web/composer/service.py
3926:    async def _require_chargeable_admission(
3963/4042/4286/4447/8790:        await self._require_chargeable_admission(session_operation_context)
```

**D31 policy row on every activation path.** The row names five paths; there are
**three** insert sites, because two pairs of paths share one entry point. All
three traced:

```
$ grep -n "quota_policies_table.insert()" src/elspeth/web/coordination/identity_authority.py
1718:                        conn.execute(quota_policies_table.insert().values(**quota))
2371:                conn.execute(quota_policies_table.insert().values(**quota))
2478:                conn.execute(quota_policies_table.insert().values(**quota))

$ for L in 1718 2371 2478; do awk -v L=$L 'NR<=L && /^    def /{fn=NR": "$0} NR==L{print fn}' \
    src/elspeth/web/coordination/identity_authority.py; done
1671:    def _ensure_identity_once(
2226:    def bootstrap_admin(
2390:    def pre_provision_identity(

$ for L in 1604 1633 1659; do awk -v L=$L 'NR<=L && /^    def /{fn=NR": "$0} NR==L{print fn}' \
    src/elspeth/web/coordination/identity_authority.py; done
1532:    def ensure_identity(     (all three internal callers are branches of one method)
```

Mapping to the five required paths:

| Required path | Insert site | Traced? |
|---|---|---|
| activate | `ensure_identity` → `_ensure_identity_once`:1718 | yes |
| local registration under open registration | same method, a branch of `ensure_identity` (callers 1604/1633/1659) | yes — one entry point, not separately traced per branch |
| pre-provision | `pre_provision_identity`:2478 | yes |
| D20 seed and CLI | `bootstrap_admin`:2371 | yes |
| D21 cutover re-admission | `bootstrap_admin`:2371, via the runbook's own command | yes — `docs/runbooks/identity-workflow-cutover.md:154` prescribes `users bootstrap-admin PROVIDER SUBJECT --username USERNAME` |

So five requirements are covered by three insert sites. What I did **not** do is
drive each of the three `ensure_identity` branches separately to confirm the
local-open-registration branch reaches 1718 — they share one method body, so the
insert is common to all three, but that is a read of the control flow rather than
an executed proof.

**Fail-closed when unrecorded.** A refusal with no audit writer raises rather
than passing silently (`quota_authority.py:554-556`,
`refuse_unrecorded_quota_exceeded`). Refusals carry dimension, cap, ceiling and
usage (asserted in `test_storage_quota_authority.py:85-97`).

Two post-landing repairs tightened it further: `9a8016c4d` *"enabling the quota
system requires both per-identity defaults"* and `7c04ecec5` *"check quota admin
expiry after locking grant"*.

### 7. Compartment marking — DONE, with one deliberate divergence

Four of the five named sites carry the marking directly:

| Site | Evidence |
|---|---|
| shareable snapshot | `shareable_reviews/service.py:316,483` |
| `library_entries` rows | `library_authority.py:107,213,350-360` |
| `auth_events.metadata_json` | `auth/audit.py:54-62` — stamped on **every** write, and a caller trying to override it raises `AuditIntegrityError` |
| signed Landscape export | `core/landscape/exporter.py:325,376,526`; `core/config.py:1723-1725` makes it *required* for `landscape-exporter-auth-v2` |

The fifth — the public YAML metadata block — is marked at the **download route**,
not inside `generate_public_yaml`, and the reason is written down:

```
src/elspeth/web/sessions/routes/composer/state.py:1352-1358
    # elspeth-06f92da0d9: this route is the one consumer that hands the user a
    # document to keep... The marker lives here rather than in
    # ``generate_public_yaml`` because the MCP, share, and acceptance-import
    # consumers of that function must keep bare bytes
    compartment_header = compartment_marking_header(request.app.state.settings.compartment_id)
```

This is a divergence from the row's literal text (*"composer/yaml_generator.py"*)
with a stated rationale, not an omission. Worth a maintainer glance, not a
defect: the other `generate_public_yaml` consumers
(`composer_mcp/server.py:606`, `workflow/inspect.py:80`, `library_authority.py:366`,
`shareable_reviews/service.py:279,629`, `_aws_ecs_acceptance/capture.py:248`)
emit bare bytes by design, and the library and share paths carry the marking in
their own row/metadata instead.

**Ingress** is recorded on every composition-state-creating path, not just chat:
`sessions/routes/messages.py`, `composer/compose.py`, `composer/state.py:977`
(YAML paste/import), `composer/proposals.py`, `composer/guided*.py` — all calling
`compartment_ingress_record` / `chat_ingress_input`, which record `sha256(text)`
and any foreign markings and **no content** (`compartments.py:37-52`).

### 8. Approver audit view — PARTIAL (backend only)

The route exists and is registered:

```
$ grep -n "audit-view" src/elspeth/web/sessions/routes/workflow/audit_view.py
155:    @router.get("/api/workflow/audit-view", response_model=WorkflowAuditViewResponse)
$ grep -n "create_workflow_audit_view_router" src/elspeth/web/app.py
1843:    app.include_router(create_workflow_audit_view_router())
```

There is no frontend consumer:

```
$ grep -rn "audit-view\|auditView\|workflow/audit" src/elspeth/web/frontend/src --include=*.ts --include=*.tsx
(no output, exit 1)
$ grep -n "workflow/" src/elspeth/web/frontend/src/api/workflow.ts
mailbox/summary, mailbox/inbox, mailbox/sent, mailbox/approvers, mailbox/{id}/seen,
approvals, reviews, inspect, quota/me, quota/{identity}   — audit-view absent
```

This is the one deliverable whose own stated rationale it contradicts. Row 10
was raised to P1 precisely because *"the audit event types are written before any
surface reads them; write-side completeness without a read side is how audit
trails stop being read."* The read side stops at the API boundary.

### 9. Mailbox — DONE

Inbox, sent folder, summary badge and the seen-marking round trip are all built
and reachable:

```
$ grep -n "MailboxDialog\|MailboxBadge" src/elspeth/web/frontend/src/App.tsx src/elspeth/web/frontend/src/components/common/AppHeader.tsx
App.tsx:897:        {showMailbox && <MailboxDialog onClose={closeMailbox} />}
AppHeader.tsx:51:        {onOpenMailbox !== undefined && <MailboxBadge onOpen={onOpenMailbox} />}
$ grep -n "mailbox" src/elspeth/web/frontend/src/api/workflow.ts
35: fetchMailboxSummary  → /api/workflow/mailbox/summary
36: fetchMailboxInbox    → /api/workflow/mailbox/inbox
37: fetchMailboxSent     → /api/workflow/mailbox/sent
38: fetchApproverDirectory → /api/workflow/mailbox/approvers
39: markApprovalSeen     → /api/workflow/mailbox/{id}/seen
```

The full round trip — request with a note, decide with a note, requester sees the
decision, badge clears — is exercised over HTTP at
`test_governance_round_trip.py:191-220`. The curator queue is served by
`LibraryDialog`'s `view === "queue"` arm (`LibraryDialog.tsx:205`).

### 10. Admin UI — DONE via People & access, with orphaned components

The live path is `PeopleAccessDialog` → `PersonDetail` → `QuotaEditor` /
`RolesEditor` / `RelationshipsEditor`. But the `AdminDialog` tree that landed in
`44cf55a65` is **dead code** — nothing outside its own tests references it:

```
$ for c in AdminDialog IdentitiesTable RolesEditor RelationshipsEditor QuotaEditor PersonDetail; do
    grep -rl "\b$c\b" src/elspeth/web/frontend/src --include=*.tsx --include=*.ts \
      | grep -v "components/admin/$c\." | grep -v "\.test\."; done
AdminDialog          → (none)
IdentitiesTable      → (none)
RolesEditor          → components/admin/PersonDetail.tsx
RelationshipsEditor  → components/admin/PersonDetail.tsx
QuotaEditor          → components/admin/PersonDetail.tsx
PersonDetail         → components/admin/PeopleAccessDialog.tsx
```

`AdminDialog.test.tsx` (169 lines) + `AdminDialog.quota.test.tsx` (44 lines) are
green against a component no user can reach. This is exactly the "a test exists"
trap: 213 lines of passing tests that prove nothing about the shipped product.
The People & access panel superseded it (`793d0f95a`, `c7c3bec8b`, `f00769a6f`)
and the old tree was not removed.

The admin function itself is delivered: identity/role/edge administration and
quota set/revoke are all reachable and were driven through an acceptance pass.

### 11. Composer completion bar — NOT BUILT as specified

Row 13 asked for *"Send for approval / Send for review / Publish to library as
three distinct affordances"* in the **composer completion bar**, with the
approval row in the readiness panel and the 409 legible before Run.

The approval row in the readiness panel is done
(`AuditReadinessPanel.tsx:445,645` render `ApprovalReadinessRow`). The three
completion-bar affordances are not. The completion bar carries only "Save for
review" (`CompletionBar.tsx:102`), and the three actions are scattered:

- Send for approval / Send for review → inside `ApprovalReadinessRow`, which
  renders only inside `AuditReadinessPanel`, which renders only inside
  `ChecksView.tsx:48` — i.e. the **Pipeline → Checks sub-tab**.
- Publish to library → in the user-menu `LibraryDialog`.

```
$ grep -rn "WorkflowRequestDialog" src/elspeth/web/frontend/src --include=*.tsx | grep -v "\.test\."
components/workflow/ApprovalReadinessRow.tsx:33:  {requestKind !== null && <WorkflowRequestDialog ... />}
$ grep -rn "AuditReadinessPanel" src/elspeth/web/frontend/src --include=*.tsx | grep -v "\.test\."
components/workspace/ChecksView.tsx:48:      <AuditReadinessPanel onSelectComponent={selectComponent} />
```

Also: none of the three labels exist in the served bundle
(`grep -c "Send for approval\|Send for review\|Publish to library"
dist/assets/index-C46mHGX7.js` → 0 each, against a control that finds "Save for
review" twice in the same file).

This should be checked against the standing placement ruling that a blocking
state and its fix affordance sit at the top level, never in a Checks sub-tab.
Approval blocks Run; its request affordance currently sits inside the Checks
sub-tab. I am flagging the conflict, not assuming the ruling still binds — that
is the maintainer's call.

### 12. Cutover runbook and compatibility record — DONE (this row is closed `skipped`; that is wrong)

`elspeth-5ef01c6ad1` was closed `skipped` by the migration. The deliverable
exists: `docs/runbooks/identity-workflow-cutover.md`, 271 lines, landed in
`44cf55a65`. It covers the stopped-service window, archive-and-export before
drop, the three CSVs (preserving `username` and `organisation_id` per the
09-20 ruling), re-admission, and a compatibility-record template
(lines 240-241). The mid-cutover session invalidation is stated (lines 145, 262).

**One stale numeral.** Line 9 says *"The current development base has Sessions
epoch 63 and Landscape epoch 43"*; live is:

```
$ grep -n "SESSION_SCHEMA_EPOCH = " src/elspeth/web/sessions/models.py
358:SESSION_SCHEMA_EPOCH = 65
$ grep -n "SQLITE_SCHEMA_EPOCH = " src/elspeth/core/landscape/schema.py
449:SQLITE_SCHEMA_EPOCH = 43
```

Sessions drifted 63 → 65. This is **not** a load-bearing wrong claim: the same
sentence instructs the operator to *"measure the final candidate values instead
of copying those numbers into the operator record"*, and the record template
takes measured values. The row's other pinned site is current:

```
$ grep -n "session_epoch\|rollback_permitted" docs/runbooks/aws-ecs-deployment.md | head -3
1621:    "candidate": {"session_epoch": 65, "landscape_epoch": 43, ...}
1630:  "rollback_permitted": false,
```

No test pins the runbook's numerals (`grep -rl "identity-workflow-cutover" tests/`
→ no output; only sibling runbooks reference it). Given the project's posture on
docs, that is acceptable; the numeral is worth a one-line correction.

### 13. Operator cutover execution — NOT DONE, correctly

Row 16 (`elspeth-b03f0aa218`) is operator-only work: recreate the stores, re-admit
the cohort, countersign the compatibility record, fire the judge bundle. An agent
must not do any of it ([O1] custody rule). Its being undone is not a gap in the
code deliverable.

---

## Tests: what I ran, and whether the tests mean anything

**Scoped Python suites** (log:
`/tmp/claude-1000/-home-john-elspeth/1fd47cd3-bb36-48f8-9166-755000d02856/scratchpad/identity-suite.log`):

```
pytest tests/unit/web/workflow/ tests/unit/web/auth/ \
  tests/unit/web/blobs/test_storage_quota_sites.py tests/unit/web/test_compartments.py \
  tests/unit/web/coordination/test_{approval,review,library,quota,quota_policy,storage_quota,r2_execute_gate,workflow_inspect,workflow_scope_reader,identity}_*.py \
  tests/unit/web/sessions/test_library_routes.py tests/unit/web/execution/test_export_marking.py \
  tests/integration/web/workflow/ tests/integration/web/test_library_workflow.py

1438 passed, 13 warnings in 65.62s
exit=0
```

**PostgreSQL testcontainer suites** (log: `…/identity-tc.log`; Docker confirmed
available, `docker info` exit 0):

```
pytest -m testcontainer -n 0 tests/testcontainer/web/test_{approval_authority,review_authority,library,quota_authority,quota_policy_authority,workflow_inspect}_postgres.py

23 passed, 1 warning in 35.09s
exit=0
```

**Frontend** (log: `…/identity-vitest.log`):

```
npx vitest run src/components/{workflow,admin,library} src/api/{workflow,workflow.quota,library,identityAdmin}.test.ts

Test Files  16 passed (16)
     Tests  99 passed (99)
exit=0
```

**What I did NOT run:** the full `pytest tests/` selection, the remaining
testcontainer files, ruff, mypy, `elspeth-lints`, and the whole frontend vitest
suite. This report does not certify the tree — it adjudicates a programme.

### Do the tests exercise production paths?

Mostly yes, and better than the usual bar.

**Production-path (route-driven).**
`tests/integration/web/workflow/test_governance_round_trip.py` (471 lines) drives
a real app over HTTP end to end:

```
line  97: POST /api/auth/login
line 103: POST /api/sessions
line 180: POST /api/sessions/{id}/execute      → asserts REFUSED (no approval)
line 191: GET  /api/workflow/mailbox/inbox
line 196: POST /api/approvals/{id}/decide      → by a non-eligible identity, refused
line 200: POST /api/approvals/{id}/decide      → by an eligible approver
line 222: POST /api/sessions/{id}/execute      → asserts ADMITTED
```

That is R2's fire case through the real compiled binding, not a fixture — the
binding on the execute side comes from `_approval_inputs_from_frozen`
(`execution/service.py:2185`), the same frozen inputs a run uses.

Row 11 also required that the suite run against a **closed** deployment, "or R11
means it is testing enforcement that was never on". The fixture satisfies it:

```
$ grep -rn "registration_mode\|workflow_governance\|compartment_id" tests/integration/web/workflow/
test_governance_round_trip.py:48:        registration_mode="closed",
test_governance_round_trip.py:49:        workflow_governance="on",
test_governance_round_trip.py:50:        compartment_id="team-red",
test_governance_round_trip.py:236:    assert all(metadata["compartment_id"] == "team-red" for _event_type, metadata in approval_events)
```

**Real boundary mutations.** The quota tests are boundary-parametrized, which is
why the inventory sometimes pins one test name for both fire and mutation — it is
one function carrying both sides, not a hollow pin:

```
tests/unit/web/blobs/test_storage_quota_sites.py:396  test_fork_preflight_refuses_before_copy_loop
  @parametrize(("cap", "refused"), [(150, True), (151, False)])
  line 410:  await blob_service.copy_blobs_for_fork(...)       ← real service call
  line 411:  assert checkpoints == 1                            ← refused before the loop
  line 412:  assert _rows(db_engine, target) == []              ← no half-populated child
  line 413:  assert [(row.operation, row.usage, row.cap) for row in recorded] == [("session_fork", 90, 100)]
```

**One test that is a claim, not evidence.**
`tests/unit/web/workflow/test_governance_suite_inventory.py` (400 lines) parses
test files with `ast` and asserts that named tests **exist and are not skipped**.
It is honest about this in its own docstring, and it carries its own
positive/negative control for its reader. It is a good anti-rot guard. It is
**not** evidence that any guard enforces anything, and it should never be cited
as such. Its inventory covers R2, R7, R8, R9, R11, R13, R14 with fire and
mutation pins each — and I spot-checked the pinned tests above rather than
trusting the inventory.

**The dead-code tests.** `AdminDialog.test.tsx` and `AdminDialog.quota.test.tsx`
(213 lines, green) test a component with zero non-test references. Passing tests,
zero product coverage.

---

## A signal worth reporting

Follow-up commits since the programme landed in `44cf55a65`:

```
$ for f in <path>; do echo "$(git log --oneline 44cf55a65..HEAD -- $f | wc -l)  $f"; done
1  src/elspeth/web/coordination/approval_authority.py
0  src/elspeth/web/coordination/review_authority.py
0  src/elspeth/web/coordination/library_authority.py
3  src/elspeth/web/coordination/quota_policy_authority.py
0  src/elspeth/web/sessions/routes/workflow/
6  src/elspeth/web/auth/people_routes.py
9  src/elspeth/web/frontend/src/components/admin/
```

The People & access surface was driven live, reviewed and repaired nine times.
The approval, review, library and mailbox surfaces have had **zero or one**
follow-up commit since they landed. Their tests are strong, including a real HTTP
round trip; but no repair pressure after landing usually means nobody has used
them against a browser yet. I report this as a signal, not a verdict — the three
gaps I did find (the rename, the audit-view read surface, the completion bar) are
all exactly the kind a live pass would surface first.

---

## The two rows closed as `skipped` — the answer

Both were closed `skipped` on 2026-09-22 by the GitHub migration with the reason
"Collapsed into elspeth-b3672e951e". Neither status reflects the work.

- **`elspeth-7c4b65bed3` (per-person quotas) — DONE, not skipped.** Both
  dimensions, all four byte-admitting sites (plus a fifth), fail-closed
  accounting, admin set/revoke route and editor, D31 policy rows on every activation entry point,
  refusals carrying dimension/cap/ceiling/usage, proven on SQLite and PostgreSQL.
- **`elspeth-5ef01c6ad1` (cutover runbook and compatibility record) — DONE, not
  skipped**, save for one stale epoch numeral on a line that already tells the
  operator to measure rather than copy. The separate operator *execution* of that
  cutover is correctly not done.

The migration reason is defensible — the detail did move to the programme folder —
but `skipped` is the wrong verb for finished work, and anyone reading the tracker
alone will conclude the opposite of the truth on both rows.

(The `filigree` MCP server failed to connect this session — `CONNECT_TIMEOUT` —
so I did not read or touch the tracker. Row states above are quoted from the
brief and from `tracker-rows.json` in the programme folder.)

---

## Plain answer to the maintainer's question

**It is essentially done, and the record is what is stale — but "essentially" is
carrying three specific items, and all three are on the read/UI side rather than
the enforcement side.** The substrate, pluggable SSO, approvals with a real R2
run gate compiled from frozen run settings, review attestations, the shared
library, both quota dimensions enforced at every byte- and token-admitting site,
compartment marking and ingress recording, the mailbox, the admin surface and the
cutover runbook are all built, wired, reachable and tested — 1438 scoped Python
tests, 23 PostgreSQL contention tests and 99 frontend tests all green, including
an end-to-end HTTP round trip that refuses a run, approves it, and then admits it.
What is left is: (1) the `Save for review` → `Share inspect link` rename that row
6 required and that still shows the old label in the served bundle; (2) the
approver audit view, which has a working, tested, registered API and no frontend
consumer at all — the one deliverable that fails its own stated purpose of giving
the audit trail a read side; and (3) the composer completion bar, where the three
affordances row 13 asked for do not exist, leaving Send-for-approval and
Send-for-review inside the Pipeline → Checks sub-tab, which is worth
checking against the standing ruling that a blocking state's fix affordance
belongs at the top level. Alongside those, three pieces of housekeeping: the
programme README still publishes a 09-13 status that the 09-20 commit overtook;
`AdminDialog.tsx` and `IdentitiesTable.tsx` are unreachable dead code carrying
213 lines of green tests; and two tracker rows that are genuinely finished are
recorded as `skipped`. None of that is enforcement risk. The honest summary is
that the programme's *backend* is done and proven, and its *last mile to the user*
is about three quarters done.
