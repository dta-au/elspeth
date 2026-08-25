"""``PoolRenderer``: spawns a single-worker process pool per rasterize call site.

Owns process isolation, wall-clock timeout, and orphan-process cleanup for
``worker.rasterize_document``. Copies the timeout/orphan-kill sequence from
``plugins/transforms/rag/query.py:143-167`` (load-bearing: without it a timed-out
worker keeps burning CPU and the interpreter hangs at exit).
"""

from __future__ import annotations

import multiprocessing as mp
import shutil
import tempfile
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

from elspeth.plugins.infrastructure.rasterize.protocol import (
    DocumentRefusal,
    DocumentRefusalKind,
    RasterizeOutcome,
    RasterizeRequest,
    RasterizeResponse,
)
from elspeth.plugins.infrastructure.rasterize.worker import CpuBudgetExceeded, rasterize_document, worker_initializer


@dataclass(frozen=True, slots=True)
class RenderLimits:
    dpi: int
    max_pages: int
    max_page_pixels: int
    max_page_bytes: int
    render_timeout_seconds: int
    worker_memory_limit_bytes: int


@dataclass(frozen=True, slots=True)
class RenderTimedOut:
    timeout_seconds: int


RenderResult = RasterizeResponse | DocumentRefusal | RenderTimedOut


class PoolRenderer:
    """Runs ``rasterize_document`` (or a test double) in an isolated spawn-context worker.

    The pool is built lazily on first ``render`` and rebuilt whenever it is torn down
    (timeout, worker death). Each ``render`` call gets its own caller-owned temp output
    directory; the caller must call ``discard`` on it in a ``finally``.
    """

    def __init__(
        self,
        limits: RenderLimits,
        *,
        worker: Callable[[RasterizeRequest], RasterizeOutcome] = rasterize_document,
    ) -> None:
        self._limits = limits
        self._worker = worker
        self._pool: ProcessPoolExecutor | None = None

    def _ensure_pool(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(
                max_workers=1,
                mp_context=mp.get_context("spawn"),
                initializer=worker_initializer,
                initargs=(self._limits.worker_memory_limit_bytes, self._limits.render_timeout_seconds),
                max_tasks_per_child=1,
            )
        return self._pool

    def _drop_pool(self) -> None:
        self._pool = None

    def _shutdown_and_drop_pool(self) -> None:
        # BrokenProcessPool / generic-Exception paths: the pool is unusable but its
        # executor (and any worker process it still owns) is not torn down by simply
        # dropping our reference to it — shut it down explicitly so it does not leak.
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
        self._drop_pool()

    def _kill_pool(self, future: Future[RasterizeOutcome]) -> None:
        # Copied from query.py:143-159 verbatim: future.cancel() only prevents a QUEUED
        # task from starting — it cannot kill a worker already executing native code.
        # shutdown(wait=False) does not terminate a running worker either; the stuck
        # process stays alive as an orphan unless we explicitly kill it.
        future.cancel()
        assert self._pool is not None
        stuck_processes = list(self._pool._processes.values())
        self._pool.shutdown(wait=False, cancel_futures=True)
        for proc in stuck_processes:
            if proc.is_alive():
                proc.kill()
        self._drop_pool()

    def live_worker_pids(self) -> tuple[int, ...]:
        if self._pool is None:
            return ()
        return tuple(proc.pid for proc in self._pool._processes.values() if proc.is_alive() and proc.pid is not None)

    def render(
        self,
        pdf_bytes: bytes,
        *,
        on_submitted: Callable[[tuple[int, ...]], None] | None = None,
    ) -> tuple[RenderResult, Path | None]:
        """Rasterize ``pdf_bytes`` in the pool worker.

        Returns ``(result, output_dir)``. The CALLER owns ``output_dir`` and must call
        ``discard(output_dir)`` in a ``finally``.

        ``on_submitted`` is a keyword-only TEST SEAM: called with ``live_worker_pids()``
        immediately after the request is submitted, so tests can capture the pid of a
        worker that is about to be killed for a timeout before it disappears.
        """
        output_dir = Path(tempfile.mkdtemp(prefix="elspeth-rasterize-"))
        request = RasterizeRequest(
            pdf_bytes=pdf_bytes,
            dpi=self._limits.dpi,
            max_pages=self._limits.max_pages,
            max_page_pixels=self._limits.max_page_pixels,
            max_page_bytes=self._limits.max_page_bytes,
            output_dir=output_dir,
        )
        pool = self._ensure_pool()
        try:
            future = pool.submit(self._worker, request)
        except Exception:
            # submit() itself raised before handing back a future to await: no caller
            # will ever receive this output_dir to discard it, so it must not leak here.
            self.discard(output_dir)
            raise
        if on_submitted is not None:
            on_submitted(self.live_worker_pids())
        try:
            outcome = future.result(timeout=self._limits.render_timeout_seconds)
        except FuturesTimeoutError:
            self._kill_pool(future)
            return RenderTimedOut(timeout_seconds=self._limits.render_timeout_seconds), output_dir
        except BrokenProcessPool as exc:
            self._shutdown_and_drop_pool()
            raise RuntimeError(
                "pdf_rasterize worker died outside its result protocol — this is a code bug, not a document problem"
            ) from exc
        except CpuBudgetExceeded:
            # SIGXCPU fired inside the worker while it was between native calls: the document
            # consumed its CPU budget. Same row outcome as the wall clock; the worker is
            # recycled by max_tasks_per_child=1, so the pool itself stays usable.
            return RenderTimedOut(timeout_seconds=self._limits.render_timeout_seconds), output_dir
        except MemoryError:
            # RLIMIT_AS tripped OUTSIDE the per-page handler in worker.py (which already
            # catches MemoryError around a single page's render and returns a RefusedPage)
            # -- so this means the document PARSE itself blew the memory limit.
            return (
                DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail="document parse exceeded the worker memory limit"),
                output_dir,
            )
        except Exception as exc:  # narrow in effect: every typed worker exception is handled
            # above; anything else escaping the worker is OUR code's bug, not a document
            # problem, and must crash loudly rather than be swallowed.
            self._shutdown_and_drop_pool()
            raise RuntimeError(
                "pdf_rasterize worker raised outside its result protocol — this is a code bug, not a document problem"
            ) from exc
        return outcome, output_dir

    def discard(self, output_dir: Path | None) -> None:
        if output_dir is not None:
            shutil.rmtree(output_dir, ignore_errors=True)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None
