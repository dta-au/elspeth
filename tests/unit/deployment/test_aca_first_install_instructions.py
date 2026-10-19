"""Offline shell rehearsal: real runbook/helpers, synthetic external commands.

This checks instruction continuity, not ARM compilation or cloud behavior.
Every external command is intercepted; an unexpected invocation fails closed.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / ".agents/skills/operating-azure-container-apps"
COLD = ROOT / "docs/runbooks/azure-container-apps-cold-install.md"
ARCHIVED_FACTS_LINK = (
    "https://github.com/dta-au/elspeth/blob/58b25d524d9c5a2550b0f895ea8f70e1dea9ea51/"
    "docs/plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md"
)


def _blocks(path: Path) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", path.read_text(), re.DOTALL)


def _assert_markdown_target(path: Path, target: str) -> None:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        # Pin the same immutable authority as the ACA runbook contract. CI's
        # shallow checkout need not contain this historical blob locally.
        assert target == ARCHIVED_FACTS_LINK, f"Unexpected archive link: {target}"
    else:
        assert (path.parent / target).resolve().is_file(), target


@pytest.mark.parametrize("target", ["SKILL.md", ARCHIVED_FACTS_LINK], ids=["local", "immutable-archive"])
def test_skill_markdown_target_accepts_existing_documents(target: str) -> None:
    _assert_markdown_target(SKILL / "SKILL.md", target)


@pytest.mark.parametrize(
    "target",
    [
        "references/missing-document-control.md",
        ARCHIVED_FACTS_LINK.replace("github.com", "example.com"),
        ARCHIVED_FACTS_LINK.replace("58b25d524d9c5a2550b0f895ea8f70e1dea9ea51", "main"),
        ARCHIVED_FACTS_LINK.replace("platform-facts.md", "missing-document-control.md"),
        ARCHIVED_FACTS_LINK.replace("https://", "http://"),
        ARCHIVED_FACTS_LINK.replace("https:", ""),
    ],
    ids=["missing-local", "wrong-host", "mutable-ref", "wrong-path", "insecure-scheme", "scheme-relative"],
)
def test_skill_markdown_target_rejects_broken_or_unapproved_documents(target: str) -> None:
    with pytest.raises(AssertionError):
        _assert_markdown_target(SKILL / "SKILL.md", target)


@pytest.mark.parametrize("account", ["enclave-builder", "second-builder"])
def test_registry_login_uses_selected_account_without_printing_token(tmp_path: Path, account: str) -> None:
    block = next(block for block in _blocks(SKILL / "references/build-and-push-ghcr.md") if "gh auth token" in block)
    for command, body in {
        "gh": '[[ "$*" == "auth status" ]] && exit 0\n'
        '[[ "$*" == "auth token --hostname github.com --user $GHCR_ACCOUNT" ]] || exit 91\n'
        "printf 'synthetic-token'\n",
        "docker": '[[ "$*" == "login ghcr.io --username $GHCR_ACCOUNT --password-stdin" ]] || exit 92\n'
        '[[ "$(cat)" == synthetic-token ]] || exit 93\n',
    }.items():
        executable = tmp_path / command
        executable.write_text("#!/bin/bash\nset -eu\n" + body)
        executable.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "GHCR_ACCOUNT": account}
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", block], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "synthetic-token" not in result.stdout + result.stderr
    env.pop("GHCR_ACCOUNT")
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", block], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "set the GitHub account" in result.stderr


def test_skill_routes_first_install_and_preserves_existing_store_hold() -> None:
    for relative in ("SKILL.md", "references/command-cheatsheet.md", "references/test-and-triage.md"):
        path = SKILL / relative
        text = path.read_text()
        assert "Current policy: new stores only." in text
        assert "rollback are held, even if an older compatibility record permits rollback" in " ".join(text.split())
        for target in re.findall(r"\]\(([^)]+\.md)\)", text):
            _assert_markdown_target(path, target)
    build = (SKILL / "references/build-and-push-ghcr.md").read_text()
    assert "continue at step 3" in build
    assert "johnm-dta" not in build


def test_cold_install_native_azure_instructions_end_to_end_offline(tmp_path: Path) -> None:
    """Run steps 1-9 in one shell, with manual selections at their stated seam."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    secret_names = (
        "session-db-url-runtime",
        "landscape-url-runtime",
        "session-db-url-schema-owner",
        "landscape-url-schema-owner",
        "secret-key",
        "shareable-link-signing-key",
        "fingerprint-key",
        "operator-metrics-bearer-token",
        "azure-api-key",
        "sso-client-secret",
        "sso-transaction-secret",
        "composer-prompt-guard",
        "composer-advisor-prompt-guard",
    )
    for name in secret_names:
        (secrets / f"elspeth-{name}").write_text("synthetic-private-value")
    # No Azure, Docker, git, PostgreSQL, Bicep, or HTTP process can escape these
    # stand-ins. The real bash, jq, resolver, validator and Job waiter still run.
    fake = """#!/usr/bin/python3
import json, os, pathlib, sys
cmd = pathlib.Path(sys.argv[0]).name
a = sys.argv[1:]
root = pathlib.Path(os.environ["REHEARSAL_ROOT"])
with (root / "events.jsonl").open("a") as log:
    log.write(json.dumps([cmd, *a]) + "\\n")
sha = "b" * 40
digest = "sha256:" + "c" * 64
prefix = "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/aca-live/providers/"
def value(flag):
    return a[a.index(flag) + 1]
if cmd == "git":
    if a[:1] == ["rev-parse"]: print(sha)
    else: assert a == ["status", "--porcelain"]
elif cmd == "bicep":
    assert a[0] == "build-params"
    pathlib.Path(value("--outfile")).write_text(json.dumps({"parameters": {
        "containerRegistryResourceId": {"value": os.environ["ACR_RESOURCE_ID"]},
        "postgresAdministratorPassword": {"value": "synthetic-private-value"}}}))
elif cmd == "psql":
    assert a == ["--no-psqlrc", "--set=ON_ERROR_STOP=1", "--file", "deploy/azure-container-apps/scripts/bootstrap-roles.sql"]
    assert os.environ["PGSSLMODE"] == "verify-full"
    (root / "roles").touch()
elif cmd == "docker":
    if a[:3] == ["buildx", "imagetools", "inspect"]: print(digest)
    elif a[:2] == ["image", "inspect"]: print(sha)
    elif a[:3] == ["buildx", "imagetools", "create"]: (root / "image-copied").touch()
    else: assert a[0] in ("pull", "run")
elif cmd == "curl":
    assert (root / "app").exists()
    if a[-1].endswith("/api/ready"): print('{"ready":true}')
    elif a[-1].endswith("/api/health"): print('{"status":"ok"}')
    else:
        assert a[-1].endswith("/api/system/status")
        print('{"deployment_target":"azure_container_apps","composer_available":true}')
elif cmd == "az":
    if a[:3] == ["deployment", "group", "create"]:
        if value("--name") == "elspeth-environment":
            outputs = {
                "environmentResourceId": prefix + "Microsoft.App/managedEnvironments/aca-env",
                "identityResourceId": prefix + "Microsoft.ManagedIdentity/userAssignedIdentities/aca-id",
                "schemaOwnerIdentityResourceId": prefix + "Microsoft.ManagedIdentity/userAssignedIdentities/aca-schema-id",
                "identityClientId": "12345678-1234-1234-1234-123456789abc",
                "keyVaultName": "aca-vault", "schemaOwnerKeyVaultName": "aca-owner",
                "nfsStorageName": "elspeth", "environmentDefaultDomain": "example.com"}
            print(json.dumps({k: {"value": v} for k, v in outputs.items()}))
        elif "deployWebApp=false" in a:
            assert (root / "roles").exists() and (root / "image-copied").exists()
            (root / "jobs").touch()
        else:
            assert "deployWebApp=true" in a and (root / "doctor-runtime").exists()
            (root / "app").touch()
    elif a[:3] == ["containerapp", "job", "start"]:
        assert (root / "jobs").exists()
        print("exact-execution")
    elif a[:4] == ["containerapp", "job", "execution", "show"]:
        assert value("--job-execution-name") == "exact-execution"
        name = value("--name")
        if name == "doctor-schema-init": assert (root / "provision-storage").exists()
        if name == "doctor-runtime": assert (root / "doctor-schema-init").exists()
        (root / name).touch()
        print("Succeeded")
    elif a[:3] == ["keyvault", "secret", "set"]:
        name = value("--name")
        vault = value("--vault-name")
        assert vault == ("aca-owner" if name.endswith("-schema-owner") else "aca-vault")
        assert pathlib.Path(value("--file")).read_text() == "synthetic-private-value"
        print("https://" + vault + ".vault.azure.net/secrets/" + name + "/" + "a" * 32)
    elif a[:2] == ["keyvault", "show"]: print(prefix + "Microsoft.KeyVault/vaults/" + value("--name"))
    elif a[:3] == ["acr", "manifest", "show-metadata"]: print(digest)
    elif a[:2] == ["containerapp", "show"]: print("elspeth-web.example.com")
    else:
        assert tuple(a[:2]) in {("account", "set"), ("account", "show"), ("group", "create"),
            ("acr", "show"), ("acr", "config"), ("acr", "login"), ("role", "assignment"),
            ("containerapp", "revision"), ("containerapp", "replica")} or a[:3] == ["deployment", "group", "what-if"], a
else: raise AssertionError(cmd)
"""
    for command in ("git", "bicep", "psql", "docker", "curl", "az"):
        executable = bin_dir / command
        executable.write_text(fake)
        executable.chmod(0o755)
    blocks = re.findall(r"```bash\n(.*?)```", COLD.read_text().split("## Prerequisites", 1)[1], re.DOTALL)
    # The custom-endpoint credential alternative, optional Search and teardown
    # are explicitly separate paths; this rehearsal selects native Azure.
    selected = [
        block
        for block in blocks
        if "COMPOSER_ENDPOINT_SECRET_NAME=" not in block and "SEARCH_SERVICE_NAME:?" not in block and "az group delete" not in block
    ]
    selections = """
python3 - "$APPLICATION_PARAMETERS" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text()
s = s.replace("REPLACE_PRIMARY_PRICING_MODEL", "azure/gpt-4o")
s = s.replace("REPLACE_ADVISOR_PRICING_MODEL", "azure/gpt-4o-mini")
s = re.sub(r"REPLACE_[A-Z_]+", "synthetic-selection", s)
p.write_text(s)
PY
"""
    script = "ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS=210\n"
    for block in selected:
        script += block + "\n"
        if 'chmod 600 "$APPLICATION_PARAMETERS"' in block:
            script += selections
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "REHEARSAL_ROOT": str(tmp_path),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "AZURE_SUBSCRIPTION_ID": "synthetic-subscription",
        "AZURE_LOCATION": "australiaeast",
        "RESOURCE_GROUP": "aca-live",
        "ACR_RESOURCE_ID": "/subscriptions/12345678/resourceGroups/aca-live/providers/Microsoft.ContainerRegistry/registries/aca",
        "ACR_LOGIN_SERVER": "aca.azurecr.io",
        "DEPLOY_REF": "b" * 40,
        "GHCR_DIGEST": "sha256:" + "c" * 64,
        "ELSPETH_POSTGRES_ADMIN_PASSWORD": "synthetic-private-value",
        "ENVIRONMENT_PARAMETERS": str(tmp_path / "environment.bicepparam"),
        "OPERATOR_OBJECT_ID": "synthetic-operator",
        "PGHOST": "aca.postgres.database.azure.com",
        "PGUSER": "admin",
        "PGPASSWORD": "synthetic-private-value",
        "PGSSLROOTCERT": "/synthetic/roots.pem",
        "ELSPETH_SCHEMA_OWNER_PASSWORD": "synthetic-owner",
        "ELSPETH_RUNTIME_PASSWORD": "synthetic-runtime",
        "SECRET_VALUE_DIR": str(secrets),
        "PROVISION_STORAGE_IMAGE": "mcr.microsoft.com/azurelinux/base/core@sha256:" + "d" * 64,
    }
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "synthetic-private-value" not in result.stdout + result.stderr
    assert (tmp_path / "app").exists()
    operator = tmp_path / "state/elspeth/azure-container-apps/aca-live"
    params = json.loads((operator / f"workload-{'b' * 40}.parameters.json").read_text())["parameters"]
    assert params["composerTransportIdleCeilingSeconds"]["value"] == 210
    assert params["image"]["value"] == "aca.azurecr.io/elspeth@sha256:" + "c" * 64
    assert not (operator / "environment.parameters.json").exists()
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events[-1] == ["curl", "--silent", "--fail-with-body", "https://elspeth-web.example.com/api/system/status"]


@pytest.mark.parametrize("missing_role", (None, "composer", "advisor"), ids=("both-configured", "missing-composer", "missing-advisor"))
def test_application_example_supplies_both_required_guard_mounts_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_role: str | None
) -> None:
    """Check real parsing/mounts/availability, without asking a provider to run."""
    import base64
    import os
    from unittest.mock import MagicMock

    from elspeth.web.catalog.protocol import CatalogService
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.config import settings_from_env

    text = (ROOT / "deploy/azure-container-apps/application.example.json").read_text()
    for placeholder, value in {
        "REPLACE_PRIMARY_PRICING_MODEL": "azure/gpt-4o",
        "REPLACE_ADVISOR_PRICING_MODEL": "azure/gpt-4o-mini",
        "REPLACE_PRIMARY_DEPLOYMENT": "enclave-primary",
        "REPLACE_ADVISOR_DEPLOYMENT": "enclave-advisor",
        "REPLACE_AZURE_RESOURCE": "enclave-models",
        "REPLACE_AZURE_API_VERSION": "2024-10-21",
        "REPLACE_ENTRA_TENANT_ID": "12345678-1234-1234-1234-123456789abc",
        "REPLACE_ENTRA_CLIENT_ID": "12345678-1234-1234-1234-123456789def",
        "REPLACE_APP_FQDN": "elspeth-web.example.com",
        "REPLACE_FIRST_ADMIN_SUBJECT": "enclave-admin",
    }.items():
        text = text.replace(placeholder, value)
    application = json.loads(text)
    # These stand for secret-store values. The example must supply the pointers;
    # this probe never injects a guard variable the example failed to declare.
    secret_values = {
        "azure-api-key": "synthetic-provider-value",
        "sso-client-secret": "synthetic-client-value",
        "sso-transaction-secret": "synthetic-transaction-value",
        "composer-prompt-guard": json.dumps(
            {
                "adapter": "azure_prompt_shields",
                "endpoint": "https://composer-shields.example.test",
                "subscription_key": "synthetic-composer-shield-value",
                "timeout_seconds": 10.0,
            }
        ),
        "composer-advisor-prompt-guard": json.dumps(
            {
                "adapter": "azure_prompt_shields",
                "endpoint": "https://advisor-shields.example.test",
                "subscription_key": "synthetic-advisor-shield-value",
                "timeout_seconds": 10.0,
            }
        ),
    }
    for key in tuple(os.environ):
        if key.startswith("ELSPETH_WEB__") or key in {"AWS_REGION", "AWS_DEFAULT_REGION"}:
            monkeypatch.delenv(key)
    monkeypatch.setenv("ELSPETH_WEB__DATA_DIR", str(tmp_path / "web"))
    monkeypatch.setenv("ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY", base64.b64encode(b"synthetic-test-value-32-bytes-long").decode())
    for parameter, field in (
        ("composerMaxCompositionTurns", "COMPOSER_MAX_COMPOSITION_TURNS"),
        ("composerMaxDiscoveryTurns", "COMPOSER_MAX_DISCOVERY_TURNS"),
        ("composerTimeoutSeconds", "COMPOSER_TIMEOUT_SECONDS"),
        ("composerRateLimitPerMinute", "COMPOSER_RATE_LIMIT_PER_MINUTE"),
        ("composerModel", "COMPOSER_MODEL"),
        ("composerAdvisorModel", "COMPOSER_ADVISOR_MODEL"),
        ("authProvider", "AUTH_PROVIDER"),
        ("registrationMode", "REGISTRATION_MODE"),
    ):
        monkeypatch.setenv("ELSPETH_WEB__" + field, str(application[parameter]))
    for entry in application["extraEnvironment"]:
        value = entry["value"] if "value" in entry else secret_values[entry["secretRef"]]
        monkeypatch.setenv(entry["name"], value)
    if missing_role is not None:
        missing_name = "COMPOSER_PROMPT_GUARD" if missing_role == "composer" else "COMPOSER_ADVISOR_PROMPT_GUARD"
        monkeypatch.delenv("ELSPETH_WEB__" + missing_name, raising=False)
    settings = settings_from_env()
    service = ComposerServiceImpl.for_trained_operator(catalog=MagicMock(spec=CatalogService), settings=settings)
    availability = service.get_availability()
    assert settings.composer_prompt_guard_mode == "required"
    assert availability.available == (missing_role is None), availability.reason
    if missing_role is not None:
        assert availability.reason == f"Composer is unavailable: required prompt guard configuration is missing for {missing_role}."
        return
    assert settings.composer_prompt_guard is not None
    assert settings.composer_advisor_prompt_guard is not None
    assert settings.composer_prompt_guard.endpoint == "https://composer-shields.example.test"
    assert settings.composer_advisor_prompt_guard.endpoint == "https://advisor-shields.example.test"
    assert settings.composer_prompt_guard.subscription_key.get_secret_value() == "synthetic-composer-shield-value"
    assert settings.composer_advisor_prompt_guard.subscription_key.get_secret_value() == "synthetic-advisor-shield-value"
    assert service.composer_prompt_guard_mount is not None
    assert service._advisor_prompt_guard_mount is not None
    assert type(service.composer_prompt_guard_mount.client) is AzurePromptGuardClient
    assert type(service._advisor_prompt_guard_mount.client) is AzurePromptGuardClient
    assert service.composer_prompt_guard_mount is not service._advisor_prompt_guard_mount
    references = {entry["name"]: entry["secretRef"] for entry in application["extraEnvironment"] if "secretRef" in entry}
    assert references["ELSPETH_WEB__COMPOSER_PROMPT_GUARD"] == "composer-prompt-guard"
    assert references["ELSPETH_WEB__COMPOSER_ADVISOR_PROMPT_GUARD"] == "composer-advisor-prompt-guard"
