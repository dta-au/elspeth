"""Plugin review 2026-11-11, P4 findings for LLM transforms and image/PDF inputs.

#33 template-misuse refusals name the offending template slot
#34 ordered-mode validation compiles the query plan a bounded number of times
#46 single-query tracer omits an empty system prompt the provider never got
#87 LLM_AUDIT_SUFFIXES registers _parts_hash
#88 populate_llm_operational_fields requires usage and model
#143 the dead validate_json_object_response parser is gone
#154 too_many_images does not report max+1 as the actual count
#59 the audited render request records max_total_bytes
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMProvider, LLMQueryResult

# ── #33: misuse in a query template is attributed to that query ─────────────


def _query_misuse_options(template: str) -> dict[str, Any]:
    return {
        "provider": "azure",
        "schema": {"mode": "observed"},
        "prompt_template": "Classify: {{ row.x }}",
        "required_input_fields": ["a"],
        "queries": [{"name": "q1", "input_fields": {"x": "a"}, "template": template}],
    }


@pytest.mark.parametrize(
    "template",
    [
        pytest.param("{{ row.source_row.keys() }}", id="row-as-object"),
        pytest.param("{{ row.source_row[row.x] }}", id="computed-key"),
    ],
)
def test_query_template_misuse_names_the_query_not_prompt_template(template: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        LLMConfig.model_validate(_query_misuse_options(template))
    message = str(exc_info.value)
    assert "LLM query 'q1' template" in message, message
    assert "LLM prompt_template" not in message, message


def test_prompt_template_misuse_still_names_prompt_template() -> None:
    options = _query_misuse_options("{{ row.x }}")
    options["prompt_template"] = "{{ row.source_row.keys() }}"
    del options["queries"][0]["template"]
    with pytest.raises(ValidationError) as exc_info:
        LLMConfig.model_validate(options)
    message = str(exc_info.value)
    assert "LLM prompt_template uses its row as an object" in message, message
    assert "query 'q1'" not in message, message


# ── #34: ordered plan compiled a bounded number of times ────────────────────


def _ordered_options(query_count: int) -> dict[str, Any]:
    queries: list[dict[str, Any]] = [{"name": "q0", "input_fields": {"text": "text"}, "template": "{{ row.text }}"}]
    for index in range(1, query_count):
        queries.append({"name": f"q{index}", "input_fields": {"value": f"q{index - 1}_answer"}, "template": "{{ row.value }}"})
    return {
        "provider": "azure",
        "schema": {"mode": "observed"},
        "query_input_mode": "ordered",
        "pool_size": 1,
        "required_input_fields": ["text"],
        "response_field": "answer",
        "queries": queries,
    }


def _plan_compilations(monkeypatch: pytest.MonkeyPatch, query_count: int) -> int:
    from elspeth.plugins.transforms.llm import base

    real = base.compile_ordered_query_plan
    calls: list[int] = []

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(base, "compile_ordered_query_plan", counting)
    config = LLMConfig.model_validate(_ordered_options(query_count))
    config.header_spelled_row_lookups()
    monkeypatch.setattr(base, "compile_ordered_query_plan", real)
    return len(calls)


def test_ordered_validation_compiles_the_plan_independently_of_query_count(monkeypatch: pytest.MonkeyPatch) -> None:
    one = _plan_compilations(monkeypatch, 1)
    five = _plan_compilations(monkeypatch, 5)
    assert five == one, f"plan compiled {one}x for 1 query but {five}x for 5 queries"


def test_ordered_external_inputs_are_unchanged() -> None:
    config = LLMConfig.model_validate(_ordered_options(3))
    specs = config.resolved_queries()
    assert [config.external_query_inputs(spec) for spec in specs] == [frozenset({"text"}), frozenset(), frozenset()]


# ── #46: single mode traces no empty system message ─────────────────────────


@pytest.mark.parametrize("system_prompt", ["", "Follow the rubric."])
def test_single_mode_tracer_system_prompt_matches_provider_messages(system_prompt: str) -> None:
    from elspeth.plugins.transforms.llm.langfuse import LangfuseTracer
    from elspeth.plugins.transforms.llm.transform import LLMTransform
    from tests.unit.plugins.llm.test_transform import _make_config, _make_ctx, _make_row

    transform = LLMTransform(_make_config(system_prompt=system_prompt))
    provider = Mock(spec=LLMProvider)
    provider.execute_query.return_value = LLMQueryResult(
        content="ok", usage=TokenUsage.known(1, 1), model="gpt-4o", finish_reason=FinishReason.STOP
    )
    tracer = Mock(spec=LangfuseTracer)
    transform._provider = provider
    transform._tracer = tracer

    result = transform._process_row(_make_row(), _make_ctx())

    assert result.status == "success"
    sent = provider.execute_query.call_args.args[0]
    traced_system = tracer.record_success.call_args.kwargs["system_prompt"]
    if system_prompt:
        assert sent[0] == ChatMessage(role="system", content=system_prompt)
        assert traced_system == system_prompt
    else:
        assert [message.role for message in sent] == ["user"]
        assert traced_system is None


# ── #87: _parts_hash is a registered audit suffix ───────────────────────────


def test_parts_hash_is_a_registered_audit_suffix() -> None:
    from elspeth.plugins.transforms import llm

    assert "_parts_hash" in llm.LLM_AUDIT_SUFFIXES
    assert "r_parts_hash" in llm.get_llm_audit_fields("r")
    assert llm.__doc__ is not None
    assert "<response_field>_parts_hash" in llm.__doc__
    emitted = llm.build_llm_audit_metadata(
        "r",
        template_hash="t",
        variables_hash=None,
        template_source=None,
        lookup_hash=None,
        lookup_source=None,
        system_prompt_source=None,
        parts_hash="p",
    )
    assert set(emitted) == set(llm.get_llm_audit_fields("r"))


# ── #88: operational fields require usage and model ─────────────────────────


def test_populate_llm_operational_fields_parameters_are_not_optional() -> None:
    from elspeth.plugins.transforms.llm import populate_llm_operational_fields

    parameters = inspect.signature(populate_llm_operational_fields).parameters
    assert parameters["usage"].annotation == "TokenUsage"
    assert parameters["model"].annotation == "str"

    output: dict[str, object] = {}
    absent_usage: Any = None  # a deliberate Tier-2 invariant breach
    raised = False
    try:
        populate_llm_operational_fields(output, "r", usage=absent_usage, model="m")
    except AttributeError:
        raised = True
    assert raised, f"usage=None was accepted and wrote {output!r}"


# ── #143: the unused parser and its result types are removed ────────────────


def test_dead_json_object_parser_is_removed() -> None:
    from elspeth.plugins.transforms.llm import validation

    for name in ("validate_json_object_response", "ValidationSuccess", "ValidationError", "ValidationResult"):
        assert name not in vars(validation), name


# ── #154: too_many_images reports a lower bound, not max+1 ──────────────────


def test_too_many_images_reports_a_lower_bound_not_max_plus_one() -> None:
    from elspeth.contracts.results import TransformResult
    from elspeth.plugins.transforms.llm.image_inputs import ImageInputConfig
    from elspeth.testing import make_pipeline_row
    from tests.unit.plugins.llm.test_image_inputs import PNG_BYTES, PNG_SHA256, FakePayloadStore, _resolve

    store = FakePayloadStore({PNG_SHA256: PNG_BYTES})
    row = make_pipeline_row({"pictures": [PNG_SHA256] * 500})
    result = _resolve(row, [ImageInputConfig(field="pictures", format="png")], store=store, max_images_per_call=4)
    assert isinstance(result, TransformResult)
    assert result.reason == {"reason": "too_many_images", "max_images": 4, "actual": "more than 4"}


# ── #59: the audited render request records max_total_bytes ─────────────────


def test_render_request_records_max_total_bytes() -> None:
    from elspeth.contracts.pdf_render import PDFRenderLimitsData
    from elspeth.plugins.transforms.pdf_rasterize import PDFRasterize

    assert "max_total_bytes" in PDFRenderLimitsData.__required_keys__
    transform = PDFRasterize({"schema": {"mode": "observed"}, "max_total_bytes": 123_456})
    request = transform._render_request("sha256:abc")
    assert request["render_limits"]["max_total_bytes"] == 123_456
