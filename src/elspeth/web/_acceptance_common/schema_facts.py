"""The one derivation of the release/schema facts a compatibility record attests.

Moved verbatim from ``_aws_ecs_acceptance/receipt_contracts.py``. Both
providers' compatibility receipts, the ECS runbook's Scenario B record
(``tests/unit/docs/test_release_version_surfaces.py``) and the shared gate
predicate read these facts from here and nowhere else, so "session 35→N,
landscape 29→M, same ``structural_changes`` label" has exactly one source.
"""

from __future__ import annotations

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

_CANDIDATE_PACKAGE_VERSION = "0.8.0"

_ROLLBACK_PACKAGE_VERSION = "0.7.1"

_ROLLBACK_BASELINE_SESSION_EPOCH = 35

_ROLLBACK_BASELINE_LANDSCAPE_EPOCH = 29

# Derived from the live epoch constants: a schema bump must rotate the label a
# compatibility receipt attests, otherwise a stale receipt keeps validating
# against a transition the candidate no longer performs.
_SCENARIO_B_STRUCTURAL_CHANGES = (
    f"session_epoch_{_ROLLBACK_BASELINE_SESSION_EPOCH}_to_{SESSION_SCHEMA_EPOCH}"
    f"_landscape_epoch_{_ROLLBACK_BASELINE_LANDSCAPE_EPOCH}_to_{SQLITE_SCHEMA_EPOCH}"
    "_blob_cleanup_guided_decline_row_union_barrier_and_coordination_schema"
)


def _expected_schema_facts(scenario_id: str) -> dict[str, object]:
    """Truthful release/schema facts the compatibility authority must attest.

    Candidate facts track the live schema-epoch constants so the validators
    always demand the current build's truth; previous facts are pinned to the
    Scenario B rollback baseline.
    """
    return {
        "candidate": {
            "session_epoch": SESSION_SCHEMA_EPOCH,
            "landscape_epoch": SQLITE_SCHEMA_EPOCH,
            "run_web_plugin_policy_present": True,
        },
        "previous": (
            {
                "session_epoch": _ROLLBACK_BASELINE_SESSION_EPOCH,
                "landscape_epoch": _ROLLBACK_BASELINE_LANDSCAPE_EPOCH,
                "run_web_plugin_policy_present": True,
            }
            if scenario_id == "B"
            else None
        ),
        "structural_changes": (_SCENARIO_B_STRUCTURAL_CHANGES if scenario_id == "B" else "initial_create"),
        "semantics_only_changes": ("guided_coalesce_timeout_seconds_and_node_options_summary_required" if scenario_id == "B" else "none"),
        "archive_export_decision": ("required_before_forward_migration" if scenario_id == "B" else "not_applicable"),
        "destructive_reset_required": False,
    }
