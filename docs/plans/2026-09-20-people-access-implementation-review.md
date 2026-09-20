# People & access implementation review

**Verdict: changes requested.** The unified panel implements the intended
person-oriented navigation, but the defects below affect credential retention,
complete access information, selection integrity and recovery.

Reviewed release/0.8.1 at `a76afb5cf711c75785dea62dc53e98ebdd304c4c` on
2026-09-20. The clean `design/people-access-panel` worktree had the identical
commit. Implementation range: `839a7be50^..a76afb5cf`; comparison specification:
`docs/plans/2026-09-20-people-access-panel.md`. Unrelated uncommitted documentation
in the primary checkout was excluded.

Applied the UX Designer information-architecture, web-application and
accessibility guidance. Independent reviews covered directory/authorization,
credential lifecycle and frontend behavior; the coordinating review covered
integration and mutation recovery. These are source and executable reproduction
findings, not a new live-browser or human usability assessment.

## Findings

### 1. P1 — Generated passwords remain visible after local credential authority is lost

**Source:** `src/elspeth/web/frontend/src/components/admin/PeopleAccessDialog.tsx:81-89`
and `:217`; credential insertion at `:59-62`.

The capability-change handler resets selection and filters but leaves the
generated-credential array intact. After a fresh capability response reports
`local_accounts=false` while identity administration remains available, the
password and Copy control are still rendered. This contradicts the plan's
immediate clearing on permission loss. This is a retained-secret UI defect;
the review does not claim that the backend permits unauthorized password resets.

**Reproduction:** reset a local person's password, assert it is displayed, cause
an authorization refusal, return identity-only capabilities, wait for “Your
permissions changed”, and observe the same password still displayed. A component
probe using the actual panel reproduced this.

**Fix:** clear credentials when local authority is lost. Bind credential response
callbacks to an authorization generation so a late response cannot repopulate
the cleared state. Cover both already-displayed and in-flight results.

### 2. P2 — Roles and approver relationships silently stop at the first 50 records

**Source:** `src/elspeth/web/frontend/src/components/admin/RelationshipsEditor.tsx:27-33`
and `:123-125`; analogous grant reads in `RolesEditor.tsx:42-55`.

The editors call the existing paginated APIs with default limit/offset, discard
pagination information, and offer no next page. The former editors provided
pagination. Relationships are divided into incoming and outgoing lists only
after this partial fetch, so an incoming approver beyond the first page can be
reported as “Nobody is assigned to approve for Jane Doe.” Hidden grants/links
cannot be inspected or revoked through this view.

**Reproduction:** an API fake slices 51 actual edge records into a 50-record
first page: 50 outgoing links and one incoming approver on page two. The panel
requests only page one, renders the false no-approver message, and has no Next
control. The probe checks that the complete fixture includes the incoming link.

**Fix:** retain explicit pagination and accurate page-scoped wording, or collect
all pages with appropriate bounds and loading/error states before asserting
complete absence. Preserve bounded counterpart-name lookup batches.

### 3. P2 — A late mutation switches selection back to the previous person

**Source:** `src/elspeth/web/frontend/src/components/admin/PeopleAccessDialog.tsx:175-177`.
Call chain: `RolesEditor.tsx:41-46` → `PersonDetail.tsx:139` → shell callback.

`onPersonChanged(key)` treats an update to any different person as a request to
replace the current selection. Mutation reloads still call it after their editor
has unmounted. It conflates ordinary record refresh with the special local-key
to identity-key transition after access setup.

**Reproduction:** start a role grant for Jane without an optional note, hold the
response, select Sam and verify Sam's heading, then complete Jane's request. The
panel switches back to Jane. This is a visible selection change, not evidence of
an invisible wrong-ID write, but it displaces the operator's chosen context.

**Fix:** capture the originating selection/generation. Refresh changed records
without changing selection; apply key migration only if its originating record
is still selected. Cover mutation callbacks as well as direct-read responses.

### 4. P2 — Name and email search lose a local person immediately after access setup

**Source:** `src/elspeth/web/auth/people_routes.py:472-473` and `:488-503`.

Before first sign-in, a provisioned local identity has no profile name/email;
those values exist in its linked local account. The merged view uses that
account's name for display, but search removes bound accounts from its account
segment and searches only the identity table. Thus the label displayed in the
directory cannot find the person. Approver pickers use the same facade.

**Reproduction through real HTTP routes and temporary databases:**

```text
before Jane Doe 200 ['local:jane']
before jane@corp.example 200 ['local:jane']
before NoSuchPerson 200 []
POST /api/auth/admin/identities (local/jane, role=user) -> 201
GET /api/auth/admin/people/local/jane -> 200
  identity.display_name=None; identity.email=None; access_state=active
  local_account.display_name='Jane Doe'; local_account.email='jane@corp.example'
after Jane Doe 200 []
after jane@corp.example 200 []
after jane 200 ['identity:<new-id>']
after NoSuchPerson 200 []
```

**Fix:** search the authorized combined projection before pagination, including
eligible linked-account fields. Preserve pending-profile redaction and exact
provider/subject joins; add before/after provisioning and page-boundary cases.

### 5. P2 — Last-admin protection blocks completion of an already-failed retirement

**Source:** `src/elspeth/web/coordination/identity_authority.py:2132-2136`,
`src/elspeth/web/auth/local.py:667-675`; CLI handling at `src/elspeth/cli.py:2223-2234`.

Credential deletion commits in one store before retirement/audit commits in
another. If retirement fails, the identity can remain active without a credential.
The new last-admin refusal runs before the credential callback and cannot
distinguish retrying that incomplete retirement from deleting a still-existing
last credential. If other administrators are subsequently removed, the documented
retry recovery is now refused. This is a regression in recovery, not a claim that
the original two-store failure window was introduced by this change.

**Reproduction with real temporary auth/session stores and one injected audit
failure (process exit 0):**

```text
initial admins 2 credentials ['alice', 'bob']
first delete alice simulated retirement audit outage
after failure admins 2 credentials ['bob']
delete bob LocalUserDeletion(credential_deleted=True, identity_retired=True)
before retry admins 1 credentials []
retry alice refused LastActiveAdminProtected the last active human administrator cannot be removed
final admins 1 credentials []
bootstrap recovery refused AdminAlreadyBootstrapped administrator history or current authority makes this bootstrap mode inert
```

The normal Bob removal is the positive control for multi-admin retirement. The
remaining phantom identity also prevents ordinary zero-admin bootstrap recovery;
the reproducer invoked the real operator-recovery bootstrap and measured that
refusal.

**Fix:** preserve protected deletion and explicit recovery for already-committed
credential removal. Establish a race-safe cross-store recovery contract; do not
simply remove last-admin protection or rely on a frontend count. Add the sequential
failure case and appropriate PostgreSQL contention coverage for the chosen repair.

### 6. P2 — Failed reconciliation removes the retry control while keeping writes blocked

**Source:** `src/elspeth/web/frontend/src/components/admin/peoplePanel.ts:109-124`;
`MutationNoticeView.tsx:18-19`.

After a transport failure, `mustReconcile` correctly blocks another write. If
“Check current details” then encounters a transient read failure, `refresh()`
changes the notice from `uncertain` to `rejected` without clearing the block.
Rejected notices have no refresh action. The form is left blocked with no local
recovery control; leaving and reopening discards the form and its reconciliation
state rather than completing recovery.

**Reproduction:** role grant rejects with a transport error; the reconciliation
read returns 503. Grant remains disabled and the notice has no Check/Retry button.
A desired-behavior component regression fails on that missing control. Its paired
successful-read control passes and re-enables the form.

**Fix:** retain reconciliation-required state and its retry affordance through
read failures. Distinguish a failed read from a rejected original mutation, and
refresh capabilities when the read fails due to lost authority.

### 7. P2 — “Check current details” after uncertain deletion performs no check

**Source:** `src/elspeth/web/frontend/src/components/admin/SignInSection.tsx:35-37`.

Deletion's mutation helper receives `nothingToReload`, an immediately successful
no-op. That is usable for a confirmed successful delete, but the same callback
also handles reconciliation after a lost response. Clicking “Check current
details” therefore clears uncertainty and re-enables deletion without querying
whether the account exists or retirement completed. The displayed state can
remain stale after an operation that actually succeeded.

**Reproduction:** reject delete with a transport error, click Check current
details, and observe the delete confirmation enabled again with no read callback
invocation. The desired-behavior regression fails: expected one read, got zero.

**Fix:** supply a real deletion-specific reconciliation operation. Interpret
authoritative absence as a possible successful deletion and refresh or leave the
detail view appropriately; handle retirement failure separately. Never label a
no-op as verification of current server state.

### 8. P2 — Switching detail tabs silently discards unsaved input

**Source:** `src/elspeth/web/frontend/src/components/admin/PersonDetail.tsx:201-208`.

Mouse and arrow-key tab changes call `setTab` directly and unmount the old editor.
Its local draft disappears and its dirty registration is removed. The shell's
discard guard protects person changes, but not section changes.

**Reproduction:** type a grant note, switch Roles → Approvers → Roles, reopen
Add role. No discard prompt appears and the note is empty. The probe confirms
that the note contained actual input before navigating.

**Fix:** apply the leave guard to tab changes or preserve the drafts until an
explicit discard. Cover both pointer and keyboard navigation.

### 9. P2 — SQLite search fails on uppercase accented names, even exact copied text

**Source:** `src/elspeth/web/coordination/identity_authority.py:1369-1382`.

The query uses Python Unicode `lower()` while the stored values use SQLite's
`lower()`. Those do not perform the same transformation for an uppercase accented
letter. An admitted identity named `Élodie Martin` cannot be found by copying its
displayed name into search. Unlinked local-account matching uses Python casefold,
so the directory also has inconsistent behavior between its two sources.

**Reproduction through the real authority on initialized SQLite:**

```text
'Martin' ['elodie']
'Élodie Martin' []
'élodie' []
'not-present' []
```

The surname and absent-name searches control for presence and absence.

**Fix:** use consistent Unicode matching across directory segments and supported
database engines. Preserve literal wildcard escaping and redaction. Add SQLite
and PostgreSQL cases for the chosen normalization behavior.

## Verification evidence

Existing focused checks, rerun for this review:

```text
Frontend: PeopleAccessDialog.test.tsx, PeopleAccessDialog.journeys.test.tsx,
QuotaEditor.test.tsx, UserMenu.test.tsx, UserMenu.workflow.test.tsx, App.test.tsx
Test Files  6 passed (6)
Tests       138 passed (138)
exit=0

Backend: tests/unit/web/auth/test_people_routes.py
         tests/unit/web/coordination/test_identity_directory.py, -n 0
20 passed in 30.04s
exit=0

Additional frontend probes asserting observed defects:
4 passed (4), exit=0

Additional desired-behavior reconciliation regressions:
2 failed | 1 passed (3), exit=1
Failures: reconciliation retry disappeared; deletion reconciliation read count 0
Control: successful role reconciliation re-enabled the form
```

The four passing probes intentionally assert the observed buggy behavior; they
are reproduction evidence, not correctness tests. The two failing regressions
assert the required behavior. Source and logs are preserved under
`.claude/lanes/people-access-review/`; temporary frontend source test files were
removed after execution. Backend reproductions used temporary databases and
explicit worktree import provenance. No live account/service changes were made.

No production files were changed. This review did not rerun the full Python,
PostgreSQL, lint or browser suites; the implementation document's earlier gate
claims are historical evidence, not fresh results from this review. No human
screen-reader or usability acceptance is claimed.
