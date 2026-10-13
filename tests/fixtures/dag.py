"""Shared DAG test settings and explicit-routing plugin construction helpers."""

from __future__ import annotations

from typing import Any

from elspeth.cli_helpers import PluginBundle

_AUTO_SOURCE_ON_SUCCESS = "_auto_source_on_success"
_AUTO_GATE_INPUT = "_auto_gate_input"
_AUTO_AGG_INPUT = "_auto_agg_input"
_AUTO_TRANSFORM_INPUT_PREFIX = "_auto_transform_input_"


def _source_settings(cls: Any, /, **kwargs: Any) -> Any:
    """Build SourceSettings with backward-compatible defaults for legacy tests."""
    options = dict(kwargs.pop("options", {}) or {})
    on_success = kwargs.pop("on_success", None)
    if on_success is None:
        on_success = options.pop("on_success", _AUTO_SOURCE_ON_SUCCESS)
    return cls(on_success=on_success, options=options, **kwargs)


def _transform_settings(cls: Any, /, **kwargs: Any) -> Any:
    """Build TransformSettings with explicit on_success and on_error.

    Callers MUST provide on_success explicitly — no defaults are inferred.
    on_error defaults to "discard" (a concrete valid value, not a sentinel).
    """
    options = dict(kwargs.pop("options", {}) or {})
    on_success = kwargs.pop("on_success", None)
    if "on_success" in options:
        on_success = options.pop("on_success")
    on_error = kwargs.pop("on_error", "discard")
    if "on_error" in options:
        on_error = options.pop("on_error")
    name = kwargs.pop("name", None)
    plugin = kwargs.get("plugin", "transform")
    if name is None:
        name = f"{plugin}_{abs(hash((plugin, repr(sorted(options.items()))))) % 10_000}"
    if on_success is None:
        msg = f"_transform_settings: on_success is required for transform '{name}' — no defaults inferred"
        raise ValueError(msg)
    input_connection = kwargs.pop("input", None)
    if input_connection is None:
        input_connection = f"{_AUTO_TRANSFORM_INPUT_PREFIX}{name}"
    return cls(
        name=name,
        input=input_connection,
        on_success=on_success,
        on_error=on_error,
        options=options,
        **kwargs,
    )


def _gate_settings(cls: Any, /, **kwargs: Any) -> Any:
    """Build GateSettings with default input for legacy tests."""
    input_connection = kwargs.pop("input", _AUTO_GATE_INPUT)
    return cls(input=input_connection, **kwargs)


def _aggregation_settings(cls: Any, /, **kwargs: Any) -> Any:
    """Build AggregationSettings from legacy call sites."""
    options = dict(kwargs.pop("options", {}) or {})
    on_success = kwargs.pop("on_success", None)
    if on_success is None and "on_success" in options:
        on_success = options.pop("on_success")
    on_error = kwargs.pop("on_error", "discard")
    input_connection = kwargs.pop("input", _AUTO_AGG_INPUT)
    return cls(input=input_connection, on_success=on_success, on_error=on_error, options=options, **kwargs)


def _apply_explicit_success_routing(settings: Any) -> Any:
    """Inject explicit on_success routing for legacy test fixtures.

    These tests historically relied on `default_sink`. This helper migrates
    fixture settings to explicit sink routing before plugin instantiation.
    The mutation is applied to a deep copy to avoid test-to-test bleed.
    """
    routed_settings = settings.model_copy(deep=True)

    sinks = routed_settings.sinks
    if not sinks:
        return routed_settings
    sink_name = next(iter(sinks.keys()))

    transforms = list(routed_settings.transforms)
    gates = list(routed_settings.gates)
    aggregations = list(routed_settings.aggregations)

    source_on_success = routed_settings.sources["primary"].on_success
    if source_on_success == _AUTO_SOURCE_ON_SUCCESS:
        if transforms or gates or aggregations:
            source_on_success = "source_out"
        else:
            source_on_success = sink_name

    updated_transforms = []
    previous_connection = source_on_success
    for _index, transform in enumerate(transforms):
        input_connection = transform.input
        if input_connection.startswith(_AUTO_TRANSFORM_INPUT_PREFIX):
            input_connection = previous_connection

        # on_success is always explicit — no inference needed.
        updated_transform = transform.model_copy(update={"input": input_connection})
        updated_transforms.append(updated_transform)
        previous_connection = transform.on_success

    updated_aggregations: list[Any] = []
    for index, aggregation in enumerate(aggregations):
        input_connection = aggregation.input
        if input_connection == _AUTO_AGG_INPUT:
            if index == 0:
                if updated_transforms:
                    input_connection = updated_transforms[-1].on_success
                else:
                    input_connection = source_on_success
            else:
                input_connection = updated_aggregations[index - 1].name

        on_success = aggregation.on_success
        if on_success is None:
            on_success = sink_name

        updated_aggregations.append(
            aggregation.model_copy(
                update={
                    "input": input_connection,
                    "on_success": on_success,
                }
            )
        )

    upstream_for_gates = source_on_success
    if updated_transforms:
        upstream_for_gates = updated_transforms[-1].on_success
    elif updated_aggregations:
        upstream_for_gates = updated_aggregations[-1].on_success

    updated_gates: list[Any] = []
    for index, gate in enumerate(gates):
        input_connection = gate.input
        if input_connection == _AUTO_GATE_INPUT:
            if index == 0:
                input_connection = upstream_for_gates
            else:
                prev_gate = updated_gates[index - 1]
                # Check if the previous gate has a named output connection
                # (i.e., at least one route was renamed from "continue" to a
                # connection name). If all routes are "continue", the DAG model
                # resolves them at runtime; the next gate shares the same input.
                prev_named_outputs = [
                    d for d in prev_gate.routes.values() if d != "continue" and d not in {str(s) for s in sinks} and d != "fork"
                ]
                if prev_named_outputs:
                    input_connection = prev_named_outputs[0]
                else:
                    input_connection = prev_gate.input

        routes = dict(gate.routes)
        if index < len(gates) - 1:
            # Rename lone "continue" routes to named connections so the next gate
            # can consume them. When multiple routes all say "continue", keep them
            # as-is — the DAG resolves "continue" at runtime.
            continue_count = sum(1 for d in routes.values() if d == "continue")
            if continue_count <= 1:
                routes = {
                    label: (f"{gate.name}_out" if destination == "continue" else destination) for label, destination in routes.items()
                }

        updated_gates.append(
            gate.model_copy(
                update={
                    "input": input_connection,
                    "routes": routes,
                }
            )
        )

    updated_source = routed_settings.sources["primary"].model_copy(update={"on_success": source_on_success})

    routed_settings = routed_settings.model_copy(
        update={
            "sources": {
                **dict(routed_settings.sources),
                "primary": updated_source,
            },
            "transforms": updated_transforms,
            "aggregations": updated_aggregations,
            "gates": updated_gates,
        }
    )

    if routed_settings.coalesce and routed_settings.gates:
        branch_to_gate_idx: dict[str, int] = {}
        for gate_idx, gate in enumerate(routed_settings.gates):
            if gate.fork_to:
                for branch in gate.fork_to:
                    branch_to_gate_idx[branch] = gate_idx

        updated_coalesce = []
        for coalesce_cfg in routed_settings.coalesce:
            produced_idxs = [branch_to_gate_idx[b] for b in coalesce_cfg.branches if b in branch_to_gate_idx]
            if produced_idxs and max(produced_idxs) == len(routed_settings.gates) - 1 and coalesce_cfg.on_success is None:
                updated_coalesce.append(coalesce_cfg.model_copy(update={"on_success": sink_name}))
            else:
                updated_coalesce.append(coalesce_cfg)

        routed_settings = routed_settings.model_copy(update={"coalesce": updated_coalesce})

    return routed_settings


def instantiate_plugins_from_config_raw(settings: Any) -> PluginBundle:
    """Instantiate plugins without any test-time routing injection."""
    from elspeth.cli_helpers import instantiate_plugins_from_config as _real_instantiate

    return _real_instantiate(settings)


def instantiate_plugins_from_config(settings: Any) -> PluginBundle:
    """Centralized test factory wrapper for plugin instantiation."""
    routed_settings = _apply_explicit_success_routing(settings)
    # Back-patch routed settings onto the original settings object so callers
    # that pass settings.coalesce or settings.gates to from_plugin_instances()
    # get the routed values (with auto-inputs resolved).
    if routed_settings.coalesce != settings.coalesce:
        object.__setattr__(settings, "coalesce", routed_settings.coalesce)
    if list(routed_settings.gates) != list(settings.gates):
        object.__setattr__(settings, "gates", routed_settings.gates)
    return instantiate_plugins_from_config_raw(routed_settings)
