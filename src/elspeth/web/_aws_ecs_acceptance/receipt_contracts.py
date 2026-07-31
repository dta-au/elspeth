"""Pure receipt schemas and binding validation for AWS ECS acceptance."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import cast
from urllib.parse import urlsplit

import httpx

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

from .contracts import (
    _GIT_SHA_PATTERN,
    _MAX_IDENTITY_CHARS,
    _SCENARIO_ID_PATTERN,
    _SHA256_PATTERN,
    MAX_EXEC_RECEIPT_CHARS,
    MAX_EXEC_STREAM_BYTES,
    AcceptanceCheckError,
    SanitizedResourceIdentity,
    _control_timestamp,
    _parse_utc_z_timestamp,
    _sha256,
)

_METRIC_NAME = "operator.acceptance.sentinel"

_TRACE_NAMES = ("RunStarted", "RunFinished")

_EXEC_RECEIPT_PREFIX = "ELSPETH_ACCEPTANCE_RECEIPT_V1:"

_EXEC_RECEIPT_FIELDS = frozenset({"version", "check", "ok", "candidate_sha", "task_arn_sha256", "scenario_id", "details"})

_S3_DETAIL_FIELDS = frozenset({"object_count", "source_sha256", "sink_sha256", "collision_rejected", "cleanup_succeeded"})

_BEDROCK_DETAIL_FIELDS = frozenset(
    {
        "returned_model_sha256",
        "provider_request_id_sha256",
        "prompt_tokens_present",
        "completion_tokens_present",
        "cache_tokens_present",
        "cost",
        "cost_source",
    }
)

_TEXTRACT_DETAIL_FIELDS = frozenset(
    {
        "transform_registered",
        "client_constructed",
        "start_document_analysis_invocable",
        "get_document_analysis_invocable",
    }
)

_GUARDRAIL_DETAIL_FIELDS = frozenset({"controls", "plugin_policy"})

_GUARDRAIL_CONTROL_FIELDS = frozenset(
    {
        "plugin_id",
        "profile_alias",
        "guardrail_version",
        "safe_case_passed",
        "attack_case_blocked",
        "request_ids_present",
        "safe_text_sha256",
        "blocked_text_sha256",
        "checked_at",
    }
)

_PLUGIN_POLICY_DETAIL_FIELDS = frozenset(
    {
        "policy_hash",
        "snapshot_hash",
        "binding_sha256",
        "tutorial_profile_ready",
        "tutorial_ready",
        "tutorial_blocker",
        "tutorial_profile_alias",
        "target_llm",
        "selected_controls",
        "landscape_evidence",
    }
)

_PLUGIN_POLICY_CONTROL_FIELDS = frozenset({"capability", "plugin_id", "profile_alias", "mode"})

_OPERATOR_DETAIL_FIELDS = frozenset(
    {
        "phase",
        "metric_name",
        "trace_names",
        "observed_at",
        "resource",
        "sentinel_sha256",
        "landscape_terminal",
        "trace_terminal_agrees",
        "collector_degraded",
        "cloud_receipt",
        "retained_metric_query",
        "retained_trace_id",
        "forbidden_content_absent",
    }
)

_OPERATOR_RESOURCE_FIELDS = frozenset({"service_name", "service_version", "deployment_environment", "cloud_provider"})

_OPERATOR_METRIC_NAMESPACE = "ELSPETH/Operator"

_OPERATOR_METRIC_DIMENSION_FIELDS = (
    ("service.name", "operator_telemetry_service_name"),
    ("deployment.environment", "operator_telemetry_environment"),
    ("service.version", "operator_telemetry_release"),
    ("aws.ecs.cluster.name", "operator_telemetry_ecs_cluster"),
    ("aws.ecs.service.name", "operator_telemetry_ecs_service"),
    ("aws.ecs.task.family", "operator_telemetry_task_definition_family"),
    ("aws.ecs.task.revision", "operator_telemetry_task_definition_revision"),
)

_CANDIDATE_PACKAGE_VERSION = "0.7.2"

_ROLLBACK_PACKAGE_VERSION = "0.7.1"

_ROLLBACK_BASELINE_SESSION_EPOCH = 35

_ROLLBACK_BASELINE_LANDSCAPE_EPOCH = 29

# Derived from the live epoch constants: a schema bump must rotate the label a
# compatibility receipt attests, otherwise a stale receipt keeps validating
# against a transition the candidate no longer performs.
_SCENARIO_B_STRUCTURAL_CHANGES = (
    f"session_epoch_{_ROLLBACK_BASELINE_SESSION_EPOCH}_to_{SESSION_SCHEMA_EPOCH}"
    f"_landscape_epoch_{_ROLLBACK_BASELINE_LANDSCAPE_EPOCH}_to_{SQLITE_SCHEMA_EPOCH}"
    "_blob_cleanup_guided_decline_and_row_union_barrier"
)


def _expected_schema_facts(scenario_id: str) -> dict[str, object]:
    """Truthful release/schema facts the compatibility authority must attest.

    Candidate facts track the live schema-epoch constants so the validators
    always demand the current build's truth; previous facts are pinned to the
    Scenario B rollback baseline.
    """
    return {
        "candidate": {
            "session_epoch": SESSION_SCHEMA_EPOCH,
            "landscape_epoch": SQLITE_SCHEMA_EPOCH,
            "run_web_plugin_policy_present": True,
        },
        "previous": (
            {
                "session_epoch": _ROLLBACK_BASELINE_SESSION_EPOCH,
                "landscape_epoch": _ROLLBACK_BASELINE_LANDSCAPE_EPOCH,
                "run_web_plugin_policy_present": True,
            }
            if scenario_id == "B"
            else None
        ),
        "structural_changes": (_SCENARIO_B_STRUCTURAL_CHANGES if scenario_id == "B" else "initial_create"),
        "semantics_only_changes": ("guided_coalesce_timeout_seconds_required" if scenario_id == "B" else "none"),
        "archive_export_decision": ("required_before_forward_migration" if scenario_id == "B" else "not_applicable"),
        "destructive_reset_required": False,
    }


def _validate_s3_receipt_details(details: Mapping[str, object]) -> None:
    if set(details) != _S3_DETAIL_FIELDS:
        raise AcceptanceCheckError("exec_receipt_schema")
    object_count = details["object_count"]
    source_sha256 = details["source_sha256"]
    sink_sha256 = details["sink_sha256"]
    if type(object_count) is not int or object_count != 1:
        raise AcceptanceCheckError("exec_receipt_schema")
    if type(source_sha256) is not str or _SHA256_PATTERN.fullmatch(source_sha256) is None:
        raise AcceptanceCheckError("exec_receipt_schema")
    if type(sink_sha256) is not str or sink_sha256 != source_sha256:
        raise AcceptanceCheckError("exec_receipt_schema")
    if details["collision_rejected"] is not True or details["cleanup_succeeded"] is not True:
        raise AcceptanceCheckError("exec_receipt_schema")


def _validate_bedrock_receipt_details(details: Mapping[str, object]) -> None:
    if set(details) != _BEDROCK_DETAIL_FIELDS:
        raise AcceptanceCheckError("exec_receipt_schema")
    for field in ("returned_model_sha256", "provider_request_id_sha256"):
        value = details[field]
        if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
            raise AcceptanceCheckError("exec_receipt_schema")
    for field in ("prompt_tokens_present", "completion_tokens_present", "cache_tokens_present"):
        if type(details[field]) is not bool:
            raise AcceptanceCheckError("exec_receipt_schema")
    cost = details["cost"]
    if cost is not None:
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            raise AcceptanceCheckError("exec_receipt_schema")
        if not math.isfinite(cost) or cost < 0:
            raise AcceptanceCheckError("exec_receipt_schema")
    if details["cost_source"] not in {"provider_reported", "litellm_calculated", "unavailable"}:
        raise AcceptanceCheckError("exec_receipt_schema")
    if (cost is None) != (details["cost_source"] == "unavailable"):
        raise AcceptanceCheckError("exec_receipt_schema")


def _validate_textract_receipt_details(details: Mapping[str, object]) -> None:
    if set(details) != _TEXTRACT_DETAIL_FIELDS:
        raise AcceptanceCheckError("exec_receipt_schema")
    if any(details[field] is not True for field in _TEXTRACT_DETAIL_FIELDS):
        raise AcceptanceCheckError("exec_receipt_schema")


def _validate_guardrail_receipt_details(details: Mapping[str, object]) -> None:
    if set(details) != _GUARDRAIL_DETAIL_FIELDS:
        raise AcceptanceCheckError("exec_receipt_schema")
    controls = details["controls"]
    if not isinstance(controls, list) or len(controls) != 2:
        raise AcceptanceCheckError("exec_receipt_schema")
    expected_plugins = ("aws_bedrock_prompt_shield", "aws_bedrock_content_safety")
    validated_aliases: list[str] = []
    for control, expected_plugin in zip(controls, expected_plugins, strict=True):
        if not isinstance(control, Mapping) or set(control) != _GUARDRAIL_CONTROL_FIELDS:
            raise AcceptanceCheckError("exec_receipt_schema")
        if control["plugin_id"] != expected_plugin:
            raise AcceptanceCheckError("exec_receipt_schema")
        alias = control["profile_alias"]
        if (
            type(alias) is not str
            or not alias
            or alias != alias.strip()
            or len(alias) > _MAX_IDENTITY_CHARS
            or any(ord(character) < 32 or ord(character) == 127 for character in alias)
        ):
            raise AcceptanceCheckError("exec_receipt_schema")
        validated_aliases.append(alias)
        guardrail_version = control["guardrail_version"]
        if type(guardrail_version) is not str or re.fullmatch(r"[1-9][0-9]*", guardrail_version) is None:
            raise AcceptanceCheckError("exec_receipt_schema")
        for field in ("safe_case_passed", "attack_case_blocked", "request_ids_present"):
            if control[field] is not True:
                raise AcceptanceCheckError("exec_receipt_schema")
        for field in ("safe_text_sha256", "blocked_text_sha256"):
            value = control[field]
            if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
                raise AcceptanceCheckError("exec_receipt_schema")
        checked_at = control["checked_at"]
        if type(checked_at) is not str:
            raise AcceptanceCheckError("exec_receipt_schema")
        try:
            _parse_utc_z_timestamp(checked_at)
        except ValueError:
            raise AcceptanceCheckError("exec_receipt_schema") from None

    plugin_policy = details["plugin_policy"]
    if not isinstance(plugin_policy, Mapping) or set(plugin_policy) != _PLUGIN_POLICY_DETAIL_FIELDS:
        raise AcceptanceCheckError("exec_receipt_schema")
    for field in ("policy_hash", "snapshot_hash", "binding_sha256"):
        value = plugin_policy[field]
        if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
            raise AcceptanceCheckError("exec_receipt_schema")
    if (
        plugin_policy["tutorial_profile_ready"] is not True
        or plugin_policy["tutorial_ready"] is not False
        or plugin_policy["tutorial_blocker"] != "tutorial_required_control_coverage"
        or plugin_policy["landscape_evidence"] is not True
        or plugin_policy["target_llm"] != "transform:llm"
    ):
        raise AcceptanceCheckError("exec_receipt_schema")
    tutorial_alias = plugin_policy["tutorial_profile_alias"]
    if (
        type(tutorial_alias) is not str
        or not tutorial_alias
        or tutorial_alias != tutorial_alias.strip()
        or len(tutorial_alias) > _MAX_IDENTITY_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in tutorial_alias)
    ):
        raise AcceptanceCheckError("exec_receipt_schema")
    selected_controls = plugin_policy["selected_controls"]
    expected_controls = (
        ("prompt_shield", "transform:aws_bedrock_prompt_shield", validated_aliases[0]),
        ("content_safety", "transform:aws_bedrock_content_safety", validated_aliases[1]),
    )
    if not isinstance(selected_controls, list) or len(selected_controls) != len(expected_controls):
        raise AcceptanceCheckError("exec_receipt_schema")
    for selected, (capability, plugin_id, profile_alias) in zip(selected_controls, expected_controls, strict=True):
        if (
            not isinstance(selected, Mapping)
            or set(selected) != _PLUGIN_POLICY_CONTROL_FIELDS
            or selected["capability"] != capability
            or selected["plugin_id"] != plugin_id
            or selected["profile_alias"] != profile_alias
            or selected["mode"] != "required"
        ):
            raise AcceptanceCheckError("exec_receipt_schema")


def _validate_operator_receipt_details(details: Mapping[str, object]) -> None:
    if set(details) != _OPERATOR_DETAIL_FIELDS:
        raise AcceptanceCheckError("exec_receipt_schema")
    phase = details["phase"]
    if phase not in {"positive", "outage"}:
        raise AcceptanceCheckError("exec_receipt_schema")
    if details["metric_name"] != _METRIC_NAME or details["trace_names"] != list(_TRACE_NAMES):
        raise AcceptanceCheckError("exec_receipt_schema")
    observed_at = details["observed_at"]
    if (
        not isinstance(observed_at, (int, float))
        or isinstance(observed_at, bool)
        or not math.isfinite(float(observed_at))
        or observed_at < 0
    ):
        raise AcceptanceCheckError("exec_receipt_schema")
    resource = details["resource"]
    if not isinstance(resource, Mapping) or set(resource) != _OPERATOR_RESOURCE_FIELDS:
        raise AcceptanceCheckError("exec_receipt_schema")
    try:
        SanitizedResourceIdentity(
            service_name=resource["service_name"],
            service_version=resource["service_version"],
            deployment_environment=resource["deployment_environment"],
            cloud_provider=resource["cloud_provider"],
        )
    except (TypeError, ValueError):
        raise AcceptanceCheckError("exec_receipt_schema") from None
    sentinel_sha256 = details["sentinel_sha256"]
    if type(sentinel_sha256) is not str or _SHA256_PATTERN.fullmatch(sentinel_sha256) is None:
        raise AcceptanceCheckError("exec_receipt_schema")
    if details["landscape_terminal"] is not True or details["forbidden_content_absent"] is not True:
        raise AcceptanceCheckError("exec_receipt_schema")
    retained_metric_query = details["retained_metric_query"]
    retained_trace_id = details["retained_trace_id"]
    if phase == "positive":
        if (
            details["trace_terminal_agrees"] is not True
            or details["collector_degraded"] is not False
            or details["cloud_receipt"] is not True
        ):
            raise AcceptanceCheckError("exec_receipt_schema")
        if (
            not isinstance(retained_metric_query, dict)
            or set(retained_metric_query) != {"namespace", "metric_name", "dimensions"}
            or retained_metric_query["namespace"] != _OPERATOR_METRIC_NAMESPACE
            or retained_metric_query["metric_name"] != _METRIC_NAME
            or not isinstance(retained_metric_query["dimensions"], list)
            or not 1 <= len(retained_metric_query["dimensions"]) <= 30
            or type(retained_trace_id) is not str
            or re.fullmatch(r"1-[0-9a-f]{8}-[0-9a-f]{24}", retained_trace_id) is None
        ):
            raise AcceptanceCheckError("exec_receipt_schema")
        seen_dimensions: set[str] = set()
        for dimension in retained_metric_query["dimensions"]:
            if not isinstance(dimension, dict) or set(dimension) != {"name", "value"}:
                raise AcceptanceCheckError("exec_receipt_schema")
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
                raise AcceptanceCheckError("exec_receipt_schema")
            seen_dimensions.add(name)
        expected_dimensions = {
            *(name for name, _field in _OPERATOR_METRIC_DIMENSION_FIELDS),
            "cloud.provider",
            "elspeth.acceptance.namespace",
            "elspeth.acceptance.sentinel",
        }
        dimensions_by_name = {dimension["name"]: dimension["value"] for dimension in retained_metric_query["dimensions"]}
        namespace = dimensions_by_name.get("elspeth.acceptance.namespace")
        if (
            seen_dimensions != expected_dimensions
            or dimensions_by_name.get("service.name") != resource["service_name"]
            or dimensions_by_name.get("deployment.environment") != resource["deployment_environment"]
            or dimensions_by_name.get("service.version") != resource["service_version"]
            or dimensions_by_name.get("cloud.provider") != resource["cloud_provider"]
            or type(namespace) is not str
            or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}-[ab]", namespace) is None
            or dimensions_by_name.get("elspeth.acceptance.sentinel") != str(int(sentinel_sha256[:12], 16))
        ):
            raise AcceptanceCheckError("exec_receipt_schema")
    elif (
        details["trace_terminal_agrees"] is not None
        or details["collector_degraded"] is not True
        or details["cloud_receipt"] is not False
        or retained_metric_query is not None
        or retained_trace_id is not None
    ):
        raise AcceptanceCheckError("exec_receipt_schema")


def _validate_exec_receipt_schema(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _EXEC_RECEIPT_FIELDS:
        raise AcceptanceCheckError("exec_receipt_schema")
    if payload["version"] != 1 or type(payload["version"]) is not int or payload["ok"] is not True:
        raise AcceptanceCheckError("exec_receipt")
    check = payload["check"]
    candidate_sha = payload["candidate_sha"]
    task_arn_sha256 = payload["task_arn_sha256"]
    scenario_id = payload["scenario_id"]
    details = payload["details"]
    if type(check) is not str or check not in {
        "verify-s3",
        "verify-bedrock",
        "verify-textract",
        "verify-bedrock-guardrails",
        "verify-connection-budget",
        "verify-operator-telemetry",
    }:
        raise AcceptanceCheckError("exec_receipt_schema")
    if type(candidate_sha) is not str or _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None:
        raise AcceptanceCheckError("exec_receipt_schema")
    if type(task_arn_sha256) is not str or _SHA256_PATTERN.fullmatch(task_arn_sha256) is None:
        raise AcceptanceCheckError("exec_receipt_schema")
    if type(scenario_id) is not str or _SCENARIO_ID_PATTERN.fullmatch(scenario_id) is None:
        raise AcceptanceCheckError("exec_receipt_schema")
    if not isinstance(details, dict):
        raise AcceptanceCheckError("exec_receipt_schema")
    if check == "verify-s3":
        _validate_s3_receipt_details(details)
    elif check == "verify-bedrock":
        _validate_bedrock_receipt_details(details)
    elif check == "verify-bedrock-guardrails":
        _validate_guardrail_receipt_details(details)
    elif check == "verify-connection-budget":
        _validate_connection_budget_receipt(details)
    elif check == "verify-textract":
        _validate_textract_receipt_details(details)
    else:
        _validate_operator_receipt_details(details)
    return payload


def resolve_exec_receipt_env(
    env: Mapping[str, str],
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, str]:
    """Resolve the current ECS task ARN from the task-local v4 metadata endpoint."""

    resolved = dict(env)
    if "ELSPETH_ACCEPTANCE_TASK_ARN" in resolved:
        raise AcceptanceCheckError("exec_receipt_binding")
    metadata_uri = env.get("ECS_CONTAINER_METADATA_URI_V4", "")
    try:
        parsed = urlsplit(metadata_uri)
        valid = bool(
            parsed.scheme == "http"
            and parsed.hostname == "169.254.170.2"
            and parsed.port in {None, 80}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(r"/v4/[A-Za-z0-9_-]{1,512}", parsed.path)
        )
    except ValueError:
        valid = False
    if not valid:
        raise AcceptanceCheckError("exec_receipt_binding")
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(2.0),
            transport=transport,
            trust_env=False,
        ) as client:
            response = client.get(f"{metadata_uri}/task")
            content = response.content
            if response.status_code != 200 or len(content) > 64 * 1024:
                raise AcceptanceCheckError("exec_receipt_binding")
        payload = json.loads(content)
    except AcceptanceCheckError:
        raise
    except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError):
        raise AcceptanceCheckError("exec_receipt_binding") from None
    task_arn = payload.get("TaskARN") if isinstance(payload, Mapping) else None
    if (
        type(task_arn) is not str
        or len(task_arn) > 2048
        or re.fullmatch(r"arn:aws(?:-us-gov|-cn)?:ecs:[a-z0-9-]+:[0-9]{12}:task/[A-Za-z0-9/_-]+", task_arn) is None
    ):
        raise AcceptanceCheckError("exec_receipt_binding")
    resolved["ELSPETH_ACCEPTANCE_TASK_ARN"] = task_arn
    return resolved


def encode_exec_receipt(check: str, details: Mapping[str, object], env: Mapping[str, str]) -> str:
    """Encode one closed in-task receipt without exposing its task ARN."""

    candidate_sha = env.get("ELSPETH_ACCEPTANCE_CANDIDATE_SHA", "")
    task_arn = env.get("ELSPETH_ACCEPTANCE_TASK_ARN", "")
    scenario_id = env.get("ELSPETH_ACCEPTANCE_SCENARIO_ID", "")
    if _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None:
        raise AcceptanceCheckError("exec_receipt_binding")
    if not task_arn.startswith("arn:aws") or len(task_arn) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in task_arn):
        raise AcceptanceCheckError("exec_receipt_binding")
    if _SCENARIO_ID_PATTERN.fullmatch(scenario_id) is None:
        raise AcceptanceCheckError("exec_receipt_binding")
    payload = _validate_exec_receipt_schema(
        {
            "version": 1,
            "check": check,
            "ok": True,
            "candidate_sha": candidate_sha,
            "task_arn_sha256": _sha256(task_arn.encode("utf-8")),
            "scenario_id": scenario_id,
            "details": dict(details),
        }
    )
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
    )
    if len(encoded) > MAX_EXEC_RECEIPT_CHARS:
        raise AcceptanceCheckError("exec_receipt")
    return f"{_EXEC_RECEIPT_PREFIX}{encoded}"


def extract_exec_receipt(
    stream: str,
    *,
    expected_candidate_sha: str,
    expected_task_arn: str,
    expected_scenario_id: str,
    expected_check: str,
    expected_plugin_policy_binding_sha256: str | None = None,
) -> dict[str, object]:
    """Extract and bind exactly one closed receipt from Session Manager output."""

    if len(stream.encode("utf-8")) > MAX_EXEC_STREAM_BYTES:
        raise AcceptanceCheckError("exec_receipt")
    receipt_lines = [line for line in stream.splitlines() if line.startswith(_EXEC_RECEIPT_PREFIX)]
    if len(receipt_lines) != 1:
        raise AcceptanceCheckError("exec_receipt")
    encoded = receipt_lines[0][len(_EXEC_RECEIPT_PREFIX) :]
    if not encoded or len(encoded) > MAX_EXEC_RECEIPT_CHARS or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        raise AcceptanceCheckError("exec_receipt")
    try:
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(f"{encoded}{padding}", altchars=b"-_", validate=True)
        payload = _validate_exec_receipt_schema(json.loads(decoded))
    except AcceptanceCheckError:
        raise
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        raise AcceptanceCheckError("exec_receipt") from None

    if payload["candidate_sha"] != expected_candidate_sha:
        raise AcceptanceCheckError("candidate_binding")
    if payload["task_arn_sha256"] != _sha256(expected_task_arn.encode("utf-8")):
        raise AcceptanceCheckError("task_binding")
    if payload["scenario_id"] != expected_scenario_id:
        raise AcceptanceCheckError("scenario_binding")
    if payload["check"] != expected_check:
        raise AcceptanceCheckError("check_binding")
    if expected_plugin_policy_binding_sha256 is not None:
        if expected_check != "verify-bedrock-guardrails" or _SHA256_PATTERN.fullmatch(expected_plugin_policy_binding_sha256) is None:
            raise AcceptanceCheckError("plugin_policy_binding")
        details = cast(dict[str, object], payload["details"])
        plugin_policy = cast(Mapping[str, object], details["plugin_policy"])
        if plugin_policy["binding_sha256"] != expected_plugin_policy_binding_sha256:
            raise AcceptanceCheckError("plugin_policy_binding")
    return payload


_FORBIDDEN_RECEIPT_KEYS = frozenset(
    {
        "password",
        "credential",
        "credentials",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "command",
        "environment",
        "provider_response",
        "raw_response",
        "raw_output",
        "message",
        "exception_text",
        "headers",
        "cookies",
        "username",
    }
)


def _validate_bounded_receipt_document(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise AcceptanceCheckError("receipt_store_schema")
    remaining = 4096

    def visit(value: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 8:
            raise AcceptanceCheckError("receipt_store_schema")
        if isinstance(value, dict):
            if len(value) > 256:
                raise AcceptanceCheckError("receipt_store_schema")
            for key, child in value.items():
                if (
                    type(key) is not str
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", key) is None
                    or key.lower() in _FORBIDDEN_RECEIPT_KEYS
                    or key.lower().endswith("_raw")
                ):
                    raise AcceptanceCheckError("receipt_store_schema")
                visit(child, depth + 1)
        elif isinstance(value, list):
            if len(value) > 1024:
                raise AcceptanceCheckError("receipt_store_schema")
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, str):
            if len(value) > 16 * 1024 or any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise AcceptanceCheckError("receipt_store_schema")
        elif (value is not None and not isinstance(value, (bool, int, float))) or (isinstance(value, float) and not math.isfinite(value)):
            raise AcceptanceCheckError("receipt_store_schema")

    visit(payload, 0)
    return payload


def _receipt_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
        raise AcceptanceCheckError("receipt_store_schema")
    return float(value)


def _receipt_nonnegative_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise AcceptanceCheckError("receipt_store_schema")
    return value


def _validate_connection_budget_receipt(
    payload: object,
    *,
    expected_acceptance_run_id_sha256: str | None = None,
    expected_cluster_id_sha256: str | None = None,
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "acceptance_run_id_sha256",
        "cluster_id_sha256",
        "window_start",
        "window_end",
        "period_seconds",
        "expected_points",
        "points",
        "high_water",
        "max_connections",
        "approved_budget",
        "safety_margin",
        "ok",
    }:
        raise AcceptanceCheckError("receipt_store_schema")
    if (
        payload["schema"] != "elspeth.rds-connection-budget.v3"
        or type(payload["acceptance_run_id_sha256"]) is not str
        or _SHA256_PATTERN.fullmatch(payload["acceptance_run_id_sha256"]) is None
        or type(payload["cluster_id_sha256"]) is not str
        or _SHA256_PATTERN.fullmatch(payload["cluster_id_sha256"]) is None
    ):
        raise AcceptanceCheckError("receipt_store_schema")
    if (expected_acceptance_run_id_sha256 is not None and payload["acceptance_run_id_sha256"] != expected_acceptance_run_id_sha256) or (
        expected_cluster_id_sha256 is not None and payload["cluster_id_sha256"] != expected_cluster_id_sha256
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    points = payload["points"]
    window_start = _control_timestamp(payload["window_start"])
    window_end = _control_timestamp(payload["window_end"])
    if (
        payload["period_seconds"] != 60
        or payload["expected_points"] != 10
        or window_end - window_start != timedelta(minutes=10)
        or window_start.second != 0
        or window_start.microsecond != 0
    ):
        raise AcceptanceCheckError("receipt_store_schema")
    expected_timestamps = [window_start + timedelta(minutes=offset) for offset in range(10)]
    if not isinstance(points, list) or len(points) != len(expected_timestamps):
        raise AcceptanceCheckError("receipt_store_schema")
    counts: list[float] = []
    observed_timestamps: list[datetime] = []
    for point in points:
        if not isinstance(point, dict) or set(point) != {"timestamp", "count"}:
            raise AcceptanceCheckError("receipt_store_schema")
        observed_timestamps.append(_control_timestamp(point["timestamp"]))
        counts.append(_receipt_number(point["count"]))
    if observed_timestamps != expected_timestamps or len(set(observed_timestamps)) != len(observed_timestamps):
        raise AcceptanceCheckError("receipt_store_schema")
    high_water = _receipt_number(payload["high_water"])
    maximum = _receipt_nonnegative_integer(payload["max_connections"])
    budget = _receipt_nonnegative_integer(payload["approved_budget"])
    margin = _receipt_nonnegative_integer(payload["safety_margin"])
    if (
        payload["ok"] is not True
        or high_water != max(counts)
        or high_water > budget
        or budget > maximum - margin
        or maximum - high_water < margin
    ):
        raise AcceptanceCheckError("receipt_store_schema")
    return payload


def _validate_terraform_receipt(
    payload: object,
    *,
    kind: str,
    subject_sha256: str,
    subject_id: str | None,
) -> dict[str, object]:
    if subject_id is not None and _SHA256_PATTERN.fullmatch(subject_id) is None:
        raise AcceptanceCheckError("receipt_store_binding")
    if not isinstance(payload, dict) or set(payload) != {"schema", "kind", "plan_sha256", "projection"}:
        raise AcceptanceCheckError("receipt_store_schema")
    expected_kind = "terraform-destroy-plan" if kind == "terraform-destroy-plan" else "terraform-plan"
    plan_sha256 = payload["plan_sha256"]
    if (
        payload["schema"] != "elspeth.aws-ecs-sanitized-evidence.v2"
        or payload["kind"] != expected_kind
        or type(plan_sha256) is not str
        or _SHA256_PATTERN.fullmatch(plan_sha256) is None
    ):
        raise AcceptanceCheckError("receipt_store_schema")
    if _sha256(plan_sha256.encode("utf-8")) != subject_sha256 or (subject_id is not None and plan_sha256 != subject_id):
        raise AcceptanceCheckError("receipt_store_binding")
    projection = payload["projection"]
    fields = {
        "resource_change_count",
        "create_count",
        "update_count",
        "delete_count",
        "replace_count",
        "no_op_count",
        "has_delete",
        "has_replace",
    }
    if not isinstance(projection, dict) or set(projection) != fields:
        raise AcceptanceCheckError("receipt_store_schema")
    count_names = fields - {"has_delete", "has_replace"}
    if any(type(projection[name]) is not int or not 0 <= projection[name] <= 100_000 for name in count_names):
        raise AcceptanceCheckError("receipt_store_schema")
    if (
        type(projection["has_delete"]) is not bool
        or type(projection["has_replace"]) is not bool
        or projection["has_delete"] != (projection["delete_count"] > 0)
        or projection["has_replace"] != (projection["replace_count"] > 0)
        or sum(projection[name] for name in count_names - {"resource_change_count"}) > projection["resource_change_count"]
    ):
        raise AcceptanceCheckError("receipt_store_schema")
    if kind == "terraform-noop" and any(
        projection[name] != 0 for name in ("create_count", "update_count", "delete_count", "replace_count")
    ):
        raise AcceptanceCheckError("receipt_store_schema")
    return payload


def _validate_event_canary_receipt(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or payload != {
        "schema": "elspeth.aws-ecs-event-canary.v1",
        "delivered": True,
        "removed": True,
    }:
        raise AcceptanceCheckError("receipt_store_schema")
    return payload


def _validate_compatibility_receipt(
    payload: object,
    *,
    scenario_id: str,
    candidate_sha: str,
    subject_id: str | None,
) -> dict[str, object]:
    fields = {
        "schema",
        "record_sha256",
        "acceptance_run_id_sha256",
        "scenario_id",
        "candidate_sha",
        "candidate_image_digest",
        "candidate_task_definition_sha256",
        "candidate_doctor_task_definition_sha256",
        "candidate_package_version",
        "previous_source_sha",
        "previous_image_digest",
        "previous_task_definition_sha256",
        "rollback_doctor_task_definition_sha256",
        "previous_package_version",
        "schema_facts",
        "forward_compatible",
        "backward_compatible",
        "rollback_permitted",
        "decision",
        "approvals_present",
        "expires_at",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise AcceptanceCheckError("receipt_store_schema")
    if (
        subject_id is None
        or _SHA256_PATTERN.fullmatch(subject_id) is None
        or payload["schema"] != "elspeth.aws-ecs-compatibility-receipt.v2"
        or payload["record_sha256"] != subject_id
        or payload["scenario_id"] != scenario_id
        or payload["candidate_sha"] != candidate_sha
        or type(payload["candidate_image_digest"]) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", payload["candidate_image_digest"]) is None
        or payload["candidate_package_version"] != _CANDIDATE_PACKAGE_VERSION
        or payload["forward_compatible"] is not True
        or payload["decision"] != "approved"
        or payload["approvals_present"] is not True
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    for field in (
        "record_sha256",
        "acceptance_run_id_sha256",
        "candidate_task_definition_sha256",
        "candidate_doctor_task_definition_sha256",
    ):
        if type(payload[field]) is not str or _SHA256_PATTERN.fullmatch(payload[field]) is None:
            raise AcceptanceCheckError("receipt_store_schema")
    previous_hash = payload["previous_task_definition_sha256"]
    rollback_doctor_hash = payload["rollback_doctor_task_definition_sha256"]
    previous_source_sha = payload["previous_source_sha"]
    previous_image_digest = payload["previous_image_digest"]
    if (scenario_id == "B") != (
        type(previous_hash) is str
        and _SHA256_PATTERN.fullmatch(previous_hash) is not None
        and type(rollback_doctor_hash) is str
        and _SHA256_PATTERN.fullmatch(rollback_doctor_hash) is not None
        and type(previous_source_sha) is str
        and re.fullmatch(r"[0-9a-f]{40}", previous_source_sha) is not None
        and type(previous_image_digest) is str
        and re.fullmatch(r"sha256:[0-9a-f]{64}", previous_image_digest) is not None
        and payload["previous_package_version"] == _ROLLBACK_PACKAGE_VERSION
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    if scenario_id == "A" and any(
        payload[field] is not None
        for field in (
            "previous_source_sha",
            "previous_image_digest",
            "previous_task_definition_sha256",
            "rollback_doctor_task_definition_sha256",
            "previous_package_version",
        )
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    if (
        payload["backward_compatible"] is not False
        or payload["rollback_permitted"] is not False
        or any(type(payload[field]) is not bool for field in ("backward_compatible", "rollback_permitted"))
    ):
        raise AcceptanceCheckError("receipt_store_schema")
    expected_schema_facts = _expected_schema_facts(scenario_id)
    if payload["schema_facts"] != expected_schema_facts:
        raise AcceptanceCheckError("receipt_store_binding")
    _control_timestamp(payload["expires_at"])
    return payload


_RECEIPT_KINDS = frozenset(
    {
        "connection-budget",
        "compatibility-record",
        "deployment-event-canary",
        "terraform-plan",
        "terraform-noop",
        "terraform-destroy-plan",
        "verify-s3",
        "verify-bedrock",
        "verify-textract",
        "verify-bedrock-guardrails",
        "verify-operator-telemetry",
    }
)

_TERRAFORM_RECEIPT_KINDS = frozenset({"terraform-plan", "terraform-noop", "terraform-destroy-plan"})


def _validate_stored_receipt(
    payload: object,
    *,
    kind: str,
    scenario_id: str,
    subject_sha256: str,
    candidate_sha: str,
    subject_id: str | None = None,
    expected_plugin_policy_binding_sha256: str | None = None,
    expected_acceptance_run_id_sha256: str | None = None,
    expected_cluster_id_sha256: str | None = None,
) -> dict[str, object]:
    document = _validate_bounded_receipt_document(payload)
    if kind == "connection-budget":
        receipt = _validate_exec_receipt_schema(document)
        if (
            receipt["check"] != "verify-connection-budget"
            or receipt["scenario_id"] != scenario_id
            or receipt["candidate_sha"] != candidate_sha
            or receipt["task_arn_sha256"] != subject_sha256
        ):
            raise AcceptanceCheckError("receipt_store_binding")
        _validate_connection_budget_receipt(
            receipt["details"],
            expected_acceptance_run_id_sha256=expected_acceptance_run_id_sha256,
            expected_cluster_id_sha256=expected_cluster_id_sha256,
        )
        return receipt
    if kind == "compatibility-record":
        return _validate_compatibility_receipt(
            document,
            scenario_id=scenario_id,
            candidate_sha=candidate_sha,
            subject_id=subject_id,
        )
    if kind == "deployment-event-canary":
        return _validate_event_canary_receipt(document)
    if kind in {"terraform-plan", "terraform-noop", "terraform-destroy-plan"}:
        return _validate_terraform_receipt(
            document,
            kind=kind,
            subject_sha256=subject_sha256,
            subject_id=subject_id,
        )
    if kind not in _RECEIPT_KINDS:
        raise AcceptanceCheckError("receipt_store_schema")
    receipt = _validate_exec_receipt_schema(document)
    if (
        receipt["check"] != kind
        or receipt["scenario_id"] != scenario_id
        or receipt["candidate_sha"] != candidate_sha
        or receipt["task_arn_sha256"] != subject_sha256
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    if expected_plugin_policy_binding_sha256 is not None:
        details = cast(dict[str, object], receipt["details"])
        plugin_policy = cast(Mapping[str, object], details["plugin_policy"])
        if (
            kind != "verify-bedrock-guardrails"
            or _SHA256_PATTERN.fullmatch(expected_plugin_policy_binding_sha256) is None
            or plugin_policy["binding_sha256"] != expected_plugin_policy_binding_sha256
        ):
            raise AcceptanceCheckError("receipt_store_binding")
    return receipt
