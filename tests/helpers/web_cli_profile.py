"""Real session admission for web/CLI profile tests with an externally chosen run id."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import structlog
import yaml
from sqlalchemy import Engine

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.execution.envelope import (
    RunExecutionInput,
    restore_execution_envelope,
    runtime_implementation_fingerprint,
    validate_run_execution_input,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord, RunRecord
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


class ProfileSessionService(SessionServiceImpl):
    """Choose only the run UUID; persist the real fenced admission and permits.

    The crash harness needs its run id before spawning the web leader. The
    override uses the production mutation capability and changes only the UUID
    allocation. Every later read, permit, event and status uses the real service.
    """

    def __init__(self, engine: Engine, *, root: Path, run_id: UUID, snapshot: PluginAvailabilitySnapshot, user_id: str) -> None:
        super().__init__(
            engine,
            telemetry=build_sessions_telemetry(),
            log=structlog.get_logger("test.web-cli-profile"),
            owner_instance_id="web-cli-profile",
            session_operation_lease_seconds=300,
        )
        self.profile_run_id = run_id
        self.profile_settings_path = root / "admitted-settings.yaml"
        self.profile_snapshot = snapshot
        self.profile_user_id = user_id

    async def create_run(
        self,
        session_id: UUID,
        state_id: UUID,
        pipeline_yaml: str | None = None,
        *,
        session_operation_context: SessionOperationContext,
        execution_input: RunExecutionInput | None = None,
    ) -> RunRecord:
        assert str(session_id) == session_operation_context.fence.session_id
        record = cast(
            RunRecord,
            await self._run_sync(
                self.session_operation_authority.mutate,
                session_operation_context,
                lambda tx: tx.runs.create_pending_run(
                    run_id=self.profile_run_id,
                    state_id=state_id,
                    pipeline_yaml=pipeline_yaml,
                    started_at=datetime.now(UTC),
                    execution_input=execution_input,
                ),
            ),
        )
        persisted = await self.get_run_execution_input(record.id)
        assert persisted is not None
        validate_run_execution_input(persisted)
        implementation = runtime_implementation_fingerprint(self.profile_snapshot)
        restored = restore_execution_envelope(
            persisted.envelope_json,
            current_snapshot=self.profile_snapshot,
            user_id=self.profile_user_id,
            auth_provider_type="local",
            resolver=None,
            implementation_fingerprint=implementation,
            deployment_generation=implementation,
        )
        # This fixture contains no credential references or blob-backed sources.
        # Followers/resume use the verified retained path, never author YAML.
        assert restored.retained_inputs
        assert not restored.blob_inputs
        assert not restored.env_ref_names
        self.profile_settings_path.write_text(yaml.safe_dump(deep_thaw(restored.settings.executable_config)), encoding="utf-8")
        return record


async def create_profile_session(
    root: Path,
    *,
    state: CompositionStateRecord,
    run_id: UUID,
    snapshot: PluginAvailabilitySnapshot,
    user_id: str,
) -> tuple[Engine, ProfileSessionService, UUID]:
    engine = create_session_engine(f"sqlite:///{root / 'sessions.db'}")
    initialize_session_schema(engine)
    sessions = ProfileSessionService(engine, root=root, run_id=run_id, snapshot=snapshot, user_id=user_id)
    session = await sessions.create_session(user_id, "web CLI profile", "local")
    compose = await SessionOperationLease.acquire(
        sessions.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=300,
    )
    try:
        await sessions.save_composition_state(
            session.id,
            CompositionStateData(
                sources=state.sources,
                nodes=state.nodes,
                edges=state.edges,
                outputs=state.outputs,
                metadata_=state.metadata_,
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
            session_operation_context=compose.context,
        )
    finally:
        await compose.close()
    return engine, sessions, session.id
