### Task K2: A real startup profile for the `kubernetes` target

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: K1. Runs before: K3. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

**Files:**
- Modify: `src/elspeth/web/deployment_profiles.py:228` (the `"kubernetes": _external_state_profile("kubernetes")` arm), `:29-35` (module docstring, the sentences from "Azure Container Apps additionally sets" to "so its profile names no variable."), `:108-111` (`PlatformIdentity` docstring: "every target except Azure Container Apps today")
- Modify: `src/elspeth/web/app.py:2144-2147` (the `/api/system/status` comment that says the platform-stamped names are "null elsewhere")
- Test: `tests/unit/web/test_deployment_profiles.py:85-89` (family map — stays `"kubernetes": "external-state"`, unchanged), `:219-228` (`TestPlatformIdentity` docstring and `test_only_azure_container_apps_names_platform_identity_variables`, renamed and widened), `:245-249` (gains its Kubernetes partner), `:318-329` (`TestProfileType` equality rebuilt with the three new field values)
- Test: `tests/unit/web/coordination/test_membership_authority.py:188-193` (`test_other_targets_register_the_package_version` uses `kubernetes` as its package-identity example; it turns red the moment the arm lands)
- Test: `tests/testcontainer/web/test_external_deployment_postgres.py:240-248` (the autouse environment fixture models only the Container Apps variables), `:280-282` (`_settings` injects `operator_telemetry_release` only for `azure-container-apps`; the `kubernetes` cases at `:386`, `:462` and `:470` boot `create_app` against real PostgreSQL and reach the arm through `app.py:1691`)

**Interfaces:**
- Consumes: the env var names `ELSPETH_K8S_POD_NAME` (downward API `metadata.name`) and `ELSPETH_K8S_REVISION` (downward API `metadata.annotations['elspeth.io/revision']`) declared in Task K1's `deploy/kubernetes/base/deployment.yaml`, and the ConfigMap key `ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE` from K1's `configmap.yaml`. On HEAD: `DeploymentStartupProfile` (`deployment_profiles.py:118-132`, frozen dataclass with `membership_identity_source: MembershipIdentitySource = "package"`), the existing `"platform-revision"` arm of `membership_identity` (`:149-155`), `read_platform_identity(profile, environ=None) -> PlatformIdentity` (`:272`), `PlatformIdentityError` (`:92`), and `web_instance_identity_from_settings(settings, *, instance_id) -> WebInstanceIdentity` (`coordination/membership_authority.py:132`).
- Produces:
  - `deployment_startup_profile("kubernetes") == DeploymentStartupProfile(target="kubernetes", contract_family="external-state", display_name="External-state", doctor_command="elspeth doctor deployment", replica_identity_env_var="ELSPETH_K8S_POD_NAME", revision_env_var="ELSPETH_K8S_REVISION", membership_identity_source="platform-revision")`.
  - For a `kubernetes` settings object, `web_instance_identity_from_settings(settings, instance_id=<minted id>)` returns a `WebInstanceIdentity` whose `deployment_generation` and `revision_label` both equal `$ELSPETH_K8S_REVISION` and whose `image_digest` equals `settings.operator_telemetry_release` (`instance_id`, `deployment_target` and `compatibility_key` are unchanged from HEAD). The profile-level field is `DeploymentMembershipIdentity.image_identity` (`deployment_profiles.py:88`); it lands in the `web_instances.image_digest` column via `membership_authority.py:146`. Task K4 asserts `{r.deployment_generation for r in rows} == {"sha-kindtest"}` on exactly this.
  - `GET /api/system/status` (`app.py:2148-2149`) reports `deployment_revision == $ELSPETH_K8S_REVISION` and `deployment_replica == $ELSPETH_K8S_POD_NAME` for a Kubernetes pod (they stay `null` for `default`, `docker-compose`, `linux-systemd` and `aws-ecs`). Task K4 reads this to prove the overlay replaced K1's placeholder.
  - Boot refusals a Kubernetes replica now carries: `ELSPETH_K8S_REVISION` absent → `ValueError("kubernetes membership identity requires the deployment contract to carry platform revision")`; `operator_telemetry_release` unset → `ValueError("kubernetes membership identity requires the deployment contract to carry operator_telemetry_release")` (revision is checked first, `deployment_profiles.py:150` before `:153`); a present-but-malformed `ELSPETH_K8S_*` value → `PlatformIdentityError` naming the variable (`:265-268`). K1's placeholder `REPLACE_PER_ROLLOUT` passes both `_PLATFORM_IDENTITY_VALUE` and `is_release_identity` (measured 2026-09-13) and is deliberately NOT refused by this profile: K1's `test_base_carries_both_rollout_placeholders` pins its presence in the base and K4 proves the overlay replaced it.

- [ ] **Step 1: Write the failing profile tests.** Four edits to `tests/unit/web/test_deployment_profiles.py`. The family map at `:82-89` is left exactly as it is (`"kubernetes": "external-state"`).

(a) Replace `:219-228` (the `TestPlatformIdentity` class line, its docstring and the first test) with:

```python
class TestPlatformIdentity:
    """The ACA and Kubernetes profiles name their platform's replica/revision variables; nothing else does."""

    def test_only_platform_stamped_targets_name_identity_variables(self) -> None:
        named = {
            target: (profile.revision_env_var, profile.replica_identity_env_var)
            for target, profile in DEPLOYMENT_STARTUP_PROFILES.items()
            if profile.revision_env_var is not None or profile.replica_identity_env_var is not None
        }
        assert named == {
            "azure-container-apps": ("CONTAINER_APP_REVISION", "CONTAINER_APP_REPLICA_NAME"),
            "kubernetes": ("ELSPETH_K8S_REVISION", "ELSPETH_K8S_POD_NAME"),
        }
```

(b) Insert directly after `test_other_targets_ignore_the_container_apps_variables` (`:245-249`, which keeps passing: the Kubernetes profile names different variables) its partner plus the Kubernetes read tests:

```python
    @pytest.mark.parametrize("target", [target for target in _TARGETS if target != "kubernetes"])
    def test_other_targets_ignore_the_kubernetes_variables(self, target: DeploymentTarget) -> None:
        """An ACA, ECS or local process with stray ELSPETH_K8S_* variables reports no platform identity."""
        environ = {"ELSPETH_K8S_REVISION": "stray", "ELSPETH_K8S_POD_NAME": "stray"}
        assert read_platform_identity(deployment_startup_profile(target), environ) == PlatformIdentity(revision=None, replica=None)

    def test_kubernetes_present_variables_are_read_verbatim(self) -> None:
        profile = deployment_startup_profile("kubernetes")
        environ = {
            "ELSPETH_K8S_REVISION": "sha-1a2b3c4",
            "ELSPETH_K8S_POD_NAME": "elspeth-web-7d9f8c6b5-xk2pq",
        }
        assert read_platform_identity(profile, environ) == PlatformIdentity(
            revision="sha-1a2b3c4",
            replica="elspeth-web-7d9f8c6b5-xk2pq",
        )

    def test_kubernetes_ignores_the_container_apps_variables(self) -> None:
        profile = deployment_startup_profile("kubernetes")
        environ = {"CONTAINER_APP_REVISION": "stray", "CONTAINER_APP_REPLICA_NAME": "stray"}
        assert read_platform_identity(profile, environ) == PlatformIdentity(revision=None, replica=None)

    @pytest.mark.parametrize(
        "malformed",
        [
            pytest.param("", id="blank"),
            pytest.param("   ", id="whitespace"),
            pytest.param("rev\r\nX-Injected: 1", id="crlf"),
            pytest.param("-leading-dash", id="leading-dash"),
            pytest.param("has space", id="space"),
            pytest.param("a" * (INSTANCE_ID_MAX_LENGTH + 1), id="too-long"),
            pytest.param("tab\there", id="tab"),
            pytest.param("ünïcode", id="non-ascii"),
        ],
    )
    def test_kubernetes_malformed_platform_value_refuses_the_boot(self, malformed: str) -> None:
        """A downward-API projection that fails the allow-list is a platform-contract failure, never a silent null."""
        profile = deployment_startup_profile("kubernetes")
        with pytest.raises(PlatformIdentityError, match="ELSPETH_K8S_POD_NAME"):
            read_platform_identity(profile, {"ELSPETH_K8S_POD_NAME": malformed})
        with pytest.raises(PlatformIdentityError, match="ELSPETH_K8S_REVISION"):
            read_platform_identity(profile, {"ELSPETH_K8S_REVISION": malformed})
```

(c) Replace `:318-329` (`TestProfileType`) with the profile the arm will carry:

```python
class TestProfileType:
    def test_profile_is_a_plain_frozen_dataclass_of_facts(self) -> None:
        """No callables are stored on the profile: the hooks are methods that dereference the modules."""
        profile = DeploymentStartupProfile(
            target="kubernetes",
            contract_family="external-state",
            display_name="External-state",
            doctor_command="elspeth doctor deployment",
            replica_identity_env_var="ELSPETH_K8S_POD_NAME",
            revision_env_var="ELSPETH_K8S_REVISION",
            membership_identity_source="platform-revision",
        )
        assert profile == deployment_startup_profile("kubernetes")
```

(d) Append at the end of the file:

```python
class TestKubernetesMembershipIdentity:
    """The Kubernetes profile binds the membership row to what the downward API stamped, never to the package version."""

    def test_kubernetes_profile_reads_platform_identity_from_the_downward_api(self) -> None:
        profile = deployment_startup_profile("kubernetes")
        assert profile.contract_family == "external-state"
        assert profile.doctor_command == "elspeth doctor deployment"
        assert profile.replica_identity_env_var == "ELSPETH_K8S_POD_NAME"
        assert profile.revision_env_var == "ELSPETH_K8S_REVISION"
        assert profile.membership_identity_source == "platform-revision"

    def test_kubernetes_membership_identity_binds_revision_and_release(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ELSPETH_K8S_POD_NAME", "elspeth-web-7d9f8c6b5-xk2pq")
        monkeypatch.setenv("ELSPETH_K8S_REVISION", "sha-1a2b3c4")
        settings = _settings(tmp_path, deployment_target="kubernetes", operator_telemetry_release="0.8.1+1a2b3c4")
        identity = deployment_startup_profile("kubernetes").membership_identity(settings)
        assert identity.generation == "sha-1a2b3c4"
        assert identity.revision == "sha-1a2b3c4"
        assert identity.image_identity == "0.8.1+1a2b3c4"

    def test_kubernetes_membership_identity_refuses_when_the_platform_stamps_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pod without the revision projection must not register under the package version."""
        monkeypatch.delenv("ELSPETH_K8S_REVISION", raising=False)
        settings = _settings(tmp_path, deployment_target="kubernetes", operator_telemetry_release="0.8.1")
        with pytest.raises(ValueError, match="kubernetes membership identity requires the deployment contract to carry platform revision"):
            deployment_startup_profile("kubernetes").membership_identity(settings)

    def test_kubernetes_membership_identity_refuses_without_a_release(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ConfigMap's ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE is part of the contract, not an optional label."""
        monkeypatch.setenv("ELSPETH_K8S_REVISION", "sha-1a2b3c4")
        settings = _settings(tmp_path, deployment_target="kubernetes")
        with pytest.raises(ValueError, match="kubernetes membership identity requires the deployment contract to carry operator_telemetry_release"):
            deployment_startup_profile("kubernetes").membership_identity(settings)
```

The `_settings` helper is the one already at `:41-51` of this file (no `secret_key`: the `WebSettings` guards at `config.py:1230-1272` relax inside pytest).

- [ ] **Step 2: Retarget the membership-authority pin and add the Kubernetes tests.** In `tests/unit/web/coordination/test_membership_authority.py` replace `:188-193` (`test_other_targets_register_the_package_version`, whose example target is `kubernetes`) with the three package-source targets and the two Kubernetes tests that mirror the ACA pair at `:134-154`:

```python
    def test_kubernetes_registers_the_downward_api_revision_and_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ELSPETH_K8S_REVISION", "sha-1a2b3c4")
        identity = web_instance_identity_from_settings(
            _settings(deployment_target="kubernetes", operator_telemetry_release="0.8.1+1a2b3c4"),
            instance_id="replica-123",
        )
        assert identity.deployment_target == "kubernetes"
        assert identity.deployment_generation == "sha-1a2b3c4"
        assert identity.revision_label == "sha-1a2b3c4"
        assert identity.image_digest == "0.8.1+1a2b3c4"

    @pytest.mark.parametrize("missing", ["revision", "release"])
    def test_kubernetes_requires_deployment_identity(self, missing: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ELSPETH_K8S_REVISION", raising=False)
        if missing != "revision":
            monkeypatch.setenv("ELSPETH_K8S_REVISION", "sha-1a2b3c4")
        settings = _settings(
            deployment_target="kubernetes",
            operator_telemetry_release=None if missing == "release" else "0.8.1+1a2b3c4",
        )
        with pytest.raises(ValueError, match="kubernetes membership identity requires"):
            web_instance_identity_from_settings(settings, instance_id="replica-123")

    @pytest.mark.parametrize("target", ["default", "docker-compose", "linux-systemd"])
    def test_package_targets_register_the_package_version(self, target: str) -> None:
        from elspeth import __version__

        identity = web_instance_identity_from_settings(_settings(deployment_target=target), instance_id="postgresql-abc")
        assert identity.deployment_target == target
        assert identity.deployment_generation == identity.image_digest == identity.revision_label == f"elspeth-{__version__}"
```

`_settings` here is the module's own helper at `:120-130` (it sets `secret_key`; measured 2026-09-13: all three package-source targets construct through it).

- [ ] **Step 3: Run both files to verify the failures.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/test_deployment_profiles.py tests/unit/web/coordination/test_membership_authority.py -n 0 > /tmp/k8s-lane-k2-fail.log 2>&1; echo exit=$?; grep -E "^(FAILED|E   )" /tmp/k8s-lane-k2-fail.log`
Expected: exit 1 with exactly these 18 `FAILED` ids (the file-level baseline on HEAD is 137 passed, measured 2026-09-13):
- `TestPlatformIdentity::test_only_platform_stamped_targets_name_identity_variables` — `AssertionError` showing the one-entry dict `{'azure-container-apps': ('CONTAINER_APP_REVISION', 'CONTAINER_APP_REPLICA_NAME')}` against the expected two entries.
- `TestPlatformIdentity::test_kubernetes_present_variables_are_read_verbatim` — `assert PlatformIdentity(revision=None, replica=None) == PlatformIdentity(revision='sha-1a2b3c4', replica='elspeth-web-7d9f8c6b5-xk2pq')`.
- `TestPlatformIdentity::test_kubernetes_malformed_platform_value_refuses_the_boot` × 8 ids (`blank`, `whitespace`, `crlf`, `leading-dash`, `space`, `too-long`, `tab`, `non-ascii`) — `Failed: DID NOT RAISE <class 'elspeth.web.deployment_profiles.PlatformIdentityError'>`.
- `TestProfileType::test_profile_is_a_plain_frozen_dataclass_of_facts` — `AssertionError` on the three differing fields (`replica_identity_env_var`, `revision_env_var`, `membership_identity_source`).
- `TestKubernetesMembershipIdentity::test_kubernetes_profile_reads_platform_identity_from_the_downward_api` — `assert None == 'ELSPETH_K8S_POD_NAME'`.
- `TestKubernetesMembershipIdentity::test_kubernetes_membership_identity_binds_revision_and_release` — `AssertionError: assert 'elspeth-0.8.1' == 'sha-1a2b3c4'`.
- `TestKubernetesMembershipIdentity::test_kubernetes_membership_identity_refuses_when_the_platform_stamps_nothing` and `TestKubernetesMembershipIdentity::test_kubernetes_membership_identity_refuses_without_a_release` — `Failed: DID NOT RAISE <class 'ValueError'>`.
- `TestIdentityFromSettings::test_kubernetes_registers_the_downward_api_revision_and_release` — `AssertionError: assert 'elspeth-0.8.1' == 'sha-1a2b3c4'`.
- `TestIdentityFromSettings::test_kubernetes_requires_deployment_identity[revision]` and `[release]` — `Failed: DID NOT RAISE <class 'ValueError'>`.

`test_other_targets_ignore_the_kubernetes_variables` (5 ids), `test_kubernetes_ignores_the_container_apps_variables` and `test_package_targets_register_the_package_version` (3 ids) pass on HEAD and must still pass after Step 4; they are the mutation partners that go red if another profile is ever handed the Kubernetes variable names or if the package arm is disturbed.

- [ ] **Step 4: Replace the arm and the three sentences it falsifies.**

`src/elspeth/web/deployment_profiles.py:228` — replace the single line `"kubernetes": _external_state_profile("kubernetes"),` with:

```python
        "kubernetes": DeploymentStartupProfile(
            target="kubernetes",
            contract_family="external-state",
            display_name="External-state",
            doctor_command="elspeth doctor deployment",
            # Downward API projections declared in deploy/kubernetes/base/deployment.yaml:
            # metadata.name and the per-rollout elspeth.io/revision pod annotation.
            replica_identity_env_var="ELSPETH_K8S_POD_NAME",
            revision_env_var="ELSPETH_K8S_REVISION",
            membership_identity_source="platform-revision",
        ),
```

`src/elspeth/web/deployment_profiles.py:29-35` — replace lines 29-35 in full (from `and reported by ``/api/system/status``. Azure Container Apps additionally` through `(``_aws_ecs_acceptance/ecs_metadata.py``) — so its profile names no variable.`) with these twelve lines:

```text
and reported by ``/api/system/status``. Two platforms additionally stamp
replica identity into the environment: Azure Container Apps sets
``CONTAINER_APP_REPLICA_NAME`` and ``CONTAINER_APP_REVISION`` on every
replica, and the Kubernetes base projects ``ELSPETH_K8S_POD_NAME``
(``metadata.name``) and ``ELSPETH_K8S_REVISION`` (the per-rollout
``elspeth.io/revision`` pod annotation) through the downward API
(``deploy/kubernetes/base/deployment.yaml``). Each of those profiles names
its pair and :func:`read_platform_identity` parses them as a Tier-3
boundary (bounded, allow-listed, fail-closed on a malformed value). AWS ECS
does not publish identity through the environment — the acceptance harness
reads the task metadata endpoint (``_aws_ecs_acceptance/ecs_metadata.py``)
— so its profile names no variable.
```

`src/elspeth/web/deployment_profiles.py:108-111` — replace the `PlatformIdentity` docstring body with:

```text
    ``revision`` and ``replica`` are ``None`` when the profile names no
    variable for them (every target except Azure Container Apps and
    Kubernetes) or the named variable is absent from the environment (an
    ACA- or Kubernetes-targeted process booted outside its platform, such as
    the unit suite).
```

`src/elspeth/web/app.py:2144-2147` — replace the four comment lines with:

```python
            # Platform-stamped revision/replica names when the target's
            # platform publishes them through the environment (Azure
            # Container Apps: CONTAINER_APP_REVISION / CONTAINER_APP_REPLICA_NAME;
            # Kubernetes: ELSPETH_K8S_REVISION / ELSPETH_K8S_POD_NAME via the
            # downward API); null elsewhere.
```

Task K8's later edit to the same module docstring is the public-claim wording (the BYO sentence flip) and rebases onto this change; K2 owns only the sentences the arm makes false.

- [ ] **Step 5: Run the profile, membership, contract and status-wiring suites.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/test_deployment_profiles.py tests/unit/web/coordination/test_membership_authority.py tests/unit/web/test_deployment_contract.py tests/unit/web/test_instance_identity_wiring.py -n 0 > /tmp/k8s-lane-k2-unit.log 2>&1; echo exit=$?; tail -5 /tmp/k8s-lane-k2-unit.log`
Expected: exit 0. `test_instance_identity_wiring.py:57-60` still sees `deployment_revision` / `deployment_replica` as `null` because its app boots the `default` target; `test_deployment_contract.py` is untouched by this task and pins that `kubernetes` stays in `EXTERNAL_POSTGRESQL_TARGETS` (`:73`).

- [ ] **Step 6: Run the PostgreSQL contract suite on the Step 4 tree and watch the Kubernetes cases refuse to boot.** The arm is reached in production only through `app.py:1691` (`web_instance_identity_from_settings` under a PostgreSQL session engine); this run is the proof that a real `create_app` on PostgreSQL derives the membership row through the new arm.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/testcontainer/web/test_external_deployment_postgres.py -m testcontainer -n 0 > /tmp/k8s-lane-k2-tc-fail.log 2>&1; echo exit=$?; grep -E "^(FAILED|E   )" /tmp/k8s-lane-k2-tc-fail.log`
Expected: exit 1, `3 failed, 8 passed` (Docker required; the file's baseline on HEAD is `11 passed`, exit 0, about 35 s, measured 2026-09-13; the autouse fixture at `:240-248` sets only the Container Apps variables). Exactly the three ids that boot `create_app` with `target="kubernetes"` fail, each at `create_app(settings)` with `ValueError: kubernetes membership identity requires the deployment contract to carry platform revision` (revision is checked before the release, `deployment_profiles.py:150`): `test_external_target_doctor_initializes_then_runtime_stays_validate_only[kubernetes]`, `test_external_target_doctor_initializes_then_runtime_stays_validate_only[kubernetes-network-latency]`, `test_external_apps_share_all_web_rate_budgets`. Every other target's case passes. The `elspeth doctor deployment` subprocess cases do not fail: the doctor never derives a membership identity (`web_instance_identity_from_settings` has one production caller, `app.py:1691`).

- [ ] **Step 7: Model the downward API in the PostgreSQL contract suite.** Two edits to `tests/testcontainer/web/test_external_deployment_postgres.py`.

Replace `:245-248` (the comment and the two `setenv` calls inside `_clear_inherited_web_settings`) with:

```python
    # Model the identity each platform injects, including the revision its
    # PostgreSQL membership row is bound to: Container Apps sets the
    # CONTAINER_APP_* pair, the Kubernetes base projects the ELSPETH_K8S_*
    # pair through the downward API. Every other startup profile ignores both.
    monkeypatch.setenv("CONTAINER_APP_REVISION", "elspeth--postgres-contract")
    monkeypatch.setenv("CONTAINER_APP_REPLICA_NAME", "elspeth--postgres-contract-replica")
    monkeypatch.setenv("ELSPETH_K8S_REVISION", "sha-postgres-contract")
    monkeypatch.setenv("ELSPETH_K8S_POD_NAME", "elspeth-web-postgres-contract-pod")
```

Replace `:281-282` (the `azure-container-apps` release injection inside `_settings`) with:

```python
    if target in {"azure-container-apps", "kubernetes"}:
        # Both platform-revision profiles bind the membership row's image
        # identity to operator_telemetry_release (the "platform-revision" arm
        # of DeploymentStartupProfile.membership_identity).
        target_settings["operator_telemetry_release"] = "a" * 40
```

- [ ] **Step 8: Run the PostgreSQL contract suite again.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/testcontainer/web/test_external_deployment_postgres.py -m testcontainer -n 0 > /tmp/k8s-lane-k2-tc.log 2>&1; echo exit=$?; tail -5 /tmp/k8s-lane-k2-tc.log`
Expected: exit 0, `11 passed` (the HEAD baseline restored); the three Kubernetes ids from Step 6 now pass and `/api/ready` reports `ready: true` for the runtime-role boot.

- [ ] **Step 9: Commit.**

```bash
cd "$(git rev-parse --show-toplevel)" && git add -- src/elspeth/web/deployment_profiles.py src/elspeth/web/app.py tests/unit/web/test_deployment_profiles.py tests/unit/web/coordination/test_membership_authority.py tests/testcontainer/web/test_external_deployment_postgres.py
git status --short
scripts/branch-safety-check.sh --intent commit
git commit -m "feat(web): kubernetes startup profile reads downward-API replica identity" -- src/elspeth/web/deployment_profiles.py src/elspeth/web/app.py tests/unit/web/test_deployment_profiles.py tests/unit/web/coordination/test_membership_authority.py tests/testcontainer/web/test_external_deployment_postgres.py
git show --stat HEAD
```

This task creates no file, so there is no `git add -N`; the five modified files are staged by name before the safety check because the check inspects the staged set. `scripts/branch-safety-check.sh` must print no `[FAIL]` line and exit 0 before the commit runs (exit 1 on any FAIL: stop and fix the reported check). `git show --stat HEAD` must list exactly those five files; a sixth file means the shared index carried a sibling lane's edit: undo with `git reset --mixed HEAD~1` (never `checkout`, `restore` or `clean`) and recommit by the same pathspec.
