"""Read-only admission of every configured local store before bootstrap."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine.url import make_url

from elspeth.core.landscape.database import LandscapeDB
from elspeth.web.auth.local import validate_existing_local_auth_store
from elspeth.web.config import WebSettings
from elspeth.web.schema_probe import SchemaState, probe_session_schema
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import explain_non_current_schema


def _sqlite_path(url: str) -> Path | None:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        raise ValueError("Local store admission requires SQLite targets")
    if parsed.database in (None, ":memory:"):
        return None
    assert parsed.database is not None
    if parsed.database.startswith("file:") or parsed.query:
        raise ValueError("Local store admission requires plain SQLite file URLs")
    path = Path(parsed.database).absolute()
    if path.is_symlink():
        raise ValueError("Local database targets must not be symlinks; preserve existing stores")
    return path.resolve()


def admit_local_stores(settings: WebSettings) -> None:
    """Refuse unsupported targets before creating or modifying another store.

    This is a startup admission phase, not an atomic transaction across stores.
    Mutation boundaries also validate their own current target.
    """
    session_url = settings.get_session_db_url()
    landscape_url = settings.get_landscape_url()
    session_path = _sqlite_path(session_url)
    landscape_path = _sqlite_path(landscape_url)
    auth_path = settings.data_dir / "auth.db" if settings.auth_provider == "local" else None
    paths = [path for path in (session_path, landscape_path, auth_path) if path is not None]
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if path.resolve() == other.resolve() or (path.exists() and other.exists() and path.samefile(other)):
                raise ValueError("Session, Landscape and credential stores require distinct targets; preserve existing stores")
    if session_path is not None and session_path.exists():
        engine = create_session_engine(session_url, read_only=True)
        try:
            if probe_session_schema(engine) not in (SchemaState.MISSING, SchemaState.CURRENT):
                explain_non_current_schema(engine)
        finally:
            engine.dispose()
    if landscape_path is not None and landscape_path.exists():
        LandscapeDB.admit_existing_sqlite_store(landscape_url, passphrase=settings.landscape_passphrase)
    if auth_path is not None and (auth_path.exists() or auth_path.is_symlink()):
        validate_existing_local_auth_store(auth_path)
