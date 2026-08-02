"""Regression tests for repository-checkout state guards."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import conftest as root_conftest


def _start_repo_elspeth_guard(
    repo_elspeth: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setattr(root_conftest, "_REPO_ELSPETH", repo_elspeth)
    guard = root_conftest._refuse_in_repo_elspeth_writes.__wrapped__()
    next(guard)
    return guard


def test_repo_elspeth_guard_detects_file_created_beneath_existing_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = tmp_path / ".elspeth" / "payloads"
    payloads.mkdir(parents=True)
    guard = _start_repo_elspeth_guard(payloads.parent, monkeypatch)

    leaked = payloads / "leaked.bin"
    directory_fd = os.open(payloads, os.O_RDONLY | os.O_DIRECTORY)
    try:
        leaked_fd = os.open("leaked.bin", os.O_WRONLY | os.O_CREAT, dir_fd=directory_fd)
        try:
            os.write(leaked_fd, b"leaked")
        finally:
            os.close(leaked_fd)
    finally:
        os.close(directory_fd)

    assert leaked.exists()
    with pytest.raises(pytest.fail.Exception, match=r"Contents under .*\.elspeth changed while this worker ran"):
        next(guard)


def test_repo_elspeth_guard_attributes_fd_relative_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dir_fd mkdir (the content-store pattern) fails at the call, attributed."""
    repo_elspeth = tmp_path / ".elspeth"
    repo_elspeth.mkdir()
    guard = _start_repo_elspeth_guard(repo_elspeth, monkeypatch)

    directory_fd = os.open(repo_elspeth, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(pytest.fail.Exception, match=r"Test wrote into the repository checkout"):
            os.mkdir("audit-export-content-store", dir_fd=directory_fd)
    finally:
        os.close(directory_fd)

    assert not (repo_elspeth / "audit-export-content-store").exists()
    with pytest.raises(StopIteration):
        next(guard)


def test_repo_elspeth_guard_detects_overwrite_beneath_existing_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = tmp_path / ".elspeth" / "payloads" / "existing.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"before")
    guard = _start_repo_elspeth_guard(payload.parents[1], monkeypatch)

    with pytest.raises(pytest.fail.Exception, match=r"Test wrote into the repository checkout"):
        payload.write_bytes(b"AFTER!")

    assert payload.read_bytes() == b"before"
    with pytest.raises(StopIteration):
        next(guard)
