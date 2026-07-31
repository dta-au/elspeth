"""Bind ELSPETH's trust-boundary decorator to Wardline's trust grammar.

ELSPETH already marks Tier-3 validation boundaries with
``elspeth.contracts.trust_boundary.trust_boundary``. Wardline deliberately
recognizes only its built-in marker modules unless an operator-authorized pack
maps another vocabulary. This pack supplies that mapping without importing or
executing ELSPETH application code.

Wardline 1.5.0's pack API cannot branch on ELSPETH's literal
``non_raising=True`` keyword: a pack seed receives only parsed ``TaintState``
level arguments, not the decorator AST. The canonical gate therefore reports
Wardline's active boundary-integrity findings for those marker forms rather
than hiding them with a severity override or suppression.
"""

from __future__ import annotations

from collections.abc import Mapping

from wardline.core.taints import TaintState
from wardline.scanner.grammar import BoundaryType, TrustGrammar
from wardline.scanner.taint.provider import FunctionTaint


def _seed_elspeth_boundary(_levels: Mapping[str, TaintState]) -> FunctionTaint:
    """Model validated output produced from raw external boundary input."""
    return FunctionTaint(TaintState.EXTERNAL_RAW, TaintState.ASSURED)


ELSPETH_TRUST_BOUNDARY = BoundaryType(
    canonical_name="trust_boundary",
    module_prefix="elspeth.contracts.trust_boundary",
    group=1,
    # ELSPETH's tier/source/source_param/invariant kwargs are metadata, not
    # Wardline trust levels, so none of them are read as a level argument.
    level_args=(),
    seed=_seed_elspeth_boundary,
    builtin=False,
)

# Wardline's pack loader extends the built-in grammar with this custom slice.
grammar = TrustGrammar(boundary_types=(ELSPETH_TRUST_BOUNDARY,), rules=())
