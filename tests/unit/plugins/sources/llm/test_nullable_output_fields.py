"""Source-native nullable extraction preserves declared types and refusal paths."""

from __future__ import annotations

import json

import pytest

from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMQueryResult
from tests.unit.plugins.sources.llm.conftest import FakeProvider
from tests.unit.plugins.sources.llm.test_source import _install_provider, _structured_source


@pytest.mark.parametrize("mode", ["observed", "flexible", "fixed"])
@pytest.mark.parametrize(("kind", "python_type"), [("string", str), ("integer", int), ("number", float), ("boolean", bool), ("enum", str)])
def test_source_null_is_a_required_nullable_declared_scalar(openrouter_config, source_context, mode, kind, python_type):
    descriptor = {"suffix": "result", "type": kind, "nullable": True}
    if kind == "enum":
        descriptor["values"] = ["green", "blue"]
    schema = {"mode": mode}
    if mode != "observed":
        schema["fields"] = ["answer: str"]
    source = _structured_source(openrouter_config, source_context, output_fields=[descriptor], schema=schema)
    provider = FakeProvider(
        LLMQueryResult(
            content='{"result":null}',
            usage=TokenUsage.known(prompt_tokens=7, completion_tokens=3),
            model="served-model",
            finish_reason=FinishReason.STOP,
        )
    )
    _install_provider(source, provider)
    rows = list(source.load(source_context))
    assert len(rows) == 1 and rows[0].row["result"] is None
    assert rows[0].contract is not None
    field = rows[0].contract.get_field("result")
    assert field.python_type is python_type and field.required and field.nullable
    assert field.source == "declared"
    assert source._schema_config.mode == mode
    sent = provider.response_formats[0]["json_schema"]["schema"]
    assert sent["required"] == ["result"]
    assert sent["properties"]["result"]["type"] == ["string" if kind == "enum" else kind, "null"]
    if kind == "enum":
        assert sent["properties"]["result"]["enum"] == ["green", "blue", None]


@pytest.mark.parametrize(("nullable", "body"), [(False, {"result": None}), (True, {}), (True, {"result": True})])
def test_source_nullable_flag_does_not_bypass_existing_quarantine(openrouter_config, source_context, nullable, body):
    source = _structured_source(
        openrouter_config,
        source_context,
        output_fields=[{"suffix": "result", "type": "number", "nullable": nullable}],
        on_validation_failure="quarantine",
    )
    provider = FakeProvider(
        LLMQueryResult(
            content=json.dumps(body),
            usage=TokenUsage.known(prompt_tokens=7, completion_tokens=3),
            model="served-model",
            finish_reason=FinishReason.STOP,
        )
    )
    _install_provider(source, provider)
    rows = list(source.load(source_context))
    assert len(rows) == 1 and rows[0].is_quarantined
    assert rows[0].quarantine_destination == "quarantine"
