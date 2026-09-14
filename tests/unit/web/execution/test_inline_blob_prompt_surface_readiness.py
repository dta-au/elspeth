"""/validate and run admission agree on LLM-authored inline blobs in an llm prompt surface.

Finding #1 policy (ADR-034): a user-uploaded ``inline_content`` blob may back an
``llm`` node's prompt surface or model, but an LLM-authored blob may not, because
the prompt-template and model-choice reviews read those options as strings and
the run substitutes the blob text afterwards. Run admission refuses it
(``InlineBlobPromptSurfaceAdmissionError``). Before this fix nothing on the
/validate path applied the refusal, so the pipeline showed ready and the run
failed after creation. These tests feed ONE state fixture to both paths.
"""

from __future__ import annotations

import hashlib
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
from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer import yaml_generator as composer_yaml_generator
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.execution._validation_materialization import llm_prompt_surface_field
from elspeth.web.execution.preflight import resolve_runtime_yaml_paths
from elspeth.web.execution.service import InlineBlobPromptSurfaceAdmissionError
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    model_choice_artifact_hash,
    prompt_review_anchor_hash_from_options,
)

_MODEL = "openai/gpt-4o"
_CONTENT = b"prompt text"
# The marker pins the real content hash, so an admitted run gets past substitution.
_SHA256 = hashlib.sha256(_CONTENT).hexdigest()
_BLOB_ID = UUID("5b7a4e0e-9e4a-4f0b-8d3e-2c0e1f0d3a4b")
_SESSION_ID = UUID(agreement._AGREEMENT_SESSION_ID)

# Every guarded option path the shared predicate names. ``queries`` and
# ``queries.q`` are whole values that would carry a query template.
_GUARDED_FIELDS = ("prompt_template", "system_prompt", "model", "queries.q.template", "queries.q", "queries")
_LLM_AUTHORED_MODALITIES = tuple(modality for modality in CreationModality if modality.requires_llm_provenance())
_USER_MODALITIES = tuple(modality for modality in CreationModality if not modality.requires_llm_provenance())


def _marker() -> dict[str, str]:
    return {"blob_ref": str(_BLOB_ID), "mode": "inline_content", "sha256": _SHA256}


def _resolved(kind: str, *, artifact_hash: str, draft: str) -> dict[str, Any]:
    return {
        "id": f"{kind}:classify",
        "kind": kind,
        "user_term": f"{kind}:classify",
        "status": "resolved",
        "draft": draft,
        "event_id": f"evt-{kind}",
        "accepted_value": draft,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": artifact_hash,
    }


def _llm_options(field: str) -> dict[str, Any]:
    """An ``llm`` node whose ``field`` option holds the marker, with every review it opens resolved."""
    options: dict[str, Any] = {
        "provider": "openrouter",
        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
        "model": _MODEL,
        "prompt_template": "Classify {{ row.text }}",
        "required_input_fields": [],
        "schema": {"mode": "observed"},
    }
    if field in ("prompt_template", "system_prompt", "model"):
        options[field] = _marker()
    elif field == "queries.q.template":
        options["queries"] = {"q": {"input_fields": {"text": "text"}, "template": _marker()}}
    elif field == "queries.q":
        options["queries"] = {"q": _marker()}
    elif field == "queries":
        options["queries"] = _marker()
    else:
        raise AssertionError(f"unknown guarded field {field!r}")
    reviews: list[dict[str, Any]] = []
    # One anchor derivation: the multi-query surface or structured skeleton,
    # else ``stable_hash(prompt_template)`` for an unstructured string prompt.
    # A marker-valued prompt_template carries no reviewable text, so no row.
    anchor = prompt_review_anchor_hash_from_options(options)
    if anchor is None and type(options["prompt_template"]) is str:
        anchor = stable_hash(options["prompt_template"])
    if anchor is not None:
        reviews.append(_resolved("llm_prompt_template", artifact_hash=anchor, draft="surface"))
    if field != "model":
        reviews.append(_resolved("llm_model_choice", artifact_hash=model_choice_artifact_hash(_MODEL), draft=_MODEL))
    options[INTERPRETATION_REQUIREMENTS_KEY] = reviews
    return options


def _state(tmp_path: Path, field: str) -> CompositionState:
    blobs_dir = tmp_path / "blobs" / str(_SESSION_ID)
    outputs_dir = tmp_path / "outputs" / str(_SESSION_ID)
    blobs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="classify_input",
            options={"path": str(blobs_dir / "input.csv"), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="classify",
                node_type="transform",
                plugin="llm",
                input="classify_input",
                on_success="results",
                on_error="discard",
                options=_llm_options(field),
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


def _record(modality: CreationModality, *, content: bytes = b"prompt text") -> BlobRecord:
    base = agreement._ready_inline_blob_record(blob_id=_BLOB_ID, session_id=_SESSION_ID, content=content, content_hash=_SHA256)
    return replace(base, creation_modality=modality)


def _validate(tmp_path: Path, field: str, modality: CreationModality) -> Any:
    record = _record(modality)
    return validate_pipeline_for_trained_operator(
        _state(tmp_path, field),
        SimpleNamespace(data_dir=tmp_path),
        composer_yaml_generator,
        blob_get_metadata=lambda blob_id: record if blob_id == _BLOB_ID else None,
        session_id=str(_SESSION_ID),
    )


def _blob_check(result: Any) -> Any:
    return next(check for check in result.checks if check.name == "blob_inline_refs")


# ── the shared predicate ────────────────────────────────────────────────────


@pytest.mark.parametrize("field", _GUARDED_FIELDS)
def test_predicate_names_every_guarded_llm_option(field: str) -> None:
    assert llm_prompt_surface_field(f"node:classify.options.{field}") == ("classify", field)


@pytest.mark.parametrize(
    "field_path",
    [
        "node:classify.options.queries.q.input_fields.text",
        "node:classify.options.response_field",
        "node:classify.options",
        "source.options.prompt_template",
        "output:results.options.model",
    ],
)
def test_predicate_leaves_other_fields_unguarded(field_path: str) -> None:
    assert llm_prompt_surface_field(field_path) is None


# ── /validate refuses what run admission refuses ────────────────────────────


@pytest.mark.parametrize("modality", _LLM_AUTHORED_MODALITIES, ids=lambda modality: modality.value)
@pytest.mark.parametrize("field", _GUARDED_FIELDS)
def test_validate_blocks_llm_authored_blob_in_llm_prompt_surface(tmp_path: Path, field: str, modality: CreationModality) -> None:
    result = _validate(tmp_path, field, modality)

    assert result.is_valid is False
    assert result.readiness.execution_ready is False
    check = _blob_check(result)
    assert check.passed is False
    assert check.detail == f"node:classify.options.{field}: llm_authored"
    [error] = [error for error in result.errors if error.error_code == "llm_authored_inline_blob_content"]
    assert error.component_id == "classify"
    assert error.component_type == "transform"
    assert f"node:classify.options.{field}" in error.message
    assert [blocker.code for blocker in result.readiness.blockers] == ["blob_inline_refs"]


@pytest.mark.parametrize("modality", _USER_MODALITIES, ids=lambda modality: modality.value)
@pytest.mark.parametrize("field", _GUARDED_FIELDS)
def test_validate_admits_user_uploaded_blob_in_llm_prompt_surface(tmp_path: Path, field: str, modality: CreationModality) -> None:
    result = _validate(tmp_path, field, modality)

    check = _blob_check(result)
    assert check.passed is True, check.detail
    assert all(error.error_code != "llm_authored_inline_blob_content" for error in result.errors)


def test_validate_admits_llm_authored_blob_outside_the_prompt_surface(tmp_path: Path) -> None:
    """A query's ``input_fields`` entry is not a prompt surface."""
    state = _state(tmp_path, "system_prompt")
    node = state.nodes[0]
    options = _llm_options("system_prompt")
    options["system_prompt"] = "You are careful."
    options["queries"] = {"q": {"input_fields": {"text": _marker()}, "template": "Q {{ row.text }}"}}
    anchor = prompt_review_anchor_hash_from_options(options)
    assert anchor is not None
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        _resolved("llm_prompt_template", artifact_hash=anchor, draft="surface"),
        _resolved("llm_model_choice", artifact_hash=model_choice_artifact_hash(_MODEL), draft=_MODEL),
    ]
    state = replace(state, nodes=(replace(node, options=options),))
    record = _record(CreationModality.LLM_GENERATED)

    result = validate_pipeline_for_trained_operator(
        state,
        SimpleNamespace(data_dir=tmp_path),
        composer_yaml_generator,
        blob_get_metadata=lambda _blob_id: record,
        session_id=str(_SESSION_ID),
    )

    assert _blob_check(result).passed is True


# ── parity: the same fixture at run admission ───────────────────────────────


def _run_admission(tmp_path: Path, field: str, modality: CreationModality) -> tuple[BaseException | None, Any, Any]:
    """Drive ``_run_pipeline`` with the runtime YAML the composer generates for the same state.

    Returns what the run raised, the fake blob service and the fake session
    service, so a caller can see how far past inline-blob admission it got.
    """
    service, session_service, loop = agreement.TestComposerRuntimeBlobInlineAgreement._execution_service(tmp_path)
    record = replace(_record(modality), session_id=session_service.run.session_id)
    blob_service = agreement._FakeBlobService(blob_record=record, content=_CONTENT)
    cast(Any, service)._blob_service = blob_service
    pipeline_yaml = resolve_runtime_yaml_paths(
        composer_yaml_generator.generate_yaml(_state(tmp_path, field)),
        str(tmp_path),
        session_id=str(_SESSION_ID),
    )
    loaded = yaml.safe_load(pipeline_yaml)
    assert type(loaded) is dict
    raised: BaseException | None = None
    with (
        patch("elspeth.web.execution.service.Orchestrator"),
        patch("elspeth.web.execution.service.load_settings_from_config_dict"),
        patch("elspeth.web.execution.service.open_landscape_db"),
        patch("elspeth.web.execution.service.FilesystemPayloadStore"),
    ):
        lease = agreement._execute_lease(loop, session_service.run.session_id)
        try:
            service._run_pipeline(str(uuid4()), pipeline_yaml, threading.Event(), session_operation_lease=lease)
        except Exception as exc:
            raised = exc
        finally:
            loop.run_until_complete(lease.close())
            loop.close()
    return raised, blob_service, session_service


@pytest.mark.parametrize("modality", _LLM_AUTHORED_MODALITIES, ids=lambda modality: modality.value)
@pytest.mark.parametrize("field", _GUARDED_FIELDS)
def test_validate_and_run_admission_both_refuse_the_same_llm_authored_fixture(
    tmp_path: Path, field: str, modality: CreationModality
) -> None:
    validate_result = _validate(tmp_path / "validate", field, modality)
    raised, blob_service, session_service = _run_admission(tmp_path / "run", field, modality)

    assert validate_result.is_valid is False
    assert _blob_check(validate_result).detail == f"node:classify.options.{field}: llm_authored"
    assert type(raised) is InlineBlobPromptSurfaceAdmissionError
    assert f"node:classify.options.{field}" in str(raised)
    assert blob_service.link_blob_to_run_calls == []
    assert blob_service.read_blob_content_calls == []
    assert session_service.recorded_blob_inline_resolutions == []


@pytest.mark.parametrize("field", _GUARDED_FIELDS)
def test_validate_and_run_admission_both_admit_the_same_user_uploaded_fixture(tmp_path: Path, field: str) -> None:
    validate_result = _validate(tmp_path / "validate", field, CreationModality.VERBATIM)
    raised, blob_service, session_service = _run_admission(tmp_path / "run", field, CreationModality.VERBATIM)

    assert _blob_check(validate_result).passed is True
    # Admission passed, and substitution completed against the pinned hash: the
    # blob was linked, read once, and its inline resolution recorded. The run
    # stops later on the fixture's settings fake, outside this contract, so what
    # it raises there is not asserted.
    assert type(raised) is not InlineBlobPromptSurfaceAdmissionError
    assert [call[0] for call in blob_service.link_blob_to_run_calls] == [_BLOB_ID]
    assert blob_service.read_blob_content_calls == [_BLOB_ID]
    assert len(session_service.recorded_blob_inline_resolutions) == 1
