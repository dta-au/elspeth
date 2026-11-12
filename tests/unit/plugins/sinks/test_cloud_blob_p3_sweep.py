"""Regression tests for the P3 cloud-blob sweep findings (20261111-plugins-review).

Each test names the sweep finding it pins. They exercise the AWS S3 sink, the
Azure Blob sink and source, and the shared Azure auth config.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.plugins.infrastructure.azure_auth import AzureAuthConfig

_SAS_ACCOUNT_URL = "https://acct.blob.core.windows.net/?sv=2022-11-02&sig=SECRETSIG"


def _validation_error(**kwargs: Any) -> ValidationError | None:
    try:
        AzureAuthConfig(**kwargs)
    except ValidationError as exc:
        return exc
    return None


class TestFinding150AccountUrlQueryString:
    """#150: account_url must refuse a query string or fragment (a pasted SAS URL)."""

    @pytest.mark.parametrize(
        "account_url",
        [
            pytest.param(_SAS_ACCOUNT_URL, id="sas-query"),
            pytest.param("https://acct.blob.core.windows.net?x=1", id="plain-query"),
            pytest.param("https://acct.blob.core.windows.net/#frag", id="fragment"),
        ],
    )
    def test_managed_identity_account_url_with_query_or_fragment_is_refused(self, account_url: str) -> None:
        error = _validation_error(use_managed_identity=True, account_url=account_url)

        assert error is not None, "account_url with a query string or fragment must be refused"
        assert "SECRETSIG" not in str(error)
        assert "sas_token" in str(error)

    def test_sas_token_with_query_bearing_account_url_is_refused(self) -> None:
        error = _validation_error(sas_token="sv=2022&sig=OTHER", account_url="https://acct.blob.core.windows.net/?x=1")

        assert error is not None, "a query on account_url would be concatenated into a malformed '?x=1?sv=' URL"

    def test_plain_account_url_is_still_accepted(self) -> None:
        config = AzureAuthConfig(use_managed_identity=True, account_url="https://acct.blob.core.windows.net/")

        assert config.auth_method == "managed_identity"
