"""Exercise cold-start inputs through the real shell resolver and jq validator."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "deploy/azure-container-apps/scripts"
SECRET_NAMES = (
    "elspeth-session-db-url-runtime",
    "elspeth-landscape-url-runtime",
    "elspeth-session-db-url-schema-owner",
    "elspeth-landscape-url-schema-owner",
    "elspeth-secret-key",
    "elspeth-shareable-link-signing-key",
    "elspeth-fingerprint-key",
    "elspeth-operator-metrics-bearer-token",
)


@pytest.fixture
def launch(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    prefix = "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/aca-live/providers/"
    outputs = {
        "environmentResourceId": prefix + "Microsoft.App/managedEnvironments/aca-env",
        "identityResourceId": prefix + "Microsoft.ManagedIdentity/userAssignedIdentities/aca-id",
        "schemaOwnerIdentityResourceId": prefix + "Microsoft.ManagedIdentity/userAssignedIdentities/aca-schema-id",
        "identityClientId": "12345678-1234-1234-1234-123456789abc",
        "nfsStorageName": "elspeth",
        "keyVaultName": "aca-vault",
        "schemaOwnerKeyVaultName": "aca-schema-vault",
    }
    output_file = tmp_path / "environment.json"
    output_file.write_text(json.dumps({key: {"value": value} for key, value in outputs.items()}))
    application = tmp_path / "application.json"
    application.write_text(
        json.dumps(
            {
                "composerMaxCompositionTurns": 24,
                "composerMaxDiscoveryTurns": 12,
                "composerTimeoutSeconds": 180,
                "composerRateLimitPerMinute": 10,
                "authProvider": "entra",
            }
        )
    )
    for name in SECRET_NAMES:
        vault = "aca-schema-vault" if name.endswith("-schema-owner") else "aca-vault"
        (tmp_path / f"{name}.version").write_text(f"https://{vault}.vault.azure.net/secrets/{name}/{'a' * 32}\n")
    azure = tmp_path / "az"
    azure.write_text("""#!/usr/bin/env python3
import os
import sys
from pathlib import Path
args = sys.argv[1:]
with (Path(os.environ["SECRET_VERSION_DIR"]) / "azure-calls").open("a") as stream:
    stream.write(repr(args) + "\\n")
if args[:3] == ["keyvault", "secret", "show"]:
    vault = args[args.index("--vault-name") + 1]
    name = args[args.index("--name") + 1]
    print(f"https://{vault}.vault.azure.net/secrets/{name}/" + "b" * 32)
elif args[:3] == ["containerapp", "job", "start"]:
    print("doctor-runtime-rc123")
elif args[:4] == ["containerapp", "job", "execution", "show"]:
    assert args[args.index("--job-execution-name") + 1] == "doctor-runtime-rc123"
    print("Succeeded")
else:
    raise AssertionError(args)
""")
    azure.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "APPLICATION_PARAMETERS": str(application),
        "SECRET_VERSION_DIR": str(tmp_path),
        "CANDIDATE_IMAGE": "registry.azurecr.io/elspeth@sha256:" + "c" * 64,
        "CANDIDATE_SHA": "d" * 40,
        "PROVISION_STORAGE_IMAGE": "mcr.microsoft.com/azurelinux/base/core@sha256:" + "e" * 64,
        "ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS": "210",
    }
    env.pop("COMPOSER_ENDPOINT_SECRET_NAME", None)
    env.pop("COMPOSER_ADVISOR_ENDPOINT_SECRET_NAME", None)
    return env, output_file, tmp_path / "resolved.json"


def _resolve(launch: tuple[dict[str, str], Path, Path]) -> subprocess.CompletedProcess[str]:
    env, outputs, target = launch
    return subprocess.run(
        ["bash", str(SCRIPTS / "resolve-workload-parameters.sh"), str(outputs), str(target)],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _update_application(env: dict[str, str], values: dict[str, object]) -> None:
    path = Path(env["APPLICATION_PARAMETERS"])
    current = json.loads(path.read_text())
    current.update(values)
    path.write_text(json.dumps(current))


def test_resolver_preserves_uploaded_version_when_latest_has_changed(launch: tuple[dict[str, str], Path, Path]) -> None:
    env, _, target = launch
    result = _resolve(launch)
    assert result.returncode == 0, result.stderr
    parameters = json.loads(target.read_text())["parameters"]
    assert parameters["sessionDbUrlRuntimeSecretUrl"]["value"].endswith("/" + "a" * 32)
    assert not (Path(env["SECRET_VERSION_DIR"]) / "azure-calls").exists()
    assert parameters["composerMaxCompositionTurns"]["value"] == 24
    assert parameters["authProvider"]["value"] == "entra"
    assert parameters["registrationMode"]["value"] == "closed"
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("missing", ["SECRET_VERSION_DIR", "APPLICATION_PARAMETERS"])
def test_resolver_requires_operator_inputs(launch: tuple[dict[str, str], Path, Path], missing: str) -> None:
    env, _, target = launch
    del env[missing]
    result = _resolve(launch)
    assert result.returncode != 0
    assert missing in result.stderr
    assert not target.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("minReplicas", "2"),
        ("maxReplicas", 2.5),
        ("minReplicas", True),
        ("composerMaxCompositionTurns", None),
        ("composerMaxDiscoveryTurns", 0),
        ("composerTimeoutSeconds", 1.5),
        ("composerRateLimitPerMinute", "10"),
        ("authProvider", "local"),
        ("authProvider", "unknown"),
        ("image", "registry.azurecr.io/elspeth:latest"),
        ("identityResourceId", "replacement"),
        ("sessionDbUrlRuntimeSecretUrl", "replacement"),
        ("composerEndpointBaseUrl", "https://llm.contoso.com/v1"),
        ("composerAdvisorEndpointBaseUrl", "https://llm.contoso.com/v1"),
    ],
)
def test_resolver_rejects_invalid_or_reserved_application_fields(
    launch: tuple[dict[str, str], Path, Path],
    field: str,
    value: object,
) -> None:
    env, _, target = launch
    _update_application(env, {field: value})
    result = _resolve(launch)
    assert result.returncode != 0
    assert not target.exists()


def test_resolver_requires_every_captured_version(launch: tuple[dict[str, str], Path, Path]) -> None:
    env, _, target = launch
    (Path(env["SECRET_VERSION_DIR"]) / f"{SECRET_NAMES[0]}.version").unlink()
    result = _resolve(launch)
    assert result.returncode != 0
    assert not target.exists()


def test_job_reports_execution_name(launch: tuple[dict[str, str], Path, Path]) -> None:
    env, _, _ = launch
    result = subprocess.run(
        ["bash", str(SCRIPTS / "run-job.sh"), "aca-live", "doctor-runtime"], env=env, cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "doctor-runtime-rc123" in result.stdout + result.stderr


def test_resolver_wires_independent_endpoints_and_extra_secrets(launch: tuple[dict[str, str], Path, Path]) -> None:
    env, _, target = launch
    for selector, name in (("COMPOSER_ENDPOINT_SECRET_NAME", "planner-key"), ("COMPOSER_ADVISOR_ENDPOINT_SECRET_NAME", "advisor-key")):
        env[selector] = name
        (Path(env["SECRET_VERSION_DIR"]) / f"{name}.version").write_text(f"https://aca-vault.vault.azure.net/secrets/{name}/{'f' * 32}\n")
    _update_application(
        env,
        {
            "composerEndpointBaseUrl": "https://planner.contoso.com/v1",
            "composerAdvisorEndpointBaseUrl": "https://advisor.contoso.com/v1",
            "composerModel": "azure/planner",
            "composerAdvisorModel": "azure/advisor",
            "extraSecrets": [{"name": "sso-client", "keyVaultUrl": "https://aca-vault.vault.azure.net/secrets/sso-client/" + "f" * 32}],
            "extraEnvironment": [
                {"name": "ELSPETH_WEB__ENTRA_CLIENT_SECRET", "secretRef": "sso-client"},
                {"name": "ELSPETH_WEB__COMPARTMENT_ID", "value": "soft-launch"},
            ],
        },
    )
    result = _resolve(launch)
    assert result.returncode == 0, result.stderr
    parameters = json.loads(target.read_text())["parameters"]
    assert "/planner-key/" in parameters["composerEndpointApiKeySecretUrl"]["value"]
    assert "/advisor-key/" in parameters["composerAdvisorEndpointApiKeySecretUrl"]["value"]
    assert parameters["extraEnvironment"]["value"][0]["secretRef"] == "sso-client"


@pytest.mark.parametrize(
    "field,value",
    [
        ("minReplicas", "2"),
        ("maxReplicas", 2.5),
        ("composerMaxCompositionTurns", True),
        ("composerTransportIdleCeilingSeconds", 241),
        ("composerTimeoutSeconds", 181),
        ("extraEnvironment", [{"name": "ELSPETH_WEB__COMPOSER_TRANSPORT_HEADROOM_SECONDS", "value": "20"}]),
        ("extraEnvironment", [{"name": "ELSPETH_WEB__SSO_CLIENT_ID", "value": "REPLACE_CLIENT_ID"}]),
        ("tags", {"operator": "replace_name"}),
        ("extraSecrets", False),
        ("extraEnvironment", None),
        ("composerModel", None),
        ("composerAdvisorEndpointBaseUrl", None),
        ("extraSecrets", [{"name": "session-db-url", "keyVaultUrl": "https://aca-vault.vault.azure.net/secrets/override/" + "f" * 32}]),
        ("extraSecrets", [{"name": "sso", "keyVaultUrl": "https://aca-schema-vault.vault.azure.net/secrets/sso/" + "f" * 32}]),
        ("extraSecrets", [{"name": "sso", "keyVaultUrl": "https://aca-vault.vault.azure.net/secrets/sso"}]),
        ("extraSecrets", [{"name": "sso", "keyVaultUrl": "https://aca-vault.vault.azure.net/secrets/sso/" + "f" * 32}] * 2),
        ("extraEnvironment", [{"name": "ELSPETH_WEB__SESSION_DB_URL", "value": "override"}]),
        ("extraEnvironment", [{"name": "elspeth_web__auth_provider", "value": "local"}]),
        ("extraEnvironment", [{"name": "PROVIDER_KEY", "secretRef": "missing"}]),
        ("extraEnvironment", [{"name": "PROVIDER_KEY", "secretRef": "secret-key", "value": "also"}]),
        ("extraEnvironment", [{"name": "CUSTOM", "value": "one"}, {"name": "custom", "value": "two"}]),
        ("composerEndpointApiKeySecretUrl", "https://aca-vault.vault.azure.net/secrets/planner/" + "f" * 32),
        ("composerAdvisorEndpointApiKeySecretUrl", "https://aca-vault.vault.azure.net/secrets/advisor/" + "f" * 32),
    ],
)
def test_validator_rejects_mutated_resolved_parameters(
    launch: tuple[dict[str, str], Path, Path],
    field: str,
    value: object,
) -> None:
    _, _, target = launch
    result = _resolve(launch)
    assert result.returncode == 0, result.stderr
    command = ["jq", "-e", "-f", str(SCRIPTS / "validate-workload-parameters.jq"), str(target)]
    assert subprocess.run(command, capture_output=True, text=True).returncode == 0
    parameters = json.loads(target.read_text())
    parameters["parameters"][field] = {"value": value}
    target.write_text(json.dumps(parameters))
    assert subprocess.run(command, capture_output=True, text=True).returncode != 0


@pytest.mark.parametrize(
    "secret_id",
    [
        "https://aca-schema-vault.vault.azure.net/secrets/elspeth-secret-key/" + "a" * 32,
        "https://aca-vault.vault.azure.net/secrets/wrong-name/" + "a" * 32,
        "https://aca-vault.vault.azure.net/secrets/elspeth-secret-key/latest",
    ],
)
def test_resolver_rejects_swapped_or_unversioned_capture(
    launch: tuple[dict[str, str], Path, Path],
    secret_id: str,
) -> None:
    env, _, target = launch
    (Path(env["SECRET_VERSION_DIR"]) / "elspeth-secret-key.version").write_text(secret_id)
    result = _resolve(launch)
    assert result.returncode != 0
    assert not target.exists()
