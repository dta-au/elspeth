"""Validation for AWS ECS acceptance scenario inventories."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Literal, cast
from urllib.parse import urlsplit

from elspeth.core.llm_profiles import LLMProfileSettings, validate_profile_alias
from elspeth.plugins.transforms.aws.guardrail_profiles import BedrockGuardrailProfileSettings
from elspeth.web.composer.provider_config import infer_provider_from_model_name, infer_provider_from_unprefixed_model_name

from .contracts import (
    _SHA256_PATTERN,
    SCENARIO_VALUE_FIELDS,
    AcceptanceCheckError,
    _sha256,
    _task_definition_family,
    plugin_policy_binding_sha256,
    scenario_resource_namespace,
)
from .contracts import (
    PLUGIN_POLICY_ASSIGNMENT_NAMES as PLUGIN_POLICY_ASSIGNMENT_NAMES,
)
from .contracts import (
    SCENARIO_ASSIGNMENT_NAMES as SCENARIO_ASSIGNMENT_NAMES,
)
from .secure_documents import _read_protected_document

_TASK_DEFINITION_COMPOSER_MODEL_ENV = (
    "ELSPETH_WEB__COMPOSER_MODEL",
    "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL",
)
_DIGEST_PINNED_CONTAINER_IMAGE_PATTERN = re.compile(
    r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[1-9][0-9]{0,4})?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+@sha256:[0-9a-f]{64}\Z"
)

ORPHAN_INVENTORY_FIELDS = frozenset(
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
_ORPHAN_INVENTORY_FIELDS = ORPHAN_INVENTORY_FIELDS
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


def _scenario_inventory_hash(inventory: Mapping[str, object]) -> str:
    return _sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _validate_gateway_profiles(values: Mapping[str, object]) -> None:
    try:
        profile_payload = json.loads(cast(str, values["ELSPETH_WEB__LLM_PROFILES"]))
    except json.JSONDecodeError:
        raise AcceptanceCheckError("scenario_inventory_schema") from None
    if type(profile_payload) is not dict or not profile_payload:
        raise AcceptanceCheckError("scenario_inventory_schema")
    admitted_profiles: dict[str, LLMProfileSettings] = {}
    try:
        for alias, profile in profile_payload.items():
            if type(alias) is not str or type(profile) is not dict:
                raise AcceptanceCheckError("scenario_inventory_schema")
            admitted_profiles[validate_profile_alias(alias)] = LLMProfileSettings.model_validate(profile)
        default_alias = validate_profile_alias(cast(str, values["ELSPETH_WEB__DEFAULT_LLM_PROFILE"]))
    except (TypeError, ValueError):
        raise AcceptanceCheckError("scenario_inventory_schema") from None
    if default_alias not in admitted_profiles:
        raise AcceptanceCheckError("scenario_inventory_schema")
    if any(
        profile.provider != "gateway"
        or profile.model.startswith("bedrock/")
        or profile.endpoint != "http://127.0.0.1:8787/v1"
        or profile.credential_scope != "server"
        or profile.credential_ref != "GATEWAY_BEARER"
        or profile.contract_major != 1
        for profile in admitted_profiles.values()
    ):
        raise AcceptanceCheckError("scenario_inventory_schema")


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
    event_rules = cast(list[dict[str, object]], orphan["event_rules"])
    if event_rule and not any(rule["rule_name"] == event_rule for rule in event_rules):
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
        and not any(rule["rule_name"] == event_rule and event_target in cast(list[object], rule["target_ids"]) for rule in event_rules)
    ):
        raise AcceptanceCheckError("scenario_inventory_binding")
    if scenario_id in {"A", "C"}:
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
    if values["DEPLOYMENT_MODE"] != ("upgrade" if scenario_id == "B" else "first"):
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

    if scenario_id in {"A", "C"}:
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
        try:
            authorization_origin = urlsplit(cast(str, values["OIDC_EXPECTED_AUTHORIZATION_ORIGIN"]))
            authorization_port = authorization_origin.port
        except ValueError:
            raise AcceptanceCheckError("scenario_inventory_schema") from None
        if (
            authorization_origin.scheme != "https"
            or not authorization_origin.hostname
            or authorization_origin.username is not None
            or authorization_origin.password is not None
            or authorization_port not in {None, 443}
            or authorization_origin.path not in {"", "/"}
            or authorization_origin.query
            or authorization_origin.fragment
            or authorization_origin.hostname != f"{namespace}.auth.{aws_region}.amazoncognito.com"
            or values["OIDC_EXPECTED_AUTHORIZATION_ORIGIN"] != f"https://{namespace}.auth.{aws_region}.amazoncognito.com"
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
        "gateway_image",
        "gateway_sha",
        "gateway_adapter",
        "gateway_adapter_version",
        "gateway_adapter_api_major",
        "gateway_adapter_fingerprint",
        "aws_account_id",
        "aws_region",
        "scenario_id",
        "phase",
        "values",
        "orphan_sweep",
    }:
        raise AcceptanceCheckError("scenario_inventory_schema")
    if (
        payload["schema"] != "elspeth.aws-ecs-scenario-inventory.v9"
        or payload["scenario_id"] != scenario_id
        or payload["phase"] != expected_phase
        or payload["acceptance_run_id"] != acceptance_run_id
        or payload["candidate_sha"] != candidate_sha
        or payload["aws_account_id"] != aws_account_id
        or payload["aws_region"] != aws_region
    ):
        raise AcceptanceCheckError("scenario_inventory_binding")
    gateway_image = payload["gateway_image"]
    gateway_sha = payload["gateway_sha"]
    gateway_adapter = payload["gateway_adapter"]
    gateway_adapter_version = payload["gateway_adapter_version"]
    gateway_adapter_api_major = payload["gateway_adapter_api_major"]
    gateway_adapter_fingerprint = payload["gateway_adapter_fingerprint"]
    if scenario_id == "C":
        expected_gateway_registry = f"{aws_account_id}.dkr.ecr.{aws_region}.amazonaws.com/"
        if (
            type(gateway_image) is not str
            or len(gateway_image) > 2048
            or not gateway_image.startswith(expected_gateway_registry)
            or _DIGEST_PINNED_CONTAINER_IMAGE_PATTERN.fullmatch(gateway_image) is None
            or type(gateway_sha) is not str
            or re.fullmatch(r"[0-9a-f]{40}", gateway_sha) is None
            or type(gateway_adapter) is not str
            or not 1 <= len(gateway_adapter) <= 256
            or gateway_adapter != gateway_adapter.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in gateway_adapter)
            # The adapter identity quadruple the Terraform admission gate
            # verified against the image's labels (elspeth-ef60d2ff3c
            # GAP-5): version mirrors the adapter-name shape, api-major is
            # a small positive integer (bool is an int subclass — reject),
            # fingerprint is the 64-hex package digest.
            or type(gateway_adapter_version) is not str
            or not 1 <= len(gateway_adapter_version) <= 256
            or gateway_adapter_version != gateway_adapter_version.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in gateway_adapter_version)
            or type(gateway_adapter_api_major) is not int
            or not 1 <= gateway_adapter_api_major <= 1000
            or type(gateway_adapter_fingerprint) is not str
            or re.fullmatch(r"[0-9a-f]{64}", gateway_adapter_fingerprint) is None
        ):
            raise AcceptanceCheckError("scenario_inventory_schema")
    elif scenario_id in {"A", "B"}:
        if (
            gateway_image is not None
            or gateway_sha is not None
            or gateway_adapter is not None
            or gateway_adapter_version is not None
            or gateway_adapter_api_major is not None
            or gateway_adapter_fingerprint is not None
        ):
            raise AcceptanceCheckError("scenario_inventory_schema")
    else:
        raise AcceptanceCheckError("scenario_inventory_binding")
    values = payload["values"]
    if type(values) is not dict or set(values) != SCENARIO_VALUE_FIELDS:
        raise AcceptanceCheckError("scenario_inventory_schema")
    for value in values.values():
        if type(value) is not str or len(value) > 16 * 1024 or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise AcceptanceCheckError("scenario_inventory_schema")
    if (
        values["AWS_REGION"] != aws_region
        or values["SCENARIO_TF_BINDING_SHA"] != tf_binding_sha256
        or values["DEPLOYMENT_MODE"] != ("upgrade" if scenario_id == "B" else "first")
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
    if (
        len(cast(str, values["CLOUDWATCH_AGENT_IMAGE"])) > 2048
        or _DIGEST_PINNED_CONTAINER_IMAGE_PATTERN.fullmatch(cast(str, values["CLOUDWATCH_AGENT_IMAGE"])) is None
        or _SHA256_PATTERN.fullmatch(cast(str, values["CLOUDWATCH_AGENT_CONFIG_JSON_SHA256"])) is None
        or _SHA256_PATTERN.fullmatch(cast(str, values["CLOUDWATCH_AGENT_OTEL_YAML_SHA256"])) is None
    ):
        raise AcceptanceCheckError("scenario_inventory_schema")
    live_model = values["ELSPETH_BEDROCK_LIVE_TEST_MODEL"]
    if scenario_id == "C":
        if live_model != "":
            raise AcceptanceCheckError("scenario_inventory_schema")
    elif not live_model.startswith("bedrock/") or any(character.isspace() for character in live_model):
        raise AcceptanceCheckError("scenario_inventory_schema")
    for name in _TASK_DEFINITION_COMPOSER_MODEL_ENV:
        model = values[name]
        if (
            not model
            or any(character.isspace() for character in model)
            or (scenario_id != "C" and (infer_provider_from_model_name(model) or infer_provider_from_unprefixed_model_name(model)) is None)
            or (scenario_id == "C" and model.startswith("bedrock/"))
        ):
            raise AcceptanceCheckError("scenario_inventory_schema")
    for name in PLUGIN_POLICY_ASSIGNMENT_NAMES:
        if name == "ELSPETH_WEB__DEFAULT_LLM_PROFILE":
            continue
        try:
            json.loads(cast(str, values[name]))
        except json.JSONDecodeError:
            raise AcceptanceCheckError("scenario_inventory_schema") from None
    if scenario_id == "C":
        _validate_gateway_profiles(values)
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
    if scenario_id == "C":
        if guardrail_profiles_payload != []:
            raise AcceptanceCheckError("scenario_inventory_schema")
        try:
            guardrail_defaults = json.loads(cast(str, values["ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES"]))
        except json.JSONDecodeError:
            raise AcceptanceCheckError("scenario_inventory_schema") from None
        if guardrail_defaults != {} or orphan["bedrock_guardrails"] != []:
            raise AcceptanceCheckError("scenario_inventory_schema")
    elif expected_phase == "preapply":
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
    values_a = cast(Mapping[str, object], inventory_a["values"])
    values_b = cast(Mapping[str, object], inventory_b["values"])
    orphan_a = cast(Mapping[str, object], inventory_a["orphan_sweep"])
    orphan_b = cast(Mapping[str, object], inventory_b["orphan_sweep"])
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
    scenarios = cast(dict[str, object], manifest["scenarios"])
    aws = cast(dict[str, object], manifest["aws"])
    scenario = cast(dict[str, object], scenarios[scenario_id])
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
    scenarios = cast(dict[str, object], manifest["scenarios"])
    aws = cast(dict[str, object], manifest["aws"])
    scenario = cast(dict[str, object], scenarios[scenario_id])
    acceptance_run_id = cast(str, manifest["acceptance_run_id"])
    candidate_sha = cast(str, manifest["candidate_sha"])
    account_id = cast(str, aws["account_id"])
    region = cast(str, aws["region"])
    binding = cast(str, scenario["tf_binding_sha256"])
    _load_preapply_scenario_inventory(manifest, scenario_id)
    inventory = _validate_scenario_inventory(
        _read_protected_document(Path(cast(str, scenario["inventory_path"])), check="scenario_inventory_file"),
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
    values = cast(dict[str, object], inventory["values"])
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
