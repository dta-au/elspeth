"""YAML import scans the state as it will be stored, after file markers are bound.

``POST /api/sessions/{id}/state/yaml`` scans the state for credential
material as it will be stored, with the file markers named by
``inline_blob_bindings`` bound, before it saves. The scan is best effort: for
one LLM step with 252 file-held query templates the bound state is over the
scan's traversal bound, so the scan stops there and the skip is counted; the
import is still accepted, stored, and opens, as it is at 251 queries.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from elspeth.web import credential_scan
from elspeth.web.composer.graph_recovery import qualify_editable_checkpoint
from elspeth.web.sessions.converters import state_from_record
from tests.unit.web.sessions.test_import_source_binding_prompt_file import _Env
from tests.unit.web.sessions.test_routes import _close_app_session_engines as _close_app_session_engines

NODE = "capacity_probe"
FIELDS = ("case_text", "case_history", "reviewer_notes")
QUERY_HEAD = "Score this criterion from 0 to 100. Case: {{ row.input_1 }} History: {{ row.input_2 }} Notes: {{ row.input_3 }}\n"
# The query count at which the bound state first exceeds the credential scan's traversal bound.
FIRST_OVER_SCAN = 252


def _query_name(index: int) -> str:
    return f"q{index + 1:03d}"


def _pipeline_yaml(queries: int) -> str:
    """One csv source, one llm step whose system prompt and every query template are file-held, two json sinks."""
    definitions = {
        _query_name(index): {
            "input_fields": {"input_1": FIELDS[0], "input_2": FIELDS[1], "input_3": FIELDS[2]},
            "output_fields": [{"suffix": "score", "type": "integer"}, {"suffix": "rationale", "type": "string"}],
        }
        for index in range(queries)
    }

    def sink() -> dict[str, Any]:
        # A fresh mapping per sink: a shared one would dump as a YAML alias, which the importer refuses.
        return {
            "plugin": "json",
            "on_write_failure": "discard",
            "options": {"format": "json", "mode": "write", "collision_policy": "auto_increment", "schema": {"mode": "observed"}},
        }

    pipeline = {
        "sources": {
            "source": {
                "plugin": "csv",
                "on_success": "probe_in",
                "on_validation_failure": "errors",
                "options": {"schema": {"mode": "fixed", "fields": [f"{name}: str" for name in FIELDS]}},
            }
        },
        "transforms": [
            {
                "name": NODE,
                "plugin": "llm",
                "input": "probe_in",
                "on_success": "results",
                "on_error": "errors",
                "options": {
                    "profile": "task-role",
                    "schema": {"mode": "observed"},
                    "required_input_fields": list(FIELDS),
                    "response_field": "capacity_probe_summary",
                    "queries": definitions,
                },
            }
        ],
        "sinks": {"results": sink(), "errors": sink()},
    }
    return yaml.safe_dump(pipeline, sort_keys=False)


async def _import_body(env: _Env, session_id: uuid.UUID, queries: int) -> dict[str, Any]:
    """Upload the source and every prompt file, as a client does, and build the import body."""
    source = await env.upload(session_id, "source.csv", (",".join(FIELDS) + "\na,b,c\n").encode(), "text/csv")
    bindings: dict[str, dict[str, str]] = {}
    system = await env.upload(session_id, "system.md", b"# Capacity probe instructions\nScore each criterion.\n", "text/plain")
    bindings[f"node:{NODE}.options.system_prompt"] = {"blob_id": str(system.id)}
    for index in range(queries):
        name = _query_name(index)
        blob = await env.upload(session_id, f"{name}.query.md", (QUERY_HEAD + f"Criterion {name}.\n").encode(), "text/plain")
        bindings[f"node:{NODE}.options.queries.{name}.template"] = {"blob_id": str(blob.id)}
    return {
        "yaml": _pipeline_yaml(queries),
        "source_blob_ids": {"source": str(source.id)},
        "inline_blob_bindings": bindings,
        "sink_path_bindings": {
            "results": {"path": {"folder": "outputs", "relative_path": "results.json"}},
            "errors": {"path": {"folder": "outputs", "relative_path": "errors.json"}},
        },
    }


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    return _Env(tmp_path)


def test_an_import_whose_bound_state_exceeds_the_scan_is_stored_and_opens(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    skips: list[dict[str, str]] = []

    class _Recorder:
        def add(self, value: int, attributes: dict[str, str]) -> None:
            skips.append(dict(attributes))

    monkeypatch.setattr(credential_scan, "_CREDENTIAL_SCAN_SKIPPED_COUNTER", _Recorder())
    session_id = asyncio.run(env.session())
    body = asyncio.run(_import_body(env, session_id, FIRST_OVER_SCAN))

    response = env.import_yaml(session_id, body)

    assert response.status_code == 200, response.text
    assert {"surface": "composer_yaml_import", "reason": "nodes"} in skips
    record = asyncio.run(env.service.get_current_state(session_id))
    assert record is not None
    assert qualify_editable_checkpoint(state_from_record(record)).eligible
    opened = env.client.get(f"/api/sessions/{session_id}/state")
    assert opened.status_code == 200, opened.text
    assert len(opened.json()["nodes"][0]["options"]["queries"]) == FIRST_OVER_SCAN


def test_one_query_fewer_imports_stores_and_opens(env: _Env) -> None:
    session_id = asyncio.run(env.session())
    body = asyncio.run(_import_body(env, session_id, FIRST_OVER_SCAN - 1))

    response = env.import_yaml(session_id, body)

    assert response.status_code == 200, response.text
    record = asyncio.run(env.service.get_current_state(session_id))
    assert record is not None
    assert qualify_editable_checkpoint(state_from_record(record)).eligible
    opened = env.client.get(f"/api/sessions/{session_id}/state")
    assert opened.status_code == 200, opened.text
    assert len(opened.json()["nodes"][0]["options"]["queries"]) == FIRST_OVER_SCAN - 1
