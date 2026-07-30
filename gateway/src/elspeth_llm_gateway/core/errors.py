"""The gateway's closed error vocabulary and its leak-proof envelope.

``GatewayError`` is deliberately unable to carry free text: its constructor
takes a ``GatewayErrorCode`` only, and every rendered message comes from the
fixed ``SAFE_MESSAGE`` table below. This is what keeps upstream bodies,
tracebacks, and internal identifiers from ever reaching a caller through an
error path — callers only ever see one of the 14 codes and its matching
operator-safe sentence.

The module-level check at the bottom binds this table to the SDK's
adapter-facing vocabulary: ``elspeth_llm_gateway.sdk.protocol.CLASSIFIABLE_CODES``
must always be a subset of the codes defined here, so an adapter can never
classify a failure into a code the core does not know how to render. Core is
allowed to import sdk (never the reverse), so the check lives here. It is a
plain ``if``/``raise RuntimeError`` rather than an ``assert`` specifically
because ``assert`` is compiled out entirely under ``python -O``.
"""

from enum import StrEnum

from elspeth_llm_gateway.sdk.protocol import CLASSIFIABLE_CODES


class GatewayErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    INBOUND_AUTHENTICATION_FAILED = "inbound_authentication_failed"
    CONTRACT_MISMATCH = "contract_mismatch"
    MODEL_NOT_ALLOWED = "model_not_allowed"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"
    CONTENT_POLICY_REJECTED = "content_policy_rejected"
    OAUTH_TOKEN_UNAVAILABLE = "oauth_token_unavailable"
    UPSTREAM_UNAUTHORIZED = "upstream_unauthorized"
    UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_RESPONSE_INVALID = "upstream_response_invalid"
    INTERNAL_ERROR = "internal_error"


HTTP_STATUS: dict[GatewayErrorCode, int] = {
    GatewayErrorCode.INVALID_REQUEST: 400,
    GatewayErrorCode.INBOUND_AUTHENTICATION_FAILED: 401,
    GatewayErrorCode.CONTRACT_MISMATCH: 400,
    GatewayErrorCode.MODEL_NOT_ALLOWED: 404,
    GatewayErrorCode.CAPABILITY_UNSUPPORTED: 422,
    GatewayErrorCode.CONTEXT_LENGTH_EXCEEDED: 400,
    GatewayErrorCode.CONTENT_POLICY_REJECTED: 400,
    GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE: 503,
    GatewayErrorCode.UPSTREAM_UNAUTHORIZED: 502,
    GatewayErrorCode.UPSTREAM_RATE_LIMITED: 429,
    GatewayErrorCode.UPSTREAM_TIMEOUT: 504,
    GatewayErrorCode.UPSTREAM_UNAVAILABLE: 503,
    GatewayErrorCode.UPSTREAM_RESPONSE_INVALID: 502,
    GatewayErrorCode.INTERNAL_ERROR: 500,
}

SAFE_MESSAGE: dict[GatewayErrorCode, str] = {
    GatewayErrorCode.INVALID_REQUEST: "The request was malformed or failed validation.",
    GatewayErrorCode.INBOUND_AUTHENTICATION_FAILED: "Authentication failed for this request.",
    GatewayErrorCode.CONTRACT_MISMATCH: "The request does not match the expected contract.",
    GatewayErrorCode.MODEL_NOT_ALLOWED: "The requested model is not permitted for this deployment.",
    GatewayErrorCode.CAPABILITY_UNSUPPORTED: "The requested capability is not supported for this model.",
    GatewayErrorCode.CONTEXT_LENGTH_EXCEEDED: "The request exceeds the model's context length limit.",
    GatewayErrorCode.CONTENT_POLICY_REJECTED: "The request was rejected by content policy.",
    GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE: "An upstream access token could not be obtained.",
    GatewayErrorCode.UPSTREAM_UNAUTHORIZED: "The upstream service rejected the request as unauthorized.",
    GatewayErrorCode.UPSTREAM_RATE_LIMITED: "The upstream service is rate limiting requests.",
    GatewayErrorCode.UPSTREAM_TIMEOUT: "The upstream service did not respond in time.",
    GatewayErrorCode.UPSTREAM_UNAVAILABLE: "The upstream service is currently unavailable.",
    GatewayErrorCode.UPSTREAM_RESPONSE_INVALID: "The upstream service returned an invalid response.",
    GatewayErrorCode.INTERNAL_ERROR: "An internal error occurred while processing the request.",
}

RETRYABLE: dict[GatewayErrorCode, bool] = {
    GatewayErrorCode.INVALID_REQUEST: False,
    GatewayErrorCode.INBOUND_AUTHENTICATION_FAILED: False,
    GatewayErrorCode.CONTRACT_MISMATCH: False,
    GatewayErrorCode.MODEL_NOT_ALLOWED: False,
    GatewayErrorCode.CAPABILITY_UNSUPPORTED: False,
    GatewayErrorCode.CONTEXT_LENGTH_EXCEEDED: False,
    GatewayErrorCode.CONTENT_POLICY_REJECTED: False,
    GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE: True,
    GatewayErrorCode.UPSTREAM_UNAUTHORIZED: False,
    GatewayErrorCode.UPSTREAM_RATE_LIMITED: True,
    GatewayErrorCode.UPSTREAM_TIMEOUT: True,
    GatewayErrorCode.UPSTREAM_UNAVAILABLE: True,
    GatewayErrorCode.UPSTREAM_RESPONSE_INVALID: False,
    GatewayErrorCode.INTERNAL_ERROR: False,
}


class GatewayError(Exception):
    """A gateway failure identified by code only — never by free text.

    ``status``, ``retryable``, and ``safe_message`` are derived from the
    fixed tables above at construction time, so nothing an adapter or an
    upstream response says can ever reach a caller through this type.
    """

    def __init__(self, code: GatewayErrorCode) -> None:
        self.code = code
        self.status = HTTP_STATUS[code]
        self.retryable = RETRYABLE[code]
        self.safe_message = SAFE_MESSAGE[code]
        super().__init__(f"{code.value}: {self.safe_message}")


def error_envelope(error: GatewayError, request_id: str) -> dict:
    """Render the OpenAI-shaped error envelope for a ``GatewayError``."""
    return {
        "error": {
            "message": error.safe_message,
            "type": "gateway_error",
            "code": error.code.value,
            "retryable": error.retryable,
            "request_id": request_id,
        }
    }


if not ({code.value for code in GatewayErrorCode} >= CLASSIFIABLE_CODES):
    # Deliberately not a bare `assert`: an `assert` statement is stripped
    # entirely under `python -O` / `PYTHONOPTIMIZE`, which would silently
    # drop this binding check in an optimized deployment -- exactly the
    # environment where a real drift between the two vocabularies would go
    # unnoticed until an adapter tried to classify into a code core doesn't
    # know how to render.
    raise RuntimeError("sdk.protocol.CLASSIFIABLE_CODES must be a subset of GatewayErrorCode")
