"""The same actual application/current-history proofs against PostgreSQL."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target as operation_postgres_target
from tests.testcontainer.web.test_composer_operations_postgres import operation_store as operation_store
from tests.unit.web.sessions.test_continuation_attachment_history_closure import (
    test_current_attachment_root_retains_complete_application_and_cache_history as test_current_attachment_root_retains_complete_application_and_cache_history,
)

pytestmark = pytest.mark.testcontainer
