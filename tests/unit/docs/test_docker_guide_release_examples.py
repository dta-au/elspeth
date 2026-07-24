"""Regression checks for Docker guide release examples."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKER_GUIDE = REPO_ROOT / "docs" / "guides" / "docker.md"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yaml"
STALE_IMAGE_TAG = "elspeth:v0.1.0"
SHIPPED_COMPOSE_IMAGE = "${REGISTRY:-ghcr.io/johnm-dta}/elspeth:${IMAGE_TAG:?set IMAGE_TAG to an immutable sha-* or v* tag}"


def test_docker_guide_uses_release_tag_variable_for_image_examples() -> None:
    text = DOCKER_GUIDE.read_text(encoding="utf-8")

    assert STALE_IMAGE_TAG not in text
    assert "IMAGE_TAG=" in text
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
