"""The same original evidence/current-owner read on real serial PostgreSQL."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target as operation_postgres_target
from tests.testcontainer.web.test_composer_operations_postgres import operation_store as operation_store
from tests.unit.web.sessions.test_continuation_runtime_evidence_reuse import (
    test_runtime_source_retains_its_actual_validated_evidence_and_rejects_another_running_owner as test_runtime_source_retains_its_actual_validated_evidence_and_rejects_another_running_owner,
)

pytestmark = pytest.mark.testcontainer
