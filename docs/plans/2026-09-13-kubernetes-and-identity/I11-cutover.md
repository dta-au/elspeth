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
  sentence occurs once). The section is the one I0 confirmed with the
  operator (DECISIONS I19).
- Test: `tests/unit/docs/test_staging_session_recreation_policy.py` (71
  lines on HEAD; 3 tests, `exit=0` measured 2026-09-13; the new tests append
  after :71). One test file: it already owns the cutover contract and reads
  `CHANGELOG.md` (:39-51), so the four other docs are read from here rather
  than opening four more test files.
- NOT touched: `src/elspeth/web/frontend/**` (landed, see above);
  `docs/runbooks/kubernetes-deployment.md` (K8's file; may not exist when
  this task executes — see the open question).

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
    `approved` row matches the compiled binding; the approver is any identity
    holding an active `approver` role who is not the author.
  - I1 / I5: `AdmissionRefusalReason.QUOTA_EXCEEDED = "quota_exceeded"`
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
    `POST /api/auth/admin/roles` with `GrantRoleRequest` (:136-139) for the
    roles outside `ActivationRole` (`admin`, `curator`, `auditor`,
    `oversight` — `_IDENTITY_ROLE_CHECK`, `sessions/models.py:3311`);
    `SessionTokenIssuer.mint` writes `"sub": identity_id`
    (`auth/session_token.py:193`), `authenticate` (:241-250) and `refresh`
    (:252-269, which calls `authenticate`) raise
    `AuthenticationError("Invalid token")` when `_principal_is_active` is
    false; `identities` (`sessions/models.py:3328`: `identity_id`,
    `provider`, `kind`, `subject`, `access_state`) and `identity_roles`
    (:3406: `identity_id`, `role`, `scope`, `revoked_at`);
    `quota_policies.tokens_per_day` / `storage_bytes` (:3843/:3849);
    `quota_default_tokens_per_day` / `quota_default_storage_bytes` /
    `quota_container_tokens_per_day` (`config.py:538-541`); the sessions-store
    tables `approvals` (:3589), `review_attestations` (:3738),
    `library_entries` (:3785) — all discarded by the recreate.
- Produces:
  - Runbook headings `### Operator notice for the workflow-epoch window` and
    `### Cutover by deployment shape` inside `## Current Cutover:` of
    `docs/runbooks/staging-session-db-recreation.md`; the second carries the
    per-shape table K8 appends a Kubernetes row to (see the open question).
  - The cutover artifact name `identity-mapping.pre.csv` (columns
    `provider, subject, pre_cutover_identity_id, kind, access_state, role, scope`),
    completed after re-provisioning as `identity-mapping.<window>.csv` with
    the new `identity_id` column, retained with the archive.
  - The ready-to-send notice as the single ```` ```text ```` fence in the
    notice section.
  - Tests in `tests/unit/docs/test_staging_session_recreation_policy.py`:
    `test_current_cutover_carries_the_workflow_epoch_operator_notice`,
    `test_operator_notice_says_re_activation_before_sign_in`,
    `test_current_cutover_routes_each_shipped_deployment_shape_to_its_recreate_procedure`,
    `test_current_cutover_cites_only_admission_refusals_the_contract_defines`,
    `test_every_deployment_runbook_points_at_the_operator_notice`,
    `test_changelog_states_the_session_invalidation_and_re_activation`.

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

    # The mapping artifact is exported from the STOPPED live store, before the
    # drop, from exactly these columns -- never claims or contact fields.
    assert "identity-mapping.pre.csv" in notice
    exports = _fences(notice, "bash")
    assert len(exports) == 2, "one SQLite export and one PostgreSQL export"
    for export in exports:
        assert "FROM identities" in export
        assert "identity_roles" in export
        assert "revoked_at IS NULL" in export
        for column in ("provider", "subject", "pre_cutover_identity_id", "access_state", "role"):
            assert column in export, column
        for forbidden in ("raw_claims_json", "email", "display_name", "subject_email_at_first_seen"):
            assert forbidden not in export, forbidden
    assert "identity-mapping.<window>.csv" in notice

    # Re-admission goes through the audited paths that already exist.
    assert "elspeth composer users bootstrap-admin" in notice
    assert "POST /api/auth/admin/identities" in notice
    assert "POST /api/auth/admin/roles" in notice

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

- [ ] **Step 2: Run the policy tests to verify the six new ones fail for the right reason.**

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

- [ ] **Step 3: Confirm the refusal vocabulary the token-admission paragraph will cite against the landed contract.**

The paragraph in Step 4 names three refusals. Two are on HEAD; the third,
`quota_exceeded`, is I1's (DECISIONS I5). Prove all three exist before
writing them into an operator document:

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
evidence. Select only the columns below. Never export `raw_claims_json`,
`email`, `display_name` or `subject_email_at_first_seen`; the artifact names
people and is retained under the same access controls as the archive.

SQLite: run this inside the [Staging Reset](#staging-reset-for-elspethexamplegovau)
procedure after its archive step has created `$SNAPSHOT_DIR`
(`$DB_PATH.pre-phase1.<timestamp>`) and before its deletion step, with
`$DB_PATH` as that procedure resolved it and the service stopped:

```bash
: "${SNAPSHOT_DIR:?run the archive step first; the mapping is retained beside the archive}"
sqlite3 -header -csv "$DB_PATH" "
  SELECT i.provider, i.subject, i.identity_id AS pre_cutover_identity_id,
         i.kind, i.access_state, r.role, r.scope
  FROM identities AS i
  LEFT JOIN identity_roles AS r
    ON r.identity_id = i.identity_id AND r.revoked_at IS NULL
  ORDER BY i.provider, i.subject, r.role
" | sudo tee "$SNAPSHOT_DIR/identity-mapping.pre.csv" >/dev/null
sudo test -s "$SNAPSHOT_DIR/identity-mapping.pre.csv"
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
  --command "\\copy (SELECT i.provider, i.subject, i.identity_id AS pre_cutover_identity_id, i.kind, i.access_state, r.role, r.scope FROM identities AS i LEFT JOIN identity_roles AS r ON r.identity_id = i.identity_id AND r.revoked_at IS NULL ORDER BY i.provider, i.subject, r.role) TO '$ARCHIVE_DIR/identity-mapping.pre.csv' CSV HEADER"
test -s "$ARCHIVE_DIR/identity-mapping.pre.csv"
```

**Re-admit the cohort inside the window, in this order.** With the recreated
stores initialized and the service still withheld from ordinary traffic:

1. Make the first administrator with `elspeth composer users bootstrap-admin`
   exactly as [Every local account lands `pending` after this
   reset](#every-local-account-lands-pending-after-this-reset) describes,
   using the administrator's `provider` and `subject` from the mapping. The
   `identity_id` it prints is the first entry of the mapping's new column.
2. Sign in as that administrator and pre-provision every row whose
   `access_state` was `active` with `POST /api/auth/admin/identities`
   (`provider`, `subject`, `role`, `note`; `username` where the pre-window
   username must be kept). `role` is the mapping's `role` where it is
   `user`, `approver` or `reviewer`, and `none` otherwise. Record the
   `identity.identity_id` each response returns beside
   `pre_cutover_identity_id`. Grant the roles the activation path cannot —
   `admin`, `curator`, `auditor`, `oversight` — afterwards with
   `POST /api/auth/admin/roles`. Rows that were `pending` or `disabled` are
   not re-provisioned; a pending person arrives `pending` again at their next
   login and is activated normally, and a disabled one stays out.
3. Approver relationships (`identity_relationships`) are not in the mapping
   and are not required for enforcement — any identity holding an active
   `approver` role who is not the author may decide — so recreate them
   through the relationship administration routes only where the deployment
   used them for the picker's default suggestion.
4. **If `workflow_governance` is `on`**, do not reopen traffic until
   readiness reports `auth_mode` ok — R11 refuses `on` under
   `auth_provider=local` with `registration_mode=open`, and `on` without a
   `compartment_id`, by name — and until at least one re-provisioned identity
   holds `approver`. Until an approver who is not the author can decide,
   every run is refused with HTTP 409 `approval_required` (R2). The
   `approvals`, `review_attestations` and `library_entries` tables live in
   the sessions store and do not survive the recreate: every governed
   composition is re-created and re-approved on the far side, and curators
   re-publish the shared library. The pre-window rows stay in the archive as
   evidence.
5. Save the completed mapping as `identity-mapping.<window>.csv` beside
   `identity-mapping.pre.csv` in the archive directory and record its path
   in the compatibility record's archive/export decision. Then verify, as the paragraph above requires,
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
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/docs/test_staging_session_recreation_policy.py tests/unit/docs/test_deployment_platform_docs.py tests/unit/docs/test_release_version_surfaces.py tests/unit/docs/test_changelog_release_links.py tests/unit/website/test_release_site_contract.py tests/unit/web/test_aws_ecs_runbook_contract.py tests/unit/web/test_azure_container_apps_runbook_contract.py -n 0 > /tmp/i11-docs-green.log 2>&1; echo exit=$?
```

Expected: `exit=0`. The seven files are the readers of
`staging-session-db-recreation.md`, `CHANGELOG.md`, `aws-ecs-deployment.md`,
`azure-container-apps-existing-service-redeploy.md` and
`ansible-ubuntu-deployment.md` (`grep -rln "staging-session-db-recreation\|aws-ecs-deployment.md\|ansible-ubuntu-deployment\|azure-container-apps-existing-service-redeploy" tests/`
measured 2026-09-13, plus the two CHANGELOG readers). Then lint the test
file the house way:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff check tests/unit/docs/test_staging_session_recreation_policy.py > /tmp/i11-ruff.log 2>&1; echo exit=$?; ruff format --check tests/unit/docs/test_staging_session_recreation_policy.py >> /tmp/i11-ruff.log 2>&1; echo exit=$?
```

Expected: `exit=0` twice. No testcontainer run: this task touches no
schema, SQL, session persistence or lock.

- [ ] **Step 10: Commit by file pathspec.**

```bash
cd "$(git rev-parse --show-toplevel)" && git status --short && scripts/branch-safety-check.sh --intent commit && git commit -m "docs(identity): cutover runbooks for the workflow epoch" -- docs/runbooks/staging-session-db-recreation.md docs/runbooks/aws-ecs-deployment.md docs/runbooks/azure-container-apps-existing-service-redeploy.md docs/runbooks/ansible-ubuntu-deployment.md CHANGELOG.md tests/unit/docs/test_staging_session_recreation_policy.py && git show --stat HEAD
```

Expected: the safety check prints no `[FAIL]` line and the `--stat` names
exactly six files. If `git status --short` showed a sibling lane's file
staged, it is not in this pathspec and stays out of the commit; do not
`git add`. Performing the cutover, countersigning the compatibility record
and firing the judge bundle (`elspeth-b03f0aa218`) remain operator actions
and are not claimed by this commit.
