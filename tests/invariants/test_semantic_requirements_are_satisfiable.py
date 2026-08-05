"""Every semantic requirement must be satisfiable by some registered producer.

The semantic-contract mechanism (``contracts/plugin_semantics``, enforced by
``web/composer/_semantic_validator``) sits OUTSIDE the ADR-010 declaration-trust
framework: no ``DeclarationContract``, no dispatch site, and — load-bearing here
— no ``negative_example()``. ADR-010 made that classmethod mandatory precisely
because it "closes the dormant-``runtime_check`` failure mode". Semantic
requirements have no equivalent gate.

The gap is not hypothetical. ``json_explode`` shipped
``accepted_value_types={SemanticValueType.LIST}`` with ``unknown_policy=FAIL``
on 2026-05-07 (5e29e115f). No plugin declares ``LIST`` as a fact — it appears
only as that requirement — so the transform was unwireable in the Composer from
every producer while the YAML path ran the same pipeline clean. It survived
three months (elspeth-7a2c9a24c3).

This is the missing gate. A FAIL-policy requirement no registered plugin can
satisfy is a fail-closed gate on an impossible declaration: it protects nothing
and simply removes the consumer from the product. WARN is the honest policy for
a property only knowable per row — see ADR-008 §Alternative 3 and ADR-014 §Tier
classification on declaration-lie vs row-level data error.

Both rosters are discovered from a live ``PluginManager`` so a newly registered
plugin cannot silently escape the check.
"""

from __future__ import annotations

from typing import Any

from elspeth.contracts.plugin_semantics import (
    SemanticOutcome,
    UnknownSemanticPolicy,
    compare_semantic,
)
from elspeth.plugins.infrastructure.manager import PluginManager


def _registered_plugin_classes() -> tuple[type, ...]:
    """Every built-in source, transform and sink class."""
    manager = PluginManager()
    manager.register_builtin_plugins()
    return (*manager.get_sources(), *manager.get_transforms(), *manager.get_sinks())


def _probe_instance(plugin_cls: type) -> Any | None:
    """Instantiate via ``probe_config()``; None when the plugin cannot be probed.

    A plugin that declares neither semantics nor a probe config contributes
    nothing to either roster, so skipping it cannot mask a defect.
    """
    probe_config = getattr(plugin_cls, "probe_config", None)
    if probe_config is None:
        return None
    try:
        return plugin_cls(probe_config())
    except (NotImplementedError, TypeError):
        return None


def _declares_own(plugin_cls: type, method_name: str) -> bool:
    """Whether the class overrides ``method_name`` rather than inheriting the base default."""
    return any(
        method_name in klass.__dict__
        for klass in plugin_cls.__mro__[:-1]
        if klass.__module__.startswith("elspeth.plugins.") and klass.__module__ != "elspeth.plugins.infrastructure.base"
    )


def _declared_facts() -> list[Any]:
    """Collect every FieldSemanticFacts any registered plugin can declare."""
    facts: list[Any] = []
    for plugin_cls in _registered_plugin_classes():
        if not _declares_own(plugin_cls, "output_semantics"):
            continue
        instance = _probe_instance(plugin_cls)
        if instance is None:
            continue
        facts.extend(instance.output_semantics().fields)
    return facts


def _declared_requirements() -> list[tuple[type, Any]]:
    """Collect every FieldSemanticRequirement any registered plugin declares."""
    requirements: list[tuple[type, Any]] = []
    for plugin_cls in _registered_plugin_classes():
        if not _declares_own(plugin_cls, "input_semantic_requirements"):
            continue
        instance = _probe_instance(plugin_cls)
        if instance is None:
            continue
        requirements.extend((plugin_cls, req) for req in instance.input_semantic_requirements().fields)
    return requirements


def test_semantic_rosters_are_non_empty() -> None:
    """Guard the guard: an empty roster would make the real test vacuously pass."""
    assert _declared_facts(), "No registered plugin declares output_semantics() — the satisfiability check below would be vacuous."
    assert _declared_requirements(), (
        "No registered plugin declares input_semantic_requirements() — the satisfiability check below would be vacuous."
    )


def test_every_fail_policy_requirement_has_a_possible_producer() -> None:
    """A FAIL requirement no producer can satisfy makes its consumer unwireable.

    Failing here means a ``FieldSemanticRequirement`` demands a fact no plugin
    declares. Either declare the fact on a producer that honestly has it, or set
    ``unknown_policy=WARN`` because the property is only knowable at runtime —
    do not leave FAIL, which silently removes the consumer from the Composer
    while the YAML path keeps working.
    """
    available_facts = _declared_facts()

    for consumer_cls, requirement in _declared_requirements():
        if requirement.unknown_policy is not UnknownSemanticPolicy.FAIL:
            continue
        satisfiable = any(compare_semantic(facts, requirement) is SemanticOutcome.SATISFIED for facts in available_facts)
        assert satisfiable, (
            f"{consumer_cls.__name__} requirement {requirement.requirement_code!r} uses "
            f"unknown_policy=FAIL but NO registered plugin declares facts that satisfy it, "
            f"so the consumer cannot be wired from any producer. Declare the fact on a real "
            f"producer, or use WARN if the property is only knowable per row."
        )


def test_json_explode_list_requirement_stays_advisory() -> None:
    """Pin the elspeth-7a2c9a24c3 resolution against a silent revert to FAIL.

    ``SemanticValueType.LIST`` is structurally undeclarable: ELSPETH's atomic
    unit is one row and a row field holds a discrete value, so the schema DSL
    has no list type and a list only ever rides transiently in an ``any`` field
    awaiting deaggregation. If a producer ever declares LIST honestly, the
    satisfiability test above becomes the guard and this pin can be retired.
    """
    pinned = [
        requirement
        for _consumer_cls, requirement in _declared_requirements()
        if requirement.requirement_code == "json_explode.array_field.list"
    ]

    assert pinned, "json_explode must still declare its array_field list requirement"
    for requirement in pinned:
        assert requirement.unknown_policy is UnknownSemanticPolicy.WARN, (
            "json_explode.array_field.list must stay WARN while no producer declares "
            "SemanticValueType.LIST; FAIL makes json_explode unwireable from every "
            "producer (elspeth-7a2c9a24c3)."
        )
