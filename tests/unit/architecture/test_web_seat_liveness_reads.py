"""Web code decides a run leader's liveness only through ``live_leader``.

``RunCoordinationRepository.seat_holder`` returns the incumbent of a lapsed seat
too, so ``seat_holder(...) is not None`` would treat a dead owner as live. Web
code has no use for the incumbent's identity, so it may not reference
``seat_holder`` at all; ``live_leader`` returns a ``LiveLeader`` only for a live
seat, and its type carries no liveness flag to forget.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_WEB = _ROOT / "src" / "elspeth" / "web"


def _attribute_references(source: str, name: str) -> list[int]:
    """Line numbers of every ``<expr>.<name>`` in ``source`` (calls, references and assignments)."""
    return [node.lineno for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute) and node.attr == name]


def _web_references(name: str) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(_WEB.rglob("*.py")):
        lines = _attribute_references(path.read_text(encoding="utf-8"), name)
        if lines:
            found[str(path.relative_to(_ROOT))] = lines
    return found


def test_web_never_reads_the_seat_holder() -> None:
    assert _web_references("seat_holder") == {}, "web code must decide leader liveness with live_leader(), not seat_holder()"


def test_scan_sees_the_web_liveness_reads() -> None:
    # Known positive: the recovery observation and the recovery-refusal handler.
    found = _web_references("live_leader")
    assert "src/elspeth/web/execution/recovery.py" in found
    assert "src/elspeth/web/execution/service.py" in found


def test_scanner_flags_a_seat_holder_liveness_read() -> None:
    flagged = "live = repositories.run_coordination.seat_holder(run_id=run_id) is not None\n"
    method_reference = "read = repositories.run_coordination.seat_holder\n"
    clean = "live = repositories.run_coordination.live_leader(run_id=run_id) is not None\n"
    assert _attribute_references(flagged, "seat_holder") == [1]
    assert _attribute_references(method_reference, "seat_holder") == [1]
    assert _attribute_references(clean, "seat_holder") == []
