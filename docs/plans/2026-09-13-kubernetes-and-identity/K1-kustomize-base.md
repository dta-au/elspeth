### Task K1: Provider-neutral Kustomize base at the multi-replica bar

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: K0. Runs before: K2, K6. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

**Files:**
- Create: `deploy/kubernetes/base/kustomization.yaml`
- Create: `deploy/kubernetes/base/configmap.yaml`
- Create: `deploy/kubernetes/base/secret.example.yaml`
- Create: `deploy/kubernetes/base/secret-schema-owner.example.yaml`
- Create: `deploy/kubernetes/base/bootstrap-roles.sql`
- Create: `deploy/kubernetes/base/bootstrap-acceptance-roles.sql`
- Create: `deploy/kubernetes/base/pvc.yaml`
- Create: `deploy/kubernetes/base/service.yaml`
- Create: `deploy/kubernetes/base/deployment.yaml`
- Create: `deploy/kubernetes/base/job-provision-storage.yaml`
- Create: `deploy/kubernetes/base/job-schema-init.yaml`
- Test: `tests/unit/deployment/test_kubernetes_bundle.py`
- Modify: `tests/unit/docs/test_deployment_platform_docs.py:72-74` (the `for absent_bundle in ("kubernetes", "platforms"):` loop whose `:74` assertion requires `deploy/kubernetes` to be empty; drop `"kubernetes"`)
- Modify: `.github/workflows/ci.yaml:752-754` (the `test` job: the Bicep install step ends at `:752` with `bicep --version`, `:753` is blank, `Install dependencies` starts at `:754`; the kubectl step is inserted between them)

**Interfaces:**
- Consumes: from Task K0, `docs/plans/2026-09-13-kubernetes-platform-facts.md` carrying the kubectl pin as `` `v1.37.0` `` and its SHA-256 `6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f` (both read from `https://dl.k8s.io/release/stable.txt` and `https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl.sha256` on 2026-09-13; if K0's re-verification records a different pair, the two constants in the test module and the two variables in the CI step take K0's values), K0's §1.3 `PROVISION_STORAGE_IMAGE=busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0` (the index digest of `busybox:1.37.0`; K1 writes it fully qualified as `docker.io/library/busybox@sha256:…` so it satisfies `DIGEST_IMAGE_RE`, and the test asserts the bare digest appears in the facts document), and K0's Step 7 measurement that `fsGroup` is not applied to a hostPath mount (why the share is provisioned by a root Job).
- Produces, for Task K2: the downward-API env var names `ELSPETH_K8S_POD_NAME` (= `metadata.name`) and `ELSPETH_K8S_REVISION` (= `metadata.annotations['elspeth.io/revision']`), and `ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE` in the ConfigMap.
- Produces, for Task K3: module-level symbols in `tests/unit/deployment/test_kubernetes_bundle.py` that K3's pin test extends rather than redefines — `REPO_ROOT`, `CI_WORKFLOW`, `PLATFORM_FACTS`, `KUBECTL_VERSION = "1.37.0"`, `KUBECTL_SHA256`, `_require_kubectl(reason: str) -> None`, `_render() -> tuple[dict[str, Any], ...]`, `_shipped_files() -> set[str]`; the CI step name `Install kubectl (Kubernetes bundle contract tests)` in the `test` job with shell variables `KUBECTL_VERSION=` / `KUBECTL_SHA256=` and the download URL `https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl` (K3's `kubernetes-render` job repeats the same block with `sudo install`, and its pin test counts `pins == 2` over the `test` and `kubernetes-render` jobs, mirroring `test_azure_container_apps_bundle.py:509-527`).
- Produces, for Tasks K4, K5 and K6 (the overlays): the base directory `deploy/kubernetes/base/`; ConfigMap `elspeth-web-config`; Secret names `elspeth-web-secrets` (keys `ELSPETH_WEB__SESSION_DB_URL`, `ELSPETH_WEB__LANDSCAPE_URL`, `ELSPETH_WEB__SECRET_KEY`, `ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY`; runtime role) and `elspeth-schema-owner-secrets` (the two URL keys; schema-owner role; referenced only by Job `elspeth-schema-init`); PVC `elspeth-state` (`ReadWriteMany`, no `storageClassName` — the overlay names the class or binds a PV); Service `elspeth-web` (ClusterIP, port `8451`, named port `http`, `sessionAffinity: ClientIP`); Deployment `elspeth-web` (`replicas: 2`) whose pod-template labels and selector are exactly `{"app.kubernetes.io/name": "elspeth-web", "app.kubernetes.io/component": "web"}` — Job pods never carry `app.kubernetes.io/name`, so `kubectl get pods -l app.kubernetes.io/name=elspeth-web` counts web pods only; Jobs `elspeth-provision-storage` and `elspeth-schema-init` (`ttlSecondsAfterFinished: 600`, `backoffLimit: 0`); the two placeholders the overlay MUST replace, `elspeth.io/revision: REPLACE_PER_ROLLOUT` (pod-template annotation) and `ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE: REPLACE_PER_ROLLOUT` (ConfigMap); the four composer settings `WebSettings` requires with no default (`config.py:306` `composer_max_composition_turns`, `:307` `composer_max_discovery_turns`, `:309` `composer_timeout_seconds`, `:338` `composer_rate_limit_per_minute`), carried in the base ConfigMap as `ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS: "15"`, `ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS: "10"`, `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS: "85"`, `ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE: "10"` (the compose bundle's values, `deploy/compose/web-postgres.yaml:14-17`), so every overlay boots without restating them and an overlay that needs a different value (K6's `180`, K7's `15`) merges over them; and the image references. The overlay patches ONE through kustomize `images:`, `ghcr.io/dta-au/elspeth` (web container and schema-init container, identical digest). The provision-storage image `docker.io/library/busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0` is pinned in the base from K0 §1.3 and pulls as-is, so no overlay rewrites it.
- Produces, for Tasks K4, K5 and K8: `deploy/kubernetes/base/bootstrap-roles.sql` (roles `elspeth_schema_owner`, `elspeth_runtime`; the production operator runs it with `psql --file` in K8's runbook) and `deploy/kubernetes/base/bootstrap-acceptance-roles.sql` (`\ir bootstrap-roles.sql` then roles `elspeth_runtime_a`, `elspeth_runtime_b`; K4's kind harness runs it at PostgreSQL init, where its `02-roles.sql` does `\ir elspeth/bootstrap-acceptance-roles.sql`, so the kind cluster gets all four application roles through this one file). K5 runs neither file: it consumes `elspeth_runtime_a` and `elspeth_runtime_b` as K4's harness creates them and edits no K4 harness file.

Two Secrets, not one: `elspeth doctor deployment --init-schema` needs DDL, the web pods must not hold DDL (Phase 6b §4 row 1; `deploy/azure-container-apps/workload.bicep:299-300` gives `doctorEnvironment` the schema-owner URLs and `webEnvironment` the runtime URLs). The schema-init Job still mounts `elspeth-web-secrets` because `WebSettings` refuses to load without `shareable_link_signing_key` (`config.py:620`, `Field(...)`) and `secret_key`; its two database URLs come from `elspeth-schema-owner-secrets` through `env`, which Kubernetes resolves ahead of every `envFrom` source, so the runtime URLs never win by accident.

Two SQL files, not one: `\getenv` leaves `:'runtime_a_password'` unexpanded when `ELSPETH_RUNTIME_A_PASSWORD` is unset and psql then fails the whole script, so a single four-role file would force a production operator to invent acceptance passwords. The split mirrors `deploy/azure-container-apps/scripts/bootstrap-roles.sql` and `bootstrap-acceptance-roles.sql` exactly.

- [ ] **Step 1: Write the failing source-contract test.**

```python
# tests/unit/deployment/test_kubernetes_bundle.py
"""Contract tests for the provider-neutral Kubernetes base.

Every structural assertion runs against the RENDERED document set
(``kubectl kustomize deploy/kubernetes/base``), never against the YAML
files, so a kustomization that drops a resource or a label transformer that
leaks onto a Job pod template is caught here rather than in kind.

``kubectl`` is pinned by version and SHA-256 in ``.github/workflows/ci.yaml``
(the pin the platform facts record). Like the Terraform and Bicep bundle
tests, a missing binary skips locally and fails loudly in CI. Server
admission of the rendered objects is proven by the kind lane
(``tests/testcontainer/deployment/test_kubernetes_kind.py``): a client
dry-run still needs an API server for REST-mapper discovery, so none is
attempted here.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE = REPO_ROOT / "deploy" / "kubernetes" / "base"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
PLATFORM_FACTS = REPO_ROOT / "docs" / "plans" / "2026-09-13-kubernetes-platform-facts.md"

# The kubectl pin: https://dl.k8s.io/release/stable.txt -> v1.37.0 and
# https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl.sha256, both read
# 2026-09-13. Task K0 re-verifies and records them in PLATFORM_FACTS; the pin
# test below asserts the workflow and the facts document carry the same pair.
KUBECTL_VERSION = "1.37.0"
KUBECTL_SHA256 = "6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f"

DIGEST_IMAGE_RE = re.compile(r"^[a-z0-9.-]+(?::\d+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
RELEASE_IMAGE_PREFIX = "ghcr.io/dta-au/elspeth@sha256:"
# PROVISION_STORAGE_IMAGE from PLATFORM_FACTS §1.3 (Task K0): the index digest
# of busybox:1.37.0. The base pins it and no overlay rewrites it, so a zero or
# drifted digest here would leave the Job in ImagePullBackOff in kind.
PROVISION_STORAGE_DIGEST = "sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0"
# The composer settings WebSettings declares Field(...) with no default
# (config.py:306, :307, :309, :338); values are deploy/compose/web-postgres.yaml:14-17.
REQUIRED_COMPOSER_SETTINGS = {
    "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS": "15",
    "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS": "10",
    "ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS": "85",
    "ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE": "10",
}
RUNTIME_SECRET = "elspeth-web-secrets"
SCHEMA_OWNER_SECRET = "elspeth-schema-owner-secrets"
RUNTIME_SECRET_KEYS = {
    "ELSPETH_WEB__SESSION_DB_URL",
    "ELSPETH_WEB__LANDSCAPE_URL",
    "ELSPETH_WEB__SECRET_KEY",
    "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY",
}
SCHEMA_OWNER_SECRET_KEYS = {"ELSPETH_WEB__SESSION_DB_URL", "ELSPETH_WEB__LANDSCAPE_URL"}
WEB_POD_SELECTOR = {"app.kubernetes.io/name": "elspeth-web", "app.kubernetes.io/component": "web"}
ROLLOUT_PLACEHOLDER = "REPLACE_PER_ROLLOUT"
JOB_TTL_SECONDS = 600


# ---------------------------------------------------------------------------
# kubectl invocation
# ---------------------------------------------------------------------------


def _require_kubectl(reason: str) -> None:
    """Skip locally when kubectl is absent; fail loudly in CI.

    The ``test`` job installs a checksum-pinned kubectl, so absence under
    GITHUB_ACTIONS (or ELSPETH_CI_KUBECTL_REQUIRED, for a local run that must
    not skip) is a broken gate, not an environment quirk -- the same rule
    ``test_azure_container_apps_bundle.py`` applies to bicep.
    """
    if shutil.which("kubectl") is not None:
        return
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("ELSPETH_CI_KUBECTL_REQUIRED"):
        pytest.fail(f"kubectl binary is missing in CI: {reason}")
    pytest.skip(f"kubectl is not installed, so {reason}")


@functools.cache
def _render() -> tuple[dict[str, Any], ...]:
    _require_kubectl("the Kubernetes base cannot be rendered")
    result = subprocess.run(["kubectl", "kustomize", str(BASE)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    documents = tuple(doc for doc in yaml.safe_load_all(result.stdout) if doc)
    assert all(isinstance(doc, dict) for doc in documents)
    return documents


def _one(kind: str) -> dict[str, Any]:
    matches = [doc for doc in _render() if doc["kind"] == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, found {len(matches)}"
    return matches[0]


def _named(kind: str, name: str) -> dict[str, Any]:
    matches = [doc for doc in _render() if doc["kind"] == kind and doc["metadata"]["name"] == name]
    assert len(matches) == 1, f"expected exactly one {kind}/{name}, found {len(matches)}"
    return matches[0]


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["spec"]["template"]["spec"]


def _only_container(doc: dict[str, Any]) -> dict[str, Any]:
    (container,) = _pod_spec(doc)["containers"]
    return container


def _kustomization() -> dict[str, Any]:
    document = yaml.safe_load((BASE / "kustomization.yaml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _shipped_files() -> set[str]:
    """Tracked plus untracked-not-ignored files under the base, by git.

    ``git`` is the oracle for "shipped": an operator's gitignored
    ``secret.local.yaml`` (``.gitignore`` ``deploy/**/*.local.yaml``) must not
    turn this inventory red, and a stray tracked file must.
    """
    listed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "--", "deploy/kubernetes/base"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return {Path(line).name for line in listed}


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def test_base_renders_exactly_the_supported_resource_kinds() -> None:
    kinds = sorted(doc["kind"] for doc in _render())
    assert kinds == ["ConfigMap", "Deployment", "Job", "Job", "PersistentVolumeClaim", "Service"]
    assert {doc["metadata"]["name"] for doc in _render() if doc["kind"] == "Job"} == {
        "elspeth-provision-storage",
        "elspeth-schema-init",
    }


def test_kustomization_uses_labels_and_lists_only_applied_resources() -> None:
    kustomization = _kustomization()
    assert "commonLabels" not in kustomization  # deprecated in kustomize v5
    (label_entry,) = kustomization["labels"]
    assert label_entry["pairs"] == {"app.kubernetes.io/name": "elspeth-web", "app.kubernetes.io/part-of": "elspeth"}
    assert label_entry.get("includeSelectors", False) is False
    assert label_entry.get("includeTemplates", False) is False
    assert kustomization["resources"] == [
        "configmap.yaml",
        "pvc.yaml",
        "service.yaml",
        "deployment.yaml",
        "job-provision-storage.yaml",
        "job-schema-init.yaml",
    ]
    assert _shipped_files() == set(kustomization["resources"]) | {
        "kustomization.yaml",
        "secret.example.yaml",
        "secret-schema-owner.example.yaml",
        "bootstrap-roles.sql",
        "bootstrap-acceptance-roles.sql",
    }


def test_base_ships_no_database_ingress_tls_or_cloud_identity() -> None:
    kinds = {doc["kind"] for doc in _render()}
    assert kinds.isdisjoint({"Secret", "StatefulSet", "Ingress", "Certificate", "StorageClass", "SecretProviderClass", "ServiceAccount"})


# ---------------------------------------------------------------------------
# Secrets and roles
# ---------------------------------------------------------------------------


def test_secret_examples_are_documentation_only() -> None:
    resources = _kustomization()["resources"]
    for filename, name, keys in (
        ("secret.example.yaml", RUNTIME_SECRET, RUNTIME_SECRET_KEYS),
        ("secret-schema-owner.example.yaml", SCHEMA_OWNER_SECRET, SCHEMA_OWNER_SECRET_KEYS),
    ):
        assert filename not in resources
        example = yaml.safe_load((BASE / filename).read_text(encoding="utf-8"))
        assert example["kind"] == "Secret"
        assert example["metadata"]["name"] == name
        assert set(example["stringData"]) == keys
        assert all(value.startswith("REPLACE_ME") for value in example["stringData"].values())
    runtime = yaml.safe_load((BASE / "secret.example.yaml").read_text(encoding="utf-8"))["stringData"]
    # config.py:594/:805 base64-decode the signing key; the recipe is `openssl rand -base64 32`.
    assert runtime["ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY"] == "REPLACE_ME_base64_of_32_random_bytes"
    assert "openssl rand -base64 32" in (BASE / "secret.example.yaml").read_text(encoding="utf-8")


def test_schema_init_runs_as_the_schema_owner_and_the_web_pod_does_not() -> None:
    schema_init = _only_container(_named("Job", "elspeth-schema-init"))
    assert schema_init["args"] == ["doctor", "deployment", "--init-schema"]
    # `env` beats every `envFrom` source, so the schema-owner URLs win over the
    # runtime URLs that elspeth-web-secrets also carries.
    overrides = {entry["name"]: entry["valueFrom"]["secretKeyRef"] for entry in schema_init["env"]}
    assert overrides == {key: {"name": SCHEMA_OWNER_SECRET, "key": key} for key in SCHEMA_OWNER_SECRET_KEYS}
    env_from = [(next(iter(source)), next(iter(source.values()))["name"]) for source in schema_init["envFrom"]]
    assert env_from == [("configMapRef", "elspeth-web-config"), ("secretRef", RUNTIME_SECRET)]

    deployment_text = yaml.safe_dump(_one("Deployment"))
    assert SCHEMA_OWNER_SECRET not in deployment_text
    web = _only_container(_one("Deployment"))
    assert [(next(iter(source)), next(iter(source.values()))["name"]) for source in web["envFrom"]] == [
        ("configMapRef", "elspeth-web-config"),
        ("secretRef", RUNTIME_SECRET),
    ]


def test_bootstrap_sql_mirrors_the_aca_role_split() -> None:
    roles = (BASE / "bootstrap-roles.sql").read_text(encoding="utf-8")
    acceptance = (BASE / "bootstrap-acceptance-roles.sql").read_text(encoding="utf-8")
    for statement in (
        "CREATE ROLE elspeth_schema_owner LOGIN PASSWORD :'schema_owner_password'",
        "CREATE ROLE elspeth_runtime LOGIN PASSWORD :'runtime_password'",
        "ALTER DATABASE elspeth_sessions OWNER TO elspeth_schema_owner;",
        "ALTER DATABASE elspeth_landscape OWNER TO elspeth_schema_owner;",
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
        "GRANT CONNECT ON DATABASE elspeth_sessions, elspeth_landscape TO elspeth_runtime;",
    ):
        assert statement in roles
    assert "\\getenv schema_owner_password ELSPETH_SCHEMA_OWNER_PASSWORD" in roles
    assert "\\getenv runtime_password ELSPETH_RUNTIME_PASSWORD" in roles
    assert "elspeth_runtime_a" not in roles and "elspeth_runtime_b" not in roles
    statements = [line for line in acceptance.splitlines() if line and not line.startswith("--")]
    assert statements[0] == "\\ir bootstrap-roles.sql"  # production roles first, whatever the comment block says
    for role in ("elspeth_runtime_a", "elspeth_runtime_b"):
        assert f"CREATE ROLE {role} LOGIN PASSWORD :'{role.removeprefix('elspeth_')}_password'" in acceptance
    # Passwords come from the environment, never from a literal in a tracked file.
    assert re.search(r"PASSWORD\s+'", roles + acceptance) is None


# ---------------------------------------------------------------------------
# Storage provisioning and Jobs
# ---------------------------------------------------------------------------


def test_share_is_provisioned_by_a_root_job_not_by_the_application_pod() -> None:
    job = _named("Job", "elspeth-provision-storage")
    assert _pod_spec(job)["securityContext"] == {"runAsUser": 0, "runAsGroup": 0}
    container = _only_container(job)
    assert container["image"] == f"docker.io/library/busybox@{PROVISION_STORAGE_DIGEST}"
    assert DIGEST_IMAGE_RE.match(container["image"])
    assert f"busybox@{PROVISION_STORAGE_DIGEST}" in PLATFORM_FACTS.read_text(encoding="utf-8")
    script = " ".join(container["command"])
    assert "mkdir -p /mnt/elspeth/data/blobs /mnt/elspeth/payloads" in script
    assert "chown -R 1654:1654 /mnt/elspeth/data /mnt/elspeth/payloads" in script
    assert "chmod 0700 /mnt/elspeth/data /mnt/elspeth/data/blobs /mnt/elspeth/payloads" in script
    assert [mount["mountPath"] for mount in container["volumeMounts"]] == ["/mnt/elspeth"]
    assert "initContainers" not in _pod_spec(_one("Deployment"))


def test_jobs_expire_so_a_redeploy_can_recreate_them() -> None:
    for name in ("elspeth-provision-storage", "elspeth-schema-init"):
        job = _named("Job", name)
        assert job["spec"]["ttlSecondsAfterFinished"] == JOB_TTL_SECONDS
        assert job["spec"]["backoffLimit"] == 0
        assert _pod_spec(job)["restartPolicy"] == "Never"
        assert _pod_spec(job)["volumes"] == [{"name": "state", "persistentVolumeClaim": {"claimName": "elspeth-state"}}]


def test_job_pods_never_match_the_web_pod_selector() -> None:
    for name in ("elspeth-provision-storage", "elspeth-schema-init"):
        labels = _named("Job", name)["spec"]["template"]["metadata"]["labels"]
        assert "app.kubernetes.io/name" not in labels
        assert labels["app.kubernetes.io/component"] != "web"
    assert _one("Deployment")["spec"]["template"]["metadata"]["labels"] == WEB_POD_SELECTOR
    assert _one("Deployment")["spec"]["selector"]["matchLabels"] == WEB_POD_SELECTOR


def test_schema_init_runs_as_the_application_identity_without_privileges() -> None:
    job = _named("Job", "elspeth-schema-init")
    assert _pod_spec(job)["securityContext"] == {"runAsUser": 1654, "runAsGroup": 1654, "fsGroup": 1654, "runAsNonRoot": True}
    assert _only_container(job)["securityContext"] == {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}


# ---------------------------------------------------------------------------
# Deployment, ConfigMap, PVC, Service
# ---------------------------------------------------------------------------


def test_deployment_carries_the_multi_replica_contract() -> None:
    spec = _one("Deployment")["spec"]
    assert spec["replicas"] == 2
    assert spec["strategy"] == {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}
    pod = spec["template"]["spec"]
    assert pod["securityContext"] == {"runAsUser": 1654, "runAsGroup": 1654, "fsGroup": 1654, "runAsNonRoot": True}
    assert pod["terminationGracePeriodSeconds"] >= 60
    web = _only_container(_one("Deployment"))
    assert web["args"] == ["web", "--host", "0.0.0.0", "--port", "8451"]
    env = {entry["name"]: entry for entry in web["env"]}
    assert env["WEB_CONCURRENCY"]["value"] == "1"
    assert env["ELSPETH_K8S_POD_NAME"]["valueFrom"] == {"fieldRef": {"fieldPath": "metadata.name"}}
    assert env["ELSPETH_K8S_REVISION"]["valueFrom"] == {"fieldRef": {"fieldPath": "metadata.annotations['elspeth.io/revision']"}}
    # The instance id is minted per process (deployment_profiles.py:249), never
    # pinned to the pod name: register() (membership_authority.py:191) refuses a
    # live unstopped lease under the same id, so a container restarting inside
    # one pod would CrashLoop until its own lease expired.
    assert "ELSPETH_WEB__INSTANCE_ID" not in env
    assert web["securityContext"] == {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    assert web["startupProbe"]["httpGet"] == {"path": "/api/health", "port": "http"}
    assert web["startupProbe"]["periodSeconds"] * web["startupProbe"]["failureThreshold"] >= 150
    assert web["livenessProbe"]["httpGet"] == {"path": "/api/health", "port": "http"}
    assert web["readinessProbe"]["httpGet"] == {"path": "/api/ready", "port": "http"}
    assert web["ports"] == [{"name": "http", "containerPort": 8451}]
    assert web["resources"]["requests"]["memory"] == web["resources"]["limits"]["memory"]
    assert [mount["mountPath"] for mount in web["volumeMounts"]] == ["/mnt/elspeth"]
    assert pod["volumes"] == [{"name": "state", "persistentVolumeClaim": {"claimName": "elspeth-state"}}]


def test_web_and_schema_init_run_the_same_release_image() -> None:
    web_image = _only_container(_one("Deployment"))["image"]
    schema_init_image = _only_container(_named("Job", "elspeth-schema-init"))["image"]
    assert web_image == schema_init_image
    assert web_image.startswith(RELEASE_IMAGE_PREFIX)
    assert DIGEST_IMAGE_RE.match(web_image)


def test_configmap_binds_the_runtime_contract() -> None:
    cm = _one("ConfigMap")
    assert cm["metadata"]["name"] == "elspeth-web-config"
    data = cm["data"]
    assert data["ELSPETH_WEB__DEPLOYMENT_TARGET"] == "kubernetes"
    assert data["ELSPETH_WEB__DEPLOYMENT_STATE_MODE"] == "external-postgresql"
    assert data["ELSPETH_WEB__HOST"] == "0.0.0.0"
    assert data["ELSPETH_WEB__PORT"] == "8451"
    assert data["ELSPETH_WEB__DATA_DIR"] == "/mnt/elspeth/data"
    assert data["ELSPETH_WEB__PAYLOAD_STORE_PATH"] == "/mnt/elspeth/payloads"
    assert data["ELSPETH_WEB__LOG_JSON"] == "true"
    assert data["ELSPETH_WEB__OPERATOR_TELEMETRY"] == "prometheus"
    # Without these four every pod and the schema-init doctor fail WebSettings
    # validation with `Field required`, whichever overlay renders the base.
    assert {key: data.get(key) for key in REQUIRED_COMPOSER_SETTINGS} == REQUIRED_COMPOSER_SETTINGS
    assert set(data).isdisjoint(RUNTIME_SECRET_KEYS)
    assert "image_digest" in (BASE / "configmap.yaml").read_text(encoding="utf-8")
    assert "image_identity in web_instances" not in (BASE / "configmap.yaml").read_text(encoding="utf-8")


def test_base_carries_both_rollout_placeholders() -> None:
    # Both literals pass _PLATFORM_IDENTITY_VALUE (deployment_profiles.py:70),
    # so the base renders and boots; the overlay MUST replace them and K4
    # asserts through /api/system/status and web_instances that it did.
    assert _one("ConfigMap")["data"]["ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE"] == ROLLOUT_PLACEHOLDER
    assert _one("Deployment")["spec"]["template"]["metadata"]["annotations"]["elspeth.io/revision"] == ROLLOUT_PLACEHOLDER


def test_pvc_is_read_write_many_and_no_storage_class_is_chosen() -> None:
    pvc = _one("PersistentVolumeClaim")
    assert pvc["metadata"]["name"] == "elspeth-state"
    assert pvc["spec"]["accessModes"] == ["ReadWriteMany"]
    assert "storageClassName" not in pvc["spec"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "100Gi"


def test_service_pins_affinity_and_named_port() -> None:
    svc = _one("Service")["spec"]
    assert svc["type"] == "ClusterIP"
    assert svc["sessionAffinity"] == "ClientIP"
    assert svc["selector"] == WEB_POD_SELECTOR
    assert svc["ports"] == [{"name": "http", "port": 8451, "targetPort": "http"}]


# ---------------------------------------------------------------------------
# CI pin
# ---------------------------------------------------------------------------


def test_ci_test_job_installs_the_pinned_kubectl() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    facts = PLATFORM_FACTS.read_text(encoding="utf-8")
    assert KUBECTL_SHA256 in facts
    assert f"`v{KUBECTL_VERSION}`" in facts
    steps = [step for step in workflow["jobs"]["test"]["steps"] if "dl.k8s.io/release" in step.get("run", "")]
    assert len(steps) == 1, "the test job installs kubectl exactly once"
    run = steps[0]["run"]
    assert f"KUBECTL_VERSION={KUBECTL_VERSION}\n" in run
    assert f"KUBECTL_SHA256={KUBECTL_SHA256}\n" in run
    assert "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" in run
    assert "printf '%s  /tmp/kubectl\\n' \"$KUBECTL_SHA256\" | sha256sum -c -" in run
    assert "install -m 0755 /tmp/kubectl /usr/local/bin/kubectl" in run
    assert "sudo" not in run  # python:*-bookworm container job, runs as root
    bicep_index = next(i for i, step in enumerate(workflow["jobs"]["test"]["steps"]) if step.get("name", "").startswith("Install Bicep"))
    assert workflow["jobs"]["test"]["steps"].index(steps[0]) == bicep_index + 1
```

- [ ] **Step 2: Put the pinned kubectl on PATH, run the test, watch it fail.**

`kubectl` is absent on the development box (`which kubectl` prints nothing); without it the thirteen rendering tests SKIP and only four fail, which proves nothing about the base. Put the K0 pin in K0's tool directory (`.claude/lanes/k8s/bin`, gitignored; K0 Step 1 may already have installed it there, and re-downloading is harmless) and verify it against the same sha the CI step will carry:

```bash
cd "$(git rev-parse --show-toplevel)" && mkdir -p .claude/lanes/k8s/bin
curl -fsSLo .claude/lanes/k8s/bin/kubectl "https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl"
printf '%s  .claude/lanes/k8s/bin/kubectl\n' 6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f | sha256sum -c -
chmod 0755 .claude/lanes/k8s/bin/kubectl
export PATH="$PWD/.claude/lanes/k8s/bin:$PATH"
kubectl version --client
```

Expected: `.claude/lanes/k8s/bin/kubectl: OK`, then `Client Version: v1.37.0` / `Kustomize Version: v5.8.1`.

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/deployment/test_kubernetes_bundle.py -n 0 > /tmp/lane-k-k1-red.log 2>&1; echo exit=$?; grep -E "^(FAILED|SKIPPED)|passed|failed" /tmp/lane-k-k1-red.log`
Expected: `exit=1`, `17 failed`. The thirteen rendering tests fail with `AssertionError: error: must build at directory: not a valid directory: evalsymlink failure on '<repo>/deploy/kubernetes/base' : lstat <repo>/deploy/kubernetes/base: no such file or directory` (kustomize's stderr, surfaced by `_render`; `<repo>` is the absolute checkout path); `test_kustomization_uses_labels_and_lists_only_applied_resources`, `test_secret_examples_are_documentation_only` and `test_bootstrap_sql_mirrors_the_aca_role_split` fail with `FileNotFoundError: [Errno 2] No such file or directory: '<repo>/deploy/kubernetes/base/kustomization.yaml'` (or the example / SQL file); `test_ci_test_job_installs_the_pinned_kubectl` fails with `AssertionError: the test job installs kubectl exactly once` / `assert 0 == 1`. (If it fails one line earlier, on `assert KUBECTL_SHA256 in facts`, K0 recorded a different pin: copy K0's version and sha into `KUBECTL_VERSION` / `KUBECTL_SHA256` and re-download.) With kubectl NOT on PATH the same command prints `4 failed, 13 skipped` with `SKIPPED [13] tests/unit/deployment/test_kubernetes_bundle.py:<the pytest.skip line in _require_kubectl>: kubectl is not installed, so the Kubernetes base cannot be rendered`; with `ELSPETH_CI_KUBECTL_REQUIRED=1` and no kubectl every rendering test fails with `Failed: kubectl binary is missing in CI: the Kubernetes base cannot be rendered` — that is the CI behaviour, since `GITHUB_ACTIONS` takes the same branch.

- [ ] **Step 3: Write the base.**

```yaml
# deploy/kubernetes/base/kustomization.yaml
# Provider-neutral base. Overlays (deploy/kubernetes/overlays/*) supply the
# image digest, the per-rollout revision stamp, storage class, ingress and
# secrets; the base never carries a secret value.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
labels:
  # Object-level labels only. includeSelectors and includeTemplates stay at
  # their false defaults: the Deployment declares its own pod-template labels
  # and selector, and the Job pod templates must never match the web Service
  # selector or a pod count keyed on app.kubernetes.io/name=elspeth-web.
  - pairs:
      app.kubernetes.io/name: elspeth-web
      app.kubernetes.io/part-of: elspeth
resources:
  - configmap.yaml
  - pvc.yaml
  - service.yaml
  - deployment.yaml
  - job-provision-storage.yaml
  - job-schema-init.yaml
# Deliberately NOT resources: secret.example.yaml and
# secret-schema-owner.example.yaml (documentation only; an overlay or the
# operator creates the real Secrets), bootstrap-roles.sql and
# bootstrap-acceptance-roles.sql (run with psql, not applied).
```

```yaml
# deploy/kubernetes/base/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: elspeth-web-config
data:
  ELSPETH_WEB__DEPLOYMENT_TARGET: kubernetes
  ELSPETH_WEB__DEPLOYMENT_STATE_MODE: external-postgresql
  ELSPETH_WEB__HOST: "0.0.0.0"
  ELSPETH_WEB__PORT: "8451"
  ELSPETH_WEB__DATA_DIR: /mnt/elspeth/data
  ELSPETH_WEB__PAYLOAD_STORE_PATH: /mnt/elspeth/payloads
  ELSPETH_WEB__LOG_JSON: "true"
  ELSPETH_WEB__OPERATOR_TELEMETRY: prometheus
  # Required by WebSettings with no default (config.py:306, :307, :309, :338):
  # without them every pod and the schema-init doctor fail with
  # `Field required`. Values are the compose bundle's
  # (deploy/compose/web-postgres.yaml:14-17). The timeout stays under the
  # default transport idle ceiling minus headroom (300 - 30, config.py:1119);
  # an overlay behind a lower-timeout hop (AKS) sets the ceiling and the
  # timeout together.
  ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS: "15"
  ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS: "10"
  ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS: "85"
  ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE: "10"
  # Set per rollout (overlay) to the candidate release; it becomes image_digest
  # in web_instances via DeploymentMembershipIdentity.image_identity
  # (deployment_profiles.py:153, membership_authority.py:149). The literal
  # below passes the identity regex, so the base renders and a forgotten
  # overlay is caught by the placeholder test and by K4's rollout assertion.
  ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE: REPLACE_PER_ROLLOUT
```

```yaml
# deploy/kubernetes/base/secret.example.yaml
# Documentation only. NOT listed in kustomization.yaml; an overlay or the
# operator creates the real `elspeth-web-secrets` (for example from a
# SecretProviderClass in deploy/kubernetes/overlays/aks/). These are the
# RUNTIME-role database URLs: the role bootstrap-roles.sql creates as
# elspeth_runtime, which holds no DDL. The web pods never see the
# schema-owner credentials (see secret-schema-owner.example.yaml).
apiVersion: v1
kind: Secret
metadata:
  name: elspeth-web-secrets
type: Opaque
stringData:
  ELSPETH_WEB__SESSION_DB_URL: REPLACE_ME_postgresql+psycopg://elspeth_runtime:PASSWORD@HOST:5432/elspeth_sessions?sslmode=verify-full
  ELSPETH_WEB__LANDSCAPE_URL: REPLACE_ME_postgresql+psycopg://elspeth_runtime:PASSWORD@HOST:5432/elspeth_landscape?sslmode=verify-full
  # At least 32 bytes (config.py:46). Generate with: openssl rand -hex 32
  ELSPETH_WEB__SECRET_KEY: REPLACE_ME_64_hex
  # Base64 of at least 32 random bytes; the value is base64-decoded at load
  # (config.py:594, :805). Generate with: openssl rand -base64 32
  ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY: REPLACE_ME_base64_of_32_random_bytes
```

```yaml
# deploy/kubernetes/base/secret-schema-owner.example.yaml
# Documentation only. NOT listed in kustomization.yaml. The SCHEMA-OWNER role
# (bootstrap-roles.sql: elspeth_schema_owner, owner of both databases) is
# referenced ONLY by job-schema-init.yaml; no Deployment mounts this Secret.
apiVersion: v1
kind: Secret
metadata:
  name: elspeth-schema-owner-secrets
type: Opaque
stringData:
  ELSPETH_WEB__SESSION_DB_URL: REPLACE_ME_postgresql+psycopg://elspeth_schema_owner:PASSWORD@HOST:5432/elspeth_sessions?sslmode=verify-full
  ELSPETH_WEB__LANDSCAPE_URL: REPLACE_ME_postgresql+psycopg://elspeth_schema_owner:PASSWORD@HOST:5432/elspeth_landscape?sslmode=verify-full
```

```sql
-- deploy/kubernetes/base/bootstrap-roles.sql
-- Cold install only. Connect as the PostgreSQL administrator with
-- PGHOST/PGPORT/PGUSER/PGPASSWORD/PGSSLMODE/PGSSLROOTCERT from the operator.
-- Passwords are read from the environment, never passed on argv or echoed.
-- Mirrors deploy/azure-container-apps/scripts/bootstrap-roles.sql: one
-- schema-owner role (DDL; used only by the elspeth-schema-init Job) and one
-- DDL-less runtime role (used by the web pods).
\getenv schema_owner_password ELSPETH_SCHEMA_OWNER_PASSWORD
\getenv runtime_password ELSPETH_RUNTIME_PASSWORD
CREATE ROLE elspeth_schema_owner LOGIN PASSWORD :'schema_owner_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE elspeth_runtime LOGIN PASSWORD :'runtime_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT elspeth_schema_owner TO CURRENT_USER WITH ADMIN OPTION;
ALTER DATABASE elspeth_sessions OWNER TO elspeth_schema_owner;
ALTER DATABASE elspeth_landscape OWNER TO elspeth_schema_owner;
REVOKE ALL ON DATABASE elspeth_sessions FROM PUBLIC;
REVOKE ALL ON DATABASE elspeth_landscape FROM PUBLIC;
GRANT CONNECT ON DATABASE elspeth_sessions, elspeth_landscape TO elspeth_runtime;
\connect elspeth_sessions
SET ROLE elspeth_schema_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO elspeth_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO elspeth_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO elspeth_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO elspeth_runtime;
\connect elspeth_landscape
SET ROLE elspeth_schema_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO elspeth_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO elspeth_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO elspeth_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO elspeth_runtime;
```

```sql
-- deploy/kubernetes/base/bootstrap-acceptance-roles.sql
-- Fresh disposable acceptance only (deploy/kubernetes/overlays/kind-acceptance);
-- fails on pre-existing roles. Mirrors
-- deploy/azure-container-apps/scripts/bootstrap-acceptance-roles.sql: the
-- production roles first, then one runtime role per acceptance replica so a
-- role revocation partitions exactly one of them.
\ir bootstrap-roles.sql
\connect postgres
\getenv runtime_a_password ELSPETH_RUNTIME_A_PASSWORD
\getenv runtime_b_password ELSPETH_RUNTIME_B_PASSWORD
CREATE ROLE elspeth_runtime_a LOGIN PASSWORD :'runtime_a_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE elspeth_runtime_b LOGIN PASSWORD :'runtime_b_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT CONNECT ON DATABASE elspeth_sessions, elspeth_landscape TO elspeth_runtime_a, elspeth_runtime_b;
\connect elspeth_sessions
SET ROLE elspeth_schema_owner;
GRANT USAGE ON SCHEMA public TO elspeth_runtime_a, elspeth_runtime_b;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO elspeth_runtime_a, elspeth_runtime_b;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO elspeth_runtime_a, elspeth_runtime_b;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO elspeth_runtime_a, elspeth_runtime_b;
\connect elspeth_landscape
SET ROLE elspeth_schema_owner;
GRANT USAGE ON SCHEMA public TO elspeth_runtime_a, elspeth_runtime_b;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO elspeth_runtime_a, elspeth_runtime_b;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO elspeth_runtime_a, elspeth_runtime_b;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO elspeth_runtime_a, elspeth_runtime_b;
```

```yaml
# deploy/kubernetes/base/pvc.yaml
# One RWX share for data/, data/blobs and payloads/ on every replica and every
# Job (Phase 6b §4). No storageClassName: the overlay names the provider's RWX
# class (Azure Files NFS 4.1 on AKS; a hostPath PV in the kind harness). An
# RWO default class cannot bind a ReadWriteMany claim, so a missing overlay
# fails at scheduling instead of silently landing on a single-node disk.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: elspeth-state
spec:
  accessModes: [ReadWriteMany]
  resources:
    requests:
      storage: 100Gi
```

```yaml
# deploy/kubernetes/base/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: elspeth-web
spec:
  type: ClusterIP
  # Affinity stays ON until Task K7 qualifies routing without it: the
  # WebSocket ticket store and the composer progress registry are
  # process-local (Phase 6b §3.5).
  sessionAffinity: ClientIP
  selector:
    app.kubernetes.io/name: elspeth-web
    app.kubernetes.io/component: web
  ports:
    - name: http
      port: 8451
      targetPort: http
```

```yaml
# deploy/kubernetes/base/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: elspeth-web
spec:
  replicas: 2
  # RollingUpdate 1/0, not Recreate: the overlap is between replicas whose
  # compatibility key is equal, which the fences serialise; an unequal-epoch
  # candidate is refused by validate_only_schema_or_raise before it is ready
  # (deployment_profiles.py:176 -> external_state_startup.py:156).
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels:
      app.kubernetes.io/name: elspeth-web
      app.kubernetes.io/component: web
  template:
    metadata:
      labels:
        app.kubernetes.io/name: elspeth-web
        app.kubernetes.io/component: web
      annotations:
        # Stamped per rollout (overlay) with the candidate short SHA; the
        # downward API projects it into ELSPETH_K8S_REVISION, which the
        # kubernetes profile (Task K2) binds to deployment_generation.
        elspeth.io/revision: REPLACE_PER_ROLLOUT
    spec:
      # SIGTERM drain: the lifespan marks the instance draining, releases its
      # fences and records stopped (Phase 6b §3.5); ACA gives it 60 s.
      terminationGracePeriodSeconds: 90
      securityContext:
        runAsUser: 1654
        runAsGroup: 1654
        fsGroup: 1654
        runAsNonRoot: true
      containers:
        - name: web
          # Digest-pinned release image; the overlay replaces the digest.
          image: ghcr.io/dta-au/elspeth@sha256:0000000000000000000000000000000000000000000000000000000000000000
          args: ["web", "--host", "0.0.0.0", "--port", "8451"]
          ports:
            - name: http
              containerPort: 8451
          envFrom:
            - configMapRef:
                name: elspeth-web-config
            - secretRef:
                name: elspeth-web-secrets
          env:
            - name: WEB_CONCURRENCY
              value: "1"
            - name: ELSPETH_K8S_POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: ELSPETH_K8S_REVISION
              valueFrom:
                fieldRef:
                  fieldPath: metadata.annotations['elspeth.io/revision']
          resources:
            requests:
              cpu: "1"
              memory: 2Gi
            limits:
              memory: 2Gi
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          startupProbe:
            httpGet: { path: /api/health, port: http }
            periodSeconds: 5
            failureThreshold: 36
          livenessProbe:
            httpGet: { path: /api/health, port: http }
            periodSeconds: 30
            failureThreshold: 3
          readinessProbe:
            httpGet: { path: /api/ready, port: http }
            periodSeconds: 10
            failureThreshold: 3
          volumeMounts:
            - name: state
              mountPath: /mnt/elspeth
      volumes:
        - name: state
          persistentVolumeClaim:
            claimName: elspeth-state
```

The sizing mirrors ACA's production parameters (`workload.bicep:105-108` `webCpu '1.0'` / `webMemory '2Gi'`; `main.bicep:42` share quota 100 GiB; replicas `workload.bicep:70` `param minReplicas int = 2`); the startup probe budget (36 × 5 s = 180 s) covers the 150 s ECS `startPeriod` the Phase 6b §3.5 probes were sized against; `terminationGracePeriodSeconds: 90` exceeds ACA's 60 s (`workload.bicep:78`).

```yaml
# deploy/kubernetes/base/job-provision-storage.yaml
# Runs once per share, as root, from a digest-pinned busybox (busybox:1.37.0,
# the index digest the Kubernetes platform facts §1.3 record; no overlay
# rewrites it): fsGroup is not
# applied to every volume type (K0 measured hostPath; NFS shares arrive
# root-owned), so the application pod (UID 1654, no capabilities) cannot
# create its own subtree. Mirrors ACA's provision-storage Job
# (deploy/azure-container-apps/workload.bicep:429-462). Re-running is
# idempotent. ttlSecondsAfterFinished lets `kubectl apply -k` on the next
# rollout recreate the Job (Job templates are immutable).
apiVersion: batch/v1
kind: Job
metadata:
  name: elspeth-provision-storage
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    metadata:
      labels:
        app.kubernetes.io/part-of: elspeth
        app.kubernetes.io/component: provision-storage
    spec:
      restartPolicy: Never
      securityContext:
        runAsUser: 0
        runAsGroup: 0
      containers:
        - name: provision-storage
          image: docker.io/library/busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0
          command: ["sh", "-c", "set -eu; mkdir -p /mnt/elspeth/data/blobs /mnt/elspeth/payloads; chown -R 1654:1654 /mnt/elspeth/data /mnt/elspeth/payloads; chmod 0700 /mnt/elspeth/data /mnt/elspeth/data/blobs /mnt/elspeth/payloads; ls -ln /mnt/elspeth"]
          volumeMounts:
            - name: state
              mountPath: /mnt/elspeth
      volumes:
        - name: state
          persistentVolumeClaim:
            claimName: elspeth-state
```

```yaml
# deploy/kubernetes/base/job-schema-init.yaml
# One-shot `elspeth doctor deployment --init-schema` as the SCHEMA-OWNER role.
# The two database URLs come from elspeth-schema-owner-secrets through `env`,
# which takes precedence over every `envFrom` source, so the runtime URLs in
# elspeth-web-secrets (mounted for ELSPETH_WEB__SECRET_KEY and the signing
# key, which WebSettings requires to load) are overridden. No Deployment
# references elspeth-schema-owner-secrets.
apiVersion: batch/v1
kind: Job
metadata:
  name: elspeth-schema-init
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    metadata:
      labels:
        app.kubernetes.io/part-of: elspeth
        app.kubernetes.io/component: schema-init
    spec:
      restartPolicy: Never
      securityContext:
        runAsUser: 1654
        runAsGroup: 1654
        fsGroup: 1654
        runAsNonRoot: true
      containers:
        - name: schema-init
          image: ghcr.io/dta-au/elspeth@sha256:0000000000000000000000000000000000000000000000000000000000000000
          args: ["doctor", "deployment", "--init-schema"]
          envFrom:
            - configMapRef:
                name: elspeth-web-config
            - secretRef:
                name: elspeth-web-secrets
          env:
            - name: ELSPETH_WEB__SESSION_DB_URL
              valueFrom:
                secretKeyRef:
                  name: elspeth-schema-owner-secrets
                  key: ELSPETH_WEB__SESSION_DB_URL
            - name: ELSPETH_WEB__LANDSCAPE_URL
              valueFrom:
                secretKeyRef:
                  name: elspeth-schema-owner-secrets
                  key: ELSPETH_WEB__LANDSCAPE_URL
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: state
              mountPath: /mnt/elspeth
      volumes:
        - name: state
          persistentVolumeClaim:
            claimName: elspeth-state
```

Why `RollingUpdate 1/0` and not `Recreate`: a rolling update overlaps two
revisions carrying two `deployment_generation` values, and that is safe for
the reason ACA's Single-mode rollout is safe (Phase 6b §3.5): the overlap is
between replicas whose compatibility key (session epoch, Landscape epoch,
coordination protocol) is equal, and the session-operation and run fences
serialise equal-key writers regardless of generation. A candidate whose
compatibility key differs is refused by
`DeploymentStartupProfile.validate_only_schema_or_raise`
(`deployment_profiles.py:176`, dispatching to `external_state_startup.py:156`)
before it becomes ready, so it never takes traffic and the old revision keeps
serving. `Recreate` would trade that for downtime without adding safety. The
July design's §Kubernetes Bundle still says `Recreate` / `replicas: 1` on HEAD
(`docs/specs/2026-07-26-finish-deferred-deployment-platforms-design.md:931`);
Task K8 rewrites it — K1 does not touch the spec.

Why the release image digest is all zeros: the same convention as the ACA
parameter files (`workload.production.bicepparam:16`); the reference has the
exact shape the overlay's `images:` transformer replaces, the render test can
assert the digest regex, and a base applied without an overlay fails at image
pull instead of running an unpinned tag. The busybox digest is NOT zeros: it
is a public, release-independent image that no overlay rewrites (K4's
`images:` names only `ghcr.io/dta-au/elspeth`), so the base carries the real
K0 §1.3 pin and the provision-storage Job pulls it as-is in kind and on AKS.

- [ ] **Step 4: Install the pinned kubectl in the CI `test` job.**

Insert between `.github/workflows/ci.yaml:752` (`bicep --version`, the last line of the Bicep step) and `:754` (`- name: Install dependencies`), keeping one blank line on each side:

```yaml
      - name: Install kubectl (Kubernetes bundle contract tests)
        working-directory: ${{ env.CI_CHECKOUT_PATH }}
        # tests/unit/deployment/test_kubernetes_bundle.py renders
        # deploy/kubernetes/base with `kubectl kustomize` and asserts on the
        # rendered documents. Like the Terraform and Bicep steps above, the
        # binary is pinned by version and SHA-256 (the pin the Kubernetes
        # platform facts record) and the tests fail loudly under
        # GITHUB_ACTIONS when it is absent rather than skipping silently.
        run: |
          KUBECTL_VERSION=1.37.0
          KUBECTL_SHA256=6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f
          curl -fsSLo /tmp/kubectl \
            "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
          printf '%s  /tmp/kubectl\n' "$KUBECTL_SHA256" | sha256sum -c -
          install -m 0755 /tmp/kubectl /usr/local/bin/kubectl
          kubectl version --client
```

No `sudo`: the `test` job runs inside `container: python:*-bookworm` (`ci.yaml:664-665`) as root, exactly like the Bicep step at `:751`. The ACA pin test (`test_azure_container_apps_bundle.py:509-527`) counts steps whose run text contains `bicep-linux-x64`, so this step does not disturb its `pins == 2`. This step is K1's, not K3's: `_require_kubectl` fails under `GITHUB_ACTIONS`, so without it the `test` job is red from the moment K1 lands.

- [ ] **Step 5: Run the bundle test and watch it pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/deployment/test_kubernetes_bundle.py -n 0 > /tmp/lane-k-k1-green.log 2>&1; echo exit=$?; tail -3 /tmp/lane-k-k1-green.log`
Expected: `exit=0`, `17 passed` (measured 2026-09-13 with kubectl v1.37.0 / kustomize v5.8.1 against these files as they stood before the composer keys and the real busybox digest were added; the two controls below re-establish it). Each assertion was also proven to discriminate by mutating the authority it reads (dropping `fsGroup`, mounting `elspeth-schema-owner-secrets` on the Deployment, `commonLabels` instead of `labels`, `includeTemplates: true`, removing a `ttlSecondsAfterFinished`, a hex signing-key placeholder, a filled-in `REPLACE_PER_ROLLOUT`, a different schema-init digest, an `app.kubernetes.io/name` label on a Job template, a literal password in the SQL, a drifted `KUBECTL_SHA256`, `Recreate`, listing `secret.example.yaml` as a resource): every one turned exactly its test red and the clean tree stayed at 17 passed. That measurement predates two assertions added since: the four `REQUIRED_COMPOSER_SETTINGS` in `test_configmap_binds_the_runtime_contract` and the facts-bound busybox digest in `test_share_is_provisioned_by_a_root_job_not_by_the_application_pod`. Control both now. First delete the `ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE` line from `configmap.yaml` and re-run: only `test_configmap_binds_the_runtime_contract` fails, `16 passed, 1 failed`. Restore the line. Then set the busybox digest in `job-provision-storage.yaml` back to 64 zeros: only `test_share_is_provisioned_by_a_root_job_not_by_the_application_pod` fails, `16 passed, 1 failed`. Restore it and confirm `17 passed`. The four composer keys are the whole required set. On HEAD, `settings_from_env()` over the base ConfigMap and runtime Secret keys (with high-entropy secrets) lists exactly `composer_max_composition_turns`, `composer_max_discovery_turns`, `composer_timeout_seconds` and `composer_rate_limit_per_minute` as `Field required`. With the four values above it builds (measured 2026-09-14) and logs only the non-fatal `composer_turn_budget_underfunded` warning (`config.py:1144-1153`). The inventory assertion uses git as its oracle (`_shipped_files`: `git ls-files --cached --others --exclude-standard`), so an operator's gitignored `deploy/kubernetes/base/secret.local.yaml` (`.gitignore:314`, `test_deploy_ignore_policy.py:43`) leaves it green while a stray untracked `stray.yaml` turns it red — both measured — and it is green before the `git add` of Step 7 because untracked-not-ignored files are listed. The module is `ruff format`-clean under the repo's `pyproject.toml`, so the pre-commit hook rewrites nothing. Server admission of the rendered objects is proven by K4's `kubectl apply -k` in kind; no clusterless dry-run exists (`kubectl apply --dry-run=client` still performs REST-mapper discovery against an API server and exits 1 with `connection refused` on a clusterless box).

- [ ] **Step 6: Delete the obsolete absent-bundle assertion.**

`tests/unit/docs/test_deployment_platform_docs.py:69-74` reads today:

```python
    # deploy/azure-container-apps/ ships from Phase 6b (its own contract test is
    # tests/unit/deployment/test_azure_container_apps_bundle.py). Desktop
    # acceptance does not manufacture a sanitized live receipt.
    for absent_bundle in ("kubernetes", "platforms"):
        bundle_path = REPO_ROOT / "deploy" / absent_bundle
        assert not bundle_path.exists() or not any(bundle_path.rglob("*"))
```

Replace those six lines with:

```python
    # deploy/azure-container-apps/ ships from Phase 6b and deploy/kubernetes/
    # from the multi-replica Kubernetes work (their own contract tests are
    # tests/unit/deployment/test_azure_container_apps_bundle.py and
    # tests/unit/deployment/test_kubernetes_bundle.py). Desktop acceptance
    # does not manufacture a sanitized live receipt.
    for absent_bundle in ("platforms",):
        bundle_path = REPO_ROOT / "deploy" / absent_bundle
        assert not bundle_path.exists() or not any(bundle_path.rglob("*"))
```

Nothing else in that file changes: `test_kubernetes_is_an_explicit_byo_zero_overlap_contract` (`:139-149`) asserts the docs still say `strategy: Recreate` / `one replica`, which stays true until Task K8 flips `docs/reference/deployment-platforms.md`; `test_support_matrix_links_only_shipped_deployment_artifacts` (`:44`) keeps the `Kubernetes (BYO manifests)` row label for the same reason.

- [ ] **Step 7: Stage the new files, then run every suite that reads the touched files.**

Stage first. `tests/unit/deployment/test_web_settings_exports_resolve.py` scans `git ls-files -- deploy docs/runbooks` (`:39`, `:62`). An untracked `configmap.yaml` is not in its input, so the gate would pass without ever reading the new `ELSPETH_WEB__*` names. Also, Step 8's `git commit -m "<msg>" -- <paths>` aborts with `pathspec did not match` on an untracked path. Stage by explicit file, never the directory:

```bash
cd "$(git rev-parse --show-toplevel)" && git add -- deploy/kubernetes/base/kustomization.yaml deploy/kubernetes/base/configmap.yaml deploy/kubernetes/base/secret.example.yaml deploy/kubernetes/base/secret-schema-owner.example.yaml deploy/kubernetes/base/bootstrap-roles.sql deploy/kubernetes/base/bootstrap-acceptance-roles.sql deploy/kubernetes/base/pvc.yaml deploy/kubernetes/base/service.yaml deploy/kubernetes/base/deployment.yaml deploy/kubernetes/base/job-provision-storage.yaml deploy/kubernetes/base/job-schema-init.yaml tests/unit/deployment/test_kubernetes_bundle.py
git status --short -- deploy/kubernetes tests/unit/deployment/test_kubernetes_bundle.py
```

Expected: twelve `A ` lines, no `??` line under `deploy/kubernetes/`.

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/deployment tests/unit/docs/test_deployment_platform_docs.py tests/unit/cicd tests/unit/test_ci_workflow_xdist.py -n 0 > /tmp/lane-k-k1.log 2>&1; echo exit=$?; tail -5 /tmp/lane-k-k1.log`
Expected: `exit=0`. `tests/unit/deployment/test_deploy_ignore_policy.py` already parametrises `deploy/kubernetes/base/deployment.yaml` as not-ignored (`:30`) and `deploy/kubernetes/base/secret.local.yaml` as ignored (`:43`), so the new directory needs no `.gitignore` edit; `tests/unit/cicd/test_state_engine_ci_selection.py:284-298` pins the `test` job's checkout depth and apt tokens only, and `tests/unit/test_ci_workflow_xdist.py` looks steps up by name, so the inserted step cascades nowhere. `test_every_exported_web_setting_resolves_to_a_live_field` now reads the staged ConfigMap. Every `ELSPETH_WEB__*` name on a non-comment line there is a live `WebSettings` field: `DEPLOYMENT_TARGET`, `DEPLOYMENT_STATE_MODE`, `HOST`, `PORT`, `DATA_DIR`, `PAYLOAD_STORE_PATH`, `LOG_JSON`, `OPERATOR_TELEMETRY`, the four `COMPOSER_*`, `OPERATOR_TELEMETRY_RELEASE`, plus the Secret examples' four keys. Without kubectl on PATH the bundle module reports `4 passed, 13 skipped` and the exit is still 0 — run Step 7 with the lane PATH from Step 2 so the 17 are executed, and read the `passed` count, not only the exit code.

- [ ] **Step 8: Commit.**

Stage first, then run the safety check, then commit: `scripts/branch-safety-check.sh` inspects the STAGED set, so every path of the commit — the twelve created files and the two modified ones — must be in the index before it runs. The twelve created files get `git add -N` first (Step 7 already staged them, so this changes nothing unless a Step 7 path was missed) because a commit pathspec only selects paths the index knows; then all fourteen are staged by name, file paths only, never a directory.

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N deploy/kubernetes/base/kustomization.yaml deploy/kubernetes/base/configmap.yaml deploy/kubernetes/base/secret.example.yaml deploy/kubernetes/base/secret-schema-owner.example.yaml deploy/kubernetes/base/bootstrap-roles.sql deploy/kubernetes/base/bootstrap-acceptance-roles.sql deploy/kubernetes/base/pvc.yaml deploy/kubernetes/base/service.yaml deploy/kubernetes/base/deployment.yaml deploy/kubernetes/base/job-provision-storage.yaml deploy/kubernetes/base/job-schema-init.yaml tests/unit/deployment/test_kubernetes_bundle.py
git add -- deploy/kubernetes/base/kustomization.yaml deploy/kubernetes/base/configmap.yaml deploy/kubernetes/base/secret.example.yaml deploy/kubernetes/base/secret-schema-owner.example.yaml deploy/kubernetes/base/bootstrap-roles.sql deploy/kubernetes/base/bootstrap-acceptance-roles.sql deploy/kubernetes/base/pvc.yaml deploy/kubernetes/base/service.yaml deploy/kubernetes/base/deployment.yaml deploy/kubernetes/base/job-provision-storage.yaml deploy/kubernetes/base/job-schema-init.yaml tests/unit/deployment/test_kubernetes_bundle.py tests/unit/docs/test_deployment_platform_docs.py .github/workflows/ci.yaml
git status --short
scripts/branch-safety-check.sh --intent commit
git commit -m "feat(deploy): provider-neutral Kubernetes base at the multi-replica bar" -- deploy/kubernetes/base/kustomization.yaml deploy/kubernetes/base/configmap.yaml deploy/kubernetes/base/secret.example.yaml deploy/kubernetes/base/secret-schema-owner.example.yaml deploy/kubernetes/base/bootstrap-roles.sql deploy/kubernetes/base/bootstrap-acceptance-roles.sql deploy/kubernetes/base/pvc.yaml deploy/kubernetes/base/service.yaml deploy/kubernetes/base/deployment.yaml deploy/kubernetes/base/job-provision-storage.yaml deploy/kubernetes/base/job-schema-init.yaml tests/unit/deployment/test_kubernetes_bundle.py tests/unit/docs/test_deployment_platform_docs.py .github/workflows/ci.yaml
git show --stat HEAD
```

Expected: `git status --short` shows the twelve created paths as `A ` and the two modified paths as `M `; the safety check prints no `[FAIL]` line and exits 0 (on a FAIL, stop before the commit); `git show --stat HEAD` ends with `14 files changed`, and `git status --short` afterwards shows no K1 path. Any other count means a sibling lane staged into the shared index: `git reset --mixed HEAD~1`, restage only the 14 paths above, and commit again. The `.claude/lanes/k8s/` kubectl is gitignored and must not appear in the stat.
