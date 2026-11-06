"""Complete classifier return DATA with its original result and terminal source.

This document is a cold value, not an END or publication permission. The SQL
owner separately replays the original P4, physical PRIMARY, END and model-pass
sources before it can use any locator retained here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import ContinuationValue, PrivateChunkRef
from elspeth.web.composer.continuation_codec import PrivateChunk
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_result_codec import (
    RetainedComposerResult,
    decode_retained_composer_result,
    encode_retained_composer_result,
)

type ClassifierReturnSourceKind = Literal["classifier_primary", "source_proof_stop", "unclassified"]
type ClassifierTerminalKind = Literal["model_end", "owned_refusal", "owned_control", "owned_error", "owned_recovery"]
type ClassifierEndDisposition = Literal["clean", "blocked"]


class _ReturnWire(ContinuationValue):
    schema_version: Literal["composer.iteration-return.v1"] = "composer.iteration-return.v1"
    result_json: str
    source_kind: ClassifierReturnSourceKind
    terminal_kind: ClassifierTerminalKind
    end_disposition: ClassifierEndDisposition | None
    primary_effect_key: str | None = Field(pattern=r"^provider:0:(0|[1-9][0-9]*)$")
    primary_input_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    primary_response: PrivateChunkRef | None
    end_effect_key: str | None
    pending_stage_key: str | None
    end_pass_message_id: str | None
    end_pass_index: int | None = Field(ge=0)

    @model_validator(mode="after")
    def require_complete_original_stamp(self) -> Self:
        primary = (self.primary_effect_key, self.primary_input_sha256, self.primary_response)
        if any(item is None for item in primary) != all(item is None for item in primary):
            raise ValueError("classifier return lost its complete original PRIMARY locator")
        if self.primary_response is not None and self.primary_response.codec != "composer.response.v1":
            raise ValueError("classifier return changed its original PRIMARY response codec")
        if self.source_kind == "classifier_primary" and all(item is None for item in primary):
            raise ValueError("classifier PRIMARY return lost its physical source locator")
        if self.source_kind != "classifier_primary" and any(item is not None for item in primary):
            raise ValueError("non-PRIMARY classifier return invented a physical source")
        if (self.end_effect_key is None) != (self.pending_stage_key is None):
            raise ValueError("classifier return lost its original pending END pair")
        if self.end_effect_key is not None and (not self.end_effect_key or not self.pending_stage_key):
            raise ValueError("classifier return lost its original pending END identity")
        if (self.end_pass_message_id is None) != (self.end_pass_index is None):
            raise ValueError("classifier return lost its original model pass locator")
        if self.end_pass_message_id is not None and not self.end_pass_message_id:
            raise ValueError("classifier return lost its original model pass identity")
        if self.terminal_kind == "model_end" and self.end_disposition is None:
            raise ValueError("classifier model END return lost its original disposition")
        if self.terminal_kind != "model_end" and self.end_disposition == "clean":
            raise ValueError("owned classifier return cannot claim a clean model END")
        if self.source_kind == "source_proof_stop" and self.terminal_kind != "owned_refusal":
            raise ValueError("source-proof stop changed its original owned refusal")
        if self.end_disposition == "clean" and (
            self.source_kind != "classifier_primary" or self.end_effect_key is None or self.end_pass_message_id is None
        ):
            raise ValueError("clean classifier return lost its complete original END and model pass locators")
        if self.source_kind == "unclassified" and self.end_pass_message_id is not None:
            raise ValueError("unclassified return cannot claim an original model pass")
        return self


@dataclass(frozen=True, slots=True)
class IterationReturnEnvelope:
    """Full original result plus untrusted locators for independent cold proof."""

    retained: RetainedComposerResult
    result_json: str
    source_kind: ClassifierReturnSourceKind
    terminal_kind: ClassifierTerminalKind
    end_disposition: ClassifierEndDisposition | None
    primary_effect_key: str | None
    primary_input_sha256: str | None
    primary_response: PrivateChunkRef | None
    end_effect_key: str | None
    pending_stage_key: str | None
    end_pass_message_id: str | None
    end_pass_index: int | None


def _wire(value: IterationReturnEnvelope) -> _ReturnWire:
    return _ReturnWire(
        result_json=value.result_json,
        source_kind=value.source_kind,
        terminal_kind=value.terminal_kind,
        end_disposition=value.end_disposition,
        primary_effect_key=value.primary_effect_key,
        primary_input_sha256=value.primary_input_sha256,
        primary_response=value.primary_response,
        end_effect_key=value.end_effect_key,
        pending_stage_key=value.pending_stage_key,
        end_pass_message_id=value.end_pass_message_id,
        end_pass_index=value.end_pass_index,
    )


def encode_iteration_return_envelope(value: IterationReturnEnvelope, *, recorder_source: PrivateChunk) -> str:
    if type(value) is not IterationReturnEnvelope or type(value.retained) is not RetainedComposerResult:
        raise TypeError("iteration return requires its complete owned result and terminal source")
    if value.result_json != encode_retained_composer_result(value.retained, recorder_source=recorder_source):
        raise AuditIntegrityError("iteration return substituted its original complete result bytes")
    if value.end_disposition == "clean" and value.retained.publication_binding is None:
        raise AuditIntegrityError("clean iteration return lost its original model publication binding")
    encoded = canonical_json(_wire(value).model_dump(mode="json"))
    if len(encoded.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("iteration return exceeds its complete private document bound")
    if decode_iteration_return_envelope(encoded, recorder_source=recorder_source) != value:
        raise AuditIntegrityError("iteration return changed in its complete cold encoding")
    return encoded


def decode_iteration_return_envelope(encoded: str, *, recorder_source: PrivateChunk) -> IterationReturnEnvelope:
    if type(encoded) is not str or len(encoded.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("iteration return has no bounded canonical document")
    parsed, error = parse_json_strict(encoded)
    if error is not None or canonical_json(parsed) != encoded:
        raise AuditIntegrityError("iteration return changed its canonical document")
    try:
        wire = _ReturnWire.model_validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("iteration return lost its closed original terminal source") from error
    if canonical_json(wire.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("iteration return normalized its original terminal source")
    retained = decode_retained_composer_result(wire.result_json, recorder_source=recorder_source)
    if wire.end_disposition == "clean" and retained.publication_binding is None:
        raise AuditIntegrityError("clean iteration return lost its original model publication binding")
    return IterationReturnEnvelope(
        retained=retained,
        result_json=wire.result_json,
        source_kind=wire.source_kind,
        terminal_kind=wire.terminal_kind,
        end_disposition=wire.end_disposition,
        primary_effect_key=wire.primary_effect_key,
        primary_input_sha256=wire.primary_input_sha256,
        primary_response=wire.primary_response,
        end_effect_key=wire.end_effect_key,
        pending_stage_key=wire.pending_stage_key,
        end_pass_message_id=wire.end_pass_message_id,
        end_pass_index=wire.end_pass_index,
    )
