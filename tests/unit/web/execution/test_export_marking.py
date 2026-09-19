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


def test_unsigned_web_export_refuses_missing_operator_marking() -> None:
    from elspeth.web.execution.export_marking import apply_operator_export_marking

    authored = LandscapeExportSettings(**_enabled_config(exporter_version="landscape-exporter-auth-v2", compartment_id="forged"))
    operator = WebSettings.model_construct(compartment_id=None)

    with pytest.raises(ValueError, match="compartment_id"):
        apply_operator_export_marking(authored, operator)


@pytest.mark.parametrize("authored_compartment", [None, "forged"])
def test_operator_marking_is_injected_before_strict_model_validation(authored_compartment: str | None) -> None:
    from elspeth.web.execution.export_marking import operator_marked_config_dict

    authored = {"landscape": {"export": _enabled_config(compartment_id=authored_compartment)}}
    operator = WebSettings.model_construct(compartment_id="research-a")

    marked = operator_marked_config_dict(authored, operator)
    effective = LandscapeExportSettings.model_validate(marked["landscape"]["export"])

    assert effective.exporter_version == "landscape-exporter-auth-v2"
    assert effective.compartment_id == "research-a"
    assert authored["landscape"]["export"]["compartment_id"] == authored_compartment
