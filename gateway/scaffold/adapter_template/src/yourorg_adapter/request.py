"""TEMPLATE: translate a CanonicalRequest into your upstream's InvokePlan.

This is the module every real translation decision lives in: message shape,
generation parameters, tool declarations, tool-choice constraints, and
structured-output format. Everywhere this template cannot fill in for you
-- because it depends on your agency's real wire schema -- is marked
``# TRANSLATION POINT`` and currently raises ``NotImplementedError``, so an
unmodified copy of this scaffold fails loudly the first time it is actually
invoked rather than silently producing a wrong request.

Read ``elspeth_llm_gateway.reference.adapter`` (the fictional
``reference_v1_invoke`` adapter shipped with the gateway) alongside this
file -- it is the worked example this template is deliberately structured
to mirror, including its own ``# TRANSLATION POINT`` comments at each
corresponding decision.
"""

from elspeth_llm_gateway.sdk.protocol import InvokePlan
from elspeth_llm_gateway.sdk.types import CanonicalRequest


def build_invoke(request: CanonicalRequest) -> InvokePlan:
    # TRANSLATION POINT: build your upstream's real request path and body
    # from request.messages, request.model_target, request.temperature,
    # request.seed, request.max_tokens, request.tools, request.tool_choice,
    # request.tool_choice_function, and request.response_format.
    #
    # InvokePlan.path is relative to the configured upstream_origin and is
    # fail-closed by construction: no leading slash, no scheme, no '..', no
    # '?'/'#', no whitespace. InvokePlan.headers may not set Authorization,
    # Host, Cookie, or X-Forwarded-For (case-insensitively) -- the gateway
    # core owns the Authorization header end to end.
    raise NotImplementedError("yourorg_adapter.request.build_invoke: implement against your agency's real invoke schema")
