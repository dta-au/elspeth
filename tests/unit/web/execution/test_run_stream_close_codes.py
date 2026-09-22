"""Parity pins for the run-progress WebSocket close-code contract.

The close code is the only part of a close the frontend may dispatch on (see
``web/execution/websocket_close.py`` for why the reason string cannot be), so
the enumeration has to hold across three places that no compiler connects:

- ``web/execution/websocket_close.RunStreamCloseCode`` -- the authority.
- ``web/execution/routes.py`` -- every ``websocket.close(code=...)`` call.
- ``web/frontend/src/api/websocket.ts`` -- the ``RUN_STREAM_CLOSE_CODE`` map
  and the ``onclose`` switch that decides reconnect vs REST fallback vs stop.

A code added on the server and not the client falls into the client's
``default`` arm and silently reconnects; a code the client stops handling
regresses to the same place with no test going red. The Python side is
measured from the live enum, the TS side is parsed with a Prettier-stable
regex, and ``test_frontend_close_codes_are_actually_parsed`` keeps an empty
match from passing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import elspeth
from elspeth.web.execution.websocket_close import TRANSIENT_BACKEND_FAILURES, RunStreamCloseCode

_FRONTEND_WEBSOCKET_PATH = Path(elspeth.__file__).parent / "web" / "frontend" / "src" / "api" / "websocket.ts"
_ROUTES_PATH = Path(elspeth.__file__).parent / "web" / "execution" / "routes.py"

# `export const RUN_STREAM_CLOSE_CODE = {` ... `} as const;`, then one
# `NAME: 1234,` record per line (Prettier-stable).
_TS_BLOCK_RE = re.compile(
    r"^export const RUN_STREAM_CLOSE_CODE = \{\n(?P<body>.*?)^\} as const;",
    re.MULTILINE | re.DOTALL,
)
_TS_MEMBER_RE = re.compile(r"^\s{2}(?P<name>[A-Z][A-Z_]*): (?P<value>\d+),$")
_TS_CASE_RE = re.compile(r"^\s*case RUN_STREAM_CLOSE_CODE\.(?P<name>[A-Z][A-Z_]*):$", re.MULTILINE)


def _ts_source() -> str:
    return _FRONTEND_WEBSOCKET_PATH.read_text(encoding="utf-8")


def _ts_close_codes() -> dict[str, int]:
    block = _TS_BLOCK_RE.search(_ts_source())
    if block is None:
        return {}
    members: dict[str, int] = {}
    for line in block.group("body").splitlines():
        member = _TS_MEMBER_RE.match(line)
        if member is not None:
            members[member.group("name")] = int(member.group("value"))
    return members


def _ts_handled_codes() -> set[str]:
    return {match.group("name") for match in _TS_CASE_RE.finditer(_ts_source())}


def test_frontend_close_codes_are_actually_parsed() -> None:
    """Smoke: the anchor path resolves and both regexes matched real records.

    Without this, a moved file or a renamed constant turns every parity
    assertion below into a comparison against an empty set, which passes.
    """
    assert _FRONTEND_WEBSOCKET_PATH.is_file(), f"expected the frontend transport at {_FRONTEND_WEBSOCKET_PATH}"
    assert len(_ts_close_codes()) == len(RunStreamCloseCode)
    assert len(_ts_handled_codes()) == len(RunStreamCloseCode)


def test_frontend_close_code_map_matches_the_enumeration() -> None:
    """Every code the server can send is spelled identically on the client."""
    assert _ts_close_codes() == {member.name: member.value for member in RunStreamCloseCode}


def test_every_close_code_has_a_named_client_arm() -> None:
    """A code with no ``case`` falls into ``default``, which reconnects.

    That default is why the map alone is not enough to pin: a server code the
    client forgot is absorbed silently and looks exactly like a deliberate
    decision to reconnect. Each one has to name itself.
    """
    unhandled = {member.name for member in RunStreamCloseCode} - _ts_handled_codes()
    assert unhandled == set(), f"frontend switch has no arm for {sorted(unhandled)}"


def test_routes_close_the_stream_only_through_the_enumeration() -> None:
    """No ``websocket.close(code=<int>)`` literal may reappear in the routes.

    Parsed with ``ast`` rather than a regex for two reasons: a regex that
    stops matching reports the same clean result as a file with no literals
    left, and the formatter wraps the longer calls across lines, which a
    line-oriented pattern would miss entirely.
    """
    tree = ast.parse(_ROUTES_PATH.read_text(encoding="utf-8"))
    literal_sites: list[int] = []
    close_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "close":
            continue
        for keyword in node.keywords:
            if keyword.arg != "code":
                continue
            close_calls += 1
            if isinstance(keyword.value, ast.Constant):
                literal_sites.append(node.lineno)
    assert close_calls > 0, "found no websocket.close(code=...) calls to check — the instrument is broken"
    assert literal_sites == [], f"close-code literals at routes.py lines {sorted(literal_sites)}"


def test_transient_tuple_admits_only_what_a_reconnect_can_fix() -> None:
    """The membership that alone may close BACKEND_UNAVAILABLE.

    The negative cases are the point: a broken query or a corrupt row fails
    the same way on the next connection, so offering them as retryable spins
    the client through the whole backoff ladder for nothing. Misclassifying
    the other way costs only a fall back to the REST recovery poll, which is
    why the admitted set stays this narrow.

    Membership is asserted with ``issubclass`` because the tuple is consumed
    as an ``except`` clause, and that is the test the interpreter itself runs.
    """
    from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
    from sqlalchemy.exc import TimeoutError as SQLAlchemyPoolTimeoutError

    assert issubclass(OperationalError, TRANSIENT_BACKEND_FAILURES)
    assert issubclass(SQLAlchemyPoolTimeoutError, TRANSIENT_BACKEND_FAILURES)

    assert not issubclass(ProgrammingError, TRANSIENT_BACKEND_FAILURES)
    assert not issubclass(IntegrityError, TRANSIENT_BACKEND_FAILURES)
    # A bare ConnectionError here is a dead CLIENT socket, not a backend
    # outage: SQLAlchemy wraps driver socket failures in OperationalError.
    assert not issubclass(ConnectionResetError, TRANSIENT_BACKEND_FAILURES)
    assert not issubclass(OSError, TRANSIENT_BACKEND_FAILURES)
    assert not issubclass(ValueError, TRANSIENT_BACKEND_FAILURES)
