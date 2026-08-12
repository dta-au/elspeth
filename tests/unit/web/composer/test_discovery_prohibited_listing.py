"""Prohibited plugins are discoverable in chat, with their policy reason (R2-F18).

``WEB_SURFACE_PROHIBITED`` plugins were silently filtered out of
``list_sources`` / ``list_transforms`` / ``list_sinks`` (``PolicyCatalogView._visible``
only ever returned ``snapshot.available``). The skill instructs the composer
model to claim policy denial "only when live discovery proves it" — a
categorically-banned plugin never appeared in discovery, so a user asking
"why can't I use X" was structurally unanswerable and got silently dropped.

These tests pin the fix: each of the three discovery tools now carries a
``prohibited`` section alongside its unchanged ``available`` listing, naming
only the ``WEB_SURFACE_PROHIBITED`` closed reason (never not-installed,
not-authorized, missing-credential, or no-profile — those stay on the
attempt-failure path). See elspeth-28a695d7f4.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import planner_authoring_aids
from elspeth.web.composer.planner_authoring_aids import discovery_digest
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import execute_tool
from elspeth.web.plugin_policy.models import (
    PluginAvailability,
    PluginAvailabilitySnapshot,
    PluginId,
    PluginUnavailableReason,
)
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from tests.unit.web.composer._helpers import _mock_catalog

_SOURCE_AWS_S3 = PluginId("source", "aws_s3")


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _snapshot_with_unavailable(
    catalog: MagicMock,
    *entries: PluginAvailability,
) -> PluginAvailabilitySnapshot:
    """Build a restricted (non-trained-operator) snapshot with explicit closed reasons.

    Mirrors the ``_restricted_policy_pair`` pattern in ``test_tools.py``:
    start from the trained-operator's unrestricted availability, then
    subtract the named plugin ids and record their closed reasons.
    """
    unrestricted = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    removed = {entry.plugin_id for entry in entries}
    return PluginAvailabilitySnapshot.create(
        policy_hash="prohibited-listing-test-policy",
        principal_scope="local:test-user",
        available=unrestricted.available - removed,
        unavailable=entries,
        selected=unrestricted.selected,
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="prohibited-listing-test-generation",
    )


def _view(catalog: MagicMock, snapshot: PluginAvailabilitySnapshot) -> PolicyCatalogView:
    return PolicyCatalogView(catalog, snapshot, MagicMock(spec=OperatorProfileRegistry))


def _list_sources_data(view: PolicyCatalogView, snapshot: PluginAvailabilitySnapshot) -> dict:
    result = execute_tool(
        "list_sources",
        {},
        _empty_state(),
        view,
        plugin_snapshot=snapshot,
    )
    assert result.success is True
    return result.data


def test_prohibited_source_is_listed_with_reason_and_explanation() -> None:
    """A WEB_SURFACE_PROHIBITED source appears in `prohibited`, not `available`."""
    catalog = _mock_catalog()
    snapshot = _snapshot_with_unavailable(
        catalog,
        PluginAvailability(_SOURCE_AWS_S3, PluginUnavailableReason.WEB_SURFACE_PROHIBITED),
    )
    view = _view(catalog, snapshot)

    data = _list_sources_data(view, snapshot)

    assert "aws_s3" not in {item.name for item in data["available"]}
    prohibited_by_name = {entry["name"]: entry for entry in data["prohibited"]}
    assert "aws_s3" in prohibited_by_name
    entry = prohibited_by_name["aws_s3"]
    assert entry["reason"] == "plugin_not_allowed_on_web"
    assert entry["explanation"]
    assert isinstance(entry["explanation"], str)


def test_prohibited_section_excluded_when_ban_does_not_apply() -> None:
    """A snapshot where aws_s3 is fully available carries no prohibited entry for it."""
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    view = PolicyCatalogView.for_trained_operator(catalog, snapshot)

    data = _list_sources_data(view, snapshot)

    assert "aws_s3" in {item.name for item in data["available"]}
    assert data["prohibited"] == ()


def test_available_section_unchanged_by_prohibited_addition() -> None:
    """The `available` listing is identical whether or not a ban is present."""
    catalog = _mock_catalog()
    baseline_snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    baseline_view = PolicyCatalogView.for_trained_operator(catalog, baseline_snapshot)
    baseline_data = _list_sources_data(baseline_view, baseline_snapshot)

    banned_snapshot = _snapshot_with_unavailable(
        catalog,
        PluginAvailability(_SOURCE_AWS_S3, PluginUnavailableReason.WEB_SURFACE_PROHIBITED),
    )
    banned_view = _view(catalog, banned_snapshot)
    banned_data = _list_sources_data(banned_view, banned_snapshot)

    baseline_available_names = {item.name for item in baseline_data["available"]}
    banned_available_names = {item.name for item in banned_data["available"]}
    assert banned_available_names == baseline_available_names - {"aws_s3"}
    # Every remaining available entry is byte-identical to its baseline counterpart.
    baseline_by_name = {item.name: item for item in baseline_data["available"]}
    for item in banned_data["available"]:
        assert item == baseline_by_name[item.name]


def test_other_unavailable_reasons_are_not_listed_as_prohibited() -> None:
    """NOT_AUTHORIZED (and every other non-categorical reason) stays off the listing."""
    catalog = _mock_catalog()
    snapshot = _snapshot_with_unavailable(
        catalog,
        PluginAvailability(_SOURCE_AWS_S3, PluginUnavailableReason.NOT_AUTHORIZED),
    )
    view = _view(catalog, snapshot)

    data = _list_sources_data(view, snapshot)

    assert "aws_s3" not in {item.name for item in data["available"]}
    assert data["prohibited"] == ()


def test_discovery_digest_and_list_tools_share_exact_prohibited_projection_for_every_kind() -> None:
    catalog = _mock_catalog()
    unrestricted = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    identities = {
        "source": next(plugin for plugin in unrestricted.available if plugin.kind == "source"),
        "transform": next(plugin for plugin in unrestricted.available if plugin.kind == "transform"),
        "sink": next(plugin for plugin in unrestricted.available if plugin.kind == "sink"),
    }
    snapshot = _snapshot_with_unavailable(
        catalog,
        *(PluginAvailability(plugin_id, PluginUnavailableReason.WEB_SURFACE_PROHIBITED) for plugin_id in identities.values()),
    )
    view = _view(catalog, snapshot)
    digest = discovery_digest(view)

    for kind, tool_name in (("source", "list_sources"), ("transform", "list_transforms"), ("sink", "list_sinks")):
        result = execute_tool(tool_name, {}, _empty_state(), view, plugin_snapshot=snapshot)
        assert result.success is True
        assert digest["prohibited"][f"{kind}s"] == list(result.data["prohibited"])

    assert "_PROHIBITED_REASON" not in planner_authoring_aids.__dict__
    assert "_PROHIBITED_EXPLANATION" not in planner_authoring_aids.__dict__


def test_list_transforms_and_list_sinks_carry_the_same_prohibited_shape() -> None:
    """The prohibited section is uniform across all three discovery tools."""
    catalog = _mock_catalog()
    sink_id = PluginId("sink", "aws_s3")
    snapshot = _snapshot_with_unavailable(
        catalog,
        PluginAvailability(sink_id, PluginUnavailableReason.WEB_SURFACE_PROHIBITED),
    )
    view = _view(catalog, snapshot)

    transforms_result = execute_tool("list_transforms", {}, _empty_state(), view, plugin_snapshot=snapshot)
    sinks_result = execute_tool("list_sinks", {}, _empty_state(), view, plugin_snapshot=snapshot)

    assert transforms_result.data["prohibited"] == ()
    sink_prohibited = {entry["name"]: entry for entry in sinks_result.data["prohibited"]}
    assert "aws_s3" in sink_prohibited
    assert sink_prohibited["aws_s3"]["reason"] == "plugin_not_allowed_on_web"
    assert "aws_s3" not in {item.name for item in sinks_result.data["available"]}
