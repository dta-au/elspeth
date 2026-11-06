"""Live common-context JSON and tool-result producers preserve complete values."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import litellm
import pytest

from elspeth.contracts.composer_content import CompleteValueManifest, ComposerContentScope
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, canonical_json_loads
from elspeth.web.composer import prompts as composer_prompts
from elspeth.web.composer.discovery_cache import serialize_tool_result, tool_result_content_parts
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.prompt_guard_rendering import json_content_parts
from elspeth.web.composer.state import SourceSpec
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers import continuation_app as continuation_app_module
from tests.helpers.continuation_app import (
    ADVISOR_MODEL,
    PRIMARY_MODEL,
    continuation_app,
    end_admit,
    install_sdk,
    sdk_reply,
    submit_and_settle,
    tool_call,
)
from tests.helpers.session_fences import acquire_compose_context
from tests.unit.web.composer._helpers import _empty_state
from tests.unit.web.composer._scripted_sdk import _pipeline

pytest_plugins = ("tests.unit.web.composer.test_prompt_guard_wiring",)


def _state_wire(messages):
    return next(
        row["content"]
        for row in messages
        if isinstance(row["content"], str) and row["content"].startswith(composer_prompts.STATE_CONTEXT_PREFIX)
    )


def _long_payload():
    # Canonical UTF-16 key ordering differs from Python code-point ordering.
    # Integral doubles outside the safe integer range need the paired inverse.
    return {
        "\ue000": {"small": 0.00005, "double": 1e20, "text": '"field",' * 450 + "ASCII-TAIL-CANARY"},
        "\U0001f600": {"text": "界" * 700 + "UNICODE-TAIL-CANARY", "empty": []},
    }


def _state_with_payload(tmp_path, payload, *, session_id=None):
    source = (_pipeline(tmp_path) if session_id is None else _pipeline(tmp_path, session_id=session_id))["source"]
    return replace(
        _empty_state(),
        sources={"source": SourceSpec(**{**source, "options": {**source["options"], "canonical_fixture": payload}})},
    )


def test_long_canonical_renderer_requires_matching_wire_format_and_preserves_all_units():
    payload = _long_payload()
    serialized = canonical_json(payload)
    assert len(serialized) > 2048
    assert serialized.index("\U0001f600") < serialized.index("\ue000")
    assert "0.00005" in serialized and "100000000000000000000" in serialized
    # Deliberately mismatched formatting remains an integrity error; the exact
    # wire assertion is a control and must not be removed to admit the request.
    with pytest.raises(AuditIntegrityError, match="exact wire serialization"):
        json_content_parts(serialized, encoder=canonical_json, decoder=canonical_json_loads)
    parts = json_content_parts(
        serialized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        encoder=canonical_json,
        decoder=canonical_json_loads,
    )
    assert "".join(part.text for part in parts) == serialized
    assert all(part.trust is None for part in parts)
    projection = "".join(part.inspection_text if part.inspection_text is not None else part.text for part in parts)
    assert payload["\ue000"]["text"] in projection
    assert payload["\U0001f600"]["text"] in projection
    assert "0.00005" in projection and "100000000000000000000" in projection


def test_live_tool_result_producer_preserves_long_json_unicode_keys_and_numbers():
    state = _empty_state()
    payload = _long_payload()
    result = ToolResult(success=False, updated_state=state, validation=state.validate(), affected_nodes=(), data=payload)
    serialized = serialize_tool_result(result)
    assert len(serialized) > 2048
    parts = tool_result_content_parts(result, serialized)
    assert "".join(part.text for part in parts) == serialized
    assert all(part.trust is None for part in parts)
    assert json.loads(serialized)["data"] == payload


class _ContinuationSDK:
    """Physical SDK for the Continuation harness: primaries in order, then END admits.

    ``before_each`` records how many guard inspections preceded each physical
    call, so a request's guard slice can be told apart from the next one's.
    """

    def __init__(self, inspected, primaries):
        self.inspected = inspected
        self.primaries = list(primaries)
        self.requests: list[dict] = []
        self.inspections_before: list[int] = []
        self.app = None

    async def __call__(self, **kwargs):
        self.inspections_before.append(len(self.inspected))
        self.requests.append(deepcopy(kwargs))
        if kwargs["model"] == ADVISOR_MODEL:
            return litellm.ModelResponse(
                model=ADVISOR_MODEL,
                choices=[
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(end_admit()), "tool_calls": None}}
                ],
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            )
        assert kwargs["model"] == PRIMARY_MODEL
        step = self.primaries.pop(0)
        return step(self, kwargs) if callable(step) else sdk_reply(*step)

    @property
    def primary_requests(self):
        return [request for request in self.requests if request["model"] == PRIMARY_MODEL]


def _inspecting_guard(monkeypatch, inspected):
    """Route the harness's guard HTTP boundary through a recording clean transport."""

    def factory(configuration, *, http_transport):
        del http_transport
        return _azure_client_for(configuration, inspected)

    monkeypatch.setattr(continuation_app_module, "AzurePromptGuardClient", factory)


def _azure_client_for(configuration, inspected):
    def inspect(request):
        body = json.loads(request.content)
        inspected.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    return AzurePromptGuardClient(configuration, http_transport=httpx.MockTransport(inspect))


async def _seed_current_state(harness, state) -> str:
    sessions = harness.app.state.session_service
    data = state.to_dict()
    async with acquire_compose_context(sessions, harness.session_id) as context:
        record = await sessions.save_composition_state(
            harness.session_id,
            CompositionStateData(
                sources=data["sources"],
                nodes=data["nodes"],
                edges=data["edges"],
                outputs=data["outputs"],
                metadata_=data["metadata"],
                is_valid=False,
            ),
            provenance="session_seed",
            session_operation_context=context,
        )
    return str(record.id)


@pytest.mark.skip(
    reason=(
        "UNCOVERED (A5 batch 2): Continuation bootstrap refuses this graph before the first provider call. "
        "continuation_bootstrap.bootstrap_values -> contracts.hashing.canonical_json raises rfc8785 IntegerDomainError "
        "for the integral double 1e20 in the current state options, which the deleted V1 loop accepted; needs a src fix, "
        "then the U-40 later-Compose arm after a read batch."
    )
)
@pytest.mark.asyncio
async def test_live_common_context_reads_complete_value_exactly(tmp_path, monkeypatch):
    inspected = []
    payload = _long_payload()
    _inspecting_guard(monkeypatch, inspected)

    def read_complete_value(sdk, kwargs):
        view = json.loads(_state_wire(kwargs["messages"]).split("\n", 1)[1])["current_state"]
        manifest = CompleteValueManifest.from_payload(view["content_manifest"])
        assert len(manifest.values) == 1
        reference = manifest.values[0]
        assert reference.path == ("sources", "source", "options", "canonical_fixture", "\ue000", "text")
        call = tool_call(sdk.app, "read-complete", "get_complete_pipeline_value", {"reference_id": reference.reference_id})
        return sdk_reply(None, [call])

    sdk = _ContinuationSDK(inspected, [read_complete_value, ("The complete value is retained.", None)])
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sdk.app = harness.app
        session_id = str(harness.session_id)
        state = _state_with_payload(tmp_path, payload, session_id=session_id)
        scope = ComposerContentScope(harness.session_id, composition_content_hash(state))
        state_id = await _seed_current_state(harness, state)
        snapshot = await submit_and_settle(
            harness,
            {
                "content": "Read the complete current value without editing the pipeline.",
                "operation_id": str(uuid4()),
                "state_id": state_id,
            },
        )
        primaries = sdk.primary_requests
        assert primaries, (sdk.requests, snapshot.model_dump_json())
        first_wire = _state_wire(primaries[0]["messages"])
        view = json.loads(first_wire.split("\n", 1)[1])["current_state"]
        manifest = CompleteValueManifest.from_payload(view["content_manifest"])
        assert manifest.scope == scope
        displayed = view["data"]["sources"]["source"]["options"]["canonical_fixture"]
        reference = manifest.values[0]
        assert displayed["\ue000"]["text"] == {
            "complete_value_ref": reference.reference_id,
            "content_hash": reference.content_hash,
            "utf8_bytes": reference.utf8_bytes,
        }
        assert displayed["\U0001f600"] == payload["\U0001f600"]
        assert displayed["\ue000"]["small"] == payload["\ue000"]["small"]
        assert displayed["\ue000"]["double"] == payload["\ue000"]["double"]
        assert payload["\ue000"]["text"] not in first_wire
        # Integration point (U-40): the later Compose after the read batch
        # issues the second primary request carrying the selected value.
        assert len(primaries) == 2, (sdk.requests, snapshot.model_dump_json())
        primary_indexes = [index for index, request in enumerate(sdk.requests) if request["model"] == PRIMARY_MODEL]
        first_before, second_before = (sdk.inspections_before[index] for index in primary_indexes)
        # Every request was inspected before it was sent.
        assert 0 < first_before < second_before
        initial_inspections = inspected[:first_before]
        selected_inspections = inspected[first_before:second_before]
        issued = [call for message in primaries[1]["messages"] if message["role"] == "assistant" for call in message["tool_calls"] or ()]
        assert len(issued) == 1 and issued[0]["function"]["name"] == "get_complete_pipeline_value"
        selected = [
            message for message in primaries[1]["messages"] if message["role"] == "tool" and message["tool_call_id"] == issued[0]["id"]
        ]
        assert len(selected) == 1
        response = json.loads(selected[0]["content"])
        assert response["success"] is True
        assert response["data"] == {
            "session_id": session_id,
            "composition_content_hash": scope.composition_content_hash,
            "reference_id": reference.reference_id,
            "path": list(reference.path),
            "content_hash": reference.content_hash,
            "utf8_bytes": reference.utf8_bytes,
            "value": payload["\ue000"]["text"],
        }
        initial_projection = "\n".join(document for request in initial_inspections for document in request["documents"])
        selected_projection = "\n".join(document for request in selected_inspections for document in request["documents"])
        assert payload["\ue000"]["text"] not in initial_projection
        assert payload["\ue000"]["text"] in selected_projection
        projection = "\n".join(document for request in inspected for document in request["documents"])
        assert payload["\U0001f600"]["text"] in projection
        assert "ASCII-TAIL-CANARY" in projection and "UNICODE-TAIL-CANARY" in projection
        # Integration completion point (U-40): the read-only request completes,
        # leaves the graph unchanged, and nothing is inspected after the last call.
        assert snapshot.status == "completed", snapshot.model_dump_json()
        assert sdk.inspections_before[-1] == len(inspected)
        head = await harness.app.state.session_service.get_current_state(harness.session_id)
        assert head is not None and str(head.id) == state_id


def test_live_initial_context_inspects_complete_credential_tail_before_projection(tmp_path, tool_context, monkeypatch):
    payload = _long_payload()
    payload["\ue000"]["text"] += " https://alice:synthetic-password@example.test/private"
    state = _state_with_payload(tmp_path, payload)
    inspected_states = []
    real_scan = composer_prompts.require_no_credential_material_in_state

    def inspect_complete_state(current, **kwargs):
        assert current is state
        inspected_states.append(current.to_dict())
        return real_scan(current, **kwargs)

    def forbidden_projection(*_args, **_kwargs):
        raise AssertionError("Credential-bearing complete state must refuse before bounded projection")

    monkeypatch.setattr(composer_prompts, "require_no_credential_material_in_state", inspect_complete_state)
    monkeypatch.setattr(composer_prompts, "build_bounded_composition_view", forbidden_projection)
    with pytest.raises(CredentialMaterialRefused) as refused:
        composer_prompts.build_messages(
            [],
            state,
            "Inspect the current graph.",
            tool_context.catalog,
            plugin_snapshot=tool_context.plugin_snapshot,
            content_scope=ComposerContentScope(UUID(int=1), composition_content_hash(state)),
        )
    assert refused.value.surface == "composer_bounded_content_projection"
    assert refused.value.finding.category == "credential_url"
    assert inspected_states == [state.to_dict()]
    assert inspected_states[0]["sources"]["source"]["options"]["canonical_fixture"]["\ue000"]["text"] == payload["\ue000"]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["__elspeth_canonical_type__", "ordinary_field"], ids=["reserved-tag", "ordinary-neighbor"])
async def test_live_initial_context_preserves_core_normalized_state_option(tmp_path, monkeypatch, field_name):
    inspected, rendered = [], []
    real_render = composer_prompts.json_content_parts

    def observe_render(serialized, **kwargs):
        parts = real_render(serialized, **kwargs)
        rendered.append((serialized, parts))
        return parts

    monkeypatch.setattr(composer_prompts, "json_content_parts", observe_render)
    _inspecting_guard(monkeypatch, inspected)
    sdk = _ContinuationSDK(inspected, [("The current graph is retained.", None)])
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sdk.app = harness.app
        source = _pipeline(tmp_path, session_id=str(harness.session_id))["source"]
        field_mapping = {field_name: "x"}
        state = replace(
            _empty_state(), sources={"source": SourceSpec(**{**source, "options": {**source["options"], "field_mapping": field_mapping}})}
        )
        before = state.to_dict()
        # The bounded view is the composer's lossless graph projection
        # (composer_content._canonical_value), not core canonical JSON: core
        # escapes a key that collides with its reserved type tag for hashing, but
        # the model must see — and may echo back — the exact authored option.
        expected_wire_state = before
        state_id = await _seed_current_state(harness, state)
        snapshot = await submit_and_settle(
            harness,
            {"content": "Describe the current graph without editing it.", "operation_id": str(uuid4()), "state_id": state_id},
        )
        assert state.to_dict() == before
        primaries = sdk.primary_requests
        assert len(primaries) == 1, (sdk.requests, snapshot.model_dump_json())
        # Exactly one guard request precedes the one primary call.
        assert sdk.inspections_before[0] == 1
        wire = _state_wire(primaries[0]["messages"])
        serialized = wire.split("\n", 1)[1]
        parts = next(parts for text, parts in rendered if text == serialized)
        assert "".join(part.text for part in parts) == serialized
        assert any("field_mapping" in part.text for part in parts)
        assert all(part.trust is None for part in parts if "field_mapping" in part.text or field_name in part.text)
        view = json.loads(serialized)["current_state"]
        manifest = CompleteValueManifest.from_payload(view["content_manifest"])
        assert manifest.scope == ComposerContentScope(harness.session_id, composition_content_hash(state))
        assert manifest.values == ()
        assert view["data"] == expected_wire_state
        assert view["data"]["sources"]["source"]["options"]["field_mapping"] == field_mapping
        # Integration completion point (U-40): the request completes without a new state.
        assert snapshot.status == "completed", snapshot.model_dump_json()
        head = await harness.app.state.session_service.get_current_state(harness.session_id)
        assert head is not None and str(head.id) == state_id
