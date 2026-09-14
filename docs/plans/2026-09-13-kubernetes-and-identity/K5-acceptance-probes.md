### Task K5: Acceptance probes — the four replica probes on Kubernetes

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: K4. Runs before: K7. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered after K4 (K0 → K1 → K2 → K3 → K4 → K5 → K7). This task drives the
provider-neutral replica probes (P1 `session_operation_fence`, P2
`session_operation_fence_execute`, P3 `role_revocation_lease_expiry`, P4a
`postgresql_and_nfs`, P4b `owner_affine` = `cannot_pass`) against two
one-replica Deployments on the K4 kind cluster, and asserts each probe as a
pytest outcome in the `kubernetes-kind` lane. It builds exactly three things
(DECISIONS K12): (a) the PostgreSQL half of the probes moves out of the
Container Apps package into `_acceptance_common/postgres_observer.py`, with the
Container Apps controller re-exporting it; (b) a Kubernetes `ReplicaController`;
(c) the kind-lane probe module plus the `kind-acceptance` overlay it applies
(DECISIONS K13). There is NO `receipt_contracts.py`, NO `kubernetes_acceptance.py`
facade, NO `CLOUD_PROVIDERS` change and NO receipt store: the evidence is the
kind-lane pytest result, and the receipt-v3 trigger recorded in
`src/elspeth/web/_azure_container_apps_acceptance/README.md:21-24,35-36`
("revisit v3 before a third provider") stays untripped because K5 implements
only the already-shared `ReplicaController` port.

Measured 2026-09-14 on the checkout at `818d04577` (its only delta from
`072141b75` is `web/composer/service.py`, `test_advisor_checkpoint.py` and
`config/cicd/soft-mapping-census.yaml`; none is read or written here):

- `src/elspeth/web/_acceptance_common/replica_probes.py:487-490` —
  `@dataclass(frozen=True) class ReplicaAddress: name: str; origin: str` (no
  `base_url`); `:493-511` `ReplicaController` (`replicas`, `partition_owner`,
  `stop_owner`, `restore_owner`); `:513-532` `EvidenceObserver`; `:567`
  `class ReplicaProbeDriver(*, controller, observer, client_factory, clock=time.monotonic, pinned_clients=None)`
  with `fence_conflict_trial(session_id, request)` (`:624`) and
  `run_start_trial(session_id, request, *, observation_timeout_seconds=30.0)`
  (`:638`); `:536` `ProbeRequest(method, path, json_body)`; decisions
  `decide_fence_conflict` (`:286`), `decide_run_start` (`:329`),
  `decide_lease_takeover` (`:365`), `decide_cross_replica_progress` (`:444`),
  `record_owner_affine_progress(*, mitigation: Literal["single_revision_sticky_sessions"])`
  (`:472`). There is no `run_replica_probes`.
- `src/elspeth/web/_azure_container_apps_acceptance/controller.py` (333 lines):
  `:42` `_RUNTIME_ROLE_PATTERN = elspeth_runtime_[a-z]`; `:48-63` the ten probe
  SQL constants (`BACKEND_PID_SQL` through `MEMBERSHIP_ROW_SQL`); `:66-77`
  `_runtime_role`, `nologin_sql`, `login_sql`; `:123-133` `SqlSession`,
  `SessionFactory`; `:143-183` `PartitionRecord`, `RoleRevocationPartition`
  (`partition(role) -> PartitionRecord`, `restore(role) -> None`; `__init__`
  refuses any role that is not `elspeth_runtime_<letter>` at `:155`);
  `:260-267` `SqlReader`; `:270-333` `PostgresEvidenceObserver`. Everything
  else (`label_url`, `IngressTopology`, `LabelWeight`, `PlatformCommands`,
  `ProbeReplica`, `ContainerAppsReplicaController`) is Container Apps code and
  stays.
- Importers of those names, all of which keep working through the re-export:
  `src/elspeth/web/azure_container_apps_acceptance.py:59-75`,
  `src/elspeth/web/azure_container_apps_observations.py:42-48`,
  `tests/unit/web/azure_container_apps_acceptance/test_replica_probes.py:47-65`,
  `test_facade_contract.py:28`, `test_live_observations.py:18-22`,
  `test_evidence_projection.py:32`.
- `tests/unit/web/acceptance_common/test_dependencies.py:17-23`
  `FORBIDDEN_PREFIXES` and `:24-34` `EXPECTED_MODULES` (the exact
  `_acceptance_common` module set, without `postgres_observer`).
- `tests/unit/architecture/test_session_db_mutation_authority.py:5389-5399`:
  `_ACCEPTANCE_PROBE_MODULES` and `_PROBE_SEAM_PROTOCOLS` = the two names
  `elspeth.web._azure_container_apps_acceptance.controller.SqlSession` and
  `elspeth.web._azure_container_apps_acceptance.controller.SqlReader`;
  `_ProbeSeamProof._is_seam` (`:9500-9503`) resolves a seam class's base
  through `_imported_qualified_name` (`:7024`), i.e. by the import statement's
  module path, never by following a re-export. Control measured on this
  checkout by scanning two copies of `azure_container_apps_acceptance.py` with
  `scan_production_writers`: the unmodified copy yields `0` sites; a copy whose
  `SqlSession`/`SqlReader` import names `._acceptance_common.postgres_observer`
  yields `6` (`_SqlAlchemyReader.rows`, `.scalar`, `_SqlAlchemySession.__init__`,
  `.execute_scalar`). So the facade's import line must not change, and then the
  manifest needs no edit. On this working tree the selection
  `-k "all_production_sessions_writers or probe_seam or aca_shared_surfaces"`
  reports `2 passed, 263 deselected, 1 xfailed`; the xfail
  (`test_all_production_sessions_writers_are_reviewed_typed_authorities`,
  "Sessions mutation authority inventory drift", 67 unexpected Landscape
  sites) contains no line with `acceptance`.
- `config/cicd/soft-mapping-census.yaml` has no entry for the Container Apps
  controller (`**parameters: object` is not a soft mapping); `python -m
  scripts.check_contracts` already exits 1 on this working tree for
  `web/interpretation_state.py` only. `config/cicd/enforce_tier_model/web.yaml`
  has no entry for the controller, and `elspeth-lints check --rules all --root
  src/elspeth` (shape-only mode) exits 1 with 2061 lines, none naming
  `_azure_container_apps_acceptance/controller`.
- The probe inputs are provider-free except P4a. P1: `POST
  /api/sessions/{id}/guided/start` (`sessions/routes/composer/guided.py:1436`,
  body `StartGuidedRequest` `schemas.py:410-424`: `operation_id`, `profile`
  (`live`|`tutorial`, `composer/guided/profile.py:15-16`), `intent`) seeds the
  deterministic `step_1_source` turn, and `POST /api/sessions/{id}/guided/respond`
  (`GuidedRespondRequest` `schemas.py:786`: `operation_id`, `turn_token`,
  `chosen`) with `chosen=["csv"]` answers it without the planner
  (`tests/integration/web/composer/guided/test_get_guided.py:67-103`); the
  Container Apps runbook builds the same trial bodies
  (`deploy/azure-container-apps/scripts/acceptance.sh:470-487`). P2: the K4
  upload + `state/yaml` import of `examples/threshold_gate`. P3:
  `azure_container_apps_observations.collect_takeover` (`:490-572`) partitions
  the literal role `elspeth_runtime_a` (`:529`), needs a long-running pipeline with one
  csv sink whose persisted path lies under `outputs/<session>`, and a
  `PhysicalSinkOracle` (`:118-188`, `@dataclass(frozen=True)`, fields `path`,
  `key_field`) that reads the sink from the runner's filesystem. The session
  operation lease defaults to 30 s (`sessions/service.py:4401`) and the orphan
  sweep runs every 300 s (`config.py:472`), both inside `Polling`'s 600 s
  ceiling (`azure_container_apps_observations.py:234-237`). P4a:
  `collect_progress` (`:437-480`) posts a composer message and requires an
  assistant reply; with no provider the messages route answers 502
  (`sessions/routes/messages.py:390-438`), so P4a runs only when a composer
  provider is supplied (open question below).
- `normalize_acceptance_origin` admits `http` only for exact loopback hosts
  (`_acceptance_common/http_client.py:32`, `:48-84`): the NodePort origins
  `http://127.0.0.1:30452|30453` qualify. The runtime image is
  `gcr.io/distroless/python3-debian13:debug-nonroot` with `/opt/venv/bin` on
  `PATH` (`Dockerfile:154`, `:176`), so `kubectl exec deployment/<name> -- /opt/venv/bin/python`
  is available in a web pod.
- The two probe roles come from K4, not from this task: K4's fixture builds
  ConfigMap `kind-postgres-init` with `01-databases.sql`, `02-roles.sql`,
  `bootstrap-roles.sql` and `bootstrap-acceptance-roles.sql` (K4 Step 3), its
  `tests/testcontainer/deployment/kubernetes/02-roles.sql` runs
  `\ir elspeth/bootstrap-acceptance-roles.sql` and its `postgresql.yaml` mounts
  the two shipped files under `elspeth/` (K4 Step 4), so PostgreSQL init
  creates `elspeth_runtime_a` and `elspeth_runtime_b` before K4 creates the
  Secrets `elspeth-web-secrets-a|b` that name them. K5 edits no K4 harness file.
- The overlay below was rendered offline with the K0 kubectl (`v1.37.0`,
  kustomize `v5.8.1`) over K1's base and K4's `kind-test` overlay: exit 0, 8
  objects (ConfigMap, Service ×2, PersistentVolumeClaim, Deployment ×2, Job
  ×2); each child directory also renders on its own (exit 0), which K3's
  render loop (`find deploy/kubernetes -name kustomization.yaml`) requires.
  `kubectl apply -k` on a missing directory fails before contacting a server
  with `error: must build at directory: not a valid directory: evalsymlink failure on '<dir>' : lstat <abs dir>: no such file or directory`
  (exit 1).

Controller shape: DECISIONS K12 names `deployments={"a": "elspeth-web-a", "b": "elspeth-web-b"}`;
a controller also needs each replica's origin and runtime role, so the
signature takes `replicas: tuple[ProbePod, ProbePod]` with
`ProbePod(address, deployment, role)` — the same shape as the Container Apps
`ProbeReplica(address, revision, role)` (`controller.py:185-197`) — and the
deployment names are exactly those two. **Open question (DECISIONS F12):** this constructor departs from the K12 text (`deployments={...}` → `replicas: tuple[ProbePod, ProbePod]`); either amend DECISIONS K12 to the `ProbePod` shape or rule that K5 must take `deployments=` and derive origin and role another way. Until ruled, the `ProbePod` shape is what this block builds.

**Files:**
- Create: `src/elspeth/web/_acceptance_common/postgres_observer.py`
- Modify: `src/elspeth/web/_azure_container_apps_acceptance/controller.py:27-77` (imports, role pattern, SQL constants, role helpers → imported back), `:120-133` (`SqlSession`, `SessionFactory` removed), `:143-183` (`PartitionRecord`, `RoleRevocationPartition` removed), `:194` (`_runtime_role` → `require_runtime_role`), `:257-333` (observer section removed); the file is regenerated whole in Step 5
- Modify: `tests/unit/web/acceptance_common/test_dependencies.py:17-34` (`FORBIDDEN_PREFIXES` gains the Kubernetes package; `EXPECTED_MODULES` gains `postgres_observer`)
- Test: `tests/unit/web/acceptance_common/test_postgres_observer.py`
- Create: `src/elspeth/web/_kubernetes_acceptance/__init__.py`, `src/elspeth/web/_kubernetes_acceptance/controller.py`
- Test: `tests/unit/web/kubernetes_acceptance/__init__.py`, `tests/unit/web/kubernetes_acceptance/test_controller.py`
- Create: `deploy/kubernetes/overlays/kind-acceptance/kustomization.yaml`, `deploy/kubernetes/overlays/kind-acceptance/shared/kustomization.yaml`, `deploy/kubernetes/overlays/kind-acceptance/replica-a/kustomization.yaml`, `deploy/kubernetes/overlays/kind-acceptance/replica-b/kustomization.yaml`
- Test: `tests/testcontainer/deployment/test_kubernetes_replica_probes.py` (marker `kind`)
- Unchanged, proven by Step 7: `src/elspeth/web/azure_container_apps_acceptance.py`, `src/elspeth/web/azure_container_apps_observations.py`, `tests/unit/architecture/test_session_db_mutation_authority.py`, `config/cicd/soft-mapping-census.yaml`, `src/elspeth/web/_acceptance_common/identity.py`, `tests/unit/web/acceptance_common/test_identity_and_errors.py`

**Interfaces:**
- Consumes:
  - From HEAD, `_acceptance_common/replica_probes.py`: `ReplicaAddress(name: str, origin: str)`, `ReplicaController`, `EvidenceObserver`, `MembershipRow`, `ReplicaProbeDriver`, `ProbeRequest`, `DEFAULT_TRIALS = 20`, `decide_fence_conflict`, `decide_run_start`, `decide_lease_takeover`, `decide_cross_replica_progress`, `record_owner_affine_progress`; `_acceptance_common/http_client.py`: `AcceptanceCredentials(mode="bearer", bearer_token: str)`, `AcceptanceHttpClient(*, origin, credentials)`; `_acceptance_common/errors.py`: `AcceptanceCheckError(check)`, `AcceptanceInputError(message)`; `azure_container_apps_observations.py`: `Capture(directory)`, `Polling(interval, timeout)`, `PhysicalSinkOracle(path, key_field)`, `collect_takeover(*, owner, survivor, session_id, sessions, observer, partition, sink, capture, polling)`, `collect_progress(*, owner, reader, session_id, capture, polling)`, `observation_document(observation)`; `_azure_container_apps_acceptance/evidence.py`: `lease_takeover_observation(payload: object)` (`:557`), `cross_replica_progress_observation(payload: object)` (`:612`).
  - From K1: Deployment `elspeth-web` (pod labels `app.kubernetes.io/name: elspeth-web`, `app.kubernetes.io/component: web`; `envFrom[0]` ConfigMap `elspeth-web-config`, `envFrom[1]` Secret `elspeth-web-secrets`), Service `elspeth-web` (one port `http`), ConfigMap `ELSPETH_WEB__DATA_DIR: /mnt/elspeth/data`, Jobs `elspeth-provision-storage`, `elspeth-schema-init`.
  - From K2: `GET /api/system/status` reports `deployment_target == "kubernetes"` and `deployment_replica == $ELSPETH_K8S_POD_NAME`.
  - From K4: `deploy/kubernetes/overlays/kind-test/` (image `elspeth-web-test:kind`, revision `sha-kindtest`, release `0.8.1+kindtest`, PVC bound to PV `elspeth-state-rwx`); the session fixture `kind_cluster -> KindCluster` (`tests/testcontainer/deployment/conftest.py`); `tests.testcontainer.deployment.kind_harness`: `REPO_ROOT`, `HERE`, `KindCluster.kubectl(*args: str) -> str` (asserts exit 0 with message `kubectl <args> failed (exit=<n>):\n<stderr>`), `KindCluster.kubeconfig: Path`, `KindCluster.database_url(role: str, database: str) -> str` (host NodePort 30432; roles `postgres`, `elspeth_schema_owner`, `elspeth_runtime`, `elspeth_runtime_a`, `elspeth_runtime_b`); the PostgreSQL roles `elspeth_runtime_a` and `elspeth_runtime_b`, created at init by K4's ConfigMap `kind-postgres-init` (`02-roles.sql` runs K1's `deploy/kubernetes/base/bootstrap-acceptance-roles.sql`, which `\ir`-includes `bootstrap-roles.sql`), with passwords in `KindCluster.passwords`; Secrets `elspeth-web-secrets-a` (`elspeth_runtime_a`) and `elspeth-web-secrets-b` (`elspeth_runtime_b`); kind host ports 30452/30453; marker `kind`; `scripts/cicd/kubernetes-kind-smoke.sh [extra pytest args]` (prints `exit=<n> wall=<s>s log=<path>`); K4's CI pin `_kind_sources()` requires `pytestmark = pytest.mark.kind` in every `tests/testcontainer/deployment/test_*.py`.
- Produces:
  - `elspeth.web._acceptance_common.postgres_observer`: `BACKEND_PID_SQL`, `TERMINATE_OWN_ROLE_BACKENDS_SQL`, `FENCE_EPOCH_SQL`, `FENCE_OWNER_SQL`, `DATABASE_NOW_SQL`, `GUIDED_OPERATIONS_SINCE_SQL`, `RUN_IDS_SQL`, `LANDSCAPE_RUN_IDS_OF_SESSION_SQL`, `LANDSCAPE_RUN_EXISTS_SQL`, `MEMBERSHIP_ROW_SQL` (all `Final[str]`); `require_runtime_role(role: str) -> str`; `nologin_sql(role: str) -> str`; `login_sql(role: str) -> str`; `class SqlSession(ABC)` (`execute_scalar(statement: str) -> object`, `close() -> None`); `SessionFactory = Callable[[], SqlSession]`; `class SqlReader(ABC)` (`scalar(statement: str, **parameters: object) -> object`, `rows(statement: str, **parameters: object) -> tuple[tuple[object, ...], ...]`); `PartitionRecord(role: str, own_backend_pid: int, terminated_backends: int)`; `RoleRevocationPartition(*, admin: SessionFactory, roles: Mapping[str, SessionFactory])` with `partition(role: str) -> PartitionRecord` and `restore(role: str) -> None`; `PostgresEvidenceObserver(*, sessions: SqlReader, landscape: SqlReader)` implementing `EvidenceObserver`. The Container Apps controller re-exports every one of these names (same objects).
  - `elspeth.web._kubernetes_acceptance.controller`: `KUBECTL_COMMAND_TIMEOUT_SECONDS: Final = 360.0`; `class KubectlCommands(ABC)` with `run(argv: Sequence[str]) -> bytes`; `KubectlSubprocess(*, kubeconfig: Path | None = None)` implementing it (argv[0] must be `kubectl`; failure → `AcceptanceCheckError("platform_command")`); `ProbePod(address: ReplicaAddress, deployment: str, role: str)` (frozen, slots); `KubernetesReplicaController(*, namespace: str, replicas: tuple[ProbePod, ProbePod], partition: RoleRevocationPartition, platform: KubectlCommands)` implementing `ReplicaController`: `replicas() -> tuple[ReplicaAddress, ReplicaAddress]`; `partition_owner(replica)` → `partition.partition(<role>)`; `stop_owner(replica)` → `kubectl -n <ns> delete pod -l app.kubernetes.io/instance=<deployment> --grace-period=0 --force --wait=false`; `restore_owner(replica)` → `partition.restore(<role>)` then `kubectl -n <ns> rollout status deployment/<deployment> --timeout=300s`.
  - `deploy/kubernetes/overlays/kind-acceptance/`: Deployments `elspeth-web-a` / `elspeth-web-b` (`replicas: 1`, labels and selector gain `app.kubernetes.io/instance: elspeth-web-a|b`, `envFrom[1]` Secret `elspeth-web-secrets-a|b`, `envFrom[2]` optional Secret `elspeth-kind-composer`), NodePort Services `elspeth-web-a` (30452) / `elspeth-web-b` (30453) selecting on the instance label; the shared ConfigMap, PVC and both Jobs from `kind-test`.
  - `tests/testcontainer/deployment/test_kubernetes_replica_probes.py`: module fixture `lane -> ProbeLane`, env switch `ELSPETH_KIND_COMPOSER_ENV_FILE`, and the six test ids defined in Step 12 (K8 cites the file and the four probe ids). Ordering: pytest collects `test_kubernetes_kind.py` (K4's proofs, then K7's appended section, whose `no_affinity_rollout` teardown re-applies `kind-test`) before this module in the same session, so this module runs after both K4's and K7's tests and hands the cluster to no later task. Its setup deletes K4's `elspeth-web` Deployment and Service itself; its teardown deletes its own two Deployments and Services, so the session ends with no web Deployment.

- [ ] **Step 1: Record the gate baselines this task must not move.**

Run: `cd "$(git rev-parse --show-toplevel)" && mkdir -p .claude/lanes/k5 && .venv/bin/python -m pytest tests/unit/architecture/test_session_db_mutation_authority.py -k "all_production_sessions_writers or probe_seam or aca_shared_surfaces" -n 0 -q -rx > /tmp/klane-k5-authority-before.log 2>&1; echo exit=$?; grep -c acceptance /tmp/klane-k5-authority-before.log; tail -1 /tmp/klane-k5-authority-before.log`
Expected: `exit=0`; `0`; `2 passed, 263 deselected, 1 xfailed in <n>s` (the xfail is the pre-existing Landscape inventory drift; record its `Unexpected/unreviewed (<n>)` count from the log).

Run: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -m scripts.check_contracts > /tmp/klane-k5-contracts-before.log 2>&1; echo exit=$?; grep -c "_acceptance_common\|_kubernetes_acceptance\|_azure_container_apps_acceptance" /tmp/klane-k5-contracts-before.log`
Expected: the exit code this working tree already has (1 on the measured tree, for `web/interpretation_state.py`), and `0`.

Run: `cd "$(git rev-parse --show-toplevel)" && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing .venv/bin/elspeth-lints check --rules all --root src/elspeth > /tmp/klane-k5-lints-before.log 2>&1; echo exit=$?; wc -l < /tmp/klane-k5-lints-before.log; grep -c "_acceptance_common/postgres_observer\|_kubernetes_acceptance\|_azure_container_apps_acceptance/controller" /tmp/klane-k5-lints-before.log`
Expected: `exit=1` (the deliberate fail-closed corpus), the line count (2061 on the measured tree), and `0`.

- [ ] **Step 2: Write the failing tests for the shared PostgreSQL module.**

```python
# tests/unit/web/acceptance_common/test_postgres_observer.py
"""The PostgreSQL half of the replica probes lives once, in the shared core.

The Container Apps controller re-exports every moved name as the SAME object,
so its facade, its observation collector and its tests keep importing from
the controller path — which is also the path the Sessions mutation-authority
manifest resolves probe seams through (``_PROBE_SEAM_PROTOCOLS``).
"""

from __future__ import annotations

import pytest

from elspeth.web._acceptance_common import postgres_observer as shared
from elspeth.web._acceptance_common.errors import AcceptanceInputError
from elspeth.web._azure_container_apps_acceptance import controller as container_apps

SHARED_MODULE = "elspeth.web._acceptance_common.postgres_observer"


@pytest.mark.parametrize(
    ("reexported", "defined"),
    [
        pytest.param(container_apps.BACKEND_PID_SQL, shared.BACKEND_PID_SQL, id="BACKEND_PID_SQL"),
        pytest.param(
            container_apps.TERMINATE_OWN_ROLE_BACKENDS_SQL,
            shared.TERMINATE_OWN_ROLE_BACKENDS_SQL,
            id="TERMINATE_OWN_ROLE_BACKENDS_SQL",
        ),
        pytest.param(container_apps.FENCE_EPOCH_SQL, shared.FENCE_EPOCH_SQL, id="FENCE_EPOCH_SQL"),
        pytest.param(container_apps.FENCE_OWNER_SQL, shared.FENCE_OWNER_SQL, id="FENCE_OWNER_SQL"),
        pytest.param(container_apps.DATABASE_NOW_SQL, shared.DATABASE_NOW_SQL, id="DATABASE_NOW_SQL"),
        pytest.param(container_apps.GUIDED_OPERATIONS_SINCE_SQL, shared.GUIDED_OPERATIONS_SINCE_SQL, id="GUIDED_OPERATIONS_SINCE_SQL"),
        pytest.param(container_apps.RUN_IDS_SQL, shared.RUN_IDS_SQL, id="RUN_IDS_SQL"),
        pytest.param(
            container_apps.LANDSCAPE_RUN_IDS_OF_SESSION_SQL,
            shared.LANDSCAPE_RUN_IDS_OF_SESSION_SQL,
            id="LANDSCAPE_RUN_IDS_OF_SESSION_SQL",
        ),
        pytest.param(container_apps.LANDSCAPE_RUN_EXISTS_SQL, shared.LANDSCAPE_RUN_EXISTS_SQL, id="LANDSCAPE_RUN_EXISTS_SQL"),
        pytest.param(container_apps.MEMBERSHIP_ROW_SQL, shared.MEMBERSHIP_ROW_SQL, id="MEMBERSHIP_ROW_SQL"),
        pytest.param(container_apps.SqlSession, shared.SqlSession, id="SqlSession"),
        pytest.param(container_apps.SessionFactory, shared.SessionFactory, id="SessionFactory"),
        pytest.param(container_apps.SqlReader, shared.SqlReader, id="SqlReader"),
        pytest.param(container_apps.PartitionRecord, shared.PartitionRecord, id="PartitionRecord"),
        pytest.param(container_apps.RoleRevocationPartition, shared.RoleRevocationPartition, id="RoleRevocationPartition"),
        pytest.param(container_apps.PostgresEvidenceObserver, shared.PostgresEvidenceObserver, id="PostgresEvidenceObserver"),
        pytest.param(container_apps.login_sql, shared.login_sql, id="login_sql"),
        pytest.param(container_apps.nologin_sql, shared.nologin_sql, id="nologin_sql"),
        pytest.param(container_apps.require_runtime_role, shared.require_runtime_role, id="require_runtime_role"),
    ],
)
def test_the_container_apps_controller_reexports_the_shared_object(reexported: object, defined: object) -> None:
    assert reexported is defined


@pytest.mark.parametrize(
    "defined",
    [
        pytest.param(shared.SqlSession, id="SqlSession"),
        pytest.param(shared.SqlReader, id="SqlReader"),
        pytest.param(shared.PartitionRecord, id="PartitionRecord"),
        pytest.param(shared.RoleRevocationPartition, id="RoleRevocationPartition"),
        pytest.param(shared.PostgresEvidenceObserver, id="PostgresEvidenceObserver"),
        pytest.param(shared.require_runtime_role, id="require_runtime_role"),
    ],
)
def test_the_postgres_half_is_defined_in_the_shared_core_not_the_provider_package(defined: type | object) -> None:
    assert defined.__module__ == SHARED_MODULE


def test_the_shared_runtime_role_guard_admits_only_elspeth_runtime_letter_roles() -> None:
    assert shared.require_runtime_role("elspeth_runtime_a") == "elspeth_runtime_a"
    for role in ("elspeth_probe_a", "elspeth_runtime_", 'elspeth_runtime_a"; DROP ROLE x'):
        with pytest.raises(AcceptanceInputError, match="runtime role must be elspeth_runtime_<letter>"):
            shared.require_runtime_role(role)
```

In `tests/unit/web/acceptance_common/test_dependencies.py` replace `:17-34` with:

```python
FORBIDDEN_PREFIXES = (
    "elspeth.web._aws_ecs_acceptance",
    "elspeth.web.aws_ecs_acceptance",
    "elspeth.web._azure_container_apps_acceptance",
    "elspeth.web.azure_container_apps_acceptance",
    "elspeth.web._kubernetes_acceptance",
    "elspeth.web.app",
)
EXPECTED_MODULES = {
    "compatibility_gate",
    "errors",
    "http_client",
    "identity",
    "postgres_observer",
    "receipt_validation",
    "replica_probes",
    "schema_facts",
    "secure_documents",
    "testcontainer_run",
}
```

- [ ] **Step 3: Run the shared-module tests and watch them fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -m pytest tests/unit/web/acceptance_common/test_postgres_observer.py tests/unit/web/acceptance_common/test_dependencies.py -n 0 --continue-on-collection-errors > /tmp/klane-k5-step3.log 2>&1; echo exit=$?; tail -15 /tmp/klane-k5-step3.log`
Expected: `exit=1`; the collection error `ImportError: cannot import name 'postgres_observer' from 'elspeth.web._acceptance_common'`; `test_shared_core_module_set_is_the_declared_one` FAILED with `AssertionError` (the live set lacks `postgres_observer`); summary `1 failed, 2 passed, 1 error`.

- [ ] **Step 4: Create the shared module (moved code, one rename).**

```python
# src/elspeth/web/_acceptance_common/postgres_observer.py
"""The PostgreSQL half of the replica probes: database seams, role-revocation partition, evidence observer.

Moved without behavioural change from
``_azure_container_apps_acceptance/controller.py`` (Task K5) so a second
platform port reuses one implementation: the shared core may not import a
provider package (``tests/unit/web/acceptance_common/test_dependencies.py``)
and neither may the Kubernetes controller. The one rename is
``_runtime_role`` -> ``require_runtime_role``, public because both platform
controllers call it.

- **Seams.** ``SqlSession`` (one open autocommit session) and ``SqlReader``
  (read-only, bound parameters) are what a facade binds to SQLAlchemy. The
  Sessions mutation-authority manifest admits a facade's seam class by the
  import path its base resolves through, and ``_PROBE_SEAM_PROTOCOLS`` names
  the Container Apps controller path. That controller re-exports these names,
  so the Container Apps facade keeps its import line unchanged.
- **Partition (P3 primary).** A session ``S`` is opened *as* the owner's
  runtime role and kept open; the admin sets the role ``NOLOGIN`` (existing
  sessions survive, new logins are refused); ``S`` terminates every other
  backend of its own role (always permitted for one's own role); ``S``
  closes. From then on the owner's pools reconnect and are refused, while the
  peer's role is untouched. The role is restored with ``LOGIN`` afterwards.
- **Observer.** The database facts the probes score, read through the
  runtime's own PostgreSQL databases.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from .errors import AcceptanceCheckError, AcceptanceInputError
from .replica_probes import EvidenceObserver, MembershipRow

_RUNTIME_ROLE_PATTERN = re.compile(r"elspeth_runtime_[a-z]\Z")

BACKEND_PID_SQL: Final = "SELECT pg_backend_pid()"
TERMINATE_OWN_ROLE_BACKENDS_SQL: Final = (
    "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename = current_user AND pid <> pg_backend_pid()"
)
"""Run from the kept-open session ``S``: terminates every other backend of ``S``'s own role and nothing else."""

FENCE_EPOCH_SQL: Final = "SELECT operation_epoch FROM session_operation_fences WHERE session_id = :session_id"
FENCE_OWNER_SQL: Final = "SELECT owner_instance_id FROM session_operation_fences WHERE session_id = :session_id"
DATABASE_NOW_SQL: Final = "SELECT clock_timestamp()"
GUIDED_OPERATIONS_SINCE_SQL: Final = "SELECT count(*) FROM guided_operations WHERE session_id = :session_id AND created_at >= :since"
RUN_IDS_SQL: Final = "SELECT id FROM runs WHERE session_id = :session_id ORDER BY id"
LANDSCAPE_RUN_IDS_OF_SESSION_SQL: Final = (
    "SELECT landscape_run_id FROM runs WHERE session_id = :session_id AND landscape_run_id IS NOT NULL"
)
LANDSCAPE_RUN_EXISTS_SQL: Final = "SELECT run_id FROM runs WHERE run_id = :run_id"
MEMBERSHIP_ROW_SQL: Final = "SELECT instance_id, state, lease_expires_at FROM web_instances WHERE instance_id = :instance_id"


def require_runtime_role(role: str) -> str:
    if _RUNTIME_ROLE_PATTERN.fullmatch(role) is None:
        raise AcceptanceInputError("runtime role must be elspeth_runtime_<letter>")
    return role


def nologin_sql(role: str) -> str:
    return f'ALTER ROLE "{require_runtime_role(role)}" NOLOGIN'


def login_sql(role: str) -> str:
    return f'ALTER ROLE "{require_runtime_role(role)}" LOGIN'


# --------------------------------------------------------------------------- seams


class SqlSession(ABC):
    """One open database session; the facade binds it to a SQLAlchemy connection in autocommit mode."""

    @abstractmethod
    def execute_scalar(self, statement: str) -> object: ...

    @abstractmethod
    def close(self) -> None: ...


SessionFactory = Callable[[], SqlSession]


class SqlReader(ABC):
    """Read-only access to one database; the facade binds it to a SQLAlchemy engine with bound parameters."""

    @abstractmethod
    def scalar(self, statement: str, **parameters: object) -> object: ...

    @abstractmethod
    def rows(self, statement: str, **parameters: object) -> tuple[tuple[object, ...], ...]: ...


# --------------------------------------------------------------------------- partition


@dataclass(frozen=True, slots=True)
class PartitionRecord:
    role: str
    own_backend_pid: int
    terminated_backends: int


class RoleRevocationPartition:
    """P3 primary primitive, exactly the sequence platform facts §4.2 (C7) records."""

    def __init__(self, *, admin: SessionFactory, roles: Mapping[str, SessionFactory]) -> None:
        for role in roles:
            require_runtime_role(role)
        self._admin = admin
        self._roles = dict(roles)

    def partition(self, role: str) -> PartitionRecord:
        own = self._roles[require_runtime_role(role)]()
        try:
            pid = own.execute_scalar(BACKEND_PID_SQL)
            if type(pid) is not int or pid <= 0:
                raise AcceptanceCheckError("partition_backend_pid")
            admin = self._admin()
            try:
                admin.execute_scalar(nologin_sql(role))
            finally:
                admin.close()
            terminated = own.execute_scalar(TERMINATE_OWN_ROLE_BACKENDS_SQL)
            if type(terminated) is not int or terminated < 0:
                raise AcceptanceCheckError("partition_terminate")
        finally:
            own.close()
        return PartitionRecord(role=role, own_backend_pid=pid, terminated_backends=terminated)

    def restore(self, role: str) -> None:
        admin = self._admin()
        try:
            admin.execute_scalar(login_sql(role))
        finally:
            admin.close()


# --------------------------------------------------------------------------- evidence observer


class PostgresEvidenceObserver(EvidenceObserver):
    """The database facts the probes score, read through the runtime's own PostgreSQL databases.

    ``guided_operation_rows(session, since_epoch=e)`` counts the rows created
    since the database clock reading taken by the ``fence_epoch`` call that
    returned ``e`` (the driver reads the epoch immediately before firing a
    trial and again after), so each trial counts its own rows and the database
    clock, not the driver's, sets the window.
    """

    def __init__(self, *, sessions: SqlReader, landscape: SqlReader) -> None:
        self._sessions = sessions
        self._landscape = landscape
        self._marks: dict[tuple[str, int], datetime] = {}

    def fence_epoch(self, session_id: str) -> int:
        epoch = self._sessions.scalar(FENCE_EPOCH_SQL, session_id=session_id)
        now = self._sessions.scalar(DATABASE_NOW_SQL)
        if type(epoch) is not int or type(now) is not datetime or now.tzinfo is None:
            raise AcceptanceCheckError("probe_observation")
        self._marks[(session_id, epoch)] = now
        return epoch

    def fence_owner(self, session_id: str) -> str | None:
        owner = self._sessions.scalar(FENCE_OWNER_SQL, session_id=session_id)
        if owner is not None and type(owner) is not str:
            raise AcceptanceCheckError("probe_observation")
        return owner

    def guided_operation_rows(self, session_id: str, *, since_epoch: int) -> int:
        if (session_id, since_epoch) not in self._marks:
            raise AcceptanceInputError("guided_operation_rows needs the fence_epoch reading taken before the trial")
        count = self._sessions.scalar(GUIDED_OPERATIONS_SINCE_SQL, session_id=session_id, since=self._marks[(session_id, since_epoch)])
        if type(count) is not int or count < 0:
            raise AcceptanceCheckError("probe_observation")
        return count

    def _ids(self, reader: SqlReader, statement: str, **parameters: object) -> tuple[str, ...]:
        ids: list[str] = []
        for row in reader.rows(statement, **parameters):
            if len(row) != 1 or type(row[0]) is not str:
                raise AcceptanceCheckError("probe_observation")
            ids.append(row[0])
        return tuple(ids)

    def runs_row_ids(self, session_id: str) -> tuple[str, ...]:
        return self._ids(self._sessions, RUN_IDS_SQL, session_id=session_id)

    def landscape_run_ids(self, session_id: str) -> tuple[str, ...]:
        """The session's Landscape run ids that exist in the Landscape ``runs`` table (not merely referenced)."""

        referenced = self._ids(self._sessions, LANDSCAPE_RUN_IDS_OF_SESSION_SQL, session_id=session_id)
        return tuple(run_id for run_id in referenced if self._ids(self._landscape, LANDSCAPE_RUN_EXISTS_SQL, run_id=run_id) == (run_id,))

    def membership_row(self, instance_id: str) -> MembershipRow | None:
        rows = self._sessions.rows(MEMBERSHIP_ROW_SQL, instance_id=instance_id)
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]) != 3:
            raise AcceptanceCheckError("probe_observation")
        found_id, state, lease_expires_at = rows[0]
        if type(found_id) is not str or type(state) is not str or type(lease_expires_at) is not datetime or lease_expires_at.tzinfo is None:
            raise AcceptanceCheckError("probe_observation")
        return MembershipRow(instance_id=found_id, state=state, lease_expires_at=lease_expires_at)
```

- [ ] **Step 5: Regenerate the Container Apps controller around the moved code.**

Keep `:1-25` (the module docstring) byte-for-byte and replace the rest of the file with the following, so the file reads:

```python
# src/elspeth/web/_azure_container_apps_acceptance/controller.py — lines 26 onward (after the unchanged docstring)

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.postgres_observer import (
    BACKEND_PID_SQL,
    DATABASE_NOW_SQL,
    FENCE_EPOCH_SQL,
    FENCE_OWNER_SQL,
    GUIDED_OPERATIONS_SINCE_SQL,
    LANDSCAPE_RUN_EXISTS_SQL,
    LANDSCAPE_RUN_IDS_OF_SESSION_SQL,
    MEMBERSHIP_ROW_SQL,
    RUN_IDS_SQL,
    TERMINATE_OWN_ROLE_BACKENDS_SQL,
    PartitionRecord,
    PostgresEvidenceObserver,
    RoleRevocationPartition,
    SessionFactory,
    SqlReader,
    SqlSession,
    login_sql,
    nologin_sql,
    require_runtime_role,
)
from elspeth.web._acceptance_common.replica_probes import ReplicaAddress, ReplicaController

# The PostgreSQL half (seams, partition, observer, probe SQL) moved to
# _acceptance_common/postgres_observer.py (Task K5) and is re-exported here as
# the same objects: the facade, the observation collector and the tests import
# it from this path, and the Sessions mutation-authority manifest resolves the
# facade's probe seams through this path (_PROBE_SEAM_PROTOCOLS).
__all__ = [
    "BACKEND_PID_SQL",
    "DATABASE_NOW_SQL",
    "FENCE_EPOCH_SQL",
    "FENCE_OWNER_SQL",
    "GUIDED_OPERATIONS_SINCE_SQL",
    "INGRESS_REQUEST_TIMEOUT_SECONDS",
    "LANDSCAPE_RUN_EXISTS_SQL",
    "LANDSCAPE_RUN_IDS_OF_SESSION_SQL",
    "MEMBERSHIP_ROW_SQL",
    "PROBE_TOPOLOGY",
    "RUN_IDS_SQL",
    "TERMINATE_OWN_ROLE_BACKENDS_SQL",
    "ContainerAppsReplicaController",
    "IngressTopology",
    "LabelWeight",
    "PartitionRecord",
    "PlatformCommands",
    "PostgresEvidenceObserver",
    "ProbeReplica",
    "RoleRevocationPartition",
    "SessionFactory",
    "SqlReader",
    "SqlSession",
    "label_url",
    "login_sql",
    "nologin_sql",
    "require_even_label_split",
    "require_runtime_role",
]

INGRESS_REQUEST_TIMEOUT_SECONDS: Final = 240
"""Container Apps HTTP ingress request timeout (platform facts §2.2); fixed, not a property."""

_LABEL_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_DOMAIN_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{0,253}\Z")
_NAME_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,30}[a-z0-9]\Z")
_RESOURCE_GROUP_PATTERN = re.compile(r"[A-Za-z0-9._()-]{1,90}\Z")


def label_url(*, app_name: str, label: str, default_domain: str) -> str:
    """The revision label URL: ``https://<app>---<label>.<environment default domain>`` (facts §2.3, triple dash)."""

    if (
        _NAME_PATTERN.fullmatch(app_name) is None
        or _LABEL_PATTERN.fullmatch(label) is None
        or _DOMAIN_PATTERN.fullmatch(default_domain) is None
    ):
        raise AcceptanceInputError("label URL parts must be lowercase platform identifiers")
    return f"https://{app_name}---{label}.{default_domain}"


@dataclass(frozen=True, slots=True)
class IngressTopology:
    """What the probes need the app's ingress to be; ``sticky`` is only meaningful in Single mode (facts §2.3, C3)."""

    active_revisions_mode: Literal["Single", "Multiple"]
    session_affinity: Literal["none", "sticky"]

    def __post_init__(self) -> None:
        if self.session_affinity == "sticky" and self.active_revisions_mode != "Single":
            raise AcceptanceInputError("session affinity is only supported in single revision mode")


PROBE_TOPOLOGY: Final = IngressTopology(active_revisions_mode="Multiple", session_affinity="none")


@dataclass(frozen=True, slots=True)
class LabelWeight:
    label: str
    weight: int


def require_even_label_split(weights: Sequence[LabelWeight], *, labels: tuple[str, str]) -> None:
    """The probe shape: exactly the two labels, 50/50, so a label URL is the only thing that selects a replica."""

    if len(weights) != 2 or {weight.label for weight in weights} != set(labels) or any(weight.weight != 50 for weight in weights):
        raise AcceptanceCheckError("probe_topology")


# --------------------------------------------------------------------------- ports


class PlatformCommands(ABC):
    """Runs one ``az`` argv (no shell) and returns its stdout; the facade binds it to a bounded subprocess."""

    @abstractmethod
    def run(self, argv: Sequence[str]) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ProbeReplica:
    """One addressable probe replica: its label URL, the revision behind it and the runtime role that revision runs as."""

    address: ReplicaAddress
    revision: str
    role: str

    def __post_init__(self) -> None:
        require_runtime_role(self.role)
        if _LABEL_PATTERN.fullmatch(self.address.name) is None:
            raise AcceptanceInputError("a probe replica is addressed by its label")


class ContainerAppsReplicaController(ReplicaController):
    """The platform port: label-URL addressing, role-revocation partition, grace-0 deactivate."""

    def __init__(
        self,
        *,
        app_name: str,
        resource_group: str,
        replicas: tuple[ProbeReplica, ProbeReplica],
        partition: RoleRevocationPartition,
        platform: PlatformCommands,
    ) -> None:
        if _NAME_PATTERN.fullmatch(app_name) is None or _RESOURCE_GROUP_PATTERN.fullmatch(resource_group) is None:
            raise AcceptanceInputError("app name and resource group must be platform identifiers")
        first, second = replicas
        if first.address.name == second.address.name or first.revision == second.revision or first.role == second.role:
            raise AcceptanceInputError("the two probe replicas must differ in label, revision and runtime role")
        self._app_name = app_name
        self._resource_group = resource_group
        self._replicas = {replica.address.name: replica for replica in replicas}
        self._pair = (first.address, second.address)
        self._partition = partition
        self._platform = platform

    def replicas(self) -> tuple[ReplicaAddress, ReplicaAddress]:
        return self._pair

    def _replica(self, label: str) -> ProbeReplica:
        if label not in self._replicas:
            raise AcceptanceInputError("unknown probe replica label")
        return self._replicas[label]

    def _revision_command(self, action: str, revision: str) -> list[str]:
        return [
            "az",
            "containerapp",
            "revision",
            action,
            "--name",
            self._app_name,
            "--resource-group",
            self._resource_group,
            "--revision",
            revision,
        ]

    def partition_owner(self, replica: str) -> None:
        self._partition.partition(self._replica(replica).role)

    def stop_owner(self, replica: str) -> None:
        self._platform.run(self._revision_command("deactivate", self._replica(replica).revision))

    def restore_owner(self, replica: str) -> None:
        target = self._replica(replica)
        self._partition.restore(target.role)
        self._platform.run(self._revision_command("activate", target.revision))
```

Do not touch `src/elspeth/web/azure_container_apps_acceptance.py`: its `from ._azure_container_apps_acceptance.controller import (` statement at `:65-75`, which names `SqlReader` and `SqlSession`, is what keeps its two seam classes admitted (Step 7 proves it).

- [ ] **Step 6: Run the shared-module, dependency and Container Apps suites; lint and type-check the moved code.**

Run: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -m pytest tests/unit/web/acceptance_common tests/unit/web/azure_container_apps_acceptance tests/unit/web/test_azure_container_apps_runbook_contract.py -n 0 > /tmp/klane-k5-step6.log 2>&1; echo exit=$?; tail -3 /tmp/klane-k5-step6.log`
Expected: `exit=0`; the summary line reads `<n> passed` with no `failed` or `error` (the 26 new ids in `test_postgres_observer.py` included).

Run: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/ruff check src/elspeth/web/_acceptance_common/postgres_observer.py src/elspeth/web/_azure_container_apps_acceptance/controller.py tests/unit/web/acceptance_common/test_postgres_observer.py tests/unit/web/acceptance_common/test_dependencies.py > /tmp/klane-k5-step6-ruff.log 2>&1; echo exit=$?; .venv/bin/ruff format --check src/elspeth/web/_acceptance_common/postgres_observer.py src/elspeth/web/_azure_container_apps_acceptance/controller.py tests/unit/web/acceptance_common/test_postgres_observer.py tests/unit/web/acceptance_common/test_dependencies.py >> /tmp/klane-k5-step6-ruff.log 2>&1; echo exit=$?; cat /tmp/klane-k5-step6-ruff.log`
Expected: `exit=0` twice. If `ruff check` reports only `I001` or `RUF022` ordering, apply `ruff check --select I001,RUF022 --fix` to the named file and rerun; if `ruff format --check` lists a file, run `ruff format` on it and rerun.

Run: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/mypy src/elspeth/web/_acceptance_common/postgres_observer.py src/elspeth/web/_azure_container_apps_acceptance/controller.py src/elspeth/web/azure_container_apps_acceptance.py src/elspeth/web/azure_container_apps_observations.py > /tmp/klane-k5-step6-mypy.log 2>&1; echo exit=$?; tail -1 /tmp/klane-k5-step6-mypy.log`
Expected: `exit=0`, `Success: no issues found in 4 source files` (strict mode; `__all__` is what makes the re-exported names explicit exports for the two importing modules).

- [ ] **Step 7: Mutation-authority manifest (DECISIONS I4), census and trust-tier corpus — prove the move needs no manifest edit.**

K5 adds no Sessions writer and moves no seam class: the manifest (`_TABLE_POLICIES` `:99-121`, `_REVIEWED_WRITERS`, `_NAMED_AUTHORITY_SYMBOLS` `:262`, `_PROBE_SEAM_PROTOCOLS` `:5395-5400`) stays byte-unedited. Prove both halves: the gate selection is unchanged from Step 1, and the instrument that would have caught a changed import still fires.

Run: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -m pytest tests/unit/architecture/test_session_db_mutation_authority.py -k "all_production_sessions_writers or probe_seam or aca_shared_surfaces" -n 0 -q -rx > /tmp/klane-k5-authority-after.log 2>&1; echo exit=$?; grep -c acceptance /tmp/klane-k5-authority-after.log; tail -1 /tmp/klane-k5-authority-after.log; diff <(grep -E '^Unexpected|^Stale' /tmp/klane-k5-authority-before.log) <(grep -E '^Unexpected|^Stale' /tmp/klane-k5-authority-after.log); echo diff_exit=$?`
Expected: `exit=0`; `0`; `2 passed, 263 deselected, 1 xfailed in <n>s`; `diff_exit=0` (the same unexpected/stale counts as before the move).

Write the import-path control (lane scratch, gitignored by `.gitignore:67`):

```python
# .claude/lanes/k5/seam_control.py
"""Control for the probe-seam admission: the facade's import path decides it.

Scans two copies of the Container Apps facade with the manifest's own
scanner: the file as committed (must yield no site) and a copy whose
SqlSession/SqlReader import names the new shared module (must yield the six
seam sites, which the gate would then report as unreviewed writers).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from tests.unit.architecture.test_session_db_mutation_authority import scan_production_writers

REPO = Path(sys.argv[1])
FACADE = "src/elspeth/web/azure_container_apps_acceptance.py"
AS_COMMITTED = """from ._azure_container_apps_acceptance.controller import (
    ContainerAppsReplicaController,
    PlatformCommands,
    PostgresEvidenceObserver,
    ProbeReplica,
    RoleRevocationPartition,
    SqlReader,
    SqlSession,
"""
REWRITTEN = """from ._acceptance_common.postgres_observer import PostgresEvidenceObserver, RoleRevocationPartition, SqlReader, SqlSession
from ._azure_container_apps_acceptance.controller import (
    ContainerAppsReplicaController,
    PlatformCommands,
    ProbeReplica,
"""

source = (REPO / FACADE).read_text(encoding="utf-8")
assert source.count(AS_COMMITTED) == 1, "the facade's controller import changed; the control no longer measures it"
with tempfile.TemporaryDirectory() as scratch:
    for label, text in (("as_committed", source), ("rewritten", source.replace(AS_COMMITTED, REWRITTEN))):
        root = Path(scratch) / label
        (root / FACADE).parent.mkdir(parents=True)
        (root / FACADE).write_text(text, encoding="utf-8")
        sites = scan_production_writers([root / FACADE], anchor=root)
        print(label, len(sites), sorted({site.symbol for site in sites}))
```

Run: `cd "$(git rev-parse --show-toplevel)" && PYTHONPATH="$PWD" .venv/bin/python .claude/lanes/k5/seam_control.py "$PWD" > /tmp/klane-k5-seam-control.log 2>&1; echo exit=$?; cat /tmp/klane-k5-seam-control.log`
Expected (measured on `818d04577` before the move, unchanged after it):
```text
exit=0
as_committed 0 []
rewritten 6 ['_SqlAlchemyReader.rows', '_SqlAlchemyReader.scalar', '_SqlAlchemySession.__init__', '_SqlAlchemySession.execute_scalar']
```

Run: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -m scripts.check_contracts > /tmp/klane-k5-contracts-after.log 2>&1; echo exit=$?; grep -c "_acceptance_common\|_kubernetes_acceptance\|_azure_container_apps_acceptance" /tmp/klane-k5-contracts-after.log; diff /tmp/klane-k5-contracts-before.log /tmp/klane-k5-contracts-after.log; echo diff_exit=$?`
Expected: the same exit code as Step 1; `0`; `diff_exit=0`. (No `--write-census`: `**parameters: object` and `Mapping[str, SessionFactory]` are not soft mappings, and the census has no row for either file.)

- [ ] **Step 8: Write the failing Kubernetes controller tests.**

```python
# tests/unit/web/kubernetes_acceptance/__init__.py
```

(empty file, as `tests/unit/web/azure_container_apps_acceptance/__init__.py` is)

```python
# tests/unit/web/kubernetes_acceptance/test_controller.py
"""The Kubernetes ReplicaController port: its kubectl argv, its partition wiring, its identity refusals, its layering."""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.postgres_observer import (
    BACKEND_PID_SQL,
    TERMINATE_OWN_ROLE_BACKENDS_SQL,
    RoleRevocationPartition,
    SqlSession,
    login_sql,
    nologin_sql,
)
from elspeth.web._acceptance_common.replica_probes import ReplicaAddress
from elspeth.web._kubernetes_acceptance.controller import (
    KubectlCommands,
    KubectlSubprocess,
    KubernetesReplicaController,
    ProbePod,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
PACKAGE = REPO_ROOT / "src" / "elspeth" / "web" / "_kubernetes_acceptance"
ORIGIN_A = "http://127.0.0.1:30452"
ORIGIN_B = "http://127.0.0.1:30453"
FORBIDDEN_PREFIXES = (
    "elspeth.web._azure_container_apps_acceptance",
    "elspeth.web.azure_container_apps_acceptance",
    "elspeth.web.azure_container_apps_observations",
    "elspeth.web._aws_ecs_acceptance",
    "elspeth.web.aws_ecs_acceptance",
    "elspeth.web.app",
)


class _Session(SqlSession):
    """Models the partition's two database sessions: the owner role's kept-open session and the admin's."""

    def __init__(self, log: list[str], name: str) -> None:
        self._log = log
        self._name = name

    def execute_scalar(self, statement: str) -> object:
        self._log.append(f"{self._name}: {statement}")
        if statement == BACKEND_PID_SQL:
            return 4242
        if statement == TERMINATE_OWN_ROLE_BACKENDS_SQL:
            return 1
        return None

    def close(self) -> None:
        self._log.append(f"{self._name}: close")


class _Platform(KubectlCommands):
    """Records each argv, and interleaves it into the shared log so ordering against the partition is observable."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.calls: list[list[str]] = []

    def run(self, argv: Sequence[str]) -> bytes:
        self.calls.append(list(argv))
        self._log.append("kubectl: " + " ".join(argv[1:]))
        return b""


def _factory(log: list[str], name: str) -> Callable[[], SqlSession]:
    def open_session() -> SqlSession:
        return _Session(log, name)

    return open_session


def _partition(log: list[str]) -> RoleRevocationPartition:
    return RoleRevocationPartition(
        admin=_factory(log, "admin"),
        roles={"elspeth_runtime_a": _factory(log, "runtime_a"), "elspeth_runtime_b": _factory(log, "runtime_b")},
    )


def _pod_a() -> ProbePod:
    return ProbePod(address=ReplicaAddress(name="a", origin=ORIGIN_A), deployment="elspeth-web-a", role="elspeth_runtime_a")


def _pod_b() -> ProbePod:
    return ProbePod(address=ReplicaAddress(name="b", origin=ORIGIN_B), deployment="elspeth-web-b", role="elspeth_runtime_b")


def _controller(
    log: list[str],
    platform: _Platform,
    *,
    replicas: tuple[ProbePod, ProbePod] | None = None,
    namespace: str = "default",
) -> KubernetesReplicaController:
    return KubernetesReplicaController(
        namespace=namespace,
        replicas=replicas if replicas is not None else (_pod_a(), _pod_b()),
        partition=_partition(log),
        platform=platform,
    )


def test_replicas_are_the_two_probe_addresses_in_order() -> None:
    log: list[str] = []
    assert _controller(log, _Platform(log)).replicas() == (ReplicaAddress("a", ORIGIN_A), ReplicaAddress("b", ORIGIN_B))


def test_partition_owner_revokes_only_the_owner_runtime_role_and_touches_no_pod() -> None:
    log: list[str] = []
    platform = _Platform(log)
    _controller(log, platform).partition_owner("b")
    assert log == [
        f"runtime_b: {BACKEND_PID_SQL}",
        f"admin: {nologin_sql('elspeth_runtime_b')}",
        "admin: close",
        f"runtime_b: {TERMINATE_OWN_ROLE_BACKENDS_SQL}",
        "runtime_b: close",
    ]
    assert platform.calls == []


def test_stop_owner_force_deletes_the_owner_pods_by_instance_label_with_no_grace() -> None:
    log: list[str] = []
    platform = _Platform(log)
    _controller(log, platform).stop_owner("a")
    assert platform.calls == [
        [
            "kubectl",
            "-n",
            "default",
            "delete",
            "pod",
            "-l",
            "app.kubernetes.io/instance=elspeth-web-a",
            "--grace-period=0",
            "--force",
            "--wait=false",
        ]
    ]
    assert not [entry for entry in log if entry.startswith(("admin:", "runtime_"))], log


def test_restore_owner_restores_login_before_waiting_for_that_deployments_rollout() -> None:
    log: list[str] = []
    platform = _Platform(log)
    _controller(log, platform).restore_owner("b")
    assert log == [
        f"admin: {login_sql('elspeth_runtime_b')}",
        "admin: close",
        "kubectl: -n default rollout status deployment/elspeth-web-b --timeout=300s",
    ]


@pytest.mark.parametrize(
    "second",
    [
        pytest.param(
            ProbePod(address=ReplicaAddress(name="a", origin=ORIGIN_B), deployment="elspeth-web-b", role="elspeth_runtime_b"),
            id="same-label",
        ),
        pytest.param(
            ProbePod(address=ReplicaAddress(name="b", origin=ORIGIN_B), deployment="elspeth-web-a", role="elspeth_runtime_b"),
            id="same-deployment",
        ),
        pytest.param(
            ProbePod(address=ReplicaAddress(name="b", origin=ORIGIN_B), deployment="elspeth-web-b", role="elspeth_runtime_a"),
            id="same-role",
        ),
    ],
)
def test_the_two_probe_pods_must_differ_in_label_deployment_and_runtime_role(second: ProbePod) -> None:
    log: list[str] = []
    with pytest.raises(AcceptanceInputError, match="must differ in label, deployment and runtime role"):
        _controller(log, _Platform(log), replicas=(_pod_a(), second))


def test_a_probe_pod_must_run_as_an_elspeth_runtime_letter_role() -> None:
    with pytest.raises(AcceptanceInputError, match="runtime role must be elspeth_runtime_<letter>"):
        ProbePod(address=ReplicaAddress(name="a", origin=ORIGIN_A), deployment="elspeth-web-a", role="elspeth_probe_a")


@pytest.mark.parametrize(
    ("label", "deployment"),
    [
        pytest.param("a", "Elspeth_Web", id="deployment-not-a-dns-label"),
        pytest.param("a", "-elspeth-web", id="deployment-leading-dash"),
        pytest.param("A", "elspeth-web-a", id="label-uppercase"),
    ],
)
def test_a_probe_pod_is_addressed_by_a_lowercase_label_and_a_kubernetes_name(label: str, deployment: str) -> None:
    with pytest.raises(AcceptanceInputError, match="lowercase label and a Kubernetes deployment name"):
        ProbePod(address=ReplicaAddress(name=label, origin=ORIGIN_A), deployment=deployment, role="elspeth_runtime_a")


def test_the_namespace_must_be_a_kubernetes_name() -> None:
    log: list[str] = []
    with pytest.raises(AcceptanceInputError, match="namespace must be a Kubernetes name"):
        _controller(log, _Platform(log), namespace="Bad Name")


def test_an_unknown_replica_label_is_refused_before_any_side_effect() -> None:
    log: list[str] = []
    controller = _controller(log, _Platform(log))
    for action in (controller.partition_owner, controller.stop_owner, controller.restore_owner):
        with pytest.raises(AcceptanceInputError, match="unknown probe replica label"):
            action("c")
    assert log == []


def test_kubectl_subprocess_refuses_an_argv_that_is_not_kubectl() -> None:
    with pytest.raises(AcceptanceInputError, match="only kubectl commands are run through the platform seam"):
        KubectlSubprocess().run(["sh", "-c", "true"])


def test_kubectl_subprocess_reports_a_failed_command_by_check_name_only(tmp_path: Path) -> None:
    # kubectl absent (OSError) and a kubeconfig file that does not exist (measured: exit 1,
    # "error: stat <path>: no such file or directory") both leave as the one static check name.
    with pytest.raises(AcceptanceCheckError, match=r"\Aacceptance check failed: platform_command\Z"):
        KubectlSubprocess(kubeconfig=tmp_path / "missing-kubeconfig").run(["kubectl", "get", "namespace", "default"])


def test_the_kubernetes_package_never_imports_a_provider_package_or_facade() -> None:
    modules = sorted(PACKAGE.glob("*.py"))
    assert {path.name for path in modules} == {"__init__.py", "controller.py"}
    for path in modules:
        imported: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
                imported.add(node.module)
        offending = sorted(name for name in imported if name.startswith(FORBIDDEN_PREFIXES))
        assert not offending, f"{path.name} imports {offending}"
```

- [ ] **Step 9: Run the controller tests and watch them fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -m pytest tests/unit/web/kubernetes_acceptance -n 0 > /tmp/klane-k5-step9.log 2>&1; echo exit=$?; tail -6 /tmp/klane-k5-step9.log`
Expected: `exit=2`; `ModuleNotFoundError: No module named 'elspeth.web._kubernetes_acceptance'`; `Interrupted: 1 error during collection`.

- [ ] **Step 10: Implement the Kubernetes controller.**

```python
# src/elspeth/web/_kubernetes_acceptance/__init__.py
"""Kubernetes acceptance: the ``ReplicaController`` port for the provider-neutral replica probes.

Only the platform port lives here (``controller``): addressing by one Service
per one-replica Deployment, partition by database-role revocation (the shared
``_acceptance_common.postgres_observer.RoleRevocationPartition``), stop by a
grace-0 forced pod delete, restore by waiting for the Deployment's rollout.
There is no receipt, facade or provider identity: the kind-lane pytest module
``tests/testcontainer/deployment/test_kubernetes_replica_probes.py`` is the
evidence. It never imports a provider package or a facade.
"""
```

```python
# src/elspeth/web/_kubernetes_acceptance/controller.py
"""The Kubernetes ``ReplicaController``: Service-per-Deployment addressing, role-revocation partition, grace-0 pod delete.

- **Addressing.** Two Deployments at one replica each (``elspeth-web-a`` /
  ``elspeth-web-b``), each behind its own Service selecting on
  ``app.kubernetes.io/instance``. The Service is the analogue of a Container
  Apps revision label URL: the address, not a load balancer, picks the
  replica. A replacement pod carries the same instance label, so the address
  survives a pod delete without relabelling.
- **Partition (P3 primary).** Each Deployment runs as its own
  ``elspeth_runtime_<letter>`` role, so ``RoleRevocationPartition`` severs
  exactly one replica from both databases without reaching its release path.
- **Stop (P3 secondary).** ``kubectl delete pod --grace-period=0 --force``: the
  owner gets no drain window. The Deployment schedules a replacement at once,
  which mints a fresh instance id and joins ``web_instances`` as a third row;
  the dead owner's row stays until its lease lapses. A probe keys on the dead
  owner's instance id, never on the row count.
- **Restore.** Restore the role's ``LOGIN``, then wait for the Deployment's
  rollout so the replica behind the address is ready again.
"""

from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.postgres_observer import RoleRevocationPartition, require_runtime_role
from elspeth.web._acceptance_common.replica_probes import ReplicaAddress, ReplicaController

KUBECTL_COMMAND_TIMEOUT_SECONDS: Final = 360.0
"""One kubectl call's ceiling: above ``restore_owner``'s ``rollout status --timeout=300s``."""

INSTANCE_LABEL: Final = "app.kubernetes.io/instance"
"""The pod label each probe Service selects on; its value is the Deployment name."""

_DNS_LABEL_PATTERN = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\Z")
_REPLICA_LABEL_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")


class KubectlCommands(ABC):
    """Runs one ``kubectl`` argv (no shell) and returns its stdout."""

    @abstractmethod
    def run(self, argv: Sequence[str]) -> bytes: ...


class KubectlSubprocess(KubectlCommands):
    """A bounded ``kubectl`` subprocess, argv only; a failure leaves as one static check name, never output."""

    def __init__(self, *, kubeconfig: Path | None = None) -> None:
        self._kubeconfig = kubeconfig

    def run(self, argv: Sequence[str]) -> bytes:
        if not argv or argv[0] != "kubectl":
            raise AcceptanceInputError("only kubectl commands are run through the platform seam")
        command = list(argv) if self._kubeconfig is None else ["kubectl", "--kubeconfig", str(self._kubeconfig), *argv[1:]]
        try:
            completed = subprocess.run(command, capture_output=True, check=False, timeout=KUBECTL_COMMAND_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            raise AcceptanceCheckError("platform_command") from None
        if completed.returncode != 0:
            raise AcceptanceCheckError("platform_command")
        return completed.stdout


@dataclass(frozen=True, slots=True)
class ProbePod:
    """One addressable probe replica: its Service origin, the Deployment behind it and the runtime role it runs as."""

    address: ReplicaAddress
    deployment: str
    role: str

    def __post_init__(self) -> None:
        require_runtime_role(self.role)
        if _REPLICA_LABEL_PATTERN.fullmatch(self.address.name) is None or _DNS_LABEL_PATTERN.fullmatch(self.deployment) is None:
            raise AcceptanceInputError("a probe pod is addressed by a lowercase label and a Kubernetes deployment name")


class KubernetesReplicaController(ReplicaController):
    """The platform port: Service addressing, role-revocation partition, grace-0 forced pod delete."""

    def __init__(
        self,
        *,
        namespace: str,
        replicas: tuple[ProbePod, ProbePod],
        partition: RoleRevocationPartition,
        platform: KubectlCommands,
    ) -> None:
        if _DNS_LABEL_PATTERN.fullmatch(namespace) is None:
            raise AcceptanceInputError("namespace must be a Kubernetes name")
        first, second = replicas
        if first.address.name == second.address.name or first.deployment == second.deployment or first.role == second.role:
            raise AcceptanceInputError("the two probe pods must differ in label, deployment and runtime role")
        self._namespace = namespace
        self._pods = {pod.address.name: pod for pod in replicas}
        self._pair = (first.address, second.address)
        self._partition = partition
        self._platform = platform

    def replicas(self) -> tuple[ReplicaAddress, ReplicaAddress]:
        return self._pair

    def _pod(self, label: str) -> ProbePod:
        if label not in self._pods:
            raise AcceptanceInputError("unknown probe replica label")
        return self._pods[label]

    def partition_owner(self, replica: str) -> None:
        self._partition.partition(self._pod(replica).role)

    def stop_owner(self, replica: str) -> None:
        target = self._pod(replica)
        self._platform.run(
            [
                "kubectl",
                "-n",
                self._namespace,
                "delete",
                "pod",
                "-l",
                f"{INSTANCE_LABEL}={target.deployment}",
                "--grace-period=0",
                "--force",
                "--wait=false",
            ]
        )

    def restore_owner(self, replica: str) -> None:
        target = self._pod(replica)
        self._partition.restore(target.role)
        self._platform.run(
            ["kubectl", "-n", self._namespace, "rollout", "status", f"deployment/{target.deployment}", "--timeout=300s"]
        )
```

- [ ] **Step 11: Run the controller tests, lint, type-check, and compare the trust-tier corpus.**

Run: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -m pytest tests/unit/web/kubernetes_acceptance tests/unit/web/acceptance_common -n 0 > /tmp/klane-k5-step11.log 2>&1; echo exit=$?; tail -3 /tmp/klane-k5-step11.log`
Expected: `exit=0`; `<n> passed` with no `failed` or `error` (the 16 ids of `test_controller.py` included).

Run: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/ruff check src/elspeth/web/_kubernetes_acceptance tests/unit/web/kubernetes_acceptance > /tmp/klane-k5-step11-ruff.log 2>&1; echo exit=$?; .venv/bin/ruff format --check src/elspeth/web/_kubernetes_acceptance tests/unit/web/kubernetes_acceptance >> /tmp/klane-k5-step11-ruff.log 2>&1; echo exit=$?; .venv/bin/mypy src/elspeth/web/_kubernetes_acceptance > /tmp/klane-k5-step11-mypy.log 2>&1; echo exit=$?; tail -1 /tmp/klane-k5-step11-mypy.log`
Expected: `exit=0` three times; `Success: no issues found in 2 source files`.

Run: `cd "$(git rev-parse --show-toplevel)" && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing .venv/bin/elspeth-lints check --rules all --root src/elspeth > /tmp/klane-k5-lints-after.log 2>&1; echo exit=$?; wc -l < /tmp/klane-k5-lints-after.log; grep "_acceptance_common/postgres_observer\|_kubernetes_acceptance\|_azure_container_apps_acceptance/controller" /tmp/klane-k5-lints-after.log; echo grep_exit=$?; diff <(sort /tmp/klane-k5-lints-before.log) <(sort /tmp/klane-k5-lints-after.log); echo diff_exit=$?`
Expected: `exit=1`; the Step 1 line count; `grep_exit=1` (no finding names a touched module); `diff_exit=0`. A finding on `KubectlSubprocess.run` or `postgres_observer.py` is a defect in this task's code: fix the code, never allowlist it here (the operator signs allowlist entries).

- [ ] **Step 12: Write the kind-lane probe module.**

```python
# tests/testcontainer/deployment/test_kubernetes_replica_probes.py
"""The replicas > 1 probes P1-P4b on Kubernetes: two one-replica Deployments on kind (marker ``kind``).

Topology (``deploy/kubernetes/overlays/kind-acceptance``): Deployments
``elspeth-web-a`` / ``elspeth-web-b`` each run as their own PostgreSQL runtime
role (``elspeth_runtime_a`` / ``elspeth_runtime_b``) behind their own NodePort
Service (30452 / 30453), sharing the RWX share and both databases. The
per-Deployment Service plays the part a revision label URL plays on Container
Apps: the address selects the replica, so the driver can fire one request at
each.

The decision tables and the P3/P4a collectors are the provider-neutral ones
the Container Apps lane scores (``_acceptance_common/replica_probes.py``,
``azure_container_apps_observations.collect_takeover`` / ``collect_progress``);
the only Kubernetes code is ``_kubernetes_acceptance/controller.py``. The
evidence is this module's result in the ``kubernetes-kind`` job; there is no
Kubernetes receipt.

A forced pod delete makes the Deployment schedule a replacement at once. The
replacement mints a fresh instance id and joins ``web_instances`` as a THIRD
row while the dead owner's row waits out its lease. No relabel is needed: the
Service selects ``app.kubernetes.io/instance``, which the replacement's pod
template carries.

Selected only by ``scripts/cicd/kubernetes-kind-smoke.sh`` (``-m kind -n 0``).
"""

from __future__ import annotations

import csv
import io
import json
import os
import secrets
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml
from sqlalchemy import Engine, create_engine, text

from elspeth.web._acceptance_common.http_client import AcceptanceCredentials, AcceptanceHttpClient
from elspeth.web._acceptance_common.postgres_observer import PostgresEvidenceObserver, RoleRevocationPartition, SqlReader, SqlSession
from elspeth.web._acceptance_common.replica_probes import (
    DEFAULT_TRIALS,
    ProbeRequest,
    ReplicaAddress,
    ReplicaProbeDriver,
    decide_cross_replica_progress,
    decide_fence_conflict,
    decide_lease_takeover,
    decide_run_start,
    record_owner_affine_progress,
)
from elspeth.web._azure_container_apps_acceptance.evidence import cross_replica_progress_observation, lease_takeover_observation
from elspeth.web._kubernetes_acceptance.controller import KubectlSubprocess, KubernetesReplicaController, ProbePod
from elspeth.web.azure_container_apps_observations import (
    Capture,
    PhysicalSinkOracle,
    Polling,
    collect_progress,
    collect_takeover,
    observation_document,
)
from tests.testcontainer.deployment.kind_harness import REPO_ROOT, KindCluster

pytestmark = pytest.mark.kind

OVERLAY = REPO_ROOT / "deploy" / "kubernetes" / "overlays" / "kind-acceptance"
EXAMPLE = REPO_ROOT / "examples" / "threshold_gate"
NAMESPACE = "default"
ORIGIN_A = "http://127.0.0.1:30452"
ORIGIN_B = "http://127.0.0.1:30453"
DEPLOYMENT_A = "elspeth-web-a"
DEPLOYMENT_B = "elspeth-web-b"
ROLE_A = "elspeth_runtime_a"  # collect_takeover partitions this literal role (azure_container_apps_observations.py:529)
ROLE_B = "elspeth_runtime_b"
RUNTIME_ROLES = ("elspeth_runtime", ROLE_A, ROLE_B)
JOBS = ("elspeth-provision-storage", "elspeth-schema-init")
SHARE_OUTPUTS = Path("/mnt/elspeth/data/outputs")  # ELSPETH_WEB__DATA_DIR (K1 ConfigMap) + outputs/<session> (paths.py:108-135)
TAKEOVER_ROWS = 200_000
COMPOSER_ENV_FILE = "ELSPETH_KIND_COMPOSER_ENV_FILE"
COMPOSER_SECRET = "elspeth-kind-composer"
FIXED_SCHEMA = {"mode": "fixed", "fields": ["id: int", "name: str", "amount: int", "category: str"]}
# Runs inside the survivor pod: print one file off the shared volume, or nothing if it is not there yet.
READ_SHARE_FILE = (
    "import pathlib, sys\n"
    "path = pathlib.Path(sys.argv[1])\n"
    "sys.stdout.buffer.write(path.read_bytes() if path.is_file() else b'')\n"
)


# --------------------------------------------------------------------------- live seams (test-side; src stays seam-free)


class _AutocommitSession(SqlSession):
    def __init__(self, engine: Engine) -> None:
        self._connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")

    def execute_scalar(self, statement: str) -> object:
        return self._connection.execute(text(statement)).scalar()

    def close(self) -> None:
        self._connection.close()


class _EngineReader(SqlReader):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def scalar(self, statement: str, **parameters: object) -> object:
        with self._engine.connect() as connection:
            return connection.execute(text(statement), parameters).scalar()

    def rows(self, statement: str, **parameters: object) -> tuple[tuple[object, ...], ...]:
        with self._engine.connect() as connection:
            return tuple(tuple(row) for row in connection.execute(text(statement), parameters).all())


def _sessions_on(engine: Engine) -> Callable[[], SqlSession]:
    def open_session() -> SqlSession:
        return _AutocommitSession(engine)

    return open_session


@dataclass(frozen=True)
class _PodSinkOracle(PhysicalSinkOracle):
    """The physical CSV duplicate-write oracle, reading the share through a pod.

    ``PhysicalSinkOracle`` reads the sink from the runner's own filesystem,
    which on Container Apps is the operator's NFS mount at the workload's
    path. The kind share is a hostPath inside the kind node container, so this
    oracle reads the same bytes with ``kubectl exec`` in the survivor pod; the
    binding to the run's persisted sink path (``bind``) is inherited unchanged.
    A trailing line without its newline is a row still being written and is
    not counted.
    """

    kubeconfig: Path
    deployment: str

    def _keys_through_pod(self) -> list[str]:
        completed = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(self.kubeconfig),
                "exec",
                f"deployment/{self.deployment}",
                "--",
                "/opt/venv/bin/python",
                "-c",
                READ_SHARE_FILE,
                str(self.path),
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
        content = completed.stdout.decode("utf-8")
        complete = content if content.endswith("\n") else content[: content.rfind("\n") + 1]
        return [row[self.key_field] for row in csv.DictReader(io.StringIO(complete), strict=True)]

    def has_effects(self) -> bool:
        return bool(self._keys_through_pod())

    def duplicates(self) -> int:
        keys = self._keys_through_pod()
        assert keys, f"{self.path} carries no rows: the duplicate-write oracle has nothing to count"
        return len(keys) - len(set(keys))


# --------------------------------------------------------------------------- cluster helpers


def _wait_job(cluster: KindCluster, name: str, *, timeout: float = 600.0) -> None:
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


def _wait_web_pods_gone(cluster: KindCluster, *, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = json.loads(cluster.kubectl("get", "pods", "-l", "app.kubernetes.io/name=elspeth-web", "-o", "json"))["items"]
        if not items:
            return
        time.sleep(2)
    raise AssertionError(f"web pods still present after {timeout}s")


def _instance_or_none(origin: str) -> str | None:
    try:
        response = httpx.get(f"{origin}/api/system/status", timeout=10.0)
    except httpx.HTTPError:
        return None
    if response.status_code != 200 or "X-Elspeth-Instance" not in response.headers:
        return None
    return response.headers["X-Elspeth-Instance"]


def _wait_ready(origin: str, *, timeout: float = 300.0) -> None:
    deadline = time.monotonic() + timeout
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{origin}/api/ready", timeout=5.0)
            if response.status_code == 200 and response.json()["ready"] is True:
                return
            last = response.text
        except httpx.HTTPError as exc:
            last = repr(exc)
        time.sleep(3)
    raise AssertionError(f"{origin} never reported ready within {timeout}s; last answer: {last}")


# --------------------------------------------------------------------------- the lane


@dataclass(frozen=True)
class ProbeLane:
    cluster: KindCluster
    controller: KubernetesReplicaController
    partition: RoleRevocationPartition
    observer: PostgresEvidenceObserver
    sessions: SqlReader
    credentials: AcceptanceCredentials

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials.bearer_token}"}

    def client(self, origin: str) -> AcceptanceHttpClient:
        return AcceptanceHttpClient(origin=origin, credentials=self.credentials)

    def driver(self) -> ReplicaProbeDriver:
        return ReplicaProbeDriver(controller=self.controller, observer=self.observer, client_factory=self.client)


@pytest.fixture(scope="module")
def lane(kind_cluster: KindCluster) -> Iterator[ProbeLane]:
    # K4's shared-state Deployment runs as elspeth_runtime on the same two
    # databases. Left up it would hold membership leases and run the orphan
    # sweep P3 attributes to the survivor, so it goes before the probe pair.
    kind_cluster.kubectl("delete", "deployment/elspeth-web", "service/elspeth-web", "--ignore-not-found=true", "--wait=true")
    _wait_web_pods_gone(kind_cluster)
    composer_env = os.environ.get(COMPOSER_ENV_FILE)
    if composer_env:
        kind_cluster.kubectl("create", "secret", "generic", COMPOSER_SECRET, f"--from-env-file={composer_env}")
    kind_cluster.kubectl("apply", "-k", str(OVERLAY))
    for job in JOBS:
        _wait_job(kind_cluster, job)
    for deployment, origin in ((DEPLOYMENT_A, ORIGIN_A), (DEPLOYMENT_B, ORIGIN_B)):
        kind_cluster.kubectl("rollout", "status", f"deployment/{deployment}", "--timeout=600s")
        _wait_ready(origin)

    admin = create_engine(kind_cluster.database_url("postgres", "elspeth_sessions"), pool_pre_ping=True)
    landscape = create_engine(kind_cluster.database_url("postgres", "elspeth_landscape"), pool_pre_ping=True)
    role_engines = {
        role: create_engine(kind_cluster.database_url(role, "elspeth_sessions"), pool_pre_ping=True) for role in (ROLE_A, ROLE_B)
    }
    partition = RoleRevocationPartition(
        admin=_sessions_on(admin),
        roles={role: _sessions_on(engine) for role, engine in role_engines.items()},
    )
    try:
        # Exactly the two probe roles hold web connections (before this test
        # opens any role engine): a revocation then severs one replica only.
        with admin.connect() as connection:
            logged_in = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT DISTINCT usename FROM pg_stat_activity "
                        "WHERE datname IN ('elspeth_sessions', 'elspeth_landscape') AND usename = ANY(:roles)"
                    ),
                    {"roles": list(RUNTIME_ROLES)},
                ).all()
            }
        assert logged_in == {ROLE_A, ROLE_B}, logged_in

        for deployment, origin in ((DEPLOYMENT_A, ORIGIN_A), (DEPLOYMENT_B, ORIGIN_B)):
            status = httpx.get(f"{origin}/api/system/status", timeout=10.0).json()
            assert status["deployment_target"] == "kubernetes", status
            assert status["deployment_replica"].startswith(f"{deployment}-"), status
        first, second = _instance_or_none(ORIGIN_A), _instance_or_none(ORIGIN_B)
        assert first is not None and second is not None and first != second, (first, second)

        # One bearer token for every request: each replica validates it
        # (shared ELSPETH_WEB__SECRET_KEY, K4 fixture) and no probe pays a login.
        registered = httpx.post(
            f"{ORIGIN_A}/api/auth/register",
            json={
                "username": f"kind-probe-{uuid.uuid4().hex[:8]}",
                "password": secrets.token_urlsafe(18),
                "display_name": "kind replica probes",
            },
            timeout=30.0,
        )
        assert registered.status_code == 200, registered.text
        credentials = AcceptanceCredentials(mode="bearer", bearer_token=registered.json()["access_token"])

        controller = KubernetesReplicaController(
            namespace=NAMESPACE,
            replicas=(
                ProbePod(address=ReplicaAddress(name="a", origin=ORIGIN_A), deployment=DEPLOYMENT_A, role=ROLE_A),
                ProbePod(address=ReplicaAddress(name="b", origin=ORIGIN_B), deployment=DEPLOYMENT_B, role=ROLE_B),
            ),
            partition=partition,
            platform=KubectlSubprocess(kubeconfig=kind_cluster.kubeconfig),
        )
        sessions = _EngineReader(admin)
        yield ProbeLane(
            cluster=kind_cluster,
            controller=controller,
            partition=partition,
            observer=PostgresEvidenceObserver(sessions=sessions, landscape=_EngineReader(landscape)),
            sessions=sessions,
            credentials=credentials,
        )
    finally:
        for role in (ROLE_A, ROLE_B):
            partition.restore(role)
        kind_cluster.kubectl(
            "delete",
            f"deployment/{DEPLOYMENT_A}",
            f"deployment/{DEPLOYMENT_B}",
            f"service/{DEPLOYMENT_A}",
            f"service/{DEPLOYMENT_B}",
            "--ignore-not-found=true",
            "--wait=true",
        )
        for engine in (admin, landscape, *role_engines.values()):
            engine.dispose()


# --------------------------------------------------------------------------- session preparation (through replica a)


def _post(lane: ProbeLane, path: str, **request: object) -> httpx.Response:
    return httpx.post(f"{ORIGIN_A}{path}", headers=lane.headers, timeout=120.0, **request)


def _new_session(lane: ProbeLane, title: str) -> str:
    created = _post(lane, "/api/sessions", json={"title": title})
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _prepared_session(lane: ProbeLane, title: str, pipeline_yaml: str, source_bytes: bytes) -> str:
    session_id = _new_session(lane, title)
    uploaded = _post(lane, f"/api/sessions/{session_id}/blobs", files={"file": ("input.csv", source_bytes, "text/csv")})
    assert uploaded.status_code == 201, uploaded.text
    imported = _post(
        lane,
        f"/api/sessions/{session_id}/state/yaml",
        json={"yaml": pipeline_yaml, "source_blob_ids": {"primary": uploaded.json()["id"]}},
    )
    assert imported.status_code == 200, imported.text
    return session_id


def _threshold_gate_yaml() -> str:
    """``examples/threshold_gate`` in the import route's shape (sinks under ``outputs/``; K4 measured it)."""
    doc = yaml.safe_load((EXAMPLE / "settings.yaml").read_text(encoding="utf-8"))
    for sink in doc["sinks"].values():
        sink["options"]["path"] = f"outputs/{Path(sink['options']['path']).name}"
    return yaml.safe_dump(doc, sort_keys=False)


def _takeover_yaml() -> str:
    """One csv source straight to ONE csv sink: the shape ``PhysicalSinkOracle.bind`` requires."""
    return yaml.safe_dump(
        {
            "sources": {
                "primary": {
                    "plugin": "csv",
                    "on_success": "takeover",
                    "options": {"path": "input.csv", "schema": FIXED_SCHEMA, "on_validation_failure": "discard"},
                }
            },
            "sinks": {
                "takeover": {
                    "plugin": "csv",
                    "on_write_failure": "discard",
                    "options": {"path": "outputs/takeover.csv", "schema": FIXED_SCHEMA},
                }
            },
        },
        sort_keys=False,
    )


def _takeover_rows() -> bytes:
    lines = ["id,name,amount,category"]
    lines.extend(f"{index},row{index},{index % 997},retail" for index in range(1, TAKEOVER_ROWS + 1))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _guided_trial(lane: ProbeLane, index: int) -> tuple[str, dict[str, object]]:
    """A fresh session with its first guided turn seeded; the body answers that turn (no planner call)."""
    session_id = _new_session(lane, f"p1-{index}")
    started = _post(
        lane,
        f"/api/sessions/{session_id}/guided/start",
        json={"operation_id": str(uuid.uuid4()), "profile": "live", "intent": "Read a CSV file and write every row to a CSV output."},
    )
    assert started.status_code == 200, started.text
    turn_token = started.json()["next_turn"]["turn_token"]
    return session_id, {"operation_id": str(uuid.uuid4()), "turn_token": turn_token, "chosen": ["csv"]}


# --------------------------------------------------------------------------- the probes, in execution order


def test_p1_concurrent_guided_operations_end_in_exactly_one_fence_conflict(lane: ProbeLane) -> None:
    prepared = [_guided_trial(lane, index) for index in range(DEFAULT_TRIALS)]
    driver = lane.driver()
    trials = [
        driver.fence_conflict_trial(session_id, ProbeRequest("POST", f"/api/sessions/{session_id}/guided/respond", body))
        for session_id, body in prepared
    ]
    result = decide_fence_conflict(trials, required_trials=DEFAULT_TRIALS)
    assert result.mechanism == "session_operation_fence", result
    assert result.outcome == "pass", result.reasons


def test_p2_concurrent_run_starts_end_in_one_run_and_one_fence_refusal(lane: ProbeLane) -> None:
    source = (EXAMPLE / "input.csv").read_bytes()
    session_ids = [_prepared_session(lane, f"p2-{index}", _threshold_gate_yaml(), source) for index in range(DEFAULT_TRIALS)]
    driver = lane.driver()
    trials = [
        driver.run_start_trial(session_id, ProbeRequest("POST", f"/api/sessions/{session_id}/execute", {}))
        for session_id in session_ids
    ]
    result = decide_run_start(trials, required_trials=DEFAULT_TRIALS)
    assert result.mechanism == "session_operation_fence_execute", result
    assert result.outcome == "pass", result.reasons


def test_p4b_owner_affine_progress_is_recorded_and_cannot_pass() -> None:
    result = record_owner_affine_progress(mitigation="single_revision_sticky_sessions")
    assert (result.probe, result.outcome, result.mechanism) == ("P4b", "cannot_pass", "owner_affine"), result


def test_p3_a_survivor_takes_over_a_role_revoked_owner_only_after_its_leases_expire(lane: ProbeLane, tmp_path: Path) -> None:
    _wait_ready(ORIGIN_A)
    _wait_ready(ORIGIN_B)
    session_id = _prepared_session(lane, "p3", _takeover_yaml(), _takeover_rows())
    sink = _PodSinkOracle(
        path=SHARE_OUTPUTS / session_id / "takeover.csv",
        key_field="id",
        kubeconfig=lane.cluster.kubeconfig,
        deployment=DEPLOYMENT_B,
    )
    with lane.client(ORIGIN_A) as owner, lane.client(ORIGIN_B) as survivor:
        observation = collect_takeover(
            owner=owner,
            survivor=survivor,
            session_id=session_id,
            sessions=lane.sessions,
            observer=lane.observer,
            partition=lane.partition,
            sink=sink,
            capture=Capture(tmp_path / "p3-evidence"),
            polling=Polling(interval=2.0, timeout=600.0),
        )
    result = decide_lease_takeover(lease_takeover_observation(observation_document(observation)))
    assert result.mechanism == "role_revocation_lease_expiry", result
    assert result.outcome == "pass", result.reasons


def test_stop_owner_force_deletes_the_owner_and_its_replacement_joins_as_a_third_member(lane: ProbeLane) -> None:
    _wait_ready(ORIGIN_A)
    _wait_ready(ORIGIN_B)
    dead, survivor = _instance_or_none(ORIGIN_A), _instance_or_none(ORIGIN_B)
    assert dead is not None and survivor is not None, (dead, survivor)
    lane.controller.stop_owner("a")
    lane.controller.restore_owner("a")
    deadline = time.monotonic() + 300
    replacement = _instance_or_none(ORIGIN_A)
    while replacement in {None, dead} and time.monotonic() < deadline:
        time.sleep(3)
        replacement = _instance_or_none(ORIGIN_A)
    assert replacement is not None and replacement != dead, "the replacement pod never answered on elspeth-web-a"
    _wait_ready(ORIGIN_A)
    rows = lane.sessions.rows(
        "SELECT instance_id, state FROM web_instances WHERE instance_id IN (:dead, :survivor, :replacement)",
        dead=dead,
        survivor=survivor,
        replacement=replacement,
    )
    states = {row[0]: row[1] for row in rows}
    assert set(states) == {dead, survivor, replacement}, states
    assert states[survivor] == "active" and states[replacement] == "active", states


def test_p4a_progress_written_through_one_replica_is_visible_through_the_other(lane: ProbeLane, tmp_path: Path) -> None:
    if not os.environ.get(COMPOSER_ENV_FILE):
        pytest.skip(
            f"P4a measures composer message visibility and needs a composer provider in the pods; "
            f"set {COMPOSER_ENV_FILE} to an env file of composer settings (open question on Task K5)"
        )
    _wait_ready(ORIGIN_A)
    _wait_ready(ORIGIN_B)
    session_id = _prepared_session(lane, "p4a", _threshold_gate_yaml(), (EXAMPLE / "input.csv").read_bytes())
    with lane.client(ORIGIN_A) as owner, lane.client(ORIGIN_B) as reader:
        observation = collect_progress(
            owner=owner,
            reader=reader,
            session_id=session_id,
            capture=Capture(tmp_path / "p4a-evidence"),
            polling=Polling(interval=2.0, timeout=180.0),
        )
    result = decide_cross_replica_progress(cross_replica_progress_observation(observation_document(observation)))
    assert result.mechanism == "postgresql_and_nfs", result
    assert result.outcome == "pass", result.reasons
```

- [ ] **Step 13: Run the probe module in the kind lane and watch the fixture fail on the missing overlay.**

Run: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -m pytest tests/testcontainer/deployment/test_kubernetes_replica_probes.py -m kind -n 0 --collect-only -q > /tmp/klane-k5-step13-collect.log 2>&1; echo exit=$?; tail -2 /tmp/klane-k5-step13-collect.log`
Expected: `exit=0`; `6 tests collected` (the module imports cleanly; the default `-m` without `-m kind` would deselect all six).

Run: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/ruff check tests/testcontainer/deployment/test_kubernetes_replica_probes.py > /tmp/klane-k5-step13-ruff.log 2>&1; echo exit=$?; .venv/bin/ruff format --check tests/testcontainer/deployment/test_kubernetes_replica_probes.py >> /tmp/klane-k5-step13-ruff.log 2>&1; echo exit=$?; cat /tmp/klane-k5-step13-ruff.log`
Expected: `exit=0` twice. If `ruff format --check` lists the module, run `ruff format` on it and rerun (K4's `conftest.py` and `kind_harness.py` are not touched by this task).

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/cicd/kubernetes-kind-smoke.sh -k replica_probes > /tmp/klane-k5-step13.log 2>&1; echo exit=$?; grep -E '^exit=|passed|error|must build at directory' /tmp/klane-k5-step13.log`
Expected: `exit=1`; the smoke line `exit=1 wall=<n>s log=<path>`; five ERRORs at setup of `lane` with `AssertionError: kubectl apply -k <repo>/deploy/kubernetes/overlays/kind-acceptance failed (exit=1):` followed by `error: must build at directory: not a valid directory: evalsymlink failure on '<repo>/deploy/kubernetes/overlays/kind-acceptance' : lstat <repo>/deploy/kubernetes/overlays/kind-acceptance: no such file or directory`; summary `1 passed, 5 errors` (P4b needs no cluster).

- [ ] **Step 14: Write the kind-acceptance overlay and render it offline.**

```yaml
# deploy/kubernetes/overlays/kind-acceptance/kustomization.yaml
# K5 replica-probe topology on kind (DECISIONS K13): two one-replica
# Deployments, each on its own PostgreSQL runtime role and its own NodePort
# Service, sharing the RWX share, the ConfigMap and both databases. A Service
# per Deployment is the Kubernetes analogue of a Container Apps revision label
# URL: the address selects the replica.
#
#   shared/     kind-test minus its Deployment and Service: ConfigMap, PVC, both Jobs
#   replica-a/  Deployment + Service elspeth-web-a, Secret elspeth-web-secrets-a (elspeth_runtime_a), NodePort 30452
#   replica-b/  Deployment + Service elspeth-web-b, Secret elspeth-web-secrets-b (elspeth_runtime_b), NodePort 30453
#
# Every child builds on ../../kind-test, so the image, both REPLACE_PER_ROLLOUT
# replacements and the hostPath PV binding are K4's values. No Secret is
# listed: the harness fixture (tests/testcontainer/deployment/conftest.py)
# creates elspeth-web-secrets-a|b with per-session values. The optional Secret
# elspeth-kind-composer exists only when ELSPETH_KIND_COMPOSER_ENV_FILE is set
# (P4a). Rendered with kubectl v1.37.0 / kustomize v5.8.1: 8 objects.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - shared
  - replica-a
  - replica-b
```

```yaml
# deploy/kubernetes/overlays/kind-acceptance/shared/kustomization.yaml
# The objects both probe replicas share: kind-test's ConfigMap, PVC and the two
# Jobs. Its Deployment and Service are removed here; replica-a/ and replica-b/
# supply one each.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../kind-test
patches:
  - patch: |-
      $patch: delete
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: elspeth-web
  - patch: |-
      $patch: delete
      apiVersion: v1
      kind: Service
      metadata:
        name: elspeth-web
```

```yaml
# deploy/kubernetes/overlays/kind-acceptance/replica-a/kustomization.yaml
# Probe replica "a": kind-test's Deployment and Service only (the shared
# objects are deleted before nameSuffix runs, so envFrom keeps naming the one
# shared ConfigMap), renamed elspeth-web-a, one replica, runtime role
# elspeth_runtime_a via Secret elspeth-web-secrets-a, and selected by
# app.kubernetes.io/instance so the Service reaches this Deployment's pod and
# its replacement only. The instance label is also what
# KubernetesReplicaController.stop_owner deletes by.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../kind-test
nameSuffix: -a
labels:
  - pairs:
      app.kubernetes.io/instance: elspeth-web-a
    includeSelectors: true
    includeTemplates: true
patches:
  - patch: |-
      $patch: delete
      apiVersion: v1
      kind: ConfigMap
      metadata:
        name: elspeth-web-config
  - patch: |-
      $patch: delete
      apiVersion: v1
      kind: PersistentVolumeClaim
      metadata:
        name: elspeth-state
  - patch: |-
      $patch: delete
      apiVersion: batch/v1
      kind: Job
      metadata:
        name: elspeth-provision-storage
  - patch: |-
      $patch: delete
      apiVersion: batch/v1
      kind: Job
      metadata:
        name: elspeth-schema-init
  - target:
      kind: Deployment
      name: elspeth-web
    patch: |-
      - op: replace
        path: /spec/replicas
        value: 1
      - op: replace
        path: /spec/template/spec/containers/0/envFrom/1/secretRef/name
        value: elspeth-web-secrets-a
      - op: add
        path: /spec/template/spec/containers/0/envFrom/-
        value:
          secretRef:
            name: elspeth-kind-composer
            optional: true
  - target:
      kind: Service
      name: elspeth-web
    patch: |-
      - op: replace
        path: /spec/ports/0/nodePort
        value: 30452
```

```yaml
# deploy/kubernetes/overlays/kind-acceptance/replica-b/kustomization.yaml
# Probe replica "b": kind-test's Deployment and Service only (the shared
# objects are deleted before nameSuffix runs, so envFrom keeps naming the one
# shared ConfigMap), renamed elspeth-web-b, one replica, runtime role
# elspeth_runtime_b via Secret elspeth-web-secrets-b, and selected by
# app.kubernetes.io/instance so the Service reaches this Deployment's pod and
# its replacement only. The instance label is also what
# KubernetesReplicaController.stop_owner deletes by.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../kind-test
nameSuffix: -b
labels:
  - pairs:
      app.kubernetes.io/instance: elspeth-web-b
    includeSelectors: true
    includeTemplates: true
patches:
  - patch: |-
      $patch: delete
      apiVersion: v1
      kind: ConfigMap
      metadata:
        name: elspeth-web-config
  - patch: |-
      $patch: delete
      apiVersion: v1
      kind: PersistentVolumeClaim
      metadata:
        name: elspeth-state
  - patch: |-
      $patch: delete
      apiVersion: batch/v1
      kind: Job
      metadata:
        name: elspeth-provision-storage
  - patch: |-
      $patch: delete
      apiVersion: batch/v1
      kind: Job
      metadata:
        name: elspeth-schema-init
  - target:
      kind: Deployment
      name: elspeth-web
    patch: |-
      - op: replace
        path: /spec/replicas
        value: 1
      - op: replace
        path: /spec/template/spec/containers/0/envFrom/1/secretRef/name
        value: elspeth-web-secrets-b
      - op: add
        path: /spec/template/spec/containers/0/envFrom/-
        value:
          secretRef:
            name: elspeth-kind-composer
            optional: true
  - target:
      kind: Service
      name: elspeth-web
    patch: |-
      - op: replace
        path: /spec/ports/0/nodePort
        value: 30453
```

Run: `cd "$(git rev-parse --show-toplevel)" && PATH="$PWD/.claude/lanes/k8s/bin:$PATH" kubectl kustomize deploy/kubernetes/overlays/kind-acceptance > /tmp/klane-k5-step14-render.log 2>&1; echo exit=$?; grep -c '^kind:' /tmp/klane-k5-step14-render.log; grep -n '^  name: \|nodePort:\|name: elspeth-web-secrets-\|name: elspeth-kind-composer\|replicas:\|REPLACE_PER_ROLLOUT' /tmp/klane-k5-step14-render.log; for dir in shared replica-a replica-b; do PATH="$PWD/.claude/lanes/k8s/bin:$PATH" kubectl kustomize "deploy/kubernetes/overlays/kind-acceptance/$dir" > "/tmp/klane-k5-step14-$dir.log" 2>&1; echo "$dir exit=$?"; done`
Expected (measured with kubectl v1.37.0 over K1's base and K4's kind-test): `exit=0`; `8`; names `elspeth-web-config`, `elspeth-web-a`, `elspeth-web-b` (Service), `elspeth-state`, `elspeth-web-a`, `elspeth-web-b` (Deployment), `elspeth-provision-storage`, `elspeth-schema-init`; `nodePort: 30452` and `nodePort: 30453`; `replicas: 1` twice; `name: elspeth-web-secrets-a` and `name: elspeth-web-secrets-b` once each; `name: elspeth-kind-composer` twice; no `REPLACE_PER_ROLLOUT`; then `shared exit=0`, `replica-a exit=0`, `replica-b exit=0`.

- [ ] **Step 15: Run the whole kind lane (the real-PostgreSQL proof for this task) and watch it pass.**

This is the F7 run for K5: the moved probe SQL and the role partition execute against PostgreSQL 16 only here (no `tests/testcontainer/web` test imports `RoleRevocationPartition` or `PostgresEvidenceObserver`, measured with `grep -rln` over `tests/testcontainer` and `tests/integration`). The `-m testcontainer` selection is unaffected by this task and is run by Step 16's gate.

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/cicd/kubernetes-kind-smoke.sh -rs > /tmp/klane-k5-step15.log 2>&1; echo exit=$?; grep -E '^exit=|passed|failed|error|SKIPPED' /tmp/klane-k5-step15.log; PATH="$PWD/.claude/lanes/k8s/bin:$PATH" kind get clusters`
Expected: `exit=0`; the smoke line `exit=0 wall=<n>s log=<path>`; `8 passed, 1 skipped` (K4's three proofs plus P1, P2, P4b, P3 and the stop/restore test; the skip is P4a with the reason `P4a measures composer message visibility and needs a composer provider in the pods; set ELSPETH_KIND_COMPOSER_ENV_FILE to an env file of composer settings (open question on Task K5)`); `kind get clusters` prints nothing. The printed wall time must stay under 30 minutes (half of the `kubernetes-kind` job's `timeout-minutes: 60`); if it does not, report the figure rather than raising the timeout.

If P3 fails with `probe_before_expiry_window_missed`, the takeover pipeline finished before the partition: raise `TAKEOVER_ROWS` (the blob route admits 100 MiB, `config.py:440`) and rerun; do not shorten a lease. If P1 fails with `winners_not_distinct_across_run` or a `dispatch_spread_ms` reason, rerun once with `-k test_p1` to separate a scheduling artefact from a fence defect, and report both runs.

With a composer provider env file available to the operator, the P4a arm is proven the same way: `ELSPETH_KIND_COMPOSER_ENV_FILE=<operator env file> scripts/cicd/kubernetes-kind-smoke.sh -k "replica_probes and (p4a or p4b)"` → `exit=0`, `2 passed` (the fixture creates Secret `elspeth-kind-composer` before the Deployments start).

- [ ] **Step 16: Run the full pre-merge gate on the frozen tree.**

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/full-suite-gate.sh --execute --detach > /tmp/klane-k5-gate.log 2>&1; echo exit=$?; cat /tmp/klane-k5-gate.log`
Expected: `exit=0` and the printed log directory and `.done` path. Poll until the `.done` file exists, then read `summary.txt` in that directory: `frozen=YES`; the `pytest` and `testcontainer` stage exit codes are `0`; the `ruff`, `mypy` and `contracts` stages match what Step 1 recorded for this working tree (the contracts stage's only drift is the pre-existing `web/interpretation_state.py`); the `lints` stage exit is `1` with the corpus identical to `/tmp/klane-k5-lints-after.log`. A red in `e2e/recovery`, `integration/pipeline` or `unit/engine/orchestrator` is re-run with `-n 0` before it is attributed to this task (AGENTS.md § Gotchas); a red `testcontainer` stage is compared with the same stage on the base commit, because that selection is not green on every base (the sink-effect lock-order proof reds intermittently) and this task changes nothing it imports.

- [ ] **Step 17: Read the working tree before staging.**

Run: `cd "$(git rev-parse --show-toplevel)" && git status --short > /tmp/klane-k5-status.log 2>&1; echo exit=$?; cat /tmp/klane-k5-status.log`
Expected: `exit=0`; the eleven created paths of this task's commit show as `??` and the two modified paths (`src/elspeth/web/_azure_container_apps_acceptance/controller.py`, `tests/unit/web/acceptance_common/test_dependencies.py`) as ` M`, plus whatever a sibling lane has touched, which Step 18 does not stage. Nothing under `.claude/lanes/` is committed.

- [ ] **Step 18: Commit.**

Stage first, then run the safety check, then commit: `scripts/branch-safety-check.sh` inspects the STAGED set, so a check run before `git add` inspects an index without this task's files. The eleven created files get `git add -N` because a commit pathspec only selects paths the index knows; then all 13 paths are staged by name (file paths only, never a directory), the check runs, and the message goes before `--`.

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N src/elspeth/web/_acceptance_common/postgres_observer.py src/elspeth/web/_kubernetes_acceptance/__init__.py src/elspeth/web/_kubernetes_acceptance/controller.py tests/unit/web/acceptance_common/test_postgres_observer.py tests/unit/web/kubernetes_acceptance/__init__.py tests/unit/web/kubernetes_acceptance/test_controller.py deploy/kubernetes/overlays/kind-acceptance/kustomization.yaml deploy/kubernetes/overlays/kind-acceptance/shared/kustomization.yaml deploy/kubernetes/overlays/kind-acceptance/replica-a/kustomization.yaml deploy/kubernetes/overlays/kind-acceptance/replica-b/kustomization.yaml tests/testcontainer/deployment/test_kubernetes_replica_probes.py
git add -- src/elspeth/web/_acceptance_common/postgres_observer.py src/elspeth/web/_azure_container_apps_acceptance/controller.py src/elspeth/web/_kubernetes_acceptance/__init__.py src/elspeth/web/_kubernetes_acceptance/controller.py tests/unit/web/acceptance_common/test_postgres_observer.py tests/unit/web/acceptance_common/test_dependencies.py tests/unit/web/kubernetes_acceptance/__init__.py tests/unit/web/kubernetes_acceptance/test_controller.py deploy/kubernetes/overlays/kind-acceptance/kustomization.yaml deploy/kubernetes/overlays/kind-acceptance/shared/kustomization.yaml deploy/kubernetes/overlays/kind-acceptance/replica-a/kustomization.yaml deploy/kubernetes/overlays/kind-acceptance/replica-b/kustomization.yaml tests/testcontainer/deployment/test_kubernetes_replica_probes.py
git status --short
scripts/branch-safety-check.sh --intent commit
git commit -m "feat(deploy): drive the replica probes against two Kubernetes Deployments on kind

Move the provider-neutral PostgreSQL half of the probes (SqlSession, SqlReader,
RoleRevocationPartition, PostgresEvidenceObserver, probe SQL) into
_acceptance_common/postgres_observer.py; the Container Apps controller
re-exports the same objects, so its facade keeps the import path the Sessions
mutation-authority manifest resolves probe seams through (manifest unedited).
Add KubernetesReplicaController and the kind-acceptance overlay (elspeth-web-a|b
on elspeth_runtime_a|b, NodePort 30452|30453, roles from the K4 harness),
and assert P1-P4b as kind-lane pytest outcomes.
P4a runs only with a composer provider (ELSPETH_KIND_COMPOSER_ENV_FILE).
No receipt, facade or CLOUD_PROVIDERS change." -- src/elspeth/web/_acceptance_common/postgres_observer.py src/elspeth/web/_azure_container_apps_acceptance/controller.py src/elspeth/web/_kubernetes_acceptance/__init__.py src/elspeth/web/_kubernetes_acceptance/controller.py tests/unit/web/acceptance_common/test_postgres_observer.py tests/unit/web/acceptance_common/test_dependencies.py tests/unit/web/kubernetes_acceptance/__init__.py tests/unit/web/kubernetes_acceptance/test_controller.py deploy/kubernetes/overlays/kind-acceptance/kustomization.yaml deploy/kubernetes/overlays/kind-acceptance/shared/kustomization.yaml deploy/kubernetes/overlays/kind-acceptance/replica-a/kustomization.yaml deploy/kubernetes/overlays/kind-acceptance/replica-b/kustomization.yaml tests/testcontainer/deployment/test_kubernetes_replica_probes.py
git show --stat HEAD
```

Expected: `git status --short` shows the eleven created paths as `A ` and the two modified paths as `M `; the safety check prints no `[FAIL]` line and exits 0 (on a FAIL, stop before the commit and fix the reported check); `git show --stat HEAD` ends with `13 files changed, <n> insertions(+), <n> deletions(-)`. Any other count means the index carried a sibling lane's staged path: undo with `git reset --mixed HEAD~1`, restage only the 13 paths above, and commit again by pathspec (never `git checkout`, `git restore` or `git clean`).
