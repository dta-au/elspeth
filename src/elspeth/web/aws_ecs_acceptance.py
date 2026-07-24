"""Sanitized AWS ECS operator-telemetry acceptance coordination.

The live AWS adapters are deliberately injected.  This coordinator owns the
ordering, bounded retry, and evidence projection contracts shared by the
in-task acceptance command: Landscape first, operational telemetry second.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import json
import math
import os
import re
import shlex
import stat
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from elspeth.core.secrets import is_secret_field
from elspeth.plugins.transforms.aws.guardrail_profiles import BedrockGuardrailProfileSettings
from elspeth.web.composer.provider_config import infer_provider_from_model_name, infer_provider_from_unprefixed_model_name

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
from ._aws_ecs_acceptance.contracts import (
    _EMERGENCY_CLEANUP_SECONDS,
    _EVIDENCE_KINDS,
    _GIT_SHA_PATTERN,
    _SCENARIO_VALUE_FIELDS,
    _SHA256_PATTERN,
    FORBIDDEN_AWS_OVERRIDE_ENV,
    MAX_CONTROL_DOCUMENT_BYTES,
    MAX_EXEC_STREAM_BYTES,
    MAX_JSON_RESPONSE_BYTES,
    PLUGIN_POLICY_ASSIGNMENT_NAMES,
    SCENARIO_ASSIGNMENT_NAMES,
    AcceptanceCheckError,
    AcceptanceHttpError,
    AcceptanceInputError,
    AcceptanceStateError,
    OperatorTelemetryAcceptanceError,
    _canonical_uuid,
    _control_timestamp,
    _sha256,
    _task_definition_family,
    _utc_timestamp,
    plugin_policy_binding_sha256,
    scenario_resource_namespace,
)
from ._aws_ecs_acceptance.contracts import (
    CONNECT_TIMEOUT_SECONDS as CONNECT_TIMEOUT_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    EVIDENCE_KINDS as EVIDENCE_KINDS,
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
    normalize_acceptance_origin as normalize_acceptance_origin,
)
from ._aws_ecs_acceptance.http_client import AcceptanceHttpClient as AcceptanceHttpClient
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
from ._aws_ecs_acceptance.receipt_contracts import (
    _CANDIDATE_PACKAGE_VERSION,
    _RECEIPT_KINDS,
    _ROLLBACK_PACKAGE_VERSION,
    _TERRAFORM_RECEIPT_KINDS,
    _expected_schema_facts,
    _validate_bounded_receipt_document,
    _validate_exec_receipt_schema,
    _validate_stored_receipt,
    encode_exec_receipt,
    extract_exec_receipt,
    resolve_exec_receipt_env,
)
from ._aws_ecs_acceptance.s3 import verify_s3 as verify_s3
from ._aws_ecs_acceptance.secure_documents import (
    _read_protected_document,
    _receipt_manifest_write_lock,
    _serialized_control_manifest_write,
    _write_protected_document,
)
from ._aws_ecs_acceptance.state import (
    AcceptanceCredentials as AcceptanceCredentials,
)
from ._aws_ecs_acceptance.state import (
    AcceptanceState as AcceptanceState,
)
from ._aws_ecs_acceptance.state import (
    _parse_state_timestamp,
)
from ._aws_ecs_acceptance.state import (
    read_acceptance_state as read_acceptance_state,
)
from ._aws_ecs_acceptance.state import (
    write_acceptance_state as write_acceptance_state,
)

_TASK_DEFINITION_PLAINTEXT_SECRET_ENV = frozenset(
    {
        "DATABASE_URL",
        "ELSPETH_DATABASE_URL",
        "ELSPETH_WEB__SESSION_DB_URL",
        "ELSPETH_WEB__LANDSCAPE_URL",
    }
)
_TASK_DEFINITION_REQUIRED_SECRET_BINDINGS = (
    ("ELSPETH_WEB__SECRET_KEY", "database-runtime", "secret_key"),
    ("ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY", "database-runtime", "shareable_link_signing_key"),
    ("ELSPETH_WEB__SESSION_DB_URL", "database-runtime", "session_url"),
    ("ELSPETH_WEB__LANDSCAPE_URL", "database-runtime", "landscape_url"),
)
_TASK_DEFINITION_OPENROUTER_SECRET_BINDING = (
    "OPENROUTER_API_KEY",
    "openrouter-composer",
    "openrouter_api_key",
)
_TASK_DEFINITION_COMPOSER_MODEL_ENV = (
    "ELSPETH_WEB__COMPOSER_MODEL",
    "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL",
)
_TASK_DEFINITION_AWS_OVERRIDE_ENV = FORBIDDEN_AWS_OVERRIDE_ENV | {"AWS_DEFAULT_REGION"}
_SECRET_VALUE_FROM_PATTERN = re.compile(
    r"arn:(aws(?:-us-gov|-cn)?):secretsmanager:([a-z0-9-]+):([0-9]{12}):"
    r"secret:([A-Za-z0-9/_+=.@-]{1,512})(?::([^:\x00-\x1f\x7f]{0,256}):([^:\x00-\x1f\x7f]{0,256}):([^:\x00-\x1f\x7f]{0,256}))?\Z"
)
_SECRET_ARN_SUFFIX_PATTERN = re.compile(r"(.+)-[A-Za-z0-9]{6}\Z")
_CLEANUP_SURFACES = (
    "coordinator",
    "evidence_export",
    "identity_cleanup",
    "shared_resource_cleanup",
    "terraform_scenario_a",
    "terraform_scenario_b",
    "orphan_sweep",
    "ecr_baseline",
    "ecr_candidate",
    "local_images",
    "final_evidence_prepare",
    "local_evidence",
    "teardown_deadline",
)
CLEANUP_SURFACES = _CLEANUP_SURFACES
_CONTROL_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "acceptance_run_id",
        "candidate_sha",
        "aws",
        "scenarios",
        "gate_ledger_path",
        "teardown_deadline_utc",
        "emergency_cleanup_deadline_utc",
        "cleanup_required",
        "cleanup_states",
        "ecr",
        "evidence",
        "verdict_failures",
        "cleanup_escalations",
        "deadline_failure_recorded",
        "final_evidence",
        "created_at",
        "updated_at",
    }
)
_GATE_LEDGER_FIELDS = frozenset(
    {
        "schema",
        "branch",
        "starting_sha",
        "plan_sha256",
        "program_base_sha",
        "reconciled_release_sha",
        "candidate_sha",
        "candidate_bound_record_count",
        "records",
        "cleanup_records",
        "success_record_count_at_cleanup_start",
        "finalized",
        "created_at",
        "updated_at",
    }
)
_GATE_RECORD_FIELDS = frozenset({"check_id", "candidate_sha", "started_at", "ended_at", "exit_status", "receipt_hash"})
_SUCCESS_GATE_CHECK_ORDER = ("candidate", "static", "tests", "image", "live")
_CLEANUP_GATE_CHECK_ORDER = ("cleanup",)
_REQUIRED_GATE_CHECK_ORDER = (*_SUCCESS_GATE_CHECK_ORDER, *_CLEANUP_GATE_CHECK_ORDER)
_REQUIRED_GATE_CHECK_IDS = frozenset(_REQUIRED_GATE_CHECK_ORDER)
_TASK1_GATE_CHECK_ORDER = ("candidate",)
_GATE_LEDGER_GET_FIELDS = frozenset(
    {"branch", "starting_sha", "plan_sha256", "program_base_sha", "reconciled_release_sha", "candidate_sha"}
)
_TERMINAL_GATE_CHECK_ID = "cleanup"
_APPLICATION_SCENARIO_IDS = frozenset({"A", "B"})
# Facts pinned to the Scenario B rollback baseline (the 0.7.0 image), and the
# behaviour-named structural migration from that baseline to the current
# landscape epoch. The label is deliberately a literal: it must be rewritten —
# not silently renumbered — whenever SQLITE_SCHEMA_EPOCH moves, and
# test_compatibility_schema_facts_track_current_epochs enforces that.


_INFRASTRUCTURE_APPROVAL_SCOPES = frozenset({"A", "B", "bootstrap"})
_ORPHAN_INVENTORY_FIELDS = frozenset(
    {
        "tag_key",
        "cleanup_owner",
        "ecs_task_definition_families",
        "elbv2_listener_arns",
        "rds_db_instance_identifiers",
        "efs_creation_tokens",
        "efs_file_system_ids",
        "efs_access_point_ids",
        "secret_ids",
        "iam_role_names",
        "log_group_names",
        "log_resource_policy_names",
        "cloudwatch_dashboard_names",
        "cloudwatch_alarm_names",
        "cloudwatch_retained_metrics",
        "xray_group_names",
        "xray_sampling_rule_names",
        "xray_retained_trace_ids",
        "transaction_search_baseline_sha256",
        "event_rules",
        "bedrock_guardrails",
        "cognito_subject_sub",
        "cognito_pool_owned",
        "expected_retained_metric_series",
        "expected_retained_trace_ids",
    }
)
_ORPHAN_SURFACES = (
    "tagging",
    "ecs",
    "elbv2",
    "rds",
    "efs",
    "secretsmanager",
    "iam",
    "logs",
    "cloudwatch",
    "xray",
    "events",
    "bedrock",
    "cognito",
    "ecr",
)
_ORPHAN_MAX_PAGES = 100
_ORPHAN_MAX_ITEMS = 10_000
_PROVIDER_GENERATED_SCENARIO_FIELDS = frozenset(
    {
        "TARGET_GROUP_ARN",
        "ALB_BASE_URL",
        "ALB_ARN",
        "CANDIDATE_TASK_DEFINITION",
        "DOCTOR_TASK_DEFINITION",
        "DOCTOR_NETWORK_CONFIGURATION",
        "PAYLOAD_VERIFIER_TASK_DEFINITION",
        "LOCAL_AUTH_VERIFIER_TASK_DEFINITION",
        "ROLLBACK_DOCTOR_TASK_DEFINITION",
        "PREVIOUS_TASK_DEFINITION",
        "FIRST_DEPLOY_LISTENER_RULE_ARN",
        "FIRST_DEPLOY_FORWARD_ACTIONS",
        "FIRST_DEPLOY_DISABLED_ACTIONS",
        "COGNITO_USER_POOL_ID",
        "OIDC_EXPECTED_ISSUER",
        "OIDC_EXPECTED_AUDIENCE",
        "OIDC_EXPECTED_AUTHORIZATION_ORIGIN",
    }
)
_RESOLVED_SCENARIO_FIELDS = frozenset(
    {
        *_PROVIDER_GENERATED_SCENARIO_FIELDS,
        "ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES",
        "ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256",
    }
)
_PROVIDER_GENERATED_ORPHAN_FIELDS = frozenset(
    {
        "elbv2_listener_arns",
        "efs_file_system_ids",
        "efs_access_point_ids",
        "bedrock_guardrails",
        "cognito_subject_sub",
        "cognito_pool_owned",
    }
)
_RETAINED_EVIDENCE_FIELDS = frozenset(
    {
        "cloudwatch_retained_metrics",
        "xray_retained_trace_ids",
        "expected_retained_metric_series",
        "expected_retained_trace_ids",
    }
)


def _control_path(value: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("/")
        or len(value) > 4096
        or "://" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise AcceptanceCheckError("control_manifest_schema")
    return value


def _validate_control_manifest(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _CONTROL_MANIFEST_FIELDS:
        raise AcceptanceCheckError("control_manifest_schema")
    if payload["schema"] != "elspeth.aws-ecs-control-manifest.v5":
        raise AcceptanceCheckError("control_manifest_schema")
    try:
        _canonical_uuid(payload["acceptance_run_id"], label="control manifest run identity")
    except AcceptanceInputError:
        raise AcceptanceCheckError("control_manifest_schema") from None
    candidate_sha = payload["candidate_sha"]
    if type(candidate_sha) is not str or _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None:
        raise AcceptanceCheckError("control_manifest_schema")
    aws = payload["aws"]
    if not isinstance(aws, dict) or set(aws) != {"account_id", "region"}:
        raise AcceptanceCheckError("control_manifest_schema")
    if type(aws["account_id"]) is not str or re.fullmatch(r"[0-9]{12}", aws["account_id"]) is None:
        raise AcceptanceCheckError("control_manifest_schema")
    if type(aws["region"]) is not str or re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]", aws["region"]) is None:
        raise AcceptanceCheckError("control_manifest_schema")
    scenarios = payload["scenarios"]
    if not isinstance(scenarios, dict) or set(scenarios) != {"A", "B"}:
        raise AcceptanceCheckError("control_manifest_schema")
    scenario_fields = {
        "preapply_inventory_path",
        "preapply_inventory_sha256",
        "inventory_path",
        "inventory_sha256",
        "inventory_phase",
        "tf_binding_sha256",
        "tf_binding_path",
        "tf_state_identity_sha256",
        "terraform_plan_receipt",
        "terraform_applied",
        "terraform_noop_receipt",
    }
    for scenario in scenarios.values():
        if not isinstance(scenario, dict) or set(scenario) != scenario_fields:
            raise AcceptanceCheckError("control_manifest_schema")
        _control_path(scenario["preapply_inventory_path"])
        preapply_inventory_sha = scenario["preapply_inventory_sha256"]
        if type(preapply_inventory_sha) is not str or _SHA256_PATTERN.fullmatch(preapply_inventory_sha) is None:
            raise AcceptanceCheckError("control_manifest_schema")
        _control_path(scenario["inventory_path"])
        inventory_sha = scenario["inventory_sha256"]
        if type(inventory_sha) is not str or _SHA256_PATTERN.fullmatch(inventory_sha) is None:
            raise AcceptanceCheckError("control_manifest_schema")
        if scenario["inventory_phase"] not in {"preapply", "resolved"}:
            raise AcceptanceCheckError("control_manifest_schema")
        if scenario["inventory_phase"] == "preapply" and (
            scenario["inventory_path"] != scenario["preapply_inventory_path"]
            or scenario["inventory_sha256"] != scenario["preapply_inventory_sha256"]
        ):
            raise AcceptanceCheckError("control_manifest_schema")
        binding = scenario["tf_binding_sha256"]
        if type(binding) is not str or _SHA256_PATTERN.fullmatch(binding) is None:
            raise AcceptanceCheckError("control_manifest_schema")
        _control_path(scenario["tf_binding_path"])
        state_identity = scenario["tf_state_identity_sha256"]
        if type(state_identity) is not str or _SHA256_PATTERN.fullmatch(state_identity) is None:
            raise AcceptanceCheckError("control_manifest_schema")
        if type(scenario["terraform_applied"]) is not bool:
            raise AcceptanceCheckError("control_manifest_schema")
        for field in ("terraform_plan_receipt", "terraform_noop_receipt"):
            value = scenario[field]
            if value is not None and (type(value) is not str or not value or len(value) > 1024):
                raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["inventory_path"] == scenarios["B"]["inventory_path"]:
        raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["inventory_sha256"] == scenarios["B"]["inventory_sha256"]:
        raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["preapply_inventory_path"] == scenarios["B"]["preapply_inventory_path"]:
        raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["preapply_inventory_sha256"] == scenarios["B"]["preapply_inventory_sha256"]:
        raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["tf_binding_sha256"] == scenarios["B"]["tf_binding_sha256"]:
        raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["tf_binding_path"] == scenarios["B"]["tf_binding_path"]:
        raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["tf_state_identity_sha256"] == scenarios["B"]["tf_state_identity_sha256"]:
        raise AcceptanceCheckError("control_manifest_schema")
    _control_path(payload["gate_ledger_path"])
    deadline = _control_timestamp(payload["teardown_deadline_utc"])
    emergency = payload["emergency_cleanup_deadline_utc"]
    if emergency is not None and _control_timestamp(emergency) <= deadline:
        raise AcceptanceCheckError("control_manifest_schema")
    if type(payload["cleanup_required"]) is not bool or type(payload["deadline_failure_recorded"]) is not bool:
        raise AcceptanceCheckError("control_manifest_schema")
    cleanup_states = payload["cleanup_states"]
    if not isinstance(cleanup_states, dict) or set(cleanup_states) != set(_CLEANUP_SURFACES):
        raise AcceptanceCheckError("control_manifest_schema")
    if any(value not in {"pending", "confirmed", "failed", "interrupted"} for value in cleanup_states.values()):
        raise AcceptanceCheckError("control_manifest_schema")
    ecr = payload["ecr"]
    if not isinstance(ecr, dict) or set(ecr) != {
        "registry",
        "repository",
        "baseline_tag",
        "candidate_tag",
        "baseline_digest",
        "candidate_digest",
    }:
        raise AcceptanceCheckError("control_manifest_schema")
    registry = ecr["registry"]
    repository = ecr["repository"]
    if registry is not None and (
        type(registry) is not str or len(registry) > 253 or re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", registry) is None
    ):
        raise AcceptanceCheckError("control_manifest_schema")
    if repository is not None and (
        type(repository) is not str or len(repository) > 256 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", repository) is None
    ):
        raise AcceptanceCheckError("control_manifest_schema")
    if (registry is None) != (repository is None):
        raise AcceptanceCheckError("control_manifest_schema")
    run_id = payload["acceptance_run_id"]
    for field in ("baseline_tag", "candidate_tag"):
        value = ecr[field]
        if value is not None and (
            type(value) is not str or len(value) > 300 or re.fullmatch(r"[A-Za-z0-9._-]+", value) is None or run_id not in value
        ):
            raise AcceptanceCheckError("control_manifest_schema")
    for field in ("baseline_digest", "candidate_digest"):
        value = ecr[field]
        if value is not None and (type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None):
            raise AcceptanceCheckError("control_manifest_schema")
    evidence = payload["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "acceptance_state_path",
        "oidc_evidence_dir",
        "destination_sha256",
        "export_receipt_path",
        "export_receipt_sha256",
        "final_export_receipt_path",
        "final_export_receipt_sha256",
        "retained_evidence_path",
        "retained_evidence_sha256",
        "receipts",
        "approvals",
    }:
        raise AcceptanceCheckError("control_manifest_schema")
    for field in ("acceptance_state_path", "oidc_evidence_dir"):
        value = evidence[field]
        if value is not None:
            _control_path(value)
    destination_sha256 = evidence["destination_sha256"]
    if type(destination_sha256) is not str or _SHA256_PATTERN.fullmatch(destination_sha256) is None:
        raise AcceptanceCheckError("control_manifest_schema")
    for path_field, hash_field in (
        ("export_receipt_path", "export_receipt_sha256"),
        ("final_export_receipt_path", "final_export_receipt_sha256"),
        ("retained_evidence_path", "retained_evidence_sha256"),
    ):
        export_path = evidence[path_field]
        export_sha256 = evidence[hash_field]
        if (export_path is None) != (export_sha256 is None):
            raise AcceptanceCheckError("control_manifest_schema")
        if export_path is not None:
            _control_path(export_path)
            if type(export_sha256) is not str or _SHA256_PATTERN.fullmatch(export_sha256) is None:
                raise AcceptanceCheckError("control_manifest_schema")
    if evidence["export_receipt_path"] is not None and evidence["export_receipt_path"] == evidence["final_export_receipt_path"]:
        raise AcceptanceCheckError("control_manifest_schema")
    receipts = evidence["receipts"]
    if not isinstance(receipts, list) or len(receipts) > 4096:
        raise AcceptanceCheckError("control_manifest_schema")
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != {
            "scenario_id",
            "kind",
            "subject_sha256",
            "receipt_sha256",
            "stored_at",
        }:
            raise AcceptanceCheckError("control_manifest_schema")
        if type(receipt["kind"]) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", receipt["kind"]) is None:
            raise AcceptanceCheckError("control_manifest_schema")
        if receipt["scenario_id"] not in _INFRASTRUCTURE_APPROVAL_SCOPES or (
            receipt["scenario_id"] == "bootstrap" and receipt["kind"] not in _TERRAFORM_RECEIPT_KINDS
        ):
            raise AcceptanceCheckError("control_manifest_schema")
        for field in ("subject_sha256", "receipt_sha256"):
            value = receipt[field]
            if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
                raise AcceptanceCheckError("control_manifest_schema")
        _control_timestamp(receipt["stored_at"])
    approvals = evidence["approvals"]
    if not isinstance(approvals, list) or len(approvals) > 4096:
        raise AcceptanceCheckError("control_manifest_schema")
    for approval in approvals:
        if not isinstance(approval, dict) or set(approval) != {
            "scenario_id",
            "kind",
            "plan_receipt_sha256",
            "approval_sha256",
            "approval_path",
            "expires_at",
            "verified_at",
        }:
            raise AcceptanceCheckError("control_manifest_schema")
        if approval["scenario_id"] not in _INFRASTRUCTURE_APPROVAL_SCOPES or approval["kind"] not in {
            "terraform-plan",
            "terraform-destroy-plan",
        }:
            raise AcceptanceCheckError("control_manifest_schema")
        for field in ("plan_receipt_sha256", "approval_sha256"):
            value = approval[field]
            if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
                raise AcceptanceCheckError("control_manifest_schema")
        _control_path(approval["approval_path"])
        expires_at = _control_timestamp(approval["expires_at"])
        if expires_at <= _control_timestamp(approval["verified_at"]):
            raise AcceptanceCheckError("control_manifest_schema")
        _control_timestamp(approval["verified_at"])
    for field in ("verdict_failures", "cleanup_escalations"):
        values = payload[field]
        if not isinstance(values, list) or len(values) > 128:
            raise AcceptanceCheckError("control_manifest_schema")
        if any(type(value) is not str or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) is None for value in values):
            raise AcceptanceCheckError("control_manifest_schema")
    final_evidence = payload["final_evidence"]
    if final_evidence is not None:
        if not isinstance(final_evidence, dict) or set(final_evidence) != {
            "phase",
            "prepared_at",
            "receipt_count",
            "receipts_sha256",
            "ledger_records_sha256",
            "precommit_manifest_sha256",
            "committed_at",
            "ledger_sha256",
        }:
            raise AcceptanceCheckError("control_manifest_schema")
        if final_evidence["phase"] not in {"prepared", "committed"}:
            raise AcceptanceCheckError("control_manifest_schema")
        _control_timestamp(final_evidence["prepared_at"])
        if type(final_evidence["receipt_count"]) is not int or final_evidence["receipt_count"] < 0:
            raise AcceptanceCheckError("control_manifest_schema")
        for field in ("receipts_sha256", "ledger_records_sha256"):
            value = final_evidence[field]
            if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
                raise AcceptanceCheckError("control_manifest_schema")
        if final_evidence["phase"] == "prepared":
            if any(final_evidence[field] is not None for field in ("precommit_manifest_sha256", "committed_at", "ledger_sha256")):
                raise AcceptanceCheckError("control_manifest_schema")
        else:
            for field in ("precommit_manifest_sha256", "ledger_sha256"):
                value = final_evidence[field]
                if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
                    raise AcceptanceCheckError("control_manifest_schema")
            _control_timestamp(final_evidence["committed_at"])
    created = _control_timestamp(payload["created_at"])
    updated = _control_timestamp(payload["updated_at"])
    if updated < created:
        raise AcceptanceCheckError("control_manifest_schema")
    return payload


def _read_control_manifest(path: Path) -> dict[str, object]:
    return _validate_control_manifest(_read_protected_document(path, check="control_manifest_file"))


def _require_mutable_control_manifest(manifest: Mapping[str, object]) -> None:
    """Reject every rewrite after final receipt authority is committed."""

    final_evidence = manifest["final_evidence"]
    if isinstance(final_evidence, Mapping) and final_evidence.get("phase") == "committed":
        raise AcceptanceCheckError("control_manifest_finalized")


def _scenario_inventory_hash(inventory: Mapping[str, object]) -> str:
    return _sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _validate_orphan_inventory(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _ORPHAN_INVENTORY_FIELDS:
        raise AcceptanceCheckError("scenario_inventory_schema")
    for field in (
        "ecs_task_definition_families",
        "elbv2_listener_arns",
        "rds_db_instance_identifiers",
        "efs_creation_tokens",
        "efs_file_system_ids",
        "efs_access_point_ids",
        "secret_ids",
        "iam_role_names",
        "log_group_names",
        "log_resource_policy_names",
        "cloudwatch_dashboard_names",
        "cloudwatch_alarm_names",
        "xray_group_names",
        "xray_sampling_rule_names",
    ):
        values = payload[field]
        if (
            not isinstance(values, list)
            or len(values) > 1024
            or len(values) != len(set(values))
            or any(
                type(value) is not str
                or not value
                or len(value) > 512
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                for value in values
            )
        ):
            raise AcceptanceCheckError("scenario_inventory_schema")
    if payload["tag_key"] != "ACCEPTANCE_RUN_ID":
        raise AcceptanceCheckError("scenario_inventory_binding")
    cleanup_owner = payload["cleanup_owner"]
    cognito_subject_sub = payload["cognito_subject_sub"]
    if (
        type(cleanup_owner) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}", cleanup_owner) is None
        or type(cognito_subject_sub) is not str
        or len(cognito_subject_sub) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in cognito_subject_sub)
    ):
        raise AcceptanceCheckError("scenario_inventory_schema")
    if type(payload["cognito_pool_owned"]) is not bool:
        raise AcceptanceCheckError("scenario_inventory_schema")
    for count_field in ("expected_retained_metric_series", "expected_retained_trace_ids"):
        value = payload[count_field]
        if type(value) is not int or not 0 <= value <= _ORPHAN_MAX_ITEMS:
            raise AcceptanceCheckError("scenario_inventory_schema")
    metric_queries = payload["cloudwatch_retained_metrics"]
    if not isinstance(metric_queries, list) or len(metric_queries) > 256:
        raise AcceptanceCheckError("scenario_inventory_schema")
    for query in metric_queries:
        if not isinstance(query, dict) or set(query) != {"namespace", "metric_name", "dimensions"}:
            raise AcceptanceCheckError("scenario_inventory_schema")
        namespace = query["namespace"]
        metric_name = query["metric_name"]
        dimensions = query["dimensions"]
        if (
            type(namespace) is not str
            or not namespace
            or len(namespace) > 255
            or type(metric_name) is not str
            or not metric_name
            or len(metric_name) > 255
            or not isinstance(dimensions, list)
            or len(dimensions) > 30
        ):
            raise AcceptanceCheckError("scenario_inventory_schema")
        seen_dimensions: set[str] = set()
        for dimension in dimensions:
            if not isinstance(dimension, dict) or set(dimension) != {"name", "value"}:
                raise AcceptanceCheckError("scenario_inventory_schema")
            name = dimension["name"]
            value = dimension["value"]
            if (
                type(name) is not str
                or not name
                or len(name) > 255
                or name in seen_dimensions
                or type(value) is not str
                or not value
                or len(value) > 1024
            ):
                raise AcceptanceCheckError("scenario_inventory_schema")
            seen_dimensions.add(name)
    trace_ids = payload["xray_retained_trace_ids"]
    if (
        not isinstance(trace_ids, list)
        or len(trace_ids) > 1024
        or len(trace_ids) != len(set(trace_ids))
        or any(type(trace_id) is not str or re.fullmatch(r"1-[0-9a-f]{8}-[0-9a-f]{24}", trace_id) is None for trace_id in trace_ids)
        or payload["expected_retained_metric_series"] != len(metric_queries)
        or payload["expected_retained_trace_ids"] != len(trace_ids)
    ):
        raise AcceptanceCheckError("scenario_inventory_schema")
    transaction_baseline = payload["transaction_search_baseline_sha256"]
    if type(transaction_baseline) is not str or _SHA256_PATTERN.fullmatch(transaction_baseline) is None:
        raise AcceptanceCheckError("scenario_inventory_schema")
    event_rules = payload["event_rules"]
    if not isinstance(event_rules, list) or len(event_rules) > 256:
        raise AcceptanceCheckError("scenario_inventory_schema")
    seen_rules: set[tuple[str, str]] = set()
    for rule in event_rules:
        if not isinstance(rule, dict) or set(rule) != {"event_bus_name", "rule_name", "target_ids"}:
            raise AcceptanceCheckError("scenario_inventory_schema")
        event_bus_name = rule["event_bus_name"]
        rule_name = rule["rule_name"]
        target_ids = rule["target_ids"]
        identity = (event_bus_name, rule_name)
        if (
            type(event_bus_name) is not str
            or not event_bus_name
            or len(event_bus_name) > 256
            or type(rule_name) is not str
            or not rule_name
            or len(rule_name) > 64
            or identity in seen_rules
            or not isinstance(target_ids, list)
            or len(target_ids) > 100
            or len(target_ids) != len(set(target_ids))
            or any(type(target) is not str or not target or len(target) > 64 for target in target_ids)
        ):
            raise AcceptanceCheckError("scenario_inventory_schema")
        seen_rules.add(identity)
    guardrails = payload["bedrock_guardrails"]
    if not isinstance(guardrails, list) or len(guardrails) > 32:
        raise AcceptanceCheckError("scenario_inventory_schema")
    seen_guardrails: set[str] = set()
    for guardrail in guardrails:
        if not isinstance(guardrail, dict) or set(guardrail) != {"identifier", "versions"}:
            raise AcceptanceCheckError("scenario_inventory_schema")
        identifier = guardrail["identifier"]
        versions = guardrail["versions"]
        if (
            type(identifier) is not str
            or not identifier
            or len(identifier) > 2048
            or identifier in seen_guardrails
            or not isinstance(versions, list)
            or len(versions) > 1000
            or len(versions) != len(set(versions))
            or any(type(version) is not str or re.fullmatch(r"[0-9]{1,8}", version) is None for version in versions)
        ):
            raise AcceptanceCheckError("scenario_inventory_schema")
        seen_guardrails.add(identifier)
    return payload


def _validate_tf_binding_receipt(
    path: Path,
    *,
    scenario_id: str,
    acceptance_run_id: str,
    aws_account_id: str,
    aws_region: str,
    expected_sha256: str,
) -> tuple[dict[str, object], str]:
    receipt = _read_protected_document(path, check="tf_binding_file")
    fields = {
        "schema",
        "acceptance_run_id",
        "scenario_id",
        "repository_commit",
        "terraform_lock_sha256",
        "terraform_version",
        "backend_type",
        "backend_encrypted",
        "backend_locked",
        "backend_state_key_sha256",
        "workspace",
        "aws_account_id",
        "aws_region",
        "vars_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields or receipt["schema"] != "elspeth.aws-ecs-tf-binding.v1":
        raise AcceptanceCheckError("tf_binding_schema")
    if (
        receipt["acceptance_run_id"] != acceptance_run_id
        or receipt["scenario_id"] != scenario_id
        or receipt["aws_account_id"] != aws_account_id
        or receipt["aws_region"] != aws_region
        or receipt["backend_type"] != "s3"
        or receipt["backend_encrypted"] is not True
        or receipt["backend_locked"] is not True
    ):
        raise AcceptanceCheckError("tf_binding_binding")
    if type(receipt["repository_commit"]) is not str or re.fullmatch(r"[0-9a-f]{40}", receipt["repository_commit"]) is None:
        raise AcceptanceCheckError("tf_binding_schema")
    for field in ("terraform_lock_sha256", "backend_state_key_sha256", "vars_sha256"):
        value = receipt[field]
        if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
            raise AcceptanceCheckError("tf_binding_schema")
    if (
        type(receipt["terraform_version"]) is not str
        or not 1 <= len(receipt["terraform_version"]) <= 64
        or any(not 32 <= ord(character) <= 126 for character in receipt["terraform_version"])
        or type(receipt["workspace"]) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,89}", receipt["workspace"]) is None
    ):
        raise AcceptanceCheckError("tf_binding_schema")
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if _sha256(canonical) != expected_sha256:
        raise AcceptanceCheckError("tf_binding_binding")
    state_identity = {
        "backend_type": receipt["backend_type"],
        "backend_state_key_sha256": receipt["backend_state_key_sha256"],
        "workspace": receipt["workspace"],
        "aws_account_id": receipt["aws_account_id"],
        "aws_region": receipt["aws_region"],
    }
    return receipt, _sha256(json.dumps(state_identity, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _listener_arn_from_rule_arn(rule_arn: str) -> str:
    match = re.fullmatch(
        r"(arn:(?:aws|aws-us-gov|aws-cn):elasticloadbalancing:[a-z0-9-]+:[0-9]{12})"
        r":listener-rule/(app/[A-Za-z0-9-]{1,32}/[0-9a-f]{16}/[0-9a-f]{16})/[0-9a-f]{16}",
        rule_arn,
    )
    if match is None:
        raise AcceptanceCheckError("scenario_inventory_binding")
    return f"{match.group(1)}:listener/{match.group(2)}"


def _validate_scenario_resource_bindings(
    values: Mapping[str, object],
    orphan: Mapping[str, object],
    *,
    scenario_id: str,
    acceptance_run_id: str,
    aws_account_id: str,
    aws_region: str,
) -> None:
    namespace = scenario_resource_namespace(acceptance_run_id, scenario_id)
    for field in (
        "ECS_CLUSTER",
        "ECS_SERVICE",
        "DB_CLUSTER_IDENTIFIER",
        "WEB_LOG_GROUP",
        "DOCTOR_LOG_GROUP",
        "OPERATOR_METRICS_LOG_GROUP",
        "ECS_DEPLOYMENT_EVENT_RULE",
        "ECS_DEPLOYMENT_EVENT_TARGET_ID",
        "ECS_DEPLOYMENT_EVENT_LOG_GROUP",
        "ELSPETH_TEST_S3_BUCKET",
    ):
        value = values[field]
        if value and (type(value) is not str or namespace not in value):
            raise AcceptanceCheckError("scenario_inventory_binding")
    for field in (
        "ecs_task_definition_families",
        "rds_db_instance_identifiers",
        "efs_creation_tokens",
        "secret_ids",
        "iam_role_names",
        "log_group_names",
        "log_resource_policy_names",
        "cloudwatch_dashboard_names",
        "cloudwatch_alarm_names",
        "xray_group_names",
        "xray_sampling_rule_names",
    ):
        if any(namespace not in identity for identity in cast(list[str], orphan[field])):
            raise AcceptanceCheckError("scenario_inventory_binding")
    if set(cast(list[str], orphan["iam_role_names"])) != {
        f"{namespace}-task-role",
        f"{namespace}-execution-role",
    }:
        raise AcceptanceCheckError("scenario_inventory_binding")
    arn_services = {
        "TARGET_GROUP_ARN": "elasticloadbalancing",
        "ALB_ARN": "elasticloadbalancing",
        "CANDIDATE_TASK_DEFINITION": "ecs",
        "DOCTOR_TASK_DEFINITION": "ecs",
        "PAYLOAD_VERIFIER_TASK_DEFINITION": "ecs",
        "LOCAL_AUTH_VERIFIER_TASK_DEFINITION": "ecs",
        "ROLLBACK_DOCTOR_TASK_DEFINITION": "ecs",
        "PREVIOUS_TASK_DEFINITION": "ecs",
        "FIRST_DEPLOY_LISTENER_RULE_ARN": "elasticloadbalancing",
    }
    for field, service in arn_services.items():
        value = values[field]
        if not value:
            continue
        if type(value) is not str:
            raise AcceptanceCheckError("scenario_inventory_binding")
        match = re.fullmatch(r"arn:(?:aws|aws-us-gov|aws-cn):([^:]+):([^:]*):([^:]*):(.+)", value)
        if match is None or match.group(1) != service or match.group(2) != aws_region or match.group(3) != aws_account_id:
            raise AcceptanceCheckError("scenario_inventory_binding")
        if namespace not in match.group(4):
            raise AcceptanceCheckError("scenario_inventory_binding")
    for field in (
        "CANDIDATE_TASK_DEFINITION",
        "DOCTOR_TASK_DEFINITION",
        "PAYLOAD_VERIFIER_TASK_DEFINITION",
        "LOCAL_AUTH_VERIFIER_TASK_DEFINITION",
        "ROLLBACK_DOCTOR_TASK_DEFINITION",
    ):
        value = values[field]
        families = cast(list[str], orphan["ecs_task_definition_families"])
        if value and _task_definition_family(value) not in families:
            raise AcceptanceCheckError("scenario_inventory_binding")
    log_group_names = cast(list[str], orphan["log_group_names"])
    for field in ("WEB_LOG_GROUP", "DOCTOR_LOG_GROUP", "OPERATOR_METRICS_LOG_GROUP", "ECS_DEPLOYMENT_EVENT_LOG_GROUP"):
        value = values[field]
        if value and value not in log_group_names:
            raise AcceptanceCheckError("scenario_inventory_binding")
    event_rule = values["ECS_DEPLOYMENT_EVENT_RULE"]
    event_rules = cast(list[object], orphan["event_rules"])
    if event_rule and not any(isinstance(rule, Mapping) and rule.get("rule_name") == event_rule for rule in event_rules):
        raise AcceptanceCheckError("scenario_inventory_binding")
    listener_arns = cast(list[str], orphan["elbv2_listener_arns"])
    listener_pattern = re.compile(
        rf"arn:(?:aws|aws-us-gov|aws-cn):elasticloadbalancing:{re.escape(aws_region)}:{re.escape(aws_account_id)}:"
        rf"listener/app/[A-Za-z0-9-]*{re.escape(namespace)}[A-Za-z0-9-]*/[0-9a-f]{{16}}/[0-9a-f]{{16}}"
    )
    if values["ALB_ARN"] and not listener_arns:
        raise AcceptanceCheckError("scenario_inventory_binding")
    if any(listener_pattern.fullmatch(listener_arn) is None for listener_arn in listener_arns):
        raise AcceptanceCheckError("scenario_inventory_binding")
    listener_rule_arn = values["FIRST_DEPLOY_LISTENER_RULE_ARN"]
    if listener_rule_arn and _listener_arn_from_rule_arn(cast(str, listener_rule_arn)) not in listener_arns:
        raise AcceptanceCheckError("scenario_inventory_binding")
    event_target = values["ECS_DEPLOYMENT_EVENT_TARGET_ID"]
    if (
        event_rule
        and event_target
        and not any(
            isinstance(rule, Mapping) and rule.get("rule_name") == event_rule and event_target in cast(list[object], rule.get("target_ids"))
            for rule in event_rules
        )
    ):
        raise AcceptanceCheckError("scenario_inventory_binding")
    if scenario_id == "A":
        if orphan["cognito_pool_owned"] is not False or orphan["cognito_subject_sub"] != "":
            raise AcceptanceCheckError("scenario_inventory_binding")
    elif values["COGNITO_USER_POOL_ID"] and (orphan["cognito_pool_owned"] is not True or not orphan["cognito_subject_sub"]):
        raise AcceptanceCheckError("scenario_inventory_binding")


def _validate_resolved_scenario_values(
    values: Mapping[str, object],
    *,
    scenario_id: str,
    acceptance_run_id: str,
    aws_region: str,
) -> None:
    namespace = scenario_resource_namespace(acceptance_run_id, scenario_id)
    required_common = {
        "ECS_CLUSTER",
        "ECS_SERVICE",
        "WEB_CONTAINER_NAME",
        "ELSPETH_WEB__DATA_DIR",
        "ELSPETH_WEB__PAYLOAD_STORE_PATH",
        "TARGET_GROUP_ARN",
        "ALB_BASE_URL",
        "ALB_ARN",
        "CANDIDATE_TASK_DEFINITION",
        "DOCTOR_TASK_DEFINITION",
        "DOCTOR_CONTAINER_NAME",
        "DOCTOR_NETWORK_CONFIGURATION",
        "PAYLOAD_VERIFIER_TASK_DEFINITION",
        "LOCAL_AUTH_VERIFIER_TASK_DEFINITION",
        "WEB_LOG_GROUP",
        "WEB_LOG_STREAM_PREFIX",
        "DOCTOR_LOG_GROUP",
        "DOCTOR_LOG_STREAM_PREFIX",
        "OPERATOR_METRICS_LOG_GROUP",
        "ECS_DEPLOYMENT_EVENT_RULE",
        "ECS_DEPLOYMENT_EVENT_TARGET_ID",
        "ECS_DEPLOYMENT_EVENT_LOG_GROUP",
        "DB_CLUSTER_IDENTIFIER",
        "ELSPETH_TEST_S3_BUCKET",
    }
    if any(not values[field] for field in required_common):
        raise AcceptanceCheckError("scenario_inventory_schema")
    if values["DEPLOYMENT_MODE"] != ("first" if scenario_id == "A" else "upgrade"):
        raise AcceptanceCheckError("scenario_inventory_binding")
    data_dir = PurePosixPath(cast(str, values["ELSPETH_WEB__DATA_DIR"]))
    payload_root = PurePosixPath(cast(str, values["ELSPETH_WEB__PAYLOAD_STORE_PATH"]))
    if (
        not data_dir.is_absolute()
        or not payload_root.is_absolute()
        or data_dir == PurePosixPath("/")
        or payload_root == data_dir
        or data_dir not in payload_root.parents
        or payload_root == data_dir / "blobs"
    ):
        raise AcceptanceCheckError("scenario_inventory_schema")
    alb_url = cast(str, values["ALB_BASE_URL"])
    parsed_alb = urlsplit(alb_url)
    if (
        parsed_alb.scheme != "https"
        or not parsed_alb.hostname
        or parsed_alb.username is not None
        or parsed_alb.password is not None
        or parsed_alb.path not in {"", "/"}
        or parsed_alb.query
        or parsed_alb.fragment
        or namespace not in parsed_alb.hostname
    ):
        raise AcceptanceCheckError("scenario_inventory_schema")
    bucket = values["ELSPETH_TEST_S3_BUCKET"]
    if (
        type(bucket) is not str
        or re.fullmatch(r"(?=.{3,63}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])", bucket) is None
        or ".." in bucket
        or namespace not in bucket
    ):
        raise AcceptanceCheckError("scenario_inventory_schema")
    try:
        network = json.loads(cast(str, values["DOCTOR_NETWORK_CONFIGURATION"]))
    except json.JSONDecodeError:
        raise AcceptanceCheckError("scenario_inventory_schema") from None
    if not isinstance(network, dict) or set(network) != {"awsvpcConfiguration"}:
        raise AcceptanceCheckError("scenario_inventory_schema")
    awsvpc = network["awsvpcConfiguration"]
    if (
        not isinstance(awsvpc, dict)
        or set(awsvpc) != {"subnets", "securityGroups", "assignPublicIp"}
        or not isinstance(awsvpc["subnets"], list)
        or not awsvpc["subnets"]
        or not isinstance(awsvpc["securityGroups"], list)
        or not awsvpc["securityGroups"]
        or awsvpc["assignPublicIp"] not in {"ENABLED", "DISABLED"}
        or any(type(item) is not str or not item for item in [*awsvpc["subnets"], *awsvpc["securityGroups"]])
    ):
        raise AcceptanceCheckError("scenario_inventory_schema")
    listener_fields = {"FIRST_DEPLOY_LISTENER_RULE_ARN", "FIRST_DEPLOY_FORWARD_ACTIONS", "FIRST_DEPLOY_DISABLED_ACTIONS"}
    if any(not values[field] for field in listener_fields):
        raise AcceptanceCheckError("scenario_inventory_binding")
    for field in ("FIRST_DEPLOY_FORWARD_ACTIONS", "FIRST_DEPLOY_DISABLED_ACTIONS"):
        try:
            actions = json.loads(cast(str, values[field]))
        except json.JSONDecodeError:
            raise AcceptanceCheckError("scenario_inventory_schema") from None
        if not isinstance(actions, list) or not actions or any(not isinstance(action, dict) for action in actions):
            raise AcceptanceCheckError("scenario_inventory_schema")
        if field == "FIRST_DEPLOY_FORWARD_ACTIONS":
            target_group = values["TARGET_GROUP_ARN"]

            def forwards_to_expected_target(
                action: Mapping[str, object],
                expected_target_group: object = target_group,
            ) -> bool:
                if action.get("Type") != "forward":
                    return False
                if action.get("TargetGroupArn") == expected_target_group:
                    return True
                forward_config = action.get("ForwardConfig")
                if not isinstance(forward_config, Mapping):
                    return False
                targets = forward_config.get("TargetGroups")
                return isinstance(targets, list) and any(
                    isinstance(target, Mapping) and target.get("TargetGroupArn") == expected_target_group for target in targets
                )

            if not any(forwards_to_expected_target(cast(Mapping[str, object], action)) for action in actions):
                raise AcceptanceCheckError("scenario_inventory_binding")
        elif not any(
            action.get("Type") == "fixed-response"
            and isinstance(action.get("FixedResponseConfig"), Mapping)
            and cast(Mapping[str, object], action["FixedResponseConfig"]).get("StatusCode") == "503"
            for action in actions
        ):
            raise AcceptanceCheckError("scenario_inventory_binding")

    if scenario_id == "A":
        forbidden = {
            "PREVIOUS_TASK_DEFINITION",
            "ROLLBACK_DOCTOR_TASK_DEFINITION",
            "COGNITO_USER_POOL_ID",
            "OIDC_EXPECTED_ISSUER",
            "OIDC_EXPECTED_AUDIENCE",
            "OIDC_EXPECTED_AUTHORIZATION_ORIGIN",
        }
        if any(values[field] for field in forbidden):
            raise AcceptanceCheckError("scenario_inventory_binding")
    else:
        required = {
            "PREVIOUS_TASK_DEFINITION",
            "ROLLBACK_DOCTOR_TASK_DEFINITION",
            "COGNITO_USER_POOL_ID",
            "OIDC_EXPECTED_ISSUER",
            "OIDC_EXPECTED_AUDIENCE",
            "OIDC_EXPECTED_AUTHORIZATION_ORIGIN",
        }
        if any(not values[field] for field in required):
            raise AcceptanceCheckError("scenario_inventory_binding")
        pool_id = cast(str, values["COGNITO_USER_POOL_ID"])
        if re.fullmatch(rf"{re.escape(aws_region)}_[A-Za-z0-9]+", pool_id) is None:
            raise AcceptanceCheckError("scenario_inventory_schema")
        if values["OIDC_EXPECTED_ISSUER"] != f"https://cognito-idp.{aws_region}.amazonaws.com/{pool_id}":
            raise AcceptanceCheckError("scenario_inventory_binding")
        authorization_origin = urlsplit(cast(str, values["OIDC_EXPECTED_AUTHORIZATION_ORIGIN"]))
        if (
            authorization_origin.scheme != "https"
            or not authorization_origin.hostname
            or authorization_origin.path not in {"", "/"}
            or authorization_origin.query
            or authorization_origin.fragment
            or namespace not in authorization_origin.hostname
        ):
            raise AcceptanceCheckError("scenario_inventory_schema")
        if re.fullmatch(r"[A-Za-z0-9_-]{8,128}", cast(str, values["OIDC_EXPECTED_AUDIENCE"])) is None:
            raise AcceptanceCheckError("scenario_inventory_schema")


def _validate_scenario_inventory(
    payload: object,
    *,
    scenario_id: str,
    acceptance_run_id: str,
    candidate_sha: str,
    aws_account_id: str,
    aws_region: str,
    tf_binding_sha256: str,
    expected_phase: Literal["preapply", "resolved"],
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "acceptance_run_id",
        "candidate_sha",
        "aws_account_id",
        "aws_region",
        "scenario_id",
        "phase",
        "values",
        "orphan_sweep",
    }:
        raise AcceptanceCheckError("scenario_inventory_schema")
    if (
        payload["schema"] != "elspeth.aws-ecs-scenario-inventory.v6"
        or payload["scenario_id"] != scenario_id
        or payload["phase"] != expected_phase
        or payload["acceptance_run_id"] != acceptance_run_id
        or payload["candidate_sha"] != candidate_sha
        or payload["aws_account_id"] != aws_account_id
        or payload["aws_region"] != aws_region
    ):
        raise AcceptanceCheckError("scenario_inventory_binding")
    values = payload["values"]
    if not isinstance(values, dict) or set(values) != _SCENARIO_VALUE_FIELDS:
        raise AcceptanceCheckError("scenario_inventory_schema")
    for value in values.values():
        if type(value) is not str or len(value) > 16 * 1024 or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise AcceptanceCheckError("scenario_inventory_schema")
    if (
        values["AWS_REGION"] != aws_region
        or values["SCENARIO_TF_BINDING_SHA"] != tf_binding_sha256
        or values["DEPLOYMENT_MODE"] != ("first" if scenario_id == "A" else "upgrade")
        or values["TARGET_PLATFORM"] not in {"linux/amd64", "linux/arm64"}
        or values["OIDC_EXPECTED_AUDIENCE_CLAIM"] not in {"aud", "client_id"}
    ):
        raise AcceptanceCheckError("scenario_inventory_binding")
    try:
        policy_binding = plugin_policy_binding_sha256(cast(Mapping[str, str], values))
    except AcceptanceCheckError:
        raise AcceptanceCheckError("scenario_inventory_schema") from None
    if values["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"] != policy_binding:
        raise AcceptanceCheckError("scenario_inventory_binding")
    live_model = values["ELSPETH_BEDROCK_LIVE_TEST_MODEL"]
    if not live_model.startswith("bedrock/") or any(character.isspace() for character in live_model):
        raise AcceptanceCheckError("scenario_inventory_schema")
    for name in _TASK_DEFINITION_COMPOSER_MODEL_ENV:
        model = values[name]
        if (
            not model
            or any(character.isspace() for character in model)
            or (infer_provider_from_model_name(model) or infer_provider_from_unprefixed_model_name(model)) is None
        ):
            raise AcceptanceCheckError("scenario_inventory_schema")
    for name in PLUGIN_POLICY_ASSIGNMENT_NAMES:
        if name == "ELSPETH_WEB__TUTORIAL_LLM_PROFILE":
            continue
        try:
            json.loads(cast(str, values[name]))
        except json.JSONDecodeError:
            raise AcceptanceCheckError("scenario_inventory_schema") from None
    for field in (
        "ECS_CLUSTER",
        "ECS_SERVICE",
        "WEB_CONTAINER_NAME",
        "ELSPETH_WEB__DATA_DIR",
        "ELSPETH_WEB__PAYLOAD_STORE_PATH",
        "DOCTOR_CONTAINER_NAME",
        "WEB_LOG_GROUP",
        "WEB_LOG_STREAM_PREFIX",
        "DOCTOR_LOG_GROUP",
        "DOCTOR_LOG_STREAM_PREFIX",
        "OPERATOR_METRICS_LOG_GROUP",
        "ECS_DEPLOYMENT_EVENT_RULE",
        "ECS_DEPLOYMENT_EVENT_TARGET_ID",
        "ECS_DEPLOYMENT_EVENT_LOG_GROUP",
        "DB_CLUSTER_IDENTIFIER",
        "ELSPETH_TEST_S3_BUCKET",
        "SCENARIO_TF_DIR",
        "SCENARIO_TF_VARS",
        "SCENARIO_TF_BINDING_FILE",
    ):
        if not values[field]:
            raise AcceptanceCheckError("scenario_inventory_schema")
    _control_path(values["SCENARIO_TF_DIR"])
    _control_path(values["SCENARIO_TF_VARS"])
    _control_path(values["SCENARIO_TF_BINDING_FILE"])
    orphan = _validate_orphan_inventory(payload["orphan_sweep"])
    try:
        guardrail_profiles_payload = json.loads(cast(str, values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"]))
    except json.JSONDecodeError:
        raise AcceptanceCheckError("scenario_inventory_schema") from None
    if expected_phase == "preapply":
        if guardrail_profiles_payload != []:
            raise AcceptanceCheckError("scenario_inventory_schema")
    else:
        if not isinstance(guardrail_profiles_payload, list) or len(guardrail_profiles_payload) != 2:
            raise AcceptanceCheckError("scenario_inventory_schema")
        try:
            guardrail_profiles = [BedrockGuardrailProfileSettings.model_validate(profile) for profile in guardrail_profiles_payload]
        except Exception:
            raise AcceptanceCheckError("scenario_inventory_schema") from None
        if {profile.plugin for profile in guardrail_profiles} != {
            "aws_bedrock_prompt_shield",
            "aws_bedrock_content_safety",
        } or any(profile.region != aws_region for profile in guardrail_profiles):
            raise AcceptanceCheckError("scenario_inventory_binding")
        try:
            guardrail_defaults = json.loads(cast(str, values["ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES"]))
        except json.JSONDecodeError:
            raise AcceptanceCheckError("scenario_inventory_schema") from None
        if (
            not isinstance(guardrail_defaults, dict)
            or set(guardrail_defaults)
            != {
                "aws_bedrock_prompt_shield",
                "aws_bedrock_content_safety",
            }
            or any(guardrail_defaults[profile.plugin] != profile.alias for profile in guardrail_profiles)
        ):
            raise AcceptanceCheckError("scenario_inventory_binding")
        owned_guardrails = cast(list[Mapping[str, object]], orphan["bedrock_guardrails"])
        owned_versions = {cast(str, guardrail["identifier"]): set(cast(list[str], guardrail["versions"])) for guardrail in owned_guardrails}
        if (
            len(owned_versions) != 2
            or {profile.guardrail_identifier for profile in guardrail_profiles} != set(owned_versions)
            or any(
                profile.guardrail_identifier not in owned_versions
                or profile.guardrail_version not in owned_versions[profile.guardrail_identifier]
                for profile in guardrail_profiles
            )
        ):
            raise AcceptanceCheckError("scenario_inventory_binding")
    for field in (
        "ecs_task_definition_families",
        "rds_db_instance_identifiers",
        "efs_creation_tokens",
        "secret_ids",
        "iam_role_names",
        "log_group_names",
        "log_resource_policy_names",
        "cloudwatch_dashboard_names",
        "cloudwatch_alarm_names",
        "xray_group_names",
        "xray_sampling_rule_names",
        "event_rules",
    ):
        if not orphan[field]:
            raise AcceptanceCheckError("scenario_inventory_schema")
    if len(cast(list[str], orphan["log_resource_policy_names"])) != 1:
        raise AcceptanceCheckError("scenario_inventory_schema")
    if any(orphan[field] != 0 for field in ("expected_retained_metric_series", "expected_retained_trace_ids")):
        raise AcceptanceCheckError("scenario_inventory_schema")
    if orphan["cloudwatch_retained_metrics"] or orphan["xray_retained_trace_ids"]:
        raise AcceptanceCheckError("scenario_inventory_schema")
    _validate_scenario_resource_bindings(
        values,
        orphan,
        scenario_id=scenario_id,
        acceptance_run_id=acceptance_run_id,
        aws_account_id=aws_account_id,
        aws_region=aws_region,
    )
    if expected_phase == "preapply":
        if any(values[field] for field in _PROVIDER_GENERATED_SCENARIO_FIELDS):
            raise AcceptanceCheckError("scenario_inventory_schema")
        empty_provider_orphans: dict[str, object] = {
            "elbv2_listener_arns": [],
            "efs_file_system_ids": [],
            "efs_access_point_ids": [],
            "bedrock_guardrails": [],
            "cognito_subject_sub": "",
            "cognito_pool_owned": False,
        }
        if any(orphan[field] != empty_value for field, empty_value in empty_provider_orphans.items()):
            raise AcceptanceCheckError("scenario_inventory_schema")
    else:
        file_system_ids = cast(list[str], orphan["efs_file_system_ids"])
        access_point_ids = cast(list[str], orphan["efs_access_point_ids"])
        if (
            len(file_system_ids) != 1
            or len(access_point_ids) != 1
            or re.fullmatch(r"fs-[0-9a-f]{8,40}", file_system_ids[0]) is None
            or re.fullmatch(r"fsap-[0-9a-f]{8,40}", access_point_ids[0]) is None
        ):
            raise AcceptanceCheckError("scenario_inventory_schema")
        _validate_resolved_scenario_values(
            values,
            scenario_id=scenario_id,
            acceptance_run_id=acceptance_run_id,
            aws_region=aws_region,
        )
    _validate_tf_binding_receipt(
        Path(values["SCENARIO_TF_BINDING_FILE"]),
        scenario_id=scenario_id,
        acceptance_run_id=acceptance_run_id,
        aws_account_id=aws_account_id,
        aws_region=aws_region,
        expected_sha256=tf_binding_sha256,
    )
    return payload


def _validate_scenario_inventory_isolation(
    inventory_a: Mapping[str, object],
    inventory_b: Mapping[str, object],
) -> None:
    values_a = inventory_a["values"]
    values_b = inventory_b["values"]
    orphan_a = inventory_a["orphan_sweep"]
    orphan_b = inventory_b["orphan_sweep"]
    assert isinstance(values_a, Mapping) and isinstance(values_b, Mapping)
    assert isinstance(orphan_a, Mapping) and isinstance(orphan_b, Mapping)
    isolated_value_fields = {
        "ECS_CLUSTER",
        "ECS_SERVICE",
        "TARGET_GROUP_ARN",
        "ALB_BASE_URL",
        "ALB_ARN",
        "CANDIDATE_TASK_DEFINITION",
        "DOCTOR_TASK_DEFINITION",
        "DOCTOR_NETWORK_CONFIGURATION",
        "PAYLOAD_VERIFIER_TASK_DEFINITION",
        "LOCAL_AUTH_VERIFIER_TASK_DEFINITION",
        "WEB_LOG_GROUP",
        "DOCTOR_LOG_GROUP",
        "OPERATOR_METRICS_LOG_GROUP",
        "ECS_DEPLOYMENT_EVENT_RULE",
        "ECS_DEPLOYMENT_EVENT_TARGET_ID",
        "ECS_DEPLOYMENT_EVENT_LOG_GROUP",
        "DB_CLUSTER_IDENTIFIER",
        "ELSPETH_TEST_S3_BUCKET",
        "SCENARIO_TF_DIR",
        "SCENARIO_TF_VARS",
        "SCENARIO_TF_BINDING_FILE",
    }
    if any(values_a[field] and values_a[field] == values_b[field] for field in isolated_value_fields):
        raise AcceptanceCheckError("scenario_inventory_isolation")
    for field in (
        "ecs_task_definition_families",
        "elbv2_listener_arns",
        "rds_db_instance_identifiers",
        "efs_creation_tokens",
        "efs_file_system_ids",
        "efs_access_point_ids",
        "secret_ids",
        "iam_role_names",
        "log_group_names",
        "log_resource_policy_names",
        "cloudwatch_dashboard_names",
        "cloudwatch_alarm_names",
        "xray_group_names",
        "xray_sampling_rule_names",
    ):
        if set(cast(list[str], orphan_a[field])) & set(cast(list[str], orphan_b[field])):
            raise AcceptanceCheckError("scenario_inventory_isolation")
    rules_a = cast(list[Mapping[str, object]], orphan_a["event_rules"])
    rules_b = cast(list[Mapping[str, object]], orphan_b["event_rules"])
    if {(rule["event_bus_name"], rule["rule_name"]) for rule in rules_a} & {
        (rule["event_bus_name"], rule["rule_name"]) for rule in rules_b
    }:
        raise AcceptanceCheckError("scenario_inventory_isolation")
    targets_a = {target for rule in rules_a for target in cast(list[str], rule["target_ids"])}
    targets_b = {target for rule in rules_b for target in cast(list[str], rule["target_ids"])}
    if targets_a & targets_b:
        raise AcceptanceCheckError("scenario_inventory_isolation")
    guardrails_a = cast(list[Mapping[str, object]], orphan_a["bedrock_guardrails"])
    guardrails_b = cast(list[Mapping[str, object]], orphan_b["bedrock_guardrails"])
    if {guardrail["identifier"] for guardrail in guardrails_a} & {guardrail["identifier"] for guardrail in guardrails_b}:
        raise AcceptanceCheckError("scenario_inventory_isolation")


def _load_preapply_scenario_inventory(manifest: Mapping[str, object], scenario_id: str) -> dict[str, object]:
    scenarios = manifest["scenarios"]
    aws = manifest["aws"]
    assert isinstance(scenarios, dict) and isinstance(aws, dict)
    scenario = scenarios[scenario_id]
    assert isinstance(scenario, dict)
    inventory = _validate_scenario_inventory(
        _read_protected_document(Path(cast(str, scenario["preapply_inventory_path"])), check="scenario_inventory_file"),
        scenario_id=scenario_id,
        acceptance_run_id=cast(str, manifest["acceptance_run_id"]),
        candidate_sha=cast(str, manifest["candidate_sha"]),
        aws_account_id=cast(str, aws["account_id"]),
        aws_region=cast(str, aws["region"]),
        tf_binding_sha256=cast(str, scenario["tf_binding_sha256"]),
        expected_phase="preapply",
    )
    if _scenario_inventory_hash(inventory) != scenario["preapply_inventory_sha256"]:
        raise AcceptanceCheckError("scenario_inventory_binding")
    return inventory


def _load_bound_scenario_inventory(
    manifest: Mapping[str, object],
    scenario_id: str,
    *,
    require_resolved: bool = False,
) -> dict[str, object]:
    scenarios = manifest["scenarios"]
    aws = manifest["aws"]
    assert isinstance(scenarios, dict) and isinstance(aws, dict)
    scenario = scenarios[scenario_id]
    assert isinstance(scenario, dict)
    acceptance_run_id = manifest["acceptance_run_id"]
    candidate_sha = manifest["candidate_sha"]
    account_id = aws["account_id"]
    region = aws["region"]
    binding = scenario["tf_binding_sha256"]
    assert isinstance(acceptance_run_id, str)
    assert isinstance(candidate_sha, str)
    assert isinstance(account_id, str)
    assert isinstance(region, str)
    assert isinstance(binding, str)
    _load_preapply_scenario_inventory(manifest, scenario_id)
    inventory = _validate_scenario_inventory(
        _read_protected_document(Path(scenario["inventory_path"]), check="scenario_inventory_file"),
        scenario_id=scenario_id,
        acceptance_run_id=acceptance_run_id,
        candidate_sha=candidate_sha,
        aws_account_id=account_id,
        aws_region=region,
        tf_binding_sha256=binding,
        expected_phase=cast(Literal["preapply", "resolved"], scenario["inventory_phase"]),
    )
    if require_resolved and scenario["inventory_phase"] != "resolved":
        raise AcceptanceCheckError("scenario_inventory_unresolved")
    if _scenario_inventory_hash(inventory) != scenario["inventory_sha256"]:
        raise AcceptanceCheckError("scenario_inventory_binding")
    values = inventory["values"]
    assert isinstance(values, dict)
    if values["SCENARIO_TF_BINDING_FILE"] != scenario["tf_binding_path"]:
        raise AcceptanceCheckError("tf_binding_binding")
    _, state_identity = _validate_tf_binding_receipt(
        Path(cast(str, scenario["tf_binding_path"])),
        scenario_id=scenario_id,
        acceptance_run_id=acceptance_run_id,
        aws_account_id=account_id,
        aws_region=region,
        expected_sha256=binding,
    )
    if state_identity != scenario["tf_state_identity_sha256"]:
        raise AcceptanceCheckError("tf_binding_binding")
    return inventory


def _validate_retained_evidence_receipt(
    payload: object,
    *,
    manifest: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "acceptance_run_id",
        "candidate_sha",
        "scenarios",
        "captured_at",
    }:
        raise AcceptanceCheckError("retained_evidence_schema")
    if (
        payload["schema"] != "elspeth.aws-ecs-retained-evidence.v1"
        or payload["acceptance_run_id"] != manifest["acceptance_run_id"]
        or payload["candidate_sha"] != manifest["candidate_sha"]
    ):
        raise AcceptanceCheckError("retained_evidence_binding")
    _control_timestamp(payload["captured_at"])
    scenario_evidence = payload["scenarios"]
    if not isinstance(scenario_evidence, dict) or set(scenario_evidence) != {"A", "B"}:
        raise AcceptanceCheckError("retained_evidence_schema")
    canonical_metrics: dict[str, set[str]] = {}
    trace_ids: dict[str, set[str]] = {}
    acceptance_run_id = cast(str, manifest["acceptance_run_id"])
    for scenario_id in ("A", "B"):
        inventory = _load_bound_scenario_inventory(manifest, scenario_id, require_resolved=True)
        orphan = inventory["orphan_sweep"]
        evidence = scenario_evidence[scenario_id]
        assert isinstance(orphan, dict)
        if not isinstance(evidence, dict) or set(evidence) != _RETAINED_EVIDENCE_FIELDS:
            raise AcceptanceCheckError("retained_evidence_schema")
        candidate_orphan = {**orphan, **evidence}
        _validate_orphan_inventory(candidate_orphan)
        metrics = cast(list[dict[str, object]], evidence["cloudwatch_retained_metrics"])
        traces = cast(list[str], evidence["xray_retained_trace_ids"])
        if len(metrics) != len(traces):
            raise AcceptanceCheckError("retained_evidence_schema")
        namespace = f"{acceptance_run_id}-{scenario_id.lower()}"
        if any(
            not any(
                isinstance(dimension, Mapping)
                and dimension.get("name") == "elspeth.acceptance.namespace"
                and dimension.get("value") == namespace
                for dimension in cast(list[object], metric["dimensions"])
            )
            for metric in metrics
        ):
            raise AcceptanceCheckError("retained_evidence_binding")
        canonical_metrics[scenario_id] = {json.dumps(metric, sort_keys=True, separators=(",", ":")) for metric in metrics}
        trace_ids[scenario_id] = set(traces)
    if canonical_metrics["A"] & canonical_metrics["B"] or trace_ids["A"] & trace_ids["B"]:
        raise AcceptanceCheckError("retained_evidence_binding")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return payload, _sha256(canonical)


def _load_retained_evidence(manifest: Mapping[str, object]) -> dict[str, object]:
    evidence = manifest["evidence"]
    assert isinstance(evidence, Mapping)
    path = evidence["retained_evidence_path"]
    expected_sha256 = evidence["retained_evidence_sha256"]
    if type(path) is not str or type(expected_sha256) is not str:
        raise AcceptanceCheckError("retained_evidence_missing")
    receipt, observed_sha256 = _validate_retained_evidence_receipt(
        _read_protected_document(Path(path), check="retained_evidence_file"),
        manifest=manifest,
    )
    if observed_sha256 != expected_sha256:
        raise AcceptanceCheckError("retained_evidence_binding")
    return receipt


@_serialized_control_manifest_write
def control_manifest_bind_retained_evidence(
    path: Path,
    *,
    receipt_path: str,
    require_complete: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Bind an immutable monotonic checkpoint of retained metric/trace identities."""

    manifest = _read_control_manifest(path)
    _require_mutable_control_manifest(manifest)
    current = now()
    if current >= _control_timestamp(manifest["teardown_deadline_utc"]):
        raise AcceptanceCheckError("control_manifest_expired")
    protected_path = _control_path(receipt_path)
    receipt, receipt_sha256 = _validate_retained_evidence_receipt(
        _read_protected_document(Path(protected_path), check="retained_evidence_file"),
        manifest=manifest,
    )
    if require_complete:
        scenarios = receipt["scenarios"]
        assert isinstance(scenarios, dict)
        if any(
            not cast(dict[str, object], scenarios[scenario_id])["cloudwatch_retained_metrics"]
            or not cast(dict[str, object], scenarios[scenario_id])["xray_retained_trace_ids"]
            for scenario_id in ("A", "B")
        ):
            raise AcceptanceCheckError("retained_evidence_incomplete")
    evidence = manifest["evidence"]
    assert isinstance(evidence, dict)
    if evidence["retained_evidence_path"] is not None:
        if evidence["retained_evidence_path"] == protected_path and evidence["retained_evidence_sha256"] == receipt_sha256:
            return manifest
        previous = _load_retained_evidence(manifest)
        if _control_timestamp(cast(str, receipt["captured_at"])) < _control_timestamp(cast(str, previous["captured_at"])):
            raise AcceptanceCheckError("retained_evidence_conflict")
        previous_scenarios = previous["scenarios"]
        next_scenarios = receipt["scenarios"]
        assert isinstance(previous_scenarios, dict) and isinstance(next_scenarios, dict)
        grew = False
        for scenario_id in ("A", "B"):
            previous_scenario = previous_scenarios[scenario_id]
            next_scenario = next_scenarios[scenario_id]
            assert isinstance(previous_scenario, dict) and isinstance(next_scenario, dict)
            previous_metrics = {
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in cast(list[dict[str, object]], previous_scenario["cloudwatch_retained_metrics"])
            }
            next_metrics = {
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in cast(list[dict[str, object]], next_scenario["cloudwatch_retained_metrics"])
            }
            previous_traces = set(cast(list[str], previous_scenario["xray_retained_trace_ids"]))
            next_traces = set(cast(list[str], next_scenario["xray_retained_trace_ids"]))
            if not previous_metrics <= next_metrics or not previous_traces <= next_traces:
                raise AcceptanceCheckError("retained_evidence_conflict")
            grew = grew or previous_metrics != next_metrics or previous_traces != next_traces
        if not grew:
            raise AcceptanceCheckError("retained_evidence_conflict")
    evidence["retained_evidence_path"] = protected_path
    evidence["retained_evidence_sha256"] = receipt_sha256
    manifest["updated_at"] = _utc_timestamp(current)
    _validate_control_manifest(manifest)
    _write_protected_document(
        path,
        manifest,
        create=False,
        exists_check="control_manifest_exists",
        write_check="control_manifest_file",
    )
    return manifest


def control_manifest_checkpoint_operator_evidence(
    path: Path,
    *,
    exec_receipt_path: str,
    checkpoint_path: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Create and bind the next immutable checkpoint from one positive operator receipt."""

    manifest = _read_control_manifest(path)
    _require_mutable_control_manifest(manifest)
    exec_receipt = _validate_exec_receipt_schema(
        _read_protected_document(Path(_control_path(exec_receipt_path)), check="operator_exec_receipt_file")
    )
    scenario_id = exec_receipt["scenario_id"]
    details = exec_receipt["details"]
    if (
        exec_receipt["check"] != "verify-operator-telemetry"
        or exec_receipt["candidate_sha"] != manifest["candidate_sha"]
        or scenario_id not in {"A", "B"}
        or not isinstance(details, dict)
        or details["phase"] != "positive"
    ):
        raise AcceptanceCheckError("retained_evidence_binding")
    metric_query = details["retained_metric_query"]
    trace_id = details["retained_trace_id"]
    assert isinstance(metric_query, dict) and isinstance(trace_id, str)
    protected_checkpoint = Path(_control_path(checkpoint_path))
    current = now()

    if protected_checkpoint.exists():
        existing, _existing_sha256 = _validate_retained_evidence_receipt(
            _read_protected_document(protected_checkpoint, check="retained_evidence_file"),
            manifest=manifest,
        )
        existing_scenarios = existing["scenarios"]
        assert isinstance(existing_scenarios, dict)
        existing_scenario = existing_scenarios[scenario_id]
        assert isinstance(existing_scenario, dict)
        if (
            metric_query not in existing_scenario["cloudwatch_retained_metrics"]
            or trace_id not in existing_scenario["xray_retained_trace_ids"]
        ):
            raise AcceptanceCheckError("retained_evidence_conflict")
    else:
        evidence = manifest["evidence"]
        assert isinstance(evidence, dict)
        if evidence["retained_evidence_path"] is None:
            scenarios: dict[str, object] = {
                scenario: {
                    "cloudwatch_retained_metrics": [],
                    "xray_retained_trace_ids": [],
                    "expected_retained_metric_series": 0,
                    "expected_retained_trace_ids": 0,
                }
                for scenario in ("A", "B")
            }
        else:
            previous = _load_retained_evidence(manifest)
            scenarios = json.loads(json.dumps(previous["scenarios"]))
        scenario = scenarios[scenario_id]
        assert isinstance(scenario, dict)
        metrics = cast(list[dict[str, object]], scenario["cloudwatch_retained_metrics"])
        traces = cast(list[str], scenario["xray_retained_trace_ids"])
        if metric_query in metrics or trace_id in traces:
            raise AcceptanceCheckError("retained_evidence_conflict")
        metrics.append(metric_query)
        traces.append(trace_id)
        scenario["expected_retained_metric_series"] = len(metrics)
        scenario["expected_retained_trace_ids"] = len(traces)
        checkpoint = {
            "schema": "elspeth.aws-ecs-retained-evidence.v1",
            "acceptance_run_id": manifest["acceptance_run_id"],
            "candidate_sha": manifest["candidate_sha"],
            "scenarios": scenarios,
            "captured_at": _utc_timestamp(current),
        }
        _validate_retained_evidence_receipt(checkpoint, manifest=manifest)
        _write_protected_document(
            protected_checkpoint,
            checkpoint,
            create=True,
            exists_check="retained_evidence_exists",
            write_check="retained_evidence_file",
            parent_check="retained_evidence_parent",
        )
    return control_manifest_bind_retained_evidence(
        path,
        receipt_path=str(protected_checkpoint),
        now=lambda: current,
    )


def control_manifest_init(
    path: Path,
    *,
    acceptance_run_id: str,
    candidate_sha: str,
    aws_account_id: str,
    aws_region: str,
    scenario_a_inventory: str,
    scenario_b_inventory: str,
    scenario_a_tf_binding: str,
    scenario_b_tf_binding: str,
    evidence_destination_sha256: str,
    gate_ledger: str,
    teardown_deadline_utc: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Create one fresh, closed, interruption-safe Plan 12 control manifest."""

    scenario_a_document = _validate_scenario_inventory(
        _read_protected_document(Path(scenario_a_inventory), check="scenario_inventory_file"),
        scenario_id="A",
        acceptance_run_id=acceptance_run_id,
        candidate_sha=candidate_sha,
        aws_account_id=aws_account_id,
        aws_region=aws_region,
        tf_binding_sha256=scenario_a_tf_binding,
        expected_phase="preapply",
    )
    scenario_b_document = _validate_scenario_inventory(
        _read_protected_document(Path(scenario_b_inventory), check="scenario_inventory_file"),
        scenario_id="B",
        acceptance_run_id=acceptance_run_id,
        candidate_sha=candidate_sha,
        aws_account_id=aws_account_id,
        aws_region=aws_region,
        tf_binding_sha256=scenario_b_tf_binding,
        expected_phase="preapply",
    )
    _validate_scenario_inventory_isolation(scenario_a_document, scenario_b_document)
    scenario_a_values = scenario_a_document["values"]
    scenario_b_values = scenario_b_document["values"]
    assert isinstance(scenario_a_values, dict) and isinstance(scenario_b_values, dict)
    scenario_a_binding_path = scenario_a_values["SCENARIO_TF_BINDING_FILE"]
    scenario_b_binding_path = scenario_b_values["SCENARIO_TF_BINDING_FILE"]
    assert isinstance(scenario_a_binding_path, str) and isinstance(scenario_b_binding_path, str)
    _, scenario_a_state_identity = _validate_tf_binding_receipt(
        Path(scenario_a_binding_path),
        scenario_id="A",
        acceptance_run_id=acceptance_run_id,
        aws_account_id=aws_account_id,
        aws_region=aws_region,
        expected_sha256=scenario_a_tf_binding,
    )
    _, scenario_b_state_identity = _validate_tf_binding_receipt(
        Path(scenario_b_binding_path),
        scenario_id="B",
        acceptance_run_id=acceptance_run_id,
        aws_account_id=aws_account_id,
        aws_region=aws_region,
        expected_sha256=scenario_b_tf_binding,
    )
    if scenario_a_state_identity == scenario_b_state_identity:
        raise AcceptanceCheckError("tf_binding_binding")
    timestamp = _utc_timestamp(now())
    manifest: dict[str, object] = {
        "schema": "elspeth.aws-ecs-control-manifest.v5",
        "acceptance_run_id": acceptance_run_id,
        "candidate_sha": candidate_sha,
        "aws": {"account_id": aws_account_id, "region": aws_region},
        "scenarios": {
            "A": {
                "preapply_inventory_path": scenario_a_inventory,
                "preapply_inventory_sha256": _scenario_inventory_hash(scenario_a_document),
                "inventory_path": scenario_a_inventory,
                "inventory_sha256": _scenario_inventory_hash(scenario_a_document),
                "inventory_phase": "preapply",
                "tf_binding_sha256": scenario_a_tf_binding,
                "tf_binding_path": scenario_a_binding_path,
                "tf_state_identity_sha256": scenario_a_state_identity,
                "terraform_plan_receipt": None,
                "terraform_applied": False,
                "terraform_noop_receipt": None,
            },
            "B": {
                "preapply_inventory_path": scenario_b_inventory,
                "preapply_inventory_sha256": _scenario_inventory_hash(scenario_b_document),
                "inventory_path": scenario_b_inventory,
                "inventory_sha256": _scenario_inventory_hash(scenario_b_document),
                "inventory_phase": "preapply",
                "tf_binding_sha256": scenario_b_tf_binding,
                "tf_binding_path": scenario_b_binding_path,
                "tf_state_identity_sha256": scenario_b_state_identity,
                "terraform_plan_receipt": None,
                "terraform_applied": False,
                "terraform_noop_receipt": None,
            },
        },
        "gate_ledger_path": gate_ledger,
        "teardown_deadline_utc": teardown_deadline_utc,
        "emergency_cleanup_deadline_utc": None,
        "cleanup_required": False,
        "cleanup_states": dict.fromkeys(_CLEANUP_SURFACES, "pending"),
        "ecr": {
            "registry": None,
            "repository": None,
            "baseline_tag": None,
            "candidate_tag": None,
            "baseline_digest": None,
            "candidate_digest": None,
        },
        "evidence": {
            "acceptance_state_path": None,
            "oidc_evidence_dir": None,
            "destination_sha256": evidence_destination_sha256,
            "export_receipt_path": None,
            "export_receipt_sha256": None,
            "final_export_receipt_path": None,
            "final_export_receipt_sha256": None,
            "retained_evidence_path": None,
            "retained_evidence_sha256": None,
            "receipts": [],
            "approvals": [],
        },
        "verdict_failures": [],
        "cleanup_escalations": [],
        "deadline_failure_recorded": False,
        "final_evidence": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _validate_control_manifest(manifest)
    if _control_timestamp(teardown_deadline_utc) <= now():
        raise AcceptanceCheckError("control_manifest_expired")
    _write_protected_document(
        path,
        manifest,
        create=True,
        exists_check="control_manifest_exists",
        write_check="control_manifest_write",
    )
    return manifest


@_serialized_control_manifest_write
def control_manifest_bind_scenario(
    path: Path,
    *,
    scenario_id: str,
    inventory_path: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Atomically replace a pre-apply inventory with its immutable resolved inventory."""

    if scenario_id not in {"A", "B"}:
        raise AcceptanceCheckError("scenario_inventory_binding")
    manifest = _read_control_manifest(path)
    _require_mutable_control_manifest(manifest)
    scenarios = manifest["scenarios"]
    aws = manifest["aws"]
    assert isinstance(scenarios, dict) and isinstance(aws, dict)
    scenario = scenarios[scenario_id]
    assert isinstance(scenario, dict)
    resolved_path = _control_path(inventory_path)
    inventory = _validate_scenario_inventory(
        _read_protected_document(Path(resolved_path), check="scenario_inventory_file"),
        scenario_id=scenario_id,
        acceptance_run_id=cast(str, manifest["acceptance_run_id"]),
        candidate_sha=cast(str, manifest["candidate_sha"]),
        aws_account_id=cast(str, aws["account_id"]),
        aws_region=cast(str, aws["region"]),
        tf_binding_sha256=cast(str, scenario["tf_binding_sha256"]),
        expected_phase="resolved",
    )
    inventory_sha256 = _scenario_inventory_hash(inventory)
    if scenario["inventory_phase"] == "resolved":
        if scenario["inventory_path"] == resolved_path and scenario["inventory_sha256"] == inventory_sha256:
            return manifest
        raise AcceptanceCheckError("scenario_inventory_conflict")
    if scenario["inventory_phase"] != "preapply" or scenario["inventory_path"] == resolved_path:
        raise AcceptanceCheckError("scenario_inventory_conflict")
    if now() >= _control_timestamp(manifest["teardown_deadline_utc"]):
        raise AcceptanceCheckError("control_manifest_expired")
    if (
        scenario["terraform_plan_receipt"] is None
        or scenario["terraform_applied"] is not True
        or scenario["terraform_noop_receipt"] is None
    ):
        raise AcceptanceCheckError("scenario_inventory_unresolved")
    preapply = _load_preapply_scenario_inventory(manifest, scenario_id)
    preapply_values = preapply["values"]
    preapply_orphan = preapply["orphan_sweep"]
    values = inventory["values"]
    orphan = inventory["orphan_sweep"]
    assert isinstance(values, dict) and isinstance(orphan, dict)
    assert isinstance(preapply_values, dict) and isinstance(preapply_orphan, dict)
    if any(values[field] != preapply_values[field] for field in _SCENARIO_VALUE_FIELDS - _RESOLVED_SCENARIO_FIELDS):
        raise AcceptanceCheckError("scenario_inventory_conflict")
    if any(orphan[field] != preapply_orphan[field] for field in _ORPHAN_INVENTORY_FIELDS - _PROVIDER_GENERATED_ORPHAN_FIELDS):
        raise AcceptanceCheckError("scenario_inventory_conflict")
    if values["SCENARIO_TF_BINDING_FILE"] != scenario["tf_binding_path"]:
        raise AcceptanceCheckError("tf_binding_binding")
    _, state_identity = _validate_tf_binding_receipt(
        Path(cast(str, scenario["tf_binding_path"])),
        scenario_id=scenario_id,
        acceptance_run_id=cast(str, manifest["acceptance_run_id"]),
        aws_account_id=cast(str, aws["account_id"]),
        aws_region=cast(str, aws["region"]),
        expected_sha256=cast(str, scenario["tf_binding_sha256"]),
    )
    if state_identity != scenario["tf_state_identity_sha256"]:
        raise AcceptanceCheckError("tf_binding_binding")
    other_id = "B" if scenario_id == "A" else "A"
    other_scenario = scenarios[other_id]
    assert isinstance(other_scenario, dict)
    other_inventory = _load_bound_scenario_inventory(manifest, other_id)
    _validate_scenario_inventory_isolation(
        inventory if scenario_id == "A" else other_inventory, other_inventory if scenario_id == "A" else inventory
    )
    scenario["inventory_path"] = resolved_path
    scenario["inventory_sha256"] = inventory_sha256
    scenario["inventory_phase"] = "resolved"
    manifest["updated_at"] = _utc_timestamp(now())
    _validate_control_manifest(manifest)
    _write_protected_document(
        path,
        manifest,
        create=False,
        exists_check="control_manifest_exists",
        write_check="control_manifest_file",
    )
    return manifest


def control_manifest_validate(
    path: Path,
    *,
    acceptance_run_id: str | None = None,
    candidate_sha: str | None = None,
    cleanup_only: bool = False,
    require_cleanup_cleared: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    manifest = _read_control_manifest(path)
    current = now()
    if (acceptance_run_id is not None and manifest["acceptance_run_id"] != acceptance_run_id) or (
        candidate_sha is not None and manifest["candidate_sha"] != candidate_sha
    ):
        raise AcceptanceCheckError("control_manifest_binding")
    expired = current >= _control_timestamp(manifest["teardown_deadline_utc"])
    if expired and not (cleanup_only and require_cleanup_cleared):
        raise AcceptanceCheckError("control_manifest_expired")
    if require_cleanup_cleared:
        if not cleanup_only or manifest["cleanup_required"] is not False:
            raise AcceptanceCheckError("control_manifest_cleanup")
        cleanup_states = manifest["cleanup_states"]
        assert isinstance(cleanup_states, dict)
        for surface, state in cleanup_states.items():
            if state == "confirmed":
                continue
            if surface == "teardown_deadline" and state == "failed" and manifest["deadline_failure_recorded"] is True:
                continue
            raise AcceptanceCheckError("control_manifest_cleanup")
        if expired and manifest["deadline_failure_recorded"] is not True:
            final_evidence = manifest["final_evidence"]
            if (
                not isinstance(final_evidence, Mapping)
                or final_evidence.get("phase") != "committed"
                or _control_timestamp(final_evidence.get("committed_at")) > _control_timestamp(manifest["teardown_deadline_utc"])
            ):
                raise AcceptanceCheckError("control_manifest_cleanup")
        _verify_final_cleanup_receipt(path, manifest)
    return manifest


def _require_current_approval(
    approvals: list[object],
    *,
    scenario_id: str,
    kind: str,
    plan_receipt_sha256: str,
    approval_sha256: str,
    current: datetime,
) -> Mapping[str, object]:
    matches = [
        approval
        for approval in approvals
        if isinstance(approval, Mapping)
        and approval.get("scenario_id") == scenario_id
        and approval.get("kind") == kind
        and approval.get("plan_receipt_sha256") == plan_receipt_sha256
        and approval.get("approval_sha256") == approval_sha256
    ]
    if len(matches) != 1:
        raise AcceptanceCheckError("control_manifest_update")
    approval = matches[0]
    if current >= _control_timestamp(approval["expires_at"]):
        raise AcceptanceCheckError("approval_expired")
    approval_path = approval["approval_path"]
    expected_sha256 = approval["approval_sha256"]
    assert isinstance(approval_path, str) and isinstance(expected_sha256, str)
    document = _read_protected_document(Path(approval_path), check="approval_file")
    observed_sha256 = _sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if observed_sha256 != expected_sha256:
        raise AcceptanceCheckError("approval_binding")
    return approval


def _validate_evidence_export_receipt(
    path: Path,
    *,
    manifest: Mapping[str, object],
    receipts_sha256: str,
    ledger_records_sha256: str,
) -> tuple[dict[str, object], str]:
    receipt = _read_protected_document(path, check="evidence_export_receipt")
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema",
        "acceptance_run_id",
        "destination_sha256",
        "receipts_sha256",
        "ledger_records_sha256",
        "artifact_count",
        "exported_at",
        "verified",
    }:
        raise AcceptanceCheckError("evidence_export_schema")
    evidence = manifest["evidence"]
    assert isinstance(evidence, Mapping)
    if (
        receipt["schema"] != "elspeth.aws-ecs-evidence-export.v1"
        or receipt["acceptance_run_id"] != manifest["acceptance_run_id"]
        or receipt["destination_sha256"] != evidence["destination_sha256"]
        or receipt["receipts_sha256"] != receipts_sha256
        or receipt["ledger_records_sha256"] != ledger_records_sha256
        or receipt["verified"] is not True
        or type(receipt["artifact_count"]) is not int
        or receipt["artifact_count"] < 1
    ):
        raise AcceptanceCheckError("evidence_export_binding")
    _control_timestamp(receipt["exported_at"])
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return receipt, _sha256(canonical)


def _reverify_bound_evidence_export_receipt(
    path: Path,
    *,
    manifest: Mapping[str, object],
    expected_sha256: str,
) -> None:
    document = _read_protected_document(path, check="evidence_export_receipt")
    receipts_sha256 = document.get("receipts_sha256")
    ledger_records_sha256 = document.get("ledger_records_sha256")
    if (
        type(receipts_sha256) is not str
        or _SHA256_PATTERN.fullmatch(receipts_sha256) is None
        or type(ledger_records_sha256) is not str
        or _SHA256_PATTERN.fullmatch(ledger_records_sha256) is None
    ):
        raise AcceptanceCheckError("evidence_export_schema")
    _receipt, observed_sha256 = _validate_evidence_export_receipt(
        path,
        manifest=manifest,
        receipts_sha256=receipts_sha256,
        ledger_records_sha256=ledger_records_sha256,
    )
    if observed_sha256 != expected_sha256:
        raise AcceptanceCheckError("evidence_export_binding")


def create_evidence_export_receipt(
    manifest_path: Path,
    *,
    ledger_path: Path,
    output_path: Path,
    artifact_count: int,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Record an evidence owner's completed and verified external export."""

    if type(artifact_count) is not int or not 1 <= artifact_count <= 100_000:
        raise AcceptanceCheckError("evidence_export_schema")
    manifest = _read_control_manifest(manifest_path)
    ledger = _read_gate_ledger(ledger_path)
    if ledger.get("candidate_sha") != manifest["candidate_sha"]:
        raise AcceptanceCheckError("evidence_export_binding")
    evidence_record_count, receipts_sha256 = _verify_stored_receipts(manifest_path, manifest)
    if artifact_count < max(1, evidence_record_count):
        raise AcceptanceCheckError("evidence_export_binding")
    receipt = {
        "schema": "elspeth.aws-ecs-evidence-export.v1",
        "acceptance_run_id": manifest["acceptance_run_id"],
        "destination_sha256": cast(Mapping[str, object], manifest["evidence"])["destination_sha256"],
        "receipts_sha256": receipts_sha256,
        "ledger_records_sha256": _gate_ledger_records_hash(ledger),
        "artifact_count": artifact_count,
        "exported_at": _utc_timestamp(now()),
        "verified": True,
    }
    _write_protected_document(
        output_path,
        receipt,
        create=True,
        exists_check="evidence_export_receipt",
        write_check="evidence_export_receipt",
        parent_check="evidence_export_receipt",
    )
    _validate_evidence_export_receipt(
        output_path,
        manifest=manifest,
        receipts_sha256=receipts_sha256,
        ledger_records_sha256=cast(str, receipt["ledger_records_sha256"]),
    )
    return receipt


@_serialized_control_manifest_write
def control_manifest_update(
    path: Path,
    *,
    cleanup_required: bool | None = None,
    ecr_baseline_tag: str | None = None,
    ecr_candidate_tag: str | None = None,
    ecr_registry: str | None = None,
    ecr_repository: str | None = None,
    ecr_baseline_digest: str | None = None,
    ecr_candidate_digest: str | None = None,
    acceptance_state_path: str | None = None,
    oidc_evidence_dir: str | None = None,
    evidence_export_receipt: str | None = None,
    final_evidence_export_receipt: str | None = None,
    terraform_plan_receipt: str | None = None,
    terraform_applied: str | None = None,
    terraform_noop_receipt: str | None = None,
    cleanup_checkpoint: str | None = None,
    verdict_failure: str | None = None,
    emergency_cleanup_deadline_utc: str | None = None,
    cleanup_escalation: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    manifest = _read_control_manifest(path)
    _require_mutable_control_manifest(manifest)
    current = now()
    tag_shape = (
        cleanup_required is True
        and evidence_export_receipt is None
        and final_evidence_export_receipt is None
        and all(value is not None for value in (ecr_baseline_tag, ecr_candidate_tag, ecr_registry, ecr_repository))
        and all(
            value is None
            for value in (
                ecr_baseline_digest,
                ecr_candidate_digest,
                acceptance_state_path,
                oidc_evidence_dir,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                cleanup_checkpoint,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    digest_shape = (
        ecr_baseline_digest is not None
        and ecr_candidate_digest is not None
        and evidence_export_receipt is None
        and final_evidence_export_receipt is None
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                acceptance_state_path,
                oidc_evidence_dir,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                cleanup_checkpoint,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    evidence_path_shape = (
        (acceptance_state_path is not None) != (oidc_evidence_dir is not None)
        and evidence_export_receipt is None
        and final_evidence_export_receipt is None
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                ecr_baseline_digest,
                ecr_candidate_digest,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                cleanup_checkpoint,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    terraform_shape = (
        sum(value is not None for value in (terraform_plan_receipt, terraform_applied, terraform_noop_receipt)) == 1
        and evidence_export_receipt is None
        and final_evidence_export_receipt is None
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                ecr_baseline_digest,
                ecr_candidate_digest,
                acceptance_state_path,
                oidc_evidence_dir,
                cleanup_checkpoint,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    checkpoint_shape = (
        cleanup_checkpoint is not None
        and evidence_export_receipt is None
        and final_evidence_export_receipt is None
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                ecr_baseline_digest,
                ecr_candidate_digest,
                acceptance_state_path,
                oidc_evidence_dir,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    verdict_shape = (
        verdict_failure == "teardown_deadline"
        and evidence_export_receipt is None
        and final_evidence_export_receipt is None
        and (emergency_cleanup_deadline_utc is None) == (cleanup_escalation is None)
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                ecr_baseline_digest,
                ecr_candidate_digest,
                acceptance_state_path,
                oidc_evidence_dir,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                cleanup_checkpoint,
            )
        )
    )
    export_shape = (
        evidence_export_receipt is not None
        and final_evidence_export_receipt is None
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                ecr_baseline_digest,
                ecr_candidate_digest,
                acceptance_state_path,
                oidc_evidence_dir,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                cleanup_checkpoint,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    final_export_shape = (
        final_evidence_export_receipt is not None
        and evidence_export_receipt is None
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                ecr_baseline_digest,
                ecr_candidate_digest,
                acceptance_state_path,
                oidc_evidence_dir,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                cleanup_checkpoint,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    if (
        sum(
            (
                tag_shape,
                digest_shape,
                evidence_path_shape,
                terraform_shape,
                checkpoint_shape,
                verdict_shape,
                export_shape,
                final_export_shape,
            )
        )
        != 1
    ):
        raise AcceptanceCheckError("control_manifest_update")
    cleanup_mutation = any(
        value is not None
        for value in (
            cleanup_checkpoint,
            verdict_failure,
            emergency_cleanup_deadline_utc,
            cleanup_escalation,
            evidence_export_receipt,
            final_evidence_export_receipt,
        )
    )
    expired = current >= _control_timestamp(manifest["teardown_deadline_utc"])
    acceptance_mutation = any(
        value is not None
        for value in (
            cleanup_required,
            ecr_baseline_tag,
            ecr_candidate_tag,
            ecr_registry,
            ecr_repository,
            ecr_baseline_digest,
            ecr_candidate_digest,
            acceptance_state_path,
            oidc_evidence_dir,
            terraform_plan_receipt,
            terraform_applied,
            terraform_noop_receipt,
        )
    )
    if expired and acceptance_mutation:
        raise AcceptanceCheckError("control_manifest_expired")
    if not cleanup_mutation and not acceptance_mutation:
        raise AcceptanceCheckError("control_manifest_update")
    if cleanup_required is not None:
        if cleanup_required is False:
            raise AcceptanceCheckError("control_manifest_cleanup")
        manifest["cleanup_required"] = True
    ecr = manifest["ecr"]
    assert isinstance(ecr, dict)
    for field, value in (
        ("baseline_tag", ecr_baseline_tag),
        ("candidate_tag", ecr_candidate_tag),
        ("registry", ecr_registry),
        ("repository", ecr_repository),
        ("baseline_digest", ecr_baseline_digest),
        ("candidate_digest", ecr_candidate_digest),
    ):
        if value is not None:
            if ecr[field] is not None and ecr[field] != value:
                raise AcceptanceCheckError("control_manifest_conflict")
            ecr[field] = value
    evidence = manifest["evidence"]
    assert isinstance(evidence, dict)
    if acceptance_state_path is not None:
        if evidence["acceptance_state_path"] is not None and evidence["acceptance_state_path"] != acceptance_state_path:
            raise AcceptanceCheckError("control_manifest_conflict")
        evidence["acceptance_state_path"] = acceptance_state_path
    if oidc_evidence_dir is not None:
        if evidence["oidc_evidence_dir"] is not None and evidence["oidc_evidence_dir"] != oidc_evidence_dir:
            raise AcceptanceCheckError("control_manifest_conflict")
        evidence["oidc_evidence_dir"] = oidc_evidence_dir
    if evidence_export_receipt is not None:
        export_path = Path(_control_path(evidence_export_receipt))
        existing_export_path = evidence["export_receipt_path"]
        existing_export_sha256 = evidence["export_receipt_sha256"]
        if existing_export_path is not None:
            if existing_export_path != evidence_export_receipt or type(existing_export_sha256) is not str:
                raise AcceptanceCheckError("control_manifest_conflict")
            _reverify_bound_evidence_export_receipt(
                export_path,
                manifest=manifest,
                expected_sha256=existing_export_sha256,
            )
        else:
            _receipt_count, receipts_sha256 = _verify_stored_receipts(path, manifest)
            ledger = _read_gate_ledger(Path(cast(str, manifest["gate_ledger_path"])))
            ledger_records_sha256 = _gate_ledger_records_hash(ledger)
            _receipt, export_sha256 = _validate_evidence_export_receipt(
                export_path,
                manifest=manifest,
                receipts_sha256=receipts_sha256,
                ledger_records_sha256=ledger_records_sha256,
            )
            evidence["export_receipt_path"] = evidence_export_receipt
            evidence["export_receipt_sha256"] = export_sha256
    if final_evidence_export_receipt is not None:
        initial_export_path = evidence["export_receipt_path"]
        initial_export_sha256 = evidence["export_receipt_sha256"]
        if type(initial_export_path) is not str or type(initial_export_sha256) is not str:
            raise AcceptanceCheckError("control_manifest_update")
        if final_evidence_export_receipt == initial_export_path:
            raise AcceptanceCheckError("control_manifest_conflict")
        _reverify_bound_evidence_export_receipt(
            Path(initial_export_path),
            manifest=manifest,
            expected_sha256=initial_export_sha256,
        )
        export_path = Path(_control_path(final_evidence_export_receipt))
        _receipt_count, receipts_sha256 = _verify_stored_receipts(path, manifest)
        ledger = _read_gate_ledger(Path(cast(str, manifest["gate_ledger_path"])))
        ledger_records_sha256 = _gate_ledger_records_hash(ledger)
        _receipt, export_sha256 = _validate_evidence_export_receipt(
            export_path,
            manifest=manifest,
            receipts_sha256=receipts_sha256,
            ledger_records_sha256=ledger_records_sha256,
        )
        if manifest["final_evidence"] is not None and (
            evidence["final_export_receipt_path"] != final_evidence_export_receipt
            or evidence["final_export_receipt_sha256"] != export_sha256
        ):
            raise AcceptanceCheckError("control_manifest_conflict")
        evidence["final_export_receipt_path"] = final_evidence_export_receipt
        evidence["final_export_receipt_sha256"] = export_sha256
    scenarios = manifest["scenarios"]
    assert isinstance(scenarios, dict)
    if terraform_plan_receipt is not None:
        parts = terraform_plan_receipt.split(":")
        if len(parts) != 4 or parts[0] not in {"A", "B"} or any(_SHA256_PATTERN.fullmatch(value) is None for value in parts[1:]):
            raise AcceptanceCheckError("control_manifest_update")
        scenario = scenarios[parts[0]]
        assert isinstance(scenario, dict)
        receipts = evidence["receipts"]
        approvals = evidence["approvals"]
        assert isinstance(receipts, list) and isinstance(approvals, list)
        subject_sha256 = _sha256(parts[1].encode("utf-8"))
        approval_matches = [
            approval
            for approval in approvals
            if isinstance(approval, dict)
            and approval.get("scenario_id") == parts[0]
            and approval.get("kind") == "terraform-plan"
            and approval.get("plan_receipt_sha256") == parts[2]
            and approval.get("approval_sha256") == parts[3]
        ]
        if (
            not any(
                isinstance(receipt, dict)
                and receipt.get("scenario_id") == parts[0]
                and receipt.get("kind") == "terraform-plan"
                and receipt.get("subject_sha256") == subject_sha256
                and receipt.get("receipt_sha256") == parts[2]
                for receipt in receipts
            )
            or len(approval_matches) != 1
        ):
            raise AcceptanceCheckError("control_manifest_update")
        _require_current_approval(
            cast(list[object], approvals),
            scenario_id=parts[0],
            kind="terraform-plan",
            plan_receipt_sha256=parts[2],
            approval_sha256=parts[3],
            current=current,
        )
        receipt_value = ":".join(parts[1:])
        if scenario["terraform_plan_receipt"] is not None and scenario["terraform_plan_receipt"] != receipt_value:
            raise AcceptanceCheckError("control_manifest_conflict")
        scenario["terraform_plan_receipt"] = receipt_value
    if terraform_applied is not None:
        parts = terraform_applied.split(":")
        if len(parts) != 4 or parts[0] not in {"A", "B"} or any(_SHA256_PATTERN.fullmatch(value) is None for value in parts[1:]):
            raise AcceptanceCheckError("control_manifest_update")
        scenario = scenarios[parts[0]]
        assert isinstance(scenario, dict)
        if scenario["terraform_plan_receipt"] != ":".join(parts[1:]):
            raise AcceptanceCheckError("control_manifest_update")
        approvals = evidence["approvals"]
        assert isinstance(approvals, list)
        _require_current_approval(
            cast(list[object], approvals),
            scenario_id=parts[0],
            kind="terraform-plan",
            plan_receipt_sha256=parts[2],
            approval_sha256=parts[3],
            current=current,
        )
        scenario["terraform_applied"] = True
    if terraform_noop_receipt is not None:
        parts = terraform_noop_receipt.split(":")
        if len(parts) != 3 or parts[0] not in {"A", "B"} or any(_SHA256_PATTERN.fullmatch(value) is None for value in parts[1:]):
            raise AcceptanceCheckError("control_manifest_update")
        scenario = scenarios[parts[0]]
        assert isinstance(scenario, dict)
        if scenario["terraform_applied"] is not True:
            raise AcceptanceCheckError("control_manifest_update")
        receipts = evidence["receipts"]
        assert isinstance(receipts, list)
        if not any(
            isinstance(receipt, dict)
            and receipt.get("scenario_id") == parts[0]
            and receipt.get("kind") == "terraform-noop"
            and receipt.get("subject_sha256") == _sha256(parts[1].encode("utf-8"))
            and receipt.get("receipt_sha256") == parts[2]
            for receipt in receipts
        ):
            raise AcceptanceCheckError("control_manifest_update")
        receipt_value = ":".join(parts[1:])
        if scenario["terraform_noop_receipt"] is not None and scenario["terraform_noop_receipt"] != receipt_value:
            raise AcceptanceCheckError("control_manifest_conflict")
        scenario["terraform_noop_receipt"] = receipt_value
    if cleanup_checkpoint is not None:
        try:
            surface, state_value = cleanup_checkpoint.split(":", 1)
        except ValueError:
            raise AcceptanceCheckError("control_manifest_update") from None
        cleanup_states = manifest["cleanup_states"]
        assert isinstance(cleanup_states, dict)
        if surface not in cleanup_states or state_value not in {"pending", "confirmed", "failed", "interrupted"}:
            raise AcceptanceCheckError("control_manifest_update")
        if cleanup_states[surface] == "confirmed" and state_value != "confirmed":
            raise AcceptanceCheckError("control_manifest_update")
        cleanup_states[surface] = state_value
    if verdict_failure is not None:
        failures = manifest["verdict_failures"]
        assert isinstance(failures, list)
        if verdict_failure not in failures:
            failures.append(verdict_failure)
        if verdict_failure == "teardown_deadline":
            manifest["deadline_failure_recorded"] = True
    if emergency_cleanup_deadline_utc is not None:
        existing_emergency = manifest["emergency_cleanup_deadline_utc"]
        if existing_emergency is not None and existing_emergency != emergency_cleanup_deadline_utc:
            raise AcceptanceCheckError("control_manifest_conflict")
        manifest["emergency_cleanup_deadline_utc"] = emergency_cleanup_deadline_utc
    if cleanup_escalation is not None:
        escalations = manifest["cleanup_escalations"]
        assert isinstance(escalations, list)
        if cleanup_escalation not in escalations:
            escalations.append(cleanup_escalation)
    manifest["updated_at"] = _utc_timestamp(current)
    _validate_control_manifest(manifest)
    _write_protected_document(
        path,
        manifest,
        create=False,
        exists_check="control_manifest_exists",
        write_check="control_manifest_file",
    )
    return manifest


def control_manifest_get(path: Path, field: str) -> str:
    value: object = _read_control_manifest(path)
    if not field or len(field) > 256:
        raise AcceptanceCheckError("control_manifest_field")
    for segment in field.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            raise AcceptanceCheckError("control_manifest_field")
        value = value[segment]
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, (str, int)):
        return str(value)
    raise AcceptanceCheckError("control_manifest_field")


def control_manifest_load_cleanup(
    path: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    manifest = _read_control_manifest(path)
    current = now()
    expired = current >= _control_timestamp(manifest["teardown_deadline_utc"])
    cleanup_committed = (
        manifest["cleanup_required"] is False
        and isinstance(manifest["final_evidence"], Mapping)
        and manifest["final_evidence"].get("phase") == "committed"
    )
    if (
        expired
        and not cleanup_committed
        and (manifest["deadline_failure_recorded"] is not True or manifest["emergency_cleanup_deadline_utc"] is None)
    ):
        emergency_deadline = manifest["emergency_cleanup_deadline_utc"]
        if emergency_deadline is None:
            emergency_deadline = _utc_timestamp(current + timedelta(seconds=_EMERGENCY_CLEANUP_SECONDS))
        assert isinstance(emergency_deadline, str)
        control_manifest_update(
            path,
            verdict_failure="teardown_deadline",
            emergency_cleanup_deadline_utc=emergency_deadline,
            cleanup_escalation="teardown_deadline",
            now=lambda: current,
        )
        manifest = _read_control_manifest(path)
    aws = manifest["aws"]
    scenarios = manifest["scenarios"]
    ecr = manifest["ecr"]
    evidence = manifest["evidence"]
    assert isinstance(aws, dict) and isinstance(scenarios, dict) and isinstance(ecr, dict) and isinstance(evidence, dict)
    scenario_a = scenarios["A"]
    scenario_b = scenarios["B"]
    assert isinstance(scenario_a, dict) and isinstance(scenario_b, dict)
    inventory_a = _load_bound_scenario_inventory(manifest, "A")
    inventory_b = _load_bound_scenario_inventory(manifest, "B")
    values_a = inventory_a["values"]
    values_b = inventory_b["values"]
    assert isinstance(values_a, dict) and isinstance(values_b, dict)
    assignments: dict[str, object] = {
        "ACCEPTANCE_REENTRY_FORBIDDEN": 1 if expired else 0,
        "ACCEPTANCE_RUN_ID": manifest["acceptance_run_id"],
        "ACCEPTANCE_TEARDOWN_DEADLINE_UTC": manifest["teardown_deadline_utc"],
        "AWS_ACCOUNT_ID": aws["account_id"],
        "AWS_REGION": aws["region"],
        "CANDIDATE_SHA": manifest["candidate_sha"],
        "CANDIDATE_TAG": ecr["candidate_tag"] or "",
        "CLEANUP_REQUIRED": 1 if manifest["cleanup_required"] else 0,
        "DEADLINE_EXPIRED": 1 if expired else 0,
        "ELSPETH_CLEANUP_MODE": 1,
        "ECR_REGISTRY": ecr["registry"] or "",
        "ECR_REPOSITORY": ecr["repository"] or "",
        "EMERGENCY_CLEANUP_DEADLINE_UTC": manifest["emergency_cleanup_deadline_utc"] or "",
        "GATE_LEDGER": manifest["gate_ledger_path"],
        "ROLLBACK_BASELINE_TAG": ecr["baseline_tag"] or "",
        "ROLLBACK_BASELINE_DIGEST": ecr["baseline_digest"] or "",
        "IMAGE_DIGEST": ecr["candidate_digest"] or "",
        "ROLLBACK_BASELINE_IMAGE": (
            f"{ecr['registry']}/{ecr['repository']}@{ecr['baseline_digest']}"
            if ecr["registry"] and ecr["repository"] and ecr["baseline_digest"]
            else ""
        ),
        "CANDIDATE_IMAGE": (
            f"{ecr['registry']}/{ecr['repository']}@{ecr['candidate_digest']}"
            if ecr["registry"] and ecr["repository"] and ecr["candidate_digest"]
            else ""
        ),
        "ACCEPTANCE_STATE": evidence["acceptance_state_path"] or "",
        "OIDC_EVIDENCE_DIR": evidence["oidc_evidence_dir"] or "",
        "EVIDENCE_DESTINATION_SHA256": evidence["destination_sha256"],
        "EVIDENCE_EXPORT_RECEIPT": evidence["export_receipt_path"] or "",
        "FINAL_EVIDENCE_EXPORT_RECEIPT": evidence["final_export_receipt_path"] or "",
        "SCENARIO_A_INVENTORY": scenario_a["inventory_path"],
        "SCENARIO_A_TF_DIR": values_a["SCENARIO_TF_DIR"],
        "SCENARIO_A_TF_VARS": values_a["SCENARIO_TF_VARS"],
        "SCENARIO_A_TF_BINDING_SHA": scenario_a["tf_binding_sha256"],
        "SCENARIO_A_TF_BINDING_FILE": scenario_a["tf_binding_path"],
        "SCENARIO_B_INVENTORY": scenario_b["inventory_path"],
        "SCENARIO_B_TF_DIR": values_b["SCENARIO_TF_DIR"],
        "SCENARIO_B_TF_VARS": values_b["SCENARIO_TF_VARS"],
        "SCENARIO_B_TF_BINDING_SHA": scenario_b["tf_binding_sha256"],
        "SCENARIO_B_TF_BINDING_FILE": scenario_b["tf_binding_path"],
    }
    return "\n".join(f"{name}={shlex.quote(str(value))}" for name, value in assignments.items()) + "\n"


def scenario_load(
    path: Path,
    *,
    scenario_id: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Render one bound scenario inventory as a closed shell assignment set."""

    if scenario_id not in {"A", "B"}:
        raise AcceptanceCheckError("scenario_inventory_binding")
    manifest = control_manifest_validate(path, now=now)
    inventory = _load_bound_scenario_inventory(manifest, scenario_id, require_resolved=True)
    values = inventory["values"]
    assert isinstance(values, dict)
    assignments = {
        "ACTIVE_SCENARIO_ID": scenario_id,
        "ACCEPTANCE_RUN_ID": manifest["acceptance_run_id"],
        **{name: values[name] for name in SCENARIO_ASSIGNMENT_NAMES[2:]},
    }
    return "\n".join(f"{name}={shlex.quote(str(assignments[name]))}" for name in SCENARIO_ASSIGNMENT_NAMES) + "\n"


def _plaintext_task_definition_secret(name: str) -> bool:
    return name in _TASK_DEFINITION_PLAINTEXT_SECRET_ENV or is_secret_field(name)


def _secrets_manager_inventory_binding(
    value_from: object,
    *,
    partition: str,
    region: str,
    account_id: str,
) -> tuple[str, str | None, str | None, str | None] | None:
    if type(value_from) is not str or len(value_from) > 2048:
        return None
    match = _SECRET_VALUE_FROM_PATTERN.fullmatch(value_from)
    if match is None or match.group(1) != partition or match.group(2) != region or match.group(3) != account_id:
        return None
    suffixed_name = match.group(4)
    name_match = _SECRET_ARN_SUFFIX_PATTERN.fullmatch(suffixed_name)
    if name_match is None:
        return None
    return name_match.group(1), match.group(5), match.group(6), match.group(7)


def validate_task_definition_policy_binding(
    payload: object,
    *,
    manifest_path: Path,
    scenario_id: str,
    container_name: str,
    expected_user: str | None = None,
    expected_image_role: str = "candidate",
) -> str:
    """Bind a returned ECS task definition's policy environment to protected inventory."""

    if (
        scenario_id not in {"A", "B"}
        or re.fullmatch(r"[A-Za-z0-9_-]{1,255}", container_name) is None
        or expected_image_role not in {"candidate", "rollback-baseline"}
    ):
        raise AcceptanceCheckError("task_definition_policy_binding")
    manifest = _read_control_manifest(manifest_path)
    inventory = _load_bound_scenario_inventory(manifest, scenario_id, require_resolved=True)
    values = inventory["values"]
    orphan = inventory["orphan_sweep"]
    if not isinstance(values, dict) or not isinstance(orphan, dict):
        raise AcceptanceCheckError("task_definition_policy_binding")
    task = payload.get("taskDefinition") if isinstance(payload, Mapping) else None
    if not isinstance(task, Mapping) or task.get("status") != "ACTIVE":
        raise AcceptanceCheckError("task_definition_policy_binding")
    task_definition_arn = task.get("taskDefinitionArn")
    task_definition_match = (
        re.fullmatch(
            r"arn:(aws(?:-us-gov|-cn)?):ecs:[a-z0-9-]+:[0-9]{12}:task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*",
            task_definition_arn,
        )
        if type(task_definition_arn) is str
        else None
    )
    if type(task_definition_arn) is not str or task_definition_match is None:
        raise AcceptanceCheckError("task_definition_policy_binding")
    containers = task.get("containerDefinitions")
    if not isinstance(containers, list) or len(containers) > 100:
        raise AcceptanceCheckError("task_definition_policy_binding")
    matches = [container for container in containers if isinstance(container, Mapping) and container.get("name") == container_name]
    if len(matches) != 1:
        raise AcceptanceCheckError("task_definition_policy_binding")
    container = matches[0]
    if container.get("essential") is not True:
        raise AcceptanceCheckError("task_definition_policy_binding")
    ecr = manifest["ecr"]
    if not isinstance(ecr, Mapping):
        raise AcceptanceCheckError("task_definition_policy_binding")
    registry = ecr["registry"]
    repository = ecr["repository"]
    digest = ecr["candidate_digest"] if expected_image_role == "candidate" else ecr["baseline_digest"]
    if type(registry) is not str or type(repository) is not str or type(digest) is not str:
        raise AcceptanceCheckError("task_definition_policy_binding")
    if container.get("image") != f"{registry}/{repository}@{digest}":
        raise AcceptanceCheckError("task_definition_policy_binding")
    aws = manifest["aws"]
    role_names = orphan.get("iam_role_names")
    if not isinstance(aws, Mapping) or not isinstance(role_names, list):
        raise AcceptanceCheckError("task_definition_policy_binding")
    account_id = aws.get("account_id")
    namespace = scenario_resource_namespace(cast(str, manifest["acceptance_run_id"]), scenario_id)
    expected_roles = {
        "taskRoleArn": f"{namespace}-task-role",
        "executionRoleArn": f"{namespace}-execution-role",
    }
    role_arns = tuple(task.get(field) for field in expected_roles)
    if role_arns[0] == role_arns[1]:
        raise AcceptanceCheckError("task_definition_policy_binding")
    for field, expected_name in expected_roles.items():
        role_arn = task.get(field)
        if type(role_arn) is not str:
            raise AcceptanceCheckError("task_definition_policy_binding")
        match = re.fullmatch(
            r"arn:aws(?:-us-gov|-cn)?:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_-]{1,64})",
            role_arn,
        )
        if match is None or match.group(1) != account_id or match.group(2) != expected_name or expected_name not in role_names:
            raise AcceptanceCheckError("task_definition_policy_binding")
    if expected_user is not None and (
        expected_user != "1000:1000"
        or container.get("user") != expected_user
        or container.get("entryPoint") != ["python", "-m", "elspeth.web.aws_ecs_acceptance"]
    ):
        raise AcceptanceCheckError("task_definition_policy_binding")
    environment = container.get("environment")
    secrets = container.get("secrets", [])
    if not isinstance(environment, list) or not isinstance(secrets, list) or len(environment) > 1_000 or len(secrets) > 1_000:
        raise AcceptanceCheckError("task_definition_policy_binding")

    observed: dict[str, str] = {}
    for entry in environment:
        if not isinstance(entry, Mapping) or set(entry) != {"name", "value"}:
            raise AcceptanceCheckError("task_definition_policy_binding")
        name = entry["name"]
        value = entry["value"]
        if type(name) is not str or type(value) is not str or name in observed:
            raise AcceptanceCheckError("task_definition_policy_binding")
        observed[name] = value
    if any(name in _TASK_DEFINITION_AWS_OVERRIDE_ENV or _plaintext_task_definition_secret(name) for name in observed):
        raise AcceptanceCheckError("task_definition_policy_binding")
    if any(observed.get(name) != values.get(name) for name in _TASK_DEFINITION_COMPOSER_MODEL_ENV):
        raise AcceptanceCheckError("task_definition_policy_binding")
    composer_providers = {
        infer_provider_from_model_name(observed[name]) or infer_provider_from_unprefixed_model_name(observed[name])
        for name in _TASK_DEFINITION_COMPOSER_MODEL_ENV
    }
    if None in composer_providers:
        raise AcceptanceCheckError("task_definition_policy_binding")
    requires_openrouter = "openrouter" in composer_providers
    secret_names: set[str] = set()
    approved_secret_ids = set(cast(list[str], orphan["secret_ids"]))
    required_secret_bindings = {
        name: (f"{namespace}-{secret_suffix}", json_key, "", "")
        for name, secret_suffix, json_key in _TASK_DEFINITION_REQUIRED_SECRET_BINDINGS
    }
    if requires_openrouter:
        name, secret_suffix, json_key = _TASK_DEFINITION_OPENROUTER_SECRET_BINDING
        required_secret_bindings[name] = (f"{namespace}-{secret_suffix}", json_key, "", "")
    aws_region = aws.get("region")
    assert task_definition_match is not None
    partition = task_definition_match.group(1)
    for entry in secrets:
        if not isinstance(entry, Mapping) or set(entry) != {"name", "valueFrom"}:
            raise AcceptanceCheckError("task_definition_policy_binding")
        name = entry["name"]
        inventory_binding = _secrets_manager_inventory_binding(
            entry["valueFrom"],
            partition=partition,
            region=cast(str, aws_region),
            account_id=cast(str, account_id),
        )
        if (
            type(name) is not str
            or name in secret_names
            or name in observed
            or name in _TASK_DEFINITION_AWS_OVERRIDE_ENV
            or inventory_binding is None
            or inventory_binding[0] not in approved_secret_ids
            or (name in required_secret_bindings and inventory_binding != required_secret_bindings[name])
        ):
            raise AcceptanceCheckError("task_definition_policy_binding")
        secret_names.add(name)
    if not required_secret_bindings.keys() <= secret_names:
        raise AcceptanceCheckError("task_definition_policy_binding")
    if not requires_openrouter and "OPENROUTER_API_KEY" in secret_names:
        raise AcceptanceCheckError("task_definition_policy_binding")

    protected_names = (
        *_TASK_DEFINITION_COMPOSER_MODEL_ENV,
        *PLUGIN_POLICY_ASSIGNMENT_NAMES,
        "ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256",
        "ELSPETH_BEDROCK_LIVE_TEST_MODEL",
        "AWS_REGION",
    )
    acceptance_run_id = cast(str, manifest["acceptance_run_id"])
    expected_runtime = {
        "ELSPETH_WEB__DATA_DIR": cast(str, values["ELSPETH_WEB__DATA_DIR"]),
        "ELSPETH_WEB__PAYLOAD_STORE_PATH": cast(str, values["ELSPETH_WEB__PAYLOAD_STORE_PATH"]),
        "ELSPETH_ACCEPTANCE_RUN_ID": acceptance_run_id,
        "ELSPETH_ACCEPTANCE_CANDIDATE_SHA": cast(str, manifest["candidate_sha"]),
        "ELSPETH_ACCEPTANCE_SCENARIO_ID": scenario_id,
        "ELSPETH_ACCEPTANCE_S3_BUCKET": cast(str, values["ELSPETH_TEST_S3_BUCKET"]),
        "ELSPETH_ACCEPTANCE_S3_PREFIX": f"{scenario_resource_namespace(acceptance_run_id, scenario_id)}/{acceptance_run_id}",
    }
    if secret_names.intersection((*protected_names, *expected_runtime)):
        raise AcceptanceCheckError("task_definition_policy_binding")
    if any(observed.get(name) != values.get(name) for name in protected_names):
        raise AcceptanceCheckError("task_definition_policy_binding")
    if any(observed.get(name) != value for name, value in expected_runtime.items()):
        raise AcceptanceCheckError("task_definition_policy_binding")
    data_dir_value = observed.get("ELSPETH_WEB__DATA_DIR")
    payload_root_value = observed.get("ELSPETH_WEB__PAYLOAD_STORE_PATH")
    try:
        data_dir = PurePosixPath(cast(str, data_dir_value))
        payload_root = PurePosixPath(cast(str, payload_root_value))
    except (TypeError, ValueError):
        raise AcceptanceCheckError("task_definition_policy_binding") from None
    if (
        type(data_dir_value) is not str
        or type(payload_root_value) is not str
        or not data_dir.is_absolute()
        or not payload_root.is_absolute()
        or data_dir == PurePosixPath("/")
        or payload_root == data_dir
        or data_dir not in payload_root.parents
        or payload_root == data_dir / "blobs"
    ):
        raise AcceptanceCheckError("task_definition_policy_binding")

    file_system_ids = orphan.get("efs_file_system_ids")
    access_point_ids = orphan.get("efs_access_point_ids")
    if (
        not isinstance(file_system_ids, list)
        or not isinstance(access_point_ids, list)
        or len(file_system_ids) != 1
        or len(access_point_ids) != 1
        or type(file_system_ids[0]) is not str
        or type(access_point_ids[0]) is not str
    ):
        raise AcceptanceCheckError("task_definition_policy_binding")
    volumes = task.get("volumes")
    mount_points = container.get("mountPoints")
    if not isinstance(volumes, list) or not isinstance(mount_points, list):
        raise AcceptanceCheckError("task_definition_policy_binding")
    volume_names: set[str] = set()
    matching_volumes: list[str] = []
    efs_volume_names: set[str] = set()
    for volume in volumes:
        if not isinstance(volume, Mapping) or type(volume.get("name")) is not str:
            raise AcceptanceCheckError("task_definition_policy_binding")
        volume_name = cast(str, volume["name"])
        if volume_name in volume_names:
            raise AcceptanceCheckError("task_definition_policy_binding")
        volume_names.add(volume_name)
        efs = volume.get("efsVolumeConfiguration")
        if not isinstance(efs, Mapping):
            continue
        efs_volume_names.add(volume_name)
        authorization = efs.get("authorizationConfig")
        if (
            efs.get("fileSystemId") == file_system_ids[0]
            and efs.get("transitEncryption") == "ENABLED"
            and (efs.get("rootDirectory") is None or efs.get("rootDirectory") == "/")
            and isinstance(authorization, Mapping)
            and authorization.get("accessPointId") == access_point_ids[0]
            and authorization.get("iam") == "ENABLED"
        ):
            matching_volumes.append(volume_name)
    if len(matching_volumes) != 1 or efs_volume_names != {matching_volumes[0]}:
        raise AcceptanceCheckError("task_definition_policy_binding")
    bound_mounts = [
        mount
        for mount in mount_points
        if isinstance(mount, Mapping) and (mount.get("sourceVolume") == matching_volumes[0] or mount.get("containerPath") == data_dir_value)
    ]
    if len(bound_mounts) != 1 or not (
        bound_mounts[0].get("sourceVolume") == matching_volumes[0]
        and bound_mounts[0].get("containerPath") == data_dir_value
        and bound_mounts[0].get("readOnly") is False
    ):
        raise AcceptanceCheckError("task_definition_policy_binding")
    try:
        observed_binding = plugin_policy_binding_sha256(observed)
    except AcceptanceCheckError:
        raise AcceptanceCheckError("task_definition_policy_binding") from None
    if observed_binding != observed["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"]:
        raise AcceptanceCheckError("task_definition_policy_binding")
    return task_definition_arn


@dataclass(frozen=True)
class OrphanSweepClients:
    """Closed AWS client bundle used by the cleanup-only orphan sweep."""

    tagging: Any
    ecs: Any
    elbv2: Any
    rds: Any
    efs: Any
    secretsmanager: Any
    iam: Any
    logs: Any
    cloudwatch: Any
    xray: Any
    events: Any
    bedrock: Any
    cognito: Any
    ecr: Any

    def __iter__(self) -> Iterator[Any]:
        return iter(
            (
                self.tagging,
                self.ecs,
                self.elbv2,
                self.rds,
                self.efs,
                self.secretsmanager,
                self.iam,
                self.logs,
                self.cloudwatch,
                self.xray,
                self.events,
                self.bedrock,
                self.cognito,
                self.ecr,
            )
        )


def _build_orphan_sweep_clients(region: str) -> OrphanSweepClients:
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise AcceptanceCheckError("orphan_sweep_runtime") from None
    config = Config(
        connect_timeout=10,
        read_timeout=30,
        retries={"mode": "standard", "total_max_attempts": 3},
    )

    created: list[Any] = []

    def client(service: str) -> Any:
        result = boto3.client(service, region_name=region, config=config)
        created.append(result)
        return result

    try:
        return OrphanSweepClients(
            tagging=client("resourcegroupstaggingapi"),
            ecs=client("ecs"),
            elbv2=client("elbv2"),
            rds=client("rds"),
            efs=client("efs"),
            secretsmanager=client("secretsmanager"),
            iam=client("iam"),
            logs=client("logs"),
            cloudwatch=client("cloudwatch"),
            xray=client("xray"),
            events=client("events"),
            bedrock=client("bedrock"),
            cognito=client("cognito-idp"),
            ecr=client("ecr"),
        )
    except Exception:
        for created_client in reversed(created):
            close = getattr(created_client, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
        raise AcceptanceCheckError("orphan_sweep_runtime") from None


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return None
    error = response.get("Error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


_ORPHAN_NOT_FOUND_CODES = frozenset(
    {
        "AccessPointNotFound",
        "ClusterNotFoundException",
        "DBClusterNotFoundFault",
        "DBInstanceNotFound",
        "FileSystemNotFound",
        "ImageNotFoundException",
        "LoadBalancerNotFound",
        "ListenerNotFound",
        "NoSuchEntity",
        "RepositoryNotFoundException",
        "ResourceNotFoundException",
        "RuleNotFound",
        "SecretNotFoundException",
        "ServiceNotFoundException",
        "TargetGroupNotFound",
        "UserPoolNotFoundException",
    }
)


def _orphan_call(client: Any, method: str, **kwargs: object) -> Mapping[str, object] | None:
    try:
        response = getattr(client, method)(**kwargs)
    except Exception as exc:
        if _aws_error_code(exc) in _ORPHAN_NOT_FOUND_CODES:
            return None
        raise AcceptanceCheckError("orphan_sweep_api") from None
    if not isinstance(response, Mapping):
        raise AcceptanceCheckError("orphan_sweep_api")
    return response


def _orphan_response_items(response: Mapping[str, object] | None, field: str) -> list[object]:
    if response is None:
        return []
    items = response.get(field)
    if not isinstance(items, list) or len(items) > _ORPHAN_MAX_ITEMS:
        raise AcceptanceCheckError("orphan_sweep_api")
    return items


def _orphan_paged_items(
    client: Any,
    method: str,
    *,
    item_field: str,
    request_token: str,
    response_token: str,
    kwargs: Mapping[str, object],
    allow_missing_items: bool = False,
) -> list[object]:
    token: str | None = None
    seen_tokens: set[str] = set()
    collected: list[object] = []
    for _page in range(_ORPHAN_MAX_PAGES):
        request = dict(kwargs)
        if token is not None:
            request[request_token] = token
        response = _orphan_call(client, method, **request)
        if response is None:
            return collected
        page_items = [] if allow_missing_items and item_field not in response else _orphan_response_items(response, item_field)
        collected.extend(page_items)
        if len(collected) > _ORPHAN_MAX_ITEMS:
            raise AcceptanceCheckError("orphan_sweep_api")
        continuation = response.get(response_token)
        if continuation in {None, ""}:
            return collected
        if type(continuation) is not str or continuation in seen_tokens:
            raise AcceptanceCheckError("orphan_sweep_api")
        seen_tokens.add(continuation)
        token = continuation
    raise AcceptanceCheckError("orphan_sweep_api")


def _orphan_inventory_values(inventory: Mapping[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    values = inventory["values"]
    orphan = inventory["orphan_sweep"]
    assert isinstance(values, dict) and isinstance(orphan, dict)
    return values, orphan


def _task_definition_owned(
    client: Any,
    task_definition_arn: str,
    *,
    family: str,
    acceptance_run_id: str,
) -> None:
    if _task_definition_family(task_definition_arn) != family:
        raise AcceptanceCheckError("orphan_sweep_binding")
    described = _orphan_call(
        client,
        "describe_task_definition",
        taskDefinition=task_definition_arn,
        include=["TAGS"],
    )
    tags_payload = described.get("tags") if described is not None else None
    if not isinstance(tags_payload, list) or not any(
        isinstance(tag, Mapping) and tag.get("key") == "ACCEPTANCE_RUN_ID" and tag.get("value") == acceptance_run_id for tag in tags_payload
    ):
        raise AcceptanceCheckError("orphan_sweep_binding")


def _transaction_search_projection(
    *,
    destination: object,
    indexing_rules: list[object],
    spans_log_group_present: bool,
) -> dict[str, object]:
    if destination not in {None, "XRay", "CloudWatchLogs"} or type(spans_log_group_present) is not bool:
        raise AcceptanceCheckError("orphan_sweep_api")
    projected_rules: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for item in indexing_rules:
        if not isinstance(item, Mapping) or not {"Name", "Rule"} <= set(item) or not set(item) <= {"Name", "Rule", "ModifiedAt"}:
            raise AcceptanceCheckError("orphan_sweep_api")
        name = item["Name"]
        rule = item["Rule"]
        if (
            type(name) is not str
            or not name
            or len(name) > 128
            or name in seen_names
            or not isinstance(rule, Mapping)
            or set(rule) != {"Probabilistic"}
        ):
            raise AcceptanceCheckError("orphan_sweep_api")
        probabilistic = rule["Probabilistic"]
        if (
            not isinstance(probabilistic, Mapping)
            or "DesiredSamplingPercentage" not in probabilistic
            or not set(probabilistic) <= {"DesiredSamplingPercentage", "ActualSamplingPercentage"}
        ):
            raise AcceptanceCheckError("orphan_sweep_api")
        desired = probabilistic["DesiredSamplingPercentage"]
        actual = probabilistic.get("ActualSamplingPercentage")
        if type(desired) not in {int, float} or not math.isfinite(float(desired)) or not 0 <= float(desired) <= 100:
            raise AcceptanceCheckError("orphan_sweep_api")
        if actual is not None and (type(actual) not in {int, float} or not math.isfinite(float(actual)) or not 0 <= float(actual) <= 100):
            raise AcceptanceCheckError("orphan_sweep_api")
        seen_names.add(name)
        projected_rules.append({"name": name, "desired_sampling_percentage": float(desired)})
    return {
        "destination": destination,
        "indexing_rules": sorted(projected_rules, key=lambda item: cast(str, item["name"])),
        "spans_log_group_present": spans_log_group_present,
    }


def orphan_sweep(
    manifest_path: Path,
    *,
    acceptance_run_id: str,
    clients: OrphanSweepClients | None = None,
    environ: Mapping[str, str] = os.environ,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Delete the two run-scoped ECR tags and prove all owned AWS surfaces empty."""

    try:
        uuid.UUID(acceptance_run_id)
    except ValueError:
        raise AcceptanceCheckError("orphan_sweep_binding") from None
    if any(name == "AWS_ENDPOINT_URL" or name.startswith("AWS_ENDPOINT_URL_") for name in environ):
        raise AcceptanceCheckError("orphan_sweep_environment")
    manifest = _read_control_manifest(manifest_path)
    if manifest["acceptance_run_id"] != acceptance_run_id or manifest["cleanup_required"] is not True:
        raise AcceptanceCheckError("orphan_sweep_binding")
    inventories = (
        _load_bound_scenario_inventory(manifest, "A"),
        _load_bound_scenario_inventory(manifest, "B"),
    )
    evidence = manifest["evidence"]
    assert isinstance(evidence, Mapping)
    if evidence["retained_evidence_path"] is None:
        retained_scenarios: dict[str, object] = {
            scenario_id: {
                "cloudwatch_retained_metrics": [],
                "xray_retained_trace_ids": [],
                "expected_retained_metric_series": 0,
                "expected_retained_trace_ids": 0,
            }
            for scenario_id in ("A", "B")
        }
    else:
        retained_evidence = _load_retained_evidence(manifest)
        loaded_scenarios = retained_evidence["scenarios"]
        assert isinstance(loaded_scenarios, dict)
        retained_scenarios = loaded_scenarios
    aws = manifest["aws"]
    ecr_manifest = manifest["ecr"]
    assert isinstance(aws, dict) and isinstance(ecr_manifest, dict)
    if clients is None:
        clients = _build_orphan_sweep_clients(str(aws["region"]))
    counts = {surface: {"queried": 0, "unapproved_survivors": 0} for surface in _ORPHAN_SURFACES}
    delete_in_progress_receipts: list[dict[str, object]] = []
    retained_metrics = 0
    retained_traces = 0
    observed_retained_metrics = 0
    observed_retained_traces = 0
    with contextlib.ExitStack() as stack:
        for client in clients:
            close = getattr(client, "close", None)
            if callable(close):
                stack.callback(close)
        try:
            repository = ecr_manifest["repository"]
            tags = (ecr_manifest["baseline_tag"], ecr_manifest["candidate_tag"])
            if type(repository) is not str or not repository or any(type(tag) is not str or not tag for tag in tags):
                raise AcceptanceCheckError("orphan_sweep_binding")
            for tag in tags:
                counts["ecr"]["queried"] += 1
                before = _orphan_call(
                    clients.ecr,
                    "describe_images",
                    registryId=aws["account_id"],
                    repositoryName=repository,
                    imageIds=[{"imageTag": tag}],
                )
                image_details = _orphan_response_items(before, "imageDetails")
                if image_details:
                    deleted = _orphan_call(
                        clients.ecr,
                        "batch_delete_image",
                        registryId=aws["account_id"],
                        repositoryName=repository,
                        imageIds=[{"imageTag": tag}],
                    )
                    if deleted is None or _orphan_response_items(deleted, "failures"):
                        raise AcceptanceCheckError("orphan_sweep_api")
                after = _orphan_call(
                    clients.ecr,
                    "describe_images",
                    registryId=aws["account_id"],
                    repositoryName=repository,
                    imageIds=[{"imageTag": tag}],
                )
                if _orphan_response_items(after, "imageDetails"):
                    counts["ecr"]["unapproved_survivors"] += 1

            tagged = _orphan_paged_items(
                clients.tagging,
                "get_resources",
                item_field="ResourceTagMappingList",
                request_token="PaginationToken",
                response_token="PaginationToken",
                kwargs={
                    "TagFilters": [{"Key": "ACCEPTANCE_RUN_ID", "Values": [acceptance_run_id]}],
                    "ResourcesPerPage": 100,
                    "IncludeComplianceDetails": False,
                },
            )
            counts["tagging"]["queried"] = 1
            allowed_deleting_task_definitions: set[str] = set()

            for scenario_id, inventory in zip(("A", "B"), inventories, strict=True):
                values, orphan = _orphan_inventory_values(inventory)
                scenario_retained = retained_scenarios[scenario_id]
                assert isinstance(scenario_retained, dict)
                orphan = {**orphan, **scenario_retained}
                retained_metric_count = orphan["expected_retained_metric_series"]
                retained_trace_count = orphan["expected_retained_trace_ids"]
                assert isinstance(retained_metric_count, int) and isinstance(retained_trace_count, int)
                retained_metrics += retained_metric_count
                retained_traces += retained_trace_count
                cluster = values["ECS_CLUSTER"]
                service = values["ECS_SERVICE"]
                services = _orphan_call(
                    clients.ecs,
                    "describe_services",
                    cluster=cluster,
                    services=[service],
                    include=["TAGS"],
                )
                counts["ecs"]["queried"] += 1
                counts["ecs"]["unapproved_survivors"] += len(_orphan_response_items(services, "services"))
                for desired_status in ("RUNNING", "PENDING"):
                    task_arns = _orphan_paged_items(
                        clients.ecs,
                        "list_tasks",
                        item_field="taskArns",
                        request_token="nextToken",
                        response_token="nextToken",
                        kwargs={
                            "cluster": cluster,
                            "serviceName": service,
                            "desiredStatus": desired_status,
                            "maxResults": 100,
                        },
                    )
                    counts["ecs"]["queried"] += 1
                    counts["ecs"]["unapproved_survivors"] += len(task_arns)
                families = orphan["ecs_task_definition_families"]
                assert isinstance(families, list)
                for family in families:
                    for desired_status in ("RUNNING", "PENDING"):
                        family_tasks = _orphan_paged_items(
                            clients.ecs,
                            "list_tasks",
                            item_field="taskArns",
                            request_token="nextToken",
                            response_token="nextToken",
                            kwargs={
                                "cluster": cluster,
                                "family": family,
                                "desiredStatus": desired_status,
                                "maxResults": 100,
                            },
                        )
                        counts["ecs"]["queried"] += 1
                        counts["ecs"]["unapproved_survivors"] += len(family_tasks)
                    by_status: dict[str, list[object]] = {}
                    for status_value in ("ACTIVE", "INACTIVE", "DELETE_IN_PROGRESS"):
                        by_status[status_value] = _orphan_paged_items(
                            clients.ecs,
                            "list_task_definitions",
                            item_field="taskDefinitionArns",
                            request_token="nextToken",
                            response_token="nextToken",
                            kwargs={
                                "familyPrefix": family,
                                "status": status_value,
                                "sort": "ASC",
                                "maxResults": 100,
                            },
                        )
                        counts["ecs"]["queried"] += 1
                        for task_definition_arn in by_status[status_value]:
                            if _task_definition_family(task_definition_arn) != family:
                                raise AcceptanceCheckError("orphan_sweep_binding")
                    verified_task_definitions: set[str] = set()
                    inactive = list(by_status["INACTIVE"])
                    for task_definition_arn in by_status["ACTIVE"]:
                        assert isinstance(task_definition_arn, str)
                        _task_definition_owned(
                            clients.ecs,
                            task_definition_arn,
                            family=family,
                            acceptance_run_id=acceptance_run_id,
                        )
                        verified_task_definitions.add(task_definition_arn)
                        deregistered = _orphan_call(
                            clients.ecs,
                            "deregister_task_definition",
                            taskDefinition=task_definition_arn,
                        )
                        if deregistered is None:
                            raise AcceptanceCheckError("orphan_sweep_api")
                        inactive.append(task_definition_arn)
                    for task_definition_arn in by_status["DELETE_IN_PROGRESS"]:
                        assert isinstance(task_definition_arn, str)
                        _task_definition_owned(
                            clients.ecs,
                            task_definition_arn,
                            family=family,
                            acceptance_run_id=acceptance_run_id,
                        )
                        verified_task_definitions.add(task_definition_arn)
                        allowed_deleting_task_definitions.add(task_definition_arn)
                    for offset in range(0, len(inactive), 10):
                        batch = inactive[offset : offset + 10]
                        for task_definition_arn in batch:
                            assert isinstance(task_definition_arn, str)
                            if task_definition_arn not in verified_task_definitions:
                                _task_definition_owned(
                                    clients.ecs,
                                    task_definition_arn,
                                    family=family,
                                    acceptance_run_id=acceptance_run_id,
                                )
                                verified_task_definitions.add(task_definition_arn)
                        deleted = _orphan_call(clients.ecs, "delete_task_definitions", taskDefinitions=batch)
                        if deleted is None or _orphan_response_items(deleted, "failures"):
                            raise AcceptanceCheckError("orphan_sweep_api")
                        allowed_deleting_task_definitions.update(cast(list[str], batch))
                    active_after = _orphan_paged_items(
                        clients.ecs,
                        "list_task_definitions",
                        item_field="taskDefinitionArns",
                        request_token="nextToken",
                        response_token="nextToken",
                        kwargs={"familyPrefix": family, "status": "ACTIVE", "sort": "ASC", "maxResults": 100},
                    )
                    inactive_after = _orphan_paged_items(
                        clients.ecs,
                        "list_task_definitions",
                        item_field="taskDefinitionArns",
                        request_token="nextToken",
                        response_token="nextToken",
                        kwargs={"familyPrefix": family, "status": "INACTIVE", "sort": "ASC", "maxResults": 100},
                    )
                    deleting_after = _orphan_paged_items(
                        clients.ecs,
                        "list_task_definitions",
                        item_field="taskDefinitionArns",
                        request_token="nextToken",
                        response_token="nextToken",
                        kwargs={
                            "familyPrefix": family,
                            "status": "DELETE_IN_PROGRESS",
                            "sort": "ASC",
                            "maxResults": 100,
                        },
                    )
                    counts["ecs"]["queried"] += 3
                    for task_definition_arn in (*active_after, *inactive_after, *deleting_after):
                        if _task_definition_family(task_definition_arn) != family:
                            raise AcceptanceCheckError("orphan_sweep_binding")
                    counts["ecs"]["unapproved_survivors"] += len(active_after) + len(inactive_after)
                    for task_definition_arn in deleting_after:
                        assert isinstance(task_definition_arn, str)
                        if task_definition_arn not in verified_task_definitions:
                            _task_definition_owned(
                                clients.ecs,
                                task_definition_arn,
                                family=family,
                                acceptance_run_id=acceptance_run_id,
                            )
                            verified_task_definitions.add(task_definition_arn)
                        allowed_deleting_task_definitions.add(task_definition_arn)
                        identity_hash = _sha256(task_definition_arn.encode())
                        delete_in_progress_receipts.append(
                            {
                                "resource_sha256": identity_hash,
                                "deregistration_receipt_sha256": _sha256(f"{identity_hash}:INACTIVE".encode()),
                                "deletion_receipt_sha256": _sha256(f"{identity_hash}:DELETE_REQUESTED".encode()),
                                "owner_sha256": _sha256(str(orphan["cleanup_owner"]).encode()),
                                "poll_receipt_sha256": _sha256(f"{identity_hash}:DELETE_IN_PROGRESS".encode()),
                                "follow_up_deadline": _utc_timestamp(now() + timedelta(hours=24)),
                                "zero_dependency_count": 0,
                            }
                        )

                for field, method, argument_name, response_field, surface in (
                    ("ALB_ARN", "describe_load_balancers", "LoadBalancerArns", "LoadBalancers", "elbv2"),
                    ("TARGET_GROUP_ARN", "describe_target_groups", "TargetGroupArns", "TargetGroups", "elbv2"),
                    ("FIRST_DEPLOY_LISTENER_RULE_ARN", "describe_rules", "RuleArns", "Rules", "elbv2"),
                    ("DB_CLUSTER_IDENTIFIER", "describe_db_clusters", "DBClusterIdentifier", "DBClusters", "rds"),
                ):
                    identity = values[field]
                    if identity:
                        argument: object = [identity] if argument_name.endswith("Arns") else identity
                        response = _orphan_call(clients.elbv2 if surface == "elbv2" else clients.rds, method, **{argument_name: argument})
                        counts[surface]["queried"] += 1
                        counts[surface]["unapproved_survivors"] += len(_orphan_response_items(response, response_field))
                listener_arns = orphan["elbv2_listener_arns"]
                assert isinstance(listener_arns, list)
                for listener_arn in listener_arns:
                    response = _orphan_call(clients.elbv2, "describe_listeners", ListenerArns=[listener_arn])
                    counts["elbv2"]["queried"] += 1
                    counts["elbv2"]["unapproved_survivors"] += len(_orphan_response_items(response, "Listeners"))
                db_instances = orphan["rds_db_instance_identifiers"]
                assert isinstance(db_instances, list)
                for identifier in db_instances:
                    response = _orphan_call(clients.rds, "describe_db_instances", DBInstanceIdentifier=identifier)
                    counts["rds"]["queried"] += 1
                    counts["rds"]["unapproved_survivors"] += len(_orphan_response_items(response, "DBInstances"))
                for creation_token in cast(list[str], orphan["efs_creation_tokens"]):
                    response = _orphan_call(clients.efs, "describe_file_systems", CreationToken=creation_token)
                    counts["efs"]["queried"] += 1
                    counts["efs"]["unapproved_survivors"] += len(_orphan_response_items(response, "FileSystems"))
                for file_system_id in cast(list[str], orphan["efs_file_system_ids"]):
                    response = _orphan_call(clients.efs, "describe_file_systems", FileSystemId=file_system_id)
                    counts["efs"]["queried"] += 1
                    counts["efs"]["unapproved_survivors"] += len(_orphan_response_items(response, "FileSystems"))
                    access_points = _orphan_paged_items(
                        clients.efs,
                        "describe_access_points",
                        item_field="AccessPoints",
                        request_token="NextToken",
                        response_token="NextToken",
                        kwargs={"FileSystemId": file_system_id, "MaxResults": 100},
                    )
                    mount_targets = _orphan_paged_items(
                        clients.efs,
                        "describe_mount_targets",
                        item_field="MountTargets",
                        request_token="Marker",
                        response_token="NextMarker",
                        kwargs={"FileSystemId": file_system_id, "MaxItems": 100},
                    )
                    counts["efs"]["queried"] += 2
                    counts["efs"]["unapproved_survivors"] += len(access_points) + len(mount_targets)
                for access_point_id in cast(list[str], orphan["efs_access_point_ids"]):
                    response = _orphan_call(clients.efs, "describe_access_points", AccessPointId=access_point_id, MaxResults=100)
                    counts["efs"]["queried"] += 1
                    counts["efs"]["unapproved_survivors"] += len(_orphan_response_items(response, "AccessPoints"))
                for secret_id in cast(list[str], orphan["secret_ids"]):
                    response = _orphan_call(clients.secretsmanager, "describe_secret", SecretId=secret_id)
                    counts["secretsmanager"]["queried"] += 1
                    if response is not None:
                        counts["secretsmanager"]["unapproved_survivors"] += 1
                for role_name in cast(list[str], orphan["iam_role_names"]):
                    response = _orphan_call(clients.iam, "get_role", RoleName=role_name)
                    counts["iam"]["queried"] += 1
                    if response is not None:
                        role = response.get("Role")
                        if not isinstance(role, Mapping) or role.get("RoleName") != role_name:
                            raise AcceptanceCheckError("orphan_sweep_api")
                        counts["iam"]["unapproved_survivors"] += 1
                resource_policies = _orphan_paged_items(
                    clients.logs,
                    "describe_resource_policies",
                    item_field="resourcePolicies",
                    request_token="nextToken",
                    response_token="nextToken",
                    kwargs={"limit": 50},
                )
                expected_resource_policies = set(cast(list[str], orphan["log_resource_policy_names"]))
                counts["logs"]["queried"] += 1
                for policy in resource_policies:
                    if not isinstance(policy, Mapping) or type(policy.get("policyName")) is not str:
                        raise AcceptanceCheckError("orphan_sweep_api")
                    if policy["policyName"] in expected_resource_policies:
                        counts["logs"]["unapproved_survivors"] += 1
                log_group_names = {
                    str(values[field]) for field in ("WEB_LOG_GROUP", "DOCTOR_LOG_GROUP", "ECS_DEPLOYMENT_EVENT_LOG_GROUP") if values[field]
                } | set(cast(list[str], orphan["log_group_names"]))
                for log_group_name in sorted(log_group_names):
                    groups = _orphan_paged_items(
                        clients.logs,
                        "describe_log_groups",
                        item_field="logGroups",
                        request_token="nextToken",
                        response_token="nextToken",
                        kwargs={"logGroupNamePrefix": log_group_name, "limit": 50},
                    )
                    counts["logs"]["queried"] += 1
                    counts["logs"]["unapproved_survivors"] += sum(
                        isinstance(group, Mapping) and group.get("logGroupName") == log_group_name for group in groups
                    )
                for dashboard_name in cast(list[str], orphan["cloudwatch_dashboard_names"]):
                    dashboards = _orphan_paged_items(
                        clients.cloudwatch,
                        "list_dashboards",
                        item_field="DashboardEntries",
                        request_token="NextToken",
                        response_token="NextToken",
                        kwargs={"DashboardNamePrefix": dashboard_name},
                    )
                    counts["cloudwatch"]["queried"] += 1
                    counts["cloudwatch"]["unapproved_survivors"] += sum(
                        isinstance(entry, Mapping) and entry.get("DashboardName") == dashboard_name for entry in dashboards
                    )
                for alarm_name in cast(list[str], orphan["cloudwatch_alarm_names"]):
                    response = _orphan_call(clients.cloudwatch, "describe_alarms", AlarmNames=[alarm_name], MaxRecords=100)
                    counts["cloudwatch"]["queried"] += 1
                    counts["cloudwatch"]["unapproved_survivors"] += (
                        len(_orphan_response_items(response, "MetricAlarms"))
                        + len(_orphan_response_items(response, "CompositeAlarms"))
                        + len(_orphan_response_items(response, "LogAlarms"))
                    )
                for metric_query in cast(list[dict[str, object]], orphan["cloudwatch_retained_metrics"]):
                    dimensions = cast(list[dict[str, str]], metric_query["dimensions"])
                    metrics = _orphan_paged_items(
                        clients.cloudwatch,
                        "list_metrics",
                        item_field="Metrics",
                        request_token="NextToken",
                        response_token="NextToken",
                        kwargs={
                            "Namespace": metric_query["namespace"],
                            "MetricName": metric_query["metric_name"],
                            "Dimensions": [{"Name": dimension["name"], "Value": dimension["value"]} for dimension in dimensions],
                            "IncludeLinkedAccounts": False,
                        },
                    )
                    counts["cloudwatch"]["queried"] += 1
                    expected_dimensions = sorted(
                        ({"Name": dimension["name"], "Value": dimension["value"]} for dimension in dimensions),
                        key=lambda item: (item["Name"], item["Value"]),
                    )
                    exact = [
                        metric
                        for metric in metrics
                        if isinstance(metric, Mapping)
                        and set(metric) == {"Namespace", "MetricName", "Dimensions"}
                        and metric.get("Namespace") == metric_query["namespace"]
                        and metric.get("MetricName") == metric_query["metric_name"]
                        and isinstance(metric.get("Dimensions"), list)
                        and sorted(metric["Dimensions"], key=lambda item: (item.get("Name"), item.get("Value"))) == expected_dimensions
                    ]
                    if len(metrics) != 1 or len(exact) != 1:
                        counts["cloudwatch"]["unapproved_survivors"] += max(1, abs(len(metrics) - 1))
                    else:
                        observed_retained_metrics += 1
                groups = _orphan_paged_items(
                    clients.xray,
                    "get_groups",
                    item_field="Groups",
                    request_token="NextToken",
                    response_token="NextToken",
                    kwargs={},
                )
                expected_groups = set(cast(list[str], orphan["xray_group_names"]))
                counts["xray"]["queried"] += 1
                counts["xray"]["unapproved_survivors"] += sum(
                    isinstance(group, Mapping) and group.get("GroupName") in expected_groups for group in groups
                )
                sampling_rules = _orphan_paged_items(
                    clients.xray,
                    "get_sampling_rules",
                    item_field="SamplingRuleRecords",
                    request_token="NextToken",
                    response_token="NextToken",
                    kwargs={},
                )
                expected_sampling_rules = set(cast(list[str], orphan["xray_sampling_rule_names"]))
                counts["xray"]["queried"] += 1
                counts["xray"]["unapproved_survivors"] += sum(
                    isinstance(record, Mapping)
                    and isinstance(record.get("SamplingRule"), Mapping)
                    and record["SamplingRule"].get("RuleName") in expected_sampling_rules
                    for record in sampling_rules
                )
                trace_ids = cast(list[str], orphan["xray_retained_trace_ids"])
                for offset in range(0, len(trace_ids), 5):
                    requested_trace_ids = trace_ids[offset : offset + 5]
                    response = _orphan_call(clients.xray, "batch_get_traces", TraceIds=requested_trace_ids)
                    if response is None:
                        raise AcceptanceCheckError("orphan_sweep_api")
                    traces = _orphan_response_items(response, "Traces")
                    if _orphan_response_items(response, "UnprocessedTraceIds"):
                        raise AcceptanceCheckError("orphan_sweep_api")
                    counts["xray"]["queried"] += 1
                    observed_trace_ids = [
                        trace_id
                        for trace in traces
                        if isinstance(trace, Mapping) and type(trace.get("Id")) is str
                        for trace_id in [cast(str, trace["Id"])]
                    ]
                    if len(traces) != len(requested_trace_ids) or sorted(observed_trace_ids) != sorted(requested_trace_ids):
                        counts["xray"]["unapproved_survivors"] += max(1, abs(len(traces) - len(requested_trace_ids)))
                    else:
                        observed_retained_traces += len(traces)
                destination_response = _orphan_call(clients.xray, "get_trace_segment_destination")
                if destination_response is None:
                    raise AcceptanceCheckError("orphan_sweep_api")
                destination = destination_response.get("Destination")
                if destination not in {None, "XRay", "CloudWatchLogs"}:
                    raise AcceptanceCheckError("orphan_sweep_api")
                indexing_rules = _orphan_paged_items(
                    clients.xray,
                    "get_indexing_rules",
                    item_field="IndexingRules",
                    request_token="NextToken",
                    response_token="NextToken",
                    kwargs={},
                    allow_missing_items=True,
                )
                spans_groups = _orphan_paged_items(
                    clients.logs,
                    "describe_log_groups",
                    item_field="logGroups",
                    request_token="nextToken",
                    response_token="nextToken",
                    kwargs={"logGroupNamePrefix": "aws/spans", "limit": 50},
                )
                transaction_projection = _transaction_search_projection(
                    destination=destination,
                    indexing_rules=indexing_rules,
                    spans_log_group_present=any(
                        isinstance(group, Mapping) and group.get("logGroupName") == "aws/spans" for group in spans_groups
                    ),
                )
                counts["xray"]["queried"] += 2
                counts["logs"]["queried"] += 1
                if (
                    _sha256(json.dumps(transaction_projection, sort_keys=True, separators=(",", ":")).encode())
                    != orphan["transaction_search_baseline_sha256"]
                ):
                    counts["xray"]["unapproved_survivors"] += 1
                event_rules = list(cast(list[dict[str, object]], orphan["event_rules"]))
                if values["ECS_DEPLOYMENT_EVENT_RULE"]:
                    event_rules.append(
                        {
                            "event_bus_name": "default",
                            "rule_name": values["ECS_DEPLOYMENT_EVENT_RULE"],
                            "target_ids": [values["ECS_DEPLOYMENT_EVENT_TARGET_ID"]] if values["ECS_DEPLOYMENT_EVENT_TARGET_ID"] else [],
                        }
                    )
                for event_rule in event_rules:
                    assert isinstance(event_rule, dict)
                    response = _orphan_call(
                        clients.events,
                        "describe_rule",
                        Name=event_rule["rule_name"],
                        EventBusName=event_rule["event_bus_name"],
                    )
                    counts["events"]["queried"] += 1
                    if response is not None:
                        counts["events"]["unapproved_survivors"] += 1
                    targets = _orphan_paged_items(
                        clients.events,
                        "list_targets_by_rule",
                        item_field="Targets",
                        request_token="NextToken",
                        response_token="NextToken",
                        kwargs={
                            "Rule": event_rule["rule_name"],
                            "EventBusName": event_rule["event_bus_name"],
                            "Limit": 100,
                        },
                    )
                    counts["events"]["queried"] += 1
                    counts["events"]["unapproved_survivors"] += len(targets)
                for guardrail in cast(list[dict[str, object]], orphan["bedrock_guardrails"]):
                    assert isinstance(guardrail, dict)
                    guardrails = _orphan_paged_items(
                        clients.bedrock,
                        "list_guardrails",
                        item_field="guardrails",
                        request_token="nextToken",
                        response_token="nextToken",
                        kwargs={"guardrailIdentifier": guardrail["identifier"], "maxResults": 1000},
                    )
                    counts["bedrock"]["queried"] += 1
                    if any(not isinstance(item, Mapping) for item in guardrails):
                        raise AcceptanceCheckError("orphan_sweep_api")
                    counts["bedrock"]["unapproved_survivors"] += len(guardrails)
                user_pool_id = values["COGNITO_USER_POOL_ID"]
                if user_pool_id and orphan["cognito_pool_owned"] is True:
                    response = _orphan_call(clients.cognito, "describe_user_pool", UserPoolId=user_pool_id)
                    counts["cognito"]["queried"] += 1
                    if response is not None:
                        counts["cognito"]["unapproved_survivors"] += 1
                subject_sub = orphan["cognito_subject_sub"]
                if user_pool_id and subject_sub:
                    users = _orphan_paged_items(
                        clients.cognito,
                        "list_users",
                        item_field="Users",
                        request_token="PaginationToken",
                        response_token="PaginationToken",
                        kwargs={
                            "UserPoolId": user_pool_id,
                            "Filter": f'sub = "{subject_sub}"',
                            "AttributesToGet": ["sub"],
                            "Limit": 60,
                        },
                    )
                    counts["cognito"]["queried"] += 1
                    counts["cognito"]["unapproved_survivors"] += len(users)
            for mapping in tagged:
                if not isinstance(mapping, Mapping) or type(mapping.get("ResourceARN")) is not str:
                    raise AcceptanceCheckError("orphan_sweep_api")
                if mapping["ResourceARN"] not in allowed_deleting_task_definitions:
                    counts["tagging"]["unapproved_survivors"] += 1
        except AcceptanceCheckError:
            raise
        except Exception:
            raise AcceptanceCheckError("orphan_sweep_api") from None
    total_survivors = sum(surface["unapproved_survivors"] for surface in counts.values())
    receipt: dict[str, object] = {
        "schema": "elspeth.aws-ecs-orphan-sweep.v1",
        "checked_at": _utc_timestamp(now()),
        "acceptance_run_id_sha256": _sha256(acceptance_run_id.encode()),
        "surfaces": counts,
        "expected_retained": {"metric_series": retained_metrics, "trace_ids": retained_traces},
        "observed_retained": {"metric_series": observed_retained_metrics, "trace_ids": observed_retained_traces},
        "delete_in_progress_receipts": delete_in_progress_receipts,
        "total_unapproved_survivors": total_survivors,
        "ok": total_survivors == 0,
    }
    _validate_bounded_receipt_document(receipt)
    if total_survivors:
        raise AcceptanceCheckError("orphan_sweep_survivors")
    return receipt


def validate_compatibility_record(
    record_path: Path,
    *,
    manifest_path: Path,
    scenario_id: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Validate the release/schema authority bound to one resolved scenario."""

    if scenario_id not in _APPLICATION_SCENARIO_IDS:
        raise AcceptanceCheckError("compatibility_record_binding")
    manifest = _read_control_manifest(manifest_path)
    inventory = _load_bound_scenario_inventory(manifest, scenario_id, require_resolved=True)
    values = inventory["values"]
    ecr = manifest["ecr"]
    assert isinstance(values, dict) and isinstance(ecr, dict)
    record = _read_protected_document(record_path, check="compatibility_record_file")
    fields = {
        "schema",
        "record_id",
        "acceptance_run_id",
        "scenario_id",
        "candidate_sha",
        "candidate_image_digest",
        "candidate_task_definition",
        "candidate_doctor_task_definition",
        "candidate_package_version",
        "previous_source_sha",
        "previous_image_digest",
        "previous_task_definition",
        "rollback_doctor_task_definition",
        "previous_package_version",
        "schema_facts",
        "forward_compatible",
        "backward_compatible",
        "rollback_permitted",
        "decision",
        "approver_identity",
        "countersigner_identity",
        "approved_at",
        "countersigned_at",
        "expires_at",
    }
    if set(record) != fields or record["schema"] != "elspeth.aws-ecs-compatibility-record.v2":
        raise AcceptanceCheckError("compatibility_record_schema")
    previous = values["PREVIOUS_TASK_DEFINITION"] if scenario_id == "B" else ""
    rollback_doctor = values["ROLLBACK_DOCTOR_TASK_DEFINITION"] if scenario_id == "B" else ""
    previous_digest = ecr["baseline_digest"] if scenario_id == "B" else ""
    baseline_tag = ecr["baseline_tag"] if scenario_id == "B" else ""
    baseline_match = re.search(r"baseline-([0-9a-f]{40})$", cast(str, baseline_tag)) if baseline_tag else None
    previous_source_sha = baseline_match.group(1) if baseline_match is not None else ""
    expected_schema_facts = _expected_schema_facts(scenario_id)
    if (
        record["acceptance_run_id"] != manifest["acceptance_run_id"]
        or record["scenario_id"] != scenario_id
        or record["candidate_sha"] != manifest["candidate_sha"]
        or record["candidate_image_digest"] != ecr["candidate_digest"]
        or record["candidate_task_definition"] != values["CANDIDATE_TASK_DEFINITION"]
        or record["candidate_doctor_task_definition"] != values["DOCTOR_TASK_DEFINITION"]
        or record["candidate_package_version"] != _CANDIDATE_PACKAGE_VERSION
        or record["previous_source_sha"] != previous_source_sha
        or record["previous_image_digest"] != previous_digest
        or record["previous_task_definition"] != previous
        or record["rollback_doctor_task_definition"] != rollback_doctor
        or record["previous_package_version"] != (_ROLLBACK_PACKAGE_VERSION if scenario_id == "B" else "")
        or record["schema_facts"] != expected_schema_facts
        or record["decision"] != "approved"
        or record["forward_compatible"] is not True
        or record["backward_compatible"] is not False
        or record["rollback_permitted"] is not False
    ):
        raise AcceptanceCheckError("compatibility_record_binding")
    if any(type(record[field]) is not bool for field in ("forward_compatible", "backward_compatible", "rollback_permitted")):
        raise AcceptanceCheckError("compatibility_record_schema")
    for field in ("record_id", "approver_identity", "countersigner_identity"):
        value = record[field]
        if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}", value) is None:
            raise AcceptanceCheckError("compatibility_record_schema")
    if record["approver_identity"] == record["countersigner_identity"]:
        raise AcceptanceCheckError("compatibility_record_schema")
    approved_at = _control_timestamp(record["approved_at"])
    countersigned_at = _control_timestamp(record["countersigned_at"])
    expires_at = _control_timestamp(record["expires_at"])
    current = now()
    if current.tzinfo is None or current.utcoffset() is None or not approved_at <= countersigned_at <= current < expires_at:
        raise AcceptanceCheckError("compatibility_record_expired")
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema": "elspeth.aws-ecs-compatibility-receipt.v2",
        "record_sha256": _sha256(canonical),
        "acceptance_run_id_sha256": _sha256(cast(str, manifest["acceptance_run_id"]).encode()),
        "scenario_id": scenario_id,
        "candidate_sha": manifest["candidate_sha"],
        "candidate_image_digest": ecr["candidate_digest"],
        "candidate_task_definition_sha256": _sha256(cast(str, values["CANDIDATE_TASK_DEFINITION"]).encode()),
        "candidate_doctor_task_definition_sha256": _sha256(cast(str, values["DOCTOR_TASK_DEFINITION"]).encode()),
        "candidate_package_version": _CANDIDATE_PACKAGE_VERSION,
        "previous_source_sha": previous_source_sha or None,
        "previous_image_digest": previous_digest or None,
        "previous_task_definition_sha256": _sha256(cast(str, previous).encode()) if previous else None,
        "rollback_doctor_task_definition_sha256": _sha256(cast(str, rollback_doctor).encode()) if rollback_doctor else None,
        "previous_package_version": _ROLLBACK_PACKAGE_VERSION if scenario_id == "B" else None,
        "schema_facts": expected_schema_facts,
        "forward_compatible": record["forward_compatible"],
        "backward_compatible": record["backward_compatible"],
        "rollback_permitted": record["rollback_permitted"],
        "decision": "approved",
        "approvals_present": True,
        "expires_at": _utc_timestamp(expires_at),
    }


def receipt_store(
    manifest_path: Path,
    *,
    scenario_id: str,
    kind: str,
    subject_id: str,
    receipt_file: Path | None = None,
    receipt_bytes: bytes | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Persist one receipt while serializing its manifest state transition."""

    with _receipt_manifest_write_lock(manifest_path, check="receipt_store_write"):
        return _receipt_store_locked(
            manifest_path,
            scenario_id=scenario_id,
            kind=kind,
            subject_id=subject_id,
            receipt_file=receipt_file,
            receipt_bytes=receipt_bytes,
            now=now,
        )


def _receipt_store_locked(
    manifest_path: Path,
    *,
    scenario_id: str,
    kind: str,
    subject_id: str,
    receipt_file: Path | None = None,
    receipt_bytes: bytes | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Persist one bounded sanitized receipt while the manifest lock is held."""

    if (
        scenario_id not in _INFRASTRUCTURE_APPROVAL_SCOPES
        or kind not in _RECEIPT_KINDS
        or (scenario_id == "bootstrap" and kind not in _TERRAFORM_RECEIPT_KINDS)
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    if (
        type(subject_id) is not str
        or not subject_id
        or len(subject_id) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in subject_id)
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    if (receipt_file is None) == (receipt_bytes is None):
        raise AcceptanceCheckError("receipt_store_input")
    if receipt_file is not None:
        document = _read_protected_document(receipt_file, check="receipt_store_file")
    else:
        assert receipt_bytes is not None
        if len(receipt_bytes) > MAX_CONTROL_DOCUMENT_BYTES:
            raise AcceptanceCheckError("receipt_store_file")
        try:
            decoded = json.loads(receipt_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AcceptanceCheckError("receipt_store_schema") from None
        if not isinstance(decoded, dict):
            raise AcceptanceCheckError("receipt_store_schema")
        document = decoded
    subject_sha256 = _sha256(subject_id.encode("utf-8"))
    manifest = _read_control_manifest(manifest_path)
    _require_mutable_control_manifest(manifest)
    candidate_sha = manifest["candidate_sha"]
    assert isinstance(candidate_sha, str)
    expected_plugin_policy_binding_sha256: str | None = None
    if kind == "verify-bedrock-guardrails":
        inventory = _load_bound_scenario_inventory(manifest, scenario_id, require_resolved=True)
        values = inventory["values"]
        assert isinstance(values, dict)
        binding = values["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"]
        assert isinstance(binding, str)
        expected_plugin_policy_binding_sha256 = binding
    if kind == "compatibility-record":
        inventory = _load_bound_scenario_inventory(manifest, scenario_id, require_resolved=True)
        values = inventory["values"]
        ecr = manifest["ecr"]
        if not isinstance(values, dict) or not isinstance(ecr, dict) or not isinstance(document, Mapping):
            raise AcceptanceCheckError("receipt_store_binding")
        previous = values["PREVIOUS_TASK_DEFINITION"] if scenario_id == "B" else ""
        rollback_doctor = values["ROLLBACK_DOCTOR_TASK_DEFINITION"] if scenario_id == "B" else ""
        baseline_tag = ecr["baseline_tag"] if scenario_id == "B" else ""
        baseline_match = re.search(r"baseline-([0-9a-f]{40})$", cast(str, baseline_tag)) if baseline_tag else None
        previous_source_sha = baseline_match.group(1) if baseline_match is not None else None
        if (
            document.get("acceptance_run_id_sha256") != _sha256(cast(str, manifest["acceptance_run_id"]).encode())
            or document.get("candidate_image_digest") != ecr["candidate_digest"]
            or document.get("candidate_task_definition_sha256") != _sha256(cast(str, values["CANDIDATE_TASK_DEFINITION"]).encode())
            or document.get("candidate_doctor_task_definition_sha256") != _sha256(cast(str, values["DOCTOR_TASK_DEFINITION"]).encode())
            or document.get("previous_source_sha") != previous_source_sha
            or document.get("previous_image_digest") != (ecr["baseline_digest"] if scenario_id == "B" else None)
            or document.get("previous_task_definition_sha256") != (_sha256(cast(str, previous).encode()) if previous else None)
            or document.get("rollback_doctor_task_definition_sha256")
            != (_sha256(cast(str, rollback_doctor).encode()) if rollback_doctor else None)
            or _control_timestamp(document.get("expires_at")) <= now()
        ):
            raise AcceptanceCheckError("receipt_store_binding")
    document = _validate_stored_receipt(
        document,
        kind=kind,
        scenario_id=scenario_id,
        subject_sha256=subject_sha256,
        candidate_sha=candidate_sha,
        subject_id=subject_id,
        expected_plugin_policy_binding_sha256=expected_plugin_policy_binding_sha256,
    )
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    receipt_sha256 = _sha256(canonical)
    receipt_directory = manifest_path.parent / f"{manifest_path.name}.receipts"
    try:
        receipt_directory.mkdir(mode=0o700, exist_ok=True)
        directory_stat = receipt_directory.lstat()
    except OSError:
        raise AcceptanceCheckError("receipt_store_write") from None
    if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077:
        raise AcceptanceCheckError("receipt_store_write")
    stored_path = receipt_directory / f"{receipt_sha256}.json"
    if stored_path.exists():
        existing = _validate_stored_receipt(
            _read_protected_document(stored_path, check="receipt_store_file"),
            kind=kind,
            scenario_id=scenario_id,
            subject_sha256=subject_sha256,
            candidate_sha=candidate_sha,
            subject_id=subject_id,
            expected_plugin_policy_binding_sha256=expected_plugin_policy_binding_sha256,
        )
        if existing != document:
            raise AcceptanceCheckError("receipt_store_conflict")
    else:
        _write_protected_document(
            stored_path,
            document,
            create=True,
            exists_check="receipt_store_conflict",
            write_check="receipt_store_write",
            parent_check="receipt_store_write",
        )
    evidence = manifest["evidence"]
    assert isinstance(evidence, dict)
    receipts = evidence["receipts"]
    assert isinstance(receipts, list)
    record = {
        "scenario_id": scenario_id,
        "kind": kind,
        "subject_sha256": subject_sha256,
        "receipt_sha256": receipt_sha256,
        "stored_at": _utc_timestamp(now()),
    }
    matches = [
        item
        for item in receipts
        if isinstance(item, dict)
        and item.get("scenario_id") == scenario_id
        and item.get("kind") == kind
        and item.get("subject_sha256") == subject_sha256
    ]
    if matches:
        comparable = {**record, "stored_at": matches[0].get("stored_at")}
        if matches != [comparable]:
            raise AcceptanceCheckError("receipt_store_conflict")
        return receipt_sha256
    receipts.append(record)
    manifest["updated_at"] = record["stored_at"]
    _validate_control_manifest(manifest)
    _write_protected_document(
        manifest_path,
        manifest,
        create=False,
        exists_check="control_manifest_exists",
        write_check="control_manifest_file",
    )
    return receipt_sha256


def _decode_approval_base64url(value: object, *, expected_bytes: int) -> bytes:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise AcceptanceCheckError("approval_verifier")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        raise AcceptanceCheckError("approval_verifier") from None
    if len(decoded) != expected_bytes or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value:
        raise AcceptanceCheckError("approval_verifier")
    return decoded


def _configured_approval_signature_verifier(
    environ: Mapping[str, str],
) -> Callable[[bytes, str, str], bool]:
    keyring_value = environ.get("ELSPETH_ACCEPTANCE_APPROVAL_KEYRING")
    if not keyring_value:
        raise AcceptanceCheckError("approval_verifier")
    keyring = _read_protected_document(Path(keyring_value), check="approval_verifier")
    if set(keyring) != {"schema", "keys"} or keyring["schema"] != "elspeth.aws-ecs-approval-keyring.v1":
        raise AcceptanceCheckError("approval_verifier")
    keys = keyring["keys"]
    if not isinstance(keys, dict) or not 1 <= len(keys) <= 64:
        raise AcceptanceCheckError("approval_verifier")
    decoded_keys: dict[str, bytes] = {}
    for key_id, encoded_key in keys.items():
        if type(key_id) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", key_id) is None:
            raise AcceptanceCheckError("approval_verifier")
        decoded_keys[key_id] = _decode_approval_base64url(encoded_key, expected_bytes=32)

    def verify(payload: bytes, signature: str, key_id: str) -> bool:
        public_key_bytes = decoded_keys.get(key_id)
        if public_key_bytes is None:
            return False
        signature_bytes = _decode_approval_base64url(signature, expected_bytes=64)
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature_bytes, payload)
        except Exception:
            return False
        return True

    return verify


@_serialized_control_manifest_write
def approval_verify(
    manifest_path: Path,
    *,
    scenario_id: str,
    kind: str,
    plan_receipt_hash: str,
    approval_file: Path,
    signature_verifier: Callable[[bytes, str, str], bool] | None = None,
    environ: Mapping[str, str] = os.environ,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Verify a bound, unexpired explicit approval through an injected trust root."""

    if signature_verifier is None:
        signature_verifier = _configured_approval_signature_verifier(environ)
    if scenario_id not in _INFRASTRUCTURE_APPROVAL_SCOPES or kind not in {"terraform-plan", "terraform-destroy-plan"}:
        raise AcceptanceCheckError("approval_binding")
    if _SHA256_PATTERN.fullmatch(plan_receipt_hash) is None:
        raise AcceptanceCheckError("approval_binding")
    manifest = _read_control_manifest(manifest_path)
    _require_mutable_control_manifest(manifest)
    evidence = manifest["evidence"]
    assert isinstance(evidence, dict)
    receipts = evidence["receipts"]
    assert isinstance(receipts, list)
    if not any(
        isinstance(receipt, dict)
        and receipt.get("scenario_id") == scenario_id
        and receipt.get("kind") == kind
        and receipt.get("receipt_sha256") == plan_receipt_hash
        for receipt in receipts
    ):
        raise AcceptanceCheckError("approval_binding")
    approval = _read_protected_document(approval_file, check="approval_file")
    fields = {
        "schema",
        "acceptance_run_id",
        "scenario_id",
        "kind",
        "plan_receipt_hash",
        "approver_identity",
        "authority",
        "decision",
        "approved_at",
        "expires_at",
        "key_id",
        "signature",
    }
    if set(approval) != fields or approval["schema"] != "elspeth.aws-ecs-approval.v1":
        raise AcceptanceCheckError("approval_schema")
    expected_authority = "terraform-apply" if kind == "terraform-plan" else "terraform-destroy"
    if (
        approval["acceptance_run_id"] != manifest["acceptance_run_id"]
        or approval["scenario_id"] != scenario_id
        or approval["kind"] != kind
        or approval["plan_receipt_hash"] != plan_receipt_hash
        or approval["authority"] != expected_authority
        or approval["decision"] != "approved"
    ):
        raise AcceptanceCheckError("approval_binding")
    for field in ("approver_identity", "key_id", "signature"):
        value = approval[field]
        if (
            type(value) is not str
            or not value
            or len(value) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise AcceptanceCheckError("approval_schema")
    approved_at = _control_timestamp(approval["approved_at"])
    expires_at = _control_timestamp(approval["expires_at"])
    current = now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise AcceptanceCheckError("approval_schema")
    if not approved_at <= current < expires_at:
        raise AcceptanceCheckError("approval_expired")
    signed = {key: value for key, value in approval.items() if key != "signature"}
    canonical = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = approval["signature"]
    key_id = approval["key_id"]
    assert isinstance(signature, str) and isinstance(key_id, str)
    try:
        verified = signature_verifier(canonical, signature, key_id)
    except Exception:
        raise AcceptanceCheckError("approval_signature") from None
    if verified is not True:
        raise AcceptanceCheckError("approval_signature")
    approval_sha256 = _sha256(json.dumps(approval, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    approvals = evidence["approvals"]
    assert isinstance(approvals, list)
    record = {
        "scenario_id": scenario_id,
        "kind": kind,
        "plan_receipt_sha256": plan_receipt_hash,
        "approval_sha256": approval_sha256,
        "approval_path": _control_path(str(approval_file)),
        "expires_at": _utc_timestamp(expires_at),
        "verified_at": _utc_timestamp(current),
    }
    matches = [
        item
        for item in approvals
        if isinstance(item, dict)
        and item.get("scenario_id") == scenario_id
        and item.get("kind") == kind
        and item.get("plan_receipt_sha256") == plan_receipt_hash
    ]
    if matches:
        comparable = {**record, "verified_at": matches[0].get("verified_at")}
        if matches != [comparable]:
            raise AcceptanceCheckError("approval_conflict")
        return approval_sha256
    approvals.append(record)
    manifest["updated_at"] = record["verified_at"]
    _validate_control_manifest(manifest)
    _write_protected_document(
        manifest_path,
        manifest,
        create=False,
        exists_check="control_manifest_exists",
        write_check="control_manifest_file",
    )
    return approval_sha256


def approval_require_current(
    manifest_path: Path,
    *,
    scenario_id: str,
    kind: str,
    plan_receipt_hash: str,
    approval_hash: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Reopen a stored approval and require it to be current at point of use."""

    if (
        scenario_id not in _INFRASTRUCTURE_APPROVAL_SCOPES
        or kind not in {"terraform-plan", "terraform-destroy-plan"}
        or _SHA256_PATTERN.fullmatch(plan_receipt_hash) is None
        or _SHA256_PATTERN.fullmatch(approval_hash) is None
    ):
        raise AcceptanceCheckError("approval_binding")
    manifest = _read_control_manifest(manifest_path)
    evidence = manifest["evidence"]
    assert isinstance(evidence, Mapping)
    approvals = evidence["approvals"]
    assert isinstance(approvals, list)
    _require_current_approval(
        cast(list[object], approvals),
        scenario_id=scenario_id,
        kind=kind,
        plan_receipt_sha256=plan_receipt_hash,
        approval_sha256=approval_hash,
        current=now(),
    )


_LOG_PROJECTION_FIELDS = (
    "event_name",
    "check",
    "class_name",
    "severity",
    "status",
    "outcome",
    "task_revision",
    "deployment_revision",
    "count",
    "ok",
)


def _safe_projection_value(field: str, value: object) -> object | None:
    if field in {"count", "task_revision", "deployment_revision"}:
        return value if type(value) is int and 0 <= value <= 2**63 - 1 else None
    if field == "ok":
        return value if type(value) is bool else None
    if type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value) is not None and "://" not in value:
        return value
    return None


def _project_log_record(record: Mapping[str, object], *, timestamp: object | None = None) -> dict[str, object]:
    projected: dict[str, object] = {}
    candidate_timestamp = timestamp if timestamp is not None else record.get("timestamp")
    if type(candidate_timestamp) is int and candidate_timestamp >= 0:
        projected["timestamp"] = candidate_timestamp
    elif type(candidate_timestamp) is str:
        try:
            _parse_state_timestamp(candidate_timestamp)
        except AcceptanceStateError:
            pass
        else:
            projected["timestamp"] = candidate_timestamp
    for field in _LOG_PROJECTION_FIELDS:
        value = _safe_projection_value(field, record.get(field))
        if value is not None:
            projected[field] = value
    return projected


def sanitize_evidence(kind: str, payload: object) -> dict[str, object]:
    """Project raw diagnostic JSON into one closed, content-free evidence schema."""

    if kind not in _EVIDENCE_KINDS or not isinstance(payload, dict):
        raise AcceptanceCheckError("sanitize_evidence_schema")
    base: dict[str, object] = {
        "schema": "elspeth.aws-ecs-sanitized-evidence.v1",
        "kind": kind,
    }
    if kind in {"web-log", "doctor-log"}:
        events = payload.get("events")
        if not isinstance(events, list) or len(events) > 10_000:
            raise AcceptanceCheckError("sanitize_evidence_schema")
        records: list[dict[str, object]] = []
        for event in events:
            if not isinstance(event, Mapping):
                raise AcceptanceCheckError("sanitize_evidence_schema")
            message = event.get("message")
            if isinstance(message, str) and len(message.encode("utf-8")) <= MAX_JSON_RESPONSE_BYTES:
                try:
                    decoded = json.loads(message)
                except json.JSONDecodeError:
                    decoded = None
            elif isinstance(message, Mapping):
                decoded = message
            else:
                decoded = None
            source = decoded if isinstance(decoded, Mapping) else event
            projected = _project_log_record(source, timestamp=event.get("timestamp"))
            if projected:
                records.append(projected)
        return {
            **base,
            "records": records,
            "counts": {"input": len(events), "projected": len(records)},
        }
    if kind == "deployment-event":
        detail = payload.get("detail", payload)
        if not isinstance(detail, Mapping):
            raise AcceptanceCheckError("sanitize_evidence_schema")
        projected = _project_log_record(detail, timestamp=payload.get("time"))
        return {**base, "records": [projected] if projected else [], "counts": {"input": 1, "projected": bool(projected)}}
    if kind == "task-definition":
        task = payload.get("taskDefinition")
        if not isinstance(task, Mapping):
            raise AcceptanceCheckError("sanitize_evidence_schema")
        revision = task.get("revision")
        network_mode = task.get("networkMode")
        containers = task.get("containerDefinitions")
        volumes = task.get("volumes")
        compatibilities = task.get("requiresCompatibilities")
        if (
            type(revision) is not int
            or revision < 1
            or network_mode not in {"awsvpc", "bridge", "host", "none"}
            or not isinstance(containers, list)
            or not isinstance(volumes, list)
            or not isinstance(compatibilities, list)
        ):
            raise AcceptanceCheckError("sanitize_evidence_schema")
        return {
            **base,
            "projection": {
                "revision": revision,
                "network_mode": network_mode,
                "container_count": len(containers),
                "volume_count": len(volumes),
                "fargate_required": "FARGATE" in compatibilities,
            },
        }
    changes = payload.get("resource_changes")
    if not isinstance(changes, list) or len(changes) > 100_000:
        raise AcceptanceCheckError("sanitize_evidence_schema")
    counts = {"create": 0, "update": 0, "delete": 0, "replace": 0, "no-op": 0}
    for resource_change in changes:
        if not isinstance(resource_change, Mapping):
            raise AcceptanceCheckError("sanitize_evidence_schema")
        change = resource_change.get("change")
        actions = change.get("actions") if isinstance(change, Mapping) else None
        if not isinstance(actions, list) or any(action not in {"create", "update", "delete", "no-op", "read"} for action in actions):
            raise AcceptanceCheckError("sanitize_evidence_schema")
        if set(actions) == {"create", "delete"}:
            counts["replace"] += 1
        elif actions == ["create"]:
            counts["create"] += 1
        elif actions == ["update"]:
            counts["update"] += 1
        elif actions == ["delete"]:
            counts["delete"] += 1
        elif actions == ["no-op"]:
            counts["no-op"] += 1
    return {
        **base,
        "projection": {
            "resource_change_count": len(changes),
            "create_count": counts["create"],
            "update_count": counts["update"],
            "delete_count": counts["delete"],
            "replace_count": counts["replace"],
            "no_op_count": counts["no-op"],
            "has_delete": counts["delete"] > 0,
            "has_replace": counts["replace"] > 0,
        },
    }


def _verify_stored_receipts(manifest_path: Path, manifest: Mapping[str, object]) -> tuple[int, str]:
    evidence = manifest["evidence"]
    assert isinstance(evidence, dict)
    receipts = evidence["receipts"]
    assert isinstance(receipts, list)
    candidate_sha = manifest["candidate_sha"]
    assert isinstance(candidate_sha, str)
    receipt_directory = manifest_path.parent / f"{manifest_path.name}.receipts"
    for record in receipts:
        assert isinstance(record, dict)
        receipt_hash = record["receipt_sha256"]
        scenario_id = record["scenario_id"]
        kind = record["kind"]
        subject_sha256 = record["subject_sha256"]
        assert isinstance(receipt_hash, str)
        assert isinstance(scenario_id, str) and isinstance(kind, str) and isinstance(subject_sha256, str)
        document = _validate_stored_receipt(
            _read_protected_document(receipt_directory / f"{receipt_hash}.json", check="cleanup_finalize_receipt"),
            kind=kind,
            scenario_id=scenario_id,
            subject_sha256=subject_sha256,
            candidate_sha=candidate_sha,
        )
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if _sha256(canonical) != receipt_hash:
            raise AcceptanceCheckError("cleanup_finalize_receipt")
    approvals = evidence["approvals"]
    assert isinstance(approvals, list)
    evidence_records = {"receipts": receipts, "approvals": approvals}
    return len(receipts) + len(approvals), _sha256(json.dumps(evidence_records, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _final_cleanup_receipt_document(
    manifest_path: Path,
    manifest: Mapping[str, object],
    *,
    ledger_sha256: str,
    receipts_sha256: str,
    committed_at: str,
) -> dict[str, object]:
    manifest_sha256 = _sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return {
        "schema": "elspeth.aws-ecs-final-cleanup-receipt.v1",
        "manifest_sha256": manifest_sha256,
        "ledger_sha256": ledger_sha256,
        "receipts_sha256": receipts_sha256,
        "committed_at": committed_at,
    }


def _verify_final_cleanup_receipt(manifest_path: Path, manifest: Mapping[str, object]) -> None:
    final_evidence = manifest["final_evidence"]
    if not isinstance(final_evidence, Mapping) or final_evidence.get("phase") != "committed":
        raise AcceptanceCheckError("cleanup_finalize_receipt")
    ledger = _read_gate_ledger(Path(cast(str, manifest["gate_ledger_path"])))
    ledger_sha256 = _gate_ledger_records_hash(ledger)
    _receipt_count, receipts_sha256 = _verify_stored_receipts(manifest_path, manifest)
    committed_at = final_evidence["committed_at"]
    if type(committed_at) is not str:
        raise AcceptanceCheckError("cleanup_finalize_receipt")
    expected = _final_cleanup_receipt_document(
        manifest_path,
        manifest,
        ledger_sha256=ledger_sha256,
        receipts_sha256=receipts_sha256,
        committed_at=committed_at,
    )
    final_receipt_path = manifest_path.with_name(f"{manifest_path.name}.final-receipt.json")
    if _read_protected_document(final_receipt_path, check="cleanup_finalize_receipt") != expected:
        raise AcceptanceCheckError("cleanup_finalize_receipt")


def _ensure_final_cleanup_receipt(
    manifest_path: Path,
    manifest: Mapping[str, object],
    *,
    ledger_sha256: str,
    receipts_sha256: str,
    committed_at: str,
) -> None:
    final_receipt = _final_cleanup_receipt_document(
        manifest_path,
        manifest,
        ledger_sha256=ledger_sha256,
        receipts_sha256=receipts_sha256,
        committed_at=committed_at,
    )
    final_receipt_path = manifest_path.with_name(f"{manifest_path.name}.final-receipt.json")
    if final_receipt_path.exists():
        if _read_protected_document(final_receipt_path, check="cleanup_finalize_receipt") != final_receipt:
            raise AcceptanceCheckError("cleanup_finalize_conflict")
        return
    _write_protected_document(
        final_receipt_path,
        final_receipt,
        create=True,
        exists_check="cleanup_finalize_conflict",
        write_check="cleanup_finalize_receipt",
    )


@_serialized_control_manifest_write
def cleanup_evidence_finalize(
    manifest_path: Path,
    *,
    ledger_path: Path,
    phase: Literal["prepare", "commit"],
    clear_cleanup_required: bool,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Prepare and commit final cleanup evidence without prematurely clearing teardown."""

    manifest = _read_control_manifest(manifest_path)
    if manifest["gate_ledger_path"] != str(ledger_path):
        raise AcceptanceCheckError("cleanup_finalize_binding")
    ledger = _read_gate_ledger(ledger_path)
    cleanup_records = ledger["cleanup_records"]
    assert isinstance(cleanup_records, list)
    terminal_records = [
        record for record in cleanup_records if isinstance(record, dict) and record.get("check_id") == _TERMINAL_GATE_CHECK_ID
    ]
    if len(terminal_records) > 1:
        raise AcceptanceCheckError("gate_ledger_conflict")
    prefix_records = [
        record for record in cleanup_records if not isinstance(record, dict) or record.get("check_id") != _TERMINAL_GATE_CHECK_ID
    ]
    if [record["check_id"] for record in prefix_records if isinstance(record, dict)] != list(_CLEANUP_GATE_CHECK_ORDER[:-1]):
        raise AcceptanceCheckError("gate_ledger_incomplete")
    receipt_count, receipts_sha256 = _verify_stored_receipts(manifest_path, manifest)
    ledger_records_sha256 = _gate_ledger_records_hash({**ledger, "cleanup_records": prefix_records})
    evidence = manifest["evidence"]
    assert isinstance(evidence, Mapping)
    export_receipt_path = evidence["final_export_receipt_path"]
    export_receipt_sha256 = evidence["final_export_receipt_sha256"]
    if type(export_receipt_path) is not str or type(export_receipt_sha256) is not str:
        raise AcceptanceCheckError("cleanup_finalize_export")
    _export_receipt, observed_export_sha256 = _validate_evidence_export_receipt(
        Path(export_receipt_path),
        manifest=manifest,
        receipts_sha256=receipts_sha256,
        ledger_records_sha256=ledger_records_sha256,
    )
    if observed_export_sha256 != export_receipt_sha256:
        raise AcceptanceCheckError("cleanup_finalize_export")
    timestamp = _utc_timestamp(now())
    candidate_sha = manifest["candidate_sha"]
    assert isinstance(candidate_sha, str)
    terminal_receipt_hash = _sha256(
        json.dumps(
            {
                "check_id": _TERMINAL_GATE_CHECK_ID,
                "prefix_records_sha256": ledger_records_sha256,
                "receipts_sha256": receipts_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if phase == "prepare":
        if clear_cleanup_required or ledger["finalized"] is not None:
            raise AcceptanceCheckError("cleanup_finalize_phase")
        existing = manifest["final_evidence"]
        prepared = {
            "phase": "prepared",
            "prepared_at": timestamp,
            "receipt_count": receipt_count,
            "receipts_sha256": receipts_sha256,
            "ledger_records_sha256": ledger_records_sha256,
            "precommit_manifest_sha256": None,
            "committed_at": None,
            "ledger_sha256": None,
        }
        if isinstance(existing, dict):
            if existing["phase"] == "committed":
                return manifest
            comparable = {**prepared, "prepared_at": existing["prepared_at"]}
            if existing != comparable:
                raise AcceptanceCheckError("cleanup_finalize_conflict")
            if terminal_records:
                terminal = terminal_records[0]
                if (
                    terminal.get("candidate_sha") != candidate_sha
                    or terminal.get("exit_status") != 0
                    or terminal.get("receipt_hash") != terminal_receipt_hash
                ):
                    raise AcceptanceCheckError("cleanup_finalize_conflict")
            return manifest
        if terminal_records:
            raise AcceptanceCheckError("cleanup_finalize_phase")
        manifest["final_evidence"] = prepared
        manifest["updated_at"] = timestamp
        _validate_control_manifest(manifest)
        _write_protected_document(
            manifest_path,
            manifest,
            create=False,
            exists_check="control_manifest_exists",
            write_check="control_manifest_file",
        )
        return manifest
    if phase != "commit" or not clear_cleanup_required:
        raise AcceptanceCheckError("cleanup_finalize_phase")
    final_evidence = manifest["final_evidence"]
    if isinstance(final_evidence, dict) and final_evidence["phase"] == "committed" and manifest["cleanup_required"] is False:
        ledger_sha256 = _gate_ledger_records_hash(ledger)
        if (
            final_evidence["ledger_sha256"] != ledger_sha256
            or final_evidence["receipts_sha256"] != receipts_sha256
            or type(final_evidence["committed_at"]) is not str
        ):
            raise AcceptanceCheckError("cleanup_finalize_conflict")
        _ensure_final_cleanup_receipt(
            manifest_path,
            manifest,
            ledger_sha256=ledger_sha256,
            receipts_sha256=receipts_sha256,
            committed_at=final_evidence["committed_at"],
        )
        return manifest
    if manifest["cleanup_required"] is not True or not isinstance(final_evidence, dict):
        raise AcceptanceCheckError("cleanup_finalize_pending")
    assert isinstance(final_evidence, dict)
    if final_evidence["phase"] != "prepared":
        raise AcceptanceCheckError("cleanup_finalize_pending")
    if (
        final_evidence["receipt_count"] != receipt_count
        or final_evidence["receipts_sha256"] != receipts_sha256
        or final_evidence["ledger_records_sha256"] != ledger_records_sha256
    ):
        raise AcceptanceCheckError("cleanup_finalize_conflict")
    cleanup_states = manifest["cleanup_states"]
    assert isinstance(cleanup_states, dict)
    for surface, state_value in cleanup_states.items():
        if surface == "coordinator":
            continue
        if surface == "teardown_deadline" and manifest["deadline_failure_recorded"] is True:
            if state_value not in {"confirmed", "failed", "pending"}:
                raise AcceptanceCheckError("cleanup_finalize_pending")
            continue
        if state_value != "confirmed":
            raise AcceptanceCheckError("cleanup_finalize_pending")
    cleanup_states["coordinator"] = "confirmed"
    if manifest["deadline_failure_recorded"] is True and cleanup_states["teardown_deadline"] != "confirmed":
        cleanup_states["teardown_deadline"] = "failed"
    if not terminal_records:
        gate_ledger_record_cleanup(
            ledger_path,
            check_id=_TERMINAL_GATE_CHECK_ID,
            exit_status=0,
            receipt_hash=terminal_receipt_hash,
            candidate_sha=candidate_sha,
            started_at=timestamp,
            ended_at=timestamp,
            now=now,
        )
    else:
        terminal = terminal_records[0]
        if (
            terminal.get("candidate_sha") != candidate_sha
            or terminal.get("exit_status") != 0
            or terminal.get("receipt_hash") != terminal_receipt_hash
        ):
            raise AcceptanceCheckError("cleanup_finalize_conflict")
    ledger = _read_gate_ledger(ledger_path)
    ledger_sha256 = _gate_ledger_records_hash(ledger)
    precommit_manifest_sha256 = _sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    final_evidence.update(
        {
            "phase": "committed",
            "precommit_manifest_sha256": precommit_manifest_sha256,
            "committed_at": timestamp,
            "ledger_sha256": ledger_sha256,
        }
    )
    manifest["cleanup_required"] = False
    manifest["updated_at"] = timestamp
    _validate_control_manifest(manifest)
    _write_protected_document(
        manifest_path,
        manifest,
        create=False,
        exists_check="control_manifest_exists",
        write_check="control_manifest_file",
    )
    final_manifest = _read_control_manifest(manifest_path)
    _ensure_final_cleanup_receipt(
        manifest_path,
        final_manifest,
        ledger_sha256=ledger_sha256,
        receipts_sha256=receipts_sha256,
        committed_at=timestamp,
    )
    return final_manifest


def _gate_records_hash(records: object) -> str:
    return _sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _gate_ledger_records_hash(ledger: Mapping[str, object]) -> str:
    return _gate_records_hash(
        {
            "records": ledger["records"],
            "cleanup_records": ledger["cleanup_records"],
        }
    )


def _validate_gate_record_stream(records: object, order: tuple[str, ...]) -> list[dict[str, object]]:
    if not isinstance(records, list) or len(records) > len(order):
        raise AcceptanceCheckError("gate_ledger_schema")
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != _GATE_RECORD_FIELDS:
            raise AcceptanceCheckError("gate_ledger_schema")
        check_id = record["check_id"]
        if type(check_id) is not str or check_id != order[index] or check_id in seen:
            raise AcceptanceCheckError("gate_ledger_schema")
        seen.add(check_id)
        candidate_sha = record["candidate_sha"]
        if type(candidate_sha) is not str or _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None:
            raise AcceptanceCheckError("gate_ledger_schema")
        started = _control_timestamp(record["started_at"])
        ended = _control_timestamp(record["ended_at"])
        if ended < started:
            raise AcceptanceCheckError("gate_ledger_schema")
        exit_status = record["exit_status"]
        receipt_hash = record["receipt_hash"]
        if type(exit_status) is not int or not 0 <= exit_status <= 255:
            raise AcceptanceCheckError("gate_ledger_schema")
        if type(receipt_hash) is not str or _SHA256_PATTERN.fullmatch(receipt_hash) is None:
            raise AcceptanceCheckError("gate_ledger_schema")
    return cast(list[dict[str, object]], records)


def _validate_gate_ledger(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _GATE_LEDGER_FIELDS:
        raise AcceptanceCheckError("gate_ledger_schema")
    if payload["schema"] != "elspeth.aws-ecs-gate-ledger.v4":
        raise AcceptanceCheckError("gate_ledger_schema")
    branch = payload["branch"]
    starting_sha = payload["starting_sha"]
    if type(branch) is not str or re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch) is None or branch.startswith("/"):
        raise AcceptanceCheckError("gate_ledger_schema")
    if type(starting_sha) is not str or _GIT_SHA_PATTERN.fullmatch(starting_sha) is None:
        raise AcceptanceCheckError("gate_ledger_schema")
    for field, pattern in (
        ("plan_sha256", _SHA256_PATTERN),
        ("program_base_sha", _GIT_SHA_PATTERN),
        ("reconciled_release_sha", _GIT_SHA_PATTERN),
    ):
        value = payload[field]
        if type(value) is not str or pattern.fullmatch(value) is None:
            raise AcceptanceCheckError("gate_ledger_schema")
    records = _validate_gate_record_stream(payload["records"], _SUCCESS_GATE_CHECK_ORDER)
    cleanup_records = _validate_gate_record_stream(payload["cleanup_records"], _CLEANUP_GATE_CHECK_ORDER)
    cleanup_start_count = payload["success_record_count_at_cleanup_start"]
    if cleanup_records:
        if type(cleanup_start_count) is not int or cleanup_start_count != len(records):
            raise AcceptanceCheckError("gate_ledger_schema")
    elif cleanup_start_count is not None:
        raise AcceptanceCheckError("gate_ledger_schema")
    bound_candidate = payload["candidate_sha"]
    bound_record_count = payload["candidate_bound_record_count"]
    if (bound_candidate is None) != (bound_record_count is None):
        raise AcceptanceCheckError("gate_ledger_schema")
    if bound_candidate is None and (len(records) > len(_TASK1_GATE_CHECK_ORDER) or cleanup_records):
        raise AcceptanceCheckError("gate_ledger_schema")
    if bound_candidate is not None and (
        type(bound_candidate) is not str
        or _GIT_SHA_PATTERN.fullmatch(bound_candidate) is None
        or type(bound_record_count) is not int
        or bound_record_count != len(_TASK1_GATE_CHECK_ORDER)
        or bound_record_count > len(records)
        or any(record["candidate_sha"] != bound_candidate for record in records[:bound_record_count])
        or any(record["candidate_sha"] != bound_candidate for record in records[bound_record_count:])
        or any(record["candidate_sha"] != bound_candidate for record in cleanup_records)
    ):
        raise AcceptanceCheckError("gate_ledger_schema")
    finalized = payload["finalized"]
    if finalized is not None:
        if not isinstance(finalized, dict) or set(finalized) != {
            "candidate_sha",
            "record_count",
            "records_sha256",
            "finalized_at",
        }:
            raise AcceptanceCheckError("gate_ledger_schema")
        if (
            type(finalized["candidate_sha"]) is not str
            or _GIT_SHA_PATTERN.fullmatch(finalized["candidate_sha"]) is None
            or finalized["candidate_sha"] != bound_candidate
            or finalized["record_count"] != len(records) + len(cleanup_records)
            or finalized["records_sha256"] != _gate_ledger_records_hash(payload)
        ):
            raise AcceptanceCheckError("gate_ledger_schema")
        _control_timestamp(finalized["finalized_at"])
    created = _control_timestamp(payload["created_at"])
    updated = _control_timestamp(payload["updated_at"])
    if updated < created:
        raise AcceptanceCheckError("gate_ledger_schema")
    return payload


def _read_gate_ledger(path: Path) -> dict[str, object]:
    return _validate_gate_ledger(_read_protected_document(path, check="gate_ledger_file"))


def gate_ledger_init(
    path: Path,
    *,
    branch: str,
    starting_sha: str,
    plan_sha256: str,
    program_base_sha: str,
    reconciled_release_sha: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Create a ledger, or accept an exact-owner unfinalized resume."""

    try:
        existing = _read_gate_ledger(path)
    except AcceptanceCheckError as exc:
        if exc.check != "gate_ledger_file" or path.exists():
            raise
    else:
        if (
            existing["branch"] == branch
            and existing["starting_sha"] == starting_sha
            and existing["plan_sha256"] == plan_sha256
            and existing["program_base_sha"] == program_base_sha
            and existing["reconciled_release_sha"] == reconciled_release_sha
            and existing["finalized"] is None
            and not existing["cleanup_records"]
        ):
            return existing
        raise AcceptanceCheckError("gate_ledger_conflict")
    timestamp = _utc_timestamp(now())
    ledger: dict[str, object] = {
        "schema": "elspeth.aws-ecs-gate-ledger.v4",
        "branch": branch,
        "starting_sha": starting_sha,
        "plan_sha256": plan_sha256,
        "program_base_sha": program_base_sha,
        "reconciled_release_sha": reconciled_release_sha,
        "candidate_sha": None,
        "candidate_bound_record_count": None,
        "records": [],
        "cleanup_records": [],
        "success_record_count_at_cleanup_start": None,
        "finalized": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _validate_gate_ledger(ledger)
    _write_protected_document(
        path,
        ledger,
        create=True,
        exists_check="gate_ledger_exists",
        write_check="gate_ledger_file",
        parent_check="gate_ledger_parent",
    )
    return ledger


def gate_ledger_get(path: Path, field: str) -> str:
    """Return one closed, non-secret ledger anchor for resume-safe shell use."""

    if field not in _GATE_LEDGER_GET_FIELDS:
        raise AcceptanceCheckError("gate_ledger_get")
    value = _read_gate_ledger(path)[field]
    if type(value) is not str:
        raise AcceptanceCheckError("gate_ledger_get")
    return value


def gate_ledger_bind_candidate(
    path: Path,
    *,
    candidate_sha: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Freeze the candidate SHA at the exact ledger boundary where it exists."""

    ledger = _read_gate_ledger(path)
    if ledger["finalized"] is not None:
        raise AcceptanceCheckError("gate_ledger_finalized")
    existing = ledger["candidate_sha"]
    if existing is not None:
        if existing == candidate_sha:
            return ledger
        raise AcceptanceCheckError("gate_ledger_conflict")
    if _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None:
        raise AcceptanceCheckError("gate_ledger_schema")
    records = ledger["records"]
    assert isinstance(records, list)
    if [record["check_id"] for record in records] != list(_TASK1_GATE_CHECK_ORDER):
        raise AcceptanceCheckError("gate_ledger_incomplete")
    if any(record["exit_status"] != 0 for record in records):
        raise AcceptanceCheckError("gate_ledger_failed")
    if any(record["candidate_sha"] != candidate_sha for record in records):
        raise AcceptanceCheckError("gate_ledger_candidate")
    timestamp = _utc_timestamp(now())
    ledger["candidate_sha"] = candidate_sha
    ledger["candidate_bound_record_count"] = len(records)
    ledger["updated_at"] = timestamp
    _validate_gate_ledger(ledger)
    _write_protected_document(
        path,
        ledger,
        create=False,
        exists_check="gate_ledger_exists",
        write_check="gate_ledger_file",
        parent_check="gate_ledger_parent",
    )
    return ledger


def _gate_ledger_record_stream(
    path: Path,
    *,
    stream: Literal["records", "cleanup_records"],
    order: tuple[str, ...],
    check_id: str,
    exit_status: int,
    receipt_hash: str,
    candidate_sha: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    ledger = _read_gate_ledger(path)
    if ledger["finalized"] is not None:
        raise AcceptanceCheckError("gate_ledger_finalized")
    records = ledger[stream]
    assert isinstance(records, list)
    bound_candidate = ledger["candidate_sha"]
    if bound_candidate is not None and candidate_sha != bound_candidate:
        raise AcceptanceCheckError("gate_ledger_candidate")
    existing = next(
        (item for item in records if isinstance(item, dict) and item.get("check_id") == check_id),
        None,
    )
    if existing is not None:
        same_evidence = (
            existing.get("candidate_sha") == candidate_sha
            and existing.get("exit_status") == exit_status
            and existing.get("receipt_hash") == receipt_hash
            and (started_at is None or existing.get("started_at") == started_at)
            and (ended_at is None or existing.get("ended_at") == ended_at)
        )
        if same_evidence:
            return ledger
        raise AcceptanceCheckError("gate_ledger_conflict")
    if stream == "records":
        cleanup_records = ledger["cleanup_records"]
        assert isinstance(cleanup_records, list)
        if cleanup_records or (bound_candidate is None and check_id not in _TASK1_GATE_CHECK_ORDER):
            raise AcceptanceCheckError("gate_ledger_phase")
    expected_index = len(records)
    if expected_index >= len(order) or check_id != order[expected_index]:
        raise AcceptanceCheckError("gate_ledger_schema")
    timestamp = _utc_timestamp(now())
    record = {
        "check_id": check_id,
        "candidate_sha": candidate_sha,
        "started_at": started_at or timestamp,
        "ended_at": ended_at or timestamp,
        "exit_status": exit_status,
        "receipt_hash": receipt_hash,
    }
    candidate = {**ledger, stream: [*records, record], "updated_at": timestamp}
    if stream == "cleanup_records" and not records:
        success_records = ledger["records"]
        assert isinstance(success_records, list)
        candidate["success_record_count_at_cleanup_start"] = len(success_records)
    _validate_gate_ledger(candidate)
    _write_protected_document(
        path,
        candidate,
        create=False,
        exists_check="gate_ledger_exists",
        write_check="gate_ledger_file",
        parent_check="gate_ledger_parent",
    )
    return candidate


def gate_ledger_record(
    path: Path,
    *,
    check_id: str,
    exit_status: int,
    receipt_hash: str,
    candidate_sha: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    return _gate_ledger_record_stream(
        path,
        stream="records",
        order=_SUCCESS_GATE_CHECK_ORDER,
        check_id=check_id,
        exit_status=exit_status,
        receipt_hash=receipt_hash,
        candidate_sha=candidate_sha,
        started_at=started_at,
        ended_at=ended_at,
        now=now,
    )


def gate_ledger_record_cleanup(
    path: Path,
    *,
    check_id: str,
    exit_status: int,
    receipt_hash: str,
    candidate_sha: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Record ordered cleanup evidence independently of incomplete success checks."""

    ledger = _read_gate_ledger(path)
    if ledger["candidate_sha"] is None:
        raise AcceptanceCheckError("gate_ledger_candidate")
    return _gate_ledger_record_stream(
        path,
        stream="cleanup_records",
        order=_CLEANUP_GATE_CHECK_ORDER,
        check_id=check_id,
        exit_status=exit_status,
        receipt_hash=receipt_hash,
        candidate_sha=candidate_sha,
        started_at=started_at,
        ended_at=ended_at,
        now=now,
    )


def gate_ledger_finalize(
    path: Path,
    *,
    candidate_sha: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    ledger = _read_gate_ledger(path)
    if ledger["finalized"] is not None:
        finalized = ledger["finalized"]
        if isinstance(finalized, dict) and finalized.get("candidate_sha") == candidate_sha:
            return ledger
        raise AcceptanceCheckError("gate_ledger_conflict")
    records = ledger["records"]
    cleanup_records = ledger["cleanup_records"]
    assert isinstance(records, list)
    if not records:
        raise AcceptanceCheckError("gate_ledger_empty")
    assert isinstance(cleanup_records, list)
    if [record["check_id"] for record in records if isinstance(record, dict)] != list(_SUCCESS_GATE_CHECK_ORDER):
        raise AcceptanceCheckError("gate_ledger_incomplete")
    if [record["check_id"] for record in cleanup_records if isinstance(record, dict)] != list(_CLEANUP_GATE_CHECK_ORDER):
        raise AcceptanceCheckError("gate_ledger_incomplete")
    if ledger["candidate_sha"] != candidate_sha:
        raise AcceptanceCheckError("gate_ledger_candidate")
    bound_record_count = ledger["candidate_bound_record_count"]
    if (
        type(bound_record_count) is not int
        or any(record["candidate_sha"] != candidate_sha for record in records[bound_record_count:])
        or any(record["candidate_sha"] != candidate_sha for record in cleanup_records)
    ):
        raise AcceptanceCheckError("gate_ledger_candidate")
    if any(record["exit_status"] != 0 for record in [*records, *cleanup_records]):
        raise AcceptanceCheckError("gate_ledger_failed")
    timestamp = _utc_timestamp(now())
    ledger["finalized"] = {
        "candidate_sha": candidate_sha,
        "record_count": len(records) + len(cleanup_records),
        "records_sha256": _gate_ledger_records_hash(ledger),
        "finalized_at": timestamp,
    }
    ledger["updated_at"] = timestamp
    _validate_gate_ledger(ledger)
    _write_protected_document(
        path,
        ledger,
        create=False,
        exists_check="gate_ledger_exists",
        write_check="gate_ledger_file",
        parent_check="gate_ledger_parent",
    )
    return ledger


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
    task_definition_policy.add_argument("--expected-user", choices=("1000:1000",))
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
    ):
        commands.add_parser(command)

    sanitize_evidence = commands.add_parser("sanitize-evidence")
    sanitize_evidence.add_argument("--kind", required=True, choices=_EVIDENCE_KINDS)
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
        }:
            with _suppress_process_output():
                if args.command == "verify-s3":
                    details = verify_s3(os.environ)
                elif args.command == "verify-bedrock":
                    details = asyncio.run(verify_bedrock(os.environ))
                elif args.command == "verify-bedrock-guardrails":
                    details = run_bedrock_guardrails_live(os.environ)
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
            _print_json(sanitize_evidence(args.kind, raw_evidence))
        else:
            raise AcceptanceCheckError("command_not_implemented")
    except AcceptanceCheckError as exc:
        _print_error({"error_class": type(exc).__name__, "check": exc.check})
        return 1
    except (AcceptanceHttpError, AcceptanceInputError, AcceptanceStateError, OperatorTelemetryAcceptanceError) as exc:
        _print_error({"error_class": type(exc).__name__})
        return 1
    except Exception:
        _print_error({"error_class": "AcceptanceInternalError"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
