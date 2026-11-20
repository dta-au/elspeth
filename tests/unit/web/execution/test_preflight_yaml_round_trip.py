"""One runtime preflight never renders and parses back the file-held text it resolved.

Materialization substitutes file-held text into the configuration. The runtime
phase used to render that configuration to YAML, parse it straight back
(``_validation_runtime.py``) and, on the no-secrets branch, parse it a second
time inside ``load_settings_from_yaml_string``; the 2 MiB text limit of that
parse made the size a graph could hold depend on the language of its prompts,
because ``yaml.dump`` escapes non-ASCII text. These tests run real web
preflights and pin:

* the configuration and settings the runtime phase now uses equal what the
  deleted dump -> parse path produced (kept below as a test-local reference);
* a graph holding the whole file-text cap validates in English, Chinese and
  Cyrillic, at today's cap and at the ruled 4 MiB cap;
* the resolved configuration's own bound refuses with a code, total and limit;
* the number of ``yaml.dump`` / ``yaml.load`` calls one preflight makes, and
  that no file-held prompt text passes through either.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import math
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
import yaml

import tests.integration.pipeline.test_composer_runtime_agreement as agreement
from elspeth.config_loading import load_settings_from_config_dict
from elspeth.contracts.blobs import BlobRecord
from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP
from elspeth.core.config import MAX_IN_MEMORY_PIPELINE_YAML_BYTES, ElspethSettings, load_bounded_pipeline_yaml
from elspeth.web.composer import yaml_generator as composer_yaml_generator
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution import _validation_materialization
from elspeth.web.execution import validation as validation_module
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    model_choice_artifact_hash,
    prompt_review_anchor_hash_from_options,
)
from tests.unit.web.execution.test_compiled_id_stability import _llm_profile_policy_context

_SESSION_ID = UUID(agreement._AGREEMENT_SESSION_ID)
_PROMPT_CHARS = 15_600


def _prompt(node: int, query: int) -> str:
    head = f"Step {node:02d} query {query:02d}. Assess {{{{ row.text }}}} against criterion {query}: yes, no, on, ~, 1e3.\n"
    body = "Consider the stated evidence against the rubric; answer: yes or no.  \n" * (_PROMPT_CHARS // 70 + 2)
    return (head + body)[:_PROMPT_CHARS] + "\n\n"


_BODIES = {
    "en": "Consider the stated evidence against the rubric; answer: yes or no.  \n",
    "zh": "请根据评分标准仔细考虑所述证据\uff0c并回答是或否。注意记录中的每一个细节。\n",
    "ru": "Внимательно рассмотрите представленные доказательства по критериям и ответьте да или нет.\n",
}


def _sized_prompt(node: int, query: int, *, size: int, language: str) -> str:
    """A prompt of exactly ``size`` UTF-8 bytes: an ASCII head, then text in ``language``."""
    text = f"Step {node:02d} query {query:02d}. Assess {{{{ row.text }}}} against criterion {query}.\n"
    while len(text.encode("utf-8")) < size:
        text += _BODIES[language]
    while len(text.encode("utf-8")) > size:
        text = text[:-1]
    return text + "x" * (size - len(text.encode("utf-8")))


@dataclass
class _Graph:
    state: CompositionState
    blobs: dict[UUID, tuple[BlobRecord, bytes]] = field(default_factory=dict)


def _graph(
    tmp_path: Path,
    *,
    llm_nodes: int,
    queries: int,
    file_prompts: int,
    prompt: Callable[[int, int], str] = _prompt,
    trained_operator: bool = False,
) -> _Graph:
    blobs_dir = tmp_path / "blobs" / str(_SESSION_ID)
    outputs_dir = tmp_path / "outputs" / str(_SESSION_ID)
    blobs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    (blobs_dir / "input.csv").write_text("text\nhello\n", encoding="utf-8")
    graph = _Graph(state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1))

    def bind(text: str) -> object:
        """Hold the first ``file_prompts`` prompts in files; the rest stay inline."""
        index = len(graph.blobs)
        if index >= file_prompts:
            return text
        content = text.encode("utf-8")
        sha256 = hashlib.sha256(content).hexdigest()
        blob_id = UUID(int=index + 1)
        record = agreement._ready_inline_blob_record(blob_id=blob_id, session_id=_SESSION_ID, content=content, content_hash=sha256)
        graph.blobs[blob_id] = (record, content)
        return {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": sha256}

    state = graph.state.with_source(
        SourceSpec(
            plugin="csv",
            on_success="c000",
            on_validation_failure="discard",
            options={"path": str(blobs_dir / "input.csv"), "schema": {"mode": "observed"}},
        )
    )
    for node in range(llm_nodes):
        queries_option = {
            f"s{node:02d}q{query:02d}": {"input_fields": {"text": "text"}, "template": bind(prompt(node, query))}
            for query in range(queries)
        }
        options: dict[str, Any] = (
            {
                # The trained-operator route wires a credential as a secret
                # marker, which validation without a secret service redacts.
                "provider": "openrouter",
                "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                "model": "openai/gpt-4o",
                "max_capacity_retry_seconds": 30,
                "queries": queries_option,
                "required_input_fields": [],
                "schema": {"mode": "observed"},
            }
            if trained_operator
            else {"profile": "llm-default", "queries": queries_option, "required_input_fields": [], "schema": {"mode": "observed"}}
        )
        anchor = prompt_review_anchor_hash_from_options(options)
        options[INTERPRETATION_REQUIREMENTS_KEY] = [
            {
                "id": f"llm_prompt_template:n{node:02d}",
                "kind": "llm_prompt_template",
                "user_term": f"llm_prompt_template:n{node:02d}",
                "status": "resolved",
                "draft": "surface",
                "event_id": f"evt-n{node:02d}",
                "accepted_value": "surface",
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": anchor,
            }
        ]
        if trained_operator:
            options[INTERPRETATION_REQUIREMENTS_KEY].append(
                {
                    "id": f"llm_model_choice:n{node:02d}",
                    "kind": "llm_model_choice",
                    "user_term": f"llm_model_choice:n{node:02d}",
                    "status": "resolved",
                    "draft": "openai/gpt-4o",
                    "event_id": f"evt-mc{node:02d}",
                    "accepted_value": "openai/gpt-4o",
                    "accepted_artifact_hash": None,
                    "resolved_prompt_template_hash": model_choice_artifact_hash("openai/gpt-4o"),
                }
            )
        state = state.with_node(
            NodeSpec(
                id=f"n{node:02d}",
                node_type="transform",
                plugin="llm",
                input=f"c{node:03d}",
                on_success="results" if node == llm_nodes - 1 else f"c{node + 1:03d}",
                on_error="discard",
                options=options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
    graph.state = state.with_output(
        OutputSpec(
            name="results",
            plugin="json",
            options={"path": str(outputs_dir / "results.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
            on_write_failure="discard",
        )
    )
    assert len(graph.blobs) == file_prompts
    return graph


def _web_preflight(tmp_path: Path, graph: _Graph) -> ValidationResult:
    profiles, snapshot = _llm_profile_policy_context(
        llm_profile={"provider": "bedrock", "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0", "region_name": "us-east-1"}
    )
    blobs = graph.blobs
    return validation_module.validate_pipeline(
        graph.state,
        cast(Any, SimpleNamespace(data_dir=tmp_path)),
        composer_yaml_generator,
        plugin_snapshot=snapshot,
        profile_registry=profiles,
        catalog=create_catalog_service(),
        blob_get_metadata=lambda blob_id: blobs[blob_id][0] if blob_id in blobs else None,
        blob_get_content=lambda blob_id: blobs[blob_id],
        session_id=str(_SESSION_ID),
    )


_SHAPES = {
    # 32 file-held prompts of 15.6 KB: the materialized config is ~600 KB.
    "32-file-prompts": {"llm_nodes": 8, "queries": 4, "file_prompts": 32},
    # 31 inline prompts and one file: every rendered text is ~600 KB.
    "31-inline-1-file": {"llm_nodes": 8, "queries": 4, "file_prompts": 1},
}


def _identical(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict and type(right) is dict:
        return list(left) == list(right) and all(_identical(left[key], right[key]) for key in left)
    if type(left) is list and type(right) is list:
        return len(left) == len(right) and all(_identical(a, b) for a, b in zip(left, right, strict=True))
    if type(left) is float and type(right) is float and (math.isnan(left) or math.isnan(right)):
        return math.isnan(left) and math.isnan(right)
    return left == right


def _rendered_module() -> ModuleType:
    # Looked up at call time so this file collects on a tree without the module.
    module_name = "elspeth.web.execution._rendered_pipeline_yaml"
    assert importlib.util.find_spec(module_name) is not None, f"{module_name} is missing"
    return importlib.import_module(module_name)


def _reference_round_trip(document: object) -> tuple[str, object]:
    """The deleted path: render the resolved configuration, then parse it back under the 2 MiB limit."""
    try:
        return ("value", load_bounded_pipeline_yaml(yaml.dump(document, default_flow_style=False)))
    except ValueError as exc:
        return ("refused", str(exc))


@dataclass
class _Capture:
    resolved: list[tuple[tuple[str, object], Any]] = field(default_factory=list)
    settings_inputs: list[object] = field(default_factory=list)
    settings: list[ElspethSettings] = field(default_factory=list)


def _capture(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    """Record what materialization resolves and what the settings load receives."""
    pipeline_type = _rendered_module().RenderedPipelineYaml
    captured = _Capture()

    def of_resolved(document: object) -> Any:
        # The reference needs the document as resolved, before anything
        # downstream could touch it: run the deleted round trip on it now.
        reference = _reference_round_trip(document)
        held = pipeline_type.of_resolved(document)
        captured.resolved.append((reference, held))
        return held

    def load_settings(config_dict: Any, *, expand_env_vars: bool = False) -> ElspethSettings:
        captured.settings_inputs.append(config_dict)
        settings = load_settings_from_config_dict(config_dict, expand_env_vars=expand_env_vars)
        captured.settings.append(settings)
        return settings

    monkeypatch.setattr(
        _validation_materialization,
        "RenderedPipelineYaml",
        SimpleNamespace(of_resolved=of_resolved, of_text=pipeline_type.of_text),
    )
    monkeypatch.setattr(validation_module, "load_settings_from_config_dict", load_settings)
    return captured


@pytest.mark.parametrize("shape", list(_SHAPES))
def test_runtime_settings_equal_the_deleted_dump_then_parse_path(shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _capture(monkeypatch)
    graph = _graph(tmp_path, **_SHAPES[shape])

    result = _web_preflight(tmp_path, graph)

    assert result.is_valid is True, [error.message for error in result.errors]
    assert len(capture.resolved) == 1, "the blob path resolves the configuration once"
    (outcome, reference_config), held = capture.resolved[0]
    assert outcome == "value", reference_config
    # The old path's input to the settings load: a parse of the rendered text.
    assert _identical(held.load(), reference_config)
    assert len(capture.settings_inputs) == 1
    assert _identical(capture.settings_inputs[0], reference_config)
    assert capture.settings[0] == load_settings_from_config_dict(cast(dict[str, object], reference_config), expand_env_vars=False)
    # The prompts arrive in the settings exactly as authored, trailing blank lines included.
    sources_and_nodes = cast(dict[str, Any], reference_config)
    first_query = sources_and_nodes["transforms"][0]["options"]["queries"]["s00q00"]["template"]
    assert first_query == _prompt(0, 0)


# ── the resolved configuration's bound does not depend on prompt language ──

_LANGUAGES = ["en", "zh", "ru"]
# The cap before it was raised. At this size the deleted path accepted English
# text and refused the same bytes of Chinese or Russian, which is what the
# first test below compares against; at the raised cap it refused all three.
_PREVIOUS_CAP = 1024 * 1024
_RULED_CAP = 4 * 1024 * 1024


def _cap_graph(tmp_path: Path, *, total_bytes: int, files: int, language: str) -> _Graph:
    """``files`` file-held prompts holding exactly ``total_bytes`` of text, eight queries per node."""
    assert total_bytes % files == 0 and files % 8 == 0
    size = total_bytes // files

    def prompt(node: int, query: int) -> str:
        return _sized_prompt(node, query, size=size, language=language)

    graph = _graph(tmp_path, llm_nodes=files // 8, queries=8, file_prompts=files, prompt=prompt)
    assert sum(len(content) for _record, content in graph.blobs.values()) == total_bytes
    return graph


@pytest.mark.parametrize("language", _LANGUAGES)
def test_a_graph_holding_the_whole_file_text_cap_validates_in_any_language(
    language: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the previous 1 MiB cap: the same bytes of text validate whatever language they are in."""
    capture = _capture(monkeypatch)
    graph = _cap_graph(tmp_path, total_bytes=_PREVIOUS_CAP, files=128, language=language)

    result = _web_preflight(tmp_path, graph)

    assert result.is_valid is True, [error.message for error in result.errors]
    (outcome, reference), held = capture.resolved[0]
    if outcome == "value":
        assert _identical(held.load(), reference)
    else:
        # The deleted path judged the escaped rendering: non-ASCII text
        # renders 2.3 (Chinese) to 3.3 (Cyrillic) bytes per byte and was refused.
        assert language != "en", reference
        assert reference == f"YAML content exceeds the {MAX_IN_MEMORY_PIPELINE_YAML_BYTES} byte YAML limit"
    assert (language == "en") == (outcome == "value")


@pytest.mark.parametrize("language", ["en", "zh"])
def test_the_ruled_4_mib_file_text_cap_validates(language: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At the ruled cap, with the resolved bound derived from it as the module derives it.

    Until the cap constant changes, the cap and the derived bound are set to
    their ruled values here; once it has changed, both settings are no-ops.
    """
    cap = max(BLOB_INLINE_AGGREGATE_BYTE_CAP, _RULED_CAP)
    monkeypatch.setattr(_validation_materialization, "BLOB_INLINE_AGGREGATE_BYTE_CAP", cap)
    monkeypatch.setattr(_rendered_module(), "MAX_RESOLVED_PIPELINE_TEXT_BYTES", 4 * cap)
    graph = _cap_graph(tmp_path, total_bytes=_RULED_CAP, files=256, language=language)

    result = _web_preflight(tmp_path, graph)

    assert result.is_valid is True, [error.message for error in result.errors]


def _bound_refusal(result: ValidationResult) -> tuple[int, int] | None:
    codes = [error.error_code for error in result.errors]
    if "resolved_pipeline_config_oversized" not in codes:
        return None
    assert codes == ["resolved_pipeline_config_oversized"], codes
    match = re.fullmatch(
        r"The resolved pipeline configuration holds (\d+) bytes of text; the limit is (\d+) bytes\.", result.errors[0].message
    )
    assert match is not None, result.errors[0].message
    failed = [check for check in result.checks if not check.passed]
    assert failed[0].name == "blob_inline_refs", failed
    assert failed[0].detail == result.errors[0].message
    # Every later check is reported as skipped after that failure, not run.
    assert all(check.outcome_code == "validation.skipped_after_failure" for check in failed[1:]), failed
    return int(match.group(1)), int(match.group(2))


def test_the_resolved_bound_refuses_one_byte_over_with_a_code_and_admits_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = _graph(tmp_path, **_SHAPES["32-file-prompts"])
    file_text = sum(len(content) for _record, content in graph.blobs.values())
    module = _rendered_module()

    monkeypatch.setattr(module, "MAX_RESOLVED_PIPELINE_TEXT_BYTES", file_text)
    refused = _bound_refusal(_web_preflight(tmp_path, graph))
    assert refused is not None
    total, limit = refused
    assert limit == file_text
    # The total counts the whole resolved configuration's text, the file text included.
    assert total > file_text

    monkeypatch.setattr(module, "MAX_RESOLVED_PIPELINE_TEXT_BYTES", total - 1)
    assert _bound_refusal(_web_preflight(tmp_path, graph)) == (total, total - 1)

    monkeypatch.setattr(module, "MAX_RESOLVED_PIPELINE_TEXT_BYTES", total)
    at_limit = _web_preflight(tmp_path, graph)
    assert at_limit.is_valid is True, [error.message for error in at_limit.errors]


def test_a_preflight_without_a_secret_service_redacts_a_marker_in_a_resolved_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The secret gate reads the resolved document, not a rendering of it.

    The trained-operator route validates without a secret service and wires
    each LLM credential as a ``{secret_ref: NAME}`` marker; unredacted, a
    marker fails settings as a non-string credential.
    """
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-only-fingerprint-key")
    capture = _capture(monkeypatch)
    graph = _graph(tmp_path, llm_nodes=2, queries=2, file_prompts=4, trained_operator=True)
    blobs = graph.blobs

    result = validation_module.validate_pipeline_for_trained_operator(
        graph.state,
        cast(Any, SimpleNamespace(data_dir=tmp_path)),
        composer_yaml_generator,
        blob_get_metadata=lambda blob_id: blobs[blob_id][0] if blob_id in blobs else None,
        blob_get_content=lambda blob_id: blobs[blob_id],
        session_id=str(_SESSION_ID),
    )

    assert result.is_valid is True, [error.message for error in result.errors]
    (outcome, reference), held = capture.resolved[0]
    assert outcome == "value"
    assert held.mentions("secret_ref") is True
    assert "secret_ref" in yaml.dump(reference, default_flow_style=False)
    api_key = capture.settings_inputs[0]["transforms"][0]["options"]["api_key"]
    assert type(api_key) is str and api_key != "", api_key


def test_the_resolved_bound_is_four_times_the_file_text_cap() -> None:
    assert _rendered_module().MAX_RESOLVED_PIPELINE_TEXT_BYTES == 4 * BLOB_INLINE_AGGREGATE_BYTE_CAP


@dataclass
class _YamlCalls:
    dumps: list[str] = field(default_factory=list)
    loads: list[str] = field(default_factory=list)


@pytest.fixture
def yaml_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[_YamlCalls]:
    """Count every ``yaml.dump`` / ``yaml.load``; each call site looks them up on the module."""
    calls = _YamlCalls()
    real_dump: Callable[..., Any] = yaml.dump
    real_load: Callable[..., Any] = yaml.load

    def dump(*args: Any, **kwargs: Any) -> Any:
        text = real_dump(*args, **kwargs)
        calls.dumps.append(text)
        return text

    def load(stream: Any, **kwargs: Any) -> Any:
        calls.loads.append(stream)
        return real_load(stream, **kwargs)

    monkeypatch.setattr(yaml, "dump", dump)
    monkeypatch.setattr(yaml, "load", load)
    yield calls


def test_the_call_counter_sees_a_known_number_of_calls(yaml_calls: _YamlCalls) -> None:
    load_bounded_pipeline_yaml("a: 1\n")
    yaml.dump({"a": 1})
    yaml.dump({"b": 2})
    assert (len(yaml_calls.dumps), len(yaml_calls.loads)) == (2, 1)


@pytest.mark.parametrize("shape", list(_SHAPES))
def test_one_web_preflight_makes_four_dumps_and_four_loads_none_of_them_file_text(
    shape: str, tmp_path: Path, yaml_calls: _YamlCalls
) -> None:
    """Pin the counts after the round trip left (base: 5 dumps, 6 loads).

    Dumps: generate + path-resolve, for the runtime and the compiled-identity
    documents. Loads: the path-resolve parse for each document, the blob-marker
    scan and the identity document. The resolved configuration is neither
    rendered nor parsed, so the text of a file-held prompt never passes
    through YAML.
    """
    graph = _graph(tmp_path, **_SHAPES[shape])

    result = _web_preflight(tmp_path, graph)

    assert result.is_valid is True, [error.message for error in result.errors]
    counts = (len(yaml_calls.dumps), len(yaml_calls.loads))
    assert counts == (4, 4), counts
    file_held = "Step 00 query 00."
    inline = "Step 00 query 01."
    assert not any(file_held in text for text in yaml_calls.dumps), "a file-held prompt was rendered"
    assert not any(file_held in text for text in yaml_calls.loads), "a file-held prompt was parsed"
    # Control: the instrument sees prompt text when YAML does carry it. In the
    # inline shape the second prompt is inline, so the generated text holds it.
    if shape == "31-inline-1-file":
        assert any(inline in text for text in yaml_calls.dumps)
        assert any(inline in text for text in yaml_calls.loads)
    else:
        assert not any(inline in text for text in yaml_calls.dumps)
