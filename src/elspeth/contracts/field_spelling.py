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

    T is a header spelling of C  iff  T not in names, C = resolve(T) != T,
                                      and C is in names,

where ``resolve`` is the UPSTREAM's own name resolution (``FieldNameResolution``):
what a lookup of ``T`` reads on the arriving row (``SchemaContract.find_name``,
at run time), what the source's ``field_mapping`` renames ``normalize(T)`` to
(``resolve_field_names``' rule, at build time), and ``normalize(T)`` itself.
Comparing only ``normalize(T)`` missed a header the source renames: under
``field_mapping: {name: b}`` a lookup of ``Name`` reads ``b`` while a
declaration ``Name: int?`` met no field, so a str was delivered under a
recorded ``int`` (Codex final review, finding 1).

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
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from elspeth.contracts.freeze import freeze_fields

if TYPE_CHECKING:
    # Type-only: schema_contract imports contracts.errors, which imports this
    # module, so a runtime import would be a cycle. Only ``find_name`` is called.
    from elspeth.contracts.schema_contract import SchemaContract

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


SpellingLeg = Literal["recorded", "renamed_as_written", "renamed", "normalized"]
"""Which leg of the upstream's resolution named the canonical field (``FieldNameResolution.resolve``).

``recorded``: the arriving row's contract resolves the literal (it is the
original name recorded for that field — a lookup ``row[literal]`` reads it).
``renamed_as_written``: a headerless source's ``field_mapping`` renames the
literal itself, a column name the source keys as written.
``renamed``: a headered source's ``field_mapping`` renames the literal's
normalized form.
``normalized``: the literal's normalized form is the field itself.
"""

FieldMappingKeys = Literal["normalized", "as_written"]
"""What a source matches its ``field_mapping`` keys against (``resolve_field_names``).

``normalized``: the normalized form of each external name (a header row, JSON
object keys, Dataverse attributes). ``as_written``: the configured column names
verbatim (headerless CSV: explicit ``columns``, or the schema's field names).
"""


@dataclass(frozen=True, slots=True)
class SourceFieldRenames:
    """The renames a source applies (``SourceProtocol.field_renames``), keyed the way the source keys them.

    ``mapping`` is the source's validated ``field_mapping`` (key -> row key),
    ``keys`` what those keys are matched against. A source keys a row by
    ``mapping.get(k, k)`` where ``k`` is ``normalize(h)`` for a header ``h``
    under ``normalized`` and the column name itself under ``as_written``, so a
    declared literal names a rename target exactly when its ``k`` is a key.
    """

    mapping: Mapping[str, str]
    keys: FieldMappingKeys

    def __post_init__(self) -> None:
        freeze_fields(self, "mapping")


NO_SOURCE_RENAMES = SourceFieldRenames(mapping={}, keys="normalized")
"""A source that renames nothing: every name it emits resolves by normalization alone."""


@dataclass(frozen=True, slots=True)
class DeclaredName:
    """One declared name, reduced once: the config literal and its normalized form ("" when it has none).

    Both are pure functions of configuration, so a node computes them once and
    every row pays only the membership tests.
    """

    literal: str
    normalized: str

    @classmethod
    def of(cls, literal: str) -> DeclaredName:
        return cls(literal=literal, normalized=normalized_field_name_or_empty(literal))


def _declared(names: Iterable[str]) -> tuple[DeclaredName, ...]:
    """Every declared name once, sorted by literal."""
    return tuple(DeclaredName.of(name) for name in sorted(set(names)))


@dataclass(frozen=True, slots=True)
class FieldNameResolution:
    """How an upstream turns a spelling into the name its rows carry — the resolution the predicate asks.

    A declaration and a lookup of the same literal must name the same field, so
    the predicate resolves a declared name the way the upstream resolves it,
    never by a rule of its own:

    - ``recorded`` (run time): the arriving row's ``SchemaContract``. Its
      ``find_name`` is the resolution ``PipelineRow`` lookups use, so what it
      returns is exactly the field ``row[literal]`` reads.
    - ``renames_as_written`` / ``renames`` (build time): each upstream source's
      ``field_mapping`` (``SourceProtocol.field_renames``), split by what the
      source matches its keys against. A source keys a row by
      ``field_mapping.get(k, k)`` (``resolve_field_names``) where ``k`` is the
      configured column name verbatim for a headerless source and
      ``normalize(h)`` for a headered one, so a literal a headerless source
      renames is looked up as written and a header spelled ``literal`` by its
      normalized form. Several sources may rename one key differently; each
      target is a candidate.
    - ``normalize(literal)`` always — the header-spelling rule itself. A
      headered source keys an unmapped header by it; a headerless source emits
      its unmapped columns as written, so behind one this leg can only fire on
      a literal that normalizes to a field the upstream carries, which the rule
      refuses as a spelling of that field whatever the source's mode.

    The build has no row contract and the run time does not re-derive a
    source's renames: a recorded original name that a downstream transform
    carried onto a renamed field is not a rename any code applies to
    ``normalize(original)``, so reading one back out of a contract would name
    fields no lookup reaches.
    """

    renames_as_written: Mapping[str, tuple[str, ...]]
    renames: Mapping[str, tuple[str, ...]]
    recorded: SchemaContract | None

    def __post_init__(self) -> None:
        freeze_fields(self, "renames_as_written", "renames")

    @classmethod
    def of_source_renames(cls, sources: Iterable[SourceFieldRenames]) -> FieldNameResolution:
        """The build-time resolution over the ``field_renames`` of every source whose rows can reach the node."""
        targets: dict[FieldMappingKeys, dict[str, set[str]]] = {"as_written": {}, "normalized": {}}
        for source in sources:
            keyed = targets[source.keys]
            for key, target in source.mapping.items():
                if key not in keyed:
                    keyed[key] = set()
                keyed[key].add(target)
        return cls(
            renames_as_written={key: tuple(sorted(found)) for key, found in targets["as_written"].items()},
            renames={key: tuple(sorted(found)) for key, found in targets["normalized"].items()},
            recorded=None,
        )

    @classmethod
    def of_contract(cls, contract: SchemaContract) -> FieldNameResolution:
        """The run-time resolution: the arriving row's own contract."""
        return cls(renames_as_written=NORMALIZATION_ONLY.renames_as_written, renames=NORMALIZATION_ONLY.renames, recorded=contract)

    def resolve(self, name: DeclaredName) -> Iterator[tuple[str, SpellingLeg]]:
        """Every field ``name`` can name upstream, strongest leg first."""
        if self.recorded is not None:
            found = self.recorded.find_name(name.literal)
            if found is not None:
                yield found, "recorded"
        if name.literal in self.renames_as_written:
            for target in self.renames_as_written[name.literal]:
                yield target, "renamed_as_written"
        if name.normalized:
            if name.normalized in self.renames:
                for target in self.renames[name.normalized]:
                    yield target, "renamed"
            yield name.normalized, "normalized"


NORMALIZATION_ONLY = FieldNameResolution(renames_as_written={}, renames={}, recorded=None)
"""The resolution with no upstream rename and no row contract: ``normalize(T)`` alone.

For names checked against names the SAME node creates (value_transform's targets
against one another), where no upstream resolution is involved.
"""


@dataclass(frozen=True, slots=True)
class HeaderSpelling:
    """A declared name that names, through the upstream's resolution, a field it does not spell.

    ``literal`` is the operator's config spelling, ``canonical`` the field the
    upstream resolves it to, ``leg`` which resolution named it. Both names are
    configuration: the literal is authored, and the canonical is what config
    makes of it (its normalized form, a ``field_mapping`` target, or the name a
    contract records for it — itself a normalized header or a configured
    target). So either may appear in audit text: a header a row carried that
    config never spelled is row-derived and never does.
    """

    literal: str
    canonical: str
    kind: Literal["read", "create"]
    leg: SpellingLeg


def _spelled(
    declared: tuple[DeclaredName, ...],
    present: Collection[str],
    resolution: FieldNameResolution,
    *,
    kind: Literal["read", "create"],
) -> tuple[HeaderSpelling, ...]:
    """The predicate: the literal absent from ``present``, a field it resolves to (not itself) in it."""
    found = []
    for name in declared:
        if name.literal in present:
            continue
        for canonical, leg in resolution.resolve(name):
            if canonical != name.literal and canonical in present:
                found.append(HeaderSpelling(literal=name.literal, canonical=canonical, kind=kind, leg=leg))
                break
    return tuple(found)


def header_spelling_canonical(name: str, present: Collection[str]) -> str | None:
    """The canonical field ``name`` is a header spelling of among names one node creates itself, or None.

    Normalization only (``NORMALIZATION_ONLY``): the names in ``present`` are
    this node's own, so no upstream resolution applies. A name that is present
    is never a header spelling (a row can carry ``B`` and ``b`` as two fields:
    a source ``field_mapping`` value bypasses normalization).
    """
    spelled = _spelled(_declared((name,)), present, NORMALIZATION_ONLY, kind="create")
    return spelled[0].canonical if spelled else None


def header_spelled_names(
    names: Iterable[str],
    present: Collection[str],
    resolution: FieldNameResolution,
    *,
    kind: Literal["read", "create"],
) -> tuple[HeaderSpelling, ...]:
    """Every name in ``names`` that is a header spelling of a field in ``present``, sorted by literal."""
    return _spelled(_declared(names), present, resolution, kind=kind)


def header_spelled_declarations(
    *,
    spellings: DeclaredSpellings,
    present: Collection[str],
    forwarded: Collection[str],
    participated: bool,
    closed: bool,
    resolution: FieldNameResolution,
) -> tuple[HeaderSpelling, ...]:
    """The build-time verdict for one consumer against one upstream vote.

    The single gate both the DAG validator and the Web Composer's Stage-1
    mirror call, so the two cannot disagree about when a build may refuse.
    ``spellings`` is the consumer's declarations (``DeclaredSpellings.of``); a
    caller whose consumer declares nothing need not resolve an upstream at all.
    ``present`` is the upstream's guaranteed fields; ``forwarded`` is the part
    of it the consumer carries onto its output (``present`` minus the fields
    the consumer removes, empty when it forwards nothing), which is what a
    created name can shadow. ``resolution`` is the renames of every source whose
    rows reach the consumer (``FieldNameResolution.of_source_renames``).

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
    residual (``DeclaredSpellings.in_row``).
    """
    if not participated:
        return ()
    return _merge_spellings(
        _spelled(spellings.creates, forwarded, resolution, kind="create"),
        _spelled(spellings.reads, present, resolution, kind="read") if closed else (),
    )


@dataclass(frozen=True, slots=True)
class DeclaredSpellings:
    """A node's declarations, each reduced ONCE to its literal and normalized form.

    Every declaration is kept, canonical ones included: an upstream's rename
    makes a normalization fixed point a header spelling too (under
    ``field_mapping: {name: b}`` the declaration ``name`` names ``b``), so no
    declaration can be dropped from config alone. What a row pays is bounded
    instead: a declared read the row carries, and a created name it forwards
    as written, are settled by set tests; only a read the row lacks or a name
    new to the row is resolved (``in_row``).
    """

    reads: tuple[DeclaredName, ...]
    creates: tuple[DeclaredName, ...]

    @classmethod
    def of(cls, *, reads: Iterable[str], creates: Iterable[str]) -> DeclaredSpellings:
        return cls(reads=_declared(reads), creates=_declared(creates))

    @property
    def is_empty(self) -> bool:
        """True for a node that declares no name at all: no row and no upstream can flag one."""
        return not self.reads and not self.creates

    def in_row(
        self,
        *,
        row_keys: Collection[str],
        forwarded_keys: Collection[str],
        contract: SchemaContract,
    ) -> tuple[HeaderSpelling, ...]:
        """The run-time verdict for one arriving row — the residual the build could not settle.

        A row's keys are exact, so there is no participation or closedness
        gate: ``row_keys`` answers both legs for a read. ``forwarded_keys`` is
        the part of the row the node carries onto its output (empty when it
        forwards nothing), which is what a created name can shadow.
        ``contract`` is the row's own ``SchemaContract``, the resolution its
        lookups use (``FieldNameResolution.of_contract``).
        """
        # A declared read the row carries, and a created name the node forwards
        # as written (an overwrite), are settled by set tests; only a read the
        # row lacks or a name new to the row is resolved, through the row's
        # contract. A node declaring only reads its rows carry pays the set
        # tests alone; one that creates a new field resolves that name per row
        # (two dict lookups).
        missing_reads = tuple(name for name in self.reads if name.literal not in row_keys)
        new_creates = tuple(name for name in self.creates if name.literal not in forwarded_keys)
        if not missing_reads and not new_creates:
            return ()
        resolution = FieldNameResolution.of_contract(contract)
        return _merge_spellings(
            _spelled(new_creates, forwarded_keys, resolution, kind="create"),
            _spelled(missing_reads, row_keys, resolution, kind="read"),
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


def _read_remedy(spelling: HeaderSpelling) -> str:
    """Why a read literal names the canonical field, for the leg that fired, and what to declare instead."""
    normalized = normalized_field_name_or_empty(spelling.literal)
    if spelling.leg == "renamed_as_written":
        return (
            f"the source's field_mapping renames its column '{spelling.literal}' to '{spelling.canonical}'. Declare '{spelling.canonical}'"
        )
    if spelling.leg == "renamed":
        rename = f"the source's field_mapping renames '{normalized}' to '{spelling.canonical}'"
        if normalized != spelling.literal:
            rename = f"headers are normalized to lowercase identifiers ('{spelling.literal}' -> '{normalized}') and {rename}"
        return f"{rename}. Declare '{spelling.canonical}'"
    if spelling.leg == "recorded" and normalized != spelling.canonical:
        return f"rows carry the field it names as '{spelling.canonical}' (a lookup of '{spelling.literal}' reads that field). Declare '{spelling.canonical}'"
    return header_normalization_remedy(spelling.literal, spelling.canonical)


def describe_header_spelling(spelling: HeaderSpelling) -> str:
    """One refused spelling, in config literals and canonical names only (value-free)."""
    if spelling.kind == "read":
        return f"'{spelling.literal}' is a header spelling of '{spelling.canonical}': {_read_remedy(spelling)}"
    return (
        f"'{spelling.literal}' is a header spelling of the arriving field '{spelling.canonical}', so the created field "
        f"would sit beside it under a second key: name it '{spelling.canonical}' to overwrite that field, or choose a "
        f"name no arriving field normalizes to"
    )


HEADER_SPELLING_RULE = (
    "Row lookups (an expression's row['<header>'], a field_mapper source, an option that only locates a field) resolve "
    "either spelling; a declaration — a schema field, required_input_fields, an input option the plugin declares, a "
    "created name — must name the field as rows carry it."
)
"""The closing sentence every refusal of the rule carries, build and run time alike."""


def describe_header_spellings(spellings: Iterable[HeaderSpelling]) -> str:
    """Render refused spellings for an error message: config literals and canonical names only."""
    return "; ".join(describe_header_spelling(spelling) for spelling in spellings)
