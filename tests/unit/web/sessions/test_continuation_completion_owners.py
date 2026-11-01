"""U-40 completion owners: a Continuation request completes through the real worker.

Runtime design §2.2 assigns the later-Compose, Advisor and End owners to U-40.
Before them, every Continuation request ended in ``settle()`` as ``failed``:
the live phase owner had no arm after the retained primary response for a
reply without tool calls, and none after an applied P4 batch.

These cases drive the production admission route, the real
``ComposerAsyncWorker`` and the real Continuation driver. Only the external
SDK (``litellm.acompletion``) and the Azure guard HTTP boundary are scripted.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import litellm
import pytest
from litellm import ModelResponse
from pydantic import SecretStr
from sqlalchemy import select

from elspeth.web.app import create_app
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.composer import prompt_guard_clients
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.config import WebSettings
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.sessions.composer_operation_versions import COMPOSER_OPERATION_VERSION_HEADER
from elspeth.web.sessions.models import chat_messages_table, composer_continuation_effects_table
from elspeth.web.sessions.schemas import ComposerOperationSnapshotV2
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.helpers.composer_operations import message_body


def _guard(monkeypatch) -> PromptGuardSettings:
    def serve(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    monkeypatch.setattr(
        prompt_guard_clients,
        "build_prompt_guard_client",
        lambda configuration: AzurePromptGuardClient(configuration, http_transport=httpx.MockTransport(serve)),
    )
    return PromptGuardSettings(endpoint="https://guard.example.test", subscription_key=SecretStr("fixture-guard-credential"))


@asynccontextmanager
async def _continuation_app(tmp_path, monkeypatch):
    """The production app, lifespan and worker; only external I/O is scripted."""
    from tests.helpers.fenced_session import seed_token_policies

    guard = _guard(monkeypatch)
    settings = WebSettings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        composer_model="test/model",
        composer_advisor_model="test/advisor",
        composer_prompt_guard=guard,
        composer_advisor_prompt_guard=guard,
        composer_prompt_guard_mode="required",
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_boot_probe_enabled=False,
        composer_rate_limit_per_minute=100,
        shareable_link_signing_key=b"\x00" * 32,
        plugin_allowlist=("transform:passthrough",),
        composer_continuation_enabled=True,
        composer_async_scan_interval_seconds=0.05,
    )
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        yield await _seed_actor(app, seed_token_policies)


async def _seed_actor(app, seed_token_policies):
    provider = app.state.auth_provider
    assert isinstance(provider, LocalAuthProvider)
    provider.create_user("continuation-user", "test continuation password", "Continuation User")
    with app.state.session_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="continuation-user")
        grant_test_pipeline_user(conn, identity_id="continuation-user")
        seed_token_policies(conn, identity_id="continuation-user")
    token = provider._issuer.mint(identity_id="continuation-user", username="continuation-user")
    session = await app.state.session_service.create_session(
        user_id="continuation-user", title="Continuation evidence", auth_provider_type="local"
    )
    return app, token, session.id


def _reply(content: str, tool_calls=None) -> ModelResponse:
    return ModelResponse(
        model="test/model",
        choices=[
            {
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
            }
        ],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    )


async def _submit_and_settle(app, token, session_id, body, *, rounds: int = 1500) -> ComposerOperationSnapshotV2:
    headers = {"Authorization": f"Bearer {token}", COMPOSER_OPERATION_VERSION_HEADER: "2"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        accepted = await client.post(f"/api/sessions/{session_id}/messages", json=body)
        assert accepted.status_code == 202, accepted.text
        assert accepted.headers[COMPOSER_OPERATION_VERSION_HEADER] == "2"
        for _ in range(rounds):
            response = await client.get(f"/api/sessions/{session_id}/operations/{body['operation_id']}")
            assert response.status_code == 200, response.text
            snapshot = ComposerOperationSnapshotV2.model_validate_json(response.content, strict=True)
            if snapshot.status in ("completed", "failed"):
                return snapshot
            await asyncio.sleep(0.02)
    raise AssertionError("Continuation request did not settle")


def _primary_effects(app, operation_id: str):
    with app.state.session_engine.connect() as conn:
        return tuple(
            conn.execute(
                select(composer_continuation_effects_table)
                .where(
                    composer_continuation_effects_table.c.operation_id == operation_id,
                    composer_continuation_effects_table.c.owner == "provider",
                )
                .order_by(composer_continuation_effects_table.c.effect_key)
            )
        )


@pytest.mark.asyncio
async def test_text_only_reply_completes_and_publishes_the_provider_words(tmp_path, monkeypatch):
    """A reply without tool calls reaches End and publishes the model's own words.

    One authoring transition, one recorded PRIMARY provider call (K-7 a).
    """
    calls: list[dict] = []

    async def completion(**kwargs):
        calls.append(kwargs)
        return _reply("The request was explained without a pipeline change.")

    monkeypatch.setattr(litellm, "acompletion", completion)

    operation_id = str(uuid4())
    async with _continuation_app(tmp_path, monkeypatch) as (app, token, session_id):
        snapshot = await _submit_and_settle(
            app, token, session_id, message_body("Explain what this composer can do.", operation_id=operation_id)
        )
        assert snapshot.status == "completed", snapshot.error
        assert snapshot.error is None and snapshot.result is not None
        assert snapshot.result.message.content == "The request was explained without a pipeline change."
        assert len(calls) == 1
        (effect,) = _primary_effects(app, operation_id)
        assert effect.effect_key == "provider:0:0" and effect.disposition == "committed"
        with app.state.session_engine.connect() as conn:
            assistants = tuple(
                conn.execute(
                    select(chat_messages_table.c.content).where(
                        chat_messages_table.c.session_id == str(session_id), chat_messages_table.c.role == "assistant"
                    )
                )
            )
        assert [row.content for row in assistants] == ["The request was explained without a pipeline change."]
