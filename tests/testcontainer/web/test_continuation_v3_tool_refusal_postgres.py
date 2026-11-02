"""Historical V3 tool refusal custody and capacity against PostgreSQL."""

from __future__ import annotations

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_postgres_target as operation_postgres_target,
)
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_store as operation_store,
)
from tests.unit.web.sessions.test_continuation_v3_tool_historical_refusal import (
    test_cold_tool_refusal_rejects_coherent_original_accumulator_substitution as test_cold_tool_refusal_rejects_coherent_original_accumulator_substitution,
)
from tests.unit.web.sessions.test_continuation_v3_tool_historical_refusal import (
    test_historical_v3_original_paid_source_retains_tool_refusal_without_new_payment as test_historical_v3_original_paid_source_retains_tool_refusal_without_new_payment,
)
from tests.unit.web.sessions.test_continuation_v3_tool_historical_refusal import (
    test_historical_v3_refusal_admits_full_tail_before_dml as test_historical_v3_refusal_admits_full_tail_before_dml,
)

pytestmark = pytest.mark.testcontainer
