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


class TestFinding65AzureHeaderEncodeIsConfigError:
    """#65: an unencodable Azure CSV header must not divert every clean row."""

    @staticmethod
    def _sink(**overrides: object) -> Any:
        from tests.unit.plugins.sinks.test_remote_object_sink_effects import _azure, _AzureStore

        config: dict[str, object] = {"blob_path": "out.csv", "format": "csv", "schema": _FIXED_NAME_SCHEMA}
        config.update(overrides)
        return _azure(_AzureStore(), **config)

    def test_unencodable_custom_header_raises_config_error_without_diverting(self) -> None:
        from elspeth.plugins.sinks.azure_blob_sink import AzureBlobRecordSerializationError

        sink = self._sink(csv_options={"encoding": "ascii"}, headers={"name": "Prénom"})

        raised, diverted = _preflight_outcome(sink, [{"name": "bob"}, {"name": "alice"}])

        assert diverted == 0, "clean rows must not be diverted for a header (configuration) defect"
        assert isinstance(raised, ValueError)
        assert not isinstance(raised, AzureBlobRecordSerializationError)
        assert "Prénom" not in str(raised)

    def test_unencodable_row_value_is_still_diverted(self) -> None:
        sink = self._sink(csv_options={"encoding": "ascii"})

        raised, diverted = _preflight_outcome(sink, [{"name": "bob"}, {"name": "Zoë"}])

        assert raised is None
        assert diverted == 1


class _Closable:
    def __init__(self, *, fail: bool = False) -> None:
        self.close_calls = 0
        self._fail = fail

    def close(self) -> None:
        self.close_calls += 1
        if self._fail:
            raise RuntimeError("transport close failed")


class _ServiceClientFake(_Closable):
    """Stands in for BlobServiceClient: owns the transport and the credential."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(fail=fail)
        self.credential = _Closable()
        self.container_client = _ContainerClientFake()

    def get_container_client(self, _container: str) -> _ContainerClientFake:
        return self.container_client


class _ContainerClientFake(_Closable):
    """A derived client: in the SDK its close() is a TransportWrapper no-op."""

    account_name = "acct"

    def __init__(self) -> None:
        super().__init__()
        self.blob_client = _Closable()

    def get_blob_client(self, _blob_path: str) -> _Closable:
        return self.blob_client


_MI_AUTH: dict[str, object] = {"use_managed_identity": True, "account_url": "https://acct.blob.core.windows.net"}
_PATCH_SERVICE_CLIENT = "elspeth.plugins.infrastructure.azure_auth.AzureAuthConfig.create_blob_service_client"


class TestFinding64AzureCloseReleasesOwnedClients:
    """#64: close() must release the BlobServiceClient transport and credential it created."""

    @staticmethod
    def _sink() -> Any:
        from elspeth.plugins.sinks.azure_blob_sink import AzureBlobSink
        from tests.fixtures.base_classes import inject_write_failure

        return inject_write_failure(
            AzureBlobSink({"container": "c", "blob_path": "out.json", "format": "json", "schema": {"mode": "observed"}, **_MI_AUTH})
        )

    def test_sink_close_closes_service_client_and_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = _ServiceClientFake()
        monkeypatch.setattr(_PATCH_SERVICE_CLIENT, lambda _self: service)
        sink = self._sink()
        sink._get_container_client()

        sink.close()

        assert service.close_calls == 1, "the BlobServiceClient owns the HTTP transport; close() must close it"
        assert service.credential.close_calls == 1, "the managed-identity credential must be closed"

    def test_sink_close_is_not_retried_after_a_raising_close(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = _ServiceClientFake(fail=True)
        monkeypatch.setattr(_PATCH_SERVICE_CLIENT, lambda _self: service)
        sink = self._sink()
        sink._get_container_client()

        first: BaseException | None = None
        try:
            sink.close()
        except RuntimeError as exc:
            first = exc
        sink.close()

        assert first is not None
        assert service.close_calls == 1
        assert service.credential.close_calls == 1, "a raising transport close must not skip the credential"

    def test_source_close_closes_service_client_and_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.plugins.sources.azure_blob_source import AzureBlobSource

        service = _ServiceClientFake()
        monkeypatch.setattr(_PATCH_SERVICE_CLIENT, lambda _self: service)
        source = AzureBlobSource(
            {
                "container": "c",
                "blob_path": "in.csv",
                "schema": {"mode": "observed"},
                "on_validation_failure": "quarantine",
                **_MI_AUTH,
            }
        )
        source._get_blob_client()

        source.close()

        assert service.container_client.blob_client.close_calls == 1
        assert service.close_calls == 1
        assert service.credential.close_calls == 1


class TestFinding66AzureJsonStreaming:
    """#66: Azure JSON/JSONL must stream into the bounded stage and terminate every JSONL record."""

    @pytest.mark.parametrize("fmt", ["json", "jsonl"])
    def test_cumulative_json_is_streamed_into_the_stage_limit(self, fmt: str, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.plugins.sinks import _remote_object_effects as remote_effects
        from tests.unit.plugins.sinks.test_remote_object_sink_effects import _azure, _AzureStore, _member, _prepare

        sink = _azure(_AzureStore(), blob_path=f"out.{fmt}", format=fmt, max_blob_bytes=8)
        original = sink._serialize_rows
        batch_sizes: list[int] = []

        def recording(rows: list[dict[str, Any]]) -> bytes:
            batch_sizes.append(len(rows))
            return original(rows)

        monkeypatch.setattr(sink, "_serialize_rows", recording)
        first = _member(0, {"id": 1})
        second = _member(1, {"id": 2})
        raised: BaseException | None = None
        try:
            _prepare(sink, effect_id="c" * 64, current=(first, second), target_snapshot=(first, second))
        except Exception as exc:
            raised = exc

        assert max(batch_sizes) == 1, "the cumulative body must not be materialised as one bytes object"
        assert isinstance(raised, remote_effects.RemoteObjectEffectLimitError)

    def test_jsonl_terminates_every_record(self) -> None:
        from pathlib import Path

        from tests.unit.plugins.sinks.test_remote_object_sink_effects import _azure, _AzureStore, _member, _prepare

        sink = _azure(_AzureStore(), blob_path="out.jsonl", format="jsonl")
        first = _member(0, {"id": 1})
        second = _member(1, {"id": 2})

        plan = _prepare(sink, effect_id="d" * 64, current=(first, second), target_snapshot=(first, second))

        body = Path(str(plan.safe_evidence["staging_path"])).read_bytes()
        assert body == b'{"id": 1}\n{"id": 2}\n'

    def test_json_array_bytes_are_unchanged(self) -> None:
        import json
        from pathlib import Path

        from tests.unit.plugins.sinks.test_remote_object_sink_effects import _azure, _AzureStore, _member, _prepare

        sink = _azure(_AzureStore(), blob_path="out.json", format="json")
        rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        members = tuple(_member(index, row) for index, row in enumerate(rows))

        plan = _prepare(sink, effect_id="e" * 64, current=members, target_snapshot=members)

        body = Path(str(plan.safe_evidence["staging_path"])).read_bytes()
        assert body == json.dumps(rows, indent=2).encode("utf-8")


class TestFinding19AzureSourceCsvDecodesPerRecord:
    """#19: one undecodable CSV byte must quarantine its record, not the whole blob."""

    @staticmethod
    def _load(blob: bytes, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        from elspeth.plugins.sources.azure_blob_source import AzureBlobSource
        from tests.fixtures.factories import make_operation_context
        from tests.unit.plugins.sources.test_azure_blob_source import _fake_blob_service

        source = AzureBlobSource(
            {
                "connection_string": "DefaultEndpointsProtocol=https;AccountName=fake;AccountKey=ZmFrZQ==;EndpointSuffix=core.windows.net",
                "container": "c",
                "blob_path": "in.csv",
                "schema": {"mode": "observed"},
                "on_validation_failure": "quarantine",
            }
        )
        service = _fake_blob_service(blob)
        monkeypatch.setattr(_PATCH_SERVICE_CLIENT, lambda _self: service)
        return list(source.load(make_operation_context(plugin_name="azure_blob")))

    def test_bad_byte_quarantines_only_its_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = self._load(b"id,name\n1,a\n2,\xff\n3,c\n", monkeypatch)

        assert [row.is_quarantined for row in rows] == [False, True, False]
        assert [rows[0].row["id"], rows[2].row["id"]] == ["1", "3"]
        quarantined = rows[1].row
        assert quarantined["__line_number__"] == "3"
        assert quarantined["__raw_bytes_hex__"] == b"2,\xff".hex()

    def test_undecodable_header_still_quarantines_the_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = self._load(b"\xff\xfe\n1,a\n", monkeypatch)

        assert len(rows) == 1
        assert rows[0].is_quarantined is True
        assert "decode" in rows[0].quarantine_error.lower()
