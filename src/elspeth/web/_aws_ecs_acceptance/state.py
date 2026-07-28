"""Protected AWS ECS acceptance state ownership."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from .contracts import (
    _SHA256_PATTERN,
    MAX_STATE_FILE_BYTES,
    AcceptanceInputError,
    AcceptanceStateError,
    _parse_utc_z_timestamp,
)

_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "session_id",
        "tutorial_session_id",
        "blob_id",
        "run_id",
        "landscape_run_id",
        "artifact_id",
        "uploaded_sha256",
        "blob_sha256",
        "artifact_sha256",
        "run_status",
        "source_rows",
        "failed_tokens",
        "captured_at",
        "completed_at",
    }
)


@dataclass(frozen=True, slots=True)
class AcceptanceCredentials:
    """One mutually exclusive acceptance authentication mode."""

    mode: Literal["local", "bearer"]
    username: str | None = None
    password: str | None = None
    bearer_token: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        username = env.get("ELSPETH_ACCEPTANCE_USERNAME") or None
        password = env.get("ELSPETH_ACCEPTANCE_PASSWORD") or None
        bearer_token = env.get("ELSPETH_ACCEPTANCE_BEARER_TOKEN") or None
        has_local_part = username is not None or password is not None
        has_local = username is not None and password is not None
        if bearer_token is not None and not has_local_part:
            return cls(mode="bearer", bearer_token=bearer_token)
        if has_local and bearer_token is None:
            return cls(mode="local", username=username, password=password)
        raise AcceptanceInputError("acceptance authentication must use exactly one complete environment mode")


@dataclass(frozen=True, slots=True)
class AcceptanceState:
    """Closed, non-secret evidence needed to re-verify one captured run."""

    schema_version: int
    session_id: str
    tutorial_session_id: str
    blob_id: str
    run_id: str
    landscape_run_id: str
    artifact_id: str
    uploaded_sha256: str
    blob_sha256: str
    artifact_sha256: str
    run_status: Literal["completed"]
    source_rows: int
    failed_tokens: int
    captured_at: str
    completed_at: str

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        if set(data) != _STATE_FIELDS:
            raise AcceptanceStateError("acceptance state schema is invalid")
        if data["schema_version"] != 1 or type(data["schema_version"]) is not int:
            raise AcceptanceStateError("acceptance state schema is invalid")

        identities = {
            field: _state_string(data, field)
            for field in ("session_id", "tutorial_session_id", "blob_id", "run_id", "landscape_run_id", "artifact_id")
        }
        for value in identities.values():
            try:
                parsed = uuid.UUID(value)
            except (ValueError, AttributeError):
                raise AcceptanceStateError("acceptance state schema is invalid") from None
            if str(parsed) != value:
                raise AcceptanceStateError("acceptance state schema is invalid")

        hashes = {field: _state_string(data, field) for field in ("uploaded_sha256", "blob_sha256", "artifact_sha256")}
        if any(_SHA256_PATTERN.fullmatch(value) is None for value in hashes.values()):
            raise AcceptanceStateError("acceptance state schema is invalid")

        run_status = _state_string(data, "run_status")
        source_rows = data["source_rows"]
        failed_tokens = data["failed_tokens"]
        if run_status != "completed" or type(source_rows) is not int or source_rows <= 0:
            raise AcceptanceStateError("acceptance state schema is invalid")
        if type(failed_tokens) is not int or failed_tokens != 0:
            raise AcceptanceStateError("acceptance state schema is invalid")

        captured_at = _state_timestamp(data, "captured_at")
        completed_at = _state_timestamp(data, "completed_at")
        if _parse_state_timestamp(completed_at) < _parse_state_timestamp(captured_at):
            raise AcceptanceStateError("acceptance state schema is invalid")

        return cls(
            schema_version=1,
            session_id=identities["session_id"],
            tutorial_session_id=identities["tutorial_session_id"],
            blob_id=identities["blob_id"],
            run_id=identities["run_id"],
            landscape_run_id=identities["landscape_run_id"],
            artifact_id=identities["artifact_id"],
            uploaded_sha256=hashes["uploaded_sha256"],
            blob_sha256=hashes["blob_sha256"],
            artifact_sha256=hashes["artifact_sha256"],
            run_status="completed",
            source_rows=source_rows,
            failed_tokens=failed_tokens,
            captured_at=captured_at,
            completed_at=completed_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "tutorial_session_id": self.tutorial_session_id,
            "blob_id": self.blob_id,
            "run_id": self.run_id,
            "landscape_run_id": self.landscape_run_id,
            "artifact_id": self.artifact_id,
            "uploaded_sha256": self.uploaded_sha256,
            "blob_sha256": self.blob_sha256,
            "artifact_sha256": self.artifact_sha256,
            "run_status": self.run_status,
            "source_rows": self.source_rows,
            "failed_tokens": self.failed_tokens,
            "captured_at": self.captured_at,
            "completed_at": self.completed_at,
        }


def _state_string(data: Mapping[str, object], field: str) -> str:
    value = data[field]
    if type(value) is not str or not value:
        raise AcceptanceStateError("acceptance state schema is invalid")
    return value


def _parse_state_timestamp(value: str) -> datetime:
    try:
        return _parse_utc_z_timestamp(value)
    except ValueError:
        raise AcceptanceStateError("acceptance state schema is invalid") from None


def _state_timestamp(data: Mapping[str, object], field: str) -> str:
    value = _state_string(data, field)
    _parse_state_timestamp(value)
    return value


def _validate_protected_stat(stat_result: os.stat_result) -> None:
    if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_uid != os.getuid() or stat_result.st_mode & 0o077:
        raise AcceptanceStateError("acceptance state must be a regular owner-only file")


def _validate_existing_state_destination(path: Path) -> None:
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise AcceptanceStateError("acceptance state destination validation failed") from None
    _validate_protected_stat(stat_result)


def write_acceptance_state(path: Path, state: AcceptanceState) -> None:
    """Atomically persist a closed state document beside its destination."""

    _validate_existing_state_destination(path)
    payload = json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > MAX_STATE_FILE_BYTES:
        raise AcceptanceStateError("acceptance state is too large")

    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError:
        raise AcceptanceStateError("acceptance state write failed") from None
    finally:
        if temporary_path is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_path)


def read_acceptance_state(path: Path) -> AcceptanceState:
    """Read and validate one bounded protected state document without following links."""

    try:
        before = path.lstat()
        _validate_protected_stat(before)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except AcceptanceStateError:
        raise
    except OSError:
        raise AcceptanceStateError("acceptance state must be a regular owner-only file") from None

    try:
        opened = os.fstat(descriptor)
        _validate_protected_stat(opened)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise AcceptanceStateError("acceptance state changed during protected read")
        if opened.st_size > MAX_STATE_FILE_BYTES:
            raise AcceptanceStateError("acceptance state is too large")
        chunks: list[bytes] = []
        remaining = MAX_STATE_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > MAX_STATE_FILE_BYTES:
            raise AcceptanceStateError("acceptance state is too large")
    except AcceptanceStateError:
        raise
    except OSError:
        raise AcceptanceStateError("acceptance state read failed") from None
    finally:
        os.close(descriptor)

    try:
        decoded = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AcceptanceStateError("acceptance state schema is invalid") from None
    if not isinstance(decoded, dict):
        raise AcceptanceStateError("acceptance state schema is invalid")
    return AcceptanceState.from_dict(decoded)
