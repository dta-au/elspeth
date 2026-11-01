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
                # Join the owner's verified release before the lifespan ends.
                await app.state.composer_async_worker.run_until_idle()
                return snapshot
            await asyncio.sleep(0.02)
    raise AssertionError("Continuation request did not settle")


def _effects(app, operation_id: str):
    with app.state.session_engine.connect() as conn:
        return tuple(
            conn.execute(
                select(composer_continuation_effects_table)
                .where(composer_continuation_effects_table.c.operation_id == operation_id)
                .order_by(composer_continuation_effects_table.c.effect_key)
            )
        )


def _assistant_contents(app, session_id) -> list[str]:
    with app.state.session_engine.connect() as conn:
        rows = conn.execute(
            select(chat_messages_table.c.content)
            .where(chat_messages_table.c.session_id == str(session_id), chat_messages_table.c.role == "assistant")
            .order_by(chat_messages_table.c.sequence_no)
        )
        return [row.content for row in rows]


class ScriptedSDK:
    """Physical SDK replies in order; each names the role it must serve.

    ``primary`` replies serve the composer model with the full palette;
    ``end`` replies serve the END Advisor review with its structured verdict.
    """

    def __init__(self, settings_model: str, advisor_model: str, steps):
        self.primary_model, self.advisor_model = settings_model, advisor_model
        self.steps = list(steps)
        self.requests: list[dict] = []

    async def __call__(self, **kwargs):
        assert self.steps, "unscripted physical SDK call"
        role, payload = self.steps.pop(0)
        self.requests.append({"role": role, **kwargs})
        if role == "primary":
            assert kwargs["model"] == self.primary_model
            assert kwargs["tools"], "every PRIMARY call carries the provider palette"
            content, tool_calls = payload
            return _reply(content, tool_calls)
        assert role == "end" and kwargs["model"] == self.advisor_model and "response_format" in kwargs
        return ModelResponse(
            model=self.advisor_model,
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(payload), "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )


def _end_admit() -> dict:
    return {
        "verdict": "CLEAN",
        "category": "other",
        "steps": [],
        "findings": "The reply answers the request and changes nothing.",
        "note": None,
        "publication_verdict": "ADMIT",
    }


@pytest.mark.asyncio
async def test_text_only_reply_completes_and_publishes_the_provider_words(tmp_path, monkeypatch):
    """A reply without tool calls reaches END, is reviewed, and publishes the model's own words.

    K-7 (a): the one authoring transition has exactly one PRIMARY provider
    call, and the END review is its own provider call (no server-authored
    reply). The END review effect is committed by the publication.
    """
    words = "The request was explained without a pipeline change."
    sdk = ScriptedSDK("test/model", "test/advisor", [("primary", (words, None)), ("end", _end_admit())])
    monkeypatch.setattr(litellm, "acompletion", sdk)

    operation_id = str(uuid4())
    async with _continuation_app(tmp_path, monkeypatch) as (app, token, session_id):
        snapshot = await _submit_and_settle(
            app, token, session_id, message_body("Explain what this composer can do.", operation_id=operation_id)
        )
        assert snapshot.status == "completed", snapshot.error
        assert snapshot.error is None and snapshot.result is not None
        # The shared finalizer appends the backend checkpoint receipt; the
        # reply above it is exactly the provider's words.
        published = snapshot.result.message.content
        assert published.split("\n\n---\n\n")[0] == words
        assert "[ELSPETH-SYSTEM] Checkpoint save status:" in published
        assert [request["role"] for request in sdk.requests] == ["primary", "end"] and not sdk.steps
        effects = {row.effect_key: row for row in _effects(app, operation_id)}
        primary = effects.pop("provider:0:0")
        assert primary.owner == "provider" and primary.disposition == "committed"
        ((end_key, end),) = effects.items()
        assert end_key.startswith("end:") and end.owner == "composer_end"
        assert end.disposition == "committed" and end.receipt_key == snapshot.result.message.id
        assert _assistant_contents(app, session_id) == [published]
