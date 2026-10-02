"""Expanded siblings retain their own external-call evidence across runs."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import (
    call_verifications_table,
    calls_table,
    node_states_table,
    token_lineage_frames_table,
    token_parents_table,
)
from elspeth.plugins.sinks.json_sink import JSONSink


@pytest.mark.parametrize("nested", [False, True])
def test_expanded_identical_requests_replay_and_verify_by_member(tmp_path: Path, nested: bool) -> None:
    source = tmp_path / "input.json"
    url = "https://example.org/page"
    source.write_text(json.dumps([{"items": [[url, url], [url, url]] if nested else [url, url]}]))
    transforms = [
        {
            "name": "expand",
            "plugin": "json_explode",
            "input": "source_out",
            "on_success": "expanded",
            "on_error": "discard",
            "options": {"array_field": "items", "output_field": "url", "include_index": False, "schema": {"mode": "observed"}},
        }
    ]
    if nested:
        transforms[0]["options"]["output_field"] = "inner"
        transforms.append(
            {
                "name": "inner_expand",
                "plugin": "json_explode",
                "input": "expanded",
                "on_success": "nested",
                "on_error": "discard",
                "options": {"array_field": "inner", "output_field": "url", "include_index": False, "schema": {"mode": "observed"}},
            }
        )
    transforms.append(
        {
            "name": "fetch",
            "plugin": "web_scrape",
            "input": "nested" if nested else "expanded",
            "on_success": "output",
            "on_error": "discard",
            "options": {
                "url_field": "url",
                "content_field": "page_content",
                "fingerprint_field": "page_fingerprint",
                "format": "text",
                "schema": {"mode": "observed"},
                "http": {"abuse_contact": "audit@example.org", "scraping_reason": "Replay test"},
            },
        }
    )
    settings = {
        "sources": {
            "primary": {
                "plugin": "json",
                "on_success": "source_out",
                "options": {
                    "path": str(source),
                    "on_validation_failure": "discard",
                    "schema": {"mode": "observed"},
                },
            }
        },
        "transforms": transforms,
        "sinks": {
            "output": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": str(tmp_path / "output.json"),
                    "schema": {"mode": "observed"},
                },
            }
        },
        "landscape": {"url": f"sqlite:///{tmp_path / 'landscape.db'}"},
        "payload_store": {"base_path": str(tmp_path / "payloads")},
        "concurrency": {"max_workers": 1},
    }
    runner = CliRunner()
    settings_path = tmp_path / "settings.yaml"

    def invoke() -> str:
        settings_path.write_text(yaml.safe_dump(settings))
        result = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.output.strip().splitlines()[-1])["run_id"]

    ip = "104.18.27.120"
    dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]
    count = 4 if nested else 2

    def response_for_member(_request: httpx.Request) -> httpx.Response:
        # The scheduler may visit nested siblings in a different order in each
        # run. This fake server assigns distinct responses by the active EXPAND
        # frame members, independent of request bytes and execution ordering.
        with LandscapeDB.from_url(settings["landscape"]["url"], create_tables=False) as db, db.engine.connect() as connection:
            active_token = connection.execute(
                select(node_states_table.c.token_id).where(
                    node_states_table.c.status == "open", node_states_table.c.node_id.like("transform_fetch_%")
                )
            ).scalar_one()
            member_ordinals = (
                connection.execute(
                    select(token_parents_table.c.ordinal)
                    .join(token_lineage_frames_table, token_lineage_frames_table.c.member_key == token_parents_table.c.token_id)
                    .where(token_lineage_frames_table.c.token_id == active_token)
                    .order_by(token_lineage_frames_table.c.depth)
                )
                .scalars()
                .all()
            )
        assert len(member_ordinals) == (2 if nested else 1)
        return httpx.Response(200, text=f"member {member_ordinals}", headers={"content-type": "text/html"})

    with respx.mock(assert_all_mocked=True) as router, patch("socket.getaddrinfo", return_value=dns):
        route = router.get(f"https://{ip}:443/page").mock(side_effect=response_for_member)
        live = invoke()
        assert route.call_count == count
    artifact = (tmp_path / "output.json").read_bytes()
    settings["run_mode"] = "replay"
    settings["replay_from"] = live
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("replay used DNS")),
        patch("elspeth.plugins.infrastructure.clients.http.httpx.Client", side_effect=AssertionError("replay used HTTP")),
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("replay published output")),
    ):
        replay = invoke()
    settings["run_mode"] = "verify"
    with (
        respx.mock(assert_all_mocked=True) as router,
        patch("socket.getaddrinfo", return_value=dns),
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("verify published output")),
    ):
        route = router.get(f"https://{ip}:443/page").mock(side_effect=response_for_member)
        verify = invoke()
        assert route.call_count == count
    assert (tmp_path / "output.json").read_bytes() == artifact
    with LandscapeDB.from_url(settings["landscape"]["url"], create_tables=False) as db, db.engine.connect() as connection:
        source_calls = (
            connection.execute(select(calls_table.c.call_id).join(node_states_table).where(node_states_table.c.run_id == live))
            .scalars()
            .all()
        )
        replay_sources = (
            connection.execute(select(calls_table.c.source_call_id).join(node_states_table).where(node_states_table.c.run_id == replay))
            .scalars()
            .all()
        )
        decisions = connection.execute(
            select(call_verifications_table.c.source_call_id, call_verifications_table.c.is_match).where(
                call_verifications_table.c.current_run_id == verify
            )
        ).all()
        assert len(source_calls) == count
        assert set(replay_sources) == set(source_calls)
        assert {row.source_call_id for row in decisions} == set(source_calls)
        assert len(decisions) == count and all(row.is_match for row in decisions)
