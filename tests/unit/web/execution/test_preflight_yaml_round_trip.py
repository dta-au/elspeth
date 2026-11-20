"""One runtime preflight parses the materialized configuration once, not three times.

Materialization renders the blob-substituted configuration to YAML; the runtime
phase used to parse that text straight back (``_validation_runtime.py``) and,
on the no-secrets branch, parse it a second time inside
``load_settings_from_yaml_string``. These tests run real web preflights on the
two graph shapes that measured the cost (file-held prompts, and inline prompts
with one file), and pin:

* the configuration and settings the runtime phase now uses equal what the
  deleted dump -> parse path produced (kept below as a test-local reference);
* the number of ``yaml.dump`` / ``yaml.load`` calls one preflight makes.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
import yaml

import tests.integration.pipeline.test_composer_runtime_agreement as agreement
from elspeth.config_loading import load_settings_from_config_dict
from elspeth.contracts.blobs import BlobRecord
from elspeth.core.config import ElspethSettings, load_bounded_pipeline_yaml
from elspeth.web.composer import yaml_generator as composer_yaml_generator
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution import _validation_materialization
from elspeth.web.execution import validation as validation_module
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, prompt_review_anchor_hash_from_options
from tests.unit.web.execution.test_compiled_id_stability import _llm_profile_policy_context

_SESSION_ID = UUID(agreement._AGREEMENT_SESSION_ID)
_PROMPT_CHARS = 15_600


def _prompt(node: int, query: int) -> str:
    head = f"Step {node:02d} query {query:02d}. Assess {{{{ row.text }}}} against criterion {query}: yes, no, on, ~, 1e3.\n"
    body = "Consider the stated evidence against the rubric; answer: yes or no.  \n" * (_PROMPT_CHARS // 70 + 2)
    return (head + body)[:_PROMPT_CHARS] + "\n\n"


@dataclass
class _Graph:
    state: CompositionState
    blobs: dict[UUID, tuple[BlobRecord, bytes]] = field(default_factory=dict)


def _graph(tmp_path: Path, *, llm_nodes: int, queries: int, file_prompts: int) -> _Graph:
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
        options: dict[str, Any] = {
            "profile": "llm-default",
            "queries": {
                f"s{node:02d}q{query:02d}": {"input_fields": {"text": "text"}, "template": bind(_prompt(node, query))}
                for query in range(queries)
            },
            "required_input_fields": [],
            "schema": {"mode": "observed"},
        }
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


@dataclass
class _Capture:
    rendered: list[tuple[object, Any]] = field(default_factory=list)
    settings_inputs: list[object] = field(default_factory=list)
    settings: list[ElspethSettings] = field(default_factory=list)


def _capture(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    """Record what materialization renders and what the settings load receives."""
    # Looked up at call time so this file collects on a tree without the module.
    module_name = "elspeth.web.execution._rendered_pipeline_yaml"
    assert importlib.util.find_spec(module_name) is not None, f"{module_name} is missing"
    rendered_type = importlib.import_module(module_name).RenderedPipelineYaml
    captured = _Capture()

    def render(document: object) -> Any:
        rendered = rendered_type.render(document)
        # The reference parse needs the document as rendered, before anything
        # downstream could touch it: take the bounded load of its own text now.
        captured.rendered.append((load_bounded_pipeline_yaml(rendered.text), rendered))
        return rendered

    def load_settings(config_dict: Any, *, expand_env_vars: bool = False) -> ElspethSettings:
        captured.settings_inputs.append(config_dict)
        settings = load_settings_from_config_dict(config_dict, expand_env_vars=expand_env_vars)
        captured.settings.append(settings)
        return settings

    monkeypatch.setattr(
        _validation_materialization,
        "RenderedPipelineYaml",
        SimpleNamespace(render=render, of_text=rendered_type.of_text),
    )
    monkeypatch.setattr(validation_module, "load_settings_from_config_dict", load_settings)
    return captured


@pytest.mark.parametrize("shape", list(_SHAPES))
def test_runtime_settings_equal_the_deleted_dump_then_parse_path(shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _capture(monkeypatch)
    graph = _graph(tmp_path, **_SHAPES[shape])

    result = _web_preflight(tmp_path, graph)

    assert result.is_valid is True, [error.message for error in result.errors]
    assert len(capture.rendered) == 1, "the blob path renders the substituted configuration once"
    reference_config, rendered = capture.rendered[0]
    # The old path's input to the settings load: a parse of the rendered text.
    assert _identical(rendered.load(), reference_config)
    assert len(capture.settings_inputs) == 1
    assert _identical(capture.settings_inputs[0], reference_config)
    assert capture.settings[0] == load_settings_from_config_dict(cast(dict[str, object], reference_config), expand_env_vars=False)
    # The prompts arrive in the settings exactly as authored, trailing blank lines included.
    sources_and_nodes = cast(dict[str, Any], reference_config)
    first_query = sources_and_nodes["transforms"][0]["options"]["queries"]["s00q00"]["template"]
    assert first_query == _prompt(0, 0)


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
def test_one_web_preflight_makes_at_most_five_dumps_and_four_loads(shape: str, tmp_path: Path, yaml_calls: _YamlCalls) -> None:
    """Pin the counts after the redundant parses left (base: 5 dumps, 6 loads).

    Dumps: generate + path-resolve, for the runtime and the compiled-identity
    documents (4), plus the substituted configuration (1). Loads: the
    path-resolve parse for each document (2), the blob-marker scan (1) and the
    identity document (1). The materialized configuration itself is never
    parsed back.
    """
    graph = _graph(tmp_path, **_SHAPES[shape])

    result = _web_preflight(tmp_path, graph)

    assert result.is_valid is True, [error.message for error in result.errors]
    counts = (len(yaml_calls.dumps), len(yaml_calls.loads))
    assert counts[0] <= 5, counts
    assert counts[1] <= 4, counts
    # Control: the counter saw the work it must see — the path-resolve parse
    # of the generated text happens on every preflight.
    assert counts[0] >= 1 and counts[1] >= 1, counts
    # The third dump is the substituted configuration; no load reads it back.
    rendered_configuration = yaml_calls.dumps[2]
    assert "blob_ref" not in rendered_configuration
    assert all(loaded is not rendered_configuration for loaded in yaml_calls.loads), counts
