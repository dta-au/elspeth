"""Shared path allowlist helpers for web subsystem.

AD-4: Single definitions used by composer tool validation,
execution validation, and execution runtime guards. Lives in
web/ (not composer/ or execution/) to avoid cross-package coupling.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path, PurePosixPath
from typing import Final, Literal
from uuid import UUID

from elspeth.contracts.composer_paths import McpConfiguredRootAuthority

SOURCE_LOCAL_PATH_OPTION_KEYS: tuple[str, ...] = ("path", "file")
SINK_LOCAL_PATH_OPTION_KEYS: tuple[str, ...] = ("path", "file", "persist_directory")
# The two per-session folders a sink may write under:
# ``data_dir/outputs/<session_id>`` and ``data_dir/blobs/<session_id>``.
SessionFolder = Literal["outputs", "blobs"]
SESSION_FOLDERS: tuple[SessionFolder, ...] = ("outputs", "blobs")
# Local filesystem keys to confine inside a transform's
# ``options["provider_config"]`` dict (RAG retrieval transforms carry a
# Chroma persist_directory there). Confined like sink paths — persist_directory
# is a local read/write target.
NESTED_LOCAL_PATH_OPTION_KEYS: tuple[str, ...] = ("persist_directory",)


def require_mcp_root_context(
    authority: McpConfiguredRootAuthority | None,
    *,
    data_dir: str | Path | None,
    session_id: str | None,
) -> None:
    """Reject contradictory owned configuration before reading user paths."""
    if authority is None:
        return
    if not isinstance(authority, McpConfiguredRootAuthority):
        raise TypeError("MCP path authority must be McpConfiguredRootAuthority")
    if session_id is not None:
        raise ValueError("MCP root authority cannot carry Web session custody")
    if data_dir is not None and Path(data_dir).resolve() != authority.root:
        raise ValueError("MCP configured root contradicts data_dir")


def is_local_path_value(value: object) -> bool:
    """Admit the lexical shape before passing an external value to Path."""
    return type(value) is str and bool(value) and "\x00" not in value


def is_absent_sink_path(plugin: object, mode: object, key: str, value: object) -> bool:
    """Only Chroma client mode permits an explicitly absent local directory.

    Other path keys still require paths, and non-null directories still pass
    through confinement. The plugin config validates the remaining connection.
    """
    return plugin == "chroma_sink" and mode == "client" and key == "persist_directory" and value is None


def resolve_data_path(value: str, data_dir: str) -> Path:
    """Resolve a path value against data_dir (relative) or as-is (absolute).

    Relative paths are joined to data_dir before resolving; absolute paths
    are resolved directly.  Traversal (``../``) is resolved by the OS —
    blocking traversals outside allowed directories is the caller's job
    (via the allowlist helpers below).

    Note: blob-backed source paths are always absolute (canonical
    ``BlobRecord.storage_path``) and are pinned to that exact value by
    ``set_source_from_blob`` and the runtime read guard in
    ``ExecutionService.start_run``.  No legacy relative-path handling is
    needed for blob sources; the runtime read guard enforces the
    audit-integrity contract.
    """
    raw = Path(value)
    if raw.is_absolute():
        return raw.resolve()

    return (Path(data_dir).resolve() / raw).resolve()


def _validate_session_path_segment(session_id: str, *, caller: str) -> None:
    if not session_id or session_id in (".", "..") or "/" in session_id or "\\" in session_id:
        raise ValueError(f"{caller}: malformed session_id {session_id!r} — expected a UUID-shaped path segment")


def managed_blob_directory(data_dir: str) -> Path:
    """Return the deployment-owned blob root for infrastructure probes."""
    return Path(data_dir).resolve() / "blobs"


def _is_uuid_path_segment(value: str) -> bool:
    """Report whether one path segment parses as a UUID.

    ``ValueError`` is the negative answer, not a swallowed fault: it is how
    :class:`~uuid.UUID` reports "this string is not a UUID". The sole caller
    passes an element of ``Path.parts``, which is ``tuple[str, ...]``, so a
    non-string can never reach here and ``AttributeError`` is not caught —
    a wrong-typed caller must crash rather than be reported as "not a UUID".
    """
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def allowed_source_directories(data_dir: str, *, session_id: str | None) -> tuple[Path, ...]:
    """Return session-owned directories from which source paths may read.

    The deployment blob root is shared infrastructure, not authorization.
    A caller without a session identity receives no readable local paths.
    """
    if session_id is None:
        return ()
    _validate_session_path_segment(session_id, caller="allowed_source_directories")
    return (managed_blob_directory(data_dir) / session_id,)


def allowed_sink_directories(data_dir: str, *, session_id: str | None) -> tuple[Path, ...]:
    """Return the set of directories to which sink paths may write.

    Includes only the caller's ``data_dir/outputs/<session_id>`` and
    ``data_dir/blobs/<session_id>`` subtrees. The deployment-level roots are
    shared infrastructure, not authorization boundaries. A sink (or a
    transform's ``provider_config.persist_directory``) must never address
    another session's subtree.

    ``session_id=None`` means the caller has no session identity: the
    result is empty. ``session_id`` is required as a keyword so no call site
    can silently keep unscoped behaviour.

    Raises:
        ValueError: if ``session_id`` is present but could alter the path
            shape (empty, a path separator, or a dot segment). Session ids
            are UUID strings everywhere in the system; anything else
            reaching this boundary is a contract violation upstream.
    """
    base = Path(data_dir).resolve()
    if session_id is None:
        return ()
    _validate_session_path_segment(session_id, caller="allowed_sink_directories")
    return (base / "outputs" / session_id, base / "blobs" / session_id)


def resolve_sink_data_path(value: str, data_dir: str, *, session_id: str | None) -> Path:
    """Resolve a sink path into the caller's session-owned local namespace.

    ``outputs/report.csv`` remains the stable authoring form, but resolves to
    ``outputs/<session_id>/report.csv`` on disk. Already-scoped paths are not
    double-prefixed. Absolute paths and non-output relative paths retain their
    literal resolution so the caller's allowlist check can reject shared or
    foreign locations rather than silently rewriting them.
    """
    raw = Path(value)
    if raw.is_absolute() or session_id is None:
        return resolve_data_path(value, data_dir)

    _validate_session_path_segment(session_id, caller="resolve_sink_data_path")
    if raw.parts and raw.parts[0] == "outputs":
        if len(raw.parts) > 1 and raw.parts[1] == session_id:
            scoped = raw
        elif len(raw.parts) > 1 and _is_uuid_path_segment(raw.parts[1]):
            # An explicitly session-scoped foreign path must remain foreign
            # so the allowlist rejects it. Never adopt it by nesting it under
            # the caller's output directory.
            scoped = raw
        else:
            scoped = Path("outputs") / session_id / Path(*raw.parts[1:])
        return (Path(data_dir).resolve() / scoped).resolve()
    return resolve_data_path(value, data_dir)


def locate_in_session_folder(resolved: Path, data_dir: str | Path, *, session_id: str) -> tuple[SessionFolder, Path] | None:
    """Return which of a session's folders holds ``resolved``, and the path below it.

    ``resolved`` must already be resolved (``resolve_data_path`` or
    ``resolve_sink_data_path``). The answer is ``(folder, relative)`` where
    ``resolved == data_dir/<folder>/<session_id>/<relative>``; ``relative`` is
    ``Path(".")`` for the folder itself. ``None`` when the path lies outside
    both of the session's folders.
    """
    _validate_session_path_segment(session_id, caller="locate_in_session_folder")
    base = Path(data_dir).resolve()
    for folder in SESSION_FOLDERS:
        root = base / folder / session_id
        if resolved.is_relative_to(root):
            return folder, resolved.relative_to(root)
    return None


def session_folder_path(data_dir: str | Path, folder: SessionFolder, *, session_id: str, relative_path: str | Path) -> Path:
    """Return ``data_dir/<folder>/<session_id>/<relative_path>`` (``data_dir`` resolved).

    A pure join: the caller proves ``relative_path`` stays inside the folder
    (``normalise_session_relative_path``) and checks containment of the result.
    """
    _validate_session_path_segment(session_id, caller="session_folder_path")
    return Path(data_dir).resolve() / folder / session_id / relative_path


# Longest session-relative path accepted anywhere one is recorded or bound.
MAX_SESSION_RELATIVE_PATH_CHARS: Final = 1024
# Unicode categories refused in a session-relative path: ``Cc`` (C0, DEL and
# C1, which includes NEL U+0085), ``Zl`` (U+2028) and ``Zp`` (U+2029). Each
# breaks a line in some log viewer or terminal.
_LINE_BREAKING_CATEGORIES: Final = frozenset({"Cc", "Zl", "Zp"})


def normalise_session_relative_path(value: str) -> str:
    """Return ``value`` as a normalised POSIX path that stays inside a session folder.

    Collapses ``.`` segments and repeated separators. Raises ``ValueError`` for
    an empty result, an absolute path, any ``..`` segment, a backslash (a
    separator on another platform) or a NUL byte: none of these can name a
    file below a session folder. Also raises for a value longer than
    ``MAX_SESSION_RELATIVE_PATH_CHARS`` or holding any other control
    character (C0, DEL or C1) or a Unicode line or paragraph separator
    (U+2028, U+2029): such a name is persisted into state and echoed in
    validation messages and logs, where a line break forges a line.
    """
    if "\\" in value or "\x00" in value:
        raise ValueError("a session-relative path must not contain a backslash or NUL byte")
    if len(value) > MAX_SESSION_RELATIVE_PATH_CHARS:
        raise ValueError(f"a session-relative path must not be longer than {MAX_SESSION_RELATIVE_PATH_CHARS} characters")
    if any(unicodedata.category(char) in _LINE_BREAKING_CATEGORIES for char in value):
        raise ValueError("a session-relative path must not contain a control character")
    pure = PurePosixPath(value)
    if pure.is_absolute():
        raise ValueError("a session-relative path must not be absolute")
    if ".." in pure.parts:
        raise ValueError("a session-relative path must not contain '..'")
    normalised = pure.as_posix()
    if normalised == ".":
        raise ValueError("a session-relative path must name a file or directory below the session folder")
    return normalised
