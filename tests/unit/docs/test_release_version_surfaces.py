"""Release-version consistency across package and current public surfaces."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CURRENT_VERSION = "0.7.2"


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_package_and_lockfile_use_current_release_version() -> None:
    pyproject = tomllib.loads(_text("pyproject.toml"))
    lockfile = tomllib.loads(_text("uv.lock"))
    locked_project = next(package for package in lockfile["package"] if package["name"] == "elspeth")

    assert pyproject["project"]["version"] == CURRENT_VERSION
    assert locked_project["version"] == CURRENT_VERSION


def test_current_release_indexes_name_release_072() -> None:
    assert "**Framework status:** `0.7.2`" in _text("docs/README.md")

    current_state = _text("docs/product/current-state.md")
    assert "**Release branch:** `release/0.7.2`" in current_state
    assert "root package metadata and lockfile identify 0.7.2" in current_state

    roadmap = _text("docs/product/roadmap.md")
    assert "Ship the 0.7.2 line" in roadmap
    assert "`release/0.7.2`" in roadmap


def test_current_container_examples_require_a_confirmed_published_tag() -> None:
    for relative_path in (
        "README.md",
        "docs/guides/docker.md",
        "docs/guides/troubleshooting.md",
        "docs/guides/your-first-pipeline.md",
        "docs/reference/environment-variables.md",
        "docs/runbooks/resume-failed-run.md",
    ):
        text = _text(relative_path)
        assert "IMAGE_TAG" in text, relative_path
        assert "exact published" in text, relative_path
        assert "v0.7.2" not in text, relative_path
        assert "v0.7.1" not in text, relative_path
        assert "elspeth:latest" not in text, relative_path

    docker = _text("docs/guides/docker.md")
    assert "docker buildx imagetools inspect" in docker
    assert "do not infer an image tag from the python package version" in docker.lower()


def test_first_pipeline_docker_walkthrough_creates_the_mounted_state_directory() -> None:
    tutorial = _text("docs/guides/your-first-pipeline.md")

    assert "mkdir -p my-pipeline/{config,input,output,data}" in tutorial
    assert "-v $(pwd)/data:/app/data" in tutorial
    assert "mkdir -p my-pipeline/{config,input,output,state}" not in tutorial


def test_current_operator_runbooks_use_072_candidate_and_071_baseline() -> None:
    ansible = _text("docs/runbooks/ansible-ubuntu-deployment.md")
    assert "schema-incompatible 0.7.2 upgrade from 0.7.1" in ansible
    assert "direct 0.7.1→0.7.2 upgrade" in ansible
    assert "full 40-character Git commit SHA" in ansible
    assert "ELSPETH_RELEASE_REF=v0.7.2" not in ansible
    assert "ELSPETH_ROLLBACK_REF=v0.7.1" not in ansible

    sharing = _text("docs/guides/sharing-pipelines.md")
    assert "For 0.7.2" in sharing
    assert "SESSION_SCHEMA_EPOCH=37" in sharing
    assert "SQLITE_SCHEMA_EPOCH=30" in sharing

    aws = _text("docs/runbooks/aws-ecs-deployment.md")
    assert "elspeth:ecs-0.7.2-closeout" in aws
    assert '"candidate_package_version": "0.7.2"' in aws
    assert '"previous_package_version": "0.7.1"' in aws
    assert '"previous": {"session_epoch": 35, "landscape_epoch": 29' in aws
