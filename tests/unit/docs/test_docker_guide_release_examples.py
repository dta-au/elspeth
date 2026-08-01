"""Regression checks for Docker guide release examples."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKER_GUIDE = REPO_ROOT / "docs" / "guides" / "docker.md"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yaml"
STALE_IMAGE_TAG = "elspeth:v0.1.0"
SHIPPED_COMPOSE_IMAGE = "${REGISTRY:-ghcr.io/johnm-dta}/elspeth:${IMAGE_TAG:?set IMAGE_TAG to an immutable sha-* or v* tag}"
THREE_FILE_COMPOSE_COMMAND = """docker compose --env-file .env \\
  -f docker-compose.yaml \\
  -f deploy/compose/postgres.yaml \\
  -f deploy/compose/web-postgres.yaml up -d"""


def test_docker_guide_uses_release_tag_variable_for_image_examples() -> None:
    text = DOCKER_GUIDE.read_text(encoding="utf-8")

    assert STALE_IMAGE_TAG not in text
    assert "IMAGE_TAG:?export an exact published sha-* or v* image tag" in text
    assert "ghcr.io/johnm-dta/elspeth:${IMAGE_TAG}" in text
    assert "your-acr.azurecr.io/elspeth:${IMAGE_TAG}" in text


def test_docker_guide_links_to_active_user_manual() -> None:
    text = DOCKER_GUIDE.read_text(encoding="utf-8")

    assert "../USER_MANUAL.md" not in text
    assert "(user-manual.md#cli-commands)" in text
    assert (DOCKER_GUIDE.parent / "user-manual.md").exists()


def test_shipped_compose_base_requires_an_immutable_release_image() -> None:
    text = BASE_COMPOSE.read_text(encoding="utf-8")

    assert SHIPPED_COMPOSE_IMAGE in text
    assert "${IMAGE_TAG:-latest}" not in text


def test_docker_guide_uses_the_shipped_three_file_postgresql_bundle() -> None:
    text = DOCKER_GUIDE.read_text(encoding="utf-8")

    assert " ".join(THREE_FILE_COMPOSE_COMMAND.split()) in " ".join(text.split())
    assert "cp deploy/compose/.env.example .env" in text
    assert "openssl rand -hex 24" in text
    assert "48-character lowercase hexadecimal" in text
    assert "doctor deployment --init-schema" in text

    verification = text[text.index("4. Verify the one-shot schema gate and readiness:") :]
    verification = verification[: verification.index("\n---")]
    assert "run --rm web-init" in verification
    assert "doctor deployment" in verification
    assert "doctor deployment --init-schema" not in verification


def test_docker_guide_explains_the_container_database_boundary() -> None:
    text = DOCKER_GUIDE.read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "PostgreSQL clients" in text
    assert "PostgreSQL clients, not a PostgreSQL server" in normalized
    assert "postgresql+psycopg://" in text
    assert "postgresql+psycopg2://" in text
    assert "Compose provisions a PostgreSQL container" in normalized
    assert "tracked AWS ECS Terraform package provisions Aurora PostgreSQL outside the application task" in normalized
    assert "one web process" in text
    assert "payload persistence" in text.lower()
    assert "database persistence" in text.lower()
    assert "AWS, an Azure Ubuntu VM, or BYO Kubernetes manifests must connect" not in normalized
    assert "Azure production and BYO Kubernetes deployments require operator-provided external PostgreSQL" in normalized
    assert "Azure VM SQLite is supported only for explicitly non-production use on one persistent host" in normalized
    assert "Native Linux may use SQLite on one persistent host" in normalized


def test_docker_guide_has_a_runnable_standalone_web_container() -> None:
    text = DOCKER_GUIDE.read_text(encoding="utf-8")
    section = text[text.index("## Standalone Web Server") : text.index("\n---", text.index("## Standalone Web Server"))]

    assert "openssl rand -hex 32" in section
    assert "openssl rand -base64 32" in section
    assert "mkdir -p ./data/blobs ./data/outputs" in section
    assert "1654:1654" in section
    assert "-p 8451:8451" in section
    assert "web --host 0.0.0.0 --port 8451" in section
    assert "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS=15" in section
    assert "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS=10" in section
    assert "ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=180.0" in section
    assert "ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE=60" in section
    for variable in (
        "ELSPETH_WEB__SECRET_KEY",
        "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY",
        "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS",
        "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS",
        "ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS",
        "ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE",
    ):
        assert variable in section


def test_docker_guide_documents_the_published_runtime_identity() -> None:
    text = " ".join(DOCKER_GUIDE.read_text(encoding="utf-8").split())

    assert "UID/GID 1654" in text
    assert "UID/GID 1000" not in text
