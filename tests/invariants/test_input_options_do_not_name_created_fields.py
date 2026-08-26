"""An input-naming config option must never resolve to a field the plugin creates.

Several transforms take an option that names the column they READ —
``web_scrape.url_field``, ``rag_retrieval.query_field``,
``aws_textract_document_analysis.key_field``,
``azure_document_intelligence.source_field`` — and then write their created
fields back onto the same row. Pointing one at the other makes the transform
consume its own output: it reads the column for a URL or an object key and
immediately overwrites it with the fetched content. Every such config
constructed cleanly, and ``compose`` returned ``is_valid`` (elspeth-09dc6407f1).

Nothing downstream closes it. ``TransformExecutor``'s collision check compares
``declared_output_fields`` against the input keys OF A ROW, so it cannot fire
until a row actually carries the column; and under ``mode: observed`` — the mode
every one of these plugins ships in its own ``probe_config`` and examples —
there is no declared field for DAG validation to carry. So the check has to be
at construction, which is what this gate asserts.

SCOPE, and why it is not "every transform". The defect needs the created field
and the named column to land on THE SAME ROW: the transform reads the column,
then writes its own output over it. ELSPETH has exactly two declarations for
that — ``passes_through_input`` (every input field survives) and
``forwards_input_fields`` (every SUCCESS row carries its input's fields, minus
``removed_input_fields``) — so the roster derives from their UNION rather than
restating the condition by hand.

That is what puts the aggregate batch family outside the roster, and the
exclusion is now the CONSEQUENCE of the rule rather than a second rule. They
declare NEITHER flag: their created fields appear on a NEW summary row, so a
generic output name colliding with an input column is the DOCUMENTED LEGITIMATE
SHAPE ``BaseTransform.consumed_input_fields`` describes — "a second-stage
aggregation reading an upstream ``mean`` while emitting its own".

That exclusion was TRIAGED, not assumed (2026-08-26). All 293 (option, target)
pairs across the ten accepting aggregates were measured three ways: the emitted
row REPLACES the input (``batch_stats`` fed ``{mean, id, marker}`` returns only
``{batch_size, count, mean, sum}``, so no row ever carries both the read column
and the created one); the pairs that look overwritten are PROVENANCE ECHOES
(``batch_threshold_summary`` emits ``value_field: "value_field"`` — the column
NAME it read, not a destroyed value); and the ratified corpus SHIPS the shape,
``examples/document_review_panel/settings.yaml`` running ``batch_stats`` with
``value_field: mean`` as a genuine second-stage read. Guarding this family
would reject four working example pipelines. Verdict: zero defects, and the
existing partial guards are already drawn on the right line —
``batch_stats``/``batch_distribution_profile`` guard ``group_by`` (whose value
BECOMES an output column) and deliberately not ``value_field`` (a pure read).

``batch_outlier_annotator`` is the counter-example that makes the union
load-bearing: it is the ONE aggregate declaring ``forwards_input_fields =
True`` (its emitted row is ``**entry.row.to_dict()`` plus annotations) and the
ONE carrying a full ``value_field`` collision guard. The correlation is causal,
not coincidental — where created fields really do land on the arriving row, the
defect is real and its author already closed it.

STATED PLAINLY, because the roster reads as more assurance than it carries:
``passes_through_input`` is a proxy for INTENT, not the mechanism. The
demotion arithmetic (``self_created_input_fields - consumed_input_fields``,
applied to the INPUT schema) does not consult it, so ``batch_stats`` with
``value_field: "mean"`` leaves ``mean`` required on input exactly the way
``batch_replicate`` left ``copy_index`` required — and an arriving row without
it is rejected either way. The difference is that a second-stage aggregation
MEANS to read an upstream ``mean``, and a row without one is then a genuine
contract violation rather than the transform demanding its own output.
``batch_stats`` therefore still constructs, whether that shape survives the
executor's collision check on the batch dispatch path remains an OPEN QUESTION,
and this gate does not adjudicate it. Widening the roster to the aggregate
batch family stays a separate decision, and it was TAKEN on 2026-08-26: the
293 pairs were triaged to zero defects and the family stays out, on the
evidence recorded above rather than on this gate deferring them.

``batch_replicate`` is the one batch-aware member in the roster on the
``passes_through_input`` limb, and it is in for the same derived reason every
other member is: it declares that flag because each emitted copy deep-copies
its originating input row before ``copy_index`` is written onto it. The predicate
this replaced — "the config class overrides ``TransformDataConfig.
declared_input_fields``" — is a naming convention, not the mechanism, and it
excluded ``batch_replicate`` while ``copies_field: "copy_index"`` was accepted
unguarded.

``line_explode`` and ``json_explode`` sit on the ``forwards_input_fields``
limb, so the union pulls them IN where the old predicate excluded them. Both
already reject the shape natively (``output_field and source_field must
differ``), which is why this widening changes no verdict — but they are now
covered on mechanism rather than by an exclusion that happened to be safe.

The roster is DISCOVERED from a live ``PluginManager``, so a new plugin that
forwards its input and names a column is swept without being added here.
Anti-vacuity is enforced four ways: the roster must be non-empty; it must still
contain every plugin the defect was measured on; every member must expose at
least one input-naming option AND create at least one field (a member missing
either would pass by having nothing to test); and each member must still
construct under its own ``probe_config``, so a guard that rejects legal configs
fails here rather than in production.

EVERY created field is swept against EVERY input-naming option, not one
representative pair. Testing only ``sorted(created)[0]`` let a guard that
covers the alphabetically first created name shield every name behind it.
Several roster members reject through NATIVE validators enumerating specific
emitted names (``pdf_rasterize._reject_field_name_collisions``,
``blob_text_expand``'s ``output_field``/``index_field`` checks), so that
shape is one edit away rather than hypothetical: narrowing
``pdf_rasterize``'s collision check to ``document_id_field`` alone and
dropping its shared-helper call leaves seven of its eight created fields
accepting ``blob_ref_field``, and the single-target sweep stays GREEN
throughout. It is measured, not argued — the widened sweep reports all seven.

Rejection is asserted BEHAVIOURALLY, not by mechanism. ``blob_fetch`` and
``blob_csv_expand`` were never affected because they carry native collision
validators, and they pass this gate through those validators rather than
through the shared helper the other four call. A future plugin may close the
hole a third way; the gate cares that the config is refused and that the error
names the option to repoint, which is the actionable half.

KNOWN LIMITS, so nobody reads more assurance into this than it carries:

* Options are discovered from each plugin's own ``probe_config()``, and only
  options whose DECLARED ANNOTATION admits ``str`` are mutated. LIST-valued
  ones are skipped — ``azure_document_intelligence.query_fields`` matches the
  ``*_fields`` naming rule but holds Azure-side query names, not row columns,
  so mutating it would assert a contract that does not exist. ``llm``'s
  ``image_inputs`` folds ``field``/``format_field`` into
  ``declared_input_fields`` (a real column-naming surface — see
  ``LLMConfig.declared_input_fields``) but those names live inside
  ``list[ImageInputConfig]`` entries, not a top-level scalar option, so the
  roster excludes ``llm`` via ``_has_a_testable_naming_surface`` rather than
  failing the anti-vacuity assertion on a plugin this mechanism cannot probe.
  ``image_inputs[].field`` naming a created field (e.g. ``response_field``) is
  therefore NOT swept by this gate.

  The annotation is what decides that, NOT the probe's value. Classifying on
  the value admitted any option that merely happened to be ``None`` in the
  probe: ``llm.output_fields`` is ``list[OutputFieldConfig] | None``, defaults
  to ``None``, and was swept as if it were a scalar — the ``str`` mutation
  could not even be constructed, so it landed in ``uncontrolled`` and this gate
  failed on a plugin its own stated scope excludes.
* Mutating a ``None``-valued option can collide with an interlock rather than
  with the created-field rule — ``aws_textract_document_analysis`` makes
  ``bucket`` and ``bucket_field`` mutually exclusive, and a future option in
  that shape would be rejected for the wrong reason. The control mutation below
  is what keeps that honest: an option that cannot first be repointed at an
  ordinary arriving column is reported rather than counted as covered.
"""

from __future__ import annotations

import copy
import types
import typing
from typing import Any

import pytest

from elspeth.plugins.infrastructure.base import BaseTransform, is_column_naming_config_option
from elspeth.plugins.infrastructure.manager import PluginManager

# Plugins measured to put a created field on the arriving row. Two were already
# closed natively when the defect was found (``blob_fetch``,
# ``blob_csv_expand``); four more were the live repros. ``batch_replicate`` is
# the fifth repro, measured when the roster stopped being keyed on the config
# class overriding ``declared_input_fields``: ``copies_field: "copy_index"``
# constructed cleanly and left ``copy_index`` required on input. The last three
# joined when the predicate became the ``passes_through_input |
# forwards_input_fields`` union — they reject natively and always did, so they
# are anchors for the ROSTER DERIVATION rather than repros: if a refactor drops
# the forwarding limb, these three fall out silently and the union's whole
# point is gone. If a refactor drops any member, coverage has been lost.
_MEASURED_DEFECT_ANCHORS = frozenset(
    {
        "aws_textract_document_analysis",
        "azure_document_intelligence",
        "batch_outlier_annotator",
        "batch_replicate",
        "blob_csv_expand",
        "blob_fetch",
        "json_explode",
        "line_explode",
        "rag_retrieval",
        "web_scrape",
    }
)

# ADR-013's generic declaration surface, not a plugin's own locator option. It
# matches the ``*_fields`` naming rule but names required columns directly
# rather than choosing where the plugin reads, so mutating it would test the
# base contract rather than the plugin's.
_GENERIC_DECLARATION_OPTIONS = frozenset({"required_input_fields"})

# An ordinary arriving column name, used to prove an option can be repointed at
# all before its created-field value is expected to be refused.
_CONTROL_COLUMN = "arriving_column_under_test"


def _registered_transform_classes() -> tuple[type[BaseTransform], ...]:
    """Every built-in transform class, from a live registry."""
    manager = PluginManager()
    manager.register_builtin_plugins()
    return tuple(manager.get_transforms())


def _writes_created_fields_onto_the_arriving_row(probe: BaseTransform) -> bool:
    """Whether this plugin's created fields land on the row its config named a column of.

    Derived from the plugin's OWN declarations rather than restated. The defect
    is a transform reading a column and then overwriting it, which needs both
    on one row, and ELSPETH has exactly two declarations for "an emitted row
    carries its input's fields":

    * ``passes_through_input`` — all-or-nothing, "it demands that EVERY input
      field survive" (base.py).
    * ``forwards_input_fields`` — the EXTRAS direction, "every SUCCESS row this
      transform emits carries every field present on its input row, EXCEPT the
      names in ``removed_input_fields``". It exists precisely because the
      all-or-nothing form is too strong for a transform that drops whole ROWS
      while still forwarding every field of the ones it keeps (elspeth-15c72686f2).

    Either one puts the created field on the arriving row, so the union is the
    mechanism. A transform declaring NEITHER emits a FRESH row and cannot
    overwrite the column it read, which makes a created name colliding with an
    input name the legitimate second-stage shape rather than this defect.

    ``batch_outlier_annotator`` is why the union is not merely tidier. It
    declares ``passes_through_input = False`` (it drops rows) and
    ``forwards_input_fields = True`` (its emitted row is
    ``**entry.row.to_dict()`` plus annotations) — and it is the ONE aggregate
    carrying a full ``value_field`` collision guard, which every sibling
    aggregate lacks and does not need. Under the ``passes_through_input``-only
    predicate this gate covered it only by luck: the plugin happened to guard
    already. Measured 2026-08-26, the union changes no verdict — it is
    robustness against a future aggregate that starts forwarding, not a fix.

    NOTE THE ARGUMENT TYPE. ``passes_through_input`` is a class attribute, but
    ``forwards_input_fields`` is per-INSTANCE — declarers compute it from
    config in ``__init__`` (base.py: "declarers set it in ``__init__``, not in
    the class body"), so ``BatchOutlierAnnotator.forwards_input_fields`` reads
    ``False`` off the CLASS and ``True`` off a constructed probe. This takes a
    probe for that reason; reading either off the class would silently restore
    the old blind spot.

    The predicate this replaced asked whether the CONFIG CLASS overrode
    ``TransformDataConfig.declared_input_fields``. That is a naming convention
    the roster members happen to share, not the mechanism, and it silently
    excluded ``batch_replicate`` — which forwards its input, creates
    ``copy_index``, and accepted ``copies_field: "copy_index"``.
    """
    return probe.passes_through_input or probe.forwards_input_fields


def _in_scope(cls: type[BaseTransform]) -> bool:
    """Roster membership: one constructed probe answers both halves.

    Constructing is what makes ``forwards_input_fields`` readable at all (see
    ``_writes_created_fields_onto_the_arriving_row``), so the two predicates
    share the probe rather than each building their own.
    """
    probe = cls(cls.probe_config())
    return _writes_created_fields_onto_the_arriving_row(probe) and _has_a_testable_naming_surface(cls, probe)


def _has_a_testable_naming_surface(cls: type[BaseTransform], probe: BaseTransform) -> bool:
    """Whether this plugin exposes a scalar naming option AND creates a field to aim it at.

    ``_writes_created_fields_onto_the_arriving_row`` is a proxy for "this
    plugin can read a column and overwrite it", and for every anchor plugin it
    coincides with having something to test. They come apart two ways.

    NO SCALAR OPTION, ``llm``: ``LLMConfig.declared_input_fields`` folds in
    ``image_inputs[].field``/``format_field`` (base.py, "``required_input_
    fields`` plus every ``image_inputs`` field/format_field"), but those names
    live inside a ``list[ImageInputConfig]``, not a top-level ``str | None``
    field, so ``_input_naming_options`` — which only mutates a top-level dict
    key (see the control-mutation step below) — finds nothing to test. Per
    KNOWN LIMITS above, list-valued naming surfaces are out of this gate's
    scope; this predicate makes the roster match that stated scope instead of
    failing the anti-vacuity assertion on a plugin the mechanism cannot
    exercise. ``image_inputs[].field`` is therefore NOT swept by this gate — a
    real coverage gap, left for a future gate widening rather than invented
    here.

    NOTHING CREATED, the content-safety and pass-through family
    (``aws_bedrock_prompt_shield``, ``keyword_filter``, ``truncate``,
    ``type_coerce``, ``passthrough``): they forward their input and create no
    field under ``probe_config``, so the mutation below has no target. An
    option cannot name a created field where there is no created field, and
    ``self_created_input_fields`` is the same authority the guard itself reads.
    """
    return bool(_input_naming_options(cls, probe)) and bool(probe.self_created_input_fields)


def _admits_a_column_name(annotation: object) -> bool:
    """Whether a config field's DECLARED type can hold a single column name.

    Classified from the annotation, not from the probe's value: an option that
    merely happens to be ``None`` in ``probe_config`` is not thereby a scalar,
    and treating it as one puts a list-valued option (``llm.output_fields``,
    ``list[OutputFieldConfig] | None``) into a sweep that can only mutate it
    into an unconstructable config.

    The question is whether the key CAN HOLD one column name, not whether it
    holds only that: ``keyword_filter.fields`` is ``str | list[str]`` and is
    legitimately mutable to a single name, so a union qualifies on any member
    that does.
    """
    if annotation is str:
        return True
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return any(_admits_a_column_name(arg) for arg in typing.get_args(annotation))
    return False


def _input_naming_options(cls: type[BaseTransform], probe: BaseTransform) -> dict[str, Any]:
    """Column-naming config options this plugin READS, with their probe values.

    Mirrors the read/write split ``BaseTransform.consumed_input_fields`` uses:
    naming alone cannot say which, so ``output_naming_config_keys`` is the
    plugin's own classification and everything else is treated as a read.
    """
    # Mirrors ``_config_named_input_columns``'s own source choice (base.py):
    # the VALIDATED config when one was captured, so an option the author
    # omitted still contributes its default, and the raw authored dict when
    # none was. ``json_explode`` is the single transform in that second state —
    # its config is a ``DataPluginConfig``, not a ``TransformDataConfig``, so
    # it cannot route through ``_initialize_declared_input_fields`` and
    # ``_validated_config`` keeps its ``None`` class default. Asserting a
    # validated config here would fail the gate on a plugin that REJECTS the
    # defect correctly, which is a gate defect rather than a plugin one.
    validated = probe._validated_config
    if validated is None:
        model_fields = type(probe).config_model.model_fields if type(probe).config_model is not None else {}
        # The raw dict omits defaulted keys, so read each option's value off
        # the model default rather than reporting a key the author never wrote
        # as absent.
        field_values = {name: probe.config.get(name, info.get_default()) for name, info in model_fields.items()}
    else:
        # ``dict(model)`` is pydantic's own field-value view: it yields the raw
        # (unserialised) value for every declared field by name, so the option
        # values are read from the model's data surface rather than probed off it.
        model_fields = type(validated).model_fields
        field_values = dict(validated)

    options: dict[str, Any] = {}
    for name, info in model_fields.items():
        if name in _GENERIC_DECLARATION_OPTIONS or name in cls.output_naming_config_keys:
            continue
        if not is_column_naming_config_option(name):
            continue
        if not _admits_a_column_name(info.annotation):
            continue
        options[name] = field_values[name]
    return options


class TestInputOptionsDoNotNameCreatedFields:
    def test_every_plugin_declaring_an_input_column_rejects_naming_a_created_field(self) -> None:
        roster = [cls for cls in _registered_transform_classes() if _in_scope(cls)]

        assert roster, "No transform declares an arriving column; the sweep would pass vacuously"
        discovered = {cls.name for cls in roster}
        assert discovered >= _MEASURED_DEFECT_ANCHORS, (
            f"Roster lost plugins the defect was measured on: {sorted(_MEASURED_DEFECT_ANCHORS - discovered)}"
        )

        unguarded: dict[str, str] = {}
        unnamed: dict[str, str] = {}
        uncontrolled: dict[str, str] = {}

        for cls in roster:
            base: dict[str, Any] = cls.probe_config()

            # The arm that must not regress: a guard that also rejects legal
            # configs is worse than the defect it closes.
            probe = cls(base)
            created = probe.self_created_input_fields
            assert created, f"{cls.name}: probe creates no fields, so the mutation below has no target"

            options = _input_naming_options(cls, probe)
            assert options, f"{cls.name}: declares an input column but exposes no input-naming option to mutate"

            for option in sorted(options):
                # CONTROL first. Repointing the option at an ordinary arriving
                # column must construct, so the rejections below are
                # attributable to the value being CREATED rather than to the
                # option having been touched at all. This is what lets the
                # assertion stay behavioural: blob_fetch and blob_csv_expand
                # reject through their own native validators, whose messages
                # share no wording with the shared helper, so matching on the
                # guard's phrasing would test the mechanism instead of the
                # contract.
                control = copy.deepcopy(base)
                control[option] = _CONTROL_COLUMN
                try:
                    cls(control)
                except Exception as exc:
                    uncontrolled[f"{cls.name}.{option}"] = f"{type(exc).__name__}: {exc}"
                    continue

                # EVERY created field, not a representative one. A native
                # validator that happens to cover the alphabetically first
                # created name otherwise hides every unguarded name behind it.
                for target in sorted(created):
                    candidate = copy.deepcopy(base)
                    candidate[option] = target
                    try:
                        cls(candidate)
                    except Exception as exc:
                        if option not in str(exc):
                            unnamed[f"{cls.name}.{option} -> {target}"] = f"{type(exc).__name__}: {exc}"
                        continue
                    unguarded[f"{cls.name}.{option} -> {target}"] = target

        assert not uncontrolled, (
            "These options could not be repointed at an ordinary arriving column, so a rejection of the "
            f"created-field value below would not be attributable to the created-field-ness: {uncontrolled}"
        )
        assert not unguarded, (
            "These input-naming options accepted a value the plugin itself creates, so the transform "
            f"would read the column it is about to overwrite: {unguarded}"
        )
        assert not unnamed, (
            "These configs were rejected, but the error does not name the option to repoint — the "
            f"author is told a field is wrong without being told which option chose it: {unnamed}"
        )

    def test_the_sweep_would_catch_a_plugin_that_stopped_rejecting(self) -> None:
        """Guard the guard: the mutation must actually be the offending shape.

        If ``probe_config`` drifted so the mutated option no longer named a
        created field, every rejection above would be for some unrelated reason
        and the gate would pass while testing nothing.
        """
        from elspeth.plugins.transforms.web_scrape import WebScrapeTransform

        probe = WebScrapeTransform(WebScrapeTransform.probe_config())
        options = _input_naming_options(WebScrapeTransform, probe)

        assert set(options) == {"url_field"}
        assert options["url_field"] not in probe.self_created_input_fields
        assert sorted(probe.self_created_input_fields)[0] in probe.declared_output_fields

    def test_the_rejection_reaches_the_composer_as_a_config_error(self) -> None:
        """A draft pipeline must get a validation error, not a crashed validate().

        The composer probes plugins by CONSTRUCTING them from in-progress,
        LLM- or user-authored options — ``_instantiate_consumer`` and
        ``_check_schema_contracts`` both call ``create_transform`` — and only
        the exception types in ``_is_config_probe_exception`` are treated as
        ordinary bad input. Anything else is classified as an engine defect and
        propagates, so a construction-time guard raising the wrong type would
        turn an authoring mistake into a crashed ``validate()``.
        """
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from elspeth.plugins.transforms.web_scrape import WebScrapeTransform
        from elspeth.web.composer.state import _is_config_probe_exception

        options = {**WebScrapeTransform.probe_config(), "url_field": "fetch_status"}

        with pytest.raises(PluginConfigError) as excinfo:
            get_shared_plugin_manager().create_transform("web_scrape", options)

        assert _is_config_probe_exception(excinfo.value)
        assert excinfo.value.plugin_name == "web_scrape"
        assert excinfo.value.component_type == "transform"
