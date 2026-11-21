"""Authoring probes distinguish unresolved blob content from invalid config."""

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from elspeth.web.composer import yaml_generator
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from elspeth.web.sessions.routes._helpers import _composer_persisted_validation
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.unit.web.execution import test_deferred_contract_runtime_refusal as runtime_proofs
from tests.unit.web.execution import test_service as _service_tests
from tests.unit.web.execution.test_service import _WebSettingsStub
from tests.unit.web.execution.test_validate_blob_inline import BLOB_ID, _ready_blob_record, _state_with_reference_join

# Run-admission fixtures, requested by name only (none of them is autouse here).
broadcaster = _service_tests.broadcaster
execution_fixture = _service_tests.execution_fixture
mock_loop = _service_tests.mock_loop
mock_session_service = _service_tests.mock_session_service
mock_settings = _service_tests.mock_settings
real_loop = _service_tests.real_loop
service = _service_tests.service


_PIN_TABLE = "sku,description\nhats,A fine hat\n"
_TABLE_WITHOUT_COLUMN = "sku,label\nhats,A fine hat\n"
_PIN_NOTICE = (
    "Node 'enrich' (reference_join) reads an attached file that this check does not open, so the fields it adds (description), "
    "their types and whether they can be empty are checked before the run starts. No change is needed."
)


def _pin_graph(tmp_path: Path, session_id: UUID, table: str, *, required: bool, fixed_upstream: bool = False) -> CompositionState:
    """The join whose table is a file, an AUTHORED ``guaranteed_fields: [description]``, and a sink asking for it."""
    state = _state_with_reference_join(
        tmp_path,
        session_id=session_id,
        reference_format="csv",
        content={"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": hashlib.sha256(table.encode()).hexdigest()},
    )
    if fixed_upstream:
        source = state.sources["source"]
        state = replace(
            state, sources={"source": replace(source, options={**source.options, "schema": {"mode": "fixed", "fields": ["product: str"]}})}
        )
    return replace(
        state,
        nodes=(
            replace(
                state.nodes[0], options={**state.nodes[0].options, "schema": {"mode": "observed", "guaranteed_fields": ["description"]}}
            ),
        ),
        outputs=(
            replace(
                state.outputs[0],
                options={
                    **state.outputs[0].options,
                    "schema": {"mode": "observed", "required_fields": ["description"]}
                    if required
                    else {"mode": "fixed", "fields": ["description: str"]},
                },
            ),
        ),
    )


# Rewritten in stage 2a from the 5bc4b56c9 pin of the same name, which held
# the opposite verdict: the graph was INVALID with ``guarantees: [(none)]``.
@pytest.mark.parametrize("required", [False, True])
def test_blob_contract_probe_reports_deferral_without_promoting_guarantees(tmp_path: Path, required: bool) -> None:
    """Valid with a notice naming the node, its field and the fact; the authored guarantee is still not promoted.

    What moved where, and why. The old pin asserted ``not is_valid`` and an
    error naming ``description``: that refusal was the only thing standing
    between unknown bytes and a run. Its job now belongs to the gates that
    read the bytes. ``test_the_pin_graph_over_a_table_without_the_column_is_
    refused_before_a_run`` (below) drives the WEB save-path runtime preflight
    and run admission over this same graph with a table lacking the column
    and asserts both refuse, with no run record. What stays here: the
    authored ``guaranteed_fields`` never reach a guarantee or an edge
    contract (with an observed upstream the join abstains; behind a fixed one
    it forwards ``product`` and nothing it authors), and the notice is the
    named one, not the generic text the old pin matched.

    Stage 2b: behind the fixed upstream the graph is valid too. ``description``
    is an ``output`` key, so the join could add it once its table is read;
    the sink edge is left unchecked (no edge contract, neither satisfied nor
    refused) and the runtime preflight decides it.
    """
    summary = _pin_graph(tmp_path / "observed", uuid4(), _PIN_TABLE, required=required).validate()
    behind_fixed = _pin_graph(tmp_path / "fixed", uuid4(), _PIN_TABLE, required=required, fixed_upstream=True).validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    [deferred] = [warning for warning in summary.warnings if warning.error_code == "contract_probe_deferred"]
    assert (deferred.component, deferred.severity, deferred.message) == ("node:enrich", "medium", _PIN_NOTICE)
    assert not any("pipeline rejected" in warning.message for warning in summary.warnings)
    for checked in (summary, behind_fixed):
        for contract in checked.edge_contracts:
            assert "description" not in contract.producer_guarantees, contract.to_dict()
    # Behind a fixed upstream: nothing about ``description`` is refused, and the edge to the sink
    # carries no verdict either way. The fixed sink still rejects the forwarded ``product`` (the
    # extras direction, as for the inline twin): that is the only error, and it names no missing field.
    behind_fixed_errors = [(entry.error_code, entry.component) for entry in behind_fixed.errors]
    assert behind_fixed_errors == ([] if required else [("sink_locked_extras", "output:results")]), behind_fixed_errors
    assert not any("Missing fields" in entry.message for entry in behind_fixed.errors)
    assert not [contract for contract in behind_fixed.edge_contracts if contract.from_id == "enrich"]
    assert [warning.message for warning in behind_fixed.warnings if warning.error_code == "contract_probe_deferred"] == [_PIN_NOTICE]


def test_a_required_field_neither_upstream_nor_the_join_could_supply_is_still_refused(tmp_path: Path) -> None:
    """Condition (v), the typo beside the rewritten pin: ``descriptoin`` is not upstream and not an ``output`` key."""
    state = _pin_graph(tmp_path, uuid4(), _PIN_TABLE, required=True, fixed_upstream=True)
    state = replace(
        state,
        outputs=(
            replace(
                state.outputs[0], options={**state.outputs[0].options, "schema": {"mode": "observed", "required_fields": ["descriptoin"]}}
            ),
        ),
    )

    summary = state.validate()

    assert not summary.is_valid
    [error] = summary.errors
    assert error.error_code == "sink_contract_violation"
    assert "Producer (reference_join) guarantees: [product]. Missing fields: [descriptoin]." in error.message


@pytest.mark.asyncio
async def test_the_pin_graph_over_a_table_without_the_column_is_refused_before_a_run(
    tmp_path: Path,
    service: ExecutionServiceImpl,
    mock_session_service: MagicMock,
    mock_settings: _WebSettingsStub,
    execution_fixture: ExecutionTestCustody,
) -> None:
    """Condition (iii): the refusal the old pin held, now made on the bytes by the save path and by run admission."""
    session_id = uuid4()
    state = _pin_graph(tmp_path, session_id, _TABLE_WITHOUT_COLUMN, required=True)
    authoring = state.validate()
    refusal = "output fields ['description'] resolved against none of the 1 reference entries"

    runtime = await asyncio.to_thread(
        runtime_proofs.save_path_preflight,
        tmp_path,
        state,
        _TABLE_WITHOUT_COLUMN,
        blob_id=BLOB_ID,
        session_id=session_id,
        mime_type="text/csv",
    )
    persisted_valid, persisted_errors = _composer_persisted_validation(authoring, runtime)
    admission = await runtime_proofs.run_admission_refusal(
        service,
        mock_session_service,
        mock_settings,
        execution_fixture,
        tmp_path,
        state,
        _TABLE_WITHOUT_COLUMN,
        blob_id=BLOB_ID,
        session_id=session_id,
        mime_type="text/csv",
    )

    assert authoring.is_valid, "authoring defers this fact; the gates below decide it"
    assert persisted_valid is False
    assert persisted_errors is not None and any(refusal in error.message for error in persisted_errors), persisted_errors
    assert any(refusal in error.message for error in admission.errors), [error.message for error in admission.errors]


def test_malformed_blob_marker_remains_a_config_probe_failure(tmp_path: Path) -> None:
    state = _state_with_reference_join(
        tmp_path,
        session_id=uuid4(),
        reference_format="csv",
        content={"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "not-a-hash"},
    )
    state = replace(
        state,
        outputs=(
            replace(
                state.outputs[0], options={**state.outputs[0].options, "schema": {"mode": "observed", "required_fields": ["description"]}}
            ),
        ),
    )

    summary = state.validate()

    assert not summary.is_valid
    assert not any(warning.error_code == "contract_probe_deferred" for warning in summary.warnings)
    assert any(warning.error_code == "contract_probe_failed" and warning.severity == "high" for warning in summary.warnings)
    assert not any("pipeline rejected" in warning.message for warning in summary.warnings)


@pytest.mark.parametrize(
    ("table", "expected_valid"),
    [(b"sku,description\nhats,A fine hat\n", True), (b"sku,description\n", False), (b"sku,description\nhats\n", False)],
)
def test_deferred_reference_table_requires_real_materialized_validation(tmp_path: Path, table: bytes, expected_valid: bool) -> None:
    session_id = uuid4()
    digest = hashlib.sha256(table).hexdigest()
    state = _state_with_reference_join(
        tmp_path,
        session_id=session_id,
        reference_format="csv",
        content={"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": digest},
    )
    record = replace(_ready_blob_record(session_id=session_id, size_bytes=len(table)), content_hash=digest, mime_type="text/csv")

    result = validate_pipeline_for_trained_operator(
        state,
        SimpleNamespace(data_dir=tmp_path),
        yaml_generator,
        blob_get_metadata=lambda _blob_id: record,
        blob_get_content=lambda _blob_id: (record, table),
        session_id=str(session_id),
    )

    assert result.is_valid is expected_valid, result.errors
    if not expected_valid:
        assert result.errors


def test_deferred_reference_table_without_blob_authority_cannot_be_execution_ready(tmp_path: Path) -> None:
    session_id = uuid4()
    state = _state_with_reference_join(
        tmp_path,
        session_id=session_id,
        reference_format="csv",
        content={"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "a" * 64},
    )

    result = validate_pipeline_for_trained_operator(state, SimpleNamespace(data_dir=tmp_path), yaml_generator, session_id=str(session_id))

    assert not result.is_valid
    assert not result.readiness.execution_ready
    assert any(check.name == "blob_inline_refs" and not check.passed for check in result.checks)


def test_unused_inline_default_does_not_discard_a_computed_contract(tmp_path: Path) -> None:
    state = _state_with_reference_join(tmp_path, session_id=uuid4(), reference_format="csv", content="sku,description\nhats,A fine hat\n")
    state = replace(
        state,
        nodes=(
            replace(
                state.nodes[0],
                options={
                    **state.nodes[0].options,
                    "on_miss": "fail",
                    "default_values": {"description": {"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "a" * 64}},
                },
            ),
        ),
        outputs=(
            replace(
                state.outputs[0], options={**state.outputs[0].options, "schema": {"mode": "observed", "required_fields": ["description"]}}
            ),
        ),
    )

    summary = state.validate()

    assert summary.is_valid, summary.errors
    assert not any(warning.error_code == "contract_probe_deferred" for warning in summary.warnings)


@pytest.mark.parametrize("invalid_field", ["on_miss", "unknown_option"])
def test_inline_marker_does_not_reclassify_an_unrelated_invalid_option(tmp_path: Path, invalid_field: str) -> None:
    state = _state_with_reference_join(
        tmp_path,
        session_id=uuid4(),
        reference_format="csv",
        content={"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "a" * 64},
    )
    state = replace(
        state,
        nodes=(
            replace(
                state.nodes[0],
                options={
                    **state.nodes[0].options,
                    invalid_field: "not-a-policy"
                    if invalid_field == "on_miss"
                    else {"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "a" * 64},
                },
            ),
        ),
        outputs=(
            replace(
                state.outputs[0], options={**state.outputs[0].options, "schema": {"mode": "observed", "required_fields": ["description"]}}
            ),
        ),
    )

    summary = state.validate()

    assert not summary.is_valid
    assert any(warning.error_code == "contract_probe_failed" for warning in summary.warnings)
    assert not any(warning.error_code == "contract_probe_deferred" for warning in summary.warnings)
