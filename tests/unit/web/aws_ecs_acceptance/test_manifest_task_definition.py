"""Ownership tests for manifest mutation and ECS task-definition admission."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from elspeth.web import aws_ecs_acceptance as acceptance
from tests.unit.web.aws_ecs_acceptance.test_manifest_schema_inventory import (
    CLOUDWATCH_AGENT_CONFIG_JSON,
    CLOUDWATCH_AGENT_OTEL_YAML,
    _init_control_manifest,
)

_CLOUDWATCH_AGENT_COMMAND = (
    "CONFIG_DIR=/tmp/elspeth-cloudwatch-agent; CTL=/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl; "
    'mkdir -p "$CONFIG_DIR"; printf \'%s\' "$ELSPETH_CW_AGENT_CONFIG_JSON_B64" | base64 -d > '
    "\"/tmp/elspeth-cloudwatch-agent/elspeth.cloudwatch-agent.v1.json\"; printf '%s' "
    '"$ELSPETH_CW_AGENT_OTEL_YAML_B64" | base64 -d > '
    "\"/tmp/elspeth-cloudwatch-agent/elspeth.cloudwatch-agent.v1.otel.yaml\"; printf '%s\\n' "
    '"$ELSPETH_CW_AGENT_CONFIG_JSON_SHA256  /tmp/elspeth-cloudwatch-agent/elspeth.cloudwatch-agent.v1.json" | '
    "sha256sum -c -; printf '%s\\n' "
    '"$ELSPETH_CW_AGENT_OTEL_YAML_SHA256  /tmp/elspeth-cloudwatch-agent/elspeth.cloudwatch-agent.v1.otel.yaml" | '
    'sha256sum -c -; "$CTL" -a fetch-config -m auto -c '
    '"file:/tmp/elspeth-cloudwatch-agent/elspeth.cloudwatch-agent.v1.json" -s; "$CTL" -a append-config -m auto -c '
    '"file:/tmp/elspeth-cloudwatch-agent/elspeth.cloudwatch-agent.v1.otel.yaml" -s; while "$CTL" -a status -m auto '
    '| grep -q \'"status": "running"\'; do sleep 30; done; exit 1'
)
_CLOUDWATCH_AGENT_HEALTH_CHECK = {
    "command": [
        "CMD-SHELL",
        '/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status -m auto | grep -q \'"status": "running"\'',
    ],
    "interval": 10,
    "timeout": 5,
    "retries": 6,
    "startPeriod": 30,
}


def test_manifest_and_task_definition_modules_exist() -> None:
    assert importlib.util.find_spec("elspeth.web._aws_ecs_acceptance.manifest") is not None
    assert importlib.util.find_spec("elspeth.web._aws_ecs_acceptance.task_definition") is not None


def test_manifest_and_task_definition_owners_are_facade_reexports_by_identity() -> None:
    from elspeth.web._aws_ecs_acceptance import manifest, task_definition

    for name in (
        "control_manifest_bind_retained_evidence",
        "control_manifest_bind_scenario",
        "control_manifest_checkpoint_operator_evidence",
        "control_manifest_get",
        "control_manifest_init",
    ):
        assert getattr(acceptance, name) is getattr(manifest, name)
    assert acceptance.validate_task_definition_policy_binding is task_definition.validate_task_definition_policy_binding


def _task_definition_policy_payload(
    tmp_path: Path,
    *,
    record_ecr: bool = True,
    composer_model: str = "openrouter/openai/gpt-5.4",
    composer_advisor_model: str = "openrouter/anthropic/claude-opus-4.6",
    cloudwatch_config_json: bytes = CLOUDWATCH_AGENT_CONFIG_JSON,
    cloudwatch_otel_yaml: bytes = CLOUDWATCH_AGENT_OTEL_YAML,
    with_cloudwatch_agent: bool = True,
) -> tuple[Path, str, dict[str, Any], dict[str, Any]]:
    manifest_path = tmp_path / "control.json"

    def bind_composer_models(inventory: dict[str, object], _scenario_id: str) -> None:
        values = inventory["values"]
        assert isinstance(values, dict)
        values["ELSPETH_WEB__COMPOSER_MODEL"] = composer_model
        values["ELSPETH_WEB__COMPOSER_ADVISOR_MODEL"] = composer_advisor_model
        values["CLOUDWATCH_AGENT_CONFIG_JSON_SHA256"] = hashlib.sha256(cloudwatch_config_json).hexdigest()
        values["CLOUDWATCH_AGENT_OTEL_YAML_SHA256"] = hashlib.sha256(cloudwatch_otel_yaml).hexdigest()

    _init_control_manifest(
        manifest_path,
        inventory_mutator=bind_composer_models,
        preapply_inventory_mutator=bind_composer_models,
    )
    if record_ecr:
        acceptance.control_manifest_update(
            manifest_path,
            cleanup_required=True,
            ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
            ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
            ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
            ecr_repository="elspeth-acceptance",
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )
        acceptance.control_manifest_update(
            manifest_path,
            ecr_baseline_digest="sha256:" + "b" * 64,
            ecr_candidate_digest="sha256:" + "d" * 64,
            now=lambda: datetime(2026, 7, 14, 1, 2, 10, tzinfo=UTC),
        )
    inventory = json.loads((tmp_path / "scenario-a.json").read_text())
    values = inventory["values"]
    container_name = values["WEB_CONTAINER_NAME"]
    environment = [
        {"name": name, "value": values[name]}
        for name in (
            "ELSPETH_WEB__DATA_DIR",
            "ELSPETH_WEB__PAYLOAD_STORE_PATH",
            "ELSPETH_WEB__COMPOSER_MODEL",
            "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL",
            *acceptance.PLUGIN_POLICY_ASSIGNMENT_NAMES,
            "ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256",
            "ELSPETH_BEDROCK_LIVE_TEST_MODEL",
            "AWS_REGION",
        )
    ]
    acceptance_run_id = inventory["acceptance_run_id"]
    environment.extend(
        [
            {"name": "ELSPETH_ACCEPTANCE_RUN_ID", "value": acceptance_run_id},
            {"name": "ELSPETH_ACCEPTANCE_CANDIDATE_SHA", "value": inventory["candidate_sha"]},
            {"name": "ELSPETH_ACCEPTANCE_SCENARIO_ID", "value": "A"},
            {"name": "ELSPETH_ACCEPTANCE_S3_BUCKET", "value": values["ELSPETH_TEST_S3_BUCKET"]},
            {
                "name": "ELSPETH_ACCEPTANCE_S3_PREFIX",
                "value": f"{acceptance.scenario_resource_namespace(acceptance_run_id, 'A')}/{acceptance_run_id}",
            },
        ]
    )
    namespace = acceptance.scenario_resource_namespace(inventory["acceptance_run_id"], "A")
    required_secret_bindings = (
        ("ELSPETH_WEB__SECRET_KEY", f"{namespace}-database-runtime", "secret_key"),
        ("ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY", f"{namespace}-database-runtime", "shareable_link_signing_key"),
        ("ELSPETH_WEB__SESSION_DB_URL", f"{namespace}-database-runtime", "session_url"),
        ("ELSPETH_WEB__LANDSCAPE_URL", f"{namespace}-database-runtime", "landscape_url"),
    )
    if composer_model.startswith("openrouter/") or composer_advisor_model.startswith("openrouter/"):
        required_secret_bindings += (("OPENROUTER_API_KEY", f"{namespace}-openrouter-composer", "openrouter_api_key"),)
    secrets = [
        {
            "name": name,
            "valueFrom": (f"arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:{secret_id}-AbCd12:{json_key}::"),
        }
        for name, secret_id, json_key in required_secret_bindings
    ]
    payload = {
        "taskDefinition": {
            "taskDefinitionArn": "arn:aws:ecs:ap-southeast-2:123456789012:task-definition/elspeth-web:17",
            "status": "ACTIVE",
            "taskRoleArn": f"arn:aws:iam::123456789012:role/{inventory['orphan_sweep']['iam_role_names'][0]}",
            "executionRoleArn": f"arn:aws:iam::123456789012:role/{inventory['orphan_sweep']['iam_role_names'][1]}",
            "containerDefinitions": [
                {
                    "name": container_name,
                    "essential": True,
                    "image": "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/elspeth-acceptance@sha256:" + "d" * 64,
                    "command": ["web", "--host", "0.0.0.0", "--port", "8451"],
                    "environment": environment,
                    "secrets": secrets,
                    "mountPoints": [
                        {
                            "sourceVolume": "data",
                            "containerPath": values["ELSPETH_WEB__DATA_DIR"],
                            "readOnly": False,
                        }
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "data",
                    "efsVolumeConfiguration": {
                        "fileSystemId": inventory["orphan_sweep"]["efs_file_system_ids"][0],
                        "transitEncryption": "ENABLED",
                        "authorizationConfig": {
                            "accessPointId": inventory["orphan_sweep"]["efs_access_point_ids"][0],
                            "iam": "ENABLED",
                        },
                    },
                }
            ],
        }
    }
    if with_cloudwatch_agent:
        _attach_cloudwatch_agent(inventory, payload, config_json=cloudwatch_config_json, otel_yaml=cloudwatch_otel_yaml)
    return manifest_path, container_name, inventory, payload


def _attach_cloudwatch_agent(
    inventory: dict[str, Any],
    payload: dict[str, Any],
    *,
    config_json: bytes = CLOUDWATCH_AGENT_CONFIG_JSON,
    otel_yaml: bytes = CLOUDWATCH_AGENT_OTEL_YAML,
) -> dict[str, Any]:
    values = inventory["values"]
    main = payload["taskDefinition"]["containerDefinitions"][0]
    main["dependsOn"] = [{"containerName": "cloudwatch-agent", "condition": "HEALTHY"}]
    sidecar = {
        "name": "cloudwatch-agent",
        "image": values["CLOUDWATCH_AGENT_IMAGE"],
        "essential": False,
        "memoryReservation": 192,
        "entryPoint": ["/bin/sh", "-ceu"],
        "command": [_CLOUDWATCH_AGENT_COMMAND],
        "environment": [
            {
                "name": "ELSPETH_CW_AGENT_CONFIG_JSON_B64",
                "value": base64.b64encode(config_json).decode("ascii"),
            },
            {
                "name": "ELSPETH_CW_AGENT_CONFIG_JSON_SHA256",
                "value": hashlib.sha256(config_json).hexdigest(),
            },
            {
                "name": "ELSPETH_CW_AGENT_OTEL_YAML_B64",
                "value": base64.b64encode(otel_yaml).decode("ascii"),
            },
            {
                "name": "ELSPETH_CW_AGENT_OTEL_YAML_SHA256",
                "value": hashlib.sha256(otel_yaml).hexdigest(),
            },
        ],
        "healthCheck": _CLOUDWATCH_AGENT_HEALTH_CHECK.copy(),
        "secrets": [],
        "mountPoints": [],
        "portMappings": [],
    }
    payload["taskDefinition"]["containerDefinitions"].append(sidecar)
    return sidecar


def test_task_definition_policy_binding_allows_bedrock_models_without_openrouter_secret(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(
        tmp_path,
        composer_model="bedrock/global.anthropic.claude-sonnet-4-6",
        composer_advisor_model="bedrock/global.anthropic.claude-opus-4-6-v1",
    )
    container = payload["taskDefinition"]["containerDefinitions"][0]

    assert "OPENROUTER_API_KEY" not in {entry["name"] for entry in container["secrets"]}
    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
    )


def test_task_definition_policy_binding_allows_only_the_cloudwatch_agent_sidecar(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)

    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
    )


def test_task_definition_policy_binding_requires_exact_published_web_command(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]
    container["command"] = ["--help"]

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_rejects_cloudwatch_sidecar_image_substitution(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    sidecar = next(
        candidate for candidate in payload["taskDefinition"]["containerDefinitions"] if candidate.get("name") == "cloudwatch-agent"
    )
    sidecar["image"] = "public.ecr.aws/attacker/cloudwatch-agent@sha256:" + "f" * 64

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param("essential", id="essential"),
        pytest.param("memory", id="memory-reservation"),
        pytest.param("entrypoint", id="entrypoint"),
        pytest.param("command", id="command"),
        pytest.param("health-command", id="health-command"),
        pytest.param("health-interval", id="health-interval"),
        pytest.param("health-extra", id="health-extra-field"),
        pytest.param("environment-extra", id="environment-extra"),
        pytest.param("environment-duplicate", id="environment-duplicate"),
        pytest.param("json-sha", id="json-companion-sha"),
        pytest.param("otel-sha", id="otel-companion-sha"),
        pytest.param("invalid-base64", id="invalid-base64"),
        pytest.param("decoded-content", id="decoded-content"),
        pytest.param("secrets", id="secrets"),
        pytest.param("mount-points", id="mount-points"),
        pytest.param("port-mappings", id="port-mappings"),
        pytest.param("environment-files", id="environment-files"),
        pytest.param("aws-override", id="aws-override"),
        pytest.param("missing-dependency", id="missing-main-dependency"),
        pytest.param("wrong-dependency", id="wrong-main-dependency"),
    ],
)
def test_task_definition_policy_binding_rejects_mutated_cloudwatch_sidecar(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    sidecar = next(
        candidate for candidate in payload["taskDefinition"]["containerDefinitions"] if candidate.get("name") == "cloudwatch-agent"
    )
    environment = sidecar["environment"]
    main = payload["taskDefinition"]["containerDefinitions"][0]

    if mutation == "essential":
        sidecar["essential"] = True
    elif mutation == "memory":
        sidecar["memoryReservation"] = 191
    elif mutation == "entrypoint":
        sidecar["entryPoint"] = ["/bin/sh", "-c"]
    elif mutation == "command":
        sidecar["command"] = [f"{_CLOUDWATCH_AGENT_COMMAND}; id"]
    elif mutation == "health-command":
        sidecar["healthCheck"]["command"] = ["CMD-SHELL", "true"]
    elif mutation == "health-interval":
        sidecar["healthCheck"]["interval"] = 11
    elif mutation == "health-extra":
        sidecar["healthCheck"]["disabled"] = True
    elif mutation == "environment-extra":
        environment.append({"name": "EXTRA", "value": "unreviewed"})
    elif mutation == "environment-duplicate":
        environment[1]["name"] = environment[0]["name"]
    elif mutation == "json-sha":
        environment[1]["value"] = "f" * 64
    elif mutation == "otel-sha":
        environment[3]["value"] = "f" * 64
    elif mutation == "invalid-base64":
        environment[0]["value"] = "not+canonical==="
    elif mutation == "decoded-content":
        environment[0]["value"] = base64.b64encode(b"{}\n").decode("ascii")
    elif mutation == "secrets":
        sidecar["secrets"] = [{"name": "TOKEN", "valueFrom": "arn:aws:secretsmanager:example"}]
    elif mutation == "mount-points":
        sidecar["mountPoints"] = [{"sourceVolume": "data", "containerPath": "/data"}]
    elif mutation == "port-mappings":
        sidecar["portMappings"] = [{"containerPort": 4317}]
    elif mutation == "environment-files":
        sidecar["environmentFiles"] = [{"type": "s3", "value": "arn:aws:s3:::attacker/env"}]
    elif mutation == "aws-override":
        environment.append({"name": "AWS_ACCESS_KEY_ID", "value": "static"})
    elif mutation == "missing-dependency":
        del main["dependsOn"]
    elif mutation == "wrong-dependency":
        main["dependsOn"] = [{"containerName": "cloudwatch-agent", "condition": "START"}]
    else:
        raise AssertionError(mutation)

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


@pytest.mark.parametrize(
    ("config_json", "otel_yaml"),
    [
        pytest.param(b"[]\n", CLOUDWATCH_AGENT_OTEL_YAML, id="non-mapping-json"),
        pytest.param(CLOUDWATCH_AGENT_CONFIG_JSON, b"[]\n", id="non-mapping-yaml"),
        pytest.param(CLOUDWATCH_AGENT_CONFIG_JSON, b"[" * 500 + b"0" + b"]" * 500, id="deeply-nested-yaml"),
        pytest.param(b'{"padding":"' + b"x" * 20_000 + b'"}\n', CLOUDWATCH_AGENT_OTEL_YAML, id="oversized-json"),
    ],
)
def test_task_definition_policy_binding_rejects_invalid_bound_cloudwatch_config(
    tmp_path: Path,
    config_json: bytes,
    otel_yaml: bytes,
) -> None:
    manifest_path, container_name, inventory, payload = _task_definition_policy_payload(
        tmp_path,
        cloudwatch_config_json=config_json,
        cloudwatch_otel_yaml=otel_yaml,
        with_cloudwatch_agent=False,
    )
    _attach_cloudwatch_agent(inventory, payload, config_json=config_json, otel_yaml=otel_yaml)

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


@pytest.mark.parametrize(
    "extra_container",
    [
        pytest.param({"name": "unreviewed-sidecar"}, id="unknown-name"),
        pytest.param({"name": "cloudwatch-agent"}, id="duplicate-cloudwatch-agent"),
        pytest.param("not-a-container-definition", id="non-mapping"),
    ],
)
def test_task_definition_policy_binding_rejects_unreviewed_container_definitions(
    tmp_path: Path,
    extra_container: object,
) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    containers = payload["taskDefinition"]["containerDefinitions"]
    if extra_container == {"name": "cloudwatch-agent"}:
        containers.append({"name": "cloudwatch-agent"})
    containers.append(extra_container)

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


@pytest.mark.parametrize(
    ("composer_model", "composer_advisor_model"),
    [
        ("openrouter/openai/gpt-5.4", "bedrock/global.anthropic.claude-opus-4-6-v1"),
        ("bedrock/global.anthropic.claude-sonnet-4-6", "openrouter/anthropic/claude-opus-4.6"),
    ],
)
def test_task_definition_policy_binding_requires_openrouter_secret_for_mixed_models(
    tmp_path: Path,
    composer_model: str,
    composer_advisor_model: str,
) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(
        tmp_path,
        composer_model=composer_model,
        composer_advisor_model=composer_advisor_model,
    )
    container = payload["taskDefinition"]["containerDefinitions"][0]
    container["secrets"] = [entry for entry in container["secrets"] if entry["name"] != "OPENROUTER_API_KEY"]

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


@pytest.mark.parametrize("reference_kind", ["missing", "unapproved"])
def test_task_definition_policy_binding_rejects_missing_or_unapproved_secret_reference(
    tmp_path: Path,
    reference_kind: str,
) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]
    entry = next(item for item in container["secrets"] if item["name"] == "ELSPETH_WEB__SESSION_DB_URL")

    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
    )

    if reference_kind == "missing":
        del entry["valueFrom"]
    else:
        entry["valueFrom"] = "arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:unapproved-database-secret-AbCd12:SESSION_DB_URL::"
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_requires_complete_runtime_secret_set(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]
    container["secrets"] = [entry for entry in container["secrets"] if entry["name"] != "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY"]

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_requires_exact_runtime_secret_selectors(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]
    shared_reference = container["secrets"][0]["valueFrom"]
    for entry in container["secrets"]:
        entry["valueFrom"] = shared_reference

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_rejects_surplus_secret_alias_to_approved_secret(tmp_path: Path) -> None:
    manifest_path, container_name, inventory, payload = _task_definition_policy_payload(tmp_path)
    namespace = acceptance.scenario_resource_namespace(inventory["acceptance_run_id"], "A")
    payload["taskDefinition"]["containerDefinitions"][0]["secrets"].append(
        {
            "name": "UNREVIEWED_SECRET_ALIAS",
            "valueFrom": (f"arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:{namespace}-database-runtime-AbCd12:secret_key::"),
        }
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


@pytest.mark.parametrize(
    ("location", "name"),
    [
        ("environment", "ELSPETH_WEB__SECRET_KEY"),
        ("environment", "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY"),
        ("environment", "ELSPETH_WEB__SESSION_DB_URL"),
        ("environment", "ELSPETH_WEB__LANDSCAPE_URL"),
        ("environment", "ELSPETH_FINGERPRINT_KEY"),
        ("environment", "ELSPETH_WEB__OIDC_CLIENT_SECRET"),
        ("environment", "DATABASE_URL"),
        ("environment", "OPENROUTER_API_KEY"),
        ("environment", "AWS_ACCESS_KEY_ID"),
        ("environment", "AWS_PROFILE"),
        ("environment", "AWS_ENDPOINT_URL"),
        ("environment", "AWS_ROLE_ARN"),
        ("secrets", "AWS_ACCESS_KEY_ID"),
    ],
)
def test_task_definition_policy_binding_rejects_plaintext_secrets_and_aws_overrides(
    tmp_path: Path,
    location: str,
    name: str,
) -> None:
    manifest_path, container_name, inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]
    if location == "environment":
        container["environment"].append({"name": name, "value": "raw-override-sentinel"})
    else:
        secret_id = inventory["orphan_sweep"]["secret_ids"][0]
        container["secrets"].append(
            {
                "name": name,
                "valueFrom": (f"arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:{secret_id}-AbCd12:AWS_ACCESS_KEY_ID::"),
            }
        )

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


@pytest.mark.parametrize(
    "field",
    [
        "ELSPETH_WEB__DATA_DIR",
        "ELSPETH_WEB__PAYLOAD_STORE_PATH",
        *acceptance.PLUGIN_POLICY_ASSIGNMENT_NAMES,
        "ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256",
        "ELSPETH_BEDROCK_LIVE_TEST_MODEL",
        "AWS_REGION",
    ],
)
def test_task_definition_policy_binding_compares_returned_environment_to_protected_inventory(
    tmp_path: Path,
    field: str,
) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    task_definition_arn = payload["taskDefinition"]["taskDefinitionArn"]
    environment = payload["taskDefinition"]["containerDefinitions"][0]["environment"]

    assert (
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )
        == task_definition_arn
    )

    observed = {entry["name"]: entry["value"] for entry in environment}
    observed[field] = "us-east-1" if field == "AWS_REGION" else "substituted"
    if field in acceptance.PLUGIN_POLICY_ASSIGNMENT_NAMES:
        observed["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"] = acceptance.plugin_policy_binding_sha256(observed)
    for entry in environment:
        entry["value"] = observed[entry["name"]]

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_requires_explicit_nonroot_one_shot_entrypoint(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]
    container["user"] = "1654:1654"
    container["entryPoint"] = ["python", "-m", "elspeth.web.aws_ecs_acceptance"]

    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
        expected_user="1654:1654",
    )
    task_definition = payload["taskDefinition"]
    original_task_role = task_definition["taskRoleArn"]
    original_execution_role = task_definition["executionRoleArn"]
    task_definition["taskRoleArn"] = original_execution_role
    task_definition["executionRoleArn"] = original_task_role
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_user="1654:1654",
        )
    task_definition["taskRoleArn"] = original_task_role
    task_definition["executionRoleArn"] = original_execution_role
    task_definition["taskRoleArn"] = "arn:aws:iam::999999999999:role/foreign-task-role"
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_user="1654:1654",
        )
    task_definition["taskRoleArn"] = original_task_role
    task_definition["volumes"].append(
        {
            "name": "foreign-data",
            "efsVolumeConfiguration": {
                "fileSystemId": "fs-ffffffffffffffffa",
                "transitEncryption": "ENABLED",
                "authorizationConfig": {"accessPointId": "fsap-ffffffffffffffffa", "iam": "ENABLED"},
            },
        }
    )
    container["mountPoints"].append({"sourceVolume": "foreign-data", "containerPath": "/foreign", "readOnly": False})
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_user="1654:1654",
        )
    task_definition["volumes"].pop()
    container["mountPoints"].pop()
    task_definition["volumes"].append({"name": "data", "host": {}})
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_user="1654:1654",
        )
    task_definition["volumes"].pop()
    container["user"] = "0"
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_user="1654:1654",
        )
    container["user"] = "1654:1654"
    payload["taskDefinition"]["volumes"][0]["efsVolumeConfiguration"]["fileSystemId"] = "fs-ffffffffffffffffa"  # type: ignore[index]
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_user="1654:1654",
        )


def test_task_definition_policy_binding_requires_manifest_pinned_image(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]

    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
    )

    reference = "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/elspeth-acceptance"
    for image in (
        f"{reference}:latest",
        f"{reference}@sha256:{'e' * 64}",
        f"{reference}@sha256:{'b' * 64}",
    ):
        container["image"] = image
        with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
            acceptance.validate_task_definition_policy_binding(
                payload,
                manifest_path=manifest_path,
                scenario_id="A",
                container_name=container_name,
            )

    del container["image"]
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_binds_rollback_image_to_baseline_digest(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_image_role="rollback-baseline",
        )

    container["image"] = "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/elspeth-acceptance@sha256:" + "b" * 64
    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
        expected_image_role="rollback-baseline",
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_image_role="latest",
        )


def test_task_definition_policy_binding_fails_closed_without_recorded_image_identity(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path, record_ecr=False)

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


@pytest.mark.parametrize(
    ("composer_model", "composer_advisor_model", "requires_openrouter"),
    [
        ("bedrock/global.anthropic.claude-sonnet-4-6", "bedrock/global.anthropic.claude-opus-4-6-v1", False),
        ("openrouter/openai/gpt-5.4", "bedrock/global.anthropic.claude-opus-4-6-v1", True),
        ("bedrock/global.anthropic.claude-sonnet-4-6", "openrouter/anthropic/claude-opus-4.6", True),
        ("openrouter/openai/gpt-5.4", "openrouter/anthropic/claude-opus-4.6", True),
    ],
)
def test_task_definition_policy_binding_provider_matrix_accepts_exact_secret_closure(
    tmp_path: Path,
    composer_model: str,
    composer_advisor_model: str,
    requires_openrouter: bool,
) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(
        tmp_path,
        composer_model=composer_model,
        composer_advisor_model=composer_advisor_model,
    )
    container = payload["taskDefinition"]["containerDefinitions"][0]

    assert ("OPENROUTER_API_KEY" in {entry["name"] for entry in container["secrets"]}) is requires_openrouter
    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
    )


def test_task_definition_policy_binding_rejects_unknown_provider(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(
        tmp_path,
        composer_model="unknown-provider/model",
        composer_advisor_model="bedrock/global.anthropic.claude-opus-4-6-v1",
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_rejects_surplus_openrouter_secret(tmp_path: Path) -> None:
    manifest_path, container_name, inventory, payload = _task_definition_policy_payload(
        tmp_path,
        composer_model="bedrock/global.anthropic.claude-sonnet-4-6",
        composer_advisor_model="bedrock/global.anthropic.claude-opus-4-6-v1",
    )
    namespace = acceptance.scenario_resource_namespace(inventory["acceptance_run_id"], "A")
    payload["taskDefinition"]["containerDefinitions"][0]["secrets"].append(
        {
            "name": "OPENROUTER_API_KEY",
            "valueFrom": (
                f"arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:{namespace}-openrouter-composer-AbCd12:openrouter_api_key::"
            ),
        }
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_requires_cloudwatch_agent_for_published_web_container(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(
        tmp_path,
        with_cloudwatch_agent=False,
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_allows_missing_cloudwatch_agent_for_one_shot_task(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(
        tmp_path,
        with_cloudwatch_agent=False,
    )
    container = payload["taskDefinition"]["containerDefinitions"][0]
    container["user"] = "1654:1654"
    container["entryPoint"] = ["python", "-m", "elspeth.web.aws_ecs_acceptance"]

    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
        expected_user="1654:1654",
    )
