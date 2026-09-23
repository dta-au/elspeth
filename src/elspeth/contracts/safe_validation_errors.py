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

- ``loc[0]``, the top-level field: a model validation names a declared field
  (or its alias) there. The one built-in error that names an UNDECLARED row
  key at that position is ``extra_forbidden``; it renders ``[undeclared]``.
  A non-string ``loc[0]`` renders ``[item]``. (A model that declares typed
  extras, ``__pydantic_extra__: dict[str, X]``, reports an undeclared key
  there under an ordinary type code; that is still a top-level field NAME,
  which the run's schema contract records anyway, never a field value.)
- every deeper ``loc`` element renders ``[item]``: without the model it
  cannot be proved to be a declared name or a list index rather than a row
  key, so none is rendered.
- the error ``type`` code, only when it is one of pydantic-core's own error
  types; anything else renders as ``custom``.
- ``msg`` is never rendered. The type code carries the meaning.

The engine's own contract checks validate ROW data against a plugin's declared
schema and put the result in a ``PluginContractViolation`` message that becomes
a routed reason, so they need the same rendering. It lives in ``contracts`` so
both layers call this one function (the engine may not import ``plugins``).
"""

from __future__ import annotations

from typing import get_args

from pydantic import ValidationError
from pydantic_core.core_schema import ErrorType

_BUILTIN_ERROR_TYPES: frozenset[str] = frozenset(get_args(ErrorType))
_UNDECLARED_FIELD = "[undeclared]"
_NESTED = "[item]"
_CUSTOM_TYPE = "custom"


def _render_location(loc: tuple[int | str, ...], error_type: str) -> str:
    if not loc:
        return "<root>"
    head = loc[0]
    if not isinstance(head, str):
        rendered_head = _NESTED
    elif error_type == "extra_forbidden":
        rendered_head = _UNDECLARED_FIELD
    else:
        rendered_head = head
    return ".".join([rendered_head, *(_NESTED for _ in loc[1:])])


def safe_validation_error_text(exc: ValidationError) -> str:
    """Render a ``ValidationError`` from its field locations and type codes only.

    Example: ``2 validation errors: amount: [int_parsing]; tags.[item]: [string_type]``.
    """
    details = exc.errors(include_input=False, include_context=False, include_url=False)
    parts = []
    for detail in details:
        error_type = detail["type"]
        rendered_type = error_type if error_type in _BUILTIN_ERROR_TYPES else _CUSTOM_TYPE
        parts.append(f"{_render_location(detail['loc'], error_type)}: [{rendered_type}]")
    noun = "error" if len(details) == 1 else "errors"
    return f"{len(details)} validation {noun}: " + "; ".join(parts)
