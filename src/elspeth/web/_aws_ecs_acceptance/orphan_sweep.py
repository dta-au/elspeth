"""Bounded AWS orphan discovery and cleanup ownership."""

from __future__ import annotations

import contextlib
import json
import math
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from botocore.exceptions import ClientError

from .contracts import AcceptanceCheckError, _sha256, _task_definition_family, _utc_timestamp
from .manifest_schema import _load_retained_evidence, _read_control_manifest
from .receipt_contracts import _validate_bounded_receipt_document
from .scenario_inventory import _ORPHAN_MAX_ITEMS, _load_bound_scenario_inventory

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
            with contextlib.suppress(Exception):
                created_client.close()
        raise AcceptanceCheckError("orphan_sweep_runtime") from None


def _aws_error_code(exc: Exception) -> str | None:
    if not isinstance(exc, ClientError):
        return None
    response = exc.response
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


def _orphan_call(method: Callable[..., object], **kwargs: object) -> Mapping[str, object] | None:
    try:
        response = method(**kwargs)
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
    method: Callable[..., object],
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
        response = _orphan_call(method, **request)
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
    values = cast(dict[str, object], inventory["values"])
    orphan = cast(dict[str, object], inventory["orphan_sweep"])
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
        client.describe_task_definition,
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
    evidence = cast(Mapping[str, object], manifest["evidence"])
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
        retained_scenarios = cast(dict[str, object], retained_evidence["scenarios"])
    aws = cast(dict[str, object], manifest["aws"])
    ecr_manifest = cast(dict[str, object], manifest["ecr"])
    if clients is None:
        clients = _build_orphan_sweep_clients(str(aws["region"]))
    counts = {surface: {"queried": 0, "unapproved_survivors": 0} for surface in _ORPHAN_SURFACES}
    delete_in_progress_receipts: list[dict[str, object]] = []
    retained_metrics = 0
    retained_traces = 0
    observed_retained_metrics = 0
    observed_retained_traces = 0
    resource_close_failed = False

    def close_client(client: Any) -> None:
        nonlocal resource_close_failed
        try:
            client.close()
        except Exception:
            resource_close_failed = True

    with contextlib.ExitStack() as stack:
        for client in clients:
            stack.callback(close_client, client)
        try:
            repository = ecr_manifest["repository"]
            tags = (ecr_manifest["baseline_tag"], ecr_manifest["candidate_tag"])
            if type(repository) is not str or not repository or any(type(tag) is not str or not tag for tag in tags):
                raise AcceptanceCheckError("orphan_sweep_binding")
            for tag in tags:
                counts["ecr"]["queried"] += 1
                before = _orphan_call(
                    clients.ecr.describe_images,
                    registryId=aws["account_id"],
                    repositoryName=repository,
                    imageIds=[{"imageTag": tag}],
                )
                image_details = _orphan_response_items(before, "imageDetails")
                if image_details:
                    deleted = _orphan_call(
                        clients.ecr.batch_delete_image,
                        registryId=aws["account_id"],
                        repositoryName=repository,
                        imageIds=[{"imageTag": tag}],
                    )
                    if deleted is None or _orphan_response_items(deleted, "failures"):
                        raise AcceptanceCheckError("orphan_sweep_api")
                after = _orphan_call(
                    clients.ecr.describe_images,
                    registryId=aws["account_id"],
                    repositoryName=repository,
                    imageIds=[{"imageTag": tag}],
                )
                if _orphan_response_items(after, "imageDetails"):
                    counts["ecr"]["unapproved_survivors"] += 1

            tagged = _orphan_paged_items(
                clients.tagging.get_resources,
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
                scenario_retained = cast(dict[str, object], retained_scenarios[scenario_id])
                orphan = {**orphan, **scenario_retained}
                retained_metric_count = cast(int, orphan["expected_retained_metric_series"])
                retained_trace_count = cast(int, orphan["expected_retained_trace_ids"])
                retained_metrics += retained_metric_count
                retained_traces += retained_trace_count
                cluster = values["ECS_CLUSTER"]
                service = values["ECS_SERVICE"]
                services = _orphan_call(
                    clients.ecs.describe_services,
                    cluster=cluster,
                    services=[service],
                    include=["TAGS"],
                )
                counts["ecs"]["queried"] += 1
                counts["ecs"]["unapproved_survivors"] += len(_orphan_response_items(services, "services"))
                for desired_status in ("RUNNING", "PENDING"):
                    task_arns = _orphan_paged_items(
                        clients.ecs.list_tasks,
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
                families = cast(list[str], orphan["ecs_task_definition_families"])
                for family in families:
                    for desired_status in ("RUNNING", "PENDING"):
                        family_tasks = _orphan_paged_items(
                            clients.ecs.list_tasks,
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
                            clients.ecs.list_task_definitions,
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
                    for item in by_status["ACTIVE"]:
                        task_definition_arn = cast(str, item)
                        _task_definition_owned(
                            clients.ecs,
                            task_definition_arn,
                            family=family,
                            acceptance_run_id=acceptance_run_id,
                        )
                        verified_task_definitions.add(task_definition_arn)
                        deregistered = _orphan_call(
                            clients.ecs.deregister_task_definition,
                            taskDefinition=task_definition_arn,
                        )
                        if deregistered is None:
                            raise AcceptanceCheckError("orphan_sweep_api")
                        inactive.append(task_definition_arn)
                    for item in by_status["DELETE_IN_PROGRESS"]:
                        task_definition_arn = cast(str, item)
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
                        for item in batch:
                            task_definition_arn = cast(str, item)
                            if task_definition_arn not in verified_task_definitions:
                                _task_definition_owned(
                                    clients.ecs,
                                    task_definition_arn,
                                    family=family,
                                    acceptance_run_id=acceptance_run_id,
                                )
                                verified_task_definitions.add(task_definition_arn)
                        deleted = _orphan_call(clients.ecs.delete_task_definitions, taskDefinitions=batch)
                        if deleted is None or _orphan_response_items(deleted, "failures"):
                            raise AcceptanceCheckError("orphan_sweep_api")
                        allowed_deleting_task_definitions.update(cast(list[str], batch))
                    active_after = _orphan_paged_items(
                        clients.ecs.list_task_definitions,
                        item_field="taskDefinitionArns",
                        request_token="nextToken",
                        response_token="nextToken",
                        kwargs={"familyPrefix": family, "status": "ACTIVE", "sort": "ASC", "maxResults": 100},
                    )
                    inactive_after = _orphan_paged_items(
                        clients.ecs.list_task_definitions,
                        item_field="taskDefinitionArns",
                        request_token="nextToken",
                        response_token="nextToken",
                        kwargs={"familyPrefix": family, "status": "INACTIVE", "sort": "ASC", "maxResults": 100},
                    )
                    deleting_after = _orphan_paged_items(
                        clients.ecs.list_task_definitions,
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
                    for item in deleting_after:
                        task_definition_arn = cast(str, item)
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
                    ("ALB_ARN", clients.elbv2.describe_load_balancers, "LoadBalancerArns", "LoadBalancers", "elbv2"),
                    ("TARGET_GROUP_ARN", clients.elbv2.describe_target_groups, "TargetGroupArns", "TargetGroups", "elbv2"),
                    ("FIRST_DEPLOY_LISTENER_RULE_ARN", clients.elbv2.describe_rules, "RuleArns", "Rules", "elbv2"),
                    ("DB_CLUSTER_IDENTIFIER", clients.rds.describe_db_clusters, "DBClusterIdentifier", "DBClusters", "rds"),
                ):
                    identity = values[field]
                    if identity:
                        argument: object = [identity] if argument_name.endswith("Arns") else identity
                        response = _orphan_call(method, **{argument_name: argument})
                        counts[surface]["queried"] += 1
                        counts[surface]["unapproved_survivors"] += len(_orphan_response_items(response, response_field))
                listener_arns = cast(list[object], orphan["elbv2_listener_arns"])
                for listener_arn in listener_arns:
                    response = _orphan_call(clients.elbv2.describe_listeners, ListenerArns=[listener_arn])
                    counts["elbv2"]["queried"] += 1
                    counts["elbv2"]["unapproved_survivors"] += len(_orphan_response_items(response, "Listeners"))
                db_instances = cast(list[object], orphan["rds_db_instance_identifiers"])
                for identifier in db_instances:
                    response = _orphan_call(clients.rds.describe_db_instances, DBInstanceIdentifier=identifier)
                    counts["rds"]["queried"] += 1
                    counts["rds"]["unapproved_survivors"] += len(_orphan_response_items(response, "DBInstances"))
                for creation_token in cast(list[str], orphan["efs_creation_tokens"]):
                    response = _orphan_call(clients.efs.describe_file_systems, CreationToken=creation_token)
                    counts["efs"]["queried"] += 1
                    counts["efs"]["unapproved_survivors"] += len(_orphan_response_items(response, "FileSystems"))
                for file_system_id in cast(list[str], orphan["efs_file_system_ids"]):
                    response = _orphan_call(clients.efs.describe_file_systems, FileSystemId=file_system_id)
                    counts["efs"]["queried"] += 1
                    counts["efs"]["unapproved_survivors"] += len(_orphan_response_items(response, "FileSystems"))
                    access_points = _orphan_paged_items(
                        clients.efs.describe_access_points,
                        item_field="AccessPoints",
                        request_token="NextToken",
                        response_token="NextToken",
                        kwargs={"FileSystemId": file_system_id, "MaxResults": 100},
                    )
                    mount_targets = _orphan_paged_items(
                        clients.efs.describe_mount_targets,
                        item_field="MountTargets",
                        request_token="Marker",
                        response_token="NextMarker",
                        kwargs={"FileSystemId": file_system_id, "MaxItems": 100},
                    )
                    counts["efs"]["queried"] += 2
                    counts["efs"]["unapproved_survivors"] += len(access_points) + len(mount_targets)
                for access_point_id in cast(list[str], orphan["efs_access_point_ids"]):
                    response = _orphan_call(clients.efs.describe_access_points, AccessPointId=access_point_id, MaxResults=100)
                    counts["efs"]["queried"] += 1
                    counts["efs"]["unapproved_survivors"] += len(_orphan_response_items(response, "AccessPoints"))
                for secret_id in cast(list[str], orphan["secret_ids"]):
                    response = _orphan_call(clients.secretsmanager.describe_secret, SecretId=secret_id)
                    counts["secretsmanager"]["queried"] += 1
                    if response is not None:
                        counts["secretsmanager"]["unapproved_survivors"] += 1
                for role_name in cast(list[str], orphan["iam_role_names"]):
                    response = _orphan_call(clients.iam.get_role, RoleName=role_name)
                    counts["iam"]["queried"] += 1
                    if response is not None:
                        role = response.get("Role")
                        if not isinstance(role, Mapping) or role.get("RoleName") != role_name:
                            raise AcceptanceCheckError("orphan_sweep_api")
                        counts["iam"]["unapproved_survivors"] += 1
                resource_policies = _orphan_paged_items(
                    clients.logs.describe_resource_policies,
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
                        clients.logs.describe_log_groups,
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
                        clients.cloudwatch.list_dashboards,
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
                    response = _orphan_call(clients.cloudwatch.describe_alarms, AlarmNames=[alarm_name], MaxRecords=100)
                    counts["cloudwatch"]["queried"] += 1
                    counts["cloudwatch"]["unapproved_survivors"] += (
                        len(_orphan_response_items(response, "MetricAlarms"))
                        + len(_orphan_response_items(response, "CompositeAlarms"))
                        + len(_orphan_response_items(response, "LogAlarms"))
                    )
                for metric_query in cast(list[dict[str, object]], orphan["cloudwatch_retained_metrics"]):
                    dimensions = cast(list[dict[str, str]], metric_query["dimensions"])
                    metrics = _orphan_paged_items(
                        clients.cloudwatch.list_metrics,
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
                    clients.xray.get_groups,
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
                    clients.xray.get_sampling_rules,
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
                    response = _orphan_call(clients.xray.batch_get_traces, TraceIds=requested_trace_ids)
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
                destination_response = _orphan_call(clients.xray.get_trace_segment_destination)
                if destination_response is None:
                    raise AcceptanceCheckError("orphan_sweep_api")
                destination = destination_response.get("Destination")
                if destination not in {None, "XRay", "CloudWatchLogs"}:
                    raise AcceptanceCheckError("orphan_sweep_api")
                indexing_rules = _orphan_paged_items(
                    clients.xray.get_indexing_rules,
                    item_field="IndexingRules",
                    request_token="NextToken",
                    response_token="NextToken",
                    kwargs={},
                    allow_missing_items=True,
                )
                spans_groups = _orphan_paged_items(
                    clients.logs.describe_log_groups,
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
                    response = _orphan_call(
                        clients.events.describe_rule,
                        Name=event_rule["rule_name"],
                        EventBusName=event_rule["event_bus_name"],
                    )
                    counts["events"]["queried"] += 1
                    if response is not None:
                        counts["events"]["unapproved_survivors"] += 1
                    targets = _orphan_paged_items(
                        clients.events.list_targets_by_rule,
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
                    guardrails = _orphan_paged_items(
                        clients.bedrock.list_guardrails,
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
                    response = _orphan_call(clients.cognito.describe_user_pool, UserPoolId=user_pool_id)
                    counts["cognito"]["queried"] += 1
                    if response is not None:
                        counts["cognito"]["unapproved_survivors"] += 1
                subject_sub = orphan["cognito_subject_sub"]
                if user_pool_id and subject_sub:
                    users = _orphan_paged_items(
                        clients.cognito.list_users,
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
    if resource_close_failed:
        raise AcceptanceCheckError("orphan_sweep_resource_close")
    return receipt
