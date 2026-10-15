"""Graph-review fact transactions on the existing isolated PostgreSQL target.

The shared controls exercise storage, qualification and writer serialization.
Their model receipts are synthetic; actual finite SDK dispatch is covered by
the separate Composer integration controls.
"""

from __future__ import annotations

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_postgres_target as operation_postgres_target,
)
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_store as operation_store,
)
from tests.unit.web.sessions.test_graph_review_facts import (
    no_provider as no_provider,
)
from tests.unit.web.sessions.test_graph_review_facts import (
    test_authored_writer_and_review_terminal_serialize_without_stale_fact as test_authored_writer_and_review_terminal_serialize_without_stale_fact,
)
from tests.unit.web.sessions.test_graph_review_facts import (
    test_both_read_qualification_owners_reject_unadmitted_lower_fact as test_both_read_qualification_owners_reject_unadmitted_lower_fact,
)
from tests.unit.web.sessions.test_graph_review_facts import (
    test_only_exact_admitted_clean_model_receipt_can_clear_transient_blocker as test_only_exact_admitted_clean_model_receipt_can_clear_transient_blocker,
)
from tests.unit.web.sessions.test_graph_review_facts import (
    test_selected_recovery_retains_only_negative_effective_review as test_selected_recovery_retains_only_negative_effective_review,
)
from tests.unit.web.sessions.test_graph_review_facts import (
    test_terminal_fact_atomicity_and_frozen_retry as test_terminal_fact_atomicity_and_frozen_retry,
)
from tests.unit.web.sessions.test_graph_review_facts import (
    test_terminal_fact_projects_current_explicit_and_recovery_without_checkpoint as test_terminal_fact_projects_current_explicit_and_recovery_without_checkpoint,
)

pytestmark = pytest.mark.testcontainer
