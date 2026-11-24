"""Accepting a pipeline proposal that binds a prompt file over the inline cap is a typed 413.

The review-card writer refuses a prompt file no run could use
(``PromptFileTooLargeError``). Manual accept renders that refusal through
``project_composer_operation_error``, which answers 413 with the YAML import's
body shape: the error code, a message naming only the node and the cap, and
what the user can do from the accept screen. The proposal stays pending and
nothing is committed.

Auto-commit never settles such a proposal: the planner mints a commit intent
only on a green runtime preflight, and an oversize prompt file is never green
(``test_explicit_approve_accept_review_cards.py::
test_oversize_prompt_file_commits_the_head_without_its_card`` pins the
``oversized_inline_blob_content`` finding that withholds it).

Real: the compose loop, ``set_pipeline``, the user upload, the HTTP accept and
reject routes, runtime preflight, settlement and the card writer. Scripted: the
provider seam and the advisor checkpoint.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.fixtures.composer_fakes import StaticPluginSnapshotFactory
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.unit.web.composer.test_service import _make_llm_response
from tests.unit.web.composer.test_tools import _mock_catalog
from tests.unit.web.sessions.routes.composer.test_explicit_approve_accept_review_cards import (
    NODE,
    _call,
    _compose_turn,
    _Harness,
    _harness,
    _upload_prompt_file,
)

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP, BLOB_INLINE_PER_REF_BYTE_CAP
from elspeth.web.app import create_app
from elspeth.web.composer.state import CompositionState
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from elspeth.web.sessions.manual_proposal_failure import ComposerManualProposalFailure
from elspeth.web.sessions.protocol import PromptFileTooLargeError

# Not credential-shaped and not prompt-like, so its absence from a body is a clean signal.
_CONTENT_MARKER = "zzfilecontentmarker"
_OVERSIZE = (_CONTENT_MARKER + "\n").encode("utf-8") * (BLOB_INLINE_PER_REF_BYTE_CAP // len(_CONTENT_MARKER) + 2)
_SMALL = b"Assess this row from a file: {{ row.text }}"


def _per_file_message(node_id: str) -> str:
    return (
        f"Node {node_id}: A prompt file exceeds the {BLOB_INLINE_PER_REF_BYTE_CAP}-byte (256 KiB) inline limit, "
        "so a run could not use it. Shorten the prompt file, then review it. "
        "The proposal is still pending: reject it, shorten the file, and ask for the change again."
    )


def _mount_manual_proposal_handler(app: FastAPI, tmp_path: Path) -> None:
    """Mount the handler create_app registers for a failed manual accept (the test app is not create_app)."""
    settings = app.state.settings.model_copy(update={"data_dir": tmp_path / "handler-owner", "composer_boot_probe_enabled": False})
    real_app = create_app(settings, process_watchdog_factory=OwnedTestProcessWatchdog)
    with ExitStack() as cleanup:
        cleanup.callback(real_app.state.readiness_probe_runner.close)
        cleanup.callback(real_app.state._session_engine_finalizer)
        cleanup.callback(real_app.state._auth_audit_finalizer)
        handler = real_app.exception_handlers[ComposerManualProposalFailure]
    app.add_exception_handler(ComposerManualProposalFailure, handler)


async def _accept_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    harness = await _harness(tmp_path, monkeypatch, "explicit_approve")
    harness.app.state.composer_service = harness.composer
    # One catalog for the compose turn and the accept, as create_app wires it.
    catalog = _mock_catalog()
    harness.app.state.catalog_service = catalog
    harness.app.state.plugin_snapshot_factory = StaticPluginSnapshotFactory(PluginAvailabilitySnapshot.for_trained_operator(catalog))
    _mount_manual_proposal_handler(harness.app, tmp_path)
    return harness


def _set_pipeline_binding(field: str, blob_id: uuid.UUID, content: bytes, call_id: str) -> Any:
    """The planner's set_pipeline: the seeded pipeline with one prompt field bound to a file."""

    def script(state: CompositionState, _state_id: uuid.UUID) -> tuple[Any, ...]:
        source = state.sources["source"]
        node = state.nodes[0]
        output = state.outputs[0]
        options = {
            key: value
            for key, value in deep_thaw(node.options).items()
            if key not in ("interpretation_requirements", "approved_prompt_artifact_hash")
        }
        options[field] = {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": sha256(content).hexdigest()}
        arguments = {
            "source": {
                "plugin": source.plugin,
                "on_success": source.on_success,
                "options": deep_thaw(source.options),
                "on_validation_failure": source.on_validation_failure,
            },
            "nodes": [
                {
                    "id": node.id,
                    "node_type": node.node_type,
                    "plugin": node.plugin,
                    "input": node.input,
                    "on_success": node.on_success,
                    "on_error": node.on_error,
                    "options": options,
                }
            ],
            "edges": [],
            "outputs": [
                {
                    "sink_name": output.name,
                    "plugin": output.plugin,
                    "options": {**deep_thaw(output.options), "mode": "write", "collision_policy": "auto_increment"},
                    "on_write_failure": output.on_write_failure,
                }
            ],
        }
        return (_call("set_pipeline", arguments, call_id), _make_llm_response(content="Staged for approval."))

    return script


async def _only_pending_pipeline_proposal(harness: _Harness) -> Any:
    proposals = await harness.service.list_composition_proposals(harness.session_id, status="pending")
    assert [(proposal.tool_name, proposal.pipeline_metadata is not None) for proposal in proposals] == [("set_pipeline", True)]
    return proposals[0]


async def _post(harness: _Harness, path: str, body: dict[str, Any]) -> Any:
    transport = ASGITransport(app=harness.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/api/sessions/{harness.session_id}/{path}", json=body)


async def _accept(harness: _Harness, proposal: Any) -> Any:
    assert proposal.pipeline_metadata is not None
    return await _post(harness, f"proposals/{proposal.id}/accept", {"draft_hash": proposal.pipeline_metadata.draft_hash})


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["system_prompt", "prompt_template"])
async def test_accept_of_an_oversize_prompt_file_is_a_typed_413_and_a_smaller_file_then_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    harness = await _accept_harness(tmp_path, monkeypatch)
    oversize = await _upload_prompt_file(harness, _OVERSIZE)
    await _compose_turn(harness, _set_pipeline_binding(field, oversize, _OVERSIZE, "pipe-1"), "Use the uploaded prompt file")
    proposal = await _only_pending_pipeline_proposal(harness)

    refused = await _accept(harness, proposal)

    assert refused.status_code == 413, refused.text
    assert refused.json() == {
        "detail": {
            "error_code": "prompt_file_too_large",
            "message": _per_file_message(NODE),
            "request_id": refused.json()["detail"]["request_id"],
        }
    }
    # The refusal names the node and the cap only, never the file's content or identity.
    for private in (_CONTENT_MARKER, str(oversize), sha256(_OVERSIZE).hexdigest()):
        assert private not in refused.text
    # Nothing committed: the proposal is still pending and the head did not move.
    assert [p.status for p in await harness.service.list_composition_proposals(harness.session_id)] == ["pending"]
    assert (await harness.service.get_current_state(harness.session_id)).id == harness.seed_state_id

    # The way on: reject the proposal, bind a file under the cap, accept that.
    rejected = await _post(harness, f"proposals/{proposal.id}/reject", {})
    assert rejected.status_code == 200, rejected.text
    small = await _upload_prompt_file(harness, _SMALL)
    await _compose_turn(harness, _set_pipeline_binding(field, small, _SMALL, "pipe-2"), "Use the shorter prompt file")
    retry = await _only_pending_pipeline_proposal(harness)
    accepted = await _accept(harness, retry)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "committed"
    head = await harness.service.get_current_state(harness.session_id)
    assert str(head.id) == accepted.json()["committed_state_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["system_prompt", "prompt_template"])
async def test_accept_of_a_prompt_file_under_the_cap_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """Control: the same path with a file under the cap reaches the writer and commits."""
    harness = await _accept_harness(tmp_path, monkeypatch)
    small = await _upload_prompt_file(harness, _SMALL)
    await _compose_turn(harness, _set_pipeline_binding(field, small, _SMALL, "pipe-1"), "Use the uploaded prompt file")
    proposal = await _only_pending_pipeline_proposal(harness)

    accepted = await _accept(harness, proposal)

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "committed"
    assert (await harness.service.get_current_state(harness.session_id)).id != harness.seed_state_id


@pytest.mark.parametrize(
    ("aggregate", "limit_bytes", "subject"),
    [
        pytest.param(False, BLOB_INLINE_PER_REF_BYTE_CAP, "A prompt file exceeds", id="per_file"),
        pytest.param(True, BLOB_INLINE_AGGREGATE_BYTE_CAP, "The inline files bound to this pipeline together exceed", id="aggregate"),
    ],
)
def test_projection_of_either_cap_is_the_typed_413(aggregate: bool, limit_bytes: int, subject: str) -> None:
    """The writer raises the same class for the per-file and the all-files cap; both take this arm."""
    projected = project_composer_operation_error(
        PromptFileTooLargeError(node_id="score", limit_bytes=limit_bytes, aggregate=aggregate), request_id="turn-request"
    )

    assert (projected.http_status, projected.failure_code, projected.diagnostic_id) == (413, "http_error", None)
    assert projected.body == {
        "detail": {
            "error_code": "prompt_file_too_large",
            "message": (
                f"Node score: {subject} the {limit_bytes}-byte ({limit_bytes // 1024} KiB) inline limit, "
                "so a run could not use it. Shorten the prompt file, then review it. "
                "The proposal is still pending: reject it, shorten the file, and ask for the change again."
            ),
            "request_id": "turn-request",
        }
    }
