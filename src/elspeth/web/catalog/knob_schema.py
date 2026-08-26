"""Composer one-knob wire shape.

Lowering happens at catalog load time inside ``CatalogServiceImpl.__init__``;
this module exposes the result types and the lowering entry points. See
docs/superpowers/specs/2026-05-14-composer-one-knob-design.md.

Trust tier: L3 web layer. ``KnobSchema`` instances are Tier 1 because we write
them from plugin models we control. Prefilled values from
``SourceInspectionFacts`` remain Tier 3.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from enum import Enum
from inspect import isclass
from typing import Annotated, Any, Literal, NotRequired, TypedDict, Union, cast, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

FieldKind = Literal[
    "text",
    "number-int",
    "number-float",
    "checkbox",
    "enum",
    "string-list",
    "blob-ref",
    "json-object",
    "json-array",
    "json-value",
]
FieldTier = Literal["essential", "common", "advanced"]


class VisibilityPredicate(TypedDict):
    """Conditional-visibility predicate for a ``KnobField``.

    ``field`` must reference an earlier-declared ``KnobField`` in the same
    ``KnobSchema``. ``equals`` is an exact value match against current form
    state. No other keys are permitted; invalid predicates raise
    ``KnobSchemaLoweringError`` at catalog load.
    """

    field: str
    equals: Any


_PREDICATE_KEYS: frozenset[str] = frozenset({"field", "equals"})


class KnobField(TypedDict):
    name: str
    label: str
    description: NotRequired[str]
    placeholder: NotRequired[str]
    kind: FieldKind
    tier: NotRequired[FieldTier]
    required: bool
    default: NotRequired[object]
    nullable: bool
    enum: NotRequired[list[str]]
    item_kind: NotRequired[Literal["text", "number-int", "number-float"]]
    item_schema: NotRequired[KnobSchema]
    visible_when: NotRequired[VisibilityPredicate]
    # Conditional requiredness. ``required`` carries pydantic's field-level
    # requiredness, which is the only requiredness the model itself knows.
    # Some fields are additionally required by a rule the COMPOSER owns and the
    # model does not: a local file sink's ``collision_policy`` is
    # ``default=None`` (YAML may omit it) yet
    # ``validate_composer_file_sink_collision_policy`` rejects a runnable file
    # sink that omits it under ``mode='write'``. Without this predicate the form
    # reports the knob optional and lets the user press Continue into a
    # guaranteed rejection (R2-F2). ``required_when`` is additive: a field is
    # required when ``required`` is true OR the predicate holds against current
    # form state.
    required_when: NotRequired[VisibilityPredicate]


class KnobSchema(TypedDict):
    fields: list[KnobField]


class SchemaFormPayload(TypedDict):
    mode: Literal["plugin_options"]
    plugin: str
    knobs: KnobSchema
    prefilled: dict[str, object]


class KnobSchemaLoweringError(Exception):
    """Raised at catalog load for malformed schemas or one-knob violations.

    Valid-but-rich fields lower to ``json-object``, ``json-array``, or
    ``json-value`` fallback knobs. True invariant violations halt startup.
    """

    def __init__(
        self,
        *,
        plugin_kind: str,
        plugin_name: str,
        field_path: str,
        constraint: str,
        remediation: str,
    ) -> None:
        message = f"Plugin {plugin_kind}/{plugin_name} field {field_path!r}: {constraint}. Remediation: {remediation}"
        super().__init__(message)
        self.plugin_kind = plugin_kind
        self.plugin_name = plugin_name
        self.field_path = field_path
        self.constraint = constraint
        self.remediation = remediation


_TYPE_TO_KIND: dict[type, Literal["text", "number-int", "number-float", "checkbox"]] = {
    str: "text",
    int: "number-int",
    float: "number-float",
    bool: "checkbox",
}


def _unwrap_annotated(annotation: Any) -> Any:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Return ``(inner_type, nullable)`` for ``T | None`` and ``Optional[T]``."""
    annotation = _unwrap_annotated(annotation)
    origin = get_origin(annotation)
    if origin in (types.UnionType, Union):
        args = get_args(annotation)
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1 and len(non_none) != len(args):
            return _unwrap_annotated(non_none[0]), True
    return annotation, False


def _kind_for_scalar(
    inner: Any,
) -> tuple[Literal["text", "number-int", "number-float", "checkbox", "enum", "json-value"], list[str] | None]:
    """Map a Python scalar, Literal, or Enum type to a field kind and enum values."""
    if get_origin(inner) is Literal:
        values = [str(value) for value in get_args(inner)]
        return "enum", values
    if isclass(inner) and issubclass(inner, Enum):
        # A closed Enum (e.g. the LLM ResponseFormat / OutputFieldType StrEnums)
        # is a genuine enumeration, not an opaque json-value. Expose its member
        # values so discovery advertises the real choice set.
        return "enum", [str(member.value) for member in inner]
    if inner in _TYPE_TO_KIND:
        return _TYPE_TO_KIND[inner], None
    return "json-value", None


def _base_field(
    *,
    name: str,
    info: FieldInfo,
    kind: FieldKind,
    nullable: bool,
) -> KnobField:
    # Lower the knob under the field's user-facing ALIAS when it has one. The
    # knob is a user-facing surface like JSON Schema / YAML, which Pydantic keys
    # by alias (e.g. the data-plugin ``schema_config`` field is exposed as
    # ``schema``). The composer's ``prefilled`` and committed plugin options are
    # also keyed by alias, so a knob lowered under the internal field name would
    # never be populated by them. ``populate_by_name`` lets the form resubmit by
    # alias regardless.
    wire_name = info.alias if info.alias is not None else name
    field: KnobField = {
        "name": wire_name,
        "label": info.title or wire_name,
        "kind": kind,
        "required": info.is_required(),
        "nullable": nullable,
    }
    # ``description`` is the CLI/YAML truth for the field and must stay accurate
    # for a hand-authored settings file. The composer form is a different
    # audience: the same knob is often narrower on the web (a source ``path`` is
    # confined to this session's uploads) and the form has no surrounding
    # document to explain a nested shape. ``composer_description`` therefore
    # *replaces* ``description`` on this surface rather than appending to it.
    composer_description = _composer_description(info)
    if composer_description is not None:
        field["description"] = composer_description
    elif info.description:
        field["description"] = info.description
    placeholder = _composer_placeholder(info)
    if placeholder is not None:
        field["placeholder"] = placeholder
    _attach_default(field, info)
    _attach_tier(field, info)
    _attach_required_when(field, info)
    return field


# Bound on nested-model recursion depth. The typed query models nest at most
# two levels (queries -> QueryDefinition -> output_fields -> OutputFieldConfig),
# so a small bound is ample and guards against a self-referential model.
_MAX_NESTED_DEPTH = 4


def _member_nested_model(member: Any) -> type[BaseModel] | None:
    """Return the nested ``BaseModel`` a ``list[M]`` / ``dict[str, M]`` / ``M`` carries."""
    member = _unwrap_annotated(member)
    origin = get_origin(member)
    if origin is list:
        args = get_args(member)
        if len(args) == 1:
            item = _unwrap_annotated(args[0])
            if isclass(item) and issubclass(item, BaseModel):
                return item
        return None
    if origin in (dict, Mapping):
        args = get_args(member)
        if len(args) == 2:
            value = _unwrap_annotated(args[1])
            if isclass(value) and issubclass(value, BaseModel):
                return value
        return None
    if isclass(member) and issubclass(member, BaseModel):
        return member
    return None


def _dual_form_nested_model(annotation: Any) -> tuple[type[BaseModel], bool] | None:
    """Detect the dual-form ``list[M] | dict[str, M]`` (optionally ``| None``) shape.

    This is the public shape of ``LLMConfig.queries``: a mapping keyed by query
    name and a list of named entries, both carrying the same nested model ``M``.
    A plain ``list[M]``, ``dict[str, M]``, or scalar union is intentionally *not*
    matched here — only the list+dict union that would otherwise collapse to an
    opaque ``json-value`` knob. Returns ``(M, nullable)`` or ``None``.
    """
    annotation = _unwrap_annotated(annotation)
    if get_origin(annotation) not in (types.UnionType, Union):
        return None
    args = get_args(annotation)
    nullable = type(None) in args
    members = [arg for arg in args if arg is not type(None)]
    if len(members) < 2:
        return None
    models: set[type[BaseModel]] = set()
    for member in members:
        model = _member_nested_model(member)
        if model is None:
            return None
        models.add(model)
    if len(models) != 1:
        return None
    return next(iter(models)), nullable


def _lower_nested_field(name: str, info: FieldInfo, *, depth: int) -> KnobField:
    """Lower one field of a nested model, recursing into further nested models."""
    inner, nullable = _unwrap_optional(info.annotation)
    origin = get_origin(inner)

    if depth < _MAX_NESTED_DEPTH:
        if origin is list:
            args = get_args(inner)
            if len(args) == 1:
                item = _unwrap_annotated(args[0])
                if isclass(item) and issubclass(item, BaseModel):
                    field = _base_field(name=name, info=info, kind="json-array", nullable=nullable)
                    field["item_schema"] = _lower_nested_model(item, depth=depth + 1)
                    return field
        if origin in (dict, Mapping):
            args = get_args(inner)
            if len(args) == 2:
                value = _unwrap_annotated(args[1])
                if isclass(value) and issubclass(value, BaseModel):
                    field = _base_field(name=name, info=info, kind="json-object", nullable=nullable)
                    field["item_schema"] = _lower_nested_model(value, depth=depth + 1)
                    return field
        if isclass(inner) and issubclass(inner, BaseModel):
            field = _base_field(name=name, info=info, kind="json-object", nullable=nullable)
            field["item_schema"] = _lower_nested_model(inner, depth=depth + 1)
            return field

    # Scalars, enums, string-lists, and dict[str, scalar] reuse the flat lowering.
    return _lower_field(name, info, plugin_kind="", plugin_name="", composer_tier_default="common")


def _lower_nested_model(model_cls: type[BaseModel], *, depth: int) -> KnobSchema:
    """Lower every (non-hidden) field of a nested model to a nested ``KnobSchema``."""
    fields: list[KnobField] = []
    for name, info in model_cls.model_fields.items():
        if _is_composer_hidden(info):
            continue
        fields.append(_lower_nested_field(name, info, depth=depth))
    return {"fields": fields}


def _lower_field(
    name: str,
    info: FieldInfo,
    *,
    plugin_kind: str,
    plugin_name: str,
    composer_tier_default: str,
) -> KnobField:
    del plugin_kind, plugin_name, composer_tier_default

    # Dual-form nested-model union (LLMConfig.queries): expose the typed query
    # structure instead of collapsing the list|dict union to an opaque json-value.
    dual_form = _dual_form_nested_model(info.annotation)
    if dual_form is not None:
        model_cls, nullable = dual_form
        field = _base_field(name=name, info=info, kind="json-array", nullable=nullable)
        field["item_schema"] = _lower_nested_model(model_cls, depth=1)
        return field

    inner, nullable = _unwrap_optional(info.annotation)
    origin = get_origin(inner)

    if origin is list:
        list_args = get_args(inner)
        if len(list_args) == 1 and _unwrap_annotated(list_args[0]) is str:
            field = _base_field(name=name, info=info, kind="string-list", nullable=nullable)
            field["item_kind"] = "text"
            return field
        return _base_field(name=name, info=info, kind="json-array", nullable=nullable)

    is_model_cls = isclass(inner) and issubclass(inner, BaseModel)
    if origin in (dict, Mapping) or is_model_cls:
        return _base_field(name=name, info=info, kind="json-object", nullable=nullable)

    kind, enum_values = _kind_for_scalar(inner)
    field = _base_field(name=name, info=info, kind=kind, nullable=nullable)
    if enum_values is not None:
        field["enum"] = enum_values
    return field


def _attach_default(field: KnobField, info: FieldInfo) -> None:
    if info.is_required() or info.default is PydanticUndefined:
        return
    field["default"] = info.default


def _attach_tier(field: KnobField, info: FieldInfo) -> None:
    extra = info.json_schema_extra
    if type(extra) is not dict:
        return
    if "composer_tier" not in extra:
        return
    tier = extra["composer_tier"]
    if tier in ("essential", "common", "advanced"):
        field["tier"] = cast(FieldTier, tier)


def _attach_required_when(field: KnobField, info: FieldInfo) -> None:
    """Attach the composer-owned conditional-requiredness predicate, if declared.

    Absence is the no-op. A present-but-malformed value is a mistake in a plugin
    model we author (``KnobSchema`` is Tier 1), so it raises at catalog load
    rather than silently shipping a form that under-gates a knob the composer
    will reject — the exact failure this predicate exists to prevent. The
    membership-then-index shape mirrors ``_attach_tier`` / ``_is_composer_hidden``.
    """
    extra = info.json_schema_extra
    if type(extra) is not dict:
        return
    if "composer_required_when" not in extra:
        return
    predicate = extra["composer_required_when"]
    if not isinstance(predicate, Mapping) or frozenset(predicate) != _PREDICATE_KEYS:
        raise TypeError(
            f"json_schema_extra['composer_required_when'] must be a mapping with keys {sorted(_PREDICATE_KEYS)}, got {predicate!r}"
        )
    target = predicate["field"]
    if type(target) is not str or not target:
        raise TypeError(f"json_schema_extra['composer_required_when']['field'] must be a non-empty str, got {target!r}")
    field["required_when"] = {"field": target, "equals": predicate["equals"]}


def lower_model_to_knob_schema(
    model_cls: type[BaseModel],
    *,
    plugin_kind: str,
    plugin_name: str,
    composer_tier_default: str = "common",
) -> KnobSchema:
    """Lower a single-model Pydantic config class to ``KnobSchema``.

    Fields whose ``json_schema_extra`` carries ``{"composer_hidden": True}``
    are skipped entirely. Use this for audit-anchor fields the runtime
    writes (e.g. ``resolved_prompt_template_hash`` on ``LLMConfig``); they
    are valid YAML inputs the composer service emits internally, but they
    must not appear as user-editable knobs because a user-set value would
    falsify the audit trail.
    """
    fields: list[KnobField] = []
    for name, info in model_cls.model_fields.items():
        if _is_composer_hidden(info):
            continue
        fields.append(
            _lower_field(
                name,
                info,
                plugin_kind=plugin_kind,
                plugin_name=plugin_name,
                composer_tier_default=composer_tier_default,
            )
        )
    return {"fields": fields}


def _is_composer_hidden(info: FieldInfo) -> bool:
    """Return True when a field is marked ``composer_hidden=True``.

    Hidden fields are still valid Pydantic inputs (the runtime writes them
    via the resolve helper); they simply must not be surfaced as knobs in
    the composer catalog UI. The membership-then-index pattern mirrors the
    offensive idiom used elsewhere in this module — direct indexing
    surfaces any non-bool value as a load-bearing crash rather than a
    silently-false default.
    """
    extra = info.json_schema_extra
    if type(extra) is not dict:
        return False
    if "composer_hidden" not in extra:
        return False
    return bool(extra["composer_hidden"])


def _composer_str_extra(info: FieldInfo, key: str) -> str | None:
    """Return the exact string a field declares under ``json_schema_extra[key]``.

    Absence is the no-op: the caller keeps whatever it would have written
    without the extra. A present-but-non-string value is a mistake in a
    plugin model we author (``KnobSchema`` is Tier 1), so it raises at
    catalog load rather than degrading to the CLI text and shipping a
    composer surface nobody notices is wrong.
    """
    extra = info.json_schema_extra
    if type(extra) is not dict:
        return None
    if key not in extra:
        return None
    value = extra[key]
    if type(value) is not str or not value:
        raise TypeError(f"json_schema_extra[{key!r}] must be a non-empty str, got {value!r}")
    return value


def _composer_description(info: FieldInfo) -> str | None:
    """Return the composer-facing description override, if the field declares one."""
    return _composer_str_extra(info, "composer_description")


def _composer_placeholder(info: FieldInfo) -> str | None:
    """Return the composer-facing input placeholder, if the field declares one.

    A placeholder is a shape hint for a free-text knob whose value has
    internal structure the label cannot carry (a JSON schema block, a
    connection string). It is never a default: nothing is submitted unless
    the user types it.
    """
    return _composer_str_extra(info, "composer_placeholder")


def lower_discriminated_to_knob_schema(
    plugin_cls: type,
    *,
    plugin_kind: str,
    plugin_name: str,
    composer_tier_default: str = "common",
) -> KnobSchema:
    """Lower a discriminated-union plugin to a flat visible_when schema."""
    try:
        discriminated_variants = cast(Any, plugin_cls).discriminated_variants
    except AttributeError as exc:
        raise KnobSchemaLoweringError(
            plugin_kind=plugin_kind,
            plugin_name=plugin_name,
            field_path="<class>",
            constraint=("plugin lacks discriminated_variants() classmethod required by DiscriminatedPlugin protocol"),
            remediation=("Implement discriminated_variants() returning (discriminator_field_name, {literal_value: variant_cls})."),
        ) from exc
    if not callable(discriminated_variants):
        raise KnobSchemaLoweringError(
            plugin_kind=plugin_kind,
            plugin_name=plugin_name,
            field_path="<class>",
            constraint=("plugin lacks discriminated_variants() classmethod required by DiscriminatedPlugin protocol"),
            remediation=("Implement discriminated_variants() returning (discriminator_field_name, {literal_value: variant_cls})."),
        )
    discriminator, variants = discriminated_variants()

    fields: list[KnobField] = [
        {
            "name": discriminator,
            "label": discriminator,
            "kind": "enum",
            "enum": list(variants.keys()),
            "required": True,
            "nullable": False,
        }
    ]
    for variant_value, variant_cls in variants.items():
        for fname, info in variant_cls.model_fields.items():
            if fname == discriminator:
                continue
            if _is_composer_hidden(info):
                continue
            inner_field = _lower_field(
                fname,
                info,
                plugin_kind=plugin_kind,
                plugin_name=plugin_name,
                composer_tier_default=composer_tier_default,
            )
            inner_field["visible_when"] = {"field": discriminator, "equals": variant_value}
            fields.append(inner_field)
    return {"fields": fields}


def validate_knob_schema(
    schema: KnobSchema,
    *,
    plugin_kind: str,
    plugin_name: str,
) -> None:
    """Validate KnobSchema invariants enforced at catalog load."""
    all_names = [field["name"] for field in schema["fields"]]
    seen_so_far: set[str] = set()
    visibility_gated: set[str] = set()

    for field in schema["fields"]:
        _validate_required_when(field, all_names, plugin_kind=plugin_kind, plugin_name=plugin_name)
        if "visible_when" not in field:
            seen_so_far.add(field["name"])
            continue

        pred = field["visible_when"]
        keys = frozenset(pred)
        if keys != _PREDICATE_KEYS:
            raise KnobSchemaLoweringError(
                plugin_kind=plugin_kind,
                plugin_name=plugin_name,
                field_path=field["name"],
                constraint=f"visible_when has keys {sorted(keys)}; only 'field' and 'equals' permitted",
                remediation="Remove extra keys; AND/OR predicates are out of scope",
            )

        target = pred["field"]
        if target not in seen_so_far:
            if target in all_names:
                raise KnobSchemaLoweringError(
                    plugin_kind=plugin_kind,
                    plugin_name=plugin_name,
                    field_path=field["name"],
                    constraint=f"visible_when references forward field {target!r}",
                    remediation="Re-order fields so the discriminator is declared first",
                )
            raise KnobSchemaLoweringError(
                plugin_kind=plugin_kind,
                plugin_name=plugin_name,
                field_path=field["name"],
                constraint=f"visible_when references unknown field {target!r}",
                remediation="Check the field name; only earlier-declared KnobFields are valid targets",
            )

        if target in visibility_gated:
            raise KnobSchemaLoweringError(
                plugin_kind=plugin_kind,
                plugin_name=plugin_name,
                field_path=field["name"],
                constraint=f"visible_when targets {target!r} which is itself visible_when-gated (nested visibility chain)",
                remediation="Flatten the predicate chain; visibility nesting is out of scope",
            )

        visibility_gated.add(field["name"])
        seen_so_far.add(field["name"])


def _validate_required_when(
    field: KnobField,
    all_names: list[str],
    *,
    plugin_kind: str,
    plugin_name: str,
) -> None:
    """Validate a ``required_when`` predicate.

    Deliberately WEAKER than the ``visible_when`` rules above, and the
    difference is load-bearing. ``visible_when`` gates RENDERING, so its target
    must be decided before the gated field is reached — hence the
    earlier-field and no-nesting rules. ``required_when`` gates only whether an
    always-rendered field must be filled; the form reads sibling state, which
    exists for every field regardless of declaration order. Forcing the
    visible_when ordering rule here would reject the one case this exists for:
    ``collision_policy`` lives on ``LocalFileSinkConfig`` and its target
    ``mode`` on the concrete sink subclass, so the target lowers LATER.

    A target naming no field at all is still a lowering bug — the predicate
    would silently never fire and the knob would under-gate exactly as it did
    before R2-F2 — so membership in ``all_names`` is checked.
    """
    if "required_when" not in field:
        return

    pred = field["required_when"]
    keys = frozenset(pred)
    if keys != _PREDICATE_KEYS:
        raise KnobSchemaLoweringError(
            plugin_kind=plugin_kind,
            plugin_name=plugin_name,
            field_path=field["name"],
            constraint=f"required_when has keys {sorted(keys)}; only 'field' and 'equals' permitted",
            remediation="Remove extra keys; AND/OR predicates are out of scope",
        )

    target = pred["field"]
    if target not in all_names:
        raise KnobSchemaLoweringError(
            plugin_kind=plugin_kind,
            plugin_name=plugin_name,
            field_path=field["name"],
            constraint=f"required_when references unknown field {target!r}",
            remediation="Check the field name; the target must be another KnobField on the same schema (forward references are legal)",
        )

    if target == field["name"]:
        raise KnobSchemaLoweringError(
            plugin_kind=plugin_kind,
            plugin_name=plugin_name,
            field_path=field["name"],
            constraint="required_when references itself",
            remediation="Point the predicate at a different field",
        )
