"""Unit tests for the bounded AWS S3 sink primitives and runtime."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, ClassVar

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from elspeth.plugins.infrastructure.config_base import PluginConfigError

DYNAMIC_SCHEMA: dict[str, Any] = {"mode": "observed"}


def _base_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "bucket": "example-bucket",
        "key": "runs/{{ run_id }}/output.csv",
        "schema": DYNAMIC_SCHEMA,
    }
    config.update(overrides)
    return config


class TestAWSS3SinkConfig:
    def test_complete_sink_is_registered_in_task_two(self) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3Sink

        assert AWSS3Sink.name == "aws_s3"

    def test_all_registered_fields_have_descriptions(self) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3SinkConfig, CSVWriteOptions

        expected = {
            "bucket",
            "key",
            "format",
            "overwrite",
            "csv_options",
            "headers",
            "region_name",
            "endpoint_url",
            "max_object_bytes",
            "max_record_chars",
        }
        assert expected <= AWSS3SinkConfig.model_fields.keys()
        assert all(AWSS3SinkConfig.model_fields[name].description for name in expected)
        assert all(CSVWriteOptions.model_fields[name].description for name in ("delimiter", "encoding", "include_header"))
        assert AWSS3SinkConfig._plugin_component_type == "sink"

    @pytest.mark.parametrize(
        "field",
        [
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_session_token",
            "access_key",
            "secret_key",
            "session_token",
            "credentials",
            "client_config",
            "client_kwargs",
        ],
    )
    def test_credential_and_client_fields_are_forbidden(self, field: str) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3SinkConfig

        assert field not in AWSS3SinkConfig.model_fields
        with pytest.raises(PluginConfigError):
            AWSS3SinkConfig.from_dict(_base_config(**{field: "sentinel"}), plugin_name="aws_s3")

    @pytest.mark.parametrize("field", ["bucket", "key"])
    @pytest.mark.parametrize("value", ["", "   ", "<OPERATOR_REQUIRED>", "operator required", "operator_required"])
    def test_blank_and_operator_placeholder_locations_are_rejected(self, field: str, value: str) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3SinkConfig

        with pytest.raises(PluginConfigError):
            AWSS3SinkConfig.from_dict(_base_config(**{field: value}), plugin_name="aws_s3")

    @pytest.mark.parametrize(
        ("field", "accepted", "rejected"),
        [
            ("bucket", "b" * 2048, "b" * 2049),
            ("key", "k" * 4096, "k" * 4097),
            ("region_name", "r" * 64, "r" * 65),
            ("max_object_bytes", 1024 * 1024 * 1024, 1024 * 1024 * 1024 + 1),
            ("max_record_chars", 8_000_000, 8_000_001),
        ],
    )
    def test_maximum_boundaries(self, field: str, accepted: Any, rejected: Any) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3SinkConfig

        values = AWSS3SinkConfig.from_dict(_base_config(**{field: accepted}), plugin_name="aws_s3").model_dump()
        assert values[field] == accepted
        with pytest.raises(PluginConfigError):
            AWSS3SinkConfig.from_dict(_base_config(**{field: rejected}), plugin_name="aws_s3")

    @pytest.mark.parametrize("field", ["max_object_bytes", "max_record_chars"])
    def test_positive_size_boundaries(self, field: str) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3SinkConfig

        values = AWSS3SinkConfig.from_dict(_base_config(**{field: 1})).model_dump()
        assert values[field] == 1
        with pytest.raises(PluginConfigError):
            AWSS3SinkConfig.from_dict(_base_config(**{field: 0}))

    @pytest.mark.parametrize("value", ["bad region", "us_east_1", ""])
    def test_invalid_region_is_rejected(self, value: str) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3SinkConfig

        with pytest.raises(PluginConfigError):
            AWSS3SinkConfig.from_dict(_base_config(region_name=value))

    @pytest.mark.parametrize(
        "value",
        [
            "ftp://localhost",
            "http://",
            "http://user:pass@localhost",
            "http://localhost?q=sentinel",
            "http://localhost#sentinel",
            "http://local host",
            "http://localhost/\x00",
            "x" * 2049,
        ],
    )
    def test_invalid_endpoint_is_rejected(self, value: str) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3SinkConfig

        with pytest.raises(PluginConfigError):
            AWSS3SinkConfig.from_dict(_base_config(endpoint_url=value))

    def test_explicit_null_endpoint_is_accepted(self) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3SinkConfig

        assert AWSS3SinkConfig.from_dict(_base_config(endpoint_url=None)).endpoint_url is None

    @pytest.mark.parametrize("key", ["bad\x00key", "bad\nkey", "{{ unclosed"])
    def test_invalid_key_template_is_rejected(self, key: str) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3SinkConfig

        with pytest.raises(PluginConfigError):
            AWSS3SinkConfig.from_dict(_base_config(key=key))

    def test_undefined_template_variable_fails_at_config(self) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3SinkConfig

        with pytest.raises(PluginConfigError, match="approved variables"):
            AWSS3SinkConfig.from_dict(_base_config(key="{{ missing }}"))

    @pytest.mark.parametrize("rendered", ["", " \t", "bad\nkey", "k" * 1025])
    def test_rendered_key_is_revalidated(self, rendered: str) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import _render_key_template

        with pytest.raises(ValueError, match="rendered key"):
            _render_key_template("{{ run_id }}", run_id=rendered, timestamp="2026-07-14T00:00:00+00:00")

    def test_key_template_rejects_expressions_before_rendering(self) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import _render_key_template

        class MultiplicationSentinel(str):
            multiplied = False

            def __mul__(self, count: int) -> str:
                self.multiplied = True
                raise AssertionError(f"template attempted multiplication by {count}")

        run_id = MultiplicationSentinel("run-1")
        with pytest.raises(ValueError, match="template"):
            _render_key_template("{{ run_id * 1000000000 }}", run_id=run_id, timestamp="2026-07-14T00:00:00+00:00")

        assert run_id.multiplied is False

    def test_sink_compiles_key_template_once_during_initialization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import elspeth.plugins.sinks.aws_s3_sink as module
        from elspeth.contracts.sink_effects import RestrictedSinkEffectContext

        sink = module.AWSS3Sink(_base_config())

        def unexpected_recompile(_source: str) -> Any:
            raise AssertionError("template was recompiled after initialization")

        monkeypatch.setattr(module, "_compile_key_template", unexpected_recompile)
        ctx = RestrictedSinkEffectContext(
            run_id="run-1",
            run_started_at=datetime(2026, 7, 28, tzinfo=UTC),
            operation_id="operation-1",
            sink_node_id="sink-1",
        )

        assert sink._effect_key(ctx) == "runs/run-1/output.csv"

    @pytest.mark.parametrize("fault", [AssertionError("programmer fault"), GeneratorExit()])
    def test_key_template_programmer_and_process_control_faults_escape(self, fault: BaseException) -> None:
        from elspeth.contracts.sink_effects import RestrictedSinkEffectContext
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3Sink

        class FaultingTemplate:
            def render(self, **_values: str) -> str:
                raise fault

        sink = AWSS3Sink(_base_config())
        sink._key_template = FaultingTemplate()
        ctx = RestrictedSinkEffectContext(
            run_id="run-1",
            run_started_at=datetime(2026, 7, 28, tzinfo=UTC),
            operation_id="operation-1",
            sink_node_id="sink-1",
        )

        with pytest.raises(type(fault), match="programmer fault" if isinstance(fault, AssertionError) else None):
            sink._effect_key(ctx)

    def test_csv_options_and_headers_match_existing_sink_contract(self) -> None:
        from elspeth.contracts.header_modes import HeaderMode
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3SinkConfig

        cfg = AWSS3SinkConfig.from_dict(
            _base_config(csv_options={"delimiter": ";", "encoding": "utf-16", "include_header": False}, headers={"a": "A"})
        )
        assert cfg.csv_options.delimiter == ";"
        assert cfg.csv_options.include_header is False
        assert cfg.headers_mode is HeaderMode.CUSTOM
        assert cfg.headers_mapping == {"a": "A"}
        with pytest.raises(PluginConfigError):
            AWSS3SinkConfig.from_dict(_base_config(csv_options={"unknown": True}))

    @pytest.mark.parametrize("encoding", ["rot_13", "base64_codec"])
    def test_non_text_to_bytes_csv_codec_is_rejected_at_config(self, encoding: str) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3SinkConfig

        with pytest.raises(PluginConfigError, match="text to bytes"):
            AWSS3SinkConfig.from_dict(_base_config(csv_options={"encoding": encoding}), plugin_name="aws_s3")


def _serialize(
    rows: list[dict[str, Any]],
    *,
    format: str,
    fieldnames: list[str] | None = None,
    max_object_bytes: int = 1024 * 1024,
    max_record_chars: int = 100_000,
    **csv_overrides: Any,
) -> Any:
    from elspeth.plugins.sinks.aws_s3_sink import CSVWriteOptions, _serialize_rows_to_spool

    return _serialize_rows_to_spool(
        rows,
        format=format,
        csv_options=CSVWriteOptions(**csv_overrides),
        fieldnames=fieldnames or ["id", "name"],
        max_object_bytes=max_object_bytes,
        max_record_chars=max_record_chars,
    )


class TestSerialization:
    @pytest.mark.parametrize(
        ("format", "expected"),
        [
            ("csv", b"id,name\r\n1,Ada\r\n2,Grace\r\n"),
            ("json", b'[{"id":1,"name":"Ada"},{"id":2,"name":"Grace"}]'),
            ("jsonl", b'{"id":1,"name":"Ada"}\n{"id":2,"name":"Grace"}\n'),
        ],
    )
    def test_shapes_hashes_rewind_and_idempotent_close(self, format: str, expected: bytes) -> None:
        serialized = _serialize([{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}], format=format)
        assert serialized.body.tell() == 0
        assert serialized.body.read() == expected
        assert serialized.size_bytes == len(expected)
        digest = hashlib.sha256(expected).digest()
        assert serialized.content_hash == digest.hex()
        assert serialized.checksum_sha256_b64 == base64.b64encode(digest).decode("ascii")
        serialized.close()
        serialized.close()
        assert serialized.body.closed

    @pytest.mark.parametrize("format", ["csv", "json", "jsonl"])
    def test_exact_object_limit_max_and_max_plus_one(self, format: str) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import S3ObjectSizeLimitError

        probe = _serialize([{"id": 1, "name": "Ada"}], format=format)
        size = probe.size_bytes
        probe.close()
        exact = _serialize([{"id": 1, "name": "Ada"}], format=format, max_object_bytes=size)
        exact.close()
        above = _serialize([{"id": 1, "name": "Ada"}], format=format, max_object_bytes=size + 1)
        above.close()
        with pytest.raises(S3ObjectSizeLimitError) as captured:
            _serialize([{"id": 1, "name": "Ada"}], format=format, max_object_bytes=size - 1)
        assert captured.value.observed_bytes > captured.value.limit_bytes
        assert "Ada" not in str(captured.value)

    @pytest.mark.parametrize(("value_size", "accepted"), [(91, True), (92, True), (93, False)])
    def test_json_record_limit_max_minus_one_max_and_max_plus_one(self, value_size: int, accepted: bool) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import S3RecordSizeLimitError

        row = {"v": "x" * value_size}  # compact JSON record length is value_size + 8
        if accepted:
            serialized = _serialize([row], format="json", fieldnames=["v"], max_record_chars=100)
            serialized.close()
        else:
            with pytest.raises(S3RecordSizeLimitError):
                _serialize([row], format="json", fieldnames=["v"], max_record_chars=100)

    @pytest.mark.parametrize(("value_size", "accepted"), [(97, True), (98, True), (99, False)])
    def test_csv_record_limit_max_minus_one_max_and_max_plus_one(self, value_size: int, accepted: bool) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import S3RecordSizeLimitError

        row = {"v": "x" * value_size}  # one value plus CRLF is value_size + 2
        if accepted:
            serialized = _serialize(
                [row],
                format="csv",
                fieldnames=["v"],
                max_record_chars=100,
                include_header=False,
            )
            serialized.close()
        else:
            with pytest.raises(S3RecordSizeLimitError):
                _serialize(
                    [row],
                    format="csv",
                    fieldnames=["v"],
                    max_record_chars=100,
                    include_header=False,
                )

    @pytest.mark.parametrize("format", ["json", "jsonl"])
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
    def test_nonfinite_and_nonserializable_values_are_static_failures(self, format: str, value: object) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import S3RecordSerializationError

        with pytest.raises(S3RecordSerializationError) as captured:
            _serialize([{"id": 1, "name": value}], format=format)
        assert "object at" not in str(captured.value)

    def test_csv_unencodable_value_is_static_failure(self) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import S3RecordSerializationError

        with pytest.raises(S3RecordSerializationError) as captured:
            _serialize([{"id": 1, "name": "snowman ☃"}], format="csv", encoding="ascii")
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None

    def test_csv_sparse_rows_serialize_missing_optional_field_as_empty_cell(self) -> None:
        serialized = _serialize(
            [{"id": 1}, {"id": 2, "name": "present"}],
            format="csv",
            fieldnames=["id", "name"],
        )
        assert serialized.body.read() == b"id,name\r\n1,\r\n2,present\r\n"
        serialized.close()

    def test_stateful_csv_encoding_uses_one_incremental_encoder_and_one_bom(self) -> None:
        serialized = _serialize(
            [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}],
            format="csv",
            encoding="utf-16",
        )
        payload = serialized.body.read()
        serialized.close()
        assert payload.count(b"\xff\xfe") + payload.count(b"\xfe\xff") == 1
        assert payload.decode("utf-16") == "id,name\r\n1,Ada\r\n2,Grace\r\n"

    @pytest.mark.parametrize("encoding", ["rot_13", "base64_codec"])
    def test_non_text_codec_runtime_failure_is_static_and_cause_free(self, encoding: str) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import (
            CSVWriteOptions,
            S3RecordSerializationError,
            _serialize_rows_to_spool,
        )

        options = CSVWriteOptions.model_construct(delimiter=",", encoding=encoding, include_header=True)
        with pytest.raises(S3RecordSerializationError) as captured:
            _serialize_rows_to_spool(
                [{"id": 1}],
                format="csv",
                csv_options=options,
                fieldnames=["id"],
                max_object_bytes=1024,
                max_record_chars=100,
            )
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None

    @pytest.mark.parametrize("format", ["csv", "json", "jsonl"])
    def test_huge_integer_conversion_is_a_static_serialization_failure(self, format: str) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import S3RecordSerializationError

        with pytest.raises(S3RecordSerializationError) as captured:
            _serialize([{"id": 10**5000, "name": "Ada"}], format=format)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None

    @pytest.mark.parametrize("format", ["csv", "json", "jsonl"])
    def test_record_limit_rejects_huge_single_value_before_writing(self, format: str) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import S3RecordSizeLimitError

        with pytest.raises(S3RecordSizeLimitError):
            _serialize([{"id": 1, "name": "x" * 101}], format=format, max_record_chars=100)

    def test_many_small_rows_can_exceed_cumulative_object_limit(self) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import S3ObjectSizeLimitError

        rows = [{"id": index, "name": "x" * 10} for index in range(100)]
        with pytest.raises(S3ObjectSizeLimitError):
            _serialize(rows, format="jsonl", max_object_bytes=200)

    def test_spool_rolls_over_at_eight_mib(self) -> None:
        serialized = _serialize(
            [{"id": index, "name": "x" * 70_000} for index in range(125)],
            format="jsonl",
            max_object_bytes=20 * 1024 * 1024,
            max_record_chars=100_000,
        )
        assert serialized.size_bytes > 8 * 1024 * 1024
        assert serialized.body._rolled is True  # type: ignore[attr-defined]
        serialized.close()

    def test_context_manager_owns_and_closes_spool(self) -> None:
        with _serialize([{"id": 1, "name": "Ada"}], format="json") as serialized:
            body = serialized.body
            assert not body.closed
        assert body.closed

    def test_spool_is_closed_when_size_failure_occurs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import elspeth.plugins.sinks.aws_s3_sink as module

        real_factory = module.tempfile.SpooledTemporaryFile
        created: list[Any] = []

        def tracking_factory(*args: Any, **kwargs: Any) -> Any:
            spool = real_factory(*args, **kwargs)
            created.append(spool)
            return spool

        monkeypatch.setattr(module.tempfile, "SpooledTemporaryFile", tracking_factory)
        with pytest.raises(module.S3ObjectSizeLimitError):
            _serialize([{"id": 1, "name": "Ada"}], format="json", max_object_bytes=1)
        assert len(created) == 1
        assert created[0].closed

    def test_no_spool_write_exceeds_64_kib(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import elspeth.plugins.sinks.aws_s3_sink as module

        real_factory = module.tempfile.SpooledTemporaryFile
        writes: list[int] = []

        class TrackingSpool:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self._spool = real_factory(*args, **kwargs)

            def write(self, data: bytes) -> int:
                writes.append(len(data))
                return self._spool.write(data)

            def seek(self, offset: int, whence: int = 0) -> int:
                return self._spool.seek(offset, whence)

            def close(self) -> None:
                self._spool.close()

        monkeypatch.setattr(module.tempfile, "SpooledTemporaryFile", TrackingSpool)
        serialized = _serialize([{"id": 1, "name": "x" * 200_000}], format="json", max_record_chars=300_000)
        serialized.close()
        assert writes
        assert max(writes) <= 64 * 1024

    def test_json_output_is_valid_without_whole_document_formatting(self) -> None:
        with _serialize([{"id": 1, "name": "Ada"}], format="json") as serialized:
            assert json.load(serialized.body) == [{"id": 1, "name": "Ada"}]


class TestProviderBoundaries:
    @staticmethod
    def _sink() -> Any:
        from elspeth.plugins.sinks.aws_s3_sink import AWSS3Sink

        return AWSS3Sink(_base_config())

    def test_arbitrary_exception_cannot_masquerade_as_missing_s3_object(self) -> None:
        class MasqueradingError(Exception):
            response: ClassVar[dict[str, object]] = {
                "Error": {"Code": "NoSuchKey"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            }

        class Client:
            def head_object(self, **_kwargs: object) -> object:
                raise MasqueradingError

        sink = self._sink()
        sink._s3_client = Client()

        with pytest.raises(MasqueradingError):
            sink._observe_effect_target("object.csv")

    def test_actual_client_error_can_report_missing_s3_object(self) -> None:
        class Client:
            def head_object(self, **_kwargs: object) -> object:
                raise ClientError(
                    {
                        "Error": {"Code": "NoSuchKey", "Message": "missing"},
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                    },
                    "HeadObject",
                )

        sink = self._sink()
        sink._s3_client = Client()

        observation = sink._observe_effect_target("object.csv")

        assert observation.exists is False

    @pytest.mark.parametrize(
        ("code", "status", "expected"),
        [
            ("PreconditionFailed", 412, "conditional"),
            ("AccessDenied", 403, "rejected"),
            ("InternalError", 500, "unknown"),
        ],
    )
    def test_only_validated_client_error_evidence_is_classified(
        self,
        code: str,
        status: int,
        expected: str,
    ) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import _provider_failure_kind

        error = ClientError(
            {
                "Error": {"Code": code, "Message": "provider message"},
                "ResponseMetadata": {"HTTPStatusCode": status},
            },
            "PutObject",
        )

        assert _provider_failure_kind(error) == expected

    @pytest.mark.parametrize(
        "response",
        [
            {},
            {"Error": {"Code": "AccessDenied"}},
            {"Error": [], "ResponseMetadata": {"HTTPStatusCode": 403}},
            {"Error": {"Code": 7}, "ResponseMetadata": {"HTTPStatusCode": 403}},
            {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": "403"}},
        ],
    )
    def test_malformed_client_error_evidence_is_not_classified(self, response: object) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import _provider_failure_kind

        error = ClientError(
            {
                "Error": {"Code": "InternalError", "Message": "provider message"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            },
            "PutObject",
        )
        error.response = response

        assert _provider_failure_kind(error) == "unknown"

    def test_non_client_botocore_error_has_unknown_write_outcome(self) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import _provider_failure_kind

        error = EndpointConnectionError(endpoint_url="https://example.invalid")

        assert _provider_failure_kind(error) == "unknown"

    def test_client_resolution_fault_escapes_before_head_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sink = self._sink()

        def fail_resolution() -> Any:
            raise AssertionError("client wiring fault")

        monkeypatch.setattr(sink, "_get_s3_client", fail_resolution)

        with pytest.raises(AssertionError, match="client wiring fault"):
            sink._observe_effect_target("object.csv")

    @pytest.mark.parametrize(
        "response",
        [
            {"ContentLength": "1"},
            {"ETag": 7},
            {"Metadata": []},
            {"Metadata": {"elspeth-content-sha256": 7}},
            {"Metadata": {"elspeth-effect-id": 7}},
            {"Metadata": {"elspeth-plan-hash": 7}},
            {"Metadata": {"elspeth-protocol-version": 7}},
            {"ChecksumSHA256": 7},
            {"ChecksumSHA256": "not-base64"},
            {
                "Metadata": {"elspeth-content-sha256": "0" * 64},
                "ChecksumSHA256": base64.b64encode(b"\x01" * 32).decode("ascii"),
            },
        ],
    )
    def test_present_malformed_head_evidence_is_rejected(self, response: dict[str, object]) -> None:
        from elspeth.plugins.sinks._remote_object_effects import RemoteObjectPreconditionError

        with pytest.raises(RemoteObjectPreconditionError, match=r"malformed|diverges"):
            self._sink()._observation_from_head(response)

    def test_sparse_well_formed_head_evidence_preserves_explicit_absence(self) -> None:
        observation = self._sink()._observation_from_head(
            {
                "ContentLength": 0,
                "ETag": '"etag"',
                "Metadata": {},
            }
        )

        assert observation.exists is True
        assert observation.size_bytes == 0
        assert observation.etag == '"etag"'
        assert observation.content_hash is None
        assert observation.checksum_b64 is None

    def test_put_programmer_fault_escapes_provider_outcome_handling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import elspeth.plugins.sinks.aws_s3_sink as module
        from elspeth.contracts.sink_effects import RestrictedSinkEffectContext

        class Plan:
            effect_id = "a" * 64
            plan_hash = "b" * 64

        class Evidence:
            target = "s3://example-bucket/runs/run-1/output.csv"
            staged_size = 0
            staged_hash = "0" * 64
            precondition = "if_none_match"
            predecessor_etag = None

        class Stage:
            def open(self, _mode: str) -> BytesIO:
                return BytesIO()

        class Client:
            def put_object(self, **_kwargs: object) -> object:
                raise AssertionError("put wiring fault")

        def validated_plan(_plan: object, *, provider: str, require_stage: bool) -> tuple[Evidence, Stage]:
            assert provider == "aws_s3"
            assert require_stage is True
            return Evidence(), Stage()

        sink = self._sink()
        sink._s3_client = Client()
        monkeypatch.setattr(module, "validate_remote_plan", validated_plan)
        ctx = RestrictedSinkEffectContext(
            run_id="run-1",
            run_started_at=datetime(2026, 7, 28, tzinfo=UTC),
            operation_id="operation-1",
            sink_node_id="sink-1",
        )

        with pytest.raises(AssertionError, match="put wiring fault"):
            sink.commit_effect(Plan(), ctx)

    def test_provider_close_failure_is_redacted(self) -> None:
        from elspeth.plugins.sinks.aws_s3_sink import S3ClientCloseError

        class Client:
            def close(self) -> None:
                raise EndpointConnectionError(endpoint_url="https://secret.example")

        sink = self._sink()
        sink._s3_client = Client()

        with pytest.raises(S3ClientCloseError, match="EndpointConnectionError") as captured:
            sink.close()

        assert "secret.example" not in str(captured.value)

    @pytest.mark.parametrize("fault", [AssertionError("close wiring fault"), GeneratorExit()])
    def test_close_programmer_and_process_control_faults_escape(self, fault: BaseException) -> None:
        class Client:
            def close(self) -> None:
                raise fault

        sink = self._sink()
        sink._s3_client = Client()

        with pytest.raises(type(fault), match="close wiring fault" if isinstance(fault, AssertionError) else None):
            sink.close()
