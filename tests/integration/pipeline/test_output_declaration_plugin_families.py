# tests/integration/pipeline/test_output_declaration_plugin_families.py
"""Each plugin family's own declared types are enforced by the engine's value check (ADR-050).

A shipped plugin declares a concrete type for every field its own code
computes (``created_output_fields()``). A plugin whose code honours its
declaration never violates it, so each case injects the fault into the
plugin's computation and shows the route: the reason names the field, both
type names, ``authorship: computed`` and ``declared_by: plugin`` — never the
value. ``test_output_declaration_batch_seams.py`` covers the reductive
aggregation, the collector and batch_replicate; this file covers the other
families:

- passthrough batch annotation (``batch_outlier_annotator`` under
  ``aggregations:``): the whole batch follows ``on_error``;
- per-row retrieval (``rag_retrieval``, whose core ``azure_ai_search`` shares):
  the row follows the transform's ``on_error``;
- per-row LLM structured output (``llm``): the declaration of each
  ``output_fields`` entry is BOUND to its ``OutputFieldConfig.type`` and the
  Tier-3 parse converts a JSON number into that type, so a benign ``5.0``
  under ``integer`` is delivered as ``5`` and a ``7`` under ``number`` as
  ``7.0``; with the parse's conversion removed (the binding without the
  parse — what the 2026-09-25 ruling forbids) the same row routes as the
  plugin's fault on ``score``. The unfaulted run is the control and records
  the bound types in the node's output contract;
- document extraction (both Textract transforms and Azure Document
  Intelligence): their backends are external services, so the check is
  called directly on an emitted row, at the per-row seam's own entry point.

The CLI cases are real ``elspeth run --execute`` invocations (in-process, so
the fault can be injected). Every token reaches a terminal outcome and no
traceback appears (``_run`` asserts it).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

from elspeth.contracts.errors import DeclaredOutputTypeViolation
from elspeth.contracts.probes import CollectionReadinessResult
from elspeth.contracts.token_usage import TokenUsage
from elspeth.engine.executors.declared_output_types import verify_created_output_types, verify_produced_output_types
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.clients.retrieval.types import RetrievalChunk
from elspeth.plugins.transforms.batch_outlier_annotator import BatchOutlierAnnotator
from elspeth.plugins.transforms.llm import validation as llm_validation
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMProvider, LLMQueryResult
from elspeth.plugins.transforms.llm.transform import LLMTransform
from elspeth.plugins.transforms.rag import core as rag_core
from elspeth.plugins.transforms.rag.formatter import FormattedContext
from elspeth.plugins.transforms.rag.transform import RAGRetrievalTransform
from elspeth.testing import make_pipeline_row
from tests.integration.pipeline.test_output_declaration_routing import (
    _audit_cells_containing,
    _json_sink,
    _json_source,
    _outcomes,
    _read_jsonl,
    _run,
    _transform_error_reasons,
    _write_jsonl,
)
from tests.invariants.test_pass_through_invariants import _registered_transform_classes

SENTINEL = "SENTINEL_S1B_COMPUTED_VALUE"

_REAL_ANNOTATION_FOR = BatchOutlierAnnotator._annotation_for


def _settings_file(tmp_path: Path, body: dict[str, Any], *, source_schema: dict[str, Any] | None = None) -> Path:
    settings: dict[str, Any] = {
        "sources": {"src": _json_source(tmp_path / "in.jsonl", schema=source_schema)},
        "concurrency": {"max_workers": 1},
        **body,
        "sinks": {"out": _json_sink(tmp_path / "out.jsonl"), "quarantine": _json_sink(tmp_path / "q.jsonl")},
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings, sort_keys=False))
    return path


def _assert_plugin_computed_reason(reason: dict[str, Any], *, field: str, expected: str, actual: str) -> None:
    assert reason["reason"] == "contract_violation"
    assert (reason["field"], reason["expected"], reason["actual"], reason["authorship"], reason["declared_by"]) == (
        field,
        expected,
        actual,
        "computed",
        "plugin",
    )
    assert f"declared {expected} by the transform itself" in reason["error"]
    assert SENTINEL not in json.dumps(reason)


def _terminal_outcomes(tmp_path: Path) -> dict[tuple[str, str], int]:
    return {(outcome, path): n for (outcome, path), n in _outcomes(tmp_path).items() if outcome is not None}


def _node_contract(tmp_path: Path, node_prefix: str) -> dict[str, tuple[str, str]]:
    """The recorded output contract of one transform node: ``{field: (python_type, source)}``."""
    con = sqlite3.connect(tmp_path / "audit.db")
    try:
        [(text,)] = con.execute("select output_contract_json from nodes where node_id like ?", (f"{node_prefix}%",)).fetchall()
    finally:
        con.close()
    return {field["normalized_name"]: (field["python_type"], field["source"]) for field in json.loads(text)["fields"]}


# ---------------------------------------------------------------------------
# Passthrough batch annotation
# ---------------------------------------------------------------------------


def _fault_outlier_mean(monkeypatch: pytest.MonkeyPatch) -> None:
    """batch_outlier_annotator computes ``outlier_mean`` (declared ``float``) as the sentinel string."""

    def annotation_for(self: BatchOutlierAnnotator, entry: Any, stats: Any) -> dict[str, object]:
        annotation = _REAL_ANNOTATION_FOR(self, entry, stats)
        annotation[self._field("mean")] = SENTINEL
        return annotation

    monkeypatch.setattr(BatchOutlierAnnotator, "_annotation_for", annotation_for)


def _outlier_aggregation() -> dict[str, Any]:
    return {
        "aggregations": [
            {
                "name": "outliers",
                "plugin": "batch_outlier_annotator",
                "input": "rows",
                "on_success": "out",
                "on_error": "quarantine",
                "trigger": {"count": 3},
                "output_mode": "passthrough",
                "options": {"schema": {"mode": "observed"}, "value_field": "amount"},
            }
        ]
    }


_OUTLIER_ROWS = [{"id": 1, "amount": 2}, {"id": 2, "amount": 3}, {"id": 3, "amount": 50}]


def test_a_passthrough_annotator_computing_the_wrong_type_fails_the_batch_value_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fault_outlier_mean(monkeypatch)
    _write_jsonl(tmp_path / "in.jsonl", _OUTLIER_ROWS)

    result = _run(_settings_file(tmp_path, _outlier_aggregation()))

    assert result.exit_code == 2, result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == []
    assert len(_read_jsonl(tmp_path / "q.jsonl")) == len(_OUTLIER_ROWS)
    assert _terminal_outcomes(tmp_path) == {("failure", "on_error_routed"): len(_OUTLIER_ROWS)}
    reasons = _transform_error_reasons(tmp_path)
    assert len(reasons) == len(_OUTLIER_ROWS)
    for reason in reasons:
        _assert_plugin_computed_reason(reason, field="outlier_mean", expected="float", actual="str")
    assert _audit_cells_containing(tmp_path, SENTINEL) == []


def test_the_same_annotator_over_int_rows_completes_with_float_statistics(tmp_path: Path) -> None:
    """Control: int input rows still yield float statistics, so the shipped declarations route nothing."""
    _write_jsonl(tmp_path / "in.jsonl", _OUTLIER_ROWS)

    result = _run(_settings_file(tmp_path, _outlier_aggregation()))

    assert result.exit_code == 0, result.output
    delivered = _read_jsonl(tmp_path / "out.jsonl")
    assert [row["amount"] for row in delivered] == [2, 3, 50]
    assert _read_jsonl(tmp_path / "q.jsonl") == []
    assert _transform_error_reasons(tmp_path) == []


# ---------------------------------------------------------------------------
# Per-row retrieval
# ---------------------------------------------------------------------------


class _StubSearcher:
    """A retrieval backend that finds one chunk for every query and is always ready."""

    def __init__(self) -> None:
        self.last_skipped_count = 0
        self.last_skipped_reasons: list[dict[str, Any]] = []

    def search(self, query: str, top_k: int, min_score: float, **audit: Any) -> list[RetrievalChunk]:
        del query, top_k, min_score, audit
        return [RetrievalChunk(content="Policy text", score=0.9, source_id="doc-1", metadata={})]

    def check_readiness(self) -> CollectionReadinessResult:
        return CollectionReadinessResult(collection="stub", reachable=True, count=1, message="stub collection")

    def close(self) -> None:
        return None


def _rag_transforms() -> dict[str, Any]:
    return {
        "transforms": [
            {
                "name": "retrieve",
                "plugin": "rag_retrieval",
                "input": "rows",
                "on_success": "out",
                "on_error": "quarantine",
                "options": {
                    "output_prefix": "policy",
                    "query_field": "question",
                    "provider": "chroma",
                    "provider_config": {"collection": "stub", "mode": "ephemeral"},
                    "schema": {"mode": "observed"},
                },
            }
        ]
    }


def test_a_retrieval_transform_computing_the_wrong_type_routes_the_row_value_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """rag_retrieval's ``rag_context`` is declared ``str``; a formatter returning a non-str routes the row."""
    monkeypatch.setattr(RAGRetrievalTransform, "_build_searcher", lambda self, ctx: _StubSearcher())
    monkeypatch.setattr(rag_core, "format_context", lambda *args, **kwargs: FormattedContext(text=[SENTINEL], truncated=False))
    _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "question": "What is the policy?"}])

    result = _run(_settings_file(tmp_path, _rag_transforms()))

    assert result.exit_code == 2, result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == []
    assert len(_read_jsonl(tmp_path / "q.jsonl")) == 1
    assert _terminal_outcomes(tmp_path) == {("failure", "on_error_routed"): 1}
    [reason] = _transform_error_reasons(tmp_path)
    _assert_plugin_computed_reason(reason, field="policy__rag_context", expected="str", actual="list")
    assert reason["emitted_index"] == 0
    assert _audit_cells_containing(tmp_path, SENTINEL) == []


def test_the_same_retrieval_completes_with_the_declared_types_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RAGRetrievalTransform, "_build_searcher", lambda self, ctx: _StubSearcher())
    _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "question": "What is the policy?"}])

    result = _run(_settings_file(tmp_path, _rag_transforms()))

    assert result.exit_code == 0, result.output
    [row] = _read_jsonl(tmp_path / "out.jsonl")
    assert (row["policy__rag_score"], row["policy__rag_count"]) == (0.9, 1)
    recorded = _node_contract(tmp_path, "transform_retrieve")
    assert {name: recorded[name] for name in recorded if name.startswith("policy__")} == {
        "policy__rag_context": ("str", "declared"),
        "policy__rag_score": ("float", "declared"),
        "policy__rag_count": ("int", "declared"),
        "policy__rag_sources": ("str", "declared"),
    }


# ---------------------------------------------------------------------------
# Per-row LLM structured output: bound and parsed together
# ---------------------------------------------------------------------------


_LLM_CONTENT = '{"score": 5.0, "confidence": 7}'


def _install_fake_llm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every LLM call answers ``_LLM_CONTENT``: an integral float under ``integer`` and an int under ``number``."""

    def create_provider(self: LLMTransform, *, llm_call_governance: Any = None) -> LLMProvider:
        del llm_call_governance
        provider = Mock(spec=LLMProvider)
        provider.execute_query.return_value = LLMQueryResult(
            content=_LLM_CONTENT, usage=TokenUsage.known(10, 5), model="fake-model", finish_reason=FinishReason.STOP
        )
        return provider

    monkeypatch.setattr(LLMTransform, "_create_provider", create_provider)


def _llm_transforms() -> dict[str, Any]:
    return {
        "transforms": [
            {
                "name": "judge",
                "plugin": "llm",
                "input": "rows",
                "on_success": "out",
                "on_error": "quarantine",
                "options": {
                    "provider": "openrouter",
                    "api_key": "fake-key-not-checked",
                    "model": "openai/gpt-4o",
                    "prompt_template": "Score {{ row.text }}",
                    "response_field": "judged",
                    "required_input_fields": ["text"],
                    "output_fields": [{"suffix": "score", "type": "integer"}, {"suffix": "confidence", "type": "number"}],
                    "schema": {"mode": "observed"},
                },
            }
        ]
    }


_LLM_SOURCE_SCHEMA = {"mode": "flexible", "fields": ["id: int", "text: str"]}


def test_an_llm_parse_that_stops_converting_routes_the_row_as_the_plugin_s_fault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "s1b-test-fingerprint-key")
    _install_fake_llm_provider(monkeypatch)
    real_parse = llm_validation.parse_field_value

    def parse_without_conversion(value: Any, field_config: Any) -> tuple[Any, str | None]:
        _parsed, error = real_parse(value, field_config)
        return (None, error) if error is not None else (value, None)

    monkeypatch.setattr(llm_validation, "parse_field_value", parse_without_conversion)
    _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "text": "a"}])

    result = _run(_settings_file(tmp_path, _llm_transforms(), source_schema=_LLM_SOURCE_SCHEMA))

    assert result.exit_code == 2, result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == []
    assert _terminal_outcomes(tmp_path) == {("failure", "on_error_routed"): 1}
    [reason] = _transform_error_reasons(tmp_path)
    # Only ``score`` (5.0 under integer) breaks its declaration: the unconverted
    # ``7`` under number is an int, which satisfies a float declaration
    # (ADR-050 Decision 5). The number conversion exists so the delivered value
    # is the bound row type, not because the check would refuse the int.
    _assert_plugin_computed_reason(reason, field="score", expected="int", actual="float")
    assert _LLM_CONTENT not in json.dumps(reason)


def test_the_bound_and_parsed_llm_output_is_delivered_in_its_declared_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "s1b-test-fingerprint-key")
    _install_fake_llm_provider(monkeypatch)
    _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}])

    result = _run(_settings_file(tmp_path, _llm_transforms(), source_schema=_LLM_SOURCE_SCHEMA))

    assert result.exit_code == 0, result.output
    outcomes = _terminal_outcomes(tmp_path)
    assert {outcome for (outcome, _path) in outcomes} == {"success"}
    assert sum(outcomes.values()) == 2
    assert _transform_error_reasons(tmp_path) == []
    recorded = _node_contract(tmp_path, "transform_judge")
    assert {name: recorded[name] for name in ("score", "confidence", "judged", "judged_model", "judged_usage")} == {
        "score": ("int", "declared"),
        "confidence": ("float", "declared"),
        "judged": ("str", "declared"),
        "judged_model": ("str", "declared"),
        "judged_usage": ("object", "declared"),
    }
    # Before the binding the node recorded ``object`` for both structured fields
    # and delivered the provider's spelling (5.0 / 7) unchanged.
    for row in _read_jsonl(tmp_path / "out.jsonl"):
        assert (row["score"], row["confidence"]) == (5, 7.0)


# ---------------------------------------------------------------------------
# Document extraction: the check at the per-row seam's own entry point
# ---------------------------------------------------------------------------


def _probe_transform(plugin_name: str) -> BaseTransform:
    [transform_cls] = [cls for cls in _registered_transform_classes() if cls.name == plugin_name]
    return transform_cls(transform_cls.probe_config())


@pytest.mark.parametrize(
    ("plugin_name", "field", "expected"),
    [
        ("aws_textract_document_analysis", "textract_text", "str"),
        ("aws_textract_inline_analysis", "textract_text", "str"),
        ("azure_document_intelligence", "di_content", "str"),
    ],
)
def test_a_document_extractor_emitting_the_wrong_type_is_the_plugin_s_violation(plugin_name: str, field: str, expected: str) -> None:
    transform = _probe_transform(plugin_name)
    assert transform._stamped_output_field_contracts()[field].python_type is str
    input_row = make_pipeline_row({"document": "ref"})
    emitted = make_pipeline_row({"document": "ref", field: 12345})
    emitted = type(emitted)(emitted.to_dict(), transform._apply_declared_output_field_contracts(emitted.contract))

    with pytest.raises(DeclaredOutputTypeViolation) as excinfo:
        verify_produced_output_types(transform=transform, input_row=input_row, emitted_rows=[emitted])

    reason = excinfo.value.to_transform_error_reason()
    assert (reason["field"], reason["expected"], reason["actual"], reason["authorship"], reason["declared_by"]) == (
        field,
        expected,
        "int",
        "computed",
        "plugin",
    )
    assert "12345" not in json.dumps(reason)


# ---------------------------------------------------------------------------
# Every concrete plugin declaration, from the live registry, is enforced
# ---------------------------------------------------------------------------

# Configurations that switch on created fields the probe config leaves off, so
# every concrete declaration a shipped transform can make is reached. Keyed by
# plugin name; each entry is merged over the plugin's ``probe_config()``.
_DECLARATION_VARIANTS: dict[str, list[dict[str, Any]]] = {
    "batch_stats": [{"value_field": "v", "group_by": "g", "compute_mean": True}],
    "batch_distribution_profile": [{"value_field": "v", "group_by": "g"}],
    "batch_classifier_metrics": [
        {"actual_field": "a", "predicted_field": "p", "positive_label": "yes"},
        {"actual_field": "a", "predicted_field": "p", "positive_label": 1},
        {"actual_field": "a", "predicted_field": "p", "positive_label": True},
    ],
    "batch_drift_compare": [{"cohort_field": "c", "value_field": "v", "value_type": "categorical"}],
    "batch_top_k": [{"field": "v", "group_by": "g"}],
    "llm": [
        {"output_fields": [{"suffix": "score", "type": "integer"}, {"suffix": "ok", "type": "boolean"}, {"suffix": "c", "type": "number"}]},
        {
            "prompt_template": "Evaluate: {{ row.text_content }}",
            "queries": {"quality": {"input_fields": {"text_content": "text"}, "output_fields": [{"suffix": "score", "type": "integer"}]}},
        },
    ],
    "aws_textract_document_analysis": [{"page_count_field": "tx_pages"}],
    "aws_textract_inline_analysis": [{"page_count_field": "tx_pages"}],
    "azure_document_intelligence": [{"page_count_field": "di_pages"}],
}


def _declaration_cases() -> list[tuple[str, dict[str, Any], str]]:
    """(plugin, config, field) for every created field whose type the plugin's code declares concretely."""
    cases: list[tuple[str, dict[str, Any], str]] = []
    for transform_cls in sorted(_registered_transform_classes(), key=lambda cls: cls.name):
        configs = [transform_cls.probe_config()]
        configs += [{**transform_cls.probe_config(), **variant} for variant in _DECLARATION_VARIANTS.get(transform_cls.name, [])]
        for config in configs:
            transform = transform_cls(config)
            created = transform.declared_output_fields | {definition.name for definition in transform.created_output_fields()}
            for name, (contract, declared_by) in sorted(transform._output_field_declarations().items()):
                if declared_by == "plugin" and contract.python_type is not object and name in created:
                    cases.append((transform_cls.name, config, name))
    return cases


_DECLARATION_CASES = _declaration_cases()


def test_the_declaration_roster_reaches_every_swept_family() -> None:
    """The roster is live, not vacuous: each family the sweep typed has cases (the registry decides the rest)."""
    plugins = {plugin for plugin, _config, _field in _DECLARATION_CASES}
    assert {
        "batch_stats",
        "batch_classifier_metrics",
        "batch_outlier_annotator",
        "report_assemble",
        "rag_retrieval",
        "azure_ai_search",
        "aws_textract_document_analysis",
        "aws_textract_inline_analysis",
        "azure_document_intelligence",
        "llm",
    } <= plugins
    assert ("llm", "quality_score") in {(plugin, field) for plugin, _config, field in _DECLARATION_CASES}


@pytest.mark.parametrize(
    ("plugin_name", "config", "field"),
    _DECLARATION_CASES,
    ids=[f"{plugin}-{field}" for plugin, _config, field in _DECLARATION_CASES],
)
def test_every_concrete_plugin_declaration_is_enforced_value_free(plugin_name: str, config: dict[str, Any], field: str) -> None:
    """A value of the wrong type in any plugin-declared created field is the plugin's violation, value-free.

    The check runs at the seam the transform runs behind: the batch flush for
    a batch-aware transform, the per-row seam otherwise. The wrong value is a
    sentinel string, or an int where the declaration is ``str``.
    """
    [transform_cls] = [cls for cls in _registered_transform_classes() if cls.name == plugin_name]
    transform = transform_cls(config)
    declared = transform._stamped_output_field_contracts()[field].python_type
    wrong: object = 12345 if declared is str else SENTINEL
    probe = make_pipeline_row({field: wrong})
    emitted = type(probe)(probe.to_dict(), transform._apply_declared_output_field_contracts(probe.contract))

    with pytest.raises(DeclaredOutputTypeViolation) as excinfo:
        if transform.is_batch_aware:
            verify_created_output_types(transform=transform, emitted_rows=[emitted])
        else:
            verify_produced_output_types(transform=transform, input_row=make_pipeline_row({"unrelated_input": 1}), emitted_rows=[emitted])

    reason = excinfo.value.to_transform_error_reason()
    assert (reason["field"], reason["expected"], reason["actual"], reason["authorship"], reason["declared_by"]) == (
        field,
        declared.__name__,
        type(wrong).__name__,
        "computed",
        "plugin",
    )
    assert str(wrong) not in json.dumps(reason)
