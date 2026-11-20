"""Write-time prompt limits and the planner's inline file member."""

from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from elspeth.contracts.enums import CreationModality
from elspeth.core.blobs_inline import BLOB_INLINE_PER_REF_BYTE_CAP
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.inline_prompt_limit import inline_prompt_counts, new_long_inline_prompt
from elspeth.web.composer.pipeline_custody import (
    finalize_pipeline_custody,
    inline_custody_audit_projection,
    inline_custody_manifest_redaction_input,
    prepare_pipeline_custody,
)
from elspeth.web.composer.pipeline_planner import PlannerCustodyConfig
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import SourceSpec
from elspeth.web.composer.tool_batch import _finalize_complete_set_pipeline_candidate
from elspeth.web.composer.tools import _execute_set_pipeline, build_set_pipeline_candidate, execute_tool
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.sessions.models import blobs_table
from elspeth.web.sessions.prompt_file_review import resolve_prompt_review_snapshot
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer import _helpers as composer_helpers
from tests.unit.web.composer._helpers import _make_settings
from tests.unit.web.composer.conftest import _fake_llm_response, _FakeComposeLLM
from tests.unit.web.composer.test_pipeline_planner import _plan, _response, _ScriptedCompletion, _session_context
from tests.unit.web.composer.test_set_pipeline_candidate import (
    _empty_state,
    _linear_args,
    _session_with_user_message,
    _structured_llm_args,
    _trained_context,
)

_stub_advisor_end_gate_clean = composer_helpers._stub_advisor_end_gate_clean


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("word " * 100, (100, 499)),
        ("word " * 101, (101, 504)),
        ("x" * 1_000, (1, 1_000)),
        ("x" * 1_001, (1, 1_001)),
        ("{{ row.text }} " + "word " * 100, (100, 499)),
    ],
)
def test_prompt_counts(text: str, expected: tuple[int, int]) -> None:
    assert inline_prompt_counts(text) == expected


@pytest.mark.parametrize("field", ["prompt_template", "system_prompt", "queries.0.template"])
@pytest.mark.parametrize("long_text", ["word " * 101, "x" * 1_001])
def test_candidate_refuses_new_long_prompt_at_each_field(tmp_path: Path, field: str, long_text: str) -> None:
    args = _structured_llm_args(tmp_path)
    options = args["nodes"][0]["options"]
    if field.startswith("queries"):
        options["queries"][0]["template"] = long_text
    else:
        options[field] = long_text
    candidate = build_set_pipeline_candidate(args, _empty_state(), _trained_context(data_dir=tmp_path))
    assert not candidate.acceptable
    assert candidate.result.updated_state == _empty_state()
    assert any(
        entry.error_code == "inline_prompt_too_long" and entry.rejected_component == "node:classify"
        for entry in candidate.result.validation.errors
    )


def test_unchanged_long_prompt_is_not_new(tmp_path: Path) -> None:
    first = build_set_pipeline_candidate(_structured_llm_args(tmp_path), _empty_state(), _trained_context(data_dir=tmp_path))
    assert first.result.success, first.result.validation.errors
    short_node = first.result.updated_state.nodes[0]
    prior = replace(
        first.result.updated_state,
        nodes=(replace(short_node, options={**short_node.options, "prompt_template": "word " * 101}),),
    )
    assert new_long_inline_prompt(prior, prior) is None
    repeated = _structured_llm_args(tmp_path)
    repeated["nodes"][0]["options"]["prompt_template"] = "word " * 101
    resent = build_set_pipeline_candidate(repeated, prior, _trained_context(data_dir=tmp_path))
    assert resent.result.success
    assert not any(entry.error_code == "inline_prompt_too_long" for entry in resent.result.validation.errors)
    changed = _structured_llm_args(tmp_path)
    changed["nodes"][0]["options"]["prompt_template"] = "word " * 100 + "different"
    edited = build_set_pipeline_candidate(changed, prior, _trained_context(data_dir=tmp_path))
    assert not edited.acceptable
    assert any(entry.error_code == "inline_prompt_too_long" for entry in edited.result.validation.errors)


def test_file_marker_and_llm_source_are_unchecked(tmp_path: Path) -> None:
    first = build_set_pipeline_candidate(_structured_llm_args(tmp_path), _empty_state(), _trained_context(data_dir=tmp_path))
    assert first.acceptable
    state = first.result.updated_state
    node = state.nodes[0]
    marker = {"blob_ref": "00000000-0000-4000-8000-000000000003", "mode": "inline_content", "sha256": "a" * 64}
    proposed = replace(state, nodes=(replace(node, options={**node.options, "prompt_template": marker}),))
    assert new_long_inline_prompt(state, proposed) is None
    source = SourceSpec(plugin="llm", on_success="rows", options={"prompt_template": "word " * 101}, on_validation_failure="discard")
    with_source = replace(state, sources={"source": source})
    assert new_long_inline_prompt(state, with_source) is None


def test_structured_compiled_text_is_counted(tmp_path: Path) -> None:
    first = build_set_pipeline_candidate(_structured_llm_args(tmp_path), _empty_state(), _trained_context(data_dir=tmp_path))
    assert first.acceptable
    state = first.result.updated_state
    node = state.nodes[0]
    options = {**node.options, "prompt_template_parts": ({"kind": "text", "text": "short"},), "prompt_template": "word " * 101}
    proposed = replace(state, nodes=(replace(node, options=options),))
    assert new_long_inline_prompt(state, proposed) == ("classify", "prompt_template", 101, 504)


def test_unnamed_list_query_uses_its_index_in_comparison(tmp_path: Path) -> None:
    first = build_set_pipeline_candidate(_structured_llm_args(tmp_path), _empty_state(), _trained_context(data_dir=tmp_path))
    assert first.acceptable
    state = first.result.updated_state
    node = state.nodes[0]
    query = dict(node.options["queries"][0])
    query.pop("name")
    query["template"] = "word " * 101
    proposed = replace(state, nodes=(replace(node, options={**node.options, "queries": [query]}),))
    assert new_long_inline_prompt(state, proposed) == ("classify", "queries.0.template", 101, 504)


@pytest.mark.parametrize("text", ["word " * 100, "x" * 1_000])
def test_boundary_prompt_is_accepted(tmp_path: Path, text: str) -> None:
    args = _structured_llm_args(tmp_path)
    args["nodes"][0]["options"]["prompt_template"] = text
    candidate = build_set_pipeline_candidate(args, _empty_state(), _trained_context(data_dir=tmp_path))
    assert candidate.acceptable, candidate.result.validation.errors


@pytest.mark.parametrize("tool_name", ["patch_node_options", "upsert_node"])
def test_dispatch_refuses_long_prompt(tmp_path: Path, tool_name: str) -> None:
    args = _structured_llm_args(tmp_path)
    initial = build_set_pipeline_candidate(args, _empty_state(), _trained_context(data_dir=tmp_path))
    assert initial.acceptable
    state = initial.result.updated_state
    context = _trained_context(data_dir=tmp_path)
    if tool_name == "patch_node_options":
        arguments = {"node_id": "classify", "patch": {"system_prompt": "word " * 101}}
    else:
        arguments = {**args["nodes"][0], "options": {**args["nodes"][0]["options"], "system_prompt": "word " * 101}}
    result = execute_tool(
        tool_name,
        arguments,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        data_dir=context.data_dir,
        session_id=context.session_id,
    )
    assert not result.success
    assert result.updated_state is state
    assert any(
        entry.error_code == "inline_prompt_too_long" and entry.rejected_component == "node:classify" for entry in result.validation.errors
    )


def test_inline_blob_member_builds_a_candidate(tmp_path: Path) -> None:
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
    assert len(candidate.prepared_prompt_blobs) == 1
    assert candidate.prepared_prompt_blobs[0][1].creation_modality is CreationModality.VERBATIM


def test_set_pipeline_directly_creates_and_binds_prompt_file(tmp_path: Path) -> None:
    engine, session_id, message_id = _session_with_user_message()
    args = _structured_llm_args(tmp_path)
    args["source"]["options"]["path"] = str(tmp_path / "blobs" / session_id / "input.csv")
    content = "file-backed prompt " * 60
    args["nodes"][0]["options"]["prompt_template"] = {
        "inline_blob": {"filename": "direct-prompt.txt", "mime_type": "text/plain", "content": content}
    }
    context = _trained_context(
        data_dir=tmp_path,
        session_engine=engine,
        session_id=session_id,
        user_message_id=message_id,
        user_message_content=content,
    )
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
    assert result.success and result.validation.is_valid
    marker = result.updated_state.nodes[0].options["prompt_template"]
    assert marker["mode"] == "inline_content"
    with engine.begin() as conn:
        row = conn.execute(select(blobs_table).where(blobs_table.c.id == marker["blob_ref"])).mappings().one()
    assert row["creation_modality"] == "verbatim"
    assert Path(row["storage_path"]).read_text() == content


@pytest.mark.parametrize("first_quota", (1_200, 900), ids=("repeat", "quota-retry"))
def test_direct_multi_file_custody_reuses_deterministic_ids(tmp_path: Path, first_quota: int) -> None:
    engine, session_id, message_id = _session_with_user_message()
    args = _structured_llm_args(tmp_path)
    args["source"]["options"]["path"] = str(tmp_path / "blobs" / session_id / "input.csv")
    for field in ("prompt_template", "system_prompt"):
        args["nodes"][0]["options"][field] = {
            "inline_blob": {"filename": f"{field}.txt", "mime_type": "text/plain", "content": field[0] * 600}
        }
    context = _trained_context(
        data_dir=tmp_path,
        session_engine=engine,
        session_id=session_id,
        user_message_id=message_id,
        user_message_content="Please prepare the pipeline.",
        composer_model_identifier="test-model",
        composer_model_version="test-version",
        composer_provider="test-provider",
        composer_skill_hash="a" * 64,
        tool_arguments_hash="b" * 64,
    )
    results = []
    for quota in (first_quota, 1_200):
        with fenced_operation_context(engine, session_id) as operation:
            results.append(
                _execute_set_pipeline(
                    args,
                    _empty_state(),
                    replace(
                        context,
                        max_blob_storage_per_session_bytes=quota,
                        session_operation_context=operation,
                        session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
                    ),
                )
            )
    assert results[0].success is (first_quota == 1_200)
    assert results[1].success, results[1].validation.errors
    if first_quota == 1_200:
        assert [result.updated_state.nodes[0].options["prompt_template"]["blob_ref"] for result in results] == [
            results[0].updated_state.nodes[0].options["prompt_template"]["blob_ref"]
        ] * 2
    else:
        assert results[0].updated_state == _empty_state()
    markers = results[1].updated_state.nodes[0].options
    assert markers["prompt_template"]["blob_ref"] != markers["system_prompt"]["blob_ref"]
    with engine.begin() as conn:
        rows = conn.execute(select(blobs_table.c.id, blobs_table.c.filename)).all()
    assert len(rows) == 2
    assert {row.filename for row in rows} == {"prompt_template.txt", "system_prompt.txt"}
    assert {str(row.id) for row in rows} == {markers["prompt_template"]["blob_ref"], markers["system_prompt"]["blob_ref"]}


def test_direct_changed_call_resends_same_prompt_file_with_new_identity(tmp_path: Path) -> None:
    engine, session_id, message_id = _session_with_user_message()
    args = _structured_llm_args(tmp_path)
    args["source"]["options"]["path"] = str(tmp_path / "blobs" / session_id / "input.csv")
    args["nodes"][0]["options"]["prompt_template"] = {
        "inline_blob": {"filename": "prompt.txt", "mime_type": "text/plain", "content": "generated prompt " * 60}
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
    for current in (args, {**args, "metadata": {"name": "renamed"}}, args):
        with fenced_operation_context(engine, session_id) as operation:
            result = _execute_set_pipeline(
                current,
                _empty_state(),
                replace(
                    context,
                    session_operation_context=operation,
                    session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
                ),
            )
        assert result.success, result.validation.errors
        ids.append(result.updated_state.nodes[0].options["prompt_template"]["blob_ref"])
    assert ids[0] != ids[1]
    assert ids[0] == ids[2]
    with engine.begin() as connection:
        assert connection.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 2


@pytest.mark.parametrize("size", (BLOB_INLINE_PER_REF_BYTE_CAP, BLOB_INLINE_PER_REF_BYTE_CAP + 1))
def test_source_multibyte_csv_keeps_existing_byte_boundary(tmp_path: Path, size: int) -> None:
    engine, session_id, message_id = _session_with_user_message()
    content = "name\n" + "é\n" * ((size - 6) // 3) + "a" * ((size - 6) % 3) + "\n"
    assert len(content.encode()) == size
    args = _linear_args(tmp_path)
    args["source"]["options"] = {"schema": {"mode": "observed"}}
    args["source"]["inline_blob"] = {"filename": "data.csv", "mime_type": "text/csv", "content": content}
    context = _trained_context(
        data_dir=tmp_path,
        session_engine=engine,
        session_id=session_id,
        user_message_id=message_id,
        user_message_content=content,
        max_blob_storage_per_session_bytes=2_000_000,
    )
    assert build_set_pipeline_candidate(args, _empty_state(), context).acceptable
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
    with engine.begin() as connection:
        assert connection.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 1


def test_complete_source_inline_blob_returns_original_envelope(tmp_path: Path) -> None:
    engine, session_id, message_id = _session_with_user_message()
    args = _linear_args(tmp_path)
    args["source"]["options"] = {"schema": {"mode": "observed"}}
    args["source"]["inline_blob"] = {"filename": "data.csv", "mime_type": "text/csv", "content": "name,score\nada,42\n"}
    context = _trained_context(
        data_dir=tmp_path,
        session_engine=engine,
        session_id=session_id,
        user_message_id=message_id,
        user_message_content="name,score\nada,42\n",
    )
    with fenced_operation_context(engine, session_id) as operation:
        result = _execute_set_pipeline(
            args,
            _empty_state(),
            replace(context, session_operation_context=operation, session_operation_authority=SQLiteLocalSessionOperationAuthority(engine)),
        )
    assert result.success and result.validation.is_valid
    assert result.data is not None and set(result.data) == {"inline_blob"}
    assert result.data["inline_blob"]["originated_in"] == "this_tool_call"
    assert result.updated_state.sources["source"].options["blob_ref"] == result.data["inline_blob"]["blob_id"]


def test_source_and_prompt_member_keep_source_envelope(tmp_path: Path) -> None:
    engine, session_id, message_id = _session_with_user_message()
    args = _structured_llm_args(tmp_path)
    args["source"]["options"] = {"schema": {"mode": "observed"}}
    args["source"]["inline_blob"] = {"filename": "data.csv", "mime_type": "text/csv", "content": "text\nblue\n"}
    args["nodes"][0]["options"]["prompt_template"] = {
        "inline_blob": {"filename": "prompt.txt", "mime_type": "text/plain", "content": "classify the row"}
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
    with fenced_operation_context(engine, session_id) as operation:
        result = _execute_set_pipeline(
            args,
            _empty_state(),
            replace(context, session_operation_context=operation, session_operation_authority=SQLiteLocalSessionOperationAuthority(engine)),
        )
    assert result.success and result.validation.is_valid
    assert result.data is not None and set(result.data) == {"inline_blob"}
    source_id = result.data["inline_blob"]["blob_id"]
    prompt_id = result.updated_state.nodes[0].options["prompt_template"]["blob_ref"]
    assert source_id != prompt_id
    assert result.updated_state.sources["source"].options["blob_ref"] == source_id
    with engine.begin() as connection:
        assert connection.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 2


def _mixed_prompt_call(tmp_path: Path, *, two_prompts: bool = False):
    engine, session_id, message_id = _session_with_user_message()
    args = _structured_llm_args(tmp_path)
    args["source"]["options"] = {"schema": {"mode": "observed"}}
    args["source"]["inline_blob"] = {"filename": "data.csv", "mime_type": "text/csv", "content": "text\nblue\n"}
    fields = ("prompt_template", "system_prompt") if two_prompts else ("prompt_template",)
    for field in fields:
        args["nodes"][0]["options"][field] = {
            "inline_blob": {"filename": f"{field}.txt", "mime_type": "text/plain", "content": field[0] * 600}
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
    return engine, session_id, args, context


def _run_mixed_prompt(engine, session_id: str, args: dict, context, *, quota: int):
    with fenced_operation_context(engine, session_id) as operation:
        return _execute_set_pipeline(
            args,
            _empty_state(),
            replace(
                context,
                max_blob_storage_per_session_bytes=quota,
                session_operation_context=operation,
                session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
            ),
        )


def test_mixed_exact_resend_three_times_reuses_prompt_bytes(tmp_path: Path) -> None:
    engine, session_id, args, context = _mixed_prompt_call(tmp_path)
    prompt_ids = []
    source_ids = []
    for _ in range(3):
        result = _run_mixed_prompt(engine, session_id, args, context, quota=5_000)
        assert result.success, result.validation.errors
        prompt_ids.append(result.updated_state.nodes[0].options["prompt_template"]["blob_ref"])
        assert result.data is not None
        source_ids.append(result.data["inline_blob"]["blob_id"])
    assert len(set(prompt_ids)) == 1
    assert len(set(source_ids)) == 3  # Source creation retains a490 UUID4 behavior.
    with engine.begin() as connection:
        rows = connection.execute(select(blobs_table.c.id, blobs_table.c.filename, blobs_table.c.size_bytes)).all()
    assert len(rows) == 4
    assert sum(row.size_bytes for row in rows if row.filename == "prompt_template.txt") == 600
    assert {str(row.id) for row in rows if row.filename == "prompt_template.txt"} == set(prompt_ids)


def test_mixed_partial_quota_retry_reuses_first_prompt(tmp_path: Path) -> None:
    engine, session_id, args, context = _mixed_prompt_call(tmp_path, two_prompts=True)
    first = _run_mixed_prompt(engine, session_id, args, context, quota=900)
    assert not first.success
    assert first.updated_state == _empty_state()
    with engine.begin() as connection:
        initial = connection.execute(select(blobs_table.c.id, blobs_table.c.filename, blobs_table.c.size_bytes)).all()
    assert {(row.filename, row.size_bytes) for row in initial} == {("data.csv", 10), ("prompt_template.txt", 600)}
    first_prompt_id = str(next(row.id for row in initial if row.filename == "prompt_template.txt"))
    # The retry adds a second 10-byte source and the missing 600-byte prompt:
    # 10 + 600 + 10 + 600 = 1,220 bytes, so quota 1,220 must suffice.
    second = _run_mixed_prompt(engine, session_id, args, context, quota=1_220)
    assert second.success, second.validation.errors
    markers = second.updated_state.nodes[0].options
    assert markers["prompt_template"]["blob_ref"] == first_prompt_id
    assert markers["system_prompt"]["blob_ref"] != first_prompt_id
    with engine.begin() as connection:
        rows = connection.execute(select(blobs_table.c.id, blobs_table.c.filename, blobs_table.c.size_bytes)).all()
    assert len(rows) == 4
    assert sum(row.size_bytes for row in rows) == 1_220
    assert [row.filename for row in rows].count("prompt_template.txt") == 1


def test_mixed_changed_prompt_or_source_gets_new_prompt_identity(tmp_path: Path) -> None:
    engine, session_id, args, context = _mixed_prompt_call(tmp_path)
    changed_prompt = {**args, "nodes": [{**args["nodes"][0], "options": {**args["nodes"][0]["options"]}}]}
    changed_prompt["nodes"][0]["options"]["prompt_template"] = {
        "inline_blob": {"filename": "prompt_template.txt", "mime_type": "text/plain", "content": "q" * 600}
    }
    changed_source = {**args, "source": {**args["source"], "inline_blob": {**args["source"]["inline_blob"], "content": "text\nred\n"}}}
    ids = []
    for current in (args, changed_prompt, changed_source, args):
        result = _run_mixed_prompt(engine, session_id, current, context, quota=5_000)
        assert result.success, result.validation.errors
        ids.append(result.updated_state.nodes[0].options["prompt_template"]["blob_ref"])
    assert len(set(ids[:3])) == 3
    assert ids[3] == ids[0]
    with engine.begin() as connection:
        rows = connection.execute(select(blobs_table.c.id, blobs_table.c.filename)).all()
    assert len([row for row in rows if row.filename == "prompt_template.txt"]) == 3
    assert len([row for row in rows if row.filename == "data.csv"]) == 4


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("filename", "other.csv"),
        ("mime_type", "text/plain"),
        ("content", "text\nred\n"),
        ("description", "Changed source description"),
    ),
)
def test_mixed_source_inline_field_changes_prompt_identity_and_creating_hash(tmp_path: Path, field: str, replacement: str) -> None:
    engine, session_id, args, context = _mixed_prompt_call(tmp_path)
    args["source"]["inline_blob"]["description"] = "Baseline source description"
    changed = deepcopy(args)
    changed["source"]["inline_blob"][field] = replacement
    prompt_ids = []
    creating_hashes = []
    for call in (args, changed, args):
        result = _run_mixed_prompt(engine, session_id, call, context, quota=5_000)
        assert result.success, result.validation.errors
        prompt_id = result.updated_state.nodes[0].options["prompt_template"]["blob_ref"]
        prompt_ids.append(prompt_id)
        with engine.begin() as connection:
            creating_hashes.append(
                connection.execute(select(blobs_table.c.creating_arguments_hash).where(blobs_table.c.id == prompt_id)).scalar_one()
            )
    assert prompt_ids[1] != prompt_ids[0]
    assert creating_hashes[1] != creating_hashes[0]
    assert prompt_ids[2] == prompt_ids[0]
    assert creating_hashes[2] == creating_hashes[0]


def test_multiple_prompt_members_settle_and_bind(tmp_path: Path) -> None:
    engine, session_id, message_id = _session_with_user_message()
    args = _structured_llm_args(tmp_path)
    args["source"]["options"]["path"] = str(tmp_path / "blobs" / session_id / "input.csv")
    contents = {"prompt_template": "first prompt " * 60, "system_prompt": "second prompt " * 60}
    for field, content in contents.items():
        args["nodes"][0]["options"][field] = {"inline_blob": {"filename": f"{field}.txt", "mime_type": "text/plain", "content": content}}
    audit_projection = inline_custody_audit_projection(args)
    manifest_shell = inline_custody_manifest_redaction_input(audit_projection)
    assert all(content not in repr(audit_projection) and content not in repr(manifest_shell) for content in contents.values())
    assert all(
        audit_projection["nodes"][0]["options"][field]["inline_blob"] == "[redacted inline content held for custody]" for field in contents
    )
    context = _trained_context(
        data_dir=tmp_path,
        session_engine=engine,
        session_id=session_id,
        user_message_id=message_id,
        user_message_content="Use the supplied CSV.",
        composer_model_identifier="test-model",
        composer_model_version="test-version",
        composer_provider="test-provider",
        composer_skill_hash="a" * 64,
        tool_arguments_hash="b" * 64,
    )
    candidate = build_set_pipeline_candidate(args, _empty_state(), context)
    assert candidate.acceptable, candidate.result.validation.errors
    assert len(candidate.prepared_prompt_blobs) == 2
    preparation = prepare_pipeline_custody(
        args,
        None,
        session_id=session_id,
        max_storage_per_session=1_000_000,
        prompt_blobs=candidate.prepared_prompt_blobs,
    )
    assert "inline_blob" not in str(preparation.arguments)
    assert len(preparation.additional_requests) == 1

    async def settle() -> None:
        with fenced_operation_context(engine, session_id) as operation:
            await finalize_pipeline_custody(
                preparation,
                engine=engine,
                data_dir=tmp_path,
                max_storage_per_session=1_000_000,
                session_operation_context=operation,
                session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
            )

    asyncio.run(settle())
    with engine.begin() as conn:
        rows = conn.execute(select(blobs_table.c.filename, blobs_table.c.creation_modality)).all()
    assert {row.filename for row in rows} == {"prompt_template.txt", "system_prompt.txt"}
    assert {row.creation_modality for row in rows} == {"llm_generated"}
    safe = build_set_pipeline_candidate(preparation.arguments, _empty_state(), context)
    assert safe.acceptable, safe.result.validation.errors
    for field, content in contents.items():
        marker = safe.result.updated_state.nodes[0].options[field]
        assert marker["mode"] == "inline_content"
        assert marker["blob_ref"]
        assert content.encode() in [request.content for request in (preparation.request, *preparation.additional_requests)]
    service = BlobServiceImpl(engine, tmp_path)
    with fenced_operation_context(engine, session_id) as operation:
        snapshot = resolve_prompt_review_snapshot(
            session_id=UUID(session_id),
            node_id="classify",
            options=safe.result.updated_state.nodes[0].options,
            read_blob=lambda blob_id: service.read_blob_content_sync(blob_id, operation),
        )
    assert snapshot is not None
    assert {binding.field_path: binding.text for binding in snapshot.bindings} == contents
    assert {binding.creation_modality for binding in snapshot.bindings} == {"llm_generated"}


@pytest.mark.asyncio
async def test_approval_candidate_finalization_refuses_long_prompt(tmp_path: Path) -> None:
    args = _structured_llm_args(tmp_path)
    args["nodes"][0]["options"]["prompt_template"] = "word " * 101
    context = _trained_context(data_dir=tmp_path)
    finalized = await _finalize_complete_set_pipeline_candidate(
        args,
        _empty_state(),
        context,
        plugin_snapshot=context.plugin_snapshot,
        policy_catalog=context.catalog,
    )
    assert not finalized.candidate.acceptable
    assert any(error.error_code == "inline_prompt_too_long" for error in finalized.candidate.result.validation.errors)


@pytest.mark.asyncio
async def test_explicit_approval_staging_refuses_long_prompt(
    tmp_path: Path,
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    sessions = composer_service_with_real_sessions._require_sessions_service()
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=create_catalog_service(), settings=_make_settings(data_dir=tmp_path), sessions_service=sessions
    )
    await sessions.update_composer_preferences(
        UUID(result_session_id), trust_mode="explicit_approve", density_default="high", actor="user:alice"
    )
    args = _structured_llm_args(tmp_path)
    args["source"]["options"]["path"] = str(tmp_path / "blobs" / result_session_id / "input.csv")
    args["nodes"][0]["options"]["prompt_template"] = "word " * 101
    llm = _FakeComposeLLM(
        (
            _fake_llm_response(tool_calls=({"id": "long_prompt", "name": "set_pipeline", "arguments": args},)),
            _fake_llm_response(content="The proposal was considered."),
        )
    )
    turn = await composer._run_one_turn_for_test(llm=llm, session_id=result_session_id, initial_state=_empty_state())
    assert await sessions.list_composition_proposals(UUID(result_session_id)) == []
    assert any(
        error.error_code == "inline_prompt_too_long" for outcome in turn.tool_outcomes for error in outcome.response.validation.errors
    ), [
        (outcome.response.success, [(error.error_code, error.message) for error in outcome.response.validation.errors])
        for outcome in turn.tool_outcomes
    ]


def test_tutorial_prompts_fit_both_limits() -> None:
    tutorial = Path("src/elspeth/web/frontend/src/components/tutorial/tutorialMachine.ts").read_text()
    for label in ("system prompt", "user prompt template"):
        match = re.search(rf"Set its {label} to: '([^']+)'", tutorial)
        assert match is not None
        words, characters = inline_prompt_counts(match.group(1))
        assert words <= 100 and characters <= 1_000, f"tutorial {label} must fit the inline prompt limit"


@pytest.mark.asyncio
async def test_planner_candidate_with_prompt_member_gets_file_and_full_review_snapshot(tmp_path: Path) -> None:
    engine, origin = await _session_context()
    args = _structured_llm_args(tmp_path)
    args["source"]["options"]["path"] = str(tmp_path / "blobs" / origin.session_id / "input.csv")
    content = "planner-authored prompt " * 60
    args["nodes"][0]["options"]["prompt_template"] = {
        "inline_blob": {"filename": "planner-prompt.txt", "mime_type": "text/plain", "content": content}
    }
    completion = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": args})))
    proposal = await _plan(
        tmp_path=tmp_path,
        tool_context=_trained_context(data_dir=tmp_path),
        completion=completion,
        originating_message=origin,
        custody_config=PlannerCustodyConfig(
            data_dir=str(tmp_path),
            session_engine=engine,
            max_storage_per_session=1_000_000,
            secret_service=None,
            runtime_preflight=None,
        ),
    )
    safe_pipeline = proposal.proposal.to_dict()["pipeline"]
    assert "inline_blob" not in str(safe_pipeline)
    marker = safe_pipeline["nodes"][0]["options"]["prompt_template"]
    assert marker["mode"] == "inline_content"
    assert proposal.candidate_state.nodes[0].options["prompt_template"]["blob_ref"] == marker["blob_ref"]
    with engine.begin() as conn:
        row = conn.execute(select(blobs_table).where(blobs_table.c.id == marker["blob_ref"])).mappings().one()
    assert row["creation_modality"] == "llm_generated"
    assert Path(row["storage_path"]).read_text() == content
    service = BlobServiceImpl(engine, tmp_path)
    with fenced_operation_context(engine, origin.session_id) as operation:
        snapshot = resolve_prompt_review_snapshot(
            session_id=UUID(origin.session_id),
            node_id="classify",
            options=proposal.candidate_state.nodes[0].options,
            read_blob=lambda blob_id: service.read_blob_content_sync(blob_id, operation),
        )
    assert snapshot is not None
    assert {binding.field_path: binding.text for binding in snapshot.bindings}["prompt_template"] == content


@pytest.mark.asyncio
async def test_planner_changed_call_resends_same_prompt_file_with_new_identity(tmp_path: Path) -> None:
    engine, origin = await _session_context()
    args = _structured_llm_args(tmp_path)
    args["source"]["options"]["path"] = str(tmp_path / "blobs" / origin.session_id / "input.csv")
    args["nodes"][0]["options"]["prompt_template"] = {
        "inline_blob": {"filename": "planner-prompt.txt", "mime_type": "text/plain", "content": "generated prompt " * 60}
    }
    ids = []
    for current in (args, {**args, "metadata": {"name": "renamed"}}, args):
        proposal = await _plan(
            tmp_path=tmp_path,
            tool_context=_trained_context(data_dir=tmp_path),
            completion=_ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": current}))),
            originating_message=origin,
            custody_config=PlannerCustodyConfig(
                data_dir=str(tmp_path),
                session_engine=engine,
                max_storage_per_session=1_000_000,
                secret_service=None,
                runtime_preflight=None,
            ),
        )
        ids.append(proposal.proposal.to_dict()["pipeline"]["nodes"][0]["options"]["prompt_template"]["blob_ref"])
    assert ids[0] != ids[1]
    assert ids[0] == ids[2]
    with engine.begin() as connection:
        assert connection.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 2
