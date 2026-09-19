"""Web export markings come from deployment settings, including recovery."""

from __future__ import annotations

import pytest

from elspeth.core.config import LandscapeExportSettings
from elspeth.web.config import WebSettings
from tests.unit.core.test_audit_export_config import _enabled_config


def test_operator_marking_overrides_authored_export_marking() -> None:
    from elspeth.web.execution.export_marking import apply_operator_export_marking

    authored = LandscapeExportSettings(**_enabled_config(exporter_version="landscape-exporter-auth-v2", compartment_id="forged"))
    operator = WebSettings.model_construct(compartment_id="research-a")

    effective = apply_operator_export_marking(authored, operator)

    assert effective.compartment_id == "research-a"
    assert effective.public_snapshot_config()["compartment_id"] == "research-a"


def test_signed_web_export_refuses_missing_operator_marking() -> None:
    from elspeth.web.execution.export_marking import apply_operator_export_marking

    authored = LandscapeExportSettings(
        **_enabled_config(
            exporter_version="landscape-exporter-auth-v2",
            compartment_id="forged",
            signing_mode="hmac_sha256",
            signer_key_id="signer-a",
            signing_secret_ref="SIGNER_KEY",
        )
    )
    operator = WebSettings.model_construct(compartment_id=None)

    with pytest.raises(ValueError, match="compartment_id"):
        apply_operator_export_marking(authored, operator)


def test_unsigned_web_export_without_operator_marking_retains_auth_v1() -> None:
    from elspeth.web.execution.export_marking import apply_operator_export_marking

    authored = LandscapeExportSettings(**_enabled_config(exporter_version="landscape-exporter-auth-v2", compartment_id="forged"))
    operator = WebSettings.model_construct(compartment_id=None)

    effective = apply_operator_export_marking(authored, operator)

    assert effective.exporter_version == "landscape-exporter-auth-v1"
    assert effective.compartment_id is None
    assert "compartment_id" not in effective.public_snapshot_config()


def test_web_export_upgrades_legacy_authored_version_to_marked_version() -> None:
    from elspeth.web.execution.export_marking import apply_operator_export_marking

    authored = LandscapeExportSettings(**_enabled_config(exporter_version="landscape-exporter-auth-v1", compartment_id=None))
    operator = WebSettings.model_construct(compartment_id="research-a")

    effective = apply_operator_export_marking(authored, operator)

    assert effective.exporter_version == "landscape-exporter-auth-v2"
    assert effective.compartment_id == "research-a"
