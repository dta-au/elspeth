"""Source-proof readiness at the real preview/Composer operation boundaries."""

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.blobs.protocol import BlobIntegrityError
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools.generation import (
    ComposerProofContext,
    MalformedSourceProofClaim,
    ProofEvaluated,
    ProofNotApplicable,
    ProofUnavailable,
    ResolvedProofBlob,
    compute_proof_diagnostics,
)
from elspeth.web.execution.schemas import ValidationCheck, ValidationReadiness, ValidationResult
from elspeth.web.sessions.models import blobs_table, sessions_table
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint, _make_llm_response, _make_settings
from tests.unit.web.composer.test_service import _composer_service_with_session, _create_session_blob_for_test, _session_engine_with_session
from tests.unit.web.composer.test_tools import _blob_operation, _mock_catalog, _stage1_valid_preview_state, execute_tool


def _passing_preflight(state: CompositionState) -> ValidationResult:
    assert state.sources
    return ValidationResult(
        is_valid=True,
        checks=[ValidationCheck(name="settings_load", passed=True, detail="Settings loaded.", affected_nodes=(), outcome_code=None)],
        errors=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )


@pytest.fixture
def proof_store(tmp_path: Path) -> Iterator[tuple[Engine, str, str, CompositionState, Path]]:
    engine, session_id = _session_engine_with_session()
    blob_id = _create_session_blob_for_test(
        engine=engine, session_id=session_id, data_dir=tmp_path, filename="values.csv", content="amount\n10\n"
    )
    with engine.connect() as connection:
        path = Path(connection.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == blob_id)).scalar_one())
    state = _stage1_valid_preview_state()
    state = replace(
        state,
        sources={
            "source": replace(
                state.sources["source"],
                options={
                    "path": str(path),
                    "blob_ref": blob_id,
                    "schema": {"mode": "observed"},
                },
            )
        },
    )
    try:
        yield engine, session_id, blob_id, state, path
    finally:
        engine.dispose()


def test_ready_proof_uses_actual_fenced_reader_once_and_outcomes_are_frozen(
    proof_store: tuple[Engine, str, str, CompositionState, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from elspeth.web.composer.tools import generation

    engine, session_id, blob_id, state, _path = proof_store
    original = generation._locked_read_ready_blob
    calls: list[str] = []

    def read(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(generation, "_locked_read_ready_blob", read)
    with _blob_operation(engine, session_id) as (authority, operation):
        context = ComposerProofContext(engine, session_id, str(tmp_path), operation, authority)
        # Shared claims are admitted independently but read one custody version.
        state = replace(state, sources={"source": state.sources["source"], "second": state.sources["source"]})
        proof = compute_proof_diagnostics(state, context=context)
    assert type(proof) is ProofEvaluated
    assert calls == [blob_id]
    assert all(diagnostic["severity"] != "blocking" for diagnostic in proof.diagnostics)
    absent = compute_proof_diagnostics(_stage1_valid_preview_state(), context=None)
    assert type(absent) is ProofNotApplicable
    unavailable = compute_proof_diagnostics(state, context=None)
    assert type(unavailable) is ProofUnavailable
    with pytest.raises(TypeError):
        unavailable.diagnostics[0]["evidence_locator"]["blob_id"] = "changed"


@pytest.mark.parametrize("status", ["pending", "error"])
def test_nonready_claim_never_reads_paths_or_integrity(
    proof_store: tuple[Engine, str, str, CompositionState, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    engine, session_id, blob_id, state, _path = proof_store
    with engine.begin() as connection:
        connection.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(status=status))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Nonready proof accessed a storage path")

    with _blob_operation(engine, session_id) as (authority, operation):
        monkeypatch.setattr(Path, "read_bytes", forbidden)
        monkeypatch.setattr(Path, "exists", forbidden)
        proof = compute_proof_diagnostics(state, context=ComposerProofContext(engine, session_id, str(tmp_path), operation, authority))
    assert type(proof) is ProofUnavailable
    assert proof.diagnostics[0]["code"] == "source_blob_unavailable"


def test_missing_and_foreign_claims_have_identical_public_diagnostics(
    proof_store: tuple[Engine, str, str, CompositionState, Path], tmp_path: Path
) -> None:
    engine, session_id, _blob_id, state, _path = proof_store
    with engine.begin() as connection:
        saved = dict(connection.execute(select(sessions_table).where(sessions_table.c.id == session_id)).mappings().one())
        foreign_id = str(uuid4())
        connection.execute(sessions_table.insert().values(**{**saved, "id": foreign_id}))
    foreign_blob_id = _create_session_blob_for_test(
        engine=engine,
        session_id=foreign_id,
        data_dir=tmp_path,
        filename="foreign-private.csv",
        content="private_column\nPRIVATE_SOURCE_CONTENT\n",
    )
    with engine.connect() as connection:
        foreign_path = connection.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == foreign_blob_id)).scalar_one()
    foreign_state = replace(
        state,
        sources={
            "source": replace(
                state.sources["source"],
                options={
                    **state.sources["source"].options,
                    "blob_ref": foreign_blob_id,
                },
            )
        },
    )
    with _blob_operation(engine, session_id) as (authority, operation):
        context = ComposerProofContext(engine, session_id, str(tmp_path), operation, authority)
        foreign = compute_proof_diagnostics(foreign_state, context=context)
        missing_id = str(uuid4())
        missing_state = replace(
            state,
            sources={
                "source": replace(
                    state.sources["source"],
                    options={
                        **state.sources["source"].options,
                        "blob_ref": missing_id,
                    },
                )
            },
        )
        missing = compute_proof_diagnostics(missing_state, context=context)
        from elspeth.web.composer.discovery_response import admit_discovery_result

        published = []
        for candidate in (foreign_state, missing_state):
            result = execute_tool(
                "preview_pipeline",
                {},
                candidate,
                _mock_catalog(),
                session_engine=engine,
                session_id=session_id,
                data_dir=str(tmp_path),
                session_operation_context=operation,
                session_operation_authority=authority,
                runtime_preflight=_passing_preflight,
            )
            assert result.success is True
            published.append(json.dumps(admit_discovery_result("preview_pipeline", result).to_dict(), sort_keys=True))
    assert type(foreign) is ProofUnavailable
    assert type(missing) is ProofUnavailable
    assert {key: value for key, value in foreign.diagnostics[0].items() if key != "evidence_locator"} == {
        key: value for key, value in missing.diagnostics[0].items() if key != "evidence_locator"
    }
    assert foreign.diagnostics[0]["evidence_locator"] == {"source": "blob", "blob_id": foreign_blob_id, "source_name": "source"}
    assert missing.diagnostics[0]["evidence_locator"] == {"source": "blob", "blob_id": missing_id, "source_name": "source"}
    assert published[0].replace(foreign_blob_id, missing_id) == published[1]
    assert all(private not in published[0] for private in (foreign_id, foreign_path, "foreign-private.csv", "PRIVATE_SOURCE_CONTENT"))


def test_malformed_later_claim_refuses_before_the_first_reader_call(
    proof_store: tuple[Engine, str, str, CompositionState, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from elspeth.web.composer.tools import generation

    engine, session_id, _blob_id, state, _path = proof_store
    state = replace(
        state,
        sources={
            "source": state.sources["source"],
            "later": replace(state.sources["source"], options={**state.sources["source"].options, "blob_ref": None}),
        },
    )
    calls: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append("reader")
        raise AssertionError("Reader ran before complete claim admission")

    monkeypatch.setattr(generation, "_locked_read_ready_blob", forbidden)
    with _blob_operation(engine, session_id) as (authority, operation), pytest.raises(MalformedSourceProofClaim) as raised:
        compute_proof_diagnostics(state, context=ComposerProofContext(engine, session_id, str(tmp_path), operation, authority))
    assert raised.value.source_name == "later"
    assert calls == []


def test_missing_bytes_reappearance_does_not_enable_unlocked_fallback(
    proof_store: tuple[Engine, str, str, CompositionState, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from elspeth.web.composer.tools import generation

    engine, session_id, _blob_id, state, path = proof_store
    body = path.read_bytes()
    path.unlink()
    original = generation._locked_read_ready_blob
    calls: list[str] = []

    def read(*args: Any, **kwargs: Any) -> Any:
        calls.append("reader")
        observed = original(*args, **kwargs)
        assert observed[0] is not None and observed[1] is None
        path.write_bytes(body)
        return observed

    monkeypatch.setattr(generation, "_locked_read_ready_blob", read)
    with _blob_operation(engine, session_id) as (authority, operation):
        proof = compute_proof_diagnostics(state, context=ComposerProofContext(engine, session_id, str(tmp_path), operation, authority))
    assert type(proof) is ProofUnavailable
    assert calls == ["reader"]
    assert path.read_bytes() == body


def test_hash_corruption_and_wrong_session_remain_typed_refusals(
    proof_store: tuple[Engine, str, str, CompositionState, Path], tmp_path: Path
) -> None:
    engine, session_id, _blob_id, state, path = proof_store
    path.write_bytes(b"amount\n99\n")
    with _blob_operation(engine, session_id) as (authority, operation):
        with pytest.raises(AuditIntegrityError, match="does not match"):
            ComposerProofContext(engine, str(uuid4()), str(tmp_path), operation, authority)
        with pytest.raises(BlobIntegrityError):
            compute_proof_diagnostics(state, context=ComposerProofContext(engine, session_id, str(tmp_path), operation, authority))
    with pytest.raises(TypeError, match="owned proof context"):
        compute_proof_diagnostics(state, context=lambda _: None)
    with pytest.raises(TypeError):
        ResolvedProofBlob(metadata={})


@pytest.mark.parametrize("fault", ["stale_fence", "unexpected_reader", "wrong_operation"])
def test_custody_and_unexpected_read_failures_never_become_unavailable(
    proof_store: tuple[Engine, str, str, CompositionState, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from elspeth.web.composer.tools import generation
    from elspeth.web.coordination.contracts import SessionOperationFenceLost

    engine, session_id, _blob_id, state, _path = proof_store
    with _blob_operation(engine, session_id) as (authority, operation):
        if fault == "wrong_operation":
            with pytest.raises(AuditIntegrityError):
                ComposerProofContext(engine, session_id, str(tmp_path), object(), authority)
            return
        if fault == "stale_fence":
            forged = replace(operation, fence=replace(operation.fence, operation_epoch=operation.fence.operation_epoch + 1))
            with pytest.raises(SessionOperationFenceLost):
                compute_proof_diagnostics(state, context=ComposerProofContext(engine, session_id, str(tmp_path), forged, authority))
            return
        original = RuntimeError("Controlled unexpected reader failure")

        def failed_reader(*args: Any, **kwargs: Any) -> Any:
            raise original

        monkeypatch.setattr(generation, "_locked_read_ready_blob", failed_reader)
        with pytest.raises(RuntimeError) as raised:
            compute_proof_diagnostics(state, context=ComposerProofContext(engine, session_id, str(tmp_path), operation, authority))
        assert raised.value is original


@pytest.mark.parametrize("entry", ["preview", "completion"])
@pytest.mark.parametrize("fault", ["wrong_nominal", "wrong_session"])
def test_incomplete_dependencies_cannot_hide_an_invalid_supplied_operation(
    proof_store: tuple[Engine, str, str, CompositionState, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    fault: str,
) -> None:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.composer.tools import generation

    engine, session_id, _blob_id, state, _path = proof_store
    completion = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path))._completion
    assert type(completion._assess_source_proof(state, session_id=session_id, session_operation_context=None)) is ProofUnavailable
    absent = execute_tool("preview_pipeline", {}, state, _mock_catalog(), runtime_preflight=_passing_preflight)
    assert absent.success is True and absent.data["preview_is_valid"] is False

    def forbidden_reader(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Invalid partial context reached a source reader")

    monkeypatch.setattr(generation, "_locked_read_ready_blob", forbidden_reader)
    with _blob_operation(engine, session_id) as (_authority, operation):
        supplied = object() if fault == "wrong_nominal" else operation
        requested_session = session_id if fault == "wrong_nominal" else str(uuid4())
        with pytest.raises(AuditIntegrityError):
            if entry == "completion":
                completion._assess_source_proof(state, session_id=requested_session, session_operation_context=supplied)
            else:
                execute_tool(
                    "preview_pipeline",
                    {},
                    state,
                    _mock_catalog(),
                    session_id=requested_session,
                    session_operation_context=supplied,
                    runtime_preflight=_passing_preflight,
                )


def test_owned_ready_metadata_corruption_keeps_its_integrity_failure(
    proof_store: tuple[Engine, str, str, CompositionState, Path],
) -> None:
    engine, _session_id, blob_id, _state, path = proof_store
    with engine.connect() as connection:
        metadata = dict(connection.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).mappings().one())
    content = path.read_bytes()
    verified = ResolvedProofBlob(
        metadata=metadata,
        verified_prefix=content,
        verified_content_hash=metadata["content_hash"],
        total_size_bytes=len(content),
    )
    assert verified.verified_prefix == content
    corrupt = {**metadata, "content_hash": None}
    with pytest.raises(AuditIntegrityError, match="null content_hash"):
        ResolvedProofBlob(
            metadata=corrupt,
            verified_prefix=content,
            verified_content_hash=metadata["content_hash"],
            total_size_bytes=len(content),
        )


@pytest.mark.parametrize("discriminator", ["valid_preview", "other_blocker", "wrong_blob", "wrong_source", "stale_state", "wrong_contract"])
def test_successful_preview_stop_requires_current_bound_unavailable_evidence(discriminator: str) -> None:
    from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult, admit_discovery_result, preview_source_proof_stop
    from elspeth.web.composer.tools._generation_responses import DIFF_PIPELINE_RESPONSE_CONTRACT

    state = _stage1_valid_preview_state()
    state = replace(
        state,
        sources={
            "source": replace(
                state.sources["source"],
                options={
                    **state.sources["source"].options,
                    "blob_ref": str(uuid4()),
                },
            )
        },
    )
    result = execute_tool("preview_pipeline", {}, state, _mock_catalog(), runtime_preflight=_passing_preflight)
    assert result.success is True
    admitted = admit_discovery_result("preview_pipeline", result)
    assert type(preview_source_proof_stop(admitted, state)) is ProofUnavailable
    wire = admitted.response.to_wire()
    if discriminator == "valid_preview":
        wire["preview_is_valid"] = True
    elif discriminator == "other_blocker":
        wire["proof_diagnostics"][0]["code"] = "source_inspection_failed"
    elif discriminator == "wrong_blob":
        wire["proof_diagnostics"][0]["evidence_locator"]["blob_id"] = str(uuid4())
    elif discriminator == "wrong_source":
        wire["proof_diagnostics"][0]["evidence_locator"]["source_name"] = "another_source"
    elif discriminator == "stale_state":
        state = replace(state, version=state.version + 1)
    if discriminator == "wrong_contract":
        admitted = AdmittedDiscoveryResult(result=result, response=admitted.response, contract=DIFF_PIPELINE_RESPONSE_CONTRACT)
    else:
        admitted = admit_discovery_result("preview_pipeline", replace(result, data=wire))
    assert preview_source_proof_stop(admitted, state) is None


def test_path_only_preview_needs_no_blob_context() -> None:
    state = _stage1_valid_preview_state()
    result = execute_tool("preview_pipeline", {}, state, _mock_catalog(), runtime_preflight=_passing_preflight)
    assert result.success is True
    assert result.updated_state is state
    assert result.validation.is_valid is True
    assert result.data["preview_is_valid"] is True
    assert result.data["proof_diagnostics"] == ()


def test_claimed_source_without_context_is_unavailable_not_proved() -> None:
    state = _stage1_valid_preview_state()
    source = state.sources["source"]
    blob_id = "ce901743-7f4b-49bf-a432-6d97136a38e9"
    state = replace(state, sources={"source": replace(source, options={**source.options, "blob_ref": blob_id})})
    result = execute_tool("preview_pipeline", {}, state, _mock_catalog(), runtime_preflight=_passing_preflight)
    assert result.success is True
    assert result.updated_state is state
    assert result.validation.is_valid is True
    assert result.runtime_preflight.is_valid is True
    assert result.data["preview_is_valid"] is False
    diagnostics = result.data["proof_diagnostics"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "source_blob_unavailable"
    assert diagnostics[0]["severity"] == "blocking"
    assert diagnostics[0]["evidence_locator"]["source_name"] == "source"
    assert diagnostics[0]["evidence_locator"]["blob_id"] == blob_id


@pytest.mark.parametrize("claim", [None, 17, "not-an-identifier", "CE901743-7F4B-49BF-A432-6D97136A38E9"])
def test_present_malformed_claim_is_saved_graph_refusal(claim: object) -> None:
    state = _stage1_valid_preview_state()
    source = state.sources["source"]
    state = replace(state, sources={"source": replace(source, options={**source.options, "blob_ref": claim})})
    result = execute_tool("preview_pipeline", {}, state, _mock_catalog(), runtime_preflight=_passing_preflight)
    assert result.success is False
    assert result.updated_state is state
    assert result.data is None
    assert result.affected_nodes == ()
    assert result.validation.is_valid is False
    assert result._state_validation_withheld is False
    rejection = result.validation.errors[0]
    assert rejection.component == "rejected_mutation"
    assert rejection.severity == "high"
    assert rejection.error_code == "source_blob_identity_invalid"
    assert rejection.rejected_component == "source"
    assert str(claim) not in rejection.message


@pytest.mark.parametrize(
    "discriminator",
    [
        "standing_only",
        "later_rejection",
        "low_severity",
        "wrong_code",
        "wrong_subject",
        "candidate",
        "affected",
        "stale",
        "canonical_claim",
        "wrong_contract",
    ],
)
def test_failed_preview_stop_requires_its_leading_current_saved_graph_rejection(discriminator: str) -> None:
    from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult, admit_discovery_result, preview_source_proof_stop
    from elspeth.web.composer.state import ValidationEntry
    from elspeth.web.composer.tools._generation_responses import DIFF_PIPELINE_RESPONSE_CONTRACT

    state = _stage1_valid_preview_state()
    state = replace(
        state,
        outputs=(),
        sources={
            "source": replace(
                state.sources["source"],
                options={
                    **state.sources["source"].options,
                    "blob_ref": "private-malformed-claim",
                },
            )
        },
    )
    standing = state.validate()
    assert standing.errors
    result = execute_tool("preview_pipeline", {}, state, _mock_catalog(), runtime_preflight=_passing_preflight)
    assert result.success is False
    assert result.validation.errors[0].severity == "high"
    assert result.validation.errors[1:] == standing.errors
    assert replace(result.validation, is_valid=standing.is_valid, errors=result.validation.errors[1:]) == standing
    admitted = admit_discovery_result("preview_pipeline", result)
    assert type(preview_source_proof_stop(admitted, state)) is MalformedSourceProofClaim
    assert "private-malformed-claim" not in json.dumps(admitted.to_dict())
    rejection = result.validation.errors[0]
    misleading = ValidationEntry(
        component="source", message="Existing finding.", severity="high", error_code="source_blob_identity_invalid"
    )
    if discriminator == "standing_only":
        result = replace(result, validation=replace(result.validation, errors=(misleading, *standing.errors)))
    elif discriminator == "later_rejection":
        result = replace(result, validation=replace(result.validation, errors=(misleading, *result.validation.errors)))
    elif discriminator in ("low_severity", "wrong_code", "wrong_subject"):
        changed = replace(
            rejection,
            **{
                "severity": "low" if discriminator == "low_severity" else rejection.severity,
                "error_code": "source_blob_unavailable" if discriminator == "wrong_code" else rejection.error_code,
                "rejected_component": "source:elsewhere" if discriminator == "wrong_subject" else rejection.rejected_component,
            },
        )
        result = replace(result, validation=replace(result.validation, errors=(changed, *standing.errors)))
    elif discriminator == "candidate":
        result = replace(result, _state_validation_withheld=True)
    elif discriminator == "affected":
        result = replace(result, affected_nodes=("source",))
    elif discriminator == "stale":
        result = replace(result, updated_state=replace(state, version=state.version + 1))
    elif discriminator == "canonical_claim":
        state = replace(
            state,
            sources={
                "source": replace(
                    state.sources["source"],
                    options={
                        **state.sources["source"].options,
                        "blob_ref": str(uuid4()),
                    },
                )
            },
        )
        result = replace(result, updated_state=state)
    if discriminator == "wrong_contract":
        admitted = AdmittedDiscoveryResult(result=result, response=None, contract=DIFF_PIPELINE_RESPONSE_CONTRACT)
    else:
        admitted = admit_discovery_result("preview_pipeline", result)
    assert preview_source_proof_stop(admitted, state) is None


@pytest.mark.asyncio
async def test_unavailable_preview_stops_issued_suffix_after_saving_completed_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from elspeth.web.composer import tool_batch

    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path))
    state = _stage1_valid_preview_state()
    source = state.sources["source"]
    state = replace(
        state,
        sources={"source": replace(source, options={**source.options, "blob_ref": "3206cfb5-6bec-4b67-81f0-de65835240b1"})},
    )
    calls: list[str] = []
    original_dispatch = tool_batch.execute_tool

    def observed_dispatch(tool_name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        calls.append(tool_name)
        if tool_name == "set_metadata" and arguments["patch"]["name"] == "Forbidden suffix":
            raise AssertionError("Unavailable preview must stop the issued authoring suffix")
        return original_dispatch(tool_name, arguments, *args, **kwargs)

    primary_calls = 0

    async def provider(**kwargs: Any) -> Any:
        nonlocal primary_calls
        primary_calls += 1
        assert primary_calls == 1, "Unavailable preview must close further primary calls"
        return _make_llm_response(
            content="I will inspect the saved source.",
            tool_calls=[
                {"id": "prefix", "name": "set_metadata", "arguments": {"patch": {"name": "Saved prefix"}}},
                {"id": "unavailable", "name": "preview_pipeline", "arguments": {}},
                {"id": "suffix", "name": "set_metadata", "arguments": {"patch": {"name": "Forbidden suffix"}}},
            ],
        )

    monkeypatch.setattr(tool_batch, "execute_tool", observed_dispatch)
    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service._preflight, "runtime_preflight", lambda state, *args, **kwargs: _passing_preflight(state))
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    result = await service.compose("Inspect the saved source and report its readiness.", [], state, session_id=session_id)
    assert primary_calls == 1
    assert calls == ["set_metadata", "preview_pipeline"]
    assert result.state.metadata.name == "Saved prefix"
    assert result.state.version == state.version + 1
    assert result.repair_turns_used == 0
    assert result.runtime_preflight is not None
    assert result.runtime_preflight.readiness.authoring_valid is True
    assert result.runtime_preflight.readiness.execution_ready is False
    assert result.runtime_preflight.readiness.completion_ready is False
    assert "source_blob_unavailable" in {error.error_code for error in result.runtime_preflight.errors}
    assert "proof_repair_exhausted" not in {error.error_code for error in result.runtime_preflight.errors}
    messages = await service._sessions_service.get_messages(UUID(session_id), limit=None, include_private_evidence=False)
    tool_rows = [row for row in messages if row.role == "tool"]
    assert [row.tool_call_id for row in tool_rows] == ["prefix", "unavailable"]
    assistant_rows = [row for row in messages if row.role == "assistant" and row.tool_calls]
    assert len(assistant_rows) == 1
    assert [call["id"] for call in assistant_rows[0].tool_calls] == ["prefix", "unavailable"]
    from elspeth.web.sessions.converters import state_from_record
    from elspeth.web.sessions.routes._helpers import _composer_chat_history

    record = await service._sessions_service.get_current_state(UUID(session_id))
    assert record is not None
    reloaded = state_from_record(record)
    assert reloaded.metadata.name == "Saved prefix"
    assert reloaded.version == record.version
    assert replace(reloaded, version=result.state.version) == result.state
    history = _composer_chat_history(messages)
    assert all(row["role"] != "tool" and "tool_calls" not in row for row in history)
    next_calls = 0

    async def next_request(**kwargs: Any) -> Any:
        nonlocal next_calls
        next_calls += 1
        assert next_calls == 1
        assert all(row["role"] != "tool" and "tool_calls" not in row for row in kwargs["messages"])
        return _make_llm_response(content="The saved draft still needs verified source content.")

    monkeypatch.setattr("litellm.acompletion", next_request)
    resumed = await service.compose(
        "Describe the saved draft without changing it.",
        history,
        reloaded,
        session_id=session_id,
        current_state_id=str(record.id),
    )
    assert next_calls == 1
    assert resumed.state is reloaded
    assert resumed.repair_turns_used == 0
    assert resumed.runtime_preflight.readiness.execution_ready is False
    assert primary_calls == 1


@pytest.mark.asyncio
async def test_malformed_preview_refuses_before_runtime_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path))
    state = _stage1_valid_preview_state()
    state = replace(
        state,
        sources={
            "source": replace(state.sources["source"], options={**state.sources["source"].options, "blob_ref": str(uuid4())}),
            "later": replace(state.sources["source"], options={**state.sources["source"].options, "blob_ref": None}),
        },
    )
    calls: list[str] = []

    async def provider(**kwargs: Any) -> Any:
        calls.append("primary")
        assert calls == ["primary"]
        return _make_llm_response(
            content="Inspecting the source.",
            tool_calls=[
                {"id": "malformed", "name": "preview_pipeline", "arguments": {}},
            ],
        )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Malformed saved claim reached runtime preflight")

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service._preflight, "runtime_preflight", forbidden)
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    result = await service.compose("Inspect the source.", [], state, session_id=session_id)
    assert calls == ["primary"]
    assert result.state is state
    assert result.repair_turns_used == 0
    assert result.runtime_preflight.errors[0].error_code == "source_blob_identity_invalid"
    assert result.runtime_preflight.errors[0].component_id == "source:later"


@pytest.mark.asyncio
@pytest.mark.parametrize("issued_preview", [False, True])
async def test_source_stop_end_deadline_preserves_convergence_and_completed_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issued_preview: bool
) -> None:
    from elspeth.web.composer.advisor_checkpoint import _AdvisorCheckpointComposeDeadlineExpired
    from elspeth.web.composer.protocol import ComposerConvergenceError

    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path))
    state = _stage1_valid_preview_state()
    state = replace(
        state,
        sources={
            "source": replace(
                state.sources["source"],
                options={
                    **state.sources["source"].options,
                    "blob_ref": str(uuid4()),
                },
            )
        },
    )
    primary_calls = 0
    phases: list[str] = []

    async def provider(**kwargs: Any) -> Any:
        nonlocal primary_calls
        primary_calls += 1
        assert primary_calls == 1
        return _make_llm_response(
            content="The source cannot be verified.",
            tool_calls=[
                {"id": "prefix", "name": "set_metadata", "arguments": {"patch": {"name": "Kept before deadline"}}},
                {"id": "preview", "name": "preview_pipeline", "arguments": {}},
            ]
            if issued_preview
            else None,
        )

    async def deadline(*args: Any, **kwargs: Any) -> Any:
        phases.append(kwargs["phase"])
        if kwargs["phase"] == "end":
            raise _AdvisorCheckpointComposeDeadlineExpired
        return await _clean_advisor_checkpoint(*args, **kwargs)

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service._preflight, "runtime_preflight", lambda state, *args, **kwargs: _passing_preflight(state))
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", deadline)
    with pytest.raises(ComposerConvergenceError) as raised:
        await service.compose("Inspect the saved source.", [], state, session_id=session_id)
    assert primary_calls == 1
    assert phases.count("end") == 1
    assert raised.value.budget_exhausted == "timeout"
    assert raised.value.max_turns == int(issued_preview)
    if issued_preview:
        assert raised.value.partial_state.version == state.version + 1
        assert raised.value.partial_state.metadata.name == "Kept before deadline"
    else:
        assert raised.value.partial_state is None
    messages = await service._sessions_service.get_messages(UUID(session_id), limit=None, include_private_evidence=True)
    assert [row.tool_call_id for row in messages if row.role == "tool"] == (["prefix", "preview"] if issued_preview else [])


@pytest.mark.asyncio
async def test_unavailable_source_keeps_an_independently_evaluated_source_objection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path))
    blob_id = _create_session_blob_for_test(
        engine=service._session_engine,
        session_id=session_id,
        data_dir=tmp_path,
        filename="columns.csv",
        content="amount,region\n10,north\n",
    )
    with service._session_engine.connect() as connection:
        path = connection.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == blob_id)).scalar_one()
    state = _stage1_valid_preview_state()
    state = replace(
        state,
        outputs=(*state.outputs, replace(state.outputs[0], name="missing_rows")),
        sources={
            "source": replace(
                state.sources["source"],
                options={
                    "path": path,
                    "blob_ref": blob_id,
                    "schema": {"mode": "fixed", "fields": ["amount: str"]},
                },
            ),
            "missing": replace(
                state.sources["source"],
                on_success="missing_rows",
                options={
                    **state.sources["source"].options,
                    "blob_ref": str(uuid4()),
                },
            ),
        },
    )
    assert state.validate().is_valid is True
    calls = 0

    async def provider(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        assert calls == 1, "Unavailable evidence must not initiate graph repair"
        return _make_llm_response(content="The sources need attention.")

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    result = await service.compose("Inspect the current sources without changing them.", [], state, session_id=session_id)
    assert calls == 1
    errors = {error.error_code for error in result.runtime_preflight.errors}
    assert "source_blob_unavailable" in errors
    assert "csv_fixed_schema_omits_observed_columns" in errors
    assert result.runtime_preflight.readiness.authoring_valid is False
    assert result.runtime_preflight.readiness.execution_ready is False
    assert result.runtime_preflight.readiness.completion_ready is False
    assert result.repair_turns_used == 0
    assert result.state is state


@pytest.mark.asyncio
@pytest.mark.parametrize("entrance", ["no_tools", "composition_cap", "discovery_cap", "staged_review"])
@pytest.mark.parametrize("end_verdict", ["clean", "flagged", "unavailable"])
async def test_all_terminal_entrances_preserve_the_source_stop_across_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrance: str, end_verdict: str
) -> None:
    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointVerdict
    from elspeth.web.sessions.protocol import CompositionStateData
    from tests.unit.web.composer.test_tools import _pending_prompt_template_llm_node

    service, session_id = _composer_service_with_session(
        catalog=_mock_catalog(),
        settings=_make_settings(
            data_dir=tmp_path,
            composer_max_composition_turns=1 if entrance == "composition_cap" else 5,
            composer_max_discovery_turns=1 if entrance == "discovery_cap" else 10,
        ),
    )
    state = _stage1_valid_preview_state()
    state = replace(
        state,
        sources={
            "source": replace(
                state.sources["source"],
                options={
                    **state.sources["source"].options,
                    "blob_ref": str(uuid4()),
                },
            )
        },
    )
    current_state_id = None
    if entrance == "staged_review":
        node = _pending_prompt_template_llm_node()
        node = replace(node, options={**node.options, "prompt_template": "Rate {{ row.colour }} as a 1-10 coolness scale"})
        state = replace(state, nodes=(node,), sources={"source": replace(state.sources["source"], on_success=node.input)})
        saved = state.to_dict()
        record = await service._sessions_service.save_composition_state(
            UUID(session_id),
            CompositionStateData(
                sources=saved["sources"],
                nodes=saved["nodes"],
                edges=saved["edges"],
                outputs=saved["outputs"],
                metadata_=saved["metadata"],
                is_valid=state.validate().is_valid,
            ),
            provenance="tool_call",
        )
        current_state_id = str(record.id)
    primary_calls = 0
    phases: list[str] = []

    async def provider(**kwargs: Any) -> Any:
        nonlocal primary_calls
        primary_calls += 1
        assert primary_calls == 1, "Source stop allowed an authoring or reply-only model call"
        if entrance == "composition_cap":
            calls = [{"id": "cap", "name": "set_metadata", "arguments": {"patch": {"name": "Kept at cap"}}}]
        elif entrance == "discovery_cap":
            calls = [{"id": "cap", "name": "preview_pipeline", "arguments": {}}]
        elif entrance == "staged_review":
            calls = [
                {
                    "id": "review",
                    "name": "request_interpretation_review",
                    "arguments": {
                        "affected_node_id": "summarize",
                        "kind": "vague_term",
                        "user_term": "cool",
                    },
                }
            ]
        else:
            calls = None
        return _make_llm_response(content="The draft needs source verification.", tool_calls=calls)

    async def review(*args: Any, **kwargs: Any) -> Any:
        phases.append(kwargs["phase"])
        if kwargs["phase"] != "end" or end_verdict == "clean":
            return await _clean_advisor_checkpoint(*args, **kwargs)
        if end_verdict == "flagged":
            return AdvisorCheckpointVerdict(
                ok=True,
                blocking=True,
                findings_text="A separate review objection.",
                publication_verdict="WITHHOLD",
                publication_binding=kwargs["publication_review"].binding,
            )
        return AdvisorCheckpointVerdict(
            ok=False, blocking=False, findings_text="Review unavailable.", failure_class="unavailable", retryable=False
        )

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service._preflight, "runtime_preflight", lambda state, *args, **kwargs: _passing_preflight(state))
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", review)
    result = await service.compose(
        "Review the current draft and its assumptions.",
        [],
        state,
        session_id=session_id,
        current_state_id=current_state_id,
    )
    assert primary_calls == 1
    assert phases.count("end") == 1
    assert result.runtime_preflight is not None
    assert "source_blob_unavailable" in {error.error_code for error in result.runtime_preflight.errors}
    assert "proof_repair_exhausted" not in {error.error_code for error in result.runtime_preflight.errors}
    assert result.runtime_preflight.readiness.execution_ready is False
    assert result.runtime_preflight.readiness.completion_ready is False
    assert result.repair_turns_used == 0
    if end_verdict != "clean":
        assert "The draft needs source verification." not in result.message
        assert "A separate review objection." not in result.message
        assert "The draft needs source verification." not in (result.raw_assistant_content or "")
    if entrance == "composition_cap":
        assert result.state.metadata.name == "Kept at cap"
        assert result.state.version == state.version + 1
    if entrance == "discovery_cap":
        assert result.state is state
        assert len(result.tool_invocations) == 1
        assert result.tool_invocations[0].tool_name == "preview_pipeline"
        assert result.tool_invocations[0].version_before == result.tool_invocations[0].version_after == state.version
    if entrance == "staged_review":
        assert result.tool_invocations[0].status.value == "success", result.tool_invocations[0]
        events = await service._sessions_service.list_interpretation_events(UUID(session_id), status="pending")
        assert any(event.affected_node_id == "summarize" and event.user_term == "cool" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("becomes_ready", [False, True])
async def test_root_transition_stop_keeps_early_end_actual_prefix_and_charges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    becomes_ready: bool,
) -> None:
    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.state import PipelineMetadata

    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path))
    blob_id = _create_session_blob_for_test(
        engine=service._session_engine,
        session_id=session_id,
        data_dir=tmp_path,
        filename="transition.csv",
        content="amount\n10\n",
    )
    initial = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    dispatched: list[str] = []
    phases: list[str] = []
    charges: list[tuple[int, int]] = []
    primary_calls = 0
    original = tool_batch.execute_tool
    classify = service._classify_and_budget_turn

    def dispatch(name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        dispatched.append(name)
        assert name != "set_metadata", "Observed unavailable evidence reopened the issued suffix"
        if name == "preview_pipeline":
            with service._session_engine.begin() as connection:
                connection.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(status="pending"))
        result = original(name, arguments, *args, **kwargs)
        assert result.success is True
        if name == "preview_pipeline" and becomes_ready:
            with service._session_engine.begin() as connection:
                connection.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(status="ready"))
        return result

    async def observed_classification(*args: Any, **kwargs: Any) -> Any:
        outcome = await classify(*args, **kwargs)
        charges.append((outcome.composition_turns_delta, outcome.discovery_turns_delta))
        return outcome

    async def provider(**kwargs: Any) -> Any:
        nonlocal primary_calls
        primary_calls += 1
        assert primary_calls == 1, "Source observation allowed another primary call"
        return _make_llm_response(
            content="I will check the bound source.",
            tool_calls=[
                {
                    "id": "source",
                    "name": "set_source_from_blob",
                    "arguments": {
                        "blob_id": blob_id,
                        "plugin": "csv",
                        "on_success": "out",
                        "on_validation_failure": "discard",
                        "options": {"schema": {"mode": "observed"}},
                    },
                },
                {
                    "id": "output",
                    "name": "set_output",
                    "arguments": {
                        "sink_name": "out",
                        "plugin": "json",
                        "on_write_failure": "discard",
                        "options": {
                            "path": "outputs/transition.json",
                            "schema": {"mode": "observed"},
                            "mode": "write",
                            "collision_policy": "auto_increment",
                        },
                    },
                },
                {"id": "preview", "name": "preview_pipeline", "arguments": {}},
                {"id": "suffix", "name": "set_metadata", "arguments": {"patch": {"name": "Must not run"}}},
            ],
        )

    async def review(*args: Any, **kwargs: Any) -> Any:
        phases.append(kwargs["phase"])
        return await _clean_advisor_checkpoint(*args, **kwargs)

    monkeypatch.setattr(tool_batch, "execute_tool", dispatch)
    monkeypatch.setattr(service, "_classify_and_budget_turn", observed_classification)
    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service._preflight, "runtime_preflight", lambda state, *args, **kwargs: _passing_preflight(state))
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", review)
    result = await service.compose("Build from the uploaded data, then check readiness.", [], initial, session_id=session_id)
    assert dispatched == ["set_source_from_blob", "set_output", "preview_pipeline"]
    assert primary_calls == 1
    assert phases == ["early", "end"]
    assert charges == [(1, 0)]
    assert result.repair_turns_used == 0
    assert [invocation.tool_call_id for invocation in result.tool_invocations] == ["source", "output", "preview"]
    assert [invocation.status.value for invocation in result.tool_invocations] == ["success"] * 3
    assert [(invocation.version_before, invocation.version_after) for invocation in result.tool_invocations] == [(1, 2), (2, 3), (3, 3)]
    assert len(result.llm_calls) == 1
    assert result.runtime_preflight.readiness.execution_ready is False
    assert {error.error_code for error in result.runtime_preflight.errors} == {"source_blob_unavailable"}
    rows = await service._sessions_service.get_messages(UUID(session_id), limit=None, include_private_evidence=True)
    assert [row.tool_call_id for row in rows if row.role == "tool"] == ["source", "output", "preview"]
    assert [[call["id"] for call in row.tool_calls] for row in rows if row.role == "assistant" and row.tool_calls] == [
        ["source", "output", "preview"]
    ]
    with service._session_engine.connect() as connection:
        assert connection.execute(select(blobs_table.c.status).where(blobs_table.c.id == blob_id)).scalar_one() == (
            "ready" if becomes_ready else "pending"
        )


@pytest.mark.asyncio
async def test_evaluated_proof_exhaustion_still_surfaces_required_backend_cards(
    proof_store: tuple[Engine, str, str, CompositionState, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.sessions.protocol import CompositionStateData
    from tests.unit.web.composer.test_service import _test_sessions_service
    from tests.unit.web.composer.test_tools import _pending_prompt_template_llm_node

    engine, session_id, _blob_id, state, _path = proof_store
    service = ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(),
        settings=_make_settings(data_dir=tmp_path),
        sessions_service=_test_sessions_service(engine, tmp_path),
        session_engine=engine,
    )
    prompt = "Summarize the amount in {{ row.amount }}."
    node = _pending_prompt_template_llm_node()
    node = replace(
        node,
        options={
            "profile": "default",
            "required_input_fields": ["amount"],
            "prompt_template": prompt,
            "interpretation_requirements": [
                {
                    "id": "prompt-review",
                    "kind": "llm_prompt_template",
                    "user_term": "llm_prompt_template:summarize",
                    "status": "pending",
                    "draft": prompt,
                }
            ],
            "schema": {"mode": "observed"},
        },
    )
    state = replace(
        state,
        nodes=(node,),
        sources={
            "source": replace(
                state.sources["source"],
                on_success=node.input,
                options={**state.sources["source"].options, "schema": {"mode": "fixed", "fields": []}},
            )
        },
    )
    saved = state.to_dict()
    record = await service._sessions_service.save_composition_state(
        UUID(session_id),
        CompositionStateData(
            sources=saved["sources"],
            nodes=saved["nodes"],
            edges=saved["edges"],
            outputs=saved["outputs"],
            metadata_=saved["metadata"],
            is_valid=state.validate().is_valid,
        ),
        provenance="tool_call",
    )
    calls = 0
    phases: list[str] = []

    async def provider(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        assert calls <= 3
        return _make_llm_response(content="The draft remains unchanged.")

    async def review(*args: Any, **kwargs: Any) -> Any:
        phases.append(kwargs["phase"])
        return await _clean_advisor_checkpoint(*args, **kwargs)

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service._preflight, "runtime_preflight", lambda state, *args, **kwargs: _passing_preflight(state))
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", review)
    result = await service.compose(
        "Review this draft without changing it.", [], state, session_id=session_id, current_state_id=str(record.id)
    )
    assert calls == 3
    assert phases == ["end"]
    assert result.repair_turns_used == 2
    assert "proof_repair_exhausted" in {error.error_code for error in result.runtime_preflight.errors}
    events = await service._sessions_service.list_interpretation_events(UUID(session_id), status="pending")
    assert any(event.affected_node_id == node.id and event.kind == "llm_prompt_template" for event in events)
    assert result.runtime_preflight.readiness.execution_ready is False


@pytest.mark.asyncio
@pytest.mark.parametrize("next_kind", ["compose_message", "compose_recompose"])
async def test_detached_source_stop_reloads_prefix_for_message_and_recompose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    next_kind: str,
) -> None:
    import httpx
    from pydantic import SecretStr

    from elspeth.web.composer.availability import compute_availability
    from elspeth.web.composer.prompt_guard import PromptGuardMount
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.prompt_guard_config import PromptGuardSettings
    from elspeth.web.sessions.protocol import CompositionStateData
    from elspeth.web.sessions.schemas import MessageWithStateResponse
    from tests.unit.web.composer.test_compose_loop_llm_audit import _make_llm_response as audited_response
    from tests.unit.web.sessions.test_routes import _make_app, _save_test_composition_state
    from tests.unit.web.sessions.test_run_composer_turn import _run, _running_job

    app, sessions = _make_app(tmp_path)
    snapshot_factory = app.state.plugin_snapshot_factory
    guard_settings = PromptGuardSettings(endpoint="https://source-shield.example.test", subscription_key="offline-source-shield")
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_prompt_guard_mode": "required",
            "composer_prompt_guard": guard_settings,
            "composer_advisor_prompt_guard": guard_settings,
            "composer_endpoint_base_url": "https://primary.example.test/v1",
            "composer_endpoint_api_key": SecretStr("offline-primary-provider"),
            "composer_advisor_endpoint_base_url": "https://advisor.example.test/v1",
            "composer_advisor_endpoint_api_key": SecretStr("offline-advisor-provider"),
        }
    )
    shield_requests: list[httpx.Request] = []

    def shield_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.host == "source-shield.example.test"
        assert request.url.path == "/contentsafety/text:shieldPrompt"
        assert request.url.params["api-version"] == "2024-09-01"
        payload = json.loads(request.content)
        assert type(payload) is dict and set(payload) == {"userPrompt", "documents"}
        assert payload["userPrompt"] in {"Inspect this saved source.", "Explain the saved draft."}
        assert type(payload["documents"]) is list and all(type(document) is str for document in payload["documents"])
        shield_requests.append(request)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in payload["documents"]],
            },
        )

    def mount(settings: PromptGuardSettings | None) -> PromptGuardMount:
        assert type(settings) is PromptGuardSettings
        return PromptGuardMount(AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(shield_response)))

    # Replace only Azure HTTP; the worker owns each real user scope, fence,
    # required-work binding, guard audit, physical SDK call and bound END.
    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", mount, raising=True)
    monkeypatch.setattr("elspeth.web.composer.service.build_prompt_guard_mount", mount, raising=True)
    # Restore actual configuration readiness over the surrounding unit fixture.
    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", compute_availability, raising=True)
    service = ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(),
        settings=app.state.settings,
        sessions_service=sessions,
        session_engine=app.state.session_engine,
    )
    assert service.get_availability().available is True
    app.state.composer_service = service
    session = await sessions.create_session("alice", "Source inspection", "local")
    state = _stage1_valid_preview_state()
    state = replace(
        state,
        sources={
            "source": replace(
                state.sources["source"],
                options={
                    "path": str(tmp_path / "missing.csv"),
                    "blob_ref": str(uuid4()),
                    "schema": {"mode": "observed"},
                },
            )
        },
    )
    payload = state.to_dict()
    await _save_test_composition_state(
        sessions,
        session.id,
        CompositionStateData(
            sources=payload["sources"],
            nodes=payload["nodes"],
            edges=payload["edges"],
            outputs=payload["outputs"],
            metadata_=payload["metadata"],
            is_valid=True,
        ),
        provenance="session_seed",
    )
    primary_calls: list[dict[str, Any]] = []
    end_calls: list[dict[str, Any]] = []

    async def provider(**kwargs: Any) -> Any:
        if "response_format" in kwargs:
            assert kwargs["response_format"]["json_schema"]["name"] == "advisor_end_response"
            end_calls.append(kwargs)
            return audited_response(
                content=json.dumps(
                    {
                        "verdict": "CLEAN",
                        "category": "other",
                        "steps": [],
                        "findings": "",
                        "note": None,
                        "publication_verdict": "ADMIT",
                    }
                )
            )
        primary_calls.append(kwargs)
        assert len(primary_calls) <= 2, "Source stop reopened this request's primary authoring"
        assert all(message["role"] != "tool" and "tool_calls" not in message for message in kwargs["messages"])
        if len(primary_calls) == 1:
            return audited_response(
                content="I will inspect the saved draft.",
                tool_calls=[
                    {"id": "prefix", "name": "set_metadata", "arguments": {"patch": {"name": "Preserved prefix"}}},
                    {"id": "preview", "name": "preview_pipeline", "arguments": {}},
                    {"id": "suffix", "name": "set_metadata", "arguments": {"patch": {"name": "Forbidden suffix"}}},
                ],
            )
        return audited_response(content="The saved draft still needs source verification.")

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service._preflight, "runtime_preflight", lambda state, *args, **kwargs: _passing_preflight(state))
    async with _running_job(app, sessions, session.id, kind="compose_message", content="Inspect this saved source.") as first:
        completed = await _run(app, first)
        result = first.observation.compose_result
    assert completed.status == "completed"
    assert result is not None
    assert result.repair_turns_used == 0
    assert len(primary_calls) == 1 and len(end_calls) == 1
    assert len(result.llm_calls) == 2
    assert [(call.prompt_tokens, call.completion_tokens) for call in result.llm_calls] == [(11, 7), (11, 7)]
    response = MessageWithStateResponse.model_validate_json(completed.result_json)
    assert "source" in response.message.content.lower()
    before = await sessions.get_messages(session.id, limit=None, include_private_evidence=True)
    prefix_tools = [row for row in before if row.role == "tool"]
    assert [row.tool_call_id for row in prefix_tools] == ["prefix", "preview"]
    assert [[call["id"] for call in row.tool_calls] for row in before if row.role == "assistant" and row.tool_calls] == [
        ["prefix", "preview"]
    ]
    assert (await sessions.get_current_state(session.id)).metadata_["name"] == "Preserved prefix"
    if next_kind == "compose_message":
        kwargs = {"content": "Explain the saved draft."}
    else:
        # Recompose legitimately retries an unanswered user row; retrying the
        # completed first turn would correctly fail its start precondition.
        retry = await sessions.add_message(session.id, "user", "Explain the saved draft.", writer_principal="route_user_message")
        kwargs = {"expected_user_message_id": retry.id}
    # The existing one-job harness wraps its callable on entry. Restore the
    # same actual factory before using that harness for another admitted job.
    app.state.plugin_snapshot_factory = snapshot_factory
    async with _running_job(app, sessions, session.id, kind=next_kind, **kwargs) as next_job:
        next_completed = await _run(app, next_job)
        next_result = next_job.observation.compose_result
    assert next_completed.status == "completed"
    assert next_result is not None and next_result.state.metadata.name == "Preserved prefix"
    assert next_result.repair_turns_used == 0
    assert len(primary_calls) == 2 and len(end_calls) == 2
    assert len(next_result.llm_calls) == 2
    after = await sessions.get_messages(session.id, limit=None, include_private_evidence=True)
    assert after[: len(before)] == before
    assert [row.id for row in after if row.role == "tool"] == [row.id for row in prefix_tools]
    assert "Forbidden suffix" not in json.dumps(primary_calls[1]["messages"])
    checks = [
        row.tool_calls[0]["check"]
        for row in after
        if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == "prompt_guard_audit"
    ]
    assert [check["role"] for check in checks] == ["composer", "advisor", "composer", "advisor"]
    assert all(
        (check["adapter"], check["mode"], check["disposition"], check["reason"])
        == ("azure_prompt_shields", "required", "allow", "clean")
        for check in checks
    )
    assert checks[0]["operation_id"] == checks[1]["operation_id"]
    assert checks[2]["operation_id"] == checks[3]["operation_id"] != checks[0]["operation_id"]
    assert {json.loads(request.content)["userPrompt"] for request in shield_requests} == {
        "Inspect this saved source.",
        "Explain the saved draft.",
    }


@pytest.mark.asyncio
async def test_unavailable_after_advisor_repair_keeps_hidden_prose_private(
    proof_store: tuple[Engine, str, str, CompositionState, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.composer.service import ComposerServiceImpl
    from tests.unit.web.composer.test_compose_loop_llm_audit import _make_llm_response as audited_response
    from tests.unit.web.composer.test_service import _test_sessions_service

    engine, session_id, blob_id, state, _path = proof_store
    service = ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(),
        settings=_make_settings(data_dir=tmp_path, composer_advisor_checkpoint_max_passes=2),
        sessions_service=_test_sessions_service(engine, tmp_path),
        session_engine=engine,
    )
    primary_calls = 0
    end_calls = 0
    hidden = "PRIVATE_REPAIR_FINDING_49D8"
    terminal = "PRIVATE_REPAIR_REPLY_4AC2"

    async def provider(**kwargs: Any) -> Any:
        nonlocal primary_calls, end_calls
        if "response_format" in kwargs:
            end_calls += 1
            return audited_response(
                content=json.dumps(
                    {
                        "verdict": "FLAGGED",
                        "category": "other",
                        "steps": [],
                        "findings": hidden,
                        "note": None,
                        "publication_verdict": "WITHHOLD",
                    }
                )
            )
        primary_calls += 1
        assert primary_calls <= 2
        if primary_calls == 1:
            return audited_response(content="I have reviewed the current source.")
        assert hidden in json.dumps(kwargs["messages"])
        with engine.begin() as connection:
            connection.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(status="pending"))
        return audited_response(content=terminal, tool_calls=[{"id": "preview", "name": "preview_pipeline", "arguments": {}}])

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service._preflight, "runtime_preflight", lambda state, *args, **kwargs: _passing_preflight(state))
    result = await service.compose("Explain the draft without changing it.", [], state, session_id=session_id)
    assert primary_calls == 2 and end_calls == 2
    assert len(result.llm_calls) == 4
    assert result.state is state
    assert result.repair_turns_used == 0
    assert "source_blob_unavailable" in {error.error_code for error in result.runtime_preflight.errors}
    assert result.runtime_preflight.readiness.execution_ready is False
    assert hidden not in result.message and terminal not in result.message
    assert hidden not in (result.raw_assistant_content or "") and terminal not in (result.raw_assistant_content or "")
    rows = await service._sessions_service.get_messages(UUID(session_id), limit=None, include_private_evidence=False)
    assert all(hidden not in row.content and terminal not in row.content for row in rows if row.role == "assistant")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ["persist", "early_error", "early_cancel"])
async def test_source_stop_preserves_original_failure_and_completed_p4_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
) -> None:
    import asyncio

    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path))
    state = _stage1_valid_preview_state()
    state = replace(
        state,
        sources={
            "source": replace(
                state.sources["source"],
                options={
                    **state.sources["source"].options,
                    "blob_ref": str(uuid4()),
                },
            )
        },
    )
    original = (
        asyncio.CancelledError("Controlled EARLY cancellation")
        if failure_site == "early_cancel"
        else AuditIntegrityError("Controlled publication failure")
    )
    persisted: list[tuple[str, ...]] = []
    primary_calls = 0
    persist = service._persist_turn_audit

    async def provider(**kwargs: Any) -> Any:
        nonlocal primary_calls
        primary_calls += 1
        assert primary_calls == 1
        return _make_llm_response(
            content="Inspecting the source.",
            tool_calls=[
                {"id": "prefix", "name": "set_metadata", "arguments": {"patch": {"name": "Kept prefix"}}},
                {"id": "preview", "name": "preview_pipeline", "arguments": {}},
                {"id": "suffix", "name": "set_metadata", "arguments": {"patch": {"name": "Forbidden suffix"}}},
            ],
        )

    async def observed_persist(**kwargs: Any) -> Any:
        ids = tuple(outcome.call.id for outcome in kwargs["tool_outcomes"])
        assert ids == ("prefix", "preview")
        if failure_site == "persist":
            raise original
        result = await persist(**kwargs)
        persisted.append(ids)
        return result

    async def early(*args: Any, **kwargs: Any) -> Any:
        if failure_site != "persist":
            raise original

    async def forbidden_end(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Source readiness swallowed an earlier owned failure")

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service, "_persist_turn_audit", observed_persist)
    monkeypatch.setattr(service._advisor_checkpoint, "_maybe_run_early_checkpoint", early)
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", forbidden_end)
    monkeypatch.setattr(service._preflight, "runtime_preflight", lambda state, *args, **kwargs: _passing_preflight(state))
    with pytest.raises(type(original)) as raised:
        await service.compose("Inspect this source.", [], state, session_id=session_id)
    assert raised.value is original
    assert primary_calls == 1
    assert persisted == ([] if failure_site == "persist" else [("prefix", "preview")])
    rows = await service._sessions_service.get_messages(UUID(session_id), limit=None, include_private_evidence=True)
    assert [row.tool_call_id for row in rows if row.role == "tool"] == ([] if failure_site == "persist" else ["prefix", "preview"])


@pytest.mark.asyncio
async def test_unavailable_source_preserves_a_verified_configuration_objections_node_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.web.execution.test_service import _proof_gate_state

    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path))
    blob_id = _create_session_blob_for_test(
        engine=service._session_engine,
        session_id=session_id,
        data_dir=tmp_path,
        filename="amounts.csv",
        content="amount\n750.00\n",
    )
    with service._session_engine.connect() as connection:
        path = Path(connection.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == blob_id)).scalar_one())
    state = _proof_gate_state(source_path=path, blob_id=UUID(blob_id))
    state = replace(
        state,
        sources={
            **state.sources,
            "missing": replace(
                state.sources["source"],
                on_success="unavailable_rows",
                options={**state.sources["source"].options, "blob_ref": str(uuid4())},
            ),
        },
        outputs=(*state.outputs, replace(state.outputs[0], name="unavailable_rows")),
    )
    monkeypatch.setattr("litellm.acompletion", lambda **kwargs: _async_response())
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", _clean_advisor_checkpoint)

    async def _async_response() -> Any:
        return _make_llm_response(content="The source configuration needs attention.")

    result = await service.compose("Inspect the current source configuration.", [], state, session_id=session_id)
    objections = [
        error for error in result.runtime_preflight.errors if error.error_code == "gate_expression_type_mismatch_against_source_schema"
    ]
    assert len(objections) == 1
    assert objections[0].component_id == "amount_gate"
    assert objections[0].component_type == "gate"
    assert result.runtime_preflight.readiness.authoring_valid is False
    assert result.repair_turns_used == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("issued_preview", [False, True])
@pytest.mark.parametrize("claim", [None, 17, "bad-claim", "CE901743-7F4B-49BF-A432-6D97136A38E9"])
async def test_malformed_claim_terminal_is_authoring_invalid_without_source_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claim: object, issued_preview: bool
) -> None:
    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path))
    state = _stage1_valid_preview_state()
    state = replace(
        state,
        sources={"source": replace(state.sources["source"], options={**state.sources["source"].options, "blob_ref": claim})},
    )
    assert state.validate().is_valid is True
    primary_calls = 0
    phases: list[str] = []

    async def provider(**kwargs: Any) -> Any:
        nonlocal primary_calls
        primary_calls += 1
        assert primary_calls == 1, "Malformed claim must not initiate another primary call"
        return _make_llm_response(
            content="The saved source needs attention.",
            tool_calls=[
                {"id": "preview", "name": "preview_pipeline", "arguments": {}},
                {"id": "suffix", "name": "set_metadata", "arguments": {"patch": {"name": "Forbidden suffix"}}},
            ]
            if issued_preview
            else None,
        )

    def forbidden_reader(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Malformed scalar reached source custody or runtime proof")

    async def review(*args: Any, **kwargs: Any) -> Any:
        phases.append(kwargs["phase"])
        return await _clean_advisor_checkpoint(*args, **kwargs)

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service._preflight, "runtime_preflight", forbidden_reader)
    monkeypatch.setattr("elspeth.web.composer.tools.generation._locked_read_ready_blob", forbidden_reader)
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", review)
    result = await service.compose("Inspect the saved source without changing it.", [], state, session_id=session_id)
    assert primary_calls == 1
    assert phases == ["end"]
    assert result.state is state
    assert result.repair_turns_used == 0
    rows = await service._sessions_service.get_messages(UUID(session_id), limit=None, include_private_evidence=True)
    assert [row.tool_call_id for row in rows if row.role == "tool"] == (["preview"] if issued_preview else [])
    assert result.runtime_preflight.errors[0].error_code == "source_blob_identity_invalid"
    assert result.runtime_preflight.errors[0].component_id == "source"
    assert result.runtime_preflight.readiness.authoring_valid is False
    assert result.runtime_preflight.readiness.execution_ready is False
    assert result.runtime_preflight.readiness.completion_ready is False


@pytest.mark.parametrize("prior_valid", [None, True, False])
def test_malformed_claim_projection_clears_only_the_established_authoring_axis(prior_valid: bool | None) -> None:
    from elspeth.web.composer.composition_completion import _source_proof_unavailable_validation

    state = _stage1_valid_preview_state()
    prior = None if prior_valid is None else _passing_preflight(state)
    if prior is not None and prior_valid is False:
        prior = prior.model_copy(update={"is_valid": False, "readiness": prior.readiness.model_copy(update={"authoring_valid": False})})
    proof = MalformedSourceProofClaim("source", "The claimed blob identifier must be a canonical UUID string.")
    result = _source_proof_unavailable_validation(state, proof, prior)
    assert result.readiness.authoring_valid is False
    assert result.readiness.execution_ready is False
    assert result.readiness.completion_ready is False
    assert result.errors[0].error_code == "source_blob_identity_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_first", [False, True])
async def test_unavailable_source_preserves_a_verified_typed_outputs_objection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_first: bool
) -> None:
    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path))
    blob_id = _create_session_blob_for_test(
        engine=service._session_engine, session_id=session_id, data_dir=tmp_path, filename="amounts.csv", content="amount\n750\n"
    )
    with service._session_engine.connect() as connection:
        path = connection.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == blob_id)).scalar_one()
    state = _stage1_valid_preview_state()
    ready = replace(state.sources["source"], options={"path": path, "blob_ref": blob_id, "schema": {"mode": "observed"}})
    missing = replace(ready, on_success="missing_rows", options={**ready.options, "blob_ref": str(uuid4())})
    state = replace(
        state,
        sources={"missing": missing, "source": ready} if missing_first else {"source": ready, "missing": missing},
        outputs=(
            replace(state.outputs[0], options={**state.outputs[0].options, "schema": {"mode": "fixed", "fields": ["amount: int"]}}),
            replace(state.outputs[0], name="missing_rows"),
        ),
    )
    assert state.validate().is_valid is True
    primary_calls = 0
    phases: list[str] = []

    async def provider(**kwargs: Any) -> Any:
        nonlocal primary_calls
        primary_calls += 1
        assert primary_calls == 1, "Unavailable evidence must not reopen typed-output repair"
        return _make_llm_response(content="The output declaration needs attention.")

    async def review(*args: Any, **kwargs: Any) -> Any:
        phases.append(kwargs["phase"])
        return await _clean_advisor_checkpoint(*args, **kwargs)

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", review)
    result = await service.compose("Inspect the source and output declarations without changing them.", [], state, session_id=session_id)
    assert primary_calls == 1
    assert phases == ["end"]
    assert result.state is state
    assert result.repair_turns_used == 0
    objections = [
        error for error in result.runtime_preflight.errors if error.error_code == "declared_input_type_mismatch_against_source_schema"
    ]
    assert len(objections) == 1
    assert objections[0].component_id == "main"
    assert objections[0].component_type == "sink"
    blockers = [
        blocker
        for blocker in result.runtime_preflight.readiness.blockers
        if blocker.code == "declared_input_type_mismatch_against_source_schema"
    ]
    assert len(blockers) == 1
    assert blockers[0].component_id == "main"
    assert blockers[0].component_type == "sink"
    unavailable = [error for error in result.runtime_preflight.errors if error.error_code == "source_blob_unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0].component_id == "source:missing"
    assert unavailable[0].component_type == "source"
    assert result.runtime_preflight.readiness.authoring_valid is False
    assert result.runtime_preflight.readiness.execution_ready is False
    assert result.runtime_preflight.readiness.completion_ready is False
