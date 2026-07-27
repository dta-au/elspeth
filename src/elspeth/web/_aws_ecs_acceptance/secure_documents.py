"""Protected-document reads, writes, and serialized mutation."""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import json
import os
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from functools import wraps
from pathlib import Path
from typing import Any, cast

from .contracts import MAX_CONTROL_DOCUMENT_BYTES, AcceptanceCheckError

_fcntl: Any
if os.name == "posix" and importlib.util.find_spec("fcntl") is not None:
    import fcntl as _fcntl
else:
    _fcntl = None


def _validate_control_parent(path: Path, *, check: str = "control_manifest_parent") -> None:
    try:
        parent = path.parent.lstat()
    except OSError:
        raise AcceptanceCheckError(check) from None
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise AcceptanceCheckError(check)


def _read_protected_document(path: Path, *, check: str) -> dict[str, object]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077:
            raise AcceptanceCheckError(check)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except AcceptanceCheckError:
        raise
    except OSError:
        raise AcceptanceCheckError(check) from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_mode & 0o077
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_size > MAX_CONTROL_DOCUMENT_BYTES
        ):
            raise AcceptanceCheckError(check)
        content = os.read(descriptor, MAX_CONTROL_DOCUMENT_BYTES + 1)
        if len(content) > MAX_CONTROL_DOCUMENT_BYTES or os.read(descriptor, 1):
            raise AcceptanceCheckError(check)
    except AcceptanceCheckError:
        raise
    except OSError:
        raise AcceptanceCheckError(check) from None
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AcceptanceCheckError(check) from None
    if not isinstance(decoded, dict):
        raise AcceptanceCheckError(check)
    return decoded


def _open_receipt_manifest_lock(lock_path: Path, *, check: str) -> int:
    """Open an existing exact-0600 lock, or atomically publish a new one."""

    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        return os.open(lock_path, flags)
    except FileNotFoundError:
        pass
    except OSError:
        raise AcceptanceCheckError(check) from None
    temporary_path: str | None = None
    temporary_descriptor: int | None = None
    try:
        temporary_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{lock_path.name}.",
            suffix=".tmp",
            dir=lock_path.parent,
        )
        os.fchmod(temporary_descriptor, 0o600)
        try:
            os.link(temporary_path, lock_path, follow_symlinks=False)
        except FileExistsError:
            os.close(temporary_descriptor)
            temporary_descriptor = None
            return os.open(lock_path, flags)
        os.unlink(temporary_path)
        temporary_path = None
        descriptor = temporary_descriptor
        temporary_descriptor = None
        return descriptor
    except OSError:
        raise AcceptanceCheckError(check) from None
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_path is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_path)


def _flock_retry_interrupted(descriptor: int, operation: int) -> None:
    """Complete a flock operation across transient signal interruptions."""

    while True:
        try:
            _fcntl.flock(descriptor, operation)
        except InterruptedError:
            continue
        return


@contextlib.contextmanager
def _receipt_manifest_write_lock(path: Path, *, check: str) -> Iterator[None]:
    """Exclude cross-process receipt writers with one crash-released sidecar."""

    _validate_control_parent(path, check=check)
    if _fcntl is None:
        raise AcceptanceCheckError(check)
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor = _open_receipt_manifest_lock(lock_path, check=check)
    try:
        try:
            opened = os.fstat(descriptor)
            linked = lock_path.lstat()
        except OSError:
            raise AcceptanceCheckError(check) from None
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise AcceptanceCheckError(check)
        try:
            _flock_retry_interrupted(descriptor, _fcntl.LOCK_EX)
        except OSError:
            raise AcceptanceCheckError(check) from None
        try:
            yield
        finally:
            try:
                _flock_retry_interrupted(descriptor, _fcntl.LOCK_UN)
            except OSError:
                raise AcceptanceCheckError(check) from None
    finally:
        os.close(descriptor)


def _serialized_control_manifest_write[**P, R](
    operation: Callable[P, R],
) -> Callable[P, R]:
    """Hold the manifest sidecar lock across one complete read-modify-write."""

    operation_signature = inspect.signature(operation)
    path_parameter = next(iter(operation_signature.parameters))

    @wraps(operation)
    def serialized(
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        bound = operation_signature.bind(*args, **kwargs)
        path = cast(Path, bound.arguments[path_parameter])
        with _receipt_manifest_write_lock(path, check="control_manifest_file"):
            return operation(*args, **kwargs)

    return serialized


def _write_protected_document(
    path: Path,
    payload: Mapping[str, object],
    *,
    create: bool,
    exists_check: str,
    write_check: str,
    parent_check: str = "control_manifest_parent",
) -> None:
    _validate_control_parent(path, check=parent_check)
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(content) > MAX_CONTROL_DOCUMENT_BYTES:
        raise AcceptanceCheckError(write_check)
    if create:
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise AcceptanceCheckError(write_check) from None
        else:
            raise AcceptanceCheckError(exists_check)
    else:
        _read_protected_document(path, check=write_check)

    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if create:
            try:
                os.link(temporary_path, path, follow_symlinks=False)
            except FileExistsError:
                raise AcceptanceCheckError(exists_check) from None
            os.unlink(temporary_path)
            temporary_path = None
        else:
            os.replace(temporary_path, path)
            temporary_path = None
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except AcceptanceCheckError:
        raise
    except OSError:
        raise AcceptanceCheckError(write_check) from None
    finally:
        if temporary_path is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_path)
