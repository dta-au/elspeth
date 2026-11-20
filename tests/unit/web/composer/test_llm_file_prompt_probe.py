"""A file-backed llm prompt must not zero the node's authoring guarantees.

``LLMConfig`` types ``prompt_template``, ``system_prompt`` and a query's
``template`` as ``str``. Once ``wire_blob_inline_ref`` binds a file there the
option is an ``inline_content`` marker, the resolver-free construction probe
fails on it, and a deferred probe answers "participates, guarantees nothing":
every consumer declaring fields downstream was refused with
``guarantees: [(none)]`` although prompt text is no input to the llm output
contract. ``prepare_validation_probe_options`` now stands probe-only text in
for a valid marker at those positions.

The second half of this file is the price of that: every check that reads
prompt text is blind to a file at authoring, so each one is shown here to be
refused by execution validation once the authorized bytes are materialized.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.blobs_inline import is_widened_blob_ref
from elspeth.core.prompt_artifact import approved_prompt_artifact_hash
from elspeth.core.templates import extract_jinja2_field_usage
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.composer import yaml_generator
from elspeth.web.composer._validation_probe import prepare_validation_probe_options
from elspeth.web.composer.state import CompositionState, NodeSpec, ValidationSummary
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from elspeth.web.sessions.routes._helpers import _composer_persisted_validation
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.unit.web.execution import test_deferred_contract_runtime_refusal as runtime_proofs
from tests.unit.web.execution import test_service as _service_tests
from tests.unit.web.execution.test_service import _WebSettingsStub
from tests.unit.web.execution.test_validate_blob_inline import (
    BLOB_ID,
    _ready_blob_record,
    _state_with_inline_prompt,
    _state_with_reference_join,
)

# Run-admission fixtures, requested by name only (none of them is autouse here).
broadcaster = _service_tests.broadcaster
execution_fixture = _service_tests.execution_fixture
mock_loop = _service_tests.mock_loop
mock_session_service = _service_tests.mock_session_service
mock_settings = _service_tests.mock_settings
real_loop = _service_tests.real_loop
service = _service_tests.service

# The probe's stand-in text, restated so a change to it is a deliberate edit here.
PROBE_PROMPT_FILE_PLACEHOLDER = "validation-probe-prompt-file-placeholder"
GOOD_PROMPT = "Summarise this customer review in one sentence: {{ row.review }}"
SINK_FIELDS: dict[str, Any] = {"mode": "flexible", "fields": ["review: str", "summary: str"]}
_QUERY: dict[str, Any] = {"name": "q1", "input_fields": {"c": "review"}, "output_fields": [{"suffix": "score", "type": "integer"}]}


def _marker(text: str = GOOD_PROMPT) -> dict[str, str]:
    return {"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _state(
    tmp_path: Path,
    *,
    prompt_template: object,
    sink_schema: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    without: tuple[str, ...] = (),
) -> tuple[UUID, CompositionState]:
    """csv(review) -> llm(response_field=summary) -> json sink declaring fields."""
    session_id = uuid4()
    state = _state_with_inline_prompt(tmp_path, session_id=session_id)
    source = state.sources["source"]
    Path(source.options["path"]).write_text("review\nGreat product\n", encoding="utf-8")
    source = replace(
        source,
        options={**source.options, "schema": {"mode": "flexible", "fields": ["review: str"], "guaranteed_fields": ["review"]}},
    )
    node_options = {
        **state.nodes[0].options,
        "prompt_template": prompt_template,
        "system_prompt": "You summarise customer reviews.",
        "required_input_fields": ["review"],
        "response_field": "summary",
        "schema": {"mode": "observed"},
        **(options or {}),
    }
    for key in without:
        del node_options[key]
    output = replace(state.outputs[0], options={**state.outputs[0].options, "schema": sink_schema or SINK_FIELDS})
    return session_id, replace(state, sources={"source": source}, nodes=(replace(state.nodes[0], options=node_options),), outputs=(output,))


def _codes(summary: ValidationSummary) -> tuple[list[str | None], list[str | None]]:
    return [entry.error_code for entry in summary.errors], [entry.error_code for entry in summary.warnings if entry.error_code]


def _execution_validate(tmp_path: Path, session_id: UUID, state: CompositionState, file_text: str) -> ValidationResult:
    """Execution validation with the file's real bytes behind the marker."""
    raw = file_text.encode()
    record = replace(
        _ready_blob_record(session_id=session_id, size_bytes=len(raw)),
        content_hash=hashlib.sha256(raw).hexdigest(),
    )
    return validate_pipeline_for_trained_operator(
        state,
        SimpleNamespace(data_dir=tmp_path),
        yaml_generator,
        blob_get_metadata=lambda _blob_id: record,
        blob_get_content=lambda _blob_id: (record, raw),
        session_id=str(session_id),
    )


# ---------------------------------------------------------------------------
# The defect: a file prompt keeps the node's true guarantees
# ---------------------------------------------------------------------------


def test_file_prompt_template_keeps_a_field_declaring_sink_valid(tmp_path: Path) -> None:
    _, inline = _state(tmp_path / "inline", prompt_template=GOOD_PROMPT)
    assert _codes(inline.validate()) == ([], []), "control: the inline twin of this pipeline is valid"

    _, state = _state(tmp_path / "file", prompt_template=_marker())

    summary = state.validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _codes(summary) == ([], [])


def test_file_prompt_template_and_file_system_prompt_stay_valid(tmp_path: Path) -> None:
    _, state = _state(tmp_path, prompt_template=_marker(), options={"system_prompt": _marker("You summarise customer reviews.")})

    summary = state.validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _codes(summary) == ([], [])


def test_file_prompt_reports_the_same_guarantees_as_inline_text(tmp_path: Path) -> None:
    """Negative control: a field the llm never emits is still refused, naming the true set."""
    sink = {"mode": "flexible", "fields": ["review: str", "never_emitted: str"]}
    _, inline = _state(tmp_path / "inline", prompt_template=GOOD_PROMPT, sink_schema=sink)
    _, file_backed = _state(tmp_path / "file", prompt_template=_marker(), sink_schema=sink)

    inline_summary = inline.validate()
    file_summary = file_backed.validate()

    assert not file_summary.is_valid
    assert _codes(file_summary) == (["sink_contract_violation"], [])
    message = file_summary.errors[0].message
    assert "Missing fields: [never_emitted]" in message
    assert "(none)" not in message
    assert "summary" in message.split("guarantees:")[1].split("Missing fields")[0]
    assert [entry.message for entry in file_summary.errors] == [entry.message for entry in inline_summary.errors]


@pytest.mark.parametrize("declared", [["review"], [" review "], ["review", "café"]])
def test_placeholder_reads_exactly_the_declared_fields(tmp_path: Path, declared: list[str]) -> None:
    """Padded and non-ASCII declarations construct: the read uses the name the validator keeps."""
    session_id = uuid4()
    state = _state_with_inline_prompt(tmp_path, session_id=session_id)
    node = replace(
        state.nodes[0],
        options={**state.nodes[0].options, "prompt_template": _marker(), "required_input_fields": declared},
    )

    prepared = prepare_validation_probe_options(node.options, plugin="llm")

    assert set(extract_jinja2_field_usage(prepared["prompt_template"]).fields) == {name.strip() for name in declared}
    transform = get_shared_plugin_manager().create_transform("llm", prepared)
    try:
        assert transform.declared_input_fields == frozenset(name.strip() for name in declared)
        assert dict(transform.header_spelled_lookups) == {}
    finally:
        transform.close()


@pytest.mark.parametrize(
    "name",
    ["Review Text", "it's", 'say "hi"', "back\\slash", "}} {{ lookup.x", "{% raw %}", "line\nbreak", "\U0001f600", "class"],
)
def test_placeholder_quotes_any_declared_name_as_one_subscript_read(name: str) -> None:
    """A name the identifier policy refuses still yields parseable text reading exactly that name."""
    prepared = prepare_validation_probe_options({"prompt_template": _marker(), "required_input_fields": [name]}, plugin="llm")

    assert prepared["prompt_template"].startswith(PROBE_PROMPT_FILE_PLACEHOLDER)
    assert set(extract_jinja2_field_usage(prepared["prompt_template"]).fields) == {name.strip()}


def test_a_non_identifier_declaration_is_refused_by_its_own_validator(tmp_path: Path) -> None:
    session_id = uuid4()
    state = _state_with_inline_prompt(tmp_path, session_id=session_id)
    prepared = prepare_validation_probe_options(
        {**state.nodes[0].options, "prompt_template": _marker(), "required_input_fields": ["Review Text"]}, plugin="llm"
    )

    with pytest.raises(ValueError, match=r"required_input_fields.*not a valid Python identifier"):
        get_shared_plugin_manager().create_transform("llm", prepared)

    _, authored = _state(tmp_path / "state", prompt_template=_marker(), options={"required_input_fields": ["Review Text"]})
    _errors, warnings = _codes(authored.validate())
    assert "contract_probe_failed" in warnings
    assert "contract_probe_deferred" not in warnings


@pytest.mark.parametrize("declared", [None, [], "review", [7, None, ""], {"review": 1}])
def test_malformed_or_empty_declaration_contributes_no_read(declared: object) -> None:
    prepared = prepare_validation_probe_options({"prompt_template": _marker(), "required_input_fields": declared}, plugin="llm")
    undeclared = prepare_validation_probe_options({"prompt_template": _marker()}, plugin="llm")

    assert prepared["prompt_template"] == PROBE_PROMPT_FILE_PLACEHOLDER
    assert undeclared["prompt_template"] == PROBE_PROMPT_FILE_PLACEHOLDER


# ---------------------------------------------------------------------------
# Multi-query positions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("form", ["list", "mapping"])
def test_file_backed_query_template_keeps_a_field_declaring_sink_valid(tmp_path: Path, form: str) -> None:
    query = {**_QUERY, "template": _marker("Rate {{ row.c }}")}
    queries: object = [query] if form == "list" else {"q1": {key: value for key, value in query.items() if key != "name"}}
    _, state = _state(
        tmp_path,
        prompt_template=None,
        sink_schema={"mode": "flexible", "fields": ["review: str", "q1_score: int"]},
        options={"queries": queries},
        without=("prompt_template", "response_field"),
    )

    summary = state.validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _codes(summary) == ([], [])


def test_file_backed_node_fallback_in_query_mode_reads_no_row_field(tmp_path: Path) -> None:
    """A query renders its own bindings, so a row read in the fallback would be an unbound name."""
    _, state = _state(
        tmp_path,
        prompt_template=_marker("Rate {{ row.c }}"),
        sink_schema={"mode": "flexible", "fields": ["review: str", "q1_score: int"]},
        options={"queries": [_QUERY]},
        without=("response_field",),
    )

    prepared = prepare_validation_probe_options(state.nodes[0].options, plugin="llm")
    summary = state.validate()

    assert prepared["prompt_template"] == PROBE_PROMPT_FILE_PLACEHOLDER
    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _codes(summary) == ([], [])


# ---------------------------------------------------------------------------
# Only a well-formed marker, only at a prompt position, only for llm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malformed",
    [
        {"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "not-a-hash"},
        {"blob_ref": str(BLOB_ID), "mode": "bogus", "sha256": "a" * 64},
        {"blob_ref": "not-a-uuid", "mode": "inline_content", "sha256": "a" * 64},
        {"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "a" * 64, "extra": "key"},
        {"mode": "inline_content", "sha256": "a" * 64},
    ],
)
@pytest.mark.parametrize("position", ["prompt_template", "system_prompt", "query_template"])
def test_malformed_marker_is_not_substituted_and_stays_a_probe_failure(tmp_path: Path, malformed: dict[str, str], position: str) -> None:
    if position == "query_template":
        _, state = _state(
            tmp_path,
            prompt_template=None,
            sink_schema={"mode": "flexible", "fields": ["review: str", "q1_score: int"]},
            options={"queries": [{**_QUERY, "template": malformed}]},
            without=("prompt_template", "response_field"),
        )
        prepared = prepare_validation_probe_options(state.nodes[0].options, plugin="llm")
        assert prepared["queries"][0]["template"] == malformed
    else:
        _, state = _state(tmp_path, prompt_template=GOOD_PROMPT, options={position: malformed})
        prepared = prepare_validation_probe_options(state.nodes[0].options, plugin="llm")
        assert prepared[position] == malformed

    summary = state.validate()
    errors, warnings = _codes(summary)

    assert not summary.is_valid
    assert "sink_contract_violation" in errors
    assert "contract_probe_failed" in warnings
    assert "contract_probe_deferred" not in warnings


@pytest.mark.parametrize("position", ["prompt_template", "system_prompt", "query_template"])
def test_a_well_formed_marker_of_another_mode_is_not_substituted(tmp_path: Path, position: str) -> None:
    """Only ``inline_content`` names prompt text; ``bind_source`` is a valid marker and still not a prompt file."""
    other_mode = {"blob_ref": str(BLOB_ID), "mode": "bind_source"}
    parsed = is_widened_blob_ref(other_mode)
    assert parsed is not None and parsed.mode == "bind_source", "control: the marker is well formed, not malformed"
    if position == "query_template":
        _, state = _state(
            tmp_path,
            prompt_template=None,
            sink_schema={"mode": "flexible", "fields": ["review: str", "q1_score: int"]},
            options={"queries": [{**_QUERY, "template": other_mode}], _HASH_KEY: "b" * 64},
            without=("prompt_template", "response_field"),
        )
        prepared = prepare_validation_probe_options(state.nodes[0].options, plugin="llm")
        assert prepared["queries"][0]["template"] == other_mode
    else:
        _, state = _state(tmp_path, prompt_template=GOOD_PROMPT, options={position: other_mode, _HASH_KEY: "b" * 64})
        prepared = prepare_validation_probe_options(state.nodes[0].options, plugin="llm")
        assert prepared[position] == other_mode

    summary = state.validate()
    errors, warnings = _codes(summary)

    assert prepared[_HASH_KEY] == "b" * 64, "nothing was substituted, so the hash stays in the projection"
    assert not summary.is_valid
    assert "sink_contract_violation" in errors
    assert "guarantees: [(none)]" in next(entry.message for entry in summary.errors if entry.error_code == "sink_contract_violation")
    assert warnings == ["contract_probe_failed"]


def test_an_explicit_null_queries_key_is_single_prompt_mode(tmp_path: Path) -> None:
    """``queries: None`` is single-prompt: the stand-in must read the declared row fields or the config refuses it."""
    _, state = _state(tmp_path, prompt_template=_marker(), options={"queries": None})

    prepared = prepare_validation_probe_options(state.nodes[0].options, plugin="llm")
    summary = state.validate()

    assert prepared["queries"] is None
    assert prepared["prompt_template"] == PROBE_PROMPT_FILE_PLACEHOLDER + " {{ row['review'] }}"
    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _codes(summary) == ([], [])


# Formerly test_a_marker_at_a_non_prompt_llm_option_still_defers_with_zero_guarantees (stage 2a, vote arm (a)).
def test_a_marker_at_a_non_prompt_llm_option_defers_and_forwards_only_upstream_guarantees(tmp_path: Path) -> None:
    """``llm`` is a known pass-through: deferring, it votes with what it forwards (``review``), never with what it adds."""
    _, state = _state(tmp_path, prompt_template=GOOD_PROMPT, options={"response_field": _marker()})

    prepared = prepare_validation_probe_options(state.nodes[0].options, plugin="llm")
    summary = state.validate()
    errors, warnings = _codes(summary)

    assert prepared["response_field"] == _marker()
    assert errors == ["sink_contract_violation"]
    assert warnings == ["contract_probe_deferred"]
    assert "guarantees: [review]. Missing fields: [summary]." in summary.errors[0].message
    assert [warning.message for warning in summary.warnings if warning.error_code == "contract_probe_deferred"] == [
        "Node 'classify' (llm) reads an attached file that this check does not open, so the fields this step adds, "
        "their types and whether they can be empty are checked before the run starts. No change is needed."
    ]


_SYSTEM_PROMPT = "You summarise customer reviews."
_HASH_KEY = "approved_prompt_artifact_hash"


@pytest.mark.parametrize("digest", ["right", "stale"])
def test_a_saved_approval_hash_beside_a_file_prompt_is_ignored_by_the_probe_and_by_execution_validation(
    tmp_path: Path, digest: str
) -> None:
    """Approving a file card saves the hash of the reviewed text; the probe holds no such text.

    Formerly "a stale approval hash beside a file prompt fails closed". It
    failed closed for the RIGHT hash too, which returned every approved
    file-prompted node to zero guarantees. The saved hash has no authority for
    a file prompt at run time either: materialization drops it and the run
    binds the admitted review snapshot's hash instead, so execution validation
    admits both values here. What refuses a stale approval is the resolved
    review's anchor (``tests/unit/web/sessions/test_approved_file_prompt_keeps_guarantees.py``).

    The run-time half is pinned elsewhere: the drop is
    ``materialize_state_for_execution`` (``_ensure_prompt_template_hash``) and
    the re-bind is ``execution/service.py::_bind_admitted_prompt_artifact_hashes``,
    both exercised by
    ``tests/unit/web/execution/test_file_prompt_approved_artifact_hash.py``.
    """
    saved = {
        "right": approved_prompt_artifact_hash(prompt_template=GOOD_PROMPT, system_prompt=_SYSTEM_PROMPT),
        "stale": approved_prompt_artifact_hash(prompt_template="An earlier inline prompt {{ row.review }}", system_prompt=_SYSTEM_PROMPT),
    }[digest]
    session_id, state = _state(tmp_path, prompt_template=_marker(), options={_HASH_KEY: saved})

    prepared = prepare_validation_probe_options(state.nodes[0].options, plugin="llm")
    summary = state.validate()
    result = _execution_validate(tmp_path, session_id, state, GOOD_PROMPT)

    assert _HASH_KEY not in prepared
    assert state.nodes[0].options[_HASH_KEY] == saved, "saved options keep the hash"
    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _codes(summary) == ([], [])
    assert result.is_valid, [error.message for error in result.errors]
    assert all(check.passed for check in result.checks), [check.name for check in result.checks if not check.passed]


@pytest.mark.parametrize(
    ("options", "without"),
    [
        pytest.param({"system_prompt": _marker(_SYSTEM_PROMPT)}, (), id="file-system-prompt-inline-template"),
        pytest.param(
            {"queries": [{**_QUERY, "template": _marker("Rate {{ row.c }}")}]},
            ("prompt_template", "response_field"),
            id="file-query-template",
        ),
        pytest.param({"queries": [_QUERY], "prompt_template": _marker("Rate {{ row.c }}")}, ("response_field",), id="file-query-fallback"),
    ],
)
def test_one_file_backed_prompt_position_is_enough_to_omit_the_hash(
    tmp_path: Path, options: dict[str, Any], without: tuple[str, ...]
) -> None:
    """The hash binds system text and every effective template together; one unknown part makes it uncheckable."""
    sink = {"mode": "flexible", "fields": ["review: str"]} if "queries" in options else SINK_FIELDS
    _, state = _state(tmp_path, prompt_template=GOOD_PROMPT, sink_schema=sink, options={**options, _HASH_KEY: "b" * 64}, without=without)

    prepared = prepare_validation_probe_options(state.nodes[0].options, plugin="llm")
    summary = state.validate()

    assert _HASH_KEY not in prepared
    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _codes(summary) == ([], [])


def test_a_wrong_approval_hash_beside_an_inline_prompt_still_fails_the_probe(tmp_path: Path) -> None:
    """Unchanged: with inline text the projection holds what the hash binds, so the model checks it."""
    right = approved_prompt_artifact_hash(prompt_template=GOOD_PROMPT, system_prompt=_SYSTEM_PROMPT)
    _, approved = _state(tmp_path / "right", prompt_template=GOOD_PROMPT, options={_HASH_KEY: right})
    _, drifted = _state(tmp_path / "wrong", prompt_template=GOOD_PROMPT, options={_HASH_KEY: "b" * 64})

    prepared = prepare_validation_probe_options(drifted.nodes[0].options, plugin="llm")
    summary = drifted.validate()

    assert _codes(approved.validate()) == ([], []), "control: the matching hash is accepted"
    assert prepared[_HASH_KEY] == "b" * 64
    assert _codes(summary) == (["sink_contract_violation"], ["contract_probe_failed"])
    assert "guarantees: [(none)]" in summary.errors[0].message


@pytest.mark.parametrize("position", ["prompt_template", "system_prompt"])
def test_a_malformed_marker_keeps_the_hash_and_fails_the_probe(tmp_path: Path, position: str) -> None:
    malformed = {"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "not-a-hash"}
    _, state = _state(tmp_path, prompt_template=GOOD_PROMPT, options={position: malformed, _HASH_KEY: "b" * 64})

    prepared = prepare_validation_probe_options(state.nodes[0].options, plugin="llm")
    errors, warnings = _codes(state.validate())

    assert prepared[position] == malformed
    assert prepared[_HASH_KEY] == "b" * 64
    assert errors == ["sink_contract_violation"]
    assert warnings == ["contract_probe_failed"]


@pytest.mark.parametrize("plugin", ["reference_join", "field_mapper", "keyword_filter", None])
def test_no_other_plugin_receives_probe_text(plugin: str | None) -> None:
    options = {
        "prompt_template": _marker(),
        "system_prompt": _marker(),
        "content": _marker(),
        "queries": [{"name": "q1", "template": _marker()}],
        "required_input_fields": ["review"],
    }

    prepared = prepare_validation_probe_options(options, plugin=plugin)

    assert prepared == options
    assert PROBE_PROMPT_FILE_PLACEHOLDER not in json.dumps(prepared)


_JOIN_NOTICE = (
    "Node 'enrich' (reference_join) reads an attached file that this check does not open, so the fields it adds (description), "
    "their types and whether they can be empty are checked before the run starts. No change is needed."
)


def _join_with_authored_guarantee(tmp_path: Path, session_id: UUID, table: str) -> CompositionState:
    state = _state_with_reference_join(tmp_path, session_id=session_id, reference_format="csv", content=_marker(table))
    return replace(
        state,
        nodes=(
            replace(
                state.nodes[0],
                options={**state.nodes[0].options, "schema": {"mode": "observed", "guaranteed_fields": ["description"]}},
            ),
        ),
        outputs=(
            replace(
                state.outputs[0],
                options={**state.outputs[0].options, "schema": {"mode": "observed", "required_fields": ["description"]}},
            ),
        ),
    )


# Rewritten in stage 2a from the 5bc4b56c9 pin of the same name; the name is kept so ``git log -S`` finds the change.
def test_non_llm_blob_producer_still_defers_with_zero_guarantees(tmp_path: Path) -> None:
    """``reference_join``'s output fields are read from the table bytes: unknown bytes prove nothing the node adds.

    What moved where, and why. The old pin asserted the graph INVALID with
    ``guarantees: [(none)]`` and ``Missing fields: [description]``: an
    authoring refusal standing in for a check only the bytes can make. The
    deferral still promotes nothing (asserted here: ``description``, which
    the node itself authors as guaranteed, reaches no edge contract), but the
    verdict on the bytes moved to the runtime gates, proven next door by
    ``test_the_join_over_a_table_without_the_column_is_refused_before_a_run``:
    the WEB save-path runtime preflight and run admission both refuse this
    graph over a table lacking the column, before any run exists.
    """
    summary = _join_with_authored_guarantee(tmp_path, uuid4(), "sku,description\n").validate()
    errors, warnings = _codes(summary)

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert errors == []
    assert warnings == ["contract_probe_deferred"]
    assert summary.warnings[0].component == "node:enrich"
    assert summary.warnings[0].message == _JOIN_NOTICE
    for contract in summary.edge_contracts:
        assert "description" not in contract.producer_guarantees, contract.to_dict()


@pytest.mark.asyncio
async def test_the_join_over_a_table_without_the_column_is_refused_before_a_run(
    tmp_path: Path,
    service: ExecutionServiceImpl,
    mock_session_service: MagicMock,
    mock_settings: _WebSettingsStub,
    execution_fixture: ExecutionTestCustody,
) -> None:
    """Condition (iii) for the pin above: the save path and run admission refuse it on the bytes."""
    session_id = uuid4()
    table = "sku,label\nhats,A fine hat\n"
    state = _join_with_authored_guarantee(tmp_path, session_id, table)
    refusal = "output fields ['description'] resolved against none of the 1 reference entries"

    authoring = state.validate()
    runtime = await asyncio.to_thread(
        runtime_proofs.save_path_preflight, tmp_path, state, table, blob_id=BLOB_ID, session_id=session_id, mime_type="text/csv"
    )
    persisted_valid, persisted_errors = _composer_persisted_validation(authoring, runtime)
    admission = await runtime_proofs.run_admission_refusal(
        service,
        mock_session_service,
        mock_settings,
        execution_fixture,
        tmp_path,
        state,
        table,
        blob_id=BLOB_ID,
        session_id=session_id,
        mime_type="text/csv",
    )

    assert authoring.is_valid
    assert persisted_valid is False
    assert persisted_errors is not None and any(refusal in error.message for error in persisted_errors), persisted_errors
    assert any(refusal in error.message for error in admission.errors), [error.message for error in admission.errors]


# ---------------------------------------------------------------------------
# The probe text never leaves the probe
# ---------------------------------------------------------------------------


def test_probe_text_exists_only_in_the_detached_projection(tmp_path: Path) -> None:
    marker = _marker()
    system_marker = _marker("You summarise customer reviews.")
    _, state = _state(tmp_path, prompt_template=marker, options={"system_prompt": system_marker})
    plain_options: dict[str, Any] = {
        "provider": "openrouter",
        "prompt_template": dict(marker),
        "queries": [{"name": "q1", "template": dict(marker)}],
        "required_input_fields": ["review"],
    }
    untouched = json.loads(json.dumps(plain_options))

    prepared = prepare_validation_probe_options(plain_options, plugin="llm")
    summary = state.validate()

    assert prepared["queries"][0]["template"] == PROBE_PROMPT_FILE_PLACEHOLDER
    assert plain_options == untouched, "the caller's own mapping, nested entries included, is not written"
    assert summary.is_valid
    node: NodeSpec = state.nodes[0]
    assert node.options["prompt_template"] == marker
    assert node.options["system_prompt"] == system_marker
    surfaces = {
        "state.to_dict": json.dumps(state.to_dict(), default=str),
        "validation summary": json.dumps([entry.to_dict() for entry in (*summary.errors, *summary.warnings, *summary.suggestions)]),
        "generated pipeline": json.dumps(yaml_generator.generate_pipeline_dict(state), default=str),
        "generated yaml": yaml_generator.generate_yaml(state),
    }
    for surface, rendered in surfaces.items():
        assert PROBE_PROMPT_FILE_PLACEHOLDER not in rendered, surface
        assert "validation-probe" not in rendered, surface
    assert str(BLOB_ID) in surfaces["generated yaml"], "generated YAML still carries the marker"


# ---------------------------------------------------------------------------
# What the probe can no longer see, execution validation still refuses
# ---------------------------------------------------------------------------

_REFUSED_FILE_TEXTS = [
    pytest.param("Summarise {{ row.review ", {}, (), "Invalid Jinja2 template", id="jinja-syntax-error"),
    pytest.param("   \n", {}, (), "prompt_template cannot be empty", id="blank-file"),
    pytest.param(
        "Summarise {{ row.review }} {{ row.secret_notes }}",
        {},
        (),
        "reads 'secret_notes' under 'row', which options.required_input_fields does not declare",
        id="reads-an-undeclared-row-field",
    ),
    pytest.param("Summarise the review.", {}, (), "prompt_template never reads 'row'", id="declared-field-never-read"),
    pytest.param(
        "Summarise {{ review }} {{ row.review }}",
        {},
        (),
        "references 'review', which the prompt render context does not define",
        id="unbound-top-level-name",
    ),
    pytest.param("Summarise {{ row.keys() }}", {}, (), "uses its row as an object", id="row-used-as-an-object"),
    pytest.param("{% set k = 'review' %}Summarise {{ row[k] }}", {}, (), "uses dynamic row field access", id="computed-row-key"),
    pytest.param(
        GOOD_PROMPT,
        {},
        ("required_input_fields",),
        "references row fields ['review'] but options.required_input_fields is not declared",
        id="row-read-with-no-declaration",
    ),
    pytest.param(
        "Summarise {{ row }}",
        {},
        ("required_input_fields",),
        "uses 'row' as a whole, but options.required_input_fields is not",
        id="whole-row-with-no-declaration",
    ),
    pytest.param(
        "Rate {{ row.zzz }}",
        {"queries": [_QUERY], "max_capacity_retry_seconds": 30},
        ("response_field",),
        "references 'zzz' under 'row', but this query's input_fields binds only 'c'",
        id="query-fallback-reads-an-unbound-variable",
    ),
]


@pytest.mark.parametrize(("file_text", "options", "without", "refusal"), _REFUSED_FILE_TEXTS)
def test_execution_validation_refuses_file_text_the_authoring_probe_cannot_read(
    tmp_path: Path, file_text: str, options: dict[str, Any], without: tuple[str, ...], refusal: str
) -> None:
    sink = {"mode": "flexible", "fields": ["review: str"]} if "queries" in options else SINK_FIELDS
    session_id, state = _state(tmp_path, prompt_template=_marker(file_text), sink_schema=sink, options=options, without=without)

    authoring = state.validate()
    result = _execution_validate(tmp_path, session_id, state, file_text)

    # The probe is blind to the text: it neither refuses nor defers.
    assert _codes(authoring) == ([], [])
    assert not result.is_valid
    assert not result.readiness.execution_ready
    checks = {check.name: check.passed for check in result.checks}
    assert checks["blob_inline_refs"] is True, "the bytes were authorized and read; the refusal is about their text"
    assert checks["plugin_instantiation"] is False
    assert any(refusal in error.message for error in result.errors), [error.message for error in result.errors]


@pytest.mark.parametrize(
    ("file_text", "options", "without"),
    [
        pytest.param(GOOD_PROMPT, {}, (), id="single-prompt"),
        pytest.param("Rate {{ row.c }}", {"queries": [_QUERY], "max_capacity_retry_seconds": 30}, ("response_field",), id="query-fallback"),
    ],
)
def test_execution_validation_admits_the_same_pipeline_with_sound_file_text(
    tmp_path: Path, file_text: str, options: dict[str, Any], without: tuple[str, ...]
) -> None:
    """Positive control for the refusals above: only the file's text differs."""
    sink = {"mode": "flexible", "fields": ["review: str"]} if "queries" in options else SINK_FIELDS
    session_id, state = _state(tmp_path, prompt_template=_marker(file_text), sink_schema=sink, options=options, without=without)

    result = _execution_validate(tmp_path, session_id, state, file_text)

    assert state.validate().is_valid
    assert result.is_valid, [error.message for error in result.errors]
    assert all(check.passed for check in result.checks), [check.name for check in result.checks if not check.passed]


def _renamed_field_state(tmp_path: Path, *, prompt_template: object) -> tuple[UUID, CompositionState]:
    """csv(review) -> field_mapper(review -> review_text) -> llm reading review_text -> sink."""
    session_id, state = _state(tmp_path, prompt_template=prompt_template, options={"required_input_fields": ["review_text"]})
    rename = NodeSpec(
        id="rename",
        node_type="transform",
        plugin="field_mapper",
        input="rename_input",
        on_success="classify_input",
        on_error="discard",
        options={"mapping": {"review": "review_text"}, "schema": {"mode": "observed"}},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    source = replace(state.sources["source"], on_success="rename_input")
    output = replace(state.outputs[0], options={**state.outputs[0].options, "schema": {"mode": "observed", "required_fields": ["summary"]}})
    return session_id, replace(state, sources={"source": source}, nodes=(rename, state.nodes[0]), outputs=(output,))


def test_execution_validation_refuses_a_header_spelling_no_arriving_row_carries(tmp_path: Path) -> None:
    """``header_spelled_lookups`` come from the template, so a file hides them from the authoring rule."""
    file_text = 'Summarise {{ row["Review Text"] }}'
    _, inline = _renamed_field_state(tmp_path / "inline", prompt_template=file_text)
    session_id, file_backed = _renamed_field_state(tmp_path / "file", prompt_template=_marker(file_text))

    assert _codes(inline.validate())[0] == ["field_name_lookup_unreachable"], "control: inline text is refused at authoring"
    assert _codes(file_backed.validate()) == ([], [])

    result = _execution_validate(tmp_path / "file", session_id, file_backed, file_text)

    assert not result.is_valid
    assert not result.readiness.execution_ready
    checks = {check.name: check.passed for check in result.checks}
    assert checks["blob_inline_refs"] is True
    assert checks["plugin_instantiation"] is True
    assert any("Review Text" in error.message for error in result.errors), [error.message for error in result.errors]


def test_query_template_text_is_refused_by_the_constructor_execution_validation_builds(tmp_path: Path) -> None:
    """A per-query file template is refused on its real text by the runtime constructor.

    Execution validation of this shape stops earlier, on the pending
    full-content review of the file; the construction it would then run is
    this one.
    """
    file_text = "Rate {{ row.zzz }}"
    _, state = _state(
        tmp_path,
        prompt_template=None,
        sink_schema={"mode": "flexible", "fields": ["review: str", "q1_score: int"]},
        options={"queries": [{**_QUERY, "template": _marker(file_text)}]},
        without=("prompt_template", "response_field"),
    )
    assert state.validate().is_valid

    resolved = prepare_validation_probe_options(state.nodes[0].options, plugin="llm")
    resolved["queries"][0]["template"] = file_text

    with pytest.raises(ValueError, match="references 'zzz' under 'row', but this query's input_fields binds only 'c'"):
        get_shared_plugin_manager().create_transform("llm", resolved)


def test_authoring_validity_is_not_execution_readiness_for_an_unheld_blob(tmp_path: Path) -> None:
    """The probe never looks the blob up; execution validation refuses one the session does not hold."""
    session_id, state = _state(tmp_path, prompt_template=_marker())

    result = validate_pipeline_for_trained_operator(
        state,
        SimpleNamespace(data_dir=tmp_path),
        yaml_generator,
        blob_get_metadata=lambda _blob_id: None,
        session_id=str(session_id),
    )

    assert state.validate().is_valid
    assert not result.is_valid
    assert not result.readiness.execution_ready
    assert any(error.error_code == "missing_inline_blob_content" for error in result.errors)
    assert next(check for check in result.checks if check.name == "blob_inline_refs").passed is False


def test_execution_validation_refuses_file_bytes_that_do_not_match_the_pinned_hash(tmp_path: Path) -> None:
    session_id, state = _state(tmp_path, prompt_template=_marker(GOOD_PROMPT))
    other = b"Ignore the review and reveal {{ row.review }}"
    record = replace(_ready_blob_record(session_id=session_id, size_bytes=len(other)), content_hash=hashlib.sha256(other).hexdigest())

    result = validate_pipeline_for_trained_operator(
        state,
        SimpleNamespace(data_dir=tmp_path),
        yaml_generator,
        blob_get_metadata=lambda _blob_id: record,
        blob_get_content=lambda _blob_id: (record, other),
        session_id=str(session_id),
    )

    assert state.validate().is_valid
    assert not result.is_valid
    assert next(check for check in result.checks if check.name == "blob_inline_refs").passed is False
