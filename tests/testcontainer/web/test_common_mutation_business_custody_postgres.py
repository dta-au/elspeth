"""The same real business/custody proofs use isolated PostgreSQL databases."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_postgres_target as operation_postgres_target,
)
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_store as operation_store,
)
from tests.unit.web.composer.test_common_mutation_business_custody import (
    test_business_callback_requires_initial_and_current_auto as test_business_callback_requires_initial_and_current_auto,
)
from tests.unit.web.composer.test_common_mutation_business_custody import (
    test_business_guard_refuses_wrong_or_corrupt_authority_without_callback as test_business_guard_refuses_wrong_or_corrupt_authority_without_callback,
)
from tests.unit.web.composer.test_common_mutation_business_custody import (
    test_each_blob_business_transition_reads_current_mode_in_its_transaction as test_each_blob_business_transition_reads_current_mode_in_its_transaction,
)
from tests.unit.web.composer.test_common_mutation_business_custody import (
    test_existing_durable_stop_is_checked_before_mutation_mode as test_existing_durable_stop_is_checked_before_mutation_mode,
)
from tests.unit.web.composer.test_common_mutation_business_custody import (
    test_guarded_blob_commit_ack_loss_preserves_actual_committed_result as test_guarded_blob_commit_ack_loss_preserves_actual_committed_result,
)
from tests.unit.web.composer.test_common_mutation_business_custody import (
    test_preferences_acquire_custody_before_session_sql as test_preferences_acquire_custody_before_session_sql,
)
from tests.unit.web.composer.test_common_mutation_business_custody import (
    test_prepared_blob_helper_forwards_optional_business_ceiling as test_prepared_blob_helper_forwards_optional_business_ceiling,
)
from tests.unit.web.composer.test_common_mutation_business_custody import (
    test_real_blob_business_drivers_preserve_rows_and_bytes_on_refusal as test_real_blob_business_drivers_preserve_rows_and_bytes_on_refusal,
)
from tests.unit.web.composer.test_common_mutation_business_custody import (
    test_separate_process_blob_custody_excludes_preference_downgrade as test_separate_process_blob_custody_excludes_preference_downgrade,
)

pytestmark = pytest.mark.testcontainer
