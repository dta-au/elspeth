"""A transform's OUTPUT contract must admit the fields the transform creates.

``BaseTransform._create_schemas`` returns ``output_schema is input_schema`` unless
the caller passes ``adds_fields=True``. A transform that declares created fields
but omits that keyword therefore publishes an output contract that does not name
its own outputs — and under ``mode: fixed`` that contract has ``extra="forbid"``,
so the transform's own emitted field is rejected against its own output schema.

No shipped transform is in that shape today (every one with a non-empty created
set builds its output schema separately), so this is an authoring trap for future
plugins rather than a live defect. It is guarded here, at the registry, rather
than at the construction site, because the construction site does not see every
transform: ``blob_csv_expand``, ``blob_fetch``, ``web_scrape``, ``llm`` and
``azure/document_intelligence`` never call ``_create_schemas`` at all, and ``llm``
additionally populates ``declared_output_fields`` only AFTER building its input
schema — so a guard inside ``_create_schemas`` is defeated by both path and
ordering. A sweep over the live ``PluginManager`` roster is blind to both.

The landed read-time guard in ``BaseTransform.input_schema`` catches the aliasing
case only when the demote set is non-empty, and demote is intersected with the
declared model's own fields — an ``observed``-mode schema declares none, so that
guard is silent for exactly the probe shape every plugin ships.
"""

from __future__ import annotations

import copy
from typing import Any

from elspeth.contracts import Determinism
from elspeth.contracts.schema import SchemaConfig
from elspeth.plugins.infrastructure.base import BaseTransform, PluginSchema
from elspeth.plugins.infrastructure.manager import PluginManager

_ALIASED = "aliased_output_schema"
_OMITS = "output_contract_omits_created_fields"


def _created_fields(transform: BaseTransform) -> frozenset[str]:
    """Every column the transform may write into a row.

    Three sources, unioned, because they diverge deliberately and no single one
    is the whole answer: ``value_transform`` keeps ``declared_output_fields``
    EMPTY so the executor's collision check stays off while overriding
    ``self_created_input_fields``, and ``_output_schema_config`` is derived
    independently — so it still names the created fields when the other two are
    wrong. Reading any one alone makes some plugin vanish from its own invariant.
    """
    contract = transform._output_schema_config
    guaranteed = set(contract.guaranteed_fields or ()) if contract is not None else set()
    return frozenset(set(transform.self_created_input_fields) | set(transform.declared_output_fields) | guaranteed)


def _output_forbids_extras(transform: BaseTransform) -> bool:
    """Whether the output model rejects a field it does not name."""
    output: type[PluginSchema] | None = getattr(transform, "output_schema", None)
    return output is not None and output.model_config.get("extra") == "forbid"


def _output_contract_violations(transform: BaseTransform) -> dict[str, str]:
    """Map violation code -> detail for one built transform. Empty means clean.

    ONE predicate, shared by both registry sweeps and by the negative controls
    below, so a control cannot pass against a re-implementation of the check
    while the sweep runs something subtly different.
    """
    created = _created_fields(transform)
    if not created:
        return {}

    violations: dict[str, str] = {}
    output: type[PluginSchema] | None = getattr(transform, "output_schema", None)
    if output is None:
        return {_ALIASED: "no output_schema was assigned"}

    if output is transform._declared_input_schema:
        # Input and output are literally the same model object, which is the
        # signature of a SHAPE-PRESERVING transform. A transform that creates
        # fields is not shape-preserving, so this is always wrong — regardless of
        # whether the shared model happens to tolerate extras today.
        violations[_ALIASED] = f"output_schema is the input model while creating {sorted(created)}"

    if _output_forbids_extras(transform):
        missing = sorted(created - set(output.model_fields))
        if missing:
            violations[_OMITS] = f"extra='forbid' output contract omits created field(s) {missing}"
    return violations


def _roster() -> list[type[BaseTransform]]:
    manager = PluginManager()
    manager.register_builtin_plugins()
    return list(manager.get_transforms())


def _label(cls: type[BaseTransform]) -> str:
    return getattr(cls, "name", cls.__name__)


class TestRegisteredTransformsPublishAnHonestOutputContract:
    """Two arms with different reach, so they carry SEPARATE liveness counters.

    The aliasing arm runs against each plugin's bare ``probe_config`` and reaches
    every transform that creates anything. The omission arm only bites once the
    output model forbids extras, which the observed-mode probes never do — it
    needs the fixed-mode re-declaration below, and even then only four shipped
    transforms reach it. A single shared counter would stay green off the first
    arm forever and let the second go vacuous unnoticed.
    """

    def test_no_registered_transform_aliases_its_output_schema_onto_its_input(self) -> None:
        """Whole-roster arm: creating fields and staying shape-preserving is a bug."""
        aliased: dict[str, str] = {}
        checked = 0
        for cls in _roster():
            probe_config = getattr(cls, "probe_config", None)
            if probe_config is None:
                continue
            try:
                instance = cls(probe_config())
            except Exception:  # unusable probe config is not this invariant's concern
                continue
            if not _created_fields(instance):
                continue
            checked += 1
            violation = _output_contract_violations(instance).get(_ALIASED)
            if violation is not None:
                aliased[_label(cls)] = violation

        assert checked > 0, "no field-creating transform was exercised — the probe shape changed"
        assert aliased == {}, (
            f"transforms sharing one schema object between input and output while creating "
            f"fields: {aliased}. Build the output schema separately — "
            f"_create_schemas(..., adds_fields=True) — so the created fields are not absent "
            f"from the transform's own output contract."
        )

    def test_no_registered_transform_forbids_the_fields_it_creates(self) -> None:
        """Consequence arm: a closed output contract must name every created field.

        Re-declares each plugin's schema as ``mode: fixed`` over the union of its
        probe fields and its created fields, because that is the only authored
        shape under which an output contract closes (``extra='forbid'``) and the
        omission becomes row-rejecting rather than merely undeclared.
        """
        omitting: dict[str, str] = {}
        forbid_checked = 0
        for cls in _roster():
            probe_config = getattr(cls, "probe_config", None)
            if probe_config is None:
                continue
            try:
                base = probe_config()
                instance = cls(base)
            except Exception:  # unusable probe config is not this invariant's concern
                continue
            created = _created_fields(instance)
            if not created:
                continue

            candidate = copy.deepcopy(base)
            schema_block: dict[str, Any] = dict(candidate.get("schema") or {})
            schema_block["mode"] = "fixed"
            schema_block["fields"] = sorted(set(schema_block.get("fields") or ()) | {f"{name}: any" for name in created})
            candidate["schema"] = schema_block
            try:
                built = cls(candidate)
            except Exception:
                # A plugin-local guard rejecting this shape is a SEPARATE contract
                # and an acceptable answer. Four transforms land here today —
                # batch_outlier_annotator and batch_replicate refuse a schema that
                # collides with the fields they overwrite, and json_explode /
                # line_explode refuse a non-normalized field name once the schema
                # declares an output contract. All four are deliberate fail-closed
                # guards, not artifacts of the re-declaration above.
                continue
            if not _output_forbids_extras(built):
                continue

            forbid_checked += 1
            violation = _output_contract_violations(built).get(_OMITS)
            if violation is not None:
                omitting[_label(cls)] = violation

        assert forbid_checked > 0, (
            "no transform produced a closed (extra='forbid') output contract — this arm is "
            "vacuous. Either the fixed-mode re-declaration above stopped reaching the "
            "plugins that build a fixed output schema, or they all moved to observed output."
        )
        assert omitting == {}, (
            f"transforms whose closed output contract omits fields they create: {omitting}. "
            f"The transform's own emitted row would fail extra_forbidden against its own "
            f"output schema."
        )


class TestTheSweepPredicateActuallyFires:
    """Negative controls: prove the shared predicate is live, not merely quiet.

    Both sweeps above pass on every shipped transform, so on their own they
    cannot distinguish "the roster is clean" from "the check is broken". These
    build the two defective shapes explicitly and assert the SAME predicate the
    sweeps call reports them.
    """

    @staticmethod
    def _adder(schema: dict[str, Any]) -> BaseTransform:
        class _Adder(BaseTransform):
            name = "output_contract_control_adder"
            determinism = Determinism.DETERMINISTIC
            declared_output_fields = frozenset({"added"})

            def __init__(self, config: dict[str, Any]) -> None:
                super().__init__(config)
                # The defect: adds_fields is left at its default False, so
                # output_schema comes back as the SAME object as input_schema.
                self.input_schema, self.output_schema = self._create_schemas(SchemaConfig.from_dict(schema), "Control")

        return _Adder({"schema": schema})

    def test_the_aliasing_shape_is_reported(self) -> None:
        """mode: fixed reproduces BOTH arms at once — this is the real trap."""
        violations = _output_contract_violations(self._adder({"mode": "fixed", "fields": ["kept: str"]}))

        assert _ALIASED in violations
        assert _OMITS in violations
        assert "added" in violations[_OMITS]

    def test_the_aliasing_shape_is_reported_even_when_extras_are_tolerated(self) -> None:
        """An observed-mode alias trips ONLY the aliasing arm.

        This is the case the landed read-time FrameworkBugError guard misses (its
        demote set is empty because an observed schema declares no fields), and it
        is why the aliasing arm cannot be folded into the omission arm.
        """
        violations = _output_contract_violations(self._adder({"mode": "observed"}))

        assert _ALIASED in violations
        assert _OMITS not in violations

    def test_a_separate_but_closed_output_contract_that_omits_a_created_field_is_reported(self) -> None:
        """The omission arm must fire on its own, without any aliasing.

        Identity is a proxy for the mechanism; the contract that actually matters
        is admissibility. A transform can build a genuinely separate output model
        and still close it over a field set that omits its own outputs.
        """

        class _SeparateButClosed(BaseTransform):
            name = "output_contract_control_separate"
            determinism = Determinism.DETERMINISTIC
            declared_output_fields = frozenset({"added"})

            def __init__(self, config: dict[str, Any]) -> None:
                super().__init__(config)
                from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

                declared = SchemaConfig.from_dict({"mode": "fixed", "fields": ["kept: str"]})
                self.input_schema = create_schema_from_config(declared, "ControlIn", allow_coercion=False)
                self.output_schema = create_schema_from_config(declared, "ControlOut", allow_coercion=False)

        transform = _SeparateButClosed({"schema": {"mode": "fixed", "fields": ["kept: str"]}})
        violations = _output_contract_violations(transform)

        assert _ALIASED not in violations
        assert _OMITS in violations
        assert "added" in violations[_OMITS]

    def test_a_compliant_transform_is_reported_clean(self) -> None:
        """The other direction: the predicate must not flag correct construction."""

        class _Compliant(BaseTransform):
            name = "output_contract_control_compliant"
            determinism = Determinism.DETERMINISTIC
            declared_output_fields = frozenset({"added"})

            def __init__(self, config: dict[str, Any]) -> None:
                super().__init__(config)
                self.input_schema, self.output_schema = self._create_schemas(
                    SchemaConfig.from_dict({"mode": "fixed", "fields": ["kept: str"]}),
                    "Control",
                    adds_fields=True,
                )

        assert _output_contract_violations(_Compliant({"schema": {"mode": "fixed", "fields": ["kept: str"]}})) == {}
