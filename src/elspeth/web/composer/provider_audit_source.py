"""Original provider audit source and complete draft DATA; no writer authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind

if TYPE_CHECKING:
    from elspeth.contracts.composer_llm_audit import ComposerLLMCall
    from elspeth.web.sessions._persist_payload import AuditMessageDraft


@dataclass(frozen=True, slots=True)
class ProviderAuditSource:
    session_id: str
    operation_id: str
    operation_epoch: int

    def __post_init__(self) -> None:
        if (
            type(self.session_id) is not str
            or not self.session_id.strip()
            or type(self.operation_id) is not str
            or not self.operation_id.strip()
            or type(self.operation_epoch) is not int
            or self.operation_epoch < 1
        ):
            raise AuditIntegrityError("Provider audit source requires complete original coordinates")


def capture_provider_audit_source(context: SessionOperationContext) -> ProviderAuditSource:
    if type(context) is not SessionOperationContext:
        raise AuditIntegrityError("Provider audit source capture requires an actual operation context")
    return ProviderAuditSource(context.fence.session_id, context.fence.operation_id, context.fence.operation_epoch)


def provider_attempt_audit_drafts(call: ComposerLLMCall, context: SessionOperationContext) -> tuple[AuditMessageDraft, ...]:
    if type(context) is not SessionOperationContext or context.operation_kind is not SessionOperationKind.COMPOSE:
        raise AuditIntegrityError("Visible response coverage requires actual COMPOSE call custody")
    return provider_attempt_audit_drafts_for_source(call, capture_provider_audit_source(context))


def provider_attempt_audit_drafts_for_source(call: ComposerLLMCall, source: ProviderAuditSource) -> tuple[AuditMessageDraft, ...]:
    from elspeth.contracts.composer_llm_audit import ComposerLLMCall
    from elspeth.web.composer.audit import llm_call_audit_envelope
    from elspeth.web.composer.audit_storage import private_diagnostic_drafts_for_source
    from elspeth.web.composer.visible_tool_event import visible_response_gap_for_source
    from elspeth.web.sessions._persist_payload import AuditMessageDraft

    if type(call) is not ComposerLLMCall or call.call_id is None:
        raise AuditIntegrityError("A provider checkpoint requires its pending attempt identity")
    if type(source) is not ProviderAuditSource:
        raise AuditIntegrityError("Provider audit projection requires nominal source DATA")
    public_call = llm_call_audit_envelope(call)
    drafts = (
        AuditMessageDraft(
            role="audit",
            content="Provider call result recorded.",
            tool_calls=(public_call, visible_response_gap_for_source(call, source, public_call)),
        ),
    )
    return drafts + private_diagnostic_drafts_for_source(call, source=source)


def audit_drafts_json(drafts: tuple[AuditMessageDraft, ...]) -> str:
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.continuation_codec import owned_json
    from elspeth.web.sessions._persist_payload import AuditMessageDraft

    if type(drafts) is not tuple or any(type(draft) is not AuditMessageDraft for draft in drafts):
        raise AuditIntegrityError("Audit draft retention requires the complete nominal tuple")
    return canonical_json(
        owned_json(
            [
                {
                    "role": draft.role,
                    "content": draft.content,
                    "tool_calls": draft.tool_calls,
                    "tool_call_id": draft.tool_call_id,
                    "parent_assistant_id": draft.parent_assistant_id,
                    "composition_state_id": draft.composition_state_id,
                    "inherit_composition_state": draft.inherit_composition_state,
                }
                for draft in drafts
            ]
        )
    )


def decode_audit_drafts_json(text: str) -> tuple[AuditMessageDraft, ...]:
    from elspeth.contracts.hashing import canonical_json
    from elspeth.contracts.json_parser import parse_json_strict
    from elspeth.web.sessions._persist_payload import AuditMessageDraft
    from elspeth.web.sessions.protocol import ChatMessageRole

    if type(text) is not str:
        raise AuditIntegrityError("Retained audit drafts lost their canonical JSON")
    rows, error = parse_json_strict(text)
    if error is not None or type(rows) is not list or canonical_json(rows) != text:
        raise AuditIntegrityError("Retained audit drafts are not a canonical ordered cohort")
    drafts: list[AuditMessageDraft] = []
    for row in rows:
        if type(row) is not dict or set(row) != {
            "role",
            "content",
            "tool_calls",
            "tool_call_id",
            "parent_assistant_id",
            "composition_state_id",
            "inherit_composition_state",
        }:
            raise AuditIntegrityError("Retained audit draft lost a complete owned field")
        role, content, calls = row["role"], row["content"], row["tool_calls"]
        call_id, parent_id, state_id = row["tool_call_id"], row["parent_assistant_id"], row["composition_state_id"]
        inherit = row["inherit_composition_state"]
        if (
            type(role) is not str
            or role not in {"user", "assistant", "system", "tool", "audit"}
            or type(content) is not str
            or (calls is not None and (type(calls) is not list or any(type(item) is not dict for item in calls)))
            or (call_id is not None and type(call_id) is not str)
            or (parent_id is not None and type(parent_id) is not str)
            or (state_id is not None and type(state_id) is not str)
            or type(inherit) is not bool
            or (not inherit and state_id is not None)
        ):
            raise AuditIntegrityError("Retained audit draft violates its complete field types")
        drafts.append(
            AuditMessageDraft(
                role=cast(ChatMessageRole, role),
                content=content,
                tool_calls=None if calls is None else tuple(calls),
                tool_call_id=call_id,
                parent_assistant_id=parent_id,
                composition_state_id=state_id,
                inherit_composition_state=inherit,
            )
        )
    result = tuple(drafts)
    if audit_drafts_json(result) != text:
        raise AuditIntegrityError("Retained audit draft changed during nominal reconstruction")
    return result
