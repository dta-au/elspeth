"""Web code decides a run leader's liveness only through ``live_leader``.

``RunCoordinationRepository.seat_holder`` returns the incumbent of a lapsed seat
too, so ``seat_holder(...) is not None`` would treat a dead owner as live. Web
code has no use for the incumbent's identity, so it may not name the other
routes to the seat: ``seat_holder``, the coordination table, its expiry column,
or a ``seat_live`` flag. ``live_leader`` returns a ``LiveLeader`` only for a
live seat, and its type carries no liveness flag to forget.

The scan matches those exact identifiers in the AST (attribute names, bare names
and imported names), not source text. It does not see string-keyed routes:
``getattr(repo, "seat_holder")`` is left to the tree-wide dynamic-attribute
gate, and a table looked up by name (``metadata.tables["run_coordination"]``)
or raw SQL is not covered here.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_WEB = _ROOT / "src" / "elspeth" / "web"

# Every way to reach seat liveness other than ``live_leader``.
_SEAT_LIVENESS_IDENTIFIERS = frozenset({"seat_holder", "run_coordination_table", "leader_heartbeat_expires_at", "seat_live"})


def _identifier_references(source: str, names: frozenset[str]) -> list[tuple[int, str]]:
    """``(line, identifier)`` for every attribute, bare name or imported name in ``names``."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in names:
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.Name) and node.id in names:
            found.append((node.lineno, node.id))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found.extend((node.lineno, alias.name) for alias in node.names if alias.name in names)
    return sorted(found)


def _web_references(names: frozenset[str]) -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(_WEB.rglob("*.py")):
        references = _identifier_references(path.read_text(encoding="utf-8"), names)
        if references:
            found[str(path.relative_to(_ROOT))] = references
    return found


def test_web_reaches_seat_liveness_only_through_live_leader() -> None:
    assert _web_references(_SEAT_LIVENESS_IDENTIFIERS) == {}, "web code must decide leader liveness with live_leader() only"


def _live_leader_calls(source: str) -> int:
    """Number of ``<expr>.live_leader(...)`` calls; a field or attribute named ``live_leader`` is not a call."""
    return sum(
        1
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "live_leader"
    )


def test_web_liveness_reads_are_the_reviewed_live_leader_calls() -> None:
    # Known positive, pinned per file: the two recovery observations (RUNNING;
    # FAILED/INTERRUPTED) and the recovery-refusal handler. A new web liveness
    # read is a reviewed addition here.
    calls: dict[str, int] = {}
    for path in sorted(_WEB.rglob("*.py")):
        count = _live_leader_calls(path.read_text(encoding="utf-8"))
        if count:
            calls[str(path.relative_to(_ROOT))] = count
    assert calls == {"src/elspeth/web/execution/recovery.py": 2, "src/elspeth/web/execution/service.py": 1}


def test_call_count_ignores_a_live_leader_field() -> None:
    assert _live_leader_calls("observation.live_leader\nlive_leader: bool\n") == 0
    assert _live_leader_calls("repositories.run_coordination.live_leader(run_id=run_id) is not None\n") == 1


def test_scanner_flags_every_other_route_to_seat_liveness() -> None:
    flagged = {
        "live = repositories.run_coordination.seat_holder(run_id=run_id) is not None\n": "seat_holder",
        "read = repositories.run_coordination.seat_holder\n": "seat_holder",
        "from elspeth.core.landscape.schema import run_coordination_table\n": "run_coordination_table",
        "import elspeth.core.landscape.schema as schema\nquery = schema.run_coordination_table\n": "run_coordination_table",
        "expired = row.leader_heartbeat_expires_at < now\n": "leader_heartbeat_expires_at",
        "live = holder.seat_live\n": "seat_live",
        "seat_live = True\n": "seat_live",
    }
    for source, identifier in flagged.items():
        assert [name for _, name in _identifier_references(source, _SEAT_LIVENESS_IDENTIFIERS)] == [identifier], source
    clean = "live = repositories.run_coordination.live_leader(run_id=run_id) is not None\n"
    assert _identifier_references(clean, _SEAT_LIVENESS_IDENTIFIERS) == []
