"""Disposable Linux raw-query process and asynchronous, bounded MCP supervisor.

Only stdlib and the lightweight wire protocol are imported before child memory
enforcement. Database credentials travel in the private capped stdin frame.
No worker output, traceback or arbitrary diagnostic is logged by the parent.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import struct
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO

from elspeth.mcp.query_protocol import (
    CLEANUP_SECONDS,
    HEADER_BYTES,
    QUERY_SECONDS,
    REQUEST_BYTES,
    RESULT_BYTES,
    WORKER_MEMORY_BYTES,
    FrameKind,
    QueryBudgetExceeded,
    QueryWorkerError,
    decode_header,
    encode_frame,
    encode_json,
)

if TYPE_CHECKING:
    from pydantic import JsonValue


@dataclass(slots=True)
class QueryRequest:
    """Child-owned parsed request; JSON parameters intentionally remain mutable."""

    database_url: str
    passphrase: str | None
    sql: str
    params: dict[str, JsonValue] | None
    limit: int


class RawQueryWorker:
    """One admitted worker per server, with immediate refusal instead of a queue."""

    def __init__(self, database_url: str, passphrase: str | None = None) -> None:
        self._database_url = database_url
        self._passphrase = passphrase
        self._busy = False

    async def execute(self, sql: str, params: dict[str, JsonValue] | None, limit: int) -> str:
        if sys.platform != "linux":
            raise QueryBudgetExceeded("Raw MCP SQL requires qualified Linux memory enforcement")
        if self._busy:
            raise QueryBudgetExceeded("Raw MCP SQL worker is busy; retry after the current query completes")
        self._busy = True
        process: asyncio.subprocess.Process | None = None
        try:
            # Reserve both cancellation and kill/reap grace within the total
            # budget, rather than adding cleanup after a full query deadline.
            async with asyncio.timeout(QUERY_SECONDS - 2 * CLEANUP_SECONDS):
                request = encode_json(
                    {"database_url": self._database_url, "passphrase": self._passphrase, "sql": sql, "params": params, "limit": limit},
                    REQUEST_BYTES,
                )
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "elspeth.mcp.query_worker",
                    str(WORKER_MEMORY_BYTES),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=8192,
                    env={
                        **os.environ,
                        # Core imports NumPy. Native thread stacks must fit
                        # inside this worker's finite address space as well.
                        "OPENBLAS_NUM_THREADS": "1",
                        "OMP_NUM_THREADS": "1",
                        "MKL_NUM_THREADS": "1",
                        "NUMEXPR_NUM_THREADS": "1",
                    },
                )
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write(struct.pack("!I", len(request)) + request)
                await process.stdin.drain()
                kind, length = decode_header(await process.stdout.readexactly(HEADER_BYTES))
                payload = await process.stdout.readexactly(length)
                if await process.stdout.read(1):
                    raise QueryWorkerError("Unexpected trailing raw query worker output")
                if await process.wait() != 0:
                    raise QueryWorkerError("Raw query worker exited unsuccessfully")
                if kind is FrameKind.SUCCESS:
                    return payload.decode("utf-8")
                if kind in (FrameKind.MEMORY, FrameKind.OUTPUT):
                    raise QueryBudgetExceeded(f"Raw query exceeded its {kind.name.lower()} budget")
                # Do not turn corruption, SQL errors or bugs into successful text
                # or misclassify unknown process death as a resource refusal.
                raise QueryWorkerError(f"Raw query failed in the isolated worker ({kind.name.lower()})")
        except TimeoutError as exc:
            raise QueryBudgetExceeded("Raw query exceeded its wall-clock budget") from exc
        except (asyncio.IncompleteReadError, UnicodeDecodeError, BrokenPipeError, ConnectionResetError) as exc:
            raise QueryWorkerError("Raw query worker failed its bounded response protocol") from exc
        finally:
            if process is not None:
                cleanup = asyncio.create_task(self._cleanup(process))
                cancelled = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                # An unreaped worker never frees its admission slot.
                cleanup.result()
                self._busy = False
                if cancelled:
                    raise asyncio.CancelledError
            else:
                self._busy = False

    @staticmethod
    async def _cleanup(process: asyncio.subprocess.Process) -> None:
        async def discard_stdout() -> None:
            if process.stdout is not None:
                while await process.stdout.read(8192):
                    pass

        # Reaping does not close a paused stdout transport. Resume/drain it
        # concurrently, retaining only one bounded chunk until actual EOF.
        discard = asyncio.create_task(discard_stdout())
        deadline = asyncio.get_running_loop().time() + 2 * CLEANUP_SECONDS
        try:
            if process.returncode is None:
                if process.stdin is not None:
                    try:
                        process.stdin.write(b"C")
                        process.stdin.close()
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # Dead worker: still wait/reap it below.
                try:
                    await asyncio.wait_for(process.wait(), CLEANUP_SECONDS)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
            try:
                await asyncio.wait_for(process.wait(), max(0, deadline - asyncio.get_running_loop().time()))
                await asyncio.wait_for(discard, max(0, deadline - asyncio.get_running_loop().time()))
            except TimeoutError as exc:
                raise QueryWorkerError("Raw query worker or response pipe could not be cleaned up") from exc
        finally:
            if not discard.done():
                discard.cancel()
                with suppress(asyncio.CancelledError):
                    await discard
            if process.stdin is not None:
                process.stdin.close()


def _read_request(stream: BinaryIO) -> QueryRequest:
    header = stream.read(4)
    if len(header) != 4:
        raise QueryWorkerError("Incomplete query request")
    length = struct.unpack("!I", header)[0]
    if length > REQUEST_BYTES:
        raise QueryBudgetExceeded("Query request exceeds its byte budget")
    payload = stream.read(length)
    if len(payload) != length:
        raise QueryWorkerError("Incomplete query request body")
    request: object = json.loads(payload)
    if not isinstance(request, dict) or set(request) != {"database_url", "passphrase", "sql", "params", "limit"}:
        raise QueryWorkerError("Invalid query request shape")
    if not isinstance(request["database_url"], str) or not isinstance(request["sql"], str):
        raise QueryWorkerError("Invalid query request strings")
    if request["passphrase"] is not None and not isinstance(request["passphrase"], str):
        raise QueryWorkerError("Invalid query request passphrase")
    if request["params"] is not None and not isinstance(request["params"], dict):
        raise QueryWorkerError("Invalid query request parameters")
    if type(request["limit"]) is not int:
        raise QueryWorkerError("Invalid query request limit")
    return QueryRequest(request["database_url"], request["passphrase"], request["sql"], request["params"], request["limit"])


def _run_query(stream: BinaryIO) -> bytes:
    # Deferred until after RLIMIT_AS, including parsing and driver imports.
    from sqlalchemy.exc import SQLAlchemyError

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.core.landscape.database import LandscapeDB, SchemaCompatibilityError
    from elspeth.core.landscape.factory import RecorderFactory
    from elspeth.mcp.analyzers.queries import query
    from elspeth.mcp.query_control import QueryControl
    from elspeth.mcp.sql_guard import MCPQueryPrivilegeError

    native_error: type[Exception] = sqlite3.Error
    try:
        request = _read_request(stream)
        if request.passphrase is not None:
            import sqlcipher3

            native_error = sqlcipher3.Error
        control = QueryControl(stream.fileno())
        db = LandscapeDB.from_url(request.database_url, passphrase=request.passphrase, create_tables=False, read_only=True)
        try:
            result = query(db, RecorderFactory.read_only(db), request.sql, request.params, request.limit, control=control)
            return encode_frame(FrameKind.SUCCESS, encode_json(result, RESULT_BYTES))
        finally:
            db.close()
    except MemoryError:
        return encode_frame(FrameKind.MEMORY)
    except QueryBudgetExceeded:
        return encode_frame(FrameKind.OUTPUT)
    except AuditIntegrityError:
        return encode_frame(FrameKind.INTEGRITY)
    except MCPQueryPrivilegeError:
        return encode_frame(FrameKind.PRIVILEGE)
    except (SQLAlchemyError, SchemaCompatibilityError, native_error):
        return encode_frame(FrameKind.DATABASE)
    except ValueError:
        return encode_frame(FrameKind.VALIDATION)
    except TypeError:
        return encode_frame(FrameKind.SERIALIZATION)


def worker_main() -> int:
    if sys.platform != "linux":
        return 1
    # Keep a private protocol descriptor; silence incidental prints and native
    # driver diagnostics. No SQL, password or traceback crosses stderr/stdout.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    with open(os.devnull, "wb") as discard:
        os.dup2(discard.fileno(), sys.stdout.fileno())
        os.dup2(discard.fileno(), sys.stderr.fileno())
    try:
        import resource

        memory = int(sys.argv[1])
        if not 0 < memory <= WORKER_MEMORY_BYTES:
            return 1
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CPU, (30, 31))
        try:
            response = _run_query(sys.stdin.buffer)
        except MemoryError:
            response = encode_frame(FrameKind.MEMORY)
        except Exception:
            # Unknown child failures stay protocol errors, with no external
            # diagnostic payload and no false resource-limit classification.
            response = encode_frame(FrameKind.INTERNAL)
        output.write(response)
        output.flush()
        return 0
    finally:
        output.close()


if __name__ == "__main__":
    raise SystemExit(worker_main())
