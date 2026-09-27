"""Runtime-equivalent validation for Composer authoring decisions.

The owner holds deployment dependencies and the process-local in-flight
coordinator. Completed verdicts remain in a caller-owned compose cache.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import NoReturn, cast
from uuid import UUID

from opentelemetry import metrics

from elspeth.contracts.blobs import BlobNotFoundError, BlobRecord, BlobServiceProtocol
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.application_policy import PluginPolicyContextFactory
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.discovery_cache import RuntimePreflightCache
from elspeth.web.composer.no_tool_policy import is_pending_interpretation_handoff, state_is_structurally_empty
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.protocol import ComposerRuntimePreflightError, ComposerSettings
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.preflight import runtime_preflight_settings_hash
from elspeth.web.execution.runtime_preflight import RuntimePreflightCoordinator, RuntimePreflightFailure, RuntimePreflightKey
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.execution.validation import validate_pipeline
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.secrets.wiring_policy import SecretWiringPolicy

_KNOWN_PREFLIGHT_EXCEPTION_CLASSES: frozenset[str] = frozenset(
    {"TimeoutError", "PluginNotFoundError", "PluginConfigError", "GraphValidationError", "ValidationError"}
)

_RUNTIME_PREFLIGHT_COUNTER = metrics.get_meter(__name__).create_counter(
    "composer.runtime_preflight.total",
    description="Total runtime-equivalent preflight invocations in the composer service",
)


def _preflight_verdict(result: ValidationResult) -> str:
    """Classify a returned validation result for bounded telemetry."""
    if result.is_valid:
        return "valid"
    if is_pending_interpretation_handoff(result):
        return "pending_review"
    return "invalid"


def _contains_blob_ref(value: object) -> bool:
    """Conservatively disable verdict reuse when an option contains a blob binding."""
    if isinstance(value, Mapping):
        return "blob_ref" in value or any(_contains_blob_ref(child) for child in value.values())
    if isinstance(value, (tuple, list)):
        return any(_contains_blob_ref(child) for child in value)
    return False


def _state_contains_blob_ref(state: CompositionState) -> bool:
    return any(
        _contains_blob_ref(options)
        for options in (
            *(source.options for source in state.sources.values()),
            *(node.options for node in state.nodes),
            *(output.options for output in state.outputs),
        )
    )


class ComposerPreflight:
    """Own Composer validation and in-flight coalescing without turn state."""

    def __init__(
        self,
        *,
        catalog: CatalogService,
        settings: ComposerSettings,
        policy_context: PluginPolicyContextFactory,
        blob_service: BlobServiceProtocol | None,
        secret_service: WebSecretResolver | None,
        secret_wiring_policy: SecretWiringPolicy,
        operator_profile_registry: OperatorProfileRegistry | None,
        coordinator: RuntimePreflightCoordinator,
    ) -> None:
        self._catalog = catalog
        self._settings = settings
        self._policy_context = policy_context
        self._blob_service = blob_service
        self._secret_service = secret_service
        self._secret_wiring_policy = secret_wiring_policy
        self._operator_profile_registry = operator_profile_registry
        self._coordinator = coordinator
        self._timeout_seconds = settings.composer_runtime_preflight_timeout_seconds

    def runtime_preflight(
        self,
        state: CompositionState,
        user_id: str | None,
        session_id: str | None,
        plugin_snapshot: PluginAvailabilitySnapshot | None = None,
        *,
        session_operation_context: SessionOperationContext | None = None,
        allow_pending_interpretation_placeholders: bool = False,
    ) -> ValidationResult:
        if plugin_snapshot is None:
            plugin_snapshot, _policy_catalog = self._policy_context.build(user_id)

        def _blob_get_metadata(blob_id: UUID) -> BlobRecord | None:
            if self._blob_service is None or session_operation_context is None:
                return None
            try:
                record = self._blob_service.get_blob_sync(blob_id, session_operation_context)
            except BlobNotFoundError:
                return None
            if session_id is not None and str(record.session_id) != session_id:
                return None
            return record

        def _blob_get_content(blob_id: UUID) -> tuple[BlobRecord, bytes]:
            if self._blob_service is None or session_operation_context is None:
                raise BlobNotFoundError(str(blob_id))
            record, content = self._blob_service.read_blob_content_sync(blob_id, session_operation_context)
            if session_id is not None and str(record.session_id) != session_id:
                raise BlobNotFoundError(str(blob_id))
            return record, content

        return validate_pipeline(
            state,
            self._settings,
            yaml_generator,
            secret_service=self._secret_service,
            secret_wiring_policy=self._secret_wiring_policy,
            user_id=user_id,
            session_id=session_id,
            blob_get_metadata=_blob_get_metadata,
            blob_get_content=_blob_get_content,
            allow_pending_interpretation_placeholders=allow_pending_interpretation_placeholders,
            plugin_snapshot=plugin_snapshot,
            profile_registry=self._operator_profile_registry,
            catalog=self._catalog,
        )

    def new_cache(self) -> RuntimePreflightCache:
        return {}

    @staticmethod
    def _raise_cached_runtime_preflight_failure(
        failure: RuntimePreflightFailure,
        *,
        state: CompositionState,
        initial_version: int,
        llm_calls: tuple[ComposerLLMCall, ...] = (),
    ) -> NoReturn:
        raise ComposerRuntimePreflightError.capture(
            failure.original_exc,
            state=state,
            initial_version=initial_version,
            llm_calls=llm_calls,
        ) from failure.original_exc

    def key(
        self,
        state: CompositionState,
        *,
        session_scope: str,
        plugin_snapshot: PluginAvailabilitySnapshot | None,
        interpretation_tolerant: bool = False,
    ) -> RuntimePreflightKey:
        settings_hash = runtime_preflight_settings_hash(self._settings)
        if plugin_snapshot is not None:
            settings_hash = f"{settings_hash}:{plugin_snapshot.snapshot_hash}"
        return RuntimePreflightKey(
            session_scope=session_scope,
            state_version=state.version,
            state_content_hash=composition_content_hash(state),
            settings_hash=settings_hash,
            interpretation_tolerant=interpretation_tolerant,
        )

    async def cached_runtime_preflight(
        self,
        state: CompositionState,
        *,
        user_id: str | None,
        session_id: str | None,
        cache: RuntimePreflightCache,
        initial_version: int,
        session_scope: str,
        llm_calls: tuple[ComposerLLMCall, ...] = (),
        plugin_snapshot: PluginAvailabilitySnapshot | None = None,
        session_operation_context: SessionOperationContext | None = None,
        interpretation_tolerant: bool = False,
        deadline: float | None = None,
    ) -> ValidationResult:
        key = self.key(
            state,
            session_scope=session_scope,
            plugin_snapshot=plugin_snapshot,
            interpretation_tolerant=interpretation_tolerant,
        )
        # Blob status changes without state version changes. Only in-flight
        # work is coalesced, and its authority is part of the coordinator key.
        contains_blob_ref = _state_contains_blob_ref(state)
        blob_operation_context = session_operation_context if contains_blob_ref else None
        coordinator_key = replace(key, session_operation_context=blob_operation_context)
        cached = cache[key] if not contains_blob_ref and key in cache else None
        if isinstance(cached, ValidationResult):
            return cached
        if isinstance(cached, RuntimePreflightFailure):
            self._raise_cached_runtime_preflight_failure(
                cached,
                state=state,
                initial_version=initial_version,
                llm_calls=llm_calls,
            )

        async def worker() -> ValidationResult:
            preflight: Callable[..., ValidationResult]
            if interpretation_tolerant and blob_operation_context is not None:
                preflight = functools.partial(
                    self.runtime_preflight,
                    session_operation_context=blob_operation_context,
                    allow_pending_interpretation_placeholders=True,
                )
            elif interpretation_tolerant:
                preflight = functools.partial(self.runtime_preflight, allow_pending_interpretation_placeholders=True)
            elif blob_operation_context is not None:
                preflight = functools.partial(self.runtime_preflight, session_operation_context=blob_operation_context)
            else:
                preflight = self.runtime_preflight
            args = (state, user_id, session_id) if plugin_snapshot is None else (state, user_id, session_id, plugin_snapshot)
            return await run_sync_in_worker(cast(Callable[..., ValidationResult], preflight), *args)

        # The deadline belongs to this awaiter. The coordinator retains its
        # worker after a timeout so another caller joins the same work.
        timeout = self._timeout_seconds
        if deadline is not None:
            timeout = max(0.0, min(timeout, deadline - asyncio.get_running_loop().time()))
        entry = await self._coordinator.run(coordinator_key, worker, timeout=timeout)
        if not contains_blob_ref:
            cache[key] = entry
        if isinstance(entry, RuntimePreflightFailure):
            exc_name = type(entry.original_exc).__name__
            exc_class = exc_name if exc_name in _KNOWN_PREFLIGHT_EXCEPTION_CLASSES else "other"
            _RUNTIME_PREFLIGHT_COUNTER.add(1, {"outcome": "failure", "exception_class": exc_class})
            self._raise_cached_runtime_preflight_failure(
                entry,
                state=state,
                initial_version=initial_version,
                llm_calls=llm_calls,
            )
        _RUNTIME_PREFLIGHT_COUNTER.add(
            1,
            {"outcome": "returned", "verdict": _preflight_verdict(entry), "interpretation_tolerant": interpretation_tolerant},
        )
        return entry

    async def pending_handoff_outstanding_findings(
        self,
        state: CompositionState,
        *,
        user_id: str | None,
        session_id: str | None,
        cache: RuntimePreflightCache,
        initial_version: int,
        session_scope: str,
        llm_calls: tuple[ComposerLLMCall, ...] = (),
        plugin_snapshot: PluginAvailabilitySnapshot | None = None,
        session_operation_context: SessionOperationContext | None = None,
        deadline: float | None = None,
    ) -> ValidationResult | None:
        """Return masked structural findings, or None for a pure handoff."""
        tolerant = await self.cached_runtime_preflight(
            state,
            user_id=user_id,
            session_id=session_id,
            cache=cache,
            initial_version=initial_version,
            session_scope=session_scope,
            llm_calls=llm_calls,
            plugin_snapshot=plugin_snapshot,
            session_operation_context=session_operation_context,
            interpretation_tolerant=True,
            deadline=deadline,
        )
        if tolerant.is_valid:
            return None
        return tolerant

    async def turn_runtime_preflight(
        self,
        *,
        state: CompositionState,
        user_id: str | None,
        session_id: str | None,
        last_runtime_preflight: ValidationResult | None,
        runtime_preflight_cache: RuntimePreflightCache,
        initial_version: int,
        session_scope: str,
        recorder: BufferingRecorder,
        plugin_snapshot: PluginAvailabilitySnapshot | None = None,
        session_operation_context: SessionOperationContext | None = None,
    ) -> ValidationResult | None:
        """A turn's deterministic verdict; structurally empty states are absent."""
        if state_is_structurally_empty(state):
            return None
        return await self.reuse_or_recompute_runtime_preflight(
            state=state,
            user_id=user_id,
            session_id=session_id,
            last_runtime_preflight=last_runtime_preflight,
            runtime_preflight_cache=runtime_preflight_cache,
            initial_version=initial_version,
            session_scope=session_scope,
            llm_calls=recorder.llm_calls,
            plugin_snapshot=plugin_snapshot,
            session_operation_context=session_operation_context,
        )

    async def reuse_or_recompute_runtime_preflight(
        self,
        *,
        state: CompositionState,
        user_id: str | None,
        session_id: str | None,
        last_runtime_preflight: ValidationResult | None,
        runtime_preflight_cache: RuntimePreflightCache,
        initial_version: int,
        session_scope: str,
        llm_calls: tuple[ComposerLLMCall, ...],
        plugin_snapshot: PluginAvailabilitySnapshot | None,
        session_operation_context: SessionOperationContext | None = None,
    ) -> ValidationResult | None:
        """The shared reuse rule for completion, repair, and advisor gates."""
        if state.version > initial_version:
            return await self.cached_runtime_preflight(
                state,
                user_id=user_id,
                session_id=session_id,
                cache=runtime_preflight_cache,
                initial_version=initial_version,
                session_scope=session_scope,
                llm_calls=llm_calls,
                plugin_snapshot=plugin_snapshot,
                session_operation_context=session_operation_context,
            )
        if last_runtime_preflight is not None and not _state_contains_blob_ref(state):
            return last_runtime_preflight
        if _state_contains_blob_ref(state):
            return await self.cached_runtime_preflight(
                state,
                user_id=user_id,
                session_id=session_id,
                cache=runtime_preflight_cache,
                initial_version=initial_version,
                session_scope=session_scope,
                llm_calls=llm_calls,
                plugin_snapshot=plugin_snapshot,
                session_operation_context=session_operation_context,
            )
        if state.sources and state.outputs and not state.validate().is_valid:
            return await self.cached_runtime_preflight(
                state,
                user_id=user_id,
                session_id=session_id,
                cache=runtime_preflight_cache,
                initial_version=initial_version,
                session_scope=session_scope,
                llm_calls=llm_calls,
                plugin_snapshot=plugin_snapshot,
                session_operation_context=session_operation_context,
            )
        return None
