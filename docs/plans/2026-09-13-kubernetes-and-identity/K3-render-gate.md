### Task K3: Checksum-pinned render gate in CI

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: K2. Runs before: K4. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered after K2 (K0 → K1 → K2 → K3 → K4). This task gives the Kubernetes
bundle its own CI lane: a `kubernetes-render` job that installs the K0-pinned
`kubectl` by SHA-256, renders the base and every overlay under
`deploy/kubernetes/` with `kubectl kustomize`, runs K1's rendered-manifest
contract test with the fail-loud switch set, and is gated by `ci-success` by
NAME (a `needs.<job>.result` check), not merely by membership in `needs`. A
pin test in K1's module binds the `kubectl` version and digest across the
`test` job, this job and the K0 facts document, and a parametrised mutation
test proves each of those assertions goes red when the thing it guards is
removed.

Measured on HEAD 072141b75 (2026-09-13):

- `ci.yaml:1044-1091` is the `azure-container-apps-bicep` job (`runs-on:
  ubuntu-24.04`, `needs: [static-analysis]` at :1047, `timeout-minutes: 15`,
  `Install Bicep (pinned)` at :1053 using `sudo install`, pytest at :1091);
  `supply-chain-audit` starts at :1093. The new job is inserted between them.
- `ci.yaml:1332-1385` is `ci-success`: `needs:` list at :1335-1344
  (`- azure-container-apps-bicep` at :1341), `if: always()` at :1345, the
  `Check all jobs passed` step at :1347 whose script carries one
  `if [[ "${{ needs.<job>.result }}" != "success" ]]` block per job (the Bicep
  block is :1369-1372) and ends with `echo "All CI checks passed!"` at :1385.
  A job that is in `needs` but absent from that script never fails the gate.
- The house pin test shape is `test_azure_container_apps_bundle.py:509-527`
  (`test_ci_pins_the_measured_bicep_release_in_both_lanes`: reads
  `PLATFORM_FACTS`, asserts the sha and the backticked version are in it,
  counts one pinned install step per lane) and `:530-545`
  (`test_ci_compiles_every_template_and_parameter_file_and_gates_ci_success`:
  asserts `'needs.azure-container-apps-bicep.result }}" != "success"' in
  check`).
- `kubectl apply --dry-run=client` performs REST-mapper discovery and exits 1
  with `dial tcp 127.0.0.1:8080: connect: connection refused` without an API
  server (verified with v1.37.0, no kubeconfig). The render job therefore has
  NO dry-run step (DECISIONS K6); server admission is proven by K4's
  `kubectl apply -k` in kind.
- The `kubectl` pin: `https://dl.k8s.io/release/stable.txt` → `v1.37.0` on
  2026-09-13; `https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl.sha256`
  → `6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f`. K0
  records these in `docs/plans/2026-09-13-kubernetes-platform-facts.md`
  §1 Toolchain pins (§1.1 CLI binaries); if K0 measured a newer stable, K0's values win and
  every literal below is replaced with them (Step 1 checks).
- Neither `kubectl` nor `kind` is on this box (`which kubectl kind` → empty;
  `/usr/bin/docker` and `/usr/bin/curl` exist; the user is in the `docker`
  group), so the local proofs download the pinned binary into
  `.claude/lanes/k3/bin` (gitignored) and prepend it to `PATH`.
- `tests/unit/test_ci_workflow_xdist.py:201-222` constrains `-n` only on the
  `test` and `integration` jobs, and `:415-428` requires any job routed to
  `nyx-ci` to be push-only; a new `ubuntu-24.04` job running `pytest -n 0`
  trips neither. `tests/unit/cicd/test_state_engine_ci_selection.py:130-160`
  pins `needs` on `state-engine-validation`, `azure-container-apps-bicep` and
  `supply-chain-audit` only, so a `needs`-less new job cascades nowhere.
- `rhysd/actionlint:1.7.12` exists on Docker Hub (manifest digest
  `sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667`);
  it is used only as a local lint of the edited workflow, compared against a
  baseline run on the untouched file, never as a CI step.

**Files:**
- Modify: `.github/workflows/ci.yaml:1044-1091` (the `azure-container-apps-bicep` job; the new `kubernetes-render` job is inserted directly after its last line :1091 and before `supply-chain-audit:` at :1093)
- Modify: `.github/workflows/ci.yaml:1335-1344` (`ci-success.needs`; the new entry follows `- azure-container-apps-bicep` at :1341)
- Modify: `.github/workflows/ci.yaml:1369-1372` (the Bicep result-check block in `Check all jobs passed`; the new block follows it, before the `supply-chain-audit` block at :1373)
- Modify: `tests/unit/deployment/test_kubernetes_bundle.py` (K1's module — it does not exist on HEAD, so no line range can be measured; K1 already defines `CI_WORKFLOW`, `PLATFORM_FACTS`, `KUBECTL_VERSION` and `KUBECTL_SHA256`; K3 adds only three constants, directly after K1's `KUBECTL_SHA256 = "6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f"` line, and appends the tests at the end of the module)
- Test: `tests/unit/deployment/test_kubernetes_bundle.py`

**Interfaces:**
- Consumes:
  - `REPO_ROOT: Path`, `CI_WORKFLOW: Path`, `PLATFORM_FACTS: Path`, `KUBECTL_VERSION: str = "1.37.0"`, `KUBECTL_SHA256: str` and `_require_kubectl(reason: str) -> None` from K1's `tests/unit/deployment/test_kubernetes_bundle.py` (K1 defines them; K3 extends rather than redefines them; the switch `_require_kubectl` honours is `ELSPETH_CI_KUBECTL_REQUIRED`, DECISIONS K7).
  - K1's `test_ci_test_job_installs_the_pinned_kubectl` in the same module. Its name contains `_ci_`, so K3's `-k "_ci_ or _red_"` selection runs it too; it checks only the `test` job, so it passes before and after this task.
  - The `Install kubectl (Kubernetes bundle contract tests)` step K1 adds to the `test` job after the Bicep step (`ci.yaml:738-752`), carrying `KUBECTL_VERSION=1.37.0` and `KUBECTL_SHA256=6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f`. K1 owns that step because K1's `_require_kubectl` `pytest.fail`s under `GITHUB_ACTIONS`, so K1's own commit would red the `test` job without it; K3 only pins it.
  - `deploy/kubernetes/base/` (K1) rendering to exactly six objects (`ConfigMap`, `Deployment`, `Job`, `Job`, `PersistentVolumeClaim`, `Service`).
  - `docs/plans/2026-09-13-kubernetes-platform-facts.md` §1 Toolchain pins, §1.1 CLI binaries (K0), carrying `` `v1.37.0` `` and the sha above.
- Produces:
  - CI job id `kubernetes-render` (`runs-on: ubuntu-24.04`, no `needs`, `timeout-minutes: 15`), whose render step loops over every `kustomization.yaml` under `deploy/kubernetes/`: K4's `overlays/kind-test/` and `overlays/kind-acceptance/` and K6's `overlays/aks/` are rendered by this job without editing it — they only have to render offline.
  - The `ci-success` gate form for Kubernetes jobs, which K4 copies for `kubernetes-kind`: one `needs` entry AND one `if [[ "${{ needs.<job>.result }}" != "success" ]]; then echo "<job> failed"; exit 1; fi` block in the check script.
  - Module constants in `test_kubernetes_bundle.py` (beside K1's pin constants): `KUBECTL_DOWNLOAD_HOST: str = "dl.k8s.io/release"`, `KUBECTL_PINNED_JOBS: tuple[str, ...] = ("test", "kubernetes-render")`, `RESULT_GATED_JOBS: tuple[str, ...] = ("kubernetes-render",)`. K4 appends `"kubernetes-kind"` to `RESULT_GATED_JOBS` only and adds `KIND_VERSION` / `KIND_SHA256` beside them; `KUBECTL_PINNED_JOBS` stays `("test", "kubernetes-render")`, because the `kubernetes-kind` job installs kubectl inside `scripts/cicd/kubernetes-kind-smoke.sh`, not through a `ci.yaml` step, and K4's `_assert_kind_lane_pins(smoke: str, kind_config: dict, facts: str) -> None` binds that script's `KUBECTL_VERSION=`/`KUBECTL_SHA256=` lines to K3's constants.
  - Checker functions `_ci_workflow() -> dict`, `_run_text(job: dict) -> str`, `_assert_kubectl_pins(workflow: dict, facts: str) -> None`, `_assert_render_gate(workflow: dict) -> None`, and the tests `test_ci_pins_the_measured_kubectl_release_in_every_lane`, `test_ci_renders_every_kustomization_and_gates_ci_success`, `test_render_gate_goes_red_under_each_mutation`, `test_kubectl_pins_go_red_under_each_mutation`, `test_kubectl_pins_go_red_when_the_facts_document_disagrees`. K4 leaves both of K3's parametrised lists unchanged; it adds its `kubernetes-kind` mutations in its own `test_kind_job_gate_goes_red_under_each_mutation` and `test_kind_lane_pins_go_red_when_the_smoke_script_drifts`.

- [ ] **Step 1: Measure the preconditions K3 consumes (K0's pin, K1's `test`-job step and switch).**

Run each line and read its output before writing anything:

```bash
cd "$(git rev-parse --show-toplevel)" && grep -n 'kubectl' docs/plans/2026-09-13-kubernetes-platform-facts.md
cd "$(git rev-parse --show-toplevel)" && grep -n 'dl.k8s.io/release\|KUBECTL_VERSION=\|KUBECTL_SHA256=\|Install kubectl' .github/workflows/ci.yaml
cd "$(git rev-parse --show-toplevel)" && grep -n 'ELSPETH_CI_KUBECTL_REQUIRED\|def _require_kubectl\|pytest.fail(\|pytest.skip(' tests/unit/deployment/test_kubernetes_bundle.py
cd "$(git rev-parse --show-toplevel)" && grep -n '^REPO_ROOT = \|^BASE = \|^CI_WORKFLOW = \|^PLATFORM_FACTS = \|^KUBECTL_VERSION = \|^KUBECTL_SHA256 = \|^KUBECTL_DOWNLOAD_HOST\|^KUBECTL_PINNED_JOBS\|^RESULT_GATED_JOBS' tests/unit/deployment/test_kubernetes_bundle.py
```

Expected:
- Line 1 prints the facts table row with `` `v1.37.0` `` and
  `6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f`. If K0
  recorded a different version or sha, use K0's values everywhere this task
  writes `1.37.0` or the sha (the test-module constants, the job YAML and
  the local proof); the facts document is the authority and the pin test
  binds the workflow to it.
- Line 2 prints exactly ONE `dl.k8s.io/release` hit, inside the `test` job
  (between `Install Bicep (Azure Container Apps bundle contract tests)` and
  `Install dependencies`), with `KUBECTL_VERSION=1.37.0` and the same sha. If
  it prints nothing, K1 is incomplete: stop and report it, do not add the
  `test`-job step here (K1's commit is the one that reds the `test` job
  without it).
- Line 3 prints the `_require_kubectl` definition and one `pytest.fail(` and
  one `pytest.skip(` inside it, and the `ELSPETH_CI_KUBECTL_REQUIRED` name.
- Line 4 prints exactly six hits, in this order: `REPO_ROOT = Path(__file__).resolve().parents[3]`,
  `BASE = REPO_ROOT / "deploy" / "kubernetes" / "base"`,
  `CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yaml"`,
  `PLATFORM_FACTS = REPO_ROOT / "docs" / "plans" / "2026-09-13-kubernetes-platform-facts.md"`,
  `KUBECTL_VERSION = "1.37.0"` and `KUBECTL_SHA256 = "6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f"` (K1 defines
  all six), and no `KUBECTL_DOWNLOAD_HOST` / `KUBECTL_PINNED_JOBS` /
  `RESULT_GATED_JOBS` hit. The three new constants below go directly after
  K1's `KUBECTL_SHA256` line; do NOT re-add K1's four (a second assignment
  would silently shadow K1's pin and let the two drift).

- [ ] **Step 2: Write the failing pin tests and their mutation controls.**

Insert three constants directly after K1's `KUBECTL_SHA256 = "6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f"`
line. K1 already defines `CI_WORKFLOW`, `PLATFORM_FACTS`, `KUBECTL_VERSION`
and `KUBECTL_SHA256` (with the comment naming their dl.k8s.io source); the
checkers below read those, so they are not repeated here. The module already
imports `subprocess`, `Path`, `pytest` and `yaml`; add the `Callable` import to
the import block:

```python
# tests/unit/deployment/test_kubernetes_bundle.py — add to the imports
from collections.abc import Callable
```

```python
# tests/unit/deployment/test_kubernetes_bundle.py — directly after K1's KUBECTL_SHA256
# Every CI lane that installs kubectl carries exactly K1's KUBECTL_VERSION and
# KUBECTL_SHA256, and the checkers below bind each lane and the facts document
# to them, the way the Bicep pin is bound in test_azure_container_apps_bundle.py.
KUBECTL_DOWNLOAD_HOST = "dl.k8s.io/release"
# Jobs that install kubectl by checksum in a ci.yaml step. K4 does NOT extend
# this: the kind lane installs kubectl inside scripts/cicd/kubernetes-kind-smoke.sh.
KUBECTL_PINNED_JOBS: tuple[str, ...] = ("test", "kubernetes-render")
# Jobs whose result ci-success must check BY NAME in its script: ci-success is
# `if: always()`, so membership in `needs` alone never fails the gate.
# K4 appends "kubernetes-kind" (this tuple only).
RESULT_GATED_JOBS: tuple[str, ...] = ("kubernetes-render",)
```

Append the checkers, the tests and the mutation controls at the end of the
module:

```python
# tests/unit/deployment/test_kubernetes_bundle.py — append at the end


# ---------------------------------------------------------------------------
# CI pins: the kubectl release and the render gate (Task K3)
# ---------------------------------------------------------------------------


def _ci_workflow() -> dict:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), "ci.yaml root must be a mapping"
    return workflow


def _run_text(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"] if isinstance(step.get("run"), str))


def _assert_kubectl_pins(workflow: dict, facts: str) -> None:
    assert KUBECTL_SHA256 in facts, "the platform facts document does not record this kubectl sha256"
    assert f"`v{KUBECTL_VERSION}`" in facts, "the platform facts document does not record this kubectl version"
    pins = 0
    for job_name in KUBECTL_PINNED_JOBS:
        job = workflow["jobs"][job_name]
        first_pytest = next(index for index, step in enumerate(job["steps"]) if "pytest" in str(step.get("run", "")))
        for index, step in enumerate(job["steps"]):
            run = step.get("run")
            if not (isinstance(run, str) and KUBECTL_DOWNLOAD_HOST in run):
                continue
            assert f"KUBECTL_VERSION={KUBECTL_VERSION}" in run, f"{job_name}: kubectl version drifted from the pin"
            assert f"KUBECTL_SHA256={KUBECTL_SHA256}" in run, f"{job_name}: kubectl sha256 drifted from the pin"
            assert "sha256sum -c -" in run, f"{job_name}: kubectl download is not checksum-verified"
            assert index < first_pytest, f"{job_name}: kubectl is installed after the tests that need it"
            pins += 1
    assert pins == len(KUBECTL_PINNED_JOBS), f"expected one pinned kubectl install per job in {KUBECTL_PINNED_JOBS}, found {pins}"


def _assert_render_gate(workflow: dict) -> None:
    job = workflow["jobs"]["kubernetes-render"]
    assert "needs" not in job, "kubernetes-render must not wait on static-analysis (operator ruling 2026-09-05)"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 15
    runs = _run_text(job)
    assert "kubectl kustomize deploy/kubernetes/base" in runs, "the render job does not render the base"
    assert "find deploy/kubernetes -name kustomization.yaml" in runs, "the render job does not render every overlay"
    assert "--dry-run=client" not in runs, "client dry-run needs an API server and reds a clusterless job"
    assert "tests/unit/deployment/test_kubernetes_bundle.py" in runs, "the render job does not run the bundle test"
    pytest_step = next(step for step in job["steps"] if "pytest" in str(step.get("run", "")))
    assert pytest_step.get("env") == {"ELSPETH_CI_KUBECTL_REQUIRED": "1"}, "the bundle test must fail loudly, not skip"

    success = workflow["jobs"]["ci-success"]
    assert success["if"] == "always()"
    check = _run_text(success)
    for gated in RESULT_GATED_JOBS:
        assert gated in success["needs"], f"{gated} is not in ci-success.needs"
        assert f'needs.{gated}.result }}}}" != "success"' in check, f"ci-success never checks needs.{gated}.result"


def test_ci_pins_the_measured_kubectl_release_in_every_lane() -> None:
    _assert_kubectl_pins(_ci_workflow(), PLATFORM_FACTS.read_text(encoding="utf-8"))


def test_ci_renders_every_kustomization_and_gates_ci_success() -> None:
    _assert_render_gate(_ci_workflow())


# Mutation controls: each removes ONE thing a checker guards and proves the
# checker goes red for that reason. A pin test that cannot go red is not a gate.


def _drop_result_check(workflow: dict) -> None:
    step = workflow["jobs"]["ci-success"]["steps"][0]
    step["run"] = step["run"].replace("needs.kubernetes-render.result", "needs.kubernetes-rendr.result")


def _drop_from_needs(workflow: dict) -> None:
    workflow["jobs"]["ci-success"]["needs"].remove("kubernetes-render")


def _wait_on_static_analysis(workflow: dict) -> None:
    workflow["jobs"]["kubernetes-render"]["needs"] = ["static-analysis"]


def _drop_base_render(workflow: dict) -> None:
    for step in workflow["jobs"]["kubernetes-render"]["steps"]:
        if isinstance(step.get("run"), str):
            step["run"] = step["run"].replace("kubectl kustomize deploy/kubernetes/base", "true")


def _let_the_bundle_test_skip(workflow: dict) -> None:
    for step in workflow["jobs"]["kubernetes-render"]["steps"]:
        if "pytest" in str(step.get("run", "")):
            step.pop("env")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_drop_result_check, "never checks needs.kubernetes-render.result"),
        (_drop_from_needs, "is not in ci-success.needs"),
        (_wait_on_static_analysis, "must not wait on static-analysis"),
        (_drop_base_render, "does not render the base"),
        (_let_the_bundle_test_skip, "must fail loudly, not skip"),
    ],
    ids=["result-check-dropped", "needs-entry-dropped", "needs-static-analysis", "base-render-dropped", "switch-dropped"],
)
def test_render_gate_goes_red_under_each_mutation(mutate: Callable[[dict], None], message: str) -> None:
    workflow = _ci_workflow()
    mutate(workflow)
    with pytest.raises(AssertionError, match=message):
        _assert_render_gate(workflow)


def _corrupt_render_job_sha(workflow: dict) -> None:
    for step in workflow["jobs"]["kubernetes-render"]["steps"]:
        if isinstance(step.get("run"), str) and KUBECTL_DOWNLOAD_HOST in step["run"]:
            step["run"] = step["run"].replace(KUBECTL_SHA256, "0" * 64)


def _delete_render_job_kubectl_step(workflow: dict) -> None:
    job = workflow["jobs"]["kubernetes-render"]
    job["steps"] = [step for step in job["steps"] if KUBECTL_DOWNLOAD_HOST not in str(step.get("run", ""))]


def _move_test_job_kubectl_after_pytest(workflow: dict) -> None:
    job = workflow["jobs"]["test"]
    (kubectl_step,) = [step for step in job["steps"] if KUBECTL_DOWNLOAD_HOST in str(step.get("run", ""))]
    job["steps"] = [step for step in job["steps"] if step is not kubectl_step] + [kubectl_step]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_corrupt_render_job_sha, "kubectl sha256 drifted from the pin"),
        (_delete_render_job_kubectl_step, "expected one pinned kubectl install per job"),
        (_move_test_job_kubectl_after_pytest, "installed after the tests that need it"),
    ],
    ids=["sha-corrupted", "install-step-deleted", "install-after-pytest"],
)
def test_kubectl_pins_go_red_under_each_mutation(mutate: Callable[[dict], None], message: str) -> None:
    workflow = _ci_workflow()
    mutate(workflow)
    with pytest.raises(AssertionError, match=message):
        _assert_kubectl_pins(workflow, PLATFORM_FACTS.read_text(encoding="utf-8"))


def test_kubectl_pins_go_red_when_the_facts_document_disagrees() -> None:
    with pytest.raises(AssertionError, match="does not record this kubectl sha256"):
        _assert_kubectl_pins(_ci_workflow(), "a facts document that records a different kubectl")
```

- [ ] **Step 3: Run the new tests to verify they fail for the right reason.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/deployment/test_kubernetes_bundle.py -n 0 -k "_ci_ or _red_" > /tmp/klane-k3-step3.log 2>&1; echo exit=$?; grep -E "^(FAILED|PASSED|ERROR)|KeyError|ValueError|passed|failed" /tmp/klane-k3-step3.log`
Expected: `exit=1`, `9 failed, 3 passed` in the summary line:
- `test_ci_pins_the_measured_kubectl_release_in_every_lane`,
  `test_ci_renders_every_kustomization_and_gates_ci_success`, the four
  `test_render_gate_goes_red_under_each_mutation` ids other than
  `needs-entry-dropped`, and `test_kubectl_pins_go_red_under_each_mutation`'s
  `sha-corrupted` and `install-step-deleted` ids fail with
  `KeyError: 'kubernetes-render'` (the job does not exist yet, so the mutation
  or the checker raises before any assertion).
- `test_render_gate_goes_red_under_each_mutation[needs-entry-dropped]` fails
  with `ValueError: list.remove(x): x not in list` (`ci-success.needs` has no
  such entry yet).
- Three ids PASS already because they never consult the missing job:
  K1's `test_ci_test_job_installs_the_pinned_kubectl` (selected by `_ci_`; it
  checks only the `test` job K1 wired),
  `test_kubectl_pins_go_red_when_the_facts_document_disagrees` (the fake facts
  string fires the facts assertion first) and
  `test_kubectl_pins_go_red_under_each_mutation[install-after-pytest]` (it
  mutates the `test` job K1 already wired, and that job is checked first).
If instead `test_ci_pins_the_measured_kubectl_release_in_every_lane` fails
with `AssertionError: the platform facts document does not record this
kubectl sha256`, K0 recorded a different stable: copy K0's version and sha
into `KUBECTL_VERSION` / `KUBECTL_SHA256` and re-run before going on.

- [ ] **Step 4: Take an actionlint baseline of the untouched workflow.**

The gate in Step 8 is an empty DELTA against this baseline, not a zero
count: on HEAD 072141b75 the baseline is empty, but a later base commit may
carry findings of its own, and those are not this task's to clear.

Pull the image first so the pull chatter does not land in the baseline log:

Run: `docker pull rhysd/actionlint:1.7.12 > /dev/null; echo exit=$?`
Expected: `exit=0`.

Run: `cd "$(git rev-parse --show-toplevel)" && docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.12 -no-color .github/workflows/ci.yaml > /tmp/klane-k3-actionlint-before.log 2>&1; echo exit=$?; wc -l /tmp/klane-k3-actionlint-before.log`
Expected: on HEAD 072141b75 this was measured as `exit=0` and `0` lines
(actionlint 1.7.12 with its bundled shellcheck reports nothing on the
untouched workflow). Whatever it prints on your base commit is the baseline;
record the line count.

- [ ] **Step 5: Add the `kubernetes-render` job.**

Insert this block after `ci.yaml:1091` (the Bicep job's last line,
`run: uv run pytest tests/unit/deployment/test_azure_container_apps_bundle.py -v`)
and before `supply-chain-audit:` at :1093, keeping one blank line on each
side. The `KUBECTL_VERSION` / `KUBECTL_SHA256` values are K0's (Step 1) and
are byte-identical to the `test` job's step; the pin test holds all three
places together.

```yaml
  # ===========================================================================
  # Kubernetes bundle: render the base and every overlay under
  # deploy/kubernetes/ with the checksum-pinned kubectl, then run the
  # rendered-manifest contract test with the fail-loud switch set. There is no
  # clusterless admission step on purpose: `kubectl apply --dry-run=client`
  # still performs REST-mapper discovery and exits 1 with "connection refused"
  # without an API server (measured 2026-09-13 with v1.37.0), so server
  # admission of these objects is proven by the kubernetes-kind job's
  # `kubectl apply -k` instead. The kubectl pin is the one
  # docs/plans/2026-09-13-kubernetes-platform-facts.md records and the `test`
  # job installs; tests/unit/deployment/test_kubernetes_bundle.py binds every
  # lane and the facts document to it.
  # No `needs: [static-analysis]` (operator ruling 2026-09-05,
  # elspeth-d8749aeaa3): ci-success still requires static-analysis, so the
  # trust-tier red still blocks the merge without hiding this verdict.
  # ===========================================================================
  kubernetes-render:
    name: Kubernetes bundle render
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    steps:
      - name: Checkout code
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1

      - name: Install kubectl (pinned)
        run: |
          KUBECTL_VERSION=1.37.0
          KUBECTL_SHA256=6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f
          curl -fsSLo /tmp/kubectl \
            "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
          printf '%s  /tmp/kubectl\n' "$KUBECTL_SHA256" | sha256sum -c -
          sudo install -m 0755 /tmp/kubectl /usr/local/bin/kubectl
          kubectl version --client

      - name: Render the base and every overlay
        # The loop picks up every kustomization under deploy/kubernetes/
        # (the base now; the kind-test, kind-acceptance and aks overlays as
        # later tasks add them) so an overlay that stops rendering offline
        # reds this job without anyone editing it.
        run: |
          set -euo pipefail
          mkdir -p /tmp/render
          kubectl kustomize deploy/kubernetes/base > /tmp/render/base.yaml
          echo "deploy/kubernetes/base: $(grep -c '^kind:' /tmp/render/base.yaml) objects"
          mapfile -t dirs < <(find deploy/kubernetes -name kustomization.yaml -printf '%h\n' | sort)
          for dir in "${dirs[@]}"; do
            out="/tmp/render/${dir//\//_}.yaml"
            kubectl kustomize "$dir" > "$out"
            echo "$dir: $(grep -c '^kind:' "$out") objects"
          done

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

      - name: Run the rendered-manifest bundle contract test
        # ELSPETH_CI_KUBECTL_REQUIRED turns _require_kubectl's skip into a
        # failure on this runner (GITHUB_ACTIONS already does; the switch is
        # what an operator sets to reproduce the fail-loud run locally).
        env:
          ELSPETH_CI_KUBECTL_REQUIRED: "1"
        run: uv run pytest tests/unit/deployment/test_kubernetes_bundle.py -v -n 0
```

- [ ] **Step 6: Gate `ci-success` on the job by name.**

Two edits in the `ci-success` job (`ci.yaml:1332-1385`). First, the `needs`
list at :1335-1344 gains one entry after `- azure-container-apps-bicep`
(:1341):

```yaml
    needs:
      - static-analysis
      - test
      - testcontainer
      - host-runner-unit
      - state-engine-validation
      - azure-container-apps-bicep
      - kubernetes-render
      - supply-chain-audit
      - e2e-frontend
      - frontend-unit
```

Second, the `Check all jobs passed` script gains one block directly after the
Bicep block (:1369-1372) and before the `supply-chain-audit` block (:1373):

```yaml
          if [[ "${{ needs.azure-container-apps-bicep.result }}" != "success" ]]; then
            echo "Azure Container Apps Bicep bundle job failed"
            exit 1
          fi
          if [[ "${{ needs.kubernetes-render.result }}" != "success" ]]; then
            echo "Kubernetes bundle render job failed"
            exit 1
          fi
          if [[ "${{ needs.supply-chain-audit.result }}" != "success" ]]; then
            echo "Dependency/license audit job failed"
            exit 1
          fi
```

`ci-success` is `if: always()`, so without the second edit a red
`kubernetes-render` would leave CI Success green; `_assert_render_gate` and
the `result-check-dropped` mutation pin exactly that.

- [ ] **Step 7: Run the pin tests and every sibling that pins `ci.yaml`.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/deployment/test_kubernetes_bundle.py -n 0 -k "_ci_ or _red_" > /tmp/klane-k3-step7a.log 2>&1; echo exit=$?; tail -3 /tmp/klane-k3-step7a.log`
Expected: `exit=0`, `12 passed` (K3's 11 ids plus K1's
`test_ci_test_job_installs_the_pinned_kubectl`, which `_ci_` also selects).

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/test_ci_workflow_xdist.py tests/unit/cicd/test_state_engine_ci_selection.py tests/unit/deployment/test_azure_container_apps_bundle.py -n 0 > /tmp/klane-k3-step7b.log 2>&1; echo exit=$?; tail -3 /tmp/klane-k3-step7b.log`
Expected: `exit=0` with a `passed` count and a `skipped` count and no
`failed`. The ACA ids that compile Bicep skip locally (`_require_bicep`,
`test_azure_container_apps_bundle.py:80-91`); the two ACA CI-pin ids
(`:509-545`) and every xdist / state-engine pin run and pass, which proves the
new job and the two `ci-success` edits cascade into no existing pin (measured
on HEAD 072141b75 before this task's edits: the same command exits 0).

- [ ] **Step 8: Lint the edited workflow against the Step 4 baseline.**

Run: `cd "$(git rev-parse --show-toplevel)" && docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.12 -no-color .github/workflows/ci.yaml > /tmp/klane-k3-actionlint-after.log 2>&1; echo exit=$?; diff /tmp/klane-k3-actionlint-before.log /tmp/klane-k3-actionlint-after.log; echo diff=$?`
Expected: `diff=0` with no diff output — the new job and the two `ci-success`
edits add no finding. Any line that appears only in the `after` log is a
defect in Step 5 or 6 (an unquoted `${{ }}`, a bad step key, a shell error
`shellcheck` sees in the render loop); fix it and re-run this step.

- [ ] **Step 9: Prove the job's commands locally with the pinned binary (positive and negative control).**

Download the pinned `kubectl` into the lane's gitignored bin, verify the sha
the way the job does, and run the render loop verbatim:

```bash
cd "$(git rev-parse --show-toplevel)" && mkdir -p .claude/lanes/k3/bin \
  && curl -fsSLo .claude/lanes/k3/bin/kubectl "https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl" \
  && printf '%s  .claude/lanes/k3/bin/kubectl\n' 6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f | sha256sum -c - \
  && chmod 0755 .claude/lanes/k3/bin/kubectl \
  && .claude/lanes/k3/bin/kubectl version --client
```

Expected: `.claude/lanes/k3/bin/kubectl: OK` and `Client Version: v1.37.0`.

```bash
cd "$(git rev-parse --show-toplevel)" && PATH="$PWD/.claude/lanes/k3/bin:$PATH" bash -euo pipefail -c '
mkdir -p /tmp/klane-k3-render
kubectl kustomize deploy/kubernetes/base > /tmp/klane-k3-render/base.yaml
echo "deploy/kubernetes/base: $(grep -c "^kind:" /tmp/klane-k3-render/base.yaml) objects"
mapfile -t dirs < <(find deploy/kubernetes -name kustomization.yaml -printf "%h\n" | sort)
for dir in "${dirs[@]}"; do
  out="/tmp/klane-k3-render/${dir//\//_}.yaml"
  kubectl kustomize "$dir" > "$out"
  echo "$dir: $(grep -c "^kind:" "$out") objects"
done' > /tmp/klane-k3-step9-render.log 2>&1; echo exit=$?; cat /tmp/klane-k3-step9-render.log
```

Expected: `exit=0` and two lines both reading `deploy/kubernetes/base: 6
objects` (the explicit render and the loop's visit of the base; K1's
`test_base_renders_exactly_the_supported_resource_kinds` pins those six).

Positive control — the whole bundle file with the switch set and the pin on
`PATH`, exactly the job's pytest step:

Run: `cd "$(git rev-parse --show-toplevel)" && PATH="$PWD/.claude/lanes/k3/bin:$PATH" ELSPETH_CI_KUBECTL_REQUIRED=1 pytest tests/unit/deployment/test_kubernetes_bundle.py -n 0 > /tmp/klane-k3-step9-bundle.log 2>&1; echo exit=$?; tail -3 /tmp/klane-k3-step9-bundle.log`
Expected: `exit=0`, no `skipped` in the summary line (every K1 render test
ran against the real binary).

Negative control — the switch with the binary absent must FAIL, not skip
(this is what the job relies on):

Run: `cd "$(git rev-parse --show-toplevel)" && PATH="$PWD/.venv/bin:/usr/bin:/bin" ELSPETH_CI_KUBECTL_REQUIRED=1 pytest tests/unit/deployment/test_kubernetes_bundle.py -n 0 -k base_renders_exactly > /tmp/klane-k3-step9-negative.log 2>&1; echo exit=$?; grep -E "FAILED|kubectl" /tmp/klane-k3-step9-negative.log | head -3`
Expected: `exit=1` and a `FAILED` line raised by K1's `_require_kubectl`
`pytest.fail`, whatever its text; the discriminator is `FAILED` versus
`skipped`. If it prints `1 skipped`, K1's switch is not honoured and this job
would skip its only contract test in CI — stop and report against K1.

- [ ] **Step 10: Lint, branch safety, then commit by pathspec.**

Run: `cd "$(git rev-parse --show-toplevel)" && ruff check tests/unit/deployment/test_kubernetes_bundle.py && ruff format --check tests/unit/deployment/test_kubernetes_bundle.py; echo exit=$?`
Expected: `exit=0`. The `I` (isort) rule is on (`pyproject.toml:297`), so the
`from collections.abc import Callable` line must sit in the stdlib group
between `import subprocess` and `from pathlib import Path`; the code blocks in
this task were run through `ruff check` and `ruff format --check` with the
repo config in that order and were clean. `tests/` is outside the mypy strict
gate (`pyproject.toml:355-357`), so the bare `dict` annotations in the
checkers are not gated.

Neither file is created by this task (K1 committed the test module), so there
is no `git add -N`; both are still staged by name before the safety check,
because `scripts/branch-safety-check.sh` inspects the staged set.
`.claude/lanes/k3/` is gitignored and must not appear in the staged set.

```bash
cd "$(git rev-parse --show-toplevel)" && git add -- .github/workflows/ci.yaml tests/unit/deployment/test_kubernetes_bundle.py
git status --short
scripts/branch-safety-check.sh --intent commit
git commit -m "ci: checksum-pinned Kubernetes render gate" -- .github/workflows/ci.yaml tests/unit/deployment/test_kubernetes_bundle.py
git show --stat HEAD
```

Expected: `git status --short` shows `M  .github/workflows/ci.yaml` and `M  tests/unit/deployment/test_kubernetes_bundle.py` staged (plus whatever a sibling lane has left unstaged, which this commit must not sweep in); the safety check prints no `[FAIL]` line and exits 0 (on a FAIL, stop before the commit); `git show --stat HEAD` ends with exactly `2 files changed`. Any other count: `git reset --mixed HEAD~1`, restage the two paths, and commit again by the same pathspec.
