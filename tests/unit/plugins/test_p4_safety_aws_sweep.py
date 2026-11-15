"""Plugin review 2026-11-11 P4 sweep: Textract, Bedrock guardrail, Azure content safety.

Findings #38, #127, #145, #158, #159 (#37 was already fixed by 06c108ea).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from elspeth.plugins.transforms.aws.guardrail_profiles import BedrockGuardrailProfileSettings
from elspeth.plugins.transforms.aws.guardrails_client import BedrockGuardrailsClient, GuardrailDecision
from elspeth.plugins.transforms.aws.textract_client import InlineAnalysisResult
from elspeth.plugins.transforms.aws.textract_config_shared import TextractQueryConfig
from elspeth.plugins.transforms.azure.content_safety import AzureContentSafety
from elspeth.testing import make_pipeline_row
from tests.unit.plugins.transforms.aws import test_textract_document_analysis as doc_analysis
from tests.unit.plugins.transforms.aws import test_textract_inline_analysis as inline_analysis
from tests.unit.plugins.transforms.aws.test_bedrock_prompt_shield import _config as _shield_config
from tests.unit.plugins.transforms.aws.test_bedrock_prompt_shield import _decision, _started_transform

# --- #38: the audited error category must not be chosen from provider text ---


def _blocks_with_provider_text(marker: str) -> list[dict[str, object]]:
    """A malformed graph whose error message embeds provider-controlled text."""
    return [
        {"BlockType": "PAGE", "Id": "page-1", "Page": 1},
        {
            "BlockType": "LINE",
            "Id": "line-1",
            "Page": 1,
            "Text": "text",
            "Confidence": 99.0,
            "Relationships": [{"Type": "CHILD", "Ids": [marker]}],
        },
    ]


@pytest.mark.parametrize("marker", ["max_blocks", "max_result_bytes"])
def test_document_analysis_dangling_id_naming_a_limit_is_malformed_response(marker: str) -> None:
    client = doc_analysis.FakeTextractClient(pages=[doc_analysis._page(blocks=_blocks_with_provider_text(marker))])

    result = doc_analysis._run(doc_analysis._transform_for_client(client))

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "malformed_response"
    assert result.reason["error_type"] == "result_validation"


@pytest.mark.parametrize("marker", ["max_blocks", "max_result_bytes"])
def test_inline_analysis_dangling_id_naming_a_limit_is_malformed_response(marker: str) -> None:
    response = inline_analysis._analyze_response(blocks=_blocks_with_provider_text(marker))
    client = inline_analysis.FakeInlineClient(result=InlineAnalysisResult(semantic_response=response, sdk_attempts=1))

    result = inline_analysis._run(inline_analysis._transform_for_client(client))

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "malformed_response"


def test_document_analysis_limit_failures_keep_their_categories() -> None:
    client = doc_analysis.FakeTextractClient(pages=[doc_analysis._page(blocks=doc_analysis._basic_blocks())])
    too_many = doc_analysis._run(doc_analysis._transform_for_client(client, max_blocks=1))
    assert too_many.reason is not None and too_many.reason["reason"] == "too_many_blocks"

    client = doc_analysis.FakeTextractClient(pages=[doc_analysis._page(blocks=doc_analysis._basic_blocks())])
    too_large = doc_analysis._run(doc_analysis._transform_for_client(client, max_result_bytes=10))
    assert too_large.reason is not None and too_large.reason["reason"] == "result_too_large"


# --- #127: content-safety composer hint must not invent a default threshold ---


def test_content_safety_hint_does_not_claim_a_default_threshold() -> None:
    assistance = AzureContentSafety.get_agent_assistance()
    assert assistance is not None
    hints = " ".join(assistance.composer_hints)

    assert "Default thresholds" not in hints
    assert "tend to be 4" not in hints
    assert "required" in hints
    assert "no default" in hints
    assert "stricter" in hints


# --- #145: every configured field is validated before the first Bedrock call ---


@pytest.mark.parametrize(
    ("second", "reason"),
    [
        (None, "missing_field"),
        (7, "non_string_field"),
        ("", "invalid_input"),
    ],
)
def test_guardrail_validates_every_field_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
    second: object,
    reason: str,
) -> None:
    transform, context = _started_transform(_shield_config(fields=["first", "second"]))
    calls: list[str] = []

    def apply(_client: BedrockGuardrailsClient, **kwargs: object) -> GuardrailDecision:
        calls.append(str(kwargs["text"]))
        return _decision()

    monkeypatch.setattr(BedrockGuardrailsClient, "apply_guardrail", apply)
    row: dict[str, object] = {"first": "safe"}
    if second is not None:
        row["second"] = second

    result = transform.process(make_pipeline_row(row), context)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == reason
    assert result.reason["field"] == "second"
    assert calls == []


# --- #158: query text admits only the ASCII whitespace the provider pattern admits ---


@pytest.mark.parametrize("whitespace", ["\u00a0", "\u2003", "\u2028", "\u3000"])
@pytest.mark.parametrize("member", ["text", "alias"])
def test_textract_query_rejects_unicode_whitespace(whitespace: str, member: str) -> None:
    values = {"text": "What is the total", member: f"What is{whitespace}the total"}

    with pytest.raises(ValidationError):
        TextractQueryConfig.model_validate(values)


@pytest.mark.parametrize("whitespace", [" ", "\t", "\n", "\r", "\f", "\v"])
def test_textract_query_accepts_ascii_whitespace(whitespace: str) -> None:
    query = TextractQueryConfig.model_validate({"text": f"What is{whitespace}the total"})

    assert query.text == f"What is{whitespace}the total"


# --- #159: guardrail profile alias is bounded at config load like the live check ---


def _profile_values(alias: str) -> dict[str, object]:
    return {
        "alias": alias,
        "plugin": "aws_bedrock_prompt_shield",
        "guardrail_identifier": "abc123guardrail",
        "guardrail_version": "7",
        "region": "us-east-1",
    }


def test_guardrail_profile_alias_over_64_characters_fails_at_config_load() -> None:
    with pytest.raises(ValidationError) as excinfo:
        BedrockGuardrailProfileSettings.model_validate(_profile_values("a" * 65))

    assert any(error["loc"] == ("alias",) for error in excinfo.value.errors())


def test_guardrail_profile_alias_of_64_characters_loads() -> None:
    profile = BedrockGuardrailProfileSettings.model_validate(_profile_values("a" * 64))

    assert profile.alias == "a" * 64
