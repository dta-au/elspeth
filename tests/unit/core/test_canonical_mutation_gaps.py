"""Mutation gap tests for core/canonical.py.

Tests targeting specific mutation survivors:
- Line 47-49: NaN/Infinity rejection with 'or' logic (not 'and')
- Ensures both float and np.floating types are checked
"""

import math

import numpy as np
import pandas as pd
import pytest


class TestNanInfinityOrLogic:
    """Tests for NaN/Infinity rejection logic.

    Targets line 49: if math.isnan(obj) or math.isinf(obj):
    Mutant might change 'or' to 'and', which would only reject values
    that are BOTH NaN AND Infinity (impossible).

    These tests ensure each condition independently triggers rejection.
    """

    def test_nan_alone_is_rejected_not_requiring_infinity(self) -> None:
        """Line 49: NaN must be rejected even without Infinity.

        If mutation changes 'or' to 'and', NaN alone would pass through
        because a value cannot be both NaN and Infinity.
        """
        from elspeth.core.canonical import _normalize_value

        with pytest.raises(ValueError, match="non-finite"):
            _normalize_value(float("nan"))

    def test_positive_infinity_alone_is_rejected_not_requiring_nan(self) -> None:
        """Line 49: +Infinity must be rejected even without NaN.

        If mutation changes 'or' to 'and', Infinity alone would pass through.
        """
        from elspeth.core.canonical import _normalize_value

        with pytest.raises(ValueError, match="non-finite"):
            _normalize_value(float("inf"))

    def test_negative_infinity_alone_is_rejected_not_requiring_nan(self) -> None:
        """Line 49: -Infinity must be rejected even without NaN.

        Ensures negative infinity is also caught.
        """
        from elspeth.core.canonical import _normalize_value

        with pytest.raises(ValueError, match="non-finite"):
            _normalize_value(float("-inf"))


class TestTypeCheckCoversBothFloatTypes:
    """Tests for type check coverage.

    Targets line 48: if isinstance(obj, float | np.floating):
    Mutant might remove one type from the union, allowing NaN/Infinity
    to slip through for that type.
    """

    def test_python_float_nan_is_rejected(self) -> None:
        """Line 48: Python float NaN must be caught by type check."""
        from elspeth.core.canonical import _normalize_value

        nan_value = float("nan")
        assert isinstance(nan_value, float)  # Verify it's a Python float

        with pytest.raises(ValueError, match="non-finite"):
            _normalize_value(nan_value)

    def test_numpy_float64_nan_is_rejected(self) -> None:
        """Line 48: numpy.float64 NaN must be caught by type check."""
        from elspeth.core.canonical import _normalize_value

        nan_value = np.float64("nan")
        assert isinstance(nan_value, np.floating)  # Verify it's numpy floating

        with pytest.raises(ValueError, match="non-finite"):
            _normalize_value(nan_value)

    def test_numpy_float32_nan_is_rejected(self) -> None:
        """Line 48: numpy.float32 NaN must also be caught.

        np.float32 is a subtype of np.floating, should be covered.
        """
        from elspeth.core.canonical import _normalize_value

        nan_value = np.float32("nan")
        assert isinstance(nan_value, np.floating)

        with pytest.raises(ValueError, match="non-finite"):
            _normalize_value(nan_value)

    def test_python_float_infinity_is_rejected(self) -> None:
        """Line 48: Python float Infinity must be caught."""
        from elspeth.core.canonical import _normalize_value

        inf_value = float("inf")
        assert isinstance(inf_value, float)

        with pytest.raises(ValueError, match="non-finite"):
            _normalize_value(inf_value)

    def test_numpy_float64_infinity_is_rejected(self) -> None:
        """Line 48: numpy.float64 Infinity must be caught."""
        from elspeth.core.canonical import _normalize_value

        inf_value = np.float64("inf")
        assert isinstance(inf_value, np.floating)

        with pytest.raises(ValueError, match="non-finite"):
            _normalize_value(inf_value)


class TestNormalFloatsPassThrough:
    """Tests verifying normal floats are NOT rejected.

    Ensures the NaN/Infinity check doesn't accidentally reject valid floats.
    """

    def test_zero_float_passes(self) -> None:
        """Zero is a valid float, should pass through."""
        from elspeth.core.canonical import _normalize_value

        assert _normalize_value(0.0) == 0.0

    def test_negative_zero_passes(self) -> None:
        """Negative zero is valid, should pass through."""
        from elspeth.core.canonical import _normalize_value

        result = _normalize_value(-0.0)
        assert result == 0.0 or math.copysign(1, result) == -1  # -0.0 or 0.0 both acceptable

    def test_large_float_passes(self) -> None:
        """Large (but finite) floats should pass through."""
        from elspeth.core.canonical import _normalize_value

        large_value = 1e308  # Near max float
        assert _normalize_value(large_value) == large_value

    def test_small_float_passes(self) -> None:
        """Small (but non-zero) floats should pass through."""
        from elspeth.core.canonical import _normalize_value

        small_value = 1e-308  # Near min positive float
        assert _normalize_value(small_value) == small_value

    def test_normal_numpy_float_passes(self) -> None:
        """Normal numpy float should pass through and be converted to Python float."""
        from elspeth.core.canonical import _normalize_value

        result = _normalize_value(np.float64(3.14))
        assert result == 3.14
        assert type(result) is float  # Should be converted from np.float64


class TestSingleElementNanArrayRejection:
    """Kill mutant: obj.size > 0 → obj.size > 1.

    A single-element NaN array must be rejected. If the size check
    becomes > 1, a single-element array (size=1) bypasses NaN detection
    and propagates into canonical JSON — producing non-deterministic
    hashes (IEEE 754: NaN != NaN).
    """

    def test_single_element_nan_array_rejected(self) -> None:
        """Single-element NaN array must trigger ValueError."""
        from elspeth.core.canonical import _normalize_value

        with pytest.raises(ValueError, match="NaN"):
            _normalize_value(np.array([float("nan")]))

    def test_single_element_inf_array_rejected(self) -> None:
        """Single-element Infinity array must also be rejected."""
        from elspeth.core.canonical import _normalize_value

        with pytest.raises(ValueError, match=r"Infinity|NaN"):
            _normalize_value(np.array([float("inf")]))

    def test_empty_array_returns_empty_list(self) -> None:
        """Empty array skips NaN check and returns []."""
        from elspeth.core.canonical import _normalize_value

        result = _normalize_value(np.array([]))
        assert result == []

    def test_multi_element_nan_array_still_rejected(self) -> None:
        """Multi-element array with NaN is rejected (existing behavior)."""
        from elspeth.core.canonical import _normalize_value

        with pytest.raises(ValueError, match="NaN"):
            _normalize_value(np.array([1.0, float("nan"), 3.0]))


class TestTopologyHashEdgeKeys:
    """Kill mutant: ``keys=True`` → ``keys=False`` in ``nx_graph.edges(keys=True)``.

    Line 224 of canonical.py: Without edge keys, multi-edges between the same
    node pair collapse into a single edge, producing identical hashes for
    topologically different graphs.

    Two graphs with the same nodes but different edge keys between the same
    pair must produce different topology hashes.
    """

    def test_different_multi_edge_keys_produce_different_hashes(self) -> None:
        """Two graphs with different edge keys between same node pair differ in hash.

        Graph 1: gate -> sink_a via "route_x" and "route_y"
        Graph 2: gate -> sink_a via "route_x" and "route_z"

        If keys=False, both graphs yield the same (gate, sink_a) edge pair
        and collapse into the same hash.
        """
        from elspeth.contracts.enums import NodeType, RoutingMode
        from elspeth.core.canonical import compute_full_topology_hash
        from elspeth.core.dag import ExecutionGraph

        graph1 = ExecutionGraph()
        graph1.add_node("source", node_type=NodeType.SOURCE, config={}, plugin_name="csv")
        graph1.add_node("gate", node_type=NodeType.GATE, config={}, plugin_name="fork_gate")
        graph1.add_node("sink_a", node_type=NodeType.SINK, config={}, plugin_name="csv")
        graph1.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph1.add_edge("gate", "sink_a", label="route_x", mode=RoutingMode.MOVE)
        graph1.add_edge("gate", "sink_a", label="route_y", mode=RoutingMode.MOVE)

        graph2 = ExecutionGraph()
        graph2.add_node("source", node_type=NodeType.SOURCE, config={}, plugin_name="csv")
        graph2.add_node("gate", node_type=NodeType.GATE, config={}, plugin_name="fork_gate")
        graph2.add_node("sink_a", node_type=NodeType.SINK, config={}, plugin_name="csv")
        graph2.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph2.add_edge("gate", "sink_a", label="route_x", mode=RoutingMode.MOVE)
        graph2.add_edge("gate", "sink_a", label="route_z", mode=RoutingMode.MOVE)

        hash1 = compute_full_topology_hash(graph1)
        hash2 = compute_full_topology_hash(graph2)

        assert hash1 != hash2, (
            "Graphs with different edge keys between same node pair must produce "
            "different hashes. If keys=False mutant is active, multi-edges collapse."
        )

    def test_single_vs_multi_edge_produces_different_hash(self) -> None:
        """Graph with one edge vs two edges between same pair must differ.

        Graph 1: gate -> sink_a via "route_x" only
        Graph 2: gate -> sink_a via "route_x" AND "route_y"

        If keys=False, graph2's two edges collapse to one, matching graph1.
        """
        from elspeth.contracts.enums import NodeType, RoutingMode
        from elspeth.core.canonical import compute_full_topology_hash
        from elspeth.core.dag import ExecutionGraph

        graph1 = ExecutionGraph()
        graph1.add_node("source", node_type=NodeType.SOURCE, config={}, plugin_name="csv")
        graph1.add_node("gate", node_type=NodeType.GATE, config={}, plugin_name="fork_gate")
        graph1.add_node("sink_a", node_type=NodeType.SINK, config={}, plugin_name="csv")
        graph1.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph1.add_edge("gate", "sink_a", label="route_x", mode=RoutingMode.MOVE)

        graph2 = ExecutionGraph()
        graph2.add_node("source", node_type=NodeType.SOURCE, config={}, plugin_name="csv")
        graph2.add_node("gate", node_type=NodeType.GATE, config={}, plugin_name="fork_gate")
        graph2.add_node("sink_a", node_type=NodeType.SINK, config={}, plugin_name="csv")
        graph2.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph2.add_edge("gate", "sink_a", label="route_x", mode=RoutingMode.MOVE)
        graph2.add_edge("gate", "sink_a", label="route_y", mode=RoutingMode.MOVE)

        hash1 = compute_full_topology_hash(graph1)
        hash2 = compute_full_topology_hash(graph2)

        assert hash1 != hash2, (
            "Single-edge vs multi-edge between same pair must hash differently. "
            "If keys=False mutant is active, the extra edge is invisible."
        )


class TestNonePassthroughAndNaTDetection:
    """Kill survivors on the passthrough and missing-value arms of _normalize_value.

    Primitive-passthrough arm: ``obj is None or isinstance(obj, str | int | bool)``
        Mutant: ``or`` → ``and``. None would fail the isinstance check and
        fall through to the trailing ``return obj``. Currently equivalent
        (returns None either way), but fragile — any type check added between
        that arm and the fall-through could intercept None and change behavior.

    Missing-value arm: ``obj is pd.NA or obj is pd.NaT``
        Mutant: either identity test deleted. Both sentinels must normalize to
        None; dropping an arm sends that sentinel to the ``return obj``
        fall-through, which then reaches rfc8785 as an unserializable object.
        Identity IS the type check here — both are singletons, so ``obj is
        pd.NaT`` already implies ``isinstance(obj, type(pd.NaT))``.
    """

    def test_none_returns_none_directly(self) -> None:
        """None must be caught by the passthrough arm, not fall through to ``return obj``.

        Kills mutant: ``or`` → ``and`` on the primitive-passthrough arm.
        """
        from elspeth.core.canonical import _normalize_value

        result = _normalize_value(None)
        assert result is None

    def test_pd_nat_returns_none(self) -> None:
        """pd.NaT must be normalized to None (intentional missing value).

        Kills mutant: deletion of the ``obj is pd.NaT`` arm.
        """
        from elspeth.core.canonical import _normalize_value

        result = _normalize_value(pd.NaT)
        assert result is None

    def test_pd_na_returns_none(self) -> None:
        """pd.NA (nullable integer NA) must also normalize to None."""
        from elspeth.core.canonical import _normalize_value

        result = _normalize_value(pd.NA)
        assert result is None

    def test_zero_dimensional_array_returns_scalar(self) -> None:
        """0-D numpy array (e.g., np.array(42)) must return the scalar value.

        Kills potential mutant on ndim == 0 check at line 91.
        """
        from elspeth.core.canonical import _normalize_value

        result = _normalize_value(np.array(42))
        assert result == 42
        assert isinstance(result, int)

    def test_zero_dimensional_float_array_returns_float(self) -> None:
        """0-D float array must return Python float, not numpy scalar."""
        from elspeth.core.canonical import _normalize_value

        result = _normalize_value(np.array(3.14))
        assert result == 3.14
        assert type(result) is float

    def test_zero_dimensional_nan_array_rejected(self) -> None:
        """0-D NaN array must be rejected, not silently passed through."""
        from elspeth.core.canonical import _normalize_value

        with pytest.raises(ValueError, match="NaN"):
            _normalize_value(np.array(float("nan")))


class TestSanitizeForCanonicalNumpyEdgeCases:
    """Kill survivors on line 281-282 of sanitize_for_canonical.

    Line 281: ``isinstance(obj, np.floating) and not math.isfinite(float(obj))``
        Mutant: comparison operator changes on the isinstance or isfinite check.
        Tests ensure numpy floating NaN/Inf are sanitized to None while normal
        numpy floats pass through unchanged.
    """

    def test_numpy_float64_nan_sanitized_to_none(self) -> None:
        """np.float64 NaN must be replaced with None."""
        from elspeth.core.canonical import sanitize_for_canonical

        result = sanitize_for_canonical(np.float64("nan"))
        assert result is None

    def test_numpy_float64_positive_inf_sanitized_to_none(self) -> None:
        """np.float64 +Inf must be replaced with None."""
        from elspeth.core.canonical import sanitize_for_canonical

        result = sanitize_for_canonical(np.float64("inf"))
        assert result is None

    def test_numpy_float64_negative_inf_sanitized_to_none(self) -> None:
        """np.float64 -Inf must be replaced with None."""
        from elspeth.core.canonical import sanitize_for_canonical

        result = sanitize_for_canonical(np.float64("-inf"))
        assert result is None

    def test_numpy_float32_nan_sanitized_to_none(self) -> None:
        """np.float32 NaN must also be caught (subtype of np.floating)."""
        from elspeth.core.canonical import sanitize_for_canonical

        result = sanitize_for_canonical(np.float32("nan"))
        assert result is None

    def test_numpy_float64_normal_value_passes_through(self) -> None:
        """Normal np.float64 must NOT be sanitized — pass through unchanged."""
        from elspeth.core.canonical import sanitize_for_canonical

        result = sanitize_for_canonical(np.float64(42.0))
        # sanitize_for_canonical doesn't convert types, just replaces non-finite
        assert float(result) == 42.0

    def test_numpy_float64_zero_passes_through(self) -> None:
        """np.float64(0.0) is finite and must pass through."""
        from elspeth.core.canonical import sanitize_for_canonical

        result = sanitize_for_canonical(np.float64(0.0))
        assert float(result) == 0.0

    def test_sanitize_nested_dict_with_numpy_nan(self) -> None:
        """Nested structures containing np.floating NaN are recursively sanitized."""
        from elspeth.core.canonical import sanitize_for_canonical

        data = {"a": np.float64(1.0), "b": {"c": np.float64("nan")}, "d": [np.float32("inf")]}
        result = sanitize_for_canonical(data)

        assert float(result["a"]) == 1.0
        assert result["b"]["c"] is None
        assert result["d"][0] is None
