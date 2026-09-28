# tests/unit/plugins/llm/test_llm_config.py
"""Tests for unified LLM config models (Task 8).

Tests the new provider-dispatched LLMConfig, domain-agnostic QuerySpec,
resolve_queries() normalization, and provider-specific config classes.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.contracts.schema import SchemaConfig
from elspeth.core.prompt_artifact import approved_prompt_artifact_hash
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.plugins.transforms.llm.multi_query import QueryDefinition
from elspeth.testing import make_pipeline_row


def _mapping_defs(defs: dict[str, dict[str, Any]]) -> dict[str, QueryDefinition]:
    """Build the typed mapping form of ``queries`` (value carries no ``name``)."""
    return {name: QueryDefinition(**spec) for name, spec in defs.items()}


# Shared observed schema for test convenience
_OBSERVED_SCHEMA = SchemaConfig(mode="observed", fields=None)

# A valid OpenRouter catalog model id (the retired anthropic/claude-3-opus was
# dropped from the litellm-derived catalog; OpenRouterConfig now rejects models
# absent from it). Mirrors test_openrouter.py.
_OPENROUTER_MODEL = "anthropic/claude-3.5-sonnet"


# ---------------------------------------------------------------------------
# LLMConfig base changes
# ---------------------------------------------------------------------------


class TestLLMConfigBase:
    """Tests for LLMConfig base class changes."""

    def test_config_validation_rejects_constant_power_template(self) -> None:
        with pytest.raises(ValidationError, match="Invalid Jinja2 template"):
            LLMConfig(
                provider="azure",
                prompt_template="{{ (3**(3**15)) % 7 }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=[],
            )

    def test_model_optional_defaults_to_none(self) -> None:
        """model field is optional and defaults to None."""
        config = LLMConfig(
            provider="azure",
            prompt_template="Classify: {{ row.text }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
        )
        assert config.model is None

    def test_model_accepts_explicit_value(self) -> None:
        config = LLMConfig(
            provider="azure",
            model="gpt-4o",
            prompt_template="Classify: {{ row.text }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
        )
        assert config.model == "gpt-4o"

    def test_provider_field_required(self) -> None:
        """provider field is required — Literal["azure", "openrouter"]."""
        with pytest.raises(ValidationError):
            LLMConfig(
                provider="invalid_provider",
                prompt_template="hello",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=[],
            )

    def test_provider_azure_accepted(self) -> None:
        config = LLMConfig(
            provider="azure",
            prompt_template="hello {{ row.text }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
        )
        assert config.provider == "azure"

    def test_provider_openrouter_accepted(self) -> None:
        config = LLMConfig(
            provider="openrouter",
            prompt_template="hello {{ row.text }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
        )
        assert config.provider == "openrouter"

    def test_queries_field_none_by_default(self) -> None:
        """queries is None when not provided (single-query mode)."""
        config = LLMConfig(
            provider="azure",
            prompt_template="hello {{ row.text }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
        )
        assert config.queries is None

    def test_approved_prompt_artifact_hash_must_match_prompt_template(self) -> None:
        """Phase 5b runtime anchor refuses prompt/hash drift at config load."""
        resolved_template = "Rate how innovative this is."
        config = LLMConfig(
            provider="azure",
            prompt_template=resolved_template,
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=[],
            approved_prompt_artifact_hash=approved_prompt_artifact_hash(prompt_template=resolved_template, system_prompt=None),
        )
        assert config.approved_prompt_artifact_hash == approved_prompt_artifact_hash(prompt_template=resolved_template, system_prompt=None)

        with pytest.raises(ValidationError, match="approved_prompt_artifact_hash"):
            LLMConfig(
                provider="azure",
                prompt_template="Rate how boring this is.",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=[],
                approved_prompt_artifact_hash=approved_prompt_artifact_hash(prompt_template=resolved_template, system_prompt=None),
            )

    def test_missing_required_input_fields_error_names_composer_options_repair(self) -> None:
        """Runtime preflight errors must name the composer patch location."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="URL: {{ row['url'] }}\nContent: {{ row['content'] }}",
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "options.required_input_fields" in message
        assert "patch_node_options" in message
        assert '"patch": {"required_input_fields": ["content", "url"]}' in message

    def test_dynamic_row_item_access_requires_explicit_opt_out(self) -> None:
        """Dynamic row[expr] access must not look like a no-row-field prompt."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template='{% set k = "ssn" %}Secret: {{ row[k] }}',
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "row[expr]" in message
        assert "options.required_input_fields: []" in message

    def test_dynamic_row_get_access_requires_explicit_opt_out(self) -> None:
        """Dynamic row.get(expr) access must fail closed by default."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="Secret: {{ row.get(k) }}",
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "row.get(expr)" in message

    def test_dynamic_row_attr_filter_requires_explicit_opt_out(self) -> None:
        """Dynamic row|attr(expr) access must fail closed by default."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{{ row | attr(row.selector) }}",
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "row|attr(expr)" in message

    def test_dynamic_row_map_attribute_filter_requires_explicit_opt_out(self) -> None:
        """Dynamic row|map(attribute=expr) access must fail closed by default."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{{ row | map(attribute=field_name) | list }}",
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "map(attribute=expr)" in message

    @pytest.mark.parametrize(
        "template",
        (
            "{{ row.note }} {{ row | dictsort }}",
            "{% for k, v in row | items %}{{ v }}{% endfor %}{{ row.note }}",
            "{{ row.note }} {{ dict(row) }}",
            "{{ row.note }} {{ '%(secret)s' % row }}",
            "{{ row.note }} {{ '{0[secret]}'.format(row) }}",
            "{% set c = [row] %}{{ row.note }} {{ c | map('dictsort') | list }}",
            "{% macro m() %}{{ varargs[0] | dictsort }}{% endmacro %}{{ row.note }} {{ m(row) }}",
            "{% macro m() %}{{ kwargs.r | dictsort }}{% endmacro %}{{ row.note }} {{ m(r=row) }}",
            "{% macro m() %}{{ caller(*varargs) }}{% endmacro %}{{ row.note }} {% call(x) m(row) %}{{ x | dictsort }}{% endcall %}",
        ),
    )
    def test_whole_row_value_is_not_a_configuration_question(self, template: str) -> None:
        """A whole row used as a value holds only the declared fields at render (ADR-051), so configuration admits it.

        The runtime projection is the confidentiality guarantee
        (tests/unit/plugins/infrastructure/test_template_projection.py); the
        static analysis stays the early error for reads it can name.
        """
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template=template,
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["note"],
        )
        assert config.required_input_fields == ["note"]

    @pytest.mark.parametrize(
        "template",
        (
            "{% macro m() %}{{ varargs[0].secret }}{% endmacro %}{{ row.note }} {{ m(row) }}",
            "{% macro m() %}{{ kwargs.r.secret }}{% endmacro %}{{ row.note }} {{ m(r=row) }}",
            "{% macro m() %}{{ caller(x=row) }}{% endmacro %}{{ row.note }} {% call(x) m() %}{{ x.secret }}{% endcall %}",
        ),
    )
    def test_field_read_through_an_implicit_macro_argument_must_be_declared(self, template: str) -> None:
        """A field read through ``varargs``, ``kwargs`` or a ``caller`` keyword is a read of that field (S0 fix round 2)."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["note"],
            )

        assert "LLM prompt_template reads 'secret' under 'row'" in str(exc_info.value)

    def test_self_holding_carrier_rejected_even_with_declared_fields(self) -> None:
        """An alias too deep to follow cannot be audited against the declared fields (S0 fix round 2)."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% set a = {'k': row} %}{% set a = {'k': a} %}{{ row.note }} {{ a.k.k.note }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["note"],
            )

        message = str(exc_info.value)
        assert "dynamic row field access (carrier-limit via a variable or macro argument that holds itself, too deep to follow)" in message
        assert "options.required_input_fields: []" in message

    def test_whole_row_value_admitted_with_explicit_opt_out(self) -> None:
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template="{{ row.note }} {{ row | dictsort }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=[],
        )

        assert config.required_input_fields == []

    def test_row_derived_map_attribute_filter_rejected_even_with_declared_selector(self) -> None:
        """Declaring the selector field is not enough when it chooses another field."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{{ rows | map(attribute=row.selector) | list }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "map(attribute=expr)" in message

    def test_attr_filter_pipeline_row_api_name_rejected_even_with_declared_selector(self) -> None:
        """row|attr('get') can call row.get(expr), so it is dynamic row access."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{{ (row | attr('get'))(row.selector) }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.get without a call" in message

    def test_row_alias_map_attribute_filter_rejected_even_with_declared_selector(self) -> None:
        """A local alias for row cannot hide map(attribute=alias.field)."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% set r = row %}{{ rows | map(attribute=r.selector) | list }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "map(attribute=expr)" in message

    def test_row_alias_attr_filter_api_name_rejected_even_with_declared_selector(self) -> None:
        """A local alias for row cannot hide row.get(expr) behind attr."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% set r = row %}{{ (r | attr('get'))(r.selector) }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.get without a call" in message

    def test_with_row_alias_map_attribute_filter_rejected_even_with_declared_selector(self) -> None:
        """A with-block alias for row cannot hide map(attribute=alias.field)."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% with r = row %}{{ lookup.rows | map(attribute=r.selector) | list }}{% endwith %}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "map(attribute=expr)" in message

    def test_row_get_method_alias_rejected_even_with_declared_selector(self) -> None:
        """Aliasing row.get cannot hide dynamic row-field reads."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% set g = row.get %}{{ g(row.selector) }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.get without a call" in message

    @pytest.mark.parametrize(
        "template",
        (
            "{% set d = {'g': row.get} %}{{ d['g']('secret') }}",
            "{% set ns = namespace(g=row.get) %}{{ ns.g('secret') }}",
            "{% set g = row.get %}{% set d = {'g': g} %}{{ d['g']('secret') }}",
        ),
    )
    def test_container_carried_row_get_alias_rejected(self, template: str) -> None:
        """Dict and namespace carriers cannot hide row.get aliases."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.get without a call" in message

    def test_container_carried_row_get_alias_dynamic_key_rejected_even_with_declared_selector(self) -> None:
        """Carrier-held row.get with row-derived keys remains dynamic."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% set ns = namespace(g=row.get) %}{{ ns.g(row.selector) }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.get without a call" in message

    @pytest.mark.parametrize(
        "template",
        (
            "{% set ns = namespace() %}{% set ns.g = row.get %}{{ ns.g('secret') }}",
            "{% set d = {'inner': {'g': row.get}} %}{{ d['inner']['g']('secret') }}",
        ),
    )
    def test_nested_or_assigned_carried_row_get_alias_rejected(self, template: str) -> None:
        """Namespace assignment and nested carriers cannot hide row.get aliases."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.get without a call" in message

    def test_namespace_assigned_row_alias_dynamic_get_rejected_even_with_declared_selector(self) -> None:
        """Namespace attribute assignment can carry row itself."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% set ns = namespace() %}{% set ns.r = row %}{{ ns.r.get(row.selector) }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "row.get(expr)" in message

    @pytest.mark.parametrize(
        ("template", "refusal"),
        (
            ("{% set d = {'inner': {'r': row}} %}{{ d['inner']['r'].get(row.selector) }}", "dynamic row field access"),
            # Naming row.get without a call is refused under every declaration.
            ("{% set xs = [row.get] %}{{ xs[0](row.selector) }}", "uses its row as an object"),
        ),
    )
    def test_nested_or_list_carried_row_access_rejected_even_with_declared_selector(self, template: str, refusal: str) -> None:
        """Nested row-object and list API carriers cannot hide row.get."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert refusal in message

    def test_loop_target_from_row_get_alias_collection_rejected_even_with_declared_selector(self) -> None:
        """Loop targets over row.get collections inherit API alias guards."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% set xs = [row.get] %}{% for g in xs %}{{ g(row.selector) }}{% endfor %}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.get without a call" in message

    @pytest.mark.parametrize(
        "template",
        (
            ("{% set d={'xs':[row.get]} %}{% macro use(g) %}{{ g(row.selector) }}{% endmacro %}{{ use(*d['xs']) }}"),
            ("{% set d={'kw': {'g': row.get}} %}{% macro use(g) %}{{ g(row.selector) }}{% endmacro %}{{ use(**d['kw']) }}"),
            (
                "{% set d={'xs':[row.get]} %}"
                "{% macro wrap() %}{{ caller(*d['xs']) }}{% endmacro %}"
                "{% call(g) wrap() %}{{ g(row.selector) }}{% endcall %}"
            ),
            (
                "{% set d={'kw': {'g': row.get}} %}"
                "{% macro wrap() %}{{ caller(**d['kw']) }}{% endmacro %}"
                "{% call(g) wrap() %}{{ g(row.selector) }}{% endcall %}"
            ),
        ),
    )
    def test_macro_or_callblock_row_get_splats_rejected_even_with_declared_selector(self, template: str) -> None:
        """Macro and callblock splats can carry row.get aliases."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.get without a call" in message

    @pytest.mark.parametrize(
        "template",
        (
            ("{% set d={'kw': {'g': row.get}} %}{% set kw=d['kw'] %}{% macro use(g) %}{{ g(row.selector) }}{% endmacro %}{{ use(**kw) }}"),
            (
                "{% set d={'kw': {'g': row.get}} %}{% set kw=d['kw'] %}"
                "{% macro wrap() %}{{ caller(**kw) }}{% endmacro %}"
                "{% call(g) wrap() %}{{ g(row.selector) }}{% endcall %}"
            ),
        ),
    )
    def test_realiased_row_get_mapping_splats_rejected_even_with_declared_selector(self, template: str) -> None:
        """Assigning a carrier slice locally must preserve row.get aliases."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.get without a call" in message

    @pytest.mark.parametrize(
        ("template", "required_fields"),
        (
            ("{% macro use(g) %}{{ g('secret') }}{% endmacro %}{{ use(**{row.selector: row.get}) }}", ["selector"]),
            (
                "{% macro wrap() %}{{ caller(**{row.selector: row.get}) }}{% endmacro %}{% call(g) wrap() %}{{ g('secret') }}{% endcall %}",
                ["selector"],
            ),
            ("{% set d={'g': row.get} %}{% set g=d[row.selector] %}{{ g('secret') }}", ["selector"]),
            ("{% set xs=[row.get] %}{% set g=xs[row.idx] %}{{ g('secret') }}", ["idx"]),
            ("{% set d={'kw': {'g': row.get}} %}{% set g=d[row.selector]['g'] %}{{ g('secret') }}", ["selector"]),
            ("{% set d={'kw': {'g': row.get}} %}{{ d[row.selector].g('secret') }}", ["selector"]),
            (
                "{% set d={'kw': {'g': row.get}} %}{% macro use(g) %}{{ g('secret') }}{% endmacro %}{{ use(**d[row.selector]) }}",
                ["selector"],
            ),
            (
                "{% set d={'xs': [row.get]} %}{% for g in d[row.selector] %}{{ g('secret') }}{% endfor %}",
                ["selector"],
            ),
            (
                "{% set d={'xs': [row.get]} %}{% macro use(g) %}{{ g('secret') }}{% endmacro %}{{ use(*d[row.selector]) }}",
                ["selector"],
            ),
        ),
    )
    def test_dynamic_key_row_get_carriers_rejected_even_with_declared_selector(self, template: str, required_fields: list[str]) -> None:
        """Dynamic carrier keys can select row.get aliases at render time."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=required_fields,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.get without a call" in message

    @pytest.mark.parametrize(
        ("template", "required_fields"),
        (
            ("{% set d = {'xs': [row]} %}{% for r in d['xs'] %}{{ r.to_dict() }}{% endfor %}", None),
            ("{% set ns = namespace(xs=[row]) %}{% for r in ns.xs %}{{ r.to_dict() }}{% endfor %}", None),
            ("{% set xs = [row] %}{% set d = {'xs': xs} %}{% for r in d['xs'] %}{{ r.to_dict() }}{% endfor %}", None),
            ("{% set d = {'xs': [row]} %}{% set xs = d['xs'] %}{% for r in xs %}{{ r.to_dict() }}{% endfor %}", None),
            (
                "{% set d = {'xs': [row]} %}{% macro leak(r) %}{{ r.to_dict() }}{% endmacro %}{{ leak(*d['xs']) }}",
                None,
            ),
            (
                "{% set d = {'xs': [row]} %}{% for r in d[row.selector] %}{{ r.to_dict() }}{% endfor %}",
                ["selector"],
            ),
            (
                "{% set d = {'xs': [row]} %}{% macro leak(r) %}{{ r.to_dict() }}{% endmacro %}{{ leak(*d[row.selector]) }}",
                ["selector"],
            ),
        ),
    )
    def test_carried_row_collection_iterable_rejected(self, template: str, required_fields: list[str] | None) -> None:
        """Row collections carried by dict/namespace paths still yield row aliases."""
        kwargs = {"required_input_fields": required_fields} if required_fields is not None else {}
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                **kwargs,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    @pytest.mark.parametrize(
        "template",
        (
            "{% set d = {'kw': {'r': row}} %}{{ d[row.selector]['r'].to_dict() }}",
            "{% set d = {'kw': {'r': row}} %}{{ d[row.selector].r.to_dict() }}",
        ),
    )
    def test_dynamic_key_row_object_carriers_rejected(self, template: str) -> None:
        """Dynamic carrier keys can select containers that carry row objects."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    def test_scalar_row_value_collection_alias_allowed_when_declared(self) -> None:
        """A list of declared row values is not a list of row objects."""
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template="{% set xs = [row.text] %}{{ xs[0] }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
        )

        assert config.required_input_fields == ["text"]

    def test_container_sibling_local_dict_lookup_allowed_when_declared(self) -> None:
        """A non-row sibling inside a namespace remains an ordinary local value."""
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template="{% set ns=namespace(r=row, params={'a': 'A'}) %}{{ ns.params.get(row.selector) }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["selector"],
        )

        assert config.required_input_fields == ["selector"]

    def test_row_to_dict_rejected_without_required_input_fields(self) -> None:
        """row.to_dict() exposes the full row and must fail closed."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{{ row.to_dict() }}",
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    @pytest.mark.parametrize("template", ("{{ row._data }}", "{{ row.__class__ }}", "{{ row.contract }}"))
    def test_pipeline_row_private_or_api_attr_rejected(self, template: str) -> None:
        """Private/API row attributes are not auditable data fields."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    def test_row_scalar_alias_map_attribute_filter_rejected_even_with_declared_selector(self) -> None:
        """A row-derived scalar alias cannot choose map(attribute=...)."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% set attr_name = row.selector %}{{ rows | map(attribute=attr_name) | list }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "map(attribute=expr)" in message

    def test_for_loop_row_alias_private_attr_rejected(self) -> None:
        """A loop target bound to row cannot expose private row state."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% for r in [row] %}{{ r._data }}{% endfor %}",
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    def test_macro_row_arg_to_dict_rejected(self) -> None:
        """A macro parameter called with row cannot expose the full row."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% macro leak(r) %}{{ r.to_dict() }}{% endmacro %}{{ leak(row) }}",
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    def test_macro_row_arg_map_attribute_rejected_even_with_declared_selector(self) -> None:
        """A macro parameter called with row cannot hide map(attribute=alias.field)."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% macro leak(r) %}{{ rows | map(attribute=r.selector) | list }}{% endmacro %}{{ leak(row) }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "map(attribute=expr)" in message

    @pytest.mark.parametrize("template", ("{{ ([row]|first)._data }}", "{{ ([row]|first).to_dict() }}"))
    def test_row_expression_private_or_api_attr_rejected(self, template: str) -> None:
        """Row-valued expressions cannot expose private/API row state."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    def test_row_expression_dynamic_get_rejected_even_with_declared_selector(self) -> None:
        """A row-valued expression cannot hide row.get(expr)."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{{ ([row]|first).get(row.selector) }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "row.get(expr)" in message

    @pytest.mark.parametrize(
        "template",
        (
            "{{ ([row] | map(attribute='to_dict') | first)() }}",
            "{{ ([row] | map(attribute='get') | first)(row.selector) }}",
            "{{ [row] | map(attribute='_data') | first }}",
        ),
    )
    def test_static_map_attribute_api_or_private_name_rejected(self, template: str) -> None:
        """Static map(attribute=...) cannot expose private/API row attributes."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    def test_for_loop_row_collection_alias_rejected(self) -> None:
        """A collection alias containing row cannot hide a row loop target."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% set xs = [row] %}{% for r in xs %}{{ r.to_dict() }}{% endfor %}",
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    def test_macro_default_row_arg_rejected(self) -> None:
        """A macro default bound to row cannot hide full-row access."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% macro leak(r=row) %}{{ r.to_dict() }}{% endmacro %}{{ leak() }}",
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    def test_macro_alias_row_arg_rejected(self) -> None:
        """Calling a macro through an alias cannot hide full-row access."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% macro leak(r) %}{{ r.to_dict() }}{% endmacro %}{% set fn = leak %}{{ fn(row) }}",
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    def test_callblock_caller_row_arg_rejected(self) -> None:
        """A caller parameter passed row by a macro cannot hide full-row access."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{% macro wrap() %}{{ caller(row) }}{% endmacro %}{% call(r) wrap() %}{{ r.to_dict() }}{% endcall %}",
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    @pytest.mark.parametrize(
        ("template", "required_fields"),
        (
            (
                "{{ row.visible }}{% macro leak(r) %}{{ r.to_dict() }}{% endmacro %}{% set ns=namespace(fn=leak) %}{{ ns.fn(row) }}",
                ["visible"],
            ),
            (
                "{% macro leak(r) %}{{ r.to_dict() }}{% endmacro %}{% set d={'fn': leak} %}{{ d[row.selector](row) }}",
                ["selector"],
            ),
            (
                "{% macro leak(r) %}{{ r.to_dict() }}{% endmacro %}{% macro run(fn) %}{{ fn(row) }}{% endmacro %}{{ run(leak) }}",
                None,
            ),
        ),
    )
    def test_carried_or_parameter_macro_alias_rejected(self, template: str, required_fields: list[str] | None) -> None:
        """Macro aliases carried through containers or parameters cannot hide full-row access."""
        kwargs = {"required_input_fields": required_fields} if required_fields is not None else {}
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                **kwargs,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    def test_carried_callblock_macro_alias_rejected(self) -> None:
        """Callblock macros invoked through carriers cannot hide full-row access."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=(
                    "{% macro wrap() %}{{ caller(row) }}{% endmacro %}"
                    "{% set ns=namespace(fn=wrap) %}{% call(r) ns.fn() %}{{ r.to_dict() }}{% endcall %}"
                ),
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    def test_row_to_checkpoint_format_rejected_without_required_input_fields(self) -> None:
        """row.to_checkpoint_format() exposes serialized row data and must fail closed."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{{ row.to_checkpoint_format() }}",
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    @pytest.mark.parametrize(
        ("template", "refusal"),
        (
            ("{% set xs = [row] %}{{ xs[0].get(row.selector) }}", "dynamic row field access"),
            ("{{ [row][0].to_dict() }}", "uses its row as an object"),
            ("{{ [row][0]._data }}", "uses its row as an object"),
        ),
    )
    def test_indexed_row_collection_receiver_rejected(self, template: str, refusal: str) -> None:
        """Indexing a known row collection cannot hide row API or dynamic access."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        assert refusal in str(exc_info.value)

    @pytest.mark.parametrize(
        "template",
        (
            "{{ [row] | join(',', attribute=row.selector) }}",
            "{{ rows | selectattr(row.selector) | list }}",
            "{{ rows | rejectattr(row.selector) | list }}",
            "{{ rows | sort(attribute=row.selector) | list }}",
            "{{ rows | groupby(row.selector) | list }}",
            "{{ rows | unique(attribute=row.selector) | list }}",
            "{{ rows | sum(attribute=row.selector) }}",
            "{{ rows | min(attribute=row.selector) }}",
            "{{ rows | max(attribute=row.selector) }}",
        ),
    )
    def test_attribute_resolving_filter_dynamic_argument_rejected_even_with_declared_selector(self, template: str) -> None:
        """Attribute-resolving filters cannot use row-derived attribute names."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "map(attribute=expr)" in message

    @pytest.mark.parametrize(
        "template",
        (
            "{{ row | attr(name=row.selector) }}",
            "{{ [row] | join(',', row.selector) }}",
            "{{ [row] | map('attr', row.selector) | first }}",
        ),
    )
    def test_dynamic_filter_argument_forms_rejected_even_with_declared_selector(self, template: str) -> None:
        """Keyword/positional filter variants cannot use row-derived attribute names."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        assert "dynamic row field access" in str(exc_info.value)

    @pytest.mark.parametrize(
        "template",
        (
            "{% macro leak(r) %}{{ r.to_dict() }}{% endmacro %}{{ leak(*[row]) }}",
            "{% macro leak(r) %}{{ r.to_dict() }}{% endmacro %}{{ leak(**{'r': row}) }}",
            "{% macro wrap() %}{{ caller(*[row]) }}{% endmacro %}{% call(r) wrap() %}{{ r.to_dict() }}{% endcall %}",
        ),
    )
    def test_macro_or_callblock_splat_row_arg_rejected(self, template: str) -> None:
        """Literal splats cannot hide passing row into macro parameters."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    @pytest.mark.parametrize(
        "template",
        (
            "{% set r, x = row, 1 %}{{ r.to_dict() }}",
            "{% set d = {'r': row} %}{{ d['r'].to_dict() }}",
            "{% set ns = namespace(r=row) %}{{ ns.r.to_dict() }}",
        ),
    )
    def test_destructured_or_carried_row_alias_rejected(self, template: str) -> None:
        """Tuple destructuring and row carrier containers cannot hide full-row access."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    @pytest.mark.parametrize(
        ("template", "refusal"),
        (
            ("{{ (row if true else row).to_dict() }}", "uses its row as an object"),
            ("{{ (row or {}).get(row.selector) }}", "dynamic row field access"),
            ("{{ (row|default({})).to_dict() }}", "uses its row as an object"),
        ),
    )
    def test_generic_row_expression_receiver_rejected(self, template: str, refusal: str) -> None:
        """Generic expressions that may yield row cannot hide row API access."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        assert refusal in str(exc_info.value)

    @pytest.mark.parametrize(
        "template",
        (
            "{{ row.get(key=row.selector) }}",
            "{{ row.get(*[row.selector]) }}",
            "{{ row.get(**{'key': row.selector}) }}",
        ),
    )
    def test_row_get_keyword_or_splat_key_rejected_even_with_declared_selector(self, template: str) -> None:
        """row.get keyword/splat key forms cannot use row-derived keys."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "row.get(expr)" in message

    @pytest.mark.parametrize(
        "template",
        (
            "{% set args = lookup.args %}{{ row.get(*args) }}",
            "{% set kwargs = lookup.kwargs %}{{ row.get(**kwargs) }}",
        ),
    )
    def test_row_get_unknown_splats_rejected(self, template: str) -> None:
        """Unknown row.get splats can supply a dynamic key at render time."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "dynamic row field access" in message
        assert "row.get(expr)" in message

    @pytest.mark.parametrize(
        "template",
        (
            "{% set args = [row] %}{% macro leak(r) %}{{ r.to_dict() }}{% endmacro %}{{ leak(*args) }}",
            "{% set kwargs = {'r': row} %}{% macro leak(r) %}{{ r.to_dict() }}{% endmacro %}{{ leak(**kwargs) }}",
        ),
    )
    def test_macro_aliased_splat_row_arg_rejected(self, template: str) -> None:
        """Aliased macro splats cannot hide passing row into macro parameters."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
            )

        message = str(exc_info.value)
        assert "uses its row as an object" in message
        assert "row.contract, row.to_dict, row.to_checkpoint_format" in message

    @pytest.mark.parametrize(
        "template",
        (
            "{{ (row | attr(**{'name': 'get'}))(row.selector) }}",
            "{{ row | attr(*['get']) }}",
            "{{ ([row] | map(**{'attribute': 'to_dict'}) | first)() }}",
        ),
    )
    def test_attribute_filter_literal_splats_rejected(self, template: str) -> None:
        """Literal filter splats must fold into attr/map guard analysis."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        assert "uses its row as an object" in str(exc_info.value)

    @pytest.mark.parametrize(
        "template",
        (
            "{% set opts = lookup.opts %}{{ row | attr(**opts) }}",
            "{% set opts = lookup.opts %}{{ [row] | map(**opts) | list }}",
            "{% set opts = lookup.opts %}{{ [row] | join(',', **opts) }}",
            "{% set args = lookup.args %}{{ [row] | selectattr(*args) | list }}",
        ),
    )
    def test_attribute_filter_unknown_splats_rejected(self, template: str) -> None:
        """Unknown splats on attribute filters can carry attribute names."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
            )

        assert "dynamic row field access" in str(exc_info.value)

    def test_groupby_attribute_keyword_rejected_when_row_derived(self) -> None:
        """groupby(attribute=...) with a row-derived attribute name is dynamic."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="{{ [row] | groupby(attribute=row.selector) | list }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        assert "dynamic row field access" in str(exc_info.value)

    def test_groupby_attribute_keyword_allows_declared_literal_field(self) -> None:
        """Static groupby(attribute=...) field names remain declarable."""
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template="{{ [row] | groupby(attribute='text') | list }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
        )

        assert config.required_input_fields == ["text"]

    @pytest.mark.parametrize(
        "template",
        (
            (
                "{% set args = [row.selector] %}"
                "{% macro leak(attr) %}{{ [row] | map(attribute=attr) | list }}{% endmacro %}"
                "{{ leak(*args) }}"
            ),
            (
                "{% set kwargs = {'attr': row.selector} %}"
                "{% macro leak(attr) %}{{ [row] | map(attribute=attr) | list }}{% endmacro %}"
                "{{ leak(**kwargs) }}"
            ),
            (
                "{% set args = [row.selector] %}"
                "{% macro leak() %}{{ caller(*args) }}{% endmacro %}"
                "{% call(attr) leak() %}{{ [row] | map(attribute=attr) | list }}{% endcall %}"
            ),
            (
                "{% set kwargs = {'attr': row.selector} %}"
                "{% macro leak() %}{{ caller(**kwargs) }}{% endmacro %}"
                "{% call(attr) leak() %}{{ [row] | map(attribute=attr) | list }}{% endcall %}"
            ),
        ),
    )
    def test_macro_or_callblock_row_value_splats_rejected(self, template: str) -> None:
        """Row-derived scalar aliases passed through macro splats stay dynamic."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["selector"],
            )

        assert "dynamic row field access" in str(exc_info.value)

    def test_non_attribute_filter_splat_row_values_allowed_when_declared(self) -> None:
        """Ordinary filters may use declared row values as non-attribute args."""
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template="{{ row.text | replace(*[row.selector, 'x']) }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text", "selector"],
        )

        assert config.required_input_fields == ["text", "selector"]

    def test_dynamic_row_access_rejected_even_with_declared_fields(self) -> None:
        """A declared field list is not a dynamic-key allowlist."""
        with pytest.raises(ValidationError, match="dynamic row field access"):
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template='{% set k = "ssn" %}Secret: {{ row[k] }}',
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["ssn"],
            )

    def test_dynamic_row_access_accepts_empty_required_fields_opt_out(self) -> None:
        """The documented empty-list opt-out remains explicit and accepted."""
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template='{% set k = "ssn" %}Secret: {{ row[k] }}',
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=[],
        )

        assert config.required_input_fields == []


class TestRequiredInputFieldsAppearInTemplate:
    """Dual of `_validate_required_input_fields_declared`: catches the inverse
    asymmetry where `required_input_fields` is declared but the prompt template
    interpolates zero `row.*` fields, so every row is sent the same static prompt.
    """

    def test_declared_fields_without_row_interpolation_rejected(self) -> None:
        """A non-empty required_input_fields with a static prompt body is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="Identify primary colours used on the page. Return JSON.",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["url", "content"],
            )

        message = str(exc_info.value)
        assert "prompt_template never reads 'row', so no declared field reaches the prompt" in message
        assert "['content', 'url']" in message
        assert "{{ row.url }}" in message
        assert "{{ row.content }}" in message
        # The remedy never points at the whole-row opt-out: under ADR-051 ``[]``
        # is the one setting that shows the template every column.
        assert "[]" not in message
        assert "remove options.required_input_fields" in message

    @pytest.mark.parametrize(
        "template",
        [
            pytest.param("{% for row in [1] %}{% endfor %}Rate this.", id="loop-variable"),
            pytest.param("{% set row = 'x' %}{{ row }}", id="set-variable"),
            pytest.param("{% macro m(row) %}{{ row }}{% endmacro %}{{ m('x') }}", id="macro-parameter"),
        ],
    )
    def test_a_row_the_template_binds_itself_is_not_a_read_of_the_row(self, template: str) -> None:
        """A local ``row`` never carries a declared field, so the declaration still goes unused."""
        with pytest.raises(ValidationError, match="prompt_template never reads 'row'"):
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template=template,
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["note"],
            )

    @pytest.mark.parametrize(
        "template",
        [
            pytest.param("{{ row | dictsort }}", id="dictsort"),
            pytest.param("{{ row | tojson }}", id="tojson"),
            pytest.param("{% for k, v in row | items %}{{ k }}={{ v }} {% endfor %}", id="items"),
            pytest.param("{% macro m() %}{{ row.note }}{% endmacro %}{{ m() }}", id="macro-body"),
        ],
    )
    def test_a_whole_row_form_with_a_declared_list_is_admitted(self, template: str) -> None:
        """ADR-051: the template's row holds exactly the declared fields, so a whole-row form interpolates them."""
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template=template,
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["note"],
        )
        assert config.required_input_fields == ["note"]

    def test_declared_fields_with_matching_row_interpolation_accepted(self) -> None:
        """Canonical case: every declared field appears as a row.* reference."""
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template="URL: {{ row.url }}\nContent: {{ row.content }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["url", "content"],
        )
        assert sorted(config.required_input_fields or []) == ["content", "url"]

    def test_declared_fields_with_partial_row_interpolation_accepted(self) -> None:
        """Validator fires only on the empty-row-refs case, not on a superset declaration.

        A declaration WIDER than the template is legitimate — "declared a field
        for downstream cleanup but not interpolated in this prompt body" is a
        real presence assertion, and this validator must not reject it.

        The converse is NOT symmetric and this docstring used to claim it was:
        extra row refs the declaration does not cover are rejected by
        ``_validate_template_variable_bindings`` (elspeth-a9ba80cb0b). A
        reference outside the declaration escapes the node's input contract
        entirely, so nothing obliges a producer to supply it.
        """
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template="URL: {{ row.url }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["url", "content"],
        )
        assert config.prompt_template == "URL: {{ row.url }}"

    def test_explicit_opt_out_empty_list_accepted_with_static_prompt(self) -> None:
        """`required_input_fields: []` is the documented opt-out and must pass."""
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template="Return a fixed JSON greeting.",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=[],
        )
        assert config.required_input_fields == []

    def test_required_input_fields_none_does_not_fire_this_check(self) -> None:
        """When `required_input_fields` is undeclared, the dual validator must not fire.

        That case is owned by `_validate_required_input_fields_declared`. With a
        static prompt that references no row.* fields, neither validator fires,
        and the config is accepted (audit-philosophy opt-out by omission).
        """
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template="Return a fixed JSON greeting.",
            schema_config=_OBSERVED_SCHEMA,
        )
        assert config.required_input_fields is None

    def test_multi_query_mode_is_out_of_scope_for_this_check(self) -> None:
        """Multi-query mode flows row data via per-query input_fields mappings.

        A top-level template without `row.*` references is therefore not by itself
        diagnostic in multi-query mode; the dual validator restricts itself to
        single-query mode where the absence is unambiguous.
        """
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            prompt_template="Assess each case.",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["case_text"],
            queries={
                "diagnosis": {
                    "input_fields": {"input_1": "case_text"},
                    "template": "Diagnose: {{ row.input_1 }}",
                }
            },
        )
        assert config.queries is not None

    def test_error_message_names_composer_repair_path(self) -> None:
        """The error must point the composer at patch_node_options to repair."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                prompt_template="Static prompt without interpolation.",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["page_body"],
            )

        message = str(exc_info.value)
        assert "patch_node_options" in message
        assert '"prompt_template"' in message
        assert "{{ row.page_body }}" in message


class TestLLMConfigResponseFieldValidation:
    """Verify LLMConfig rejects invalid response_field names.

    Bug: elspeth-23d1bcff6b. LLMConfig accepts invalid response_field names
    even though downstream schema builders require a non-empty Python identifier.
    Bad config survives model validation and only explodes later.
    """

    def test_empty_response_field_rejected(self) -> None:
        """Empty string response_field is rejected."""
        with pytest.raises(ValidationError, match="response_field"):
            LLMConfig(
                provider="azure",
                prompt_template="hello",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=[],
                response_field="",
            )

    def test_whitespace_response_field_rejected(self) -> None:
        """Whitespace-only response_field is rejected."""
        with pytest.raises(ValidationError, match="response_field"):
            LLMConfig(
                provider="azure",
                prompt_template="hello",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=[],
                response_field="   ",
            )

    def test_non_identifier_response_field_rejected(self) -> None:
        """Non-Python-identifier response_field is rejected (e.g., 'my-field')."""
        with pytest.raises(ValidationError, match="response_field"):
            LLMConfig(
                provider="azure",
                prompt_template="hello",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=[],
                response_field="my-field",
            )

    def test_valid_identifier_response_field_accepted(self) -> None:
        """Valid Python identifier response_field is accepted."""
        config = LLMConfig(
            provider="azure",
            prompt_template="hello",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=[],
            response_field="llm_output",
        )
        assert config.response_field == "llm_output"


# ---------------------------------------------------------------------------
# Provider-specific configs
# ---------------------------------------------------------------------------


class TestAzureOpenAIConfig:
    """Tests for Azure-specific config class."""

    def test_requires_deployment_name(self) -> None:
        from elspeth.plugins.transforms.llm.providers.azure import AzureOpenAIConfig

        with pytest.raises((ValidationError, ValueError)):
            AzureOpenAIConfig(  # type: ignore[call-arg]  # intentionally missing required args
                prompt_template="hello",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=[],
                # Missing deployment_name, endpoint, api_key
            )

    def test_model_defaults_to_deployment_name(self) -> None:
        from elspeth.plugins.transforms.llm.providers.azure import AzureOpenAIConfig

        config = AzureOpenAIConfig(
            deployment_name="gpt-4o-deploy",
            endpoint="https://test.openai.azure.com/",
            api_key="key",
            prompt_template="hello",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=[],
        )
        # Azure sets model = deployment_name when model is empty/None
        assert config.model == "gpt-4o-deploy"

    def test_tracing_field_on_azure(self) -> None:
        from elspeth.plugins.transforms.llm.providers.azure import AzureOpenAIConfig

        config = AzureOpenAIConfig(
            deployment_name="gpt-4o",
            endpoint="https://test.openai.azure.com/",
            api_key="key",
            prompt_template="hello",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=[],
            tracing={"provider": "langfuse", "public_key": "pk"},
        )
        assert config.tracing is not None


class TestAzureOpenAIConfigTracing:
    """Tests for tracing configuration in AzureOpenAIConfig (from_dict path)."""

    def _make_azure_base_config(self) -> dict[str, Any]:
        """Create base config with all required fields for Azure."""
        return {
            "provider": "azure",
            "deployment_name": "gpt-4",
            "endpoint": "https://test.openai.azure.com",
            "api_key": "test-key",
            "prompt_template": "Hello {{ row.name }}",
            "schema": {"mode": "observed"},
            "required_input_fields": [],
        }

    def test_tracing_field_accepts_none(self) -> None:
        """Tracing field defaults to None (no tracing)."""
        from elspeth.plugins.transforms.llm.providers.azure import AzureOpenAIConfig

        config = AzureOpenAIConfig.from_dict(self._make_azure_base_config())
        assert config.tracing is None

    def test_tracing_field_accepts_azure_ai_config(self) -> None:
        """Tracing field accepts Azure AI configuration dict."""
        from elspeth.plugins.transforms.llm.providers.azure import AzureOpenAIConfig

        cfg = self._make_azure_base_config()
        cfg["tracing"] = {
            "provider": "azure_ai",
            "connection_string": "InstrumentationKey=xxx",
            "enable_content_recording": True,
        }
        config = AzureOpenAIConfig.from_dict(cfg)
        assert config.tracing is not None
        assert config.tracing["provider"] == "azure_ai"

    def test_tracing_field_accepts_langfuse_config(self) -> None:
        """Tracing field accepts Langfuse configuration dict."""
        from elspeth.plugins.transforms.llm.providers.azure import AzureOpenAIConfig

        cfg = self._make_azure_base_config()
        cfg["tracing"] = {
            "provider": "langfuse",
            "public_key": "pk-xxx",
            "secret_key": "sk-xxx",
        }
        config = AzureOpenAIConfig.from_dict(cfg)
        assert config.tracing is not None
        assert config.tracing["provider"] == "langfuse"


class TestOpenRouterConfigTracing:
    """Tests for tracing configuration in OpenRouterConfig (from_dict path)."""

    def _make_openrouter_base_config(self) -> dict[str, Any]:
        """Create base config with all required fields for OpenRouter."""
        return {
            "provider": "openrouter",
            "model": _OPENROUTER_MODEL,
            "api_key": "test-key",
            "prompt_template": "Hello {{ row.name }}",
            "schema": {"mode": "observed"},
            "required_input_fields": [],
        }

    def test_tracing_field_accepts_none(self) -> None:
        """Tracing field defaults to None (no tracing)."""
        from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterConfig

        config = OpenRouterConfig.from_dict(self._make_openrouter_base_config())
        assert config.tracing is None

    def test_tracing_field_accepts_langfuse_config(self) -> None:
        """Tracing field accepts Langfuse configuration dict."""
        from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterConfig

        cfg = self._make_openrouter_base_config()
        cfg["tracing"] = {
            "provider": "langfuse",
            "public_key": "pk-xxx",
            "secret_key": "sk-xxx",
        }
        config = OpenRouterConfig.from_dict(cfg)
        assert config.tracing is not None
        assert config.tracing["provider"] == "langfuse"


class TestOpenRouterConfig:
    """Tests for OpenRouter-specific config class."""

    def test_requires_model(self) -> None:
        """OpenRouter requires model to be non-None."""
        from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterConfig

        # model=None should fail validation
        with pytest.raises((ValidationError, ValueError)):
            OpenRouterConfig(  # type: ignore[call-arg]  # intentionally missing model
                api_key="key",
                prompt_template="hello",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=[],
                # model not provided — should fail because OpenRouter needs it
            )

    def test_accepts_explicit_model(self) -> None:
        from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterConfig

        config = OpenRouterConfig(
            model="openai/gpt-4o",
            api_key="key",
            prompt_template="hello",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=[],
        )
        assert config.model == "openai/gpt-4o"


# ---------------------------------------------------------------------------
# Domain-agnostic QuerySpec
# ---------------------------------------------------------------------------


class TestQuerySpec:
    """Tests for the new domain-agnostic QuerySpec."""

    def test_post_init_rejects_empty_name(self) -> None:
        from elspeth.plugins.transforms.llm.multi_query import QuerySpec

        with pytest.raises(ValueError, match="name must be non-empty"):
            QuerySpec(name="", input_fields=MappingProxyType({"text": "text"}))

    def test_post_init_rejects_empty_input_fields(self) -> None:
        from elspeth.plugins.transforms.llm.multi_query import QuerySpec

        with pytest.raises(ValueError, match="input_fields must be non-empty"):
            QuerySpec(name="q1", input_fields=MappingProxyType({}))

    def test_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        from elspeth.plugins.transforms.llm.multi_query import QuerySpec

        spec = QuerySpec(name="q1", input_fields=MappingProxyType({"text": "text_col"}))
        with pytest.raises(FrozenInstanceError):
            spec.name = "modified"  # type: ignore[misc]

    def test_defaults(self) -> None:
        from elspeth.plugins.transforms.llm.multi_query import QuerySpec, ResponseFormat

        spec = QuerySpec(name="q1", input_fields=MappingProxyType({"text": "text_col"}))
        assert spec.response_format == ResponseFormat.STANDARD
        assert spec.output_fields is None
        assert spec.template is None
        assert spec.max_tokens is None

    def test_build_template_context_named_variables(self) -> None:
        """Named input_fields map to template variables directly."""
        from elspeth.plugins.transforms.llm.multi_query import QuerySpec

        spec = QuerySpec(
            name="q1",
            input_fields=MappingProxyType({"text_content": "text", "category_name": "category"}),
        )
        from elspeth.plugins.infrastructure.templates import DeclaredFields, TemplateRow

        row = make_pipeline_row({"text": "hello world", "category": "science", "extra": "ignored"})
        ctx = spec.build_template_context(row, DeclaredFields(frozenset({"text", "category"})))

        assert ctx["text_content"] == "hello world"
        assert ctx["category_name"] == "science"
        # source_row is the row projected to the node's declaration (ADR-051): 'extra' is not in it.
        assert type(ctx["source_row"]) is TemplateRow
        assert dict(ctx["source_row"]) == {"text": "hello world", "category": "science"}

    def test_build_template_context_missing_field_raises(self) -> None:
        from elspeth.plugins.transforms.llm.multi_query import QuerySpec

        spec = QuerySpec(
            name="q1",
            input_fields=MappingProxyType({"text_content": "text"}),
        )
        from elspeth.plugins.infrastructure.templates import ALL_FIELDS

        with pytest.raises(KeyError, match="text"):
            spec.build_template_context(make_pipeline_row({"other": "value"}), ALL_FIELDS)

    def test_input_fields_is_deeply_immutable(self) -> None:
        """input_fields dict must be truly immutable — shared across rows."""
        from types import MappingProxyType

        from elspeth.plugins.transforms.llm.multi_query import QuerySpec

        original = {"text": "text_col", "cat": "category_col"}
        spec = QuerySpec(name="q1", input_fields=MappingProxyType(original))

        assert isinstance(spec.input_fields, MappingProxyType)
        with pytest.raises(TypeError):
            spec.input_fields["injected"] = "evil"  # type: ignore[index]

        # Caller's original dict must be decoupled
        original["injected"] = "evil"
        assert "injected" not in spec.input_fields

    def test_output_fields_is_tuple(self) -> None:
        """output_fields list must be stored as tuple when provided."""
        from elspeth.plugins.transforms.llm.multi_query import OutputFieldConfig, OutputFieldType, QuerySpec

        fields = [OutputFieldConfig(suffix="label", type=OutputFieldType.STRING)]
        spec = QuerySpec(name="q1", input_fields=MappingProxyType({"text": "col"}), output_fields=tuple(fields))

        assert isinstance(spec.output_fields, tuple)
        # Caller's original list must be decoupled
        fields.append(OutputFieldConfig(suffix="extra", type=OutputFieldType.STRING))
        assert len(spec.output_fields) == 1


# ---------------------------------------------------------------------------
# resolve_queries()
# ---------------------------------------------------------------------------


class TestResolveQueries:
    """Tests for resolve_queries() normalization."""

    def test_empty_list_raises(self) -> None:
        from elspeth.plugins.transforms.llm.multi_query import resolve_queries

        with pytest.raises(ValueError, match="no queries configured"):
            resolve_queries([])

    def test_empty_dict_raises(self) -> None:
        from elspeth.plugins.transforms.llm.multi_query import resolve_queries

        with pytest.raises(ValueError, match="no queries configured"):
            resolve_queries({})

    def test_dict_to_list_normalization(self) -> None:
        from elspeth.plugins.transforms.llm.multi_query import resolve_queries

        result = resolve_queries(
            _mapping_defs(
                {
                    "q1": {
                        "input_fields": {"text": "text_col"},
                    },
                    "q2": {
                        "input_fields": {"category": "cat_col"},
                    },
                }
            )
        )
        assert len(result) == 2
        names = {q.name for q in result}
        assert names == {"q1", "q2"}

    def test_list_normalization(self) -> None:
        from elspeth.plugins.transforms.llm.multi_query import QuerySpec, resolve_queries

        specs = [
            QuerySpec(name="q1", input_fields=MappingProxyType({"text": "text_col"})),
        ]
        result = resolve_queries(specs)
        assert len(result) == 1
        assert result[0].name == "q1"

    def test_key_collision_raises(self) -> None:
        """Two queries whose name+suffix combination produces the same full output key.

        Query "q1_extra" with suffix "score" -> key "q1_extra_score"
        Query "q1" with suffix "extra_score" -> key "q1_extra_score"

        Both produce the identical full output key, so resolve_queries must raise.
        """
        from elspeth.plugins.transforms.llm.multi_query import resolve_queries

        with pytest.raises(ValueError, match="collision"):
            resolve_queries(
                _mapping_defs(
                    {
                        "q1_extra": {
                            "input_fields": {"text": "text_col"},
                            "output_fields": [{"suffix": "score", "type": "integer"}],
                        },
                        "q1": {
                            "input_fields": {"text": "text_col"},
                            "output_fields": [{"suffix": "extra_score", "type": "integer"}],
                        },
                    }
                )
            )

    def test_reserved_suffix_raises_error(self) -> None:
        """Output field with reserved _error suffix raises ValueError."""
        from elspeth.plugins.transforms.llm.multi_query import resolve_queries

        with pytest.raises(ValueError, match="reserved LLM suffix"):
            resolve_queries(
                _mapping_defs(
                    {
                        "q1": {
                            "input_fields": {"text": "text_col"},
                            "output_fields": [{"suffix": "error", "type": "string"}],
                        },
                    }
                )
            )

    def test_reserved_suffix_from_constants_raises_error(self) -> None:
        """Output field with suffix derived from LLM_GUARANTEED_SUFFIXES (e.g., 'usage') raises ValueError."""
        from elspeth.plugins.transforms.llm.multi_query import resolve_queries

        with pytest.raises(ValueError, match="reserved LLM suffix"):
            resolve_queries(
                _mapping_defs(
                    {
                        "q1": {
                            "input_fields": {"text": "text_col"},
                            "output_fields": [{"suffix": "usage", "type": "string"}],
                        },
                    }
                )
            )

    def test_single_query_returns_one_element_list(self) -> None:
        from elspeth.plugins.transforms.llm.multi_query import resolve_queries

        result = resolve_queries(
            _mapping_defs(
                {
                    "only_one": {"input_fields": {"text": "text_col"}},
                }
            )
        )
        assert len(result) == 1

    def test_rejects_positional_template_variables(self) -> None:
        """Templates with {{ input_1 }} pattern raise with migration guidance."""
        from elspeth.plugins.transforms.llm.multi_query import resolve_queries

        with pytest.raises(ValueError, match="positional variables"):
            resolve_queries(
                _mapping_defs(
                    {
                        "q1": {
                            "input_fields": {"text": "text_col"},
                            "template": "Evaluate {{ input_1 }} quality",
                        },
                    }
                )
            )


class TestMultiQueryInputFieldsValidation:
    """Regression: _validate_required_input_fields_declared must check multi-query input_fields."""

    def test_multi_query_dict_form_requires_declaration(self) -> None:
        """Multi-query with input_fields must require required_input_fields declaration."""
        with pytest.raises(ValidationError, match="required_input_fields"):
            LLMConfig(
                provider="openrouter",
                model="test-model",
                prompt_template="Static template",
                schema_config=_OBSERVED_SCHEMA,
                queries={
                    "q1": {
                        "input_fields": {"text": "customer_text"},
                    },
                },
            )

    def test_multi_query_list_form_requires_declaration(self) -> None:
        """Multi-query list form also triggers required_input_fields check."""
        with pytest.raises(ValidationError, match="required_input_fields"):
            LLMConfig(
                provider="openrouter",
                model="test-model",
                prompt_template="Static template",
                schema_config=_OBSERVED_SCHEMA,
                queries=[
                    {
                        "name": "q1",
                        "input_fields": {"text": "customer_text"},
                    },
                ],
            )

    def test_multi_query_with_explicit_fields_passes(self) -> None:
        """Multi-query with required_input_fields declared passes validation."""
        config = LLMConfig(
            provider="openrouter",
            model="test-model",
            prompt_template="Static template",
            schema_config=_OBSERVED_SCHEMA,
            queries={
                "q1": {
                    "input_fields": {"text": "customer_text"},
                },
            },
            required_input_fields=["customer_text"],
        )
        assert config.required_input_fields == ["customer_text"]

    def test_multi_query_opt_out_passes(self) -> None:
        """Multi-query with empty required_input_fields (opt-out) passes."""
        config = LLMConfig(
            provider="openrouter",
            model="test-model",
            prompt_template="Static template",
            schema_config=_OBSERVED_SCHEMA,
            queries={
                "q1": {
                    "input_fields": {"text": "customer_text"},
                },
            },
            required_input_fields=[],
        )
        assert config.required_input_fields == []


class TestStructuredQueryProbeClassification:
    """Ruling A: cross-query validation surfaces as the redacted-safe category.

    Cross-query validation (duplicate names, reserved suffixes, output-key
    collisions, list-form name presence) used to raise a bare ``ValueError``
    from ``resolve_queries`` at ``LLMTransform.__init__`` time, which escaped as
    a generic 500 because it was neither a ``PluginConfigError`` nor matched by
    ``_is_config_probe_exception``. Relocating it into an ``LLMConfig``
    ``model_validator`` means ``from_dict`` wraps the failure into
    ``PluginConfigError`` — the §5.3 redacted-safe configuration-probe category.
    """

    def _base_config(self, **overrides: Any) -> dict[str, Any]:
        config: dict[str, Any] = {
            "provider": "azure",
            "prompt_template": "Assess: {{ row.text }}",
            "schema": {"mode": "observed"},
            "required_input_fields": [],
        }
        config.update(overrides)
        return config

    def test_duplicate_query_names_from_dict_raise_plugin_config_error(self) -> None:
        """A malformed structured-query draft lands in PluginConfigError via from_dict."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError

        bad = self._base_config(
            queries=[
                {"name": "diagnosis", "input_fields": {"text": "col_a"}},
                {"name": "diagnosis", "input_fields": {"text": "col_b"}},
            ]
        )
        with pytest.raises(PluginConfigError, match="Duplicate query name"):
            LLMConfig.from_dict(bad, plugin_name="llm")

    def test_reserved_suffix_from_dict_raises_plugin_config_error(self) -> None:
        from elspeth.plugins.infrastructure.config_base import PluginConfigError

        bad = self._base_config(
            queries={
                "q1": {
                    "input_fields": {"text": "col_a"},
                    "output_fields": [{"suffix": "usage", "type": "string"}],
                },
            }
        )
        with pytest.raises(PluginConfigError, match="reserved LLM suffix"):
            LLMConfig.from_dict(bad, plugin_name="llm")

    def test_list_entry_missing_name_from_dict_raises_plugin_config_error(self) -> None:
        from elspeth.plugins.infrastructure.config_base import PluginConfigError

        bad = self._base_config(queries=[{"input_fields": {"text": "col_a"}}])
        with pytest.raises(PluginConfigError, match="must include a 'name'"):
            LLMConfig.from_dict(bad, plugin_name="llm")

    def test_malformed_query_draft_is_classified_as_config_probe(self) -> None:
        """The relocated failure is matched by the composer probe classifier."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.web.composer.state import _is_config_probe_exception

        bad = self._base_config(
            queries=[
                {"name": "diagnosis", "input_fields": {"text": "col_a"}},
                {"name": "diagnosis", "input_fields": {"text": "col_b"}},
            ]
        )
        try:
            LLMConfig.from_dict(bad, plugin_name="llm")
        except PluginConfigError as exc:
            assert _is_config_probe_exception(exc) is True
        else:  # pragma: no cover - the draft must fail
            pytest.fail("malformed structured-query draft did not fail closed")

    def test_valid_structured_queries_from_dict_succeeds(self) -> None:
        """A well-formed structured-query draft still validates (vacuousness guard)."""
        good = self._base_config(
            queries={
                "clarity": {
                    "input_fields": {"text": "col_a"},
                    "response_format": "structured",
                    "output_fields": [
                        {"suffix": "score", "type": "integer"},
                        {"suffix": "rationale", "type": "string"},
                    ],
                },
            }
        )
        config = LLMConfig.from_dict(good, plugin_name="llm")
        assert config.queries is not None


# ---------------------------------------------------------------------------
# Template variable bindings (config-time parity with the composer guards)
# ---------------------------------------------------------------------------


class TestTemplateVariableBindings:
    """Config-time rejection of templates whose interpolations can never bind.

    ``PromptTemplate.render`` supplies exactly ``{row, lookup}`` under
    StrictUndefined; in multi-query mode ``row`` carries the query's
    ``input_fields`` variables plus ``source_row`` (``build_template_context``
    → ``render`` wraps the synthetic context under ``row``). The former
    ``validate_prompt_template`` only compile-checked syntax, so a bare-name
    template passed config validation and crashed every row at render — the
    YAML-authoring twin of the composer's ``prompt_template_unbound_variables``
    / ``query_template_unbound_row_fields`` guards (elspeth-bea314a89b
    follow-up).
    """

    def _single(self, template: str, **overrides: Any) -> LLMConfig:
        kwargs: dict[str, Any] = {
            "provider": "azure",
            "prompt_template": template,
            "schema_config": _OBSERVED_SCHEMA,
            "required_input_fields": [],
        }
        kwargs.update(overrides)
        return LLMConfig(**kwargs)

    def _multi(self, queries: Any, template: str = "Assess: {{ row.input_1 }}") -> LLMConfig:
        return LLMConfig(
            provider="azure",
            prompt_template=template,
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=[],
            queries=queries,
        )

    # ── Single-prompt mode ──────────────────────────────────────────────

    def test_single_prompt_bare_name_rejected(self) -> None:
        """The acceptance-run defect shape, now caught at YAML/config time."""
        with pytest.raises(ValidationError, match="prompt render context does not define") as exc_info:
            self._single("Classify: {{ text }}")
        message = str(exc_info.value)
        assert "'text'" in message
        assert "row." in message

    def test_single_prompt_names_all_offenders_sorted(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            self._single("{{ zeta }} then {{ alpha }}")
        message = str(exc_info.value)
        assert message.index("'alpha'") < message.index("'zeta'")

    @pytest.mark.parametrize(
        "template",
        [
            "Classify: {{ row.text }}",
            'Classify: {{ row["Original Header"] }}',
            "Instructions: {{ lookup.instructions }}",
            "Static prompt with no interpolation at all.",
            "{% set t = row.text %}Classify: {{ t }}",
            "{{ range(3) | join(', ') }}",
        ],
    )
    def test_single_prompt_bound_or_static_accepted(self, template: str) -> None:
        assert self._single(template).prompt_template == template

    def test_single_prompt_local_assigned_in_every_if_branch_is_accepted(self) -> None:
        template = '{% if row.flag %}{% set verdict = "YES" %}{% else %}{% set verdict = "NO" %}{% endif %}{{ verdict }}'

        assert self._single(template).prompt_template == template

    def test_single_prompt_local_assigned_in_only_one_if_branch_is_rejected(self) -> None:
        template = '{% if row.flag %}{% set verdict = "YES" %}{% endif %}{{ verdict }}'

        with pytest.raises(ValidationError, match="prompt render context does not define") as exc_info:
            self._single(template)

        assert "'verdict'" in str(exc_info.value)

    def test_dynamic_access_error_keeps_primacy(self) -> None:
        """``{{ row.get(k) }}`` is both dynamic AND has an unbound ``k`` — the
        dynamic-access validator is defined first and must keep firing, or its
        opt-out guidance (required_input_fields: []) disappears behind the
        binding error."""
        with pytest.raises(ValidationError, match="dynamic row field access"):
            self._single("Secret: {{ row.get(k) }}", required_input_fields=["text"])

    # ── Single-prompt declaration agreement (elspeth-a9ba80cb0b) ────────

    def test_single_prompt_row_field_outside_declaration_rejected(self) -> None:
        """The reported defect: declare one field, reference another.

        Both were accepted before, the edge contract was satisfied by the
        DECLARATION, and every row then raised ``UndefinedError`` at render.
        """
        with pytest.raises(ValidationError, match="required_input_fields does not declare") as exc_info:
            self._single("Rate: {{ row.case_study }}", required_input_fields=["case_study_1"])
        message = str(exc_info.value)
        assert "'case_study'" in message
        assert "'case_study_1'" in message

    def test_single_prompt_declared_reference_accepted(self) -> None:
        template = "Rate: {{ row.case_study }}"
        assert self._single(template, required_input_fields=["case_study"]).prompt_template == template

    def test_single_prompt_wider_declaration_accepted(self) -> None:
        """A declaration wider than the template is a presence assertion, not a defect."""
        template = "Rate: {{ row.case_study }}"
        assert self._single(template, required_input_fields=["case_study", "audit_id"]).prompt_template == template

    def test_single_prompt_partially_declared_rejected(self) -> None:
        """Reads two fields, declares one — the shortfall names only the undeclared field."""
        with pytest.raises(ValidationError, match="required_input_fields does not declare") as exc_info:
            self._single("{{ row.a }} {{ row.b }}", required_input_fields=["a"])
        message = str(exc_info.value)
        assert "reads 'b' under 'row'" in message
        assert "reads 'a'" not in message and "'a', 'b'" not in message, "the declared field is not part of the shortfall"

    def test_single_prompt_conditional_reference_is_not_a_guard(self) -> None:
        """``{% if row.b %}`` forces ``__bool__`` on StrictUndefined and RAISES.

        Measured, not assumed — it reads like an optional guard and is not one,
        so it must be reported like any other unconditional read. (``is defined``
        and ``| default()`` genuinely do tolerate absence; no in-tree template
        pairs either with a non-empty declaration, and this rule deliberately
        does not attempt guard analysis to tell them apart.)
        """
        with pytest.raises(ValidationError, match="required_input_fields does not declare"):
            self._single("{% if row.b %}{{ row.b }}{% endif %}", required_input_fields=["a"])

    def test_single_prompt_undeclared_reference_raises_at_render_today(self) -> None:
        """Pins the runtime consequence the rejection claims, so the message cannot drift.

        Without this the message's "every row fails the whole node at render
        with 'Undeclared field'" is an unverified assertion: the row a template
        sees holds only the declared fields (ADR-051), so the undeclared read
        fails even on a row that carries the column.
        """
        from elspeth.plugins.infrastructure.templates import DeclaredFields, TemplateError, TemplateRow
        from elspeth.plugins.transforms.llm.templates import PromptTemplate

        row = TemplateRow.project(make_pipeline_row({"case_study": "x", "case_study_1": "y"}), DeclaredFields(frozenset({"case_study_1"})))
        with pytest.raises(TemplateError, match=r"^Undeclared field: the template reads 'case_study'"):
            PromptTemplate("Rate: {{ row.case_study }}").render(row)

    def test_single_prompt_original_header_literal_accepted_against_normalized_declaration(self) -> None:
        """``row["Original Header"]`` resolves through ``SchemaContract.find_name``.

        The literal is not a declarable name at all (``validate_field_names``
        requires an identifier), so it can only be an ``original_name`` and the
        row key it stands for is its canonical form. Declaring that key covers
        it — and is the ONLY thing that does.
        """
        template = 'Rate: {{ row["Original Header"] }}'
        assert self._single(template, required_input_fields=["original_header"]).prompt_template == template

    def test_single_prompt_uncovered_original_header_literal_rejected_naming_the_declarable_form(self) -> None:
        """A bracket literal is not exempt — it is bridged, then checked.

        Dropping it outright accepted a declaration that omitted the field
        entirely and then raised at render for every row, while the sibling
        declared-fields validator was already naming the right key. The message
        must name that key: the literal itself is rejected on application.
        """
        with pytest.raises(ValidationError, match="required_input_fields does not declare") as exc_info:
            self._single('Rate: {{ row["Original Header"] }}', required_input_fields=["something_else"])
        assert "declare as 'original_header'" in str(exc_info.value)

    @pytest.mark.parametrize("template", ["Hello {{ row.Name }}", "Hello {{ row['Name'] }}", "Hello {{ row.get('Name', '') }}"])
    def test_single_prompt_header_spelling_of_a_declared_field_accepted(self, template: str) -> None:
        """``{{ row.Name }}`` under a declared ``name`` reads ``name`` by its header spelling (ADR-051 (b), S-02).

        The template row resolves a declared field by its canonical and its
        recorded original name, so a source whose header is ``Name`` delivers
        it; a row whose header is spelled otherwise fails that row at render.
        """
        assert self._single(template, required_input_fields=["name"]).prompt_template == template

    def test_header_spelled_lookups_are_published_for_the_build(self) -> None:
        """Each spelling config admits is published (literal -> declared field): the build proves a row can carry it."""
        config = self._single(
            "{{ row['Name'] }} {{ row.score }} {{ row['Score_Text'] }}", required_input_fields=["name", "score", "score_text"]
        )
        assert config.header_spelled_row_lookups() == {"Name": "name", "Score_Text": "score_text"}

    def test_the_opt_out_publishes_no_lookup(self) -> None:
        assert self._single("{{ row['Name'] }}", required_input_fields=[]).header_spelled_row_lookups() == {}

    def test_multi_query_columns_and_source_row_reads_are_published(self) -> None:
        config = LLMConfig(
            provider="azure",
            prompt_template="Assess {{ row.text }} {{ row.source_row['Topic'] }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["name", "topic"],
            queries={"q1": {"input_fields": {"text": "Name"}}},
        )
        assert config.header_spelled_row_lookups() == {"Name": "name", "Topic": "topic"}

    def test_single_prompt_spelling_of_an_undeclared_field_rejected(self) -> None:
        """A spelling of a field the node does not declare is still an undeclared read."""
        with pytest.raises(ValidationError, match="required_input_fields does not declare") as exc_info:
            self._single("Hello {{ row.Title }}", required_input_fields=["name"])
        assert "'Title'" in str(exc_info.value)

    def test_single_prompt_keyword_literal_accepted(self) -> None:
        """``row["class"]`` cannot be declared — ``class`` is a Python keyword — so
        it is bridged to the key it resolves to."""
        template = 'Rate: {{ row["class"] }}'
        assert self._single(template, required_input_fields=["class_"]).prompt_template == template

    def test_remedy_ordering_leads_with_rewrite_not_declare(self) -> None:
        """The ordering is a finding, not a style choice.

        ``verify_declared_required_fields`` is a plain set difference over row
        keys with NO dual-name limb, so declaring a read name the producer does
        not guarantee is ACCEPTED here and then raises
        ``DeclaredRequiredInputFieldsViolation`` on every row. Leading with
        "add the name" would hand the planner a repair that clears this error
        and breaks the run.
        """
        with pytest.raises(ValidationError) as exc_info:
            self._single("Hello {{ row.title }}", required_input_fields=["name"])
        message = str(exc_info.value)
        assert message.index("Rewrite each reference") < message.index("Add a name to options.required_input_fields")
        assert "ONLY if the upstream producer guarantees that exact name" in message
        assert "refused when the pipeline is validated" in message

    def test_declaring_an_unguaranteed_read_name_is_refused_when_the_pipeline_is_validated(self, tmp_path: Path) -> None:
        """Pins the fact the remedy's warning rests on, so it cannot drift silently.

        The plugin config accepts the "just declare what you read" repair, but
        the pipeline's validation refuses it: an explicit required_input_fields
        name the producer does not guarantee fails Phase 1 (it used to be
        accepted and fail every row at run time).
        """
        import yaml
        from typer.testing import CliRunner

        from elspeth.cli import app

        self._single("Hello {{ row.Name }}", required_input_fields=["Name"])

        (tmp_path / "in.csv").write_text("Name\nAda\n")
        sink = {
            "plugin": "json",
            "on_write_failure": "discard",
            "options": {"path": str(tmp_path / "out.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
        }
        settings = {
            "sources": {
                "src": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"path": str(tmp_path / "in.csv"), "on_validation_failure": "discard", "schema": {"mode": "observed"}},
                }
            },
            "transforms": [
                {
                    "name": "greet",
                    "plugin": "llm",
                    "input": "rows",
                    "on_success": "out",
                    "on_error": "discard",
                    "options": {
                        "provider": "openrouter",
                        "model": "openai/gpt-4o",
                        "api_key": "placeholder-not-a-key",  # secret-scan: allow-this-line
                        "prompt_template": "Hello {{ row.Name }}",
                        "required_input_fields": ["Name"],
                        "schema": {"mode": "observed"},
                    },
                }
            ],
            "sinks": {"out": sink},
            "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        }
        (tmp_path / "settings.yaml").write_text(yaml.safe_dump(settings, sort_keys=False))

        result = CliRunner().invoke(app, ["validate", "-s", str(tmp_path / "settings.yaml")])

        assert result.exit_code == 1, result.output
        assert "Traceback" not in result.output
        assert "'Name'" in result.output

    def test_single_prompt_declaration_opt_out_suppresses_the_check(self) -> None:
        """``required_input_fields: []`` is the documented opt-out and must keep working."""
        template = "Rate: {{ row.case_study }}"
        assert self._single(template, required_input_fields=[]).prompt_template == template

    def test_single_prompt_undeclared_check_does_not_fire_without_a_declaration(self) -> None:
        """``None`` belongs to the sibling declared-validator, which owns the better message."""
        with pytest.raises(ValidationError, match="is not declared") as exc_info:
            self._single("Rate: {{ row.case_study }}", required_input_fields=None)
        assert "does not declare" not in str(exc_info.value)

    def test_undeclared_check_yields_to_the_dynamic_access_validator(self) -> None:
        """Dynamic access is defined first and keeps its own opt-out guidance."""
        with pytest.raises(ValidationError, match="dynamic row field access"):
            self._single("Rate: {{ row[k] }} {{ row.case_study }}", required_input_fields=["case_study_1"])

    def test_declared_validator_suggests_a_value_the_binding_check_then_accepts(self) -> None:
        """Applying the suggested remedy must CLEAR the error, not return a new one.

        The undeclared-fields message emits its suggestion verbatim; before this
        it emitted raw bracket literals that ``validate_field_names`` rejects on
        application, so the planner's only repair was itself invalid.
        """
        import json
        import re

        template = 'A {{ row["Original Header"] }} B {{ row.text }} C {{ row["!!!"] }}'
        with pytest.raises(ValidationError) as exc_info:
            self._single(template, required_input_fields=None)
        suggestion = re.search(r"options\.required_input_fields: (\[[^\]]*\])  # Require", str(exc_info.value))
        assert suggestion is not None, "no machine-applicable suggestion in the message"
        suggested = json.loads(suggestion.group(1))

        assert self._single(template, required_input_fields=suggested).required_input_fields == suggested

    # ── Multi-query mode ────────────────────────────────────────────────

    def test_query_override_bare_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="multi-query render context does not define") as exc_info:
            self._multi({"q1": {"input_fields": {"text": "body", "input_1": "body"}, "template": "Classify {{ text }}"}})
        message = str(exc_info.value)
        assert "'q1'" in message
        assert "'text'" in message
        assert "input_fields" in message

    def test_query_override_unbound_row_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="input_fields binds only") as exc_info:
            self._multi(
                {"diagnosis": {"input_fields": {"input_1": "background"}, "template": "BG: {{ row.input_1 }} SYM: {{ row.input_2 }}"}}
            )
        message = str(exc_info.value)
        assert "'diagnosis'" in message
        assert "'input_2'" in message
        assert "'input_1'" in message
        assert "source_row" in message

    def test_query_override_bound_row_fields_accepted(self) -> None:
        config = self._multi(
            {
                "q1": {
                    "input_fields": {"input_1": "background", "input-2": "symptoms"},
                    "template": (
                        "{{ row.input_1 }} / {{ row['input-2'] }} / {{ row.source_row.raw_column }}"
                        " / {{ lookup.rubric }} / {{ range(3) | join(', ') }}"
                    ),
                }
            }
        )
        assert config.queries is not None

    def test_query_override_local_assigned_in_every_if_branch_is_accepted(self) -> None:
        template = '{% if row.flag %}{% set verdict = "YES" %}{% else %}{% set verdict = "NO" %}{% endif %}{{ verdict }}'

        config = self._multi({"q1": {"input_fields": {"flag": "source_flag"}, "template": template}})

        assert config.queries is not None

    def test_query_override_local_assigned_in_only_one_if_branch_is_rejected(self) -> None:
        template = '{% if row.flag %}{% set verdict = "YES" %}{% endif %}{{ verdict }}'

        with pytest.raises(ValidationError, match="multi-query render context does not define") as exc_info:
            self._multi({"q1": {"input_fields": {"flag": "source_flag"}, "template": template}})

        message = str(exc_info.value)
        assert "'q1'" in message
        assert "'verdict'" in message

    def test_node_template_bare_name_used_by_query_rejected(self) -> None:
        """The legacy positional idiom ``{{ input_1 }}`` never binds — the
        render context wraps input_fields variables under ``row``."""
        with pytest.raises(ValidationError, match="multi-query render context does not define") as exc_info:
            self._multi({"q1": {"input_fields": {"input_1": "col_a"}}}, template="Assess: {{ input_1 }}")
        assert "'input_1'" in str(exc_info.value)

    def test_shared_node_template_local_assigned_in_every_if_branch_is_accepted(self) -> None:
        template = '{% if row.flag %}{% set verdict = "YES" %}{% else %}{% set verdict = "NO" %}{% endif %}{{ verdict }}'

        config = self._multi({"q1": {"input_fields": {"flag": "source_flag"}}}, template=template)

        assert config.queries is not None

    def test_shared_node_template_local_assigned_in_only_one_if_branch_is_rejected(self) -> None:
        template = '{% if row.flag %}{% set verdict = "YES" %}{% endif %}{{ verdict }}'

        with pytest.raises(ValidationError, match="multi-query render context does not define") as exc_info:
            self._multi({"q1": {"input_fields": {"flag": "source_flag"}}}, template=template)

        assert "'verdict'" in str(exc_info.value)

    def test_node_template_used_by_no_query_is_not_checked(self) -> None:
        """Every query overrides the template, so the node-level slot never
        renders — the shipped multi-query examples carry exactly this dead
        template and must keep validating."""
        config = self._multi(
            {"q1": {"input_fields": {"text": "body"}, "template": "Classify {{ row.text }}"}},
            template="Assess: {{ input_1 }}",
        )
        assert config.queries is not None

    def test_shared_node_template_checked_against_each_querys_bindings(self) -> None:
        with pytest.raises(ValidationError, match="input_fields binds only") as exc_info:
            self._multi(
                {
                    "ok_query": {"input_fields": {"input_1": "col_a"}},
                    "broken_query": {"input_fields": {"text": "col_b"}},
                },
                template="Assess: {{ row.input_1 }}",
            )
        message = str(exc_info.value)
        assert "'broken_query'" in message

    def test_from_dict_wraps_binding_error_as_plugin_config_error(self) -> None:
        """The web/probe path must see the redacted-safe §5.3 category, not a
        bare ValueError escaping as a 500."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError

        bad = {
            "provider": "azure",
            "prompt_template": "Assess: {{ row.input_1 }}",
            "schema": {"mode": "observed"},
            "required_input_fields": [],
            "queries": {"q1": {"input_fields": {"input_1": "col"}, "template": "{{ row.nope }}"}},
        }
        with pytest.raises(PluginConfigError, match="input_fields binds only"):
            LLMConfig.from_dict(bad, plugin_name="llm")


def test_the_only_field_an_llm_transform_writes_as_any_is_the_usage_mapping() -> None:
    """LLMConfig refuses every authored scalar over a written-``any`` field because that field is the usage mapping.

    If another written field ever becomes ``any``, that refusal's reason (and
    message) would be false for it: this pin makes the change re-decide it.
    """
    from elspeth.plugins.transforms.llm import _OUTPUT_FIELD_TYPE_TO_SCHEMA, _SUFFIX_SCHEMA_TYPES

    assert {suffix for suffix, field_type in _SUFFIX_SCHEMA_TYPES.items() if field_type == "any"} == {"_usage"}
    assert "any" not in _OUTPUT_FIELD_TYPE_TO_SCHEMA.values()
