"""Quota settings and their stored policy quantities share a 64-bit range."""

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.web.config import WebSettings
from elspeth.web.sessions.models import quota_policies_table, token_usage_ledger_table


@pytest.mark.parametrize(
    "field",
    ["quota_default_tokens_per_day", "quota_default_storage_bytes", "quota_container_tokens_per_day", "quota_container_storage_bytes"],
)
@pytest.mark.parametrize("value", [5 * 1024**3, 2**63 - 1])
def test_quota_settings_accept_representable_large_quantities(field: str, value: int) -> None:
    settings = WebSettings.model_validate(
        {
            "composer_max_composition_turns": 15,
            "composer_max_discovery_turns": 10,
            "composer_timeout_seconds": 85.0,
            "composer_rate_limit_per_minute": 10,
            "shareable_link_signing_key": b"\x00" * 32,
            field: value,
        }
    )
    assert settings.model_dump()[field] == value


@pytest.mark.parametrize(
    "field",
    ["quota_default_tokens_per_day", "quota_default_storage_bytes", "quota_container_tokens_per_day", "quota_container_storage_bytes"],
)
def test_quota_settings_reject_quantities_larger_than_database_range(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        WebSettings.model_validate(
            {
                "composer_max_composition_turns": 15,
                "composer_max_discovery_turns": 10,
                "composer_timeout_seconds": 85.0,
                "composer_rate_limit_per_minute": 10,
                "shareable_link_signing_key": b"\x00" * 32,
                field: 2**63,
            }
        )


@pytest.mark.parametrize("column", ["tokens_per_day", "storage_bytes", "dual_control_above_tokens"])
def test_quota_policy_ddl_uses_postgres_bigint(column: str) -> None:
    assert quota_policies_table.c[column].type.compile(dialect=postgresql.dialect()) == "BIGINT"


@pytest.mark.parametrize("column", ["prompt_tokens", "completion_tokens"])
def test_missing_provider_counts_can_remain_unknown(column: str) -> None:
    assert token_usage_ledger_table.c[column].nullable


@pytest.mark.parametrize("option", ["--quota-tokens-per-day", "--quota-storage-bytes"])
def test_cli_rejects_quota_overflow_before_opening_stores(option: str, tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "--no-dotenv",
            "composer",
            "users",
            "bootstrap-admin",
            "local",
            "operator",
            "--note",
            "quota range regression",
            "--data-dir",
            str(tmp_path),
            option,
            str(2**63),
        ],
    )
    assert result.exit_code == 2
    assert "9223372036854775807" in result.output
    assert not (tmp_path / "sessions.db").exists()
