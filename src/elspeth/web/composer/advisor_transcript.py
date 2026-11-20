"""Owned visible conversation evidence for every Advisor entry path.

This is a review projection, not a second provider protocol. Native thinking,
private diagnostics and static authoring prompts never enter this module.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast, final, get_args
from uuid import UUID

from pydantic import JsonValue

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import is_lower_sha256_hex, stable_hash
from elspeth.contracts.prompt_guard import PromptGuardContentPart
from elspeth.web.composer.context_budget import (
    ContextBudgetPolicy,
    ContextBudgetRefusal,
    SheddableKind,
    SheddableSegment,
    decide_context_budget,
    escaped_bytes,
)
from elspeth.web.composer.control_messages import replay_composer_control_message
from elspeth.web.composer.prompt_guard_rendering import JsonPath, _json_inspection_text, json_content_parts
from elspeth.web.composer.visible_tool_event import (
    VISIBLE_RESPONSE_GAP_KIND,
    VISIBLE_TOOL_EVENT_KIND,
    VisibleToolEvent,
    validate_visible_response_gap,
)
from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND, COMPOSER_WITHHELD_REPLY_SCHEMA, WithheldReplyOrigin
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.validation import _redact_sensitive_content

type TranscriptBlobReader = Callable[[str], tuple[Mapping[str, Any], bytes] | None]


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return None
    return value if type(value) is dict else None


def _redact_text(text: str) -> str:
    """Scan the complete authored scalar with the central contextual redactor.

    Token-by-token scans lose assignment context and miss secrets. Authored
    JSON keys grant no identity exemption; owned links live in typed event
    metadata outside this scalar.
    """
    return _redact_sensitive_content(text)


def _visible_calls(calls: object, *, received: bool) -> list[dict[str, Any]]:
    if calls is None:
        return []
    if not isinstance(calls, list | tuple):
        raise AuditIntegrityError("Visible assistant tool calls are not an owned sequence")
    selected: list[dict[str, Any]] = []
    identities: set[str] = set()
    for call in calls:
        if not isinstance(call, Mapping) or "_kind" in call:
            raise AuditIntegrityError("Visible assistant request has an invalid call envelope")
        function = call.get("function")
        if (
            type(call.get("id")) is not str
            or call.get("type") != "function"
            or not isinstance(function, Mapping)
            or type(function.get("name")) is not str
            or type(function.get("arguments")) is not str
        ):
            raise AuditIntegrityError("Visible assistant request has invalid function fields")
        if call["id"] in identities:
            raise AuditIntegrityError("Visible assistant request repeats a tool identity")
        identities.add(call["id"])
        projected: dict[str, Any] = {
            "tool_call_id": call["id"],
            "tool_name": function["name"],
            "received_arguments": function["arguments"] if received else None,
            "argument_provenance": "provider_received" if received else "persisted_effective_original_unavailable",
        }
        if received:
            projected["effective_arguments_ref"] = "received_arguments"
        else:
            projected["effective_arguments"] = function["arguments"]
        selected.append(projected)
    return selected


def _wire_event(message: Mapping[str, Any], event_id: str) -> dict[str, Any]:
    role = message.get("role")
    content = message.get("content")
    if role not in {"assistant", "user", "system", "tool"} or (content is not None and type(content) is not str):
        raise AuditIntegrityError("Visible current message has unsupported role or content")
    event: dict[str, Any] = {
        "event_id": event_id,
        "source": "current_request",
        "role": role,
        "authorship": "composer" if role == "assistant" else "tool" if role == "tool" else "backend_control",
        "content": content,
    }
    if role == "assistant":
        event["tool_calls"] = _visible_calls(message.get("tool_calls"), received=True)
    if role == "tool":
        call_id = message.get("tool_call_id")
        if type(call_id) is not str:
            raise AuditIntegrityError("Visible tool result lacks its exact request identity")
        event["tool_call_id"] = call_id
        event["primary_delivery"] = "not_yet_confirmed_by_a_successful_prepared_request"
    return event


@final
@dataclass(frozen=True, slots=True)
class AdvisorTranscriptHistory:
    """A session/actor/request-bound snapshot of authenticated stored rows."""

    session_id: str
    actor_user_id: str
    request_id: str
    request_hash: str
    events_json: str = field(repr=False)

    @classmethod
    def from_records(
        cls,
        *,
        session_id: str,
        actor_user_id: str,
        current_request_id: str,
        request: str,
        records: Sequence[ChatMessageRecord],
    ) -> AdvisorTranscriptHistory:
        events: list[dict[str, Any]] = []
        row_ids: set[str] = set()
        prior_sequence: int | None = None
        found_current = False
        public_call_records: dict[str, tuple[Mapping[str, Any], dict[str, Any]]] = {}
        for row in records:
            if type(row) is not ChatMessageRecord or str(row.session_id) != session_id or str(row.id) in row_ids:
                raise AuditIntegrityError("Advisor history contains a foreign or duplicate session row")
            row_ids.add(str(row.id))
            if row.sequence_no is not None:
                if prior_sequence is not None and row.sequence_no <= prior_sequence:
                    raise AuditIntegrityError("Advisor history is not in persisted chronological order")
                prior_sequence = row.sequence_no
            current = str(row.id) == current_request_id
            if current:
                if row.role != "user" or row.writer_principal not in {"route_user_message", "session_fork"} or row.content != request:
                    raise AuditIntegrityError("Advisor current request lacks authenticated human provenance")
                found_current = True
            control = replay_composer_control_message(
                stored_role=row.role,
                writer_principal=row.writer_principal,
                content=row.content,
                tool_calls=row.tool_calls,
            )
            withheld = None
            response_gaps = [envelope for envelope in row.tool_calls or () if envelope.get("_kind") == VISIBLE_RESPONSE_GAP_KIND]
            public_calls = [envelope for envelope in row.tool_calls or () if envelope.get("_kind") == "llm_call_audit"]
            checkpointed_public_event = None
            if response_gaps:
                if len(response_gaps) == 1 and not public_calls:
                    # The joined writer filters a public call already durably
                    # checkpointed by its provider owner. Bind the remaining
                    # coverage row to that exact earlier public record; never
                    # duplicate its token accounting or infer response bytes.
                    recorded_call_id = response_gaps[0].get("provider_call_id")
                    if type(recorded_call_id) is not str:
                        raise AuditIntegrityError("Visible response coverage lacks its physical call identity")
                    recorded = public_call_records.get(recorded_call_id)
                    if recorded is not None:
                        public_calls = [recorded[0]]
                        checkpointed_public_event = recorded[1]
                if row.role != "audit" or row.writer_principal != "compose_loop" or len(response_gaps) != 1 or len(public_calls) != 1:
                    raise AuditIntegrityError("Visible response coverage lacks its exact public Composer audit")
                validate_visible_response_gap(response_gaps[0], public_calls[0])
            visible_envelopes = [envelope for envelope in row.tool_calls or () if envelope.get("_kind") == VISIBLE_TOOL_EVENT_KIND]
            invocation_envelopes = [
                envelope for envelope in row.tool_calls or () if envelope.get("_kind") == "audit" and "invocation" in envelope
            ]
            public_invocations = (
                bool(invocation_envelopes)
                and len(invocation_envelopes) + len(visible_envelopes) == len(row.tool_calls or ())
                and all(
                    envelope.get("_kind") == "audit" and isinstance(envelope.get("invocation"), Mapping)
                    for envelope in invocation_envelopes
                )
            )
            if row.role == "audit" and row.tool_calls is not None and len(row.tool_calls) == 1:
                envelope = row.tool_calls[0]
                if envelope.get("_kind") == COMPOSER_WITHHELD_REPLY_KIND:
                    if (
                        row.writer_principal != "compose_loop"
                        or envelope.get("schema") != COMPOSER_WITHHELD_REPLY_SCHEMA
                        or envelope.get("origin") not in get_args(WithheldReplyOrigin)
                        or envelope.get("content_hash") != hashlib.sha256(row.content.encode()).hexdigest()
                    ):
                        raise AuditIntegrityError("Withheld visible reply lacks its stored content binding")
                    withheld = envelope
            public_call_evidence = bool(public_calls) and row.role == "audit" and row.writer_principal == "compose_loop"
            if row.role == "audit" and control is None and withheld is None and not public_invocations and not public_call_evidence:
                # Private provider snapshots, native metadata and public call
                # diagnostics are not visible conversation events.
                continue
            event: dict[str, Any] = {
                "event_id": str(row.id),
                "source": "persisted_session",
                "sequence_no": row.sequence_no,
                "role": (
                    control["role"]
                    if control is not None
                    else "assistant"
                    if withheld is not None
                    else "tool"
                    if row.role == "audit" and public_invocations
                    else row.role
                ),
                "writer_principal": row.writer_principal,
                "operation_id": str(row.operation_id) if row.operation_id is not None else None,
                "created_at": row.created_at.isoformat(),
                "authorship": (
                    "human"
                    if row.role == "user" and row.writer_principal in {"route_user_message", "session_fork"}
                    else "backend_control"
                    if control is not None or row.role in {"system", "user"}
                    else "composer"
                    if row.role == "assistant" or withheld is not None
                    else "tool"
                ),
            }
            if current:
                event["content_ref"] = "complete_original_user_request_section"
            else:
                event["content"] = control["content"] if control is not None else row.content
            if public_call_evidence:
                public_metadata = public_calls[0].get("call")
                if (
                    len(public_calls) != 1
                    or not isinstance(public_metadata, Mapping)
                    or (public_metadata.get("call_id") is not None and type(public_metadata["call_id"]) is not str)
                ):
                    raise AuditIntegrityError("Visible public call metadata lacks its physical identity")
                gap = response_gaps[0] if response_gaps else None
                event["authorship"] = "backend_public_call_metadata"
                event["visible_response_coverage_gap"] = {
                    "provider_call_id": public_metadata.get("call_id"),
                    "physical_call_identity": "recorded" if public_metadata.get("call_id") is not None else "unavailable",
                    "status": public_metadata.get("status"),
                    "reason": gap["reason"]
                    if gap is not None
                    else "Historical public call metadata contains no response bytes; unrecorded visible fields are unavailable.",
                    "operation_provenance": (
                        "historical_operation_binding_unavailable"
                        if gap is None
                        else "write_time_COMPOSE_attested"
                        if gap["session_id"] == session_id
                        else "copied_source_session_not_child_authority"
                    ),
                }
                if checkpointed_public_event is not None:
                    checkpointed_public_event["visible_response_coverage_gap"] = event["visible_response_coverage_gap"]
                    checkpointed_public_event["response_coverage_record_id"] = str(row.id)
                    continue
                if public_metadata.get("call_id") is not None and public_metadata["call_id"] in public_call_records:
                    raise AuditIntegrityError("Advisor public call history repeats a physical identity")
                if public_metadata.get("call_id") is not None:
                    public_call_records[public_metadata["call_id"]] = (public_calls[0], event)
            if row.raw_content is not None and row.raw_content != row.content:
                if row.content.startswith(row.raw_content):
                    event["provider_visible_prose_char_range"] = [0, len(row.raw_content)]
                else:
                    event["provider_visible_prose_before_backend_augmentation"] = row.raw_content
            if row.role == "assistant":
                event["tool_calls"] = _visible_calls(row.tool_calls, received=False)
            if withheld is not None:
                event["publication_status"] = "withheld_visible_provider_reply"
                event["withheld_origin"] = withheld["origin"]
            if row.role == "tool" or (row.role == "audit" and public_invocations):
                event["tool_call_id"] = row.tool_call_id
                event["parent_assistant_id"] = str(row.parent_assistant_id) if row.parent_assistant_id is not None else None
                event["history_availability"] = "stored_projection_original_result_bytes_unavailable"
                event["primary_delivery"] = "historical_provider_delivery_not_reconstructed"
                if invocation_envelopes:
                    # Legacy invocation rows carry the actual tool event, not
                    # a provider-request snapshot. Validate both canonical
                    # bindings before projecting their already-redacted bytes.
                    invocations = []
                    for envelope in invocation_envelopes:
                        if envelope.get("_kind") != "audit" or not isinstance(envelope.get("invocation"), Mapping):
                            raise AuditIntegrityError("Historical tool row has unsupported audit provenance")
                        invocation = envelope["invocation"]
                        if (
                            type(invocation.get("tool_call_id")) is not str
                            or type(invocation.get("tool_name")) is not str
                            or invocation.get("status") not in {"success", "arg_error", "plugin_crash", "cancelled"}
                            or invocation.get("session_id") not in {None, session_id}
                            or (row.tool_call_id is not None and invocation.get("tool_call_id") != row.tool_call_id)
                        ):
                            raise AuditIntegrityError("Historical tool audit belongs to a different session or call")
                        for stem in ("arguments", "result"):
                            canonical = invocation.get(stem + "_canonical")
                            digest = invocation.get(stem + "_hash")
                            if canonical is None and digest is None and stem == "result":
                                continue
                            if type(canonical) is not str or hashlib.sha256(canonical.encode()).hexdigest() != digest:
                                raise AuditIntegrityError("Historical tool audit canonical binding is invalid")
                        invocations.append(
                            {
                                key: invocation[key]
                                for key in (
                                    "tool_call_id",
                                    "tool_name",
                                    "status",
                                    "arguments_canonical",
                                    "result_canonical",
                                    "arguments_hash",
                                    "result_hash",
                                    "error_class",
                                    "error_message",
                                )
                                if key in invocation
                            }
                        )
                    event["legacy_invocations"] = invocations
                    event["invocation_provenance"] = "public_canonical_audit_without_parent_protocol"
                    if len(invocations) == 1 and row.content == invocations[0].get("result_canonical"):
                        del event["content"]
                        event["content_ref"] = "legacy_invocations[0].result_canonical"
                if visible_envelopes:
                    if len(visible_envelopes) != 1:
                        raise AuditIntegrityError("Stored tool row repeats visible evidence")
                    visible = VisibleToolEvent.from_envelope(visible_envelopes[0])
                    if row.writer_principal != "compose_loop":
                        event["visible_event_availability"] = "unavailable_writer_custody"
                    elif visible.session_id != session_id or (
                        row.role == "tool" and visible.parent_assistant_id != str(row.parent_assistant_id)
                    ):
                        # Fork copies retain source envelopes. They are source
                        # provenance, never live child-session authority.
                        event["visible_event_availability"] = "unavailable_original_session_or_operation_binding"
                    else:
                        parent = next(
                            (item for item in events if isinstance(item, dict) and item["event_id"] == str(row.parent_assistant_id)), None
                        )
                        matches = (
                            [call for call in parent.get("tool_calls", []) if call["tool_call_id"] == row.tool_call_id]
                            if parent is not None
                            else []
                        )
                        if invocation_envelopes:
                            legacy = event["legacy_invocations"]
                            if not isinstance(legacy, list) or len(legacy) != 1 or not isinstance(legacy[0], dict):
                                raise AuditIntegrityError("Visible invocation lacks its exact public canonical event")
                            linked = legacy[0]
                            display_args = linked["arguments_canonical"]
                            call_id, name = linked["tool_call_id"], linked["tool_name"]
                        elif parent is not None and parent.get("writer_principal") == "compose_loop" and len(matches) == 1:
                            linked = matches[0]
                            display_args = linked["effective_arguments"]
                            call_id, name = row.tool_call_id, linked["tool_name"]
                        else:
                            raise AuditIntegrityError("Visible tool evidence lacks its exact owned parent")
                        visible.validate_recorded_link(
                            session_id=session_id,
                            operation_id=None,
                            tool_call_id=call_id,
                            tool_name=name,
                            display_arguments=display_args,
                            display_result=row.content,
                            parent_assistant_id=str(row.parent_assistant_id) if row.parent_assistant_id is not None else None,
                        )
                        event["creator_operation_id"] = visible.operation_id
                        if parent is not None:
                            prior_operation = parent.get("creator_operation_id")
                            if prior_operation is not None and prior_operation != visible.operation_id:
                                raise AuditIntegrityError("One persisted assistant carries conflicting creator operations")
                            parent["creator_operation_id"] = visible.operation_id
                        payload = json.loads(visible.payload_json)
                        linked["received_arguments"] = payload["received_arguments"]
                        linked["effective_arguments"] = (
                            payload["effective_arguments"] if payload["effective_arguments"] is not None else display_args
                        )
                        linked.pop("effective_arguments_ref", None)
                        linked["argument_provenance"] = "ordinary_received_and_effective_event"
                        if payload["selected_result"] is not None:
                            event["content"] = payload["selected_result"]
                            event.pop("content_ref", None)
                        event["selected_result_retained"] = payload["selected_result"] is not None
                        event["visible_event_availability"] = payload["availability_gaps"]
                        event["history_availability"] = "ordinary_visible_event_secret_redacted"
                elif not invocation_envelopes and row.tool_calls is not None:
                    raise AuditIntegrityError("Historical tool row has unsupported provenance")
            events.append(event)
        if not found_current:
            raise AuditIntegrityError("Advisor history is missing its exact current human request")
        return cls(session_id, actor_user_id, current_request_id, stable_hash(request), json.dumps(events, ensure_ascii=False))

    def validate_for(self, *, session_id: str | None, actor_user_id: str | None, request_id: str, request: str) -> None:
        if (self.session_id, self.actor_user_id, self.request_id, self.request_hash) != (
            session_id,
            actor_user_id,
            request_id,
            stable_hash(request),
        ):
            raise AuditIntegrityError("Advisor history belongs to another authenticated request")


def _redacted_events(events: list[dict[str, Any]]) -> list[JsonValue]:
    """Redact authored text only; preserve owned ordering and linkage metadata."""
    projected: list[JsonValue] = []
    for event in events:
        value = dict(event)
        for key in ("content", "provider_visible_prose_before_backend_augmentation"):
            if type(value.get(key)) is str:
                value[key] = _redact_text(value[key])
        if "tool_calls" in value:
            value["tool_calls"] = [
                dict(call)
                | {
                    key: _redact_text(call[key]) if type(call[key]) is str else call[key]
                    for key in ("received_arguments", "effective_arguments")
                    if key in call
                }
                for call in value["tool_calls"]
            ]
        if "legacy_invocations" in value:
            value["legacy_invocations"] = [
                dict(call)
                | {
                    key: _redact_text(call[key]) if type(call.get(key)) is str else call.get(key)
                    for key in ("arguments_canonical", "result_canonical", "error_message")
                }
                for call in value["legacy_invocations"]
            ]
        if "recovered_read" in value:
            value["recovered_read"] = dict(value["recovered_read"]) | {
                "content": _redact_sensitive_content(value["recovered_read"]["content"])
            }
        projected.append(cast(JsonValue, value))
    return projected


def _recover_read_bodies(events: list[dict[str, Any]], reader: TranscriptBlobReader | None, session_id: str | None) -> None:
    """Recover only a recorded successful read, through current blob custody.

    Parent/request identity selects the blob. The stored result's hash and
    character range select the exact historical body; redacted filenames or
    result-side blob ids never supply access authority.
    """
    assistants = {event["event_id"]: event for event in events if event["role"] == "assistant"}
    cache: dict[str, tuple[Mapping[str, Any], bytes] | None] = {}
    for event in events:
        if event["role"] != "tool" or event["source"] != "persisted_session":
            continue
        if event.get("writer_principal") != "compose_loop":
            event["read_body_availability"] = "unavailable_tool_writer_custody"
            continue
        selections: list[tuple[str, str, str]] = []
        legacy = event.get("legacy_invocations")
        if legacy is not None:
            selections = [
                (call["tool_name"], call["arguments_canonical"], call["result_canonical"])
                for call in legacy
                if call["status"] == "success" and call["result_canonical"] is not None
            ]
        else:
            parent = assistants.get(event.get("parent_assistant_id"))
            if parent is not None and (
                parent.get("writer_principal") != "compose_loop"
                or parent.get("creator_operation_id", parent.get("operation_id")) is None
                or parent.get("creator_operation_id", parent.get("operation_id"))
                != event.get("creator_operation_id", event.get("operation_id"))
            ):
                event["read_body_availability"] = "unavailable_parent_writer_or_operation_binding"
                continue
            calls = (
                []
                if parent is None
                else [call for call in parent.get("tool_calls", []) if call["tool_call_id"] == event.get("tool_call_id")]
            )
            if len(calls) == 1 and type(event.get("content")) is str:
                selections = [(calls[0]["tool_name"], calls[0]["effective_arguments"], event["content"])]
            elif event.get("tool_call_id") is not None:
                event["read_body_availability"] = "unavailable_parent_request_binding"
        for name, arguments_text, result_text in selections:
            if name not in {"get_blob_content", "read_attachment"}:
                continue
            if event.get("selected_result_retained") is True:
                event["read_body_availability"] = "ordinary_visible_result_secret_redacted_no_blob_reread"
                continue
            arguments, result = _json_object(arguments_text), _json_object(result_text)
            if arguments is None or result is None or result.get("success") is not True:
                event["read_body_availability"] = "unavailable_successful_result_binding"
                continue
            data = result.get("data")
            blob_id = arguments.get("blob_id")
            if type(data) is not dict or type(blob_id) is not str:
                event["read_body_availability"] = "unavailable_recorded_selector"
                continue
            try:
                UUID(blob_id)
            except ValueError:
                event["read_body_availability"] = "unavailable_recorded_selector"
                continue
            digest, total, start, end = (data.get(key) for key in ("content_hash", "total_chars", "start_char", "end_char"))
            if (
                not is_lower_sha256_hex(digest)
                or type(total) is not int
                or type(start) is not int
                or type(end) is not int
                or not 0 <= start <= end <= total
                or (arguments.get("expected_content_hash") is not None and arguments["expected_content_hash"] != digest)
                or (name == "get_blob_content" and arguments.get("offset_chars", 0) != start)
                or (name == "read_attachment" and (start != 0 or end != total or data.get("truncated") is not False))
            ):
                event["read_body_availability"] = "unavailable_recorded_range_or_hash"
                continue
            if reader is None:
                event["read_body_availability"] = "unavailable_current_blob_authority"
                continue
            if blob_id not in cache:
                cache[blob_id] = reader(blob_id)
            current = cache[blob_id]
            if current is None:
                event["read_body_availability"] = "unavailable_current_blob_access"
                continue
            metadata, raw = current
            if (
                metadata.get("id") != blob_id
                or metadata.get("status") != "ready"
                or metadata.get("session_id") != session_id
                or metadata.get("content_hash") != digest
                or hashlib.sha256(raw).hexdigest() != digest
                or metadata.get("size_bytes") != len(raw)
                or data.get("size_bytes") != len(raw)
            ):
                event["read_body_availability"] = "unavailable_stale_or_mismatched_blob"
                continue
            try:
                text = raw.decode("utf-8")
                # Full-file inspection precedes slicing, exactly as the
                # existing reader. No credential or traversal cap bypass.
                require_no_credential_material({"content": text}, surface="advisor_historical_read_recovery")
            except (UnicodeDecodeError, CredentialMaterialRefused):
                event["read_body_availability"] = "unavailable_blob_disclosure_refused"
                continue
            if len(text) != total or any(key in data and data[key] != metadata.get(key) for key in ("created_by", "creation_modality")):
                event["read_body_availability"] = "unavailable_recorded_origin_or_extent"
                continue
            event["read_body_availability"] = "recovered_exact_recorded_range_current_custody_verified"
            if data.get("content") == text[start:end]:
                event["read_body_availability"] = "stored_body_matches_recorded_range_current_custody_verified"
                continue
            event["recovered_read"] = {
                "blob_id": blob_id,
                "content_hash": digest,
                "size_bytes": len(raw),
                "total_chars": total,
                "start_char": start,
                "end_char": end,
                "content": text[start:end],
                "created_by": metadata.get("created_by"),
                "creation_modality": metadata.get("creation_modality"),
                "origin_provenance": (
                    "recorded_origin_verified"
                    if "created_by" in data and "creation_modality" in data
                    else "current_blob_origin_historical_origin_unavailable"
                ),
                "primary_delivery": "historical_provider_delivery_not_reconstructed",
            }


# The availability gap a transcript reduced to fit the context budget carries.
CONTEXT_BUDGET_TRANSCRIPT_OMISSION_GAP: Final[str] = "oldest_events_omitted_for_context_budget"
# The gap an Advisor transcript carries when it sends earlier tool results as a stub.
CONTEXT_BUDGET_TRANSCRIPT_STUB_GAP: Final[str] = "older_tool_results_stubbed_for_context_budget"
# What an earlier tool result reads once the Advisor's context budget stubs it.
# The compose loop's stub tells the model to call the tool again; the Advisor
# has no tools, so its stub states only the omission.
ADVISOR_TRANSCRIPT_TOOL_RESULT_STUB: Final[str] = "[Result omitted by ELSPETH to fit the context budget.]"
# The gap an Advisor transcript carries when it shortens earlier user requests.
CONTEXT_BUDGET_TRANSCRIPT_USER_REQUEST_GAP: Final[str] = "older_user_requests_shortened_for_context_budget"
# How much of an earlier user request the last reduction tier keeps, in characters.
ADVISOR_TRANSCRIPT_USER_REQUEST_KEEP_CHARS: Final[int] = 2_000
# What follows the kept start of a shortened earlier user request; it carries
# the request's original length so the Advisor knows how much it cannot see.
ADVISOR_TRANSCRIPT_USER_REQUEST_MARKER: Final[str] = (
    "\n[ELSPETH shortened this earlier user request to its first {kept:,} of {length:,} characters to fit the context budget.]"
)
# Worst-case wire bytes a reduced Advisor request adds that the event savings do
# not count: the longer reduced transcript header with its counts, the
# ``context_budget_reduction`` object and the three gaps, all JSON-escaped
# inside the user message, and the separator a saving over-counts when the last
# earlier event goes. Those are under 850; the rest is margin. A shortened
# request's own marker is counted in its saving.
ADVISOR_TRANSCRIPT_REDUCTION_ALLOWANCE_BYTES: Final[int] = 1024

# Where the current request's full text lives; the event carrying one of these
# as its ``content_ref`` stands for the current request.
AdvisorRequestReference = Literal["complete_original_user_request_section", "initial_planner_request.intent"]
_REQUEST_REFERENCES: Final[frozenset[str]] = frozenset(get_args(AdvisorRequestReference))


@final
@dataclass(frozen=True, slots=True)
class AdvisorTranscriptReduction:
    """Exact counts an Advisor transcript reduced to fit the context budget discloses."""

    events_omitted: int
    tool_results_stubbed: int
    user_requests_shortened: int = 0

    def __post_init__(self) -> None:
        counts = (self.events_omitted, self.tool_results_stubbed, self.user_requests_shortened)
        if any(type(count) is not int for count in counts):
            raise TypeError("Advisor transcript reduction counts must be exact integers")
        if min(counts) < 0 or sum(counts) == 0:
            raise ValueError("Advisor transcript reduction must omit, stub or shorten at least one earlier event")


def _earlier_event_limit(events: Sequence[dict[str, JsonValue]]) -> int:
    """Index of the current request's reference event: every event before it belongs to an earlier turn.

    The reference event, any persisted row after it (an interrupted prefix of
    this same request) and every event observed this turn are this turn's
    evidence. Without a reference event no event is known to be earlier.
    """
    return next(
        (index for index, event in enumerate(events) if "content_ref" in event and event["content_ref"] in _REQUEST_REFERENCES),
        0,
    )


def _stubbed_tool_event(event: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    """The event with every tool-result body replaced by the Advisor stub; None when it carries none.

    The event is this module's own projection (see ``from_records``):
    ``legacy_invocations`` is a list of objects and ``recovered_read`` an
    object with a string ``content``.
    """
    if event["role"] != "tool":
        return None
    stubbed = dict(event)
    changed = False
    if "content" in event and type(event["content"]) is str and event["content"] != ADVISOR_TRANSCRIPT_TOOL_RESULT_STUB:
        stubbed["content"] = ADVISOR_TRANSCRIPT_TOOL_RESULT_STUB
        changed = True
    if "legacy_invocations" in event:
        invocations: list[JsonValue] = []
        for invocation in cast(list[dict[str, JsonValue]], event["legacy_invocations"]):
            if (
                "result_canonical" in invocation
                and type(invocation["result_canonical"]) is str
                and invocation["result_canonical"] != ADVISOR_TRANSCRIPT_TOOL_RESULT_STUB
            ):
                invocation = dict(invocation) | {"result_canonical": ADVISOR_TRANSCRIPT_TOOL_RESULT_STUB}
                changed = True
            invocations.append(invocation)
        stubbed["legacy_invocations"] = invocations
    if "recovered_read" in event:
        recovered = cast(dict[str, JsonValue], event["recovered_read"])
        if recovered["content"] != ADVISOR_TRANSCRIPT_TOOL_RESULT_STUB:
            stubbed["recovered_read"] = recovered | {"content": ADVISOR_TRANSCRIPT_TOOL_RESULT_STUB}
            changed = True
    return stubbed if changed else None


def _event_wire_bytes(event: dict[str, JsonValue]) -> int:
    """An event's escaped share of the transcript inside a request message, plus its separator."""
    return escaped_bytes(json.dumps(event, ensure_ascii=False)) + 1


def _shortened_user_event(event: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    """A human event cut to its first characters plus a marker with its original length; None when it is short enough."""
    content = event["content"] if "content" in event else None
    if type(content) is not str or len(content) <= ADVISOR_TRANSCRIPT_USER_REQUEST_KEEP_CHARS:
        return None
    marker = ADVISOR_TRANSCRIPT_USER_REQUEST_MARKER.format(kept=ADVISOR_TRANSCRIPT_USER_REQUEST_KEEP_CHARS, length=len(content))
    return event | {"content": content[:ADVISOR_TRANSCRIPT_USER_REQUEST_KEEP_CHARS] + marker}


def _within_shed_above(policy: ContextBudgetPolicy, request_bytes: int) -> bool:
    """Whether a request of this many wire bytes is at or under the high mark, priced as the budgeter prices it with no anchor."""
    return (
        math.ceil(request_bytes / policy.fallback_bytes_per_token) <= policy.shed_above * policy.max_prompt_tokens
        and request_bytes <= policy.max_request_bytes
    )


@final
@dataclass(frozen=True, slots=True)
class AdvisorVisibleTranscript:
    """Immutable available visible chronology, possibly reduced to fit a context budget (see ``availability_gaps``); all text is untrusted."""

    request: str = field(repr=False)
    binding: str
    provider_json: str = field(repr=False)
    semantic_binding: str
    # Set only on the transcript an Advisor request sends after its context
    # budget reduced it; the header that introduces it states these counts.
    reduction: AdvisorTranscriptReduction | None = None

    def with_request_reference(
        self,
        reference: AdvisorRequestReference,
    ) -> AdvisorVisibleTranscript:
        """Point to the existing full request in another owned entry path."""
        payload = json.loads(self.provider_json)
        payload["current_request_ref"] = reference
        for event in payload["events"]:
            if event.get("content_ref") == "complete_original_user_request_section":
                event["content_ref"] = reference
        return AdvisorVisibleTranscript(
            self.request,
            stable_hash({"transcript": self.binding, "request_reference": reference}),
            json.dumps(payload, ensure_ascii=False),
            self.semantic_binding,
            # Renaming the reference changes no event, so a reduction's counts stay exact.
            self.reduction,
        )

    def event_bytes(self) -> tuple[int, ...]:
        """UTF-8 bytes each event occupies in ``provider_json``, plus one for its separator.

        Measured on the event's own serialization inside ``provider_json``, not
        on canonical JSON, whose number rendering can be longer: removing
        events while one remains removes at least these bytes, because each
        separator is two. Removing a lone last event over-counts by one.
        """
        payload = json.loads(self.provider_json)
        return tuple(len(json.dumps(event, ensure_ascii=False).encode("utf-8")) + 1 for event in payload["events"])

    def event_wire_bytes(self) -> tuple[int, ...]:
        """Wire bytes each event occupies once the transcript is carried as a request message's content, plus one.

        The transcript travels inside a JSON string, so each event's
        serialization is escaped once more; escaping is per character, so an
        event's share is its own escaped length. The separator escapes to its
        own two bytes, and the plus-one carries :meth:`event_bytes`'s
        convention: a lower bound while one event remains, one over when the
        lone last event goes.
        """
        payload = json.loads(self.provider_json)
        return tuple(escaped_bytes(json.dumps(event, ensure_ascii=False)) + 1 for event in payload["events"])

    def sheddable_event_indices(self) -> tuple[int, ...]:
        """Indices of every event except the current request's reference.

        A legacy ``content_ref`` (``legacy_invocations[0].result_canonical``)
        points inside its own event, so dropping that event leaves no
        reference dangling.
        """
        payload = json.loads(self.provider_json)
        return tuple(
            index
            for index, event in enumerate(payload["events"])
            if "content_ref" not in event or event["content_ref"] not in _REQUEST_REFERENCES
        )

    def without_events(self, indices: frozenset[int]) -> AdvisorVisibleTranscript:
        """Drop sheddable events to fit a context budget; the gap is disclosed once.

        ``binding`` describes the reduced transcript; ``request`` and
        ``semantic_binding`` are unchanged. No indices returns this transcript.
        """
        if not indices:
            return self
        if self.reduction is not None:
            raise ValueError("a transcript already reduced for an Advisor request cannot be reduced again")
        if not indices <= frozenset(self.sheddable_event_indices()):
            raise ValueError("only sheddable transcript events can be omitted")
        payload = json.loads(self.provider_json)
        payload["events"] = [event for index, event in enumerate(payload["events"]) if index not in indices]
        if CONTEXT_BUDGET_TRANSCRIPT_OMISSION_GAP not in payload["availability_gaps"]:
            payload["availability_gaps"].append(CONTEXT_BUDGET_TRANSCRIPT_OMISSION_GAP)
        return AdvisorVisibleTranscript(
            self.request,
            stable_hash({"transcript": self.binding, "omitted_events": sorted(indices)}),
            json.dumps(payload, ensure_ascii=False),
            self.semantic_binding,
        )

    def context_budget_segments(self) -> tuple[SheddableSegment, ...]:
        """What an Advisor request may leave out of this transcript, oldest first, as context-budget segments.

        Only events before the current request's reference are eligible, and
        never a human-authored one. A tool event with a result body yields a
        ``stub:<index>`` segment (its saving: the body's bytes less the
        stub's) and every eligible event a ``drop:<index>`` segment (its
        saving: what remains of it once stubbed). Stubs rank before drops, so
        a decision stubs every eligible result before it drops any event, and
        a dropped event's two savings add up to its whole size.
        """
        events = json.loads(self.provider_json)["events"]
        stubs: list[SheddableSegment] = []
        drops: list[SheddableSegment] = []
        for index, event in enumerate(events[: _earlier_event_limit(events)]):
            if event["authorship"] == "human":
                continue
            size = _event_wire_bytes(event)
            stubbed = _stubbed_tool_event(event)
            stubbed_size = _event_wire_bytes(stubbed) if stubbed is not None else size
            if stubbed_size < size:
                stubs.append(SheddableSegment(kind=SheddableKind.TOOL_RESULT, key=f"stub:{index}", saving_bytes=size - stubbed_size))
                size = stubbed_size
            drops.append(SheddableSegment(kind=SheddableKind.HATCH_EVENT, key=f"drop:{index}", saving_bytes=size))
        return (*stubs, *drops)

    def user_request_segments(self) -> tuple[SheddableSegment, ...]:
        """Earlier user requests the last reduction tier may shorten, oldest first, as ``shorten:<index>`` segments.

        Only a human event before the current request's reference whose text
        is longer than the kept start is eligible; its saving is its size less
        the shortened event's, marker included. The current request and this
        turn's events are never eligible.
        """
        events = json.loads(self.provider_json)["events"]
        segments: list[SheddableSegment] = []
        for index, event in enumerate(events[: _earlier_event_limit(events)]):
            shortened = _shortened_user_event(event) if event["authorship"] == "human" else None
            if shortened is not None:
                saving = _event_wire_bytes(event) - _event_wire_bytes(shortened)
                segments.append(SheddableSegment(kind=SheddableKind.USER_REQUEST, key=f"shorten:{index}", saving_bytes=saving))
        return tuple(segments)

    def reduced(
        self, *, stubbed: frozenset[int], omitted: frozenset[int], shortened: frozenset[int] = frozenset()
    ) -> AdvisorVisibleTranscript:
        """Stub earlier tool results, leave earlier events out and shorten earlier user requests, disclosing exact counts.

        ``binding`` covers this transcript's binding plus the index sets;
        ``request`` and ``semantic_binding`` are unchanged, so leaving evidence
        out is never mistaken for new or lost repair progress. No indices
        returns this transcript.
        """
        if not stubbed and not omitted and not shortened:
            return self
        if self.reduction is not None:
            # Indices name this transcript's events and the counts would restart.
            raise ValueError("a transcript already reduced for an Advisor request cannot be reduced again")
        payload = json.loads(self.provider_json)
        events = payload["events"]
        earlier = list(enumerate(events[: _earlier_event_limit(events)]))
        eligible = {index for index, event in earlier if event["authorship"] != "human"}
        if not omitted <= eligible or not stubbed <= eligible:
            raise ValueError("only earlier, non-human transcript events can be reduced")
        if not shortened <= {index for index, event in earlier if event["authorship"] == "human"}:
            raise ValueError("only earlier user requests can be shortened")
        projected: list[JsonValue] = []
        stubs_sent = 0
        for index, event in enumerate(events):
            if index in omitted:
                continue
            if index in stubbed:
                replacement = _stubbed_tool_event(event)
                if replacement is None:
                    raise ValueError("only an earlier tool result with a body can be stubbed")
                event = replacement
                stubs_sent += 1
            if index in shortened:
                replacement = _shortened_user_event(event)
                if replacement is None:
                    raise ValueError("only an earlier user request longer than the kept start can be shortened")
                event = replacement
            projected.append(event)
        payload["events"] = projected
        for gap, applies in (
            (CONTEXT_BUDGET_TRANSCRIPT_OMISSION_GAP, bool(omitted)),
            (CONTEXT_BUDGET_TRANSCRIPT_STUB_GAP, stubs_sent > 0),
            (CONTEXT_BUDGET_TRANSCRIPT_USER_REQUEST_GAP, bool(shortened)),
        ):
            if applies and gap not in payload["availability_gaps"]:
                payload["availability_gaps"].append(gap)
        reduction = AdvisorTranscriptReduction(
            events_omitted=len(omitted), tool_results_stubbed=stubs_sent, user_requests_shortened=len(shortened)
        )
        payload["context_budget_reduction"] = {
            "older_events_omitted": reduction.events_omitted,
            "older_tool_results_stubbed": reduction.tool_results_stubbed,
            "older_user_requests_shortened": reduction.user_requests_shortened,
        }
        return AdvisorVisibleTranscript(
            self.request,
            stable_hash(
                {"transcript": self.binding, "omitted_events": sorted(omitted)}
                | ({"stubbed_events": sorted(stubbed)} if stubbed else {})
                | ({"shortened_events": sorted(shortened)} if shortened else {})
            ),
            json.dumps(payload, ensure_ascii=False),
            self.semantic_binding,
            reduction,
        )

    def untrusted_content_parts(self) -> tuple[PromptGuardContentPart, ...]:
        payload = json.loads(self.provider_json)
        partitions: dict[JsonPath, tuple[PromptGuardContentPart, ...]] = {}

        def walk(value: JsonValue, path: JsonPath = ()) -> None:
            if type(value) is str:
                # Tool results/arguments are serialized JSON strings inside
                # this review DTO. Inspect their decoded bodies, including
                # Unicode-escaped authored instructions, without changing the
                # exact visible string or relaxing guard trust.
                nested = (
                    _json_object(value)
                    if path
                    and path[-1]
                    in {
                        "content",
                        "received_arguments",
                        "effective_arguments",
                        "arguments_canonical",
                        "result_canonical",
                    }
                    else None
                )
                inspection = _json_inspection_text(json.dumps(nested, ensure_ascii=False)) if nested is not None else value
                extent = max(len(value), len(inspection))
                partitions[path] = tuple(
                    PromptGuardContentPart(value[i : i + 8_000], inspection_text=inspection[i : i + 8_000]) for i in range(0, extent, 8_000)
                )
            elif isinstance(value, dict):
                for key, child in value.items():
                    walk(child, (*path, key))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, (*path, index))

        walk(payload)
        return json_content_parts(self.provider_json, ensure_ascii=False, string_parts=partitions)


def fit_advisor_transcript_to_context_budget(
    transcript: AdvisorVisibleTranscript, *, policy: ContextBudgetPolicy, request_wire_bytes: int
) -> AdvisorVisibleTranscript:
    """Reduce an Advisor request's transcript to the compose loop's context budget, or return it unchanged.

    ``request_wire_bytes`` is the wire size of the whole Advisor request as it
    would be sent with ``transcript`` unreduced. Under ``shed_above`` of the
    window nothing changes, byte for byte. Above it, earlier tool results are
    stubbed and then earlier events dropped, oldest first, toward ``shed_to``.
    Only when those two tiers cannot bring the request under ``shed_above``
    are earlier user requests shortened, oldest first, to their first
    characters, until it is under. The current request and this turn's events
    are never reduced. Each request decides afresh: the Advisor replays no
    cached conversation, so there is no shed state to keep stable. Nothing is
    ever refused here: a request still over the window, or over the byte
    maximum, is sent with everything eligible reduced, and the provider judges
    its own limit.
    """
    segments = transcript.context_budget_segments()
    remaining = request_wire_bytes + ADVISOR_TRANSCRIPT_REDUCTION_ALLOWANCE_BYTES
    shed: tuple[str, ...] = ()
    if segments:
        try:
            decision = decide_context_budget(
                policy=policy, request_bytes=remaining, segments=segments, previously_shed=frozenset(), on_over_estimate="send"
            )
            shed, remaining = decision.shed_keys, decision.request_bytes_after
        except ContextBudgetRefusal as refusal:
            # With on_over_estimate="send" the budgeter never raises its "tokens"
            # refusal (that one is raised only for "refuse"), so a refusal here
            # is the byte ceiling, raised only once every segment is shed. Any
            # other ceiling would be a new budgeter contract, not an Advisor fault.
            if refusal.ceiling != "bytes":
                raise
            shed, remaining = tuple(segment.key for segment in segments), refusal.request_bytes
    shortened: set[int] = set()
    for segment in transcript.user_request_segments():
        if _within_shed_above(policy, remaining):
            break
        shortened.add(int(segment.key.removeprefix("shorten:")))
        remaining -= segment.saving_bytes
    stubbed = frozenset(int(key.removeprefix("stub:")) for key in shed if key.startswith("stub:"))
    omitted = frozenset(int(key.removeprefix("drop:")) for key in shed if key.startswith("drop:"))
    return transcript.reduced(stubbed=stubbed, omitted=omitted, shortened=frozenset(shortened))


@dataclass(slots=True)
class _ObservedMessage:
    message: Mapping[str, Any] = field(repr=False)
    event: dict[str, Any] = field(repr=False)
    pending_adoption: bool = False


@final
class AdvisorTranscriptOwner:
    """One request's append-only event ledger, separate from prompt retention."""

    def __init__(self, *, request: str, history: AdvisorTranscriptHistory | None = None, blob_reader: TranscriptBlobReader | None = None):
        self.request = request
        self._history = history
        self._blob_reader = blob_reader
        self._excluded: list[Mapping[str, Any]] = []
        self._observed: list[_ObservedMessage] = []

    def begin_messages(self, messages: Sequence[Mapping[str, Any]]) -> None:
        """Exclude already-owned history, current request and static setup once."""
        self._excluded.extend(messages)

    def covers_prior_requests(self, contents: Sequence[str]) -> bool:
        """Verify distinct chronological human records before replacing a projection.

        This validates coverage only; repeated words remain separate events and
        never become a chronology deduplication key.
        """
        if self._history is None:
            return False
        humans = iter(
            event["content"] for event in json.loads(self._history.events_json) if event["authorship"] == "human" and "content" in event
        )
        return all(any(content == candidate for candidate in humans) for content in contents)

    def observe_messages(self, messages: Sequence[Mapping[str, Any]]) -> None:
        for message in messages:
            if any(message is excluded for excluded in self._excluded):
                continue
            known = next((entry for entry in self._observed if entry.message is message), None)
            if known is None and message.get("role") == "assistant" and message.get("tool_calls"):
                calls = _visible_calls(message["tool_calls"], received=True)
                identities = [(call["tool_call_id"], call["tool_name"]) for call in calls]
                matches = [
                    entry
                    for entry in self._observed
                    if entry.pending_adoption
                    and [
                        (call["tool_call_id"], call["tool_name"]) for call in cast(list[dict[str, Any]], entry.event.get("tool_calls", []))
                    ]
                    == identities
                ]
                if len(matches) > 1:
                    raise AuditIntegrityError("Current provider replies reused ambiguous tool-call identities")
                if matches:
                    known = matches[0]
                    known.message = message
                    known.pending_adoption = False
            if known is None:
                event = _wire_event(message, f"current:{len(self._observed) + 1}")
                self._observed.append(_ObservedMessage(message, event))
            elif message.get("role") == "assistant":
                current = _visible_calls(message.get("tool_calls"), received=True)
                previous = cast(list[dict[str, Any]], known.event["tool_calls"])
                if [(call["tool_call_id"], call["tool_name"]) for call in current] != [
                    (call["tool_call_id"], call["tool_name"]) for call in previous
                ]:
                    raise AuditIntegrityError("Observed assistant tool identities changed after receipt")
                for original, effective in zip(previous, current, strict=True):
                    arguments = effective["received_arguments"]
                    if original["received_arguments"] != arguments:
                        original["effective_arguments"] = arguments
                        original.pop("effective_arguments_ref", None)
                        original["argument_provenance"] = "provider_received_then_backend_finalized"

    def record_provider_reply(
        self,
        *,
        provider_call_id: str,
        message: Mapping[str, Any],
        admission: Literal["admitted", "received_unadmitted"] = "admitted",
        unavailable_fields: tuple[str, ...] = (),
    ) -> None:
        """Observe visible planner words independently of protocol replay."""
        if type(provider_call_id) is not str or not provider_call_id:
            raise AuditIntegrityError("Visible provider reply lacks its actual physical call identity")
        if any(entry.event.get("provider_call_id") == provider_call_id for entry in self._observed):
            raise AuditIntegrityError("Visible provider reply was registered twice")
        event = _wire_event(message, f"provider:{provider_call_id}")
        event["provider_call_id"] = provider_call_id
        event["admission"] = admission
        if unavailable_fields:
            event["unavailable_visible_fields"] = list(unavailable_fields)
        self._observed.append(_ObservedMessage(message, event, True))

    def admit_provider_reply(self, provider_call_id: str | None) -> None:
        """Mark the already received event admitted after owned settlement."""
        matches = [entry for entry in self._observed if entry.event.get("provider_call_id") == provider_call_id]
        if provider_call_id is None or len(matches) != 1 or matches[0].event.get("admission") != "received_unadmitted":
            raise AuditIntegrityError("Admitted planner response differs from its received visible event")
        matches[0].event["admission"] = "admitted"

    def current_tool_strings(self, tool_call_id: str, tool_name: str) -> tuple[str | None, str | None, str | None]:
        """Return actual owned received/effective/selected strings for storage."""
        self.observe_messages([entry.message for entry in self._observed])
        calls = [
            call
            for entry in self._observed
            if entry.event["role"] == "assistant"
            for call in entry.event.get("tool_calls", [])
            if call["tool_call_id"] == tool_call_id and call["tool_name"] == tool_name
        ]
        results = [
            entry.event for entry in self._observed if entry.event["role"] == "tool" and entry.event.get("tool_call_id") == tool_call_id
        ]
        if len(calls) > 1 or len(results) > 1:
            raise AuditIntegrityError("Visible tool storage has ambiguous current call identity")
        call = calls[0] if calls else None
        received = call["received_arguments"] if call is not None else None
        effective = call.get("effective_arguments", received) if call is not None else None
        return received, effective, results[0]["content"] if results else None

    def candidate_event_id(self, *, provider_call_id: str | None, candidate: str) -> str | None:
        for entry in self._observed:
            if entry.event.get("provider_call_id") == provider_call_id and entry.event.get("content") == candidate:
                return cast(str, entry.event["event_id"])
        return None

    def snapshot(self) -> AdvisorVisibleTranscript:
        self.observe_messages([entry.message for entry in self._observed])
        events = json.loads(self._history.events_json) if self._history is not None else []
        _recover_read_bodies(events, self._blob_reader, self._history.session_id if self._history is not None else None)
        history_facts = stable_hash(events)
        current_events = [json.loads(json.dumps(entry.event, ensure_ascii=False)) for entry in self._observed]
        events.extend(current_events)
        gaps: list[JsonValue] = [] if self._history is not None else ["authenticated_persisted_history_not_supplied"]
        payload = {
            "events": _redacted_events(events),
            "availability_gaps": gaps,
            "current_request_ref": "complete_original_user_request_section",
            "scope": "same_session_visible_events_only; native_thinking_and_static_internal_prompts_excluded",
        }
        # Exact publication binding covers every event above. The repair key
        # instead counts new successful evidence, not repeated calls, new
        # provider ids, apologies or replayed Advisor findings as progress.
        facts: set[str] = set()
        files: dict[tuple[str, str, int], list[tuple[int, int]]] = {}
        for event in current_events:
            if event["role"] != "tool" or type(event.get("content")) is not str:
                continue
            result = _json_object(event["content"])
            if result is None or result.get("success") is not True or "data" not in result:
                continue
            data = result["data"]
            if (
                type(data) is dict
                and is_lower_sha256_hex(data.get("content_hash"))
                and all(type(data.get(key)) is int for key in ("total_chars", "start_char", "end_char"))
                and type(data.get("blob_id")) is str
            ):
                files.setdefault((data["blob_id"], data["content_hash"], data["total_chars"]), []).append(
                    (data["start_char"], data["end_char"])
                )
            else:
                facts.add(stable_hash(data))
        file_facts = []
        for identity, ranges in sorted(files.items()):
            merged: list[tuple[int, int]] = []
            for start, end in sorted(ranges):
                if merged and start <= merged[-1][1]:
                    merged[-1] = merged[-1][0], max(merged[-1][1], end)
                else:
                    merged.append((start, end))
            file_facts.append((identity, merged))
        return AdvisorVisibleTranscript(
            _redact_sensitive_content(self.request),
            stable_hash({"events": events, "gaps": gaps}),
            json.dumps(payload, ensure_ascii=False),
            stable_hash({"history": history_facts, "facts": sorted(facts), "files": file_facts, "gaps": gaps}),
        )

    def use_blob_reader(self, reader: TranscriptBlobReader) -> None:
        """Attach the service-owned current-operation reader, never a tool argument."""
        self._blob_reader = reader

    def confirm_read_delivery(self, *, tool_call_id: str, provider_call_id: str) -> None:
        for entry in self._observed:
            if entry.event["role"] == "tool" and entry.event.get("tool_call_id") == tool_call_id:
                entry.event["primary_delivery"] = {
                    "status": "confirmed_in_successful_prepared_request",
                    "provider_call_id": provider_call_id,
                }
