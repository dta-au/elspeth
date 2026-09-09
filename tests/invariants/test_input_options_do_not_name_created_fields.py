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

SCOPE, and why it is not "every transform". The roster is the transforms whose
CONFIG declares an arriving column, i.e. whose config class overrides
``TransformDataConfig.declared_input_fields``. Batch transforms are deliberately
outside it: for them, created-and-consumed overlap is a DOCUMENTED LEGITIMATE
SHAPE — ``BaseTransform.consumed_input_fields`` names "a second-stage
aggregation reading an upstream ``mean`` while emitting its own" as the
canonical case, because batch output names are generic. ``batch_stats`` with
``value_field: "mean"`` therefore constructs today with ``mean`` in its
``self_created_input_fields``, and whether that shape survives the executor's
collision check on the batch dispatch path is an open question this gate does
not adjudicate. Widening the roster to batch transforms is a separate decision,
not a tightening of this one.

The roster is DISCOVERED from a live ``PluginManager``, so a new plugin that
declares an input column is swept without being added here. Anti-vacuity is
enforced four ways: the roster must be non-empty; it must still contain every
plugin the defect was measured on; every member must expose at least one
input-naming option (a member with none would pass by having nothing to test);
and each member must still construct under its own ``probe_config``, so a guard
that rejects legal configs fails here rather than in production.

Rejection is asserted BEHAVIOURALLY, not by mechanism. ``blob_fetch`` and
``blob_csv_expand`` were never affected because they carry native collision
validators, and they pass this gate through those validators rather than
through the shared helper the other four call. A future plugin may close the
hole a third way; the gate cares that the config is refused and that the error
names the option to repoint, which is the actionable half.

KNOWN LIMITS, so nobody reads more assurance into this than it carries:

* Options are discovered from each plugin's own ``probe_config()``, and only
  options DECLARED as a scalar column name (``str`` or ``str | None``) are
  mutated. The declared ANNOTATION is the test, not the probe's runtime value:
  reading the value admitted ``llm.output_fields`` — annotated
  ``list[OutputFieldConfig] | None`` and merely defaulting to ``None`` — into a
  sweep that then assigned it a plain string, so the CONTROL arm died on a
  pydantic list-type error and the whole gate aborted before reaching its real
  assertion (elspeth-2865a8efb8; landed by 2b452ff8c, which lifted the
  multi-query ``output_fields`` to the llm transform's config top level). A
  value-shaped filter cannot tell "no column configured" from "not a column
  option at all"; the annotation can, so it is what decides.
  Genuinely list-valued naming surfaces stay out of scope —
  ``azure_document_intelligence.query_fields`` matches the ``*_fields`` naming
  rule but holds Azure-side query names, not row columns, so mutating it would
  assert a contract that does not exist. ``llm``'s ``image_inputs`` folds
  ``field``/``format_field`` into ``declared_input_fields`` (a real
  column-naming surface — see ``LLMConfig.declared_input_fields``) but those
  names live inside ``list[ImageInputConfig]`` entries, not a top-level scalar
  option, so ``llm`` exposes NO scalar naming option and leaves the roster via
  ``_has_a_scalar_naming_option``. ``image_inputs[].field`` naming a created
  field (e.g. ``response_field``) is therefore NOT swept by this gate — tracked
  debt, not a silent gap, and pinned below by
  ``test_llm_is_unswept_because_it_has_no_scalar_naming_option``, which DERIVES
  llm's absence from the same machinery the roster uses rather than declaring
  it. The day llm gains a scalar naming option, that test fails and llm rejoins
  the roster on its own.
* Mutating a ``None``-valued option can collide with an interlock rather than
  with the created-field rule — ``aws_textract_document_analysis`` makes
  ``bucket`` and ``bucket_field`` mutually exclusive, and a future option in
  that shape would be rejected for the wrong reason. The control mutation below
  is what keeps that honest: an option that cannot first be repointed at an
  ordinary arriving column is reported rather than counted as covered.
"""

from __future__ import annotations

import copy
from types import UnionType
from typing import Any, Union, get_args, get_origin

import pytest

from elspeth.plugins.infrastructure.base import BaseTransform, is_column_naming_config_option
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.manager import PluginManager

# Plugins measured to declare an arriving column through a config option. Two
# were already closed natively when the defect was found (``blob_fetch``,
# ``blob_csv_expand``); the other four were the live repros. If a refactor drops
# one out of scope, coverage of the original defect has been lost silently.
_MEASURED_DEFECT_ANCHORS = frozenset(
    {
        "aws_textract_document_analysis",
        "azure_document_intelligence",
        "blob_csv_expand",
        "blob_fetch",
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


def _declares_an_arriving_column(cls: type[BaseTransform]) -> bool:
    """Whether this plugin's config folds a config option into its input contract."""
    config_model = cls.config_model
    if config_model is None or not issubclass(config_model, TransformDataConfig):
        return False
    return config_model.declared_input_fields is not TransformDataConfig.declared_input_fields


def _has_a_scalar_naming_option(cls: type[BaseTransform]) -> bool:
    """Whether this plugin exposes at least one ``str | None`` naming option to mutate.

    ``_declares_an_arriving_column`` is a proxy for "this config has a
    column-naming surface to test", and for every anchor plugin the two
    coincide. They came apart for ``llm``: ``LLMConfig.declared_input_fields``
    folds in ``image_inputs[].field``/``format_field`` (base.py, "``required_
    input_fields`` plus every ``image_inputs`` field/format_field"), but those
    names live inside a ``list[ImageInputConfig]``, not a top-level ``str |
    None`` field, so ``_input_naming_options`` — which only mutates a top-
    level dict key (see the control-mutation step below) — finds nothing to
    test. Per KNOWN LIMITS above, list-valued naming surfaces are out of this
    gate's scope; this predicate makes the roster match that stated scope
    instead of failing the anti-vacuity assertion on a plugin the mechanism
    cannot exercise. ``image_inputs[].field`` is therefore NOT swept by this
    gate — a real coverage gap, left for a future gate widening rather than
    invented here.
    """
    probe = cls(cls.probe_config())
    return bool(_input_naming_options(cls, probe))


def _declares_a_scalar_column_name(annotation: Any) -> bool:
    """Whether a config field is DECLARED to hold one column name: ``str`` or ``str | None``.

    The mutation below assigns a plain string, so an option this returns False
    for cannot be exercised by this gate at all — assigning a string to a
    list-annotated field fails in pydantic before any plugin guard is reached,
    which is a fact about the mutation mechanism and not about the plugin.

    ANNOTATION, never the probe's runtime value. The two disagree on exactly the
    shape that broke this gate: ``llm.output_fields`` is
    ``list[OutputFieldConfig] | None`` and its probe value is ``None``, so a
    value-shaped filter (``value is None or isinstance(value, str)``) admitted
    it as if it were an unset column option (elspeth-2865a8efb8). "No column
    configured" and "not a column option at all" are indistinguishable in the
    value and unambiguous in the type.

    ``str | None`` is admitted because an unset scalar option is still a scalar
    option — ``aws_textract_document_analysis.version_field`` is exactly that,
    and dropping it would silently narrow the sweep.
    """
    if annotation is str:
        return True
    if get_origin(annotation) is not UnionType and get_origin(annotation) is not Union:
        return False
    # A union qualifies only if it is str-plus-None. Any other member means the
    # field can legitimately hold a non-string, so a string mutation is not a
    # test of the plugin's contract.
    members = set(get_args(annotation))
    return str in members and members <= {str, type(None)}


def _input_naming_options(cls: type[BaseTransform], probe: BaseTransform) -> dict[str, str | None]:
    """Column-naming config options this plugin READS, with their probe values.

    Mirrors the read/write split ``BaseTransform.consumed_input_fields`` uses:
    naming alone cannot say which, so ``output_naming_config_keys`` is the
    plugin's own classification and everything else is treated as a read.
    """
    validated = probe._validated_config
    assert validated is not None, f"{cls.name}: probe captured no validated config; options cannot be discovered"

    # ``dict(model)`` is pydantic's own field-value view: it yields the raw
    # (unserialised) value for every declared field by name, so the option
    # values are read from the model's data surface rather than probed off it.
    field_values = dict(validated)

    options: dict[str, str | None] = {}
    for name, field in type(validated).model_fields.items():
        if name in _GENERIC_DECLARATION_OPTIONS or name in cls.output_naming_config_keys:
            continue
        if not is_column_naming_config_option(name):
            continue
        if not _declares_a_scalar_column_name(field.annotation):
            continue
        options[name] = field_values[name]
    return options


class TestInputOptionsDoNotNameCreatedFields:
    def test_every_plugin_declaring_an_input_column_rejects_naming_a_created_field(self) -> None:
        roster = [cls for cls in _registered_transform_classes() if _declares_an_arriving_column(cls) and _has_a_scalar_naming_option(cls)]

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

            target = sorted(created)[0]
            for option in sorted(options):
                # CONTROL first. Repointing the option at an ordinary arriving
                # column must construct, so the rejection below is attributable
                # to the value being CREATED rather than to the option having
                # been touched at all. This is what lets the assertion stay
                # behavioural: blob_fetch and blob_csv_expand reject through
                # their own native validators, whose messages share no wording
                # with the shared helper, so matching on the guard's phrasing
                # would test the mechanism instead of the contract.
                control = copy.deepcopy(base)
                control[option] = _CONTROL_COLUMN
                try:
                    cls(control)
                except Exception as exc:
                    uncontrolled[f"{cls.name}.{option}"] = f"{type(exc).__name__}: {exc}"
                    continue

                candidate = copy.deepcopy(base)
                candidate[option] = target
                try:
                    cls(candidate)
                except Exception as exc:
                    if option not in str(exc):
                        unnamed[f"{cls.name}.{option}"] = f"{type(exc).__name__}: {exc}"
                    continue
                unguarded[f"{cls.name}.{option}"] = target

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

    def test_llm_is_unswept_because_it_has_no_scalar_naming_option(self) -> None:
        """``llm``'s absence from the roster is DERIVED, never declared.

        ``llm`` really does declare arriving columns — ``LLMConfig.declared_input_fields``
        folds in every ``image_inputs[].field`` / ``format_field`` — but they
        live inside ``list[ImageInputConfig]`` entries, and this gate's mutation
        assigns a top-level string. So llm is genuinely unswept, and that gap is
        tracked debt (see KNOWN LIMITS), not something this file may quietly
        launder into looking like coverage.

        The failure mode this test exists to prevent is a STALE EXEMPTION. A test
        asserting "llm is exempt" would pin EXISTENCE, not TRUTH: it would keep
        passing on the day llm gains a top-level scalar naming option, which is
        exactly the day the sweep needed to pick llm back up. So the assertion
        COMPUTES llm's option set from the same ``_input_naming_options`` the
        roster uses, and requires it to be empty. Give llm a ``str`` naming
        option and this fails, pointing at the roster it must now join —
        the same derive-don't-restate discipline the file's own docstring
        demands of the plugins it sweeps.

        Both halves are asserted, because they can come apart and the pair is
        what makes the exemption honest: llm is in scope by the arriving-column
        predicate, and out of scope only by the scalar-option one.
        """
        llm_classes = [cls for cls in _registered_transform_classes() if cls.name == "llm"]
        assert len(llm_classes) == 1, f"expected exactly one registered 'llm' transform, got {len(llm_classes)}"
        llm_cls = llm_classes[0]

        assert _declares_an_arriving_column(llm_cls), (
            "llm no longer declares an arriving column, so the exemption below is describing a plugin "
            "that has left this gate's scope entirely — re-read LLMConfig.declared_input_fields."
        )

        probe = llm_cls(llm_cls.probe_config())
        try:
            options = _input_naming_options(llm_cls, probe)
        finally:
            probe.close()

        assert options == {}, (
            f"llm now exposes scalar column-naming option(s) {sorted(options)}, so it is no longer outside "
            f"this gate's mutation mechanism and must be swept like every other plugin. Delete this test's "
            f"exemption and confirm llm passes the sweep above for the right reason — it will now be in the "
            f"roster automatically via _has_a_scalar_naming_option."
        )
        assert not _has_a_scalar_naming_option(llm_cls), "roster predicate and option discovery disagree about llm"

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
