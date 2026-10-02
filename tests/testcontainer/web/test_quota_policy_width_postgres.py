"""Exercise the AWS five-GiB policy through PostgreSQL identity activation."""

import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.engine import make_url

from elspeth.web.auth.models import IdentityClaims
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import IdentityActivated, RepositoryIdentityAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import quota_policies_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


def test_activation_persists_five_gib_and_large_token_limits(external_deployment_postgres_url: str) -> None:
    database = f"quota_width_{uuid.uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    try:
        with control.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    finally:
        control.dispose()
    url = make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False)
    engine = create_session_engine(url)
    try:
        initialize_session_schema(engine)
        authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
        recorded: list[IdentityActivated] = []
        outcome = authority.bootstrap_admin(
            claims=IdentityClaims(provider="local", subject="quota-operator", username="quota-operator"),
            note="AWS policy width regression",
            quota_tokens_per_day=2**31 + 1,
            quota_storage_bytes=5 * 1024**3,
            record=recorded.append,
        )
        assert len(recorded) == 1
        assert recorded[0].quota_written
        with engine.begin() as conn:
            conn.execute(update(quota_policies_table).values(dual_control_above_tokens=2**31 + 2))
            row = conn.execute(select(quota_policies_table)).one()
            assert row.identity_id == outcome.record.identity_id
            assert row.storage_bytes == 5368709120
            assert row.tokens_per_day == 2**31 + 1
            assert row.dual_control_above_tokens == 2**31 + 2
    finally:
        engine.dispose()
