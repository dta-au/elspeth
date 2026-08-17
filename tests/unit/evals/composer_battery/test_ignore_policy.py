"""Git ignore contracts for the composer battery (mirror of composer_parity/test_ignore_policy.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]


def _is_ignored(path: str) -> bool:
    return subprocess.run(["git", "check-ignore", "--no-index", "--quiet", path], cwd=REPO_ROOT, check=False).returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        "runs/r1/canary/1/messages.json",
        "runs/anything",
        "jwt.txt",
        "login.json",
        "state/credentials.json",
        "service.access_token",
        "service.api_key",
        "certificate.pem",
        ".env",
    ],
)
def test_credential_and_capture_paths_stay_ignored(path: str) -> None:
    assert _is_ignored(f"evals/composer-battery/{path}"), path


@pytest.mark.parametrize("path", ["corpus.md", "README.md", "drive_battery.py", "scenarios/canary/scenario.json", "calibration/README.md"])
def test_instrument_files_are_tracked(path: str) -> None:
    assert not _is_ignored(f"evals/composer-battery/{path}"), path
