"""TEMPLATE: adapter-specific configuration options.

``validate_configuration`` receives the ``options`` dict the gateway core
passes when it resolves the adapter (currently always ``{}`` -- there is no
per-request configuration surface yet). Replace this once your adapter
needs deployment-specific options (a region, a deployment id, an upstream
API version pin). Until then, keep it accepting nothing, exactly like the
reference adapter (``elspeth_llm_gateway.reference.adapter``), so a stray
option fails startup loudly instead of being silently ignored.
"""


def validate_configuration(options: dict) -> None:
    if options:
        raise ValueError(f"yourorg_adapter accepts no configuration options yet, got: {sorted(options)}")
