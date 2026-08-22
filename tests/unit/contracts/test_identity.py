"""Tests for identity contracts."""

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.identity import (
    LineageFrame,
    TokenInfo,
    innermost_expand_frame,
    innermost_fork_frame,
    lineage_path_from_json,
    lineage_path_to_json,
    path_branch_name,
    path_expand_group_id,
    path_fork_group_id,
    pop_closer_frame,
)
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.testing import make_field


def _make_contract() -> SchemaContract:
    """Create a minimal schema contract for testing."""
    return SchemaContract(
        mode="FLEXIBLE",
        fields=(make_field("field", str, original_name="field", source="declared"),),
        locked=True,
    )


class TestTokenInfo:
    """Tests for TokenInfo."""

    def test_create_token_info(self) -> None:
        """Can create TokenInfo with required fields."""
        contract = _make_contract()
        pipeline_row = PipelineRow({"field": "value"}, contract)

        token = TokenInfo(
            row_id="row-123",
            token_id="tok-456",
            row_data=pipeline_row,
        )

        assert token.row_id == "row-123"
        assert token.token_id == "tok-456"
        assert token.row_data["field"] == "value"
        assert token.row_data is pipeline_row, "row_data must be a reference to pipeline_row, not a copy"
        assert token.branch_name is None

    def test_token_info_with_branch(self) -> None:
        """branch_name is derived (ruling 21) from the innermost FORK frame in lineage_path."""
        contract = _make_contract()
        pipeline_row = PipelineRow({}, contract)

        token = TokenInfo(
            row_id="row-123",
            token_id="tok-456",
            row_data=pipeline_row,
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="sentiment"),),
        )

        assert token.branch_name == "sentiment"

    def test_rejects_empty_row_id(self) -> None:
        """TokenInfo rejects empty row_id at construction time."""
        contract = _make_contract()
        pipeline_row = PipelineRow({}, contract)

        with pytest.raises(ValueError, match="row_id must not be empty"):
            TokenInfo(row_id="", token_id="tok-1", row_data=pipeline_row)

    def test_rejects_empty_token_id(self) -> None:
        """TokenInfo rejects empty token_id at construction time."""
        contract = _make_contract()
        pipeline_row = PipelineRow({}, contract)

        with pytest.raises(ValueError, match="token_id must not be empty"):
            TokenInfo(row_id="row-1", token_id="", row_data=pipeline_row)

    def test_token_info_row_data_immutable(self) -> None:
        """TokenInfo.row_data (PipelineRow) is immutable for audit integrity."""
        contract = _make_contract()
        pipeline_row = PipelineRow({"field": "value"}, contract)

        token = TokenInfo(row_id="r", token_id="t", row_data=pipeline_row)

        # PipelineRow should raise TypeError on modification attempt
        with pytest.raises(TypeError, match="immutable"):
            token.row_data["field"] = "modified"

    def test_token_info_is_frozen(self) -> None:
        """TokenInfo is immutable — field assignment raises FrozenInstanceError."""
        contract = _make_contract()
        pipeline_row = PipelineRow({}, contract)

        token = TokenInfo(row_id="r", token_id="t", row_data=pipeline_row)
        # branch_name is a derived, read-only property (ruling 21) — not a
        # dataclass field — so its own frozen-enforcement coverage lives at
        # the lineage_path field below, not a direct property assignment.
        with pytest.raises(FrozenInstanceError):
            token.lineage_path = ()  # type: ignore[misc]  # testing frozen enforcement
        with pytest.raises(FrozenInstanceError):
            token.row_id = "new_row_id"  # type: ignore[misc]  # testing frozen enforcement

    def test_with_updated_data_preserves_lineage(self) -> None:
        """with_updated_data() preserves all lineage fields."""
        contract = _make_contract()
        original_row = PipelineRow({"field": "original"}, contract)
        updated_row = PipelineRow({"field": "updated"}, contract)

        original = TokenInfo(
            row_id="row-1",
            token_id="tok-1",
            row_data=original_row,
            lineage_path=(
                LineageFrame(kind=FrameKind.FORK, group_id="fork-123", member_key="path_a"),
                LineageFrame(kind=FrameKind.EXPAND, group_id="expand-789", member_key="tok-child"),
            ),
        )

        updated = original.with_updated_data(updated_row)

        # Data changed
        assert updated.row_data["field"] == "updated"
        assert original.row_data["field"] == "original"

        # All lineage preserved
        assert updated.row_id == "row-1"
        assert updated.token_id == "tok-1"
        assert updated.branch_name == "path_a"
        assert updated.fork_group_id == "fork-123"
        assert updated.expand_group_id == "expand-789"

    def test_with_updated_data_preserves_resume_fields(self) -> None:
        """resume_attempt_offset and resume_checkpoint_id survive with_updated_data().

        These are the propagation fields for mid-DAG resume re-drives (ADDENDUM 4).
        Dropping them in with_updated_data would cause a node_states collision one node
        downstream on resume. This test mechanically enforces the dataclasses.replace
        propagation that the docstring describes.
        """
        contract = _make_contract()
        original = TokenInfo(
            row_id="row-1",
            token_id="tok-1",
            row_data=PipelineRow({"field": "original"}, contract),
            resume_attempt_offset=3,
            resume_checkpoint_id="ck-abc",
        )
        updated = original.with_updated_data(PipelineRow({"field": "updated"}, contract))
        assert updated.resume_attempt_offset == 3
        assert updated.resume_checkpoint_id == "ck-abc"

    def test_lineage_path_defaults_empty_and_survives_with_updated_data(self) -> None:
        contract = _make_contract()
        path = (LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a"),)
        token = TokenInfo(
            row_id="row-1",
            token_id="tok-1",
            row_data=PipelineRow({"field": "v"}, contract),
            lineage_path=path,
        )
        assert token.lineage_path == path
        updated = token.with_updated_data(PipelineRow({"field": "w"}, contract))
        assert updated.lineage_path == path
        bare = TokenInfo(row_id="row-1", token_id="tok-2", row_data=PipelineRow({"field": "v"}, contract))
        assert bare.lineage_path == ()

    @pytest.mark.parametrize(
        "bad_path",
        [
            pytest.param([LineageFrame(kind=FrameKind.FORK, group_id="g", member_key="m")], id="list-not-tuple"),
            pytest.param((("fork", "g", "m"),), id="raw-tuple-entry"),
        ],
    )
    def test_lineage_path_rejects_untyped_values(self, bad_path: object) -> None:
        with pytest.raises(TypeError):
            TokenInfo(
                row_id="row-1",
                token_id="tok-1",
                row_data=PipelineRow({"field": "v"}, _make_contract()),
                lineage_path=bad_path,  # type: ignore[arg-type]
            )

    def test_token_info_has_no_join_group_id(self) -> None:
        """§4.1 / ruling 20: a merge is an event, not a membership — the join
        context rides RowResult/PendingOutcome/WorkItem carriers, never TokenInfo."""
        import dataclasses

        assert "join_group_id" not in {f.name for f in dataclasses.fields(TokenInfo)}


# TestTokenInfoLineageFieldGuards (empty-string branch_name/fork_group_id/
# expand_group_id rejected at TokenInfo construction) is retired by the WS1b
# flip: those three are no longer TokenInfo constructor kwargs at all — they
# are derived, read-only properties over lineage_path (ruling 21). A
# TokenInfo literally cannot be built with an empty member_key/group_id
# anymore: LineageFrame.__post_init__ refuses it before TokenInfo ever sees
# the value — see TestLineageFrame::test_frame_rejects_bad_fields below.
# TestTokenInfo::test_token_info_with_branch and
# ::test_with_updated_data_preserves_lineage cover the None/non-empty
# derived-property reads that remain meaningful at this layer.


class TestTokenInfoResumeOffsetInvariant:
    """One-way invariant: resume_attempt_offset > 0 ⟹ resume_checkpoint_id is not None.

    A positive offset only originates from a resume re-drive (which always stamps a
    checkpoint id). Offset 0 is deliberately ambiguous — it covers both a run-1 token
    AND a never-stepped token re-driven on resume (max_attempt -1 → offset 0) — so the
    converse is NOT required. The authoritative resume marker is resume_checkpoint_id,
    not the offset (see explain()).
    """

    def _kwargs(self, **overrides: Any) -> dict[str, Any]:
        contract = _make_contract()
        base: dict[str, Any] = {
            "row_id": "r1",
            "token_id": "t1",
            "row_data": PipelineRow({"x": 1}, contract=contract),
        }
        base.update(overrides)
        return base

    def test_positive_offset_without_checkpoint_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="resume_attempt_offset=1 > 0 requires a resume_checkpoint_id"):
            TokenInfo(**self._kwargs(resume_attempt_offset=1, resume_checkpoint_id=None))

    def test_positive_offset_with_checkpoint_id_accepted(self) -> None:
        t = TokenInfo(**self._kwargs(resume_attempt_offset=1, resume_checkpoint_id="ck-1"))
        assert t.resume_attempt_offset == 1
        assert t.resume_checkpoint_id == "ck-1"

    def test_bool_offset_rejected(self) -> None:
        with pytest.raises(TypeError, match=r"TokenInfo\.resume_attempt_offset must be int.*got bool"):
            TokenInfo(**self._kwargs(resume_attempt_offset=True, resume_checkpoint_id="ck-1"))

    def test_negative_offset_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"TokenInfo\.resume_attempt_offset must be >= 0.*got -1"):
            TokenInfo(**self._kwargs(resume_attempt_offset=-1))

    def test_zero_offset_without_checkpoint_id_accepted(self) -> None:
        # Ambiguous-but-valid: a run-1 token, OR a never-stepped token re-driven on resume.
        t = TokenInfo(**self._kwargs(resume_attempt_offset=0, resume_checkpoint_id=None))
        assert t.resume_attempt_offset == 0
        assert t.resume_checkpoint_id is None

    def test_zero_offset_with_checkpoint_id_accepted(self) -> None:
        # A genuine resume re-drive of a token never stepped before the interrupt
        # (max_attempt -1 → offset 0) still carries the checkpoint id. This is the
        # exact case the old "0 = not a resume re-drive" comment got wrong.
        t = TokenInfo(**self._kwargs(resume_attempt_offset=0, resume_checkpoint_id="ck-1"))
        assert t.resume_attempt_offset == 0
        assert t.resume_checkpoint_id == "ck-1"


class TestLineageFrame:
    def test_frame_construction_and_freeze(self) -> None:
        frame = LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a")
        assert frame.kind is FrameKind.FORK
        with pytest.raises(FrozenInstanceError):
            frame.group_id = "other"  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("kind", "group_id", "member_key"),
        [
            pytest.param("fork", "fg-1", "path_a", id="kind-is-a-bare-string"),
            pytest.param(FrameKind.FORK, "", "path_a", id="empty-group-id"),
            pytest.param(FrameKind.EXPAND, "eg-1", "", id="empty-member-key"),
            pytest.param(FrameKind.EXPAND, None, "m", id="none-group-id"),
        ],
    )
    def test_frame_rejects_bad_fields(self, kind: object, group_id: object, member_key: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            LineageFrame(kind=kind, group_id=group_id, member_key=member_key)  # type: ignore[arg-type]

    def test_json_round_trip_outermost_first(self) -> None:
        path = (
            LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-9"),
            LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a"),
        )
        assert lineage_path_from_json(lineage_path_to_json(path)) == path
        assert lineage_path_from_json(lineage_path_to_json(())) == ()

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("not json", id="not-json"),
            pytest.param('{"a": 1}', id="not-a-list"),
            pytest.param('[["fork", "fg-1"]]', id="two-element-frame"),
            pytest.param('[["merge", "g", "m"]]', id="unknown-kind"),
        ],
    )
    def test_json_rejects_corrupt_payloads(self, raw: str) -> None:
        with pytest.raises(ValueError):
            lineage_path_from_json(raw)

    def test_innermost_helpers_pick_the_innermost_of_each_kind(self) -> None:
        outer_fork = LineageFrame(kind=FrameKind.FORK, group_id="fg-outer", member_key="a")
        expand = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-1")
        inner_fork = LineageFrame(kind=FrameKind.FORK, group_id="fg-inner", member_key="b")
        path = (outer_fork, expand, inner_fork)
        assert innermost_fork_frame(path) is inner_fork
        assert innermost_expand_frame(path) is expand
        assert innermost_fork_frame(()) is None
        assert innermost_expand_frame((outer_fork,)) is None

    def test_path_wrappers_derive_the_retiring_stored_fields(self) -> None:
        outer_fork = LineageFrame(kind=FrameKind.FORK, group_id="fg-outer", member_key="a")
        expand = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-1")
        inner_fork = LineageFrame(kind=FrameKind.FORK, group_id="fg-inner", member_key="b")
        path = (outer_fork, expand, inner_fork)
        assert path_branch_name(path) == "b"
        assert path_fork_group_id(path) == "fg-inner"
        assert path_expand_group_id(path) == "eg-1"
        assert path_branch_name(()) is None
        assert path_fork_group_id(()) is None
        assert path_expand_group_id((outer_fork,)) is None


class TestPopCloserFrame:
    def test_pops_exactly_the_matching_innermost_frame(self) -> None:
        outer = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-1")
        inner = LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="a")
        assert pop_closer_frame((outer, inner), kind=FrameKind.FORK, group_id="fg-1") == (outer,)
        assert pop_closer_frame((outer,), kind=FrameKind.EXPAND, group_id="eg-1") == ()

    @pytest.mark.parametrize(
        ("path", "kind", "group_id"),
        [
            pytest.param((), FrameKind.FORK, "fg-1", id="empty-path"),
            pytest.param(
                (LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="t"),),
                FrameKind.FORK,
                "eg-1",
                id="wrong-kind",
            ),
            pytest.param(
                (LineageFrame(kind=FrameKind.FORK, group_id="fg-2", member_key="a"),),
                FrameKind.FORK,
                "fg-1",
                id="wrong-group",
            ),
            pytest.param(
                (
                    LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="a"),
                    LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="t"),
                ),
                FrameKind.FORK,
                "fg-1",
                id="matching-frame-buried-not-innermost",
            ),
        ],
    )
    def test_refuses_any_non_matching_innermost_frame(self, path: tuple[LineageFrame, ...], kind: FrameKind, group_id: str) -> None:
        with pytest.raises(OrchestrationInvariantError, match="innermost"):
            pop_closer_frame(path, kind=kind, group_id=group_id)


class TestTokenInfoExtraFields:
    """Tests covering two specific extras on TokenInfo.row_data:

    - The schema contract is reachable via ``row_data.contract``.
    - ``PipelineRow.to_dict()`` round-trips non-contract (extra) fields
      attached during pipeline execution.
    """

    def test_row_data_contract_accessible(self) -> None:
        """The schema contract is reachable via token.row_data.contract."""
        contract = SchemaContract(
            mode="FIXED",
            fields=(
                make_field(
                    "amount",
                    int,
                    original_name="'Amount'",
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )
        pipeline_row = PipelineRow({"amount": 100}, contract)

        token = TokenInfo(
            row_id="row_001",
            token_id="token_001",
            row_data=pipeline_row,
        )

        assert token.row_data.contract is contract
        assert token.row_data.contract.mode == "FIXED"

    def test_pipeline_row_to_dict_includes_extra_fields(self) -> None:
        """to_dict() returns ALL fields, not just contract-declared ones.

        Pipeline execution can attach extras (computed fields, nested objects)
        beyond the declared schema. to_dict() must round-trip those, otherwise
        downstream sinks lose data the audit trail needs.
        """
        contract = SchemaContract(
            mode="FIXED",
            fields=(
                make_field(
                    "amount",
                    int,
                    original_name="'Amount'",
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )
        data_with_extras = {"amount": 100, "computed_field": "extra", "nested": {"a": 1}}
        pipeline_row = PipelineRow(data_with_extras, contract)

        result = pipeline_row.to_dict()

        assert result["amount"] == 100
        assert result["computed_field"] == "extra"
        assert result["nested"] == {"a": 1}
