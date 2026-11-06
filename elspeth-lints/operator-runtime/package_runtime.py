"""Assemble an offline, noneditable operator artifact from reviewed wheels.

This is key-free build tooling. It never installs, changes ownership, resolves
dependencies, imports wheel code, or qualifies the resulting artifact for use.
The separate operator must authenticate the complete wheel/platform closure,
install it under protected ownership, and qualify the host before activation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

_MAX_MEMBER_BYTES = 33_554_432
_MAX_ARTIFACT_BYTES = 268_435_456


def _member_destination(name: str) -> Path:
    logical = PurePosixPath(name)
    if (
        logical.is_absolute()
        or str(logical) != name
        or any(part in {".", ".."} for part in logical.parts)
        or not logical.parts
        or "\\" in name
        or "\x00" in name
    ):
        raise ValueError("refused artifact member path")
    if (
        logical.suffix in {".pth", ".egg-link", ".pyc"}
        or logical.name in {"sitecustomize.py", "usercustomize.py", "direct_url.json"}
        or any(part.startswith("__editable__") or part == "__pycache__" for part in logical.parts)
    ):
        raise ValueError("refused artifact member with editable or startup behavior")
    if logical.parts[0].endswith(".data"):
        if len(logical.parts) < 3 or logical.parts[1] not in {"purelib", "platlib"}:
            raise ValueError("refused artifact member with unsupported wheel data layout")
        logical = PurePosixPath(*logical.parts[2:])
    return Path("lib", *logical.parts)


def assemble_artifact(*, wheels: list[Path], output: Path, operator_uid: int, candidate_uid: int, platform_libraries: list[Path]) -> None:
    """Assemble bytes and a manifest; output ownership remains unqualified."""
    if operator_uid < 0 or candidate_uid < 0 or operator_uid == candidate_uid:
        raise ValueError("operator and candidate UIDs must be distinct nonnegative integers")
    if output.exists() or output.is_symlink():
        raise ValueError("artifact output already exists")
    if not wheels:
        raise ValueError("explicit reviewed input wheels are required")
    members: list[tuple[Path, str, Path]] = []
    observed: set[Path] = set()
    total = 0
    for wheel in wheels:
        if wheel.suffix != ".whl" or wheel.is_symlink():
            raise ValueError("input must be an ordinary wheel file")
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                destination = _member_destination(member.filename)
                mode = member.external_attr >> 16
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                    raise ValueError("refused artifact member with special file type")
                if member.file_size > _MAX_MEMBER_BYTES or member.file_size < 0:
                    raise ValueError("refused artifact member exceeding size limit")
                total += member.file_size
                if total > _MAX_ARTIFACT_BYTES or len(members) >= 65_534:
                    raise ValueError("artifact exceeds assembly limits")
                if destination in observed:
                    raise ValueError("duplicate artifact member destination")
                observed.add(destination)
                members.append((wheel, member.filename, destination))
    required = Path("lib/elspeth_lints/core/operator_runtime.py")
    if required not in observed:
        raise ValueError("input wheels do not include the operator runtime")
    output.mkdir(parents=False)
    try:
        library_directory = output / "platform/lib"
        library_directory.mkdir(parents=True)
        for library in platform_libraries:
            # Copy resolved bytes, never symlink the build machine into runtime.
            source = library.resolve(strict=True)
            if not source.is_file() or not library.name or (library_directory / library.name).exists():
                raise ValueError("invalid or duplicate platform library input")
            shutil.copyfile(source, library_directory / library.name)
            (library_directory / library.name).chmod(0o644)
        for wheel, member_name, destination in members:
            target = output / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(wheel) as archive:
                content = archive.read(member_name)
            if len(content) > _MAX_MEMBER_BYTES:
                raise ValueError("refused artifact member exceeding extracted size limit")
            target.write_bytes(content)
            target.chmod(0o644)
        shutil.copyfile(Path(__file__).with_name("bootstrap.py"), output / "bootstrap.py")
        (output / "bootstrap.py").chmod(0o644)
        inventory: dict[str, str] = {}
        for path in sorted(output.rglob("*")):
            if path.is_dir():
                path.chmod(0o755)
            else:
                with path.open("rb") as stream:
                    inventory[path.relative_to(output).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = {"schema": 1, "operator_uid": operator_uid, "candidate_uid": candidate_uid, "files": inventory}
        (output / "runtime-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (output / "runtime-manifest.json").chmod(0o644)
        output.chmod(0o755)
    except BaseException:
        shutil.rmtree(output)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--operator-uid", type=int, required=True)
    parser.add_argument("--candidate-uid", type=int, required=True)
    parser.add_argument("--platform-library", type=Path, action="append", default=[])
    args = parser.parse_args()
    try:
        assemble_artifact(
            wheels=args.wheel,
            output=args.output,
            operator_uid=args.operator_uid,
            candidate_uid=args.candidate_uid,
            platform_libraries=args.platform_library,
        )
    except (OSError, ValueError, zipfile.BadZipFile):
        sys.stderr.write("operator artifact assembly refused; inputs or artifact member are invalid.\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
