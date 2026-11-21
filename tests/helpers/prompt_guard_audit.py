"""The prompt guard audit check's shape pins, grouped in one place.

The ``units_scanned``/``units_reused`` fields (schema version 2) record how
many distinct units a check sent to the dedicated service and how many it
admitted from an earlier allow in the same session under the same policy.
If their form changes, change it here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

PROMPT_GUARD_AUDIT_SCHEMA_VERSION = 2

PROMPT_GUARD_AUDIT_CHECK_KEYS = frozenset(
    {
        "schema_version",
        "audit_id",
        "operation_id",
        "operation_epoch",
        "role",
        "adapter",
        "mode",
        "policy_fingerprint",
        "request_hash",
        "disposition",
        "reason",
        "started_at",
        "duration_ms",
        "units_scanned",
        "units_reused",
    }
)


def assert_prompt_guard_check_shape(check: Mapping[str, Any]) -> None:
    """Exact key set, version and reuse-count types of one persisted check."""
    assert set(check) == PROMPT_GUARD_AUDIT_CHECK_KEYS
    assert check["schema_version"] == PROMPT_GUARD_AUDIT_SCHEMA_VERSION
    assert type(check["units_scanned"]) is int and check["units_scanned"] >= 0
    assert type(check["units_reused"]) is int and check["units_reused"] >= 0


def unscanned_check_counts() -> dict[str, int]:
    """Reuse counts of a check that sent nothing (unconfigured or unsupported)."""
    return {"units_scanned": 0, "units_reused": 0}


def assert_prompt_guard_wire_payload(payload: Mapping[str, Any], *, user_prompt: str) -> None:
    """A physical call carries either the user prompt alone or documents alone."""
    assert set(payload) == {"userPrompt", "documents"}
    if payload["userPrompt"]:
        assert payload["userPrompt"] == user_prompt
        assert payload["documents"] == []
    else:
        assert payload["documents"]


def assert_prompt_guard_wire_payloads(payloads: Sequence[Mapping[str, Any]], *, user_prompt: str) -> None:
    """Every call has the split shape, and the user prompt was sent at least once.

    A later check may reuse an earlier allow of the same prompt, so the number
    of calls carrying it is not fixed here.
    """
    for payload in payloads:
        assert_prompt_guard_wire_payload(payload, user_prompt=user_prompt)
    if payloads:
        assert any(payload["userPrompt"] == user_prompt for payload in payloads)
