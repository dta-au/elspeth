"""Durable session-family revocation shared by every application replica."""

from datetime import UTC, datetime

from sqlalchemy import Engine, delete, insert, select, update

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.coordination.membership_authority import _DATABASE_CLOCK_SQL, _database_clock_value
from elspeth.web.sessions.models import auth_session_families_table as families


class RepositoryAuthSessionAuthority:
    """Refresh preserves a family; revoking it cannot race into a new family.

    Revocation is monotonic. Missing and expired rows always refuse, including
    after expiry cleanup, so removal can never resurrect a credential.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._clock_sql = _DATABASE_CLOCK_SQL[engine.dialect.name]

    def create(self, *, family_id: str, identity_id: str, provider: AuthProviderType, expires_at: int) -> None:
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            expired = (
                conn.execute(
                    select(families.c.family_id)
                    .where(families.c.expires_at <= now)
                    .order_by(families.c.expires_at, families.c.family_id)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            if expired:
                conn.execute(delete(families).where(families.c.family_id.in_(expired)))
            conn.execute(
                insert(families).values(
                    family_id=family_id,
                    identity_id=identity_id,
                    provider=provider,
                    expires_at=datetime.fromtimestamp(expires_at, UTC),
                )
            )

    def is_active(self, *, family_id: str, identity_id: str, provider: AuthProviderType) -> bool:
        with self._engine.connect() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            return (
                conn.execute(
                    select(families.c.family_id).where(
                        families.c.family_id == family_id,
                        families.c.identity_id == identity_id,
                        families.c.provider == provider,
                        families.c.revoked_at.is_(None),
                        families.c.expires_at > now,
                    )
                ).first()
                is not None
            )

    def revoke(self, *, family_id: str, identity_id: str, provider: AuthProviderType) -> None:
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            conn.execute(
                update(families)
                .where(
                    families.c.family_id == family_id,
                    families.c.identity_id == identity_id,
                    families.c.provider == provider,
                    families.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
