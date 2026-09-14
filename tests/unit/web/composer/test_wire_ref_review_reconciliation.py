"""Review custody for the ref-wiring composer tools.

``wire_blob_inline_ref`` and ``wire_secret_ref`` rewrite a component's options
in place. Two defects lived there:

* an LLM-authored blob could be wired as an ``llm`` node's prompt surface or
  model, where the review enumerators read only strings, so planner-written
  prompt text reached execution with no prompt/model review (finding #1);
* neither tool ran ``reconcile_authoritative_reviews``, so a resolved review
  whose artifact the wire rewrote stayed ``resolved`` while its anchor no
  longer matched, and Execute failed with a bare drift ``ValueError``
  (findings #27, #42 and the wire_secret_ref critic brief).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Literal, cast

import pytest

from elspeth.contracts.enums import CreationModality
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.secrets import ResolvedSecret, SecretInventoryItem
from elspeth.web.composer.state import CompositionState, NodeSpec
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.composer.tools.secrets import _execute_wire_secret_ref
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    WEB_SCRAPE_HTTP_IDENTITY_USER_TERM,
    InterpretationReviewPending,
    materialize_state_for_execution,
    model_choice_artifact_hash,
    pending_execution_interpretation_sites,
    pipeline_decision_artifact_hash,
    prompt_review_anchor_hash_from_options,
)
from elspeth.web.secrets.wiring_policy import SecretWiringPolicy, SecretWiringRule
from elspeth.web.sessions.models import blobs_table
from tests.unit.web.composer import test_blob_inline_tools as blob_tools

# Reuse the session/blob fixture the blob-tool suite already owns.
blob_env = blob_tools.blob_env

_MODEL = "openai/gpt-4o"


def _blob_id(result: ToolResult) -> str:
    assert isinstance(result.data, Mapping)
    blob_id = result.data["blob_id"]
    assert isinstance(blob_id, str)
    return blob_id


def _resolved(requirement_id: str, kind: str, user_term: str, *, artifact_hash: str, draft: str) -> dict[str, Any]:
    evidence_field = "accepted_artifact_hash" if kind == "pipeline_decision" else "resolved_prompt_template_hash"
    requirement: dict[str, Any] = {
        "id": requirement_id,
        "kind": kind,
        "user_term": user_term,
        "status": "resolved",
        "draft": draft,
        "event_id": f"evt-{requirement_id}",
        "accepted_value": draft,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": None,
    }
    requirement[evidence_field] = artifact_hash
    return requirement


def _with_node_options(state: CompositionState, node_id: str, options: dict[str, Any]) -> CompositionState:
    return replace(state, nodes=tuple(replace(node, options=options) if node.id == node_id else node for node in state.nodes))


def _multi_query_options() -> dict[str, Any]:
    return {
        "provider": "openrouter",
        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
        "model": _MODEL,
        "prompt_template": "Base {{ row.x }}",
        "system_prompt": "You are careful.",
        "queries": {"q": {"input_fields": {"x": "x"}, "template": "Q {{ row.x }}"}},
        "required_input_fields": [],
        "schema": {"mode": "observed"},
    }


def _reviewed_multi_query_state() -> CompositionState:
    options = _multi_query_options()
    anchor = prompt_review_anchor_hash_from_options(options)
    assert anchor is not None
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        _resolved(
            "llm_prompt_template:classify",
            "llm_prompt_template",
            "llm_prompt_template:classify",
            artifact_hash=anchor,
            draft="surface",
        ),
        _resolved(
            "llm_model_choice:classify",
            "llm_model_choice",
            "llm_model_choice:classify",
            artifact_hash=model_choice_artifact_hash(_MODEL),
            draft=_MODEL,
        ),
    ]
    return _with_node_options(blob_tools._inline_ref_state(), "classify", options)


def _wire_blob(blob_env: dict[str, Any], state: CompositionState, field_path: str, blob_id: str) -> ToolResult:
    return blob_tools.execute_tool(
        "wire_blob_inline_ref",
        {"field_path": field_path, "blob_id": blob_id},
        state,
        blob_tools._catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_context=blob_env["operation"],
        session_operation_authority=blob_env["authority"],
    )


def _requirement_status(state: CompositionState, node_id: str, kind: str) -> str:
    node = next(node for node in state.nodes if node.id == node_id)
    rows = deep_thaw(node.options)[INTERPRETATION_REQUIREMENTS_KEY]
    status = next(row["status"] for row in rows if row["kind"] == kind)
    assert isinstance(status, str)
    return status


# ── (A) LLM-authored blobs never become an LLM prompt surface or model ──────


# Every option path the shared guard names: the three prompt/model roots, a
# query's template, and a whole ``queries.<name>`` or ``queries`` value that
# would carry one. Dropping any arm of the guard turns its row red.
_GUARDED_FIELDS = ("prompt_template", "model", "system_prompt", "queries.q.template", "queries.q", "queries")
_LLM_AUTHORED_MODALITIES = tuple(modality for modality in CreationModality if modality.requires_llm_provenance())


def _llm_authored_blob_id(blob_env: dict[str, Any], modality: CreationModality) -> str:
    blob = blob_tools._create_blob(blob_env, content="Planner-written prompt {{ row.x }}", llm_authored=True)
    assert blob.success is True
    blob_id = _blob_id(blob)
    with blob_env["engine"].begin() as conn:
        conn.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(creation_modality=modality.value))
    return blob_id


@pytest.mark.parametrize("modality", _LLM_AUTHORED_MODALITIES, ids=lambda modality: modality.value)
@pytest.mark.parametrize("field", _GUARDED_FIELDS)
def test_llm_authored_blob_is_refused_into_llm_prompt_surface_and_model(
    blob_env: dict[str, Any], field: str, modality: CreationModality
) -> None:
    blob_id = _llm_authored_blob_id(blob_env, modality)
    state = _with_node_options(blob_tools._inline_ref_state(), "classify", _multi_query_options())

    result = _wire_blob(blob_env, state, f"node:classify.options.{field}", blob_id)

    assert result.success is False
    assert result.updated_state is state
    message = result.validation.errors[0].message
    assert message.startswith(f"wire_blob_inline_ref cannot wire LLM-authored blob '{blob_id}' into LLM node 'classify' option '{field}': ")
    assert "Planner-written prompt" not in message


@pytest.mark.parametrize("field", _GUARDED_FIELDS)
def test_user_uploaded_blob_is_accepted_into_every_llm_prompt_surface_and_model(blob_env: dict[str, Any], field: str) -> None:
    """The accepted control for each guarded field: only the modality decides."""
    blob = blob_tools._create_blob(blob_env, content="Uploaded prompt {{ row.x }}")
    assert blob.success is True
    state = _with_node_options(blob_tools._inline_ref_state(), "classify", _multi_query_options())

    result = _wire_blob(blob_env, state, f"node:classify.options.{field}", _blob_id(blob))

    assert result.success is True, [error.message for error in result.validation.errors]


def test_llm_authored_blob_refusal_leaves_non_prompt_llm_options_wireable(blob_env: dict[str, Any]) -> None:
    """The guard is scoped to prompt/model review domains, not the whole node."""
    blob = blob_tools._create_blob(blob_env, content="q_input", llm_authored=True)
    state = _with_node_options(blob_tools._inline_ref_state(), "classify", _multi_query_options())

    result = _wire_blob(blob_env, state, "node:classify.options.queries.q.input_fields.x", _blob_id(blob))

    assert result.success is True


def test_user_uploaded_blob_into_system_prompt_is_still_accepted(blob_env: dict[str, Any]) -> None:
    blob = blob_tools._create_blob(blob_env, content="You are an uploaded system prompt.")
    assert blob.success is True
    state = blob_tools._inline_ref_state()

    result = _wire_blob(blob_env, state, "node:classify.options.system_prompt", _blob_id(blob))

    assert result.success is True
    marker = deep_thaw(result.updated_state.nodes[0].options)["system_prompt"]
    assert marker["blob_ref"] == _blob_id(blob)
    assert marker["mode"] == "inline_content"


# ── (B) the wire reconciles authoritative reviews ───────────────────────────


def test_resolved_multi_query_surface_review_reopens_after_user_blob_wire(blob_env: dict[str, Any]) -> None:
    blob = blob_tools._create_blob(blob_env, content="Uploaded query template {{ row.x }}")
    state = _reviewed_multi_query_state()
    assert pending_execution_interpretation_sites(state) == ()

    result = _wire_blob(blob_env, state, "node:classify.options.queries.q.template", _blob_id(blob))

    assert result.success is True
    wired = result.updated_state
    assert _requirement_status(wired, "classify", "llm_prompt_template") == "pending"
    assert _requirement_status(wired, "classify", "llm_model_choice") == "resolved"
    assert [(site.component_id, site.kind.value) for site in pending_execution_interpretation_sites(wired)] == [
        ("classify", "llm_prompt_template")
    ]
    assert isinstance(materialize_state_for_execution(wired), InterpretationReviewPending)


@pytest.mark.parametrize("field", ["abuse_contact", "scraping_reason"])
def test_web_scrape_http_identity_review_refuses_blob_wire_at_the_tool(blob_env: dict[str, Any], field: str) -> None:
    blob = blob_tools._create_blob(blob_env, content="ops@example.org")
    scrape = NodeSpec(
        id="scrape",
        node_type="transform",
        plugin="web_scrape",
        input="rows",
        on_success="scraped",
        on_error="discard",
        options={
            "url_field": "url",
            "content_field": "content",
            "http": {"abuse_contact": "ops@example.com", "scraping_reason": "research", "allowed_hosts": "public_only"},
        },
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    base = replace(blob_tools._inline_ref_state(), nodes=(scrape,))
    identity_hash = pipeline_decision_artifact_hash(scrape, base.nodes, user_term=WEB_SCRAPE_HTTP_IDENTITY_USER_TERM)
    options = dict(deep_thaw(scrape.options))
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        _resolved(
            "web_scrape_http_identity:scrape",
            "pipeline_decision",
            WEB_SCRAPE_HTTP_IDENTITY_USER_TERM,
            artifact_hash=identity_hash,
            draft="identity",
        )
    ]
    state = _with_node_options(base, "scrape", options)
    assert pending_execution_interpretation_sites(state) == ()

    result = _wire_blob(blob_env, state, f"node:scrape.options.http.{field}", _blob_id(blob))

    assert result.success is False
    assert result.updated_state is state
    error = result.validation.errors[0]
    assert error.error_code == "review_reconciliation_failed"
    assert f"http.{field}" in error.message


# ── wire_secret_ref reconciles too ──────────────────────────────────────────


class _AvailableSecretService:
    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        del user_id
        return []

    def has_ref(self, user_id: str, name: str) -> bool:
        del user_id, name
        return True

    def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
        del user_id, name
        return None


def _secret_context() -> ToolContext:
    policy = SecretWiringPolicy(
        rules=(
            SecretWiringRule(
                secret="OTHER_API_KEY",
                component_type="transform",
                plugin="llm",
                option_key="api_key",
            ),
        )
    )
    return ToolContext(
        catalog=cast(Any, None),
        plugin_snapshot=cast(Any, None),
        secret_service=_AvailableSecretService(),
        secret_wiring_policy=policy,
        user_id="test-user",
    )


def _llm_state_with_model_review(model_value: object) -> CompositionState:
    base = blob_tools._inline_ref_state()
    options = dict(deep_thaw(base.nodes[0].options))
    options["model"] = model_value
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        _resolved(
            "llm_model_choice:classify",
            "llm_model_choice",
            "llm_model_choice:classify",
            artifact_hash=model_choice_artifact_hash(_MODEL),
            draft=_MODEL,
        )
    ]
    return _with_node_options(base, "classify", options)


_SECRET_TARGETS = ("node", "source", "output")


def _secret_wire(state: CompositionState, target: str) -> tuple[dict[str, Any], ToolContext]:
    """Arguments and a wiring policy that admit ``OTHER_API_KEY`` into ``target``'s ``api_key``."""
    if target == "node":
        return {"name": "OTHER_API_KEY", "target": "node", "target_id": "classify", "option_key": "api_key"}, _secret_context()
    component_type: Literal["source", "sink"]
    if target == "source":
        component_type = "source"
        plugin = state.sources["source"].plugin
        args: dict[str, Any] = {"name": "OTHER_API_KEY", "target": "source", "option_key": "api_key"}
    elif target == "output":
        component_type = "sink"
        plugin = state.outputs[0].plugin
        args = {"name": "OTHER_API_KEY", "target": "output", "target_id": state.outputs[0].name, "option_key": "api_key"}
    else:
        raise AssertionError(f"unknown secret target {target!r}")
    context = replace(
        _secret_context(),
        secret_wiring_policy=SecretWiringPolicy(
            rules=(SecretWiringRule(secret="OTHER_API_KEY", component_type=component_type, plugin=plugin, option_key="api_key"),)
        ),
    )
    return args, context


def _wired_component_options(state: CompositionState, target: str) -> Mapping[str, Any]:
    if target == "node":
        return state.nodes[0].options
    if target == "source":
        return state.sources["source"].options
    return state.outputs[0].options


@pytest.mark.parametrize("target", _SECRET_TARGETS)
def test_wire_secret_ref_every_target_arm_refuses_a_drifted_resolved_review(target: str) -> None:
    """Every target arm reconciles, not only the node arm.

    Reconciliation reads the whole state, so a drifted review on the llm node
    refuses a secret wire into the node, the source or the output. Reverting
    any arm to an unreconciled result turns its row red.
    """
    drifted_model = {"blob_ref": "00000000-0000-4000-8000-000000000001", "mode": "inline_content", "sha256": "0" * 64}
    state = _llm_state_with_model_review(drifted_model)
    args, context = _secret_wire(state, target)

    result = _execute_wire_secret_ref(args, state, context)

    assert result.success is False
    assert result.updated_state is state
    error = result.validation.errors[0]
    assert error.error_code == "review_reconciliation_failed"
    assert "has no model" in error.message


@pytest.mark.parametrize("target", _SECRET_TARGETS)
def test_wire_secret_ref_keeps_a_coherent_resolved_review(target: str) -> None:
    state = _llm_state_with_model_review(_MODEL)
    args, context = _secret_wire(state, target)

    result = _execute_wire_secret_ref(args, state, context)

    assert result.success is True, [error.message for error in result.validation.errors]
    wired = result.updated_state
    assert deep_thaw(_wired_component_options(wired, target))["api_key"] == {"secret_ref": "OTHER_API_KEY"}
    assert _requirement_status(wired, "classify", "llm_model_choice") == "resolved"
    assert wired.version == state.version + 1
