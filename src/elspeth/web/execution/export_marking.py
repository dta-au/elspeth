"""Bind Web audit exports to the deployment's compartment marking."""

from __future__ import annotations

from typing import Any

from elspeth.contracts.audit_export import validate_compartment_id
from elspeth.core.config import LandscapeExportSettings
from elspeth.web.config import WebSettings


def operator_marked_config_dict(config: dict[str, Any], web_settings: WebSettings) -> dict[str, Any]:
    """Copy an authored config with the operator's marking before model validation."""
    landscape = config["landscape"] if "landscape" in config else None
    if type(landscape) is not dict:
        return config
    export = landscape["export"] if "export" in landscape else None
    if type(export) is not dict:
        return config
    compartment_id = web_settings.compartment_id
    if compartment_id is not None:
        validate_compartment_id(compartment_id)
    return {
        **config,
        "landscape": {**landscape, "export": {**export, "compartment_id": compartment_id}},
    }


def apply_operator_export_marking(export_config: LandscapeExportSettings, web_settings: WebSettings) -> LandscapeExportSettings:
    """Replace a pipeline-authored export marking with operator authority."""
    if not export_config.enabled:
        return export_config
    compartment_id = web_settings.compartment_id
    validate_compartment_id(compartment_id)
    return LandscapeExportSettings.model_validate(
        {
            **export_config.model_dump(),
            "exporter_version": "landscape-exporter-auth-v2",
            "compartment_id": compartment_id,
        }
    )
