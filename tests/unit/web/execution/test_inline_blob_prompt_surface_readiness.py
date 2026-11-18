"""File-backed scalar prompts require current approval regardless of author modality."""

from __future__ import annotations

import hashlib
import sys
import threading
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import pytest
import yaml

import tests.integration.pipeline.test_composer_runtime_agreement as agreement
from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.blobs_inline import InlineBlobNotTextError
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer import yaml_generator as composer_yaml_generator
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.execution._validation_materialization import llm_prompt_surface_field
from elspeth.web.execution.preflight import resolve_runtime_yaml_paths
from elspeth.web.execution.service import InlineBlobPromptSurfaceAdmissionError
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    model_choice_artifact_hash,
    prompt_review_anchor_hash_from_options,
)
from elspeth.web.sessions.prompt_file_review import PromptFileReviewStale
from elspeth.web.sessions.protocol import CompositionStateRecord
from tests.helpers import execution_custody
from tests.helpers.execution_custody import ExecutionTestCustody

execution_fixture = execution_custody.execution_fixture

_MODEL = "openai/gpt-4o"
_CONTENT = b"prompt text"
# The marker pins the real content hash, so an admitted run gets past substitution.
_SHA256 = hashlib.sha256(_CONTENT).hexdigest()
_BLOB_ID = UUID("5b7a4e0e-9e4a-4f0b-8d3e-2c0e1f0d3a4b")
_SESSION_ID = UUID(agreement._AGREEMENT_SESSION_ID)

# Every guarded option path the shared predicate names. ``queries`` and
# ``queries.q`` are whole values that would carry a query template.
_GUARDED_FIELDS = ("prompt_template", "system_prompt", "model", "queries.q.template", "queries.0.template", "queries.q", "queries")
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
    elif field == "queries.0.template":
        # The list form, addressed by canonical position (staging hotfix list-index paths).
        options["queries"] = [{"name": "q", "input_fields": {"text": "text"}, "template": _marker()}]
    elif field == "queries.q":
        options["queries"] = {"q": _marker()}
    elif field == "queries":
        options["queries"] = _marker()
    else:
        raise AssertionError(f"unknown guarded field {field!r}")
    reviews: list[dict[str, Any]] = []
    # One anchor derivation: the multi-query surface or structured skeleton,
    # else ``stable_hash(prompt_template)`` for an unstructured string prompt.
    # A marker-valued prompt_template has no literal text to review in this
    # metadata fixture. Other shapes retain their existing skeleton review.
    anchor = prompt_review_anchor_hash_from_options(options)
    if anchor is None and type(options["prompt_template"]) is str:
        anchor = stable_hash(options["prompt_template"])
    if anchor is not None and field != "prompt_template":
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
        blob_get_content=lambda _blob_id: (record, _CONTENT),
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


# ── /validate accepts supported bytes and refuses unsupported positions ────


@pytest.mark.parametrize("modality", _LLM_AUTHORED_MODALITIES, ids=lambda modality: modality.value)
@pytest.mark.parametrize("field", _GUARDED_FIELDS)
def test_validate_accepts_llm_authored_scalar_prompt_bytes(tmp_path: Path, field: str, modality: CreationModality) -> None:
    result = _validate(tmp_path, field, modality)
    check = _blob_check(result)
    if field in {"prompt_template", "system_prompt", "queries.q.template", "queries.0.template"}:
        assert check.passed is True, check.detail
        assert all(error.error_code != "llm_authored_inline_blob_content" for error in result.errors)
    else:
        assert check.passed is False
        assert any(error.error_code == "inline_blob_field_path_invalid" for error in result.errors)


@pytest.mark.parametrize("modality", _USER_MODALITIES, ids=lambda modality: modality.value)
@pytest.mark.parametrize("field", _GUARDED_FIELDS)
def test_validate_accepts_user_scalar_prompt_bytes(tmp_path: Path, field: str, modality: CreationModality) -> None:
    result = _validate(tmp_path, field, modality)

    check = _blob_check(result)
    if field in {"prompt_template", "system_prompt", "queries.q.template", "queries.0.template"}:
        assert check.passed is True, check.detail
    else:
        assert check.passed is False
        assert any(error.error_code == "inline_blob_field_path_invalid" for error in result.errors)


@pytest.mark.parametrize("field", ["prompt_template", "queries.0.template"])
def test_deferred_prompt_validation_does_not_admit_a_binary_blob(tmp_path: Path, field: str) -> None:
    """A manually authored PDF marker has a typed validation refusal."""
    content = b"%PDF-1.4\n%%EOF\n"
    record = replace(
        _record(CreationModality.VERBATIM, content=content),
        mime_type="application/pdf",
        content_hash=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    state = _state(tmp_path, field)
    node = state.nodes[0]
    options = cast(dict[str, Any], deep_thaw(node.options))
    marker = {"blob_ref": str(_BLOB_ID), "mode": "inline_content", "sha256": record.content_hash}
    if field == "prompt_template":
        options["prompt_template"] = marker
    else:
        options["queries"][0]["template"] = marker
        anchor = prompt_review_anchor_hash_from_options(options)
        assert anchor is not None
        for review in options[INTERPRETATION_REQUIREMENTS_KEY]:
            if review["kind"] == "llm_prompt_template":
                review["resolved_prompt_template_hash"] = anchor
    state = replace(state, nodes=(replace(node, options=options),))
    try:
        result = validate_pipeline_for_trained_operator(
            state,
            SimpleNamespace(data_dir=tmp_path),
            composer_yaml_generator,
            blob_get_metadata=lambda blob_id: record if blob_id == _BLOB_ID else None,
            blob_get_content=lambda _blob_id: (record, content),
            session_id=str(_SESSION_ID),
        )
    except ValueError:
        pytest.fail("Non-text inline content escaped validation as an exception")
    assert result.is_valid is False
    assert _blob_check(result).passed is False
    assert any(error.error_code == "inline_blob_not_text" and ".txt or .md" in error.message for error in result.errors)


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
        blob_get_content=lambda _blob_id: (record, _CONTENT),
        session_id=str(_SESSION_ID),
    )

    assert _blob_check(result).passed is True


# ── parity: the same fixture at run admission ───────────────────────────────


def _run_admission(
    tmp_path: Path,
    field: str,
    modality: CreationModality,
    execution_fixture: ExecutionTestCustody,
    *,
    mime_type: str = "text/plain",
    approved: bool = False,
    after_substitution: Callable[[dict[str, Any], Any], None] | None = None,
    landscape_probe: Any | None = None,
    run_id: UUID | None = None,
    on_final_approval: Callable[[Any], None] | None = None,
    resume_existing: bool = False,
    on_orchestrator: Callable[[Any], None] | None = None,
) -> tuple[BaseException | None, Any, Any]:
    """Drive ``_run_pipeline`` with the runtime YAML the composer generates for the same state.

    Returns what the run raised, the fake blob service and the fake session
    service, so a caller can see how far past inline-blob admission it got.
    ``resume_existing`` (with ``landscape_probe``) reaches the resume branch
    with checkpoint recovery stubbed; the patched orchestrator's ``resume``
    stops the run, and ``on_orchestrator`` receives the orchestrator instance.
    """
    service, session_service, loop = agreement.TestComposerRuntimeBlobInlineAgreement._execution_service(
        tmp_path, execution_fixture=execution_fixture
    )
    source_state = _state(tmp_path, field)
    state_data = source_state.to_dict()
    state_id = uuid4()
    state_record = CompositionStateRecord(
        id=state_id,
        session_id=session_service.run.session_id,
        version=1,
        sources=state_data["sources"],
        nodes=state_data["nodes"],
        edges=state_data["edges"],
        outputs=state_data["outputs"],
        metadata_=state_data["metadata"],
        is_valid=True,
        validation_errors=None,
        created_at=datetime.now(UTC),
        derived_from_state_id=None,
    )

    class PromptApprovals:
        """The fake session service's prompt-approval seam: the state the run names, and its approval verdict."""

        async def get_state_in_session(self, requested_state_id: UUID, requested_session_id: UUID) -> CompositionStateRecord:
            assert requested_state_id == state_id
            assert requested_session_id == session_service.run.session_id
            return state_record

        async def assert_prompt_file_approvals(self, snapshot: Any) -> None:
            if not approved:
                raise PromptFileReviewStale()
            if on_final_approval is not None:
                on_final_approval(snapshot)

    # The run names this state; the fake's get_run returns ``run`` itself.
    session_service.run.state_id = state_id
    session_service.prompt_approval_service = PromptApprovals()
    if resume_existing:
        from elspeth.core.config import RateLimitSettings

        @dataclass(slots=True)
        class ResumeRuntimeSettings(agreement._RuntimeSettingsFake):
            """The orchestrator setup the resume branch reaches reads the operator rate limit."""

            execution_rate_limit: RateLimitSettings = dataclass_field(default_factory=lambda: RateLimitSettings(enabled=False))

        web_settings = cast(agreement._RuntimeSettingsFake, cast(Any, service)._settings)
        cast(Any, service)._settings = ResumeRuntimeSettings(
            data_dir=web_settings.data_dir,
            payload_store_path=web_settings.payload_store_path,
            deployment_target=web_settings.deployment_target,
            landscape_passphrase=web_settings.landscape_passphrase,
            secret_wiring_allowlist=web_settings.secret_wiring_allowlist,
        )
    record = replace(_record(modality), session_id=session_service.run.session_id, mime_type=mime_type)
    blob_service = agreement._FakeBlobService(blob_record=record, content=_CONTENT)
    if resume_existing:

        class ResumeBlobService(agreement._FakeBlobService):
            """The resume branch runs inside the run's output write admission."""

            def run_output_write_admission(self, run_id: UUID, *, session_operation_context: Any) -> nullcontext[None]:
                del run_id, session_operation_context
                return nullcontext()

        blob_service = ResumeBlobService(blob_record=record, content=_CONTENT)
    cast(Any, service)._blob_service = blob_service
    pipeline_yaml = resolve_runtime_yaml_paths(
        composer_yaml_generator.generate_yaml(source_state),
        str(tmp_path),
        session_id=str(_SESSION_ID),
    )
    loaded = yaml.safe_load(pipeline_yaml)
    assert type(loaded) is dict
    raised: BaseException | None = None
    with (
        ExitStack() as resume_patches,
        patch("elspeth.web.execution.service.Orchestrator") as mock_orchestrator,
        patch("elspeth.web.execution.service.load_settings_from_config_dict") as mock_load,
        patch("elspeth.web.execution.service.open_landscape_db") as mock_open_landscape,
        patch("elspeth.web.execution.service.FilesystemPayloadStore"),
        patch("elspeth.web.execution.service.build_validated_runtime_graph") as mock_runtime_graph,
        patch("elspeth.web.execution.service.assemble_and_validate_pipeline_config") as mock_assemble,
    ):
        if landscape_probe is not None:
            from tests.unit.web.execution.test_service import _execution_graph_stub, _mock_pipeline_settings, _plugin_bundle_stub

            mock_open_landscape.return_value = landscape_probe
            mock_load.return_value = _mock_pipeline_settings()
            mock_runtime_graph.return_value = SimpleNamespace(plugin_bundle=_plugin_bundle_stub(), graph=_execution_graph_stub())
            if resume_existing:
                # A checkpointed, resumable run: only the prompt-input check
                # stands between admission and the orchestrator's resume.
                resume_patches.enter_context(patch("elspeth.core.checkpoint.CheckpointManager"))
                resume_patches.enter_context(patch("elspeth.web.execution.service.check_source_lifecycle_resumable"))
                resume_patches.enter_context(patch("elspeth.web.execution.service.RecoveryManager"))
                mock_orchestrator.return_value.resume.side_effect = RuntimeError("stop at resume")
            else:
                mock_assemble.side_effect = RuntimeError("stop after prompt input evidence")
        if after_substitution is not None:

            def inspect_substituted(config_dict: dict[str, Any], *, expand_env_vars: bool) -> None:
                assert expand_env_vars is False
                after_substitution(config_dict, blob_service)
                raise RuntimeError("stop after admitted rows")

            mock_load.side_effect = inspect_substituted
        lease: SessionOperationLease | None = None
        try:
            lease = agreement._execute_lease(loop, session_service.run.session_id, service=service, execution_fixture=execution_fixture)
            try:
                service._run_pipeline(
                    str(run_id or uuid4()),
                    pipeline_yaml,
                    threading.Event(),
                    session_operation_lease=lease,
                    resume_existing=resume_existing,
                )
            except Exception as exc:
                raised = exc
        finally:
            agreement._close_direct_runtime_probe(loop, lease, execution_fixture, sys.exc_info()[1] or raised)
        if on_orchestrator is not None:
            on_orchestrator(mock_orchestrator.return_value)
    return raised, blob_service, session_service


def test_run_admission_refuses_non_text_marker_with_typed_code(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    raised, blob_service, session_service = _run_admission(
        tmp_path, "prompt_template", CreationModality.VERBATIM, execution_fixture, mime_type="application/pdf"
    )

    assert isinstance(raised, InlineBlobNotTextError)
    assert raised.error_code == "inline_blob_not_text"
    assert ".txt or .md" in str(raised)
    assert blob_service.link_blob_to_run_calls == []
    assert blob_service.read_blob_content_calls == []
    assert session_service.recorded_blob_inline_resolutions == []


@pytest.mark.parametrize("modality", _LLM_AUTHORED_MODALITIES, ids=lambda modality: modality.value)
@pytest.mark.parametrize("field", _GUARDED_FIELDS)
def test_run_admission_refuses_unapproved_agent_file_or_unsupported_position(
    tmp_path: Path, field: str, modality: CreationModality, execution_fixture: ExecutionTestCustody
) -> None:
    validate_result = _validate(tmp_path / "validate", field, modality)
    raised, blob_service, session_service = _run_admission(tmp_path / "run", field, modality, execution_fixture)

    if field in {"prompt_template", "system_prompt", "queries.q.template", "queries.0.template"}:
        assert _blob_check(validate_result).passed is True
        assert type(raised) is PromptFileReviewStale
        assert [call[0] for call in blob_service.link_blob_to_run_calls] == [_BLOB_ID]
        assert blob_service.read_blob_content_calls == [_BLOB_ID]
    else:
        assert _blob_check(validate_result).passed is False
        assert type(raised) is InlineBlobPromptSurfaceAdmissionError
        assert blob_service.link_blob_to_run_calls == []
        assert blob_service.read_blob_content_calls == []
    assert session_service.recorded_blob_inline_resolutions == []


@pytest.mark.parametrize("field", _GUARDED_FIELDS)
def test_run_admission_refuses_unapproved_user_file_or_unsupported_position(
    tmp_path: Path, field: str, execution_fixture: ExecutionTestCustody
) -> None:
    validate_result = _validate(tmp_path / "validate", field, CreationModality.VERBATIM)
    raised, blob_service, session_service = _run_admission(tmp_path / "run", field, CreationModality.VERBATIM, execution_fixture)

    if field in {"prompt_template", "system_prompt", "queries.q.template", "queries.0.template"}:
        assert _blob_check(validate_result).passed is True
        assert type(raised) is PromptFileReviewStale
        assert [call[0] for call in blob_service.link_blob_to_run_calls] == [_BLOB_ID]
        assert blob_service.read_blob_content_calls == [_BLOB_ID]
    else:
        assert _blob_check(validate_result).passed is False
        assert type(raised) is InlineBlobPromptSurfaceAdmissionError
        assert blob_service.link_blob_to_run_calls == []
        assert blob_service.read_blob_content_calls == []
    assert session_service.recorded_blob_inline_resolutions == []


def test_admitted_prompt_bytes_are_reused_for_later_rows(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    provider = Mock(spec=agreement.LLMProvider)
    provider.execute_query.return_value = agreement.LLMQueryResult(
        content="accepted",
        usage=agreement.TokenUsage.known(4, 1),
        model=_MODEL,
        finish_reason=agreement.FinishReason.STOP,
    )

    def run_two_rows(config_dict: dict[str, Any], blob_service: Any) -> None:
        prompt = config_dict["transforms"][0]["options"]["prompt_template"]
        assert prompt == _CONTENT.decode()
        transform = agreement.LLMTransform(
            {
                "provider": "openrouter",
                "model": _MODEL,
                "api_key": "test-key",
                "prompt_template": prompt,
                "schema": {"mode": "observed"},
                "required_input_fields": [],
            }
        )
        transform._provider = provider
        for index in range(2):
            context = agreement.make_context(
                state_id="state-123",
                run_id="run-123",
                token=agreement.TokenInfo(row_id=f"row-{index}", token_id=f"token-{index}", row_data=agreement.make_pipeline_row({})),
            )
            assert transform._process_row(agreement.make_pipeline_row({"text": str(index)}), context).status == "success"
            if index == 0:
                blob_service.content = b"Changed after admission"

    raised, blob_service, session_service = _run_admission(
        tmp_path,
        "prompt_template",
        CreationModality.LLM_GENERATED,
        execution_fixture,
        approved=True,
        after_substitution=run_two_rows,
    )
    assert str(raised) == "stop after admitted rows"
    assert blob_service.read_blob_content_calls == [_BLOB_ID]
    assert len(session_service.recorded_blob_inline_resolutions) == 1
    assert provider.execute_query.call_count == 2
    assert all(
        call.args[0] == [agreement.ChatMessage(role="user", content=_CONTENT.decode())] for call in provider.execute_query.call_args_list
    )


def test_final_admission_records_exact_prompt_input(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    from sqlalchemy import select

    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.core.landscape.factory import RecorderFactory
    from elspeth.core.landscape.schema import run_prompt_inputs_table
    from elspeth.web.execution.service import _run_prompt_input_evidence

    landscape_url = f"sqlite:///{tmp_path / 'landscape.db'}"
    run_id = uuid4()
    admitted: list[Any] = []
    with LandscapeDB(landscape_url) as db:
        raised, blob_service, _session_service = _run_admission(
            tmp_path,
            "prompt_template",
            CreationModality.LLM_GENERATED,
            execution_fixture,
            approved=True,
            landscape_probe=db,
            run_id=run_id,
            on_final_approval=admitted.append,
        )
        assert str(raised) == "stop after prompt input evidence"
        assert blob_service.read_blob_content_calls == [_BLOB_ID]
        assert len(admitted) == 1
        blob_service.content = b"changed after final admission"
    with LandscapeDB.from_url(landscape_url, create_tables=False) as db:
        RecorderFactory(db).run_lifecycle.begin_run(
            {}, "v1", run_id=str(run_id), run_prompt_inputs=_run_prompt_input_evidence(tuple(admitted))
        )
    with LandscapeDB.from_url(landscape_url, create_tables=False) as db, db.read_only_connection() as conn:
        row = conn.execute(select(run_prompt_inputs_table)).one()
    assert row.run_id == str(run_id)
    assert row.node_id == "classify"
    assert row.field_path == "prompt_template"
    assert row.blob_id == str(_BLOB_ID)
    assert row.content_sha256 == hashlib.sha256(_CONTENT).hexdigest()
    assert row.content_sha256 != hashlib.sha256(blob_service.content).hexdigest()


@pytest.mark.parametrize("start_bytes_changed", [True, False], ids=["reapproved_after_start", "same_bytes"])
def test_resume_refuses_prompt_bytes_that_differ_from_run_start_evidence(
    tmp_path: Path, execution_fixture: ExecutionTestCustody, start_bytes_changed: bool
) -> None:
    """A run started on approved bytes V1 cannot resume on re-approved bytes V2.

    The resume's fresh admission approves the current file content (V2); the
    Landscape holds the evidence ``begin_run`` wrote when the run started.
    Only identical evidence may reach ``orchestrator.resume``.

    One execution service per test, so the run-start header is seeded from the
    resume's own final approval, before the resume branch reads it: the control
    holds exactly the evidence the resume admits, the refusal case the same
    node, field and blob with V1's hash.
    """
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.core.landscape.factory import RecorderFactory
    from elspeth.web.execution.service import _run_prompt_input_evidence

    run_id = uuid4()
    admitted: list[tuple[Any, ...]] = []
    orchestrators: list[Any] = []
    with LandscapeDB(f"sqlite:///{tmp_path / 'landscape.db'}") as db:

        def seed_run_start(snapshot: Any) -> None:
            current = _run_prompt_input_evidence((snapshot,))
            admitted.append(current)
            if start_bytes_changed:
                started = tuple(replace(item, content_sha256=hashlib.sha256(b"prompt text v1").hexdigest()) for item in current)
            else:
                started = current
            RecorderFactory(db).run_lifecycle.begin_run({}, "v1", run_id=str(run_id), run_prompt_inputs=started)

        raised, _blob_service, _session_service = _run_admission(
            tmp_path,
            "prompt_template",
            CreationModality.LLM_GENERATED,
            execution_fixture,
            approved=True,
            landscape_probe=db,
            run_id=run_id,
            on_final_approval=seed_run_start,
            resume_existing=True,
            on_orchestrator=orchestrators.append,
        )

    [current] = admitted
    assert [(item.node_id, item.field_path, item.blob_id, item.content_sha256) for item in current] == [
        ("classify", "prompt_template", str(_BLOB_ID), _SHA256)
    ]
    [orchestrator] = orchestrators
    if start_bytes_changed:
        assert type(raised) is AuditIntegrityError
        assert str(raised) == "Resumed run prompt input differs from the run's start evidence"
        orchestrator.resume.assert_not_called()
    else:
        assert str(raised) == "stop at resume"
        orchestrator.resume.assert_called_once()
    orchestrator.run.assert_not_called()


@pytest.mark.parametrize("field", ["prompt_template", "system_prompt", "queries.q.template", "queries.q", "queries"])
def test_unresolved_prompt_blobs_keep_site_evidence_and_remove_stale_full_artifact(tmp_path: Path, field: str) -> None:
    from elspeth.web.interpretation_state import materialize_state_for_execution

    state = _state(tmp_path, field)
    node = state.nodes[0]
    original_reviews = node.options[INTERPRETATION_REQUIREMENTS_KEY]
    state = replace(state, nodes=(replace(node, options={**node.options, "approved_prompt_artifact_hash": "a" * 64}),))
    materialized = materialize_state_for_execution(state)
    assert isinstance(materialized, CompositionState)
    options = materialized.nodes[0].options
    assert "approved_prompt_artifact_hash" not in options
    assert options[INTERPRETATION_REQUIREMENTS_KEY] == original_reviews


def test_user_system_blob_substitution_does_not_mint_full_text_approval(tmp_path: Path) -> None:
    from elspeth.core.blobs_inline import _discover_blob_content_refs, _substitute_blob_content_refs
    from elspeth.web.interpretation_state import materialize_state_for_execution

    materialized = materialize_state_for_execution(_state(tmp_path, "system_prompt"))
    assert isinstance(materialized, CompositionState)
    config = yaml.safe_load(composer_yaml_generator.generate_yaml(materialized))
    refs = _discover_blob_content_refs(config)
    resolved, evidence = _substitute_blob_content_refs(
        config, dict.fromkeys(refs, _CONTENT), refs=refs, blob_metadata={_BLOB_ID: ("text/plain", len(_CONTENT))}
    )
    llm_options = resolved["transforms"][0]["options"]
    assert llm_options["system_prompt"] == _CONTENT.decode()
    assert "approved_prompt_artifact_hash" not in llm_options
    assert evidence[0].content_hash == _SHA256
    assert evidence[0].blob_id == _BLOB_ID


def test_unused_fallback_blob_does_not_hide_effective_reviewed_artifact() -> None:
    from elspeth.web.interpretation_state import approved_prompt_artifact_hash_from_options

    options = {
        "prompt_template": _marker(),
        "system_prompt": "Decorator",
        "queries": {"q": {"input_fields": {"text": "text"}, "template": "Q {{ row.text }}"}},
    }
    actual = approved_prompt_artifact_hash_from_options(options)
    assert actual is not None
    assert actual == approved_prompt_artifact_hash_from_options({**options, "prompt_template": "unused text"})


@pytest.mark.parametrize(
    "invalid",
    [
        {"system_prompt": 7},
        {"queries": {"q": {"template": 7}}},
        {"queries": 7},
    ],
)
def test_blob_source_does_not_hide_malformed_sibling_prompt_options(invalid: dict[str, Any]) -> None:
    from elspeth.web.interpretation_state import approved_prompt_artifact_hash_from_options

    with pytest.raises(ValueError):
        approved_prompt_artifact_hash_from_options({"prompt_template": _marker(), **invalid})
