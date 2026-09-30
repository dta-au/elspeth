"""Power Automate modes through CLI settings, real graph and durable audit."""

from __future__ import annotations

import base64
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import Result
from sqlalchemy import select, update
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import (
    calls_table,
    nodes_table,
    rows_table,
    runs_table,
    sink_effect_members_table,
    token_outcomes_table,
    validation_errors_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.plugins.sinks.power_automate import PowerAutomateSink
from elspeth.plugins.sources.power_automate import PowerAutomateSource
from elspeth.plugins.transforms.passthrough import PassThrough
from tests.fixtures.power_automate import READ_URL, WRITE_URL, DurablePowerAutomateFlow, pipeline_settings


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "power-automate-integration-key")
    monkeypatch.setenv("POWER_AUTOMATE_READ_TRIGGER_URL", READ_URL)
    monkeypatch.setenv("POWER_AUTOMATE_WRITE_TRIGGER_URL", WRITE_URL)


def invoke(tmp_path: Path, settings: dict[str, object]) -> Result:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    return CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])


def test_real_pipeline_accounts_candidates_and_audits_exact_http_evidence(tmp_path: Path, credentials: None) -> None:
    flow = DurablePowerAutomateFlow(
        tmp_path / "target.db",
        pages=[
            [{"record_id": "A", "result": "ok"}, None, ["bad"]],
            [{"record_id": "B", "result": "ok"}, {"record_id": 7, "result": "bad"}],
        ],
    )
    with flow.transport():
        result = invoke(tmp_path, pipeline_settings(tmp_path))
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output.strip().splitlines()[-1])
    (completed,) = [event for line in result.output.splitlines() if (event := json.loads(line)).get("event") == "run_completed"]
    assert summary["rows_processed"] == 2
    assert completed["succeeded"] == 2
    assert completed["failed"] == 0
    assert [action["data"]["record_id"] for action in flow.actions()] == ["A", "B"]
    assert [request["cursor"] for request in flow.requests("read")] == [None, "1"]
    assert Counter(request["operation"] for request in flow.requests()) == {"read": 2, "status": 2, "write": 2}
    store = FilesystemPayloadStore(tmp_path / "payloads")
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        calls = connection.execute(select(calls_table)).mappings().all()
        members = connection.execute(select(sink_effect_members_table)).mappings().all()
        outcomes = connection.execute(select(token_outcomes_table)).mappings().all()
        discarded = connection.execute(select(validation_errors_table)).mappings().all()
    assert len(outcomes) == 2
    assert len(discarded) + len(outcomes) == 5
    assert len(discarded) == 3
    assert all(row["destination"] == "discard" for row in discarded)
    assert [json.loads(row["row_data_json"]) for row in discarded] == [
        None,
        ["bad"],
        {"record_id": 7, "result": "bad"},
    ]
    assert {member["member_effect_id"] for member in members} == {action["delivery_id"] for action in flow.actions()}
    keys = [(call["operation_id"], call["call_index"]) for call in calls]
    assert len(keys) == len(set(keys))
    http_evidence = []
    for call in calls:
        if call["response_ref"] is None:
            continue
        payload = json.loads(store.retrieve(call["response_ref"]))
        if "decoded_body" in payload:
            evidence = payload["decoded_body"]
            assert evidence["complete"] is True
            decoded = base64.b64decode(evidence["body_b64"], validate=True)
            assert len(decoded) == evidence["decoded_size"]
            http_evidence.append(json.loads(decoded))
            assert call["request_ref"] is not None
    assert len(http_evidence) == 6
    assert len(calls) > len(http_evidence)


def test_replay_unset_secrets_and_verify_volatile_headers_publish_no_sink(
    tmp_path: Path, credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "ok"}]])
    settings = pipeline_settings(tmp_path)
    with flow.transport():
        live = invoke(tmp_path, settings)
    assert live.exit_code == 0, live.output
    settings.update(run_mode="replay", replay_from=json.loads(live.output.strip().splitlines()[-1])["run_id"])
    before = flow.requests()
    monkeypatch.delenv("POWER_AUTOMATE_READ_TRIGGER_URL")
    monkeypatch.delenv("POWER_AUTOMATE_WRITE_TRIGGER_URL")
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("offline replay resolved DNS")),
        patch.object(PowerAutomateSource, "load", side_effect=AssertionError("replay loaded source")),
        patch.object(PowerAutomateSink, "make_http_post_factory", side_effect=AssertionError("replay composed sink HTTP")),
    ):
        replay = invoke(tmp_path, settings)
    assert replay.exit_code == 0, replay.output
    assert flow.requests() == before
    monkeypatch.setenv("POWER_AUTOMATE_READ_TRIGGER_URL", READ_URL)
    settings["run_mode"] = "verify"
    flow.headers["x-flow-request-id"] = "changed"
    flow.pretty = True
    with (
        flow.transport(),
        patch.object(PowerAutomateSink, "make_http_post_factory", side_effect=AssertionError("verify composed sink HTTP")),
    ):
        verified = invoke(tmp_path, settings)
    assert verified.exit_code == 0, verified.output
    assert len(flow.requests("read")) == 2
    assert len(flow.requests("write")) == 1
    assert len(flow.actions()) == 1


def test_verify_changed_source_fails_before_downstream_start(tmp_path: Path, credentials: None) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "ok"}]])
    settings = pipeline_settings(tmp_path)
    with flow.transport():
        live = invoke(tmp_path, settings)
    assert live.exit_code == 0, live.output
    settings.update(run_mode="verify", replay_from=json.loads(live.output.strip().splitlines()[-1])["run_id"])
    flow.pages[0][0]["result"] = "changed"
    with (
        flow.transport(),
        patch.object(PassThrough, "on_start", side_effect=AssertionError("downstream started before source verification")) as startup,
    ):
        mismatch = invoke(tmp_path, settings)
    assert mismatch.exit_code == 2, mismatch.output
    startup.assert_not_called()
    assert len(flow.actions()) == 1


def test_shipped_example_runs_without_tenant_credentials(tmp_path: Path, credentials: None) -> None:
    example = Path(__file__).resolve().parents[3] / "examples" / "power_automate" / "settings.yaml"
    settings = yaml.safe_load(example.read_text())
    settings["landscape"]["url"] = f"sqlite:///{tmp_path / 'landscape.db'}"
    settings["payload_store"] = {"base_path": str(tmp_path / "payloads")}
    settings["sinks"]["quarantine"]["options"]["path"] = str(tmp_path / "quarantine.jsonl")
    settings["concurrency"] = {"max_workers": 1}
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "ok"}]])
    with flow.transport():
        result = invoke(tmp_path, settings)
    assert result.exit_code == 0, result.output
    assert len(flow.actions()) == 1


def test_offline_replay_changed_nonsecret_config_refuses_before_credentials(
    tmp_path: Path, credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "ok"}]])
    settings = pipeline_settings(tmp_path)
    with flow.transport():
        live = invoke(tmp_path, settings)
    assert live.exit_code == 0, live.output
    settings.update(run_mode="replay", replay_from=json.loads(live.output.strip().splitlines()[-1])["run_id"])
    settings["sources"]["records"]["options"]["query"]["dataset"] = "changed"
    monkeypatch.delenv("POWER_AUTOMATE_READ_TRIGGER_URL")
    monkeypatch.delenv("POWER_AUTOMATE_WRITE_TRIGGER_URL")
    with patch("socket.getaddrinfo", side_effect=AssertionError("invalid replay resolved DNS")) as dns:
        refused = invoke(tmp_path, settings)
    assert refused.exit_code != 0, refused.output
    dns.assert_not_called()
    assert "power_automate_options_differ" in refused.output + str(refused.exception)
    assert len(flow.actions()) == 1
    assert len(flow.requests("read")) == 1


def test_snapshot_cap_refuses_before_any_sink_effect(tmp_path: Path, credentials: None, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "x" * 512}]])
    monkeypatch.setattr("elspeth.engine.orchestrator.source_iteration.SOURCE_SNAPSHOT_MAX_BYTES", 128)
    with flow.transport():
        refused = invoke(tmp_path, pipeline_settings(tmp_path))
    assert refused.exit_code != 0, refused.output
    assert "snapshot" in refused.output.lower()
    assert flow.requests("write") == []
    assert flow.requests("status") == []
    assert flow.actions() == []


def test_multiple_snapshot_sources_refuse_before_loading(tmp_path: Path, credentials: None) -> None:
    settings = pipeline_settings(tmp_path)
    settings["sources"]["second"] = deepcopy(settings["sources"]["records"])
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "ok"}]])
    with (
        flow.transport(),
        patch.object(PowerAutomateSource, "load", side_effect=AssertionError("multiple snapshot sources loaded")) as load,
    ):
        refused = invoke(tmp_path, settings)
    assert refused.exit_code != 0, refused.output
    load.assert_not_called()
    assert flow.requests() == []


def test_empty_final_snapshot_replays_and_verifies_without_sink_activity(tmp_path: Path, credentials: None) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[]])
    settings = pipeline_settings(tmp_path)
    with flow.transport():
        live = invoke(tmp_path, settings)
    assert live.exit_code == 0, live.output
    original = json.loads(live.output.strip().splitlines()[-1])
    assert original["status"] == "empty"
    settings["replay_from"] = original["run_id"]
    for mode in ("replay", "verify"):
        settings["run_mode"] = mode
        with flow.transport():
            result = invoke(tmp_path, settings)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output.strip().splitlines()[-1])["status"] == "empty"
    assert len(flow.requests("read")) == 2
    assert flow.requests("write") == []
    assert flow.requests("status") == []
    assert flow.actions() == []


@pytest.mark.parametrize("invalid_value", [9007199254740992, chr(0xD800)], ids=["unsafe-integer", "lone-surrogate"])
@pytest.mark.parametrize("media_type", ["application/json", "APPLICATION/JSON", "Application/Json; charset=UTF-8"])
def test_unrepresentable_json_page_records_complete_http_error_before_rows(
    tmp_path: Path, credentials: None, invalid_value: object, media_type: str
) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[invalid_value, {"record_id": "A", "result": "ok"}]])
    flow.headers["content-type"] = media_type
    with flow.transport():
        failed = invoke(tmp_path, pipeline_settings(tmp_path))
    assert failed.exit_code != 0, failed.output
    assert str(invalid_value) not in failed.output
    assert flow.requests("write") == flow.requests("status") == flow.actions() == []
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        calls = connection.execute(select(calls_table)).mappings().all()
        source_rows = connection.execute(select(rows_table)).mappings().all()
    assert source_rows == []
    assert len(calls) == 1
    assert calls[0]["status"] == "error"
    assert calls[0]["response_ref"] is not None
    payload = json.loads(FilesystemPayloadStore(tmp_path / "payloads").retrieve(calls[0]["response_ref"]))
    assert payload["body"] is None
    assert payload["decoded_body"]["complete"] is True
    decoded = base64.b64decode(payload["decoded_body"]["body_b64"], validate=True)
    assert json.loads(decoded)["rows"][0] == invalid_value
    assert json.loads(calls[0]["error_json"])["message"] == "invalid_json"


@pytest.mark.parametrize("media_type", ["application/json", "APPLICATION/JSON", "Application/Json; charset=UTF-8"])
def test_safe_numeric_candidate_is_individually_discarded(tmp_path: Path, credentials: None, media_type: str) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[42, {"record_id": "A", "result": "ok"}]])
    flow.headers["content-type"] = media_type
    with flow.transport():
        result = invoke(tmp_path, pipeline_settings(tmp_path))
    assert result.exit_code == 0, result.output
    assert len(flow.actions()) == 1
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        discarded = connection.execute(select(validation_errors_table)).mappings().all()
        calls = connection.execute(select(calls_table).order_by(calls_table.c.created_at)).mappings().all()
    assert len(discarded) == 1
    assert json.loads(discarded[0]["row_data_json"]) == 42
    payload = json.loads(FilesystemPayloadStore(tmp_path / "payloads").retrieve(calls[0]["response_ref"]))
    assert payload["body"]["rows"][0] == 42


def test_actual_pipeline_routes_source_quarantine_and_sticky_sink_rejection(tmp_path: Path, credentials: None) -> None:
    settings = pipeline_settings(tmp_path)
    settings["sources"]["records"]["options"]["on_validation_failure"] = "quarantine"
    settings["sinks"]["publish"]["on_write_failure"] = "quarantine"
    settings["sinks"]["quarantine"] = {
        "plugin": "json",
        "on_write_failure": "discard",
        "options": {"path": str(tmp_path / "quarantine.jsonl"), "format": "jsonl", "mode": "append", "schema": {"mode": "observed"}},
    }
    flow = DurablePowerAutomateFlow(
        tmp_path / "target.db", pages=[[None, {"record_id": "A", "result": "ok"}, {"record_id": "B", "result": "rejected"}]]
    )
    flow.reject_record_ids.add("B")
    with flow.transport():
        result = invoke(tmp_path, settings)
    assert result.exit_code == 1, result.output
    assert [action["data"]["record_id"] for action in flow.actions()] == ["A"]
    quarantine = [json.loads(line) for line in (tmp_path / "quarantine.jsonl").read_text().splitlines()]
    assert len(quarantine) == 2
    assert {"_raw": None} in quarantine
    assert any(row.get("record_id") == "B" for row in quarantine)
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        outcomes = connection.execute(select(token_outcomes_table)).mappings().all()
    assert len(outcomes) == 3
    assert Counter(outcome["outcome"] for outcome in outcomes) == {"success": 1, "failure": 1, "transient": 1}
    assert Counter(outcome["sink_name"] for outcome in outcomes) == {"publish": 1, "quarantine": 2}


@pytest.mark.parametrize("corruption", ["version", "source_hash", "settings_hash", "payload"])
def test_offline_replay_corrupt_archive_refuses_without_secret_lookup(
    tmp_path: Path, credentials: None, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[{"record_id": "A", "result": "ok"}]])
    settings = pipeline_settings(tmp_path)
    with flow.transport():
        live = invoke(tmp_path, settings)
    assert live.exit_code == 0, live.output
    settings.update(run_mode="replay", replay_from=json.loads(live.output.strip().splitlines()[-1])["run_id"])
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.begin() as connection:
        if corruption == "version":
            connection.execute(update(nodes_table).where(nodes_table.c.node_type == "source").values(plugin_version="99.0"))
        elif corruption == "source_hash":
            connection.execute(update(nodes_table).where(nodes_table.c.node_type == "source").values(source_file_hash="sha256:" + "0" * 16))
        elif corruption == "settings_hash":
            connection.execute(update(runs_table).values(config_hash="0" * 64))
        else:
            payload_ref = connection.execute(select(rows_table.c.source_data_ref)).scalar_one()
            (tmp_path / "payloads" / payload_ref[:2] / payload_ref).write_bytes(b"corrupt")
    monkeypatch.delenv("POWER_AUTOMATE_READ_TRIGGER_URL")
    monkeypatch.delenv("POWER_AUTOMATE_WRITE_TRIGGER_URL")
    with patch("socket.getaddrinfo", side_effect=AssertionError("corrupt replay resolved DNS")) as dns:
        refused = invoke(tmp_path, settings)
    assert refused.exit_code != 0, refused.output
    dns.assert_not_called()
    diagnostic = refused.output + str(refused.exception)
    if corruption == "payload":
        assert "payload" in diagnostic.lower()
    else:
        assert "power_automate_archive_" in diagnostic
    assert len(flow.actions()) == len(flow.requests("read")) == 1


@pytest.mark.parametrize("row_policy", ["discard", "quarantine"])
def test_replay_and_verify_preserve_ordered_bad_candidate_decisions(tmp_path: Path, credentials: None, row_policy: str) -> None:
    flow = DurablePowerAutomateFlow(tmp_path / "target.db", pages=[[None, ["bad"], {"_raw": ["bad"]}, {"record_id": "A", "result": "ok"}]])
    settings = pipeline_settings(tmp_path)
    if row_policy == "quarantine":
        settings["sources"]["records"]["options"]["on_validation_failure"] = "quarantine"
        settings["sinks"]["quarantine"] = {
            "plugin": "json",
            "on_write_failure": "discard",
            "options": {"path": str(tmp_path / "quarantine.jsonl"), "format": "jsonl", "mode": "append", "schema": {"mode": "observed"}},
        }
    with flow.transport():
        live = invoke(tmp_path, settings)
    assert live.exit_code == (1 if row_policy == "quarantine" else 0), live.output
    original_id = json.loads(live.output.strip().splitlines()[-1])["run_id"]
    settings["replay_from"] = original_id
    run_ids = [original_id]
    for mode in ("replay", "replay", "verify"):
        settings["run_mode"] = mode
        settings["replay_from"] = original_id if mode == "verify" else run_ids[-1]
        with flow.transport():
            result = invoke(tmp_path, settings)
        assert result.exit_code == live.exit_code, result.output
        assert result.output, repr(result.exception)
        run_ids.append(json.loads(result.output.strip().splitlines()[-1])["run_id"])
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        decisions = []
        for run_id in run_ids:
            rows = (
                connection.execute(
                    select(validation_errors_table)
                    .where(validation_errors_table.c.run_id == run_id)
                    .order_by(validation_errors_table.c.created_at, validation_errors_table.c.error_id)
                )
                .mappings()
                .all()
            )
            if row_policy == "quarantine":
                source_rows = (
                    connection.execute(select(rows_table).where(rows_table.c.run_id == run_id).order_by(rows_table.c.source_row_index))
                    .mappings()
                    .all()
                )
                source_row_ids = {row["row_id"] for row in source_rows}
                assert len(source_row_ids) == 4
                assert all(row["row_id"] in source_row_ids for row in rows)
                assert source_rows[1]["source_data_ref"] == source_rows[2]["source_data_ref"]
                assert rows[1]["row_id"] != rows[2]["row_id"]
            else:
                assert all(row["row_id"] is None for row in rows)
            decisions.append([(row["row_hash"], row["row_data_json"], row["error"], row["destination"]) for row in rows])
    assert len(decisions[0]) == 3
    assert [json.loads(decision[1]) for decision in decisions[0]] == [None, ["bad"], {"_raw": ["bad"]}]
    assert all(decision == decisions[0] for decision in decisions[1:])
    assert len(flow.requests("read")) == 2
    assert len(flow.requests("write")) == len(flow.actions()) == 1
