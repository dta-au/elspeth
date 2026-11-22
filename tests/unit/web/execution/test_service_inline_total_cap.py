"""Approval compile and run admission: the total file-held text cap at its old and new boundaries.

Both sites enforce the total on blob METADATA before any byte is read. "Admitted"
is observed as reaching the content read (the stub raises a sentinel there), so
these tests measure the cap check itself and nothing after it, in particular not
the resolved-YAML bound.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.blobs_inline import BlobContentResolutionError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP, BLOB_INLINE_PER_REF_BYTE_CAP
from elspeth.web.execution.protocol import FrozenRunSettings
from elspeth.web.execution.service import ExecutionServiceImpl
from tests.unit.web.execution import test_service as _service_tests
from tests.unit.web.execution.test_service import (
    _blob_record_stub,
    _blob_service_stub,
    _composition_state_record,
    _execute_lease,
    _run_record_stub,
)

_live_execute_lease = _service_tests._live_execute_lease
broadcaster = _service_tests.broadcaster
execution_fixture = _service_tests.execution_fixture
isolate_raw_sink_effect_eligibility_gate = _service_tests.isolate_raw_sink_effect_eligibility_gate
mock_loop = _service_tests.mock_loop
mock_session_service = _service_tests.mock_session_service
mock_settings = _service_tests.mock_settings
real_loop = _service_tests.real_loop
service = _service_tests.service

OLD_AGGREGATE_CAP = 1024 * 1024
FILES_AT_CAP = BLOB_INLINE_AGGREGATE_BYTE_CAP // BLOB_INLINE_PER_REF_BYTE_CAP
# Spelled here, not imported, so this file collects on the base tree.
BLOB_INLINE_AGGREGATE_FIELD_PATH = "(aggregate)"


def inline_blob_total_refusal(total: int, cap: int, bindings: int) -> str:
    return (
        f"File-held text totals {total} bytes across {bindings} file bindings; the limit is {cap} bytes. "
        "A file bound in more than one place counts once per place. Remove a binding or shorten the largest files."
    )


class _ReachedContentRead(Exception):
    """Raised by the content read: the metadata checks, the total among them, all passed.

    Not a RuntimeError: the ``service`` fixture's ``_call_async`` bridge
    swallows RuntimeError and returns None.
    """


def _sizes_totalling(total: int) -> list[int]:
    full, remainder = divmod(total, BLOB_INLINE_PER_REF_BYTE_CAP)
    return [BLOB_INLINE_PER_REF_BYTE_CAP] * full + ([remainder] if remainder else [])


# (case id, file sizes, bindings per file, admitted)
_CASES = [
    pytest.param(_sizes_totalling(OLD_AGGREGATE_CAP + 1), 1, True, id="old-cap-plus-one"),
    pytest.param(_sizes_totalling(BLOB_INLINE_AGGREGATE_BYTE_CAP), 1, True, id="exactly-cap"),
    pytest.param([*_sizes_totalling(BLOB_INLINE_AGGREGATE_BYTE_CAP), 1], 1, False, id="cap-plus-one"),
    pytest.param([BLOB_INLINE_PER_REF_BYTE_CAP], FILES_AT_CAP, True, id="one-file-bound-to-the-cap"),
    pytest.param([BLOB_INLINE_PER_REF_BYTE_CAP], FILES_AT_CAP + 1, False, id="one-file-bound-once-more"),
]


def _bindings(session_id: UUID, sizes: list[int], repeat: int) -> tuple[dict[UUID, Any], dict[str, dict[str, str]]]:
    """Records by id, and the node options binding each file ``repeat`` times."""
    records: dict[UUID, Any] = {}
    options: dict[str, dict[str, str]] = {}
    for file_index, size in enumerate(sizes):
        blob_id = uuid4()
        digest = hashlib.sha256(f"file-{file_index}".encode()).hexdigest()
        records[blob_id] = _blob_record_stub(blob_id=blob_id, session_id=session_id, content_hash=digest, size_bytes=size)
        for binding in range(repeat):
            options[f"note_{file_index}_{binding}"] = {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": digest}
    return records, options


def _blob_service(records: dict[UUID, Any]) -> Any:
    async def get_blob(blob_id: UUID, *, session_operation_context: SessionOperationContext) -> Any:
        return records[blob_id]

    async def read_blob_content(blob_id: UUID, *, session_operation_context: SessionOperationContext) -> bytes:
        raise _ReachedContentRead(str(blob_id))

    blob_service = _blob_service_stub()
    blob_service.get_blob.side_effect = get_blob
    blob_service.read_blob_content.side_effect = read_blob_content
    blob_service.link_blob_to_run.return_value = None
    return blob_service


def _assert_total_refusal(refusal: BlobContentResolutionError, sizes: list[int], repeat: int) -> None:
    total = sum(sizes) * repeat
    bindings = len(sizes) * repeat
    assert refusal.oversized == ((BLOB_INLINE_AGGREGATE_FIELD_PATH, total, BLOB_INLINE_AGGREGATE_BYTE_CAP),)
    assert refusal.aggregate_binding_count == bindings
    assert inline_blob_total_refusal(total, BLOB_INLINE_AGGREGATE_BYTE_CAP, bindings) in str(refusal)


@pytest.mark.parametrize(("sizes", "repeat", "admitted"), _CASES)
def test_approval_compile_enforces_the_total(service: ExecutionServiceImpl, sizes: list[int], repeat: int, admitted: bool) -> None:
    session_id = uuid4()
    records, options = _bindings(session_id, sizes, repeat)
    service.set_openrouter_catalog_snapshot(sha256="b" * 64, source="bundled")
    blob_service = _blob_service(records)
    cast(Any, service)._blob_service = blob_service
    config = {
        "source": {"plugin": "csv", "options": {}},
        "transforms": [{"name": "annotate", "plugin": "field_mapper", "options": options}],
    }
    record = _composition_state_record(session_id=session_id, source_path=Path("input.csv"), output_path=Path("output.jsonl"), nodes=[])
    cast(MagicMock, service._session_service).get_state_in_session.return_value = record
    snapshot = service._plugin_snapshot_for_user("author", operation="approval test")
    frozen = FrozenRunSettings(plugin_snapshot=snapshot, executable_config=config, audit_safe_config=config)

    expected: type[BaseException] = _ReachedContentRead if admitted else BlobContentResolutionError
    with pytest.raises(expected) as raised:
        service._approval_inputs_from_frozen(
            frozen,
            state_id=record.id,
            user_id="author",
            session_id=session_id,
            session_operation_context=_execute_lease().context,
        )

    assert blob_service.get_blob.await_count == len(sizes)
    if not admitted:
        _assert_total_refusal(cast(BlobContentResolutionError, raised.value), sizes, repeat)
        blob_service.read_blob_content.assert_not_awaited()


@pytest.mark.parametrize(("sizes", "repeat", "admitted"), _CASES)
@patch("elspeth.web.execution.service.Orchestrator")
@patch("elspeth.web.execution.service.build_validated_runtime_graph")
@patch("elspeth.web.execution.service.load_settings_from_yaml_string")
@patch("elspeth.web.execution.service.open_landscape_db")
@patch("elspeth.web.execution.service.FilesystemPayloadStore")
def test_run_admission_enforces_the_total(
    mock_payload_cls: MagicMock,
    mock_landscape_cls: MagicMock,
    mock_load: MagicMock,
    mock_runtime_graph: MagicMock,
    mock_orch_cls: MagicMock,
    service: ExecutionServiceImpl,
    mock_session_service: MagicMock,
    sizes: list[int],
    repeat: int,
    admitted: bool,
) -> None:
    del mock_payload_cls, mock_landscape_cls, mock_runtime_graph
    run_id = uuid4()
    owner_session = uuid4()
    records, options = _bindings(owner_session, sizes, repeat)
    mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)
    blob_service = _blob_service(records)
    cast(Any, service)._blob_service = blob_service
    option_lines = "\n".join(
        f"      {key}:\n        blob_ref: {marker['blob_ref']}\n        mode: inline_content\n        sha256: {marker['sha256']}"
        for key, marker in options.items()
    )
    pipeline_yaml = f"""
source:
  plugin: csv
  options:
    path: input.csv
transforms:
  - name: annotate
    plugin: field_mapper
    options:
{option_lines}
sinks:
  primary:
    plugin: json
    options:
      path: output.jsonl
"""

    expected: type[BaseException] = _ReachedContentRead if admitted else BlobContentResolutionError
    with pytest.raises(expected) as raised:
        service._run_pipeline(str(run_id), pipeline_yaml, threading.Event(), session_operation_lease=_execute_lease())

    if admitted:
        # Linked to the run, then read: the total check let it through.
        assert blob_service.link_blob_to_run.await_count == len(sizes)
    else:
        _assert_total_refusal(cast(BlobContentResolutionError, raised.value), sizes, repeat)
        blob_service.read_blob_content.assert_not_awaited()
        blob_service.link_blob_to_run.assert_not_awaited()
        mock_session_service.record_blob_inline_resolutions.assert_not_called()
    mock_load.assert_not_called()
    mock_orch_cls.assert_not_called()
