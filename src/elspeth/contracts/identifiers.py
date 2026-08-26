"""Shared identifier validation policy."""

from __future__ import annotations

import keyword
from collections.abc import Sequence
from typing import TypeGuard


def is_valid_field_name(name: object) -> TypeGuard[str]:
    """Whether ``name`` is ACCEPTED by the shared identifier policy.

    The single statement of the acceptance set. ``validate_field_name`` decides
    acceptance by calling this and only then chooses which rejection to word, so
    the two can never disagree about which names are field names — and callers
    that need the answer as a BOOLEAN (a heuristic gate that must abstain rather
    than raise) ask this instead of hand-restating ``isidentifier()`` +
    ``iskeyword()``. That restatement is what this exists to remove: it read
    identically at every site until the policy moved, and then it did not.

    Deliberately ``type(name) is str``, matching ``validate_field_name``'s own
    type gate rather than ``isinstance``: a ``str`` SUBCLASS is not accepted by
    the raising path, so a predicate that accepted one would reintroduce the
    disagreement in miniature.

    Empty is rejected here with no ``allow_empty`` escape. That option belongs
    to the raising path, which is answering "may this CALLER omit the name?" —
    a different question from "is this a field name?", and one no gate asks.

    ``TypeGuard[str]`` rather than ``bool`` so the positive branch still narrows
    for mypy, which the ``isinstance`` half of the removed restatement was
    silently providing at both gate sites. Deliberately NOT ``TypeIs`` (PEP
    742): ``TypeIs`` narrows the NEGATIVE branch too, and a rejected name is
    very often still a ``str`` — every keyword is — so it would be a false
    claim about the else-branch.
    """
    return type(name) is str and name.isidentifier() and not keyword.iskeyword(name)


def validate_field_name(
    name: object,
    context: str,
    *,
    strip: bool = False,
    allow_empty: bool = False,
    invalid_identifier_message: str | None = None,
) -> str:
    """Validate and return one field name under the shared identifier policy."""
    if type(name) is not str:
        raise ValueError(f"{context} must be a string, got {type(name).__name__}")

    value = name.strip() if strip else name
    if not value:
        if allow_empty:
            return value
        if invalid_identifier_message is not None:
            raise ValueError(invalid_identifier_message)
        if strip:
            raise ValueError(f"{context} cannot be empty or whitespace-only")
        raise ValueError(f"{context} '{value}' is not a valid Python identifier")
    if is_valid_field_name(value):
        return value
    # ACCEPTANCE is settled above; the branches below only choose the wording of
    # a rejection already decided. Keyword first because it is the narrower
    # reason and carries its own message: every keyword IS an identifier, so the
    # two conditions never both apply and the order cannot change a verdict.
    if keyword.iskeyword(value):
        raise ValueError(f"{context} '{value}' is a Python keyword")
    raise ValueError(invalid_identifier_message or f"{context} '{value}' is not a valid Python identifier")


def validate_field_names(
    names: Sequence[object],
    context: str,
    *,
    strip: bool = False,
    allow_empty_sequence: bool = True,
    allow_duplicates: bool = False,
) -> tuple[str, ...]:
    """Validate and return field names under the shared identifier policy."""
    if isinstance(names, (str, bytes)) or not isinstance(names, Sequence):
        raise ValueError(f"{context} must be a sequence of field names, got {type(names).__name__}")
    if not names and not allow_empty_sequence:
        raise ValueError(f"{context} must not be empty")

    result = tuple(validate_field_name(name, f"{context}[{i}]", strip=strip) for i, name in enumerate(names))

    if not allow_duplicates:
        seen: set[str] = set()
        for name in result:
            if name in seen:
                raise ValueError(f"Duplicate field names in {context}: {name}")
            seen.add(name)
    return result


__all__ = [
    "is_valid_field_name",
    "validate_field_name",
    "validate_field_names",
]
