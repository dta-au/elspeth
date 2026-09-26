"""The one authority for a field name's canonical spelling.

A header-reading source keys every row by the NORMALIZED form of each external
header (``"Name"`` -> ``"name"``) and records the raw header as the field's
``original_name`` (``SchemaContract``). Row LOOKUPS resolve either spelling
(``PipelineRow.__getitem__`` goes through ``SchemaContract.find_name``), so
``row['Name']`` reads ``name``. A DECLARATION is different: it is a name a node
commits to before any row exists — a schema field, a required input, a column
option, a created target — and the engine compares it to row keys as written.
A declaration spelled by the original header therefore never meets the field it
means: an optional one is silently inert, a required one falsely missing, a
created one shadows the arriving field under a second key.

Operator ruling 2026-09-25 (elspeth-5887fb7928, "field-name spelling rule"):
declarations use the canonical name; a header spelling is REJECTED. The source
has done that for its own schema since elspeth-3664e213c4
(``plugins.sources.field_normalization.check_declared_fields_reachable``); this
module generalises the same test to every downstream declaration surface, as one
predicate every surface calls (the 2026-09-26 amendment's "ONE shared
predicate"):

    T is a header spelling of C  iff  T not in names, normalize(T) != T,
                                      and C = normalize(T) is in names.

Lives at L0 because four layers must ask exactly this question and none may
restate it: the DAG build validator (core), the transform, batch and sink
preflights (engine), plugin config validation (plugins) and the Web Composer's
Stage-1 mirror (web). The normalization algorithm itself lives here for the same
reason: the predicate is only as good as its agreement with the function the
sources use to derive row keys, so there is one copy of it.
``plugins.sources.field_normalization.normalize_field_name`` is the Tier-3
wrapper the sources call; it adds the external-header refusal of an empty
result.
"""

from __future__ import annotations

import keyword
import re
import unicodedata
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Literal

# Algorithm version for audit trail - frozen per major version.
# Increment when algorithm changes affect output.
NORMALIZATION_ALGORITHM_VERSION = "1.0.1"

# Pre-compiled regex patterns (module level for efficiency)
_CONSECUTIVE_UNDERSCORES = re.compile(r"_+")


def _is_identifier_continue(char: str) -> bool:
    """Return whether char can appear after a valid Python identifier start."""
    return f"_{char}".isidentifier()


def _replace_non_identifier_chars(value: str) -> str:
    """Replace characters outside Python's identifier alphabet with underscores."""
    return "".join(char if _is_identifier_continue(char) else "_" for char in value)


def normalized_field_name_or_empty(raw: str) -> str:
    """The row key a header-reading source derives from ``raw``; empty means none.

    Rules applied in order:
    1. Unicode NFC normalization (canonical composition)
    2. Strip leading/trailing whitespace
    3. Lowercase
    4. Replace non-identifier chars with underscore
    5. Collapse consecutive underscores
    6. Strip leading/trailing underscores
    7. Prefix with underscore if starts with digit
    8. Append underscore if result is Python keyword

    An empty result means no header produces a name. The sources' Tier-3 entry
    point (``normalize_field_name``) refuses that as bad external data; callers
    that need "does anything normalize to this?" as data read the empty string
    here instead of catching that refusal.

    Raises:
        ValueError: If the algorithm produces a non-empty non-identifier — a bug
            in this function, never a property of the input.
    """
    # Step 1: Unicode NFC normalization
    normalized = unicodedata.normalize("NFC", raw)

    # Step 2: Strip whitespace
    normalized = normalized.strip()

    # Step 3: Lowercase
    normalized = normalized.lower()

    # Step 4: Replace non-identifier chars with underscore. Python's \w includes
    # Unicode number symbols like superscript two that are not legal identifiers.
    normalized = _replace_non_identifier_chars(normalized)

    # Step 5: Collapse consecutive underscores
    normalized = _CONSECUTIVE_UNDERSCORES.sub("_", normalized)

    # Step 6: Strip leading/trailing underscores
    normalized = normalized.strip("_")

    # Step 7: Prefix if the first remaining char cannot start an identifier.
    if normalized and not normalized.isidentifier() and f"_{normalized}".isidentifier():
        normalized = f"_{normalized}"

    # Step 8: Handle Python keywords
    if keyword.iskeyword(normalized):
        normalized = f"{normalized}_"

    # Defense-in-depth: verify a non-empty result is a valid identifier
    if normalized and not normalized.isidentifier():
        raise ValueError(
            f"Header '{raw}' normalized to '{normalized}' which is not a valid identifier. This is a bug in the normalization algorithm."
        )

    return normalized


def header_spelling_canonical(name: str, present: Collection[str]) -> str | None:
    """The canonical field ``name`` is a header spelling of, or None.

    The shared predicate of the field-name spelling rule: ``name`` is absent from
    ``present``, it is not its own normalized form, and its normalized form IS
    present. ``present`` is whatever the caller can vouch for — a participating
    upstream's guaranteed fields at build time, an arriving row's keys at run
    time. A name that is present is never a header spelling (a row can carry
    ``B`` and ``b`` as two fields: a source ``field_mapping`` value bypasses
    normalization), and a name whose normalized form is absent may be a created
    or a merely missing field, which other checks own.
    """
    if name in present:
        return None
    canonical = normalized_field_name_or_empty(name)
    if not canonical or canonical == name:
        return None
    return canonical if canonical in present else None


@dataclass(frozen=True, slots=True)
class HeaderSpelling:
    """One declared name found to be a header spelling of a present field.

    ``literal`` is the operator's config spelling and ``canonical`` its
    normalized form. Both are derived from configuration, never from a row, so
    either may appear in audit text: the original header a row actually
    carried (``FieldContract.original_name``) is row-derived and never does.
    """

    literal: str
    canonical: str
    kind: Literal["read", "create"]


def header_spelled_names(names: Iterable[str], present: Collection[str], *, kind: Literal["read", "create"]) -> tuple[HeaderSpelling, ...]:
    """Every name in ``names`` that is a header spelling of a field in ``present``, sorted by literal."""
    found = []
    for name in sorted(set(names)):
        canonical = header_spelling_canonical(name, present)
        if canonical is not None:
            found.append(HeaderSpelling(literal=name, canonical=canonical, kind=kind))
    return tuple(found)


def header_spelled_declarations(
    *,
    reads: Iterable[str],
    creates: Iterable[str],
    present: Collection[str],
    forwarded: Collection[str],
    participated: bool,
    closed: bool,
) -> tuple[HeaderSpelling, ...]:
    """The build-time verdict for one consumer against one upstream vote.

    The single gate both the DAG validator and the Web Composer's Stage-1
    mirror call, so the two cannot disagree about when a build may refuse.
    ``present`` is the upstream's guaranteed fields; ``forwarded`` is the part
    of it the consumer carries onto its output (``present`` minus the fields the
    consumer removes, empty when it forwards nothing), which is what a created
    name can shadow.

    A READ declaration (a field the node looks up on arriving rows) is refused
    only when the upstream vote is PARTICIPATING and CLOSED. The predicate's
    first leg is an ABSENCE claim (``T`` not present), and absence needs an
    upper bound: an open upstream may carry ``Name`` as well as ``name`` (a
    source ``field_mapping`` value, an undeclared column), and then ``Name: str``
    is a correct declaration of the other field — the same reasoning
    ``validate_transform_declared_input_fields`` gives for requiring ``closed``
    (elspeth-9c5ff8fa7d).

    A CREATED name (a target the node writes) is refused whenever the vote
    participates, per the 2026-09-26 amendment's build half, on the pattern of
    ``validate_transform_output_field_collisions``. The residual this admits —
    an open upstream that also carries the header literal as a field of its
    own — is a pipeline naming two fields by spellings of one name.

    Everything the build cannot settle is enforced per row by the runtime
    residual, which applies ``header_spelling_canonical`` to the arriving row.
    """
    if not participated:
        return ()
    return _merge_spellings(
        header_spelled_names(creates, forwarded, kind="create"),
        header_spelled_names(reads, present, kind="read") if closed else (),
    )


def header_spelled_row_declarations(
    *,
    reads: Iterable[str],
    creates: Iterable[str],
    row_keys: Collection[str],
    forwarded_keys: Collection[str],
) -> tuple[HeaderSpelling, ...]:
    """The run-time verdict for one arriving row — the residual the build could not settle.

    A row's keys are exact, so there is no participation or closedness gate:
    ``row_keys`` answers both legs of the predicate. ``forwarded_keys`` is the
    part of the row the node carries onto its output (empty when it forwards
    nothing), which is what a created name can shadow.
    """
    return _merge_spellings(
        header_spelled_names(creates, forwarded_keys, kind="create"),
        header_spelled_names(reads, row_keys, kind="read"),
    )


def _merge_spellings(created: tuple[HeaderSpelling, ...], read: tuple[HeaderSpelling, ...]) -> tuple[HeaderSpelling, ...]:
    """One entry per literal, sorted; a name both read and created reports as created (the stronger claim)."""
    created_literals = {spelling.literal for spelling in created}
    merged = [*created, *(spelling for spelling in read if spelling.literal not in created_literals)]
    return tuple(sorted(merged, key=lambda spelling: spelling.literal))


def header_normalization_remedy(literal: str, canonical: str, *, header_kind: str = "headers") -> str:
    """The one wording of why a header spelling cannot be declared and what to declare instead.

    Shared with the source's own reachability check so an operator meets the
    same sentence wherever the rule fires.
    """
    return f"{header_kind} are normalized to lowercase identifiers ('{literal}' -> '{canonical}'). Declare '{canonical}'"


def describe_header_spelling(spelling: HeaderSpelling) -> str:
    """One refused spelling, in config literals and canonical names only (value-free)."""
    if spelling.kind == "read":
        return f"'{spelling.literal}' is a header spelling of '{spelling.canonical}': {header_normalization_remedy(spelling.literal, spelling.canonical)}"
    return (
        f"'{spelling.literal}' is a header spelling of the arriving field '{spelling.canonical}', so the created field "
        f"would sit beside it under a second key: name it '{spelling.canonical}' to overwrite that field, or choose a "
        f"name no arriving field normalizes to"
    )


HEADER_SPELLING_RULE = (
    "Row lookups (an expression's row['<header>'], a field_mapper source) resolve either spelling; a declaration — a schema field, a "
    "required or column-naming option, a created target — must name the field as rows carry it."
)
"""The closing sentence every refusal of the rule carries, build and run time alike."""


def describe_header_spellings(spellings: Iterable[HeaderSpelling]) -> str:
    """Render refused spellings for an error message: config literals and canonical names only."""
    return "; ".join(describe_header_spelling(spelling) for spelling in spellings)
