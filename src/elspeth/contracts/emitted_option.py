"""Declaration that a plugin option's VALUE is emitted into output.

Some plugin options are not references, selectors or field names — their literal
value is rendered into row data or into an artifact's bytes. A ``${VAR}`` in such
an option is expanded to a host environment value and then written where a
recipient can read it: a CSV header cell, a report title, the suffix on every
truncated field.

Two separate enforcement points need that fact:

* the plugin's own config model, which rejects a raw ``${VAR}`` when it validates
  its options; and
* :func:`elspeth.config_loading._reject_sensitive_plugin_env_placeholders_before_expansion`,
  the pre-expansion guard in the settings loader.

The guard exists because the plugin-side check is BYPASSABLE. On the CLI/YAML
path ``_expand_env_vars`` runs first, so by the time the plugin validates its
options it sees ``s3cr3t-host-value`` — a clean string that no longer matches
``${...}`` — and passes it.

Historically both enforcement points stated the policy independently: a
``field_validator`` on the plugin, and a hand-maintained
``{plugin_name: {field, ...}}`` map in the loader. They agreed only by luck —
one declarer, one map entry — and the map was a no-op for every plugin absent
from it, which is how ``truncate.suffix`` and ``csv.headers`` reached artifact
bytes (elspeth-8f0a6b3391).

So a literal-value emitter is declared ONCE, on the option field itself, and
both enforcement points DERIVE from that declaration:

.. code-block:: python

    class ReportAssembleConfig(TransformDataConfig):
        title: Annotated[
            str | None,
            EmittedToOutput("report_assemble emits this value in user-visible report output"),
        ] = Field(default=None, description="Optional report title")

The marker is metadata only. It performs no validation itself — the enforcing
validator lives on the plugin config base, and the loader guard reads the same
metadata through :func:`emitted_option_fields` and :func:`emitted_option_values`.

Options whose value names an output field already have a separate,
truth-tested declaration on the transform class:
``BaseTransform.output_naming_config_keys``. The loader guard unions that
authority with these markers, rather than duplicating every output-field list
as annotations. Use ``EmittedToOutput`` for literal values such as separators
and for derived naming options such as a prefix, where the configured value is
not itself one complete output field name.

**Declare on the base class, never on a redeclaration.** ``Annotated`` metadata
is inherited, but a subclass that RE-declares the field silently drops it:

.. code-block:: text

    Base             metadata=[EmittedToOutput(...)]
    Child            metadata=[EmittedToOutput(...)]   <- inherited
    ChildRedeclares  metadata=[]                       <- silently dropped

A field marked on a base and redeclared in a subclass is therefore unprotected
in that subclass with nothing to show for it, which is the same drift this
module removes.

When several models declare the same option and do NOT share a base — the sink
``headers`` option is declared by ``SinkPathConfig`` and, on a different branch
of the hierarchy, by the S3 and Azure sink configs — do not mark each site by
hand. Declare an annotated alias once and use it at every site
(``HeaderModeOption`` in ``plugins.infrastructure.config_base``). A shared base
or mixin would unify them too, but inherited fields sort first in
``model_json_schema()``, so it reorders the emitted schema for no behavioural
gain; an alias changes no emitted schema at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import UnionType
from typing import Union, get_args, get_origin

from pydantic import BaseModel

from elspeth.contracts.trust_boundary import trust_boundary

# The canonical ``${VAR}`` / ``${VAR:-default}`` reference pattern.
#
# This is the EXPANSION authority: ``_expand_env_vars`` substitutes exactly what
# this matches. Every check that exists to pre-empt expansion must use this same
# pattern, or it guards against a different language than the one that runs.
# Group 1 is the variable name, group 2 the optional default.
ENV_VAR_REFERENCE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass(frozen=True, slots=True)
class EmittedToOutput:
    """Marks an option field whose literal value is written into output.

    Attach via :data:`typing.Annotated` on the field's declaration. Applies to
    options whose VALUE is emitted — not to options that name an input column or
    select a mode, whose value never appears in output.

    Args:
        reason: Operator-facing explanation of where the value surfaces, quoted
            verbatim in the rejection message. Write it so someone who has never
            read this module understands why their ``${VAR}`` was refused —
            "``truncate`` appends this to every truncated value", not "forbidden".
        reject_at_validation: Also reject literal placeholders during plugin
            validation. Set False for content materialized after environment
            expansion: it must stay literal when read from a file or supplied
            through a loader that does not expand host environment values.
    """

    reason: str
    reject_at_validation: bool = True

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("EmittedToOutput.reason must explain where the value surfaces; it is shown to operators")


def emitted_option_fields(model: type[BaseModel] | None, *, for_env_expansion: bool = False) -> dict[str, str]:
    """Return ``{field_name: reason}`` for every :class:`EmittedToOutput` field.

    Reads ``model_fields`` metadata, so an inherited marker is found on the
    subclass without the subclass restating it.

    Args:
        model: A plugin config model, or ``None``. ``None`` is a normal return
            from ``get_config_model()`` for plugins that take no options (the
            ``null`` source) and means "declares nothing" — not an error.
        for_env_expansion: Include fields protected only before environment
            expansion. The default preserves plugin-validation semantics.

    Returns:
        Mapping of field name to the declared reason. Empty when the model
        declares no emitted options.
    """
    if model is None:
        return {}

    declared: dict[str, str] = {}
    for field_name in model.model_fields:
        for entry in model.model_fields[field_name].metadata:
            if isinstance(entry, EmittedToOutput) and (entry.reject_at_validation or for_env_expansion):
                declared[field_name] = entry.reason
                break
    return declared


def emitted_option_values(
    model: type[BaseModel] | None,
    value: object,
    *,
    nested_only: bool = False,
    for_env_expansion: bool = False,
) -> Iterator[tuple[str, object, str]]:
    """Yield declared emitted values and their paths in raw or validated config.

    Traverse only typed model/list/mapping fields, never arbitrary option data.
    A marker applies to that field's entire value; an unmarked container does
    not inherit a child's marker. In particular, a lookup's defaults do not
    make its selectors or reference table content emitted options.

    Malformed raw shapes remain the responsibility of config validation. The
    traversal does not validate a model or expand any environment references.
    ``for_env_expansion`` includes pre-expansion-only declarations; the default
    yields only fields whose literals are also forbidden during validation.
    """
    if model is not None:
        yield from _model_emitted_values(model, value, "", nested_only=nested_only, for_env_expansion=for_env_expansion)


def _model_emitted_values(
    model: type[BaseModel], value: object, prefix: str, *, nested_only: bool, for_env_expansion: bool
) -> Iterator[tuple[str, object, str]]:
    if isinstance(value, BaseModel):
        owned = True
        values = dict(value)
    elif isinstance(value, Mapping):
        owned = False
        values = dict(value)
    else:
        return

    declared = emitted_option_fields(model, for_env_expansion=for_env_expansion)
    for name, field in model.model_fields.items():
        # Validated models iterate by field name; raw authors may use aliases.
        key = name if owned else field.alias or name
        if key not in values:
            if not owned and name in values and model.model_config.get("populate_by_name"):
                key = name
            else:
                continue
        display_name = field.alias or key
        path = f"{prefix}.{display_name}" if prefix else display_name
        item = values[key]
        if name in declared and (prefix or not nested_only):
            yield path, item, declared[name]
        yield from _annotation_emitted_values(field.annotation, item, path, for_env_expansion=for_env_expansion)


def _annotation_emitted_values(
    annotation: object, value: object, path: str, *, for_env_expansion: bool
) -> Iterator[tuple[str, object, str]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield from _model_emitted_values(annotation, value, path, nested_only=False, for_env_expansion=for_env_expansion)
        return
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Union or origin is UnionType:
        for member in args:
            yield from _annotation_emitted_values(member, value, path, for_env_expansion=for_env_expansion)
    elif origin is list and args and isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _annotation_emitted_values(args[0], item, f"{path}[{index}]", for_env_expansion=for_env_expansion)
    elif origin is dict and len(args) == 2 and isinstance(value, Mapping):
        for key, item in value.items():
            yield from _annotation_emitted_values(args[1], item, f"{path}[{key!r}]", for_env_expansion=for_env_expansion)


@trust_boundary(
    tier=3,
    source=(
        "the raw value of one plugin option from a pipeline author's settings YAML or a web-authored "
        "config dict — an untyped str | dict | list tree ELSPETH does not own"
    ),
    source_param="value",
    suppresses=("R5",),
    invariant=(
        "returns True only when a recognized str/dict/list-tuple shape contains an env reference; every "
        "unsupported shape returns False; never raises on malformed input"
    ),
    non_raising=True,
)
def env_placeholders_in(value: object) -> bool:
    """Does ``value`` contain an env reference anywhere inside it?

    Recursive, because an emitted option is not always a bare string: the sink
    ``headers`` option takes ``str | dict[str, str] | None`` and it is the
    MAPPING form whose values are written as the artifact's header row. A
    top-level ``isinstance(value, str)`` check walks straight past it.
    """
    if isinstance(value, str):
        return bool(ENV_VAR_REFERENCE_PATTERN.search(value))
    if isinstance(value, dict):
        return any(env_placeholders_in(item) for item in value.values()) or any(env_placeholders_in(key) for key in value)
    if isinstance(value, list | tuple):
        return any(env_placeholders_in(item) for item in value)
    return False
