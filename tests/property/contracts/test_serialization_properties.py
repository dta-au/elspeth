# tests/property/contracts/test_serialization_properties.py
"""Property-based tests for contract dataclass serialization.

These tests verify that ELSPETH's core contract dataclasses serialize
correctly for audit storage - a critical requirement for the audit trail.

Serialization Properties:
- TokenInfo preserves identity fields through construction
- TransformResult JSON round-trips preserve data correctly
- RoutingAction JSON round-trips preserve routing decisions
- Frozen dataclasses serialize to valid JSON

IMPORTANT: These tests complement test_results_properties.py and
test_routing_properties.py by focusing on serialization aspects.
Factory method invariants are tested in those other modules.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, cast

from hypothesis import given, settings
from hypothesis import strategies as st

from elspeth.contracts import TransformErrorReason
from elspeth.contracts.enums import FrameKind, RoutingKind, RoutingMode
from elspeth.contracts.errors import ConfigGateReason, TransformSuccessReason
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.identity import LineageFrame, TokenInfo
from elspeth.contracts.results import TransformResult
from elspeth.contracts.routing import RoutingAction
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.testing import make_pipeline_row
from tests.strategies.ids import id_strings
from tests.strategies.json import row_data

# =============================================================================
# Helper Functions
# =============================================================================


def _make_observed_contract() -> SchemaContract:
    """Create an OBSERVED schema contract for property tests."""
    return SchemaContract(mode="OBSERVED", fields=())


def _wrap_dict_as_pipeline_row(data: dict[str, Any]) -> PipelineRow:
    """Wrap dict as PipelineRow with OBSERVED contract for property tests."""
    return PipelineRow(data, _make_observed_contract())


def _token_to_dict(token: TokenInfo) -> dict[str, Any]:
    """Convert TokenInfo to dict, handling PipelineRow serialization.

    This is needed because TokenInfo.row_data is now PipelineRow (not dict),
    and dataclasses.asdict() doesn't know how to serialize custom classes.
    We manually convert PipelineRow to dict before calling asdict().

    Args:
        token: TokenInfo instance to serialize

    Returns:
        Dictionary with row_data as plain dict
    """
    # Convert token to dict, handling PipelineRow -> dict conversion
    # asdict() will crash on PipelineRow, so we manually build the dict.
    return {
        "row_id": token.row_id,
        "token_id": token.token_id,
        "row_data": token.row_data.to_dict() if isinstance(token.row_data, PipelineRow) else token.row_data,
        # Derived from lineage_path (ruling 21) — read via the properties,
        # never independently constructed.
        "branch_name": token.branch_name,
        "fork_group_id": token.fork_group_id,
        "expand_group_id": token.expand_group_id,
    }


# =============================================================================
# Strategies for serialization testing
# =============================================================================

# WS1b flip: branch_name/fork_group_id are no longer independent TokenInfo
# constructor kwargs — both are derived from the SAME innermost FORK frame
# (ruling 21), so they can no longer vary independently. expand_group_id
# comes from a separate, optionally-nested-inside-the-fork EXPAND frame.
# These strategies generate a lineage_path exercising both axes together.
_non_empty_branch_names = st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz_")
_non_empty_group_ids = id_strings

_optional_fork_frame = st.one_of(
    st.none(),
    st.tuples(_non_empty_group_ids, _non_empty_branch_names).map(
        lambda pair: LineageFrame(kind=FrameKind.FORK, group_id=pair[0], member_key=pair[1])
    ),
)
_optional_expand_frame = st.one_of(
    st.none(),
    st.tuples(_non_empty_group_ids, _non_empty_branch_names).map(
        lambda pair: LineageFrame(kind=FrameKind.EXPAND, group_id=pair[0], member_key=pair[1])
    ),
)
lineage_paths = st.tuples(_optional_fork_frame, _optional_expand_frame).map(
    lambda pair: tuple(frame for frame in pair if frame is not None)
)

# Non-empty ID strings (required fields)
non_empty_ids = st.text(min_size=1, max_size=40, alphabet="0123456789abcdef")

# Path names for routing
path_names = st.text(
    min_size=1,
    max_size=30,
    alphabet="abcdefghijklmnopqrstuvwxyz_0123456789",
).filter(lambda s: s[0].isalpha())

# Unique path lists for fork
unique_path_lists = st.lists(path_names, min_size=1, max_size=5, unique=True)

# ConfigGateReason: condition + result (from config-driven gates)
config_gate_reasons = st.fixed_dictionaries(
    {
        "condition": st.text(min_size=1, max_size=50),
        "result": st.text(min_size=1, max_size=20),
    }
)

# RoutingReason union for property tests
routing_reasons = st.one_of(
    st.none(),
    config_gate_reasons,
)

# TransformErrorReason dictionaries for TransformResult.error()
# Valid TransformErrorReason requires "reason" field with Literal-typed value
_test_error_categories = [
    "api_error",
    "missing_field",
    "validation_failed",
    "test_error",
    "property_test_error",
]

transform_error_reasons = st.fixed_dictionaries(
    {"reason": st.sampled_from(_test_error_categories)},
    optional={
        "error": st.text(min_size=1, max_size=100),
        "field": st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnopqrstuvwxyz_"),
    },
)

# TransformSuccessReason dictionaries for TransformResult.success()
success_reasons: st.SearchStrategy[dict[str, Any]] = st.fixed_dictionaries(
    {"action": st.sampled_from(["processed", "validated", "enriched", "passthrough"])},
    optional={
        "fields_modified": st.lists(st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnopqrstuvwxyz_"), max_size=5),
        "validation_warnings": st.lists(st.text(min_size=1, max_size=50), max_size=3),
    },
)


# =============================================================================
# TokenInfo Construction and Serialization Properties
# =============================================================================


class TestTokenInfoConstructionProperties:
    """Property tests for TokenInfo construction preserving identity."""

    @given(
        row_id=non_empty_ids,
        token_id=non_empty_ids,
        data=row_data,
    )
    @settings(max_examples=100)
    def test_token_info_preserves_row_id(
        self,
        row_id: str,
        token_id: str,
        data: dict[str, Any],
    ) -> None:
        """Property: TokenInfo preserves row_id through construction."""
        token = TokenInfo(row_id=row_id, token_id=token_id, row_data=_wrap_dict_as_pipeline_row(data))
        assert token.row_id == row_id

    @given(
        row_id=non_empty_ids,
        token_id=non_empty_ids,
        data=row_data,
    )
    @settings(max_examples=100)
    def test_token_info_preserves_token_id(
        self,
        row_id: str,
        token_id: str,
        data: dict[str, Any],
    ) -> None:
        """Property: TokenInfo preserves token_id through construction."""
        token = TokenInfo(row_id=row_id, token_id=token_id, row_data=_wrap_dict_as_pipeline_row(data))
        assert token.token_id == token_id

    @given(
        row_id=non_empty_ids,
        token_id=non_empty_ids,
        data=row_data,
    )
    @settings(max_examples=100)
    def test_token_info_preserves_row_data(
        self,
        row_id: str,
        token_id: str,
        data: dict[str, Any],
    ) -> None:
        """Property: TokenInfo preserves row_data through construction."""
        token = TokenInfo(row_id=row_id, token_id=token_id, row_data=_wrap_dict_as_pipeline_row(data))
        assert token.row_data.to_dict() == data

    @given(
        row_id=non_empty_ids,
        token_id=non_empty_ids,
        data=row_data,
        path=lineage_paths,
    )
    @settings(max_examples=100)
    def test_token_info_preserves_optional_fields(
        self,
        row_id: str,
        token_id: str,
        data: dict[str, Any],
        path: tuple[LineageFrame, ...],
    ) -> None:
        """Property: TokenInfo preserves lineage_path, and its derived optional
        fields (ruling 21) read back the innermost FORK/EXPAND frame."""
        token = TokenInfo(
            row_id=row_id,
            token_id=token_id,
            row_data=_wrap_dict_as_pipeline_row(data),
            lineage_path=path,
        )
        assert token.lineage_path == path
        fork_frame = next((frame for frame in path if frame.kind is FrameKind.FORK), None)
        expand_frame = next((frame for frame in path if frame.kind is FrameKind.EXPAND), None)
        assert token.branch_name == (fork_frame.member_key if fork_frame is not None else None)
        assert token.fork_group_id == (fork_frame.group_id if fork_frame is not None else None)
        assert token.expand_group_id == (expand_frame.group_id if expand_frame is not None else None)


class TestTokenInfoJsonSerializationProperties:
    """Property tests for TokenInfo JSON serialization."""

    @given(
        row_id=non_empty_ids,
        token_id=non_empty_ids,
        data=row_data,
    )
    @settings(max_examples=100)
    def test_token_info_serializes_to_valid_json(
        self,
        row_id: str,
        token_id: str,
        data: dict[str, Any],
    ) -> None:
        """Property: TokenInfo serializes to valid JSON."""
        token = TokenInfo(row_id=row_id, token_id=token_id, row_data=_wrap_dict_as_pipeline_row(data))

        # _token_to_dict + json.dumps should not raise
        serialized = json.dumps(_token_to_dict(token))

        # Result should be valid JSON
        parsed = json.loads(serialized)
        assert isinstance(parsed, dict)

    @given(
        row_id=non_empty_ids,
        token_id=non_empty_ids,
        data=row_data,
    )
    @settings(max_examples=100)
    def test_token_info_json_round_trip_preserves_identity(
        self,
        row_id: str,
        token_id: str,
        data: dict[str, Any],
    ) -> None:
        """Property: TokenInfo JSON round-trip preserves identity fields."""
        token = TokenInfo(row_id=row_id, token_id=token_id, row_data=_wrap_dict_as_pipeline_row(data))

        serialized = json.dumps(_token_to_dict(token))
        parsed = json.loads(serialized)

        assert parsed["row_id"] == row_id
        assert parsed["token_id"] == token_id
        assert parsed["row_data"] == data

    @given(
        row_id=non_empty_ids,
        token_id=non_empty_ids,
        data=row_data,
        path=lineage_paths,
    )
    @settings(max_examples=100)
    def test_token_info_json_round_trip_preserves_optional_fields(
        self,
        row_id: str,
        token_id: str,
        data: dict[str, Any],
        path: tuple[LineageFrame, ...],
    ) -> None:
        """Property: TokenInfo JSON round-trip preserves the derived optional
        fields (ruling 21: read from lineage_path, not independently set)."""
        token = TokenInfo(
            row_id=row_id,
            token_id=token_id,
            row_data=_wrap_dict_as_pipeline_row(data),
            lineage_path=path,
        )

        serialized = json.dumps(_token_to_dict(token))
        parsed = json.loads(serialized)

        assert parsed["branch_name"] == token.branch_name
        assert parsed["fork_group_id"] == token.fork_group_id
        assert parsed["expand_group_id"] == token.expand_group_id


# =============================================================================
# TransformResult JSON Serialization Properties
# =============================================================================


class TestTransformResultJsonSerializationProperties:
    """Property tests for TransformResult JSON serialization."""

    @given(data=row_data)
    @settings(max_examples=100)
    def test_transform_result_success_serializes_to_valid_json(
        self,
        data: dict[str, Any],
    ) -> None:
        """Property: TransformResult.success() serializes to valid JSON."""
        result = TransformResult.success(make_pipeline_row(data), success_reason={"action": "test"})

        result_dict = asdict(result)
        result_dict["row"] = result.row.to_dict() if result.row is not None else None
        serialized = json.dumps(result_dict)
        parsed = json.loads(serialized)

        assert isinstance(parsed, dict)
        assert parsed["status"] == "success"

    @given(data=row_data, success_reason=success_reasons)
    @settings(max_examples=100)
    def test_transform_result_success_json_round_trip_preserves_row(
        self,
        data: dict[str, Any],
        success_reason: TransformSuccessReason,
    ) -> None:
        """Property: TransformResult.success() JSON round-trip preserves row."""
        result = TransformResult.success(make_pipeline_row(data), success_reason=success_reason)

        result_dict = asdict(result)
        result_dict["row"] = result.row.to_dict() if result.row is not None else None
        serialized = json.dumps(result_dict)
        parsed = json.loads(serialized)

        assert parsed["status"] == "success"
        assert parsed["row"] == data
        assert parsed["reason"] is None
        assert parsed["success_reason"] == success_reason

    @given(reason=transform_error_reasons)
    @settings(max_examples=100)
    def test_transform_result_error_json_round_trip_preserves_reason(
        self,
        reason: TransformErrorReason,
    ) -> None:
        """Property: TransformResult.error() JSON round-trip preserves reason."""
        result = TransformResult.error(reason)

        serialized = json.dumps(asdict(result))
        parsed = json.loads(serialized)

        assert parsed["status"] == "error"
        assert parsed["reason"] == reason
        assert parsed["row"] is None

    @given(data=row_data, retryable=st.booleans())
    @settings(max_examples=50)
    def test_transform_result_error_json_round_trip_preserves_retryable(
        self,
        data: dict[str, Any],
        retryable: bool,
    ) -> None:
        """Property: TransformResult.error() JSON round-trip preserves retryable flag."""
        result = TransformResult.error({"reason": "test_error"}, retryable=retryable)

        serialized = json.dumps(asdict(result))
        parsed = json.loads(serialized)

        assert parsed["retryable"] is retryable

    @given(rows=st.lists(row_data, min_size=1, max_size=5), success_reason=success_reasons)
    @settings(max_examples=100)
    def test_transform_result_success_multi_json_round_trip_preserves_rows(
        self,
        rows: list[dict[str, Any]],
        success_reason: dict[str, Any],
    ) -> None:
        """Property: TransformResult.success_multi() JSON round-trip preserves rows.

        All rows share a single contract instance (required by success_multi).
        """
        from elspeth.testing import make_contract

        all_keys: dict[str, type] = {}
        for r in rows:
            all_keys.update(dict.fromkeys(r, object))
        contract = make_contract(fields=all_keys) if all_keys else make_contract()

        pipeline_rows = [PipelineRow(r, contract) for r in rows]
        result = TransformResult.success_multi(
            pipeline_rows,
            success_reason=cast(TransformSuccessReason, success_reason),
        )

        # Serialize: convert PipelineRows to dicts before JSON encoding
        result_dict = asdict(result)
        result_dict["rows"] = [r.to_dict() for r in result.rows] if result.rows else None
        serialized = json.dumps(result_dict)
        parsed = json.loads(serialized)

        assert parsed["status"] == "success"
        assert parsed["rows"] is not None
        assert len(parsed["rows"]) == len(rows)
        for orig, parsed_row in zip(rows, parsed["rows"], strict=True):
            assert parsed_row == orig
        assert parsed["row"] is None
        assert parsed["success_reason"] == success_reason


# =============================================================================
# RoutingAction JSON Serialization Properties
# =============================================================================


class TestRoutingActionJsonSerializationProperties:
    """Property tests for RoutingAction JSON serialization."""

    @given(reason=routing_reasons)
    @settings(max_examples=100)
    def test_routing_action_continue_serializes_to_valid_json(
        self,
        reason: ConfigGateReason | None,
    ) -> None:
        """Property: RoutingAction.continue_() serializes to valid JSON."""
        action = RoutingAction.continue_(reason=reason)

        # Custom serialization needed for frozen dataclass with MappingProxyType
        serialized = json.dumps(_routing_action_to_dict(action))
        parsed = json.loads(serialized)

        assert isinstance(parsed, dict)
        assert parsed["kind"] == RoutingKind.CONTINUE

    @given(reason=routing_reasons)
    @settings(max_examples=100)
    def test_routing_action_continue_json_round_trip_preserves_invariants(
        self,
        reason: ConfigGateReason | None,
    ) -> None:
        """Property: RoutingAction.continue_() JSON round-trip preserves invariants."""
        action = RoutingAction.continue_(reason=reason)

        serialized = json.dumps(_routing_action_to_dict(action))
        parsed = json.loads(serialized)

        assert parsed["kind"] == RoutingKind.CONTINUE
        assert parsed["destinations"] == []
        assert parsed["mode"] == RoutingMode.MOVE

    @given(label=path_names, reason=routing_reasons)
    @settings(max_examples=100)
    def test_routing_action_route_json_round_trip_preserves_destination(
        self,
        label: str,
        reason: ConfigGateReason | None,
    ) -> None:
        """Property: RoutingAction.route() JSON round-trip preserves destination."""
        action = RoutingAction.route(label, reason=reason)

        serialized = json.dumps(_routing_action_to_dict(action))
        parsed = json.loads(serialized)

        assert parsed["kind"] == RoutingKind.ROUTE
        assert parsed["destinations"] == [label]
        assert parsed["mode"] == RoutingMode.MOVE

    @given(paths=unique_path_lists, reason=routing_reasons)
    @settings(max_examples=100)
    def test_routing_action_fork_json_round_trip_preserves_paths(
        self,
        paths: list[str],
        reason: ConfigGateReason | None,
    ) -> None:
        """Property: RoutingAction.fork_to_paths() JSON round-trip preserves paths."""
        action = RoutingAction.fork_to_paths(paths, reason=reason)

        serialized = json.dumps(_routing_action_to_dict(action))
        parsed = json.loads(serialized)

        assert parsed["kind"] == RoutingKind.FORK_TO_PATHS
        assert parsed["destinations"] == paths
        assert parsed["mode"] == RoutingMode.COPY


class TestRoutingActionReasonSerializationProperties:
    """Property tests for RoutingAction reason field serialization."""

    @given(reason=routing_reasons)
    @settings(max_examples=100)
    def test_routing_action_reason_json_round_trip(
        self,
        reason: ConfigGateReason | None,
    ) -> None:
        """Property: RoutingAction reason field JSON round-trips correctly."""
        action = RoutingAction.continue_(reason=reason)

        serialized = json.dumps(_routing_action_to_dict(action))
        parsed = json.loads(serialized)

        # RoutingAction preserves None as None, dicts are deep-copied
        assert parsed["reason"] == reason

    def test_routing_action_empty_reason_serializes(self) -> None:
        """Property: None reason serializes to null."""
        action = RoutingAction.continue_()

        serialized = json.dumps(_routing_action_to_dict(action))
        parsed = json.loads(serialized)

        assert parsed["reason"] is None


# =============================================================================
# Helper Functions
# =============================================================================


def _routing_action_to_dict(action: RoutingAction) -> dict[str, Any]:
    """Convert RoutingAction to JSON-serializable dict.

    RoutingAction uses tuple for destinations which needs conversion
    for standard JSON serialization.
    """
    return {
        "kind": action.kind.value,
        "destinations": list(action.destinations),
        "mode": action.mode.value,
        "reason": deep_thaw(action.reason) if action.reason is not None else None,
    }
