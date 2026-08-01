# tests/plugins/llm/test_templates.py
"""Tests for Jinja2 prompt template engine."""

import hashlib

import pytest

from elspeth.contracts.schema_contract import SchemaContract
from elspeth.plugins.infrastructure.templates import TemplateError
from elspeth.plugins.transforms.llm.templates import PromptTemplate
from elspeth.testing import make_field, make_row


class TestPromptTemplate:
    """Tests for PromptTemplate wrapper."""

    def test_simple_variable_substitution(self) -> None:
        """Basic variable substitution works."""
        template = PromptTemplate("Hello, {{ row.name }}!")
        result = template.render({"name": "World"})
        assert result == "Hello, World!"

    def test_template_with_loop(self) -> None:
        """Jinja2 loops work."""
        template = PromptTemplate(
            """
Analyze these entries:
{% for entry in row.entries %}
- {{ entry.name }}: {{ entry.value }}
{% endfor %}
""".strip()
        )
        result = template.render(
            {
                "entries": [
                    {"name": "A", "value": 1},
                    {"name": "B", "value": 2},
                ]
            }
        )
        assert "- A: 1" in result
        assert "- B: 2" in result

    def test_template_with_default_filter(self) -> None:
        """Jinja2 default filter works."""
        template = PromptTemplate("Focus: {{ row.focus | default('general') }}")
        assert template.render({}) == "Focus: general"
        assert template.render({"focus": "quality"}) == "Focus: quality"

    def test_template_hash_is_stable(self) -> None:
        """Same template string produces same hash."""
        t1 = PromptTemplate("Hello, {{ name }}!")
        t2 = PromptTemplate("Hello, {{ name }}!")
        assert t1.template_hash == t2.template_hash

    def test_different_templates_have_different_hashes(self) -> None:
        """Different templates have different hashes."""
        t1 = PromptTemplate("Hello, {{ name }}!")
        t2 = PromptTemplate("Goodbye, {{ name }}!")
        assert t1.template_hash != t2.template_hash

    def test_render_returns_metadata(self) -> None:
        """render() returns prompt and audit metadata."""
        template = PromptTemplate("Analyze: {{ row.text }}")
        result = template.render_with_metadata({"text": "sample"})

        assert result.prompt == "Analyze: sample"
        assert result.template_hash is not None
        assert result.variables_hash is not None
        assert result.rendered_hash is not None

    def test_static_render_uses_lookup_without_row_binding(self) -> None:
        """Static source prompts receive lookup data but never a synthetic row."""
        template = PromptTemplate(
            "Summarise {{ lookup.topic }}",
            template_source="prompts/source.j2",
            lookup_data={"topic": "audit"},
            lookup_source="prompts/lookup.yaml",
        )

        result = template.render_static_with_metadata()

        assert result.prompt == "Summarise audit"
        assert result.variables_hash == hashlib.sha256(b"{}").hexdigest()
        assert result.rendered_hash == hashlib.sha256(b"Summarise audit").hexdigest()
        assert result.template_source == "prompts/source.j2"
        assert result.lookup_hash == template.lookup_hash
        assert result.lookup_source == "prompts/lookup.yaml"
        assert result.contract_hash is None

    def test_static_render_does_not_inject_empty_row(self) -> None:
        template = PromptTemplate("{{ row.text | default('synthetic') }}")

        with pytest.raises(TemplateError, match="row"):
            template.render_static_with_metadata()

    def test_static_render_wraps_arithmetic_failure(self) -> None:
        template = PromptTemplate("{{ 1 / 0 }}")

        with pytest.raises(TemplateError, match="Template rendering failed"):
            template.render_static_with_metadata()

    def test_row_render_wraps_arithmetic_failure(self) -> None:
        template = PromptTemplate("{{ 1 / row.zero }}")

        with pytest.raises(TemplateError, match="Template rendering failed"):
            template.render({"zero": 0})

    def test_render_with_metadata_accepts_pipeline_row(self) -> None:
        """render_with_metadata() accepts PipelineRow inputs directly."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(make_field("prompt_text", str, original_name="Prompt Text"),),
            locked=True,
        )
        row = make_row({"prompt_text": "sample"}, contract=contract)
        template = PromptTemplate("Analyze: {{ row['Prompt Text'] }}")

        result = template.render_with_metadata(row, contract=contract)

        assert result.prompt == "Analyze: sample"
        assert result.variables_hash is not None

    def test_undefined_variable_raises_error(self) -> None:
        """Missing required variable raises TemplateError."""
        template = PromptTemplate("Hello, {{ row.name }}!")
        with pytest.raises(TemplateError, match="name"):
            template.render({})  # No 'name' provided in row

    def test_sandboxed_prevents_dangerous_operations(self) -> None:
        """Sandboxed environment blocks dangerous operations."""
        # Attempt to access dunder attributes (blocked by SandboxedEnvironment)
        dangerous = PromptTemplate("{{ ''.__class__.__mro__ }}")
        # SecurityError is wrapped in TemplateError with "Sandbox violation" message
        with pytest.raises(TemplateError, match="Sandbox violation"):
            dangerous.render({})

    def test_rendered_prompt_includes_source_metadata(self) -> None:
        """RenderedPrompt includes template and lookup source paths."""
        template = PromptTemplate(
            "Hello, {{ row.name }}!",
            template_source="prompts/greeting.j2",
            lookup_data={"greetings": ["Hi", "Hello"]},
            lookup_source="prompts/lookups.yaml",
        )
        result = template.render_with_metadata({"name": "World"})

        assert result.template_source == "prompts/greeting.j2"
        assert result.lookup_hash is not None
        assert result.lookup_source == "prompts/lookups.yaml"

    def test_lookup_simple_access(self) -> None:
        """Templates can access lookup data."""
        template = PromptTemplate(
            "Category: {{ lookup.categories[0] }}",
            lookup_data={"categories": ["Electronics", "Clothing", "Food"]},
        )
        result = template.render({})
        assert result == "Category: Electronics"

    def test_lookup_two_dimensional(self) -> None:
        """Templates can do two-dimensional lookups: lookup.X[row.Y]."""
        template = PromptTemplate(
            "Tone: {{ lookup.tones[row.tone_id] }}",
            lookup_data={"tones": {"0": "formal", "1": "casual", "2": "technical"}},
        )
        result = template.render({"tone_id": "1"})
        assert result == "Tone: casual"

    def test_lookup_missing_key_raises_error(self) -> None:
        """Missing lookup key raises TemplateError (strict mode)."""
        template = PromptTemplate(
            "Category: {{ lookup.categories[row.cat_id] }}",
            lookup_data={"categories": {"0": "A", "1": "B"}},
        )
        with pytest.raises(TemplateError):
            template.render({"cat_id": "99"})  # No key "99"

    def test_lookup_iteration(self) -> None:
        """Templates can iterate over lookup data."""
        template = PromptTemplate(
            """Categories:
{% for cat in lookup.categories %}
- {{ cat.name }}
{% endfor %}""",
            lookup_data={
                "categories": [
                    {"name": "Electronics"},
                    {"name": "Clothing"},
                ]
            },
        )
        result = template.render({})
        assert "- Electronics" in result
        assert "- Clothing" in result

    def test_lookup_hash_is_stable(self) -> None:
        """Same lookup data produces same hash."""
        data = {"categories": ["A", "B", "C"]}
        t1 = PromptTemplate("{{ lookup.categories }}", lookup_data=data)
        t2 = PromptTemplate("{{ lookup.categories }}", lookup_data=data)
        assert t1.lookup_hash == t2.lookup_hash

    def test_lookup_top_level_mutation_after_init_does_not_change_render_or_hash(self) -> None:
        """Lookup is snapshotted at init so external mutation cannot desync audit hashes."""
        lookup = {"k": "v1"}
        template = PromptTemplate("Value: {{ lookup.k }}", lookup_data=lookup)
        initial_hash = template.lookup_hash

        lookup["k"] = "v2"
        result = template.render_with_metadata({"row_id": "r1"})

        assert result.prompt == "Value: v1"
        assert result.lookup_hash == initial_hash

    def test_lookup_nested_mutation_after_init_does_not_change_render_or_hash(self) -> None:
        """Nested lookup structures are snapshotted at init for audit consistency."""
        lookup = {"meta": {"tone": "formal"}}
        template = PromptTemplate("Tone: {{ lookup.meta.tone }}", lookup_data=lookup)
        initial_hash = template.lookup_hash

        lookup["meta"]["tone"] = "casual"
        result = template.render_with_metadata({"row_id": "r1"})

        assert result.prompt == "Tone: formal"
        assert result.lookup_hash == initial_hash

    def test_no_lookup_has_none_hash(self) -> None:
        """Template without lookup data has None lookup_hash."""
        template = PromptTemplate("Hello, {{ row.name }}!")
        assert template.lookup_hash is None
        assert template.lookup_source is None

    def test_empty_lookup_has_hash(self) -> None:
        """Template with empty lookup_data={} still gets a hash.

        We distinguish None (no lookup configured) from {} (empty lookup).
        An empty lookup is still a valid configuration that should be auditable.
        Per CLAUDE.md: "No inference - if it's not recorded, it didn't happen."
        """
        template = PromptTemplate("Hello, {{ row.name }}!", lookup_data={})
        assert template.lookup_hash is not None  # Empty dict still gets hashed
        assert template.lookup_source is None  # No source file specified


class TestImmutableSandboxPreventsLookupMutation:
    """Regression tests for Phase 0 fix: template mutation via lookup.update().

    Bug: SandboxedEnvironment allowed dict mutation methods like .update()
    on lookup data passed to templates. This meant templates could silently
    mutate audit-tracked lookup data, breaking hash integrity.

    Fix: Changed SandboxedEnvironment to ImmutableSandboxedEnvironment,
    which blocks all mutation methods on containers.
    """

    def test_lookup_update_raises_template_error(self) -> None:
        """Calling lookup.update() in template raises TemplateError.

        Before the fix, this would silently succeed, mutating the lookup dict
        and invalidating the lookup_hash recorded at template init.
        """
        template = PromptTemplate(
            "{{ lookup.update({'injected': true}) }}",
            lookup_data={"safe_key": "safe_value"},
        )
        with pytest.raises(TemplateError):
            template.render({})

    def test_lookup_pop_raises_template_error(self) -> None:
        """Calling lookup.pop() in template raises TemplateError."""
        template = PromptTemplate(
            "{{ lookup.pop('key') }}",
            lookup_data={"key": "value"},
        )
        with pytest.raises(TemplateError):
            template.render({})

    def test_lookup_setdefault_raises_template_error(self) -> None:
        """Calling lookup.setdefault() in template raises TemplateError."""
        template = PromptTemplate(
            "{{ lookup.setdefault('new_key', 'injected') }}",
            lookup_data={"key": "value"},
        )
        with pytest.raises(TemplateError):
            template.render({})

    def test_row_dict_mutation_blocked(self) -> None:
        """Calling update on row data dict is also blocked by immutable sandbox."""
        template = PromptTemplate("{{ row.update({'injected': true}) }}")
        with pytest.raises(TemplateError):
            template.render({"safe": "data"})


class TestPromptTemplateCanonicalSafety:
    """Tests for canonicalization error handling in render_with_metadata().

    Regression tests for P2-2026-01-31-azure-canonicalization-crash:
    Row data containing NaN or Infinity should raise TemplateError
    (row-scoped failure) instead of crashing the entire pipeline.
    """

    def test_nan_in_row_raises_template_error(self) -> None:
        """NaN in row data raises TemplateError, not ValueError.

        Per Three-Tier Trust Model: row data failures should be quarantined,
        not crash the entire run. TemplateError is caught by transform plugins
        and converted to TransformResult.error().
        """
        template = PromptTemplate("Value: {{ row.value }}")
        row = {"value": float("nan")}

        with pytest.raises(TemplateError, match="Cannot compute variables hash"):
            template.render_with_metadata(row)

    def test_infinity_in_row_raises_template_error(self) -> None:
        """Infinity in row data raises TemplateError, not ValueError."""
        template = PromptTemplate("Value: {{ row.value }}")
        row = {"value": float("inf")}

        with pytest.raises(TemplateError, match="Cannot compute variables hash"):
            template.render_with_metadata(row)

    def test_negative_infinity_in_row_raises_template_error(self) -> None:
        """Negative infinity in row data raises TemplateError, not ValueError."""
        template = PromptTemplate("Value: {{ row.value }}")
        row = {"value": float("-inf")}

        with pytest.raises(TemplateError, match="Cannot compute variables hash"):
            template.render_with_metadata(row)

    def test_nan_in_nested_structure_raises_template_error(self) -> None:
        """NaN in nested dict/list raises TemplateError."""
        template = PromptTemplate("Data: {{ row.data }}")
        row = {"data": {"nested": [1, 2, float("nan"), 4]}}

        with pytest.raises(TemplateError, match="Cannot compute variables hash"):
            template.render_with_metadata(row)

    def test_normal_render_still_works(self) -> None:
        """Normal row data still renders correctly (sanity check)."""
        template = PromptTemplate("Name: {{ row.name }}, Age: {{ row.age }}")
        row = {"name": "Alice", "age": 30}

        result = template.render_with_metadata(row)

        assert result.prompt == "Name: Alice, Age: 30"
        assert result.variables_hash is not None
        assert result.template_hash is not None

    def test_render_without_metadata_unaffected(self) -> None:
        """render() (without metadata) is unaffected by NaN since it doesn't canonicalize.

        The rendering itself works - it's only the hash computation that fails.
        This confirms the bug is specifically in render_with_metadata().
        """
        template = PromptTemplate("Value: {{ row.value }}")
        row = {"value": float("nan")}

        # render() works (doesn't compute hash)
        result = template.render(row)
        assert result == "Value: nan"

        # render_with_metadata() fails (computes hash)
        with pytest.raises(TemplateError, match="Cannot compute variables hash"):
            template.render_with_metadata(row)


class TestSyntheticContextWithFixedContract:
    """Regression tests for multi-query template rendering with FIXED contracts.

    Bug: MultiQueryStrategy._execute_one_query passes contract=row.contract to
    render_with_metadata(). The template context is a synthetic dict built from
    input_fields (keys are template variable names, not source column names).
    In FIXED mode, PipelineRow rejects any key not in the contract, causing
    TemplateError for valid multi-query templates.

    The fix: pass contract=None for synthetic template contexts, since they
    don't conform to the source row's schema.
    """

    def test_synthetic_context_without_contract_succeeds(self) -> None:
        """Synthetic template context renders correctly with contract=None.

        Multi-query template contexts have keys like "text_content" (template
        variable names from input_fields), not source columns like "cs1_bg".
        Passing contract=None avoids wrapping in PipelineRow, which would
        reject these synthetic keys in FIXED mode.
        """
        template = PromptTemplate("Evaluate: {{ row.text_content }}")

        # Synthetic context from build_template_context: maps template vars to row values
        synthetic_ctx = {"text_content": "sample data", "source_row": "ignored"}

        # With contract=None, rendering succeeds (the fix)
        result = template.render_with_metadata(synthetic_ctx, contract=None)
        assert "sample data" in result.prompt

    def test_synthetic_context_with_fixed_contract_render(self) -> None:
        """render() with synthetic context and FIXED contract should also work."""
        fixed_contract = SchemaContract(
            mode="FIXED",
            fields=(make_field("amount", int, required=True, source="declared"),),
            locked=True,
        )
        template = PromptTemplate("Amount is {{ row.value }}")
        synthetic_ctx = {"value": 42}

        # With contract=None, this works
        result = template.render(synthetic_ctx, contract=None)
        assert result == "Amount is 42"

        # With FIXED contract, this WOULD fail because "value" is not in contract
        with pytest.raises(TemplateError, match="Undefined variable"):
            template.render(synthetic_ctx, contract=fixed_contract)

    def test_synthetic_context_with_flexible_contract_works(self) -> None:
        """FLEXIBLE contract allows synthetic keys (extra fields fall through)."""
        flexible_contract = SchemaContract(
            mode="FLEXIBLE",
            fields=(make_field("cs1_bg", str, required=True, source="declared"),),
            locked=True,
        )
        template = PromptTemplate("Evaluate: {{ row.text_content }}")
        synthetic_ctx = {"text_content": "sample data"}

        # FLEXIBLE mode allows unknown keys — no bug in this mode
        result = template.render(synthetic_ctx, contract=flexible_contract)
        assert "sample data" in result
