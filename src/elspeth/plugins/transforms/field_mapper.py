"""FieldMapper transform plugin.

Renames, selects, and reorganizes row fields.

IMPORTANT: Transforms use allow_coercion=False to catch upstream bugs.
If the source outputs wrong types, the transform crashes immediately.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

from pydantic import Field, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.contract_propagation import narrow_contract_to_output
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema import FieldDefinition, SchemaConfig, declare_missing_guaranteed_fields
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.sentinels import MISSING
from elspeth.plugins.infrastructure.utils import get_nested_field
from elspeth.plugins.sources.field_normalization import ExternalHeaderError, normalize_field_name


def _names_a_row_key(name: str) -> bool:
    """Whether ``name`` is the key a row actually carries that field under.

    A header-reading source derives a row key with ``normalize_field_name``, so
    the literals that reliably name a row key are that function's FIXED POINTS.
    ``str.isidentifier`` is not that test and was the defect
    (elspeth-f262a8c678): normalization also LOWERCASES and keyword-suffixes,
    so ``'B'``, ``'Name'``, ``'userID'`` and ``'class'`` are identifiers that a
    normalized header never yields — they reach ``process`` through
    ``contract.resolve_name``, exactly like ``'First Name'``.

    This is a MAY-NOT predicate, not a proof of absence, and the name overstates
    it. A non-fixed-point CAN be a row key in its own right: headerless
    ``columns`` are used as authored and a source's ``field_mapping`` values are
    identifier-checked but never lowercased (``resolve_field_names``), so a row
    really can carry ``'B'``. Construction cannot tell the two readings apart,
    so a non-fixed-point is treated as unnameable and the declaration ABSTAINS —
    safe under either reading, which a positive claim would not be.
    ``_canonical_row_key`` fails closed on the same ambiguity for the overlap
    rule (elspeth-bb470636d1).

    A literal that normalizes to nothing names no field at all: the source
    boundary rejects such a header, so no contract can carry one. That mirrors
    ``value_transform._row_key_aliases``, whose handling this matches
    deliberately — ``ExternalHeaderError`` is answered, a bare ``ValueError``
    from an algorithm bug propagates, so the error class a bad mapping key
    raises at construction is unchanged.
    """
    try:
        return normalize_field_name(name) == name
    except ExternalHeaderError:
        return False


def _canonical_row_key(name: str) -> str:
    """The row key a mapping literal collapses to when ``process`` uses it.

    Config validation and ``process`` name fields in two different spaces, and
    that disagreement was the defect (elspeth-bb470636d1). ``process`` starts
    from ``row.to_dict()``, whose keys are the NORMALIZED names, then deletes a
    renamed source by whichever of two keys the row answers to — the literal
    when it is already in the output dict, else ``contract.resolve_name`` —
    while writing ``output[target]`` under the LITERAL target. Comparing config
    literals therefore cannot see that ``'B'`` and ``'b'`` are one field.

    This key ADDS a rejection limb; it does not replace the literal one, and
    canonicalising the identity guard alone would be a REGRESSION. A row can
    carry ``'B'`` and ``'b'`` as two distinct keys: a source's ``field_mapping``
    values bypass ``normalize_field_name`` (``resolve_field_names`` validates
    them with ``isidentifier()`` only) and headerless ``columns`` are taken as
    already-clean identifiers. So a literal ``'B'`` source may name a row key in
    its own right, and ``{'B': 'b', 'b': 'y'}`` is a real two-field rename chain
    that destroys one value — a canonical identity guard would wave it through
    as a no-op. Reject on EITHER limb: literals as before, canonical keys as
    well.

    The canonical limb rejects a non-canonical target that collides with another
    source's key (``{'a': 'B', 'b': 'c'}``) even though that shape is MEASURABLY
    lossless — two keys in, two keys out, in either order. It has to: with no
    contract at construction there is no way to know whether the row keys that
    field under ``'B'`` or under ``'b'``, and the ``'b'`` reading is
    ``{'a': 'B', 'B': 'c'}``, which does destroy a value. Fail closed.

    Two literals are keyed by themselves rather than normalized. A DOTTED name
    is a nested read, never a row key, and ``normalize_field_name`` would fold
    its dot into an underscore and invent an overlap with an unrelated sibling.
    A name that normalizes to nothing raises ``ExternalHeaderError`` and names
    no field at all, so it can alias nothing; answering that error the way
    ``_names_a_row_key`` does keeps a bad mapping key raising the same class it
    always has, and returning the literal keeps two such keys distinct. The two
    helpers differ only in their error branch, and must: ``_names_a_row_key``
    asks whether a literal IS a row key, so an unnormalizable name is False,
    while this asks which key it stands for, which is itself.

    LIMIT — this narrows the class, it does not close it. ``resolve_name`` is an
    ``original_name`` index lookup, not a call to ``normalize_field_name``, so a
    source whose ``field_mapping`` renames a header off its normalized form
    still resolves to a key no config literal predicts — raw header
    ``'Weird Header'`` under ``field_mapping: {'weird_header': 'b'}`` yields the
    row key ``'b'`` (``field_mapping`` is keyed by the EFFECTIVE name, not the
    raw one), while ``normalize_field_name('Weird Header')`` is
    ``'weird_header'``. Only a contract present at construction could see that,
    and none is.
    """
    if "." in name:
        return name
    try:
        return normalize_field_name(name)
    except ExternalHeaderError:
        return name


class FieldMapperConfig(TransformDataConfig):
    """Configuration for field mapper transform.

    Requires 'schema' in config to define input/output expectations.
    Use 'schema: {mode: observed}' for dynamic field handling.
    """

    mapping: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping from existing input field names to output field names.",
    )
    select_only: bool = Field(default=False, description="When true, emit only fields named in the mapping.")
    strict: bool = Field(default=False, description="When true, fail if any mapped source field is missing from an input row.")

    @model_validator(mode="after")
    def _reject_duplicate_targets(self) -> FieldMapperConfig:
        """Reject mappings where multiple sources map to the same target.

        Duplicate targets cause silent data loss: the last write wins,
        overwriting the value from the earlier mapping without any error.
        This also produces incorrect contract metadata (type/original_name
        lineage from the wrong source field).
        """
        if not self.mapping:
            return self
        targets: list[str] = list(self.mapping.values())
        seen: set[str] = set()
        duplicates: set[str] = set()
        for target in targets:
            if target in seen:
                duplicates.add(target)
            seen.add(target)
        if duplicates:
            # Build source->target details for the error message
            collisions: dict[str, list[str]] = {}
            for source, target in self.mapping.items():
                if target in duplicates:
                    collisions.setdefault(target, []).append(source)
            msg = (
                f"Mapping has duplicate target field names: "
                f"{', '.join(f'{t!r} <- {srcs}' for t, srcs in sorted(collisions.items()))}. "
                f"Multiple sources mapping to the same target causes silent data loss."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _reject_overlapping_rename_graphs(self) -> FieldMapperConfig:
        """Reject mappings whose targets are also sources.

        FieldMapper preserves audit lineage for renames. Swaps/chains require
        target fields to carry metadata from a different source while also
        existing as source fields themselves, which is ambiguous without a more
        expressive contract model.

        Rejects on EITHER of two readings of the mapping, because ``process``
        picks between exactly those two names when it deletes a renamed source:
        the config LITERALS, and the ``_canonical_row_key`` values. The literal
        limb is the one that has always been here — a literal like ``'B'`` can be
        a row key in its own right, so it must keep being compared as written.
        The canonical limb is what makes ``{'a': 'b', 'B': 'c'}`` the same rename
        chain as ``{'a': 'b', 'b': 'c'}``, rejected identically, when ``'B'``
        resolves to ``'b'`` instead (elspeth-bb470636d1).
        """
        if not self.mapping:
            return self

        sources = set(self.mapping)
        source_keys = {_canonical_row_key(source) for source in self.mapping}
        overlaps: dict[str, list[str]] = {}
        for source, target in self.mapping.items():
            target_key = _canonical_row_key(target)
            literal_overlap = source != target and target in sources
            canonical_overlap = _canonical_row_key(source) != target_key and target_key in source_keys
            if literal_overlap or canonical_overlap:
                described = target if literal_overlap or target_key == target else f"{target} (row key {target_key!r})"
                if described not in overlaps:
                    overlaps[described] = []
                overlaps[described].append(source)

        if overlaps:
            details = ", ".join(f"{target!r} is both target for {srcs} and source" for target, srcs in sorted(overlaps.items()))
            raise ValueError(
                "Mapping contains an overlapping rename graph: "
                f"{details}. Targets that are also sources are order-dependent and can cause silent data loss. "
                "Names are compared both as written and as the row keys they normalize to, so a source and a target "
                "that normalize alike — a case variant, a keyword, a digit-prefixed header — count as one name."
            )

        return self


class FieldMapper(BaseTransform):
    """Map, rename, and select row fields.

    Config options:
        schema: Required. Schema for input/output (use {mode: observed} for any fields)
        mapping: Dict of source_field -> target_field
            - Simple: {"old": "new"} renames old to new
            - Nested: {"meta.source": "origin"} extracts nested field
        select_only: If True, only include mapped fields (default: False)
        strict: If True, error on missing source fields (default: False)
    """

    name = "field_mapper"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:06d6c7747f672853"
    config_model = FieldMapperConfig
    usage_when_to_use: str = (
        "Use to rename, select, or drop known row fields into a stable downstream shape, including "
        "selecting only the mapped fields when an explicit projection is required."
    )
    usage_when_not_to_use: str = (
        "Not for expression evaluation or type coercion: use value_transform for calculated values "
        "and type_coerce when field values need explicit type normalization."
    )
    example_use: str = """transform:
  plugin: field_mapper
  options:
    mapping:
      id: application_id
      applicant: applicant_name
      notes: notes
    select_only: true
    schema:
      mode: observed
"""
    capability_tags: tuple[str, ...] = ("fields", "mapping", "rename", "cleanup")

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        """Minimal config for the ADR-009 backward invariant."""
        return {
            "schema": {"mode": "observed"},
            "mapping": {
                "field_mapper_probe_source": "field_mapper_probe_target",
            },
            "strict": True,
        }

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = FieldMapperConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._mapping: dict[str, str] = cfg.mapping
        self._select_only: bool = cfg.select_only
        self._strict: bool = cfg.strict
        self._schema_config = cfg.schema_config

        self.declared_output_fields = self._derive_declared_output_fields(cfg)

        # Wider than declared_output_fields: every rename target is CREATED here
        # whenever its source arrives, even when it is not GUARANTEED (non-strict
        # mode, or a source that only resolves at runtime). Requiring any of them
        # on input is the elspeth-d6eeb3a71d trap, so all of them demote.
        self._self_created_input_fields = frozenset(target for source, target in cfg.mapping.items() if source != target)

        # Field-forwarding declaration for the extras direction
        # (elspeth-15c72686f2). Without select_only, process() deep-copies the
        # whole input row and then renames within it, so every unmapped column
        # survives onto the output — including an upstream llm's
        # <response_field>_usage / _model, which a locked downstream consumer
        # rejects per-row.
        #
        # A source is REMOVED only under the exact condition process() removes
        # it: not select_only, no dot (a dotted source is a nested read that
        # leaves its root field in place), and renamed to a different target.
        # An unresolved original header removes a normalized name that only
        # `contract.resolve_name` knows at runtime, so it cannot be named
        # here — the whole declaration abstains rather than under-state the
        # removal set, which is the only direction that could produce a FALSE
        # build-time rejection of a working rename pipeline. That class is
        # every non-fixed-point of `normalize_field_name`, not just the
        # visibly messy "First Name": a case variant like "Name" or "userID"
        # deletes `name` / `userid` just as unnameably (elspeth-f262a8c678).
        # Identity mappings are NOT exempt: {"First Name": "First Name"}
        # still deletes the normalized key and writes the literal header key,
        # so the removal is equally unnameable here.
        #
        # Deliberately WITHOUT `_build_field_mapper_output_schema_config`'s
        # `source in base_guaranteed` filter: that set is the node's own
        # AUTHORED guarantees, whereas this rule is applied to fields proven
        # present by the upstream walk, which the authored config never names.
        self.forwards_input_fields = not cfg.select_only and not any(self._is_unresolved_original_source(source) for source in cfg.mapping)
        # Empty unless forwarding: a removal set is a subtraction from what this
        # node forwards, so carrying one while forwarding nothing describes
        # nothing. NodeInfo rejects the pairing at graph construction rather
        # than letting it sit unread.
        self.removed_input_fields = (
            frozenset(source for source, target in cfg.mapping.items() if source != target and self._is_static_normalized_source(source))
            if self.forwards_input_fields
            else frozenset()
        )

        self.input_schema, self.output_schema = self._create_schemas(
            cfg.schema_config,
            "FieldMapper",
            adds_fields=True,
        )
        self._output_schema_config = self._build_field_mapper_output_schema_config(cfg)

    @staticmethod
    def _is_static_normalized_source(source: str) -> bool:
        """Return True when constructor-time contract math can use source.

        Only a normalization fixed point names a row key this constructor can
        state; see ``_names_a_row_key``. Dotted sources are nested reads, not
        row keys, so they are excluded here and in the complement below.
        """
        return "." not in source and _names_a_row_key(source)

    @staticmethod
    def _is_unresolved_original_source(source: str) -> bool:
        """Return True when source may be an original header resolved only at runtime.

        The exact complement of ``_is_static_normalized_source`` over
        non-dotted sources: a literal that is not the row's own key reaches
        ``process`` through ``contract.resolve_name``, so which normalized name
        it deletes is unknowable until a row arrives.
        """
        return "." not in source and not _names_a_row_key(source)

    @classmethod
    def _mapping_target_is_guaranteed(
        cls,
        cfg: FieldMapperConfig,
        source: str,
        base_guaranteed: set[str],
    ) -> bool:
        """Whether target exists on every successful row for this mapping."""
        if cls._is_unresolved_original_source(source):
            return False
        if cfg.strict:
            return True
        return cls._is_static_normalized_source(source) and source in base_guaranteed

    @property
    def self_created_input_fields(self) -> frozenset[str]:
        """Override: every rename target may be created, not just the guaranteed ones."""
        return self._self_created_input_fields

    @classmethod
    def _derive_declared_output_fields(cls, cfg: FieldMapperConfig) -> frozenset[str]:
        """Derive targets safe for executor-level declared-output checks."""
        base_guaranteed = set(cfg.schema_config.guaranteed_fields or ())
        return frozenset(
            target
            for source, target in cfg.mapping.items()
            if source != target and cls._mapping_target_is_guaranteed(cfg, source, base_guaranteed)
        )

    def backward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        """Exercise the real rename/drop path for the backward invariant."""
        return [
            self._augment_invariant_probe_row(
                probe,
                field_name="field_mapper_probe_source",
                value="mapped",
            )
        ]

    @classmethod
    def _project_field_declarations_onto_output(cls, cfg: FieldMapperConfig) -> tuple[FieldDefinition, ...] | None:
        """Re-express the AUTHORED INPUT field declarations as the EMITTED shape.

        ``cfg.schema_config`` is the INPUT contract — see
        ``BaseTransform._build_output_schema_config``, whose ``schema_config``
        argument is documented as "the transform's input schema config (base
        fields)". field_mapper is REDUCTIVE: ``process`` keeps only the mapping
        targets under ``select_only``, and deletes a renamed source in BOTH
        modes. Copying the authored declarations onto the output therefore left
        consumed-but-never-emitted fields marked ``required`` on output;
        ``get_effective_guaranteed_fields`` unions required declared fields in,
        so the transform's own contract rejected its own emitted row with
        ``SchemaConfigModeViolation`` (elspeth-a2bf676e6f). That is the
        reductive-override obligation ``BaseTransform._build_output_schema_config``
        documents, and the same defect ``BatchStats`` closed for aggregations
        (elspeth-f5f798f797).

        A rename moves the value unchanged, so the target INHERITS the source's
        authored declaration instead of degrading to the ``any``-typed
        placeholder ``declare_missing_guaranteed_fields`` would otherwise append.

        Returns ``None`` for observed-style schemas, which carry no explicit
        declarations to project.
        """
        fields = cfg.schema_config.fields
        if fields is None:
            return None

        authored = {field.name: field for field in fields}
        projected: dict[str, FieldDefinition] = {}

        # Emitted targets first, so a target that collides with an unmapped
        # field of the same name is described by the value that lands there —
        # which for a rename is the SOURCE's value, hence the source's
        # declaration. An author may declare either side, so a declaration
        # written against the emitted name is the fallback when the source
        # carries none.
        for source, target in cfg.mapping.items():
            if source == target:
                declaration = authored[source] if source in authored else None
            else:
                source_field = authored[source] if source in authored else None
                if source_field is not None:
                    declaration = replace(source_field, name=target)
                else:
                    declaration = authored[target] if target in authored else None
            if declaration is not None:
                projected[target] = declaration

        # The deep-copy branch forwards every unmapped column.
        if not cfg.select_only:
            # An unresolved original header deletes a normalized key this
            # constructor cannot name, so we cannot say WHICH forwarded column
            # stopped being emitted. Dropping them all is NOT the safe
            # direction: ``fields`` feeds two opposed limbs — declared-REQUIRED
            # names union into ``get_effective_guaranteed_fields`` (so
            # over-declaring promises rows that may not arrive), while the
            # declared NAMES are the fixed-mode extras allow-list in
            # ``verify_schema_config_mode._allowed_declared_fields`` (so
            # under-declaring rejects rows that do). Declaring the forwarded
            # columns OPTIONAL satisfies both: the name stays admissible, the
            # guarantee is not made.
            unnameable_removal = any(cls._is_unresolved_original_source(source) for source in cfg.mapping)
            renamed_away = {source for source, target in cfg.mapping.items() if source != target}
            for field in fields:
                if field.name in projected or field.name in renamed_away:
                    continue
                projected[field.name] = replace(field, required=False) if unnameable_removal else field

        return tuple(projected.values())

    def _build_field_mapper_output_schema_config(self, cfg: FieldMapperConfig) -> SchemaConfig:
        """Build output schema config reflecting the mapped output shape.

        FieldMapper is shape-changing: it removes source fields and adds target fields.
        The base _build_output_schema_config() incorrectly copies input fields into
        output guarantees. This method builds the correct output field set.

        When select_only=True: output guarantees are ONLY the mapping targets.
        When select_only=False: output guarantees are input fields MINUS removed
            sources PLUS new targets.
        """
        base_guaranteed = set(cfg.schema_config.guaranteed_fields or ())
        guaranteed_targets = {
            target for source, target in cfg.mapping.items() if self._mapping_target_is_guaranteed(cfg, source, base_guaranteed)
        }

        if cfg.select_only:
            # Only mapped targets appear in output
            output_fields = guaranteed_targets
        else:
            # Input fields minus removed sources plus new targets.
            # A source is removed from output when it's renamed to a different
            # target — or identity-mapped from an original header, which
            # deletes the normalized key and rewrites the literal header key,
            # removing a field this constructor cannot name.
            has_unresolved_original_removal = any(self._is_unresolved_original_source(source) for source in cfg.mapping)
            if has_unresolved_original_removal:
                passthrough_fields: set[str] = set()
            else:
                removed_sources = {
                    source
                    for source, target in cfg.mapping.items()
                    if source != target and self._is_static_normalized_source(source) and source in base_guaranteed
                }
                passthrough_fields = base_guaranteed - removed_sources
            output_fields = passthrough_fields | guaranteed_targets

        # Always include declared_output_fields (targets that aren't also sources)
        output_fields |= self.declared_output_fields

        # Preserve None-vs-empty-tuple semantics: None = abstain, () = explicitly empty.
        # If upstream declared guarantees or we computed non-empty output, declare explicitly.
        upstream_declared = cfg.schema_config.guaranteed_fields is not None
        if upstream_declared or output_fields:
            guaranteed_fields_result = tuple(sorted(output_fields))
        else:
            guaranteed_fields_result = None

        return SchemaConfig(
            mode=cfg.schema_config.mode,
            # Mapping targets are guaranteed on output but absent from the
            # authored input fields; declare them so the config satisfies the
            # guaranteed-fields-are-declared invariant (elspeth-97487736ca).
            # The projection below is what makes those authored declarations
            # describe the EMITTED shape rather than the consumed one
            # (elspeth-a2bf676e6f).
            fields=declare_missing_guaranteed_fields(
                self._project_field_declarations_onto_output(cfg),
                guaranteed_fields_result,
            ),
            guaranteed_fields=guaranteed_fields_result,
            audit_fields=cfg.schema_config.audit_fields,
            required_fields=cfg.schema_config.required_fields,
        )

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        """Apply field mapping to row.

        Args:
            row: Input row data
            ctx: Plugin context

        Returns:
            TransformResult with mapped row data

        Raises:
            PluginContractViolation: Raised by executor if row fails input schema
                validation. This indicates a bug in the upstream source/transform.
        """
        # Keep a normalized dict view only for validation and dotted-path lookups.
        row_data = row.to_dict()

        # Start with empty or copy depending on select_only
        if self._select_only:
            output: dict[str, Any] = {}
        else:
            output = copy.deepcopy(row_data)

        # Apply mappings
        applied_mappings: dict[str, str] = {}
        for source, target in self._mapping.items():
            if "." in source:
                # Dotted-path navigation is an operation on Tier-2 row values, not a
                # type-contract check. Contracts are flat (no nested-shape guarantee),
                # so a PRESENT non-dict intermediate (e.g. ``user`` is a str when the
                # mapping expects ``user.name``) is operation-unsafe data, not an
                # upstream type-contract violation. Route the offending row to on_error
                # — recorded and attributable — rather than raising and crashing the
                # whole run on a single malformed nested value. A genuinely absent
                # intermediate still returns MISSING (handled below), preserving the
                # strict/non-strict distinction for true absence.
                try:
                    value = get_nested_field(row_data, source)
                except TypeError as exc:
                    return TransformResult.error(
                        {
                            "reason": "type_mismatch",
                            "field": source,
                            "error": str(exc),
                            "message": f"Dotted-path field '{source}' is not navigable on this row: {exc}",
                        }
                    )
            elif source in row:
                value = row[source]
            else:
                value = MISSING

            if value is MISSING:
                if self._strict:
                    return TransformResult.error(
                        {"reason": "missing_field", "field": source, "message": f"Required field '{source}' not found in row"}
                    )
                continue  # Skip missing fields in non-strict mode

            # Remove old key if it exists (for rename within same dict)
            if not self._select_only and "." not in source and source in row:
                if source in output:
                    del output[source]
                else:
                    normalized_source = row.contract.resolve_name(source)
                    if normalized_source in output:
                        del output[normalized_source]

            output[target] = value
            applied_mappings[source] = target

        # Track field changes
        fields_modified: list[str] = []
        fields_added: list[str] = []
        for target in applied_mappings.values():
            if target in row:
                fields_modified.append(target)
            else:
                fields_added.append(target)

        # Update contract to reflect field mapping (renames and removals).
        # Dotted sources are nested extractions, not renames: the root field
        # survives in the output and the extracted value's type is unrelated
        # to the root's contract, so those targets must be inferred from the
        # output value. Contracts are flat — presenting "meta.source" as a
        # rename source would (correctly) fail narrow_contract_to_output's
        # unknown-source invariant.
        renamed_fields = {source: target for source, target in applied_mappings.items() if "." not in source}
        output_contract = narrow_contract_to_output(
            input_contract=row.contract,
            output_row=output,
            renamed_fields=renamed_fields,
        )
        output_contract = self._apply_declared_output_field_contracts(output_contract)
        output_contract = self._align_output_contract(output_contract)

        return TransformResult.success(
            PipelineRow(output, output_contract),
            success_reason={
                "action": "mapped",
                "fields_modified": fields_modified,
                "fields_added": fields_added,
            },
        )

    def close(self) -> None:
        """No resources to release."""
        pass

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name="field_mapper",
                issue_code=None,
                summary="Rename, drop, or reorder row fields. Stateless and shape-changing — declares new field names in output_schema.",
                composer_hints=(
                    "Config keys are 'mapping' (dict of source->target), 'select_only' (bool, default false), 'strict' (bool, default false), plus 'schema'. Rename with {old: new}; keep a field with {x: x}; drop a field by omitting it under select_only: true.",
                    "field_mapper has no 'drop'/'include'/'rename_only' keys — dropping is done by omitting the field under select_only: true.",
                    "Use select_only: true when cleanup means 'save only these fields'; with select_only true, mapping should whitelist exactly the saved output fields.",
                    "A select_only whitelist must preserve every field required by the downstream sink; include each required field as a mapping target before routing the mapper to that sink.",
                    "For scraped-content cleanup before a user-facing sink, field_mapper is the only utility transform that actually removes raw fields. Place it immediately before the sink and omit raw content and fingerprint fields from mapping.",
                    "For web_scrape content enriched by an LLM and saved without raw page bodies, the final topology is source -> web_scrape -> llm -> field_mapper(cleanup) -> sink. A JSON sink named cleanup is not a cleanup transform.",
                    "A validator-valid direct route from web_scrape or an LLM to the sink is still incomplete when raw scraped-content cleanup is required; insert or restore this field_mapper immediately before the sink.",
                    "A cleanup stream name is not a cleanup node; if an upstream producer points to a cleanup stream, create a field_mapper that consumes it before stopping, and do not offer to repair it later.",
                    "For final cleanup routing, set the upstream LLM or scraper on_success to the cleanup mapper, and set the cleanup mapper on_success to the sink.",
                    "If an LLM routes directly to a JSON sink whose name sounds like cleanup, cleanup is still missing; the LLM passes through raw scrape fields until this field_mapper whitelists them.",
                    "If a final cleanup field_mapper points to an intermediate stream with no downstream node, route the mapper directly to the existing sink by setting on_success to the sink name or by using an on_success edge.",
                    "When a final cleanup field_mapper points to an intermediate stream with no downstream node, do not remove the cleanup mapper or output to clear the validation error.",
                    "A field_mapper before web_scrape or before raw scraped fields exist cannot satisfy scraped-content cleanup; source-shaping mappers are separate from final cleanup.",
                    "Final cleanup should preserve requested enrichment, extraction, scoring, or LLM response fields unless the user explicitly asked to drop them.",
                    "If the user already asked to remove, drop, exclude, or avoid saving raw scrape fields, that request is the authorization and requirement to add the cleanup field_mapper; do not ask whether to add cleanup later.",
                    "For scraped-content cleanup review, use user_term 'drop_raw_html_fields' even when the configured raw body field is named content, html, raw_html, or another page-body field.",
                    "When field_mapper implements a cleanup, retention, drop, or output-shaping choice that the user did not spell out mechanically, stage a pipeline_decision interpretation requirement on that field_mapper node before set_pipeline and request its review after mutation succeeds.",
                    "The pipeline_decision review records the row-shaping decision for audit; it is not permission to omit the cleanup node.",
                    "naming a sink or output like cleanup, filtered, or final does not clean data; only a field_mapper row-shaping transform changes which fields reach the sink.",
                    "Renames are pure transformations — no coercion happens here. Use type_coerce for type changes.",
                    "If the downstream consumer expects a specific field name not in the source, field_mapper is the right tool.",
                ),
            )
        return None
