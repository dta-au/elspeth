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
from elspeth.web.sessions.models import chat_messages_table, composer_continuation_effects_table
from elspeth.web.sessions.schemas import ComposerOperationSnapshot
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


async def _submit_and_settle(app, token, session_id, body, *, rounds: int = 240) -> ComposerOperationSnapshot:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        accepted = await client.post(f"/api/sessions/{session_id}/messages", json=body)
        assert accepted.status_code == 202, accepted.text
        for _ in range(rounds):
            response = await client.get(f"/api/sessions/{session_id}/operations/{body['operation_id']}")
            if response.status_code == 503 and response.json()["error_type"] == "database_unavailable":
                # SQLite serialises writers; a poll can lose its busy wait to
                # the owner's locked reads. The browser observer also retries.
                await asyncio.sleep(0.25)
                continue
            assert response.status_code == 200, response.text
            snapshot = ComposerOperationSnapshot.model_validate_json(response.content, strict=True)
            if snapshot.status in ("completed", "failed"):
                # Join the owner's verified release before the lifespan ends.
                await app.state.composer_async_worker.run_until_idle()
                return snapshot
            await asyncio.sleep(0.25)
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
        assert role in ("end", "early") and kwargs["model"] == self.advisor_model and "response_format" in kwargs
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


def _tool_call(app, call_id: str, name: str, arguments: dict) -> dict:
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments

    dialect = app.state.composer_service._planner_dialect
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": canonical_json(encode_semantic_arguments(name, dialect, arguments))},
    }


class _LateToolSDK(ScriptedSDK):
    """Tool calls need the app's planner dialect, so they are built at call time."""

    def __init__(self, app_ref: list, steps):
        super().__init__("test/model", "test/advisor", steps)
        self.app_ref = app_ref

    async def __call__(self, **kwargs):
        role, payload = self.steps[0]
        if role == "primary" and callable(payload):
            self.steps[0] = (role, payload(self.app_ref[0]))
        return await super().__call__(**kwargs)


@pytest.mark.asyncio
async def test_tool_reply_runs_its_batch_then_a_second_primary_then_publishes(tmp_path, monkeypatch):
    """Tool reply -> tool -> P4 -> a second PRIMARY call -> END -> published.

    K-7 (a): each authoring transition has its own provider call. The second
    PRIMARY call sees the tool result; nothing between the two calls is
    server-authored.
    """
    words = "The pipeline is empty, so there is nothing to change yet."
    app_ref: list = []
    sdk = _LateToolSDK(
        app_ref,
        [
            ("primary", lambda app: ("Reading the current state.", [_tool_call(app, "read_1", "get_pipeline_state", {})])),
            ("primary", (words, None)),
            ("end", _end_admit()),
        ],
    )
    monkeypatch.setattr(litellm, "acompletion", sdk)
    operation_id = str(uuid4())
    async with _continuation_app(tmp_path, monkeypatch) as (app, token, session_id):
        app_ref.append(app)
        snapshot = await _submit_and_settle(
            app, token, session_id, message_body("What does the current pipeline do?", operation_id=operation_id)
        )
        assert snapshot.status == "completed", snapshot.error
        assert [request["role"] for request in sdk.requests] == ["primary", "primary", "end"] and not sdk.steps
        second = sdk.requests[1]["messages"]
        assert any(message.get("role") == "tool" and message.get("tool_call_id") == "read_1" for message in second)
        effects = {row.effect_key: row for row in _effects(app, operation_id)}
        assert {key.split(":", 1)[0] for key in effects} == {"provider", "tool", "batch", "advance", "end"}
        assert all(row.disposition == "committed" for row in effects.values())
        assert {key for key in effects if key.startswith("provider:")} == {"provider:0:0", "provider:0:1"}
        published = snapshot.result.message.content
        assert published.split("\n\n---\n\n")[0] == words


def _early_clean() -> dict:
    return {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "The draft is coherent.", "note": None}


async def _seed_csv_session(app, tmp_path, session_id, trust_mode: str) -> None:
    (tmp_path / "blobs" / str(session_id)).mkdir(parents=True, exist_ok=True)
    (tmp_path / "blobs" / str(session_id) / "input.csv").write_text("name\nAda\n", encoding="utf-8")
    (tmp_path / "outputs" / str(session_id)).mkdir(parents=True, exist_ok=True)
    await app.state.session_service.update_composer_preferences(session_id, trust_mode=trust_mode, density_default="high", actor="test")


@pytest.mark.asyncio
@pytest.mark.parametrize("trust_mode", ("explicit_approve", "auto_commit"))
async def test_set_pipeline_proposal_completes_under_each_trust_mode(tmp_path, monkeypatch, trust_mode):
    """A Continuation set_pipeline authors a proposal (explicit_approve) or a saved state (auto_commit).

    The durable proposal binding must accept a Continuation segment owner.
    The pipeline is the provider's own tool argument; nothing is server-authored.
    """
    from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline

    app_ref: list = []
    session_ref: list = []

    def authoring(app):
        pipeline = _pipeline(tmp_path, str(session_ref[0]))
        return ("Building the pipeline.", [_tool_call(app, "author_1", "set_pipeline", pipeline)])

    sdk = _LateToolSDK(
        app_ref,
        # The EARLY checkpoint reviews an empty -> non-empty saved graph. A
        # pending proposal saves nothing, so explicit_approve has no EARLY call.
        [
            ("primary", authoring),
            *((("early", _early_clean()),) if trust_mode == "auto_commit" else ()),
            ("primary", ("The draft was saved.", None)),
            ("end", _end_admit()),
        ],
    )
    monkeypatch.setattr(litellm, "acompletion", sdk)
    operation_id = str(uuid4())
    async with _continuation_app(tmp_path, monkeypatch) as (app, token, session_id):
        app_ref.append(app)
        session_ref.append(session_id)
        await _seed_csv_session(app, tmp_path, session_id, trust_mode)
        snapshot = await _submit_and_settle(
            app, token, session_id, message_body("Build a CSV to JSONL pipeline.", operation_id=operation_id)
        )
        assert snapshot.status == "completed", snapshot.error
        expected = ["primary", "early", "primary", "end"] if trust_mode == "auto_commit" else ["primary", "primary", "end"]
        assert [request["role"] for request in sdk.requests] == expected and not sdk.steps
        result = snapshot.result
        if trust_mode == "explicit_approve":
            assert len(result.proposals) == 1
        else:
            assert result.state is not None and not result.proposals
