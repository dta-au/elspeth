# tests/integration/checkpoint/test_serialization.py
"""Tests for checkpoint serialization module.

Migrated from tests/core/checkpoint/test_serialization.py.
No external test imports needed - uses only production code.
"""

from datetime import UTC, datetime

import pytest

from elspeth.core.checkpoint.serialization import (
    checkpoint_dumps,
    checkpoint_loads,
)


class TestCheckpointSerialization:
    """Unit tests for type-preserving JSON serialization."""

    def test_round_trip_primitives(self) -> None:
        """Primitive types round-trip correctly."""
        data = {
            "string": "hello",
            "int": 42,
            "float": 3.14,
            "bool": True,
            "none": None,
            "list": [1, 2, 3],
            "nested": {"a": 1, "b": 2},
        }

        result = checkpoint_loads(checkpoint_dumps(data))
        assert result == data

    def test_datetime_serialization_format(self) -> None:
        """datetime is serialized with type tag."""
        dt = datetime(2026, 2, 5, 12, 30, 45, tzinfo=UTC)
        serialized = checkpoint_dumps({"created_at": dt})

        # Should contain collision-safe type envelope
        assert "__elspeth_type__" in serialized
        assert "__elspeth_value__" in serialized
        assert "2026-02-05T12:30:45" in serialized

    def test_datetime_round_trip(self) -> None:
        """datetime values round-trip with type fidelity."""
        dt = datetime(2026, 2, 5, 12, 30, 45, tzinfo=UTC)
        data = {"created_at": dt}

        result = checkpoint_loads(checkpoint_dumps(data))

        assert isinstance(result["created_at"], datetime)
        assert result["created_at"] == dt

    def test_naive_datetime_gets_utc(self) -> None:
        """Naive datetime gets UTC timezone on serialization."""
        aware_dt = datetime(2026, 2, 5, 12, 30, 45, tzinfo=UTC)
        naive_dt = aware_dt.replace(tzinfo=None)
        data = {"created_at": naive_dt}

        result = checkpoint_loads(checkpoint_dumps(data))

        assert result["created_at"].tzinfo is not None
        assert result["created_at"].replace(tzinfo=None) == naive_dt

    def test_datetime_in_list(self) -> None:
        """datetime values in lists round-trip correctly."""
        dt1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        dt2 = datetime(2026, 6, 15, 12, 30, 0, tzinfo=UTC)
        data = {"dates": [dt1, dt2]}

        result = checkpoint_loads(checkpoint_dumps(data))

        assert isinstance(result["dates"][0], datetime)
        assert isinstance(result["dates"][1], datetime)
        assert result["dates"][0] == dt1
        assert result["dates"][1] == dt2

    def test_datetime_in_nested_dict(self) -> None:
        """datetime values in nested dicts round-trip correctly."""
        dt = datetime(2026, 2, 5, 12, 30, 45, tzinfo=UTC)
        data = {"metadata": {"updated_at": dt}}

        result = checkpoint_loads(checkpoint_dumps(data))

        assert isinstance(result["metadata"]["updated_at"], datetime)
        assert result["metadata"]["updated_at"] == dt

    def test_rejects_nan(self) -> None:
        """NaN values are rejected (engine-patterns-reference skill §Canonical JSON)."""
        data = {"value": float("nan")}

        with pytest.raises(ValueError, match="non-finite float"):
            checkpoint_dumps(data)

    def test_rejects_infinity(self) -> None:
        """Infinity values are rejected (engine-patterns-reference skill §Canonical JSON)."""
        data = {"value": float("inf")}

        with pytest.raises(ValueError, match="non-finite float"):
            checkpoint_dumps(data)

    def test_rejects_negative_infinity(self) -> None:
        """Negative infinity is also rejected."""
        data = {"value": float("-inf")}

        with pytest.raises(ValueError, match="non-finite float"):
            checkpoint_dumps(data)

    def test_rejects_nested_nan(self) -> None:
        """NaN in nested structure is still rejected."""
        data = {"outer": {"inner": {"value": float("nan")}}}

        with pytest.raises(ValueError, match="non-finite float"):
            checkpoint_dumps(data)

    def test_rejects_nan_in_list(self) -> None:
        """NaN in list is still rejected."""
        data = {"values": [1.0, float("nan"), 3.0]}

        with pytest.raises(ValueError, match="non-finite float"):
            checkpoint_dumps(data)

    def test_aggregation_state_structure(self) -> None:
        """Real aggregation state structure serializes correctly."""
        dt = datetime(2026, 2, 5, 12, 30, 45, tzinfo=UTC)
        agg_state = {
            "node-001": {
                "tokens": [
                    {
                        "token_id": "tok-001",
                        "row_id": "row-001",
                        "branch_name": None,
                        "fork_group_id": None,
                        "join_group_id": None,
                        "expand_group_id": None,
                        "row_data": {
                            "id": 1,
                            "name": "test",
                            "created_at": dt,
                            "amount": 42.50,
                        },
                        "contract_version": "abc123",
                    }
                ],
                "batch_id": "batch-001",
                "elapsed_age_seconds": 5.5,
                "count_fire_offset": None,
                "condition_fire_offset": None,
                "contract": {
                    "mode": "FLEXIBLE",
                    "locked": True,
                    "version_hash": "def456",
                    "fields": [],
                },
            },
            "_version": "2.0",
        }

        result = checkpoint_loads(checkpoint_dumps(agg_state))

        # Verify structure preserved
        assert result["_version"] == "2.0"
        assert result["node-001"]["batch_id"] == "batch-001"

        # Verify datetime restored
        restored_dt = result["node-001"]["tokens"][0]["row_data"]["created_at"]
        assert isinstance(restored_dt, datetime)
        assert restored_dt == dt

    def test_empty_dict_serializes(self) -> None:
        """Empty dict serializes and deserializes correctly."""
        result = checkpoint_loads(checkpoint_dumps({}))
        assert result == {}

    def test_empty_list_serializes(self) -> None:
        """Empty list serializes and deserializes correctly."""
        result = checkpoint_loads(checkpoint_dumps({"items": []}))
        assert result == {"items": []}
