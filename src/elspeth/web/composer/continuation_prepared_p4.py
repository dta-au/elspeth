"""Complete historical prepared P4 input; no transaction or live capability.

This is the pre-write input owner, not a batch snapshot or committed receipt.
It retains the original preparing creator for business-SQL comparison.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus, PrivateToolAuthoring
from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
from elspeth.contracts.credential_material import DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_p4_state_payload import decode_p4_state_payload, encode_p4_state_payload
from elspeth.web.composer.continuation_result_credentials import require_retained_argument_credentials, require_retained_result_text
from elspeth.web.composer.continuation_tool_batch import _decode_owned, _dump_owned, _read_json_value, _response_contract
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence, build_private_tool_evidence, diagnostic_evidence_identity
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.composer.withheld_replies import WithheldReply
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions._persist_payload import RedactedToolRow, RejectionRecord


class _RetainedP4Row(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    tool_call_id: str
    content: str
    state_payload_json: str | None
    visible_event_json: str | None


class PreparedP4Value(BaseModel):
    """Complete historical P4 input, never a current operation capability."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.prepared-p4.v1"] = "composer.prepared-p4.v1"
    session_id: str = Field(min_length=1)
    creator_operation_id: str = Field(min_length=1)
    creator_operation_epoch: int = Field(gt=0)
    assistant_content: str
    raw_content: str | None
    redacted_calls_json: str
    redacted_rows_json: str
    revoked_rows_json: str
    revoked_rows_are_original: bool
    mutation_ceiling_json: str | None
    authority_revocation_json: str | None
    private_tool_evidence_json: str
    private_tool_failures_json: str
    rejection_records_json: str
    withheld_replies_json: str
    source_audit_json: str
    parent_composition_state_id: str | None
    expected_current_state_id: str | None
    assistant_row_uses_current_dispatch: bool
    crash_pending: bool


@dataclass(frozen=True, slots=True)
class PreparedP4Members:
    """Decoded historical members; no context, connection, witness or writer."""

    redacted_calls: tuple[Mapping[str, Any], ...]
    redacted_rows: tuple[RedactedToolRow, ...]
    revoked_rows: tuple[RedactedToolRow, ...]
    mutation_ceiling: ComposerMutationCeiling | None
    authority_revocation: ComposerMutationRevocation | None
    private_tool_evidence: tuple[ComposerDiagnosticEvidence, ...]
    private_tool_failures: tuple[ComposerDiagnosticEvidence, ...]
    rejection_records: tuple[RejectionRecord, ...]
    withheld_replies: tuple[WithheldReply, ...]
    invocations: tuple[ComposerToolInvocation, ...]
    authorings: tuple[PrivateToolAuthoring, ...]

    def __post_init__(self) -> None:
        freeze_fields(self, "redacted_calls")


_P4_VALUE = TypeAdapter(PreparedP4Value)
_P4_ROWS = TypeAdapter(tuple[_RetainedP4Row, ...])
_P4_CEILING = TypeAdapter(ComposerMutationCeiling)
_P4_REVOCATION = TypeAdapter(ComposerMutationRevocation)
_P4_REJECTIONS = TypeAdapter(tuple[RejectionRecord, ...])
_P4_WITHHELD = TypeAdapter(tuple[WithheldReply, ...])
_P4_INVOCATIONS = TypeAdapter(tuple[ComposerToolInvocation, ...])
_P4_AUTHORINGS = TypeAdapter(tuple[PrivateToolAuthoring, ...])


def _p4_rows_json(rows: tuple[RedactedToolRow, ...], *, env_ref_names: frozenset[str]) -> str:
    if type(rows) is not tuple or any(type(row) is not RedactedToolRow for row in rows):
        raise TypeError("P4 retention requires actual immutable tool rows")
    retained = []
    for row in rows:
        if row.visible_event is not None and type(row.visible_event) is not VisibleToolEvent:
            raise TypeError("P4 retention requires actual visible evidence")
        retained.append(
            _RetainedP4Row(
                tool_call_id=row.tool_call_id,
                content=row.content,
                state_payload_json=None
                if row.composition_state_payload is None
                else encode_p4_state_payload(row.composition_state_payload, env_ref_names=env_ref_names),
                visible_event_json=None if row.visible_event is None else canonical_json(row.visible_event.to_envelope()),
            )
        )
    return _dump_owned(_P4_ROWS, tuple(retained))


def _p4_rows(encoded: str, *, env_ref_names: frozenset[str]) -> tuple[RedactedToolRow, ...]:
    values = _decode_owned(_P4_ROWS, encoded)
    rows = []
    for value in values:
        event = None
        if value.visible_event_json is not None:
            envelope = _read_json_value(value.visible_event_json)
            if type(envelope) is not dict:
                raise AuditIntegrityError("retained P4 visible evidence is not an object")
            event = VisibleToolEvent.from_envelope(envelope)
            if canonical_json(event.to_envelope()) != value.visible_event_json:
                raise AuditIntegrityError("retained P4 visible evidence lost fields")
        rows.append(
            RedactedToolRow(
                tool_call_id=value.tool_call_id,
                content=value.content,
                composition_state_payload=None
                if value.state_payload_json is None
                else decode_p4_state_payload(value.state_payload_json, env_ref_names=env_ref_names),
                visible_event=event,
            )
        )
    return tuple(rows)


def _p4_evidence_json(values: tuple[ComposerDiagnosticEvidence, ...]) -> str:
    if type(values) is not tuple or any(type(value) is not ComposerDiagnosticEvidence for value in values):
        raise TypeError("P4 retention requires actual private diagnostic records")
    return canonical_json([value.to_envelope() for value in values])


def _p4_evidence(encoded: str, value: PreparedP4Value) -> tuple[ComposerDiagnosticEvidence, ...]:
    envelopes = _read_json_value(encoded)
    if type(envelopes) is not list:
        raise AuditIntegrityError("retained P4 private evidence is not an array")
    result = tuple(
        ComposerDiagnosticEvidence.from_envelope(
            envelope,
            expected_session_id=value.session_id,
            expected_operation_id=value.creator_operation_id,
        )
        for envelope in envelopes
    )
    if _p4_evidence_json(result) != encoded or any(item.operation_epoch != value.creator_operation_epoch for item in result):
        raise AuditIntegrityError("retained P4 private evidence changed its complete historical binding")
    return result


def _p4_safe_text(text: str | None, *, env_ref_names: frozenset[str], tool_name: str | None = None) -> None:
    if text is None:
        return
    parsed, error = parse_json_strict(text)
    if error is not None:
        require_no_credential_material(text, surface="continuation P4 source text", env_ref_names=env_ref_names)
    else:
        if tool_name is None:
            require_no_credential_material(parsed, surface="continuation P4 decoded source", env_ref_names=env_ref_names)
        else:
            require_retained_argument_credentials(tool_name, owned_json(parsed), env_ref_names=env_ref_names)


def _p4_mapping_hash(encoded: str, digest: str) -> None:
    value = _read_json_value(encoded)
    if type(value) is not dict or sha256(encoded.encode("utf-8")).hexdigest() != digest:
        raise AuditIntegrityError("retained P4 invocation lost its canonical mapping hash")


def _require_crash_rejection(rejection: RejectionRecord, invocation: ComposerToolInvocation, *, env_ref_names: frozenset[str]) -> None:
    """Admit only the generic crash producer's diagnostic after full joins."""
    error_class = invocation.error_class
    if (
        invocation.status is not ComposerToolStatus.PLUGIN_CRASH
        or invocation.tool_call_id != rejection.tool_call_id
        or invocation.tool_name != rejection.tool_name
        or error_class is None
        or not error_class
        or invocation.error_message != error_class
        or invocation.result_canonical is not None
        or invocation.result_hash is not None
        or invocation.version_after is not None
        or invocation.error_category is not None
        or invocation.private_failure is None
    ):
        raise AuditIntegrityError("retained P4 crash diagnostic lost its original thrown invocation")
    payload, error = parse_json_strict(rejection.planner_payload)
    if error is not None:
        raise AuditIntegrityError("retained P4 crash diagnostic is not strict JSON")
    require_no_credential_material(payload, surface="continuation P4 crash diagnostic", env_ref_names=env_ref_names)
    if (
        type(payload) is not dict
        or set(payload) != {"error_class", "error_message"}
        or payload["error_class"] != error_class
        or payload["error_message"] != error_class
        or rejection.error_code != error_class
        or rejection.message != error_class
        or rejection.planner_payload != json.dumps({"error_class": error_class, "error_message": error_class})
    ):
        raise AuditIntegrityError("retained P4 crash diagnostic differs from its original class-only rejection")


def prepared_p4_members(value: PreparedP4Value, *, env_ref_names: frozenset[str] = frozenset()) -> PreparedP4Members:
    if type(value) is not PreparedP4Value:
        raise TypeError("P4 decode requires its complete owned value")
    # Revalidate even model_construct/model_copy inputs before reading fields.
    value = _decode_owned(_P4_VALUE, canonical_json(value.model_dump(mode="json", warnings="error")))
    require_no_credential_material(
        {
            "session_id": value.session_id,
            "creator_operation_id": value.creator_operation_id,
            "parent_composition_state_id": value.parent_composition_state_id,
            "expected_current_state_id": value.expected_current_state_id,
        },
        surface="continuation P4 identity and lineage",
        env_ref_names=env_ref_names,
    )
    _p4_safe_text(value.assistant_content, env_ref_names=env_ref_names)
    _p4_safe_text(value.raw_content, env_ref_names=env_ref_names)
    calls_value = _read_json_value(value.redacted_calls_json)
    if type(calls_value) is not list or any(type(call) is not dict for call in calls_value):
        raise AuditIntegrityError("retained P4 calls are not their object array")
    calls: list[Mapping[str, Any]] = []
    for call in calls_value:
        if type(call) is not dict:
            raise AuditIntegrityError("retained P4 call is not an object")
        function = call.get("function")
        if (
            set(call) != {"id", "type", "function", "strict_sent", "wire_conformant"}
            or type(call["id"]) is not str
            or not call["id"]
            or call["type"] != "function"
            or type(function) is not dict
            or set(function) != {"name", "arguments"}
            or type(function["name"]) is not str
            or not function["name"]
            or type(function["arguments"]) is not str
            or any(call[key] is not None and type(call[key]) is not bool for key in ("strict_sent", "wire_conformant"))
        ):
            raise AuditIntegrityError("retained P4 call differs from the actual assistant call projection")
        _p4_safe_text(function["arguments"], env_ref_names=env_ref_names, tool_name=function["name"])
        calls.append(call)
    call_ids = [call["id"] for call in calls]
    if len(set(call_ids)) != len(call_ids):
        raise AuditIntegrityError("retained P4 repeats an issued call")
    rows = _p4_rows(value.redacted_rows_json, env_ref_names=env_ref_names)
    revoked = _p4_rows(value.revoked_rows_json, env_ref_names=env_ref_names)
    if value.revoked_rows_are_original:
        if value.revoked_rows_json != value.redacted_rows_json:
            raise AuditIntegrityError("retained P4 alias points to different rows")
        revoked = rows
    row_ids = [row.tool_call_id for row in rows]
    if len(set(row_ids)) != len(row_ids) or row_ids != call_ids:
        raise AuditIntegrityError("retained P4 rows lost the exact executed call cohort")
    if len(revoked) != len(rows):
        raise AuditIntegrityError("retained P4 refusal alternate changed the cohort")
    if not any(row.composition_state_payload is not None for row in rows) and not value.revoked_rows_are_original:
        raise AuditIntegrityError("retained nongraph P4 rows lost tuple identity")
    audit = _read_json_value(value.source_audit_json)
    if type(audit) is not dict or set(audit) != {"invocations", "authorings"}:
        raise AuditIntegrityError("retained P4 source audit lost its complete closed shape")
    invocations = _decode_owned(_P4_INVOCATIONS, canonical_json(audit["invocations"]))
    authorings = _decode_owned(_P4_AUTHORINGS, canonical_json(audit["authorings"]))
    if [item.tool_call_id for item in invocations] != row_ids or [item.tool_call_id for item in authorings] != row_ids:
        raise AuditIntegrityError("retained P4 source audit lost its executed cohort")
    statuses_by_id = {item.tool_call_id: item.status for item in invocations}
    for index, (row, alternate) in enumerate(zip(rows, revoked, strict=True)):
        function = calls[index]["function"]
        for selected in (row, alternate):
            require_retained_result_text(
                function["name"], selected.content, env_ref_names=env_ref_names, display=True, outcome_status=invocations[index].status
            )
            if selected.visible_event is not None:
                event = selected.visible_event
                visible, error = parse_json_strict(event.payload_json)
                if error is not None or type(visible) is not dict:
                    raise AuditIntegrityError("retained P4 visible evidence lost its admitted object")
                require_no_credential_material(
                    event.producer_declared_request_id,
                    surface="continuation P4 visible request metadata",
                    env_ref_names=env_ref_names,
                )
                require_no_credential_material(
                    visible["availability_gaps"],
                    surface="continuation P4 visible availability metadata",
                    env_ref_names=env_ref_names,
                )
                for name in ("received_arguments", "effective_arguments", "selected_result"):
                    text = visible[name]
                    if text is not None and type(text) is not str:
                        raise AuditIntegrityError("retained P4 visible evidence lost its admitted text")
                    if name == "selected_result":
                        # This is the historical visible producer's redacted
                        # serialized text, not the original selected response.
                        # Redaction can alter hashes or make JSON invalid.
                        # Match capture's text policy without interpreting
                        # schema/FieldMapper labels as credential field names.
                        require_no_credential_material(
                            text,
                            surface="continuation P4 retained visible result",
                            env_ref_names=env_ref_names,
                            max_string_bytes=max(
                                DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_string_bytes,
                                0 if text is None else len(text.encode("utf-8")),
                            ),
                        )
                    else:
                        _p4_safe_text(text, env_ref_names=env_ref_names, tool_name=function["name"])
                if event.operation_epoch != value.creator_operation_epoch or event.parent_assistant_id is not None:
                    raise AuditIntegrityError("retained P4 visible evidence has another epoch or is already published")
                event.validate_recorded_link(
                    session_id=value.session_id,
                    operation_id=value.creator_operation_id,
                    tool_call_id=row.tool_call_id,
                    tool_name=function["name"],
                    display_arguments=function["arguments"],
                    display_result=selected.content,
                )
        if row.composition_state_payload is None:
            if alternate != row:
                raise AuditIntegrityError("retained P4 refusal changed an independent result")
        elif (
            alternate.tool_call_id != row.tool_call_id
            or alternate.composition_state_payload is not None
            or ((row.visible_event is None) != (alternate.visible_event is None))
            or (row.visible_event is not None and alternate.visible_event != row.visible_event.with_authority_revocation(alternate.content))
        ):
            raise AuditIntegrityError("retained P4 refusal kept graph authority or changed evidence")
    ceiling = None if value.mutation_ceiling_json is None else _decode_owned(_P4_CEILING, value.mutation_ceiling_json)
    revocation = None if value.authority_revocation_json is None else _decode_owned(_P4_REVOCATION, value.authority_revocation_json)
    if ceiling is not None and ceiling.session_id != value.session_id:
        raise AuditIntegrityError("retained P4 ceiling belongs to another session")
    if revocation is not None and (ceiling is None or revocation.ceiling != ceiling):
        raise AuditIntegrityError("retained P4 revocation differs from its ceiling")
    if ceiling is not None:
        require_no_credential_material(
            {"session_id": ceiling.session_id, "request_id": ceiling.request_id},
            surface="continuation P4 mutation request metadata",
            env_ref_names=env_ref_names,
        )
    evidence = _p4_evidence(value.private_tool_evidence_json, value)
    failures = _p4_evidence(value.private_tool_failures_json, value)
    if [item.tool_call_id for item in evidence] != row_ids:
        raise AuditIntegrityError("retained P4 private evidence lost its executed cohort")
    if len({item.evidence_id for item in (*evidence, *failures)}) != len(evidence) + len(failures):
        raise AuditIntegrityError("retained P4 private evidence identities collide")
    if len({item.tool_call_id for item in failures}) != len(failures):
        raise AuditIntegrityError("retained P4 private failure repeats a call")
    for item in failures:
        if (
            item.tool_call_id not in row_ids
            or item.tool_name != "composer_failure"
            or item.status != "tool_failure"
            or item.evidence_id != diagnostic_evidence_identity("tool-failure", item.tool_call_id)
        ):
            raise AuditIntegrityError("retained P4 private failure changed its owned identity")
    rejections = _decode_owned(_P4_REJECTIONS, value.rejection_records_json)
    names_by_id = {call["id"]: call["function"]["name"] for call in calls}
    if len({item.tool_call_id for item in rejections}) != len(rejections) or any(
        item.tool_call_id not in names_by_id or item.tool_name != names_by_id[item.tool_call_id] for item in rejections
    ):
        raise AuditIntegrityError("retained P4 rejection names an unexecuted call")
    withheld = _decode_owned(_P4_WITHHELD, value.withheld_replies_json)
    for reply in withheld:
        _p4_safe_text(reply.content, env_ref_names=env_ref_names)
    for index, (invocation, authoring, private) in enumerate(zip(invocations, authorings, evidence, strict=True)):
        tool_name = calls[index]["function"]["name"]
        if (
            invocation.tool_name != tool_name
            or authoring.tool_name != tool_name
            or private.tool_name != tool_name
            or private.status != invocation.status.value
            or (invocation.private_authoring is not None and invocation.private_authoring != authoring)
        ):
            raise AuditIntegrityError("retained P4 source audit names another selected tool or authoring")
        if invocation.strict_sent != calls[index]["strict_sent"] or invocation.wire_conformant != calls[index]["wire_conformant"]:
            raise AuditIntegrityError("retained P4 call wire facts differ from its invocation")
        _p4_safe_text(invocation.actor, env_ref_names=env_ref_names)
        _p4_safe_text(invocation.error_class, env_ref_names=env_ref_names)
        for text in (
            authoring.raw_arguments,
            authoring.arguments_canonical,
            authoring.authority_arguments_canonical,
            invocation.arguments_canonical,
            invocation.authority_arguments_canonical,
        ):
            _p4_safe_text(text, env_ref_names=env_ref_names, tool_name=tool_name)
        require_retained_result_text(tool_name, invocation.result_canonical, env_ref_names=env_ref_names, outcome_status=invocation.status)
        for text in (invocation.error_message, None if invocation.private_failure is None else invocation.private_failure.canonical):
            _p4_safe_text(text, env_ref_names=env_ref_names)
        expected_private = build_private_tool_evidence(
            session_id=value.session_id,
            operation_id=value.creator_operation_id,
            operation_epoch=value.creator_operation_epoch,
            tool_call_id=invocation.tool_call_id,
            tool_name=tool_name,
            status=invocation.status.value,
            arguments_canonical=authoring.arguments_canonical,
            authority_arguments_canonical=authoring.authority_arguments_canonical,
            raw_arguments=authoring.raw_arguments,
            result_canonical=invocation.result_canonical,
            error_category=None if invocation.error_category is None else invocation.error_category.value,
        )
        if private != expected_private:
            raise AuditIntegrityError("retained P4 private evidence differs from its actual source audit")
        _p4_mapping_hash(invocation.arguments_canonical, invocation.arguments_hash)
        if invocation.result_canonical is None:
            if invocation.result_hash is not None:
                raise AuditIntegrityError("retained P4 absent result has a hash")
        elif invocation.result_hash is None:
            raise AuditIntegrityError("retained P4 result has no hash")
        else:
            _p4_mapping_hash(invocation.result_canonical, invocation.result_hash)
        if invocation.authority_arguments_canonical is None:
            if invocation.authority_arguments_hash is not None:
                raise AuditIntegrityError("retained P4 absent authority arguments have a hash")
        elif invocation.authority_arguments_hash is None:
            raise AuditIntegrityError("retained P4 authority arguments have no hash")
        else:
            _p4_mapping_hash(invocation.authority_arguments_canonical, invocation.authority_arguments_hash)
    expected_failures = tuple(
        build_private_tool_evidence(
            session_id=value.session_id,
            operation_id=value.creator_operation_id,
            operation_epoch=value.creator_operation_epoch,
            tool_call_id=item.tool_call_id,
            tool_name="composer_failure",
            status="tool_failure",
            arguments_canonical=None,
            result_canonical=item.private_failure.canonical,
            evidence_id=diagnostic_evidence_identity("tool-failure", item.tool_call_id),
        )
        for item in invocations
        if item.private_failure is not None
    )
    if failures != expected_failures:
        raise AuditIntegrityError("retained P4 private failures differ from their actual source audit")
    # A thrown synchronous Discovery has no successful response envelope.
    # Its separate rejection is admitted only after every original source
    # cohort/hash/private-failure join above, never from an early status tag.
    invocations_by_id = {item.tool_call_id: item for item in invocations}
    for rejection in rejections:
        _p4_safe_text(rejection.tool_call_id, env_ref_names=env_ref_names)
        _p4_safe_text(rejection.tool_name, env_ref_names=env_ref_names)
        _p4_safe_text(rejection.error_code, env_ref_names=env_ref_names)
        _p4_safe_text(rejection.message, env_ref_names=env_ref_names)
        invocation = invocations_by_id[rejection.tool_call_id]
        if invocation.status is ComposerToolStatus.PLUGIN_CRASH and _response_contract(rejection.tool_name) is not None:
            _require_crash_rejection(rejection, invocation, env_ref_names=env_ref_names)
        else:
            require_retained_result_text(
                rejection.tool_name,
                rejection.planner_payload,
                env_ref_names=env_ref_names,
                outcome_status=statuses_by_id[rejection.tool_call_id],
            )
    return PreparedP4Members(
        tuple(calls), rows, revoked, ceiling, revocation, evidence, failures, rejections, withheld, invocations, authorings
    )


def capture_prepared_p4(
    *,
    context: SessionOperationContext,
    assistant_content: str,
    raw_content: str | None,
    redacted_calls: tuple[Mapping[str, Any], ...],
    redacted_rows: tuple[RedactedToolRow, ...],
    revoked_rows: tuple[RedactedToolRow, ...],
    mutation_ceiling: ComposerMutationCeiling | None,
    authority_revocation: ComposerMutationRevocation | None,
    private_tool_evidence: tuple[ComposerDiagnosticEvidence, ...],
    private_tool_failures: tuple[ComposerDiagnosticEvidence, ...],
    rejection_records: tuple[RejectionRecord, ...],
    withheld_replies: tuple[WithheldReply, ...],
    invocations: tuple[ComposerToolInvocation, ...],
    authorings: tuple[PrivateToolAuthoring, ...],
    parent_composition_state_id: str | None,
    expected_current_state_id: str | None,
    assistant_row_uses_current_dispatch: bool,
    crash_pending: bool,
    env_ref_names: frozenset[str] = frozenset(),
) -> PreparedP4Value:
    if type(context) is not SessionOperationContext or context.operation_kind is not SessionOperationKind.COMPOSE:
        raise TypeError("P4 capture requires actual COMPOSE provenance")
    for values, expected in (
        (invocations, ComposerToolInvocation),
        (authorings, PrivateToolAuthoring),
        (rejection_records, RejectionRecord),
        (withheld_replies, WithheldReply),
    ):
        if type(values) is not tuple or any(type(item) is not expected for item in values):
            raise TypeError("P4 capture requires its actual immutable nominal cohorts")
    if mutation_ceiling is not None:
        if type(mutation_ceiling) is not ComposerMutationCeiling:
            raise TypeError("P4 capture requires its actual mutation ceiling")
        mutation_ceiling.validate_context(context)
    if authority_revocation is not None and type(authority_revocation) is not ComposerMutationRevocation:
        raise TypeError("P4 capture requires its actual mutation revocation")
    value = PreparedP4Value(
        session_id=context.fence.session_id,
        creator_operation_id=context.fence.operation_id,
        creator_operation_epoch=context.fence.operation_epoch,
        assistant_content=assistant_content,
        raw_content=raw_content,
        redacted_calls_json=canonical_json(owned_json(redacted_calls)),
        redacted_rows_json=_p4_rows_json(redacted_rows, env_ref_names=env_ref_names),
        revoked_rows_json=_p4_rows_json(revoked_rows, env_ref_names=env_ref_names),
        revoked_rows_are_original=revoked_rows is redacted_rows,
        mutation_ceiling_json=None if mutation_ceiling is None else _dump_owned(_P4_CEILING, mutation_ceiling),
        authority_revocation_json=None if authority_revocation is None else _dump_owned(_P4_REVOCATION, authority_revocation),
        private_tool_evidence_json=_p4_evidence_json(private_tool_evidence),
        private_tool_failures_json=_p4_evidence_json(private_tool_failures),
        rejection_records_json=_dump_owned(_P4_REJECTIONS, rejection_records),
        withheld_replies_json=_dump_owned(_P4_WITHHELD, withheld_replies),
        source_audit_json=canonical_json(
            {
                "invocations": _read_json_value(_dump_owned(_P4_INVOCATIONS, invocations)),
                "authorings": _read_json_value(_dump_owned(_P4_AUTHORINGS, authorings)),
            }
        ),
        parent_composition_state_id=parent_composition_state_id,
        expected_current_state_id=expected_current_state_id,
        assistant_row_uses_current_dispatch=assistant_row_uses_current_dispatch,
        crash_pending=crash_pending,
    )
    prepared_p4_members(value, env_ref_names=env_ref_names)
    return value


def encode_prepared_p4(value: PreparedP4Value, *, env_ref_names: frozenset[str] = frozenset()) -> str:
    prepared_p4_members(value, env_ref_names=env_ref_names)
    return canonical_json(value.model_dump(mode="json", warnings="error"))


def decode_prepared_p4(encoded: str, *, env_ref_names: frozenset[str] = frozenset()) -> PreparedP4Value:
    value = _decode_owned(_P4_VALUE, encoded)
    prepared_p4_members(value, env_ref_names=env_ref_names)
    return value


def p4_semantic_input(value: PreparedP4Value, *, env_ref_names: frozenset[str] = frozenset()) -> str:
    """Bind complete historical input; equality grants no writer authority.

    Business SQL retains its preparing creator. Unlike immutable reads, this
    projection deliberately includes creator and visible-event provenance.
    """
    prepared_p4_members(value, env_ref_names=env_ref_names)
    payload = value.model_dump(mode="json", warnings="error")
    return canonical_json({"schema": "composer.p4-input.v1", "prepared": owned_json(payload)})
