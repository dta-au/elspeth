"""Owned visible conversation evidence for every Advisor entry path.

This is a review projection, not a second provider protocol. Native thinking,
private diagnostics and static authoring prompts never enter this module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast, final, get_args
from uuid import UUID

from pydantic import JsonValue

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import is_lower_sha256_hex, stable_hash
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_THINKING_PARTS_KEY,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardContentPart,
)
from elspeth.web.composer.continuation_message_shelf import (
    MessageShelfSnapshot,
    RetainedObservedMessage,
    RetainedTranscriptHistory,
    encode_transcript_object_json,
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

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers

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


@final
@dataclass(frozen=True, slots=True)
class AdvisorVisibleTranscript:
    """Immutable complete available visible chronology; all text is untrusted."""

    request: str = field(repr=False)
    binding: str
    provider_json: str = field(repr=False)
    semantic_binding: str

    def with_request_reference(
        self,
        reference: Literal["complete_original_user_request_section", "initial_planner_request.intent"],
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

    def snapshot_for_continuation(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        materialization: CurrentContinuationMessageProducers | None = None,
    ) -> MessageShelfSnapshot:
        shelf: list[Mapping[str, Any]] = []

        def intern(message: Mapping[str, Any]) -> int:
            for index, existing in enumerate(shelf):
                if existing is message:
                    return index
            shelf.append(message)
            return len(shelf) - 1

        current = tuple(intern(message) for message in messages)
        excluded = tuple(intern(message) for message in self._excluded)
        observed = tuple(
            RetainedObservedMessage(
                message_index=intern(entry.message),
                event_json=encode_transcript_object_json(entry.event),
                pending_adoption=entry.pending_adoption,
            )
            for entry in self._observed
        )
        forbidden = frozenset(
            {
                PROMPT_GUARD_TRUST_KEY,
                PROMPT_GUARD_CONTENT_PARTS_KEY,
                PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
                PROMPT_GUARD_THINKING_PARTS_KEY,
            }
        )
        from elspeth.web.composer.continuation_prompt_recipes import ContinuationMessageRecipe

        recipes: tuple[ContinuationMessageRecipe | None, ...]
        if materialization is None:
            if any(forbidden.intersection(message) for message in shelf):
                raise AuditIntegrityError(
                    "Continuation requires current producer materialization for guard-bearing messages; "
                    "retain this request and reacquire the owned producer before resuming."
                )
            messages_json = tuple(encode_transcript_object_json(message) for message in shelf)
            recipes = ()
        else:
            from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers

            if type(materialization) is not CurrentContinuationMessageProducers:
                raise AuditIntegrityError("transcript capture requires its actual current producer owner")
            captured = tuple(materialization.capture_message(message) for message in shelf)
            messages_json = tuple(item[0] for item in captured)
            recipes = tuple(item[1] for item in captured)
        history = self._history
        return MessageShelfSnapshot(
            request=self.request,
            history=RetainedTranscriptHistory(
                session_id=history.session_id,
                actor_user_id=history.actor_user_id,
                request_id=history.request_id,
                request_hash=history.request_hash,
                events_json=history.events_json,
            )
            if history is not None
            else None,
            messages_json=messages_json,
            producer_recipes=recipes,
            current_indexes=current,
            excluded_indexes=excluded,
            observed=observed,
        )

    @classmethod
    def for_continuation(
        cls,
        snapshot: MessageShelfSnapshot,
        *,
        session_id: str,
        actor_user_id: str,
        request_id: str,
        request: str,
        materialization: CurrentContinuationMessageProducers | None = None,
    ) -> tuple[AdvisorTranscriptOwner, list[dict[str, Any]]]:
        history = cls._continuation_history(
            snapshot, session_id=session_id, actor_user_id=actor_user_id, request_id=request_id, request=request
        )
        if materialization is None:
            if any(recipe is not None for recipe in snapshot.producer_recipes):
                raise AuditIntegrityError("retained producers require current selected materialization")
            shelf = [json.loads(text) for text in snapshot.messages_json]
        else:
            from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers

            if type(materialization) is not CurrentContinuationMessageProducers:
                raise AuditIntegrityError("transcript restore requires its actual current producer owner")
            shelf = materialization.restore_shelf(snapshot)
        return cls._from_continuation_shelf(snapshot, request=request, history=history, shelf=shelf)

    @classmethod
    def _continuation_history(
        cls, snapshot: MessageShelfSnapshot, *, session_id: str, actor_user_id: str, request_id: str, request: str
    ) -> AdvisorTranscriptHistory | None:
        if type(snapshot) is not MessageShelfSnapshot or snapshot.request != request:
            raise AuditIntegrityError("transcript shelf belongs to another request")
        history = None
        if snapshot.history is not None:
            retained = snapshot.history
            history = AdvisorTranscriptHistory(
                retained.session_id,
                retained.actor_user_id,
                retained.request_id,
                retained.request_hash,
                retained.events_json,
            )
            history.validate_for(
                session_id=session_id,
                actor_user_id=actor_user_id,
                request_id=request_id,
                request=request,
            )
        return history

    @classmethod
    def _from_continuation_shelf(
        cls, snapshot: MessageShelfSnapshot, *, request: str, history: AdvisorTranscriptHistory | None, shelf: list[dict[str, Any]]
    ) -> tuple[AdvisorTranscriptOwner, list[dict[str, Any]]]:
        """Shared object assembly only; no saved-wire/current admission."""
        owner = cls(request=request, history=history)
        owner._excluded = [shelf[index] for index in snapshot.excluded_indexes]
        owner._observed = [
            _ObservedMessage(
                shelf[entry.message_index],
                json.loads(entry.event_json),
                entry.pending_adoption,
            )
            for entry in snapshot.observed
        ]
        return owner, [shelf[index] for index in snapshot.current_indexes]
