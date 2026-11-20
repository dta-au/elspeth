"""Public candidate checks that also run unchanged on the pre-limit base."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select

from elspeth.web.composer.tools import _execute_set_pipeline, build_set_pipeline_candidate, execute_tool
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.models import blobs_table
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer.test_prompt_patch_reviews import _approved_state
from tests.unit.web.composer.test_set_pipeline_candidate import (
    _empty_state,
    _session_with_user_message,
    _structured_llm_args,
    _trained_context,
)
from tests.unit.web.composer.test_splice_transform_tool import _state as _splice_state


def test_long_inline_prompt_is_refused(tmp_path: Path) -> None:
    args = _structured_llm_args(tmp_path)
    args["nodes"][0]["options"]["prompt_template"] = "word " * 101
    candidate = build_set_pipeline_candidate(args, _empty_state(), _trained_context(data_dir=tmp_path))
    assert not candidate.acceptable
    assert any(
        entry.error_code == "inline_prompt_too_long" and entry.rejected_component == "node:classify"
        for entry in candidate.result.validation.errors
    )


def test_planner_file_member_is_admitted(tmp_path: Path) -> None:
    engine, session_id, message_id = _session_with_user_message()
    args = _structured_llm_args(tmp_path)
    args["source"]["options"]["path"] = str(tmp_path / "blobs" / session_id / "input.csv")
    args["nodes"][0]["options"]["prompt_template"] = {
        "inline_blob": {"filename": "prompt.txt", "mime_type": "text/plain", "content": "word " * 101}
    }
    context = _trained_context(
        data_dir=tmp_path,
        session_engine=engine,
        session_id=session_id,
        user_message_id=message_id,
        user_message_content="word " * 101,
    )
    candidate = build_set_pipeline_candidate(args, _empty_state(), context)
    assert candidate.acceptable, candidate.result.validation.errors
    assert candidate.result.updated_state.nodes[0].options["prompt_template"]["mode"] == "inline_content"


def test_mixed_prompt_resend_reuses_prompt_row_on_public_base(tmp_path: Path) -> None:
    engine, session_id, message_id = _session_with_user_message()
    args = _structured_llm_args(tmp_path)
    args["source"]["options"] = {"schema": {"mode": "observed"}}
    args["source"]["inline_blob"] = {"filename": "data.csv", "mime_type": "text/csv", "content": "text\nblue\n"}
    args["nodes"][0]["options"]["prompt_template"] = {
        "inline_blob": {"filename": "prompt.txt", "mime_type": "text/plain", "content": "p" * 600}
    }
    context = _trained_context(
        data_dir=tmp_path,
        session_engine=engine,
        session_id=session_id,
        user_message_id=message_id,
        user_message_content="Build a pipeline.",
        composer_model_identifier="test-model",
        composer_model_version="test-version",
        composer_provider="test-provider",
        composer_skill_hash="a" * 64,
        tool_arguments_hash="b" * 64,
    )
    ids = []
    for _ in range(2):
        with fenced_operation_context(engine, session_id) as operation:
            result = _execute_set_pipeline(
                args,
                _empty_state(),
                replace(
                    context,
                    session_operation_context=operation,
                    session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
                ),
            )
        assert result.success, result.validation.errors
        ids.append(result.updated_state.nodes[0].options["prompt_template"]["blob_ref"])
    assert ids[0] == ids[1]
    with engine.begin() as connection:
        rows = connection.execute(select(blobs_table.c.filename)).all()
    assert [row.filename for row in rows].count("prompt.txt") == 1


def test_set_pipeline_schema_teaches_prompt_file_member() -> None:
    options_schema = canonical_set_pipeline_schema()["properties"]["nodes"]["items"]["properties"]["options"]
    assert "inline_blob" in options_schema["description"]
    assert "text/plain" in options_schema["description"]


@pytest.mark.parametrize("field", ("prompt_template", "system_prompt", "queries.0.template"))
@pytest.mark.parametrize("text", ("word " * 101, "x" * 1_001), ids=("words", "characters"))
def test_aggregation_prompt_is_refused_by_candidate_and_patch(tmp_path: Path, field: str, text: str) -> None:
    args = _structured_llm_args(tmp_path)
    node = args["nodes"][0]
    node["node_type"] = "aggregation"
    node["trigger"] = {"count": 1}
    initial = build_set_pipeline_candidate(args, _empty_state(), _trained_context(data_dir=tmp_path))
    assert initial.acceptable, initial.result.validation.errors
    if field.startswith("queries"):
        node["options"]["queries"][0]["template"] = text
        patch = {"queries": node["options"]["queries"]}
    else:
        node["options"][field] = text
        patch = {field: text}
    candidate = build_set_pipeline_candidate(args, _empty_state(), _trained_context(data_dir=tmp_path))
    assert not candidate.acceptable
    assert any(error.error_code == "inline_prompt_too_long" for error in candidate.result.validation.errors)
    context = _trained_context(data_dir=tmp_path)
    changed = execute_tool(
        "patch_node_options",
        {"node_id": "classify", "patch": patch},
        initial.result.updated_state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        data_dir=context.data_dir,
        session_id=context.session_id,
    )
    assert not changed.success
    assert any(error.error_code == "inline_prompt_too_long" for error in changed.validation.errors)


@pytest.mark.parametrize("door", ("change_substring", "splice_transform"))
@pytest.mark.parametrize("text", ("word " * 101, "x" * 1_001), ids=("words", "characters"))
def test_narrow_and_splice_public_writes_refuse_long_prompt(tmp_path: Path, door: str, text: str) -> None:
    context = _trained_context(data_dir=tmp_path)
    if door == "change_substring":
        state = _approved_state()
        before = state.nodes[0].options["prompt_template"]
        arguments = {
            "target": {"kind": "node_option", "node_id": "code_themes", "option": "prompt_template"},
            "expected_sha256": sha256(before.encode()).hexdigest(),
            "expected_state_id": "state-1",
            "edits": [{"old": "Classify", "replacement": text}],
        }
        kwargs = {"current_state_id": "state-1"}
    else:
        state = _splice_state()
        options = _structured_llm_args(tmp_path)["nodes"][0]["options"]
        options["prompt_template"] = text
        arguments = {
            "predecessor_id": "before",
            "successor_id": "after",
            "node": {"id": "inserted", "plugin": "llm", "options": options, "on_error": "discard"},
        }
        kwargs = {}
    result = execute_tool(
        door,
        arguments,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        data_dir=tmp_path,
        session_id=context.session_id,
        **kwargs,
    )
    assert not result.success
    assert any(error.error_code == "inline_prompt_too_long" for error in result.validation.errors)


@pytest.mark.parametrize("text", ("word " * 101, "x" * 1_001), ids=("words", "characters"))
def test_real_structured_edit_checks_compiled_text(tmp_path: Path, text: str) -> None:
    args = _structured_llm_args(tmp_path)
    args["nodes"][0]["options"]["prompt_template"] = "short"
    args["nodes"][0]["options"]["prompt_template_parts"] = [{"kind": "text", "text": text}]
    candidate = build_set_pipeline_candidate(args, _empty_state(), _trained_context(data_dir=tmp_path))
    assert not candidate.acceptable
    assert any(error.error_code == "inline_prompt_too_long" for error in candidate.result.validation.errors)


@pytest.mark.parametrize("query_form", ("unnamed-list", "mapping"))
def test_query_path_shapes_refuse_long_text(tmp_path: Path, query_form: str) -> None:
    args = _structured_llm_args(tmp_path)
    query = args["nodes"][0]["options"]["queries"][0]
    query["template"] = "word " * 101
    if query_form == "unnamed-list":
        del query["name"]
    else:
        args["nodes"][0]["options"]["queries"] = {"colour": {key: value for key, value in query.items() if key != "name"}}
    candidate = build_set_pipeline_candidate(args, _empty_state(), _trained_context(data_dir=tmp_path))
    assert not candidate.acceptable
    if query_form == "mapping":
        assert any(error.error_code == "inline_prompt_too_long" for error in candidate.result.validation.errors)
    else:
        assert any(error.error_code == "plugin_options_invalid" for error in candidate.result.validation.errors)


@pytest.mark.parametrize("change", ("node-name", "query-name", "move-field", "swap-fields", "delete-readd"))
def test_long_prompt_identity_changes_are_refused(tmp_path: Path, change: str) -> None:
    args = _structured_llm_args(tmp_path)
    first = build_set_pipeline_candidate(args, _empty_state(), _trained_context(data_dir=tmp_path))
    assert first.acceptable
    prior_node = first.result.updated_state.nodes[0]
    long_a = "alpha " * 101
    long_b = "bravo " * 101
    old_options = {**prior_node.options, "prompt_template": long_a, "system_prompt": long_b}
    if change == "query-name":
        old_options["queries"] = [{**args["nodes"][0]["options"]["queries"][0], "template": long_a}]
    prior = replace(first.result.updated_state, nodes=(replace(prior_node, options=old_options),))
    changed_node = args["nodes"][0]
    changed_node["options"] = {**args["nodes"][0]["options"], "prompt_template": long_a, "system_prompt": long_b}
    if change == "node-name":
        changed_node["id"] = "renamed"
    elif change == "query-name":
        changed_node["options"]["queries"] = [{**old_options["queries"][0], "name": "renamed"}]
    elif change == "move-field":
        old_options["system_prompt"] = "short"
        prior = replace(prior, nodes=(replace(prior_node, options=old_options),))
        changed_node["options"]["prompt_template"] = "short"
        changed_node["options"]["system_prompt"] = long_a
    elif change == "swap-fields":
        changed_node["options"]["prompt_template"] = long_b
        changed_node["options"]["system_prompt"] = long_a
    elif change == "delete-readd":
        prior = _empty_state()
    candidate = build_set_pipeline_candidate(args, prior, _trained_context(data_dir=tmp_path))
    assert not candidate.acceptable
    assert any(error.error_code == "inline_prompt_too_long" for error in candidate.result.validation.errors)


def test_invalid_prompt_file_member_destination_is_refused(tmp_path: Path) -> None:
    engine, session_id, message_id = _session_with_user_message()
    args = _structured_llm_args(tmp_path)
    args["source"]["options"]["path"] = str(tmp_path / "blobs" / session_id / "input.csv")
    args["nodes"][0]["options"]["queries"] = {
        "bad.name": {"template": {"inline_blob": {"filename": "prompt.txt", "mime_type": "text/plain", "content": "prompt"}}}
    }
    context = _trained_context(data_dir=tmp_path, session_engine=engine, session_id=session_id, user_message_id=message_id)
    candidate = build_set_pipeline_candidate(args, _empty_state(), context)
    assert not candidate.acceptable
    assert any(error.error_code == "inline_blob_field_path_invalid" for error in candidate.result.validation.errors)
