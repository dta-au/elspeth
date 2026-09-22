"""Close codes for the run-progress WebSocket, and what a client may do next.

The code is the ONLY part of a close a client may dispatch on. The reason
string beside it is operator-facing prose: unversioned, capped at 123 bytes,
and droppable by an intermediary, so a client keying recovery on the text keys
on something no test binds and no proxy guarantees.

The concrete gap this enumeration closes is the durable poller's backend arm.
``except (SQLAlchemyError, ConnectionError, OSError)`` closed with ONE reason,
"Run progress unavailable", for a set that mixes ``OperationalError`` (the
database was unreachable for a moment; reconnecting may well succeed) with
``ProgrammingError`` and ``IntegrityError`` (a defect in the query or the row
that fails identically on every future connection). No client could split that
arm by reading the reason, because both halves sent the same one. Splitting it
needs a second code (polling audit 2026-09-22, finding 1).

So the retry decision is carried by the code:

- ``BACKEND_UNAVAILABLE`` -- the run is fine, this server could not read it
  right now. The client reconnects with backoff, exactly as it does for an
  abnormal transport close.
- ``INTERNAL_ERROR`` -- an integrity or accounting failure, or any other
  first-party defect. Reconnecting would re-run the same failing read, so the
  client stops streaming and falls back to polling run status over REST.
- ``AUTH_FAILED`` / ``RUN_UNAVAILABLE`` -- terminal refusals; the client
  neither reconnects nor polls.

1006 is absent deliberately: the browser synthesises it when a connection dies
without a close frame, and no server ever sends it.

The 4xxx values mirror the HTTP status they correspond to (4001/401, 4004/404,
4503/503), which is the convention the first two already set.
``tests/unit/web/execution/test_run_stream_close_codes.py`` pins this
enumeration against the frontend's copy in ``api/websocket.ts``.
"""

from __future__ import annotations

from enum import IntEnum

from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyPoolTimeoutError


class RunStreamCloseCode(IntEnum):
    """Every close code the run-progress WebSocket may send."""

    NORMAL = 1000
    INTERNAL_ERROR = 1011
    AUTH_FAILED = 4001
    RUN_UNAVAILABLE = 4004
    BACKEND_UNAVAILABLE = 4503


#: The failures that may close ``BACKEND_UNAVAILABLE``. This is an ``except``
#: tuple, not a predicate, so the interpreter's own handler dispatch does the
#: classifying: no caller can consult it from the wrong place, and the narrow
#: arm must be written above the broad one or it never matches. That ordering
#: is pinned behaviourally by ``tests/unit/web/execution/test_websocket.py``,
#: because a shadowed arm is silent -- the code still compiles and still
#: closes, just with the wrong code.
#:
#: Misclassifying is not symmetric, which is why this set is deliberately
#: narrow. Calling a transient fault permanent costs only a fall back to the
#: REST recovery poll, which still reaches the run; calling a permanent fault
#: transient spins a client through the whole backoff ladder against something
#: no reconnect can clear. When in doubt, leave it out.
#:
#: ``OperationalError`` is already this application's "database is unavailable,
#: retry in a moment" marker -- ``app.py`` registers it as the HTTP 503
#: ``database_unavailable`` handler. Pool ``TimeoutError`` is exhaustion, which
#: is momentary by definition.
#:
#: Pool ``TimeoutError`` is the ONE deliberate divergence from that HTTP
#: handler, which leaves it a 500. The two layers offer different fallbacks: a
#: stream that refuses to reconnect drops into a 3s REST poll, so calling pool
#: exhaustion permanent would hit the exhausted pool roughly ten times more
#: often than a reconnect ladder capped at 30s. The cheaper answer for a
#: saturated pool is to back off and re-open.
#:
#: Deliberately NOT included: every other ``SQLAlchemyError``, per the
#: asymmetry above. Nor ``ConnectionError`` -- despite reading like a transport
#: blip, it is not a *backend* fault here. SQLAlchemy wraps driver socket
#: failures in ``OperationalError``, so a bare ``ConnectionError`` in these
#: handlers comes from writing to a client socket that has already gone, and a
#: close frame aimed at a departed client is delivered to nobody.
TRANSIENT_BACKEND_FAILURES: tuple[type[Exception], ...] = (
    OperationalError,
    SQLAlchemyPoolTimeoutError,
)


__all__ = ["TRANSIENT_BACKEND_FAILURES", "RunStreamCloseCode"]
