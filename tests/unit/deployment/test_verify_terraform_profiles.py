"""Strict offline verification of Terraform provider and backend profiles."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = REPO_ROOT / "deploy" / "aws-ecs" / "terraform" / "scripts" / "verify-terraform-profiles.py"

INSTALLER = "elspeth-installer"
LIFECYCLE = "elspeth-iam-lifecycle"
ROOT_PROFILE = "elspeth-acceptance"


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def _plan() -> dict[str, object]:
    return {
        "format_version": "1.2",
        "variables": {
            "aws_profile": {"value": INSTALLER},
            "iam_lifecycle_aws_profile": {"value": LIFECYCLE},
        },
        "resource_changes": [],
    }


def _backend() -> dict[str, object]:
    return {
        "version": 3,
        "backend": {
            "type": "s3",
            "config": {"bucket": "example-state", "profile": INSTALLER},
        },
    }


def _run_plan(path: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "plan",
            "--plan-json",
            str(path),
            "--installer-profile",
            INSTALLER,
            "--iam-lifecycle-profile",
            LIFECYCLE,
            "--forbidden-profile",
            ROOT_PROFILE,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )


def _run_backend(path: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "backend",
            "--backend-state",
            str(path),
            "--installer-profile",
            INSTALLER,
            "--forbidden-profile",
            ROOT_PROFILE,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )


def _assert_refused(completed: subprocess.CompletedProcess[bytes]) -> None:
    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr.startswith(b"verify-terraform-profiles: refused ")


def test_plan_accepts_only_the_two_exact_distinct_profiles(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    _write_json(path, _plan())

    completed = _run_plan(path)

    assert completed.returncode == 0
    assert completed.stdout == b"terraform plan profiles verified\n"
    assert completed.stderr == b""


@pytest.mark.parametrize(
    ("variable", "replacement"),
    [
        ("aws_profile", ROOT_PROFILE),
        ("iam_lifecycle_aws_profile", ROOT_PROFILE),
        ("aws_profile", LIFECYCLE),
        ("iam_lifecycle_aws_profile", INSTALLER),
        ("aws_profile", True),
    ],
    ids=["root-installer", "root-lifecycle", "collapsed-installer", "collapsed-lifecycle", "non-string"],
)
def test_plan_refuses_wrong_collapsed_root_or_non_string_profiles(
    tmp_path: Path,
    variable: str,
    replacement: object,
) -> None:
    payload = _plan()
    variables = payload["variables"]
    assert isinstance(variables, dict)
    entry = variables[variable]
    assert isinstance(entry, dict)
    entry["value"] = replacement
    path = tmp_path / "plan.json"
    _write_json(path, payload)

    _assert_refused(_run_plan(path))


def test_plan_refuses_collapsed_expected_profiles_before_reading_the_artifact(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    _write_json(path, _plan())

    completed = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "plan",
            "--plan-json",
            str(path),
            "--installer-profile",
            INSTALLER,
            "--iam-lifecycle-profile",
            INSTALLER,
            "--forbidden-profile",
            ROOT_PROFILE,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )

    _assert_refused(completed)


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b'{"variables":{},"variables":{}}',
        b'{"variables":{"aws_profile":{"value":NaN}}}',
        b"[]",
    ],
    ids=["invalid-json", "duplicate-key", "non-finite", "non-object"],
)
def test_plan_refuses_malformed_or_ambiguous_json(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "plan.json"
    path.write_bytes(raw)

    _assert_refused(_run_plan(path))


def test_backend_accepts_only_the_initialized_s3_installer_profile(tmp_path: Path) -> None:
    path = tmp_path / "terraform.tfstate"
    _write_json(path, _backend())

    completed = _run_backend(path)

    assert completed.returncode == 0
    assert completed.stdout == b"terraform backend profile verified\n"
    assert completed.stderr == b""


@pytest.mark.parametrize(
    "replacement",
    [ROOT_PROFILE, LIFECYCLE, True, None],
    ids=["root", "lifecycle", "non-string", "missing-value"],
)
def test_backend_refuses_any_profile_other_than_the_exact_installer(
    tmp_path: Path,
    replacement: object,
) -> None:
    payload = _backend()
    backend = payload["backend"]
    assert isinstance(backend, dict)
    config = backend["config"]
    assert isinstance(config, dict)
    config["profile"] = replacement
    path = tmp_path / "terraform.tfstate"
    _write_json(path, payload)

    _assert_refused(_run_backend(path))


def test_backend_refuses_a_non_s3_backend_even_with_the_expected_profile(tmp_path: Path) -> None:
    payload = _backend()
    backend = payload["backend"]
    assert isinstance(backend, dict)
    backend["type"] = "local"
    path = tmp_path / "terraform.tfstate"
    _write_json(path, payload)

    _assert_refused(_run_backend(path))
