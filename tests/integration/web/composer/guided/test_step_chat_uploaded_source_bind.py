"""Planner-authored upload references retain confirmation and atomic custody."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.sessions.routes.composer import guided as guided_route
from elspeth.web.sessions.routes.composer import guided_chat_atomic
from tests.integration.web.composer.guided.test_step_chat import (
    _CHAT_SOLVER_ACOMPLETION,
    _create_session,
    _fake_llm_reply,
    _fake_resolve_source_response_csv,
    _fake_source_resolution_tool_call,
    _ReturningLiteLLMCompletion,
    _SequencedLiteLLMCompletion,
)
from tests.integration.web.composer.guided.test_wrong_stage_intent import _PAIR_RETAIN_ARGUMENTS
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient

_UPLOAD_SENTINEL = 'I\'ve uploaded "{filename}"; please use it as the pipeline input.'


def _chat_body(turn: dict, message: str) -> dict[str, str]:
    return {
        "operation_id": str(uuid4()),
        "turn_token": turn["turn_token"],
        "message": message,
    }


def _upload_inline_blob(
    client: TestClient,
    session_id: str,
    *,
    filename: str,
    content: str,
    mime_type: str,
) -> str:
    resp = client.post(
        f"/api/sessions/{session_id}/blobs/inline",
        json={"filename": filename, "content": content, "mime_type": mime_type},
    )
    assert resp.status_code == 201, resp.json()
    return resp.json()["id"]


def _upload_inventory_csv(client: TestClient, session_id: str, *, filename: str = "inventory.csv") -> str:
    return _upload_inline_blob(
        client,
        session_id,
        filename=filename,
        content="sku,quantity\nSKU-001,2\n",
        mime_type="text/csv",
    )


def _guided(client: TestClient, session_id: str) -> dict:
    resp = client.get(f"/api/sessions/{session_id}/guided")
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _resolve_upload(
    monkeypatch: pytest.MonkeyPatch, blob_id: str, *, options: dict | None = None, plugin: str = "csv"
) -> _ReturningLiteLLMCompletion:
    """Exercise the real solver, parser and route with provider-authored options."""
    completion = _ReturningLiteLLMCompletion(
        _fake_source_resolution_tool_call(
            {
                "upload_ref": blob_id,
                "plugin": plugin,
                "options": options if options is not None else {"schema": {"mode": "observed", "guaranteed_fields": ["sku", "quantity"]}},
                "on_validation_failure": "discard",
                "assistant_message": "I propose inventory.csv as the source; please review its columns.",
            }
        )
    )
    completion.response.choices[0].message.tool_calls[0].id = "resolve-upload"
    monkeypatch.setattr(_CHAT_SOLVER_ACOMPLETION, completion)
    return completion


def _post_respond(client: TestClient, session_id: str, **kwargs: object) -> dict:
    turn = _guided(client, session_id)["next_turn"]
    assert turn is not None
    body: dict[str, object] = {"operation_id": str(uuid4()), "turn_token": turn["turn_token"]}
    body.update(kwargs)
    resp = client.post(f"/api/sessions/{session_id}/guided/respond", json=body)
    assert resp.status_code == 200, resp.json()
    return resp.json()


def test_upload_sentinel_after_typed_prose_binds_at_the_source_selection_turn(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model authors the pending source; ordinary confirmation commits it."""
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    completion = _resolve_upload(monkeypatch, blob_id)
    initial_turn = _guided(client, session_id)["next_turn"]
    assert initial_turn["type"] == "single_select"

    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(
            initial_turn,
            "Please use this one.\n" + _UPLOAD_SENTINEL.format(filename="inventory.csv"),
        ),
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(completion.calls) == 1
    provider_input = json.dumps(completion.calls[0]["messages"])
    assert blob_id in provider_input
    assert "sku" in provider_input and "quantity" in provider_input
    assert "SKU-001" not in provider_input
    assert body["next_turn"]["type"] == "inspect_and_confirm"
    assert body["next_turn"]["payload"]["observed"]["columns"] == ["sku", "quantity"]
    assert body["composition_state"]["sources"] == {}
    assert body["assistant_message_kind"] == "assistant"
    assert "inventory.csv" in body["guided_session"]["chat_history"][-1]["content"]

    # The review card is the authoritative durable turn, and confirming it
    # through the ordinary wizard control is what finally binds the source.
    refreshed = _guided(client, session_id)
    assert refreshed["next_turn"] == body["next_turn"]
    assert refreshed["composition_state"]["sources"] == {}
    confirmed = _post_respond(client, session_id, edited_values={"columns": ["sku", "quantity"]})
    assert confirmed["next_turn"]["type"] == "review_components"
    reviewed = confirmed["composition_state"]["composer_meta"]["guided_session"]["reviewed_sources"]
    assert len(reviewed) == 1
    reviewed_source = next(iter(reviewed.values()))
    assert reviewed_source["plugin"] == "csv"
    assert reviewed_source["options"]["path"] == f"blob:{blob_id}"
    assert reviewed_source["observed_columns"] == ["sku", "quantity"]
    assert len(completion.calls) == 1


def test_upload_sentinel_bind_replays_the_same_operation_without_a_second_cohort(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retried operation_id replays the settled bind, byte for byte.

    The bind is the only guided-chat settlement that emits AND answers TWO turn
    occurrences (the selection it arrived on plus the schema form it answered on
    the user's behalf), so its replay descriptor and five-payload cohort need
    their own proof.
    """
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    completion = _resolve_upload(monkeypatch, blob_id)
    body = _chat_body(_guided(client, session_id)["next_turn"], _UPLOAD_SENTINEL.format(filename="inventory.csv"))

    first = client.post(f"/api/sessions/{session_id}/guided/chat", json=body)
    assert first.status_code == 200, first.json()
    settled = first.json()
    history = settled["guided_session"]["history"]
    assert [record["turn_type"] for record in history] == ["single_select", "schema_form", "inspect_and_confirm"]
    assert [record["response_hash"] is None for record in history] == [False, False, True]
    assert len(completion.calls) == 1

    replay = client.post(f"/api/sessions/{session_id}/guided/chat", json=body)

    assert replay.status_code == 200, replay.json()
    assert replay.json() == settled
    assert _guided(client, session_id)["guided_session"]["history"] == history
    assert len(completion.calls) == 1


def test_upload_sentinel_binds_the_built_source_at_the_schema_form(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The matching-upload resolution is consumed, not silently discarded.

    Selecting CSV while the upload is ready captures the blob's inspection
    facts on the pending intent, so answering the form from the sentinel lands
    on the same confirmation-gated review card instead of committing a source.
    """
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    form = _post_respond(client, session_id, chosen=["csv"])["next_turn"]
    assert form["type"] == "schema_form"
    assert form["payload"]["prefilled"]["path"].startswith("blob:")
    completion = _resolve_upload(monkeypatch, blob_id)

    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(form, _UPLOAD_SENTINEL.format(filename="inventory.csv")),
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(completion.calls) == 1
    assert body["next_turn"]["type"] == "inspect_and_confirm"
    assert body["next_turn"]["payload"]["observed"]["columns"] == ["sku", "quantity"]
    assert body["composition_state"]["sources"] == {}
    assert body["assistant_message_kind"] == "assistant"


def test_upload_sentinel_matcher_accepts_a_trailing_line_and_rejects_embedded_prose() -> None:
    """The sentinel is the trailing line, never a whole-message prefix."""
    sentinel = _UPLOAD_SENTINEL.format(filename="inventory.csv")
    assert guided_route._step_1_uploaded_input_filename(sentinel) == "inventory.csv"
    assert guided_route._step_1_uploaded_input_filename(f"use this\n{sentinel}") == "inventory.csv"
    assert guided_route._step_1_uploaded_input_filename(f"use this\n{sentinel}\n") == "inventory.csv"
    assert guided_route._step_1_uploaded_input_filename(f"{sentinel}\nand rename the columns") is None
    assert guided_route._step_1_uploaded_input_filename('Use the uploaded "inventory.csv" file.') is None
    assert guided_route._step_1_uploaded_input_filename(_UPLOAD_SENTINEL.format(filename="")) is None
    assert guided_route._step_1_uploaded_input_filename(_UPLOAD_SENTINEL.format(filename='a"b')) is None


@pytest.mark.parametrize("at_form", [False, True])
def test_provider_options_survive_pending_review_and_confirmation(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch, at_form: bool
) -> None:
    """A successful token call followed by server prefill cannot satisfy this."""
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    initial_turn = _post_respond(client, session_id, chosen=["csv"])["next_turn"] if at_form else _guided(client, session_id)["next_turn"]
    authored = {"schema": {"mode": "observed", "guaranteed_fields": ["sku"]}, "encoding": "utf-8-sig"}
    completion = _resolve_upload(monkeypatch, blob_id, options=authored)
    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(initial_turn, _UPLOAD_SENTINEL.format(filename="inventory.csv")),
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(completion.calls) == 1
    assert body["next_turn"]["type"] == "inspect_and_confirm"
    pending_guided = body["composition_state"]["composer_meta"]["guided_session"]
    (pending,) = pending_guided["pending_source_intents"].values()
    assert pending["plugin"] == "csv"
    assert pending["options"]["encoding"] == authored["encoding"]
    assert pending["options"]["schema"] == authored["schema"]
    (form_record,) = [record for record in pending_guided["history"] if record["turn_type"] == "schema_form"]
    form_response = json.loads(client.app.state.payload_store.retrieve(form_record["response_hash"]))["payload"]
    assert form_response["edited_values"]["plugin"] == "csv"
    assert form_response["edited_values"]["options"] == {
        **authored,
        "path": f"blob:{blob_id}",
        "on_validation_failure": "discard",
    }
    assert pending_guided["reviewed_sources"] == {}
    assert body["composition_state"]["sources"] == {}
    refreshed = _guided(client, session_id)
    assert (
        refreshed["composition_state"]["composer_meta"]["guided_session"]["pending_source_intents"]
        == pending_guided["pending_source_intents"]
    )
    confirmed = _post_respond(client, session_id, edited_values={"columns": ["sku", "quantity"]})
    (source,) = confirmed["composition_state"]["composer_meta"]["guided_session"]["reviewed_sources"].values()
    assert source["options"]["encoding"] == authored["encoding"]
    assert source["options"]["schema"] == authored["schema"]
    assert source["options"]["path"] == f"blob:{blob_id}"


@pytest.mark.parametrize("at_form", [False, True])
@pytest.mark.parametrize("retain_later", [False, True])
def test_invalid_uploaded_config_is_corrected_by_provider_before_binding(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch, at_form: bool, retain_later: bool
) -> None:
    """The recorded invalid schema must reach a retry, never server repair."""
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    initial = _post_respond(client, session_id, chosen=["csv"])["next_turn"] if at_form else _guided(client, session_id)["next_turn"]
    invalid = {"schema": {"mode": "observed", "fields": {"sku": "str", "quantity": "int"}}, "encoding": "utf-8-sig"}
    authored = {"schema": {"mode": "flexible", "fields": ["sku: str", "quantity: int"]}, "encoding": "utf-8-sig"}
    replies = [_resolve_upload(monkeypatch, blob_id, options=options).response for options in (invalid, authored)]
    if retain_later:
        for reply in replies:
            reply.choices[0].message.tool_calls.append(
                SimpleNamespace(
                    id="retain-later-transform",
                    function=SimpleNamespace(name="retain_deferred_intent", arguments=json.dumps(_PAIR_RETAIN_ARGUMENTS)),
                )
            )
    completion = _SequencedLiteLLMCompletion(replies)
    monkeypatch.setattr(_CHAT_SOLVER_ACOMPLETION, completion)
    request = _chat_body(initial, _UPLOAD_SENTINEL.format(filename="inventory.csv"))
    response = client.post(f"/api/sessions/{session_id}/guided/chat", json=request)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(completion.calls) == 2
    assert body["next_turn"]["type"] == "inspect_and_confirm"
    assert body["next_turn"]["payload"]["observed"]["columns"] == ["sku", "quantity"]
    expected_options = {**authored, "path": f"blob:{blob_id}", "on_validation_failure": "discard"}
    configured_options = {**expected_options, "columns": None, "delimiter": ",", "field_mapping": None, "skip_rows": 0}
    guided = body["composition_state"]["composer_meta"]["guided_session"]
    if retain_later:
        (intent,) = guided["deferred_intents"]
        assert intent["target_stage"] == "topology"
    else:
        assert guided["deferred_intents"] == []
    (pending,) = guided["pending_source_intents"].values()
    assert pending["options"] == configured_options
    (form_record,) = [record for record in guided["history"] if record["turn_type"] == "schema_form"]
    form_response = json.loads(client.app.state.payload_store.retrieve(form_record["response_hash"]))["payload"]
    assert form_response["edited_values"] == {"plugin": "csv", "options": expected_options}
    assert guided["reviewed_sources"] == {}
    assert body["composition_state"]["sources"] == {}
    refreshed = _guided(client, session_id)
    assert refreshed["next_turn"] == body["next_turn"]
    assert refreshed["composition_state"]["composer_meta"]["guided_session"]["pending_source_intents"] == guided["pending_source_intents"]
    replay = client.post(f"/api/sessions/{session_id}/guided/chat", json=request)
    assert replay.status_code == 200, replay.json()
    assert replay.json() == body
    assert len(completion.calls) == 2
    confirmed = _post_respond(client, session_id, edited_values={"columns": ["sku", "quantity"]})
    (source,) = confirmed["composition_state"]["composer_meta"]["guided_session"]["reviewed_sources"].values()
    assert source["options"] == {key: value for key, value in configured_options.items() if key != "on_validation_failure"}
    assert source["on_validation_failure"] == "discard"
    assert len(completion.calls) == 2


@pytest.mark.parametrize("at_form", [False, True])
def test_exhausted_invalid_uploaded_config_preserves_source_custody(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch, at_form: bool
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    if at_form:
        _post_respond(client, session_id, chosen=["csv"])
    before = _guided(client, session_id)
    completion = _resolve_upload(
        monkeypatch,
        blob_id,
        options={"schema": {"mode": "observed", "fields": {"sku": "str", "quantity": "int"}}, "encoding": "utf-8-sig"},
    )
    request = _chat_body(before["next_turn"], _UPLOAD_SENTINEL.format(filename="inventory.csv"))
    response = client.post(f"/api/sessions/{session_id}/guided/chat", json=request)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(completion.calls) == 2
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert body["next_turn"] == before["next_turn"]
    for key in ("pending_source_intents", "reviewed_sources", "history"):
        assert (
            body["composition_state"]["composer_meta"]["guided_session"][key]
            == before["composition_state"]["composer_meta"]["guided_session"][key]
        )
    assert body["composition_state"]["sources"] == {}
    blobs = asyncio.run(client.app.state.blob_service.list_blobs(UUID(session_id)))
    assert [str(blob.id) for blob in blobs] == [blob_id]
    replay = client.post(f"/api/sessions/{session_id}/guided/chat", json=request)
    assert replay.status_code == 200, replay.json()
    assert replay.json() == body
    assert len(completion.calls) == 2


@pytest.mark.parametrize("at_form", [False, True])
def test_provider_chat_only_cannot_author_an_uploaded_source(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch, at_form: bool
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    if at_form:
        _post_respond(client, session_id, chosen=["csv"])
    before = _guided(client, session_id)
    completion = _ReturningLiteLLMCompletion(_fake_llm_reply("Please clarify which source behavior you intend."))
    monkeypatch.setattr(_CHAT_SOLVER_ACOMPLETION, completion)
    for message in ("I want to discuss this input.", _UPLOAD_SENTINEL.format(filename="inventory.csv")):
        call_count = len(completion.calls)
        response = client.post(f"/api/sessions/{session_id}/guided/chat", json=_chat_body(before["next_turn"], message))
        assert response.status_code == 200, response.json()
        body = response.json()
        assert len(completion.calls) > call_count
        assert body["next_turn"] == before["next_turn"]
        for key in ("pending_source_intents", "reviewed_sources", "history"):
            assert (
                body["composition_state"]["composer_meta"]["guided_session"][key]
                == before["composition_state"]["composer_meta"]["guided_session"][key]
            )
        assert body["composition_state"]["sources"] == {}
    blobs = asyncio.run(client.app.state.blob_service.list_blobs(UUID(session_id)))
    assert [str(blob.id) for blob in blobs] == [blob_id]


@pytest.mark.parametrize("upload_exists", [False, True])
def test_uploaded_request_cannot_substitute_provider_invented_inline_content(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch, upload_exists: bool
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    expected_blob_ids = [_upload_inventory_csv(client, session_id)] if upload_exists else []
    before = _guided(client, session_id)
    completion = _ReturningLiteLLMCompletion(_fake_resolve_source_response_csv())
    monkeypatch.setattr(_CHAT_SOLVER_ACOMPLETION, completion)
    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(before["next_turn"], _UPLOAD_SENTINEL.format(filename="inventory.csv")),
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(completion.calls) == 1
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["next_turn"] == before["next_turn"]
    for key in ("pending_source_intents", "reviewed_sources", "history"):
        assert (
            body["composition_state"]["composer_meta"]["guided_session"][key]
            == before["composition_state"]["composer_meta"]["guided_session"][key]
        )
    assert body["composition_state"]["sources"] == {}
    blobs = asyncio.run(client.app.state.blob_service.list_blobs(UUID(session_id)))
    assert [str(blob.id) for blob in blobs] == expected_blob_ids


def test_uploaded_request_cannot_reselect_plugin_from_another_ready_blob(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    _upload_inventory_csv(client, session_id)
    _post_respond(client, session_id, chosen=["csv"])
    _upload_inline_blob(client, session_id, filename="rows.json", content='[{"sku":"other"}]', mime_type="application/json")
    before = _guided(client, session_id)
    completion = _ReturningLiteLLMCompletion(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="reselect_source_plugin",
                                    arguments=json.dumps({"plugin": "json", "assistant_message": "I selected the JSON upload."}),
                                )
                            )
                        ],
                    )
                )
            ]
        )
    )
    monkeypatch.setattr(_CHAT_SOLVER_ACOMPLETION, completion)
    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(before["next_turn"], _UPLOAD_SENTINEL.format(filename="inventory.csv")),
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(completion.calls) == 1
    assert body["next_turn"] == before["next_turn"]
    for key in ("pending_source_intents", "reviewed_sources", "history"):
        assert (
            body["composition_state"]["composer_meta"]["guided_session"][key]
            == before["composition_state"]["composer_meta"]["guided_session"][key]
        )
    assert body["composition_state"]["sources"] == {}


def test_unoffered_upload_reference_is_a_model_defect_without_binding(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A form bound to one inspected blob cannot silently switch to another."""
    client = composer_test_client
    session_id = _create_session(client)
    _upload_inventory_csv(client, session_id)
    form = _post_respond(client, session_id, chosen=["csv"])["next_turn"]
    blob_id = _upload_inline_blob(
        client,
        session_id,
        filename="rows.json",
        content='[{"sku": "SKU-001"}]',
        mime_type="application/json",
    )
    completion = _resolve_upload(monkeypatch, blob_id)

    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(form, _UPLOAD_SENTINEL.format(filename="rows.json")),
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(completion.calls) == 2
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "model_defect"
    assert body["next_turn"] == form
    assert body["composition_state"]["sources"] == {}


def test_provider_plugin_mismatch_is_rejected_without_server_substitution(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    before = _guided(client, session_id)
    completion = _resolve_upload(monkeypatch, blob_id, plugin="json")
    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(before["next_turn"], _UPLOAD_SENTINEL.format(filename="inventory.csv")),
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(completion.calls) == 2
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert body["next_turn"] == before["next_turn"]
    for key in ("pending_source_intents", "reviewed_sources", "history"):
        assert (
            body["composition_state"]["composer_meta"]["guided_session"][key]
            == before["composition_state"]["composer_meta"]["guided_session"][key]
        )
    assert body["composition_state"]["sources"] == {}


@pytest.mark.parametrize("error_type", [PluginConfigError, ValueError])
def test_rejected_uploaded_bind_degrades_to_a_not_applied_turn(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """External response and plugin configuration rejections preserve custody."""
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    completion = _resolve_upload(monkeypatch, blob_id)
    initial_turn = _guided(client, session_id)["next_turn"]

    def reject_bind(**_kwargs: object) -> object:
        raise error_type("injected uploaded-bind transition rejection")

    monkeypatch.setattr(guided_chat_atomic, "_prepare_step_1_uploaded_source_bind", reject_bind)

    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(initial_turn, _UPLOAD_SENTINEL.format(filename="inventory.csv")),
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(completion.calls) == 1
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert "still uploaded" in body["assistant_message"]
    assert body["next_turn"] == initial_turn
    assert body["composition_state"]["sources"] == {}
    blobs = asyncio.run(client.app.state.blob_service.list_blobs(UUID(session_id)))
    assert [str(blob.id) for blob in blobs] == [blob_id]


def test_rejected_provider_options_keep_grouped_deferred_intent_and_disposition(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    before = _guided(client, session_id)
    completion = _resolve_upload(monkeypatch, blob_id, options={"schema": {"mode": "flexible"}})
    completion.response.choices[0].message.tool_calls.append(
        SimpleNamespace(
            id="retain-later-transform",
            function=SimpleNamespace(name="retain_deferred_intent", arguments=json.dumps(_PAIR_RETAIN_ARGUMENTS)),
        )
    )
    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(
            before["next_turn"],
            "Later add the passthrough transform.\n" + _UPLOAD_SENTINEL.format(filename="inventory.csv"),
        ),
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(completion.calls) == 2
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert "couldn't apply" in body["assistant_message"]
    assert "I saved that instruction for the topology stage." in body["assistant_message"]
    assert body["next_turn"] == before["next_turn"]
    actual_guided = body["composition_state"]["composer_meta"]["guided_session"]
    (intent,) = actual_guided["deferred_intents"]
    assert intent["target_stage"] == "topology"
    for key in ("pending_source_intents", "reviewed_sources", "history"):
        assert actual_guided[key] == before["composition_state"]["composer_meta"]["guided_session"][key]
    assert body["composition_state"]["sources"] == {}


@pytest.mark.parametrize("error_type", [AuditIntegrityError, InvariantError])
def test_uploaded_bind_integrity_failure_fails_the_operation_closed(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """Corrupt owned custody is an integrity failure, never plugin rejection."""
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    completion = _resolve_upload(monkeypatch, blob_id)
    initial_turn = _guided(client, session_id)["next_turn"]

    primary = error_type("injected uploaded-bind custody failure")

    def break_bind(**_kwargs: object) -> object:
        raise primary

    monkeypatch.setattr(guided_chat_atomic, "_prepare_step_1_uploaded_source_bind", break_bind)

    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(initial_turn, _UPLOAD_SENTINEL.format(filename="inventory.csv")),
    )
    assert response.status_code == 500
    assert response.json()["detail"]["failure_code"] == "integrity_error"
    assert str(primary) not in response.text
    assert len(completion.calls) == 1
    assert _guided(client, session_id)["composition_state"]["sources"] == {}


@pytest.mark.parametrize("error_type", [AuditIntegrityError, InvariantError])
@pytest.mark.parametrize("at_form", [False, True])
def test_grouped_uploaded_config_integrity_failure_does_not_salvage_retain(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception], at_form: bool
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    if at_form:
        _post_respond(client, session_id, chosen=["csv"])
    before = _guided(client, session_id)
    completion = _resolve_upload(monkeypatch, blob_id)
    completion.response.choices[0].message.tool_calls.append(
        SimpleNamespace(
            id="retain-later-transform",
            function=SimpleNamespace(name="retain_deferred_intent", arguments=json.dumps(_PAIR_RETAIN_ARGUMENTS)),
        )
    )
    primary = error_type("injected uploaded-config authority integrity failure")

    def corrupt_authority(*_args: object, **_kwargs: object) -> object:
        raise primary

    monkeypatch.setattr(guided_route, "_schema8_schema_authority", corrupt_authority)
    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(before["next_turn"], _UPLOAD_SENTINEL.format(filename="inventory.csv")),
    )
    assert response.status_code == 500, response.json()
    assert response.json()["detail"]["failure_code"] == "integrity_error"
    assert str(primary) not in response.text
    assert len(completion.calls) == 1
    after = _guided(client, session_id)
    assert after["next_turn"] == before["next_turn"]
    for key in ("deferred_intents", "pending_source_intents", "reviewed_sources", "history"):
        assert (
            after["composition_state"]["composer_meta"]["guided_session"][key]
            == before["composition_state"]["composer_meta"]["guided_session"][key]
        )
    assert after["composition_state"]["sources"] == {}


@pytest.mark.parametrize("retain_later", [False, True])
def test_invalid_uploaded_config_then_prose_retry_never_binds_source(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch, retain_later: bool
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    before = _guided(client, session_id)
    invalid_reply = _resolve_upload(
        monkeypatch, blob_id, options={"schema": {"mode": "observed", "fields": {"sku": "str", "quantity": "int"}}}
    ).response
    if retain_later:
        invalid_reply.choices[0].message.tool_calls.append(
            SimpleNamespace(
                id="retain-later-transform",
                function=SimpleNamespace(name="retain_deferred_intent", arguments=json.dumps(_PAIR_RETAIN_ARGUMENTS)),
            )
        )
    completion = _SequencedLiteLLMCompletion([invalid_reply, _fake_llm_reply("Please clarify the intended input schema.")])
    monkeypatch.setattr(_CHAT_SOLVER_ACOMPLETION, completion)
    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(before["next_turn"], _UPLOAD_SENTINEL.format(filename="inventory.csv")),
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(completion.calls) == 2
    assert body["next_turn"] == before["next_turn"]
    actual_guided = body["composition_state"]["composer_meta"]["guided_session"]
    for key in ("pending_source_intents", "reviewed_sources", "history"):
        assert actual_guided[key] == before["composition_state"]["composer_meta"]["guided_session"][key]
    assert body["composition_state"]["sources"] == {}
    if retain_later:
        (intent,) = actual_guided["deferred_intents"]
        expected_retained = {
            **_PAIR_RETAIN_ARGUMENTS,
            "redacted_summary": "Future topology instruction for transform plugin 'passthrough'; 1 structural constraint(s).",
        }
        for key, value in expected_retained.items():
            assert intent[key] == value
        assert body["assistant_message_kind"] == "synthetic_failure"
        assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
        assert "I saved that instruction for the topology stage." in body["assistant_message"]
    else:
        assert actual_guided["deferred_intents"] == []


class TestSourceFormBlobPrefillFallback:
    """A Step-1 form whose intent captured no inspection facts still prefills.

    ``path`` has no practically legal web value other than the ``blob:<id>``
    sentinel, so a form emitted without prefill is a dead end. A plural raw
    ready set resolves no blob identity at selection time, which is exactly
    when the intent captures nothing — but the prefill stays recency-blind: it
    fires only when exactly ONE ready upload could prefill the selected plugin.
    """

    def test_one_compatible_upload_prefills_past_an_unrelated_ready_blob(
        self,
        composer_test_client: TestClient,
    ) -> None:
        client = composer_test_client
        session_id = _create_session(client)
        compatible = _upload_inventory_csv(client, session_id, filename="inventory.csv")
        _upload_inline_blob(
            client,
            session_id,
            filename="rows.json",
            content='[{"sku": "SKU-001"}]',
            mime_type="application/json",
        )

        form = _post_respond(client, session_id, chosen=["csv"])["next_turn"]

        assert form["type"] == "schema_form"
        # The newest ready blob is the JSON one, so this prefill is not "the
        # latest upload" — it is the only upload that could prefill csv.
        assert form["payload"]["prefilled"]["path"] == f"blob:{compatible}"
        # Display prefill only: the intent captured no facts, so the wizard's
        # own Continue still owns the commit decision.
        intents = _guided(client, session_id)["composition_state"]["composer_meta"]["guided_session"]["pending_source_intents"]
        assert next(iter(intents.values()))["inspection_facts"] is None

    def test_compatible_upload_fallback_uses_bounded_verified_inspection(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fallback inspection must never materialize every ready blob in memory."""
        client = composer_test_client
        session_id = _create_session(client)
        _upload_inventory_csv(client, session_id, filename="inventory.csv")
        _upload_inline_blob(
            client,
            session_id,
            filename="rows.json",
            content='[{"sku": "SKU-001"}]',
            mime_type="application/json",
        )
        blob_service = client.app.state.blob_service
        original_prefix_read = blob_service.read_blob_content_prefix_verified
        prefix_limits: list[int] = []

        async def _forbid_full_read(*_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("guided fallback must not materialize full blob content")

        async def _record_prefix_read(blob_id: UUID, *, prefix_bytes: int, **kwargs: object) -> tuple[bytes, str, int]:
            prefix_limits.append(prefix_bytes)
            return await original_prefix_read(blob_id, prefix_bytes=prefix_bytes, **kwargs)

        monkeypatch.setattr(blob_service, "read_blob_content", _forbid_full_read)
        monkeypatch.setattr(blob_service, "read_blob_content_prefix_verified", _record_prefix_read)

        form = _post_respond(client, session_id, chosen=["csv"])["next_turn"]

        assert form["payload"]["prefilled"]["path"].startswith("blob:")
        assert prefix_limits == [8 * 1024, 8 * 1024]

    def test_several_compatible_uploads_stay_ambiguous_and_do_not_prefill(
        self,
        composer_test_client: TestClient,
    ) -> None:
        """Recency is not source intent — the no-latest-blob rule still holds."""
        client = composer_test_client
        session_id = _create_session(client)
        _upload_inventory_csv(client, session_id, filename="first.csv")
        _upload_inventory_csv(client, session_id, filename="second.csv")

        form = _post_respond(client, session_id, chosen=["csv"])["next_turn"]

        assert form["type"] == "schema_form"
        assert form["payload"]["prefilled"] == {"schema": {"mode": "observed"}}

    def test_incompatible_ready_uploads_do_not_prefill_the_form(
        self,
        composer_test_client: TestClient,
    ) -> None:
        client = composer_test_client
        session_id = _create_session(client)
        for filename in ("first.json", "second.json"):
            _upload_inline_blob(
                client,
                session_id,
                filename=filename,
                content='[{"sku": "SKU-001"}]',
                mime_type="application/json",
            )

        form = _post_respond(client, session_id, chosen=["csv"])["next_turn"]

        assert form["type"] == "schema_form"
        assert "path" not in form["payload"]["prefilled"]

    def test_another_sessions_upload_never_prefills_the_form(
        self,
        composer_test_client: TestClient,
    ) -> None:
        client = composer_test_client
        other_session_id = _create_session(client)
        _upload_inventory_csv(client, other_session_id, filename="inventory.csv")
        session_id = _create_session(client)

        form = _post_respond(client, session_id, chosen=["csv"])["next_turn"]

        assert form["type"] == "schema_form"
        assert form["payload"]["prefilled"] == {"schema": {"mode": "observed"}}
        assert asyncio.run(client.app.state.blob_service.list_blobs(UUID(session_id))) == []
