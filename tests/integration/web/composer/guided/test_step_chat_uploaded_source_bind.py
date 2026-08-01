"""Deterministic uploaded-blob binding through guided Chat.

The frontend paperclip appends a fixed bind sentence to the chat box after an
upload completes. That sentence carries no blob id and no plugin, so routing it
to the provider asks a model to invent the file's bytes — which the resolver
correctly rejects, wedging the flow. These tests pin the deterministic route:
the sentinel binds from server-held inspection facts, stops on the
confirmation-gated review card, and never commits a source on its own.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from elspeth.web.sessions.routes.composer import guided as guided_route
from elspeth.web.sessions.routes.composer.guided_chat_atomic import GuidedChatProviderOutcome
from tests.integration.web.composer.guided.test_step_chat import _create_session
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


def _refuse_provider(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Install a provider runner that records (and fails) any provider call."""
    calls: list[dict] = []

    async def _never_called(**kwargs: object) -> GuidedChatProviderOutcome:
        calls.append(dict(kwargs))
        raise AssertionError("the deterministic upload route must not call a provider")

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _never_called)
    return calls


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
    """THE acceptance regression: fresh session, upload, sentinel appended to prose.

    A fresh guided session opens on the source plugin ``single_select`` turn —
    the exact turn a first upload arrives on — and the helper appends its bind
    sentence AFTER whatever the user typed. The turn must bind deterministically
    (no provider call), derive the plugin from the inspected content kind, and
    stop on the ``inspect_and_confirm`` review card with the observed columns.
    Nothing is committed: ``composition_state.sources`` stays empty until the
    user confirms.
    """
    client = composer_test_client
    session_id = _create_session(client)
    blob_id = _upload_inventory_csv(client, session_id)
    provider_calls = _refuse_provider(monkeypatch)
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
    assert provider_calls == []
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
    _upload_inventory_csv(client, session_id)
    _refuse_provider(monkeypatch)
    body = _chat_body(_guided(client, session_id)["next_turn"], _UPLOAD_SENTINEL.format(filename="inventory.csv"))

    first = client.post(f"/api/sessions/{session_id}/guided/chat", json=body)
    assert first.status_code == 200, first.json()
    settled = first.json()
    history = settled["guided_session"]["history"]
    assert [record["turn_type"] for record in history] == ["single_select", "schema_form", "inspect_and_confirm"]
    assert [record["response_hash"] is None for record in history] == [False, False, True]

    replay = client.post(f"/api/sessions/{session_id}/guided/chat", json=body)

    assert replay.status_code == 200, replay.json()
    assert replay.json() == settled
    assert _guided(client, session_id)["guided_session"]["history"] == history


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
    _upload_inventory_csv(client, session_id)
    form = _post_respond(client, session_id, chosen=["csv"])["next_turn"]
    assert form["type"] == "schema_form"
    assert form["payload"]["prefilled"]["path"].startswith("blob:")
    provider_calls = _refuse_provider(monkeypatch)

    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(form, _UPLOAD_SENTINEL.format(filename="inventory.csv")),
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert provider_calls == []
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


def test_incompatible_upload_reports_a_type_mismatch_without_binding(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready upload whose content cannot prefill the selected plugin is refused honestly."""
    client = composer_test_client
    session_id = _create_session(client)
    _upload_inventory_csv(client, session_id)
    form = _post_respond(client, session_id, chosen=["csv"])["next_turn"]
    _upload_inline_blob(
        client,
        session_id,
        filename="rows.json",
        content='[{"sku": "SKU-001"}]',
        mime_type="application/json",
    )
    provider_calls = _refuse_provider(monkeypatch)

    response = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(form, _UPLOAD_SENTINEL.format(filename="rows.json")),
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert provider_calls == []
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert body["next_turn"] == form
    assert body["composition_state"]["sources"] == {}


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
