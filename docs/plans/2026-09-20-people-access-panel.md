# People & access: unified Composer administration

Status: proposed design and implementation plan; application changes have not been made.

Target: release/0.8.1, confirmed by the maintainer on 2026-09-20. Inspected base:
`5ae03b8ac09561feb47f6dfa0fd1947154d0d669`. Planning branch:
`design/people-access-panel`; worktree: `.claude/worktrees/people-access-panel`.

Position in the identity sequence
([master plan](2026-09-13-kubernetes-and-identity/2026-09-13-kubernetes-and-identity-master-plan.md)):
this work replaces the administration shells that task I9 created
(`AdminDialog`, `IdentitiesTable`, `RolesEditor`, `RelationshipsEditor`; `QuotaEditor`
is kept and re-hosted). It does not depend on task I11 and does not change what
I11 accepts: I11's re-admission step drives the identity administration *routes*,
and every one of those routes and its guard stays as it is. The approver picker
here is an administrator's lookup across the whole authorized directory, so it
uses the new people read facade; the mailbox approver directory
(`GET /api/workflow/mailbox/approvers`) keeps serving a requester choosing their
own approver and is not superseded.

## Recommendation

Replace **User management** and **Identity administration** with one
**People & access** panel. Administrators find a person once, then manage their
access, roles, approvers, usage limits and available sign-in controls in that
person's detail view. Preserve the existing independent backend permissions.

This proposal applies the `using-ux-designer` information-architecture,
web-application and accessibility guidance. It includes independent frontend UX
and backend contract reviews. Findings below come from source inspection, not a
live browser session or a usability study. The design is a hypothesis to validate
with the tasks at the end of this plan.

### Options considered

| Option | Benefit | Cost / limitation |
| --- | --- | --- |
| One dialog containing the existing technical tabs | Small navigation change | Still requires manual identity IDs and separate account/access journeys; does not solve the reported usability problem |
| **One person-oriented panel** | One place to find someone and complete their administration tasks; preserves Composer context | Needs directory lookup and correlation contracts as well as frontend changes; recommended |
| Dedicated administration page | More room for future large-scale administration | Adds routing/navigation scope and takes users away from Composer; unnecessary for this request |

## Current experience and evidence

All paths in this section are relative to the repository root at the inspected SHA.

| Observation | Source |
| --- | --- |
| Separate menu entries have different permission gates | `src/elspeth/web/frontend/src/components/common/UserMenu.tsx:269`, `src/elspeth/web/frontend/src/App.tsx:784` |
| Local account management creates accounts, resets generated passwords and deletes accounts | `src/elspeth/web/frontend/src/components/settings/UserAdminDialog.tsx:7`, `src/elspeth/web/auth/admin_routes.py:112` |
| Identity administration has Identities, Roles and Relationships tabs | `src/elspeth/web/frontend/src/components/admin/AdminDialog.tsx:9` |
| Role and relationship forms ask for opaque identity IDs; the identity table displays username/subject | `src/elspeth/web/frontend/src/components/admin/RolesEditor.tsx:46`, `src/elspeth/web/frontend/src/components/admin/RelationshipsEditor.tsx:59`, `src/elspeth/web/frontend/src/components/admin/IdentitiesTable.tsx:158` |
| Identity listing starts with pending access, paginates by state, and has no text-search parameter | `src/elspeth/web/auth/identity_admin_routes.py:415` |
| Active rows eagerly load quota; pre-provisioning is always displayed beneath the table | `src/elspeth/web/frontend/src/components/admin/IdentitiesTable.tsx:85`, `src/elspeth/web/frontend/src/components/admin/IdentitiesTable.tsx:188` |
| Local username and identity UUID have deliberately different meanings | `src/elspeth/web/auth/admin_routes.py:112`, `src/elspeth/web/frontend/src/components/settings/UserAdminDialog.tsx:28` |
| Deleting a local account also retires its identity, preserves history, and frees its natural key for a fresh identity | `src/elspeth/web/auth/local.py:601`, `src/elspeth/web/coordination/identity_authority.py:1941` |
| Mutation and subsequent reload share one failure handler | `src/elspeth/web/frontend/src/components/admin/IdentitiesTable.tsx:119`, `src/elspeth/web/frontend/src/components/admin/RolesEditor.tsx:35` |

The central UX issue is that the current information architecture follows storage
and API concepts. Someone trying to give Jane access must understand which menu
owns which record, then manually carry identifiers between forms.

## Proposed panel

Account menu entry: **People & access**.

Desktop: one wide modal using the existing dialog chrome, with a directory beside
the selected person's details. Narrow screens: list or detail occupies the panel;
**Back to people** restores the prior query, filter, page and focus. Keep the
Composer session and draft intact underneath the panel.

Illustrative wireframe; names and states are fictional, not measured live data:

```text
People & access                                      Add person    Close
Manage access, permissions and sign-in accounts.

Search name, username or email _________________________________________
Status: All v        Sign-in method: All v        Type: People v

People                         | Jane Doe                 Active
-------------------------------| jane.doe · Local sign-in
Jane Doe       Active   Local  |
Sam Lee        Pending  SSO    | Access
Alex Kim       Not set up      | Access is active.       Disable access
                               |
Previous              Next    | [Roles] [Approvers] [Usage & limits]
                               | User · Deployment-wide · No expiry
                               | Add role
                               |
                               | Sign-in
                               | Local account          Reset password
                               | Advanced details v
```

### Directory

- Default to all human records; make **Pending access** a prominent filter rather
  than the only initial view. A local-only administrator instead sees their
  permitted local accounts and an explanation of that scope.
- Primary label: display name when available, otherwise username, otherwise
  provider subject. Secondary text disambiguates username and provider; identical
  names must never imply identical people. Expose the identity ID in technical
  details and as a copy action, not as a required input for ordinary work.
- Keep the list compact: name, sign-in method and access state. Load roles,
  relationships and quotas for the selected record instead of every row.
- Distinguish **Pending access**, **Active**, **Disabled**, **Access not set up**
  and **Access details unavailable**. The last two are presentation states, not
  new values for the persisted identity access-state enum. A failed lookup must
  not be represented as an absent identity.
- Search covers the full authorized directory, not only the loaded page. Use
  server pagination and stable ordering. Show no total or pending badge unless
  the server supplies an authoritative count.
- Preserve service identities through an explicit **Service accounts** type
  filter for identity administrators. Keep their type visible and preserve
  existing supported administration; creation remains operator-managed.
- Retired historical identities remain distinguishable from a new account that
  reuses the same username. Never merge them by display name or email.
- Preserve the current redaction of never-admitted pending identities: use
  subject/provider when profile fields are withheld. Search, sorting and named
  pickers must use the same authorized projection, not reveal withheld fields
  indirectly through matching. A separately authorized local-account section
  must not backfill the redacted identity profile.

### Person details

| Area | Content and actions |
| --- | --- |
| Access | Current state, relevant reason/date, Approve access / Enable access / Disable access; explain self-action and last-administrator refusals |
| Roles | Existing grants with scope and expiry; add deployment-wide role, revoke a specific grant; explain role purpose in plain language |
| Approvers | “Approvers for Jane” and “People Jane approves for”; searchable named selection and explicit relationship direction |
| Usage & limits | Token and storage usage, personal caps and shared container ceilings; retain unknown/unavailable states |
| Sign-in | Provider-managed explanation, or authorized local password/reset/delete controls; local development scope remains visible |
| Advanced details | Identity ID, provider subject, organisation, relevant lifecycle metadata; copy identifiers without making them the main workflow |

Keep access status visible above the detail sections. Use one active edit form at
a time, with explicit Save/Confirm and Cancel. Do not autosave permission changes
on blur. Editing a role must not silently discard an unrelated credential result.
No new global role/relationship management page is required: existing grant and
relationship actions remain available through person context.

## Core journeys

### Add a local person

1. **Add person → Create local account**, available only to the configured local
   dev administrator. Collect username, display name and optional email.
2. Create through the existing credential route. Show the generated password in
   a dedicated completion region with Copy and Dismiss; no automatic email claim.
3. Show the actual next access step. If no identity exists, say **Access not set
   up**. An administrator with identity authority can **Set up access** using
   exact local provider/subject correlation, an initial role and required note.
4. A local-only administrator sees “Account created. An access administrator must
   set up access.” Do not pretend that creating the password grants access.
5. These are separate commits in separate stores. If credential creation succeeds
   and access setup fails, retain the account and show **Retry access setup**.
   Never rerun account creation as a generic retry or delete it automatically.

### Prepare external sign-in access

**Add person → Prepare sign-in access** is available to identity administrators.
Ask for the provider, its subject identifier, optional organisation, initial role
and required note. Explain how the configured provider identifies the person;
email is not automatically a provider subject. Use the existing pre-provision
operation. State that this prepares ELSPETH access and does not create an IdP
account or send an invitation. Keep backend-supported provider semantics; obtain
any configured-provider hints from server configuration, never infer them from
the administrator's username.

### Approve pending access

**Pending access → person → Approve access** shows the selected person, initial
role and required note. On success, keep the detail visible with its updated
state even if it no longer belongs in the pending list. Offer **Back to pending
access**; do not leave focus attached to a removed row.

Dormant people returning to pending may retain earlier grants. Show the retained
roles before confirmation; label the `none` activation choice **Add no new role**,
not “No permissions”. Initial activation choices remain user, approver, reviewer
and none. The source distinction is in `identity_authority.py:2320`.

### Change a role or approver

The selected person's UUID is bound by the application. Show role scope and the
timezone for expiry entry and display; send a UTC timestamp through the existing
contract. Existing scoped/expiring grants retain their semantics. Initial scope
is deployment-wide, matching the current UI; this is not a scoped-role redesign.
Show roles as a set of grants. Explain incompatible admin/workload-role
combinations and service-specific restrictions using the existing authority
rules; do not model changing roles as one atomic replacement dropdown. Revoke
and grant are separate writes with separate outcomes.

For approvers, show a named picker with provider/username disambiguation and a
confirmation sentence: **“Assign Sam Lee to approve for Jane Doe.”** Preserve
relationship effective dates in the read view. Existing backend eligibility,
self-reference, organisational and graph checks remain authoritative.

### Disable access or delete a local account

Make **Disable access** the normal reversible access-management action, with a
required reason and a named confirmation. Explain relationship consequences from
the existing disable contract; enabling access must not promise to restore
revoked approver links.

Keep **Delete local account** separately labelled under Sign-in. Its confirmation
must explain that it removes local credentials, retires the associated identity,
preserves history and does not transfer old permissions/history to a later user
of that username. Do not describe it as password-only deletion or a synonym for
Disable. See the deletion prerequisite below.

## Permissions and data contracts

### Capability matrix

| Current caller | Panel visibility and allowed data/actions |
| --- | --- |
| Neither capability | No management entry; server rejects management calls |
| Local dev-admin only | Local accounts and credential operations; no identity directory, roles, relationships or quota reads |
| Identity admin only | Identity directory and existing identity/role/relationship/quota operations; no local credential inventory, reset or deletion |
| Both | Combined directory and both sets of actions, with exact correlation |

These are independent capabilities, not one new super-admin boolean. Current UI
signals come from `/api/auth/me` and the mailbox summary; every backend operation
still checks live authority. Refresh capabilities when opening and after an
authorization refusal. Losing one capability removes its data/actions immediately;
the other permitted portion can remain open. Sign-out clears the entire panel,
selection, cached administrative data and any displayed password.

Add an explicit server capability projection for the new shell, so mailbox
loading does not decide whether administration exists. Quota authorization must
retain its own admin/oversight policy: this consolidation does not give oversight
users the identity directory or remove their existing quota workflows elsewhere.

### Required directory support

The current endpoints do not provide a complete searchable combined roster.
Implement a small read facade, proposed as `GET /api/auth/admin/people`, plus a
selected-record lookup. Keep mutation routes and their existing guards separate.

The facade must:

- Resolve the two permissions independently and query only authorized sources.
  An OR admission check alone is insufficient if it then returns every field.
- Return a discriminated record with a stable key, authorized identity fields,
  authorized local-account fields, and advisory action capabilities. Do not
  overload `user_id`: keep `identity_id` and `local_username` separate.
- Represent identity-only and local-account-only records explicitly. Where both
  are authorized, correlate only `provider == local` and `subject == username`.
  Use the authority's natural-key lookup and preserve retired-key semantics.
- Support bounded literal text search, status/provider/type filters, deterministic
  ordering and a bounded page with `has_more`. Filter and order the combined
  permitted records before page slicing; joining only the first identity page
  against all local accounts produces a misleading roster.
- Resolve a selected record directly without rescanning paginated results.
  Local-only references and identity references must be distinct variants. After
  access setup, replace the local-only key with the returned identity key.
- Return name/username/provider labels for relationship endpoints through a
  bounded authorized lookup, including counterparts not on the current page.
- Use no-store responses. Keep generated passwords out of this read facade,
  persistent stores, URLs, logs and analytics.
- Treat credential-store or identity-store failure as an explicit unavailable
  source, never as an empty source. If a complete merged page cannot be formed,
  show an error and Retry rather than claiming complete search results.

No new identity schema or account-linking mechanism is proposed. Use typed owned
DTOs, repository methods and existing worker execution boundaries. Role labels
are not authorization decisions, and action capabilities are UI advice only.
The stores do not share a transaction: read results are snapshots for display,
with fresh checks on every mutation.

### Deletion prerequisite to resolve during implementation

Source inspection shows the dev-admin deletion route checks self-deletion by
username, then `LocalAuthProvider.delete_user()` deletes the credential before
calling identity retirement. `retire_identity()` disables and rewrites the
natural key; it does not call the normal disable operation. Do not assume the
normal disable path's last-active-admin protection covers this path.

Before carrying deletion into the new UI, add a focused reproduction for the
last-administrator case and retirement failure. If confirmed, repair the server
orchestration and race protection before enabling the replacement action. A
frontend count check is insufficient. Reuse the existing authority/locking
patterns; define recovery for a credential deletion followed by retirement
failure, including the existing idempotent retirement retry. Do not conceal a
confirmed defect by dropping deletion from the delivered feature.

## Interaction and accessibility requirements

- Reuse shared dialog, button, input, spacing and theme primitives. Preserve the
  existing Account-trigger focus restoration and modal focus containment.
- Use native controls, visible labels and labelled sections. If detail sections
  use tabs, implement their arrow/Home/End and roving-focus contract.
- On narrow screens, show list or details with a visible Back action. Verify
  reflow at 320 CSS pixels and 200% zoom; long subjects/emails must wrap without
  hiding confirmation buttons. Test light, dark and forced-colors themes.
- Give statuses text labels, errors associated field descriptions, and actions
  person-specific accessible names. Screen-reader announcements name both the
  person and the completed action, without reading a password aloud in a live
  status region.
- Loading, load failure with Retry, empty directory and no matching results are
  separate states. A failed initial request must not keep saying “Loading”.
- Distinguish mutation rejection, uncertain transport outcome and successful
  mutation followed by failed refresh. For the last case say **“Saved. Could not
  refresh details.”** Reconcile uncertain outcomes before offering another write.
- Cancel or ignore stale requests when selection/query/capabilities change;
  never render one person's response under another person's heading. Bind every
  action to the selected stable identity, not its position in a result list.
- Keep safe draft inputs after validation failures. Warn before abandoning
  unsaved edits. Confirmation is an inline subview in the existing modal, not a
  second competing focus trap. Escape cancels that subview first.
- Keep generated passwords in local component memory until explicitly dismissed,
  replaced, or the panel closes. Warn before voluntarily closing while the only
  displayed copy is present. Never delay clearing on sign-out/permission loss.
  Copy failure leaves manual selection available. A lost response must explain
  that recovery requires an explicit new reset, not password retrieval.
  Password reset does not revoke outstanding JWTs (`auth/local.py:649`); its
  confirmation and success copy must not promise that it signs everyone out.

## Implementation sequence

Each step is part of one cohesive delivery. A navigation-only intermediate state
is useful for development but does not satisfy this request.

1. **Pin contracts and reproduce risky lifecycle cases.** Read current
   CONTRIBUTING whole-tree gates. Add meaningful regressions for separate
   capabilities, same-name/different-provider records, credential-only records,
   recycled usernames, deletion protection and partial failure. Decide the typed
   directory response and its stable-key variants before building consumers.
2. **Add directory reads.** Implement authorized search, combined pagination,
   selected-record and counterpart lookup in the existing auth/identity layers.
   Test source failure and authority loss; fix any confirmed deletion prerequisite
   through existing authority patterns. Keep writes on guarded existing routes.
3. **Build the panel and person context.** Add `PeopleAccessDialog`, directory,
   detail and capability/data hooks under frontend `components/admin/`. Replace
   the two App booleans, AppHeader callbacks and UserMenu entries with one. Make
   RolesEditor, RelationshipsEditor and QuotaEditor consume selected-person
   context. Extract credential forms/results from UserAdminDialog without nesting
   its old modal. Add scoped styles using existing tokens.
4. **Complete all journeys and failure states.** Named approver selection,
   local create → access setup, external pre-provision, role expiry, quotas,
   disable/enable and delete confirmations. Preserve current backend constraints,
   service identity visibility, role scopes, effective dates and shared ceilings.
5. **Validate and retire old shells.** Remove superseded dialog entry points and
   tests that merely pin their old labels; transfer behavioral regression cases
   to the new panel. Run the checks below, review the final diff, and demonstrate
   the task scenarios before proposing the branch for release integration.

Primary existing files: frontend `App.tsx`, `components/common/AppHeader.tsx`,
`components/common/UserMenu.tsx`, `components/settings/UserAdminDialog.tsx`,
`components/admin/{AdminDialog,IdentitiesTable,RolesEditor,RelationshipsEditor,QuotaEditor}.tsx`,
`api/{client,identityAdmin,workflow}.ts`, and `types/{identityAdmin,workflow}.ts`.
Backend seams: `web/auth/{admin_routes,identity_admin_routes,local}.py`,
`web/coordination/identity_authority.py` and its repository collaborators.

## Validation and completion criteria

### Automated checks for implementation

- Frontend component/API tests cover all four capability combinations, permission
  loss while open, search beyond the first page, unlinked accounts, duplicate
  names, stale responses, partial saves, transient passwords and every journey.
- Preserve behavioral cases from `UserMenu.test.tsx`, `UserMenu.workflow.test.tsx`,
  `UserAdminDialog.test.tsx`, `AdminDialog.test.tsx`, `IdentitiesTable.test.tsx`,
  `AdminDialog.quota.test.tsx`, `QuotaEditor.test.tsx` and `api/identityAdmin.test.ts`.
- Run frontend typecheck, ESLint, CSS lint, build and the frontend test suite.
  Stylesheet changes require the whole frontend suite per CONTRIBUTING's CSS
  barrel/token/forced-colors gate.
- Backend focused checks include `tests/unit/web/auth/test_admin_routes.py`,
  `test_identity_admin_routes.py`, identity repository/authority tests and new
  directory tests. Cover authoritative permissions, correlation, pagination,
  literal search and absence of credential leakage to identity-only callers.
- SQL, persistence or lock changes require the serial PostgreSQL testcontainer
  selection, including `test_identity_last_admin_race_postgres.py` and
  `test_identity_rebound_lock_order_postgres.py`. Shared authority changes require
  the full default Python suite before merge. Use the canonical full-suite gate,
  one broad suite at a time, with explicit worktree import provenance and saved
  logs/exit codes. Apply all affected whole-tree contract/lint gates; do not sign
  or globally clear the standing trust-tier corpus.
- Run browser scenarios serially in this worktree with an isolated test backend
  and disposable test identities, not live account mutations. Include keyboard
  flow, focus return, axe checks, responsive layout and a manual screen-reader
  check. Do not infer accessibility conformance from axe alone.

### Task-based acceptance

Ask an administrator unfamiliar with the new layout to complete these tasks
without telling them which section to use:

1. Find a pending person and give them the intended initial access.
2. Create a local account and explain whether access is ready or needs setup.
3. Grant an expiring role to the intended person without pasting an identity ID.
4. Assign an approver and correctly describe who approves for whom.
5. Reset a local password and recover from clipboard failure.
6. Change a personal quota and explain the shared container ceiling.
7. Disable access and distinguish that from deleting a local account.

Record task completion, wrong-person/direction errors, ID-copying and points of
hesitation. Completion requires all supported journeys in one panel, zero required
opaque-ID entry for routine person/role/approver tasks, correct permission behavior
and clear partial-failure recovery. Do not invent time-savings claims; compare
timings only after observing both versions.

## Scope and validation status

This work creates the planning worktree and this document. No application code,
authentication settings, live users or deployed service has changed. No automated
application tests or browser/usability checks have been run for this documentation
change. The implementation checks above are required future work, not results.

The proposal does not introduce invitation email delivery, IdP account editing,
bulk permission operations, identity merging, a new role model, or audit-history
erasure. Composer planning/provider behavior is outside this UI consolidation.
