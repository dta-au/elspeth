"""Contracts for the project-owned self-hosted runner hardening tool."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "cicd" / "harden-self-hosted-runners.sh"
HARDENING = """\
[Service]
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectClock=true
ProtectControlGroups=true
ProtectHostname=true
ProtectKernelLogs=true
ProtectKernelModules=true
ProtectKernelTunables=true
LockPersonality=true
RestrictRealtime=true
RestrictSUIDSGID=true
CapabilityBoundingSet=
AmbientCapabilities=
"""


def _runner_layout(tmp_path: Path, *, secure: bool) -> tuple[Path, Path]:
    runner_root = tmp_path / "runners"
    service_dir = tmp_path / "systemd"
    runner_root.mkdir()
    service_dir.mkdir()
    owner = os.getuid()
    group = os.getgid()

    for number in range(1, 5):
        runner = runner_root / f"elspeth-nyx-{number}"
        runner.mkdir(mode=0o700 if secure else 0o755)
        for name in (".credentials", ".credentials_rsaparams", ".env", ".path"):
            path = runner / name
            path.write_text("test-only\n", encoding="utf-8")
            path.chmod(0o600 if secure else 0o664)

        unit = service_dir / f"actions.runner.dta-au-elspeth.nyx-elspeth-{number}.service"
        unit.write_text(
            f"[Service]\nUser={owner}\nGroup={group}\nWorkingDirectory={runner}\n",
            encoding="utf-8",
        )
        unit.chmod(0o644 if secure else 0o664)
        if secure:
            drop_in = service_dir / f"{unit.name}.d" / "10-elspeth-hardening.conf"
            drop_in.parent.mkdir()
            drop_in.write_text(HARDENING, encoding="utf-8")
            drop_in.chmod(0o644)

    return runner_root, service_dir


def _audit(runner_root: Path, service_dir: Path) -> subprocess.CompletedProcess[str]:
    assert SCRIPT.is_file(), "runner hardening implementation is missing"
    return subprocess.run(
        [
            str(SCRIPT),
            "--check",
            "--runner-root",
            str(runner_root),
            "--service-dir",
            str(service_dir),
            "--owner",
            str(os.getuid()),
            "--group",
            str(os.getgid()),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_runner_audit_rejects_world_readable_credentials_and_missing_hardening(tmp_path: Path) -> None:
    runner_root, service_dir = _runner_layout(tmp_path, secure=False)

    result = _audit(runner_root, service_dir)

    assert result.returncode == 1
    assert "mode 664, expected 600" in result.stdout
    assert "missing hardening drop-in" in result.stdout
    assert "runner_host_controls=FAIL" in result.stdout


def test_runner_audit_accepts_four_hardened_runner_services(tmp_path: Path) -> None:
    runner_root, service_dir = _runner_layout(tmp_path, secure=True)

    result = _audit(runner_root, service_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "runner_count=4" in result.stdout
    assert "runner_host_controls=PASS" in result.stdout


def test_runner_audit_rejects_locked_directory_for_current_privileges(tmp_path: Path) -> None:
    runner_root, service_dir = _runner_layout(tmp_path, secure=True)
    locked_runner = runner_root / "elspeth-nyx-2"
    locked_runner.chmod(0o000)

    try:
        # CI containers can traverse mode-000 directories with CAP_DAC_OVERRIDE.
        can_traverse = os.access(locked_runner, os.X_OK, effective_ids=True)
        result = _audit(runner_root, service_dir)
    finally:
        locked_runner.chmod(0o700)

    if can_traverse:
        assert result.returncode == 1, result.stdout + result.stderr
        assert f"{locked_runner} mode 0, expected 700" in result.stdout
        assert "runner_host_controls=FAIL failures=1" in result.stdout
        assert result.stderr == ""
    else:
        assert result.returncode == 2, result.stdout + result.stderr
        assert f"cannot traverse {locked_runner}" in result.stderr
        assert "rerun --check as root" in result.stderr


@pytest.mark.parametrize(
    "missing_name",
    [".credentials", ".credentials_rsaparams", ".env", ".path"],
)
def test_runner_audit_rejects_missing_custody_files(tmp_path: Path, missing_name: str) -> None:
    runner_root, service_dir = _runner_layout(tmp_path, secure=True)
    (runner_root / "elspeth-nyx-2" / missing_name).unlink()

    result = _audit(runner_root, service_dir)

    assert result.returncode == 1
    assert f"missing {missing_name}" in result.stdout
