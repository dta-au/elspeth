"""Portable completed attachment application DATA, never a current grant."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AttachmentApplicationData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.attachment-application.v1"] = "composer.attachment-application.v1"
    effect_key: str = Field(min_length=1)
    source_checkpoint_id: str = Field(min_length=1)
    source_cursor: int = Field(ge=0)
    source_snapshot_json: str
    prepared_json: str
    response_json: str
    published_result_json: str
    receipt_json: str
    before_evidence_json: str
    after_evidence_json: str
    after_batch_json: str
    before_iteration_json: str
    after_iteration_json: str
    before_caches_json: str
    after_caches_json: str
    application_sequence: int = Field(ge=1)
    application_generation: int = Field(ge=1)
    application_operation_id: str = Field(min_length=1)
    application_operation_epoch: int = Field(ge=1)
