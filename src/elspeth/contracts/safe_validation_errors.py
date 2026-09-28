"""Value-free rendering of Pydantic validation failures (elspeth-a300402c58, elspeth-5887fb7928).

``str(ValidationError)`` echoes the offending INPUT VALUE by default. Source
plugins sit on the Tier-3 boundary and their quarantine error text lands
verbatim in ``node_states.error_json``, the DIVERT routing reason, and audit
exports — surfaces the input-data hashing discipline deliberately keeps raw
payloads out of. The full raw row still travels on ``SourceRow.row`` to the
designated quarantine sink by design.

Dropping ``input`` is not enough. Pydantic's ``msg`` is free text a custom
validator writes (``ValueError(f"bad customer {v}")`` puts the value in it),
``loc`` names a dict KEY taken from the row for ``dict[str, X]`` fields, and a
``PydanticCustomError`` chooses its own ``type`` string. So the rendering is
built only from parts that cannot carry row content:

- ``loc[0]`` is rendered only when the schema that was validated DECLARES it:
  a field name, or an alias the schema declares for a field (``alias``, or
  ``validation_alias`` as a string, an ``AliasChoices`` member or the head of
  an ``AliasPath``). Anything else at that position was chosen by whoever
  produced the row: an ``extra_forbidden`` key, a typed-extras key
  (``__pydantic_extra__: dict[str, X]``), or a dict key a ``model_validator``
  put there by re-raising an inner ``ValidationError``. It renders
  ``[undeclared]``. The ``ValidationError`` carries no reference to its model,
  which is why the caller passes the schema class it validated.
- every deeper ``loc`` element renders ``[item]``: pydantic reports nested
  model fields, list indexes and a row's own dict keys (str or int) in the same
  positions, so none is rendered.
- the error ``type`` code, only when it is one of pydantic-core's own error
  types or one of ELSPETH's own closed codes (``ELSPETH_ERROR_TYPES``: a
  fixed string an ELSPETH validator raises, e.g. ``non_canonical_number``);
  anything else renders as ``custom``.
- ``msg`` is never rendered. The type code carries the meaning.

The engine's own contract checks validate ROW data against a plugin's declared
schema and put the result in a ``PluginContractViolation`` message that becomes
a routed reason, so they need the same rendering. It lives in ``contracts`` so
both layers call this one function (the engine may not import ``plugins``).
"""

from __future__ import annotations

from typing import Final, get_args

from pydantic import AliasChoices, AliasPath, ValidationError
from pydantic_core.core_schema import ErrorType

from elspeth.contracts.data import PluginSchema

_BUILTIN_ERROR_TYPES: frozenset[str] = frozenset(get_args(ErrorType))

# The source-boundary schema's rule: a number canonical JSON refuses by value
# (NaN/Infinity, an integer beyond ±(2**53-1)). Raised as this fixed
# PydanticCustomError type so the rendered reason names the rule.
NON_CANONICAL_NUMBER_ERROR_TYPE: Final = "non_canonical_number"
# ELSPETH's own closed error codes: fixed strings, never built from data, so
# printing one cannot carry row content.
ELSPETH_ERROR_TYPES: frozenset[str] = frozenset({NON_CANONICAL_NUMBER_ERROR_TYPE})
_UNDECLARED_FIELD = "[undeclared]"
_NESTED = "[item]"
_CUSTOM_TYPE = "custom"


def _alias_heads(alias: str | AliasPath | AliasChoices | None) -> set[str]:
    """The top-level keys a field alias can put at ``loc[0]``."""
    if alias is None:
        return set()
    if isinstance(alias, str):
        return {alias}
    if isinstance(alias, AliasPath):
        head = alias.path[0]
        return {head} if isinstance(head, str) else set()
    heads: set[str] = set()
    for choice in alias.choices:
        heads |= _alias_heads(choice)
    return heads


def _declared_names(schema: type[PluginSchema]) -> frozenset[str]:
    """Every top-level name the schema declares: field names and their aliases."""
    names: set[str] = set()
    for field_name, field in schema.model_fields.items():
        names.add(field_name)
        names |= _alias_heads(field.alias)
        names |= _alias_heads(field.validation_alias)
    return frozenset(names)


def _render_location(loc: tuple[int | str, ...], declared: frozenset[str]) -> str:
    if not loc:
        return "<root>"
    head = loc[0]
    rendered_head = head if isinstance(head, str) and head in declared else _UNDECLARED_FIELD
    return ".".join([rendered_head, *(_NESTED for _ in loc[1:])])


def safe_validation_error_text(exc: ValidationError, schema: type[PluginSchema]) -> str:
    """Render a ``ValidationError`` raised by ``schema`` from declared names and type codes only.

    ``schema`` is the class whose ``model_validate`` raised ``exc``; it decides
    which top-level names are declared and may be printed.

    Example: ``2 validation errors: amount: [int_parsing]; tags.[item]: [string_type]``.
    """
    declared = _declared_names(schema)
    details = exc.errors(include_input=False, include_context=False, include_url=False)
    parts = []
    for detail in details:
        error_type = detail["type"]
        rendered_type = error_type if error_type in _BUILTIN_ERROR_TYPES or error_type in ELSPETH_ERROR_TYPES else _CUSTOM_TYPE
        parts.append(f"{_render_location(detail['loc'], declared)}: [{rendered_type}]")
    noun = "error" if len(details) == 1 else "errors"
    return f"{len(details)} validation {noun}: " + "; ".join(parts)
