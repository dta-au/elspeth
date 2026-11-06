"""Bounded offline image/release inspection; supplied code is never executed.

Static dependency checks establish declared ELF edges, not dynamic-import,
provider, service-manager or host qualification. No network/resolver is used.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import struct
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILE = 64 * 1024 * 1024
MAX_IMAGE = 512 * 1024 * 1024
MAX_RELEASE = 2 * 1024 * 1024 * 1024
MAX_FILES = 65536
PROFILE = "ubuntu-x86_64-offline-v1"
TRANSPORTS = ("openrouter", "claude_agent_sdk", "codex_cli")
ROLE_ROOTS = {
    "signer": "opt/elspeth/operator",
    "judge_supervisor": "opt/elspeth/judge-supervisor",
    "openrouter": "opt/elspeth/judge-workers/openrouter",
    "claude_agent_sdk": "opt/elspeth/judge-workers/claude_agent_sdk",
    "codex_cli": "opt/elspeth/judge-workers/codex_cli",
    "baseline_supervisor": "opt/elspeth/baseline-supervisor",
    "baseline_worker": "opt/elspeth/baseline-worker",
}
NATIVE_PATH = "usr/local/libexec/elspeth-operator"
LIBRARY_ROOTS = ("lib/x86_64-linux-gnu", "usr/lib/x86_64-linux-gnu", "lib", "usr/lib")
_UNIT_IDENTITIES = {
    "judge": "f77d273772ea5ede5905ceb06b26296e22484e6dabb6ce33bea325ae8e8b38be",
    "baseline": "0a8d1db6b2501b9a0ccc572f48267aa590511067d664c56f3e6cce47ebeb212c",
}


class ArtifactError(ValueError):
    """Supplied bytes, bindings or installation targets do not meet the profile."""


@dataclass(frozen=True, slots=True)
class ArtifactFile:
    data: bytes
    mode: int


@dataclass(frozen=True, slots=True)
class ElfInfo:
    interpreter: str | None
    needed: tuple[str, ...]
    soname: str | None
    dynamic: bool = False


@dataclass(frozen=True, slots=True)
class Release:
    files: dict[str, ArtifactFile]
    description: dict[str, Any]


def installation_path(name: str) -> str:
    """Configs/units remain inactive pending independent operator qualification."""
    return name + ".pending" if name.startswith("etc/") else name


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ArtifactError("invalid SHA256 digest")
    return value


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def fields(value: object, expected: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise ArtifactError("invalid artifact object fields")
    return dict(value)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ArtifactError("duplicate JSON field")
        result[name] = value
    return result


def _nonfinite(_value: str) -> None:
    raise ArtifactError("nonfinite artifact JSON is unsupported")


def decode(raw: bytes) -> dict[str, Any]:
    if len(raw) > 8 * 1024 * 1024:
        raise ArtifactError("JSON exceeds size bound")
    try:
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ArtifactError("invalid artifact JSON") from exc
    if type(value) is not dict:
        raise ArtifactError("artifact JSON must be an object")
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while pending:
        child, depth = pending.pop()
        nodes += 1
        if depth > 16 or nodes > 500000:
            raise ArtifactError("artifact JSON exceeds structural bounds")
        if type(child) is dict:
            pending.extend((entry, depth + 1) for entry in child.values())
        elif type(child) is list:
            pending.extend((entry, depth + 1) for entry in child)
    return dict(value)


def logical_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value or "\x00" in value:
        raise ArtifactError("invalid artifact path")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {".", ".."} for part in path.parts):
        raise ArtifactError("invalid artifact path")
    return value


def _startup_path(name: str) -> None:
    path = PurePosixPath(name)
    if path.suffix in {".pth", ".egg-link", ".pyc"} or path.name in {
        "sitecustomize.py",
        "usercustomize.py",
        "direct_url.json",
        ".env",
        "provider-environment.json",
        "auth.json",
    }:
        raise ArtifactError("editable or executable startup file is unsupported")
    if any(part.startswith("__editable__") or part in {"__pycache__", ".git", ".venv"} for part in path.parts):
        raise ArtifactError("mutable artifact path is unsupported")


def read_archive(raw: bytes, *, maximum: int = MAX_IMAGE) -> dict[str, ArtifactFile]:
    """Read only ordinary, bounded, canonical uncompressed tar members."""
    if len(raw) > maximum:
        raise ArtifactError("archive exceeds size bound")
    result: dict[str, ArtifactFile] = {}
    seen: set[str] = set()
    directories: set[str] = set()
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive:
                name = logical_path(member.name)
                if name in seen or len(seen) >= MAX_FILES:
                    raise ArtifactError("duplicate member or archive file limit")
                seen.add(name)
                _startup_path(name)
                if member.pax_headers or member.mode & ~0o777 or member.mode & 0o022:
                    raise ArtifactError("unsupported archive metadata or writable mode")
                if member.isdir():
                    directories.add(name)
                    continue
                if not member.isfile() or member.size < 0 or member.size > MAX_FILE or member.mode not in {0o444, 0o555, 0o644, 0o755}:
                    raise ArtifactError("archive member must be a bounded ordinary file")
                total += member.size
                if total > maximum:
                    raise ArtifactError("archive data exceeds size bound")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ArtifactError("archive member is unavailable")
                with stream:
                    content = stream.read(MAX_FILE + 1)
                if len(content) != member.size:
                    raise ArtifactError("archive member size mismatch")
                result[name] = ArtifactFile(content, member.mode)
    except (tarfile.TarError, OSError) as exc:
        raise ArtifactError("invalid ordinary tar archive") from exc
    for name in seen:
        if any(str(parent) in result for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise ArtifactError("archive file/ancestor collision")
    if directories.intersection(result):
        raise ArtifactError("archive file/directory collision")
    return result


def write_archive(files: dict[str, ArtifactFile]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, file in sorted(files.items()):
            member = tarfile.TarInfo(logical_path(name))
            member.size, member.mode, member.uid, member.gid, member.mtime = len(file.data), file.mode, 0, 0, 0
            archive.addfile(member, io.BytesIO(file.data))
    return output.getvalue()


def read_elf(raw: bytes) -> ElfInfo:
    """Parse declared ELF64 x86-64 dependencies without loading the executable."""
    if len(raw) < 64 or raw[:7] != b"\x7fELF\x02\x01\x01":
        raise ArtifactError("unsupported ELF64 encoding")
    kind, machine, version = struct.unpack_from("<HHI", raw, 16)
    phoff = struct.unpack_from("<Q", raw, 32)[0]
    phsize, phnum = struct.unpack_from("<HH", raw, 54)
    if kind not in {2, 3} or machine != 62 or version != 1 or phsize != 56 or not 1 <= phnum <= 1024:
        raise ArtifactError("unsupported ELF64 platform/header")
    if phoff + phnum * phsize > len(raw):
        raise ArtifactError("truncated ELF program headers")
    loads: list[tuple[int, int, int]] = []
    dynamic: bytes | None = None
    interpreter: str | None = None
    for index in range(phnum):
        ptype, _flags, offset, address, _physical, size, _memory, _align = struct.unpack_from("<IIQQQQQQ", raw, phoff + index * phsize)
        if offset + size > len(raw):
            raise ArtifactError("ELF segment escapes file")
        if ptype == 1:
            loads.append((address, offset, size))
        elif ptype == 2:
            if dynamic is not None or size % 16:
                raise ArtifactError("invalid ELF dynamic segment")
            dynamic = raw[offset : offset + size]
        elif ptype == 3:
            if interpreter is not None or size > 4096 or size < 2 or raw[offset + size - 1] != 0:
                raise ArtifactError("invalid ELF interpreter")
            try:
                interpreter = raw[offset : offset + size - 1].decode("ascii")
            except UnicodeError as exc:
                raise ArtifactError("invalid ELF interpreter") from exc
            if interpreter not in {
                "/lib64/ld-linux-x86-64.so.2",
                "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
                "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
            }:
                raise ArtifactError("unsupported ELF interpreter path")
    if dynamic is None:
        return ElfInfo(interpreter, (), None)
    table: dict[int, list[int]] = {}
    terminated = False
    for position in range(0, len(dynamic), 16):
        tag, value = struct.unpack_from("<qQ", dynamic, position)
        if tag == 0:
            terminated = True
            break
        if tag in {15, 29, 0x6FFFFEFB, 0x6FFFFEFC, 0x7FFFFFFD, 0x7FFFFFFF}:
            raise ArtifactError("ELF RPATH/RUNPATH/audit/filter behavior is unsupported")
        table.setdefault(tag, []).append(value)
    if not terminated:
        raise ArtifactError("unterminated ELF dynamic table")
    if 1 not in table and 14 not in table:
        return ElfInfo(interpreter, (), None, True)
    if len(table.get(5, [])) != 1 or len(table.get(10, [])) != 1:
        raise ArtifactError("ELF string table is unavailable")
    address, size = table[5][0], table[10][0]
    matches = [(offset + address - base, size) for base, offset, count in loads if base <= address and address + size <= base + count]
    if len(matches) != 1 or size > MAX_FILE:
        raise ArtifactError("ELF string table mapping is ambiguous")
    offset, size = matches[0]
    strings = raw[offset : offset + size]

    def string(position: int) -> str:
        end = strings.find(b"\x00", position)
        if position >= len(strings) or end < 0 or end - position > 4096:
            raise ArtifactError("invalid ELF dynamic string")
        try:
            value = strings[position:end].decode("ascii")
        except UnicodeError as exc:
            raise ArtifactError("invalid ELF dynamic string") from exc
        if not value or "/" in value or value in {".", ".."}:
            raise ArtifactError("unsafe ELF dependency name")
        return value

    needed = tuple(string(value) for value in table.get(1, []))
    if len(set(needed)) != len(needed) or len(table.get(14, [])) > 1:
        raise ArtifactError("duplicate ELF dependency identity")
    return ElfInfo(interpreter, needed, string(table[14][0]) if 14 in table else None, True)


def validate_elf_closure(files: dict[str, ArtifactFile], *, signer: bool = False) -> None:
    """Check every declared ELF edge; dynamic-load completeness remains unproved."""
    libraries = ("platform/lib", *LIBRARY_ROOTS) if signer else LIBRARY_ROOTS
    elves = {name: read_elf(file.data) for name, file in files.items() if file.data.startswith(b"\x7fELF")}
    for info in elves.values():
        if info.interpreter is not None and info.interpreter.lstrip("/") not in files:
            raise ArtifactError("ELF interpreter is missing from supplied closure")
        for needed in info.needed:
            candidates = [f"{root}/{needed}" for root in libraries if f"{root}/{needed}" in elves]
            if not candidates:
                raise ArtifactError("ELF dependency is missing from supplied closure")
            identities = {sha256(files[path].data) for path in candidates}
            if len(identities) != 1:
                raise ArtifactError("ELF dependency has ambiguous providers")
            if any(elves[path].soname not in {None, needed} for path in candidates):
                raise ArtifactError("ELF dependency SONAME mismatch")
    for file in files.values():
        if file.data.startswith(b"#!"):
            line = file.data.split(b"\n", 1)[0]
            try:
                interpreter = line[2:].decode("ascii").split()
            except UnicodeError as exc:
                raise ArtifactError("invalid executable shebang") from exc
            if (
                not interpreter
                or not interpreter[0].startswith("/")
                or interpreter[0] == "/usr/bin/env"
                or interpreter[0].lstrip("/") not in files
            ):
                raise ArtifactError("unbound or environment-selected shebang interpreter")


def validate_image(files: dict[str, ArtifactFile]) -> str:
    from elspeth_lints.core.operator_runtime import OperatorRuntimeUnavailableError, parse_runtime_manifest

    manifest = files.get("runtime-manifest.json")
    if manifest is None:
        raise ArtifactError("complete runtime manifest is required")
    try:
        parsed = parse_runtime_manifest(manifest.data)
    except OperatorRuntimeUnavailableError as exc:
        raise ArtifactError("invalid supplied runtime manifest") from exc
    inventory = parsed["files"]
    if not isinstance(inventory, dict) or set(inventory) != set(files) - {"runtime-manifest.json"}:
        raise ArtifactError("supplied runtime inventory mismatch")
    if any(sha256(files[name].data) != value for name, value in inventory.items()):
        raise ArtifactError("supplied runtime digest mismatch")
    return sha256(manifest.data)


def _open_parent(path: Path) -> tuple[int, str]:
    """Anchor a path's parent without following any ancestor symlink."""
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise ArtifactError("input path must be canonical")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory, absolute.name
    except BaseException:
        os.close(directory)
        raise


def _read_regular(path: Path, *, maximum: int) -> bytes:
    directory, name = _open_parent(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
                raise ArtifactError("input is not a bounded ordinary file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(maximum + 1)
            after = os.fstat(fd)
            before_binding = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                metadata.st_mode,
                metadata.st_uid,
                metadata.st_gid,
            )
            after_binding = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_mode,
                after.st_uid,
                after.st_gid,
            )
            # Reading can legitimately update atime; it is not input drift.
            if len(raw) > maximum or after_binding != before_binding:
                raise ArtifactError("input changed or exceeds size bound")
            return raw
        finally:
            os.close(fd)
    except OSError as exc:
        raise ArtifactError("ordinary input path unavailable") from exc
    finally:
        os.close(directory)


def _bound_input(root: Path, description: object) -> bytes:
    data = fields(description, {"path", "sha256", "size"})
    name = logical_path(data["path"])
    if type(data["size"]) is not int or not 0 <= data["size"] <= MAX_IMAGE:
        raise ArtifactError("invalid input size")
    raw = _read_regular(root / name, maximum=MAX_IMAGE)
    if len(raw) != data["size"] or sha256(raw) != digest(data["sha256"]):
        raise ArtifactError("input digest or size mismatch")
    return raw


def _uid(value: object, *, nonroot: bool = False) -> int:
    if type(value) is not int or not (1 if nonroot else 0) <= value <= 2**31 - 1:
        raise ArtifactError("invalid explicit principal")
    return value


def _principals(value: object) -> dict[str, Any]:
    data = fields(
        value, {"operator_uid", "operator_gid", "candidate_uid", "allowed_client_uids", "workers", "baseline_uid", "baseline_gid"}
    )
    operator = _uid(data["operator_uid"])
    candidate = _uid(data["candidate_uid"], nonroot=True)
    _uid(data["operator_gid"])
    _uid(data["baseline_gid"], nonroot=True)
    baseline = _uid(data["baseline_uid"], nonroot=True)
    callers = data["allowed_client_uids"]
    if type(callers) is not list or not 1 <= len(callers) <= 16:
        raise ArtifactError("explicit caller principals are required")
    for uid in callers:
        _uid(uid)
    if len(set(callers)) != len(callers) or operator not in callers:
        raise ArtifactError("caller principals must be unique and include operator")
    workers = fields(data["workers"], set(TRANSPORTS))
    worker_ids: list[int] = []
    for transport in TRANSPORTS:
        worker = fields(workers[transport], {"uid", "gid", "authentication"})
        worker_ids.append(_uid(worker["uid"], nonroot=True))
        _uid(worker["gid"], nonroot=True)
        if type(worker["authentication"]) is not str or worker["authentication"] not in {
            "subscription",
            "api",
            "bedrock",
            "vertex",
            "azure",
        }:
            raise ArtifactError("unsupported explicit provider authentication mode")
    isolated = [*worker_ids, baseline]
    if operator == candidate or len(set(isolated)) != len(isolated) or set(isolated).intersection({*callers, candidate}):
        raise ArtifactError("principal custody overlaps")
    return data


def _package_bytes(files: dict[str, ArtifactFile], package: str) -> dict[str, bytes]:
    matches: dict[str, bytes] = {}
    for name, file in files.items():
        parts = PurePosixPath(name).parts
        if package in parts:
            index = parts.index(package)
            relative = "/".join(parts[index + 1 :])
            if relative in matches:
                raise ArtifactError("multiple first-party package roots")
            matches[relative] = file.data
    if "__init__.py" not in matches:
        raise ArtifactError("complete first-party package is missing")
    return matches


def _require_image_paths(role: str, files: dict[str, ArtifactFile], minor: int) -> None:
    interpreter = f"usr/bin/python3.{minor}" if role.startswith("baseline_") else "usr/bin/python3"
    required: tuple[str, ...]
    if role == "signer":
        required = ("bootstrap.py", "lib/elspeth_lints/core/operator_runtime.py", "lib/elspeth/__init__.py")
        if not any(name.startswith("platform/lib/") for name in files):
            raise ArtifactError("supplied signer platform library bytes are required")
    else:
        required = (interpreter, f"usr/lib/python3.{minor}/encodings/__init__.py")
    if any(path not in files for path in required):
        raise ArtifactError("image fixed startup path is missing")
    if role != "signer" and not files[interpreter].data.startswith(b"\x7fELF"):
        raise ArtifactError("image interpreter must be actual ELF bytes")
    executables = [] if role == "signer" else [interpreter]
    package_root = "lib" if role == "signer" else "usr/lib/python3/dist-packages"
    # This Ubuntu profile uses its distro-managed dist-packages slot. Another
    # Python layout requires a separately reviewed profile, never discovery.
    module = {
        "signer": "cli",
        "judge_supervisor": "judge_supervisor",
        "baseline_supervisor": "baseline_supervisor",
        "baseline_worker": "baseline_worker",
    }.get(role, "judge_worker")
    entries: tuple[str, ...] = (
        "__init__.py",
        "core/__init__.py",
        "core/operator_runtime.py",
        "core/judge.py",
        "core/judge_rpc.py",
        f"core/{module}.py",
    )
    if role.startswith("baseline_"):
        entries = (*entries, "core/baseline_rpc.py")
    if any(f"{package_root}/elspeth_lints/{entry}" not in files for entry in entries):
        raise ArtifactError("fixed image entry module or startup import root is missing")
    for name in files:
        if "elspeth_lints" in PurePosixPath(name).parts and not name.startswith(package_root + "/elspeth_lints/"):
            raise ArtifactError("first-party package is outside the fixed import root")
    _package_bytes(files, "elspeth_lints")
    if role in {"judge_supervisor", "baseline_supervisor"} and any(
        name not in files for name in ("usr/bin/systemd-run", "usr/bin/systemctl")
    ):
        raise ArtifactError("supervisor system-manager client closure is missing")
    if role in {"judge_supervisor", "baseline_supervisor"}:
        executables.extend(("usr/bin/systemd-run", "usr/bin/systemctl"))
    if any(files[name].mode & 0o111 != 0o111 for name in executables):
        raise ArtifactError("fixed image executable lacks traversal/execution permission")


def _validate_image_principals(role: str, files: dict[str, ArtifactFile], principals: dict[str, Any]) -> None:
    from elspeth_lints.core.operator_runtime import parse_runtime_manifest

    manifest = parse_runtime_manifest(files["runtime-manifest.json"].data)
    # The existing schema binds signer/candidate identity globally. Image
    # filesystem ownership is independently root-only; worker process UIDs
    # remain explicit in external admission, never asserted by image code.
    if manifest["operator_uid"] != principals["operator_uid"] or manifest["candidate_uid"] != principals["candidate_uid"]:
        raise ArtifactError("runtime manifest principal binding differs from release")


def _configurations(
    images: dict[str, dict[str, ArtifactFile]], units: dict[str, bytes], principals: dict[str, Any]
) -> dict[str, ArtifactFile]:
    ids = {role: validate_image(files) for role, files in images.items()}
    judge_unit = units["judge"]
    baseline_unit = units["baseline"]
    judge = {
        "version": 1,
        "profile": "ubuntu-systemd-v1",
        "socket": "/run/elspeth-judge/supervisor.sock",
        "runtime_root": "/opt/elspeth/judge-supervisor",
        "artifact_id": ids["judge_supervisor"],
        "executable": "/opt/elspeth/judge-supervisor/usr/bin/python3",
        "executable_sha256": sha256(images["judge_supervisor"]["usr/bin/python3"].data),
        "allowed_client_uids": principals["allowed_client_uids"],
        "worker_artifacts": {name: ids[name] for name in TRANSPORTS},
        "unit_sha256": sha256(judge_unit),
    }
    workers = {
        name: {
            **principals["workers"][name],
            "root_directory": "/" + ROLE_ROOTS[name],
            "artifact_id": ids[name],
            "provider_home": "/var/lib/elspeth-judge/providers/" + name,
        }
        for name in TRANSPORTS
    }
    baseline = {
        "version": 1,
        "profile": "ubuntu-systemd-python313-v1",
        "artifact_id": ids["baseline_supervisor"],
        "interpreter_sha256": sha256(images["baseline_supervisor"]["usr/bin/python3.13"].data),
        "unit_sha256": sha256(baseline_unit),
        "allowed_client_uids": principals["allowed_client_uids"],
        "worker_uid": principals["baseline_uid"],
        "worker_gid": principals["baseline_gid"],
        "worker_artifact_id": ids["baseline_worker"],
    }
    # Exercise the actual admission schemas; parser acceptance grants no host authority.
    from elspeth_lints.core.baseline_rpc import parse_admission
    from elspeth_lints.core.judge_rpc import parse_admission_data

    parse_admission_data(judge)
    parse_admission(baseline)
    configs = {
        "etc/elspeth/judge-workers.json": canonical(judge),
        "etc/elspeth/judge-supervisor.json": canonical({"version": 1, "profile": "ubuntu-systemd-v1", "workers": workers}),
        "etc/elspeth/baseline-workers.json": canonical(baseline),
        "etc/systemd/system/elspeth-judge-supervisor.service": judge_unit,
        "etc/systemd/system/elspeth-baseline-supervisor.service": baseline_unit,
    }
    return {name: ArtifactFile(raw, 0o644) for name, raw in configs.items()}


def _validate_unit(raw: bytes, role: str) -> None:
    """Only the reviewed fixed source unit is accepted; no arbitrary unit hooks."""
    # Exact source-template identity accounts for whitespace, duplicate/reset
    # settings, continuations, section changes and mount overrides together.
    # A template change requires deliberate profile review/rebinding here.
    if role not in _UNIT_IDENTITIES or sha256(raw) != _UNIT_IDENTITIES[role]:
        raise ArtifactError("service unit differs from the exact reviewed template")


def _child_directory(parent: int, name: str, *, create: bool) -> tuple[int, bool]:
    made = False
    if create:
        try:
            os.mkdir(name, mode=0o755, dir_fd=parent)
            made = True
        except FileExistsError:
            pass
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if made:
            # mkdir's requested mode is masked by umask; restore the declared
            # mode only on a directory this invocation just created.
            os.fchmod(child, 0o755)
        if os.fstat(child).st_mode & 0o022:
            raise ArtifactError("intermediate directory is writable by group/other")
        return child, made
    except BaseException:
        os.close(child)
        raise


def build_release(*, catalog: Path, expected_sha256: str, input_root: Path, output: Path) -> str:
    raw = _read_regular(catalog, maximum=8 * 1024 * 1024)
    if sha256(raw) != digest(expected_sha256):
        raise ArtifactError("external catalog digest mismatch")
    data = fields(decode(raw), {"version", "profile", "python_minor", "principals", "inputs", "host_platform", "units"})
    if (
        type(data["version"]) is not int
        or data["version"] != 1
        or data["profile"] != PROFILE
        or type(data["python_minor"]) is not int
        or data["python_minor"] not in {12, 13}
    ):
        raise ArtifactError("unsupported explicit release profile")
    principals = _principals(data["principals"])
    inputs = fields(data["inputs"], {*ROLE_ROOTS, "native"})
    images: dict[str, dict[str, ArtifactFile]] = {}
    for role in ROLE_ROOTS:
        images[role] = read_archive(_bound_input(input_root, inputs[role]))
        validate_image(images[role])
        _validate_image_principals(role, images[role], principals)
        _require_image_paths(role, images[role], 13 if role.startswith("baseline_") else data["python_minor"])
        validate_elf_closure(images[role], signer=role == "signer")
    for package in ("elspeth", "elspeth_lints"):
        if _package_bytes(images["signer"], package) != _package_bytes(images["baseline_worker"], package):
            raise ArtifactError("signer and baseline worker first-party package bytes differ")
    native = _bound_input(input_root, inputs["native"])
    info = read_elf(native)
    if info.interpreter is not None or info.needed or info.dynamic:
        raise ArtifactError("native launcher must be statically linked")
    platform = read_archive(_bound_input(input_root, data["host_platform"]))
    if any(not (name.startswith("usr/") or name.startswith("lib/") or name.startswith("lib64/")) for name in platform):
        raise ArtifactError("host platform has unsupported paths")
    for required in (
        f"usr/bin/python3.{data['python_minor']}",
        f"usr/lib/python3.{data['python_minor']}/encodings/__init__.py",
        "usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
        "usr/bin/git",
    ):
        if required not in platform:
            raise ArtifactError("supplied host platform prerequisite is incomplete")
    validate_elf_closure(platform)
    units_data = fields(data["units"], {"judge", "baseline"})
    units = {role: _bound_input(input_root, units_data[role]) for role in units_data}
    for role, unit in units.items():
        _validate_unit(unit, role)
    files = {f"{ROLE_ROOTS[role]}/{name}": file for role, image in images.items() for name, file in image.items()}
    files[NATIVE_PATH] = ArtifactFile(native, 0o755)
    files.update(_configurations(images, units, principals))
    if len(files) >= MAX_FILES:
        raise ArtifactError("aggregate release file count exceeds bound")
    host = {name: {"sha256": sha256(file.data), "mode": file.mode} for name, file in platform.items()}
    description = {
        "version": 1,
        "profile": PROFILE,
        "python_minor": data["python_minor"],
        "principals": principals,
        "host_platform": host,
        "files": {name: {"sha256": sha256(file.data), "mode": file.mode} for name, file in files.items()},
        "installation": {name: installation_path(name) for name in files},
    }
    files["release.json"] = ArtifactFile(canonical(description), 0o644)
    packed = write_archive(files)
    if len(packed) > MAX_RELEASE:
        raise ArtifactError("release exceeds size bound")
    # Creating a new ordinary output cannot overwrite any caller-owned path.
    parent, name = _open_parent(output)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(packed)
    finally:
        os.close(parent)
    return sha256(packed)


def verify_release(path: Path, *, expected_sha256: str) -> Release:
    raw = _read_regular(path, maximum=MAX_RELEASE)
    if sha256(raw) != digest(expected_sha256):
        raise ArtifactError("external release digest mismatch")
    files = read_archive(raw, maximum=MAX_RELEASE)
    descriptor = files.pop("release.json", None)
    if descriptor is None:
        raise ArtifactError("release descriptor missing")
    data = fields(decode(descriptor.data), {"version", "profile", "python_minor", "principals", "host_platform", "files", "installation"})
    if (
        data["version"] != 1
        or type(data["version"]) is not int
        or data["profile"] != PROFILE
        or type(data["python_minor"]) is not int
        or data["python_minor"] not in {12, 13}
    ):
        raise ArtifactError("unsupported release description")
    _principals(data["principals"])
    inventory = data["files"]
    if type(inventory) is not dict or set(inventory) != set(files):
        raise ArtifactError("release inventory mismatch")
    if data["installation"] != {name: installation_path(name) for name in files}:
        raise ArtifactError("release pending installation projection mismatch")
    for name, file in files.items():
        binding = fields(inventory[name], {"sha256", "mode"})
        if sha256(file.data) != digest(binding["sha256"]) or type(binding["mode"]) is not int or file.mode != binding["mode"]:
            raise ArtifactError("release file digest or mode mismatch")
        if name not in {
            NATIVE_PATH,
            "etc/elspeth/judge-workers.json",
            "etc/elspeth/judge-supervisor.json",
            "etc/elspeth/baseline-workers.json",
            "etc/systemd/system/elspeth-judge-supervisor.service",
            "etc/systemd/system/elspeth-baseline-supervisor.service",
        } and not any(name.startswith(root + "/") for root in ROLE_ROOTS.values()):
            raise ArtifactError("release destination outside fixed profile")
    required = {
        NATIVE_PATH,
        "etc/elspeth/judge-workers.json",
        "etc/elspeth/judge-supervisor.json",
        "etc/elspeth/baseline-workers.json",
        "etc/systemd/system/elspeth-judge-supervisor.service",
        "etc/systemd/system/elspeth-baseline-supervisor.service",
    }
    if not required.issubset(files):
        raise ArtifactError("release fixed artifact is missing")
    images = {
        role: {name[len(root) + 1 :]: file for name, file in files.items() if name.startswith(root + "/")}
        for role, root in ROLE_ROOTS.items()
    }
    units = {name: files[f"etc/systemd/system/elspeth-{name}-supervisor.service"].data for name in ("judge", "baseline")}
    for role, image in images.items():
        _validate_image_principals(role, image, data["principals"])
        _require_image_paths(role, image, 13 if role.startswith("baseline_") else data["python_minor"])
        validate_elf_closure(image, signer=role == "signer")
    for role, unit in units.items():
        _validate_unit(unit, role)
    native_info = read_elf(files[NATIVE_PATH].data)
    if native_info.interpreter is not None or native_info.needed or native_info.dynamic:
        raise ArtifactError("native launcher must be statically linked")
    for package in ("elspeth", "elspeth_lints"):
        if _package_bytes(images["signer"], package) != _package_bytes(images["baseline_worker"], package):
            raise ArtifactError("signer and baseline worker first-party package bytes differ")
    expected_configs = _configurations(images, units, data["principals"])
    if any(files.get(name) != file for name, file in expected_configs.items()):
        raise ArtifactError("generated release configuration differs from measured artifacts")
    if type(data["host_platform"]) is not dict or not data["host_platform"]:
        raise ArtifactError("host prerequisite inventory missing")
    for name, value in data["host_platform"].items():
        logical_path(name)
        if not name.startswith(("usr/", "lib/", "lib64/")):
            raise ArtifactError("host prerequisite escapes platform paths")
        entry = fields(value, {"sha256", "mode"})
        digest(entry["sha256"])
        if type(entry["mode"]) is not int or entry["mode"] not in {0o444, 0o555, 0o644, 0o755}:
            raise ArtifactError("invalid host prerequisite mode")
    return Release(files, data)


def stage_release(path: Path, *, expected_sha256: str, root: Path) -> None:
    """Fixture-only extraction: no chown, host access, services or activation."""
    release = verify_release(path, expected_sha256=expected_sha256)
    if root.exists() or root.is_symlink():
        raise ArtifactError("fixture root must be absent")
    parent, leaf = _open_parent(root)
    try:
        os.mkdir(leaf, mode=0o700, dir_fd=parent)
        root_fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        for logical, file in sorted(release.files.items()):
            name = installation_path(logical)
            descriptor = os.dup(root_fd)
            try:
                parts = PurePosixPath(name).parts
                for part in parts[:-1]:
                    child, _made = _child_directory(descriptor, part, create=True)
                    os.close(descriptor)
                    descriptor = child
                fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, file.mode, dir_fd=descriptor)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(file.data)
                    os.fchmod(stream.fileno(), file.mode)
            finally:
                os.close(descriptor)
    finally:
        os.close(root_fd)


def verify_host_prerequisites(release: Release, *, root: Path = Path("/")) -> None:
    """Inspect a supplied/installed host; never copy or repair its /usr tree."""
    from elspeth_lints.core.operator_runtime import validate_protected_path

    observed: dict[str, ArtifactFile] = {}
    for name, binding in release.description["host_platform"].items():
        path = root / name
        validate_protected_path(path)
        raw = _read_regular(path, maximum=MAX_FILE)
        if sha256(raw) != binding["sha256"] or stat.S_IMODE(path.lstat().st_mode) != binding["mode"]:
            raise ArtifactError("host platform prerequisite differs; no repair is authorized")
        observed[name] = ArtifactFile(raw, binding["mode"])
    validate_elf_closure(observed)


def directory_intentions(release: Release) -> dict[str, tuple[int, int, int]]:
    """Explicit numeric directory ownership/modes; no account provisioning."""
    p = release.description["principals"]
    result = {
        "run/elspeth/operator": (p["operator_uid"], p["operator_gid"], 0o700),
        "run/elspeth-judge": (0, 0, 0o755),
        "run/elspeth-judge/evidence": (0, 0, 0o700),
        "run/elspeth-baseline": (0, 0, 0o755),
        "run/elspeth-baseline/snapshots": (0, 0, 0o700),
    }
    for transport in TRANSPORTS:
        worker = p["workers"][transport]
        result["var/lib/elspeth-judge/providers/" + transport] = (worker["uid"], worker["gid"], 0o700)
    return result


def install_release(path: Path, *, expected_sha256: str) -> None:
    """Future key-free root installation; never called by fixture staging/tests.

    The installer itself must first be independently authenticated. This method
    installs absent fixed destinations only, leaves services inactive, and does
    not open signer/provider credentials or execute installed package code.
    """
    import grp
    import pwd

    from elspeth_lints.core.operator_runtime import validate_protected_path

    if os.getuid() != 0 or os.geteuid() != 0:
        raise ArtifactError("installation requires an independently authenticated root operator")
    release = verify_release(path, expected_sha256=expected_sha256)
    verify_host_prerequisites(release)
    principals = release.description["principals"]
    uid_values = {principals["operator_uid"], principals["candidate_uid"], principals["baseline_uid"], *principals["allowed_client_uids"]}
    gid_values = {principals["operator_gid"], principals["baseline_gid"]}
    for worker in principals["workers"].values():
        uid_values.add(worker["uid"])
        gid_values.add(worker["gid"])
    try:
        for uid in uid_values:
            pwd.getpwuid(uid)
        for gid in gid_values:
            grp.getgrgid(gid)
    except KeyError as exc:
        raise ArtifactError("explicit installation principals must already exist") from exc
    roots = [
        *ROLE_ROOTS.values(),
        NATIVE_PATH,
        *[name for name in release.files if name.startswith("etc/")],
        *[installation_path(name) for name in release.files if name.startswith("etc/")],
    ]
    intentions = directory_intentions(release)
    for name in (*roots, *intentions):
        target = Path("/") / name
        if target.exists() or target.is_symlink():
            raise ArtifactError("fixed installation destination already exists; upgrades are unsupported")
        for ancestor in reversed(target.parents):
            if ancestor.exists() or ancestor.is_symlink():
                validate_protected_path(ancestor, directory=True)
    created_files: list[tuple[str, int, int]] = []
    created_directories: list[tuple[str, int, int]] = []
    root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def directory(parts: tuple[str, ...], *, create: bool = True) -> int:
        descriptor = os.dup(root_fd)
        try:
            for index, part in enumerate(parts):
                child, made = _child_directory(descriptor, part, create=create)
                metadata = os.fstat(child)
                if made:
                    created_directories.append(("/".join(parts[: index + 1]), metadata.st_dev, metadata.st_ino))
                if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                    os.close(child)
                    raise ArtifactError("installation ancestor is not protected")
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    try:
        # Runtime image bytes precede pending config/unit files. Active admission
        # paths are never published by checksum-only installation.
        for logical, file in sorted(release.files.items(), key=lambda item: (item[0].startswith("etc/"), item[0])):
            name = installation_path(logical)
            parts = PurePosixPath(name).parts
            parent = directory(parts[:-1])
            try:
                fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                metadata = os.fstat(fd)
                created_files.append((name, metadata.st_dev, metadata.st_ino))
                with os.fdopen(fd, "wb") as stream:
                    stream.write(file.data)
                    stream.flush()
                    os.fchown(stream.fileno(), 0, 0)
                    os.fchmod(stream.fileno(), file.mode)
                    os.fsync(stream.fileno())
                actual_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                with os.fdopen(actual_fd, "rb") as reader:
                    if hashlib.file_digest(reader, "sha256").hexdigest() != sha256(file.data):
                        raise ArtifactError("installed bytes differ from authenticated release")
            finally:
                os.close(parent)
        # Create leaf directories only after every immutable byte has landed.
        for name, (uid, gid, mode) in intentions.items():
            parts = PurePosixPath(name).parts
            parent = directory(parts[:-1])
            try:
                os.mkdir(parts[-1], mode=0o700, dir_fd=parent)
                metadata = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                created_directories.append((name, metadata.st_dev, metadata.st_ino))
                fd = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    os.fchown(fd, uid, gid)
                    os.fchmod(fd, mode)
                finally:
                    os.close(fd)
            finally:
                os.close(parent)
    except BaseException:
        # Reopen only protected parents through the anchored root; remove an
        # entry only when its device/inode still identify our new object.
        for entries, is_directory in ((created_files, False), (created_directories, True)):
            for name, device, inode in reversed(entries):
                parts = PurePosixPath(name).parts
                parent = directory(parts[:-1], create=False)
                try:
                    metadata = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                    if (metadata.st_dev, metadata.st_ino) != (device, inode):
                        raise ArtifactError("installation rollback found a changed object; preserving it")
                    if is_directory:
                        os.rmdir(parts[-1], dir_fd=parent)
                    else:
                        os.unlink(parts[-1], dir_fd=parent)
                finally:
                    os.close(parent)
        raise
    finally:
        os.close(root_fd)
