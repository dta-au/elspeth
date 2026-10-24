"""Closed historical application values; none carries execution authority."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from pydantic import Field, model_validator

from elspeth.web.composer.continuation import ContinuationValue, LogicalRequestBinding, PrivateChunkRef


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
    response_json: str
    pending_batch: PrivateChunkRef
    pending_batch_json: str
    prepared_input: PrivateChunkRef
    prepared_input_json: str
    result: PrivateChunkRef
    result_json: str
    verification: Literal["verified", "not_applicable", "authored_state_mismatch", "checkpoint_decode_or_compare_failed"]


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
