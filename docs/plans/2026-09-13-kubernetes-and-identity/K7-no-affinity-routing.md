### Task K7: Qualify routing without affinity

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: K5, K6. Runs before: K8. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

**Files:**
- Create: `tests/testcontainer/deployment/kubernetes/no-affinity-overlay/kustomization.yaml`
- Create: `tests/testcontainer/deployment/kubernetes/no-affinity-overlay/patch-composer-stall.yaml`
- Create: `tests/testcontainer/deployment/kubernetes/no-affinity-overlay/secret-composer-stall.yaml`
- Modify: `tests/testcontainer/deployment/test_kubernetes_kind.py` (K4 creates it; K7 appends one delimited section at the end of the file)
- Modify (conditional — flip outcome only): `deploy/kubernetes/base/service.yaml` (K1's file; the affinity comment and the `sessionAffinity: ClientIP` line — the selector keeps both labels), `tests/unit/deployment/test_kubernetes_bundle.py` (K1's `test_service_pins_affinity_and_named_port` and K6's `test_aks_ingress_pins_cookie_affinity_websocket_timeouts_and_upload_size`), `deploy/kubernetes/overlays/aks/ingress.yaml` (K6's file; the header comment and the eight cookie-affinity annotations — `affinity`, `affinity-mode` and the six `session-cookie-*` keys — only; K7 waits on K6)
- Modify (both outcomes): `docs/plans/2026-09-13-kubernetes-platform-facts.md` (K0's document; the slot K0 reserves, `### 5.2 Affinity residuals` (tagged `[K7]`): the pass entry on the flip outcome, the failing surfaces on the residual outcome)

**Interfaces:**
- Consumes (K4): from `tests/testcontainer/deployment/conftest.py` the session fixture `kind_cluster -> KindCluster`; from `tests/testcontainer/deployment/kind_harness.py` the frozen dataclass `KindCluster` with `kubectl(*args: str) -> str` (runs `kubectl --kubeconfig <kubeconfig> *args`, asserts exit 0 with `kubectl <args> failed (exit=<n>):\n<stderr>`, returns stdout), `node_port(service: str) -> int`, `pod_url(pod: str) -> str` (a per-pod `kubectl port-forward`, `http://127.0.0.1:<port>`), `install(overlay: Path, *, timeout: float = 600.0) -> None` (renders the overlay once and applies it in cold-install order: prerequisites and `elspeth-provision-storage`, waited to `Complete`; `elspeth-schema-init`, waited; then Services and Deployments; its first command is `kubectl kustomize <overlay> -o <tmp>`), and the module constants `HERE` (`tests/testcontainer/deployment/kubernetes/`) and `REPO_ROOT`; from K4's `tests/testcontainer/deployment/test_kubernetes_kind.py` its import block (replaced by K7 Step 1a) and the module names `OVERLAY` (`deploy/kubernetes/overlays/kind-test`), `WEB_PODS`, `TERMINAL`, `_wait_ready(url: str, *, timeout: float = 300.0) -> None` and `_running_web_pods(cluster: KindCluster) -> list[str]` (sorted names of Running web pods, no count assertion); the marker `kind`; the K4 overlay directory `deploy/kubernetes/overlays/kind-test/` (one Deployment `elspeth-web`, `replicas: 2`, container `web`, Service `elspeth-web` on NodePort 30451, ConfigMap `elspeth-web-config`).
- Consumes (K1, `tests/unit/deployment/test_kubernetes_bundle.py`): `_render() -> tuple[dict[str, Any], ...]`, `_one(kind: str) -> dict[str, Any]` (reads the base render only), `WEB_POD_SELECTOR`, `_require_kubectl(reason: str) -> None`, the test `test_service_pins_affinity_and_named_port`; (K6, same module): `_render_aks() -> list[dict]`, `_one_of(docs: Iterable[dict], kind: str) -> dict`, `MAX_UPLOAD_BYTES`, the test `test_aks_ingress_pins_cookie_affinity_websocket_timeouts_and_upload_size`, and `deploy/kubernetes/overlays/aks/ingress.yaml`; (K0): §1.3 `PROVISION_STORAGE_IMAGE=busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0`, which K7's sidecar writes fully qualified as `docker.io/library/busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0` exactly as K1's `job-provision-storage.yaml` does, and the facts document.
- Consumes (HEAD, measured 2026-09-13): the three durable authorities `app.py:1720-1725` installs whenever the sessions engine dialect is `postgresql` — `RepositorySessionWebsocketTicketAuthority` (`web/coordination/websocket_ticket_authority.py:21`, `ttl_seconds=30` at `:44`), `DatabaseComposerProgressRegistry` over `SessionComposerProgressAuthority` (`web/coordination/composer_progress_authority.py:469` / `:109`; `get_latest` `:395` and `list_active` `:415` scope by session ownership, never by `owner_instance_id`), `RepositoryRunProgressReader` (`web/execution/run_progress_reader.py:14`). Routes: `POST /api/auth/register` (`web/auth/routes.py:351`, 200, `TokenResponse.access_token` `:131`), `POST /api/sessions` (201, `id`), `POST /api/sessions/{session_id}/blobs/inline` (`web/blobs/routes.py:260`, 201, `CreateInlineBlobRequest.filename/content/mime_type` `blobs/schemas.py:106-108`), `POST /api/sessions/{session_id}/state/yaml` (`sessions/routes/composer/state.py:805`, `ImportStateYamlRequest.yaml` + `source_blob_ids` `:210-214`; `_state_with_imported_source_blobs` `:437-477` rewrites the source `path` to `blob.storage_path` BEFORE `_reject_disallowed_source_paths` `:843-847`, which is why the CSV enters as a blob and not as a mounted file — `allowed_source_directories` is `blobs/<session_id>` only, `web/paths.py:70-79`), `POST /api/sessions/{session_id}/execute` (`web/execution/routes.py:1006`, 202, `run_id`), `GET /api/runs/{run_id}` (`:1273`, `status` ∈ `SessionRunStatus`, `sessions/protocol.py:220`), `POST /api/runs/{run_id}/ws-ticket` (`:2012`, `ticket`), `WS /ws/runs/{run_id}?ticket=` (`:1574`; durable replay `_poll_durable_run_progress` `:2058-2105` sends `RunEvent` JSON with `event_sequence`/`event_type` and closes 1000 after the terminal event; a spent ticket closes 4001 at `:1602` BEFORE `accept`, which uvicorn turns into an HTTP 403 handshake rejection — `.venv/lib/python3.13/site-packages/uvicorn/protocols/websockets/websockets_impl.py:278-284`), `POST /api/sessions/{session_id}/messages` (`sessions/routes/messages.py:105`; publishes `starting` durably at `:241-249` before the provider call), `GET /api/sessions/_active` (`sessions/routes/sessions.py:794`, non-terminal phases only, `contracts/composer_progress.py:70-78`), `GET /api/sessions/{session_id}/composer-progress` (`sessions/routes/composer/state.py:496`, `ComposerProgressSnapshot` with `request_id`, `phase`, `updated_at`, `inflight_requests` — `web/composer/progress.py:57-70`), `GET /api/health` and `GET /api/system/status` (`web/app.py:2107`, `instance_id`), response header `X-Elspeth-Instance` (`web/middleware/instance_identity.py:32`). Config: `composer_endpoint_base_url` accepts `http://` ONLY for a numeric loopback host (`web/config.py:140-170`; measured: `http://127.0.0.1:8080/v1` accepted, `http://composer-stall.default.svc.cluster.local:8080/v1` refused), and must be paired with `composer_endpoint_api_key` (`:1197`); same pair for the advisor role (`:1204`); `composer_timeout_seconds` must stay under `composer_transport_idle_ceiling_seconds - composer_transport_headroom_seconds` = 300 − 30 (`:68-69`, `:1119-1126`); a wall clock under 15 s per turn only WARNS (`:1145-1152`, `_COMPOSER_PLANNING_SECONDS_PER_TURN = 15.0` at `:75`). A turn whose provider never answers ends as `phase="failed"`, `reason="convergence_wall_clock_timeout"` (`web/composer/progress.py:339-375`) with HTTP 422, or — if LiteLLM's own timeout fires first — as `phase="failed"` with HTTP 502 (`messages.py:669-693`).
- Produces: the measured affinity verdict K8 writes into the runbook and `docs/reference/deployment-platforms.md`. On the FLIP outcome: `deploy/kubernetes/base/service.yaml` carries `sessionAffinity: "None"`, `test_service_pins_affinity_and_named_port` becomes `test_service_ships_without_affinity_and_keeps_the_named_port` (asserts `"None"` and keeps the `ClusterIP`, `WEB_POD_SELECTOR` and named-port pins), the AKS ingress ships with all eight cookie-affinity annotations (`affinity`, `affinity-mode`, `session-cookie-name`, `session-cookie-max-age`, `session-cookie-expires`, `session-cookie-change-on-failure`, `session-cookie-secure`, `session-cookie-samesite`) commented out under `# Optional, for owner-affine debugging only`, and `test_aks_ingress_pins_cookie_affinity_websocket_timeouts_and_upload_size` becomes `test_aks_ingress_ships_without_cookie_affinity_and_keeps_websocket_timeouts_and_upload_size`, and K0's slot `### 5.2 Affinity residuals` (tagged `[K7]`) in `docs/plans/2026-09-13-kubernetes-platform-facts.md` reads `none — affinity dropped in test(deploy): qualify Kubernetes routing without session affinity and drop the ClientIP default`. On the RESIDUAL outcome: the same slot names each failing surface, and no manifest change. Test ids later tasks may select: `test_client_ip_affinity_pins_one_replica_per_client` (control; deleted on the flip outcome because the base no longer pins ClientIP), `test_service_scatters_across_both_replicas_without_affinity`, `test_run_started_on_one_replica_replays_and_burns_its_ticket_on_the_other`, `test_composer_turn_in_flight_on_one_replica_is_visible_from_the_other`. The `kubernetes-kind` CI job (K4, `-m kind -n 0` over `tests/testcontainer/deployment`) selects all four without a workflow edit.

What this task decides. The base pins `sessionAffinity: ClientIP` (K1) and the AKS ingress pins cookie affinity (K6) because the Phase 6b bar says "sticky ingress until no-affinity is qualified". Qualification means proving, on a live two-replica cluster, that every surface the SPA reaches after a reconnect — the run WebSocket ticket, the run progress replay, the composer in-flight poll and the latest-progress snapshot — is served from the shared database and not from the process that started the work. HEAD predicts a pass: all three stores are database-backed when the sessions engine is PostgreSQL (`app.py:1720-1725`), and `get_latest`/`list_active` never filter on the owning instance. The test exists because a prediction is not a measurement.

Why the composer half needs a stalled provider. `POST /messages` publishes `starting` (`messages.py:241-249`) and then calls the provider; in K4's provider-free harness the call is refused at `compose()` (`composer/service.py:3692+37`, `ComposerServiceError`) within milliseconds, so the in-flight window that `/_active` reports is too short to poll honestly and any "did B see it" assertion would race. The overlay therefore adds a **sidecar** in the web pod that accepts TCP on `127.0.0.1:8080` and never answers (measured 2026-09-13 with `docker run busybox sh -c 'while true; do sleep 3600 | nc -l -p 8080; done'`: `curl -m 5 -X POST http://127.0.0.1:18080/v1/chat/completions` exits 28 — timed out with 0 bytes — never 7/56), and points BOTH composer roles at it through the loopback-only `http://` allowance. The provider is still called; nothing bypasses it (Composer invariant 1 is about authoring structure server-side, which this does not do). The turn ends deterministically at `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=15`.

- [ ] **Step 1: Write the failing tests.**

Edit K4's module `tests/testcontainer/deployment/test_kubernetes_kind.py` in two places. The module keeps ONE import block at the top (ruff `E402`/`I001`; `select` at `pyproject.toml:293-305`, run by the pre-commit `ruff` hook at `.pre-commit-config.yaml:45-51`), and K7 defines no name that K4's module or `kind_harness.py` already defines (`F811`; re-binding K4's `WEB_PODS` tuple as a string would also break K4's `_running_web_pods`, which unpacks it).

(a) Replace K4's import block — from `from __future__ import annotations` through `from tests.testcontainer.deployment.kind_harness import REPO_ROOT, KindCluster` — with this merged block. `tests` is not in `known-first-party` (`pyproject.toml:315-316`), so ruff's isort sorts `from tests...` into the third-party section, ahead of `websockets`, with no blank line before it:

```python
from __future__ import annotations

import concurrent.futures
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import sqlalchemy as sa
import yaml
from tests.testcontainer.deployment.kind_harness import HERE, REPO_ROOT, KindCluster
from websockets.exceptions import ConnectionClosedOK, InvalidStatus
from websockets.sync.client import connect as ws_connect

from elspeth.contracts.composer_progress import NON_TERMINAL_PROGRESS_PHASES
```

(b) Append this section to the END of the module, after two blank lines. It imports nothing, calls kubectl only through `KindCluster.kubectl` and `KindCluster.install`, and reuses K4's `OVERLAY`, `TERMINAL`, `_wait_ready` and `_running_web_pods` and the harness's `HERE`. Measured 2026-09-14: K4's Step 1 module with (a) and (b) spliced in, piped through `ruff check` (ruff 0.15.4, the pre-commit pin) with `--stdin-filename tests/testcontainer/deployment/test_kubernetes_kind.py`, exits 0, and the same splice with `HERE` dropped from (a) exits 1 with `F821 Undefined name HERE`. `ruff format --diff` on the splice reports one hunk only, and it is in K4's re-apply test (the long `assert unchanged, f"..."` line), which K4's module alone reports identically: (a) and (b) add no formatting change.

```python
# ── K7: routing without session affinity ──────────────────────────────────
# Every helper below drives the public HTTP surface the SPA drives. The run
# half seeds a csv→csv pipeline through the inline-blob and YAML-import routes
# because a kind pod cannot see the test's filesystem and the execute path
# admits source files only under data_dir/blobs/<session_id>
# (web/paths.py:70-79; state.py:437-477 binds the blob before the allowlist).
# Reused from K4 above, never redefined here: OVERLAY, TERMINAL, _wait_ready,
# _running_web_pods; from kind_harness: HERE, KindCluster.

NO_AFFINITY_OVERLAY = HERE / "no-affinity-overlay"
SCATTER_REQUESTS = 40

_CSV_PASSTHROUGH_YAML = """\
sources:
  primary:
    plugin: csv
    on_success: out
    on_validation_failure: discard
    options:
      path: in.csv
      schema:
        mode: fixed
        fields: ["id: int", "name: str", "value: int"]
sinks:
  out:
    plugin: csv
    on_write_failure: discard
    options:
      path: outputs/k7-result.csv
      schema:
        mode: fixed
        fields: ["id: int", "name: str", "value: int"]
"""


def _apply_and_roll(kind_cluster: KindCluster, overlay: Path, *, settle_timeout: float = 300.0) -> None:
    """Install an overlay in cold-install order, force a fresh rollout, and wait until the Service answers.

    ``KindCluster.install`` (K4) renders the overlay once and applies the
    prerequisites and the provisioner, then schema-init, then the Service and
    Deployment, waiting each Job to ``Complete``. K7's selection runs on a
    fresh cluster when it is run alone (Steps 2 and 4), so the first call here
    is a cold install, where one ``apply -k`` would start schema-init beside
    the provisioner; on a warm cluster the same call leaves unchanged objects
    unchanged and recreates and waits any Job the 600 s TTL reaped.

    ``rollout restart`` is unconditional: whether a ConfigMap change alone
    restarts the pods depends on the generator's name-suffix hash, which this
    test does not own. ``rollout status`` returns once the new ReplicaSet is
    available, but a replaced pod keeps phase Running while it terminates and
    K4's ``_running_web_pods`` counts Running pods, so settle on exactly two
    before any test binds pod names.
    """
    kind_cluster.install(overlay)
    kind_cluster.kubectl("rollout", "restart", "deployment/elspeth-web")
    kind_cluster.kubectl("rollout", "status", "deployment/elspeth-web", "--timeout=600s")
    deadline = time.monotonic() + settle_timeout
    pods = _running_web_pods(kind_cluster)
    while len(pods) != 2 and time.monotonic() < deadline:
        time.sleep(2)
        pods = _running_web_pods(kind_cluster)
    assert len(pods) == 2, f"expected two running web pods after the rollout, found {pods}"
    _wait_ready(f"http://127.0.0.1:{kind_cluster.node_port('elspeth-web')}/api/ready")


def _service_url(kind_cluster: KindCluster) -> str:
    return f"http://127.0.0.1:{kind_cluster.node_port('elspeth-web')}"


def _scatter(base: str) -> set[str]:
    """One fresh TCP connection per request: a shared Client keeps the socket
    alive and would pin one pod regardless of the Service's affinity."""
    instances: set[str] = set()
    for _ in range(SCATTER_REQUESTS):
        response = httpx.get(f"{base}/api/health", headers={"Connection": "close"}, timeout=10.0)
        assert response.status_code == 200, response.text
        instances.add(response.headers["X-Elspeth-Instance"])
    return instances


def _register(base: str) -> dict[str, str]:
    response = httpx.post(
        f"{base}/api/auth/register",
        json={"username": f"k7-{uuid.uuid4().hex[:8]}", "password": "k7-passw0rd-not-a-secret", "display_name": "K7"},
        timeout=30.0,
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_session(base: str, headers: dict[str, str], *, title: str) -> str:
    # A non-default title keeps messages.py's first-message auto-titling
    # (maybe_auto_title_session) from opening a second provider connection.
    response = httpx.post(f"{base}/api/sessions", headers=headers, json={"title": title}, timeout=30.0)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _seed_csv_passthrough(base: str, headers: dict[str, str], session_id: str) -> None:
    blob = httpx.post(
        f"{base}/api/sessions/{session_id}/blobs/inline",
        headers=headers,
        json={"filename": "in.csv", "content": "id,name,value\n1,a,10\n2,b,20\n3,c,30\n", "mime_type": "text/csv"},
        timeout=30.0,
    )
    assert blob.status_code == 201, blob.text
    imported = httpx.post(
        f"{base}/api/sessions/{session_id}/state/yaml",
        headers=headers,
        json={"yaml": _CSV_PASSTHROUGH_YAML, "source_blob_ids": {"primary": blob.json()["id"]}},
        timeout=60.0,
    )
    assert imported.status_code == 200, imported.text


def _execute(base: str, headers: dict[str, str], session_id: str) -> str:
    response = httpx.post(f"{base}/api/sessions/{session_id}/execute", headers=headers, timeout=30.0)
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def _wait_terminal(base: str, headers: dict[str, str], run_id: str, *, timeout: float = 120.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = httpx.get(f"{base}/api/runs/{run_id}", headers=headers, timeout=30.0)
        assert response.status_code == 200, response.text
        status = response.json()["status"]
        if status in TERMINAL:
            return status
        time.sleep(1.0)
    raise AssertionError(f"run {run_id} never reached a terminal status")


def _mint_ticket(base: str, headers: dict[str, str], run_id: str) -> str:
    response = httpx.post(f"{base}/api/runs/{run_id}/ws-ticket", headers=headers, timeout=30.0)
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def _ws(base: str, run_id: str, ticket: str) -> str:
    parts = urlsplit(base)
    return urlunsplit(("ws", parts.netloc, f"/ws/runs/{run_id}", f"ticket={ticket}", ""))


def _replay(base: str, run_id: str, ticket: str) -> tuple[list[dict], int]:
    """Consume one ticket and drain the durable replay; return (events, close code)."""
    events: list[dict] = []
    with ws_connect(_ws(base, run_id, ticket), open_timeout=20) as socket:
        try:
            while True:
                events.append(json.loads(socket.recv(timeout=60)))
        except ConnectionClosedOK as closed:
            assert closed.rcvd is not None
            return events, closed.rcvd.code


@pytest.fixture(scope="module")
def no_affinity_rollout(kind_cluster: KindCluster):
    """Roll the no-affinity overlay out for the K7 tests, then put K4's overlay back
    so the acceptance file (K5) inherits K4's environment, not this one's."""
    _apply_and_roll(kind_cluster, NO_AFFINITY_OVERLAY)
    try:
        yield kind_cluster
    finally:
        _apply_and_roll(kind_cluster, OVERLAY)


def test_client_ip_affinity_pins_one_replica_per_client(kind_cluster: KindCluster) -> None:
    """CONTROL: under K4's overlay (ClientIP) one source address always lands on one pod.

    Runs before the fixture below flips the Service so the scatter instrument
    is proven against a known-negative before it is trusted on the positive.
    """
    _apply_and_roll(kind_cluster, OVERLAY)
    assert len(_scatter(_service_url(kind_cluster))) == 1


def test_service_scatters_across_both_replicas_without_affinity(no_affinity_rollout: KindCluster) -> None:
    kind_cluster = no_affinity_rollout
    pods = _running_web_pods(kind_cluster)
    reported = {httpx.get(f"{kind_cluster.pod_url(pod)}/api/system/status", timeout=10.0).json()["instance_id"] for pod in pods}
    seen = _scatter(_service_url(kind_cluster))
    assert seen == reported and len(seen) == 2, f"Service routed to {seen}; pods report {reported}"


def test_run_started_on_one_replica_replays_and_burns_its_ticket_on_the_other(no_affinity_rollout: KindCluster) -> None:
    kind_cluster = no_affinity_rollout
    pod_a, pod_b = _running_web_pods(kind_cluster)
    a, b = kind_cluster.pod_url(pod_a), kind_cluster.pod_url(pod_b)
    headers = _register(a)
    session_id = _create_session(a, headers, title="K7 run replay")
    _seed_csv_passthrough(a, headers, session_id)
    run_id = _execute(a, headers, session_id)

    # Replay through B from sequence 0: every event A committed arrives, in order,
    # then the server closes 1000. The ticket lives 30 s, so mint-then-connect is
    # one call apart.
    events, close_code = _replay(b, run_id, _mint_ticket(a, headers, run_id))
    assert close_code == 1000
    assert [event["event_sequence"] for event in events] == list(range(1, len(events) + 1))
    assert events[-1]["event_type"] == "completed", events[-1]
    assert _wait_terminal(b, headers, run_id) == "completed"

    # Single use across replicas: consume on B, then present the same ticket to A.
    # A's consume returns None and the route closes 4001 BEFORE accept, which
    # uvicorn reports to a real client as an HTTP 403 handshake rejection.
    ticket = _mint_ticket(a, headers, run_id)
    _replay(b, run_id, ticket)
    with pytest.raises(InvalidStatus, match=r"HTTP 403") as refused, ws_connect(_ws(a, run_id, ticket), open_timeout=20):
        pass
    assert refused.value.response.status_code == 403


def test_composer_turn_in_flight_on_one_replica_is_visible_from_the_other(no_affinity_rollout: KindCluster) -> None:
    kind_cluster = no_affinity_rollout
    pod_a, pod_b = _running_web_pods(kind_cluster)
    a, b = kind_cluster.pod_url(pod_a), kind_cluster.pod_url(pod_b)
    headers = _register(a)
    session_id = _create_session(a, headers, title="K7 composer in flight")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(
            httpx.post,
            f"{a}/api/sessions/{session_id}/messages",
            headers=headers,
            json={"content": "Read the CSV and write it back out unchanged."},
            timeout=90.0,
        )
        # In-flight poll through B: the stalled sidecar holds A's
        # provider call for COMPOSER_TIMEOUT_SECONDS=15, so the window is real.
        in_flight: dict | None = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and in_flight is None:
            active = httpx.get(f"{b}/api/sessions/_active", headers=headers, timeout=10.0)
            assert active.status_code == 200, active.text
            in_flight = next((snapshot for snapshot in active.json() if snapshot["session_id"] == session_id), None)
            if in_flight is None:
                time.sleep(0.5)
        assert in_flight is not None, "replica B never listed A's in-flight composer turn under /_active"
        assert in_flight["phase"] in NON_TERMINAL_PROGRESS_PHASES, in_flight
        assert in_flight["request_id"] is not None
        response = turn.result(timeout=90.0)

    # The stalled provider ends the turn as a wall-clock timeout (422) or, if
    # LiteLLM's own timeout fires first, as a provider error (502). Either way
    # the terminal snapshot is durable and B serves the same one A wrote.
    assert response.status_code in {422, 502}, response.text
    latest_b = httpx.get(f"{b}/api/sessions/{session_id}/composer-progress", headers=headers, timeout=10.0).json()
    latest_a = httpx.get(f"{a}/api/sessions/{session_id}/composer-progress", headers=headers, timeout=10.0).json()
    assert latest_b["request_id"] == in_flight["request_id"]
    assert latest_b["phase"] == "failed", latest_b
    assert latest_b["inflight_requests"] == 0
    assert latest_a["updated_at"] == latest_b["updated_at"] and latest_a["phase"] == latest_b["phase"]
    still_active = httpx.get(f"{b}/api/sessions/_active", headers=headers, timeout=10.0).json()
    assert all(snapshot["session_id"] != session_id for snapshot in still_active)
```

- [ ] **Step 2: Run the K7 selection and watch the three overlay tests error.**

Run (the tools directory is the one K4's `scripts/cicd/kubernetes-kind-smoke.sh` installs the checksum-pinned `kubectl`/`kind` into; `-m kind` is load-bearing because `addopts` deselects the marker):

```bash
cd "$(git rev-parse --show-toplevel)" && export PATH="${ELSPETH_K8S_TOOLS:-$PWD/.claude/lanes/k8s/bin}:$PATH" && pytest tests/testcontainer/deployment/test_kubernetes_kind.py -m kind -n 0 -k "affinity or replays or in_flight" > /tmp/k8s-K7-step2.log 2>&1; echo exit=$?
```

Expected: `exit=1`. `test_client_ip_affinity_pins_one_replica_per_client` PASSES (the control runs on K4's overlay, which exists). The other three ERROR at fixture setup with the `AssertionError` that `KindCluster.kubectl` raises on a non-zero exit — `kubectl kustomize <repo>/tests/testcontainer/deployment/kubernetes/no-affinity-overlay -o <tmp>/kind-install-<suffix> failed (exit=1):` (the first command of K4's `KindCluster.install`) followed by kubectl's stderr, which carries `error: must build at directory: not a valid directory` for the missing overlay. A `NameError` or an import-time error that also takes down K4's five tests means Step 1's import block or a K4 name was not reused as written. A control that FAILS here (scatter set of size 2 under ClientIP) means the scatter instrument or K4's Service is wrong; stop and fix that before writing the overlay.

- [ ] **Step 3: Write the no-affinity overlay.**

```yaml
# tests/testcontainer/deployment/kubernetes/no-affinity-overlay/kustomization.yaml
# K7: K4's shared-state overlay with session affinity OFF and a stalled
# composer endpoint, so a composer turn in flight on one replica can be
# observed from the other. Test-only: never a deployment target, never
# referenced from deploy/.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../../../../deploy/kubernetes/overlays/kind-test
  - secret-composer-stall.yaml
patches:
  - target: { kind: Service, name: elspeth-web }
    patch: |-
      - op: replace
        path: /spec/sessionAffinity
        value: "None"
  - target: { kind: Deployment, name: elspeth-web }
    path: patch-composer-stall.yaml
configMapGenerator:
  - name: elspeth-web-config
    behavior: merge
    literals:
      # The test registers its own users; K4's overlay may run closed.
      - ELSPETH_WEB__REGISTRATION_MODE=open
      # Both roles route to the loopback sidecar below. The base-URL validator
      # (web/config.py:140-170) admits http:// for a numeric loopback host only,
      # which is why the stall is a sidecar and not a Service.
      - ELSPETH_WEB__COMPOSER_MODEL=openai/k7-stall
      - ELSPETH_WEB__COMPOSER_ADVISOR_MODEL=openai/k7-stall-advisor
      - ELSPETH_WEB__COMPOSER_ENDPOINT_BASE_URL=http://127.0.0.1:8080/v1
      - ELSPETH_WEB__COMPOSER_ADVISOR_ENDPOINT_BASE_URL=http://127.0.0.1:8080/v1
      # 15 s wall clock: under the 300 - 30 s transport ceiling (config.py:1119);
      # a sub-15 s-per-turn budget only warns (config.py:1145).
      - ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=15
      # The boot probe would otherwise sit in the stall for 5 s per pod boot.
      - ELSPETH_WEB__COMPOSER_BOOT_PROBE_ENABLED=false
```

```yaml
# tests/testcontainer/deployment/kubernetes/no-affinity-overlay/patch-composer-stall.yaml
# JSON patch on Deployment/elspeth-web: (1) hand the web container the stall
# credentials; (2) add a sidecar that accepts TCP on 127.0.0.1:8080 and never
# answers. busybox nc with an open, silent stdin (`sleep 3600 |`) holds every
# accepted connection until the CLIENT gives up — measured 2026-09-13:
# `curl -m 5 -X POST http://127.0.0.1:18080/v1/chat/completions` exits 28
# (timed out, 0 bytes), never 7 (refused) or 56 (reset). The image is K0 §1.3's
# PROVISION_STORAGE_IMAGE (index digest of busybox:1.37.0), written fully
# qualified exactly as K1's job-provision-storage.yaml writes it.
- op: add
  path: /spec/template/spec/containers/0/envFrom/-
  value:
    secretRef:
      name: elspeth-web-composer-stall
- op: add
  path: /spec/template/spec/containers/-
  value:
    name: composer-stall
    image: docker.io/library/busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0
    command: ["sh", "-c", "while true; do sleep 3600 | nc -l -p 8080; done"]
    ports:
      - name: stall
        containerPort: 8080
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: ["ALL"]
```

```yaml
# tests/testcontainer/deployment/kubernetes/no-affinity-overlay/secret-composer-stall.yaml
# Test-only literals. Nothing reads them: config.py:1197/:1204 only require
# that a configured endpoint URL is PAIRED with a key, and the sidecar never
# looks at the request. They exist so no ambient provider key is needed
# (availability.py:69-70 waives the env-key requirement once the endpoint is
# configured), which is also why no OPENAI_/ANTHROPIC_-shaped value lands here.
apiVersion: v1
kind: Secret
metadata:
  name: elspeth-web-composer-stall
type: Opaque
stringData:
  ELSPETH_WEB__COMPOSER_ENDPOINT_API_KEY: k7-stall-not-a-credential  # secret-scan: allow-this-line
  ELSPETH_WEB__COMPOSER_ADVISOR_ENDPOINT_API_KEY: k7-stall-not-a-credential  # secret-scan: allow-this-line
```

Prove the render before touching the cluster (structural, no API server):

```bash
cd "$(git rev-parse --show-toplevel)" && export PATH="${ELSPETH_K8S_TOOLS:-$PWD/.claude/lanes/k8s/bin}:$PATH" && kubectl kustomize tests/testcontainer/deployment/kubernetes/no-affinity-overlay > /tmp/k8s-K7-render.yaml; echo exit=$?; grep -c 'sessionAffinity: "None"' /tmp/k8s-K7-render.yaml; grep -c 'name: composer-stall' /tmp/k8s-K7-render.yaml; grep -c 'ELSPETH_WEB__COMPOSER_ENDPOINT_BASE_URL: http://127.0.0.1:8080/v1' /tmp/k8s-K7-render.yaml
```

Expected: `exit=0` then `1`, `1`, `1`. A `0` on the first grep means the Service patch did not apply (check the Service name K4's overlay renders); a `0` on the third means the ConfigMap merge did not land (the generated ConfigMap must be named `elspeth-web-config` before hashing, exactly as K4's merge names it).

- [ ] **Step 4: Run the K7 selection on the live cluster and read the verdict.**

```bash
cd "$(git rev-parse --show-toplevel)" && export PATH="${ELSPETH_K8S_TOOLS:-$PWD/.claude/lanes/k8s/bin}:$PATH" && pytest tests/testcontainer/deployment/test_kubernetes_kind.py -m kind -n 0 -k "affinity or replays or in_flight" > /tmp/k8s-K7-step4.log 2>&1; echo exit=$?
```

Then run the whole kind lane once, exactly as CI will, so the restore in the fixture teardown is proven to hand K4's environment on:

```bash
cd "$(git rev-parse --show-toplevel)" && scripts/cicd/kubernetes-kind-smoke.sh > /tmp/k8s-K7-smoke.log 2>&1; echo exit=$?; kind get clusters
```

Expected on the flip path: both `exit=0`, `kind get clusters` prints nothing.

Reading a red honestly — this is the discrimination rule for the two outcomes below:

- A **residual** is a failed `assert` whose message names a durable surface: the scatter set (`Service routed to`), the replay (`event_sequence`, `event_type`, close code), the ticket (`InvalidStatus`/403 not raised, i.e. the ticket was consumable twice), the in-flight poll (`replica B never listed A's in-flight composer turn under /_active`) or the terminal snapshot (`request_id`, `updated_at`, `inflight_requests`). Copy the assertion text and the surface into the facts document (the residual branch below) and keep `ClientIP`. Do not weaken the assertion.
- A **harness defect** is anything else: an `AssertionError` reading `kubectl ... failed (exit=` from `KindCluster.kubectl`, `rollout status` timing out, `expected two running web pods after the rollout`, `/api/ready` never 200, a 4xx from `register`/`state/yaml`/`execute` (read the body — a 400 from the YAML import names the field), the control test failing, or the run ending `failed`. Fix the harness and rerun; it is never written up as a residual.
- The kind lane is serial and owns one cluster; do not add `-n`. If the lane goes red only in the smoke run and not in the selection, diff the two logs before blaming this task (AGENTS.md § Gotchas, flaky-under-parallelism note applies to the recovery suites, not to this lane).

- [ ] **Step 5a (conditional — every K7 assertion passed): flip the default off.**

Edit `deploy/kubernetes/base/service.yaml` — the whole file after the edit:

```yaml
# deploy/kubernetes/base/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: elspeth-web
spec:
  type: ClusterIP
  # Qualified without affinity on kind (Task K7, 2026-09): tickets, run
  # progress and composer progress are served from the shared PostgreSQL
  # stores, so any replica may answer any request. ClientIP is not needed
  # for correctness; set it only for owner-affine debugging.
  sessionAffinity: "None"
  selector:
    app.kubernetes.io/name: elspeth-web
    app.kubernetes.io/component: web
  ports:
    - name: http
      port: 8451
      targetPort: http
```

Replace K1's `test_service_pins_affinity_and_named_port` in `tests/unit/deployment/test_kubernetes_bundle.py` with:

```python
def test_service_ships_without_affinity_and_keeps_the_named_port() -> None:
    svc = _one("Service")["spec"]
    assert svc["type"] == "ClusterIP"
    # Qualified by tests/testcontainer/deployment/test_kubernetes_kind.py (K7):
    # every reconnect surface is database-backed, so no replica is special.
    assert svc["sessionAffinity"] == "None"
    assert svc["selector"] == WEB_POD_SELECTOR
    assert svc["ports"] == [{"name": "http", "port": 8451, "targetPort": "http"}]
```

Replace K6's `test_aks_ingress_pins_cookie_affinity_websocket_timeouts_and_upload_size` in the same file with the test below. `_one` is K1's base-only helper (`_one(kind)`); the overlay render goes through K6's `_one_of(docs, kind)`. Every non-affinity assertion K6's test made is kept verbatim.

```python
def test_aks_ingress_ships_without_cookie_affinity_and_keeps_websocket_timeouts_and_upload_size() -> None:
    ing = _one_of(_render_aks(), "Ingress")
    ann = ing["metadata"]["annotations"]
    # Affinity is optional after K7: ingress.yaml carries all eight cookie-affinity
    # annotations (affinity, affinity-mode, session-cookie-*) commented out under
    # "Optional, for owner-affine debugging only", so none may render. The prefix
    # match goes red if any single one of them is uncommented.
    rendered_affinity = sorted(
        key for key in ann if key.startswith(("nginx.ingress.kubernetes.io/affinity", "nginx.ingress.kubernetes.io/session-cookie-"))
    )
    assert rendered_affinity == [], rendered_affinity
    # The WebSocket idle timeouts and the upload size are unrelated to affinity and stay pinned.
    assert int(ann["nginx.ingress.kubernetes.io/proxy-read-timeout"]) >= 3600
    assert int(ann["nginx.ingress.kubernetes.io/proxy-send-timeout"]) >= 3600
    # ingress-nginx defaults proxy-body-size to 1m; the app accepts max_upload_bytes.
    assert ann["nginx.ingress.kubernetes.io/proxy-body-size"] == f"{MAX_UPLOAD_BYTES // (1024 * 1024)}m"
    assert ing["spec"]["ingressClassName"] == "webapprouting.kubernetes.azure.com"
    (rule,) = ing["spec"]["rules"]
    (path,) = rule["http"]["paths"]
    assert path["backend"]["service"] == {"name": "elspeth-web", "port": {"name": "http"}}
    (tls,) = ing["spec"]["tls"]
    assert tls["hosts"] == [rule["host"]] and tls["secretName"] == "elspeth-web-tls"
```

In `deploy/kubernetes/overlays/aks/ingress.yaml` (K6's file; K7 runs after K6), comment out ONLY the eight cookie-affinity annotations and rewrite the header comment that says affinity stays on until K7. The file from its first line through the annotation block, before the edit, as K6 writes it:

```yaml
# deploy/kubernetes/overlays/aks/ingress.yaml
# ingress-nginx as managed by the AKS application-routing add-on (ingress class
# webapprouting.kubernetes.azure.com). Session affinity stays ON until Task K7
# qualifies routing without it: the cookie, not the base Service's
# sessionAffinity: ClientIP, is what pins a browser to one replica here, because
# ingress-nginx proxies to pod endpoints and never passes through the ClusterIP.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: elspeth-web
  annotations:
    nginx.ingress.kubernetes.io/affinity: cookie
    # `balanced` (the default) re-shards cookies when the Deployment scales;
    # `persistent` keeps an issued cookie on its replica until that pod goes.
    nginx.ingress.kubernetes.io/affinity-mode: persistent
    nginx.ingress.kubernetes.io/session-cookie-name: elspeth-replica
    nginx.ingress.kubernetes.io/session-cookie-max-age: "86400"
    nginx.ingress.kubernetes.io/session-cookie-expires: "86400"
    nginx.ingress.kubernetes.io/session-cookie-change-on-failure: "true"
    nginx.ingress.kubernetes.io/session-cookie-secure: "true"
    nginx.ingress.kubernetes.io/session-cookie-samesite: Lax
    # WebSocket idle time on the nginx hop (run progress at /ws/runs/{run_id}).
    # The Azure LB in front of the controller cuts idle TCP at 4 minutes by
    # default; that hop, not these, sets the composer ceiling (kustomization.yaml).
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    # ingress-nginx defaults to 1m; WebSettings.max_upload_bytes is 100 MiB.
    nginx.ingress.kubernetes.io/proxy-body-size: 100m
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
```

and after the edit (everything from `spec:` to the end of the file is unchanged, and the four non-affinity annotations keep their values and comments):

```yaml
# deploy/kubernetes/overlays/aks/ingress.yaml
# ingress-nginx as managed by the AKS application-routing add-on (ingress class
# webapprouting.kubernetes.azure.com). Routing without session affinity was
# qualified on kind (Task K7): WebSocket tickets, run progress and composer
# progress are served from PostgreSQL, so no replica is special and the
# cookie-affinity annotations below ship commented out.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: elspeth-web
  annotations:
    # Optional, for owner-affine debugging only: uncomment this whole group to
    # pin a browser to one replica while diagnosing that replica. The base
    # Service's sessionAffinity cannot do it here, because ingress-nginx proxies
    # to pod endpoints and never passes through the ClusterIP.
    # nginx.ingress.kubernetes.io/affinity: cookie
    # `balanced` (the default) re-shards cookies when the Deployment scales;
    # `persistent` keeps an issued cookie on its replica until that pod goes.
    # nginx.ingress.kubernetes.io/affinity-mode: persistent
    # nginx.ingress.kubernetes.io/session-cookie-name: elspeth-replica
    # nginx.ingress.kubernetes.io/session-cookie-max-age: "86400"
    # nginx.ingress.kubernetes.io/session-cookie-expires: "86400"
    # nginx.ingress.kubernetes.io/session-cookie-change-on-failure: "true"
    # nginx.ingress.kubernetes.io/session-cookie-secure: "true"
    # nginx.ingress.kubernetes.io/session-cookie-samesite: Lax
    # WebSocket idle time on the nginx hop (run progress at /ws/runs/{run_id}).
    # The Azure LB in front of the controller cuts idle TCP at 4 minutes by
    # default; that hop, not these, sets the composer ceiling (kustomization.yaml).
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    # ingress-nginx defaults to 1m; WebSettings.max_upload_bytes is 100 MiB.
    nginx.ingress.kubernetes.io/proxy-body-size: 100m
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
```

Write the pass entry into K0's reserved slot: in `docs/plans/2026-09-13-kubernetes-platform-facts.md`, under the heading `### 5.2 Affinity residuals` (tagged `[K7]`), replace K0's placeholder paragraph with the entry below. It names this outcome's commit by its subject, because the sha does not exist until Step 6 makes the commit; replace `YYYY-MM-DD` with the date of the Step 4 run.

```markdown
none — affinity dropped in test(deploy): qualify Kubernetes routing without session affinity and drop the ClientIP default
(measured YYYY-MM-DD on kind: `scripts/cicd/kubernetes-kind-smoke.sh` and the K7
selection both exit 0 over `tests/testcontainer/deployment/kubernetes/no-affinity-overlay`).
```

Run the render tests with the pinned tools on PATH — on this box a bare run SKIPS through `_require_kubectl`, and a skip is not a green:

```bash
cd "$(git rev-parse --show-toplevel)" && export PATH="${ELSPETH_K8S_TOOLS:-$PWD/.claude/lanes/k8s/bin}:$PATH" && pytest tests/unit/deployment/test_kubernetes_bundle.py -n 0 -rs > /tmp/k8s-K7-bundle.log 2>&1; echo exit=$?; grep -c "SKIPPED" /tmp/k8s-K7-bundle.log
```

Expected: `exit=0` and `0` skipped. Then re-run the K7 selection command (the first command of the live-cluster run above) once more against the flipped base (the no-affinity overlay's Service patch is now a no-op replace of `"None"` with `"None"`, and the control test measures K4's overlay, which inherits the base): expected `exit=0` — except that `test_client_ip_affinity_pins_one_replica_per_client` now FAILS, because the base no longer pins ClientIP. Delete that control test in the same commit and rename nothing else; the scatter test's known-negative is now the K1 render assertion above.

- [ ] **Step 5b (conditional — at least one K7 assertion failed on a durable surface): record the residual and keep ClientIP.**

Write into K0's reserved slot: in `docs/plans/2026-09-13-kubernetes-platform-facts.md`, under the heading `### 5.2 Affinity residuals` (tagged `[K7]`; the heading stays K0's), replace K0's placeholder paragraph with:

```markdown
Measured YYYY-MM-DD on kind (`scripts/cicd/kubernetes-kind-smoke.sh`, K4 harness,
`tests/testcontainer/deployment/kubernetes/no-affinity-overlay`). Routing without
session affinity is NOT qualified; `deploy/kubernetes/base/service.yaml` keeps
`sessionAffinity: ClientIP` and the AKS ingress keeps cookie affinity.

| Surface | Test | Assertion text (verbatim from the log) | Owner |
| --- | --- | --- | --- |
| <run replay / ticket single-use / composer in-flight / latest snapshot / Service scatter> | `<test id>` | `<the failing assert line and its message>` | filigree ticket id, filed with the log path |

Nothing in `deploy/` changes until every row above is closed and this entry
is replaced with the §5.2 pass entry by the task that closes it.
```

Fill the table from `/tmp/k8s-K7-step4.log`; one row per failing surface; file one filigree ticket per row (`filigree create` with the log path in the description) and put its id in the Owner column. Leave `service.yaml`, `test_kubernetes_bundle.py` and `ingress.yaml` untouched. The K7 tests stay in the file as written — a residual is a red the lane must keep reporting, not a test to delete or xfail.

- [ ] **Step 6: Commit (one of the two, by file pathspec — message BEFORE the separator, never a directory).**

Both blocks stage first, then run the safety check, then commit: `scripts/branch-safety-check.sh` inspects the STAGED set. The three `no-affinity-overlay/*.yaml` files are created by this task and untracked, so they get `git add -N` first (a commit pathspec only selects paths the index knows); then every path is staged by name, file paths only.

Flip outcome (8 files):

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N tests/testcontainer/deployment/kubernetes/no-affinity-overlay/kustomization.yaml tests/testcontainer/deployment/kubernetes/no-affinity-overlay/patch-composer-stall.yaml tests/testcontainer/deployment/kubernetes/no-affinity-overlay/secret-composer-stall.yaml
git add -- tests/testcontainer/deployment/test_kubernetes_kind.py tests/testcontainer/deployment/kubernetes/no-affinity-overlay/kustomization.yaml tests/testcontainer/deployment/kubernetes/no-affinity-overlay/patch-composer-stall.yaml tests/testcontainer/deployment/kubernetes/no-affinity-overlay/secret-composer-stall.yaml deploy/kubernetes/base/service.yaml tests/unit/deployment/test_kubernetes_bundle.py deploy/kubernetes/overlays/aks/ingress.yaml docs/plans/2026-09-13-kubernetes-platform-facts.md
git status --short
scripts/branch-safety-check.sh --intent commit
git commit -m "test(deploy): qualify Kubernetes routing without session affinity and drop the ClientIP default" -- tests/testcontainer/deployment/test_kubernetes_kind.py tests/testcontainer/deployment/kubernetes/no-affinity-overlay/kustomization.yaml tests/testcontainer/deployment/kubernetes/no-affinity-overlay/patch-composer-stall.yaml tests/testcontainer/deployment/kubernetes/no-affinity-overlay/secret-composer-stall.yaml deploy/kubernetes/base/service.yaml tests/unit/deployment/test_kubernetes_bundle.py deploy/kubernetes/overlays/aks/ingress.yaml docs/plans/2026-09-13-kubernetes-platform-facts.md
git show --stat HEAD
```

Residual outcome (5 files):

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N tests/testcontainer/deployment/kubernetes/no-affinity-overlay/kustomization.yaml tests/testcontainer/deployment/kubernetes/no-affinity-overlay/patch-composer-stall.yaml tests/testcontainer/deployment/kubernetes/no-affinity-overlay/secret-composer-stall.yaml
git add -- tests/testcontainer/deployment/test_kubernetes_kind.py tests/testcontainer/deployment/kubernetes/no-affinity-overlay/kustomization.yaml tests/testcontainer/deployment/kubernetes/no-affinity-overlay/patch-composer-stall.yaml tests/testcontainer/deployment/kubernetes/no-affinity-overlay/secret-composer-stall.yaml docs/plans/2026-09-13-kubernetes-platform-facts.md
git status --short
scripts/branch-safety-check.sh --intent commit
git commit -m "test(deploy): measure Kubernetes routing without session affinity; record the residuals and keep ClientIP" -- tests/testcontainer/deployment/test_kubernetes_kind.py tests/testcontainer/deployment/kubernetes/no-affinity-overlay/kustomization.yaml tests/testcontainer/deployment/kubernetes/no-affinity-overlay/patch-composer-stall.yaml tests/testcontainer/deployment/kubernetes/no-affinity-overlay/secret-composer-stall.yaml docs/plans/2026-09-13-kubernetes-platform-facts.md
git show --stat HEAD
```

Expected in either block: `git status --short` shows the three overlay paths as `A ` and the other named paths as `M `, and the staged set is exactly the files named; the safety check prints no `[FAIL]` line and exits 0 (on a FAIL, stop before the commit); `git show --stat HEAD` ends with `8 files changed` on the flip outcome or `5 files changed` on the residual outcome. Any other count: `git reset --mixed HEAD~1`, restage only the named paths, and commit again. The pre-commit secret scanner rescans every line of the touched Secret file, which is why both `stringData` lines carry `# secret-scan: allow-this-line`.
