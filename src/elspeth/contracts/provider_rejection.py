"""Safe, serializable evidence of a rejected provider request.

Only closed reason codes cross this boundary. Provider prose, parameter names,
request values and the original exception belong outside this public contract.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RequestRejectionReason = Literal["invalid_request", "context_length_exceeded", "unsupported_parameter"]
ReasonDisposition = Literal["retained", "absent", "unrecognized", "unavailable"]
ReasonSource = Literal["sdk_body", "http_response", "unavailable"]


class SafeRequestRejectionData(TypedDict):
    category: Literal["request_rejected"]
    exception_type: Literal["BadRequestError"]
    reason_code: RequestRejectionReason | None
    reason_disposition: ReasonDisposition
    reason_source: ReasonSource
    diagnostic_reference: str | None
    retryable: Literal[False]


class SafeRequestRejection(BaseModel):
    """An owned explanation of exactly the safe reason evidence retained."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    category: Literal["request_rejected"] = "request_rejected"
    exception_type: Literal["BadRequestError"] = "BadRequestError"
    reason_code: RequestRejectionReason | None
    reason_disposition: ReasonDisposition
    reason_source: ReasonSource
    diagnostic_reference: str | None = Field(default=None, pattern=r"^provider-failure:[0-9a-f]{64}$")
    retryable: Literal[False] = False

    @field_validator("retryable", mode="before")
    @classmethod
    def _require_false_boolean(cls, value: object) -> Literal[False]:
        if type(value) is not bool or value:
            raise ValueError("A rejected request must be explicitly nonretryable")
        return False

    @model_validator(mode="after")
    def _check_reason_evidence(self) -> Self:
        if (self.reason_disposition == "retained") != (self.reason_code is not None):
            raise ValueError("Only a retained rejection reason may contain a reason code")
        if self.reason_disposition == "retained" and self.reason_source == "unavailable":
            raise ValueError("A retained rejection reason requires an observed source")
        return self

    @property
    def explanation(self) -> str:
        """Render owned text; never interpolate any upstream field or value."""
        if self.reason_code == "context_length_exceeded":
            return "The request exceeded the model's context limit. Reduce the review context before trying again."
        if self.reason_code == "unsupported_parameter":
            return "The request included an unsupported parameter. Check the provider request configuration before trying again."
        if self.reason_code == "invalid_request":
            return "The provider rejected invalid request input. A more specific rejection reason is unavailable."
        if self.reason_disposition == "absent":
            return "The provider rejected the request. No structured reason code was supplied in the inspected error envelope."
        if self.reason_disposition == "unrecognized":
            return "The provider rejected the request. Its structured reason could not be safely recognized."
        return "The provider rejected the request. The structured rejection reason is unavailable."

    def to_wire(self) -> SafeRequestRejectionData:
        return {
            "category": self.category,
            "exception_type": self.exception_type,
            "reason_code": self.reason_code,
            "reason_disposition": self.reason_disposition,
            "reason_source": self.reason_source,
            "diagnostic_reference": self.diagnostic_reference,
            "retryable": self.retryable,
        }

    @property
    def public_detail(self) -> str:
        reference = f" Diagnostic reference: {self.diagnostic_reference}." if self.diagnostic_reference is not None else ""
        return "The completion advisory review request was rejected; Composer completion remains withheld. " + self.explanation + reference

    @property
    def handoff_detail(self) -> str:
        reference = f" Diagnostic reference: {self.diagnostic_reference}." if self.diagnostic_reference is not None else ""
        return "The completion advisory review request was rejected. " + self.explanation + reference

    @classmethod
    def from_wire(cls, value: object) -> Self:
        """Strictly reconstruct the closed public contract, never its cause."""
        if type(value) is dict:
            admitted = value
        elif isinstance(value, MappingProxyType):
            admitted = dict(value)
        else:
            raise ValueError("Safe rejection descriptor requires an owned dictionary")
        if set(admitted) != {
            "category",
            "exception_type",
            "reason_code",
            "reason_disposition",
            "reason_source",
            "diagnostic_reference",
            "retryable",
        }:
            raise ValueError("Safe rejection descriptor requires exactly its complete wire fields")
        return cls.model_validate(admitted, strict=True)
