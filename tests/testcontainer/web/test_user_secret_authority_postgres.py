"""PostgreSQL serialization proof for the user-secret mutation authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
import sqlalchemy as sa

from elspeth.web.secrets.user_store import RepositoryUserSecretAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import user_secrets_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


def test_postgres_same_key_upserts_serialize_across_independent_engines(
    external_deployment_postgres_url: str,
) -> None:
    first_engine = create_session_engine(external_deployment_postgres_url)
    second_engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(first_engine)
    first = RepositoryUserSecretAuthority(first_engine)
    second = RepositoryUserSecretAuthority(second_engine)
    name = f"AUTHORITY_RACE_{uuid4().hex}"
    barrier = Barrier(2)

    def write(authority: RepositoryUserSecretAuthority, marker: int) -> None:
        barrier.wait(timeout=10)
        authority.upsert_encrypted_secret(
            name=name,
            user_id="postgres-authority-user",
            auth_provider_type="local",
            encrypted_value=f"ciphertext-{marker}".encode(),
            salt=bytes([marker]) * 16,
        )

    try:
        with first_engine.connect() as conn:
            before = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (pool.submit(write, first, 1), pool.submit(write, second, 2))
            for future in futures:
                future.result(timeout=20)

        with first_engine.connect() as conn:
            rows = conn.execute(
                sa.select(user_secrets_table).where(
                    sa.and_(
                        user_secrets_table.c.name == name,
                        user_secrets_table.c.user_id == "postgres-authority-user",
                        user_secrets_table.c.auth_provider_type == "local",
                    )
                )
            ).all()
            after = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()

        assert len(rows) == 1
        row = rows[0]
        assert row.version == 2
        assert row.encrypted_value in {b"ciphertext-1", b"ciphertext-2"}
        assert row.salt in {b"\x01" * 16, b"\x02" * 16}
        assert before <= row.created_at <= after
        assert before <= row.updated_at <= after
    finally:
        first_engine.dispose()
        second_engine.dispose()
