"""Nominal complete attachment evidence with lossless untrusted guard units."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from pydantic import JsonValue, ValidationError, model_validator

from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.prompt_guard import PromptGuardContentPart
from elspeth.web.composer.redaction import ReadAttachmentDataModel
from elspeth.web.composer.response_contracts import AdmittedResponse, ResponseContract

_ATTACHMENT_GUARD_UNIT_CHARACTERS = 8_000


class CompleteAttachmentDataModel(ReadAttachmentDataModel):
    """Exact producer facts, separate from the redacted audit shadow."""

    @model_validator(mode="after")
    def require_complete_attachment(self) -> CompleteAttachmentDataModel:
        if self.creation_modality not in CreationModality:
            raise ValueError("Attachment origin must use the recorded creation modality vocabulary")
        if self.start_char != 0 or self.end_char != self.total_chars or len(self.content) != self.total_chars:
            raise ValueError("Attachment content must cover its complete original character range")
        if self.truncated or self.next_offset_chars is not None:
            raise ValueError("A complete attachment cannot be truncated or have a continuation")
        encoded = self.content.encode("utf-8")
        if len(encoded) != self.size_bytes:
            raise ValueError("Attachment byte size must match its complete UTF-8 content")
        if hashlib.sha256(encoded).hexdigest() != self.content_hash:
            raise ValueError("Attachment identity must match its complete original UTF-8 bytes")
        return self


@dataclass(frozen=True, slots=True)
class AdmittedAttachmentResponse(AdmittedResponse):
    attachment: CompleteAttachmentDataModel
    contract: ReadAttachmentResponseContract

    def to_wire(self) -> JsonValue:
        return cast(JsonValue, self.attachment.model_dump(mode="json"))

    def readmit(self, contract: ResponseContract) -> AdmittedAttachmentResponse:
        if contract is not self.contract:
            raise FrameworkBugError("Cached attachment response contract changed")
        return self.contract.admit(self.attachment)

    def untrusted_content_parts(self) -> tuple[PromptGuardContentPart, ...]:
        content = self.attachment.content
        return tuple(
            PromptGuardContentPart(content[start : start + _ATTACHMENT_GUARD_UNIT_CHARACTERS])
            for start in range(0, len(content), _ATTACHMENT_GUARD_UNIT_CHARACTERS)
        )


class ReadAttachmentResponseContract(ResponseContract):
    def admit(self, value: object) -> AdmittedAttachmentResponse:
        if type(value) not in (CompleteAttachmentDataModel, dict, MappingProxyType):
            raise FrameworkBugError("Attachment producer returned an invalid response")
        try:
            attachment = CompleteAttachmentDataModel.model_validate(dict(value) if isinstance(value, MappingProxyType) else value)
        except (ValidationError, TypeError, ValueError):
            raise FrameworkBugError("Attachment producer returned an invalid complete response") from None
        return AdmittedAttachmentResponse(attachment, self)


READ_ATTACHMENT_RESPONSE_CONTRACT = ReadAttachmentResponseContract()
