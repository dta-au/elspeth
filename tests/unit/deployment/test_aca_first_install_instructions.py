"""Offline shell rehearsal: real runbook/helpers, synthetic external commands.

This checks instruction continuity, not ARM compilation or cloud behavior.
Every external command is intercepted; an unexpected invocation fails closed.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / ".agents/skills/operating-azure-container-apps"
COLD = ROOT / "docs/runbooks/azure-container-apps-cold-install.md"


def _blocks(path: Path) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", path.read_text(), re.DOTALL)


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
            assert (path.parent / target).resolve().is_file(), target
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
