"""Bounds on template-name discovery before Composer admits authored text."""

import json
from dataclasses import replace
from uuid import UUID

import pytest

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.templates import MAX_JINJA_TEMPLATE_BYTES, JinjaTemplateSizeExceeded, extract_jinja2_fields
from elspeth.plugins.infrastructure.templates import TemplateError, create_sandboxed_environment
from elspeth.web.composer import advisor_context
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, _parse_template_names
from tests.unit.web.composer._helpers import _make_settings


def _template_source(size: int, *, multibyte: bool) -> str:
    prefix = "Inspect {{ row.field }}. "
    remaining = size - len(prefix.encode("utf-8"))
    return prefix + ("é" * (remaining // 2) + " " * (remaining % 2) if multibyte else " " * remaining)


def _graph_with_effective_template(template: str, shape: str) -> CompositionState:
    if shape == "node":
        options = {"prompt_template": template}
    elif shape == "query_override":
        options = {"prompt_template": "Unused {{ row.unused }}.", "queries": {"review": {"template": template}}}
    else:
        options = {"options": {"prompt_template": template, "queries": {"review": {"input_fields": {"field": "field"}}}}}
    return CompositionState(
        nodes=(
            NodeSpec(
                id="review",
                node_type="transform",
                plugin="llm",
                input="input",
                on_success="output",
                on_error=None,
                options=options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _prepare(state: CompositionState, owner: str):
    scope = ComposerContentScope(UUID(int=1), composition_content_hash(state))
    if owner == "manual":
        return advisor_context.fit_advisor_checkpoint_summary(state, {}, 0, content_scope=scope)
    checkpoint = AdvisorCheckpointOwner(
        settings=_make_settings(),
        composer_skill_text="Synthetic Advisor policy.",
        advisor_endpoint_base_url=None,
        advisor_endpoint_api_key=None,
        sessions_service=None,
        chargeable_admission=ComposerChargeableAdmission(None),
    )
    return checkpoint._build_checkpoint_arguments(phase=owner, state=state, content_scope=scope)


def test_template_name_advisory_rejects_unbounded_compiler_expressions() -> None:
    for source in (
        "{{ (3**(3**16)) % 7 }}{{ row.name }}",
        "{% autoescape ('x' * 1000000000)|length > 0 %}{{ row.name }}{% endautoescape %}",
    ):
        parsed, error = _parse_template_names(source)
        assert parsed is None
        assert error is not None


@pytest.mark.parametrize("multibyte", [False, True], ids=["ascii", "utf8"])
def test_shared_template_source_limit_keeps_its_nominal_size_signal(multibyte: bool) -> None:
    assert MAX_JINJA_TEMPLATE_BYTES == 16_384
    fitting = _template_source(16_384, multibyte=multibyte)
    excess = _template_source(16_385, multibyte=multibyte)
    assert len(fitting.encode("utf-8")) == 16_384
    assert len(excess.encode("utf-8")) == 16_385
    assert extract_jinja2_fields(fitting) == frozenset({"field"})
    with pytest.raises(JinjaTemplateSizeExceeded):
        extract_jinja2_fields(excess)
    # The shared validation/preview parser still enforces the same engine
    # limit through its existing TemplateError contract.
    assert create_sandboxed_environment().parse(fitting) is not None
    with pytest.raises(TemplateError, match="16384 UTF-8 bytes"):
        create_sandboxed_environment().parse(excess)
    parsed, error = _parse_template_names(excess)
    assert parsed is None and error == "Template exceeds 16384 UTF-8 bytes"


@pytest.mark.parametrize("owner", ["manual", "early", "end"])
@pytest.mark.parametrize("shape", ["node", "query_override", "nested_node_fallback"])
@pytest.mark.parametrize("multibyte", [False, True], ids=["ascii", "utf8"])
def test_advisor_preparation_classifies_only_effective_template_size_excess(owner: str, shape: str, multibyte: bool) -> None:
    fitting = _template_source(16_384, multibyte=multibyte)
    fitting_state = _graph_with_effective_template(fitting, shape)
    prepared = _prepare(fitting_state, owner)
    summary = prepared if owner == "manual" else prepared["_elspeth_advisor_summary_parts"]
    assert fitting in "".join(part.text for part in summary.values)
    excess = _template_source(16_385, multibyte=multibyte)
    with pytest.raises(ComposerContextUnsupported) as refused:
        _prepare(_graph_with_effective_template(excess, shape), owner)
    assert refused.value.reason == "graph_resource_limit"
    assert type(refused.value.__cause__) is JinjaTemplateSizeExceeded
    assert excess not in str(refused.value) and excess not in json.dumps(refused.value.to_payload())
    assert refused.value.llm_calls == ()


@pytest.mark.parametrize("owner", ["manual", "early", "end"])
def test_unused_node_template_does_not_replace_effective_query_analysis(owner: str) -> None:
    state = _graph_with_effective_template("Effective {{ row.field }}.", "query_override")
    state = replace(state, nodes=(replace(state.nodes[0], options={**state.nodes[0].options, "prompt_template": " " * 16_385}),))
    assert _prepare(state, owner) is not None


def test_malformed_template_remains_explicit_unknown_analysis() -> None:
    state = _graph_with_effective_template("{{ row.field", "node")
    assert "named row field references: UNKNOWN (invalid template syntax)" in advisor_context.summarize_pipeline_for_advisor(state)
    assert advisor_context._render_interpolated_row_fields_parts(state.nodes[0]) == (
        "named row field references: UNKNOWN (invalid template syntax)",
    )


@pytest.mark.parametrize("owner", ["manual", "early", "end"])
def test_other_template_resource_failures_keep_their_original_exception(owner: str) -> None:
    state = _graph_with_effective_template("{{ row.field }}" * 257, "node")
    with pytest.raises(ValueError, match="too many Jinja expressions or blocks") as failure:
        _prepare(state, owner)
    assert type(failure.value) is ValueError


@pytest.mark.parametrize("owner", ["manual", "early", "end"])
@pytest.mark.parametrize("failure_type", [ValueError, AuditIntegrityError])
def test_template_preparation_does_not_classify_foreign_errors_by_message(owner, failure_type, monkeypatch) -> None:
    failure = failure_type("Template exceeds 16384 UTF-8 bytes")

    def fail_field_extraction(_template):
        raise failure

    monkeypatch.setattr(advisor_context, "extract_jinja2_fields", fail_field_extraction)
    with pytest.raises(failure_type) as propagated:
        _prepare(_graph_with_effective_template("{{ row.field }}", "node"), owner)
    assert propagated.value is failure
