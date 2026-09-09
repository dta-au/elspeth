"""TEMPLATE: the adapter class the gateway resolves by entry point.

Once ``descriptor.py``, ``config.py``, ``request.py``, ``response.py``, and
``errors.py`` are all implemented against your agency's real schema, this
class is what ``pyproject.toml``'s
``[project.entry-points."elspeth_llm_gateway.adapters"]`` table points the
gateway at, keyed by ``descriptor.ADAPTER_NAME`` -- and what
``ELSPETH_LLM_GATEWAY_ADAPTER`` is set to at deploy time. Nothing in this
file itself is a translation point; it only wires the five modules above
together to satisfy ``elspeth_llm_gateway.sdk.protocol.AdapterProtocol``
plus the optional ``ModelTargetValidator`` (see ``config.py``).
"""

from elspeth_llm_gateway.sdk.protocol import AdapterDescriptor, ErrorClassification, InvokePlan, UpstreamFailure
from elspeth_llm_gateway.sdk.types import CanonicalRequest, CanonicalResponse

from yourorg_adapter import config, descriptor, errors, request, response


class YourOrgAdapter:
    """Implements ``elspeth_llm_gateway.sdk.protocol.AdapterProtocol``."""

    def descriptor(self) -> AdapterDescriptor:
        return descriptor.build_descriptor()

    def validate_configuration(self, options: dict) -> None:
        config.validate_configuration(options)

    def validate_model_target(self, target: dict) -> None:
        # Implements the optional ``sdk.protocol.ModelTargetValidator``, which
        # is what makes /readyz a real admission gate for this deployment's
        # model mappings. Optional to the runtime, mandatory for conformance.
        config.validate_model_target(target)

    def build_invoke(self, canonical_request: CanonicalRequest) -> InvokePlan:
        return request.build_invoke(canonical_request)

    def parse_success(self, body: dict) -> CanonicalResponse:
        return response.parse_success(body)

    def classify_error(self, failure: UpstreamFailure) -> ErrorClassification:
        return errors.classify_error(failure)
