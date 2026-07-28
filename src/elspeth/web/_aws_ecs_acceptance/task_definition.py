"""ECS task-definition policy admission bound to protected inventory."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import cast

import yaml

from elspeth.core.secrets import is_secret_field
from elspeth.web.composer.provider_config import infer_provider_from_model_name, infer_provider_from_unprefixed_model_name

from .contracts import FORBIDDEN_AWS_OVERRIDE_ENV, AcceptanceCheckError, plugin_policy_binding_sha256, scenario_resource_namespace
from .manifest_schema import _read_control_manifest
from .scenario_inventory import _TASK_DEFINITION_COMPOSER_MODEL_ENV, PLUGIN_POLICY_ASSIGNMENT_NAMES, _load_bound_scenario_inventory

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
_TASK_DEFINITION_AWS_OVERRIDE_ENV = FORBIDDEN_AWS_OVERRIDE_ENV | {"AWS_DEFAULT_REGION"}
_SECRET_VALUE_FROM_PATTERN = re.compile(
    r"arn:(aws(?:-us-gov|-cn)?):secretsmanager:([a-z0-9-]+):([0-9]{12}):"
    r"secret:([A-Za-z0-9/_+=.@-]{1,512})(?::([^:\x00-\x1f\x7f]{0,256}):([^:\x00-\x1f\x7f]{0,256}):([^:\x00-\x1f\x7f]{0,256}))?\Z"
)
_SECRET_ARN_SUFFIX_PATTERN = re.compile(r"(.+)-[A-Za-z0-9]{6}\Z")
_CLOUDWATCH_AGENT_CONFIG_MAX_BYTES = 16 * 1024
_CLOUDWATCH_AGENT_CONFIG_MAX_BASE64_CHARS = 4 * ((_CLOUDWATCH_AGENT_CONFIG_MAX_BYTES + 2) // 3)
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
_CLOUDWATCH_AGENT_ENV_NAMES = frozenset(
    {
        "ELSPETH_CW_AGENT_CONFIG_JSON_B64",
        "ELSPETH_CW_AGENT_CONFIG_JSON_SHA256",
        "ELSPETH_CW_AGENT_OTEL_YAML_B64",
        "ELSPETH_CW_AGENT_OTEL_YAML_SHA256",
    }
)
_PUBLISHED_WEB_COMMAND = ("web", "--host", "0.0.0.0", "--port", "8451")


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


def _decode_cloudwatch_agent_config(value: object) -> bytes:
    if type(value) is not str or not value or len(value) > _CLOUDWATCH_AGENT_CONFIG_MAX_BASE64_CHARS:
        raise AcceptanceCheckError("task_definition_policy_binding")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise AcceptanceCheckError("task_definition_policy_binding") from None
    if not decoded or len(decoded) > _CLOUDWATCH_AGENT_CONFIG_MAX_BYTES or base64.b64encode(decoded).decode("ascii") != value:
        raise AcceptanceCheckError("task_definition_policy_binding")
    return decoded


def _cloudwatch_json_object(content: bytes) -> Mapping[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise AcceptanceCheckError("task_definition_policy_binding") from None
    if not isinstance(document, Mapping):
        raise AcceptanceCheckError("task_definition_policy_binding")
    return document


def _cloudwatch_yaml_object(content: bytes) -> Mapping[str, object]:
    try:
        document = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError, OverflowError):
        raise AcceptanceCheckError("task_definition_policy_binding") from None
    if not isinstance(document, Mapping):
        raise AcceptanceCheckError("task_definition_policy_binding")
    return document


def _validate_cloudwatch_agent_sidecar(
    sidecar: Mapping[str, object],
    main_container: Mapping[str, object],
    values: Mapping[str, object],
) -> None:
    if (
        sidecar.get("name") != "cloudwatch-agent"
        or sidecar.get("image") != values["CLOUDWATCH_AGENT_IMAGE"]
        or sidecar.get("essential") is not False
        or type(sidecar.get("memoryReservation")) is not int
        or sidecar.get("memoryReservation") != 192
        or sidecar.get("entryPoint") != ["/bin/sh", "-ceu"]
        or sidecar.get("command") != [_CLOUDWATCH_AGENT_COMMAND]
        or sidecar.get("healthCheck") != _CLOUDWATCH_AGENT_HEALTH_CHECK
        or sidecar.get("secrets", []) != []
        or sidecar.get("mountPoints", []) != []
        or sidecar.get("portMappings", []) != []
        or sidecar.get("environmentFiles", []) != []
        or main_container.get("dependsOn") != [{"containerName": "cloudwatch-agent", "condition": "HEALTHY"}]
    ):
        raise AcceptanceCheckError("task_definition_policy_binding")

    environment = sidecar.get("environment")
    if not isinstance(environment, list) or len(environment) != len(_CLOUDWATCH_AGENT_ENV_NAMES):
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
    if (
        set(observed) != _CLOUDWATCH_AGENT_ENV_NAMES
        or set(observed).intersection(_TASK_DEFINITION_AWS_OVERRIDE_ENV)
        or any(_plaintext_task_definition_secret(name) for name in observed)
    ):
        raise AcceptanceCheckError("task_definition_policy_binding")

    config_json = _decode_cloudwatch_agent_config(observed["ELSPETH_CW_AGENT_CONFIG_JSON_B64"])
    otel_yaml = _decode_cloudwatch_agent_config(observed["ELSPETH_CW_AGENT_OTEL_YAML_B64"])
    _cloudwatch_json_object(config_json)
    _cloudwatch_yaml_object(otel_yaml)
    expected_json_sha256 = values["CLOUDWATCH_AGENT_CONFIG_JSON_SHA256"]
    expected_otel_sha256 = values["CLOUDWATCH_AGENT_OTEL_YAML_SHA256"]
    if (
        observed["ELSPETH_CW_AGENT_CONFIG_JSON_SHA256"] != expected_json_sha256
        or observed["ELSPETH_CW_AGENT_OTEL_YAML_SHA256"] != expected_otel_sha256
        or hashlib.sha256(config_json).hexdigest() != expected_json_sha256
        or hashlib.sha256(otel_yaml).hexdigest() != expected_otel_sha256
    ):
        raise AcceptanceCheckError("task_definition_policy_binding")


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
    values = cast(dict[str, object], inventory["values"])
    orphan = cast(dict[str, object], inventory["orphan_sweep"])
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
    container_names: list[str] = []
    for candidate in containers:
        if not isinstance(candidate, Mapping) or type(candidate.get("name")) is not str:
            raise AcceptanceCheckError("task_definition_policy_binding")
        container_names.append(cast(str, candidate["name"]))
    if len(container_names) != len(set(container_names)) or not set(container_names) <= {container_name, "cloudwatch-agent"}:
        raise AcceptanceCheckError("task_definition_policy_binding")
    matches = [container for container in containers if isinstance(container, Mapping) and container.get("name") == container_name]
    if len(matches) != 1:
        raise AcceptanceCheckError("task_definition_policy_binding")
    container = matches[0]
    if container.get("essential") is not True:
        raise AcceptanceCheckError("task_definition_policy_binding")
    is_published_web_container = expected_user is None and container_name == values["WEB_CONTAINER_NAME"]
    if is_published_web_container and container.get("command") != list(_PUBLISHED_WEB_COMMAND):
        raise AcceptanceCheckError("task_definition_policy_binding")
    sidecars = [candidate for candidate in containers if isinstance(candidate, Mapping) and candidate.get("name") == "cloudwatch-agent"]
    if not sidecars:
        if is_published_web_container:
            raise AcceptanceCheckError("task_definition_policy_binding")
    else:
        _validate_cloudwatch_agent_sidecar(sidecars[0], container, values)
    ecr = cast(Mapping[str, object], manifest["ecr"])
    registry = ecr["registry"]
    repository = ecr["repository"]
    digest = ecr["candidate_digest"] if expected_image_role == "candidate" else ecr["baseline_digest"]
    if type(registry) is not str or type(repository) is not str or type(digest) is not str:
        raise AcceptanceCheckError("task_definition_policy_binding")
    if container.get("image") != f"{registry}/{repository}@{digest}":
        raise AcceptanceCheckError("task_definition_policy_binding")
    aws = cast(Mapping[str, object], manifest["aws"])
    role_names = cast(list[str], orphan["iam_role_names"])
    account_id = aws["account_id"]
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
        expected_user != "1654:1654"
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
    if any(observed.get(name) != values[name] for name in _TASK_DEFINITION_COMPOSER_MODEL_ENV):
        raise AcceptanceCheckError("task_definition_policy_binding")
    composer_providers = {
        infer_provider_from_model_name(observed[name]) or infer_provider_from_unprefixed_model_name(observed[name])
        for name in _TASK_DEFINITION_COMPOSER_MODEL_ENV
    }
    if None in composer_providers or not composer_providers <= {"bedrock", "openrouter"}:
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
    aws_region = aws["region"]
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
    if secret_names != set(required_secret_bindings):
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
    if any(observed.get(name) != values[name] for name in protected_names):
        raise AcceptanceCheckError("task_definition_policy_binding")
    if any(observed.get(name) != value for name, value in expected_runtime.items()):
        raise AcceptanceCheckError("task_definition_policy_binding")
    data_dir_value = observed["ELSPETH_WEB__DATA_DIR"]
    payload_root_value = observed["ELSPETH_WEB__PAYLOAD_STORE_PATH"]
    try:
        data_dir = PurePosixPath(data_dir_value)
        payload_root = PurePosixPath(payload_root_value)
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

    file_system_ids = cast(list[str], orphan["efs_file_system_ids"])
    access_point_ids = cast(list[str], orphan["efs_access_point_ids"])
    if (
        len(file_system_ids) != 1
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
