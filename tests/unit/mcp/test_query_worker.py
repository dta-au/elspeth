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
from mcp.server import Server
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent

from elspeth.core.landscape.database import LandscapeDB
from elspeth.mcp import query_worker
from elspeth.mcp.analyzer import LandscapeAnalyzer
from elspeth.mcp.query_protocol import QueryBudgetExceeded, QueryWorkerError
from elspeth.mcp.query_worker import RawQueryWorker
from elspeth.mcp.server import create_server

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


async def _call(server: Server, name: str, arguments: dict) -> CallToolResult:
    response = await server.request_handlers[CallToolRequest](
        CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
    )
    assert isinstance(response.root, CallToolResult)
    return response.root


def _text(result: CallToolResult) -> str:
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    return result.content[0].text


@pytest.mark.asyncio
async def test_mcp_dispatch_uses_worker_and_preserves_exact_json(
    audit_target: tuple[str, str | None], monkeypatch: pytest.MonkeyPatch, process_pids: list[int]
) -> None:
    url, passphrase = audit_target
    server = create_server(url, passphrase=passphrase)

    def forbidden_parent_query(*args: object, **kwargs: object) -> object:
        pytest.fail("MCP must not execute raw SQL in the parent analyzer")

    monkeypatch.setattr(LandscapeAnalyzer, "query", forbidden_parent_query)
    result = await _call(server, "query", {"sql": "SELECT :x AS value", "params": {"x": '雪\n"'}})
    assert not result.isError
    assert _text(result) == json.dumps([{"value": '雪\n"'}], indent=2)
    _assert_reaped(process_pids)


@pytest.mark.asyncio
async def test_mcp_giant_field_refusal_and_recovery(
    audit_target: tuple[str, str | None], monkeypatch: pytest.MonkeyPatch, process_pids: list[int]
) -> None:
    monkeypatch.setattr(query_worker, "WORKER_MEMORY_BYTES", 256 * 1024 * 1024)
    server = create_server(audit_target[0], passphrase=audit_target[1])
    result = await _call(server, "query", {"sql": "SELECT hex(zeroblob(268435456)) AS value"})
    assert result.isError
    assert "memory budget" in _text(result)
    _assert_reaped(process_pids)
    followup = await _call(server, "query", {"sql": "SELECT 1 AS value"})
    assert not followup.isError
    assert _text(followup) == json.dumps([{"value": 1}], indent=2)


@pytest.mark.asyncio
async def test_mcp_status_and_busy_refusal_during_query(audit_target: tuple[str, str | None], process_pids: list[int]) -> None:
    server = create_server(audit_target[0], passphrase=audit_target[1])
    task = asyncio.create_task(_call(server, "query", {"sql": HUNG_SQL}))
    try:
        for _ in range(100):
            if process_pids:
                break
            await asyncio.sleep(0.01)
        assert process_pids
        status = await asyncio.wait_for(_call(server, "get_mcp_status", {}), 1)
        assert not status.isError
        busy = await asyncio.wait_for(_call(server, "query", {"sql": "SELECT 1"}), 1)
        assert busy.isError and "busy" in _text(busy)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _assert_reaped(process_pids)
        assert not (await _call(server, "query", {"sql": "SELECT 1"})).isError
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", ["DELETE FROM runs", "SELECT * FROM nonexistent", "SELECT 1 AS x, 2 AS x", "SELECT randomblob(4)"])
async def test_mcp_non_budget_failures_remain_errors(audit_target: tuple[str, str | None], sql: str) -> None:
    server = create_server(audit_target[0], passphrase=audit_target[1])
    result = await _call(server, "query", {"sql": sql})
    assert result.isError
    assert "isolated worker" in _text(result)
    assert sql not in _text(result)


@pytest.mark.asyncio
async def test_mcp_input_budget_refuses_before_spawn(audit_target: tuple[str, str | None], process_pids: list[int]) -> None:
    server = create_server(audit_target[0], passphrase=audit_target[1])
    result = await _call(server, "query", {"sql": "SELECT :x", "params": {"x": "x" * (2 * 1024 * 1024)}})
    assert result.isError
    assert process_pids == []


@pytest.mark.asyncio
async def test_deadline_reaps_query_and_allows_next_query(
    audit_target: tuple[str, str | None], monkeypatch: pytest.MonkeyPatch, process_pids: list[int]
) -> None:
    normal_query_seconds = query_worker.QUERY_SECONDS
    monkeypatch.setattr(query_worker, "QUERY_SECONDS", 2.5)
    monkeypatch.setattr(query_worker, "CLEANUP_SECONDS", 0.1)
    worker = RawQueryWorker(*audit_target)
    started = time.monotonic()
    with pytest.raises(QueryBudgetExceeded, match="wall-clock"):
        await worker.execute(HUNG_SQL, None, 1)
    assert time.monotonic() - started < 5
    _assert_reaped(process_pids)
    # The shortened deadline controls the hung request. A fresh encrypted
    # worker also pays process/import/key setup, so use its normal allowance
    # when checking that reaping released admission for the healthy follow-up.
    monkeypatch.setattr(query_worker, "QUERY_SECONDS", normal_query_seconds)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["caller-cancel", "parent-deadline", "unknown-handle"])
async def test_pre_handle_startup_remains_owned(audit_target: tuple[str, str | None], monkeypatch: pytest.MonkeyPatch, ending: str) -> None:
    real_create = asyncio.create_subprocess_exec
    real_cleanup = RawQueryWorker._cleanup_owned
    created = asyncio.Event()
    release_handle = asyncio.Event()
    cleaning = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    startup_cancelled: list[bool] = []

    async def delayed_handle(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        if processes:
            return await real_create(*args, **kwargs)
        process = await real_create(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        created.set()
        try:
            await release_handle.wait()
        except asyncio.CancelledError:
            startup_cancelled.append(True)
            raise
        return process

    async def observe_cleanup(
        worker: RawQueryWorker, process: asyncio.subprocess.Process | None, spawn: asyncio.Task[asyncio.subprocess.Process]
    ) -> None:
        cleaning.set()
        await real_cleanup(worker, process, spawn)

    monkeypatch.setattr(query_worker.asyncio, "create_subprocess_exec", delayed_handle)
    monkeypatch.setattr(RawQueryWorker, "_cleanup_owned", observe_cleanup)
    monkeypatch.setattr(query_worker, "CLEANUP_SECONDS", 0.1)
    monkeypatch.setattr(query_worker, "QUERY_SECONDS", 0.4 if ending != "caller-cancel" else 10.0)
    worker = RawQueryWorker(*audit_target)
    task = asyncio.create_task(worker.execute("SELECT 1", None, 1))
    try:
        await asyncio.wait_for(created.wait(), 5)
        assert processes[0].returncode is None
        if ending == "caller-cancel":
            task.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        with pytest.raises(QueryBudgetExceeded, match="busy"):
            await worker.execute("SELECT 1", None, 1)
        if ending == "unknown-handle":
            with pytest.raises(QueryWorkerError, match="startup could not be cleaned up"):
                await asyncio.wait_for(task, 2)
            assert processes[0].returncode is None
            assert startup_cancelled == []
            with pytest.raises(QueryBudgetExceeded, match="busy"):
                await worker.execute("SELECT 1", None, 1)
            assert worker._pending_cleanup is not None
            release_handle.set()
            await asyncio.wait_for(worker._pending_cleanup, 2)
        else:
            if ending == "caller-cancel":
                task.cancel()  # Repeated cancellation must not cancel startup.
                await asyncio.sleep(0)
                task.cancel()
            release_handle.set()
            expected = asyncio.CancelledError if ending == "caller-cancel" else QueryBudgetExceeded
            with pytest.raises(expected):
                await asyncio.wait_for(task, 2)
        assert startup_cancelled == []
        assert processes[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
        # Restore a normal worker target to prove admission was released only
        # after the controlled child was actually reaped.
        monkeypatch.setattr(query_worker.asyncio, "create_subprocess_exec", real_create)
        monkeypatch.setattr(query_worker, "QUERY_SECONDS", 30.0)
        assert json.loads(await worker.execute("SELECT 1 AS value", None, 1)) == [{"value": 1}]
    finally:
        release_handle.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        # Fallback is deliberately after the ownership/reap assertions.
        if processes and processes[0].returncode is None:
            processes[0].kill()
            await processes[0].wait()
        if worker._pending_cleanup is not None and not worker._pending_cleanup.done():
            await worker._pending_cleanup
