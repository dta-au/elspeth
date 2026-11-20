"""File-held prompts as the advisor sees them.

A prompt position (``prompt_template``, ``system_prompt``, a query's
``template``) may hold a valid ``inline_content`` file marker instead of
text. The advisor's pipeline evidence states, for each such file, its name,
size and who wrote it. A file the user wrote is not sent: a human-authored
file needs its connection approved, not its text reviewed (maintainer ruling
2026-11-21). Any other file -- one the composer wrote, a pipeline output, a
file imported from a shared link or copied by a session fork -- is sent whole
and reviewed like an inline prompt. Authorship that is not proven to be the
user's sends the text.

This module owns the authorship decision and the authorized read. The read is
synchronous custody I/O and runs in a worker thread before the evidence is
rendered; the renderers in ``advisor_context`` receive the immutable result
and never read storage.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import groupby
from operator import itemgetter
from typing import Final, Literal
from uuid import UUID

from elspeth.contracts.blobs import (
    BlobContentMissingError,
    BlobNotFoundError,
    BlobRecord,
    BlobServiceProtocol,
    BlobStateError,
)
from elspeth.contracts.blobs_inline import WidenedBlobRefShape
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.core.blobs_inline import BLOB_INLINE_PER_REF_BYTE_CAP
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.execution._validation_materialization import is_user_uploaded_blob

PromptFileAuthor = Literal["user", "imported", "copied", "composer", "pipeline"]
"""Who wrote a prompt file, as the advisor evidence states it.

``user``: written by this session's user (upload, inline create, editor save).
``imported``: copied in by a shared-link import (written by another person).
``copied``: a session-fork copy of a user file; its original description, and
so whether it was itself an import, is not kept. ``composer``: written by the
composer (or LLM-authored). ``pipeline``: a pipeline output.
"""

PromptFileUnavailable = Literal[
    "no_reader",
    "not_found",
    "not_ready",
    "content_missing",
    "changed_since_wired",
    "too_large",
    "not_text",
]
"""Why the file's facts or text could not be read; the text is then not reviewed."""

PromptFileKey = tuple[UUID, str, str]
"""``(blob id, marker sha256, marker encoding)``: the bytes a marker selects and how they decode."""


def advisor_prompt_file_author(*, created_by: str, creation_modality: CreationModality, source_description: str | None) -> PromptFileAuthor:
    """The ONE authorship decision for the advisor: who wrote this prompt file.

    Only ``user`` keeps the text back. It requires the user-upload predicate
    the file-inject gate uses (``is_user_uploaded_blob``: created by the user
    and not LLM-authored) AND a description that is not an import's or a
    session-fork copy's. Every other row is sent, so the decision fails
    toward sending. The two description forms are read from the code that
    writes them, never from a copied literal.
    """
    # Local imports: both writer modules import composer code at load time.
    from elspeth.web.blobs.service import SESSION_FORK_COPY_DESCRIPTION_PREFIX
    from elspeth.web.shareable_reviews.importer import SHARED_IMPORT_DESCRIPTION_PREFIX

    if is_user_uploaded_blob(created_by=created_by, creation_modality=creation_modality):
        if source_description is not None and source_description.startswith(SHARED_IMPORT_DESCRIPTION_PREFIX):
            return "imported"
        if source_description is not None and source_description.startswith(SESSION_FORK_COPY_DESCRIPTION_PREFIX):
            return "copied"
        return "user"
    if created_by == "pipeline":
        return "pipeline"
    return "composer"


@dataclass(frozen=True, slots=True)
class AdvisorPromptFile:
    """What the advisor is told about one file-held prompt.

    ``text`` is present only for a file not recorded as the user's own writing whose bytes
    were read, matched the marker and decoded. ``unavailable`` names why the
    facts or text are missing; a ``user`` file with neither is ``not_sent``.
    """

    filename: str | None
    size_bytes: int | None
    author: PromptFileAuthor | None
    text: str | None = field(default=None, repr=False)
    unavailable: PromptFileUnavailable | None = None

    def __post_init__(self) -> None:
        if self.text is not None and (self.unavailable is not None or self.author in (None, "user")):
            raise ValueError("AdvisorPromptFile: text is sent only for a readable file not recorded as the user's own writing")
        if self.unavailable is None and self.text is None and self.author != "user":
            raise ValueError("AdvisorPromptFile: a file not recorded as the user's own writing needs its text or an unavailable reason")
        if self.author is None and self.unavailable is None:
            raise ValueError("AdvisorPromptFile: unknown authorship needs an unavailable reason")

    @property
    def text_state(self) -> str:
        """``sent``, ``not_sent`` or ``unavailable:<reason>`` -- the evidence's ``text=`` field."""
        if self.text is not None:
            return "sent"
        if self.unavailable is not None:
            return f"unavailable:{self.unavailable}"
        return "not_sent"


_NO_READER: Final = AdvisorPromptFile(filename=None, size_bytes=None, author=None, unavailable="no_reader")


@dataclass(frozen=True, slots=True)
class AdvisorPromptFiles:
    """The read facts of every file-held prompt in one rendered graph.

    ``read`` is False when no authorized reader was available (sessionless
    compose, no blob service, or no operation context): every file is then
    ``unavailable:no_reader``. When ``read`` is True a marker absent from
    ``files`` means the collector and the renderer disagree on a prompt
    position, which is a composer defect, and the lookup raises.
    """

    files: Mapping[PromptFileKey, AdvisorPromptFile] = field(default_factory=dict)
    read: bool = False

    def __post_init__(self) -> None:
        freeze_fields(self, "files")

    def file(self, marker: WidenedBlobRefShape) -> AdvisorPromptFile:
        if not self.read:
            return _NO_READER
        key = prompt_file_key(marker)
        if key not in self.files:
            raise InvariantError(f"Advisor prompt file {marker.blob_id} was rendered but never read")
        return self.files[key]


UNREAD_PROMPT_FILES: Final = AdvisorPromptFiles()


def prompt_file_key(marker: WidenedBlobRefShape) -> PromptFileKey:
    if marker.sha256 is None:
        raise ValueError("An inline_content marker always carries sha256")
    return (marker.blob_id, marker.sha256, marker.encoding)


_NOT_FOUND: Final = AdvisorPromptFile(filename=None, size_bytes=None, author=None, unavailable="not_found")


def _facts(record: BlobRecord, *, unavailable: PromptFileUnavailable | None = None, text: str | None = None) -> AdvisorPromptFile:
    return AdvisorPromptFile(
        filename=record.filename,
        size_bytes=record.size_bytes,
        author=advisor_prompt_file_author(
            created_by=record.created_by,
            creation_modality=record.creation_modality,
            source_description=record.source_description,
        ),
        text=text,
        unavailable=unavailable,
    )


@dataclass(frozen=True, slots=True)
class PromptFileRead:
    """The facts read for the requested keys and the custody transactions the read spent."""

    files: Mapping[PromptFileKey, AdvisorPromptFile]
    transactions: int

    def __post_init__(self) -> None:
        freeze_fields(self, "files")


def _read_prompt_text(
    blob_service: BlobServiceProtocol, record: BlobRecord, key: PromptFileKey, context: SessionOperationContext
) -> AdvisorPromptFile:
    """The hash-verified, strictly decoded text of a file not recorded as the user's own writing, or the fact naming why not.

    A missing row, a row no longer ready and absent stored bytes are custody
    states the advisor is told about. A blob whose stored bytes disagree with
    its own row hash raises ``BlobIntegrityError`` (Tier 1) and propagates.
    """
    blob_id, sha256, encoding = key
    try:
        content_record, content = blob_service.read_blob_content_sync(blob_id, context)
    except BlobNotFoundError:
        return _facts(record, unavailable="not_found")
    except BlobStateError:
        return _facts(record, unavailable="not_ready")
    except BlobContentMissingError:
        return _facts(record, unavailable="content_missing")
    if content_record.content_hash != sha256:
        return _facts(content_record, unavailable="changed_since_wired")
    try:
        text = content.decode(encoding)
    except UnicodeDecodeError:
        return _facts(content_record, unavailable="not_text")
    return _facts(content_record, text=text)


def _batched_records(
    blob_service: BlobServiceProtocol, blob_ids: frozenset[UUID], context: SessionOperationContext
) -> tuple[tuple[BlobRecord, ...], UUID | None]:
    """One fenced ``get_blobs_sync``: every record, or no record and the id that failed the read.

    The batch fails whole on its first missing or out-of-custody id and names
    it. An id outside the requested set would make the caller's retry loop
    unbounded, so it is a contract breach and raises.
    """
    try:
        return blob_service.get_blobs_sync(blob_ids, context), None
    except BlobNotFoundError as exc:
        missing = UUID(exc.blob_id)
        if missing not in blob_ids:
            raise InvariantError(f"get_blobs_sync named blob {missing}, which was not requested") from exc
        return (), missing


def _read_prompt_file_metadata(
    blob_service: BlobServiceProtocol, blob_ids: Iterable[UUID], context: SessionOperationContext
) -> tuple[dict[UUID, BlobRecord], int]:
    """Fenced metadata for every blob in one ``get_blobs_sync`` transaction, and the transactions spent.

    The seam for the metadata read. A blob that is missing or out of this
    session's custody fails the whole batch and names itself; it is left out
    of the result (stated ``not_found``) and the rest are read again, so each
    such blob costs one more transaction. The usual case is exactly one.
    """
    remaining = frozenset(blob_ids)
    transactions = 0
    while remaining:
        records, missing = _batched_records(blob_service, remaining, context)
        transactions += 1
        if missing is None:
            return {record.id: record for record in records}, transactions
        remaining = remaining - {missing}
    return {}, transactions


def _record_prompt_files(
    blob_service: BlobServiceProtocol, record: BlobRecord, keys: Sequence[PromptFileKey], context: SessionOperationContext
) -> tuple[dict[PromptFileKey, AdvisorPromptFile], int]:
    """Facts for every marker that selects one read blob, and the content transactions spent.

    One content read per marker whose file is not recorded as the user's own
    writing; a user-written file is never content-read.
    """
    author = advisor_prompt_file_author(
        created_by=record.created_by, creation_modality=record.creation_modality, source_description=record.source_description
    )
    files: dict[PromptFileKey, AdvisorPromptFile] = {}
    transactions = 0
    for key in keys:
        if record.status != "ready":
            files[key] = _facts(record, unavailable="not_ready")
        elif record.content_hash != key[1]:
            files[key] = _facts(record, unavailable="changed_since_wired")
        elif author == "user":
            files[key] = _facts(record)
        elif record.size_bytes > BLOB_INLINE_PER_REF_BYTE_CAP:
            files[key] = _facts(record, unavailable="too_large")
        else:
            transactions += 1
            files[key] = _read_prompt_text(blob_service, record, key, context)
    return files, transactions


def read_advisor_prompt_files(
    keys: Iterable[PromptFileKey],
    *,
    blob_service: BlobServiceProtocol,
    context: SessionOperationContext,
) -> PromptFileRead:
    """Read the facts, and the text of every file not recorded as the user's own writing, under the caller's fence.

    Synchronous custody I/O: call it in a worker thread. One batched metadata
    read for every blob (no byte read for a user-written file), then the
    hash-verified content read for the rest, one per marker. The row's hash
    must equal the marker's, as the review card requires; the bytes decode
    strictly with the marker's encoding, as the runtime resolver decodes them.
    A blob the metadata read does not return is ``not_found``.
    """
    wanted = sorted(set(keys))
    records, transactions = _read_prompt_file_metadata(blob_service, (key[0] for key in wanted), context)
    files: dict[PromptFileKey, AdvisorPromptFile] = {}
    for blob_id, blob_keys in groupby(wanted, key=itemgetter(0)):
        if blob_id not in records:
            files.update(dict.fromkeys(blob_keys, _NOT_FOUND))
            continue
        blob_files, spent = _record_prompt_files(blob_service, records[blob_id], tuple(blob_keys), context)
        files.update(blob_files)
        transactions += spent
    return PromptFileRead(files=files, transactions=transactions)
