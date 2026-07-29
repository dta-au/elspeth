"""RowUnionSettings config surface (row_union v1, elspeth-a5b86149d4).

row_union is a correlated same-row_id N->N UNION ALL barrier: require_all
only, pass-through payloads, released group continues on one declared
on_success connection. The settings model therefore has no policy/merge
axes — just name, ordered branches, on_success, and an optional timeout.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from elspeth.core.config import ElspethSettings, RowUnionSettings

_MINIMAL = {
    "sources": {"primary": {"plugin": "csv", "on_success": "rows"}},
    "sinks": {"output": {"plugin": "csv", "on_write_failure": "discard"}},
}


class TestRowUnionSettings:
    def test_list_branches_normalize_to_identity_dict(self) -> None:
        settings = RowUnionSettings(
            name="variant_union",
            branches=["control_branch", "treatment_branch"],
            on_success="experiment_in",
        )
        assert settings.branches == {
            "control_branch": "control_branch",
            "treatment_branch": "treatment_branch",
        }
        assert settings.on_success == "experiment_in"
        assert settings.timeout_seconds is None

    def test_dict_branches_map_to_input_connections(self) -> None:
        settings = RowUnionSettings(
            name="variant_union",
            branches={"control_branch": "control_scored", "treatment_branch": "treatment_scored"},
            on_success="experiment_in",
        )
        assert settings.branches["control_branch"] == "control_scored"

    def test_declared_branch_order_is_preserved(self) -> None:
        settings = RowUnionSettings(
            name="variant_union",
            branches=["b_branch", "a_branch", "c_branch"],
            on_success="out",
        )
        assert list(settings.branches) == ["b_branch", "a_branch", "c_branch"]

    def test_rejects_fewer_than_two_branches(self) -> None:
        with pytest.raises(ValidationError):
            RowUnionSettings(name="u", branches=["only_one"], on_success="out")

    def test_rejects_duplicate_list_branches(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate"):
            RowUnionSettings(name="u", branches=["a", "a"], on_success="out")

    def test_requires_on_success(self) -> None:
        with pytest.raises(ValidationError):
            RowUnionSettings(name="u", branches=["a", "b"])

    def test_rejects_nonpositive_timeout(self) -> None:
        with pytest.raises(ValidationError):
            RowUnionSettings(name="u", branches=["a", "b"], on_success="out", timeout_seconds=0)

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            RowUnionSettings(name="u", branches=["a", "b"], on_success="out", merge="union")


class TestElspethSettingsRowUnions:
    def test_row_unions_section_parses(self) -> None:
        settings = ElspethSettings(
            **_MINIMAL,
            row_unions=[{"name": "variant_union", "branches": ["a", "b"], "on_success": "out"}],
        )
        assert settings.row_unions[0].name == "variant_union"

    def test_defaults_to_empty(self) -> None:
        settings = ElspethSettings(**_MINIMAL)
        assert settings.row_unions == []

    def test_node_name_collision_with_coalesce_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unique across"):
            ElspethSettings(
                **_MINIMAL,
                coalesce=[{"name": "merge_point", "branches": ["a", "b"]}],
                row_unions=[{"name": "merge_point", "branches": ["c", "d"], "on_success": "out"}],
            )
