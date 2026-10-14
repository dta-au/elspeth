"""Execution admission: authoritative proof, fanout and governed/semantic gates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch
from uuid import UUID, uuid4

import pytest

from elspeth.contracts import SinkProtocol, SourceProtocol
from elspeth.contracts.chargeable_admission import AdmissionRefusalReason
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.hashing import stable_hash
from elspeth.engine.orchestrator.types import PipelineConfig
from elspeth.web.blobs.protocol import BlobIntegrityError, BlobNotFoundError, BlobServiceProtocol
from elspeth.web.coordination.contracts import StartPermitState
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.errors import ExecutionApprovalRequired, PipelineValidationError
from elspeth.web.execution.protocol import FrozenRunSettings
from elspeth.web.execution.secret_guard import ExecutionSecretApprovalRequired
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.helpers.session_fences import make_blob_read_context
from tests.unit.web.execution import test_service as _service_tests
from tests.unit.web.execution.test_service import (
    _TEST_PIPELINE_YAML,
    _blob_record_stub,
    _blob_rows_entry,
    _blob_rows_pipeline_yaml,
    _blob_rows_record_for_entry,
    _blob_service_stub,
    _coalesce_node,
    _collector_marker_node,
    _composition_state_record,
    _execute,
    _execute_lease,
    _execution_graph_stub,
    _fanout_llm_node,
    _fork_gate_node,
    _governed_approval_inputs,
    _install_ready_proof_blob,
    _install_ready_proof_blobs,
    _mock_pipeline_settings,
    _ModelCopyNamespace,
    _plugin_bundle_stub,
    _proof_gate_state,
    _queue_fan_in_state,
    _queue_node,
    _row_union_node,
    _run_record_stub,
    _successful_core_validation_result,
    _text_source,
    _WebSettingsStub,
    _YamlGeneratorStub,
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


class TestAuthoritativeProofDiagnostics:
    @pytest.mark.asyncio
    async def test_freeform_claimed_blob_with_wrong_session_fails_closed_without_reading(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "freeform-claimed-amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        blob_service = create_autospec(BlobServiceProtocol, instance=True)
        blob_service.get_blob.return_value = _blob_record_stub(
            blob_id=blob_id,
            session_id=uuid4(),
            filename=source_path.name,
            mime_type="text/csv",
            size_bytes=source_path.stat().st_size,
            content_hash=hashlib.sha256(source_path.read_bytes()).hexdigest(),
            storage_path=str(source_path),
            status="ready",
        )
        service._blob_service = blob_service

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert result.checks[23].name == "proof_diagnostics"
        assert result.checks[23].passed is False
        assert [error.error_code for error in result.errors] == ["source_inspection_failed"]
        blob_service.get_blob.assert_awaited_once_with(blob_id, session_operation_context=context)
        blob_service.read_blob_content_prefix_verified.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_freeform_blob_with_valid_custody_reads_once_and_rejects_numeric_gate(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "freeform-amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        blob_service = _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert result.checks[23].name == "proof_diagnostics"
        assert result.checks[23].passed is False
        assert [error.error_code for error in result.errors] == ["gate_expression_type_mismatch_against_source_schema"]
        blob_service.get_blob.assert_awaited_once_with(blob_id, session_operation_context=context)
        blob_service.read_blob_content_prefix_verified.assert_awaited_once_with(
            blob_id,
            prefix_bytes=8 * 1024,
            session_operation_context=context,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "custody_failure",
        ["wrong_session", "wrong_path", "not_ready", "not_found"],
    )
    async def test_freeform_blob_with_failed_custody_is_invalid_without_reading(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
        custody_failure: str,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / f"freeform-{custody_failure}.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        blob_service = create_autospec(BlobServiceProtocol, instance=True)
        if custody_failure == "not_found":
            blob_service.get_blob.side_effect = BlobNotFoundError(str(blob_id))
        else:
            blob_service.get_blob.return_value = _blob_record_stub(
                blob_id=blob_id,
                session_id=uuid4() if custody_failure == "wrong_session" else session_id,
                filename=source_path.name,
                mime_type="text/csv",
                size_bytes=source_path.stat().st_size,
                content_hash=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                storage_path=(str(tmp_path / "different-custody-path.csv") if custody_failure == "wrong_path" else str(source_path)),
                status="pending" if custody_failure == "not_ready" else "ready",
            )
        service._blob_service = blob_service

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert result.checks[23].name == "proof_diagnostics"
        assert result.checks[23].passed is False
        assert [error.error_code for error in result.errors] == ["source_inspection_failed"]
        blob_service.get_blob.assert_awaited_once_with(blob_id, session_operation_context=context)
        blob_service.read_blob_content_prefix_verified.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_observed_csv_numeric_gate_after_declared_queue_is_rejected(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        from elspeth.web.composer.state import SourceSpec

        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "queued-amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        direct_state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        state = replace(
            direct_state,
            sources={
                "source": replace(
                    direct_state.sources["source"],
                    on_success="inbound",
                ),
                "other": SourceSpec(
                    plugin="csv",
                    on_success="inbound",
                    options={
                        "path": str(tmp_path / "other.csv"),
                        "schema": {"mode": "fixed", "fields": ["amount: float"]},
                    },
                    on_validation_failure="discard",
                ),
            },
            nodes=(
                _queue_node("inbound"),
                replace(direct_state.nodes[0], input="inbound"),
            ),
        )
        _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert [error.error_code for error in result.errors] == ["gate_expression_type_mismatch_against_source_schema"]

    @pytest.mark.asyncio
    async def test_more_than_256_blob_sources_blocks_instead_of_partially_passing(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        from elspeth.web.composer.state import SourceSpec

        base = _proof_gate_state(source_path=tmp_path / "source-0.csv", blob_id=uuid4())
        sources = {
            f"source_{index}": SourceSpec(
                plugin="csv",
                on_success=f"rows_{index}",
                options={
                    "path": str(tmp_path / f"source-{index}.csv"),
                    "blob_ref": str(uuid4()),
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="discard",
            )
            for index in range(257)
        }
        state = replace(base, sources=sources, nodes=())
        service._blob_service = None

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            session_id = uuid4()
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert [error.error_code for error in result.errors] == ["source_inspection_failed"]

    @pytest.mark.asyncio
    async def test_observed_csv_numeric_gate_after_passthrough_is_rejected(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        from elspeth.web.composer.state import NodeSpec

        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "passthrough-amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        direct_state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        gate = direct_state.nodes[0]
        passthrough = NodeSpec(
            id="retain_fields",
            node_type="transform",
            plugin="passthrough",
            input="rows",
            on_success="preserved_rows",
            on_error=None,
            options={"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = replace(
            direct_state,
            nodes=(passthrough, replace(gate, input="preserved_rows")),
        )
        _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert [error.error_code for error in result.errors] == ["gate_expression_type_mismatch_against_source_schema"]

    @pytest.mark.asyncio
    async def test_second_observed_csv_source_feeding_numeric_gate_is_rejected(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        from elspeth.web.composer.state import SourceSpec

        session_id = uuid4()
        safe_blob_id = uuid4()
        risky_blob_id = uuid4()
        safe_path = tmp_path / "first-safe.csv"
        risky_path = tmp_path / "second-risky.csv"
        safe_path.write_text("label\nready\n", encoding="utf-8")
        risky_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        risky_state = _proof_gate_state(source_path=risky_path, blob_id=risky_blob_id)
        risky_source = replace(risky_state.sources["source"], on_success="risky_rows")
        state = replace(
            risky_state,
            sources={
                "source": SourceSpec(
                    plugin="csv",
                    on_success="safe_rows",
                    options={
                        "path": str(safe_path),
                        "blob_ref": str(safe_blob_id),
                        "schema": {"mode": "observed"},
                    },
                    on_validation_failure="discard",
                ),
                "risky": risky_source,
            },
            nodes=(replace(risky_state.nodes[0], input="risky_rows"),),
        )
        blob_service = _install_ready_proof_blobs(
            service,
            session_id=session_id,
            bindings={safe_blob_id: safe_path, risky_blob_id: risky_path},
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert [error.error_code for error in result.errors] == ["gate_expression_type_mismatch_against_source_schema"]
        assert blob_service.read_blob_content_prefix_verified.await_count == 2

    @pytest.mark.asyncio
    async def test_sources_sharing_blob_evaluate_both_topologies_with_one_verified_read(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "shared-amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        base = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        base_source = base.sources["source"]
        base_gate = base.nodes[0]
        state = replace(
            base,
            sources={
                "orders": replace(base_source, on_success="order_rows"),
                "refunds": replace(base_source, on_success="refund_rows"),
            },
            nodes=(
                replace(base_gate, id="order_gate", input="order_rows"),
                replace(base_gate, id="refund_gate", input="refund_rows"),
            ),
        )
        blob_service = _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert [error.component_id for error in result.errors] == ["order_gate", "refund_gate"]
        blob_service.read_blob_content_prefix_verified.assert_awaited_once_with(
            blob_id,
            prefix_bytes=8 * 1024,
            session_operation_context=context,
        )

    @pytest.mark.asyncio
    async def test_validate_state_rejects_observed_csv_numeric_gate_after_canonical_core(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert [check.name for check in result.checks[:23]] == [check.name for check in _successful_core_validation_result().checks]
        assert result.checks[23].name == "proof_diagnostics"
        assert result.checks[23].passed is False
        assert [error.error_code for error in result.errors] == ["gate_expression_type_mismatch_against_source_schema"]
        assert result.is_valid is False
        assert result.readiness.authoring_valid is False
        assert result.readiness.execution_ready is False
        assert result.readiness.completion_ready is False

    @pytest.mark.asyncio
    async def test_authoritative_proof_uses_verified_prefix_without_direct_path_read(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "verified-prefix.csv"
        source_path.write_text("amount\n250.00\n", encoding="utf-8")
        state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        blob_service = _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with (
            patch(
                "elspeth.web.execution.validation.validate_pipeline",
                return_value=_successful_core_validation_result(),
            ),
            patch.object(Path, "read_bytes", side_effect=AssertionError("proof must use verified prefix API")),
        ):
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        blob_service.read_blob_content_prefix_verified.assert_awaited_once_with(
            blob_id,
            prefix_bytes=8 * 1024,
            session_operation_context=context,
        )

    @pytest.mark.asyncio
    async def test_execute_rejects_observed_csv_numeric_gate_before_create_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: _WebSettingsStub,
        tmp_path: Path,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_dir = tmp_path / "blobs" / str(session_id)
        source_dir.mkdir(parents=True)
        source_path = source_dir / "amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        state_dict = state.to_dict()
        state_record = mock_session_service.get_current_state.return_value
        state_record.session_id = session_id
        state_record.source = state_dict["sources"]["source"]
        state_record.sources = state_dict["sources"]
        state_record.nodes = state_dict["nodes"]
        state_record.edges = state_dict["edges"]
        state_record.outputs = state_dict["outputs"]
        mock_settings.data_dir = tmp_path
        _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )
        snapshot_calls = 0
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())

        def _snapshot_for_user(_user_id: str) -> PluginAvailabilitySnapshot:
            nonlocal snapshot_calls
            snapshot_calls += 1
            return snapshot

        service._plugin_snapshot_factory = _snapshot_for_user

        with (
            patch(
                "elspeth.web.execution.validation.validate_pipeline",
                return_value=_successful_core_validation_result(),
            ),
            patch("elspeth.web.execution.service.validate_semantic_contracts", return_value=((), (), ())),
            pytest.raises(PipelineValidationError) as exc_info,
        ):
            await _execute(service, session_id=session_id, user_id="alice", execution_fixture=execution_fixture)

        assert exc_info.value.errors[0].error_code == "gate_expression_type_mismatch_against_source_schema"
        assert snapshot_calls == 1
        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("schema_mode", ["fixed", "flexible"])
    async def test_explicit_numeric_source_schema_passes_proof(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
        schema_mode: str,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / f"{schema_mode}-amounts.csv"
        source_path.write_text("amount\n250.00\n", encoding="utf-8")
        state = _proof_gate_state(
            source_path=source_path,
            blob_id=blob_id,
            schema_mode=schema_mode,
        )
        _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is True
        assert result.checks[23].name == "proof_diagnostics"
        assert result.checks[23].passed is True
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_observed_string_comparison_passes_proof(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "regions.csv"
        source_path.write_text("region\nNSW\nVIC\n", encoding="utf-8")
        state = _proof_gate_state(
            source_path=source_path,
            blob_id=blob_id,
            condition="row['region'] == 'NSW'",
        )
        _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is True
        assert result.checks[23].name == "proof_diagnostics"
        assert result.checks[23].passed is True

    @pytest.mark.asyncio
    async def test_uninspectable_claimed_blob_source_fails_closed(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        blob_id = uuid4()
        source_path = tmp_path / "not-authoritatively-resolved.csv"
        state = _proof_gate_state(
            source_path=source_path,
            blob_id=blob_id,
        )
        service._blob_service = None

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            session_id = uuid4()
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert result.checks[23].name == "proof_diagnostics"
        assert result.checks[23].passed is False
        assert [error.error_code for error in result.errors] == ["source_inspection_failed"]

    @pytest.mark.asyncio
    async def test_ambiguous_blob_path_binding_fails_without_reading_bytes(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "canonical.csv"
        source_path.write_text("amount\n250.00\n", encoding="utf-8")
        base = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        source = base.sources["source"]
        state = replace(
            base,
            sources={
                "source": replace(
                    source,
                    options={
                        **source.options,
                        "file": str(tmp_path / "conflicting.csv"),
                    },
                )
            },
        )
        blob_service = _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert result.checks[23].name == "proof_diagnostics"
        assert result.checks[23].passed is False
        assert [error.error_code for error in result.errors] == ["source_inspection_failed"]
        blob_service.read_blob_content_prefix_verified.assert_not_awaited()


class TestFanoutGuardProducerIndexDerivesPublication:
    """The producer index must ask the helper, not test ``on_success`` by hand.

    ``_build_producer_index`` read ``node.on_success`` directly and installed
    only queues under their own id. A non-terminal coalesce and an aggregation
    that omits ``on_success`` publish under their own ids too, so neither was
    ever registered — and ``walk_label`` does a ``.get()`` that returns
    silently on a miss, so the upstream trace simply stopped.

    Consequence (elspeth-8190d4e4cf): a ``batch_replicate`` aggregation over
    500 rows feeding an ``llm`` node returned no guard at all. No 428, no ack
    token, and ``annotate_pipeline_yaml_with_fanout_guard`` never ran, so the
    launch YAML lost its audit comment as well. Unacknowledged LLM spend with
    no record a guard was ever owed. FALSE ACCEPT.
    """

    @staticmethod
    def _state_with(node: Any, *, llm_input: str, data_path: Path) -> Any:
        from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec

        return CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="src_out",
                options={"path": str(data_path.name), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(node, _fanout_llm_node(llm_input)),
            edges=(),
            outputs=(
                OutputSpec(name="out", plugin="json", options={"schema": {"mode": "observed"}}, on_write_failure="discard"),
                OutputSpec(name="errors", plugin="json", options={"schema": {"mode": "observed"}}, on_write_failure="discard"),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )

    @staticmethod
    def _replicating_aggregation(*, on_success: str | None) -> Any:
        from elspeth.web.composer.state import NodeSpec

        return NodeSpec(
            id="agg",
            node_type="aggregation",
            plugin="batch_replicate",
            input="src_out",
            on_success=on_success,
            on_error="errors",
            options={"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={"count": 10},
            output_mode="transform",
        )

    def _csv(self, tmp_path: Path) -> Path:
        path = tmp_path / "in.csv"
        path.write_text("a,b\n" + "".join(f"{i},{i}\n" for i in range(500)), encoding="utf-8")
        return path

    def test_implicit_aggregation_upstream_of_llm_still_raises_the_guard(self, tmp_path: Path) -> None:
        """The reproduction: two runtime-identical graphs, only the guard differed."""
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        data_path = self._csv(tmp_path)
        implicit = self._state_with(self._replicating_aggregation(on_success=None), llm_input="agg", data_path=data_path)
        explicit = self._state_with(self._replicating_aggregation(on_success="agg_out"), llm_input="agg_out", data_path=data_path)

        implicit_guard = evaluate_execution_fanout_guard(implicit, data_dir=tmp_path)
        explicit_guard = evaluate_execution_fanout_guard(explicit, data_dir=tmp_path)

        assert implicit_guard is not None, (
            "A transform-mode batch_replicate aggregation feeding an LLM raised NO guard when it omitted "
            "on_success — unacknowledged LLM spend, and no audit comment on the launch YAML either."
        )
        assert explicit_guard is not None
        assert implicit_guard.risk_level == explicit_guard.risk_level, (
            "The two graphs are runtime-identical; omitting on_success must not change the risk level."
        )
        assert [risk.node_id for risk in implicit_guard.risks] == [risk.node_id for risk in explicit_guard.risks]

    def test_every_implicit_self_publisher_registers_as_a_producer_of_its_own_id(self, tmp_path: Path) -> None:
        """The pin, ENUMERATED FROM the helper's own set — not restated here.

        A fourth node kind added to ``_IMPLICIT_SELF_PUBLISHING_NODE_TYPES``
        cannot skip this site: it joins the loop automatically and fails here
        if the producer index does not register it.
        """
        from elspeth.web.composer._producer_resolver import _IMPLICIT_SELF_PUBLISHING_NODE_TYPES, published_success_connection
        from elspeth.web.composer.state import NodeSpec
        from elspeth.web.execution.fanout_guard import _build_producer_index

        data_path = self._csv(tmp_path)
        for kind in sorted(_IMPLICIT_SELF_PUBLISHING_NODE_TYPES):
            node = NodeSpec(
                id="publisher",
                node_type=kind,
                plugin=None,
                # A queue's input IS its own id; the others consume upstream.
                input="publisher" if kind == "queue" else "src_out",
                on_success=None,
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"only": "src_out"} if kind == "coalesce" else None,
                policy="all" if kind == "coalesce" else None,
                merge="row_union" if kind == "coalesce" else None,
                **({"trigger": {"count": 10}} if kind == "aggregation" else {}),
            )
            assert published_success_connection(node) == "publisher", kind
            index = _build_producer_index(self._state_with(node, llm_input="publisher", data_path=data_path))
            assert "publisher" in index.by_connection, (
                f"A '{kind}' publishing under its own id is not registered in the fanout guard's producer "
                "index, so walk_label() returns silently on it and the upstream trace stops — the LLM "
                "fanout guard is disabled for every pipeline routing through one. Register "
                "published_success_connection(node); do not restate the set here."
            )

    def test_a_queue_owns_its_own_id_regardless_of_node_order(self, tmp_path: Path) -> None:
        """Deleting the separate queue install must not depend on declaration order.

        The removed block re-installed each queue as the canonical producer of
        its id AFTER the main loop. Deriving the channel makes the queue
        self-register inside the loop instead, and ``register``'s divert only
        routes away producers whose key is not ``node:<queue id>`` — so the
        queue wins either way. Measured here rather than reasoned.
        """
        from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
        from elspeth.web.execution.fanout_guard import _build_producer_index

        data_path = self._csv(tmp_path)
        queue = _queue_node("q")
        feeders = (_fanout_llm_node("src_out", node_id="a"), _fanout_llm_node("src_out", node_id="b"))
        feeders = tuple(replace(node, on_success="q") for node in feeders)

        for label, nodes in (
            ("feeders first", (*feeders, queue)),
            ("queue first", (queue, *feeders)),
        ):
            state = CompositionState(
                source=SourceSpec(
                    plugin="csv",
                    on_success="src_out",
                    options={"path": str(data_path.name), "schema": {"mode": "observed"}},
                    on_validation_failure="discard",
                ),
                nodes=nodes,
                edges=(),
                outputs=(
                    OutputSpec(name="out", plugin="json", options={"schema": {"mode": "observed"}}, on_write_failure="discard"),
                    OutputSpec(name="errors", plugin="json", options={"schema": {"mode": "observed"}}, on_write_failure="discard"),
                ),
                metadata=PipelineMetadata(),
                version=1,
            )
            index = _build_producer_index(state)
            owner = index.by_connection.get("q")
            assert owner is not None and owner.node is not None and owner.node.id == "q", label
            assert sorted(p.node.id for p in index.queue_predecessors.get("q", ()) if p.node) == ["a", "b"], label


class TestExecutionFanoutGuard:
    def test_source_native_llm_bounds_downstream_llm_transform_to_one_row(self, tmp_path: Path) -> None:
        """A source:llm may emit zero or one row, never unknown fanout."""
        from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        state = CompositionState(
            source=SourceSpec(
                plugin="llm",
                on_success="generated",
                options={
                    "provider": "openrouter",
                    "model": "openai/gpt-4o-mini",
                    "prompt_template": "Write one briefing.",
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="discard",
            ),
            nodes=(_fanout_llm_node("generated", node_id="refine"),),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        assert evaluate_execution_fanout_guard(state, data_dir=tmp_path) is None

    @pytest.mark.asyncio
    async def test_line_explode_to_llm_requires_ack_before_run_creation(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        tmp_path: Path,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """A deaggregation transform upstream of LLM must stop at launch."""
        from elspeth.web.execution.fanout_guard import ExecutionFanoutGuardRequired

        data_dir = tmp_path
        session_id = uuid4()
        blob_dir = data_dir / "blobs" / str(session_id)
        output_dir = data_dir / "outputs" / str(session_id)
        blob_dir.mkdir(parents=True)
        output_dir.mkdir(parents=True)
        source_path = blob_dir / "input.txt"
        source_path.write_text("alpha\nbeta\n", encoding="utf-8")
        mock_settings.data_dir = data_dir
        mock_session_service.get_current_state.return_value = _composition_state_record(
            session_id=session_id,
            source_path=source_path,
            output_path=output_dir / "out.jsonl",
            nodes=[
                {
                    "id": "explode_lines",
                    "node_type": "transform",
                    "plugin": "line_explode",
                    "input": "source_rows",
                    "on_success": "line_rows",
                    "on_error": "errors",
                    "options": {"source_field": "body", "schema": {"mode": "observed"}},
                },
                {
                    "id": "classify_line",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "line_rows",
                    "on_success": "out",
                    "on_error": "errors",
                    "options": {
                        "provider": "openrouter",
                        "model": "openai/gpt-4o-mini",
                        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                    },
                },
            ],
        )

        with (
            patch.object(service, "_run_pipeline"),
            patch("elspeth.web.execution.service.validate_semantic_contracts", return_value=((), (), ())),
        ):
            # The wired OPENROUTER_API_KEY trips the secret-approval guard
            # first (elspeth-f3c1aafd25); acknowledge it to reach the fanout
            # guard under test.
            with pytest.raises(ExecutionSecretApprovalRequired) as secret_raised:
                await _execute(service, session_id=session_id, execution_fixture=execution_fixture)
            with pytest.raises(ExecutionFanoutGuardRequired) as raised:
                await _execute(
                    service,
                    session_id=session_id,
                    secret_ack_token=secret_raised.value.guard.ack_token,
                    execution_fixture=execution_fixture,
                )

        guard = raised.value.guard
        assert guard.ack_token
        assert guard.risks[0].node_id == "classify_line"
        assert guard.risks[0].provider == "openrouter"
        assert guard.risks[0].model == "openai/gpt-4o-mini"
        assert guard.risks[0].credential_ref == "secret_ref:OPENROUTER_API_KEY"
        assert guard.risks[0].estimated_provider_calls is None
        assert mock_session_service.create_run.await_count == 0

    @pytest.mark.asyncio
    async def test_acknowledged_line_explode_to_llm_records_guard_in_run_yaml(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        tmp_path: Path,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """Accepted fanout warnings are persisted with the run launch record."""
        from elspeth.web.execution.fanout_guard import ExecutionFanoutGuardRequired

        data_dir = tmp_path
        blob_dir = data_dir / "blobs"
        output_dir = data_dir / "outputs"
        blob_dir.mkdir()
        output_dir.mkdir()
        source_path = blob_dir / "input.txt"
        source_path.write_text("alpha\nbeta\n", encoding="utf-8")
        session_id = uuid4()
        mock_settings.data_dir = data_dir
        mock_session_service.get_current_state.return_value = _composition_state_record(
            session_id=session_id,
            source_path=source_path,
            output_path=output_dir / "out.jsonl",
            nodes=[
                {
                    "id": "explode_lines",
                    "node_type": "transform",
                    "plugin": "line_explode",
                    "input": "source_rows",
                    "on_success": "line_rows",
                    "on_error": "errors",
                    "options": {"source_field": "body", "schema": {"mode": "observed"}},
                },
                {
                    "id": "classify_line",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "line_rows",
                    "on_success": "out",
                    "on_error": "errors",
                    "options": {
                        "provider": "openrouter",
                        "model": "openai/gpt-4o-mini",
                        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                    },
                },
            ],
        )

        with (
            patch.object(service, "_run_pipeline"),
            patch("elspeth.web.execution.service.validate_semantic_contracts", return_value=((), (), ())),
        ):
            # Secret approval (elspeth-f3c1aafd25) gates first, then fanout.
            with pytest.raises(ExecutionSecretApprovalRequired) as secret_raised:
                await _execute(service, session_id=session_id, execution_fixture=execution_fixture)
            with pytest.raises(ExecutionFanoutGuardRequired) as raised:
                await _execute(
                    service,
                    session_id=session_id,
                    secret_ack_token=secret_raised.value.guard.ack_token,
                    execution_fixture=execution_fixture,
                )

        run_id = uuid4()
        mock_session_service.create_run.return_value = _run_record_stub(id=run_id)
        # This test isolates fanout acknowledgement. Direct source ->
        # line_explode now correctly fails the independent UNKNOWN semantic
        # contract gate, so keep that sibling gate stubbed on the accepted
        # retry just as it is on the initial guard-producing call above.
        authority = execution_fixture.observe_authority(session_id)
        with (
            patch.object(service, "_run_pipeline") as run_pipeline,
            patch.object(service, "_loop", asyncio.get_running_loop()),
            patch("elspeth.web.execution.service.validate_semantic_contracts", return_value=((), (), ())),
        ):
            try:
                await _execute(
                    service,
                    session_id=session_id,
                    authority=authority,
                    fanout_ack_token=raised.value.guard.ack_token,
                    secret_ack_token=secret_raised.value.guard.ack_token,
                    execution_fixture=execution_fixture,
                )
            finally:
                # The real executor and its lease-completion callback must
                # finish before the fixture closes the worker's bridge loop.
                await execution_fixture.shutdown_service(service)

        run_pipeline.assert_called_once()
        acquired = [context for name, context in authority.calls if name == "acquire"]
        released = [context for name, context in authority.calls if name == "release"]
        assert len(acquired) == 1
        assert released == acquired
        assert not service._lease_completion_futures

        create_call = mock_session_service.create_run.await_args_list[-1]
        persisted_yaml = create_call.kwargs["pipeline_yaml"]
        assert "elspeth_execution_fanout_guard" in persisted_yaml
        assert "elspeth_execution_secret_approval" in persisted_yaml
        assert '"accepted":true' in persisted_yaml
        assert raised.value.guard.ack_token in persisted_yaml
        assert secret_raised.value.guard.ack_token in persisted_yaml
        assert '"node_id":"classify_line"' in persisted_yaml
        assert '"secret_name":"OPENROUTER_API_KEY"' in persisted_yaml

    @pytest.mark.asyncio
    async def test_direct_small_text_source_to_llm_executes_without_ack(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        tmp_path: Path,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """A direct low-cardinality source->LLM path remains a one-click run."""
        data_dir = tmp_path
        blob_dir = data_dir / "blobs"
        output_dir = data_dir / "outputs"
        blob_dir.mkdir()
        output_dir.mkdir()
        source_path = blob_dir / "input.txt"
        source_path.write_text("alpha\nbeta\n", encoding="utf-8")
        session_id = uuid4()
        mock_settings.data_dir = data_dir
        mock_session_service.get_current_state.return_value = _composition_state_record(
            session_id=session_id,
            source_path=source_path,
            output_path=output_dir / "out.jsonl",
            nodes=[
                {
                    "id": "classify_row",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "source_rows",
                    "on_success": "out",
                    "on_error": "errors",
                    "options": {
                        "provider": "openrouter",
                        "model": "openai/gpt-4o-mini",
                        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                    },
                },
            ],
        )

        authority = execution_fixture.observe_authority(session_id)
        with (
            patch.object(service, "_run_pipeline"),
            patch.object(service, "_loop", asyncio.get_running_loop()),
            patch("elspeth.web.execution.service.validate_semantic_contracts", return_value=((), (), ())),
        ):
            try:
                # The wired secret still requires its own out-of-band approval
                # (elspeth-f3c1aafd25); low cardinality only removes the FANOUT ack.
                with pytest.raises(ExecutionSecretApprovalRequired) as secret_raised:
                    await _execute(service, session_id=session_id, execution_fixture=execution_fixture)
                run_id = await _execute(
                    service,
                    session_id=session_id,
                    authority=authority,
                    secret_ack_token=secret_raised.value.guard.ack_token,
                    execution_fixture=execution_fixture,
                )
            finally:
                await execution_fixture.shutdown_service(service)

        assert isinstance(run_id, UUID)
        acquired = [context for name, context in authority.calls if name == "acquire"]
        released = [context for name, context in authority.calls if name == "release"]
        assert len(acquired) == 1
        assert released == acquired
        assert not service._lease_completion_futures
        persisted_yaml = mock_session_service.create_run.await_args.kwargs["pipeline_yaml"]
        assert "elspeth_execution_fanout_guard" not in persisted_yaml
        assert "elspeth_execution_secret_approval" in persisted_yaml

    @pytest.mark.asyncio
    async def test_non_first_named_source_to_llm_uses_its_own_cardinality(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        tmp_path: Path,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """Named source fanout accounting must not inspect only the compatibility source."""
        from elspeth.web.execution.fanout_guard import ExecutionFanoutGuardRequired

        data_dir = tmp_path
        session_id = uuid4()
        blob_dir = data_dir / "blobs" / str(session_id)
        output_dir = data_dir / "outputs" / str(session_id)
        blob_dir.mkdir(parents=True)
        output_dir.mkdir(parents=True)
        orders_path = blob_dir / "orders.txt"
        refunds_path = blob_dir / "refunds.txt"
        orders_path.write_text("one\n", encoding="utf-8")
        refunds_path.write_text("\n".join(f"refund-{i}" for i in range(101)) + "\n", encoding="utf-8")
        mock_settings.data_dir = data_dir
        mock_session_service.get_current_state.return_value = _composition_state_record(
            session_id=session_id,
            source_path=orders_path,
            output_path=output_dir / "out.jsonl",
            sources={
                "orders": {
                    "plugin": "text",
                    "on_success": "orders_rows",
                    "on_validation_failure": "discard",
                    "options": {"path": str(orders_path), "column": "body", "schema": {"mode": "observed"}},
                },
                "refunds": {
                    "plugin": "text",
                    "on_success": "refunds_rows",
                    "on_validation_failure": "discard",
                    "options": {"path": str(refunds_path), "column": "body", "schema": {"mode": "observed"}},
                },
            },
            nodes=[
                {
                    "id": "classify_refund",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "refunds_rows",
                    "on_success": "out",
                    "on_error": "errors",
                    "options": {
                        "provider": "openrouter",
                        "model": "openai/gpt-4o-mini",
                        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                    },
                },
            ],
        )

        with (
            patch.object(service, "_run_pipeline"),
            patch("elspeth.web.execution.service.validate_semantic_contracts", return_value=((), (), ())),
        ):
            # Acknowledge the wired-secret approval first (elspeth-f3c1aafd25).
            with pytest.raises(ExecutionSecretApprovalRequired) as secret_raised:
                await _execute(service, session_id=session_id, execution_fixture=execution_fixture)
            with pytest.raises(ExecutionFanoutGuardRequired) as raised:
                await _execute(
                    service,
                    session_id=session_id,
                    secret_ack_token=secret_raised.value.guard.ack_token,
                    execution_fixture=execution_fixture,
                )

        risk = raised.value.guard.risks[0]
        assert risk.estimated_provider_calls == 101
        assert risk.upstream_fanout == ("source:refunds:text:estimated_rows=101",)
        assert mock_session_service.create_run.await_count == 0

    def test_coalesce_mapping_traverses_values_once_across_nesting_and_cycles(self, tmp_path: Path) -> None:
        """Mapping values remain reachable and duplicate/cyclic paths are visited once."""
        from elspeth.web.composer.state import CompositionState, PipelineMetadata
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        rows = tmp_path / "rows.txt"
        rows.write_text("\n".join(f"row-{i}" for i in range(101)) + "\n", encoding="utf-8")
        inner = _coalesce_node(
            node_id="inner",
            on_success="inner_rows",
            branches={
                "primary_branch": "actual_source_rows",
                "duplicate_branch": "actual_source_rows",
                "cycle_branch": "outer_rows",
            },
        )
        outer = _coalesce_node(
            node_id="outer",
            on_success="outer_rows",
            branches={"nested_branch": "inner_rows"},
        )
        state = CompositionState(
            sources={"rows": _text_source(rows, "actual_source_rows")},
            nodes=(inner, outer, _fanout_llm_node("outer_rows")),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None, "101 reachable rows must require a guard"
        risk = guard.risks[0]
        assert risk.estimated_provider_calls == 101
        assert risk.upstream_fanout == ("source:rows:text:estimated_rows=101",)

    def test_row_union_traverses_every_branch_for_provider_cost(self, tmp_path: Path) -> None:
        """A downstream LLM sees the summed cardinality of every released branch."""
        from elspeth.web.composer.state import CompositionState, PipelineMetadata
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        control = tmp_path / "control.txt"
        treatment = tmp_path / "treatment.txt"
        control.write_text("\n".join(f"control-{i}" for i in range(60)) + "\n", encoding="utf-8")
        treatment.write_text("\n".join(f"treatment-{i}" for i in range(60)) + "\n", encoding="utf-8")
        state = CompositionState(
            sources={
                "control": _text_source(control, "control_done"),
                "treatment": _text_source(treatment, "treatment_done"),
            },
            nodes=(
                _row_union_node(
                    branches={
                        "control": "control_done",
                        "treatment": "treatment_done",
                    }
                ),
                _fanout_llm_node("unioned_rows"),
            ),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None
        assert guard.risks[0].estimated_provider_calls == 120
        assert set(guard.risks[0].upstream_fanout) == {
            "source:control:text:estimated_rows=60",
            "source:treatment:text:estimated_rows=60",
        }

    # ── Queue fan-in provider-cost guard (elspeth-6421ffa028) ───────────
    # A declared queue fans multiple upstream sources into one LLM. The
    # cost guard MUST traverse EVERY predecessor and combine cardinalities
    # conservatively — never resolve to whichever source registered last.

    def test_queue_fan_in_combines_known_source_cardinalities(self, tmp_path: Path) -> None:
        """Two known source predecessors of a queue are summed, not last-writer-wins."""
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        orders = tmp_path / "orders.txt"
        refunds = tmp_path / "refunds.txt"
        orders.write_text("\n".join(f"order-{i}" for i in range(60)) + "\n", encoding="utf-8")
        refunds.write_text("\n".join(f"refund-{i}" for i in range(60)) + "\n", encoding="utf-8")
        state = _queue_fan_in_state(
            sources={
                "orders": _text_source(orders, "inbound"),
                "refunds": _text_source(refunds, "inbound"),
            }
        )

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None, "60 + 60 = 120 > threshold must require a guard"
        risk = guard.risks[0]
        assert risk.node_id == "classify"
        # Summed conservatively across both predecessors, not a single source's 60.
        assert risk.estimated_provider_calls == 120
        assert set(risk.upstream_fanout) == {
            "source:orders:text:estimated_rows=60",
            "source:refunds:text:estimated_rows=60",
        }

    def test_queue_fan_in_one_unknown_predecessor_guards_with_unknown_calls(self, tmp_path: Path) -> None:
        """Any unknown-cardinality predecessor keeps the sum None and risk HIGH."""
        from elspeth.web.composer.state import SourceSpec
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        orders = tmp_path / "orders.txt"
        orders.write_text("\n".join(f"order-{i}" for i in range(5)) + "\n", encoding="utf-8")
        # A csv source with no path/limit has an unknowable cardinality.
        unknown = SourceSpec(
            plugin="csv",
            on_success="inbound",
            options={"schema": {"mode": "observed"}},
            on_validation_failure="discard",
        )
        state = _queue_fan_in_state(sources={"orders": _text_source(orders, "inbound"), "refunds": unknown})

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None, "an unknown predecessor must never silently return no guard"
        risk = guard.risks[0]
        assert risk.estimated_provider_calls is None
        assert risk.risk_level == "high"
        assert "source:refunds:csv:estimated_rows=unknown" in risk.upstream_fanout

    def test_queue_fan_in_token_creating_predecessor_stays_visible(self, tmp_path: Path) -> None:
        """A token-creating transform feeding the queue is not lost behind the queue."""
        from elspeth.web.composer.state import NodeSpec
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        orders = tmp_path / "orders.txt"
        refunds = tmp_path / "refunds.txt"
        orders.write_text("alpha\nbeta\n", encoding="utf-8")
        refunds.write_text("gamma\ndelta\n", encoding="utf-8")
        explode = NodeSpec(
            id="explode_lines",
            node_type="transform",
            plugin="line_explode",
            input="orders_rows",
            on_success="inbound",
            on_error="errors",
            options={"source_field": "body", "schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = _queue_fan_in_state(
            sources={
                "orders": _text_source(orders, "orders_rows"),
                "refunds": _text_source(refunds, "inbound"),
            },
            extra_nodes=(explode,),
        )

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None
        risk = guard.risks[0]
        # Token-creating fanout upstream of a queue is unbounded, not summable.
        assert risk.estimated_provider_calls is None
        assert any(marker.startswith("transform:explode_lines:line_explode") for marker in risk.upstream_fanout)

    def test_queue_fan_in_guard_is_source_order_invariant(self, tmp_path: Path) -> None:
        """Reversing predecessor order yields an identical guard decision."""
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        orders = tmp_path / "orders.txt"
        refunds = tmp_path / "refunds.txt"
        orders.write_text("\n".join(f"order-{i}" for i in range(60)) + "\n", encoding="utf-8")
        refunds.write_text("\n".join(f"refund-{i}" for i in range(60)) + "\n", encoding="utf-8")
        forward = _queue_fan_in_state(
            sources={
                "orders": _text_source(orders, "inbound"),
                "refunds": _text_source(refunds, "inbound"),
            }
        )
        reverse = _queue_fan_in_state(
            sources={
                "refunds": _text_source(refunds, "inbound"),
                "orders": _text_source(orders, "inbound"),
            }
        )

        guard_forward = evaluate_execution_fanout_guard(forward, data_dir=tmp_path)
        guard_reverse = evaluate_execution_fanout_guard(reverse, data_dir=tmp_path)

        assert guard_forward is not None and guard_reverse is not None
        assert guard_forward.risks[0].estimated_provider_calls == 120
        assert guard_reverse.risks[0].estimated_provider_calls == 120
        assert sorted(guard_forward.risks[0].upstream_fanout) == sorted(guard_reverse.risks[0].upstream_fanout)

    # ── Deterministic fork fan-out arithmetic (elspeth session 39578c6f) ─
    # A fork gate duplicates each row exactly once per branch, so fork-only
    # topologies keep provable per-row and total call counts. Only genuinely
    # unbounded expansion (creates_tokens / collector / transform-mode
    # aggregation) or a fork recombined by a combining fan-in stays unknown.

    def test_fork_gate_keeps_estimate_and_reports_per_row_calls(self, tmp_path: Path) -> None:
        """Fork-only topology: per-node estimate = rows x 1, risk graded by magnitude."""
        from elspeth.web.composer.state import CompositionState, PipelineMetadata
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        rows = tmp_path / "rows.txt"
        rows.write_text("\n".join(f"row-{i}" for i in range(5)) + "\n", encoding="utf-8")
        state = CompositionState(
            sources={"rows": _text_source(rows, "raw_rows")},
            nodes=(
                _fork_gate_node(node_id="fan_out", input_label="raw_rows", fork_to=("branch_a", "branch_b")),
                _fanout_llm_node("branch_a", node_id="assess_a"),
                _fanout_llm_node("branch_b", node_id="assess_b"),
            ),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None, "fork markers still require an acknowledgement"
        assert len(guard.risks) == 2
        for risk in guard.risks:
            # Each source row reaches each leaf exactly once: 5 rows x 1 call.
            assert risk.estimated_provider_calls == 5
            assert risk.provider_calls_per_row == 1
            # A known, small estimate is graded by magnitude, not by the mere
            # presence of a fork marker.
            assert risk.risk_level == "medium"
            assert "per source row" in risk.message
        assert guard.risk_level == "medium"
        # The pipeline-level per-row cost is the number an operator can
        # sanity-check against intent: 2 calls per source row, 10 total.
        assert "2 provider call(s) per source row" in guard.summary
        assert "estimated 10 total" in guard.summary

    def test_fork_gate_with_unknown_rows_reports_per_row_calls(self, tmp_path: Path) -> None:
        """Unknown row count + deterministic forks: say the per-row cost, stay high."""
        from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        unknown = SourceSpec(
            plugin="csv",
            on_success="raw_rows",
            options={"schema": {"mode": "observed"}},
            on_validation_failure="discard",
        )
        state = CompositionState(
            sources={"rows": unknown},
            nodes=(
                _fork_gate_node(node_id="fan_out", input_label="raw_rows", fork_to=("branch_a", "branch_b")),
                _fanout_llm_node("branch_a", node_id="assess_a"),
                _fanout_llm_node("branch_b", node_id="assess_b"),
            ),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None
        for risk in guard.risks:
            assert risk.estimated_provider_calls is None
            assert risk.risk_level == "high", "unknown row count keeps spend unbounded"
            assert "1 openrouter model openai/gpt-4o-mini call(s) per source row" in risk.message
        assert "2 provider call(s) per source row" in guard.summary
        assert "could not be estimated" in guard.summary

    def test_fork_recombined_by_row_union_refuses_the_arithmetic(self, tmp_path: Path) -> None:
        """Fork copies rejoined by a row_union would double-count the deduped source."""
        from elspeth.web.composer.state import CompositionState, PipelineMetadata
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        rows = tmp_path / "rows.txt"
        rows.write_text("\n".join(f"row-{i}" for i in range(5)) + "\n", encoding="utf-8")
        state = CompositionState(
            sources={"rows": _text_source(rows, "raw_rows")},
            nodes=(
                _fork_gate_node(node_id="fan_out", input_label="raw_rows", fork_to=("branch_a", "branch_b")),
                _row_union_node(branches={"a": "branch_a", "b": "branch_b"}),
                _fanout_llm_node("unioned_rows"),
            ),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None
        risk = guard.risks[0]
        # The truth is 10 calls (2 copies x 5 rows) but the source dedup
        # counts 5; the guard must refuse the estimate rather than publish it.
        assert risk.estimated_provider_calls is None
        assert risk.risk_level == "high"
        assert "unknown number" in risk.message
        assert "per source row" not in risk.message


class TestGovernedExecutionAdmission:
    def test_config_mismatch_refuses_before_blob_rows_payload_staging(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock
    ) -> None:
        service._settings.workflow_governance = "on"
        run_id = uuid4()
        owner_session = uuid4()
        content = b"row source content"
        blob_id = uuid4()
        entry = _blob_rows_entry(content, blob_id=blob_id)
        config = json.loads(_blob_rows_pipeline_yaml([entry]))
        snapshot = service._plugin_snapshot_for_user("author", operation="approval test")
        frozen = FrozenRunSettings(plugin_snapshot=snapshot, executable_config=config, audit_safe_config=config)
        envelope = SimpleNamespace(
            settings=frozen,
            secret_resolver=None,
            env_ref_names=(),
            blob_inputs=[SimpleNamespace(reference=SimpleNamespace(blob_id=blob_id), retained=object())],
        )
        mock_session_service.get_run.return_value = _run_record_stub(
            id=run_id, session_id=owner_session, status="running", landscape_run_id=str(run_id)
        )
        mock_session_service.assess_run_start_admission.side_effect = [
            SimpleNamespace(state=StartPermitState.PENDING, execution_refusal=None),
            SimpleNamespace(state=StartPermitState.REFUSED, execution_refusal=AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH),
        ]
        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _blob_rows_record_for_entry(entry, session_id=owner_session)
        service._blob_service = blob_service
        pipeline_config = PipelineConfig(
            sources={"main": MagicMock(spec=SourceProtocol)},
            transforms=[],
            sinks={"primary": MagicMock(spec=SinkProtocol)},
            config={"changed": True},
        )

        with (
            patch.object(service, "_approval_inputs_from_frozen", return_value=_governed_approval_inputs(service)),
            patch.object(service, "_require_current_binding_generation"),
            patch.object(service, "_settle_admission_refusal", new_callable=AsyncMock) as settle,
            patch("elspeth.core.landscape.run_start_admission.RunStartAdmissionRepository.observe", return_value=None),
            patch("elspeth.web.execution.service.open_landscape_db"),
            patch("elspeth.web.execution.service.FilesystemPayloadStore") as payload_store,
            patch("elspeth.web.execution.service.read_retained_input", return_value=content),
            patch("elspeth.web.execution.service.load_settings_from_config_dict", return_value=_mock_pipeline_settings()),
            patch("elspeth.web.execution.service.build_validated_runtime_graph") as graph,
            patch("elspeth.web.execution.service.assemble_and_validate_pipeline_config", return_value=pipeline_config),
            patch("elspeth.web.execution.service.audit_safe_resolved_config", return_value={"changed": True}),
        ):
            graph.return_value = SimpleNamespace(plugin_bundle=_plugin_bundle_stub(), graph=_execution_graph_stub())
            payload_store.return_value.store.return_value = entry["payload_ref"]
            outcome = service._run_pipeline(
                str(run_id),
                _blob_rows_pipeline_yaml([entry]),
                threading.Event(),
                frozen,
                "author",
                session_operation_lease=_execute_lease(),
                durable_admission=True,
                restored_envelope=cast(Any, envelope),
            )

        assert outcome is None
        assert mock_session_service.assess_run_start_admission.await_count == 2
        settle.assert_awaited_once()
        payload_store.return_value.store.assert_not_called()

    def test_inline_content_is_compiled_from_verified_bytes(self, service: ExecutionServiceImpl, mock_session_service: MagicMock) -> None:
        del mock_session_service
        content = b"Verified user prompt"
        blob_id = uuid4()
        session_id = uuid4()
        digest = hashlib.sha256(content).hexdigest()
        service.set_openrouter_catalog_snapshot(sha256="b" * 64, source="bundled")
        blob_service = create_autospec(BlobServiceProtocol, instance=True)
        blob_service.get_blob.return_value = _blob_record_stub(
            blob_id=blob_id,
            session_id=session_id,
            content_hash=digest,
            size_bytes=len(content),
            mime_type="text/plain",
            status="ready",
            creation_modality=CreationModality.VERBATIM,
        )
        blob_service.read_blob_content.return_value = content
        service._blob_service = blob_service
        marker = {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": digest}
        config = {
            "source": {"plugin": "csv", "options": {}},
            "transforms": [{"name": "classify", "plugin": "llm", "options": {"prompt_template": marker}}],
        }
        snapshot = service._plugin_snapshot_for_user("author", operation="approval test")
        frozen = FrozenRunSettings(plugin_snapshot=snapshot, executable_config=config, audit_safe_config=config)
        loaded: list[dict[str, Any]] = []

        def load(config_dict: dict[str, Any], *, expand_env_vars: bool) -> _ModelCopyNamespace:
            assert expand_env_vars is False
            loaded.append(config_dict)
            return _mock_pipeline_settings()

        with (
            patch("elspeth.web.execution.service.load_settings_from_config_dict", side_effect=load),
            patch("elspeth.web.operator_telemetry.apply_operator_pipeline_telemetry", side_effect=lambda settings, _web: settings),
            patch(
                "elspeth.web.execution.export_marking.apply_operator_export_marking",
                return_value=_mock_pipeline_settings().landscape.export,
            ),
            patch("elspeth.web.execution.service.audit_safe_resolved_config", side_effect=lambda *_args, **_kwargs: loaded[-1]),
            patch("elspeth.web.execution.service.runtime_val_manifest_sha256", return_value="c" * 64),
        ):
            inputs = service._approval_inputs_from_frozen(
                frozen,
                user_id="author",
                session_id=session_id,
                session_operation_context=_execute_lease().context,
            )

        assert loaded[0]["transforms"][0]["options"]["prompt_template"] == content.decode()
        assert inputs.config_hash == stable_hash(loaded[0])
        blob_service.get_blob.assert_awaited_once()
        blob_service.read_blob_content.assert_awaited_once()

    def test_inline_content_hash_drift_refuses_approval_compilation(self, service: ExecutionServiceImpl) -> None:
        content = b"Original user prompt"
        blob_id = uuid4()
        session_id = uuid4()
        digest = hashlib.sha256(content).hexdigest()
        service.set_openrouter_catalog_snapshot(sha256="b" * 64, source="bundled")
        blob_service = create_autospec(BlobServiceProtocol, instance=True)
        blob_service.get_blob.return_value = _blob_record_stub(
            blob_id=blob_id,
            session_id=session_id,
            content_hash=digest,
            size_bytes=len(content),
            mime_type="text/plain",
            status="ready",
            creation_modality=CreationModality.VERBATIM,
        )
        blob_service.read_blob_content.return_value = b"Changed user prompt"
        service._blob_service = blob_service
        config = {
            "source": {"plugin": "csv", "options": {}},
            "transforms": [
                {
                    "name": "classify",
                    "plugin": "llm",
                    "options": {"prompt_template": {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": digest}},
                }
            ],
        }
        snapshot = service._plugin_snapshot_for_user("author", operation="approval test")
        frozen = FrozenRunSettings(plugin_snapshot=snapshot, executable_config=config, audit_safe_config=config)
        with pytest.raises(BlobIntegrityError):
            service._approval_inputs_from_frozen(
                frozen,
                user_id="author",
                session_id=session_id,
                session_operation_context=_execute_lease().context,
            )

    @pytest.mark.asyncio
    async def test_missing_approval_refuses_before_run_creation(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, tmp_path: Path, execution_fixture: ExecutionTestCustody
    ) -> None:
        service._settings.workflow_governance = "on"
        service._settings.data_dir = tmp_path
        source = tmp_path / "input.csv"
        source.write_bytes(b"name\nAda\n")
        cast(_YamlGeneratorStub, service._yaml_generator).result = json.dumps(
            {"source": {"plugin": "csv", "options": {"path": str(source)}}}
        )
        approval = _governed_approval_inputs(service)
        mock_session_service.check_approval_binding.return_value = AdmissionRefusalReason.APPROVAL_REQUIRED

        with (
            patch.object(service, "_approval_inputs_from_frozen", return_value=approval),
            pytest.raises(ExecutionApprovalRequired) as exc_info,
        ):
            await _execute(service, session_id=uuid4(), user_id="author", execution_fixture=execution_fixture)

        assert exc_info.value.reason is AdmissionRefusalReason.APPROVAL_REQUIRED
        assert exc_info.value.binding == approval.binding
        mock_session_service.check_approval_binding.assert_awaited_once()
        mock_session_service.create_run.assert_not_awaited()
        assert not (tmp_path / "retained-run-inputs").exists()

    @pytest.mark.asyncio
    async def test_approval_rechecked_after_retaining_source_bytes(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, tmp_path: Path, execution_fixture: ExecutionTestCustody
    ) -> None:
        service._settings.workflow_governance = "on"
        service._settings.data_dir = tmp_path
        source = tmp_path / "input.csv"
        source.write_bytes(b"name\nAda\n")
        cast(_YamlGeneratorStub, service._yaml_generator).result = json.dumps(
            {"source": {"plugin": "csv", "options": {"path": str(source)}}}
        )
        approval = _governed_approval_inputs(service)
        mock_session_service.check_approval_binding.side_effect = [
            None,
            AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH,
        ]

        with (
            patch.object(service, "_approval_inputs_from_frozen", return_value=approval),
            pytest.raises(ExecutionApprovalRequired) as exc_info,
        ):
            await _execute(service, session_id=uuid4(), user_id="author", execution_fixture=execution_fixture)

        assert exc_info.value.reason is AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH
        assert mock_session_service.check_approval_binding.await_count == 2
        assert (tmp_path / "retained-run-inputs").is_dir()
        mock_session_service.create_run.assert_not_awaited()

    def test_worker_rechecks_approval_before_permit_issue(self, service: ExecutionServiceImpl, mock_session_service: MagicMock) -> None:
        service._settings.workflow_governance = "on"
        approval = _governed_approval_inputs(service)
        snapshot = service._plugin_snapshot_for_user("author", operation="approval test")
        frozen = FrozenRunSettings(
            plugin_snapshot=snapshot,
            executable_config={"source": {"plugin": "csv", "options": {}}},
            audit_safe_config={"source": {"plugin": "csv", "options": {}}},
        )
        mock_session_service.assess_run_start_admission.return_value = SimpleNamespace(
            state=StartPermitState.REFUSED, execution_refusal=None
        )
        mock_session_service.assess_run_start_admission.side_effect = None
        with (
            patch.object(service, "_approval_inputs_from_frozen", return_value=approval),
            patch.object(service, "_settle_admission_refusal", new_callable=AsyncMock) as settle,
        ):
            outcome = service._run_pipeline(
                str(uuid4()),
                _TEST_PIPELINE_YAML,
                threading.Event(),
                frozen,
                "author",
                session_operation_lease=_execute_lease(),
                durable_admission=True,
            )
        assert outcome is None
        assert mock_session_service.assess_run_start_admission.await_args.kwargs["approval"] is approval
        mock_session_service.issue_run_start_permit.assert_not_awaited()
        settle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_off_mode_keeps_existing_run_start_calls(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        service._settings.workflow_governance = "off"
        with patch.object(service, "_run_pipeline"):
            await _execute(service, session_id=uuid4(), user_id="author", execution_fixture=execution_fixture)
        mock_session_service.check_approval_binding.assert_not_awaited()
        mock_session_service.create_run.assert_awaited_once()


class TestExecuteSemanticContractViolation:
    """Execution must reject transform pairings that violate semantic contracts.

    Replaces the legacy TestTransformFramingRestriction. The new
    SemanticContractViolationError carries structured ``entries`` and
    ``contracts`` records; the regex assertions still anchor on
    ``line_explode``/``Semantic contract`` because the diagnostic now
    names the consumer plugin and the contract code in the message,
    not the option that the operator must edit (``text_separator``).
    """

    @staticmethod
    def _set_web_scrape_line_explode_state(
        mock_session_service: MagicMock,
        *,
        session_id: UUID,
        scrape_options: dict[str, Any] | None = None,
    ) -> None:
        state = mock_session_service.get_current_state.return_value
        web_scrape_options = {
            "schema": {"mode": "flexible", "fields": ["url: str"]},
            "required_input_fields": ["url"],
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "content_fingerprint",
            "format": "text",
            "fingerprint_mode": "content",
            "http": {
                "abuse_contact": "pipeline@example.com",
                "scraping_reason": "test scrape",
                "allowed_hosts": "public_only",
            },
        }
        web_scrape_options.update(scrape_options or {})
        state.source = {
            "plugin": "text",
            "on_success": "scrape_in",
            "options": {
                "path": f"blobs/{session_id}/urls.txt",
                "column": "url",
                "schema": {"mode": "fixed", "fields": ["url: str"]},
            },
            "on_validation_failure": "discard",
        }
        state.nodes = [
            {
                "id": "scrape_page",
                "node_type": "transform",
                "plugin": "web_scrape",
                "input": "scrape_in",
                "on_success": "explode_in",
                "on_error": "discard",
                "options": web_scrape_options,
            },
            {
                "id": "split_lines",
                "node_type": "transform",
                "plugin": "line_explode",
                "input": "explode_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "schema": {
                        "mode": "flexible",
                        "fields": [
                            "url: str",
                            "content: str",
                            "content_fingerprint: str",
                        ],
                    },
                    "required_input_fields": ["content"],
                    "source_field": "content",
                    "output_field": "line",
                    "include_index": True,
                    "index_field": "line_index",
                },
            },
        ]
        state.edges = None
        state.outputs = [
            {
                "name": "results",
                "plugin": "json",
                "options": {"path": "outputs/lines.json", "format": "json"},
                "on_write_failure": "discard",
            }
        ]

    @pytest.mark.asyncio
    async def test_execute_rejects_compact_web_scrape_text_before_creating_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        mock_settings.data_dir = "/tmp/elspeth_data"
        session_id = uuid4()
        self._set_web_scrape_line_explode_state(mock_session_service, session_id=session_id)

        # SemanticContractViolationError IS a ValueError, so legacy
        # ``except ValueError`` paths still catch it. New callers should
        # catch the specific type and read .entries/.contracts.
        with pytest.raises(ValueError, match="line_explode"):
            await _execute(service, session_id=session_id, execution_fixture=execution_fixture)

        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_compact_text_raises_structured_exception(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """Verify the structured payload — the whole point of the new exception.

        Frontend banners and MCP error renderers consume entries and
        contracts directly; falling back to ``str(exc)`` parsing would
        make this surface as fragile as the pre-Phase-4 string concat.
        """
        from elspeth.web.execution.errors import SemanticContractViolationError

        mock_settings.data_dir = "/tmp/elspeth_data"
        session_id = uuid4()
        self._set_web_scrape_line_explode_state(mock_session_service, session_id=session_id)

        with pytest.raises(SemanticContractViolationError) as excinfo:
            await _execute(service, session_id=session_id, execution_fixture=execution_fixture)

        exc = excinfo.value
        assert len(exc.entries) >= 1
        assert any("Semantic contract" in e.message for e in exc.entries)
        assert any(c.outcome.value == "conflict" for c in exc.contracts)
        assert any(c.consumer_plugin == "line_explode" for c in exc.contracts)

    @pytest.mark.asyncio
    async def test_execute_allows_newline_framed_web_scrape_text(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        mock_settings.data_dir = "/tmp/elspeth_data"
        session_id = uuid4()
        self._set_web_scrape_line_explode_state(
            mock_session_service,
            session_id=session_id,
            scrape_options={"text_separator": "\n"},
        )

        with patch.object(service, "_run_pipeline"):
            run_id = await _execute(service, session_id=session_id, execution_fixture=execution_fixture)

        assert isinstance(run_id, UUID)


class TestFanoutMarkerCollectorArm:
    def test_collector_contributes_a_fanout_marker(self) -> None:
        from elspeth.web.execution.fanout_guard import _fanout_marker_for_node

        marker = _fanout_marker_for_node(_collector_marker_node("collector"))

        assert marker == "collector:c1:batch_replicate"

    def test_collector_is_no_longer_asymmetric_with_aggregation(self) -> None:
        """The defect in one line: the SAME collector-eligible plugin was
        marked on an aggregation and unmarked on a collector. Both kinds
        release plugin-authored output rows, so both must be visible.
        """
        from elspeth.web.execution.fanout_guard import _fanout_marker_for_node

        assert _fanout_marker_for_node(_collector_marker_node("aggregation")) is not None
        assert _fanout_marker_for_node(_collector_marker_node("collector")) is not None

    def test_collector_arm_does_not_key_on_creates_tokens(self) -> None:
        """Pins the trap the arm's comment names.

        ``batch_replicate`` declares ``creates_tokens=False`` yet multiplies
        M = sum(copies), and it is collector-eligible. Narrowing the collector
        arm to ``transform_cls.creates_tokens`` by analogy with the transform
        arm would return None here and silently reinstate the gap. This test
        fails if anyone does that.
        """
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from elspeth.web.execution.fanout_guard import _fanout_marker_for_node

        plugin_cls = get_shared_plugin_manager().get_transform_by_name("batch_replicate")
        assert plugin_cls.creates_tokens is False, "premise: the counter-example still declares creates_tokens=False"
        assert plugin_cls.is_batch_aware is True, "premise: the counter-example is still collector-eligible"

        assert _fanout_marker_for_node(_collector_marker_node("collector")) is not None

    def test_collector_marker_reaches_the_guard_trace(self, tmp_path: Path) -> None:
        """End to end: the collector's marker must appear in the risk row's
        ``upstream_fanout``, not merely somewhere in the guard. The opener's
        marker is present too and is the control — before the fix the tuple
        held only the opener.
        """
        import csv

        from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        source_path = tmp_path / "in.csv"
        with source_path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["a"])
            for index in range(20):
                writer.writerow([f"r{index}"])

        opener = NodeSpec(
            id="op",
            node_type="transform",
            plugin="json_explode",
            input="src_out",
            on_success="op_out",
            on_error="errors",
            options={"source_field": "a"},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="src_out",
                options={"path": "in.csv"},
                on_validation_failure="discard",
            ),
            nodes=(opener, _collector_marker_node("collector"), _fanout_llm_node("c1_out", node_id="gen")),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        guard = evaluate_execution_fanout_guard(state, data_dir=str(tmp_path))

        assert guard is not None
        upstream = guard.risks[0].upstream_fanout
        assert "collector:c1:batch_replicate" in upstream
        assert "transform:op:json_explode" in upstream  # control: the opener was always marked
