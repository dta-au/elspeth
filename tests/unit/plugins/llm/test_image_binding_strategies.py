# tests/unit/plugins/llm/test_image_binding_strategies.py
"""Task 5: binding resolved image parts into single- and multi-query LLM calls.

Text-only configs must keep the user message content a plain ``str`` (audit
byte-identity pin). With images configured, content becomes
``(TextPart, *ImageParts)`` in spec order then list order, ``parts_hash`` rides
success_reason.metadata as ``{field_prefix}_parts_hash``, and the tracer sees
only bytes-free ``ImagePart.audit_view()`` projections — never raw bytes.

FakePayloadStore mirrors tests/unit/plugins/llm/test_image_inputs.py.
"""

from __future__ import annotations

import hashlib
from types import MappingProxyType, SimpleNamespace
from typing import Any

from elspeth.contracts.chat_parts import ChatMessage, ImagePart, TextPart, parts_hash
from elspeth.contracts.payload_store import PayloadNotFoundError
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.transforms.llm.image_inputs import ImageInputConfig
from elspeth.plugins.transforms.llm.multi_query import OutputFieldConfig, OutputFieldType, QuerySpec, ResponseFormat
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMQueryResult
from elspeth.plugins.transforms.llm.templates import PromptTemplate
from elspeth.plugins.transforms.llm.transform import MultiQueryStrategy, SingleQueryStrategy
from elspeth.testing import make_pipeline_row
from tests.unit.contracts.test_chat_parts import JPEG_BYTES, PNG_BYTES

PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()
JPEG_SHA256 = hashlib.sha256(JPEG_BYTES).hexdigest()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePayloadStore:
    def __init__(self, contents: dict[str, bytes] | None = None) -> None:
        self.contents = {PNG_SHA256: PNG_BYTES, JPEG_SHA256: JPEG_BYTES} if contents is None else contents
        self.retrieve_calls: list[str] = []

    def store(self, content: bytes) -> str:
        raise AssertionError("image binding must never store payloads")

    def retrieve(self, content_hash: str) -> bytes:
        self.retrieve_calls.append(content_hash)
        try:
            return self.contents[content_hash]
        except KeyError:
            raise PayloadNotFoundError(content_hash) from None

    def exists(self, content_hash: str) -> bool:
        return content_hash in self.contents

    def delete(self, content_hash: str) -> bool:
        raise AssertionError("image binding must never delete payloads")


class FakeProvider:
    """Captures every execute_query call's kwargs, in order."""

    def __init__(self, result: LLMQueryResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result or LLMQueryResult(
            content='{"answer": "ok"}',
            usage=TokenUsage.known(10, 5),
            model="gpt-4o",
            finish_reason=FinishReason.STOP,
        )

    def execute_query(
        self,
        messages: Any,
        *,
        model: str,
        temperature: float,
        max_tokens: int | None,
        audit_parent: Any,
        response_format: dict[str, Any] | None = None,
    ) -> LLMQueryResult:
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        return self._result

    def runtime_preflight(self, *, operation_id: str, model: str) -> None:
        raise AssertionError("not exercised in these tests")

    def close(self) -> None:
        pass


class FakeTracer:
    def __init__(self) -> None:
        self.success_calls: list[dict[str, Any]] = []
        self.error_calls: list[dict[str, Any]] = []

    def record_success(self, **kwargs: Any) -> None:
        self.success_calls.append(kwargs)

    def record_error(self, **kwargs: Any) -> None:
        self.error_calls.append(kwargs)

    def flush(self) -> None:
        pass


def _make_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        state_id="state-123",
        run_id="run-123",
        token=SimpleNamespace(token_id="token-1"),
        shutdown_event=None,
    )


def _identity(x: Any) -> Any:
    return x


def _make_single_strategy(
    *,
    image_specs: tuple[ImageInputConfig, ...] = (),
    max_image_bytes: int = 10_000_000,
    max_images_per_call: int = 20,
    prompt_template: str = "Describe: {{ row.text }}",
) -> SingleQueryStrategy:
    return SingleQueryStrategy(
        template=PromptTemplate(prompt_template),
        system_prompt=None,
        system_prompt_source=None,
        model="gpt-4o",
        temperature=0.0,
        max_tokens=100,
        response_field="llm_response",
        align_output_contract=_identity,
        apply_declared_output_field_contracts=_identity,
        image_specs=image_specs,
        max_image_bytes=max_image_bytes,
        max_images_per_call=max_images_per_call,
    )


def _make_multi_strategy(
    *,
    query_specs: list[QuerySpec],
    image_specs: tuple[ImageInputConfig, ...] = (),
    max_image_bytes: int = 10_000_000,
    max_images_per_call: int = 20,
    prompt_template: str = "Process: {{ row.text_content }}",
) -> MultiQueryStrategy:
    return MultiQueryStrategy(
        query_specs=query_specs,
        template=PromptTemplate(prompt_template),
        system_prompt=None,
        system_prompt_source=None,
        model="gpt-4o",
        temperature=0.0,
        max_tokens=100,
        response_field="llm_response",
        align_output_contract=_identity,
        align_output_row_contract=_identity,
        apply_declared_output_field_contracts=_identity,
        image_specs=image_specs,
        max_image_bytes=max_image_bytes,
        max_images_per_call=max_images_per_call,
        executor=None,
    )


def _assert_no_bytes(obj: Any) -> None:
    if isinstance(obj, bytes):
        raise AssertionError(f"bytes leaked into audit/tracer data: {obj!r}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _assert_no_bytes(k)
            _assert_no_bytes(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _assert_no_bytes(v)


# ---------------------------------------------------------------------------
# SingleQueryStrategy
# ---------------------------------------------------------------------------


class TestSingleQueryTextOnly:
    def test_text_only_content_is_plain_str(self) -> None:
        """Regression pin: no image_specs configured -> byte-identical to pre-image tree."""
        strategy = _make_single_strategy()
        provider = FakeProvider()
        row = make_pipeline_row({"text": "hello"})

        result = strategy.execute(row, _make_ctx(), provider=provider, tracer=FakeTracer())

        assert result.status == "success"
        sent = provider.calls[0]["messages"][-1]
        assert isinstance(sent, ChatMessage)
        assert isinstance(sent.content, str)
        assert sent.content == "Describe: hello"

    def test_text_only_success_metadata_has_no_parts_hash(self) -> None:
        strategy = _make_single_strategy()
        provider = FakeProvider()
        row = make_pipeline_row({"text": "hello"})

        result = strategy.execute(row, _make_ctx(), provider=provider, tracer=FakeTracer())

        assert result.success_reason is not None
        assert "llm_response_parts_hash" not in result.success_reason["metadata"]

    def test_text_only_tracer_extra_metadata_is_none(self) -> None:
        strategy = _make_single_strategy()
        provider = FakeProvider()
        tracer = FakeTracer()
        row = make_pipeline_row({"text": "hello"})

        strategy.execute(row, _make_ctx(), provider=provider, tracer=tracer)

        assert tracer.success_calls[0]["extra_metadata"] is None


class TestSingleQueryWithImages:
    def _specs(self) -> tuple[ImageInputConfig, ...]:
        return (
            ImageInputConfig(field="pic1", format="png"),
            ImageInputConfig(field="pic2", format="jpeg"),
        )

    def test_content_is_text_then_images_in_spec_then_list_order(self) -> None:
        strategy = _make_single_strategy(image_specs=self._specs())
        provider = FakeProvider()
        row = make_pipeline_row(
            {
                "text": "hello",
                "pic1": PNG_SHA256,
                "pic2": [JPEG_SHA256, JPEG_SHA256],
            }
        )

        result = strategy.execute(row, _make_ctx(), provider=provider, tracer=FakeTracer(), payload_store=FakePayloadStore())

        assert result.status == "success", result.reason
        sent = provider.calls[0]["messages"][-1]
        assert isinstance(sent.content, tuple)
        assert len(sent.content) == 4
        text_part, img1, img2, img3 = sent.content
        assert isinstance(text_part, TextPart)
        assert text_part.text == "Describe: hello"
        assert isinstance(img1, ImagePart) and img1.format == "png" and img1.sha256 == PNG_SHA256
        assert isinstance(img2, ImagePart) and img2.format == "jpeg" and img2.sha256 == JPEG_SHA256
        assert isinstance(img3, ImagePart) and img3.format == "jpeg" and img3.sha256 == JPEG_SHA256

    def test_resolve_failure_never_calls_provider_and_returns_result_unchanged(self) -> None:
        strategy = _make_single_strategy(image_specs=self._specs())
        provider = FakeProvider()
        row = make_pipeline_row({"text": "hello", "pic2": [JPEG_SHA256]})  # pic1 missing, required

        result = strategy.execute(row, _make_ctx(), provider=provider, tracer=FakeTracer(), payload_store=FakePayloadStore())

        assert provider.calls == []
        assert result.status == "error"
        assert result.reason == {"reason": "missing_field", "field": "pic1"}

    def test_success_metadata_carries_parts_hash_matching_parts_hash_fn(self) -> None:
        strategy = _make_single_strategy(image_specs=self._specs())
        provider = FakeProvider()
        row = make_pipeline_row({"text": "hello", "pic1": PNG_SHA256, "pic2": [JPEG_SHA256]})

        result = strategy.execute(row, _make_ctx(), provider=provider, tracer=FakeTracer(), payload_store=FakePayloadStore())

        assert result.status == "success", result.reason
        content = provider.calls[0]["messages"][-1].content
        expected = parts_hash(content)
        assert result.success_reason is not None
        assert result.success_reason["metadata"]["llm_response_parts_hash"] == expected

    def test_tracer_success_receives_bytes_free_image_parts_metadata(self) -> None:
        strategy = _make_single_strategy(image_specs=self._specs())
        provider = FakeProvider()
        tracer = FakeTracer()
        row = make_pipeline_row({"text": "hello", "pic1": PNG_SHA256, "pic2": [JPEG_SHA256]})

        strategy.execute(row, _make_ctx(), provider=provider, tracer=tracer, payload_store=FakePayloadStore())

        assert len(tracer.success_calls) == 1
        extra = tracer.success_calls[0]["extra_metadata"]
        assert extra is not None
        assert list(extra.keys()) == ["image_parts"]
        assert len(extra["image_parts"]) == 2
        assert extra["image_parts"][0] == {
            "type": "image",
            "format": "png",
            "sha256": PNG_SHA256,
            "byte_count": len(PNG_BYTES),
            "blob_ref": PNG_SHA256,
        }
        _assert_no_bytes(extra)
        _assert_no_bytes(tracer.success_calls[0])

    def test_tracer_error_receives_bytes_free_image_parts_metadata_on_provider_failure(self) -> None:
        from elspeth.plugins.infrastructure.clients.llm import LLMClientError

        class _FailingProvider(FakeProvider):
            def execute_query(self, *args: Any, **kwargs: Any) -> LLMQueryResult:
                self.calls.append(kwargs | {"messages": list(args[0]) if args else kwargs.get("messages")})
                raise LLMClientError("boom", retryable=False)

        strategy = _make_single_strategy(image_specs=self._specs())
        provider = _FailingProvider()
        tracer = FakeTracer()
        row = make_pipeline_row({"text": "hello", "pic1": PNG_SHA256, "pic2": [JPEG_SHA256]})

        result = strategy.execute(row, _make_ctx(), provider=provider, tracer=tracer, payload_store=FakePayloadStore())

        assert result.status == "error"
        assert len(tracer.error_calls) == 1
        extra = tracer.error_calls[0]["extra_metadata"]
        assert extra is not None and extra["image_parts"]
        _assert_no_bytes(tracer.error_calls[0])


# ---------------------------------------------------------------------------
# MultiQueryStrategy
# ---------------------------------------------------------------------------


class TestMultiQueryWithImages:
    def _specs(self) -> tuple[ImageInputConfig, ...]:
        return (ImageInputConfig(field="pic1", format="png"),)

    def test_images_bind_identically_on_each_query_message(self) -> None:
        query_specs = [
            QuerySpec(name="sentiment", input_fields=MappingProxyType({"text_content": "text"})),
            QuerySpec(name="topic", input_fields=MappingProxyType({"text_content": "text"})),
        ]
        strategy = _make_multi_strategy(query_specs=query_specs, image_specs=self._specs())
        provider = FakeProvider(
            LLMQueryResult(content="plain text", usage=TokenUsage.known(10, 5), model="gpt-4o", finish_reason=FinishReason.STOP)
        )
        row = make_pipeline_row({"text": "hello", "pic1": PNG_SHA256})

        result = strategy.execute(row, _make_ctx(), provider=provider, tracer=FakeTracer(), payload_store=FakePayloadStore())

        assert result.status == "success", result.reason
        assert len(provider.calls) == 2
        for call in provider.calls:
            content = call["messages"][-1].content
            assert isinstance(content, tuple)
            assert isinstance(content[0], TextPart)
            assert len(content) == 2
            assert isinstance(content[1], ImagePart)
            assert content[1].sha256 == PNG_SHA256

        metadata = result.success_reason["metadata"] if result.success_reason else {}
        assert metadata["sentiment_llm_response_parts_hash"] == parts_hash(provider.calls[0]["messages"][-1].content)
        assert metadata["topic_llm_response_parts_hash"] == parts_hash(provider.calls[1]["messages"][-1].content)

    def test_structured_response_format_and_images_coexist_on_same_call(self) -> None:
        query_specs = [
            QuerySpec(
                name="extract",
                input_fields=MappingProxyType({"text_content": "text"}),
                response_format=ResponseFormat.STRUCTURED,
                output_fields=(OutputFieldConfig(suffix="answer", type=OutputFieldType.STRING),),
            ),
        ]
        strategy = _make_multi_strategy(query_specs=query_specs, image_specs=self._specs())
        provider = FakeProvider(
            LLMQueryResult(content='{"answer": "ok"}', usage=TokenUsage.known(10, 5), model="gpt-4o", finish_reason=FinishReason.STOP)
        )
        row = make_pipeline_row({"text": "hello", "pic1": PNG_SHA256})

        result = strategy.execute(row, _make_ctx(), provider=provider, tracer=FakeTracer(), payload_store=FakePayloadStore())

        assert result.status == "success", result.reason
        assert len(provider.calls) == 1
        call = provider.calls[0]
        assert call["response_format"] is not None
        assert call["response_format"]["type"] == "json_schema"
        content = call["messages"][-1].content
        assert isinstance(content, tuple)
        assert isinstance(content[0], TextPart)
        assert isinstance(content[1], ImagePart)

    def test_standard_mode_schema_suffix_and_images_travel_in_same_message(self) -> None:
        """Standard mode appends the JSON-contract suffix to provider_prompt
        BEFORE images are attached — schema text and images share one message."""
        query_specs = [
            QuerySpec(
                name="extract",
                input_fields=MappingProxyType({"text_content": "text"}),
                response_format=ResponseFormat.STANDARD,
                output_fields=(OutputFieldConfig(suffix="answer", type=OutputFieldType.STRING),),
            ),
        ]
        strategy = _make_multi_strategy(query_specs=query_specs, image_specs=self._specs())
        provider = FakeProvider(
            LLMQueryResult(content='{"answer": "ok"}', usage=TokenUsage.known(10, 5), model="gpt-4o", finish_reason=FinishReason.STOP)
        )
        row = make_pipeline_row({"text": "hello", "pic1": PNG_SHA256})

        result = strategy.execute(row, _make_ctx(), provider=provider, tracer=FakeTracer(), payload_store=FakePayloadStore())

        assert result.status == "success", result.reason
        content = provider.calls[0]["messages"][-1].content
        assert isinstance(content, tuple)
        text_part = content[0]
        assert isinstance(text_part, TextPart)
        assert "required output contract" in text_part.text
        assert isinstance(content[1], ImagePart)

    def test_resolve_failure_never_calls_provider(self) -> None:
        query_specs = [
            QuerySpec(name="sentiment", input_fields=MappingProxyType({"text_content": "text"})),
        ]
        strategy = _make_multi_strategy(query_specs=query_specs, image_specs=self._specs())
        provider = FakeProvider()
        row = make_pipeline_row({"text": "hello"})  # pic1 missing, required

        result = strategy.execute(row, _make_ctx(), provider=provider, tracer=FakeTracer(), payload_store=FakePayloadStore())

        assert provider.calls == []
        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "missing_field"
        assert result.reason["field"] == "pic1"
