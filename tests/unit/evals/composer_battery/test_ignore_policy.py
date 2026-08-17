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
        "jwt-1.txt",
        "jwt_1.txt",
        "login.json",
        "login_battery.json",
        "state/credentials.json",
        "service.access_token",
        "service.api_key",
        "service.bearer_token",
        "certificate.pem",
        "client.p12",
        "client.pfx",
        ".env",
        ".env.staging",
    ],
)
def test_credential_and_capture_paths_stay_ignored(path: str) -> None:
    """The `!/evals/composer-battery/**` re-include undoes the repo-wide credential rules at the top of
    .gitignore, so the full composer-parity block has to be re-stated after it — the last matching rule wins."""
    assert _is_ignored(f"evals/composer-battery/{path}"), path


@pytest.mark.parametrize(
    "path",
    [
        "corpus.md",
        "README.md",
        "drive_battery.py",
        "scenarios/canary/scenario.json",
        "calibration/README.md",
        ".env.example",  # the documented sample stays trackable: parity re-includes example/template/sample
        ".env.template",
        ".env.sample",
    ],
)
def test_instrument_files_are_tracked(path: str) -> None:
    assert not _is_ignored(f"evals/composer-battery/{path}"), path
