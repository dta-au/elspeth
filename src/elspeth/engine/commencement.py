"""Commencement gate evaluation — pre-flight go/no-go checks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elspeth.contracts.errors import CommencementGateFailedError
from elspeth.contracts.freeze import deep_freeze
from elspeth.contracts.preflight import CommencementGateResult
from elspeth.core.commencement_gate_expression import (
    COMMENCEMENT_GATE_ALLOWED_NAMES,
    redact_commencement_gate_condition,
    validate_commencement_gate_condition,
)
from elspeth.core.dependency_config import CommencementGateConfig
from elspeth.core.expression_parser import ExpressionParser, ExpressionSecurityError
from elspeth.engine.error_boundary import reraise_if_engine_crash_through


def build_preflight_context(
    *,
    dependency_results: dict[str, Any],
    collection_probes: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the pre-flight context dict for gate expression evaluation.

    Returns a namespace dict with two keys accessible in gate expressions:
    - ``dependency_runs``: {name: {run_id, settings_hash, duration_ms, indexed_at}} for each dependency
    - ``collections``: {name: {reachable, count}} for each probed collection

    There is deliberately no ``env`` namespace: it was never populated (the
    sole caller passed nothing) and it was the surface of
    elspeth-83261b699c — "Commencement gate non-bool failure can echo env
    secret values into audit/error text".
    """
    return {
        "dependency_runs": dependency_results,
        "collections": collection_probes,
    }


def _build_audit_snapshot(context: Mapping[str, Any]) -> Mapping[str, Any]:
    """Build a frozen context snapshot for audit."""
    frozen: Mapping[str, Any] = deep_freeze(
        {
            "dependency_runs": context["dependency_runs"],
            "collections": context["collections"],
        }
    )
    return frozen


def validate_gate_expressions(gates: list[CommencementGateConfig]) -> None:
    """Validate all gate expressions at config time, before any side effects.

    ExpressionParser validates syntax and security at construction time.
    Calling this before dependency resolution ensures malformed expressions
    are rejected before sub-pipelines run and mutate external state.

    Raises:
        ExpressionSecurityError: If expression contains forbidden constructs
        ExpressionSyntaxError: If expression is not valid Python syntax
    """
    for gate in gates:
        validate_commencement_gate_condition(gate.condition)


def evaluate_commencement_gates(
    gates: list[CommencementGateConfig],
    context: dict[str, Any],
) -> list[CommencementGateResult]:
    """Evaluate gates sequentially. Raises CommencementGateFailedError on failure.

    Context should be a namespace dict with keys from COMMENCEMENT_GATE_ALLOWED_NAMES
    (collections, dependency_runs). Unknown keys are not rejected here —
    the ExpressionParser restricts name access during evaluation.
    The entire context dict is deep-frozen before evaluation.
    """
    frozen_context = deep_freeze(context)
    # Build audit snapshot from the frozen context (same object used for evaluation) to ensure the snapshot reflects exactly what the gate saw.
    audit_snapshot = _build_audit_snapshot(frozen_context)

    results: list[CommencementGateResult] = []
    for gate in gates:
        audited_condition = redact_commencement_gate_condition(gate.condition)
        try:
            parser = ExpressionParser(
                gate.condition,
                allowed_names=list(COMMENCEMENT_GATE_ALLOWED_NAMES),
            )
            result = parser.evaluate(frozen_context)
            if not isinstance(result, bool):
                raise CommencementGateFailedError(
                    gate_name=gate.name,
                    condition=audited_condition,
                    reason=(
                        # NB: do NOT include {result!r} — for a gate like
                        # dependency_runs['x']['run_id'] the result IS the raw
                        # context value, and this reason bypasses the audit
                        # snapshot (elspeth-83261b699c, whose original surface
                        # was the now-removed env namespace). The type alone
                        # is the actionable diagnostic.
                        f"Gate expression returned {type(result).__name__}, "
                        f"not bool. Commencement gates must evaluate to True or False — "
                        f"use a comparison (e.g., '> 0', '== \"expected\"') instead of "
                        f"relying on Python truthiness."
                    ),
                    context_snapshot=audit_snapshot,
                )
            passed = result
        except CommencementGateFailedError:
            raise
        except ExpressionSecurityError:
            # Post-A1 the evaluator's fail-closed visit() raises this for a
            # validator-allowed-but-evaluator-uncovered node — a framework
            # bug, never a user's failing gate. Wrapping it below would
            # relabel it "your gate failed"; let it crash through instead.
            raise
        except BaseException as exc:
            reraise_if_engine_crash_through(exc)
            if not isinstance(exc, Exception):
                raise
            raise CommencementGateFailedError(
                gate_name=gate.name,
                condition=audited_condition,
                reason=f"Expression raised {type(exc).__name__}",
                context_snapshot=audit_snapshot,
            ) from exc

        if not passed:
            raise CommencementGateFailedError(
                gate_name=gate.name,
                condition=audited_condition,
                reason="Condition evaluated to falsy",
                context_snapshot=audit_snapshot,
            )

        results.append(
            CommencementGateResult(
                name=gate.name,
                condition=audited_condition,
                result=True,
                context_snapshot=audit_snapshot,
            )
        )
    return results
