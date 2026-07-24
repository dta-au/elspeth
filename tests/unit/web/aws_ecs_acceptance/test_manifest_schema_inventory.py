"""Ownership tests for acceptance manifest and inventory validation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._aws_ecs_acceptance import contracts, manifest_schema, scenario_inventory
from tests.unit.web.aws_ecs_acceptance.test_bedrock_guardrails import _guardrail_env


def test_manifest_schema_and_scenario_inventory_modules_exist() -> None:
    assert importlib.util.find_spec("elspeth.web._aws_ecs_acceptance.manifest_schema") is not None
    assert importlib.util.find_spec("elspeth.web._aws_ecs_acceptance.scenario_inventory") is not None


def test_manifest_and_inventory_owners_are_facade_reexports_by_identity() -> None:
    for name in (
        "_load_retained_evidence",
        "_read_control_manifest",
        "_require_mutable_control_manifest",
        "_validate_control_manifest",
        "_validate_retained_evidence_receipt",
    ):
        assert getattr(acceptance, name) is getattr(manifest_schema, name)

    for name in (
        "_load_bound_scenario_inventory",
        "_load_preapply_scenario_inventory",
        "_scenario_inventory_hash",
        "_validate_scenario_inventory",
        "_validate_scenario_inventory_isolation",
        "_validate_tf_binding_receipt",
    ):
        assert getattr(acceptance, name) is getattr(scenario_inventory, name)

    for name in ("PLUGIN_POLICY_ASSIGNMENT_NAMES", "SCENARIO_ASSIGNMENT_NAMES"):
        assert getattr(acceptance, name) is getattr(scenario_inventory, name)
        assert getattr(scenario_inventory, name) is getattr(contracts, name)


def _terraform_receipt(*, kind: str = "terraform-plan", deletes: int = 0) -> dict[str, object]:
    return {
        "schema": "elspeth.aws-ecs-sanitized-evidence.v1",
        "kind": kind,
        "projection": {
            "resource_change_count": deletes,
            "create_count": 0,
            "update_count": 0,
            "delete_count": deletes,
            "replace_count": 0,
            "no_op_count": 0,
            "has_delete": deletes > 0,
            "has_replace": False,
        },
    }


def _scenario_inventory(
    run_id: str,
    scenario_id: str,
    binding: str,
    binding_file: str,
    *,
    phase: str = "resolved",
) -> dict[str, object]:
    values = {name: "" for name in acceptance.SCENARIO_ASSIGNMENT_NAMES if name not in {"ACTIVE_SCENARIO_ID", "ACCEPTANCE_RUN_ID"}}
    namespace = acceptance.scenario_resource_namespace(run_id, scenario_id)
    account = "123456789012"
    region = "ap-southeast-2"
    task_families = [f"acceptance-{namespace}"]
    task_arns = [f"arn:aws:ecs:{region}:{account}:task-definition/{task_families[0]}:{revision}" for revision in range(1, 7)]
    load_balancer_suffix = f"app/{namespace}-alb/0123456789abcdef"
    listener_arn = f"arn:aws:elasticloadbalancing:{region}:{account}:listener/{load_balancer_suffix}/0123456789abcdef"
    listener_rule_arn = (
        f"arn:aws:elasticloadbalancing:{region}:{account}:listener-rule/{load_balancer_suffix}/0123456789abcdef/0123456789abcdef"
    )
    log_groups = [
        f"/aws/ecs/{namespace}-web",
        f"/aws/ecs/{namespace}-doctor",
        f"/aws/events/{namespace}-deployments",
        f"/aws/ecs/{namespace}-operator-metrics",
    ]
    values.update(
        {
            "DEPLOYMENT_MODE": "first" if scenario_id == "A" else "upgrade",
            "TARGET_PLATFORM": "linux/amd64",
            "AWS_REGION": region,
            "ECS_CLUSTER": f"acceptance-{namespace}-cluster",
            "ECS_SERVICE": f"acceptance-{namespace}-service",
            "WEB_CONTAINER_NAME": "elspeth-web",
            "ELSPETH_WEB__DATA_DIR": "/var/lib/elspeth",
            "ELSPETH_WEB__PAYLOAD_STORE_PATH": "/var/lib/elspeth/payloads",
            "ELSPETH_WEB__COMPOSER_MODEL": "openrouter/openai/gpt-5.4",
            "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL": "openrouter/anthropic/claude-opus-4.6",
            "ELSPETH_BEDROCK_LIVE_TEST_MODEL": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
            "TARGET_GROUP_ARN": f"arn:aws:elasticloadbalancing:{region}:{account}:targetgroup/{namespace}-target/0123456789abcdef",
            "ALB_BASE_URL": f"https://{namespace}.example.invalid",
            "ALB_ARN": f"arn:aws:elasticloadbalancing:{region}:{account}:loadbalancer/app/{namespace}-alb/0123456789abcdef",
            "CANDIDATE_TASK_DEFINITION": task_arns[0],
            "DOCTOR_TASK_DEFINITION": task_arns[1],
            "DOCTOR_CONTAINER_NAME": "doctor",
            "DOCTOR_NETWORK_CONFIGURATION": json.dumps(
                {
                    "awsvpcConfiguration": {
                        "subnets": [f"subnet-0123456789abcde{scenario_id.lower()}"],
                        "securityGroups": [f"sg-0123456789abcde{scenario_id.lower()}"],
                        "assignPublicIp": "DISABLED",
                    }
                },
                separators=(",", ":"),
            ),
            "PAYLOAD_VERIFIER_TASK_DEFINITION": task_arns[2],
            "LOCAL_AUTH_VERIFIER_TASK_DEFINITION": task_arns[3],
            "WEB_LOG_GROUP": log_groups[0],
            "WEB_LOG_STREAM_PREFIX": "web",
            "DOCTOR_LOG_GROUP": log_groups[1],
            "DOCTOR_LOG_STREAM_PREFIX": "doctor",
            "OPERATOR_METRICS_LOG_GROUP": log_groups[3],
            "ECS_DEPLOYMENT_EVENT_RULE": f"{namespace}-deployments",
            "ECS_DEPLOYMENT_EVENT_TARGET_ID": f"{namespace}-deployment-log",
            "ECS_DEPLOYMENT_EVENT_LOG_GROUP": log_groups[2],
            "DB_CLUSTER_IDENTIFIER": f"{namespace}-aurora",
            "ELSPETH_TEST_S3_BUCKET": f"elspeth-{namespace}",
            "SCENARIO_TF_DIR": f"/iac/scenario-{scenario_id.lower()}",
            "SCENARIO_TF_VARS": f"/iac/scenario-{scenario_id.lower()}.tfvars",
            "SCENARIO_TF_BINDING_SHA": binding,
            "SCENARIO_TF_BINDING_FILE": binding_file,
            "OIDC_EXPECTED_AUDIENCE_CLAIM": "client_id",
        }
    )
    policy_env = _guardrail_env()
    scenario_profiles = json.loads(policy_env["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"])
    compact_namespace = namespace.replace("-", "")
    for profile in scenario_profiles:
        profile["guardrail_identifier"] = (
            f"{compact_namespace}{'prompt' if profile['plugin'] == 'aws_bedrock_prompt_shield' else 'content'}"
        )
    policy_env["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"] = json.dumps(scenario_profiles, separators=(",", ":"))
    values.update({name: policy_env[name] for name in acceptance.PLUGIN_POLICY_ASSIGNMENT_NAMES})
    values["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"] = acceptance.plugin_policy_binding_sha256(values)
    values.update(
        {
            "FIRST_DEPLOY_LISTENER_RULE_ARN": listener_rule_arn,
            "FIRST_DEPLOY_FORWARD_ACTIONS": json.dumps(
                [{"Type": "forward", "TargetGroupArn": values["TARGET_GROUP_ARN"]}], separators=(",", ":")
            ),
            "FIRST_DEPLOY_DISABLED_ACTIONS": '[{"Type":"fixed-response","FixedResponseConfig":{"StatusCode":"503"}}]',
        }
    )
    if scenario_id == "B":
        pool_id = f"{region}_AbCd1234"
        values.update(
            {
                "PREVIOUS_TASK_DEFINITION": task_arns[5],
                "ROLLBACK_DOCTOR_TASK_DEFINITION": task_arns[4],
                "COGNITO_USER_POOL_ID": pool_id,
                "OIDC_EXPECTED_ISSUER": f"https://cognito-idp.{region}.amazonaws.com/{pool_id}",
                "OIDC_EXPECTED_AUDIENCE": "1234567890abcdefghijklmnop",
                "OIDC_EXPECTED_AUTHORIZATION_ORIGIN": f"https://{namespace}.auth.{region}.amazoncognito.com",
            }
        )
    if phase == "preapply":
        for field in (
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
        ):
            values[field] = ""
        values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"] = "[]"
        values["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"] = acceptance.plugin_policy_binding_sha256(values)
    guardrail_profiles = json.loads(values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"])
    bedrock_guardrails = [
        {
            "identifier": profile["guardrail_identifier"],
            "versions": [profile["guardrail_version"]],
        }
        for profile in guardrail_profiles
    ]
    return {
        "schema": "elspeth.aws-ecs-scenario-inventory.v6",
        "acceptance_run_id": run_id,
        "candidate_sha": "c" * 40,
        "aws_account_id": account,
        "aws_region": region,
        "scenario_id": scenario_id,
        "phase": phase,
        "values": values,
        "orphan_sweep": {
            "tag_key": "ACCEPTANCE_RUN_ID",
            "cleanup_owner": "aws-acceptance-owner",
            "ecs_task_definition_families": task_families,
            "elbv2_listener_arns": [listener_arn] if phase == "resolved" else [],
            "rds_db_instance_identifiers": [f"{namespace}-aurora-1"],
            "efs_creation_tokens": [f"{namespace}-efs"],
            "efs_file_system_ids": [f"fs-0123456789abcde{scenario_id.lower()}"] if phase == "resolved" else [],
            "efs_access_point_ids": [f"fsap-0123456789abcde{scenario_id.lower()}"] if phase == "resolved" else [],
            "secret_ids": [
                f"{namespace}-database-runtime",
                f"{namespace}-database-schema",
                f"{namespace}-database-bootstrap",
                f"{namespace}-openrouter-composer",
            ],
            "iam_role_names": [f"{namespace}-task-role", f"{namespace}-execution-role"],
            "log_group_names": log_groups,
            "log_resource_policy_names": [f"{namespace}-delivery-policy"],
            "cloudwatch_dashboard_names": [f"{namespace}-dashboard"],
            "cloudwatch_alarm_names": [f"{namespace}-alarm"],
            "cloudwatch_retained_metrics": [],
            "xray_group_names": [f"{namespace}-xray"],
            "xray_sampling_rule_names": [f"{namespace}-sampling"],
            "xray_retained_trace_ids": [],
            "transaction_search_baseline_sha256": hashlib.sha256(
                json.dumps(
                    {
                        "destination": None,
                        "indexing_rules": [],
                        "spans_log_group_present": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "event_rules": [
                {
                    "event_bus_name": "default",
                    "rule_name": f"{namespace}-deployments",
                    "target_ids": [f"{namespace}-deployment-log"],
                }
            ],
            "bedrock_guardrails": bedrock_guardrails,
            "cognito_subject_sub": "subject-1234" if scenario_id == "B" and phase == "resolved" else "",
            "cognito_pool_owned": scenario_id == "B" and phase == "resolved",
            "expected_retained_metric_series": 0,
            "expected_retained_trace_ids": 0,
        },
    }


def _init_control_manifest(
    path: Path,
    *,
    run_id: str = "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
    deadline: str = "2026-07-14T05:00:00Z",
    inventory_mutator: Callable[[dict[str, object], str], None] | None = None,
    preapply_inventory_mutator: Callable[[dict[str, object], str], None] | None = None,
    retained_mutator: Callable[[dict[str, object]], None] | None = None,
    binding_mutator: Callable[[dict[str, object], str], None] | None = None,
    bind_resolved: bool = True,
    bind_retained: bool = True,
    prepare_apply_evidence: bool = True,
) -> dict[str, object]:
    scenario_a = path.parent / "scenario-a.json"
    scenario_b = path.parent / "scenario-b.json"
    scenario_a_preapply = path.parent / "scenario-a-preapply.json"
    scenario_b_preapply = path.parent / "scenario-b-preapply.json"
    bindings: dict[str, str] = {}
    for inventory_path, preapply_path, scenario in (
        (scenario_a, scenario_a_preapply, "A"),
        (scenario_b, scenario_b_preapply, "B"),
    ):
        binding_path = path.parent / f"tf-binding-{scenario.lower()}.json"
        binding_receipt = {
            "schema": "elspeth.aws-ecs-tf-binding.v1",
            "acceptance_run_id": run_id,
            "scenario_id": scenario,
            "repository_commit": ("a" if scenario == "A" else "b") * 40,
            "terraform_lock_sha256": ("c" if scenario == "A" else "d") * 64,
            "terraform_version": "1.9.0",
            "backend_type": "s3",
            "backend_encrypted": True,
            "backend_locked": True,
            "backend_state_key_sha256": hashlib.sha256(f"state-{scenario}".encode()).hexdigest(),
            "workspace": f"acceptance-{scenario.lower()}",
            "aws_account_id": "123456789012",
            "aws_region": "ap-southeast-2",
            "vars_sha256": ("e" if scenario == "A" else "f") * 64,
        }
        if binding_mutator is not None:
            binding_mutator(binding_receipt, scenario)
        binding_path.write_text(json.dumps(binding_receipt))
        os.chmod(binding_path, 0o600)
        binding = hashlib.sha256(json.dumps(binding_receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        bindings[scenario] = binding
        preapply_inventory = _scenario_inventory(run_id, scenario, binding, str(binding_path), phase="preapply")
        if preapply_inventory_mutator is not None:
            preapply_inventory_mutator(preapply_inventory, scenario)
        preapply_path.write_text(json.dumps(preapply_inventory))
        os.chmod(preapply_path, 0o600)
        inventory = _scenario_inventory(run_id, scenario, binding, str(binding_path), phase="resolved")
        if inventory_mutator is not None:
            inventory_mutator(inventory, scenario)
        inventory_path.write_text(json.dumps(inventory))
        os.chmod(inventory_path, 0o600)
    acceptance.control_manifest_init(
        path,
        acceptance_run_id=run_id,
        candidate_sha="c" * 40,
        aws_account_id="123456789012",
        aws_region="ap-southeast-2",
        scenario_a_inventory=str(scenario_a_preapply),
        scenario_b_inventory=str(scenario_b_preapply),
        scenario_a_tf_binding=bindings["A"],
        scenario_b_tf_binding=bindings["B"],
        evidence_destination_sha256="9" * 64,
        gate_ledger=str(path.parent / "gate-ledger.json"),
        teardown_deadline_utc=deadline,
        now=lambda: datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
    )
    if prepare_apply_evidence:
        for scenario, plan_character, noop_character in (("A", "1", "3"), ("B", "2", "4")):
            plan_sha = plan_character * 64
            plan_path = path.parent / f"{scenario.lower()}-plan-receipt.json"
            plan_path.write_text(json.dumps(_terraform_receipt()))
            os.chmod(plan_path, 0o600)
            plan_receipt_hash = acceptance.receipt_store(
                path,
                scenario_id=scenario,
                kind="terraform-plan",
                subject_id=plan_sha,
                receipt_file=plan_path,
                now=lambda: datetime(2026, 7, 14, 1, 0, 10, tzinfo=UTC),
            )
            approval_path = path.parent / f"{scenario.lower()}-plan-approval.json"
            approval_path.write_text(
                json.dumps(
                    {
                        "schema": "elspeth.aws-ecs-approval.v1",
                        "acceptance_run_id": run_id,
                        "scenario_id": scenario,
                        "kind": "terraform-plan",
                        "plan_receipt_hash": plan_receipt_hash,
                        "approver_identity": "infrastructure-owner",
                        "authority": "terraform-apply",
                        "decision": "approved",
                        "approved_at": "2026-07-14T01:00:00Z",
                        "expires_at": "2026-07-14T04:00:00Z",
                        "key_id": "owner-key-1",
                        "signature": "opaque-signature",
                    }
                )
            )
            os.chmod(approval_path, 0o600)
            approval_hash = acceptance.approval_verify(
                path,
                scenario_id=scenario,
                kind="terraform-plan",
                plan_receipt_hash=plan_receipt_hash,
                approval_file=approval_path,
                signature_verifier=lambda _payload, _signature, _key_id: True,
                now=lambda: datetime(2026, 7, 14, 1, 0, 20, tzinfo=UTC),
            )
            plan_binding = f"{scenario}:{plan_sha}:{plan_receipt_hash}:{approval_hash}"
            acceptance.control_manifest_update(
                path,
                terraform_plan_receipt=plan_binding,
                now=lambda: datetime(2026, 7, 14, 1, 0, 30, tzinfo=UTC),
            )
            acceptance.control_manifest_update(
                path,
                terraform_applied=plan_binding,
                now=lambda: datetime(2026, 7, 14, 1, 0, 40, tzinfo=UTC),
            )
            noop_sha = noop_character * 64
            noop_path = path.parent / f"{scenario.lower()}-noop-receipt.json"
            noop_path.write_text(json.dumps(_terraform_receipt()))
            os.chmod(noop_path, 0o600)
            noop_receipt_hash = acceptance.receipt_store(
                path,
                scenario_id=scenario,
                kind="terraform-noop",
                subject_id=noop_sha,
                receipt_file=noop_path,
                now=lambda: datetime(2026, 7, 14, 1, 0, 50, tzinfo=UTC),
            )
            acceptance.control_manifest_update(
                path,
                terraform_noop_receipt=f"{scenario}:{noop_sha}:{noop_receipt_hash}",
                now=lambda: datetime(2026, 7, 14, 1, 0, 55, tzinfo=UTC),
            )
    if bind_resolved:
        acceptance.control_manifest_bind_scenario(
            path,
            scenario_id="A",
            inventory_path=str(scenario_a),
            now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
        )
        acceptance.control_manifest_bind_scenario(
            path,
            scenario_id="B",
            inventory_path=str(scenario_b),
            now=lambda: datetime(2026, 7, 14, 1, 1, 10, tzinfo=UTC),
        )
    if bind_resolved and bind_retained:
        retained_evidence: dict[str, object] = {
            "schema": "elspeth.aws-ecs-retained-evidence.v1",
            "acceptance_run_id": run_id,
            "candidate_sha": "c" * 40,
            "scenarios": {
                scenario: {
                    "cloudwatch_retained_metrics": [
                        {
                            "namespace": "ELSPETH/Acceptance",
                            "metric_name": "CompletedRuns",
                            "dimensions": [
                                {"name": "elspeth.acceptance.namespace", "value": f"{run_id}-{scenario.lower()}"},
                            ],
                        }
                    ],
                    "xray_retained_trace_ids": [f"1-1234567{0 if scenario == 'A' else 1}-{'a' if scenario == 'A' else 'b'}" + "0" * 23],
                    "expected_retained_metric_series": 1,
                    "expected_retained_trace_ids": 1,
                }
                for scenario in ("A", "B")
            },
            "captured_at": "2026-07-14T01:01:20Z",
        }
        if retained_mutator is not None:
            retained_mutator(retained_evidence)
        retained_path = path.parent / "retained-evidence.json"
        retained_path.write_text(json.dumps(retained_evidence))
        os.chmod(retained_path, 0o600)
        acceptance.control_manifest_bind_retained_evidence(
            path,
            receipt_path=str(retained_path),
            now=lambda: datetime(2026, 7, 14, 1, 1, 30, tzinfo=UTC),
        )
    return json.loads(path.read_text())


def test_control_manifest_init_update_validate_get_and_cleanup_assignments_are_closed_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    manifest = _init_control_manifest(path)

    assert path.stat().st_mode & 0o777 == 0o600
    assert manifest["schema"] == "elspeth.aws-ecs-control-manifest.v5"
    acceptance.control_manifest_validate(
        path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        candidate_sha="c" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    updated = acceptance.control_manifest_update(
        path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    assert updated["cleanup_required"] is True
    assert acceptance.control_manifest_get(path, "cleanup_states.orphan_sweep") == "pending"
    assignments = acceptance.control_manifest_load_cleanup(
        path,
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )
    assert assignments.splitlines() == [
        "ACCEPTANCE_REENTRY_FORBIDDEN=0",
        "ACCEPTANCE_RUN_ID=4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        "ACCEPTANCE_TEARDOWN_DEADLINE_UTC=2026-07-14T05:00:00Z",
        "AWS_ACCOUNT_ID=123456789012",
        "AWS_REGION=ap-southeast-2",
        "CANDIDATE_SHA=cccccccccccccccccccccccccccccccccccccccc",
        "CANDIDATE_TAG=acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        "CLEANUP_REQUIRED=1",
        "DEADLINE_EXPIRED=0",
        "ELSPETH_CLEANUP_MODE=1",
        "ECR_REGISTRY=123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        "ECR_REPOSITORY=elspeth-acceptance",
        "EMERGENCY_CLEANUP_DEADLINE_UTC=''",
        f"GATE_LEDGER={tmp_path}/gate-ledger.json",
        "ROLLBACK_BASELINE_TAG=acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        "ROLLBACK_BASELINE_DIGEST=''",
        "IMAGE_DIGEST=''",
        "ROLLBACK_BASELINE_IMAGE=''",
        "CANDIDATE_IMAGE=''",
        "ACCEPTANCE_STATE=''",
        "OIDC_EVIDENCE_DIR=''",
        f"EVIDENCE_DESTINATION_SHA256={'9' * 64}",
        "EVIDENCE_EXPORT_RECEIPT=''",
        "FINAL_EVIDENCE_EXPORT_RECEIPT=''",
        f"SCENARIO_A_INVENTORY={tmp_path}/scenario-a.json",
        "SCENARIO_A_TF_DIR=/iac/scenario-a",
        "SCENARIO_A_TF_VARS=/iac/scenario-a.tfvars",
        f"SCENARIO_A_TF_BINDING_SHA={manifest['scenarios']['A']['tf_binding_sha256']}",  # type: ignore[index]
        f"SCENARIO_A_TF_BINDING_FILE={tmp_path}/tf-binding-a.json",
        f"SCENARIO_B_INVENTORY={tmp_path}/scenario-b.json",
        "SCENARIO_B_TF_DIR=/iac/scenario-b",
        "SCENARIO_B_TF_VARS=/iac/scenario-b.tfvars",
        f"SCENARIO_B_TF_BINDING_SHA={manifest['scenarios']['B']['tf_binding_sha256']}",  # type: ignore[index]
        f"SCENARIO_B_TF_BINDING_FILE={tmp_path}/tf-binding-b.json",
    ]


def test_control_manifest_deadline_blocks_acceptance_but_records_and_permits_cleanup_only_resume(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    _init_control_manifest(path, deadline="2026-07-14T02:00:00Z")
    acceptance.control_manifest_update(
        path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 30, tzinfo=UTC),
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_expired"):
        acceptance.control_manifest_validate(
            path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            candidate_sha="c" * 40,
            now=lambda: datetime(2026, 7, 14, 2, 1, tzinfo=UTC),
        )
    assignments = acceptance.control_manifest_load_cleanup(
        path,
        now=lambda: datetime(2026, 7, 14, 2, 1, tzinfo=UTC),
    )
    assert "DEADLINE_EXPIRED=1" in assignments
    assert "ELSPETH_CLEANUP_MODE=1" in assignments
    assert "EMERGENCY_CLEANUP_DEADLINE_UTC=2026-07-14T05:01:00Z" in assignments
    assert "ACCEPTANCE_REENTRY_FORBIDDEN=1" in assignments
    assert acceptance.control_manifest_get(path, "deadline_failure_recorded") == "true"
    assert acceptance.control_manifest_get(path, "verdict_failures") == '["teardown_deadline"]'
    assert acceptance.control_manifest_get(path, "cleanup_escalations") == '["teardown_deadline"]'

    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_conflict"):
        acceptance.control_manifest_update(
            path,
            verdict_failure="teardown_deadline",
            emergency_cleanup_deadline_utc="2026-07-14T03:32:00Z",
            cleanup_escalation="teardown_deadline",
            now=lambda: datetime(2026, 7, 14, 2, 1, 30, tzinfo=UTC),
        )

    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_expired"):
        acceptance.control_manifest_update(
            path,
            cleanup_required=True,
            ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
            ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-too-late",
            ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
            ecr_repository="elspeth-acceptance",
            now=lambda: datetime(2026, 7, 14, 2, 2, tzinfo=UTC),
        )
    acceptance.control_manifest_update(
        path,
        cleanup_checkpoint="orphan_sweep:confirmed",
        now=lambda: datetime(2026, 7, 14, 2, 2, tzinfo=UTC),
    )
    assert acceptance.control_manifest_get(path, "cleanup_states.orphan_sweep") == "confirmed"


def test_control_manifest_rejects_existing_init_symlink_permissive_and_wrong_binding(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    _init_control_manifest(path)
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_exists"):
        _init_control_manifest(path)
    os.chmod(path, 0o644)
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_file"):
        acceptance.control_manifest_get(path, "candidate_sha")

    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_file"):
        acceptance.control_manifest_get(link, "candidate_sha")

    os.chmod(path, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_binding"):
        acceptance.control_manifest_validate(
            path,
            acceptance_run_id="64b984d2-b617-42f7-ac4f-c0955ea9aadc",
            candidate_sha="c" * 40,
            now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
        )


def test_control_manifest_rejects_shared_terraform_state_and_foreign_scenario_resource(tmp_path: Path) -> None:
    def share_state(receipt: dict[str, object], scenario: str) -> None:
        if scenario == "B":
            receipt["backend_state_key_sha256"] = hashlib.sha256(b"state-A").hexdigest()
            receipt["workspace"] = "acceptance-a"

    with pytest.raises(acceptance.AcceptanceCheckError, match="tf_binding_binding"):
        _init_control_manifest(tmp_path / "shared-state.json", binding_mutator=share_state)

    def foreign_arn(inventory: dict[str, object], scenario: str) -> None:
        if scenario == "A":
            values = inventory["values"]
            assert isinstance(values, dict)
            values["TARGET_GROUP_ARN"] = "arn:aws:elasticloadbalancing:us-east-1:999999999999:targetgroup/foreign/0123456789abcdef"

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        _init_control_manifest(tmp_path / "foreign-resource.json", inventory_mutator=foreign_arn)


@pytest.mark.parametrize(
    "authorization_origin",
    [
        pytest.param(
            "https://operator:secret@elspeth-acceptance-example-b.auth.ap-southeast-2.amazoncognito.com",
            id="userinfo",
        ),
        pytest.param(
            "https://elspeth-acceptance-example-b.auth.ap-southeast-2.amazoncognito.com:444",
            id="nonstandard-port",
        ),
        pytest.param(
            "https://elspeth-acceptance-example-b.auth.ap-southeast-2.amazoncognito.com:not-a-port",
            id="malformed-port",
        ),
    ],
)
def test_scenario_inventory_rejects_nonstandard_oidc_authorization_origin(
    tmp_path: Path,
    authorization_origin: str,
) -> None:
    def mutate_authorization_origin(inventory: dict[str, object], scenario: str) -> None:
        if scenario == "B":
            values = inventory["values"]
            assert isinstance(values, dict)
            namespace = acceptance.scenario_resource_namespace(inventory["acceptance_run_id"], scenario)
            values["OIDC_EXPECTED_AUTHORIZATION_ORIGIN"] = authorization_origin.replace(
                "elspeth-acceptance-example-b",
                namespace,
            )

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_schema"):
        _init_control_manifest(
            tmp_path / "unsafe-oidc-authorization-origin.json",
            inventory_mutator=mutate_authorization_origin,
        )


def test_tf_binding_rejects_non_ascii_terraform_version(tmp_path: Path) -> None:
    def non_ascii_version(receipt: dict[str, object], scenario: str) -> None:
        if scenario == "A":
            receipt["terraform_version"] = "1.9.0-ß"

    with pytest.raises(acceptance.AcceptanceCheckError, match="tf_binding_schema"):
        _init_control_manifest(tmp_path / "non-ascii-version.json", binding_mutator=non_ascii_version)

    def drift_policy_binding(inventory: dict[str, object], scenario: str) -> None:
        if scenario == "A":
            values = inventory["values"]
            assert isinstance(values, dict)
            values["ELSPETH_WEB__PLUGIN_ALLOWLIST"] = "[]"

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        _init_control_manifest(tmp_path / "policy-drift.json", inventory_mutator=drift_policy_binding)


def test_scenario_inventory_binds_listener_rule_to_its_parent_listener(tmp_path: Path) -> None:
    def replace_listener_with_rule(inventory: dict[str, object], scenario: str) -> None:
        if scenario != "A":
            return
        values = inventory["values"]
        orphan = inventory["orphan_sweep"]
        assert isinstance(values, dict)
        assert isinstance(orphan, dict)
        orphan["elbv2_listener_arns"] = [values["FIRST_DEPLOY_LISTENER_RULE_ARN"]]

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        _init_control_manifest(tmp_path / "rule-as-listener.json", inventory_mutator=replace_listener_with_rule)

    def omit_upgrade_listener(inventory: dict[str, object], scenario: str) -> None:
        if scenario == "B":
            orphan = inventory["orphan_sweep"]
            assert isinstance(orphan, dict)
            orphan["elbv2_listener_arns"] = []

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        _init_control_manifest(tmp_path / "missing-upgrade-listener.json", inventory_mutator=omit_upgrade_listener)


def test_scenario_load_is_exact_shell_round_trippable_and_rejects_inventory_drift(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    manifest = _init_control_manifest(path)
    assignments = acceptance.scenario_load(
        path,
        scenario_id="A",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    assert "OIDC_TEST_USERNAME" not in assignments
    assert "PASSWORD" not in assignments
    script = f"{assignments}\nprintf '%s\\n' \"$ACTIVE_SCENARIO_ID|$ECS_CLUSTER|$SCENARIO_TF_BINDING_SHA\""
    completed = subprocess.run(
        ["env", "-i", "bash", "--noprofile", "--norc", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    namespace = acceptance.scenario_resource_namespace("4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48", "A")
    assert completed.stdout == f"A|acceptance-{namespace}-cluster|{manifest['scenarios']['A']['tf_binding_sha256']}\n"  # type: ignore[index]

    inventory = tmp_path / "scenario-a.json"
    drifted = json.loads(inventory.read_text())
    drifted["values"]["ECS_CLUSTER"] = "unbound-drift"
    inventory.write_text(json.dumps(drifted))
    os.chmod(inventory, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        acceptance.scenario_load(
            path,
            scenario_id="A",
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )


def test_scenario_inventory_requires_atomic_preapply_to_resolved_binding(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    manifest = _init_control_manifest(path, bind_resolved=False)
    assert manifest["scenarios"]["A"]["inventory_phase"] == "preapply"  # type: ignore[index]
    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_unresolved"):
        acceptance.scenario_load(path, scenario_id="A", now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC))
    assert "SCENARIO_A_TF_DIR=/iac/scenario-a" in acceptance.control_manifest_load_cleanup(
        path, now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC)
    )

    resolved_path = tmp_path / "scenario-a.json"

    def bind_now() -> datetime:
        return datetime(2026, 7, 14, 1, 1, tzinfo=UTC)

    bound = acceptance.control_manifest_bind_scenario(path, scenario_id="A", inventory_path=str(resolved_path), now=bind_now)
    assert bound["scenarios"]["A"]["inventory_phase"] == "resolved"  # type: ignore[index]
    assert "ACTIVE_SCENARIO_ID=A" in acceptance.scenario_load(path, scenario_id="A", now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC))
    assert acceptance.control_manifest_bind_scenario(path, scenario_id="A", inventory_path=str(resolved_path), now=bind_now) == bound

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        acceptance.control_manifest_bind_scenario(
            path,
            scenario_id="A",
            inventory_path=str(tmp_path / "scenario-b.json"),
            now=bind_now,
        )


def test_scenario_inventory_resolves_real_provider_guardrail_profiles(tmp_path: Path) -> None:
    def preapply(inventory: dict[str, object], _scenario: str) -> None:
        values = inventory["values"]
        assert isinstance(values, dict)
        values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"] = "[]"
        values["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"] = acceptance.plugin_policy_binding_sha256(values)

    def resolved(inventory: dict[str, object], _scenario: str) -> None:
        values = inventory["values"]
        orphan = inventory["orphan_sweep"]
        assert isinstance(values, dict)
        assert isinstance(orphan, dict)
        profiles = json.loads(values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"])
        orphan["bedrock_guardrails"] = [
            {
                "identifier": profile["guardrail_identifier"],
                "versions": [profile["guardrail_version"]],
            }
            for profile in profiles
        ]

    manifest = _init_control_manifest(
        tmp_path / "guardrail-control.json",
        preapply_inventory_mutator=preapply,
        inventory_mutator=resolved,
    )

    assert manifest["scenarios"]["A"]["inventory_phase"] == "resolved"  # type: ignore[index]
    assert manifest["scenarios"]["B"]["inventory_phase"] == "resolved"  # type: ignore[index]


def test_scenario_inventory_rejects_guardrails_not_bound_to_policy_profiles(tmp_path: Path) -> None:
    def mismatched(inventory: dict[str, object], _scenario: str) -> None:
        orphan = inventory["orphan_sweep"]
        assert isinstance(orphan, dict)
        orphan["bedrock_guardrails"] = [{"identifier": "differentguardrail", "versions": ["1"]}]

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        _init_control_manifest(tmp_path / "mismatched-guardrails.json", inventory_mutator=mismatched)


def test_scenario_inventory_rejects_duplicate_profile_guardrail_binding(tmp_path: Path) -> None:
    def duplicate_profile_binding(inventory: dict[str, object], _scenario: str) -> None:
        values = inventory["values"]
        assert isinstance(values, dict)
        profiles = json.loads(values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"])
        profiles[1]["guardrail_identifier"] = profiles[0]["guardrail_identifier"]
        profiles[1]["guardrail_version"] = profiles[0]["guardrail_version"]
        values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"] = json.dumps(profiles, separators=(",", ":"))
        values["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"] = acceptance.plugin_policy_binding_sha256(values)

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        _init_control_manifest(tmp_path / "duplicate-profile-guardrail.json", inventory_mutator=duplicate_profile_binding)


def test_scenario_inventory_bind_requires_apply_evidence_deadline_and_preserved_preapply_contract(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing-evidence-control.json"
    _init_control_manifest(missing_path, bind_resolved=False, prepare_apply_evidence=False)
    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_unresolved"):
        acceptance.control_manifest_bind_scenario(
            missing_path,
            scenario_id="A",
            inventory_path=str(tmp_path / "scenario-a.json"),
            now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
        )

    expired_path = tmp_path / "expired-control.json"
    _init_control_manifest(expired_path, deadline="2026-07-14T02:00:00Z", bind_resolved=False)
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_expired"):
        acceptance.control_manifest_bind_scenario(
            expired_path,
            scenario_id="A",
            inventory_path=str(tmp_path / "scenario-a.json"),
            now=lambda: datetime(2026, 7, 14, 2, 1, tzinfo=UTC),
        )

    def drift_service(inventory: dict[str, object], scenario: str) -> None:
        if scenario == "A":
            values = inventory["values"]
            assert isinstance(values, dict)
            namespace = acceptance.scenario_resource_namespace("4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48", "A")
            values["ECS_SERVICE"] = f"acceptance-{namespace}-changed-service"

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_conflict"):
        _init_control_manifest(tmp_path / "drift-control.json", inventory_mutator=drift_service)

    preserved_path = tmp_path / "preserved-control.json"
    manifest = _init_control_manifest(preserved_path)
    scenario_a = manifest["scenarios"]["A"]
    assert scenario_a["preapply_inventory_path"].endswith("scenario-a-preapply.json")
    assert scenario_a["preapply_inventory_path"] != scenario_a["inventory_path"]
    assert len(scenario_a["preapply_inventory_sha256"]) == 64
    preapply_path = Path(scenario_a["preapply_inventory_path"])
    preapply = json.loads(preapply_path.read_text())
    preapply["values"]["DB_CLUSTER_IDENTIFIER"] += "-drift"
    preapply_path.write_text(json.dumps(preapply))
    os.chmod(preapply_path, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        acceptance.control_manifest_load_cleanup(
            preserved_path,
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )


def _retained_checkpoint(run_id: str, included: set[str], captured_at: str) -> dict[str, object]:
    return {
        "schema": "elspeth.aws-ecs-retained-evidence.v1",
        "acceptance_run_id": run_id,
        "candidate_sha": "c" * 40,
        "scenarios": {
            scenario: {
                "cloudwatch_retained_metrics": (
                    [
                        {
                            "namespace": "ELSPETH/Acceptance",
                            "metric_name": "CompletedRuns",
                            "dimensions": [{"name": "elspeth.acceptance.namespace", "value": f"{run_id}-{scenario.lower()}"}],
                        }
                    ]
                    if scenario in included
                    else []
                ),
                "xray_retained_trace_ids": (
                    [f"1-1234567{0 if scenario == 'A' else 1}-{'a' if scenario == 'A' else 'b'}" + "0" * 23] if scenario in included else []
                ),
                "expected_retained_metric_series": 1 if scenario in included else 0,
                "expected_retained_trace_ids": 1 if scenario in included else 0,
            }
            for scenario in ("A", "B")
        },
        "captured_at": captured_at,
    }


def test_complete_retained_evidence_requires_paired_metric_and_trace_counts(tmp_path: Path) -> None:
    run_id = "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48"
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_retained=False)
    checkpoint = _retained_checkpoint(run_id, {"A", "B"}, "2026-07-14T01:01:20Z")
    scenarios = checkpoint["scenarios"]
    assert isinstance(scenarios, dict)
    scenario_a = scenarios["A"]
    assert isinstance(scenario_a, dict)
    metrics = scenario_a["cloudwatch_retained_metrics"]
    assert isinstance(metrics, list)
    metrics.append(
        {
            "namespace": "ELSPETH/Acceptance",
            "metric_name": "CompletedRunsDuplicate",
            "dimensions": [
                {"name": "elspeth.acceptance.namespace", "value": f"{run_id}-a"},
            ],
        }
    )
    scenario_a["expected_retained_metric_series"] = 2
    receipt_path = tmp_path / "mismatched-retained.json"
    receipt_path.write_text(json.dumps(checkpoint))
    os.chmod(receipt_path, 0o600)

    with pytest.raises(acceptance.AcceptanceCheckError, match="retained_evidence_schema"):
        acceptance.control_manifest_bind_retained_evidence(
            manifest_path,
            receipt_path=str(receipt_path),
            require_complete=True,
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )
