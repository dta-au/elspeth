"""An accepted narrow-tool proposal commits its head together with its review cards.

In ``explicit_approve`` every composer tool call becomes a proposal. Accepting
a proposal for any tool other than ``set_pipeline`` replays the tool and
commits the new head. When that head owes an interpretation review, the same
transaction must record the pending review card: a head that owes a review
with no card cannot be reviewed until something else repairs it.

Per transition:

* proposal (compose turn): the scripted provider is called; the head does
  not move; no card is written.
* accept (HTTP): no provider call; the head and its cards are written in one
  transaction, or neither is.
* head: the card binds the committed state and carries the
  ``backend_auto_surface:`` provenance the auto-commit surfacer writes.

Real: the compose loop, the tools, the session database, the HTTP accept
route. Scripted: the provider seam and the advisor checkpoint.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from tests.helpers.session_fences import acquire_compose_context
from tests.integration.web.composer.test_freeform_proposal_prevalidation import _clean_advisor_checkpoint
from tests.unit.web.composer.test_prompt_patch_reviews import _approved_state
from tests.unit.web.composer.test_service import _make_llm_response, _make_settings
from tests.unit.web.composer.test_tools import _mock_catalog
from tests.unit.web.sessions.test_blob_proposal_accept import _save_composition_state
from tests.unit.web.sessions.test_routes import _make_app

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.contracts.freeze import deep_thaw
from elspeth.core.blobs_inline import BLOB_INLINE_PER_REF_BYTE_CAP
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.interpretation_surfacing import (
    BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX,
    unsurfaceable_pending_interpretation_review_sites,
)
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, NodeSpec
from elspeth.web.composer.tools import execute_tool as production_execute_tool
from elspeth.web.coordination.repository import _RepositoryInterpretationMutations
from elspeth.web.interpretation_state import (
    PROMPT_SHIELD_USER_TERM,
    InterpretationReviewPending,
    approved_prompt_artifact_hash_from_options,
    interpretation_sites,
    materialize_state_for_execution,
    pipeline_decision_artifact_hash,
    prompt_review_anchor_hash_from_options,
    prompt_review_draft_from_options,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import interpretation_events_table
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    InterpretationDraftMismatchError,
    InterpretationPlaceholderConsumedError,
    PromptFileTooLargeError,
)
from elspeth.web.sessions.routes.composer import proposals as proposal_routes

NODE = "code_themes"
FILE_TEXT = "Assess this row from a file: {{ row.text }}"
PROMPT_TERM = f"llm_prompt_template:{NODE}"
MODEL_TERM = f"llm_model_choice:{NODE}"
OTHER_MODEL = "openai/gpt-4o-mini"
LEGACY_TERM = "suitable"
LEGACY_SITE = (NODE, LEGACY_TERM, InterpretationKind.VAGUE_TERM)
PROMPT_SITE = (NODE, PROMPT_TERM, InterpretationKind.LLM_PROMPT_TEMPLATE)
MODEL_SITE = (NODE, MODEL_TERM, InterpretationKind.LLM_MODEL_CHOICE)
SHIELD_SITE = (NODE, PROMPT_SHIELD_USER_TERM, InterpretationKind.PIPELINE_DECISION)
SKIP_EVENT = "composer_interpretation.surface_skipped"
SHIELD_DRAFT = "Public web content from web_scrape flows directly into the LLM. Recommend inserting azure_prompt_shield."

Script = Callable[[CompositionState, UUID], tuple[Any, ...]]
Seed = Callable[[CompositionState], CompositionState]


class _ScriptedProvider:
    """The provider seam: plays a fixed script and counts every call."""

    def __init__(self) -> None:
        self._responses: list[Any] = []
        self.calls = 0

    def load(self, *responses: Any) -> None:
        self._responses = list(responses)

    async def __call__(self, messages: list[dict[str, Any]], _tools: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        if not self._responses:
            return _make_llm_response(content="Done.")
        response = self._responses.pop(0)
        return response(messages) if callable(response) else response


@dataclass(frozen=True)
class _Harness:
    app: Any
    service: Any
    composer: ComposerServiceImpl
    blob_service: BlobServiceImpl
    provider: _ScriptedProvider
    session_id: UUID
    seed_state_id: UUID
    seed_state: CompositionState


def _call(name: str, arguments: dict[str, Any], call_id: str = "edit-1") -> Any:
    return _make_llm_response(tool_calls=[{"id": call_id, "name": name, "arguments": arguments}])


def _last_tool_payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
    tool_messages = [message for message in messages if message["role"] == "tool"]
    return json.loads(tool_messages[-1]["content"])


def _created_blob_id(messages: list[dict[str, Any]]) -> str:
    payload = _last_tool_payload(messages)
    assert payload["success"] is True, payload
    return payload["data"]["blob_id"]


def _create_blob(content: str) -> Any:
    return _call("create_blob", {"filename": "prompt.txt", "mime_type": "text/plain", "content": content}, "blob-1")


def _change_substring(state: CompositionState, state_id: UUID) -> tuple[Any, ...]:
    text = state.nodes[0].options["prompt_template"]
    return (
        _call(
            "change_substring",
            {
                "target": {"kind": "node_option", "node_id": NODE, "option": "prompt_template"},
                "expected_sha256": sha256(text.encode("utf-8")).hexdigest(),
                "expected_state_id": str(state_id),
                "edits": [{"old": "Classify", "replacement": "Assess", "replace_all": False}],
            },
        ),
        _make_llm_response(content="Edited."),
    )


def _patch_prompt_text(state: CompositionState, _state_id: UUID) -> tuple[Any, ...]:
    text = state.nodes[0].options["prompt_template"]
    return (
        _call("patch_node_options", {"node_id": NODE, "patch": {"prompt_template": text.replace("Classify", "Assess")}}),
        _make_llm_response(content="Edited."),
    )


def _upsert_node(state: CompositionState, _state_id: UUID) -> tuple[Any, ...]:
    node = state.nodes[0]
    options = {
        key: value
        for key, value in deep_thaw(node.options).items()
        if key not in ("interpretation_requirements", "approved_prompt_artifact_hash")
    }
    options["prompt_template"] = options["prompt_template"].replace("Classify", "Assess")
    return (
        _call(
            "upsert_node",
            {
                "id": node.id,
                "node_type": node.node_type,
                "plugin": node.plugin,
                "input": node.input,
                "on_success": node.on_success,
                "on_error": node.on_error,
                "options": options,
            },
        ),
        _make_llm_response(content="Edited."),
    )


def _wire_created_file(_state: CompositionState, _state_id: UUID) -> tuple[Any, ...]:
    return (
        _create_blob(FILE_TEXT),
        lambda messages: _call(
            "wire_blob_inline_ref",
            {"field_path": f"node:{NODE}.options.prompt_template", "blob_id": _created_blob_id(messages)},
        ),
        _make_llm_response(content="Bound."),
    )


def _wire_existing_file(blob_id: UUID, call_id: str) -> Script:
    def script(_state: CompositionState, _state_id: UUID) -> tuple[Any, ...]:
        return (
            _call("wire_blob_inline_ref", {"field_path": f"node:{NODE}.options.prompt_template", "blob_id": str(blob_id)}, call_id),
            _make_llm_response(content="Bound."),
        )

    return script


def _patch_file_marker(_state: CompositionState, _state_id: UUID) -> tuple[Any, ...]:
    return (
        _create_blob(FILE_TEXT),
        lambda messages: _call(
            "patch_node_options",
            {
                "node_id": NODE,
                "patch": {
                    "prompt_template": {
                        "blob_ref": _created_blob_id(messages),
                        "mode": "inline_content",
                        "sha256": sha256(FILE_TEXT.encode("utf-8")).hexdigest(),
                    }
                },
            },
        ),
        _make_llm_response(content="Bound."),
    )


def _seed_resolved(state: CompositionState) -> CompositionState:
    """Every review on the seed is resolved: nothing is owed before the proposal."""
    return state


def _seed_legacy_vague_placeholder(state: CompositionState) -> CompositionState:
    """The seed owes a vague-term review the backend CANNOT turn into a card.

    A bare legacy ``{{ interpretation: ... }}`` token has no staged
    requirement, so there is no draft to show: the site-to-writer projection
    returns nothing for it and the site stays fail-closed at the run gate.
    The seed's prompt review is re-resolved over the new text, so the token
    is the only thing owed.
    """
    node = state.nodes[0]
    options = deep_thaw(node.options)
    options["prompt_template"] = options["prompt_template"] + " Use a {{ interpretation: " + LEGACY_TERM + " }} tone."
    options.pop("approved_prompt_artifact_hash")
    prompt_review = next(row for row in options["interpretation_requirements"] if row["kind"] == "llm_prompt_template")
    prompt_review["draft"] = prompt_review_draft_from_options(options)
    prompt_review["accepted_value"] = prompt_review["draft"]
    prompt_review["resolved_prompt_template_hash"] = prompt_review_anchor_hash_from_options(options)
    options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    return state.with_node(replace(node, options=options))


def _seed_resolved_shield_behind_scrape(state: CompositionState) -> CompositionState:
    """Scraped web content reaches the LLM step, with its shield review resolved.

    The prompt-shield decision's artifact hash binds every step on the LLM's
    upstream path, so a rewire that changes that path re-opens the review.
    The seed is mid-repair: a passthrough also reads the scrape output and
    feeds nothing, which the rewire below completes into one valid line
    (scrape, passthrough, LLM). Nothing is owed on the seed.
    """
    llm = state.nodes[0]
    scrape = NodeSpec(
        id="scrape",
        node_type="transform",
        plugin="web_scrape",
        input=llm.input,
        on_success="scraped",
        on_error="discard",
        options={
            "schema": {"mode": "fixed", "fields": ["url: str"]},
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "content_fingerprint",
            "format": "markdown",
            "http": {"abuse_contact": "ops@example.org", "scraping_reason": "Synthetic test page", "allowed_hosts": "public_only"},
        },
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    mid = replace(scrape, id="mid", plugin="passthrough", input="scraped", on_success="inbound", options={"schema": {"mode": "observed"}})
    llm = replace(llm, input="scraped")
    nodes = (scrape, mid, llm)
    options = deep_thaw(llm.options)
    options["interpretation_requirements"].append(
        {
            "id": f"prompt_injection_shield_review:{NODE}",
            "kind": "pipeline_decision",
            "user_term": PROMPT_SHIELD_USER_TERM,
            "status": "resolved",
            "draft": SHIELD_DRAFT,
            "event_id": "shield-event",
            "accepted_value": SHIELD_DRAFT,
            "accepted_artifact_hash": pipeline_decision_artifact_hash(llm, nodes, user_term=PROMPT_SHIELD_USER_TERM),
            "resolved_prompt_template_hash": None,
        }
    )
    options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    return replace(state, nodes=(scrape, mid, replace(llm, options=options)))


def _rewire_through_mid(_state: CompositionState, _state_id: UUID) -> tuple[Any, ...]:
    """The planner re-points the LLM step at the passthrough output, re-sending nothing else."""
    return (
        _call("upsert_node", {"id": NODE, "node_type": "transform", "input": "inbound", "routing_only": True}),
        _make_llm_response(content="Rewired."),
    )


def _patch_prompt_and_model(prompt_template: Any) -> Script:
    """One narrow edit that re-opens TWO reviews: the prompt and the model choice."""

    def script(state: CompositionState, _state_id: UUID) -> tuple[Any, ...]:
        value = prompt_template(state) if callable(prompt_template) else prompt_template
        return (
            _call("patch_node_options", {"node_id": NODE, "patch": {"prompt_template": value, "model": OTHER_MODEL}}),
            _make_llm_response(content="Edited."),
        )

    return script


def _assess_prompt_text(state: CompositionState) -> str:
    return state.nodes[0].options["prompt_template"].replace("Classify", "Assess")


TOOL_ROWS = [
    pytest.param(_change_substring, "change_substring", False, id="change_substring"),
    pytest.param(_patch_prompt_text, "patch_node_options", False, id="patch_node_options_prompt_text"),
    pytest.param(_upsert_node, "upsert_node", False, id="upsert_node"),
    pytest.param(_wire_created_file, "wire_blob_inline_ref", True, id="wire_blob_inline_ref_file"),
    pytest.param(_patch_file_marker, "patch_node_options", True, id="patch_node_options_file_marker"),
]


async def _harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trust_mode: str, seed: Seed = _seed_resolved) -> _Harness:
    app, service = _make_app(tmp_path)
    # One blob service behind both readers, as ``create_app`` wires it: the
    # composer's runtime preflight, and the session service's custody read
    # that a file-backed prompt card snapshots the file through.
    blob_service = BlobServiceImpl(service._engine, tmp_path)
    monkeypatch.setattr(service, "_inline_blob_read", lambda context, blob_id: blob_service.read_blob_content_sync(blob_id, context))
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy_catalog = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    with patch(
        "elspeth.web.composer.service.compute_availability",
        return_value=ComposerAvailability(available=True, model="test-model", provider="test"),
    ):
        composer = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=_make_settings(data_dir=tmp_path),
            sessions_service=service,
            session_engine=service._engine,
            blob_service=blob_service,
        )
    monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    provider = _ScriptedProvider()
    # The provider seam stays scripted for the whole test, so a provider call
    # made by ANY transition, the accept included, is counted.
    monkeypatch.setattr(composer._provider_gateway, "_call_llm", provider)

    def trained_dispatch(name: str, arguments: dict[str, object], state: object, _route_catalog: object, **kwargs: Any) -> object:
        return production_execute_tool(name, arguments, state, policy_catalog, **{**kwargs, "plugin_snapshot": snapshot})

    monkeypatch.setattr(proposal_routes, "execute_tool", trained_dispatch)
    session = await service.create_session("alice", "Prompt edit", "local")
    await service.update_composer_preferences(session.id, trust_mode=trust_mode, density_default="high", actor="user:alice")

    # Session-owned paths, so runtime preflight judges the pipeline rather
    # than the fixture's placeholder paths.
    state = seed(_approved_state())
    blob_dir = tmp_path / "blobs" / str(session.id)
    blob_dir.mkdir(parents=True, exist_ok=True)
    (blob_dir / "input.csv").write_text("text\nhello\n", encoding="utf-8")
    output_dir = tmp_path / "outputs" / str(session.id)
    output_dir.mkdir(parents=True, exist_ok=True)
    source = state.sources["source"]
    state = state.with_source(replace(source, options={**deep_thaw(source.options), "path": str(blob_dir / "input.csv")}))
    output = state.outputs[0]
    state = state.with_output(replace(output, options={**deep_thaw(output.options), "path": str(output_dir / "output.csv")}))
    before = state.to_dict()
    saved = await _save_composition_state(
        service,
        session.id,
        CompositionStateData(
            sources=before["sources"],
            nodes=before["nodes"],
            edges=before["edges"],
            outputs=before["outputs"],
            metadata_=before["metadata"],
            is_valid=False,
        ),
        provenance="session_seed",
    )
    return _Harness(app, service, composer, blob_service, provider, session.id, saved.id, state)


async def _compose_turn(harness: _Harness, script: Script, text: str = "Edit the prompt") -> Any:
    """One real compose turn against the current head, on the scripted provider."""
    head = await harness.service.get_current_state(harness.session_id)
    state = state_from_record(head)
    message = await harness.service.add_message(harness.session_id, "user", text, writer_principal="route_user_message")
    harness.provider.load(*script(state, head.id))
    async with acquire_compose_context(harness.service, harness.session_id) as compose_context:
        return await harness.composer.compose(
            text,
            [],
            state,
            session_id=str(harness.session_id),
            current_state_id=str(head.id),
            user_id="alice",
            user_message_id=str(message.id),
            session_operation_context=compose_context,
        )


async def _accept(harness: _Harness, proposal_id: UUID) -> Any:
    transport = ASGITransport(app=harness.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/api/sessions/{harness.session_id}/proposals/{proposal_id}/accept")


async def _pending_cards(harness: _Harness) -> list[Any]:
    return await harness.service.list_interpretation_events(harness.session_id, status="pending")


def _card_sites(cards: list[Any]) -> list[tuple[str, str, InterpretationKind]]:
    return [(card.affected_node_id, card.user_term, card.kind) for card in cards]


def _state_sites(state: CompositionState) -> set[tuple[str, str, InterpretationKind]]:
    return {(site.component_id, site.user_term, site.kind) for site in interpretation_sites(state)}


async def _only_pending_proposal(harness: _Harness, tool_name: str) -> Any:
    proposals = await harness.service.list_composition_proposals(harness.session_id, status="pending")
    assert [proposal.tool_name for proposal in proposals] == [tool_name]
    return proposals[0]


async def _upload_prompt_file(harness: _Harness, content: bytes) -> UUID:
    """A user upload, so the file's size is not bounded by a model turn."""
    async with acquire_compose_context(harness.service, harness.session_id) as upload_context:
        record = await harness.blob_service.create_blob(
            session_id=harness.session_id,
            filename="prompt.txt",
            content=content,
            mime_type="text/plain",
            created_by="user",
            session_operation_context=upload_context,
        )
    return record.id


async def _runtime_validation(harness: _Harness, *, past_pending_reviews: bool) -> Any:
    """The dry-run validation of the head.

    ``past_pending_reviews=False`` is what POST /validate runs: it stops at an
    owed review. ``True`` is the composer's own preflight after a review
    handoff, which looks past owed reviews at what else blocks a run.
    """
    head = await harness.service.get_current_state(harness.session_id)
    async with acquire_compose_context(harness.service, harness.session_id) as context:
        return await asyncio.to_thread(
            harness.composer._preflight.runtime_preflight,
            state_from_record(head),
            "alice",
            str(harness.session_id),
            session_operation_context=context,
            allow_pending_interpretation_placeholders=past_pending_reviews,
        )


def _skip_lines(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["event"] == SKIP_EVENT]


def _event_rows(harness: _Harness) -> list[Any]:
    with harness.service._engine.connect() as conn:
        return list(
            conn.execute(
                select(interpretation_events_table.c.id).where(interpretation_events_table.c.session_id == str(harness.session_id))
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("script", "tool_name", "file_bound"), TOOL_ROWS)
async def test_accepted_proposal_commits_head_with_its_review_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: Script, tool_name: str, file_bound: bool
) -> None:
    harness = await _harness(tmp_path, monkeypatch, "explicit_approve")
    assert interpretation_sites(harness.seed_state) == ()

    # Transition 1, proposal: the planner's own tool call, held for approval.
    await _compose_turn(harness, script)
    provider_calls_for_proposal = harness.provider.calls
    assert provider_calls_for_proposal >= 2
    proposal = await _only_pending_proposal(harness, tool_name)
    assert (await harness.service.get_current_state(harness.session_id)).id == harness.seed_state_id
    assert await _pending_cards(harness) == []

    # Transition 2, accept: no provider call.
    accepted = await _accept(harness, proposal.id)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "committed"
    assert harness.provider.calls == provider_calls_for_proposal

    # Transition 3, head: the committed state owes one prompt review and has its card.
    head = await harness.service.get_current_state(harness.session_id)
    assert head.id != harness.seed_state_id
    assert str(head.id) == accepted.json()["committed_state_id"]
    node = next(item for item in head.nodes if item["id"] == NODE)
    assert isinstance(node["options"]["prompt_template"], str) is not file_bound
    assert _state_sites(state_from_record(head)) == {PROMPT_SITE}
    cards = await _pending_cards(harness)
    assert _card_sites(cards) == [PROMPT_SITE], "the accepted head owes a prompt review but has no pending review card"
    assert cards[0].composition_state_id == head.id
    assert cards[0].tool_call_id is not None and cards[0].tool_call_id.startswith(BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX)


@pytest.mark.asyncio
@pytest.mark.parametrize(("script", "tool_name", "file_bound"), TOOL_ROWS)
async def test_auto_commit_turn_stages_the_same_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: Script, tool_name: str, file_bound: bool
) -> None:
    """Control: the default mode already writes this card at the end of the turn."""
    harness = await _harness(tmp_path, monkeypatch, "auto_commit")

    await _compose_turn(harness, script)

    assert await harness.service.list_composition_proposals(harness.session_id, status="pending") == []
    head = await harness.service.get_current_state(harness.session_id)
    assert head.id != harness.seed_state_id
    node = next(item for item in head.nodes if item["id"] == NODE)
    assert isinstance(node["options"]["prompt_template"], str) is not file_bound
    cards = await _pending_cards(harness)
    assert _card_sites(cards) == [PROMPT_SITE]
    assert cards[0].composition_state_id == head.id
    assert cards[0].tool_call_id is not None and cards[0].tool_call_id.startswith(BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX)


def _storage_fault(_original: Any) -> Any:
    def writer(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("injected fault while recording the review card")

    return writer


def _prompt_card_refused(_original: Any) -> Any:
    def writer(*_args: Any, **_kwargs: Any) -> Any:
        raise InterpretationDraftMismatchError("injected: the staged prompt draft is not the surfaced draft")

    return writer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "status_code"),
    [
        pytest.param(_storage_fault, 500, id="storage_fault"),
        pytest.param(_prompt_card_refused, 409, id="prompt_card_refused_by_writer"),
    ],
)
async def test_fault_while_recording_the_card_leaves_no_new_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: Callable[[Any], Any], status_code: int
) -> None:
    """The head and its cards are one transaction: a failed card write commits nothing."""
    harness = await _harness(tmp_path, monkeypatch, "explicit_approve")
    await _compose_turn(harness, _change_substring)
    proposal = await _only_pending_proposal(harness, "change_substring")

    with monkeypatch.context() as injected:
        injected.setattr(
            _RepositoryInterpretationMutations,
            "create_or_reconcile_pending",
            fault(_RepositoryInterpretationMutations.create_or_reconcile_pending),
        )
        failed = await _accept(harness, proposal.id)

    assert failed.status_code == status_code, failed.text
    assert (await harness.service.get_current_state(harness.session_id)).id == harness.seed_state_id
    assert (await _only_pending_proposal(harness, "change_substring")).id == proposal.id
    assert _event_rows(harness) == []

    # Nothing was consumed: the same proposal accepts once the fault is gone.
    retried = await _accept(harness, proposal.id)
    assert retried.status_code == 200, retried.text
    head = await harness.service.get_current_state(harness.session_id)
    assert str(head.id) == retried.json()["committed_state_id"]
    cards = await _pending_cards(harness)
    assert _card_sites(cards) == [PROMPT_SITE]
    assert cards[0].composition_state_id == head.id


@pytest.mark.asyncio
async def test_refused_card_that_is_not_a_prompt_review_is_left_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer refusing a non-prompt card costs that card only, as after an auto-commit."""
    harness = await _harness(tmp_path, monkeypatch, "explicit_approve")
    await _compose_turn(harness, _patch_prompt_and_model(_assess_prompt_text))
    proposal = await _only_pending_proposal(harness, "patch_node_options")
    original = _RepositoryInterpretationMutations.create_or_reconcile_pending
    attempted: list[InterpretationKind] = []

    def refuse_model_choice(mutations: Any, command: Any, validator: Any) -> Any:
        attempted.append(command.kind)
        if command.kind is InterpretationKind.LLM_MODEL_CHOICE:
            raise InterpretationPlaceholderConsumedError("injected: the model-choice requirement is no longer pending")
        return original(mutations, command, validator)

    monkeypatch.setattr(_RepositoryInterpretationMutations, "create_or_reconcile_pending", refuse_model_choice)

    with structlog.testing.capture_logs() as events:
        accepted = await _accept(harness, proposal.id)

    assert accepted.status_code == 200, accepted.text
    head = await harness.service.get_current_state(harness.session_id)
    assert str(head.id) == accepted.json()["committed_state_id"]
    assert _state_sites(state_from_record(head)) == {PROMPT_SITE, MODEL_SITE}
    assert attempted == [InterpretationKind.LLM_PROMPT_TEMPLATE, InterpretationKind.LLM_MODEL_CHOICE]
    assert _card_sites(await _pending_cards(harness)) == [PROMPT_SITE]
    # The card left out leaves the same one line the post-commit surfacer
    # writes for a refused card: identifiers and the exception class, never
    # the refusal message.
    assert _skip_lines(events) == [
        {
            "event": SKIP_EVENT,
            "log_level": "warning",
            "session_id": str(harness.session_id),
            "composition_state_id": str(head.id),
            "affected_node_id": NODE,
            "kind": InterpretationKind.LLM_MODEL_CHOICE.value,
            "repair_mode": False,
            "error_class": "InterpretationPlaceholderConsumedError",
        }
    ]
    assert "no longer pending" not in repr(_skip_lines(events))


@pytest.mark.asyncio
async def test_a_card_skipped_inside_a_rolled_back_accept_is_not_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The skip line is written after the commit: a skip inside an accept that rolls back never reaches the log.

    The writer leaves the prompt card out (an oversize-file refusal, which the
    accept tolerates), then a storage fault on the model card rolls the whole
    accept back. Nothing was skipped in the end, so nothing is logged.
    """
    harness = await _harness(tmp_path, monkeypatch, "explicit_approve")
    await _compose_turn(harness, _patch_prompt_and_model(_assess_prompt_text))
    proposal = await _only_pending_proposal(harness, "patch_node_options")
    attempted: list[InterpretationKind] = []

    def skip_then_fault(_mutations: Any, command: Any, _validator: Any) -> Any:
        attempted.append(command.kind)
        if command.kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
            raise PromptFileTooLargeError(node_id=NODE, limit_bytes=BLOB_INLINE_PER_REF_BYTE_CAP, aggregate=False)
        raise RuntimeError("injected fault after a skipped card")

    with monkeypatch.context() as injected:
        injected.setattr(_RepositoryInterpretationMutations, "create_or_reconcile_pending", skip_then_fault)
        with structlog.testing.capture_logs() as events:
            failed = await _accept(harness, proposal.id)

    assert failed.status_code == 500, failed.text
    assert attempted == [InterpretationKind.LLM_PROMPT_TEMPLATE, InterpretationKind.LLM_MODEL_CHOICE]
    assert (await harness.service.get_current_state(harness.session_id)).id == harness.seed_state_id
    assert _event_rows(harness) == []
    assert _skip_lines(events) == []


@pytest.mark.asyncio
async def test_review_tool_is_refused_for_a_row_inside_a_pending_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """While the proposal is pending the model cannot raise its cards; accept does.

    A staged proposal is not applied, so the review tool is checked against
    the unchanged saved pipeline, which does not owe the review yet. The call
    is refused as an argument error and raises no card. The natural repair,
    re-sending the change, is refused because a proposal is already pending.
    Accepting the proposal then raises the card with no further model call.
    """
    harness = await _harness(tmp_path, monkeypatch, "explicit_approve")
    seen: dict[str, dict[str, Any]] = {}

    def review_then(key: str, response: Any) -> Callable[[list[dict[str, Any]]], Any]:
        def respond(messages: list[dict[str, Any]]) -> Any:
            seen[key] = _last_tool_payload(messages)
            return response

        return respond

    def script(state: CompositionState, _state_id: UUID) -> tuple[Any, ...]:
        text = state.nodes[0].options["prompt_template"]
        change = {"node_id": NODE, "patch": {"prompt_template": text.replace("Classify", "Assess"), "model": OTHER_MODEL}}
        review = {"affected_node_id": NODE, "kind": "llm_model_choice", "user_term": MODEL_TERM}
        return (
            _call("patch_node_options", change, "edit-1"),
            review_then("staged", _call("request_interpretation_review", review, "review-1")),
            review_then("review", _call("patch_node_options", change, "edit-2")),
            review_then("resend", _make_llm_response(content="Awaiting approval.")),
        )

    await _compose_turn(harness, script)

    assert seen["staged"]["data"]["status"] == "APPROVAL_REQUIRED"
    assert seen["review"] == {
        "error": "Tool 'request_interpretation_review' failed: 'affected_node_id' must be a pending llm_model_choice "
        "interpretation requirement or placeholder, got missing pending llm_model_choice review site"
    }
    assert seen["resend"]["success"] is False
    assert [error["error_code"] for error in seen["resend"]["validation"]["errors"]] == ["approval_proposal_pending"]
    assert await _pending_cards(harness) == []
    proposal = await _only_pending_proposal(harness, "patch_node_options")
    calls_before_accept = harness.provider.calls

    accepted = await _accept(harness, proposal.id)

    assert accepted.status_code == 200, accepted.text
    assert sorted(_card_sites(await _pending_cards(harness))) == sorted([PROMPT_SITE, MODEL_SITE])
    assert harness.provider.calls == calls_before_accept


@pytest.mark.asyncio
@pytest.mark.parametrize("trust_mode", ["explicit_approve", "auto_commit"])
async def test_accepted_routing_only_rewire_commits_the_reopened_shield_review_with_its_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trust_mode: str
) -> None:
    """A routing-only rewire is accepted through the same route, and owes the card it re-opens.

    Moving the LLM step's input changes the upstream path its prompt-shield
    decision was approved over, so the rewire re-opens that review and names
    it in ``reopened_reviews``. The authored options are kept as stored. Both
    modes end with the card bound to the new head; under approval it is
    written by the accept, with no provider call.
    """
    harness = await _harness(tmp_path, monkeypatch, trust_mode, _seed_resolved_shield_behind_scrape)
    assert _state_sites(harness.seed_state) == set()
    stored = next(node for node in harness.seed_state.nodes if node.id == NODE)

    await _compose_turn(harness, _rewire_through_mid, "Feed the LLM step from the passthrough")
    if trust_mode == "explicit_approve":
        proposal = await _only_pending_proposal(harness, "upsert_node")
        assert proposal.arguments_json["routing_only"] is True
        assert (await harness.service.get_current_state(harness.session_id)).id == harness.seed_state_id
        assert await _pending_cards(harness) == []
        calls_before_accept = harness.provider.calls
        accepted = await _accept(harness, proposal.id)
        assert accepted.status_code == 200, accepted.text
        assert harness.provider.calls == calls_before_accept

    head = await harness.service.get_current_state(harness.session_id)
    assert head.id != harness.seed_state_id
    head_state = state_from_record(head)
    rewired = next(node for node in head_state.nodes if node.id == NODE)
    assert rewired.input == "inbound"
    assert rewired.options["prompt_template"] == stored.options["prompt_template"]
    assert _state_sites(head_state) == {SHIELD_SITE}
    cards = await _pending_cards(harness)
    assert _card_sites(cards) == [SHIELD_SITE], "the accepted rewire re-opened the shield review but has no pending review card"
    assert cards[0].composition_state_id == head.id
    assert cards[0].llm_draft == SHIELD_DRAFT


def _edit_against(state_id: UUID, text: str, seen: dict[str, Any], call_id: str) -> Script:
    """One ``change_substring`` naming ``state_id`` as the inspected state; records the tool result."""

    def script(_state: CompositionState, _head_id: UUID) -> tuple[Any, ...]:
        def respond(messages: list[dict[str, Any]]) -> Any:
            seen["payload"] = _last_tool_payload(messages)
            return _make_llm_response(content="Edited.")

        return (
            _call(
                "change_substring",
                {
                    "target": {"kind": "node_option", "node_id": NODE, "option": "prompt_template"},
                    "expected_sha256": sha256(text.encode("utf-8")).hexdigest(),
                    "expected_state_id": str(state_id),
                    "edits": [{"old": "Assess", "replacement": "Rate", "replace_all": False}],
                },
                call_id,
            ),
            respond,
        )

    return script


@pytest.mark.asyncio
async def test_opted_out_accept_appends_the_auto_resolved_head_and_names_the_accepted_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under opt-out the accept's own transaction auto-resolves the owed card and appends a derived head.

    As in ``set_pipeline`` settlement, ``committed_state_id`` names the state
    the proposal committed; the head is one version later, derived from it.
    Clients take the head from the state read: an edit against the accepted
    state is refused as changed, and an edit against the real head is staged
    and accepts.
    """
    harness = await _harness(tmp_path, monkeypatch, "explicit_approve")
    async with acquire_compose_context(harness.service, harness.session_id) as context:
        await harness.service.record_session_interpretation_opt_out(
            session_id=harness.session_id, actor="user:alice", session_operation_context=context
        )
    await _compose_turn(harness, _change_substring)
    proposal = await _only_pending_proposal(harness, "change_substring")

    accepted = await _accept(harness, proposal.id)

    assert accepted.status_code == 200, accepted.text
    reported = UUID(accepted.json()["committed_state_id"])
    # (a) The accept appended the auto-resolved head, derived from the accepted state.
    versions = await harness.service.get_state_versions(harness.session_id)
    assert (versions[0].id, versions[0].version) == (harness.seed_state_id, 1)
    assert [(state.version, state.derived_from_state_id) for state in versions[1:]] == [(2, harness.seed_state_id), (3, reported)]
    assert versions[1].id == reported
    # (b) The response names the accepted state; the head read returns the derived version.
    head = await harness.service.get_current_state(harness.session_id)
    assert (head.id, head.version) == (versions[2].id, 3)
    assert head.id != reported
    # The card was auto-resolved against the accepted state, and nothing is left pending.
    events = await harness.service.list_interpretation_events(harness.session_id)
    assert [(event.kind, event.choice, event.composition_state_id) for event in events if event.kind is not None] == [
        (InterpretationKind.LLM_PROMPT_TEMPLATE, InterpretationChoice.OPTED_OUT, reported)
    ]
    assert await _pending_cards(harness) == []

    # (c) An edit that names the reported state as its inspected state is refused as changed.
    text = state_from_record(head).nodes[0].options["prompt_template"]
    assert state_from_record(await harness.service.get_state(reported)).nodes[0].options["prompt_template"] == text
    stale: dict[str, Any] = {}
    await _compose_turn(harness, _edit_against(reported, text, stale, "edit-stale"), "Edit it again")
    assert stale["payload"]["success"] is False
    assert [error["error_code"] for error in stale["payload"]["validation"]["errors"]] == ["target_changed"]
    assert await harness.service.list_composition_proposals(harness.session_id, status="pending") == []
    # The same edit against the real head is staged and accepts.
    fresh: dict[str, Any] = {}
    await _compose_turn(harness, _edit_against(head.id, text, fresh, "edit-head"), "Edit it again")
    assert fresh["payload"]["data"]["status"] == "APPROVAL_REQUIRED", fresh["payload"]
    second = await _only_pending_proposal(harness, "change_substring")
    assert second.base_state_id == head.id
    again = await _accept(harness, second.id)
    assert again.status_code == 200, again.text
    final = await harness.service.get_current_state(harness.session_id)
    assert final.version > head.version
    assert "Rate" in state_from_record(final).nodes[0].options["prompt_template"]


@pytest.mark.asyncio
async def test_two_owed_reviews_get_two_cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control for the tolerance cases: with nothing refused, every owed site gets its card."""
    harness = await _harness(tmp_path, monkeypatch, "explicit_approve")
    await _compose_turn(harness, _patch_prompt_and_model(_assess_prompt_text))
    proposal = await _only_pending_proposal(harness, "patch_node_options")

    accepted = await _accept(harness, proposal.id)

    assert accepted.status_code == 200, accepted.text
    head = await harness.service.get_current_state(harness.session_id)
    assert _state_sites(state_from_record(head)) == {PROMPT_SITE, MODEL_SITE}
    cards = await _pending_cards(harness)
    assert _card_sites(cards) == [PROMPT_SITE, MODEL_SITE]
    assert {card.composition_state_id for card in cards} == {head.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("trust_mode", ["explicit_approve", "auto_commit"])
async def test_unsurfaceable_site_is_left_out_as_the_surfacer_leaves_it_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trust_mode: str
) -> None:
    """A site the backend cannot turn into a card never fails the accept.

    The seed already owes such a review, unrelated to the proposal. Both
    modes end in the same place: the prompt card written, nothing for the
    unsurfaceable site.
    """
    harness = await _harness(tmp_path, monkeypatch, trust_mode, _seed_legacy_vague_placeholder)
    assert _state_sites(harness.seed_state) == {LEGACY_SITE}
    assert unsurfaceable_pending_interpretation_review_sites(harness.seed_state) != ()
    await _compose_turn(harness, _change_substring)
    if trust_mode == "explicit_approve":
        proposal = await _only_pending_proposal(harness, "change_substring")
        accepted = await _accept(harness, proposal.id)
        assert accepted.status_code == 200, accepted.text

    head = await harness.service.get_current_state(harness.session_id)
    assert head.id != harness.seed_state_id
    head_state = state_from_record(head)
    assert _state_sites(head_state) == {PROMPT_SITE, LEGACY_SITE}
    assert {(site.component_id, site.user_term, site.kind) for site in unsurfaceable_pending_interpretation_review_sites(head_state)} == {
        LEGACY_SITE
    }
    cards = await _pending_cards(harness)
    assert _card_sites(cards) == [PROMPT_SITE]
    assert cards[0].composition_state_id == head.id


@pytest.mark.asyncio
async def test_skipping_prepare_leaves_out_exactly_the_unsurfaceable_sites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One predicate decides both: what the cohort leaves out is what the surfacer cannot eventize."""
    harness = await _harness(tmp_path, monkeypatch, "explicit_approve", _seed_legacy_vague_placeholder)
    await _compose_turn(harness, _change_substring)
    proposal = await _only_pending_proposal(harness, "change_substring")
    assert (await _accept(harness, proposal.id)).status_code == 200
    state = state_from_record(await harness.service.get_current_state(harness.session_id))
    surfacing = harness.app.state.interpretation_surfacing
    unsurfaceable = {(site.component_id, site.user_term, site.kind) for site in unsurfaceable_pending_interpretation_review_sites(state)}
    assert unsurfaceable == {LEGACY_SITE}

    drafts = surfacing.prepare_surfaceable_pending_interpretation_reviews(state)

    assert {(draft.affected_node_id, draft.user_term, draft.kind) for draft in drafts} == _state_sites(state) - unsurfaceable
    # The settlement cohort keeps refusing the same state outright.
    with pytest.raises(InvariantError):
        surfacing.prepare_pending_interpretation_reviews(state)


@pytest.mark.asyncio
async def test_oversize_prompt_file_commits_the_head_without_its_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A prompt file over the inline cap can never run, so no card is owed for it.

    The accept still commits, and the other owed site gets its card. The
    oversize site is the one head this change still leaves owing a review with
    no card, so the test pins what protects the user there: the run gate and
    runtime validation both refuse, no later surfacing pass raises a card
    while the file stays over the cap, and re-pointing the prompt at a file
    under the cap raises the card with that accept.
    """
    harness = await _harness(tmp_path, monkeypatch, "explicit_approve")
    content = b"x" * (BLOB_INLINE_PER_REF_BYTE_CAP + 1)
    oversize = await _upload_prompt_file(harness, content)
    marker = {"blob_ref": str(oversize), "mode": "inline_content", "sha256": sha256(content).hexdigest()}
    await _compose_turn(harness, _patch_prompt_and_model(marker))
    proposal = await _only_pending_proposal(harness, "patch_node_options")

    with structlog.testing.capture_logs() as events:
        accepted = await _accept(harness, proposal.id)

    assert accepted.status_code == 200, accepted.text
    head = await harness.service.get_current_state(harness.session_id)
    assert str(head.id) == accepted.json()["committed_state_id"]
    head_state = state_from_record(head)
    assert _state_sites(head_state) == {PROMPT_SITE, MODEL_SITE}
    cards = await _pending_cards(harness)
    assert _card_sites(cards) == [MODEL_SITE]
    assert cards[0].composition_state_id == head.id
    assert [(line["kind"], line["error_class"], line["composition_state_id"]) for line in _skip_lines(events)] == [
        (InterpretationKind.LLM_PROMPT_TEMPLATE.value, "PromptFileTooLargeError", str(head.id))
    ]

    # Run refuses: the execution gate still reports the prompt review as owed.
    gate = materialize_state_for_execution(head_state)
    assert type(gate) is InterpretationReviewPending
    assert PROMPT_SITE in {(site.component_id, site.user_term, site.kind) for site in gate.sites}
    # Validate refuses. What the user's validation names is the owed review,
    # not the file size; the size refusal is what the planner's preflight sees.
    validation = await _runtime_validation(harness, past_pending_reviews=False)
    assert validation.is_valid is False
    assert {(error.error_code, error.component_id) for error in validation.errors} == {("interpretation_review_pending", NODE)}
    assert any(PROMPT_TERM in error.message for error in validation.errors)
    planner_view = await _runtime_validation(harness, past_pending_reviews=True)
    assert planner_view.is_valid is False
    assert [(error.error_code, error.component_id) for error in planner_view.errors] == [("oversized_inline_blob_content", NODE)]
    assert str(BLOB_INLINE_PER_REF_BYTE_CAP) in planner_view.errors[0].message

    # No later pass raises the card while the file is over the cap: this is
    # the repair pass POST /validate runs before it validates.
    async with acquire_compose_context(harness.service, harness.session_id) as repair_context:
        await harness.app.state.interpretation_surfacing.surface_pending_interpretation_reviews(
            head_state,
            session_id=str(harness.session_id),
            current_state_id=str(head.id),
            only_missing_evidence=True,
            session_operation_context=repair_context,
        )
    assert _card_sites(await _pending_cards(harness)) == [MODEL_SITE]

    # The way out: a prompt file under the cap. Its accept raises the card.
    small = await _upload_prompt_file(harness, FILE_TEXT.encode("utf-8"))
    await _compose_turn(harness, _wire_existing_file(small, "edit-2"), "Use the shorter prompt file")
    replacement = await _only_pending_proposal(harness, "wire_blob_inline_ref")
    replaced = await _accept(harness, replacement.id)
    assert replaced.status_code == 200, replaced.text
    final = await harness.service.get_current_state(harness.session_id)
    final_cards = await _pending_cards(harness)
    assert sorted(_card_sites(final_cards)) == sorted([PROMPT_SITE, MODEL_SITE])
    prompt_card = next(card for card in final_cards if card.kind is InterpretationKind.LLM_PROMPT_TEMPLATE)
    assert prompt_card.composition_state_id == final.id
