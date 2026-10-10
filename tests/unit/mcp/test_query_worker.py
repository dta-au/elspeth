"""Actual disposable-query controls; no live/provider or shared database access."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from elspeth.core.landscape.database import LandscapeDB
from elspeth.mcp import query_worker
from elspeth.mcp.analyzer import LandscapeAnalyzer
from elspeth.mcp.query_protocol import QueryBudgetExceeded, QueryWorkerError
from elspeth.mcp.query_worker import RawQueryWorker

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Qualified Linux worker memory enforcement")


@pytest.fixture(params=[None, "query-worker-test-passphrase"])
def audit_target(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[tuple[str, str | None]]:
    passphrase = request.param
    if passphrase is not None:
        pytest.importorskip("sqlcipher3")
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    LandscapeDB.from_url(url, passphrase=passphrase).close()
    yield url, passphrase


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql,params,limit",
    [
        ("SELECT :x AS value, '雪' AS unicode", {"x": 'quoted\n"'}, 100),
        ("WITH a AS (SELECT 1 AS n) SELECT a.n + b.n AS total FROM a JOIN a b ON a.n = b.n", None, 100),
        ("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x < 8) SELECT sum(x) AS total FROM n", None, 100),
        ("WITH n(x) AS (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3) SELECT x FROM n", None, 2),
    ],
)
async def test_exact_query_json_and_healthy_followup(
    audit_target: tuple[str, str | None], sql: str, params: dict | None, limit: int
) -> None:
    url, passphrase = audit_target
    analyzer = LandscapeAnalyzer(url, passphrase=passphrase)
    try:
        expected = json.dumps(analyzer.query(sql, params, limit), indent=2)
    finally:
        analyzer.close()
    worker = RawQueryWorker(url, passphrase)
    assert await worker.execute(sql, params, limit) == expected
    assert await worker.execute("SELECT 7 AS value", None, 1) == '[\n  {\n    "value": 7\n  }\n]'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql,category",
    [
        ("DELETE FROM runs", "validation"),
        ("SELECT 1 AS x, 2 AS x", "validation"),
        ("SELECT * FROM nonexistent", "database"),
        ("SELECT randomblob(4) AS value", "serialization"),
    ],
)
async def test_non_budget_errors_are_errors_without_sql_diagnostics(audit_target: tuple[str, str | None], sql: str, category: str) -> None:
    worker = RawQueryWorker(*audit_target)
    with pytest.raises(QueryWorkerError, match=category) as failure:
        await worker.execute(sql, None, 1)
    assert sql not in str(failure.value)
    assert "SELECT" not in str(failure.value)
    assert await worker.execute("SELECT 1 AS value", None, 1)


@pytest.mark.asyncio
async def test_giant_single_value_is_contained_and_worker_recovers(
    audit_target: tuple[str, str | None], monkeypatch: pytest.MonkeyPatch, process_pids: list[int]
) -> None:
    # The request asks for 512 MiB; the disposable process may map only 256 MiB.
    # Allocation fails before the driver can materialize the giant scalar.
    monkeypatch.setattr(query_worker, "WORKER_MEMORY_BYTES", 256 * 1024 * 1024)
    worker = RawQueryWorker(*audit_target)
    with pytest.raises(QueryBudgetExceeded, match="memory"):
        await worker.execute("SELECT hex(zeroblob(268435456)) AS value", None, 1)
    _assert_reaped(process_pids)
    assert await worker.execute("SELECT 1 AS value", None, 1)


@pytest.mark.asyncio
async def test_complete_oversized_output_is_refused_without_truncation(audit_target: tuple[str, str | None]) -> None:
    worker = RawQueryWorker(*audit_target)
    with pytest.raises(QueryBudgetExceeded, match="output"):
        await worker.execute("SELECT hex(zeroblob(5000000)) AS value", None, 1)
    assert await worker.execute("SELECT 1 AS value", None, 1)


HUNG_SQL = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) AS total FROM n"


@pytest.fixture
def process_pids(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    original = asyncio.create_subprocess_exec
    pids: list[int] = []

    async def record(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await original(*args, **kwargs)
        pids.append(process.pid)
        return process

    monkeypatch.setattr(query_worker.asyncio, "create_subprocess_exec", record)
    return pids


def _assert_reaped(pids: list[int]) -> None:
    assert pids
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.asyncio
async def test_deadline_reaps_query_and_allows_next_query(
    audit_target: tuple[str, str | None], monkeypatch: pytest.MonkeyPatch, process_pids: list[int]
) -> None:
    monkeypatch.setattr(query_worker, "QUERY_SECONDS", 2.5)
    monkeypatch.setattr(query_worker, "CLEANUP_SECONDS", 0.1)
    worker = RawQueryWorker(*audit_target)
    started = time.monotonic()
    with pytest.raises(QueryBudgetExceeded, match="wall-clock"):
        await worker.execute(HUNG_SQL, None, 1)
    assert time.monotonic() - started < 5
    _assert_reaped(process_pids)
    assert await worker.execute("SELECT 1 AS value", None, 1)


@pytest.mark.asyncio
async def test_cancellation_busy_refusal_and_event_loop_health(audit_target: tuple[str, str | None], process_pids: list[int]) -> None:
    worker = RawQueryWorker(*audit_target)
    task = asyncio.create_task(worker.execute(HUNG_SQL, None, 1))
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if process_pids:
                break
        assert process_pids, "the event loop must run while query startup/execution is active"
        with pytest.raises(QueryBudgetExceeded, match="busy"):
            await worker.execute("SELECT 1", None, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _assert_reaped(process_pids)
        assert await worker.execute("SELECT 1 AS value", None, 1)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_input_budget_and_unsupported_platform_fail_closed(
    audit_target: tuple[str, str | None], monkeypatch: pytest.MonkeyPatch, process_pids: list[int]
) -> None:
    worker = RawQueryWorker(*audit_target)
    with pytest.raises(QueryBudgetExceeded):
        await worker.execute("SELECT :x", {"x": "x" * (2 * 1024 * 1024)}, 1)
    assert process_pids == []
    monkeypatch.setattr(query_worker.sys, "platform", "win32")
    with pytest.raises(QueryBudgetExceeded, match="Linux"):
        await worker.execute("SELECT 1", None, 1)
    assert process_pids == []


@pytest.mark.asyncio
async def test_cancellation_drains_paused_response_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    real_create = asyncio.create_subprocess_exec
    real_read = asyncio.StreamReader.readexactly
    processes: list[asyncio.subprocess.Process] = []
    body_boundary = asyncio.Event()

    async def controlled_child(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await real_create(
            sys.executable,
            "-c",
            "import os,struct,time; os.write(1,b'S'+struct.pack('!I',1000000)); os.write(1,b'x'*100000); time.sleep(60)",
            **kwargs,
        )
        processes.append(process)
        return process

    async def pause_before_body(reader: asyncio.StreamReader, size: int) -> bytes:
        if size > 5:
            body_boundary.set()
            await asyncio.Event().wait()
        return await real_read(reader, size)

    monkeypatch.setattr(query_worker.asyncio, "create_subprocess_exec", controlled_child)
    monkeypatch.setattr(asyncio.StreamReader, "readexactly", pause_before_body)
    task = asyncio.create_task(RawQueryWorker("unused-controlled-worker").execute("SELECT 1", None, 1))
    try:
        await asyncio.wait_for(body_boundary.wait(), 5)
        assert processes[0].stdout is not None
        reader = processes[0].stdout
        for _ in range(100):
            if reader._paused:
                break
            await asyncio.sleep(0.01)
        assert reader._paused, "control must establish output backpressure before cancellation"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
        assert reader._transport.is_closing(), "reaping alone must not leave a paused response pipe open"
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        if processes and processes[0].stdout is not None:
            # Fallback is after assertions, so it cannot hide a descriptor leak.
            processes[0].stdout._transport.close()
