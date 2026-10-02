"""Approval transitions persist to the signed Landscape auth-event source."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.schema import auth_events_table
from elspeth.web.auth.audit import AuthAuditRecorder
from elspeth.web.coordination.approval_authority import ApprovalBinding, ApprovalRecord, ApprovalSupersession


def _record(*, decision: str | None, note: str | None) -> ApprovalRecord:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    return ApprovalRecord(
        approval_id="approval-1",
        session_id="session-1",
        state_id="state-1",
        binding=ApprovalBinding("config", "canonical", "manifest", "catalog", "generation", "policy"),
        requested_by_identity_id="author",
        approver_identity_id="addressed",
        requested_at=now,
        decided_at=now if decision is not None else None,
        decision=decision,
        request_note="please review",
        decision_seen_at=None,
        decided_by_identity_id="cover" if decision is not None else None,
        decision_note=note,
        revoked_by_identity_id=None,
        revocation_actor_kind=None,
        revocation_event_id=None,
    )


def test_requested_and_decided_events_persist_request_and_cover_actor(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'approval-audit.db'}"
    recorder = AuthAuditRecorder(landscape_url=url, landscape_passphrase=None, create_tables=True)
    recorder.record_approval_requested(None, provider="local", approval=_record(decision=None, note=None))
    recorder.record_approval_decided(
        None, provider="local", approval=_record(decision="approved", note="reviewed"), actor_identity_id="cover"
    )
    with LandscapeDB.from_url(url) as db, db.read_only_connection() as conn:
        rows = conn.execute(select(auth_events_table).order_by(auth_events_table.c.occurred_at, auth_events_table.c.event_id)).all()
    assert {row.event_type for row in rows} == {"approval_requested", "approval_decided"}
    events = {row.event_type: row for row in rows}
    assert events["approval_requested"].identity_id == "author"
    assert events["approval_decided"].identity_id == "cover"
    requested = json.loads(events["approval_requested"].metadata_json)
    decided = json.loads(events["approval_decided"].metadata_json)
    assert requested["binding"]["binding_generation_fingerprint"] == "generation"
    assert decided["decision"] == "approved" and decided["decided_by_identity_id"] == "cover"


def _retired(approval_id: str, trigger_approval_id: str) -> ApprovalSupersession:
    approval = replace(_record(decision="approved", note=None), approval_id=approval_id, decision="superseded")
    return ApprovalSupersession(
        approval=approval,
        provider="local",
        actor_identity_id="cover",
        cause="later_rejection",
        trigger_approval_id=trigger_approval_id,
    )


def test_rejection_and_multiple_supersessions_commit_as_one_audit_batch(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'approval-batch.db'}"
    recorder = AuthAuditRecorder(landscape_url=url, landscape_passphrase=None, create_tables=True, compartment_id="team-red")
    rejected = replace(_record(decision="rejected", note="needs changes"), approval_id="rejected-3")
    recorder.record_approval_rejection_with_supersessions(
        None,
        provider="local",
        approval=rejected,
        actor_identity_id="cover",
        supersessions=(_retired("approved-1", rejected.approval_id), _retired("approved-2", rejected.approval_id)),
    )
    with LandscapeDB.from_url(url) as db, db.read_only_connection() as conn:
        rows = conn.execute(select(auth_events_table)).all()
    assert len(rows) == 3
    metadata = [json.loads(row.metadata_json) for row in rows]
    assert {item["approval_id"]: item["decision"] for item in metadata} == {
        "approved-1": "superseded",
        "approved-2": "superseded",
        "rejected-3": "rejected",
    }
    assert all(item["compartment_id"] == "team-red" for item in metadata)
    assert all(item["trigger_approval_id"] == "rejected-3" for item in metadata if item["decision"] == "superseded")


def test_rejection_audit_batch_rolls_back_when_later_supersession_insert_fails(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'approval-batch-failure.db'}"
    recorder = AuthAuditRecorder(landscape_url=url, landscape_passphrase=None, create_tables=True)
    with recorder.start().write_connection() as conn:
        conn.exec_driver_sql(
            "CREATE TRIGGER reject_supersession BEFORE INSERT ON auth_events "
            "WHEN json_extract(NEW.metadata_json, '$.decision') = 'superseded' "
            "BEGIN SELECT RAISE(ABORT, 'supersession audit unavailable'); END"
        )
    rejected = replace(_record(decision="rejected", note="needs changes"), approval_id="rejected-2")
    with pytest.raises(LandscapeRecordError):
        recorder.record_approval_rejection_with_supersessions(
            None,
            provider="local",
            approval=rejected,
            actor_identity_id="cover",
            supersessions=(_retired("approved-1", rejected.approval_id),),
        )
    with LandscapeDB.from_url(url) as db, db.read_only_connection() as conn:
        assert conn.execute(select(auth_events_table)).all() == []
