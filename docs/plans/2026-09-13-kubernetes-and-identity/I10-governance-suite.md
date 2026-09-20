### Task I10: Workflow-governance suite (fire + mutation per refusal)

> **Current execution note (2026-09-19):** The 2026-09-19 [execution map](../2026-09-19-identity-workflow-finalization.md) supersedes pinned baseline and contrary decision tests below. Prove role-based non-author approval at inbox, inspect and decide; prove later same-state rejection retires earlier approval and blocks run admission; prove supersession audit events and signed-export `compartment_id`, with positive and mutation controls.
> Also prove that an attestation without an open exact-state review request is
> refused, and that state-creating Composer chat paste records compartment
> ingress evidence.

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: I8, I9. Runs before: I11. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered after I9 and before I11 (see the master's Workstream layout and ordering), with the direct edge I8 → I10
for the closed deployment. Spec: `docs/specs/2026-09-02-pluggable-sso-design.md`
§Testing → Workflow governance (:1276-1311) and §Refusals R2, R7, R8, R9, R11,
R13, R14 (:1057-1188). Measured on the working-tree HEAD `818d04577`, one
commit past `072141b75`; that commit touched only
`src/elspeth/web/composer/service.py`, `tests/unit/web/composer/test_advisor_checkpoint.py`
and `config/cicd/soft-mapping-census.yaml`, none of which this block reads.

What this task is, and why it is not a re-typing of I1–I9. Every ancestor
already wrote the fire and mutation-derivation pair for the refusal it owns,
next to the code it owns (R14 in I1, R13 and its sites in I2, R2 and the
concurrent-decide loser in I3, reviewer = author in I4, curator = publisher in
I5, the read-side cycle walk in I7, R11 in I8, the mailbox round trip at route
level in I9; R7, R8 and R9 authority tests are on HEAD in
`tests/unit/web/coordination/test_identity_authority.py`). Duplicating them
would add maintenance and prove nothing new. I10 therefore delivers two
things:

1. **A fail-closed inventory** — `tests/unit/web/workflow/test_governance_suite_inventory.py`
   names every spec item with its fire tests and mutation-derivation tests by
   module path and test name, and fails when a named test is missing, skipped
   or xfailed, or is pinned as both halves without being parametrised over the
   authority row. Deleting or renaming any governance test, or dropping an item
   the spec lists, turns it red. Its reader is proven against planted positive
   and negative modules in the same file.
2. **Only the gaps, written in full**, on the closed deployment:
   - R7 cycle refusal through the route, with a mutation twin (HEAD :945 is
     fire-only); R7 approver-role refusal with a mutation twin (HEAD :914 is
     fire-only);
   - a cycle seeded past R7 by direct SQL, refused (`relationship_cycle`) on
     the next insert whose ancestor walk passes through it — the write side;
     I7 :905 covers only the read-side walk;
   - R8 in both grant orders through `POST /api/auth/admin/roles`, including
     the revoked-workload-row case that must not refuse (HEAD :857 goes
     through the authority, and reaches the revoked row by revoking, not by
     seeding);
   - R9 through the real local login: `create_app` wires
     `identity_dormancy_days` into the local admission (`src/elspeth/web/app.py:1094-1110`,
     `_admit_identity` passes `identity_dormancy_days=settings.identity_dormancy_days` to `ensure_identity`),
     so a dormant local identity is re-pended at login on the closed deployment;
   - the full approval round trip on `create_app` — request with a note,
     approver's badge, decide with a note, the losing second decider, the
     requester sees decision and note, the badge clears — with the Landscape
     `auth_events` rows carrying the I6 compartment marking, and a mutation
     twin proving the badge derives from `approvals.decision_seen_at`;
   - author = approver as a schema violation: `ck_approvals_author_is_not_approver`
     (`src/elspeth/web/sessions/models.py:3647-3650`) has no pin in I3.

Four decisions taken against the shorthand:

- **No `tests/unit/web/conftest.py` edit.** I8 Step 8 creates `closed_local_settings`
  and `closed_local_app`; this task consumes `closed_local_app` in the unit
  schema module and inventories I8's proof that it passes R11.
- **The route suite lives in `tests/integration/web/workflow/`, on `create_app`,
  not on `closed_local_app`.** pytest conftests are directory-scoped, so the
  unit-tree fixture is invisible there, and the identity-admin routes read the
  bearer token themselves: `_require_identity_admin` calls
  `await get_current_user(request)` directly (`src/elspeth/web/auth/identity_admin_routes.py:341-351`),
  which a `dependency_overrides` entry never reaches. The suite authenticates
  with real local-auth tokens, the I3 Step 29 pattern.
- **No `tests/testcontainer/web/test_workflow_concurrency_postgres.py`.** The only
  concurrency item the spec names for this suite is "the losing half of the
  concurrent-decide guard", which I3 Step 31 already proves on PostgreSQL in
  `tests/testcontainer/web/test_approval_decide_race_postgres.py`; I4, I5 and
  I9 each carry their own race proof. This task adds no SQL, lock, schema or
  writer, so F7 does not require a new testcontainer file; Step 10 still runs
  the workflow PostgreSQL files the inventory depends on.
- **The UTC-midnight rollover case is the token dimension's only; storage has
  no day-boundary case.** The spec asks for one "day-boundary rollover case"
  (`docs/specs/2026-09-02-pluggable-sso-design.md:1297-1298`), and only R14
  names a day boundary (:1168). R13 (:1127-1130) bounds a live total, and D18
  (:77) calls storage "a **level** not a rate": `SUM(blobs.size_bytes)` over
  live rows, with no window to reset. I2's `admit_storage_bytes_on_connection`
  takes no clock argument (`session_id`, `additional_bytes`, `operation`,
  `record`), so a storage test could not name a midnight instant. The
  inventory therefore pins I1's
  `test_r14_day_rolls_over_at_utc_midnight_on_the_database_clock` and has no
  R13 twin. **Operator ruling needed:** accepting tokens-only matches the spec.
  If the operator instead wants a test showing the storage total does not reset
  at UTC midnight, it belongs in I2's
  `tests/unit/web/coordination/test_storage_quota_authority.py`. It would seed a
  blob whose `blobs.created_at` (`src/elspeth/web/sessions/models.py:2716`) is
  before today's UTC midnight and show that the blob still counts toward the cap. This inventory would then gain a matching `Pin`, and I10's
  Step 3 expectation would not change.

**Files:**
- Create: `tests/unit/web/workflow/test_governance_suite_inventory.py` (the inventory; the package `tests/unit/web/workflow/__init__.py` is I3's)
- Create: `tests/unit/web/workflow/test_governance_schema_violations.py` (author = approver as a CHECK violation, on I8's `closed_local_app`)
- Create: `tests/integration/web/workflow/test_governance_refusals.py` (the gap tests on `create_app`; the package `tests/integration/web/workflow/__init__.py` is I3's)
- No edit: `tests/unit/web/conftest.py` (I8 owns `closed_local_app`); `tests/unit/architecture/test_session_db_mutation_authority.py` (no production writer is added, so no writer row or authority binding is needed; Steps 1 and 11 prove the gate does not move); `CHANGELOG.md` (test-only change, nothing user-visible)
- Not committed (lane-private positive controls, Step 7): `/tmp/i10-mutants/i10_mutant_r7.py`, `/tmp/i10-mutants/i10_mutant_r8.py`

**Interfaces:**
- Consumes:
  - I8: `WebSettings.workflow_governance: Literal["off", "on"] = "off"`; `_check_auth_mode(settings: WebSettings) -> ReadinessCheck` (`src/elspeth/web/readiness.py:315`) and `ReadinessCheck(name, ok, detail)` (`:42`) with the byte-exact details `local authentication configured; workflow governance on` and `workflow_governance=on is refused with auth_provider=local and registration_mode=open (R11): one person can hold many local identities, so every author-is-not-approver rule is defeatable; set registration_mode=closed or email_verified, or set workflow_governance=off`; fixture `closed_local_app(tmp_path, closed_local_settings) -> SyncASGITestClient` in `tests/unit/web/conftest.py` with `client.app.state.phase3_engine` and `client.app.state.settings`; tests `TestReadinessAuthAndReport::test_r11_refuses_governance_under_open_local_registration`, `::test_r11_derives_from_the_registration_mode_setting`, `::test_report_is_not_ready_under_r11_and_names_only_auth_mode`, `::test_closed_local_app_is_a_supported_governance_configuration`, `::test_the_shared_route_fixture_sits_in_the_r11_combination` in `tests/unit/web/test_readiness.py` (class at HEAD :910).
  - I1: tests `test_r14_refuses_when_the_day_total_reaches_the_identity_cap`, `test_r14_derives_from_the_identity_policy_row_not_a_constant`, `test_r14_day_rolls_over_at_utc_midnight_on_the_database_clock` (parametrised over the clock) in `tests/unit/web/coordination/test_quota_authority.py`; `test_quota_exceeded_row_carries_dimension_cap_ceiling_and_usage` in `tests/unit/web/auth/test_audit.py`; `test_composer_quota_refusal_writes_quota_exceeded_before_returning`, `test_composer_quota_audit_derives_from_the_policy_row` in `tests/unit/web/sessions/test_token_usage_adapters.py`; the PostgreSQL file `tests/testcontainer/web/test_quota_authority_postgres.py`.
  - I2: tests `test_r13_refuses_when_the_identity_total_across_sessions_would_exceed_the_cap`, `test_r13_derives_from_the_identity_policy_row_not_a_constant` in `tests/unit/web/coordination/test_storage_quota_authority.py`; `test_upload_create_blob_is_refused_before_any_byte_reaches_disk`, `test_upload_routes_answer_413_naming_cap_ceiling_and_usage`, `test_reserve_inline_custody_admits_through_r13`, `test_run_output_finalize_marks_the_over_quota_output_error_and_removes_its_bytes`, `test_fork_refuses_before_the_copy_loop_leaves_a_child_row` (each parametrised over `CAP_CASES`, the identity's `storage_bytes` row), `test_guided_settlement_inline_custody_admits_through_r13`, `test_unlinked_output_finalize_facet_admits_through_r13`, `test_fork_copy_reservation_admits_through_r13_when_usage_grows_after_the_pre_check` in `tests/unit/web/blobs/test_storage_quota_sites.py`; `test_storage_quota_exceeded_row_names_the_storage_dimension` in `tests/unit/web/auth/test_audit.py`.
  - I3: routes `POST /api/sessions/{session_id}/approvals` (201, body `ApprovalRequestBody(state_id, approver_identity_id, note)`), `POST /api/approvals/{approval_id}/decide` (body `ApprovalDecisionBody(decision, note)`), error envelope `{"detail": {"error_type": "approval_already_decided", "detail": <str>, "current_state": <str>}}`, and the 404 envelope `{"detail": {"error_type": "approval_not_found", "detail": <str>}}` that `decide` returns to a caller who is neither the addressed approver nor the requester, writing no audit row (I3 decision 7); `ApprovalView` fields `approval_id`, `decision`, `request_note`, `decision_note`, `decided_by_identity_id`, `decision_seen_at`; `approval_requested` metadata keys `approver_identity_id`, `note`, and `approval_decided` metadata keys `decision`, `note`, rows anchored on `identity_id` = requester / deciding actor; `_state_data(work_dir: Path, session_id: str) -> CompositionStateData` in `tests/integration/web/workflow/test_approvals.py`; tests `test_r2_through_the_real_binding_tuple` (same file), `test_execute_refuses_without_an_approved_matching_binding`, `test_r2_derives_from_the_binding_tuple` (`tests/unit/web/coordination/test_r2_execute_gate.py`), `test_r2_derives_from_every_binding_field`, `test_author_cannot_be_approver`, `test_decide_refuses_the_author_even_with_an_approver_role`, `test_only_the_addressed_approver_may_decide`, `test_decide_guards_on_decision_not_on_decided_at` (`tests/unit/web/coordination/test_approval_authority.py`), `test_request_refusals_are_409_with_the_closed_code`, `test_only_the_addressed_approver_decides_and_a_repeat_decision_gets_the_current_state` (`tests/unit/web/workflow/test_approval_routes.py`), `test_the_losing_concurrent_decider_gets_the_current_state_and_writes_nothing` (`tests/testcontainer/web/test_approval_decide_race_postgres.py`); router registration of `create_approvals_router()` in `web/app.py`.
  - I4: tests `test_request_refuses_the_author_as_reviewer`, `test_attest_refuses_the_author_and_the_check_backs_it`, `test_attest_snapshots_the_author_and_closes_the_open_request` in `tests/unit/web/coordination/test_review_authority.py`; PostgreSQL file `tests/testcontainer/web/test_review_authority_postgres.py`.
  - I5: tests `test_the_publisher_cannot_curate_their_own_entry`, `test_the_curator_is_not_publisher_rule_holds_in_the_schema`, `test_accept_makes_the_entry_browseable_and_records_the_curator` in `tests/unit/web/coordination/test_library_authority.py`; PostgreSQL file `tests/testcontainer/web/test_library_postgres.py`.
  - I6: every `auth_events.metadata_json` row written by `AuthAuditRecorder` carries `"compartment_id"` (via `_CompartmentStampedAuthAudit`), filled from `WebSettings.compartment_id` by `AuthAuditRecorder.from_settings`.
  - I7: test `test_a_seeded_cycle_terminates_and_never_returns_the_caller` in `tests/unit/web/coordination/test_workflow_scope_reader.py`.
  - I9: routes `GET /api/workflow/mailbox/summary` → `MailboxSummaryResponse(governance, roles, approvals_to_decide, reviews_to_attest, decisions_unseen)`, `GET /api/workflow/mailbox/inbox` → `{approvals: [ApprovalView], reviews: [ReviewRequestView]}`, `GET /api/workflow/mailbox/sent` → `{approvals: [ApprovalView], reviews: [ReviewSentView]}`, `POST /api/workflow/mailbox/{approval_id}/seen` → `ApprovalView`; `create_mailbox_router()` registered in `web/app.py` (I9 Step 15); tests `test_sent_and_seen_round_trip_clears_the_unseen_count`, `test_summary_counts_each_folder_and_reports_live_roles` in `tests/unit/web/workflow/test_mailbox_routes.py`; PostgreSQL file `tests/testcontainer/web/test_workflow_mailbox_postgres.py`.
  - HEAD: `create_app(settings: WebSettings | None = None)` (`src/elspeth/web/app.py:1132`), `app.state.session_engine` (:1515), `app.state.auth_provider` (:1570); `LocalAuthProvider.create_user(user_id, password, display_name, email=None, *, email_verified=True)` (`src/elspeth/web/auth/local.py:421`); `POST /api/auth/login` answering an `AuthenticationError` with 401 and `detail=exc.detail` (`src/elspeth/web/auth/routes.py:302-337`); `AccessPending` detail `Account is pending — awaiting administrator approval` (`src/elspeth/web/auth/models.py:69-81`); `POST /api/auth/admin/roles` (`identity_admin_routes.py:588`), `POST /api/auth/admin/relationships` (:670), `POST /api/auth/admin/relationships/{relationship_id}/revoke` (:701); `_refused` → 409 `{"refusal": <snake_case class name>, "detail": str(exc)}` (:371-384); refusal messages `RoleForbiddenForIdentity` "that role cannot be held by this identity" (`identity_authority.py:194`), `ApproverRoleRequired` "the overseeing identity must hold an active approver role" (:219), `RelationshipCycle` "the relationship would close a cycle in the org tree" (:224); `_refuse_role_conflict` (:1075, called at :2080, :2314, :2605) and `_ACTIVE_INCOMING_EDGES` (:757, read at :2741 and :2751 inside `assert_relationship`'s ancestor walk :2745-2760); `_is_active` treats any non-NULL `revoked_at` as inactive (:889-892); `_dormant_since` strictly longer than the window, from `max(last_login_at, activated_at)` (:941-978), evaluated for every ACTIVE row with no provider gate ("R9 IS NOT EXCLUDED FOR LOCAL AUTH, unlike R3", :1612-1624); the local `_record_dormant` callback (`src/elspeth/web/app.py:1071-1085`) calls `AuthAuditRecorder.record_identity_dormant`, which writes `event_type="identity_disabled"` (`src/elspeth/web/auth/audit.py:802-843`); `WebSettings.identity_dormancy_days` default 90 (`config.py:556`), `auth_rate_limit_per_minute` default 20 (:436); tables `identities_table`, `identity_roles_table` (`sessions/models.py:3406`), `identity_relationships_table` (:3465-3510, partial unique active incoming edge), `approvals_table` (:3588-3656, CHECK `ck_approvals_author_is_not_approver` :3647-3650); `auth_events_table` (`core/landscape/schema.py:2542`, `metadata_json` Text); `LandscapeDB.from_url(url).read_only_connection()`; `ensure_test_identity(conn, *, identity_id, provider="local")` (`tests/fixtures/identities.py:10`, inserts an ACTIVE row); `_make_session(conn, *, session_id, user_id="test_user", auth_provider_type="local", title="test session", created_at=None, updated_at=None)` (`tests/unit/web/conftest.py:73`); `_lifespan_test_client(app)` (`tests/integration/web/conftest.py:291`), `_save_composition_state_with_compose_authority(service, session_id, state, *, provenance)` (:86); `SyncASGITestClient.get/post(url, **kwargs)` forwarding `headers=`/`json=` to httpx (`tests/unit/web/_sync_asgi_client.py:14`); HEAD tests in `tests/unit/web/coordination/test_identity_authority.py`: `test_relationship_refusals` (:914), `test_a_cycle_is_refused_inside_the_transaction` (:945), `test_r8_refuses_admin_and_workload_roles_in_both_grant_orders` (:857), `test_activating_an_admin_holder_with_a_workload_role_is_refused` (:695), `test_dormancy_re_pends_the_identity_and_writes_the_disable_event` (:1632), `test_dormancy_reads_the_container_window_rather_than_a_hardcoded_number` (:1659), `test_dormancy_of_the_last_active_human_admin_leaves_them_active_and_admitted` (:1794), `test_dormancy_re_pends_an_admin_who_is_not_the_last_one` (:1817).
- Produces (in `tests/unit/web/workflow/test_governance_suite_inventory.py`):
  - `Pin(path: str, test: str)` — frozen slotted dataclass; `test` is `test_name` or `TestClass::test_name`.
  - `GovernanceItem(item: str, fire: Sequence[Pin], mutation: Sequence[Pin])` — frozen slotted dataclass; every instance in the module passes tuples.
  - `inventory_problems(items: Sequence[GovernanceItem], root: Path) -> list[str]` — the reader; returns one line per hollow entry, in item order.
  - `GOVERNANCE_SUITE: Final[Sequence[GovernanceItem]]` (24 items), `SPEC_ITEMS: Final[frozenset[str]]`, `NOT_TESTED: Final[Mapping[str, str]]` (R10, attestation as a ledger, flex teams, each with the spec's reason), `REPO_ROOT: Final[Path]`.

- [ ] **Step 1: Record the mutation-authority manifest gate before any file exists.**

This task adds no production writer, so the manifest's writer-row edit does not
apply. Record the gate's summary now so Step 11 can prove the task did not
move it. Measure, do not remember: ancestors disagree on what this gate
reports at rest (the gate XFAILs on a clean HEAD, measured 2026-09-14 on 818d04577; I1 Step 15 records that baseline and I3 Step 35 now expects the same XFAIL), so the only honest comparison is before against after on this tree.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py -n 0 -rxXfE > /tmp/i10-lane-manifest-before.log 2>&1; echo exit=$?`

Expected: `exit=0` (an XFAIL does not fail the run). Keep the log; Step 11 reads it.

- [ ] **Step 2: Write the failing inventory.**

Create `tests/unit/web/workflow/test_governance_suite_inventory.py`:

```python
"""The workflow-governance suite as a fail-closed inventory (Task I10).

Spec: docs/specs/2026-09-02-pluggable-sso-design.md §Testing → Workflow
governance (:1276-1311). Every refusal that actually refuses (R2, R7, R8, R9,
R11, R13, R14) needs a fire test AND a mutation-derivation test, and the
section adds the round trip, the concurrent-decide loser, both quota
dimensions and the token dimension's UTC-midnight rollover, the separation
rules as violations and a seeded pre-existing cycle.

Tasks I1-I9 each wrote the pair for the refusal they own, next to the code
they own. This module does not re-type those tests. It names every one, by
module path and test name, and fails when a named test is missing, skipped
or xfailed, or is pinned as both the fire and the mutation half without being
parametrised over the authority row. Deleting or renaming a governance test
turns this red; so does dropping an item the spec lists.

The reader parses each module with ``ast``. ``test_the_reader_*`` run it
against planted modules that must pass and planted modules that must fail,
so a reader that silently matches nothing cannot pass.
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
# ``pytest.mark.skip`` is a prefix of ``pytest.mark.skipif``, so both are caught.
_SKIP_MARKS: Final = ("pytest.mark.skip", "pytest.mark.xfail")


@dataclass(frozen=True, slots=True)
class Pin:
    """One test: a repo-relative module path and ``test_name`` or ``TestClass::test_name``."""

    path: str
    test: str


@dataclass(frozen=True, slots=True)
class GovernanceItem:
    """One spec item and the tests that fire it and derive it from its authority."""

    item: str
    fire: Sequence[Pin]
    mutation: Sequence[Pin]


@dataclass(frozen=True, slots=True)
class _ModuleTests:
    marks_by_test: Mapping[str, Sequence[str]]
    module_marks: str


def _decorator_marks(decorators: Sequence[ast.expr]) -> list[str]:
    return [ast.unparse(decorator.func if isinstance(decorator, ast.Call) else decorator) for decorator in decorators]


def _read_module(source: Path) -> _ModuleTests:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    marks_by_test: dict[str, list[str]] = {}
    module_marks = ""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
            marks_by_test[node.name] = _decorator_marks(node.decorator_list)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            class_marks = _decorator_marks(node.decorator_list)
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and member.name.startswith("test_"):
                    marks_by_test[f"{node.name}::{member.name}"] = [*class_marks, *_decorator_marks(member.decorator_list)]
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets):
            module_marks = ast.unparse(node.value)
    return _ModuleTests(marks_by_test=marks_by_test, module_marks=module_marks)


def inventory_problems(items: Sequence[GovernanceItem], root: Path) -> list[str]:
    """Every way ``items`` is hollow against the tree under ``root``, one line each, in item order."""
    problems: list[str] = []
    modules: dict[str, _ModuleTests | None] = {}

    def module_for(path: str) -> _ModuleTests | None:
        if path not in modules:
            source = root / path
            modules[path] = _read_module(source) if source.is_file() else None
        return modules[path]

    for entry in items:
        for kind, pins in (("fire", entry.fire), ("mutation", entry.mutation)):
            if not pins:
                problems.append(f"{entry.item} [{kind}]: no test pinned")
            for pin in pins:
                module = module_for(pin.path)
                if module is None:
                    problems.append(f"{entry.item} [{kind}]: module {pin.path} does not exist")
                elif pin.test not in module.marks_by_test:
                    problems.append(f"{entry.item} [{kind}]: test '{pin.path}::{pin.test}' not found in collected ids")
                elif any(skip in text for skip in _SKIP_MARKS for text in (*module.marks_by_test[pin.test], module.module_marks)):
                    problems.append(f"{entry.item} [{kind}]: test '{pin.path}::{pin.test}' is skipped or xfailed")
        for pin in entry.fire:
            if pin not in entry.mutation:
                continue
            module = module_for(pin.path)
            if module is not None and pin.test in module.marks_by_test and "pytest.mark.parametrize" not in module.marks_by_test[pin.test]:
                problems.append(f"{entry.item}: '{pin.path}::{pin.test}' is pinned as both fire and mutation but is not parametrised")
    return problems


# ── Module paths ───────────────────────────────────────────────────────────

_SUITE: Final = "tests/integration/web/workflow/test_governance_refusals.py"
_SCHEMA_VIOLATIONS: Final = "tests/unit/web/workflow/test_governance_schema_violations.py"
_APPROVALS_INTEGRATION: Final = "tests/integration/web/workflow/test_approvals.py"
_R2_GATE: Final = "tests/unit/web/coordination/test_r2_execute_gate.py"
_APPROVAL_AUTHORITY: Final = "tests/unit/web/coordination/test_approval_authority.py"
_APPROVAL_ROUTES: Final = "tests/unit/web/workflow/test_approval_routes.py"
_DECIDE_RACE_PG: Final = "tests/testcontainer/web/test_approval_decide_race_postgres.py"
_MAILBOX_ROUTES: Final = "tests/unit/web/workflow/test_mailbox_routes.py"
_IDENTITY_AUTHORITY: Final = "tests/unit/web/coordination/test_identity_authority.py"
_SCOPE_READER: Final = "tests/unit/web/coordination/test_workflow_scope_reader.py"
_READINESS: Final = "tests/unit/web/test_readiness.py"
_STORAGE_AUTHORITY: Final = "tests/unit/web/coordination/test_storage_quota_authority.py"
_STORAGE_SITES: Final = "tests/unit/web/blobs/test_storage_quota_sites.py"
_AUTH_AUDIT: Final = "tests/unit/web/auth/test_audit.py"
_QUOTA_AUTHORITY: Final = "tests/unit/web/coordination/test_quota_authority.py"
_TOKEN_ADAPTERS: Final = "tests/unit/web/sessions/test_token_usage_adapters.py"
_REVIEW_AUTHORITY: Final = "tests/unit/web/coordination/test_review_authority.py"
_LIBRARY_AUTHORITY: Final = "tests/unit/web/coordination/test_library_authority.py"

# ── The spec's list, item by item (sso-design.md :1283-1311) ───────────────

SPEC_ITEMS: Final = frozenset(
    {
        "R2: no run without an approved row matching the real compiled binding",
        "R7: an approver edge that would close a cycle",
        "R7: an approver edge whose overseer lacks an active approver role",
        "R7: a seeded pre-existing cycle in identity_relationships",
        "R8: admin granted to a workload-role holder",
        "R8: a workload role granted to an admin holder",
        "R9: login of an identity dormant past the container window",
        "R9: D34 last-active-admin exemption",
        "R11: governance under local auth with open registration",
        "R11: the suite runs against a closed local deployment",
        "R13: storage over the identity cap or the container ceiling",
        "R13 site 1: multipart and inline upload",
        "R13 site 2: inline custody",
        "R13 site 3: run-output finalize",
        "R13 site 4: copy_blobs_for_fork",
        "R13: quota_exceeded row carries dimension, cap, ceiling and usage",
        "R14: an LLM call over tokens_per_day or the container ceiling",
        "R14: the day boundary is UTC midnight",
        "R14: quota_exceeded row carries dimension, cap, ceiling and usage",
        "round trip: request and decide with notes, requester sees both, badge clears",
        "concurrent decide: the losing decider gets the current state",
        "separation: author = approver",
        "separation: curator = publisher",
        "separation: reviewer = author",
    }
)

NOT_TESTED: Final[Mapping[str, str]] = {
    "R10 service-identity provenance": "no reachable path: no service-credential mechanism ships this sprint (spec :1304-1305)",
    "attestation as a ledger": (
        "attestation has no refusal to mutate, by its own design as a ledger (spec :1305-1306); "
        "its one separation rule, reviewer = author, is inventoried below"
    ),
    "flex teams": "a property of two live deployments, not of code (spec :1306-1307)",
}

GOVERNANCE_SUITE: Final[Sequence[GovernanceItem]] = (
    GovernanceItem(
        "R2: no run without an approved row matching the real compiled binding",
        fire=(
            Pin(_APPROVALS_INTEGRATION, "test_r2_through_the_real_binding_tuple"),
            Pin(_R2_GATE, "test_execute_refuses_without_an_approved_matching_binding"),
        ),
        mutation=(
            Pin(_R2_GATE, "test_r2_derives_from_the_binding_tuple"),
            Pin(_APPROVAL_AUTHORITY, "test_r2_derives_from_every_binding_field"),
        ),
    ),
    GovernanceItem(
        "R7: an approver edge that would close a cycle",
        fire=(
            Pin(_SUITE, "test_r7_refuses_an_edge_that_closes_a_cycle_through_the_route"),
            Pin(_IDENTITY_AUTHORITY, "test_a_cycle_is_refused_inside_the_transaction"),
        ),
        mutation=(Pin(_SUITE, "test_r7_cycle_refusal_derives_from_the_active_edge_rows"),),
    ),
    GovernanceItem(
        "R7: an approver edge whose overseer lacks an active approver role",
        fire=(
            Pin(_SUITE, "test_r7_refuses_an_overseer_without_an_active_approver_role"),
            Pin(_IDENTITY_AUTHORITY, "test_relationship_refusals"),
        ),
        mutation=(Pin(_SUITE, "test_r7_approver_role_refusal_derives_from_the_role_row"),),
    ),
    GovernanceItem(
        "R7: a seeded pre-existing cycle in identity_relationships",
        fire=(
            Pin(_SUITE, "test_a_seeded_pre_existing_cycle_is_reported_on_the_next_insert"),
            Pin(_SCOPE_READER, "test_a_seeded_cycle_terminates_and_never_returns_the_caller"),
        ),
        mutation=(Pin(_SUITE, "test_the_seeded_cycle_report_derives_from_the_seeded_edge_row"),),
    ),
    GovernanceItem(
        "R8: admin granted to a workload-role holder",
        fire=(
            Pin(_SUITE, "test_r8_refuses_admin_after_a_workload_role_through_the_route"),
            Pin(_IDENTITY_AUTHORITY, "test_r8_refuses_admin_and_workload_roles_in_both_grant_orders"),
            Pin(_IDENTITY_AUTHORITY, "test_activating_an_admin_holder_with_a_workload_role_is_refused"),
        ),
        mutation=(Pin(_SUITE, "test_r8_admits_admin_beside_a_revoked_workload_row"),),
    ),
    GovernanceItem(
        "R8: a workload role granted to an admin holder",
        fire=(Pin(_SUITE, "test_r8_refuses_a_workload_role_after_admin_through_the_route"),),
        mutation=(Pin(_SUITE, "test_r8_workload_refusal_derives_from_the_live_admin_row"),),
    ),
    GovernanceItem(
        "R9: login of an identity dormant past the container window",
        fire=(
            Pin(_SUITE, "test_r9_re_pends_a_dormant_local_identity_at_login"),
            Pin(_IDENTITY_AUTHORITY, "test_dormancy_re_pends_the_identity_and_writes_the_disable_event"),
        ),
        mutation=(
            Pin(_SUITE, "test_r9_derives_from_the_last_login_row"),
            Pin(_IDENTITY_AUTHORITY, "test_dormancy_reads_the_container_window_rather_than_a_hardcoded_number"),
        ),
    ),
    GovernanceItem(
        "R9: D34 last-active-admin exemption",
        fire=(Pin(_IDENTITY_AUTHORITY, "test_dormancy_of_the_last_active_human_admin_leaves_them_active_and_admitted"),),
        mutation=(Pin(_IDENTITY_AUTHORITY, "test_dormancy_re_pends_an_admin_who_is_not_the_last_one"),),
    ),
    GovernanceItem(
        "R11: governance under local auth with open registration",
        fire=(
            Pin(_READINESS, "TestReadinessAuthAndReport::test_r11_refuses_governance_under_open_local_registration"),
            Pin(_READINESS, "TestReadinessAuthAndReport::test_report_is_not_ready_under_r11_and_names_only_auth_mode"),
        ),
        mutation=(Pin(_READINESS, "TestReadinessAuthAndReport::test_r11_derives_from_the_registration_mode_setting"),),
    ),
    GovernanceItem(
        "R11: the suite runs against a closed local deployment",
        fire=(
            Pin(_SUITE, "test_r11_refuses_the_open_twin_of_the_suite_deployment"),
            Pin(_READINESS, "TestReadinessAuthAndReport::test_the_shared_route_fixture_sits_in_the_r11_combination"),
        ),
        mutation=(
            Pin(_SUITE, "test_the_suite_deployment_is_admitted_by_r11"),
            Pin(_READINESS, "TestReadinessAuthAndReport::test_closed_local_app_is_a_supported_governance_configuration"),
        ),
    ),
    GovernanceItem(
        "R13: storage over the identity cap or the container ceiling",
        fire=(Pin(_STORAGE_AUTHORITY, "test_r13_refuses_when_the_identity_total_across_sessions_would_exceed_the_cap"),),
        mutation=(Pin(_STORAGE_AUTHORITY, "test_r13_derives_from_the_identity_policy_row_not_a_constant"),),
    ),
    GovernanceItem(
        "R13 site 1: multipart and inline upload",
        fire=(
            Pin(_STORAGE_SITES, "test_upload_create_blob_is_refused_before_any_byte_reaches_disk"),
            Pin(_STORAGE_SITES, "test_upload_routes_answer_413_naming_cap_ceiling_and_usage"),
        ),
        mutation=(
            Pin(_STORAGE_SITES, "test_upload_create_blob_is_refused_before_any_byte_reaches_disk"),
            Pin(_STORAGE_SITES, "test_upload_routes_answer_413_naming_cap_ceiling_and_usage"),
        ),
    ),
    GovernanceItem(
        "R13 site 2: inline custody",
        fire=(
            Pin(_STORAGE_SITES, "test_reserve_inline_custody_admits_through_r13"),
            Pin(_STORAGE_SITES, "test_guided_settlement_inline_custody_admits_through_r13"),
        ),
        mutation=(Pin(_STORAGE_SITES, "test_reserve_inline_custody_admits_through_r13"),),
    ),
    GovernanceItem(
        "R13 site 3: run-output finalize",
        fire=(
            Pin(_STORAGE_SITES, "test_run_output_finalize_marks_the_over_quota_output_error_and_removes_its_bytes"),
            Pin(_STORAGE_SITES, "test_unlinked_output_finalize_facet_admits_through_r13"),
        ),
        mutation=(Pin(_STORAGE_SITES, "test_run_output_finalize_marks_the_over_quota_output_error_and_removes_its_bytes"),),
    ),
    GovernanceItem(
        "R13 site 4: copy_blobs_for_fork",
        fire=(
            Pin(_STORAGE_SITES, "test_fork_refuses_before_the_copy_loop_leaves_a_child_row"),
            Pin(_STORAGE_SITES, "test_fork_copy_reservation_admits_through_r13_when_usage_grows_after_the_pre_check"),
        ),
        mutation=(Pin(_STORAGE_SITES, "test_fork_refuses_before_the_copy_loop_leaves_a_child_row"),),
    ),
    GovernanceItem(
        "R13: quota_exceeded row carries dimension, cap, ceiling and usage",
        fire=(Pin(_AUTH_AUDIT, "test_storage_quota_exceeded_row_names_the_storage_dimension"),),
        mutation=(Pin(_STORAGE_SITES, "test_upload_routes_answer_413_naming_cap_ceiling_and_usage"),),
    ),
    GovernanceItem(
        "R14: an LLM call over tokens_per_day or the container ceiling",
        fire=(Pin(_QUOTA_AUTHORITY, "test_r14_refuses_when_the_day_total_reaches_the_identity_cap"),),
        mutation=(Pin(_QUOTA_AUTHORITY, "test_r14_derives_from_the_identity_policy_row_not_a_constant"),),
    ),
    # Tokens only: storage is a level with no daily window (spec :77, :1127-1130), and R14 alone names UTC midnight (:1168).
    GovernanceItem(
        "R14: the day boundary is UTC midnight",
        fire=(Pin(_QUOTA_AUTHORITY, "test_r14_day_rolls_over_at_utc_midnight_on_the_database_clock"),),
        mutation=(Pin(_QUOTA_AUTHORITY, "test_r14_day_rolls_over_at_utc_midnight_on_the_database_clock"),),
    ),
    GovernanceItem(
        "R14: quota_exceeded row carries dimension, cap, ceiling and usage",
        fire=(
            Pin(_AUTH_AUDIT, "test_quota_exceeded_row_carries_dimension_cap_ceiling_and_usage"),
            Pin(_TOKEN_ADAPTERS, "test_composer_quota_refusal_writes_quota_exceeded_before_returning"),
        ),
        mutation=(Pin(_TOKEN_ADAPTERS, "test_composer_quota_audit_derives_from_the_policy_row"),),
    ),
    GovernanceItem(
        "round trip: request and decide with notes, requester sees both, badge clears",
        fire=(
            Pin(_SUITE, "test_round_trip_request_decide_see_and_the_badge_clears"),
            Pin(_MAILBOX_ROUTES, "test_sent_and_seen_round_trip_clears_the_unseen_count"),
        ),
        mutation=(
            Pin(_SUITE, "test_the_badge_derives_from_the_seen_stamp_row"),
            Pin(_MAILBOX_ROUTES, "test_summary_counts_each_folder_and_reports_live_roles"),
        ),
    ),
    GovernanceItem(
        "concurrent decide: the losing decider gets the current state",
        fire=(
            Pin(_DECIDE_RACE_PG, "test_the_losing_concurrent_decider_gets_the_current_state_and_writes_nothing"),
            Pin(_APPROVAL_ROUTES, "test_only_the_addressed_approver_decides_and_a_repeat_decision_gets_the_current_state"),
            Pin(_SUITE, "test_round_trip_request_decide_see_and_the_badge_clears"),
        ),
        mutation=(Pin(_APPROVAL_AUTHORITY, "test_decide_guards_on_decision_not_on_decided_at"),),
    ),
    GovernanceItem(
        "separation: author = approver",
        fire=(
            Pin(_APPROVAL_AUTHORITY, "test_author_cannot_be_approver"),
            Pin(_APPROVAL_AUTHORITY, "test_decide_refuses_the_author_even_with_an_approver_role"),
            Pin(_APPROVAL_ROUTES, "test_request_refusals_are_409_with_the_closed_code"),
            Pin(_SCHEMA_VIOLATIONS, "test_author_is_approver_is_refused_by_the_schema"),
        ),
        mutation=(
            Pin(_APPROVAL_AUTHORITY, "test_only_the_addressed_approver_may_decide"),
            Pin(_SCHEMA_VIOLATIONS, "test_the_author_is_approver_check_derives_from_the_approver_column"),
        ),
    ),
    GovernanceItem(
        "separation: curator = publisher",
        fire=(
            Pin(_LIBRARY_AUTHORITY, "test_the_publisher_cannot_curate_their_own_entry"),
            Pin(_LIBRARY_AUTHORITY, "test_the_curator_is_not_publisher_rule_holds_in_the_schema"),
        ),
        mutation=(Pin(_LIBRARY_AUTHORITY, "test_accept_makes_the_entry_browseable_and_records_the_curator"),),
    ),
    GovernanceItem(
        "separation: reviewer = author",
        fire=(
            Pin(_REVIEW_AUTHORITY, "test_request_refuses_the_author_as_reviewer"),
            Pin(_REVIEW_AUTHORITY, "test_attest_refuses_the_author_and_the_check_backs_it"),
        ),
        mutation=(Pin(_REVIEW_AUTHORITY, "test_attest_snapshots_the_author_and_closes_the_open_request"),),
    ),
)


def test_every_governance_item_has_a_live_fire_and_mutation_test() -> None:
    problems = inventory_problems(GOVERNANCE_SUITE, REPO_ROOT)
    if problems:
        pytest.fail("governance suite inventory is hollow:\n" + "\n".join(problems), pytrace=False)


def test_the_inventory_names_every_item_the_spec_lists_and_nothing_else() -> None:
    names = [entry.item for entry in GOVERNANCE_SUITE]
    assert len(names) == len(set(names)), "an item is inventoried twice"
    assert set(names) == SPEC_ITEMS
    assert set(NOT_TESTED) == {"R10 service-identity provenance", "attestation as a ledger", "flex teams"}
    assert set(NOT_TESTED).isdisjoint(names)


# ── Controls: the reader against planted modules ───────────────────────────

_CONTROL_MODULE: Final = textwrap.dedent(
    """
    import pytest


    def test_plain():
        pass


    @pytest.mark.parametrize("cap", [100, 101])
    def test_parametrised(cap):
        pass


    @pytest.mark.skip(reason="control")
    def test_skipped():
        pass


    class TestGrouped:
        def test_member(self):
            pass
    """
)
_SKIPPED_CONTROL_MODULE: Final = textwrap.dedent(
    """
    import pytest

    pytestmark = pytest.mark.skip(reason="control")


    def test_inside_a_skipped_module():
        pass
    """
)


def _control_root(tmp_path: Path) -> Path:
    (tmp_path / "control_tests.py").write_text(_CONTROL_MODULE, encoding="utf-8")
    (tmp_path / "control_skipped_module.py").write_text(_SKIPPED_CONTROL_MODULE, encoding="utf-8")
    return tmp_path


def test_the_reader_admits_real_pairs_class_members_and_a_parametrised_self_pair(tmp_path: Path) -> None:
    """Known-positive control: a reader that refuses everything fails here."""
    root = _control_root(tmp_path)
    plain = Pin("control_tests.py", "test_plain")
    member = Pin("control_tests.py", "TestGrouped::test_member")
    parametrised = Pin("control_tests.py", "test_parametrised")
    items = (
        GovernanceItem("real pair", fire=(plain, member), mutation=(parametrised,)),
        GovernanceItem("parametrised self pair", fire=(parametrised,), mutation=(parametrised,)),
    )
    assert inventory_problems(items, root) == []


def test_the_reader_reports_every_way_an_entry_can_be_hollow(tmp_path: Path) -> None:
    """Known-negative control: a reader that silently matches nothing fails here."""
    root = _control_root(tmp_path)
    plain = Pin("control_tests.py", "test_plain")
    items = (
        GovernanceItem("missing test", fire=(Pin("control_tests.py", "test_absent"),), mutation=(plain,)),
        GovernanceItem("missing module", fire=(Pin("no_such_module.py", "test_plain"),), mutation=(plain,)),
        GovernanceItem(
            "skipped",
            fire=(Pin("control_tests.py", "test_skipped"),),
            mutation=(Pin("control_skipped_module.py", "test_inside_a_skipped_module"),),
        ),
        GovernanceItem("unparametrised self pair", fire=(plain,), mutation=(plain,)),
        GovernanceItem("no mutation", fire=(plain,), mutation=()),
    )
    assert inventory_problems(items, root) == [
        "missing test [fire]: test 'control_tests.py::test_absent' not found in collected ids",
        "missing module [fire]: module no_such_module.py does not exist",
        "skipped [fire]: test 'control_tests.py::test_skipped' is skipped or xfailed",
        "skipped [mutation]: test 'control_skipped_module.py::test_inside_a_skipped_module' is skipped or xfailed",
        "unparametrised self pair: 'control_tests.py::test_plain' is pinned as both fire and mutation but is not parametrised",
        "no mutation [mutation]: no test pinned",
    ]
```

- [ ] **Step 3: Run the inventory to verify it fails for exactly the two modules this task has not written yet.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_governance_suite_inventory.py -n 0 > /tmp/i10-lane-inventory-red.log 2>&1; echo exit=$?`

Expected: `exit=1`, summary `1 failed, 3 passed`. The one failure is
`test_every_governance_item_has_a_live_fire_and_mutation_test`, whose text is
`Failed: governance suite inventory is hollow:` followed by exactly these 19
lines:

```text
R7: an approver edge that would close a cycle [fire]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R7: an approver edge that would close a cycle [mutation]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R7: an approver edge whose overseer lacks an active approver role [fire]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R7: an approver edge whose overseer lacks an active approver role [mutation]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R7: a seeded pre-existing cycle in identity_relationships [fire]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R7: a seeded pre-existing cycle in identity_relationships [mutation]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R8: admin granted to a workload-role holder [fire]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R8: admin granted to a workload-role holder [mutation]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R8: a workload role granted to an admin holder [fire]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R8: a workload role granted to an admin holder [mutation]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R9: login of an identity dormant past the container window [fire]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R9: login of an identity dormant past the container window [mutation]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R11: the suite runs against a closed local deployment [fire]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
R11: the suite runs against a closed local deployment [mutation]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
round trip: request and decide with notes, requester sees both, badge clears [fire]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
round trip: request and decide with notes, requester sees both, badge clears [mutation]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
concurrent decide: the losing decider gets the current state [fire]: module tests/integration/web/workflow/test_governance_refusals.py does not exist
separation: author = approver [fire]: module tests/unit/web/workflow/test_governance_schema_violations.py does not exist
separation: author = approver [mutation]: module tests/unit/web/workflow/test_governance_schema_violations.py does not exist
```

Any additional line names an ancestor test that is missing, renamed or
skipped on this branch: an ancestor task has not landed or drifted from its
block. Stop and report that line; never edit a `Pin` to make the inventory
fit the tree.

- [ ] **Step 4: Write the author = approver schema-violation pair on the closed deployment.**

Create `tests/unit/web/workflow/test_governance_schema_violations.py`:

```python
"""Author = approver as a schema violation (sso-design.md §Testing → Workflow governance, :1299-1300).

I3 pins the authority's refusal (``ApprovalAuthorIsApprover``) and the route's
409. The sessions schema also carries ``ck_approvals_author_is_not_approver``
(sessions/models.py:3647-3650), which is what holds if a future writer skips
the authority; nothing pinned it. The pair runs on I8's ``closed_local_app``
engine, the deployment every governance test must run against (R11).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.conftest import _make_session

from elspeth.web.sessions.models import approvals_table

_SESSION_ID = "governed-session"
_BINDING_JSON = {
    "config_hash": "1" * 64,
    "canonical_version": "sha256-rfc8785-v1",
    "runtime_val_manifest_sha256": "3" * 64,
    "openrouter_catalog_sha256": "2" * 64,
    "binding_generation_fingerprint": "c" * 64,
    "policy_hash": "a" * 64,
}


@pytest.fixture
def governed_engine(closed_local_app: Any) -> Any:
    settings = closed_local_app.app.state.settings
    assert (settings.registration_mode, settings.workflow_governance) == ("closed", "on")
    engine = closed_local_app.app.state.phase3_engine
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="bob")
        _make_session(conn, session_id=_SESSION_ID, user_id="alice")
    return engine


def _insert_open_request(engine: Any, *, approval_id: str, requested_by: str, approver: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(approvals_table).values(
                approval_id=approval_id,
                session_id=_SESSION_ID,
                state_id="state-1",
                binding_json=_BINDING_JSON,
                requested_by_identity_id=requested_by,
                approver_identity_id=approver,
                requested_at=datetime.now(UTC),
            )
        )


def _approval_ids(engine: Any) -> list[str]:
    with engine.connect() as conn:
        return [row.approval_id for row in conn.execute(select(approvals_table.c.approval_id)).all()]


def test_author_is_approver_is_refused_by_the_schema(governed_engine: Any) -> None:
    """Fire: a row naming the requester as its own approver never lands, whatever wrote it."""
    with pytest.raises(IntegrityError, match="ck_approvals_author_is_not_approver"):
        _insert_open_request(governed_engine, approval_id="self-approval", requested_by="alice", approver="alice")
    assert _approval_ids(governed_engine) == []


def test_the_author_is_approver_check_derives_from_the_approver_column(governed_engine: Any) -> None:
    """Mutation-derivation: the same row with only ``approver_identity_id`` changed is admitted."""
    _insert_open_request(governed_engine, approval_id="peer-approval", requested_by="alice", approver="bob")
    assert _approval_ids(governed_engine) == ["peer-approval"]
```

- [ ] **Step 5: Write the route suite on the closed `create_app` deployment.**

Create `tests/integration/web/workflow/test_governance_refusals.py`:

```python
"""Workflow-governance refusals on the closed deployment (sso-design.md §Testing → Workflow governance, :1276-1311).

Task I10. Every ``governed`` test runs on ``create_app`` with
``auth_provider="local"``, ``registration_mode="closed"``,
``workflow_governance="on"`` and a compartment: the deployment R11 admits
(:1115-1124), asserted by the fixture before the app is built. Callers
authenticate with real local-auth bearer tokens, because the identity-admin
routes read the token themselves (``_require_identity_admin`` calls
``get_current_user`` directly, identity_admin_routes.py:341-351) and a
dependency override would never reach it.

What only this module proves (the rest of the suite is inventoried in
tests/unit/web/workflow/test_governance_suite_inventory.py):

* R7 and R8 through the routes the deployment exposes, each with a
  mutation-derivation twin that changes an authority row and watches the
  verdict flip;
* a cycle seeded past R7 by direct SQL is refused on the next insert whose
  ancestor walk passes through it, not silently admitted;
* R9 through the real local login (app.py:1094-1110 wires the dormancy
  window into local admission);
* the approval round trip, the losing second decider, and the Landscape
  ``auth_events`` rows carrying the compartment marking.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, insert, select, update
from tests.fixtures.identities import ensure_test_identity
from tests.integration.web.conftest import _lifespan_test_client, _save_composition_state_with_compose_authority
from tests.integration.web.workflow.test_approvals import _state_data
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient

from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import auth_events_table
from elspeth.web.config import WebSettings
from elspeth.web.readiness import ReadinessCheck, _check_auth_mode
from elspeth.web.sessions.models import approvals_table, identities_table, identity_relationships_table, identity_roles_table

pytestmark = pytest.mark.integration

_PASSWORD = "password123"
_COMPARTMENT = "test-compartment"
_DORMANCY_DAYS = 90
_REQUEST_NOTE = "please review the passthrough before Friday"
_DECISION_NOTE = "name an owner for the output before this runs"
_GOVERNANCE_ADMITTED = "local authentication configured; workflow governance on"
_R11_DETAIL = (
    "workflow_governance=on is refused with auth_provider=local and registration_mode=open (R11): "
    "one person can hold many local identities, so every author-is-not-approver rule is defeatable; "
    "set registration_mode=closed or email_verified, or set workflow_governance=off"
)
_CYCLE = {"refusal": "relationship_cycle", "detail": "the relationship would close a cycle in the org tree"}
_ROLE_FORBIDDEN = {"refusal": "role_forbidden_for_identity", "detail": "that role cannot be held by this identity"}
_APPROVER_REQUIRED = {"refusal": "approver_role_required", "detail": "the person who approves must hold an active Approver role; give them that role first, then assign them"}
# root and erin administer; bob and carol approve; anna/bert/cleo form the R7 chain;
# piet/quin carry the seeded cycle; dave holds a workload role; alice and milo hold none.
_PEOPLE = ("root", "alice", "bob", "carol", "anna", "bert", "cleo", "dave", "erin", "piet", "quin", "milo")
_GRANTS = (
    ("root", "admin"),
    ("erin", "admin"),
    ("bob", "approver"),
    ("carol", "approver"),
    ("anna", "approver"),
    ("bert", "approver"),
    ("cleo", "approver"),
    ("piet", "approver"),
    ("quin", "approver"),
    ("dave", "user"),
)


@dataclass(frozen=True, slots=True)
class _Governed:
    client: TestClient
    engine: Engine
    landscape_url: str
    data_dir: Path


def _settings(tmp_path: Path) -> WebSettings:
    return WebSettings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        auth_provider="local",
        registration_mode="closed",
        workflow_governance="on",
        compartment_id=_COMPARTMENT,
        identity_dormancy_days=_DORMANCY_DAYS,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )


def _role_id(identity_id: str, role: str) -> str:
    return f"role-{identity_id}-{role}"


@pytest.fixture
def governed(tmp_path: Path) -> Iterator[_Governed]:
    from elspeth.web.app import create_app

    for directory in ("blobs", "outputs", "runs"):
        (tmp_path / directory).mkdir()
    (tmp_path / "payloads").mkdir(mode=0o700)
    settings = _settings(tmp_path)
    # R11 guard: this module never runs against a deployment readiness refuses.
    assert _check_auth_mode(settings) == ReadinessCheck("auth_mode", True, _GOVERNANCE_ADMITTED)
    assert settings.landscape_url is not None
    app = create_app(settings=settings)
    now = datetime.now(UTC)
    # Closed registration admits only identities that are already active
    # (auth/local.py:1159-1167), so every person is seeded active under their
    # username (a local subject is the username).
    with app.state.session_engine.begin() as conn:
        for identity_id in _PEOPLE:
            ensure_test_identity(conn, identity_id=identity_id)
        for identity_id, role in _GRANTS:
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=_role_id(identity_id, role), identity_id=identity_id, role=role, granted_at=now, granted_by_identity_id="root"
                )
            )
    for identity_id in _PEOPLE:
        app.state.auth_provider.create_user(identity_id, _PASSWORD, display_name=identity_id.title())
    with _lifespan_test_client(app) as client:
        yield _Governed(client=client, engine=app.state.session_engine, landscape_url=settings.landscape_url, data_dir=tmp_path)


def _login(governed: _Governed, username: str) -> Any:
    return governed.client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})


def _bearer(governed: _Governed, username: str) -> dict[str, str]:
    response = _login(governed, username)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _refusal(response: Any) -> dict[str, Any]:
    assert response.status_code == 409, response.text
    detail: dict[str, Any] = response.json()["detail"]
    return detail


def _edge(governed: _Governed, headers: dict[str, str], from_identity_id: str, to_identity_id: str) -> Any:
    return governed.client.post(
        "/api/auth/admin/relationships",
        headers=headers,
        json={"from_identity_id": from_identity_id, "to_identity_id": to_identity_id, "relationship_type": "approver"},
    )


def _grant(governed: _Governed, headers: dict[str, str], identity_id: str, role: str) -> Any:
    return governed.client.post("/api/auth/admin/roles", headers=headers, json={"identity_id": identity_id, "role": role})


def _active_edges(engine: Engine) -> set[tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(identity_relationships_table.c.from_identity_id, identity_relationships_table.c.to_identity_id).where(
                identity_relationships_table.c.revoked_at.is_(None)
            )
        ).all()
    return {(row.from_identity_id, row.to_identity_id) for row in rows}


def _seed_edge(engine: Engine, relationship_id: str, from_identity_id: str, to_identity_id: str) -> None:
    """Write an edge past R7, the way data predating the guard would exist."""
    with engine.begin() as conn:
        conn.execute(
            insert(identity_relationships_table).values(
                relationship_id=relationship_id,
                from_identity_id=from_identity_id,
                to_identity_id=to_identity_id,
                relationship_type="approver",
                asserted_by_identity_id="root",
                asserted_at=datetime.now(UTC),
            )
        )


def _live_roles(engine: Engine, identity_id: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(identity_roles_table.c.role).where(
                identity_roles_table.c.identity_id == identity_id, identity_roles_table.c.revoked_at.is_(None)
            )
        ).all()
    return {row.role for row in rows}


def _revoke_role_row(engine: Engine, identity_id: str, role: str) -> None:
    with engine.begin() as conn:
        result = conn.execute(
            update(identity_roles_table)
            .where(identity_roles_table.c.role_id == _role_id(identity_id, role))
            .values(revoked_at=datetime.now(UTC) - timedelta(hours=1))
        )
    assert result.rowcount == 1


def _identity_row(engine: Engine, identity_id: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(select(identities_table).where(identities_table.c.identity_id == identity_id)).one()


def _backdate_login(engine: Engine, identity_id: str, *, days: int) -> None:
    """Dormancy is measured on the database clock from max(last_login_at, activated_at) (identity_authority.py:941-978)."""
    stamped = datetime.now(UTC) - timedelta(days=days)
    with engine.begin() as conn:
        result = conn.execute(
            update(identities_table)
            .where(identities_table.c.identity_id == identity_id)
            .values(last_login_at=stamped, activated_at=stamped)
        )
    assert result.rowcount == 1


def _audit_rows(landscape_url: str) -> list[Any]:
    """``auth_events`` is a LANDSCAPE table (core/landscape/schema.py:2542): read the Landscape, not the sessions store."""
    with LandscapeDB.from_url(landscape_url) as db, db.read_only_connection() as conn:
        return list(conn.execute(select(auth_events_table).order_by(auth_events_table.c.occurred_at)).fetchall())


def _governed_session(governed: _Governed, headers: dict[str, str]) -> str:
    created = governed.client.post("/api/sessions", headers=headers, json={"title": "governed round trip"})
    assert created.status_code == 201, created.text
    session_id: str = created.json()["id"]
    asyncio.run(
        _save_composition_state_with_compose_authority(
            governed.client.app.state.session_service,
            UUID(session_id),
            _state_data(governed.data_dir, session_id),
            provenance="session_seed",
        )
    )
    return session_id


def _request_approval(governed: _Governed, headers: dict[str, str], session_id: str) -> str:
    requested = governed.client.post(
        f"/api/sessions/{session_id}/approvals",
        headers=headers,
        json={"state_id": None, "approver_identity_id": "bob", "note": _REQUEST_NOTE},
    )
    assert requested.status_code == 201, requested.text
    assert requested.json()["request_note"] == _REQUEST_NOTE
    approval_id: str = requested.json()["approval_id"]
    return approval_id


def _reject(governed: _Governed, headers: dict[str, str], approval_id: str) -> None:
    decided = governed.client.post(
        f"/api/approvals/{approval_id}/decide", headers=headers, json={"decision": "rejected", "note": _DECISION_NOTE}
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["decision"] == "rejected"


def _summary(governed: _Governed, headers: dict[str, str]) -> dict[str, Any]:
    response = governed.client.get("/api/workflow/mailbox/summary", headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _set_seen_stamp(engine: Engine, approval_id: str, value: datetime | None) -> None:
    with engine.begin() as conn:
        result = conn.execute(update(approvals_table).where(approvals_table.c.approval_id == approval_id).values(decision_seen_at=value))
    assert result.rowcount == 1


# ── R11: the suite's own deployment ────────────────────────────────────────


def test_r11_refuses_the_open_twin_of_the_suite_deployment(tmp_path: Path) -> None:
    """Fire: the suite deployment with ONLY registration_mode flipped to open is refused by readiness."""
    open_twin = _settings(tmp_path).model_copy(update={"registration_mode": "open"})
    assert _check_auth_mode(open_twin) == ReadinessCheck("auth_mode", False, _R11_DETAIL)


def test_the_suite_deployment_is_admitted_by_r11(tmp_path: Path) -> None:
    """Mutation-derivation: the same settings with the closed mode every test here uses are admitted."""
    assert _check_auth_mode(_settings(tmp_path)) == ReadinessCheck("auth_mode", True, _GOVERNANCE_ADMITTED)


# ── R7 ─────────────────────────────────────────────────────────────────────


def test_r7_refuses_an_edge_that_closes_a_cycle_through_the_route(governed: _Governed) -> None:
    """Fire: anna -> bert -> cleo exists; cleo -> anna would make anna her own ancestor."""
    root = _bearer(governed, "root")
    assert _edge(governed, root, "anna", "bert").status_code == 201
    assert _edge(governed, root, "bert", "cleo").status_code == 201
    assert _refusal(_edge(governed, root, "cleo", "anna")) == _CYCLE
    assert _active_edges(governed.engine) == {("anna", "bert"), ("bert", "cleo")}


def test_r7_cycle_refusal_derives_from_the_active_edge_rows(governed: _Governed) -> None:
    """Mutation-derivation: revoke bert -> cleo and the same cleo -> anna insert is admitted."""
    root = _bearer(governed, "root")
    assert _edge(governed, root, "anna", "bert").status_code == 201
    middle = _edge(governed, root, "bert", "cleo")
    assert middle.status_code == 201, middle.text
    revoked = governed.client.post(
        f"/api/auth/admin/relationships/{middle.json()['relationship_id']}/revoke",
        headers=root,
        json={"note": "bert no longer oversees cleo"},
    )
    assert revoked.status_code == 200, revoked.text
    admitted = _edge(governed, root, "cleo", "anna")
    assert admitted.status_code == 201, admitted.text
    assert _active_edges(governed.engine) == {("anna", "bert"), ("cleo", "anna")}


def test_r7_refuses_an_overseer_without_an_active_approver_role(governed: _Governed) -> None:
    """Fire: milo holds no approver role, so milo cannot oversee alice."""
    root = _bearer(governed, "root")
    assert _refusal(_edge(governed, root, "milo", "alice")) == _APPROVER_REQUIRED
    assert _active_edges(governed.engine) == set()


def test_r7_approver_role_refusal_derives_from_the_role_row(governed: _Governed) -> None:
    """Mutation-derivation: give milo an approver ROW and the same edge is admitted."""
    root = _bearer(governed, "root")
    with governed.engine.begin() as conn:
        conn.execute(
            insert(identity_roles_table).values(
                role_id=_role_id("milo", "approver"),
                identity_id="milo",
                role="approver",
                granted_at=datetime.now(UTC),
                granted_by_identity_id="root",
            )
        )
    admitted = _edge(governed, root, "milo", "alice")
    assert admitted.status_code == 201, admitted.text
    assert _active_edges(governed.engine) == {("milo", "alice")}


# ── R7: a seeded pre-existing cycle ────────────────────────────────────────


def test_a_seeded_pre_existing_cycle_is_reported_on_the_next_insert(governed: _Governed) -> None:
    """Fire: piet <-> quin predates R7. piet -> milo walks piet's ancestors, revisits a node already walked, and is refused.

    The walk (identity_authority.py:2745-2760) refuses when it revisits a node,
    so data R7 never saw is reported as ``relationship_cycle`` instead of being
    extended or looping.
    """
    _seed_edge(governed.engine, "seeded-piet-quin", "piet", "quin")
    _seed_edge(governed.engine, "seeded-quin-piet", "quin", "piet")
    root = _bearer(governed, "root")
    assert _refusal(_edge(governed, root, "piet", "milo")) == _CYCLE
    assert _active_edges(governed.engine) == {("piet", "quin"), ("quin", "piet")}


def test_the_seeded_cycle_report_derives_from_the_seeded_edge_row(governed: _Governed) -> None:
    """Mutation-derivation: revoke the seeded quin -> piet row and the same piet -> milo insert is admitted."""
    _seed_edge(governed.engine, "seeded-piet-quin", "piet", "quin")
    _seed_edge(governed.engine, "seeded-quin-piet", "quin", "piet")
    with governed.engine.begin() as conn:
        conn.execute(
            update(identity_relationships_table)
            .where(identity_relationships_table.c.relationship_id == "seeded-quin-piet")
            .values(revoked_at=datetime.now(UTC), revoked_by_identity_id="root")
        )
    root = _bearer(governed, "root")
    admitted = _edge(governed, root, "piet", "milo")
    assert admitted.status_code == 201, admitted.text
    assert _active_edges(governed.engine) == {("piet", "quin"), ("piet", "milo")}


# ── R8: both grant orders ──────────────────────────────────────────────────


def test_r8_refuses_admin_after_a_workload_role_through_the_route(governed: _Governed) -> None:
    """Fire, order 1: dave holds a live ``user`` row; admin is refused."""
    root = _bearer(governed, "root")
    assert _refusal(_grant(governed, root, "dave", "admin")) == _ROLE_FORBIDDEN
    assert _live_roles(governed.engine, "dave") == {"user"}


def test_r8_admits_admin_beside_a_revoked_workload_row(governed: _Governed) -> None:
    """Mutation-derivation, order 1: the case that must NOT refuse (spec :1096-1101).

    Seed dave's ``user`` row as revoked and the same grant lands, leaving a
    live ``admin`` row beside the revoked workload row.
    """
    _revoke_role_row(governed.engine, "dave", "user")
    root = _bearer(governed, "root")
    admitted = _grant(governed, root, "dave", "admin")
    assert admitted.status_code == 201, admitted.text
    assert _live_roles(governed.engine, "dave") == {"admin"}
    with governed.engine.connect() as conn:
        revoked = conn.execute(
            select(identity_roles_table.c.revoked_at).where(identity_roles_table.c.role_id == _role_id("dave", "user"))
        ).one()
    assert revoked.revoked_at is not None


def test_r8_refuses_a_workload_role_after_admin_through_the_route(governed: _Governed) -> None:
    """Fire, order 2: erin holds a live ``admin`` row; approver is refused."""
    root = _bearer(governed, "root")
    assert _refusal(_grant(governed, root, "erin", "approver")) == _ROLE_FORBIDDEN
    assert _live_roles(governed.engine, "erin") == {"admin"}


def test_r8_workload_refusal_derives_from_the_live_admin_row(governed: _Governed) -> None:
    """Mutation-derivation, order 2: revoke erin's admin ROW and the same approver grant lands."""
    _revoke_role_row(governed.engine, "erin", "admin")
    root = _bearer(governed, "root")
    admitted = _grant(governed, root, "erin", "approver")
    assert admitted.status_code == 201, admitted.text
    assert _live_roles(governed.engine, "erin") == {"approver"}


# ── R9 through the local login ─────────────────────────────────────────────


def test_r9_re_pends_a_dormant_local_identity_at_login(governed: _Governed) -> None:
    """Fire: alice last logged in one day past the window; the login is refused and she drops to pending."""
    _backdate_login(governed.engine, "alice", days=_DORMANCY_DAYS + 1)
    refused = _login(governed, "alice")
    assert refused.status_code == 401, refused.text
    assert refused.json() == {"detail": "Account is pending — awaiting administrator approval"}
    row = _identity_row(governed.engine, "alice")
    assert (row.access_state, row.disable_reason) == ("pending", "dormant")
    assert "identity_disabled" in {event.event_type for event in _audit_rows(governed.landscape_url) if event.identity_id == "alice"}


def test_r9_derives_from_the_last_login_row(governed: _Governed) -> None:
    """Mutation-derivation: the same identity with its stored login one day INSIDE the window is admitted."""
    _backdate_login(governed.engine, "alice", days=_DORMANCY_DAYS - 1)
    admitted = _login(governed, "alice")
    assert admitted.status_code == 200, admitted.text
    row = _identity_row(governed.engine, "alice")
    assert (row.access_state, row.disable_reason) == ("active", None)
    assert "identity_disabled" not in {event.event_type for event in _audit_rows(governed.landscape_url) if event.identity_id == "alice"}


# ── The round trip ─────────────────────────────────────────────────────────


def test_round_trip_request_decide_see_and_the_badge_clears(governed: _Governed) -> None:
    """Request with a note, decide with a note, the requester sees both, the badge clears (spec :1293-1294)."""
    client = governed.client
    alice = _bearer(governed, "alice")
    bob = _bearer(governed, "bob")
    carol = _bearer(governed, "carol")
    session_id = _governed_session(governed, alice)
    approval_id = _request_approval(governed, alice, session_id)

    # The approver's badge counts the open request; the requester has no news yet.
    assert _summary(governed, bob)["approvals_to_decide"] == 1
    assert _summary(governed, alice)["decisions_unseen"] == 0
    inbox = client.get("/api/workflow/mailbox/inbox", headers=bob)
    assert inbox.status_code == 200, inbox.text
    assert [(row["approval_id"], row["request_note"]) for row in inbox.json()["approvals"]] == [(approval_id, _REQUEST_NOTE)]

    _reject(governed, bob, approval_id)

    # The losing half of the decide guard: the addressed approver decides again after the decision.
    late = client.post(f"/api/approvals/{approval_id}/decide", headers=bob, json={"decision": "approved", "note": None})
    assert late.status_code == 409, late.text
    assert late.json()["detail"]["error_type"] == "approval_already_decided"
    assert late.json()["detail"]["current_state"] == "rejected"
    # A live approver the request was not addressed to learns nothing about it (I3 decision 7).
    hidden = client.post(f"/api/approvals/{approval_id}/decide", headers=carol, json={"decision": "approved", "note": None})
    assert hidden.status_code == 404, hidden.text
    assert hidden.json()["detail"]["error_type"] == "approval_not_found"

    assert _summary(governed, bob)["approvals_to_decide"] == 0
    assert _summary(governed, alice)["decisions_unseen"] == 1
    sent = client.get("/api/workflow/mailbox/sent", headers=alice)
    assert sent.status_code == 200, sent.text
    (row,) = sent.json()["approvals"]
    assert (row["approval_id"], row["decision"], row["decided_by_identity_id"], row["request_note"], row["decision_note"]) == (
        approval_id,
        "rejected",
        "bob",
        _REQUEST_NOTE,
        _DECISION_NOTE,
    )

    seen = client.post(f"/api/workflow/mailbox/{approval_id}/seen", headers=alice)
    assert seen.status_code == 200, seen.text
    assert seen.json()["decision_seen_at"] is not None
    assert _summary(governed, alice)["decisions_unseen"] == 0

    # R4: both rows were written before the responses; bob's repeat and carol's hidden decide wrote none.
    rows = [event for event in _audit_rows(governed.landscape_url) if event.event_type.startswith("approval_")]
    assert [(event.event_type, event.identity_id) for event in rows] == [("approval_requested", "alice"), ("approval_decided", "bob")]
    requested_metadata, decided_metadata = (json.loads(event.metadata_json) for event in rows)
    assert (requested_metadata["approver_identity_id"], requested_metadata["note"], requested_metadata["compartment_id"]) == (
        "bob",
        _REQUEST_NOTE,
        _COMPARTMENT,
    )
    assert (decided_metadata["decision"], decided_metadata["note"], decided_metadata["compartment_id"]) == (
        "rejected",
        _DECISION_NOTE,
        _COMPARTMENT,
    )


def test_the_badge_derives_from_the_seen_stamp_row(governed: _Governed) -> None:
    """Mutation-derivation: the requester's unseen count follows ``approvals.decision_seen_at`` and nothing else."""
    alice = _bearer(governed, "alice")
    bob = _bearer(governed, "bob")
    approval_id = _request_approval(governed, alice, _governed_session(governed, alice))
    _reject(governed, bob, approval_id)
    seen = governed.client.post(f"/api/workflow/mailbox/{approval_id}/seen", headers=alice)
    assert seen.status_code == 200, seen.text
    assert _summary(governed, alice)["decisions_unseen"] == 0
    _set_seen_stamp(governed.engine, approval_id, None)
    assert _summary(governed, alice)["decisions_unseen"] == 1
    _set_seen_stamp(governed.engine, approval_id, datetime.now(UTC))
    assert _summary(governed, alice)["decisions_unseen"] == 0
```

- [ ] **Step 6: Run the two new modules.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_governance_schema_violations.py tests/integration/web/workflow/test_governance_refusals.py -n 0 > /tmp/i10-lane-new-modules.log 2>&1; echo exit=$?`

Expected: `exit=0`, `18 passed` (2 in the schema module, 16 in the route
suite). Diagnosis for the reds an executor can meet, none of which is fixed
by loosening an assertion:

- A route test failing at `_bearer` with 401 means closed registration did
  not admit the seeded identity: check the identity row is `active` and that
  `create_user` ran before `_lifespan_test_client` (the I3 Step 29 order).
- `test_round_trip_request_decide_see_and_the_badge_clears` failing at
  `requested_metadata["compartment_id"]` with `KeyError` means I6's
  `_CompartmentStampedAuthAudit` is not on this branch: I6 has not landed.
- `test_r9_re_pends_a_dormant_local_identity_at_login` returning 200 means
  local admission no longer passes `identity_dormancy_days`: re-measure
  `src/elspeth/web/app.py` `_admit_identity` before touching the test.

- [ ] **Step 7: Prove the new fire tests go red when their guard is removed (positive-control mutants).**

The mutants live outside the tree and are loaded as pytest plugins, so no
repository file is edited and nothing needs restoring.

```bash
mkdir -p /tmp/i10-mutants && cat > /tmp/i10-mutants/i10_mutant_r8.py <<'PY'
"""I10 control: R8 removed. The two R8 fire tests must fail; the two mutation twins must still pass."""

from elspeth.web.coordination import identity_authority


def _no_conflict(*, kind, role, held):
    return None


def pytest_configure(config):
    identity_authority._refuse_role_conflict = _no_conflict
PY
cat > /tmp/i10-mutants/i10_mutant_r7.py <<'PY'
"""I10 control: the R7 ancestor walk blinded. The two cycle fire tests must fail; their mutation twins must still pass."""

from sqlalchemy import bindparam, false, select

from elspeth.web.coordination import identity_authority
from elspeth.web.sessions.models import identity_relationships_table


def pytest_configure(config):
    identity_authority._ACTIVE_INCOMING_EDGES = select(identity_relationships_table.c.from_identity_id).where(
        identity_relationships_table.c.to_identity_id == bindparam("to_identity_id"),
        identity_relationships_table.c.relationship_type == bindparam("relationship_type"),
        false(),
    )
PY
echo exit=$?
```

Expected: `exit=0`.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && PYTHONPATH="/tmp/i10-mutants${PYTHONPATH:+:$PYTHONPATH}" pytest tests/integration/web/workflow/test_governance_refusals.py -n 0 -p i10_mutant_r8 -k r8 > /tmp/i10-lane-mutant-r8.log 2>&1; echo exit=$?`

Expected: `exit=1`, `2 failed, 2 passed, 12 deselected`; the failures are
exactly `test_r8_refuses_admin_after_a_workload_role_through_the_route` and
`test_r8_refuses_a_workload_role_after_admin_through_the_route`, each at
`assert response.status_code == 409` with `assert 201 == 409`.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && PYTHONPATH="/tmp/i10-mutants${PYTHONPATH:+:$PYTHONPATH}" pytest tests/integration/web/workflow/test_governance_refusals.py -n 0 -p i10_mutant_r7 -k cycle > /tmp/i10-lane-mutant-r7.log 2>&1; echo exit=$?`

Expected: `exit=1`, `2 failed, 2 passed, 12 deselected`; the failures are
exactly `test_r7_refuses_an_edge_that_closes_a_cycle_through_the_route` and
`test_a_seeded_pre_existing_cycle_is_reported_on_the_next_insert`, each with
`assert 201 == 409`. A mutant run that exits 0 means the test does not reach
the guard it names: fix the test, not the mutant.

- [ ] **Step 8: Run the inventory; it must now pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_governance_suite_inventory.py -n 0 > /tmp/i10-lane-inventory-green.log 2>&1; echo exit=$?`

Expected: `exit=0`, `4 passed`.

- [ ] **Step 9: Run the whole governance suite the inventory names.**

This is the default parallel run (`addopts` carries `-n 12`), because the
selection spans eighteen modules including the 2,367-line identity-authority
file; `-n 0` stays reserved for single ids.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_governance_suite_inventory.py tests/unit/web/workflow/test_governance_schema_violations.py tests/integration/web/workflow/test_governance_refusals.py tests/integration/web/workflow/test_approvals.py tests/unit/web/coordination/test_r2_execute_gate.py tests/unit/web/coordination/test_approval_authority.py tests/unit/web/workflow/test_approval_routes.py tests/unit/web/workflow/test_mailbox_routes.py tests/unit/web/coordination/test_identity_authority.py tests/unit/web/coordination/test_workflow_scope_reader.py tests/unit/web/test_readiness.py tests/unit/web/coordination/test_storage_quota_authority.py tests/unit/web/blobs/test_storage_quota_sites.py tests/unit/web/auth/test_audit.py tests/unit/web/coordination/test_quota_authority.py tests/unit/web/sessions/test_token_usage_adapters.py tests/unit/web/coordination/test_review_authority.py tests/unit/web/coordination/test_library_authority.py > /tmp/i10-lane-governance-suite.log 2>&1; echo exit=$?`

Expected: `exit=0`. A red id is re-run alone with `-n 0` before it is
attributed; this task edited none of the modules except the three it
created, so a red outside those three is an ancestor defect to report, not
an I10 fix.

- [ ] **Step 10: Run the workflow PostgreSQL proofs the inventory depends on.**

Docker is required, and `-m testcontainer` is required or the selection is
empty and pytest exits 5. The concurrent-decide loser is I3's file; the others
are the dialect proofs of the quota, review, library and mailbox refusals the
inventory names.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/testcontainer/web/test_approval_decide_race_postgres.py tests/testcontainer/web/test_quota_authority_postgres.py tests/testcontainer/web/test_review_authority_postgres.py tests/testcontainer/web/test_library_postgres.py tests/testcontainer/web/test_workflow_mailbox_postgres.py -m testcontainer -n 0 > /tmp/i10-lane-pg.log 2>&1; echo exit=$?`

Expected: `exit=0`; the log shows both parametrisations of
`test_the_losing_concurrent_decider_gets_the_current_state_and_writes_nothing`
passed.

- [ ] **Step 11: Prove the mutation-authority manifest gate did not move.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py -n 0 -rxXfE > /tmp/i10-lane-manifest-after.log 2>&1; echo exit=$?`

Expected: `exit=0`. Then compare the two summaries line for line (outcome
lines carry the gate's counters in their reasons; timings are excluded):

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python - <<'PY'
from pathlib import Path


def outcome_lines(name: str) -> list[str]:
    lines = Path(name).read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.startswith(("XFAIL", "XPASS", "FAILED", "ERROR"))]


before = outcome_lines("/tmp/i10-lane-manifest-before.log")
after = outcome_lines("/tmp/i10-lane-manifest-after.log")
# Control: the before-log must be a real pytest run, or an empty list proves nothing.
assert "test_session_db_mutation_authority.py" in Path("/tmp/i10-lane-manifest-before.log").read_text(encoding="utf-8")
print("before", len(before), "after", len(after))
assert before == after, (before, after)
PY
echo exit=$?
```

Expected: `exit=0`. Any difference means a production writer changed while
this task ran (another lane on the shared checkout): stop and find it; this
task adds none.

- [ ] **Step 12: Lint the three files and run the whole-tree test gates that scan tests.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff check tests/unit/web/workflow/test_governance_suite_inventory.py tests/unit/web/workflow/test_governance_schema_violations.py tests/integration/web/workflow/test_governance_refusals.py > /tmp/i10-lane-ruff.log 2>&1; echo exit=$?`

Expected: `exit=0`.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff format --check tests/unit/web/workflow/test_governance_suite_inventory.py tests/unit/web/workflow/test_governance_schema_violations.py tests/integration/web/workflow/test_governance_refusals.py > /tmp/i10-lane-ruff-format.log 2>&1; echo exit=$?`

Expected: `exit=0` (on a non-zero exit run `ruff format` over exactly those three files and re-run the check).

The masquerade gate pins attribute probes in tests too. None of the three
files calls `getattr` or `hasattr`, so the baseline must not move:

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/elspeth_lints/test_masquerade_gate.py -n 0 > /tmp/i10-lane-masquerade.log 2>&1; echo exit=$?`

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m elspeth_lints.rules.masquerade.seed_baseline --check > /tmp/i10-lane-masquerade-check.log 2>&1; echo exit=$?`

Expected: both `exit=0`. The static direct-writer gate
(`tests/unit/web/sessions/test_static_direct_writers.py`) guards only
`chat_messages` and `composition_states` (`_TABLE_IDENTIFIER_TO_NAME`, :85-88);
these tests insert into `approvals`, `identity_roles` and
`identity_relationships` only, so it does not apply. mypy's gate stage covers
`src/` and `elspeth-lints/src/`, not tests.

- [ ] **Step 13: Run the full pre-merge gate, including the PostgreSQL job.**

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest,testcontainer > /tmp/i10-lane-gate-launch.log 2>&1; echo exit=$?`

Expected: `exit=0` (the detached child is launched). The launch log prints the
run directory and its wait loop; run that loop, then read `summary.txt` in the
run directory. Expected lines: `stage=ruff exit=0`, `stage=mypy exit=0`,
`stage=contracts exit=0`, `stage=lints exit=1 seconds=<n> fatal=0` (the deliberate
fail-closed corpus; compare its recorded count to the same stage on the base
commit, never to zero), `stage=pytest exit=0`, `stage=testcontainer exit=0`,
`after=<hash> frozen=yes`, and `RESULT=PASS`. Do not edit the tree while it
runs: `frozen=NO` marks the run as not evidence. A red `pytest` id in
`e2e/recovery`, `integration/pipeline` or `unit/engine/orchestrator`, or a
red `testcontainer` id outside the files in Step 10, is re-run alone
(`-n 0`, plus `-m testcontainer` for the latter) and diffed against the same
run on the base commit before it is attributed to this task.

- [ ] **Step 14: Check branch safety and commit by file pathspec.**

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/branch-safety-check.sh --intent commit`

Expected: no `[FAIL]` line (exit 0). Read every `[WARN]` line and accept it knowingly.

The three files are untracked, and a commit pathspec naming an untracked
path is refused, so record only that the three paths exist:

Run: `cd "$(git rev-parse --show-toplevel)" && git add -N tests/unit/web/workflow/test_governance_suite_inventory.py tests/unit/web/workflow/test_governance_schema_violations.py tests/integration/web/workflow/test_governance_refusals.py; echo exit=$?`

Expected: `exit=0`.

```bash
cd "$(git rev-parse --show-toplevel)" && git status --short && git commit -m "test(identity): workflow-governance fire and mutation suite" -- tests/unit/web/workflow/test_governance_suite_inventory.py tests/unit/web/workflow/test_governance_schema_violations.py tests/integration/web/workflow/test_governance_refusals.py
```

Run: `cd "$(git rev-parse --show-toplevel)" && git show --stat HEAD > /tmp/i10-lane-commit-stat.log 2>&1; echo exit=$?`

Expected: `exit=0`; the stat lists exactly those three files and reads `3 files changed`.
