"""Regression tests for deployment artifact ignore policy."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[3]


def _is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", "--", path],
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode in {0, 1}
    return result.returncode == 0


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
