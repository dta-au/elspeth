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


class TestFinding29S3ClosedLifecycle:
    """#29: a closed AWSS3Sink must not silently build a new client."""

    def test_provider_call_after_close_raises_closed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.contracts.sink_effects import SinkEffectInspectionRequest
        from elspeth.plugins.sinks import aws_s3_sink
        from tests.unit.plugins.sinks.test_remote_object_sink_effects import _CTX, _s3, _S3Store

        built: list[object] = []

        def build_fresh_client(*_args: object) -> _S3Store:
            store = _S3Store()
            built.append(store)
            return store

        class _ClosableS3Store(_S3Store):
            def close(self) -> None:
                return None

        monkeypatch.setattr(aws_s3_sink, "build_s3_client", build_fresh_client)
        sink = _s3(_ClosableS3Store())
        sink.close()

        raised: BaseException | None = None
        try:
            sink.inspect_effect(SinkEffectInspectionRequest(effect_id="a" * 64, target="{}", predecessor_descriptor=None), _CTX)
        except Exception as exc:
            raised = exc

        assert built == [], "a closed sink must not construct a replacement S3 client"
        assert type(raised).__name__ == "S3SinkClosedError"

    @pytest.mark.parametrize(
        "symbol",
        ["S3SinkPoisonedError", "_raise_conditional_rejected", "_raise_sink_write_rejected", "_raise_audit_integrity"],
    )
    def test_unused_failure_helpers_are_not_declared(self, symbol: str) -> None:
        from elspeth.plugins.sinks import aws_s3_sink

        assert symbol not in vars(aws_s3_sink), f"{symbol} is declared but never raised; it claims a guard the sink lacks"


_FIXED_NAME_SCHEMA: dict[str, Any] = {"mode": "fixed", "fields": ["name: str"]}


def _preflight_outcome(sink: Any, rows: list[dict[str, object]]) -> tuple[BaseException | None, int]:
    """Run the per-member preflight; return (raised exception, diverted row count)."""
    from elspeth.contracts.sink_effects import SinkEffectPipelineMembersInput
    from tests.unit.plugins.sinks.test_remote_object_sink_effects import _member

    members = tuple(_member(index, row) for index, row in enumerate(rows))
    effect_input = SinkEffectPipelineMembersInput(
        members=members,
        target_snapshot_members=members,
        target_delivered_member_count=len(members),
    )
    raised: BaseException | None = None
    try:
        sink._preflight_effect_members(effect_input)
    except Exception as exc:
        raised = exc
    return raised, len(sink._get_diversions())


class TestFinding28S3ProbeChecksDisplayHeader:
    """#28: the S3 per-row probe must check the header bytes the final object writes."""

    @staticmethod
    def _sink(**overrides: object) -> Any:
        from tests.unit.plugins.sinks.test_remote_object_sink_effects import _s3, _S3Store

        config: dict[str, object] = {
            "key": "runs/{{ run_id }}/out.csv",
            "format": "csv",
            "schema": _FIXED_NAME_SCHEMA,
        }
        config.update(overrides)
        return _s3(_S3Store(), **config)

    def test_unencodable_custom_header_is_a_config_error_not_a_row_failure(self) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import S3RecordSerializationError

        sink = self._sink(csv_options={"encoding": "ascii"}, headers={"name": "Café"})

        raised, diverted = _preflight_outcome(sink, [{"name": "bob"}, {"name": "alice"}])

        assert isinstance(raised, ValueError), "the probe must check the display header the final object writes"
        assert not isinstance(raised, S3RecordSerializationError), "a header defect is not row-attributable"
        assert diverted == 0
        assert "Café" not in str(raised)

    def test_oversized_custom_header_is_a_config_error_not_a_row_failure(self) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import S3RecordSerializationError

        sink = self._sink(headers={"name": "H" * 64}, max_record_chars=32)

        raised, diverted = _preflight_outcome(sink, [{"name": "bob"}])

        assert isinstance(raised, ValueError), "the probe must apply max_record_chars to the display header"
        assert not isinstance(raised, S3RecordSerializationError)
        assert diverted == 0

    def test_encodable_custom_header_still_passes_the_probe(self) -> None:
        sink = self._sink(csv_options={"encoding": "latin-1"}, headers={"name": "Café"})

        raised, diverted = _preflight_outcome(sink, [{"name": "bob"}])

        assert raised is None
        assert diverted == 0
