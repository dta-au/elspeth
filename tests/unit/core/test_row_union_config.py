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


class TestRowUnionSettingsIdentifierGuards:
    """Identifier hardening at parity with CoalesceSettings.

    row_union shipped with none of coalesce's field validators, so names,
    branch keys/values and on_success accepted whitespace, ``__`` system
    prefixes, reserved edge labels and unbounded lengths.
    """

    @pytest.mark.parametrize("bad_name", ["", "   ", "\t"])
    def test_rejects_empty_or_whitespace_name(self, bad_name: str) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            RowUnionSettings(name=bad_name, branches=["a", "b"], on_success="out")

    def test_name_is_trimmed(self) -> None:
        settings = RowUnionSettings(name="  variant_union  ", branches=["a", "b"], on_success="out")
        assert settings.name == "variant_union"

    def test_rejects_dunder_prefixed_name(self) -> None:
        with pytest.raises(ValidationError, match="__"):
            RowUnionSettings(name="__internal", branches=["a", "b"], on_success="out")

    @pytest.mark.parametrize("reserved", ["continue", "fork", "on_success"])
    def test_rejects_reserved_name(self, reserved: str) -> None:
        with pytest.raises(ValidationError, match="reserved"):
            RowUnionSettings(name=reserved, branches=["a", "b"], on_success="out")

    def test_rejects_overlong_name(self) -> None:
        with pytest.raises(ValidationError, match="max length"):
            RowUnionSettings(name="u" * 39, branches=["a", "b"], on_success="out")

    def test_rejects_invalid_name_characters(self) -> None:
        with pytest.raises(ValidationError, match="invalid characters"):
            RowUnionSettings(name="variant union", branches=["a", "b"], on_success="out")

    @pytest.mark.parametrize("bad_branch", ["", "   "])
    def test_rejects_empty_branch_name(self, bad_branch: str) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            RowUnionSettings(name="u", branches={bad_branch: "conn", "b": "b"}, on_success="out")

    @pytest.mark.parametrize("bad_conn", ["", "   "])
    def test_rejects_empty_branch_input_connection(self, bad_conn: str) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            RowUnionSettings(name="u", branches={"a": bad_conn, "b": "b"}, on_success="out")

    def test_branch_keys_and_values_are_trimmed(self) -> None:
        settings = RowUnionSettings(
            name="u",
            branches={"  control  ": "  control_scored  ", "treatment": "treatment_scored"},
            on_success="out",
        )
        assert settings.branches == {"control": "control_scored", "treatment": "treatment_scored"}

    def test_rejects_dunder_branch_name(self) -> None:
        with pytest.raises(ValidationError, match="__"):
            RowUnionSettings(name="u", branches={"__system": "conn", "b": "b"}, on_success="out")

    def test_rejects_reserved_branch_name(self) -> None:
        with pytest.raises(ValidationError, match="reserved"):
            RowUnionSettings(name="u", branches={"continue": "conn", "b": "b"}, on_success="out")

    def test_rejects_dunder_branch_input_connection(self) -> None:
        with pytest.raises(ValidationError, match="__"):
            RowUnionSettings(name="u", branches={"a": "__system", "b": "b"}, on_success="out")

    def test_rejects_overlong_branch_name(self) -> None:
        with pytest.raises(ValidationError, match="max length"):
            RowUnionSettings(name="u", branches={"a" * 65: "conn", "b": "b"}, on_success="out")

    def test_rejects_branch_keys_that_collide_after_trim(self) -> None:
        # min_length=2 is checked on the raw dict, so untrimmed keys could
        # silently collapse a 2-branch group into 1 — a group-size corruption.
        with pytest.raises(ValidationError, match="collide"):
            RowUnionSettings(name="u", branches={"a": "x", " a": "y"}, on_success="out")

    @pytest.mark.parametrize("bad_on_success", ["", "   "])
    def test_rejects_empty_on_success(self, bad_on_success: str) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            RowUnionSettings(name="u", branches=["a", "b"], on_success=bad_on_success)

    def test_on_success_is_trimmed(self) -> None:
        settings = RowUnionSettings(name="u", branches=["a", "b"], on_success="  union_out  ")
        assert settings.on_success == "union_out"

    def test_rejects_dunder_on_success(self) -> None:
        with pytest.raises(ValidationError, match="__"):
            RowUnionSettings(name="u", branches=["a", "b"], on_success="__system")

    def test_rejects_reserved_on_success(self) -> None:
        with pytest.raises(ValidationError, match="reserved"):
            RowUnionSettings(name="u", branches=["a", "b"], on_success="continue")

    def test_rejects_overlong_on_success(self) -> None:
        with pytest.raises(ValidationError, match="max length"):
            RowUnionSettings(name="u", branches=["a", "b"], on_success="o" * 65)

    def test_rejects_infinite_timeout(self) -> None:
        # `gt=0` lets `inf` through, and check_timeouts compares `elapsed > inf`
        # — so an infinite timeout silently disables the whole timeout sweep.
        with pytest.raises(ValidationError, match="finite"):
            RowUnionSettings(name="u", branches=["a", "b"], on_success="out", timeout_seconds=float("inf"))

    def test_rejects_nan_timeout(self) -> None:
        with pytest.raises(ValidationError):
            RowUnionSettings(name="u", branches=["a", "b"], on_success="out", timeout_seconds=float("nan"))


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
