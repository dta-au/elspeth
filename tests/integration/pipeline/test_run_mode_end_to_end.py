"""A retained live CLI run can replay and verify without sink publication."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import call_verifications_table, node_states_table, runs_table
from elspeth.plugins.sinks.json_sink import JSONSink
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.transforms.passthrough import PassThrough


def _settings(tmp_path: Path) -> dict[str, object]:
    source_path = tmp_path / "input.csv"
    source_path.write_text("value\n7\n8\n")
    sink_path = tmp_path / "output.json"
    return {
        "sources": {
            "primary": {
                "plugin": "csv",
                "on_success": "source_out",
                "options": {
                    "path": str(source_path),
                    "on_validation_failure": "discard",
                    "schema": {"mode": "observed"},
                },
            }
        },
        "transforms": [
            {
                "name": "copy",
                "plugin": "passthrough",
                "input": "source_out",
                "on_success": "output",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        ],
        "sinks": {
            "output": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(sink_path), "schema": {"mode": "observed"}},
            }
        },
        "landscape": {"url": f"sqlite:///{tmp_path / 'landscape.db'}"},
        "concurrency": {"max_workers": 1},
        "payload_store": {"base_path": str(tmp_path / "payloads")},
    }


def test_cli_live_replay_verify_preserves_sink_artifact(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.yaml"
    sink_path = tmp_path / "output.json"
    settings = _settings(tmp_path)
    runner = CliRunner()

    def invoke() -> dict[str, object]:
        settings_path.write_text(yaml.safe_dump(settings))
        result = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.output.strip().splitlines()[-1])

    live = invoke()
    assert live["status"] == "completed"
    artifact = sink_path.read_bytes()

    settings["run_mode"] = "replay"
    settings["replay_from"] = live["run_id"]
    with (
        patch.object(CSVSource, "on_start", side_effect=AssertionError("replay started live source")),
        patch.object(CSVSource, "load", side_effect=AssertionError("replay read live source")),
        patch.object(JSONSink, "on_start", side_effect=AssertionError("replay started live sink")),
        patch.object(JSONSink, "inspect_effect", side_effect=AssertionError("replay inspected live sink")),
        patch.object(JSONSink, "prepare_effect", side_effect=AssertionError("replay prepared live sink")),
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("replay committed live sink")),
        patch.object(JSONSink, "write", side_effect=AssertionError("replay wrote sink")),
        patch.object(JSONSink, "close", side_effect=AssertionError("replay closed live sink")),
    ):
        replay = invoke()
    assert replay["status"] == "completed"
    assert sink_path.read_bytes() == artifact

    settings["run_mode"] = "verify"
    with (
        patch.object(JSONSink, "on_start", side_effect=AssertionError("verify started live sink")),
        patch.object(JSONSink, "inspect_effect", side_effect=AssertionError("verify inspected live sink")),
        patch.object(JSONSink, "prepare_effect", side_effect=AssertionError("verify prepared live sink")),
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("verify committed live sink")),
        patch.object(JSONSink, "write", side_effect=AssertionError("verify wrote sink")),
        patch.object(JSONSink, "close", side_effect=AssertionError("verify closed live sink")),
    ):
        verify = invoke()
    assert verify["status"] == "completed"
    assert sink_path.read_bytes() == artifact


def test_verify_accepts_sparse_json_source_contract_growth(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    source = cast("dict[str, object]", settings["sources"])["primary"]
    source = cast("dict[str, object]", source)
    source["plugin"] = "json"
    options = cast("dict[str, object]", source["options"])
    input_path = tmp_path / "input.json"
    input_path.write_text('[{"value":7},{"value":8,"extra":"x"}]', encoding="utf-8")
    options["path"] = str(input_path)
    settings_path = tmp_path / "settings.yaml"
    runner = CliRunner()

    def invoke() -> object:
        settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
        return runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])

    live = invoke()
    assert live.exit_code == 0, live.output
    settings["run_mode"] = "verify"
    settings["replay_from"] = json.loads(live.output.strip().splitlines()[-1])["run_id"]

    verify = invoke()

    assert verify.exit_code == 0, verify.output


def test_replay_accepts_live_run_with_default_worker_count(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.pop("concurrency")
    settings_path = tmp_path / "settings.yaml"
    runner = CliRunner()
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    live = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    assert live.exit_code == 0, live.output
    settings["run_mode"] = "replay"
    settings["replay_from"] = json.loads(live.output.strip().splitlines()[-1])["run_id"]
    settings["concurrency"] = {"max_workers": 1}
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")

    replay = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])

    assert replay.exit_code == 0, replay.output


def test_verify_rejects_late_source_drift_before_transform_start(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    sources = cast("dict[str, object]", settings["sources"])
    second_path = tmp_path / "second.csv"
    second_path.write_text("value\n9\n")
    sources["second"] = {
        "plugin": "csv",
        "on_success": "second_output",
        "options": {"path": str(second_path), "on_validation_failure": "discard", "schema": {"mode": "observed"}},
    }
    sinks = cast("dict[str, object]", settings["sinks"])
    sinks["second_output"] = {
        "plugin": "json",
        "on_write_failure": "discard",
        "options": {"path": str(tmp_path / "second_output.json"), "schema": {"mode": "observed"}},
    }
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
    runner = CliRunner()
    live = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    assert live.exit_code == 0, live.output
    live_run_id = json.loads(live.output.strip().splitlines()[-1])["run_id"]

    second_path.write_text("value\n99\n")
    settings["run_mode"] = "verify"
    settings["replay_from"] = live_run_id
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
    with patch.object(PassThrough, "on_start", side_effect=AssertionError("transform started before complete source check")) as startup:
        result = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    assert result.exit_code != 0, result.output
    assert "source" in result.output.lower()
    startup.assert_not_called()


def test_verify_source_drift_has_distinct_nonfatal_cli_verdict(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings_path = tmp_path / "settings.yaml"
    runner = CliRunner()
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    live = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    assert live.exit_code == 0, live.output
    settings["run_mode"] = "verify"
    settings["replay_from"] = json.loads(live.output.strip().splitlines()[-1])["run_id"]
    (tmp_path / "input.csv").write_text("value\n7\n99\n", encoding="utf-8")
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")

    verify = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])

    assert verify.exit_code == 2, verify.output
    assert '"event": "verification_mismatch"' in verify.output
    assert '"traceback"' not in verify.output


def test_cli_http_replay_has_no_network_and_verify_persists_mismatch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "input.csv").write_text("url\nhttps://example.org/page\n")
    settings["transforms"] = [
        {
            "name": "fetch",
            "plugin": "web_scrape",
            "input": "source_out",
            "on_success": "output",
            "on_error": "discard",
            "options": {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "page_content",
                "fingerprint_field": "page_fingerprint",
                "format": "text",
                "http": {"abuse_contact": "audit@example.org", "scraping_reason": "Replay verification test"},
            },
        }
    ]
    settings_path = tmp_path / "settings.yaml"
    sink_path = tmp_path / "output.json"
    runner = CliRunner()
    ip = "104.18.27.120"

    def invoke() -> object:
        settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
        return runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])

    def fixed_dns(_host: str, port: int, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    with respx.mock(assert_all_mocked=True) as router, patch("socket.getaddrinfo", side_effect=fixed_dns):
        route = router.get(f"https://{ip}:443/page").mock(
            return_value=httpx.Response(200, text="<html><body>original page</body></html>", headers={"content-type": "text/html"})
        )
        live = invoke()
        assert live.exit_code == 0, live.output
        assert route.call_count == 1
    live_run_id = json.loads(live.output.strip().splitlines()[-1])["run_id"]
    artifact = sink_path.read_bytes()

    settings["run_mode"] = "replay"
    settings["replay_from"] = live_run_id
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("replay resolved DNS")),
        patch("elspeth.plugins.infrastructure.clients.http.httpx.Client", side_effect=AssertionError("replay opened HTTP client")),
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("replay published sink")),
    ):
        replay = invoke()
    assert replay.exit_code == 0, replay.output
    assert sink_path.read_bytes() == artifact

    settings["run_mode"] = "verify"
    with respx.mock(assert_all_mocked=True) as router, patch("socket.getaddrinfo", side_effect=fixed_dns):
        router.get(f"https://{ip}:443/page").mock(
            return_value=httpx.Response(200, text="<html><body>original page</body></html>", headers={"content-type": "text/html"})
        )
        verify = invoke()
    assert verify.exit_code == 0, verify.output
    verify_run_id = json.loads(verify.output.strip().splitlines()[-1])["run_id"]
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        decisions = (
            connection.execute(
                select(call_verifications_table.c.is_match).where(call_verifications_table.c.current_run_id == verify_run_id)
            )
            .scalars()
            .all()
        )
        assert decisions and all(decisions)
        before = set(connection.execute(select(runs_table.c.run_id)).scalars())

    with respx.mock(assert_all_mocked=True) as router, patch("socket.getaddrinfo", side_effect=fixed_dns):
        router.get(f"https://{ip}:443/page").mock(
            return_value=httpx.Response(200, text="<html><body>changed page</body></html>", headers={"content-type": "text/html"})
        )
        mismatch = invoke()
    assert mismatch.exit_code != 0, mismatch.output
    assert sink_path.read_bytes() == artifact
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        after = set(connection.execute(select(runs_table.c.run_id)).scalars())
        (failed_run_id,) = after - before
        decisions = connection.execute(
            select(call_verifications_table.c.is_match, call_verifications_table.c.source_call_id).where(
                call_verifications_table.c.current_run_id == failed_run_id
            )
        ).all()
        assert any(is_match is False and source_call_id is not None for is_match, source_call_id in decisions)

        reasons = connection.execute(
            select(node_states_table.c.success_reason_json).where(node_states_table.c.run_id == live_run_id)
        ).scalars()
        output_refs = [
            metadata["fetch_response_processed_hash"]
            for reason_json in reasons
            if reason_json is not None
            if (metadata := json.loads(reason_json).get("metadata")) is not None
            if "fetch_response_processed_hash" in metadata
        ]
        assert len(output_refs) == 1

    # Corrupt the retained transform output. Admission must reject it before
    # replay can use the archived HTTP response or touch the sink.
    output_ref = output_refs[0]
    (tmp_path / "payloads" / output_ref[:2] / output_ref).write_bytes(b"tampered archived output")
    settings["run_mode"] = "replay"
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("corrupt replay resolved DNS")),
        patch("elspeth.plugins.infrastructure.clients.http.httpx.Client", side_effect=AssertionError("corrupt replay opened HTTP client")),
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("corrupt replay published sink")),
    ):
        corrupt = invoke()
    assert corrupt.exit_code != 0, corrupt.output
    assert "payload" in corrupt.output.lower() or "integrity" in corrupt.output.lower()
    assert sink_path.read_bytes() == artifact


def test_cli_replay_refuses_sensitive_query_without_exact_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "web-scrape-test-key")
    (tmp_path / "input.csv").write_text("url\nhttps://example.org/page?token=synthetic-value\n")
    settings["transforms"] = [
        {
            "name": "fetch",
            "plugin": "web_scrape",
            "input": "source_out",
            "on_success": "output",
            "on_error": "discard",
            "options": {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "page_content",
                "fingerprint_field": "page_fingerprint",
                "format": "text",
                "http": {"abuse_contact": "audit@example.org", "scraping_reason": "Replay refusal test"},
            },
        }
    ]
    settings_path = tmp_path / "settings.yaml"
    runner = CliRunner()
    ip = "104.18.27.120"

    def invoke() -> object:
        settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
        return runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])

    def fixed_dns(_host: str, port: int, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    with respx.mock(assert_all_mocked=True) as router, patch("socket.getaddrinfo", side_effect=fixed_dns):
        route = router.get(f"https://{ip}:443/page?token=synthetic-value").mock(
            return_value=httpx.Response(200, text="safe page", headers={"content-type": "text/html"})
        )
        live = invoke()
    assert live.exit_code == 0, live.output
    assert route.call_count == 1
    artifact = (tmp_path / "output.json").read_bytes()
    settings["run_mode"] = "replay"
    settings["replay_from"] = json.loads(live.output.strip().splitlines()[-1])["run_id"]
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("replay resolved DNS")),
        patch("elspeth.plugins.infrastructure.clients.http.httpx.Client", side_effect=AssertionError("replay opened HTTP client")),
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("replay published sink")),
    ):
        replay = invoke()
    assert replay.exit_code != 0, replay.output
    assert "lacks transport" in replay.output
    assert (tmp_path / "output.json").read_bytes() == artifact


def test_cli_openrouter_replay_and_verify_ignore_response_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "openrouter-replay-test-key")
    settings = _settings(tmp_path)
    (tmp_path / "input.csv").write_text("value\n7\n")
    settings["transforms"] = [
        {
            "name": "answer",
            "plugin": "llm",
            "input": "source_out",
            "on_success": "output",
            "on_error": "discard",
            "options": {
                "provider": "openrouter",
                "model": "openai/gpt-4o",
                "api_key": "test-key",
                "prompt_template": "Answer the question.",
                "temperature": 0.0,
                "schema": {"mode": "observed"},
            },
        }
    ]
    settings_path = tmp_path / "settings.yaml"
    runner = CliRunner()

    def invoke() -> object:
        settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
        return runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])

    def completion(response_id: str, created: int, content: str, date: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": response_id,
                "created": created,
                "model": "openai/gpt-4o",
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
            headers={"date": date},
        )

    url = "https://openrouter.ai/api/v1/chat/completions"
    with respx.mock(assert_all_mocked=True) as router:
        route = router.post(url).mock(return_value=completion("first", 100, "answer", "Mon, 01 Jan 2024 00:00:00 GMT"))
        live = invoke()
    assert live.exit_code == 0, live.output
    assert route.call_count >= 1
    source_run_id = json.loads(live.output.strip().splitlines()[-1])["run_id"]
    artifact = (tmp_path / "output.json").read_bytes()

    settings["run_mode"] = "replay"
    settings["replay_from"] = source_run_id
    with (
        patch("elspeth.plugins.infrastructure.clients.http.httpx.Client", side_effect=AssertionError("replay opened HTTP client")),
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("replay published sink")),
    ):
        replay = invoke()
    assert replay.exit_code == 0, replay.output
    assert (tmp_path / "output.json").read_bytes() == artifact

    settings["run_mode"] = "verify"
    with (
        respx.mock(assert_all_mocked=True) as router,
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("verify published sink")),
    ):
        route = router.post(url).mock(return_value=completion("second-longer", 200, "answer", "Tue, 02 Jan 2024 00:00:00 GMT"))
        verify = invoke()
    assert verify.exit_code == 0, verify.output
    assert route.call_count >= 1
    verify_run_id = json.loads(verify.output.strip().splitlines()[-1])["run_id"]
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        decisions = (
            connection.execute(
                select(call_verifications_table.c.is_match).where(call_verifications_table.c.current_run_id == verify_run_id)
            )
            .scalars()
            .all()
        )
    assert len(decisions) >= 2 and all(decisions)
    assert (tmp_path / "output.json").read_bytes() == artifact

    with respx.mock(assert_all_mocked=True) as router:
        router.post(url).mock(return_value=completion("third", 300, "changed answer", "Wed, 03 Jan 2024 00:00:00 GMT"))
        mismatch = invoke()
    assert mismatch.exit_code == 2, mismatch.output
    assert "verification_mismatch" in mismatch.output


def test_cli_llm_replay_skips_sdk_and_verify_persists_mismatch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "input.csv").write_text("value\n7\n")
    settings["transforms"] = [
        {
            "name": "answer",
            "plugin": "llm",
            "input": "source_out",
            "on_success": "output",
            "on_error": "discard",
            "options": {
                "provider": "azure",
                "deployment_name": "gpt-4o",
                "endpoint": "https://test.openai.azure.com",
                "api_key": "test-key",
                "prompt_template": "Answer the question.",
                "temperature": 0.0,
                "schema": {"mode": "observed"},
            },
        }
    ]
    settings_path = tmp_path / "settings.yaml"
    sink_path = tmp_path / "output.json"
    runner = CliRunner()

    def invoke() -> object:
        settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
        return runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])

    def provider_response(content: str) -> SimpleNamespace:
        response = {"model": "gpt-4o", "choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))],
            model="gpt-4o",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            model_dump=lambda *_args, **_kwargs: response,
        )

    from openai import AzureOpenAI

    client = MagicMock(spec=AzureOpenAI)
    client.chat.completions.create.return_value = provider_response("original answer")
    with patch("openai.AzureOpenAI", return_value=client) as sdk:
        live = invoke()
    assert live.exit_code == 0, live.output
    assert sdk.call_count == 1
    assert client.chat.completions.create.call_count >= 1
    live_run_id = json.loads(live.output.strip().splitlines()[-1])["run_id"]
    artifact = sink_path.read_bytes()

    settings["run_mode"] = "replay"
    settings["replay_from"] = live_run_id
    with (
        patch("openai.AzureOpenAI", side_effect=AssertionError("replay constructed Azure SDK")),
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("replay published sink")),
    ):
        replay = invoke()
    assert replay.exit_code == 0, replay.output
    assert sink_path.read_bytes() == artifact

    settings["run_mode"] = "verify"
    client = MagicMock(spec=AzureOpenAI)
    client.chat.completions.create.return_value = provider_response("original answer")
    with patch("openai.AzureOpenAI", return_value=client):
        verify = invoke()
    assert verify.exit_code == 0, verify.output
    verify_run_id = json.loads(verify.output.strip().splitlines()[-1])["run_id"]
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        decisions = (
            connection.execute(
                select(call_verifications_table.c.is_match).where(call_verifications_table.c.current_run_id == verify_run_id)
            )
            .scalars()
            .all()
        )
        assert decisions and all(decisions)
        before = set(connection.execute(select(runs_table.c.run_id)).scalars())

    client = MagicMock(spec=AzureOpenAI)
    client.chat.completions.create.return_value = provider_response("changed answer")
    with (
        patch("openai.AzureOpenAI", return_value=client),
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("verify published sink")),
    ):
        mismatch = invoke()
    assert mismatch.exit_code != 0, mismatch.output
    assert sink_path.read_bytes() == artifact
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        after = set(connection.execute(select(runs_table.c.run_id)).scalars())
        (failed_run_id,) = after - before
        decisions = connection.execute(
            select(call_verifications_table.c.is_match, call_verifications_table.c.source_call_id).where(
                call_verifications_table.c.current_run_id == failed_run_id
            )
        ).all()
        assert any(is_match is False and source_call_id is not None for is_match, source_call_id in decisions)
