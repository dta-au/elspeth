"""Plugin lifecycle teardown for the orchestrator.

This module owns the finally-block cleanup contract: ``on_complete(ctx)`` then
``close()`` on every plugin (transforms, sinks, optionally the source), with
each hook individually guarded so one plugin's failure does not prevent the
others from cleaning up. Tier-1 errors (``TIER_1_ERRORS``) bypass the guard
and propagate immediately — they signal system-level corruption, not a
recoverable cleanup failure.

Pure delegation target for the Orchestrator (same pattern as
graph_wiring.py / runtime_preflight.py): it holds no orchestrator state and
operates only on the parameters passed in.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache, partial
from pathlib import Path
from types import CodeType, TracebackType
from typing import TYPE_CHECKING, cast

import structlog

import elspeth.contracts.errors as contract_errors
from elspeth.contracts.enums import RunMode
from elspeth.contracts.secret_scrub import scrub_text_for_audit

if TYPE_CHECKING:
    from elspeth.contracts.plugin_context import PluginContext
    from elspeth.contracts.plugin_protocols import SinkProtocol, SourceProtocol, TransformProtocol
    from elspeth.engine.orchestrator.types import PipelineConfig

slog = structlog.get_logger(__name__)

_CLEANUP_ERROR_PREVIEW_CHARS = 160
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_MAX_DIAGNOSTIC_CHAIN = 8
_MAX_DIAGNOSTIC_FRAMES = 8
_MAX_DIAGNOSTIC_SOURCE_BYTES = 1024 * 1024


@lru_cache(maxsize=16)
def _trusted_source_codes(path: Path, fingerprint: tuple[int, int, int, int, int]) -> frozenset[CodeType]:
    """Compile bounded trusted source without executing it; cache by file identity.

    The caller admits paths inside the installed package before this read.
    Read/parse failure only withholds optional coordinates, never the cleanup
    failure itself. The fingerprint invalidates cached evidence on source edits.
    """
    try:
        with path.open("rb") as source:
            content = source.read(_MAX_DIAGNOSTIC_SOURCE_BYTES + 1)
        if len(content) > _MAX_DIAGNOSTIC_SOURCE_BYTES:
            return frozenset()
        pending = [compile(content, str(path), "exec", dont_inherit=True)]
    except (OSError, SyntaxError, ValueError):
        return frozenset()
    codes: set[CodeType] = set()
    while pending and len(codes) < 4096:
        code = pending.pop()
        codes.add(code)
        pending.extend(value for value in code.co_consts if isinstance(value, CodeType))
    return frozenset(codes)


@dataclass(frozen=True)
class _CleanupErrorStructure:
    cause_types: tuple[str, ...]
    frames: tuple[str, ...]
    cause_chain_truncated: bool
    frames_withheld: int
    frames_truncated: bool


def _code_structure_is_safe(code: CodeType) -> bool:
    """Admit compiler metadata types before code hashing/equality can call them.

    Generated code can inject arbitrary objects into co_consts. Do not invoke
    their hash/equality methods while collecting optional failure metadata.
    The shared item budget also bounds deeply nested or oversized constants.
    """
    pending: list[tuple[object, int]] = [(code, 0)]
    remaining = 4096
    remaining_bytes = _MAX_DIAGNOSTIC_SOURCE_BYTES
    while pending and remaining:
        value, depth = pending.pop()
        remaining -= 1
        kind = type(value)
        children: Collection[object]
        if kind is CodeType:
            candidate = cast(CodeType, value)
            text_fields = (candidate.co_name, candidate.co_qualname, candidate.co_filename)
            byte_fields = (candidate.co_code, candidate.co_linetable, candidate.co_exceptiontable)
            name_fields = (candidate.co_names, candidate.co_varnames, candidate.co_freevars, candidate.co_cellvars)
            if any(type(text) is not str for text in text_fields) or any(type(data) is not bytes for data in byte_fields):
                return False
            if any(type(names) is not tuple or len(names) > remaining for names in name_fields):
                return False
            if any(type(name) is not str for names in name_fields for name in names):
                return False
            if type(candidate.co_consts) is not tuple or len(candidate.co_consts) > remaining:
                return False
            children = (*candidate.co_consts, *text_fields, *byte_fields, *name_fields)
        elif kind is tuple or kind is frozenset:
            children = cast("tuple[object, ...] | frozenset[object]", value)
        elif kind is str or kind is bytes:
            remaining_bytes -= len(cast("str | bytes", value))
            if remaining_bytes < 0:
                return False
            continue
        elif kind is int:
            remaining_bytes -= (cast(int, value).bit_length() + 7) // 8
            if remaining_bytes < 0:
                return False
            continue
        elif value is None or value is Ellipsis or any(kind is scalar for scalar in (bool, float, complex)):
            continue
        else:
            return False
        if depth >= 32 or len(children) + len(pending) > remaining:
            return False
        pending.extend((child, depth + 1) for child in children)
    return not pending


def _cleanup_error_structure(error: Exception) -> _CleanupErrorStructure:
    """Capture bounded structural evidence, admitting coordinates against source.

    A traceback filename alone is not authority: generated code can choose any
    filename. Only code matching compiled installed package source contributes
    a relative path and a line in that code's line table. External/generated
    frames, messages, source lines, function names and locals are never emitted.
    """
    causes: list[str] = []
    frames: list[str] = []
    seen: set[int] = set()
    sources: dict[Path, frozenset[CodeType]] = {}
    withheld = 0
    frames_truncated = False
    examined_frames = 0
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < _MAX_DIAGNOSTIC_CHAIN:
        if seen:
            name: str = type.__dict__["__name__"].__get__(type(current))
            if type(name) is not str:
                causes.append("<invalid-exception-class>")
            elif len(name) > 80:
                causes.append("<exception-class-name-too-long>")
            else:
                causes.append(scrub_text_for_audit(name) if name.isidentifier() else "<invalid-exception-class>")
        seen.add(id(current))
        tb: TracebackType | None = BaseException.__dict__["__traceback__"].__get__(current)
        while tb is not None and examined_frames < _MAX_DIAGNOSTIC_FRAMES:
            examined_frames += 1
            code = tb.tb_frame.f_code
            next_tb = tb.tb_next
            admitted = False
            # Generated code may put str subclasses in header fields. Admit
            # them before pathlib or code hashing can dispatch user methods.
            safe_code = _code_structure_is_safe(code)
            path = Path(code.co_filename) if safe_code and len(code.co_filename) <= 4096 else None
            if path is not None and path.is_relative_to(_PACKAGE_ROOT):
                try:
                    try:
                        path = path.resolve()
                    except RuntimeError:
                        # Python 3.12 reports symlink loops as RuntimeError.
                        withheld += 1
                        tb = next_tb
                        continue
                    if path.is_relative_to(_PACKAGE_ROOT) and path.suffix == ".py":
                        if path not in sources and len(sources) < 4:
                            stat = path.stat()
                            fingerprint = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
                            sources[path] = _trusted_source_codes(path, fingerprint)
                        if path in sources and code in sources[path] and any(line == tb.tb_lineno for _, _, line in code.co_lines()):
                            frames.append(f"elspeth/{path.relative_to(_PACKAGE_ROOT).as_posix()}:{tb.tb_lineno}")
                            admitted = True
                except (OSError, ValueError):
                    pass  # Optional source coordinates unavailable; counted as withheld below.
            if not admitted:
                withheld += 1
            tb = next_tb
        if tb is not None:
            frames_truncated = True
        cause: BaseException | None = BaseException.__dict__["__cause__"].__get__(current)
        suppressed: bool = BaseException.__dict__["__suppress_context__"].__get__(current)
        context: BaseException | None = BaseException.__dict__["__context__"].__get__(current)
        current = cause if cause is not None else (None if suppressed else context)
    return _CleanupErrorStructure(tuple(causes), tuple(frames), current is not None, withheld, frames_truncated)


@contextmanager
def plugin_node_scope(ctx: PluginContext, node_id: str | None) -> Iterator[None]:
    """Temporarily scope lifecycle hooks to a plugin node."""
    previous_node_id = ctx.node_id
    ctx.node_id = node_id
    try:
        yield
    finally:
        ctx.node_id = previous_node_id


def plugin_lifecycle_node_id(
    plugin: SourceProtocol | TransformProtocol | SinkProtocol,
    *,
    plugin_name: str,
    hook_label: str,
) -> str | None:
    """Return the plugin node_id required for lifecycle attribution."""
    try:
        return plugin.node_id
    except AttributeError as exc:
        raise contract_errors.OrchestrationInvariantError(
            f"{hook_label} plugin {plugin_name!r} is missing required node_id attribute; "
            "lifecycle attribution requires SourceProtocol/TransformProtocol/SinkProtocol.node_id."
        ) from exc


def _safe_cleanup_error_text(error: Exception) -> tuple[str, str | None, int | None]:
    """Return public-safe text and raw-message evidence when obtainable.

    A formatter failure has no raw message to hash or measure. Its explicit
    marker describes that failure; it must not acquire fabricated raw-message
    provenance from the marker's own bytes.
    """
    try:
        raw_text = str(error)
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception as formatting_error:
        return f"<unrepresentable {type(error).__name__}: {type(formatting_error).__name__}>", None, None

    digest = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()[:16]
    public_text = scrub_text_for_audit(raw_text)
    if len(public_text) > _CLEANUP_ERROR_PREVIEW_CHARS:
        public_text = public_text[:_CLEANUP_ERROR_PREVIEW_CHARS] + "..."
    return public_text, digest, len(raw_text)


def cleanup_plugins(
    config: PipelineConfig,
    ctx: PluginContext,
    *,
    include_source: bool = True,
    pending_exc: BaseException | None,
    started_sources: Mapping[str, SourceProtocol] | None = None,
    started_transforms: Sequence[TransformProtocol] | None = None,
    started_sinks: Mapping[str, SinkProtocol] | None = None,
) -> None:
    """Clean up all plugins in the finally block.

    Implements the lifecycle teardown contract:
    1. on_complete(ctx) on all plugins (transforms, sinks, optionally source)
    2. close() on all plugins (source, transforms, sinks)

    on_complete() is called even on pipeline error -- it signals "processing
    is done" (success or failure), not "processing succeeded". close() is
    pure resource teardown and always follows on_complete().

    Each call is individually try/excepted so one plugin's failure does not
    prevent other plugins from cleaning up. All errors are collected and
    raised together after all cleanup completes — unless an exception is
    already propagating (this function runs in ``finally`` blocks), in which
    case the collected cleanup errors are logged and the in-flight exception
    is preserved as the primary outcome.

    Extracted from _execute_run() and _process_resumed_rows() to eliminate
    duplication of the finally-block cleanup pattern.

    Args:
        config: Pipeline configuration
        ctx: Plugin context
        include_source: If True (default), calls on_complete() and close()
            on the source. Set to False for resume path where source wasn't opened.
        pending_exc: Base exception that will propagate from the caller's cleanup
            boundary, if any. Ordinary cleanup failures are logged instead of
            masking it. Pass None when the boundary is returning normally.
        started_sources: Optional subset of sources whose on_start() completed.
            Defaults to all configured sources for steady-state run cleanup.
        started_transforms: Optional subset of transforms whose on_start()
            completed. Defaults to all configured transforms for steady-state
            run cleanup.
        started_sinks: Optional subset of sinks whose on_start() completed.
            Defaults to all configured sinks for steady-state run cleanup.

    Raises:
        RuntimeError: If any plugin cleanup hook fails and no exception is
            already propagating. When a pending exception exists, cleanup
            failures are logged instead so they never mask it.
    """
    logger = slog
    cleanup_errors: list[str] = []
    sources_for_cleanup = started_sources if started_sources is not None else config.sources
    transforms_for_cleanup = started_transforms if started_transforms is not None else config.transforms
    sinks_for_cleanup = started_sinks if started_sinks is not None else config.sinks
    if ctx.run_mode is RunMode.REPLAY:
        sources_for_cleanup = {}
    if ctx.run_mode is not RunMode.LIVE:
        sinks_for_cleanup = {}

    def record_cleanup_error(hook: str, plugin_name: str, node_id: str | None, error: Exception) -> None:
        public_error, error_digest, error_length = _safe_cleanup_error_text(error)
        structure = _cleanup_error_structure(error)
        logger.warning(
            "Plugin cleanup hook failed",
            run_id=ctx.run_id,
            hook=hook,
            plugin=plugin_name,
            node_id=node_id,
            error=public_error,
            error_type=type(error).__name__,
            error_sha256=error_digest,
            error_length=error_length,
            cause_types=structure.cause_types,
            frames=structure.frames,
            cause_chain_truncated=structure.cause_chain_truncated,
            frames_withheld=structure.frames_withheld,
            frames_truncated=structure.frames_truncated,
        )
        cleanup_errors.append(
            f"{hook}({plugin_name}): {type(error).__name__}: {public_error} "
            f"[node_id={node_id}, message_sha256={error_digest}, message_length={error_length}, "
            f"cause_types={structure.cause_types}, frames={structure.frames}, "
            f"cause_chain_truncated={structure.cause_chain_truncated}, frames_withheld={structure.frames_withheld}, "
            f"frames_truncated={structure.frames_truncated}]"
        )

    def run_hook(hook_label: str, plugin_name: str, node_id: str | None, fn: Callable[[], None]) -> None:
        # Plugin cleanup MUST attempt every hook even when one fails — broad
        # catch is required by the best-effort lifecycle contract documented
        # above. FrameworkBugError / AuditIntegrityError (TIER_1_ERRORS) signal
        # system-level corruption that must crash immediately, so they are
        # re-raised by the dedicated Tier-1 clause BEFORE the broad catch can
        # downgrade them to a cleanup warning. Everything else is recorded and
        # folded into the RuntimeError raised after all hooks finish.
        try:
            with plugin_node_scope(ctx, node_id):
                fn()
        except contract_errors.TIER_1_ERRORS:
            raise
        except Exception as exc:
            record_cleanup_error(hook_label, plugin_name, node_id, exc)

    # Call on_complete for all plugins (even on error).
    # Base classes provide no-op implementations, so no hasattr needed.
    # functools.partial preserves the bound-method type for mypy and avoids
    # the loop-variable closure trap that lambdas would otherwise need
    # default-argument workarounds for.
    for transform in transforms_for_cleanup:
        run_hook(
            "transform.on_complete",
            transform.name,
            plugin_lifecycle_node_id(transform, plugin_name=transform.name, hook_label="transform.on_complete"),
            partial(transform.on_complete, ctx),
        )
    for sink in sinks_for_cleanup.values():
        run_hook(
            "sink.on_complete",
            sink.name,
            plugin_lifecycle_node_id(sink, plugin_name=sink.name, hook_label="sink.on_complete"),
            partial(sink.on_complete, ctx),
        )
    if include_source:
        for source in sources_for_cleanup.values():
            run_hook(
                "source.on_complete",
                source.name,
                plugin_lifecycle_node_id(source, plugin_name=source.name, hook_label="source.on_complete"),
                partial(source.on_complete, ctx),
            )

    # Close source (if included) and all sinks
    if include_source:
        for source in sources_for_cleanup.values():
            run_hook(
                "source.close",
                source.name,
                plugin_lifecycle_node_id(source, plugin_name=source.name, hook_label="source.close"),
                source.close,
            )

    # Close all transforms (release resources - file handles, connections, etc.)
    for transform in transforms_for_cleanup:
        run_hook(
            "transform.close",
            transform.name,
            plugin_lifecycle_node_id(transform, plugin_name=transform.name, hook_label="transform.close"),
            transform.close,
        )

    # Close all sinks
    for sink in sinks_for_cleanup.values():
        run_hook(
            "sink.close",
            sink.name,
            plugin_lifecycle_node_id(sink, plugin_name=sink.name, hook_label="sink.close"),
            sink.close,
        )

    if cleanup_errors:
        error_summary = "; ".join(cleanup_errors)
        if pending_exc is not None:
            # An exception is already propagating through the caller's finally
            # block. Raising here would REPLACE it — e.g. swap a
            # _RunFailedWithPartialResultError (real partial counters, failed
            # ceremony) for a cleanup RuntimeError handled by the generic
            # ceremony. The per-hook failures are already logged above; record
            # the aggregate and let the original exception continue.
            logger.error(
                "Plugin cleanup failed during exception propagation; original error preserved",
                run_id=ctx.run_id,
                cleanup_errors=tuple(cleanup_errors),
                pending_error_type=type(pending_exc).__name__,
            )
            return
        raise RuntimeError(f"Plugin cleanup failed: {error_summary}")
