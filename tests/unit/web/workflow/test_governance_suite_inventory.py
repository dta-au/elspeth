"""Fail-closed map from workflow-governance requirements to executable tests.

The inventory protects the spec's fire/mutation pairs from disappearing during
subsequent work. A separate positive and negative control checks its reader.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class Pin:
    path: str
    test: str


@dataclass(frozen=True, slots=True)
class Item:
    name: str
    fire: tuple[Pin, ...]
    mutation: tuple[Pin, ...]


@dataclass(frozen=True, slots=True)
class ModuleTests:
    marks: dict[str, tuple[str, ...]]
    module_marks: tuple[str, ...]


def _marks(decorators: list[ast.expr]) -> tuple[str, ...]:
    return tuple(ast.unparse(node.func if isinstance(node, ast.Call) else node) for node in decorators)


def _read_module(path: Path) -> ModuleTests:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    marks: dict[str, tuple[str, ...]] = {}
    module_marks: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets):
            module_marks.append(ast.unparse(node.value))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
            marks[node.name] = _marks(node.decorator_list)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            class_marks = _marks(node.decorator_list)
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and member.name.startswith("test_"):
                    marks[f"{node.name}::{member.name}"] = (*class_marks, *_marks(member.decorator_list))
    return ModuleTests(marks, tuple(module_marks))


def inventory_problems(items: tuple[Item, ...], root: Path) -> list[str]:
    """Return every missing, disabled, or hollow evidence pin."""
    problems: list[str] = []
    modules: dict[str, ModuleTests | None] = {}

    def module_for(path: str) -> ModuleTests | None:
        if path not in modules:
            source = root / path
            modules[path] = _read_module(source) if source.is_file() else None
        return modules[path]

    for item in items:
        for kind, pins in (("fire", item.fire), ("mutation", item.mutation)):
            if not pins:
                problems.append(f"{item.name} [{kind}]: no test pinned")
            for pin in pins:
                module = module_for(pin.path)
                if module is None:
                    problems.append(f"{item.name} [{kind}]: missing module {pin.path}")
                    continue
                if pin.test not in module.marks:
                    problems.append(f"{item.name} [{kind}]: missing test {pin.path}::{pin.test}")
                    continue
                marks = (*module.module_marks, *module.marks[pin.test])
                if any("pytest.mark.skip" in mark or "pytest.mark.xfail" in mark for mark in marks):
                    problems.append(f"{item.name} [{kind}]: disabled test {pin.path}::{pin.test}")
        for pin in set(item.fire).intersection(item.mutation):
            module = module_for(pin.path)
            if (
                module is not None
                and pin.test in module.marks
                and not any("pytest.mark.parametrize" in mark for mark in module.marks[pin.test])
            ):
                problems.append(f"{item.name}: shared pin is not parametrized {pin.path}::{pin.test}")
    return problems


APP = "tests/integration/web/workflow/test_governance_round_trip.py"
R2 = "tests/unit/web/coordination/test_r2_execute_gate.py"
APPROVAL = "tests/unit/web/coordination/test_approval_authority.py"
APPROVAL_ROUTES = "tests/unit/web/workflow/test_approval_routes.py"
APPROVAL_PG = "tests/testcontainer/web/test_approval_authority_postgres.py"
SCHEMA = "tests/unit/web/workflow/test_governance_schema_violations.py"
MAILBOX = "tests/unit/web/workflow/test_mailbox_routes.py"
IDENTITY = "tests/unit/web/coordination/test_identity_authority.py"
READINESS = "tests/unit/web/test_readiness.py"
STORAGE = "tests/unit/web/coordination/test_storage_quota_authority.py"
SITES = "tests/unit/web/blobs/test_storage_quota_sites.py"
AUTH_AUDIT = "tests/unit/web/auth/test_audit.py"
APPROVAL_AUDIT = "tests/unit/web/auth/test_approval_audit.py"
TOKENS = "tests/unit/web/coordination/test_quota_authority.py"
TOKEN_ADAPTERS = "tests/unit/web/sessions/test_token_usage_adapters.py"
REVIEW = "tests/unit/web/coordination/test_review_authority.py"
REVIEW_ROUTES = "tests/unit/web/workflow/test_review_routes.py"
LIBRARY = "tests/unit/web/coordination/test_library_authority.py"
LIBRARY_APP = "tests/integration/web/test_library_workflow.py"
CHAT = "tests/unit/web/sessions/test_routes.py"
YAML_INGRESS = "tests/unit/web/sessions/routes/composer/test_yaml_ingress.py"
EXPORT = "tests/unit/web/execution/test_export_marking.py"
EXPORT_CONTRACT = "tests/unit/contracts/test_audit_export_hashing.py"

CATALOG: Final[tuple[Item, ...]] = (
    Item(
        "R2 exact compiled binding and pre-run refusal",
        (Pin(APP, "test_non_addressed_approver_can_decide_and_admit_exact_run"), Pin(R2, "test_no_approval_is_a_persisted_refusal")),
        (
            Pin(R2, "test_exact_binding_admits_but_a_different_field_refuses"),
            Pin(R2, "test_approval_for_a_different_state_cannot_admit_this_run"),
            Pin(R2, "test_recovery_cannot_retarget_an_issued_permit_to_a_new_binding"),
        ),
    ),
    Item(
        "R2 later rejection blocks recovery",
        (Pin(APP, "test_later_rejection_retires_approval_and_blocks_run"),),
        (Pin(R2, "test_later_rejection_retirement_refuses_recovery_of_issued_permit"),),
    ),
    Item(
        "R7 cycle on insert",
        (Pin(IDENTITY, "test_a_cycle_is_refused_inside_the_transaction"),),
        (Pin(APP, "test_r7_cycle_refusal_follows_live_relationship_rows"),),
    ),
    Item(
        "R7 active approver role on overseeing identity",
        (Pin(IDENTITY, "test_relationship_refusals"),),
        (Pin(APP, "test_r7_unqualified_overseer_becomes_eligible_only_after_role_grant"),),
    ),
    Item(
        "R7 seeded pre-existing cycle",
        (Pin(APP, "test_r7_seeded_cycle_refuses_the_next_insert"),),
        (Pin(APP, "test_r7_seeded_cycle_is_refused_until_its_authority_row_is_revoked"),),
    ),
    Item(
        "R8 grant admin after workload",
        (Pin(IDENTITY, "test_r8_refuses_admin_and_workload_roles_in_both_grant_orders"),),
        (Pin(APP, "test_r8_admin_conflict_derives_from_live_workload_role"),),
    ),
    Item(
        "R8 grant workload after admin",
        (Pin(IDENTITY, "test_activating_an_admin_holder_with_a_workload_role_is_refused"),),
        (Pin(APP, "test_r8_workload_conflict_derives_from_live_admin_role"),),
    ),
    Item(
        "R9 login dormancy window",
        (Pin(IDENTITY, "test_dormancy_re_pends_the_identity_and_writes_the_disable_event"),),
        (
            Pin(APP, "test_r9_local_login_dormancy_depends_on_stored_window"),
            Pin(IDENTITY, "test_dormancy_reads_the_container_window_rather_than_a_hardcoded_number"),
        ),
    ),
    Item(
        "R9 last active human admin exemption",
        (Pin(IDENTITY, "test_dormancy_of_the_last_active_human_admin_leaves_them_active_and_admitted"),),
        (Pin(IDENTITY, "test_dormancy_re_pends_an_admin_who_is_not_the_last_one"),),
    ),
    Item(
        "R11 open-local governance refusal",
        (Pin(READINESS, "TestReadinessAuthAndReport::test_r11_refuses_open_local_governance"),),
        (Pin(READINESS, "TestReadinessAuthAndReport::test_r11_admits_non_open_local_governance"),),
    ),
    Item(
        "R11 closed-local real-app execution",
        (Pin(APP, "test_non_addressed_approver_can_decide_and_admit_exact_run"),),
        (Pin(APP, "test_later_rejection_retires_approval_and_blocks_run"),),
    ),
    Item(
        "R13 identity and container storage authority",
        (Pin(STORAGE, "test_storage_admission_uses_identity_and_container_policy_rows"),),
        (Pin(STORAGE, "test_storage_admission_uses_identity_and_container_policy_rows"),),
    ),
    Item(
        "R13 upload admission",
        (Pin(SITES, "test_upload_http_refusal_carries_identity_storage_measurement"),),
        (Pin(SITES, "test_upload_reservation_admits_identity_growth_before_file_write"),),
    ),
    Item(
        "R13 inline custody",
        (Pin(SITES, "test_composer_inline_custody_uses_shared_reservation_gate"),),
        (Pin(SITES, "test_guided_full_inline_settlement_admits_on_held_connection"),),
    ),
    Item(
        "R13 run-output finalize",
        (Pin(SITES, "test_run_output_finalization_removes_over_quota_bytes"),),
        (Pin(SITES, "test_unlinked_output_finalize_admits_actual_bytes"),),
    ),
    Item(
        "R13 library-fork copy",
        (Pin(SITES, "test_fork_preflight_refuses_before_copy_loop"),),
        (Pin(SITES, "test_fork_per_copy_reservation_rechecks_growth"),),
    ),
    Item(
        "R13 storage audit measurements",
        (Pin(AUTH_AUDIT, "test_storage_quota_exceeded_row_names_storage_and_measured_usage"),),
        (Pin(SITES, "test_upload_http_refusal_carries_identity_storage_measurement"),),
    ),
    Item(
        "R14 token identity and container ceilings",
        (Pin(TOKENS, "test_r14_refuses_when_the_day_total_reaches_the_identity_cap"),),
        (
            Pin(TOKENS, "test_r14_derives_from_the_identity_policy_row_not_a_constant"),
            Pin(TOKENS, "test_r14_container_ceiling_aggregates_usage_across_identities"),
        ),
    ),
    Item(
        "R14 UTC midnight",
        (Pin(TOKENS, "test_r14_day_rolls_over_at_utc_midnight_on_the_database_clock"),),
        (Pin(TOKENS, "test_r14_day_rolls_over_at_utc_midnight_on_the_database_clock"),),
    ),
    Item(
        "R14 token audit measurements",
        (
            Pin(AUTH_AUDIT, "test_quota_exceeded_row_carries_dimension_cap_ceiling_and_usage"),
            Pin(TOKEN_ADAPTERS, "test_composer_quota_refusal_writes_quota_exceeded_before_returning"),
        ),
        (Pin(TOKEN_ADAPTERS, "test_composer_quota_audit_derives_from_the_policy_row"),),
    ),
    Item(
        "approval note, decision, seen badge round trip",
        (Pin(APP, "test_non_addressed_approver_can_decide_and_admit_exact_run"),),
        (Pin(MAILBOX, "test_sent_seen_and_directory_follow_current_grants"),),
    ),
    Item(
        "concurrent decision loser",
        (Pin(APPROVAL_PG, "test_two_eligible_approvers_race_to_decide_one_open_request"),),
        (Pin(APPROVAL_ROUTES, "test_request_inbox_cover_decision_and_sent_round_trip"),),
    ),
    Item(
        "separation author and approver",
        (
            Pin(APPROVAL_ROUTES, "test_revoked_approver_and_author_cannot_decide"),
            Pin(SCHEMA, "test_author_is_approver_is_refused_by_the_schema"),
        ),
        (
            Pin(APPROVAL, "test_any_active_non_author_approver_can_decide_and_addressed_is_first"),
            Pin(SCHEMA, "test_author_approver_constraint_admits_a_distinct_identity"),
        ),
    ),
    Item(
        "separation publisher and curator",
        (Pin(LIBRARY, "test_curator_grant_is_live_and_publisher_cannot_curate"),),
        (Pin(LIBRARY_APP, "test_library_round_trip_preserves_frozen_provenance_and_audit"),),
    ),
    Item(
        "separation author and reviewer",
        (Pin(REVIEW, "test_request_refuses_bad_owner_state_and_reviewer"),),
        (Pin(APP, "test_review_attestation_requires_an_open_exact_state_request"),),
    ),
    Item(
        "role eligible non-author approver",
        (Pin(APP, "test_non_addressed_approver_can_decide_and_admit_exact_run"),),
        (
            Pin(APPROVAL, "test_any_active_non_author_approver_can_decide_and_addressed_is_first"),
            Pin(MAILBOX, "test_role_based_inbox_orders_addressed_first_and_conceals_author"),
        ),
    ),
    Item(
        "later rejection retires approval with audit",
        (Pin(APP, "test_later_rejection_retires_approval_and_blocks_run"),),
        (
            Pin(APPROVAL_AUDIT, "test_rejection_audit_batch_rolls_back_when_later_supersession_insert_fails"),
            Pin(AUTH_AUDIT, "test_approval_supersession_writes_cause_and_trigger_to_landscape"),
        ),
    ),
    Item(
        "review attestation needs open exact-state request",
        (Pin(APP, "test_review_request_for_previous_state_cannot_authorize_new_state"),),
        (
            Pin(REVIEW_ROUTES, "test_open_request_for_another_state_does_not_authorize_attestation"),
            Pin(REVIEW, "test_attestation_requires_open_exact_state_request_and_remains_a_non_gating_ledger"),
        ),
    ),
    Item(
        "chat-pasted state ingress",
        (Pin(CHAT, "TestMessageRoutes::test_chat_paste_state_records_exact_ingress_on_created_state"),),
        (
            Pin(CHAT, "TestMessageRoutes::test_chat_paste_clarification_then_confirmation_retains_first_ingress"),
            Pin(YAML_INGRESS, "test_pasted_yaml_records_exact_text_hash_and_foreign_marking"),
        ),
    ),
    Item(
        "signed Landscape export compartment marking",
        (Pin(EXPORT, "test_signed_web_export_refuses_missing_operator_marking"),),
        (
            Pin(EXPORT, "test_operator_marking_is_injected_before_strict_model_validation"),
            Pin(EXPORT_CONTRACT, "test_compartment_identifier_is_accepted_at_derivation_boundaries"),
        ),
    ),
)

REQUIRED: Final = frozenset(
    {
        "R2 exact compiled binding and pre-run refusal",
        "R2 later rejection blocks recovery",
        "R7 cycle on insert",
        "R7 active approver role on overseeing identity",
        "R7 seeded pre-existing cycle",
        "R8 grant admin after workload",
        "R8 grant workload after admin",
        "R9 login dormancy window",
        "R9 last active human admin exemption",
        "R11 open-local governance refusal",
        "R11 closed-local real-app execution",
        "R13 identity and container storage authority",
        "R13 upload admission",
        "R13 inline custody",
        "R13 run-output finalize",
        "R13 library-fork copy",
        "R13 storage audit measurements",
        "R14 token identity and container ceilings",
        "R14 UTC midnight",
        "R14 token audit measurements",
        "approval note, decision, seen badge round trip",
        "concurrent decision loser",
        "separation author and approver",
        "separation publisher and curator",
        "separation author and reviewer",
        "role eligible non-author approver",
        "later rejection retires approval with audit",
        "review attestation needs open exact-state request",
        "chat-pasted state ingress",
        "signed Landscape export compartment marking",
    }
)

NOT_TESTED: Final = {
    "R10 service-credential provenance": "No service-credential admission mechanism ships with this workflow release.",
    "attestation as a non-gating ledger": "Attestation has no run-admission refusal; its author/reviewer rule and exact open request are pinned above.",
    "flex teams": "Flex-team behavior needs two live deployments and is outside the code-level suite.",
}


def test_required_governance_items_have_live_fire_and_mutation_tests() -> None:
    names = [item.name for item in CATALOG]
    assert len(names) == len(set(names)), "duplicate inventory item"
    assert set(names) == REQUIRED
    assert set(NOT_TESTED) == {"R10 service-credential provenance", "attestation as a non-gating ledger", "flex teams"}
    assert set(NOT_TESTED).isdisjoint(REQUIRED)
    problems = inventory_problems(CATALOG, ROOT)
    if problems:
        pytest.fail("governance inventory is hollow:\n" + "\n".join(problems), pytrace=False)


def test_inventory_reader_positive_and_negative_controls(tmp_path: Path) -> None:
    (tmp_path / "control.py").write_text(
        "import pytest\n"
        "def test_plain(): pass\n"
        "@pytest.mark.parametrize('case', [1, 2])\n"
        "def test_parameterized(case): pass\n"
        "@pytest.mark.skip(reason='control')\n"
        "def test_skipped(): pass\n"
        "class TestGrouped:\n    def test_member(self): pass\n",
        encoding="utf-8",
    )
    (tmp_path / "skipped.py").write_text(
        "import pytest\npytestmark = pytest.mark.xfail(reason='control')\ndef test_marked(): pass\n",
        encoding="utf-8",
    )
    plain = Pin("control.py", "test_plain")
    parameterized = Pin("control.py", "test_parameterized")
    member = Pin("control.py", "TestGrouped::test_member")
    assert (
        inventory_problems(
            (Item("positive", (plain, member), (parameterized,)), Item("self pair", (parameterized,), (parameterized,))),
            tmp_path,
        )
        == []
    )
    problems = inventory_problems(
        (
            Item("missing module", (Pin("absent.py", "test_plain"),), (plain,)),
            Item("missing test", (Pin("control.py", "test_absent"),), (plain,)),
            Item("disabled", (Pin("control.py", "test_skipped"),), (Pin("skipped.py", "test_marked"),)),
            Item("same unparameterized", (plain,), (plain,)),
            Item("empty", (), ()),
        ),
        tmp_path,
    )
    assert len(problems) == 7
    assert "missing module" in problems[0]
    assert "missing test" in problems[1]
    assert "disabled test" in problems[2] and "disabled test" in problems[3]
    assert "shared pin is not parametrized" in problems[4]
    assert "no test pinned" in problems[5] and "no test pinned" in problems[6]
