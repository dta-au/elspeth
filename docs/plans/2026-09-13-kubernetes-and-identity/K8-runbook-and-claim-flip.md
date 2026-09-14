### Task K8: Runbook and public-claim flip

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: K7, I11. Runs before: nothing (a workstream end). Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered after I11 (and so last overall): Step 6 appends a Kubernetes row to the `### Cutover by deployment shape` table that I11 creates in `docs/runbooks/staging-session-db-recreation.md` (`grep -n Kubernetes docs/runbooks/staging-session-db-recreation.md` prints nothing on HEAD; the table does not exist until I11 lands). Line cites below were measured on 072141b75 and are unchanged through 818d04577 (`git diff --stat 072141b75 818d04577` touches only composer files).

**Files:**
- Create: `docs/runbooks/kubernetes-deployment.md`
- Create: `tests/unit/web/test_kubernetes_runbook_contract.py` (house location: `test_aws_ecs_runbook_contract.py` and `test_azure_container_apps_runbook_contract.py` both live in `tests/unit/web/`)
- Modify: `docs/reference/deployment-platforms.md:10-11` (intro sentence "Azure VM production and Kubernetes BYO deployments"), `:15-17` ("ELSPETH web currently supports one web process or replica"), `:27` (the `| Kubernetes (BYO manifests) |` support-matrix row), `:30-32` (tracked-artifact inventory sentence), `:38-40` (the "Except for ACA's Single/sticky configuration" bullet), `:180-193` (the whole `## Kubernetes` section)
- Modify: `docs/guides/docker.md:137` ("Azure production and BYO Kubernetes deployments"), `:370-375` (the `### Kubernetes` section: heading at :370, body :372-375), `:550` (See-also line "Maintained and BYO support boundaries")
- Modify: `docs/runbooks/index.md:18` (the Deployment Platforms row ending "Kubernetes is BYO"); insert one row after `:19`
- Modify: `docs/README.md:20` (Start-here table: insert a "Deploying on Kubernetes" row after the AWS row)
- Modify: `docs/repository-structure.md:26` (Deployment row of the top-level table), `:56-61` (the `deploy/` bullet ending "Kubernetes remains BYO and has no shipped directory in this release"), `:110-116` (Deployment spread paragraph)
- Modify: `README.md:1073-1076` (the sentence "for the maintained Compose, AWS ECS, and native Linux paths plus the explicit Azure VM and Kubernetes boundaries"), `:1168` (doc-index row description "deferred platform boundaries"); insert one row after `:1172`
- Modify: `ARCHITECTURE.md:731-732` (the "maintained deployment set" sentence; only the Kubernetes clause)
- Modify: `docs/specs/2026-07-26-finish-deferred-deployment-platforms-design.md:928-952` (`## Kubernetes Bundle`, through "Harness PostgreSQL never appears in the shipped base.") and `:1564-1565` (the multiple-steady-state-replicas Non-Goals bullet) — this task, not K1, rewrites both spec passages (K1 leaves the spec untouched)
- Modify: `tests/unit/docs/test_deployment_platform_docs.py:18` (add four path constants after `CHANGELOG = REPO_ROOT / "CHANGELOG.md"`), and replace three whole functions, named here without line ranges because K1's edit of the `absent_bundle` loop (6 lines → 8) shifts everything below it: `test_support_matrix_links_only_shipped_deployment_artifacts` (K1 already removed `"kubernetes"` from its absent-bundle tuple), `test_kubernetes_is_an_explicit_byo_zero_overlap_contract`, `test_navigation_and_repository_structure_are_honest`
- Modify: `src/elspeth/web/deployment_profiles.py:29-35` (module docstring "**Identity.**" paragraph: add the Kubernetes downward-API sentence)
- Modify: `CHANGELOG.md` under `## 0.8.1 - 2026-09-10` — insert one bullet after `:62` (`  for the Single/sticky configuration and operating limitations.`, the last line of the "Azure Container Apps deployment" bullet), keeping the blank `:63` before the `---` at `:64`. Confirm the section with the operator before the first commit: the heading is dated, no 0.8.1 tag exists, and `tests/unit/website/test_release_site_contract.py:55-61` pins `## {CURRENT_VERSION} - `.
- Modify: `docs/runbooks/staging-session-db-recreation.md` — append one `| Kubernetes |` row to the table under I11's `### Cutover by deployment shape`, directly after its `| VM, external PostgreSQL |` row (I11 creates the table; no line number exists on HEAD). This file is shared with I11, like `CHANGELOG.md`.

Not touched: `CHANGELOG.md:202` (0.8.0 history, stays verbatim), K1's `absent_bundle` loop edit (Step 1's replacement of `test_support_matrix_links_only_shipped_deployment_artifacts` reproduces it verbatim), `ARCHITECTURE.md:732-735`'s Azure Container Apps sentence (stale but not a Kubernetes claim; see open questions).

**Interfaces:**
- Consumes: from K1 the base resource names `elspeth-web-config` (ConfigMap), `elspeth-state` (PVC), `elspeth-web` (Service and Deployment), `elspeth-provision-storage` and `elspeth-schema-init` (Jobs, both `ttlSecondsAfterFinished: 600`), the Secrets `elspeth-web-secrets` (keys `ELSPETH_WEB__SESSION_DB_URL`, `ELSPETH_WEB__LANDSCAPE_URL`, `ELSPETH_WEB__SECRET_KEY`, `ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY`) and `elspeth-schema-owner-secrets` (the two URL keys, referenced only by `job-schema-init`), the placeholders `elspeth.io/revision: REPLACE_PER_ROLLOUT` and `ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE: REPLACE_PER_ROLLOUT`, `deploy/kubernetes/base/bootstrap-roles.sql` (roles `elspeth_schema_owner`, `elspeth_runtime` only; `\getenv schema_owner_password ELSPETH_SCHEMA_OWNER_PASSWORD` and `\getenv runtime_password ELSPETH_RUNTIME_PASSWORD`, K1.md Step 3) and `deploy/kubernetes/base/bootstrap-acceptance-roles.sql` (`\ir bootstrap-roles.sql`, then roles `elspeth_runtime_a`, `elspeth_runtime_b`; run only by the kind harness, never by a production operator), the pod label `app.kubernetes.io/name=elspeth-web`; from K2 `deployment_startup_profile("kubernetes")` with `replica_identity_env_var="ELSPETH_K8S_POD_NAME"`, `revision_env_var="ELSPETH_K8S_REVISION"` so `web_instances.deployment_generation == revision_label == ELSPETH_K8S_REVISION` and `image_digest == ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE` (`membership_authority.py:139-155`); from K3/K4 the CI job ids `kubernetes-render` and `kubernetes-kind`, `scripts/cicd/kubernetes-kind-smoke.sh`, the `kind` marker and `tests/testcontainer/deployment/test_kubernetes_kind.py`; from K5 `tests/testcontainer/deployment/test_kubernetes_replica_probes.py` and the probe ids `session_operation_fence`, `session_operation_fence_execute`, `role_revocation_lease_expiry`, `postgresql_and_nfs` (`owner_affine` = `cannot_pass`); from K6 `deploy/kubernetes/overlays/aks/` (ingress-nginx cookie `elspeth-replica`, `proxy-read-timeout` ≥ 3600); from K7 the shipped `sessionAffinity` value in `deploy/kubernetes/base/service.yaml`; from K0 `docs/plans/2026-09-13-kubernetes-platform-facts.md`; from I11 the heading `### Cutover by deployment shape` inside `## Current Cutover:` of `docs/runbooks/staging-session-db-recreation.md`, its four-column table (`| Shape | Stores | Recreate procedure | Re-admission entry |`) ending in the `| VM, external PostgreSQL |` row, the anchor `#operator-notice-for-the-workflow-epoch-window`, and I11's `test_current_cutover_routes_each_shipped_deployment_shape_to_its_recreate_procedure`, which requires every link target in that subsection to be a file under `docs/runbooks/` and no `epoch <digit>` text in it.
- Produces: `docs/runbooks/kubernetes-deployment.md` with the fixed headings `## 1. Select and prove the cluster` … `## 9. Verify`, `## Rollback`, `## Scale and drain`, `## Redeploy`, `## Acceptance evidence`, `## Troubleshooting`, `## Teardown`; `tests/unit/web/test_kubernetes_runbook_contract.py` (module constants `RUNBOOK`, `SECTION_HEADINGS`, `RENDER_GLOBS`, `OVERLAY_VARIABLES`, helpers `_section`, `_fences`, `_heredoc`, `_require_kubectl`); the support-matrix label `"Kubernetes"` and the status string `Implemented; kind acceptance for the two-replica configuration; no live AKS acceptance is claimed`; the CHANGELOG bullet title `Kubernetes is a maintained multi-replica target`. the `| Kubernetes |` row in I11's `### Cutover by deployment shape` table. No later task consumes these. Two files are shared with Workstream I: `CHANGELOG.md` and `docs/runbooks/staging-session-db-recreation.md` (both edited by I11 before K8; rebase, do not merge by hand).

- [ ] **Step 1: Flip the deployment-docs contract test first.** Edit `tests/unit/docs/test_deployment_platform_docs.py`. After line 18 (`CHANGELOG = REPO_ROOT / "CHANGELOG.md"`) add:

```python
DOCKER_GUIDE = REPO_ROOT / "docs" / "guides" / "docker.md"
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
DOCS_INDEX = REPO_ROOT / "docs" / "README.md"
SESSION_RESET_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "staging-session-db-recreation.md"
```

Replace the whole function `test_support_matrix_links_only_shipped_deployment_artifacts` (as K1 left it) with:

```python
def test_support_matrix_links_only_shipped_deployment_artifacts() -> None:
    text = _read(PLATFORM_DOC)

    for label in (
        "Docker Compose",
        "AWS ECS",
        "Azure Container Apps",
        "Azure Ubuntu VM",
        "Kubernetes",
        "Native Linux",
    ):
        assert f"| {label} |" in text

    for relative_path in (
        "../guides/docker.md",
        "../runbooks/aws-ecs-cold-install.md",
        "../runbooks/aws-ecs-deployment.md",
        "../runbooks/ansible-ubuntu-deployment.md",
        "../runbooks/kubernetes-deployment.md",
        "../../deploy/aws-ecs/terraform",
        "../../deploy/compose",
        "../../deploy/kubernetes/base",
        "../../deploy/kubernetes/overlays/aks",
        "../../deploy/linux-systemd/elspeth-web.service",
    ):
        assert relative_path in text
        assert (PLATFORM_DOC.parent / relative_path).resolve().exists()

    for shipped_path in (
        "deploy/compose/postgres.yaml",
        "deploy/compose/web-postgres.yaml",
        "deploy/kubernetes/base/kustomization.yaml",
        "deploy/kubernetes/base/deployment.yaml",
        "deploy/kubernetes/base/job-schema-init.yaml",
        "deploy/linux-systemd/elspeth-web.service",
        "deploy/linux-systemd/elspeth-web.env.example",
    ):
        assert (REPO_ROOT / shipped_path).is_file()

    # deploy/azure-container-apps/ ships from Phase 6b and deploy/kubernetes/
    # from the multi-replica plan; each has its own bundle contract test under
    # tests/unit/deployment/. Desktop and kind acceptance do not manufacture a
    # sanitized live receipt.
    for absent_bundle in ("platforms",):
        bundle_path = REPO_ROOT / "deploy" / absent_bundle
        assert not bundle_path.exists() or not any(bundle_path.rglob("*"))
```

Replace the whole function `test_kubernetes_is_an_explicit_byo_zero_overlap_contract` with two functions:

```python
def test_kubernetes_documents_the_multi_replica_contract() -> None:
    text = _read(PLATFORM_DOC)
    section = _section(text, "## Kubernetes")

    for phrase in (
        "deploy/kubernetes/base",
        "ReadWriteMany",
        "external PostgreSQL",
        "two replicas",
        "elspeth.io/revision",
        "kind acceptance",
        "kubernetes-deployment.md",
        "No live AKS acceptance is claimed",
        "`sqlite-single`",
        "ELSPETH_K8S_REVISION",
    ):
        assert phrase in section, phrase
    assert "| Kubernetes | External PostgreSQL" in text
    assert "Implemented; kind acceptance for the two-replica configuration" in text
    for stale in ("BYO", "strategy: Recreate", "no maintained bundle", "ships no manifests", "one replica and one process"):
        assert stale not in text, stale


def test_public_surfaces_no_longer_call_kubernetes_byo() -> None:
    readme = _read(README)
    docker_guide = _read(DOCKER_GUIDE)
    architecture = _read(ARCHITECTURE)
    docs_index = _read(DOCS_INDEX)
    changelog = _normalized(CHANGELOG)

    assert "docs/runbooks/kubernetes-deployment.md" in readme
    assert "Kubernetes boundaries" not in readme
    assert "deferred platform boundaries" not in readme
    assert "deploy/kubernetes/base" in docker_guide
    assert "../runbooks/kubernetes-deployment.md" in docker_guide
    assert "does not ship Kubernetes manifests" not in docker_guide
    assert "BYO" not in docker_guide
    assert "Kubernetes BYO" not in architecture
    assert "deploy/kubernetes/base" in architecture
    assert "runbooks/kubernetes-deployment.md" in docs_index
    assert "Kubernetes is a maintained multi-replica target" in changelog

    # I11's per-shape cutover table routes the Kubernetes shape to this runbook.
    session_reset = _read(SESSION_RESET_RUNBOOK)
    (row,) = [line for line in session_reset.splitlines() if line.startswith("| Kubernetes |")]
    assert session_reset.index("### Cutover by deployment shape") < session_reset.index(row)
    assert row.startswith("| Kubernetes | PostgreSQL, both stores |"), row
    for target in (
        "kubernetes-deployment.md#6-initialize-schemas",
        "kubernetes-deployment.md#7-prove-runtime-credentials",
        "kubernetes-deployment.md#8-deploy-the-workload",
        "#operator-notice-for-the-workflow-epoch-window",
    ):
        assert target in row, target
```

Replace the whole function `test_navigation_and_repository_structure_are_honest` with:

```python
def test_navigation_and_repository_structure_are_honest() -> None:
    readme = _read(README)
    runbook_index = _read(RUNBOOK_INDEX)
    structure = _read(REPOSITORY_STRUCTURE)

    assert "docs/reference/deployment-platforms.md" in readme
    assert "Deployment Platforms" in runbook_index
    assert "Azure Container Apps" in runbook_index
    assert "desktop acceptance" in runbook_index
    assert "Kubernetes" in runbook_index
    assert "kubernetes-deployment.md" in runbook_index
    assert "BYO" not in runbook_index

    assert "`deploy/compose/`" in structure
    assert "`deploy/aws-ecs/terraform/`" in structure
    assert "`deploy/kubernetes/base/`" in structure
    assert "`deploy/linux-systemd/`" in structure
    assert "Kubernetes remains BYO" not in structure
    for text in (_normalized(README), _normalized(REPOSITORY_STRUCTURE)):
        assert "desktop acceptance" in text
        assert "Single/sticky" in text
    assert "support claim waits for that receipt" not in readme
    assert "not a supported target in this release" not in _normalized(REPOSITORY_STRUCTURE)
```

- [ ] **Step 2: Write the failing runbook contract test.** Create `tests/unit/web/test_kubernetes_runbook_contract.py`:

````python
"""Executable contract for the Kubernetes deployment runbook.

Mirrors ``test_azure_container_apps_runbook_contract.py`` for the one
Kubernetes runbook. Three rules carry over from the ECS and ACA lessons: every
bash fence parses, no secret value ever reaches argv, and the cold install
orders storage, schema, runtime credentials and traffic. One rule is inverted:
the runbook carries NO epoch literal (it names ``SESSION_SCHEMA_EPOCH`` and
``SQLITE_SCHEMA_EPOCH`` by symbol and path), so a schema bump never fans out
to this file.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "kubernetes-deployment.md"
PLATFORM_FACTS = REPO_ROOT / "docs" / "plans" / "2026-09-13-kubernetes-platform-facts.md"
FACTS_LINK = "../plans/2026-09-13-kubernetes-platform-facts.md"
SECTION_HEADINGS = (
    "## 1. Select and prove the cluster",
    "## 2. Create the database roles",
    "## 3. Publish the image by digest",
    "## 4. Create the Secrets",
    "## 5. Provision storage",
    "## 6. Initialize schemas",
    "## 7. Prove runtime credentials",
    "## 8. Deploy the workload",
    "## 9. Verify",
)
# `kubectl kustomize <dir> -o <dir>` writes one file per resource named
# `<group>_<version>_<kind>_<name>.yaml`, lower-cased, with no namespace
# prefix even when the overlay sets `namespace:`; the core group omits the
# group segment (measured 2026-09-14, kubectl v1.37.0 / kustomize v5.8.1 over
# K1's base: `apps_v1_deployment_elspeth-web.yaml`,
# `batch_v1_job_elspeth-provision-storage.yaml`,
# `batch_v1_job_elspeth-schema-init.yaml`,
# `v1_configmap_elspeth-web-config.yaml`,
# `v1_persistentvolumeclaim_elspeth-state.yaml`, `v1_service_elspeth-web.yaml`).
# The runbook applies them by these globs so the group/version prefix is never
# hardcoded.
RENDER_GLOBS = (
    "*_configmap_elspeth-web-config.yaml",
    "*_persistentvolumeclaim_elspeth-state.yaml",
    "*_job_elspeth-provision-storage.yaml",
    "*_job_elspeth-schema-init.yaml",
    "*_service_elspeth-web.yaml",
    "*_deployment_elspeth-web.yaml",
)
# `${BASE_RELATIVE}` is substituted per test (it depends on tmp_path): kustomize
# refuses an absolute path under `resources:` with
# "new root '<path>' cannot be absolute" (measured 2026-09-14, also under
# `--load-restrictor LoadRestrictionsNone`), so the runbook writes a relative one.
BASE_DIR = REPO_ROOT / "deploy" / "kubernetes" / "base"
OVERLAY_VARIABLES = {
    "${NAMESPACE}": "elspeth",
    "${RWX_STORAGE_CLASS}": "nfs-rwx",
    "${CANDIDATE_SHA:0:7}": "1a2b3c4",
    "${ELSPETH_RELEASE}": "0.8.1+1a2b3c4",
    "${GHCR_DIGEST}": "sha256:" + "ab" * 32,
}
TEST_IMAGE = "ghcr.io/dta-au/elspeth@sha256:" + "ab" * 32


def _text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def _fences(text: str, language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)


def _heredoc(script: str, marker: str) -> str:
    """The body of the ONE heredoc in ``script``; asserts there is exactly one so
    the closing marker cannot belong to a later heredoc."""
    opener = f"<<{marker}\n"
    assert script.count(opener) == 1, script
    start = script.index(opener) + len(opener)
    end = script.index(f"\n{marker}\n", start)
    return script[start:end]


def _require_kubectl(reason: str) -> None:
    """Skip locally when kubectl is absent; fail loudly in CI.

    Duplicates the helper in ``tests/unit/deployment/test_kubernetes_bundle.py``
    on purpose (test modules do not import each other). The ``test`` job
    installs a checksum-pinned kubectl, so absence under GITHUB_ACTIONS or
    ELSPETH_CI_KUBECTL_REQUIRED is a broken gate, not an environment quirk.
    """
    if shutil.which("kubectl") is not None:
        return
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("ELSPETH_CI_KUBECTL_REQUIRED"):
        pytest.fail(f"kubectl binary is missing in CI: {reason}")
    pytest.skip(f"kubectl is not installed, so {reason}")


def test_every_bash_fence_is_syntactically_valid() -> None:
    fences = _fences(_text(), "bash")
    assert len(fences) >= 9
    for index, script in enumerate(fences, start=1):
        result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"bash fence {index}: {result.stderr}"


def test_cold_install_orders_storage_schema_runtime_before_traffic() -> None:
    text = _text()
    numbered = [line for line in text.splitlines() if re.match(r"^## \d\. ", line)]
    assert numbered == list(SECTION_HEADINGS)
    positions = [text.index(heading) for heading in SECTION_HEADINGS]
    assert positions == sorted(positions)

    normalized = " ".join(text.split())
    for phrase in (
        "job/elspeth-provision-storage",
        "job/elspeth-schema-init",
        "job/elspeth-doctor-runtime",
        "rollout status deployment/elspeth-web",
        "`STALE` is a stop",
        "sslmode=verify-full",
        "1654:1654",
        "ReadWriteMany",
        "SMB does not",
        "no SQLite mode at replicas > 1",
        "`sqlite-single`",
        "--from-env-file",
    ):
        assert phrase in normalized, phrase

    # The Deployment and Service files are applied only in step 8; the schema
    # Job only in step 6; the provisioner only in step 5. `str.index` finds the
    # FIRST mention, so a glob named earlier than its step fails here.
    for opens, applied, closes in (
        ("## 5. Provision storage", "*_job_elspeth-provision-storage.yaml", "## 6. Initialize schemas"),
        ("## 6. Initialize schemas", "*_job_elspeth-schema-init.yaml", "## 7. Prove runtime credentials"),
        ("## 8. Deploy the workload", "*_service_elspeth-web.yaml", "## 9. Verify"),
        ("## 8. Deploy the workload", "*_deployment_elspeth-web.yaml", "## 9. Verify"),
    ):
        assert text.index(opens) < text.index(applied) < text.index(closes), applied


def test_no_epoch_literal_is_carried_by_the_runbook() -> None:
    text = _text()
    assert re.findall(r"session epoch \d+", text, flags=re.IGNORECASE) == []
    assert re.findall(r"landscape epoch \d+", text, flags=re.IGNORECASE) == []
    assert "SESSION_SCHEMA_EPOCH" in text
    assert "SQLITE_SCHEMA_EPOCH" in text


def test_secret_values_never_reach_argv() -> None:
    scripts = _fences(_text(), "bash")
    creates = [line for script in scripts for line in script.splitlines() if "create secret" in line]
    assert len(creates) == 2, creates
    for line in creates:
        assert "--from-env-file=" in line, line
    for script in scripts:
        assert "--from-literal" not in script, script
        assert "PGPASSWORD=" not in script, script
        assert "postgresql+psycopg://" not in script, script


def test_runtime_credential_proof_uses_only_the_runtime_secret() -> None:
    section = _section(_text(), "## 7. Prove runtime credentials", "## 8. Deploy the workload")
    (script,) = _fences(section, "bash")
    manifest = _heredoc(script, "EOF").replace("${NAMESPACE}", "elspeth").replace("${CANDIDATE_IMAGE}", TEST_IMAGE)
    job = yaml.safe_load(manifest)

    assert job["kind"] == "Job"
    assert job["metadata"]["name"] == "elspeth-doctor-runtime"
    assert job["spec"]["ttlSecondsAfterFinished"] == 600
    assert job["spec"]["backoffLimit"] == 0
    pod = job["spec"]["template"]["spec"]
    assert pod["securityContext"] == {"runAsUser": 1654, "runAsGroup": 1654, "fsGroup": 1654, "runAsNonRoot": True}
    (container,) = pod["containers"]
    assert container["image"] == TEST_IMAGE
    assert container["args"] == ["doctor", "deployment", "--json"]
    assert container["securityContext"] == {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    assert {ref["secretRef"]["name"] for ref in container["envFrom"] if "secretRef" in ref} == {"elspeth-web-secrets"}
    assert {ref["configMapRef"]["name"] for ref in container["envFrom"] if "configMapRef" in ref} == {"elspeth-web-config"}
    assert [mount["mountPath"] for mount in container["volumeMounts"]] == ["/mnt/elspeth"]
    assert "elspeth-schema-owner-secrets" not in section
    assert "jq -e 'all(.[]; .ok)'" in script


def test_rollback_is_conditional_on_the_previous_image_understanding_the_schemas() -> None:
    rollback = _section(_text(), "## Rollback", "## Scale and drain")
    for phrase in ("PREVIOUS_IMAGE", "rollout undo", "repair forward", "elspeth-doctor-previous", "never pass `--init-schema`"):
        assert phrase in rollback, phrase
    # There is no acceptance receipt on this target (no receipt store; the kind-lane pytest result is the evidence).
    assert "compatibility record" not in rollback
    assert "receipt" not in rollback


def test_drain_is_described_from_the_lifespan_not_invented() -> None:
    section = _section(_text(), "## Scale and drain", "## Redeploy")
    for phrase in (
        "SIGTERM",
        "instance_draining",
        "`draining`",
        "`stopped`",
        "terminationGracePeriodSeconds: 90",
        "/api/ready",
        "web_instances",
    ):
        assert phrase in section, phrase


def test_status_block_cites_the_bundle_the_facts_and_the_kind_evidence() -> None:
    text = _text()
    assert PLATFORM_FACTS.is_file()
    assert FACTS_LINK in text
    assert "deploy/kubernetes/base" in text
    assert "**Status.**" in text
    assert "**LIVE" in text
    normalized = " ".join(text.replace("> ", "").split())
    for phrase in (
        "kind acceptance",
        "kubernetes-render",
        "kubernetes-kind",
        "scripts/cicd/kubernetes-kind-smoke.sh",
        "-m kind -n 0",
        "No live AKS acceptance is claimed",
        "session_operation_fence",
        "session_operation_fence_execute",
        "role_revocation_lease_expiry",
        "postgresql_and_nfs",
        "`owner_affine`",
        "`cannot_pass`",
    ):
        assert phrase in normalized, phrase
    for stale in ("BYO", "strategy: Recreate", "one replica and one process"):
        assert stale not in text, stale


def test_image_publication_is_a_digest_reference_never_a_local_build() -> None:
    text = _text()
    assert "docker buildx imagetools inspect" in text
    assert "@${GHCR_DIGEST}" in text
    assert "docker build " not in text


def test_operator_overlay_renders_and_splits_per_resource(tmp_path: Path) -> None:
    _require_kubectl("the operator overlay cannot be rendered")
    text = _text()
    section = _section(text, "## 5. Provision storage", "## 6. Initialize schemas")
    overlay = _heredoc(_fences(section, "bash")[0], "EOF")
    assert "  - ${BASE_RELATIVE}\n" in overlay, overlay

    overlay_dir = tmp_path / "overlay"
    overlay_dir.mkdir()
    overlay = overlay.replace("${BASE_RELATIVE}", os.path.relpath(BASE_DIR, overlay_dir))
    for token, value in OVERLAY_VARIABLES.items():
        overlay = overlay.replace(token, value)
    assert "$" not in overlay, overlay
    (overlay_dir / "kustomization.yaml").write_text(overlay, encoding="utf-8")
    render_dir = tmp_path / "render"
    render_dir.mkdir()
    subprocess.run(["kubectl", "kustomize", str(overlay_dir), "-o", str(render_dir)], check=True, capture_output=True, text=True)

    assert len(list(render_dir.iterdir())) == 6
    for pattern in RENDER_GLOBS:
        assert pattern in text, pattern
        (rendered,) = list(render_dir.glob(pattern))
        assert yaml.safe_load(rendered.read_text(encoding="utf-8"))["metadata"]["namespace"] == "elspeth"

    (deployment_file,) = list(render_dir.glob("*_deployment_elspeth-web.yaml"))
    deployment = yaml.safe_load(deployment_file.read_text(encoding="utf-8"))
    assert deployment["spec"]["template"]["metadata"]["annotations"]["elspeth.io/revision"] == "sha-1a2b3c4"
    (web,) = deployment["spec"]["template"]["spec"]["containers"]
    assert web["image"] == TEST_IMAGE
    (configmap_file,) = list(render_dir.glob("*_configmap_elspeth-web-config.yaml"))
    assert yaml.safe_load(configmap_file.read_text(encoding="utf-8"))["data"]["ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE"] == "0.8.1+1a2b3c4"
    (pvc_file,) = list(render_dir.glob("*_persistentvolumeclaim_elspeth-state.yaml"))
    assert yaml.safe_load(pvc_file.read_text(encoding="utf-8"))["spec"]["storageClassName"] == "nfs-rwx"
    rendered_text = "".join(path.read_text(encoding="utf-8") for path in render_dir.iterdir())
    assert "REPLACE_PER_ROLLOUT" not in rendered_text
    assert "sha256:" + "0" * 64 not in rendered_text  # K1's all-zero release-digest placeholder
````

- [ ] **Step 3: Run both test files and watch them fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/docs/test_deployment_platform_docs.py tests/unit/web/test_kubernetes_runbook_contract.py -n 0 > /tmp/k-lane-k8-red.log 2>&1; echo exit=$?; grep -E "^(FAILED|ERROR|PASSED|SKIPPED)|passed|failed" /tmp/k-lane-k8-red.log`
Expected: `exit=1`. In `test_deployment_platform_docs.py`: `test_support_matrix_links_only_shipped_deployment_artifacts` fails at `assert "| Kubernetes |" in text` (HEAD's row reads `| Kubernetes (BYO manifests) |`), `test_kubernetes_documents_the_multi_replica_contract` fails at `assert phrase in section` for `deploy/kubernetes/base`, `test_public_surfaces_no_longer_call_kubernetes_byo` fails at `assert "docs/runbooks/kubernetes-deployment.md" in readme`, `test_navigation_and_repository_structure_are_honest` fails at `assert "kubernetes-deployment.md" in runbook_index`. In `test_kubernetes_runbook_contract.py`: every test except the kubectl-gated one fails with `FileNotFoundError: [Errno 2] No such file or directory: '<repo>/docs/runbooks/kubernetes-deployment.md'`; `test_operator_overlay_renders_and_splits_per_resource` is SKIPPED locally (`kubectl is not installed`) and fails with the same `FileNotFoundError` in CI.

- [ ] **Step 4: Write the runbook.** Create `docs/runbooks/kubernetes-deployment.md` with exactly this content. One executor decision is marked `EXECUTOR:` in the prose below and must be resolved (not left in the file): the `sessionAffinity` sentence follows whatever `deploy/kubernetes/base/service.yaml` says after K7.

````markdown
# Runbook: Deploy ELSPETH on Kubernetes (two-replica Kustomize base)

Use this procedure to install ELSPETH on any conformant Kubernetes cluster with
the tracked, provider-neutral Kustomize base at
[`deploy/kubernetes/base/`](../../deploy/kubernetes/base). The base ships one
`Deployment` of two replicas behind a `RollingUpdate` (`maxSurge: 1`,
`maxUnavailable: 0`), one web process per replica, external PostgreSQL for both
stores under two roles, one `ReadWriteMany` claim for files, a root storage
provisioner Job, a schema-init Job, downward-API replica identity and a SIGTERM
drain. Tool versions and the storage measurements behind it are in the
[platform facts](../plans/2026-09-13-kubernetes-platform-facts.md); the
[AKS overlay](../../deploy/kubernetes/overlays/aks) adds Azure Files NFS
storage, Key Vault CSI secrets and an ingress-nginx cookie-affinity ingress.

> **Status.** The base has kind acceptance in CI: the `kubernetes-render` job
> renders it with a checksum-pinned `kubectl`, and the `kubernetes-kind` job
> (`scripts/cicd/kubernetes-kind-smoke.sh`, which runs
> `pytest tests/testcontainer/deployment/test_kubernetes_kind.py tests/testcontainer/deployment/test_kubernetes_replica_probes.py -m kind -n 0`)
> proves two-replica startup under one deployment generation, shared state
> across replicas, a provider-free run, and the replica probes
> `session_operation_fence`, `session_operation_fence_execute`,
> `role_revocation_lease_expiry` and `postgresql_and_nfs`; `owner_affine` stays
> `cannot_pass`. No live AKS acceptance is claimed: the AKS overlay renders and
> is otherwise unmeasured. This is an executable operator procedure; steps
> marked **LIVE** require measurements during execution and belong in
> operator-local notes, never in a tracked file.

## Choose the correct procedure

| Need | Procedure |
| --- | --- |
| Install ELSPETH on a cluster you already operate | This runbook |
| Replace the image or configuration on a cluster that already runs ELSPETH | [Redeploy](#redeploy) below |
| Deploy on Azure Container Apps instead of a cluster | [ACA cold install](azure-container-apps-cold-install.md) |
| Run exactly one Azure Ubuntu VM instead | [Native Linux/Azure VM runbook](ansible-ubuntu-deployment.md) |

## Fast path

1. prove the context, namespace and a `ReadWriteMany` StorageClass;
2. create the schema-owner and runtime roles on the external PostgreSQL;
3. resolve the candidate image digest and pin it;
4. create the runtime Secret and the schema-owner Secret from operator-local files;
5. write the operator overlay, render it to one file per resource, apply the
   ConfigMap, the claim and the `elspeth-provision-storage` Job;
6. apply the `elspeth-schema-init` Job (schema-owner role);
7. run a one-shot `elspeth-doctor-runtime` Job (runtime role);
8. apply the Service and the Deployment and prove the rollout; and
9. verify public behaviour and both replica identities.

Every step has a stop condition. Do not skip forward after a failed
StorageClass, role, image, doctor or readiness check.

## Result and limits

A successful install has:

- one `Deployment` `elspeth-web` with `replicas: 2`, `RollingUpdate`
  `maxSurge: 1` / `maxUnavailable: 0`, startup, liveness and readiness probes
  on `/api/health`, `/api/health` and `/api/ready`, and
  `terminationGracePeriodSeconds: 90`;
- both databases (`elspeth_sessions`, `elspeth_landscape`) on operator-provided
  external PostgreSQL (16 or 17), one schema-owner role and one runtime role,
  both schemas initialized at the release's `SESSION_SCHEMA_EPOCH`
  (`src/elspeth/web/sessions/models.py`) and `SQLITE_SCHEMA_EPOCH`
  (`src/elspeth/core/landscape/schema.py`);
- one `ReadWriteMany` claim `elspeth-state` mounted at `/mnt/elspeth` on every
  pod and Job with `data`, `data/blobs` and `payloads` owned `1654:1654`, mode
  `0700`. NFS 4.1 qualifies; SMB does not. **The share carries no database**
  and there is no SQLite mode at replicas > 1 (`sqlite-single` is refused for
  the `kubernetes` target at configuration time);
- two Secrets: `elspeth-web-secrets` (runtime role URLs, `SECRET_KEY`,
  `SHAREABLE_LINK_SIGNING_KEY`) referenced by the pods, and
  `elspeth-schema-owner-secrets` (schema-owner role URLs) referenced only by
  the schema-init Job; no manifest gives a pod the schema-owner credentials;
- a `ClusterIP` Service `elspeth-web` on port 8451. EXECUTOR: keep exactly one
  of the next two sentences, the one that matches
  `deploy/kubernetes/base/service.yaml` after Task K7. Session affinity is
  `ClientIP` until routing without affinity is qualified. Session affinity is
  `None`: routing without affinity was qualified in kind (durable tickets,
  durable composer progress, single-use ticket consumption across replicas);
- no ingress, TLS, StorageClass, database or cloud identity in the base; the
  operator supplies them or applies the AKS overlay.

The base never claims horizontal throughput scaling: more replicas add
availability and rollout overlap, and shared rate-limit budgets are not
multiplied by replica count.

## Prerequisites

- `kubectl` at the version pinned in the platform facts, `kind` only if you
  intend to run the acceptance lane locally, `psql`, `jq`, `curl` and Docker
  Buildx (`imagetools` resolves digests; nothing is built here).
- A kubeconfig context with rights to create a namespace, Secrets, a claim,
  Jobs, a Service and a Deployment.
- A StorageClass that provisions `ReadWriteMany` volumes (NFS 4.1 qualifies;
  SMB does not; kind's default `standard` class is `ReadWriteOnce` and must
  not be used).
- External PostgreSQL reachable from the cluster and from the operator host,
  with the two databases created and an administrator login. TLS to the
  server with `sslmode=verify-full`; the runtime image's CA store carries the
  public roots only, so a private CA needs a derived image or an extra
  mounted bundle, which this runbook does not cover.
- The candidate digest already published to GitHub Container Registry by
  `build-push.yaml`.

Set only operator-selected, non-secret inputs:

```bash
set -Eeuo pipefail
umask 077
: "${KUBECONFIG:?set the kubeconfig for the target cluster}"
: "${KUBE_CONTEXT:?set the kubectl context name}"
: "${NAMESPACE:?set the namespace, for example elspeth}"
: "${RWX_STORAGE_CLASS:?set a ReadWriteMany-capable StorageClass name}"
: "${DEPLOY_REF:?set the exact branch, tag, or commit to deploy}"
: "${ELSPETH_RELEASE:?set the release identity the membership row records, for example 0.8.1+<short sha>}"

CANDIDATE_SHA=$(git rev-parse "${DEPLOY_REF}^{commit}")
test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"
test -z "$(git status --porcelain)"
CHECKOUT=$(git rev-parse --show-toplevel)
OPERATOR_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/elspeth/kubernetes/${KUBE_CONTEXT}/${NAMESPACE}"
mkdir -p "$OPERATOR_DIR/overlay"
chmod 700 "$OPERATOR_DIR"
RENDER_DIR="$OPERATOR_DIR/render-${CANDIDATE_SHA}"
BASE_RELATIVE=$(realpath --relative-to="$OPERATOR_DIR/overlay" "$CHECKOUT/deploy/kubernetes/base")
kubectl config use-context "$KUBE_CONTEXT"
kubectl version --client --output=json | jq -er '.clientVersion.gitVersion'
```

Compare the printed client version with the platform facts pin. Stop on a
mismatch: the base is rendered and admitted in CI with that exact `kubectl`.

## 1. Select and prove the cluster

```bash
kubectl cluster-info
kubectl get storageclass "$RWX_STORAGE_CLASS" -o json | jq -er '.provisioner'
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"
kubectl get namespace "$NAMESPACE" -o jsonpath='{.metadata.name}'
```

> **LIVE:** the provisioner name and its documented `ReadWriteMany` support.
> Stop if the class is `ReadWriteOnce`-only; a claim on it stays `Pending`
> forever in step 5.

## 2. Create the database roles

The environment owns the databases and the administrator; ELSPETH owns the
two application roles. From the operator host, point `psql` at the server
with `PGHOST`, `PGUSER` set to the administrator, `PGDATABASE=postgres`,
`PGSSLMODE=verify-full` and the operator's `PGSSLROOTCERT`. Supply every
password through the environment; the script reads them with `\getenv` and
they never appear on argv or in the log.
`deploy/kubernetes/base/bootstrap-roles.sql` reads exactly two:
`ELSPETH_SCHEMA_OWNER_PASSWORD` and `ELSPETH_RUNTIME_PASSWORD`.

```bash
: "${PGHOST:?set the external PostgreSQL host}"
: "${PGUSER:?set the administrator login}"
: "${PGPASSWORD:?export the administrator password without printing it}"
: "${PGSSLROOTCERT:?set the operator PostgreSQL CA bundle path}"
: "${ELSPETH_SCHEMA_OWNER_PASSWORD:?export a fresh schema-owner password}"
: "${ELSPETH_RUNTIME_PASSWORD:?export a different fresh runtime password}"
export PGDATABASE=postgres PGSSLMODE=verify-full
psql --no-psqlrc --set=ON_ERROR_STOP=1 \
  --file "$CHECKOUT/deploy/kubernetes/base/bootstrap-roles.sql" \
  >"$OPERATOR_DIR/bootstrap-roles.log" 2>&1
unset ELSPETH_SCHEMA_OWNER_PASSWORD ELSPETH_RUNTIME_PASSWORD
```

This cold-only script fails on existing roles. It makes `elspeth_schema_owner`
the owner of both databases and grants `elspeth_runtime` only connection,
schema usage and default data/sequence/function privileges on objects that
owner creates. The runtime role therefore reads every session table and can
`INSERT` and `UPDATE` `web_instances` (a replica registers itself there at
boot), but holds no DDL. A production install runs only this file. The
acceptance roles `elspeth_runtime_a` and `elspeth_runtime_b` live in the
separate `deploy/kubernetes/base/bootstrap-acceptance-roles.sql` (which
includes this file first); only the kind harness runs it, so no production
server ever has them.

## 3. Publish the image by digest

```bash
GHCR_DIGEST=$(docker buildx imagetools inspect "ghcr.io/dta-au/elspeth:sha-${CANDIDATE_SHA}" \
  --format '{{.Manifest.Digest}}')
export CANDIDATE_IMAGE="ghcr.io/dta-au/elspeth@${GHCR_DIGEST}"
printf '%s\n' "$CANDIDATE_IMAGE" >"$OPERATOR_DIR/candidate-image-${CANDIDATE_SHA}.txt"
```

Deploy the `@sha256:` reference, never a tag. If the cluster pulls from a
private registry, copy the index with
`docker buildx imagetools create --tag <registry>/elspeth:sha-<sha> ghcr.io/dta-au/elspeth@<digest>`
(a registry-to-registry copy preserves the digest; a second build never
does), prove the copied digest equals `GHCR_DIGEST`, and add
`newName: <registry>/elspeth` under `images:` in the overlay of step 5.

## 4. Create the Secrets

Prepare two mode-0600 `KEY=VALUE` files in a mode-0700 operator-local
directory through the operator's secret manager. Never type a value on the
command line.

`elspeth-web-secrets.env` (runtime role):

- `ELSPETH_WEB__SESSION_DB_URL` and `ELSPETH_WEB__LANDSCAPE_URL` as
  `postgresql+psycopg://elspeth_runtime:<url-escaped password>@<host>:5432/<database>?sslmode=verify-full&sslrootcert=system`
  (one per database; `sslrootcert=system` uses the image's CA store);
- `ELSPETH_WEB__SECRET_KEY` and `ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY`,
  each generated with `openssl rand -base64 32` (the signing key must decode
  as base64 to at least 32 bytes; `src/elspeth/web/config.py` refuses less).

`elspeth-schema-owner-secrets.env` (schema-owner role): the same two URL keys
with `elspeth_schema_owner` as the user. Nothing else belongs in it.

```bash
: "${SECRET_VALUE_DIR:?absolute mode-0700 directory holding the two env files}"
for env_file in elspeth-web-secrets.env elspeth-schema-owner-secrets.env; do
  test "$(stat -c '%a' "$SECRET_VALUE_DIR/$env_file")" = "600"
done
kubectl -n "$NAMESPACE" create secret generic elspeth-web-secrets --from-env-file="$SECRET_VALUE_DIR/elspeth-web-secrets.env"
kubectl -n "$NAMESPACE" create secret generic elspeth-schema-owner-secrets --from-env-file="$SECRET_VALUE_DIR/elspeth-schema-owner-secrets.env"
test "$(kubectl -n "$NAMESPACE" get secret elspeth-web-secrets -o json | jq -c '.data | keys')" = \
  '["ELSPETH_WEB__LANDSCAPE_URL","ELSPETH_WEB__SECRET_KEY","ELSPETH_WEB__SESSION_DB_URL","ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY"]'
test "$(kubectl -n "$NAMESPACE" get secret elspeth-schema-owner-secrets -o json | jq -c '.data | keys')" = \
  '["ELSPETH_WEB__LANDSCAPE_URL","ELSPETH_WEB__SESSION_DB_URL"]'
```

Only key names are printed. On AKS the [overlay](../../deploy/kubernetes/overlays/aks)
replaces this step with a `SecretProviderClass` that syncs Key Vault versions
into a Secret of the same name `elspeth-web-secrets`; the schema-owner Secret
is still created here, from the vault the runtime identity cannot read.

## 5. Provision storage

Write the operator overlay outside Git. It pins the namespace, the digest, the
per-rollout revision annotation (`ELSPETH_K8S_REVISION` is projected from it
and becomes `web_instances.deployment_generation`), the release identity
(`ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE`, which becomes
`web_instances.image_digest`) and the StorageClass. Both `REPLACE_PER_ROLLOUT`
placeholders in the base pass the identity regexes, so a forgotten patch boots
and reports a placeholder as its generation: the render check below refuses
that. It also refuses K1's all-zero release digest (`sha256:` followed by 64
zeros), which a site overlay without `images:` would leave in place. Retain this file for redeployment.

```bash
cat >"$OPERATOR_DIR/overlay/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: ${NAMESPACE}
resources:
  - ${BASE_RELATIVE}
images:
  - name: ghcr.io/dta-au/elspeth
    digest: ${GHCR_DIGEST}
patches:
  - target:
      kind: Deployment
      name: elspeth-web
    patch: |-
      - op: replace
        path: /spec/template/metadata/annotations/elspeth.io~1revision
        value: sha-${CANDIDATE_SHA:0:7}
  - target:
      kind: ConfigMap
      name: elspeth-web-config
    patch: |-
      - op: replace
        path: /data/ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE
        value: ${ELSPETH_RELEASE}
  - target:
      kind: PersistentVolumeClaim
      name: elspeth-state
    patch: |-
      - op: add
        path: /spec/storageClassName
        value: ${RWX_STORAGE_CLASS}
EOF
rm -rf "$RENDER_DIR"
mkdir -p "$RENDER_DIR"
kubectl kustomize "$OPERATOR_DIR/overlay" -o "$RENDER_DIR"
test "$(ls "$RENDER_DIR" | wc -l)" -eq 6
if grep -rq 'REPLACE_PER_ROLLOUT\|sha256:0\{64\}' "$RENDER_DIR"; then
  echo "a base placeholder (REPLACE_PER_ROLLOUT or the all-zero release digest) survived the render; fix the overlay before applying" >&2
  exit 1
fi
kubectl apply -f "$RENDER_DIR"/*_configmap_elspeth-web-config.yaml
kubectl apply -f "$RENDER_DIR"/*_persistentvolumeclaim_elspeth-state.yaml
kubectl apply -f "$RENDER_DIR"/*_job_elspeth-provision-storage.yaml
kubectl -n "$NAMESPACE" wait --for=condition=complete job/elspeth-provision-storage --timeout=300s
kubectl -n "$NAMESPACE" logs job/elspeth-provision-storage | tee "$OPERATOR_DIR/provision-storage-${CANDIDATE_SHA}.log"
```

`kubectl kustomize -o <directory>` writes one file per resource, named
`<group>_<version>_<kind>_<name>.yaml` with no namespace prefix (the core
group has no group segment: `v1_configmap_elspeth-web-config.yaml`,
`apps_v1_deployment_elspeth-web.yaml`); applying by file is what orders
storage, schema and runtime proof before traffic, because `kubectl apply -k`
would start the Deployment at once. The overlay names the base by a path
relative to the overlay directory (`BASE_RELATIVE`, computed in the
prerequisites): kustomize refuses an absolute path under `resources:` with
`new root '<path>' cannot be absolute`, whatever the load restrictor. If the
checkout moves, rerun the prerequisites and rewrite the overlay.

The provisioner is a root Job from a digest-pinned busybox, never an init
container in the application pod: `fsGroup` is not applied to every volume
type and a fresh share arrives root-owned, so the application pod (UID 1654,
no capabilities) cannot create its own subtree. The Job creates
`/mnt/elspeth/data`, `/mnt/elspeth/data/blobs` and `/mnt/elspeth/payloads` as
`1654:1654`, mode `0700`, and is idempotent. Both Jobs carry
`ttlSecondsAfterFinished: 600`: capture the log now, and expect the Job object
to be gone ten minutes after completion.

> **LIVE:** the `ls -ln /mnt/elspeth` lines in the Job log: owner `1654`,
> group `1654`, mode `drwx------` on all three paths.

## 6. Initialize schemas

```bash
kubectl apply -f "$RENDER_DIR"/*_job_elspeth-schema-init.yaml
kubectl -n "$NAMESPACE" wait --for=condition=complete job/elspeth-schema-init --timeout=600s
kubectl -n "$NAMESPACE" logs job/elspeth-schema-init | tee "$OPERATOR_DIR/schema-init-${CANDIDATE_SHA}.log"
```

`elspeth-schema-init` runs `elspeth doctor deployment --init-schema` as the
schema-owner role from `elspeth-schema-owner-secrets`; no pod ever references
that Secret. The doctor exits non-zero on any failing check and the Job has
`backoffLimit: 0`, so `wait --for=condition=complete` is the gate: a timeout
means the Job failed (`kubectl -n "$NAMESPACE" get job elspeth-schema-init -o jsonpath='{.status.conditions}'`
names the reason). `--init-schema` initializes only `MISSING` or repairable
schemas; `STALE` is a stop, not a migration: recreate both stores per the
[session DB reset runbook](staging-session-db-recreation.md) and rerun.

## 7. Prove runtime credentials

The base ships no runtime doctor Job (its resource set is pinned to exactly
two Jobs), so run an operator-local one-shot Job with the runtime Secret and
the same image. It must pass before any pod takes traffic.

```bash
kubectl -n "$NAMESPACE" apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: elspeth-doctor-runtime
  namespace: ${NAMESPACE}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsUser: 1654
        runAsGroup: 1654
        fsGroup: 1654
        runAsNonRoot: true
      containers:
        - name: doctor-runtime
          image: ${CANDIDATE_IMAGE}
          args: ["doctor", "deployment", "--json"]
          envFrom:
            - configMapRef:
                name: elspeth-web-config
            - secretRef:
                name: elspeth-web-secrets
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
EOF
kubectl -n "$NAMESPACE" wait --for=condition=complete job/elspeth-doctor-runtime --timeout=300s
kubectl -n "$NAMESPACE" logs job/elspeth-doctor-runtime | grep '^\[' | tee "$OPERATOR_DIR/doctor-runtime-${CANDIDATE_SHA}.json" | jq -e 'all(.[]; .ok)'
```

`elspeth doctor deployment --json` prints one JSON list of
`{name, ok, detail}` checks on stdout and moves its log lines to stderr; the
container log interleaves both, and the report is the only line beginning
with `[` (log lines are JSON objects under `ELSPETH_WEB__LOG_JSON=true`).
Every check must be `ok`, including `separate_db_targets`, `session_tls`,
`landscape_tls`, `session_schema` and `landscape_schema`; the runtime role
must find both schemas already present because it cannot create them.

## 8. Deploy the workload

```bash
kubectl apply -f "$RENDER_DIR"/*_service_elspeth-web.yaml
kubectl apply -f "$RENDER_DIR"/*_deployment_elspeth-web.yaml
kubectl -n "$NAMESPACE" rollout status deployment/elspeth-web --timeout=600s
kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/name=elspeth-web -o wide
test "$(kubectl -n "$NAMESPACE" get deployment elspeth-web -o jsonpath='{.status.readyReplicas}')" -eq 2
```

Production shape: `replicas: 2`, `RollingUpdate` with `maxSurge: 1` and
`maxUnavailable: 0`, `terminationGracePeriodSeconds: 90`, startup probe
`/api/health` (5 s × 36), liveness `/api/health` (30 s × 3), readiness
`/api/ready` (10 s × 3), `securityContext` UID/GID 1654 with `runAsNonRoot`
and all capabilities dropped, environment
`ELSPETH_WEB__DEPLOYMENT_TARGET=kubernetes`,
`ELSPETH_WEB__DEPLOYMENT_STATE_MODE=external-postgresql`,
`ELSPETH_WEB__HOST=0.0.0.0`, `WEB_CONCURRENCY=1`, `ELSPETH_WEB__LOG_JSON=true`,
and the downward-API projections `ELSPETH_K8S_POD_NAME` (from
`metadata.name`) and `ELSPETH_K8S_REVISION` (from the `elspeth.io/revision`
annotation the overlay patched). Two replicas with an equal compatibility key
overlap safely: the session-operation and run fences serialise them, and a
candidate whose `SESSION_SCHEMA_EPOCH`, `SQLITE_SCHEMA_EPOCH` or coordination
protocol differs is refused by validate-only startup before it becomes ready,
so the old replicas keep serving.

Expose the Service through your ingress controller. If the ingress adds a
proxy read timeout, set `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS`
in the overlay to the minimum across hops minus 10; the AKS overlay does this
against an ingress-nginx `proxy-read-timeout` of at least 3600 with cookie
affinity `elspeth-replica`.

## 9. Verify

```bash
kubectl -n "$NAMESPACE" port-forward service/elspeth-web 18451:8451 >"$OPERATOR_DIR/port-forward.log" 2>&1 &
PORT_FORWARD_PID=$!
sleep 3
curl --silent --fail-with-body http://127.0.0.1:18451/api/health
curl --silent --fail-with-body http://127.0.0.1:18451/api/ready | jq -e '.ready == true'
curl --silent --fail-with-body http://127.0.0.1:18451/api/system/status \
  | jq -e --arg revision "sha-${CANDIDATE_SHA:0:7}" \
      '.deployment_target == "kubernetes" and .deployment_revision == $revision and (.deployment_replica | startswith("elspeth-web-"))'
kill "$PORT_FORWARD_PID"
wait "$PORT_FORWARD_PID" 2>/dev/null || true

: >"$OPERATOR_DIR/instances-${CANDIDATE_SHA}.txt"
for pod in $(kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/name=elspeth-web -o jsonpath='{.items[*].metadata.name}'); do
  kubectl -n "$NAMESPACE" port-forward "pod/${pod}" 18452:8451 >/dev/null 2>&1 &
  POD_FORWARD_PID=$!
  sleep 3
  curl --silent --fail-with-body --dump-header - --output /dev/null http://127.0.0.1:18452/api/health \
    | grep -i '^x-elspeth-instance:' | tee -a "$OPERATOR_DIR/instances-${CANDIDATE_SHA}.txt"
  kill "$POD_FORWARD_PID"
  wait "$POD_FORWARD_PID" 2>/dev/null || true
done
test "$(sort -u "$OPERATOR_DIR/instances-${CANDIDATE_SHA}.txt" | wc -l)" -eq 2

psql --no-psqlrc --set=ON_ERROR_STOP=1 --dbname elspeth_sessions --tuples-only --command \
  "SELECT instance_id, deployment_generation, revision_label, image_digest, state FROM web_instances WHERE state = 'active' ORDER BY started_at" \
  | tee "$OPERATOR_DIR/web-instances-${CANDIDATE_SHA}.txt"
```

Require: HTTP 200 on both probes; `/api/system/status` reporting
`deployment_target` `kubernetes`, `deployment_revision` equal to the patched
annotation and `deployment_replica` equal to a pod name; two distinct
`X-Elspeth-Instance` values (instance ids are minted per process, never
pinned to the pod name); and exactly two `active` rows in `web_instances`
whose `deployment_generation` and `revision_label` both equal
`sha-<short sha>` and whose `image_digest` equals `ELSPETH_RELEASE`. Record
the context, namespace, digest and revision in operator-local notes under
`~/.local/state/elspeth/kubernetes/`, not in a tracked file.

> **LIVE:** the two instance ids and the two `web_instances` rows.

## Rollback

Nothing on this target records rollback permission ahead of time, so the
rule is measured, not looked up: `kubectl rollout undo` is permitted only when the
previous image understands the schemas now on the server. Prove it with the
step 7 Job pointed at the previous digest (never pass `--init-schema` here):

```bash
: "${PREVIOUS_IMAGE:?the ghcr.io/dta-au/elspeth@sha256 reference the cluster ran before this rollout}"
kubectl -n "$NAMESPACE" get job elspeth-doctor-previous >/dev/null 2>&1 && kubectl -n "$NAMESPACE" delete job elspeth-doctor-previous --wait=true
kubectl -n "$NAMESPACE" get job elspeth-doctor-runtime -o json \
  | jq --arg image "$PREVIOUS_IMAGE" '
      .metadata = {name: "elspeth-doctor-previous", namespace: .metadata.namespace}
      | del(.status, .spec.selector, .spec.template.metadata.labels)
      | .spec.template.spec.containers[0].image = $image' \
  | kubectl apply -f -
kubectl -n "$NAMESPACE" wait --for=condition=complete job/elspeth-doctor-previous --timeout=300s
kubectl -n "$NAMESPACE" logs job/elspeth-doctor-previous | grep '^\[' | jq -e 'all(.[]; .ok)'
kubectl -n "$NAMESPACE" rollout undo deployment/elspeth-web
kubectl -n "$NAMESPACE" rollout status deployment/elspeth-web --timeout=600s
```

If the step 7 Job has already been reaped by its TTL, re-apply the step 7
manifest with `PREVIOUS_IMAGE` in place of `CANDIDATE_IMAGE` instead of
cloning it. If the previous image reports `session_schema` or
`landscape_schema` not ok, it does not understand the current schemas: keep
the candidate and repair forward. A cold install has no previous image and
never rolls back. After a permitted rollback, repeat step 9 against the
restored ReplicaSet.

## Scale and drain

`kubectl -n "$NAMESPACE" scale deployment/elspeth-web --replicas=<n>` with
`n` between 2 and 4 is supported; every replica needs the runtime Secret and
the shared claim, nothing else. Scale-in and every rolling replacement
terminate pods with SIGTERM, which the application lifespan handles in this
order (`src/elspeth/web/app.py`): the `instance_draining` event is set so
`/api/ready` fails at once and the platform stops routing new work here; the
replica's `web_instances` row is written `draining`; in-flight execution
drains; the row is written `stopped` with its lease expired so a peer takes
over immediately; then telemetry and the worker pool shut down. The base
bounds all of that with `terminationGracePeriodSeconds: 90`. A pod killed
outside the grace period (node loss, `--grace-period=0`) leaves its row
`active` until the lease expires; peers take over at expiry, which the
`role_revocation_lease_expiry` probe measures.

## Redeploy

For an image or configuration replacement on a cluster that already runs
ELSPETH: repeat the prerequisites with the new `DEPLOY_REF`, step 3, and
step 5's overlay rewrite and render (skip its `apply` lines for the claim and
the provisioner: the share already exists; re-apply the ConfigMap file), then
step 6 (a previous `elspeth-schema-init` older than its TTL is gone; delete it
with `kubectl -n "$NAMESPACE" delete job elspeth-schema-init --wait=true` if
it is not, because Job templates are immutable), step 7, and step 8, whose
`kubectl apply` of the Deployment file starts the rolling update. Finish with
step 9. Replace the runtime Secret before step 7 when rotating keys; a rotated
`ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY` invalidates outstanding links.

## Acceptance evidence

The evidence for the claims above is the CI kind lane, not a receipt:
`scripts/cicd/kubernetes-kind-smoke.sh` creates a disposable kind cluster,
builds and loads the image, applies `deploy/kubernetes/overlays/kind-test/`
(one Deployment, two replicas, shared hostPath `ReadWriteMany` volume, harness
PostgreSQL) and `deploy/kubernetes/overlays/kind-acceptance/` (two one-replica
Deployments `elspeth-web-a` and `elspeth-web-b` on roles `elspeth_runtime_a`
and `elspeth_runtime_b`, created by
`deploy/kubernetes/base/bootstrap-acceptance-roles.sql`), and runs
`pytest tests/testcontainer/deployment/test_kubernetes_kind.py tests/testcontainer/deployment/test_kubernetes_replica_probes.py -m kind -n 0`.
The probe results are pytest outcomes by probe id; `owner_affine` is
`cannot_pass` by construction. Run the same script locally with Docker, `kind`
and `kubectl` installed at the platform-facts pins.

## Troubleshooting

### Pods restart before the schema Job has completed

The web process validates both schemas at startup and never creates them
(`ExternalStateSchemaNotReadyError` in the pod log). This is the expected
state if the Deployment was applied before step 6 completed; finish steps 6
and 7 and the next restart becomes ready.

### The claim stays `Pending`

The StorageClass cannot provision `ReadWriteMany`. Delete the claim, choose a
class that can (step 1), and re-render.

### `permission denied for table web_instances`

The runtime role lacks `INSERT`/`UPDATE` on `web_instances`. Default
privileges apply only to objects the schema owner creates after
`bootstrap-roles.sql` ran; if the schema was initialized as a different role,
recreate it as `elspeth_schema_owner` (step 6) or grant the verbs explicitly.

### The runtime doctor fails `session_tls` or `landscape_tls`

`verify-full` fails when the host in the URL does not match the server
certificate, or when the server's CA is not in the image's public store.
Use the server's canonical host name; a private CA needs a mounted bundle and
`sslrootcert=<path>` in the URL.

## Teardown

```bash
kubectl -n "$NAMESPACE" scale deployment/elspeth-web --replicas=0
kubectl -n "$NAMESPACE" rollout status deployment/elspeth-web --timeout=300s
kubectl delete namespace "$NAMESPACE"
```

Scaling to zero first drains every replica through SIGTERM so no
`web_instances` row is left `active` under a live lease. Deleting the
namespace deletes the claim; whether the volume's data survives is the
StorageClass's `reclaimPolicy`, so back up the share first. The databases and
roles are outside the cluster and untouched.
````

- [ ] **Step 5: Flip `docs/reference/deployment-platforms.md`.** Six edits, each a full replacement.

Lines 10-11 (`provisions Azure Database for PostgreSQL. Azure VM production and Kubernetes` / `BYO deployments require operator-provided external PostgreSQL.`) become:

```markdown
provisions Azure Database for PostgreSQL. Azure VM production and Kubernetes
deployments require operator-provided external PostgreSQL.
```

Lines 15-17 become:

```markdown
Use an immutable, release-specific image tag or digest. Run one web process
per replica; replicas > 1 are supported only on the Azure Container Apps and
Kubernetes targets with external PostgreSQL for both stores. Payload
persistence is separate from database persistence: preserve both stores
across every replacement.
```

Line 27 (the `| Kubernetes (BYO manifests) |` row) becomes one line:

```markdown
| Kubernetes | External PostgreSQL (two databases; schema-owner and runtime roles) | One `ReadWriteMany` claim (NFS 4.1 qualifies; SMB does not) | [Kustomize base](../../deploy/kubernetes/base), [deployment runbook](../runbooks/kubernetes-deployment.md), [AKS overlay](../../deploy/kubernetes/overlays/aks) | Implemented; kind acceptance for the two-replica configuration; no live AKS acceptance is claimed |
```

Lines 30-32 become:

```markdown
There is no generated deployment-profile schema in this release. The tracked
deployment artifacts are the Compose and portable systemd bundles, the AWS ECS
Terraform package, the ACA Bicep bundle, the Kubernetes Kustomize base with
its AKS overlay, and their acceptance controllers.
```

Lines 38-40 become:

```markdown
- Run one web process per replica (`WEB_CONCURRENCY=1`). Except for the
  Azure Container Apps and Kubernetes multi-replica configurations below,
  keep one replica and stop the old process before starting its replacement.
```

Lines 180-193 (the whole `## Kubernetes` section, to end of file) become:

```markdown
## Kubernetes

The provider-neutral `kubernetes` target ships a maintained Kustomize base at
[`deploy/kubernetes/base`](../../deploy/kubernetes/base) at the same
multi-replica bar as Azure Container Apps: two replicas behind one
`RollingUpdate` Deployment (`maxSurge: 1`, `maxUnavailable: 0`), one web
process per replica, external PostgreSQL for both stores under two roles (a
schema-owner role used only by the `elspeth-schema-init` Job and a DDL-less
runtime role used by the pods), one `ReadWriteMany` claim for `data`,
`data/blobs` and `payloads` (NFS 4.1 qualifies; SMB does not), a root
`elspeth-provision-storage` Job that owns the share subtree to UID/GID 1654
(`1654:1654`, mode `0700`), downward-API replica identity
(`ELSPETH_K8S_POD_NAME` from the pod name and
`ELSPETH_K8S_REVISION` from the per-rollout `elspeth.io/revision` annotation,
recorded as the membership row's deployment generation), `/api/health`
liveness and `/api/ready` readiness, and a SIGTERM drain bounded by
`terminationGracePeriodSeconds: 90`. The base installs no PostgreSQL,
ingress, TLS, StorageClass or cloud identity; the
[AKS overlay](../../deploy/kubernetes/overlays/aks) adds Azure Files NFS
storage, Key Vault CSI secrets and an ingress-nginx cookie-affinity ingress.

Follow the [Kubernetes runbook](../runbooks/kubernetes-deployment.md).
Evidence is kind acceptance in CI: the `kubernetes-render` job renders the
base with a checksum-pinned `kubectl`, and the `kubernetes-kind` job proves
two-replica startup under one deployment generation, shared state across
replicas, a provider-free run, and the replica probes P1–P4 (`owner_affine`
remains `cannot_pass`). No live AKS acceptance is claimed. `sqlite-single` is
refused for this target at configuration time; the operator owns
storage-class behaviour, backups and database availability.
```

Keep the literal phrase `UID/GID 1654` in the new section:
`test_support_matrix_includes_runtime_connection_and_doctor_inputs` (:80-87)
pins it, and its only occurrence on HEAD is line 189, which this section
replaces (measured 2026-09-13: applying the six edits without it turns that
test red). EXECUTOR: the session-affinity sentence is deliberately absent
from this section so the K7 result lives in one place (the runbook and
`service.yaml`); do not add one.

- [ ] **Step 6: Flip the remaining public surfaces.** Each edit is a full replacement of the lines named.

`docs/guides/docker.md:137` (`outside the application task. Azure production and BYO Kubernetes deployments`) becomes:

```markdown
outside the application task. Azure production and Kubernetes deployments
```

`docs/guides/docker.md:370-375` (the `### Kubernetes` section) becomes:

```markdown
### Kubernetes

The tracked Kustomize base at `deploy/kubernetes/base` runs this image as two
replicas behind a `RollingUpdate` with external PostgreSQL for both stores,
one `ReadWriteMany` claim and downward-API replica identity; an AKS overlay
adds Azure Files NFS, Key Vault CSI and ingress. Follow the
[Kubernetes runbook](../runbooks/kubernetes-deployment.md) and the
[deployment platform contract](../reference/deployment-platforms.md#kubernetes).
```

`docs/guides/docker.md:550` becomes:

```markdown
- [Deployment Platforms](../reference/deployment-platforms.md) - Maintained deployment paths and support boundaries
- [Kubernetes Deployment](../runbooks/kubernetes-deployment.md) - Two-replica Kustomize base on any cluster with RWX storage and external PostgreSQL
```

`docs/runbooks/index.md:18` becomes:

```markdown
| [Deployment Platforms](../reference/deployment-platforms.md) | Choose Compose, AWS ECS, native Linux, Azure Ubuntu VM, Azure Container Apps, or Kubernetes; ACA has desktop acceptance for Single/sticky with local PostgreSQL runtime evidence; Kubernetes has kind acceptance for the two-replica base |
```

Insert after `docs/runbooks/index.md:19` (the Native Linux row):

```markdown
| [Kubernetes Deployment](kubernetes-deployment.md) | Install the two-replica Kustomize base on a cluster with `ReadWriteMany` storage and external PostgreSQL; AKS overlay; rollback, drain and redeploy |
```

Insert after `docs/README.md:20` (the "Deploying a new AWS stack" row):

```markdown
| Deploying on Kubernetes | [Kubernetes Deployment](runbooks/kubernetes-deployment.md), then the [platform contract](reference/deployment-platforms.md#kubernetes) |
```

`docs/repository-structure.md:26` becomes one line:

```markdown
| Deployment | `deploy/`, root `Dockerfile` / `docker-compose.yaml` | How the service is shipped and run: `deploy/compose/` adds the maintained PostgreSQL Compose bundle, `deploy/aws-ecs/terraform/` contains the supported disposable ECS cold-install source, `deploy/azure-container-apps/` contains the Container Apps Bicep source, `deploy/kubernetes/base/` contains the two-replica Kustomize base with its `overlays/` (kind harness, AKS), and `deploy/linux-systemd/` contains the portable native-Linux unit and environment example. The root `deploy/elspeth-web.service` is a gitignored source-checkout development unit, not repository content. |
```

`docs/repository-structure.md:56-61` becomes:

```markdown
- **`deploy/`** → `compose/` (PostgreSQL and web Compose overlays),
  `aws-ecs/terraform/` (disposable single-replica AWS cold-install source),
  `azure-container-apps/` (Container Apps Bicep source; desktop acceptance for
  Single/sticky; runtime evidence is local PostgreSQL), `kubernetes/base/`
  (provider-neutral two-replica Kustomize base; kind acceptance in CI) with
  `kubernetes/overlays/` (`kind-test/`, `kind-acceptance/`, `aks/`), and
  `linux-systemd/` (portable native-Linux service and environment example).
```

`docs/repository-structure.md:110-116` becomes:

```markdown
✓ Distinct by *target*: `deploy/compose/` = maintained database/web overlays;
`deploy/aws-ecs/terraform/` = maintained disposable AWS ECS infrastructure;
`deploy/azure-container-apps/` = Container Apps Bicep source (desktop acceptance
for Single/sticky; no live cloud acceptance claimed);
`deploy/kubernetes/base/` = two-replica Kustomize base (kind acceptance; the
AKS overlay renders, no live AKS acceptance claimed);
`deploy/linux-systemd/` = portable host
service and environment example; root `Dockerfile`/`docker-compose.yaml` =
container image and CLI-oriented base;
```

`README.md:1073-1076` (from `[AWS ECS cold-install runbook]` through `and Kubernetes boundaries. The separate`) becomes:

```markdown
[AWS ECS cold-install runbook](docs/runbooks/aws-ecs-cold-install.md); for a
Kubernetes cluster, the [Kubernetes runbook](docs/runbooks/kubernetes-deployment.md).
See the [deployment platform matrix](docs/reference/deployment-platforms.md)
for the maintained Compose, AWS ECS, native Linux, Azure Container Apps and
Kubernetes paths plus the explicit Azure VM boundary. The separate
```

`README.md:1168` becomes one line:

```markdown
| [docs/reference/deployment-platforms.md](docs/reference/deployment-platforms.md) | Operators | Maintained deployment paths, database ownership, persistence, and platform boundaries |
```

Insert after `README.md:1172` (the AWS existing-service row):

```markdown
| [docs/runbooks/kubernetes-deployment.md](docs/runbooks/kubernetes-deployment.md) | Operators | Install the two-replica Kustomize base on a Kubernetes cluster; rollback, drain, redeploy |
```

`ARCHITECTURE.md:731-732` (`The maintained deployment set is Docker Compose, AWS ECS, native Linux systemd,` / `one Azure Ubuntu VM, and Kubernetes BYO. Azure Container Apps support is`) becomes:

```markdown
The maintained deployment set is Docker Compose, AWS ECS, native Linux systemd,
one Azure Ubuntu VM, and Kubernetes (`deploy/kubernetes/base`, two replicas,
kind acceptance in CI). Azure Container Apps support is
```

`docs/runbooks/staging-session-db-recreation.md` (the table I11 wrote under `### Cutover by deployment shape`; this edit comes after Step 4 because I11's `test_current_cutover_routes_each_shipped_deployment_shape_to_its_recreate_procedure` requires every link target in that subsection to exist as a file under `docs/runbooks/`, and the row carries no epoch numeral because the same test refuses `epoch <digit>`): insert directly after the row that begins `| VM, external PostgreSQL |`, as the last row of the table:

```markdown
| Kubernetes | PostgreSQL, both stores | the database owner archives/exports, drops and recreates both databases, then [kubernetes-deployment.md](kubernetes-deployment.md) [§6. Initialize schemas](kubernetes-deployment.md#6-initialize-schemas), [§7. Prove runtime credentials](kubernetes-deployment.md#7-prove-runtime-credentials) and [§8. Deploy the workload](kubernetes-deployment.md#8-deploy-the-workload) for the candidate; a recreate is never the image-only [Redeploy](kubernetes-deployment.md#redeploy) path | this runbook, [Re-admit the cohort](#operator-notice-for-the-workflow-epoch-window) with the deployment's provider |
```

- [ ] **Step 7: Rewrite the July spec to the multi-replica bar.** In `docs/specs/2026-07-26-finish-deferred-deployment-platforms-design.md` replace lines 928-952 (from `## Kubernetes Bundle` through `provider-free run. Harness PostgreSQL never appears in the shipped base.`) with:

```markdown
## Kubernetes Bundle

`deploy/kubernetes/base/` contains a provider-neutral Kustomize base at the
multi-replica bar the Phase 6b Azure Container Apps plan established (§3.5
Rollout, §4 Storage and multi-replica contract), superseding the `Recreate`,
`replicas: 1` shape this section specified until 2026-09-13. It ships:

- one `Deployment`, `replicas: 2`, `strategy: RollingUpdate` with
  `maxSurge: 1` and `maxUnavailable: 0`: overlapping replicas carry an equal
  compatibility key (session epoch, Landscape epoch, coordination protocol)
  and the session-operation and run fences serialise them, while a candidate
  with an unequal key is refused by validate-only startup before it becomes
  ready, so the old replicas keep serving;
- one ClusterIP `Service` on port 8451 whose session affinity is `ClientIP`
  until routing without affinity is qualified in kind;
- non-secret runtime/composer configuration in one ConfigMap, with the
  per-rollout release identity (`ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE`) as
  a placeholder every overlay replaces;
- downward-API replica identity: `ELSPETH_K8S_POD_NAME` from `metadata.name`
  and `ELSPETH_K8S_REVISION` from the per-rollout `elspeth.io/revision` pod
  annotation, which the `kubernetes` startup profile records as the
  membership row's deployment generation;
- one `ReadWriteMany` persistent-volume claim for data, blob and payload paths
  with no storage class chosen (NFS 4.1 qualifies; SMB does not);
- a root `elspeth-provision-storage` Job from a digest-pinned busybox that
  creates the share subtree as `1654:1654`, mode `0700`, because `fsGroup`
  does not reach every volume type — never an init container in the
  application pod;
- an `elspeth-schema-init` Job running `elspeth doctor deployment
  --init-schema` as the schema-owner role from its own Secret
  (`elspeth-schema-owner-secrets`); the pods reference only the runtime Secret
  (`elspeth-web-secrets`); both Jobs carry `ttlSecondsAfterFinished` so a
  redeploy recreates them;
- two documentation-only Secret examples excluded from `kustomization.yaml`;
- an immutable GHCR image reference by digest;
- `WEB_CONCURRENCY=1`, target `kubernetes`, and external PostgreSQL state;
- liveness `/api/health`, readiness `/api/ready`, a startup probe, and a
  SIGTERM drain bounded by `terminationGracePeriodSeconds: 90`; and
- non-root UID/GID 1654 with every capability dropped.

The base does not install PostgreSQL, ingress, TLS, a storage class, a database
operator, or cloud identity controllers. Operators supply those dependencies;
the AKS overlay under `deploy/kubernetes/overlays/aks/` supplies Azure Files
NFS storage, Key Vault CSI secrets and an ingress with cookie affinity.

Static CI renders the base with a version- and checksum-pinned `kubectl`. A
Docker-backed kind lane builds and loads the final ELSPETH image, provisions
PostgreSQL only as harness infrastructure, initializes separate sessions and
Landscape databases, applies the kind overlays, and proves readiness of two
replicas under one deployment generation, a provider-free run whose output is
visible from the other replica, and the replica probes P1–P4. Harness
PostgreSQL never appears in the shipped base.
```

Replace lines 1564-1565 (the bullet `- Supporting multiple steady-state replicas, multiple web processes per` / `  replica, or horizontal throughput scaling.`) with:

```markdown
- Supporting multiple web processes per replica, or horizontal throughput
  scaling: replicas > 1 (Azure Container Apps, Kubernetes) add availability
  and rollout overlap, never multiplied shared budgets.
```

- [ ] **Step 8: Extend the startup-profile docstring.** In `src/elspeth/web/deployment_profiles.py` replace lines 29-35 (`and reported by ``/api/system/status``. Azure Container Apps additionally` through `(``_aws_ecs_acceptance/ecs_metadata.py``) — so its profile names no variable.`) with:

```python
and reported by ``/api/system/status``. Azure Container Apps additionally
sets ``CONTAINER_APP_REPLICA_NAME`` and ``CONTAINER_APP_REVISION`` on every
replica; the ACA profile names them and :func:`read_platform_identity`
parses them as a Tier-3 boundary (bounded, allow-listed, fail-closed on a
malformed value). Kubernetes publishes nothing by itself: the shipped base
(``deploy/kubernetes/base/deployment.yaml``) projects ``ELSPETH_K8S_POD_NAME``
from ``metadata.name`` and ``ELSPETH_K8S_REVISION`` from the per-rollout
``elspeth.io/revision`` annotation through the downward API, and the
``kubernetes`` profile names those two variables. AWS ECS does not publish
identity through the environment — the acceptance harness reads the task
metadata endpoint (``_aws_ecs_acceptance/ecs_metadata.py``) — so its profile
names no variable.
```

- [ ] **Step 9: Add the changelog entry.** In `CHANGELOG.md`, after line 62 (`  for the Single/sticky configuration and operating limitations.`, the last line of the "Azure Container Apps deployment" bullet), keeping the blank line 63 before the `---` at line 64, insert (numbers measured on HEAD; if I0/I11 have added bullets above, anchor on that closing line, not the number):

```markdown
- **Kubernetes is a maintained multi-replica target.** `deploy/kubernetes/base`
  ships a provider-neutral Kustomize base at the Azure Container Apps bar: two
  replicas behind a `RollingUpdate`, external PostgreSQL for both stores under
  schema-owner and runtime roles in separate Secrets, one `ReadWriteMany` claim
  provisioned by a root Job, downward-API replica identity, a SIGTERM drain,
  and an AKS overlay. CI renders the base with a checksum-pinned `kubectl` and
  proves two-replica startup, shared state and the replica probes in kind; no
  live AKS acceptance is claimed. See the
  [Kubernetes runbook](docs/runbooks/kubernetes-deployment.md).
```

This lands under `## 0.8.1 - 2026-09-10` because the 2026-09-06 ruling put Kubernetes in 0.8.1; confirm the section with the operator before the first commit (I0 asks the same question for the identity workstream; the second of K8/I0 to land rebases its bullet under the confirmed heading). `tests/unit/website/test_release_site_contract.py:57` requires exactly one `## {CURRENT_VERSION} - ` heading, so never open a second 0.8.1 heading.

- [ ] **Step 10: Run the doc contracts, the runbook contract and every neighbour that reads these files.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/docs tests/unit/web/test_kubernetes_runbook_contract.py tests/unit/web/test_azure_container_apps_runbook_contract.py tests/unit/web/test_aws_ecs_runbook_contract.py tests/unit/web/test_deployment_profiles.py tests/unit/website/test_release_site_contract.py tests/unit/deployment -n 0 > /tmp/k-lane-k8.log 2>&1; echo exit=$?; tail -5 /tmp/k-lane-k8.log`
Expected: `exit=0`; `test_operator_overlay_renders_and_splits_per_resource` and the K1/K6 render tests report SKIPPED locally (`kubectl is not installed`) and run in the CI `test` job, where `ELSPETH_CI_KUBECTL_REQUIRED` turns absence into a failure.

Run: `cd "$(git rev-parse --show-toplevel)" && ruff format tests/unit/web/test_kubernetes_runbook_contract.py tests/unit/docs/test_deployment_platform_docs.py src/elspeth/web/deployment_profiles.py > /tmp/k-lane-k8-ruff.log 2>&1; echo exit=$?; ruff check tests/unit/web/test_kubernetes_runbook_contract.py tests/unit/docs/test_deployment_platform_docs.py src/elspeth/web/deployment_profiles.py >> /tmp/k-lane-k8-ruff.log 2>&1; echo exit=$?`
Expected: `exit=0` twice (`line-length = 140`, `pyproject.toml:283`; `ruff format` may rewrap a line, which is why the pytest run above is repeated once after it if anything changed).

Run: `cd "$(git rev-parse --show-toplevel)" && grep -rn "Kubernetes BYO\|Kubernetes is BYO\|Kubernetes remains BYO\|BYO manifests\|no maintained bundle\|does not ship Kubernetes manifests" README.md ARCHITECTURE.md docs/reference docs/guides docs/runbooks docs/repository-structure.md docs/README.md docs/specs/2026-07-26-finish-deferred-deployment-platforms-design.md; echo exit=$?`
Expected: no lines and `exit=1` (grep found nothing). Control the instrument first: `grep -c "Kubernetes BYO" CHANGELOG.md` must print `1` (the untouched 0.8.0 history line at `CHANGELOG.md:202`), proving the pattern matches when the phrase is present.

- [ ] **Step 11: Commit.** Stage first, then run the safety check, then commit: `scripts/branch-safety-check.sh` inspects the STAGED set (artefact paths, user-home paths, the secret scanner), so a check run before `git add` inspects an index that does not yet hold this task's files. The two created files get `git add -N` (intent-to-add) first because a commit pathspec only selects tracked paths; then every path is staged by name, the check runs immediately before the commit, and the message goes before `--` with every path a file (F3).

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N docs/runbooks/kubernetes-deployment.md tests/unit/web/test_kubernetes_runbook_contract.py
git add -- docs/runbooks/kubernetes-deployment.md tests/unit/web/test_kubernetes_runbook_contract.py tests/unit/docs/test_deployment_platform_docs.py docs/reference/deployment-platforms.md docs/guides/docker.md docs/runbooks/index.md docs/README.md docs/repository-structure.md README.md ARCHITECTURE.md docs/specs/2026-07-26-finish-deferred-deployment-platforms-design.md src/elspeth/web/deployment_profiles.py CHANGELOG.md docs/runbooks/staging-session-db-recreation.md
git status --short
scripts/branch-safety-check.sh --intent commit
git commit -m "docs(deploy): Kubernetes is a maintained multi-replica target" -- docs/runbooks/kubernetes-deployment.md tests/unit/web/test_kubernetes_runbook_contract.py tests/unit/docs/test_deployment_platform_docs.py docs/reference/deployment-platforms.md docs/guides/docker.md docs/runbooks/index.md docs/README.md docs/repository-structure.md README.md ARCHITECTURE.md docs/specs/2026-07-26-finish-deferred-deployment-platforms-design.md src/elspeth/web/deployment_profiles.py CHANGELOG.md docs/runbooks/staging-session-db-recreation.md
git show --stat HEAD
```

Expected: `git show --stat HEAD` lists exactly 14 files. If it lists more, a sibling lane staged into the shared index: `git reset --mixed HEAD~1`, restage only the 14 paths above, and commit again.
