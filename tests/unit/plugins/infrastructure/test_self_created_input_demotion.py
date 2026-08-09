"""Self-created fields must never be required on a transform's INPUT schema.

A transform's ``schema:`` block is its INPUT contract, but plugins also use it
to name the shape they emit. Declaring a field the transform CREATES made that
field required on input, so every row was rejected at
``TransformExecutor`` input validation (elspeth-d6eeb3a71d, elspeth-5955a9c421).

The fix demotes self-created fields to optional in the DERIVED INPUT pydantic
model only. The ``SchemaConfig`` is untouched, so the OUTPUT contract still
guarantees them and ``guaranteed_fields`` stays legal.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.manager import PluginManager


def _required_input_fields(transform: Any) -> set[str]:
    schema = transform.input_schema
    return {name for name, field in schema.model_fields.items() if field.is_required()}


def _registered_transform_classes(manager: PluginManager) -> list[type[BaseTransform]]:
    transforms: list[type[BaseTransform]] = []
    for candidate in manager.get_transforms():
        candidate_object: object = candidate
        if not isinstance(candidate_object, type) or not issubclass(candidate_object, BaseTransform):
            raise AssertionError(f"registered transform is not a BaseTransform: {candidate!r}")
        transforms.append(candidate_object)
    return transforms


class TestClassBodyDeclarationStillDemotes:
    """`input_schema = X` in a subclass BODY must not bypass demotion.

    A class-body assignment lands in ``cls.__dict__`` and wins the MRO lookup
    ahead of the base property, so reads would return the UNDEMOTED model —
    silently, since nothing raises. That pattern is the one BaseTransform's own
    docstring teaches, so it has to keep working AND be correct.
    """

    @staticmethod
    def _schema() -> type:
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        return create_schema_from_config(
            SchemaConfig.from_dict({"mode": "flexible", "fields": ["kept: str", "made: float"]}),
            "ClassBodyDemo",
            allow_coercion=False,
        )

    def test_class_body_assignment_is_routed_through_demotion(self) -> None:
        from elspeth.contracts import Determinism
        from elspeth.plugins.infrastructure.base import BaseTransform

        declared = self._schema()

        class Shadow(BaseTransform):
            name = "shadow_demo"
            determinism = Determinism.DETERMINISTIC
            input_schema = declared
            declared_output_fields = frozenset({"made"})

        transform = Shadow({"schema": {"mode": "flexible"}})

        assert "made" not in _required_input_fields(transform)
        transform.input_schema.model_validate({"kept": "a"}, strict=True)

    def test_class_body_assignment_does_not_leak_into_the_class_dict(self) -> None:
        """The raw model must not stay in __dict__, or it shadows again."""
        from elspeth.contracts import Determinism
        from elspeth.plugins.infrastructure.base import BaseTransform

        declared = self._schema()

        class Shadow(BaseTransform):
            name = "shadow_dict_demo"
            determinism = Determinism.DETERMINISTIC
            input_schema = declared

        assert "input_schema" not in Shadow.__dict__
        assert Shadow._declared_input_schema is declared


class TestDemotionDropsPresenceOnly:
    """Demotion removes the PRESENCE requirement and nothing else.

    Absent is fine — the transform creates the field. A value that IS supplied
    is still validated exactly as declared, constraints included: ``float`` maps
    to ``FiniteFloat``, whose ``allow_inf_nan=False`` lives in the FieldInfo
    METADATA rather than the annotation, so rebuilding a field from
    ``.annotation`` alone silently accepts NaN and Infinity in a codebase that
    goes out of its way to reject them.
    """

    def test_created_field_keeps_every_declared_constraint(self) -> None:
        from elspeth.plugins.transforms.batch_stats import BatchStats

        transform = BatchStats(
            {
                "schema": {"mode": "flexible", "fields": ["amount: float", "mean: float"]},
                "value_field": "amount",
            }
        )
        schema = transform.input_schema

        assert "mean" not in _required_input_fields(transform)
        # The assertion whose absence let the constraint drop through.
        assert schema.model_fields["mean"].metadata == schema.model_fields["amount"].metadata
        schema.model_validate({"amount": 1.0}, strict=True)
        schema.model_validate({"amount": 1.0, "mean": 2.5}, strict=True)
        for bad in (float("nan"), float("inf"), "1,234.00", None):
            with pytest.raises(ValidationError, match="mean"):
                schema.model_validate({"amount": 1.0, "mean": bad}, strict=True)

    def test_consumed_field_keeps_its_finite_float_constraint(self) -> None:
        """The arm that must NOT regress: no demotion means no constraint loss."""
        from elspeth.plugins.transforms.batch_stats import BatchStats

        transform = BatchStats(
            {
                "schema": {"mode": "flexible", "fields": ["mean: float"]},
                "value_field": "mean",
            }
        )

        assert "mean" in _required_input_fields(transform)
        assert transform.input_schema.model_fields["mean"].metadata
        transform.input_schema.model_validate({"mean": 2.5}, strict=True)
        for bad in (float("nan"), float("inf"), "1,234.00", None):
            with pytest.raises(ValidationError, match="mean"):
                transform.input_schema.model_validate({"mean": bad}, strict=True)


class TestConsumedColumnsSeeOptionDefaults:
    """An input column the author never typed is still a consumed column.

    ``consumed_input_fields`` reads the VALIDATED config, so an option left at
    its default contributes its value. Reading the raw authored dict instead
    would make the column invisible, and an invisible column is one that gets
    demoted — the same defect one door along.
    """

    def test_defaulted_input_column_option_is_consumed(self) -> None:
        from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand

        # blob_ref_field defaults to "blob_ref" and is NOT set here.
        transform = BlobCSVExpand({"schema": {"mode": "observed"}})

        assert "blob_ref_field" not in transform.config
        assert "blob_ref" in transform.consumed_input_fields

    def test_defaulted_output_column_option_is_not_consumed(self) -> None:
        """The other half: a defaulted OUTPUT option must stay demotable."""
        from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand

        transform = BlobCSVExpand({"schema": {"mode": "observed"}})

        assert "row_index_field" in transform.output_naming_config_keys
        assert "csv_row_index" not in transform.consumed_input_fields


class TestDemotionIsInspectable:
    """The framework's disagreement with the authored config must be greppable."""

    def test_demoted_input_fields_names_exactly_what_was_overridden(self) -> None:
        from elspeth.plugins.transforms.batch_stats import BatchStats

        transform = BatchStats(
            {
                "schema": {"mode": "flexible", "fields": ["amount: float", "mean: float", "count: any"]},
                "value_field": "amount",
            }
        )

        # 'amount' is consumed, so untouched; 'mean'/'count' are created and were
        # declared required, so the framework overrode the author there.
        assert transform.demoted_input_fields == frozenset({"mean", "count"})

    def test_nothing_is_reported_when_the_author_and_framework_agree(self) -> None:
        from elspeth.plugins.transforms.batch_stats import BatchStats

        transform = BatchStats({"schema": {"mode": "observed"}, "value_field": "amount"})

        assert transform.demoted_input_fields == frozenset()


class TestSelfCreatedFieldsAreOptionalOnInput:
    """Registry-wide invariant plus the representative plugins from the sweep."""

    @pytest.mark.parametrize("mode", ["flexible", "fixed"])
    def test_no_registered_transform_requires_its_own_output_fields_on_input(self, mode: str) -> None:
        """Whole-registry invariant: declaring own outputs never traps the input contract."""
        manager = PluginManager()
        manager.register_builtin_plugins()

        trapped: dict[str, list[str]] = {}
        checked = 0
        for cls in _registered_transform_classes(manager):
            probe_config = cls.probe_config
            try:
                base = probe_config()
            except NotImplementedError:
                continue
            try:
                instance = cls(base)
            except Exception:  # probe config unusable, not this invariant's concern
                continue

            # Gate on the SAME quantity the mechanism reads. input_schema demotes
            # self_created_input_fields, not declared_output_fields, and the two
            # differ deliberately: value_transform keeps declared_output_fields
            # EMPTY so the executor's collision check stays off (value_transform.py),
            # while overriding self_created_input_fields precisely because of that.
            # Gating on declared_output_fields skipped value_transform entirely —
            # the plugin this whole design was shaped around, left unguarded by its
            # own invariant. An invariant whose scope is computed from a different
            # quantity than the mechanism reads is blind exactly where they diverge,
            # and reads as whole-registry while it is not.
            # ...but do NOT read the scope from that property alone, or emptying it
            # makes the plugin VANISH from its own invariant instead of failing it.
            # _output_schema_config is derived independently (for value_transform,
            # from the operation targets), so it still names the created fields when
            # self_created_input_fields is wrong — which is what the gate must survive.
            output_contract = instance._output_schema_config
            independent = set(output_contract.guaranteed_fields or ()) if output_contract is not None else set()
            outputs = set(instance.self_created_input_fields or frozenset()) | independent
            if not outputs:
                continue

            declaring = copy.deepcopy(base)
            schema_block: dict[str, Any] = dict(declaring.get("schema") or {})
            schema_block["mode"] = mode
            schema_block["fields"] = [f"{name}: any" for name in sorted(outputs)]
            declaring["schema"] = schema_block
            try:
                declared = cls(declaring)
            except Exception:  # a plugin-local guard rejecting is a separate contract
                continue

            checked += 1
            collisions = sorted(outputs & _required_input_fields(declared))
            if collisions:
                trapped[cls.name] = collisions
                continue

            # Not just required-ness: a row carrying NONE of the created fields
            # must actually validate. Under mode:fixed this also proves the
            # demoted fields were kept in the model rather than dropped, since
            # extra="forbid" would otherwise reject a row that supplies one.
            declared.input_schema.model_validate({}, strict=True)
            declared.input_schema.model_validate(dict.fromkeys(sorted(outputs), "any-value"), strict=True)

        assert checked > 0, "no transform was exercised — the probe shape changed"
        assert trapped == {}, (
            f"transforms requiring their own created fields on input: {trapped}. "
            f"Fix at the plugin: if the option naming one of these is an OUTPUT knob, add it to "
            f"output_naming_config_keys; if the field is created but not in declared_output_fields, "
            f"override self_created_input_fields; if it is genuinely read, it belongs in "
            f"required_fields or required_input_fields instead."
        )

    def test_field_mapper_target_is_optional_on_input(self) -> None:
        """field_mapper renames into a target; requiring the target on input is a trap."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": {"mode": "flexible", "fields": ["source_id: str", "target_id: str"]},
                "mapping": {"source_id": "target_id"},
                "strict": True,
            }
        )

        assert "target_id" in transform.declared_output_fields
        assert "target_id" not in _required_input_fields(transform)
        transform.input_schema.model_validate({"source_id": "abc"}, strict=True)

    def test_every_output_classified_option_really_names_a_written_column(self) -> None:
        """Guard the OVER-declare direction, which loses data silently.

        Under-declaring an output knob leaves a created field required — the
        original bug, and the two other sweeps catch it. OVER-declaring, i.e.
        listing a knob that actually names an INPUT column, silently strips that
        column's requirement, and every other check here SKIPS declared keys, so
        they only prove the classifier is self-consistent, not that it is right.

        Checked via the consequence, which is observable: point the knob at a
        sentinel column name and the sentinel must show up in what the plugin
        CREATES. An output knob names a column the plugin writes, so it must.
        A read column never will — which is what would catch, say,
        ``json_explode.array_field`` being mislisted.
        """
        manager = PluginManager()
        manager.register_builtin_plugins()

        misclassified: dict[str, str] = {}
        checked = 0
        for cls in _registered_transform_classes(manager):
            declared_keys = cls.output_naming_config_keys
            probe_config = cls.probe_config
            if not declared_keys:
                continue
            try:
                base = probe_config()
            except NotImplementedError:
                continue

            for key in sorted(declared_keys):
                sentinel = f"sentinel_output_{key}"
                candidate = copy.deepcopy(base)
                candidate[key] = sentinel
                try:
                    built = cls(candidate)
                except Exception:  # a plugin-local guard rejecting is a separate contract
                    continue
                checked += 1
                created = set(built.self_created_input_fields) | set(built.declared_output_fields)
                output_contract = built._output_schema_config
                created |= set(output_contract.guaranteed_fields or ()) if output_contract is not None else set()
                if sentinel not in created:
                    misclassified[f"{cls.name}.{key}"] = sentinel

        assert checked > 0, "no output-naming option was exercised — the declarations vanished"
        assert misclassified == {}, (
            f"options declared in output_naming_config_keys whose value never appears as a written "
            f"column: {misclassified}. Either the option actually names a column the plugin READS — "
            f"remove it from output_naming_config_keys, or its value silently loses its input "
            f"requirement — or the plugin does not report it as created."
        )

    def test_every_read_classified_column_option_reaches_consumed_input_fields(self) -> None:
        """The generic half of the classification contract.

        Nothing in an option's name, type or position separates a READ column
        from a WRITTEN one — ``batch_top_k.field`` and ``web_scrape.content_field``
        are syntactically identical — so the read/write split can only come from
        the plugin's own ``output_naming_config_keys`` declaration. Given that
        classification, this assertion IS fully generic: every option not
        declared as output-naming must have its value protected from demotion.
        """
        from elspeth.plugins.infrastructure.base import is_column_naming_config_option

        manager = PluginManager()
        manager.register_builtin_plugins()

        unprotected: dict[str, str] = {}
        checked = 0
        for cls in _registered_transform_classes(manager):
            probe_config = cls.probe_config
            try:
                base = probe_config()
                transform = cls(base)
            except Exception:  # unusable probe config is not this contract's concern
                continue
            consumed = transform.consumed_input_fields
            for key, value in base.items():
                if not is_column_naming_config_option(key) or not isinstance(value, str):
                    continue
                if key in cls.output_naming_config_keys:
                    continue
                checked += 1
                if value not in consumed:
                    unprotected[f"{cls.name}.{key}"] = value

        assert checked > 0, "no read-classified column option was exercised — probe shape changed"
        assert unprotected == {}, f"read columns missing from consumed_input_fields: {unprotected}"

    def test_no_registered_transform_demotes_a_column_its_own_config_consumes(self) -> None:
        """Registry-wide: a configured input column keeps its requirement.

        Points each plugin's column knob (``value_field``, ``field``,
        ``group_by``, ``*_field``) at one of the plugin's OWN output field names
        — the collision that makes a consume-and-create transform possible,
        since batch output names are generic. The column must then either stay
        required on input, or be rejected at construction; what it must never do
        is silently lose the requirement (elspeth-d6eeb3a71d round 2).
        """
        manager = PluginManager()
        manager.register_builtin_plugins()

        lost: dict[str, str] = {}
        checked = 0
        for cls in _registered_transform_classes(manager):
            probe_config = cls.probe_config
            try:
                base = probe_config()
                instance = cls(base)
            except Exception:  # unusable probe config, not this invariant's concern
                continue
            outputs = sorted(instance.declared_output_fields or frozenset())
            knobs = [
                k for k in base if (k == "group_by" or k == "field" or k.endswith("_field")) and k not in cls.output_naming_config_keys
            ]
            if not outputs or not knobs:
                continue

            for knob in knobs:
                if not isinstance(base.get(knob), str):
                    continue
                collide = outputs[0]
                candidate = copy.deepcopy(base)
                candidate[knob] = collide
                schema_block: dict[str, Any] = dict(candidate.get("schema") or {})
                schema_block["mode"] = "flexible"
                schema_block["fields"] = [f"{name}: any" for name in outputs]
                candidate["schema"] = schema_block
                try:
                    built = cls(candidate)
                except Exception:  # fail-closed rejection is an acceptable answer
                    continue
                checked += 1
                if collide not in _required_input_fields(built):
                    lost[f"{cls.name}.{knob}"] = collide

        assert checked > 0, "sweep exercised no plugin/knob pair — the probe shape changed"
        assert lost == {}, f"configured input columns silently demoted: {lost}"

    def test_field_mapper_target_is_optional_on_input_without_strict(self) -> None:
        """'Fields I may create' is wider than 'fields I guarantee'.

        Non-strict field_mapper only GUARANTEES a target when its source is
        itself guaranteed, so declared_output_fields is empty here — but the
        transform still creates the target whenever the source arrives, so
        requiring it on input is the same trap.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": {"mode": "flexible", "fields": ["source_id: str", "target_id: str"]},
                "mapping": {"source_id": "target_id"},
                "strict": False,
            }
        )

        assert transform.declared_output_fields == frozenset()
        assert "target_id" not in _required_input_fields(transform)
        transform.input_schema.model_validate({"source_id": "abc"}, strict=True)

    def test_batch_stats_stat_fields_are_optional_on_input(self) -> None:
        """batch_stats emits mean/count/sum; declaring them must not demand them."""
        from elspeth.plugins.transforms.batch_stats import BatchStats

        transform = BatchStats(
            {
                "schema": {
                    "mode": "flexible",
                    "fields": ["amount: float", "count: any", "sum: any", "batch_size: any"],
                },
                "value_field": "amount",
            }
        )

        assert {"count", "sum", "batch_size"} <= transform.declared_output_fields
        assert {"count", "sum", "batch_size"}.isdisjoint(_required_input_fields(transform))
        transform.input_schema.model_validate({"amount": 1.5}, strict=True)

    def test_batch_distribution_profile_conditional_fields_are_optional_on_input(self) -> None:
        """Same conditional-diagnostics gap as batch_stats, in its sibling.

        ``missing_indices`` / ``non_finite_indices`` are written into the result
        but excluded from declared_output_fields because they are not
        guaranteed, so the base default leaves them demanded on input.
        """
        from elspeth.plugins.transforms.batch_distribution_profile import BatchDistributionProfile

        transform = BatchDistributionProfile(
            {
                "schema": {"mode": "flexible", "fields": ["score: float", "missing_indices: any"]},
                "value_field": "score",
            }
        )

        assert "missing_indices" not in transform.declared_output_fields
        assert "missing_indices" not in _required_input_fields(transform)

    def test_batch_stats_conditionally_emitted_fields_are_optional_on_input(self) -> None:
        """Conditional diagnostics are emitted-sometimes, so they are never inputs.

        ``skipped_missing`` and friends are deliberately excluded from
        declared_output_fields (they are not GUARANTEED), but batch_stats does
        write them, so requiring them on input is the same trap.
        """
        from elspeth.plugins.transforms.batch_stats import BatchStats

        transform = BatchStats(
            {
                "schema": {"mode": "flexible", "fields": ["amount: float", "skipped_missing: any"]},
                "value_field": "amount",
            }
        )

        assert "skipped_missing" not in transform.declared_output_fields
        assert "skipped_missing" not in _required_input_fields(transform)

    def test_batch_stats_group_by_stays_required_on_input(self) -> None:
        """group_by is echoed on output but READ from input, so it must not demote.

        It lands in declared_output_fields (the aggregate carries it through),
        which makes the base-class default the wrong demotion set here.
        """
        from elspeth.plugins.transforms.batch_stats import BatchStats

        transform = BatchStats(
            {
                "schema": {"mode": "flexible", "fields": ["amount: float", "tier: str"]},
                "value_field": "amount",
                "group_by": "tier",
            }
        )

        assert "tier" in transform.declared_output_fields
        assert "tier" in _required_input_fields(transform)

    def test_batch_distribution_profile_group_by_stays_required_on_input(self) -> None:
        """Same shape as batch_stats: the grouping column is read, not created."""
        from elspeth.plugins.transforms.batch_distribution_profile import BatchDistributionProfile

        transform = BatchDistributionProfile(
            {
                "schema": {"mode": "flexible", "fields": ["amount: float", "tier: str"]},
                "value_field": "amount",
                "group_by": "tier",
            }
        )

        assert "tier" in transform.declared_output_fields
        assert "tier" in _required_input_fields(transform)

    def test_second_stage_aggregation_keeps_the_consumed_stat_field_required(self) -> None:
        """A field the transform both CONSUMES and EMITS keeps its input requirement.

        Batch output names are generic, so a second-stage aggregation can read an
        upstream ``mean`` while also emitting one. ``value_field`` names it as
        consumed, so demotion must not reach it — otherwise a genuine contract
        violation stops being caught at the transform boundary and surfaces
        deeper in, unaudited.

        The PRESENCE arm is the one at risk here; an earlier version of this test
        asserted only that the object built and that the TYPE arm survived, both
        of which stayed true while the requirement was silently lost.
        """
        from elspeth.plugins.transforms.batch_stats import BatchStats

        transform = BatchStats(
            {
                "schema": {"mode": "fixed", "fields": ["mean: float", "grp: str"]},
                "value_field": "mean",
                "group_by": "grp",
            }
        )

        assert "mean" in transform.declared_output_fields
        assert "mean" in _required_input_fields(transform)
        with pytest.raises(ValidationError, match="mean"):
            transform.input_schema.model_validate({"grp": "a"}, strict=True)
        transform.input_schema.model_validate({"grp": "a", "mean": 2.5}, strict=True)

    def test_a_field_named_in_required_fields_is_never_demoted(self) -> None:
        """required_fields is an explicit "I consume this" declaration and always wins.

        It is the general discrimination the base class has for
        consume-and-create plugins; 11 of the 12 batch transforms already route
        their configured input columns through it.

        ``mean`` is chosen so the ``required_fields`` limb of
        ``consumed_input_fields`` is the ONLY thing protecting it: it is
        self-created, and ``value_field`` points at ``amount``, so the
        config-named-column surface does not reach it either. An earlier version
        of this test used ``BatchTopK`` with ``field: count`` and NO
        ``required_fields`` key at all — ``count`` arrived by the config-named
        route, so the test passed unchanged with the limb deleted outright
        (elspeth-3790106260). The two asserts below therefore state the routes
        explicitly rather than trusting the name.
        """
        from elspeth.plugins.transforms.batch_stats import BatchStats

        transform = BatchStats(
            {
                "schema": {
                    "mode": "flexible",
                    "fields": ["amount: float", "mean: float"],
                    "required_fields": ["mean"],
                },
                "value_field": "amount",
            }
        )

        assert "mean" in transform.self_created_input_fields
        assert "mean" not in transform._config_named_input_columns()
        assert "mean" not in transform.declared_input_fields
        assert "mean" in _required_input_fields(transform)

    def test_required_fields_survives_on_a_transform_that_builds_schemas_indirectly(self) -> None:
        """The limb only protects a field if the plugin actually CAPTURED its schema config.

        ``consumed_input_fields`` reads ``self._schema_config``. Ten registered
        transforms — ``line_explode`` among them — passed their validated
        ``schema_config`` to the schema factory but never stored it, so the limb
        read the ``None`` class default and contributed nothing: a field named in
        ``required_fields`` was demoted at runtime while
        ``get_raw_node_required_fields`` still enforced it at build time
        (elspeth-3790106260). Capture is now central, in
        ``_initialize_declared_input_fields``.

        ``line_explode`` is the measured case: ``source_field`` names ``body``,
        so ``line`` is reachable only through ``required_fields``.
        """
        from elspeth.contracts.schema import get_raw_node_required_fields
        from elspeth.plugins.transforms.line_explode import LineExplode

        options = {
            "source_field": "body",
            "output_field": "line",
            "schema": {
                "mode": "fixed",
                "fields": ["body: str", "line: str"],
                "required_fields": ["line"],
            },
        }
        transform = LineExplode(options)

        assert transform._schema_config is not None
        assert "line" in transform.self_created_input_fields
        assert "line" not in transform._config_named_input_columns()
        # The build-time contract and the runtime contract must name the same field.
        assert get_raw_node_required_fields(options, owner="line_explode") == frozenset({"line"})
        assert "line" in _required_input_fields(transform)
        assert "line" not in transform.demoted_input_fields

    def test_declared_field_not_created_by_the_transform_stays_required(self) -> None:
        """Demotion is scoped to self-created fields; genuine inputs keep their contract."""
        from elspeth.plugins.transforms.batch_stats import BatchStats

        transform = BatchStats(
            {
                "schema": {"mode": "flexible", "fields": ["amount: float", "count: any"]},
                "value_field": "amount",
            }
        )

        assert "amount" in _required_input_fields(transform)
