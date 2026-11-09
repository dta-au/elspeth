"""Worker callables for PoolRenderer tests (module-level so they pickle by reference).

Lives under ``elspeth.testing`` rather than ``tests/fixtures`` because these callables
are submitted to a spawned child process: the child's ``sys.path`` is copied from the
parent (multiprocessing/spawn.py), so the module must be importable through the
installed package, not through pytest's ``tests/`` rootdir insertion.
"""

from __future__ import annotations

import os
import time

from elspeth.plugins.infrastructure.rasterize.protocol import RasterizeOutcome, RasterizeRequest, RasterizeResponse


def sleeping_worker(request: RasterizeRequest) -> RasterizeOutcome:
    time.sleep(30)
    return RasterizeResponse(page_count=0, rendered=(), refused=())


def crashing_worker(request: RasterizeRequest) -> RasterizeOutcome:
    os._exit(3)


def raising_worker(request: RasterizeRequest) -> RasterizeOutcome:
    raise KeyError("worker bug")
