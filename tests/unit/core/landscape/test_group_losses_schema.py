"""Schema contract for the unified group_losses ledger (spec §4.3/§6.2)."""

import dataclasses

import pytest
from sqlalchemy import inspect

from elspeth.contracts.scheduler import GroupLossSpec
from tests.fixtures.landscape import make_landscape_db


def test_group_loss_spec_is_frozen_and_five_fields():
    spec = GroupLossSpec(
        closer_name="merge_paths",
        group_id="fg_001",
        member_key="path_a",
        token_id="tok_001",
        reason="quarantined",
    )
    assert [f.name for f in dataclasses.fields(spec)] == [
        "closer_name",
        "group_id",
        "member_key",
        "token_id",
        "reason",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.reason = "other"  # type: ignore[misc]


def test_group_losses_table_shape_and_natural_key():
    db = make_landscape_db()
    inspector = inspect(db.engine)
    assert "group_losses" in inspector.get_table_names()
    assert "coalesce_branch_losses" not in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("group_losses")}
    assert columns == {
        "loss_id",
        "run_id",
        "closer_name",
        "group_id",
        "member_key",
        "token_id",
        "reason",
        "recorded_by",
        "recorded_at",
        "adopted_epoch",
    }
    indexes = {ix["name"]: ix for ix in inspector.get_indexes("group_losses")}
    natural = indexes["uq_group_losses_natural"]
    assert natural["unique"]
    assert natural["column_names"] == ["run_id", "closer_name", "group_id", "member_key"]
