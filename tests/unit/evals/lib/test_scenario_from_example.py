"""Tests for panel/RGR scenario criteria derivation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from evals.lib.scenario_from_example import build_criteria_from_target


def test_build_criteria_pins_output_plugins_from_structural_target() -> None:
    target: dict[str, Any] = {
        "source": {"plugin": "csv", "shape_token": "csv", "columns": ["id", "approved"]},
        "transforms": [],
        "gates": [
            {
                "name": "approval_check",
                "condition": "row['approved'] == 'true'",
                "is_fork": False,
                "fork_paths": [],
                "routes": {"true": "approved", "false": "rejected"},
            }
        ],
        "aggregations": [],
        "coalesce_nodes": [],
        "sinks": [
            {"name": "approved", "plugin": "csv", "shape_token": "csv"},
            {"name": "rejected", "plugin": "csv", "shape_token": "csv"},
        ],
    }

    criteria = build_criteria_from_target(target)

    assert criteria["green_criteria"]["must_have_output_plugins"] == ["csv", "csv"]


# --------------------------------------------------------------------------
# _extract_source — plural ``sources:`` map (ADR-025), legacy fallback, hard-fail
# --------------------------------------------------------------------------


def _fixed_csv(path: str, fields: list[str]) -> dict[str, Any]:
    return {"plugin": "csv", "on_success": "raw", "options": {"path": path, "schema": {"mode": "fixed", "fields": fields}}}


def test_extract_source_reads_plural_sources_map() -> None:
    from evals.lib.scenario_from_example import _extract_source

    doc = {"sources": {"primary": _fixed_csv("in.csv", ["id: int", "product: str"])}}

    src = _extract_source(doc)

    assert src["plugin"] == "csv"
    assert src["name"] == "primary"
    assert src["source_count"] == 1
    assert src["path"] == "in.csv"
    assert src["schema_mode"] == "fixed"
    assert src["columns"] == ["id", "product"]


def test_extract_source_multi_source_takes_first_declared_and_counts() -> None:
    from evals.lib.scenario_from_example import _extract_source

    doc = {"sources": {"orders": _fixed_csv("orders.csv", ["id: int"]), "refunds": _fixed_csv("refunds.csv", ["id: int"])}}

    src = _extract_source(doc)

    assert src["name"] == "orders"
    assert src["source_count"] == 2
    assert src["path"] == "orders.csv"


def test_extract_source_falls_back_to_legacy_singular_key() -> None:
    from evals.lib.scenario_from_example import _extract_source

    src = _extract_source({"source": _fixed_csv("legacy.csv", ["a: str"])})

    assert src["plugin"] == "csv"
    assert src["name"] is None
    assert src["source_count"] == 1
    assert src["columns"] == ["a"]


@pytest.mark.parametrize("doc", [{}, {"sources": {}}, {"sources": {"primary": {"options": {}}}}, {"source": {"plugin": ""}}])
def test_extract_source_raises_when_no_source_plugin(doc: dict[str, Any]) -> None:
    """A null source must be an error, never a silently weaker target."""
    from evals.lib.scenario_from_example import _extract_source

    with pytest.raises(ValueError, match="no source plugin"):
        _extract_source(doc)


def test_every_plain_example_settings_yields_a_source_plugin() -> None:
    """Truth-test over the tracked corpus: no example may extract a null source.

    Guards the 2026-08-16 regression where every ``examples/*/settings.yaml``
    (all plural ``sources:``) extracted ``plugin: None`` and every derived
    chain silently dropped its source token.
    """
    from evals.lib.scenario_from_example import extract_structural_target

    root = Path(__file__).resolve().parents[4] / "examples"
    plain = sorted(p.parent for p in root.glob("*/settings.yaml"))
    assert len(plain) >= 20, plain
    null_sources = []
    for example_dir in plain:
        try:
            target = extract_structural_target(example_dir)
        except ValueError as exc:  # a real settings file with no source is itself a finding
            null_sources.append((example_dir.name, str(exc)))
            continue
        if not target["source"].get("plugin"):
            null_sources.append((example_dir.name, "None"))
    assert not null_sources, null_sources
