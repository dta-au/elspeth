"""Approved file-backed prompts: the frozen revision's approvals and artifact hash, end to end.

B1: materialization drops ``approved_prompt_artifact_hash`` for a file marker
(the option-only derivation cannot read a blob), and substitution only inlines
text. The worker and the approval compile both restore it from the admitted
``PromptReviewSnapshot``, so provider-call audit rows carry it and the worker's
config hash agrees with the approved one.

B2: approval compilation asserts prompt-file approvals on the revision it froze,
never the session head, so an unapproved head cannot refuse approval of an
approved revision (and cannot lend that revision its hash).

Approvals are real (card resolution or editor save, on a live sessions DB);
``_run_pipeline`` and ``compile_approval_binding`` are the real service paths.
The substituted runtime options reach a real ``LLMTransform`` and OpenRouter
provider with only ``httpx.Client`` replaced, so the provider's own audit write
is what is observed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
import yaml

import tests.integration.pipeline.test_composer_runtime_agreement as agreement
from elspeth.contracts import CallType
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.identity import TokenInfo
from elspeth.core.prompt_artifact import approved_prompt_artifact_hash
from elspeth.plugins.infrastructure.batching.ports import CollectorOutputPort
from elspeth.plugins.transforms.llm.transform import LLMTransform
from elspeth.testing import make_pipeline_row
from elspeth.web.composer import yaml_generator as composer_yaml_generator
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.execution.errors import UnresolvedInterpretationPlaceholderError
from elspeth.web.execution.protocol import FrozenRunSettings
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.prompt_file_approval import _blob_record, _read_custody_bytes
from elspeth.web.sessions.prompt_file_review import PromptReviewSnapshot
from elspeth.web.sessions.protocol import CompositionStateRecord
from tests.fixtures.factories import make_context
from tests.fixtures.mock_audit import mock_audit_authority
from tests.helpers import execution_custody
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.plugins.llm.test_openrouter import InMemoryAuditWriter
from tests.unit.web.blobs.test_prompt_editor import _approve_card, _live_prompt_setup, _patch_and_save, _save, _save_editor_state
from tests.unit.web.blobs.test_routes import _upload_blob
from tests.unit.web.execution.test_service import _mock_pipeline_settings

execution_fixture = execution_custody.execution_fixture


class _StopAtProviderBoundary(RuntimeError):
    """Stops the worker once the provider call has been audited."""


@dataclass
class _ApprovedSession:
    client: Any
    service: Any
    blob_service: Any
    session_id: str
    blob_id: UUID
    text: str
    record: CompositionStateRecord


@contextmanager
def _openrouter_http_reply(content: str) -> Iterator[list[dict[str, Any]]]:
    """Replace only ``httpx.Client``: echo the request model, answer ``content``."""
    requests: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        body = kwargs["json"]
        requests.append(body)
        payload = {
            "id": "chatcmpl-b1",
            "object": "chat.completion",
            "created": 0,
            "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        }
        return httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", url),
        )

    class _Client:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def post(self, url: str, **kwargs: Any) -> httpx.Response:
            return post(url, **kwargs)

        @contextmanager
        def stream(self, method: str, url: str, **kwargs: Any) -> Iterator[httpx.Response]:
            assert method == "POST"
            yield post(url, **kwargs)

        def close(self) -> None:
            return None

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: Any) -> bool:
            return False

    with patch("httpx.Client", _Client):
        yield requests


def _audited_llm_call(options: dict[str, Any]) -> tuple[Any, LLMTransform]:
    """Run one row through a real transform built from runtime options; return its LLM audit call."""
    writer = InMemoryAuditWriter()
    transform = LLMTransform({**options, "api_key": "test-key"})
    transform.on_start(make_context(**mock_audit_authority("test-run"), landscape=writer))
    collector = CollectorOutputPort()
    transform.connect_output(collector, max_pending=1)
    ctx = make_context(
        **mock_audit_authority("test-run"),
        state_id="b1-state",
        token=TokenInfo(row_id="row-1", token_id="token-1", row_data=make_pipeline_row({})),
        landscape=writer,
    )
    try:
        with _openrouter_http_reply("accepted") as requests:
            transform.accept(make_pipeline_row({}), ctx)
            transform.flush_batch_processing(timeout=10.0)
        assert len(requests) == 1
    finally:
        transform.close()
    llm_calls = [call for call in writer.calls if call.call_type is CallType.LLM]
    assert len(llm_calls) == 1
    return llm_calls[0], transform


def _save_state(approved: _ApprovedSession, data: dict[str, Any]) -> CompositionStateRecord:
    _save_editor_state(approved.service, approved.blob_service, approved.session_id, data)
    record = asyncio.run(approved.service.get_current_state(UUID(approved.session_id)))
    assert record is not None
    return cast(CompositionStateRecord, record)


def _approved_session(tmp_path: Path, approval: str) -> _ApprovedSession:
    """A live session whose node ``first`` binds an approved prompt file (card or editor save).

    The fixture state names ``/tmp`` paths; the approved revision is re-saved
    with its source and output under ``tmp_path`` (prompt options untouched) so
    approval compilation can digest the source file as it does in production.
    """
    client, service, blob_service, session_id, uploaded, _second, events = _live_prompt_setup(tmp_path / "approval")
    text = "first before"
    if approval == "card":
        _approve_card(service, blob_service, session_id, events["first"])
    else:
        text = "owner edited prompt"
        saved = _save(client, session_id, uploaded["id"], text, uploaded["content_hash"])
        assert saved.status_code == 200, saved.text
    current = asyncio.run(service.get_current_state(UUID(session_id)))
    assert current is not None
    data = state_from_record(current).to_dict()
    source_path = tmp_path / "input.csv"
    source_path.write_text("text\nhello\n")
    for source in data["sources"].values():
        source["options"]["path"] = str(source_path)
    for output in data["outputs"]:
        output["options"]["path"] = str(tmp_path / "output.jsonl")
    approved = _ApprovedSession(client, service, blob_service, session_id, UUID(uploaded["id"]), text, current)
    approved.record = _save_state(approved, data)
    return approved


def _unapproved_head(approved: _ApprovedSession, tmp_path: Path) -> tuple[CompositionStateRecord, str]:
    """The agent rebinds node ``first`` to a newly uploaded file: a head whose prompt file is not approved."""
    head_text = "head never approved"
    second = _upload_blob(approved.client, approved.session_id, head_text.encode(), "head.txt", "text/plain")
    marker = {"blob_ref": second["id"], "mode": "inline_content", "sha256": second["content_hash"]}
    _patch_and_save(approved.service, approved.blob_service, approved.session_id, tmp_path, {"prompt_template": marker})
    head = asyncio.run(approved.service.get_current_state(UUID(approved.session_id)))
    assert head is not None and head.id != approved.record.id
    return cast(CompositionStateRecord, head), head_text


@dataclass
class _Harness:
    """One execution service per test (the custody fixture admits one registration)."""

    service: ExecutionServiceImpl
    session_service: Any
    loop: asyncio.AbstractEventLoop


def _harness(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> _Harness:
    service, session_service, loop = agreement.TestComposerRuntimeBlobInlineAgreement._execution_service(
        tmp_path / "service", execution_fixture=execution_fixture
    )
    service.set_openrouter_catalog_snapshot(sha256="b" * 64, source="bundled")
    return _Harness(service, session_service, loop)


def _compile_frozen(harness: _Harness, approved: _ApprovedSession, state_id: UUID) -> dict[str, Any]:
    """Run the real ``compile_approval_binding`` for ``state_id``; return what it froze and hashed.

    Approval compilation reads the live sessions DB and custody directly, as the
    approval route does, so those collaborators are the real ones for this call.
    """
    service = harness.service
    captured: dict[str, Any] = {"loaded": []}
    real_inputs = ExecutionServiceImpl._approval_inputs_from_frozen

    def inputs_spy(self_: ExecutionServiceImpl, frozen: FrozenRunSettings, **kwargs: Any) -> Any:
        captured["frozen"] = frozen
        return real_inputs(self_, frozen, **kwargs)

    def load(config_dict: dict[str, Any], *, expand_env_vars: bool) -> Any:
        assert expand_env_vars is False
        captured["loaded"].append(config_dict)
        return _mock_pipeline_settings()

    # Preflight (plugin/graph validation of the fixture state) is out of scope here;
    # what is under test is which revision approval is asserted on and what it binds.
    preflight = SimpleNamespace(is_valid=True, errors=(), readiness=SimpleNamespace(execution_ready=True, blockers=()))
    with (
        patch.object(service, "_session_service", approved.service),
        patch.object(service, "_blob_service", approved.blob_service),
        patch.object(service, "_yaml_generator", composer_yaml_generator),
        patch.object(service, "_call_async", asyncio.run),
        patch.object(ExecutionServiceImpl, "_approval_inputs_from_frozen", inputs_spy),
        patch.object(service, "_authoritative_state_preflight", autospec=True, return_value=preflight),
        patch("elspeth.web.execution.service.load_settings_from_config_dict", side_effect=load),
        patch("elspeth.web.operator_telemetry.apply_operator_pipeline_telemetry", side_effect=lambda settings, _web: settings),
        patch(
            "elspeth.web.execution.export_marking.apply_operator_export_marking",
            return_value=_mock_pipeline_settings().landscape.export,
        ),
        patch("elspeth.web.execution.service.audit_safe_resolved_config", side_effect=lambda *_a, **_k: captured["loaded"][-1]),
        patch("elspeth.web.execution.service.runtime_val_manifest_sha256", return_value="c" * 64),
        fenced_operation_context(approved.blob_service._engine, approved.session_id) as operation,
    ):
        captured["binding"] = asyncio.run(
            service.compile_approval_binding(UUID(approved.session_id), state_id, user_id="alice", session_operation_context=operation)
        )
    (captured["config"],) = captured["loaded"]
    return captured


def _run_worker(
    harness: _Harness,
    execution_fixture: ExecutionTestCustody,
    monkeypatch: pytest.MonkeyPatch,
    approved: _ApprovedSession,
    pipeline_yaml: str,
) -> dict[str, Any]:
    """Drive ``_run_pipeline`` for the approved revision; stop after the provider call is audited."""
    service, session_service, loop = harness.service, harness.session_service, harness.loop
    session_service.run.session_id = UUID(approved.session_id)
    session_service.run.state_id = approved.record.id
    session_service.prompt_approval_service = approved.service
    with approved.blob_service._engine.connect() as conn:
        blob_record = _blob_record(conn, UUID(approved.session_id), approved.blob_id)
    content = _read_custody_bytes(blob_record)
    assert content == approved.text.encode()
    cast(Any, service)._blob_service = agreement._FakeBlobService(blob_record=blob_record, content=content)

    real_admission = ExecutionServiceImpl._assert_state_prompt_file_approvals
    observed: dict[str, Any] = {"admitted": []}

    def admission_spy(self_: ExecutionServiceImpl, *args: Any, **kwargs: Any) -> tuple[PromptReviewSnapshot, ...]:
        snapshots = real_admission(self_, *args, **kwargs)
        observed["admitted"].append(snapshots)
        return snapshots

    monkeypatch.setattr(ExecutionServiceImpl, "_assert_state_prompt_file_approvals", admission_spy)

    def provider_boundary(config_dict: dict[str, Any], *, expand_env_vars: bool = True) -> None:
        assert expand_env_vars is False
        observed["config"] = config_dict
        (options,) = [dict(node["options"]) for node in config_dict["transforms"] if node["name"] == "first"]
        observed["options"] = options
        observed["call"], observed["transform"] = _audited_llm_call(options)
        raise _StopAtProviderBoundary

    lease: SessionOperationLease | None = None
    with (
        patch("elspeth.web.execution.service.open_landscape_db"),
        patch("elspeth.web.execution.service.FilesystemPayloadStore"),
        patch("elspeth.web.execution.service.load_settings_from_config_dict", side_effect=provider_boundary),
    ):
        try:
            lease = agreement._execute_lease(loop, session_service.run.session_id, service=service, execution_fixture=execution_fixture)
            with pytest.raises(_StopAtProviderBoundary):
                service._run_pipeline(str(uuid4()), pipeline_yaml, threading.Event(), session_operation_lease=lease)
        finally:
            agreement._close_direct_runtime_probe(loop, lease, execution_fixture, sys.exc_info()[1])
    monkeypatch.undo()
    return observed


def _first_options(config: dict[str, Any]) -> dict[str, Any]:
    (options,) = [node["options"] for node in config["transforms"] if node["name"] == "first"]
    return cast(dict[str, Any], options)


@pytest.mark.parametrize("approval", ["card", "editor_save"])
def test_governed_file_prompt_worker_config_matches_frozen_config_and_audits_hash(
    tmp_path: Path, execution_fixture: ExecutionTestCustody, approval: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(B1) The worker binds what the approval compile bound, and the provider audit carries it."""
    approved = _approved_session(tmp_path, approval)
    expected = approved_prompt_artifact_hash(prompt_template=approved.text, system_prompt=None)
    harness = _harness(tmp_path, execution_fixture)

    frozen = _compile_frozen(harness, approved, approved.record.id)
    # The defect's precondition: the frozen executable config itself carries no hash
    # for the file prompt; only admission can supply it.
    assert "approved_prompt_artifact_hash" not in _first_options(cast(dict[str, Any], frozen["frozen"].executable_config))
    assert _first_options(frozen["config"])["approved_prompt_artifact_hash"] == expected

    # The governed worker runs the frozen executable config.
    worker = _run_worker(harness, execution_fixture, monkeypatch, approved, yaml.safe_dump(deep_thaw(frozen["frozen"].executable_config)))
    (snapshot,) = worker["admitted"][-1]
    assert (snapshot.node_id, snapshot.effective_artifact_hash) == ("first", expected)
    # The provider's own audit write: what lands in calls.approved_prompt_artifact_hash.
    assert worker["call"].approved_prompt_artifact_hash == expected
    assert worker["transform"]._approved_prompt_artifact_hash == expected
    # Same transforms at both hash inputs, so no config-hash mismatch at the worker recheck.
    assert worker["config"]["transforms"] == frozen["config"]["transforms"]


def test_governed_approval_of_frozen_revision_is_not_refused_by_unapproved_head(
    tmp_path: Path, execution_fixture: ExecutionTestCustody
) -> None:
    """(B2) Approving V1 while the head V2 binds an unapproved file is allowed; V2 itself still refuses."""
    approved = _approved_session(tmp_path, "card")
    head, _head_text = _unapproved_head(approved, tmp_path)
    harness = _harness(tmp_path, execution_fixture)
    try:
        compiled = _compile_frozen(harness, approved, approved.record.id)
        assert compiled["binding"] is not None

        # Control: the head revision itself is still refused (its rebound file reopened review).
        with pytest.raises(UnresolvedInterpretationPlaceholderError):
            _compile_frozen(harness, approved, head.id)
    finally:
        harness.loop.close()


def test_frozen_revision_carries_its_own_effective_hash_not_the_heads(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    """(B1 x B2) With HEAD != the frozen revision, the bound hash is the frozen revision's."""
    approved = _approved_session(tmp_path, "card")
    _head, head_text = _unapproved_head(approved, tmp_path)
    harness = _harness(tmp_path, execution_fixture)
    try:
        compiled = _compile_frozen(harness, approved, approved.record.id)
    finally:
        harness.loop.close()

    bound = _first_options(compiled["config"])["approved_prompt_artifact_hash"]
    assert bound == approved_prompt_artifact_hash(prompt_template=approved.text, system_prompt=None)
    assert bound != approved_prompt_artifact_hash(prompt_template=head_text, system_prompt=None)
    assert _first_options(compiled["config"])["prompt_template"] == approved.text
