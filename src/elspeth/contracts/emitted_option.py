"""Declaration that a plugin option's VALUE is emitted into output.

Some plugin options are not references, selectors or field names — their literal
value is rendered into row data or into an artifact's bytes. A ``${VAR}`` in such
an option is expanded to a host environment value and then written where a
recipient can read it: a CSV header cell, a report title, the suffix on every
truncated field.

Two separate enforcement points need that fact:

* the plugin's own config model, which rejects a raw ``${VAR}`` when it validates
  its options; and
* :func:`elspeth.core.config._reject_sensitive_plugin_env_placeholders_before_expansion`,
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

So the fact is declared ONCE, on the option field itself, and both enforcement
points DERIVE from that declaration:

.. code-block:: python

    class ReportAssembleConfig(TransformDataConfig):
        title: Annotated[
            str | None,
            EmittedToOutput("report_assemble emits this value in user-visible report output"),
        ] = Field(default=None, description="Optional report title")

The marker is metadata only. It performs no validation itself — the enforcing
validator lives on the plugin config base, and the loader guard reads the same
metadata through :func:`emitted_option_fields`.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING, get_args, get_origin

if TYPE_CHECKING:
    from pydantic import BaseModel

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
    """

    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("EmittedToOutput.reason must explain where the value surfaces; it is shown to operators")


def emitted_option_fields(model: type[BaseModel] | None) -> dict[str, str]:
    """Return ``{field_name: reason}`` for every :class:`EmittedToOutput` field.

    Reads ``model_fields`` metadata, so an inherited marker is found on the
    subclass without the subclass restating it.

    Args:
        model: A plugin config model, or ``None``. ``None`` is a normal return
            from ``get_config_model()`` for plugins that take no options (the
            ``null`` source) and means "declares nothing" — not an error.

    Returns:
        Mapping of field name to the declared reason. Empty when the model
        declares no emitted options.
    """
    if model is None:
        return {}

    declared: dict[str, str] = {}
    for field_name in model.model_fields:
        for entry in model.model_fields[field_name].metadata:
            if isinstance(entry, EmittedToOutput):
                declared[field_name] = entry.reason
                break
    return declared


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


def annotation_declares_emitted_output(annotation: object) -> bool:
    """Is this raw annotation an ``Annotated[..., EmittedToOutput(...)]``?

    For callers holding an annotation rather than a pydantic field — chiefly
    tests asserting that a declaration survives a refactor. Prefer
    :func:`emitted_option_fields` when a model is available: pydantic normalises
    inherited and unioned annotations, and this function does not.
    """
    if get_origin(annotation) is None:
        return False
    return any(isinstance(entry, EmittedToOutput) for entry in get_args(annotation)[1:])
