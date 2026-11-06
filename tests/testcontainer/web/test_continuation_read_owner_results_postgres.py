"""The real row-equality prerequisite on PostgreSQL; no custody/recovery claim."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target as operation_postgres_target
from tests.testcontainer.web.test_composer_operations_postgres import operation_store as operation_store
from tests.unit.web.sessions.test_continuation_read_owner_results import (
    test_current_blob_version_check_reads_real_rows_and_preserves_transaction as test_current_blob_version_check_reads_real_rows_and_preserves_transaction,
)

pytestmark = pytest.mark.testcontainer
