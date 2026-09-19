"""Durable quota administration events in the Landscape auth trail."""

from __future__ import annotations

from typing import Any

from elspeth.web.coordination.quota_authority import QuotaPolicyRow
from elspeth.web.coordination.quota_policy_authority import QuotaPolicyChange
from tests.unit.web.auth.test_audit import _durable_recorder, _durable_rows, _metadata, _request


def _change(*, action: str, dimension: str | None, policy: QuotaPolicyRow | None) -> QuotaPolicyChange:
    return QuotaPolicyChange(
        identity_id="alice",
        actor_identity_id="root",
        action=action,
        dimension=dimension,
        policy=policy,
        previous=QuotaPolicyRow(policy_id="old", tokens_per_day=500, storage_bytes=2000),
        container_policy=QuotaPolicyRow(policy_id="container", tokens_per_day=9000, storage_bytes=90000),
        tokens_used_today=75,
        storage_bytes_used=600,
        on_behalf_of="person@example.org",
        console_request_id="console-42",
    )


def test_admin_quota_set_records_dimension_and_measured_usage(tmp_path: Any) -> None:
    recorder, url = _durable_recorder(tmp_path)
    recorder.record_quota_set(
        _request(),
        provider="local",
        change=_change(
            action="set",
            dimension="storage",
            policy=QuotaPolicyRow(policy_id="new", tokens_per_day=500, storage_bytes=3000),
        ),
    )
    (row,) = _durable_rows(url)
    assert (row.event_type, row.outcome, row.identity_id, row.request_id) == ("quota_set", "success", "alice", "request-id")
    metadata = _metadata(row)
    assert {key: metadata[key] for key in ("action", "dimension", "cap", "previous_cap", "ceiling", "usage")} == {
        "action": "set",
        "dimension": "storage",
        "cap": 3000,
        "previous_cap": 2000,
        "ceiling": 90000,
        "usage": 600,
    }
    assert (metadata["policy_id"], metadata["revoked_policy_id"]) == ("new", "old")
    assert (metadata["on_behalf_of"], metadata["console_request_id"]) == ("person@example.org", "console-42")


def test_revoke_records_both_dimensions(tmp_path: Any) -> None:
    recorder, url = _durable_recorder(tmp_path)
    recorder.record_quota_set(None, provider="local", change=_change(action="revoke", dimension=None, policy=None))
    rows = _durable_rows(url)
    assert len(rows) == 2 and {row.event_type for row in rows} == {"quota_set"}
    by_dimension = {_metadata(row)["dimension"]: _metadata(row) for row in rows}
    assert {dimension: (data["cap"], data["previous_cap"], data["usage"]) for dimension, data in by_dimension.items()} == {
        "tokens": (None, 500, 75),
        "storage": (None, 2000, 600),
    }
    assert all(data["action"] == "revoke" and data["revoked_policy_id"] == "old" for data in by_dimension.values())
