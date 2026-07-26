# Finish Deferred Deployment Platforms Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to
> implement this plan task-by-task. In this session, use
> superpowers:subagent-driven-development, superpowers:test-driven-development,
> and superpowers:verification-before-completion as required by the task.

**Goal:** Complete the maintained Kubernetes, Azure Container Apps (ACA), and
machine-readable deployment-profile surfaces for release `0.7.2`, backed by
database-fenced transient overlap, fail-closed recovery, release-gated CI, and
live provider evidence.

**Architecture:** PostgreSQL-backed deployments use a three-part compatibility
key, persistent per-session operation fences, run ownership, and a durable
cross-database run-start saga. Kubernetes remains stop-before-start with
`Recreate`; ACA permits brief old/new overlap only for equal compatibility
keys, while schema or protocol changes use a forward-only maintenance cutover
with fresh database roles and a generation-specific NFS path. Public ACA
support is promoted from release-candidate to maintained only after review,
an immutable image build, live non-production acceptance, and receipt binding.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, SQLite,
pytest/testcontainers, Docker, Kustomize/kubectl, kind, Bicep, YAML/JSON
Schema, Prometheus/OpenTelemetry, and GitHub Actions.

**Design:**
`docs/superpowers/specs/2026-07-26-finish-deferred-deployment-platforms-design.md`

---

## Execution Contract

- The implementation already has an isolated worktree at
  `/home/john/elspeth/.worktrees/deferred-platform-completion`, on branch
  `codex/deferred-platform-completion`, based on
  `release/0.7.2@696b3d1414ed7a6789c8f25bf5cbdc5450385bdd`. Do not create a
  second worktree, reuse the retired cross-platform worktree, or cherry-pick the
  reverted ACA bundle as finished work.
- Run all commands from that worktree. Install with
  `env -u VIRTUAL_ENV uv sync --frozen --all-extras`.
- After this plan receives a GO verdict, commit this revised design and plan as
  one docs-only commit before Task 1. Do not begin implementation with the
  reviewed plan left as an uncommitted worktree change.
- Follow RED -> GREEN -> focused regression -> commit for every task. A failing
  test must fail for the named missing behavior before production code changes.
- Heartbeat the already-building runtime and current provider/closeout claims at
  task boundaries and before long review, local-gate, image-build, or live-cloud
  phases. Start only genuinely unclaimed issues.
- Keep the SQLite path single-process, but route it through the same
  `SessionOperationAuthority` API and table-backed exact-CAS contract as shared
  service code. SQLite has no distributed membership/takeover; it never has an
  unfenced mutation bypass. Distributed coordination requires external
  PostgreSQL and fails startup when its repository is unavailable.
- Session creation and epoch-1 fence acquisition are one lifecycle operation.
  IDs are server-generated and non-caller-selectable. Closed kind `create`
  initializes under a non-null epoch-1 operation ID/token/owner/lease, then
  releases that fence in the same transaction before return; the first later
  operation advances to epoch 2. Physical deletion under
  the current archive fence may cascade the session and fence; there is no
  separate deleted-ID registry or permanent-ID promise, only the guarantee that stale
  update-only CAS cannot recreate deleted rows.
- Keep `WEB_CONCURRENCY=1`, one process per container, one steady-state replica,
  and `horizontal_scale_supported: false` for all five profiles.
- `SESSION_SCHEMA_EPOCH` advances from 36 to 37. Keep
  `SQLITE_SCHEMA_EPOCH == 29` unless implementation discovers that the
  Landscape schema itself must change; if it does, stop, revise the design and
  plan, and repeat plan review before changing it.
- Set `WEB_COORDINATION_PROTOCOL_VERSION = 1`. Increment it for an incompatible
  membership, fence, typed-start-permit/saga, atomic-baseline, cancellation,
  recovery, cleanup-claim, or execution-authority semantic change. Do not bump
  it for compatible telemetry labels/exporters or provider/docs-only changes.
  Any bump requires a hard cut and synchronized code, tests, envelopes,
  profiles, runbooks, and receipts.
- Acquire authorities in this order:
  `SessionOperationFence -> RunOwnershipFence -> Landscape CoordinationToken`.
  Cancellation request insertion is the only non-owner mutation, and only
  records a durable request.
- Task 12C exclusively registers the `multi_instance` and `kubernetes_kind`
  markers in `pyproject.toml` before either heavy suite is committed. Only Task
  20 may later edit `.github/workflows/ci.yaml`,
  `.github/workflows/build-push.yaml`, or
  `tests/unit/deployment/test_deployment_ci_gates.py`. Task 2B establishes the
  Docker/Node and initial build-workflow contract; Task 20 extends its tests
  while taking sole workflow ownership. This serial ownership avoids workflow
  and marker conflicts.
- Tasks 3-13 own `src/elspeth/web/sessions/**` serially. Task 3 owns every
  epoch-37 code/test/doc fact atomically, without changing platform support
  status. Tasks 18, 19, and 24 own profiles and public deployment claims
  serially. Task 24 is the only task allowed to promote ACA from
  `release-candidate` to `maintained`.
- Do not alter the refactored AWS facade boundary. Shared changes must retain
  `src/elspeth/web/aws_ecs_acceptance.py` as the facade and private owners under
  `src/elspeth/web/_aws_ecs_acceptance/`.
- Live ACA acceptance is mandatory. Task 22 performs a read-only authority,
  Azure-account, registry, publish-rights, signing, and tooling preflight before
  freezing or publishing anything. If any prerequisite is absent, record the
  exact missing authority on the ACA task and stop before image publication.
  Do not fabricate, waive, mock, or substitute local evidence.
- The operator signing issue `elspeth-18fe6e759e` does not block Tasks 1-20,
  but it blocks Task 21 completion, source freeze, live ACA acceptance, and the
  requested local merge. Agent-merge still runs trust-tier/boundary scanners and non-keyed
  coverage/edit checks in `shape-only-when-key-missing` mode; only
  cryptographic signature authenticity remains unverified. Operator-release
  requires the HMAC key and reruns the same inventory in `required` mode. Its
  green result is required before Task 22, after receipt binding, and after the
  merge. If operator authority is absent, update the P0 with the exact blocker
  and stop at Task 21; never treat shape-only as authoritative.

## Tracker Graph

At implementation start, reconcile live Filigree state and use atomic
`work_start` / `start-work --advance`; never claim and transition separately.

```text
elspeth-aad3788b81 baseline example-path bug (repair first)

elspeth-b5d7aa5655 maintained multi-replica-safe web runtime (already building)
  +-- elspeth-3d1d1fcb6c session/blob mutation-read-run admission coordination
  +-- elspeth-245b21351b post-CAS restore leadership loss
  +-- elspeth-f321e3ff21 bind recovery to exact implementation identity

elspeth-d335ba121b fenced lease recovery
  `-- implementation landed in a844f6749; verify and reconcile only

elspeth-cec5c47cef Wardline gate recognizes zero ELSPETH trust boundaries
  `-- Task 19A repairs it before Task 20 installs the release gate

create P1 Kubernetes task -----------------------------+
                                                       +--> create P1 closeout task
elspeth-b5d7aa5655 --> create P1 ACA task -------------+
```

Create exactly three new issues only when equivalent live issues still do not
exist: Kubernetes/kind, ACA/Bicep/provider acceptance, and final profiles/docs/
CI/merge closeout. Make ACA depend on `elspeth-b5d7aa5655`; make closeout depend
on Kubernetes and ACA. Link the open runtime bugs to
`elspeth-b5d7aa5655` and close them only after their exact regressions pass.
`elspeth-d335ba121b` is not new implementation scope: commit `a844f6749`
already landed its strict/legacy scheduler lease recovery split. Reconcile and
close it only after the exact Task 2 regressions pass. Heartbeat the already-
building `elspeth-b5d7aa5655` claim; call `work_start` only for unclaimed issues
and never steal or restart a live claim.
Do not duplicate the landed Docker work (`elspeth-8d2bea608f`) and do not make
this work depend wholesale on the broad DAG corpus (`elspeth-ef29ef6ba4`).

## Dependency and Commit Order

```text
1 baseline repair
  -> 2 tracker reconciliation -> 2A local tool bootstrap
  -> 2B Docker frontend/runtime input alignment
  -> 3 contracts/schema/epoch
  -> 4 session-operation fence
  -> 5 fence every session/blob/composer mutation
  -> 6 membership + run ownership
  -> 7 lifecycle/readiness/compatibility
  -> 8 durable input envelope -> 8A Sessions permit issuance
  -> 8B atomic Landscape baseline -> 9 restartable saga
  -> 9A execution-authority checks
  -> 10 recovery admission + cancel-only reconciliation
  -> 11 cross-replica tickets/progress/rate limiting
  -> 12 audit-first + Sessions cleanup
  -> 12B closed telemetry/exporter runtime delta
  -> 12C marker registration
  -> 13 two-process failure/cancel/race corpus
       -> 16 ACA Bicep -> 17 ACA validator/driver/schema -> 18 runbooks
       -> 19 profiles (RC)

3 -> 14 Kubernetes base -> 15 kind smoke ------------------------------+
15 + 17 + 18 + 19 -> 19A non-inert Wardline trust surface
                    -> 20 unified CI and accepted-digest release gate ---+
20 -> 21 review/repair/full pre-acceptance verification
   -> 22 freeze source + immutable image
   -> 23 live ACA acceptance
   -> 24 bind receipt and promote claims (closed path allowlist)
   -> 25 binding verifier and complete gates
   -> 26 independent final review/repair
   -> 27 merge to release/0.7.2
```

Tasks 14-15 may run while Tasks 4-13 proceed, but they must not edit the shared
CI files. Every numbered implementation task has one logical commit. If a RED
test unexpectedly passes or an expected path differs, stop that task, inspect
the live implementation, and update this plan before broadening the patch.

## Verified Tool and Action Pins

Use these exact versions and Linux amd64 digests in scripts, workflow contract
tests, and documentation:

| Tool/action | Exact pin |
|---|---|
| Node.js | `v24.18.0`, Linux x64 tar.xz SHA-256 `55aa7153f9d88f28d765fcdad5ae6945b5c0f98a36881703817e4c450fa76742` |
| Node Docker base | `node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d` (multiarch index) |
| npm | `11.6.2`, registry tarball SHA-256 `585f95094ee5cb2788ee11d90f2a518a7c9ef6e083fa141d0b63ca3383675a20` |
| cosign | `v3.0.6`, `cosign-linux-amd64` SHA-256 `c956e5dfcac53d52bcf058360d579472f0c1d2d9b69f55209e256fe7783f4c74` |
| Bicep | `v0.45.15`, `bicep-linux-x64` SHA-256 `ff5b194b042c220df4a50d6768ed1d6c39a32894bfdc4ff83d62b115d966a7ce` |
| kubectl | `v1.36.3`, Linux amd64 SHA-256 `ebbd080e7c2e275093b55915722043257eb24004363e20acb3c4d71919f88336` |
| kind | `v0.32.0`, Linux amd64 SHA-256 `50030de23cf40a18505f20426f6a8506bedf13c6e509244bd1fa9463721b0f54` |
| kind node | `kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5` |
| Azure CLI | `mcr.microsoft.com/azure-cli:2.88.0@sha256:0ada293b85df638db8db5ee3f3f74893724b1ce66a1dd7da5470b1dd341c202b` (Linux amd64) |
| PostgreSQL client | `postgres:17.6-bookworm@sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3` (index; Linux amd64 child `sha256:45cd22f8d32e189d245403954882f88e7a8714301fda80dab6da90f1265b25a3`) |
| checkout | `actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10` |
| setup-node | `actions/setup-node@48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e` |
| setup-uv | `astral-sh/setup-uv@38f3f104447c67c051c4a08e39b64a148898af3a` |
| upload-artifact | `actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` |

The exact container test command shape is:

```bash
env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer <explicit-paths>
```

Dedicated lanes add their marker intersection, for example
`-m "testcontainer and multi_instance"` or
`-m "testcontainer and kubernetes_kind"`. Never mix unit paths into a marker-
selected container command; run the unit command separately.

## Task 1: Repair the Baseline and Establish a Green Starting Point

**Issue:** `elspeth-aad3788b81`

**Files:**

- Modify: `tests/e2e/examples/test_shipped_examples.py`

1. Run the isolated regression and confirm the current order-dependent failure:

   First query `elspeth-aad3788b81`. Atomically start it only if unclaimed; if
   this implementation identity already owns it, heartbeat instead; if another
   active owner holds it, stop rather than stealing the claim.

   ```bash
   env -u VIRTUAL_ENV uv sync --frozen --all-extras
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     'tests/e2e/examples/test_shipped_examples.py::TestShippedExamples::test_shipped_journal_paths_resolve_next_to_audit_db_with_hostile_env[landscape-journal]'
   ```

   Expected RED: `sqlite3.OperationalError: unable to open database file`.

2. In `_copy_example_to_tmp`, create the copied example's `runs/` directory
   after `shutil.copytree`. Keep the test independent of a previously executed
   example and do not change production path resolution.
3. Rerun the isolated parameter, then the whole file twice in separate
   processes:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     'tests/e2e/examples/test_shipped_examples.py::TestShippedExamples::test_shipped_journal_paths_resolve_next_to_audit_db_with_hostile_env[landscape-journal]'
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 tests/e2e/examples/test_shipped_examples.py
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 tests/e2e/examples/test_shipped_examples.py
   ```

   Expected GREEN: all selected tests pass both times.
4. Record the known starting-point gates. This is not the local release gate:
   Task 21 also covers slow/stress/performance, frontend, deployment, and
   receipt-aware work.

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 tests/unit tests/integration
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer tests/testcontainer/web
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0
   ```

   Record totals and any unrelated baseline failure before proceeding.
5. Commit:

   ```bash
   git add tests/e2e/examples/test_shipped_examples.py
   git commit -m "test(examples): isolate copied journal output paths"
   ```

**Done when:** the isolated defect and full example file are order-independent,
the baseline result is recorded, and `elspeth-aad3788b81` is closed with the
commit and exact test commands.

## Task 2: Reconcile and Start the Tracker Graph

**Files:** none.

1. Query the live canonical issues named in the Tracker Graph. Heartbeat the
   existing `elspeth-b5d7aa5655` building claim; do not call `work_start` on it
   or another actively claimed issue.
2. Atomically start only unclaimed open implementation issues with the
   implementation identity, using `advance=true` where a bug remains in
   triage/confirmed. Assign `elspeth-245b21351b` to Task 10's `resume.py`
   repair and `elspeth-f321e3ff21` to Tasks 8/10's immutable implementation-
   identity admission.
3. Verify the already-landed `elspeth-d335ba121b` fix at `a844f6749` without
   editing its implementation:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/core/landscape/test_scheduler_fencing.py \
     tests/unit/core/landscape/test_scheduler_lease_recovery_races.py \
     tests/unit/core/landscape/test_leader_fence_stale_token.py \
     tests/unit/engine/test_lease_recovery_sweep.py \
     tests/unit/engine/test_scheduler_drain_characterization.py
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/e2e/recovery/test_concurrent_resume.py \
     tests/e2e/recovery/test_follower_coordination_chaos.py \
     tests/e2e/recovery/test_liveness_aware_reap.py \
     tests/e2e/recovery/test_suspended_winner_fences.py
   ```

   Reconcile/close that issue with commit and test evidence only if both
   commands pass; otherwise reopen scope from the exact regression rather than
   reimplementing the same fix speculatively.
4. Create only the three missing P1 tasks and add the dependencies shown above.
5. Add a comment recording branch
   `codex/deferred-platform-completion`, base `696b3d141`, and this plan path.

**Done when:** ownership and dependencies are live and no duplicate Docker,
Kubernetes, ACA, or closeout issue exists. No Git commit is expected.

## Task 2A: Bootstrap Checksum-Pinned Local Deployment Tools

**Files:** ignored cache only: `.cache/deployment-tools/`.

1. Verify `.cache/` is ignored and host prerequisites exist:

   ```bash
   git check-ignore -q .cache
   command -v curl sha256sum docker git jq uv tar xz
   docker version
   docker buildx version
   git --version
   uv --version
   ```

2. Create `.cache/deployment-tools/bin`, use a private `mktemp -d` staging
   directory with an EXIT trap, download Node.js `v24.18.0`, npm `11.6.2`,
   cosign `v3.0.6`, kubectl `v1.36.3`, kind `v0.32.0`, and Bicep `v0.45.15`
   from their official release URLs, and verify the exact SHA-256 values before
   atomically moving executable files into the cache. A checksum mismatch
   deletes the staged artifact and stops. The host's Node 20 installation is
   not an eligible fallback.

   ```bash
   export DEPLOYMENT_TOOL_ROOT="$PWD/.cache/deployment-tools"
   mkdir -p "$DEPLOYMENT_TOOL_ROOT/bin"
   tool_stage=$(mktemp -d "$DEPLOYMENT_TOOL_ROOT/stage.XXXXXX")
   trap 'rm -rf -- "$tool_stage"' EXIT
   curl --fail --location --proto '=https' --tlsv1.2 \
     https://nodejs.org/download/release/v24.18.0/node-v24.18.0-linux-x64.tar.xz \
     -o "$tool_stage/node.tar.xz"
   curl --fail --location --proto '=https' --tlsv1.2 \
     https://registry.npmjs.org/npm/-/npm-11.6.2.tgz -o "$tool_stage/npm.tgz"
   curl --fail --location --proto '=https' --tlsv1.2 \
     https://github.com/sigstore/cosign/releases/download/v3.0.6/cosign-linux-amd64 \
     -o "$tool_stage/cosign"
   curl --fail --location --proto '=https' --tlsv1.2 \
     https://dl.k8s.io/release/v1.36.3/bin/linux/amd64/kubectl -o "$tool_stage/kubectl"
   curl --fail --location --proto '=https' --tlsv1.2 \
     https://kind.sigs.k8s.io/dl/v0.32.0/kind-linux-amd64 -o "$tool_stage/kind"
   curl --fail --location --proto '=https' --tlsv1.2 \
     https://github.com/Azure/bicep/releases/download/v0.45.15/bicep-linux-x64 -o "$tool_stage/bicep"
   printf '%s  %s\n' \
     55aa7153f9d88f28d765fcdad5ae6945b5c0f98a36881703817e4c450fa76742 "$tool_stage/node.tar.xz" \
     585f95094ee5cb2788ee11d90f2a518a7c9ef6e083fa141d0b63ca3383675a20 "$tool_stage/npm.tgz" \
     c956e5dfcac53d52bcf058360d579472f0c1d2d9b69f55209e256fe7783f4c74 "$tool_stage/cosign" \
     ebbd080e7c2e275093b55915722043257eb24004363e20acb3c4d71919f88336 "$tool_stage/kubectl" \
     50030de23cf40a18505f20426f6a8506bedf13c6e509244bd1fa9463721b0f54 "$tool_stage/kind" \
     ff5b194b042c220df4a50d6768ed1d6c39a32894bfdc4ff83d62b115d966a7ce "$tool_stage/bicep" \
     | sha256sum --check --strict
   tar -xJf "$tool_stage/node.tar.xz" -C "$tool_stage"
   node_target="$DEPLOYMENT_TOOL_ROOT/node-v24.18.0"
   if test ! -e "$node_target"; then
     mv "$tool_stage/node-v24.18.0-linux-x64" "$node_target"
   fi
   npm_target="$DEPLOYMENT_TOOL_ROOT/npm-11.6.2"
   if test ! -e "$npm_target"; then
     npm_stage="$tool_stage/npm-11.6.2"
     PATH="$node_target/bin:$PATH" "$node_target/bin/npm" install --global \
       --prefix "$npm_stage" "$tool_stage/npm.tgz"
     mv "$npm_stage" "$npm_target"
   fi
   install -m 0755 "$tool_stage/cosign" "$DEPLOYMENT_TOOL_ROOT/bin/cosign.new"
   install -m 0755 "$tool_stage/kubectl" "$DEPLOYMENT_TOOL_ROOT/bin/kubectl.new"
   install -m 0755 "$tool_stage/kind" "$DEPLOYMENT_TOOL_ROOT/bin/kind.new"
   install -m 0755 "$tool_stage/bicep" "$DEPLOYMENT_TOOL_ROOT/bin/bicep.new"
   mv -f "$DEPLOYMENT_TOOL_ROOT/bin/cosign.new" "$DEPLOYMENT_TOOL_ROOT/bin/cosign"
   mv -f "$DEPLOYMENT_TOOL_ROOT/bin/kubectl.new" "$DEPLOYMENT_TOOL_ROOT/bin/kubectl"
   mv -f "$DEPLOYMENT_TOOL_ROOT/bin/kind.new" "$DEPLOYMENT_TOOL_ROOT/bin/kind"
   mv -f "$DEPLOYMENT_TOOL_ROOT/bin/bicep.new" "$DEPLOYMENT_TOOL_ROOT/bin/bicep"
   ```
3. Pull the exact Linux-amd64 Azure CLI image from the pin table and verify it:

   ```bash
   docker pull --platform linux/amd64 \
     mcr.microsoft.com/azure-cli:2.88.0@sha256:0ada293b85df638db8db5ee3f3f74893724b1ce66a1dd7da5470b1dd341c202b
   docker run --rm --platform linux/amd64 \
     mcr.microsoft.com/azure-cli:2.88.0@sha256:0ada293b85df638db8db5ee3f3f74893724b1ce66a1dd7da5470b1dd341c202b \
     az version
   ```

   Expected: Azure CLI reports `2.88.0`. The host has no required `az`
   installation; all plan commands use this immutable image.
   Also inspect the pinned Node multiarch base and PostgreSQL client index,
   assert the PostgreSQL linux/amd64 child digest, and run `psql --version`
   from that exact platform:

   ```bash
   docker buildx imagetools inspect \
     node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d
   docker buildx imagetools inspect --raw \
     postgres:17.6-bookworm@sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3 \
     | jq -e '.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest == "sha256:45cd22f8d32e189d245403954882f88e7a8714301fda80dab6da90f1265b25a3"'
   docker run --rm --platform linux/amd64 \
     postgres:17.6-bookworm@sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3 \
     psql --version
   ```
4. Export `DEPLOYMENT_TOOL_ROOT="$PWD/.cache/deployment-tools"` and prepend
   `$DEPLOYMENT_TOOL_ROOT/npm-11.6.2/bin`,
   `$DEPLOYMENT_TOOL_ROOT/node-v24.18.0/bin`, and
   `$DEPLOYMENT_TOOL_ROOT/bin` to `PATH` for every later local command. Assert
   `node --version` is exactly `v24.18.0`, `npm --version` is exactly `11.6.2`,
   and verify `cosign version`, `kubectl version --client`, `kind version`, and
   `bicep --version`.
   The local release-gate script must perform the same exact Node/npm assertions
   before frontend work. Never write to `/tmp`, `/usr`, or another shared tool
   directory.

**Done when:** every tool reports its pinned version from ignored local state.
No Git commit is expected.

## Task 2B: Align Docker Frontend Runtime and Remove Public README from Image Inputs

**Files:**

- Modify: `Dockerfile`
- Create: `tests/unit/test_node_runtime_contract.py`
- Modify: `tests/unit/test_build_push_release_checks.py`

1. Write RED contracts requiring the frontend stage to use exactly
   `node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d`,
   install and verify npm `11.6.2`, and remain valid for both amd64 and arm64.
   Require the build-workflow contract to use Node `24.18.0`/npm `11.6.2` and
   reject the old base.
2. Add a RED image-input test rejecting `COPY README.md` or any host README
   dependency. Require a deterministic build-only README stub with fixed bytes
   and timestamp before project installation so Hatch metadata still builds.
   Prove changing host `README.md` is not a Docker input.
3. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/test_node_runtime_contract.py \
     tests/unit/test_build_push_release_checks.py
   ```

   Expected RED: the Dockerfile uses the older Node base and copies README.
4. Update only the Dockerfile and contracts. Build both target stages with
   buildx, assert `node --version == v24.18.0` and `npm --version == 11.6.2`,
   and prove the deterministic stub is the package build input.
5. Commit:

   ```bash
   git add Dockerfile tests/unit/test_node_runtime_contract.py \
     tests/unit/test_build_push_release_checks.py
   git commit -m "build: align frontend runtime and image inputs"
   ```

**Done when:** the Docker frontend is pinned for both target architectures and
later README claim edits are cryptographically disjoint from image inputs.

## Task 3: Define Coordination Contracts and Advance Session Epoch 37

**Files:**

- Create: `src/elspeth/web/coordination/__init__.py`
- Create: `src/elspeth/web/coordination/contracts.py`
- Modify: `src/elspeth/web/sessions/models.py`
- Modify: `src/elspeth/web/sessions/schema.py`
- Modify: `src/elspeth/web/sessions/protocol.py`
- Modify: `src/elspeth/web/secrets/user_store.py`
- Modify: `src/elspeth/web/_aws_ecs_acceptance/receipt_contracts.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `website/get-started.html`
- Modify: `docs/guides/sharing-pipelines.md`
- Modify: `docs/product/current-state.md`
- Modify: `docs/runbooks/staging-session-db-recreation.md`
- Modify: `docs/runbooks/aws-ecs-deployment.md`
- Create: `tests/unit/web/coordination/test_contracts.py`
- Test: `tests/unit/web/sessions/test_schema.py`
- Test: `tests/unit/web/sessions/test_blob_inline_resolutions_schema.py`
- Test: `tests/unit/web/sessions/test_interpretation_events_table.py`
- Test: `tests/unit/web/secrets/test_user_store.py`
- Test: `tests/integration/web/composer/guided/test_schema9_epoch.py`
- Test: `tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py`
- Test: `tests/unit/web/aws_ecs_acceptance/test_cleanup_control_service.py`
- Test: `tests/unit/docs/test_readme_release_surface.py`
- Test: `tests/unit/docs/test_composer_capability_docs.py`
- Test: `tests/unit/docs/test_release_version_surfaces.py`
- Test: `tests/unit/docs/test_staging_session_recreation_policy.py`
- Test: `tests/unit/website/test_release_site_contract.py`

1. Write RED tests for immutable `CompatibilityKey`, `SessionOperationFence`,
   `RunOwnershipFence`, bounded state/reason enums, and leak-safe fence-loss
   errors. Assert the authority fields exactly:

   ```text
   SessionOperationFence(session_id, operation_id, lease_token, operation_epoch)
   RunOwnershipFence(run_id, owner_instance_id, owner_epoch)
   CompatibilityKey(session_epoch, landscape_epoch, coordination_protocol)
   ```

   Also assert epoch 37, `WEB_COORDINATION_PROTOCOL_VERSION == 1`, and the exact
   tables/indexes for instances, session-operation authority, durable typed web
   start permits and their start-vs-cancel state, run ownership/saga state,
   `run_execution_inputs`, tickets, composer inflight/progress, rate-limit
   buckets/events, and bounded cleanup claims.
   The recreated `user_secrets` table has a non-null positive monotonic
   `version`; every upsert advances it atomically so an envelope can bind a
   durable row identity plus version without preserving an obsolete value.

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/web/coordination/test_contracts.py \
     tests/unit/web/sessions/test_schema.py \
     tests/unit/web/sessions/test_blob_inline_resolutions_schema.py \
     tests/unit/web/sessions/test_interpretation_events_table.py \
     tests/unit/web/secrets/test_user_store.py \
     tests/integration/web/composer/guided/test_schema9_epoch.py \
     tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py \
     tests/unit/web/aws_ecs_acceptance/test_cleanup_control_service.py \
     tests/unit/docs/test_readme_release_surface.py \
     tests/unit/docs/test_composer_capability_docs.py \
     tests/unit/docs/test_release_version_surfaces.py \
     tests/unit/docs/test_staging_session_recreation_policy.py \
     tests/unit/website/test_release_site_contract.py
   ```

   Expected RED: missing coordination contracts/tables and epoch 36 assertions.
2. Add frozen value objects and closed enums. Encode the exact protocol bump
   rules in code comments/contracts so a membership, fence, typed-permit/saga,
   baseline, cancellation, recovery, cleanup-claim, or execution-authority
   incompatibility must bump from 1, while compatible telemetry/provider work
   does not. Add database constraints for
   nonblank IDs, positive epochs, valid states, indexed expiry fields, and one
   persistent operation row per session. Include `create` in the operation-kind
   vocabulary and require non-null operation ID/token/owner/lease fields even
   when released; `released_at` is the inactive-state discriminator.
   `lease_token` is random authority, distinct from diagnostic
   `owner_instance_id`.
3. Set `SESSION_SCHEMA_EPOCH = 37`; preserve guided schema 10 and Landscape
   epoch 29. Keep the pre-release delete/recreate posture rather than adding a
   migration path. Update every named current 0.7.2 code, test, runbook,
   website, and release surface in the same commit. Preserve historical 0.7.1
   epoch-35 text. Change the AWS structural label from the 35-to-36 blob-only
   label to an exact reviewed 35-to-37 coordination-schema description, while
   preserving the facade/private-package boundary. Do not promote ACA support.
4. Run the RED command. Expected GREEN: exact schema and stale epoch refusal
   pass for SQLite, no current 0.7.2 surface says epoch 36, and PostgreSQL
   behavior remains reserved for Task 4.
5. Commit:

   ```bash
   git add src/elspeth/web/coordination src/elspeth/web/sessions \
     src/elspeth/web/_aws_ecs_acceptance/receipt_contracts.py \
     src/elspeth/web/secrets/user_store.py \
     README.md CHANGELOG.md website/get-started.html \
     docs/guides/sharing-pipelines.md docs/product/current-state.md \
     docs/runbooks/staging-session-db-recreation.md docs/runbooks/aws-ecs-deployment.md \
     tests/unit/web/coordination tests/unit/web/sessions \
     tests/unit/web/secrets/test_user_store.py \
     tests/integration/web/composer/guided/test_schema9_epoch.py \
     tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py \
     tests/unit/web/aws_ecs_acceptance/test_cleanup_control_service.py \
     tests/unit/docs/test_readme_release_surface.py \
     tests/unit/docs/test_composer_capability_docs.py \
     tests/unit/docs/test_release_version_surfaces.py \
     tests/unit/docs/test_staging_session_recreation_policy.py \
     tests/unit/website/test_release_site_contract.py
   git commit -m "feat(web): define epoch-37 coordination schema"
   ```

**Done when:** session epoch is 37, Landscape remains 29, coordination protocol
is exactly 1, and the schema encodes all authority and expiry invariants without
exposing raw database handles or a deleted-ID registry.

## Task 4: Implement the Persistent Session-Operation Fence

**Issue:** part of `elspeth-3d1d1fcb6c`. The unrelated scheduler lease bug
`elspeth-d335ba121b` was reconciled in Task 2 and is not implemented here.

**Files:**

- Create: `src/elspeth/web/coordination/repository.py`
- Create: `src/elspeth/web/coordination/sqlite_authority.py`
- Modify: `src/elspeth/web/sessions/service.py`
- Modify: `src/elspeth/web/sessions/protocol.py`
- Create: `tests/unit/web/coordination/test_session_operation_fence.py`
- Create: `tests/unit/web/coordination/test_sqlite_session_operation_authority.py`
- Create: `tests/testcontainer/web/test_session_operation_fence_postgres.py`

1. Write RED cases proving:

   - `create_session_with_initial_fence` mints a server-generated,
     non-caller-selectable UUID, create operation ID/token, owner and database-
     clock lease; it inserts the session plus closed-kind `create` epoch-1
     fence, performs every initialization write under that fence, then
     atomically releases it before commit/return; collision retries the whole
     operation with a new ID;
   - the persisted epoch-1 row is inactive with every authority field non-null,
     `released_at == lease_expires_at ==` database release time, and creation
     can never return or leak an active lease;
   - the first later operation locks epoch 1, advances to epoch 2, mints a new
     operation ID/token/owner/lease and clears `released_at`;
   - release retains the authority row while the parent session exists;
   - reacquisition after release or expiry uses a different token and a
     strictly greater epoch;
   - takeover requires both operation and owner-instance leases to expire by
     PostgreSQL time;
   - two claimants produce exactly one winner;
   - renew, progress, release, and every durable mutation CAS exact
     `session_id + operation_id + lease_token + operation_epoch`; and
   - a stale CAS changes zero rows and raises `SessionOperationFenceLost`;
   - `SQLiteLocalSessionOperationAuthority` implements the same acquire, renew,
     CAS, release, archive-delete, and fence-lost signatures without membership
     or peer takeover; and
   - physical session delete holds the current archive fence and deletes parent
     plus fence by one transaction/cascade; no deleted-ID registry/table is
     created, while stale update-only CAS remains incapable of recreating
     either row.

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/web/coordination/test_session_operation_fence.py \
     tests/unit/web/coordination/test_sqlite_session_operation_authority.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer \
     tests/testcontainer/web/test_session_operation_fence_postgres.py
   ```

   Expected RED: repository methods are absent.
2. Implement one `SessionOperationAuthority` protocol. Make session creation
   and initial acquisition one lifecycle API; no caller may supply/adopt an ID
   or separately insert epoch 1. Define active exactly as `released_at IS NULL`
   plus a live database-clock lease. PostgreSQL later acquisition
   uses a row lock and sessions-database time. SQLite combines the existing
   process/file lock with a table-backed random token/epoch CAS in the same
   mutation transaction; shared services contain no SQLite fence bypass.
3. Keep the fence for as long as its parent session is soft-retained. The sole
   deletion path is schema-owned cascade during a current-fence physical
   archive delete. Session IDs remain server-generated/non-caller-selectable;
   acquire/renew/mutate/release and stale-retry paths are update-only. Cleanup
   cannot independently delete/reset the row, and no permanent deleted-ID
   state is introduced.
4. Rerun both commands. Expected GREEN includes the idle/restart/takeover
   retained-row cases and exact stale-state immutability.
5. Commit:

   ```bash
   git add src/elspeth/web/coordination src/elspeth/web/sessions \
     tests/unit/web/coordination/test_session_operation_fence.py \
     tests/unit/web/coordination/test_sqlite_session_operation_authority.py \
     tests/testcontainer/web/test_session_operation_fence_postgres.py
   git commit -m "feat(web): persist monotonic session operation fences"
   ```

**Done when:** epochs are monotonic while a session exists, physical deletion
is current-fenced, epoch-1 creation is already inactive before return, first
later work begins at epoch 2, and stale or suspended workers cannot recreate
deleted state or take a fallback write path.

## Task 5: Fence Every Session, Composer, Guided, Blob, and Archive Mutation

**Issue:** `elspeth-3d1d1fcb6c`.

**Files:**

- Modify: `src/elspeth/web/sessions/service.py`
- Modify: `src/elspeth/web/sessions/guided_operations.py`
- Modify: `src/elspeth/web/sessions/routes/messages.py`
- Modify: `src/elspeth/web/sessions/routes/runs.py`
- Modify: `src/elspeth/web/sessions/routes/sessions.py`
- Modify: `src/elspeth/web/sessions/routes/composer/compose.py`
- Modify: `src/elspeth/web/sessions/routes/composer/proposals.py`
- Modify: `src/elspeth/web/sessions/routes/composer/pipeline_settlement.py`
- Modify: `src/elspeth/web/sessions/routes/composer/state.py`
- Modify: `src/elspeth/web/sessions/routes/interpretation.py`
- Modify: `src/elspeth/web/sessions/audit_story_service.py`
- Modify: `src/elspeth/web/sessions/proposal_blob_refs.py`
- Modify: `src/elspeth/web/sessions/engine.py`
- Modify: `src/elspeth/web/composer/service.py`
- Modify: `src/elspeth/web/composer/progress.py`
- Modify: `src/elspeth/web/composer/tutorial_service.py`
- Modify: `src/elspeth/web/composer/reviewed_source_authority.py`
- Modify: `src/elspeth/web/composer/tools/sources.py`
- Modify: `src/elspeth/web/composer/tools/blobs.py`
- Modify: `src/elspeth/web/blobs/service.py`
- Modify: `src/elspeth/contracts/blobs.py`
- Modify: `src/elspeth/web/execution/service.py`
- Modify: `src/elspeth/web/execution/outputs.py`
- Create: `tests/unit/architecture/test_session_db_mutation_authority.py`
- Create: `tests/unit/web/sessions/test_operation_fence_wiring.py`
- Test: `tests/unit/web/blobs/test_service.py`
- Test: `tests/unit/web/composer/test_blob_inline_tools.py`
- Create: `tests/unit/contracts/test_web_blob_fencing.py`
- Create: `tests/testcontainer/web/test_session_mutation_fencing_postgres.py`

1. Before wiring, build an AST/repository inventory of **every** Sessions-
   database mutator and classify it as session-scoped or global. A session-
   scoped writer must accept and exact-CAS `SessionOperationFence` in the same
   transaction. A global writer must be explicitly listed with its separate
   authority and cannot accept a session ID as an implicit bypass. Any new or
   unclassified mutator fails
   `tests/unit/architecture/test_session_db_mutation_authority.py`.
2. Write RED tests for compose/recompose, message append, proposal creation and
   settlement, guided seed/import/revert/settlement, blob finalization,
   execution admission, progress update, archive, and delete. Include blob
   creation/reattachment/finalization paths in `web/composer/tools/blobs.py`,
   `web/blobs/service.py`, and `contracts/blobs.py`, not only route callers.
   Guided writes
   must carry both `GuidedOperationFence` and `SessionOperationFence`.
   Include the current discovered owners: composer tutorial/state/reviewed-
   source/source-tool writers; audit-story and proposal-reference writes;
   interpretation events; execution admission/projection/output writers; and
   the existing service/routes/guided/blob paths. Archive/delete must refuse an
   active operation lease. Cross-replica reads must observe durable state rather
   than local broadcaster state.
3. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/web/sessions/test_operation_fence_wiring.py \
     tests/unit/architecture/test_session_db_mutation_authority.py \
     tests/unit/web/blobs/test_service.py \
     tests/unit/web/composer/test_blob_inline_tools.py \
     tests/unit/contracts/test_web_blob_fencing.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer \
     tests/testcontainer/web/test_session_mutation_fencing_postgres.py
   ```

   Expected RED: at least one mutation accepts no fence or writes after loss.
4. Thread the typed fence through service boundaries; make every affected write
   execute its CAS in the same transaction as the state change. Treat composer
   inflight rows as capacity bookkeeping, never correctness authority.
5. Rerun the commands plus `tests/unit/web/sessions`. Expected GREEN: stale
   workers change neither row state nor event sequence.
6. Commit:

   ```bash
   git add src/elspeth/web/sessions src/elspeth/web/composer \
     src/elspeth/web/blobs src/elspeth/web/execution \
     src/elspeth/contracts/blobs.py tests/unit/web/sessions \
     tests/unit/architecture/test_session_db_mutation_authority.py \
     tests/unit/web/blobs/test_service.py \
     tests/unit/web/composer/test_blob_inline_tools.py \
     tests/unit/contracts/test_web_blob_fencing.py \
     tests/testcontainer/web/test_session_mutation_fencing_postgres.py
   git commit -m "fix(web): fence all durable session mutation paths"
   ```

**Done when:** every Sessions mutator is exhaustively classified, every
session-scoped write is fenced, every global write names its separate authority,
and the existing issue's read/write/run-admission race reproducer is green.

## Task 6: Add Membership, Run Ownership, and Exhaustive Landscape Fencing

**Issue:** remaining ownership surface of `elspeth-3d1d1fcb6c`.
`elspeth-245b21351b` is deliberately owned by Task 10's resume repair.

**Files:**

- Modify: `src/elspeth/web/coordination/repository.py`
- Create: `src/elspeth/web/coordination/lifecycle.py`
- Modify: `src/elspeth/web/sessions/service.py`
- Modify: `src/elspeth/web/execution/service.py`
- Modify: `src/elspeth/web/execution/outputs.py`
- Modify: `src/elspeth/web/blobs/service.py`
- Modify: `src/elspeth/web/composer/tools/blobs.py`
- Modify: `src/elspeth/contracts/blobs.py`
- Modify: `src/elspeth/core/landscape/run_coordination_repository.py`
- Modify: `src/elspeth/core/landscape/run_lifecycle_repository.py`
- Modify: `src/elspeth/core/landscape/scheduler_repository.py`
- Modify: `src/elspeth/core/landscape/execution_repository.py`
- Modify: `src/elspeth/core/landscape/scheduler/leases.py`
- Modify: `src/elspeth/core/landscape/scheduler/fencing.py`
- Modify: `src/elspeth/core/landscape/execution/sink_effects.py`
- Modify: `src/elspeth/core/landscape/execution/sink_effect_reservation.py`
- Modify: `src/elspeth/core/landscape/execution/sink_effect_finalization.py`
- Modify: `src/elspeth/core/checkpoint/manager.py`
- Modify: `src/elspeth/engine/orchestrator/heartbeat.py`
- Modify: `src/elspeth/engine/orchestrator/checkpointing.py`
- Modify: `src/elspeth/engine/orchestrator/leader_drain.py`
- Create: `tests/unit/web/coordination/test_run_ownership.py`
- Create: `tests/unit/architecture/test_web_landscape_mutation_fencing.py`
- Create: `tests/unit/core/landscape/test_database_clock_authority.py`
- Create: `tests/testcontainer/web/test_run_ownership_postgres.py`

1. Write RED tests for registration, heartbeat, active/draining/stopped states,
   database-clock lease expiry, `FOR UPDATE SKIP LOCKED` claim contention,
   epoch increment and full-fence CAS. Assert every sessions run projection,
   output link, composer/blob finalization, and terminal update matches the full
   `RunOwnershipFence`.
2. Create an architecture inventory of every Landscape mutation reachable from
   a web run: run lifecycle/status; graph; coordination/workers; rows/tokens/
   outcomes; node/routing state; scheduler queue/lease/barrier/disposition;
   batch/aggregation/coalesce; calls/operations; sink effects/reservations/
   finalization; checkpoint; artifact; source completion; export; terminal
   finalization. Every inventory entry must accept the current Landscape token
   and open a transaction whose first statement verifies it. Predeclare the
   sole exception implemented in Task 8B:
   `begin_run_with_baseline` may create epoch 1 only from a supplied closed
   `WebRunStartPermit` or `LocalRunStartPermit`; it validates/records the permit
   subject inside Landscape and never reads or mutates Sessions. An
   unclassified mutating method, another token creator, cross-database access,
   or unfenced web caller fails the test.
3. Use sessions-database transaction time only for sessions instance/run/
   operation authority, and Landscape transaction time only for Landscape
   leadership, worker, scheduler/effect lease, checkpoint, and stale-owner
   decisions. A test with deliberately divergent database clocks must prove no
   timestamp crosses adapters or participates in a cross-database comparison.
4. Write acquisition-failure tests for the required order. If run ownership is
   obtained but Landscape leadership fails, or leadership is obtained and a
   later validation fails, compensate/release every newly acquired authority
   or durably mark recovery required. Partial acquisition cannot leak a usable
   lease/token.
5. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/web/coordination/test_run_ownership.py \
     tests/unit/architecture/test_web_landscape_mutation_fencing.py \
     tests/unit/core/landscape/test_database_clock_authority.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer \
     tests/testcontainer/web/test_run_ownership_postgres.py
   ```

   Expected RED: membership/run authority or the exhaustive Landscape fence/
   clock inventory is absent.
6. Implement registration, renewal, drain, stop, claim, takeover, complete
   mutation-token plumbing, and partial-acquisition compensation. Require both
   sessions instance and run lease expiry before takeover.
7. Rerun both commands and `tests/unit/web/execution tests/unit/web/sessions`.
8. Commit:

   ```bash
   git add src/elspeth/web/coordination src/elspeth/web/sessions \
     src/elspeth/web/execution src/elspeth/web/blobs \
     src/elspeth/web/composer/tools/blobs.py src/elspeth/contracts/blobs.py \
     src/elspeth/core/landscape src/elspeth/core/checkpoint/manager.py \
     src/elspeth/engine/orchestrator/heartbeat.py \
     src/elspeth/engine/orchestrator/checkpointing.py \
     src/elspeth/engine/orchestrator/leader_drain.py \
     tests/unit/web/coordination tests/unit/architecture/test_web_landscape_mutation_fencing.py \
     tests/unit/core/landscape/test_database_clock_authority.py \
     tests/testcontainer/web/test_run_ownership_postgres.py
   git commit -m "feat(web): fence instance and run ownership"
   ```

**Done when:** only one claimant advances an epoch, every web Landscape writer
is structurally token-fenced, each database is its own time authority, and no
partial acquisition can leave usable authority behind.

## Task 7: Enforce Compatibility-Key Readiness and Lifecycle Drain

**Files:**

- Modify: `src/elspeth/web/config.py`
- Modify: `src/elspeth/web/deployment_contract.py`
- Modify: `src/elspeth/web/app.py`
- Modify: `src/elspeth/web/readiness.py`
- Modify: `src/elspeth/web/coordination/lifecycle.py`
- Create: `tests/unit/web/coordination/test_lifecycle.py`
- Test: `tests/unit/web/test_readiness.py`
- Create: `tests/testcontainer/web/test_compatibility_overlap_postgres.py`

1. Write RED cases for exact key
   `(SESSION_SCHEMA_EPOCH, SQLITE_SCHEMA_EPOCH,
   WEB_COORDINATION_PROTOCOL_VERSION) == (37, 29, 1)`, generation registration,
   protocol bump classification, equal-key
   overlap readiness under one stable `deployment_generation` across distinct
   revision labels/image digests, unequal-key or generation refusal before any fence, prewarm without
   stealing live work, active-to-draining shutdown, and stopped cleanup.
2. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/web/coordination/test_lifecycle.py tests/unit/web/test_readiness.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer \
     tests/testcontainer/web/test_compatibility_overlap_postgres.py
   ```

   Expected RED: readiness does not compare the complete key.
3. Implement lifecycle heartbeats and readiness. Equal-key rollout changes the
   revision label but never the storage generation; only hard cut creates a new
   generation. Shutdown must fail readiness,
   reject new claims, continue renewing owned work during bounded drain, then
   mark stopped. SQLite has no membership service but still uses the local
   `SessionOperationAuthority`; distributed selection without PostgreSQL fails
   startup.
4. Rerun both commands and `tests/unit/web/test_app.py`.
5. Commit:

   ```bash
   git add src/elspeth/web tests/unit/web/coordination \
     tests/unit/web/test_readiness.py tests/unit/web/test_app.py \
     tests/testcontainer/web/test_compatibility_overlap_postgres.py
   git commit -m "feat(web): gate transient overlap on exact compatibility"
   ```

**Done when:** same-key revisions may prewarm/overlap, incompatible revisions
are unready and obtain zero operation/run fences, and the liveness endpoint
remains shallow.

## Task 8: Persist Exact, Secret-Reference-Only Execution Inputs

**Issue:** `elspeth-f321e3ff21` (implementation identity half; Task 10 owns
recovery refusal).

**Files:**

- Create: `src/elspeth/web/execution/envelope.py`
- Modify: `src/elspeth/contracts/secrets.py`
- Modify: `src/elspeth/core/secrets.py`
- Modify: `src/elspeth/core/security/secret_loader.py`
- Modify: `src/elspeth/web/execution/protocol.py`
- Modify: `src/elspeth/web/execution/service.py`
- Modify: `src/elspeth/web/sessions/service.py`
- Modify: `src/elspeth/web/secrets/service.py`
- Modify: `src/elspeth/web/secrets/user_store.py`
- Create: `tests/unit/web/execution/test_execution_envelope.py`
- Modify: `tests/unit/core/security/test_secret_loader.py`
- Modify: `tests/unit/core/test_resolve_secret_refs.py`
- Modify: `tests/unit/contracts/test_secrets.py`
- Modify: `tests/unit/web/secrets/test_service.py`
- Modify: `tests/unit/web/secrets/test_user_store.py`

1. Write RED tests that serialize an explicit envelope schema version, graph,
   frozen non-secret settings, `PluginAvailabilitySnapshot`, canonical input
   digest, source identity/digest/completeness, topology, configuration,
   runtime, application, plugin-registry, immutable image/OCI revision,
   deployment generation, and exact implementation-identity fingerprints.
   Persist the full compatibility key with
   `WEB_COORDINATION_PROTOCOL_VERSION=1` and reject a missing/inferred protocol.
   Secret references must bind immutable `resolver_kind`, target identity, and
   resolver version. A versioned Key Vault reference binds vault/name/version;
   a user-secret reference binds durable row/version. An environment-variable
   name without a deployment-secret version remains valid for a fresh run but
   is explicitly ineligible for automatic recovery. Incomplete source posture
   is likewise valid for fresh execution and explicitly recovery-ineligible.
   Reject resolved secrets, database
   URLs, tokens, key material, and known credential-bearing field names; allow
   only resolver handles/environment names/vault key IDs.
   Key Vault tests require fresh binding to capture the provider-returned
   version, exact recovery lookup through `get_secret(name, version)`, and a
   fresh-run cache key over vault/name/version rather than name alone;
   automatic recovery bypasses/revalidates that cache against the provider.
   User-secret tests
   require a fresh lookup to return row ID/version and an atomic exact resolver
   that returns a value only while both still match; a later upsert/version
   advance makes the old reference fail without reading the new value.
   Define a persistence-safe `BoundSecretRef` carrier and version-aware
   `WebSecretResolver.bind(...)` / `resolve_exact(...)` contract. The web
   service and core tree-walk must preserve resolver kind, redacted target
   identity, and version alongside the ephemeral value all the way to
   `ExecutionService`; they may not reconstruct a name/fingerprint-only
   `ResolvedSecret`, call a private store directly, or fall back to `resolve`
   by name during recovery.
2. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/web/execution/test_execution_envelope.py \
     tests/unit/core/security/test_secret_loader.py \
     tests/unit/core/test_resolve_secret_refs.py \
     tests/unit/contracts/test_secrets.py \
     tests/unit/web/secrets/test_service.py \
     tests/unit/web/secrets/test_user_store.py
   ```

   Expected RED: no exact durable execution envelope exists.
3. Implement version-aware resolver boundaries before persisting envelopes.
   A Key Vault fresh resolve returns the ephemeral value plus a normalized
   vault/name/provider-version reference; later automatic recovery can request
   only that exact version and must bypass/revalidate any fresh-run value cache.
   Cache entries use the full immutable reference.
   User-secret fresh resolution returns the ephemeral value plus row ID and
   monotonic version; recovery uses one atomic row-ID/version predicate. The
   value never enters the envelope, cache key, audit, logs, or diagnostics.
   Thread the redacted `BoundSecretRef` through `ResolvedSecret`,
   `WebSecretService`, `resolve_secret_refs`, and `ExecutionService` without
   dropping metadata. Repr/str and every diagnostic omit target/version where
   it can reveal tenancy; serialization persists only the explicitly approved
   reference fields, never the value.
4. Implement canonical serialization and hashing. Keep the private envelope out
   of APIs, audit export, diagnostics, logs, and receipts. Diagnostic rendering
   redacts resolver targets/tenancy as well as values.
5. Rerun the command plus execution validation/settings regression tests.
6. Commit:

   ```bash
   git add src/elspeth/contracts/secrets.py src/elspeth/core/secrets.py \
     src/elspeth/core/security/secret_loader.py src/elspeth/web/execution \
     src/elspeth/web/sessions src/elspeth/web/secrets/service.py \
     src/elspeth/web/secrets/user_store.py \
     tests/unit/web/execution/test_execution_envelope.py \
     tests/unit/core/security/test_secret_loader.py \
     tests/unit/core/test_resolve_secret_refs.py tests/unit/contracts/test_secrets.py \
     tests/unit/web/secrets/test_service.py \
     tests/unit/web/secrets/test_user_store.py
   git commit -m "feat(web): bind durable runs to exact execution inputs"
   ```

**Done when:** no resolved secret crosses persistence, every resolver target is
immutably version-bound or recovery-ineligible, and the implementation image/
OCI/generation fingerprint is durable.

## Task 8A: Issue and Rehydrate Web Run-Start Permits in Sessions

**Files:**

- Create: `src/elspeth/contracts/run_start.py`
- Modify: `src/elspeth/web/execution/protocol.py`
- Modify: `src/elspeth/web/execution/service.py`
- Modify: `src/elspeth/web/sessions/protocol.py`
- Modify: `src/elspeth/web/sessions/service.py`
- Create: `tests/unit/contracts/test_run_start_permits.py`
- Create: `tests/unit/web/execution/test_start_permit_issuance.py`

1. Write RED tests for the closed durable
   `RunStartPermit = WebRunStartPermit | LocalRunStartPermit` contract and the
   Sessions-only web issuance API. `start_intent` atomically stores the stable
   run ID, immutable envelope, run fence, and `pending` start-vs-cancel state.
   Under the current operation and run fences, one compare-and-swap either
   performs `pending -> start_permitted` and persists one exact
   `WebRunStartPermit`, or `pending -> cancelled_before_permit` and persists no
   permit. Test exact retry, conflicting retry, stale-fence refusal, crash-safe
   rehydration, and mutual exclusion of the two terminal CAS outcomes.
2. Require web permits to contain stable permit/run IDs, monotonic permit epoch,
   non-secret fence identities/epochs, immutable envelope/topology/source/
   checkpoint subject hashes, generation/key, and a canonical subject hash.
   Local permits explicitly contain a single local owner and no Sessions
   authority. No web path may construct either variant outside the contracts
   and Sessions issuance API.
3. Run RED/GREEN:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/contracts/test_run_start_permits.py \
     tests/unit/web/execution/test_start_permit_issuance.py
   ```

   Expected RED: no durable Sessions permit issuance/rehydration API exists.
4. Implement and commit the unused-until-Task-8B Sessions primitive without
   switching any web run to the new Landscape start seam yet. This keeps the
   intermediate commit green while making it impossible for Task 8B to
   fabricate a permit or misuse `LocalRunStartPermit`.
5. Commit:

   ```bash
   git add src/elspeth/contracts/run_start.py src/elspeth/web/execution \
     src/elspeth/web/sessions tests/unit/contracts/test_run_start_permits.py \
     tests/unit/web/execution/test_start_permit_issuance.py
   git commit -m "feat(web): persist fenced run-start permits"
   ```

**Done when:** Sessions is the sole issuer of a durable web permit, cancel-
before-permit issues none, and exact permit state rehydrates before any caller
is switched to the Landscape atomic-baseline API.

## Task 8B: Replace Every Fresh-Run Path with an Atomic Audit Baseline

**Files:**

- Modify: `src/elspeth/contracts/run_start.py`
- Modify: `src/elspeth/core/landscape/run_lifecycle_repository.py`
- Modify: `src/elspeth/core/landscape/factory.py`
- Modify: `src/elspeth/core/checkpoint/manager.py`
- Modify: `src/elspeth/engine/orchestrator/run_lifecycle.py`
- Modify: `src/elspeth/engine/orchestrator/checkpointing.py`
- Modify: `src/elspeth/engine/orchestrator/leader_drain.py`
- Modify: every CLI/web/test fixture/direct caller found by the architecture
  inventory before implementation
- Create: `tests/unit/architecture/test_atomic_run_baseline_inventory.py`
- Modify: `tests/unit/contracts/test_run_start_permits.py`
- Modify: `tests/unit/core/landscape/test_run_lifecycle_repository.py`
- Modify: `tests/unit/core/landscape/test_factory.py`
- Modify: `tests/unit/core/checkpoint/test_recovery.py`
- Modify: `tests/integration/checkpoint/test_recovery.py`
- Modify: `tests/integration/pipeline/orchestrator/test_graceful_shutdown.py`
- Modify: `tests/integration/pipeline/test_resume_comprehensive.py`
- Create: `tests/testcontainer/web/test_run_start_baseline_postgres.py`

1. Write RED tests for `begin_run_with_baseline`: rollback leaves no Landscape
   run; commit atomically creates deterministic run UUID, epoch-1 coordination
   token, and sequence-0 audit baseline; exact retry returns the same bundle;
   mismatched fingerprint refuses. Use the Task 8A closed durable
   `RunStartPermit = WebRunStartPermit | LocalRunStartPermit` contract. Web
   permits contain stable permit/run IDs, monotonic permit epoch, non-secret
   fence identities/epochs, immutable subject hashes, generation/key, and
   canonical subject hash. Local permits explicitly name a single local owner
   and contain no Sessions authority. It is the sole Landscape first-statement
   token-creation exception and accepts only one of those serialized variants.
   No other method may mint epoch 1. With periodic checkpoints disabled,
   sequence 0 still records topology/envelope/source posture and
   `automatic_recovery_eligible: false`, plugin execution remains valid, and no
   later checkpoint is written.
2. Write the architecture inventory before refactoring. It must fail while any
   fresh CLI, web, fixture, `RecorderFactory.run_lifecycle`, or direct
   repository path calls old `begin_run`, while
   `CheckpointCoordinator.checkpoint_run_start` exists, or while another path
   can write sequence 0 after creation.
3. Run RED:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/architecture/test_atomic_run_baseline_inventory.py \
     tests/unit/contracts/test_run_start_permits.py \
     tests/unit/core/landscape/test_run_lifecycle_repository.py \
     tests/unit/core/landscape/test_factory.py \
     tests/unit/core/checkpoint/test_recovery.py \
     tests/integration/checkpoint/test_recovery.py \
     tests/integration/pipeline/orchestrator/test_graceful_shutdown.py \
     tests/integration/pipeline/test_resume_comprehensive.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer \
     tests/testcontainer/web/test_run_start_baseline_postgres.py
   ```

4. Replace `begin_run` with one epoch-29 `begin_run_with_baseline`
   transaction and remove the late sequence-0 seam. It validates and records
   the supplied permit variant/ID/run/canonical subject entirely inside its
   Landscape transaction; it never queries, locks, updates, or calls Sessions.
   The same exact permit and baseline inputs return the existing bundle;
   changed/reused permit subjects fail closed. All other Landscape mutations
   retain first-statement token verification. Update
   `RunLifecycleCoordinator`, checkpointing, leader drain, CLI, web, every
   fixture, and every direct caller. A web start obtains or rehydrates only the
   Task 8A Sessions-issued `WebRunStartPermit`; it cannot construct one locally
   or substitute `LocalRunStartPermit`. CLI/direct callers use only the local
   variant. Do not add a Landscape schema bump.
5. Prove absence and GREEN:

   ```bash
   if rg -n '\.begin_run\(' src tests \
     --glob '!tests/unit/architecture/test_atomic_run_baseline_inventory.py'; then exit 1; fi
   if rg -n 'checkpoint_run_start' src/elspeth; then exit 1; fi
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/architecture/test_atomic_run_baseline_inventory.py \
     tests/unit/contracts/test_run_start_permits.py \
     tests/unit/core/landscape/test_run_lifecycle_repository.py \
     tests/unit/core/landscape/test_factory.py \
     tests/unit/core/checkpoint/test_recovery.py \
     tests/integration/checkpoint/test_recovery.py \
     tests/integration/pipeline/orchestrator/test_graceful_shutdown.py \
     tests/integration/pipeline/test_resume_comprehensive.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer \
     tests/testcontainer/web/test_run_start_baseline_postgres.py
   ```

6. Commit all mechanically updated direct callers in this one refactor commit:

   ```bash
   git add src/elspeth/core/landscape/run_lifecycle_repository.py \
     src/elspeth/contracts/run_start.py \
     src/elspeth/core/landscape/factory.py src/elspeth/core/checkpoint/manager.py \
     src/elspeth/engine/orchestrator/run_lifecycle.py \
     src/elspeth/engine/orchestrator/checkpointing.py \
     src/elspeth/engine/orchestrator/leader_drain.py src/elspeth/cli.py \
     src/elspeth/web tests/unit/contracts/test_run_start_permits.py \
     tests/conftest.py tests/e2e tests/fixtures tests/performance tests/property \
     tests/unit tests/integration tests/testcontainer
   git commit -m "refactor(engine): make every fresh run baseline atomic"
   ```

**Done when:** no old fresh-run or late-sequence-zero seam exists, no other
Landscape path can create epoch 1, and checkpoint-disabled/direct-fixture paths
obey a typed permit baseline without any Landscape-to-Sessions access.

## Task 9: Implement the Restartable Cross-Database Run-Start Saga

**Files:**

- Create: `src/elspeth/web/execution/saga.py`
- Modify: `src/elspeth/web/execution/service.py`
- Modify: `src/elspeth/web/sessions/service.py`
- Modify: `src/elspeth/web/sessions/protocol.py`
- Create: `tests/unit/web/execution/test_run_start_saga.py`
- Create: `tests/testcontainer/web/test_run_start_saga_postgres.py`

1. Starting from Task 8A's already-green Sessions issuance primitive and Task
   8B's already-green Landscape baseline consumer, write RED crash-injection
   tests around every cross-database transition:

   ```text
   draft -> start_intent -> start_permit_issued -> baseline_checkpointed -> running -> terminal
   start_permit_issued|baseline_checkpointed|running -> recovery_required
   start_intent -> terminal_cancelled
   start_permit_issued|baseline_checkpointed|running -> cancel_pending -> terminal_cancelled
   ```

   Sessions `start_intent` must atomically commit the envelope/run fence and
   start-vs-cancel state. The Sessions CAS `pending -> start_permitted` issues
   and persists a `WebRunStartPermit` only under the current operation/run
   fences; `pending -> cancelled_before_permit` issues none. Permit state and
   immutable subjects must rehydrate exactly. Those Sessions facts are saga-
   joined to Landscape, never cross-database atomic. Cover the explicit crash
   after permit issuance but before any Landscape row, before/after the
   Landscape commit, and before/after each
   sessions CAS. A Landscape bundle that exists but is not yet bound must be
   exact-verified and bound to the same UUID; it must never create a duplicate.
   Include incomplete-source fresh runs: execution is valid, the baseline
   records the honest incomplete posture, and automatic recovery is ineligible.
2. Add a deterministic cancellation-vs-start CAS race. Cancel-before-permit
   winner has no permit and no Landscape run. Start-permit winner means eventual
   baseline, not immediate baseline: if cancellation arrives before Landscape,
   the reconciler rehydrates the permit, materializes the atomic baseline solely
   to fenced terminal-cancel, and makes zero plugin calls. Exact Sessions and
   Landscape retries observe one winner and one permit/bundle.
3. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 tests/unit/web/execution/test_run_start_saga.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer \
     tests/testcontainer/web/test_run_start_saga_postgres.py
   ```

   Expected RED: the current optimistic two-database start has an unhandled
   crash window.
4. Implement monotonic idempotent transitions under both fences. Prepared
   continuation is allowed only before durable plugin/source/sink effects and
   only when every envelope/baseline fact matches.
5. Rerun both commands twice to expose duplicate/retry instability.
6. Commit:

   ```bash
   git add src/elspeth/web/execution src/elspeth/web/sessions \
     tests/unit/web/execution/test_run_start_saga.py \
     tests/testcontainer/web/test_run_start_saga_postgres.py
   git commit -m "feat(web): reconcile durable cross-database run starts"
   ```

**Done when:** every crash boundary advances the same identity exactly once or
persists bounded `recovery_required`, a start-won permit always converges to one
baseline even when later cancelled, cancel-before-permit creates none, and
incomplete-source fresh execution remains valid but recovery-ineligible.

## Task 9A: Require Execution Authority and Synchronous Audit at Every Boundary

**Files:**

- Modify: `src/elspeth/engine/orchestrator/ports.py`
- Create: `src/elspeth/engine/orchestrator/execution_admission.py`
- Modify: `src/elspeth/engine/orchestrator/source_iteration.py`
- Modify: `src/elspeth/engine/orchestrator/run_context_factory.py`
- Modify: `src/elspeth/engine/processor.py`
- Modify: `src/elspeth/engine/retry.py`
- Modify: `src/elspeth/engine/batch_adapter.py`
- Modify: `src/elspeth/engine/executors/transform.py`
- Modify: `src/elspeth/engine/executors/aggregation.py`
- Modify: `src/elspeth/engine/executors/sink.py`
- Modify: `src/elspeth/engine/executors/sink_effects.py`
- Modify: `src/elspeth/engine/orchestrator/aggregation.py`
- Modify: `src/elspeth/engine/orchestrator/sink_flush.py`
- Modify: `src/elspeth/engine/orchestrator/export.py`
- Modify: `src/elspeth/engine/orchestrator/audit_export_effects.py`
- Modify: `src/elspeth/engine/orchestrator/run_lifecycle.py`
- Modify: `src/elspeth/engine/orchestrator/cleanup.py`
- Modify: `src/elspeth/engine/triggers.py`
- Create: `tests/unit/architecture/test_execution_authority_inventory.py`
- Create: `tests/unit/engine/test_execution_authority_check.py`
- Create: `tests/integration/pipeline/orchestrator/test_execution_authority_loss.py`

1. Define an injected `ExecutionAuthorityCheck` protocol/callback. For web runs
   it independently verifies sessions `RunOwnershipFence` with sessions time
   and Landscape token/minimum margin with Landscape time; it never compares
   absolute timestamps. CLI single-owner execution supplies the explicit local
   implementation, not `None` or a bypass.
2. Define a synchronous `ExecutionAuditAdmission` alongside authority. A closed
   invocation-admitted fact must commit under current authority immediately
   before each plugin/effect call; audit failure prevents invocation. After
   return or raise, recheck authority and synchronously commit the closed post-
   call/result-admission fact before any result, failure, retry decision, audit-
   export outcome, trigger outcome, or effect disposition can commit. Derived
   telemetry occurs only after the audit and associated durable-state commit.
3. Write a complete AST inventory, not a hand-picked callback list. It must
   enumerate `run_context_factory.py`, `executors/sink_effects.py`,
   `orchestrator/audit_export_effects.py`, `triggers.py`, and all existing
   source/processor/retry/batch/transform/aggregation/sink/export/lifecycle/
   cleanup owners. Require a check immediately before every
   source `on_start`/`load`/iterator advance, transform/process, retry attempt,
   batch submission, aggregation lifecycle/process, sink/effect publication,
   export, run lifecycle, `on_complete`, `close`, cleanup callback, and other
   plugin/effect call, with the synchronous admission-audit commit between the
   authority check and invocation. Require the second authority check and post-
   call audit before returned-state commit, followed by telemetry. Any
   unclassified call expression, audit gap, result commit, or early telemetry
   fails architecture tests.
4. Write failure-injection tests for authority or audit loss before call,
   during a long call, after return/before commit, and before lifecycle close.
   Before-call audit failure invokes nothing; post-return audit failure commits
   no result/disposition and emits no success telemetry. Framework-owned local
   resource cleanup may still run.
5. Run RED/GREEN:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/architecture/test_execution_authority_inventory.py \
     tests/unit/engine/test_execution_authority_check.py \
     tests/integration/pipeline/orchestrator/test_execution_authority_loss.py
   ```

6. Implement the minimal protocol plumbing and rerun neighboring processor,
   executor, retry, source-iteration, lifecycle, sink-effect, and export tests.
7. Commit:

   ```bash
   git add src/elspeth/engine tests/unit/architecture/test_execution_authority_inventory.py \
     tests/unit/engine/test_execution_authority_check.py \
     tests/integration/pipeline/orchestrator/test_execution_authority_loss.py
   git commit -m "feat(engine): audit-admit every plugin execution boundary"
   ```

**Done when:** structural and behavioral tests prove no source/processor/
executor/effect/lifecycle call or returned-result commit can bypass authority
plus synchronous audit, and telemetry cannot precede authoritative state.

## Task 10: Add Fail-Closed Automatic Recovery and Cancel-Only Reconciliation

**Issues:** `elspeth-245b21351b`, `elspeth-f321e3ff21`.

**Files:**

- Create: `src/elspeth/web/execution/recovery_admission.py`
- Create: `src/elspeth/web/execution/reconciler.py`
- Modify: `src/elspeth/contracts/secrets.py`
- Modify: `src/elspeth/core/secrets.py`
- Modify: `src/elspeth/core/security/secret_loader.py`
- Modify: `src/elspeth/web/execution/service.py`
- Modify: `src/elspeth/web/execution/routes.py`
- Modify: `src/elspeth/web/secrets/service.py`
- Modify: `src/elspeth/web/secrets/user_store.py`
- Modify: `src/elspeth/engine/orchestrator/resume.py`
- Create: `tests/unit/web/execution/test_recovery_admission.py`
- Create: `tests/unit/web/execution/test_cancel_reconciliation.py`
- Modify: `tests/unit/core/security/test_secret_loader.py`
- Modify: `tests/unit/core/test_resolve_secret_refs.py`
- Modify: `tests/unit/contracts/test_secrets.py`
- Modify: `tests/unit/web/secrets/test_service.py`
- Modify: `tests/unit/web/secrets/test_user_store.py`
- Test: `tests/integration/pipeline/orchestrator/test_resume_guardrails.py`
- Create: `tests/testcontainer/web/test_recovery_reconciliation_postgres.py`

1. Write RED admission cases for exact deployment generation, immutable image
   digest/OCI revision, implementation/application/plugin/graph/config/runtime
   fingerprints, immutable resolver kind/target/version, valid sequence-0/latest checkpoint,
   complete immutable sources, topology/VAL/schema/profile match, explicit
   source and sink `RESUME` admission, sink tri-state, and effect policy.
   An unversioned environment reference, generation or revision drift, and any
   missing/unknown/drifted fact, incomplete
   sources, IO writes, state-changing external calls without exact idempotency,
   or sink `UNKNOWN` must persist a closed reason and make zero plugin calls.
   For Key Vault, admission validates the exact vault/name/version reference
   and post-admission resolution requests that version, never latest; for a
   user secret, admission and atomic resolution require the same row ID and
   monotonic version. Test the precise continuity policy: a newer Key Vault
   latest version does not invalidate the still-enabled/readable bound version;
   recovery fetches that exact older version without fallback. Disable/delete
   the bound Key Vault version between admission and resolution and prove the
   uncached provider revalidation refuses. Advance the user-secret row version
   and prove its exact predicate refuses. Every refusal makes zero plugin calls
   and performs no fallback/latest or name-only read.
2. Add the exact `elspeth-245b21351b` regression in
   `src/elspeth/engine/orchestrator/resume.py`: after a successful leadership
   CAS, any restore/admission failure must release/compensate that new token or
   terminalize it as recovery-required. It cannot return while retaining live
   leadership. Also test partial sessions/Landscape acquisition loss.
3. Write RED cancellation cases: before Landscape creation, after baseline,
   peer cancellation, cancel plus owner death, completion/cancel race, and
   cancel-only successor. Reuse Task 9's start-intent CAS: cancel-before-permit
   creates no permit/Landscape run; start-permit winner with no Landscape row
   must rehydrate the durable permit, materialize sequence 0 solely to terminal-
   cancel, and invoke zero plugins. The reconciler must not call resume to
   cancel.
4. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/web/execution/test_recovery_admission.py \
     tests/unit/web/execution/test_cancel_reconciliation.py \
     tests/unit/core/security/test_secret_loader.py \
     tests/unit/core/test_resolve_secret_refs.py \
     tests/unit/contracts/test_secrets.py \
     tests/unit/web/secrets/test_service.py \
     tests/unit/web/secrets/test_user_store.py \
     tests/integration/pipeline/orchestrator/test_resume_guardrails.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer \
     tests/testcontainer/web/test_recovery_reconciliation_postgres.py
   ```

   Expected RED: automatic claim lacks the full eligibility/cancel ordering.
5. Implement metadata-only admission before plugin instantiation. Preserve
   `RecoveryManager` refusal for missing/empty checkpoints and incomplete
   sources, while preserving Task 9's rule that incomplete sources are legal
   for fresh execution. Resolve secret references only after admission and
   immediately before the owning call, through the Task 8 exact-version Key
   Vault or user-row/version API through the public version-aware resolver
   carrier—never a private store bypass. An unavailable/disabled/deleted bound
   Key Vault version or mismatched user row/version is a durable refusal; a
   newer still-separate Key Vault version is not. Never retry latest or resolve
   by name only. Recheck both leases before each plugin boundary.
6. Implement cancellation ordering: persist request; owner drains with fences;
   checkpoint/interrupt; release Landscape; finalize blobs and sessions exactly
   once; release sessions authority last. A cancel-only successor performs zero
   replay.
7. Rerun the commands and existing recovery suites, including the exact
   post-CAS restore failure and changed-image/generation tests.
8. Commit:

   ```bash
   git add src/elspeth/contracts/secrets.py src/elspeth/core/secrets.py \
     src/elspeth/core/security/secret_loader.py src/elspeth/web/execution \
     src/elspeth/web/secrets/service.py src/elspeth/web/secrets/user_store.py \
     src/elspeth/engine/orchestrator/resume.py \
     tests/unit/web/execution tests/integration/pipeline/orchestrator/test_resume_guardrails.py \
     tests/unit/core/security/test_secret_loader.py tests/unit/core/test_resolve_secret_refs.py \
     tests/unit/contracts/test_secrets.py tests/unit/web/secrets/test_service.py \
     tests/unit/web/secrets/test_user_store.py \
     tests/testcontainer/web/test_recovery_reconciliation_postgres.py
   git commit -m "feat(web): fail closed on unsafe recovery and cancellation"
   ```

**Done when:** recovery is an eligibility-gated optimization, not generic HA,
and every ineligible/cancelled path performs zero plugin calls.

## Task 11: Make Tickets, Composer Progress, and Rate Limits Cross-Replica Safe

**Files:**

- Modify: `src/elspeth/web/execution/websocket_ticket.py`
- Modify: `src/elspeth/web/execution/progress.py`
- Modify: `src/elspeth/web/composer/progress.py`
- Modify: `src/elspeth/web/middleware/rate_limit.py`
- Modify: `src/elspeth/web/sessions/service.py`
- Create: `tests/unit/web/execution/test_websocket_ticket.py`
- Test: `tests/unit/web/composer/test_progress.py`
- Test: `tests/unit/web/middleware/test_rate_limit.py`
- Create: `tests/testcontainer/web/test_cross_replica_signals_postgres.py`

1. Write RED cases for one-time SHA-256 ticket consumption by a peer,
   one-latest-snapshot composer progress per session, fenced inflight updates,
   durable WebSocket replay, and atomic HMAC-subject rate-window pruning/insert.
   SQLite retains current in-memory behavior.
2. Run unit tests separately from the explicit container command:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/web/execution/test_websocket_ticket.py \
     tests/unit/web/composer/test_progress.py \
     tests/unit/web/middleware/test_rate_limit.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer \
     tests/testcontainer/web/test_cross_replica_signals_postgres.py
   ```

   Expected RED: process-local state prevents peer handoff.
3. Implement PostgreSQL repositories using database time and exact operation
   fences. Store only ticket/subject digests, never preimages.
4. Rerun both commands twice.
5. Commit:

   ```bash
   git add src/elspeth/web/execution src/elspeth/web/composer \
     src/elspeth/web/middleware src/elspeth/web/sessions \
     tests/unit/web/execution/test_websocket_ticket.py \
     tests/unit/web/composer/test_progress.py \
     tests/unit/web/middleware/test_rate_limit.py \
     tests/testcontainer/web/test_cross_replica_signals_postgres.py
   git commit -m "feat(web): persist cross-replica web coordination signals"
   ```

**Done when:** peer ticket/progress/cancellation handoff works without shared
memory and rate limiting is atomic across replicas.

## Task 12: Make Authoritative Transitions Audit-First and Bound Sessions Cleanup

**Files:**

- Create: `src/elspeth/web/coordination/cleanup.py`
- Create: `src/elspeth/web/coordination/audit.py`
- Modify: `src/elspeth/web/coordination/lifecycle.py`
- Modify: `src/elspeth/web/execution/saga.py`
- Modify: `src/elspeth/web/execution/reconciler.py`
- Modify: `src/elspeth/web/execution/recovery_admission.py`
- Modify: `src/elspeth/web/execution/service.py`
- Modify: `src/elspeth/web/sessions/service.py`
- Modify: the Task 9A engine authority-owner inventory
- Create: `tests/unit/web/coordination/test_cleanup.py`
- Create: `tests/unit/web/coordination/test_audit_primacy.py`
- Create: `tests/unit/architecture/test_coordination_audit_ownership.py`
- Create: `tests/testcontainer/web/test_coordination_retention_postgres.py`

1. Write RED cleanup tests for expired/consumed tickets, expired/completed
   inflight rows, old rate events, empty stale buckets, stopped/expired
   unreferenced instances, and terminal transient saga-control rows. Seed fresh
   rows and permanent execution envelopes, cancellation requests, non-terminal
   saga/run state, run/audit facts, and sequence-0/later checkpoints that must
   remain. This cleaner is Sessions-database-only: its global claim and row
   `SKIP LOCKED` claims live in Sessions, it introduces no Landscape schema or
   epoch change, and existing Landscape retention is out of scope. Prove a
   retained session fence is never independently deleted; only current-fence
   physical parent deletion may cascade it.
2. Write RED boundedness cases: configurable interval/batch/retention, indexed
   database-time selection, **both** a global random-token/monotonic-epoch
   cleanup claim and row-level `FOR UPDATE SKIP LOCKED`, short statement
   timeout, maximum renewals/batches, concurrent cleaners, repeated
   convergence, unique subjects, and HMAC key rotation. Claim loss, expiry,
   epoch change, or zero-row renewal must raise `CleanupClaimLost` before any
   delete; a SIGSTOP/successor/SIGCONT stale cleaner deletes nothing.
3. Write RED audit-primacy cases and an exhaustive ownership gate. Explicit
   Sessions owners are saga, reconciler, recovery admission, web execution
   service, sessions service/routes, coordination lifecycle, and cleanup.
   Explicit engine/Landscape owners are every Task 9A authority owner,
   including run-context factory, sink effects, audit-export effects, and
   triggers, plus coordination repositories. Saga, fence, takeover, recovery,
   cancellation, cleanup, execution-authority, and plugin/effect admission or
   refusal facts commit synchronously in the authoritative database before any
   derived signal. At every Task 9A call site require exact order: authority
   check -> synchronous invocation-admitted audit -> invocation -> post-call
   authority check -> synchronous result-admission audit -> result/disposition
   commit -> telemetry. Pre-call audit failure prevents invocation; post-call
   audit failure prevents result/disposition and success telemetry. A raised
   call is audited before retry/terminal handling. An unowned transition or
   ordering gap fails the architecture test.
4. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/web/coordination/test_cleanup.py \
     tests/unit/web/coordination/test_audit_primacy.py \
     tests/unit/architecture/test_coordination_audit_ownership.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer \
     tests/testcontainer/web/test_coordination_retention_postgres.py
   ```

   Expected RED: authoritative transitions are not exhaustively audit-owned and
   no bounded Sessions cleanup claim exists.
5. Implement audit-first transitions and the resilient doubly-fenced Sessions
   cleaner. One batch failure emits only bounded fallback health evidence and
   does not kill future iterations. Do not recompute digest preimages or add a
   Landscape cleanup table.
6. Rerun both commands and all saga/recovery/execution-authority regressions.
7. Commit:

   ```bash
   git add src/elspeth/web/coordination src/elspeth/web/execution \
     src/elspeth/web/sessions src/elspeth/engine \
     tests/unit/web/coordination \
     tests/unit/architecture/test_coordination_audit_ownership.py \
     tests/testcontainer/web/test_coordination_retention_postgres.py
   git commit -m "feat(web): audit authority and bound sessions retention"
   ```

**Done when:** audit is mandatory before invocation and result admission,
telemetry follows authoritative state, stale Sessions cleanup authority cannot
delete, cleanup converges without deleting durable facts, and Landscape
schema/retention remains unchanged.

## Task 12B: Add Closed Telemetry and Resilient Exporters

**Files:**

- Create: `src/elspeth/web/coordination/telemetry.py`
- Modify: `src/elspeth/web/config.py`
- Modify: `src/elspeth/web/operator_telemetry.py`
- Modify: `src/elspeth/web/app.py`
- Modify: `src/elspeth/core/landscape/exporter.py`
- Modify: `src/elspeth/engine/orchestrator/audit_export_effects.py`
- Create: `tests/unit/web/coordination/test_telemetry.py`
- Create: `tests/unit/web/coordination/test_exporter_resilience.py`

1. Starting from Task 12's mandatory audit invariant, write RED telemetry and
   redaction cases for closed low-cardinality labels on registration,
   heartbeat, loop failure, drain, takeover, fence loss, saga, recovery,
   cancellation, cleanup, and exporter health. Reject run/session/instance/
   user/client/ticket/subject/URL/content identifiers, secret references or
   values, digests, and raw exception text.
2. Prove exporter failure cannot roll back, replace, or suppress a committed
   audit fact and emits at most one bounded infrastructure-health log. No
   success metric is emitted before the audit commit. Audit failure remains
   fail-closed from Task 12.
3. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/web/coordination/test_telemetry.py \
     tests/unit/web/coordination/test_exporter_resilience.py \
     tests/unit/web/test_prometheus_extras.py
   ```

   Expected RED: the closed exporter/telemetry delta is absent.
4. Implement the compatible runtime delta without changing the compatibility
   key (`WEB_COORDINATION_PROTOCOL_VERSION` remains 1), authority decisions,
   or Task 12 audit ordering. Rerun the command and telemetry exporter suites.
5. Commit:

   ```bash
   git add src/elspeth/web/coordination/telemetry.py src/elspeth/web/config.py \
     src/elspeth/web/operator_telemetry.py src/elspeth/web/app.py \
     src/elspeth/core/landscape/exporter.py \
     src/elspeth/engine/orchestrator/audit_export_effects.py \
     tests/unit/web/coordination/test_telemetry.py \
     tests/unit/web/coordination/test_exporter_resilience.py
   git commit -m "feat(web): export closed coordination telemetry"
   ```

**Done when:** telemetry is derived from already-committed audit facts, exporter
failure is bounded, and this commit supplies a real compatible N runtime delta.

## Task 12C: Register Heavy-Test Markers Before Committing Heavy Tests

**Files (exclusive ownership):**

- Modify: `pyproject.toml`
- Modify: `tests/unit/test_ci_workflow_xdist.py`

1. Write a RED contract requiring registered `multi_instance` and
   `kubernetes_kind` markers and requiring the ordinary container selection to
   exclude both. Unknown marker warnings and a zero-collected dedicated
   selection are failures, not skips.
2. Register both markers before Task 13 or Task 15 creates a marked file. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 tests/unit/test_ci_workflow_xdist.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 \
     -m "testcontainer and multi_instance" --collect-only tests/testcontainer; test "$?" -eq 5
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 \
     -m "testcontainer and kubernetes_kind" --collect-only tests/testcontainer; test "$?" -eq 5
   ```

   The pre-file exit-5 assertion is temporary evidence that registration is
   recognized while no matching test exists. Task 20's hosted jobs must instead
   fail on zero tests once the files exist.
3. Commit:

   ```bash
   git add pyproject.toml tests/unit/test_ci_workflow_xdist.py
   git commit -m "test(ci): register deployment container markers"
   ```

**Done when:** strict collection recognizes both markers before either heavy
suite enters history.

## Task 13: Prove the Full Two-Process Failure, Cancellation, and Race Matrix

**Files:**

- Create: `tests/testcontainer/web/multiprocess/__init__.py`
- Create: `tests/testcontainer/web/multiprocess/harness.py`
- Create: `tests/testcontainer/web/test_multi_instance_overlap.py`
- Create: `tests/testcontainer/web/test_multi_instance_saga.py`
- Create: `tests/testcontainer/web/test_multi_instance_recovery.py`
- Create: `tests/testcontainer/web/test_multi_instance_cancellation.py`
- Create: `tests/testcontainer/web/test_multi_instance_retention.py`

1. Mark every file with both already-registered `testcontainer` and
   `multi_instance`. Strict collection and dedicated commands must collect a
   nonzero scenario count.
2. Add independent-process tests for:

   - distinct-revision equal-key N/N-1 overlap, traffic/drain/rollback, and
     incompatible-key refusal with one stable `deployment_generation`;
   - dual-fence single winner/loss and two concurrent claimants;
   - saga crash before/after web permit issuance, before/after Landscape
     baseline, every Sessions transition, exact permit rehydration/idempotency,
     and no Landscape-to-Sessions access;
   - `SIGKILL` after a complete source/checkpoint, with only the replacement
     recovering (the killed process is never expected to resume);
   - `SIGSTOP`, lease expiry, takeover, then `SIGCONT`, with stale refusal
     before a durable write;
   - missing input/checkpoint, incomplete source, changed implementation,
     effect-unsafe transform, and sink tri-state refusal with zero plugin calls;
   - cancel-before-permit, cancel after permit/before Landscape (eventual
     baseline solely to terminal-cancel), peer cancellation, cancel plus owner
     kill, cancel/completion race, and cancel-only successor;
   - long effectful call returning after lease loss, whose result cannot commit;
   - ticket/progress handoff and retention convergence; and
   - graceful `SIGTERM` drain.
3. Avoid a commit cycle: after both runtime commits exist, build N-1 from the
   Task 12 mandatory-audit/Sessions-cleanup commit and N from the Task 12B
   closed telemetry/exporter commit. Thus both sides include the audit
   invariant while N has a real compatible runtime delta. Both declare exact
   key `(37, 29, 1)` and the same storage generation. Require different
   immutable image digests and exact OCI revision labels equal to their source
   commits; inspect and assert those labels before starting either process. Use
   those distinct images only for rollout/prewarm/drain/
   equal-key rollback. Positive automatic takeover uses two processes from the
   **same** N image/fingerprint; the distinct-revision case must instead prove
   changed-fingerprint recovery refusal with zero plugin calls.
4. Run every file separately so ownership is exact, then the combined selection
   twice:

   ```bash
   for test_file in \
     tests/testcontainer/web/test_multi_instance_overlap.py \
     tests/testcontainer/web/test_multi_instance_saga.py \
     tests/testcontainer/web/test_multi_instance_recovery.py \
     tests/testcontainer/web/test_multi_instance_cancellation.py \
     tests/testcontainer/web/test_multi_instance_retention.py; do
     env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 \
       -m "testcontainer and multi_instance" "$test_file"
   done
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m "testcontainer and multi_instance" \
     tests/testcontainer/web/test_multi_instance_overlap.py \
     tests/testcontainer/web/test_multi_instance_saga.py \
     tests/testcontainer/web/test_multi_instance_recovery.py \
     tests/testcontainer/web/test_multi_instance_cancellation.py \
     tests/testcontainer/web/test_multi_instance_retention.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m "testcontainer and multi_instance" \
     tests/testcontainer/web/test_multi_instance_overlap.py \
     tests/testcontainer/web/test_multi_instance_saga.py \
     tests/testcontainer/web/test_multi_instance_recovery.py \
     tests/testcontainer/web/test_multi_instance_cancellation.py \
     tests/testcontainer/web/test_multi_instance_retention.py
   ```

   Before the tests, assert for both images:

   ```bash
   test "$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$N_MINUS_1_IMAGE")" = "$TASK_12_SHA"
   test "$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$N_IMAGE")" = "$TASK_12B_SHA"
   test "$(docker inspect --format '{{index .Config.Labels "io.elspeth.install-extras"}}' "$N_MINUS_1_IMAGE")" = all
   test "$(docker inspect --format '{{index .Config.Labels "io.elspeth.install-extras"}}' "$N_IMAGE")" = all
   test "$(docker inspect --format '{{index .Config.Labels "io.elspeth.web-coordination-protocol"}}' "$N_MINUS_1_IMAGE")" = 1
   test "$(docker inspect --format '{{index .Config.Labels "io.elspeth.web-coordination-protocol"}}' "$N_IMAGE")" = 1
   ```

   Expected GREEN: every per-file and combined run passes, repeated combined
   runs have identical scenario counts, one authority winner, no duplicate
   finalization/plugin effect, and no leaked processes.
5. Run neighboring AWS facade and PostgreSQL owner tests:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py \
     tests/unit/web/test_aws_ecs_acceptance.py \
     tests/unit/web/aws_ecs_acceptance \
     tests/unit/web/test_aws_ecs_runbook_contract.py
   ```
6. Commit:

   ```bash
   git add tests/testcontainer/web/multiprocess tests/testcontainer/web/test_multi_instance_*.py
   git commit -m "test(web): prove fenced transient overlap and recovery"
   ```

**Done when:** the complete matrix passes twice and the four runtime tracker
issues have exact regression evidence ready for closure.

## Task 14: Ship a Provider-Neutral Kubernetes Recreate Base

**Files:**

- Create: `deploy/kubernetes/base/kustomization.yaml`
- Create: `deploy/kubernetes/base/deployment.yaml`
- Create: `deploy/kubernetes/base/service.yaml`
- Create: `deploy/kubernetes/base/persistent-volume-claim.yaml`
- Create: `deploy/kubernetes/base/config-map.yaml`
- Create: `deploy/kubernetes/base/secret.example.yaml`
- Create: `tests/unit/deployment/test_kubernetes_bundle.py`

1. Write RED static tests requiring one `Deployment` with `Recreate`, one
   replica, `WEB_CONCURRENCY=1`, external PostgreSQL secret refs for sessions
   and Landscape, target `kubernetes`, immutable image example, port 8451,
   shallow liveness, readiness, UID/GID 1654, restrictive PVC initialization,
   and one ClusterIP `Service` plus PVC/config map. Reject PostgreSQL, ingress,
   TLS, storage class, database operator, or cloud identity resources. Exclude
   `secret.example.yaml` from Kustomize resources.
2. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 tests/unit/deployment/test_kubernetes_bundle.py
   ```

   Expected RED: bundle is absent.
3. Implement the minimal provider-neutral base and render with the verified
   kubectl binary:

   ```bash
   export DEPLOYMENT_TOOL_ROOT="$PWD/.cache/deployment-tools"
   render_dir=$(mktemp -d "$PWD/.cache/deployment-tools/render.XXXXXX")
   trap 'rm -rf -- "$render_dir"' EXIT
   "$DEPLOYMENT_TOOL_ROOT/bin/kubectl" kustomize deploy/kubernetes/base > "$render_dir/kubernetes.yaml"
   "$DEPLOYMENT_TOOL_ROOT/bin/kubectl" apply --dry-run=client --validate=false -f "$render_dir/kubernetes.yaml"
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 tests/unit/deployment/test_kubernetes_bundle.py
   ```

4. Commit:

   ```bash
   git add deploy/kubernetes/base tests/unit/deployment/test_kubernetes_bundle.py
   git commit -m "feat(deploy): add provider-neutral Kubernetes base"
   ```

**Done when:** rendering is deterministic and the shipped base contains no
harness/provider infrastructure.

## Task 15: Prove Kubernetes Startup and a Provider-Free Run in kind

**Files:**

- Create: `tests/testcontainer/deployment/kubernetes/kind-config.yaml`
- Create: `tests/testcontainer/deployment/kubernetes/postgresql.yaml`
- Create: `tests/testcontainer/deployment/kubernetes/storage-class.yaml`
- Create: `tests/testcontainer/deployment/kubernetes/persistent-volume.yaml`
- Create: `tests/testcontainer/deployment/kubernetes/provider-free-run.yaml`
- Create: `tests/testcontainer/deployment/kubernetes/test-overlay/kustomization.yaml`
- Create: `tests/testcontainer/deployment/kubernetes/test-overlay/secret.yaml`
- Create: `tests/testcontainer/deployment/test_kubernetes_kind.py`
- Create: `scripts/ci/kubernetes-kind-smoke.sh`

1. Write RED harness-contract tests for unique cluster/image names, the pinned
   kind/node/kubectl values, local image build/load, separate sessions and
   Landscape PostgreSQL databases, readiness, one provider-free run, bounded
   waits, sanitized diagnostics, and unconditional cleanup. Mark the file with
   both markers registered in Task 12C. Add a harness-only static StorageClass
   and PV, retain the shipped PVC unchanged, and reject any overlay `emptyDir`
   substitution.
2. Run:

   ```bash
   DEPLOYMENT_TOOL_ROOT="$PWD/.cache/deployment-tools" \
     env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m "testcontainer and kubernetes_kind" \
     tests/testcontainer/deployment/test_kubernetes_kind.py
   ```

   Expected RED: kind harness is absent.
3. Implement PostgreSQL/storage only under the harness directory; never
   reference them from the shipped base. Create a unique namespace
   `elspeth-kind-<nonce>`, create exact `elspeth_sessions_<nonce>` and
   `elspeth_landscape_<nonce>` databases with a bounded PostgreSQL init job,
   apply the shipped PVC plus harness PV/StorageClass, and assert:

   ```bash
   test "$(kubectl -n "$namespace" get pvc elspeth-data -o jsonpath='{.status.phase}')" = Bound
   test -z "$(kubectl -n "$namespace" get deploy elspeth-web -o jsonpath='{.spec.template.spec.volumes[?(@.emptyDir)].name}')"
   kubectl -n "$namespace" wait --for=condition=available deployment/elspeth-web --timeout=180s
   ```

4. Build/load the current immutable image, wait for `/api/ready`, then apply
   `provider-free-run.yaml`: a bounded Kubernetes Job using that same image,
   a harness ConfigMap containing settings/input, and exact arguments
   `run --settings /harness/settings.yaml --execute --format json`. Wait for
   Job completion, capture its sanitized log into a per-test `tmp_path`, parse
   exactly one `execution_result` with terminal success, and query Landscape to
   prove the matching audited run/baseline. No network/provider credential is
   present.
5. Delete the namespace and uniquely named cluster in `finally`/shell trap,
   wait until both are absent, and never print Secret objects or expanded URLs.
   Run twice and confirm `kind get clusters` has no test cluster.
6. Commit:

   ```bash
   git add tests/testcontainer/deployment/kubernetes \
     tests/testcontainer/deployment/test_kubernetes_kind.py \
     scripts/ci/kubernetes-kind-smoke.sh
   git commit -m "test(deploy): prove Kubernetes startup in kind"
   ```

**Done when:** two clean runs prove startup/readiness/execution with no leaked
cluster and no provider assumptions in `deploy/kubernetes/base/`.

## Task 16: Implement the Workload-Only ACA Bicep Module

**Files:**

- Create: `deploy/azure-container-apps/main.bicep`
- Create: `deploy/azure-container-apps/main.example.bicepparam`
- Create: `tests/unit/deployment/test_azure_container_apps_bundle.py`

1. Inspect the reverted bundle only as reference:

   ```bash
   git show 773fbd3bf:deploy/azure-container-apps/main.bicep | sed -n '1,260p'
   git show 773fbd3bf:tests/unit/deployment/test_azure_container_apps_bundle.py | sed -n '1,260p'
   ```

2. Write RED source-contract tests requiring an existing custom-VNet ACA
   environment, user-assigned identity, Key Vault refs, immutable ACR digest,
   external PostgreSQL URLs by secret ref, explicit deployment generation/full
   compatibility key, NFS Azure Files generation subpath, UID/GID 1654, health/
   ready probes, one process, min/max steady state one, and non-secret example
   parameters. Reject database/network/storage/identity/Key Vault creation,
   credentials in parameters, privileged permission repair, mutable image tags,
   and unconditional overlap claims.
3. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 tests/unit/deployment/test_azure_container_apps_bundle.py
   ```

   Expected RED: module is absent.
4. Implement ACA multiple-revision mode. Steady state is exactly one active
   revision at 100% traffic; equal-key overlap keeps one stable
   `deployment_generation`. Hard-cut inputs require old revision traffic 0,
   inactive state, and replicas 0 before role revocation.
5. Compile with the cached Bicep `v0.45.15` in an isolated directory:

   ```bash
   export DEPLOYMENT_TOOL_ROOT="$PWD/.cache/deployment-tools"
   bicep_dir=$(mktemp -d "$PWD/.cache/deployment-tools/bicep.XXXXXX")
   trap 'rm -rf -- "$bicep_dir"' EXIT
   "$DEPLOYMENT_TOOL_ROOT/bin/bicep" build deploy/azure-container-apps/main.bicep --stdout > "$bicep_dir/template.json"
   "$DEPLOYMENT_TOOL_ROOT/bin/bicep" build-params deploy/azure-container-apps/main.example.bicepparam --stdout > "$bicep_dir/parameters.json"
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 tests/unit/deployment/test_azure_container_apps_bundle.py
   ```

6. Commit:

   ```bash
   git add deploy/azure-container-apps/main.bicep \
     deploy/azure-container-apps/main.example.bicepparam \
     tests/unit/deployment/test_azure_container_apps_bundle.py
   git commit -m "feat(deploy): add workload-only ACA module"
   ```

**Done when:** compiled artifacts use only operator prerequisites and exact-key
overlap inputs, with no provider resources or secrets created by the module.

## Task 17: Build the ACA Validator, Driver, Evidence Schema, and Scenario Contract

**Files:**

- Create: `src/elspeth/web/azure_container_apps_acceptance.py`
- Create: `src/elspeth/web/acceptance_common.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/__init__.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/authority.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/azure_adapter.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/db_admin.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/inventory.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/traffic.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/coordination_scenarios.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/nfs_faults.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/evidence.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/receipt.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/registry_artifacts.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/ledger.py`
- Create: `src/elspeth/web/_azure_container_apps_acceptance/cleanup.py`
- Modify: `src/elspeth/web/_aws_ecs_acceptance/receipt_contracts.py`
- Create: `scripts/acceptance/azure-container-apps.sh`
- Create: `deploy/azure-container-apps/acceptance-evidence.schema.json`
- Create: `tests/unit/deployment/test_azure_container_apps_acceptance.py`
- Create: `tests/unit/deployment/test_azure_container_apps_acceptance_contract.py`
- Create: `tests/unit/architecture/test_azure_container_apps_acceptance_dependencies.py`
- Create: `tests/unit/web/azure_container_apps_acceptance/test_facade.py`
- Create: `tests/unit/web/azure_container_apps_acceptance/test_authority.py`
- Create: `tests/unit/web/azure_container_apps_acceptance/test_ledger.py`
- Create: `tests/unit/web/azure_container_apps_acceptance/test_traffic.py`
- Create: `tests/unit/web/azure_container_apps_acceptance/test_nfs_faults.py`
- Create: `tests/unit/web/azure_container_apps_acceptance/test_evidence_receipt.py`
- Create: `tests/unit/web/azure_container_apps_acceptance/test_registry_artifacts.py`
- Create: `tests/unit/web/azure_container_apps_acceptance/test_cleanup.py`

1. Write RED tests for schema `elspeth.aca-provider-acceptance.v1`, immutable
   subject hashes, strict field allowlisting, dual-registry multiarch index plus
   amd64/arm64 children, authority-bound signer identity/issuer, BuildKit
   provenance/SBOM subjects, receipt/profile two-way binding,
   pre-review timestamp refusal, cleanup success, and exactly these 20 passed
   scenario IDs:

   1. `authority-preflight`
   2. `immutable-subject-binding`
   3. `startup-readiness-provider-free-run`
   4. `equal-key-overlap-handoff`
   5. `peer-cancellation`
   6. `websocket-ticket-handoff`
   7. `composer-progress-handoff`
   8. `maintenance-prewarm`
   9. `sigstop-stale-owner-refusal`
   10. `sigkill-owner-takeover`
   11. `azure-files-cross-revision-visibility`
   12. `azure-files-atomic-replace-contention`
   13. `azure-files-crash-before-replace`
   14. `azure-files-crash-after-replace`
   15. `azure-files-tombstone-delete-recovery`
   16. `global-coordination-retention`
   17. `runtime-telemetry-and-redaction`
   18. `incompatible-key-readiness-refusal`
   19. `maintenance-cutover-forward-only`
   20. `authority-scoped-cleanup-and-baseline-restore`

   Registry-artifact tests use complete OCI index/manifest/attestation fixtures.
   They require both recorded platform children in both registries, reject
   `null`, missing, duplicate, extra-subject, or wrong-subject evidence, require
   one provenance and one SPDX statement per child, accept only SLSA predicate
   types `https://slsa.dev/provenance/v0.2` and
   `https://slsa.dev/provenance/v1`, and require SBOM predicate type
   `https://spdx.dev/Document`. BuildKit evidence binds platform children;
   cosign binds the multiarch index.

2. Write RED authority/driver tests requiring a regular non-symlink mode-0600
   file owned by the caller, `environment_class: non-production`, destructive
   cleanup authorization, tenant/subscription/exact resource-group and managed-
   environment IDs, run prefix/deadline, exact prerequisite IDs, external
   mode-0700 raw-evidence directory, and live tags
   `elspeth.environment=acceptance` plus `elspeth.production=false`.
   Require separate operator-approved, disjoint run-scoped database-name and
   database-role prefixes, an exact dedicated acceptance-only Key Vault ARM ID
   plus disposable secret-name prefix/purge-or-tombstone policy/sole writer
   principal, and an exact normalized NFS run root beneath the authorized
   share. Require an operator-attested control-plane change freeze and exclusive
   data-plane writer window through cleanup; enumerate access policies and role
   assignments and reject another or uninspectable set/delete/recover/purge
   principal. A prefix by itself is not concurrency authority. The driver
   ledger may narrow/derive subjects inside these
   bounds but can never create or broaden destructive authority.
   Read and bind the vault's soft-delete retention, purge-protection setting,
   caller purge permission, and explicit authority choice for run-owned secret
   names: purge when allowed or retain an exact owned soft-deleted tombstone.
   Require database-admin authority as a versioned non-secret resolver: either
   AAD token acquisition metadata or exact Key Vault secret name plus immutable
   version. Authority/evidence/ledger may contain resolver identity, never the
   resolved token/password/URL/value. Preflight uses the pinned PostgreSQL
   client image and proves only the exact required create/connect/terminate/
   role privileges before mutation.
3. Require a separate external atomic control ledger: regular non-symlink,
   caller-owned mode `0600`, outside the worktree. Updates use same-directory
   mode-0600 temp, file fsync, atomic rename, and directory fsync. Reject parent
   traversal, absolute/escaping NFS children, any symlink component, authority/
   image/generation/subject mismatch, or a prefix where an exact ID is required.
   Before any mutation it records intent for exact ARM container IDs and
   disposable IDs, disjoint contained database and role prefixes, each unique
   run-scoped disposable Key Vault secret **name** plus nonce/owner tag (never a
   not-yet-created version), existing prerequisite exact versions and their
   read-only or disable/restore posture, vault retention/purge posture and
   allowed terminal cleanup state, normalized NFS run root,
   revision/image/generation/key, scenario nonce, hashes, and cleanup deadline.
   ARM container authority never
   implies unrestricted authority over contained DB/role/secret/NFS objects.
   Reject every ledger subject outside or broader than its corresponding
   operator-granted contained-object bound.
4. Prove every mutation rechecks current Azure account/exact IDs/tags; every
   created resource receives run/owner tags; cleanup accepts only resource IDs
   returned by this run and cannot delete/retag/replace operator prerequisites.
   Raw evidence and authority paths/content must never enter the tracked receipt.
   Use that dedicated mutation vault, never a read-only prerequisite vault, for
   disposable secrets. Acquire and live-verify exclusive writer custody before
   checking absence and retain it through terminal cleanup. Then prove the
   intended name is absent from both active and soft-deleted inventories; any
   pre-existing collision blocks without mutation. The list+set API is not
   represented as atomic: the operator-attested/enforced exclusive window is
   the reservation that excludes a writer in the gap.
   Adopt the single provider-returned version only after success. On ambiguous
   timeout, list the complete active inventory for the intended name and
   require it to be a singleton equal to the matching nonce+owner version, with
   no deleted-name collision; zero/multiple/unmatched versions block and no
   second set occurs. Immediately before whole-name deletion, repeat the
   singleton all-versions ownership proof and revalidate exclusive custody;
   custody drift or an unmatched version blocks deletion. A competing external
   writer is a breached authority/change-freeze condition, not a race Azure's
   whole-name delete can fence. Delete/recover/purge is whole-name/all-version scope and
   cleanup is authorized by that unique name. If expressly authorized and
   possible, purge proves absence from active and deleted inventories;
   otherwise the exact owned soft-deleted tombstone is the accepted terminal
   state and its retention/purge-protection posture is recorded. Never recover
   or purge a non-owned name. Existing prerequisite versions
   are never deleted/recovered/purged and may only be read or exact-version
   disabled/restored. Capture preflight baselines for out-of-scope ARM children,
   databases, roles, secret names/versions/states, and NFS entries; cleanup must prove every
   baseline unchanged as well as every run-owned object absent/restored or,
   only for the authorized disposable Key Vault name scope, the exact owned
   soft-deleted tombstone terminal.
5. Test the intent -> action_started -> live postcondition_observed state
   machine. Every provider edge is idempotent by exact observed postcondition;
   timeout/disconnect is ambiguous and triggers a read of the pre-recorded exact
   identity before any bounded retry. `--resume` continues the first incomplete
   step. `--cleanup-only` runs no scenario and reconciles only ledger-owned
   subjects. Include missing/uninspectable/competing writer-authority refusal,
   access-policy/role drift refusal, active/deleted pre-set collision refusal, the provider-
   returned/adopted Key Vault version transition, ambiguous zero/multiple/
   unmatched-version refusal, concurrent-version pre-delete refusal, purge-
   allowed absence, purge-protected owned-tombstone terminal state, and non-
   owned tombstone refusal. Every
   scenario has a hard timeout and no silent retry loop.
6. Test ACA multiple-revision transitions: candidate 0% prewarm, exact-key/
   stable-generation readiness, candidate100/old0 readback, old drain,
   deactivate, then old traffic0 + replicas0 before steady. Hard cut requires
   every old revision inactive/traffic0/replicas0 before DB role revocation.
   The SIGSTOP acceptance-only probe profile is nonce/revision-bound, has a
   liveness budget greater than lease+takeover+observation, cannot be selected
   in production, and is removed before steady state.
7. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/deployment/test_azure_container_apps_acceptance.py \
     tests/unit/deployment/test_azure_container_apps_acceptance_contract.py \
     tests/unit/architecture/test_azure_container_apps_acceptance_dependencies.py \
     tests/unit/web/azure_container_apps_acceptance
   ```

   Expected RED: validator/driver/schema are absent.
8. Extract provider-neutral canonical hashing, closed projection, immutable-
   subject, and redaction helpers into `acceptance_common.py`; migrate AWS to
   them with unchanged AWS facade/receipt regressions. ACA must never import an
   AWS-private module. Keep the public ACA facade side-effect-free; provider/
   domain logic lives only in the acyclic private package. The shell script is
   a thin facade invocation.
   `registry_artifacts.py` owns remote OCI attestation resolution and the
   fail-closed child-subject/predicate checks; the public facade only parses the
   command and delegates. No provider call occurs at import time.
9. Implement Azure calls only through `azure_adapter.py`, which executes the
   exact pinned Azure CLI image using a disposable mode-0700 copy of the
   operator Azure config. That copy may refresh, is never mounted over the
   source config, and is destroyed after the run. Implement database calls only
   through `db_admin.py` using
   `postgres:17.6-bookworm@sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3`
   on linux/amd64 child
   `sha256:45cd22f8d32e189d245403954882f88e7a8714301fda80dab6da90f1265b25a3`.
   Resolve credentials into that process boundary only and destroy them after
   use. Implement strict shell mode, pre-recorded
   intents, exact-ID cleanup reconciliation, redacted external evidence, and a
   best-effort trap that never substitutes for the ledger.
10. Rerun the command, all AWS facade/private receipt tests, architecture
    dependency tests, and the secret scan.
11. Commit:

   ```bash
   git add src/elspeth/web/azure_container_apps_acceptance.py \
     src/elspeth/web/acceptance_common.py \
     src/elspeth/web/_azure_container_apps_acceptance \
     src/elspeth/web/_aws_ecs_acceptance/receipt_contracts.py \
     scripts/acceptance/azure-container-apps.sh \
     deploy/azure-container-apps/acceptance-evidence.schema.json \
     tests/unit/deployment/test_azure_container_apps_acceptance.py \
     tests/unit/deployment/test_azure_container_apps_acceptance_contract.py \
     tests/unit/architecture/test_azure_container_apps_acceptance_dependencies.py \
     tests/unit/web/azure_container_apps_acceptance tests/unit/web/aws_ecs_acceptance
   git commit -m "feat(deploy): define safe ACA provider acceptance"
   ```

**Done when:** local contract tests prove exact authority, scenario, redaction,
subject, registry-attestation, and cleanup semantics without claiming live
provider success.

## Task 18: Document Epoch-37 Hard Cut, Kubernetes, and ACA Candidate Operation

**Files:**

- Create: `docs/runbooks/kubernetes-deployment.md`
- Create: `docs/runbooks/azure-container-apps-deployment.md`
- Create: `docs/runbooks/azure-container-apps-acceptance.md`
- Modify: `docs/runbooks/index.md`
- Modify: `docs/runbooks/staging-session-db-recreation.md`
- Modify: `docs/runbooks/aws-ecs-deployment.md`
- Modify: `docs/reference/deployment-platforms.md`
- Test: `tests/unit/docs/test_deployment_platform_docs.py`
- Test: `tests/unit/docs/test_staging_session_recreation_policy.py`

1. Write RED docs tests for the ordered maintenance state machine: maintenance
   ingress; drain/cancel; set every old revision to traffic 0; deactivate it;
   verify traffic 0 and replicas 0; revoke versioned old sessions/Landscape roles;
   terminate old connections; verify no reconnect; irreversible boundary;
   recreate pre-release databases; create fresh roles/secrets; select fresh
   generation-specific `elspeth/e37/<cutover-id>` NFS subtree; probe; restore
   ingress. Before role revocation the operator may abort; after it, only fix
   forward. Do not restart the old image against recreated state.
2. Require ACA multiple-revision equal-key steps with one stable storage
   generation: candidate active at 0, direct revision readiness, candidate100/
   old0 live readback, drain, deactivate, and old traffic0/replicas0 before
   candidate steady. Every edge records intent/action/observed postcondition and
   is idempotent under resume. Require incompatible-key refusal, plus distinct
   `SIGKILL` and `SIGSTOP`/`SIGCONT` acceptance procedures. Document the ACA
   authority/live tags/exact IDs/raw-evidence/cleanup boundaries and all 20
   scenarios. State the exact compatibility key `(37, 29, 1)` and protocol-v1
   bump rules. Document versioned DB-admin resolver authority, pinned `psql`
   client, ephemeral Azure-config copy, contained DB/role/Key-Vault/NFS scopes,
   and outside-baseline proof. Kubernetes docs must state `Recreate` and
   external prerequisites.
3. Keep public ACA wording at release-candidate/unmaintained. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/docs/test_deployment_platform_docs.py \
     tests/unit/docs/test_staging_session_recreation_policy.py
   ```

   Expected RED: complete cutover/provider contract is absent.
4. Implement the runbooks/reference, rerun, and commit:

   ```bash
   git add docs/runbooks docs/reference/deployment-platforms.md \
     tests/unit/docs/test_deployment_platform_docs.py \
     tests/unit/docs/test_staging_session_recreation_policy.py
   git commit -m "docs(deploy): define maintained cutover and provider operation"
   ```

**Done when:** runbooks describe enforceable equal-key overlap and forward-only
hard cuts without prematurely claiming ACA provider acceptance.

## Task 19: Add Exactly Five Profiles and Pre-Author Both Receipt States

**Files:**

- Create: `deploy/platforms/schema.json`
- Create: `deploy/platforms/docker-compose.yaml`
- Create: `deploy/platforms/linux-systemd.yaml`
- Create: `deploy/platforms/aws-ecs.yaml`
- Create: `deploy/platforms/azure-container-apps.yaml`
- Create: `deploy/platforms/kubernetes.yaml`
- Create: `tests/unit/deployment/fixtures/aca_release_candidate/`
- Create: `tests/unit/deployment/fixtures/aca_maintained/`
- Create: `tests/unit/deployment/test_platform_profiles.py`
- Create: `tests/unit/deployment/test_aca_support_state_contract.py`
- Modify: `tests/unit/docs/test_public_release_docs.py`
- Modify: `tests/unit/docs/test_readme_release_surface.py`
- Modify: `tests/unit/docs/test_changelog_release_links.py`
- Modify: `tests/unit/website/test_release_site_contract.py`

1. Write RED tests requiring exactly five profiles, tracked existing artifacts
   and Task 18 runbooks, release-specific/immutable images, state/database
   ownership, extras, payload storage, process/replica posture, complete key
   `(SESSION_SCHEMA_EPOCH=37, SQLITE_SCHEMA_EPOCH=29,
   WEB_COORDINATION_PROTOCOL_VERSION=1)`,
   rollout, automated acceptance, and runbook. Enforce external PostgreSQL plus
   `postgres` for AWS/ACA/Kubernetes, Compose-only sidecar, no production
   `auto`, and no horizontal scale.
2. Require `none` overlap for Compose/systemd/AWS/Kubernetes and ACA-only
   equal-generation/equal-key `fenced-transient`, ACA multiple-revision mode,
   one active/100%-traffic steady state, generation-on-hard-cut, and
   same-key-only rollback. Include `storage_generation_required_on_schema_cutover:
   true`, `schema_change_posture: isolated-hard-cut`, and
   `rollback_scope: same-compatibility-key-only`.
3. Pre-author receipt-aware tests before acceptance using two complete tree
   fixtures: receipt absent + profile/docs/site/changelog all release-candidate;
   and valid bidirectionally bound receipt + profile/docs/site/changelog all
   maintained. Mutating subject, support state, profile contract, reference,
   SHA, or public claim fails. A malformed checked-in receipt is hard repository
   corruption, never a silent downgrade.
4. Run RED/GREEN:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/deployment/test_platform_profiles.py \
     tests/unit/deployment/test_aca_support_state_contract.py \
     tests/unit/docs/test_public_release_docs.py \
     tests/unit/docs/test_readme_release_surface.py \
     tests/unit/docs/test_changelog_release_links.py \
     tests/unit/website/test_release_site_contract.py
   ```

5. Implement the schema/profiles in release-candidate state with no receipt
   fields or maintained claim; validate every tracked path with Git.
6. Commit:

   ```bash
   git add deploy/platforms tests/unit/deployment \
     tests/unit/docs/test_public_release_docs.py \
     tests/unit/docs/test_readme_release_surface.py \
     tests/unit/docs/test_changelog_release_links.py \
     tests/unit/website/test_release_site_contract.py
   git commit -m "feat(deploy): add receipt-aware platform profiles"
   ```

**Done when:** runbooks preexist every profile reference, exactly five profiles
validate, and both legal ACA support states are tested before live acceptance.

## Task 19A: Make the Wardline Trust-Surface Gate Non-Inert

**Issue:** `elspeth-cec5c47cef`.

**Files:**

- Modify: `weft.toml`
- Modify: the narrow existing boundary/validation functions identified by
  Wardline explain output, only when a real boundary fix is required
- Create: `tests/fixtures/wardline/unsafe_crossing.py`
- Create: `tests/fixtures/wardline/safe_crossing.py`
- Create: `tests/unit/architecture/test_wardline_trust_surface.py`

1. Start the P1 atomically with `--advance`. Write RED tests that inspect the
   existing `@elspeth.contracts.trust_boundary.trust_boundary` inventory and
   require `weft.toml` to deliberately declare real external producers and
   guarded sanitisers/trusted producers through Wardline's supported
   `untrusted_sources` and `sanitisers` configuration. Do not blindly classify
   every custom decorator the same way and do not add Wardline as an ELSPETH
   runtime dependency merely to satisfy discovery.
2. Add one intentionally unsafe isolated fixture using Wardline's built-in
   external/trusted markers and one validated fixture. The architecture test
   runs Wardline over those fixtures and requires the unsafe crossing to exit 1
   with `PY-WL-101` while the safe crossing exits 0. The fixtures remain outside
   the configured production `source_roots`, so the repository gate does not
   hide or baseline the deliberate defect.
3. Run the live RED posture and focused tests:

   ```bash
   wardline assure . --format json
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/architecture/test_wardline_trust_surface.py
   ```

   Expected RED: live posture reports `boundaries_total: 0` and null coverage,
   or the declared fixture crossing is not enforced.
4. Configure the smallest truthful production trust surface. Run a local-only,
   fail-on-unanalyzed full scan; for every active error, immediately use
   `wardline explain-taint` and repair validation at the actual boundary. Do not
   baseline, waive, insert blind decorators, weaken severity, or edit the
   fixtures to manufacture green.
5. Run GREEN twice and assert both count and coverage are positive:

   ```bash
   wardline scan . --format jsonl --output .wardline/findings.jsonl \
     --fail-on error --fail-on-unanalyzed --local-only
   wardline assure . --format json | \
     jq -e '(.boundaries_total | type == "number") and .boundaries_total > 0 and (.coverage_pct | type == "number") and .coverage_pct > 0'
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/architecture/test_wardline_trust_surface.py
   ```

6. Commit the exact configuration, fixtures/tests, and any explained boundary
   repairs in one P1 commit; close the issue only after the second identical
   scan/posture pass.

   ```bash
   git add weft.toml tests/fixtures/wardline \
     tests/unit/architecture/test_wardline_trust_surface.py
   git add -u -- src/elspeth
   git diff --cached --name-only
   git commit -m "fix(security): make Wardline trust coverage effective"
   ```

**Done when:** the real ELSPETH trust surface is nonzero and has a definite
numeric posture, a known unsafe crossing trips the error gate, a validated
crossing passes, and no suppression or runtime dependency fakes coverage.

## Task 20: Unify CI, Gate Modes, and Dual-Registry Accepted-Index Publication

**Files (exclusive ownership for this task):**

- Modify: `.github/workflows/ci.yaml`
- Modify: `.github/workflows/build-push.yaml`
- Create: `scripts/ci/local-release-gates.sh`
- Create: `tests/unit/deployment/test_deployment_ci_gates.py`
- Create: `tests/unit/scripts/ci/test_local_release_gates.py`
- Modify: `tests/unit/test_ci_workflow_xdist.py`
- Modify: `tests/unit/test_build_push_release_checks.py`

1. Write RED workflow-contract tests requiring `release/**` branch filters and
   jobs `web-multi-instance`, `kubernetes-kind`, and
   `azure-container-apps-bicep`. Require every job in `ci-success.needs` and an
   explicit success assertion. Reject path-filter skipping. Each new job uses
   `ubuntu-24.04`, `needs: [static-analysis]`, an explicit timeout, minimum
   read-only permissions, exact checksums/actions, and fails when its dedicated
   selection collects zero tests. Make the ordinary container job use:

   ```text
   -m "testcontainer and not multi_instance and not kubernetes_kind"
   ```

   Require dedicated jobs to use explicit paths and:

   ```text
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m "testcontainer and multi_instance" ...
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m "testcontainer and kubernetes_kind" ...
   ```

2. Require exact tool/action pins from this plan. The Bicep job compiles both
   files and runs `tests/unit/deployment/test_azure_container_apps_bundle.py`
   plus acceptance contract tests. The kind job verifies kind/kubectl
   checksums before collection. Frontend jobs use the pinned setup-node action,
   exact Node `24.18.0`, install exact npm `11.6.2`, and verify both versions
   before `npm ci`. Split unit and container commands everywhere.
3. Write RED release-workflow tests with two unchanged modes. Ordinary main-
   branch image flow keeps its current build behavior. A version-tag release
   validates the maintained receipt/profile and finds the already-present
   accepted all-extras multiarch candidate index in the exact GHCR and ACR
   repositories. Within each registry, promote that repository's candidate
   digest to the final tag with `docker buildx imagetools create`; prohibit a
   build or cross-registry copy. Require both final tags to resolve to the same
   accepted index digest and exact amd64/arm64 children. Using cached cosign
   v3.0.6, verify each index with exact receipt-bound
   `--certificate-identity` and `--certificate-oidc-issuer`. Inspect OCI
   revision, `io.elspeth.install-extras=all`, representative imports,
   build-input manifest, and the packaged fail-closed child-subject validator
   over BuildKit provenance/SBOM predicates, not printed buildx output or bare
   attestation-existence verification.
4. Create a repository-owned `scripts/ci/local-release-gates.sh` and RED
   contract test with two modes. No arguments means `--agent-merge`: strict
   shell mode, project-root resolution,
   isolated `mktemp -d` artifacts/traps, and mirrors the live workflow's static/
   policy enforcers; ruff check + format; mypy; Python 85% global plus existing
   Landscape/canonical/orchestrator/contracts floors; all Python unit/property/
   E2E and integration selections; ordinary, multi-instance, and kind container
   lanes; frontend `npm ci`, typecheck, vitest, and Playwright; Bicep compile;
   deployment/profile/receipt/release gates. It fails on zero tests and accepts
   no path that silently skips a required phase. Local parity is current-tree
   evidence, not proof that GitHub-hosted required contexts ran.
   At minimum, the script contains these exact quality invocations (plus the
   live static/enforcer commands copied verbatim from `ci.yaml`):

   ```bash
   env -u VIRTUAL_ENV uv run --frozen ruff check src/ tests/ scripts/ examples/ elspeth-lints/src/
   env -u VIRTUAL_ENV uv run --frozen ruff format --check src/ tests/ scripts/ examples/ elspeth-lints/src/
   env -u VIRTUAL_ENV uv run --frozen mypy src/ elspeth-lints/src/
   env -u VIRTUAL_ENV uv run --isolated --all-extras --frozen --python 3.12 pytest -q -n 0 tests/unit
   env -u VIRTUAL_ENV uv run --frozen python scripts/check_contracts.py
   env -u VIRTUAL_ENV uv run --frozen python scripts/cicd/check_slot_type_cross_language.py
   env -u VIRTUAL_ENV uv run --frozen python scripts/cicd/generate_skill_inventory.py --check
   env -u VIRTUAL_ENV uv run --frozen pytest tests/ -q -n 0 \
     --cov=src/elspeth --cov-report=term-missing --cov-fail-under=85 -m "not testcontainer"
   env -u VIRTUAL_ENV uv run --frozen coverage report --include="src/elspeth/core/landscape/*" --fail-under=92
   env -u VIRTUAL_ENV uv run --frozen coverage report --include="src/elspeth/core/canonical.py" --fail-under=99
   env -u VIRTUAL_ENV uv run --frozen coverage report --include="src/elspeth/engine/orchestrator/*" --fail-under=90
   env -u VIRTUAL_ENV uv run --frozen coverage report --include="src/elspeth/contracts/*" --fail-under=62
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 \
     -m "testcontainer and not multi_instance and not kubernetes_kind" tests/testcontainer
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 \
     -m "testcontainer and multi_instance" tests/testcontainer/web/test_multi_instance_*.py
   env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 \
     -m "testcontainer and kubernetes_kind" tests/testcontainer/deployment/test_kubernetes_kind.py
   (cd src/elspeth/web/frontend && npm ci && npm run typecheck && \
     npm test -- --run && npx --no-install playwright install chromium && \
     CI=true npm run test:e2e)
   wardline scan . --format jsonl --output "$artifact_dir/wardline-findings.jsonl" \
     --fail-on error --fail-on-unanalyzed --local-only
   wardline assure . --format json > "$artifact_dir/wardline-assure.json"
   jq -e '(.boundaries_total | type == "number") and .boundaries_total > 0 and (.coverage_pct | type == "number") and .coverage_pct > 0' \
     "$artifact_dir/wardline-assure.json"
   legis policy-boundary-check --root src/elspeth --repo-root .
   ```

   Copy the current `uv export` plus `pip-audit --strict` advisory policy and
   `pip-licenses --fail-on "GPL;AGPL"` command from `ci.yaml` verbatim into the
   agent-merge inventory. Put generated requirement/license reports only in the
   isolated artifact directory.

   The contract test also requires exact Bicep/cache-tool invocations, the
   Wardline coverage/scan and Legis policy-boundary commands above, and every
   CI policy/elspeth-lints enforcer in both modes; shortening either inventory
   is a gate failure. Wardline assurance must report a positive boundary count
   and numeric coverage—zero/null is an inert gate failure—and its scan writes
   only to the isolated artifact directory with sibling emission disabled. It
   requires the Python 3.12 phase to use
   `uv run --isolated --all-extras --frozen --python 3.12` so it neither lacks
   optional test/runtime dependencies nor replaces the prepared project
   environment. Agent-merge includes Python 3.12, pip-audit/license, and
   the trust inventory below with
   `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing`.
   It derives an explicit merge-base ref and runs non-keyed coverage/edit checks
   plus both scanners; the missing HMAC waives only cryptographic signature
   authenticity, never findings, metadata shape/scope, coverage, or edit rules:

   ```text
   check-judge-coverage --allowlist-root config/cicd/enforce_tier_model --forbid-unverified-judge-metadata
   check-judge-coverage --allowlist-root config/cicd/enforce_trust_boundary_honesty --forbid-unverified-judge-metadata
   elspeth-lints check --rules trust_tier.tier_model --root src/elspeth
   elspeth-lints check --rules trust_boundary.tests,trust_boundary.scope,trust_boundary.tier --root src/elspeth
   ```

   `--operator-release` requires nonempty
   `ELSPETH_JUDGE_METADATA_HMAC_KEY`, sets verification mode `required`, and
   reruns the same two coverage phases and two scanner phases against the same
   merge base. Its coverage commands omit the fork-specific
   `--forbid-unverified-judge-metadata` flag because required-mode scanners
   cryptographically verify current/re-signed records; keeping that flag would
   reject legitimate operator re-signing unconditionally. Tests assert phase
   parity and permit only the verification-mode/key posture and this keyed-vs-
   fork coverage flag difference. Neither mode claims hosted contexts ran.
   Agent-merge is explicitly non-authoritative; Task 21/25/27 must pass
   operator-release before freeze/merge. The open P0 blocks that required
   authentication, not agent-merge scanner execution.
5. Run RED tests:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/deployment/test_deployment_ci_gates.py \
     tests/unit/test_ci_workflow_xdist.py \
     tests/unit/test_build_push_release_checks.py \
     tests/unit/scripts/ci/test_local_release_gates.py
   ```

   Expected RED: missing jobs, exact selections, or aggregate dependencies.
6. Modify only the unified CI workflow and build/push workflow. Do not add deployment
   workflow files. All three jobs run on every applicable CI invocation so an
   aggregate cannot turn green by skipping them.
7. Run the unit command, each exact job test command, and a dry-run/list mode of
   the local gate proving every required phase is present.
8. Commit:

   ```bash
   git add .github/workflows/ci.yaml .github/workflows/build-push.yaml \
     scripts/ci/local-release-gates.sh \
     tests/unit/deployment/test_deployment_ci_gates.py \
     tests/unit/test_ci_workflow_xdist.py \
     tests/unit/test_build_push_release_checks.py \
     tests/unit/scripts/ci/test_local_release_gates.py
   git commit -m "ci: gate platforms and promote accepted image index"
   ```

**Done when:** unified CI cannot pass without all deployment jobs, Wardline
coverage is nonzero and its error gate plus Legis boundary evidence pass,
agent-merge and operator-release run phase-equivalent trust scanners/coverage with only
verification/key posture and the fork-only unverified-metadata flag differing,
shape-only is never an authoritative merge gate, and
dual-registry publication cannot rebuild, cross-copy, or substitute the
accepted index.

## Task 21: Review, Repair, and Verify the Complete Candidate Before Freeze

**Files:** none planned. Finding-driven repairs use the narrowest owning files
and one focused regression commit each.

1. Re-audit the atomic epoch-37 commit from Task 3. Search every current 0.7.2
   code, test, runbook, website, AWS receipt, and release surface for a stale
   epoch-36 fact, while preserving historical 0.7.1 epoch-35 statements. Run:

   ```bash
   rg -n 'SESSION_SCHEMA_EPOCH.{0,20}36|session epoch 36|expect 36' \
     src tests README.md CHANGELOG.md website docs
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/web/sessions/test_schema.py \
     tests/unit/web/sessions/test_blob_inline_resolutions_schema.py \
     tests/unit/web/sessions/test_interpretation_events_table.py \
     tests/integration/web/composer/guided/test_schema9_epoch.py \
     tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py \
     tests/unit/web/aws_ecs_acceptance/test_cleanup_control_service.py \
     tests/unit/docs/test_release_version_surfaces.py \
     tests/unit/docs/test_readme_release_surface.py \
     tests/unit/docs/test_composer_capability_docs.py \
     tests/unit/docs/test_staging_session_recreation_policy.py \
     tests/unit/website/test_release_site_contract.py
   ```

   Expected: no current 0.7.2 surface says epoch 36, Landscape remains 29, the
   AWS structural label names the 35-to-37 change, and historical assertions
   remain intact. Classify search matches rather than blindly editing history.
2. Run an independent code-review session over Tasks 1-20, repair every blocker
   and relevant warning with focused regressions, then execute the exact
   repository-owned gate used again after acceptance:

   ```bash
   env -u VIRTUAL_ENV bash scripts/ci/local-release-gates.sh --agent-merge
   env -u VIRTUAL_ENV bash scripts/ci/local-release-gates.sh --operator-release
   ```

   The first command reports every preparatory phase and a nonzero test count,
   including shape-only trust coverage/scanners. The second requires the
   operator-held HMAC key and reruns the phase-equivalent inventory with
   signature verification required and without the fork-only
   `--forbid-unverified-judge-metadata` flag. Resolve stale allowlist/source
   entries, scope-fingerprint drift, and signature failures through the
   operator-owned P0; do not weaken or suppress the policies. If the key or
   valid signatures are absent, comment the exact blocker on
   `elspeth-18fe6e759e` and stop here. These commands do not claim hosted `CI
   Success`/CodeQL contexts ran. Close the P0 only after required-mode evidence
   is current.
   Confirm Task 19A already closed `elspeth-cec5c47cef`; the full gate must
   reproduce its positive Wardline boundary/coverage posture, unsafe-crossing
   enforcement, error scan, and Legis policy-boundary evidence. Any regression
   reopens/repairs that P1 before operator-release; do not weaken the assertion
   or treat an inert scan as green.
   Commit each repair separately; rerun its focused RED/GREEN regression and
   the complete script after the final repair.

**Done when:** independent review has no blocker, both preparatory and operator-
required pre-provider gates are green, the P0 is resolved with current
evidence, the worktree is clean, and ACA still says release-candidate.

## Task 22: Freeze the Reviewed Candidate and Build the Immutable Image

**Files:** no source edits; build artifacts remain untracked.

1. Before freezing or publishing, perform a read-only preflight. Require the
   exact mode-0600 authority and control-ledger paths, mode-0700 raw-evidence
   directory, current Azure login/account matching tenant/subscription, live
   non-production tags and exact prerequisite IDs; exact GHCR and ACR
   repository names with candidate publish/read scope; and authority-bound
   cosign certificate identity and OIDC issuer. Require the separate operator-
   granted database-name, database-role, disposable-secret-name/purge-policy,
   and normalized NFS-root bounds that Task 17 validates; ledger intent must be
   a strict subset of each. Reconfirm the exact dedicated mutation vault, sole
   writer principal, enumerated assignments/policies, and operator change-freeze
   window through cleanup; a general prerequisite vault or name prefix alone is
   insufficient. Require Docker/buildx with
   amd64+arm64/QEMU support, cached cosign v3.0.6, jq, Git/uv/node/npm,
   kubectl/kind/Bicep checksums, the pinned Azure CLI image, and pinned
   PostgreSQL client index/amd64 child.

   Database-admin authority must be a versioned non-secret resolver (AAD token
   metadata or exact Key Vault secret name/version). Resolve it only into the
   pinned `psql` process, prove exact create/connect/terminate/role privileges,
   and retain no value. Make an ephemeral caller-owned mode-0700 copy of Azure
   CLI config; the container may refresh that copy, never the source, and the
   copy is destroyed after preflight/run. If any check is absent or ambiguous,
   comment the exact blocker on ACA and stop without publishing.
2. Confirm clean state and capture the reviewed immutable subject:

   ```bash
   test -z "$(git status --porcelain)"
   CANDIDATE_SHA=$(git rev-parse HEAD^{commit})
   CANDIDATE_TREE=$(git rev-parse HEAD^{tree})
   git show --stat --oneline "$CANDIDATE_SHA"
   ```

3. Generate the canonical build-input manifest before build. It covers
   `Dockerfile`, `.dockerignore`, admitted `src/**`, frontend locks,
   `pyproject.toml`, `uv.lock`, `elspeth-lints/**`, the deterministic build-only
   README stub, `INSTALL_EXTRAS=all`, pinned base/uv images, both target
   platforms, BuildKit/buildx version, and build args. Host `README.md` is not
   an image input. Prove every Task 24 allowlisted path is disjoint from this
   manifest before building.

4. Build one all-extras multiarch index and push that same build to the exact
   preflighted GHCR and ACR repositories:

   ```bash
   export DEPLOYMENT_TOOL_ROOT="$PWD/.cache/deployment-tools"
   export INSTALL_EXTRAS="all"
   export TARGET_PLATFORMS="linux/amd64,linux/arm64"
   export GHCR_CANDIDATE_REF="$GHCR_ACCEPTANCE_REPOSITORY:acceptance-$CANDIDATE_SHA"
   export ACR_CANDIDATE_REF="$ACR_ACCEPTANCE_REPOSITORY:acceptance-$CANDIDATE_SHA"
   docker buildx build --platform "$TARGET_PLATFORMS" \
     --build-arg INSTALL_EXTRAS="$INSTALL_EXTRAS" \
     --label "org.opencontainers.image.revision=$CANDIDATE_SHA" \
     --label "io.elspeth.install-extras=all" \
     --label "io.elspeth.web-coordination-protocol=1" \
     --provenance=mode=max --sbom=true --push \
     --tag "$GHCR_CANDIDATE_REF" --tag "$ACR_CANDIDATE_REF" .
   export CANDIDATE_INDEX_DIGEST="$(docker buildx imagetools inspect "$GHCR_CANDIDATE_REF" --format '{{.Manifest.Digest}}')"
   test -n "$CANDIDATE_INDEX_DIGEST"
   test "$(docker buildx imagetools inspect "$ACR_CANDIDATE_REF" --format '{{.Manifest.Digest}}')" = "$CANDIDATE_INDEX_DIGEST"
   artifact_dir=$(mktemp -d "$PWD/.cache/deployment-tools/artifacts.XXXXXX")
   trap 'rm -rf -- "$artifact_dir"' EXIT
   docker buildx imagetools inspect "$GHCR_CANDIDATE_REF" --raw > "$artifact_dir/ghcr-index.json"
   docker buildx imagetools inspect "$ACR_CANDIDATE_REF" --raw > "$artifact_dir/acr-index.json"
   export CANDIDATE_AMD64_DIGEST="$(jq -er '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest] | if length == 1 then .[0] else error("expected exactly one linux/amd64 image descriptor") end' "$artifact_dir/ghcr-index.json")"
   export CANDIDATE_ARM64_DIGEST="$(jq -er '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "arm64") | .digest] | if length == 1 then .[0] else error("expected exactly one linux/arm64 image descriptor") end' "$artifact_dir/ghcr-index.json")"
   ACR_AMD64_DIGEST="$(jq -er '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest] | if length == 1 then .[0] else error("expected exactly one linux/amd64 image descriptor") end' "$artifact_dir/acr-index.json")"
   ACR_ARM64_DIGEST="$(jq -er '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "arm64") | .digest] | if length == 1 then .[0] else error("expected exactly one linux/arm64 image descriptor") end' "$artifact_dir/acr-index.json")"
   [[ "$CANDIDATE_AMD64_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
   [[ "$CANDIDATE_ARM64_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
   test "$ACR_AMD64_DIGEST" = "$CANDIDATE_AMD64_DIGEST"
   test "$ACR_ARM64_DIGEST" = "$CANDIDATE_ARM64_DIGEST"
   ```

5. Inspect both raw indexes and bind the exact linux/amd64 and linux/arm64 child
   digests; they must match across registries. Pull/smoke amd64 natively and
   arm64 through QEMU. Assert OCI revision, extras/protocol labels, version, and
   representative all-extra imports (`elspeth.web.app`, `psycopg`, `boto3`,
   and `openai`) on both architectures. Live ACA later uses the amd64 child.
   Save diagnostic projections, then invoke the packaged registry-artifact
   validator as the actual gate. `imagetools inspect` may exit zero while
   rendering `null`, so none of the projection commands is an assertion by
   itself:

   ```bash
   docker buildx imagetools inspect "$GHCR_CANDIDATE_REF" --format '{{json .Provenance}}' > "$artifact_dir/ghcr-provenance.json"
   docker buildx imagetools inspect "$GHCR_CANDIDATE_REF" --format '{{json .SBOM}}' > "$artifact_dir/ghcr-sbom.json"
   docker buildx imagetools inspect "$ACR_CANDIDATE_REF" --format '{{json .Provenance}}' > "$artifact_dir/acr-provenance.json"
   docker buildx imagetools inspect "$ACR_CANDIDATE_REF" --format '{{json .SBOM}}' > "$artifact_dir/acr-sbom.json"
   env -u VIRTUAL_ENV uv run --frozen python -m \
     elspeth.web.azure_container_apps_acceptance verify-image-artifacts \
     --ghcr-reference "$GHCR_ACCEPTANCE_REPOSITORY@$CANDIDATE_INDEX_DIGEST" \
     --acr-reference "$ACR_ACCEPTANCE_REPOSITORY@$CANDIDATE_INDEX_DIGEST" \
     --amd64-child "$CANDIDATE_AMD64_DIGEST" \
     --arm64-child "$CANDIDATE_ARM64_DIGEST" \
     --diagnostic-directory "$artifact_dir"
   docker run --rm --platform linux/amd64 --entrypoint python \
     "$GHCR_ACCEPTANCE_REPOSITORY@$CANDIDATE_INDEX_DIGEST" -c \
     'import elspeth.web.app, psycopg, boto3, openai'
   docker run --rm --platform linux/arm64 --entrypoint python \
     "$GHCR_ACCEPTANCE_REPOSITORY@$CANDIDATE_INDEX_DIGEST" -c \
     'import elspeth.web.app, psycopg, boto3, openai'
   ```

6. Sign each repository index with cached cosign v3.0.6, then verify the
   authority-bound identity and issuer explicitly:

   ```bash
   COSIGN="$DEPLOYMENT_TOOL_ROOT/bin/cosign"
   "$COSIGN" sign --yes "$GHCR_ACCEPTANCE_REPOSITORY@$CANDIDATE_INDEX_DIGEST"
   "$COSIGN" sign --yes "$ACR_ACCEPTANCE_REPOSITORY@$CANDIDATE_INDEX_DIGEST"
   "$COSIGN" verify --certificate-identity "$COSIGN_CERTIFICATE_IDENTITY" \
     --certificate-oidc-issuer "$COSIGN_CERTIFICATE_OIDC_ISSUER" \
     "$GHCR_ACCEPTANCE_REPOSITORY@$CANDIDATE_INDEX_DIGEST"
   "$COSIGN" verify --certificate-identity "$COSIGN_CERTIFICATE_IDENTITY" \
     --certificate-oidc-issuer "$COSIGN_CERTIFICATE_OIDC_ISSUER" \
     "$ACR_ACCEPTANCE_REPOSITORY@$CANDIDATE_INDEX_DIGEST"
   ```

   The validator independently resolves OCI attestation manifests and blobs. It
   fails on null/missing/duplicate data, requires one supported-SLSA provenance
   statement and one `https://spdx.dev/Document` statement for each exact child
   in each registry, and rejects any subject-to-child mismatch.
   Record index/child digests, both registry subjects, build-input manifest,
   buildx/cosign versions, inspected provenance/SBOM, signature identity/issuer,
   OCI labels, and smoke results outside Git. Do not use bare `cosign
   verify-attestation` or successful diagnostic rendering as provenance/SBOM
   subject proof.
7. Use the packaged validator to render the deterministic *final intended ACA
   profile candidate* into the external acceptance workspace. It is the
   reviewed Task 19 profile with `support_status: maintained`, the fixed receipt
   reference `docs/operator/evidence/azure-container-apps/0.7.2.json`, and only
   the receipt `sha256` value omitted. Review that candidate read-only and hash
   its canonical contract. Task 24 must write exactly this candidate plus the
   eventual receipt digest; it may not design a different profile after live
   acceptance.
8. Hash Bicep, compiled template/parameters, validator, driver, private scenario
   package/control-ledger contract, evidence schema,
   acceptance tests, runbook contract, and the final intended ACA profile
   contract (canonicalized with only receipt `reference` and `sha256` omitted,
   as required by the design). Save these non-secret values in the external
   acceptance workspace.
9. Do not edit any bound source after this point. If a bound input changes,
   discard this freeze and repeat Tasks 21-22.

**Done when:** exact source/tree/all-extras index/amd64+arm64 child/artifact and
dual-registry identity-bound signature facts exist outside Git for Task 23. No
Git commit is expected.

## Task 23: Execute Live ACA and Azure Files Acceptance

**Files during execution:** raw evidence outside the worktree only.

1. Require an operator-supplied authority file and current Azure login:

   ```bash
   scripts/acceptance/azure-container-apps.sh \
     --authority-file /secure/aca-authority.json \
     --control-ledger /secure/aca-control-ledger.json \
     --candidate-sha "$CANDIDATE_SHA" \
     --image-index-digest "$CANDIDATE_INDEX_DIGEST" \
     --amd64-child-digest "$CANDIDATE_AMD64_DIGEST"
   ```

2. The preflight must reverify authority and ledger mode/owner/non-symlink,
   atomic-ledger subject binding, tenant/subscription, exact
   resource-group/environment/prerequisite IDs, live non-production tags,
   cleanup deadline, external raw-evidence directory, immutable image, exact
   GHCR/ACR index plus amd64 child, versioned DB-admin resolver, pinned `psql`
   client/privileges, ephemeral Azure-config copy, and all Task 22 hashes before
   the first mutation. No resolved DB or Azure credential enters authority,
   ledger, evidence, logs, or receipt.
3. Execute all 20 closed scenarios once each with per-scenario timeouts and
   ledger intent/action/observed-postcondition edges. Ambiguous outcomes are
   reconciled against exact identities; they are never treated as success or
   silently retried. The real Azure Files NFS mount is
   mandatory for the five storage scenarios. Use `SIGSTOP`/takeover/`SIGCONT`
   for stale-resume refusal and a separate permanent `SIGKILL` owner case.
4. Prove equal-key rollout ends with candidate active at 100% and every old
   revision inactive at traffic 0/replicas 0. Prove hard-cut reaches those old-
   revision postconditions before role revocation. Remove the acceptance-only
   SIGSTOP probe profile before steady state.
5. On success or failure, the trap requests cleanup; ledger-driven
   `--cleanup-only` is the durable recovery path. Separately reconcile exact
   ARM containers/disposable IDs, contained DB/role prefixes, disposable Key
   Vault unique-name scope/all versions using the single adopted version as
   evidence, and either expressly authorized purge with active/deleted absence
   or the exact owned soft-deleted tombstone required by purge protection;
   prerequisite exact-version disable/restore only, and the
   normalized NFS subtree. Refuse zero/multiple nonce+owner version matches.
   Restore prior traffic/revision
   settings and compare live post-cleanup inventories with captured outside-
   scope baselines; every prerequisite and out-of-authority child must remain
   unchanged, and each run-owned subject must be absent/restored or in its
   explicitly authorized owned soft-deleted terminal state. Destroy the
   ephemeral Azure-config copy and any resolved DB
   credential material.
6. Validate raw evidence outside Git, scan canaries/redaction, and retain it
   only in the operator's access-controlled store. Do not stage raw logs,
   authority content/path, database URLs, secret refs, IDs forbidden by the
   schema, or crash dumps.

**Blocking rule:** if any authority, credential, prerequisite, live tag,
provider behavior, cleanup, or scenario fails, keep ACA release-candidate,
comment the exact bounded reason on the ACA issue, repair within scope, repeat
review/freeze/build, and rerun all 20 scenarios. No partial receipt is valid.

**Done when:** the validator reports all 20 passed, cleanup/baseline restoration
passed, and a sanitized receipt candidate bound to Task 22 is ready. No Git
commit is expected yet.

## Task 24: Bind the Sanitized Receipt and Promote ACA Claims

**Closed post-acceptance path allowlist:**

- Create: `docs/operator/evidence/azure-container-apps/0.7.2.json`
- Modify: `deploy/platforms/azure-container-apps.yaml`
- Modify: `docs/reference/deployment-platforms.md`
- Modify: `docs/product/current-state.md`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `website/get-started.html`

No other file may change in this task.

1. Generate the tracked receipt through the packaged validator's strict
   allowlist. Require schema ID, reviewed source/tree, identical GHCR/ACR
   all-extras index digest, amd64/arm64 children, OCI/extras/protocol labels,
   identity-bound signers, inspected provenance/SBOM subjects, import/QEMU/
   live-amd64 smoke results, all artifact/profile hashes, exact compatibility
   key `(37, 29, 1)`, non-secret Azure subjects, pinned tools, exact scenario
   outcomes/evidence digests, authority-file SHA-256, database-clock time,
   redaction result, and cleanup/outside-baseline success.
2. Compute the SHA-256 of the receipt's exact bytes. Materialize exactly the
   final intended ACA profile candidate reviewed and hashed in Task 22, adding
   only the receipt's exact-byte digest. Recompute the canonical profile-
   contract hash in the receipt and verify both directions without self-
   reference. Any difference from the reviewed candidate other than the
   receipt digest invalidates acceptance.
3. Promote public ACA claims from release-candidate to maintained. Make
   Kubernetes maintained only if Task 15 and unified CI passed.
4. Run read-only binding checks without changing implementation:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/deployment/test_azure_container_apps_acceptance.py \
     tests/unit/deployment/test_azure_container_apps_acceptance_contract.py \
     tests/unit/deployment/test_platform_profiles.py \
     tests/unit/deployment/test_aca_support_state_contract.py \
     tests/unit/docs/test_deployment_platform_docs.py \
     tests/unit/docs/test_public_release_docs.py \
     tests/unit/docs/test_readme_release_surface.py \
     tests/unit/docs/test_changelog_release_links.py \
     tests/unit/docs/test_release_version_surfaces.py \
     tests/unit/website/test_release_site_contract.py
   allowlist_dir=$(mktemp -d "$PWD/.cache/deployment-tools/allowlist.XXXXXX")
   trap 'rm -rf -- "$allowlist_dir"' EXIT
   {
     git diff --name-only "$CANDIDATE_SHA" --
     git ls-files --others --exclude-standard
   } | sort -u > "$allowlist_dir/actual"
   printf '%s\n' \
     CHANGELOG.md README.md deploy/platforms/azure-container-apps.yaml \
     docs/operator/evidence/azure-container-apps/0.7.2.json \
     docs/product/current-state.md docs/reference/deployment-platforms.md \
     website/get-started.html | sort -u > "$allowlist_dir/allowed"
   comm -23 "$allowlist_dir/actual" "$allowlist_dir/allowed" > "$allowlist_dir/forbidden"
   test ! -s "$allowlist_dir/forbidden"
   ```

   Also compare every allowlisted path against Task 22's canonical image-input
   manifest and require an empty intersection. `README.md` is legal here only
   because Task 2B removed it from Docker inputs and uses a deterministic build-
   only stub. Expected: tests pass, changed paths are a subset of the closed
   allowlist, and no allowed claim path is an image input.
5. Commit only the allowlisted files:

   ```bash
   git add docs/operator/evidence/azure-container-apps/0.7.2.json \
     deploy/platforms/azure-container-apps.yaml docs/reference/deployment-platforms.md \
     docs/product/current-state.md CHANGELOG.md README.md website/get-started.html
   git commit -m "docs(deploy): bind maintained ACA provider evidence"
   ```

**Done when:** receipt and profile hashes bind both directions, public claims
name the evidence, the allowlist is image-input-disjoint, and no bound
implementation input changed after acceptance.

## Task 25: Run the Post-Acceptance Binding Verifier and Complete Gates

**Files:** no edits unless a failure forces invalidation and return to Task 21.

1. Recompute every receipt-bound hash from Git and registry, confirm the
   acceptance timestamp follows the reviewed candidate, verify source/tree/
   OCI revision and compatibility key, and ensure no forbidden field/canary is
   present in the tracked receipt or displayed output.
2. Confirm the only post-candidate changes are Task 24 allowlisted claim/
   receipt paths. Any other change invalidates the receipt and returns execution
   to Task 21, including another independent review and live rerun.
3. Run complete gates:

   ```bash
   env -u VIRTUAL_ENV bash scripts/ci/local-release-gates.sh --agent-merge
   env -u VIRTUAL_ENV bash scripts/ci/local-release-gates.sh --operator-release
   ```

4. Confirm agent-merge ran the same current-tree architecture, docs, version,
   Compose, Linux, AWS, dual-registry accepted-index release workflow,
   redaction, frontend, Python 3.12, pip-audit/license, coverage, deployment and
   Wardline/Legis gates as Task 21, including shape-only trust coverage and
   scanners. Confirm operator-release reran the phase-equivalent inventory in
   required signature mode, omitted only the fork-specific unverified-metadata
   flag, and passed on the receipt-bound tree. This is local evidence only; do
   not claim `CI Success`, CodeQL,
   or another hosted status ran.

**Done when:** both gate modes pass against the bound receipt commit, required
signature verification is current, and the worktree remains clean. No Git
commit is expected.

## Task 26: Conduct Final Independent Review and Repair Sessions

**Files:** determined only by findings; any bound-input repair invalidates ACA
evidence and returns to Task 21.

1. Run independent read-only reviews covering architecture/reality, runtime
   safety, database/saga/recovery, Kubernetes/ACA operations, CI/release gates,
   evidence/redaction, and documentation truthfulness.
2. Classify every finding against the live diff and exact reproducer. Repair
   all valid blockers and relevant warnings with a RED regression and focused
   verification.
3. If any repair touches code, tests, Bicep, profile contract, runbook contract,
   validator, driver, evidence schema, image, or acceptance test, invalidate the
   receipt and repeat Tasks 21-26. Claim-only wording/receipt binding may be
   repaired within Task 24's allowlist only if it does not change the canonical
   profile contract; otherwise repeat acceptance.
4. Re-audit current HEAD and rerun Task 25 after the last repair. Do not rely on
   earlier green output.

**Done when:** final reviewers report no blocker, every repair is committed and
reverified, the receipt is current, and the branch is clean.

## Task 27: Close Tracker Work and Merge to `release/0.7.2`

**Files:** no planned edits.

1. Heartbeat the still-building runtime/closeout claims and add provisional
   exact commits/verification evidence, but do not close implementation tasks
   before the merged tree is verified.
2. Verify primary and implementation worktrees are clean, release has not
   drifted unexpectedly, and the implementation branch contains the intended
   base:

   ```bash
   git -C /home/john/elspeth status --short
   git -C /home/john/elspeth/.worktrees/deferred-platform-completion status --short
   git -C /home/john/elspeth merge-base --is-ancestor \
     696b3d1414ed7a6789c8f25bf5cbdc5450385bdd \
     codex/deferred-platform-completion
   git -C /home/john/elspeth log --oneline --left-right \
     release/0.7.2...codex/deferred-platform-completion
   ```

3. If `release/0.7.2` advanced, inspect every intervening commit, merge/rebase
   the release tip into the implementation branch without discarding user
   changes, rerun impacted focused gates and the full Task 25 verifier, and
   repeat receipt acceptance if any bound input changed.
4. With both worktrees clean and all gates current, merge locally from the
   primary worktree using a non-interactive merge commit:

   ```bash
   git -C /home/john/elspeth switch release/0.7.2
   git -C /home/john/elspeth merge --no-ff codex/deferred-platform-completion \
     -m "merge: finish deferred deployment platforms for 0.7.2"
   ```

5. On merged `release/0.7.2`, rerun the binding verifier and both exact local
   gate modes, not only narrow tests:

   ```bash
   env -u VIRTUAL_ENV bash scripts/ci/local-release-gates.sh --agent-merge
   env -u VIRTUAL_ENV bash scripts/ci/local-release-gates.sh --operator-release
   ```

   Confirm the merge tree preserves the reviewed receipt and exact dual-
   registry index/child/artifact bindings. This does not claim hosted statuses
   ran. Required signature verification must pass; shape-only cannot validate
   the merge. Do not push unless the user separately asks.
6. Only after post-merge verification passes, add the merge commit and final
   commands to the existing runtime issues and three created tasks, then close
   exactly the proven implementation scopes. `elspeth-18fe6e759e` must already
   be resolved by Task 21 and its evidence reverified here.

**Done when:** local `release/0.7.2` contains the reviewed implementation and
current receipt, the merge is verified in required signature mode, and tracker
implementation tasks are closed. Hosted release publication remains outside
this local-merge plan.

## Final Acceptance Checklist

- [ ] Baseline defect `elspeth-aad3788b81` is fixed and full baseline recorded.
- [ ] PostgreSQL and SQLite use the same `SessionOperationAuthority` contract;
  creation uses closed kind `create`, non-null epoch-1 ID/token/owner/lease,
  initializes under that fence, atomically releases it before return, and first
  later acquisition advances to epoch 2;
  physical current-fence deletion cascades both, no deleted-ID registry exists,
  and stale update-only CAS cannot recreate rows.
- [ ] The exhaustive Sessions mutation inventory classifies every writer;
  session/blob/composer/tutorial/guided/audit-story/proposal/interpretation/
  execution writes are fenced and every global writer names its authority.
- [ ] PostgreSQL authority and run/leader leases use their own database clocks;
  divergent app clocks do not affect acquisition, renewal, expiry, or cleanup.
- [ ] Durable typed `WebRunStartPermit` and explicit `LocalRunStartPermit`
  subjects are exact/idempotent. Landscape validates/records the supplied
  permit and atomically creates run/token/sequence 0 without reading or
  mutating Sessions.
- [ ] Sessions start-vs-cancel CAS is the web linearization: cancel-before-
  permit leaves no Landscape run; start-permit winner means eventual baseline.
  A later cancel after a pre-Landscape crash rehydrates the permit, materializes
  the baseline solely to terminal-cancel, and invokes zero plugins. Incomplete
  sources never gain recovery eligibility.
- [ ] Every plugin/effect invocation is ordered authority -> synchronous
  admission audit -> call; after return, authority -> synchronous result audit
  -> result/disposition commit -> telemetry. Audit failure blocks the call or
  result exactly at its boundary.
- [ ] Recovery requires the exact revision, image digest, OCI revision,
  implementation fingerprint, resolver identity, and stable generation;
  missing, drifted, unversioned, or unsafe facts fail closed.
- [ ] Audit ownership is exhaustive across saga/reconciler/recovery/service/
  sessions and engine authority owners; derived telemetry follows committed
  audit and exporter failure cannot erase it.
- [ ] New cleanup is Sessions-only, globally epoch-fenced plus row-claim
  `SKIP LOCKED`, bounded, and leaves Landscape schema/epoch/retention unchanged;
  it never deletes durable run/audit/checkpoint/cancel facts.
- [ ] Metrics/logs use closed low-cardinality labels and leak no identifiers,
  content, URLs, secret refs/values, digests, or raw exception text.
- [ ] `SESSION_SCHEMA_EPOCH == 37`, guided schema 10, Landscape epoch 29, and
  `WEB_COORDINATION_PROTOCOL_VERSION == 1`; bump rules and every code/test/
  profile/runbook/envelope/receipt surface agree.
- [ ] N-1 is Task 12 and N is Task 12B: both contain mandatory audit, use key
  `(37,29,1)` and one generation, but have distinct real revision/image
  identities; incompatible-key readiness/fence refusal, changed-image recovery
  refusal, and forward-only generation/NFS cutover all pass.
- [ ] Permanent `SIGKILL` and resumable `SIGSTOP`/`SIGCONT` cases are separate.
- [ ] Kubernetes base is provider-neutral; kind proves a real startup/run.
- [ ] ACA Bicep is workload-only, uses immutable inputs, and reaches one active
  revision at 100% traffic in steady state; hard cut drains old traffic and
  replicas to zero before old authority or storage access is revoked.
- [ ] Exactly five profiles validate with one process/replica and honest
  overlap/support posture.
- [ ] Unified `CI` includes the three deployment jobs in `ci-success.needs`,
  uses release filters, exact pins, explicit markers/paths, and no skip path.
- [ ] Wardline assurance reports positive trust-boundary count and numeric
  coverage, Wardline fail-on-error runs local-only, and Legis policy-boundary
  evidence passes; zero/null trust coverage is not accepted as green.
- [ ] `--agent-merge` and `--operator-release` both pass before freeze, after
  acceptance, and after merge,
  including Python 3.12, pip-audit/license, trust-tier/boundary scanners, and
  non-keyed coverage/edit checks. Agent-merge is diagnostic shape-only;
  operator-release requires the key, reruns phase-equivalent coverage/scanners
  in required mode without the fork-only unverified-metadata flag, and is the
  authoritative local freeze/merge gate. Neither claims hosted contexts ran.
- [ ] Independent review, authority/registry/signing/tool preflight, and all
  provider-free gates precede source freeze and immutable image build.
- [ ] Live ACA/Azure Files acceptance passes all 20 scenarios under exact
  authority and cleanup boundaries.
- [ ] The external mode-0600 control ledger records intent before mutation,
  separate exact ARM-container and contained DB/role/disposable-Key-Vault-
  unique-name-all-versions/NFS scope, adopted provider version as evidence, and
  purge-or-owned-soft-deleted terminal state bound to live vault protection,
  with a dedicated mutation vault under an operator-attested/enforced sole-
  writer/change-freeze window through cleanup,
  with active/deleted pre-set absence and singleton all-version ownership
  reverified before whole-name deletion,
  plus prerequisite exact-version disable/restore only; outside baselines,
  reconciliation, timeouts, resume and cleanup-only state; unsafe paths and
  ambiguous unverified outcomes fail. DB admin uses a
  versioned non-secret resolver and pinned `psql`; no values enter evidence.
- [ ] Sanitized receipt/profile bind both directions and every post-acceptance
  tracked or untracked change is allowlisted; any bound drift caused a full
  repeat.
- [ ] One all-extras amd64/arm64 build pushed an identical index to exact GHCR
  and ACR, bound both children/OCI labels/import smokes, passed QEMU arm64 and
  live ACA amd64, and was identity-signed in each registry with cached cosign.
- [ ] The packaged validator rejected null/missing/duplicate data and proved
  one supported-SLSA provenance plus one SPDX SBOM subject for each exact
  amd64/arm64 child in both registries; cosign bound the common index. Host
  README and every Task 24 path are image-input-disjoint.
- [ ] Version-tag publication promotes the already-present accepted digest
  within each registry with no build/cross-copy/substitution and verifies both
  final tags, children, labels and signer identity.
- [ ] Final independent review and complete current-HEAD verification pass.
- [ ] Branch is merged and reverified locally on `release/0.7.2`; hosted
  publication and hosted required-check evidence remain outside this local-
  merge plan.
