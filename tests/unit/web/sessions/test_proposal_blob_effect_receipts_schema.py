"""Schema contract for durable ordinary blob-proposal effect receipts."""

from __future__ import annotations

from sqlalchemy import inspect

from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH, proposal_blob_effect_receipts_table


def test_proposal_blob_effect_receipt_schema_is_exact(engine) -> None:
    assert SESSION_SCHEMA_EPOCH == 44
    assert tuple(proposal_blob_effect_receipts_table.primary_key.columns.keys()) == ("proposal_id",)
    assert set(proposal_blob_effect_receipts_table.c.keys()) == {
        "proposal_id",
        "session_id",
        "tool_name",
        "blob_id",
        "arguments_hash",
        "result_blob_snapshot",
        "result_blob_snapshot_hash",
        "accepted_event_id",
        "created_at",
        "accepted_at",
    }
    assert proposal_blob_effect_receipts_table.c.accepted_event_id.unique is True
    assert proposal_blob_effect_receipts_table.c.accepted_event_id.nullable is True
    assert proposal_blob_effect_receipts_table.c.accepted_at.nullable is True

    reflected = inspect(engine)
    foreign_keys = reflected.get_foreign_keys("proposal_blob_effect_receipts")
    assert any(
        fk["constrained_columns"] == ["proposal_id", "session_id"]
        and fk["referred_table"] == "composition_proposals"
        and fk["referred_columns"] == ["id", "session_id"]
        for fk in foreign_keys
    )
    assert any(
        fk["constrained_columns"] == ["accepted_event_id"]
        and fk["referred_table"] == "proposal_events"
        and fk["referred_columns"] == ["id"]
        for fk in foreign_keys
    )

    checks = {constraint["name"] for constraint in reflected.get_check_constraints("proposal_blob_effect_receipts")}
    assert {
        "ck_proposal_blob_effect_receipts_acceptance_bundle",
        "ck_proposal_blob_effect_receipts_arguments_hash",
        "ck_proposal_blob_effect_receipts_blob_id_nonblank",
        "ck_proposal_blob_effect_receipts_result_hash",
        "ck_proposal_blob_effect_receipts_tool",
    } <= checks
