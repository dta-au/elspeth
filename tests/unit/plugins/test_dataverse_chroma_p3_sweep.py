"""Regression tests for the P3 Dataverse sweep findings (20261111-plugins-review).

Each test class names the sweep finding it pins. The Chroma findings left with
the Chroma plugins in 0.8.18.
"""

from __future__ import annotations

from typing import Any, cast

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


def _fetchxml_source_config(fetch_xml: str) -> dict[str, Any]:
    config = _source_config(fetch_xml=fetch_xml)
    del config["entity"]
    return config


class TestFinding49FetchXmlEntityRequiredAtConfigTime:
    """#49: FetchXML must name exactly one <entity> under <fetch>, checked at config time."""

    @pytest.mark.parametrize(
        "fetch_xml",
        [
            "<fetch><all-attributes/></fetch>",
            "<fetch><entity><attribute name='fullname'/></entity></fetch>",
            "<fetch><entity name='contact'/><entity name='account'/></fetch>",
        ],
        ids=["no-entity", "entity-without-name", "two-entities"],
    )
    def test_fetchxml_without_exactly_one_named_entity_rejected(self, fetch_xml: str) -> None:
        from elspeth.plugins.sources.dataverse import DataverseSourceConfig

        with pytest.raises(PluginConfigError, match="entity"):
            DataverseSourceConfig.from_dict(_fetchxml_source_config(fetch_xml))

    def test_fetchxml_with_one_named_entity_accepted(self) -> None:
        from elspeth.plugins.sources.dataverse import DataverseSourceConfig

        xml = "<fetch><entity name='contact'><attribute name='fullname'/></entity></fetch>"
        assert DataverseSourceConfig.from_dict(_fetchxml_source_config(xml)).fetch_xml == xml


class _FakeAzureCredential:
    def get_token(self, *scopes: str) -> Any:
        raise AssertionError("classification must not acquire a token")


class TestFinding76DataverseRetryAfterCarried:
    """#76: the parsed 429 Retry-After delay is a typed field on DataverseClientError; HTTP-date is parsed."""

    @staticmethod
    def _client() -> Any:
        from elspeth.plugins.infrastructure.clients.dataverse import DataverseClient

        client = DataverseClient(environment_url=_ENV_URL, credential=cast(Any, _FakeAzureCredential()))
        client.close()
        return client

    def test_delta_seconds_retry_after_is_carried(self) -> None:
        error = self._client()._classify_error(429, {"retry-after": "7"}, 1.0)
        assert "retry_after" in vars(error)
        assert error.retryable is True
        assert error.retry_after == 7.0

    def test_http_date_retry_after_is_parsed_not_defaulted(self) -> None:
        from datetime import UTC, datetime, timedelta
        from email.utils import format_datetime

        when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
        error = self._client()._classify_error(429, {"retry-after": when}, 1.0)
        assert "retry_after" in vars(error)
        assert error.retryable is True
        assert 20.0 <= error.retry_after <= 31.0

    def test_http_date_beyond_cap_is_non_retryable(self) -> None:
        from datetime import UTC, datetime, timedelta
        from email.utils import format_datetime

        when = format_datetime(datetime.now(UTC) + timedelta(hours=1), usegmt=True)
        error = self._client()._classify_error(429, {"retry-after": when}, 1.0)
        assert error.retryable is False
        assert "retry_after" in vars(error)
        assert error.retry_after > 3000.0

    def test_absent_retry_after_and_non_throttle_status_carry_none(self) -> None:
        # 503 is a throttle status and carries its Retry-After since #76's
        # consumption landed (test_dataverse_retry_after.py); 500 does not.
        client = self._client()
        missing = client._classify_error(429, {}, 1.0)
        server_error = client._classify_error(500, {"retry-after": "5"}, 1.0)
        assert "retry_after" in vars(missing)
        assert missing.retry_after is None
        assert missing.retryable is True
        assert "retry_after" in vars(server_error)
        assert server_error.retry_after is None


def _sink_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "environment_url": _ENV_URL,
        "auth": _AUTH,
        "entity": "contacts",
        "alternate_key": "emailaddress1",
        "field_mapping": {"email": "emailaddress1", "name": "fullname"},
        "schema": {"mode": "observed"},
    }
    config.update(overrides)
    return config


class TestFinding82UpsertKeyQuoteEscaping:
    """#82: a single quote in an alternate-key value is doubled (OData) before percent-encoding."""

    def test_apostrophe_key_value_is_odata_escaped(self) -> None:
        import urllib.parse

        from elspeth.plugins.sinks.dataverse import DataverseSink

        url = DataverseSink(_sink_config())._build_upsert_url("o'brien@x.com")

        # Servers percent-decode before parsing OData: the decoded key predicate
        # must be one balanced string literal with the quote doubled.
        assert urllib.parse.unquote(url).endswith("/contacts(emailaddress1='o''brien@x.com')")

    def test_key_value_without_quotes_unchanged(self) -> None:
        from elspeth.plugins.sinks.dataverse import DataverseSink

        url = DataverseSink(_sink_config())._build_upsert_url("alice@example.com")
        assert url == f"{_ENV_URL}/api/data/v9.2/contacts(emailaddress1='alice%40example.com')"


class TestFinding85LookupKeysMustBeMapped:
    """#85: a lookup keyed by a pipeline field absent from field_mapping is refused, not silently dropped."""

    def test_unmapped_lookup_key_rejected(self) -> None:
        from elspeth.plugins.sinks.dataverse import DataverseSinkConfig

        config = _sink_config(
            field_mapping={"email": "emailaddress1"},
            lookups={"account_id": {"target_entity": "accounts", "target_field": "parentcustomerid"}},
        )
        with pytest.raises(PluginConfigError, match="account_id"):
            DataverseSinkConfig.from_dict(config)

    def test_mapped_lookup_key_accepted(self) -> None:
        from elspeth.plugins.sinks.dataverse import DataverseSinkConfig

        config = _sink_config(
            field_mapping={"email": "emailaddress1", "account_id": "parentcustomerid"},
            lookups={"account_id": {"target_entity": "accounts", "target_field": "parentcustomerid"}},
        )
        cfg = DataverseSinkConfig.from_dict(config)
        assert cfg.lookups is not None
        assert set(cfg.lookups) == {"account_id"}
