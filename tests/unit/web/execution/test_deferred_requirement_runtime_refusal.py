"""A field requirement that only a file's bytes name is decided before a run exists.

Authoring cannot read two kinds of requirement when a file reference stands
where the field is named (deferred-contract audit 4.4 and its input-side rows):

- a ``text`` or ``document`` sink whose ``field`` holds a file reference does
  not construct, so the field it writes from is not required of its producer
  (a file at its ``path`` is refused outright at authoring and by both gates,
  below);
- a transform whose input-field option (``blob_ref_field``) holds a file
  reference does not construct, so the field it reads is not required of its
  upstream.

Authoring therefore reads each graph valid and says so with one
``contract_probe_deferred`` notice on that output or node. The two web gates
then decide the requirement on the real bytes, with the same state: the save
path (``save_path_preflight``) and run admission (``run_admission_refusal``,
which asserts no run record, no blob linked to a run and no sink file). Each
case has a control over a file that names a field the producer guarantees, so
each refusal is the field's and not the harness's.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, create_autospec, patch
from uuid import UUID

import pytest

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.blobs.protocol import BlobServiceProtocol
from elspeth.web.composer import yaml_generator as composer_yaml_generator
from elspeth.web.composer.state import CompositionState, OutputSpec, ValidationSummary
from elspeth.web.execution.errors import PipelineValidationError
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.execution.validation import validate_pipeline as _real_validate_pipeline
from elspeth.web.sessions.routes._helpers import _composer_persisted_validation
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.unit.web.composer import test_deferred_contract_notice as graphs
from tests.unit.web.execution import test_deferred_contract_runtime_refusal as runtime
from tests.unit.web.execution.test_service import _execute, _WebSettingsStub

_live_execute_lease = runtime._live_execute_lease
broadcaster = runtime.broadcaster
execution_fixture = runtime.execution_fixture
isolate_raw_sink_effect_eligibility_gate = runtime.isolate_raw_sink_effect_eligibility_gate
mock_loop = runtime.mock_loop
mock_session_service = runtime.mock_session_service
mock_settings = runtime.mock_settings
real_loop = runtime.real_loop
service = runtime.service

OBSERVED: dict[str, Any] = {"mode": "observed"}
ABSENT = "body"  # the producer guarantees id and k only
PRESENT = "k"
_TEXT = "text/plain"


def _outputs_dir(tmp_path: Path) -> Path:
    outputs = tmp_path / "outputs" / str(graphs.SESSION_ID)
    outputs.mkdir(parents=True, exist_ok=True)
    return outputs


def _sink_path(tmp_path: Path, plugin: str) -> str:
    return str(_outputs_dir(tmp_path) / ("o.txt" if plugin == "text" else "o.md"))


def _text_sink_graph(tmp_path: Path, plugin: str, *, path: object, field: object) -> CompositionState:
    """csv(id, k) -> passthrough -> ``plugin`` sink writing from ``field``."""
    base = graphs._graph(
        tmp_path, graphs._transform("pt", "passthrough", "src_out", "output", {"schema": OBSERVED}), source_schema=graphs.FIXED_SOURCE
    )
    sink = OutputSpec(name="output", plugin=plugin, options={"path": path, "field": field, "schema": OBSERVED}, on_write_failure="discard")
    return replace(base, outputs=(sink,))


def _input_field_graph(tmp_path: Path, plugin: str, *, field: object) -> CompositionState:
    """csv(id, k) -> ``plugin`` reading its blob reference from ``field`` -> json sink."""
    options = {**get_shared_plugin_manager().get_transform_by_name(plugin).probe_config(), "schema": OBSERVED, "blob_ref_field": field}
    return graphs._graph(tmp_path, graphs._transform("t", plugin, "src_out", "output", options), source_schema=graphs.FIXED_SOURCE)


class _Case:
    """One graph whose requirement a file decides: ``build(tmp_path, content, required_field)``.

    ``content`` stands at the file-held position; ``bytes_for(tmp_path,
    required_field)`` is the file's text when the graph requires ``required_field``.
    """

    def __init__(
        self,
        component: str,
        build: Callable[[Path, object, str], CompositionState],
        bytes_for: Callable[[Path, str], str],
        refusal: str,
    ) -> None:
        self.component = component
        self.build = build
        self.bytes_for = bytes_for
        self.refusal = refusal

    def file_held(self, tmp_path: Path, required_field: str) -> tuple[CompositionState, str]:
        text = self.bytes_for(tmp_path, required_field)
        return self.build(tmp_path, graphs._marker(text), required_field), text

    def inline_twin(self, tmp_path: Path, required_field: str) -> CompositionState:
        return self.build(tmp_path, self.bytes_for(tmp_path, required_field), required_field)


def _sink_field_case(plugin: str) -> _Case:
    return _Case(
        "output:output",
        lambda tmp_path, content, _required: _text_sink_graph(tmp_path, plugin, path=_sink_path(tmp_path, plugin), field=content),
        lambda _tmp_path, required: required,
        "requires fields ['body']",
    )


def _input_field_case(plugin: str) -> _Case:
    return _Case(
        "node:t",
        lambda tmp_path, content, _required: _input_field_graph(tmp_path, plugin, field=content),
        lambda _tmp_path, required: required,
        "requires input fields ['body']",
    )


_CASES: dict[str, _Case] = {
    "text_sink_field": _sink_field_case("text"),
    "document_sink_field": _sink_field_case("document"),
    "blob_text_expand_blob_ref_field": _input_field_case("blob_text_expand"),
}


def _notices(summary: ValidationSummary) -> list[tuple[str, str]]:
    return [(entry.component, entry.message) for entry in summary.warnings if entry.error_code == graphs.DEFERRED]


def _valid_with_one_notice_on(case: _Case, state: CompositionState) -> ValidationSummary:
    summary = state.validate()
    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    notices = _notices(summary)
    assert [component for component, _ in notices] == [case.component], notices
    assert "checked before the run starts" in notices[0][1]
    return summary


@pytest.mark.parametrize("name", sorted(_CASES))
def test_the_inline_twin_is_refused_at_authoring(tmp_path: Path, name: str) -> None:
    """The requirement is real: with the field named inline, authoring refuses the same graph."""
    summary = _CASES[name].inline_twin(tmp_path, ABSENT).validate()

    assert not summary.is_valid
    assert any(ABSENT in entry.message for entry in summary.errors), [entry.message for entry in summary.errors]
    assert _notices(summary) == []


@pytest.mark.parametrize("name", sorted(_CASES))
def test_authoring_reads_the_file_held_graph_valid_and_says_so_on_that_component(tmp_path: Path, name: str) -> None:
    case = _CASES[name]
    for required in (ABSENT, PRESENT):
        state, _text = case.file_held(tmp_path, required)
        _valid_with_one_notice_on(case, state)


@pytest.mark.parametrize("name", sorted(_CASES))
def test_the_save_path_persists_the_runtime_refusal(tmp_path: Path, name: str) -> None:
    case = _CASES[name]
    state, text = case.file_held(tmp_path, ABSENT)
    authoring = _valid_with_one_notice_on(case, state)
    reads: list[UUID] = []

    result = runtime.save_path_preflight(tmp_path, state, text, mime_type=_TEXT, reads=reads)
    persisted_valid, persisted_errors = _composer_persisted_validation(authoring, result)

    assert graphs.TABLE_BLOB in reads, "the preflight read the file's bytes"
    assert result.is_valid is False
    assert persisted_valid is False
    assert persisted_errors is not None
    assert any(case.refusal in error.message for error in persisted_errors), [error.message for error in persisted_errors]


@pytest.mark.parametrize("name", sorted(_CASES))
def test_the_save_path_admits_the_same_graph_over_a_file_naming_a_guaranteed_field(tmp_path: Path, name: str) -> None:
    """Control: only the named field differs, so the refusal above is the field's."""
    case = _CASES[name]
    state, text = case.file_held(tmp_path, PRESENT)
    authoring = _valid_with_one_notice_on(case, state)

    result = runtime.save_path_preflight(tmp_path, state, text, mime_type=_TEXT)

    assert result.is_valid, [error.message for error in result.errors]
    assert _composer_persisted_validation(authoring, result) == (True, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_CASES))
async def test_run_admission_refuses_before_any_run_exists(
    service: ExecutionServiceImpl,
    mock_session_service: MagicMock,
    mock_settings: _WebSettingsStub,
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    name: str,
) -> None:
    case = _CASES[name]
    state, text = case.file_held(tmp_path, ABSENT)
    _valid_with_one_notice_on(case, state)

    refused = await runtime.run_admission_refusal(
        service, mock_session_service, mock_settings, execution_fixture, tmp_path, state, text, mime_type=_TEXT
    )

    assert any(case.refusal in error.message for error in refused.errors), [error.message for error in refused.errors]


# A file at a sink's ``path`` is refused whatever the file says: every path
# option must be a string (``web/execution/_validation_authoring.py``), so the
# bytes are never read. Authoring applies the same rule, so it refuses the
# sink as a config error and carries no deferred notice for it.
_PATH_REFUSAL = "path must be a string path"


def _file_held_sink_path(tmp_path: Path, plugin: str) -> tuple[CompositionState, str]:
    text = _sink_path(tmp_path, plugin)
    return _text_sink_graph(tmp_path, plugin, path=graphs._marker(text), field=PRESENT), text


@pytest.mark.parametrize("plugin", ["document", "text"])
def test_authoring_refuses_a_file_held_sink_path_without_a_notice(tmp_path: Path, plugin: str) -> None:
    state, _text = _file_held_sink_path(tmp_path, plugin)
    authoring = state.validate()

    assert authoring.is_valid is False
    assert [(entry.component, entry.error_code) for entry in authoring.errors] == [("output:output", "plugin_options_invalid")]
    assert _PATH_REFUSAL in authoring.errors[0].message
    assert _notices(authoring) == []


@pytest.mark.parametrize("plugin", ["document", "text"])
def test_a_string_sink_path_beside_a_file_held_field_is_not_refused(tmp_path: Path, plugin: str) -> None:
    """Control: only the reference at ``path`` is refused; ``field`` still defers."""
    state = _text_sink_graph(tmp_path, plugin, path=_sink_path(tmp_path, plugin), field=graphs._marker(PRESENT))
    authoring = state.validate()

    assert [entry.error_code for entry in authoring.errors if entry.component == "output:output"] == []
    assert [component for component, _ in _notices(authoring)] == ["output:output"]


@pytest.mark.parametrize("plugin", ["document", "text"])
def test_the_save_path_refuses_a_file_held_sink_path(tmp_path: Path, plugin: str) -> None:
    state, text = _file_held_sink_path(tmp_path, plugin)
    authoring = state.validate()

    result = runtime.save_path_preflight(tmp_path, state, text, mime_type=_TEXT)
    persisted_valid, persisted_errors = _composer_persisted_validation(authoring, result)

    assert persisted_valid is False
    assert persisted_errors is not None
    assert any(_PATH_REFUSAL in error.message for error in persisted_errors), [error.message for error in persisted_errors]


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin", ["document", "text"])
async def test_run_admission_refuses_a_file_held_sink_path_before_any_run_exists(
    service: ExecutionServiceImpl,
    mock_session_service: MagicMock,
    mock_settings: _WebSettingsStub,
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    plugin: str,
) -> None:
    """Run admission refuses before any run exists, as an invalid pipeline.

    The sink-path allowlist in ``ExecutionServiceImpl._execute_locked`` admits
    only text at a local path option, so the stored file reference is refused
    with ``PipelineValidationError`` naming the sink and option, before
    ``Path(value)`` and before anything is created.
    """
    state, text = _file_held_sink_path(tmp_path, plugin)
    state_dict = state.to_dict()
    state_record = mock_session_service.get_current_state.return_value
    state_record.session_id = graphs.SESSION_ID
    state_record.source = state_dict["sources"]["source"]
    state_record.sources = state_dict["sources"]
    state_record.nodes = state_dict["nodes"]
    state_record.edges = state_dict["edges"]
    state_record.outputs = state_dict["outputs"]
    mock_settings.data_dir = tmp_path
    blob_service = create_autospec(BlobServiceProtocol, instance=True)
    blob_service.get_blob.return_value = runtime._record(text, blob_id=graphs.TABLE_BLOB, session_id=graphs.SESSION_ID, mime_type=_TEXT)
    blob_service.read_blob_content.return_value = text.encode()
    service._blob_service = blob_service
    service._yaml_generator = composer_yaml_generator

    with (
        patch("elspeth.web.execution.validation.validate_pipeline", new=_real_validate_pipeline),
        pytest.raises(PipelineValidationError) as refused,
    ):
        await _execute(service, session_id=graphs.SESSION_ID, user_id="alice", execution_fixture=execution_fixture)

    assert [(error.component_id, error.component_type, error.message) for error in refused.value.errors] == [
        ("output", "sink", "Sink 'output' path must be text, not a file reference or other value.")
    ]

    mock_session_service.create_run.assert_not_awaited()
    blob_service.link_blob_to_run.assert_not_awaited()
    assert not Path(text).exists()


def test_a_sink_whose_config_fails_beside_the_file_reference_carries_no_notice(tmp_path: Path) -> None:
    """Mixed config errors are a config failure, not a deferral (the transform probe's rule)."""
    state = _text_sink_graph(tmp_path, "text", path=_sink_path(tmp_path, "text"), field=graphs._marker(ABSENT))
    sink = state.outputs[0]
    broken = replace(state, outputs=(replace(sink, options={**dict(sink.options), "encoding": "not-an-encoding"}),))

    assert _notices(broken.validate()) == []


def test_a_sink_without_a_file_reference_carries_no_notice(tmp_path: Path) -> None:
    state = _text_sink_graph(tmp_path, "text", path=_sink_path(tmp_path, "text"), field=PRESENT)

    summary = state.validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _notices(summary) == []


@pytest.mark.parametrize("field", [PRESENT, graphs._marker(ABSENT)], ids=["inline_field", "file_held_field"])
def test_a_sink_refused_before_pydantic_runs_carries_no_notice(tmp_path: Path, field: object) -> None:
    """A non-dict ``schema`` is refused by ``from_dict``'s own pre-check, with no pydantic cause: a config failure, never a deferral."""
    state = _text_sink_graph(tmp_path, "text", path=_sink_path(tmp_path, "text"), field=field)
    sink = state.outputs[0]
    broken = replace(state, outputs=(replace(sink, options={**dict(sink.options), "schema": "observed"}),))

    assert _notices(broken.validate()) == []
