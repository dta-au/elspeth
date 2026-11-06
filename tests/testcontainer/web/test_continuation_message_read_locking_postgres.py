"""Same actual bridge/current-final guard on serial PostgreSQL."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target as operation_postgres_target
from tests.testcontainer.web.test_composer_operations_postgres import operation_store as operation_store
from tests.unit.web.sessions.test_continuation_message_read_locking import (
    test_current_bridge_reuses_actual_read_and_keeps_fresh_final_refusal as test_current_bridge_reuses_actual_read_and_keeps_fresh_final_refusal,
)

pytestmark = pytest.mark.testcontainer
