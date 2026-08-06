"""Shared commencement gate expression contract."""

from __future__ import annotations

from elspeth.core.expression_parser import ExpressionParser

# No "env": the namespace was never populated (the sole engine caller passed
# nothing) and was the surface of elspeth-83261b699c — "Commencement gate
# non-bool failure can echo env secret values into audit/error text".
COMMENCEMENT_GATE_ALLOWED_NAMES = ("collections", "dependency_runs")


def validate_commencement_gate_condition(condition: str) -> None:
    """Validate a commencement gate condition against the shared context contract."""
    ExpressionParser(condition, allowed_names=list(COMMENCEMENT_GATE_ALLOWED_NAMES))
