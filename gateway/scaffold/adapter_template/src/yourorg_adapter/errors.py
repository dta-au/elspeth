"""TEMPLATE: classify an upstream failure into the gateway's closed vocabulary.

``classify_error`` may only return a code from
``elspeth_llm_gateway.sdk.protocol.CLASSIFIABLE_CODES`` -- anything else is
rejected by ``ErrorClassification``'s own validator. Read
``elspeth_llm_gateway.reference.adapter.ReferenceV1InvokeAdapter.classify_error``
(and its ``_FAULT_KIND_TO_CLASSIFICATION`` table) alongside this file for
the worked example.
"""

from elspeth_llm_gateway.sdk.protocol import ErrorClassification, UpstreamFailure

# A failure shape this adapter does not recognise must classify safely, not
# raise -- keep this as the fallback for every branch below that doesn't
# match.
_FALLBACK_CLASSIFICATION = ErrorClassification(code="upstream_response_invalid", retryable=False)


def classify_error(failure: UpstreamFailure) -> ErrorClassification:
    # TRANSLATION POINT: replace with your upstream's real failure-body ->
    # classification mapping. `failure.status` is the upstream HTTP status;
    # `failure.body` is the already-bounded parsed JSON body, or None if it
    # wasn't valid JSON. Every code you can plausibly emit needs
    # conformance/unit coverage of its own.
    if failure.body is None:
        return _FALLBACK_CLASSIFICATION
    return _FALLBACK_CLASSIFICATION
