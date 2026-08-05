"""A transform that can demote input fields must have captured its schema config.

``BaseTransform.consumed_input_fields`` builds the set that demotion is
subtracted against from three surfaces, two of which its docstring calls
NON-NEGOTIABLE: ADR-013's ``declared_input_fields`` and
``SchemaConfig.required_fields``. Those two are exactly what
``get_raw_node_required_fields`` (``contracts/schema.py``) reads to build the
DAG's BUILD-TIME required-field contract, while ``fields[].required`` — the
surface demotion touches — is deliberately excluded from it
(``core/dag/guarantees.py``). That is what makes the docstring's promise true:
demotion changes only the surface the DAG does not check, so the compile-time
and runtime contracts cannot diverge.

The promise held only for plugins that actually STORED their schema config. Ten
registered transforms passed their validated ``schema_config`` straight to the
schema factory and never assigned ``self._schema_config``, leaving the ``None``
class default in place — so the ``required_fields`` limb read nothing and
contributed an empty set. A field named in ``required_fields`` was then demoted
out of the runtime ``input_schema`` while ``get_raw_node_required_fields`` still
enforced it at build time: precisely the "silently splitting the two layers"
outcome the docstring warns about (elspeth-3790106260). Capture is now central,
in ``BaseTransform._initialize_declared_input_fields``.

This is the missing sibling of the ``_output_schema_config`` gate. It asserts
the RELATION rather than the attribute: ``_schema_config is not None`` would
pass for any SchemaConfig, including one unrelated to the authored config, so
the gate instead authors a ``required_fields`` entry and demands the two layers
agree about it.

The roster is discovered from a live ``PluginManager``. Anti-vacuity is
enforced three ways: the in-scope roster must be non-empty, it must still
contain the plugins this defect was measured on, and a plugin that REJECTS the
mutated config must be named in ``_EXPECTED_MUTATION_REJECTIONS`` rather than
silently dropping out of coverage.

KNOWN LIMITS, so nobody reads more assurance into this than it carries:

* Fields are taken from each plugin under its ``probe_config()``, one arbitrary
  configuration, and only the first demotable field is exercised per plugin. A
  capture regression reachable only under a non-probe config would not be
  caught here.
* The sweep authors ``schema.required_fields`` and never
  ``required_input_fields``, so it covers ONE of the two non-negotiable
  surfaces. The other, ADR-013's ``declared_input_fields``, is populated by the
  same central hook, and the one registered transform that does not reach that
  hook — ``json_explode``, whose config extends ``DataPluginConfig`` rather
  than ``TransformDataConfig`` by deliberate design — rejects
  ``required_input_fields`` outright as an extra key, so the surface is closed
  by construction there rather than by this gate.

Both are strictly weaker than the defect this catches — a plugin that never
captures its schema config under any configuration — so the gate is worth
having as-is. Tighten it if either limit ever hides a real defect.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from elspeth.contracts.schema import get_raw_node_required_fields
from elspeth.plugins.infrastructure.manager import PluginManager

# Plugins whose config validation legitimately REJECTS the synthetic mutation
# below, so they cannot be exercised through it. Each rejection is a fail-closed
# answer to an author error, not a capture gap:
#
# * ``batch_outlier_annotator`` / ``batch_replicate`` forbid declaring the
#   fields they add while the option that adds them is on.
# * ``json_explode`` / ``line_explode`` require their source column to be
#   declared in ``fields``, and a probe schema that declares none leaves the
#   mutation naming only created fields.
#
# ``line_explode`` — the plugin the defect was measured on end to end — is
# covered directly by
# ``tests/unit/plugins/infrastructure/test_self_created_input_demotion.py``
# (``test_required_fields_survives_on_a_transform_that_builds_schemas_indirectly``).
# A NEW name appearing here is a decision to make, not a skip to accept.
_EXPECTED_MUTATION_REJECTIONS = frozenset(
    {
        "batch_outlier_annotator",
        "batch_replicate",
        "json_explode",
        "line_explode",
    }
)

# Plugins that shipped with ``_schema_config`` unset AND have a demotable field
# under their probe config. They anchor the roster: if a refactor drops one out
# of scope, coverage of the original defect has been lost silently.
_MEASURED_REGRESSION_ANCHORS = frozenset(
    {
        "aws_textract_document_analysis",
        "azure_document_intelligence",
        "blob_csv_expand",
        "blob_fetch",
        "llm",
        "rag_retrieval",
        "web_scrape",
    }
)


def _registered_transform_classes() -> tuple[type, ...]:
    """Every built-in transform class, from a live registry."""
    manager = PluginManager()
    manager.register_builtin_plugins()
    return tuple(manager.get_transforms())


def _demand_created_field(base: Mapping[str, Any], created: frozenset[str]) -> tuple[dict[str, Any], str]:
    """Config that declares every created field and REQUIRES one of them.

    Preserves the probe's own declared fields — dropping them makes plugins that
    validate a source column against ``fields`` reject the result — and adds the
    created ones so the ``required_fields`` entry is declarable.
    """
    chosen = sorted(created)[0]
    candidate = copy.deepcopy(dict(base))
    schema_block: dict[str, Any] = dict(candidate.get("schema") or {})
    existing = [spec for spec in (schema_block.get("fields") or []) if isinstance(spec, str)]
    existing_names = {spec.split(":")[0].strip() for spec in existing}
    schema_block["mode"] = "flexible"
    schema_block["fields"] = [*existing, *(f"{name}: any" for name in sorted(created) if name not in existing_names)]
    schema_block["required_fields"] = [chosen]
    candidate["schema"] = schema_block
    return candidate, chosen


class TestInputSchemaConfigIsCaptured:
    def test_no_registered_transform_demotes_a_field_its_config_declares_required(self) -> None:
        """Build-time and runtime must name the same required field.

        ``get_raw_node_required_fields`` reads the authored options; the runtime
        contract is ``input_schema`` after demotion. A field in both the demoted
        set and the build-time required set is the two layers disagreeing.
        """
        in_scope: list[str] = []
        no_demotable_field: list[str] = []
        rejected: dict[str, str] = {}
        diverged: dict[str, dict[str, Any]] = {}

        for cls in _registered_transform_classes():
            plugin_name = getattr(cls, "name", cls.__name__)
            probe_config = getattr(cls, "probe_config", None)
            assert probe_config is not None, f"{plugin_name} has no probe_config(); the roster cannot be swept"

            base = probe_config()
            probed = cls(base)

            # Demotion can only touch fields the transform creates AND the
            # declared input model actually carries.
            created = probed.self_created_input_fields
            declared_model = probed._declared_input_schema
            if declared_model is None or not created:
                no_demotable_field.append(plugin_name)
                continue

            candidate, chosen = _demand_created_field(base, created)
            try:
                built = cls(candidate)
            except Exception as exc:  # the rejection itself is the datum, not a skip
                rejected[plugin_name] = f"{type(exc).__name__}: {exc}"
                continue

            in_scope.append(plugin_name)
            build_time_required = get_raw_node_required_fields(candidate, owner=plugin_name)
            assert chosen in build_time_required, (
                f"{plugin_name}: mutation did not reach the build-time contract — the gate would pass vacuously"
            )
            overlap = built.demoted_input_fields & build_time_required
            if overlap:
                diverged[plugin_name] = {
                    "demoted_at_runtime": sorted(overlap),
                    "required_at_build_time": sorted(build_time_required),
                    "captured_schema_config": built._schema_config is not None,
                }

        assert in_scope, "sweep exercised no transform — the probe shape changed"
        assert _MEASURED_REGRESSION_ANCHORS.issubset(set(in_scope)), (
            f"plugins this defect was measured on left the roster: {sorted(_MEASURED_REGRESSION_ANCHORS - set(in_scope))}"
        )
        assert set(rejected) <= _EXPECTED_MUTATION_REJECTIONS, (
            f"unexpected construction failures — these plugins lost coverage silently: "
            f"{ {name: rejected[name] for name in sorted(set(rejected) - _EXPECTED_MUTATION_REJECTIONS)} }"
        )
        assert diverged == {}, f"build-time required fields demoted at runtime: {diverged}"

    def test_every_transform_that_validates_its_config_captures_the_schema_block(self) -> None:
        """The central capture applies to every transform on the validated-config path.

        ``_initialize_declared_input_fields`` is the authoritative config-validation
        hook, and ``TransformDataConfig.schema_config`` is required and non-None,
        so any transform that reached that hook must expose a captured config.
        Plugins that capture by other means satisfy this too; the assertion is on
        the outcome, not the route.
        """
        uncaptured: list[str] = []
        checked = 0

        for cls in _registered_transform_classes():
            plugin_name = getattr(cls, "name", cls.__name__)
            probe_config = getattr(cls, "probe_config", None)
            assert probe_config is not None, f"{plugin_name} has no probe_config(); the roster cannot be swept"

            instance = cls(probe_config())
            checked += 1
            if instance._schema_config is None:
                uncaptured.append(plugin_name)

        assert checked > 0, "sweep exercised no transform — the registry is empty"
        assert uncaptured == [], f"transforms that never captured their input schema config: {uncaptured}"
