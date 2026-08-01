"""Catalog projection constrained by one frozen availability snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from elspeth.contracts.plugin_capabilities import PluginCapability
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.catalog.schemas import PluginKind, PluginSchemaInfo, PluginSummary
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.plugin_policy.profiles import LoweredPluginConfig, OperatorProfileRegistry

if TYPE_CHECKING:
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.plugin_policy.validation import PluginPolicyValidationResult, ProfileAwareValidationResult


class PolicyCatalogView:
    def __init__(
        self,
        full: CatalogService,
        snapshot: PluginAvailabilitySnapshot,
        profiles: OperatorProfileRegistry,
    ) -> None:
        self._full = full
        self.snapshot = snapshot
        self._profiles: OperatorProfileRegistry | None = profiles
        self._full_items_cache: dict[PluginKind, list[PluginSummary]] = {}

    @classmethod
    def for_trained_operator(
        cls,
        full: CatalogService,
        snapshot: PluginAvailabilitySnapshot,
    ) -> PolicyCatalogView:
        """Return the explicit full-catalog projection for the local MCP."""
        if not snapshot.is_trained_operator:
            raise ValueError("trained_operator_snapshot_required")
        view = cls.__new__(cls)
        view._full = full
        view.snapshot = snapshot
        view._profiles = None
        view._full_items_cache = {}
        return view

    def _full_items(self, kind: PluginKind) -> list[PluginSummary]:
        """Return this kind's unrestricted catalog listing, fetched at most once per view.

        ``list_*`` and ``list_prohibited_*`` both need the same unrestricted
        listing (one to keep only ``snapshot.available``, the other to keep
        only the ``WEB_SURFACE_PROHIBITED`` entries) — a discovery tool
        dispatch calls both back-to-back, so caching here is what keeps a
        single ``list_sources`` call from re-deriving every ``PluginSummary``
        twice. Pinned by ``test_cacheable_tool_returns_cached_result`` /
        ``test_cache_hit_rebuilds_result_envelope_from_current_state``
        (exact ``catalog.list_sources.call_count``) in test_service.py.
        """
        if kind not in self._full_items_cache:
            if kind == "source":
                self._full_items_cache[kind] = self._full.list_sources()
            elif kind == "transform":
                self._full_items_cache[kind] = self._full.list_transforms()
            else:
                self._full_items_cache[kind] = self._full.list_sinks()
        return self._full_items_cache[kind]

    def _visible(self, kind: PluginKind, items: list[PluginSummary]) -> list[PluginSummary]:
        return [item for item in items if PluginId(kind, item.name) in self.snapshot.available]

    def list_sources(self) -> list[PluginSummary]:
        return self._visible("source", self._full_items("source"))

    def list_transforms(self) -> list[PluginSummary]:
        return self._visible("transform", self._full_items("transform"))

    def list_sinks(self) -> list[PluginSummary]:
        return self._visible("sink", self._full_items("sink"))

    def _prohibited(self, kind: PluginKind, items: list[PluginSummary]) -> list[PluginSummary]:
        """Return items closed by the categorical ``WEB_SURFACE_PROHIBITED`` ban.

        The ONLY unavailable reason this surfaces. Every other reason (not
        installed, not authorized, missing credential, no operator profile)
        stays silent here — those describe ordinary "not selectable *yet*"
        gaps an operator can close, so they belong to the attempt-failure
        path (``set_source`` etc.), not a standing discovery listing. This
        reason is different in kind: nothing an operator does in this
        deployment can clear it, so a user asking "why can't I use X"
        deserves the answer without first attempting and failing. See
        R2-F18 / elspeth-28a695d7f4.
        """
        banned = {
            availability.plugin_id
            for availability in self.snapshot.unavailable
            if availability.reason is PluginUnavailableReason.WEB_SURFACE_PROHIBITED
        }
        return [item for item in items if PluginId(kind, item.name) in banned]

    def list_prohibited_sources(self) -> list[PluginSummary]:
        """Return source plugins categorically banned from the web surface."""
        return self._prohibited("source", self._full_items("source"))

    def list_prohibited_transforms(self) -> list[PluginSummary]:
        """Return transform plugins categorically banned from the web surface."""
        return self._prohibited("transform", self._full_items("transform"))

    def list_prohibited_sinks(self) -> list[PluginSummary]:
        """Return sink plugins categorically banned from the web surface."""
        return self._prohibited("sink", self._full_items("sink"))

    def capability_groups(self) -> dict[PluginCapability, tuple[PluginId, ...]]:
        """Return safe visible plugin IDs grouped by declared capability."""
        groups: dict[PluginCapability, list[PluginId]] = {capability: [] for capability in PluginCapability}
        visible = (
            *((PluginId("source", item.name), item) for item in self.list_sources()),
            *((PluginId("transform", item.name), item) for item in self.list_transforms()),
            *((PluginId("sink", item.name), item) for item in self.list_sinks()),
        )
        for plugin_id, summary in visible:
            for declaration in summary.policy_capabilities:
                groups[declaration.capability].append(plugin_id)
        return {capability: tuple(sorted(plugin_ids)) for capability, plugin_ids in groups.items() if plugin_ids}

    def _require_available(self, plugin_id: PluginId) -> None:
        if plugin_id not in self.snapshot.available:
            raise ValueError("plugin_not_enabled")

    def unavailable_reason(self, plugin_id: PluginId) -> PluginUnavailableReason | None:
        """Return the closed policy reason for an identity, or ``None``."""
        if plugin_id in self.snapshot.available:
            return None
        unavailable = {item.plugin_id: item.reason for item in self.snapshot.unavailable}
        if plugin_id in unavailable:
            return unavailable[plugin_id]
        installed = {
            *(PluginId("source", item.name) for item in self._full.list_sources()),
            *(PluginId("transform", item.name) for item in self._full.list_transforms()),
            *(PluginId("sink", item.name) for item in self._full.list_sinks()),
        }
        if plugin_id not in installed:
            return PluginUnavailableReason.NOT_INSTALLED
        return PluginUnavailableReason.NOT_AUTHORIZED

    def get_schema(self, plugin_type: PluginKind, name: str) -> PluginSchemaInfo:
        plugin_id = PluginId(plugin_type, name)
        self._require_available(plugin_id)
        aliases = dict(self.snapshot.usable_profile_aliases).get(plugin_id, ())
        if self._profiles is None:
            return self._full.get_schema(plugin_type, name)
        return self._profiles.public_schema(
            plugin_id,
            self._full.get_schema(plugin_type, name),
            available_aliases=aliases,
        )

    def lower_operator_profile_options(
        self,
        plugin_id: PluginId,
        *,
        alias: str,
        safe_options: dict[str, object],
    ) -> LoweredPluginConfig:
        """Lower authored profile options without exposing the binding.

        Persisted state retains the opaque alias and safe options; this
        compatibility boundary only returns an in-memory executable view.
        """
        if self._profiles is None:
            raise ValueError("plugin_has_no_operator_profile")
        available_aliases = dict(self.snapshot.usable_profile_aliases).get(plugin_id, ())
        if alias not in available_aliases:
            raise ValueError("profile_unavailable")
        return self._profiles.lower_options(plugin_id, alias=alias, safe_options=safe_options)

    def validate_authored_state(self, state: CompositionState) -> PluginPolicyValidationResult:
        """Validate policy and lower private bindings without mutating authored state."""
        from elspeth.web.plugin_policy.validation import validate_plugin_policy

        return validate_plugin_policy(
            state,
            snapshot=self.snapshot,
            profile_registry=self._profiles,
            catalog=self._full,
        )

    def validate_composition_state(self, state: CompositionState) -> ProfileAwareValidationResult:
        """Return the shared authored/executable validation projection."""
        from elspeth.web.plugin_policy.validation import validate_authored_composition_state

        return validate_authored_composition_state(
            state,
            snapshot=self.snapshot,
            profile_registry=self._profiles,
            catalog=self._full,
        )

    def post_call_hints(
        self,
        *,
        plugin_type: PluginKind,
        plugin_name: str,
        tool_name: str,
        config_snapshot: Mapping[str, object],
    ) -> tuple[str, ...]:
        plugin_id = PluginId(plugin_type, plugin_name)
        self._require_available(plugin_id)
        return self._full.post_call_hints(
            plugin_type=plugin_type,
            plugin_name=plugin_name,
            tool_name=tool_name,
            config_snapshot=config_snapshot,
        )
