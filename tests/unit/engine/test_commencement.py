"""Tests for commencement gate evaluation."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from elspeth.contracts.errors import CommencementGateFailedError, GracefulShutdownError
from elspeth.core.dependency_config import CommencementGateConfig
from elspeth.engine.commencement import (
    build_preflight_context,
    evaluate_commencement_gates,
)


class TestEvaluateCommencementGates:
    def test_passing_gate(self) -> None:
        gates = [
            CommencementGateConfig(
                name="ready",
                condition="collections['test']['count'] > 0",
            )
        ]
        context = {
            "dependency_runs": {},
            "collections": {"test": {"count": 10, "reachable": True}},
        }
        results = evaluate_commencement_gates(gates, context)
        assert len(results) == 1
        assert results[0].result is True
        assert results[0].name == "ready"

    def test_failing_gate_raises(self) -> None:
        gates = [
            CommencementGateConfig(
                name="ready",
                condition="collections['test']['count'] > 0",
            )
        ]
        context = {
            "dependency_runs": {},
            "collections": {"test": {"count": 0, "reachable": False}},
        }
        with pytest.raises(CommencementGateFailedError, match="ready"):
            evaluate_commencement_gates(gates, context)

    def test_failing_gate_redacts_non_key_string_literals(self) -> None:
        sensitive_literal = "literal-sensitive-value-9f3a"
        gates = [
            CommencementGateConfig(
                name="literal_check",
                condition=f"collections['orders']['count'] == '{sensitive_literal}'",
            )
        ]
        context = {
            "dependency_runs": {},
            "collections": {"orders": {"count": 0, "reachable": True}},
        }

        with pytest.raises(CommencementGateFailedError) as exc_info:
            evaluate_commencement_gates(gates, context)

        error = exc_info.value
        assert error.condition == "collections['orders']['count'] == '<redacted-string-literal>'"
        assert sensitive_literal not in str(error)

    @pytest.mark.parametrize(
        ("condition_template", "expected_condition"),
        [
            (
                "collections[{'public': %r}] is None",
                "collections[{'public': '<redacted-string-literal>'}] is None",
            ),
            (
                "collections.get({'public': %r}) is None",
                "collections.get({'public': '<redacted-string-literal>'}) is None",
            ),
        ],
    )
    def test_composite_lookup_keys_redact_non_key_literals(
        self,
        condition_template: str,
        expected_condition: str,
    ) -> None:
        sensitive_literal = "composite-key-sensitive-value-9f3a"
        gate = CommencementGateConfig(
            name="composite_key",
            condition=condition_template % sensitive_literal,
        )

        with pytest.raises(CommencementGateFailedError) as exc_info:
            evaluate_commencement_gates([gate], {"dependency_runs": {}, "collections": {}})

        error = exc_info.value
        assert error.condition == expected_condition
        assert "'public'" in error.condition
        assert sensitive_literal not in str(error)

    def test_composite_dict_key_redacts_nested_non_key_literals(self) -> None:
        sensitive_literal = "composite-dict-key-sensitive-value-9f3a"
        gate = CommencementGateConfig(
            name="composite_dict_key",
            condition=f"collections['orders'] in {{{{'public': '{sensitive_literal}'}}: 1}}",
        )

        with pytest.raises(CommencementGateFailedError) as exc_info:
            evaluate_commencement_gates(
                [gate],
                {"dependency_runs": {}, "collections": {"orders": "ready"}},
            )

        error = exc_info.value
        assert error.condition == ("collections['orders'] in {{'public': '<redacted-string-literal>'}: 1}")
        assert "'public'" in error.condition
        assert sensitive_literal not in str(error)

    def test_expression_error_raises(self) -> None:
        gates = [
            CommencementGateConfig(
                name="bad",
                condition="collections['missing']['count'] > 0",
            )
        ]
        context: dict[str, dict[str, object]] = {
            "dependency_runs": {},
            "collections": {},
        }
        with pytest.raises(CommencementGateFailedError, match="bad"):
            evaluate_commencement_gates(gates, context)

    def test_non_bool_result_does_not_echo_context_secret(self) -> None:
        """elspeth-83261b699c invariant, re-expressed post-env-removal: a gate
        that returns a sensitive context string (non-bool) must not embed the
        raw value in the failure reason/message — the audit snapshot records
        structure, but the reason text bypasses that protection. The original
        vehicle was env['API_KEY']; the invariant survives the namespace."""
        secret = "sk-SUPERSECRET-9f3a"
        gates = [CommencementGateConfig(name="check", condition="dependency_runs['index']['run_id']")]
        context = {"dependency_runs": {"index": {"run_id": secret}}, "collections": {}}
        with pytest.raises(CommencementGateFailedError) as exc_info:
            evaluate_commencement_gates(gates, context)
        err = exc_info.value
        assert secret not in str(err)
        assert secret not in (err.reason or "")
        # The diagnostic type is still present.
        assert "str" in (err.reason or "")

    def test_expression_exception_does_not_echo_context_secret(self) -> None:
        """Expression exception reasons must not include raw context-derived values."""
        secret = "sk-SUPERSECRET-9f3a"
        gates = [
            CommencementGateConfig(
                name="lookup",
                condition="collections[dependency_runs['index']['run_id']]['count'] > 0",
            )
        ]
        context = {
            "dependency_runs": {"index": {"run_id": secret}},
            "collections": {"known": {"count": 1}},
        }

        with pytest.raises(CommencementGateFailedError) as exc_info:
            evaluate_commencement_gates(gates, context)

        err = exc_info.value
        assert secret not in str(err)
        assert secret not in (err.reason or "")
        assert "ExpressionEvaluationError" in (err.reason or "")

    def test_snapshot_is_deep_frozen(self) -> None:
        gates = [
            CommencementGateConfig(
                name="ready",
                condition="collections['test']['count'] > 0",
            )
        ]
        context = {
            "dependency_runs": {},
            "collections": {"test": {"count": 5, "reachable": True}},
        }
        results = evaluate_commencement_gates(gates, context)
        assert isinstance(results[0].context_snapshot, MappingProxyType)

    def test_multiple_gates_all_pass(self) -> None:
        gates = [
            CommencementGateConfig(name="g1", condition="collections['a']['count'] > 0"),
            CommencementGateConfig(name="g2", condition="collections['b']['count'] > 0"),
        ]
        context = {
            "dependency_runs": {},
            "collections": {
                "a": {"count": 5, "reachable": True},
                "b": {"count": 3, "reachable": True},
            },
        }
        results = evaluate_commencement_gates(gates, context)
        assert len(results) == 2

    def test_passing_gate_redacts_literals_but_preserves_map_keys(self) -> None:
        sensitive_literal = "literal-sensitive-value-9f3a"
        gate = CommencementGateConfig(
            name="literal_check",
            condition=(
                "collections.get('orders')['status'] in {'ready': 1} "
                "and dependency_runs['indexer']['run_id'] == 'run-safe' "
                f"and dependency_runs['indexer']['run_id'] != '{sensitive_literal}'"
            ),
        )
        context = {
            "dependency_runs": {"indexer": {"run_id": "run-safe"}},
            "collections": {"orders": {"status": "ready"}},
        }

        result = evaluate_commencement_gates([gate], context)[0]

        assert result.condition == (
            "collections.get('orders')['status'] in {'ready': 1} "
            "and dependency_runs['indexer']['run_id'] == '<redacted-string-literal>' "
            "and (dependency_runs['indexer']['run_id'] != '<redacted-string-literal>')"
        )
        assert sensitive_literal not in result.condition

    def test_second_gate_fails_stops_evaluation(self) -> None:
        gates = [
            CommencementGateConfig(name="g1", condition="collections['a']['count'] > 0"),
            CommencementGateConfig(name="g2", condition="collections['b']['count'] > 0"),
        ]
        context = {
            "dependency_runs": {},
            "collections": {
                "a": {"count": 5, "reachable": True},
                "b": {"count": 0, "reachable": False},
            },
        }
        with pytest.raises(CommencementGateFailedError, match="g2"):
            evaluate_commencement_gates(gates, context)

    def test_env_reference_rejected_at_config_time(self) -> None:
        """``env`` is no longer an allowed name; the config class rejects the
        condition at construction, before any evaluation can be reached."""
        condition = "env['ENVIRONMENT'] == 'production'"

        with pytest.raises(ValidationError) as exc_info:
            CommencementGateConfig(
                name="env_check",
                condition=condition,
            )

        rendered = str(exc_info.value)
        assert "unsupported or unsafe syntax" in rendered
        assert "ENVIRONMENT" not in rendered
        assert "production" not in rendered

    def test_empty_gates_returns_empty(self) -> None:
        results = evaluate_commencement_gates([], {"dependency_runs": {}, "collections": {}})
        assert results == []

    def test_context_mutation_after_evaluation_does_not_affect_snapshot(self) -> None:
        """TOCTOU protection: mutating original context must not change recorded snapshots."""
        gate = CommencementGateConfig(name="always_pass", condition="True")
        context: dict[str, Any] = {
            "dependency_runs": {"dep1": {"run_id": "r1"}},
            "collections": {"col1": {"count": 5, "reachable": True}},
        }
        results = evaluate_commencement_gates([gate], context)

        # Mutate the original context after evaluation
        context["dependency_runs"]["dep1"]["run_id"] = "MUTATED"
        context["collections"]["col1"]["count"] = 999
        context["new_key"] = "injected"

        # Snapshot must reflect pre-mutation state
        snapshot = results[0].context_snapshot
        assert snapshot["dependency_runs"]["dep1"]["run_id"] == "r1"
        assert snapshot["collections"]["col1"]["count"] == 5
        assert "new_key" not in snapshot


class TestCommencementGateCrashThrough:
    """Programming errors must crash through, not be wrapped as gate failures."""

    def _make_context(self) -> dict[str, object]:
        return {
            "dependency_runs": {},
            "collections": {},
        }

    def test_type_error_crashes_through(self) -> None:
        from unittest.mock import patch

        gate = CommencementGateConfig(name="g", condition="collections['x']['count'] > 0")
        context = self._make_context()

        with (
            patch("elspeth.engine.commencement.ExpressionParser") as mock_cls,
            pytest.raises(TypeError),
        ):
            mock_cls.return_value.evaluate.side_effect = TypeError("bad operand")
            evaluate_commencement_gates([gate], context)

    def test_attribute_error_crashes_through(self) -> None:
        from unittest.mock import patch

        gate = CommencementGateConfig(name="g", condition="True")
        context = self._make_context()

        with (
            patch("elspeth.engine.commencement.ExpressionParser") as mock_cls,
            pytest.raises(AttributeError),
        ):
            mock_cls.return_value.evaluate.side_effect = AttributeError("no attr")
            evaluate_commencement_gates([gate], context)

    def test_name_error_crashes_through(self) -> None:
        from unittest.mock import patch

        gate = CommencementGateConfig(name="g", condition="True")
        context = self._make_context()

        with (
            patch("elspeth.engine.commencement.ExpressionParser") as mock_cls,
            pytest.raises(NameError),
        ):
            mock_cls.return_value.evaluate.side_effect = NameError("undefined")
            evaluate_commencement_gates([gate], context)

    def test_graceful_shutdown_crashes_through(self) -> None:
        from unittest.mock import patch

        gate = CommencementGateConfig(name="g", condition="True")
        context = self._make_context()
        shutdown = GracefulShutdownError(rows_processed=0, run_id="run-1")

        with (
            patch("elspeth.engine.commencement.ExpressionParser") as mock_cls,
            pytest.raises(GracefulShutdownError),
        ):
            mock_cls.return_value.evaluate.side_effect = shutdown
            evaluate_commencement_gates([gate], context)


class TestBuildPreflightContext:
    def test_includes_all_sections(self) -> None:
        context = build_preflight_context(
            dependency_results={},
            collection_probes={"test": {"count": 5, "reachable": True}},
        )
        assert "dependency_runs" in context
        assert "collections" in context


class TestCommencementGateTypeEnforcement:
    """Regression: non-boolean gate results must be rejected, not coerced."""

    def test_non_boolean_result_rejected(self) -> None:
        """Gate returning a truthy non-boolean (e.g., int 1) is a config error."""
        gates = [
            CommencementGateConfig(
                name="count_check",
                condition="collections['data']['count']",
            ),
        ]
        context: dict[str, Any] = {
            "collections": {"data": {"count": 5}},
            "dependency_runs": {},
        }
        with pytest.raises(CommencementGateFailedError, match="not bool"):
            evaluate_commencement_gates(gates, context)

    def test_string_result_rejected(self) -> None:
        """Gate returning a truthy string is a config error."""
        gates = [
            CommencementGateConfig(
                name="label_check",
                condition="collections.get('data')",
            ),
        ]
        context: dict[str, Any] = {
            "collections": {"data": "ready"},
            "dependency_runs": {},
        }
        with pytest.raises(CommencementGateFailedError, match="not bool"):
            evaluate_commencement_gates(gates, context)

    def test_boolean_result_accepted(self) -> None:
        """Gate returning actual bool passes normally."""
        gates = [
            CommencementGateConfig(
                name="real_check",
                condition="collections['data']['count'] > 0",
            ),
        ]
        context: dict[str, Any] = {
            "collections": {"data": {"count": 5}},
            "dependency_runs": {},
        }
        results = evaluate_commencement_gates(gates, context)
        assert len(results) == 1
        assert results[0].result is True


class TestCommencementGateConfigErrorBoundary:
    def test_security_rejection_is_an_owned_non_disclosing_validation_error(self) -> None:
        sensitive_literal = "security-rejected-sensitive-value-9f3a"
        condition = f"{{'public': '{sensitive_literal}'}}['public'] == 'expected'"

        with pytest.raises(ValidationError) as exc_info:
            CommencementGateConfig(name="unsafe", condition=condition)

        rendered = str(exc_info.value)
        assert "unsupported or unsafe syntax" in rendered
        assert sensitive_literal not in rendered
        assert condition not in rendered

    def test_nested_model_validation_does_not_restore_rejected_condition_input(self) -> None:
        class GateEnvelope(BaseModel):
            commencement_gates: list[CommencementGateConfig]

        sensitive_literal = "nestsecret9f3a"
        condition = f"{{'public': '{sensitive_literal}'}}['public'] == 'expected'"

        with pytest.raises(ValidationError) as exc_info:
            GateEnvelope(
                commencement_gates=[
                    {
                        "name": "unsafe",
                        "condition": condition,
                    }
                ]
            )

        rendered = str(exc_info.value)
        assert "unsupported or unsafe syntax" in rendered
        assert "<redacted-commencement-gate-condition>" in rendered
        assert "nestsecret" not in rendered
        assert sensitive_literal not in rendered
        assert condition not in rendered

    def test_syntax_rejection_is_an_owned_non_disclosing_validation_error(self) -> None:
        sensitive_literal = "syntax-rejected-sensitive-value-9f3a"
        condition = f"collections['orders'] == '{sensitive_literal}"

        with pytest.raises(ValidationError) as exc_info:
            CommencementGateConfig(name="malformed", condition=condition)

        rendered = str(exc_info.value)
        assert "invalid syntax" in rendered
        assert sensitive_literal not in rendered
        assert condition not in rendered


class TestEnvNamespaceRemoved:
    """The vestigial ``env`` namespace is gone (elspeth-83261b699c cleanup).

    ``env`` was YAML-only, its sole caller never passed it, and it was the
    surface of "Commencement gate non-bool failure can echo env secret
    values into audit/error text". Removing the namespace removes that
    surface entirely.
    """

    def test_preflight_context_has_exactly_two_namespaces(self) -> None:
        context = build_preflight_context(
            dependency_results={},
            collection_probes={"test": {"count": 5, "reachable": True}},
        )
        assert set(context.keys()) == {"dependency_runs", "collections"}

    def test_audit_snapshot_shape_has_no_env_keys(self) -> None:
        gates = [
            CommencementGateConfig(
                name="check",
                condition="collections['data']['count'] > 0",
            ),
        ]
        context: dict[str, Any] = {
            "collections": {"data": {"count": 1}},
            "dependency_runs": {},
        }
        results = evaluate_commencement_gates(gates, context)
        assert set(results[0].context_snapshot.keys()) == {"dependency_runs", "collections"}


class TestExpressionSecurityErrorCrashesThrough:
    """A post-A1 framework bug must not be relabelled as a gate failure.

    The evaluator's fail-closed visit() raises ExpressionSecurityError for a
    validator-allowed-but-evaluator-uncovered node — a framework bug, not a
    user's failing gate. The ``except BaseException`` wrapper here previously
    relabelled it CommencementGateFailedError ("Expression raised ...").
    """

    def test_expression_security_error_propagates_unwrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.core.expression_parser import ExpressionParser, ExpressionSecurityError

        def injected_framework_bug(self: ExpressionParser, context: Any) -> Any:
            raise ExpressionSecurityError("Evaluator has no handler for FakeExpr nodes")

        monkeypatch.setattr(ExpressionParser, "evaluate", injected_framework_bug)
        gates = [
            CommencementGateConfig(
                name="check",
                condition="collections['data']['count'] > 0",
            ),
        ]
        context: dict[str, Any] = {
            "collections": {"data": {"count": 1}},
            "dependency_runs": {},
        }
        with pytest.raises(ExpressionSecurityError, match="no handler"):
            evaluate_commencement_gates(gates, context)
