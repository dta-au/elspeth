"""Config-surface tests for collectors:/scopes: (barrier-scopes spec §3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from elspeth.core.config import (
    CollectorSettings,
    ScopeSettings,
    load_settings_from_config_dict,
)

_MINIMAL = {
    "sources": {"main": {"plugin": "csv", "options": {"path": "in.csv", "schema": {"mode": "observed"}}, "on_success": "rows"}},
    "sinks": {"out": {"plugin": "json", "options": {"path": "out.json"}, "on_write_failure": "discard"}},
    "transforms": [
        {
            "name": "explode",
            "plugin": "json_explode",
            "input": "rows",
            "on_success": "pages",
            "on_error": "discard",
            "options": {"field": "items", "schema": {"mode": "observed"}},
        },
        # A second, distinct opener candidate — review round 1: needed so the
        # closer-collision test can collide on closer alone (different opener
        # per scope) rather than colliding on both axes at once.
        {
            "name": "explode2",
            "plugin": "json_explode",
            "input": "rows",
            "on_success": "pages2",
            "on_error": "discard",
            "options": {"field": "items", "schema": {"mode": "observed"}},
        },
    ],
}


def _with_collector_scope(**scope_overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        **_MINIMAL,
        "collectors": [
            {
                "name": "page_stitcher",
                "plugin": "batch_stats",
                "input": "pages",
                "on_success": "out",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
        ],
        "scopes": [
            {"name": "document_pages", "opener": "explode", "closer": "page_stitcher", "policy": "require_all", **scope_overrides},
        ],
    }
    return doc


class TestCollectorSettings:
    def test_valid_collector_parses(self) -> None:
        c = CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="pages", on_success="assembled_out", on_error="discard")
        assert c.name == "page_stitcher"
        assert c.options == {}

    def test_collector_has_no_trigger_field(self) -> None:
        # Closers flush on end_of_group ONLY (spec §5): a trigger key is extra=forbid rejected.
        with pytest.raises(ValidationError, match="trigger"):
            CollectorSettings(name="c", plugin="p", input="i", on_success="o", on_error="discard", trigger={"count": 5})

    def test_collector_name_reserved_rejected(self) -> None:
        with pytest.raises(ValidationError, match="reserved"):
            CollectorSettings(name="continue", plugin="p", input="i", on_success="o", on_error="discard")

    def test_collector_on_error_defaults_to_none_derives_from_structure(self) -> None:
        # 2026-08-22 synthesis: on_error is optional; None = the route derives
        # from structure (spec §7 rule 9) — losses settle through the scope's
        # group machinery, realized by WS3/WS4.
        c = CollectorSettings(name="c", plugin="p", input="i", on_success="o")
        assert c.on_error is None

    def test_collector_on_error_must_be_sink_or_discard_shaped_when_given(self) -> None:
        with pytest.raises(ValidationError, match="on_error"):
            CollectorSettings(name="c", plugin="p", input="i", on_success="o", on_error="  ")


class TestScopeSettings:
    def test_policy_is_required_no_default(self) -> None:
        with pytest.raises(ValidationError, match="policy"):
            ScopeSettings(name="s", opener="explode", closer="stitch")  # type: ignore[call-arg]

    def test_policy_vocabulary_is_closed(self) -> None:
        # Collector policy v1 = require_all|best_effort (spec decision 15); quorum/first deferred.
        with pytest.raises(ValidationError):
            ScopeSettings(name="s", opener="explode", closer="stitch", policy="quorum")

    def test_on_group_failure_is_rejected(self) -> None:
        # Deleted (ADR-042): group-failure handling is structural — a failed
        # group escalates iff an enclosing bound group exists. The field is
        # refused (extra="forbid") so a config declaring it fails loudly
        # instead of carrying a value nothing reads.
        with pytest.raises(ValidationError):
            ScopeSettings(name="s", opener="explode", closer="stitch", policy="require_all", on_group_failure="quarantine")


class TestElspethSettingsCrossRefs:
    def test_valid_collector_scope_pipeline_parses(self) -> None:
        settings = load_settings_from_config_dict(_with_collector_scope())
        assert settings.collectors[0].name == "page_stitcher"
        assert settings.scopes[0].closer == "page_stitcher"
        assert settings.max_bound_region_depth == 5

    def test_scope_closer_must_name_a_collector(self) -> None:
        doc = _with_collector_scope()
        doc["scopes"][0]["closer"] = "not_a_collector"  # type: ignore[index]
        with pytest.raises(ValueError, match="must name a collectors: entry"):
            load_settings_from_config_dict(doc)

    def test_collector_without_scope_rejected(self) -> None:
        doc = _with_collector_scope()
        doc["scopes"] = []
        with pytest.raises(ValueError, match="no scopes: entry binds"):
            load_settings_from_config_dict(doc)

    def test_scope_opener_must_name_a_transform(self) -> None:
        doc = _with_collector_scope()
        doc["scopes"][0]["opener"] = "missing_transform"  # type: ignore[index]
        with pytest.raises(ValueError, match="must name a transforms: entry"):
            load_settings_from_config_dict(doc)

    def test_two_scopes_cannot_share_a_closer(self) -> None:
        # Collides on closer ONLY: distinct openers ("explode" / "explode2"),
        # same closer ("page_stitcher") — review round 1 disambiguation.
        doc = _with_collector_scope()
        doc["scopes"] = [doc["scopes"][0], {**doc["scopes"][0], "name": "second", "opener": "explode2"}]  # type: ignore[index,list-item]
        with pytest.raises(ValueError, match=r"one scope per closer|already bound"):
            load_settings_from_config_dict(doc)

    def test_two_scopes_cannot_share_an_opener(self) -> None:
        # Collides on opener ONLY: same opener ("explode"), distinct closers
        # ("page_stitcher" / "page_stitcher2", each declared and bound by its
        # own scope so neither trips the unbound-collector check first) —
        # review round 1 disambiguation.
        doc = _with_collector_scope()
        doc["collectors"].append(  # type: ignore[union-attr]
            {
                "name": "page_stitcher2",
                "plugin": "batch_stats",
                "input": "pages",
                "on_success": "out",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        )
        doc["scopes"] = [  # type: ignore[index]
            doc["scopes"][0],  # type: ignore[index]
            {"name": "second", "opener": "explode", "closer": "page_stitcher2", "policy": "require_all"},
        ]
        with pytest.raises(ValueError, match="one scope per opener"):
            load_settings_from_config_dict(doc)

    def test_max_bound_region_depth_override(self) -> None:
        doc = {**_MINIMAL, "max_bound_region_depth": 8}
        settings = load_settings_from_config_dict(doc)
        assert settings.max_bound_region_depth == 8

    def test_max_bound_region_depth_floor(self) -> None:
        with pytest.raises(ValidationError):
            load_settings_from_config_dict({**_MINIMAL, "max_bound_region_depth": 0})
