"""Mixed replica settings cannot silently widen an active shared bucket."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.rate_limit_authority import RateLimitPolicyMismatchError, RepositoryRateLimitAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import rate_limit_buckets_table, rate_limit_events_table
from elspeth.web.sessions.schema import initialize_session_schema


@pytest.mark.parametrize("changed", ["limit", "window"])
def test_mixed_replica_policy_refuses_without_consuming_or_resetting(changed: str) -> None:
    engine = create_session_engine("sqlite:///:memory:")
    try:
        initialize_session_schema(engine)
        authority = RepositoryRateLimitAuthority(engine, signing_key=b"s" * 32)
        assert authority.admit(scope="auth", subject="same-user", limit=1, window_seconds=60).allowed
        with pytest.raises(RateLimitPolicyMismatchError):
            authority.admit(
                scope="auth", subject="same-user", limit=5 if changed == "limit" else 1, window_seconds=120 if changed == "window" else 60
            )
        # The original replica still observes its exhausted shared capacity.
        assert not authority.admit(scope="auth", subject="same-user", limit=1).allowed
        with engine.begin() as conn:
            assert len(conn.execute(select(rate_limit_events_table)).all()) == 1
            assert conn.execute(select(rate_limit_buckets_table.c.request_limit)).scalar_one() == 1
            expired = database_now(conn) - timedelta(seconds=1)
            conn.execute(update(rate_limit_buckets_table).values(expires_at=expired))
        # A fully expired bucket can adopt the new deployed policy.
        assert authority.admit(scope="auth", subject="same-user", limit=5).allowed
    finally:
        engine.dispose()
