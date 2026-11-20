"""Runtime-equivalent validation for Composer authoring decisions.

The owner holds deployment dependencies and the process-local in-flight
coordinator. Completed verdicts remain in a caller-owned compose cache.
"""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import NoReturn, cast
from uuid import UUID

from opentelemetry import metrics

from elspeth.contracts.blobs import BlobNotFoundError, BlobRecord, BlobServiceProtocol
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.core.secrets import collect_secret_ref_marker_sites
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.catalog.validation_reuse import (
    PREFLIGHT_REUSE_DECLINED_COUNTER,
    VALIDATION_REUSE_COUNTER,
    MutationEpoch,
    PreflightReuseDeclineReason,
)
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.application_policy import PluginPolicyContextFactory
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.discovery_cache import (
    BlobBoundPreflightVerdict,
    BlobEvidence,
    ComposeRoundPreflightCache,
    PreflightEvidence,
    ResolvedLocalPath,
    RuntimePreflightCache,
)
from elspeth.web.composer.no_tool_policy import is_pending_interpretation_handoff, state_is_structurally_empty
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.protocol import ComposerRuntimePreflightError, ComposerSettings
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.preflight import runtime_preflight_settings_hash
from elspeth.web.execution.runtime_preflight import (
    RuntimePreflightCoordinator,
    RuntimePreflightEntry,
    RuntimePreflightFailure,
    RuntimePreflightKey,
)
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.execution.validation import validate_pipeline
from elspeth.web.execution.validation_memo import BlobReadLog
from elspeth.web.paths import (
    NESTED_LOCAL_PATH_OPTION_KEYS,
    SINK_LOCAL_PATH_OPTION_KEYS,
    SOURCE_LOCAL_PATH_OPTION_KEYS,
    is_local_path_value,
    resolve_data_path,
    resolve_sink_data_path,
)
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


def _state_has_secret_ref_sites(state: CompositionState, env_ref_names: frozenset[str]) -> bool:
    """Whether any option wires a deferred secret (a marker, or ``${NAME}`` for a listed name).

    The preflight resolves a wired secret's value, which no evidence names, so
    such a state is never reused; the execution-side memo keeps the same
    states out (``web/execution/service.py``, ``_state_has_secret_ref_sites``).
    """
    return any(
        collect_secret_ref_marker_sites(options, env_ref_names)
        for options in (
            *(source.options for source in state.sources.values()),
            *(node.options for node in state.nodes),
            *(output.options for output in state.outputs),
        )
    )


def _collect_state_blob_ref_ids(state: CompositionState, into: set[UUID]) -> bool:
    """Add every ``blob_ref`` id in the state's options; False when one is not a UUID string."""
    return all(
        _collect_blob_ref_ids(options, into)
        for options in (
            *(source.options for source in state.sources.values()),
            *(node.options for node in state.nodes),
            *(output.options for output in state.outputs),
        )
    )


def _collect_blob_ref_ids(value: object, into: set[UUID]) -> bool:
    """Add every ``blob_ref`` id under ``value``; False when one is not a UUID string.

    This is the superset of the ids a preflight can read: every mapping that
    carries the key, as ``_contains_blob_ref`` sees it. A value that does not
    parse ends reuse for the call, so the base path judges it.
    """
    if isinstance(value, Mapping):
        if "blob_ref" in value:
            ref = value["blob_ref"]
            if type(ref) is not str:
                return False
            try:
                into.add(UUID(ref))
            except ValueError:
                return False
        return all(_collect_blob_ref_ids(child, into) for child in value.values())
    if isinstance(value, (tuple, list)):
        return all(_collect_blob_ref_ids(child, into) for child in value)
    return True


def _resolved_local_paths(state: CompositionState, data_dir: str, session_id: str | None) -> tuple[ResolvedLocalPath, ...]:
    """Every local path the preflight's path allowlist resolves, resolved the same way.

    ``Path.resolve`` follows symbolic links, so the verdict on a path depends
    on the filesystem as well as the state: a link created at a sink path can
    move its resolved target out of the session's folders. This mirrors the
    composer preflight's path phase (``validate_authored_paths`` in
    ``web/execution/_validation_authoring.py``, which runs with no MCP root
    authority here): source keys through ``resolve_data_path``, sink keys and
    ``provider_config`` keys through ``resolve_sink_data_path``. A value that
    is not a path string is judged from the state alone and is skipped; a
    resolution error is recorded by its class, as the phase reports it.
    """
    resolved: list[ResolvedLocalPath] = []

    def _record(component: str, key: str, value: object, resolver: Callable[[str], Path]) -> None:
        if not is_local_path_value(value):
            return
        try:
            outcome = str(resolver(cast(str, value)))
        except (OSError, TypeError, ValueError) as exc:
            outcome = f"unresolvable:{type(exc).__name__}"
        resolved.append((component, key, outcome))

    def _source_path(value: str) -> Path:
        return resolve_data_path(value, data_dir)

    def _sink_path(value: str) -> Path:
        return resolve_sink_data_path(value, data_dir, session_id=session_id)

    for source_name, source in state.sources.items():
        for key in SOURCE_LOCAL_PATH_OPTION_KEYS:
            if key in source.options:
                _record(f"source:{source_name}", key, source.options[key], _source_path)
    for output in state.outputs:
        for key in SINK_LOCAL_PATH_OPTION_KEYS:
            if key in output.options:
                _record(f"sink:{output.name}", key, output.options[key], _sink_path)
    for node in state.nodes:
        if node.plugin is None or "provider_config" not in node.options:
            continue
        provider_config = node.options["provider_config"]
        if not isinstance(provider_config, Mapping):
            continue
        for key in NESTED_LOCAL_PATH_OPTION_KEYS:
            if key in provider_config:
                _record(f"node:{node.id}", key, provider_config[key], _sink_path)
    return tuple(resolved)


def _judge_bracket(
    before: PreflightEvidence | PreflightReuseDeclineReason,
    after: PreflightEvidence | PreflightReuseDeclineReason,
    observed: BlobReadLog,
) -> PreflightEvidence | PreflightReuseDeclineReason:
    """The evidence a compute was judged on, or why it cannot be vouched for.

    The evidence read just before and just after the compute must be equal,
    and every blob row the compute's own reads observed must equal the row in
    that evidence: a change that happens and reverts during the compute is
    seen by the compute but by neither bracket read.
    """
    if not isinstance(before, PreflightEvidence):
        return before
    if not isinstance(after, PreflightEvidence):
        return after
    if after != before:
        return "evidence_changed"
    reads = observed.freeze()
    if reads is None:
        return "evidence_changed"
    rows = {blob.record.id: blob.record for blob in before.blobs}
    if any(blob_id not in rows or rows[blob_id] != record for blob_id, record in reads):
        return "evidence_changed"
    return before


def _store_decision(
    entry: RuntimePreflightEntry,
    bracketing: list[PreflightEvidence | PreflightReuseDeclineReason],
    *,
    epoch_moved: bool,
) -> PreflightEvidence | PreflightReuseDeclineReason:
    """What a blob-bearing compute's verdict is stored under, or why it is not stored."""
    if not isinstance(entry, ValidationResult):
        return "compute_failed"
    if not bracketing:
        return "joined_in_flight"
    if epoch_moved:
        return "mutation_during_compute"
    return bracketing[0]


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
        validation_reuse_enabled: bool = False,
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
        self._validation_reuse_enabled = validation_reuse_enabled

    def runtime_preflight(
        self,
        state: CompositionState,
        user_id: str | None,
        session_id: str | None,
        plugin_snapshot: PluginAvailabilitySnapshot | None = None,
        *,
        session_operation_context: SessionOperationContext | None = None,
        allow_pending_interpretation_placeholders: bool = False,
        blob_reads: BlobReadLog | None = None,
    ) -> ValidationResult:
        """Run the runtime-equivalent validation once.

        ``blob_reads``, when given, records every blob row this run's own reads
        observed (``None`` for a row not found; a failed content read makes the
        log unusable). Reuse stores the verdict only if those observations
        equal the evidence read around the run, so a change that happens and
        reverts while the run is reading is never stored.
        """
        if plugin_snapshot is None:
            plugin_snapshot, _policy_catalog = self._policy_context.build(user_id)

        def _blob_get_metadata(blob_id: UUID) -> BlobRecord | None:
            if self._blob_service is None or session_operation_context is None:
                return None
            try:
                record = self._blob_service.get_blob_sync(blob_id, session_operation_context)
            except BlobNotFoundError:
                if blob_reads is not None:
                    blob_reads.record(blob_id, None)
                return None
            if blob_reads is not None:
                blob_reads.record(blob_id, record)
            if session_id is not None and str(record.session_id) != session_id:
                return None
            return record

        def _blob_get_content(blob_id: UUID) -> tuple[BlobRecord, bytes]:
            if self._blob_service is None or session_operation_context is None:
                raise BlobNotFoundError(str(blob_id))
            try:
                record, content = self._blob_service.read_blob_content_sync(blob_id, session_operation_context)
            except BaseException:
                # A failed content read is not a function of the metadata row
                # the evidence re-checks, so the run must not be stored; the
                # execution memo does the same (``web/execution/service.py``).
                if blob_reads is not None:
                    blob_reads.mark_uncacheable()
                raise
            if blob_reads is not None:
                blob_reads.record(blob_id, record)
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

    def new_cache(self, *, mutation_epoch: MutationEpoch | None = None) -> RuntimePreflightCache:
        """One compose call's cache.

        With reuse switched on and the round view's mutation epoch, the cache
        can also hold verdicts for blob-bearing states. Otherwise it is the
        plain mapping, and blob-bearing states always recompute, as before
        reuse existed.
        """
        if mutation_epoch is None or not self._validation_reuse_enabled:
            return {}
        return ComposeRoundPreflightCache(mutation_epoch=mutation_epoch)

    def _read_preflight_evidence(
        self,
        state: CompositionState,
        session_operation_context: SessionOperationContext | None,
        user_id: str | None,
        session_id: str | None,
    ) -> PreflightEvidence | PreflightReuseDeclineReason:
        """Read what a runtime preflight of ``state`` reads that can change, or say why not.

        Runs in a worker thread, through the same ``list_refs`` call, the same
        fenced metadata read as ``get_blob_sync`` (every record in one
        ``get_blobs_sync`` transaction) under the same operation context, and
        the same path resolvers that the preflight's own readers use. A reason means "cannot vouch for
        the evidence": the caller then neither reuses nor stores, so the
        compute that follows runs exactly as base and reports any failure
        through its own reads.

        Outside blob storage the preflight reads the filesystem only by
        resolving source, sink and ``provider_config`` paths (symbolic links
        included) for the path allowlist; those resolutions are evidence.
        Whether a file exists at a source or sink path is not: plugins are
        built in preflight mode, where local file sinks skip output-collision
        resolution and sources do not open their input, and sink effects are
        inspected only on the execution path.
        """
        if self._blob_service is None:
            return "no_blob_service"
        if session_operation_context is None:
            return "no_operation_context"
        blob_ids: set[UUID] = set()
        if not _collect_state_blob_ref_ids(state, blob_ids):
            return "blob_ref_not_uuid"
        try:
            secret_inventory = (
                frozenset(self._secret_service.list_refs(user_id))
                if self._secret_service is not None and user_id is not None
                else frozenset()
            )
            if _state_has_secret_ref_sites(state, frozenset(item.name for item in secret_inventory)):
                return "secret_ref_sites"
            blobs: list[BlobEvidence] = []
            # One fenced transaction for every record (one fence write at any
            # N); a missing or out-of-custody blob fails the whole read, as one
            # failing ``get_blob_sync`` did. Stats follow the commit, as before:
            # the before/after bracket, not this read, vouches for them.
            for record in self._blob_service.get_blobs_sync(blob_ids, session_operation_context):
                stat = os.stat(record.storage_path)
                blobs.append(
                    BlobEvidence(
                        record=record,
                        storage_size=stat.st_size,
                        storage_mtime_ns=stat.st_mtime_ns,
                        storage_ctime_ns=stat.st_ctime_ns,
                        storage_inode=stat.st_ino,
                    )
                )
            local_paths = _resolved_local_paths(state, str(self._settings.data_dir), session_id)
        except Exception:
            # Counted by the caller as ``evidence_read_failed``; not raised,
            # because the base compute performs the same reads next and owns
            # the failure it meets.
            return "evidence_read_failed"
        return PreflightEvidence(blobs=tuple(blobs), secret_inventory=secret_inventory, local_paths=local_paths)

    def _remaining_budget(self, deadline: float | None) -> float:
        """This awaiter's seconds left: the preflight timeout, cut to the deadline."""
        timeout = self._timeout_seconds
        if deadline is not None:
            timeout = max(0.0, min(timeout, deadline - asyncio.get_running_loop().time()))
        return timeout

    async def _current_evidence_within_budget(
        self,
        state: CompositionState,
        session_operation_context: SessionOperationContext | None,
        user_id: str | None,
        session_id: str | None,
        deadline: float | None,
    ) -> PreflightEvidence | None:
        """Re-read the evidence for a hit inside the caller's budget, or None.

        None sends the caller down the base path: with no budget left that
        path refuses admission and raises ``TimeoutError`` exactly as base
        does, so a reuse never answers past a deadline a compute could not.
        """
        budget = self._remaining_budget(deadline)
        if budget <= 0:
            return None
        try:
            current = await asyncio.wait_for(
                run_sync_in_worker(self._read_preflight_evidence, state, session_operation_context, user_id, session_id),
                timeout=budget,
            )
        except TimeoutError:
            return None
        return current if isinstance(current, PreflightEvidence) else None

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

        # Blob-bearing states reuse only through the round store, and only a
        # returned verdict for this exact state object, in the same mutation
        # epoch, while a fresh read of the evidence, inside this caller's
        # budget, equals what the compute was bracketed by. A plain dict cache
        # has no such store.
        blob_store = (
            cache if contains_blob_ref and self._validation_reuse_enabled and isinstance(cache, ComposeRoundPreflightCache) else None
        )
        if blob_store is not None and coordinator_key in blob_store.blob_verdicts:
            stored = blob_store.blob_verdicts[coordinator_key]
            if stored.state is state and stored.mutation_epoch == blob_store.mutation_epoch.value:
                current = await self._current_evidence_within_budget(state, blob_operation_context, user_id, session_id, deadline)
                if current is not None and current == stored.evidence:
                    VALIDATION_REUSE_COUNTER.add(1, {"layer": "runtime_preflight", "interpretation_tolerant": interpretation_tolerant})
                    return stored.result.model_copy(deep=True)
        epoch_at_start = blob_store.mutation_epoch.value if blob_store is not None else 0
        # Filled only by this caller's own worker: the evidence the compute was
        # judged on, or why it cannot be vouched for. A caller that joins
        # another caller's in-flight worker gets no entry and stores nothing.
        bracketing: list[PreflightEvidence | PreflightReuseDeclineReason] = []

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
            if blob_store is None:
                return await run_sync_in_worker(cast(Callable[..., ValidationResult], preflight), *args)
            unbracketed = cast(Callable[..., ValidationResult], preflight)

            def bracketed(*call_args: object) -> ValidationResult:
                before = self._read_preflight_evidence(state, blob_operation_context, user_id, session_id)
                observed = BlobReadLog()
                result = unbracketed(*call_args, blob_reads=observed)
                bracketing.append(
                    _judge_bracket(
                        before,
                        self._read_preflight_evidence(state, blob_operation_context, user_id, session_id)
                        if isinstance(before, PreflightEvidence)
                        else before,
                        observed,
                    )
                )
                return result

            return await run_sync_in_worker(bracketed, *args)

        # The deadline belongs to this awaiter. The coordinator retains its
        # worker after a timeout so another caller joins the same work.
        entry = await self._coordinator.run(coordinator_key, worker, timeout=self._remaining_budget(deadline))
        if not contains_blob_ref:
            cache[key] = entry
        elif blob_store is not None:
            judged = _store_decision(entry, bracketing, epoch_moved=blob_store.mutation_epoch.value != epoch_at_start)
            if isinstance(judged, PreflightEvidence) and isinstance(entry, ValidationResult):
                blob_store.blob_verdicts[coordinator_key] = BlobBoundPreflightVerdict(
                    state=state,
                    mutation_epoch=epoch_at_start,
                    evidence=judged,
                    result=entry.model_copy(deep=True),
                )
            elif not isinstance(judged, PreflightEvidence):
                PREFLIGHT_REUSE_DECLINED_COUNTER.add(1, {"reason": judged})
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
