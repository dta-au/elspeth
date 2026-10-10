"""Real PostgreSQL job/quota admission, including independent process engines."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_postgres_target as operation_postgres_target,
)
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_store as operation_store,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_cancelled_awaiter_leaves_worker_owned_atomic_pair as test_cancelled_awaiter_leaves_worker_owned_atomic_pair,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_commit_ack_loss_preserves_job_charge_pair_and_exact_retry as test_commit_ack_loss_preserves_job_charge_pair_and_exact_retry,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_cross_session_budget_denial_rolls_back_losing_job as test_cross_session_budget_denial_rolls_back_losing_job,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_distinct_subjects_have_independent_composer_budgets as test_distinct_subjects_have_independent_composer_budgets,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_duplicate_leaves_second_slot_for_distinct_session as test_duplicate_leaves_second_slot_for_distinct_session,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_full_bucket_replay_and_changed_binding_never_charge as test_full_bucket_replay_and_changed_binding_never_charge,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_independent_processes_share_composer_sql_budget as test_independent_processes_share_composer_sql_budget,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_nonquota_refusals_preserve_budget as test_nonquota_refusals_preserve_budget,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_overlapping_same_id_commits_one_job_and_one_charge as test_overlapping_same_id_commits_one_job_and_one_charge,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_quota_connection_must_be_exact_selected_engine as test_quota_connection_must_be_exact_selected_engine,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_quota_sql_fault_rolls_back_both_effects_and_retry as test_quota_sql_fault_rolls_back_both_effects_and_retry,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_received_connection_rejects_inactive_closed_and_foreign_transactions as test_received_connection_rejects_inactive_closed_and_foreign_transactions,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_received_connection_writer_cannot_open_or_commit_another_transaction as test_received_connection_writer_cannot_open_or_commit_another_transaction,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_reopened_engine_preserves_budget_and_replays_existing_action as test_reopened_engine_preserves_budget_and_replays_existing_action,
)
from tests.unit.web.coordination.test_composer_atomic_quota import (
    test_standalone_composer_consumer_and_operation_share_one_bucket as test_standalone_composer_consumer_and_operation_share_one_bucket,
)

pytestmark = pytest.mark.testcontainer
