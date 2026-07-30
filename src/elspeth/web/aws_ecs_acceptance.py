"""Sanitized AWS ECS operator-telemetry acceptance coordination.

The live AWS adapters are deliberately injected.  This coordinator owns the
ordering, bounded retry, and evidence projection contracts shared by the
in-task acceptance command: Landscape first, operational telemetry second.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from ._aws_ecs_acceptance.approvals import (
    _require_current_approval as _require_current_approval,
)
from ._aws_ecs_acceptance.approvals import (
    approval_require_current as approval_require_current,
)
from ._aws_ecs_acceptance.approvals import (
    approval_verify as approval_verify,
)
from ._aws_ecs_acceptance.bedrock import (
    _suppress_process_output,
)
from ._aws_ecs_acceptance.bedrock import (
    build_plugin_policy_acceptance as build_plugin_policy_acceptance,
)
from ._aws_ecs_acceptance.bedrock import (
    run_bedrock_guardrails_live as run_bedrock_guardrails_live,
)
from ._aws_ecs_acceptance.bedrock import (
    verify_bedrock as verify_bedrock,
)
from ._aws_ecs_acceptance.bedrock import (
    verify_bedrock_guardrails as verify_bedrock_guardrails,
)
from ._aws_ecs_acceptance.capture import (
    FIXED_INPUT_BYTES as FIXED_INPUT_BYTES,
)
from ._aws_ecs_acceptance.capture import (
    TUTORIAL_INPUT_BYTES as TUTORIAL_INPUT_BYTES,
)
from ._aws_ecs_acceptance.capture import (
    build_canonical_tutorial_pipeline_yaml as build_canonical_tutorial_pipeline_yaml,
)
from ._aws_ecs_acceptance.capture import (
    build_fixed_pipeline_yaml as build_fixed_pipeline_yaml,
)
from ._aws_ecs_acceptance.capture import (
    capture,
    provision_storage,
    verify_api,
    verify_local_auth,
    verify_payloads,
)
from ._aws_ecs_acceptance.cleanup import cleanup_evidence_finalize as cleanup_evidence_finalize
from ._aws_ecs_acceptance.contracts import (
    _EVIDENCE_KINDS,
    MAX_CONTROL_DOCUMENT_BYTES,
    MAX_EXEC_STREAM_BYTES,
    MAX_JSON_RESPONSE_BYTES,
    AcceptanceCheckError,
    AcceptanceHttpError,
    AcceptanceInputError,
    AcceptanceStateError,
    OperatorTelemetryAcceptanceError,
    scenario_resource_namespace,
)
from ._aws_ecs_acceptance.contracts import (
    CONNECT_TIMEOUT_SECONDS as CONNECT_TIMEOUT_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    EVIDENCE_KINDS as EVIDENCE_KINDS,
)
from ._aws_ecs_acceptance.contracts import (
    FORBIDDEN_AWS_OVERRIDE_ENV as FORBIDDEN_AWS_OVERRIDE_ENV,
)
from ._aws_ecs_acceptance.contracts import (
    MAX_BLOB_RESPONSE_BYTES as MAX_BLOB_RESPONSE_BYTES,
)
from ._aws_ecs_acceptance.contracts import (
    MAX_EXEC_RECEIPT_CHARS as MAX_EXEC_RECEIPT_CHARS,
)
from ._aws_ecs_acceptance.contracts import (
    MAX_STATE_FILE_BYTES as MAX_STATE_FILE_BYTES,
)
from ._aws_ecs_acceptance.contracts import (
    POOL_TIMEOUT_SECONDS as POOL_TIMEOUT_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    READ_TIMEOUT_SECONDS as READ_TIMEOUT_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    RUN_POLL_DEADLINE_SECONDS as RUN_POLL_DEADLINE_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    RUN_POLL_INTERVAL_SECONDS as RUN_POLL_INTERVAL_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    WRITE_TIMEOUT_SECONDS as WRITE_TIMEOUT_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    SanitizedResourceIdentity as SanitizedResourceIdentity,
)
from ._aws_ecs_acceptance.contracts import (
    acceptance_error_envelope as _acceptance_error_envelope,
)
from ._aws_ecs_acceptance.contracts import (
    current_acceptance_step as _current_acceptance_step,
)
from ._aws_ecs_acceptance.contracts import (
    normalize_acceptance_origin as normalize_acceptance_origin,
)
from ._aws_ecs_acceptance.contracts import (
    plugin_policy_binding_sha256 as plugin_policy_binding_sha256,
)
from ._aws_ecs_acceptance.contracts import (
    reset_acceptance_step as _reset_acceptance_step,
)
from ._aws_ecs_acceptance.control_service import (
    control_manifest_load_cleanup as control_manifest_load_cleanup,
)
from ._aws_ecs_acceptance.control_service import control_manifest_update as control_manifest_update
from ._aws_ecs_acceptance.control_service import control_manifest_validate as control_manifest_validate
from ._aws_ecs_acceptance.control_service import scenario_load as scenario_load
from ._aws_ecs_acceptance.control_service import (
    validate_compatibility_record as validate_compatibility_record,
)
from ._aws_ecs_acceptance.evidence import (
    create_evidence_export_receipt as create_evidence_export_receipt,
)
from ._aws_ecs_acceptance.evidence import sanitize_evidence as sanitize_evidence
from ._aws_ecs_acceptance.gate_ledger import (
    _CLEANUP_GATE_CHECK_ORDER as _CLEANUP_GATE_CHECK_ORDER,
)
from ._aws_ecs_acceptance.gate_ledger import (
    _GATE_LEDGER_GET_FIELDS,
)
from ._aws_ecs_acceptance.gate_ledger import (
    _REQUIRED_GATE_CHECK_IDS as _REQUIRED_GATE_CHECK_IDS,
)
from ._aws_ecs_acceptance.gate_ledger import (
    _SUCCESS_GATE_CHECK_ORDER as _SUCCESS_GATE_CHECK_ORDER,
)
from ._aws_ecs_acceptance.gate_ledger import (
    _TASK1_GATE_CHECK_ORDER as _TASK1_GATE_CHECK_ORDER,
)
from ._aws_ecs_acceptance.gate_ledger import (
    _TERMINAL_GATE_CHECK_ID as _TERMINAL_GATE_CHECK_ID,
)
from ._aws_ecs_acceptance.gate_ledger import (
    _gate_ledger_records_hash as _gate_ledger_records_hash,
)
from ._aws_ecs_acceptance.gate_ledger import (
    gate_ledger_bind_candidate as gate_ledger_bind_candidate,
)
from ._aws_ecs_acceptance.gate_ledger import gate_ledger_finalize as gate_ledger_finalize
from ._aws_ecs_acceptance.gate_ledger import gate_ledger_get as gate_ledger_get
from ._aws_ecs_acceptance.gate_ledger import gate_ledger_init as gate_ledger_init
from ._aws_ecs_acceptance.gate_ledger import gate_ledger_record as gate_ledger_record
from ._aws_ecs_acceptance.gate_ledger import (
    gate_ledger_record_cleanup as gate_ledger_record_cleanup,
)
from ._aws_ecs_acceptance.http_client import AcceptanceHttpClient as AcceptanceHttpClient
from ._aws_ecs_acceptance.manifest import (
    control_manifest_bind_retained_evidence as control_manifest_bind_retained_evidence,
)
from ._aws_ecs_acceptance.manifest import (
    control_manifest_bind_scenario as control_manifest_bind_scenario,
)
from ._aws_ecs_acceptance.manifest import (
    control_manifest_checkpoint_operator_evidence as control_manifest_checkpoint_operator_evidence,
)
from ._aws_ecs_acceptance.manifest import control_manifest_get as control_manifest_get
from ._aws_ecs_acceptance.manifest import control_manifest_init as control_manifest_init
from ._aws_ecs_acceptance.manifest_schema import (
    CLEANUP_SURFACES as CLEANUP_SURFACES,
)
from ._aws_ecs_acceptance.manifest_schema import (
    _load_retained_evidence as _load_retained_evidence,
)
from ._aws_ecs_acceptance.manifest_schema import (
    _read_control_manifest as _read_control_manifest,
)
from ._aws_ecs_acceptance.manifest_schema import (
    _require_mutable_control_manifest as _require_mutable_control_manifest,
)
from ._aws_ecs_acceptance.manifest_schema import (
    _validate_control_manifest as _validate_control_manifest,
)
from ._aws_ecs_acceptance.manifest_schema import (
    _validate_retained_evidence_receipt as _validate_retained_evidence_receipt,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    AcceptancePolicy as AcceptancePolicy,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    AuditSentinel as AuditSentinel,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    AWSOperatorMetricEmitter as AWSOperatorMetricEmitter,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    AWSOperatorTelemetryQueries as AWSOperatorTelemetryQueries,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    ExistingLandscapeLifecycleAudit as ExistingLandscapeLifecycleAudit,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    OperatorTelemetryEvidence as OperatorTelemetryEvidence,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    OperatorTelemetryOutageEvidence as OperatorTelemetryOutageEvidence,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    PublicApiLifecycleAudit as PublicApiLifecycleAudit,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    TelemetryQueries as TelemetryQueries,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    TelemetrySentinelEmitter as TelemetrySentinelEmitter,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    operator_metric_dimensions as operator_metric_dimensions,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    verify_connection_budget_live as verify_connection_budget_live,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    verify_operator_telemetry as verify_operator_telemetry,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    verify_operator_telemetry_live as verify_operator_telemetry_live,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    verify_operator_telemetry_outage as verify_operator_telemetry_outage,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    xray_trace_id as xray_trace_id,
)
from ._aws_ecs_acceptance.orphan_sweep import (
    OrphanSweepClients as OrphanSweepClients,
)
from ._aws_ecs_acceptance.orphan_sweep import (
    _transaction_search_projection as _transaction_search_projection,
)
from ._aws_ecs_acceptance.orphan_sweep import orphan_sweep as orphan_sweep
from ._aws_ecs_acceptance.receipt_contracts import (
    _expected_schema_facts as _expected_schema_facts,
)
from ._aws_ecs_acceptance.receipt_contracts import (
    encode_exec_receipt,
    extract_exec_receipt,
    resolve_exec_receipt_env,
)
from ._aws_ecs_acceptance.receipt_store import receipt_store as receipt_store
from ._aws_ecs_acceptance.s3 import verify_s3 as verify_s3
from ._aws_ecs_acceptance.scenario_inventory import (
    PLUGIN_POLICY_ASSIGNMENT_NAMES as PLUGIN_POLICY_ASSIGNMENT_NAMES,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    SCENARIO_ASSIGNMENT_NAMES as SCENARIO_ASSIGNMENT_NAMES,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    _load_bound_scenario_inventory as _load_bound_scenario_inventory,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    _load_preapply_scenario_inventory as _load_preapply_scenario_inventory,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    _scenario_inventory_hash as _scenario_inventory_hash,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    _validate_scenario_inventory as _validate_scenario_inventory,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    _validate_scenario_inventory_isolation as _validate_scenario_inventory_isolation,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    _validate_tf_binding_receipt as _validate_tf_binding_receipt,
)
from ._aws_ecs_acceptance.state import (
    AcceptanceCredentials as AcceptanceCredentials,
)
from ._aws_ecs_acceptance.state import (
    AcceptanceState as AcceptanceState,
)
from ._aws_ecs_acceptance.state import (
    read_acceptance_state as read_acceptance_state,
)
from ._aws_ecs_acceptance.state import (
    write_acceptance_state as write_acceptance_state,
)
from ._aws_ecs_acceptance.task_definition import (
    validate_task_definition_policy_binding as validate_task_definition_policy_binding,
)
from ._aws_ecs_acceptance.textract import verify_textract as verify_textract


def build_parser() -> argparse.ArgumentParser:
    """Build the closed command surface used by the Plan 12 controller."""

    parser = argparse.ArgumentParser(prog="python -m elspeth.web.aws_ecs_acceptance")
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture")
    capture.add_argument("--state-file", required=True)

    verify_api = commands.add_parser("verify-api")
    verify_api.add_argument("--state-file", required=True)

    verify_payloads = commands.add_parser("verify-payloads")
    verify_payloads.add_argument("--landscape-run-id", required=True)

    scenario_namespace = commands.add_parser("scenario-namespace")
    scenario_namespace.add_argument("--acceptance-run-id", required=True)
    scenario_namespace.add_argument("--scenario-id", required=True, choices=("A", "B"))

    verify_operator = commands.add_parser("verify-operator-telemetry")
    verify_operator.add_argument("--phase", choices=("positive", "outage"), default="positive")
    verify_operator.add_argument("--landscape-run-id")

    verify_connection = commands.add_parser("verify-connection-budget")
    verify_connection.add_argument("--cluster-id", required=True)
    verify_connection.add_argument("--start-time", required=True)
    verify_connection.add_argument("--approved-budget", required=True, type=int)
    verify_connection.add_argument("--safety-margin", required=True, type=int)

    extract_receipt = commands.add_parser("extract-exec-receipt")
    extract_receipt.add_argument(
        "--check",
        required=True,
        choices=(
            "verify-s3",
            "verify-bedrock",
            "verify-bedrock-guardrails",
            "verify-connection-budget",
            "verify-operator-telemetry",
            "verify-textract",
        ),
    )
    extract_receipt.add_argument("--candidate-sha", required=True)
    extract_receipt.add_argument("--task-arn", required=True)
    extract_receipt.add_argument("--scenario-id", required=True)
    extract_receipt.add_argument("--plugin-policy-binding-sha256")

    control = commands.add_parser("control-manifest")
    control_actions = control.add_subparsers(dest="control_action", required=True)
    control_init = control_actions.add_parser("init")
    for option in (
        "file",
        "acceptance-run-id",
        "candidate-sha",
        "aws-account-id",
        "aws-region",
        "scenario-a-inventory",
        "scenario-b-inventory",
        "scenario-a-tf-binding",
        "scenario-b-tf-binding",
        "evidence-destination-sha256",
        "gate-ledger",
        "teardown-deadline-utc",
    ):
        control_init.add_argument(f"--{option}", required=True)
    control_validate = control_actions.add_parser("validate")
    control_validate.add_argument("--file", required=True)
    control_validate.add_argument("--acceptance-run-id")
    control_validate.add_argument("--candidate-sha")
    control_validate.add_argument("--cleanup-only", action="store_true")
    control_validate.add_argument("--require-cleanup-cleared", action="store_true")
    control_get = control_actions.add_parser("get")
    control_get.add_argument("--file", required=True)
    control_get.add_argument("--field", required=True)
    control_load = control_actions.add_parser("load-cleanup")
    control_load.add_argument("--file", required=True)
    control_load.add_argument("--shell-assignments", action="store_true", required=True)
    control_bind = control_actions.add_parser("bind-scenario")
    control_bind.add_argument("--file", required=True)
    control_bind.add_argument("--scenario-id", required=True, choices=("A", "B"))
    control_bind.add_argument("--inventory", required=True)
    control_bind_retained = control_actions.add_parser("bind-retained-evidence")
    control_bind_retained.add_argument("--file", required=True)
    control_bind_retained.add_argument("--receipt", required=True)
    control_bind_retained.add_argument("--require-complete", action="store_true")
    control_checkpoint_operator = control_actions.add_parser("checkpoint-operator-evidence")
    control_checkpoint_operator.add_argument("--file", required=True)
    control_checkpoint_operator.add_argument("--exec-receipt", required=True)
    control_checkpoint_operator.add_argument("--checkpoint", required=True)
    control_update = control_actions.add_parser("update")
    control_update.add_argument("--file", required=True)
    control_update.add_argument("--cleanup-required", choices=("true",))
    control_update.add_argument("--ecr-baseline-tag")
    control_update.add_argument("--ecr-candidate-tag")
    control_update.add_argument("--ecr-registry")
    control_update.add_argument("--ecr-repository")
    control_update.add_argument("--ecr-baseline-digest")
    control_update.add_argument("--ecr-candidate-digest")
    control_update.add_argument("--acceptance-state-path")
    control_update.add_argument("--oidc-evidence-dir")
    control_update.add_argument("--evidence-export-receipt")
    control_update.add_argument("--final-evidence-export-receipt")
    control_update.add_argument("--terraform-plan-receipt")
    control_update.add_argument("--terraform-applied")
    control_update.add_argument("--terraform-noop-receipt")
    control_update.add_argument("--cleanup-checkpoint")
    control_update.add_argument("--verdict-failure")
    control_update.add_argument("--emergency-cleanup-deadline-utc")
    control_update.add_argument("--cleanup-escalation")

    ledger = commands.add_parser("gate-ledger")
    ledger_actions = ledger.add_subparsers(dest="ledger_action", required=True)
    ledger_init = ledger_actions.add_parser("init")
    ledger_init.add_argument("--file", required=True)
    ledger_init.add_argument("--branch", required=True)
    ledger_init.add_argument("--starting-sha", required=True)
    ledger_init.add_argument("--plan-sha256", required=True)
    ledger_init.add_argument("--program-base-sha", required=True)
    ledger_init.add_argument("--reconciled-release-sha", required=True)
    ledger_get = ledger_actions.add_parser("get")
    ledger_get.add_argument("--file", required=True)
    ledger_get.add_argument("--field", required=True, choices=tuple(sorted(_GATE_LEDGER_GET_FIELDS)))
    ledger_record = ledger_actions.add_parser("record")
    ledger_record.add_argument("--file", required=True)
    ledger_record.add_argument("--check-id", required=True)
    ledger_record.add_argument("--exit-status", required=True, type=int)
    ledger_record.add_argument("--receipt-hash", required=True)
    ledger_record.add_argument("--candidate-sha", required=True)
    ledger_record.add_argument("--started-at")
    ledger_record.add_argument("--ended-at")
    ledger_cleanup = ledger_actions.add_parser("record-cleanup")
    ledger_cleanup.add_argument("--file", required=True)
    ledger_cleanup.add_argument("--check-id", required=True)
    ledger_cleanup.add_argument("--exit-status", required=True, type=int)
    ledger_cleanup.add_argument("--receipt-hash", required=True)
    ledger_cleanup.add_argument("--candidate-sha", required=True)
    ledger_cleanup.add_argument("--started-at")
    ledger_cleanup.add_argument("--ended-at")
    ledger_bind = ledger_actions.add_parser("bind-candidate")
    ledger_bind.add_argument("--file", required=True)
    ledger_bind.add_argument("--candidate-sha", required=True)
    ledger_finalize = ledger_actions.add_parser("finalize")
    ledger_finalize.add_argument("--file", required=True)
    ledger_finalize.add_argument("--candidate-sha", required=True)

    receipt_command = commands.add_parser("receipt-store")
    receipt_command.add_argument("--file", required=True)
    receipt_command.add_argument("--scenario-id", required=True, choices=("A", "B", "bootstrap"))
    receipt_command.add_argument("--kind", required=True)
    receipt_command.add_argument("--subject-id", required=True)
    receipt_input = receipt_command.add_mutually_exclusive_group(required=True)
    receipt_input.add_argument("--receipt-file")
    receipt_input.add_argument("--receipt-stdin", action="store_true")

    approval_command = commands.add_parser("approval-verify")
    approval_command.add_argument("--file", required=True)
    approval_command.add_argument("--scenario-id", required=True, choices=("A", "B", "bootstrap"))
    approval_command.add_argument("--kind", required=True, choices=("terraform-plan", "terraform-destroy-plan"))
    approval_command.add_argument("--plan-receipt-hash", required=True)
    approval_command.add_argument("--approval-file", required=True)
    approval_current = commands.add_parser("approval-require-current")
    approval_current.add_argument("--file", required=True)
    approval_current.add_argument("--scenario-id", required=True, choices=("A", "B", "bootstrap"))
    approval_current.add_argument("--kind", required=True, choices=("terraform-plan", "terraform-destroy-plan"))
    approval_current.add_argument("--plan-receipt-hash", required=True)
    approval_current.add_argument("--approval-hash", required=True)

    scenario_command = commands.add_parser("scenario-load")
    scenario_command.add_argument("--file", required=True)
    scenario_command.add_argument("--scenario-id", required=True, choices=("A", "B"))
    scenario_command.add_argument("--shell-assignments", action="store_true", required=True)

    task_definition_policy = commands.add_parser("validate-task-definition-policy")
    task_definition_policy.add_argument("--file", required=True)
    task_definition_policy.add_argument("--scenario-id", required=True, choices=("A", "B"))
    task_definition_policy.add_argument("--container-name", required=True)
    task_definition_policy.add_argument("--expected-user", choices=("1654:1654",))
    task_definition_policy.add_argument("--expected-image-role", choices=("candidate", "rollback-baseline"), default="candidate")

    compatibility_record = commands.add_parser("compatibility-record-validate")
    compatibility_record.add_argument("--file", required=True)
    compatibility_record.add_argument("--scenario-id", required=True, choices=("A", "B"))
    compatibility_record.add_argument("--record", required=True)

    orphan_command = commands.add_parser("orphan-sweep")
    orphan_command.add_argument("--file", required=True)
    orphan_command.add_argument("--acceptance-run-id", required=True)

    cleanup_command = commands.add_parser("cleanup-evidence-finalize")
    cleanup_command.add_argument("--file", required=True)
    cleanup_command.add_argument("--ledger", required=True)
    cleanup_command.add_argument("--phase", required=True, choices=("prepare", "commit"))
    cleanup_command.add_argument("--clear-cleanup-required", action="store_true")

    evidence_export = commands.add_parser("evidence-export-receipt")
    evidence_export.add_argument("--file", required=True)
    evidence_export.add_argument("--ledger", required=True)
    evidence_export.add_argument("--output", required=True)
    evidence_export.add_argument("--artifact-count", required=True, type=int)

    for command in (
        "provision-storage",
        "verify-local-auth",
        "verify-s3",
        "verify-bedrock",
        "verify-bedrock-guardrails",
        "verify-textract",
    ):
        commands.add_parser(command)

    sanitize_evidence = commands.add_parser("sanitize-evidence")
    sanitize_evidence.add_argument("--kind", required=True, choices=_EVIDENCE_KINDS)
    sanitize_evidence.add_argument("--plan-sha256")
    return parser


def _print_json(value: object) -> None:
    sys.stdout.write(f"{json.dumps(value, sort_keys=True, separators=(',', ':'))}\n")


def _print_error(value: object) -> None:
    sys.stderr.write(f"{json.dumps(value, sort_keys=True, separators=(',', ':'))}\n")


def _write_stdout_line(value: str) -> None:
    sys.stdout.write(f"{value}\n")


def main(argv: list[str] | None = None) -> int:
    """Dispatch the closed acceptance command surface with static failures."""

    args = build_parser().parse_args(argv)
    _reset_acceptance_step()

    try:
        if args.command == "capture":
            capture(os.environ, state_file=Path(args.state_file))
        elif args.command == "provision-storage":
            _print_json(provision_storage())
        elif args.command == "scenario-namespace":
            _write_stdout_line(scenario_resource_namespace(args.acceptance_run_id, args.scenario_id))
        elif args.command == "verify-api":
            _print_json(verify_api(os.environ, state_file=Path(args.state_file)))
        elif args.command == "verify-payloads":
            _print_json(verify_payloads(args.landscape_run_id))
        elif args.command == "verify-local-auth":
            _print_json(verify_local_auth())
        elif args.command in {
            "verify-s3",
            "verify-bedrock",
            "verify-bedrock-guardrails",
            "verify-connection-budget",
            "verify-operator-telemetry",
            "verify-textract",
        }:
            with _suppress_process_output():
                if args.command == "verify-s3":
                    details = verify_s3(os.environ)
                elif args.command == "verify-bedrock":
                    details = asyncio.run(verify_bedrock(os.environ))
                elif args.command == "verify-bedrock-guardrails":
                    details = run_bedrock_guardrails_live(os.environ)
                elif args.command == "verify-textract":
                    details = verify_textract(os.environ)
                elif args.command == "verify-connection-budget":
                    details = verify_connection_budget_live(
                        os.environ,
                        cluster_id=args.cluster_id,
                        start_time=args.start_time,
                        approved_budget=args.approved_budget,
                        safety_margin=args.safety_margin,
                    )
                else:
                    details = verify_operator_telemetry_live(
                        os.environ,
                        phase=args.phase,
                        landscape_run_id=args.landscape_run_id,
                    )
            _write_stdout_line(encode_exec_receipt(args.command, details, resolve_exec_receipt_env(os.environ)))
        elif args.command == "extract-exec-receipt":
            stream = sys.stdin.read(MAX_EXEC_STREAM_BYTES + 1)
            if args.check == "verify-bedrock-guardrails" and args.plugin_policy_binding_sha256 is None:
                raise AcceptanceCheckError("plugin_policy_binding")
            _print_json(
                extract_exec_receipt(
                    stream,
                    expected_candidate_sha=args.candidate_sha,
                    expected_task_arn=args.task_arn,
                    expected_scenario_id=args.scenario_id,
                    expected_check=args.check,
                    expected_plugin_policy_binding_sha256=args.plugin_policy_binding_sha256,
                )
            )
        elif args.command == "control-manifest":
            path = Path(args.file)
            if args.control_action == "init":
                control_manifest_init(
                    path,
                    acceptance_run_id=args.acceptance_run_id,
                    candidate_sha=args.candidate_sha,
                    aws_account_id=args.aws_account_id,
                    aws_region=args.aws_region,
                    scenario_a_inventory=args.scenario_a_inventory,
                    scenario_b_inventory=args.scenario_b_inventory,
                    scenario_a_tf_binding=args.scenario_a_tf_binding,
                    scenario_b_tf_binding=args.scenario_b_tf_binding,
                    evidence_destination_sha256=args.evidence_destination_sha256,
                    gate_ledger=args.gate_ledger,
                    teardown_deadline_utc=args.teardown_deadline_utc,
                )
            elif args.control_action == "validate":
                if args.cleanup_only != args.require_cleanup_cleared:
                    raise AcceptanceCheckError("control_manifest_cleanup")
                control_manifest_validate(
                    path,
                    acceptance_run_id=args.acceptance_run_id,
                    candidate_sha=args.candidate_sha,
                    cleanup_only=args.cleanup_only,
                    require_cleanup_cleared=args.require_cleanup_cleared,
                )
            elif args.control_action == "get":
                _write_stdout_line(control_manifest_get(path, args.field))
            elif args.control_action == "load-cleanup":
                sys.stdout.write(control_manifest_load_cleanup(path))
            elif args.control_action == "bind-scenario":
                control_manifest_bind_scenario(path, scenario_id=args.scenario_id, inventory_path=args.inventory)
            elif args.control_action == "bind-retained-evidence":
                control_manifest_bind_retained_evidence(
                    path,
                    receipt_path=args.receipt,
                    require_complete=args.require_complete,
                )
            elif args.control_action == "checkpoint-operator-evidence":
                control_manifest_checkpoint_operator_evidence(
                    path,
                    exec_receipt_path=args.exec_receipt,
                    checkpoint_path=args.checkpoint,
                )
            else:
                control_manifest_update(
                    path,
                    cleanup_required=True if args.cleanup_required == "true" else None,
                    ecr_baseline_tag=args.ecr_baseline_tag,
                    ecr_candidate_tag=args.ecr_candidate_tag,
                    ecr_registry=args.ecr_registry,
                    ecr_repository=args.ecr_repository,
                    ecr_baseline_digest=args.ecr_baseline_digest,
                    ecr_candidate_digest=args.ecr_candidate_digest,
                    acceptance_state_path=args.acceptance_state_path,
                    oidc_evidence_dir=args.oidc_evidence_dir,
                    evidence_export_receipt=args.evidence_export_receipt,
                    final_evidence_export_receipt=args.final_evidence_export_receipt,
                    terraform_plan_receipt=args.terraform_plan_receipt,
                    terraform_applied=args.terraform_applied,
                    terraform_noop_receipt=args.terraform_noop_receipt,
                    cleanup_checkpoint=args.cleanup_checkpoint,
                    verdict_failure=args.verdict_failure,
                    emergency_cleanup_deadline_utc=args.emergency_cleanup_deadline_utc,
                    cleanup_escalation=args.cleanup_escalation,
                )
        elif args.command == "gate-ledger":
            path = Path(args.file)
            if args.ledger_action == "init":
                gate_ledger_init(
                    path,
                    branch=args.branch,
                    starting_sha=args.starting_sha,
                    plan_sha256=args.plan_sha256,
                    program_base_sha=args.program_base_sha,
                    reconciled_release_sha=args.reconciled_release_sha,
                )
            elif args.ledger_action == "get":
                _write_stdout_line(gate_ledger_get(path, args.field))
            elif args.ledger_action == "record":
                gate_ledger_record(
                    path,
                    check_id=args.check_id,
                    exit_status=args.exit_status,
                    receipt_hash=args.receipt_hash,
                    candidate_sha=args.candidate_sha,
                    started_at=args.started_at,
                    ended_at=args.ended_at,
                )
            elif args.ledger_action == "record-cleanup":
                gate_ledger_record_cleanup(
                    path,
                    check_id=args.check_id,
                    exit_status=args.exit_status,
                    receipt_hash=args.receipt_hash,
                    candidate_sha=args.candidate_sha,
                    started_at=args.started_at,
                    ended_at=args.ended_at,
                )
            elif args.ledger_action == "bind-candidate":
                gate_ledger_bind_candidate(path, candidate_sha=args.candidate_sha)
            else:
                gate_ledger_finalize(path, candidate_sha=args.candidate_sha)
        elif args.command == "receipt-store":
            receipt_bytes = sys.stdin.buffer.read(MAX_CONTROL_DOCUMENT_BYTES + 1) if args.receipt_stdin else None
            _write_stdout_line(
                receipt_store(
                    Path(args.file),
                    scenario_id=args.scenario_id,
                    kind=args.kind,
                    subject_id=args.subject_id,
                    receipt_file=Path(args.receipt_file) if args.receipt_file else None,
                    receipt_bytes=receipt_bytes,
                )
            )
        elif args.command == "approval-verify":
            _write_stdout_line(
                approval_verify(
                    Path(args.file),
                    scenario_id=args.scenario_id,
                    kind=args.kind,
                    plan_receipt_hash=args.plan_receipt_hash,
                    approval_file=Path(args.approval_file),
                )
            )
        elif args.command == "approval-require-current":
            approval_require_current(
                Path(args.file),
                scenario_id=args.scenario_id,
                kind=args.kind,
                plan_receipt_hash=args.plan_receipt_hash,
                approval_hash=args.approval_hash,
            )
        elif args.command == "scenario-load":
            sys.stdout.write(scenario_load(Path(args.file), scenario_id=args.scenario_id))
        elif args.command == "validate-task-definition-policy":
            raw_payload = sys.stdin.read(MAX_JSON_RESPONSE_BYTES + 1)
            if len(raw_payload.encode("utf-8")) > MAX_JSON_RESPONSE_BYTES:
                raise AcceptanceCheckError("task_definition_policy_binding")
            try:
                payload = json.loads(raw_payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise AcceptanceCheckError("task_definition_policy_binding") from None
            task_definition_arn = validate_task_definition_policy_binding(
                payload,
                manifest_path=Path(args.file),
                scenario_id=args.scenario_id,
                container_name=args.container_name,
                expected_user=args.expected_user,
                expected_image_role=args.expected_image_role,
            )
            _print_json({"task_definition_arn": task_definition_arn})
        elif args.command == "compatibility-record-validate":
            _print_json(
                validate_compatibility_record(
                    Path(args.record),
                    manifest_path=Path(args.file),
                    scenario_id=args.scenario_id,
                )
            )
        elif args.command == "orphan-sweep":
            _print_json(orphan_sweep(Path(args.file), acceptance_run_id=args.acceptance_run_id))
        elif args.command == "cleanup-evidence-finalize":
            cleanup_evidence_finalize(
                Path(args.file),
                ledger_path=Path(args.ledger),
                phase=args.phase,
                clear_cleanup_required=args.clear_cleanup_required,
            )
        elif args.command == "evidence-export-receipt":
            _print_json(
                create_evidence_export_receipt(
                    Path(args.file),
                    ledger_path=Path(args.ledger),
                    output_path=Path(args.output),
                    artifact_count=args.artifact_count,
                )
            )
        elif args.command == "sanitize-evidence":
            content = sys.stdin.buffer.read(MAX_CONTROL_DOCUMENT_BYTES + 1)
            if len(content) > MAX_CONTROL_DOCUMENT_BYTES:
                raise AcceptanceCheckError("sanitize_evidence_schema")
            try:
                raw_evidence = json.loads(content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise AcceptanceCheckError("sanitize_evidence_schema") from None
            _print_json(sanitize_evidence(args.kind, raw_evidence, plan_sha256=args.plan_sha256))
        else:
            raise AcceptanceCheckError("command_not_implemented")
    except (AcceptanceCheckError, AcceptanceHttpError, AcceptanceInputError, AcceptanceStateError, OperatorTelemetryAcceptanceError) as exc:
        _print_error(_acceptance_error_envelope(exc))
        return 1
    except Exception:
        # True last resort: nothing about the exception is trusted, so only
        # the closed internal code and the tagged step are projected.
        _print_error(
            {
                "error_class": "AcceptanceInternalError",
                "error_code": "acceptance_internal",
                "step": _current_acceptance_step(),
            }
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
