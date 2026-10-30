"""Closed historical application values; none carries execution authority."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from pydantic import Field, model_validator

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ContinuationValue, LogicalRequestBinding, PrivateChunkRef
from elspeth.web.composer.continuation_codec import PrivateChunk, verify_private_chunk
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES


@dataclass(frozen=True, slots=True)
class P4EffectProducerData:
    creator_sequence: int
    creator_generation: int
    creator_operation_id: str
    creator_operation_epoch: int

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not int or value < 1
                for value in (
                    self.creator_sequence,
                    self.creator_generation,
                    self.creator_operation_epoch,
                )
            )
            or type(self.creator_operation_id) is not str
            or not self.creator_operation_id
        ):
            raise ValueError("historical P4 producer has incomplete original identity")


class PortableP4ApplicationRecord(ContinuationValue):
    checkpoint_id: str = Field(min_length=1)
    cursor: int = Field(ge=0)
    effect_key: str
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_id: str
    creator_sequence: int = Field(ge=1)
    creator_generation: int = Field(ge=1)
    creator_operation_id: str = Field(min_length=1)
    creator_operation_epoch: int = Field(ge=1)
    response: PrivateChunkRef
    pending_batch: PrivateChunkRef
    prepared_input: PrivateChunkRef
    result: PrivateChunkRef
    verification: Literal["verified", "not_applicable", "authored_state_mismatch", "checkpoint_decode_or_compare_failed"]

    @model_validator(mode="after")
    def source_roles(self) -> PortableP4ApplicationRecord:
        if (
            self.response.codec != "composer.response.v1"
            or self.pending_batch.codec != "composer.p4-pending-batch.v1"
            or self.prepared_input.codec != "composer.prepared-p4.v1"
            or self.result.codec != "composer.owner-result.v1"
        ):
            raise ValueError("application record changed its closed original source roles")
        return self


class PortableP4ApplicationEntry(ContinuationValue):
    """One complete ordered journal and shared graph, without recursive copies."""

    schema_version: Literal["composer.evidence-applications.v1"] = "composer.evidence-applications.v1"
    logical: LogicalRequestBinding
    applications: tuple[PortableP4ApplicationRecord, ...]
    graph_json: str

    @model_validator(mode="after")
    def unique_effects(self) -> PortableP4ApplicationEntry:
        keys = tuple(item.effect_key for item in self.applications)
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("portable application journal must contain unique actual effects")
        cursors = tuple(item.cursor for item in self.applications)
        if any(left >= right for left, right in pairwise(cursors)):
            raise ValueError("portable application journal changed selected cursor order")
        return self


@dataclass(frozen=True, slots=True, init=False)
class P4ApplicationSources:
    """Original bounded leaf owners; no graph or current SQL authority.

    Each occurrence is checked before storage identity deduplication. Complete
    selected SQL cohorts may include other frame sources; only the four closed
    record roles enter this owner, in their first-occurrence order.
    """

    chunks: tuple[PrivateChunk, ...]

    def __init__(self, entry: PortableP4ApplicationEntry | None, chunks: tuple[PrivateChunk, ...]) -> None:
        if (entry is not None and type(entry) is not PortableP4ApplicationEntry) or type(chunks) is not tuple:
            raise TypeError("application sources require original entry/chunk owners")
        available: dict[tuple[str, str], PrivateChunk] = {}
        for chunk in chunks:
            if type(chunk) is not PrivateChunk:
                raise AuditIntegrityError("application source is not its original private chunk")
            key = chunk.reference.codec, chunk.reference.digest
            prior = available.get(key)
            if prior is not None and prior != chunk:
                raise AuditIntegrityError("application source identity has conflicting original bytes or lengths")
            available.setdefault(key, chunk)
        selected: dict[tuple[str, str], PrivateChunk] = {}
        for record in () if entry is None else entry.applications:
            for reference in (record.response, record.pending_batch, record.prepared_input, record.result):
                key = reference.codec, reference.digest
                prior = selected.get(key)
                if prior is not None and prior.reference != reference:
                    raise AuditIntegrityError("application reference has conflicting lengths before deduplication")
                actual = available.get(key)
                if actual is None or actual.reference != reference:
                    raise AuditIntegrityError("application lost its original selected dependency")
                if not 0 <= reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES or (
                    utf8_size(actual.canonical, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES) != reference.byte_length
                ):
                    raise AuditIntegrityError("application dependency exceeds its original complete document bound")
                verify_private_chunk(reference, actual.canonical)
                selected.setdefault(key, actual)
        object.__setattr__(self, "chunks", tuple(selected.values()))

    def chunk(self, reference: PrivateChunkRef) -> PrivateChunk:
        for chunk in self.chunks:
            if chunk.reference == reference:
                return chunk
        raise AuditIntegrityError("application role has no admitted original source")
