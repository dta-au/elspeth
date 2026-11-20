"""A file-held ``file_inject`` entry must not zero the node's authoring guarantees.

``FileInjectEntryConfig`` types ``content`` as ``str``. In the composer that
position only ever holds an ``inline_content`` marker for a file the user
uploaded, so the resolver-free construction probe failed on it with a
string-type error, the probe was classified as deferred, and a deferred probe
answers "participates, guarantees nothing": every consumer declaring fields
downstream was refused with ``Producer (file_inject) guarantees: [(none)]``,
fields arriving from upstream included, although the all-inline twin of the
same graph is valid. ``prepare_validation_probe_options`` now stands
probe-only content in for a valid marker wherever the node's output contract
does not depend on the file's bytes.

The bytes do decide one fact: a ``json`` entry holding a JSON null makes an
undeclared written field nullable. That field is typed ``any``, and no
authoring verdict separates a nullable ``any`` from a non-null one, so the
entry is stood in all the same. ``test_PIN_undeclared_json_nullability_is_
unobservable_at_authoring`` holds that premise across the consumer shapes
and goes red the day it stops holding. A node still keeps every marker, and
defers, when one entry's own options or the schema block are malformed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.schema import SchemaConfig
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.plugins.infrastructure.preflight import plugin_preflight_mode
from elspeth.web.composer import yaml_generator
from elspeth.web.composer._validation_probe import prepare_validation_probe_options
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec, ValidationSummary
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.routes._helpers import _runtime_preflight_for_state
from tests.unit.web.composer import test_file_inject_upload_only_tool_parity as upload_tools
from tests.unit.web.execution.test_validate_blob_inline import _ready_blob_record, _state_with_reference_join

# Reuse the session/blob fixture the blob-tool suite already owns.
blob_env = upload_tools.blob_env

# The probe's stand-in text, restated so a change to it is a deliberate edit here.
PROBE_FILE_PLACEHOLDER = "validation-probe-file-placeholder"

SESSION_ID = UUID("7d0c4b1e-2f3a-4c5d-8e6f-1a2b3c4d5e6f")
RUBRIC_BLOB = UUID("3a1f0c52-6b7d-4e8f-9a0b-1c2d3e4f5a6b")
PROMPT_BLOB = UUID("4b2e1d63-7c8e-4f90-8b1c-2d3e4f5a6b7c")
SYSTEM_BLOB = UUID("5c3f2e74-8d9f-4a01-9c2d-3e4f5a6b7c8d")
QUERY_BLOB = UUID("6d4a3f85-9eaf-4b12-8d3e-4f5a6b7c8d9e")
ENTRY_BLOB = UUID("7e5b4a96-afb0-4c23-9e4f-5a6b7c8d9eaf")
SECOND_ENTRY_BLOB = UUID("8f6c5ba7-b0c1-4d34-8f5a-6b7c8d9eafb0")

SOURCE_SCHEMA: dict[str, Any] = {
    "mode": "fixed",
    "fields": ["gadget_id: str", "gadget_kind: str", "remark_one: str", "remark_two: str"],
}
RUBRIC_TEXT = "Grade each gadget from 1 to 5."
PROMPT_TEXT = "Using {{ row.rubric_text }}, assess: {{ row.remark_one }}"
SYSTEM_TEXT = "You assess gadgets."
QUERY_TEXT = "Rate {{ row.c }}"


def _marker(blob_id: UUID, text: str) -> dict[str, str]:
    return {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _codes(summary: ValidationSummary) -> tuple[list[str | None], list[str | None]]:
    return [entry.error_code for entry in summary.errors], [entry.error_code for entry in summary.warnings if entry.error_code]


def _verdict(summary: ValidationSummary) -> dict[str, Any]:
    return {
        "is_valid": summary.is_valid,
        "errors": [(entry.component, entry.error_code, entry.message) for entry in summary.errors],
        "warnings": [(entry.component, entry.error_code) for entry in summary.warnings],
    }


def _transform(node_id: str, plugin: str, *, input: str, on_success: str, options: dict[str, Any]) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin=plugin,
        input=input,
        on_success=on_success,
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


# ---------------------------------------------------------------------------
# The customer's shape, with invented names
# ---------------------------------------------------------------------------


def _model_review(node_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": f"model_choice_review:{node_id}",
            "kind": "llm_model_choice",
            "user_term": f"llm_model_choice:{node_id}",
            "status": "resolved",
            "draft": "openai/gpt-4o",
            "event_id": "model-choice-accepted",
            "accepted_value": "openai/gpt-4o",
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": stable_hash("openai/gpt-4o"),
        }
    ]


def _llm_base(node_id: str) -> dict[str, Any]:
    return {
        "provider": "openrouter",
        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
        "model": "openai/gpt-4o",
        "schema": {"mode": "observed"},
        INTERPRETATION_REQUIREMENTS_KEY: _model_review(node_id),
    }


def _chain_source(tmp_path: Path, name: str) -> SourceSpec:
    path = tmp_path / "blobs" / str(SESSION_ID) / f"{name}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("gadget_id,gadget_kind,remark_one,remark_two\ng1,round,fine,ok\n", encoding="utf-8")
    return SourceSpec(
        plugin="csv", on_success="inject_in", options={"path": str(path), "schema": SOURCE_SCHEMA}, on_validation_failure="discard"
    )


def _chain(tmp_path: Path, *, inject_file: bool, prompt_files: bool, query_file: bool) -> CompositionState:
    """csv -> file_inject (mode all, text) -> llm single prompt -> llm multi-query -> field_mapper -> json sink declaring fields."""
    (tmp_path / "outputs" / str(SESSION_ID)).mkdir(parents=True, exist_ok=True)
    inject = _transform(
        "inject_rubric",
        "file_inject",
        input="inject_in",
        on_success="assess_in",
        options={
            "mode": "all",
            "files": {
                "rubric": {
                    "content": _marker(RUBRIC_BLOB, RUBRIC_TEXT) if inject_file else RUBRIC_TEXT,
                    "format": "text",
                    "output_field": "rubric_text",
                }
            },
            "schema": {"mode": "observed"},
        },
    )
    assess = _transform(
        "assess_gadget",
        "llm",
        input="assess_in",
        on_success="rate_in",
        options={
            **_llm_base("assess_gadget"),
            "prompt_template": _marker(PROMPT_BLOB, PROMPT_TEXT) if prompt_files else PROMPT_TEXT,
            "system_prompt": _marker(SYSTEM_BLOB, SYSTEM_TEXT) if prompt_files else SYSTEM_TEXT,
            "required_input_fields": ["rubric_text", "remark_one"],
            "response_field": "assessment",
        },
    )
    rate = _transform(
        "rate_gadget",
        "llm",
        input="rate_in",
        on_success="map_in",
        options={
            **_llm_base("rate_gadget"),
            "system_prompt": SYSTEM_TEXT,
            "required_input_fields": ["remark_two"],
            "max_capacity_retry_seconds": 30,
            "queries": [
                {
                    "name": "q1",
                    "input_fields": {"c": "remark_two"},
                    "output_fields": [{"suffix": "score", "type": "integer"}],
                    "template": _marker(QUERY_BLOB, QUERY_TEXT) if query_file else QUERY_TEXT,
                }
            ],
        },
    )
    mapper = _transform(
        "final_map",
        "field_mapper",
        input="map_in",
        on_success="results",
        options={"mapping": {"gadget_id": "gadget"}, "schema": {"mode": "observed"}},
    )
    sink = OutputSpec(
        name="results",
        plugin="json",
        options={
            "path": str(tmp_path / "outputs" / str(SESSION_ID) / "results.jsonl"),
            "format": "jsonl",
            "schema": {"mode": "flexible", "fields": ["gadget: str", "gadget_kind: str", "assessment: str", "q1_score: int"]},
        },
        on_write_failure="discard",
    )
    return CompositionState(
        source=_chain_source(tmp_path, "first"),
        nodes=(inject, assess, rate, mapper),
        edges=(),
        outputs=(sink,),
        metadata=PipelineMetadata(),
        version=1,
    )


def _swap_source(tmp_path: Path, state: CompositionState) -> CompositionState:
    """The swap as the bind tool writes it: a new path, the same declared schema."""
    return replace(state, sources={"source": _chain_source(tmp_path, "second")}, version=state.version + 1)


_CHAIN_VARIANTS = {
    "file_inject_file_only": {"inject_file": True, "prompt_files": False, "query_file": False},
    "all_files": {"inject_file": True, "prompt_files": True, "query_file": True},
}


@pytest.mark.parametrize("variant", sorted(_CHAIN_VARIANTS))
def test_file_held_chain_validates_like_its_all_inline_twin(tmp_path: Path, variant: str) -> None:
    inline = _chain(tmp_path / "twin", inject_file=False, prompt_files=False, query_file=False).validate()
    assert _codes(inline) == ([], []), "control: the all-inline twin is valid"

    summary = _chain(tmp_path / "file", **_CHAIN_VARIANTS[variant]).validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _codes(summary) == _codes(inline) == ([], [])


@pytest.mark.parametrize("variant", sorted([*_CHAIN_VARIANTS, "all_inline"]))
def test_a_source_swap_changes_nothing(tmp_path: Path, variant: str) -> None:
    flags = _CHAIN_VARIANTS[variant] if variant in _CHAIN_VARIANTS else {"inject_file": False, "prompt_files": False, "query_file": False}
    state = _chain(tmp_path, **flags)

    before = _verdict(state.validate())
    after = _verdict(_swap_source(tmp_path, state).validate())

    assert before == after
    assert before["is_valid"], before["errors"]


def test_file_held_chain_names_the_true_set_when_a_field_is_really_missing(tmp_path: Path) -> None:
    """Negative control: a field nobody writes is still refused, and upstream fields are no longer lost."""
    state = _chain(tmp_path, inject_file=True, prompt_files=True, query_file=True)
    sink = state.outputs[0]
    fields = [*sink.options["schema"]["fields"], "never_written: str"]
    state = replace(state, outputs=(replace(sink, options={**sink.options, "schema": {"mode": "flexible", "fields": fields}}),))
    inline = _chain(tmp_path / "twin", inject_file=False, prompt_files=False, query_file=False)
    inline = replace(
        inline,
        outputs=(replace(inline.outputs[0], options={**inline.outputs[0].options, "schema": {"mode": "flexible", "fields": fields}}),),
    )

    summary = state.validate()

    assert not summary.is_valid
    assert _codes(summary) == _codes(inline.validate()) == (["sink_contract_violation"], [])
    [error] = summary.errors
    assert "Missing fields: [never_written]" in error.message
    assert "(none)" not in error.message


def _record_for(blob_id: UUID, text: str) -> BlobRecord:
    raw = text.encode()
    return replace(
        _ready_blob_record(session_id=SESSION_ID, blob_id=blob_id, size_bytes=len(raw)),
        content_hash=hashlib.sha256(raw).hexdigest(),
    )


def _execution_validate(tmp_path: Path, state: CompositionState, texts: dict[UUID, str]) -> ValidationResult:
    """Execution validation with each file's real bytes behind its marker."""
    records = {blob_id: _record_for(blob_id, text) for blob_id, text in texts.items()}
    return validate_pipeline_for_trained_operator(
        state,
        SimpleNamespace(data_dir=tmp_path),
        yaml_generator,
        blob_get_metadata=lambda blob_id: records[blob_id] if blob_id in records else None,
        blob_get_content=lambda blob_id: (records[blob_id], texts[blob_id].encode()),
        session_id=str(SESSION_ID),
    )


def _with_resolved_query_review(state: CompositionState) -> CompositionState:
    """Resolve the multi-query node's prompt-surface review, as the review card would."""
    from elspeth.web.interpretation_state import prompt_review_anchor_hash_from_options, prompt_review_draft_from_options

    node = next(node for node in state.nodes if node.id == "rate_gadget")
    options = deep_thaw(node.options)
    requirements = options.pop(INTERPRETATION_REQUIREMENTS_KEY)
    anchor = prompt_review_anchor_hash_from_options(options)
    draft = prompt_review_draft_from_options(options)
    assert anchor is not None and draft is not None
    review = {
        "id": "prompt_template_review:rate_gadget",
        "kind": "llm_prompt_template",
        "user_term": "llm_prompt_template:rate_gadget",
        "status": "resolved",
        "draft": draft,
        "event_id": "evt-prompt",
        "accepted_value": draft,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": anchor,
    }
    options[INTERPRETATION_REQUIREMENTS_KEY] = [*requirements, review]
    return replace(state, nodes=tuple(replace(n, options=options) if n.id == "rate_gadget" else n for n in state.nodes))


def test_execution_validation_admits_the_file_held_chain_with_its_real_bytes(tmp_path: Path) -> None:
    """The authoring verdict is checked against the execution verdict on the materialized bytes.

    The multi-query template stays inline here: a reviewed prompt surface is
    what run admission needs from that node, and the review is staged the way
    the card resolves it. Every other file of the customer's shape is held.
    """
    state = _with_resolved_query_review(_chain(tmp_path, inject_file=True, prompt_files=True, query_file=False))

    result = _execution_validate(tmp_path, state, {RUBRIC_BLOB: RUBRIC_TEXT, PROMPT_BLOB: PROMPT_TEXT, SYSTEM_BLOB: SYSTEM_TEXT})

    assert state.validate().is_valid
    assert result.is_valid, [error.message for error in result.errors]
    assert all(check.passed for check in result.checks), [check.name for check in result.checks if not check.passed]


# ---------------------------------------------------------------------------
# Every format, both modes: the probe builds and guarantees what the bytes would
# ---------------------------------------------------------------------------


def _all(entry: dict[str, Any], *, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"mode": "all", "files": {"guide": {**entry, "output_field": "guide_value"}}, "schema": schema or {"mode": "observed"}}


def _select(entry: dict[str, Any], *, schema: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "mode": "select",
        "key_field": "kind",
        "output_field": "guide_value",
        "files": {"alpha": dict(entry)},
        "schema": schema or {"mode": "observed"},
        **extra,
    }


def _declared(field: dict[str, Any]) -> dict[str, Any]:
    return {"mode": "flexible", "fields": [{"name": "guide_value", "required": True, **field}]}


# (id, options builder taking the content value, real file text)
_BUILDS = [
    pytest.param(lambda c: _all({"content": c, "format": "text"}), "Plain guidance.\n", id="text-all"),
    pytest.param(lambda c: _select({"content": c, "format": "text"}), "Plain guidance.\n", id="text-select"),
    pytest.param(lambda c: _all({"content": c, "format": "csv", "delimiter": ";"}), "code;label\nA1;first\n", id="csv-all-delimiter"),
    pytest.param(lambda c: _select({"content": c, "format": "csv"}), "code,label\nA1,first\n", id="csv-select"),
    pytest.param(
        lambda c: _select({"content": c, "format": "csv", "delimiter": "p"}), "codepLabel\nA1pfirst\n", id="csv-select-letter-delimiter"
    ),
    pytest.param(lambda c: _all({"content": c, "format": "jsonl"}), '{"a": 1}\n{"a": null}\n', id="jsonl-all"),
    pytest.param(lambda c: _select({"content": c, "format": "jsonl"}, on_miss="null"), '"x"\n', id="jsonl-select-on-miss-null"),
    pytest.param(
        lambda c: _all({"content": c, "format": "json"}, schema=_declared({"type": "any", "nullable": False})),
        '{"a": 1}',
        id="json-all-declared-any",
    ),
    pytest.param(
        lambda c: _all({"content": c, "format": "json", "data_key": "score"}, schema=_declared({"type": "int", "nullable": False})),
        '{"score": 3, "other": "x"}',
        id="json-all-data-key-declared-int",
    ),
    pytest.param(
        lambda c: _all({"content": c, "format": "json"}, schema=_declared({"type": "float", "nullable": True})),
        "null",
        id="json-all-declared-nullable-float-holding-null",
    ),
    pytest.param(
        lambda c: _all({"content": c, "format": "json"}, schema=_declared({"type": "bool", "nullable": False})),
        "true",
        id="json-all-declared-bool",
    ),
    pytest.param(
        lambda c: _all({"content": c, "format": "json"}, schema=_declared({"type": "str", "nullable": False})),
        '"a sentence"',
        id="json-all-declared-str",
    ),
    pytest.param(
        lambda c: _select({"content": c, "format": "json"}, schema=_declared({"type": "any", "nullable": False})),
        "[1, 2]",
        id="json-select-declared",
    ),
    pytest.param(lambda c: _select({"content": c, "format": "json"}, on_miss="null"), "[1, 2]", id="json-select-on-miss-null"),
    pytest.param(lambda c: _all({"content": c, "format": "json"}), '{"a": 1}', id="json-all-undeclared"),
    pytest.param(lambda c: _all({"content": c, "format": "json", "data_key": "k"}), '{"k": [1]}', id="json-all-data-key-undeclared"),
    pytest.param(lambda c: _select({"content": c, "format": "json"}), '"x"', id="json-select-on-miss-unset-undeclared"),
    pytest.param(
        lambda c: _select({"content": c, "format": "json"}, on_miss="default", default_content="fallback"),
        "3",
        id="json-select-on-miss-default-undeclared",
    ),
    pytest.param(
        lambda c: _all({"content": c, "format": "json"}, schema={"mode": "flexible", "fields": ["unrelated: str"]}),
        "true",
        id="json-all-declares-another-field",
    ),
    pytest.param(
        lambda c: _select({"content": c, "format": "json", "data_key": "k"}, on_miss="null"),
        '{"k": null}',
        id="json-select-data-key-on-miss-null-holding-null",
    ),
]


def _probe_output_schema(options: dict[str, Any]) -> SchemaConfig:
    with plugin_preflight_mode(True):
        transform = get_shared_plugin_manager().create_transform("file_inject", options)
    try:
        return transform._output_schema_config
    finally:
        transform.close()


@pytest.mark.parametrize(("build", "real_text"), _BUILDS)
def test_marker_probe_builds_and_guarantees_what_the_inline_twin_does(build: Any, real_text: str) -> None:
    marked = build(_marker(ENTRY_BLOB, real_text))
    inline = build(real_text)

    prepared = prepare_validation_probe_options(marked, plugin="file_inject")
    probed = _probe_output_schema(prepared)
    real = _probe_output_schema(inline)

    assert probed.mode == real.mode
    assert probed.guaranteed_fields == real.guaranteed_fields
    assert probed.fields == real.fields, "names, types and nullability all match the bytes"
    assert "guide_value" in (probed.guaranteed_fields or ())
    assert build(_marker(ENTRY_BLOB, real_text)) == marked, "the caller's options are not written"


# ---------------------------------------------------------------------------
# What still keeps the marker: an entry whose own options are malformed
# ---------------------------------------------------------------------------

_MALFORMED_ENTRIES = [
    pytest.param({"format": "json", "data_key": 5}, id="json-data-key-not-a-string"),
    pytest.param({"format": "csv", "delimiter": 5}, id="csv-delimiter-not-a-string"),
    pytest.param({}, id="format-missing"),
]


@pytest.mark.parametrize("malformed", _MALFORMED_ENTRIES)
def test_an_entry_with_malformed_options_keeps_every_marker_and_fails_like_its_twin(tmp_path: Path, malformed: dict[str, Any]) -> None:
    options = {
        "mode": "all",
        "files": {
            "notes": {"content": _marker(ENTRY_BLOB, "Plain guidance."), "format": "text", "output_field": "notes_text"},
            "table": {"content": _marker(SECOND_ENTRY_BLOB, "[1]"), "output_field": "guide_value", **malformed},
        },
        "schema": {"mode": "observed"},
    }
    inline = deep_thaw(options)
    inline["files"]["notes"]["content"] = "Plain guidance."
    inline["files"]["table"]["content"] = "[1]"

    prepared = prepare_validation_probe_options(options, plugin="file_inject")
    summary = _inject_state(tmp_path / "file", options).validate()

    assert prepared["files"] == options["files"], "no partial substitution: the well-formed text entry keeps its marker too"
    assert not summary.is_valid
    assert _codes(summary) == _codes(_inject_state(tmp_path / "inline", inline).validate())
    assert "contract_probe_failed" in _codes(summary)[1]
    assert "contract_probe_deferred" not in _codes(summary)[1]


def _inject_state(tmp_path: Path, options: dict[str, Any], *, sink_schema: dict[str, Any] | None = None) -> CompositionState:
    """csv(kind, item) -> file_inject -> json sink."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "input.csv"
    path.write_text("kind,item\nalpha,one\n", encoding="utf-8")
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": str(path), "schema": {"mode": "flexible", "fields": ["kind: str", "item: str"]}},
            on_validation_failure="discard",
        ),
        nodes=(_transform("inject", "file_inject", input="rows", on_success="results", options=options),),
        edges=(),
        outputs=(
            OutputSpec(
                name="results",
                plugin="json",
                options={
                    "path": str(tmp_path / "results.jsonl"),
                    "format": "jsonl",
                    "schema": sink_schema or {"mode": "flexible", "fields": ["kind: str", "item: str", "guide_value: any"]},
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


@pytest.mark.parametrize("file_text", ['{"a": 1}', "null"])
def test_undeclared_json_entry_validates_like_its_inline_twin(tmp_path: Path, file_text: str) -> None:
    """Whatever the file holds, including a JSON null, authoring reaches the same verdict as the twin."""
    summary = _inject_state(tmp_path / "file", _all({"content": _marker(ENTRY_BLOB, file_text), "format": "json"})).validate()
    twin = _inject_state(tmp_path / "inline", _all({"content": file_text, "format": "json"})).validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _verdict(summary) == _verdict(twin)
    assert _codes(summary) == ([], [])


@pytest.mark.parametrize(
    ("fmt", "file_text", "field_type"),
    [("json", '{"a": 1}', "any"), ("json", "7", "int"), ("text", "Plain guidance.", "str"), ("csv", "a,b\n1,2\n", "any")],
)
def test_a_node_declaring_its_own_written_field_validates_including_its_inbound_edge(
    tmp_path: Path, fmt: str, file_text: str, field_type: str
) -> None:
    """Base refused the inbound edge: the deferred probe fell back to the raw declaration as a requirement."""
    schema = {
        "mode": "flexible",
        "fields": ["kind: str", "item: str", {"name": "guide_value", "type": field_type, "required": True, "nullable": False}],
    }
    file_held = _inject_state(tmp_path / "file", _all({"content": _marker(ENTRY_BLOB, file_text), "format": fmt}, schema=schema))
    inline = _inject_state(tmp_path / "inline", _all({"content": file_text, "format": fmt}, schema=schema))

    summary = file_held.validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert not any("Consumer (file_inject) requires fields" in entry.message for entry in summary.errors)
    assert _codes(summary) == _codes(inline.validate()) == ([], [])


@pytest.mark.parametrize("field_type", ["str", "any"])
def test_a_json_entry_declared_outside_the_number_and_bool_types_constructs_with_the_string_stand_in(
    tmp_path: Path, field_type: str
) -> None:
    """``SUPPORTED_TYPES`` is ``{str, int, float, bool, any}``; the stand-in table covers int, float and bool.

    For a declared ``str`` or ``any`` the stand-in stays the JSON string
    placeholder, which both types admit, so the node constructs with its
    declared written field and the probe neither defers nor fails.
    """
    schema = {
        "mode": "flexible",
        "fields": ["kind: str", "item: str", {"name": "guide_value", "type": field_type, "required": True, "nullable": False}],
    }
    options = _all({"content": _marker(ENTRY_BLOB, '"Plain guidance."'), "format": "json"}, schema=schema)

    prepared = prepare_validation_probe_options(options, plugin="file_inject")
    summary = _inject_state(tmp_path, options, sink_schema={"mode": "flexible", "fields": [f"guide_value: {field_type}"]}).validate()

    assert json.loads(prepared["files"]["guide"]["content"]) == PROBE_FILE_PLACEHOLDER
    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _codes(summary) == ([], [])


@pytest.mark.parametrize("field_type", ["list", "dict", "object", "array", "json", "list[str]"])
def test_a_container_declared_for_the_written_field_is_a_config_error_with_or_without_the_file(tmp_path: Path, field_type: str) -> None:
    """The schema language has no container type: ``SchemaConfig`` refuses it, so no stand-in is ever asked for one.

    The file-held node and its inline twin get the same config error; the
    marker changes nothing about it.
    """
    schema = {"mode": "flexible", "fields": ["kind: str", "item: str", f"guide_value: {field_type}"]}
    file_held = _inject_state(tmp_path / "file", _all({"content": _marker(ENTRY_BLOB, "[1, 2]"), "format": "json"}, schema=schema))
    inline = _inject_state(tmp_path / "inline", _all({"content": "[1, 2]", "format": "json"}, schema=schema))

    with pytest.raises(ValueError, match=f"Unknown type '{re.escape(field_type)}'"):
        SchemaConfig.from_dict(schema)
    summary = file_held.validate()

    assert not summary.is_valid
    assert "contract_config_invalid" in _codes(summary)[0]
    assert _codes(summary) == _codes(inline.validate())


def test_several_bytes_independent_entries_are_all_substituted(tmp_path: Path) -> None:
    options = {
        "mode": "all",
        "files": {
            "notes": {"content": _marker(ENTRY_BLOB, "Plain guidance."), "format": "text", "output_field": "notes_text"},
            "table": {"content": _marker(SECOND_ENTRY_BLOB, "a,b\n1,2\n"), "format": "csv", "output_field": "guide_value"},
        },
        "schema": {"mode": "observed"},
    }

    summary = _inject_state(tmp_path, options).validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _codes(summary) == ([], [])


@pytest.mark.parametrize(
    "malformed",
    [
        {"blob_ref": str(ENTRY_BLOB), "mode": "inline_content", "sha256": "not-a-hash"},
        {"blob_ref": "not-a-uuid", "mode": "inline_content", "sha256": "a" * 64},
        {"blob_ref": str(ENTRY_BLOB), "mode": "inline_content", "sha256": "a" * 64, "extra": "key"},
        {"blob_ref": str(ENTRY_BLOB), "mode": "bind_source"},
    ],
)
def test_malformed_marker_is_not_substituted_and_stays_a_probe_failure(tmp_path: Path, malformed: dict[str, str]) -> None:
    options = _all({"content": malformed, "format": "text"})

    prepared = prepare_validation_probe_options(options, plugin="file_inject")
    summary = _inject_state(tmp_path, options).validate()
    errors, warnings = _codes(summary)

    assert prepared["files"]["guide"]["content"] == malformed
    assert not summary.is_valid
    assert "sink_contract_violation" in errors
    assert "contract_probe_failed" in warnings
    assert "contract_probe_deferred" not in warnings


@pytest.mark.parametrize(
    "schema", [{"fields": ["guide_value: any"]}, {"mode": "flexible", "fields": ["guide_value: nonsense"]}, "observed"]
)
def test_an_unparseable_schema_substitutes_nothing_and_is_still_refused(tmp_path: Path, schema: object) -> None:
    """The config model refuses the block itself; the probe guesses no declaration from it."""
    options = _all({"content": _marker(ENTRY_BLOB, '{"a": 1}'), "format": "json"})
    options["schema"] = schema
    text_options = _all({"content": _marker(ENTRY_BLOB, "Plain guidance."), "format": "text"})
    text_options["schema"] = schema

    prepared = prepare_validation_probe_options(options, plugin="file_inject")
    text_prepared = prepare_validation_probe_options(text_options, plugin="file_inject")
    errors, warnings = _codes(_inject_state(tmp_path, options).validate())

    assert prepared["files"] == options["files"]
    assert text_prepared["files"] == text_options["files"]
    assert "contract_config_invalid" in errors
    assert "contract_probe_deferred" not in warnings


@pytest.mark.parametrize(
    ("extra", "bad_entry"),
    [
        pytest.param({"on_miss": "sometimes"}, {}, id="bad-on-miss"),
        pytest.param({}, {"format": "yaml"}, id="bad-format"),
        pytest.param({}, {"data_key": "k"}, id="data-key-on-text"),
        pytest.param({"bogus_option": 1}, {}, id="unknown-option"),
    ],
)
def test_a_valid_marker_beside_a_genuinely_bad_option_is_still_an_error(
    tmp_path: Path, extra: dict[str, Any], bad_entry: dict[str, Any]
) -> None:
    options = _select({"content": _marker(ENTRY_BLOB, "Plain guidance."), "format": "text", **bad_entry}, **extra)

    summary = _inject_state(tmp_path, options).validate()
    warnings = _codes(summary)[1]

    assert not summary.is_valid
    assert "contract_probe_failed" in warnings
    assert "contract_probe_deferred" not in warnings


def test_reference_join_with_a_marker_still_defers_and_warns(tmp_path: Path) -> None:
    """``reference_join`` reads its joined values from the table bytes: no file_inject stand-in reaches it, so it still defers.

    Since stage 2a a deferral is not a refusal: behind this observed source
    the join abstains, the sink's requirement is decided before the run
    starts (``test_deferred_contract_notice.py``), and the notice is the only
    thing authoring says.
    """
    state = _state_with_reference_join(
        tmp_path, session_id=SESSION_ID, reference_format="csv", content=_marker(ENTRY_BLOB, "sku,description\n")
    )
    state = replace(
        state,
        outputs=(
            replace(
                state.outputs[0],
                options={**state.outputs[0].options, "schema": {"mode": "observed", "required_fields": ["description"]}},
            ),
        ),
    )

    prepared = prepare_validation_probe_options(state.nodes[0].options, plugin="reference_join")
    summary = state.validate()
    errors, warnings = _codes(summary)

    assert PROBE_FILE_PLACEHOLDER not in json.dumps(prepared, default=str)
    assert prepared["reference_content"] == _marker(ENTRY_BLOB, "sku,description\n")
    assert warnings == ["contract_probe_deferred"]
    assert errors == []


@pytest.mark.parametrize("plugin", ["reference_join", "llm", "field_mapper", None])
def test_no_other_plugin_receives_file_inject_stand_ins(plugin: str | None) -> None:
    options = {"mode": "all", "files": {"guide": {"content": _marker(ENTRY_BLOB, "x"), "format": "text", "output_field": "guide_value"}}}

    prepared = prepare_validation_probe_options(options, plugin=plugin)

    assert prepared["files"] == options["files"]


# ---------------------------------------------------------------------------
# Narrow types and nullability: execution validation owns every content check
# ---------------------------------------------------------------------------


def _inject_execution_state(tmp_path: Path, options: dict[str, Any], *, sink_schema: dict[str, Any] | None = None) -> CompositionState:
    blobs = tmp_path / "blobs" / str(SESSION_ID)
    blobs.mkdir(parents=True, exist_ok=True)
    (tmp_path / "outputs" / str(SESSION_ID)).mkdir(parents=True, exist_ok=True)
    state = _inject_state(blobs, options, sink_schema=sink_schema)
    output = state.outputs[0]
    return replace(
        state,
        outputs=(replace(output, options={**output.options, "path": str(tmp_path / "outputs" / str(SESSION_ID) / "results.jsonl")}),),
    )


@pytest.mark.parametrize(
    ("declared", "file_text", "refusal"),
    [
        pytest.param({"type": "int", "nullable": False}, '"three"', "schema declares 'guide_value' as int", id="int-declared-string-held"),
        pytest.param(
            {"type": "int", "nullable": False}, "null", "schema declares 'guide_value' as never null", id="non-null-declared-null-held"
        ),
        pytest.param({"type": "any", "nullable": False}, "{not json", "not valid JSON", id="malformed-json"),
    ],
)
def test_execution_validation_refuses_bytes_the_declaration_rules_out(
    tmp_path: Path, declared: dict[str, Any], file_text: str, refusal: str
) -> None:
    options = _all({"content": _marker(ENTRY_BLOB, file_text), "format": "json"}, schema=_declared(declared))
    state = _inject_execution_state(tmp_path, options, sink_schema={"mode": "flexible", "fields": ["guide_value: any"]})

    authoring = state.validate()
    result = _execution_validate(tmp_path, state, {ENTRY_BLOB: file_text})

    assert _codes(authoring) == ([], []), "the probe proves nothing about the bytes: it neither refuses nor defers"
    assert not result.is_valid
    checks = {check.name: check.passed for check in result.checks}
    assert checks["blob_inline_refs"] is True, "the bytes were read; the refusal is about what they hold"
    assert checks["plugin_instantiation"] is False
    assert any(refusal in error.message for error in result.errors), [error.message for error in result.errors]


def test_execution_validation_admits_bytes_the_declaration_allows(tmp_path: Path) -> None:
    """Positive control for the refusals above: only the file's bytes differ."""
    options = _all({"content": _marker(ENTRY_BLOB, "3"), "format": "json"}, schema=_declared({"type": "int", "nullable": False}))
    state = _inject_execution_state(tmp_path, options, sink_schema={"mode": "flexible", "fields": ["guide_value: int"]})

    result = _execution_validate(tmp_path, state, {ENTRY_BLOB: "3"})

    assert state.validate().is_valid
    assert result.is_valid, [error.message for error in result.errors]


@pytest.mark.parametrize(
    ("fmt", "file_text", "declared_type"),
    [
        ("text", "3", "int"),
        ("csv", "a,b\n1,2\n", "int"),
        ("csv", "a,b\n1,2\n", "str"),
        ("jsonl", "1\n2\n", "int"),
        ("jsonl", '"x"\n', "str"),
    ],
)
def test_a_concrete_type_over_a_format_that_can_never_hold_it_is_still_refused(
    tmp_path: Path, fmt: str, file_text: str, declared_type: str
) -> None:
    """Text is always a string and csv/jsonl always a list: this refusal holds under any content, so authoring makes it."""
    schema = _declared({"type": declared_type, "nullable": False})
    file_held = _inject_state(tmp_path / "file", _all({"content": _marker(ENTRY_BLOB, file_text), "format": fmt}, schema=schema)).validate()
    twin = _inject_state(tmp_path / "inline", _all({"content": file_text, "format": fmt}, schema=schema)).validate()

    assert not file_held.is_valid
    assert _codes(file_held) == _codes(twin)
    assert "contract_probe_failed" in _codes(file_held)[1]
    assert "contract_probe_deferred" not in _codes(file_held)[1]


# ---------------------------------------------------------------------------
# Undeclared json: the one bytes-dependent fact is unobservable at authoring
# ---------------------------------------------------------------------------


def _row_union_state(tmp_path: Path, inject_options: dict[str, Any], other_fields: list[Any]) -> CompositionState:
    """csv -> fork gate -> {file_inject arm, passthrough arm} -> row_union -> passthrough -> json sink."""
    path = tmp_path / "input.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("item\none\n", encoding="utf-8")
    gate = NodeSpec(
        id="fork_rows",
        node_type="gate",
        plugin=None,
        input="fork_in",
        on_success=None,
        on_error=None,
        options={},
        condition="True",
        routes={"true": "fork", "false": "fork"},
        fork_to=("inject_branch", "other_branch"),
        branches=None,
        policy=None,
        merge=None,
    )
    inject = _transform("inject", "file_inject", input="inject_branch", on_success="inject_done", options=inject_options)
    other = _transform(
        "other", "passthrough", input="other_branch", on_success="other_done", options={"schema": {"mode": "fixed", "fields": other_fields}}
    )
    union = NodeSpec(
        id="arm_union",
        node_type="row_union",
        plugin=None,
        input="inject_done",
        on_success="union_out",
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches={"inject_branch": "inject_done", "other_branch": "other_done"},
        policy=None,
        merge=None,
    )
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="fork_in",
            options={"path": str(path), "schema": {"mode": "flexible", "fields": ["item: str"]}},
            on_validation_failure="discard",
        ),
        nodes=(
            gate,
            inject,
            other,
            union,
            _transform("after_union", "passthrough", input="union_out", on_success="results", options={"schema": {"mode": "observed"}}),
        ),
        edges=(),
        outputs=(OutputSpec(name="results", plugin="json", options={"schema": {"mode": "observed"}}, on_write_failure="discard"),),
        metadata=PipelineMetadata(),
        version=1,
    )


_OTHER_ARM = ["item: str", {"name": "guide_value", "type": "str", "required": True, "nullable": False}]


def _row_union_codes(summary: ValidationSummary) -> list[str | None]:
    return [entry.error_code for entry in summary.errors if entry.component == "node:arm_union"]


def _pin_inject_options(content: object) -> dict[str, Any]:
    return _all({"content": content, "format": "json"})


def _pin_shape_required_field_sink(tmp_path: Path, content: object) -> CompositionState:
    return _inject_state(tmp_path, _pin_inject_options(content))


def _pin_shape_typed_sink(tmp_path: Path, content: object) -> CompositionState:
    sink = {
        "mode": "flexible",
        "fields": ["kind: str", "item: str", {"name": "guide_value", "type": "str", "required": True, "nullable": False}],
    }
    return _inject_state(tmp_path, _pin_inject_options(content), sink_schema=sink)


def _pin_shape_pass_through_chain(tmp_path: Path, content: object) -> CompositionState:
    state = _inject_state(tmp_path, _pin_inject_options(content))
    inject = replace(state.nodes[0], on_success="relay_in")
    relay = _transform("relay", "passthrough", input="relay_in", on_success="results", options={"schema": {"mode": "observed"}})
    return replace(state, nodes=(inject, relay))


def _pin_shape_row_union(other_type: str, other_nullable: bool, *, item_type: str = "str") -> Any:
    def build(tmp_path: Path, content: object) -> CompositionState:
        other = [f"item: {item_type}", {"name": "guide_value", "type": other_type, "required": True, "nullable": other_nullable}]
        options = _all({"content": content, "format": "json"}, schema={"mode": "fixed", "fields": ["item: str"]})
        return _row_union_state(tmp_path, options, other)

    return build


def _pin_shape_coalesce_union(other_type: str) -> Any:
    def build(tmp_path: Path, content: object) -> CompositionState:
        """The same fork, joined by a union coalesce instead of a row_union."""
        options = _all({"content": content, "format": "json"}, schema={"mode": "fixed", "fields": ["item: str"]})
        other = ["item: str", {"name": "guide_value", "type": other_type, "required": True, "nullable": False}]
        state = _row_union_state(tmp_path, options, other)
        nodes = []
        for node in state.nodes:
            if node.node_type == "row_union":
                node = replace(node, node_type="coalesce", policy="require_all", merge="union", input="inject_done")
            nodes.append(node)
        return replace(state, nodes=tuple(nodes))

    return build


_RENDERED_AS_NON_NULL_TYPE = "message_type"
_RENDERED_AS_NON_NULL_FLAG = "detail_nullable"
_RENDERED_AS_NON_NULL_MEMBERSHIP = "conflicting_membership"

_PIN_SHAPES = [
    pytest.param(_pin_shape_required_field_sink, frozenset(), id="sink-requiring-the-field"),
    pytest.param(_pin_shape_typed_sink, frozenset(), id="sink-declaring-it-str-non-null"),
    pytest.param(_pin_shape_pass_through_chain, frozenset(), id="pass-through-chain"),
    pytest.param(_pin_shape_row_union("any", False), frozenset(), id="row-union-fixed-vs-any-non-null"),
    pytest.param(_pin_shape_row_union("any", True), frozenset(), id="row-union-fixed-vs-any-nullable"),
    pytest.param(
        _pin_shape_row_union("str", False),
        frozenset({_RENDERED_AS_NON_NULL_TYPE, _RENDERED_AS_NON_NULL_FLAG}),
        id="row-union-fixed-vs-str-non-null",
    ),
    pytest.param(
        _pin_shape_row_union("str", True),
        frozenset({_RENDERED_AS_NON_NULL_TYPE, _RENDERED_AS_NON_NULL_FLAG}),
        id="row-union-fixed-vs-str-nullable",
    ),
    pytest.param(
        _pin_shape_row_union("any", False, item_type="int"),
        frozenset({_RENDERED_AS_NON_NULL_MEMBERSHIP, _RENDERED_AS_NON_NULL_FLAG}),
        id="row-union-fixed-vs-any-non-null-beside-another-conflict",
    ),
    pytest.param(_pin_shape_coalesce_union("any"), frozenset(), id="coalesce-union-vs-any"),
    pytest.param(_pin_shape_coalesce_union("str"), frozenset(), id="coalesce-union-vs-str"),
]


_ROW_UNION_REPORT = "row_union_schema_incompatible"
_WRITTEN_FIELD = "guide_value"
_INJECT_BRANCH = "inject_branch"

_GENERATED_SCHEMA_NAME = re.compile(r"(_CoalesceUnionSchema_)\d+")
"""Each validation numbers its generated union schema classes afresh; the counter carries no fact.

Applied to every message of all three graphs: it is the only text compared
inexactly outside ``_render_null_twin_as_non_null``.
"""

_NULLABLE_ANY_MISMATCH = re.compile(rf"({_WRITTEN_FIELD} \(expected [^()]*?, got )typing\.Any \| None\)")


def _render_null_twin_as_non_null(report: dict[str, Any]) -> tuple[dict[str, Any], frozenset[str]]:
    """Rewrite ONE report of the JSON-null twin as the non-null twin would render it; say what fired.

    Used only by the pin, only on the null twin, only on the
    ``row_union_schema_incompatible`` report, and only for the undeclared
    json written field. That report is the one authoring surface that renders
    the field's nullability (every place a probed ``nullable`` is published
    is that report's ``row_union_schema`` detail, built once at
    ``state.py:5896``). Three renderings change and nothing else:

    - ``message_type``: the type-mismatch line prints ``typing.Any | None``
      where the non-null twin prints ``Any``;
    - ``detail_nullable``: the field's ``nullable`` flag in the file_inject
      branch's detail is ``true`` where the non-null twin has ``false``;
    - ``conflicting_membership``: when the other branch also types the field
      ``any``, the field is listed as conflicting only because whole field
      definitions are compared (``core/dag/schema_validation.py:1157-1163``),
      so it leaves the list (detail and message) as it is absent for the
      non-null twin. The two ``any`` definitions are compatible on their own.
    """
    if report["error_code"] != _ROW_UNION_REPORT:
        return report, frozenset()
    report = json.loads(json.dumps(report))
    fired: set[str] = set()
    message, count = _NULLABLE_ANY_MISMATCH.subn(r"\1Any)", report["message"])
    if count:
        fired.add(_RENDERED_AS_NON_NULL_TYPE)
    detail = report["row_union_schema"]
    for branch in detail["branches"]:
        for field in branch["fields"]:
            if (
                branch["branch"] == _INJECT_BRANCH
                and field["name"] == _WRITTEN_FIELD
                and field["field_type"] == "any"
                and field["nullable"]
            ):
                field["nullable"] = False
                fired.add(_RENDERED_AS_NON_NULL_FLAG)
    other_types = {
        field["field_type"]
        for branch in detail["branches"]
        if branch["branch"] != _INJECT_BRANCH
        for field in branch["fields"]
        if field["name"] == _WRITTEN_FIELD
    }
    if other_types == {"any"} and _WRITTEN_FIELD in detail["conflicting_fields"]:
        listed = detail["conflicting_fields"]
        kept = [name for name in listed if name != _WRITTEN_FIELD]
        message = message.replace(f"conflicting fields {listed}", f"conflicting fields {kept}")
        detail["conflicting_fields"] = kept
        fired.add(_RENDERED_AS_NON_NULL_MEMBERSHIP)
    report["message"] = message
    return report, frozenset(fired)


def _reports(summary: ValidationSummary) -> dict[str, Any]:
    def entry(item: Any) -> dict[str, Any]:
        facts = item.to_dict()
        facts["error_code"] = item.error_code
        facts["message"] = _GENERATED_SCHEMA_NAME.sub(r"\1N", facts["message"])
        return facts

    return {
        "is_valid": summary.is_valid,
        "errors": [entry(item) for item in summary.errors],
        "warnings": [entry(item) for item in summary.warnings],
        "edge_contracts": [contract.to_dict() for contract in summary.edge_contracts],
    }


def _null_twin_rendered_as_non_null(reports: dict[str, Any]) -> tuple[dict[str, Any], frozenset[str]]:
    fired: set[str] = set()
    errors = []
    for report in reports["errors"]:
        rendered, report_fired = _render_null_twin_as_non_null(report)
        errors.append(rendered)
        fired |= report_fired
    return {**reports, "errors": errors}, frozenset(fired)


@pytest.mark.parametrize(("shape", "expected_renderings"), _PIN_SHAPES)
def test_PIN_undeclared_json_nullability_is_unobservable_at_authoring(
    tmp_path: Path, shape: Any, expected_renderings: frozenset[str]
) -> None:
    """PIN: the undeclared-json stand-in is sound only while this holds.

    A JSON null in the file makes the undeclared written field nullable; any
    other value leaves it non-null. The field is typed ``any``, and no
    authoring VERDICT separates a nullable ``any`` from a non-null one. The
    twins' whole authoring results (verdict, every report's code, component,
    severity, message text and structured detail, edge contracts) are
    compared exactly. The one exception is ``_render_null_twin_as_non_null``:
    three renderings inside the row_union report, applied to the null twin
    only, asserted to fire exactly where expected and to be the ONLY
    difference. The file-held node, whose probe sees a non-null stand-in,
    must equal the non-null twin exactly. If a consumer's verdict ever
    depends on nullability for ``any``, this goes red and the undeclared
    json stand-in must go back to deferring.
    """
    holding_null = _reports(shape(tmp_path / "null", "null").validate())
    holding_value = _reports(shape(tmp_path / "value", '{"a": 1}').validate())
    file_held = _reports(shape(tmp_path / "file", _marker(ENTRY_BLOB, "null")).validate())

    rendered_null, fired = _null_twin_rendered_as_non_null(holding_null)

    assert fired == expected_renderings, "the normalisation fired somewhere other than expected"
    if expected_renderings:
        assert holding_null != holding_value, "the normalisation must be needed where it fires"
    else:
        assert holding_null == holding_value
    assert rendered_null == holding_value, "an authoring consumer now separates nullable any from non-null any"
    assert file_held == holding_value


def test_what_the_file_held_node_publishes_for_its_undeclared_json_field(tmp_path: Path) -> None:
    """Positive pin of the published residue.

    ``nullable: false`` below is the STAND-IN's value, not a fact about the
    file: with the bytes, a JSON null would make it ``true``. It appears only
    inside a ``row_union_schema_incompatible`` error, which is an error in
    both twins; the runtime preflight in the same round reads the bytes and
    reports the exact type.
    """
    summary = _pin_shape_row_union("str", False)(tmp_path, _marker(ENTRY_BLOB, "null")).validate()

    (report,) = [entry.to_dict() for entry in summary.errors if entry.error_code == _ROW_UNION_REPORT]
    (inject_branch,) = [branch for branch in report["row_union_schema"]["branches"] if branch["branch"] == _INJECT_BRANCH]
    (published,) = [field for field in inject_branch["fields"] if field["name"] == _WRITTEN_FIELD]

    assert published == {"name": _WRITTEN_FIELD, "field_type": "any", "required": True, "nullable": False}
    assert f"{_WRITTEN_FIELD} (expected str, got Any)" in report["message"]


def _row_union_of_two_file_held_arms(tmp_path: Path, inject_text: str) -> CompositionState:
    """The fixed/fixed row_union with BOTH arms file-held, so the runtime preflight reaches the row_union check.

    The inject arm leaves the written field undeclared and its file holds
    ``inject_text``; the other arm is a ``file_inject`` declaring the field
    ``str``, non-null. Paths sit in the session's folders the runtime admits.
    """
    session_root = tmp_path / "blobs" / str(SESSION_ID)
    (tmp_path / "outputs" / str(SESSION_ID)).mkdir(parents=True)
    state = _row_union_state(
        session_root,
        _all({"content": _marker(ENTRY_BLOB, inject_text), "format": "json"}, schema={"mode": "fixed", "fields": ["item: str"]}),
        ["item: str"],
    )
    other_options = _all(
        {"content": _marker(SECOND_ENTRY_BLOB, '"a sentence"'), "format": "json"},
        schema={"mode": "fixed", "fields": _OTHER_ARM},
    )
    nodes = tuple(replace(node, plugin="file_inject", options=other_options) if node.id == "other" else node for node in state.nodes)
    (sink,) = state.outputs
    sink_path = str(tmp_path / "outputs" / str(SESSION_ID) / "results.json")
    return replace(state, nodes=nodes, outputs=(replace(sink, options={**sink.options, "path": sink_path}),))


def _runtime_preflight(tmp_path: Path, state: CompositionState, texts: dict[UUID, str]) -> ValidationResult:
    """The runtime preflight the composer state routes run, with the session's file bytes behind each marker."""
    records = {blob_id: _record_for(blob_id, text) for blob_id, text in texts.items()}
    catalog = create_catalog_service()
    return asyncio.run(
        _runtime_preflight_for_state(
            state,
            settings=SimpleNamespace(data_dir=tmp_path, secret_wiring_allowlist=(), composer_runtime_preflight_timeout_seconds=60.0),
            secret_service=None,
            user_id=None,
            session_id=SESSION_ID,
            plugin_snapshot=PluginAvailabilitySnapshot.for_trained_operator(catalog),
            profile_registry=None,
            catalog=catalog,
            blob_get_content=lambda blob_id: (records[blob_id], texts[blob_id].encode()),
        )
    )


@pytest.mark.parametrize(
    ("inject_text", "runtime_type"),
    [pytest.param("null", "typing.Any | None", id="file-holds-null"), pytest.param('{"a": 1}', "Any)", id="file-holds-a-value")],
)
def test_the_runtime_preflight_in_the_same_round_reports_the_exact_type(tmp_path: Path, inject_text: str, runtime_type: str) -> None:
    """Proves the residue sentence in ``_file_inject_entry_stand_in``'s docstring.

    Authoring renders the undeclared json field as ``Any`` whatever the file
    holds; the runtime preflight, given the same state and the file's bytes,
    renders ``typing.Any | None`` for a file holding a JSON null and ``Any``
    for one holding a value. The second case is the control: the runtime's
    rendering follows the bytes, not the shape.
    """
    state = _row_union_of_two_file_held_arms(tmp_path, inject_text)

    authoring = state.validate()
    runtime = _runtime_preflight(tmp_path, state, {ENTRY_BLOB: inject_text, SECOND_ENTRY_BLOB: '"a sentence"'})

    (authoring_report,) = [entry.message for entry in authoring.errors if entry.error_code == _ROW_UNION_REPORT]
    assert f"{_WRITTEN_FIELD} (expected str, got Any)" in authoring_report
    assert runtime.is_valid is False
    (runtime_report,) = [
        error.message for error in runtime.errors if "receives incompatible schemas from multiple branches" in error.message
    ]
    assert f"{_WRITTEN_FIELD} (expected str, got {runtime_type}" in runtime_report


@pytest.mark.parametrize(
    ("nullable", "conflicts"),
    [pytest.param(False, False, id="declared-non-null"), pytest.param(True, True, id="declared-nullable")],
)
def test_declared_json_branch_of_a_fixed_row_union_runs_the_real_check(tmp_path: Path, nullable: bool, conflicts: bool) -> None:
    schema = {"mode": "fixed", "fields": ["item: str", {"name": "guide_value", "type": "str", "required": True, "nullable": nullable}]}
    file_held = _row_union_state(
        tmp_path / "file", _all({"content": _marker(ENTRY_BLOB, '"a sentence"'), "format": "json"}, schema=schema), _OTHER_ARM
    )
    inline = _row_union_state(tmp_path / "inline", _all({"content": '"a sentence"', "format": "json"}, schema=schema), _OTHER_ARM)

    summary = file_held.validate()

    assert "contract_probe_deferred" not in _codes(summary)[1]
    assert _row_union_codes(summary) == _row_union_codes(inline.validate())
    assert ("row_union_schema_incompatible" in _row_union_codes(summary)) is conflicts


# ---------------------------------------------------------------------------
# The stand-in never leaves the probe
# ---------------------------------------------------------------------------


def test_stand_in_exists_only_in_the_detached_projection(tmp_path: Path) -> None:
    state = _chain(tmp_path, inject_file=True, prompt_files=True, query_file=True)
    options = state.nodes[0].options
    saved = json.loads(json.dumps(deep_thaw(options)))

    prepared = prepare_validation_probe_options(options, plugin="file_inject")
    summary = state.validate()

    assert prepared["files"]["rubric"]["content"] == PROBE_FILE_PLACEHOLDER
    assert deep_thaw(state.nodes[0].options) == saved, "saved options keep the marker"
    assert deep_thaw(state.nodes[0].options)["files"]["rubric"]["content"] == _marker(RUBRIC_BLOB, RUBRIC_TEXT)
    assert summary.is_valid
    surfaces = {
        "state.to_dict": json.dumps(state.to_dict(), default=str),
        "validation summary": json.dumps([entry.to_dict() for entry in (*summary.errors, *summary.warnings, *summary.suggestions)]),
        "generated pipeline": json.dumps(yaml_generator.generate_pipeline_dict(state), default=str),
        "generated yaml": yaml_generator.generate_yaml(state),
    }
    for surface, rendered in surfaces.items():
        assert PROBE_FILE_PLACEHOLDER not in rendered, surface
        assert "validation-probe" not in rendered, surface
    assert str(RUBRIC_BLOB) in surfaces["generated yaml"], "generated YAML still carries the marker"


def test_wiring_an_upload_through_the_tool_returns_a_valid_result_without_the_stand_in(blob_env: dict[str, Any]) -> None:
    """Through ``wire_blob_inline_ref``: a sink requiring the written field is satisfied, and the tool result holds no stand-in."""
    earlier = upload_tools._upload(blob_env, content="Earlier upload.\n")
    state = upload_tools._state(upload_tools._node("inject", upload_tools._options(earlier)))
    output = state.outputs[0]
    state = replace(
        state, outputs=(replace(output, options={**output.options, "schema": {"mode": "observed", "required_fields": ["guidance"]}}),)
    )
    uploaded = upload_tools._upload(blob_env)

    result = upload_tools._execute(
        "wire_blob_inline_ref",
        {"field_path": "node:inject.options.files.invoice.content", "blob_id": uploaded["blob_ref"]},
        state,
        blob_env,
    )

    assert result.success is True, [error.message for error in result.validation.errors]
    assert result.validation.is_valid, [error.to_dict() for error in result.validation.errors]
    assert "contract_probe_deferred" not in _codes(result.validation)[1]
    [node] = [node for node in result.updated_state.nodes if node.id == "inject"]
    assert deep_thaw(node.options)["files"]["invoice"]["content"] == uploaded
    rendered = json.dumps(result.to_dict(), default=str)
    assert PROBE_FILE_PLACEHOLDER not in rendered
    assert "validation-probe" not in rendered


# ---------------------------------------------------------------------------
# Required-control wiring on a file-held graph
# ---------------------------------------------------------------------------


def _profiled(state: CompositionState) -> CompositionState:
    """The guardrail policy view admits llm nodes only through an operator profile alias."""
    nodes = []
    for node in state.nodes:
        if node.plugin == "llm":
            kept = {
                key: value
                for key, value in node.options.items()
                if key not in ("provider", "api_key", "model", "max_capacity_retry_seconds", INTERPRETATION_REQUIREMENTS_KEY)
            }
            node = replace(node, options={**kept, "profile": "sonnet"})
        nodes.append(node)
    return replace(state, nodes=tuple(nodes))


def test_required_controls_wire_into_a_file_held_chain_and_the_wired_graph_validates(tmp_path: Path) -> None:
    """Every wiring call site re-validates and raises AuditIntegrityError on an invalid wired graph."""
    from elspeth.web.composer.required_controls import wire_required_controls_state
    from tests.unit.web.composer.test_planner_authoring_aids import _guardrail_profile_view

    view, snapshot = _guardrail_profile_view(tmp_path)
    state = _profiled(_chain(tmp_path, inject_file=True, prompt_files=True, query_file=True))

    before = view.validate_composition_state(state).validation
    wired = wire_required_controls_state(state, snapshot, view)
    after = view.validate_composition_state(wired).validation

    assert before.is_valid, [entry.to_dict() for entry in before.errors]
    inserted = [node.plugin for node in wired.nodes if node.id not in {existing.id for existing in state.nodes}]
    assert inserted, "the REQUIRED controls were wired in"
    assert after.is_valid, [entry.to_dict() for entry in after.errors]
    assert "contract_probe_deferred" not in {entry.error_code for entry in after.warnings}


def test_a_real_missing_field_still_leaves_the_wired_graph_invalid(tmp_path: Path) -> None:
    """Control: wiring repairs nothing; a field nobody writes is still refused after it."""
    from elspeth.web.composer.required_controls import wire_required_controls_state
    from tests.unit.web.composer.test_planner_authoring_aids import _guardrail_profile_view

    view, snapshot = _guardrail_profile_view(tmp_path)
    state = _profiled(_chain(tmp_path, inject_file=True, prompt_files=True, query_file=True))
    sink = state.outputs[0]
    fields = [*sink.options["schema"]["fields"], "never_written: str"]
    state = replace(state, outputs=(replace(sink, options={**sink.options, "schema": {"mode": "flexible", "fields": fields}}),))

    wired = wire_required_controls_state(state, snapshot, view)
    after = view.validate_composition_state(wired).validation

    assert not after.is_valid
    assert any("never_written" in entry.message for entry in after.errors)
