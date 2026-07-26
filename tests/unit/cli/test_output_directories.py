"""Tests for CLI output directory preflight."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from elspeth.cli import _ensure_output_directories
from elspeth.core.config import ElspethSettings
from elspeth.core.payload_store import FilesystemPayloadStore


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are required for this regression")
def test_ensure_output_directories_creates_payload_store_owner_only_under_permissive_umask(tmp_path: Path) -> None:
    payload_path = tmp_path / "payloads"
    config = ElspethSettings(
        sources={"primary": {"plugin": "csv", "on_success": "output"}},
        sinks={"output": {"plugin": "json", "on_write_failure": "discard"}},
        landscape={"url": f"sqlite:///{tmp_path / 'state' / 'audit.db'}"},
        payload_store={"base_path": payload_path},
    )

    previous_umask = os.umask(0o002)
    try:
        errors = _ensure_output_directories(config)
    finally:
        os.umask(previous_umask)

    assert errors == []
    assert stat.S_IMODE(payload_path.stat().st_mode) == 0o700
    FilesystemPayloadStore(payload_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX O_NOFOLLOW semantics are required for this regression")
def test_ensure_output_directories_does_not_chmod_raced_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload_path = tmp_path / "payloads"
    victim_path = tmp_path / "victim"
    victim_path.write_text("preserve my permissions", encoding="utf-8")
    victim_path.chmod(0o644)
    config = ElspethSettings(
        sources={"primary": {"plugin": "csv", "on_success": "output"}},
        sinks={"output": {"plugin": "json", "on_write_failure": "discard"}},
        landscape={"url": f"sqlite:///{tmp_path / 'state' / 'audit.db'}"},
        payload_store={"base_path": payload_path},
    )
    original_mkdir = Path.mkdir

    def mkdir_then_swap(path: Path, *args: object, **kwargs: object) -> None:
        original_mkdir(path, *args, **kwargs)
        if path == payload_path:
            path.rmdir()
            path.symlink_to(victim_path)

    monkeypatch.setattr(Path, "mkdir", mkdir_then_swap)

    errors = _ensure_output_directories(config)

    assert errors
    assert payload_path.is_symlink()
    assert stat.S_IMODE(victim_path.stat().st_mode) == 0o644
