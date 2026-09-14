### Task I11: Cutover mechanics — the operator's instructions for the workflow-epoch window

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: I10. Runs before: K8. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered last in Workstream I (I10 → I11). Spec: §Rollout order step 6
(sso-design.md:1335-1338, "Prepare the operator's cutover instructions and
compatibility record" and "Performing the cutover is an operator deployment
decision, not this sprint's work"), §Two epochs, one window (:585-656: the
per-deployment cutover, the `(provider, subject, pre_cutover_user_id,
identity_id)` mapping artifact — `pre_cutover_user_id` was the pre-0.8.0
username; after 0.8.0 the pre-window key is an `identity_id`, so the artifact
below names that column `pre_cutover_identity_id` — the
re-admission that "is part of the window, not the morning after", and the
notice that "says re-activation happened, not merely 'log in again'"), R2
(:1060-1063) and R11 (:1115-1124). Tickets: `elspeth-5ef01c6ad1` (its
epoch-fan-out half is I0's; this task is its runbook-split and operator-notice
half) and `elspeth-b03f0aa218` (the operator-only cutover, countersign and
judge-bundle firing — NOT done here; it stays open after this task).

Measured on HEAD 072141b75 so the executor knows what is already there:

- The Playwright OIDC harness rewrite to the confidential-client handoff
  LANDED on 2026-09-05 (`git log -1 -- src/elspeth/web/frontend/tests/e2e/harness/oidc-evidence.ts`
  → 851a15dfb). `grep -rn epoch src/elspeth/web/frontend/tests/e2e/harness/
  src/elspeth/web/frontend/playwright.oidc.config.ts
  src/elspeth/web/frontend/tests/e2e/aws-ecs-oidc.staging.spec.ts` exits 1:
  no epoch literal lives there, so this task touches nothing under
  `src/elspeth/web/frontend/`.
- Every epoch NUMERAL site (`CHANGELOG.md:9-10`, the runbook heading at
  `staging-session-db-recreation.md:5` and its `:157/:159` cites, the
  `PRAGMA user_version` comments at `:734-735`, `sharing-pipelines.md:73`,
  the ECS compatibility record at `aws-ecs-deployment.md:1609-1611`,
  `receipt_contracts.py`, the ACA runbook lines the regex at
  `test_azure_container_apps_runbook_contract.py:182-199` pins) is I0's
  fan-out. **This task adds no epoch numeral anywhere**; the Step 1 tests
  assert that, because each added numeral would be one more site for the
  next bump and a regex hit in the ACA contract test.
- The re-admission mechanics are already in the runbook: `### Every local
  account lands \`pending\` after this reset` (`staging-session-db-recreation.md:165-229`,
  with `elspeth composer users bootstrap-admin` at :188-189 and the
  pre-provision / activate API paragraphs at :198-212) and `### What the
  derived keys change across this boundary` (:231-262). This task does not
  repeat them; it adds what the workflow epoch needs on top and links to them.
- What is MISSING on HEAD, and is this task's deliverable: (1) why every
  bearer session is refused after the recreate and the notice text that says
  so; (2) the identity-mapping artifact export before the drop, so
  pre-window audit evidence stays interpretable; (3) the governance gate —
  with `workflow_governance=on` (I8) nobody can run until an approver is
  re-activated (R2, I3); (4) the per-deployment-shape routing (ECS, ACA,
  VM-SQLite, VM-PostgreSQL — the VM in-place rebuild is withdrawn, D21, so
  every shape recreates); (5) the token-admission paragraph at :214-225,
  which says "complete accounting is not implemented" and "Adding policy
  rows does not make chargeable work available in this release" — both
  false once I1's ledger lands, and no other task owns that runbook text;
  (6) pointers from each deployment runbook to the notice (none of the ACA
  runbooks mentions re-admission at all: `grep -n "pending\|re-admission"
  docs/runbooks/azure-container-apps-*.md` prints nothing).

**Files:**
- Modify: `docs/runbooks/staging-session-db-recreation.md:214-225` (the
  paragraph starting `**Token admission is already fail-closed.**` and ending
  `shortcut.`; the :227-229 paragraph `Verify that an admitted account can log
  in again,` follows it and is kept),
  `:262-264` (two new `###` subsections inserted after :262 `independent
  Secrets Manager binding — and is unaffected by this boundary.` and before
  :264 `## Historical Cutover: 0.7.0 (two-DB reset)` — they MUST sit inside
  that range: `test_current_cutover_and_verification_use_live_schema_epochs`
  slices the current section on those two `## ` headings), and `:778` (the
  procedure sentence `On the 0.8.0 cutover, also settle the identity lockout
  at this point`)
- Modify: `docs/runbooks/aws-ecs-deployment.md:1441-1443` (one paragraph
  inserted after :1441 `first person.` — the end of the "Identity Providers
  guide, §Admitting the first person" paragraph — and before :1443 `The
  legacy browser-client settings`). No bash fence is added:
  `test_every_bash_fence_is_syntactically_valid` (test_aws_ecs_runbook_contract.py:407)
  runs `bash -n` over every fence and :422 forbids a line starting `aws `.
- Modify: `docs/runbooks/azure-container-apps-existing-service-redeploy.md:268-270`
  (one paragraph inserted after the `> **LIVE:**` callout at :267-268 and
  before :270 `## Rollback`). Zero epoch numerals: the ACA contract test
  regex-matches `session epoch (\d+)` case-insensitively across this file.
- Modify: `docs/runbooks/ansible-ubuntu-deployment.md:60-66` (`## Release
  compatibility`, currently the 0.7.1→0.8.0 sentence; unpinned on HEAD —
  `grep -n "0.7.1\|Release compatibility" tests/unit/docs/test_deployment_platform_docs.py`
  prints nothing)
- Modify: `CHANGELOG.md:19-26` (the paragraph that opens `ELSPETH does not
  migrate either predecessor database in place before 1.0.` and closes with
  the two-line sentence `Do not roll older code back over the recreated
  databases; keep the service drained and repair this release forward.` at
  :25-26, under `## 0.8.1 - 2026-09-10`). I0 edits :9-10 of the same section
  and may shift these lines by one; anchor on that whole closing sentence,
  not the number — its last line alone, `drained and repair this release
  forward.`, also occurs at :554 in the 0.8.0 section (measured; the whole
  sentence occurs once). As I0 says, confirm the section with the operator
  before the first commit (see Global Constraints).
- Test: `tests/unit/docs/test_staging_session_recreation_policy.py` (71
  lines on HEAD; 3 tests, `exit=0` measured 2026-09-13; the new tests append
  after :71). It already owns the cutover contract and reads
  `CHANGELOG.md` (:39-51), so the four other docs are read from here rather
  than opening four more test files.
- Create: `tests/unit/web/auth/test_cutover_identity_restoration.py` — the
  re-admission behaviour, which a text contract cannot prove: it executes the
  runbook's SQLite export statements verbatim against a real pre-window
  sessions store and drives the documented restore of identities, grants
  and approver edges through the real identity administration routes
  (`POST /api/auth/admin/identities`, `/roles` and `/relationships`) against
  a recreated one. It sits beside
  `test_identity_admin_routes.py` because it imports that module's `_Harness`,
  `_build` and `_bearer` rather than copying a harness that I1, I3, I4, I7
  and I9 extend, and it is a new file rather than an append because those
  tasks edit that module.
- NOT touched: `src/elspeth/web/frontend/**` (landed, see above);
  `docs/runbooks/kubernetes-deployment.md` (K8's file; may not exist when
  this task executes; K8 runs after I11 and appends its own row).

**Interfaces:**
- Consumes:
  - I0: `SESSION_SCHEMA_EPOCH = 57` (`sessions/models.py:333`) and every
    numeral site already fanned out; the three HEAD tests in the policy file
    pass again at 57 before this task starts.
  - I8: `WebSettings.workflow_governance: Literal["off", "on"]`
    (`ELSPETH_WEB__WORKFLOW_GOVERNANCE`); `_check_auth_mode` refuses
    `on` + `auth_provider=local` + `registration_mode=open` (R11) and `on`
    without `compartment_id`, as readiness failures; the `configuration.md`
    rows I8 adds after :372.
  - I3: execute refuses HTTP 409 `error_type="approval_required"` unless an
    `approved` row matches the compiled binding. `decide` admits only the
    approver the request names (`approvals.approver_identity_id`, I3
    decision 7): a live approver the request was not addressed to gets
    `ApprovalNotFound` (404 `approval_not_found`), the author gets
    `ApprovalAuthorIsApprover`, and no approval check reads
    `identity_relationships`.
  - I1: `AdmissionRefusalReason.QUOTA_EXCEEDED = "quota_exceeded"`
    (`contracts/chargeable_admission.py`), the ledger's "NULL means unknown,
    never zero" rule and "a day with no rows measures zero"; on HEAD the enum
    (:19-25) carries `quota_policy_missing` and `token_accounting_unavailable`.
  - HEAD: `elspeth composer users bootstrap-admin PROVIDER SUBJECT --note TEXT
    [--username TEXT] [--session-db-url URL] [--landscape-url URL]
    [--quota-tokens-per-day N --quota-storage-bytes N]` (measured from
    `--help`; the two quota options come together or not at all); `GET /api/auth/admin/identities?access_state=<pending|active|disabled>&limit=&offset=`
    (`identity_admin_routes.py:401-421`; `IdentityView` :161-187 never
    exposes `raw_claims_json`); `POST /api/auth/admin/identities` with
    `PreProvisionIdentityRequest` (:117-125: `provider`, `subject`,
    `username?`, `organisation_id?`, `role: ActivationRole =
    Literal["user", "approver", "reviewer", "none"]` (`contracts/auth.py:65`),
    `note`) returning `ActivationResponse.identity.identity_id` (:239-242);
    `POST /api/auth/admin/roles` with `GrantRoleRequest` (:136-143:
    `identity_id`, `role: IdentityRole` — every role in `_IDENTITY_ROLE_CHECK`,
    `sessions/models.py:3311` — `scope: str | None`,
    `expires_at: AwareDatetime | None` parsed with `strict=False`, `note`),
    the only admin route that carries a grant's `scope` and `expires_at`:
    `pre_provision_identity` writes its grant with `scope=None` and
    `expires_at=None` (`coordination/identity_authority.py:2226-2227`), and so
    does `bootstrap_admin` (:2114-2115), whose CLI has no expiry option;
    `grant_role` (:2573) refuses `expires_at <= now` with a bare
    `ValueError("expires_at must be in the future")` (:2601), which is not an
    `IdentityAuthorityRefusal`, so `_refused` (`identity_admin_routes.py:371-384`)
    does not translate it and the client receives HTTP 500 with no row written
    (measured through `_build`'s app with `raise_app_exceptions=False`);
    `_is_active` (`identity_authority.py:889-892`: unrevoked, and
    `expires_at` NULL or later than database now) is the predicate every
    authorization read applies; `revoke_role` refuses the last active human
    administrator with `LastActiveAdminProtected` (:2680; route refusal code
    `last_active_admin_protected`); `GET /api/auth/admin/roles?identity_id=`
    returns `RoleView` (`identity_admin_routes.py:201-210`, with `scope` and
    `expires_at`). `AwareDatetime` accepts ISO-8601 with `Z`
    (`2999-06-30T12:00:00.123456Z`) and refuses both PostgreSQL's default text
    (`2999-06-30 12:00:00.123456+00`) and SQLite's stored naive text
    (`2999-06-30 12:00:00.123456`) (measured 2026-09-15 with a model carrying
    `GrantRoleRequest`'s `expires_at` field definition);
    `SessionTokenIssuer.mint` writes `"sub": identity_id`
    (`auth/session_token.py:193`), `authenticate` (:241-250) and `refresh`
    (:252-269, which calls `authenticate`) raise
    `AuthenticationError("Invalid token")` when `_principal_is_active` is
    false; `identities` (`sessions/models.py:3328`: `identity_id`,
    `provider`, `kind`, `subject`, `access_state`) and `identity_roles`
    (:3406: `identity_id`, `role`, `expires_at` (:3421,
    `DateTime(timezone=True)`; SQLite stores it as naive text
    `YYYY-MM-DD HH:MM:SS.ffffff` that the authority reads back as UTC —
    measured 2026-09-15), `scope`, `revoked_at`);
    `quota_policies.tokens_per_day` / `storage_bytes` (:3843/:3849);
    `quota_default_tokens_per_day` / `quota_default_storage_bytes` /
    `quota_container_tokens_per_day` (`config.py:538-541`); the sessions-store
    tables `approvals` (:3589), `review_attestations` (:3738),
    `library_entries` (:3785) — all discarded by the recreate.
  - HEAD tests: `tests/unit/web/auth/test_identity_admin_routes.py`
    `_Harness` (:103-111: `app`, `authority`, `audit`, `root_identity_id`,
    `engine`), `_build(tmp_path)` (:118-158: a file-backed sessions store at
    `tmp_path / "sessions.db"` — the directory must already exist — local
    users `root`, `alice`, `bob` and `carol` with password `password123`, and
    `root` bootstrapped as the first administrator through
    `RepositoryIdentityAuthority.bootstrap_admin`) and
    `_bearer(client, username)` (:165-168). I1 (`:93-98`, a
    `record_quota_exceeded` member), I3 (`:50-95`), I4 (`:96-97`) and I9
    (`:97`, a member above `def only(`) extend `_RecordingAuditWriter`, and
    I7 widens the `:28` import and appends tests after `:570-579`; none of
    their plan files defines or renames these three (the only plan hit for
    `def _build`, `def _bearer` or `class _Harness` is I10's own `_bearer`,
    in another module).
  - I7: Step 17 replaces the `POST /api/auth/admin/roles` handler (I7
    Produces: "the unchanged admin path, plus the delegated arm"). Its admin
    arm still passes the body's `scope` and `expires_at` to `grant_role` and
    still translates only `IdentityAuthorityRefusal` through `_refused`, so
    the bare `ValueError` for an expiry that is no longer in the future still
    reaches the client as HTTP 500 on the I7 tree. A task that later types
    that refusal must update this task's re-admission step 3 and `_restore`
    in `test_cutover_identity_restoration.py`. The delegated arm,
    `RepositoryIdentityAuthority.grant_curator_as_approver`, admits an
    approver's `curator` grant only when an active `approver` edge runs
    directly from the approver to the target; `GET /api/workflow/audit-view`
    (`RepositoryWorkflowScopeReader`) shows an approver the identities their
    active `approver` edges reach transitively. Re-admission step 4 names both.
  - I9: `GET /api/workflow/mailbox/approvers` returns
    `ApproverDirectoryResponse(approvers, suggested_identity_ids)`; the
    suggestion is the live approvers with an active `approver` edge to the
    caller, read through `list_relationships`. Re-admission step 4 names it
    as the picker's default suggestion.
  - HEAD relationship administration (`auth/identity_admin_routes.py`):
    `POST /api/auth/admin/relationships` (:670-699, HTTP 201) with
    `AssertRelationshipRequest` (:152-158: `from_identity_id`,
    `to_identity_id`, `relationship_type`, `effective_from: AwareDatetime |
    None` and `effective_until: AwareDatetime | None`, both parsed with
    `strict=False` like `GrantRoleRequest.expires_at`, and `note`), returning
    `RelationshipView` (:219-230, `relationship_id` first);
    `GET /api/auth/admin/relationships?identity_id=&include_revoked=`
    (:646-668, edges on either end, unrevoked only by default) and
    `POST /api/auth/admin/relationships/{relationship_id}/revoke`
    (:701-726); `RelationshipType = Literal["approver"]`
    (`contracts/auth.py:57`). The route calls
    `RepositoryIdentityAuthority.assert_relationship`
    (`coordination/identity_authority.py:2695`), which refuses, each through
    `_refused` as a typed response and before any row is written: a missing
    end (`IdentityNotFound`, 404 `identity_not_found`), an end that is not
    `active` (`IdentityNotActive` :167, 409 `identity_not_active`), a `from`
    identity with no active `approver` grant (`ApproverRoleRequired` :219, 409
    `approver_role_required`), a self edge (`RelationshipSelfEdge` :214), a
    second active edge into the same `to` (`RelationshipAlreadyActive` :234,
    `DefaultApproverAlreadyAssigned` :229) and a cycle (`RelationshipCycle`
    :224, the bounded ancestor walk :2747-2760); only
    `effective_from >= effective_until` is a bare `ValueError` (HTTP 500). It
    records the restoring administrator as `asserted_by_identity_id` and the
    database now as `asserted_at`. `disable_identity` (:2489) revokes every
    active edge incident to the identity it disables (:2536-2547); an R9
    dormancy re-pend sets `access_state="pending"` (:1688-1700) and "the
    org-tree revocation cascade does NOT run" (:1672-1675), so an edge
    incident to a `pending` identity stays unrevoked.
    `identity_relationships` (`sessions/models.py:3467-3508`:
    `relationship_id`, `from_identity_id`, `to_identity_id`,
    `relationship_type`, `asserted_by_identity_id`, `asserted_at`,
    `effective_from`, `effective_until`, `revoked_at`,
    `revoked_by_identity_id`, `note`; `effective_from` / `effective_until`
    are "ANNOTATION ONLY" and "no check reads" them, :3493-3497; the partial
    unique index `uq_identity_relationships_active_incoming` allows one
    unrevoked edge per `to_identity_id` and `relationship_type`, :3512-3519).
    The window columns are `DateTime(timezone=True)`, stored by SQLite as the
    same naive text as `identity_roles.expires_at`.
  - HEAD authority surface the restoration tests seed and read with
    (`coordination/identity_authority.py`): `IdentityAdminActor` (:278),
    `_WORKLOAD_ROLES = frozenset({"user", "approver", "reviewer", "curator"})`
    (:101, the R8 set `_refuse_role_conflict` (:1075-1083) evaluates over
    active grants only), `RepositoryIdentityAuthority.pre_provision_identity`
    (:2159), `grant_role` (:2573), `revoke_role` (:2648), `disable_identity`
    (:2489), `assert_relationship` (:2695), `revoke_relationship` (:2800),
    `list_relationships` (:1320),
    `read_identity_by_natural_key` (:1249), `active_roles` (:1277),
    `holds_active_role` (:1285) and `count_active_human_admins` (:1294); and
    `identity_roles_table` (`sessions/models.py:3406`), written directly only
    to back-date an expiry, and `identities_table` (:3328), written directly
    only to set the `pending` state an R9 dormancy re-pend leaves, which no
    admin route can produce because dormancy needs a login. No plan file renames or re-signatures any of
    these: `git grep` of the plan directory for `_WORKLOAD_ROLES`,
    `class IdentityAdminActor` and each `def` name hits only I7:147 (the
    `identity_authority.py:2646-2648` Files line) and I7:1437 (Step 16's
    "between `            return grant`" line), which insert
    `grant_curator_as_approver` between `grant_role` and `revoke_role`
    without changing either, and I7:148 (the `identity_admin_routes.py`
    Files line) and I7:1630 (`    async def grant_role(`), the route handler
    also named `grant_role`. The I7 line numbers move whenever I7 is edited:
    re-find each hit by the quoted text.
- Produces:
  - Runbook headings `### Operator notice for the workflow-epoch window` and
    `### Cutover by deployment shape` inside `## Current Cutover:` of
    `docs/runbooks/staging-session-db-recreation.md`; the second carries the
    per-shape table K8 appends a Kubernetes row to (K8 runs after I11).
  - The cutover artifacts, retained with the archive:
    `identity-mapping.pre.csv`, one row per identity (columns
    `provider, subject, pre_cutover_identity_id, kind, access_state`), and
    `identity-grants.pre.csv`, one row per unrevoked grant (columns
    `provider, subject, pre_cutover_identity_id, role, scope, expires_at`;
    `expires_at` is UTC ISO-8601 ending `Z`, empty for a grant that never
    expires), and `identity-relationships.pre.csv`, one row per unrevoked
    edge (columns `from_provider, from_subject, from_pre_cutover_identity_id,
    to_provider, to_subject, to_pre_cutover_identity_id, relationship_type,
    effective_from, effective_until`; both window columns UTC ISO-8601
    ending `Z`, empty when unset; the SQLite export of a store with no
    unrevoked edge is an empty file with no header). After re-admission they
    are completed as `identity-mapping.<window>.csv` (adds `identity_id`),
    `identity-grants.<window>.csv` (adds `restored_role_id` and `outcome`,
    one of `bootstrap`, `restored`, `lapsed`, `not_restored`) and
    `identity-relationships.<window>.csv` (adds `restored_relationship_id`
    and `outcome`, one of `restored`, `not_restored`).
  - The ready-to-send notice as the single ```` ```text ```` fence in the
    notice section.
  - Tests in `tests/unit/docs/test_staging_session_recreation_policy.py`:
    `test_current_cutover_carries_the_workflow_epoch_operator_notice`,
    `test_operator_notice_says_re_activation_before_sign_in`,
    `test_current_cutover_routes_each_shipped_deployment_shape_to_its_recreate_procedure`,
    `test_current_cutover_cites_only_admission_refusals_the_contract_defines`,
    `test_every_deployment_runbook_points_at_the_operator_notice`,
    `test_changelog_states_the_session_invalidation_and_re_activation`.
  - Tests in `tests/unit/web/auth/test_cutover_identity_restoration.py`:
    `test_cutover_restores_each_identity_once_and_only_grants_active_at_restoration`,
    `test_a_grant_that_lapses_between_the_comparison_and_the_call_is_refused_and_writes_nothing`,
    `test_an_expiring_bootstrap_administrator_gets_its_expiry_back_from_a_second_administrator`,
    `test_a_newly_appointed_administrator_is_not_given_back_a_workload_grant_admin_excludes`,
    `test_cutover_restores_an_edge_only_when_both_ends_and_an_approver_grant_came_back`.

- [ ] **Step 1: Write the failing documentation-contract tests.**

Append to `tests/unit/docs/test_staging_session_recreation_policy.py` after
:71 (the last line, `    assert f"### {section}\n" in _RUNBOOK.read_text(encoding="utf-8")`).
`re`, `Path`, `SESSION_SCHEMA_EPOCH` and `_RUNBOOK` are already bound at
:3-10; one new import is added to the first-party block, directly BEFORE :6
`from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH` (`contracts`
sorts before `core`; placed after :8 it is a ruff `I001` finding — measured):

```python
from elspeth.contracts.chargeable_admission import AdmissionRefusalReason
```

Then the appended tests:

```python


_ECS_RUNBOOK = Path("docs/runbooks/aws-ecs-deployment.md")
_ACA_REDEPLOY_RUNBOOK = Path("docs/runbooks/azure-container-apps-existing-service-redeploy.md")
_VM_RUNBOOK = Path("docs/runbooks/ansible-ubuntu-deployment.md")
_CHANGELOG = Path("CHANGELOG.md")
_NOTICE_HEADING = "### Operator notice for the workflow-epoch window"
_SHAPES_HEADING = "### Cutover by deployment shape"
_NOTICE_ANCHOR = "(#operator-notice-for-the-workflow-epoch-window)"
# Built indirectly so this file can be quoted inside a fenced block of the plan.
_FENCE = "`" * 3


def _current_cutover() -> str:
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    return runbook.split("## Current Cutover:", maxsplit=1)[1].split("## Historical Cutover:", maxsplit=1)[0]


def _subsection(text: str, heading: str) -> str:
    """The body under ``heading`` up to the next ``###`` or ``##`` heading."""
    assert heading in text, heading
    body = text.split(f"{heading}\n", maxsplit=1)[1]
    return body.split("\n### ", maxsplit=1)[0].split("\n## ", maxsplit=1)[0]


def _fences(text: str, language: str) -> list[str]:
    return re.findall(rf"{_FENCE}{language}\n(.*?){_FENCE}", text, flags=re.DOTALL)


def test_current_cutover_carries_the_workflow_epoch_operator_notice() -> None:
    """Spec §Two epochs, one window: the invalidation, the mapping artifact and re-admission are IN the window."""
    cutover = _current_cutover()
    notice = _subsection(cutover, _NOTICE_HEADING)

    # The mechanism, named: a token's ``sub`` is the identity_id and every
    # request re-checks it against the store the recreate empties.
    assert "`sub`" in notice
    assert "`identity_id`" in notice
    assert "`Invalid token`" in notice
    assert "`refresh`" in notice

    # The mapping is exported from the STOPPED live store, before the drop, as
    # three files: one row per identity, one row per unrevoked grant with its
    # scope and expiry, and one row per unrevoked approver edge with both ends
    # -- never claims or contact fields.
    assert "identity-mapping.pre.csv" in notice
    assert "identity-grants.pre.csv" in notice
    assert "identity-relationships.pre.csv" in notice
    exports = _fences(notice, "bash")
    assert len(exports) == 2, "one SQLite export and one PostgreSQL export"
    for export in exports:
        assert "FROM identities" in export
        assert "identity_roles" in export
        assert "FROM identity_relationships" in export
        assert "revoked_at IS NULL" in export
        assert "identity-mapping.pre.csv" in export
        assert "identity-grants.pre.csv" in export
        assert "identity-relationships.pre.csv" in export
        for column in (
            "provider",
            "subject",
            "pre_cutover_identity_id",
            "kind",
            "access_state",
            "role",
            "scope",
            "expires_at",
            "from_pre_cutover_identity_id",
            "to_pre_cutover_identity_id",
            "relationship_type",
            "effective_from",
            "effective_until",
        ):
            assert column in export, column
        for forbidden in ("raw_claims_json", "email", "display_name", "subject_email_at_first_seen"):
            assert forbidden not in export, forbidden
    assert "identity-mapping.<window>.csv" in notice
    assert "identity-grants.<window>.csv" in notice
    assert "identity-relationships.<window>.csv" in notice
    # Edges come from the export like identities and grants, never from the archive.
    assert "archived store" not in notice

    # Re-admission goes through the audited paths that already exist: each
    # identity once with no role, then each grant still active with its scope
    # and expiry, then each edge whose ends and overseeing approver grant came
    # back. tests/unit/web/auth/test_cutover_identity_restoration.py
    # executes the SQLite export and drives these steps through the routes.
    assert "elspeth composer users bootstrap-admin" in notice
    assert "POST /api/auth/admin/identities" in notice
    assert '`"role": "none"`' in notice
    assert "POST /api/auth/admin/roles" in notice
    assert "`lapsed`" in notice
    assert "`last_active_admin_protected`" in notice
    assert "POST /api/auth/admin/relationships" in notice
    assert "`approver_role_required`" in notice

    # The governance gate: with enforcement on, no approver means no runs.
    assert "workflow_governance" in notice
    assert "`approval_required`" in notice
    assert "`approver`" in notice
    for table in ("approvals", "review_attestations", "library_entries"):
        assert f"`{table}`" in notice, table

    # Exactly one ready-to-send notice, and no epoch numeral anywhere in the
    # section: numerals are I0's fan-out sites, not this section's.
    assert len(_fences(notice, "text")) == 1
    assert re.search(r"epoch \d", notice, flags=re.IGNORECASE) is None

    # The procedure's hand-back step sends the operator here.
    procedure = _RUNBOOK.read_text(encoding="utf-8").split("### Procedure", maxsplit=1)[1]
    assert _NOTICE_ANCHOR in procedure
    assert "On the 0.8.0 cutover, also settle" not in procedure


def test_operator_notice_says_re_activation_before_sign_in() -> None:
    """Spec :651-653: the notice says re-activation happened, not merely 'log in again', and names the secrets."""
    notice = _subsection(_current_cutover(), _NOTICE_HEADING)
    (text,) = _fences(notice, "text")
    lowered = text.lower()

    assert "re-activated" in lowered
    assert "sign in again" in lowered
    assert lowered.index("re-activated") < lowered.index("sign in again")
    assert "log in again" not in lowered
    assert "stored secrets" in lowered
    assert "re-enter" in lowered
    assert "shared library" in lowered
    assert "approvals" in lowered
    assert "account is pending" in lowered
    assert "<admin contact>" in text
    assert "<release>" in text


def test_current_cutover_routes_each_shipped_deployment_shape_to_its_recreate_procedure() -> None:
    """Every shape recreates; the withdrawn VM in-place rebuild (spec rev2.7, D21) has no row."""
    shapes = _subsection(_current_cutover(), _SHAPES_HEADING)
    rows = [line for line in shapes.splitlines() if line.startswith("| ")]
    assert len(rows) >= 6, "header, separator and four shape rows"

    def _row(label: str) -> str:
        matches = [row for row in rows if f"| {label} |" in row]
        assert len(matches) == 1, label
        return matches[0]

    ecs = _row("ECS (Fargate)")
    assert "aws-ecs-deployment.md" in ecs
    assert "rollback_permitted: false" in ecs
    assert "bootstrap-admin oidc" in ecs
    aca = _row("Azure Container Apps")
    assert "azure-container-apps-cold-install.md" in aca
    assert "azure-container-apps-existing-service-redeploy.md" in aca
    sqlite = _row("VM, SQLite")
    assert "#staging-reset-for-elspethexamplegovau" in sqlite
    assert "ansible-ubuntu-deployment.md" in sqlite
    postgres = _row("VM, external PostgreSQL")
    assert "ansible-ubuntu-deployment.md" in postgres
    assert "--init-schema" in postgres

    assert "in place" in shapes
    assert "withdrawn" in shapes
    assert re.search(r"epoch \d", shapes, flags=re.IGNORECASE) is None
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", shapes):
        assert (Path("docs/runbooks") / target).is_file(), target


def test_current_cutover_cites_only_admission_refusals_the_contract_defines() -> None:
    """The token-admission paragraph names the shipped ledger posture; every refusal it cites is a contract member."""
    cutover = _current_cutover()
    start = cutover.index("**Token admission after the recreate.**")
    paragraph = cutover[start:].split("\n\n", maxsplit=1)[0]

    cited = {"quota_policy_missing", "quota_exceeded", "token_accounting_unavailable"}
    assert cited <= set(re.findall(r"`([a-z_]+)`", paragraph))
    assert cited <= {reason.value for reason in AdmissionRefusalReason}
    assert "UTC" in paragraph
    assert "`NULL`" in paragraph
    assert "measures zero" in paragraph
    assert "complete accounting is not implemented" not in cutover
    assert "does not make chargeable work available" not in cutover
    assert "Token admission is already fail-closed" not in cutover


def test_every_deployment_runbook_points_at_the_operator_notice() -> None:
    """ECS, ACA and the VM runbook each send the operator to the notice from the step where it applies."""
    ecs = _ECS_RUNBOOK.read_text(encoding="utf-8")
    auth = ecs.split("## Authentication and secret injection", maxsplit=1)[1].split("\n### Real-browser OIDC evidence", maxsplit=1)[0]
    assert "staging-session-db-recreation.md" in auth
    assert "Operator notice for the workflow-epoch window" in auth
    assert "identity mapping" in auth
    assert "`approval_required`" in auth

    aca = _ACA_REDEPLOY_RUNBOOK.read_text(encoding="utf-8")
    prove = aca.split("## 7. Prove public behaviour and identity", maxsplit=1)[1].split("\n## Rollback", maxsplit=1)[0]
    assert "staging-session-db-recreation.md" in prove
    assert "Operator notice for the workflow-epoch window" in prove
    assert "azure-container-apps-cold-install.md" in prove
    assert "image-only" in prove

    vm = _VM_RUNBOOK.read_text(encoding="utf-8")
    compatibility = vm.split("## Release compatibility", maxsplit=1)[1].split("\n## Install the immutable release", maxsplit=1)[0]
    assert "0.7.1→0.8.0" not in compatibility
    assert "staging-session-db-recreation.md" in compatibility
    assert "Operator notice for the workflow-epoch window" in compatibility
    assert "in-place rebuild" in compatibility
    assert "Initialize external schemas once" in compatibility


def test_changelog_states_the_session_invalidation_and_re_activation() -> None:
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    release_0_8_1 = changelog.split("## 0.8.1 -", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    normalized = " ".join(release_0_8_1.split())

    assert "Every signed-in session is refused after the recreate" in normalized
    assert "re-activating the cohort" in normalized
    assert "re-enter their stored secrets" in normalized
    assert "Operator notice for the workflow-epoch window" in normalized
```

Then create `tests/unit/web/auth/test_cutover_identity_restoration.py`. A text
contract cannot prove what re-admission restores, so this file runs the
runbook's SQLite export statements exactly as written against a real
pre-window sessions store and drives the documented steps through the real
admin routes against a recreated one; step 1 is `_build`'s own
`bootstrap_admin` of `root`. It imports the admin-route harness rather than
copying it, and its client passes `raise_app_exceptions=False` so the HTTP 500
of a grant that lapses during the call reaches the driver as an operator sees
it. `_build` needs its directory to exist, hence `_store_directory`:

```python
"""Cutover re-admission: each identity once, and only the grants still active (I11).

The session DB reset runbook's SQLite export is executed VERBATIM against a
real pre-window sessions store, and its re-admission steps are driven through
the real identity administration routes against a freshly recreated one. What
is pinned is the operator's outcome, read back through
``GET /api/auth/admin/roles``, ``GET /api/auth/admin/relationships`` and the
authority itself: which identities exist, which grants they hold with which
``scope`` and ``expires_at``, and which approver edges join them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from elspeth.contracts.auth import IdentityRole
from elspeth.web.coordination.identity_authority import _WORKLOAD_ROLES, IdentityAdminActor
from elspeth.web.sessions.models import identities_table, identity_roles_table

from .test_identity_admin_routes import _bearer, _build, _Harness

pytestmark = pytest.mark.asyncio

_RUNBOOK = Path("docs/runbooks/staging-session-db-recreation.md")
_NOTICE_HEADING = "### Operator notice for the workflow-epoch window"
_IDENTITY_FILE = "identity-mapping.pre.csv"
_GRANT_FILE = "identity-grants.pre.csv"
_RELATIONSHIP_FILE = "identity-relationships.pre.csv"
# Centuries from any test clock in both directions, so no case depends on timing.
_FUTURE = datetime(2999, 6, 30, 12, 0, 0, 123456, tzinfo=UTC)
_LATER_FUTURE = datetime(2999, 12, 31, 23, 59, 59, 654321, tzinfo=UTC)
_PAST = datetime(2001, 1, 1, tzinfo=UTC)
_NOTE = "cutover re-admission"
# The roles the runbook names as never held beside ``admin`` (R8).
_RUNBOOK_WORKLOAD_ROLES = frozenset({"user", "approver", "reviewer", "curator"})
# One runbook sentence per decision ``_restore`` takes, as an operator reads it
# (whitespace collapsed). A prose edit that changes a rule removes its sentence,
# so every test here goes red before it restores anything.
_RULES = (
    "For every other row of `identity-mapping.pre.csv` with `kind` `human` and `access_state` `active`, call `POST /api/auth/admin/identities` once",
    '`"role": "none"`',
    "The role is always `none`",
    "For every row of `identity-grants.pre.csv` whose identity step 1 or step 2 restored",
    "omit `scope` or `expires_at` when the cell is empty",
    "At or before now: make no call and record the grant `lapsed`.",
    "confirm no row was written, and record that grant `lapsed` too.",
    "Skip the bootstrap administrator's unscoped `admin` row: step 1 restored it.",
    "A newly appointed administrator's `user`, `approver`, `reviewer` or `curator` grant that is still active: make no call and record it `not_restored`.",
)
# The same pin for each decision ``_restore_edges`` takes (re-admission step 4).
_EDGE_RULES = (
    "For every row of `identity-relationships.pre.csv`",
    "Both ends restored and an `approver` grant of the `from` identity recorded `restored` in step 3: call `POST /api/auth/admin/relationships`",
    "omit `effective_from` or `effective_until` when the cell is empty",
    "Either end not restored, or no `approver` grant of the `from` identity recorded `restored`: make no call and record the edge `not_restored`.",
    "refuses with 409 `approver_role_required` and writes nothing.",
    "confirm no edge was written, and record that edge `not_restored` too.",
)


def _notice() -> str:
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    cutover = runbook.split("## Current Cutover:", maxsplit=1)[1].split("## Historical Cutover:", maxsplit=1)[0]
    assert _NOTICE_HEADING in cutover, _NOTICE_HEADING
    return cutover.split(f"{_NOTICE_HEADING}\n", maxsplit=1)[1].split("\n### ", maxsplit=1)[0]


def _sqlite_export_statements() -> dict[str, str]:
    """The runbook's SQLite export statements, keyed by the file each one writes.

    ``[^"]*`` cannot run past a statement's closing quote: the statements carry
    no double quote, so a missing or reshaped statement fails the count below
    instead of being swallowed into its neighbour.
    """
    found = re.findall(r'sqlite3 -header -csv "\$DB_PATH" "([^"]*)" \| sudo tee "\$SNAPSHOT_DIR/([a-z.-]+)"', _notice())
    statements = {target: sql for sql, target in found}
    assert len(found) == 3, found
    assert set(statements) == {_IDENTITY_FILE, _GRANT_FILE, _RELATIONSHIP_FILE}, sorted(statements)
    return statements


def _export(harness: _Harness, sql: str) -> list[dict[str, str]]:
    """Rows as ``sqlite3 -header -csv`` writes them: every value text, NULL as an empty field."""
    with harness.engine.connect() as conn:
        result = conn.exec_driver_sql(sql)
        columns = list(result.keys())
        return [{column: "" if value is None else str(value) for column, value in zip(columns, row, strict=True)} for row in result.all()]


def _store_directory(tmp_path: Path, window_side: str) -> Path:
    """The pre-window and recreated stores live side by side, never in one file."""
    directory = tmp_path / window_side
    directory.mkdir()
    return directory


def _admin_actor(harness: _Harness) -> IdentityAdminActor:
    return IdentityAdminActor(identity_id=harness.root_identity_id, on_behalf_of=None, console_request_id=None)


def _pre_provision(harness: _Harness, subject: str) -> str:
    event = harness.authority.pre_provision_identity(
        actor=_admin_actor(harness),
        provider="local",
        subject=subject,
        username=None,
        organisation_id=None,
        role="none",
        note="pre-window cohort",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=lambda _event: None,
    )
    return event.record.identity_id


def _grant(harness: _Harness, identity_id: str, role: IdentityRole, *, scope: str | None = None, expires_at: datetime | None = None) -> str:
    grant = harness.authority.grant_role(
        actor=_admin_actor(harness),
        identity_id=identity_id,
        role=role,
        scope=scope,
        expires_at=expires_at,
        note="pre-window grant",
        record=lambda _event: None,
    )
    return grant.role_id


def _set_expiry(harness: _Harness, role_id: str, expires_at: datetime) -> None:
    """Rewrite a stored grant's expiry: no route writes a past one, and bootstrap writes none."""
    with harness.engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == role_id).values(expires_at=expires_at))


def _assert_edge(
    harness: _Harness,
    from_identity_id: str,
    to_identity_id: str,
    *,
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
) -> str:
    edge = harness.authority.assert_relationship(
        actor=_admin_actor(harness),
        from_identity_id=from_identity_id,
        to_identity_id=to_identity_id,
        relationship_type="approver",
        effective_from=effective_from,
        effective_until=effective_until,
        note="pre-window edge",
        record=lambda _event: None,
    )
    return edge.relationship_id


def _re_pend(harness: _Harness, identity_id: str) -> None:
    """The state an R9 dormancy re-pend leaves: ``pending``, with the identity's edges NOT revoked."""
    with harness.engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == identity_id).values(access_state="pending"))


def _root_admin_role_id(harness: _Harness) -> str:
    (grant,) = [grant for grant in harness.authority.active_roles(identity_id=harness.root_identity_id) if grant.role == "admin"]
    return grant.role_id


def _row(rows: list[dict[str, str]], subject: str) -> dict[str, str]:
    (match,) = [row for row in rows if row["subject"] == subject]
    return match


@dataclass(frozen=True)
class _Restoration:
    identity_ids: dict[str, str]
    """``pre_cutover_identity_id`` -> the restored ``identity_id``."""
    outcomes: dict[tuple[str, str, str], str]
    """``(subject, role, scope)`` -> ``bootstrap`` / ``restored`` / ``lapsed`` / ``not_restored``."""
    identity_calls: int
    refused_as_lapsed: tuple[tuple[str, str, str], ...]


async def _roles(client: AsyncClient, headers: dict[str, str], identity_id: str) -> set[tuple[str, str | None, datetime | None]]:
    response = await client.get("/api/auth/admin/roles", headers=headers, params={"identity_id": identity_id})
    assert response.status_code == 200, response.text
    return {
        (role["role"], role["scope"], None if role["expires_at"] is None else datetime.fromisoformat(role["expires_at"]))
        for role in response.json()["roles"]
    }


async def _restore(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    identities: list[dict[str, str]],
    grants: list[dict[str, str]],
    bootstrap: dict[str, str],
    bootstrap_identity_id: str,
    now: datetime,
    new_appointment: bool,
) -> _Restoration:
    """Steps 2 and 3 of the runbook's re-admission, as written; step 1 is ``_build``'s own bootstrap.

    ``new_appointment`` is step 1's last branch: every pre-window administrator
    grant has lapsed and the deployment owner named the identity bootstrapped.
    """
    prose = " ".join(_notice().split())
    assert [rule for rule in _RULES if rule not in prose] == []
    restored = {bootstrap["pre_cutover_identity_id"]: bootstrap_identity_id}
    identity_calls = 0
    for row in identities:
        if row["pre_cutover_identity_id"] == bootstrap["pre_cutover_identity_id"]:
            continue
        if row["kind"] != "human" or row["access_state"] != "active":
            continue
        response = await client.post(
            "/api/auth/admin/identities",
            headers=headers,
            json={"provider": row["provider"], "subject": row["subject"], "role": "none", "note": _NOTE},
        )
        assert response.status_code == 201, response.text
        assert response.json()["role"] is None
        identity_calls += 1
        restored[row["pre_cutover_identity_id"]] = response.json()["identity"]["identity_id"]

    outcomes: dict[tuple[str, str, str], str] = {}
    refused: list[tuple[str, str, str]] = []
    for row in grants:
        key = (row["subject"], row["role"], row["scope"])
        assert key not in outcomes, key
        if row["pre_cutover_identity_id"] not in restored:
            outcomes[key] = "not_restored"
            continue
        identity_id = restored[row["pre_cutover_identity_id"]]
        is_bootstrap = identity_id == bootstrap_identity_id
        if is_bootstrap and not new_appointment and row["role"] == "admin" and row["scope"] == "":
            outcomes[key] = "bootstrap"
            continue
        if row["expires_at"] != "" and datetime.fromisoformat(row["expires_at"]) <= now:
            outcomes[key] = "lapsed"
            continue
        if is_bootstrap and new_appointment and row["role"] in _RUNBOOK_WORKLOAD_ROLES:
            outcomes[key] = "not_restored"
            continue
        body: dict[str, str] = {"identity_id": identity_id, "role": row["role"], "note": _NOTE}
        if row["scope"] != "":
            body["scope"] = row["scope"]
        if row["expires_at"] != "":
            body["expires_at"] = row["expires_at"]
        response = await client.post("/api/auth/admin/roles", headers=headers, json=body)
        if response.status_code == 201:
            outcomes[key] = "restored"
            continue
        # The runbook's lapse-during-the-call branch: refused as a 500, nothing written.
        assert response.status_code == 500, response.text
        held = await _roles(client, headers, identity_id)
        assert all(role != row["role"] or (scope or "") != row["scope"] for role, scope, _expiry in held), held
        outcomes[key] = "lapsed"
        refused.append(key)
    return _Restoration(identity_ids=restored, outcomes=outcomes, identity_calls=identity_calls, refused_as_lapsed=tuple(refused))


@dataclass(frozen=True)
class _EdgeRestoration:
    outcomes: dict[tuple[str, str], str]
    """``(from_subject, to_subject)`` -> ``restored`` / ``not_restored``."""
    relationship_ids: dict[tuple[str, str], str]
    refused_as_lapsed: tuple[tuple[str, str], ...]


async def _restore_edges(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    edges: list[dict[str, str]],
    grants: list[dict[str, str]],
    restoration: _Restoration,
) -> _EdgeRestoration:
    """Step 4 of the runbook's re-admission, as written, after ``_restore`` ran steps 2 and 3."""
    prose = " ".join(_notice().split())
    assert [rule for rule in _EDGE_RULES if rule not in prose] == []
    approvers = {
        row["pre_cutover_identity_id"]
        for row in grants
        if row["role"] == "approver" and restoration.outcomes[(row["subject"], row["role"], row["scope"])] == "restored"
    }
    outcomes: dict[tuple[str, str], str] = {}
    relationship_ids: dict[tuple[str, str], str] = {}
    refused: list[tuple[str, str]] = []
    for row in edges:
        key = (row["from_subject"], row["to_subject"])
        assert key not in outcomes, key
        from_pre = row["from_pre_cutover_identity_id"]
        to_pre = row["to_pre_cutover_identity_id"]
        if from_pre not in restoration.identity_ids or to_pre not in restoration.identity_ids or from_pre not in approvers:
            outcomes[key] = "not_restored"
            continue
        to_identity_id = restoration.identity_ids[to_pre]
        body: dict[str, str] = {
            "from_identity_id": restoration.identity_ids[from_pre],
            "to_identity_id": to_identity_id,
            "relationship_type": row["relationship_type"],
            "note": _NOTE,
        }
        if row["effective_from"] != "":
            body["effective_from"] = row["effective_from"]
        if row["effective_until"] != "":
            body["effective_until"] = row["effective_until"]
        response = await client.post("/api/auth/admin/relationships", headers=headers, json=body)
        if response.status_code == 201:
            outcomes[key] = "restored"
            relationship_ids[key] = response.json()["relationship_id"]
            continue
        # The runbook's lapse-after-step-3 branch: a typed refusal, nothing written.
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["refusal"] == "approver_role_required", response.text
        listed = await client.get("/api/auth/admin/relationships", headers=headers, params={"identity_id": to_identity_id})
        assert listed.status_code == 200, listed.text
        assert all(edge["to_identity_id"] != to_identity_id for edge in listed.json()["relationships"]), listed.text
        outcomes[key] = "not_restored"
        refused.append(key)
    return _EdgeRestoration(outcomes=outcomes, relationship_ids=relationship_ids, refused_as_lapsed=tuple(refused))


def _client_seeing_server_errors(harness: _Harness) -> AsyncClient:
    """An operator sees a 500 as a response, so the transport must not re-raise it."""
    return AsyncClient(transport=ASGITransport(app=harness.app, raise_app_exceptions=False), base_url="http://test")


async def test_cutover_restores_each_identity_once_and_only_grants_active_at_restoration(tmp_path: Path) -> None:
    pre = _build(_store_directory(tmp_path, "pre"))
    alice = _pre_provision(pre, "alice")
    bob = _pre_provision(pre, "bob")
    carol = _pre_provision(pre, "carol")
    # The bootstrap administrator also holds a future-expiring grant of its own.
    _grant(pre, pre.root_identity_id, "auditor", expires_at=_FUTURE)
    # One identity, several grants: unexpiring, scoped and future-expiring, expired but never revoked.
    _grant(pre, alice, "approver")
    _grant(pre, alice, "reviewer", scope="lib-1", expires_at=_FUTURE)
    _set_expiry(pre, _grant(pre, alice, "curator", expires_at=_FUTURE), _PAST)
    # Revoked: never exported, never restored.
    pre.authority.revoke_role(
        actor=_admin_actor(pre), role_id=_grant(pre, bob, "approver"), note="left the panel", record=lambda _event: None
    )
    _grant(pre, bob, "user")
    # A disabled identity keeps its grant row; neither comes back.
    _grant(pre, carol, "approver")
    pre.authority.disable_identity(actor=_admin_actor(pre), identity_id=carol, reason="left the organisation", record=lambda _event: None)

    statements = _sqlite_export_statements()
    identities = _export(pre, statements[_IDENTITY_FILE])
    grants = _export(pre, statements[_GRANT_FILE])

    assert list(identities[0]) == ["provider", "subject", "pre_cutover_identity_id", "kind", "access_state"]
    assert sorted(row["subject"] for row in identities) == ["alice", "bob", "carol", "root"]
    assert list(grants[0]) == ["provider", "subject", "pre_cutover_identity_id", "role", "scope", "expires_at"]
    assert len(grants) == 7
    assert {(row["subject"], row["role"], row["scope"], row["expires_at"]) for row in grants} == {
        ("root", "admin", "", ""),
        ("root", "auditor", "", "2999-06-30T12:00:00.123456Z"),
        ("alice", "approver", "", ""),
        ("alice", "reviewer", "lib-1", "2999-06-30T12:00:00.123456Z"),
        ("alice", "curator", "", "2001-01-01T00:00:00.000000Z"),
        ("bob", "user", "", ""),
        ("carol", "approver", "", ""),
    }
    # Step 1's choice: the one human, active identity with an unscoped, unexpiring admin grant.
    unexpiring_admins = {row["subject"] for row in grants if row["role"] == "admin" and row["scope"] == "" and row["expires_at"] == ""}
    assert unexpiring_admins == {"root"}
    bootstrap = _row(identities, "root")

    post = _build(_store_directory(tmp_path, "post"))
    async with _client_seeing_server_errors(post) as client:
        headers = await _bearer(client, "root")
        restoration = await _restore(
            client,
            headers,
            identities=identities,
            grants=grants,
            bootstrap=bootstrap,
            bootstrap_identity_id=post.root_identity_id,
            now=datetime.now(UTC),
            new_appointment=False,
        )
        active = await client.get("/api/auth/admin/identities", headers=headers, params={"access_state": "active"})
        assert active.status_code == 200, active.text
        assert sorted(view["subject"] for view in active.json()["identities"]) == ["alice", "bob", "root"]
        restored_alice = restoration.identity_ids[alice]
        restored_bob = restoration.identity_ids[bob]
        assert await _roles(client, headers, restored_alice) == {("approver", None, None), ("reviewer", "lib-1", _FUTURE)}
        assert await _roles(client, headers, restored_bob) == {("user", None, None)}
        assert await _roles(client, headers, post.root_identity_id) == {("admin", None, None), ("auditor", None, _FUTURE)}

    assert restoration.identity_calls == 2
    assert restoration.refused_as_lapsed == ()
    assert restoration.outcomes == {
        ("root", "admin", ""): "bootstrap",
        ("root", "auditor", ""): "restored",
        ("alice", "approver", ""): "restored",
        ("alice", "reviewer", "lib-1"): "restored",
        ("alice", "curator", ""): "lapsed",
        ("bob", "user", ""): "restored",
        ("carol", "approver", ""): "not_restored",
    }
    assert post.authority.read_identity_by_natural_key(provider="local", subject="carol") is None
    # The authority's own reading agrees: the expiry restored is the one enforcement sees.
    assert {(grant.role, grant.scope, grant.expires_at) for grant in post.authority.active_roles(identity_id=restored_alice)} == {
        ("approver", None, None),
        ("reviewer", "lib-1", _FUTURE),
    }
    assert post.authority.holds_active_role(identity_id=restored_alice, role="curator") is False


async def test_a_grant_that_lapses_between_the_comparison_and_the_call_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    pre = _build(_store_directory(tmp_path, "pre"))
    alice = _pre_provision(pre, "alice")
    _set_expiry(pre, _grant(pre, alice, "reviewer", expires_at=_FUTURE), _PAST)
    statements = _sqlite_export_statements()
    identities = _export(pre, statements[_IDENTITY_FILE])
    grants = _export(pre, statements[_GRANT_FILE])

    post = _build(_store_directory(tmp_path, "post"))
    async with _client_seeing_server_errors(post) as client:
        headers = await _bearer(client, "root")
        # An operator comparison dated before the expiry stands in for a grant
        # that was still active when compared and had lapsed by the call.
        restoration = await _restore(
            client,
            headers,
            identities=identities,
            grants=grants,
            bootstrap=_row(identities, "root"),
            bootstrap_identity_id=post.root_identity_id,
            now=datetime(2000, 12, 31, tzinfo=UTC),
            new_appointment=False,
        )
        assert await _roles(client, headers, restoration.identity_ids[alice]) == set()

    assert restoration.refused_as_lapsed == (("alice", "reviewer", ""),)
    assert restoration.outcomes[("alice", "reviewer", "")] == "lapsed"


async def test_an_expiring_bootstrap_administrator_gets_its_expiry_back_from_a_second_administrator(tmp_path: Path) -> None:
    pre = _build(_store_directory(tmp_path, "pre"))
    alice = _pre_provision(pre, "alice")
    _grant(pre, alice, "admin", expires_at=_FUTURE)
    # No pre-window administrator grant is unexpiring, and root's expires last.
    _set_expiry(pre, _root_admin_role_id(pre), _LATER_FUTURE)
    statements = _sqlite_export_statements()
    identities = _export(pre, statements[_IDENTITY_FILE])
    grants = _export(pre, statements[_GRANT_FILE])
    admin_grants = [row for row in grants if row["role"] == "admin" and row["scope"] == ""]
    assert all(row["expires_at"] != "" for row in admin_grants)
    chosen = max(admin_grants, key=lambda row: datetime.fromisoformat(row["expires_at"]))
    assert chosen["subject"] == "root"

    post = _build(_store_directory(tmp_path, "post"))
    async with _client_seeing_server_errors(post) as client:
        root_headers = await _bearer(client, "root")
        restoration = await _restore(
            client,
            root_headers,
            identities=identities,
            grants=grants,
            bootstrap=_row(identities, "root"),
            bootstrap_identity_id=post.root_identity_id,
            now=datetime.now(UTC),
            new_appointment=False,
        )
        # The bootstrap grant does not expire although the pre-window one did.
        assert await _roles(client, root_headers, post.root_identity_id) == {("admin", None, None)}

        alice_headers = await _bearer(client, "alice")
        listed = await client.get("/api/auth/admin/roles", headers=alice_headers, params={"identity_id": post.root_identity_id})
        assert listed.status_code == 200, listed.text
        (bootstrap_grant,) = listed.json()["roles"]
        revoked = await client.post(
            f"/api/auth/admin/roles/{bootstrap_grant['role_id']}/revoke",
            headers=alice_headers,
            json={"note": "restore the pre-window expiry"},
        )
        assert revoked.status_code == 200, revoked.text
        regranted = await client.post(
            "/api/auth/admin/roles",
            headers=alice_headers,
            json={"identity_id": post.root_identity_id, "role": "admin", "expires_at": chosen["expires_at"], "note": _NOTE},
        )
        assert regranted.status_code == 201, regranted.text
        assert await _roles(client, alice_headers, post.root_identity_id) == {("admin", None, _LATER_FUTURE)}
        assert await _roles(client, alice_headers, restoration.identity_ids[alice]) == {("admin", None, _FUTURE)}

    assert restoration.outcomes == {("alice", "admin", ""): "restored", ("root", "admin", ""): "bootstrap"}
    assert post.authority.count_active_human_admins() == 2


async def test_a_newly_appointed_administrator_is_not_given_back_a_workload_grant_admin_excludes(tmp_path: Path) -> None:
    pre = _build(_store_directory(tmp_path, "pre"))
    alice = _pre_provision(pre, "alice")
    alice_admin = _grant(pre, alice, "admin", expires_at=_FUTURE)
    # root's administration lapses; alice, still an administrator, then gives root a
    # workload grant and a future-expiring auditor grant, which R8 allows because
    # root no longer holds an active admin grant.
    _set_expiry(pre, _root_admin_role_id(pre), _PAST)
    alice_actor = IdentityAdminActor(identity_id=alice, on_behalf_of=None, console_request_id=None)
    pre.authority.grant_role(
        actor=alice_actor,
        identity_id=pre.root_identity_id,
        role="approver",
        scope=None,
        expires_at=None,
        note="pre-window grant",
        record=lambda _event: None,
    )
    pre.authority.grant_role(
        actor=alice_actor,
        identity_id=pre.root_identity_id,
        role="auditor",
        scope=None,
        expires_at=_FUTURE,
        note="pre-window grant",
        record=lambda _event: None,
    )
    # Then alice's administration lapses too: no administrator is left to restore.
    _set_expiry(pre, alice_admin, _PAST)
    statements = _sqlite_export_statements()
    identities = _export(pre, statements[_IDENTITY_FILE])
    grants = _export(pre, statements[_GRANT_FILE])
    now = datetime.now(UTC)
    admin_grants = [row for row in grants if row["role"] == "admin" and row["scope"] == ""]
    assert sorted(row["subject"] for row in admin_grants) == ["alice", "root"]
    assert all(row["expires_at"] != "" and datetime.fromisoformat(row["expires_at"]) <= now for row in admin_grants)
    # The runbook's four names are the authority's own R8 set.
    assert _RUNBOOK_WORKLOAD_ROLES == _WORKLOAD_ROLES

    # The deployment owner names root; _build's bootstrap is that new appointment.
    post = _build(_store_directory(tmp_path, "post"))
    async with _client_seeing_server_errors(post) as client:
        headers = await _bearer(client, "root")
        restoration = await _restore(
            client,
            headers,
            identities=identities,
            grants=grants,
            bootstrap=_row(identities, "root"),
            bootstrap_identity_id=post.root_identity_id,
            now=now,
            new_appointment=True,
        )
        assert await _roles(client, headers, post.root_identity_id) == {("admin", None, None), ("auditor", None, _FUTURE)}
        assert await _roles(client, headers, restoration.identity_ids[alice]) == set()
        # Why the approver grant is not restored: the real API refuses it beside admin.
        refused = await client.post(
            "/api/auth/admin/roles", headers=headers, json={"identity_id": post.root_identity_id, "role": "approver", "note": _NOTE}
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["refusal"] == "role_forbidden_for_identity"

    assert restoration.identity_calls == 1
    assert restoration.refused_as_lapsed == ()
    assert restoration.outcomes == {
        ("alice", "admin", ""): "lapsed",
        ("root", "admin", ""): "lapsed",
        ("root", "approver", ""): "not_restored",
        ("root", "auditor", ""): "restored",
    }


async def test_cutover_restores_an_edge_only_when_both_ends_and_an_approver_grant_came_back(tmp_path: Path) -> None:
    pre = _build(_store_directory(tmp_path, "pre"))
    alice = _pre_provision(pre, "alice")
    bob = _pre_provision(pre, "bob")
    carol = _pre_provision(pre, "carol")
    dave = _pre_provision(pre, "dave")
    erin = _pre_provision(pre, "erin")
    frank = _pre_provision(pre, "frank")
    gina = _pre_provision(pre, "gina")
    _grant(pre, alice, "approver")
    # Restored, with its annotation window carried exactly as exported.
    _assert_edge(pre, alice, bob, effective_from=_PAST, effective_until=_FUTURE)
    # Revoked: never exported, never restored.
    pre.authority.revoke_relationship(
        actor=_admin_actor(pre), relationship_id=_assert_edge(pre, alice, carol), note="reorganised", record=lambda _event: None
    )
    # The overseeing approver's grant lapsed before the window: exported, not restored.
    erin_approver = _grant(pre, erin, "approver", expires_at=_FUTURE)
    _assert_edge(pre, erin, dave)
    _set_expiry(pre, erin_approver, _PAST)
    # frank's grant comes back in step 3 and lapses before step 4 reaches his edge.
    _grant(pre, frank, "approver", expires_at=_FUTURE)
    _assert_edge(pre, frank, alice)
    # A dormancy re-pend keeps the edge; its pending end is not restored.
    _assert_edge(pre, alice, gina)
    _re_pend(pre, gina)

    statements = _sqlite_export_statements()
    identities = _export(pre, statements[_IDENTITY_FILE])
    grants = _export(pre, statements[_GRANT_FILE])
    edges = _export(pre, statements[_RELATIONSHIP_FILE])

    assert list(edges[0]) == [
        "from_provider",
        "from_subject",
        "from_pre_cutover_identity_id",
        "to_provider",
        "to_subject",
        "to_pre_cutover_identity_id",
        "relationship_type",
        "effective_from",
        "effective_until",
    ]
    assert len(edges) == 4
    assert {
        (row["from_subject"], row["to_subject"], row["relationship_type"], row["effective_from"], row["effective_until"]) for row in edges
    } == {
        ("alice", "bob", "approver", "2001-01-01T00:00:00.000000Z", "2999-06-30T12:00:00.123456Z"),
        ("erin", "dave", "approver", "", ""),
        ("frank", "alice", "approver", "", ""),
        ("alice", "gina", "approver", "", ""),
    }
    assert {(row["from_pre_cutover_identity_id"], row["to_pre_cutover_identity_id"]) for row in edges} == {
        (alice, bob),
        (erin, dave),
        (frank, alice),
        (alice, gina),
    }
    assert _row(identities, "gina")["access_state"] == "pending"

    post = _build(_store_directory(tmp_path, "post"))
    async with _client_seeing_server_errors(post) as client:
        headers = await _bearer(client, "root")
        restoration = await _restore(
            client,
            headers,
            identities=identities,
            grants=grants,
            bootstrap=_row(identities, "root"),
            bootstrap_identity_id=post.root_identity_id,
            now=datetime.now(UTC),
            new_appointment=False,
        )
        assert restoration.outcomes[("erin", "approver", "")] == "lapsed"
        assert restoration.outcomes[("frank", "approver", "")] == "restored"
        (frank_approver,) = [
            grant for grant in post.authority.active_roles(identity_id=restoration.identity_ids[frank]) if grant.role == "approver"
        ]
        _set_expiry(post, frank_approver.role_id, _PAST)

        edge_restoration = await _restore_edges(client, headers, edges=edges, grants=grants, restoration=restoration)

        listed = await client.get("/api/auth/admin/relationships", headers=headers)
        assert listed.status_code == 200, listed.text
        (restored_edge,) = listed.json()["relationships"]
        assert (restored_edge["from_identity_id"], restored_edge["to_identity_id"], restored_edge["relationship_type"]) == (
            restoration.identity_ids[alice],
            restoration.identity_ids[bob],
            "approver",
        )
        assert datetime.fromisoformat(restored_edge["effective_from"]) == _PAST
        assert datetime.fromisoformat(restored_edge["effective_until"]) == _FUTURE
        assert restored_edge["relationship_id"] == edge_restoration.relationship_ids[("alice", "bob")]
        assert restored_edge["asserted_by_identity_id"] == post.root_identity_id
        # Why erin's edge is not restored: the real API refuses it without an active approver grant.
        refused = await client.post(
            "/api/auth/admin/relationships",
            headers=headers,
            json={
                "from_identity_id": restoration.identity_ids[erin],
                "to_identity_id": restoration.identity_ids[dave],
                "relationship_type": "approver",
                "note": _NOTE,
            },
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["refusal"] == "approver_role_required"

    assert gina not in restoration.identity_ids
    assert edge_restoration.refused_as_lapsed == (("frank", "alice"),)
    assert edge_restoration.outcomes == {
        ("alice", "bob"): "restored",
        ("alice", "gina"): "not_restored",
        ("erin", "dave"): "not_restored",
        ("frank", "alice"): "not_restored",
    }
```

- [ ] **Step 2: Run the policy tests and the restoration tests to verify the eleven new ones fail for the right reason.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/docs/test_staging_session_recreation_policy.py -n 0 > /tmp/i11-docs-red.log 2>&1; echo exit=$?
```

Expected: `exit=1` with `6 failed, 3 passed`. The three HEAD tests keep
passing (I0 already fanned the numeral out).
`test_current_cutover_carries_the_workflow_epoch_operator_notice`,
`test_operator_notice_says_re_activation_before_sign_in` and
`test_current_cutover_routes_each_shipped_deployment_shape_to_its_recreate_procedure`
fail in `_subsection` with `AssertionError: ### Operator notice for the
workflow-epoch window` / `### Cutover by deployment shape` (the heading is
absent);
`test_current_cutover_cites_only_admission_refusals_the_contract_defines`
fails with `ValueError: substring not found` at
`cutover.index("**Token admission after the recreate.**")` (the paragraph
still carries its HEAD lead-in);
`test_every_deployment_runbook_points_at_the_operator_notice` fails at
`assert "staging-session-db-recreation.md" in auth` (the ECS §Authentication
section links only the Identity Providers guide);
`test_changelog_states_the_session_invalidation_and_re_activation` fails at
`assert "Every signed-in session is refused after the recreate" in normalized`.
If instead the file errors at import on `AdmissionRefusalReason`, stop: the
contract module moved and the Consumes block above is stale.

Then run the restoration tests against the unedited runbook:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/auth/test_cutover_identity_restoration.py -n 0 > /tmp/i11-restore-red.log 2>&1; echo exit=$?
```

Expected: `exit=1` with `5 failed`. Each test seeds its pre-window store and
then fails in `_notice()` at `assert _NOTICE_HEADING in cutover` with
`AssertionError: ### Operator notice for the workflow-epoch window` (the
heading is absent until Step 4; measured 2026-09-15 against the HEAD
runbook). An `ImportError` naming `_build`, `_bearer` or `_Harness` means a
sibling task renamed a helper in `test_identity_admin_routes.py`: stop and
import the renamed helper; do not copy the harness.

- [ ] **Step 3: Confirm the refusal vocabulary the token-admission paragraph will cite against the landed contract.**

The paragraph in Step 4 names three refusals. Two are on HEAD; the third,
`quota_exceeded`, is I1's `AdmissionRefusalReason.QUOTA_EXCEEDED`. Prove all
three exist before writing them into an operator document:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python - <<'PY'
from elspeth.contracts.chargeable_admission import AdmissionRefusalReason
members = sorted(reason.value for reason in AdmissionRefusalReason)
print(members)
assert {"quota_policy_missing", "quota_exceeded", "token_accounting_unavailable"} <= set(members)
print("ok")
PY
```

Expected: the list, then `ok`. Then read which member I1's landed test
asserts for a day whose ledger total is unknown:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_quota_authority.py -n 0 -k "unknown" -v > /tmp/i11-i1-unknown.log 2>&1; echo exit=$?
```

Expected: `exit=0`. `exit=5` means no test name matched `unknown` — that is
not a failure; open the file and find the test that inserts a ledger row
with `prompt_tokens=None` (I1's "NULL means unknown" test) and read it
instead. Open `tests/unit/web/coordination/test_quota_authority.py`
at the test the log names and read the `AdmissionRefusalReason` member its
`match=` pattern (or its evidence assertion) pins for the unknown-total day. The paragraph below writes `token_accounting_unavailable`
for that case, which is the HEAD member for "the total cannot be measured";
if I1 landed a different member for it, write THAT member into the
paragraph and into the `cited` set of
`test_current_cutover_cites_only_admission_refusals_the_contract_defines`.
The doc follows the code, never the reverse.

- [ ] **Step 4: Edit the session DB reset runbook — the token-admission paragraph, the two new subsections, the hand-back sentence.**

(a) Replace `docs/runbooks/staging-session-db-recreation.md:214-225` (from
`**Token admission is already fail-closed.**` through `shortcut.`) with:

```markdown
**Token admission after the recreate.** Activation and pre-provisioning write
an identity's `quota_policies` row from the container defaults when both
`quota_default_tokens_per_day` and `quota_default_storage_bytes` are
configured, and the `token_usage_ledger` starts empty: a day with no charged
work measures zero, which is a measurement, not an unknown. A configured
`quota_default_tokens_per_day` still requires an active identity policy and a
configured `quota_container_tokens_per_day` an active container policy; a
missing required slot refuses chargeable work with `quota_policy_missing`. An
applicable active policy admits work while the identity's UTC-day ledger total
stays under `tokens_per_day` and refuses with `quota_exceeded` at the cap; the
day rolls over at UTC midnight, not at the deployment's local midnight. A
ledger row whose usage is unknown (`NULL`, never zero) makes the day's total
unknown, and an unknown total refuses with `token_accounting_unavailable`
rather than admitting. Only an active identity with no configured token-policy
requirements and no applicable active policy receives the explicit no-quota
allowance. Keep the deployment's intended policy posture; do not remove
policies as a recovery shortcut.
```

(b) Insert the two subsections after :262 (`independent Secrets Manager
binding — and is unaffected by this boundary.`) and its following blank line,
before `## Historical Cutover: 0.7.0 (two-DB reset)`:

````markdown
### Operator notice for the workflow-epoch window

Every signed-in session is refused from the first request after the recreate,
local and SSO alike, and it is not a logout. A session token's `sub` is the
`identity_id` (`src/elspeth/web/auth/session_token.py`), and `authenticate`
— which `refresh` also calls, so a refresh chain cannot outlive it — confirms
on every request that the identity is still `active` in the sessions store.
The recreated store holds no identities, so every token minted before the
window fails that check with HTTP 401 `Invalid token`. Users are unknown, not
signed out, until an operator re-admits them; that is why re-admission is a
step of this window and not the morning after.

**Export the identity mapping before the drop.** Pre-window audit evidence
names identities the recreate discards: `principal_scope` inside every
`WebPluginPolicyEvidence` hash and the `identity_id` on every `auth_events`
row. Re-provisioning mints new ids, so the only key from that evidence to the
people it describes is a mapping you export now, from the STOPPED live store,
before deleting or dropping it — never by reopening the archive, which is
evidence. The export is three files, because an identity, its grants and its
approver edges are restored by different calls: `identity-mapping.pre.csv`
holds one row per identity, `identity-grants.pre.csv` one row per unrevoked
grant with its `scope` and `expires_at`, and `identity-relationships.pre.csv`
one row per unrevoked `approver` edge (`identity_relationships`) with both
ends and its `effective_from` / `effective_until` annotation. A revoked grant
or edge is not exported and is never restored; it stays in the archive.
Disabling an identity revoked its edges, so they are not exported; a dormancy
re-pend did not, so an edge with a `pending` end is. An expired grant IS
exported, and so is an edge whose overseeing approver's grant has expired,
because whether either is still active is decided at restoration, not here.
`expires_at`, `effective_from` and `effective_until` are written as UTC
ISO-8601 with a `Z` suffix, the form `POST /api/auth/admin/roles` and
`POST /api/auth/admin/relationships` accept; the text either database prints
by default is refused by them. `sqlite3` writes no header for a query with no
row, so the SQLite relationships file of a store with no unrevoked edge is
empty, and only that file is checked for existence rather than content.
Select only the columns below. Never export
`raw_claims_json`, `email`, `display_name` or `subject_email_at_first_seen`;
the files name people and are retained under the same access controls as the
archive.

SQLite: run this inside the [Staging Reset](#staging-reset-for-elspethexamplegovau)
procedure after its archive step has created `$SNAPSHOT_DIR`
(`$DB_PATH.pre-phase1.<timestamp>`) and before its deletion step, with
`$DB_PATH` as that procedure resolved it and the service stopped:

```bash
: "${SNAPSHOT_DIR:?run the archive step first; the mapping is retained beside the archive}"
sqlite3 -header -csv "$DB_PATH" "
  SELECT provider, subject, identity_id AS pre_cutover_identity_id, kind, access_state
  FROM identities
  ORDER BY provider, subject
" | sudo tee "$SNAPSHOT_DIR/identity-mapping.pre.csv" >/dev/null
sqlite3 -header -csv "$DB_PATH" "
  SELECT i.provider, i.subject, r.identity_id AS pre_cutover_identity_id,
         r.role, r.scope, replace(r.expires_at, ' ', 'T') || 'Z' AS expires_at
  FROM identity_roles AS r
  JOIN identities AS i ON i.identity_id = r.identity_id
  WHERE r.revoked_at IS NULL
  ORDER BY i.provider, i.subject, r.role, r.scope
" | sudo tee "$SNAPSHOT_DIR/identity-grants.pre.csv" >/dev/null
sqlite3 -header -csv "$DB_PATH" "
  SELECT f.provider AS from_provider, f.subject AS from_subject, r.from_identity_id AS from_pre_cutover_identity_id,
         t.provider AS to_provider, t.subject AS to_subject, r.to_identity_id AS to_pre_cutover_identity_id,
         r.relationship_type,
         replace(r.effective_from, ' ', 'T') || 'Z' AS effective_from,
         replace(r.effective_until, ' ', 'T') || 'Z' AS effective_until
  FROM identity_relationships AS r
  JOIN identities AS f ON f.identity_id = r.from_identity_id
  JOIN identities AS t ON t.identity_id = r.to_identity_id
  WHERE r.revoked_at IS NULL
  ORDER BY t.provider, t.subject, f.provider, f.subject
" | sudo tee "$SNAPSHOT_DIR/identity-relationships.pre.csv" >/dev/null
sudo test -s "$SNAPSHOT_DIR/identity-mapping.pre.csv"
sudo test -s "$SNAPSHOT_DIR/identity-grants.pre.csv"
sudo test -f "$SNAPSHOT_DIR/identity-relationships.pre.csv"
```

PostgreSQL (ECS, Azure Container Apps, or a VM on external PostgreSQL):
`$ARCHIVE_DIR` is the directory that holds this window's archive/export on
the database operator's host, and `$SCHEMA_OWNER_SESSION_URL` is the
schema-owner role's URL for the stopped sessions database, supplied for this
window only and never exported to the service:

```bash
: "${ARCHIVE_DIR:?the directory holding the archive/export for this window}"
: "${SCHEMA_OWNER_SESSION_URL:?the schema-owner URL of the stopped sessions database}"
psql "$SCHEMA_OWNER_SESSION_URL" --no-psqlrc --set=ON_ERROR_STOP=1 \
  --command "\\copy (SELECT provider, subject, identity_id AS pre_cutover_identity_id, kind, access_state FROM identities ORDER BY provider, subject) TO '$ARCHIVE_DIR/identity-mapping.pre.csv' CSV HEADER" \
  --command "\\copy (SELECT i.provider, i.subject, r.identity_id AS pre_cutover_identity_id, r.role, r.scope, to_char(r.expires_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS expires_at FROM identity_roles AS r JOIN identities AS i ON i.identity_id = r.identity_id WHERE r.revoked_at IS NULL ORDER BY i.provider, i.subject, r.role, r.scope) TO '$ARCHIVE_DIR/identity-grants.pre.csv' CSV HEADER" \
  --command "\\copy (SELECT f.provider AS from_provider, f.subject AS from_subject, r.from_identity_id AS from_pre_cutover_identity_id, t.provider AS to_provider, t.subject AS to_subject, r.to_identity_id AS to_pre_cutover_identity_id, r.relationship_type, to_char(r.effective_from AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS effective_from, to_char(r.effective_until AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS effective_until FROM identity_relationships AS r JOIN identities AS f ON f.identity_id = r.from_identity_id JOIN identities AS t ON t.identity_id = r.to_identity_id WHERE r.revoked_at IS NULL ORDER BY t.provider, t.subject, f.provider, f.subject) TO '$ARCHIVE_DIR/identity-relationships.pre.csv' CSV HEADER"
test -s "$ARCHIVE_DIR/identity-mapping.pre.csv"
test -s "$ARCHIVE_DIR/identity-grants.pre.csv"
test -s "$ARCHIVE_DIR/identity-relationships.pre.csv"
```

**Re-admit the cohort inside the window, in this order.** With the recreated
stores initialized and the service still withheld from ordinary traffic.
Each identity is restored once and each of its grants separately, because
`POST /api/auth/admin/identities` cannot carry a grant's `scope` or
`expires_at` — any role it grants is unscoped and never expires — and only
`POST /api/auth/admin/roles` carries both. Every restored grant names the
administrator as `granted_by_identity_id` and the restoration time as
`granted_at`; the originals stay in the archive.

1. **Bootstrap one administrator.** Choose the row of
   `identity-mapping.pre.csv` with `kind` `human` and `access_state` `active`
   whose `identity-grants.pre.csv` rows include `role` `admin` with an empty
   `scope` and an empty `expires_at`, and make it the first administrator with
   `elspeth composer users bootstrap-admin` and that row's `provider` and
   `subject`, exactly as [Every local account lands `pending` after this
   reset](#every-local-account-lands-pending-after-this-reset) describes. The
   command writes an unscoped `admin` grant that never expires, which is that
   grant row restored; the `identity_id` it prints is the first entry of the
   mapping's new column. If no row qualifies because every pre-window
   administrator grant carries an `expires_at`, choose the human, active
   identity whose unscoped `admin` grant expires last and record in the
   compatibility record that its bootstrap grant does not expire although the
   pre-window grant did. After step 3, a second restored administrator revokes
   that bootstrap grant with `POST /api/auth/admin/roles/{role_id}/revoke`,
   taking the `role_id` from `GET /api/auth/admin/roles?identity_id=<identity_id>`
   (the command prints only the `identity_id`). If the exported `expires_at`
   is still in the future, the same administrator then re-grants it with
   `POST /api/auth/admin/roles` carrying that `expires_at`. If it has passed,
   the revocation stands without a re-grant: that authority ended at its own
   `expires_at`. With no second active administrator the API refuses the
   revocation with `last_active_admin_protected`, and the unexpiring grant
   stands as recorded.
   If every unscoped `admin` grant in the grants file has lapsed by now, there
   is no administrator to restore: bootstrap the identity the deployment owner
   names, and record it in the compatibility record as a new appointment, not
   a restoration. The authority never lets `admin` sit beside `user`,
   `approver`, `reviewer` or `curator`, so step 3 restores none of the new
   administrator's still-active grants of those four roles and records each
   `not_restored`. If that person must keep workload authority, the owner
   names a different administrator.
2. **Restore each identity once.** Sign in as that administrator. For every
   other row of `identity-mapping.pre.csv` with `kind` `human` and
   `access_state` `active`, call `POST /api/auth/admin/identities` once, with
   the row's `provider` and `subject`, `"role": "none"` and a nonempty `note`,
   and record the `identity.identity_id` the response returns beside
   `pre_cutover_identity_id`. The role is always `none`: every grant,
   `user`, `approver` and `reviewer` included, is restored in step 3 so that it
   keeps its `scope` and `expires_at`. `username` is not exported and not
   sent; it is the `subject` until the person's first sign-in refreshes it.
   Rows that were `pending` or `disabled` are not re-provisioned and none of
   their grants is restored: a pending person arrives `pending` again at their
   next login and is activated normally, and a disabled one stays out. A row
   with `kind` `service` is not restored either — pre-provisioning creates
   only `human` identities and no administration route creates a `service`
   one — and stays in the archive.
3. **Restore each grant that is still active.** For every row of
   `identity-grants.pre.csv` whose identity step 1 or step 2 restored, compare
   its `expires_at` with the current UTC time immediately before the call:
   - Empty, or later than now: call `POST /api/auth/admin/roles` with the
     restored `identity_id`, the row's `role`, its `scope` and its
     `expires_at` exactly as exported — omit `scope` or `expires_at` when the
     cell is empty, and never re-type an expiry in a local offset — and record
     the `role_id` the response returns. The grant keeps its scope and ends at
     the instant it would have ended had the store not been recreated.
   - At or before now: make no call and record the grant `lapsed`. Its
     authority ended at its own `expires_at`, and the recreate neither revives
     nor extends it; that includes a grant that was active when you exported
     and lapsed during the window. The API itself refuses an `expires_at` that
     is not in the future and writes nothing, so a grant that lapses between
     your comparison and the call is refused — today as HTTP 500, not a typed
     409. List the identity's roles with
     `GET /api/auth/admin/roles?identity_id=<identity_id>`, confirm no row was
     written, and record that grant `lapsed` too.
   - Skip the bootstrap administrator's unscoped `admin` row: step 1 restored
     it. A new appointment restored no row, so its lapsed `admin` rows are
     recorded `lapsed` like any other.
   - A newly appointed administrator's `user`, `approver`, `reviewer` or
     `curator` grant that is still active: make no call and record it
     `not_restored`.

   The authority refuses an `admin` grant beside an active `user`, `approver`,
   `reviewer` or `curator` grant with 409 `role_forbidden_for_identity`. The
   pre-window store refused the same pair, so for an identity restored from
   the export a restore that meets that refusal has diverged from the export:
   stop and compare the grants file with the archive before continuing. A
   newly appointed administrator does not meet it, because the bullet above
   makes no call for those grants.
   Then list each restored identity's roles with
   `GET /api/auth/admin/roles?identity_id=<identity_id>` and confirm every
   row's `role`, `scope` and `expires_at` against the export.
4. **Restore each approver edge whose ends came back.** Approver
   relationships (`identity_relationships`) did not survive the recreate.
   Who may decide an approval is not read from them: `decide` admits only the
   approver the request names. They do carry three things. An approver may
   appoint a `curator` only over an identity their own active `approver` edge
   points to directly. `GET /api/workflow/audit-view` shows an approver only
   the identities their active edges reach. The approver picker's default
   suggestion is the requester's active edges. Restore them from
   `identity-relationships.pre.csv`, after step 3: the API admits an edge
   only while both ends are `active` and its `from` identity holds an active
   `approver` grant. An empty file means there is no edge to restore. For
   every row of `identity-relationships.pre.csv`:
   - Both ends restored and an `approver` grant of the `from` identity
     recorded `restored` in step 3: call `POST /api/auth/admin/relationships`
     with the `identity_id` values steps 1 and 2 recorded for
     `from_pre_cutover_identity_id` and `to_pre_cutover_identity_id`, the
     row's `relationship_type`, and its `effective_from` and `effective_until`
     exactly as exported — omit `effective_from` or `effective_until` when the
     cell is empty — with a nonempty `note`, and record the `relationship_id`
     the response returns. The window is an annotation no check reads, so it
     is restored as exported even when it has passed; it does not decide
     whether the edge is restored.
   - Either end not restored, or no `approver` grant of the `from` identity
     recorded `restored`: make no call and record the edge `not_restored`.
     That includes an edge with a `pending` end and an edge whose overseeing
     approver's grant was recorded `lapsed`.
   - An `approver` grant that step 3 restored can still lapse before its edge
     is called; the API then refuses with 409 `approver_role_required` and
     writes nothing. List the `to` identity's edges with
     `GET /api/auth/admin/relationships?identity_id=<identity_id>`, confirm no
     edge was written, and record that edge `not_restored` too.

   Every restored edge names the administrator as `asserted_by_identity_id`
   and the restoration time as `asserted_at`; the originals stay in the
   archive. The pre-window store refused a second active edge into one
   identity and a cycle, so a restore refused with
   `relationship_already_active`, `default_approver_already_assigned`,
   `relationship_cycle` or `identity_not_active` has diverged from the
   export: stop and compare the relationships file with the archive before
   continuing. Then list each restored identity's edges with
   `GET /api/auth/admin/relationships?identity_id=<identity_id>` and confirm
   every edge's ends and window against the export.
5. **If `workflow_governance` is `on`**, do not reopen traffic until
   readiness reports `auth_mode` ok — R11 refuses `on` under
   `auth_provider=local` with `registration_mode=open`, and `on` without a
   `compartment_id`, by name — and until at least one restored identity
   holds an active `approver` grant. Until an approver who is not the author
   can decide, every run is refused with HTTP 409 `approval_required` (R2).
   The `approvals`, `review_attestations` and `library_entries` tables live in
   the sessions store and do not survive the recreate: every governed
   composition is re-created and re-approved on the far side, and curators
   re-publish the shared library. The pre-window rows stay in the archive as
   evidence.
6. Save the completed files beside the `.pre.csv` files in the archive
   directory: `identity-mapping.<window>.csv` adds the new `identity_id`
   column (empty for a row that was not restored), and
   `identity-grants.<window>.csv` adds `restored_role_id` and an `outcome`:
   `bootstrap` for the grant step 1 restored (no row has it after a new
   appointment, which the compatibility record carries), with the `role_id`
   `GET /api/auth/admin/roles?identity_id=<identity_id>` lists for it;
   `restored`, with the `role_id` step 3 recorded; `lapsed`; or
   `not_restored`, for a grant of an identity step 1 or step 2 did not restore
   and for a newly appointed administrator's `user`, `approver`, `reviewer` or
   `curator` grant. `restored_role_id` is empty for the last two. When a
   second administrator re-grants an expiring bootstrap grant, its row becomes
   `restored` with the new `role_id`; when it revokes that grant without a
   re-grant, the row becomes `lapsed`. Record both paths in the compatibility record's
   archive/export decision. `identity-relationships.<window>.csv` adds
   `restored_relationship_id` and an `outcome`: `restored`, with the
   `relationship_id` step 4 recorded, or `not_restored`, with
   `restored_relationship_id` empty; an empty SQLite export is saved as an
   empty completed file. Then verify, as the paragraph above requires,
   that an admitted account can sign in and that the expected
   chargeable-admission result is observed before reopening traffic.

**Send this notice for the window, with the placeholders filled.** It says
re-activation happened, because it did — a notice that only says "log in
again" sends everyone who was not in the cohort straight into the `pending`
wall with no explanation — and it names what people must re-enter:

```text
Subject: ELSPETH <deployment> — database recreation, <date> <start>–<end> <tz>

ELSPETH will be unavailable during this window while its session and audit
databases are recreated for the <release> schema. This is a recreation, not
an upgrade in place. When it reopens:

- Your account has been re-activated by an administrator from the pre-window
  record. Every previous session is invalid; sign in again as usual. If you
  are refused with "Account is pending", you were not in the re-admission
  cohort — ask <admin contact> to activate you.
- Stored secrets (the provider API keys you entered in the Composer) do not
  survive the recreation. Re-enter them before running a pipeline.
- Composer sessions, chat history, run history, approvals, review
  attestations and the shared library start empty. Exports taken before the
  window remain valid evidence; they name pre-window identities, and the
  operator holds the mapping.
- Uploaded files under the data directory and payload storage are kept.
```

### Cutover by deployment shape

Every shipped shape recreates both stores; none rebuilds in place. The
in-place VM rebuild offered in an earlier revision of the identity spec is
withdrawn (spec rev2.7, D21): it contradicted the pre-1.0 gate every other
shape cites and would have carried `user_secrets` bytes across a key change
as silent corruption. The mapping export and re-admission above are common to
every row; what differs is who recreates the store and which runbook carries
the procedure.

| Shape | Stores | Recreate procedure | Re-admission entry |
| --- | --- | --- | --- |
| ECS (Fargate) | PostgreSQL, both stores | [aws-ecs-deployment.md](aws-ecs-deployment.md) §3. Apply the schema compatibility gate (the database operator archives/exports, drops, recreates and runs `--init-schema`), the bound compatibility record with `rollback_permitted: false`, then §7. Prove rollback refusal | [aws-ecs-deployment.md](aws-ecs-deployment.md) §Authentication and secret injection (`bootstrap-admin oidc <sub>`) |
| Azure Container Apps | PostgreSQL, both stores (Flexible Server) | the database owner archives/exports, drops and recreates both databases, then reruns the schema Job from [azure-container-apps-cold-install.md](azure-container-apps-cold-install.md) §6. Initialize schemas and §7. Prove runtime credentials, then [azure-container-apps-existing-service-redeploy.md](azure-container-apps-existing-service-redeploy.md) §4–§7 for the candidate revision; an epoch-crossing candidate is never the image-only path | this runbook, [Re-admit the cohort](#operator-notice-for-the-workflow-epoch-window) with the deployment's provider |
| VM, SQLite | `sessions.db` and `audit.db` files | this runbook: [Staging Reset](#staging-reset-for-elspethexamplegovau) (archive + delete + recreate, sidecars as one artifact set) and [Phase 5b](#phase-5b-two-db-reset) for the Landscape file, inside [ansible-ubuntu-deployment.md](ansible-ubuntu-deployment.md) §Stop-before-start upgrade for the service lifecycle | this runbook, [Re-admit the cohort](#operator-notice-for-the-workflow-epoch-window) with `bootstrap-admin local` |
| VM, external PostgreSQL | PostgreSQL, both stores | the database owner archives/exports, drops and recreates both databases; then [ansible-ubuntu-deployment.md](ansible-ubuntu-deployment.md) §Initialize external schemas once with the schema-owner URLs (`elspeth doctor deployment --init-schema`), swapped back to the runtime URLs before §Upgrade validation: external PostgreSQL | this runbook, [Re-admit the cohort](#operator-notice-for-the-workflow-epoch-window) with the deployment's provider |

````

(c) At :778, replace the clause `On the 0.8.0 cutover, also settle the
identity lockout at this point: every local account is \`pending\` until an
operator activates it, per [Every local account lands \`pending\` after this
reset](#every-local-account-lands-pending-after-this-reset).` with:

```markdown
On every cutover from 0.8.0 onward, also settle the identity lockout at this point: every local account is `pending` until an operator activates it, per [Every local account lands `pending` after this reset](#every-local-account-lands-pending-after-this-reset), and complete the mapping, re-admission and notice steps in [Operator notice for the workflow-epoch window](#operator-notice-for-the-workflow-epoch-window) before handing the service back.
```

- [ ] **Step 5: Edit the ECS runbook — one paragraph in §Authentication and secret injection.**

In `docs/runbooks/aws-ecs-deployment.md`, after :1441 (`first person.`, the
end of the paragraph that links the Identity Providers guide) and its
following blank line, before `The legacy browser-client settings (\`oidc_*\`)`,
insert:

```markdown
Crossing a schema epoch with an existing pool re-admits nobody automatically,
and every session — Cognito or local — is refused from the first request
after the recreate, because a session token's subject is an identity the
recreated store no longer holds. Before the drop in section 3, export the
identity mapping from the stopped store; after `bootstrap-admin`,
re-provision the cohort through the admin API and send the window notice,
all per the [session DB reset runbook](staging-session-db-recreation.md),
§Operator notice for the workflow-epoch window. Where the task definition
exports `ELSPETH_WEB__WORKFLOW_GOVERNANCE=on`, at least one re-provisioned
identity must hold `approver` before traffic is enabled in section 5, or
every run is refused with `approval_required`; the pre-window approvals,
attestations and library rows stay in the archive as evidence.

```

No bash fence is added, so `test_every_bash_fence_is_syntactically_valid`
and the `aws ` line rule are untouched.

- [ ] **Step 6: Edit the ACA redeploy runbook — one paragraph at the end of §7.**

In `docs/runbooks/azure-container-apps-existing-service-redeploy.md`, after
the `> **LIVE:**` callout (:267-268) and its following blank line, before
`## Rollback` (:270), insert:

```markdown
A candidate that crosses a schema epoch is not an image-only redeploy. The
database owner archives/exports, drops and recreates both databases and
reruns the schema Job from the [cold-install runbook](azure-container-apps-cold-install.md)
§6 before step 5, and every signed-in session is refused after the recreate
because its token names an identity the recreated store no longer holds.
Export the identity mapping from the stopped store before the drop,
re-admit the cohort after the candidate revision is ready, and send the
window notice, all per the [session DB reset runbook](staging-session-db-recreation.md),
§Operator notice for the workflow-epoch window; run the authenticated flow
above only after re-admission.

```

The paragraph carries no epoch numeral: `test_every_epoch_literal_matches_the_live_constants`
(`test_azure_container_apps_runbook_contract.py:182`) regex-matches
`session epoch (\d+)` and `landscape epoch (\d+)` case-insensitively over
this file.

- [ ] **Step 7: Rewrite the VM runbook's §Release compatibility.**

Replace `docs/runbooks/ansible-ubuntu-deployment.md:62-66` (the paragraph
that opens `The schema-incompatible 0.8.0 upgrade from 0.7.1 is not an
in-place session database migration.` and closes `Do not start 0.7.1 against
the recreated 0.8.0 session database.`) with:

```markdown
Every pre-1.0 release that advances a schema epoch is a recreation, not an
in-place migration; 0.8.1 is one, and the CHANGELOG's cutover paragraph
names the epochs. Before such an upgrade: archive required evidence, export
the identity mapping, drain and stop the old service, recreate the session
and Landscape stores at the new epoch — the SQLite files per the
[session DB reset runbook](staging-session-db-recreation.md), external
PostgreSQL by dropping and recreating both databases and rerunning
§Initialize external schemas once with the schema-owner URLs — then re-admit
the cohort and send the window notice per that runbook's
§Operator notice for the workflow-epoch window, and repair forward. Do not
start the previous release against a recreated store. SQLite has no
in-place rebuild path: the one an earlier spec revision offered is withdrawn.
```

- [ ] **Step 8: Add the cutover sentence to the CHANGELOG.**

In `CHANGELOG.md`, under `## 0.8.1 - 2026-09-10`, in the paragraph that ends
`Do not roll older code back over the recreated databases; keep the service
drained and repair this release forward.` (HEAD :25-26; re-measure after I0,
which edits :9-10 of the same section; match the whole two-line sentence,
because its second line alone also occurs at :554 under 0.8.0), append after
that sentence, inside the same paragraph:

```markdown
Every signed-in session is refused after the recreate — a session token names
an identity the recreated store no longer holds — so the window includes
exporting the identity mapping, re-activating the cohort and sending the
operator notice (runbook §Operator notice for the workflow-epoch window);
users re-enter their stored secrets, and approvals, review attestations and
library entries start empty.
```

`test_changelog_preserves_historical_epochs_and_assigns_live_epochs_to_current_release`
(`tests/unit/website/test_release_site_contract.py:53`) slices this section
and still finds `install 0.8.1`; nothing it pins moves.

- [ ] **Step 9: Run the new tests and every gate that reads a file this task edited.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/docs/test_staging_session_recreation_policy.py tests/unit/docs/test_deployment_platform_docs.py tests/unit/docs/test_release_version_surfaces.py tests/unit/docs/test_changelog_release_links.py tests/unit/website/test_release_site_contract.py tests/unit/web/test_aws_ecs_runbook_contract.py tests/unit/web/test_azure_container_apps_runbook_contract.py tests/unit/web/auth/test_cutover_identity_restoration.py tests/unit/web/auth/test_identity_admin_routes.py -n 0 > /tmp/i11-docs-green.log 2>&1; echo exit=$?
```

Expected: `exit=0`. The first seven files are the readers of
`staging-session-db-recreation.md`, `CHANGELOG.md`, `aws-ecs-deployment.md`,
`azure-container-apps-existing-service-redeploy.md` and
`ansible-ubuntu-deployment.md` (`grep -rln "staging-session-db-recreation\|aws-ecs-deployment.md\|ansible-ubuntu-deployment\|azure-container-apps-existing-service-redeploy" tests/`
measured 2026-09-13, plus the two CHANGELOG readers); the last two are the
restoration tests and the admin-route module whose `_Harness`, `_build` and
`_bearer` they import. Then lint the two test files the house way:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff check tests/unit/docs/test_staging_session_recreation_policy.py tests/unit/web/auth/test_cutover_identity_restoration.py > /tmp/i11-ruff.log 2>&1; echo exit=$?; ruff format --check tests/unit/docs/test_staging_session_recreation_policy.py tests/unit/web/auth/test_cutover_identity_restoration.py >> /tmp/i11-ruff.log 2>&1; echo exit=$?
```

Expected: `exit=0` twice. The restoration tests execute the SQLite export
only. Prove the PostgreSQL fence against a throwaway PostgreSQL 17 container
seeded through the real authority: the proof runs the fence exactly as the
runbook prints it and parses every exported grant with the real
`GrantRoleRequest` and every exported edge with the real
`AssertRelationshipRequest`. It needs Docker, as the testcontainer suite does, and
local port 55439 free. It first removes a container an aborted run left
behind, and it exports `PYTHONPATH` and asserts `elspeth.__file__` lies under
this tree's `src/`, so a worktree proves its own code:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && { docker rm -f i11-export-proof > /dev/null 2>&1 || true; } && export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" && docker run -d --rm --name i11-export-proof -e POSTGRES_USER=proof -e POSTGRES_PASSWORD=proof -e POSTGRES_DB=sessions -p 127.0.0.1:55439:5432 postgres:17-alpine > /tmp/i11-pg-proof.log 2>&1 && python - >> /tmp/i11-pg-proof.log 2>&1 <<'PY'
import csv
import io
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.exc import OperationalError

import elspeth
from elspeth.web.auth.identity_admin_routes import AssertRelationshipRequest, GrantRoleRequest
from elspeth.web.auth.models import IdentityClaims
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import IdentityAdminActor, RepositoryIdentityAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema

# In a worktree the .venv imports the main checkout's elspeth unless PYTHONPATH names this tree.
assert Path(elspeth.__file__).resolve().is_relative_to(Path.cwd().resolve() / "src"), elspeth.__file__
engine = create_session_engine("postgresql+psycopg://proof:proof@127.0.0.1:55439/sessions")
for _attempt in range(60):
    try:
        with engine.connect():
            break
    except OperationalError:
        time.sleep(1)
initialize_session_schema(engine)
authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
root = authority.bootstrap_admin(
    claims=IdentityClaims(provider="local", subject="root", username="root"),
    note="export proof",
    quota_tokens_per_day=None,
    quota_storage_bytes=None,
    record=lambda _event: None,
)
actor = IdentityAdminActor(identity_id=root.record.identity_id, on_behalf_of=None, console_request_id=None)
alice = authority.pre_provision_identity(
    actor=actor,
    provider="local",
    subject="alice",
    username=None,
    organisation_id=None,
    role="none",
    note="export proof",
    quota_tokens_per_day=None,
    quota_storage_bytes=None,
    record=lambda _event: None,
).record.identity_id
# A +10:00 expiry: PostgreSQL stores the instant, and the export must print it in UTC.
authority.grant_role(
    actor=actor,
    identity_id=alice,
    role="reviewer",
    scope="lib-1",
    expires_at=datetime(2999, 6, 30, 22, 0, 0, 123456, tzinfo=timezone(timedelta(hours=10))),
    note="export proof",
    record=lambda _event: None,
)
revoked = authority.grant_role(actor=actor, identity_id=alice, role="approver", scope=None, expires_at=None, note="export proof", record=lambda _event: None)
authority.revoke_role(actor=actor, role_id=revoked.role_id, note="export proof", record=lambda _event: None)
# An edge needs an active approver grant on its from end, so alice holds one again.
authority.grant_role(actor=actor, identity_id=alice, role="approver", scope=None, expires_at=None, note="export proof", record=lambda _event: None)
bob = authority.pre_provision_identity(
    actor=actor,
    provider="local",
    subject="bob",
    username=None,
    organisation_id=None,
    role="none",
    note="export proof",
    quota_tokens_per_day=None,
    quota_storage_bytes=None,
    record=lambda _event: None,
).record.identity_id
plus_ten = timezone(timedelta(hours=10))
first_edge = authority.assert_relationship(
    actor=actor,
    from_identity_id=alice,
    to_identity_id=bob,
    relationship_type="approver",
    effective_from=None,
    effective_until=None,
    note="export proof",
    record=lambda _event: None,
)
authority.revoke_relationship(actor=actor, relationship_id=first_edge.relationship_id, note="export proof", record=lambda _event: None)
# +10:00 window ends: PostgreSQL stores the instants, and the export must print them in UTC.
authority.assert_relationship(
    actor=actor,
    from_identity_id=alice,
    to_identity_id=bob,
    relationship_type="approver",
    effective_from=datetime(2001, 1, 1, 10, 0, 0, tzinfo=plus_ten),
    effective_until=datetime(2999, 6, 30, 22, 0, 0, 123456, tzinfo=plus_ten),
    note="export proof",
    record=lambda _event: None,
)

runbook = Path("docs/runbooks/staging-session-db-recreation.md").read_text(encoding="utf-8")
notice = runbook.split("### Operator notice for the workflow-epoch window\n", maxsplit=1)[1].split("\n### ", maxsplit=1)[0]
fence = "`" * 3
(postgres_export,) = [block for block in re.findall(rf"{fence}bash\n(.*?){fence}", notice, flags=re.DOTALL) if block.startswith(': "${ARCHIVE_DIR')]
script = "set -euo pipefail\nARCHIVE_DIR=/tmp\nSCHEMA_OWNER_SESSION_URL=postgresql://proof:proof@127.0.0.1:5432/sessions\n" + postgres_export  # secret-scan: allow-this-line
subprocess.run(["docker", "exec", "-i", "i11-export-proof", "bash", "-s"], input=script, text=True, check=True)
mapping_csv = subprocess.run(["docker", "exec", "i11-export-proof", "cat", "/tmp/identity-mapping.pre.csv"], capture_output=True, text=True, check=True).stdout
grants_csv = subprocess.run(["docker", "exec", "i11-export-proof", "cat", "/tmp/identity-grants.pre.csv"], capture_output=True, text=True, check=True).stdout
edges_csv = subprocess.run(
    ["docker", "exec", "i11-export-proof", "cat", "/tmp/identity-relationships.pre.csv"], capture_output=True, text=True, check=True
).stdout
print(mapping_csv, end="")
print(grants_csv, end="")
print(edges_csv, end="")
mapping = list(csv.DictReader(io.StringIO(mapping_csv)))
grants = list(csv.DictReader(io.StringIO(grants_csv)))
edges = list(csv.DictReader(io.StringIO(edges_csv)))
assert [(row["subject"], row["kind"], row["access_state"]) for row in mapping] == [
    ("alice", "human", "active"),
    ("bob", "human", "active"),
    ("root", "human", "active"),
]
assert [(row["subject"], row["role"], row["scope"], row["expires_at"]) for row in grants] == [
    ("alice", "approver", "", ""),
    ("alice", "reviewer", "lib-1", "2999-06-30T12:00:00.123456Z"),
    ("root", "admin", "", ""),
]
assert [(row["from_subject"], row["to_subject"], row["relationship_type"], row["effective_from"], row["effective_until"]) for row in edges] == [
    ("alice", "bob", "approver", "2001-01-01T00:00:00.000000Z", "2999-06-30T12:00:00.123456Z"),
]
for row in grants:
    body = {"identity_id": row["pre_cutover_identity_id"], "role": row["role"], "note": "export proof"}
    if row["scope"] != "":
        body["scope"] = row["scope"]
    if row["expires_at"] != "":
        body["expires_at"] = row["expires_at"]
    GrantRoleRequest.model_validate(body)
for row in edges:
    edge_body = {
        "from_identity_id": row["from_pre_cutover_identity_id"],
        "to_identity_id": row["to_pre_cutover_identity_id"],
        "relationship_type": row["relationship_type"],
        "note": "export proof",
    }
    for column in ("effective_from", "effective_until"):
        if row[column] != "":
            edge_body[column] = row[column]
    AssertRelationshipRequest.model_validate(edge_body)
print("postgres export parses as GrantRoleRequest and AssertRelationshipRequest")
PY
echo exit=$?; docker stop i11-export-proof > /dev/null; tail -11 /tmp/i11-pg-proof.log
```

Expected: `exit=0`, and the log tail prints the three files: `alice`, `bob`
and `root` in the mapping; in the grants `local,alice,<identity_id>,approver,,`,
`local,alice,<identity_id>,reviewer,lib-1,2999-06-30T12:00:00.123456Z` (granted
with a `+10:00` expiry and printed in UTC) and
`local,root,<identity_id>,admin,,`, with the revoked `approver` grant absent
(only the re-granted one is listed); in the relationships
`local,alice,<identity_id>,local,bob,<identity_id>,approver,2001-01-01T00:00:00.000000Z,2999-06-30T12:00:00.123456Z`
(asserted with `+10:00` window ends and printed in UTC), with the revoked
first edge absent; then
`postgres export parses as GrantRoleRequest and AssertRelationshipRequest`.
Without the `to_char(...)` column the same proof prints
`2999-06-30 12:00:00.123456+00`, which the admin API refuses, and fails at
its row assertion (measured 2026-09-15 for `expires_at`). No
testcontainer-marked test is added: this task changes no schema, session
persistence or lock, and this proof exercises the one PostgreSQL statement
text it adds.

- [ ] **Step 10: Commit by file pathspec.**

```bash
cd "$(git rev-parse --show-toplevel)" && git status --short && scripts/branch-safety-check.sh --intent commit && git commit -m "docs(identity): cutover runbooks for the workflow epoch" -- docs/runbooks/staging-session-db-recreation.md docs/runbooks/aws-ecs-deployment.md docs/runbooks/azure-container-apps-existing-service-redeploy.md docs/runbooks/ansible-ubuntu-deployment.md CHANGELOG.md tests/unit/docs/test_staging_session_recreation_policy.py tests/unit/web/auth/test_cutover_identity_restoration.py && git show --stat HEAD
```

Expected: the safety check prints no `[FAIL]` line and the `--stat` names
exactly seven files. If `git status --short` showed a sibling lane's file
staged, it is not in this pathspec and stays out of the commit; do not
`git add`. Performing the cutover, countersigning the compatibility record
and firing the judge bundle (`elspeth-b03f0aa218`) remain operator actions
and are not claimed by this commit.
