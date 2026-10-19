"""Ordinary visible tool evidence, distinct from private provider diagnostics.

The display projection stays unchanged. This owned companion retains received
and effective argument strings and the actual selected result for later Advisor
review. All authored strings are centrally secret-redacted before persistence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Final, final

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.credential_material import DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
from elspeth.web.validation import _redact_sensitive_content

VISIBLE_TOOL_EVENT_KIND: Final = "composer_visible_tool_event"
VISIBLE_TOOL_EVENT_SCHEMA: Final = "composer.visible-tool-event.v1"
VISIBLE_RESPONSE_GAP_KIND: Final = "composer_visible_response_gap"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@final
@dataclass(frozen=True, slots=True)
class VisibleToolEvent:
    session_id: str
    operation_id: str
    operation_epoch: int
    # Producer-declared correlation only. Historical ingress authentication is
    # separate; this value grants no access, authority or deduplication.
    producer_declared_request_id: str | None
    tool_call_id: str
    tool_name: str
    display_arguments_hash: str
    display_result_hash: str
    payload_json: str = field(repr=False)
    payload_hash: str
    parent_assistant_id: str | None = None

    @classmethod
    def capture(
        cls,
        *,
        context: SessionOperationContext,
        request_id: str | None,
        tool_call_id: str,
        tool_name: str,
        received_arguments: str | None,
        effective_arguments: str | None,
        selected_result: str | None,
        display_arguments: str,
        display_result: str,
    ) -> VisibleToolEvent:
        if type(context) is not SessionOperationContext or context.operation_kind is not SessionOperationKind.COMPOSE:
            raise AuditIntegrityError("Visible tool evidence requires exact COMPOSE custody")
        content: dict[str, Any] = {}
        gaps: list[str] = []
        for name, value in (
            ("received_arguments", received_arguments),
            ("effective_arguments", effective_arguments),
            ("selected_result", selected_result),
        ):
            if value is None:
                content[name] = None
                gaps.append(name + "_unavailable")
                continue
            if type(value) is not str:
                raise AuditIntegrityError("Visible tool evidence requires exact serialized strings")
            redacted = _redact_sensitive_content(value)
            try:
                require_no_credential_material(
                    redacted,
                    surface="composer_visible_tool_persistence",
                    max_string_bytes=max(DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_string_bytes, len(redacted.encode("utf-8"))),
                )
            except CredentialMaterialRefused:
                content[name] = None
                gaps.append(name + "_credential_disclosure_refused")
            else:
                content[name] = redacted
        content["availability_gaps"] = gaps
        payload = json.dumps(content, ensure_ascii=False)
        return cls(
            context.fence.session_id,
            context.fence.operation_id,
            context.fence.operation_epoch,
            request_id,
            tool_call_id,
            tool_name,
            _hash(display_arguments),
            _hash(display_result),
            payload,
            _hash(payload),
        )

    def validate(
        self,
        *,
        context: SessionOperationContext,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        display_arguments: str,
        display_result: str,
    ) -> None:
        if (
            type(self) is not VisibleToolEvent
            or type(context) is not SessionOperationContext
            or context.operation_kind is not SessionOperationKind.COMPOSE
            or context.fence.session_id != session_id
            or self.operation_id != context.fence.operation_id
            or self.operation_epoch != context.fence.operation_epoch
        ):
            raise AuditIntegrityError("Visible tool event differs from its actual fenced display row")
        self.validate_recorded_link(
            session_id=session_id,
            operation_id=context.fence.operation_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            display_arguments=display_arguments,
            display_result=display_result,
        )

    def validate_recorded_link(
        self,
        *,
        session_id: str,
        operation_id: str | None,
        tool_call_id: str,
        tool_name: str,
        display_arguments: str,
        display_result: str,
        parent_assistant_id: str | None = None,
    ) -> None:
        """Verify historical row linkage without minting a live operation fence."""
        if (
            self.session_id != session_id
            or (operation_id is not None and self.operation_id != operation_id)
            or self.parent_assistant_id != parent_assistant_id
            or self.tool_call_id != tool_call_id
            or self.tool_name != tool_name
            or self.display_arguments_hash != _hash(display_arguments)
            or self.display_result_hash != _hash(display_result)
            or self.payload_hash != _hash(self.payload_json)
        ):
            raise AuditIntegrityError("Visible tool event differs from its recorded display row")

    def with_parent(self, assistant_id: str) -> VisibleToolEvent:
        if self.parent_assistant_id is not None or type(assistant_id) is not str or not assistant_id:
            raise AuditIntegrityError("Visible tool event parent can be assigned only by its new P4 row")
        return replace(self, parent_assistant_id=assistant_id)

    def to_envelope(self) -> dict[str, Any]:
        return {"_kind": VISIBLE_TOOL_EVENT_KIND, "schema": VISIBLE_TOOL_EVENT_SCHEMA, **asdict(self)}

    @classmethod
    def from_envelope(cls, envelope: Mapping[str, Any]) -> VisibleToolEvent:
        expected = {"_kind", "schema", *cls.__dataclass_fields__}
        if (
            set(envelope) != expected
            or envelope.get("_kind") != VISIBLE_TOOL_EVENT_KIND
            or envelope.get("schema") != VISIBLE_TOOL_EVENT_SCHEMA
        ):
            raise AuditIntegrityError("Visible tool event has an unknown stored shape")
        values = {name: envelope[name] for name in cls.__dataclass_fields__}
        if (
            any(
                type(value) is not str
                for name, value in values.items()
                if name not in {"producer_declared_request_id", "parent_assistant_id", "operation_epoch"}
            )
            or any(
                values[name] is not None and type(values[name]) is not str
                for name in ("producer_declared_request_id", "parent_assistant_id")
            )
            or type(values["operation_epoch"]) is not int
            or values["operation_epoch"] < 1
        ):
            raise AuditIntegrityError("Visible tool event has invalid stored field types")
        result = cls(**values)
        if result.payload_hash != _hash(result.payload_json):
            raise AuditIntegrityError("Visible tool event lost its exact body binding")
        payload = json.loads(result.payload_json)
        if (
            type(payload) is not dict
            or set(payload) != {"received_arguments", "effective_arguments", "selected_result", "availability_gaps"}
            or any(
                payload[key] is not None and type(payload[key]) is not str
                for key in ("received_arguments", "effective_arguments", "selected_result")
            )
            or type(payload["availability_gaps"]) is not list
            or any(type(item) is not str for item in payload["availability_gaps"])
        ):
            raise AuditIntegrityError("Visible tool event has invalid stored content fields")
        return result


def visible_response_gap(
    call: ComposerLLMCall,
    context: SessionOperationContext,
    public_call: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist record-scoped missing bytes without claiming operation coverage."""
    if (
        type(call) is not ComposerLLMCall
        or type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
    ):
        raise AuditIntegrityError("Visible response coverage requires actual COMPOSE call custody")
    return {
        "_kind": VISIBLE_RESPONSE_GAP_KIND,
        "schema": "composer.visible-response-gap.v1",
        "session_id": context.fence.session_id,
        "operation_id": context.fence.operation_id,
        "operation_epoch": context.fence.operation_epoch,
        "provider_call_id": call.call_id,
        "status": call.status.value,
        "public_call_hash": _hash(json.dumps(deep_thaw(public_call), ensure_ascii=False, sort_keys=True)),
        "reason": "This public call metadata record contains no response bytes. Ordinary visible tool/reply records remain separate; unrecorded response fields are unavailable.",
    }


def validate_visible_response_gap(gap: Mapping[str, Any], public_call: Mapping[str, Any]) -> None:
    expected = {
        "_kind",
        "schema",
        "session_id",
        "operation_id",
        "operation_epoch",
        "provider_call_id",
        "status",
        "public_call_hash",
        "reason",
    }
    call = public_call.get("call")
    if (
        set(gap) != expected
        or gap.get("_kind") != VISIBLE_RESPONSE_GAP_KIND
        or gap.get("schema") != "composer.visible-response-gap.v1"
        or not isinstance(call, Mapping)
        or gap.get("provider_call_id") != call.get("call_id")
        or gap.get("status") != call.get("status")
        or gap.get("public_call_hash") != _hash(json.dumps(deep_thaw(public_call), ensure_ascii=False, sort_keys=True))
        or gap.get("reason")
        != "This public call metadata record contains no response bytes. Ordinary visible tool/reply records remain separate; unrecorded response fields are unavailable."
        or type(gap.get("session_id")) is not str
        or type(gap.get("operation_id")) is not str
        or type(gap.get("operation_epoch")) is not int
        or gap["operation_epoch"] < 1
    ):
        raise AuditIntegrityError("Stored visible-response gap lost its exact public call binding")


def validate_visible_audit_row(
    *,
    envelopes: tuple[Mapping[str, Any], ...],
    content: str,
    role: str,
    writer_principal: str,
    session_id: str,
    context: SessionOperationContext,
) -> None:
    """Admit the two ordinary companions only at an actual COMPOSE write.

    Other audit writers also accept PROPOSAL authority. Neither a copied kind
    string nor an envelope's own operation id can widen that authority.
    """
    companions = [item for item in envelopes if item.get("_kind") in {VISIBLE_TOOL_EVENT_KIND, VISIBLE_RESPONSE_GAP_KIND}]
    if not companions:
        return
    if (
        len(companions) != 1
        or len(envelopes) != 2
        or role != "audit"
        or writer_principal != "compose_loop"
        or type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
        or context.fence.session_id != session_id
    ):
        raise AuditIntegrityError("Ordinary visible audit evidence lacks actual COMPOSE producer custody")
    companion = companions[0]
    public = next(item for item in envelopes if item is not companion)
    if companion["_kind"] == VISIBLE_RESPONSE_GAP_KIND:
        validate_visible_response_gap(companion, public)
        if (
            companion["session_id"] != session_id
            or companion["operation_id"] != context.fence.operation_id
            or companion["operation_epoch"] != context.fence.operation_epoch
        ):
            raise AuditIntegrityError("Visible response coverage belongs to another operation")
    else:
        invocation = public.get("invocation")
        if public.get("_kind") != "audit" or not isinstance(invocation, Mapping):
            raise AuditIntegrityError("Visible tool evidence lacks its public invocation")
        visible = VisibleToolEvent.from_envelope(companion)
        visible.validate(
            context=context,
            session_id=session_id,
            tool_call_id=invocation["tool_call_id"],
            tool_name=invocation["tool_name"],
            display_arguments=invocation["arguments_canonical"],
            display_result=content,
        )
