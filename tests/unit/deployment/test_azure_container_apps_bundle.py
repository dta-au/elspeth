"""Static contract tests for the Azure Container Apps deployment bundle."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BICEP = REPO_ROOT / "deploy" / "azure-container-apps" / "main.bicep"
PARAMETERS = REPO_ROOT / "deploy" / "azure-container-apps" / "main.example.bicepparam"
DOCKERFILE = REPO_ROOT / "Dockerfile"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yaml"

ZERO_UUID = "00000000-0000-0000-0000-000000000000"
BICEP_VERSION = "v0.44.1"
BICEP_SHA256 = "e17dc9a9888184886bb0c0051a3230b83b19f342749999f707bc571c3dfd2f45"


def _bicep_text() -> str:
    return BICEP.read_text(encoding="utf-8")


def _parameter_text() -> str:
    return PARAMETERS.read_text(encoding="utf-8")


def _env_block(text: str, name: str) -> str:
    match = re.search(rf"{{\s*name: '{re.escape(name)}'(?P<body>.*?)\n\s*}}", text, re.DOTALL)
    assert match is not None, f"missing environment entry {name}"
    return match.group(0)


def test_module_requires_existing_platform_resources_and_secret_urls() -> None:
    text = _bicep_text()

    required_parameters = {
        "containerAppsEnvironmentId",
        "image",
        "userAssignedIdentityResourceId",
        "nfsStorageName",
        "storageSubPath",
        "sessionDatabaseSecretUrl",
        "landscapeDatabaseSecretUrl",
        "webSecretKeySecretUrl",
        "shareableLinkSigningKeySecretUrl",
        "fingerprintKeySecretUrl",
    }
    for name in required_parameters:
        assert re.search(rf"^param {name} string$", text, re.MULTILINE), name

    resources = re.findall(r"^resource\s+\w+\s+'([^']+)'", text, re.MULTILINE)
    assert resources == ["Microsoft.App/containerApps@2024-03-01"]
    assert "Microsoft.DBforPostgreSQL" not in text
    assert "Microsoft.App/managedEnvironments" not in text


def test_module_fixes_single_revision_and_replica_contract() -> None:
    text = _bicep_text()

    assert "activeRevisionsMode: 'Single'" in text
    assert "minReplicas: 1" in text
    assert "maxReplicas: 1" in text
    assert "targetPort: 8451" in text
    assert "ELSPETH_WEB__DEPLOYMENT_TARGET" in text
    assert "value: 'azure-container-apps'" in _env_block(text, "ELSPETH_WEB__DEPLOYMENT_TARGET")
    assert "value: 'external-postgresql'" in _env_block(text, "ELSPETH_WEB__DEPLOYMENT_STATE_MODE")


def test_module_binds_key_vault_references_without_inline_secret_values() -> None:
    text = _bicep_text()

    secret_bindings = {
        "ELSPETH_WEB__SESSION_DB_URL": "session-database-url",
        "ELSPETH_WEB__LANDSCAPE_URL": "landscape-database-url",
        "ELSPETH_WEB__SECRET_KEY": "web-secret-key",
        "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY": "shareable-link-signing-key",
        "ELSPETH_FINGERPRINT_KEY": "fingerprint-key",
    }
    for env_name, secret_name in secret_bindings.items():
        block = _env_block(text, env_name)
        assert f"secretRef: '{secret_name}'" in block
        assert "value:" not in block

    for secret_name, parameter_name in {
        "session-database-url": "sessionDatabaseSecretUrl",
        "landscape-database-url": "landscapeDatabaseSecretUrl",
        "web-secret-key": "webSecretKeySecretUrl",
        "shareable-link-signing-key": "shareableLinkSigningKeySecretUrl",
        "fingerprint-key": "fingerprintKeySecretUrl",
    }.items():
        match = re.search(rf"name: '{secret_name}'(?P<body>.*?)(?=\n\s*}}|\n\s*])", text, re.DOTALL)
        assert match is not None, secret_name
        block = match.group(0)
        assert f"keyVaultUrl: {parameter_name}" in block
        assert "identity: userAssignedIdentityResourceId" in block
        assert "value:" not in block

    assert "connectionString" not in text
    assert "password" not in text.lower()


def test_module_mounts_operator_prepared_nfs_state_without_privilege_repair() -> None:
    text = _bicep_text()

    assert "storageType: 'NfsAzureFile'" in text
    assert "storageName: nfsStorageName" in text
    assert "subPath: storageSubPath" in text
    assert "value: '/mnt/elspeth/data'" in _env_block(text, "ELSPETH_WEB__DATA_DIR")
    assert "value: '/mnt/elspeth/payloads'" in _env_block(text, "ELSPETH_WEB__PAYLOAD_STORE_PATH")
    assert "data/blobs" in text
    assert "payloads" in text
    assert "UID/GID 1000" in text
    assert "0700" in text
    for forbidden in ("runAsUser", "securityContext", "initContainer", "chown", "chmod"):
        assert forbidden not in text


def test_module_sets_composer_defaults_probes_and_exact_web_command() -> None:
    text = _bicep_text()

    composer_contract = {
        "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS": ("composerMaxCompositionTurns", 15),
        "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS": ("composerMaxDiscoveryTurns", 10),
        "ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS": ("composerTimeoutSeconds", 85),
        "ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE": ("composerRateLimitPerMinute", 10),
    }
    for env_name, (parameter_name, default) in composer_contract.items():
        assert re.search(rf"^param {parameter_name} int = {default}$", text, re.MULTILINE)
        assert f"value: string({parameter_name})" in _env_block(text, env_name)

    assert "type: 'Liveness'" in text
    assert "path: '/api/health'" in text
    assert "type: 'Readiness'" in text
    assert "path: '/api/ready'" in text
    assert re.search(
        r"command:\s*\[\s*'elspeth'\s*'web'\s*'--host'\s*'0\.0\.0\.0'\s*'--port'\s*'8451'\s*]",
        text,
        re.DOTALL,
    )
    assert re.search(r"(?:^|[/:])latest(?:$|[\s\"'}])", text, re.MULTILINE) is None


def test_example_parameters_are_inert_specific_and_compilable() -> None:
    text = _parameter_text()

    assert text.startswith("using './main.bicep'\n")
    assert text.count(ZERO_UUID) >= 2
    assert "param location = 'australiaeast'" in text
    assert "param image = 'elspethexample.azurecr.io/elspeth:sha-0000000000000000000000000000000000000000'" in text
    assert "param nfsStorageName = 'elspeth-nfs'" in text
    assert "param storageSubPath = 'elspeth'" in text
    vault_origin = "https://elspeth-example.vault.azure.net/secrets/"
    for secret_name in (
        "session-db-url",
        "landscape-db-url",
        "web-secret-key",
        "shareable-link-signing-key",
        "fingerprint-key",
    ):
        assert f"{vault_origin}{secret_name}" in text
    for forbidden in ("password", "credential", "connectionString", "secretValue", "latest"):
        assert forbidden.lower() not in text.lower()


def test_container_runtime_identity_remains_uid_gid_1000() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "groupadd --gid 1000 elspeth" in text
    assert "useradd --uid 1000 --gid elspeth" in text
    assert re.search(r"^USER elspeth$", text, re.MULTILINE)


def test_ci_pins_and_verifies_bicep_before_compiling_both_artifacts() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")

    assert BICEP_VERSION in text
    assert "https://github.com/Azure/bicep/releases/download/" in text
    assert BICEP_SHA256 in text
    assert "urllib.request.urlretrieve(url, destination)" in text
    checksum_position = text.index(BICEP_SHA256)
    verification_position = text.index("sha256sum -c -", checksum_position)
    chmod_position = text.index("chmod +x /tmp/bicep")
    build_position = text.index("/tmp/bicep build deploy/azure-container-apps/main.bicep --stdout >/dev/null")
    params_position = text.index("/tmp/bicep build-params deploy/azure-container-apps/main.example.bicepparam --stdout >/dev/null")
    assert checksum_position < verification_position < chmod_position < build_position < params_position
    assert "urllib.request" in text[:build_position]
