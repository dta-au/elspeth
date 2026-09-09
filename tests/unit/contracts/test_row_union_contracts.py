"""Contract-level surface for the row_union barrier primitive.

The v1 contract (filigree elspeth-a5b86149d4, product decision 2026-07-16)
defines row_union as a correlated same-row_id N->N UNION ALL barrier over
declared fork branches. These tests pin the contract vocabulary: the node
type, the name NewType, and the typed failure payload.
"""

from __future__ import annotations

import pytest

from elspeth.contracts import RowUnionName
from elspeth.contracts.enums import NodeType
from elspeth.contracts.errors import RowUnionFailureReason


class TestNodeTypeRowUnion:
    def test_row_union_member_exists_with_db_value(self) -> None:
        assert NodeType.ROW_UNION.value == "row_union"

    def test_row_union_is_distinct_from_other_join_primitives(self) -> None:
        assert NodeType.ROW_UNION is not NodeType.COALESCE
        assert NodeType.ROW_UNION is not NodeType.QUEUE


class TestRowUnionName:
    def test_name_newtype_wraps_str(self) -> None:
        name = RowUnionName("variant_union")
        assert name == "variant_union"


class TestRowUnionFailureReason:
    def test_constructs_with_required_fields(self) -> None:
        reason = RowUnionFailureReason(
            failure_reason="row_union_timeout",
            expected_branches=("control_branch", "treatment_branch"),
            branches_arrived=("control_branch",),
        )
        assert reason.failure_reason == "row_union_timeout"
        assert reason.expected_branches == ("control_branch", "treatment_branch")
        assert reason.branches_arrived == ("control_branch",)
        assert reason.timeout_ms is None

    def test_rejects_empty_failure_reason(self) -> None:
        with pytest.raises(ValueError, match="failure_reason"):
            RowUnionFailureReason(
                failure_reason="",
                expected_branches=("a", "b"),
                branches_arrived=(),
            )

    def test_rejects_empty_expected_branches(self) -> None:
        with pytest.raises(ValueError, match="expected_branches"):
            RowUnionFailureReason(
                failure_reason="row_union_branch_lost",
                expected_branches=(),
                branches_arrived=(),
            )

    def test_rejects_negative_timeout(self) -> None:
        with pytest.raises(ValueError, match="timeout_ms"):
            RowUnionFailureReason(
                failure_reason="row_union_timeout",
                expected_branches=("a", "b"),
                branches_arrived=("a",),
                timeout_ms=-1,
            )
