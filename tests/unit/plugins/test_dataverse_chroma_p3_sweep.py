"""Regression tests for the P3 Dataverse / Chroma sweep findings (20261111-plugins-review).

Each test class names the sweep finding it pins.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError

_ENV_URL = "https://myorg.crm.dynamics.com"
_AUTH = {
    "method": "service_principal",
    "tenant_id": "tenant-123",
    "client_id": "client-456",
    "client_secret": "secret-789",  # secret-scan: allow-this-line
}


def _source_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "environment_url": _ENV_URL,
        "auth": _AUTH,
        "entity": "contact",
        "schema": {"mode": "observed"},
        "on_validation_failure": "quarantine",
    }
    config.update(overrides)
    return config


class TestFinding48SelectAndTopValidation:
    """#48: select entries must be Dataverse identifiers; top must be >= 1."""

    @pytest.mark.parametrize(
        "select_field",
        ["fullname#x", "fullname&$filter=statecode eq 1", "full name", "fullname,emailaddress1", "FullName", "a/b"],
    )
    def test_non_identifier_select_entry_rejected_at_config_time(self, select_field: str) -> None:
        from elspeth.plugins.sources.dataverse import DataverseSourceConfig

        with pytest.raises(PluginConfigError, match=r"select\[0\]"):
            DataverseSourceConfig.from_dict(_source_config(select=[select_field]))

    @pytest.mark.parametrize("select_field", ["fullname", "_parentcustomerid_value", " emailaddress1 "])
    def test_identifier_select_entries_still_accepted(self, select_field: str) -> None:
        from elspeth.plugins.sources.dataverse import DataverseSourceConfig

        cfg = DataverseSourceConfig.from_dict(_source_config(select=[select_field]))
        assert cfg.select == [select_field.strip()]

    @pytest.mark.parametrize("top", [0, -1])
    def test_non_positive_top_rejected_at_config_time(self, top: int) -> None:
        from elspeth.plugins.sources.dataverse import DataverseSourceConfig

        with pytest.raises(PluginConfigError, match="top"):
            DataverseSourceConfig.from_dict(_source_config(top=top))
