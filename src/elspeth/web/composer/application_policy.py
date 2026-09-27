"""Fresh plugin policy contexts for Composer application operations."""

from __future__ import annotations

from collections.abc import Callable

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.protocol import ComposerServiceError
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry


class PluginPolicyContextFactory:
    """Construct one policy snapshot and catalog view for each request."""

    def __init__(
        self,
        catalog: CatalogService,
        snapshot_factory: Callable[[str], PluginAvailabilitySnapshot],
        profile_registry: OperatorProfileRegistry | None,
        *,
        trained_operator_mode: bool,
    ) -> None:
        self._catalog = catalog
        self._snapshot_factory = snapshot_factory
        self._profile_registry = profile_registry
        self._trained_operator_mode = trained_operator_mode

    def build(self, user_id: str | None) -> tuple[PluginAvailabilitySnapshot, PolicyCatalogView]:
        """Return the current authenticated or trained-operator policy context."""
        if self._trained_operator_mode:
            snapshot = self._snapshot_factory(user_id or "trained-operator")
            return snapshot, PolicyCatalogView.for_trained_operator(self._catalog, snapshot)
        if user_id is None:
            raise ComposerServiceError("Authenticated plugin policy context is required.")
        if self._profile_registry is None:
            raise RuntimeError("operator_profile_registry not wired")
        snapshot = self._snapshot_factory(user_id)
        return snapshot, PolicyCatalogView(self._catalog, snapshot, self._profile_registry)
