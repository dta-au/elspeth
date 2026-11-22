"""Create a generated fixture's graph in a session through the normal authenticated API.

Used by the edit battery's ``setup`` action. In order:

1. refuse a session whose graph is not empty (setup never replaces a graph);
2. upload the source data and every prompt file (``POST .../blobs``);
3. import ``pipeline.yaml`` with the source, prompt-file and sink-path
   bindings (``POST .../state/yaml``), as a person importing YAML does;
4. resolve each prompt review card the import raised, accepted as drafted,
   only after checking that the card belongs to one of the fixture's LLM
   steps and shows exactly the text of the files uploaded for that step;
5. require the resulting graph to be valid with no review left pending.

A review of any other kind is left for the operator and setup fails, naming
it. Nothing here writes to the database or calls a provider.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from scripts.staging.large_graph_fixture import LoadedFixture
from scripts.staging.opslib import ApiError, JsonObject, OperationFailed, WebClient, require_dict, require_list

PROMPT_REVIEW_KIND = "llm_prompt_template"
GRAPH_KEYS = ("sources", "nodes", "edges", "outputs", "metadata")


def graph_document_bytes(state: Mapping[str, Any]) -> int:
    """Bytes of the graph as one JSON document (sources, nodes, edges, outputs, metadata), as the planner's whole-graph tool takes it."""
    return len(json.dumps({key: state[key] for key in GRAPH_KEYS}, separators=(",", ":"), sort_keys=True).encode())


def _pending(client: WebClient, session_id: str) -> list[JsonObject]:
    events = require_dict(client.get_json(f"/api/sessions/{session_id}/interpretations?status=pending"), "reviews")["events"]
    return [require_dict(event, "review") for event in require_list(events, "reviews")]


def _current_state(client: WebClient, session_id: str) -> JsonObject | None:
    try:
        state = client.get_json(f"/api/sessions/{session_id}/state")
    except ApiError as exc:
        if exc.status == 404:
            return None
        raise
    # A session with no graph yet answers null.
    return None if state is None else require_dict(state, "state")


def setup_fixture(client: WebClient, session_id: str, fixture: LoadedFixture, *, emit: Callable[..., None]) -> JsonObject:
    """Create the fixture's graph in ``session_id``; return the summary. Raises ``OperationFailed`` on any refusal."""
    before = _current_state(client, session_id)
    if before is not None and (before["sources"] or before["nodes"] or before["outputs"]):
        raise OperationFailed("setup needs a session whose graph is empty; create a new session for the fixture")
    manifest = fixture.manifest
    entries = [manifest["source"]["file"], *manifest["files"]]
    blob_ids: dict[str, str] = {}
    for entry in entries:
        uploaded = require_dict(
            client.upload_file(
                f"/api/sessions/{session_id}/blobs",
                filename=entry["name"],
                mime_type=entry["mime_type"],
                data=fixture.contents[entry["name"]],
            ),
            "uploaded file",
        )
        blob_ids[entry["name"]] = str(uploaded["id"])
    emit("fixture_files_uploaded", files=len(blob_ids), bytes=sum(len(fixture.contents[name]) for name in blob_ids))
    client.post_json(f"/api/sessions/{session_id}/state/yaml", fixture.import_body(blob_ids))

    # The texts each LLM step's card must show, by the blob id uploaded for each of its slots.
    expected: dict[str, dict[str, str]] = {}
    for entry in manifest["files"]:
        node = entry["slot"].split(":", 1)[1].split(".", 1)[0]
        expected.setdefault(node, {})[blob_ids[entry["name"]]] = fixture.contents[entry["name"]].decode("utf-8")
    approved: list[str] = []
    left: list[str] = []
    for event in _pending(client, session_id):
        if event["kind"] != PROMPT_REVIEW_KIND:
            left.append(f"{event['kind']}:{event['affected_node_id'] if 'affected_node_id' in event else '?'}")
            continue
        snapshot = require_dict(client.get_json(f"/api/sessions/{session_id}/interpretations/{event['id']}/prompt"), "prompt review")
        node = snapshot["node_id"]
        if node not in manifest["llm_steps"] or node not in expected or snapshot["is_live_review"] is not True:
            left.append(f"{PROMPT_REVIEW_KIND}:{node}")
            continue
        shown = {binding["blob_id"]: binding["text"] for binding in require_list(snapshot["bindings"], "bindings")}
        mine = {blob_id: text for blob_id, text in expected[node].items() if blob_id in shown}
        if shown != mine or not shown:
            raise OperationFailed(f"the prompt review for {node} does not show exactly the fixture's files for it; not approved")
        client.post_json(
            f"/api/sessions/{session_id}/interpretations/{event['id']}/resolve",
            {"choice": "accepted_as_drafted", "reviewed_anchor": snapshot["anchor"]},
        )
        approved.append(node)
    state = _current_state(client, session_id)
    if state is None:
        raise OperationFailed("the import left no graph in the session")
    summary: JsonObject = {
        "state_id": state["id"],
        "valid": state["is_valid"] is True,
        "validation_errors": [
            str(error["message"]) if type(error) is dict and "message" in error else str(error)
            for error in (state["validation_errors"] or [])
        ][:10],
        "files_uploaded": len(blob_ids),
        "prompt_reviews_approved": sorted(approved),
        "reviews_left": sorted(left)
        + [f"{event['kind']}" for event in _pending(client, session_id) if event["kind"] == PROMPT_REVIEW_KIND],
        "nodes": len(state["nodes"]),
        "graph_document_bytes": graph_document_bytes(state),
        "measures": manifest["measures"],
        "positions": manifest["positions"],
    }
    emit("fixture_setup", **summary)
    if summary["reviews_left"]:
        raise OperationFailed(
            "reviews the setup cannot check are pending: " + ", ".join(summary["reviews_left"]) + "; resolve them in the Composer"
        )
    if not summary["valid"]:
        raise OperationFailed("the imported fixture graph is not valid: " + "; ".join(summary["validation_errors"]))
    return summary
