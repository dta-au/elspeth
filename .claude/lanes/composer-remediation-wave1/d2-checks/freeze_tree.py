"""Freeze tracked tree and untracked source/test inputs without following links."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


root = Path.cwd().resolve()
tracked_and_untracked = subprocess.check_output(
    ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
).split(b"\0")
ignored_inputs = subprocess.check_output(
    ["git", "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--", "src", "tests", "elspeth-lints/src"]
).split(b"\0")
cache_directories = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
names = set(tracked_and_untracked)
names.update(name for name in ignored_inputs if not cache_directories.intersection(Path(os.fsdecode(name)).parts))
entries = []
for name in sorted(names - {b""}):
    relative = Path(os.fsdecode(name))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"Nonlexical Git input: {relative}")
    path = root / relative
    for parent in relative.parents:
        if (root / parent).is_symlink():
            raise RuntimeError(f"Symlink ancestor of input: {relative}")
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode):
        payload = os.fsencode(os.readlink(path))
        kind = "symlink"
    elif stat.S_ISREG(before.st_mode):
        kind = "file"
        digest = hashlib.sha256()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise RuntimeError(f"Input replaced before open: {relative}")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        payload = None
    else:
        raise RuntimeError(f"Nonregular input: {relative}")
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns
    ):
        raise RuntimeError(f"Input changed during freeze: {relative}")
    entries.append({"path": str(relative), "kind": kind, "mode": stat.S_IMODE(before.st_mode),
                    "sha256": hashlib.sha256(payload).hexdigest() if payload is not None else digest.hexdigest()})
manifest = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(), "entries": entries}
encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
Path(sys.argv[1]).write_bytes(encoded + b"\n")
print(json.dumps({"head": manifest["head"], "inputs": len(entries), "sha256": hashlib.sha256(encoded).hexdigest()}))
