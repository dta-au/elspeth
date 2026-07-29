"""Adapter SDK: canonical types and the closed capability vocabulary.

This package is the public vocabulary adapters and the core service share. It
must not import anything from ``elspeth_llm_gateway.core`` or from the
surrounding ELSPETH repository.
"""

from elspeth_llm_gateway.sdk.protocol import (
    CLASSIFIABLE_CODES,
    AdapterDescriptor,
    AdapterProtocol,
    ErrorClassification,
    InvokePlan,
    UpstreamFailure,
)
from elspeth_llm_gateway.sdk.types import (
    CanonicalMessage,
    CanonicalRequest,
    CanonicalResponse,
    CanonicalToolCall,
    CanonicalToolDef,
    CanonicalUsage,
    Capability,
    FinishReason,
    ResponseFormatSpec,
)

__all__ = [
    "CLASSIFIABLE_CODES",
    "AdapterDescriptor",
    "AdapterProtocol",
    "CanonicalMessage",
    "CanonicalRequest",
    "CanonicalResponse",
    "CanonicalToolCall",
    "CanonicalToolDef",
    "CanonicalUsage",
    "Capability",
    "ErrorClassification",
    "FinishReason",
    "InvokePlan",
    "ResponseFormatSpec",
    "UpstreamFailure",
]
