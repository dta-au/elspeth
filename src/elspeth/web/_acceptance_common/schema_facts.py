"""Shared new-store admission and release/schema facts for deployment receipts.

Scenario B facts describe historical rollback-refusal diagnostics only. They
do not authorize an upgrade during the temporary compatibility window.
"""

from __future__ import annotations

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

from .errors import AcceptanceCheckError

_CANDIDATE_PACKAGE_VERSION = "0.8.6"

_ROLLBACK_PACKAGE_VERSION = "0.7.1"

_ROLLBACK_BASELINE_SESSION_EPOCH = 35

_ROLLBACK_BASELINE_LANDSCAPE_EPOCH = 29

# Scenario B is retained only for historical rollback-refusal diagnostics.
# It is not a supported deployment transition during the temporary
# no-backwards-compatibility window; no expiry has been specified.
_SCENARIO_B_STRUCTURAL_CHANGES = (
    f"unsupported_session_epoch_{_ROLLBACK_BASELINE_SESSION_EPOCH}_to_{SESSION_SCHEMA_EPOCH}"
    f"_landscape_epoch_{_ROLLBACK_BASELINE_LANDSCAPE_EPOCH}_to_{SQLITE_SCHEMA_EPOCH}"
    "_preserve_existing_stores"
)


def _require_supported_deployment_scenario(scenario_id: str) -> None:
    """Require explicit Scenario A before application deployment or writes.

    Cleanup and historical diagnostics do not call this admission guard.
    Infrastructure-only scopes remain validated by their owning entry point.
    """
    if scenario_id != "A":
        raise AcceptanceCheckError("compatibility_transition_unsupported")


def _schema_facts_equal(value: object, expected: object) -> bool:
    """Compare the closed derived JSON facts without bool/int/float coercion."""

    if type(value) is not type(expected):
        return False
    if type(expected) is dict:
        assert type(value) is dict
        return value.keys() == expected.keys() and all(_schema_facts_equal(value[key], fact) for key, fact in expected.items())
    return value == expected


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
        "semantics_only_changes": ("not_applicable" if scenario_id == "B" else "none"),
        "archive_export_decision": ("preserve_existing_store" if scenario_id == "B" else "not_applicable"),
        "destructive_reset_required": False,
        "store_policy": "new_store_only",
        "existing_store_action": "preserve_and_refuse",
        "in_place_upgrade_supported": False,
    }
