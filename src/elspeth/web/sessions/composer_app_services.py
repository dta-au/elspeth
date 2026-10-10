"""Explicit per-job dependencies for detached composer turns."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from sqlalchemy import Engine
from starlette.applications import Starlette

from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.interpretation_surfacing import InterpretationSurfacing
from elspeth.web.composer.progress import ComposerProgressRegistry
from elspeth.web.composer.protocol import ComposerService
from elspeth.web.config import WebSettings
from elspeth.web.coordination.composer_progress_authority import DatabaseComposerProgressRegistry
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.secrets.service import ScopedSecretResolver
from elspeth.web.sessions.protocol import SessionServiceProtocol

if TYPE_CHECKING:
    from elspeth.web.sessions.routes._helpers import _SessionComposeLockRegistry


@final
@dataclass(frozen=True, slots=True)
class ComposerAppServices:
    session_service: SessionServiceProtocol
    composer_service: ComposerService
    settings: WebSettings
    catalog_service: CatalogService
    operator_profile_registry: OperatorProfileRegistry
    scoped_secret_resolver: ScopedSecretResolver
    session_engine: Engine
    interpretation_surfacing: InterpretationSurfacing
    plugin_snapshot_for_user_id: Callable[[str], PluginAvailabilitySnapshot]
    progress_registry: ComposerProgressRegistry | DatabaseComposerProgressRegistry
    compose_locks: _SessionComposeLockRegistry


def composer_app_services(app: Starlette) -> ComposerAppServices:
    """Resolve services afresh for each job, including provider test seams."""
    from elspeth.web.sessions.routes._helpers import composer_session_lock_registry

    return ComposerAppServices(
        session_service=app.state.session_service,
        composer_service=app.state.composer_service,
        settings=app.state.settings,
        catalog_service=app.state.catalog_service,
        operator_profile_registry=app.state.operator_profile_registry,
        scoped_secret_resolver=app.state.scoped_secret_resolver,
        session_engine=app.state.session_engine,
        interpretation_surfacing=app.state.interpretation_surfacing,
        plugin_snapshot_for_user_id=app.state.plugin_snapshot_factory.for_user_id,
        progress_registry=app.state.composer_progress_registry,
        compose_locks=composer_session_lock_registry(app),
    )
