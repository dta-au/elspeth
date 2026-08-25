"""LLM configuration model.

Provides LLMConfig extending TransformDataConfig with LLM-specific fields:
model, prompt_template, system_prompt, temperature, max_tokens, response_field,
and pool configuration (flat fields assembled into PoolConfig).
"""

from __future__ import annotations

import json
from typing import Any, Final, Literal

from jinja2 import TemplateSyntaxError
from pydantic import Field, field_validator, model_validator

from elspeth.contracts.hashing import stable_hash
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.pooling import PoolConfig
from elspeth.plugins.infrastructure.templates import TemplateError, create_sandboxed_environment, find_runtime_unbound_variables
from elspeth.plugins.transforms.llm.image_inputs import ImageInputConfig
from elspeth.plugins.transforms.llm.multi_query import QueryDefinition, resolve_queries
from elspeth.plugins.transforms.llm.templates import PromptTemplate

# The names PromptTemplate.render actually supplies (templates.py builds the
# context as exactly {"row": ..., "lookup": ...} in both single- and
# multi-query mode) plus the Jinja2 environment globals (range, namespace,
# ...) that resolve at render time. Any other top-level template name hits
# StrictUndefined and raises TemplateError live. Mirrors the composer-side
# constants in web/composer/state.py.
_PROMPT_CONTEXT_NAMES: frozenset[str] = frozenset({"row", "lookup"})
_PROMPT_GLOBAL_NAMES: frozenset[str] = frozenset(create_sandboxed_environment().globals)

# The one name build_template_context injects beside the query's own
# input_fields variables (multi_query.py): the full source row, reachable as
# row.source_row.<column> inside a query template.
_MULTI_QUERY_IMPLICIT_ROW_NAMES: frozenset[str] = frozenset({"source_row"})


# Single-owned by the plugin layer and imported by the composer rule, so the
# tool-call surface and the planner's repair turn cannot serve different
# remedies for one error code (elspeth-920bd88299).
#
# Rewrite-the-reference leads DELIBERATELY. Declaring the read name is correct
# only when the producer guarantees that exact spelling, and config time cannot
# tell: ``SchemaContract.find_name`` matches a field's ``normalized_name`` OR
# its ``original_name``, so ``{{ row.Name }}`` may resolve against a header
# ``Name`` while the row key is ``name`` — and ``verify_declared_required_fields``
# is a plain set difference over row keys with NO dual-name limb. Measured:
# declaring the read name there is ACCEPTED at config time and then raises
# DeclaredRequiredInputFieldsViolation on EVERY row. Leading with it would hand
# the planner a repair that clears this error and breaks the run.
_UNDECLARED_ROW_FIELDS_REMEDY: Final[str] = (
    "Rewrite each reference to a field the node already declares — that always applies, and a "
    "spelling the declaration does not carry works at best by accident of the producer's original "
    "header. Add a name to options.required_input_fields ONLY if the upstream producer guarantees "
    "that exact name (declare the parenthesised form where one is shown; the bracket literal itself "
    "is not a legal declaration entry). Declaring a name the producer does not guarantee is accepted "
    "here and then fails every row at run time. Do not empty required_input_fields to silence this: "
    "[] withdraws the contract for every field the node reads, including the unconditional ones."
)


class LLMConfig(TransformDataConfig):
    """Configuration for LLM transforms.

    Extends TransformDataConfig to get:
    - schema: Input/output schema configuration (REQUIRED)
    - required_input_fields: Fields this transform requires (optional but recommended)

    IMPORTANT: Template Field Requirements
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    If your template references row fields (e.g., {{ row.customer_id }}),
    you SHOULD declare them in `required_input_fields`. This enables DAG
    validation to catch missing fields at config time rather than runtime.

    Use the helper utility to discover fields:

        from elspeth.core.templates import extract_jinja2_fields

        fields = extract_jinja2_fields(your_template)
        # Returns: frozenset({'customer_id', 'amount'})
        # Then add to config: required_input_fields: [customer_id, amount]

    For templates with conditional logic ({% if row.x %}...{% endif %}),
    only declare the fields that are TRULY required (always accessed).

    LLM-specific fields:
    - provider: LLM provider ("azure", "openrouter", "bedrock", or "gateway")
    - model: Model identifier (optional — Azure uses deployment_name instead)
    - prompt_template: Jinja2 prompt template (required)
    - system_prompt: Optional system message
    - temperature: Sampling temperature (default 0.0 for determinism)
    - max_tokens: Maximum response tokens
    - response_field: Field name for LLM response in output
    - queries: Multi-query specs (None = single-query mode)

    Pool configuration (flat fields assembled into PoolConfig when pool_size > 1):
    - pool_size: Number of concurrent requests (1 = sequential, no pooling)
    - min_dispatch_delay_ms: Floor for delay between dispatches
    - max_dispatch_delay_ms: Ceiling for delay
    - backoff_multiplier: Multiply delay on capacity error (must be > 1)
    - recovery_step_ms: Subtract from delay on success
    - max_capacity_retry_seconds: Max time to retry capacity errors per row
    """

    provider: Literal["azure", "openrouter", "bedrock", "gateway"] = Field(..., description="LLM provider")
    model: str | None = Field(None, description="Model identifier (optional — Azure uses deployment_name)")
    queries: list[QueryDefinition] | dict[str, QueryDefinition] | None = Field(
        None, description="Multi-query specs (None = single-query mode)"
    )
    prompt_template: str = Field(..., description="Jinja2 prompt template")
    system_prompt: str | None = Field(None, description="Optional system prompt")
    temperature: float = Field(0.0, ge=0.0, le=2.0, description="Sampling temperature")
    max_tokens: int | None = Field(None, gt=0, description="Maximum tokens in response")
    response_field: str = Field("llm_response", description="Field name for LLM response in output")

    # Image inputs (docs/superpowers/specs/2026-08-25-llm-image-input-design.md §4):
    # absent (None) is exactly today's text-only behavior. Each entry names a
    # row column holding a payload-store blob ref (str or list[str]); its image
    # format comes from a literal or a per-row mime column, resolved at message
    # assembly time (image_inputs.resolve_image_parts), never here.
    # min_length=1: spec §4 requires entries "non-empty and distinct" — an
    # authored `image_inputs: []` is a mistake to catch at config-build time,
    # not a silent no-op alias for omitting the key entirely (fail-fast, per
    # AWSTextractInlineAnalysisConfig's "at least one output target" precedent).
    image_inputs: list[ImageInputConfig] | None = Field(
        None, min_length=1, description="Row columns to resolve as image message parts (absent = text-only)"
    )
    max_image_bytes: int = Field(5_242_880, gt=0, le=20_971_520, description="Per-image byte cap (hard upper bound 20 MiB)")
    max_images_per_call: int = Field(20, gt=0, description="Maximum resolved images per LLM call")

    # File-based content with source paths for audit trail
    lookup: dict[str, Any] | None = Field(None, description="Lookup data loaded from YAML file")
    prompt_template_source: str | None = Field(None, description="Prompt template file path for audit (None if inline)")
    lookup_source: str | None = Field(None, description="Lookup file path for audit (None if no lookup)")
    system_prompt_source: str | None = Field(None, description="System prompt file path for audit (None if inline)")

    # Phase 5b Task 9 — cross-DB hash anchor for interpretation events.
    # When this LLM transform is downstream of a resolved interpretation
    # event, the session service writes ``stable_hash(resolved prompt
    # template)`` here (via ``resolve_interpretation_event`` →
    # ``_patch_llm_transform_prompt`` → ``composition_states.nodes[i].options``).
    # The runtime reads this field and forwards it to every LLM call so the
    # Landscape ``calls.resolved_prompt_template_hash`` column is populated
    # — the cross-DB anchor an auditor uses to join a Landscape call back to
    # the session-DB interpretation_events row. ``None`` is the legitimate
    # value for non-interpretation LLM transforms (most LLM nodes never go
    # through an interpretation surface).
    resolved_prompt_template_hash: str | None = Field(
        None,
        description="Cross-DB hash anchor for interpretation events (Phase 5b Task 9)",
        json_schema_extra={"composer_hidden": True},
    )

    # Pool configuration fields (flat - assembled into PoolConfig by pool_config property)
    pool_size: int = Field(1, ge=1, description="Number of concurrent requests (1 = sequential)")
    min_dispatch_delay_ms: int = Field(0, ge=0, description="Minimum dispatch delay in milliseconds")
    max_dispatch_delay_ms: int = Field(5000, ge=0, description="Maximum dispatch delay in milliseconds")
    backoff_multiplier: float = Field(2.0, gt=1.0, description="Backoff multiplier on capacity error")
    recovery_step_ms: int = Field(50, ge=0, description="Recovery step in milliseconds")
    max_capacity_retry_seconds: int = Field(3600, gt=0, description="Max seconds to retry capacity errors")

    @property
    def pool_config(self) -> PoolConfig | None:
        """Get pool configuration if pooling is enabled.

        Returns None if pool_size <= 1 (sequential mode).
        Otherwise returns a PoolConfig built from flat fields.

        Returns:
            PoolConfig instance or None if sequential mode.
        """
        if self.pool_size <= 1:
            return None
        return PoolConfig(
            pool_size=self.pool_size,
            min_dispatch_delay_ms=self.min_dispatch_delay_ms,
            max_dispatch_delay_ms=self.max_dispatch_delay_ms,
            backoff_multiplier=self.backoff_multiplier,
            recovery_step_ms=self.recovery_step_ms,
            max_capacity_retry_seconds=self.max_capacity_retry_seconds,
        )

    @field_validator("response_field")
    @classmethod
    def validate_response_field(cls, v: str) -> str:
        """Validate response_field is a valid Python identifier."""
        if not v or not v.strip():
            raise ValueError("response_field must be non-empty")
        if not v.isidentifier():
            raise ValueError(f"response_field must be a valid Python identifier, got {v!r}")
        return v

    @field_validator("queries", mode="before")
    @classmethod
    def _inject_mapping_query_names(cls, value: Any) -> Any:
        """Inject the mapping key as each query's ``name`` for the mapping form.

        The two accepted authoring forms are asymmetric under ``extra=forbid``:
        a mapping value legitimately omits ``name`` (the key supplies it) while a
        list entry must carry its own. This before-validator injects the key into
        each raw mapping value so the resulting :class:`QueryDefinition` carries a
        populated ``name`` (mapping key wins over any name in the value, matching
        the historical mapping-form semantics). List input and already-typed
        values pass through untouched; the list-form ``name`` requirement is
        enforced in ``resolve_queries`` as a safe configuration error.
        """
        if isinstance(value, dict):
            injected: dict[Any, Any] = {}
            for key, definition in value.items():
                if isinstance(definition, dict):
                    injected[key] = {**definition, "name": key}
                else:
                    injected[key] = definition
            return injected
        return value

    @field_validator("prompt_template")
    @classmethod
    def validate_prompt_template(cls, v: str) -> str:
        """Validate prompt_template is non-empty and syntactically valid."""
        if not v or not v.strip():
            raise ValueError("prompt_template cannot be empty")
        # Validate template syntax at config time
        try:
            PromptTemplate(v)
        except TemplateError as e:
            raise ValueError(f"Invalid Jinja2 template: {e}") from e
        return v

    @model_validator(mode="after")
    def _validate_resolved_prompt_template_hash_matches_template(self) -> LLMConfig:
        """Refuse runtime configs whose interpretation hash anchor drifted."""
        if self.resolved_prompt_template_hash is None:
            return self

        expected_hash = stable_hash(self.prompt_template)
        if self.resolved_prompt_template_hash != expected_hash:
            raise ValueError(
                "resolved_prompt_template_hash must equal stable_hash(prompt_template); "
                f"expected {expected_hash!r}, got {self.resolved_prompt_template_hash!r}"
            )
        return self

    @model_validator(mode="after")
    def _validate_cross_query_rules(self) -> LLMConfig:
        """Run cross-query validation at config-parse time (safe-failure placement).

        The per-query shape is already enforced by ``QueryDefinition`` /
        ``OutputFieldConfig``. The *cross-query* rules — list-form name presence,
        duplicate names, reserved-suffix collisions, cross-query output-key
        collisions, and legacy positional variables — live in
        ``resolve_queries``. Running that normalizer here (rather than only later
        in ``LLMTransform.__init__``) means a malformed structured-query draft
        raises ``ValueError`` *inside* ``model_validate``, which ``from_dict``
        wraps into the redacted-safe ``PluginConfigError`` category (§5.3) instead
        of escaping as a bare ``ValueError`` / 500.

        Only *validation* moves earlier; the frozen ``QuerySpec`` runtime
        resolution still happens in ``LLMTransform.__init__`` (the second call is
        idempotent). This validator is deliberately scoped to config-shape errors:
        it delegates solely to ``resolve_queries`` and does not catch or reshape
        any exception, so a genuine framework failure would still propagate.
        """
        if self.queries is None:
            return self
        resolve_queries(self.queries)
        return self

    @model_validator(mode="after")
    def _validate_image_inputs_field_names_unique(self) -> LLMConfig:
        """Reject image_inputs entries that name the same row column twice.

        Each entry's ``field`` selects a distinct blob-ref column; a duplicate
        would resolve the same column twice into the assembled message with no
        defined ordering between the two resolutions.
        """
        if self.image_inputs is None:
            return self
        names = [spec.field for spec in self.image_inputs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"Duplicate image_inputs field names: {duplicates}. Each entry's field must be unique")
        return self

    def _field_extraction_templates(self) -> tuple[tuple[str, str], ...]:
        """Return (label, template) for every LLM Jinja2 template that can interpolate row data."""
        templates = [("prompt_template", self.prompt_template)]
        if self.queries is not None:
            for spec in resolve_queries(self.queries):
                if spec.template:
                    templates.append((f"query {spec.name!r} template", spec.template))
        return tuple(templates)

    @model_validator(mode="after")
    def _validate_dynamic_row_access_requires_explicit_opt_out(self) -> LLMConfig:
        """Fail closed when row fields are accessed through parse-time dynamic keys."""
        if self.required_input_fields == []:
            return self

        from elspeth.core.templates import extract_jinja2_field_usage

        dynamic_accesses: list[str] = []
        for label, template in self._field_extraction_templates():
            try:
                extraction = extract_jinja2_field_usage(template)
            except TemplateSyntaxError as e:
                # An unparseable template cannot be proven free of dynamic row
                # access, so it must fail here — as the structured TemplateError
                # the constructor advertises, not a raw jinja2 exception.
                raise TemplateError(f"Invalid template syntax in {label}: {e}") from e
            dynamic_accesses.extend(extraction.dynamic_accesses)

        if not dynamic_accesses:
            return self

        access_kinds = sorted(set(dynamic_accesses))
        access_examples_by_kind = {
            "attr": "row|attr(expr)",
            "get": "row.get(expr)",
            "item": "row[expr]",
            "map(attribute)": "map(attribute=expr)",
            "row-api": "row API",
        }
        access_examples = ", ".join(access_examples_by_kind[kind] for kind in access_kinds)
        raise ValueError(
            "LLM prompt_template uses dynamic row field access "
            f"({', '.join(access_kinds)} via {access_examples}). "
            "Dynamic row keys cannot be audited against options.required_input_fields. "
            "Use static row.field or row['field'] references, or set "
            "options.required_input_fields: [] to explicitly opt out and accept runtime risk."
        )

    @model_validator(mode="after")
    def _validate_required_input_fields_declared(self) -> LLMConfig:
        """Require explicit field declaration when template references row fields.

        This enforces the "explicit contracts" pattern from ELSPETH's audit philosophy.
        If a template accesses row.field, the user MUST declare what fields are required.

        In multi-query mode, required fields are derived from the union of all
        query specs' input_fields values (the row column names), plus any row
        references in the top-level template and per-query template overrides.

        Opt-out mechanism:
        - required_input_fields: [field_a, field_b]  # Declare specific requirements
        - required_input_fields: []                   # Explicit opt-out (accept runtime risk)

        Omitting required_input_fields entirely when template has row references is an error.
        This prevents "Drifting Goals" pattern where teams deploy without thinking about contracts.
        """
        # None means "not specified" - this triggers the check
        # Empty list [] means "explicit opt-out" - this is allowed
        fields_not_declared = self.required_input_fields is None

        if fields_not_declared:
            from elspeth.core.templates import extract_jinja2_fields

            if self.queries is not None:
                # Multi-query mode: required row fields are the union of all
                # query specs' input_fields values (row column names), plus
                # any row.* references in the top-level and per-query templates.
                extracted: set[str] = set()
                # Collect row column names from input_fields mappings
                for spec in resolve_queries(self.queries):
                    extracted.update(spec.input_fields.values())
                    if spec.template:
                        extracted.update(extract_jinja2_fields(spec.template))
                # Also check the top-level template for row references
                extracted.update(extract_jinja2_fields(self.prompt_template))
            else:
                # Single-query mode: detect row references in the template
                extracted = set(extract_jinja2_fields(self.prompt_template))

            if extracted:
                required_fields = sorted(extracted)
                # The suggested value must be one ``validate_field_names`` will
                # accept. A bracket read returns its literal verbatim, so
                # ``{{ row["Original Header"] }}`` extracts a name no
                # declaration can carry; suggesting it hands the planner a
                # repair that is rejected on application. Offer the canonical
                # row key such a literal resolves to at render
                # (``SchemaContract.resolve_name`` accepts either spelling),
                # and drop a name with no declarable form at all
                # (elspeth-a9ba80cb0b).
                from elspeth.plugins.sources.field_normalization import declarable_field_name

                suggested_fields = sorted(
                    {entry for entry in (declarable_field_name(name) for name in required_fields) if entry is not None}
                )
                required_fields_json = json.dumps(suggested_fields)
                raise ValueError(
                    f"LLM prompt_template references row fields {required_fields} but "
                    f"options.required_input_fields is not declared.\n\n"
                    "You must explicitly declare field requirements inside the LLM node options:\n"
                    f"  options.required_input_fields: {required_fields_json}  # Require these fields\n"
                    "  options.required_input_fields: []                    # Accept runtime risk (opt-out)\n\n"
                    "Composer repair examples:\n"
                    f'  patch_node_options({{"node_id": "<node_id>", "patch": {{"required_input_fields": {required_fields_json}}}}})\n'
                    f"  set_pipeline/upsert_node: include options.required_input_fields={required_fields_json} on the llm node.\n\n"
                    "Use extract_jinja2_fields() from elspeth.core.templates to discover fields. "
                    "This explicit declaration enables DAG validation to catch missing fields at config time."
                )
        return self

    @model_validator(mode="after")
    def _validate_required_input_fields_appear_in_template(self) -> LLMConfig:
        """Reject single-query configs that declare row-field requirements the template never uses.

        Dual of `_validate_required_input_fields_declared`. That check catches the
        "template uses row.X but contract is undeclared" footgun. This check catches
        the inverse "contract declares X but template never references row.X" footgun
        — a prompt body that does not interpolate any row data, so every row receives
        the same static prompt and the model has no per-row context to reason about.

        Scope:
        - Single-query mode only (``queries is None``). Multi-query mode flows row
          data via per-query ``input_fields`` mappings, so an empty ``row.*`` set in
          the top-level template is not by itself diagnostic.
        - Empty ``required_input_fields: []`` is the explicit opt-out and passes.
        - ``required_input_fields is None`` is handled by the sibling validator;
          this check only fires when fields are declared.
        """
        if self.queries is not None:
            return self
        if self.required_input_fields is None or len(self.required_input_fields) == 0:
            return self

        from elspeth.core.templates import extract_jinja2_fields

        template_fields = extract_jinja2_fields(self.prompt_template)
        if template_fields:
            return self

        declared = sorted(self.required_input_fields)
        declared_json = json.dumps(declared)
        example_interpolations = " ".join(f"{{{{ row.{f} }}}}" for f in declared)
        raise ValueError(
            f"LLM options.required_input_fields declares {declared} but the "
            "prompt_template does not interpolate any row.* fields. "
            "Every row would receive the same static prompt and the model would "
            "have no row-specific context to reason about.\n\n"
            "Fix one of the following:\n"
            f"  (a) Reference the declared fields inside prompt_template using "
            f"Jinja2 row-namespace syntax, e.g. {example_interpolations}\n"
            "  (b) If the fields are required for runtime presence but intentionally "
            "not interpolated into the prompt, set\n"
            "      options.required_input_fields: []   # explicit opt-out\n"
            "      and document the presence assertion elsewhere.\n\n"
            "Composer repair example:\n"
            f'  patch_node_options({{"node_id": "<node_id>", "patch": '
            f'{{"prompt_template": "<...includes {example_interpolations}...>"}}}})\n\n'
            f"Declared fields: {declared_json}. Template row.* references: []."
        )

    @model_validator(mode="after")
    def _validate_template_variable_bindings(self) -> LLMConfig:
        """Reject templates whose names may be unbound on a render path.

        ``PromptTemplate.render`` supplies exactly ``{row, lookup}`` under
        StrictUndefined in BOTH modes — multi-query rendering wraps the
        query's synthetic context (its ``input_fields`` variables plus
        ``source_row``) under ``row`` (``_execute_one_query`` →
        ``render_with_metadata``). Two config-time-provable defects:

        * a top-level name outside ``{row, lookup}`` + environment globals that
          is not definitely assigned locally before every load (covers the
          legacy positional ``{{ input_N }}`` idiom, which is a bare name);
        * in multi-query mode, a ``row.<name>`` reference outside that
          query's ``input_fields`` keys + ``{source_row}`` raises
          ``Undefined variable`` when that query renders;
        * in single-prompt mode, a ``row.<name>`` reference outside
          ``required_input_fields`` (elspeth-a9ba80cb0b). This limb is a
          CONTRACT check, not a proof of failure, and its wording must not
          borrow the multi-query branch's. A query renders a synthetic context,
          so an unbound name provably raises; single-prompt binds ``row`` to
          the WHOLE row, so an undeclared reference raises only when that
          column happens to be absent — which is exactly what the declaration
          exists to rule out. ``required_input_fields`` is the audited set the
          DAG checks against upstream guarantees and
          ``verify_declared_required_fields`` re-checks per row, so a reference
          outside it escapes both. Skipped when the declaration is ``None``
          (the sibling validator above already rejects that with row
          references present) and when it is ``[]``, the documented opt-out.
          ``undeclared_row_fields`` owns the comparison; it drops undeclarable
          bracket literals and matches case variants, so the only remedy this
          limb ever names — declare the name, or rewrite the reference — always
          clears it. A genuinely OPTIONAL read guarded by ``is defined`` /
          ``| default()`` is the one shape with no honest repair here; none
          exists in the tree, and guard analysis is deliberately not attempted
          (``{% if row.x %}`` raises where ``{% if row.x is defined %}`` does
          not, one token apart).

        Each query's effective template is its ``template`` override when
        present, else the node-level ``prompt_template``; a node-level
        template no query falls back to never renders and is not checked
        (the shipped multi-query examples carry exactly that dead slot).
        YAML-authoring twin of the composer guards emitting
        ``prompt_template_unbound_variables`` /
        ``query_template_unbound_row_fields`` — the wording here mirrors
        those messages so the planner's repair patterns match both layers.

        Defined LAST deliberately: the dynamic-access and required-fields
        validators above carry their own opt-out guidance and must keep
        primacy over a plain binding error (after-validators run in
        definition order).
        """
        from elspeth.core.templates import extract_jinja2_field_usage
        from elspeth.plugins.sources.field_normalization import describe_undeclared_row_fields, undeclared_row_fields

        env = create_sandboxed_environment()

        def unbound_top_level(template: str) -> list[str]:
            # Field validators already compile-checked both template slots,
            # so parse cannot fail here; no TemplateSyntaxError handling.
            names = find_runtime_unbound_variables(env.parse(template))
            return sorted(names - _PROMPT_CONTEXT_NAMES - _PROMPT_GLOBAL_NAMES)

        if self.queries is None:
            unbound = unbound_top_level(self.prompt_template)
            if unbound:
                names = ", ".join(f"'{name}'" for name in unbound)
                raise ValueError(
                    f"LLM prompt_template references {names}, which the prompt render context does not "
                    "define — row data is only available as 'row.<field>' and lookup data as "
                    "'lookup.<key>', so rendering fails with 'Undefined variable' at runtime and none of "
                    "the row's data reaches the model. Rewrite each name as '{{ row.<field> }}' or "
                    "'{{ lookup.<key> }}', or remove the reference."
                )
            if self.required_input_fields:
                undeclared = undeclared_row_fields(
                    extract_jinja2_field_usage(self.prompt_template).fields,
                    self.required_input_fields,
                )
                if undeclared:
                    fields = describe_undeclared_row_fields(undeclared)
                    declared_names = ", ".join(f"'{name}'" for name in sorted(self.required_input_fields))
                    raise ValueError(
                        f"LLM prompt_template reads {fields} under 'row', which "
                        f"options.required_input_fields does not declare — it declares {declared_names}. "
                        "required_input_fields IS this node's input contract: it is what the DAG checks "
                        "against the upstream producer's guarantees and what the engine verifies on every "
                        "row. A reference outside it is required by nothing, so no producer is obliged to "
                        "supply it, and a row that arrives without it fails the whole node at render with "
                        "'Undefined variable' — an unattributed template error rather than a named contract "
                        f"violation. {_UNDECLARED_ROW_FIELDS_REMEDY}"
                    )
            return self

        node_template_specs: list[str] = []
        for spec in resolve_queries(self.queries):
            if spec.template is not None:
                template = spec.template
                source_desc = f"query '{spec.name}' template"
                unbound = unbound_top_level(template)
                if unbound:
                    names = ", ".join(f"'{name}'" for name in unbound)
                    raise ValueError(
                        f"Query '{spec.name}' template references {names}, which the multi-query render "
                        "context does not define — a query template sees only 'row' (this query's "
                        "input_fields variables plus 'row.source_row') and 'lookup', so rendering fails "
                        "with 'Undefined variable' at runtime. Rewrite each name as '{{ row.<variable> }}' "
                        "where <variable> is one of this query's input_fields keys, or bind it in "
                        "input_fields first."
                    )
            else:
                template = self.prompt_template
                source_desc = "the node-level prompt_template"
                node_template_specs.append(spec.name)

            bound = frozenset(spec.input_fields)
            unbound_fields = sorted(extract_jinja2_field_usage(template).fields - bound - _MULTI_QUERY_IMPLICIT_ROW_NAMES)
            if unbound_fields:
                fields = ", ".join(f"'{name}'" for name in unbound_fields)
                bound_names = ", ".join(f"'{name}'" for name in sorted(bound))
                raise ValueError(
                    f"Query '{spec.name}' renders {source_desc}, which references {fields} under 'row', "
                    f"but this query's input_fields binds only {bound_names} (plus 'source_row'). At "
                    "render the query context contains exactly its input_fields variables, so each "
                    "unbound reference fails with 'Undefined variable' and the query errors for every "
                    "row. Add the missing variables to input_fields (template variable → row column), "
                    "rename the reference to a bound variable, or use 'row.source_row.<column>' for "
                    "direct row access."
                )

        if node_template_specs:
            unbound = unbound_top_level(self.prompt_template)
            if unbound:
                names = ", ".join(f"'{name}'" for name in unbound)
                users = ", ".join(f"'{name}'" for name in node_template_specs)
                raise ValueError(
                    f"prompt_template references {names}, which the multi-query render context does not "
                    f"define — queries without a template override ({users}) render it with 'row' bound "
                    "to their input_fields variables (plus 'row.source_row') and 'lookup', so rendering "
                    "fails with 'Undefined variable' at runtime. Rewrite each name as "
                    "'{{ row.<variable> }}' with <variable> an input_fields key of every query that uses "
                    "this template, or give those queries template overrides."
                )
        return self

    @property
    def declared_input_fields(self) -> frozenset[str]:
        """``required_input_fields`` plus every ``image_inputs`` field/format_field.

        Mirrors ``AWSTextractInlineAnalysisConfig.declared_input_fields``: an
        image input column is consumed the same as any other authored input
        column, so the DAG must see it in the requiredness contract even
        though nothing in ``prompt_template`` interpolates it.
        """
        if self.image_inputs is None:
            return super().declared_input_fields
        image_field_names = {name for spec in self.image_inputs for name in (spec.field, spec.format_field) if name is not None}
        return super().declared_input_fields | frozenset(image_field_names)
