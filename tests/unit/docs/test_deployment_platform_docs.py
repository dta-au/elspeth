"""Public deployment documentation contract."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PLATFORM_DOC = REPO_ROOT / "docs" / "reference" / "deployment-platforms.md"
README = REPO_ROOT / "README.md"
DOCKER_GUIDE = REPO_ROOT / "docs" / "guides" / "docker.md"
ENVIRONMENT_REFERENCE = REPO_ROOT / "docs" / "reference" / "environment-variables.md"
REPOSITORY_STRUCTURE = REPO_ROOT / "docs" / "repository-structure.md"
RUNBOOK_INDEX = REPO_ROOT / "docs" / "runbooks" / "index.md"
AWS_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "aws-ecs-deployment.md"
UBUNTU_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "ansible-ubuntu-deployment.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalized(path: Path) -> str:
    return " ".join(_read(path).split())


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
        "../runbooks/aws-ecs-deployment.md",
        "../runbooks/ansible-ubuntu-deployment.md",
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
        "Compose is the only shipped bundle that provisions PostgreSQL",
        "immutable, release-specific image",
        "one web process or replica",
        "Payload persistence is separate from database persistence",
        "doctor deployment --init-schema",
        "doctor aws-ecs --init-schema",
    ):
        assert phrase in text

    assert "Every other production topology supplies its own external PostgreSQL service" not in text
    assert "AWS ECS, Azure production, and Kubernetes BYO deployments require operator-provided external PostgreSQL" in text
    assert "Native Linux may instead use SQLite on one persistent host" in text


def test_maintained_entry_points_repeat_the_database_and_process_boundaries() -> None:
    for path in (README, DOCKER_GUIDE, AWS_RUNBOOK, UBUNTU_RUNBOOK):
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


def test_native_linux_documents_sqlite_or_postgresql_and_postgres_extra() -> None:
    text = _read(UBUNTU_RUNBOOK)

    assert "uv sync --frozen --extra webui --extra azure --extra llm --extra postgres" in text
    assert "SQLite" in text
    assert "single host" in text
    assert "external PostgreSQL" in text
    assert "doctor deployment --init-schema" in text


def test_aws_retains_the_zero_overlap_ecs_controls() -> None:
    text = _read(AWS_RUNBOOK)

    assert "minimumHealthyPercent" in text
    assert "maximumPercent" in text
    assert "desiredCount" in text
    assert "minimumHealthyPercent=0" in text
    assert "maximumPercent=100" in text
    assert "desiredCount=1" in text
    assert "doctor aws-ecs --init-schema" in text


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
    assert "`deploy/linux-systemd/`" in structure
    for absent_path in ("deploy/azure-container-apps", "deploy/kubernetes", "deploy/platforms"):
        assert absent_path not in structure


def test_unreleased_changelog_does_not_claim_multi_replica_support() -> None:
    text = _read(CHANGELOG)
    unreleased = text.split("## 0.7.2", maxsplit=1)[0]

    assert "## Unreleased" in unreleased
    assert "cross-platform deployment contract" in unreleased.lower()
    assert "Docker Compose" in unreleased
    assert "AWS ECS" in unreleased
    assert "native Linux" in unreleased
    assert "Azure Ubuntu VM" in unreleased
    assert "Kubernetes" in unreleased
    assert "multi-replica" not in unreleased.lower()
    assert "deploy/platforms" not in unreleased
