# Cross-Platform Deployment Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ELSPETH's Docker Compose, AWS ECS, Azure Container Apps, Kubernetes, and native Linux support explicit, deployable, and testable, including correct PostgreSQL client dependencies and database/storage wiring.

**Architecture:** Keep the ELSPETH image app-only and require both PostgreSQL client drivers in every external-PostgreSQL image profile. Add explicit deployment-target and state-mode settings, extract a provider-neutral external-state startup/doctor contract from the AWS implementation, ship one tracked bundle per supported platform, and enforce the support matrix through ordinary unit and release tests. Compose may own a PostgreSQL sidecar; AWS, Azure, and Kubernetes use external managed PostgreSQL; native Linux supports SQLite or external PostgreSQL. Every shipped web profile remains one process/replica.

**Tech Stack:** Python 3.13, Pydantic, SQLAlchemy, Typer, pytest, Testcontainers, Docker Compose, PostgreSQL 16, systemd, Azure Bicep, Kubernetes/Kustomize, JSON Schema, PyYAML, GitHub Actions.

---

## Approved Design

Implement against:
`docs/superpowers/specs/2026-07-24-cross-platform-deployment-contract-design.md`.

Do not add PostgreSQL server packages or a second process to the ELSPETH image.
Do not add a static AWS task definition; AWS deployments must continue cloning
the live task definition before changing the image revision. Do not raise the
web worker/replica count above one.

## File Map

### Runtime contract

- Modify `src/elspeth/web/config.py`: add deployment target/state-mode types and settings.
- Modify `src/elspeth/web/deployment_contract.py`: add state resolution and provider-neutral validation while preserving the AWS validator.
- Modify `src/elspeth/web/schema_probe.py`: give shared PostgreSQL pool settings a provider-neutral name with an AWS compatibility alias.
- Create `src/elspeth/web/external_state_startup.py`: provider-neutral mounted-directory and validate-only PostgreSQL startup gates.
- Modify `src/elspeth/web/aws_ecs_startup.py`: retain AWS-specific enforcement and compatibility exception behavior while delegating shared work.
- Modify `src/elspeth/web/app.py`: apply external-state startup and schema policy by resolved state mode.
- Modify `src/elspeth/web/landscape_access.py`: derive lazy-schema policy from resolved state mode.
- Modify `src/elspeth/web/readiness.py`: use explicit external URLs/paths for every external-state target.
- Modify `src/elspeth/web/doctor.py`: collect shared deployment checks and retain AWS-specific checks.
- Modify `src/elspeth/cli.py`: add `doctor deployment` and retain `doctor aws-ecs`.

### Tracked deployment artifacts

- Modify `.gitignore`: track support artifacts while ignoring real secrets and local overrides.
- Modify `docker-compose.yaml`: use the real image registry and require an explicit immutable/release tag.
- Create `deploy/compose/postgres.yaml`: PostgreSQL service and CLI wiring.
- Create `deploy/compose/web-postgres.yaml`: schema-init and single-process web services.
- Create `deploy/compose/postgres-init.sql`: create separate session and Landscape databases.
- Create `deploy/compose/.env.example`: variable names and safe example values only.
- Create `deploy/linux-systemd/elspeth-web.service`: portable single-process native service.
- Create `deploy/linux-systemd/elspeth-web.env.example`: portable settings surface.
- Create `deploy/azure-container-apps/main.bicep`: single-replica Container Apps workload.
- Create `deploy/azure-container-apps/main.example.bicepparam`: non-secret deployment parameter example.
- Create `deploy/kubernetes/base/kustomization.yaml`: Kustomize base inventory.
- Create `deploy/kubernetes/base/deployment.yaml`: single-replica web workload.
- Create `deploy/kubernetes/base/service.yaml`: cluster service.
- Create `deploy/kubernetes/base/configmap.yaml`: non-secret target/state configuration.
- Create `deploy/kubernetes/base/pvc.yaml`: persistent payload/data claim.
- Create `deploy/kubernetes/base/secret.example.yaml`: required secret key names with inert values.
- Create `deploy/platforms/schema.json`: deployment-profile schema.
- Create five `deploy/platforms/*.yaml` profiles: Compose, Linux, AWS, Azure, and Kubernetes.

### Tests and documentation

- Create `tests/unit/deployment/test_deploy_ignore_policy.py`.
- Create `tests/unit/deployment/test_platform_profiles.py`.
- Create `tests/unit/deployment/test_compose_bundle.py`.
- Create `tests/unit/deployment/test_linux_systemd_bundle.py`.
- Create `tests/unit/deployment/test_azure_container_apps_bundle.py`.
- Create `tests/unit/deployment/test_kubernetes_bundle.py`.
- Create `tests/unit/web/test_external_state_startup.py`.
- Modify existing config, deployment-contract, app, readiness, Landscape, doctor, CLI, image-release, and PostgreSQL testcontainer tests named in the tasks below.
- Create `docs/reference/deployment-platforms.md`: authoritative support matrix.
- Modify `README.md`, `docs/guides/docker.md`, `docs/reference/environment-variables.md`, `docs/repository-structure.md`, `docs/runbooks/index.md`, `docs/runbooks/aws-ecs-deployment.md`, `docs/runbooks/ansible-ubuntu-deployment.md`, and `CHANGELOG.md`.

The existing `deploy/elspeth-web.service` remains the live staging-specific
unit. This plan adds a portable unit beside it; it does not replace the live
service implicitly.

### Task 1: Make deployment artifacts trackable without exposing secrets

**Files:**
- Create: `tests/unit/deployment/test_deploy_ignore_policy.py`
- Modify: `.gitignore`

- [ ] **Step 1: Add a failing ignore-policy regression**

Use `git check-ignore --no-index --quiet` in a small helper and assert these
exact outcomes:

```python
@pytest.mark.parametrize(
    "path",
    [
        "deploy/platforms/aws-ecs.yaml",
        "deploy/compose/postgres.yaml",
        "deploy/linux-systemd/elspeth-web.service",
        "deploy/azure-container-apps/main.bicep",
        "deploy/kubernetes/base/deployment.yaml",
    ],
)
def test_shipped_deployment_artifacts_are_not_ignored(path: str) -> None:
    assert _is_ignored(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "deploy/elspeth-web.env",
        "deploy/compose/.env",
        "deploy/compose/operator.local.yaml",
        "deploy/kubernetes/base/secret.local.yaml",
        "deploy/Caddyfile",
        "deploy/elspeth-web.service.bak-20260724",
    ],
)
def test_local_deployment_secrets_and_overrides_stay_ignored(path: str) -> None:
    assert _is_ignored(path) is True


def test_example_environment_file_is_trackable() -> None:
    assert _is_ignored("deploy/compose/.env.example") is False
```

- [ ] **Step 2: Run the test and confirm the blanket ignore is red**

Run:

```bash
uv run --frozen pytest -q tests/unit/deployment/test_deploy_ignore_policy.py
```

Expected: shipped artifact assertions fail because `deploy/` is blanket
ignored.

- [ ] **Step 3: Replace the blanket rule with narrow local-state rules**

Remove `deploy/` and add:

```gitignore
# Deployment support artifacts are tracked. Only operator-local values and
# generated backups remain private.
deploy/**/*.env
!deploy/**/*.env.example
deploy/**/*.local.yaml
deploy/**/*.local.yml
deploy/**/*.bak-*
deploy/Caddyfile
```

- [ ] **Step 4: Re-run the focused regression**

Run:

```bash
uv run --frozen pytest -q tests/unit/deployment/test_deploy_ignore_policy.py
```

Expected: all cases pass.

- [ ] **Step 5: Commit**

```bash
git add .gitignore tests/unit/deployment/test_deploy_ignore_policy.py
git commit -m "build: track deployment support artifacts"
```

### Task 2: Add explicit deployment target and state-mode resolution

**Files:**
- Modify: `src/elspeth/web/config.py`
- Modify: `src/elspeth/web/deployment_contract.py`
- Modify: `tests/unit/web/test_config.py`
- Modify: `tests/unit/web/test_deployment_contract.py`

- [ ] **Step 1: Add failing configuration acceptance tests**

Add parameterized tests proving `WebSettings` accepts:

```python
def _web_settings_kwargs() -> dict[str, object]:
    return {
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": bytes(range(32)),
    }


@pytest.mark.parametrize(
    "target",
    [
        "default",
        "docker-compose",
        "linux-systemd",
        "aws-ecs",
        "azure-container-apps",
        "kubernetes",
    ],
)
def test_supported_deployment_target_is_accepted(target: str) -> None:
    settings = WebSettings(**(_web_settings_kwargs() | {"deployment_target": target}))
    assert settings.deployment_target == target


@pytest.mark.parametrize(
    "mode",
    ["auto", "sqlite-single", "external-postgresql"],
)
def test_supported_deployment_state_mode_is_accepted(mode: str) -> None:
    settings = WebSettings(**(_web_settings_kwargs() | {"deployment_state_mode": mode}))
    assert settings.deployment_state_mode == mode
```

Keep the existing rejection test for `azure-aca`; it must remain invalid
because the canonical value is `azure-container-apps`.

- [ ] **Step 2: Add the failing resolution matrix**

Import `resolve_deployment_state_mode` and cover these exact cases:

| Target | Configured mode | URL posture | Expected |
|---|---|---|---|
| `default` | `auto` | neither explicit | `sqlite-single` |
| `default` | `auto` | one explicit SQLite URL | `sqlite-single` |
| `default` | `auto` | two explicit SQLite URLs | `sqlite-single` |
| `docker-compose` | `auto` | neither explicit | `sqlite-single` |
| `linux-systemd` | `auto` | neither explicit | `sqlite-single` |
| `default` | `auto` | two PostgreSQL URLs | `external-postgresql` |
| `aws-ecs` | `auto` | two PostgreSQL URLs | `external-postgresql` |
| `azure-container-apps` | `auto` | two PostgreSQL URLs | `external-postgresql` |
| `kubernetes` | `auto` | two PostgreSQL URLs | `external-postgresql` |
| any supported target | explicit mode | matching URLs | the explicit mode |

Add negative tests proving:

- cloud/Kubernetes plus `sqlite-single` raises
  `DeploymentConfigurationError`;
- one explicit PostgreSQL URL under local `auto` raises after the missing
  side resolves to its SQLite default;
- a SQLite/PostgreSQL pair under `auto` raises;
- `sqlite-single` plus either PostgreSQL URL raises; and
- two URLs using a dialect other than SQLite or PostgreSQL raise rather than
  being reclassified as local SQLite.

- [ ] **Step 3: Run the focused tests and confirm they are red**

```bash
uv run --frozen pytest -q tests/unit/web/test_config.py tests/unit/web/test_deployment_contract.py
```

Expected: new target literals, the state setting, and resolver do not exist.

- [ ] **Step 4: Add the exact settings types**

In `config.py`, add:

```python
DeploymentTarget = Literal[
    "default",
    "docker-compose",
    "linux-systemd",
    "aws-ecs",
    "azure-container-apps",
    "kubernetes",
]
DeploymentStateMode = Literal["auto", "sqlite-single", "external-postgresql"]
```

Use them on:

```python
deployment_target: DeploymentTarget = "default"
deployment_state_mode: DeploymentStateMode = "auto"
```

- [ ] **Step 5: Implement pure, fail-closed state resolution**

In `deployment_contract.py`, add
`DeploymentConfigurationError`,
`EXTERNAL_POSTGRESQL_TARGETS`, and
`resolve_deployment_state_mode(settings: WebSettings)`. Its return annotation
is `Literal["sqlite-single", "external-postgresql"]`.

For cloud/Kubernetes targets, the implementation uses raw
`session_db_url` and `landscape_url` plus `model_fields_set`, because
both PostgreSQL URLs must be explicit. For local targets, resolve missing sides
through `get_session_db_url()` and `get_landscape_url()` so existing zero,
one, and two explicit SQLite configurations remain valid. Parse schemes with
SQLAlchemy's `make_url`. Error messages may name setting fields but must not
include raw URLs or credentials.

- [ ] **Step 6: Re-run, type-check, and commit**

```bash
uv run --frozen pytest -q tests/unit/web/test_config.py tests/unit/web/test_deployment_contract.py
uv run --frozen mypy src/elspeth/web/config.py src/elspeth/web/deployment_contract.py
git add src/elspeth/web/config.py src/elspeth/web/deployment_contract.py tests/unit/web/test_config.py tests/unit/web/test_deployment_contract.py
git commit -m "feat(web): model deployment target and state mode"
```

### Task 3: Extract the provider-neutral external-state startup contract

**Files:**
- Create: `src/elspeth/web/external_state_startup.py`
- Create: `tests/unit/web/test_external_state_startup.py`
- Modify: `src/elspeth/web/deployment_contract.py`
- Modify: `src/elspeth/web/schema_probe.py`
- Modify: `src/elspeth/web/aws_ecs_startup.py`
- Modify: `tests/unit/web/test_deployment_contract.py`
- Modify: `tests/unit/web/test_schema_probe.py`
- Modify: `tests/unit/web/test_aws_ecs_startup.py`

- [ ] **Step 1: Add failing shared-contract tests**

Add `validate_external_postgresql_settings(settings)` tests for every
external-capable target. Each complete settings object must pass checks named:

```python
[
    "deployment_target",
    "deployment_state_mode",
    "session_db_url",
    "landscape_url",
    "separate_db_targets",
    "data_dir",
    "payload_store_path",
    "host",
    "secret_key",
    "shareable_link_signing_key",
]
```

Add negative cases for SQLite URLs, a missing URL, the same logical database
through driver aliases, an implicit path, an unsupported target/mode pair, and
an unsafe secret. Explicitly reject `postgresql+asyncpg://` and
`postgresql+pg8000://`: the only accepted forms are bare `postgresql://`
(psycopg2), explicit `postgresql+psycopg2://`, and
`postgresql+psycopg://` (psycopg v3). Assert the raw driver fragment,
username, password, host, database name, and filesystem path do not appear in
any detail.

- [ ] **Step 2: Add failing provider-neutral startup tests**

Move/copy the behavior tests for mounted directories, retry budget, engine
disposal, and validate-only schema checks from
`test_aws_ecs_startup.py` into `test_external_state_startup.py`. Name the
new public API:

The new public API consists of
`ExternalStateStartupContractError`,
`ExternalStateSchemaNotReadyError`,
`enforce_external_state_contract(settings: WebSettings)`,
`require_runtime_directories_mounted(settings: WebSettings)`, and
`validate_only_schema_or_raise(settings: WebSettings, session_engine: Engine)`.

The tests must assert validate-only startup performs no DDL and that every
constructed engine is disposed on failure.

- [ ] **Step 3: Run the shared tests and confirm missing symbols**

```bash
uv run --frozen pytest -q tests/unit/web/test_deployment_contract.py tests/unit/web/test_schema_probe.py tests/unit/web/test_external_state_startup.py tests/unit/web/test_aws_ecs_startup.py
```

Expected: shared validator/startup imports fail before implementation.

- [ ] **Step 4: Extract common validation and startup behavior**

Implement shared checks in `deployment_contract.py`. Use
`require_distinct_postgres_targets()` for logical target separation and
convert its failure into a redacted `ContractCheck`.

Move generic filesystem and schema-probe behavior into
`external_state_startup.py`. Diagnostic guidance must be:

```text
Run 'elspeth doctor deployment' for full diagnostics.
```

Log event names become `external_state_schema_probe_retry`; attributes remain
bounded to label, attempt, elapsed seconds, and exception class.

Rename the pool constant to `EXTERNAL_POSTGRES_POOL_KWARGS` and keep
`AWS_ECS_POOL_KWARGS = EXTERNAL_POSTGRES_POOL_KWARGS` as a compatibility alias.
`postgres_engine_kwargs()` remains the one provider-neutral factory.

- [ ] **Step 5: Preserve AWS compatibility as a thin specialization**

`validate_aws_ecs_settings()` must compose the shared checks with the existing
AWS telemetry identity checks. `enforce_aws_ecs_contract()` must still raise
`AwsEcsStartupContractError`, and AWS schema failures must still be catchable
as `AwsEcsSchemaNotReadyError`. Keep compatibility exports in
`aws_ecs_startup.py` while delegating filesystem/schema mechanics to the new
module.

- [ ] **Step 6: Re-run, lint, and commit**

```bash
uv run --frozen pytest -q tests/unit/web/test_deployment_contract.py tests/unit/web/test_schema_probe.py tests/unit/web/test_external_state_startup.py tests/unit/web/test_aws_ecs_startup.py
uv run --frozen ruff check src/elspeth/web/deployment_contract.py src/elspeth/web/schema_probe.py src/elspeth/web/external_state_startup.py src/elspeth/web/aws_ecs_startup.py tests/unit/web/test_deployment_contract.py tests/unit/web/test_schema_probe.py tests/unit/web/test_external_state_startup.py tests/unit/web/test_aws_ecs_startup.py
git add src/elspeth/web/deployment_contract.py src/elspeth/web/schema_probe.py src/elspeth/web/external_state_startup.py src/elspeth/web/aws_ecs_startup.py tests/unit/web/test_deployment_contract.py tests/unit/web/test_schema_probe.py tests/unit/web/test_external_state_startup.py tests/unit/web/test_aws_ecs_startup.py
git commit -m "refactor(web): share external PostgreSQL startup contract"
```

### Task 4: Apply state-mode policy across application startup and readiness

**Files:**
- Modify: `src/elspeth/web/app.py`
- Modify: `src/elspeth/web/landscape_access.py`
- Modify: `src/elspeth/web/readiness.py`
- Modify: `src/elspeth/web/auth/audit.py`
- Modify: `tests/unit/web/test_app.py`
- Modify: `tests/unit/web/test_landscape_access.py`
- Modify: `tests/unit/web/test_readiness.py`
- Modify: `tests/unit/web/auth/test_audit.py`

- [ ] **Step 1: Add the failing state-policy matrix**

Parameterize schema creation with these exact expectations:

```python
[
    ("default", "sqlite-single", True),
    ("docker-compose", "sqlite-single", True),
    ("linux-systemd", "sqlite-single", True),
    ("default", "external-postgresql", False),
    ("docker-compose", "external-postgresql", False),
    ("linux-systemd", "external-postgresql", False),
    ("aws-ecs", "external-postgresql", False),
    ("azure-container-apps", "external-postgresql", False),
    ("kubernetes", "external-postgresql", False),
]
```

Cover `landscape_create_tables_allowed`, lifespan reconciliation,
`AuthAuditRecorder.from_settings`, and app startup. Invalid combinations must
fail before a URL is opened or a directory is created.

- [ ] **Step 2: Add failing readiness and engine-ownership tests**

For AWS, Azure, Kubernetes, external Compose, and external Linux, assert:

- raw explicit session/Landscape URLs are probed;
- explicit data/payload directories must already exist;
- the session engine is created once, disposed on all failed startup paths, and
  finalized on successful app teardown;
- missing or stale schemas fail without DDL; and
- the diagnostic names `doctor deployment`, except the AWS compatibility
  exception may additionally name `doctor aws-ecs`.

Keep SQLite tests proving local directories and schemas initialize as before.

- [ ] **Step 3: Run the focused tests and observe target-only branches fail**

```bash
uv run --frozen pytest -q tests/unit/web/test_app.py tests/unit/web/test_landscape_access.py tests/unit/web/test_readiness.py tests/unit/web/auth/test_audit.py
```

- [ ] **Step 4: Replace AWS-only state decisions with resolved-mode decisions**

Compute the resolved state mode once during `create_app()` and store it as:

```python
app.state.deployment_state_mode = resolved_state_mode
```

Use `external-postgresql` to select validate-only startup, explicit URLs,
existing mounted directories, PostgreSQL engine kwargs, readiness ownership,
and `create_tables=False`. Preserve AWS-only telemetry bootstrap and
acceptance logic behind `deployment_target == "aws-ecs"`.

- [ ] **Step 5: Run focused tests, type-check, and commit**

```bash
uv run --frozen pytest -q tests/unit/web/test_app.py tests/unit/web/test_landscape_access.py tests/unit/web/test_readiness.py tests/unit/web/auth/test_audit.py
uv run --frozen mypy src/elspeth/web/app.py src/elspeth/web/landscape_access.py src/elspeth/web/readiness.py src/elspeth/web/auth/audit.py
git add src/elspeth/web/app.py src/elspeth/web/landscape_access.py src/elspeth/web/readiness.py src/elspeth/web/auth/audit.py tests/unit/web/test_app.py tests/unit/web/test_landscape_access.py tests/unit/web/test_readiness.py tests/unit/web/auth/test_audit.py
git commit -m "feat(web): enforce external state by deployment mode"
```

### Task 5: Generalize deployment doctor while keeping the AWS command stable

**Files:**
- Modify: `src/elspeth/web/doctor.py`
- Modify: `src/elspeth/cli.py`
- Modify: `tests/unit/web/test_doctor.py`
- Modify: `tests/unit/cli/test_doctor_command.py`

- [ ] **Step 1: Add failing generic-doctor tests**

Add `collect_deployment_checks(settings, init_schema=False)` tests for
external Compose, Linux, AWS, Azure, and Kubernetes. Assert common check names
and schema behavior are identical. Add local SQLite coverage proving
`--init-schema` is rejected with a named `deployment_state_mode` check
rather than opening or replacing a local database.

- [ ] **Step 2: Add failing CLI rendering tests**

Invoke:

```python
runner.invoke(app, ["--no-dotenv", "doctor", "deployment", "--json"])
```

Prove exit 0/1 behavior, ordered JSON, text rendering, redacted settings-load
errors, and `--init-schema` propagation. Keep all existing
`doctor aws-ecs` tests.

- [ ] **Step 3: Run the doctor tests and confirm the command is absent**

```bash
uv run --frozen pytest -q tests/unit/web/test_doctor.py tests/unit/cli/test_doctor_command.py
```

- [ ] **Step 4: Implement a common command runner**

Extract the current rendering/error boundary into a private CLI helper used by
both commands. `doctor deployment` calls
`collect_deployment_checks`. `doctor aws-ecs` first requires target
`aws-ecs`, then calls the same collector with AWS-specific checks enabled.
Retain the existing bare ordered JSON schema and exit codes.

Split `plugin_and_dependency_checks` so the shared deployment doctor checks
both `psycopg` and `psycopg2`, while S3, Bedrock, boto3, ijson, AWS OTLP,
and guardrail checks run only for `aws-ecs`. Azure, Kubernetes, Compose, and
Linux lean images must not fail because AWS-only packages are absent.

- [ ] **Step 5: Re-run, lint, and commit**

```bash
uv run --frozen pytest -q tests/unit/web/test_doctor.py tests/unit/cli/test_doctor_command.py
uv run --frozen ruff check src/elspeth/web/doctor.py src/elspeth/cli.py tests/unit/web/test_doctor.py tests/unit/cli/test_doctor_command.py
git add src/elspeth/web/doctor.py src/elspeth/cli.py tests/unit/web/test_doctor.py tests/unit/cli/test_doctor_command.py
git commit -m "feat(cli): add provider-neutral deployment doctor"
```

### Task 6: Repair Docker Compose and ship a self-contained PostgreSQL web path

**Files:**
- Modify: `docker-compose.yaml`
- Create: `deploy/compose/postgres.yaml`
- Create: `deploy/compose/web-postgres.yaml`
- Create: `deploy/compose/postgres-init.sql`
- Create: `deploy/compose/.env.example`
- Create: `tests/unit/deployment/test_compose_bundle.py`
- Modify: `tests/unit/docs/test_docker_guide_release_examples.py`

- [ ] **Step 1: Add failing static Compose tests**

Use `yaml.safe_load` plus subprocess calls to assert:

- the base image is exactly
  `${REGISTRY:-ghcr.io/johnm-dta}/elspeth:${IMAGE_TAG:?set IMAGE_TAG to an immutable sha-* or v* tag}`;
- no Compose artifact contains a `latest` default;
- the PostgreSQL overlay uses `postgres:16-alpine`, a named data volume, a
  health check, and `postgres-init.sql`;
- initialization creates exactly `elspeth_sessions` and
  `elspeth_landscape`;
- `state-init` creates the data, blob, and payload directories under the
  named state volume with mode `0700`;
- CLI PostgreSQL mode sets `DATABASE_URL`;
- web mode sets deployment target `docker-compose`, state mode
  `external-postgresql`, both distinct web database URLs, data/payload paths,
  `deploy.replicas: 1`, one worker, a readiness health check, and
  persistent storage;
- `web-init` waits for healthy PostgreSQL and successful `state-init`, then
  runs `doctor deployment --init-schema`;
- `web` waits for successful `web-init`;
- the overlay passes
  `ELSPETH_WEB__COMPOSER_BOOT_PROBE_ENABLED` with a production default of
  `true`; and
- the overlay sets all four required non-secret composer settings:
  `ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS=15`,
  `ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS=10`,
  `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=85`, and
  `ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE=10`;
- `.env.example` instructs operators to generate `POSTGRES_PASSWORD` with
  `openssl rand -hex 24`, keeping the raw password safe to interpolate into
  both PostgreSQL URLs without ambiguous URI encoding; and
- `.env.example` contains variable names but no committed non-empty API key,
  database password, JWT secret, or signing key.

Run `docker compose config --quiet` for:

```bash
IMAGE_TAG=sha-test POSTGRES_PASSWORD=0123456789abcdef0123456789abcdef0123456789abcdef ELSPETH_WEB_SECRET_KEY=test-only-secret-key-with-more-than-32-bytes ELSPETH_WEB_SHAREABLE_LINK_SIGNING_KEY=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8= docker compose -f docker-compose.yaml -f deploy/compose/postgres.yaml -f deploy/compose/web-postgres.yaml config --quiet
```

- [ ] **Step 2: Run tests and confirm the current Compose contract is red**

```bash
uv run --frozen pytest -q tests/unit/deployment/test_compose_bundle.py tests/unit/docs/test_docker_guide_release_examples.py
```

Expected: registry/tag, database wiring, and missing overlays fail.

- [ ] **Step 3: Repair the base and add complete overlays**

Keep the root service CLI-first and SQLite by default. The PostgreSQL overlay
must not use a profile that can be enabled without rewiring ELSPETH. Use the
combined invocation shown above as the supported path.

The web service command is exactly:

```yaml
command: ["web", "--host", "0.0.0.0", "--port", "8451"]
```

The Compose health check uses the image's Python standard library to GET
`http://127.0.0.1:8451/api/ready`; it must not assume `curl` is installed.
The smoke test separately verifies both `/api/health` and `/api/ready`.

Set `WEB_CONCURRENCY: "1"`, publish port `8451`, and mount one named volume
once at `/app/state`. Set `ELSPETH_WEB__DATA_DIR=/app/state/data` and
`ELSPETH_WEB__PAYLOAD_STORE_PATH=/app/state/payloads`. Do not place an API key
in the example file; pass through supported provider-key variable names as
empty operator inputs. Document `POSTGRES_PASSWORD` as a 48-character
lowercase hexadecimal value generated with `openssl rand -hex 24`. The same
value is the PostgreSQL role password and is interpolated into both connection
URLs, so the supported Compose path deliberately restricts it to URL-unreserved
characters instead of accepting an ambiguously encoded free-form password.

`state-init` uses the same ELSPETH image with an entrypoint override, runs as
the image's UID/GID 1000, and idempotently runs
`install -d -m 0700 /app/state/data /app/state/data/blobs
/app/state/payloads` so reused volumes are repaired to the required mode. It
exits before `web-init`; it does not run alongside the web process.

- [ ] **Step 4: Re-run static validation**

```bash
uv run --frozen pytest -q tests/unit/deployment/test_compose_bundle.py tests/unit/docs/test_docker_guide_release_examples.py
```

Expected: pass without contacting a registry or cloud provider.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yaml deploy/compose tests/unit/deployment/test_compose_bundle.py tests/unit/docs/test_docker_guide_release_examples.py
git commit -m "feat(deploy): ship wired Compose PostgreSQL services"
```

### Task 7: Add a portable native Linux service bundle

**Files:**
- Create: `deploy/linux-systemd/elspeth-web.service`
- Create: `deploy/linux-systemd/elspeth-web.env.example`
- Create: `tests/unit/deployment/test_linux_systemd_bundle.py`
- Modify: `tests/unit/deployment/test_elspeth_web_service.py`

- [ ] **Step 1: Add failing portable-unit tests**

Assert the new unit:

- runs as `User=elspeth` and `Group=elspeth`;
- uses `WorkingDirectory=/opt/elspeth`;
- requires `EnvironmentFile=/etc/elspeth/elspeth-web.env`;
- uses `StateDirectory=elspeth` with mode `0700`;
- creates `/var/lib/elspeth/data/blobs` and
  `/var/lib/elspeth/payloads` with mode `0700` in `ExecStartPre`;
- starts `/usr/bin/env /opt/elspeth/.venv/bin/elspeth web --host 0.0.0.0
  --port 8451`, so systemd can verify the portable unit before the
  application is installed at its target path;
- has no workers flag and sets `WEB_CONCURRENCY=1`;
- allows writes only under `/var/lib/elspeth` and `/run/elspeth`;
- retains the existing hardening directives and `Restart=on-failure`; and
- does not contain `/home/john`.

Assert the environment example names target, state mode, data path, payload
path, both optional external database URLs as commented examples, the four
required composer settings with values `15`, `10`, `85`, and `10`
respectively, and application secret variables without assigning usable
secrets. Active blank URL assignments are forbidden because `WebSettings`
rejects them.

- [ ] **Step 2: Confirm the bundle is missing**

```bash
uv run --frozen pytest -q tests/unit/deployment/test_linux_systemd_bundle.py tests/unit/deployment/test_elspeth_web_service.py
```

- [ ] **Step 3: Add the portable unit without modifying the staging unit**

Use:

```ini
Environment=ELSPETH_WEB__DEPLOYMENT_TARGET=linux-systemd
Environment=WEB_CONCURRENCY=1
StateDirectory=elspeth
StateDirectoryMode=0700
ExecStartPre=/usr/bin/install -d -m 0700 /var/lib/elspeth/data/blobs /var/lib/elspeth/payloads
ReadWritePaths=/var/lib/elspeth /run/elspeth
```

The environment example defaults to `sqlite-single` with data and payload
paths under `/var/lib/elspeth` and explains the two lines that must change
for `external-postgresql`.

- [ ] **Step 4: Verify syntax and tests**

```bash
systemd-analyze verify deploy/linux-systemd/elspeth-web.service
uv run --frozen pytest -q tests/unit/deployment/test_linux_systemd_bundle.py tests/unit/deployment/test_elspeth_web_service.py
```

Expected: systemd verification exits zero and both old/new unit contracts pass.

- [ ] **Step 5: Commit**

```bash
git add deploy/linux-systemd tests/unit/deployment/test_linux_systemd_bundle.py tests/unit/deployment/test_elspeth_web_service.py
git commit -m "feat(deploy): add portable Linux systemd bundle"
```

### Task 8: Add a single-replica Azure Container Apps bundle

**Files:**
- Create: `deploy/azure-container-apps/main.bicep`
- Create: `deploy/azure-container-apps/main.example.bicepparam`
- Create: `tests/unit/deployment/test_azure_container_apps_bundle.py`
- Modify: `.github/workflows/ci.yaml`

- [ ] **Step 1: Add failing Azure artifact tests**

The tests must parse the Bicep source as text and assert exact contract tokens:

- required parameters for Container Apps environment ID, immutable image
  reference, user-assigned identity resource ID, NFS Azure Files storage name,
  pre-provisioned storage subpath,
  both database Key Vault secret URLs, and application Key Vault secret URLs;
- `activeRevisionsMode: 'Single'`;
- `minReplicas: 1` and `maxReplicas: 1`;
- `ELSPETH_WEB__DEPLOYMENT_TARGET=azure-container-apps`;
- `ELSPETH_WEB__DEPLOYMENT_STATE_MODE=external-postgresql`;
- secret references for session URL, Landscape URL, JWT key, signing key, and
  fingerprint key;
- `ELSPETH_WEB__DATA_DIR` and `ELSPETH_WEB__PAYLOAD_STORE_PATH` under the
  mounted NFS Azure Files path;
- all four required composer environment variables with Bicep parameters
  defaulted to `15`, `10`, `85`, and `10` respectively;
- a `subPath` mount rooted at the operator-prepared `elspeth` directory;
- `storageType: 'NfsAzureFile'`;
- ingress target port 8451;
- liveness `/api/health` and readiness `/api/ready`; and
- no inline secret value or `latest` image tag.

The example parameter file must start with `using './main.bicep'` and use
inert, compilable values: subscription and resource-group IDs use the all-zero
UUID, location is `australiaeast`, the image is
`elspethexample.azurecr.io/elspeth:sha-0000000000000000000000000000000000000000`,
the storage name is `elspeth-nfs`, the subpath is `elspeth`, and Key Vault URLs use
`https://elspeth-example.vault.azure.net/secrets/session-db-url`,
`landscape-db-url`, `web-secret-key`,
`shareable-link-signing-key`, and `fingerprint-key` under that same vault
origin. It contains no credential value.

Cross-check the application container's inherited identity against
`Dockerfile`: the runtime stage must still declare UID/GID 1000 and
`USER elspeth`. Do not invent `runAsUser`, Kubernetes-style
`securityContext`, or a non-root init container that cannot write a
root-owned NFS share.

- [ ] **Step 2: Run the test and confirm the artifact is absent**

```bash
uv run --frozen pytest -q tests/unit/deployment/test_azure_container_apps_bundle.py
```

- [ ] **Step 3: Implement the workload-only Bicep module**

The module assumes the custom-VNet Container Apps environment, managed
identity, Key Vault secrets, and NFS Azure Files environment storage already
exist. It also requires an operator-prepared `elspeth` subdirectory containing
`data/blobs` and `payloads`, all owned by UID/GID 1000 and mode `0700`.
The Azure runbook prepares these paths from a trusted NFS administration host
before creating a revision and verifies them with
`stat -c '%u:%g:%a %n'`; the expected output for each path starts
`1000:1000:700`. The app revision is not granted privilege to repair storage.

The module must not create a database or embed a connection string. Accept
Key Vault secret URLs as parameters, define Container Apps
`configuration.secrets` entries with `keyVaultUrl` plus the managed
identity resource ID, and bind only the resulting `secretRef` names into
container environment variables.

Set the container command to:

```bicep
command: [
  'elspeth'
  'web'
  '--host'
  '0.0.0.0'
  '--port'
  '8451'
]
```

- [ ] **Step 4: Add required pinned Bicep compilation to CI**

Add a static-analysis step that downloads Bicep CLI `v0.44.1` from the
official Azure/Bicep GitHub release, verifies Linux x64 SHA-256
`e17dc9a9888184886bb0c0051a3230b83b19f342749999f707bc571c3dfd2f45`,
and runs:

```bash
/tmp/bicep build deploy/azure-container-apps/main.bicep --stdout >/dev/null
/tmp/bicep build-params deploy/azure-container-apps/main.example.bicepparam --stdout >/dev/null
```

Use the same Python `urllib.request` download pattern already present in the
workflow's pinned actionlint step. The unit test asserts the version, checksum,
and both compile commands remain in `ci.yaml`. This is a required gate, not
an optional operator check.

- [ ] **Step 5: Re-run tests, compile locally with the pinned binary, and commit**

```bash
uv run --frozen pytest -q tests/unit/deployment/test_azure_container_apps_bundle.py
plan_bicep_bin="$(mktemp)"
python - "$plan_bicep_bin" <<'PY'
from pathlib import Path
import hashlib
import sys
import urllib.request

destination = Path(sys.argv[1])
url = "https://github.com/Azure/bicep/releases/download/v0.44.1/bicep-linux-x64"
payload = urllib.request.urlopen(url, timeout=60).read()
expected = "e17dc9a9888184886bb0c0051a3230b83b19f342749999f707bc571c3dfd2f45"
if hashlib.sha256(payload).hexdigest() != expected:
    raise SystemExit("Bicep CLI checksum mismatch")
destination.write_bytes(payload)
PY
chmod +x "$plan_bicep_bin"
"$plan_bicep_bin" build deploy/azure-container-apps/main.bicep --stdout >/dev/null
"$plan_bicep_bin" build-params deploy/azure-container-apps/main.example.bicepparam --stdout >/dev/null
rm -- "$plan_bicep_bin"
git add deploy/azure-container-apps .github/workflows/ci.yaml tests/unit/deployment/test_azure_container_apps_bundle.py
git commit -m "feat(deploy): add Azure Container Apps bundle"
```

### Task 9: Add a single-replica Kubernetes/Kustomize base

**Files:**
- Create: `deploy/kubernetes/base/kustomization.yaml`
- Create: `deploy/kubernetes/base/deployment.yaml`
- Create: `deploy/kubernetes/base/service.yaml`
- Create: `deploy/kubernetes/base/configmap.yaml`
- Create: `deploy/kubernetes/base/pvc.yaml`
- Create: `deploy/kubernetes/base/secret.example.yaml`
- Create: `tests/unit/deployment/test_kubernetes_bundle.py`
- Modify: `.github/workflows/ci.yaml`

- [ ] **Step 1: Add failing manifest-contract tests**

Parse every YAML document and assert:

- Kustomize includes Deployment, Service, ConfigMap, and PVC, but excludes the
  example Secret;
- Deployment replicas are exactly one;
- Deployment strategy is exactly `Recreate`, so an update cannot overlap old
  and new web pods;
- image repository is `ghcr.io/johnm-dta/elspeth` with an immutable example
  SHA tag and `imagePullPolicy: IfNotPresent`;
- command/args start one `elspeth web` process;
- `WEB_CONCURRENCY=1`;
- target/state come from ConfigMap and equal `kubernetes` /
  `external-postgresql`;
- ConfigMap provides the four required composer settings with values `15`,
  `10`, `85`, and `10` respectively;
- session/Landscape URLs and application keys come from Secret key refs;
- data/payload paths are under the mounted PVC;
- a non-root init container using the same immutable ELSPETH image creates the
  data, blob, and payload directories with `umask 077` before the app starts;
- the pod has liveness `/api/health`, readiness `/api/ready`, and a
  non-root security context; and
- no PostgreSQL Deployment, StatefulSet, Service, password, or connection URL
  is shipped.

- [ ] **Step 2: Run the test and confirm manifests are absent**

```bash
uv run --frozen pytest -q tests/unit/deployment/test_kubernetes_bundle.py
```

- [ ] **Step 3: Implement the Kustomize base**

Use pod `fsGroup: 1000` only to make the empty volume root writable. The init
container runs as UID/GID 1000, creates child directories with mode `0700`,
and the application uses only those restrictive child paths. The support
contract requires a storage class that honors `fsGroup`; otherwise the
operator must pre-provision a PVC path writable by UID/GID 1000. Document this
precondition and let the init container fail rather than weakening directory
permissions.

Use ConfigMap values for non-secret settings and Secret keys:

```text
session-db-url
landscape-url
web-secret-key
shareable-link-signing-key
fingerprint-key
```

The example Secret must use inert values that fail production shape checks and
must state that it is documentation only. Operators create a real Secret
outside the base before applying it.

- [ ] **Step 4: Add required pinned Kustomize rendering to CI**

Add a static-analysis step that downloads the official Kubernetes
`kubectl` `v1.35.6` Linux amd64 binary, verifies SHA-256
`5d11e2ba01ea68ffd053f56e27738e2b4330013ee67f7e46c6da6c585d3c9926`,
and runs:

```bash
/tmp/kubectl kustomize deploy/kubernetes/base >/dev/null
```

Use the same checksum-before-execution pattern as the Bicep step. The
Kubernetes bundle test asserts the version, checksum, and command remain in
`ci.yaml`.

- [ ] **Step 5: Verify the bundle locally with the pinned client**

```bash
uv run --frozen pytest -q tests/unit/deployment/test_kubernetes_bundle.py
plan_kubectl_bin="$(mktemp)"
python - "$plan_kubectl_bin" <<'PY'
from pathlib import Path
import hashlib
import sys
import urllib.request

destination = Path(sys.argv[1])
url = "https://dl.k8s.io/release/v1.35.6/bin/linux/amd64/kubectl"
payload = urllib.request.urlopen(url, timeout=60).read()
expected = "5d11e2ba01ea68ffd053f56e27738e2b4330013ee67f7e46c6da6c585d3c9926"
if hashlib.sha256(payload).hexdigest() != expected:
    raise SystemExit("kubectl checksum mismatch")
destination.write_bytes(payload)
PY
chmod +x "$plan_kubectl_bin"
"$plan_kubectl_bin" kustomize deploy/kubernetes/base >/dev/null
rm -- "$plan_kubectl_bin"
```

- [ ] **Step 6: Commit**

```bash
git add deploy/kubernetes/base .github/workflows/ci.yaml tests/unit/deployment/test_kubernetes_bundle.py
git commit -m "feat(deploy): add Kubernetes single-replica base"
```

### Task 10: Add the machine-readable support profiles and AWS profile

**Files:**
- Create: `deploy/platforms/schema.json`
- Create: `deploy/platforms/docker-compose.yaml`
- Create: `deploy/platforms/linux-systemd.yaml`
- Create: `deploy/platforms/aws-ecs.yaml`
- Create: `deploy/platforms/azure-container-apps.yaml`
- Create: `deploy/platforms/kubernetes.yaml`
- Create: `tests/unit/deployment/test_platform_profiles.py`
- Modify: `tests/unit/test_build_push_release_checks.py`

- [ ] **Step 1: Add failing profile inventory/schema tests**

Require exactly these target filenames:

```python
{
    "docker-compose",
    "linux-systemd",
    "aws-ecs",
    "azure-container-apps",
    "kubernetes",
}
```

Validate every file against `schema.json` with `jsonschema`. The schema
requires:

```yaml
profile_version: 1
deployment_target: docker-compose
supported_state_modes: [sqlite-single, external-postgresql]
recommended_state_mode: external-postgresql
database_ownership: compose-sidecar
required_extras: [webui, postgres]
image_delivery: ghcr
max_web_processes_per_replica: 1
max_web_replicas: 1
payload_storage: named-volume
artifact_paths:
  - docker-compose.yaml
  - deploy/compose/postgres.yaml
  - deploy/compose/web-postgres.yaml
runbook: docs/guides/docker.md
```

The schema enumerates the five target names, three database-ownership values,
four image-delivery values, and these payload-storage values:
`named-volume`, `host-persistent`, `external-filesystem`, and
`persistent-volume`. Reject unknown fields and empty lists.

- [ ] **Step 2: Add failing cross-profile invariants**

Assert:

- no profile advertises `auto`;
- AWS/Azure/Kubernetes support only `external-postgresql`, use
  `external-managed`, and require `postgres`;
- Compose supports both modes and uses `compose-sidecar`;
- Linux supports both modes and uses `host-or-managed`;
- every profile fixes `max_web_processes_per_replica` and `max_web_replicas`
  to one;
- every artifact/runbook path exists and is tracked by `git ls-files`;
- every ELSPETH image reference is either an immutable/release-tagged GHCR
  example or an explicit immutable ECR/ACR deployment parameter, and none uses
  mutable `latest`; and
- AWS required extras are exactly `webui`, `llm`, `aws`, and
  `postgres`, matching the lean-image checks in `build-push.yaml`.

- [ ] **Step 3: Run tests and confirm profile files are absent**

```bash
uv run --frozen pytest -q tests/unit/deployment/test_platform_profiles.py tests/unit/test_build_push_release_checks.py
```

- [ ] **Step 4: Create schema and profiles**

Use these exact recommended modes and ownership values:

| Target | Recommended mode | Database ownership | Image delivery | Payload storage | Required extras |
|---|---|---|---|---|---|
| Docker Compose | `external-postgresql` | `compose-sidecar` | `ghcr` | `named-volume` | `webui,postgres` |
| Linux systemd | `sqlite-single` | `host-or-managed` | `native-package` | `host-persistent` | `webui,postgres` |
| AWS ECS | `external-postgresql` | `external-managed` | `ecr` | `external-filesystem` | `webui,llm,aws,postgres` |
| Azure Container Apps | `external-postgresql` | `external-managed` | `acr` | `external-filesystem` | `webui,llm,azure,postgres` |
| Kubernetes | `external-postgresql` | `external-managed` | `ghcr` | `persistent-volume` | `webui,llm,postgres` |

AWS `artifact_paths` points to its profile plus the existing release workflow
and acceptance implementation, not a fabricated task definition.

- [ ] **Step 5: Stage profiles, run profile/release tests, and commit**

```bash
git add deploy/platforms tests/unit/deployment/test_platform_profiles.py tests/unit/test_build_push_release_checks.py
uv run --frozen pytest -q tests/unit/deployment/test_platform_profiles.py tests/unit/test_build_push_release_checks.py
git commit -m "feat(deploy): codify supported platform profiles"
```

### Task 11: Document one honest installation path per profile

**Files:**
- Create: `docs/reference/deployment-platforms.md`
- Modify: `README.md`
- Modify: `docs/guides/docker.md`
- Modify: `docs/reference/environment-variables.md`
- Modify: `docs/repository-structure.md`
- Modify: `docs/runbooks/index.md`
- Modify: `docs/runbooks/aws-ecs-deployment.md`
- Modify: `docs/runbooks/ansible-ubuntu-deployment.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/unit/docs/test_docker_guide_release_examples.py`
- Create: `tests/unit/docs/test_deployment_platform_docs.py`

- [ ] **Step 1: Add failing documentation-contract tests**

Assert the support matrix has one row per profile and links each tracked
artifact/runbook. Assert every relevant guide states:

- the ELSPETH image contains PostgreSQL clients, not PostgreSQL server;
- Compose is the only shipped bundle that may provision PostgreSQL;
- AWS, Azure, and Kubernetes require external managed PostgreSQL;
- Linux supports SQLite single-host or external PostgreSQL;
- both URL forms and their drivers are named;
- all production images/tags are immutable/release-specific;
- web support is one process/replica; and
- payload persistence is separate from database persistence.

Assert the Ubuntu/Azure lean install command includes `postgres`, fixing the
current `webui,azure,llm` omission.

Assert the Compose guide uses `openssl rand -hex 24` for the shared
`POSTGRES_PASSWORD`/connection-URL value and does not suggest embedding an
arbitrary unencoded password in a URL. Assert the Kubernetes guide says the
storage class must honor pod `fsGroup: 1000`, or the operator must
pre-provision a UID/GID-1000-writable persistent path.

Assert the Azure runbook no longer configures
`activeRevisionsMode: Multiple` or performs overlapping traffic-shift
deployments. Its replacement sequence must deactivate/drain the old revision,
prove zero running replicas, deploy one new revision in Single mode, run
`doctor deployment` and readiness checks, and only then restore traffic. The
runbook must call out the deliberate availability interruption.

Assert the Kubernetes guide names `strategy: Recreate`, and the AWS guide
retains `minimumHealthyPercent=0`, `maximumPercent=100`, and
`desiredCount=1`. These three controls are the provider-specific evidence for
the same zero-overlap web contract.

- [ ] **Step 2: Run documentation tests and confirm the support matrix is absent**

```bash
uv run --frozen pytest -q tests/unit/docs/test_deployment_platform_docs.py tests/unit/docs/test_docker_guide_release_examples.py
```

- [ ] **Step 3: Write the support matrix and update entry points**

The matrix must distinguish:

| Profile | Database | Payload storage | Deployment entry point |
|---|---|---|---|
| Docker Compose | bundled sidecar or operator external PostgreSQL; SQLite for CLI/local mode | named volume | exact three-file Compose command |
| AWS ECS | external Aurora/PostgreSQL | EFS/external filesystem | existing live-task clone runbook |
| Azure Container Apps | external Azure Database for PostgreSQL | NFS Azure Files | Bicep module + zero-overlap Azure runbook |
| Kubernetes | external PostgreSQL | PVC | Kustomize base |
| Native Linux | SQLite single host or external PostgreSQL | host persistent directory | portable systemd unit |

Document `doctor deployment --init-schema` as the external database bootstrap
command and retain `doctor aws-ecs --init-schema` as its AWS-compatible
operator entry point.

Replace the Azure runbook's Multiple-revision traffic-shifting section rather
than leaving two competing procedures. Link the NFS/custom-VNet prerequisite
and operator-prepared UID/GID-1000 subpath to Microsoft's Container Apps
storage and container references.

- [ ] **Step 4: Update release notes and repository structure**

Add an Unreleased changelog entry describing the deployment contract without
claiming multi-replica support. List `deploy/compose`,
`deploy/linux-systemd`, `deploy/azure-container-apps`,
`deploy/kubernetes`, and `deploy/platforms` in repository structure.

- [ ] **Step 5: Run docs tests, link checks already present in the suite, and commit**

```bash
uv run --frozen pytest -q tests/unit/docs/test_deployment_platform_docs.py tests/unit/docs/test_docker_guide_release_examples.py
git add README.md CHANGELOG.md docs tests/unit/docs/test_deployment_platform_docs.py tests/unit/docs/test_docker_guide_release_examples.py
git commit -m "docs: publish cross-platform deployment matrix"
```

### Task 12: Prove PostgreSQL and platform behavior end to end

**Files:**
- Modify: `tests/testcontainer/web/test_aws_ecs_validate_only_startup.py`
- Modify: `tests/testcontainer/web/test_doctor_aws_ecs_postgres.py`
- Create: `tests/testcontainer/web/test_external_deployment_postgres.py`

- [ ] **Step 1: Generalize PostgreSQL testcontainer coverage**

Parameterize external startup/doctor cases over:

```python
[
    "docker-compose",
    "linux-systemd",
    "aws-ecs",
    "azure-container-apps",
    "kubernetes",
]
```

For each target, create two logical PostgreSQL databases, initialize through
`doctor deployment --init-schema`, then assert validate-only web startup and
readiness succeed. Keep AWS-specific OTLP identity fixtures only in the AWS
case.

Add regressions proving one shared database, stale schema, and missing schema
fail closed without leaking a URL or password. Preserve the existing positive
contract that a runtime role denied DDL succeeds when both schemas are already
current. Separately prove `doctor deployment --init-schema` fails cleanly when
the schema is missing and its initializer role is denied DDL.

- [ ] **Step 2: Run focused Docker-backed tests**

```bash
uv run --frozen pytest -q tests/testcontainer/web/test_external_deployment_postgres.py tests/testcontainer/web/test_aws_ecs_validate_only_startup.py tests/testcontainer/web/test_doctor_aws_ecs_postgres.py
```

Expected: pass when Docker is available. A Docker-unavailable environment may
skip only tests already marked for that condition; it must not convert an
application failure into a skip.

- [ ] **Step 3: Run a real Compose smoke test**

Build the current checkout so the smoke test does not depend on a future
published tag:

```bash
docker build -t elspeth-deployment-contract/elspeth:test .
export IMAGE_TAG=test
export REGISTRY=elspeth-deployment-contract
export POSTGRES_PASSWORD=0123456789abcdef0123456789abcdef0123456789abcdef
export ELSPETH_WEB_SECRET_KEY=test-only-secret-key-with-more-than-32-bytes
export ELSPETH_WEB_SHAREABLE_LINK_SIGNING_KEY=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=
export ELSPETH_WEB_COMPOSER_BOOT_PROBE_ENABLED=false
compose_cmd=(
  docker compose
  -p elspeth-deployment-contract-test
  -f docker-compose.yaml
  -f deploy/compose/postgres.yaml
  -f deploy/compose/web-postgres.yaml
)
cleanup_compose_smoke() {
  "${compose_cmd[@]}" down --volumes
}
trap cleanup_compose_smoke EXIT
"${compose_cmd[@]}" up -d --wait postgres web
curl --fail --silent http://127.0.0.1:8451/api/health
curl --fail --silent http://127.0.0.1:8451/api/ready
```

Expected: both return HTTP 200. The `EXIT` trap uses the same explicit project
name and file set as `up`, so it removes this test's containers and volumes on
success or failure without targeting another Compose stack.

- [ ] **Step 4: Run the complete focused regression set**

```bash
uv run --frozen pytest -q tests/unit/deployment tests/unit/web/test_config.py tests/unit/web/test_deployment_contract.py tests/unit/web/test_schema_probe.py tests/unit/web/test_external_state_startup.py tests/unit/web/test_aws_ecs_startup.py tests/unit/web/test_app.py tests/unit/web/test_landscape_access.py tests/unit/web/test_readiness.py tests/unit/web/test_doctor.py tests/unit/cli/test_doctor_command.py tests/unit/test_build_push_release_checks.py tests/unit/docs/test_docker_guide_release_examples.py tests/unit/docs/test_deployment_platform_docs.py
```

- [ ] **Step 5: Run static and repository gates**

```bash
uv run --frozen ruff check src/ tests/ scripts/ examples/ elspeth-lints/src/
uv run --frozen ruff format --check src/ tests/ scripts/ examples/ elspeth-lints/src/
uv run --frozen mypy src/ elspeth-lints/src/
uv run --frozen python scripts/check_contracts.py
PYTHONPATH=elspeth-lints/src uv run --frozen python -m elspeth_lints.core.cli check --rules meta.no-new-bespoke-cicd-enforcer --root .
git diff --check
```

Do not add a new bespoke `scripts/cicd/enforce_*.py` gate. Profile invariants
belong in the ordinary unit-test suite created in Task 10.

- [ ] **Step 6: Run the full unit suite**

```bash
uv run --frozen pytest -q tests/unit
```

Expected: zero failures. Record skips separately; do not describe skipped
provider CLI validation as executed.

- [ ] **Step 7: Inspect image dependency evidence**

```bash
docker run --rm --entrypoint python elspeth-deployment-contract/elspeth:test -c 'import psycopg, psycopg2; print(psycopg.__version__); print(psycopg2.__version__)'
docker image inspect --format '{{ index .Config.Labels "io.elspeth.install-extras" }}' elspeth-deployment-contract/elspeth:test
```

Expected: both imports succeed and the generic image label is `all`.

- [ ] **Step 8: Commit final integration-only changes**

If Task 12 required test/workflow corrections:

```bash
git add tests/testcontainer/web
git commit -m "test(deploy): verify external PostgreSQL profiles"
```

If no files changed, do not create an empty commit.

## Completion Criteria

- The ELSPETH image is still one non-root application process and contains both
  PostgreSQL client drivers.
- Compose has a tested PostgreSQL sidecar path that creates and wires distinct
  session/Landscape databases.
- AWS, Azure, and Kubernetes profiles require external PostgreSQL and persistent
  payload storage.
- Native Linux has a portable tracked systemd bundle supporting explicit
  SQLite-single or external-PostgreSQL operation.
- Runtime config recognizes every supported target and rejects invalid
  target/state combinations.
- External PostgreSQL startup is validate-only on every platform.
- Every web bundle fixes worker/replica count to one.
- Profile artifacts, docs, image extras, and release workflow remain
  cross-checked by tests.
- No production secret or mutable `latest` tag is committed.
- Focused unit, PostgreSQL testcontainer, Compose smoke, static-analysis, and
  full unit gates pass, with unavailable provider CLIs reported honestly.
