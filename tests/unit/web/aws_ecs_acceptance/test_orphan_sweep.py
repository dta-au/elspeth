"""Owner tests for AWS ECS orphan discovery and cleanup."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._aws_ecs_acceptance import orphan_sweep as owner
from tests.unit.web.aws_ecs_acceptance.test_manifest_schema_inventory import (
    _init_control_manifest,
    _retained_checkpoint,
)


def test_facade_reexports_orphan_sweep_owners_by_identity() -> None:
    assert acceptance.OrphanSweepClients is owner.OrphanSweepClients
    assert acceptance._transaction_search_projection is owner._transaction_search_projection
    assert acceptance.orphan_sweep is owner.orphan_sweep


def test_aws_error_code_rejects_exception_objects_that_only_pretend_to_be_client_errors() -> None:
    class Pretender(RuntimeError):
        def __init__(self) -> None:
            self.response = {"Error": {"Code": "ResourceNotFoundException"}}

    assert owner._aws_error_code(Pretender()) is None


def test_retained_evidence_is_one_way_post_observation_state_and_detects_drift(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    evidence = manifest["evidence"]
    assert evidence["retained_evidence_path"].endswith("retained-evidence.json")
    for scenario in ("A", "B"):
        inventory = json.loads(Path(manifest["scenarios"][scenario]["inventory_path"]).read_text())
        assert inventory["orphan_sweep"]["cloudwatch_retained_metrics"] == []
        assert inventory["orphan_sweep"]["xray_retained_trace_ids"] == []

    second_receipt = tmp_path / "second-retained.json"
    second_receipt.write_text(Path(evidence["retained_evidence_path"]).read_text())
    os.chmod(second_receipt, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="retained_evidence_conflict"):
        acceptance.control_manifest_bind_retained_evidence(
            manifest_path,
            receipt_path=str(second_receipt),
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )

    retained_path = Path(evidence["retained_evidence_path"])
    retained = json.loads(retained_path.read_text())
    retained["captured_at"] = "2026-07-14T01:02:00Z"
    retained_path.write_text(json.dumps(retained))
    os.chmod(retained_path, 0o600)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, 10, tzinfo=UTC),
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="retained_evidence_binding"):
        owner.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=_empty_orphan_clients(),
            environ={},
        )


def test_retained_evidence_checkpoints_grow_monotonically_and_cover_mid_failure(tmp_path: Path) -> None:
    run_id = "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48"
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_retained=False)
    partial_path = tmp_path / "retained-a.json"
    partial_path.write_text(json.dumps(_retained_checkpoint(run_id, {"A"}, "2026-07-14T01:01:20Z")))
    os.chmod(partial_path, 0o600)
    acceptance.control_manifest_bind_retained_evidence(
        manifest_path,
        receipt_path=str(partial_path),
        now=lambda: datetime(2026, 7, 14, 1, 1, 30, tzinfo=UTC),
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="retained_evidence_incomplete"):
        acceptance.control_manifest_bind_retained_evidence(
            manifest_path,
            receipt_path=str(partial_path),
            require_complete=True,
            now=lambda: datetime(2026, 7, 14, 1, 1, 40, tzinfo=UTC),
        )
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag=f"acceptance-{run_id}-baseline",
        ecr_candidate_tag=f"acceptance-{run_id}-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    partial_receipt = owner.orphan_sweep(
        manifest_path,
        acceptance_run_id=run_id,
        clients=_empty_orphan_clients(),
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )
    assert partial_receipt["expected_retained"] == {"metric_series": 1, "trace_ids": 1}
    assert partial_receipt["observed_retained"] == {"metric_series": 1, "trace_ids": 1}

    complete_path = tmp_path / "retained-ab.json"
    complete_path.write_text(json.dumps(_retained_checkpoint(run_id, {"A", "B"}, "2026-07-14T01:04:00Z")))
    os.chmod(complete_path, 0o600)
    acceptance.control_manifest_bind_retained_evidence(
        manifest_path,
        receipt_path=str(complete_path),
        require_complete=True,
        now=lambda: datetime(2026, 7, 14, 1, 4, tzinfo=UTC),
    )
    assert acceptance.control_manifest_get(manifest_path, "evidence.retained_evidence_path") == str(complete_path)

    with pytest.raises(acceptance.AcceptanceCheckError, match="retained_evidence_conflict"):
        acceptance.control_manifest_bind_retained_evidence(
            manifest_path,
            receipt_path=str(partial_path),
            now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
        )


class _FakeOrphanClient:
    def __init__(self, responses: Mapping[str, object]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False
        self.close_calls = 0
        self.close_error: Exception | None = None

    def _invoke(self, name: str, kwargs: dict[str, object]) -> object:
        self.calls.append((name, kwargs))
        response = self.responses[name]
        if callable(response):
            return response(**kwargs)
        if isinstance(response, list):
            if not response:
                raise AssertionError(f"unexpected extra {name} call")
            return response.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    # The AWS operations this double supports are written out explicitly: the set is
    # exactly the operations `orphan_sweep` reaches on its client bundle. An operation
    # outside this closed surface raises AttributeError rather than being synthesised.
    def batch_delete_image(self, **kwargs: object) -> object:
        return self._invoke("batch_delete_image", kwargs)

    def batch_get_traces(self, **kwargs: object) -> object:
        return self._invoke("batch_get_traces", kwargs)

    def delete_task_definitions(self, **kwargs: object) -> object:
        return self._invoke("delete_task_definitions", kwargs)

    def deregister_task_definition(self, **kwargs: object) -> object:
        return self._invoke("deregister_task_definition", kwargs)

    def describe_access_points(self, **kwargs: object) -> object:
        return self._invoke("describe_access_points", kwargs)

    def describe_alarms(self, **kwargs: object) -> object:
        return self._invoke("describe_alarms", kwargs)

    def describe_db_clusters(self, **kwargs: object) -> object:
        return self._invoke("describe_db_clusters", kwargs)

    def describe_db_instances(self, **kwargs: object) -> object:
        return self._invoke("describe_db_instances", kwargs)

    def describe_file_systems(self, **kwargs: object) -> object:
        return self._invoke("describe_file_systems", kwargs)

    def describe_images(self, **kwargs: object) -> object:
        return self._invoke("describe_images", kwargs)

    def describe_listeners(self, **kwargs: object) -> object:
        return self._invoke("describe_listeners", kwargs)

    def describe_load_balancers(self, **kwargs: object) -> object:
        return self._invoke("describe_load_balancers", kwargs)

    def describe_log_groups(self, **kwargs: object) -> object:
        return self._invoke("describe_log_groups", kwargs)

    def describe_mount_targets(self, **kwargs: object) -> object:
        return self._invoke("describe_mount_targets", kwargs)

    def describe_resource_policies(self, **kwargs: object) -> object:
        return self._invoke("describe_resource_policies", kwargs)

    def describe_rule(self, **kwargs: object) -> object:
        return self._invoke("describe_rule", kwargs)

    def describe_rules(self, **kwargs: object) -> object:
        return self._invoke("describe_rules", kwargs)

    def describe_secret(self, **kwargs: object) -> object:
        return self._invoke("describe_secret", kwargs)

    def describe_services(self, **kwargs: object) -> object:
        return self._invoke("describe_services", kwargs)

    def describe_target_groups(self, **kwargs: object) -> object:
        return self._invoke("describe_target_groups", kwargs)

    def describe_task_definition(self, **kwargs: object) -> object:
        return self._invoke("describe_task_definition", kwargs)

    def describe_user_pool(self, **kwargs: object) -> object:
        return self._invoke("describe_user_pool", kwargs)

    def get_groups(self, **kwargs: object) -> object:
        return self._invoke("get_groups", kwargs)

    def get_indexing_rules(self, **kwargs: object) -> object:
        return self._invoke("get_indexing_rules", kwargs)

    def get_resources(self, **kwargs: object) -> object:
        return self._invoke("get_resources", kwargs)

    def get_role(self, **kwargs: object) -> object:
        return self._invoke("get_role", kwargs)

    def get_sampling_rules(self, **kwargs: object) -> object:
        return self._invoke("get_sampling_rules", kwargs)

    def get_trace_segment_destination(self, **kwargs: object) -> object:
        return self._invoke("get_trace_segment_destination", kwargs)

    def list_dashboards(self, **kwargs: object) -> object:
        return self._invoke("list_dashboards", kwargs)

    def list_guardrails(self, **kwargs: object) -> object:
        return self._invoke("list_guardrails", kwargs)

    def list_metrics(self, **kwargs: object) -> object:
        return self._invoke("list_metrics", kwargs)

    def list_targets_by_rule(self, **kwargs: object) -> object:
        return self._invoke("list_targets_by_rule", kwargs)

    def list_task_definitions(self, **kwargs: object) -> object:
        return self._invoke("list_task_definitions", kwargs)

    def list_tasks(self, **kwargs: object) -> object:
        return self._invoke("list_tasks", kwargs)

    def list_users(self, **kwargs: object) -> object:
        return self._invoke("list_users", kwargs)

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _OrphanNotFound(ClientError):
    def __init__(self) -> None:
        super().__init__({"Error": {"Code": "ResourceNotFoundException"}}, "AcceptanceTest")


class _OrphanListenerNotFound(ClientError):
    def __init__(self) -> None:
        super().__init__({"Error": {"Code": "ListenerNotFound"}}, "AcceptanceTest")


class _OrphanRepositoryNotFound(ClientError):
    def __init__(self) -> None:
        super().__init__({"Error": {"Code": "RepositoryNotFoundException"}}, "AcceptanceTest")


class _OrphanNoSuchEntity(ClientError):
    def __init__(self) -> None:
        super().__init__({"Error": {"Code": "NoSuchEntity"}}, "AcceptanceTest")


def _empty_orphan_clients(*, tagged: list[dict[str, object]] | None = None) -> owner.OrphanSweepClients:
    return owner.OrphanSweepClients(
        tagging=_FakeOrphanClient({"get_resources": {"ResourceTagMappingList": tagged or []}}),
        ecs=_FakeOrphanClient(
            {
                "describe_services": {"services": [], "failures": []},
                "list_tasks": {"taskArns": []},
                "list_task_definitions": {"taskDefinitionArns": []},
            }
        ),
        elbv2=_FakeOrphanClient(
            {
                "describe_load_balancers": {"LoadBalancers": []},
                "describe_listeners": {"Listeners": []},
                "describe_rules": {"Rules": []},
                "describe_target_groups": {"TargetGroups": []},
            }
        ),
        rds=_FakeOrphanClient({"describe_db_clusters": {"DBClusters": []}, "describe_db_instances": {"DBInstances": []}}),
        efs=_FakeOrphanClient(
            {
                "describe_file_systems": {"FileSystems": []},
                "describe_access_points": {"AccessPoints": []},
                "describe_mount_targets": {"MountTargets": []},
            }
        ),
        secretsmanager=_FakeOrphanClient({"describe_secret": _OrphanNotFound()}),
        iam=_FakeOrphanClient({"get_role": _OrphanNoSuchEntity()}),
        logs=_FakeOrphanClient({"describe_log_groups": {"logGroups": []}, "describe_resource_policies": {"resourcePolicies": []}}),
        cloudwatch=_FakeOrphanClient(
            {
                "list_dashboards": {"DashboardEntries": []},
                "describe_alarms": {"MetricAlarms": [], "CompositeAlarms": [], "LogAlarms": []},
                "list_metrics": lambda **kwargs: {
                    "Metrics": [
                        {
                            "Namespace": kwargs["Namespace"],
                            "MetricName": kwargs["MetricName"],
                            "Dimensions": kwargs["Dimensions"],
                        }
                    ]
                },
            }
        ),
        xray=_FakeOrphanClient(
            {
                "get_groups": {"Groups": []},
                "get_sampling_rules": {"SamplingRuleRecords": []},
                "batch_get_traces": lambda **kwargs: {
                    "Traces": [{"Id": trace_id} for trace_id in kwargs["TraceIds"]],
                    "UnprocessedTraceIds": [],
                },
                "get_trace_segment_destination": {"Destination": None},
                "get_indexing_rules": {"IndexingRules": []},
            }
        ),
        events=_FakeOrphanClient({"describe_rule": _OrphanNotFound(), "list_targets_by_rule": {"Targets": []}}),
        bedrock=_FakeOrphanClient({"list_guardrails": {"guardrails": []}}),
        cognito=_FakeOrphanClient({"describe_user_pool": _OrphanNotFound(), "list_users": {"Users": []}}),
        ecr=_FakeOrphanClient({"describe_images": {"imageDetails": []}, "batch_delete_image": {"imageIds": [], "failures": []}}),
    )


def test_orphan_sweep_closes_all_clients_emits_only_counts_and_accepts_zero_survivors(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    receipt = owner.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=clients,
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )

    assert receipt["schema"] == "elspeth.aws-ecs-orphan-sweep.v1"
    assert receipt["total_unapproved_survivors"] == 0
    assert receipt["ok"] is True
    assert "4adf8a87" not in json.dumps(receipt)
    assert all(client.closed for client in clients)


def test_orphan_sweep_attempts_every_close_and_translates_close_failures(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    assert isinstance(clients.tagging, _FakeOrphanClient)
    assert isinstance(clients.ecr, _FakeOrphanClient)
    clients.tagging.close_error = RuntimeError("provider tagging close detail")
    clients.ecr.close_error = RuntimeError("provider ecr close detail")

    with pytest.raises(acceptance.AcceptanceCheckError) as exc_info:
        owner.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )

    assert exc_info.value.check == "orphan_sweep_resource_close"
    assert "provider" not in str(exc_info.value)
    assert all(client.close_calls == 1 for client in clients)


def test_orphan_sweep_preserves_primary_failure_when_client_close_also_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    assert isinstance(clients.tagging, _FakeOrphanClient)
    assert isinstance(clients.ecr, _FakeOrphanClient)
    clients.tagging.responses["get_resources"] = RuntimeError("provider api detail")
    clients.tagging.close_error = RuntimeError("provider tagging close detail")
    clients.ecr.close_error = RuntimeError("provider ecr close detail")

    with pytest.raises(acceptance.AcceptanceCheckError) as exc_info:
        owner.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )

    assert exc_info.value.check == "orphan_sweep_api"
    assert "provider" not in str(exc_info.value)
    assert all(client.close_calls == 1 for client in clients)


@pytest.mark.parametrize("surface", ["guardrail-draft", "iam-role", "logs-resource-policy"])
def test_orphan_sweep_rejects_non_taggable_or_unlisted_owned_survivors(tmp_path: Path, surface: str) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    if surface == "guardrail-draft":
        clients.bedrock.responses["list_guardrails"] = {"guardrails": [{"version": "DRAFT"}]}  # type: ignore[union-attr]
    elif surface == "iam-role":
        clients.iam.responses["get_role"] = lambda **kwargs: {"Role": {"RoleName": kwargs["RoleName"]}}  # type: ignore[union-attr]
    else:
        namespace = acceptance.scenario_resource_namespace("4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48", "A")
        clients.logs.responses["describe_resource_policies"] = {  # type: ignore[union-attr]
            "resourcePolicies": [{"policyName": f"{namespace}-delivery-policy"}]
        }

    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_survivors"):
        owner.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )


def test_orphan_sweep_accepts_listener_already_removed_by_terraform(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    assert isinstance(clients.elbv2, _FakeOrphanClient)
    clients.elbv2.responses["describe_listeners"] = _OrphanListenerNotFound()

    receipt = owner.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=clients,
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )

    assert receipt["ok"] is True
    listener_calls = [kwargs for method, kwargs in clients.elbv2.calls if method == "describe_listeners"]
    assert len(listener_calls) == 2
    assert all("listener-rule/" not in str(call["ListenerArns"][0]) for call in listener_calls)


def test_orphan_sweep_accepts_bootstrap_repository_not_created_or_already_removed(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    assert isinstance(clients.ecr, _FakeOrphanClient)
    clients.ecr.responses["describe_images"] = _OrphanRepositoryNotFound()

    receipt = owner.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=clients,
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )

    assert receipt["ok"] is True


@pytest.mark.parametrize("bind_resolved", [False, True])
def test_orphan_sweep_accepts_early_or_mid_failure_before_retained_evidence_is_bound(tmp_path: Path, bind_resolved: bool) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path, bind_resolved=bind_resolved, bind_retained=False)
    assert manifest["evidence"]["retained_evidence_path"] is None  # type: ignore[index]
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )

    receipt = owner.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=_empty_orphan_clients(),
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )

    assert receipt["ok"] is True
    assert receipt["expected_retained"] == {"metric_series": 0, "trace_ids": 0}
    assert receipt["observed_retained"] == {"metric_series": 0, "trace_ids": 0}


def test_orphan_sweep_counts_log_alarms_as_unapproved_survivors(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    clients.cloudwatch.responses["describe_alarms"] = {  # type: ignore[union-attr]
        "MetricAlarms": [],
        "CompositeAlarms": [],
        "LogAlarms": [{"AlarmName": "unexpected-log-alarm"}],
    }
    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_survivors"):
        owner.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )
    assert all(client.closed for client in clients)


def test_transaction_search_projection_accepts_aws_response_without_optional_actual_percentage() -> None:
    assert owner._transaction_search_projection(
        destination="CloudWatchLogs",
        indexing_rules=[
            {
                "Name": "Default",
                "Rule": {"Probabilistic": {"DesiredSamplingPercentage": 1.0}},
            }
        ],
        spans_log_group_present=True,
    ) == {
        "destination": "CloudWatchLogs",
        "indexing_rules": [{"name": "Default", "desired_sampling_percentage": 1.0}],
        "spans_log_group_present": True,
    }


def test_orphan_sweep_accepts_aws_response_without_optional_empty_indexing_rules(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    clients.xray.responses["get_indexing_rules"] = {}  # type: ignore[union-attr]

    receipt = owner.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=clients,
        environ={},
    )

    assert receipt["total_unapproved_survivors"] == 0
    assert all(client.closed for client in clients)


def test_orphan_sweep_queries_exact_retained_metric_trace_and_transaction_search_identities(tmp_path: Path) -> None:
    trace_id = f"1-12345678-{'a' * 24}"

    def add_retained_identities(receipt: dict[str, object]) -> None:
        scenarios = receipt["scenarios"]
        assert isinstance(scenarios, dict)
        scenario_a = scenarios["A"]
        assert isinstance(scenario_a, dict)
        scenario_a["cloudwatch_retained_metrics"] = [
            {
                "namespace": "ELSPETH/Acceptance",
                "metric_name": "CompletedRuns",
                "dimensions": [
                    {
                        "name": "elspeth.acceptance.namespace",
                        "value": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-a",
                    },
                ],
            }
        ]
        scenario_a["xray_retained_trace_ids"] = [trace_id]
        scenario_a["expected_retained_metric_series"] = 1
        scenario_a["expected_retained_trace_ids"] = 1

    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, retained_mutator=add_retained_identities)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    receipt = owner.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=clients,
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )

    assert receipt["expected_retained"] == {"metric_series": 2, "trace_ids": 2}
    assert receipt["observed_retained"] == {"metric_series": 2, "trace_ids": 2}
    assert (
        "list_metrics",
        {
            "Namespace": "ELSPETH/Acceptance",
            "MetricName": "CompletedRuns",
            "Dimensions": [
                {
                    "Name": "elspeth.acceptance.namespace",
                    "Value": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-a",
                },
            ],
            "IncludeLinkedAccounts": False,
        },
    ) in clients.cloudwatch.calls  # type: ignore[union-attr]
    assert ("batch_get_traces", {"TraceIds": [trace_id]}) in clients.xray.calls  # type: ignore[union-attr]
    assert [method for method, _kwargs in clients.xray.calls].count("get_trace_segment_destination") == 2  # type: ignore[union-attr]
    assert [method for method, _kwargs in clients.xray.calls].count("get_indexing_rules") == 2  # type: ignore[union-attr]
    assert all(
        kwargs == {}
        for method, kwargs in clients.xray.calls  # type: ignore[union-attr]
        if method == "get_indexing_rules"
    )
    assert any(
        method == "describe_log_groups" and kwargs.get("logGroupNamePrefix") == "aws/spans"
        for method, kwargs in clients.logs.calls  # type: ignore[union-attr]
    )


def test_orphan_sweep_rejects_transaction_search_drift(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    clients.xray.responses["get_trace_segment_destination"] = {"Destination": "CloudWatchLogs"}  # type: ignore[union-attr]

    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_survivors"):
        owner.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )
    assert all(client.closed for client in clients)


def test_orphan_sweep_rejects_same_count_transaction_rule_drift_and_extra_retained_series(tmp_path: Path) -> None:
    def configure(inventory: dict[str, object], _scenario: str) -> None:
        orphan = inventory["orphan_sweep"]
        assert isinstance(orphan, dict)
        orphan["transaction_search_baseline_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    "destination": None,
                    "indexing_rules": [{"name": "Default", "desired_sampling_percentage": 1.0}],
                    "spans_log_group_present": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    manifest_path = tmp_path / "control.json"
    _init_control_manifest(
        manifest_path,
        inventory_mutator=configure,
        preapply_inventory_mutator=configure,
    )
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    clients.xray.responses["get_indexing_rules"] = {  # type: ignore[union-attr]
        "IndexingRules": [
            {
                "Name": "Default",
                "Rule": {"Probabilistic": {"DesiredSamplingPercentage": 2.0, "ActualSamplingPercentage": 2.0}},
            }
        ]
    }
    clients.cloudwatch.responses["list_metrics"] = {  # type: ignore[union-attr]
        "Metrics": [
            {"Namespace": "ELSPETH/Acceptance", "MetricName": "CompletedRuns", "Dimensions": []},
            {"Namespace": "ELSPETH/Acceptance", "MetricName": "CompletedRuns", "Dimensions": [{"Name": "Extra"}]},
        ]
    }
    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_survivors"):
        owner.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )


def test_orphan_sweep_rejects_tagged_survivor_and_endpoint_override_without_leaking_identity(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients(tagged=[{"ResourceARN": "arn:aws:ecs:region:account:secret-survivor"}])
    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_survivors") as raised:
        owner.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )
    assert "secret-survivor" not in str(raised.value)
    assert all(client.closed for client in clients)

    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_environment"):
        owner.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=_empty_orphan_clients(),
            environ={"AWS_ENDPOINT_URL_ECS": "https://example.invalid"},
        )


def test_orphan_sweep_deletes_ecr_tags_and_moves_owned_active_task_definition_to_tracked_deletion(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    namespace = acceptance.scenario_resource_namespace("4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48", "A")
    task_definition_arn = f"arn:aws:ecs:ap-southeast-2:123456789012:task-definition/acceptance-{namespace}:1"
    clients = _empty_orphan_clients(tagged=[{"ResourceARN": task_definition_arn}])
    clients.ecs.responses.update(  # type: ignore[union-attr]
        {
            "list_task_definitions": [
                {"taskDefinitionArns": [task_definition_arn]},
                {"taskDefinitionArns": []},
                {"taskDefinitionArns": []},
                {"taskDefinitionArns": []},
                {"taskDefinitionArns": []},
                {"taskDefinitionArns": [task_definition_arn]},
                *[{"taskDefinitionArns": []} for _ in range(6)],
            ],
            "describe_task_definition": {
                "taskDefinition": {"taskDefinitionArn": task_definition_arn},
                "tags": [{"key": "ACCEPTANCE_RUN_ID", "value": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48"}],
            },
            "deregister_task_definition": {"taskDefinition": {"status": "INACTIVE"}},
            "delete_task_definitions": {"taskDefinitions": [{"status": "DELETE_IN_PROGRESS"}], "failures": []},
        }
    )
    clients.ecr.responses.update(  # type: ignore[union-attr]
        {
            "describe_images": [
                {"imageDetails": [{"imageTags": ["baseline"]}]},
                {"imageDetails": []},
                {"imageDetails": [{"imageTags": ["candidate"]}]},
                {"imageDetails": []},
            ],
            "batch_delete_image": {"imageIds": [{"imageDigest": "sha256:opaque"}], "failures": []},
        }
    )

    receipt = owner.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=clients,
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )

    assert receipt["total_unapproved_survivors"] == 0
    deletion_receipts = receipt["delete_in_progress_receipts"]
    assert isinstance(deletion_receipts, list) and len(deletion_receipts) == 1
    assert task_definition_arn not in json.dumps(receipt)
    ecr_methods = [method for method, _kwargs in clients.ecr.calls]  # type: ignore[union-attr]
    assert ecr_methods == [
        "describe_images",
        "batch_delete_image",
        "describe_images",
        "describe_images",
        "batch_delete_image",
        "describe_images",
    ]


def test_orphan_sweep_rejects_task_definition_family_prefix_collision(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    clients.ecs.responses["list_task_definitions"] = [  # type: ignore[union-attr]
        {
            "taskDefinitionArns": [
                "arn:aws:ecs:ap-southeast-2:123456789012:task-definition/acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-a-foreign:1"
            ]
        }
    ]

    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_binding"):
        owner.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )
    assert all(client.closed for client in clients)


def test_orphan_sweep_rejects_repeated_pagination_token_and_closes_clients(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    clients.tagging.responses["get_resources"] = [  # type: ignore[union-attr]
        {"ResourceTagMappingList": [], "PaginationToken": "repeat"},
        {"ResourceTagMappingList": [], "PaginationToken": "repeat"},
    ]

    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_api"):
        owner.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )
    assert all(client.closed for client in clients)
