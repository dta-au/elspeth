### Task K4: kind harness — two replicas, shared RWX, real PostgreSQL, provider-free run

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: K3. Runs before: K5. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered after K3 (K0 → K1 → K2 → K3 → K4 → K5 → K7). This task puts the
shipped base on a real API server: a kind cluster with the tree under test
built and loaded as an image, a hostPath `ReadWriteMany` PersistentVolume,
PostgreSQL 16 carrying both databases and the schema-owner/runtime roles, the
two Secrets the base references, and the `kind-test` overlay (one Deployment,
`replicas: 2`, NodePort 30451). Three proofs run against it: both replicas
register distinct `web_instances` members under ONE generation with both
rollout placeholders replaced; a second `kubectl apply -k` succeeds while a
finished Job still exists (and recreates any Job the 600 s TTL already reaped); and a provider-free CSV run started through one pod
has its sink artefacts served by the OTHER pod off the shared volume and the
shared Landscape. A registered `kind` marker keeps the lane out of the default
and Testcontainer selections; one smoke script is the single code path for the
desk and the `kubernetes-kind` CI job, which `ci-success` gates by name.

Measured on HEAD 072141b75 (2026-09-13):

- `which kubectl kind docker` → `/usr/bin/docker` only; `deploy/kubernetes/`
  and `tests/testcontainer/deployment/` do not exist (K1 and this task create
  them). `tests/testcontainer/web/` has no `__init__.py`; the new directory
  mirrors that.
- `pyproject.toml:452` `--strict-markers`; `:454` the default `-m` expression
  `not slow and not stress and not performance and not testcontainer and not live_provider`;
  `:467-479` the `markers` list (no `kind`).
- `ci.yaml:895-950` is the `testcontainer` job (`runs-on: ubuntu-24.04` at
  :900, `timeout-minutes: 30` at :906, pytest at :938-942 with `-m testcontainer`,
  `upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` at :946);
  `host-runner-unit:` starts at :952. `ci-success` is :1332-1385 (`needs:`
  :1335-1344, `if: always()` :1345, one `needs.<job>.result` block per job).
  No CI job runs `docker build` today.
- `tests/unit/cicd/test_state_engine_ci_selection.py:211-248`
  (`test_no_job_restates_the_marker_selection_except_the_testcontainer_override`)
  splits every pytest step's run text on whitespace and pins the token after
  `-m` in the testcontainer job to exactly `testcontainer` (:247-248);
  `:251-282` (`test_testcontainer_job_selects_the_postgresql_matrix`) asserts
  the literal `"-m testcontainer" in commands` at :268. Both go red when the
  selection becomes `-m "testcontainer and not kind"`, so both are edited here.
  `:335` (`test_default_selection_excludes_protected_live_lanes`) asserts only
  `not live_provider` and survives the addopts change.
- The house skip-locally/fail-in-CI shape is
  `tests/unit/deployment/test_azure_container_apps_bundle.py:80-91`
  (`_require_bicep`); the host-runner env-switch precedent is
  `test_state_engine_ci_selection.py:300-325` (`ELSPETH_CI_DOCKER_REQUIRED`).
  `tests/testcontainer/web/conftest.py:31-36` is the xdist refusal
  (`is_xdist_worker(request) or os.environ.get("PYTEST_XDIST_WORKER")` →
  `pytest.UsageError`).
- HTTP surface of the provider-free run: `POST /api/auth/register`
  (`auth/routes.py:350-371`, body `RegisterRequest` :80-86 =
  `username`/`password`/`display_name`, returns `TokenResponse.access_token`
  :128-131; open registration is the default, `config.py:219`);
  `POST /api/sessions` → 201 with `id` (`sessions/routes/sessions.py:735`);
  `POST /api/sessions/{session_id}/blobs` multipart field `file`
  (`blobs/routes.py:132,140`) → 201 `BlobMetadataResponse.id`
  (`blobs/schemas.py:59`); `POST /api/sessions/{session_id}/state/yaml`
  (`sessions/routes/composer/state.py:804-810`, body `ImportStateYamlRequest`
  :210-214 = `yaml` + `source_blob_ids`), which rebinds each named source's
  `path` to the uploaded blob's `storage_path` and stamps `blob_ref`
  (:437-494), drops a `landscape` block on purpose
  (`composer/yaml_importer.py:84-86`: the audit URL is the deployment's)
  and refuses any source path outside `data_dir/{outputs,blobs}/<session>`
  (:342-354 over `paths.py:105`); `POST /api/sessions/{session_id}/execute`
  → 202 `run_id`; `GET /api/runs/{run_id}` (`RunStatusResponse`,
  `execution/schemas.py:1176`, terminal statuses `sessions/protocol.py:221`,
  `accounting.source.rows_processed` :340-372); `GET /api/runs/{run_id}/outputs`
  (`execution/routes.py:1789`, `RunOutputArtifact` :956-1008 with
  `artifact_id`, `path_or_uri`, `storage_kind`, `downloadable`) and
  `GET /api/runs/{run_id}/outputs/{artifact_id}/content` (:1832). Sink paths
  in the `outputs/<name>` authoring form resolve to
  `data_dir/outputs/<session>/<name>` (`paths.py:108-135`). Every response
  carries `X-Elspeth-Instance` (`middleware/instance_identity.py:32`).
  `tests/integration/web/test_execute_pipeline.py:97-185` is the same
  login → create → seed → execute → poll walk, but it stages its CSV with
  `shutil.copy` onto the app's own filesystem (:112-114), which a pod cannot
  see — hence the upload route here.
- The example: `examples/threshold_gate/settings.yaml` (csv source with a
  fixed schema, one gate, two csv sinks, no LLM, `landscape:` block) and
  `examples/threshold_gate/input.csv` (8 rows; `amount > 1000` for Bob 1500,
  Diana 3000, Frank 2000, Henry 5000). `grep -L llm examples/*/settings.yaml`
  lists it; `examples/hello_world` does not exist. DECISIONS K11 says to mount
  the example's files through the test ConfigMap; the import route refuses a
  source path outside the session's blob/output directories (measured above),
  so the example's CSV bytes enter through the upload route instead and the
  example's YAML is what the import route seeds. The example is still the one
  real, provider-free pipeline the run executes.
- The external-state contract (`deployment_contract.py:356-421`) requires
  plain PostgreSQL URLs for the two distinct databases, `ELSPETH_WEB__HOST` =
  the container bind address, a `secret_key` of at least 32 bytes
  (`config.py:46`) and a base64 `shareable_link_signing_key` decoding to at
  least 32 bytes (`config.py:594-598`, :802-806); authenticated TLS is
  required only for `aws-ecs` (:369-376). The deployment doctor probes the
  runtime directories with a temp-file write (`web/doctor.py:57-108`), so the
  schema-init Job needs the share provisioned before it runs.
- The harness PostgreSQL image is K0's facts document §1.3
  `HARNESS_POSTGRES_IMAGE=postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94`
  (the index digest of `postgres:16`, recorded by K0 for this manifest; psql 16,
  so `\getenv` works in init scripts; the image carries `pg_isready` and
  `/docker-entrypoint-initdb.d`). `deploy/compose/postgres.yaml:16` pins a
  different image (`postgres:16-alpine@sha256:57c72fd2…`) for a different
  consumer, the compose bundle; the kind lane does not borrow it.
  `deploy/azure-container-apps/scripts/bootstrap-roles.sql` reads
  `ELSPETH_SCHEMA_OWNER_PASSWORD` / `ELSPETH_RUNTIME_PASSWORD` with `\getenv`
  and `bootstrap-acceptance-roles.sql` reads `ELSPETH_RUNTIME_A_PASSWORD` /
  `ELSPETH_RUNTIME_B_PASSWORD`; K1 mirrors them as two files:
  `deploy/kubernetes/base/bootstrap-roles.sql` creates only
  `elspeth_schema_owner` and `elspeth_runtime`, and
  `deploy/kubernetes/base/bootstrap-acceptance-roles.sql` runs
  `\ir bootstrap-roles.sql` and then creates `elspeth_runtime_a` and
  `elspeth_runtime_b` (DECISIONS K2). The harness must therefore run the
  acceptance file, or the `elspeth-web-secrets-a|b` URLs this fixture mints
  name roles that do not exist and K5's two acceptance pods fail to boot.
- The postgres image entrypoint (measured 2026-09-14 on the pinned
  `postgres:16@sha256:f1c3376c…`, psql 16.15,
  `/usr/local/bin/docker-entrypoint.sh:191,195,363`) runs each
  top-level `/docker-entrypoint-initdb.d/*` entry in name order, `*.sql` with
  `docker_process_sql -f "$f"`, and prints `ignoring` for anything else,
  directories included. With `-f`, `\ir` resolves relative to the running
  file's directory. So a top-level `02-roles.sql` that runs
  `\ir elspeth/bootstrap-acceptance-roles.sql` executes both shipped files
  exactly once, as long as the ConfigMap mounts them under `elspeth/`.
- `scripts/git-hooks/pre-commit-secret-scan.sh:33-44`: the
  `Connection string with password` pattern fires on any tracked
  database URL literal that embeds a user and password, so no harness file carries a URL or a
  password; the fixture mints every credential per session and builds URLs
  with `sqlalchemy.engine.URL.create`.
- `.gitignore:67` ignores `.claude/lanes/`, where the smoke script keeps the
  pinned tools and its logs.

**Files:**
- Create: `deploy/kubernetes/overlays/kind-test/kustomization.yaml`
- Create: `tests/testcontainer/deployment/kubernetes/kind-config.yaml`, `tests/testcontainer/deployment/kubernetes/pv-rwx-hostpath.yaml`, `tests/testcontainer/deployment/kubernetes/postgresql.yaml`, `tests/testcontainer/deployment/kubernetes/01-databases.sql`, `tests/testcontainer/deployment/kubernetes/02-roles.sql`
- Create: `tests/testcontainer/deployment/kind_harness.py`, `tests/testcontainer/deployment/conftest.py`, `tests/testcontainer/deployment/test_kubernetes_kind.py`
- Create: `scripts/cicd/kubernetes-kind-smoke.sh` (mode 0755)
- Modify: `pyproject.toml:454` (the default `-m` expression gains `and not kind`), `pyproject.toml:467-479` (register the `kind` marker)
- Modify: `.github/workflows/ci.yaml:938-942` (testcontainer selection becomes `-m "testcontainer and not kind"`); new job `kubernetes-kind` inserted directly after K3's `kubernetes-render` job (K3 places it before `supply-chain-audit:`, HEAD :1093); `ci.yaml:1335-1344` (`ci-success.needs`) and the `Check all jobs passed` script (:1347-1385)
- Modify: `tests/unit/cicd/test_state_engine_ci_selection.py:211-248,268` (the testcontainer override pins)
- Modify: `docs/plans/2026-09-13-kubernetes-platform-facts.md` (K0's document; Step 6 fills the two slots K0 reserves for this task: the §2.5 `[K4]` row and `### 5.1 Kind lane wall time` (tagged `[K4]`))
- Test: `tests/unit/deployment/test_kubernetes_bundle.py` (K3's pin module: `RESULT_GATED_JOBS` gains `"kubernetes-kind"`; `KIND_VERSION`, `KIND_SHA256` and four tests are added)

**Interfaces:**
- Consumes:
  - K1's base: Deployment `elspeth-web` (pod label `app.kubernetes.io/name: elspeth-web`, pod-template annotation `elspeth.io/revision: REPLACE_PER_ROLLOUT`, `envFrom` `elspeth-web-config` + `elspeth-web-secrets`), Service `elspeth-web` (ClusterIP, one port named `http`), PVC `elspeth-state`, ConfigMap `elspeth-web-config` (`ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE: REPLACE_PER_ROLLOUT`, `ELSPETH_WEB__DATA_DIR: /mnt/elspeth/data`), Jobs `elspeth-provision-storage` and `elspeth-schema-init` (`ttlSecondsAfterFinished: 600`; schema-init `envFrom` ends with `secretRef: elspeth-schema-owner-secrets`), image `ghcr.io/dta-au/elspeth@sha256:0000000000000000000000000000000000000000000000000000000000000000` (K1's all-zero release-digest placeholder, which this task's overlay `images:` replaces), `deploy/kubernetes/base/bootstrap-roles.sql` creating `elspeth_schema_owner` and `elspeth_runtime` from the `\getenv` variables `ELSPETH_SCHEMA_OWNER_PASSWORD` and `ELSPETH_RUNTIME_PASSWORD`, and `deploy/kubernetes/base/bootstrap-acceptance-roles.sql`, which runs `\ir bootstrap-roles.sql` and then creates `elspeth_runtime_a` and `elspeth_runtime_b` from `ELSPETH_RUNTIME_A_PASSWORD` and `ELSPETH_RUNTIME_B_PASSWORD`. Both run over the pre-existing databases `elspeth_sessions` and `elspeth_landscape`.
  - K2's profile: `GET /api/system/status` reports `deployment_target == "kubernetes"`, `deployment_revision == $ELSPETH_K8S_REVISION`, `deployment_replica == $ELSPETH_K8S_POD_NAME`, and `web_instances.deployment_generation == revision_label == $ELSPETH_K8S_REVISION`, `image_digest == $ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE`.
  - K3's `tests/unit/deployment/test_kubernetes_bundle.py`: `REPO_ROOT`, `CI_WORKFLOW`, `PLATFORM_FACTS = REPO_ROOT / "docs" / "plans" / "2026-09-13-kubernetes-platform-facts.md"`, `KUBECTL_VERSION`, `KUBECTL_SHA256`, `RESULT_GATED_JOBS: tuple[str, ...]`, `_ci_workflow() -> dict`, `_run_text(job: dict) -> str`, `_assert_render_gate(workflow: dict) -> None` (iterates `RESULT_GATED_JOBS`), and the job id `kubernetes-render`. K3's `KUBECTL_PINNED_JOBS` is NOT extended: the kind lane installs kubectl inside the smoke script, and the pin test below binds the script's `KUBECTL_VERSION=`/`KUBECTL_SHA256=` lines to K3's constants instead.
  - K0's facts document `docs/plans/2026-09-13-kubernetes-platform-facts.md`: §1.1 `KIND_VERSION=0.33.0`, `KIND_SHA256=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d`, `KIND_URL=https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64` (plus the `KUBECTL_*` pair K3 already carries); §1.2 `KIND_NODE_IMAGE=kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5`; §1.3 `HARNESS_POSTGRES_IMAGE=postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94`.
- Produces:
  - `tests/testcontainer/deployment/conftest.py`: session fixture `kind_cluster -> KindCluster`; `tests/testcontainer/deployment/kind_harness.py`: the `KindCluster` frozen dataclass with `KindCluster.kubectl(*args: str) -> str`, `KindCluster.kubeconfig: Path` (the cluster's kubeconfig file, constructed by the session fixture and consumed by K5), `KindCluster.node_port(service: str) -> int`, `KindCluster.pod_url(pod: str) -> str` (a per-pod `kubectl port-forward`, bypassing the Service's ClientIP affinity), `KindCluster.database_url(role: str, database: str) -> str` (host-side, NodePort 30432) and `KindCluster.cluster_database_url(role: str, database: str) -> str` (in-cluster host `postgres.default.svc.cluster.local:5432`), `KindCluster.passwords: Mapping[str, str]` keyed by role; module constants `IMAGE = "elspeth-web-test:kind"`, `REPO_ROOT`, `HERE` (the `kubernetes/` manifest directory), `BASE_ROLES_SQL: Path` (`deploy/kubernetes/base/bootstrap-roles.sql`), `ACCEPTANCE_ROLES_SQL: Path` (`deploy/kubernetes/base/bootstrap-acceptance-roles.sql`), `PASSWORD_ENV`, `RUNTIME_SECRETS`; helper `write_env_file(directory: Path, name: str, values: Mapping[str, str]) -> Path`. Test modules import from `tests.testcontainer.deployment.kind_harness` (never from the conftest). K5 and K7 reuse the fixture.
  - Cluster objects the fixture creates before any test runs: Secrets `kind-postgres-credentials`, `elspeth-web-secrets` (role `elspeth_runtime`), `elspeth-web-secrets-a` (`elspeth_runtime_a`), `elspeth-web-secrets-b` (`elspeth_runtime_b`) — all four runtime Secrets share one `ELSPETH_WEB__SECRET_KEY` and one `ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY` so a token minted by any replica validates on every replica — and `elspeth-schema-owner-secrets` (`elspeth_schema_owner`); ConfigMap `kind-postgres-init` (keys `01-databases.sql`, `02-roles.sql`, `bootstrap-roles.sql`, `bootstrap-acceptance-roles.sql`; PostgreSQL init creates all five roles in `PASSWORD_ENV`, `elspeth_runtime_a`/`elspeth_runtime_b` included, with the passwords held in `KindCluster.passwords`); PersistentVolume `elspeth-state-rwx` bound to PVC `elspeth-state`; StatefulSet/Service `postgres` (pod `postgres-0`, NodePort 30432). kind maps host ports 30451 (K4 Service), 30452/30453 (K5's `elspeth-web-a`/`elspeth-web-b`) and 30432.
  - `deploy/kubernetes/overlays/kind-test/` — the shared-state overlay (`sha-kindtest` revision, release `0.8.1+kindtest`, NodePort 30451). K5's `kind-acceptance` overlay is a sibling and consumes the same fixture and Secrets.
  - `scripts/cicd/kubernetes-kind-smoke.sh [extra pytest args]` — installs the pinned kubectl/kind into `${ELSPETH_K8S_TOOLS:-.claude/lanes/k8s/bin}`, exports `ELSPETH_KIND_CLUSTER_NAME` and `KUBECONFIG`, runs `uv run --frozen pytest -q -n 0 -m kind tests/testcontainer/deployment`, writes `${ELSPETH_KIND_LOG_DIR:-.claude/lanes/k8s/logs}/kind-smoke-<timestamp>.log`, prints `exit=<n> wall=<s>s`, dumps cluster state on failure and always deletes the cluster. K5 Step 13 and Step 15 and K7 Step 4 run it.
  - pytest marker `kind` (excluded by the default and Testcontainer selections; selected only by the script); env switch `ELSPETH_CI_KIND_REQUIRED` (missing `kind`/`kubectl`/`docker` → `pytest.fail` instead of `pytest.skip`).
  - CI job id `kubernetes-kind` (`runs-on: ubuntu-24.04`, `timeout-minutes: 60`, no `needs`), gated by name in `ci-success`; module constants `KIND_VERSION`, `KIND_SHA256`, `KIND_SMOKE`, `KIND_CONFIG` in `test_kubernetes_bundle.py`.

- [ ] **Step 1: Write the failing kind proofs.**

```python
# tests/testcontainer/deployment/test_kubernetes_kind.py
"""The shipped Kubernetes base on a kind cluster: two replicas, one RWX share,
one PostgreSQL, a provider-free run (marker ``kind``).

Selected only by ``scripts/cicd/kubernetes-kind-smoke.sh`` and the
``kubernetes-kind`` CI job (``-m kind -n 0``); the default and Testcontainer
selections exclude the marker (pyproject addopts, ci.yaml). The session
fixture in ``conftest.py`` owns the cluster; these tests apply the overlay
and talk HTTP and SQL.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
import yaml

from tests.testcontainer.deployment.kind_harness import REPO_ROOT, KindCluster

pytestmark = pytest.mark.kind

OVERLAY = REPO_ROOT / "deploy" / "kubernetes" / "overlays" / "kind-test"
EXAMPLE = REPO_ROOT / "examples" / "threshold_gate"
JOBS = ("elspeth-provision-storage", "elspeth-schema-init")
# Running only: the two Jobs' pods carry the same app label once they have
# Succeeded, and a field selector on phase excludes them.
WEB_PODS = ("-l", "app.kubernetes.io/name=elspeth-web", "--field-selector", "status.phase=Running")
TERMINAL = {"completed", "completed_with_failures", "failed", "empty", "cancelled"}  # sessions/protocol.py:221
HIGH_VALUE_NAMES = {"Bob", "Diana", "Frank", "Henry"}  # examples/threshold_gate/input.csv rows with amount > 1000


def _wait_ready(url: str, *, timeout: float = 300.0) -> None:
    deadline = time.monotonic() + timeout
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=5.0)
            if response.status_code == 200 and response.json()["ready"] is True:
                return
            last = response.text
        except httpx.HTTPError as exc:
            last = repr(exc)
        time.sleep(3)
    raise AssertionError(f"{url} never reported ready within {timeout}s; last answer: {last}")


def _wait_job(cluster: KindCluster, name: str, *, timeout: float = 600.0) -> None:
    """Complete, or fail NOW with the Job's logs — never sit on a failed Job until the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = json.loads(cluster.kubectl("get", "job", name, "-o", "json"))["status"]
        if status.get("succeeded", 0) >= 1:
            return
        if status.get("failed", 0) >= 1:
            logs = cluster.kubectl("logs", f"job/{name}", "--all-containers", "--tail=100")
            safe = "\n".join(line for line in logs.splitlines() if "postgresql+psycopg://" not in line)
            raise AssertionError(f"job/{name} failed:\n{safe}")
        time.sleep(3)
    raise AssertionError(f"job/{name} did not complete within {timeout}s")


def _running_web_pods(cluster: KindCluster) -> list[str]:
    items = json.loads(cluster.kubectl("get", "pods", *WEB_PODS, "-o", "json"))["items"]
    return sorted(item["metadata"]["name"] for item in items)


def test_two_replicas_register_distinct_members_under_one_generation(kind_cluster: KindCluster) -> None:
    # Server-side admission of the whole rendered set (base + overlay): the kind
    # API server is the only place this plan has one (no clusterless dry-run).
    kind_cluster.kubectl("apply", "--dry-run=server", "-k", str(OVERLAY))
    kind_cluster.kubectl("apply", "-k", str(OVERLAY))
    # provision-storage first: the schema-init doctor probes the share with a
    # temp-file write (web/doctor.py:57-108) and the Job has backoffLimit 0.
    _wait_job(kind_cluster, "elspeth-provision-storage")
    _wait_job(kind_cluster, "elspeth-schema-init")
    kind_cluster.kubectl("rollout", "status", "deployment/elspeth-web", "--timeout=600s")
    _wait_ready(f"http://127.0.0.1:{kind_cluster.node_port('elspeth-web')}/api/ready")

    pods = _running_web_pods(kind_cluster)
    assert len(pods) == 2, pods

    # Instance ids are minted per process, so bind rows to pods through what
    # each pod reports about itself, never through the pod name. The overlay
    # patched BOTH base placeholders: the revision annotation (K2 reads it as
    # ELSPETH_K8S_REVISION) and the release literal in the ConfigMap.
    reported: dict[str, str] = {}
    for pod in pods:
        status = httpx.get(f"{kind_cluster.pod_url(pod)}/api/system/status", timeout=10.0).json()
        assert status["deployment_target"] == "kubernetes", status
        assert status["deployment_revision"] == "sha-kindtest", status
        assert status["deployment_replica"] == pod, status
        reported[pod] = status["instance_id"]
    assert len(set(reported.values())) == 2, reported

    engine = sa.create_engine(kind_cluster.database_url("elspeth_runtime", "elspeth_sessions"))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT instance_id, deployment_generation, revision_label, image_digest, state "
                    "FROM web_instances WHERE lease_expires_at > CURRENT_TIMESTAMP"
                )
            ).all()
    finally:
        engine.dispose()
    assert {row.instance_id for row in rows} == set(reported.values()), rows
    assert {row.deployment_generation for row in rows} == {"sha-kindtest"}, rows
    assert {row.revision_label for row in rows} == {"sha-kindtest"}, rows
    # ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE lands in image_digest via
    # DeploymentMembershipIdentity.image_identity (membership_authority.py:146).
    assert {row.image_digest for row in rows} == {"0.8.1+kindtest"}, rows
    assert {row.state for row in rows} == {"active"}, rows


def _jobs(cluster: KindCluster) -> dict[str, dict]:
    """The lane's Jobs that exist right now, by name.

    A List read, not ``get job <name>``: a Job the TTL controller has reaped is
    simply absent here, where a named get would exit 1 (and
    ``KindCluster.kubectl`` asserts exit 0).
    """
    items = json.loads(cluster.kubectl("get", "jobs", "-o", "json"))["items"]
    return {item["metadata"]["name"]: item for item in items if item["metadata"]["name"] in JOBS}


def test_reapplying_the_overlay_succeeds_while_the_finished_jobs_still_exist(kind_cluster: KindCluster) -> None:
    # Job templates are immutable: an identical re-apply must leave a live
    # finished Job `unchanged` (same uid), never fail with "field is
    # immutable". The base sets ttlSecondsAfterFinished: 600, and the previous
    # test's waits (schema-init <= 600 s, rollout <= 600 s, readiness <= 300 s)
    # can outlast it on a hosted runner, so either Job may already be reaped;
    # a reaped Job is `created` again, which is the redeploy-after-TTL path.
    # Re-running either Job is safe: provision-storage is idempotent (K1's
    # job-provision-storage.yaml) and `doctor deployment --init-schema` leaves
    # a CURRENT schema untouched (web/doctor.py:617-650 on 072141b75: only
    # MISSING/PARTIAL states are initialised). If both Jobs were recreated,
    # wait for them and re-apply at once: schema-init finished seconds earlier,
    # so the second apply is guaranteed to meet a live finished Job.
    unchanged: set[str] = set()
    live: dict[str, str] = {}
    for _attempt in range(2):
        live = {name: job["metadata"]["uid"] for name, job in _jobs(kind_cluster).items()}
        out = kind_cluster.kubectl("apply", "-k", str(OVERLAY))
        after = _jobs(kind_cluster)
        created: list[str] = []
        for name in JOBS:  # provision-storage first: the order the Jobs must finish in
            if f"job.batch/{name} unchanged" in out:
                unchanged.add(name)
                assert name in live, (name, sorted(live), out)
                if name in after:  # absent only if the TTL reaped it after the apply returned
                    assert after[name]["metadata"]["uid"] == live[name], name
                    assert after[name]["spec"]["ttlSecondsAfterFinished"] == 600, name
                    assert after[name]["status"].get("succeeded") == 1, after[name]["status"]
            else:
                assert f"job.batch/{name} created" in out, out
                created.append(name)
        for name in created:
            _wait_job(kind_cluster, name)
        if unchanged:
            break
    assert unchanged, (
        f"no finished Job was live at either re-apply; the immutable-template path went unexercised (live before the last apply: {sorted(live)})"
    )
    kind_cluster.kubectl("rollout", "status", "deployment/elspeth-web", "--timeout=120s")
    assert len(_running_web_pods(kind_cluster)) == 2


def _example_yaml_for_the_import_route(settings_yaml: str) -> str:
    """The example as the CLI runs it, in the shape the import route admits.

    The source path is replaced by the uploaded blob (``source_blob_ids``
    rebinds it: sessions/routes/composer/state.py:437-494); sink paths take
    the ``outputs/<name>`` authoring form the session-scoped sink allowlist
    resolves (paths.py:108-135). The ``landscape`` block needs no edit: the
    importer drops it on purpose because the audit URL is the deployment's
    (composer/yaml_importer.py:84-86). Measured offline on HEAD: the
    rewritten example imports as one ``gate`` node and two outputs at
    ``outputs/normal.csv`` / ``outputs/high_values.csv``.
    """
    doc = yaml.safe_load(settings_yaml)
    for sink in doc["sinks"].values():
        sink["options"]["path"] = f"outputs/{Path(sink['options']['path']).name}"
    return yaml.safe_dump(doc, sort_keys=False)


def test_provider_free_run_completes_and_its_outputs_are_served_by_the_other_replica(kind_cluster: KindCluster) -> None:
    pod_a, pod_b = _running_web_pods(kind_cluster)
    a, b = kind_cluster.pod_url(pod_a), kind_cluster.pod_url(pod_b)
    with httpx.Client(timeout=30.0) as client:
        registered = client.post(
            f"{a}/api/auth/register",
            json={"username": f"kind-{uuid.uuid4().hex[:8]}", "password": "kind-harness-only", "display_name": "kind harness"},
        )
        assert registered.status_code == 200, registered.text
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

        created = client.post(f"{a}/api/sessions", headers=headers, json={"title": "kind provider-free run"})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]

        uploaded = client.post(
            f"{a}/api/sessions/{session_id}/blobs",
            headers=headers,
            files={"file": ("input.csv", (EXAMPLE / "input.csv").read_bytes(), "text/csv")},
        )
        assert uploaded.status_code == 201, uploaded.text
        blob_id = uploaded.json()["id"]

        imported = client.post(
            f"{a}/api/sessions/{session_id}/state/yaml",
            headers=headers,
            json={
                "yaml": _example_yaml_for_the_import_route((EXAMPLE / "settings.yaml").read_text(encoding="utf-8")),
                "source_blob_ids": {"primary": blob_id},
            },
        )
        assert imported.status_code == 200, imported.text

        executed = client.post(f"{a}/api/sessions/{session_id}/execute", headers=headers)
        assert executed.status_code == 202, executed.text
        run_id = executed.json()["run_id"]
        owner_instance = executed.headers["X-Elspeth-Instance"]

        deadline = time.monotonic() + 180
        status: dict = {}
        while time.monotonic() < deadline:
            polled = client.get(f"{a}/api/runs/{run_id}", headers=headers)
            assert polled.status_code == 200, polled.text
            status = polled.json()
            if status["status"] in TERMINAL:
                break
            time.sleep(2)
        assert status["status"] == "completed", status
        assert status["accounting"]["source"]["rows_processed"] == 8, status["accounting"]

        # The OTHER replica serves the manifest (shared Landscape) and the
        # bytes (shared RWX share) of a run it did not execute.
        listed = client.get(f"{b}/api/runs/{run_id}/outputs", headers=headers)
        assert listed.status_code == 200, listed.text
        assert listed.headers["X-Elspeth-Instance"] != owner_instance
        artifacts = {Path(artifact["path_or_uri"]).name: artifact for artifact in listed.json()["artifacts"]}
        assert set(artifacts) == {"normal.csv", "high_values.csv"}, artifacts
        for artifact in artifacts.values():
            assert artifact["storage_kind"] == "sink_file", artifact
            assert artifact["downloadable"] is True, artifact
        content = client.get(
            f"{b}/api/runs/{run_id}/outputs/{artifacts['high_values.csv']['artifact_id']}/content",
            headers=headers,
        )
        assert content.status_code == 200, content.text
        assert content.headers["X-Elspeth-Instance"] != owner_instance
        names = {line.split(",")[1] for line in content.text.strip().splitlines()[1:]}
        assert names == HIGH_VALUE_NAMES, content.text
```

- [ ] **Step 2: Register the marker, exclude it from the default selection, and run the new file to verify it fails for the right reason.**

```toml
# pyproject.toml — replace line 454
    "-m", "not slow and not stress and not performance and not testcontainer and not kind and not live_provider",  # Remote-provider evidence is separately operator-gated
```

```toml
# pyproject.toml — insert after line 475 (the `testcontainer:` marker line)
    "kind: marks the Docker-backed kind cluster lane (deploy/kubernetes on a kind cluster); excluded by the default and Testcontainer selections and selected only by scripts/cicd/kubernetes-kind-smoke.sh (-m kind -n 0)",
```

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/testcontainer/deployment --collect-only -q -n 0 > /tmp/klane-k4-step2-default.log 2>&1; echo exit=$?; tail -3 /tmp/klane-k4-step2-default.log`
Expected: the last line reads `3 deselected` and no test id is listed (the default `-m` excludes the marker).

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/testcontainer/deployment -m kind -n 0 > /tmp/klane-k4-step2-kind.log 2>&1; echo exit=$?; tail -5 /tmp/klane-k4-step2-kind.log`
Expected: exit 2 (collection interrupted): `ModuleNotFoundError: No module named 'tests.testcontainer.deployment.kind_harness'` — the harness module and the conftest do not exist yet. (`tests/__init__.py` makes `tests` a regular package and PEP 420 makes `tests.testcontainer.deployment` an importable namespace portion beneath it, the same path `tests/testcontainer/web/conftest.py:16` takes for `tests.helpers.postgres_target`; measured: `import tests.testcontainer.web.conftest` resolves from the repo root.)

- [ ] **Step 3: Write the harness module and the session fixture.**

The cluster handle lives in an ordinary module so test files import it by
name; the conftest holds only the fixture and the lane's admission rule.

```python
# tests/testcontainer/deployment/kind_harness.py
"""The handle the ``kind``-marked deployment proofs hold on their cluster.

``KindCluster`` is constructed once per session by ``conftest.kind_cluster``
and does three things for the tests: runs ``kubectl`` against the session's
kubeconfig, builds database URLs for the roles the fixture minted (with
``sqlalchemy.engine.URL.create`` — no credential-shaped literal anywhere), and
addresses one pod directly through a ``kubectl port-forward`` so a test can
choose which replica answers despite the Service's ClientIP affinity.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy.engine import URL

HERE = Path(__file__).parent / "kubernetes"
REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_ROLES_SQL = REPO_ROOT / "deploy" / "kubernetes" / "base" / "bootstrap-roles.sql"
# Creates elspeth_runtime_a / elspeth_runtime_b after \ir-including BASE_ROLES_SQL (K1); the
# kind-acceptance overlay's Secrets elspeth-web-secrets-a|b name these roles.
ACCEPTANCE_ROLES_SQL = REPO_ROOT / "deploy" / "kubernetes" / "base" / "bootstrap-acceptance-roles.sql"
IMAGE = "elspeth-web-test:kind"
POSTGRES_HOST_IN_CLUSTER = "postgres.default.svc.cluster.local"
POSTGRES_NODE_PORT = 30432
# role -> the env variable that sets its password: postgres via the image's POSTGRES_PASSWORD,
# the next two via BASE_ROLES_SQL's \getenv, the last two via ACCEPTANCE_ROLES_SQL's \getenv.
PASSWORD_ENV: Mapping[str, str] = {
    "postgres": "POSTGRES_PASSWORD",
    "elspeth_schema_owner": "ELSPETH_SCHEMA_OWNER_PASSWORD",
    "elspeth_runtime": "ELSPETH_RUNTIME_PASSWORD",
    "elspeth_runtime_a": "ELSPETH_RUNTIME_A_PASSWORD",
    "elspeth_runtime_b": "ELSPETH_RUNTIME_B_PASSWORD",
}
# Secret name -> the runtime role whose URLs it carries (K4's base uses the
# first; K5's kind-acceptance overlay binds elspeth-web-a|b to the other two).
RUNTIME_SECRETS: Mapping[str, str] = {
    "elspeth-web-secrets": "elspeth_runtime",
    "elspeth-web-secrets-a": "elspeth_runtime_a",
    "elspeth-web-secrets-b": "elspeth_runtime_b",
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_env_file(directory: Path, name: str, values: Mapping[str, str]) -> Path:
    """A 0600 KEY=VALUE file for ``kubectl create secret --from-env-file`` (values never touch argv)."""
    path = directory / f"{name}.env"
    path.touch(mode=0o600)
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    return path


@dataclass(frozen=True)
class KindCluster:
    """Handle on the session's cluster; ``conftest.kind_cluster`` constructs it."""

    cluster_name: str
    kubeconfig: Path
    passwords: Mapping[str, str]
    _forwards: dict[str, tuple[subprocess.Popen[bytes], int]] = field(default_factory=dict, repr=False, compare=False)

    def kubectl(self, *args: str) -> str:
        result = subprocess.run(
            ["kubectl", "--kubeconfig", str(self.kubeconfig), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"kubectl {' '.join(args)} failed (exit={result.returncode}):\n{result.stderr}"
        return result.stdout

    def node_port(self, service: str) -> int:
        return int(json.loads(self.kubectl("get", "svc", service, "-o", "json"))["spec"]["ports"][0]["nodePort"])

    def database_url(self, role: str, database: str) -> str:
        """Host-side URL for ``role`` on the harness PostgreSQL (kind maps NodePort 30432)."""
        return self._url(role, database, host="127.0.0.1", port=POSTGRES_NODE_PORT)

    def cluster_database_url(self, role: str, database: str) -> str:
        """In-cluster URL for ``role``: what the Secrets carry."""
        return self._url(role, database, host=POSTGRES_HOST_IN_CLUSTER, port=5432)

    def _url(self, role: str, database: str, *, host: str, port: int) -> str:
        url = URL.create("postgresql+psycopg", username=role, password=self.passwords[role], host=host, port=port, database=database)
        return url.render_as_string(hide_password=False)

    def pod_url(self, pod: str) -> str:
        """Address one pod directly, bypassing the Service's ClientIP affinity.

        Starts ``kubectl port-forward`` on first use, waits until /api/health
        answers through it, and keeps it for the session; teardown closes it.
        """
        if pod not in self._forwards:
            port = free_port()
            proc = subprocess.Popen(
                ["kubectl", "--kubeconfig", str(self.kubeconfig), "port-forward", f"pod/{pod}", f"{port}:8451"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2.0).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(1)
            else:
                proc.terminate()
                raise AssertionError(f"port-forward to pod/{pod} never answered /api/health")
            self._forwards[pod] = (proc, port)
        return f"http://127.0.0.1:{self._forwards[pod][1]}"

    def close_forwards(self) -> None:
        for proc, _port in self._forwards.values():
            proc.terminate()
        self._forwards.clear()
```

```python
# tests/testcontainer/deployment/conftest.py
"""One kind cluster per pytest session for the ``kind``-marked deployment proofs.

The fixture owns the whole harness so the tests only apply overlays and talk
HTTP and SQL: tool presence (skip locally, fail under ELSPETH_CI_KIND_REQUIRED),
``kind create cluster`` from ``kubernetes/kind-config.yaml``, ``docker build`` +
``kind load`` of the tree under test as ``elspeth-web-test:kind``, the hostPath
ReadWriteMany PersistentVolume, PostgreSQL 16 with the two databases and the
five roles the base and the acceptance overlay bind to
(``kubernetes/02-roles.sql`` runs ``deploy/kubernetes/base/bootstrap-acceptance-roles.sql``,
which ``\ir``-includes ``bootstrap-roles.sql``), the runtime and schema-owner
Secrets, and teardown. Every credential is minted per session: nothing
credential-shaped lives in a tracked file.
"""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from xdist import is_xdist_worker

from tests.testcontainer.deployment.kind_harness import (
    ACCEPTANCE_ROLES_SQL,
    BASE_ROLES_SQL,
    HERE,
    IMAGE,
    PASSWORD_ENV,
    REPO_ROOT,
    RUNTIME_SECRETS,
    KindCluster,
    write_env_file,
)

_SEQUENTIAL_COMMAND = "scripts/cicd/kubernetes-kind-smoke.sh (uv run --frozen pytest -q -n 0 -m kind tests/testcontainer/deployment)"


def _require_kind_lane(request: pytest.FixtureRequest) -> None:
    """Reject xdist workers, and the absence of the lane's tools: skip locally, fail in CI."""
    if is_xdist_worker(request) or os.environ.get("PYTEST_XDIST_WORKER") is not None:
        raise pytest.UsageError(f"The kind lane owns one cluster per session and must run serially: {_SEQUENTIAL_COMMAND}")
    for tool in ("kind", "kubectl", "docker"):
        if shutil.which(tool) is not None:
            continue
        if os.environ.get("ELSPETH_CI_KIND_REQUIRED"):
            pytest.fail(f"{tool} is required by ELSPETH_CI_KIND_REQUIRED but is not installed")
        pytest.skip(f"{tool} is not installed: the kind lane cannot run here ({_SEQUENTIAL_COMMAND} installs the pinned tools)")


@pytest.fixture(scope="session")
def kind_cluster(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[KindCluster]:
    _require_kind_lane(request)
    work = tmp_path_factory.mktemp("kind")
    # The smoke script exports both so its failure trap can inspect and delete
    # the cluster; a bare pytest run gets a throwaway name and kubeconfig.
    name = os.environ.get("ELSPETH_KIND_CLUSTER_NAME") or f"elspeth-{uuid.uuid4().hex[:8]}"
    kubeconfig = Path(os.environ.get("KUBECONFIG") or work / "kubeconfig")
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    passwords = {role: secrets.token_urlsafe(24) for role in PASSWORD_ENV}
    secret_key = secrets.token_hex(32)  # 64 chars: above the 32-byte floor for non-local hosts (config.py:46)
    signing_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")  # the `openssl rand -base64 32` recipe (config.py:594)
    cluster = KindCluster(cluster_name=name, kubeconfig=kubeconfig, passwords=passwords)

    subprocess.run(
        ["kind", "create", "cluster", "--name", name, "--config", str(HERE / "kind-config.yaml"), "--wait", "120s"],
        check=True,
        env=env,
    )
    try:
        subprocess.run(["docker", "build", "-t", IMAGE, str(REPO_ROOT)], check=True)
        subprocess.run(["kind", "load", "docker-image", IMAGE, "--name", name], check=True, env=env)

        postgres_env = write_env_file(work, "postgres", {PASSWORD_ENV[role]: password for role, password in passwords.items()})
        cluster.kubectl("create", "secret", "generic", "kind-postgres-credentials", f"--from-env-file={postgres_env}")
        # 02-roles.sql runs bootstrap-acceptance-roles.sql, which \ir-includes
        # bootstrap-roles.sql: all five roles the base and the kind-acceptance
        # overlay bind to (postgresql.yaml mounts the two shipped files under elspeth/).
        cluster.kubectl(
            "create", "configmap", "kind-postgres-init",
            f"--from-file=01-databases.sql={HERE / '01-databases.sql'}",
            f"--from-file=02-roles.sql={HERE / '02-roles.sql'}",
            f"--from-file=bootstrap-roles.sql={BASE_ROLES_SQL}",
            f"--from-file=bootstrap-acceptance-roles.sql={ACCEPTANCE_ROLES_SQL}",
        )
        # One SECRET_KEY and one signing key across every runtime Secret: a
        # token minted by any replica must validate on every replica.
        shared_keys = {"ELSPETH_WEB__SECRET_KEY": secret_key, "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY": signing_key}
        for secret_name, role in RUNTIME_SECRETS.items():
            values = {
                "ELSPETH_WEB__SESSION_DB_URL": cluster.cluster_database_url(role, "elspeth_sessions"),
                "ELSPETH_WEB__LANDSCAPE_URL": cluster.cluster_database_url(role, "elspeth_landscape"),
                **shared_keys,
            }
            cluster.kubectl("create", "secret", "generic", secret_name, f"--from-env-file={write_env_file(work, secret_name, values)}")
        owner = {
            "ELSPETH_WEB__SESSION_DB_URL": cluster.cluster_database_url("elspeth_schema_owner", "elspeth_sessions"),
            "ELSPETH_WEB__LANDSCAPE_URL": cluster.cluster_database_url("elspeth_schema_owner", "elspeth_landscape"),
        }
        cluster.kubectl(
            "create", "secret", "generic", "elspeth-schema-owner-secrets",
            f"--from-env-file={write_env_file(work, 'elspeth-schema-owner-secrets', owner)}",
        )

        cluster.kubectl("apply", "-f", str(HERE / "pv-rwx-hostpath.yaml"), "-f", str(HERE / "postgresql.yaml"))
        # Ready only once the init scripts have run: the readiness probe is a
        # TCP pg_isready, and the entrypoint's init-time server listens on the
        # unix socket alone.
        cluster.kubectl("rollout", "status", "statefulset/postgres", "--timeout=300s")
        yield cluster
    finally:
        cluster.close_forwards()
        subprocess.run(["kind", "delete", "cluster", "--name", name], check=False, env=env)
```

- [ ] **Step 4: Write the harness manifests and the kind-test overlay.**

```yaml
# tests/testcontainer/deployment/kubernetes/kind-config.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    # KIND_NODE_IMAGE from docs/plans/2026-09-13-kubernetes-platform-facts.md
    # §1.2 (kind v0.33.0's pre-built node for the pinned kubectl v1.37.0, by
    # digest). tests/unit/deployment/test_kubernetes_bundle.py binds this line
    # to that document.
    image: kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
    extraPortMappings:
      - containerPort: 30451   # Service elspeth-web   (kind-test overlay, this task)
        hostPort: 30451
      - containerPort: 30452   # Service elspeth-web-a (kind-acceptance overlay, K5)
        hostPort: 30452
      - containerPort: 30453   # Service elspeth-web-b (kind-acceptance overlay, K5)
        hostPort: 30453
      - containerPort: 30432   # Service postgres      (harness)
        hostPort: 30432
```

```yaml
# tests/testcontainer/deployment/kubernetes/pv-rwx-hostpath.yaml
# A single-node hostPath volume satisfies ReadWriteMany for the harness (K0
# measured it); the default `standard` StorageClass is ReadWriteOnce and must
# not bind the base's claim, so the overlay pins storageClassName "" and this
# volumeName on the PVC.
apiVersion: v1
kind: PersistentVolume
metadata:
  name: elspeth-state-rwx
spec:
  capacity:
    storage: 5Gi
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Delete
  storageClassName: ""
  claimRef:
    name: elspeth-state
    namespace: default
  hostPath:
    path: /var/elspeth-state
    type: DirectoryOrCreate
```

```sql
-- tests/testcontainer/deployment/kubernetes/01-databases.sql
-- Harness only. The two databases the base expects, created by the bootstrap
-- superuser; 02-roles.sql (next in name order) runs the shipped bootstrap
-- files, which hand them to elspeth_schema_owner and create the runtime roles
-- from the \getenv passwords in the pod environment.
CREATE DATABASE elspeth_sessions;
CREATE DATABASE elspeth_landscape;
```

```sql
-- tests/testcontainer/deployment/kubernetes/02-roles.sql
-- Harness only. The postgres image runs every top-level *.sql in
-- /docker-entrypoint-initdb.d in name order and ignores directories, so the
-- two shipped bootstrap files are mounted one level down (postgresql.yaml
-- ConfigMap items) and run exactly once from here:
-- deploy/kubernetes/base/bootstrap-acceptance-roles.sql includes
-- bootstrap-roles.sql itself (\ir, relative to its own directory), then
-- creates elspeth_runtime_a and elspeth_runtime_b for the kind-acceptance
-- overlay. Passwords come from the kind-postgres-credentials Secret
-- (ELSPETH_RUNTIME_A_PASSWORD / ELSPETH_RUNTIME_B_PASSWORD).
\ir elspeth/bootstrap-acceptance-roles.sql
```

```yaml
# tests/testcontainer/deployment/kubernetes/postgresql.yaml
# Throwaway PostgreSQL for the kind lane: no persistent volume, credentials
# from the Secret the fixture mints, init scripts from the ConfigMap the
# fixture builds (01-databases.sql and 02-roles.sql here, plus
# deploy/kubernetes/base/bootstrap-roles.sql and bootstrap-acceptance-roles.sql under elspeth/).
apiVersion: v1
kind: Service
metadata:
  name: postgres
spec:
  type: NodePort
  selector:
    app: kind-postgres
  ports:
    - name: postgres
      port: 5432
      targetPort: 5432
      nodePort: 30432
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
spec:
  serviceName: postgres
  replicas: 1
  selector:
    matchLabels:
      app: kind-postgres
  template:
    metadata:
      labels:
        app: kind-postgres
    spec:
      containers:
        - name: postgres
          # HARNESS_POSTGRES_IMAGE from docs/plans/2026-09-13-kubernetes-platform-facts.md §1.3; psql 16 supports \getenv.
          image: postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94
          envFrom:
            - secretRef:
                name: kind-postgres-credentials
          ports:
            - name: postgres
              containerPort: 5432
          readinessProbe:
            # TCP on purpose: during init the entrypoint's temporary server
            # listens on the unix socket only, so this stays red until the
            # init scripts have run and the real server is up.
            exec:
              command: ["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]
            periodSeconds: 2
            failureThreshold: 90
          volumeMounts:
            - name: init
              mountPath: /docker-entrypoint-initdb.d
              readOnly: true
      volumes:
        - name: init
          configMap:
            name: kind-postgres-init
            # 02-roles.sql \ir-includes the shipped files from elspeth/; a
            # directory at the top level is skipped by the image entrypoint,
            # so each bootstrap file runs exactly once.
            items:
              - key: 01-databases.sql
                path: 01-databases.sql
              - key: 02-roles.sql
                path: 02-roles.sql
              - key: bootstrap-roles.sql
                path: elspeth/bootstrap-roles.sql
              - key: bootstrap-acceptance-roles.sql
                path: elspeth/bootstrap-acceptance-roles.sql
```

```yaml
# deploy/kubernetes/overlays/kind-test/kustomization.yaml
# K4 shared-state proof: the base exactly as shipped (one Deployment,
# replicas 2, ClientIP affinity) on a kind cluster, reachable on NodePort
# 30451. No Secret is listed here — the harness fixture creates
# elspeth-web-secrets and elspeth-schema-owner-secrets with per-session values
# (tests/testcontainer/deployment/conftest.py), exactly as an operator does,
# and no tracked file carries a credential. The two REPLACE_PER_ROLLOUT
# placeholders the base ships are patched by value, so the render carries
# neither; tests/testcontainer/deployment/test_kubernetes_kind.py proves both
# replacements through /api/system/status and web_instances.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
images:
  # The base pins the release by digest; the harness loads a tag into kind
  # (`kind load docker-image elspeth-web-test:kind`), and kustomize rewrites
  # every reference to the base image, the schema-init Job's included.
  - name: ghcr.io/dta-au/elspeth
    newName: elspeth-web-test
    newTag: kind
patches:
  - target:
      kind: Deployment
      name: elspeth-web
    patch: |-
      - op: replace
        path: /spec/template/metadata/annotations/elspeth.io~1revision
        value: sha-kindtest
  - target:
      kind: ConfigMap
      name: elspeth-web-config
    patch: |-
      - op: replace
        path: /data/ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE
        value: 0.8.1+kindtest
  - target:
      kind: Service
      name: elspeth-web
    patch: |-
      - op: replace
        path: /spec/type
        value: NodePort
      - op: add
        path: /spec/ports/0/nodePort
        value: 30451
  - target:
      kind: PersistentVolumeClaim
      name: elspeth-state
    patch: |-
      - op: add
        path: /spec/storageClassName
        value: ""
      - op: add
        path: /spec/volumeName
        value: elspeth-state-rwx
```

Prove the overlay renders offline the way K3's render job will render it:

Run: `cd "$(git rev-parse --show-toplevel)" && PATH="$PWD/.claude/lanes/k8s/bin:$PATH" kubectl kustomize deploy/kubernetes/overlays/kind-test > /tmp/klane-k4-step4-render.log 2>&1; echo exit=$?; grep -c '^kind:' /tmp/klane-k4-step4-render.log; grep -n 'REPLACE_PER_ROLLOUT\|sha-kindtest\|0.8.1+kindtest\|nodePort\|elspeth-web-test:kind' /tmp/klane-k4-step4-render.log`
Expected: exit 0; `6` objects (ConfigMap, Deployment, Job, Job, PersistentVolumeClaim, Service); no `REPLACE_PER_ROLLOUT` line; one `sha-kindtest`, one `0.8.1+kindtest`, one `nodePort: 30451`, and every `image:` line for the application reads `elspeth-web-test:kind`. (The pinned kubectl is not on PATH until the smoke script in the next step has installed it once; run that step's install first if `kubectl` is missing.)

- [ ] **Step 5: Write the smoke script (one code path for CI and the desk).**

```bash
#!/usr/bin/env bash
# scripts/cicd/kubernetes-kind-smoke.sh — the kind lane, for CI and operators alike.
#
# Installs kubectl and kind by checksum (the K0 pins below; the same values
# docs/plans/2026-09-13-kubernetes-platform-facts.md records and
# tests/unit/deployment/test_kubernetes_bundle.py binds), then runs the
# `kind`-marked proofs serially: the fixture in
# tests/testcontainer/deployment/conftest.py builds and loads the image,
# creates the cluster, PostgreSQL and the Secrets, and deletes the cluster at
# teardown. The kubernetes-kind CI job calls exactly this script, so a green
# desk run and a green CI run are the same run. On failure the trap dumps
# pod state and the web logs (database URLs filtered) and deletes the cluster
# if the fixture could not.
#
# Usage: scripts/cicd/kubernetes-kind-smoke.sh [extra pytest args]
#   ELSPETH_K8S_TOOLS    where the pinned binaries live  (default .claude/lanes/k8s/bin, gitignored)
#   ELSPETH_KIND_LOG_DIR where the pytest log is written (default .claude/lanes/k8s/logs, gitignored)
set -Eeuo pipefail

# K0 pins (docs/plans/2026-09-13-kubernetes-platform-facts.md §1.1 CLI binaries).
# KUBECTL_* must equal the values the `test` and `kubernetes-render` jobs carry.
KUBECTL_VERSION=1.37.0
KUBECTL_SHA256=6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f
KIND_VERSION=0.33.0
KIND_SHA256=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
TOOLS="${ELSPETH_K8S_TOOLS:-$REPO_ROOT/.claude/lanes/k8s/bin}"
LOG_DIR="${ELSPETH_KIND_LOG_DIR:-$REPO_ROOT/.claude/lanes/k8s/logs}"
mkdir -p "$TOOLS" "$LOG_DIR"

install_pinned() {
    # install_pinned <name> <sha256> <url>: download once, verify always.
    local name="$1" sha="$2" url="$3" target="$TOOLS/$1"
    if [[ -x "$target" ]] && printf '%s  %s\n' "$sha" "$target" | sha256sum -c - >/dev/null 2>&1; then
        return
    fi
    curl -fsSLo "$target.download" "$url"
    printf '%s  %s\n' "$sha" "$target.download" | sha256sum -c -
    install -m 0755 "$target.download" "$target"
    rm -f "$target.download"
}
install_pinned kubectl "$KUBECTL_SHA256" "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
install_pinned kind "$KIND_SHA256" "https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-linux-amd64"
export PATH="$TOOLS:$PATH"
kubectl version --client
kind version
docker version --format 'docker {{.Server.Version}}'

# Exported for the fixture, so this trap can find what it created.
export ELSPETH_KIND_CLUSTER_NAME="elspeth-kind-$(date +%s)-$$"
export KUBECONFIG="$LOG_DIR/kubeconfig-$ELSPETH_KIND_CLUSTER_NAME"

cleanup() {
    local status=$?
    if [[ $status -ne 0 ]]; then
        echo "kind smoke failed (exit=$status); cluster state follows" >&2
        kubectl get pods -o wide >&2 || true
        kubectl describe deployment/elspeth-web >&2 || true
        kubectl logs -l app.kubernetes.io/name=elspeth-web --all-containers --tail=200 2>&1 \
            | grep -v -i 'postgresql+psycopg://' >&2 || true
    fi
    kind delete cluster --name "$ELSPETH_KIND_CLUSTER_NAME" >/dev/null 2>&1 || true
    rm -f "$KUBECONFIG"
    exit "$status"
}
trap cleanup EXIT

cd "$REPO_ROOT"
log="$LOG_DIR/kind-smoke-$(date +%Y%m%dT%H%M%S).log"
start=$(date +%s)
set +e
CI=1 uv run --frozen pytest -q -n 0 -m kind tests/testcontainer/deployment "$@" > "$log" 2>&1
status=$?
set -e
echo "exit=$status wall=$(( $(date +%s) - start ))s log=$log"
tail -30 "$log"
exit "$status"
```

Then `chmod 0755 scripts/cicd/kubernetes-kind-smoke.sh`. The four pin
assignments and the download URLs are K0's §1.1 literals (`KUBECTL_URL`,
`KIND_URL`), and `kind-config.yaml` carries §1.2's `KIND_NODE_IMAGE`; if K0's
re-fetch moved any of them, the facts document is the authority and every copy
here changes with it. A wrong sha makes `sha256sum -c` refuse the download, and
Step 7's `_assert_kind_lane_pins` binds the script, the kind-config and the
facts document together.

- [ ] **Step 6: Run the smoke twice and record the wall time.**

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/cicd/kubernetes-kind-smoke.sh > /tmp/klane-k4-step6-run1.log 2>&1; echo exit=$?; grep -E '^exit=|passed|failed|error' /tmp/klane-k4-step6-run1.log; PATH="$PWD/.claude/lanes/k8s/bin:$PATH" kind get clusters`
Expected: `exit=0 wall=<n>s`, `3 passed`, and `kind get clusters` prints nothing (the cluster is gone).

Run it a second time with the same command into `/tmp/klane-k4-step6-run2.log` and expect the same three lines: the harness is repeatable from a clean box (the second run reuses the checksum-verified tools and the Docker layer cache, so its wall time is the steady-state number).

If the first test fails in `_wait_job(kind_cluster, "elspeth-schema-init")` with the doctor reporting a red `data_dir`/`blob`/`payload_store` directory check while `elspeth-provision-storage` is still pulling `busybox`, that is a K1 ordering defect (both Jobs are created by one `kubectl apply -k` and the schema-init doctor probes the share); report it against K1 rather than patching the overlay.

Write the measured wall times into the two slots K0 reserved in the facts document (do not add a new section):

1. In §2.5, replace the wall-time cell of the `[K4]` row (the cell that begins `written by Task K4 Step 6`) with `WALL_FROM_RUN_1 (cold) / WALL_FROM_RUN_2 (warm)`.
2. Under the heading `### 5.1 Kind lane wall time` (tagged `[K4]`), replace K0's placeholder paragraph with:

```markdown
Measured on the development box on the day the lane landed
(`scripts/cicd/kubernetes-kind-smoke.sh`, two consecutive runs):

| Run | `docker build` cached | Wall time |
|---|---|---|
| first (cold tools, cold layers) | no | WALL_FROM_RUN_1 |
| second (warm) | yes | WALL_FROM_RUN_2 |

`kubernetes-kind` runs with `timeout-minutes: 60` (DECISIONS K9); revisit the
budget if a hosted-runner run exceeds half of it.
```

Replace `WALL_FROM_RUN_1` and `WALL_FROM_RUN_2` in both slots with the two printed `wall=` figures before committing; the document is K0's, and these two slots are the only edits this task makes to it.

- [ ] **Step 7: Write the failing CI pin tests.**

Append to `tests/unit/deployment/test_kubernetes_bundle.py`. K3's module already carries `REPO_ROOT`, `CI_WORKFLOW`, `PLATFORM_FACTS`, `KUBECTL_VERSION`, `KUBECTL_SHA256`, `RESULT_GATED_JOBS`, `_ci_workflow()`, `_run_text()`, `_assert_render_gate()` and imports `subprocess`, `Path`, `pytest`, `yaml` and `Callable`; add `import os`, `import re` and `import tomllib` to its import block, and change K3's tuple so `ci-success` is checked for the kind job too:

```python
# tests/unit/deployment/test_kubernetes_bundle.py — replace K3's RESULT_GATED_JOBS line
RESULT_GATED_JOBS: tuple[str, ...] = ("kubernetes-render", "kubernetes-kind")
```

```python
# tests/unit/deployment/test_kubernetes_bundle.py — directly after RESULT_GATED_JOBS
# The kind pin K0 recorded in PLATFORM_FACTS §1.1 (CLI binaries). The kind
# lane installs both tools inside scripts/cicd/kubernetes-kind-smoke.sh (one
# code path for CI and the desk), so its kubectl pin is bound through the
# script text, not through a workflow step, and KUBECTL_PINNED_JOBS stays as
# K3 left it.
KIND_VERSION = "0.33.0"
KIND_SHA256 = "aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d"
KIND_SMOKE = REPO_ROOT / "scripts" / "cicd" / "kubernetes-kind-smoke.sh"
KIND_CONFIG = REPO_ROOT / "tests" / "testcontainer" / "deployment" / "kubernetes" / "kind-config.yaml"
KIND_NODE_IMAGE_RE = re.compile(r"^kindest/node:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")
PYPROJECT = REPO_ROOT / "pyproject.toml"
DEFAULT_MARKER_EXPRESSION = "not slow and not stress and not performance and not testcontainer and not kind and not live_provider"
TESTCONTAINER_MARKER_EXPRESSION = '-m "testcontainer and not kind"'
```

```python
# tests/unit/deployment/test_kubernetes_bundle.py — append at the end


# ---------------------------------------------------------------------------
# The kind lane: tool pins, marker isolation, the CI job (Task K4)
# ---------------------------------------------------------------------------


def _assert_kind_lane_pins(smoke: str, kind_config: dict, facts: str) -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", KIND_SHA256), "KIND_SHA256 is not a sha256 (K0 §1.1)"
    assert KIND_SHA256 in facts, "the platform facts document does not record this kind sha256"
    assert f"`v{KIND_VERSION}`" in facts, "the platform facts document does not record this kind version"
    for name, value in (
        ("KUBECTL_VERSION", KUBECTL_VERSION),
        ("KUBECTL_SHA256", KUBECTL_SHA256),
        ("KIND_VERSION", KIND_VERSION),
        ("KIND_SHA256", KIND_SHA256),
    ):
        assert f"\n{name}={value}\n" in smoke, f"the smoke script's {name} drifted from the pin"
    assert "sha256sum -c -" in smoke, "the smoke script does not checksum-verify its downloads"
    assert "-m kind" in smoke and "-n 0" in smoke, "the smoke script must select the kind marker serially"
    node_image = kind_config["nodes"][0]["image"]
    assert KIND_NODE_IMAGE_RE.fullmatch(node_image), f"kind-config node image is not pinned by digest: {node_image}"
    assert node_image in facts, "the platform facts document does not record this kindest/node image"


def _assert_kind_marker_isolation(pyproject: dict, workflow: dict, sources: dict[str, str]) -> None:
    options = pyproject["tool"]["pytest"]["ini_options"]
    assert any(marker.startswith("kind:") for marker in options["markers"]), "the kind marker is not registered"
    addopts = options["addopts"]
    expressions = [addopts[index + 1] for index, token in enumerate(addopts) if token == "-m"]
    assert expressions == [DEFAULT_MARKER_EXPRESSION], "the default selection does not exclude the kind marker"
    testcontainer = _run_text(workflow["jobs"]["testcontainer"])
    assert TESTCONTAINER_MARKER_EXPRESSION in testcontainer, "the Testcontainer job does not exclude the kind marker"
    assert "tests/testcontainer/deployment" not in testcontainer, "the Testcontainer job names the kind lane"
    for path, source in sources.items():
        assert "pytestmark = pytest.mark.kind" in source, f"{path} does not carry the kind marker"
    conftest = (REPO_ROOT / "tests" / "testcontainer" / "deployment" / "conftest.py").read_text(encoding="utf-8")
    assert "ELSPETH_CI_KIND_REQUIRED" in conftest and "pytest.fail(" in conftest and "pytest.skip(" in conftest


def _assert_kind_job(workflow: dict) -> None:
    job = workflow["jobs"]["kubernetes-kind"]
    assert "needs" not in job, "kubernetes-kind must not wait on static-analysis (operator ruling 2026-09-05)"
    assert "container" not in job, "kubernetes-kind needs a Docker daemon, which container jobs do not have"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 60
    smoke_steps = [step for step in job["steps"] if "scripts/cicd/kubernetes-kind-smoke.sh" in str(step.get("run", ""))]
    assert len(smoke_steps) == 1, "kubernetes-kind must run the smoke script exactly once"
    (smoke,) = smoke_steps
    assert smoke.get("env", {}).get("ELSPETH_CI_KIND_REQUIRED") == "1", "the kind lane must fail loudly, not skip"
    uploads = [step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    assert any(step.get("if") == "always()" for step in uploads), "the kind log is not uploaded on failure"


def _kind_sources() -> dict[str, str]:
    directory = REPO_ROOT / "tests" / "testcontainer" / "deployment"
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(directory.glob("test_*.py"))}


def test_kind_lane_pins_kind_and_the_node_image_from_the_facts_document() -> None:
    assert os.access(KIND_SMOKE, os.X_OK), "the smoke script is not executable"
    _assert_kind_lane_pins(
        KIND_SMOKE.read_text(encoding="utf-8"),
        yaml.safe_load(KIND_CONFIG.read_text(encoding="utf-8")),
        PLATFORM_FACTS.read_text(encoding="utf-8"),
    )


def test_kind_marker_is_registered_and_selected_only_by_the_kind_lane() -> None:
    sources = _kind_sources()
    assert sources, "no kind-lane test module found"
    _assert_kind_marker_isolation(tomllib.loads(PYPROJECT.read_text(encoding="utf-8")), _ci_workflow(), sources)


def test_ci_kind_job_runs_the_smoke_on_a_docker_capable_runner_and_gates_ci_success() -> None:
    workflow = _ci_workflow()
    _assert_kind_job(workflow)
    _assert_render_gate(workflow)  # RESULT_GATED_JOBS now carries kubernetes-kind: needs + result check by name


def _drop_kind_result_check(workflow: dict) -> None:
    step = workflow["jobs"]["ci-success"]["steps"][0]
    step["run"] = step["run"].replace("needs.kubernetes-kind.result", "needs.kubernetes-kin.result")


def _drop_kind_from_needs(workflow: dict) -> None:
    workflow["jobs"]["ci-success"]["needs"].remove("kubernetes-kind")


def _let_the_kind_lane_skip(workflow: dict) -> None:
    for step in workflow["jobs"]["kubernetes-kind"]["steps"]:
        if "kubernetes-kind-smoke.sh" in str(step.get("run", "")):
            step.pop("env")


def _move_kind_job_into_a_container(workflow: dict) -> None:
    workflow["jobs"]["kubernetes-kind"]["container"] = {"image": "python:3.13-bookworm"}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_drop_kind_result_check, "never checks needs.kubernetes-kind.result"),
        (_drop_kind_from_needs, "is not in ci-success.needs"),
        (_let_the_kind_lane_skip, "must fail loudly, not skip"),
        (_move_kind_job_into_a_container, "needs a Docker daemon"),
    ],
    ids=["result-check-dropped", "needs-entry-dropped", "switch-dropped", "container-job"],
)
def test_kind_job_gate_goes_red_under_each_mutation(mutate: Callable[[dict], None], message: str) -> None:
    workflow = _ci_workflow()
    mutate(workflow)
    with pytest.raises(AssertionError, match=message):
        _assert_kind_job(workflow)
        _assert_render_gate(workflow)


def _widen_the_testcontainer_override(workflow: dict) -> None:
    for step in workflow["jobs"]["testcontainer"]["steps"]:
        if isinstance(step.get("run"), str):
            step["run"] = step["run"].replace(TESTCONTAINER_MARKER_EXPRESSION, "-m testcontainer")


def test_kind_marker_isolation_goes_red_when_the_testcontainer_override_widens() -> None:
    workflow = _ci_workflow()
    _widen_the_testcontainer_override(workflow)
    with pytest.raises(AssertionError, match="Testcontainer job does not exclude the kind marker"):
        _assert_kind_marker_isolation(tomllib.loads(PYPROJECT.read_text(encoding="utf-8")), workflow, _kind_sources())


def test_kind_marker_isolation_goes_red_when_the_default_selection_drops_the_exclusion() -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    index = addopts.index("-m") + 1
    addopts[index] = addopts[index].replace(" and not kind", "")
    with pytest.raises(AssertionError, match="default selection does not exclude the kind marker"):
        _assert_kind_marker_isolation(pyproject, _ci_workflow(), _kind_sources())


def test_kind_lane_pins_go_red_when_the_smoke_script_drifts() -> None:
    smoke = KIND_SMOKE.read_text(encoding="utf-8").replace(f"KIND_SHA256={KIND_SHA256}", f"KIND_SHA256={'0' * 64}")
    with pytest.raises(AssertionError, match="KIND_SHA256 drifted from the pin"):
        _assert_kind_lane_pins(smoke, yaml.safe_load(KIND_CONFIG.read_text(encoding="utf-8")), PLATFORM_FACTS.read_text(encoding="utf-8"))
```

`KIND_VERSION` / `KIND_SHA256` are K0's §1.1 literals, the same values the
script carries, so `_assert_kind_lane_pins` passes on the first run; the drift
test above is what proves it can go red.

- [ ] **Step 8: Run the pin tests to verify they fail for the right reason.**

Run: `cd "$(git rev-parse --show-toplevel)" && PATH="$PWD/.claude/lanes/k8s/bin:$PATH" ELSPETH_CI_KUBECTL_REQUIRED=1 pytest tests/unit/deployment/test_kubernetes_bundle.py -n 0 -v > /tmp/klane-k4-step8.log 2>&1; echo exit=$?; grep -E "PASSED|FAILED|ERROR|SKIPPED" /tmp/klane-k4-step8.log | sed 's/ *\[.*%\]//' | head -40`
Expected: exit 1. K1's render tests and K3's kubectl-pin and mutation tests PASS as before (K3's `test_render_gate_goes_red_under_each_mutation[*]` still pass: every mutation trips its own assertion before the loop reaches the new tuple entry); the new tests split exactly like this:
- PASS: `test_kind_lane_pins_kind_and_the_node_image_from_the_facts_document` (the script, the kind-config and the facts document already agree), `test_kind_lane_pins_go_red_when_the_smoke_script_drifts`, `test_kind_marker_isolation_goes_red_when_the_default_selection_drops_the_exclusion` (the mutated addopts trip the first assertion regardless of ci.yaml), and `test_kind_marker_isolation_goes_red_when_the_testcontainer_override_widens` (vacuous until ci.yaml carries the expression: the widening mutation is a no-op today and the unmodified job already fails the same assertion).
- FAIL `the Testcontainer job does not exclude the kind marker`: `test_kind_marker_is_registered_and_selected_only_by_the_kind_lane`.
- FAIL `KeyError: 'kubernetes-kind'`: `test_ci_kind_job_runs_the_smoke_on_a_docker_capable_runner_and_gates_ci_success` and `test_kind_job_gate_goes_red_under_each_mutation[result-check-dropped|switch-dropped|container-job]` (a `KeyError` is not the `AssertionError` the `pytest.raises` expects, so the mutation tests fail rather than pass vacuously); `[needs-entry-dropped]` FAILS with `ValueError: list.remove(x): x not in list` for the same reason.
- FAIL `kubernetes-kind is not in ci-success.needs`: K3's `test_ci_renders_every_kustomization_and_gates_ci_success`, because `RESULT_GATED_JOBS` grew.

- [ ] **Step 9: Wire CI — the job, the ci-success gate, and the Testcontainer exclusion.**

Replace the testcontainer job's pytest step (`ci.yaml:938-942`):

```yaml
        run: |
          uv run pytest tests/ \
            -v \
            -m "testcontainer and not kind" \
            -n 0 \
            --junitxml=testcontainer-junit.xml
```

and extend that step's comment (the block ending at :937) with:

```yaml
        # `and not kind`: the kind-marked deployment proofs under
        # tests/testcontainer/deployment need kind/kubectl and a docker build
        # and run only in the kubernetes-kind job (Task K4); this override
        # must not sweep them in (spec :1478-1480 forbids the sweep).
```

Insert the job directly after K3's `kubernetes-render` job (before `supply-chain-audit:`):

```yaml
  # ===========================================================================
  # Kubernetes kind lane: the shipped base on a real API server — two
  # replicas, one RWX share, one PostgreSQL, a provider-free run (Task K4;
  # K5 adds the replica probes). One code path with the desk: the smoke
  # script installs the pinned kubectl/kind, the fixture builds and loads the
  # image, and pytest runs `-m kind -n 0`. Always GitHub-hosted, like
  # testcontainer: the self-hosted jobs are `container:` jobs with no Docker
  # daemon. No `needs: [static-analysis]` (operator ruling 2026-09-05,
  # elspeth-d8749aeaa3). timeout-minutes is the DECISIONS K9 budget; the
  # measured wall time is recorded in
  # docs/plans/2026-09-13-kubernetes-platform-facts.md §5.1 Kind lane wall time.
  # ===========================================================================
  kubernetes-kind:
    name: Kubernetes kind lane (two replicas, shared state)
    runs-on: ubuntu-24.04
    timeout-minutes: 60
    steps:
      - name: Checkout code
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1

      - name: Install uv
        uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9
        with:
          version: ${{ env.UV_VERSION }}
          enable-cache: false
          prune-cache: true

      - name: Install system dependencies
        run: |
          sudo apt-get update -q
          sudo apt-get install -y -q libsqlcipher-dev

      - name: Install dependencies
        run: uv sync --frozen --all-extras

      - name: Run the kind smoke (pinned kubectl and kind, docker build, kind load, -m kind -n 0)
        # ELSPETH_CI_KIND_REQUIRED turns the fixture's skip-when-tools-missing
        # into a failure here, mirroring host-runner-unit's
        # ELSPETH_CI_DOCKER_REQUIRED; the script puts the pinned tools on PATH
        # before pytest starts, so a skip on this runner is a broken lane.
        env:
          ELSPETH_CI_KIND_REQUIRED: "1"
          ELSPETH_K8S_TOOLS: /tmp/k8s-tools
          ELSPETH_KIND_LOG_DIR: /tmp/k8s-logs
        run: scripts/cicd/kubernetes-kind-smoke.sh

      - name: Upload kind lane log
        if: always()
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a
        with:
          name: kubernetes-kind-log
          path: /tmp/k8s-logs/kind-smoke-*.log
          retention-days: 30
```

In `ci-success` (`ci.yaml:1335-1344`) add `- kubernetes-kind` to `needs`
directly after K3's `- kubernetes-render` entry, and in the
`Check all jobs passed` script add, directly after K3's `kubernetes-render`
block and before the `supply-chain-audit` block:

```yaml
          if [[ "${{ needs.kubernetes-kind.result }}" != "success" ]]; then
            echo "Kubernetes kind lane job failed"
            exit 1
          fi
```

Then update the two pins in `tests/unit/cicd/test_state_engine_ci_selection.py`
that the new Testcontainer expression breaks. At `:247-248` replace

```python
    assert set(marker_expressions) == {"testcontainer"}, marker_expressions
    assert marker_expressions["testcontainer"] == ["testcontainer"], marker_expressions
```

with

```python
    assert set(marker_expressions) == {"testcontainer"}, marker_expressions
    # The override is quoted, so the whitespace split above sees only its
    # first token; pin the whole expression on the raw text instead. The
    # kubernetes-kind job carries no `-m` of its own: its selection lives in
    # scripts/cicd/kubernetes-kind-smoke.sh (Task K4), and the `kind` marker
    # it selects is deselected here so the two jobs never share an id.
    assert '-m "testcontainer and not kind"' in _run_lines(_job("testcontainer")), marker_expressions
```

and rewrite the last five sentences of that test's docstring (:225-231) to:

```python
    ``not testcontainer``. It is safe for the same reason it is narrow —
    ``testcontainer and live_provider`` and ``testcontainer and (slow or stress
    or performance)`` each collect zero ids tree-wide, so no guard it drops has
    anything to catch. Pinning the expression to exactly
    ``testcontainer and not kind`` keeps it that way: a widened override here
    would silently re-select the live lanes or the kind lane (whose proofs
    need kind/kubectl and a docker build the Testcontainer runner never
    installs), which is the failure this whole family is about.
    """
```

At `:268` replace `assert "-m testcontainer" in commands` with
`assert '-m "testcontainer and not kind"' in commands`.

- [ ] **Step 10: Run the pin suites and the render check.**

Run: `cd "$(git rev-parse --show-toplevel)" && PATH="$PWD/.claude/lanes/k8s/bin:$PATH" ELSPETH_CI_KUBECTL_REQUIRED=1 pytest tests/unit/deployment/test_kubernetes_bundle.py tests/unit/cicd/test_state_engine_ci_selection.py tests/unit/deployment/test_azure_container_apps_bundle.py -n 0 > /tmp/klane-k4-step10.log 2>&1; echo exit=$?; tail -3 /tmp/klane-k4-step10.log`
Expected: exit 0 (the ACA bundle tests skip if `bicep` is absent locally; they are listed because they share `ci-success` assertions).

Run: `cd "$(git rev-parse --show-toplevel)" && actionlint .github/workflows/ci.yaml > /tmp/klane-k4-step10-actionlint.log 2>&1; echo exit=$?` — expected exit 0 when `actionlint` is installed; if it is not, say so in the handoff rather than skipping the mention.

Run the default-selection control for the new addopts expression:
`cd "$(git rev-parse --show-toplevel)" && pytest tests/testcontainer/deployment tests/unit/deployment/test_kubernetes_bundle.py --collect-only -q -n 0 > /tmp/klane-k4-step10-collect.log 2>&1; echo exit=$?; tail -2 /tmp/klane-k4-step10-collect.log`
Expected: the kind-lane ids appear only in the `deselected` count; every `test_kubernetes_bundle.py` id is collected.

- [ ] **Step 11: Commit.**

Stage first, then run the safety check, then commit: `scripts/branch-safety-check.sh` inspects the STAGED set. The ten created files get `git add -N` because a commit pathspec only selects paths the index knows; then all fifteen paths are staged by name (file paths only, never a directory), the check runs, and the message goes before `--`.

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N deploy/kubernetes/overlays/kind-test/kustomization.yaml tests/testcontainer/deployment/kubernetes/kind-config.yaml tests/testcontainer/deployment/kubernetes/pv-rwx-hostpath.yaml tests/testcontainer/deployment/kubernetes/postgresql.yaml tests/testcontainer/deployment/kubernetes/01-databases.sql tests/testcontainer/deployment/kubernetes/02-roles.sql tests/testcontainer/deployment/kind_harness.py tests/testcontainer/deployment/conftest.py tests/testcontainer/deployment/test_kubernetes_kind.py scripts/cicd/kubernetes-kind-smoke.sh
git add -- deploy/kubernetes/overlays/kind-test/kustomization.yaml tests/testcontainer/deployment/kubernetes/kind-config.yaml tests/testcontainer/deployment/kubernetes/pv-rwx-hostpath.yaml tests/testcontainer/deployment/kubernetes/postgresql.yaml tests/testcontainer/deployment/kubernetes/01-databases.sql tests/testcontainer/deployment/kubernetes/02-roles.sql tests/testcontainer/deployment/kind_harness.py tests/testcontainer/deployment/conftest.py tests/testcontainer/deployment/test_kubernetes_kind.py scripts/cicd/kubernetes-kind-smoke.sh pyproject.toml .github/workflows/ci.yaml tests/unit/deployment/test_kubernetes_bundle.py tests/unit/cicd/test_state_engine_ci_selection.py docs/plans/2026-09-13-kubernetes-platform-facts.md
git status --short
scripts/branch-safety-check.sh --intent commit
git commit -m "test(deploy): prove two-replica Kubernetes startup and shared state in kind" -- deploy/kubernetes/overlays/kind-test/kustomization.yaml tests/testcontainer/deployment/kubernetes/kind-config.yaml tests/testcontainer/deployment/kubernetes/pv-rwx-hostpath.yaml tests/testcontainer/deployment/kubernetes/postgresql.yaml tests/testcontainer/deployment/kubernetes/01-databases.sql tests/testcontainer/deployment/kubernetes/02-roles.sql tests/testcontainer/deployment/kind_harness.py tests/testcontainer/deployment/conftest.py tests/testcontainer/deployment/test_kubernetes_kind.py scripts/cicd/kubernetes-kind-smoke.sh pyproject.toml .github/workflows/ci.yaml tests/unit/deployment/test_kubernetes_bundle.py tests/unit/cicd/test_state_engine_ci_selection.py docs/plans/2026-09-13-kubernetes-platform-facts.md
git show --stat HEAD
```

Expected: `git status --short` shows the ten created paths as `A ` and the five modified paths as `M `; the safety check prints no `[FAIL]` line and exits 0 (on a FAIL, stop before the commit); `git show --stat HEAD` ends with `15 files changed`. Any other count means a sibling lane staged into the shared index: `git reset --mixed HEAD~1`, restage only the 15 paths above, and commit again.

The pre-commit secret scanner rescans every staged line; nothing here carries
a URL with a password or a quoted 30+ character secret, so it should pass
without an allow marker — if it fires, fix the line rather than marking it.
