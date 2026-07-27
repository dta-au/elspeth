"""Admission and audit regressions for guided discovery dispatch."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.guided import _discovery as discovery_module
from elspeth.web.composer.guided._discovery import _execute_discovery_call
from elspeth.web.composer.guided.errors import GuidedSolverResponseShapeError
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import _dispatch as dispatch_module
from elspeth.web.composer.tools._common import ToolContext, ToolResult
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _catalog() -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]:
    catalog = MagicMock(spec=CatalogService)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return PolicyCatalogView.for_trained_operator(catalog, snapshot), snapshot


def _dispatch(
    name: str,
    arguments: dict[str, object],
    recorder: BufferingRecorder,
) -> dict[str, object]:
    return _dispatch_raw(name, json.dumps(arguments), recorder)


def _dispatch_raw(
    name: str,
    raw_arguments: object,
    recorder: BufferingRecorder,
) -> dict[str, object]:
    catalog, snapshot = _catalog()
    return _execute_discovery_call(
        tool_call=SimpleNamespace(
            id="guided-call-1",
            function=SimpleNamespace(name=name, arguments=raw_arguments),
        ),
        state=_empty_state(),
        catalog=catalog,
        plugin_snapshot=snapshot,
        secret_service=None,
        user_id=None,
        actor="guided-test",
        recorder=recorder,
    )


@pytest.mark.parametrize("raw_arguments", [{"component": "source"}, [], b"{}"])
def test_non_string_provider_arguments_record_one_arg_error_without_handler(
    raw_arguments: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(*args: object, **kwargs: object) -> ToolResult:
        del args, kwargs
        raise AssertionError("non-string provider arguments reached the handler")

    monkeypatch.setattr(discovery_module, "execute_tool", _explode)
    recorder = BufferingRecorder()

    with pytest.raises(GuidedSolverResponseShapeError):
        _dispatch_raw("get_pipeline_state", raw_arguments, recorder)

    assert len(recorder.invocations) == 1
    invocation = recorder.invocations[0]
    assert invocation.status == ComposerToolStatus.ARG_ERROR
    assert json.loads(invocation.arguments_canonical) == {
        "_actual_type": type(raw_arguments).__name__,
        "_argument_shape_error": "non_string",
    }


@pytest.mark.parametrize(
    ("raw_arguments", "expected_reason"),
    [
        ('{"secret":"sk-test-secret"', "invalid_json"),
        ("[]", "non_object"),
    ],
)
def test_malformed_provider_argument_shape_records_one_arg_error_without_handler(
    raw_arguments: str,
    expected_reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(*args: object, **kwargs: object) -> ToolResult:
        del args, kwargs
        raise AssertionError("malformed provider arguments reached the handler")

    monkeypatch.setattr(discovery_module, "execute_tool", _explode)
    recorder = BufferingRecorder()

    with pytest.raises(GuidedSolverResponseShapeError):
        _dispatch_raw("get_pipeline_state", raw_arguments, recorder)

    assert len(recorder.invocations) == 1
    invocation = recorder.invocations[0]
    assert invocation.status == ComposerToolStatus.ARG_ERROR
    assert "sk-test-secret" not in invocation.arguments_canonical
    assert json.loads(invocation.arguments_canonical)["_argument_shape_error"] == expected_reason


def test_schema_invalid_arguments_do_not_enter_handler_and_record_arg_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(
        arguments: dict[str, object],
        state: CompositionState,
        context: ToolContext,
    ) -> ToolResult:
        del arguments, state, context
        raise AssertionError("schema-invalid arguments reached the handler")

    monkeypatch.setattr(
        dispatch_module,
        "_DISCOVERY_TOOLS",
        {**dispatch_module._DISCOVERY_TOOLS, "get_pipeline_state": _explode},
    )
    recorder = BufferingRecorder()

    with pytest.raises(GuidedSolverResponseShapeError):
        _dispatch("get_pipeline_state", {"unexpected": "secret-value"}, recorder)

    assert len(recorder.invocations) == 1
    assert recorder.invocations[0].status == ComposerToolStatus.ARG_ERROR


def test_noncanonical_parsed_arguments_record_one_arg_error() -> None:
    recorder = BufferingRecorder()

    with pytest.raises(GuidedSolverResponseShapeError):
        _dispatch("get_pipeline_state", {"component": float("nan")}, recorder)

    assert len(recorder.invocations) == 1
    assert recorder.invocations[0].status == ComposerToolStatus.ARG_ERROR


def test_semantic_failure_records_one_success_disposition() -> None:
    recorder = BufferingRecorder()

    message = _dispatch("get_plugin_schema", {"plugin_type": "source", "name": "missing"}, recorder)

    assert message["role"] == "tool"
    assert len(recorder.invocations) == 1
    invocation = recorder.invocations[0]
    assert invocation.status == ComposerToolStatus.SUCCESS
    assert invocation.result_canonical is not None
    assert json.loads(invocation.result_canonical)["success"] is False


def test_success_records_one_success_disposition() -> None:
    recorder = BufferingRecorder()

    message = _dispatch("get_pipeline_state", {}, recorder)

    assert message["role"] == "tool"
    assert len(recorder.invocations) == 1
    invocation = recorder.invocations[0]
    assert invocation.status == ComposerToolStatus.SUCCESS
    assert invocation.result_canonical is not None
    assert json.loads(invocation.result_canonical)["success"] is True


@pytest.mark.parametrize("exc", [KeyError("handler defect"), RuntimeError("handler defect")])
def test_handler_exception_records_one_plugin_crash(
    exc: Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_handler_exception(
        arguments: dict[str, object],
        state: CompositionState,
        context: ToolContext,
    ) -> ToolResult:
        del arguments, state, context
        raise exc

    monkeypatch.setattr(
        dispatch_module,
        "_DISCOVERY_TOOLS",
        {**dispatch_module._DISCOVERY_TOOLS, "get_pipeline_state": _raise_handler_exception},
    )
    recorder = BufferingRecorder()

    with pytest.raises(type(exc), match="handler defect"):
        _dispatch("get_pipeline_state", {}, recorder)

    assert len(recorder.invocations) == 1
    assert recorder.invocations[0].status == ComposerToolStatus.PLUGIN_CRASH


def test_audit_result_canonicalization_failure_records_one_plugin_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _noncanonical_result(*args: object, **kwargs: object) -> ToolResult:
        del args, kwargs
        return ToolResult(
            success=True,
            updated_state=_empty_state(),
            validation=_empty_state().validate(),
            affected_nodes=(),
            data={"noncanonical": float("nan")},
        )

    monkeypatch.setattr(discovery_module, "execute_tool", _noncanonical_result)
    recorder = BufferingRecorder()

    with pytest.raises(ValueError):
        _dispatch("get_pipeline_state", {}, recorder)

    assert len(recorder.invocations) == 1
    assert recorder.invocations[0].status == ComposerToolStatus.PLUGIN_CRASH


def test_tool_message_serialization_failure_records_one_plugin_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(_result: ToolResult) -> str:
        raise RuntimeError("serialization defect")

    monkeypatch.setattr(discovery_module, "serialize_tool_result", _explode)
    recorder = BufferingRecorder()

    with pytest.raises(RuntimeError, match="serialization defect"):
        _dispatch("get_pipeline_state", {}, recorder)

    assert len(recorder.invocations) == 1
    assert recorder.invocations[0].status == ComposerToolStatus.PLUGIN_CRASH
