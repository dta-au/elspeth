"""Bind Web audit exports to the deployment's compartment marking."""

from __future__ import annotations

from elspeth.core.config import LandscapeExportSettings
from elspeth.web.config import WebSettings


def apply_operator_export_marking(export_config: LandscapeExportSettings, web_settings: WebSettings) -> LandscapeExportSettings:
    """Replace a pipeline-authored export marking with operator authority."""
    if not export_config.enabled:
        return export_config
    compartment_id = web_settings.compartment_id
    if type(compartment_id) is not str or not compartment_id.strip():
        if export_config.sign:
            raise ValueError("compartment_id is required for signed Web audit export")
        # Local deployments can run with governance off and no compartment.
        # Their unsigned exports keep the historical unmarked auth-v1 shape.
        return LandscapeExportSettings.model_validate(
            {
                **export_config.model_dump(),
                "exporter_version": "landscape-exporter-auth-v1",
                "compartment_id": None,
            }
        )
    return LandscapeExportSettings.model_validate(
        {
            **export_config.model_dump(),
            "exporter_version": "landscape-exporter-auth-v2",
            "compartment_id": compartment_id,
        }
    )
