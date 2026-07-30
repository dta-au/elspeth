"""Pure receipt contracts for the AWS ECS acceptance controller."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable

import httpx
import pytest

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._aws_ecs_acceptance import receipt_contracts
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH


def test_moved_public_receipt_contracts_are_facade_reexports_by_identity() -> None:
    for name in ("encode_exec_receipt", "extract_exec_receipt", "resolve_exec_receipt_env"):
        assert getattr(acceptance, name) is getattr(receipt_contracts, name)


def _s3_receipt_details() -> dict[str, object]:
    return {
        "object_count": 1,
        "source_sha256": "a" * 64,
        "sink_sha256": "a" * 64,
        "collision_rejected": True,
        "cleanup_succeeded": True,
    }


def _guardrail_receipt_details() -> dict[str, object]:
    return {
        "controls": [
            {
                "plugin_id": "aws_bedrock_prompt_shield",
                "profile_alias": "prompt-approved",
                "guardrail_version": "7",
                "safe_case_passed": True,
                "attack_case_blocked": True,
                "request_ids_present": True,
                "safe_text_sha256": "a" * 64,
                "blocked_text_sha256": "b" * 64,
                "checked_at": "2026-07-14T01:02:03Z",
            },
            {
                "plugin_id": "aws_bedrock_content_safety",
                "profile_alias": "content-approved",
                "guardrail_version": "11",
                "safe_case_passed": True,
                "attack_case_blocked": True,
                "request_ids_present": True,
                "safe_text_sha256": "c" * 64,
                "blocked_text_sha256": "d" * 64,
                "checked_at": "2026-07-14T01:02:03Z",
            },
        ],
        "plugin_policy": _plugin_policy_receipt(),
    }


def _plugin_policy_receipt(*, include_landscape: bool = True) -> dict[str, object]:
    receipt: dict[str, object] = {
        "policy_hash": "1" * 64,
        "snapshot_hash": "2" * 64,
        "binding_sha256": "3" * 64,
        "tutorial_profile_ready": True,
        "tutorial_ready": False,
        "tutorial_blocker": "tutorial_required_control_coverage",
        "tutorial_profile_alias": "tutorial",
        "target_llm": "transform:llm",
        "selected_controls": [
            {
                "capability": "prompt_shield",
                "plugin_id": "transform:aws_bedrock_prompt_shield",
                "profile_alias": "prompt-approved",
                "mode": "required",
            },
            {
                "capability": "content_safety",
                "plugin_id": "transform:aws_bedrock_content_safety",
                "profile_alias": "content-approved",
                "mode": "required",
            },
        ],
    }
    if include_landscape:
        receipt["landscape_evidence"] = True
    return receipt


def _operator_receipt_details(*, phase: str = "positive") -> dict[str, object]:
    positive = phase == "positive"
    return {
        "phase": phase,
        "metric_name": "operator.acceptance.sentinel",
        "trace_names": ["RunStarted", "RunFinished"],
        "observed_at": 1234.5,
        "resource": {
            "service_name": "elspeth-web",
            "service_version": "0.7.1",
            "deployment_environment": "acceptance",
            "cloud_provider": "aws",
        },
        "sentinel_sha256": "e" * 64,
        "landscape_terminal": True,
        "trace_terminal_agrees": True if positive else None,
        "collector_degraded": not positive,
        "cloud_receipt": positive,
        "retained_metric_query": (
            {
                "namespace": "ELSPETH/Operator",
                "metric_name": "operator.acceptance.sentinel",
                "dimensions": [
                    {"name": "service.name", "value": "elspeth-web"},
                    {
                        "name": "deployment.environment",
                        "value": "acceptance",
                    },
                    {"name": "service.version", "value": "0.7.1"},
                    {"name": "aws.ecs.cluster.name", "value": "cluster-a"},
                    {"name": "aws.ecs.service.name", "value": "service-a"},
                    {"name": "aws.ecs.task.family", "value": "elspeth-web"},
                    {"name": "aws.ecs.task.revision", "value": "17"},
                    {"name": "cloud.provider", "value": "aws"},
                    {
                        "name": "elspeth.acceptance.namespace",
                        "value": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-a",
                    },
                    {"name": "elspeth.acceptance.sentinel", "value": str(int(("e" * 64)[:12], 16))},
                ],
            }
            if positive
            else None
        ),
        "retained_trace_id": "1-12345670-a00000000000000000000000" if positive else None,
        "forbidden_content_absent": True,
    }


def _connection_budget_details() -> dict[str, object]:
    return {
        "schema": "elspeth.rds-connection-budget.v3",
        "acceptance_run_id_sha256": "b" * 64,
        "cluster_id_sha256": "a" * 64,
        "window_start": "2026-07-14T01:00:00Z",
        "window_end": "2026-07-14T01:10:00Z",
        "period_seconds": 60,
        "expected_points": 10,
        "points": [{"timestamp": f"2026-07-14T01:{minute:02d}:00Z", "count": 8.0} for minute in range(10)],
        "high_water": 8.0,
        "max_connections": 100,
        "approved_budget": 20,
        "safety_margin": 10,
        "ok": True,
    }


def _terraform_plan_receipt(plan_sha256: str) -> dict[str, object]:
    return {
        "schema": "elspeth.aws-ecs-sanitized-evidence.v2",
        "kind": "terraform-plan",
        "plan_sha256": plan_sha256,
        "projection": {
            "resource_change_count": 0,
            "create_count": 0,
            "update_count": 0,
            "delete_count": 0,
            "replace_count": 0,
            "no_op_count": 0,
            "has_delete": False,
            "has_replace": False,
        },
    }


def _receipt_env() -> dict[str, str]:
    return {
        "ELSPETH_ACCEPTANCE_CANDIDATE_SHA": "c" * 40,
        "ELSPETH_ACCEPTANCE_TASK_ARN": "arn:aws:ecs:ap-southeast-2:123456789012:task/cluster/private-task-id",
        "ELSPETH_ACCEPTANCE_SCENARIO_ID": "scenario-a",
    }


def test_exec_receipt_binding_resolves_exact_task_arn_from_ecs_v4_metadata_without_emitting_response() -> None:
    task_arn = "arn:aws:ecs:ap-southeast-2:123456789012:task/cluster/private-task-id"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"TaskARN": task_arn})

    env = {
        "ELSPETH_ACCEPTANCE_CANDIDATE_SHA": "c" * 40,
        "ELSPETH_ACCEPTANCE_SCENARIO_ID": "scenario-a",
        "ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/private-token",
    }
    resolved = acceptance.resolve_exec_receipt_env(env, transport=httpx.MockTransport(handler))
    assert resolved["ELSPETH_ACCEPTANCE_TASK_ARN"] == task_arn
    assert requests[0].url == "http://169.254.170.2/v4/private-token/task"

    sentinel = acceptance.encode_exec_receipt("verify-s3", _s3_receipt_details(), resolved)
    assert task_arn not in sentinel


@pytest.mark.parametrize(
    "metadata_uri",
    [
        "https://169.254.170.2/v4/token",
        "http://example.invalid/v4/token",
        "http://169.254.170.2/v3/token",
        "http://user@169.254.170.2/v4/token",
    ],
)
def test_exec_receipt_binding_rejects_non_ecs_metadata_origins(metadata_uri: str) -> None:
    with pytest.raises(acceptance.AcceptanceCheckError, match="exec_receipt_binding"):
        acceptance.resolve_exec_receipt_env({"ECS_CONTAINER_METADATA_URI_V4": metadata_uri}, transport=httpx.MockTransport(pytest.fail))


def test_exec_receipt_binding_rejects_caller_supplied_task_arn() -> None:
    with pytest.raises(acceptance.AcceptanceCheckError, match="exec_receipt_binding"):
        acceptance.resolve_exec_receipt_env(
            {
                "ELSPETH_ACCEPTANCE_TASK_ARN": "arn:aws:ecs:ap-southeast-2:123456789012:task/cluster/forged",
                "ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/private-token",
            },
            transport=httpx.MockTransport(pytest.fail),
        )


def test_exec_receipt_round_trip_binds_candidate_task_hash_scenario_and_check() -> None:
    env = _receipt_env()
    sentinel = acceptance.encode_exec_receipt("verify-s3", _s3_receipt_details(), env)

    assert sentinel.startswith("ELSPETH_ACCEPTANCE_RECEIPT_V1:")
    assert env["ELSPETH_ACCEPTANCE_TASK_ARN"] not in sentinel
    envelope = acceptance.extract_exec_receipt(
        f"Session Manager plugin banner\r\n{sentinel}\r\nExiting session\r\n",
        expected_candidate_sha=env["ELSPETH_ACCEPTANCE_CANDIDATE_SHA"],
        expected_task_arn=env["ELSPETH_ACCEPTANCE_TASK_ARN"],
        expected_scenario_id=env["ELSPETH_ACCEPTANCE_SCENARIO_ID"],
        expected_check="verify-s3",
    )

    assert envelope == {
        "version": 1,
        "check": "verify-s3",
        "ok": True,
        "candidate_sha": "c" * 40,
        "task_arn_sha256": hashlib.sha256(env["ELSPETH_ACCEPTANCE_TASK_ARN"].encode()).hexdigest(),
        "scenario_id": "scenario-a",
        "details": _s3_receipt_details(),
    }


@pytest.mark.parametrize(
    ("check", "details"),
    [
        ("verify-bedrock-guardrails", _guardrail_receipt_details()),
        ("verify-connection-budget", _connection_budget_details()),
        ("verify-operator-telemetry", _operator_receipt_details()),
        ("verify-operator-telemetry", _operator_receipt_details(phase="outage")),
    ],
)
def test_exec_receipt_supports_closed_guardrail_and_operator_telemetry_schemas(
    check: str,
    details: dict[str, object],
) -> None:
    env = _receipt_env()
    sentinel = acceptance.encode_exec_receipt(check, details, env)
    envelope = acceptance.extract_exec_receipt(
        sentinel,
        expected_candidate_sha=env["ELSPETH_ACCEPTANCE_CANDIDATE_SHA"],
        expected_task_arn=env["ELSPETH_ACCEPTANCE_TASK_ARN"],
        expected_scenario_id=env["ELSPETH_ACCEPTANCE_SCENARIO_ID"],
        expected_check=check,
    )
    assert envelope["details"] == details


def test_guardrail_exec_receipt_must_match_controller_policy_binding() -> None:
    env = _receipt_env()
    details = _guardrail_receipt_details()
    sentinel = acceptance.encode_exec_receipt("verify-bedrock-guardrails", details, env)

    with pytest.raises(acceptance.AcceptanceCheckError, match="plugin_policy_binding"):
        acceptance.extract_exec_receipt(
            sentinel,
            expected_candidate_sha=env["ELSPETH_ACCEPTANCE_CANDIDATE_SHA"],
            expected_task_arn=env["ELSPETH_ACCEPTANCE_TASK_ARN"],
            expected_scenario_id=env["ELSPETH_ACCEPTANCE_SCENARIO_ID"],
            expected_check="verify-bedrock-guardrails",
            expected_plugin_policy_binding_sha256="4" * 64,
        )


@pytest.mark.parametrize(
    "mutation",
    ["wrong_target", "missing_landscape", "alias_mismatch", "non_required_mode", "mutable_guardrail_version"],
)
def test_guardrail_exec_receipt_rejects_incomplete_or_mismatched_plugin_policy_evidence(mutation: str) -> None:
    details = _guardrail_receipt_details()
    policy = details["plugin_policy"]
    assert isinstance(policy, dict)
    selected = policy["selected_controls"]
    assert isinstance(selected, list)
    if mutation == "wrong_target":
        policy["target_llm"] = "transform:other"
    elif mutation == "missing_landscape":
        policy["landscape_evidence"] = False
    elif mutation == "alias_mismatch":
        selected[0]["profile_alias"] = "different"  # type: ignore[index]
    elif mutation == "non_required_mode":
        selected[1]["mode"] = "recommend"  # type: ignore[index]
    else:
        controls = details["controls"]
        assert isinstance(controls, list)
        controls[0]["guardrail_version"] = "DRAFT"  # type: ignore[index]

    with pytest.raises(acceptance.AcceptanceCheckError, match="exec_receipt_schema"):
        acceptance.encode_exec_receipt("verify-bedrock-guardrails", details, _receipt_env())


@pytest.mark.parametrize("mutation", ["missing_fixed_dimension", "wrong_sentinel_value", "extra_dimension"])
def test_operator_exec_receipt_rejects_non_exact_retained_metric_query(mutation: str) -> None:
    details = _operator_receipt_details()
    query = details["retained_metric_query"]
    assert isinstance(query, dict)
    dimensions = query["dimensions"]
    assert isinstance(dimensions, list)
    if mutation == "missing_fixed_dimension":
        dimensions[:] = [dimension for dimension in dimensions if dimension["name"] != "aws.ecs.cluster.name"]  # type: ignore[index]
    elif mutation == "wrong_sentinel_value":
        next(dimension for dimension in dimensions if dimension["name"] == "elspeth.acceptance.sentinel")["value"] = "1"  # type: ignore[index]
    else:
        dimensions.append({"name": "unexpected", "value": "value"})

    with pytest.raises(acceptance.AcceptanceCheckError, match="exec_receipt_schema"):
        acceptance.encode_exec_receipt("verify-operator-telemetry", details, _receipt_env())


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda env: {**env, "ELSPETH_ACCEPTANCE_CANDIDATE_SHA": "d" * 40}, "candidate_binding"),
        (lambda env: {**env, "ELSPETH_ACCEPTANCE_TASK_ARN": env["ELSPETH_ACCEPTANCE_TASK_ARN"] + "-other"}, "task_binding"),
        (lambda env: {**env, "ELSPETH_ACCEPTANCE_SCENARIO_ID": "scenario-b"}, "scenario_binding"),
    ],
)
def test_exec_receipt_rejects_wrong_bindings_with_static_failures(mutator: Callable[[dict[str, str]], dict[str, str]], match: str) -> None:
    env = _receipt_env()
    sentinel = acceptance.encode_exec_receipt("verify-s3", _s3_receipt_details(), env)
    expected = mutator(env)

    with pytest.raises(acceptance.AcceptanceCheckError, match=match):
        acceptance.extract_exec_receipt(
            sentinel,
            expected_candidate_sha=expected["ELSPETH_ACCEPTANCE_CANDIDATE_SHA"],
            expected_task_arn=expected["ELSPETH_ACCEPTANCE_TASK_ARN"],
            expected_scenario_id=expected["ELSPETH_ACCEPTANCE_SCENARIO_ID"],
            expected_check="verify-s3",
        )


@pytest.mark.parametrize("stream", ["no receipt", "ELSPETH_ACCEPTANCE_RECEIPT_V1:not-base64", "{sentinel}\n{sentinel}"])
def test_exec_receipt_rejects_missing_malformed_or_duplicate_sentinels(stream: str) -> None:
    env = _receipt_env()
    sentinel = acceptance.encode_exec_receipt("verify-s3", _s3_receipt_details(), env)
    stream = stream.format(sentinel=sentinel)

    with pytest.raises(acceptance.AcceptanceCheckError, match="exec_receipt") as raised:
        acceptance.extract_exec_receipt(
            stream,
            expected_candidate_sha=env["ELSPETH_ACCEPTANCE_CANDIDATE_SHA"],
            expected_task_arn=env["ELSPETH_ACCEPTANCE_TASK_ARN"],
            expected_scenario_id=env["ELSPETH_ACCEPTANCE_SCENARIO_ID"],
            expected_check="verify-s3",
        )

    assert "not-base64" not in str(raised.value)


@pytest.mark.parametrize("forbidden_key", ["provider_response", "credential", "task_arn", "model_id", "url", "error"])
def test_exec_receipt_rejects_unknown_or_raw_detail_fields(forbidden_key: str) -> None:
    env = _receipt_env()
    details = {**_s3_receipt_details(), forbidden_key: "raw-secret-sentinel"}

    with pytest.raises(acceptance.AcceptanceCheckError, match="exec_receipt_schema") as raised:
        acceptance.encode_exec_receipt("verify-s3", details, env)

    assert "raw-secret-sentinel" not in str(raised.value)


def test_exec_receipt_rejects_false_or_oversized_untrusted_payload() -> None:
    env = _receipt_env()
    payload = {
        "version": 1,
        "check": "verify-s3",
        "ok": False,
        "candidate_sha": env["ELSPETH_ACCEPTANCE_CANDIDATE_SHA"],
        "task_arn_sha256": "a" * 64,
        "scenario_id": env["ELSPETH_ACCEPTANCE_SCENARIO_ID"],
        "details": _s3_receipt_details(),
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sentinel = f"ELSPETH_ACCEPTANCE_RECEIPT_V1:{encoded}"
    for stream in (sentinel, f"ELSPETH_ACCEPTANCE_RECEIPT_V1:{'a' * (acceptance.MAX_EXEC_RECEIPT_CHARS + 1)}"):
        with pytest.raises(acceptance.AcceptanceCheckError, match="exec_receipt"):
            acceptance.extract_exec_receipt(
                stream,
                expected_candidate_sha=env["ELSPETH_ACCEPTANCE_CANDIDATE_SHA"],
                expected_task_arn=env["ELSPETH_ACCEPTANCE_TASK_ARN"],
                expected_scenario_id=env["ELSPETH_ACCEPTANCE_SCENARIO_ID"],
                expected_check="verify-s3",
            )


def _stored_exec_receipt(check: str = "verify-s3", details: dict[str, object] | None = None) -> dict[str, object]:
    env = _receipt_env()
    sentinel = acceptance.encode_exec_receipt(check, details or _s3_receipt_details(), env)
    return acceptance.extract_exec_receipt(
        sentinel,
        expected_candidate_sha=env["ELSPETH_ACCEPTANCE_CANDIDATE_SHA"],
        expected_task_arn=env["ELSPETH_ACCEPTANCE_TASK_ARN"],
        expected_scenario_id=env["ELSPETH_ACCEPTANCE_SCENARIO_ID"],
        expected_check=check,
    )


def _stored_connection_budget_receipt() -> dict[str, object]:
    return {
        "version": 1,
        "check": "verify-connection-budget",
        "ok": True,
        "candidate_sha": "c" * 40,
        "task_arn_sha256": "d" * 64,
        "scenario_id": "A",
        "details": _connection_budget_details(),
    }


@pytest.mark.parametrize(
    ("mutation", "expected_check"),
    [
        ("extra_key", "exec_receipt_schema"),
        ("malformed_hash", "exec_receipt_schema"),
        ("wrong_subject_hash", "receipt_store_binding"),
        ("wrong_scenario", "receipt_store_binding"),
        ("wrong_candidate", "receipt_store_binding"),
    ],
)
def test_stored_exec_receipt_rejects_open_keys_hashes_and_wrong_bindings(mutation: str, expected_check: str) -> None:
    payload = _stored_exec_receipt()
    subject_sha256 = payload["task_arn_sha256"]
    scenario_id = "scenario-a"
    candidate_sha = "c" * 40
    assert isinstance(subject_sha256, str)
    if mutation == "extra_key":
        payload["unreviewed"] = True
    elif mutation == "malformed_hash":
        payload["task_arn_sha256"] = "not-a-hash"
    elif mutation == "wrong_subject_hash":
        subject_sha256 = "f" * 64
    elif mutation == "wrong_scenario":
        scenario_id = "scenario-b"
    else:
        candidate_sha = "d" * 40

    with pytest.raises(acceptance.AcceptanceCheckError, match=expected_check):
        receipt_contracts._validate_stored_receipt(
            payload,
            kind="verify-s3",
            scenario_id=scenario_id,
            subject_sha256=subject_sha256,
            candidate_sha=candidate_sha,
        )


@pytest.mark.parametrize("field", ["high_water", "max_connections", "approved_budget", "safety_margin"])
def test_stored_connection_budget_rejects_bool_as_number(field: str) -> None:
    payload = _stored_connection_budget_receipt()
    details = payload["details"]
    assert isinstance(details, dict)
    details[field] = True

    with pytest.raises(acceptance.AcceptanceCheckError, match="receipt_store_schema"):
        receipt_contracts._validate_stored_receipt(
            payload,
            kind="connection-budget",
            scenario_id="A",
            subject_sha256="d" * 64,
            candidate_sha="c" * 40,
            expected_acceptance_run_id_sha256="b" * 64,
            expected_cluster_id_sha256="a" * 64,
        )


@pytest.mark.parametrize("field", ["max_connections", "approved_budget", "safety_margin"])
def test_stored_connection_budget_rejects_integral_float_for_integer_limit(field: str) -> None:
    payload = _stored_connection_budget_receipt()
    details = payload["details"]
    assert isinstance(details, dict)
    details[field] = float(details[field])

    with pytest.raises(acceptance.AcceptanceCheckError, match="receipt_store_schema"):
        receipt_contracts._validate_stored_receipt(
            payload,
            kind="connection-budget",
            scenario_id="A",
            subject_sha256="d" * 64,
            candidate_sha="c" * 40,
            expected_acceptance_run_id_sha256="b" * 64,
            expected_cluster_id_sha256="a" * 64,
        )


@pytest.mark.parametrize("mutation", ["candidate", "scenario", "task", "run", "cluster"])
def test_stored_connection_budget_binds_full_exec_and_runtime_identity(mutation: str) -> None:
    payload = _stored_connection_budget_receipt()
    subject_sha256 = "d" * 64
    expected_run_sha256 = "b" * 64
    expected_cluster_sha256 = "a" * 64
    details = payload["details"]
    assert isinstance(details, dict)
    if mutation == "candidate":
        payload["candidate_sha"] = "e" * 40
    elif mutation == "scenario":
        payload["scenario_id"] = "B"
    elif mutation == "task":
        subject_sha256 = "e" * 64
    elif mutation == "run":
        expected_run_sha256 = "e" * 64
    else:
        expected_cluster_sha256 = "e" * 64

    with pytest.raises(acceptance.AcceptanceCheckError, match="receipt_store_binding"):
        receipt_contracts._validate_stored_receipt(
            payload,
            kind="connection-budget",
            scenario_id="A",
            subject_sha256=subject_sha256,
            candidate_sha="c" * 40,
            expected_acceptance_run_id_sha256=expected_run_sha256,
            expected_cluster_id_sha256=expected_cluster_sha256,
        )


def test_stored_connection_budget_rejects_unbound_bare_details() -> None:
    with pytest.raises(acceptance.AcceptanceCheckError, match="exec_receipt_schema"):
        receipt_contracts._validate_stored_receipt(
            _connection_budget_details(),
            kind="connection-budget",
            scenario_id="A",
            subject_sha256="d" * 64,
            candidate_sha="c" * 40,
            expected_acceptance_run_id_sha256="b" * 64,
            expected_cluster_id_sha256="a" * 64,
        )


@pytest.mark.parametrize("include_subject_id", [False, True])
def test_stored_terraform_receipt_binds_exact_plan_sha_even_during_final_verification(include_subject_id: bool) -> None:
    with pytest.raises(acceptance.AcceptanceCheckError, match="receipt_store_binding"):
        receipt_contracts._validate_stored_receipt(
            _terraform_plan_receipt("a" * 64),
            kind="terraform-plan",
            scenario_id="A",
            subject_sha256=hashlib.sha256(("b" * 64).encode()).hexdigest(),
            candidate_sha="c" * 40,
            subject_id="b" * 64 if include_subject_id else None,
        )


def test_stored_operator_receipt_rejects_wrong_retained_namespace() -> None:
    payload = _stored_exec_receipt("verify-operator-telemetry", _operator_receipt_details())
    details = payload["details"]
    assert isinstance(details, dict)
    query = details["retained_metric_query"]
    assert isinstance(query, dict)
    query["namespace"] = "ELSPETH/Other"
    subject_sha256 = payload["task_arn_sha256"]
    assert isinstance(subject_sha256, str)

    with pytest.raises(acceptance.AcceptanceCheckError, match="exec_receipt_schema"):
        receipt_contracts._validate_stored_receipt(
            payload,
            kind="verify-operator-telemetry",
            scenario_id="scenario-a",
            subject_sha256=subject_sha256,
            candidate_sha="c" * 40,
        )


def _compatibility_receipt(scenario_id: str) -> dict[str, object]:
    previous = scenario_id == "B"
    return {
        "schema": "elspeth.aws-ecs-compatibility-receipt.v2",
        "record_sha256": "a" * 64,
        "acceptance_run_id_sha256": "b" * 64,
        "scenario_id": scenario_id,
        "candidate_sha": "c" * 40,
        "candidate_image_digest": "sha256:" + "d" * 64,
        "candidate_task_definition_sha256": "e" * 64,
        "candidate_doctor_task_definition_sha256": "f" * 64,
        "candidate_package_version": "0.7.2",
        "previous_source_sha": "1" * 40 if previous else None,
        "previous_image_digest": "sha256:" + "2" * 64 if previous else None,
        "previous_task_definition_sha256": "3" * 64 if previous else None,
        "rollback_doctor_task_definition_sha256": "4" * 64 if previous else None,
        "previous_package_version": "0.7.1" if previous else None,
        "schema_facts": receipt_contracts._expected_schema_facts(scenario_id),
        "forward_compatible": True,
        "backward_compatible": False,
        "rollback_permitted": False,
        "decision": "approved",
        "approvals_present": True,
        "expires_at": "2026-07-26T00:00:00Z",
    }


@pytest.mark.parametrize("scenario_id", ["A", "B"])
def test_stored_compatibility_receipt_accepts_exact_scenario_variants(scenario_id: str) -> None:
    payload = _compatibility_receipt(scenario_id)

    assert (
        receipt_contracts._validate_stored_receipt(
            payload,
            kind="compatibility-record",
            scenario_id=scenario_id,
            subject_sha256="9" * 64,
            candidate_sha="c" * 40,
            subject_id="a" * 64,
        )
        is payload
    )


@pytest.mark.parametrize(
    "mutation",
    ["subject_hash", "scenario", "candidate", "a_with_previous", "bool_compatibility"],
)
def test_stored_compatibility_receipt_rejects_wrong_hash_binding_or_variant(mutation: str) -> None:
    payload = _compatibility_receipt("A")
    subject_id = "a" * 64
    scenario_id = "A"
    candidate_sha = "c" * 40
    if mutation == "subject_hash":
        subject_id = "9" * 64
    elif mutation == "scenario":
        scenario_id = "B"
    elif mutation == "candidate":
        candidate_sha = "d" * 40
    elif mutation == "a_with_previous":
        payload["previous_source_sha"] = "1" * 40
    else:
        payload["backward_compatible"] = 0

    with pytest.raises(acceptance.AcceptanceCheckError, match=r"receipt_store_(?:binding|schema)"):
        receipt_contracts._validate_stored_receipt(
            payload,
            kind="compatibility-record",
            scenario_id=scenario_id,
            subject_sha256="9" * 64,
            candidate_sha=candidate_sha,
            subject_id=subject_id,
        )


def test_compatibility_schema_facts_track_current_epochs() -> None:
    """Regression: elspeth-7f4c87b341 — the compatibility validators must demand
    the CURRENT candidate epochs and a migration label naming the current
    landscape epoch, so truthful facts are accepted and a future epoch bump
    fails here instead of silently requiring a false attestation."""
    facts = acceptance._expected_schema_facts("B")
    candidate = facts["candidate"]
    assert isinstance(candidate, dict)
    assert candidate["session_epoch"] == SESSION_SCHEMA_EPOCH
    assert candidate["landscape_epoch"] == SQLITE_SCHEMA_EPOCH
    previous = facts["previous"]
    assert isinstance(previous, dict)
    label = facts["structural_changes"]
    assert isinstance(label, str)
    assert previous == {
        "session_epoch": 35,
        "landscape_epoch": 29,
        "run_web_plugin_policy_present": True,
    }
    assert label == (
        f"session_epoch_35_to_{SESSION_SCHEMA_EPOCH}_landscape_epoch_29_to_{SQLITE_SCHEMA_EPOCH}"
        "_blob_cleanup_guided_decline_and_row_union_barrier"
    )
    facts_a = acceptance._expected_schema_facts("A")
    assert facts_a["previous"] is None
    assert facts_a["structural_changes"] == "initial_create"
    assert facts_a["archive_export_decision"] == "not_applicable"
