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

WHAT THIS DOES AND DOES NOT CATCH — measured, so the next author inherits the
boundary rather than rediscovering it:

    aliased output + creates fields (fixed)      CAUGHT (both codes)
    aliased output + creates fields (observed)   CAUGHT (aliasing only)
    separate closed output omitting created      CAUGHT
    separate closed output naming created        clean, correctly
    aliased output but creates nothing           clean, correctly — shape-preserving
    separate OBSERVED output omitting created    clean, DELIBERATELY: extra='allow'
                                                 admits the field anyway, so nothing
                                                 is wrong to report

Known limits, none of them defects of this file:
  * Fields a transform creates only at RUNTIME, never declared statically, are
    invisible here — ``value_transform``'s dynamic-key case is the example.
  * Each plugin is exercised through ONE ``probe_config``. For plugins whose
    created set is config-dependent this is a sample, not an exhaustive sweep;
    it is still roster-complete over plugins.
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
    """Every column the transform itself writes into a row.

    Exactly the two sources that state what the PLUGIN creates, because they
    diverge deliberately: ``value_transform`` keeps ``declared_output_fields``
    EMPTY so the executor's collision check stays off, and overrides
    ``self_created_input_fields`` instead — so that property is its ONLY source.

    Measured by substitution over the live roster, because the direction is easy
    to state backwards: dropping ``self_created_input_fields`` loses
    value_transform (24 -> 23); dropping ``declared_output_fields`` loses no
    plugin today (24 -> 24). Both stay anyway — the second is the surface the
    executor's collision check reads, so a plugin declaring only there must not
    become invisible here the moment one appears.

    The union is also load-bearing against a fix already queued elsewhere.
    elspeth-dea96e0830 proposes narrowing batch-aware reductive transforms'
    ``self_created_input_fields`` to the empty set. All 13 batch-aware
    transforms carry a non-empty ``declared_output_fields``, so reading the
    union keeps every one of them in this sweep when that lands; reading
    ``self_created_input_fields`` alone would silently drop 13 transforms and
    trip the liveness floor for a reason unrelated to this invariant.

    ``_output_schema_config.guaranteed_fields`` is deliberately NOT a third
    source. It is built as ``schema_config.guaranteed_fields |
    declared_output_fields`` (base.py ``_build_output_schema_config``), so it
    folds in the AUTHOR's pass-through guarantees — fields that arrive on the
    row and are merely promised onward, not manufactured. Including it made
    this predicate disagree with the framework's own guard: a shape-preserving
    ``passthrough`` declaring ``guaranteed_fields: [foo]`` creates nothing, yet
    was reported as aliasing while creating ``foo``, and the advice attached to
    that report would have pushed a correct closed contract into observed mode.
    It also bought nothing: across all field-creating transforms in the roster
    it adds zero fields and drops zero plugins from the sweep.
    """
    return frozenset(set(transform.self_created_input_fields) | set(transform.declared_output_fields))


def _output_forbids_extras(transform: BaseTransform) -> bool:
    """Whether the output model rejects a field it does not name."""
    output: type[PluginSchema] | None = getattr(transform, "output_schema", None)
    return output is not None and output.model_config.get("extra") == "forbid"


def _consumed_identifier_columns(transform: BaseTransform) -> frozenset[str]:
    """Consumed input columns a fixed-mode re-declaration must declare.

    Since elspeth-d3958d90f5, construction rejects a ``mode: fixed`` schema
    that omits an AUTHORED consumed column — the configured read could never
    succeed — and every probe_config authors its column options. A candidate
    omitting them would only measure that guard, not the output-contract
    invariant these arms exist for. The validated surface read here also
    includes option DEFAULTS (a superset of what the guard checks); declaring
    a defaulted column is harmless for these candidates. Dotted and other
    non-identifier spellings are exempt from the guard and cannot be schema
    fields, so they are excluded here too.
    """
    consumed = set(transform.declared_input_fields) | set(transform._config_named_input_columns())
    return frozenset(name for name in consumed if name.isidentifier())


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
        unbuildable: dict[str, str] = {}
        checked = 0
        for cls in _roster():
            probe_config = getattr(cls, "probe_config", None)
            if probe_config is None:
                continue
            try:
                instance = cls(probe_config())
            except Exception as exc:
                # Recorded, never swallowed — see the assertion below.
                unbuildable[_label(cls)] = type(exc).__name__
                continue
            if not _created_fields(instance):
                continue
            checked += 1
            violation = _output_contract_violations(instance).get(_ALIASED)
            if violation is not None:
                aliased[_label(cls)] = violation

        # LIVENESS, asserted two ways because a count alone is only a proxy.
        #
        # First, exactly: every registered transform must BUILD from its own
        # probe_config. Silently skipping an unbuildable plugin is how this
        # whole arm goes quietly blind — a genuinely defective transform whose
        # probe_config happens to raise would otherwise be excused by the very
        # sweep meant to catch it. Zero fail today, so this needs no allowlist;
        # if a legitimate exemption ever appears, name it here rather than
        # widening the except.
        assert unbuildable == {}, (
            f"transforms that could not be built from their own probe_config: {unbuildable}. "
            f"Fix the probe_config or exempt it explicitly — an unbuildable transform is "
            f"invisible to this invariant."
        )
        # Second, as a floor: the above cannot see a transform that was
        # deregistered rather than broken. 24 of 32 registered transforms create
        # fields; the 8 that do not are the four guardrail transforms plus
        # keyword_filter, passthrough, truncate and type_coerce, which write in
        # place.
        assert checked >= 24, f"only {checked} field-creating transforms were exercised, expected at least 24 — the roster shrank"
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
        unbuildable: dict[str, str] = {}
        rebuild_rejected: set[str] = set()
        forbid_checked = 0
        for cls in _roster():
            probe_config = getattr(cls, "probe_config", None)
            if probe_config is None:
                continue
            try:
                base = probe_config()
                instance = cls(base)
            except Exception as exc:
                # Recorded, never swallowed — same discipline as the arm above.
                unbuildable[_label(cls)] = type(exc).__name__
                continue
            created = _created_fields(instance)
            if not created:
                continue

            candidate = copy.deepcopy(base)
            schema_block: dict[str, Any] = dict(candidate.get("schema") or {})
            schema_block["mode"] = "fixed"
            # Consumed columns must be declared or the elspeth-d3958d90f5
            # construction guard rejects the candidate before this arm's
            # invariant is ever measured.
            schema_block["fields"] = sorted(
                set(schema_block.get("fields") or ()) | {f"{name}: any" for name in created | _consumed_identifier_columns(instance)}
            )
            candidate["schema"] = schema_block
            try:
                built = cls(candidate)
            except Exception:
                # A plugin-local guard rejecting this shape is a SEPARATE contract
                # and an acceptable answer — batch_outlier_annotator and
                # batch_replicate refuse a schema that collides with the fields
                # they overwrite. json_explode / line_explode used to land here
                # by refusing a schema that omitted their source field; once the
                # candidates began declaring consumed columns
                # (elspeth-d3958d90f5) that guard is satisfied and both moved
                # INTO this arm's coverage. Recorded and pinned below, because a
                # count stated only in prose is exactly how an arm loses
                # coverage without anyone noticing.
                rebuild_rejected.add(_label(cls))
                continue
            if not _output_forbids_extras(built):
                continue

            forbid_checked += 1
            violation = _output_contract_violations(built).get(_OMITS)
            if violation is not None:
                omitting[_label(cls)] = violation

        assert unbuildable == {}, (
            f"transforms that could not be built from their own probe_config: {unbuildable}. "
            f"An unbuildable transform is invisible to this arm."
        )
        # The deliberate fail-closed rejections are PINNED, not counted. A count
        # would let one plugin stop guarding while another started, silently
        # swapping coverage; naming the set makes either change fail loudly.
        assert rebuild_rejected == {"batch_outlier_annotator", "batch_replicate"}, (
            f"the set of transforms rejecting the fixed-mode re-declaration changed: "
            f"{sorted(rebuild_rejected)}. Each is a deliberate plugin-local guard; if one "
            f"stopped rejecting it now needs to satisfy this arm instead."
        )
        # A FLOOR, not "> 0" — this arm reaches exactly four transforms today, and
        # "> 0" would let three of them silently drop out while staying green.
        assert forbid_checked >= 4, (
            f"only {forbid_checked} transforms produced a closed (extra='forbid') output "
            f"contract, expected at least 4 — either the fixed-mode re-declaration stopped "
            f"reaching the plugins that build a fixed output schema, or they moved to observed."
        )
        assert omitting == {}, (
            f"transforms whose closed output contract omits fields they create: {omitting}. "
            f"The transform's own emitted row would fail extra_forbidden against its own "
            f"output schema."
        )

    def test_no_registered_transform_publishes_an_output_config_its_own_parser_rejects(self) -> None:
        """Invariant arm (elspeth-97487736ca): guaranteed fields must be DECLARED.

        The omission arm above unions each plugin's created fields into
        ``schema.fields`` before rebuilding, so it constructs the very fix for
        the shape it checks and can never reconstruct guaranteed-but-not-declared
        — the state where ``_build_output_schema_config`` (or a plugin-local
        equivalent) merges created fields into ``guaranteed_fields`` while
        copying the authored ``fields`` through untouched. Under ``mode: fixed``
        the model built from that config is ``extra='forbid'`` over the authored
        fields alone, so the created field is simultaneously guaranteed on
        output and forbidden by the output model.

        This arm re-declares each plugin's schema as ``mode: fixed`` over the
        probe's own fields WITHOUT the created fields, then uses
        ``SchemaConfig.from_dict(config.to_dict())`` as the oracle — the exact
        validator the direct ``SchemaConfig(...)`` construction bypasses.
        """
        parser_rejected: dict[str, str] = {}
        omitting: dict[str, str] = {}
        unbuildable: set[str] = set()
        round_tripped = 0
        for cls in _roster():
            probe_config = getattr(cls, "probe_config", None)
            if probe_config is None:
                continue
            base = probe_config()
            try:
                bare = cls(base)
            except Exception:
                unbuildable.add(_label(cls))
                continue
            candidate = copy.deepcopy(base)
            schema_block: dict[str, Any] = dict(candidate.get("schema") or {})
            schema_block["mode"] = "fixed"
            if not schema_block.get("fields"):
                # A fixed schema needs at least one authored field; two neutral
                # columns stand in for the user's own data. Created fields are
                # deliberately NOT added — that omission is the shape under test.
                # Consumed columns ARE added: since elspeth-d3958d90f5 the
                # construction guard rejects a fixed schema omitting them, and
                # they are orthogonal to the created-field shape measured here.
                schema_block["fields"] = ["probe_a: str", "probe_b: str"]
            schema_block["fields"] = sorted(set(schema_block["fields"]) | {f"{name}: any" for name in _consumed_identifier_columns(bare)})
            candidate["schema"] = schema_block
            try:
                built = cls(candidate)
            except Exception:
                # Plugin-local guards rejecting this shape are a separate,
                # acceptable contract — pinned exactly below.
                unbuildable.add(_label(cls))
                continue
            if not _created_fields(built):
                continue
            config = built._output_schema_config
            if config is None:
                # Shape-preserving plugins leave the config unset; the DAG
                # builder falls back to parsing the raw schema via from_dict,
                # which is valid by construction.
                continue
            round_tripped += 1
            try:
                SchemaConfig.from_dict(config.to_dict())
            except ValueError as exc:
                parser_rejected[_label(cls)] = str(exc)
            violation = _output_contract_violations(built).get(_OMITS)
            if violation is not None:
                omitting[_label(cls)] = violation

        # Every field-creating transform builds under this candidate now that
        # consumed columns are declared — json_explode / line_explode's
        # source-field guards were satisfied by exactly that declaration.
        assert unbuildable == set(), (
            f"the set of transforms rejecting the fixed-mode probe changed: {sorted(unbuildable)}. "
            f"A newly-rejecting transform has left this arm's coverage; a newly-accepting one "
            f"must now satisfy it."
        )
        # A FLOOR: 20 field-creating transforms publish an explicit output
        # config under this candidate today.
        assert round_tripped >= 20, (
            f"only {round_tripped} output configs reached the from_dict oracle, expected at "
            f"least 20 — the roster shrank or plugins moved to observed/unset configs."
        )
        assert parser_rejected == {}, (
            f"output configs their own parser rejects: {parser_rejected}. "
            f"A guaranteed or required field is not declared in the schema — the transform "
            f"publishes a contract SchemaConfig.from_dict would refuse to build."
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

    def test_a_shape_preserving_transform_that_creates_nothing_is_reported_clean(self) -> None:
        """The control this file was missing, and the one that matters most.

        A shape-preserving transform legitimately shares ONE schema object
        between input and output — that aliasing is correct, not a defect, and
        the predicate must stay silent for it even when the author declares
        ``guaranteed_fields``. An earlier revision of ``_created_fields`` read
        ``_output_schema_config.guaranteed_fields`` as a third source; because
        the framework builds that as ``schema_config.guaranteed_fields |
        declared_output_fields``, the author's pass-through promise came back as
        a "created" field and ``passthrough`` was reported as aliasing while
        creating a field it never writes. The attached advice would have pushed
        a correct closed contract into observed mode.

        This asserts against the SHIPPED plugin rather than a local stub, so the
        control cannot drift from the shape a real author writes.
        """
        from elspeth.plugins.transforms.passthrough import PassThrough

        transform = PassThrough({"schema": {"mode": "fixed", "fields": ["foo: str"], "guaranteed_fields": ["foo"]}})

        assert transform.declared_output_fields == frozenset()
        assert transform.self_created_input_fields == frozenset()
        assert _created_fields(transform) == frozenset(), (
            "an author's pass-through guarantee is not a created field — see the "
            "_created_fields docstring for why guaranteed_fields is not a source"
        )
        assert _output_contract_violations(transform) == {}

    def test_a_separate_observed_output_omitting_a_created_field_is_reported_clean(self) -> None:
        """The last documented behaviour that was prose rather than a gate.

        An OPEN output contract (``extra='allow'``) that does not name a created
        field is CORRECT: the emitted row carrying that field validates anyway,
        so there is nothing to report. Only a CLOSED contract makes the omission
        row-rejecting, which is the sibling control above.

        Pinned because this row looks like an oversight and is the one a future
        author is most likely to "fix" — tightening the predicate to flag it
        would break no other test and would convert a correct open contract into
        a reported defect, the same class of false positive as the ``passthrough``
        one, arriving by a different door.
        """

        class _SeparateButOpen(BaseTransform):
            name = "output_contract_control_separate_open"
            determinism = Determinism.DETERMINISTIC
            declared_output_fields = frozenset({"added"})

            def __init__(self, config: dict[str, Any]) -> None:
                super().__init__(config)
                from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

                declared = SchemaConfig.from_dict({"mode": "observed"})
                self.input_schema = create_schema_from_config(declared, "OpenControlIn", allow_coercion=False)
                self.output_schema = create_schema_from_config(declared, "OpenControlOut", allow_coercion=False)

        transform = _SeparateButOpen({"schema": {"mode": "observed"}})

        # Genuinely separate models, and the output tolerates extras — so the
        # predicate must decline BOTH codes, for two independent reasons.
        assert transform.output_schema is not transform._declared_input_schema
        assert not _output_forbids_extras(transform)
        assert _output_contract_violations(transform) == {}
