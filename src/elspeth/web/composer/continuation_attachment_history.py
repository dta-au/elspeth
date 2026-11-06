from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.attachment_admission import AttachmentReadAdmission
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_attachment_application_values import AttachmentApplicationData
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, verify_private_chunk
from elspeth.web.credential_guard import require_no_credential_material


class RetainedAttachmentInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    session_id: str = Field(min_length=1)
    blob_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    detector_version: str = Field(min_length=1)
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_actual(cls, actual: AttachmentReadAdmission) -> Self:
        if type(actual) is not AttachmentReadAdmission:
            raise TypeError("inspection history requires the actual complete-original receipt")
        return cls(
            session_id=actual.session_id,
            blob_id=actual.blob_id,
            content_hash=actual.content_hash,
            size_bytes=actual.size_bytes,
            detector_version=actual.detector_version,
            policy_fingerprint=actual.policy_fingerprint,
        )


class RetainedAttachmentRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    effect_key: str = Field(min_length=1)
    inspections: tuple[RetainedAttachmentInspection, ...] = Field(min_length=1)


class ContinuationAttachmentHistory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.attachment-admissions.v1"] = "composer.attachment-admissions.v1"
    session_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    reads: tuple[RetainedAttachmentRead, ...]
    applications: tuple[AttachmentApplicationData, ...] = ()

    @model_validator(mode="after")
    def require_history_scope(self) -> Self:
        keys = tuple(read.effect_key for read in self.reads)
        if len(keys) != len(set(keys)):
            raise ValueError("attachment history duplicates a read effect")
        applied = tuple(value.effect_key for value in self.applications)
        if len(applied) != len(set(applied)):
            raise ValueError("attachment history repeats an actual application")
        if any(item.session_id != self.session_id for read in self.reads for item in read.inspections):
            raise ValueError("attachment history contains another session")
        return self


def empty_attachment_history(*, session_id: str, operation_id: str) -> ContinuationAttachmentHistory:
    return ContinuationAttachmentHistory(session_id=session_id, operation_id=operation_id, reads=())


def extend_attachment_history(
    history: ContinuationAttachmentHistory,
    *,
    reads: tuple[RetainedAttachmentRead, ...] = (),
    applications: tuple[AttachmentApplicationData, ...] = (),
) -> ContinuationAttachmentHistory:
    """The history with entries appended, built through validation.

    model_copy(update=...) skips require_history_scope, which would admit a
    read from another session or a repeated effect without complaint.
    """
    return ContinuationAttachmentHistory(
        session_id=history.session_id,
        operation_id=history.operation_id,
        reads=(*history.reads, *reads),
        applications=(*history.applications, *applications),
    )


def encode_attachment_history(
    history: ContinuationAttachmentHistory,
    *,
    env_ref_names: frozenset[str] = frozenset(),
    source_chunks: tuple[PrivateChunk, ...] = (),
) -> PrivateChunk:
    if type(history) is not ContinuationAttachmentHistory:
        raise TypeError("attachment codec requires its complete nominal history")
    encoded = canonical_json(history.model_dump(mode="json", exclude={"applications"} if not history.applications else set()))
    try:
        checked = ContinuationAttachmentHistory.model_validate_json(encoded)
    except ValidationError as error:
        raise AuditIntegrityError("attachment history violates its complete owned fields") from error
    if canonical_json(checked.model_dump(mode="json", exclude={"applications"} if not checked.applications else set())) != encoded:
        raise AuditIntegrityError("attachment history lost or normalized a field")
    _require_history_metadata(history, env_ref_names=env_ref_names)
    if history.applications:
        from elspeth.web.composer.continuation_attachment_application import admit_attachment_application

        for application in history.applications:
            admitted, retained = admit_attachment_application(application, env_ref_names=env_ref_names, source_chunks=source_chunks)
            if (admitted.logical.session_id, admitted.logical.operation_id) != (history.session_id, history.operation_id):
                raise AuditIntegrityError("attachment application belongs to another history")
            inspections = tuple(item for item in history.reads if item.effect_key == application.effect_key)
            expected = (
                ()
                if not retained.attachment_inspections
                else (RetainedAttachmentRead(effect_key=application.effect_key, inspections=retained.attachment_inspections),)
            )
            if inspections != expected:
                raise AuditIntegrityError("attachment application changed its complete original inspection cohort")
    return encode_private_chunk(codec="composer.attachment-admissions.v1", canonical=encoded, env_ref_names=env_ref_names)


def decode_attachment_history(
    chunk: PrivateChunk,
    *,
    session_id: str,
    operation_id: str,
    env_ref_names: frozenset[str] = frozenset(),
    source_chunks: tuple[PrivateChunk, ...] = (),
) -> ContinuationAttachmentHistory:
    result = decode_attachment_history_fields(chunk, session_id=session_id, operation_id=operation_id, env_ref_names=env_ref_names)
    if encode_attachment_history(result, env_ref_names=env_ref_names, source_chunks=source_chunks).canonical != chunk.canonical:
        raise AuditIntegrityError("attachment history lost fields during complete decode")
    return result


def decode_attachment_history_fields(
    chunk: PrivateChunk,
    *,
    session_id: str,
    operation_id: str,
    env_ref_names: frozenset[str] = frozenset(),
) -> ContinuationAttachmentHistory:
    """Parse the closed fields for dependency discovery, never source admission.

    Full decoding still requires the actual original application dependencies.
    The SQL owner separately proves each original checkpoint/event/creator.
    """
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.attachment-admissions.v1":
        raise AuditIntegrityError("attachment history has another private codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        result = ContinuationAttachmentHistory.model_validate_json(chunk.canonical, strict=True)
    except ValidationError as exc:
        raise AuditIntegrityError("attachment history violates its complete owned codec") from exc
    encoded = canonical_json(result.model_dump(mode="json", exclude={"applications"} if not result.applications else set()))
    if encoded != chunk.canonical:
        raise AuditIntegrityError("attachment history lost fields during complete decode")
    if result.session_id != session_id or result.operation_id != operation_id:
        raise AuditIntegrityError("attachment history differs from the selected logical request")
    _require_history_metadata(result, env_ref_names=env_ref_names)
    return result


def _require_history_metadata(history: ContinuationAttachmentHistory, *, env_ref_names: frozenset[str]) -> None:
    require_no_credential_material(
        history.model_dump(mode="json", exclude={"reads": {"__all__": {"effect_key"}}, "applications": True}),
        surface="attachment inspection history",
        env_ref_names=env_ref_names,
    )
    for read in history.reads:
        require_no_credential_material(read.effect_key, surface="attachment historical effect identity", env_ref_names=env_ref_names)


def attachment_history_dependencies(history: ContinuationAttachmentHistory) -> tuple[PrivateChunkRef, ...]:
    """Closed original before/after evidence roles, preserving every occurrence."""
    from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot

    if type(history) is not ContinuationAttachmentHistory:
        raise TypeError("attachment dependency discovery requires its closed history")
    references: list[PrivateChunkRef] = []
    for application in history.applications:
        for text in (application.before_evidence_json, application.after_evidence_json):
            try:
                snapshot = TurnEvidenceSnapshot.model_validate_json(text)
            except (ValidationError, ValueError, TypeError) as error:
                raise AuditIntegrityError("attachment history changed its original evidence schema") from error
            if canonical_json(snapshot.model_dump(mode="json")) != text:
                raise AuditIntegrityError("attachment history normalized its original evidence fields")
            references.append(snapshot.recorder)
            if snapshot.application_entry is not None:
                for record in snapshot.application_entry.applications:
                    references.extend((record.response, record.pending_batch, record.prepared_input, record.result))
    return tuple(references)


def append_attachment_inspections(
    history: ContinuationAttachmentHistory,
    *,
    effect_key: str,
    inspections: tuple[AttachmentReadAdmission, ...],
) -> ContinuationAttachmentHistory:
    """Capture actual completed inspections; this pure operation grants no current use."""
    if type(history) is not ContinuationAttachmentHistory or type(inspections) is not tuple:
        raise TypeError("attachment capture requires exact historical and actual receipt owners")
    incoming = RetainedAttachmentRead(
        effect_key=effect_key, inspections=tuple(RetainedAttachmentInspection.from_actual(item) for item in inspections)
    )
    previous = tuple(read for read in history.reads if read.effect_key == effect_key)
    if previous:
        if previous != (incoming,):
            raise AuditIntegrityError("same attachment effect changed its complete inspections")
        return history
    return extend_attachment_history(history, reads=(incoming,))
