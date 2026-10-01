"""Durable remote effects survive response loss and genuine worker death."""

from __future__ import annotations

import os
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch

import httpx
import pytest
import yaml
from click.testing import Result
from sqlalchemy import select
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts.call_data import CallPayload, HTTPCallRequest
from elspeth.contracts.enums import CallStatus, CallType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.execution.calls import CallAuditRepository
from elspeth.core.landscape.schema import runs_table, sink_effect_members_table, token_outcomes_table
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator, SinkEffectExecutionSeam
from elspeth.plugins.sources.power_automate import PowerAutomateSource
from tests.e2e.recovery.harness import spawn_database_process_with_pause
from tests.e2e.recovery.test_sink_effect_process_death_matrix import (
    _install_short_run_liveness,
    _install_short_sink_lease,
    _wait_until_run_is_resumable,
)
from tests.fixtures.power_automate import (
    PROTOCOL,
    PUBLIC_IP,
    READ_URL,
    WRITE_URL,
    DurablePowerAutomateFlow,
    independent_payload_hash,
    pipeline_settings,
)
from tests.integration.plugins.test_power_automate_pipeline import invoke


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "power-automate-integration-key")
    monkeypatch.setenv("POWER_AUTOMATE_READ_TRIGGER_URL", READ_URL)
    monkeypatch.setenv("POWER_AUTOMATE_WRITE_TRIGGER_URL", WRITE_URL)
    original_init = SinkEffectCoordinator.__init__

    def short_lease(self: SinkEffectCoordinator, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("lease_ttl", timedelta(seconds=0.2))
        kwargs.setdefault("poll_interval", 0.02)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(SinkEffectCoordinator, "__init__", short_lease)


def _run_id(tmp_path: Path) -> str:
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        return connection.execute(select(runs_table.c.run_id)).scalar_one()


def _resume(tmp_path: Path, run_id: str) -> Result:
    return CliRunner().invoke(
        app, ["--no-dotenv", "resume", run_id, "--settings", str(tmp_path / "settings.yaml"), "--execute", "--format", "json"]
    )


@pytest.mark.parametrize("reject", [False, True])
def test_remote_response_loss_reconciles_original_delivery_and_sticky_rejection(tmp_path: Path, reject: bool) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "ok"}]])
    if reject:
        flow.reject_record_ids.add("A")
    flow.drop_next_write_response = True
    with flow.transport():
        failed = invoke(tmp_path, pipeline_settings(tmp_path))
    assert failed.exit_code != 0, failed.output
    first_write = flow.requests("write")[0]
    assert len(flow.actions()) == (0 if reject else 1)
    with (
        flow.transport(),
        patch.object(PowerAutomateSource, "load", side_effect=AssertionError("resume reloaded source")),
        patch.object(PowerAutomateSource, "on_start", side_effect=AssertionError("resume started source")),
    ):
        resumed = _resume(tmp_path, _run_id(tmp_path))
    assert resumed.exit_code == (2 if reject else 0), resumed.output
    assert len(flow.requests("read")) == 1
    assert {request["delivery_id"] for request in flow.requests("status")} == {first_write["delivery_id"]}
    assert len(flow.requests("write")) == (2 if reject else 1)
    assert len(flow.actions()) == (0 if reject else 1)
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        members = connection.execute(select(sink_effect_members_table)).mappings().all()
        outcomes = connection.execute(select(token_outcomes_table)).mappings().all()
    assert len(members) == len(outcomes) == 1
    assert members[0]["member_effect_id"] == first_write["delivery_id"]
    assert members[0]["member_state"] == "finalized"
    assert members[0]["prepared_disposition"] == ("diverted" if reject else "accepted")
    assert outcomes[0]["outcome"] == ("failure" if reject else "success")


@pytest.mark.parametrize("unknown_reason", ["pending", "expired", "conflict"])
def test_unknown_remote_evidence_never_resubmits(tmp_path: Path, unknown_reason: str) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "ok"}]])
    flow.drop_next_write_response = True
    with flow.transport():
        failed = invoke(tmp_path, pipeline_settings(tmp_path))
    assert failed.exit_code != 0
    (write,) = flow.requests("write")
    flow.set_state(str(write["delivery_id"]), unknown_reason)
    with flow.transport():
        resumed = _resume(tmp_path, _run_id(tmp_path))
    assert resumed.exit_code != 0, resumed.output
    assert len(flow.requests("status")) == 2
    assert flow.requests("write") == [write]
    assert len(flow.actions()) == 1


def test_missing_ledger_with_late_pending_write_blocks_initial_resubmit(tmp_path: Path) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "ok"}]])
    flow.absent_state = "unknown"
    release = Event()
    pending: Future[httpx.Response] | None = None

    def delayed_old_invocation(body: dict[str, object]) -> httpx.Response:
        assert release.wait(timeout=10)
        late_write = {**body, "operation": "write", "data": {"record_id": "A", "result": "ok"}}
        return flow.handle(httpx.Request("POST", WRITE_URL, json=late_write))

    with ThreadPoolExecutor(max_workers=1) as executor:

        def queue_old_invocation(body: dict[str, object]) -> None:
            nonlocal pending
            pending = executor.submit(delayed_old_invocation, body)

        flow.after_status = queue_old_invocation
        try:
            with flow.transport():
                failed = invoke(tmp_path, pipeline_settings(tmp_path))
            assert failed.exit_code != 0
            assert len(flow.requests("status")) == 1
            assert flow.requests("write") == []
            assert flow.actions() == []
        finally:
            release.set()
        assert pending is not None
        assert pending.result(timeout=5).json()["state"] == "applied"
    assert len(flow.actions()) == len(flow.requests("write")) == 1


def test_partial_group_finalized_member_is_skipped_and_equal_rows_have_distinct_ids(tmp_path: Path) -> None:
    data = {"record_id": "A", "result": "ok"}
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[data], [data]])
    flow.drop_write_response_at = 2

    def require_complete_snapshot() -> None:
        assert len(flow.requests("read")) == 2

    flow.after_durable_write = require_complete_snapshot
    with flow.transport():
        failed = invoke(tmp_path, pipeline_settings(tmp_path))
    assert failed.exit_code != 0, failed.output
    before = flow.actions()
    assert len(before) == 2
    assert before[0]["delivery_id"] != before[1]["delivery_id"]
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        members = connection.execute(select(sink_effect_members_table).order_by(sink_effect_members_table.c.ordinal)).mappings().all()
    assert len(members) == 2
    assert members[0]["effect_id"] == members[1]["effect_id"]
    assert members[0]["member_state"] == "finalized"
    flow.drop_write_response_at = None
    with flow.transport(), patch.object(PowerAutomateSource, "load", side_effect=AssertionError("partial resume read source")):
        resumed = _resume(tmp_path, _run_id(tmp_path))
    assert resumed.exit_code == 0, resumed.output
    assert flow.actions() == before
    assert len(flow.requests("write")) == 2
    assert len(flow.requests("status")) == 3
    assert flow.requests("status")[-1]["delivery_id"] == before[1]["delivery_id"]


def test_incomplete_streaming_source_refuses_resume_without_reloading(tmp_path: Path) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "ok"}], []])
    flow.fail_read_at = 2
    with flow.transport():
        failed = invoke(tmp_path, pipeline_settings(tmp_path, snapshot_for_resume=False))
    assert failed.exit_code != 0, failed.output
    before = flow.requests()
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("incomplete resume resolved source DNS")) as dns,
        patch.object(PowerAutomateSource, "load", side_effect=AssertionError("incomplete resume reloaded source")) as load,
    ):
        refused = _resume(tmp_path, _run_id(tmp_path))
    assert refused.exit_code != 0, refused.output
    assert "source" in refused.output.lower()
    dns.assert_not_called()
    load.assert_not_called()
    assert flow.requests() == before


def test_post_send_audit_failure_reconciles_without_republishing(tmp_path: Path) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "ok"}]])
    original = CallAuditRepository.record_operation_call
    refused = False

    def fail_write_audit(
        self: CallAuditRepository,
        operation_id: str,
        call_type: CallType,
        status: CallStatus,
        request_data: CallPayload,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal refused
        request = request_data
        if isinstance(request, HTTPCallRequest) and request.json is not None and request.json.get("operation") == "write" and not refused:
            refused = True
            raise AuditIntegrityError("controlled post-send audit failure")
        return original(self, operation_id, call_type, status, request_data, *args, **kwargs)

    with flow.transport(), patch.object(CallAuditRepository, "record_operation_call", fail_write_audit):
        failed = invoke(tmp_path, pipeline_settings(tmp_path))
    assert failed.exit_code != 0, failed.output
    assert refused
    assert len(flow.actions()) == len(flow.requests("write")) == 1
    with flow.transport():
        resumed = _resume(tmp_path, _run_id(tmp_path))
    assert resumed.exit_code == 0, resumed.output
    assert len(flow.actions()) == len(flow.requests("write")) == 1


def test_configured_rotation_refuses_old_resume_and_new_run_adopts_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "ok"}]])
    flow.drop_next_write_response = True
    with flow.transport():
        failed = invoke(tmp_path, pipeline_settings(tmp_path))
    assert failed.exit_code != 0, failed.output
    before = flow.requests()
    old_id = flow.actions()[0]["delivery_id"]
    monkeypatch.setenv("POWER_AUTOMATE_WRITE_TRIGGER_URL", WRITE_URL.replace("synthetic-write", "rotated-write"))
    with patch("socket.getaddrinfo", side_effect=AssertionError("rotated old run dispatched")) as dns:
        refused = _resume(tmp_path, _run_id(tmp_path))
    assert refused.exit_code != 0, refused.output
    dns.assert_not_called()
    assert flow.requests() == before
    with flow.transport() as router:
        router.post(f"https://{PUBLIC_IP}:443/write?sig=rotated-write").mock(side_effect=flow.handle)
        fresh = invoke(tmp_path, pipeline_settings(tmp_path))
    assert fresh.exit_code == 0, fresh.output
    assert len(flow.actions()) == 2
    assert flow.actions()[1]["delivery_id"] != old_id


@pytest.mark.parametrize("deduplicate,expected_actions", [(True, 1), (False, 8)])
def test_same_id_concurrent_write_controls_remote_dedup(tmp_path: Path, deduplicate: bool, expected_actions: int) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", deduplicate=deduplicate)
    data = {"record_id": "A", "result": "ok"}
    body = {
        "protocol": PROTOCOL,
        "operation": "write",
        "delivery_id": "a" * 64,
        "payload_sha256": independent_payload_hash(data),
        "data": data,
    }
    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(lambda _: flow.handle(httpx.Request("POST", WRITE_URL, json=body)), range(8)))
    assert len(flow.actions()) == expected_actions
    receipts = [response.json() for response in responses]
    assert all(receipt == receipts[0] for receipt in receipts)
    changed = {**body, "payload_sha256": "b" * 64}
    assert flow.handle(httpx.Request("POST", WRITE_URL, json=changed)).json()["state"] == "unknown"
    assert len(flow.actions()) == expected_actions


def _worker_at_remote_commit(db: LandscapeDB, pause: object, root: str, seam_value: str) -> None:
    assert callable(pause)
    del db
    _install_short_run_liveness()
    _install_short_sink_lease()
    path = Path(root)
    flow = DurablePowerAutomateFlow(path / "target.db", pages=[[{"record_id": "A", "result": "ok"}]])
    original_fault = SinkEffectCoordinator._fault

    def pause_at_target(self: SinkEffectCoordinator, seam: SinkEffectExecutionSeam) -> None:
        if seam.value == seam_value:
            pause()
        original_fault(self, seam)

    if seam_value == "remote_commit":
        flow.after_durable_write = pause
    with flow.transport(), patch.object(SinkEffectCoordinator, "_fault", pause_at_target):
        result = invoke(path, pipeline_settings(path))
    assert result.exit_code == 0, result.output


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL oracle is POSIX-specific")
def test_killed_worker_restores_snapshot_and_original_member_identity(tmp_path: Path) -> None:
    exercise_power_automate_process_death(tmp_path, "remote_commit")


def exercise_power_automate_process_death(tmp_path: Path, seam_value: str) -> None:
    settings = pipeline_settings(tmp_path)
    (tmp_path / "settings.yaml").write_text(yaml.safe_dump(settings))
    database_url = f"sqlite:///{tmp_path / 'landscape.db'}"
    with LandscapeDB(database_url):
        pass
    with (
        patch.dict(
            os.environ,
            {
                "ELSPETH_FINGERPRINT_KEY": "power-automate-integration-key",
                "POWER_AUTOMATE_READ_TRIGGER_URL": READ_URL,
                "POWER_AUTOMATE_WRITE_TRIGGER_URL": WRITE_URL,
            },
        ),
        spawn_database_process_with_pause(
            database_url=database_url,
            seam="power-automate-" + seam_value,
            action=_worker_at_remote_commit,
            action_args=(str(tmp_path), seam_value),
        ) as child,
    ):
        child.wait_until_ready(timeout=20)
        child.kill()
        assert child.wait_for_exit(timeout=20).was_killed
    flow = DurablePowerAutomateFlow(tmp_path / "target.db")
    killed_actions = flow.actions()
    with LandscapeDB.from_url(database_url, create_tables=False) as db, db.engine.connect() as connection:
        original_delivery = connection.execute(select(sink_effect_members_table.c.member_effect_id)).scalar_one()
    if seam_value == SinkEffectExecutionSeam.BEFORE_EFFECT.value:
        assert killed_actions == []
    else:
        assert len(killed_actions) == 1
        assert killed_actions[0]["delivery_id"] == original_delivery
    run_id = _run_id(tmp_path)
    _wait_until_run_is_resumable(database_url, run_id)
    with (
        patch.dict(
            os.environ,
            {
                "ELSPETH_FINGERPRINT_KEY": "power-automate-integration-key",
                "POWER_AUTOMATE_READ_TRIGGER_URL": READ_URL,
                "POWER_AUTOMATE_WRITE_TRIGGER_URL": WRITE_URL,
            },
        ),
        flow.transport(),
        patch.object(PowerAutomateSource, "load", side_effect=AssertionError("restart read source")),
        patch.object(PowerAutomateSource, "on_start", side_effect=AssertionError("restart started source")),
    ):
        resumed = _resume(tmp_path, run_id)
    assert resumed.exit_code == 0, resumed.output
    (final_action,) = flow.actions()
    assert final_action["delivery_id"] == original_delivery
    if killed_actions:
        assert flow.actions() == killed_actions
    assert len(flow.requests("read")) == len(flow.requests("write")) == 1
    assert all(request["delivery_id"] == original_delivery for request in flow.requests("status"))
