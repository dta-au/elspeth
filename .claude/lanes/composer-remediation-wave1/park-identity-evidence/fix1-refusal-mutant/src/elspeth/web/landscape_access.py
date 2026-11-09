"""Fail-closed web-layer access to writable Landscape databases."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from elspeth.core.landscape.database import LandscapeDB
from elspeth.web.deployment_contract import resolve_deployment_state_mode
from elspeth.web.schema_probe import postgres_engine_kwargs

if TYPE_CHECKING:
    from elspeth.web.config import WebSettings


def landscape_create_tables_allowed(
    settings: WebSettings,
    deployment_state_mode: Literal["sqlite-single", "external-postgresql"] | None = None,
) -> bool:
    """Return whether this deployment may lazily create Landscape schema."""
    state_mode = deployment_state_mode or resolve_deployment_state_mode(settings)
    return state_mode == "sqlite-single"


def open_landscape_db(
    settings: WebSettings,
    deployment_state_mode: Literal["sqlite-single", "external-postgresql"] | None = None,
) -> LandscapeDB:
    """Open a writable Landscape database with the deployment schema policy."""
    state_mode = deployment_state_mode or resolve_deployment_state_mode(settings)
    create_tables = state_mode == "sqlite-single"
    if state_mode == "external-postgresql":
        url = settings.landscape_url
        assert url is not None
    else:
        url = settings.get_landscape_url()
    return LandscapeDB.from_url(
        url,
        passphrase=settings.landscape_passphrase,
        create_tables=create_tables,
        **postgres_engine_kwargs(url),
    )
