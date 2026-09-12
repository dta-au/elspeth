"""Auth history must be explicitly covered by signed run exports."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from elspeth.core.landscape.auth_audit_repository import AuthAuditRepository
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.exporter import LandscapeExporter
from elspeth.core.landscape.schema import auth_events_table
from tests.unit.core.landscape.test_audit_export_read_model import COMPLETED_AT, _insert_run


def _event(db: LandscapeDB, event_id: str, *, after: bool = False) -> None:
    _, values = AuthAuditRepository._auth_event_values(
        event_type="role_granted",
        outcome="success",
        provider="vanguard",
        user_id="subject",
        username="Person",
        failure_category=None,
        request_id=None,
        client_host=None,
        user_agent=None,
        metadata={"role": "admin"},
        identity_id="target-identity",
    )
    values["event_id"] = event_id
    values["occurred_at"] = COMPLETED_AT + timedelta(seconds=1 if after else -1)
    with db.engine.begin() as conn:
        conn.execute(auth_events_table.insert().values(values))


def test_default_export_declares_auth_history_omitted() -> None:
    with LandscapeDB.in_memory() as db:
        _insert_run(db, run_id="run", status="completed", completed_at=COMPLETED_AT)
        _event(db, "event")
        records = list(LandscapeExporter(db).export_run("run", sign=False))
        coverage = [row for row in records if row["record_type"] == "auth_event_coverage"]
        assert len(coverage) == 1
        assert coverage[0]["policy"] == "omitted"
        assert coverage[0]["selected_count"] is None


def test_signed_deployment_export_includes_stored_history_and_empty_coverage() -> None:
    with LandscapeDB.in_memory() as db:
        _insert_run(db, run_id="run", status="completed", completed_at=COMPLETED_AT)
        exporter = LandscapeExporter(
            db, signing_key=b"test-auth-export-key", signer_key_id="test", auth_events="deployment_snapshot", row_batch_size=1
        )
        empty = list(exporter.export_run("run", sign=True))
        assert next(row for row in empty if row["record_type"] == "auth_event_coverage")["selected_count"] == 0
        for name in ("c", "a", "b"):
            _event(db, name)
        _event(db, "future", after=True)
        with db.engine.connect() as conn:
            assert len(conn.execute(select(auth_events_table)).all()) == 4
        records = list(exporter.export_run("run", sign=True))
        events = [row for row in records if row["record_type"] == "auth_event"]
        assert [row["event_id"] for row in events] == ["a", "b", "c"]
        assert events[0]["metadata"] == {"role": "admin"}
        assert events[0]["identity_id"] == "target-identity"
        assert "organisation_id" not in events[0]
        coverage = next(row for row in records if row["record_type"] == "auth_event_coverage")
        assert coverage["selected_count"] == 3
        assert all("signature" in row for row in records)


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("export_format", ["json", "csv"])
def test_auth_history_pure_and_spooled_derivation_are_identical(signed: bool, export_format: str) -> None:
    with LandscapeDB.in_memory() as db:
        _insert_run(db, run_id="run", status="completed", completed_at=COMPLETED_AT)
        _event(db, "first")
        _event(db, "second")
        exporter = LandscapeExporter(
            db,
            signing_key=b"test-auth-export-key" if signed else None,
            signer_key_id="test" if signed else None,
            auth_events="deployment_snapshot",
            export_format=export_format,
            per_chunk_record_limit=1,
        )
        streamed = list(exporter.export_run("run", sign=signed))
        derived = exporter.derive_run_bundle("run", sign=signed)
        assert streamed == [*derived.record_objects, derived.final_manifest]
        assert len(derived.chunks) == len(derived.record_objects)
