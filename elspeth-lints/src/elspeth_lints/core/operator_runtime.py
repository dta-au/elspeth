"""Admission for the separately installed, OS-authenticated signer runtime.

These checks do not turn editable Python into a trusted process. The static
native launcher authenticates startup paths before starting Python; only its
protected bootstrap calls the private admission function, before loading keys.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

OPERATOR_ENTRYPOINT = "/usr/local/libexec/elspeth-operator"
OPERATOR_ARTIFACT_ROOT = Path("/opt/elspeth/operator")
OPERATOR_LOADER = Path("/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2")
_PRIVATE_DIRECTORY = Path("/run/elspeth/operator")
_HANDOFF_MAGIC = b"ELSPETHOP2\x00"
_MAX_HANDOFF_BYTES = 1_048_576
_MANIFEST_NAME = "runtime-manifest.json"
_MAX_MANIFEST_BYTES = 4_194_304
_SIGNER_ENV_NAMES = frozenset(
    {
        "ELSPETH_JUDGE_METADATA_HMAC_KEY",
        "ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE",
        "ELSPETH_JUDGE_OVERRIDE_TOKEN",
        "ELSPETH_JUDGE_OVERRIDE_TOKEN_SHA256",
    }
)


class OperatorRuntimeUnavailableError(RuntimeError):
    """The protected operator runtime is absent or cannot be authenticated."""


def _python_profile(minor: int) -> tuple[Path, Path]:
    """Select one compiled Ubuntu profile, never an ambient executable path."""
    if type(minor) is not int:
        raise OperatorRuntimeUnavailableError("unsupported operator Python profile")
    if minor == 12:
        return Path("/usr/bin/python3.12"), Path("/usr/lib/python3.12")
    if minor == 13:
        return Path("/usr/bin/python3.13"), Path("/usr/lib/python3.13")
    raise OperatorRuntimeUnavailableError("unsupported operator Python profile")


def _validate_python_startup(minor: int, *, version: tuple[int, int], search_path: tuple[str, ...]) -> tuple[Path, Path]:
    interpreter, stdlib = _python_profile(minor)
    expected_path = (str(stdlib), str(stdlib / "lib-dynload"), str(OPERATOR_ARTIFACT_ROOT / "lib"))
    if version != (3, minor) or search_path != expected_path:
        raise OperatorRuntimeUnavailableError("operator Python profile does not match interpreter startup")
    return interpreter, stdlib


@dataclass(frozen=True, slots=True)
class OperatorRuntimeEvidence:
    """Observed startup identity, created before signer values enter Python."""

    artifact_root: Path
    artifact_id: str
    operator_uid: int
    candidate_uid: int
    invocation_cwd: Path
    argv: tuple[str, ...]
    python_minor: int = 12

    def __post_init__(self) -> None:
        _python_profile(self.python_minor)

    @property
    def python_version(self) -> tuple[int, int]:
        return (3, self.python_minor)

    @property
    def interpreter(self) -> Path:
        return _python_profile(self.python_minor)[0]

    @property
    def stdlib(self) -> Path:
        return _python_profile(self.python_minor)[1]


@dataclass(frozen=True, slots=True)
class _NativeHandoff:
    artifact_root: Path
    invocation_cwd: Path
    operator_uid: int
    candidate_uid: int
    python_minor: int
    argv: tuple[str, ...]
    selected_environment: tuple[tuple[str, str], ...] = field(repr=False)


_runtime_evidence: OperatorRuntimeEvidence | None = None


def current_operator_runtime() -> OperatorRuntimeEvidence | None:
    """Return established process authority; never initialize from ambient state."""
    return _runtime_evidence


def require_operator_runtime() -> OperatorRuntimeEvidence:
    """Refuse ordinary Python callers before they load or use signer values."""
    evidence = current_operator_runtime()
    if evidence is None:
        raise OperatorRuntimeUnavailableError(f"key-bearing operation requires the protected operator runtime: {OPERATOR_ENTRYPOINT}")
    return evidence


def runtime_private_directory() -> Path:
    """Return the separately provisioned signer scratch directory, ignoring env."""
    evidence = require_operator_runtime()
    validate_protected_path(_PRIVATE_DIRECTORY.parent, directory=True)
    try:
        metadata = _PRIVATE_DIRECTORY.lstat()
    except OSError as exc:
        raise OperatorRuntimeUnavailableError("private operator directory is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != evidence.operator_uid or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise OperatorRuntimeUnavailableError("private operator directory ownership or mode is unsafe")
    return _PRIVATE_DIRECTORY


def validate_protected_path(path: Path, *, directory: bool = False) -> None:
    """Require root-owned, non-writable, symlink-free absolute path ancestry."""
    if not path.is_absolute() or ".." in path.parts:
        raise OperatorRuntimeUnavailableError("protected path must be absolute and canonical")
    for current in (*reversed(path.parents), path):
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise OperatorRuntimeUnavailableError("protected runtime path is unavailable") from exc
        if metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise OperatorRuntimeUnavailableError("protected runtime path ownership or mode is unsafe")
        expected_directory = current != path or directory
        kind_ok = stat.S_ISDIR(metadata.st_mode) if expected_directory else stat.S_ISREG(metadata.st_mode)
        if not kind_ok:
            raise OperatorRuntimeUnavailableError("protected runtime path has an unsupported object type")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OperatorRuntimeUnavailableError("duplicate runtime manifest field")
        result[key] = value
    return result


def _read_manifest(artifact_root: Path) -> tuple[bytes, dict[str, object]]:
    manifest_path = artifact_root / _MANIFEST_NAME
    validate_protected_path(artifact_root, directory=True)
    validate_protected_path(manifest_path)
    with manifest_path.open("rb") as stream:
        raw = stream.read(_MAX_MANIFEST_BYTES + 1)
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise OperatorRuntimeUnavailableError("runtime manifest exceeds size limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (ValueError, UnicodeError) as exc:
        raise OperatorRuntimeUnavailableError("invalid runtime manifest JSON") from exc
    if not isinstance(value, dict) or set(value) != {"schema", "operator_uid", "candidate_uid", "files"}:
        raise OperatorRuntimeUnavailableError("invalid runtime manifest fields")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise OperatorRuntimeUnavailableError("unsupported runtime manifest schema")
    for field_name in ("operator_uid", "candidate_uid"):
        uid = value[field_name]
        if type(uid) is not int or uid < 0:
            raise OperatorRuntimeUnavailableError("invalid runtime principal")
    if value["operator_uid"] == value["candidate_uid"]:
        raise OperatorRuntimeUnavailableError("operator and candidate principals must differ")
    return raw, value


def validate_artifact_manifest(artifact_root: Path) -> str:
    """Check the exact protected artifact inventory and return manifest SHA256."""
    raw, manifest = _read_manifest(artifact_root)
    files = manifest["files"]
    if not isinstance(files, dict) or not files or len(files) > 65_536:
        raise OperatorRuntimeUnavailableError("invalid runtime artifact inventory")
    expected: set[str] = set()
    for relative, digest in files.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise OperatorRuntimeUnavailableError("invalid runtime artifact inventory entry")
        logical = PurePosixPath(relative)
        if (
            logical.is_absolute()
            or str(logical) != relative
            or any(part in {".", ".."} for part in logical.parts)
            or relative == _MANIFEST_NAME
            or not logical.parts
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise OperatorRuntimeUnavailableError("invalid runtime artifact inventory entry")
        expected.add(relative)
    observed: set[str] = set()
    for parent, directories, filenames in os.walk(artifact_root, followlinks=False):
        directory = Path(parent)
        for name in directories:
            validate_protected_path(directory / name, directory=True)
        for name in filenames:
            path = directory / name
            relative = path.relative_to(artifact_root).as_posix()
            validate_protected_path(path)
            if relative != _MANIFEST_NAME:
                observed.add(relative)
    if observed != expected:
        raise OperatorRuntimeUnavailableError("runtime artifact inventory mismatch")
    for relative in sorted(expected):
        with (artifact_root / relative).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != files[relative]:
            raise OperatorRuntimeUnavailableError("runtime artifact digest mismatch")
    return hashlib.sha256(raw).hexdigest()


def _selected_rekey_names(argv: tuple[str, ...]) -> set[str]:
    selected = set(_SIGNER_ENV_NAMES)
    for index, argument in enumerate(argv):
        for option in ("--old-key-env", "--new-key-env"):
            if argument == option and index + 1 < len(argv):
                selected.add(argv[index + 1])
            elif argument.startswith(option + "="):
                selected.add(argument[len(option) + 1 :])
    return selected


def _decode_native_handoff(raw: bytes) -> _NativeHandoff:
    if len(raw) > _MAX_HANDOFF_BYTES or not raw.startswith(_HANDOFF_MAGIC):
        raise OperatorRuntimeUnavailableError("invalid native startup handoff")
    position = len(_HANDOFF_MAGIC)

    def integer() -> int:
        nonlocal position
        if position + 4 > len(raw):
            raise OperatorRuntimeUnavailableError("truncated native startup handoff")
        value = int.from_bytes(raw[position : position + 4], "big")
        position += 4
        return value

    def string() -> str:
        nonlocal position
        length = integer()
        if position + length > len(raw):
            raise OperatorRuntimeUnavailableError("truncated native startup handoff")
        try:
            result = raw[position : position + length].decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise OperatorRuntimeUnavailableError("native startup handoff must be UTF-8") from exc
        position += length
        if "\x00" in result:
            raise OperatorRuntimeUnavailableError("invalid native startup string")
        return result

    artifact_root, cwd = Path(string()), Path(string())
    operator_uid, candidate_uid = integer(), integer()
    python_minor = integer()
    _python_profile(python_minor)
    argument_count = integer()
    if argument_count > 1024:
        raise OperatorRuntimeUnavailableError("too many operator arguments")
    argv = tuple(string() for _ in range(argument_count))
    environment_count = integer()
    if environment_count > 64:
        raise OperatorRuntimeUnavailableError("too many selected signer values")
    environment = tuple((string(), string()) for _ in range(environment_count))
    if position != len(raw) or not artifact_root.is_absolute() or not cwd.is_absolute():
        raise OperatorRuntimeUnavailableError("invalid native startup handoff fields")
    allowed = _selected_rekey_names(argv)
    names: set[str] = set()
    for name, _ in environment:
        if not name or "=" in name or name not in allowed or name in names:
            raise OperatorRuntimeUnavailableError("invalid selected signer value")
        names.add(name)
    return _NativeHandoff(artifact_root, cwd, operator_uid, candidate_uid, python_minor, argv, environment)


def _admit_native_runtime() -> OperatorRuntimeEvidence:
    """Protected bootstrap-only admission; there is no ambient lazy fallback."""
    global _runtime_evidence
    if _runtime_evidence is not None:
        raise OperatorRuntimeUnavailableError("operator startup already admitted")
    if not sys.flags.isolated or not sys.flags.no_site or not sys.dont_write_bytecode:
        raise OperatorRuntimeUnavailableError("operator interpreter startup is not isolated")
    expected_module = OPERATOR_ARTIFACT_ROOT / "lib/elspeth_lints/core/operator_runtime.py"
    if Path(__file__) != expected_module or Path(os.readlink("/proc/self/exe")) != OPERATOR_LOADER:
        raise OperatorRuntimeUnavailableError("operator startup artifact is not the installed runtime")
    validate_protected_path(Path(OPERATOR_ENTRYPOINT))
    validate_protected_path(OPERATOR_LOADER)
    descriptor = 3
    try:
        metadata = os.fstat(descriptor)
        required_seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & required_seals != required_seals:
            raise OperatorRuntimeUnavailableError("native startup handoff is not sealed")
        if metadata.st_size > _MAX_HANDOFF_BYTES or not stat.S_ISREG(metadata.st_mode):
            raise OperatorRuntimeUnavailableError("invalid native startup handoff file")
        raw = os.pread(descriptor, _MAX_HANDOFF_BYTES + 1, 0)
    except OSError as exc:
        raise OperatorRuntimeUnavailableError("native startup handoff unavailable") from exc
    finally:
        with suppress(OSError):
            os.close(descriptor)
    handoff = _decode_native_handoff(raw)
    interpreter, stdlib = _validate_python_startup(
        handoff.python_minor,
        version=(sys.version_info.major, sys.version_info.minor),
        search_path=tuple(sys.path),
    )
    validate_protected_path(interpreter)
    validate_protected_path(stdlib, directory=True)
    validate_protected_path(stdlib / "lib-dynload", directory=True)
    if handoff.artifact_root != OPERATOR_ARTIFACT_ROOT or handoff.argv != tuple(sys.argv[1:]):
        raise OperatorRuntimeUnavailableError("native startup operation binding mismatch")
    expected_command = (
        str(OPERATOR_LOADER),
        "--inhibit-cache",
        "--library-path",
        str(OPERATOR_ARTIFACT_ROOT / "platform/lib"),
        "--glibc-hwcaps-mask",
        "",
        str(interpreter),
        "-I",
        "-S",
        "-B",
        str(OPERATOR_ARTIFACT_ROOT / "bootstrap.py"),
        *handoff.argv,
    )
    with Path("/proc/self/cmdline").open("rb") as command_stream:
        command_bytes = command_stream.read(_MAX_HANDOFF_BYTES + 1)
    if command_bytes != b"\x00".join(argument.encode("utf-8") for argument in expected_command) + b"\x00":
        raise OperatorRuntimeUnavailableError("native loader startup binding mismatch")
    artifact_id = validate_artifact_manifest(OPERATOR_ARTIFACT_ROOT)
    _, manifest = _read_manifest(OPERATOR_ARTIFACT_ROOT)
    if (
        os.getuid() != os.geteuid()
        or os.getuid() != manifest["operator_uid"]
        or metadata.st_uid != os.getuid()
        or handoff.operator_uid != os.getuid()
        or handoff.candidate_uid != manifest["candidate_uid"]
        or handoff.operator_uid == handoff.candidate_uid
    ):
        raise OperatorRuntimeUnavailableError("operator principal is not authorized")
    evidence = OperatorRuntimeEvidence(
        OPERATOR_ARTIFACT_ROOT,
        artifact_id,
        handoff.operator_uid,
        handoff.candidate_uid,
        handoff.invocation_cwd,
        handoff.argv,
        handoff.python_minor,
    )
    os.chdir(handoff.invocation_cwd)
    for name, value in handoff.selected_environment:
        os.environ[name] = value
    _runtime_evidence = evidence
    return evidence
