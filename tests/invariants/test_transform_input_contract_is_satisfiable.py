"""Every registered transform must be able to consume what actually arrives.

The output side of the transform contract has a gate — ``guaranteed_fields``
must cover ``declared_output_fields``, enforced across real instances by
``tests/integration/plugins/transforms/test_output_schema_contract.py``. The
INPUT side had none, and the input side is where the unsatisfiable contract
lives: a transform whose ``input_schema`` REQUIRES a field the transform itself
CREATES rejects every row at ``TransformExecutor``'s
``input_schema.model_validate(..., strict=True)``. Not some rows — every row,
because the field cannot be present before the transform that writes it runs
(elspeth-d6eeb3a71d).

``BaseTransform.input_schema`` closes that by demoting
``self_created_input_fields - consumed_input_fields`` to optional on the derived
input model. This file is the durable, registry-wide form of the one-off parity
probe that fix was verified with: it re-arms the trap on every registered
transform and asserts the framework disarms it.

WHICH "REQUIRED" SURFACE THIS TARGETS, because the schema DSL has two and they
behave OPPOSITELY on a created field:

* ``fields: ["x: any"]`` — ``fields[].required`` is True unless the spec carries
  the ``?`` optional marker. This is the surface demotion touches, deliberately
  excluded from the DAG's build-time required set (``core/dag/guarantees.py``),
  and it is the surface this file arms.
* ``schema.required_fields: [x]`` — a CONSUMPTION declaration. It is a
  non-negotiable member of ``consumed_input_fields`` (see that property's
  docstring) precisely because ``get_raw_node_required_fields`` reads it to build
  the build-time contract, so demoting it would split compile-time from runtime.
  A created field named there is NOT demoted and stays required — that is the
  documented second-stage-aggregation case (a transform reading an upstream
  ``mean`` while emitting its own), indistinguishable from author error at the
  config layer, and therefore not this file's business. The agreement of those
  two layers is gated by
  ``tests/invariants/test_input_schema_config_is_captured.py``.

The predicate below is written against ``created - consumed`` rather than
``created`` alone for exactly that reason: a field the transform declares it
READS may legitimately stay required, whatever else it does with it.

This also generalises the ``09dc6407f1`` class. A config option that NAMES a
field the transform creates is picked up by ``_config_named_input_columns``,
lands in ``consumed_input_fields``, and so escapes demotion — leaving the
created field required on input. That reaches the same unsatisfiable contract by
a different route, and the armed sweep here is what would catch it registry-wide.

KNOWN LIMITS, so nobody reads more assurance into this than it carries:

* Every transform is exercised under its ``probe_config()``, ONE arbitrary
  configuration. A declaration that only appears under some other config — a
  ``mode``-conditional field block, an option that adds a created field only when
  enabled — is invisible here. This is the same limit the semantic-satisfiability
  and schema-capture gates carry, and for the same reason.
* Only the ``fields[].required`` surface is armed, per the split above.
* Satisfiability is asked of the DECLARED model, not of a live pipeline. A
  contract that is satisfiable in principle but that no upstream producer in a
  given DAG actually satisfies is a topology question, not this one.

All three are strictly weaker than the defect this catches — a transform that
demands on input a field it exists to create — so the gate is worth having
as-is. Tighten it if any limit ever hides a real defect.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, cast

from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.manager import PluginManager

# Plugins whose config validation REJECTS the armed config below. Each rejection
# is a fail-closed answer to an author error, not a coverage gap:
#
# * ``batch_outlier_annotator`` / ``batch_replicate`` forbid DECLARING the fields
#   they add while the option that adds them is on — the strongest possible
#   answer to this defect class, refusing the trap shape outright.
# * ``json_explode`` / ``line_explode`` require their source column to appear in
#   ``fields``, and a probe schema that declares none leaves the armed config
#   naming only created fields.
#
# A NEW name appearing here is a decision to make, not a skip to accept: it means
# a transform stopped being exercised by the load-bearing sweep.
_EXPECTED_ARMING_REJECTIONS = frozenset(
    {
        "batch_outlier_annotator",
        "batch_replicate",
        "json_explode",
        "line_explode",
    }
)

# Transforms that must stay inside the armed sweep. Not an arbitrary sample — one
# per creation mechanism, so a refactor cannot quietly drop a whole family:
#
# * ``llm`` / ``rag_retrieval`` — fields created from a provider response.
# * ``web_scrape`` / ``azure_document_intelligence`` — fields created from a
#   network fetch, the family whose configs name both read and written columns.
# * ``field_mapper`` — created set computed in ``__init__`` from ``mapping``, the
#   reference shape for overriding ``self_created_input_fields`` as a property.
# * ``batch_stats`` — batch aggregation, where output names are generic enough to
#   collide with a genuine upstream input.
# * ``value_transform`` — the one transform that keeps ``declared_output_fields``
#   EMPTY while still creating its operation targets, so it is the only member
#   whose created set comes from neither the declared outputs nor the config
#   columns. If it leaves this roster the override path is untested.
_ARMED_SWEEP_ANCHORS = frozenset(
    {
        "azure_document_intelligence",
        "batch_stats",
        "field_mapper",
        "llm",
        "rag_retrieval",
        "value_transform",
        "web_scrape",
    }
)


def _registered_transform_classes() -> tuple[type[BaseTransform], ...]:
    """Every built-in transform class, from a live registry.

    Discovery rather than a literal roster: a hand-typed list silently stops
    covering whatever is added after it was typed, which is the defect this
    file's own ticket was raised about.

    The cast documents the same framework invariant
    ``test_pass_through_invariants._registered_transform_classes`` relies on: the
    registry is typed to the protocol, but every registered plugin subclasses
    ``BaseTransform`` to inherit the demotion machinery read here.
    """
    manager = PluginManager()
    manager.register_builtin_plugins()
    return cast("tuple[type[BaseTransform], ...]", tuple(manager.get_transforms()))


def _probe_instance(plugin_cls: type[BaseTransform]) -> BaseTransform:
    """Instantiate via ``probe_config()``, or fail loudly.

    Unprobeable is NOT a skip. Every registered transform is probeable today, and
    a transform that lost that property would drop out of this gate entirely
    while still shipping — the silent-coverage-loss failure mode that
    ``test_semantic_requirements_are_satisfiable`` closes the same way.
    """
    assert any("probe_config" in klass.__dict__ for klass in plugin_cls.__mro__), (
        f"{plugin_cls.__name__} has no probe_config(), so its input contract is invisible to this gate. Add probe_config(); do not let a transform be skipped."
    )
    return plugin_cls(plugin_cls.probe_config())


def _declared_required_fields(transform: BaseTransform) -> frozenset[str]:
    """Fields the AUTHORED input model requires, BEFORE demotion runs."""
    declared = transform._declared_input_schema
    if declared is None:
        return frozenset()
    return frozenset(name for name, info in declared.model_fields.items() if info.is_required())


def _runtime_required_fields(transform: BaseTransform) -> frozenset[str]:
    """Fields the DERIVED input model requires — what the executor enforces per row."""
    return frozenset(name for name, info in transform.input_schema.model_fields.items() if info.is_required())


def _arm_created_fields(base: Mapping[str, Any], created: frozenset[str]) -> dict[str, Any]:
    """Config that DECLARES every created field as a required input field.

    Field specs default to ``required=True``; only a ``?`` marker makes one
    optional. So naming the created fields in ``fields`` — and never in
    ``required_fields``, which means consumption — arms exactly the
    elspeth-d6eeb3a71d trap on the surface demotion is responsible for.

    The probe's own declared fields are preserved: dropping them makes plugins
    that validate a source column against ``fields`` reject the result, which
    would look like a coverage gap rather than the mutation being wrong.
    ``mode`` is forced to ``flexible`` because ``observed`` mode refuses a
    ``fields`` block outright.
    """
    candidate = copy.deepcopy(dict(base))
    schema_block: dict[str, Any] = dict(candidate.get("schema") or {})
    existing = [spec for spec in (schema_block.get("fields") or []) if isinstance(spec, str)]
    existing_names = {spec.split(":")[0].strip().rstrip("?") for spec in existing}
    schema_block["mode"] = "flexible"
    schema_block["fields"] = [*existing, *(f"{name}: any" for name in sorted(created) if name not in existing_names)]
    candidate["schema"] = schema_block
    return candidate


class TestTransformInputContractIsSatisfiable:
    def test_transform_roster_is_non_empty_and_fully_probeable(self) -> None:
        """Guard the guard: an empty or unprobeable roster makes every test below vacuous."""
        classes = _registered_transform_classes()
        assert classes, "No transforms registered — every satisfiability check below would pass vacuously."
        for plugin_cls in classes:
            _probe_instance(plugin_cls)

    def test_no_transform_requires_a_field_it_creates(self) -> None:
        """Natural state: no shipped config demands on input a field it creates.

        Checks BOTH surfaces of the same question so a regression is attributable
        rather than merely detected:

        * ``authored`` — the transform's own config declares required a field it
          creates and does not read. That is a plugin-authoring defect even
          though demotion currently rescues it, because it means the declared
          contract and the real one disagree by design rather than by accident.
        * ``unsatisfiable`` — demotion did not rescue it, so the executor will
          reject every row.

        This assertion is WEAK TODAY and the docstring must say so: no registered
        transform declares ANY required input field under its ``probe_config()``,
        so there is presently nothing to demote and nothing to get wrong. It is
        kept as the direct mirror of the output-side gate and as the guard for a
        transform authored into the trap shape tomorrow;
        ``test_declaring_a_created_field_required_is_neutralised`` carries the
        load and is non-vacuous by construction.
        """
        authored: dict[str, list[str]] = {}
        unsatisfiable: dict[str, list[str]] = {}

        for plugin_cls in _registered_transform_classes():
            transform = _probe_instance(plugin_cls)
            plugin_name = plugin_cls.name

            # A field the transform READS may legitimately stay required, whatever
            # else it does with it — demotion is defined on created MINUS consumed.
            demotable = transform.self_created_input_fields - transform.consumed_input_fields
            if not demotable:
                continue

            declared_trap = sorted(demotable & _declared_required_fields(transform))
            if declared_trap:
                authored[plugin_name] = declared_trap
            runtime_trap = sorted(demotable & _runtime_required_fields(transform))
            if runtime_trap:
                unsatisfiable[plugin_name] = runtime_trap

        assert unsatisfiable == {}, (
            f"These transforms REQUIRE on input a field they create and do not read, so "
            f"TransformExecutor rejects every row: {unsatisfiable}. Either the field is "
            f"genuinely read — declare it in schema.required_fields or required_input_fields "
            f"so it counts as consumed — or it must not be declared required."
        )
        assert authored == {}, (
            f"These transforms declare required a field they create and do not read: {authored}. "
            f"Demotion currently rescues them at runtime, so this is not yet a live failure, but "
            f"the declared contract disagrees with the enforced one. Drop the requirement."
        )

    def test_declaring_a_created_field_required_is_neutralised(self) -> None:
        """Armed sweep: re-arm the elspeth-d6eeb3a71d trap registry-wide.

        For every transform that creates fields, rebuild it from a config that
        declares those fields required on input, then assert the derived
        ``input_schema`` requires none of the ones the transform does not read.
        This is the durable form of the parity probe that fix was verified with,
        and unlike the natural-state test above it is non-vacuous by
        construction: the arming step is asserted to have actually taken effect
        on the declared model before the demotion claim is checked.
        """
        armed: list[str] = []
        no_created_fields: list[str] = []
        rejected: dict[str, str] = {}
        unsatisfiable: dict[str, dict[str, Any]] = {}

        for plugin_cls in _registered_transform_classes():
            plugin_name = plugin_cls.name
            base = plugin_cls.probe_config()
            created = _probe_instance(plugin_cls).self_created_input_fields
            if not created:
                no_created_fields.append(plugin_name)
                continue

            try:
                transform = plugin_cls(_arm_created_fields(base, created))
            except Exception as exc:  # the rejection itself is the datum, not a skip
                rejected[plugin_name] = f"{type(exc).__name__}: {exc}"
                continue

            # Arming must have reached the DECLARED model, or the demotion claim
            # below would be asserted against a config that never posed the question.
            declared_required = _declared_required_fields(transform)
            assert created & declared_required, (
                f"{plugin_name}: arming did not make any created field required on the declared "
                f"input model, so the trap was never set and the check below would pass vacuously."
            )
            armed.append(plugin_name)

            demotable = transform.self_created_input_fields - transform.consumed_input_fields
            still_required = sorted(demotable & _runtime_required_fields(transform))
            if still_required:
                unsatisfiable[plugin_name] = {
                    "required_but_created": still_required,
                    "demoted": sorted(transform.demoted_input_fields),
                }

        assert armed, "armed no transform — the probe config shape or the created-field surface changed"
        assert _ARMED_SWEEP_ANCHORS.issubset(set(armed)), (
            f"transforms that must stay in the armed sweep left it: {sorted(_ARMED_SWEEP_ANCHORS - set(armed))}. "
            f"A creation mechanism is now untested."
        )
        # Every registered transform must land in exactly one bucket. Without this
        # the three buckets are just tallies: a transform could stop being armed
        # for a reason none of them records — an exception swallowed upstream, a
        # roster that quietly shrank — and the anchors would still pass because
        # they only check the names they name.
        registered = {cls.name for cls in _registered_transform_classes()}
        accounted = set(armed) | set(rejected) | set(no_created_fields)
        assert accounted == registered, (
            f"transforms unaccounted for by the armed sweep: {sorted(registered - accounted)}. "
            f"Each must be armed, rejected with a documented reason, or declare no created fields."
        )
        assert set(rejected) <= _EXPECTED_ARMING_REJECTIONS, (
            f"unexpected construction failures — these transforms lost coverage silently: "
            f"{ {name: rejected[name] for name in sorted(set(rejected) - _EXPECTED_ARMING_REJECTIONS)} }"
        )
        assert unsatisfiable == {}, (
            f"input_schema still REQUIRES fields these transforms create and do not read, so every "
            f"row would be rejected at the executor: {unsatisfiable}. BaseTransform.input_schema must "
            f"demote self_created_input_fields - consumed_input_fields (elspeth-d6eeb3a71d)."
        )
