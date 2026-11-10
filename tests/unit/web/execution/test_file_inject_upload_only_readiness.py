"""/validate and run admission refuse ``file_inject`` content that is not a user-uploaded file.

``file_inject`` writes configured content into row data, where a prompt
template can read it, and no review covers it. In the composer the only
accepted value at ``files.<entry>.content`` is an ``inline_content`` marker for
a user-uploaded blob, and ``default_content`` is not accepted. The authoring
tools refuse the rest; these tests show the two execution gates refuse the same
states through the same predicate module, so a state that reached persistence
another way (an import, an older session) still cannot validate or run.

Each refusal feeds ONE state to both gates, and has a control: the same state
with a user-uploaded blob attached is admitted by both.
"""

from __future__ import annotations

import hashlib
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import yaml

import tests.integration.pipeline.test_composer_runtime_agreement as agreement
from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.enums import CreationModality
from elspeth.plugins.transforms.file_inject import FileInjectConfig
from elspeth.web.blobs.protocol import BlobIntegrityError
from elspeth.web.composer import yaml_generator as composer_yaml_generator
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.execution import _validation_materialization as guard
from elspeth.web.execution.preflight import resolve_runtime_yaml_paths
from elspeth.web.execution.service import InlineBlobPromptSurfaceAdmissionError
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from tests.helpers import execution_custody
from tests.helpers.execution_custody import ExecutionTestCustody

execution_fixture = execution_custody.execution_fixture

_CONTENT = b"Check the totals against the line items.\n"
_SHA256 = hashlib.sha256(_CONTENT).hexdigest()
_BLOB_ID = UUID("7c1d2a44-3f5b-4c1e-9a70-5e2b8f4d6c19")
_SESSION_ID = UUID(agreement._AGREEMENT_SESSION_ID)
_ERROR_CODE = "file_inject_content_not_uploaded"
_LLM_AUTHORED_MODALITIES = tuple(modality for modality in CreationModality if modality.requires_llm_provenance())
_USER_MODALITIES = tuple(modality for modality in CreationModality if not modality.requires_llm_provenance())


def _marker(sha256: str = _SHA256) -> dict[str, str]:
    return {"blob_ref": str(_BLOB_ID), "mode": "inline_content", "sha256": sha256}


def _options(content: object, **extra: object) -> dict[str, Any]:
    return {
        "schema": {"mode": "observed"},
        "mode": "select",
        "key_field": "kind",
        "output_field": "guidance",
        "files": {"invoice": {"content": content, "format": "text"}},
        **extra,
    }


def _state(tmp_path: Path, options: dict[str, Any]) -> CompositionState:
    blobs_dir = tmp_path / "blobs" / str(_SESSION_ID)
    outputs_dir = tmp_path / "outputs" / str(_SESSION_ID)
    blobs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="inject_input",
            options={"path": str(blobs_dir / "input.csv"), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="inject",
                node_type="transform",
                plugin="file_inject",
                input="inject_input",
                on_success="results",
                on_error="discard",
                options=options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="results",
                plugin="json",
                options={"path": str(outputs_dir / "results.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _record(modality: CreationModality) -> BlobRecord:
    base = agreement._ready_inline_blob_record(blob_id=_BLOB_ID, session_id=_SESSION_ID, content=_CONTENT, content_hash=_SHA256)
    return replace(base, creation_modality=modality)


def _validate(tmp_path: Path, options: dict[str, Any], modality: CreationModality = CreationModality.VERBATIM) -> Any:
    record = _record(modality)
    return validate_pipeline_for_trained_operator(
        _state(tmp_path, options),
        SimpleNamespace(data_dir=tmp_path),
        composer_yaml_generator,
        blob_get_metadata=lambda blob_id: record if blob_id == _BLOB_ID else None,
        blob_get_content=lambda _blob_id: (record, _CONTENT),
        session_id=str(_SESSION_ID),
    )


def _blob_check(result: Any) -> Any:
    return next(check for check in result.checks if check.name == "blob_inline_refs")


def _run_admission(
    tmp_path: Path,
    options: dict[str, Any],
    execution_fixture: ExecutionTestCustody,
    modality: CreationModality = CreationModality.VERBATIM,
) -> tuple[BaseException | None, Any, Any]:
    """Drive ``_run_pipeline`` with the runtime YAML the composer generates for the same state."""
    service, session_service, loop = agreement.TestComposerRuntimeBlobInlineAgreement._execution_service(
        tmp_path, execution_fixture=execution_fixture
    )
    record = replace(_record(modality), session_id=session_service.run.session_id)
    blob_service = agreement._FakeBlobService(blob_record=record, content=_CONTENT)
    cast(Any, service)._blob_service = blob_service
    pipeline_yaml = resolve_runtime_yaml_paths(
        composer_yaml_generator.generate_yaml(_state(tmp_path, options)),
        str(tmp_path),
        session_id=str(_SESSION_ID),
    )
    assert type(yaml.safe_load(pipeline_yaml)) is dict
    raised: BaseException | None = None
    with (
        patch("elspeth.web.execution.service.Orchestrator"),
        patch("elspeth.web.execution.service.load_settings_from_config_dict"),
        patch("elspeth.web.execution.service.open_landscape_db"),
        patch("elspeth.web.execution.service.FilesystemPayloadStore"),
    ):
        lease: SessionOperationLease | None = None
        try:
            lease = agreement._execute_lease(loop, session_service.run.session_id, service=service, execution_fixture=execution_fixture)
            try:
                service._run_pipeline(str(uuid4()), pipeline_yaml, threading.Event(), session_operation_lease=lease)
            except Exception as exc:
                raised = exc
        finally:
            agreement._close_direct_runtime_probe(loop, lease, execution_fixture, sys.exc_info()[1] or raised)
    return raised, blob_service, session_service


def _assert_validate_refuses(result: Any, *, option_path: str, cause: str) -> Any:
    assert result.is_valid is False
    assert result.readiness.execution_ready is False
    check = _blob_check(result)
    assert check.passed is False
    assert check.detail == f"node:inject.options.{option_path}: {cause}"
    [error] = [error for error in result.errors if error.error_code == _ERROR_CODE]
    assert error.component_id == "inject"
    assert error.component_type == "transform"
    assert f"file_inject node 'inject' option '{option_path}' is refused: " in error.message
    assert [blocker.code for blocker in result.readiness.blockers] == ["blob_inline_refs"]
    return error


def _assert_admission_refuses(raised: BaseException | None, blob_service: Any, session_service: Any, *, option_path: str) -> None:
    assert type(raised) is InlineBlobPromptSurfaceAdmissionError
    assert f"node:inject.options.{option_path}" in str(raised)
    assert blob_service.link_blob_to_run_calls == []
    assert blob_service.read_blob_content_calls == []
    assert session_service.recorded_blob_inline_resolutions == []


def _assert_steers_to_an_upload(suggestion: str) -> None:
    step_1 = suggestion.index("(1) tell the user to save the text as a file and upload it")
    step_2 = suggestion.index("(2) once it is uploaded, find it with list_blobs / get_blob_metadata")
    step_3 = suggestion.index("(3) attach it")
    assert step_1 < step_2 < step_3
    assert "wire_blob_inline_ref at 'node:inject.options.files.invoice.content'" in suggestion
    assert '{"blob_ref": "<blob_id>", "mode": "inline_content", "sha256": "<content_hash>"}' in suggestion


# ── the shared predicate ────────────────────────────────────────────────────


@pytest.mark.parametrize("option_path", ["files", "files.A", "files.A.content"])
def test_guard_covers_nested_marker_beneath_files(option_path: str) -> None:
    surface = guard.guarded_inline_surface_field(f"node:n.options.{option_path}", node_plugins={"n": "file_inject"})

    assert surface == ("n", option_path, "file_inject")
    for modality in CreationModality:
        assert (
            guard.is_llm_authored_prompt_surface_binding(
                f"node:n.options.{option_path}", node_plugins={"n": "file_inject"}, creation_modality=modality
            )
            is modality.requires_llm_provenance()
        )


@pytest.mark.parametrize("plugin", ["reference_join", "llm", "field_mapper"])
def test_guard_ignores_files_option_on_other_plugins(plugin: str) -> None:
    assert guard.guarded_inline_surface_field("node:n.options.files.A.content", node_plugins={"n": plugin}) is None
    assert (
        guard.file_inject_blob_refusal(
            "node:n.options.files.A.content",
            blob_id=_BLOB_ID,
            node_plugins={"n": plugin},
            creation_modality=CreationModality.LLM_GENERATED,
        )
        is None
    )


@pytest.mark.parametrize("field_path", ["node:n.options.key_field", "node:n.options.filesystem", "node:other.options.files.A.content"])
def test_guard_leaves_other_file_inject_options_and_unknown_nodes_alone(field_path: str) -> None:
    assert guard.guarded_inline_surface_field(field_path, node_plugins={"n": "file_inject"}) is None


def test_guard_still_names_llm_prompt_surfaces_and_only_on_llm_nodes() -> None:
    assert guard.guarded_inline_surface_field("node:c.options.prompt_template", node_plugins={"c": "llm"}) == (
        "c",
        "prompt_template",
        "llm",
    )
    assert guard.guarded_inline_surface_field("node:c.options.prompt_template", node_plugins={"c": "file_inject"}) is None
    assert guard.guarded_inline_surface_field("node:c.options.response_field", node_plugins={"c": "llm"}) is None


def test_option_refusals_name_every_literal_entry_and_default_content() -> None:
    refusals = guard.file_inject_option_refusals(
        "n",
        {
            "files": {
                "a": {"content": _marker(), "format": "text"},
                "b": {"content": "literal", "format": "text"},
                "c": {"content": {"blob_ref": str(_BLOB_ID), "mode": "bind_source"}, "format": "text"},
                "d": {"format": "text"},
            },
            "default_content": {"any": "value"},
        },
    )

    assert [(refusal.option_path, refusal.cause) for refusal in refusals] == [
        ("default_content", "default_content"),
        ("files.b.content", "literal_content"),
        ("files.c.content", "literal_content"),
    ]


def test_option_refusals_accept_an_uploaded_marker_and_a_null_default() -> None:
    assert guard.file_inject_option_refusals("n", {"files": {"a": {"content": _marker(), "format": "text"}}, "default_content": None}) == ()


def test_runtime_config_refusals_read_every_node_section() -> None:
    literal = {"files": {"a": {"content": "literal", "format": "text"}}}
    config: dict[str, object] = {
        "transforms": [{"name": "t", "plugin": "file_inject", "options": literal}],
        "aggregations": [{"name": "g", "plugin": "file_inject", "options": literal}],
        "collectors": [{"name": "k", "plugin": "reference_join", "options": literal}],
    }

    assert [refusal.field_path for refusal in guard.file_inject_runtime_config_refusals(config)] == [
        "node:t.options.files.a.content",
        "node:g.options.files.a.content",
    ]
    assert guard.runtime_config_node_plugins(config) == {"t": "file_inject", "g": "file_inject", "k": "reference_join"}


# ── cause 1: a marker naming an LLM-authored blob ───────────────────────────


@pytest.mark.parametrize("modality", _LLM_AUTHORED_MODALITIES, ids=lambda modality: modality.value)
def test_validate_refuses_llm_authored_file_inject_ref(tmp_path: Path, modality: CreationModality) -> None:
    result = _validate(tmp_path, _options(_marker()), modality)

    error = _assert_validate_refuses(result, option_path="files.invoice.content", cause="llm_authored_blob")
    assert f"blob '{_BLOB_ID}' was created by the composer, not uploaded by the user" in error.message
    _assert_steers_to_an_upload(error.suggestion)


@pytest.mark.parametrize("modality", _USER_MODALITIES, ids=lambda modality: modality.value)
def test_validate_accepts_user_uploaded_file_inject_ref(tmp_path: Path, modality: CreationModality) -> None:
    result = _validate(tmp_path, _options(_marker()), modality)

    check = _blob_check(result)
    assert check.passed is True, check.detail
    assert all(error.error_code != _ERROR_CODE for error in result.errors)


@pytest.mark.parametrize("modality", _LLM_AUTHORED_MODALITIES, ids=lambda modality: modality.value)
def test_run_admission_refuses_llm_authored_file_inject_ref(
    tmp_path: Path, modality: CreationModality, execution_fixture: ExecutionTestCustody
) -> None:
    raised, blob_service, session_service = _run_admission(tmp_path, _options(_marker()), execution_fixture, modality)

    _assert_admission_refuses(raised, blob_service, session_service, option_path="files.invoice.content")
    assert f"blob '{_BLOB_ID}' was created by the composer, not uploaded by the user" in str(raised)


def test_run_admission_accepts_user_uploaded_file_inject_ref(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    raised, blob_service, session_service = _run_admission(tmp_path, _options(_marker()), execution_fixture)

    # Admission passed and substitution completed against the pinned hash. The
    # run stops later on the fixture's settings fake, outside this contract.
    assert type(raised) is not InlineBlobPromptSurfaceAdmissionError
    assert [call[0] for call in blob_service.link_blob_to_run_calls] == [_BLOB_ID]
    assert blob_service.read_blob_content_calls == [_BLOB_ID]
    assert len(session_service.recorded_blob_inline_resolutions) == 1


# ── cause 2: literal content ────────────────────────────────────────────────


def test_validate_and_run_admission_both_refuse_literal_file_inject_content(
    tmp_path: Path, execution_fixture: ExecutionTestCustody
) -> None:
    options = _options("Check the totals against the line items.\n")

    result = _validate(tmp_path / "validate", options)
    raised, blob_service, session_service = _run_admission(tmp_path / "run", options, execution_fixture)

    error = _assert_validate_refuses(result, option_path="files.invoice.content", cause="literal_content")
    assert "it holds literal content, not an uploaded file" in error.message
    assert "Check the totals" not in error.message + error.suggestion
    _assert_steers_to_an_upload(error.suggestion)
    _assert_admission_refuses(raised, blob_service, session_service, option_path="files.invoice.content")
    assert "it holds literal content, not an uploaded file" in str(raised)
    assert "Check the totals" not in str(raised)


def test_literal_entry_is_refused_beside_an_uploaded_one(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    """The literal check does not depend on whether the state also carries a marker."""
    options = _options(_marker())
    options["files"]["contract"] = {"content": "Check the parties.\n", "format": "text"}

    result = _validate(tmp_path / "validate", options)
    raised, blob_service, session_service = _run_admission(tmp_path / "run", options, execution_fixture)

    _assert_validate_refuses(result, option_path="files.contract.content", cause="literal_content")
    _assert_admission_refuses(raised, blob_service, session_service, option_path="files.contract.content")


# ── cause 3: default_content in any form ────────────────────────────────────


@pytest.mark.parametrize("default_content", ["Fallback wording.\n", _marker()], ids=["literal", "uploaded_marker"])
def test_validate_and_run_admission_both_refuse_file_inject_default_content(
    tmp_path: Path, default_content: object, execution_fixture: ExecutionTestCustody
) -> None:
    options = _options(_marker(), on_miss="default", default_content=default_content)

    result = _validate(tmp_path / "validate", options)
    raised, blob_service, session_service = _run_admission(tmp_path / "run", options, execution_fixture)

    error = _assert_validate_refuses(result, option_path="default_content", cause="default_content")
    assert "default_content is not accepted in the composer" in error.message
    assert "Use on_miss: null or on_miss: fail and handle the miss downstream" in error.suggestion
    assert "Fallback wording" not in error.message + error.suggestion
    _assert_admission_refuses(raised, blob_service, session_service, option_path="default_content")


def test_validate_and_run_admission_both_accept_on_miss_null(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    options = _options(_marker(), on_miss="null")

    result = _validate(tmp_path / "validate", options)
    raised, blob_service, _session_service = _run_admission(tmp_path / "run", options, execution_fixture)

    assert _blob_check(result).passed is True
    assert type(raised) is not InlineBlobPromptSurfaceAdmissionError
    assert blob_service.read_blob_content_calls == [_BLOB_ID]


# ── a hand-written marker with a wrong or stale hash fails closed ───────────


def test_wrong_marker_hash_fails_closed_at_validate_and_run_admission(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    options = _options(_marker(sha256="0" * 64))

    result = _validate(tmp_path / "validate", options)
    raised, blob_service, session_service = _run_admission(tmp_path / "run", options, execution_fixture)

    check = _blob_check(result)
    assert check.passed is False
    assert check.detail == "node:inject.options.files.invoice.content: hash_mismatch"
    assert result.readiness.execution_ready is False
    assert type(raised) is BlobIntegrityError
    assert blob_service.read_blob_content_calls == []
    assert session_service.recorded_blob_inline_resolutions == []


# ── the CLI is not judged by the composer rule ──────────────────────────────


def test_cli_settings_loader_accepts_literal_content_and_default_content() -> None:
    """A settings file's author wrote its content; only the composer refuses it."""
    settings = load_settings_from_yaml_string(
        """
sources:
  primary:
    plugin: csv
    on_success: rows
    options:
      path: input.csv
      schema: {mode: observed}
      on_validation_failure: discard
transforms:
  - name: inject
    plugin: file_inject
    input: rows
    on_success: results
    on_error: discard
    options:
      schema: {mode: observed}
      mode: select
      key_field: kind
      output_field: guidance
      on_miss: default
      default_content: "Use the general checklist.\\n"
      files:
        "invoice":
          content: "Check the totals against the line items.\\n"
          format: text
sinks:
  results:
    plugin: json
    on_write_failure: discard
    options:
      path: results.jsonl
      format: jsonl
      schema: {mode: observed}
"""
    )

    [transform] = settings.transforms
    config = FileInjectConfig.from_dict(dict(transform.options), plugin_name="file_inject")
    assert config.files["invoice"].content == "Check the totals against the line items.\n"
    assert config.default_content == "Use the general checklist.\n"
