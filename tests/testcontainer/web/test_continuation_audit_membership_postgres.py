"""Real PostgreSQL execution of the shared audit-membership SQL controls."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import make_url
from tests.helpers.postgres_target import postgres_test_target
from tests.unit.web.sessions.test_continuation_audit_membership import (
    test_duplicate_rows_remain_distinct_without_slot_multiplication as test_duplicate_rows_remain_distinct_without_slot_multiplication,
)
from tests.unit.web.sessions.test_continuation_audit_membership import (
    test_each_private_identity_retains_disagreeing_copies as test_each_private_identity_retains_disagreeing_copies,
)
from tests.unit.web.sessions.test_continuation_audit_membership import (
    test_each_public_identity_retains_disagreeing_partner as test_each_public_identity_retains_disagreeing_partner,
)
from tests.unit.web.sessions.test_continuation_audit_membership import (
    test_exact_members_ignore_unrelated_payload_before_limit as test_exact_members_ignore_unrelated_payload_before_limit,
)
from tests.unit.web.sessions.test_continuation_audit_membership import (
    test_identity_requires_json_string_equality as test_identity_requires_json_string_equality,
)
from tests.unit.web.sessions.test_continuation_audit_membership import (
    test_nonidentity_metadata_is_not_an_early_admission_filter as test_nonidentity_metadata_is_not_an_early_admission_filter,
)
from tests.unit.web.sessions.test_continuation_audit_membership import (
    test_object_root_claim_beside_valid_array_remains_duplicate_candidate as test_object_root_claim_beside_valid_array_remains_duplicate_candidate,
)
from tests.unit.web.sessions.test_continuation_audit_membership import (
    test_oversized_matching_row_survives_bounded_projection as test_oversized_matching_row_survives_bounded_projection,
)
from tests.unit.web.sessions.test_continuation_audit_membership import (
    test_private_request_and_failure_members_keep_both_rows as test_private_request_and_failure_members_keep_both_rows,
)
from tests.unit.web.sessions.test_continuation_audit_membership import (
    test_scalar_roots_and_nonobject_slots_do_not_claim_membership as test_scalar_roots_and_nonobject_slots_do_not_claim_membership,
)
from tests.unit.web.sessions.test_continuation_audit_membership import (
    test_target_after_unrelated_and_scalar_slots_remains_member as test_target_after_unrelated_and_scalar_slots_remains_member,
)

from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    with postgres_test_target(driver="psycopg", mem_limit="256m", nano_cpus=500_000_000) as target:
        database = f"audit_membership_{uuid4().hex}"
        admin = create_session_engine(target).execution_options(isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
        url = make_url(target).set(database=database)
        engine = create_session_engine(url.render_as_string(hide_password=False))
        try:
            initialize_session_schema(engine)
            yield engine
        finally:
            engine.dispose()
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            admin.dispose()
