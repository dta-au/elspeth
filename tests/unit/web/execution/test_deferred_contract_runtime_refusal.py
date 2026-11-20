"""Every fact authoring defers on a file-held table is decided before a run exists.

Authoring abstains on what a ``reference_join`` table's bytes decide and says
so with ``contract_probe_deferred`` (``tests/unit/web/composer/
test_deferred_contract_notice.py``). These tests drive the two web gates that
must then decide each fact on the real bytes, with the same state:

- the save path: ``_runtime_preflight_for_state`` (the runtime preflight the
  composer routes run with the session's blob content) and
  ``_composer_persisted_validation``, which persists the runtime verdict over
  the authoring one;
- run admission: ``ExecutionServiceImpl.execute``, which refuses with
  ``PipelineValidationError`` before ``create_run``.

Each refusal asserts no run record, no blob linked to a run and no sink file.
A deferred graph that could start a run and fail mid-flight would fail here.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, create_autospec, patch
from uuid import UUID

import pytest

from elspeth.contracts.blobs import BlobRecord
from elspeth.web.blobs.protocol import BlobServiceProtocol
from elspeth.web.composer import yaml_generator as composer_yaml_generator
from elspeth.web.composer.state import CompositionState
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.errors import PipelineValidationError
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.execution.validation import validate_pipeline as _real_validate_pipeline
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.routes._helpers import _composer_persisted_validation, _runtime_preflight_for_state
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.unit.web.composer import test_deferred_contract_notice as graphs
from tests.unit.web.execution import test_service as _service_tests
from tests.unit.web.execution.test_service import _execute, _WebSettingsStub
from tests.unit.web.execution.test_validate_blob_inline import _ready_blob_record

_live_execute_lease = _service_tests._live_execute_lease
broadcaster = _service_tests.broadcaster
execution_fixture = _service_tests.execution_fixture
isolate_raw_sink_effect_eligibility_gate = _service_tests.isolate_raw_sink_effect_eligibility_gate
mock_loop = _service_tests.mock_loop
mock_session_service = _service_tests.mock_session_service
mock_settings = _service_tests.mock_settings
real_loop = _service_tests.real_loop
service = _service_tests.service


def _missing_column(tmp_path: Path, content: object) -> CompositionState:
    """The consumer requires the joined field; the table lacks the column it is read from."""
    return graphs._graph(
        tmp_path,
        graphs._join_node("enrich", "src_out", "output", graphs._join(content, schema=graphs.OBSERVED)),
        source_schema=graphs.OBSERVED,
        sink_schema={"mode": "observed", "required_fields": ["added"]},
    )


_NO_DESCRIPTION_TABLE = '[{"sku": "p", "label": "v"}]'

# fact -> (graph, conflicting table, the runtime's refusal)
_FACTS: dict[str, tuple[Callable[[Path, object], CompositionState], str, str]] = {
    "non_null_consumer_fed_json_null": (
        graphs._edge_to_node,
        graphs.NULL_CELL_TABLE,
        "field 'added': consumer requires 'str', producer emits",
    ),
    "row_union_nullable_conflict": (graphs._row_union, graphs.NULL_CELL_TABLE, "receives incompatible schemas from multiple branches"),
    "authored_narrow_type_refuted": (graphs._declared_narrow, graphs.INT_TABLE, "schema declares 'added' as str"),
    "consumer_requires_a_column_the_table_lacks": (
        _missing_column,
        _NO_DESCRIPTION_TABLE,
        "output fields ['added'] resolved against none of the 1 reference entries",
    ),
}


def _record(text: str, *, blob_id: UUID, session_id: UUID, mime_type: str) -> BlobRecord:
    raw = text.encode()
    return replace(
        _ready_blob_record(session_id=session_id, blob_id=blob_id, size_bytes=len(raw)),
        filename="reference.json" if mime_type == "application/json" else "reference.csv",
        mime_type=mime_type,
        content_hash=hashlib.sha256(raw).hexdigest(),
    )


def save_path_preflight(
    tmp_path: Path,
    state: CompositionState,
    table: str,
    *,
    blob_id: UUID = graphs.TABLE_BLOB,
    session_id: UUID = graphs.SESSION_ID,
    mime_type: str = "application/json",
    reads: list[UUID] | None = None,
) -> ValidationResult:
    """The runtime preflight the composer state routes run, with the session's blob content behind it."""
    record = _record(table, blob_id=blob_id, session_id=session_id, mime_type=mime_type)

    def blob_get_content(requested: UUID) -> tuple[BlobRecord, bytes]:
        if reads is not None:
            reads.append(requested)
        if requested != blob_id:
            raise KeyError(requested)
        return record, table.encode()

    catalog = create_catalog_service()
    return asyncio.run(
        _runtime_preflight_for_state(
            state,
            settings=SimpleNamespace(data_dir=tmp_path, secret_wiring_allowlist=(), composer_runtime_preflight_timeout_seconds=60.0),
            secret_service=None,
            user_id=None,
            session_id=session_id,
            plugin_snapshot=PluginAvailabilitySnapshot.for_trained_operator(catalog),
            profile_registry=None,
            catalog=catalog,
            blob_get_content=blob_get_content,
        )
    )


async def run_admission_refusal(
    service: ExecutionServiceImpl,
    mock_session_service: MagicMock,
    mock_settings: _WebSettingsStub,
    execution_fixture: ExecutionTestCustody,
    tmp_path: Path,
    state: CompositionState,
    table: str,
    *,
    blob_id: UUID = graphs.TABLE_BLOB,
    session_id: UUID = graphs.SESSION_ID,
    mime_type: str = "application/json",
) -> PipelineValidationError:
    """Drive ``execute`` over ``state`` with the real validator and the table behind its marker; return the refusal.

    Asserts nothing was created on the way: no run record, no blob linked to a
    run, no sink file. The bytes were read, so the refusal is a verdict on them.
    """
    state_dict = state.to_dict()
    state_record = mock_session_service.get_current_state.return_value
    state_record.session_id = session_id
    state_record.source = state_dict["sources"]["source"]
    state_record.sources = state_dict["sources"]
    state_record.nodes = state_dict["nodes"]
    state_record.edges = state_dict["edges"]
    state_record.outputs = state_dict["outputs"]
    mock_settings.data_dir = tmp_path
    blob_service = create_autospec(BlobServiceProtocol, instance=True)
    blob_service.get_blob.return_value = _record(table, blob_id=blob_id, session_id=session_id, mime_type=mime_type)
    blob_service.read_blob_content.return_value = table.encode()
    service._blob_service = blob_service
    service._yaml_generator = composer_yaml_generator
    sink_path = Path(state.outputs[0].options["path"])

    with (
        # The ``service`` fixture stubs the core validator VALID for mechanics
        # tests; this is about the validator's verdict on real bytes.
        patch("elspeth.web.execution.validation.validate_pipeline", new=_real_validate_pipeline),
        pytest.raises(PipelineValidationError) as exc_info,
    ):
        await _execute(service, session_id=session_id, user_id="alice", execution_fixture=execution_fixture)

    blob_service.read_blob_content.assert_awaited()
    mock_session_service.create_run.assert_not_awaited()
    blob_service.link_blob_to_run.assert_not_awaited()
    assert not sink_path.exists()
    return exc_info.value


def _authoring_is_valid_with_the_notice(state: CompositionState) -> Any:
    summary = state.validate()
    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert len(graphs._notices(summary)) == 1
    return summary


@pytest.mark.parametrize("fact", sorted(_FACTS))
def test_the_save_path_persists_the_runtime_refusal_of_a_deferred_fact(tmp_path: Path, fact: str) -> None:
    build, table, refusal = _FACTS[fact]
    state = build(tmp_path, graphs._marker(table))
    authoring = _authoring_is_valid_with_the_notice(state)
    reads: list[UUID] = []
    runtime = save_path_preflight(tmp_path, state, table, reads=reads)
    persisted_valid, persisted_errors = _composer_persisted_validation(authoring, runtime)

    assert graphs.TABLE_BLOB in reads, "the preflight read the table's bytes"
    assert runtime.is_valid is False
    assert persisted_valid is False
    assert persisted_errors is not None
    assert any(refusal in error.message for error in persisted_errors), [error.message for error in persisted_errors]


@pytest.mark.asyncio
@pytest.mark.parametrize("fact", sorted(_FACTS))
async def test_run_admission_refuses_a_deferred_fact_before_any_run_exists(
    service: ExecutionServiceImpl,
    mock_session_service: MagicMock,
    mock_settings: _WebSettingsStub,
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    fact: str,
) -> None:
    build, table, refusal = _FACTS[fact]
    state = build(tmp_path, graphs._marker(table))
    _authoring_is_valid_with_the_notice(state)

    refused = await run_admission_refusal(service, mock_session_service, mock_settings, execution_fixture, tmp_path, state, table)

    assert any(refusal in error.message for error in refused.errors), [error.message for error in refused.errors]


@pytest.mark.parametrize("fact", sorted(_FACTS))
def test_the_save_path_admits_the_same_graph_over_an_agreeing_table(tmp_path: Path, fact: str) -> None:
    """Control: only the bytes differ, so each refusal above is the fact's and not the harness's."""
    build = _FACTS[fact][0]
    state = build(tmp_path, graphs._marker(graphs.STR_TABLE))
    authoring = _authoring_is_valid_with_the_notice(state)

    runtime = save_path_preflight(tmp_path, state, graphs.STR_TABLE)

    assert runtime.is_valid, [error.message for error in runtime.errors]
    assert _composer_persisted_validation(authoring, runtime) == (True, None)
