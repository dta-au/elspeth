"""Tests for the DAG package facade."""

from __future__ import annotations

import pytest


def test_dag_facade_does_not_export_coalesce_merge_internals() -> None:
    import elspeth.core.dag as dag

    internal_helpers = {
        "merge_guaranteed_fields",
        "merge_union_contracts",
        "merge_union_fields",
    }

    assert internal_helpers.isdisjoint(dag.__all__)
    for helper_name in internal_helpers:
        assert not hasattr(dag, helper_name)
        with pytest.raises(ImportError):
            exec(f"from elspeth.core.dag import {helper_name}", {})


def test_coalesce_merge_module_does_not_reexport_runtime_contract_merge() -> None:
    """merge_union_contracts is runtime contract API (elspeth.contracts.union_merge).

    The build-time DAG helper must not act as a discoverability facade for it
    (elspeth-8eb284dc58).
    """
    import elspeth.core.dag.coalesce_merge as coalesce_merge

    assert "merge_union_contracts" not in coalesce_merge.__all__
    assert not hasattr(coalesce_merge, "merge_union_contracts")
    with pytest.raises(ImportError):
        exec("from elspeth.core.dag.coalesce_merge import merge_union_contracts", {})
