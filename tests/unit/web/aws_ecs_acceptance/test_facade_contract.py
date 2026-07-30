"""Permanent contract tests for the AWS ECS acceptance facade."""

from __future__ import annotations

import argparse
import ast
import io
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.web import aws_ecs_acceptance as acceptance

EXPECTED_COMMANDS = {
    "approval-require-current",
    "approval-verify",
    "capture",
    "cleanup-evidence-finalize",
    "compatibility-record-validate",
    "control-manifest",
    "evidence-export-receipt",
    "extract-exec-receipt",
    "gate-ledger",
    "orphan-sweep",
    "provision-storage",
    "receipt-store",
    "sanitize-evidence",
    "scenario-load",
    "scenario-namespace",
    "validate-task-definition-policy",
    "verify-api",
    "verify-bedrock",
    "verify-bedrock-guardrails",
    "verify-connection-budget",
    "verify-local-auth",
    "verify-operator-telemetry",
    "verify-payloads",
    "verify-s3",
    "verify-textract",
}

EXPECTED_PUBLIC_EXPORTS = {
    "AWSOperatorMetricEmitter",
    "AWSOperatorTelemetryQueries",
    "AcceptanceCheckError",
    "AcceptanceCredentials",
    "AcceptanceHttpClient",
    "AcceptanceHttpError",
    "AcceptanceInputError",
    "AcceptancePolicy",
    "AcceptanceState",
    "AcceptanceStateError",
    "AuditSentinel",
    "CLEANUP_SURFACES",
    "CONNECT_TIMEOUT_SECONDS",
    "EVIDENCE_KINDS",
    "ExistingLandscapeLifecycleAudit",
    "FIXED_INPUT_BYTES",
    "FORBIDDEN_AWS_OVERRIDE_ENV",
    "MAX_BLOB_RESPONSE_BYTES",
    "MAX_CONTROL_DOCUMENT_BYTES",
    "MAX_EXEC_RECEIPT_CHARS",
    "MAX_EXEC_STREAM_BYTES",
    "MAX_JSON_RESPONSE_BYTES",
    "MAX_STATE_FILE_BYTES",
    "OperatorTelemetryAcceptanceError",
    "OperatorTelemetryEvidence",
    "OperatorTelemetryOutageEvidence",
    "OrphanSweepClients",
    "PLUGIN_POLICY_ASSIGNMENT_NAMES",
    "POOL_TIMEOUT_SECONDS",
    "PublicApiLifecycleAudit",
    "READ_TIMEOUT_SECONDS",
    "RUN_POLL_DEADLINE_SECONDS",
    "RUN_POLL_INTERVAL_SECONDS",
    "SCENARIO_ASSIGNMENT_NAMES",
    "SanitizedResourceIdentity",
    "TUTORIAL_INPUT_BYTES",
    "TelemetryQueries",
    "TelemetrySentinelEmitter",
    "WRITE_TIMEOUT_SECONDS",
    "approval_require_current",
    "approval_verify",
    "build_canonical_tutorial_pipeline_yaml",
    "build_fixed_pipeline_yaml",
    "build_parser",
    "build_plugin_policy_acceptance",
    "capture",
    "cleanup_evidence_finalize",
    "control_manifest_bind_retained_evidence",
    "control_manifest_bind_scenario",
    "control_manifest_checkpoint_operator_evidence",
    "control_manifest_get",
    "control_manifest_init",
    "control_manifest_load_cleanup",
    "control_manifest_update",
    "control_manifest_validate",
    "create_evidence_export_receipt",
    "encode_exec_receipt",
    "extract_exec_receipt",
    "gate_ledger_bind_candidate",
    "gate_ledger_finalize",
    "gate_ledger_get",
    "gate_ledger_init",
    "gate_ledger_record",
    "gate_ledger_record_cleanup",
    "main",
    "normalize_acceptance_origin",
    "operator_metric_dimensions",
    "orphan_sweep",
    "plugin_policy_binding_sha256",
    "provision_storage",
    "read_acceptance_state",
    "receipt_store",
    "resolve_exec_receipt_env",
    "run_bedrock_guardrails_live",
    "sanitize_evidence",
    "scenario_load",
    "scenario_resource_namespace",
    "validate_compatibility_record",
    "validate_task_definition_policy_binding",
    "verify_api",
    "verify_bedrock",
    "verify_bedrock_guardrails",
    "verify_connection_budget_live",
    "verify_local_auth",
    "verify_operator_telemetry",
    "verify_operator_telemetry_live",
    "verify_operator_telemetry_outage",
    "verify_payloads",
    "verify_s3",
    "verify_textract",
    "write_acceptance_state",
    "xray_trace_id",
}

PRIVATE_OWNER_EXPORTS = {
    "approvals": frozenset({"approval_require_current", "approval_verify"}),
    "bedrock": frozenset(
        {
            "build_plugin_policy_acceptance",
            "run_bedrock_guardrails_live",
            "verify_bedrock",
            "verify_bedrock_guardrails",
        }
    ),
    "capture": frozenset(
        {
            "FIXED_INPUT_BYTES",
            "TUTORIAL_INPUT_BYTES",
            "build_canonical_tutorial_pipeline_yaml",
            "build_fixed_pipeline_yaml",
            "capture",
            "provision_storage",
            "verify_api",
            "verify_local_auth",
            "verify_payloads",
        }
    ),
    "cleanup": frozenset({"cleanup_evidence_finalize"}),
    "contracts": frozenset(
        {
            "AcceptanceCheckError",
            "AcceptanceHttpError",
            "AcceptanceInputError",
            "AcceptanceStateError",
            "CONNECT_TIMEOUT_SECONDS",
            "EVIDENCE_KINDS",
            "FORBIDDEN_AWS_OVERRIDE_ENV",
            "MAX_BLOB_RESPONSE_BYTES",
            "MAX_CONTROL_DOCUMENT_BYTES",
            "MAX_EXEC_RECEIPT_CHARS",
            "MAX_EXEC_STREAM_BYTES",
            "MAX_JSON_RESPONSE_BYTES",
            "MAX_STATE_FILE_BYTES",
            "OperatorTelemetryAcceptanceError",
            "POOL_TIMEOUT_SECONDS",
            "READ_TIMEOUT_SECONDS",
            "RUN_POLL_DEADLINE_SECONDS",
            "RUN_POLL_INTERVAL_SECONDS",
            "SanitizedResourceIdentity",
            "WRITE_TIMEOUT_SECONDS",
            "normalize_acceptance_origin",
            "plugin_policy_binding_sha256",
            "scenario_resource_namespace",
        }
    ),
    "control_service": frozenset(
        {
            "control_manifest_load_cleanup",
            "control_manifest_update",
            "control_manifest_validate",
            "scenario_load",
            "validate_compatibility_record",
        }
    ),
    "evidence": frozenset({"create_evidence_export_receipt", "sanitize_evidence"}),
    "gate_ledger": frozenset(
        {
            "gate_ledger_bind_candidate",
            "gate_ledger_finalize",
            "gate_ledger_get",
            "gate_ledger_init",
            "gate_ledger_record",
            "gate_ledger_record_cleanup",
        }
    ),
    "http_client": frozenset({"AcceptanceHttpClient"}),
    "manifest": frozenset(
        {
            "control_manifest_bind_retained_evidence",
            "control_manifest_bind_scenario",
            "control_manifest_checkpoint_operator_evidence",
            "control_manifest_get",
            "control_manifest_init",
        }
    ),
    "manifest_schema": frozenset({"CLEANUP_SURFACES"}),
    "operator_telemetry": frozenset(
        {
            "AWSOperatorMetricEmitter",
            "AWSOperatorTelemetryQueries",
            "AcceptancePolicy",
            "AuditSentinel",
            "ExistingLandscapeLifecycleAudit",
            "OperatorTelemetryEvidence",
            "OperatorTelemetryOutageEvidence",
            "PublicApiLifecycleAudit",
            "TelemetryQueries",
            "TelemetrySentinelEmitter",
            "operator_metric_dimensions",
            "verify_connection_budget_live",
            "verify_operator_telemetry",
            "verify_operator_telemetry_live",
            "verify_operator_telemetry_outage",
            "xray_trace_id",
        }
    ),
    "orphan_sweep": frozenset({"OrphanSweepClients", "orphan_sweep"}),
    "receipt_contracts": frozenset({"encode_exec_receipt", "extract_exec_receipt", "resolve_exec_receipt_env"}),
    "receipt_store": frozenset({"receipt_store"}),
    "s3": frozenset({"verify_s3"}),
    "scenario_inventory": frozenset({"PLUGIN_POLICY_ASSIGNMENT_NAMES", "SCENARIO_ASSIGNMENT_NAMES"}),
    "state": frozenset({"AcceptanceCredentials", "AcceptanceState", "read_acceptance_state", "write_acceptance_state"}),
    "task_definition": frozenset({"validate_task_definition_policy_binding"}),
    "textract": frozenset({"verify_textract"}),
}


@dataclass(frozen=True)
class ActionSurface:
    action_class: str
    destination: str
    option_strings: tuple[str, ...]
    required: bool
    nargs: str | int | None = None
    choices: tuple[str, ...] | None = None
    type_identity: str | None = None
    default: object = None
    const: object = None
    metavar: object = None
    mutex_group: int | None = None


@dataclass(frozen=True)
class MutexSurface:
    required: bool
    member_destinations: tuple[str, ...]


@dataclass(frozen=True)
class ParserSurface:
    actions: tuple[ActionSurface, ...]
    mutex_groups: tuple[MutexSurface, ...] = ()


def _store(
    destination: str,
    *,
    required: bool = False,
    choices: tuple[str, ...] | None = None,
    type_identity: str | None = None,
    default: object = None,
    mutex_group: int | None = None,
) -> ActionSurface:
    return ActionSurface(
        "_StoreAction",
        destination,
        (f"--{destination.replace('_', '-')}",),
        required,
        choices=choices,
        type_identity=type_identity,
        default=default,
        mutex_group=mutex_group,
    )


def _flag(destination: str, *, required: bool = False, mutex_group: int | None = None) -> ActionSurface:
    return ActionSurface(
        "_StoreTrueAction",
        destination,
        (f"--{destination.replace('_', '-')}",),
        required,
        nargs=0,
        default=False,
        const=True,
        mutex_group=mutex_group,
    )


S = _store
F = _flag
AB = ("A", "B")
ABS = ("A", "B", "bootstrap")

EXPECTED_PARSER_SURFACE = {
    ("capture",): ParserSurface((S("state_file", required=True),)),
    ("verify-api",): ParserSurface((S("state_file", required=True),)),
    ("verify-payloads",): ParserSurface((S("landscape_run_id", required=True),)),
    ("scenario-namespace",): ParserSurface((S("acceptance_run_id", required=True), S("scenario_id", required=True, choices=AB))),
    ("verify-operator-telemetry",): ParserSurface((S("phase", choices=("outage", "positive"), default="positive"), S("landscape_run_id"))),
    ("verify-connection-budget",): ParserSurface(
        (
            S("cluster_id", required=True),
            S("start_time", required=True),
            S("approved_budget", required=True, type_identity="builtins.int"),
            S("safety_margin", required=True, type_identity="builtins.int"),
        )
    ),
    ("extract-exec-receipt",): ParserSurface(
        (
            S(
                "check",
                required=True,
                choices=(
                    "verify-bedrock",
                    "verify-bedrock-guardrails",
                    "verify-connection-budget",
                    "verify-operator-telemetry",
                    "verify-s3",
                    "verify-textract",
                ),
            ),
            S("candidate_sha", required=True),
            S("task_arn", required=True),
            S("scenario_id", required=True),
            S("plugin_policy_binding_sha256"),
        )
    ),
    ("control-manifest", "init"): ParserSurface(
        tuple(
            S(name, required=True)
            for name in (
                "file",
                "acceptance_run_id",
                "candidate_sha",
                "aws_account_id",
                "aws_region",
                "scenario_a_inventory",
                "scenario_b_inventory",
                "scenario_a_tf_binding",
                "scenario_b_tf_binding",
                "evidence_destination_sha256",
                "gate_ledger",
                "teardown_deadline_utc",
            )
        )
    ),
    ("control-manifest", "validate"): ParserSurface(
        (
            S("file", required=True),
            S("acceptance_run_id"),
            S("candidate_sha"),
            F("cleanup_only"),
            F("require_cleanup_cleared"),
        )
    ),
    ("control-manifest", "get"): ParserSurface((S("file", required=True), S("field", required=True))),
    ("control-manifest", "load-cleanup"): ParserSurface((S("file", required=True), F("shell_assignments", required=True))),
    ("control-manifest", "bind-scenario"): ParserSurface(
        (S("file", required=True), S("scenario_id", required=True, choices=AB), S("inventory", required=True))
    ),
    ("control-manifest", "bind-retained-evidence"): ParserSurface(
        (S("file", required=True), S("receipt", required=True), F("require_complete"))
    ),
    ("control-manifest", "checkpoint-operator-evidence"): ParserSurface(
        (S("file", required=True), S("exec_receipt", required=True), S("checkpoint", required=True))
    ),
    ("control-manifest", "update"): ParserSurface(
        (
            S("file", required=True),
            S("cleanup_required", choices=("true",)),
            *(
                S(name)
                for name in (
                    "ecr_baseline_tag",
                    "ecr_candidate_tag",
                    "ecr_registry",
                    "ecr_repository",
                    "ecr_baseline_digest",
                    "ecr_candidate_digest",
                    "acceptance_state_path",
                    "oidc_evidence_dir",
                    "evidence_export_receipt",
                    "final_evidence_export_receipt",
                    "terraform_plan_receipt",
                    "terraform_applied",
                    "terraform_noop_receipt",
                    "cleanup_checkpoint",
                    "verdict_failure",
                    "emergency_cleanup_deadline_utc",
                    "cleanup_escalation",
                )
            ),
        )
    ),
    ("gate-ledger", "init"): ParserSurface(
        tuple(
            S(name, required=True)
            for name in (
                "file",
                "branch",
                "starting_sha",
                "plan_sha256",
                "program_base_sha",
                "reconciled_release_sha",
            )
        )
    ),
    ("gate-ledger", "get"): ParserSurface(
        (
            S("file", required=True),
            S(
                "field",
                required=True,
                choices=(
                    "branch",
                    "candidate_sha",
                    "plan_sha256",
                    "program_base_sha",
                    "reconciled_release_sha",
                    "starting_sha",
                ),
            ),
        )
    ),
    ("gate-ledger", "record"): ParserSurface(
        (
            S("file", required=True),
            S("check_id", required=True),
            S("exit_status", required=True, type_identity="builtins.int"),
            S("receipt_hash", required=True),
            S("candidate_sha", required=True),
            S("started_at"),
            S("ended_at"),
        )
    ),
    ("gate-ledger", "record-cleanup"): ParserSurface(
        (
            S("file", required=True),
            S("check_id", required=True),
            S("exit_status", required=True, type_identity="builtins.int"),
            S("receipt_hash", required=True),
            S("candidate_sha", required=True),
            S("started_at"),
            S("ended_at"),
        )
    ),
    ("gate-ledger", "bind-candidate"): ParserSurface((S("file", required=True), S("candidate_sha", required=True))),
    ("gate-ledger", "finalize"): ParserSurface((S("file", required=True), S("candidate_sha", required=True))),
    ("receipt-store",): ParserSurface(
        (
            S("file", required=True),
            S("scenario_id", required=True, choices=ABS),
            S("kind", required=True),
            S("subject_id", required=True),
            S("receipt_file", mutex_group=0),
            F("receipt_stdin", mutex_group=0),
        ),
        (MutexSurface(True, ("receipt_file", "receipt_stdin")),),
    ),
    ("approval-verify",): ParserSurface(
        (
            S("file", required=True),
            S("scenario_id", required=True, choices=ABS),
            S("kind", required=True, choices=("terraform-destroy-plan", "terraform-plan")),
            S("plan_receipt_hash", required=True),
            S("approval_file", required=True),
        )
    ),
    ("approval-require-current",): ParserSurface(
        (
            S("file", required=True),
            S("scenario_id", required=True, choices=ABS),
            S("kind", required=True, choices=("terraform-destroy-plan", "terraform-plan")),
            S("plan_receipt_hash", required=True),
            S("approval_hash", required=True),
        )
    ),
    ("scenario-load",): ParserSurface(
        (S("file", required=True), S("scenario_id", required=True, choices=AB), F("shell_assignments", required=True))
    ),
    ("validate-task-definition-policy",): ParserSurface(
        (
            S("file", required=True),
            S("scenario_id", required=True, choices=AB),
            S("container_name", required=True),
            S("expected_user", choices=("1654:1654",)),
            S("expected_image_role", choices=("candidate", "rollback-baseline"), default="candidate"),
        )
    ),
    ("compatibility-record-validate",): ParserSurface(
        (S("file", required=True), S("scenario_id", required=True, choices=AB), S("record", required=True))
    ),
    ("orphan-sweep",): ParserSurface((S("file", required=True), S("acceptance_run_id", required=True))),
    ("cleanup-evidence-finalize",): ParserSurface(
        (
            S("file", required=True),
            S("ledger", required=True),
            S("phase", required=True, choices=("commit", "prepare")),
            F("clear_cleanup_required"),
        )
    ),
    ("evidence-export-receipt",): ParserSurface(
        (
            S("file", required=True),
            S("ledger", required=True),
            S("output", required=True),
            S("artifact_count", required=True, type_identity="builtins.int"),
        )
    ),
    ("provision-storage",): ParserSurface(()),
    ("verify-local-auth",): ParserSurface(()),
    ("verify-s3",): ParserSurface(()),
    ("verify-bedrock",): ParserSurface(()),
    ("verify-bedrock-guardrails",): ParserSurface(()),
    ("verify-textract",): ParserSurface(()),
    ("sanitize-evidence",): ParserSurface(
        (
            S(
                "kind",
                required=True,
                choices=(
                    "deployment-event",
                    "doctor-log",
                    "task-definition",
                    "terraform-destroy-plan",
                    "terraform-plan",
                    "web-log",
                ),
            ),
            S("plan_sha256"),
        )
    ),
}


def _callable_identity(value: Callable[..., object] | None) -> str | None:
    if value is None:
        return None
    return f"{value.__module__}.{value.__qualname__}"


def _parser_surface(parser: argparse.ArgumentParser) -> dict[tuple[str, ...], ParserSurface]:
    surfaces: dict[tuple[str, ...], ParserSurface] = {}

    def visit(candidate: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        subparsers = [action for action in candidate._actions if isinstance(action, argparse._SubParsersAction)]
        if subparsers:
            assert len(subparsers) == 1
            for name, child in subparsers[0].choices.items():
                visit(child, (*path, name))
            return

        group_membership = {
            id(action): index for index, group in enumerate(candidate._mutually_exclusive_groups) for action in group._group_actions
        }
        actions = tuple(
            ActionSurface(
                action_class=type(action).__name__,
                destination=action.dest,
                option_strings=tuple(sorted(action.option_strings)),
                required=action.required,
                nargs=action.nargs,
                choices=tuple(sorted(action.choices)) if action.choices is not None else None,
                type_identity=_callable_identity(action.type),
                default=action.default,
                const=action.const,
                metavar=action.metavar,
                mutex_group=group_membership.get(id(action)),
            )
            for action in candidate._actions
            if action.dest != "help"
        )
        groups = tuple(
            MutexSurface(group.required, tuple(action.dest for action in group._group_actions))
            for group in candidate._mutually_exclusive_groups
        )
        surfaces[path] = ParserSurface(actions, groups)

    visit(parser, ())
    return surfaces


def test_parser_surface_is_the_exact_reviewed_contract() -> None:
    parser = acceptance.build_parser()
    root_subparsers = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    assert len(root_subparsers) == 1
    assert set(root_subparsers[0].choices) == EXPECTED_COMMANDS
    assert _parser_surface(parser) == EXPECTED_PARSER_SURFACE


def test_all_selected_base_public_exports_remain_importable() -> None:
    assert len(EXPECTED_PUBLIC_EXPORTS) == 92
    missing = EXPECTED_PUBLIC_EXPORTS.difference(vars(acceptance))
    assert not missing


def test_all_private_owned_exports_are_facade_reexports_by_identity() -> None:
    facade_owned = {"build_parser", "main"}
    private_owned = {name for names in PRIVATE_OWNER_EXPORTS.values() for name in names}
    assert len(private_owned) == 90
    assert private_owned.isdisjoint(facade_owned)
    assert private_owned | facade_owned == EXPECTED_PUBLIC_EXPORTS

    for name in facade_owned:
        assert getattr(acceptance, name).__module__ == acceptance.__name__

    for module_name, names in PRIVATE_OWNER_EXPORTS.items():
        owner = import_module(f"elspeth.web._aws_ecs_acceptance.{module_name}")
        for name in names:
            assert getattr(acceptance, name) is getattr(owner, name), f"{name} is not owned by {module_name}"


def test_public_export_literal_matches_selected_base_module_definitions() -> None:
    source_path = Path(acceptance.__file__ or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                defined.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            defined.update(
                child.id for target in targets for child in ast.walk(target) if isinstance(child, ast.Name) and not child.id.startswith("_")
            )
    # This equality characterizes the monolith. Once extraction starts, direct
    # importability above remains the permanent facade contract.
    if not (Path(source_path).parent / "_aws_ecs_acceptance").exists():
        assert defined == EXPECTED_PUBLIC_EXPORTS


@dataclass(frozen=True)
class DispatchContract:
    target: str
    positional: tuple[object, ...]
    keywords: Mapping[str, object]


DISPATCH_ENVIRONMENT = {"ELSPETH_DISPATCH_SENTINEL": "reviewed-environment"}


def _dispatch_contract(target: str, *positional: object, **keywords: object) -> DispatchContract:
    return DispatchContract(target, positional, keywords)


D = _dispatch_contract
EXPECTED_DISPATCH_CONTRACTS = {
    ("approval-require-current",): D(
        "approval_require_current",
        Path("control.json"),
        scenario_id="A",
        kind="terraform-plan",
        plan_receipt_hash="2" * 64,
        approval_hash="3" * 64,
    ),
    ("approval-verify",): D(
        "approval_verify",
        Path("control.json"),
        scenario_id="A",
        kind="terraform-plan",
        plan_receipt_hash="2" * 64,
        approval_file=Path("approval.json"),
    ),
    ("capture",): D("capture", DISPATCH_ENVIRONMENT, state_file=Path("state.json")),
    ("cleanup-evidence-finalize",): D(
        "cleanup_evidence_finalize",
        Path("control.json"),
        ledger_path=Path("ledger.json"),
        phase="prepare",
        clear_cleanup_required=False,
    ),
    ("compatibility-record-validate",): D(
        "validate_compatibility_record",
        Path("record.json"),
        manifest_path=Path("control.json"),
        scenario_id="A",
    ),
    ("control-manifest", "bind-retained-evidence"): D(
        "control_manifest_bind_retained_evidence",
        Path("control.json"),
        receipt_path="receipt.json",
        require_complete=False,
    ),
    ("control-manifest", "bind-scenario"): D(
        "control_manifest_bind_scenario",
        Path("control.json"),
        scenario_id="A",
        inventory_path="inventory.json",
    ),
    ("control-manifest", "checkpoint-operator-evidence"): D(
        "control_manifest_checkpoint_operator_evidence",
        Path("control.json"),
        exec_receipt_path="exec.json",
        checkpoint_path="checkpoint.json",
    ),
    ("control-manifest", "get"): D("control_manifest_get", Path("control.json"), "branch"),
    ("control-manifest", "init"): D(
        "control_manifest_init",
        Path("control.json"),
        acceptance_run_id="00000000-0000-4000-8000-000000000001",
        candidate_sha="a" * 40,
        aws_account_id="123456789012",
        aws_region="ap-southeast-2",
        scenario_a_inventory="a.json",
        scenario_b_inventory="b.json",
        scenario_a_tf_binding="a-binding.json",
        scenario_b_tf_binding="b-binding.json",
        evidence_destination_sha256="b" * 64,
        gate_ledger="ledger.json",
        teardown_deadline_utc="2026-07-25T00:00:00Z",
    ),
    ("control-manifest", "load-cleanup"): D("control_manifest_load_cleanup", Path("control.json")),
    ("control-manifest", "update"): D(
        "control_manifest_update",
        Path("control.json"),
        cleanup_required=None,
        ecr_baseline_tag=None,
        ecr_candidate_tag=None,
        ecr_registry=None,
        ecr_repository=None,
        ecr_baseline_digest=None,
        ecr_candidate_digest=None,
        acceptance_state_path=None,
        oidc_evidence_dir=None,
        evidence_export_receipt=None,
        final_evidence_export_receipt=None,
        terraform_plan_receipt=None,
        terraform_applied=None,
        terraform_noop_receipt=None,
        cleanup_checkpoint=None,
        verdict_failure=None,
        emergency_cleanup_deadline_utc=None,
        cleanup_escalation=None,
    ),
    ("control-manifest", "validate"): D(
        "control_manifest_validate",
        Path("control.json"),
        acceptance_run_id="00000000-0000-4000-8000-000000000001",
        candidate_sha="a" * 40,
        cleanup_only=False,
        require_cleanup_cleared=False,
    ),
    ("evidence-export-receipt",): D(
        "create_evidence_export_receipt",
        Path("control.json"),
        ledger_path=Path("ledger.json"),
        output_path=Path("evidence.json"),
        artifact_count=1,
    ),
    ("extract-exec-receipt",): D(
        "extract_exec_receipt",
        "{}",
        expected_candidate_sha="a" * 40,
        expected_task_arn="arn:aws:ecs:ap-southeast-2:123456789012:task/cluster/task",
        expected_scenario_id="A",
        expected_check="verify-s3",
        expected_plugin_policy_binding_sha256=None,
    ),
    ("gate-ledger", "bind-candidate"): D("gate_ledger_bind_candidate", Path("control.json"), candidate_sha="a" * 40),
    ("gate-ledger", "finalize"): D("gate_ledger_finalize", Path("control.json"), candidate_sha="a" * 40),
    ("gate-ledger", "get"): D("gate_ledger_get", Path("control.json"), "branch"),
    ("gate-ledger", "init"): D(
        "gate_ledger_init",
        Path("control.json"),
        branch="release/0.7.2",
        starting_sha="c" * 40,
        plan_sha256="d" * 64,
        program_base_sha="e" * 40,
        reconciled_release_sha="f" * 40,
    ),
    ("gate-ledger", "record"): D(
        "gate_ledger_record",
        Path("control.json"),
        check_id="check",
        exit_status=0,
        receipt_hash="1" * 64,
        candidate_sha="a" * 40,
        started_at=None,
        ended_at=None,
    ),
    ("gate-ledger", "record-cleanup"): D(
        "gate_ledger_record_cleanup",
        Path("control.json"),
        check_id="check",
        exit_status=0,
        receipt_hash="1" * 64,
        candidate_sha="a" * 40,
        started_at=None,
        ended_at=None,
    ),
    ("orphan-sweep",): D("orphan_sweep", Path("control.json"), acceptance_run_id="00000000-0000-4000-8000-000000000001"),
    ("provision-storage",): D("provision_storage"),
    ("receipt-store",): D(
        "receipt_store",
        Path("control.json"),
        scenario_id="A",
        kind="web-log",
        subject_id="subject",
        receipt_file=Path("receipt.json"),
        receipt_bytes=None,
    ),
    ("sanitize-evidence",): D("sanitize_evidence", "web-log", {}, plan_sha256=None),
    ("scenario-load",): D("scenario_load", Path("control.json"), scenario_id="A"),
    ("scenario-namespace",): D("scenario_resource_namespace", "00000000-0000-4000-8000-000000000001", "A"),
    ("validate-task-definition-policy",): D(
        "validate_task_definition_policy_binding",
        {},
        manifest_path=Path("control.json"),
        scenario_id="A",
        container_name="web",
        expected_user=None,
        expected_image_role="candidate",
    ),
    ("verify-api",): D("verify_api", DISPATCH_ENVIRONMENT, state_file=Path("state.json")),
    ("verify-bedrock",): D("verify_bedrock", DISPATCH_ENVIRONMENT),
    ("verify-bedrock-guardrails",): D("run_bedrock_guardrails_live", DISPATCH_ENVIRONMENT),
    ("verify-connection-budget",): D(
        "verify_connection_budget_live",
        DISPATCH_ENVIRONMENT,
        cluster_id="cluster",
        start_time="2026-07-24T00:00:00Z",
        approved_budget=8,
        safety_margin=2,
    ),
    ("verify-local-auth",): D("verify_local_auth"),
    ("verify-operator-telemetry",): D(
        "verify_operator_telemetry_live",
        DISPATCH_ENVIRONMENT,
        phase="positive",
        landscape_run_id="run-id",
    ),
    ("verify-payloads",): D("verify_payloads", "run-id"),
    ("verify-s3",): D("verify_s3", DISPATCH_ENVIRONMENT),
    ("verify-textract",): D("verify_textract", DISPATCH_ENVIRONMENT),
}


class _DispatchInput:
    def __init__(self, value: bytes = b"{}") -> None:
        self._text = io.StringIO(value.decode("utf-8"))
        self.buffer = io.BytesIO(value)

    def read(self, size: int = -1) -> str:
        return self._text.read(size)


def _dispatch_namespace(path: tuple[str, ...]) -> SimpleNamespace:
    values: dict[str, Any] = {
        "command": path[0],
        "control_action": path[1] if path[0] == "control-manifest" else None,
        "ledger_action": path[1] if path[0] == "gate-ledger" else None,
        "state_file": "state.json",
        "landscape_run_id": "run-id",
        "acceptance_run_id": "00000000-0000-4000-8000-000000000001",
        "scenario_id": "A",
        "phase": "prepare" if path[0] == "cleanup-evidence-finalize" else "positive",
        "cluster_id": "cluster",
        "start_time": "2026-07-24T00:00:00Z",
        "approved_budget": 8,
        "safety_margin": 2,
        "check": "verify-s3",
        "candidate_sha": "a" * 40,
        "task_arn": "arn:aws:ecs:ap-southeast-2:123456789012:task/cluster/task",
        "plugin_policy_binding_sha256": None,
        "file": "control.json",
        "aws_account_id": "123456789012",
        "aws_region": "ap-southeast-2",
        "scenario_a_inventory": "a.json",
        "scenario_b_inventory": "b.json",
        "scenario_a_tf_binding": "a-binding.json",
        "scenario_b_tf_binding": "b-binding.json",
        "evidence_destination_sha256": "b" * 64,
        "gate_ledger": "ledger.json",
        "teardown_deadline_utc": "2026-07-25T00:00:00Z",
        "cleanup_only": False,
        "require_cleanup_cleared": False,
        "field": "branch",
        "shell_assignments": True,
        "inventory": "inventory.json",
        "receipt": "receipt.json",
        "require_complete": False,
        "exec_receipt": "exec.json",
        "checkpoint": "checkpoint.json",
        "cleanup_required": None,
        "ecr_baseline_tag": None,
        "ecr_candidate_tag": None,
        "ecr_registry": None,
        "ecr_repository": None,
        "ecr_baseline_digest": None,
        "ecr_candidate_digest": None,
        "acceptance_state_path": None,
        "oidc_evidence_dir": None,
        "evidence_export_receipt": None,
        "final_evidence_export_receipt": None,
        "terraform_plan_receipt": None,
        "terraform_applied": None,
        "terraform_noop_receipt": None,
        "cleanup_checkpoint": None,
        "verdict_failure": None,
        "emergency_cleanup_deadline_utc": None,
        "cleanup_escalation": None,
        "branch": "release/0.7.2",
        "starting_sha": "c" * 40,
        "plan_sha256": "d" * 64 if path == ("gate-ledger", "init") else None,
        "program_base_sha": "e" * 40,
        "reconciled_release_sha": "f" * 40,
        "check_id": "check",
        "exit_status": 0,
        "receipt_hash": "1" * 64,
        "started_at": None,
        "ended_at": None,
        "kind": "terraform-plan" if path[0].startswith("approval-") else "web-log",
        "subject_id": "subject",
        "receipt_file": "receipt.json",
        "receipt_stdin": False,
        "plan_receipt_hash": "2" * 64,
        "approval_file": "approval.json",
        "approval_hash": "3" * 64,
        "container_name": "web",
        "expected_user": None,
        "expected_image_role": "candidate",
        "record": "record.json",
        "ledger": "ledger.json",
        "clear_cleanup_required": False,
        "output": "evidence.json",
        "artifact_count": 1,
    }
    return SimpleNamespace(**values)


_DISPATCH_RESULTS: Mapping[str, object] = {
    "approval_verify": "4" * 64,
    "control_manifest_get": "value",
    "control_manifest_load_cleanup": "CLEANUP_REQUIRED=false\n",
    "gate_ledger_get": "value",
    "receipt_store": "5" * 64,
    "scenario_load": "SCENARIO_ID=A\n",
    "scenario_resource_namespace": "a-f8b447a7b5b51f38c800",
    "validate_task_definition_policy_binding": "arn:aws:ecs:ap-southeast-2:123456789012:task-definition/web:1",
}


@pytest.mark.parametrize(("path", "contract"), sorted(EXPECTED_DISPATCH_CONTRACTS.items()))
def test_every_dispatch_leaf_calls_its_reviewed_target(
    path: tuple[str, ...],
    contract: DispatchContract,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    result = _DISPATCH_RESULTS.get(contract.target, {})

    if contract.target == "verify_bedrock":

        async def target_spy(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return result

    else:

        def target_spy(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return result

    namespace = _dispatch_namespace(path)
    monkeypatch.setattr(acceptance, "build_parser", lambda: SimpleNamespace(parse_args=lambda _argv: namespace))
    monkeypatch.setattr(acceptance, contract.target, target_spy)
    monkeypatch.setattr(acceptance.os, "environ", DISPATCH_ENVIRONMENT)
    monkeypatch.setattr(acceptance, "_suppress_process_output", lambda: nullcontext())
    monkeypatch.setattr(acceptance, "resolve_exec_receipt_env", lambda _env: {})
    monkeypatch.setattr(acceptance, "encode_exec_receipt", lambda *_args: "encoded-receipt")
    monkeypatch.setattr(sys, "stdin", _DispatchInput())

    assert acceptance.main([]) == 0
    assert len(calls) == 1
    assert capsys.readouterr().err == ""

    positional, keywords = calls[0]
    assert positional == contract.positional
    assert keywords == contract.keywords


def test_json_output_and_static_failure_envelopes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(acceptance, "provision_storage", lambda: {"z": 2, "a": 1})
    assert acceptance.main(["provision-storage"]) == 0
    captured = capsys.readouterr()
    assert captured.out == '{"a":1,"z":2}\n'
    assert captured.err == ""

    def checked_failure() -> None:
        raise acceptance.AcceptanceCheckError("facade_contract")

    monkeypatch.setattr(acceptance, "provision_storage", checked_failure)
    assert acceptance.main(["provision-storage"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "check": "facade_contract",
        "error_class": "AcceptanceCheckError",
        "step": None,
    }

    def unexpected_failure() -> None:
        raise RuntimeError("must not leak")

    monkeypatch.setattr(acceptance, "provision_storage", unexpected_failure)
    assert acceptance.main(["provision-storage"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error_class": "AcceptanceInternalError",
        "error_code": "acceptance_internal",
        "step": None,
    }
    assert "must not leak" not in captured.err


def test_scenario_namespace_selected_base_value(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "scenario-namespace",
        "--acceptance-run-id",
        "00000000-0000-4000-8000-000000000001",
        "--scenario-id",
        "A",
    ]
    assert acceptance.scenario_resource_namespace(argv[2], argv[4]) == "a-f8b447a7b5b51f38c800"
    assert acceptance.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out == "a-f8b447a7b5b51f38c800\n"
    assert captured.err == ""


def test_module_help_remains_executable() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "elspeth.web.aws_ecs_acceptance", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "usage: python -m elspeth.web.aws_ecs_acceptance" in completed.stdout
    assert completed.stderr == ""
