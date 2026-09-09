"""A transform's output contract must never mint an explicit zero from an abstention.

``SchemaConfig.guaranteed_fields`` carries a THREE-way vocabulary, and the
distinction is semantic, not stylistic (``contracts/schema.py``,
``declares_guaranteed_fields``):

* ``None`` = ABSTAIN — "I make no claim." Skipped in the coalesce intersection,
  and deferred to per-row enforcement at a sink.
* ``()``   = EXPLICIT ZERO — "I claim nothing." PARTICIPATES in the vote and
  collapses the intersection to empty.
* ``(...)`` = a real claim.

Every transform that builds an output contract starts from its INPUT contract's
guarantees and adds its own emitted fields. The idiom that does so is

    base_guaranteed = set(schema_config.guaranteed_fields or ())

which reads the raw tuple, so an abstaining input arrives as the empty set and
is indistinguishable from an input that explicitly guaranteed nothing. Four
builders guard the difference explicitly (``BaseTransform._build_output_schema_config``,
``value_transform``, ``json_explode``, ``_build_llm_output_schema_config`` all
test ``guaranteed_fields is not None`` before declaring); two more preserve it a
second way (``pdf_rasterize``, ``blob_csv_expand`` fall back to the input's own
tuple when their computed set is empty). The rest declare unconditionally and
are safe only because their added set is never empty.

THIS TEST IS THE GATE ON THAT "never empty". For every registered transform,
driven through its own ``probe_config()`` — whose input schema abstains — the
resulting output ``guaranteed_fields`` must be ``None`` (abstention preserved)
or non-empty (a real claim about fields the transform genuinely adds). ``()`` is
the failure: an abstention laundered into a participating claim of nothing.

WHY IT MATTERS. The minted ``()`` is not inert. It participates in the coalesce
intersection and collapses it to empty, and it flips a sink's abstention
deferral into a build-time verdict — so a downstream consumer stops being told
"unknown, check per row" and starts being told "guaranteed: nothing." The
polarity is the dangerous one on at least one path: composer and runtime read
these halves through different accessors, and the epic that produced this test
(``elspeth-3fd13a27ed`` and the ``field_mapper`` fix in ``341e07c1a``) found a
live composer/runtime disagreement rooted in exactly this collapse.

DISCOVERY, NOT A LITERAL ROSTER — the same doctrine
``test_transform_probe_coverage`` states, for the same reason. A hand-typed list
stops covering whatever is added after it was typed. Membership here is every
registered transform, so a new plugin lands in this gate automatically or the
gate is worthless.

WHAT THIS PROVES, AND WHAT IT DOES NOT. It proves the property for each
plugin's OWN ``probe_config()`` — one configuration per transform, the canonical
one. It does NOT prove it for all configurations. Five builders declare
``guaranteed_fields`` unconditionally with no ``is not None`` guard and no
empty-set fallback:

    web_scrape, blob_fetch, line_explode, reference_join,
    and the llm-source arm (build_llm_source_output_schema_config)

Each is safe today only because its own config model forces at least one added
field — ``reference_join`` rejects an empty ``output`` map at construction, and
the other four each have a required output-field option that defaults non-empty.
That is a per-plugin structural argument, NOT something this test checks: a
future option making any of those sets optional would reintroduce the mint, and
this test would only catch it if the plugin's own ``probe_config()`` exercised
the empty shape. Stated plainly rather than left for someone to assume the
coverage is total.
"""

from __future__ import annotations

from typing import cast

from elspeth.contracts.schema import SchemaConfig
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.manager import PluginManager
from tests.invariants.test_pass_through_invariants import (
    _probe_instantiate,
    _UnprobeableTransform,
)

# Transforms excluded for a documented structural reason.
#
# EMPTY, and that is the honest state: every registered transform is probeable
# (``test_every_registered_transform_is_probeable`` gates that separately) and
# every one of them either builds an output contract or declines to, both of
# which this file handles as legitimate outcomes.
#
# A name added here must carry a structural justification, never "it was
# failing". A minted ``()`` is a defect to fix at the plugin's own
# ``_build_output_schema_config``; suppressing it here would reintroduce exactly
# the blindness this gate exists to remove.
_STRUCTURAL_EXCLUSIONS: frozenset[str] = frozenset()


def _registered_transform_classes() -> tuple[type[BaseTransform], ...]:
    """Every built-in transform class, from a live registry.

    The cast documents the framework invariant its sibling gates rely on: the
    registry is typed to the protocol, but every registered plugin subclasses
    ``BaseTransform`` to inherit the contract machinery read here.
    """
    manager = PluginManager()
    manager.register_builtin_plugins()
    return cast("tuple[type[BaseTransform], ...]", tuple(manager.get_transforms()))


def _plugin_name(plugin_cls: type[BaseTransform]) -> str:
    return plugin_cls.name


def _in_scope() -> tuple[type[BaseTransform], ...]:
    return tuple(cls for cls in _registered_transform_classes() if _plugin_name(cls) not in _STRUCTURAL_EXCLUSIONS)


def _probe_contracts(plugin_cls: type[BaseTransform]) -> tuple[SchemaConfig | None, SchemaConfig | None]:
    """Return ``(input contract, output contract)`` for one transform's canonical probe.

    Reads ``_schema_config`` / ``_output_schema_config`` directly rather than
    through a sentinel ``getattr``: ``BaseTransform`` supplies both, so a missing
    attribute is a framework bug that must crash rather than be coerced into a
    passing verdict (ADR-032 — nominally type what ELSPETH owns).
    """
    transform = _probe_instantiate(plugin_cls)
    try:
        return transform._schema_config, transform._output_schema_config
    finally:
        transform.close()


def _classify(output_contract: SchemaConfig | None) -> str:
    """Bucket one transform's output contract. See the module docstring."""
    if output_contract is None:
        return "no output contract"
    if output_contract.guaranteed_fields is None:
        return "preserved"
    if len(output_contract.guaranteed_fields) == 0:
        return "minted empty"
    return "claims"


def test_probe_inputs_abstain_so_this_gate_is_not_vacuous() -> None:
    """Guard the guard: the premise the whole file rests on.

    Every assertion below is about what a transform does with an ABSTAINING
    input. If a ``probe_config()`` ever declares explicit ``guaranteed_fields``,
    that transform stops exercising the abstention path and silently drops out
    of the gate while still reporting green — the vacuous pass this file's
    siblings exist to catch. Fail here instead, naming the plugin.
    """
    in_scope = _in_scope()
    assert in_scope, "No transforms in scope — every assertion below would be vacuously true."

    non_abstaining: dict[str, tuple[str, ...]] = {}
    for plugin_cls in in_scope:
        try:
            input_contract, _output_contract = _probe_contracts(plugin_cls)
        except _UnprobeableTransform:
            # Probeability is its own gate (test_every_registered_transform_is_probeable).
            # Duplicating that failure here would report one defect as two.
            continue
        if input_contract is not None and input_contract.guaranteed_fields is not None:
            non_abstaining[_plugin_name(plugin_cls)] = input_contract.guaranteed_fields

    assert non_abstaining == {}, (
        f"These transforms' probe_config() declares explicit guaranteed_fields, so their canonical probe "
        f"no longer exercises the abstaining-input path this file gates: {non_abstaining}. "
        f"Either drop the declaration from probe_config(), or add a second probe that abstains."
    )


def test_classification_covers_both_outcomes_so_the_gate_discriminates() -> None:
    """Probe validity: a registry that stopped loading must not read as success.

    An empty registry, or one where every transform landed in a single arm,
    would make the main assertion below trivially true. Requiring at least one
    transform in EACH of the two legitimate arms means the gate is demonstrably
    able to tell them apart.
    """
    buckets: dict[str, list[str]] = {}
    for plugin_cls in _in_scope():
        try:
            _input_contract, output_contract = _probe_contracts(plugin_cls)
        except _UnprobeableTransform:
            continue
        buckets.setdefault(_classify(output_contract), []).append(_plugin_name(plugin_cls))

    assert buckets.get("preserved"), (
        "No transform preserved its input's abstention (guaranteed_fields is None). "
        "Expected at least passthrough/truncate/type_coerce/keyword_filter — a registry "
        "that loaded nothing, or a semantic change, would look identical to a pass here."
    )
    assert buckets.get("claims"), (
        "No transform declared a non-empty guarantee. Expected the additive majority "
        "(web_scrape, blob_fetch, line_explode, the batch_* family, ...)."
    )


def test_no_transform_mints_an_explicit_zero_from_an_abstaining_input() -> None:
    """THE INVARIANT. ``()`` out of an abstaining input is an abstention laundered into a claim.

    Failures accumulate rather than aborting at the first one: a bare loop with
    an inline assert stops at the earliest offender and hides every plugin behind
    it, which for a whole-registry gate is the same blindness it exists to catch.
    """
    minted: dict[str, str] = {}
    for plugin_cls in _in_scope():
        try:
            input_contract, output_contract = _probe_contracts(plugin_cls)
        except _UnprobeableTransform:
            continue
        if input_contract is not None and input_contract.guaranteed_fields is not None:
            # Not an abstaining input — reported by the premise gate above.
            continue
        if _classify(output_contract) == "minted empty":
            minted[_plugin_name(plugin_cls)] = "output guaranteed_fields == () while its input contract abstained (None)"

    assert minted == {}, (
        f"These transforms convert an abstaining input contract into an EXPLICIT ZERO guarantee: {minted}. "
        f"An empty tuple participates in the coalesce intersection and collapses it, and turns a sink's "
        f"per-row deferral into a build-time verdict of 'guarantees nothing'. Fix at the plugin's "
        f"_build_output_schema_config: declare the tuple only when the upstream declared one "
        f"(`schema_config.guaranteed_fields is not None`) or when the computed set is non-empty — "
        f"see BaseTransform._build_output_schema_config for the canonical guard."
    )
