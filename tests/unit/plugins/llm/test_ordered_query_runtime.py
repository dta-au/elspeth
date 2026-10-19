"""Deterministic provider controls for ordered row-local publication."""

import pytest

from elspeth.contracts.errors import DeclaredOutputTypeViolation
from elspeth.plugins.infrastructure.clients.llm import RateLimitError
from elspeth.plugins.transforms.llm.transform import LLMTransform
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context
from tests.unit.plugins.llm.test_azure_multi_query import _make_provider
from tests.unit.plugins.llm.test_ordered_query_contract import ordered_options


def make_transform(prefix: str | None = None) -> LLMTransform:
    options = ordered_options()
    options.update(deployment_name="gpt-4o", endpoint="https://test.openai.azure.com", api_key="test-key")
    options["queries"][0]["response_format"] = "structured"
    options["queries"][0]["structured_output_prefix"] = prefix
    options["queries"][1]["input_fields"]["score"] = "extract_score" if prefix is None else (f"{prefix}_score" if prefix else "score")
    options["queries"][1]["template"] = "score={{ row.score }} original={{ row.source_row.text }}"
    return LLMTransform(options)


def test_normalized_success_is_bound_in_later_prompt_and_original_row_stays_intact() -> None:
    transform = make_transform()
    provider = _make_provider([{"score": 5.0}, "accepted"])
    transform._provider = provider
    row = make_pipeline_row({"text": "entry"})
    result = transform._process_row(row, make_context())
    assert result.status == "success"
    assert result.row["extract_score"] == 5
    assert type(result.row["extract_score"]) is int
    assert provider.execute_query.call_count == 2
    assert provider.execute_query.call_args_list[1].args[0][-1].content == "score=5 original=entry"
    assert row.to_dict() == {"text": "entry"}


@pytest.mark.parametrize("score", [True, 5.5, None])
def test_invalid_structured_intermediate_never_calls_b(score: object) -> None:
    transform = make_transform()
    provider = _make_provider([{"score": score}, "must not run"])
    transform._provider = provider
    row = make_pipeline_row({"text": "entry"})
    result = transform._process_row(row, make_context())
    assert result.status == "error"
    assert provider.execute_query.call_count == 1
    assert row.to_dict() == {"text": "entry"}


def test_declared_value_violation_stops_before_b(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.transforms.llm.transform import MultiQueryStrategy

    transform = make_transform()
    provider = _make_provider([{"score": 5}])
    transform._provider = provider
    execute = MultiQueryStrategy._execute_one_query

    def malformed_output(self, *args, **kwargs):
        result = execute(self, *args, **kwargs)
        assert isinstance(result, MultiQueryStrategy._QuerySuccess)
        return MultiQueryStrategy._QuerySuccess({**result.fields, "extract_score": "value-must-not-leak"}, result.audit_metadata)

    monkeypatch.setattr(MultiQueryStrategy, "_execute_one_query", malformed_output)
    with pytest.raises(DeclaredOutputTypeViolation) as exc:
        transform._process_row(make_pipeline_row({"text": "entry"}), make_context())
    assert provider.execute_query.call_count == 1
    assert "value-must-not-leak" not in str(exc.value)


@pytest.mark.parametrize("prefix", [None, "", "published"])
def test_retry_keeps_b_snapshot_and_does_not_repeat_a(monkeypatch: pytest.MonkeyPatch, prefix: str | None) -> None:
    transform = make_transform(prefix)
    provider = _make_provider([{"score": 5}, "accepted"])
    original_execute = provider.execute_query.side_effect
    attempts: list[str] = []

    def capacity_once(messages, **kwargs):
        attempts.append(messages[-1].content)
        if len(attempts) == 2:
            raise RateLimitError("synthetic capacity")
        return original_execute(messages, **kwargs)

    provider.execute_query.side_effect = capacity_once
    monkeypatch.setattr("elspeth.plugins.transforms.llm.transform.time.sleep", lambda _: None)
    transform._provider = provider
    result = transform._process_row(make_pipeline_row({"text": "entry"}), make_context())
    assert result.status == "success"
    assert len(attempts) == 3
    assert attempts[1] == attempts[2] == "score=5 original=entry"


def test_header_alias_of_generated_output_is_rejected_before_a() -> None:
    from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract

    transform = make_transform()
    provider = _make_provider([{"score": 5}])
    transform._provider = provider
    row = PipelineRow(
        {"text": "entry", "other": "ambiguous"},
        SchemaContract(
            mode="FLEXIBLE",
            fields=(
                FieldContract("text", "text", str, True, "declared"),
                FieldContract("other", "extract_score", str, True, "declared"),
            ),
            locked=True,
        ),
    )
    result = transform._process_row(row, make_context())
    assert result.status == "error"
    assert provider.execute_query.call_count == 0


def test_interleaved_rows_never_share_intermediate_values() -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from elspeth.contracts.token_usage import TokenUsage
    from elspeth.plugins.transforms.llm.provider import FinishReason, LLMQueryResult

    transform = make_transform()
    provider = _make_provider()
    barrier = threading.Barrier(2)
    later_prompts: list[str] = []

    def interleave(messages, **kwargs):
        prompt = messages[-1].content
        if prompt in {"alpha", "beta"}:
            barrier.wait(timeout=5)
            content = '{"score": 1}' if prompt == "alpha" else '{"score": 2}'
        else:
            later_prompts.append(prompt)
            content = "accepted"
        return LLMQueryResult(content=content, usage=TokenUsage.known(1, 1), model="gpt-4o", finish_reason=FinishReason.STOP)

    provider.execute_query.side_effect = interleave
    transform._provider = provider
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(transform._process_row, make_pipeline_row({"text": name}), make_context()) for name in ("alpha", "beta")]
        results = [future.result(timeout=10) for future in futures]
    assert all(result.status == "success" for result in results)
    assert set(later_prompts) == {"score=1 original=alpha", "score=2 original=beta"}


@pytest.mark.parametrize("prefix", [None, "", "published"])
def test_shutdown_after_a_preserves_atomic_failure_and_stops_b(prefix: str | None) -> None:
    import threading

    transform = make_transform(prefix)
    provider = _make_provider([{"score": 5}])
    execute = provider.execute_query.side_effect
    shutdown = threading.Event()

    def shutdown_after_response(messages, **kwargs):
        response = execute(messages, **kwargs)
        shutdown.set()
        return response

    provider.execute_query.side_effect = shutdown_after_response
    transform._provider = provider
    ctx = make_context()
    ctx.shutdown_event = shutdown
    result = transform._process_row(make_pipeline_row({"text": "entry"}), ctx)
    assert result.status == "error"
    assert result.row is None
    assert provider.execute_query.call_count == 1
    assert result.reason["discarded_successful_queries"] == 1


@pytest.mark.parametrize("prefix", [None, "", "published"])
def test_replay_miss_on_b_propagates_without_live_fallback_or_repeating_a(prefix: str | None) -> None:
    from elspeth.plugins.infrastructure.clients.replayer import ReplayMissError

    transform = make_transform(prefix)
    provider = _make_provider([{"score": 5}])
    execute = provider.execute_query.side_effect

    def missing_b(messages, **kwargs):
        if messages[-1].content.startswith("score="):
            raise ReplayMissError("synthetic-missing-b", {"messages": [message.content for message in messages]})
        return execute(messages, **kwargs)

    provider.execute_query.side_effect = missing_b
    transform._provider = provider
    with pytest.raises(ReplayMissError):
        transform._process_row(make_pipeline_row({"text": "entry"}), make_context())
    assert provider.execute_query.call_count == 2
