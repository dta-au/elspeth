"""Recipe scaffolding for the composer. Tier-3 boundary; see CLAUDE.md.

Slot validation runs *before* scaffolding, so a wrong-shape slot value (e.g., a
URL passed where ``blob_id`` is required) is rejected at the recipe boundary
rather than silently producing a config that fails at runtime — operators get a
diagnostic that points at the recipe input, not at a downstream plugin.

Boundary contract: recipes never read blob bytes. Resolving and inspecting blob
content lives in ``source_inspection.py``; recipes only manipulate the typed
slot values they were given.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal, final
from uuid import UUID

from elspeth.contracts.composer_slots import SlotSpec
from elspeth.contracts.composer_slots import SlotType as SlotType
from elspeth.contracts.freeze import freeze_fields
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId


@dataclass(frozen=True, slots=True)
class InlineRecipeBlob:
    """Literal request-owned bytes to settle through normal planner custody."""

    filename: str
    mime_type: str
    content: str


@dataclass(frozen=True, slots=True)
class RecipeReviewedSourceSummary:
    """Surface-neutral public facts for one reviewed source."""

    name: str
    plugin: str
    field_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecipeReviewedOutputSummary:
    """Surface-neutral public facts for one reviewed output."""

    name: str
    plugin: str
    required_fields: tuple[str, ...]


@final
class ReviewedOutputProjectionConflict(Exception):
    """Closed conflict between an exact projection and reviewed fields."""

    error_code: Literal["reviewed_output_projection_conflict"] = "reviewed_output_projection_conflict"

    def __init__(self, missing_fields: tuple[str, ...]) -> None:
        if type(missing_fields) is not tuple:
            raise TypeError("ReviewedOutputProjectionConflict.missing_fields must be an exact tuple")
        if not missing_fields:
            raise ValueError("ReviewedOutputProjectionConflict.missing_fields must not be empty")
        if any(type(field) is not str or not field for field in missing_fields):
            raise TypeError("ReviewedOutputProjectionConflict.missing_fields must contain non-empty exact strings")
        if len(set(missing_fields)) != len(missing_fields):
            raise ValueError("ReviewedOutputProjectionConflict.missing_fields must be unique")
        super().__init__("an exact retained-field projection omits reviewed output fields")
        self.missing_fields = missing_fields


def reviewed_output_projection_conflict(
    *,
    retained_fields: tuple[str, ...],
    required_fields: tuple[str, ...],
) -> ReviewedOutputProjectionConflict | None:
    """Return the ordered missing-field conflict for one exact projection.

    The caller owns the association between this projection and this reviewed
    output. Compatibility is set inclusion only: projection order and
    additional retained fields are permitted, and this helper never mutates
    either side of the reviewed contract.
    """

    if type(retained_fields) is not tuple or type(required_fields) is not tuple:
        raise TypeError("projection field sets must be exact tuples")
    if any(type(field) is not str or not field for field in (*retained_fields, *required_fields)):
        raise TypeError("projection field sets must contain non-empty exact strings")
    retained = set(retained_fields)
    missing = tuple(dict.fromkeys(field for field in required_fields if field not in retained))
    if not missing:
        return None
    return ReviewedOutputProjectionConflict(missing)


@dataclass(frozen=True, slots=True)
class RecipeSourceBinding:
    """Exact server-owned source identity available to a recipe matcher."""

    name: str
    plugin: str
    blob_id: str


@dataclass(frozen=True, slots=True)
class RecipeOutputBinding:
    """Exact server-owned output destination available to a matcher."""

    name: str
    plugin: str
    path: str
    format: str


@dataclass(frozen=True, slots=True)
class RecipeIntentContext:
    """Facts a registry matcher may use without depending on a UI surface."""

    user_text: str
    reviewed_sources: tuple[RecipeReviewedSourceSummary, ...] = ()
    reviewed_outputs: tuple[RecipeReviewedOutputSummary, ...] = ()
    policy_snapshot: PluginAvailabilitySnapshot | None = None
    source_bindings: tuple[RecipeSourceBinding, ...] = ()
    output_bindings: tuple[RecipeOutputBinding, ...] = ()

    def __post_init__(self) -> None:
        if type(self.user_text) is not str:
            raise TypeError("RecipeIntentContext.user_text must be an exact str")
        exact_sequences = (
            (self.reviewed_sources, RecipeReviewedSourceSummary, "reviewed_sources"),
            (self.reviewed_outputs, RecipeReviewedOutputSummary, "reviewed_outputs"),
            (self.source_bindings, RecipeSourceBinding, "source_bindings"),
            (self.output_bindings, RecipeOutputBinding, "output_bindings"),
        )
        for values, expected_type, field_name in exact_sequences:
            if type(values) is not tuple or any(type(value) is not expected_type for value in values):
                raise TypeError(f"RecipeIntentContext.{field_name} must be an exact tuple of {expected_type.__name__}")
        if self.policy_snapshot is not None and type(self.policy_snapshot) is not PluginAvailabilitySnapshot:
            raise TypeError("RecipeIntentContext.policy_snapshot must be an exact PluginAvailabilitySnapshot or None")


@dataclass(frozen=True, slots=True)
class RecipeIntentCandidate:
    """One recipe-owned sparse slot extraction before registry arbitration."""

    slots: Mapping[str, object]
    inline_blob: InlineRecipeBlob | None = None

    def __post_init__(self) -> None:
        freeze_fields(self, "slots")


@dataclass(frozen=True, slots=True)
class RecipeIntentMatch:
    """The unique complete executable registry match for one request."""

    recipe_name: str
    slots: Mapping[str, object]
    inline_blob: InlineRecipeBlob | None = None

    def __post_init__(self) -> None:
        freeze_fields(self, "slots")


type RecipeIntentMatcher = Callable[[RecipeIntentContext], RecipeIntentCandidate | None]


@dataclass(frozen=True, slots=True)
class RecipeSpec:
    """Declares a recipe — its slot schema and the scaffold function."""

    name: str
    description: str
    slots: Mapping[str, SlotSpec]
    build: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    """Pure function: validated slots → set_pipeline-compatible args dict."""
    required_plugins: frozenset[PluginId] = frozenset()
    alternative_plugin_groups: tuple[frozenset[PluginId], ...] = ()
    matcher: RecipeIntentMatcher | None = None

    def __post_init__(self) -> None:
        # ``frozen=True`` only blocks attribute reassignment; the underlying
        # mapping is mutable through the attribute reference. ``freeze_fields``
        # converts the dict to a MappingProxyType (recursively, including the
        # SlotSpec values which are themselves frozen) so registry consumers
        # cannot mutate a recipe's slot table after construction.
        freeze_fields(self, "slots")


class RecipeValidationError(ValueError):
    """Raised when operator-supplied slots fail validation."""


def _coerce_slot(name: str, spec: SlotSpec, raw: Any) -> Any:
    """Validate and coerce one slot value against its declared type."""
    if spec.slot_type == "blob_id":
        if not isinstance(raw, str):
            raise RecipeValidationError(
                f"slot '{name}' must be a UUID string for a session blob "
                f"(got type {type(raw).__name__}). To use a URL, first call "
                "create_blob with mime_type='text/plain' to wrap it."
            )
        try:
            UUID(raw)
        except ValueError as exc:
            raise RecipeValidationError(
                f"slot '{name}' must be a valid UUID (got {raw!r}). To use a "
                "URL, first call create_blob with mime_type='text/plain' to "
                "wrap it; the returned blob_id is what this slot accepts."
            ) from exc
        return raw

    if spec.slot_type == "str":
        if not isinstance(raw, str):
            raise RecipeValidationError(f"slot '{name}' must be a string (got type {type(raw).__name__})")
        return raw

    if spec.slot_type == "float":
        if isinstance(raw, bool):
            raise RecipeValidationError(f"slot '{name}' must be a number (got bool — use 0.0 or 1.0 explicitly)")
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return float(raw)
            except ValueError as exc:
                raise RecipeValidationError(f"slot '{name}' must be a number; could not coerce {raw!r} to float") from exc
        raise RecipeValidationError(f"slot '{name}' must be a number (got type {type(raw).__name__})")

    if spec.slot_type == "int":
        if isinstance(raw, bool):
            raise RecipeValidationError(f"slot '{name}' must be an integer (got bool — use 0 or 1 explicitly)")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            try:
                return int(raw)
            except ValueError as exc:
                raise RecipeValidationError(f"slot '{name}' must be an integer; could not coerce {raw!r}") from exc
        raise RecipeValidationError(f"slot '{name}' must be an integer (got type {type(raw).__name__})")

    if spec.slot_type == "str_list":
        # Operator-supplied list of strings. Accept only a list/tuple of
        # str entries — no string-splitting, because a single comma-separated
        # value would be ambiguous (is "a,b" one field or two?). The slot
        # caller is the LLM agent, which can construct lists natively.
        #
        # Returns a tuple, not a list: the coerced value may end up in a
        # ``SlotSpec.default`` on a ``frozen=True`` dataclass, where a
        # mutable list would silently bypass the frozen contract. Recipes
        # that need a list rebind via ``list(...)`` at the build-function
        # boundary (see _build_classify_recipe).
        if not isinstance(raw, (list, tuple)):
            raise RecipeValidationError(f"slot '{name}' must be a JSON array of strings (got type {type(raw).__name__})")
        items: list[str] = []
        for index, item in enumerate(raw):
            if not isinstance(item, str):
                raise RecipeValidationError(f"slot '{name}'[{index}] must be a string (got type {type(item).__name__})")
            items.append(item)
        return tuple(items)

    raise RecipeValidationError(f"recipe slot type {spec.slot_type!r} is not implemented")


def validate_slots(recipe: RecipeSpec, raw_slots: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a raw slots dict against a recipe's declared schema.

    Returns a new dict containing only the recipe's declared slots,
    coerced to their declared types. Raises ``RecipeValidationError``
    on missing required slots, unknown slot names, or type-coercion
    failures.
    """
    unknown = set(raw_slots) - set(recipe.slots)
    if unknown:
        raise RecipeValidationError(f"recipe '{recipe.name}' does not accept slot(s): {sorted(unknown)}. Accepted: {sorted(recipe.slots)}.")
    coerced: dict[str, Any] = {}
    for slot_name, spec in recipe.slots.items():
        if slot_name in raw_slots:
            coerced[slot_name] = _coerce_slot(slot_name, spec, raw_slots[slot_name])
        elif spec.required:
            raise RecipeValidationError(
                f"recipe '{recipe.name}' is missing required slot '{slot_name}': {spec.description or spec.slot_type}"
            )
        else:
            coerced[slot_name] = spec.default
    return coerced


# ---------------------------------------------------------------------------
# Recipe 1: classify-rows-llm-jsonl
#
#   csv source (blob)  →  llm transform (response stored in label_field)
#                       →  jsonl sink (single output)
# ---------------------------------------------------------------------------


_RECIPE1_SLOTS: Final[dict[str, SlotSpec]] = {
    "source_blob_id": SlotSpec(
        slot_type="blob_id",
        description="UUID of the operator-supplied CSV blob (use create_blob to wrap inline content first)",
    ),
    "classifier_template": SlotSpec(
        slot_type="str",
        description="Jinja2 template for the LLM prompt; reference row fields as {{ row['col'] }}",
    ),
    "profile": SlotSpec(
        slot_type="str",
        description="Opaque operator-approved LLM profile alias from the public llm schema",
    ),
    "label_field": SlotSpec(
        slot_type="str",
        required=False,
        default="classification",
        description="Row field name where the LLM response is written",
    ),
    "required_input_fields": SlotSpec(
        slot_type="str_list",
        required=False,
        default=(),
        description=(
            "Row field names the classifier_template depends on. The LLMConfig "
            "validator demands an explicit list when the template references "
            "row.* — pass the field names you reference in classifier_template, "
            "or accept the recipe default (empty list) which is the "
            "documented opt-out ('accept runtime risk') and refine later via "
            "patch_node_options."
        ),
    ),
    "output_path": SlotSpec(
        slot_type="str",
        required=False,
        default="outputs/classified.jsonl",
        description="JSONL output path",
    ),
}


def _build_classify_recipe(slots: Mapping[str, Any]) -> dict[str, Any]:
    """Build set_pipeline args for the classify-rows-llm-jsonl recipe."""
    # ``blob_id`` is a TOP-LEVEL key of ``source`` (sibling of ``options``),
    # NOT a member of ``options``. ``_execute_set_pipeline`` reads it via
    # ``src_args.get("blob_id")`` and feeds it to ``_resolve_source_blob``,
    # which authoritatively materialises ``options["path"]`` and the
    # canonical ``options["blob_ref"]``. Putting ``blob_id`` inside
    # ``options`` would skip resolution and leave the source unbound — the
    # proof step (``compute_proof_diagnostics`` reads ``options["blob_ref"]``)
    # would then silently report no diagnostics.
    required_input_fields = list(slots["required_input_fields"])
    return {
        "source": {
            "plugin": "csv",
            "blob_id": slots["source_blob_id"],
            "on_success": "rows",
            "options": {
                "schema": {"mode": "observed"},
            },
            "on_validation_failure": "discard",
        },
        "nodes": [
            {
                "id": "classifier",
                "node_type": "transform",
                "plugin": "llm",
                "input": "rows",
                "on_success": "labelled",
                "on_error": "discard",
                "options": {
                    "profile": slots["profile"],
                    "prompt_template": slots["classifier_template"],
                    "response_field": slots["label_field"],
                    "schema": {"mode": "observed", "fields": None},
                    "required_input_fields": required_input_fields,
                },
            }
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "labelled",
                "plugin": "json",
                "options": {
                    "path": slots["output_path"],
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {
            "name": "classify-rows-llm-jsonl",
            "description": (
                f"LLM classification of CSV rows; classification stored in field "
                f"'{slots['label_field']}', written to {slots['output_path']}"
            ),
        },
    }


# ---------------------------------------------------------------------------
# Recipe 2: split-by-numeric-threshold
#
#   csv source (blob)  →  type_coerce (numeric field)
#                       →  gate (row[field] >= threshold)
#                       →  above_output sink + below_output sink
# ---------------------------------------------------------------------------


_RECIPE2_SLOTS: Final[dict[str, SlotSpec]] = {
    "source_blob_id": SlotSpec(
        slot_type="blob_id",
        description="UUID of the operator-supplied CSV blob",
    ),
    "field": SlotSpec(
        slot_type="str",
        description="Column to compare against the threshold (must be numeric or coercible)",
    ),
    "threshold": SlotSpec(
        slot_type="float",
        description="Numeric threshold; rows with field >= threshold go to above_output_path",
    ),
    "above_output_path": SlotSpec(
        slot_type="str",
        required=False,
        default="outputs/above.jsonl",
        description="JSONL output for rows meeting/exceeding the threshold",
    ),
    "below_output_path": SlotSpec(
        slot_type="str",
        required=False,
        default="outputs/below.jsonl",
        description="JSONL output for rows below the threshold",
    ),
}


def _build_threshold_recipe(slots: Mapping[str, Any]) -> dict[str, Any]:
    """Build set_pipeline args for the split-by-numeric-threshold recipe."""
    field = slots["field"]
    threshold = slots["threshold"]
    return {
        "source": {
            "plugin": "csv",
            "blob_id": slots["source_blob_id"],
            "on_success": "rows",
            "options": {
                "schema": {"mode": "observed"},
            },
            "on_validation_failure": "discard",
        },
        "nodes": [
            {
                "id": "coerce_numeric",
                "node_type": "transform",
                "plugin": "type_coerce",
                "input": "rows",
                "on_success": "numeric_rows",
                "on_error": "discard",
                "options": {
                    # type_coerce extends DataPluginConfig, which makes
                    # ``schema`` a required field. Recipes use observed
                    # mode so any input columns flow through; the operator
                    # can refine to a fixed schema via patch_node_options
                    # once inspect_source has surfaced the actual headers.
                    "schema": {"mode": "observed"},
                    "conversions": [{"field": field, "to": "float"}],
                },
            },
            {
                "id": "threshold_gate",
                "node_type": "gate",
                "input": "numeric_rows",
                "condition": f"row[{field!r}] >= {threshold}",
                "routes": {"true": "above", "false": "below"},
            },
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "above",
                "plugin": "json",
                "options": {
                    "path": slots["above_output_path"],
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
            {
                "sink_name": "below",
                "plugin": "json",
                "options": {
                    "path": slots["below_output_path"],
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
        ],
        "metadata": {
            "name": "split-by-numeric-threshold",
            "description": (
                f"CSV rows split by {field} >= {threshold}; above → {slots['above_output_path']}, below → {slots['below_output_path']}"
            ),
        },
    }


# ---------------------------------------------------------------------------
# Recipe 3: fork-coalesce-truncate-jsonl
#
#   csv source (blob)  →  fork gate (routes:{all:fork}, fork_to:[path_a, path_b])
#                       →  passthrough (path A)        + truncate (path B)
#                       →  coalesce (merge=nested, {key_a:path_a_out, key_b:path_b_out})
#                       →  jsonl sink (one merged output)
#
# Wiring discipline (gate.fork_to ↔ path.input/on_success ↔ coalesce.branches)
# is encoded once here so the LLM agent never has to maintain it. Slot-fillable
# axes: which CSV blob, which field to truncate, max length, suffix, output
# path, and the two top-level merge keys. Path A is fixed as ``passthrough``
# because the canonical use case is "keep the original row alongside a
# transformed copy"; alternative path-A transforms would be a different
# recipe.
# ---------------------------------------------------------------------------


_INLINE_SOURCE_BLOB_SENTINEL: Final[str] = str(UUID(int=0))
_FORK_CSV_MARKER_RE = re.compile(
    r"(?:customer\s+rows\s*)?\(csv\):\s*\n(?P<csv>.*?)"
    r"(?=(?:customer\s+rows\s*)?\(csv\):\s*\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_FORK_CSV_LABEL_RE = re.compile(r"(?:customer\s+rows\s*)?\(csv\):", re.IGNORECASE)
_FORK_OUTPUT_PATH_RE = re.compile(r"\bat\s+(?P<path>[^\s:]+\.jsonl)\b", re.IGNORECASE)
_JSONL_PATH_TOKEN_RE = re.compile(r"(?P<path>[^\s,:;]+\.jsonl)\b", re.IGNORECASE)
_FORK_TRUNCATE_RE = re.compile(
    r"\btruncates?\s+the\s+(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s+field\s+to\s+(?P<max_chars>\d+)\s+characters?",
    re.IGNORECASE,
)
_FORK_SUFFIX_RE = re.compile(r"\bsuffix\s+(?P<quote>['\"])(?P<suffix>.*?)(?P=quote)", re.IGNORECASE)
_QUOTED_VALUE_RE = re.compile(r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)")
_QUOTED_PROSE_RE = re.compile(r"(?<![A-Za-z0-9])(?P<quote>['\"])[^'\"]+(?P=quote)")
_FORK_KEYS_RE = re.compile(
    r"(?:under\s+separate\s+keys|(?:use|select|choose)\s+(?:the\s+)?keys)\s+"
    r"`(?P<key_a>[A-Za-z_][A-Za-z0-9_]*)`\s+and\s+`(?P<key_b>[A-Za-z_][A-Za-z0-9_]*)`",
    re.IGNORECASE,
)
_FORK_NEGATED_TRUNCATE_RE = re.compile(
    r"\b(?:do(?:es)?\s+not|don't|doesn't|must\s+not|should\s+not|shouldn't|never|avoid(?:s|ed|ing)?|without)\b"
    r"[^.;\n]*\btruncat(?:e|es|ed|ing)\b",
    re.IGNORECASE,
)
_FORK_ALTERNATIVE_OUTPUT_RE = re.compile(
    r"\b(?:alternatively|otherwise|or)\s+"
    r"(?:(?:write|save)(?:\s+it)?\s+(?:at|to)\s+)?(?P<path>[^\s,:;]+\.jsonl)\b",
    re.IGNORECASE,
)
_FORK_ALTERNATIVE_SUFFIX_RE = re.compile(
    r"\b(?:alternatively|otherwise|or)\s+(?:(?:use|select)\s+)?(?:the\s+)?(?:suffix\s+)?"
    r"(?P<quote>['\"])(?P<suffix>.*?)(?P=quote)",
    re.IGNORECASE,
)
_FORK_AMBIGUOUS_KEYS_RE = re.compile(
    r"\bkeys\s+`?[A-Za-z_][A-Za-z0-9_]*`?\s+and\s+`?[A-Za-z_][A-Za-z0-9_]*`?\s+"
    r"(?:or|alternatively|otherwise)\s+(?:(?:use|select)\s+(?:the\s+)?keys\s+)?"
    r"`?[A-Za-z_][A-Za-z0-9_]*`?\s+and\s+`?[A-Za-z_][A-Za-z0-9_]*`?",
    re.IGNORECASE,
)


def _match_fork_coalesce_intent(context: RecipeIntentContext) -> RecipeIntentCandidate | None:
    """Extract required fork semantics and only the optional slots stated."""

    csv_matches = tuple(_FORK_CSV_MARKER_RE.finditer(context.user_text))
    if len(csv_matches) != len(tuple(_FORK_CSV_LABEL_RE.finditer(context.user_text))) or not csv_matches:
        return None
    authority_text = context.user_text[: csv_matches[0].start()]
    lower = authority_text.lower()
    if not all(needle in lower for needle in ("two ways in parallel", "single merged output row", "truncat")):
        return None
    for phrase in ("two ways in parallel", "single merged output row"):
        for phrase_match in re.finditer(re.escape(phrase), authority_text, re.IGNORECASE):
            if _match_is_negated(authority_text, phrase_match, _AUTHORITY_NEGATION_RE):
                return None
    truncate_matches = tuple(_FORK_TRUNCATE_RE.finditer(authority_text))
    if not truncate_matches:
        return None
    if _FORK_NEGATED_TRUNCATE_RE.search(authority_text) is not None:
        return None
    if any(_match_is_negated(context.user_text, match, _AUTHORITY_NEGATION_RE) for match in csv_matches):
        return None
    if any(_match_is_negated(authority_text, match, _AUTHORITY_NEGATION_RE) for match in truncate_matches):
        return None
    truncate_values = tuple((match.group("field"), int(match.group("max_chars"))) for match in truncate_matches)
    truncate_value = truncate_values[0]
    if any(candidate != truncate_value for candidate in truncate_values[1:]):
        return None
    csv_values = tuple(match.group("csv").strip() for match in csv_matches)
    csv_content = csv_values[0]
    if any(candidate != csv_content for candidate in csv_values[1:]):
        return None
    if "\n" not in csv_content:
        return None

    slots: dict[str, object] = {
        "truncate_field": truncate_value[0],
        "max_chars": truncate_value[1],
    }
    output_matches = tuple(_FORK_OUTPUT_PATH_RE.finditer(authority_text))
    if any(_match_is_negated(authority_text, match, _AUTHORITY_NEGATION_RE) for match in output_matches):
        return None
    output_paths = tuple(
        path_match.group("path")
        for output_match in output_matches
        for path_match in _JSONL_PATH_TOKEN_RE.finditer(
            authority_text,
            *_clause_bounds(authority_text, output_match.start()),
        )
    ) + tuple(match.group("path") for match in _FORK_ALTERNATIVE_OUTPUT_RE.finditer(authority_text) if output_matches)
    if output_paths and any(candidate != output_paths[0] for candidate in output_paths[1:]):
        return None
    if output_paths:
        slots["output_path"] = output_paths[0]
    suffix_matches = tuple(_FORK_SUFFIX_RE.finditer(authority_text))
    if any(_match_is_negated(authority_text, match, _AUTHORITY_NEGATION_RE) for match in suffix_matches):
        return None
    suffixes = tuple(
        quoted.group("value")
        for suffix_match in suffix_matches
        for quoted in _QUOTED_VALUE_RE.finditer(
            authority_text,
            *_clause_bounds(authority_text, suffix_match.start()),
        )
    ) + tuple(match.group("suffix") for match in _FORK_ALTERNATIVE_SUFFIX_RE.finditer(authority_text) if suffix_matches)
    if suffixes and any(candidate != suffixes[0] for candidate in suffixes[1:]):
        return None
    if suffixes:
        slots["truncation_suffix"] = suffixes[0]
    if _FORK_AMBIGUOUS_KEYS_RE.search(authority_text) is not None:
        return None
    keys_matches = tuple(_FORK_KEYS_RE.finditer(authority_text))
    if any(_match_is_negated(authority_text, match, _AUTHORITY_NEGATION_RE) for match in keys_matches):
        return None
    keys = tuple((match.group("key_a"), match.group("key_b")) for match in keys_matches)
    if keys and any(candidate != keys[0] for candidate in keys[1:]):
        return None
    if keys:
        slots["key_a"], slots["key_b"] = keys[0]
    return RecipeIntentCandidate(
        slots=slots,
        inline_blob=InlineRecipeBlob(
            filename="inline-fork-coalesce.csv",
            mime_type="text/csv",
            content=csv_content,
        ),
    )


_RECIPE3_SLOTS: Final[dict[str, SlotSpec]] = {
    "source_blob_id": SlotSpec(
        slot_type="blob_id",
        description="UUID of the operator-supplied CSV blob (use create_blob to wrap inline content first)",
    ),
    "truncate_field": SlotSpec(
        slot_type="str",
        description="Name of the row field that path B truncates (e.g., 'description'). Path A leaves the row unchanged.",
    ),
    "max_chars": SlotSpec(
        slot_type="int",
        description=(
            "Maximum length of the truncated field on path B (suffix counts toward this length, "
            "so it must be strictly greater than the suffix length)."
        ),
    ),
    "truncation_suffix": SlotSpec(
        slot_type="str",
        required=False,
        default="...",
        description="Suffix appended when truncation occurs on path B (e.g., '...').",
    ),
    "output_path": SlotSpec(
        slot_type="str",
        required=False,
        default="outputs/merged.jsonl",
        description="JSONL output path for the merged rows.",
    ),
    "key_a": SlotSpec(
        slot_type="str",
        required=False,
        default="path_a",
        description="Top-level field in each merged output row that holds the unchanged-path row body.",
    ),
    "key_b": SlotSpec(
        slot_type="str",
        required=False,
        default="path_b",
        description="Top-level field in each merged output row that holds the truncated-path row body.",
    ),
}


def _build_fork_coalesce_truncate_recipe(slots: Mapping[str, Any]) -> dict[str, Any]:
    """Build set_pipeline args for the fork-coalesce-truncate-jsonl recipe.

    Path A is ``passthrough`` (row unchanged); path B is ``truncate`` with the
    operator-named field clipped to ``max_chars`` (with optional suffix). The
    coalesce node merges both paths under operator-supplied keys via
    ``merge: nested``, so the output rows are ``{key_a: <full row>, key_b:
    <truncated row>}``.

    Wiring discipline: ``coalesce.branches`` is mapping-form:
    ``{branch_name: input_connection}``. The branch names are the operator-
    supplied ``key_a`` / ``key_b`` values (and become nested output keys);
    the input connections are the post-transform path outputs. This is the
    runtime-required representation for transformed fork branches.
    """
    key_a = slots["key_a"]
    key_b = slots["key_b"]
    truncate_field = slots["truncate_field"]
    max_chars = slots["max_chars"]
    suffix = slots["truncation_suffix"]
    if key_a == key_b:
        raise RecipeValidationError("fork/coalesce key_a and key_b must be distinct")
    if max_chars <= len(suffix):
        raise RecipeValidationError("fork/coalesce truncation max_chars must be greater than the truncation suffix length")
    branch_a_input = "path_a"
    branch_b_input = "path_b"
    branch_a_output = "path_a_out"
    branch_b_output = "path_b_out"
    return {
        "source": {
            "plugin": "csv",
            "blob_id": slots["source_blob_id"],
            "on_success": "rows",
            "options": {
                "schema": {"mode": "observed"},
            },
            "on_validation_failure": "discard",
        },
        "nodes": [
            {
                "id": "fork_gate",
                "node_type": "gate",
                "input": "rows",
                # validate_boolean_routes contract: boolean predicates require
                # "true"/"false" labels. This recipe intentionally returns the
                # string literal "all" so the single route label is runtime-valid
                # while still forking every row.
                "condition": "'all'",
                "routes": {"all": "fork"},
                # Internal connection names are fixed and separate from the
                # user-visible nested merge keys. This prevents a legitimate
                # output key from colliding with source or sink connections.
                "fork_to": [branch_a_input, branch_b_input],
            },
            {
                "id": "path_a_passthrough",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": branch_a_input,
                "on_success": branch_a_output,
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                },
            },
            {
                "id": "path_b_truncate",
                "node_type": "transform",
                "plugin": "truncate",
                "input": branch_b_input,
                "on_success": branch_b_output,
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                    "fields": {truncate_field: max_chars},
                    "suffix": suffix,
                },
            },
            {
                "id": "merge_paths",
                "node_type": "coalesce",
                # ``input`` is required by NodeSpec for every node, but the
                # producer-resolver special-cases coalesce (it walks
                # ``branches`` for routing, not ``input``). The literal
                # sentinel ``"branches"`` is the established convention
                # (see tests/unit/web/composer/test_producer_resolver.py).
                "input": "branches",
                # Mapping form: branch names are the nested output keys, while
                # values are the post-transform connections consumed by coalesce.
                "branches": {key_a: branch_a_output, key_b: branch_b_output},
                "policy": "require_all",
                "merge": "nested",
                "on_success": "merged_rows",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "merged_rows",
                "plugin": "json",
                "options": {
                    "path": slots["output_path"],
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {
            "name": "fork-coalesce-truncate-jsonl",
            "description": (
                f"Fork+coalesce: each row produces one merged output row with "
                f"'{key_a}' (unchanged) and '{key_b}' (field '{truncate_field}' "
                f"truncated to {max_chars} chars with suffix {suffix!r}); "
                f"written to {slots['output_path']}"
            ),
        },
    }


# ---------------------------------------------------------------------------
# Recipe 4: web-scrape-llm-project-jsonl
#
#   json/csv locator-row source     →  web_scrape (fetch page content)
#                                    →  llm (project a named response)
#                                    →  field_mapper(select_only) cleanup
#                                    →  jsonl sink (single output)
#
# web_scrape is a TRANSFORM, not a source: the head source is a json/csv blob
# with one explicitly named locator field. The field_mapper drops the raw scraped content/fingerprint
# (data minimization) and stages the kind=pipeline_decision raw-HTML cleanup
# requirement so the blocking cleanup contract (raw_html_cleanup_review_contract_error,
# interpretation_state.py) passes deterministically.
#
# Prompt-injection shield (rev 4, re-polarized): the recipe OMITS an unbuildable
# azure_prompt_shield hard node (the composer cannot instantiate it without
# configured endpoint+api_key secrets — elspeth-abb2cb0931, a CONDITIONAL
# security ticket, NOT a licence to remove all shield signal). It does NOT
# suppress the existing medium-severity prompt-shield advisory warning
# (prompt_shield_recommendation_warning_pairs), which surfaces at the wire
# stage from validate(). See test_no_azure_prompt_shield_hard_node AND the P4.3
# advisory-presence test.
# ---------------------------------------------------------------------------


_GENERIC_WEB_PROMPT_TEMPLATE: Final[str] = "Analyze the following public page and return the requested response:\n\n{{ row['content'] }}"
_LEGACY_RATING_TEMPLATE: Final[str] = "Rate the appeal of this government web page from 1-10 and explain briefly:\n\n{{ row['content'] }}"
_RAW_SCRAPE_FIELDS: Final[frozenset[str]] = frozenset({"content", "content_fingerprint"})
_WEB_SCRAPE_GENERATED_FIELDS: Final[frozenset[str]] = frozenset(
    {"content", "content_fingerprint", "fetch_status", "fetch_url_final", "fetch_url_final_ip"}
)
_FIELD_TOKEN_RE = re.compile(r"`(?P<field>[A-Za-z_][A-Za-z0-9_]*)`")
_RESPONSE_FIELD_RE = re.compile(
    r"\b(?:write|produce|generate|create|return|store)(?:\s+(?:a|an))?(?:\s+(?:short|concise|brief))?\s+"
    r"(?:`(?P<quoted>[A-Za-z_][A-Za-z0-9_]*)`|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))",
    re.IGNORECASE,
)
_EMAIL_PATTERN: Final[str] = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
_ABUSE_CONTACT_RE = re.compile(
    rf"(?:\babuse[-_\s]+contact(?:\s+(?:is\s+)?|\s*[:=]\s*)(?P<after>{_EMAIL_PATTERN})"
    rf"|\b(?P<before>{_EMAIL_PATTERN})\s+as\s+(?:the\s+)?(?:scraping\s+)?abuse[-_\s]+contact\b)",
    re.IGNORECASE,
)
_SCRAPING_REASON_RE = re.compile(
    r"\b(?:scraping\s+reason|reason\s+for\s+(?:the\s+)?(?:scrape|fetch))\s*(?:is|:)?\s*"
    r"(?P<quote>['\"])(?P<reason>[^'\"]+)(?P=quote)",
    re.IGNORECASE,
)
_ABUSE_CONTACT_LABEL_RE = re.compile(r"\b(?:scraping\s+)?abuse[-_\s]+contact\b", re.IGNORECASE)
_SCRAPING_REASON_LABEL_RE = re.compile(
    r"\b(?:scraping\s+reason|reason\s+for\s+(?:the\s+)?(?:scrape|fetch))\b",
    re.IGNORECASE,
)
_AUTHORITY_NEGATION_RE = re.compile(
    r"\b(?:(?:do|does|did)\s+not|don't|doesn't|didn't|never|no|not|must\s+not|should\s+not|shouldn't|"
    r"cannot|can't|avoid(?:s|ed|ing)?|without)\b",
    re.IGNORECASE,
)
_CLAUSE_DELIMITER_RE = re.compile(r";|\r?\n|\.(?=\s|$)")
_LLM_TOKEN_RE = re.compile(r"\b(?:an?\s+)?llm\b", re.IGNORECASE)
_RESPONSE_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don't|never|must\s+not|should\s+not|shouldn't|avoid|without)\s+"
    r"(?:(?:have|having|let|ask|asking|use|using)\s+)?(?:an?\s+|the\s+)?llm\b(?:\s+to)?[^.;\n]*"
    r"\b(?:write|produce|generate|create|return|store)\b"
    r"|\bllm\b[^.;\n,]{0,80}\b(?:must\s+)?not\s+(?:write|produce|generate|create|return|store)\b"
    r"|\b(?:do\s+not|don't|never|must\s+not|should\s+not|shouldn't|avoid)\s+"
    r"(?:write|produce|generate|create|return|store)\b",
    re.IGNORECASE,
)
_PROJECTION_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don't|never|must\s+not|should\s+not|shouldn't)\s+(?:keep|retain|project)\b",
    re.IGNORECASE,
)
_PROFILE_PREFIX_RE = re.compile(
    r"\b(?:an?\s+)?llm\s+profile\s*(?:(?:is|named)\s+|[:=]\s*)(?:the\s+)?"
    r"(?:`(?P<quoted>[A-Za-z0-9][A-Za-z0-9_-]*)`|(?P<plain>[A-Za-z0-9][A-Za-z0-9_-]*))",
    re.IGNORECASE,
)
_PROFILE_SUFFIX_RE = re.compile(
    r"\b(?:use|select|choose)\s+(?:the\s+)?"
    r"(?:`(?P<quoted>[A-Za-z0-9][A-Za-z0-9_-]*)`|(?!(?:an?|the)\b)(?P<plain>[A-Za-z0-9][A-Za-z0-9_-]*))\s+"
    r"(?:llm\s+)?profile\b",
    re.IGNORECASE,
)
_PROFILE_BARE_ALIAS_RE = re.compile(
    r"\b(?:use|select|choose)\s+(?:the\s+)?"
    r"(?:`(?P<quoted>[A-Za-z0-9][A-Za-z0-9_-]*)`|(?P<plain>[A-Za-z0-9]+-[A-Za-z0-9_-]+))(?![A-Za-z0-9_@-])",
    re.IGNORECASE,
)
_PROFILE_LABEL_RE = re.compile(
    r"\b(?:an?\s+)?llm\s+profile\b|\bprofile\s*(?:is\b|named\b|[:=])",
    re.IGNORECASE,
)
_FETCH_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don't|never|must\s+not|should\s+not|shouldn't|avoid)\s+(?:fetch|scrape)\b",
    re.IGNORECASE,
)
_NEGATIVE_ACTION_PATTERN: Final[str] = r"(?:do\s+not|don't|never|must\s+not|should\s+not|shouldn't)"
_SOURCE_AUTHORITY_RE = re.compile(
    rf"\b(?P<negative>{_NEGATIVE_ACTION_PATTERN}\s+)?(?:use|select|choose|read)\s+"
    r"(?:an?\s+|the\s+)?(?P<reviewed>reviewed\s+)?(?:(?P<format>[A-Za-z][A-Za-z0-9_-]*)\s+)?source\b"
    r"(?:\s+(?:named\s+)?(?:`(?P<quoted>[A-Za-z_][A-Za-z0-9_-]*)`|"
    r"(?!(?:for|with|from|rows?|material)\b)(?P<name>[A-Za-z_][A-Za-z0-9_-]*)))?",
    re.IGNORECASE,
)
_OUTPUT_AUTHORITY_RE = re.compile(
    rf"\b(?P<negative>{_NEGATIVE_ACTION_PATTERN}\s+)?(?:write|save|use|select|choose)\s+"
    r"(?:an?\s+|the\s+)?(?P<reviewed>reviewed\s+)?(?:(?P<format>[A-Za-z][A-Za-z0-9_-]*)\s+)?output\b"
    r"(?:\s+(?:named\s+)?(?:`(?P<quoted>[A-Za-z_][A-Za-z0-9_-]*)`|(?P<name>(?!at\b)[A-Za-z_][A-Za-z0-9_-]*)))?"
    r"(?:\s+at\s+(?P<path>[A-Za-z0-9_./-]*[A-Za-z0-9_/-]))?",
    re.IGNORECASE,
)
_OUTPUT_PATH_AUTHORITY_RE = re.compile(
    r"\b(?:write|save)\s+(?:(?:the\s+)?output|it)\s+(?:at|to)\s+"
    r"(?P<path>[A-Za-z0-9_./-]*[A-Za-z0-9_/-])",
    re.IGNORECASE,
)
_OUTPUT_LEADING_AUTHORITY_RE = re.compile(
    rf"\boutput\s+(?:(?P<negative>{_NEGATIVE_ACTION_PATTERN})\s+|(?P<modal>must|should)\s+)?(?:be\s+)?"
    r"(?P<format>[A-Za-z][A-Za-z0-9_-]*)\b",
    re.IGNORECASE,
)
_RESPONSE_DIRECT_ALTERNATIVE_RE = re.compile(
    r"^\s*(?:(?:or|alternatively|otherwise)\s+|/\s*)"
    r"(?:an?\s+)?(?:`[A-Za-z_][A-Za-z0-9_]*`|[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_RESPONSE_DIRECT_COORDINATOR_RE = re.compile(r"^\s*,?\s*(?:and|or|then|alternatively|otherwise)\s*$", re.IGNORECASE)
_PIPELINE_CLEANUP_START_RE = re.compile(
    r"^(?:then\s+|finally\s+)?(?:remove|drop|discard|exclude)\b"
    r"(?=[^.;\n]*(?:\braw\s+(?:(?:page|scraped)\s+)?(?:content|html)\b|\bfingerprint\s+(?:columns?|fields?)\b))",
    re.IGNORECASE,
)
_WEB_LOCATOR_RE = re.compile(
    r"\b(?:fetch|scrape)\s+(?:"
    r"(?:every|each)\s+(?:(?:reviewed\s+)?pages?\s+(?:(?:named\s+by|at(?:\s+its)?)\s+))?"
    r"|the\s+page\s+at(?:\s+its)?\s+)"
    r"`(?P<field>[A-Za-z_][A-Za-z0-9_]*)`",
    re.IGNORECASE,
)
_SOURCE_AUTHORITY_ALTERNATIVE_RE = re.compile(
    r"^\s*(?:or|alternatively|otherwise|/)\s+(?:an?\s+|the\s+)?(?:reviewed\s+)?"
    r"(?:[A-Za-z][A-Za-z0-9_-]*\s+)?source\b",
    re.IGNORECASE,
)
_OUTPUT_AUTHORITY_ALTERNATIVE_RE = re.compile(
    r"^\s*(?:or|alternatively|otherwise|/)\s+(?:(?:write|save|use|select|choose)\s+)?"
    r"(?:an?\s+|the\s+)?(?:reviewed\s+)?(?:[A-Za-z][A-Za-z0-9_-]*\s+)?output\b",
    re.IGNORECASE,
)
_RETENTION_CLAUSE_RE = re.compile(
    r"\b(?:keep|retain|project)\s+(?:only|exactly)\s+(?P<fields>.*?)"
    r"(?=(?:[.;]|$|,?\s+(?:and|then)\s+(?:(?:write|save|send|output)\b|(?:keep|retain|project)\s+(?:only|exactly)\b)"
    r"|,?\s+(?:and\s+)?(?:abuse[-_\s]+contact|scraping\s+reason|reason\s+for\s+(?:the\s+)?(?:scrape|fetch))\b))",
    re.IGNORECASE | re.DOTALL,
)
_PROJECTION_FIELD_LIST_RE = re.compile(
    r"`[A-Za-z_][A-Za-z0-9_]*`"
    r"(?:\s*(?:,\s*(?:and\s+)?|\s+and\s+)`[A-Za-z_][A-Za-z0-9_]*`)*",
)


def _clause_bounds(user_text: str, position: int) -> tuple[int, int]:
    """Return the delimiter-bounded clause containing ``position``."""

    start = 0
    for delimiter in _CLAUSE_DELIMITER_RE.finditer(user_text):
        if delimiter.end() <= position:
            start = delimiter.end()
            continue
        if delimiter.start() >= position:
            return start, delimiter.start()
    return start, len(user_text)


def _match_is_negated(user_text: str, match: re.Match[str], negation: re.Pattern[str]) -> bool:
    start, end = _clause_bounds(user_text, match.start())
    return negation.search(user_text[start:end]) is not None


def _match_prefix_is_negated(user_text: str, match: re.Match[str]) -> bool:
    """Return whether unquoted authority leading into ``match`` is negated."""

    clause_start, _clause_end = _clause_bounds(user_text, match.start())
    return any(
        not _position_is_in_quoted_prose(user_text, negation.start())
        for negation in _AUTHORITY_NEGATION_RE.finditer(user_text, clause_start, match.end())
    )


def _match_clause_residue(user_text: str, match: re.Match[str]) -> str:
    """Return text after a parsed authority value in its own clause."""

    _clause_start, clause_end = _clause_bounds(user_text, match.start())
    return user_text[match.end() : clause_end]


def _residue_starts_with_alternative(residue: str) -> bool:
    return re.match(r"^\s*,?\s*(?:or|alternatively|otherwise)\b|^\s*/", residue, re.IGNORECASE) is not None


def _pipeline_cleanup_starts(user_text: str) -> tuple[int, ...]:
    """Return clause starts that describe a post-LLM projection cleanup."""

    boundaries = (0, *(delimiter.end() for delimiter in _CLAUSE_DELIMITER_RE.finditer(user_text)))
    starts: list[int] = []
    for boundary in boundaries:
        content_start = boundary + len(user_text[boundary:]) - len(user_text[boundary:].lstrip())
        _clause_start, clause_end = _clause_bounds(user_text, content_start)
        if _PIPELINE_CLEANUP_START_RE.search(user_text[content_start:clause_end]) is not None:
            starts.append(content_start)
    return tuple(starts)


def _web_response_semantic_span(user_text: str) -> tuple[int, int] | None:
    """Return the LLM semantic region before projection/HTTP authority."""

    llm_match = _LLM_TOKEN_RE.search(user_text)
    if llm_match is None:
        return None
    nonsemantic_starts = tuple(
        match.start() for pattern in (_RETENTION_CLAUSE_RE, _ABUSE_CONTACT_RE, _SCRAPING_REASON_RE) for match in pattern.finditer(user_text)
    ) + _pipeline_cleanup_starts(user_text)
    end = min((start for start in nonsemantic_starts if start > llm_match.start()), default=len(user_text))
    return llm_match.start(), end


def _position_is_in_quoted_prose(user_text: str, position: int) -> bool:
    return any(match.start() < position < match.end() for match in _QUOTED_PROSE_RE.finditer(user_text))


def _profile_match_is_subordinate_response_prose(user_text: str, match: re.Match[str]) -> bool:
    semantic_span = _web_response_semantic_span(user_text)
    return semantic_span is not None and semantic_span[0] <= match.start() < semantic_span[1]


def _residue_starts_with_profile_alternative(residue: str) -> bool:
    return (
        re.match(
            r"^\s*(?:or|and|alternatively|otherwise|/)\s+(?:`?[A-Za-z0-9][A-Za-z0-9_-]*`?)",
            residue,
            re.IGNORECASE,
        )
        is not None
    )


def _response_authority_matches(user_text: str, *, start: int, end: int) -> tuple[re.Match[str], ...]:
    """Return response declarations, excluding subordinate or quoted prose."""

    candidates = tuple(
        match for match in _RESPONSE_FIELD_RE.finditer(user_text, start, end) if not _position_is_in_quoted_prose(user_text, match.start())
    )
    if not candidates:
        return ()
    authorities = [candidates[0]]
    for candidate in candidates[1:]:
        previous = authorities[-1]
        previous_clause = _clause_bounds(user_text, previous.start())
        candidate_clause = _clause_bounds(user_text, candidate.start())
        if previous_clause == candidate_clause:
            if _RESPONSE_DIRECT_COORDINATOR_RE.fullmatch(user_text[previous.end() : candidate.start()]) is not None:
                authorities.append(candidate)
            continue
        prefix = user_text[candidate_clause[0] : candidate.start()]
        if re.fullmatch(r"\s*(?:(?:and|then|or|alternatively|otherwise)\s+)?", prefix, re.IGNORECASE) is not None:
            authorities.append(candidate)
    return tuple(authorities)


def _response_match_is_negated(user_text: str, match: re.Match[str]) -> bool:
    clause_start, _clause_end = _clause_bounds(user_text, match.start())
    return _RESPONSE_NEGATION_RE.search(user_text[clause_start : match.end()]) is not None


def _web_locator_field(user_text: str, source_fields: tuple[str, ...]) -> str | None:
    """Bind the locator only from the fetch/scrape operation's own span."""

    llm_match = _LLM_TOKEN_RE.search(user_text)
    if llm_match is None:
        return None
    unique = _unique_in_order([match.group("field") for match in _WEB_LOCATOR_RE.finditer(user_text, 0, llm_match.start())])
    if len(unique) != 1 or unique[0] not in source_fields:
        return None
    return unique[0]


def _llm_generated_fields(response_field: str) -> frozenset[str]:
    return frozenset({response_field, f"{response_field}_usage", f"{response_field}_model"})


def _web_projection_namespace_collisions(
    *,
    locator_field: str,
    response_field: str,
    upstream_fields: tuple[str, ...],
) -> frozenset[str]:
    """Return cross-stage generated-field collisions for the generic graph."""

    upstream = {*upstream_fields, locator_field}
    llm_generated = _llm_generated_fields(response_field)
    return frozenset(
        (upstream & _WEB_SCRAPE_GENERATED_FIELDS) | (upstream & llm_generated) | (_WEB_SCRAPE_GENERATED_FIELDS & llm_generated)
    )


_RECIPE_WEB_PROJECT_SLOTS: Final[dict[str, SlotSpec]] = {
    "source_blob_id": SlotSpec(
        slot_type="blob_id",
        description="UUID of the operator-supplied locator-row blob; use create_blob to wrap inline content first",
    ),
    "source_plugin": SlotSpec(
        slot_type="str",
        description="Resolved locator-row source plugin; must be 'json' or 'csv' and match server-owned source authority.",
    ),
    "locator_field": SlotSpec(
        slot_type="str",
        description="Exact source-row field carrying each page locator.",
    ),
    "profile": SlotSpec(
        slot_type="str",
        description="Opaque operator-approved LLM profile alias",
    ),
    "prompt_template": SlotSpec(
        slot_type="str",
        required=False,
        default=_GENERIC_WEB_PROMPT_TEMPLATE,
        description=(
            "Jinja2 prompt template. When omitted, the recipe's visible generic default asks the LLM to analyze "
            "the scraped content and return the requested response."
        ),
    ),
    "response_field": SlotSpec(
        slot_type="str",
        description="Exact row field where the LLM response is stored.",
    ),
    "required_input_fields": SlotSpec(
        slot_type="str_list",
        required=False,
        default=("content",),
        description="Exact input fields consumed by prompt_template.",
    ),
    "retained_fields": SlotSpec(
        slot_type="str_list",
        description="Exact output projection retained after raw scrape cleanup.",
    ),
    "abuse_contact": SlotSpec(
        slot_type="str",
        description=(
            "Operator-owned monitored contact address sent in web_scrape HTTP metadata. "
            "It must be supplied explicitly or by exact reviewed server authority; the recipe never invents one."
        ),
    ),
    "scraping_reason": SlotSpec(
        slot_type="str",
        description=(
            "Operator-authored reason for scraping, sent in web_scrape HTTP metadata. "
            "It must be supplied explicitly or by exact reviewed server authority; the recipe never derives or guesses one."
        ),
    ),
    "output_path": SlotSpec(
        slot_type="str",
        description="Exact reviewed JSONL output path.",
    ),
    "allowed_hosts": SlotSpec(
        slot_type="str_list",
        required=False,
        # Tuple default (not []): recipes.py warns a mutable list default would
        # silently bypass the frozen contract; the sibling str_list slot
        # required_input_fields uses default=() (recipes.py:190). _coerce_slot
        # (recipes.py:51) returns tuple(items) for a SUPPLIED str_list; an omitted
        # slot gets spec.default verbatim (recipes.py:142) — () is already a tuple.
        default=(),
        description=(
            "SSRF allowlist for the web_scrape node, as a list of CIDR strings. "
            "Empty (the default) omits the key so the web_scrape field default "
            "'public_only' applies — the correct value for a public host. SSRF safety comes "
            "from the web_scrape enforcement boundary (CidrStr validation + the "
            "'public_only' field default), not from the slot being unreachable — "
            "apply_pipeline_recipe is a Tier-3 boundary that can forward an "
            "LLM-authored value, which the enforcement boundary still constrains."
        ),
    ),
}


def _build_web_scrape_project_recipe(slots: Mapping[str, Any]) -> dict[str, Any]:
    """Build a source → scrape → LLM → exact projection → JSONL graph.

    Emits source → web_scrape → llm → field_mapper(cleanup) → jsonl, named by
    connection labels (NOT EdgeSpec objects — guided passes edges=[]). The
    field_mapper drops the raw scraped content/fingerprint and stages the
    kind=pipeline_decision raw-HTML cleanup requirement so the blocking cleanup
    contract passes. The unbuildable azure_prompt_shield hard node is omitted
    (elspeth-abb2cb0931); the existing medium-severity prompt-shield advisory
    is left to fire from validate() — the recipe MUST NOT suppress it.
    """
    # Function-level imports keep the recipe builder's dependency boundary
    # narrow and avoid circular imports with the tools plane.
    from elspeth.contracts.composer_interpretation import InterpretationKind
    from elspeth.web.interpretation_state import (
        INTERPRETATION_REQUIREMENTS_KEY,
        RAW_HTML_CLEANUP_REVIEW_DRAFT,
        RAW_HTML_CLEANUP_USER_TERM,
    )

    source_plugin = slots["source_plugin"]
    if source_plugin not in {"csv", "json"}:
        raise RecipeValidationError("web projection recipe source_plugin must be 'csv' or 'json'")
    locator_field = slots["locator_field"]
    response_field = slots["response_field"]
    retained_fields = tuple(slots["retained_fields"])
    if type(locator_field) is not str or not locator_field:
        raise RecipeValidationError("web projection recipe locator_field must be non-empty")
    if type(response_field) is not str or not response_field:
        raise RecipeValidationError("web projection recipe response_field must be non-empty")
    namespace_collisions = _web_projection_namespace_collisions(
        locator_field=locator_field,
        response_field=response_field,
        upstream_fields=tuple(field for field in retained_fields if field != response_field),
    )
    if namespace_collisions:
        raise RecipeValidationError(
            f"web projection recipe generated field namespace collides with raw scrape or LLM outputs: {sorted(namespace_collisions)}"
        )
    if not retained_fields or len(set(retained_fields)) != len(retained_fields):
        raise RecipeValidationError("web projection recipe retained_fields must be a non-empty unique list")
    if response_field not in retained_fields:
        raise RecipeValidationError("web projection recipe retained_fields must include response_field")

    content_field = "content"
    fingerprint_field = "content_fingerprint"
    cleanup_requirement = {
        "kind": InterpretationKind.PIPELINE_DECISION.value,
        "user_term": RAW_HTML_CLEANUP_USER_TERM,
        "draft": RAW_HTML_CLEANUP_REVIEW_DRAFT,
    }
    web_scrape_options: dict[str, Any] = {
        "schema": {"mode": "observed"},
        "url_field": locator_field,
        "content_field": content_field,
        "fingerprint_field": fingerprint_field,
        "format": "markdown",
        "http": {
            # OPERATOR: these values are visible to scraped third
            # parties. They are required slots, not tutorial
            # defaults: use a monitored operator-owned inbox and an
            # accurate reason, or do not apply the recipe.
            "abuse_contact": slots["abuse_contact"],
            "scraping_reason": slots["scraping_reason"],
        },
    }
    allowed_hosts = slots["allowed_hosts"]
    if allowed_hosts:
        # SSRF allowlist supplied by exact reviewed authority. Empty -> omitted
        # -> the web_scrape field default public_only.
        # The allowlist is a field of ``WebScrapeHTTPConfig`` (web_scrape.py),
        # so it MUST nest under ``http`` beside abuse_contact/scraping_reason —
        # a top-level key is rejected by the plugin (extra:forbid).
        web_scrape_options["http"]["allowed_hosts"] = list(allowed_hosts)
    return {
        "source": {
            "plugin": source_plugin,
            "blob_id": slots["source_blob_id"],
            "on_success": "rows",
            "options": {
                "schema": {"mode": "observed"},
            },
            "on_validation_failure": "discard",
        },
        "nodes": [
            {
                "id": "url_rows",
                "node_type": "transform",
                "plugin": "web_scrape",
                "input": "rows",
                "on_success": "scraped",
                "on_error": "discard",
                "options": web_scrape_options,
            },
            {
                "id": "rate_pages",
                "node_type": "transform",
                "plugin": "llm",
                "input": "scraped",
                "on_success": "rated",
                "on_error": "discard",
                "options": {
                    "profile": slots["profile"],
                    "prompt_template": slots["prompt_template"],
                    "response_field": response_field,
                    "schema": {"mode": "observed"},
                    "required_input_fields": list(slots["required_input_fields"]),
                },
            },
            {
                "id": "drop_raw_html",
                "node_type": "transform",
                "plugin": "field_mapper",
                "input": "rated",
                "on_success": "clean",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                    "select_only": True,
                    # mapping preserves ONLY the user-facing fields; the raw
                    # content/fingerprint are intentionally absent (dropped).
                    "mapping": {field: field for field in retained_fields},
                    INTERPRETATION_REQUIREMENTS_KEY: [cleanup_requirement],
                },
            },
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "clean",
                "plugin": "json",
                "options": {
                    "path": slots["output_path"],
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {
            "name": "web-scrape-llm-project-jsonl",
            "description": (
                f"Scrape each locator in '{locator_field}', store the LLM response in '{response_field}', "
                f"retain exactly {list(retained_fields)!r}, and write JSONL to {slots['output_path']}"
            ),
        },
    }


_RECIPE_WEB_RATE_SLOTS: Final[dict[str, SlotSpec]] = {
    "source_blob_id": _RECIPE_WEB_PROJECT_SLOTS["source_blob_id"],
    "source_plugin": _RECIPE_WEB_PROJECT_SLOTS["source_plugin"],
    "profile": _RECIPE_WEB_PROJECT_SLOTS["profile"],
    "rating_template": SlotSpec(
        slot_type="str",
        required=False,
        default=_LEGACY_RATING_TEMPLATE,
        description="Legacy rating prompt template.",
    ),
    "abuse_contact": _RECIPE_WEB_PROJECT_SLOTS["abuse_contact"],
    "scraping_reason": _RECIPE_WEB_PROJECT_SLOTS["scraping_reason"],
    "output_path": SlotSpec(
        slot_type="str",
        required=False,
        default="outputs/ratings.jsonl",
        description="JSONL output path",
    ),
    "allowed_hosts": _RECIPE_WEB_PROJECT_SLOTS["allowed_hosts"],
}


def _build_legacy_web_rating_recipe(slots: Mapping[str, Any]) -> Mapping[str, Any]:
    """Translate the legacy rating slots onto the generic graph builder."""

    return _build_web_scrape_project_recipe(
        {
            "source_blob_id": slots["source_blob_id"],
            "source_plugin": slots["source_plugin"],
            "locator_field": "url",
            "profile": slots["profile"],
            "prompt_template": slots["rating_template"],
            "response_field": "rating",
            "required_input_fields": ("content",),
            "retained_fields": ("url", "rating"),
            "abuse_contact": slots["abuse_contact"],
            "scraping_reason": slots["scraping_reason"],
            "output_path": slots["output_path"],
            "allowed_hosts": slots["allowed_hosts"],
        }
    )


def _unique_in_order(values: list[str]) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return tuple(unique)


def _web_response_contract(user_text: str) -> tuple[str, str] | None:
    """Return one exact response field and semantic instruction, else abstain."""

    semantic_span = _web_response_semantic_span(user_text)
    if semantic_span is None:
        return None
    semantic_start, semantic_end = semantic_span
    nonsemantic_starts = (semantic_end,)
    response_matches = _response_authority_matches(user_text, start=semantic_start, end=semantic_end)
    if not response_matches:
        return None
    contracts: list[tuple[str, str]] = []
    for index, response_match in enumerate(response_matches):
        if _response_match_is_negated(user_text, response_match):
            return None
        boundary_starts = [start for start in nonsemantic_starts if start >= response_match.end()]
        if index + 1 < len(response_matches):
            boundary_starts.append(response_matches[index + 1].start())
        end = min(boundary_starts, default=len(user_text))
        response_residue = user_text[response_match.end() : end]
        if _RESPONSE_DIRECT_ALTERNATIVE_RE.match(response_residue) is not None:
            return None
        clause_start, _clause_end = _clause_bounds(user_text, response_match.start())
        llm_mentions = tuple(_LLM_TOKEN_RE.finditer(user_text, clause_start, response_match.start()))
        instruction_start = llm_mentions[-1].end() if llm_mentions else response_match.start()
        profile_matches = tuple(
            match
            for pattern in (_PROFILE_PREFIX_RE, _PROFILE_SUFFIX_RE)
            for match in pattern.finditer(user_text, clause_start, response_match.start())
        )
        if profile_matches:
            instruction_start = max(match.end() for match in profile_matches)
        instruction = user_text[instruction_start:end].strip(" ,.;")
        instruction = re.sub(r"^to\s+", "", instruction, count=1, flags=re.IGNORECASE)
        instruction = re.sub(r"(?:[,;]\s*)?(?:and|then)\s*$", "", instruction, flags=re.IGNORECASE).strip(" ,.;")
        field = response_match.group("quoted") or response_match.group("plain")
        if not instruction or field is None:
            return None
        contracts.append((field, " ".join(instruction.split())))
    contract = contracts[0]
    if any(candidate != contract for candidate in contracts[1:]):
        return None
    field, instruction = contract
    return field, f"User instruction: {instruction}\n\nPublic page content:\n{{{{ row['content'] }}}}"


def _web_retained_fields(user_text: str) -> tuple[str, ...] | None:
    """Return one fully parsed exact retention/projection, else abstain."""

    clauses = tuple(_RETENTION_CLAUSE_RE.finditer(user_text))
    if not clauses:
        return None
    projections: list[tuple[str, ...]] = []
    for clause in clauses:
        if _match_is_negated(user_text, clause, _PROJECTION_NEGATION_RE):
            return None
        raw_fields = clause.group("fields").strip()
        if _PROJECTION_FIELD_LIST_RE.fullmatch(raw_fields) is None:
            return None
        fields = tuple(match.group("field") for match in _FIELD_TOKEN_RE.finditer(raw_fields))
        if not fields or len(set(fields)) != len(fields):
            return None
        projections.append(fields)
    projection = projections[0]
    if any(candidate != projection for candidate in projections[1:]):
        return None
    return projection


def _has_negated_label_clause(user_text: str, label_pattern: re.Pattern[str]) -> bool:
    return any(_match_prefix_is_negated(user_text, label) for label in label_pattern.finditer(user_text))


def _web_abuse_contact(user_text: str) -> str | None:
    """Return one affirmative explicitly labelled abuse contact, else abstain."""

    if _has_negated_label_clause(user_text, _ABUSE_CONTACT_LABEL_RE):
        return None
    matches = tuple(_ABUSE_CONTACT_RE.finditer(user_text))
    if not matches:
        return None
    for match in matches:
        residue = _match_clause_residue(user_text, match)
        if _residue_starts_with_alternative(residue) or re.search(_EMAIL_PATTERN, residue) is not None:
            return None
    contacts = tuple(match.group("after") or match.group("before") for match in matches)
    contact = contacts[0]
    if contact is None or any(candidate != contact for candidate in contacts[1:]):
        return None
    return contact


def _web_scraping_reason(user_text: str) -> str | None:
    """Return one exact explicitly labelled ASCII scraping reason, else abstain."""

    if _has_negated_label_clause(user_text, _SCRAPING_REASON_LABEL_RE):
        return None
    matches = tuple(_SCRAPING_REASON_RE.finditer(user_text))
    if not matches:
        return None
    for match in matches:
        residue = _match_clause_residue(user_text, match)
        if (
            _residue_starts_with_alternative(residue)
            or re.match(r"^\s*,\s*['\"]", residue) is not None
            or re.match(r"^\s*and\s+['\"]", residue, re.IGNORECASE) is not None
        ):
            return None
    reasons = tuple(match.group("reason").strip() for match in matches)
    reason = reasons[0]
    if not reason or not reason.isascii() or any(candidate != reason for candidate in reasons[1:]):
        return None
    return reason


def _single_llm_profile(context: RecipeIntentContext) -> str | None:
    snapshot = context.policy_snapshot
    if snapshot is None:
        return None
    aliases = dict(snapshot.usable_profile_aliases).get(PluginId("transform", "llm"), ())
    for alias in aliases:
        negated_alias = re.compile(
            rf"\b{_NEGATIVE_ACTION_PATTERN}\s+use\s+(?:the\s+)?`?{re.escape(alias)}`?(?![A-Za-z0-9_-])",
            re.IGNORECASE,
        )
        if negated_alias.search(context.user_text) is not None:
            return None
    authority_matches = tuple(
        sorted(
            (
                *(
                    match
                    for match in _PROFILE_PREFIX_RE.finditer(context.user_text)
                    if not _position_is_in_quoted_prose(context.user_text, match.start())
                ),
                *(
                    match
                    for match in _PROFILE_SUFFIX_RE.finditer(context.user_text)
                    if not _position_is_in_quoted_prose(context.user_text, match.start())
                ),
                *(
                    match
                    for match in _PROFILE_BARE_ALIAS_RE.finditer(context.user_text)
                    if not _position_is_in_quoted_prose(context.user_text, match.start())
                    and not _profile_match_is_subordinate_response_prose(context.user_text, match)
                    and (
                        (match.group("quoted") or match.group("plain")) in aliases
                        or (match.group("quoted") or match.group("plain") or "").casefold().startswith("profile-")
                    )
                ),
            ),
            key=lambda match: match.start(),
        )
    )
    if any(_match_is_negated(context.user_text, match, _AUTHORITY_NEGATION_RE) for match in authority_matches):
        return None
    if authority_matches:
        if any(_residue_starts_with_profile_alternative(_match_clause_residue(context.user_text, match)) for match in authority_matches):
            return None
        selected = tuple((match.group("quoted") or match.group("plain")) for match in authority_matches)
        profile = selected[0]
        if any(candidate != profile for candidate in selected[1:]) or profile not in aliases:
            return None
        return profile
    if _PROFILE_LABEL_RE.search(context.user_text) is not None:
        return None
    return aliases[0] if len(aliases) == 1 else None


def _reviewed_web_authority_is_compatible(
    context: RecipeIntentContext,
    *,
    source: RecipeReviewedSourceSummary,
    output: RecipeReviewedOutputSummary,
    output_binding: RecipeOutputBinding,
) -> bool:
    """Return whether explicit source/output prose agrees with reviewed authority."""

    semantic_span = _web_response_semantic_span(context.user_text)
    if any(
        not _position_is_in_quoted_prose(context.user_text, fetch_negation.start())
        and (semantic_span is None or not (semantic_span[0] <= fetch_negation.start() < semantic_span[1]))
        for fetch_negation in _FETCH_NEGATION_RE.finditer(context.user_text)
    ):
        return False
    source_authorities = tuple(
        authority
        for authority in _SOURCE_AUTHORITY_RE.finditer(context.user_text)
        if not _position_is_in_quoted_prose(context.user_text, authority.start())
        and (semantic_span is None or not (semantic_span[0] <= authority.start() < semantic_span[1]))
    )
    for source_authority in source_authorities:
        if source_authority.group("negative") is not None:
            return False
        source_format = source_authority.group("format")
        if source_format is not None and source_format.casefold() != source.plugin.casefold():
            return False
        source_name = source_authority.group("quoted") or source_authority.group("name")
        if source_name is not None and source_name != source.name:
            return False
        if source_authority.group("reviewed") is not None and source_format is None and source_name is None:
            return False
        if _SOURCE_AUTHORITY_ALTERNATIVE_RE.match(_match_clause_residue(context.user_text, source_authority)) is not None:
            return False

    output_authorities = tuple(
        authority
        for authority in _OUTPUT_AUTHORITY_RE.finditer(context.user_text)
        if not _position_is_in_quoted_prose(context.user_text, authority.start())
        and (semantic_span is None or not (semantic_span[0] <= authority.start() < semantic_span[1]))
    )
    for output_authority in output_authorities:
        if output_authority.group("negative") is not None:
            return False
        output_format = output_authority.group("format")
        if output_format is not None and output_format.casefold() != output_binding.format.casefold():
            return False
        output_name = output_authority.group("quoted") or output_authority.group("name")
        if output_name is not None and output_name != output.name:
            return False
        output_path = output_authority.group("path")
        if output_path is not None and output_path != output_binding.path:
            return False
        if output_authority.group("reviewed") is not None and output_format is None and output_name is None:
            return False
        if _OUTPUT_AUTHORITY_ALTERNATIVE_RE.match(_match_clause_residue(context.user_text, output_authority)) is not None:
            return False
    output_path_authorities = tuple(
        authority
        for authority in _OUTPUT_PATH_AUTHORITY_RE.finditer(context.user_text)
        if not _position_is_in_quoted_prose(context.user_text, authority.start())
        and (semantic_span is None or not (semantic_span[0] <= authority.start() < semantic_span[1]))
    )
    if output_path_authorities:
        output_paths = tuple(authority.group("path") for authority in output_path_authorities)
        if any(path != output_binding.path for path in output_paths):
            return False
    for output_authority in _OUTPUT_LEADING_AUTHORITY_RE.finditer(context.user_text):
        clause_start, _clause_end = _clause_bounds(context.user_text, output_authority.start())
        if context.user_text[clause_start : output_authority.start()].strip():
            continue
        if output_authority.group("negative") is not None:
            return False
        if output_authority.group("format").casefold() != output_binding.format.casefold():
            return False
    return True


def _match_web_scrape_project_intent(context: RecipeIntentContext) -> RecipeIntentCandidate | None:
    """Match only complete, unambiguous reviewed scrape/project requests."""

    lower = context.user_text.lower()
    if not any(term in lower for term in ("fetch", "scrape")) or "llm" not in lower:
        return None
    if not any(term in lower for term in ("keep only", "retain exactly", "retain only", "keep exactly")):
        return None
    if len(context.reviewed_sources) != 1 or len(context.reviewed_outputs) != 1:
        return None
    if len(context.source_bindings) != 1 or len(context.output_bindings) != 1:
        return None
    source = context.reviewed_sources[0]
    output = context.reviewed_outputs[0]
    source_binding = context.source_bindings[0]
    output_binding = context.output_bindings[0]
    if (source.name, source.plugin) != (source_binding.name, source_binding.plugin):
        return None
    if (output.name, output.plugin) != (output_binding.name, output_binding.plugin):
        return None
    if source.plugin not in {"csv", "json"} or output.plugin != "json" or output_binding.format != "jsonl":
        return None
    try:
        UUID(source_binding.blob_id)
    except ValueError:
        return None
    if not output_binding.path:
        return None
    if not _reviewed_web_authority_is_compatible(
        context,
        source=source,
        output=output,
        output_binding=output_binding,
    ):
        return None

    response_contract = _web_response_contract(context.user_text)
    if response_contract is None:
        return None
    response_field, prompt_template = response_contract
    locator_field = _web_locator_field(context.user_text, tuple(source.field_names))
    if locator_field is None or locator_field == response_field:
        return None
    if _web_projection_namespace_collisions(
        locator_field=locator_field,
        response_field=response_field,
        upstream_fields=tuple(source.field_names),
    ):
        return None
    explicit_retained_fields = _web_retained_fields(context.user_text)
    if explicit_retained_fields is None:
        return None
    known_fields = {*source.field_names, response_field}
    if any(field not in known_fields for field in explicit_retained_fields):
        return None
    retained_fields = explicit_retained_fields
    if not retained_fields or response_field not in retained_fields or _RAW_SCRAPE_FIELDS.intersection(retained_fields):
        return None
    profile = _single_llm_profile(context)
    if profile is None:
        return None
    abuse_contact = _web_abuse_contact(context.user_text)
    if abuse_contact is None:
        return None
    reason = _web_scraping_reason(context.user_text)
    if reason is None:
        return None
    projection_conflict = reviewed_output_projection_conflict(
        retained_fields=retained_fields,
        required_fields=tuple(output.required_fields),
    )
    if projection_conflict is not None:
        raise projection_conflict
    return RecipeIntentCandidate(
        slots={
            "source_blob_id": source_binding.blob_id,
            "source_plugin": source_binding.plugin,
            "locator_field": locator_field,
            "profile": profile,
            "prompt_template": prompt_template,
            "response_field": response_field,
            "retained_fields": retained_fields,
            "abuse_contact": abuse_contact,
            "scraping_reason": reason,
            "output_path": output_binding.path,
        }
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_RECIPES: Final[dict[str, RecipeSpec]] = {
    "classify-rows-llm-jsonl": RecipeSpec(
        name="classify-rows-llm-jsonl",
        description=(
            "Apply an LLM classifier to every row of a CSV blob and write a "
            "JSONL output with each row's classification stored in a named "
            "field. Use for: 'classify these tickets as high/medium/low', "
            "'tag these reviews as positive/negative', 'pick a category for "
            "each row'. The CSV must already be uploaded as a session blob."
        ),
        slots=_RECIPE1_SLOTS,
        build=_build_classify_recipe,
        required_plugins=frozenset({PluginId("source", "csv"), PluginId("transform", "llm"), PluginId("sink", "json")}),
    ),
    "split-by-numeric-threshold": RecipeSpec(
        name="split-by-numeric-threshold",
        description=(
            "Split CSV rows by a numeric threshold into two JSONL outputs. "
            "Coerces the field to float before comparison so a string-typed "
            "CSV column is handled correctly. Use for: 'route prices above "
            "100 to high.jsonl', 'separate scores >= 0.8 from the rest', "
            "'split orders by amount'."
        ),
        slots=_RECIPE2_SLOTS,
        build=_build_threshold_recipe,
        required_plugins=frozenset({PluginId("source", "csv"), PluginId("transform", "type_coerce"), PluginId("sink", "json")}),
    ),
    "fork-coalesce-truncate-jsonl": RecipeSpec(
        name="fork-coalesce-truncate-jsonl",
        description=(
            "Fork+coalesce: process each CSV row two ways in parallel and "
            "merge into a single output row. Path A keeps the row unchanged; "
            "path B truncates a named field to a maximum length (with optional "
            "suffix). The merged output row exposes both paths as named "
            "top-level fields. Use for: 'process each row two ways and combine', "
            "'keep the original alongside a truncated copy', 'fan out then "
            "rejoin under separate keys'. Wiring (gate.fork_to ↔ path.on_success "
            "↔ coalesce.branches naming invariants) is server-side and not the "
            "agent's responsibility."
        ),
        slots=_RECIPE3_SLOTS,
        build=_build_fork_coalesce_truncate_recipe,
        required_plugins=frozenset(
            {
                PluginId("source", "csv"),
                PluginId("transform", "passthrough"),
                PluginId("transform", "truncate"),
                PluginId("sink", "json"),
            }
        ),
        matcher=_match_fork_coalesce_intent,
    ),
    "web-scrape-llm-project-jsonl": RecipeSpec(
        name="web-scrape-llm-project-jsonl",
        description=(
            "Fetch each page named by an arbitrary source locator field, project an LLM response into an explicitly named "
            "field, remove the raw fetched content and fingerprint, retain an exact caller-supplied field set, and write "
            "JSONL through a reviewed destination. The generic prompt has a visible recipe-owned default; response and "
            "projection fields, HTTP identity, source binding, profile, and output destination are never guessed."
        ),
        slots=_RECIPE_WEB_PROJECT_SLOTS,
        build=_build_web_scrape_project_recipe,
        required_plugins=frozenset(
            {
                PluginId("transform", "web_scrape"),
                PluginId("transform", "llm"),
                PluginId("transform", "field_mapper"),
                PluginId("sink", "json"),
            }
        ),
        alternative_plugin_groups=(frozenset({PluginId("source", "csv"), PluginId("source", "json")}),),
        matcher=_match_web_scrape_project_intent,
    ),
    "web-scrape-llm-rate-jsonl": RecipeSpec(
        name="web-scrape-llm-rate-jsonl",
        description=(
            "Fetch each URL in a blob of {url: ...} rows, rate the page with an "
            "LLM, drop the raw scraped HTML and fingerprint, and write a JSONL "
            "output of url + rating. Use for: 'scrape these pages and rate them', "
            "'fetch each site and score it'. The URL list must already be uploaded "
            "as a session blob (json or csv rows with a url column). The resolved "
            "source_plugin slot preserves whether the materialised source is json "
            "or csv. The raw-HTML cleanup is staged as a pipeline_decision so the "
            "data-minimization contract passes deterministically."
        ),
        slots=_RECIPE_WEB_RATE_SLOTS,
        build=_build_legacy_web_rating_recipe,
        required_plugins=frozenset(
            {
                PluginId("transform", "web_scrape"),
                PluginId("transform", "llm"),
                PluginId("transform", "field_mapper"),
                PluginId("sink", "json"),
            }
        ),
        alternative_plugin_groups=(frozenset({PluginId("source", "csv"), PluginId("source", "json")}),),
    ),
}


def unavailable_recipe_plugin(
    recipe: RecipeSpec,
    snapshot: PluginAvailabilitySnapshot,
    *,
    raw_slots: Mapping[str, Any] | None = None,
) -> PluginId | None:
    """Return the first unavailable dependency, or ``None`` when usable."""
    for plugin_id in sorted(recipe.required_plugins):
        if plugin_id not in snapshot.available:
            return plugin_id
    if raw_slots is not None and isinstance(raw_slots.get("source_plugin"), str):
        try:
            selected_source = PluginId("source", raw_slots["source_plugin"])
        except ValueError as exc:
            raise RecipeValidationError("recipe source_plugin must be a registered source plugin id") from exc
        if selected_source not in snapshot.available:
            return selected_source
    if "profile" in recipe.slots:
        llm_id = PluginId("transform", "llm")
        usable_aliases = dict(snapshot.usable_profile_aliases).get(llm_id, ())
        if not usable_aliases:
            return llm_id
        if raw_slots is not None and isinstance(raw_slots.get("profile"), str) and raw_slots["profile"] not in usable_aliases:
            return llm_id
    for alternatives in recipe.alternative_plugin_groups:
        if alternatives.isdisjoint(snapshot.available):
            return min(alternatives)
    return None


def match_registered_recipe_intent(context: RecipeIntentContext) -> RecipeIntentMatch | None:
    """Return the unique complete executable recipe match, else fall back.

    Matchers produce sparse values: absent optional slots remain absent here so
    the recipe declaration's ordinary ``validate_slots`` path remains the sole
    owner of defaults. A malformed, incomplete, unavailable, or ambiguous
    candidate never partially applies a recipe.
    """

    if type(context) is not RecipeIntentContext:
        raise TypeError("context must be an exact RecipeIntentContext")
    complete: list[RecipeIntentMatch] = []
    for recipe in _RECIPES.values():
        matcher = recipe.matcher
        if matcher is None:
            continue
        candidate = matcher(context)
        if candidate is None:
            continue
        if type(candidate) is not RecipeIntentCandidate:
            raise TypeError(f"recipe '{recipe.name}' matcher must return RecipeIntentCandidate or None")
        missing_required = [
            name
            for name, spec in recipe.slots.items()
            if spec.required and name not in candidate.slots and not (candidate.inline_blob is not None and spec.slot_type == "blob_id")
        ]
        if missing_required:
            continue
        try:
            validation_slots = dict(candidate.slots)
            if candidate.inline_blob is not None:
                missing_blob_slots = [
                    name
                    for name, spec in recipe.slots.items()
                    if spec.required and spec.slot_type == "blob_id" and name not in validation_slots
                ]
                if len(missing_blob_slots) != 1:
                    continue
                validation_slots[missing_blob_slots[0]] = _INLINE_SOURCE_BLOB_SENTINEL
            coerced_slots = validate_slots(recipe, validation_slots)
            recipe.build(coerced_slots)
            if (
                context.policy_snapshot is not None
                and unavailable_recipe_plugin(
                    recipe,
                    context.policy_snapshot,
                    raw_slots=candidate.slots,
                )
                is not None
            ):
                continue
        except RecipeValidationError:
            continue
        complete.append(
            RecipeIntentMatch(
                recipe_name=recipe.name,
                slots=candidate.slots,
                inline_blob=candidate.inline_blob,
            )
        )
    return complete[0] if len(complete) == 1 else None


def list_recipes(snapshot: PluginAvailabilitySnapshot) -> list[dict[str, Any]]:
    """Return discovery metadata for recipes usable in this snapshot."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "slots": {
                slot_name: {
                    "type": s.slot_type,
                    "required": s.required,
                    "default": s.default,
                    "description": s.description,
                    **(
                        {"choices": list(dict(snapshot.usable_profile_aliases).get(PluginId("transform", "llm"), ()))}
                        if slot_name == "profile"
                        else {}
                    ),
                }
                for slot_name, s in spec.slots.items()
            },
        }
        for spec in _RECIPES.values()
        if unavailable_recipe_plugin(spec, snapshot) is None
    ]


def get_recipe(name: str) -> RecipeSpec | None:
    """Return a recipe spec by name, or None if not registered."""
    # ``name`` is external (composer-LLM-authored); "no such recipe" is a real
    # answer, so ``None`` is honest absence, not a fabricated default. Explicit
    # membership keeps that absence signal structural rather than a swallow.
    if name in _RECIPES:
        return _RECIPES[name]
    return None


def apply_recipe(name: str, raw_slots: Mapping[str, Any]) -> dict[str, Any]:
    """Validate slots and return the set_pipeline args for a recipe.

    Raises ``RecipeValidationError`` if the recipe is unknown or the
    slots fail validation. The returned dict is consumable directly by
    ``set_pipeline``.
    """
    # ``name`` is external (composer-LLM-authored). Convert an unknown-recipe
    # KeyError directly into the typed RecipeValidationError the caller routes,
    # preserving the exception chain.
    try:
        recipe = _RECIPES[name]
    except KeyError as exc:
        raise RecipeValidationError(f"recipe '{name}' is not registered. Available recipes: {sorted(_RECIPES)}.") from exc
    coerced = validate_slots(recipe, raw_slots)
    # Concrete recipe builders return dict; the Mapping return type on the
    # RecipeSpec contract is the looser superset (Mapping ⊇ dict). Convert
    # to the concrete dict the caller (set_pipeline executor) requires.
    return dict(recipe.build(coerced))


@cache  # Process-scoped: module source on disk is immutable for the process lifetime.
def recipe_catalog_content_hash() -> str:
    """Hex SHA-256 over recipes.py byte content.

    Cache input #5 of the tutorial run-cache key (C2). The recipe registry +
    builders here author the cached pipeline's option-level content (provider,
    model, prompt_template, response_field, schema mode, output format).
    ``_state_matches_cached_topology`` is option-blind by design and cannot
    catch that drift, so option fidelity is guaranteed by keying this module's
    source here.
    """
    recipes_path = Path(__file__)
    digest = hashlib.sha256()
    digest.update(recipes_path.read_bytes())
    return digest.hexdigest()
