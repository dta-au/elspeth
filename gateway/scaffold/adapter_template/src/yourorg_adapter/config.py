"""TEMPLATE: adapter-specific configuration options, and model-target validation.

``validate_configuration`` receives the ``options`` dict the gateway core
passes when it resolves the adapter (currently always ``{}`` -- there is no
per-request configuration surface yet). Replace this once your adapter
needs deployment-specific options (a region, a deployment id, an upstream
API version pin). Until then, keep it accepting nothing, exactly like the
reference adapter (``elspeth_llm_gateway.reference.adapter``), so a stray
option fails startup loudly instead of being silently ignored.

``validate_model_target`` is the one you MUST implement: it is what
``/readyz`` uses to decide whether each configured
``ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS`` value is one your ``request.py`` can
actually read. The mapping value is opaque to the gateway core, so if you
leave this as-is a deployment mapped to a target your adapter cannot use
will pass readiness and then fail every completion. The conformance kit
checks that your adapter implements it (``/readyz`` reports
``adapter.validates_model_targets``), so a derived image that skips this
does not qualify.
"""


def validate_configuration(options: dict) -> None:
    if options:
        raise ValueError(f"yourorg_adapter accepts no configuration options yet, got: {sorted(options)}")


def validate_model_target(target: dict) -> None:
    # TRANSLATION POINT: assert exactly the keys `request.build_invoke` reads
    # out of `CanonicalRequest.model_target`, and only those. Keep the two in
    # step: a rule that drifts from what build_invoke actually reads puts the
    # readiness gate back to passing while completions fail.
    #
    # Purely computational -- no I/O, no filesystem or network access, no
    # credential lookup: /readyz makes no OAuth call and no upstream call,
    # and this must not be what changes that. `target` is a deep copy, so
    # mutating it has no effect; raise any exception to reject it (the
    # message is never published, only a fixed readiness error code).
    raise NotImplementedError("yourorg_adapter: implement validate_model_target against your real model-target shape")
