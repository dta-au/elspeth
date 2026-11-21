"""Catalog projection constrained by one frozen availability snapshot."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from elspeth.contracts.plugin_capabilities import PluginCapability
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.catalog.schemas import PluginKind, PluginSchemaInfo, PluginSummary
from elspeth.web.catalog.validation_reuse import VALIDATION_REUSE_COUNTER, MutationEpoch
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.plugin_policy.profiles import LoweredPluginConfig, OperatorProfileRegistry

if TYPE_CHECKING:
    from elspeth.contracts.plugin_assistance import PluginAssistance
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.plugin_policy.validation import PluginPolicyValidationResult, ProfileAwareValidationResult

# At most three distinct states were measured per tool call; eight leaves room
# without holding more than a handful of states the round already holds.
_VALIDATION_MEMO_MAX_ENTRIES: Final = 8


@dataclass(frozen=True, slots=True)
class _MemoizedValidation:
    """One returned verdict, bound to the exact state object it judged.

    The strong reference keeps ``id(state)`` from being reused while the
    entry lives, and a hit also requires ``entry.state is state``. Neither the
    content hash nor ``==`` is a safe key: both ignore mapping key order, and
    authoring errors come out in key order.
    """

    state: CompositionState
    mutation_epoch: int
    result: ProfileAwareValidationResult


class PolicyCatalogView:
    def __init__(
        self,
        full: CatalogService,
        snapshot: PluginAvailabilitySnapshot,
        profiles: OperatorProfileRegistry,
        *,
        validation_memo_enabled: bool = False,
    ) -> None:
        self._full = full
        self.snapshot = snapshot
        self._profiles: OperatorProfileRegistry | None = profiles
        self._full_items_cache: dict[PluginKind, list[PluginSummary]] = {}
        self._init_validation_reuse(validation_memo_enabled)

    @classmethod
    def for_trained_operator(
        cls,
        full: CatalogService,
        snapshot: PluginAvailabilitySnapshot,
        *,
        validation_memo_enabled: bool = False,
    ) -> PolicyCatalogView:
        """Return the explicit full-catalog projection for the local MCP."""
        if not snapshot.is_trained_operator:
            raise ValueError("trained_operator_snapshot_required")
        view = cls.__new__(cls)
        view._full = full
        view.snapshot = snapshot
        view._profiles = None
        view._full_items_cache = {}
        view._init_validation_reuse(validation_memo_enabled)
        return view

    def _init_validation_reuse(self, validation_memo_enabled: bool) -> None:
        # The view is built per compose call or per request, so the memo and
        # the epoch live exactly as long as that; nothing outlives the view.
        self.mutation_epoch = MutationEpoch()
        self._validation_memo_enabled = validation_memo_enabled
        self._validation_memo: OrderedDict[int, _MemoizedValidation] = OrderedDict()
        self._validation_memo_lock = threading.Lock()

    def record_mutation_tool_end(self) -> None:
        """Advance the round's mutation epoch and drop every memoized verdict.

        Called by ``execute_tool`` when a mutation-kind tool call ends, in a
        ``finally``. The round's runtime-preflight cache reads the same epoch.
        """
        with self._validation_memo_lock:
            self.mutation_epoch.advance()
            self._validation_memo.clear()

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

    def _usable_profile_aliases(self, plugin_id: PluginId) -> tuple[str, ...]:
        """Return the operator-profile aliases this principal may author for ``plugin_id``.

        ``build_plugin_snapshot`` records an entry in
        ``snapshot.usable_profile_aliases`` only for a plugin whose
        ``web_config_authority`` is ``WebConfigAuthority.OPERATOR_PROFILED``,
        so for anything this view can reach, absence means "not
        operator-profiled" and the explicit empty answer is the RESTRICTIVE
        one: no alias is offered on the wire, ``public_summary`` /
        ``public_schema`` are never asked to project a binding that does not
        exist, and ``lower_operator_profile_options`` refuses every alias.
        A profiled plugin whose principal has no usable alias is recorded
        present-with-``()`` *and* declined ``PROFILE_UNAVAILABLE`` by the same
        producer, so it does not enter ``snapshot.available``. If authored
        options nevertheless carry a ``profile`` key, the lowering boundary
        still rejects every alias with ``ValueError("profile_unavailable")``.

        Never widen the absent branch to anything but ``()``: crediting a
        plugin with aliases the snapshot did not grant it would hand an
        unprofiled plugin the operator's private binding. Pinned by
        ``test_unprofiled_plugin_is_granted_no_operator_profile_alias``.
        """
        aliases_by_plugin = dict(self.snapshot.usable_profile_aliases)
        if plugin_id not in aliases_by_plugin:
            return ()
        return aliases_by_plugin[plugin_id]

    def _visible(self, kind: PluginKind, items: list[PluginSummary]) -> list[PluginSummary]:
        visible = [item for item in items if PluginId(kind, item.name) in self.snapshot.available]
        if self._profiles is None:
            return visible
        projected: list[PluginSummary] = []
        for item in visible:
            plugin_id = PluginId(kind, item.name)
            aliases = self._usable_profile_aliases(plugin_id)
            if not aliases:
                projected.append(item)
                continue
            projected.append(
                self._profiles.public_summary(
                    plugin_id,
                    item,
                    self._full.get_schema(kind, item.name),
                    available_aliases=aliases,
                )
            )
        return projected

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
        aliases = self._usable_profile_aliases(plugin_id)
        if self._profiles is None:
            return self._full.get_schema(plugin_type, name)
        return self._profiles.public_schema(
            plugin_id,
            self._full.get_schema(plugin_type, name),
            available_aliases=aliases,
        )

    def project_agent_assistance(
        self,
        plugin_type: PluginKind,
        name: str,
        assistance: PluginAssistance,
    ) -> PluginAssistance:
        """Project raw plugin guidance through request-scoped profile policy."""
        plugin_id = PluginId(plugin_type, name)
        self._require_available(plugin_id)
        if self._profiles is None:
            return assistance
        aliases = self._usable_profile_aliases(plugin_id)
        if not aliases:
            return assistance
        return self._profiles.public_assistance(plugin_id, assistance)

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
        available_aliases = self._usable_profile_aliases(plugin_id)
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
        """Return the shared authored/executable validation projection.

        With the memo enabled, a second call for the same state object in the
        same mutation epoch returns the first call's result object. Every other
        verdict input is fixed for the life of this view: ``snapshot`` is a
        frozen value set at construction, the profile registry and the catalog
        are fixed, authoring validation reads no blob record (file markers are
        a fixed placeholder there), no secret and no filesystem path. A call
        that raises stores nothing, so the next call runs the validation
        again. The result is frozen dataclasses and tuples throughout, so
        handing every caller the one object lets none of them change it for
        another.
        """
        from elspeth.web.plugin_policy.validation import validate_authored_composition_state

        if not self._validation_memo_enabled:
            return validate_authored_composition_state(
                state,
                snapshot=self.snapshot,
                profile_registry=self._profiles,
                catalog=self._full,
            )
        # Sound only while authoring validation is a pure function of the
        # state, the policy snapshot, the profile registry and the catalog.
        # A blob, secret, clock or mutable-cache read added inside authoring
        # validation breaks this memo: add that input to the key, or stop
        # memoizing.
        with self._validation_memo_lock:
            epoch = self.mutation_epoch.value
            key = id(state)
            if key in self._validation_memo:
                entry = self._validation_memo[key]
                if entry.state is state and entry.mutation_epoch == epoch:
                    self._validation_memo.move_to_end(key)
                    VALIDATION_REUSE_COUNTER.add(1, {"layer": "policy_view"})
                    return entry.result
        result = validate_authored_composition_state(
            state,
            snapshot=self.snapshot,
            profile_registry=self._profiles,
            catalog=self._full,
        )
        with self._validation_memo_lock:
            # A mutation that ended while this ran makes the verdict
            # unreusable; do not store it under an epoch that has passed.
            if self.mutation_epoch.value == epoch:
                self._validation_memo[key] = _MemoizedValidation(state=state, mutation_epoch=epoch, result=result)
                self._validation_memo.move_to_end(key)
                while len(self._validation_memo) > _VALIDATION_MEMO_MAX_ENTRIES:
                    self._validation_memo.popitem(last=False)
        return result

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
