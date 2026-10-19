"""Execution service lifecycle and terminal-state tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    InterfaceError,
    OperationalError,
    PendingRollbackError,
    ProgrammingError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from elspeth.contracts.enums import RunStatus
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.core.config import LandscapeExportSettings, load_bounded_pipeline_yaml
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorSignoffGateFact
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.envelope import RunExecutionInput, validate_run_execution_input
from elspeth.web.execution.errors import CompletionGateIntegrityError, ExecutionReadinessError, PipelineValidationError
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.protocol import FrozenRunSettings
from elspeth.web.execution.schemas import ValidationError, ValidationReadiness, ValidationReadinessBlocker, ValidationResult
from elspeth.web.execution.service import _LOSS_WATCHER_ESCALATE_AFTER_FAILURES, ExecutionServiceImpl, close_execute_lease_before_transfer
from elspeth.web.execution.validation import validate_pipeline as _real_validate_pipeline
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, prompt_review_anchor_hash_from_options
from elspeth.web.plugin_policy.models import PluginAvailability, PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import LEGAL_RUN_TRANSITIONS, IllegalRunTransitionError, RunAlreadyActiveError, SessionRunStatus
from elspeth.web.sessions.telemetry import build_sessions_telemetry, observed_value
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.helpers.session_fences import make_blob_read_context
from tests.unit.core.test_audit_export_config import _enabled_config
from tests.unit.web.execution import test_service as _service_tests
from tests.unit.web.execution.test_service import (
    _RESOLVED_TEST_PIPELINE_YAML,
    _TEST_PIPELINE_YAML,
    _admitted_runtime_setup,
    _blob_service_stub,
    _collect_terminal_types,
    _composition_state_record,
    _configure_runtime_success,
    _execute,
    _execute_lease,
    _execution_graph_stub,
    _join_loop_cleanup_worker,
    _mock_pipeline_settings,
    _orchestrator_result_stub,
    _orchestrator_stub,
    _plugin_bundle_stub,
    _ready_csv_blob_for_execution,
    _run_accounting_for_status,
    _run_record_stub,
    _successful_core_validation_result,
    _WebSettingsStub,
    _YamlGeneratorStub,
)

_live_execute_lease = _service_tests._live_execute_lease
broadcaster = _service_tests.broadcaster
execution_fixture = _service_tests.execution_fixture
isolate_raw_sink_effect_eligibility_gate = _service_tests.isolate_raw_sink_effect_eligibility_gate
mock_loop = _service_tests.mock_loop
mock_pipeline_config_assembly = _service_tests.mock_pipeline_config_assembly
mock_session_service = _service_tests.mock_session_service
mock_settings = _service_tests.mock_settings
real_loop = _service_tests.real_loop
service = _service_tests.service


class TestExecutionFlow:
    @pytest.mark.parametrize("authored_compartment", [None, "forged"])
    @pytest.mark.parametrize("signing_mode", ["unsigned", "hmac_sha256"])
    @pytest.mark.asyncio
    async def test_operator_export_marking_precedes_web_model_load_and_preserves_authored_yaml(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        authored_compartment: str | None,
        signing_mode: str,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        service._settings.compartment_id = "operator-a"
        authored_export = _enabled_config(compartment_id=authored_compartment)
        if signing_mode == "hmac_sha256":
            authored_export.update(signing_mode=signing_mode, signer_key_id="signer-a", signing_secret_ref="SIGNER_KEY")
        authored_config = {"source": {"plugin": "csv", "options": {}}, "landscape": {"export": authored_export}}
        cast(_YamlGeneratorStub, service._yaml_generator).result = json.dumps(authored_config)
        effective_export = LandscapeExportSettings.model_validate({**authored_export, "compartment_id": "operator-a"})
        parsed = SimpleNamespace(landscape=SimpleNamespace(export=effective_export))

        with (
            patch("elspeth.web.execution.service.load_settings_from_config_dict", return_value=parsed) as load_settings,
            patch.object(service, "_run_pipeline"),
        ):
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

        loaded_export = load_settings.call_args.args[0]["landscape"]["export"]
        assert loaded_export["compartment_id"] == "operator-a"
        assert mock_session_service.create_run.await_args.kwargs["pipeline_yaml"] is not None
        saved_yaml = load_bounded_pipeline_yaml(mock_session_service.create_run.await_args.kwargs["pipeline_yaml"])
        assert saved_yaml["landscape"]["export"]["compartment_id"] == authored_compartment

    @pytest.mark.parametrize("signing_mode", ["unsigned", "hmac_sha256"])
    @pytest.mark.asyncio
    async def test_export_missing_operator_compartment_refuses_before_run(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, signing_mode: str, execution_fixture: ExecutionTestCustody
    ) -> None:
        export = LandscapeExportSettings.model_construct(enabled=True, signing_mode=signing_mode, compartment_id=None)
        cast(_YamlGeneratorStub, service._yaml_generator).result = json.dumps(
            {"source": {"plugin": "csv", "options": {}}, "landscape": {"export": {"enabled": True}}}
        )
        parsed = SimpleNamespace(landscape=SimpleNamespace(export=export))

        with (
            patch("elspeth.web.execution.service.load_settings_from_config_dict", return_value=parsed),
            patch.object(service, "_run_pipeline"),
            pytest.raises(ValueError, match="compartment_id"),
        ):
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_web_export_refuses_deployment_auth_coverage_before_run(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        service._settings.compartment_id = "operator-a"
        cast(_YamlGeneratorStub, service._yaml_generator).result = json.dumps(
            {
                "sources": {"input": {"plugin": "csv", "on_success": "output", "options": {}}},
                "sinks": {
                    "output": {"plugin": "csv", "on_write_failure": "discard", "options": {}},
                    "archive": {"plugin": "csv", "on_write_failure": "discard", "options": {}},
                },
                "landscape": {"export": _enabled_config(compartment_id=None, auth_events="deployment_snapshot")},
            }
        )

        with (
            patch.object(service, "_run_pipeline"),
            pytest.raises(ValueError, match="auth_events=deployment_snapshot"),
        ):
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

        mock_session_service.create_run.assert_not_awaited()

    def test_queued_run_rejects_rotated_plugin_binding_before_runtime_config_use(self, service: ExecutionServiceImpl) -> None:
        base = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())

        def with_generation(value: str) -> PluginAvailabilitySnapshot:
            return PluginAvailabilitySnapshot.create(
                policy_hash=base.policy_hash,
                principal_scope="local:alice",
                available=base.available,
                unavailable=base.unavailable,
                selected=base.selected,
                usable_profile_aliases=base.usable_profile_aliases,
                selected_profile_aliases=base.selected_profile_aliases,
                binding_generation_fingerprint=value,
                control_modes=base.control_modes,
            )

        frozen = FrozenRunSettings(
            plugin_snapshot=with_generation("generation-before-queue"),
            executable_config={},
            audit_safe_config={},
        )
        service._plugin_snapshot_factory = lambda _user_id: with_generation("generation-after-queue")

        with pytest.raises(RuntimeError, match="binding changed while execution was queued"):
            service._require_current_binding_generation(frozen, user_id="alice")

    @pytest.mark.asyncio
    async def test_execute_returns_run_id_immediately(self, service: ExecutionServiceImpl, execution_fixture: ExecutionTestCustody) -> None:
        """execute() returns a UUID without blocking on pipeline completion."""
        with patch.object(service, "_run_pipeline"):
            run_id = await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_execute_rejects_non_string_yaml_generator_output(
        self, service: ExecutionServiceImpl, execution_fixture: ExecutionTestCustody
    ) -> None:
        """YamlGenerator contract violations must fail fast, not spin in PyYAML."""
        cast(_YamlGeneratorStub, service._yaml_generator).result = object()

        with pytest.raises(TypeError, match="must return str"):
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

    @pytest.mark.asyncio
    async def test_execute_creates_run_via_session_service(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """Admission persists the immutable execution input under the exact EXECUTE lease."""
        session_id = uuid4()
        state_record = mock_session_service.get_current_state.return_value
        authority = execution_fixture.observe_authority(session_id)
        with patch.object(service, "_run_pipeline"):
            await _execute(service, session_id=session_id, authority=authority, execution_fixture=execution_fixture)
        acquired = [context for name, context in authority.calls if name == "acquire"]
        assert len(acquired) == 1
        mock_session_service.create_run.assert_awaited_once()
        create_call = mock_session_service.create_run.await_args
        execution_input = create_call.kwargs["execution_input"]
        assert type(execution_input) is RunExecutionInput
        validate_run_execution_input(execution_input)
        envelope_document = json.loads(execution_input.envelope_json)
        assert envelope_document["executable_config"] == {"source": {"plugin": "csv", "options": {}}}
        assert envelope_document["audit_safe_config"] == envelope_document["executable_config"]
        assert create_call.args == ()
        assert create_call.kwargs == {
            "session_id": session_id,
            "state_id": state_record.id,
            "pipeline_yaml": _RESOLVED_TEST_PIPELINE_YAML,
            "execution_input": execution_input,
            # The exact context of the EXECUTE lease execute() was handed.
            "session_operation_context": acquired[0],
        }

    @pytest.mark.asyncio
    async def test_execute_freezes_snapshot_and_distinct_configs_before_submission(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: _WebSettingsStub,
        tmp_path: Path,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        session_id = uuid4()
        (tmp_path / "blobs").mkdir()
        (tmp_path / "outputs").mkdir()
        source_path = tmp_path / "blobs" / "input.txt"
        output_path = tmp_path / "outputs" / "output.jsonl"
        source_path.write_text("Ada\n", encoding="utf-8")
        mock_settings.data_dir = tmp_path
        state_record = _composition_state_record(
            session_id=session_id,
            source_path=source_path,
            output_path=output_path,
            nodes=[],
        )
        mock_session_service.get_current_state.return_value = state_record
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
        service._plugin_snapshot_factory = lambda _user_id: snapshot
        physical_submit = service._executor.submit

        with (
            patch.object(service, "_run_pipeline", return_value=None),
            patch.object(service._executor, "submit", wraps=physical_submit) as submit,
        ):
            await _execute(service, session_id=session_id, user_id="alice", execution_fixture=execution_fixture)

        submitted_args = submit.call_args.args[0].args
        assert submitted_args[3].plugin_snapshot is snapshot
        assert submitted_args[3].executable_config is not submitted_args[3].audit_safe_config

    @pytest.mark.asyncio
    async def test_execute_lowers_profile_into_fanout_guard_and_frozen_executable_config(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: _WebSettingsStub,
        tmp_path: Path,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from elspeth.web.composer import yaml_generator
        from elspeth.web.config import WebSettings
        from elspeth.web.execution.fanout_guard import ExecutionFanoutGuardRequired, evaluate_execution_fanout_guard
        from elspeth.web.interpretation_state import InterpretationReviewPending, materialize_state_for_execution
        from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
        from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
        from elspeth.web.plugin_policy.validation import validate_plugin_policy

        private_model = "bedrock/anthropic.claude-3-haiku-20240307-v1:0"
        private_region = "ap-southeast-2"
        web_settings = WebSettings(
            data_dir=tmp_path,
            composer_max_composition_turns=4,
            composer_max_discovery_turns=4,
            composer_timeout_seconds=60,
            composer_rate_limit_per_minute=20,
            shareable_link_signing_key=b"0123456789abcdef0123456789abcdef",
            llm_profiles={
                "tutorial": {
                    "provider": "bedrock",
                    "model": private_model,
                    "region_name": private_region,
                }
            },
            default_llm_profile="tutorial",
        )
        runtime_policy = RuntimeWebPluginConfig.from_settings(web_settings)
        policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime_policy)
        profiles = OperatorProfileRegistry(policy=policy, settings=runtime_policy)
        unrestricted = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
        snapshot = PluginAvailabilitySnapshot.create(
            policy_hash=policy.policy_hash,
            principal_scope="test:alice",
            available=unrestricted.available,
            unavailable=(),
            selected=unrestricted.selected,
            usable_profile_aliases=((PluginId("transform", "llm"), ("tutorial",)),),
            selected_profile_aliases=((PluginId("transform", "llm"), "tutorial"),),
            binding_generation_fingerprint=stable_hash("runtime-policy-generation"),
        )
        session_id = uuid4()
        (tmp_path / "blobs").mkdir()
        (tmp_path / "outputs").mkdir()
        source_path = tmp_path / "blobs" / "input.txt"
        source_path.write_text("Ada\n" * 34, encoding="utf-8")
        mock_settings.data_dir = tmp_path
        summarise_options: dict[str, Any] = {
            "profile": "tutorial",
            "prompt_template": "Summarise {{ row }}",
            "schema": {"mode": "observed", "fields": None},
            "required_input_fields": [],
            "queries": [
                {"name": "summary", "input_fields": {"text": "body"}},
                {"name": "sentiment", "input_fields": {"text": "body"}},
                {"name": "topics", "input_fields": {"text": "body"}},
            ],
        }
        # A multi-query node's accepted review is anchored to the whole prompt
        # SURFACE (per-query templates + system prompt + node-level template),
        # exactly as the live resolve path writes it; a bare hash of the
        # node-level prompt_template is not that anchor, and execution
        # refuses it as drift.
        summarise_options[INTERPRETATION_REQUIREMENTS_KEY] = [
            {
                "id": "prompt_template_review:summarise",
                "kind": "llm_prompt_template",
                "user_term": "llm_prompt_template:summarise",
                "status": "resolved",
                "draft": "Summarise {{ row }}",
                "event_id": "prompt-template-accepted:summarise",
                "accepted_value": "Summarise {{ row }}",
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": prompt_review_anchor_hash_from_options(summarise_options),
            }
        ]
        state_record = _composition_state_record(
            session_id=session_id,
            source_path=source_path,
            output_path=tmp_path / "outputs" / "output.jsonl",
            nodes=[
                {
                    "id": "summarise",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "source_rows",
                    "on_success": "out",
                    "on_error": "discard",
                    "options": summarise_options,
                }
            ],
        )
        mock_session_service.get_current_state.return_value = state_record
        service._yaml_generator = yaml_generator
        service._plugin_snapshot_factory = lambda _user_id: snapshot
        service._operator_profile_registry = profiles
        physical_submit = service._executor.submit

        with (
            patch.object(service, "_run_pipeline", return_value=None),
            patch.object(service._executor, "submit", wraps=physical_submit) as submit,
        ):
            with pytest.raises(ExecutionFanoutGuardRequired) as raised:
                await _execute(service, session_id=session_id, user_id="alice", execution_fixture=execution_fixture)

            risk = raised.value.guard.risks[0]
            assert risk.provider == "bedrock"
            assert risk.model == private_model
            assert risk.provider_calls_per_row == 3
            assert risk.estimated_provider_calls == 102
            materialized_state = materialize_state_for_execution(state_from_record(state_record))
            assert not isinstance(materialized_state, InterpretationReviewPending)
            executable_state = validate_plugin_policy(
                materialized_state,
                snapshot=snapshot,
                profile_registry=profiles,
                catalog=create_catalog_service(),
            ).executable_state
            expected_guard = evaluate_execution_fanout_guard(executable_state, data_dir=tmp_path)
            assert expected_guard is not None
            assert raised.value.guard.ack_token == expected_guard.ack_token
            assert private_region in json.dumps(executable_state.to_dict(), default=dict)
            assert private_region not in json.dumps(raised.value.guard.to_dict())

            await _execute(
                service,
                session_id=session_id,
                user_id="alice",
                fanout_ack_token=raised.value.guard.ack_token,
                execution_fixture=execution_fixture,
            )

        frozen = submit.call_args.args[0].args[3]
        executable = json.dumps(frozen.executable_config, default=dict)
        audit_safe = json.dumps(frozen.audit_safe_config, default=dict)
        assert private_model in executable
        assert private_region in executable
        assert "tutorial" in audit_safe
        assert private_model not in audit_safe
        assert private_region not in audit_safe

    @pytest.mark.asyncio
    async def test_execute_rejects_invalid_pipeline_before_run_creation(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """Fix 1 (notes/composer-advisor-surface-map-2026-06-08.md): execute() must
        fail CLOSED on a pipeline that fails validate_pipeline — raising a structured
        PipelineValidationError BEFORE create_run — instead of launching an opaque
        ``status=failed`` run. This closes the tutorial bypass (tutorial_service calls
        execute() directly with no pre-run validation) and the advisory-gate gap.
        """
        invalid = ValidationResult(
            is_valid=False,
            checks=[],
            errors=[
                ValidationError(
                    component_id="rate",
                    component_type="transform",
                    message="Graph validation failed: 'rate' requires field 'content' not emitted upstream",
                    suggestion=None,
                    error_code=None,
                )
            ],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=False,
                completion_ready=False,
                blockers=[
                    ValidationReadinessBlocker(
                        suggestion=None,
                        note=None,
                        code="graph_structure",
                        component_id="rate",
                        component_type="transform",
                        detail="Graph validation failed.",
                    )
                ],
            ),
        )
        with (
            patch("elspeth.web.execution.validation.validate_pipeline", return_value=invalid),
            pytest.raises(PipelineValidationError) as exc_info,
        ):
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

        # No opaque failed-run: the gate refuses BEFORE create_run.
        assert mock_session_service.create_run.await_count == 0
        # Carries the structured errors for the route to surface as a 422.
        assert exc_info.value.errors
        assert exc_info.value.errors[0].component_id == "rate"

    @pytest.mark.asyncio
    async def test_execute_rejects_backend_execution_readiness_before_run_creation(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """Execution admission follows readiness even when validation is green."""
        blocker = ValidationReadinessBlocker(
            suggestion=None,
            note=None,
            code="runtime_admission",
            component_id="pipeline",
            component_type="pipeline",
            detail="The selected runtime policy does not admit this pipeline.",
        )
        not_execution_ready = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=False,
                completion_ready=False,
                blockers=[blocker],
            ),
        )

        with (
            patch("elspeth.web.execution.validation.validate_pipeline", return_value=not_execution_ready),
            pytest.raises(ExecutionReadinessError) as exc_info,
        ):
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

        assert exc_info.value.blockers == (blocker,)
        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_rejects_backend_execution_readiness_without_blockers(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        not_execution_ready = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=False,
                completion_ready=False,
                blockers=[],
            ),
        )

        with (
            patch("elspeth.web.execution.validation.validate_pipeline", return_value=not_execution_ready),
            pytest.raises(ExecutionReadinessError) as exc_info,
        ):
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

        assert exc_info.value.blockers == ()
        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_rejects_saved_now_disabled_plugin_before_run_or_constructor(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: _WebSettingsStub,
        tmp_path: Path,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        session_id = uuid4()
        (tmp_path / "blobs").mkdir()
        (tmp_path / "outputs").mkdir()
        source_path = tmp_path / "blobs" / "input.txt"
        source_path.write_text("Ada\n", encoding="utf-8")
        mock_settings.data_dir = tmp_path
        mock_session_service.get_current_state.return_value = _composition_state_record(
            session_id=session_id,
            source_path=source_path,
            output_path=tmp_path / "outputs" / "output.jsonl",
            nodes=[],
        )
        unrestricted = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
        disabled = PluginId("sink", "json")
        snapshot = PluginAvailabilitySnapshot.create(
            policy_hash="runtime-policy",
            principal_scope="test:alice",
            available=unrestricted.available - {disabled},
            unavailable=(PluginAvailability(disabled, PluginUnavailableReason.NOT_AUTHORIZED),),
            selected=unrestricted.selected,
            usable_profile_aliases=(),
            selected_profile_aliases=(),
            binding_generation_fingerprint="runtime-policy-generation",
        )
        service._plugin_snapshot_factory = lambda _user_id: snapshot

        with (
            patch("elspeth.web.execution.validation.validate_pipeline", side_effect=_real_validate_pipeline),
            patch("elspeth.web.execution.validation.instantiate_runtime_plugins") as instantiate,
            patch.object(service._executor, "submit") as submit,
            pytest.raises(PipelineValidationError),
        ):
            await _execute(service, session_id=session_id, user_id="alice", execution_fixture=execution_fixture)

        mock_session_service.create_run.assert_not_awaited()
        instantiate.assert_not_called()
        submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_rejects_aws_s3_endpoint_url_before_run_or_provider_instantiation(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        endpoint_sentinel = "https://provider-canary.attacker.invalid/private"
        state_record = mock_session_service.get_current_state.return_value
        state_record.source = {
            "plugin": "aws_s3",
            "on_success": "out",
            "options": {"endpoint_url": endpoint_sentinel},
            "on_validation_failure": "discard",
        }
        state_record.sources = None
        state_record.nodes = []
        state_record.edges = []
        state_record.outputs = [
            {
                "name": "out",
                "plugin": "json",
                "options": {},
                "on_write_failure": "discard",
            }
        ]
        unrestricted = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
        snapshot = PluginAvailabilitySnapshot.create(
            policy_hash="runtime-policy",
            principal_scope="test:alice",
            available=unrestricted.available,
            unavailable=(),
            selected=unrestricted.selected,
            usable_profile_aliases=(),
            selected_profile_aliases=(),
            binding_generation_fingerprint="runtime-policy-generation",
        )
        service._plugin_snapshot_factory = lambda _user_id: snapshot

        with (
            patch("elspeth.web.execution.validation.validate_pipeline", side_effect=_real_validate_pipeline),
            patch("elspeth.web.execution.validation.load_settings_from_yaml_string") as mock_load,
            patch("elspeth.web.execution.validation.instantiate_runtime_plugins") as mock_instantiate,
            patch.object(service._executor, "submit") as mock_submit,
            pytest.raises(PipelineValidationError) as exc_info,
        ):
            await _execute(service, session_id=state_record.session_id, execution_fixture=execution_fixture)

        assert mock_session_service.create_run.await_count == 0
        mock_load.assert_not_called()
        mock_instantiate.assert_not_called()
        mock_submit.assert_not_called()
        assert exc_info.value.errors[0].error_code == "aws_s3_endpoint_url_not_allowed"
        assert endpoint_sentinel not in str(exc_info.value)
        assert endpoint_sentinel not in repr(exc_info.value)

    @pytest.mark.asyncio
    async def test_execute_allows_valid_pipeline_through_the_gate(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """Fix 1: a valid pipeline passes the new pre-run gate and still creates a run."""
        valid = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=True,
                completion_ready=True,
                blockers=[],
            ),
        )
        with (
            patch("elspeth.web.execution.validation.validate_pipeline", return_value=valid),
            patch.object(service, "_run_pipeline"),
        ):
            run_id = await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)
        assert isinstance(run_id, UUID)
        mock_session_service.create_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_merges_selected_state_fact_against_authored_state(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """Completion facts bind to the persisted graph, not runtime materialization."""
        from elspeth.web.composer.state import SourceSpec
        from elspeth.web.execution.completion_gates import (
            CompletionGateFacts,
            completion_gate_fingerprint,
        )
        from elspeth.web.execution.completion_gates import (
            merge_completion_gates as real_merge_completion_gates,
        )

        selected_record = mock_session_service.get_state.return_value
        session_id = selected_record.session_id
        authored_state = state_from_record(selected_record)
        fact = AdvisorSignoffGateFact(
            cause=AdvisorBlockCause.GRAPH_REJECTED,
            suggestion=None,
            detail="The advisor sign-off could not be obtained; the pipeline cannot complete.",
            for_graph=completion_gate_fingerprint(authored_state),
            note=None,
        )
        selected_record.composer_meta = {
            "completion_gates": {
                "schema_version": 2,
                "advisor_signoff": {
                    "cause": "graph_rejected",
                    "suggestion": None,
                    "status": "blocked",
                    "detail": fact.detail,
                    "for_graph": fact.for_graph,
                    "note": None,
                },
            }
        }
        runtime_state = authored_state.with_named_source(
            "runtime_materialized",
            SourceSpec(
                plugin="text",
                on_success="runtime_rows",
                options={},
                on_validation_failure="discard",
            ),
        )
        valid = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=True,
                completion_ready=True,
                blockers=[],
            ),
        )

        with (
            patch("elspeth.web.execution.service.materialize_state_for_execution", return_value=runtime_state),
            patch("elspeth.web.execution.validation.validate_pipeline", return_value=valid),
            patch(
                "elspeth.web.execution.service.merge_completion_gates",
                wraps=real_merge_completion_gates,
            ) as merge,
            patch.object(service, "_run_pipeline"),
        ):
            run_id = await _execute(service, session_id=session_id, state_id=selected_record.id, execution_fixture=execution_fixture)

        assert isinstance(run_id, UUID)
        merge.assert_called_once()
        proof_result, merged_facts, fingerprint_state = merge.call_args.args
        assert proof_result is valid
        assert proof_result.checks == []
        assert merged_facts == CompletionGateFacts(advisor_signoff=fact)
        assert fingerprint_state == authored_state
        mock_session_service.get_current_state.assert_not_awaited()
        mock_session_service.create_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_rejects_corrupt_completion_gate_before_run_creation(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        selected_record = mock_session_service.get_current_state.return_value
        private_persisted_detail = "private-persisted-advisor-detail"
        selected_record.composer_meta = {
            "completion_gates": {
                "schema_version": 2,
                "advisor_signoff": {
                    "status": "blocked",
                    "detail": private_persisted_detail,
                    "suggestion": None,
                    "note": None,
                    "cause": "graph_rejected",
                    # Required for_graph intentionally absent: Tier-1 corruption.
                },
            }
        }

        with pytest.raises(CompletionGateIntegrityError) as exc_info:
            await _execute(service, session_id=selected_record.session_id, execution_fixture=execution_fixture)

        assert private_persisted_detail not in str(exc_info.value)
        assert private_persisted_detail not in repr(exc_info.value)
        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_validate_returns_structured_failure_when_no_current_state(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """validate() must return a typed no-state blocker instead of raising."""
        session_id = uuid4()
        mock_session_service.get_current_state.return_value = None

        result = await service.validate(session_id, session_operation_context=make_blob_read_context(session_id), user_id="alice")

        assert result.is_valid is False
        assert result.checks[0].name == "state_exists"
        assert result.checks[0].passed is False
        assert result.errors[0].message == "No composition state exists for this session"
        assert result.readiness.authoring_valid is False
        assert result.readiness.blockers[0].code == "state_exists"
        mock_session_service.get_current_state.assert_awaited_once_with(session_id)

    @pytest.mark.asyncio
    async def test_validate_delegates_current_state_to_worker_backed_validate_state(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """validate() materializes the current state and delegates scoped validation."""
        session_id = uuid4()
        expected = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=True,
                completion_ready=True,
                blockers=[],
            ),
        )
        validate_state = AsyncMock(spec=service.validate_state, return_value=expected)
        service.validate_state = validate_state  # type: ignore[method-assign]

        context = make_blob_read_context(session_id)
        result = await service.validate(session_id, session_operation_context=context, user_id="alice")

        assert result is expected
        validate_state.assert_awaited_once()
        delegated_state = validate_state.await_args.args[0]
        assert delegated_state.version == mock_session_service.get_current_state.return_value.version
        assert validate_state.await_args.kwargs == {
            "session_operation_context": context,
            "user_id": "alice",
            "session_id": session_id,
            "completion_gates": None,
        }

    @pytest.mark.asyncio
    async def test_validate_state_runs_validation_in_worker_with_secret_context(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """validate_state() keeps sync validation off the event loop."""
        state = state_from_record(mock_session_service.get_current_state.return_value)
        expected = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=True,
                completion_ready=True,
                blockers=[],
            ),
        )

        with patch("elspeth.web.execution.service.run_sync_in_worker", new_callable=AsyncMock) as run_worker:
            run_worker.return_value = expected
            session_id = uuid4()
            context = make_blob_read_context(session_id)
            result = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)

        assert result is expected
        run_worker.assert_awaited_once()
        assert run_worker.await_args.kwargs == {}
        (worker_call,) = run_worker.await_args.args
        # The handoff is a local function whose body is a direct call the
        # session-operation-lease gate can follow (elspeth-01e919e13e), not a
        # ``partial`` that hides the callee and its context from the walk.
        assert not isinstance(worker_call, partial)
        assert worker_call.__qualname__ == "ExecutionServiceImpl._authoritative_state_preflight.<locals>._preflight"
        with patch.object(service, "_authoritative_state_preflight_sync", autospec=True, return_value=expected) as preflight_sync:
            assert worker_call() is expected
        preflight_sync.assert_called_once()
        assert preflight_sync.call_args.args == (state,)
        forwarded = preflight_sync.call_args.kwargs
        assert set(forwarded) == {"plugin_snapshot", "user_id", "session_id", "session_operation_context"}
        assert forwarded["user_id"] == "alice"
        assert forwarded["session_id"] == session_id
        assert forwarded["session_operation_context"] is context
        assert forwarded["plugin_snapshot"] is not None

    @pytest.mark.asyncio
    async def test_validate_state_merges_persisted_completion_gate(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """A persisted advisor sign-off blocker survives the fresh recompute."""
        from elspeth.web.execution.completion_gates import (
            CompletionGateFacts,
            completion_gate_fingerprint,
        )
        from elspeth.web.execution.schemas import ADVISOR_SIGNOFF_BLOCKED_CODE, ValidationCheck

        state = state_from_record(mock_session_service.get_current_state.return_value)
        facts = CompletionGateFacts(
            advisor_signoff=AdvisorSignoffGateFact(
                cause=AdvisorBlockCause.GRAPH_REJECTED,
                suggestion=None,
                detail="The advisor sign-off could not be obtained; the pipeline cannot complete.",
                for_graph=completion_gate_fingerprint(state),
                note=None,
            )
        )

        core = _successful_core_validation_result()
        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=core,
        ):
            session_id = uuid4()
            result = await service.validate_state(
                state,
                session_operation_context=make_blob_read_context(session_id),
                user_id="alice",
                session_id=session_id,
                completion_gates=facts,
            )

        assert result.is_valid is True
        assert result.readiness.authoring_valid is True
        assert result.readiness.execution_ready is True
        assert result.readiness.completion_ready is False
        assert [blocker.code for blocker in result.readiness.blockers] == [ADVISOR_SIGNOFF_BLOCKED_CODE]
        assert result.checks == [
            *core.checks,
            ValidationCheck(
                name="advisor_signoff",
                passed=False,
                detail="The advisor sign-off could not be obtained; the pipeline cannot complete.",
                affected_nodes=(),
                outcome_code=None,
            ),
        ]

    @pytest.mark.asyncio
    async def test_validate_passes_record_completion_gates_to_validate_state(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """validate() parses the record's composer_meta and threads the facts through."""
        from elspeth.web.execution.completion_gates import CompletionGateFacts

        session_id = uuid4()
        mock_session_service.get_current_state.return_value.composer_meta = {
            "completion_gates": {
                "schema_version": 2,
                "advisor_signoff": {
                    "cause": "graph_rejected",
                    "suggestion": None,
                    "status": "blocked",
                    "detail": "The advisor sign-off could not be obtained; the pipeline cannot complete.",
                    "for_graph": "0" * 64,
                    "note": None,
                },
            }
        }
        expected = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=True,
                completion_ready=True,
                blockers=[],
            ),
        )
        validate_state = AsyncMock(spec=service.validate_state, return_value=expected)
        service.validate_state = validate_state  # type: ignore[method-assign]

        await service.validate(session_id, session_operation_context=make_blob_read_context(session_id), user_id="alice")

        validate_state.assert_awaited_once()
        assert validate_state.await_args.kwargs["completion_gates"] == CompletionGateFacts(
            advisor_signoff=AdvisorSignoffGateFact(
                cause=AdvisorBlockCause.GRAPH_REJECTED,
                suggestion=None,
                detail="The advisor sign-off could not be obtained; the pipeline cannot complete.",
                for_graph="0" * 64,
                note=None,
            )
        )

    @pytest.mark.asyncio
    async def test_get_status_returns_run_status(self, service: ExecutionServiceImpl, mock_session_service: MagicMock) -> None:
        run_id = uuid4()
        mock_session_service.get_run.return_value = _run_record_stub(
            id=run_id,  # B7: RunRecord uses `id`, not `run_id`
            status="running",
            started_at=datetime.now(tz=UTC),
            finished_at=None,
            rows_processed=50,
            rows_succeeded=48,
            rows_failed=2,
            rows_routed_success=0,
            rows_routed_failure=0,
            rows_quarantined=0,
            error=None,
            landscape_run_id=None,
        )
        status = await service.get_status(run_id)
        assert status.status == "running"
        assert status.accounting is None


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestB2ShutdownEvent:
    """B2 fix: _run_pipeline() MUST pass shutdown_event to orchestrator.run().

    If shutdown_event is omitted, the Orchestrator calls signal.signal()
    from the worker thread, raising ValueError: signal only works in main thread.
    """

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_shutdown_event_passed_to_orchestrator_run(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        mock_load.return_value = _mock_pipeline_settings()
        mock_bundle = _plugin_bundle_stub()
        mock_instantiate.return_value = mock_bundle
        mock_graph = _execution_graph_stub()
        mock_graph_cls.from_plugin_instances.return_value = mock_graph
        shutdown_event = threading.Event()
        run_id = uuid4()

        mock_orch = _orchestrator_stub(_orchestrator_result_stub(run_id=str(run_id)))
        mock_orch_cls.return_value = mock_orch

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED),
        ):
            service._run_pipeline(str(run_id), "source:\n  plugin: csv", shutdown_event, session_operation_lease=_execute_lease())

        # B2 invariant: shutdown_event was passed
        orch_run_call = mock_orch.run.call_args
        assert orch_run_call[1].get("shutdown_event") is shutdown_event, (
            "B2 VIOLATION: shutdown_event not passed to orchestrator.run(). This will cause ValueError: signal only works in main thread."
        )
        assert orch_run_call[1].get("run_id") == str(run_id), (
            "Run diagnostics require the web run UUID to be the Landscape run_id while the run is still active."
        )
        evidence = orch_run_call[1].get("web_plugin_policy_evidence")
        assert isinstance(evidence, WebPluginPolicyEvidence)
        assert evidence.snapshot_hash
        assert evidence.decision_codes == ("policy_allowed",)
        assert "audit_export_content_store" in orch_run_call.kwargs
        assert "audit_export_content_store_resolver" in orch_run_call.kwargs

        running_calls = [call for call in mock_session_service.update_run_status.await_args_list if call.kwargs.get("status") == "running"]
        assert running_calls
        assert running_calls[0].kwargs.get("landscape_run_id") == str(run_id)

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_enabled_web_export_constructs_and_threads_production_store(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
    ) -> None:
        del mock_payload, mock_landscape
        settings = _mock_pipeline_settings()
        export_settings = SimpleNamespace(enabled=True, sink="primary")
        settings.landscape.export = export_settings
        mock_load.return_value = settings
        mock_instantiate.return_value = _plugin_bundle_stub()
        mock_graph_cls.from_plugin_instances.return_value = _execution_graph_stub()
        run_id = str(uuid4())
        mock_orch = _orchestrator_stub(_orchestrator_result_stub(run_id=run_id))
        mock_orch_cls.return_value = mock_orch
        store = object()
        resolver = object()

        with (
            patch(
                "elspeth.web.execution.export_marking.apply_operator_export_marking",
                return_value=export_settings,
            ),
            patch(
                "elspeth.core.audit_export_content_store.create_audit_export_content_store",
                return_value=(store, resolver),
            ) as create_store,
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED),
            ),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

        create_store.assert_called_once_with(export_settings)
        assert mock_orch.run.call_args.kwargs["audit_export_content_store"] is store
        assert mock_orch.run.call_args.kwargs["audit_export_content_store_resolver"] is resolver


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestCancelMechanism:
    @pytest.mark.asyncio
    async def test_cancel_active_run_sets_event(self, service: ExecutionServiceImpl) -> None:
        run_id = uuid4()
        event = threading.Event()
        service._shutdown_events[str(run_id)] = event

        await service.cancel(run_id, user=UserIdentity(user_id="peer-user", username="peer-user"))

        assert event.is_set(), "cancel() must set the threading.Event so the Orchestrator detects it during row processing"

    @pytest.mark.asyncio
    async def test_get_status_reads_durable_cancel_without_local_event(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        run_id = uuid4()
        event = threading.Event()
        event.set()
        service._shutdown_events[str(run_id)] = event
        mock_session_service.get_run.return_value = _run_record_stub(
            id=run_id,
            status="running",
            started_at=datetime.now(UTC),
            finished_at=None,
            error=None,
            landscape_run_id=None,
            cancel_requested_at=datetime.now(UTC),
        )
        del service._shutdown_events[str(run_id)]

        status = await service.get_status(run_id)

        assert status.status == "running"
        assert status.cancel_requested is True

    @pytest.mark.asyncio
    async def test_peer_cancel_is_persisted_without_local_worker(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        from elspeth.web.auth.models import UserIdentity

        run_id = uuid4()
        run = _run_record_stub(id=run_id, status="running")
        mock_session_service.get_run.return_value = run
        mock_session_service.request_run_cancellation.return_value = run
        user = UserIdentity(user_id="peer-user", username="peer-user")
        await service.cancel(run_id, user=user)
        mock_session_service.request_run_cancellation.assert_awaited_once_with(
            run_id,
            session_id=run.session_id,
            user_id=user.user_id,
            auth_provider_type=service._settings.auth_provider,
        )
        mock_session_service.update_run_status.assert_not_called()

    @pytest.mark.parametrize("terminal_status", ["completed", "completed_with_failures", "failed", "empty", "cancelled"])
    @pytest.mark.asyncio
    async def test_cancel_terminal_run_is_noop(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        terminal_status: str,
    ) -> None:
        """Cancelling any terminal run does nothing."""
        run_id = uuid4()
        mock_session_service.get_run.return_value = _run_record_stub(status=terminal_status)
        await service.cancel(run_id, user=UserIdentity(user_id="peer-user", username="peer-user"))
        mock_session_service.update_run_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_idempotent_on_set_event(self, service: ExecutionServiceImpl) -> None:
        """Setting an already-set event is safe."""
        run_id = uuid4()
        event = threading.Event()
        event.set()
        service._shutdown_events[str(run_id)] = event

        # Should not raise
        await service.cancel(run_id, user=UserIdentity(user_id="peer-user", username="peer-user"))
        assert event.is_set()

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_cancelled_run_broadcasts_cancelled_event(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """When orchestrator raises GracefulShutdownError, _run_pipeline
        broadcasts 'cancelled' and updates status accordingly."""
        from elspeth.contracts.errors import GracefulShutdownError

        mock_orch = _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
        )
        assert mock_orch is not None
        # Orchestrator raises GracefulShutdownError on actual cancellation
        mock_orch.run.side_effect = GracefulShutdownError(
            rows_processed=50,
            run_id="test-run-001",
            rows_succeeded=48,
            rows_failed=2,
            rows_routed_success=0,
            rows_routed_failure=0,
            rows_quarantined=0,
        )

        shutdown_event = threading.Event()
        shutdown_event.set()
        run_id = str(uuid4())

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED_WITH_FAILURES),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", shutdown_event, session_operation_lease=_execute_lease())

        # The test sets shutdown_event BEFORE _run_pipeline, so the early
        # shutdown check (line 534) fires — no orchestrator runs, no row counts.
        # Verify status updated to "cancelled" via the early-exit path.
        status_calls = mock_session_service.update_run_status.call_args_list
        final_status_call = status_calls[-1]
        assert final_status_call.kwargs["status"] == "cancelled"

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_graceful_shutdown_forwards_row_counts(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """GracefulShutdownError row counts are forwarded to update_run_status.

        Regression: prior test only asserted status=='cancelled' but did
        not verify that rows_processed, rows_succeeded, rows_failed, and
        rows_quarantined were propagated from the GSE to the session service.
        """
        from elspeth.contracts.errors import GracefulShutdownError

        mock_orch = _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
        )
        assert mock_orch is not None
        mock_orch.run.side_effect = GracefulShutdownError(
            rows_processed=50,
            run_id="test-run-gse",
            rows_succeeded=48,
            rows_failed=2,
            rows_routed_success=0,
            rows_routed_failure=0,
            rows_quarantined=0,
        )

        # Do NOT set shutdown_event — let _run_pipeline proceed past the
        # early check so orchestrator.run() fires and raises the GSE.
        shutdown_event = threading.Event()
        run_id = str(uuid4())

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED_WITH_FAILURES),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", shutdown_event, session_operation_lease=_execute_lease())

        status_calls = mock_session_service.update_run_status.call_args_list
        # Second call is the GSE handler (first is running transition)
        gse_call = status_calls[-1]
        assert gse_call.kwargs["status"] == "cancelled"
        assert gse_call.kwargs["rows_processed"] == 50
        assert gse_call.kwargs["rows_succeeded"] == 48
        assert gse_call.kwargs["rows_failed"] == 2
        assert gse_call.kwargs["rows_routed_success"] == 0
        assert gse_call.kwargs["rows_routed_failure"] == 0
        assert gse_call.kwargs["rows_quarantined"] == 0

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_completed_run_not_misclassified_when_event_set_late(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Race guard: if shutdown_event is set AFTER orchestrator completes
        (returns normally), the run must still be classified as 'completed',
        not 'cancelled'."""
        run_result = _orchestrator_result_stub(
            run_id="landscape-late-cancel",
            status=RunStatus.COMPLETED_WITH_FAILURES,
            rows_processed=50,
            rows_succeeded=48,
            rows_failed=2,
        )
        mock_orch = _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=run_result,
        )
        assert mock_orch is not None

        # Simulate late cancel: event is set DURING orchestrator.run()
        # (after it returns its result), not before _run_pipeline starts.
        # This tests the race where cancel() fires after the orchestrator
        # finishes but before status is persisted.
        shutdown_event = threading.Event()

        original_return = run_result

        def set_event_on_run(*args: object, **kwargs: object) -> SimpleNamespace:
            shutdown_event.set()
            return original_return

        mock_orch.run.side_effect = set_event_on_run
        run_id = str(uuid4())

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED_WITH_FAILURES),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", shutdown_event, session_operation_lease=_execute_lease())

        # Must be "completed", NOT "cancelled"
        status_calls = mock_session_service.update_run_status.call_args_list
        final_status_call = status_calls[-1]
        assert "completed" in str(final_status_call), f"Expected 'completed' status update, got: {final_status_call}"

    # ── Race condition: cancel() before _run_pipeline starts ──────────

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_run_pipeline_exits_gracefully_when_already_cancelled(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Race fix: if cancel() set DB to 'cancelled' before _run_pipeline
        starts, the pending→running transition fails. _run_pipeline must
        detect this and exit cleanly — no Orchestrator, no crash."""
        run_id = str(uuid4())

        # Simulate: update_run_status("running") raises because status is "cancelled"
        mock_session_service.update_run_status.side_effect = IllegalRunTransitionError("cancelled", "running", frozenset())
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        # Should NOT raise — graceful exit
        with _admitted_runtime_setup():
            service._run_pipeline(run_id, _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

        # No Orchestrator or LandscapeDB instantiated (early return)
        mock_landscape.assert_not_called()
        mock_payload.assert_not_called()

        # Only the one failed "running" attempt — no "failed" status update
        assert mock_session_service.update_run_status.call_count == 1

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_run_pipeline_early_shutdown_skips_setup(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """If shutdown_event is already set when _run_pipeline starts,
        skip all setup and immediately transition to cancelled."""
        run_id = str(uuid4())

        shutdown_event = threading.Event()
        shutdown_event.set()

        service._run_pipeline(run_id, "source:\n  plugin: csv", shutdown_event, session_operation_lease=_execute_lease())

        # No LandscapeDB or PayloadStore constructed (skipped setup)
        mock_landscape.assert_not_called()
        mock_payload.assert_not_called()

        # Status updated to "cancelled"
        status_calls = mock_session_service.update_run_status.call_args_list
        assert len(status_calls) == 1
        assert "cancelled" in str(status_calls[0])

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_run_pipeline_reraises_valueerror_when_not_cancelled(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """If update_run_status raises ValueError for a reason other than
        'already cancelled', _run_pipeline must re-raise (offensive programming)."""
        run_id = str(uuid4())

        mock_session_service.update_run_status.side_effect = IllegalRunTransitionError("completed", "running", frozenset())
        mock_session_service.get_run.return_value = _run_record_stub(status="completed")

        with _admitted_runtime_setup(), pytest.raises(ValueError, match="completed"):
            service._run_pipeline(run_id, _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_running_transition_does_not_swallow_non_illegal_value_errors(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Tier-1 invariant: the four non-illegal-transition ValueError sites in
        update_run_status (run-not-found, landscape_run_id overwrite,
        completed-without-landscape, failed-without-error) must NOT be caught by
        the cancelled-race recovery at the running-transition site (originally
        684).  The only catchable class is IllegalRunTransitionError.

        Discriminator (regression-resistant): under the *old* broad
        ``except ValueError`` the cancelled-race recovery would (a) consult
        get_run, (b) see status == "cancelled", (c) broadcast a "cancelled" SSE
        event, and (d) ``return`` silently — masking the Tier-1 breach as a
        normal cancellation.  The bare ValueError would never propagate.

        Under the narrowed catch, the bare ValueError propagates verbatim
        (preserving the original message), no "cancelled" SSE is emitted from
        the cancelled-race path, and the only get_run call comes from the
        downstream BaseException post-terminal recovery (separate audit-primacy
        machinery — its get_run is correct and expected).

        Without this test a future maintainer could re-widen
        ``except IllegalRunTransitionError`` back to ``except ValueError`` and
        the recovery-path tests would still pass.
        """
        run_id = str(uuid4())

        sentinel_message = "landscape_run_id already set to 'sentinel-existing-id'; cannot overwrite"
        mock_session_service.update_run_status.side_effect = ValueError(sentinel_message)
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        broadcast_calls: list[tuple[str, Any]] = []
        original_broadcast = service._broadcaster.broadcast

        def spy_broadcast(rid: str, event: Any) -> None:
            broadcast_calls.append((rid, event))
            original_broadcast(rid, event)

        service._broadcaster.broadcast = spy_broadcast  # type: ignore[assignment]

        with _admitted_runtime_setup(), pytest.raises(ValueError, match="sentinel-existing-id") as exc_info:
            service._run_pipeline(run_id, _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

        # Discriminator #1: the propagating exception is bare ValueError, not the
        # narrow subclass — proves the catch did not match.
        assert not isinstance(exc_info.value, IllegalRunTransitionError)
        # Discriminator #2: no "cancelled" SSE was broadcast.  Old broad-catch
        # behaviour would emit one before silently returning.
        cancelled_events = [event for (_, event) in broadcast_calls if event.event_type == "cancelled"]
        assert cancelled_events == [], f"unexpected cancelled SSE — masking window re-opened: {cancelled_events}"

    @pytest.mark.asyncio
    async def test_shutdown_event_registered_before_blob_linkage(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """Race fix part 2: _shutdown_events registration must happen before
        blob linkage, so cancel() finds the event during the blob window."""
        session_id = uuid4()
        run_id = uuid4()
        blob_ref = str(uuid4())
        canonical_path = f"/tmp/data/blobs/{session_id}/{blob_ref}_input.csv"
        mock_session_service.create_run.return_value = _run_record_stub(id=run_id)

        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _ready_csv_blob_for_execution(
            blob_ref=blob_ref,
            session_id=session_id,
            storage_path=canonical_path,
        )

        async def tracking_link(*args: Any, **kwargs: Any) -> None:
            # At the time blob linkage runs, the event MUST already exist
            assert str(run_id) in service._shutdown_events, "RACE: _shutdown_events not registered before blob linkage"

        blob_service.link_blob_to_run.side_effect = tracking_link
        cast(Any, service)._blob_service = blob_service

        # Set up state record with a source containing a blob_ref.
        # Use a real dict so state_from_record → deep_thaw works correctly.
        # path must equal blob.storage_path to satisfy the Tier 1 read
        # guard for blob-backed sources.
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"blob_ref": blob_ref, "path": canonical_path},
            "on_validation_failure": "quarantine",
        }

        with patch.object(service, "_run_pipeline"):
            await _execute(service, session_id=session_id, execution_fixture=execution_fixture)

    @pytest.mark.asyncio
    async def test_shutdown_event_cleaned_up_on_blob_linkage_failure(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """If blob linkage raises after event registration, the event must
        be cleaned up to avoid leaking into _shutdown_events."""
        session_id = uuid4()
        run_id = uuid4()
        blob_ref = str(uuid4())
        canonical_path = f"/tmp/data/blobs/{session_id}/{blob_ref}_input.csv"
        mock_session_service.create_run.return_value = _run_record_stub(id=run_id)

        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _ready_csv_blob_for_execution(
            blob_ref=blob_ref,
            session_id=session_id,
            storage_path=canonical_path,
        )
        blob_service.link_blob_to_run.side_effect = RuntimeError("blob storage unavailable")
        cast(Any, service)._blob_service = blob_service

        # Use a real dict so state_from_record → deep_thaw works correctly.
        # path must equal blob.storage_path to satisfy the Tier 1 read
        # guard for blob-backed sources.
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"blob_ref": blob_ref, "path": canonical_path},
            "on_validation_failure": "quarantine",
        }

        with pytest.raises(RuntimeError, match="blob storage unavailable"):
            await _execute(service, session_id=session_id, execution_fixture=execution_fixture)

        assert str(run_id) not in service._shutdown_events


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestCompletionPathExternalCancellation:
    """Defence-in-depth: if the DB says 'cancelled' when _run_pipeline
    tries to write 'completed', exit gracefully — no 'failed' broadcast,
    no BaseException cascade, no re-raise."""

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_run_pipeline_exits_gracefully_when_completed_but_db_cancelled(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Pipeline completes, but orphan cleanup already set DB to 'cancelled'.
        _run_pipeline must detect this and return cleanly."""
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(
                run_id="landscape-run-123",
                status=RunStatus.COMPLETED_WITH_FAILURES,
                rows_processed=100,
                rows_succeeded=95,
                rows_failed=5,
            ),
        )

        run_id = str(uuid4())

        # First call: update_run_status("running") succeeds.
        # Second call: update_run_status("completed") raises ValueError
        # because the DB was externally set to "cancelled".
        call_count = 0

        async def status_side_effect(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise IllegalRunTransitionError("cancelled", "completed", frozenset())

        mock_session_service.update_run_status.side_effect = status_side_effect
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        # Should NOT raise — graceful exit
        service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

        # The "failed" path should NOT have been entered: check that
        # update_run_status was called exactly twice (running + completed),
        # NOT three times (running + completed + failed).
        assert mock_session_service.update_run_status.call_count == 2

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_cancelled_compensating_event_broadcast_on_external_cancel(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """When pipeline completes but DB says 'cancelled', exactly one
        terminal event must be broadcast: 'cancelled' (the DB is authoritative).
        No 'completed' event should be emitted — finalize-first ordering
        ensures the terminal broadcast reflects the actual DB state."""
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(
                run_id="landscape-run-789",
                status=RunStatus.COMPLETED_WITH_FAILURES,
                rows_processed=100,
                rows_succeeded=95,
                rows_failed=5,
            ),
        )

        run_id = str(uuid4())

        call_count = 0

        async def status_side_effect(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise IllegalRunTransitionError("cancelled", "completed", frozenset())

        mock_session_service.update_run_status.side_effect = status_side_effect
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        broadcast_calls: list[tuple[str, Any]] = []
        original_broadcast = service._broadcaster.broadcast

        def spy_broadcast(rid: str, event: Any) -> None:
            broadcast_calls.append((rid, event))
            original_broadcast(rid, event)

        service._broadcaster.broadcast = spy_broadcast  # type: ignore[assignment]

        service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

        event_types = [call[1].event_type for call in broadcast_calls]
        terminal_types = [et for et in event_types if et in ("completed", "failed", "cancelled")]
        assert terminal_types == ["cancelled"], f"Expected exactly one 'cancelled' terminal, got: {terminal_types}"

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_external_cancel_finalizes_output_blobs_as_error(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Cancelled runs must not leave output blobs finalized as ready."""
        from elspeth.web.blobs.protocol import BlobFinalizationResult

        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(
                run_id="landscape-run-blob-cancel",
                rows_processed=7,
                rows_succeeded=7,
            ),
        )

        blob_state = {"status": "pending"}
        blob_calls: list[bool] = []

        async def finalize_run_output_blobs(
            run_id: UUID, success: bool, *, session_operation_context: SessionOperationContext
        ) -> BlobFinalizationResult:
            del run_id, session_operation_context
            blob_calls.append(success)
            if blob_state["status"] == "pending":
                blob_state["status"] = "ready" if success else "error"
            return BlobFinalizationResult(finalized=[], errors=[])

        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = finalize_run_output_blobs
        cast(Any, service)._blob_service = blob_service

        async def status_side_effect(*args: Any, **kwargs: Any) -> None:
            if kwargs.get("status") == "completed":
                raise IllegalRunTransitionError("cancelled", "completed", frozenset())

        mock_session_service.update_run_status.side_effect = status_side_effect
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED),
        ):
            service._run_pipeline(str(uuid4()), "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

        assert blob_calls == [False]
        assert blob_state["status"] == "error"

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_completion_guard_reraises_for_non_cancelled_status(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """If update_run_status('completed') raises ValueError for a reason
        other than 'already cancelled', the error must propagate (offensive)."""
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(run_id="landscape-run-456"),
        )

        run_id = str(uuid4())

        call_count = 0

        async def status_side_effect(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise IllegalRunTransitionError("completed", "completed", frozenset())

        mock_session_service.update_run_status.side_effect = status_side_effect
        # DB says "completed" (not "cancelled") — this should re-raise
        mock_session_service.get_run.return_value = _run_record_stub(status="completed")

        with pytest.raises(ValueError, match="completed"):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_completion_guard_does_not_swallow_non_illegal_value_errors(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Tier-1 invariant for the completion-transition catch (originally
        ~915): a bare ValueError raised for a non-illegal-transition reason
        (run-not-found, landscape_run_id overwrite, completed-without-landscape,
        failed-without-error) must propagate verbatim and must NOT trigger the
        cancelled-race silent-swallow path.

        Discriminator (regression-resistant): under the old broad
        ``except ValueError`` the cancelled-race recovery would (a) consult
        get_run, (b) see status == "cancelled", (c) broadcast a "cancelled"
        SSE event, and (d) ``return`` silently — masking the Tier-1 breach as
        a normal cancellation.  The bare ValueError would never propagate.

        Under the narrowed catch the bare ValueError propagates and no
        "cancelled" SSE is emitted from the cancelled-race path.  (The
        downstream BaseException post-terminal recovery may consult get_run
        as part of separate audit-primacy machinery; that's expected and
        correct, so we assert on broadcast shape rather than get_run call
        count.)

        Without this test a future widening of
        ``except IllegalRunTransitionError`` back to ``except ValueError`` would
        silently re-open the masking window identified by silent-failure-hunter
        (H1) — and the existing recovery-path tests would still pass.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(run_id="landscape-run-sentinel"),
        )

        run_id = str(uuid4())
        call_count = 0
        sentinel_message = "landscape_run_id already set to 'sentinel-existing-id'; cannot overwrite"

        async def status_side_effect(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                # Bare ValueError simulating one of the four non-illegal-transition
                # invariant breaches in update_run_status.
                raise ValueError(sentinel_message)

        mock_session_service.update_run_status.side_effect = status_side_effect
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        broadcast_calls: list[tuple[str, Any]] = []
        original_broadcast = service._broadcaster.broadcast

        def spy_broadcast(rid: str, event: Any) -> None:
            broadcast_calls.append((rid, event))
            original_broadcast(rid, event)

        service._broadcaster.broadcast = spy_broadcast  # type: ignore[assignment]

        with pytest.raises(ValueError, match="sentinel-existing-id") as exc_info:
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

        # Discriminator #1: bare ValueError, not the narrow subclass.
        assert not isinstance(exc_info.value, IllegalRunTransitionError)
        # Discriminator #2: no "cancelled" SSE — old broad-catch behaviour
        # would emit one before silently returning.
        cancelled_events = [event for (_, event) in broadcast_calls if event.event_type == "cancelled"]
        assert cancelled_events == [], f"unexpected cancelled SSE — masking window re-opened: {cancelled_events}"


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestPostCompletionExceptionRecovery:
    """Defence-in-depth: when a BaseException fires AFTER ``update_run_status``
    has already committed a terminal state, the recovery must not attempt an
    illegal terminal→failed transition.

    elspeth-879f6de6bd: ``LEGAL_RUN_TRANSITIONS`` makes all five terminal
    statuses (``completed``, ``completed_with_failures``, ``failed``,
    ``empty``, ``cancelled``) outgoing-empty.  The pre-fix recovery at
    ``_run_pipeline``'s ``except BaseException`` handler attempted
    ``update_run_status(status="failed", ...)`` unconditionally, raising
    ``ValueError("Illegal run transition: 'completed' → 'failed'. Allowed: []")``
    and losing the original exception in ``__context__``.

    The fix consults ``get_run`` first; if the run is already terminal it
    skips both the status update and the misleading ``failed`` SSE broadcast
    (audit primacy: SSE must not contradict the audit row).
    """

    @staticmethod
    def _make_completed_orchestrator(status: RunStatus = RunStatus.COMPLETED) -> MagicMock:
        """Build an orchestrator stub whose ``run`` returns a terminal result.

        Counts are status-aware so the SSE-payload status/count cross-consistency
        validator (``CompletedData._check_status_consistency``) accepts the
        result.  COMPLETED_WITH_FAILURES requires both a success and a failure
        indicator; EMPTY requires zero rows; COMPLETED requires success only;
        FAILED tolerates any shape.
        """
        if status == RunStatus.COMPLETED_WITH_FAILURES:
            result = _orchestrator_result_stub(
                run_id="landscape-run-postcompletion",
                status=status,
                rows_processed=10,
                rows_succeeded=8,
                rows_failed=2,
            )
        elif status == RunStatus.EMPTY:
            result = _orchestrator_result_stub(
                run_id="landscape-run-postcompletion",
                status=status,
                rows_processed=0,
                rows_succeeded=0,
                rows_failed=0,
            )
        else:
            result = _orchestrator_result_stub(
                run_id="landscape-run-postcompletion",
                status=status,
                rows_processed=10,
                rows_succeeded=10,
                rows_failed=0,
            )
        return _orchestrator_stub(result)

    @staticmethod
    def _wrap_broadcaster_to_raise(
        service: ExecutionServiceImpl,
        *,
        on_event_type: str,
        exc: BaseException,
    ) -> list[tuple[str, Any]]:
        """Replace the broadcaster with a spy that records all calls and raises
        ``exc`` when the broadcast event_type matches ``on_event_type``.

        Returns the list that will be appended to as broadcasts occur.  Tests
        can assert on event_types after ``_run_pipeline`` returns.
        """
        original_broadcast = service._broadcaster.broadcast
        broadcast_calls: list[tuple[str, Any]] = []

        def crashing_broadcast(rid: str, event: Any) -> None:
            broadcast_calls.append((rid, event))
            if event.event_type == on_event_type:
                raise exc
            original_broadcast(rid, event)

        service._broadcaster.broadcast = crashing_broadcast  # type: ignore[assignment]
        return broadcast_calls

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_post_completion_broadcast_crash_skips_failed_status_update(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Run completes; success-path ``broadcast("completed", ...)`` raises
        ``RuntimeError``.  The recovery must NOT attempt a third
        ``update_run_status(status="failed", ...)`` because the audit row is
        already ``completed`` (illegal terminal→failed transition).
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=self._make_completed_orchestrator(RunStatus.COMPLETED).run.return_value,
        )

        # update_run_status is permissive (just records calls); the audit row
        # is conceptually "completed" after the second call.  The recovery's
        # third call (with status="failed") MUST NOT happen.
        mock_session_service.get_run.return_value = _run_record_stub(status="completed")

        broadcast_calls = self._wrap_broadcaster_to_raise(
            service,
            on_event_type="completed",
            exc=RuntimeError("simulated SSE crash"),
        )

        run_id = str(uuid4())
        with (
            patch("elspeth.web.execution.service.slog") as mock_slog,
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED),
            ),
            pytest.raises(RuntimeError, match="simulated SSE crash"),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

        # Exactly two status updates: "running" (line 650) and the terminal
        # "completed" (line 857).  No third "failed" call.
        statuses = [c.kwargs.get("status") for c in mock_session_service.update_run_status.call_args_list]
        assert statuses == ["running", "completed"], f"Expected [running, completed], got {statuses}"

        # Audit-primacy guarantees enforced at this site:
        #   1. No third update_run_status (asserted above).
        #   2. No "failed" SSE broadcast (asserted below) — would contradict
        #      the audit row's true terminal status.
        # Post-audit-exception observability is provided by three channels
        # (see service.py post-terminal-exception comment block):
        #   - The audit ``runs`` row (status="completed" — verified by the
        #     ``statuses`` assertion above by transitive proof).
        #   - ``run_pipeline_failed`` — emitted at the TOP of the except
        #     handler, before the terminality split, so it carries the
        #     diagnostic even here.  Pinned by
        #     ``test_diagnostic_log_fires_even_when_audit_row_is_already_terminal``.
        #   - ``_on_pipeline_done``'s safety-net slog
        #     (``pipeline_done_callback_exception``) — fires against the
        #     re-raised exc once the Future completes.  Tested separately
        #     in the _on_pipeline_done test class.
        # This test must NOT pin a slog at the post-terminal-exception
        # branch itself: per the ``logging-telemetry-policy`` skill §Logging
        # Policy the logger is not the correct surface for post-audit
        # operational signal.
        post_terminal_logs = [
            c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_terminal_exception_in_run_pipeline"
        ]
        assert post_terminal_logs == [], (
            "post_terminal_exception_in_run_pipeline slog has been removed "
            "(audit-primacy fix); the post-audit signal is captured by the "
            "audit row + _on_pipeline_done safety-net log."
        )

        # No "failed" SSE event must be emitted from the recovery — the run
        # actually completed; broadcasting "failed" would diverge from audit.
        recovery_failed_events = [event for (_, event) in broadcast_calls if event.event_type == "failed"]
        assert recovery_failed_events == [], f"Recovery must not broadcast 'failed' when run is terminal; got: {recovery_failed_events}"

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_post_completion_with_failures_also_skips_failed_status_update(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Regression catcher for the partial-tuple bug pattern.

        Several call sites in the codebase use a hardcoded
        ``("completed", "failed", "cancelled")`` tuple to detect terminality,
        which omits ``completed_with_failures`` and ``empty``.  The fix MUST
        use the full terminal set derived from ``LEGAL_RUN_TRANSITIONS`` /
        ``SESSION_TERMINAL_RUN_STATUS_VALUES``.  This test proves the guard
        fires for ``completed_with_failures`` — a state the partial tuple
        would miss.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=self._make_completed_orchestrator(RunStatus.COMPLETED_WITH_FAILURES).run.return_value,
        )

        mock_session_service.get_run.return_value = _run_record_stub(status="completed_with_failures")

        self._wrap_broadcaster_to_raise(
            service,
            on_event_type="completed",
            exc=RuntimeError("simulated SSE crash"),
        )

        run_id = str(uuid4())
        with (
            patch("elspeth.web.execution.service.slog") as mock_slog,
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED_WITH_FAILURES),
            ),
            pytest.raises(RuntimeError),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

        statuses = [c.kwargs.get("status") for c in mock_session_service.update_run_status.call_args_list]
        assert statuses == ["running", "completed_with_failures"], f"Expected [running, completed_with_failures], got {statuses}"

        # Audit-primacy fix: the post-audit slog has been removed from
        # this site.  See test_post_completion_broadcast_crash_skips_failed_status_update
        # for the rationale; the assertion here pins the same invariant for
        # the COMPLETED_WITH_FAILURES branch (the non-completed branch the
        # partial-tuple bug pattern would have missed).
        post_terminal_logs = [
            c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_terminal_exception_in_run_pipeline"
        ]
        assert post_terminal_logs == [], "post_terminal_exception_in_run_pipeline slog has been removed (audit-primacy fix)."

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_post_completion_get_run_probe_failure_falls_through(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """When ``get_run`` itself raises during the recovery probe, fall
        through to the existing best-effort recovery (attempt
        ``update_run_status("failed", ...)``).

        Documents a **known gap**: this test exercises the fall-through with
        a permissive ``update_run_status`` mock that does NOT enforce
        ``LEGAL_RUN_TRANSITIONS``.  In production, if the run is genuinely
        terminal AND the probe fails AND ``update_run_status`` therefore
        raises ``ValueError``, the original exception is still lost — same
        as today's behaviour.  Closing that gap requires a larger design
        change (fail-closed on probe failure, or post-hoc reconcile of the
        ValueError) and is out of scope for elspeth-879f6de6bd.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=self._make_completed_orchestrator(RunStatus.COMPLETED).run.return_value,
        )

        # Probe raises a generic SQLAlchemy-family-ish error.  Use the actual
        # SQLAlchemyError to match the recovery's narrow catch.
        mock_session_service.get_run.side_effect = SQLAlchemyError("simulated DB hiccup")

        self._wrap_broadcaster_to_raise(
            service,
            on_event_type="completed",
            exc=RuntimeError("simulated SSE crash"),
        )

        run_id = str(uuid4())
        with (
            patch("elspeth.web.execution.service.slog") as mock_slog,
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED),
            ),
            pytest.raises(RuntimeError, match="simulated SSE crash"),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

        # The probe failure must surface as a structured log so it's observable.
        probe_failed_logs = [c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_exception_run_state_probe_failed"]
        assert len(probe_failed_logs) == 1, f"Expected one post_exception_run_state_probe_failed log, got {len(probe_failed_logs)}"

        # Fall-through: recovery DID attempt the failed update.  Three calls
        # total: running, completed, failed.
        statuses = [c.kwargs.get("status") for c in mock_session_service.update_run_status.call_args_list]
        assert statuses == ["running", "completed", "failed"], (
            f"Probe-failure path must fall through to update_run_status('failed', ...); got {statuses}"
        )

        # The post_terminal_exception_in_run_pipeline log must NOT have
        # been emitted (we couldn't determine the run was terminal).
        post_terminal_logs = [
            c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_terminal_exception_in_run_pipeline"
        ]
        assert post_terminal_logs == []

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_post_completion_probe_failure_with_legal_transitions_preserves_original_exception(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Lock the residual data-loss path into the test surface (pr-test-analyzer IG-2).

        Sister to ``test_post_completion_get_run_probe_failure_falls_through``,
        but wires real ``LEGAL_RUN_TRANSITIONS`` semantics into the
        ``update_run_status`` mock so the gap acknowledged in the production
        comment block on the post-exception probe (``run_already_terminal``
        false-on-probe-failure) is actually exercised by the test surface —
        not just documented in prose.

        Scenario:
          1. Pipeline runs to completion → ``update_run_status('completed', ...)``
             commits the audit row.
          2. Success-path ``broadcast('completed', ...)`` raises ``RuntimeError``.
          3. Recovery probes ``get_run`` → SQLAlchemyError (probe failure).
          4. ``run_already_terminal`` stays ``False`` (probe couldn't determine).
          5. Recovery falls through to ``update_run_status('failed', ...)`` against
             a row whose true status is ``completed``.
          6. Real ``LEGAL_RUN_TRANSITIONS`` mock raises
             ``IllegalRunTransitionError`` (matching production
             ``SessionService.update_run_status``'s transition guard).
          7. Production recovery's narrow ``except (SQLAlchemyError, OSError)``
             does NOT catch the ``ValueError`` subclass; it propagates and
             shadows the original ``RuntimeError``.

        Correct behaviour: the ``RuntimeError("simulated SSE crash")`` is the
        operationally-relevant signal and MUST be the surfacing exception —
        the ``IllegalRunTransitionError`` is an artefact of a recovery attempt
        that should never have been made against an already-terminal row.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=self._make_completed_orchestrator(RunStatus.COMPLETED).run.return_value,
        )

        # Stateful mock that mirrors SessionService.update_run_status's
        # transition validation (sessions/service.py:665-667). Driven by the
        # real ``LEGAL_RUN_TRANSITIONS`` table so the test stays in lockstep
        # with the production validator without duplicating its terminal-set
        # closure.
        audit_row_status: dict[str, SessionRunStatus] = {"current": "pending"}

        def _legal_transitions_update(_run_id: Any, *, status: str, **__: Any) -> None:
            current = audit_row_status["current"]
            allowed = LEGAL_RUN_TRANSITIONS[current]
            if status not in allowed:
                raise IllegalRunTransitionError(current, status, allowed)
            audit_row_status["current"] = cast(SessionRunStatus, status)

        mock_session_service.update_run_status.side_effect = _legal_transitions_update

        # Probe fails — same as the sibling test.  Drives the recovery into
        # the fall-through branch where the residual data-loss occurs.
        mock_session_service.get_run.side_effect = SQLAlchemyError("simulated DB hiccup")

        self._wrap_broadcaster_to_raise(
            service,
            on_event_type="completed",
            exc=RuntimeError("simulated SSE crash"),
        )

        run_id = str(uuid4())
        # Closes the elspeth-879f6de6bd gap: the IllegalRunTransitionError
        # raised by the fall-through update_run_status('failed', ...) against
        # an already-terminal row is now caught narrowly in
        # ``ExecutionServiceImpl._run_pipeline``'s BaseException recovery
        # (branch 3 — see the prelude comment block at that site).  The
        # narrow catch promotes the run into the audit-primacy stance via
        # ``irte.current_status`` as in-band proof of terminality, so the
        # original SSE-crash RuntimeError surfaces and the ``failed`` SSE
        # broadcast is suppressed.
        with (
            patch("elspeth.web.execution.service.slog") as mock_slog,
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED),
            ),
            pytest.raises(RuntimeError, match="simulated SSE crash"),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

        # Audit-primacy completion: branch 3 must NOT have broadcast a
        # ``failed`` SSE event (the audit row is in a real terminal status
        # — broadcasting ``failed`` would contradict it).  The branch-3 log
        # ``post_exception_recovery_aborted_run_terminal`` is the
        # SRE-discoverable resolution of the probe-failure ambiguity.
        recovery_aborted_logs = [
            c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_exception_recovery_aborted_run_terminal"
        ]
        assert len(recovery_aborted_logs) == 1, f"branch-3 recovery slog must fire exactly once; got {len(recovery_aborted_logs)}"
        # The probe-failure slog still fires upstream — the IRTE catch is
        # the *resolution*, not a replacement for the probe-failure record.
        probe_failed_logs = [c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_exception_run_state_probe_failed"]
        assert len(probe_failed_logs) == 1, f"probe-failure slog must still fire upstream of the IRTE catch; got {len(probe_failed_logs)}"
        # Three update_run_status attempts: running, completed, failed (the
        # third one raises IRTE which is caught).  The attempt is recorded
        # on the mock even though the side_effect raised.
        statuses = [c.kwargs.get("status") for c in mock_session_service.update_run_status.call_args_list]
        assert statuses == ["running", "completed", "failed"], (
            f"branch-3 recovery path must attempt the failed update (caught by IRTE); got {statuses}"
        )

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_post_completion_get_run_probe_value_error_propagates(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """ValueError from the post-exception ``get_run`` probe must propagate,
        not be absorbed.

        Audit-primacy contract (docs/guides/data-trust-and-error-handling.md
        §The Three-Tier Trust Model): ``get_run`` can raise
        ``ValueError`` only via Tier 1 audit-data corruption — "Run not found"
        (the row vanished mid-run), malformed UUID columns, or non-UTC
        ``started_at`` / ``finished_at``.  All three are Tier 1 invariant
        violations that MUST crash immediately.

        Pre-fix behaviour absorbed ValueError in the probe catch alongside
        ``SQLAlchemyError``/``OSError`` and fell through to the best-effort
        ``update_run_status`` recovery, which would re-encounter the same
        corruption.  The narrow catch (mirroring the sibling pattern at the
        ``update_run_status`` recovery — commits b8ba2214/127417cb) keeps
        Tier 1 corruption visible at the call site.

        This test pins that contract so future re-widening of the catch is
        caught at review time.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=self._make_completed_orchestrator(RunStatus.COMPLETED).run.return_value,
        )

        # Probe raises ValueError — the canonical Tier 1 signal from get_run
        # ("Run not found", malformed UUID, or non-UTC datetime).  All three
        # share this exception class and must surface, not be absorbed.
        mock_session_service.get_run.side_effect = ValueError("Run not found: simulated Tier 1 corruption")

        self._wrap_broadcaster_to_raise(
            service,
            on_event_type="completed",
            exc=RuntimeError("simulated SSE crash"),
        )

        run_id = str(uuid4())
        # The visible exception MUST be the probe's ValueError (Tier 1
        # corruption surfaces as itself).  The original RuntimeError is
        # preserved on ``__context__`` by Python's normal exception chaining.
        with (
            patch("elspeth.web.execution.service.slog") as mock_slog,
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED),
            ),
            pytest.raises(ValueError, match="Run not found") as exc_info,
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

        # Exception chain pins the original cause — the probe ValueError
        # is raised while handling the RuntimeError, so __context__ MUST
        # carry the original SSE crash.  Without this, debugging the
        # post-completion exception gets harder, not easier.
        assert isinstance(exc_info.value.__context__, RuntimeError)
        assert "simulated SSE crash" in str(exc_info.value.__context__)

        # Probe-failure slog MUST NOT fire — the ValueError exits the try
        # block via propagation, not through the narrow except.  If a future
        # change re-widens the catch to include ValueError, this assertion
        # will fail and surface the regression.
        probe_failed_logs = [c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_exception_run_state_probe_failed"]
        assert probe_failed_logs == [], (
            "ValueError from get_run must propagate (Tier 1 corruption); "
            f"probe-failed slog should not fire, got {len(probe_failed_logs)} call(s)."
        )

        # The fall-through update_run_status("failed", ...) MUST NOT have
        # been called — control left the BaseException handler before the
        # recovery branch.  Only the "running" and "completed" updates from
        # the happy path are present.
        statuses = [c.kwargs.get("status") for c in mock_session_service.update_run_status.call_args_list]
        assert statuses == ["running", "completed"], f"ValueError-propagation path must skip the recovery update_run_status; got {statuses}"

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_running_state_exception_still_records_failed(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Negative control: when the orchestrator raises BEFORE the terminal
        transition (run is still ``running``), the existing happy-path
        recovery must still land — the new guard MUST NOT short-circuit
        non-terminal states.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
        )
        mock_orch = mock_orch_cls.return_value
        mock_orch.run.side_effect = RuntimeError("orchestrator blew up mid-run")

        # DB still holds "running" because we never reached the terminal
        # transition.
        mock_session_service.get_run.return_value = _run_record_stub(status="running")

        run_id = str(uuid4())
        with patch("elspeth.web.execution.service.slog") as mock_slog, pytest.raises(RuntimeError, match="orchestrator blew up"):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

        statuses = [c.kwargs.get("status") for c in mock_session_service.update_run_status.call_args_list]
        assert statuses == ["running", "failed"], f"Non-terminal path must still record failure; got {statuses}"

        post_terminal_logs = [
            c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_terminal_exception_in_run_pipeline"
        ]
        assert post_terminal_logs == [], "Guard must NOT fire for non-terminal current status"


class TestGetLiveRunIds:
    """Tests for get_live_run_ids — used by periodic orphan cleanup."""

    def test_returns_empty_when_no_active_runs(
        self,
        service: ExecutionServiceImpl,
    ) -> None:
        """No runs registered → empty frozenset."""
        assert service.get_live_run_ids() == frozenset()

    def test_returns_registered_run_ids(
        self,
        service: ExecutionServiceImpl,
    ) -> None:
        """Manually registered shutdown events appear in live run IDs."""
        event = threading.Event()
        with service._shutdown_events_lock:
            service._shutdown_events["run-abc"] = event
            service._shutdown_events["run-def"] = event
        assert service.get_live_run_ids() == frozenset({"run-abc", "run-def"})

    def test_includes_signalled_events_until_worker_exits(
        self,
        service: ExecutionServiceImpl,
    ) -> None:
        """Signalled runs stay live until _run_pipeline() removes them.

        A set shutdown event means cancellation was requested, not that the
        worker thread has finished its GracefulShutdownError unwinding.
        Periodic orphan cleanup must keep excluding the run until the
        worker's finally block removes the registry entry.
        """
        live_event = threading.Event()
        signalled_event = threading.Event()
        signalled_event.set()
        with service._shutdown_events_lock:
            service._shutdown_events["run-live"] = live_event
            service._shutdown_events["run-signalled"] = signalled_event
        assert service.get_live_run_ids() == frozenset({"run-live", "run-signalled"})

    def test_returns_snapshot_not_live_reference(
        self,
        service: ExecutionServiceImpl,
    ) -> None:
        """Returned frozenset is a snapshot — later changes don't affect it."""
        event = threading.Event()
        with service._shutdown_events_lock:
            service._shutdown_events["run-1"] = event
        snapshot = service.get_live_run_ids()
        with service._shutdown_events_lock:
            service._shutdown_events["run-2"] = event
        # Snapshot should not include run-2
        assert snapshot == frozenset({"run-1"})


class TestOneActiveRun:
    @pytest.mark.asyncio
    async def test_second_execute_raises_run_already_active(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """B6: Only one pending/running run per session."""
        session_id = uuid4()
        mock_session_service.get_active_run.return_value = _run_record_stub(status="running")

        with pytest.raises(RunAlreadyActiveError):
            await _execute(service, session_id=session_id, execution_fixture=execution_fixture)

    @pytest.mark.asyncio
    async def test_execute_after_completed_run_succeeds(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """After a run completes, a new one can start."""
        mock_session_service.get_active_run.return_value = None
        with patch.object(service, "_run_pipeline"):
            run_id = await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)
        assert isinstance(run_id, UUID)


class TestEventBusBridge:
    """Verify that ProgressEvent from the Orchestrator's EventBus
    is translated to RunEvent and broadcast via the ProgressBroadcaster."""

    @pytest.mark.parametrize("error_type", [OSError, SQLAlchemyError])
    def test_failed_event_persistence_never_broadcasts(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        error_type: type[Exception],
    ) -> None:
        from elspeth.contracts.cli import ProgressEvent

        failure = error_type("event storage unavailable")
        mock_session_service.append_run_event.side_effect = failure
        progress = ProgressEvent(
            rows_processed=1,
            rows_succeeded=1,
            rows_failed=0,
            rows_quarantined=0,
            rows_routed_success=0,
            rows_routed_failure=0,
            elapsed_seconds=1.0,
        )
        run_id = str(uuid4())
        event = service._to_run_event(run_id, progress)
        with patch.object(service._broadcaster, "broadcast") as broadcast, pytest.raises(error_type) as caught:
            service._persist_and_broadcast_run_event(run_id, event, session_operation_lease=_execute_lease())
        assert caught.value is failure
        broadcast.assert_not_called()

    def test_progress_event_translated_to_run_event(self, service: ExecutionServiceImpl) -> None:
        """_to_run_event maps ProgressEvent fields to RunEvent.data.

        elspeth-5069612f3c — assert the routed split (MOVE / DIVERT) is
        plumbed verbatim through the translator. Pre-fix the engine emitter
        folded ``rows_routed_success`` into ``rows_succeeded`` and dropped
        ``rows_routed_failure`` entirely; the wire payload then lacked the
        fields. This test guards against regression to that shape.
        """
        from elspeth.contracts.cli import ProgressEvent
        from elspeth.web.execution.schemas import ProgressData

        progress = ProgressEvent(
            rows_processed=100,
            rows_succeeded=92,
            rows_failed=5,
            rows_quarantined=3,
            rows_routed_success=7,
            rows_routed_failure=2,
            elapsed_seconds=10.5,
        )
        run_id = "run-123"
        run_event = service._to_run_event(run_id, progress)

        assert run_event.event_type == "progress"
        assert isinstance(run_event.data, ProgressData)
        # S-8: assert every counter passes through with its real producer
        # value.  Non-zero values in every slot guard against a future
        # producer that hardcodes any single counter to 0 (which Pydantic
        # cannot detect — making the upstream ProgressEvent require all six
        # is the structural defense, this assertion is the test surface).
        assert run_event.data.source_rows_processed == 100
        assert run_event.data.tokens_succeeded == 92
        assert run_event.data.tokens_failed == 5
        assert run_event.data.tokens_quarantined == 3
        assert run_event.data.tokens_routed_success == 7
        assert run_event.data.tokens_routed_failure == 2
        assert run_event.run_id == "run-123"

    def test_progress_event_persisted_even_without_subscriber(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        from elspeth.contracts.cli import ProgressEvent

        service._broadcast_progress_event(
            "4a8ebbb1-dc97-4875-8b3d-1890ec7ead87",
            ProgressEvent(
                rows_processed=100,
                rows_succeeded=92,
                rows_failed=5,
                rows_quarantined=3,
                rows_routed_success=7,
                rows_routed_failure=2,
                elapsed_seconds=10.5,
            ),
            session_operation_lease=_execute_lease(),
        )

        mock_session_service.append_run_event.assert_awaited_once()
        call = mock_session_service.append_run_event.await_args
        assert call.kwargs["run_id"] == UUID("4a8ebbb1-dc97-4875-8b3d-1890ec7ead87")
        assert call.kwargs["event_type"] == "progress"
        assert call.kwargs["data"]["source_rows_processed"] == 100

    def test_progress_broadcast_closed_loop_records_drop_telemetry(self, service: ExecutionServiceImpl) -> None:
        """Loop-closed progress drops are operational telemetry, not slog-only."""
        from elspeth.contracts.cli import ProgressEvent

        loop = asyncio.new_event_loop()
        try:
            broadcaster = ProgressBroadcaster(loop)
            run_id = "4a8ebbb1-dc97-4875-8b3d-1890ec7ead87"
            broadcaster.subscribe(run_id)
            loop.close()
            service._broadcaster = broadcaster

            service._broadcast_progress_event(
                run_id,
                ProgressEvent(
                    rows_processed=100,
                    rows_succeeded=92,
                    rows_failed=5,
                    rows_quarantined=3,
                    rows_routed_success=7,
                    rows_routed_failure=2,
                    elapsed_seconds=10.5,
                ),
                session_operation_lease=_execute_lease(),
            )
        finally:
            if not loop.is_closed():
                loop.close()

        assert observed_value(service._telemetry.progress_broadcast_dropped_total) == 1


class TestB8AsyncBridging:
    """B8/C1 fix: _call_async() bridges sync thread to async event loop.

    These tests need the REAL _call_async (not the test fixture's mock),
    so they construct a fresh service with a mock loop whose
    run_coroutine_threadsafe is controlled.
    """

    def test_call_async_returns_coroutine_result(
        self,
        broadcaster: ProgressBroadcaster,
        mock_settings: MagicMock,
        mock_session_service: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """_call_async() schedules coroutine and returns its result."""
        mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
        svc = execution_fixture.bind(
            ExecutionServiceImpl.for_trained_operator(
                loop=mock_loop,
                broadcaster=broadcaster,
                settings=mock_settings,
                session_service=mock_session_service,
                yaml_generator=_YamlGeneratorStub(),
                telemetry=build_sessions_telemetry(),
                execution_lease_release_registry=execution_fixture.registry(execution_fixture.loop),
            )
        )
        mock_future = MagicMock(spec=Future)
        mock_future.result.return_value = "test_result"

        async def dummy_coro() -> str:
            return "test_result"

        coro = dummy_coro()
        with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future):
            result = svc._call_async(coro)
        coro.close()
        assert result == "test_result"
        mock_future.result.assert_called_once_with(timeout=30.0)

    def test_call_async_propagates_coroutine_exception(
        self,
        broadcaster: ProgressBroadcaster,
        mock_settings: MagicMock,
        mock_session_service: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """If the coroutine raises, _call_async re-raises from future.result()."""
        mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
        svc = execution_fixture.bind(
            ExecutionServiceImpl.for_trained_operator(
                loop=mock_loop,
                broadcaster=broadcaster,
                settings=mock_settings,
                session_service=mock_session_service,
                yaml_generator=_YamlGeneratorStub(),
                telemetry=build_sessions_telemetry(),
                execution_lease_release_registry=execution_fixture.registry(execution_fixture.loop),
            )
        )
        mock_future = MagicMock(spec=Future)
        mock_future.result.side_effect = ValueError("db error")

        async def failing_coro() -> None:
            raise ValueError("db error")

        coro = failing_coro()
        with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future), pytest.raises(ValueError, match="db error"):
            svc._call_async(coro)
        coro.close()

    def test_call_async_raises_timeout_error(
        self,
        broadcaster: ProgressBroadcaster,
        mock_settings: MagicMock,
        mock_session_service: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """R6 fix: _call_async raises TimeoutError after 30s, preventing deadlock."""
        mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
        svc = execution_fixture.bind(
            ExecutionServiceImpl.for_trained_operator(
                loop=mock_loop,
                broadcaster=broadcaster,
                settings=mock_settings,
                session_service=mock_session_service,
                yaml_generator=_YamlGeneratorStub(),
                telemetry=build_sessions_telemetry(),
                execution_lease_release_registry=execution_fixture.registry(execution_fixture.loop),
            )
        )
        mock_future = MagicMock(spec=Future)
        mock_future.result.side_effect = concurrent.futures.TimeoutError()

        async def hanging_coro() -> None:
            pass

        coro = hanging_coro()
        with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future), pytest.raises(concurrent.futures.TimeoutError):
            svc._call_async(coro)
        coro.close()


class TestAsyncShutdown:
    """Shutdown must keep the event loop available for worker cleanup."""

    @pytest.mark.asyncio
    async def test_shutdown_keeps_loop_available_for_worker_cleanup(
        self, mock_settings: MagicMock, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """Regression: draining the executor must not strand worker _call_async calls."""
        loop = asyncio.get_running_loop()
        svc = execution_fixture.bind(
            ExecutionServiceImpl.for_trained_operator(
                loop=loop,
                broadcaster=ProgressBroadcaster(loop),
                settings=mock_settings,
                session_service=mock_session_service,
                yaml_generator=_YamlGeneratorStub(),
                telemetry=build_sessions_telemetry(),
                execution_lease_release_registry=execution_fixture.registry(execution_fixture.loop),
            )
        )

        session_id = uuid4()
        observed = execution_fixture.observe_authority(session_id)
        lease = await execution_fixture.acquire(
            svc.execution_lease_release_registry,
            observed.authority,
            session_id=session_id,
            owner_instance_id="late-loop-cleanup-worker",
            lease_seconds=30,
        )
        assert lease.execution_obligation is not None
        assert lease.execution_obligation.context is lease.context
        assert not lease.closed

        run_id = str(uuid4())
        shutdown_event = threading.Event()
        with svc._shutdown_events_lock:
            svc._shutdown_events[run_id] = shutdown_event

        cleanup_applied = asyncio.Event()

        async def update_run_status(*args: Any, **kwargs: Any) -> None:
            cleanup_applied.set()

        mock_session_service.update_run_status.side_effect = update_run_status

        def short_call_async(coro: Coroutine[Any, Any, Any]) -> Any:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                return future.result(timeout=1.0)
            except concurrent.futures.TimeoutError:
                future.cancel()
                raise

        cast(Any, svc)._call_async = short_call_async

        worker_done = threading.Event()
        worker_errors: list[str] = []
        worker_originals: list[BaseException] = []

        async def cleanup_on_loop() -> None:
            primary: BaseException | None = None
            try:
                await mock_session_service.update_run_status(
                    uuid4(),
                    status="cancelled",
                    session_operation_context=lease.context,
                )
            except BaseException as original:
                primary = original
                raise
            finally:
                await close_execute_lease_before_transfer(lease, primary=primary)

        def worker() -> None:
            shutdown_event.wait()
            try:
                svc._call_async(cleanup_on_loop())
            except BaseException as exc:
                worker_errors.append(type(exc).__name__)
                worker_originals.append(exc)
            finally:
                worker_done.set()

        submission_original: BaseException | None = None
        try:
            svc._executor.submit(worker)
        except BaseException as original:
            submission_original = original
            svc.execution_lease_release_registry.record_failure(original)

        await _join_loop_cleanup_worker(svc, execution_fixture, lease, worker_originals=worker_originals, primary=submission_original)

        assert worker_done.is_set()
        assert worker_errors == []
        assert cleanup_applied.is_set()


class TestRunningStatusFailure:
    """W15: What happens when the initial status update to 'running' fails."""

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_running_status_failure_marks_run_failed(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """If update_run_status('running') fails, the except BaseException
        block attempts to set 'failed'. Run stays 'pending' if both fail."""
        # Fail the running-status write, after the independent admission call.
        mock_session_service.update_run_status.side_effect = [ConnectionError("DB connection lost"), None]
        original_call_async = service._call_async
        call_count = 0

        def failing_call_async(coro: Coroutine[Any, Any, Any]) -> Any:
            nonlocal call_count
            call_count += 1
            return original_call_async(coro)

        cast(Any, service)._call_async = failing_call_async

        with _admitted_runtime_setup(), pytest.raises(ConnectionError):
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

        # The except block tried to set "failed" after the running write failed.
        assert call_count >= 2
        assert [call.kwargs["status"] for call in mock_session_service.update_run_status.call_args_list] == ["running", "failed"]


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestTerminalOrderingInvariant:
    """Bug: elspeth-25df1be367 — run termination is published before output
    blob finalization. A late finalize failure triggers a second terminal event
    via except BaseException.

    Per docs/contracts/system-operations.md §Complete Token State Diagram:
    "Every token reaches exactly one terminal state — no silent drops."
    """

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_single_terminal_when_finalize_raises_blob_not_found(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        mock_settings: MagicMock,
        mock_session_service: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """When finalize_run_output_blobs raises BlobNotFoundError after
        a successful orchestrator.run(), exactly one terminal event must
        be broadcast — not completed-then-failed."""
        from elspeth.web.blobs.protocol import BlobNotFoundError

        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(
                run_id="landscape-run-1",
                status=RunStatus.COMPLETED_WITH_FAILURES,
                rows_processed=10,
                rows_succeeded=9,
                rows_failed=1,
            ),
        )

        mock_broadcaster = MagicMock(spec=ProgressBroadcaster)
        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = BlobNotFoundError("blob-vanished")

        svc = execution_fixture.bind(
            ExecutionServiceImpl.for_trained_operator(
                loop=MagicMock(spec=asyncio.AbstractEventLoop),
                broadcaster=mock_broadcaster,
                settings=mock_settings,
                session_service=mock_session_service,
                yaml_generator=_YamlGeneratorStub(),
                telemetry=build_sessions_telemetry(),
                blob_service=blob_service,
                execution_lease_release_registry=execution_fixture.registry(execution_fixture.loop),
            )
        )
        _real_loop = asyncio.new_event_loop()
        try:
            cast(Any, svc)._call_async = lambda coro: _real_loop.run_until_complete(coro)

            with contextlib.suppress(Exception):
                svc._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

            terminals = _collect_terminal_types(mock_broadcaster)
            assert len(terminals) == 1, (
                f"Exactly one terminal event expected, got {terminals}. A finalize failure must not trigger a second terminal broadcast."
            )
        finally:
            _real_loop.close()

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_externally_cancelled_run_emits_single_cancelled_terminal(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        mock_settings: MagicMock,
        mock_session_service: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """When a run completes but the DB status is already 'cancelled'
        (external orphan cleanup raced), exactly one terminal event must
        be emitted — not completed-then-cancelled."""
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(
                run_id="landscape-run-2",
                rows_processed=5,
                rows_succeeded=5,
            ),
        )

        mock_broadcaster = MagicMock(spec=ProgressBroadcaster)

        # Simulate external cancel: update_run_status("running") succeeds,
        # then update_run_status("completed") raises ValueError because
        # orphan cleanup already set the DB status to "cancelled".
        async def _selective_update(run_id, *, status="", **kwargs):
            if status == "completed":
                raise IllegalRunTransitionError("cancelled", "completed", frozenset())
            return None

        mock_session_service.update_run_status.side_effect = _selective_update
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        svc = execution_fixture.bind(
            ExecutionServiceImpl.for_trained_operator(
                loop=MagicMock(spec=asyncio.AbstractEventLoop),
                broadcaster=mock_broadcaster,
                settings=mock_settings,
                session_service=mock_session_service,
                yaml_generator=_YamlGeneratorStub(),
                telemetry=build_sessions_telemetry(),
                execution_lease_release_registry=execution_fixture.registry(execution_fixture.loop),
            )
        )
        _real_loop = asyncio.new_event_loop()
        try:
            cast(Any, svc)._call_async = lambda coro: _real_loop.run_until_complete(coro)

            svc._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

            terminals = _collect_terminal_types(mock_broadcaster)
            assert len(terminals) == 1, (
                f"Exactly one terminal event expected, got {terminals}. "
                "External cancellation must produce a single 'cancelled', "
                "not 'completed' followed by 'cancelled'."
            )
            assert terminals[0] == "cancelled", f"Terminal should be 'cancelled' (DB is authoritative), got '{terminals[0]}'."
        finally:
            _real_loop.close()


class TestSessionLockCleanup:
    """Tests that cleanup_session_lock removes per-session asyncio.Lock entries."""

    def test_cleanup_removes_existing_lock(self, service: ExecutionServiceImpl) -> None:
        """cleanup_session_lock removes the lock for a known session."""
        session_id = str(uuid4())
        service._session_locks[session_id] = asyncio.Lock()
        service.cleanup_session_lock(session_id)
        assert session_id not in service._session_locks

    def test_cleanup_noop_for_unknown_session(self, service: ExecutionServiceImpl) -> None:
        """cleanup_session_lock is a no-op for an unknown session."""
        service.cleanup_session_lock("nonexistent")  # Should not raise

    def test_cleanup_does_not_affect_other_sessions(self, service: ExecutionServiceImpl) -> None:
        """Cleaning up one session leaves other sessions' locks intact."""
        session_a = str(uuid4())
        session_b = str(uuid4())
        service._session_locks[session_a] = asyncio.Lock()
        service._session_locks[session_b] = asyncio.Lock()
        service.cleanup_session_lock(session_a)
        assert session_a not in service._session_locks
        assert session_b in service._session_locks


class TestLossWatcherPollResilience:
    """The execution loss watcher must survive transient session-DB errors.

    ``_signal_shutdown_on_operation_loss`` is the only carrier of a durable
    ``cancel_requested_at`` (written by whichever replica received the cancel)
    to the worker that owns the run. A single exception from its poll used to
    end the task for good, and ``_on_pipeline_done`` discarded that exception,
    so a later cross-replica cancel was silently ignored.
    """

    def _watch(
        self,
        service: ExecutionServiceImpl,
        real_loop: asyncio.AbstractEventLoop,
        shutdown_event: threading.Event,
        run_id: UUID,
    ) -> None:
        # The timeout is a hang guard for a broken watcher, not a timing claim.
        real_loop.run_until_complete(
            asyncio.wait_for(
                service._signal_shutdown_on_operation_loss(_execute_lease(), shutdown_event, run_id=run_id),
                timeout=60,
            )
        )

    @pytest.mark.parametrize(
        ("transient_error", "exc_class"),
        [
            pytest.param(
                OperationalError("SELECT runs", {}, Exception("database is locked")),
                "OperationalError",
                id="operational",
            ),
            # The PostgreSQL session engine is a bounded QueuePool
            # (postgres_engine_kwargs); exhaustion raises this pool checkout
            # timeout, which is not an OperationalError.
            pytest.param(
                SQLAlchemyTimeoutError("QueuePool limit of size 5 overflow 5 reached, connection timed out"),
                "TimeoutError",
                id="pool-checkout-timeout",
            ),
            pytest.param(
                InterfaceError("SELECT runs", {}, Exception("connection already closed")),
                "InterfaceError",
                id="interface",
            ),
            # SQLAlchemy's disconnect detection flags the wrapped DBAPI error
            # whatever class it maps to.
            pytest.param(
                DBAPIError("SELECT runs", {}, Exception("terminating connection"), connection_invalidated=True),
                "DBAPIError",
                id="connection-invalidated",
            ),
            # Raised by the pool itself on a dead connection at checkout; not a
            # DBAPIError, so only the transient tuple can admit it.
            pytest.param(
                DisconnectionError("connection dead at checkout"),
                "DisconnectionError",
                id="disconnection",
            ),
        ],
    )
    def test_transient_poll_error_does_not_lose_a_later_remote_cancel(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        real_loop: asyncio.AbstractEventLoop,
        transient_error: SQLAlchemyError,
        exc_class: str,
    ) -> None:
        run_id = uuid4()
        shutdown_event = threading.Event()
        polls: list[UUID] = []

        async def get_run(requested: UUID) -> SimpleNamespace:
            polls.append(requested)
            assert not shutdown_event.is_set(), "a poll failure must not cancel the run"
            if len(polls) == 1:
                raise transient_error
            return _run_record_stub(id=run_id, status="running", cancel_requested_at=datetime.now(UTC))

        mock_session_service.get_run.side_effect = get_run

        with patch("elspeth.web.execution.service.slog") as mock_slog:
            self._watch(service, real_loop, shutdown_event, run_id)

        assert shutdown_event.is_set(), "the durable cancel after a transient poll error must still reach the worker"
        assert polls == [run_id, run_id]
        mock_slog.warning.assert_called_once()
        event_name, *_ = mock_slog.warning.call_args.args
        assert event_name == "execution_loss_watcher_poll_retrying"
        assert mock_slog.warning.call_args.kwargs == {
            "run_id": str(run_id),
            "exc_class": exc_class,
            "consecutive_failures": 1,
        }
        mock_slog.error.assert_not_called()

    def test_repeated_poll_errors_escalate_to_error_logs_and_never_cancel_the_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        real_loop: asyncio.AbstractEventLoop,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("elspeth.web.execution.service._LOSS_WATCHER_POLL_SECONDS", 0.001)
        monkeypatch.setattr("elspeth.web.execution.service._LOSS_WATCHER_MAX_BACKOFF_SECONDS", 0.004)
        run_id = uuid4()
        shutdown_event = threading.Event()
        failures = _LOSS_WATCHER_ESCALATE_AFTER_FAILURES + 2
        polls: list[UUID] = []

        async def get_run(requested: UUID) -> SimpleNamespace:
            polls.append(requested)
            # Cancelling a running pipeline because the session DB is flaky is
            # an operator decision this watcher does not take.
            assert not shutdown_event.is_set(), "repeated poll failures must not set the shutdown event"
            if len(polls) <= failures:
                raise OperationalError("SELECT runs", {}, Exception("server closed the connection unexpectedly"))
            return _run_record_stub(id=run_id, status="running", cancel_requested_at=datetime.now(UTC))

        mock_session_service.get_run.side_effect = get_run

        with patch("elspeth.web.execution.service.slog") as mock_slog:
            self._watch(service, real_loop, shutdown_event, run_id)

        assert shutdown_event.is_set(), "the watcher must keep polling through the outage and deliver the cancel"
        assert len(polls) == failures + 1
        warning_counts = [call.kwargs["consecutive_failures"] for call in mock_slog.warning.call_args_list]
        assert warning_counts == list(range(1, _LOSS_WATCHER_ESCALATE_AFTER_FAILURES))
        assert {call.args[0] for call in mock_slog.warning.call_args_list} == {"execution_loss_watcher_poll_retrying"}
        error_counts = [call.kwargs["consecutive_failures"] for call in mock_slog.error.call_args_list]
        assert error_counts == list(range(_LOSS_WATCHER_ESCALATE_AFTER_FAILURES, failures + 1))
        assert {call.args[0] for call in mock_slog.error.call_args_list} == {"execution_loss_watcher_poll_degraded"}

    @pytest.mark.parametrize(
        ("non_transient_error", "exc_class"),
        [
            # PostgreSQL wording: SQLite reports a missing column as an
            # OperationalError, which this watcher retries.
            pytest.param(
                ProgrammingError("SELECT runs", {}, Exception("column runs.cancel_requested_at does not exist")),
                "ProgrammingError",
                id="programming",
            ),
            # A DBAPI error SQLAlchemy did NOT flag as a disconnect is not
            # retried merely for being a DBAPIError.
            pytest.param(
                DBAPIError("SELECT runs", {}, Exception("unclassified driver error"), connection_invalidated=False),
                "DBAPIError",
                id="dbapi-connection-valid",
            ),
            # Not a DBAPIError, so only the final SQLAlchemyError arm can
            # handle it; the two params above never reach that arm.
            pytest.param(
                PendingRollbackError("Can't reconnect until invalid transaction is rolled back"),
                "PendingRollbackError",
                id="non-dbapi-pending-rollback",
            ),
        ],
    )
    def test_non_transient_poll_error_is_logged_and_raised_without_cancelling(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        real_loop: asyncio.AbstractEventLoop,
        non_transient_error: SQLAlchemyError,
        exc_class: str,
    ) -> None:
        run_id = uuid4()
        shutdown_event = threading.Event()
        polls: list[UUID] = []

        async def get_run(requested: UUID) -> SimpleNamespace:
            polls.append(requested)
            if len(polls) == 1:
                raise non_transient_error
            # Reached only if the watcher swallowed the error and polled again:
            # the watcher then returns normally and pytest.raises goes red
            # promptly instead of spinning until the hang guard.
            return _run_record_stub(id=run_id, status="running", cancel_requested_at=datetime.now(UTC))

        mock_session_service.get_run.side_effect = get_run

        with patch("elspeth.web.execution.service.slog") as mock_slog, pytest.raises(SQLAlchemyError) as raised:
            self._watch(service, real_loop, shutdown_event, run_id)

        assert raised.value is non_transient_error
        assert polls == [run_id]
        assert not shutdown_event.is_set()
        mock_slog.warning.assert_not_called()
        mock_slog.error.assert_called_once()
        event_name, *_ = mock_slog.error.call_args.args
        assert event_name == "execution_loss_watcher_poll_failed"
        # Class names only: SQLAlchemy messages carry SQL and connection detail.
        assert mock_slog.error.call_args.kwargs == {"run_id": str(run_id), "exc_class": exc_class}

    def test_done_callback_logs_a_failed_loss_watcher_instead_of_discarding_it(
        self,
        service: ExecutionServiceImpl,
        real_loop: asyncio.AbstractEventLoop,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        original = OperationalError("SELECT runs", {}, Exception("database is locked"))
        actual_signal = service._signal_shutdown_on_operation_loss

        async def failed_watcher(current_lease, shutdown_event, *, run_id, close_requested):
            # Real owned watcher producer delegates the ordinary cooperative
            # close, then raises the original endpoint fault. This logging unit
            # test does not claim physical SQL retry/fault coverage.
            await actual_signal(current_lease, shutdown_event, run_id=run_id, close_requested=close_requested)
            raise original

        service._loop = real_loop

        with (
            patch.object(service, "_signal_shutdown_on_operation_loss", side_effect=failed_watcher),
            patch("elspeth.web.execution.service.slog") as mock_slog,
        ):
            stage = real_loop.run_until_complete(
                execution_fixture.callback_stage(service, watcher_name="execution-operation-loss-test-run")
            )
            lease = stage.lease
            with pytest.raises(OperationalError) as caught:
                real_loop.run_until_complete(stage.join())
            assert caught.value is original
            execution_fixture.witness_cleanup_original(original)

        mock_slog.error.assert_called_once()
        event_name, *_ = mock_slog.error.call_args.args
        assert event_name == "execution_loss_watcher_failed"
        assert mock_slog.error.call_args.kwargs == {
            "watcher_task": "execution-operation-loss-test-run",
            "exc_class_chain": ["OperationalError"],
        }
        assert lease.closed, "logging the watcher failure must not skip the lease close"

    def test_done_callback_does_not_log_a_loss_watcher_it_cancelled(
        self,
        service: ExecutionServiceImpl,
        real_loop: asyncio.AbstractEventLoop,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        service._loop = real_loop
        stage = real_loop.run_until_complete(execution_fixture.callback_stage(service, watcher_name="execution-operation-loss-idle-run"))
        watcher = stage.watcher
        lease = stage.lease
        watcher_owner = service._loss_watcher_owner(watcher, lease)

        with patch("elspeth.web.execution.service.slog") as mock_slog:
            real_loop.run_until_complete(stage.join())

        assert watcher.done() and not watcher.cancelled()
        assert watcher_owner.close_requested.is_set()
        assert watcher_owner.outcome == (watcher, None)
        mock_slog.error.assert_not_called()
        assert lease.closed
