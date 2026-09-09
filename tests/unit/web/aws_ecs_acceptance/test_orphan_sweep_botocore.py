"""Offline real-botocore proof for orphan-sweep AWS request and response shapes.

Every other orphan-sweep test drives hand-written fakes that accept any keyword
and return any key. ``botocore.stub.Stubber`` wraps a real ``boto3`` client and
validates both the outbound parameters and the stubbed response against
botocore's shipped service model, so a mistyped request key or an invented
response field fails here instead of silently reporting a torn-down account
clean.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

import boto3
import pytest
from botocore.stub import Stubber

from elspeth.web._aws_ecs_acceptance import orphan_sweep as owner
from elspeth.web._aws_ecs_acceptance.contracts import AcceptanceCheckError

_REGION = "ap-southeast-2"
_ACCOUNT = "123456789012"
_RUN_ID = "6ff5f1f0-6d4b-4a0f-8f0d-0f9f8f0d0f9f"
_FAMILY = "elspeth-web"
_TASK_DEFINITION_ARN = f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:task-definition/{_FAMILY}:1"


def _items(response: Mapping[str, object] | None, field: str) -> list[Any]:
    """Read one modelled response collection through the sweep's own accessor."""
    return cast("list[Any]", owner._orphan_response_items(response, field))


def _paged(method: Any, *, item_field: str, request_token: str, response_token: str, kwargs: Mapping[str, object]) -> list[Any]:
    return cast(
        "list[Any]",
        owner._orphan_paged_items(
            method,
            item_field=item_field,
            request_token=request_token,
            response_token=response_token,
            kwargs=kwargs,
        ),
    )


def _sdk(service: str) -> Any:
    """Build a real client that can never reach the network under a Stubber."""
    return boto3.client(
        service,
        region_name=_REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


# --------------------------------------------------------------------------
# Resource Groups Tagging API — the sweep-wide survivor query.
# --------------------------------------------------------------------------


def test_tagging_get_resources_request_and_pagination_match_the_service_model() -> None:
    sdk = _sdk("resourcegroupstaggingapi")
    kwargs = {
        "TagFilters": [{"Key": "ACCEPTANCE_RUN_ID", "Values": [_RUN_ID]}],
        "ResourcesPerPage": 100,
        "IncludeComplianceDetails": False,
    }
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_resources",
            {"ResourceTagMappingList": [{"ResourceARN": f"{_TASK_DEFINITION_ARN}"}], "PaginationToken": "page-2"},
            expected_params=kwargs,
        )
        stubber.add_response(
            "get_resources",
            {"ResourceTagMappingList": [{"ResourceARN": f"{_TASK_DEFINITION_ARN}0"}], "PaginationToken": ""},
            expected_params={**kwargs, "PaginationToken": "page-2"},
        )
        items = _paged(
            sdk.get_resources,
            item_field="ResourceTagMappingList",
            request_token="PaginationToken",
            response_token="PaginationToken",
            kwargs=kwargs,
        )
        stubber.assert_no_pending_responses()
    assert [item["ResourceARN"] for item in items] == [_TASK_DEFINITION_ARN, f"{_TASK_DEFINITION_ARN}0"]


# --------------------------------------------------------------------------
# ECS — the destructive path: ownership proof, then deregister/delete.
# --------------------------------------------------------------------------


def test_describe_task_definition_ownership_probe_matches_the_service_model() -> None:
    sdk = _sdk("ecs")
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_task_definition",
            {
                "taskDefinition": {"taskDefinitionArn": _TASK_DEFINITION_ARN, "family": _FAMILY, "revision": 1},
                "tags": [{"key": "ACCEPTANCE_RUN_ID", "value": _RUN_ID}],
            },
            expected_params={"taskDefinition": _TASK_DEFINITION_ARN, "include": ["TAGS"]},
        )
        owner._task_definition_owned(sdk, _TASK_DEFINITION_ARN, family=_FAMILY, acceptance_run_id=_RUN_ID)
        stubber.assert_no_pending_responses()


def test_describe_task_definition_without_the_run_tag_refuses_ownership() -> None:
    sdk = _sdk("ecs")
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_task_definition",
            {
                "taskDefinition": {"taskDefinitionArn": _TASK_DEFINITION_ARN, "family": _FAMILY, "revision": 1},
                "tags": [{"key": "ACCEPTANCE_RUN_ID", "value": "another-run"}],
            },
            expected_params={"taskDefinition": _TASK_DEFINITION_ARN, "include": ["TAGS"]},
        )
        with pytest.raises(AcceptanceCheckError, match="orphan_sweep_binding"):
            owner._task_definition_owned(sdk, _TASK_DEFINITION_ARN, family=_FAMILY, acceptance_run_id=_RUN_ID)


def test_list_task_definitions_paging_matches_the_service_model() -> None:
    sdk = _sdk("ecs")
    kwargs = {"familyPrefix": _FAMILY, "status": "ACTIVE", "sort": "ASC", "maxResults": 100}
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "list_task_definitions",
            {"taskDefinitionArns": [_TASK_DEFINITION_ARN], "nextToken": "page-2"},
            expected_params=kwargs,
        )
        stubber.add_response(
            "list_task_definitions",
            {"taskDefinitionArns": []},
            expected_params={**kwargs, "nextToken": "page-2"},
        )
        arns = _paged(
            sdk.list_task_definitions,
            item_field="taskDefinitionArns",
            request_token="nextToken",
            response_token="nextToken",
            kwargs=kwargs,
        )
        stubber.assert_no_pending_responses()
    assert arns == [_TASK_DEFINITION_ARN]


def test_list_tasks_request_shape_matches_the_service_model() -> None:
    sdk = _sdk("ecs")
    kwargs = {"cluster": "elspeth-cluster", "serviceName": "elspeth-web", "desiredStatus": "RUNNING", "maxResults": 100}
    with Stubber(sdk) as stubber:
        stubber.add_response("list_tasks", {"taskArns": []}, expected_params=kwargs)
        assert (
            _paged(
                sdk.list_tasks,
                item_field="taskArns",
                request_token="nextToken",
                response_token="nextToken",
                kwargs=kwargs,
            )
            == []
        )
        stubber.assert_no_pending_responses()


def test_describe_services_request_shape_matches_the_service_model() -> None:
    sdk = _sdk("ecs")
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_services",
            {"services": [], "failures": []},
            expected_params={"cluster": "elspeth-cluster", "services": ["elspeth-web"], "include": ["TAGS"]},
        )
        response = owner._orphan_call(sdk.describe_services, cluster="elspeth-cluster", services=["elspeth-web"], include=["TAGS"])
        stubber.assert_no_pending_responses()
    assert _items(response, "services") == []


def test_deregister_and_delete_task_definitions_match_the_service_model() -> None:
    sdk = _sdk("ecs")
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "deregister_task_definition",
            {"taskDefinition": {"taskDefinitionArn": _TASK_DEFINITION_ARN, "family": _FAMILY, "revision": 1, "status": "INACTIVE"}},
            expected_params={"taskDefinition": _TASK_DEFINITION_ARN},
        )
        stubber.add_response(
            "delete_task_definitions",
            {"taskDefinitions": [{"taskDefinitionArn": _TASK_DEFINITION_ARN, "status": "DELETE_IN_PROGRESS"}], "failures": []},
            expected_params={"taskDefinitions": [_TASK_DEFINITION_ARN]},
        )
        deregistered = owner._orphan_call(sdk.deregister_task_definition, taskDefinition=_TASK_DEFINITION_ARN)
        deleted = owner._orphan_call(sdk.delete_task_definitions, taskDefinitions=[_TASK_DEFINITION_ARN])
        stubber.assert_no_pending_responses()
    assert deregistered is not None
    assert _items(deleted, "failures") == []


# --------------------------------------------------------------------------
# ELBv2 / RDS — identity-scoped describe calls.
# --------------------------------------------------------------------------


def test_elbv2_describe_load_balancers_matches_the_service_model() -> None:
    sdk = _sdk("elbv2")
    arn = f"arn:aws:elasticloadbalancing:{_REGION}:{_ACCOUNT}:loadbalancer/app/elspeth/abc123"
    with Stubber(sdk) as stubber:
        stubber.add_response("describe_load_balancers", {"LoadBalancers": []}, expected_params={"LoadBalancerArns": [arn]})
        response = owner._orphan_call(sdk.describe_load_balancers, LoadBalancerArns=[arn])
        stubber.assert_no_pending_responses()
    assert _items(response, "LoadBalancers") == []


def test_elbv2_describe_target_groups_and_rules_match_the_service_model() -> None:
    sdk = _sdk("elbv2")
    target_group_arn = f"arn:aws:elasticloadbalancing:{_REGION}:{_ACCOUNT}:targetgroup/elspeth/abc123"
    rule_arn = f"arn:aws:elasticloadbalancing:{_REGION}:{_ACCOUNT}:listener-rule/app/elspeth/abc123/def456/ghi789"
    with Stubber(sdk) as stubber:
        stubber.add_response("describe_target_groups", {"TargetGroups": []}, expected_params={"TargetGroupArns": [target_group_arn]})
        stubber.add_response("describe_rules", {"Rules": []}, expected_params={"RuleArns": [rule_arn]})
        assert _items(owner._orphan_call(sdk.describe_target_groups, TargetGroupArns=[target_group_arn]), "TargetGroups") == []
        assert _items(owner._orphan_call(sdk.describe_rules, RuleArns=[rule_arn]), "Rules") == []
        stubber.assert_no_pending_responses()


def test_rds_describe_db_clusters_and_instances_match_the_service_model() -> None:
    sdk = _sdk("rds")
    with Stubber(sdk) as stubber:
        stubber.add_response("describe_db_clusters", {"DBClusters": []}, expected_params={"DBClusterIdentifier": "elspeth-db"})
        stubber.add_response("describe_db_instances", {"DBInstances": []}, expected_params={"DBInstanceIdentifier": "elspeth-db-1"})
        assert _items(owner._orphan_call(sdk.describe_db_clusters, DBClusterIdentifier="elspeth-db"), "DBClusters") == []
        assert _items(owner._orphan_call(sdk.describe_db_instances, DBInstanceIdentifier="elspeth-db-1"), "DBInstances") == []
        stubber.assert_no_pending_responses()


# --------------------------------------------------------------------------
# EFS — the only asymmetric pagination pair in the sweep (Marker/NextMarker).
# --------------------------------------------------------------------------


def test_efs_describe_mount_targets_uses_marker_request_and_next_marker_response() -> None:
    sdk = _sdk("efs")
    kwargs = {"FileSystemId": "fs-0123456789abcdef0", "MaxItems": 100}
    mount_target = {
        "MountTargetId": "fsmt-0123456789abcdef0",
        "FileSystemId": "fs-0123456789abcdef0",
        "SubnetId": "subnet-0123456789abcdef0",
        "LifeCycleState": "available",
    }
    with Stubber(sdk) as stubber:
        stubber.add_response("describe_mount_targets", {"MountTargets": [mount_target], "NextMarker": "page-2"}, expected_params=kwargs)
        stubber.add_response("describe_mount_targets", {"MountTargets": []}, expected_params={**kwargs, "Marker": "page-2"})
        mount_targets = _paged(
            sdk.describe_mount_targets,
            item_field="MountTargets",
            request_token="Marker",
            response_token="NextMarker",
            kwargs=kwargs,
        )
        stubber.assert_no_pending_responses()
    assert len(mount_targets) == 1


def test_efs_describe_file_systems_and_access_points_match_the_service_model() -> None:
    sdk = _sdk("efs")
    with Stubber(sdk) as stubber:
        stubber.add_response("describe_file_systems", {"FileSystems": []}, expected_params={"CreationToken": "elspeth-efs"})
        stubber.add_response(
            "describe_access_points",
            {"AccessPoints": []},
            expected_params={"FileSystemId": "fs-0123456789abcdef0", "MaxResults": 100},
        )
        assert _items(owner._orphan_call(sdk.describe_file_systems, CreationToken="elspeth-efs"), "FileSystems") == []
        assert (
            _paged(
                sdk.describe_access_points,
                item_field="AccessPoints",
                request_token="NextToken",
                response_token="NextToken",
                kwargs={"FileSystemId": "fs-0123456789abcdef0", "MaxResults": 100},
            )
            == []
        )
        stubber.assert_no_pending_responses()


# --------------------------------------------------------------------------
# IAM — the ClientError control-flow path, raised by botocore itself.
# --------------------------------------------------------------------------


def test_iam_get_role_reads_the_modelled_role_shape() -> None:
    sdk = _sdk("iam")
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_role",
            {
                "Role": {
                    "Path": "/",
                    "RoleName": "elspeth-task",
                    "RoleId": "AROAEXAMPLEROLEID123",
                    "Arn": f"arn:aws:iam::{_ACCOUNT}:role/elspeth-task",
                    "CreateDate": datetime(2026, 7, 1, tzinfo=UTC),
                }
            },
            expected_params={"RoleName": "elspeth-task"},
        )
        response = owner._orphan_call(sdk.get_role, RoleName="elspeth-task")
        stubber.assert_no_pending_responses()
    assert response is not None
    assert cast("Mapping[str, Any]", response)["Role"]["RoleName"] == "elspeth-task"


def test_iam_get_role_absent_is_a_real_botocore_client_error_and_reads_as_swept() -> None:
    sdk = _sdk("iam")
    with Stubber(sdk) as stubber:
        stubber.add_client_error(
            "get_role",
            service_error_code="NoSuchEntity",
            service_message="The role with name elspeth-task cannot be found.",
            http_status_code=404,
            expected_params={"RoleName": "elspeth-task"},
        )
        assert owner._orphan_call(sdk.get_role, RoleName="elspeth-task") is None
        stubber.assert_no_pending_responses()


def test_unmodelled_client_error_code_is_projected_as_a_sweep_api_failure() -> None:
    sdk = _sdk("iam")
    with Stubber(sdk) as stubber:
        stubber.add_client_error("get_role", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(AcceptanceCheckError, match="orphan_sweep_api"):
            owner._orphan_call(sdk.get_role, RoleName="elspeth-task")


# --------------------------------------------------------------------------
# Secrets Manager / CloudWatch Logs.
# --------------------------------------------------------------------------


def test_secretsmanager_describe_secret_matches_the_service_model() -> None:
    sdk = _sdk("secretsmanager")
    secret_id = f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:elspeth-db-abc123"
    with Stubber(sdk) as stubber:
        stubber.add_response("describe_secret", {"ARN": secret_id, "Name": "elspeth-db"}, expected_params={"SecretId": secret_id})
        assert owner._orphan_call(sdk.describe_secret, SecretId=secret_id) is not None
        stubber.assert_no_pending_responses()


def test_logs_describe_log_groups_and_resource_policies_match_the_service_model() -> None:
    sdk = _sdk("logs")
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_log_groups",
            {"logGroups": [{"logGroupName": "/elspeth/web"}]},
            expected_params={"logGroupNamePrefix": "/elspeth/web", "limit": 50},
        )
        stubber.add_response(
            "describe_resource_policies",
            {"resourcePolicies": [{"policyName": "elspeth-delivery"}]},
            expected_params={"limit": 50},
        )
        groups = _paged(
            sdk.describe_log_groups,
            item_field="logGroups",
            request_token="nextToken",
            response_token="nextToken",
            kwargs={"logGroupNamePrefix": "/elspeth/web", "limit": 50},
        )
        policies = _paged(
            sdk.describe_resource_policies,
            item_field="resourcePolicies",
            request_token="nextToken",
            response_token="nextToken",
            kwargs={"limit": 50},
        )
        stubber.assert_no_pending_responses()
    assert [group["logGroupName"] for group in groups] == ["/elspeth/web"]
    assert [policy["policyName"] for policy in policies] == ["elspeth-delivery"]


# --------------------------------------------------------------------------
# CloudWatch — describe_alarms reads three alarm collections.
# --------------------------------------------------------------------------


def test_cloudwatch_describe_alarms_reads_every_modelled_alarm_collection() -> None:
    sdk = _sdk("cloudwatch")
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_alarms",
            {"MetricAlarms": [], "CompositeAlarms": [], "LogAlarms": []},
            expected_params={"AlarmNames": ["elspeth-5xx"], "MaxRecords": 100},
        )
        response = owner._orphan_call(sdk.describe_alarms, AlarmNames=["elspeth-5xx"], MaxRecords=100)
        stubber.assert_no_pending_responses()
    assert response is not None
    for field in ("MetricAlarms", "CompositeAlarms", "LogAlarms"):
        assert _items(response, field) == []


def test_cloudwatch_list_metrics_request_shape_matches_the_service_model() -> None:
    sdk = _sdk("cloudwatch")
    kwargs = {
        "Namespace": "ELSPETH/Acceptance",
        "MetricName": "RetainedSeries",
        "Dimensions": [{"Name": "AcceptanceRunId", "Value": _RUN_ID}],
        "IncludeLinkedAccounts": False,
    }
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "list_metrics",
            {
                "Metrics": [
                    {
                        "Namespace": "ELSPETH/Acceptance",
                        "MetricName": "RetainedSeries",
                        "Dimensions": [{"Name": "AcceptanceRunId", "Value": _RUN_ID}],
                    }
                ]
            },
            expected_params=kwargs,
        )
        metrics = _paged(
            sdk.list_metrics,
            item_field="Metrics",
            request_token="NextToken",
            response_token="NextToken",
            kwargs=kwargs,
        )
        stubber.assert_no_pending_responses()
    assert len(metrics) == 1


# --------------------------------------------------------------------------
# ECR — the run-scoped tag deletion the sweep actually performs.
# --------------------------------------------------------------------------


def test_ecr_describe_and_batch_delete_image_match_the_service_model() -> None:
    sdk = _sdk("ecr")
    expected = {"registryId": _ACCOUNT, "repositoryName": "elspeth-web", "imageIds": [{"imageTag": "candidate"}]}
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_images",
            {"imageDetails": [{"registryId": _ACCOUNT, "repositoryName": "elspeth-web", "imageTags": ["candidate"]}]},
            expected_params=expected,
        )
        stubber.add_response(
            "batch_delete_image",
            {"imageIds": [{"imageTag": "candidate"}], "failures": []},
            expected_params=expected,
        )
        before = owner._orphan_call(sdk.describe_images, **expected)
        deleted = owner._orphan_call(sdk.batch_delete_image, **expected)
        stubber.assert_no_pending_responses()
    assert len(_items(before, "imageDetails")) == 1
    assert _items(deleted, "failures") == []


# --------------------------------------------------------------------------
# X-Ray / EventBridge / Bedrock / Cognito — remaining sweep surfaces.
# --------------------------------------------------------------------------


def test_xray_group_and_sampling_rule_reads_match_the_service_model() -> None:
    sdk = _sdk("xray")
    with Stubber(sdk) as stubber:
        stubber.add_response("get_groups", {"Groups": [{"GroupName": "elspeth"}]}, expected_params={})
        stubber.add_response(
            "get_sampling_rules",
            {
                "SamplingRuleRecords": [
                    {
                        "SamplingRule": {
                            "RuleName": "elspeth",
                            "ResourceARN": "*",
                            "Priority": 1,
                            "FixedRate": 0.05,
                            "ReservoirSize": 1,
                            "ServiceName": "*",
                            "ServiceType": "*",
                            "Host": "*",
                            "HTTPMethod": "*",
                            "URLPath": "*",
                            "Version": 1,
                        }
                    }
                ]
            },
            expected_params={},
        )
        groups = _paged(sdk.get_groups, item_field="Groups", request_token="NextToken", response_token="NextToken", kwargs={})
        rules = _paged(
            sdk.get_sampling_rules, item_field="SamplingRuleRecords", request_token="NextToken", response_token="NextToken", kwargs={}
        )
        stubber.assert_no_pending_responses()
    assert [group["GroupName"] for group in groups] == ["elspeth"]
    assert rules[0]["SamplingRule"]["RuleName"] == "elspeth"


def test_xray_trace_segment_destination_read_matches_the_service_model() -> None:
    sdk = _sdk("xray")
    with Stubber(sdk) as stubber:
        stubber.add_response("get_trace_segment_destination", {"Destination": "XRay", "Status": "ACTIVE"}, expected_params={})
        response = owner._orphan_call(sdk.get_trace_segment_destination)
        stubber.assert_no_pending_responses()
    assert response is not None
    assert response.get("Destination") == "XRay"


def test_events_rule_and_target_reads_match_the_service_model() -> None:
    sdk = _sdk("events")
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_rule",
            {"Name": "elspeth-deployments", "Arn": f"arn:aws:events:{_REGION}:{_ACCOUNT}:rule/elspeth-deployments"},
            expected_params={"Name": "elspeth-deployments", "EventBusName": "default"},
        )
        stubber.add_response(
            "list_targets_by_rule",
            {"Targets": [{"Id": "elspeth-deployment-target", "Arn": f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:/elspeth/deployments"}]},
            expected_params={"Rule": "elspeth-deployments", "EventBusName": "default", "Limit": 100},
        )
        assert owner._orphan_call(sdk.describe_rule, Name="elspeth-deployments", EventBusName="default") is not None
        targets = _paged(
            sdk.list_targets_by_rule,
            item_field="Targets",
            request_token="NextToken",
            response_token="NextToken",
            kwargs={"Rule": "elspeth-deployments", "EventBusName": "default", "Limit": 100},
        )
        stubber.assert_no_pending_responses()
    assert [target["Id"] for target in targets] == ["elspeth-deployment-target"]


def test_bedrock_list_guardrails_request_shape_matches_the_service_model() -> None:
    sdk = _sdk("bedrock")
    kwargs = {"guardrailIdentifier": "abcdefghij12", "maxResults": 1000}
    with Stubber(sdk) as stubber:
        stubber.add_response("list_guardrails", {"guardrails": []}, expected_params=kwargs)
        assert (
            _paged(
                sdk.list_guardrails,
                item_field="guardrails",
                request_token="nextToken",
                response_token="nextToken",
                kwargs=kwargs,
            )
            == []
        )
        stubber.assert_no_pending_responses()


def test_cognito_user_pool_and_user_reads_match_the_service_model() -> None:
    sdk = _sdk("cognito-idp")
    kwargs = {
        "UserPoolId": f"{_REGION}_abcdefghi",
        "Filter": f'sub = "{_RUN_ID}"',
        "AttributesToGet": ["sub"],
        "Limit": 60,
    }
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_user_pool",
            {"UserPool": {"Id": f"{_REGION}_abcdefghi", "Name": "elspeth"}},
            expected_params={"UserPoolId": f"{_REGION}_abcdefghi"},
        )
        stubber.add_response("list_users", {"Users": []}, expected_params=kwargs)
        assert owner._orphan_call(sdk.describe_user_pool, UserPoolId=f"{_REGION}_abcdefghi") is not None
        assert (
            _paged(
                sdk.list_users,
                item_field="Users",
                request_token="PaginationToken",
                response_token="PaginationToken",
                kwargs=kwargs,
            )
            == []
        )
        stubber.assert_no_pending_responses()
