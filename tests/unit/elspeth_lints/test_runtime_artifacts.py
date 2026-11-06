"""Offline artifact inspection treats supplied executable bytes as data."""

from __future__ import annotations

import io
import struct
import tarfile

import pytest

from elspeth_lints.core.runtime_artifacts import ArtifactError, ArtifactFile, read_archive, read_elf, validate_elf_closure


def archive_bytes(name: str, data: bytes, *, kind: bytes = tarfile.REGTYPE) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(data)
        member.type = kind
        archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def test_archive_positive_and_path_mutation_control() -> None:
    assert read_archive(archive_bytes("usr/bin/tool", b"data"))["usr/bin/tool"].data == b"data"
    for name in ("../escape", "/absolute", "x/../escape", "sitecustomize.py", "lib/injected.pth"):
        with pytest.raises(ArtifactError):
            read_archive(archive_bytes(name, b"data"))
    with pytest.raises(ArtifactError):
        read_archive(archive_bytes("link", b"", kind=tarfile.SYMTYPE))


def test_elf_parser_rejects_nonelf_and_truncation() -> None:
    for raw in (b"not ELF", b"\x7fELF" + b"\x00" * 60):
        with pytest.raises(ArtifactError):
            read_elf(raw)


def elf_bytes(*, needed: str | None = None, soname: str | None = None, unsafe_rpath: bool = False) -> bytes:
    strings = b"\x00"
    entries: list[tuple[int, int]] = []
    for tag, value in ((1, needed), (14, soname)):
        if value is not None:
            entries.append((tag, len(strings)))
            strings += value.encode() + b"\x00"
    dynamic = needed is not None or soname is not None or unsafe_rpath
    count = 2 if dynamic else 1
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", header, 16, 3, 62, 1)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HHH", header, 52, 64, 56, count)
    if dynamic:
        string_offset = 64 + 56 * count
        table_offset = string_offset + len(strings)
        entries.extend(((5, string_offset), (10, len(strings))))
        if unsafe_rpath:
            entries.append((29, 0))
        entries.append((0, 0))
        table = b"".join(struct.pack("<qQ", tag, value) for tag, value in entries)
        size = table_offset + len(table)
        return (
            bytes(header)
            + struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, size, size, 1)
            + struct.pack("<IIQQQQQQ", 2, 4, table_offset, table_offset, 0, len(table), len(table), 1)
            + strings
            + table
        )
    return bytes(header) + struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, 120, 120, 1)


def test_elf_declared_closure_controls() -> None:
    executable = ArtifactFile(elf_bytes(needed="libfixture.so"), 0o755)
    library = ArtifactFile(elf_bytes(soname="libfixture.so"), 0o644)
    files = {"usr/bin/python3": executable, "usr/lib/libfixture.so": library}
    validate_elf_closure(files)
    with pytest.raises(ArtifactError, match="missing"):
        validate_elf_closure({"usr/bin/python3": executable})
    with pytest.raises(ArtifactError, match="ambiguous"):
        validate_elf_closure({**files, "lib/libfixture.so": ArtifactFile(elf_bytes(soname="different.so"), 0o644)})
    with pytest.raises(ArtifactError, match="RPATH"):
        read_elf(elf_bytes(unsafe_rpath=True))
    assert read_elf(elf_bytes()).dynamic is False
