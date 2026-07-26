"""Session persistence for the composer MCP server.

Sessions are stored as JSON files in the scratch directory.
Each file contains the serialized CompositionState (via to_dict/from_dict
round-trip) plus session metadata.

Layer: L3 (application). Imports from L3 (web.composer.state).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if importlib.util.find_spec("fcntl") is None:  # pragma: no cover - Windows fallback
    fcntl_module: Any = None
else:
    import fcntl as fcntl_module

from elspeth.web.composer.state import CompositionState, PipelineMetadata

# Tier-3 boundary: session_id arrives as an LLM-controlled MCP argument.
# The valid shape is the output of ``new_session`` — ``uuid.uuid4().hex[:12]``
# — a 12-character lowercase hex string. Anything else is either a client
# bug or a path-traversal attempt; either way, reject at the boundary
# rather than coerce (silent coercion would re-point the agent's intended
# session to a different file, a meaning-changing operation).
_SESSION_ID_RE = re.compile(r"^[a-f0-9]{12}$")

# Process-local fast path for thread-level serialization. See
# ``SessionManager._locked_session`` for the cross-process lock that pairs
# with this registry when ``fcntl`` is available.
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_REGISTRY_MUTEX = threading.Lock()


class InvalidSessionIdError(ValueError):
    """Raised when a session_id does not match the allowed shape.

    Subclasses ``ValueError`` for compatibility with callers that treat
    invalid identifier syntax as a value-domain failure. The MCP tool
    boundary converts this specific exception to ``ToolArgumentError``;
    generic ``ValueError`` escaping dispatch remains a plugin crash.
    """

    def __init__(self, session_id: str) -> None:
        # The message echoes only the caller-supplied value — never a
        # server-side filesystem path — so no information is leaked back
        # to the LLM that it didn't already provide.
        super().__init__(f"Invalid session_id: {session_id!r}")
        self.session_id = session_id


class CorruptSessionFileError(ValueError):
    """Raised when a canonical session file cannot be parsed as a session."""

    def __init__(self, session_id: str, reason: str) -> None:
        super().__init__(f"Corrupt session file for {session_id}: {reason}")
        self.session_id = session_id
        self.reason = reason


class StaleSessionVersionError(ValueError):
    """Raised when a save is not based on the current durable session."""

    def __init__(self, session_id: str, *, incoming_version: int, on_disk_version: int) -> None:
        super().__init__(
            f"Refusing stale save for {session_id}: incoming version {incoming_version} "
            f"does not match the current base at on-disk version {on_disk_version}."
        )
        self.session_id = session_id
        self.incoming_version = incoming_version
        self.on_disk_version = on_disk_version


def _validate_session_id(session_id: str) -> None:
    """Enforce the session_id shape at the filesystem boundary.

    Called from ``_session_path`` so every read/write/delete path inherits
    the guard automatically — a future method that calls ``_session_path``
    cannot accidentally bypass validation.

    The MCP tool schema declares ``session_id`` as ``"type": "string"``,
    so non-str inputs are a contract violation, not an attack. If one
    slips through, ``re.Pattern.fullmatch`` raises ``TypeError`` which
    the server's top-level handler converts to a clean tool error —
    equivalent security outcome, one fewer defensive branch.
    """
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise InvalidSessionIdError(session_id)


def _session_lock(session_id: str) -> threading.Lock:
    """Return the process-local mutex guarding one canonical session ID."""
    if session_id in _SESSION_LOCKS:
        return _SESSION_LOCKS[session_id]
    with _SESSION_LOCKS_REGISTRY_MUTEX:
        if session_id not in _SESSION_LOCKS:
            _SESSION_LOCKS[session_id] = threading.Lock()
        return _SESSION_LOCKS[session_id]


@dataclass(frozen=True, slots=True)
class SessionToken:
    """Opaque compare-and-swap evidence bound to one session's exact bytes."""

    _session_id: str = field(repr=False)
    _digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SessionCheckout:
    """A validated state snapshot paired with its immutable durable evidence."""

    session_id: str
    state: CompositionState
    token: SessionToken


@dataclass(frozen=True, slots=True)
class SessionSaveResult:
    """Result of a successful create, idempotent save, or CAS replacement."""

    path: Path
    token: SessionToken


class SessionCheckoutMismatchError(ValueError):
    """Raised when a save targets a session other than the active checkout."""

    def __init__(self, requested_session_id: str, active_session_id: str | None) -> None:
        super().__init__(f"Session {requested_session_id} is not the active checkout")
        self.requested_session_id = requested_session_id
        self.active_session_id = active_session_id


def _session_token(session_id: str, raw: bytes) -> SessionToken:
    """Return opaque CAS evidence for exact durable bytes of one session."""
    return SessionToken(session_id, hashlib.sha256(raw).hexdigest())


class SessionNotFoundError(Exception):
    """Raised when a session ID does not correspond to a saved file."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session not found: {session_id}")
        self.session_id = session_id


class SessionManager:
    """Manages CompositionState sessions as JSON files on disk.

    Each session is a single JSON file: ``{scratch_dir}/{session_id}.json``.
    The file contains the full CompositionState serialized via to_dict().
    """

    def __init__(self, scratch_dir: Path) -> None:
        self._dir = scratch_dir

    def new_session(self, *, name: str = "Untitled Pipeline") -> tuple[str, CompositionState]:
        """Create a new empty session. Does not persist until save() is called."""
        session_id = uuid.uuid4().hex[:12]
        state = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(name=name),
            version=1,
        )
        return session_id, state

    def save(self, session_id: str, state: CompositionState) -> Path:
        """Create or idempotently re-save without overwrite authority.

        Compatibility callers that need to update an existing session must
        first call ``checkout()`` and supply its token to ``save_if_current``.
        A tokenless divergent overwrite always fails closed.
        """
        return self.save_if_current(session_id, state, expected_token=None).path

    def save_if_current(
        self,
        session_id: str,
        state: CompositionState,
        *,
        expected_token: SessionToken | None,
    ) -> SessionSaveResult:
        """Persist state only when explicit checkout evidence is still current."""
        path = self._session_path(session_id)
        self._dir.mkdir(parents=True, exist_ok=True)
        data = state.to_dict()
        serialized = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        with self._locked_session(session_id):
            if path.exists():
                on_disk, raw = self._read_session_snapshot(path, session_id)
                current_token = _session_token(session_id, raw)

                # Idempotency is byte-exact: an already-durable request is a
                # successful no-op even when checkout evidence is stale or
                # absent. Return current evidence without replacing the file.
                if raw == serialized:
                    return SessionSaveResult(path=path, token=current_token)

                if expected_token is None or expected_token._session_id != session_id or expected_token._digest != current_token._digest:
                    raise StaleSessionVersionError(
                        session_id,
                        incoming_version=state.version,
                        on_disk_version=on_disk.version,
                    )
                if state.version < on_disk.version:
                    raise StaleSessionVersionError(
                        session_id,
                        incoming_version=state.version,
                        on_disk_version=on_disk.version,
                    )
            elif expected_token is not None:
                # A manager that previously loaded/saved this session cannot
                # silently resurrect it after another actor deleted the file.
                raise StaleSessionVersionError(
                    session_id,
                    incoming_version=state.version,
                    on_disk_version=0,
                )
            self._atomic_write(path, serialized)
            return SessionSaveResult(path=path, token=_session_token(session_id, serialized))

    def checkout(self, session_id: str) -> SessionCheckout:
        """Load validated state together with its exact durable CAS evidence."""
        path = self._session_path(session_id)
        if not path.exists():
            raise SessionNotFoundError(session_id)
        try:
            with self._locked_session(session_id):
                state, raw = self._read_session_snapshot(path, session_id)
                return SessionCheckout(
                    session_id=session_id,
                    state=state,
                    token=_session_token(session_id, raw),
                )
        except FileNotFoundError as exc:
            raise SessionNotFoundError(session_id) from exc

    def load(self, session_id: str) -> CompositionState:
        """Load session state without retaining overwrite authority."""
        return self.checkout(session_id).state

    def delete(self, session_id: str) -> None:
        """Delete a saved session while preserving its audit events sidecar.

        The canonical session JSON is mutable state and is removed on delete.
        The audit events sidecar (``{scratch}/{session_id}.events.jsonl``)
        is append-only Tier-1 evidence and must outlive the session JSON so
        the final delete_session tombstone can be appended without erasing the
        prior tool-decision history.
        """
        path = self._session_path(session_id)
        if not path.exists():
            raise SessionNotFoundError(session_id)
        try:
            with self._locked_session(session_id):
                if not path.exists():
                    raise SessionNotFoundError(session_id)
                path.unlink()
        except FileNotFoundError as exc:
            raise SessionNotFoundError(session_id) from exc

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all saved sessions with ID, name, and version."""
        if not self._dir.exists():
            return []
        sessions: list[dict[str, Any]] = []
        for path in sorted(self._dir.glob("*.json")):
            session_id = path.stem
            if not _SESSION_ID_RE.fullmatch(session_id):
                continue
            found, state = self._read_session_state_for_listing(path, session_id)
            if not found or state is None:
                continue
            sessions.append(
                {
                    "session_id": session_id,
                    "name": state.metadata.name,
                    "version": state.version,
                }
            )
        return sessions

    def _session_path(self, session_id: str) -> Path:
        # Chokepoint guard — every filesystem-touching method (save, load,
        # delete) routes here, so validating once covers all three.
        _validate_session_id(session_id)
        return self._dir / f"{session_id}.json"

    def _lock_path(self, session_id: str) -> Path:
        _validate_session_id(session_id)
        return self._dir / f".{session_id}.lock"

    @contextmanager
    def _locked_session(self, session_id: str) -> Iterator[None]:
        """Serialize version-check + replace across threads and processes."""
        with _session_lock(session_id):
            if fcntl_module is None:
                yield
                return
            lock_path = self._lock_path(session_id)
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl_module.flock(lock_file.fileno(), fcntl_module.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl_module.flock(lock_file.fileno(), fcntl_module.LOCK_UN)

    def _read_session_state_for_listing(self, path: Path, session_id: str) -> tuple[bool, CompositionState | None]:
        """Load one session for list_sessions without failing the whole listing."""
        try:
            return True, self._read_session_state(path, session_id)
        except (CorruptSessionFileError, FileNotFoundError):
            return False, None

    def _read_session_state(self, path: Path, session_id: str) -> CompositionState:
        """Parse one canonical session file into a validated state snapshot."""
        state, _raw = self._read_session_snapshot(path, session_id)
        return state

    def _read_session_snapshot(self, path: Path, session_id: str) -> tuple[CompositionState, bytes]:
        """Read validated state and the exact bytes used as its CAS identity."""
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise CorruptSessionFileError(session_id, f"could not read file: {exc}") from exc
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CorruptSessionFileError(session_id, "invalid UTF-8") from exc
        if decoded.startswith("\ufeff"):
            raise CorruptSessionFileError(session_id, "UTF-8 BOM is not permitted")
        try:
            data = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise CorruptSessionFileError(session_id, f"invalid JSON: {exc.msg}") from exc
        if type(data) is not dict:
            raise CorruptSessionFileError(session_id, "top-level JSON value must be an object")
        try:
            state = CompositionState.from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptSessionFileError(session_id, f"invalid session payload: {exc}") from exc
        return state, raw

    def _atomic_write(self, path: Path, serialized: bytes) -> None:
        """Write to a sibling tempfile and replace the canonical file atomically."""
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "wb") as tmp_file:
                tmp_file.write(serialized)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            tmp_path.replace(path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
