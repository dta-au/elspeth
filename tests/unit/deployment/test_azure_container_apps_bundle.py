"""Contract tests for the Azure Container Apps Bicep bundle.

Every structural assertion runs against the COMPILED ARM JSON of the templates
resolved against a parameter file, never against Bicep text. Bicep emits any
object that references a parameter as an ARM expression string
(``[createObject(...)]``), so the tests carry a small evaluator for exactly
the ARM functions the compiled templates use; an unsupported function is an
error, which keeps the templates inside the evaluated subset.

The Bicep CLI is pinned by version and SHA-256 in ``.github/workflows/ci.yaml``
(the same pin the platform facts record). Like the Terraform package tests, a
missing binary skips locally and fails loudly in CI.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE = REPO_ROOT / "deploy" / "azure-container-apps"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
BUILD_PUSH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-push.yaml"
PLATFORM_FACTS = REPO_ROOT / "docs" / "plans" / "2026-09-05-phase6b-azure-container-apps-platform-facts.md"

BICEP_VERSION = "0.46.1"
BICEP_SHA256 = "3e011d629ea4311b7a7dd8f0040ab2b1a072ea4ff5d02cb75e0e55a9a6703fb9"
INGRESS_REQUEST_TIMEOUT_SECONDS = 240
DIGEST_IMAGE_RE = re.compile(r"^[a-z0-9.-]+(?::\d+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
VERSIONED_SECRET_URL_RE = re.compile(r"^https://[a-z0-9-]+\.vault\.azure\.net/secrets/[a-z0-9-]+/[0-9a-f]{32}$")

EXPECTED_FILES = {
    "README.md",
    "main.bicep",
    "environment.bicep",
    "workload.bicep",
    "modules/registry-pull-role.bicep",
    "main.example.bicepparam",
    "main.acceptance.bicepparam",
    "environment.example.bicepparam",
    "workload.production.bicepparam",
    "workload.acceptance.bicepparam",
    "kql/doctor-report.kql",
    "kql/run-sentinel-by-replica.kql",
    "kql/replica-lifecycle.kql",
    "kql/fence-conflict-409.kql",
    "scripts/acceptance.sh",
}

TEMPLATES = ("main", "environment", "workload")
PARAMETER_FILES = {
    "main.example": "main",
    "main.acceptance": "main",
    "environment.example": "environment",
    "workload.production": "workload",
    "workload.acceptance": "workload",
}


# ---------------------------------------------------------------------------
# Bicep invocation
# ---------------------------------------------------------------------------


def _require_bicep(reason: str) -> None:
    """Skip locally when bicep is absent; fail loudly in CI.

    The workflow installs a checksum-pinned bicep, so absence under
    GITHUB_ACTIONS is a broken gate, not an environment quirk (the same rule
    ``test_aws_ecs_terraform_package.py`` applies to terraform).
    """
    if shutil.which("bicep") is not None:
        return
    if os.environ.get("GITHUB_ACTIONS"):
        pytest.fail(f"bicep binary is missing in CI: {reason}")
    pytest.skip(f"bicep is not installed, so {reason}")


def _bicep(*args: str) -> str:
    _require_bicep("the bundle cannot be compiled")
    result = subprocess.run(["bicep", *args], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "Error" not in result.stderr, result.stderr
    return result.stdout


@functools.cache
def _template(name: str) -> dict[str, Any]:
    document = json.loads(_bicep("build", str(BUNDLE / f"{name}.bicep"), "--stdout"))
    assert isinstance(document, dict)
    return document


@functools.cache
def _parameters(name: str) -> dict[str, Any]:
    built = json.loads(_bicep("build-params", str(BUNDLE / f"{name}.bicepparam"), "--stdout"))
    document = json.loads(built["parametersJson"]) if "parametersJson" in built else built
    return {key: entry["value"] for key, entry in document["parameters"].items()}


# ---------------------------------------------------------------------------
# A bounded ARM expression evaluator for the compiled templates
# ---------------------------------------------------------------------------


class _Ref:
    """An unresolvable runtime reference (module outputs), kept as a marker."""

    def __init__(self, path: tuple[str, ...]) -> None:
        self.path = path

    def child(self, name: str) -> _Ref:
        return _Ref((*self.path, name))

    def __getitem__(self, index: object) -> _Ref:
        return self.child(str(index))

    def __str__(self) -> str:
        return "<ref:" + ".".join(self.path) + ">"

    def __repr__(self) -> str:
        return str(self)


class _Lambda:
    def __init__(self, names: list[str], body: tuple[Any, ...]) -> None:
        self.names = names
        self.body = body


_TOKEN_RE = re.compile(r"\s*(?:('(?:[^']|'')*')|(-?\d+)|([A-Za-z_][A-Za-z0-9_]*)|([()\[\],.]))")


def _tokenize(expression: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(expression):
        match = _TOKEN_RE.match(expression, position)
        if match is None or match.end() == position:
            if expression[position:].strip() == "":
                break
            raise AssertionError(f"unparsable ARM expression at {position}: {expression!r}")
        position = match.end()
        string, number, ident, punct = match.groups()
        if string is not None:
            tokens.append(("str", string[1:-1].replace("''", "'")))
        elif number is not None:
            tokens.append(("num", number))
        elif ident is not None:
            tokens.append(("ident", ident))
        else:
            tokens.append(("punct", punct))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.index = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self, kind: str, value: str | None = None) -> str:
        token = self.peek()
        assert token is not None and token[0] == kind and (value is None or token[1] == value), (token, kind, value)
        self.index += 1
        return token[1]

    def parse(self) -> tuple[Any, ...]:
        node = self.parse_expression()
        assert self.peek() is None, self.tokens[self.index :]
        return node

    def parse_expression(self) -> tuple[Any, ...]:
        token = self.peek()
        assert token is not None
        if token[0] == "str":
            node: tuple[Any, ...] = ("str", self.take("str"))
        elif token[0] == "num":
            node = ("num", int(self.take("num")))
        else:
            name = self.take("ident")
            self.take("punct", "(")
            args: list[tuple[Any, ...]] = []
            while self.peek() != ("punct", ")"):
                args.append(self.parse_expression())
                if self.peek() == ("punct", ","):
                    self.take("punct", ",")
            self.take("punct", ")")
            node = ("call", name, args)
        while True:
            if self.peek() == ("punct", "["):
                self.take("punct", "[")
                node = ("index", node, self.parse_expression())
                self.take("punct", "]")
            elif self.peek() == ("punct", "."):
                self.take("punct", ".")
                node = ("prop", node, self.take("ident"))
            else:
                return node


class _Evaluator:
    """Evaluates the compiled template's expressions against a parameter set."""

    def __init__(self, template: dict[str, Any], parameters: dict[str, Any]) -> None:
        self.template = template
        self.parameters = dict(parameters)
        for name, definition in template.get("parameters", {}).items():
            if name not in self.parameters and "defaultValue" in definition:
                self.parameters[name] = self.value(definition["defaultValue"])
        self._variables: dict[str, Any] = {}

    def value(self, node: Any, scope: dict[str, Any] | None = None) -> Any:
        if isinstance(node, str):
            if node.startswith("[[") or not (node.startswith("[") and node.endswith("]")):
                return node
            return self._eval(_Parser(_tokenize(node[1:-1])).parse(), scope or {})
        if isinstance(node, dict):
            return {key: self.value(item, scope) for key, item in node.items()}
        if isinstance(node, list):
            return [self.value(item, scope) for item in node]
        return node

    def variable(self, name: str) -> Any:
        if name not in self._variables:
            self._variables[name] = self.value(self.template["variables"][name])
        return self._variables[name]

    def _eval(self, node: tuple[Any, ...], scope: dict[str, Any]) -> Any:
        kind = node[0]
        if kind == "str":
            return node[1]
        if kind == "num":
            return node[1]
        if kind == "index":
            base = self._eval(node[1], scope)
            index = self._eval(node[2], scope)
            return base[index]
        if kind == "prop":
            base = self._eval(node[1], scope)
            if isinstance(base, _Ref):
                return base.child(node[2])
            return base[node[2]]
        assert kind == "call"
        name, args = node[1], node[2]
        if name == "lambda":
            names = [self._eval(arg, scope) for arg in args[:-1]]
            return _Lambda([str(item) for item in names], args[-1])
        values = [self._eval(arg, scope) for arg in args]
        return self._call(name, values, scope)

    def _call(self, name: str, args: list[Any], scope: dict[str, Any]) -> Any:
        match name:
            case "parameters":
                if args[0] not in self.parameters:
                    raise AssertionError(f"parameter {args[0]!r} has no value and no default")
                return self.parameters[args[0]]
            case "variables":
                return self.variable(args[0])
            case "lambdaVariables":
                return scope[args[0]]
            case "createObject":
                return {args[index]: args[index + 1] for index in range(0, len(args), 2)}
            case "createArray":
                return list(args)
            case "concat":
                if all(isinstance(arg, list) for arg in args):
                    return [item for arg in args for item in arg]
                return "".join(str(arg) for arg in args)
            case "union":
                merged: dict[str, Any] = {}
                for arg in args:
                    merged.update(arg)
                return merged
            case "empty":
                return args[0] is None or len(args[0]) == 0
            case "equals":
                return args[0] == args[1]
            case "if":
                return args[1] if args[0] else args[2]
            case "format":
                text = str(args[0])
                for index, arg in enumerate(args[1:]):
                    text = text.replace("{" + str(index) + "}", str(arg))
                return text
            case "json":
                return json.loads(args[0])
            case "null":
                return None
            case "true":
                return True
            case "false":
                return False
            case "string":
                if isinstance(args[0], bool):
                    return "true" if args[0] else "false"
                return str(args[0])
            case "split":
                return str(args[0]).split(str(args[1]))
            case "last":
                return args[0][-1]
            case "take":
                return args[0][: int(args[1])]
            case "replace":
                return str(args[0]).replace(str(args[1]), str(args[2]))
            case "toLower":
                return str(args[0]).lower()
            case "map":
                function = args[1]
                assert isinstance(function, _Lambda)
                return [self._eval(function.body, {**scope, function.names[0]: item}) for item in args[0]]
            case "uniqueString":
                return "uniq0123456789"[:13]
            case "resourceGroup":
                return {
                    "id": "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/elspeth-test",
                    "name": "elspeth-test",
                    "location": "australiaeast",
                }
            case "subscription":
                return {
                    "subscriptionId": "00000000-0000-0000-0000-000000000000",
                    "id": "/subscriptions/00000000-0000-0000-0000-000000000000",
                }
            case "environment":
                return {"name": "AzureCloud", "suffixes": {"storage": "core.windows.net"}}
            case "reference":
                return _Ref(tuple(str(arg) for arg in args))
            case "resourceId" | "subscriptionResourceId" | "extensionResourceId":
                return "<id:" + "/".join(str(arg) for arg in args) + ">"
        raise AssertionError(f"ARM function {name!r} is outside the evaluated subset; keep the templates inside it")


def _resources(template: dict[str, Any]) -> list[dict[str, Any]]:
    resources = template["resources"]
    return list(resources.values()) if isinstance(resources, dict) else list(resources)


def _module_parameters(template_name: str, parameter_file: str, deployment_name: str) -> dict[str, Any]:
    """The resolved parameter values a module deployment passes to its AVM template."""
    template = _template(template_name)
    evaluator = _Evaluator(template, _parameters(parameter_file))
    for resource in _resources(template):
        if resource["type"] != "Microsoft.Resources/deployments":
            continue
        if evaluator.value(resource["name"]) != deployment_name:
            continue
        # A ternary-valued module parameter compiles to ONE expression that
        # yields the whole ``{"value": ...}`` entry; evaluate first, then unwrap.
        return {key: evaluator.value(entry)["value"] for key, entry in resource["properties"]["parameters"].items()}
    raise AssertionError(f"no module deployment named {deployment_name!r} in {template_name}.bicep")


def _module_resource(template_name: str, parameter_file: str, deployment_name: str) -> dict[str, Any]:
    template = _template(template_name)
    evaluator = _Evaluator(template, _parameters(parameter_file))
    for resource in _resources(template):
        if resource["type"] == "Microsoft.Resources/deployments" and evaluator.value(resource["name"]) == deployment_name:
            return resource
    raise AssertionError(deployment_name)


def _env_map(env: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in env}


# ---------------------------------------------------------------------------
# Package shape
# ---------------------------------------------------------------------------


def test_package_contains_only_the_supported_source_and_operator_inputs() -> None:
    present = {path.relative_to(BUNDLE).as_posix() for path in BUNDLE.rglob("*") if path.is_file()}
    assert present == EXPECTED_FILES


def test_ci_pins_the_measured_bicep_release_in_both_lanes() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    facts = PLATFORM_FACTS.read_text(encoding="utf-8")
    assert BICEP_SHA256 in facts
    assert f"`{BICEP_VERSION}`" in facts

    pins = 0
    for job_name in ("test", "azure-container-apps-bicep"):
        job = workflow["jobs"][job_name]
        for step in job["steps"]:
            run = step.get("run")
            if isinstance(run, str) and "bicep-linux-x64" in run:
                assert f"BICEP_VERSION={BICEP_VERSION}" in run, job_name
                assert f"BICEP_SHA256={BICEP_SHA256}" in run, job_name
                assert "sha256sum -c -" in run, job_name
                pins += 1
    assert pins == 2


def test_ci_compiles_every_template_and_parameter_file_and_gates_ci_success() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["azure-container-apps-bicep"]
    runs = "\n".join(step.get("run", "") for step in job["steps"] if isinstance(step.get("run"), str))
    for template in TEMPLATES:
        assert f"bicep build deploy/azure-container-apps/{template}.bicep --stdout" in runs, template
    for parameter_file in PARAMETER_FILES:
        assert f"bicep build-params deploy/azure-container-apps/{parameter_file}.bicepparam --stdout" in runs, parameter_file
    assert "tests/unit/deployment/test_azure_container_apps_bundle.py" in runs
    assert "GITHUB_ACTIONS" not in runs or "GITHUB_ACTIONS=" not in runs

    success = workflow["jobs"]["ci-success"]
    assert "azure-container-apps-bicep" in success["needs"]
    check = "\n".join(step.get("run", "") for step in success["steps"])
    assert 'needs.azure-container-apps-bicep.result }}" != "success"' in check


def test_every_template_compiles_and_every_parameter_file_builds() -> None:
    for template in TEMPLATES:
        # main.bicep is subscription-scoped (subscriptionDeploymentTemplate).
        assert _template(template)["$schema"].endswith("eploymentTemplate.json#")
    for parameter_file, template in PARAMETER_FILES.items():
        values = _parameters(parameter_file)
        declared = set(_template(template)["parameters"])
        assert set(values) <= declared, (parameter_file, set(values) - declared)
        required = {name for name, definition in _template(template)["parameters"].items() if "defaultValue" not in definition}
        assert required <= set(values), (parameter_file, required - set(values))


# ---------------------------------------------------------------------------
# Parameter files
# ---------------------------------------------------------------------------


def test_workload_parameter_files_pin_the_rollout_shapes() -> None:
    production = _parameters("workload.production")
    acceptance = _parameters("workload.acceptance")

    assert production["activeRevisionsMode"] == "Single"
    assert production["stickySessionsAffinity"] == "sticky"
    assert production["minReplicas"] >= 1
    assert production["maxReplicas"] >= 2
    assert production["runtimeRoleLabel"] == ""

    assert acceptance["activeRevisionsMode"] == "Multiple"
    assert acceptance["stickySessionsAffinity"] == "none"
    assert acceptance["minReplicas"] == 1
    assert acceptance["maxReplicas"] == 1
    assert acceptance["runtimeRoleLabel"] == "a"

    for values in (production, acceptance):
        assert values["composerTransportIdleCeilingSeconds"] <= INGRESS_REQUEST_TIMEOUT_SECONDS
        assert DIGEST_IMAGE_RE.match(values["image"]), values["image"]
        assert DIGEST_IMAGE_RE.match(values["provisionStorageImage"]), values["provisionStorageImage"]
        for name, value in values.items():
            if name.endswith("SecretUrl") and value != "":
                assert VERSIONED_SECRET_URL_RE.match(value), (name, value)


def test_platform_parameter_files_never_carry_the_administrator_password() -> None:
    for parameter_file in ("main.example", "main.acceptance", "environment.example"):
        text = (BUNDLE / f"{parameter_file}.bicepparam").read_text(encoding="utf-8")
        assert "param postgresAdministratorPassword = readEnvironmentVariable('ELSPETH_POSTGRES_ADMIN_PASSWORD', '')" in text
        assert _parameters(parameter_file)["postgresAdministratorPassword"] == ""


def test_acceptance_stack_is_disposable_and_the_production_stack_is_not() -> None:
    acceptance = _parameters("main.acceptance")
    production = _parameters("main.example")

    assert acceptance["acceptanceRunId"] != ""
    assert acceptance["keyVaultPurgeProtection"] is False
    assert acceptance["zoneRedundant"] is False
    assert acceptance["postgresPublicNetworkAccess"] == "Enabled"
    assert acceptance["postgresFirewallRules"]
    assert acceptance["keyVaultAllowedIpRules"]

    assert production.get("acceptanceRunId", "") == ""
    assert production["keyVaultPurgeProtection"] is True
    assert production["postgresPublicNetworkAccess"] == "Disabled"
    assert production["postgresFirewallRules"] == []

    template = _template("main")
    evaluator = _Evaluator(template, acceptance)
    group = next(resource for resource in _resources(template) if resource["type"] == "Microsoft.Resources/resourceGroups")
    assert evaluator.value(group["tags"])["elspeth.acceptance-run-id"] == acceptance["acceptanceRunId"]
    assert evaluator.value(group["name"]) == acceptance["resourceGroupName"]


# ---------------------------------------------------------------------------
# Workload: the container app, resolved from compiled ARM
# ---------------------------------------------------------------------------


def test_transport_ceiling_parameter_has_no_default_and_is_capped_at_the_ingress_timeout() -> None:
    definition = _template("workload")["parameters"]["composerTransportIdleCeilingSeconds"]
    assert "defaultValue" not in definition
    assert definition["type"] == "int"
    assert definition["maxValue"] == INGRESS_REQUEST_TIMEOUT_SECONDS
    assert definition["minValue"] >= 1


@pytest.mark.parametrize("parameter_file", ["workload.production", "workload.acceptance"])
def test_container_app_binds_the_runtime_contract_from_compiled_arm(parameter_file: str) -> None:
    values = _parameters(parameter_file)
    app = _module_parameters("workload", parameter_file, f"{values['containerAppName']}-app")

    assert app["activeRevisionsMode"] == values["activeRevisionsMode"]
    assert app["stickySessionsAffinity"] == values["stickySessionsAffinity"]
    assert app["scaleSettings"] == {"minReplicas": values["minReplicas"], "maxReplicas": values["maxReplicas"]}
    assert app["terminationGracePeriodSeconds"] == values["terminationGracePeriodSeconds"]
    assert app["revisionSuffix"] == values["revisionSuffix"]
    assert app["ingressExternal"] is True
    assert app["ingressAllowInsecure"] is False
    assert app["ingressTargetPort"] == 8451
    assert app["workloadProfileName"] == "Consumption"
    if values["activeRevisionsMode"] == "Multiple":
        assert app["traffic"] == [{"latestRevision": True, "weight": 100}]
    else:
        assert app["traffic"] is None

    registry = values["image"].split("/")[0]
    assert app["registries"] == [{"server": registry, "identity": values["identityResourceId"]}]
    assert app["managedIdentities"] == {"userAssignedResourceIds": [values["identityResourceId"]]}

    secrets = {entry["name"]: entry for entry in app["secrets"]}
    assert set(secrets) == {
        "secret-key",
        "shareable-link-signing-key",
        "fingerprint-key",
        "operator-metrics-bearer-token",
        "session-db-url",
        "landscape-url",
    }
    for entry in secrets.values():
        assert set(entry) == {"name", "keyVaultUrl", "identity"}, entry
        assert entry["identity"] == values["identityResourceId"]
        assert VERSIONED_SECRET_URL_RE.match(entry["keyVaultUrl"]), entry
    assert secrets["session-db-url"]["keyVaultUrl"] == values["sessionDbUrlRuntimeSecretUrl"]
    assert secrets["landscape-url"]["keyVaultUrl"] == values["landscapeUrlRuntimeSecretUrl"]

    assert app["volumes"] == [
        {
            "name": "elspeth-state",
            "storageType": "NfsAzureFile",
            "storageName": values["nfsStorageName"],
            "mountOptions": "actimeo=30,nconnect=4,noresvport",
        }
    ]

    (container,) = app["containers"]
    assert container["name"] == "elspeth-web"
    assert container["image"] == values["image"]
    assert container["args"] == ["web", "--host", "0.0.0.0", "--port", "8451"]
    assert container["resources"] == {"cpu": 1.0, "memory": values["webMemory"]}
    assert container["volumeMounts"] == [{"volumeName": "elspeth-state", "mountPath": "/mnt/elspeth"}]

    env = _env_map(container["env"])
    for name, expected in {
        "ELSPETH_WEB__DEPLOYMENT_TARGET": "azure-container-apps",
        "ELSPETH_WEB__DEPLOYMENT_STATE_MODE": "external-postgresql",
        "ELSPETH_WEB__HOST": "0.0.0.0",
        "ELSPETH_WEB__PORT": "8451",
        "WEB_CONCURRENCY": "1",
        "ELSPETH_WEB__LOG_JSON": "true",
        "ELSPETH_WEB__OPERATOR_TELEMETRY": "prometheus",
        "ELSPETH_WEB__DATA_DIR": "/mnt/elspeth/data",
        "ELSPETH_WEB__PAYLOAD_STORE_PATH": "/mnt/elspeth/payloads",
        "ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS": str(values["composerTransportIdleCeilingSeconds"]),
    }.items():
        assert env[name] == {"name": name, "value": expected}, name
    for name, secret in {
        "ELSPETH_WEB__SESSION_DB_URL": "session-db-url",
        "ELSPETH_WEB__LANDSCAPE_URL": "landscape-url",
        "ELSPETH_WEB__SECRET_KEY": "secret-key",
        "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY": "shareable-link-signing-key",
        "ELSPETH_FINGERPRINT_KEY": "fingerprint-key",
        "ELSPETH_WEB__OPERATOR_METRICS_BEARER_TOKEN": "operator-metrics-bearer-token",
    }.items():
        assert env[name] == {"name": name, "secretRef": secret}, name
    for entry in env.values():
        assert not ({"value", "secretRef"} <= set(entry)), entry

    probes = {probe["type"]: probe for probe in container["probes"]}
    assert set(probes) == {"Startup", "Liveness", "Readiness"}
    assert probes["Startup"]["httpGet"] == {"path": "/api/health", "port": 8451, "scheme": "HTTP"}
    assert probes["Startup"]["periodSeconds"] * probes["Startup"]["failureThreshold"] == 150
    assert probes["Liveness"]["httpGet"]["path"] == "/api/health"
    assert probes["Readiness"]["httpGet"]["path"] == "/api/ready"
    for probe in probes.values():
        assert 1 <= probe["failureThreshold"] <= 10, probe
        assert 1 <= probe["periodSeconds"] <= 240, probe
        assert 1 <= probe["timeoutSeconds"] <= 240, probe
        assert probe.get("initialDelaySeconds", 1) <= 60, probe


def test_avm_container_app_wires_session_affinity_from_the_parameter() -> None:
    resource = _module_resource("workload", "workload.production", "elspeth-web-app")
    nested = json.dumps(resource["properties"]["template"])
    assert "stickySessions" in nested
    assert "parameters('stickySessionsAffinity')" in nested
    assert "Microsoft.App/containerApps" in nested


@pytest.mark.parametrize("parameter_file", ["workload.production", "workload.acceptance"])
def test_jobs_share_the_mount_identity_and_digest(parameter_file: str) -> None:
    values = _parameters(parameter_file)
    suffix = f"-{values['runtimeRoleLabel']}" if values["runtimeRoleLabel"] else ""

    provision = _module_parameters("workload", parameter_file, "provision-storage-job")
    schema_init = _module_parameters("workload", parameter_file, "doctor-schema-init-job")
    runtime = _module_parameters("workload", parameter_file, f"doctor-runtime{suffix}-job")

    for job in (provision, schema_init, runtime):
        assert job["triggerType"] == "Manual"
        assert job["manualTriggerConfig"] == {"parallelism": 1, "replicaCompletionCount": 1}
        assert job["replicaRetryLimit"] == 0
        assert job["managedIdentities"] == {"userAssignedResourceIds": [values["identityResourceId"]]}
        assert job["volumes"][0]["storageType"] == "NfsAzureFile"
        assert job["containers"][0]["volumeMounts"] == [{"volumeName": "elspeth-state", "mountPath": "/mnt/elspeth"}]

    assert provision["name"] == "provision-storage"
    (provision_container,) = provision["containers"]
    assert provision_container["image"] == values["provisionStorageImage"]
    script = provision_container["command"][-1]
    assert "chown -R 1654:1654 /mnt/elspeth/data /mnt/elspeth/payloads" in script
    assert "chmod 0700 /mnt/elspeth/data /mnt/elspeth/data/blobs /mnt/elspeth/payloads" in script
    assert "secrets" not in provision

    assert schema_init["name"] == "doctor-schema-init"
    (init_container,) = schema_init["containers"]
    assert init_container["image"] == values["image"]
    assert init_container["args"] == ["doctor", "deployment", "--init-schema", "--json"]
    init_secrets = {entry["name"]: entry["keyVaultUrl"] for entry in schema_init["secrets"]}
    assert init_secrets["session-db-url"] == values["sessionDbUrlSchemaOwnerSecretUrl"]
    assert init_secrets["landscape-url"] == values["landscapeUrlSchemaOwnerSecretUrl"]

    assert runtime["name"] == f"doctor-runtime{suffix}"
    (runtime_container,) = runtime["containers"]
    assert runtime_container["image"] == values["image"]
    assert runtime_container["args"] == ["doctor", "deployment", "--json"]
    runtime_secrets = {entry["name"]: entry["keyVaultUrl"] for entry in runtime["secrets"]}
    assert runtime_secrets["session-db-url"] == values["sessionDbUrlRuntimeSecretUrl"]
    assert runtime_secrets["landscape-url"] == values["landscapeUrlRuntimeSecretUrl"]

    for job in (schema_init, runtime):
        env = _env_map(job["containers"][0]["env"])
        assert env["ELSPETH_WEB__DEPLOYMENT_TARGET"]["value"] == "azure-container-apps"
        assert env["ELSPETH_WEB__DEPLOYMENT_STATE_MODE"]["value"] == "external-postgresql"
        assert env["ELSPETH_WEB__SESSION_DB_URL"] == {"name": "ELSPETH_WEB__SESSION_DB_URL", "secretRef": "session-db-url"}


# ---------------------------------------------------------------------------
# Environment: the storage contract, resolved from compiled ARM
# ---------------------------------------------------------------------------


def test_environment_creates_no_registry_and_pulls_from_the_existing_one() -> None:
    template = _template("environment")
    values = _parameters("environment.example")
    evaluator = _Evaluator(template, values)
    nested_types = {
        resource_type
        for resource in _resources(template)
        for resource_type in re.findall(r'"type": "([^"]+)"', json.dumps(resource.get("properties", {}).get("template", {})))
    }
    assert "Microsoft.ContainerRegistry/registries" not in nested_types

    pull = next(resource for resource in _resources(template) if evaluator.value(resource["name"]) == "elspeth-registry-pull")
    parts = values["containerRegistryResourceId"].split("/")
    assert evaluator.value(pull["subscriptionId"]) == parts[2]
    assert evaluator.value(pull["resourceGroup"]) == parts[4]
    assert evaluator.value(pull["properties"]["parameters"]["registryName"]["value"]) == parts[-1]
    nested = json.dumps(pull["properties"]["template"])
    assert "7f951dda-4ed3-4680-a7ca-43fe172d538d" in nested
    assert "Microsoft.Authorization/roleAssignments" in nested


def test_environment_states_the_storage_contract_once() -> None:
    files = _module_parameters("environment", "environment.example", "elspeth-file-storage")
    assert files["kind"] == "FileStorage"
    assert files["skuName"] == "Premium_LRS"
    assert files["supportsHttpsTrafficOnly"] is False
    assert files["allowSharedKeyAccess"] is False
    assert files["publicNetworkAccess"] == "Disabled"
    assert files["networkAcls"] == {"bypass": "AzureServices", "defaultAction": "Deny"}
    (share,) = files["fileServices"]["shares"]
    assert share["name"] == "elspeth"
    assert share["enabledProtocols"] == "NFS"
    assert share["rootSquash"] == "NoRootSquash"
    (endpoint,) = files["privateEndpoints"]
    assert endpoint["service"] == "file"
    assert "fileDnsZone" in str(endpoint["privateDnsZoneGroup"]["privateDnsZoneGroupConfigs"][0]["privateDnsZoneResourceId"])

    environment = _module_parameters("environment", "environment.example", "elspeth-environment")
    (storage,) = environment["storages"]
    assert storage["kind"] == "NFS"
    assert storage["accessMode"] == "ReadWrite"
    assert storage["name"] == "elspeth-nfs"
    assert "elspeth-file-storage" in str(storage["storageAccountName"]) or "fileStorage" in str(storage["storageAccountName"])
    assert environment["appLogsConfiguration"]["destination"] == "log-analytics"
    assert "logAnalytics" in str(environment["appLogsConfiguration"]["logAnalyticsWorkspaceResourceId"])
    assert environment["workloadProfiles"] == [{"name": "Consumption", "workloadProfileType": "Consumption"}]
    assert environment["internal"] is False
    assert environment["publicNetworkAccess"] == "Enabled"
    assert "vnet" in str(environment["infrastructureSubnetResourceId"])

    postgres = _module_parameters("environment", "environment.example", "elspeth-postgres")
    assert postgres["version"] == "17"
    assert postgres["authConfig"] == {"activeDirectoryAuth": "Disabled", "passwordAuth": "Enabled"}
    assert {database["name"] for database in postgres["databases"]} == {"elspeth_sessions", "elspeth_landscape"}
    assert postgres["publicNetworkAccess"] == "Disabled"
    (postgres_endpoint,) = postgres["privateEndpoints"]
    assert postgres_endpoint["service"] == "postgresqlServer"
    assert postgres["administratorLoginPassword"] == ""

    blob = _module_parameters("environment", "environment.example", "elspeth-blob-storage")
    assert blob["kind"] == "StorageV2"
    (container,) = blob["blobServices"]["containers"]
    assert container["name"] == "elspeth-payloads"
    assert container["roleAssignments"][0]["roleDefinitionIdOrName"] == "Storage Blob Data Contributor"
    assert blob["privateEndpoints"][0]["service"] == "blob"


def test_environment_binds_identity_network_and_key_vault_shapes() -> None:
    vault = _module_parameters("environment", "environment.example", "elspeth-key-vault")
    assert vault["enableRbacAuthorization"] is True
    assert vault["enablePurgeProtection"] is True
    assert vault["publicNetworkAccess"] == "Disabled"
    assert vault["networkAcls"] == {"bypass": "AzureServices", "defaultAction": "Deny", "ipRules": []}
    assert vault["roleAssignments"][0]["roleDefinitionIdOrName"] == "Key Vault Secrets User"
    assert vault["privateEndpoints"][0]["service"] == "vault"

    acceptance_vault = _module_parameters("environment", "main.acceptance", "elspeth-key-vault")
    assert acceptance_vault["enablePurgeProtection"] is False
    assert acceptance_vault["publicNetworkAccess"] == "Enabled"
    assert acceptance_vault["networkAcls"]["ipRules"] == [{"value": ip} for ip in _parameters("main.acceptance")["keyVaultAllowedIpRules"]]

    nsg = _module_parameters("environment", "environment.example", "elspeth-infra-nsg")
    rules = {rule["properties"]["destinationPortRange"]: rule["properties"] for rule in nsg["securityRules"]}
    assert set(rules) == {"2049", "445"}
    for rule in rules.values():
        assert rule["direction"] == "Outbound"
        assert rule["access"] == "Allow"
        assert rule["protocol"] == "Tcp"

    vnet = _module_parameters("environment", "environment.example", "elspeth-vnet")
    infrastructure, private_endpoints = vnet["subnets"]
    assert infrastructure["delegation"] == "Microsoft.App/environments"
    assert "infrastructureNsg" in str(infrastructure["networkSecurityGroupResourceId"])
    assert private_endpoints["privateEndpointNetworkPolicies"] == "Disabled"

    zones = {
        _module_parameters("environment", "environment.example", name)["name"]
        for name in ("elspeth-dns-file", "elspeth-dns-blob", "elspeth-dns-postgres", "elspeth-dns-vault")
    }
    assert zones == {
        "privatelink.file.core.windows.net",
        "privatelink.blob.core.windows.net",
        "privatelink.postgres.database.azure.com",
        "privatelink.vaultcore.azure.net",
    }


# ---------------------------------------------------------------------------
# Driver, queries, and the image publication contract in CI
# ---------------------------------------------------------------------------


def test_acceptance_driver_routes_every_platform_call_through_protected_capture() -> None:
    script = (BUNDLE / "scripts" / "acceptance.sh").read_text(encoding="utf-8")
    result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert os.access(BUNDLE / "scripts" / "acceptance.sh", os.X_OK)

    for line in script.splitlines():
        stripped = line.lstrip()
        for raw in ("az ", "psql ", "bicep ", "curl "):
            assert not stripped.startswith(raw), line
    for helper in ("az_capture", "az_deploy_capture", "az_exec_capture", "bicep_capture", "curl_capture", "psql_capture"):
        assert f"{helper}() {{" in script, helper
    for stage in ("stage_environment", "stage_image", "stage_jobs", "stage_workload", "stage_probes", "stage_evidence", "stage_cleanup"):
        assert f"{stage}()" in script, stage
    assert "facade_not_landed_6b5" in script
    assert 'test "$acr_digest" = "$CANDIDATE_IMAGE_DIGEST"' in script
    assert "docker buildx imagetools create" in script
    assert "sha256sum" in script


def test_kql_queries_use_the_documented_tables_and_window_tokens() -> None:
    for name in ("doctor-report", "run-sentinel-by-replica", "replica-lifecycle", "fence-conflict-409"):
        text = (BUNDLE / "kql" / f"{name}.kql").read_text(encoding="utf-8")
        assert "ContainerAppConsoleLogs_CL" in text or "ContainerAppSystemLogs_CL" in text, name
        assert "__WINDOW_START__" in text and "__WINDOW_END__" in text, name
    assert "Session operation is already active" in (BUNDLE / "kql" / "fence-conflict-409.kql").read_text(encoding="utf-8")


def test_build_push_copies_the_ghcr_digest_to_acr_and_asserts_equality() -> None:
    workflow = yaml.safe_load(BUILD_PUSH_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["build-push"]
    steps = {step.get("name"): step for step in job["steps"]}

    copy = steps["Copy GHCR image to ACR (digest-preserving)"]
    assert copy["id"] == "acr-copy"
    assert "push_acr == 'true'" in copy["if"]
    assert "push_ghcr == 'true'" in copy["if"]
    assert "docker buildx imagetools create" in copy["run"]
    assert "docker buildx imagetools inspect" in copy["run"]
    assert 'test "$ACR_DIGEST" = "$GHCR_DIGEST"' in copy["run"]
    assert "docker buildx build" not in copy["run"]

    build = steps["Build and push to ACR"]
    assert build["id"] == "acr-push"
    assert "push_ghcr != 'true'" in build["if"]

    assert job["outputs"]["acr_digest"] == "${{ steps.acr-push.outputs.digest || steps.acr-copy.outputs.digest }}"
    sign = steps["Sign ACR image digest"]
    assert "acr-copy" in sign["if"] and "acr-push" in sign["if"]
    assert "steps.acr-push.outputs.digest || steps.acr-copy.outputs.digest" in sign["env"]["IMAGE_DIGEST"]
