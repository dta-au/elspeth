"""Git ignore contracts for the Composer parity corpus."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]


def _is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", path],
        cwd=REPO_ROOT,
        check=False,
    )
    return result.returncode == 0


@pytest.mark.parametrize(
    "filename",
    [
        "jwt.txt",
        "jwt-case.txt",
        "jwt_case.txt",
        "service.api_key",
        "service.access_token",
        "service.bearer_token",
        "login.json",
        "login_admin.json",
        "certificate.pem",
        "certificate.p12",
        "certificate.pfx",
        ".env",
        ".env.production",
    ],
)
def test_credential_shaped_corpus_files_remain_ignored(filename: str) -> None:
    assert _is_ignored(f"evals/composer-parity/nested/case/{filename}")


@pytest.mark.parametrize(
    "filename",
    [
        "README.md",
        "request.json",
        "expected.csv",
        ".env.example",
        ".env.template",
        ".env.sample",
    ],
)
def test_safe_corpus_files_remain_trackable(filename: str) -> None:
    assert not _is_ignored(f"evals/composer-parity/nested/case/{filename}")
