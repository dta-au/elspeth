"""Immutable source copies retained independently of editable session storage.

The store has no automatic pruning: admitting a durable run creates a retention
obligation which lasts through recovery and audit retention. A session edit or
blob cleanup therefore cannot remove these bytes.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO, cast

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.execution.protocol import FrozenRunSettings


@dataclass(frozen=True, slots=True)
class RetainedRunInput:
    original_path: str
    retained_path: str
    content_hash: str
    size_bytes: int


class RetainedInputUnavailable(ValueError):
    """The admitted input cannot be restored without changing its bytes."""


def read_retained_input(retained: RetainedRunInput) -> bytes:
    try:
        content = Path(retained.retained_path).read_bytes()
    except FileNotFoundError as exc:
        raise RetainedInputUnavailable("retained_input_missing") from exc
    if len(content) != retained.size_bytes or hashlib.sha256(content).hexdigest() != retained.content_hash:
        raise RetainedInputUnavailable("retained_input_changed")
    return content


def verify_retained_input(retained: RetainedRunInput) -> None:
    try:
        with Path(retained.retained_path).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            size = stream.tell()
    except FileNotFoundError as exc:
        raise RetainedInputUnavailable("retained_input_missing") from exc
    if digest != retained.content_hash or size != retained.size_bytes:
        raise RetainedInputUnavailable("retained_input_changed")


def retain_source_file(source: Path, *, root: Path) -> RetainedRunInput:
    with source.open("rb") as original:
        return _retain_stream(original, original_path=str(source), root=root)


def retain_source_bytes(content: bytes, *, original_path: str, root: Path) -> RetainedRunInput:
    """Retain bytes already verified under blob custody, without rereading a path."""
    return _retain_stream(io.BytesIO(content), original_path=original_path, root=root)


def _retain_stream(original: BinaryIO, *, original_path: str, root: Path) -> RetainedRunInput:
    """Publish a complete, fsynced content copy with an atomic no-overwrite link."""
    root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".retaining-", dir=root)
    temporary = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "wb") as destination:
            while chunk := original.read(1024 * 1024):
                destination.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        content_hash = digest.hexdigest()
        retained_path = root / f"{content_hash}{Path(original_path).suffix}"
        with suppress(FileExistsError):
            os.link(temporary, retained_path)
        directory_descriptor = os.open(root, os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        retained = RetainedRunInput(original_path, str(retained_path), content_hash, size)
        verify_retained_input(retained)
        return retained
    finally:
        temporary.unlink(missing_ok=True)


def retain_execution_inputs(frozen: FrozenRunSettings, *, root: Path) -> tuple[FrozenRunSettings, tuple[RetainedRunInput, ...]]:
    """Keep path sources on admitted bytes; preserve their audit-safe identity."""
    config = cast(dict[str, Any], deep_thaw(frozen.executable_config))
    sources: list[Any] = []
    if "sources" in config:
        sources.extend(config["sources"].values())
    if "source" in config:
        sources.append(config["source"])
    retained_inputs: list[RetainedRunInput] = []
    for source in sources:
        options = source["options"]
        if "path" not in options:
            continue
        path = options["path"]
        if not isinstance(path, str):
            raise TypeError("Admitted source path must be a string")
        retained = retain_source_file(Path(path), root=root)
        retained_inputs.append(retained)
        options["path"] = retained.retained_path
    return replace(frozen, executable_config=config), tuple(retained_inputs)
