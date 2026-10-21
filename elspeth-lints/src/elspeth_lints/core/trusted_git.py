"""Bounded, callback-free Git observations over a private metadata copy.

Candidate configuration is parsed as data with includes disabled; it is never
installed as Git configuration. Only raw metadata commands run, against copies
of regular files. This is not an OS sandbox: the installed Git executable and
the observer's private temporary directory belong to the trusted runtime.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import get_ident
from types import TracebackType

from elspeth_lints.core import operator_runtime

_GIT = "/usr/bin/git"
_FILE_LIMIT = 64 * 1024 * 1024
_COPY_LIMIT = 1024 * 1024 * 1024
_COPY_FILES = 100_000
_HEX = re.compile(r"[0-9a-f]+")


class SourceSnapshotError(ValueError):
    """Candidate source or Git metadata cannot be observed safely."""


def _open_directory(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise


def require_directory(path: Path) -> None:
    """Require a directory without following any symbolic-link component."""
    try:
        os.close(_open_directory(path))
    except OSError as exc:
        raise SourceSnapshotError(f"cannot inspect directory without symbolic links: {path}") from exc


def _open_file(path: Path) -> int:
    parent = _open_directory(path.parent)
    try:
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def _read_file(path: Path, *, limit: int) -> tuple[bytes, int]:
    descriptor = _open_file(path)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceSnapshotError(f"snapshot input must be a regular file: {path}")
        if metadata.st_size > limit:
            raise SourceSnapshotError(f"snapshot input exceeds byte limit: {path}")
        payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise SourceSnapshotError(f"snapshot input exceeds byte limit: {path}")
        after = os.fstat(stream.fileno())
        if (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise SourceSnapshotError(f"snapshot input changed during read: {path}")
        return payload, metadata.st_mode


def read_regular_file(path: Path, *, limit: int = _FILE_LIMIT) -> bytes:
    """Read bounded regular bytes using no-follow descriptors for every ancestor."""
    try:
        return _read_file(Path(os.path.abspath(path)), limit=limit)[0]
    except OSError as exc:
        raise SourceSnapshotError(f"cannot read snapshot input without symbolic links: {path}") from exc


def _optional_file(path: Path, *, limit: int = _FILE_LIMIT) -> bytes | None:
    try:
        return _read_file(path, limit=limit)[0]
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SourceSnapshotError(f"cannot read Git metadata without symbolic links: {path}") from exc


def _working_entry(path: Path) -> tuple[bytes, str] | None:
    parent = _open_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        metadata = os.fstat(descriptor)
        if stat.S_ISLNK(metadata.st_mode):
            # Linux readlinkat(AT_EMPTY_PATH) reads this pinned link itself.
            # It never opens the link target, including a FIFO or key file.
            return os.readlink(b"", dir_fd=descriptor), "120000"
        if stat.S_ISDIR(metadata.st_mode):
            return None  # A directory replacement removes the tracked file.
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceSnapshotError("working-tree metadata requires regular files or symbolic links")
    finally:
        os.close(descriptor)
    payload, mode = _read_file(path, limit=_FILE_LIMIT)
    return payload, "100755" if mode & stat.S_IXUSR else "100644"


def _relative_path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8")
    except UnicodeError as exc:
        raise SourceSnapshotError("Git paths must be valid UTF-8") from exc
    parts = value.split("/")
    if not value or any(part in {"", ".", "..", ".git"} for part in parts) or "\0" in value:
        raise SourceSnapshotError("Git path contains unsafe traversal or .git metadata")
    return value


def repository_root(path: Path) -> Path:
    """Discover an ordinary or linked worktree without executing candidate Git."""
    absolute = Path(os.path.abspath(path))
    require_directory(absolute)
    for candidate in (absolute, *absolute.parents):
        if os.path.lexists(candidate / ".git"):
            return candidate
    raise SourceSnapshotError(f"cannot resolve Git repository for {path}")


@dataclass(frozen=True, slots=True)
class GitEntry:
    mode: str
    oid: str


class TrustedGitRepository:
    """A finite private view; no candidate hooks/config/alternate paths survive."""

    def __init__(self, path: Path) -> None:
        self.repo_root = repository_root(path)
        try:
            private_parent = (
                operator_runtime.runtime_private_directory() if operator_runtime.current_operator_runtime() is not None else None
            )
        except operator_runtime.OperatorRuntimeUnavailableError as exc:
            raise SourceSnapshotError("protected source observer scratch is unavailable") from exc
        self._temporary = tempfile.TemporaryDirectory(prefix="elspeth-source-git-", dir=private_parent)
        self.private = Path(self._temporary.name)
        self._copied_bytes = 0
        self._copied_files = 0
        self._enumerated = 0
        self.filemode = True
        self.algorithm = "sha1"
        try:
            self._prepare()
        except BaseException:
            self._temporary.cleanup()
            raise

    def __enter__(self) -> TrustedGitRepository:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self._temporary.cleanup()

    def _count_entry(self) -> None:
        self._enumerated += 1
        if self._enumerated > _COPY_FILES:
            raise SourceSnapshotError("Git metadata exceeds enumeration resource limit")

    def _copy(self, source: Path, destination: Path, *, optional: bool = False) -> None:
        self._copied_files += 1
        if self._copied_files > _COPY_FILES:
            raise SourceSnapshotError("Git metadata exceeds private-copy file limit")
        try:
            descriptor = _open_file(source)
        except FileNotFoundError:
            if optional:
                return
            raise SourceSnapshotError(f"missing required Git metadata: {source}") from None
        except OSError as exc:
            raise SourceSnapshotError(f"cannot copy Git metadata without symbolic links: {source}") from exc
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SourceSnapshotError("Git metadata must be a regular file")
            if before.st_size > _COPY_LIMIT - self._copied_bytes:
                raise SourceSnapshotError("Git metadata exceeds private-copy byte limit")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as output:
                while chunk := stream.read(min(1024 * 1024, _COPY_LIMIT - self._copied_bytes + 1)):
                    self._copied_bytes += len(chunk)
                    if self._copied_bytes > _COPY_LIMIT:
                        raise SourceSnapshotError("Git metadata exceeds private-copy byte limit")
                    output.write(chunk)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise SourceSnapshotError("Git metadata changed while copying")

    def _run(self, *arguments: str) -> bytes:
        result = self._run_result(*arguments)
        if result.returncode != 0:
            raise SourceSnapshotError(f"cannot read Git {arguments[0]} metadata (unsupported or corrupt repository/index)")
        return result.stdout

    def _run_result(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        if operator_runtime.current_operator_runtime() is not None:
            try:
                operator_runtime.validate_protected_path(Path(_GIT))
            except operator_runtime.OperatorRuntimeUnavailableError as exc:
                raise SourceSnapshotError("Git executable is not protected by trusted ownership and ancestry") from exc
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.private),
            "XDG_CONFIG_HOME": str(self.private),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
        try:
            with subprocess.Popen(
                [_GIT, "--no-pager", "--no-replace-objects", "--literal-pathspecs", "--git-dir", str(self.private), *arguments],
                cwd=self.private,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            ) as process:
                try:
                    if process.stdout is None or process.stderr is None:
                        raise SourceSnapshotError("Git metadata output pipes are unavailable")
                    chunks: list[bytes] = []
                    diagnostics: list[bytes] = []
                    total = 0
                    deadline = time.monotonic() + 30
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        selector.register(process.stderr, selectors.EVENT_READ)
                        while selector.get_map():
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise SourceSnapshotError("Git metadata observation exceeded time limit")
                            for event, _ in selector.select(remaining):
                                chunk = os.read(event.fd, min(65536, _FILE_LIMIT - total + 1))
                                if not chunk:
                                    selector.unregister(event.fileobj)
                                    continue
                                total += len(chunk)
                                if total > _FILE_LIMIT:
                                    raise SourceSnapshotError("Git metadata output exceeds byte limit")
                                if event.fileobj is process.stdout:
                                    chunks.append(chunk)
                                else:
                                    diagnostics.append(chunk)
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                    return subprocess.CompletedProcess([_GIT, *arguments], process.returncode, b"".join(chunks), b"".join(diagnostics))
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SourceSnapshotError("cannot execute Git while observing source snapshot") from exc

    def _prepare(self) -> None:
        marker = self.repo_root / ".git"
        if marker.is_symlink():
            raise SourceSnapshotError("Git metadata must not be a symbolic link")
        if marker.is_dir():
            git_dir = marker
        else:
            payload = read_regular_file(marker, limit=4096)
            if not payload.startswith(b"gitdir: ") or b"\n" in payload.rstrip(b"\n"):
                raise SourceSnapshotError("unsupported Git worktree gitfile")
            try:
                location = Path(payload[8:].rstrip(b"\n").decode("utf-8"))
            except UnicodeError as exc:
                raise SourceSnapshotError("Git worktree path must be UTF-8") from exc
            git_dir = Path(os.path.abspath(self.repo_root / location))
        require_directory(git_dir)
        common_data = _optional_file(git_dir / "commondir", limit=4096)
        if common_data is None:
            common_dir = git_dir
        else:
            try:
                common_location = Path(common_data.rstrip(b"\n").decode("utf-8"))
            except UnicodeError as exc:
                raise SourceSnapshotError("Git common directory must be UTF-8") from exc
            common_dir = Path(os.path.abspath(git_dir / common_location))
            require_directory(common_dir)
        self._common_dir = common_dir
        (self.private / "objects").mkdir()
        (self.private / "refs").mkdir()
        (self.private / "config").write_text("[core]\nrepositoryformatversion = 0\nbare = true\n", encoding="ascii")
        for number, config_path in enumerate((common_dir / "config", git_dir / "config.worktree")):
            config_payload = _optional_file(config_path, limit=1024 * 1024)
            if config_payload is not None:
                copied_config = self.private / f"observed-config-{number}"
                copied_config.write_bytes(config_payload)
                self._parse_config(copied_config)
        if self.algorithm == "sha256":
            (self.private / "config").write_text(
                "[core]\nrepositoryformatversion = 1\nbare = true\n[extensions]\nobjectformat = sha256\n", encoding="ascii"
            )
        for unsupported in (common_dir / "shallow", common_dir / "info" / "grafts"):
            if os.path.lexists(unsupported):
                raise SourceSnapshotError("unsupported shallow/grafted Git layout")
        objects = common_dir / "objects"
        require_directory(objects)
        for name in ("alternates", "http-alternates"):
            if os.path.lexists(objects / "info" / name):
                raise SourceSnapshotError("Git object alternates are unsupported")
        self._copy_objects(objects)
        self._copy(git_dir / "HEAD", self.private / "HEAD")
        self._copy(git_dir / "index", self.private / "index", optional=True)
        self._copy(common_dir / "packed-refs", self.private / "packed-refs", optional=True)
        self._copy_refs(common_dir / "refs", self.private / "refs")
        if git_dir != common_dir and (git_dir / "refs").exists():
            self._copy_refs(git_dir / "refs", self.private / "refs")

    def _parse_config(self, copied_config: Path) -> None:
        raw = self._run("config", "--no-includes", "--null", "--list", "--file", str(copied_config))
        for record in raw.split(b"\0"):
            if not record:
                continue
            raw_key, _, raw_value = record.partition(b"\n")
            try:
                key = raw_key.decode("utf-8").lower()
                value = raw_value.decode("utf-8").lower()
            except UnicodeError as exc:
                raise SourceSnapshotError("Git configuration must be valid UTF-8") from exc
            if key.startswith(("include.", "includeif.")):
                raise SourceSnapshotError("Git configuration includes are unsupported and are never followed")
            if key.startswith("extensions.") and key not in {"extensions.objectformat", "extensions.worktreeconfig"}:
                raise SourceSnapshotError(f"unsupported Git configuration extension: {key}")
            if key == "extensions.objectformat":
                if value not in {"sha1", "sha256"}:
                    raise SourceSnapshotError("unsupported Git object format")
                self.algorithm = value
            elif key == "core.filemode":
                if value not in {"true", "false", "yes", "no", "on", "off", "1", "0", ""}:
                    raise SourceSnapshotError("unsupported Git core.filemode value")
                self.filemode = value in {"true", "yes", "on", "1", ""}
            elif key == "core.autocrlf" and value not in {"false", "no", "off", "0"}:
                raise SourceSnapshotError("Git core.autocrlf transformations are unsupported")
            elif key == "core.repositoryformatversion" and value not in {"0", "1"}:
                raise SourceSnapshotError("unsupported Git repository format version")
            elif key == "core.worktree" or (key == "core.bare" and value not in {"false", "no", "off", "0"}):
                raise SourceSnapshotError("unsupported bare/retargeted Git worktree configuration")
            elif key in {"core.attributesfile", "core.eol", "core.symlinks", "core.sparsecheckout", "core.splitindex"}:
                allowed = (
                    (key == "core.eol" and value == "lf")
                    or (key in {"core.sparsecheckout", "core.splitindex"} and value in {"false", "no", "off", "0"})
                    or (key == "core.symlinks" and value in {"true", "yes", "on", "1", ""})
                )
                if not allowed:
                    raise SourceSnapshotError(f"unsupported Git transformation/layout setting: {key}")

    def _copy_objects(self, objects: Path) -> None:
        try:
            for directory in objects.iterdir():
                self._count_entry()
                if directory.name == "info":
                    require_directory(directory)
                    continue
                if directory.name != "pack" and (len(directory.name) != 2 or _HEX.fullmatch(directory.name) is None):
                    raise SourceSnapshotError("unsupported Git object directory entry")
                require_directory(directory)
                for item in directory.iterdir():
                    self._count_entry()
                    if directory.name == "pack":
                        if item.suffix == ".promisor":
                            raise SourceSnapshotError("partial/promisor Git objects are unsupported")
                        if item.suffix not in {".pack", ".idx", ".rev"}:
                            continue
                        if not re.fullmatch(r"pack-[0-9a-f]+\.(pack|idx|rev)", item.name):
                            raise SourceSnapshotError("unsupported Git pack filename")
                    elif _HEX.fullmatch(item.name) is None or len(item.name) not in {38, 62}:
                        raise SourceSnapshotError("unsupported loose Git object filename")
                    self._copy(item, self.private / "objects" / directory.name / item.name)
        except OSError as exc:
            raise SourceSnapshotError("cannot enumerate bounded Git objects") from exc

    def _copy_refs(self, source: Path, destination: Path) -> None:
        if len(destination.relative_to(self.private).parts) > 128:
            raise SourceSnapshotError("Git reference paths exceed depth limit")
        require_directory(source)
        try:
            for item in source.iterdir():
                self._count_entry()
                if item.name == "replace":
                    raise SourceSnapshotError("Git replacement refs are unsupported")
                _relative_path(item.name.encode("utf-8"))
                if item.is_dir() and not item.is_symlink():
                    self._copy_refs(item, destination / item.name)
                else:
                    self._copy(item, destination / item.name)
        except OSError as exc:
            raise SourceSnapshotError("cannot enumerate Git refs") from exc

    def head(self) -> str:
        value = self._run("rev-parse", "--verify", "HEAD^{commit}").strip().decode("ascii")
        if len(value) != (40 if self.algorithm == "sha1" else 64) or _HEX.fullmatch(value) is None:
            raise SourceSnapshotError("Git HEAD must be a full lowercase revision")
        return value

    def index(self) -> dict[str, GitEntry]:
        self._validate_index()
        result: dict[str, GitEntry] = {}
        for record in self._run("ls-files", "--stage", "-z").split(b"\0"):
            if not record:
                continue
            header, separator, raw_path = record.partition(b"\t")
            fields = header.split(b" ")
            if not separator or len(fields) != 3 or fields[2] != b"0" or fields[0] not in {b"100644", b"100755", b"120000", b"160000"}:
                raise SourceSnapshotError("unsupported sparse/unmerged Git index")
            path = _relative_path(raw_path)
            if path in result or set(fields[1]) == {48}:
                raise SourceSnapshotError("unsupported duplicate/intent-to-add Git index entry")
            result[path] = GitEntry(fields[0].decode("ascii"), fields[1].decode("ascii"))
        return result

    def _validate_index(self) -> None:
        payload = _optional_file(self.private / "index")
        if payload is None:
            return
        hash_size = 20 if self.algorithm == "sha1" else 32
        end = len(payload) - hash_size
        if len(payload) < 12 + hash_size or payload[:4] != b"DIRC":
            raise SourceSnapshotError("malformed Git index header")
        version = int.from_bytes(payload[4:8], "big")
        if version not in {2, 3}:
            raise SourceSnapshotError("unsupported Git index version (supported: 2 and 3)")
        if hashlib.new(self.algorithm, payload[:end]).digest() != payload[end:]:
            raise SourceSnapshotError("Git index checksum mismatch")
        count = int.from_bytes(payload[8:12], "big")
        if count > _COPY_FILES:
            raise SourceSnapshotError("Git index exceeds entry limit")
        offset = 12
        for _ in range(count):
            start = offset
            flags_offset = offset + 40 + hash_size
            if flags_offset + 2 > end:
                raise SourceSnapshotError("truncated Git index entry")
            flags = int.from_bytes(payload[flags_offset : flags_offset + 2], "big")
            offset = flags_offset + 2
            if flags & 0x8000:
                raise SourceSnapshotError("unsupported assume-unchanged Git index flag")
            if flags & 0x3000:
                raise SourceSnapshotError("unsupported unmerged Git index stages")
            if flags & 0x4000:
                if offset + 2 > end:
                    raise SourceSnapshotError("truncated extended Git index flags")
                extended = int.from_bytes(payload[offset : offset + 2], "big")
                offset += 2
                if extended & 0x2000:
                    raise SourceSnapshotError("unsupported intent-to-add Git index flag")
                if extended:
                    raise SourceSnapshotError("unsupported skip-worktree/extended Git index flag")
            terminator = payload.find(b"\0", offset, end)
            if terminator < 0:
                raise SourceSnapshotError("unterminated Git index path")
            offset = start + ((terminator + 1 - start + 7) // 8) * 8
            if offset > end:
                raise SourceSnapshotError("truncated Git index padding")
        while offset < end:
            if offset + 8 > end:
                raise SourceSnapshotError("truncated Git index extension")
            extension = payload[offset : offset + 4]
            length = int.from_bytes(payload[offset + 4 : offset + 8], "big")
            if extension[:1].islower():
                raise SourceSnapshotError("unsupported mandatory/split/sparse Git index extension")
            offset += 8 + length
            if offset > end:
                raise SourceSnapshotError("truncated Git index extension payload")

    def tree(self, head: str) -> dict[str, GitEntry]:
        result: dict[str, GitEntry] = {}
        for record in self._run("ls-tree", "-r", "-z", head).split(b"\0"):
            if not record:
                continue
            header, separator, raw_path = record.partition(b"\t")
            fields = header.split(b" ")
            if not separator or len(fields) != 3:
                raise SourceSnapshotError("malformed Git HEAD tree")
            result[_relative_path(raw_path)] = GitEntry(fields[0].decode("ascii"), fields[2].decode("ascii"))
        return result

    def require_untransformed(self, paths: tuple[str, ...], entries: dict[str, GitEntry]) -> None:
        attributes = {self.repo_root / ".gitattributes", self._common_dir / "info" / "attributes"}
        for relative in paths:
            path = self.repo_root / relative
            attributes.update(parent / ".gitattributes" for parent in path.parents if parent.is_relative_to(self.repo_root))
        for path in attributes:
            payload = _optional_file(path)
            if payload is not None and any(line.strip() and not line.lstrip().startswith(b"#") for line in payload.splitlines()):
                raise SourceSnapshotError("Git attributes are unsupported for raw source observation")
            if path.is_relative_to(self.repo_root):
                entry = entries.get(path.relative_to(self.repo_root).as_posix())
                if entry is not None:
                    payload = self._run("cat-file", "blob", entry.oid)
                    if any(line.strip() and not line.lstrip().startswith(b"#") for line in payload.splitlines()):
                        raise SourceSnapshotError("indexed Git attributes are unsupported for raw source observation")

    def source_dirty(self, source_root: Path, head: str, index: dict[str, GitEntry]) -> bool:
        tree = self.tree(head)
        relative_source = source_root.relative_to(self.repo_root)
        prefix = "" if relative_source == Path(".") else relative_source.as_posix()
        source_paths = tuple(path for path in tree.keys() | index.keys() if not prefix or path.startswith(prefix + "/"))
        self.require_untransformed(source_paths, tree | index)
        dirty = False
        for relative in source_paths:
            old = tree.get(relative)
            staged = index.get(relative)
            if staged is None:
                dirty = True
                continue
            if staged.mode == "160000" or (old is not None and old.mode == "160000"):
                raise SourceSnapshotError("source Git submodules are unsupported")
            path = self.repo_root / relative
            try:
                payload, mode = _read_file(path, limit=_FILE_LIMIT)
            except FileNotFoundError:
                dirty = dirty or old is not None
                continue
            except OSError as exc:
                raise SourceSnapshotError(f"source path must be regular without symbolic links: {relative}") from exc
            if staged.mode == "120000" or (old is not None and old.mode == "120000"):
                raise SourceSnapshotError("symbolic links under tracked source are unsupported")
            oid = hashlib.new(self.algorithm, b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()
            observed_mode = "100755" if mode & stat.S_IXUSR else "100644"
            dirty = dirty or old is None or old.oid != oid or (self.filemode and old.mode != observed_mode)
        return dirty

    def changed_worktree_paths(self, baseline: str) -> tuple[str, ...]:
        """Match the ratchet's added/modified paths without working-tree Git."""
        tree = self.tree(baseline)
        index = self.index()
        paths = tuple(sorted(tree.keys() | index.keys()))
        self.require_untransformed(paths, tree | index)
        changed: list[str] = []
        for relative in paths:
            old = tree.get(relative)
            staged = index.get(relative)
            if staged is None:
                continue  # The ratchet's ACMR selection excludes deletions.
            if staged.mode == "160000" or (old is not None and old.mode == "160000"):
                raise SourceSnapshotError("working-tree metadata comparison does not support submodules")
            try:
                entry = _working_entry(self.repo_root / relative)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SourceSnapshotError("cannot compare working-tree bytes without symbolic links") from exc
            if entry is None:
                continue
            payload, observed_mode = entry
            if old is not None and (old.mode == "120000") != (observed_mode == "120000"):
                continue  # ACMR excludes regular/symlink type changes (T).
            oid = hashlib.new(self.algorithm, b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()
            if old is None or old.oid != oid or (self.filemode and old.mode != observed_mode):
                changed.append(relative)
        return tuple(changed)


@dataclass(slots=True)
class _MetadataOperation:
    repo_root: Path
    thread_id: int
    stack: ExitStack
    view: TrustedGitRepository | None = None

    def repository(self) -> TrustedGitRepository:
        if self.view is None:
            self.view = self.stack.enter_context(TrustedGitRepository(self.repo_root))
        return self.view


_METADATA_OPERATION: ContextVar[_MetadataOperation | None] = ContextVar("elspeth_git_metadata_operation", default=None)


@contextmanager
def git_metadata_operation(repo_root: Path) -> Iterator[None]:
    """Reuse one lazy immutable view in this synchronous operation and thread."""
    root = Path(os.path.abspath(repo_root))
    current = _METADATA_OPERATION.get()
    if current is not None and current.repo_root == root and current.thread_id == get_ident():
        yield
        return
    with ExitStack() as stack:
        operation = _MetadataOperation(root, get_ident(), stack)
        token = _METADATA_OPERATION.set(operation)
        try:
            yield
        finally:
            _METADATA_OPERATION.reset(token)


def _revision(value: str) -> str:
    if not value or value.startswith("-") or any(character in value for character in ("\0", "\n", "\r", ":")):
        raise SourceSnapshotError("unsupported option-like or malformed Git revision")
    return value


def _object_spec(value: str) -> str:
    revision, separator, path = value.partition(":")
    if not separator:
        raise SourceSnapshotError("unsupported Git object/path command")
    return f"{_revision(revision)}:{_relative_path(path.encode('utf-8'))}"


def _literal_path(value: str) -> str:
    return "." if value == "." else _relative_path(value.encode("utf-8"))


def _metadata_command(view: TrustedGitRepository, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    if len(arguments) == 2 and arguments[0] == "show":
        return view._run_result("cat-file", "blob", _object_spec(arguments[1]))
    if len(arguments) == 3 and arguments[:2] == ["cat-file", "-e"]:
        return view._run_result("cat-file", "-e", _object_spec(arguments[2]))
    if len(arguments) == 4 and arguments[:3] == ["rev-parse", "--verify", "--quiet"]:
        return view._run_result("rev-parse", "--verify", "--quiet", "--end-of-options", _revision(arguments[3]))
    if len(arguments) == 6 and arguments[:3] == ["ls-tree", "-r", "--name-only"] and arguments[4] == "--":
        return view._run_result("ls-tree", "-r", "--name-only", _revision(arguments[3]), "--", _literal_path(arguments[5]))
    if len(arguments) == 8 and arguments[:4] == ["diff", "--name-only", "-z", "--diff-filter=ACMRT"] and arguments[6] == "--":
        return view._run_result(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            *arguments[1:4],
            _revision(arguments[4]),
            _revision(arguments[5]),
            "--",
            _literal_path(arguments[7]),
        )
    if len(arguments) == 6 and arguments[:4] == ["diff", "--name-only", "--diff-filter=ACMR", "-z"] and arguments[5] == "--":
        baseline = view._run_result("rev-parse", "--verify", "--quiet", "--end-of-options", f"{_revision(arguments[4])}^{{commit}}")
        if baseline.returncode != 0:
            return view._run_result(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--name-only",
                "--diff-filter=ACMR",
                "-z",
                _revision(arguments[4]),
                "HEAD",
                "--",
            )
        paths = view.changed_worktree_paths(baseline.stdout.strip().decode("ascii"))
        encoded = b"".join(path.encode("utf-8") + b"\0" for path in paths)
        return subprocess.CompletedProcess([_GIT, *arguments], 0, encoded, b"")
    raise SourceSnapshotError("unsupported Git metadata command shape")


def run_git_metadata(arguments: list[str], *, repo_root: Path) -> subprocess.CompletedProcess[str]:
    """Execute only the finite raw metadata grammar used by the audit commands."""
    with git_metadata_operation(repo_root):
        operation = _METADATA_OPERATION.get()
        if operation is None:
            raise SourceSnapshotError("Git metadata operation is unavailable")
        result = _metadata_command(operation.repository(), arguments)
        try:
            return subprocess.CompletedProcess(arguments, result.returncode, result.stdout.decode("utf-8"), result.stderr.decode("utf-8"))
        except UnicodeError as exc:
            raise SourceSnapshotError("Git metadata command output must be valid UTF-8") from exc
