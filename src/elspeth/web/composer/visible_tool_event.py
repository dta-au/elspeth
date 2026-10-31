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
from typing import Any, Final, Literal, TypedDict, final

from pydantic import JsonValue

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.credential_material import DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.bounded_json import bounded_json_loads
from elspeth.web.composer.pipeline_custody import inline_custody_audit_projection
from elspeth.web.composer.provider_audit_source import ProviderAuditSource, capture_provider_audit_source
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
from elspeth.web.validation import _redact_sensitive_content

VISIBLE_TOOL_EVENT_KIND: Final = "composer_visible_tool_event"
VISIBLE_TOOL_EVENT_SCHEMA: Final = "composer.visible-tool-event.v1"
VISIBLE_RESPONSE_GAP_KIND: Final = "composer_visible_response_gap"
SELECTED_RESULT_AUTHORITY_REVOKED: Final = "selected_result_authority_revoked"


class VisibleResponseGapWire(TypedDict):
    _kind: Literal["composer_visible_response_gap"]
    schema: Literal["composer.visible-response-gap.v1"]
    session_id: str
    operation_id: str
    operation_epoch: int
    provider_call_id: str | None
    status: str
    public_call_hash: str
    reason: str


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unique_json_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise ValueError("Visible tool evidence contains duplicate JSON keys")
    return document


def _inline_custody_safe_text(value: str) -> tuple[str | None, str | None]:
    """Project private inline values before retaining ordinary pipeline evidence."""
    changed = False

    def project_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        nonlocal changed
        document = _unique_json_object(pairs)
        if "inline_blob" not in document:
            return document
        projected = dict(inline_custody_audit_projection(document))
        changed = changed or projected != document
        return projected

    try:
        # The hook removes private values before the decoded UTF-8 check. Even
        # an invalid surrogate inside an inline draft cannot escape in public
        # evidence, while ordinary sibling fields remain available.
        projected = bounded_json_loads(value, label="visible pipeline evidence", object_pairs_hook=project_object)
    except ValueError:
        return None, "inline_custody_projection_unavailable"
    if type(projected) is not dict:
        return None, "inline_custody_projection_unavailable"
    return (json.dumps(projected, ensure_ascii=False) if changed else value), None


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
            if tool_name == "set_pipeline":
                value, gap = _inline_custody_safe_text(value)
                if gap is not None:
                    content[name] = None
                    gaps.append(name + "_" + gap)
                    continue
                assert value is not None
            redacted = _redact_sensitive_content(value)
            if tool_name == "set_pipeline":
                projected_redacted, gap = _inline_custody_safe_text(redacted)
                if gap is not None:
                    content[name] = None
                    gaps.append(name + "_credential_redaction_invalid_json")
                    continue
                assert projected_redacted is not None
                redacted = projected_redacted
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

    def with_authority_revocation(self, display_result: str) -> VisibleToolEvent:
        """Record that P4 refused the selected result after the model saw it.

        The refusal becomes the event's outcome; the superseded candidate is
        not retained as replayable visible evidence.
        """
        if self.parent_assistant_id is not None:
            raise AuditIntegrityError("Only an unpersisted visible event may select its display")
        if type(display_result) is not str:
            raise TypeError("Visible display must be serialized text")
        content = json.loads(self.payload_json)
        content["selected_result"] = None
        content["availability_gaps"] = [*content["availability_gaps"], SELECTED_RESULT_AUTHORITY_REVOKED]
        payload = json.dumps(content, ensure_ascii=False)
        return replace(self, display_result_hash=_hash(display_result), payload_json=payload, payload_hash=_hash(payload))

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
        try:
            payload_hash = _hash(result.payload_json)
        except UnicodeEncodeError as exc:
            raise AuditIntegrityError("Visible tool event stored payload is not valid UTF-8") from exc
        if result.payload_hash != payload_hash:
            raise AuditIntegrityError("Visible tool event lost its exact body binding")
        payload, parse_error = parse_json_strict(result.payload_json)
        if parse_error is not None:
            raise AuditIntegrityError("Visible tool event stored payload is not strict JSON")
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
        try:
            for name in ("received_arguments", "effective_arguments", "selected_result"):
                if payload[name] is not None:
                    payload[name].encode("utf-8")
            for gap in payload["availability_gaps"]:
                gap.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise AuditIntegrityError("Visible tool event stored payload contains text that is not valid UTF-8") from exc
        if result.tool_name == "set_pipeline":
            for name in ("received_arguments", "effective_arguments", "selected_result"):
                original = payload[name]
                if original is not None:
                    projected, gap = _inline_custody_safe_text(original)
                    if gap is not None or projected != original:
                        raise AuditIntegrityError(
                            "Historical visible tool evidence contains unsafe or unparseable inline custody data; "
                            "public replay is refused while the original audit record is retained"
                        )
        return result


def visible_response_gap(
    call: ComposerLLMCall,
    context: SessionOperationContext,
    public_call: Mapping[str, Any],
) -> VisibleResponseGapWire:
    """Persist record-scoped missing bytes without claiming operation coverage."""
    if (
        type(call) is not ComposerLLMCall
        or type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
    ):
        raise AuditIntegrityError("Visible response coverage requires actual COMPOSE call custody")
    return visible_response_gap_for_source(call, capture_provider_audit_source(context), public_call)


def visible_response_gap_for_source(
    call: ComposerLLMCall,
    source: ProviderAuditSource,
    public_call: Mapping[str, Any],
) -> VisibleResponseGapWire:
    """Project original source labels as DATA, without writer authority."""
    if type(call) is not ComposerLLMCall or type(source) is not ProviderAuditSource:
        raise AuditIntegrityError("Visible response source projection requires nominal DATA")
    return {
        "_kind": VISIBLE_RESPONSE_GAP_KIND,
        "schema": "composer.visible-response-gap.v1",
        "session_id": source.session_id,
        "operation_id": source.operation_id,
        "operation_epoch": source.operation_epoch,
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
