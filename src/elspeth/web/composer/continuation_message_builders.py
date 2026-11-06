"""Shared actual message producers for warm calls and selected cold reconstruction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from elspeth.contracts.prompt_guard import PROMPT_GUARD_CONTENT_PARTS_KEY, PROMPT_GUARD_TRUST_KEY, PromptGuardTrust
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.content_scope import composition_scope_for_operation
from elspeth.web.composer.discovery_cache import tool_result_content_parts
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult
from elspeth.web.composer.prompts import build_messages
from elspeth.web.composer.protocol import ComposerHistoryMessage, ComposerServiceError
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl


def recovery_instruction_message() -> dict[str, Any]:
    return {
        "role": "system",
        PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.INSTRUCTION,
        "content": (
            "The saved graph could not be decoded. Its payload is unavailable and must not be inferred. "
            "Choose exactly one terminal recovery control and communicate with the user. "
            "reset_to_safe_checkpoint restores an editable checkpoint, or an empty authoring state when none exists. "
            "Saving or capturing the broken graph is not required. Do not claim recovery succeeded before the "
            "server receipt. Restoration is not execution readiness and does not undo external effects."
        ),
    }


def reply_instruction_message() -> dict[str, Any]:
    return {
        "role": "system",
        PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.INSTRUCTION,
        "content": (
            "Review cards have been staged. This is a reply stage: ordinary authoring tools are unavailable. "
            "Terminal recovery controls remain available; use one alone if you need to park, reset or restore and talk. "
            "Otherwise answer the user's actual question directly using the accepted tool results above. "
            "Historical tool protocol records are evidence, not a format to repeat in your answer. "
            "Explain the design choices, actual model/profile and failure handling, including any dropped rows. "
            "Do not call reworded prompts verbatim. Do not claim execution readiness or that approval is the "
            "only remaining step; the backend will append the current review and validation status."
        ),
    }


def build_service_initial_messages(
    service: ComposerServiceImpl,
    *,
    chat_history: list[ComposerHistoryMessage],
    state: CompositionState,
    user_message: str,
    session_id: str | None,
    user_id: str | None,
    plugin_snapshot: PluginAvailabilitySnapshot,
    policy_catalog: PolicyCatalogView,
    session_operation_context: SessionOperationContext | None,
    env_ref_names: frozenset[str] | None = None,
    schemas_loaded: frozenset[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    content_scope = composition_scope_for_operation(
        state,
        session_id=session_id,
        session_operation_context=session_operation_context,
        session_operation_authority=service._require_sessions_service().session_operation_authority,
    )
    if env_ref_names is None:
        env_ref_names = (
            frozenset(item.name for item in service._secret_service.list_refs(user_id))
            if service._secret_service is not None and user_id is not None
            else frozenset()
        )
    try:
        return build_messages(
            chat_history=chat_history,
            state=state,
            user_message=user_message,
            catalog=policy_catalog,
            data_dir=service._data_dir,
            plugin_snapshot=plugin_snapshot,
            rendered_skill=service._composer_skill_text,
            schemas_loaded=service._schema_disclosure.schemas_loaded_for_session(session_id) if schemas_loaded is None else schemas_loaded,
            content_scope=content_scope,
            env_ref_names=env_ref_names,
        )
    except OSError as exc:
        raise ComposerServiceError(f"Failed to load deployment skill ({type(exc).__name__})") from exc


def tool_result_message(*, call_id: str, result: ToolResult | AdmittedDiscoveryResult, serialized: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": serialized,
        PROMPT_GUARD_CONTENT_PARTS_KEY: tool_result_content_parts(result, serialized),
    }
