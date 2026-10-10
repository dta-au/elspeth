"""Composer/runtime constructor-time template contract agreement.

The ADR-040 divergence registry remains in test_composer_runtime_agreement.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings, TransformSettings
from elspeth.plugins.infrastructure.config_base import PluginConfigError


class TestComposerRuntimeTemplateLiteralAgreement:
    """Shape 30 — a template whose own literals fail on every row (elspeth-5887fb7928 S3).

    The composer side is its mutation gate (``_prevalidate_transform``, what
    upsert_node / set_pipeline run); the runtime side is plugin instantiation
    from settings, as ``elspeth validate`` does. Both must refuse the same
    template text with the same template message, for ``llm`` and
    ``rag_retrieval``; a template whose failure depends on the row is admitted
    by both.

    Bug verification protocol: removing the ``_check_configuration_literals``
    call from ``_BoundedEnvironment.parse`` makes every rejection case fail on
    both sides (the composer returns None, the runtime DID NOT RAISE);
    removing ``RetrievalOutputConfig.validate_query_template`` fails the
    ``rag_retrieval`` composer assertions only (the runtime still refuses at
    QueryBuilder construction) — the pre-fix disagreement.
    """

    _REFUSED = (
        pytest.param(
            "{{ row.q | truncate(2) }}", "truncate() arguments can never be satisfied: expected length >= 3, got 2", id="truncate"
        ),
        pytest.param(
            "{{ row.q | truncate(1" + "0" * 400 + ", leeway=0.5) }}",
            "truncate() arguments can never be satisfied: int too large to convert to float",
            id="truncate-overflow",
        ),
        pytest.param(
            "{{ row.q | truncate(10.5) }}", "truncate() length must be an integer literal, got float.", id="truncate-float-length"
        ),
        pytest.param(
            "{{ row.q | truncate(10.5, leeway=row.n) }}",
            "truncate() length must be an integer literal, got float.",
            id="truncate-float-length-beside-row-leeway",
        ),
        pytest.param("{{ row.q | truncate(True) }}", "truncate() length must be an integer literal, got bool.", id="truncate-bool-length"),
        # Each precondition over only the arguments it reads (review-F1-final-minors-r1 F1).
        pytest.param(
            "{{ row.q | truncate(2, leeway=row.n) }}",
            "truncate() arguments can never be satisfied: expected length >= 3, got 2",
            id="truncate-short-length-beside-row-leeway",
        ),
        pytest.param(
            "{{ row.q | truncate(row.n, leeway=-1) }}",
            "truncate() arguments can never be satisfied: expected leeway >= 0, got -1",
            id="truncate-negative-leeway-beside-row-length",
        ),
        pytest.param(
            "{{ row.q | truncate(10, foo=1) }}", "truncate() got an unexpected keyword argument 'foo'.", id="truncate-unknown-keyword"
        ),
        pytest.param(
            "{{ row.q | truncate(1e400) }}",
            "A number literal in this template is too large for a float (it overflows to infinity).",
            id="infinite-literal",
        ),
        pytest.param("{% if row.q %}{{ row.q | no_such_filter }}{% endif %}", "No filter named 'no_such_filter'.", id="filter-in-if"),
        pytest.param("{% if row.q is no_such_test %}x{% endif %}", "No test named 'no_such_test'.", id="test-in-if"),
        pytest.param("{{ [row.q] | map('no_such_filter') | join }}", "No filter named 'no_such_filter'.", id="map-filter-name"),
        pytest.param("{{ row.q | }}", "expected token 'name', got 'end of print statement'", id="malformed"),
    )

    @staticmethod
    def _options(plugin: str, template: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """(composer options, runtime options) for one template."""
        if plugin == "llm":
            common: dict[str, Any] = {
                "provider": "openrouter",
                "model": "openai/gpt-4.1-nano",
                "prompt_template": template,
                "required_input_fields": ["q", "n"],
                "schema": {"mode": "observed"},
            }
            return {**common, "api_key": {"secret_ref": "OPENROUTER_API_KEY"}}, {**common, "api_key": "sk-test-key"}
        rag = {
            "query_field": "q",
            "query_template": template,
            # The template reads row.n: declared, as the llm arm does (Shape 32).
            "required_input_fields": ["q", "n"],
            "output_prefix": "sci",
            "provider": "chroma",
            "provider_config": {"collection": "agreement", "mode": "ephemeral"},
            "schema": {"mode": "observed"},
        }
        return rag, rag

    @staticmethod
    def _runtime(tmp_path: Path, plugin: str, options: dict[str, Any]) -> None:
        csv_path = tmp_path / "in.csv"
        csv_path.write_text("q,n\nhello,1\n", encoding="utf-8")
        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="t1",
                    options={
                        "path": str(csv_path),
                        "schema": {"mode": "observed", "guaranteed_fields": ["q", "n"]},
                        "on_validation_failure": "discard",
                    },
                )
            },
            transforms=[TransformSettings(name="t1", plugin=plugin, input="t1", on_success="main", on_error="discard", options=options)],
            sinks={
                "main": SinkSettings(
                    plugin="csv", on_write_failure="discard", options={"path": str(tmp_path / "out.csv"), "schema": {"mode": "observed"}}
                )
            },
        )
        instantiate_plugins_from_config(config)

    @pytest.mark.parametrize("plugin", ["llm", "rag_retrieval"])
    @pytest.mark.parametrize(("template", "message"), _REFUSED)
    def test_both_refuse_a_template_its_literals_fail(self, tmp_path: Path, plugin: str, template: str, message: str) -> None:
        from elspeth.web.composer.tools._common import _prevalidate_transform

        composer_options, runtime_options = self._options(plugin, template)
        composer = _prevalidate_transform(plugin, composer_options)
        assert composer is not None
        assert message in composer, composer
        with pytest.raises(PluginConfigError, match=re.escape(message)):
            self._runtime(tmp_path, plugin, runtime_options)

    @pytest.mark.parametrize("plugin", ["llm", "rag_retrieval"])
    @pytest.mark.parametrize(
        "template",
        [
            pytest.param("{{ row.q | truncate(row.n) }}", id="truncate-length-from-the-row"),
            pytest.param("{% if row.q %}{{ row.q | truncate(3) }}{% endif %}", id="valid-literal-inside-if"),
            pytest.param("{{ row.q | truncate(3, leeway=row.n) }}", id="satisfied-length-beside-row-leeway"),
            pytest.param("{{ row.q | truncate(0, end=row.q) }}", id="zero-length-beside-row-end"),
        ],
    )
    def test_both_admit_a_template_whose_failure_depends_on_the_row(self, tmp_path: Path, plugin: str, template: str) -> None:
        """Control: the row decides these, so they build and any failure routes per row."""
        from elspeth.web.composer.tools._common import _prevalidate_transform

        composer_options, runtime_options = self._options(plugin, template)
        assert _prevalidate_transform(plugin, composer_options) is None
        self._runtime(tmp_path, plugin, runtime_options)


class TestComposerRuntimeRagQueryTemplateAgreement:
    """Shape 32 — a RAG query template reads only what its node declares (elspeth-5887fb7928 P3, ADR-051 (f)).

    The composer side is its mutation gate (``_prevalidate_transform``); the
    runtime side is plugin instantiation from settings, as ``elspeth
    validate`` does. Both run ``RetrievalOutputConfig``, so both refuse the
    same template with the same text, and admit the same controls.

    Bug verification protocol: deleting the
    ``RetrievalOutputConfig._validate_query_template_row_reads`` validator
    makes every refusal case fail on both sides (the composer returns None,
    the runtime DID NOT RAISE); the controls stay green.
    """

    _REFUSED = (
        pytest.param(
            "{{ query }} {{ row.secret }}",
            ["q"],
            "query_template reads 'secret' under 'row', which this node does not declare",
            id="undeclared",
        ),
        pytest.param(
            "{{ query }} {{ row.secret }}",
            None,
            "query_template reads 'secret' under 'row', but options.required_input_fields is not declared",
            id="omitted-with-row-reads",
        ),
        pytest.param(
            "{{ query }} {{ row[query] }}", ["q"], "query_template uses dynamic row field access (item via row[expr])", id="computed-key"
        ),
        pytest.param(
            "{{ query }} words", ["q", "n"], "options.required_input_fields declares 'n', but query_template never reads 'row'", id="dual"
        ),
        pytest.param("{{ qurey }}", [], "query_template references 'qurey', which the query render context does not define", id="unbound"),
    )

    @staticmethod
    def _options(template: str, required_input_fields: list[str] | None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "query_field": "q",
            "query_template": template,
            "output_prefix": "sci",
            "provider": "chroma",
            "provider_config": {"collection": "agreement", "mode": "ephemeral"},
            "schema": {"mode": "observed"},
        }
        if required_input_fields is not None:
            options["required_input_fields"] = required_input_fields
        return options

    @pytest.mark.parametrize(("template", "required_input_fields", "message"), _REFUSED)
    def test_both_refuse_a_read_outside_the_declaration(
        self, tmp_path: Path, template: str, required_input_fields: list[str] | None, message: str
    ) -> None:
        from elspeth.web.composer.tools._common import _prevalidate_transform

        options = self._options(template, required_input_fields)
        composer = _prevalidate_transform("rag_retrieval", options)
        assert composer is not None
        assert message in composer, composer
        with pytest.raises(PluginConfigError, match=re.escape(message)):
            TestComposerRuntimeTemplateLiteralAgreement._runtime(tmp_path, "rag_retrieval", options)

    @pytest.mark.parametrize(
        ("template", "required_input_fields"),
        [
            pytest.param("{{ query }} {{ row.n }}", ["n"], id="declared-read"),
            pytest.param("{{ row.q }}", None, id="the-query-field-needs-no-declaration"),
            pytest.param("{{ query }} {{ row | dictsort }}", ["q", "n"], id="whole-row-use-holds-the-declaration"),
            pytest.param("{{ query }} {{ row[query] }}", [], id="the-opt-out"),
        ],
    )
    def test_both_admit_a_read_the_declaration_covers(self, tmp_path: Path, template: str, required_input_fields: list[str] | None) -> None:
        from elspeth.web.composer.tools._common import _prevalidate_transform

        options = self._options(template, required_input_fields)
        assert _prevalidate_transform("rag_retrieval", options) is None
        TestComposerRuntimeTemplateLiteralAgreement._runtime(tmp_path, "rag_retrieval", options)


class TestComposerRuntimeTemplateRowApiAgreement:
    """Shape 34 — the one template row API (ADR-051 (c), elspeth-5887fb7928 G3): both surfaces refuse the row as an object.

    A retired row-API name, a call on a row field and an uncalled ``row.get``
    are refused under every declaration, ``[]`` included, for ``llm`` (its
    ``row`` and a query's ``row.source_row``) and ``rag_retrieval``; a field
    read and every whole-row filter are admitted by both. The composer side is
    its mutation gate (``_prevalidate_transform``); the runtime side is plugin
    instantiation from settings, as ``elspeth validate`` does.

    Bug verification protocol: restoring the declaration-dependent early
    return ahead of the row-API check (``LLMConfig._validate_template_row_access``
    returning on ``[]`` first; ``RetrievalOutputConfig`` returning on
    ``AllFields`` first) turns every ``[]`` refusal case red on BOTH sides.
    """

    _REFUSED = (
        pytest.param("{{ row.to_dict() }}", "row.contract, row.to_dict", id="retired-name"),
        pytest.param("{{ row.keys() | list }}", "a call on a row field", id="keys-call"),
        pytest.param("{{ row['keys']() }}", "a call on a row field", id="item-call"),
        pytest.param("{{ row.get }}", "row.get without a call", id="uncalled-get"),
    )

    @staticmethod
    def _options(plugin: str, template: str, required_input_fields: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
        if plugin == "llm":
            common: dict[str, Any] = {
                "provider": "openrouter",
                "model": "openai/gpt-4.1-nano",
                "prompt_template": template,
                "required_input_fields": required_input_fields,
                "schema": {"mode": "observed"},
            }
            return {**common, "api_key": {"secret_ref": "OPENROUTER_API_KEY"}}, {**common, "api_key": "sk-test-key"}
        if plugin == "llm-multi-query":
            multi: dict[str, Any] = {
                "provider": "openrouter",
                "model": "openai/gpt-4.1-nano",
                "prompt_template": "x",
                "queries": {"q1": {"input_fields": {"text": "q"}, "template": template.replace("row", "row.source_row")}},
                "required_input_fields": required_input_fields,
                "schema": {"mode": "observed"},
            }
            return {**multi, "api_key": {"secret_ref": "OPENROUTER_API_KEY"}}, {**multi, "api_key": "sk-test-key"}
        rag = {
            "query_field": "q",
            "query_template": "{{ query }} " + template,
            "required_input_fields": required_input_fields,
            "output_prefix": "sci",
            "provider": "chroma",
            "provider_config": {"collection": "agreement", "mode": "ephemeral"},
            "schema": {"mode": "observed"},
        }
        return rag, rag

    @pytest.mark.parametrize("plugin", ["llm", "llm-multi-query", "rag_retrieval"])
    @pytest.mark.parametrize("required_input_fields", [pytest.param(["q", "n"], id="list"), pytest.param([], id="opt-out")])
    @pytest.mark.parametrize(("template", "message"), _REFUSED)
    def test_both_refuse_the_row_used_as_an_object(
        self, tmp_path: Path, plugin: str, required_input_fields: list[str], template: str, message: str
    ) -> None:
        from elspeth.web.composer.tools._common import _prevalidate_transform

        composer_options, runtime_options = self._options(plugin, template, required_input_fields)
        registered = "llm" if plugin == "llm-multi-query" else plugin
        composer = _prevalidate_transform(registered, composer_options)
        assert composer is not None
        assert "uses its row as an object" in composer, composer
        assert message in composer, composer
        assert "required_input_fields: []" not in composer
        with pytest.raises(PluginConfigError, match=re.escape(message)):
            TestComposerRuntimeTemplateLiteralAgreement._runtime(tmp_path, registered, runtime_options)

    @pytest.mark.parametrize("plugin", ["llm", "llm-multi-query", "rag_retrieval"])
    @pytest.mark.parametrize("required_input_fields", [pytest.param(["q", "n"], id="list"), pytest.param([], id="opt-out")])
    @pytest.mark.parametrize(
        "template",
        [
            pytest.param("{{ row.get('n', 'none') }} {{ row.q.upper() }}", id="get-and-a-value-method"),
            pytest.param("{{ row | tojson }} {{ row | dictsort }} {{ row | list }} {{ dict(row) }} {{ row }}", id="whole-row-forms"),
        ],
    )
    def test_both_admit_a_field_read_and_the_whole_row_forms(
        self, tmp_path: Path, plugin: str, required_input_fields: list[str], template: str
    ) -> None:
        from elspeth.web.composer.tools._common import _prevalidate_transform

        composer_options, runtime_options = self._options(plugin, template, required_input_fields)
        registered = "llm" if plugin == "llm-multi-query" else plugin
        assert _prevalidate_transform(registered, composer_options) is None
        TestComposerRuntimeTemplateLiteralAgreement._runtime(tmp_path, registered, runtime_options)
