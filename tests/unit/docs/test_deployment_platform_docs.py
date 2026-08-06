"""Public deployment documentation contract."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PLATFORM_DOC = REPO_ROOT / "docs" / "reference" / "deployment-platforms.md"
README = REPO_ROOT / "README.md"
DOCKER_GUIDE = REPO_ROOT / "docs" / "guides" / "docker.md"
ENVIRONMENT_REFERENCE = REPO_ROOT / "docs" / "reference" / "environment-variables.md"
REPOSITORY_STRUCTURE = REPO_ROOT / "docs" / "repository-structure.md"
RUNBOOK_INDEX = REPO_ROOT / "docs" / "runbooks" / "index.md"
AWS_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "aws-ecs-deployment.md"
AWS_COLD_INSTALL_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "aws-ecs-cold-install.md"
AWS_TERRAFORM_PACKAGE = REPO_ROOT / "deploy" / "aws-ecs" / "terraform"
UBUNTU_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "ansible-ubuntu-deployment.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalized(path: Path) -> str:
    return " ".join(_read(path).split())


def _section(text: str, start: str, end: str | None = None) -> str:
    section = text[text.index(start) :]
    if end is not None:
        section = section[: section.index(end)]
    return section


def test_support_matrix_links_only_shipped_deployment_artifacts() -> None:
    text = _read(PLATFORM_DOC)

    for label in (
        "Docker Compose",
        "AWS ECS",
        "Azure Ubuntu VM",
        "Kubernetes (BYO manifests)",
        "Native Linux",
    ):
        assert f"| {label} |" in text

    for relative_path in (
        "../guides/docker.md",
        "../runbooks/aws-ecs-cold-install.md",
        "../runbooks/aws-ecs-deployment.md",
        "../runbooks/ansible-ubuntu-deployment.md",
        "../../deploy/aws-ecs/terraform",
        "../../deploy/compose",
        "../../deploy/linux-systemd/elspeth-web.service",
    ):
        assert relative_path in text
        assert (PLATFORM_DOC.parent / relative_path).resolve().exists()

    for shipped_path in (
        "deploy/compose/postgres.yaml",
        "deploy/compose/web-postgres.yaml",
        "deploy/linux-systemd/elspeth-web.service",
        "deploy/linux-systemd/elspeth-web.env.example",
    ):
        assert (REPO_ROOT / shipped_path).is_file()

    for absent_bundle in ("azure-container-apps", "kubernetes", "platforms"):
        bundle_path = REPO_ROOT / "deploy" / absent_bundle
        assert not bundle_path.exists() or not any(bundle_path.rglob("*"))


def test_support_matrix_states_the_shared_runtime_contract() -> None:
    text = _normalized(PLATFORM_DOC)

    for phrase in (
        "PostgreSQL clients, not a PostgreSQL server",
        "postgresql+psycopg://",
        "postgresql+psycopg2://",
        "Compose provisions a PostgreSQL container",
        "tracked AWS ECS Terraform package provisions Aurora PostgreSQL",
        "immutable, release-specific image",
        "one web process or replica",
        "Payload persistence is separate from database persistence",
        "doctor deployment --init-schema",
        "doctor aws-ecs --init-schema",
        "UID/GID 1654",
    ):
        assert phrase in text

    assert "UID/GID 1000" not in text

    assert "Every other production topology supplies its own external PostgreSQL service" not in text
    assert "Azure production and Kubernetes BYO deployments require operator-provided external PostgreSQL" in text
    assert "Native Linux may instead use SQLite on one persistent host" in text


def test_maintained_entry_points_repeat_the_database_and_process_boundaries() -> None:
    for path in (README, DOCKER_GUIDE, AWS_RUNBOOK, AWS_COLD_INSTALL_RUNBOOK, UBUNTU_RUNBOOK):
        text = _normalized(path)
        assert "PostgreSQL clients" in text, path
        assert "not a PostgreSQL server" in text, path
        assert "postgresql+psycopg://" in text, path
        assert "postgresql+psycopg2://" in text, path
        assert "one web process" in text, path
        assert "payload persistence" in text.lower(), path
        assert "database persistence" in text.lower(), path


def test_azure_support_is_one_stop_before_start_linux_vm() -> None:
    matrix = _read(PLATFORM_DOC)
    runbook = _read(UBUNTU_RUNBOOK)
    combined = f"{matrix}\n{runbook}"

    for phrase in (
        "exactly one Azure Ubuntu VM",
        "deploy/linux-systemd/elspeth-web.service",
        "WEB_CONCURRENCY=1",
        "stop-before-start",
        "Front Door",
        "Azure Database for PostgreSQL",
        "persistent host storage",
        "elspeth-b5d7aa5655",
    ):
        assert phrase in combined

    assert "azure-container-apps" in matrix
    assert "reserved" in matrix.lower()
    assert "unsupported" in matrix.lower()
    assert "activeRevisionsMode" not in runbook
    assert "traffic-shift" not in runbook.lower()
    assert "per-revision" not in runbook.lower()
    assert "deploy/azure-container-apps" not in combined


def test_kubernetes_is_an_explicit_byo_zero_overlap_contract() -> None:
    text = _read(PLATFORM_DOC)

    for phrase in (
        "BYO manifests only",
        "strategy: Recreate",
        "one replica",
        "external PostgreSQL",
        "persistent payload storage",
    ):
        assert phrase in text

    assert "Kustomize" not in text
    assert "ships no manifests" in text
    assert "deploy/kubernetes" not in text


def test_native_linux_documents_sqlite_or_postgresql_and_runtime_extras() -> None:
    text = _read(UBUNTU_RUNBOOK)
    runtime_sync = "uv sync --frozen --extra webui --extra azure --extra llm --extra aws --extra postgres"

    assert text.count(runtime_sync) == 3
    assert "SQLite" in text
    assert "single host" in text
    assert "external PostgreSQL" in text
    assert "doctor deployment --init-schema" in text


def test_native_linux_branches_upgrade_and_rollback_checks_by_state_mode() -> None:
    text = _read(UBUNTU_RUNBOOK)

    for prefix, next_heading in (
        ("### Upgrade validation: external PostgreSQL", "### Upgrade validation: SQLite"),
        ("### Rollback validation: external PostgreSQL", "### Rollback validation: SQLite"),
    ):
        external = _section(text, prefix, next_heading)
        assert "doctor deployment" in external
        assert "--init-schema" not in external
        assert "curl -fsS http://127.0.0.1:8451/api/ready" in external

    upgrade_sqlite = _section(text, "### Upgrade validation: SQLite", "### Restore public service")
    rollback_sqlite = _section(text, "### Rollback validation: SQLite", "### Restore the rollback")
    for sqlite in (upgrade_sqlite, rollback_sqlite):
        assert "doctor deployment" not in sqlite
        assert "/var/lib/elspeth/data/sessions.db" in sqlite
        assert "/var/lib/elspeth/data/runs/audit.db" in sqlite
        assert "stat -c '%U:%G'" in sqlite
        assert "sudo systemctl start elspeth-web.service" in sqlite
        assert "curl -fsS http://127.0.0.1:8451/api/ready" in sqlite


def test_native_linux_completion_checks_are_state_mode_specific() -> None:
    text = _read(UBUNTU_RUNBOOK)
    external = _section(text, "### External PostgreSQL completion", "### SQLite completion")
    sqlite = _section(text, "### SQLite completion", "## See also")

    assert "doctor deployment" in external
    assert "exec elspeth doctor deployment" in external
    assert "sudo systemctl is-active --quiet elspeth-web.service" in external
    assert "curl -fsS http://127.0.0.1:8451/api/ready" in external
    assert "doctor deployment" not in sqlite
    assert "/var/lib/elspeth/data/sessions.db" in sqlite
    assert "/var/lib/elspeth/data/runs/audit.db" in sqlite
    assert "sudo -u elspeth test -r" in sqlite
    assert "stat -c '%U:%G'" in sqlite
    assert "sudo systemctl is-active --quiet elspeth-web.service" in sqlite
    assert "/api/ready" in sqlite


def test_native_linux_trust_gate_runs_after_checkout_with_dev_dependency_then_syncs_lean() -> None:
    text = " ".join(_read(UBUNTU_RUNBOOK).replace("\\\n", "").split())
    gate = (
        "uv run --frozen --extra dev elspeth-lints check --rules trust_tier.tier_model "
        "--root src/elspeth --allowlist-dir config/cicd/enforce_tier_model"
    )
    lean_sync = "uv sync --frozen --extra webui --extra azure --extra llm --extra aws --extra postgres"

    assert text.index('git -C /opt/elspeth checkout --detach "$ELSPETH_RELEASE_REF"') < text.index(gate)
    assert text.index(gate) < text.index(lean_sync)
    assert "removes the development-only gate dependencies" in text

    pyproject = tomllib.loads(_read(REPO_ROOT / "pyproject.toml"))
    assert "elspeth-lints" in pyproject["project"]["optional-dependencies"]["dev"]


def test_native_linux_rebuilds_frontend_after_upgrade_and_rollback_checkouts() -> None:
    text = _read(UBUNTU_RUNBOOK)

    for section_start, section_end, ref_name in (
        ("## Stop-before-start upgrade", "### Upgrade validation: external PostgreSQL", "ELSPETH_RELEASE_REF"),
        ("## Rollback", "### Rollback validation: external PostgreSQL", "ELSPETH_ROLLBACK_REF"),
    ):
        section = " ".join(_section(text, section_start, section_end).replace("\\\n", "").split())
        checkout = f'git -C /opt/elspeth checkout --detach "${ref_name}"'
        node = "sudo -u elspeth node --version"
        npm = "sudo -u elspeth npm --version"
        npm_ci = "sudo -u elspeth npm --prefix src/elspeth/web/frontend ci"
        frontend_build = "sudo -u elspeth npm --prefix src/elspeth/web/frontend run build"

        assert section.count(checkout) == 1
        assert section.index(checkout) < section.index(node)
        assert section.index(node) < section.index(npm)
        assert section.index(npm) < section.index(npm_ci)
        assert section.index(npm_ci) < section.index(frontend_build)


def test_aws_retains_the_zero_overlap_ecs_controls() -> None:
    text = _read(AWS_RUNBOOK)

    assert "minimumHealthyPercent" in text
    assert "maximumPercent" in text
    assert "desiredCount" in text
    assert "minimumHealthyPercent=0" in text
    assert "maximumPercent=100" in text
    assert "desiredCount=1" in text
    assert "doctor aws-ecs --init-schema" in text


def test_aws_docs_distinguish_tracked_cold_install_from_acceptance_inputs() -> None:
    for path in (PLATFORM_DOC, README, CHANGELOG, AWS_RUNBOOK, AWS_COLD_INSTALL_RUNBOOK):
        assert "live-task clone" not in _read(path).lower(), path

    matrix = _normalized(PLATFORM_DOC)
    assert "tracked AWS ECS Terraform package" in matrix
    assert "creates a VPC, Aurora PostgreSQL" in matrix
    assert AWS_TERRAFORM_PACKAGE.is_dir()
    assert "deploy/aws-ecs/terraform" in _read(AWS_COLD_INSTALL_RUNBOOK)
    assert "CANDIDATE_TASK_DEFINITION" in _read(AWS_RUNBOOK)
    assert "DOCTOR_TASK_DEFINITION" in _read(AWS_RUNBOOK)
    assert "PREVIOUS_TASK_DEFINITION" in _read(AWS_RUNBOOK)
    assert "aws-ecs-cold-install.md" in _read(README)
    assert "AWS ECS Terraform cold-install package" in _normalized(CHANGELOG)


def test_azure_sqlite_scope_is_consistent() -> None:
    for path in (PLATFORM_DOC, DOCKER_GUIDE, UBUNTU_RUNBOOK):
        text = _normalized(path)
        assert "Azure production requires external Azure Database for PostgreSQL" in text, path
        assert "Azure VM SQLite is supported only for explicitly non-production use on one persistent host" in text, path


def test_environment_reference_documents_deployment_state_settings() -> None:
    text = _read(ENVIRONMENT_REFERENCE)

    for name in (
        "ELSPETH_WEB__DEPLOYMENT_TARGET",
        "ELSPETH_WEB__DEPLOYMENT_STATE_MODE",
        "ELSPETH_WEB__SESSION_DB_URL",
        "ELSPETH_WEB__LANDSCAPE_URL",
        "ELSPETH_WEB__DATA_DIR",
        "ELSPETH_WEB__PAYLOAD_STORE_PATH",
        "WEB_CONCURRENCY",
    ):
        assert name in text


def test_navigation_and_repository_structure_are_honest() -> None:
    readme = _read(README)
    runbook_index = _read(RUNBOOK_INDEX)
    structure = _read(REPOSITORY_STRUCTURE)

    assert "docs/reference/deployment-platforms.md" in readme
    assert "Deployment Platforms" in runbook_index
    assert "Azure Container Apps" in runbook_index
    assert "deferred" in runbook_index.lower()
    assert "Kubernetes" in runbook_index
    assert "BYO" in runbook_index

    assert "`deploy/compose/`" in structure
    assert "`deploy/aws-ecs/terraform/`" in structure
    assert "`deploy/linux-systemd/`" in structure
    for absent_path in ("deploy/azure-container-apps", "deploy/kubernetes", "deploy/platforms"):
        assert absent_path not in structure


def test_release_changelog_states_cross_platform_contract_without_multi_replica_claim() -> None:
    """The notes for the release that ships these platforms must not oversell them.

    The entry was authored under ``## Unreleased`` and promoted into ``## 0.7.2``
    when that release was cut, so the gate follows the text rather than the
    heading it was first written under. ``## Unreleased`` above it is asserted
    to exist but is deliberately not asserted to be non-empty — that is release
    bookkeeping, not a deployment-contract claim.
    """
    text = _read(CHANGELOG)
    assert "## Unreleased" in text
    # Whitespace-normalised: the changelog hard-wraps prose, so "one Azure\n
    # Ubuntu VM" would defeat a raw substring match. Normalising also tightens
    # the multi-replica prohibition, which a wrapped occurrence could evade.
    release = " ".join(_section(text, "## 0.7.2", "## 0.7.1").split())

    assert "cross-platform deployment contract" in release.lower()
    assert "Docker Compose" in release
    assert "AWS ECS" in release
    assert "native Linux" in release
    assert "Azure Ubuntu VM" in release
    assert "Kubernetes" in release
    assert "multi-replica" not in release.lower()
    assert "deploy/platforms" not in release
