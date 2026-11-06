"""Immutable Git/source snapshot binding for staged review artifacts.

The tier-model scanner consumes Python source plus YAML allowlist files.  This
module binds those exact bytes to a real Git revision and proves that every
consumed input has a tracked logical identity.  A transaction candidate may
provide the physical allowlist bytes while retaining the public allowlist's
logical Git identity.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from elspeth_lints.core.allowlist import iter_allowlist_yaml_paths
from elspeth_lints.core.trusted_git import (
    SourceSnapshotError as SourceSnapshotError,
)
from elspeth_lints.core.trusted_git import (
    TrustedGitRepository,
    read_regular_file,
    repository_root,
    require_directory,
)
from elspeth_lints.rules.trust_tier.tier_model.rule import iter_scannable_python_files


class SourceSnapshotChangedError(SourceSnapshotError):
    """The source snapshot changed while it was being observed."""


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Exact immutable binding carried by a review-bundle envelope."""

    source_rev: str
    source_dirty: bool
    source_snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class _SnapshotInput:
    label: str
    physical_path: Path
    logical_path: Path


def source_snapshot_sha256(
    *,
    source_root: Path,
    allowlist_dir: Path,
    _iter_python_files: Callable[[Path], Iterable[Path]] | None = None,
) -> str:
    """Hash exactly the Python/YAML bytes consumed by the tier-model scanner."""
    inputs = _snapshot_inputs(
        source_root=Path(source_root),
        allowlist_dir=Path(allowlist_dir),
        logical_allowlist_dir=Path(allowlist_dir),
        iter_python_files=_iter_python_files,
    )
    return _snapshot_digest(inputs)


def current_git_head(repo_root: Path) -> str:
    """Return the exact full lowercase Git HEAD or fail closed."""
    try:
        with TrustedGitRepository(repo_root) as observed:
            return observed.head()
    except SourceSnapshotError as exc:
        raise SourceSnapshotError(f"cannot bind source snapshot to Git HEAD: {exc}") from exc


def tracked_relative_paths(repo_root: Path) -> tuple[str, ...]:
    """Return the callback-free whole-index census of safe UTF-8 relative paths."""
    with TrustedGitRepository(repo_root) as observed:
        if observed.repo_root != Path(os.path.abspath(repo_root)):
            raise SourceSnapshotError("tracked path enumeration requires the canonical repository root")
        return tuple(sorted(observed.index()))


def capture_source_snapshot(
    *,
    source_root: Path,
    allowlist_dir: Path,
    logical_allowlist_dir: Path | None = None,
) -> SourceSnapshot:
    """Capture one stable, tracked, exact scanner-input snapshot.

    ``allowlist_dir`` supplies bytes.  ``logical_allowlist_dir`` supplies Git
    path identity and defaults to that same directory; coherent-publish
    verification passes a private transaction candidate as the former and the
    public allowlist path as the latter.
    """
    first = observe_source_snapshot(
        source_root=source_root,
        allowlist_dir=allowlist_dir,
        logical_allowlist_dir=logical_allowlist_dir,
    )
    second = observe_source_snapshot(
        source_root=source_root,
        allowlist_dir=allowlist_dir,
        logical_allowlist_dir=logical_allowlist_dir,
    )
    if first != second:
        changed: list[str] = []
        if first.source_rev != second.source_rev:
            changed.append("Git HEAD")
        if first.source_dirty != second.source_dirty:
            changed.append("source dirty state")
        if first.source_snapshot_sha256 != second.source_snapshot_sha256:
            changed.append("source/allowlist inputs")
        raise SourceSnapshotChangedError(f"{', '.join(changed)} changed while capturing source snapshot")
    return second


def observe_source_snapshot(
    *,
    source_root: Path,
    allowlist_dir: Path,
    logical_allowlist_dir: Path | None = None,
) -> SourceSnapshot:
    """Return one source observation for a caller that brackets its own work.

    Standalone consumers should use :func:`capture_source_snapshot`, which
    proves a stable double observation.  Long-running derivations and
    verification routines call this lower-level operation once before and once
    after their work; nesting a double capture at both ends adds no stronger
    temporal guarantee and needlessly doubles every Git walk and byte read.
    """
    try:
        resolved_source_root = Path(os.path.abspath(source_root))
        resolved_allowlist_dir = Path(os.path.abspath(allowlist_dir))
        resolved_logical_allowlist_dir = Path(os.path.abspath(logical_allowlist_dir or allowlist_dir))
    except OSError as exc:
        raise SourceSnapshotError(f"cannot resolve source snapshot roots: {exc}") from exc
    return _observe_source_snapshot(
        source_root=resolved_source_root,
        allowlist_dir=resolved_allowlist_dir,
        logical_allowlist_dir=resolved_logical_allowlist_dir,
    )


def _observe_source_snapshot(
    *,
    source_root: Path,
    allowlist_dir: Path,
    logical_allowlist_dir: Path,
) -> SourceSnapshot:
    for label, path in (
        ("source_root", source_root),
        ("allowlist_dir", allowlist_dir),
        ("logical_allowlist_dir", logical_allowlist_dir),
    ):
        if not path.is_dir():
            raise SourceSnapshotError(f"{label} is not a directory: {path}")
        require_directory(path)

    source_repo = _git_repository_root(source_root)
    allowlist_repo = _git_repository_root(logical_allowlist_dir)
    if source_repo != allowlist_repo:
        raise SourceSnapshotError(
            f"source root and logical allowlist root must resolve inside the same Git repository: {source_repo} != {allowlist_repo}"
        )
    with TrustedGitRepository(source_repo) as observed:
        try:
            source_rev = observed.head()
        except SourceSnapshotError as exc:
            raise SourceSnapshotError(f"cannot bind source snapshot to Git HEAD: {exc}") from exc
        inputs = _snapshot_inputs(
            source_root=source_root,
            allowlist_dir=allowlist_dir,
            logical_allowlist_dir=logical_allowlist_dir,
        )
        index = observed.index()
        _require_tracked_inputs(repo_root=source_repo, inputs=inputs, tracked=set(index))
        source_dirty = observed.source_dirty(source_root, source_rev, index)
        return SourceSnapshot(
            source_rev=source_rev,
            source_dirty=source_dirty,
            source_snapshot_sha256=_snapshot_digest(inputs),
        )


def _snapshot_inputs(
    *,
    source_root: Path,
    allowlist_dir: Path,
    logical_allowlist_dir: Path,
    iter_python_files: Callable[[Path], Iterable[Path]] | None = None,
) -> tuple[_SnapshotInput, ...]:
    if not source_root.is_dir():
        raise SourceSnapshotError(f"source_root is not a directory: {source_root}")
    if not allowlist_dir.is_dir():
        raise SourceSnapshotError(f"allowlist_dir is not a directory: {allowlist_dir}")
    inputs: list[_SnapshotInput] = []
    discovery = iter_scannable_python_files if iter_python_files is None else iter_python_files
    try:
        discovered: Iterable[Path] = discovery(source_root)
        for path in sorted(discovered):
            relative = path.relative_to(source_root)
            inputs.append(
                _SnapshotInput(
                    label=f"source/{relative.as_posix()}",
                    physical_path=path,
                    logical_path=path,
                )
            )
        for path in iter_allowlist_yaml_paths(allowlist_dir):
            relative = path.relative_to(allowlist_dir)
            inputs.append(
                _SnapshotInput(
                    label=f"allowlist/{relative.as_posix()}",
                    physical_path=path,
                    logical_path=logical_allowlist_dir / relative,
                )
            )
    except OSError as exc:
        raise SourceSnapshotError(f"cannot enumerate scanner inputs: {exc}") from exc
    inputs.sort(key=lambda item: item.label)
    labels = [item.label for item in inputs]
    if len(labels) != len(set(labels)):
        raise SourceSnapshotError("source snapshot contains duplicate logical input labels")
    return tuple(inputs)


def _git_repository_root(path: Path) -> Path:
    return repository_root(path)


def _require_tracked_inputs(*, repo_root: Path, inputs: tuple[_SnapshotInput, ...], tracked: set[str]) -> None:
    for item in inputs:
        _reject_symlink_input(item)
        logical_path = item.logical_path
        if not logical_path.is_relative_to(repo_root):
            raise SourceSnapshotError(f"scanner input {item.label!r} does not map inside Git repository {repo_root}")
        relative = logical_path.relative_to(repo_root).as_posix()
        if relative not in tracked:
            raise SourceSnapshotError(
                f"scanner input {item.label!r} must map to a tracked Git path; logical path {relative!r} is untracked"
            )


def _snapshot_digest(inputs: tuple[_SnapshotInput, ...]) -> str:
    records: list[dict[str, str]] = []
    for item in inputs:
        _reject_symlink_input(item)
        try:
            payload = read_regular_file(item.physical_path)
        except (OSError, SourceSnapshotError) as exc:
            raise SourceSnapshotError(f"cannot read scanner input {item.label!r} at {item.physical_path}: {exc}") from exc
        records.append({"path": item.label, "sha256": hashlib.sha256(payload).hexdigest()})
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reject_symlink_input(item: _SnapshotInput) -> None:
    try:
        physical_is_symlink = item.physical_path.is_symlink()
        logical_is_symlink = item.logical_path.is_symlink()
    except OSError as exc:
        raise SourceSnapshotError(f"cannot inspect scanner input {item.label!r}: {exc}") from exc
    if physical_is_symlink or logical_is_symlink:
        raise SourceSnapshotError(
            f"scanner input {item.label!r} must not be a symbolic link (physical={item.physical_path}, logical={item.logical_path})"
        )
