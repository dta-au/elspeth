"""Runtime preflight must not touch external systems during plugin setup."""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from pydantic import SecretBytes

from elspeth.core.config import ElspethSettings, load_bounded_pipeline_yaml, load_settings_from_yaml_string, resolve_config
from elspeth.plugins.infrastructure.preflight import plugin_preflight_mode, plugin_preflight_mode_enabled
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.plugins.sinks.csv_sink import CSVSink
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.sources.llm import LLMSource
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution import preflight as execution_preflight
from elspeth.web.execution.preflight import (
    audit_safe_resolved_config,
    build_validated_runtime_graph,
    instantiate_runtime_plugins,
    make_policy_bound_sink_factory,
)
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from elspeth.web.plugin_policy.models import (
    PluginAvailability,
    PluginAvailabilitySnapshot,
    PluginId,
    PluginUnavailableReason,
)


def _forbid_socket_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("runtime preflight plugin instantiation must not open network sockets")

    monkeypatch.setattr(socket, "socket", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)


def _web_settings(tmp_path: Path) -> WebSettings:
    return WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=10,
        composer_max_discovery_turns=5,
        composer_timeout_seconds=30.0,
        composer_rate_limit_per_minute=60,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
    )


def _csv_worker_probe_state(tmp_path: Path) -> CompositionState:
    blobs_dir = tmp_path / "blobs" / "test-session"
    outputs_dir = tmp_path / "outputs" / "test-session"
    blobs_dir.mkdir(parents=True)
    outputs_dir.mkdir(parents=True)
    input_path = blobs_dir / "input.csv"
    input_path.write_text("name\nAda\n", encoding="utf-8")
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="primary",
            options={"path": str(input_path), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="primary",
                plugin="csv",
                options={"path": str(outputs_dir / "out.csv"), "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _chroma_persist_outside_data_dir_state(tmp_path: Path) -> CompositionState:
    blobs_dir = tmp_path / "blobs" / "test-session"
    outputs_dir = tmp_path / "outputs" / "test-session"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    input_path = blobs_dir / "input.csv"
    input_path.write_text("id,text\n1,Ada\n", encoding="utf-8")
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="primary",
            options={"path": str(input_path), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="primary",
                plugin="chroma_sink",
                options={
                    "collection": "docs",
                    "mode": "persistent",
                    "persist_directory": str(tmp_path.parent / "outside-chroma"),
                    "field_mapping": {"id_field": "id", "document_field": "text"},
                    "schema": {"mode": "fixed", "fields": ["id: str", "text: str"]},
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _minimal_csv_pipeline_yaml(tmp_path: Path) -> str:
    """Minimal CSV source → CSV sink pipeline YAML with absolute paths under tmp_path."""
    blobs_dir = tmp_path / "blobs"
    outputs_dir = tmp_path / "outputs"
    blobs_dir.mkdir(exist_ok=True)
    outputs_dir.mkdir(exist_ok=True)
    input_path = blobs_dir / "probe_input.csv"
    input_path.write_text("name\nAda\n", encoding="utf-8")
    return f"""\
sources:
  primary:
    plugin: csv
    on_success: output
    options:
      path: {input_path!s}
      on_validation_failure: discard
      schema:
        mode: observed
sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: {outputs_dir / "probe_output.csv"!s}
      schema:
        mode: observed
"""


def _snapshot_without(plugin_id: PluginId) -> PluginAvailabilitySnapshot:
    unrestricted = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
    return PluginAvailabilitySnapshot.create(
        policy_hash="runtime-policy",
        principal_scope="test:alice",
        available=unrestricted.available - {plugin_id},
        unavailable=(PluginAvailability(plugin_id, PluginUnavailableReason.NOT_AUTHORIZED),),
        selected=unrestricted.selected,
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="runtime-policy-generation",
    )


def _snapshot_with_profiles(*profiles: tuple[PluginId, str]) -> PluginAvailabilitySnapshot:
    unrestricted = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
    return PluginAvailabilitySnapshot.create(
        policy_hash="runtime-policy",
        principal_scope="test:alice",
        available=unrestricted.available,
        unavailable=(),
        selected=unrestricted.selected,
        usable_profile_aliases=tuple((plugin_id, (alias,)) for plugin_id, alias in profiles),
        selected_profile_aliases=tuple(profiles),
        binding_generation_fingerprint="runtime-policy-generation",
    )


def _profiled_llm_audit_inputs(
    tmp_path: Path,
    *,
    source_name: str = "primary",
) -> tuple[ElspethSettings, dict[str, Any], PluginAvailabilitySnapshot]:
    (tmp_path / "blobs").mkdir(exist_ok=True)
    (tmp_path / "outputs").mkdir(exist_ok=True)
    input_path = tmp_path / "blobs" / "profile_input.csv"
    input_path.write_text("text\nAda\n", encoding="utf-8")
    executable_yaml = f"""\
sources:
  {source_name}:
    plugin: csv
    on_success: llm_step
    options:
      path: {input_path}
      on_validation_failure: discard
      schema:
        mode: observed
transforms:
  - name: llm_step
    plugin: llm
    input: llm_step
    on_success: output
    on_error: discard
    options:
      provider: openrouter
      api_key: probe-key
      model: openai/gpt-4o
      prompt_template: "Summarise {{{{ row.text }}}}"
      schema:
        mode: observed
      required_input_fields: []
sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: {tmp_path / "outputs" / "profile_output.csv"}
      schema:
        mode: observed
"""
    settings = load_settings_from_yaml_string(executable_yaml)
    audit_safe = load_bounded_pipeline_yaml(executable_yaml)
    assert type(audit_safe) is dict
    transforms = audit_safe["transforms"]
    assert type(transforms) is list
    llm_options = transforms[0]["options"]
    transforms[0]["options"] = {
        "profile": "tutorial",
        "prompt_template": llm_options["prompt_template"],
        "schema": llm_options["schema"],
        "required_input_fields": llm_options["required_input_fields"],
    }
    snapshot = _snapshot_with_profiles((PluginId("transform", "llm"), "tutorial"))
    return settings, audit_safe, snapshot


def test_profiled_s3_runtime_uses_private_binding_only_for_boto_call(tmp_path: Path) -> None:
    import yaml

    from elspeth.contracts.freeze import deep_thaw
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
    from elspeth.plugins.sources.aws_s3_source import AWSS3Source
    from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

    private_bucket = "operator-private-bucket-marker"
    private_prefix = "operator-private-prefix-marker"
    relative_key = "records/input.csv"
    profile_alias = "demo-input"
    web_settings = WebSettings.model_validate(
        {
            **_web_settings(tmp_path).model_dump(),
            "plugin_allowlist": ["source:aws_s3"],
            "deployment_aws_region": "ap-southeast-1",
            "aws_s3_source_profiles": [
                {
                    "alias": profile_alias,
                    "bucket": private_bucket,
                    "prefix": private_prefix,
                }
            ],
        }
    )
    runtime_config = RuntimeWebPluginConfig.from_settings(web_settings)
    profiles = OperatorProfileRegistry(
        policy=compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime_config),
        settings=runtime_config,
    )
    lowered = profiles.lower_options(
        PluginId("source", "aws_s3"),
        alias=profile_alias,
        safe_options={
            "key": relative_key,
            "format": "csv",
            "schema": {"mode": "observed"},
            "on_validation_failure": "quarantine",
        },
    )
    output_path = tmp_path / "outputs" / "profiled-s3.csv"
    output_path.parent.mkdir(exist_ok=True)
    executable_config = {
        "sources": {
            "primary": {
                "plugin": "aws_s3",
                "on_success": "quarantine",
                "options": deep_thaw(lowered.executable_options),
            }
        },
        "sinks": {
            "quarantine": {
                "plugin": "csv",
                "on_write_failure": "discard",
                "options": {"path": str(output_path), "schema": {"mode": "observed"}},
            }
        },
    }
    audit_safe_config = {
        **executable_config,
        "sources": {
            "primary": {
                "plugin": "aws_s3",
                "on_success": "quarantine",
                "options": deep_thaw(lowered.audit_safe_options),
            }
        },
    }
    settings = load_settings_from_yaml_string(yaml.safe_dump(executable_config))
    snapshot = _snapshot_with_profiles((PluginId("source", "aws_s3"), profile_alias))
    identity = lowered.profiled_s3_audit_identity
    assert identity is not None

    with (
        patch.object(execution_preflight, "instantiate_runtime_plugins", wraps=instantiate_runtime_plugins) as instantiate,
        pytest.raises(ValueError, match="requires audit-safe settings"),
    ):
        build_validated_runtime_graph(
            settings,
            plugin_snapshot=snapshot,
            profiled_s3_audit_identities=(("primary", identity),),
        )
    instantiate.assert_not_called()

    with pytest.raises(KeyError, match="audit identities have no source"):
        build_validated_runtime_graph(
            settings,
            plugin_snapshot=snapshot,
            audit_safe_settings=audit_safe_config,
        )

    runtime = build_validated_runtime_graph(
        settings,
        plugin_snapshot=snapshot,
        audit_safe_settings=audit_safe_config,
        profiled_s3_audit_identities=(("primary", identity),),
    )
    source = runtime.plugin_bundle.sources["primary"]
    assert isinstance(source, AWSS3Source)

    class _Body:
        def read(self, _size: int) -> bytes:
            return b""

        def close(self) -> None:
            return None

    class _Client:
        def __init__(self) -> None:
            self.head_calls: list[dict[str, object]] = []
            self.get_calls: list[dict[str, object]] = []

        def head_object(self, **kwargs: object) -> object:
            self.head_calls.append(kwargs)
            return {"ContentLength": 0, "ETag": '"etag"'}

        def get_object(self, **kwargs: object) -> object:
            self.get_calls.append(kwargs)
            return {"ContentLength": 0, "Body": _Body()}

        def close(self) -> None:
            return None

    class _Context:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.validation_errors: list[dict[str, object]] = []

        def record_call(self, **kwargs: object) -> None:
            self.calls.append(kwargs)

        def record_validation_error(self, **kwargs: object) -> None:
            self.validation_errors.append(kwargs)

    client = _Client()
    context = _Context()
    source._s3_client = client
    rows = list(source.load(cast(Any, context)))

    executable_key = f"{private_prefix}/{relative_key}"
    assert client.head_calls == [{"Bucket": private_bucket, "Key": executable_key}]
    assert client.get_calls == [{"Bucket": private_bucket, "Key": executable_key, "IfMatch": '"etag"'}]
    assert context.calls[0]["request_data"] == {
        "operation": "read_object",
        "profile": profile_alias,
        "key": relative_key,
    }
    assert context.validation_errors[0]["row"] == {
        "profile": profile_alias,
        "key": relative_key,
        "error": "CSV parse error: empty file contains no header row",
    }
    assert rows[0].row == context.validation_errors[0]["row"]
    assert source.config == deep_thaw(lowered.audit_safe_options)

    graph_configs = [runtime.graph.get_node_info(node_id).config for node_id in runtime.graph.topological_order()]
    persisted_projection = json.dumps(
        {
            "source_config": source.config,
            "graph_configs": graph_configs,
            "call_audit": context.calls,
            "validation_errors": context.validation_errors,
            "quarantine_rows": [row.row for row in rows],
        },
        default=dict,
    )
    assert profile_alias in persisted_projection
    assert relative_key in persisted_projection
    assert private_bucket not in persisted_projection
    assert private_prefix not in persisted_projection
    assert executable_key not in persisted_projection


def test_profiled_textract_runtime_uses_private_binding_only_for_aws_calls(tmp_path: Path) -> None:
    """Custody NFR (ADR-036, elspeth-cd0f6a6cd9): a profiled Textract run must
    persist ZERO call records containing the operator bucket literal."""
    import yaml

    from elspeth.contracts.call_data import RawCallPayload
    from elspeth.contracts.freeze import deep_thaw
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
    from elspeth.plugins.transforms.aws.textract_document_analysis import AWSTextractDocumentAnalysis
    from elspeth.testing import make_pipeline_row
    from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

    private_bucket = "operator-private-bucket-marker"
    private_prefix = "operator-private-prefix-marker"
    profile_alias = "acceptance-docs"
    transform_id = PluginId("transform", "aws_textract_document_analysis")
    web_settings = WebSettings.model_validate(
        {
            **_web_settings(tmp_path).model_dump(),
            "plugin_allowlist": ["transform:aws_textract_document_analysis"],
            "deployment_aws_region": "ap-southeast-1",
            "aws_textract_profiles": [
                {
                    "alias": profile_alias,
                    "bucket": private_bucket,
                    "key_prefix": private_prefix,
                }
            ],
        }
    )
    runtime_config = RuntimeWebPluginConfig.from_settings(web_settings)
    profiles = OperatorProfileRegistry(
        policy=compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime_config),
        settings=runtime_config,
    )
    lowered = profiles.lower_options(
        transform_id,
        alias=profile_alias,
        safe_options={
            "key_field": "document_key",
            "feature_types": ["FORMS"],
            "text_field": "textract_text",
            "schema": {"mode": "observed"},
        },
    )
    blobs_dir = tmp_path / "blobs"
    outputs_dir = tmp_path / "outputs"
    blobs_dir.mkdir(exist_ok=True)
    outputs_dir.mkdir(exist_ok=True)
    input_path = blobs_dir / "manifest.csv"
    input_path.write_text("document_key\ninvoice.pdf\n", encoding="utf-8")
    executable_config = {
        "sources": {
            "primary": {
                "plugin": "csv",
                "on_success": "docs_in",
                "options": {"path": str(input_path), "on_validation_failure": "discard", "schema": {"mode": "observed"}},
            }
        },
        "transforms": [
            {
                "name": "textract_1",
                "plugin": "aws_textract_document_analysis",
                "input": "docs_in",
                "on_success": "output",
                "on_error": "discard",
                "options": deep_thaw(lowered.executable_options),
            }
        ],
        "sinks": {
            "output": {
                "plugin": "csv",
                "on_write_failure": "discard",
                "options": {"path": str(outputs_dir / "profiled-textract.csv"), "schema": {"mode": "observed"}},
            }
        },
    }
    audit_safe_config = {
        **executable_config,
        "transforms": [
            {
                "name": "textract_1",
                "plugin": "aws_textract_document_analysis",
                "input": "docs_in",
                "on_success": "output",
                "on_error": "discard",
                "options": deep_thaw(lowered.audit_safe_options),
            }
        ],
    }
    settings = load_settings_from_yaml_string(yaml.safe_dump(executable_config))
    snapshot = _snapshot_with_profiles((transform_id, profile_alias))
    identity = lowered.profiled_textract_audit_identity
    assert identity is not None

    with pytest.raises(ValueError, match="profiled Textract runtime requires audit-safe settings"):
        build_validated_runtime_graph(
            settings,
            plugin_snapshot=snapshot,
            profiled_textract_audit_identities=(("textract_1", identity),),
        )

    with pytest.raises(KeyError, match="audit identities have no transform"):
        build_validated_runtime_graph(
            settings,
            plugin_snapshot=snapshot,
            audit_safe_settings=audit_safe_config,
        )

    runtime = build_validated_runtime_graph(
        settings,
        plugin_snapshot=snapshot,
        audit_safe_settings=audit_safe_config,
        profiled_textract_audit_identities=(("textract_1", identity),),
    )
    transform = next(wired.plugin for wired in runtime.plugin_bundle.transforms if wired.settings.name == "textract_1")
    assert isinstance(transform, AWSTextractDocumentAnalysis)
    assert transform.config == deep_thaw(lowered.audit_safe_options)

    class _HeadBucketSDK:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def head_bucket(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return {
                "BucketRegion": "ap-southeast-1",
                "ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {}, "RetryAttempts": 0},
            }

        def close(self) -> None:
            return None

    class _TextractSDK:
        def __init__(self) -> None:
            self.start_calls: list[dict[str, object]] = []

        def start_document_analysis(self, **kwargs: object) -> object:
            self.start_calls.append(kwargs)
            return {"JobId": "job-1", "ResponseMetadata": {"RequestId": "r", "RetryAttempts": 0, "HTTPStatusCode": 200}}

        def get_document_analysis(self, **kwargs: object) -> object:
            return {
                "JobStatus": "SUCCEEDED",
                "DocumentMetadata": {"Pages": 1},
                "AnalyzeDocumentModelVersion": "1.0",
                "Blocks": [
                    {"BlockType": "PAGE", "Id": "page-1", "Page": 1},
                    {"BlockType": "LINE", "Id": "line-1", "Page": 1, "Text": "hello", "Confidence": 99.0},
                ],
                "ResponseMetadata": {"RequestId": "r", "RetryAttempts": 0, "HTTPStatusCode": 200},
            }

        def close(self) -> None:
            return None

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def allocate_call_index(self, state_id: str) -> int:
            del state_id
            return len(self.calls)

        def record_call(self, **kwargs: object) -> SimpleNamespace:
            self.calls.append(kwargs)
            return SimpleNamespace(id=f"call-{len(self.calls)}")

    recorder = _Recorder()
    telemetry_events: list[object] = []
    head_bucket_sdk = _HeadBucketSDK()
    textract_sdk = _TextractSDK()
    transform._recorder = cast(Any, recorder)
    transform._run_id = "run-1"
    transform._node_id = "textract_1"
    transform._telemetry_emit = telemetry_events.append
    transform._s3_sdk_client = head_bucket_sdk
    transform._sdk_client = textract_sdk
    transform._poll_interval_seconds = 0.001
    transform._poll_max_interval_seconds = 0.001

    result = transform._process_single_with_state(
        make_pipeline_row({"document_key": "invoice.pdf"}),
        "state-1",
        token_id="token-1",
    )

    executable_key = f"{private_prefix}/invoice.pdf"
    assert result.status == "success"
    assert head_bucket_sdk.calls == [{"Bucket": private_bucket}]
    assert textract_sdk.start_calls[0]["DocumentLocation"] == {"S3Object": {"Bucket": private_bucket, "Name": executable_key}}

    graph_configs = [runtime.graph.get_node_info(node_id).config for node_id in runtime.graph.topological_order()]
    persisted_projection = json.dumps(
        {
            "transform_config": transform.config,
            "graph_configs": graph_configs,
            "call_audit": recorder.calls,
            "telemetry": telemetry_events,
            "result_reason": result.success_reason,
        },
        default=lambda value: value.to_dict() if isinstance(value, RawCallPayload) else str(value),
    )
    assert profile_alias in persisted_projection
    assert '"key": "invoice.pdf"' in persisted_projection
    assert private_bucket not in persisted_projection
    assert private_prefix not in persisted_projection
    assert executable_key not in persisted_projection


def test_unprofiled_s3_runtime_without_audit_safe_carrier_retains_raw_cli_identity(tmp_path: Path) -> None:
    import yaml

    from elspeth.plugins.sources.aws_s3_source import AWSS3Source

    output_path = tmp_path / "outputs" / "raw-s3.csv"
    output_path.parent.mkdir(exist_ok=True)
    raw_config = {
        "sources": {
            "primary": {
                "plugin": "aws_s3",
                "on_success": "output",
                "options": {
                    "bucket": "raw-cli-bucket",
                    "key": "raw/records.csv",
                    "region_name": "ap-southeast-1",
                    "endpoint_url": "https://minio.operator.invalid",
                    "format": "csv",
                    "schema": {"mode": "observed"},
                    "on_validation_failure": "discard",
                },
            }
        },
        "sinks": {
            "output": {
                "plugin": "csv",
                "on_write_failure": "discard",
                "options": {"path": str(output_path), "schema": {"mode": "observed"}},
            }
        },
    }
    settings = load_settings_from_yaml_string(yaml.safe_dump(raw_config))

    runtime = build_validated_runtime_graph(
        settings,
        plugin_snapshot=PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service()),
    )

    source = runtime.plugin_bundle.sources["primary"]
    assert isinstance(source, AWSS3Source)
    assert source.config["bucket"] == "raw-cli-bucket"
    assert source.config["key"] == "raw/records.csv"
    assert source.config["endpoint_url"] == "https://minio.operator.invalid"
    assert source._audit_object_identity() == {"bucket": "raw-cli-bucket", "key": "raw/records.csv"}


def _external_plugin_probe_pipeline_yaml(tmp_path: Path) -> str:
    """Pipeline YAML with representative external plugins for constructor-purity checks.

    Source: CSV (guaranteed importable, no network in constructor).
    Transforms:
      - llm (openrouter provider, probe_config): client deferred to on_start()
        via _create_provider(); constructor does schema/config parsing only.
      - web_scrape (probe_config): HTTP session deferred to on_start(); constructor
        computes IP allowlist and parses config.
    Sinks include all risky external-client families available in this checkout:
      - azure_blob: BlobServiceClient deferred via _get_blob_client() lazy property.
      - dataverse: DataverseClient/credential deferred to on_start().
      - chroma_sink: chromadb.HttpClient deferred to on_start().
      - csv: baseline, no external client.

    The RAG transform is omitted from the transform chain because it requires the
    on_start() provider (azure_search) for construction of a live RetrievalProvider,
    and adding it as a transform would require the full LifecycleContext during
    graph validation which is outside the scope of constructor-purity tests.
    RAG's on_start()-deferred pattern is structurally identical to the LLM transform
    already covered, and the sink representatives ensure coverage of all four
    external-client families.
    """
    blobs_dir = tmp_path / "blobs"
    outputs_dir = tmp_path / "outputs"
    blobs_dir.mkdir(exist_ok=True)
    outputs_dir.mkdir(exist_ok=True)
    input_path = blobs_dir / "external_probe_input.csv"
    input_path.write_text("llm_probe_text,web_scrape_probe_url\ntest,http://example.com\n", encoding="utf-8")
    return f"""\
sources:
  primary:
    plugin: csv
    on_success: llm_step
    options:
      path: {input_path!s}
      on_validation_failure: discard
      schema:
        mode: observed
transforms:
  - name: llm_step
    plugin: llm
    input: llm_step
    on_success: scrape_step
    on_error: discard
    options:
      provider: openrouter
      api_key: probe-key
      model: openai/gpt-4o
      prompt_template: "{{{{ row.llm_probe_text }}}}"
      schema:
        mode: observed
      required_input_fields: []
  - name: scrape_step
    plugin: web_scrape
    input: scrape_step
    on_success: csv_primary
    on_error: discard
    options:
      schema:
        mode: observed
      url_field: web_scrape_probe_url
      content_field: page_content
      fingerprint_field: page_fingerprint
      http:
        abuse_contact: invariants@example.com
        scraping_reason: ADR-009 invariant probe
        allowed_hosts:
          - 93.184.216.34/32
sinks:
  csv_primary:
    plugin: csv
    on_write_failure: discard
    options:
      path: {outputs_dir / "external_probe_output.csv"!s}
      schema:
        mode: observed
  azure_blob_probe:
    plugin: azure_blob
    on_write_failure: discard
    options:
      container: probe-container
      blob_path: probe/output.csv
      format: csv
      schema:
        mode: observed
      connection_string: DefaultEndpointsProtocol=https;AccountName=probe;AccountKey=cHJvYmUK;EndpointSuffix=core.windows.net
  dataverse_probe:
    plugin: dataverse
    on_write_failure: discard
    options:
      environment_url: https://invariant.example.crm.dynamics.com
      entity: probe_entity
      alternate_key: probe_field
      schema:
        mode: observed
      auth:
        method: managed_identity
      field_mapping:
        probe_field: probe_field
  chroma_probe:
    plugin: chroma_sink
    on_write_failure: discard
    options:
      collection: probe-collection
      mode: client
      host: invariant.example.com
      port: 8000
      ssl: true
      schema:
        mode: observed
      field_mapping:
        document_field: page_content
        id_field: probe_id
"""


def test_preflight_mode_instantiates_external_plugins_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Representative external constructors stay pure in preflight mode.

    Include plugins whose real runtime path creates Azure, Dataverse, OpenAI/
    OpenRouter, RAG, Chroma, or HTTP clients in lifecycle methods. Prefer each
    plugin's probe_config() where it exists; the test is about constructor
    purity, not live credentials.
    """
    pipeline_yaml = _external_plugin_probe_pipeline_yaml(tmp_path)
    settings = load_settings_from_yaml_string(pipeline_yaml)

    _forbid_socket_calls(monkeypatch)
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True)

    assert bundle.sources
    assert bundle.sinks


def test_llm_source_constructor_is_network_free_without_executable_profile_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_socket_calls(monkeypatch)
    config = {
        "provider": "openrouter",
        "model": "openai/gpt-5-mini",
        "api_key": "resolved-secret",
        "prompt_template": "Write one audit briefing.",
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }

    with (
        patch.object(LLMSource, "_create_provider", autospec=True) as create_provider,
        plugin_preflight_mode(True),
    ):
        source = LLMSource(config)

    create_provider.assert_not_called()
    assert "profile_alias" not in source.config
    assert source.provider_config.provider == "openrouter"


def test_llm_source_audit_projection_uses_authored_profile_and_restores_executable_config() -> None:
    source = LLMSource(
        {
            "provider": "openrouter",
            "model": "openai/gpt-5-mini",
            "api_key": "resolved-secret",
            "prompt_template": "Write one audit briefing.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        }
    )
    bundle = cast(
        Any,
        SimpleNamespace(
            sources={"generated_brief": source},
            transforms=(),
            aggregations={},
            sinks={},
        ),
    )
    audit_safe = {
        "sources": {
            "generated_brief": {
                "plugin": "llm",
                "on_success": "output",
                "options": {
                    "profile": "source-profile",
                    "prompt_template": "Write one audit briefing.",
                    "schema": {"mode": "observed"},
                    "on_validation_failure": "discard",
                },
            }
        },
        "transforms": [],
        "aggregations": [],
        "sinks": {},
    }
    snapshot = _snapshot_with_profiles((PluginId("source", "llm"), "source-profile"))
    executable_config = source.config

    with execution_preflight._audit_safe_plugin_configs(
        bundle,
        audit_safe_settings=audit_safe,
        plugin_snapshot=snapshot,
    ):
        assert source.config["profile"] == "source-profile"
        assert "provider" not in source.config
        assert "api_key" not in source.config

    assert source.config is executable_config
    assert "profile_alias" not in source.config


@pytest.mark.asyncio
async def test_run_sync_in_worker_preserves_preflight_mode_for_plugin_constructors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Constructors see preflight mode through the production worker path.

    This pins the runtime contract, not the ContextVar implementation detail:
    validate_pipeline_for_trained_operator() may run in a ThreadPoolExecutor via run_sync_in_worker(),
    and constructors must still observe preflight mode inside that worker.
    """
    observed: list[tuple[str, bool]] = []
    original_source_init = CSVSource.__init__
    original_sink_init = CSVSink.__init__

    def source_init(self: CSVSource, config: dict[str, Any]) -> None:
        observed.append(("source", plugin_preflight_mode_enabled()))
        original_source_init(self, config)

    def sink_init(self: CSVSink, config: dict[str, Any]) -> None:
        observed.append(("sink", plugin_preflight_mode_enabled()))
        original_sink_init(self, config)

    monkeypatch.setattr(CSVSource, "__init__", source_init)
    monkeypatch.setattr(CSVSink, "__init__", sink_init)

    result = await run_sync_in_worker(
        partial(validate_pipeline_for_trained_operator, session_id="test-session"),
        _csv_worker_probe_state(tmp_path),
        _web_settings(tmp_path),
        yaml_generator,
    )

    assert result.is_valid is True
    # EVERY constructor call, not a fixed sequence. Validation constructs
    # plugins more than once — the semantic-contract validator probes each
    # sink to read its input_semantic_requirements() before the runtime
    # instantiation runs at all — and the contract this test names is that no
    # constructor EVER runs outside preflight mode during validation. Pinning
    # the exact list made an added probe read as a regression while a probe
    # that genuinely escaped the guard (CSVSink resolves an output collision
    # path, touching the filesystem, unless preflight is set) would have read
    # the same way.
    assert observed, "constructors must actually run, or this test is vacuous"
    assert all(preflight for _kind, preflight in observed), f"every constructor must observe preflight mode; saw {observed}"
    assert {kind for kind, _preflight in observed} == {"source", "sink"}


def test_validate_pipeline_rejects_chroma_persist_directory_outside_data_dir(tmp_path: Path) -> None:
    result = validate_pipeline_for_trained_operator(
        _chroma_persist_outside_data_dir_state(tmp_path),
        _web_settings(tmp_path),
        yaml_generator,
        session_id="test-session",
    )

    assert result.is_valid is False
    path_check = next(check for check in result.checks if check.name == "path_allowlist")
    assert path_check.passed is False
    assert "persist_directory" in path_check.detail


def test_runtime_mode_default_does_not_enable_preflight_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The normal execution path must remain real runtime mode by default.

    I5 strengthening: the previous version of this test only asserted
    ``bundle.sources``, which would pass even if the
    ContextVar were permanently stuck at True. The monkeypatched
    constructor probe directly observes ``plugin_preflight_mode_enabled()``
    at instantiation time and asserts the False default — that is the
    actual property the test name claims to pin.
    """
    observed: list[bool] = []
    original_source_init = CSVSource.__init__

    def source_init(self: CSVSource, config: dict[str, Any]) -> None:
        observed.append(plugin_preflight_mode_enabled())
        original_source_init(self, config)

    monkeypatch.setattr(CSVSource, "__init__", source_init)

    pipeline_yaml = _minimal_csv_pipeline_yaml(tmp_path)
    settings = load_settings_from_yaml_string(pipeline_yaml)

    bundle = instantiate_plugins_from_config(settings)

    assert bundle.sources
    assert observed == [False], (
        "Default runtime mode (no plugin_preflight_mode wrapper) MUST "
        "instantiate plugins with plugin_preflight_mode_enabled() == False. "
        f"Observed: {observed}"
    )


def test_runtime_factory_includes_sources_in_the_value_source_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from elspeth.engine.orchestrator import preflight as orchestrator_preflight

    settings = load_settings_from_yaml_string(_minimal_csv_pipeline_yaml(tmp_path))
    original = orchestrator_preflight.validate_value_source_compliance
    observed_sources: list[dict[str, object]] = []

    def record_value_source_inputs(
        transforms: Sequence[Any],
        *,
        sources: Mapping[str, object] | None = None,
    ) -> None:
        assert sources is not None
        observed_sources.append(dict(sources))
        original(transforms, sources=sources)

    monkeypatch.setattr(orchestrator_preflight, "validate_value_source_compliance", record_value_source_inputs)

    bundle = instantiate_plugins_from_config(settings, preflight_mode=True)

    assert observed_sources == [dict(bundle.sources)]


def test_web_runtime_rejects_disabled_plugin_before_any_constructor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = load_settings_from_yaml_string(_minimal_csv_pipeline_yaml(tmp_path))
    constructed: list[str] = []

    monkeypatch.setattr(CSVSource, "__init__", lambda *_args, **_kwargs: constructed.append("source"))
    monkeypatch.setattr(CSVSink, "__init__", lambda *_args, **_kwargs: constructed.append("sink"))

    with pytest.raises(ValueError, match="frozen plugin snapshot"):
        instantiate_runtime_plugins(
            settings,
            plugin_snapshot=_snapshot_without(PluginId("sink", "csv")),
        )

    assert constructed == []


def test_delayed_sink_factory_uses_frozen_snapshot_before_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = load_settings_from_yaml_string(_minimal_csv_pipeline_yaml(tmp_path))
    constructed: list[str] = []
    monkeypatch.setattr(CSVSink, "__init__", lambda *_args, **_kwargs: constructed.append("sink"))

    factory = make_policy_bound_sink_factory(
        settings,
        plugin_snapshot=_snapshot_without(PluginId("sink", "csv")),
    )

    with pytest.raises(ValueError, match="frozen plugin snapshot"):
        factory("output")

    assert constructed == []


def test_profile_private_bindings_never_enter_run_or_node_audit_config(tmp_path: Path) -> None:
    settings, audit_safe, snapshot = _profiled_llm_audit_inputs(tmp_path)

    runtime = build_validated_runtime_graph(
        settings,
        plugin_snapshot=snapshot,
        audit_safe_settings=audit_safe,
    )
    run_config = audit_safe_resolved_config(
        settings,
        audit_safe_settings=audit_safe,
        plugin_snapshot=snapshot,
    )
    node_configs = [runtime.graph.get_node_info(node_id).config for node_id in runtime.graph.topological_order()]
    rendered = json.dumps({"run": run_config, "nodes": node_configs}, default=dict)

    assert "tutorial" in rendered
    assert "openai/gpt-4o" not in rendered
    assert "probe-key" not in rendered


@pytest.mark.parametrize(
    ("section", "corrupt_value"),
    [
        ("sources", None),
        ("transforms", {}),
        ("aggregations", {}),
        ("sinks", []),
    ],
)
def test_audit_safe_resolved_config_rejects_corrupt_resolved_section(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    section: str,
    corrupt_value: object,
) -> None:
    settings, audit_safe, snapshot = _profiled_llm_audit_inputs(tmp_path)
    resolved = resolve_config(settings)
    resolved[section] = corrupt_value
    monkeypatch.setattr(execution_preflight, "resolve_config", lambda _settings: resolved)

    with pytest.raises(TypeError, match=section):
        audit_safe_resolved_config(
            settings,
            audit_safe_settings=audit_safe,
            plugin_snapshot=snapshot,
        )


@pytest.mark.parametrize(
    ("section", "component_name"),
    [
        ("sources", "primary"),
        ("transforms", None),
        ("aggregations", None),
        ("sinks", "output"),
    ],
)
def test_audit_safe_resolved_config_rejects_corrupt_resolved_component(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    section: str,
    component_name: str | None,
) -> None:
    settings, audit_safe, snapshot = _profiled_llm_audit_inputs(tmp_path)
    resolved = resolve_config(settings)
    components = resolved[section]
    if component_name is None:
        assert type(components) is list
        if components:
            components[0] = None
        else:
            components.append(None)
    else:
        assert type(components) is dict
        components[component_name] = None
    monkeypatch.setattr(execution_preflight, "resolve_config", lambda _settings: resolved)

    with pytest.raises(TypeError, match=section):
        audit_safe_resolved_config(
            settings,
            audit_safe_settings=audit_safe,
            plugin_snapshot=snapshot,
        )


@pytest.mark.parametrize("missing_field", ["plugin", "name"])
def test_audit_safe_resolved_config_requires_resolved_transform_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing_field: str,
) -> None:
    settings, audit_safe, snapshot = _profiled_llm_audit_inputs(tmp_path)
    resolved = resolve_config(settings)
    del resolved["transforms"][0][missing_field]
    monkeypatch.setattr(execution_preflight, "resolve_config", lambda _settings: resolved)

    with pytest.raises(KeyError, match=missing_field):
        audit_safe_resolved_config(
            settings,
            audit_safe_settings=audit_safe,
            plugin_snapshot=snapshot,
        )


def test_audit_safe_resolved_config_requires_authored_profile_options(tmp_path: Path) -> None:
    settings, audit_safe, snapshot = _profiled_llm_audit_inputs(tmp_path)
    del audit_safe["transforms"][0]["options"]

    with pytest.raises(KeyError, match="options"):
        audit_safe_resolved_config(
            settings,
            audit_safe_settings=audit_safe,
            plugin_snapshot=snapshot,
        )


def test_audit_safe_projections_preserve_historical_singular_source_contract(tmp_path: Path) -> None:
    settings, audit_safe, _snapshot = _profiled_llm_audit_inputs(tmp_path, source_name="source")
    authored_sources = audit_safe["sources"]
    assert type(authored_sources) is dict
    audit_safe["source"] = authored_sources["source"]
    del audit_safe["sources"]
    snapshot = _snapshot_with_profiles(
        (PluginId("source", "csv"), "source-profile"),
        (PluginId("transform", "llm"), "tutorial"),
    )

    runtime = build_validated_runtime_graph(
        settings,
        plugin_snapshot=snapshot,
        audit_safe_settings=audit_safe,
    )
    run_config = audit_safe_resolved_config(
        settings,
        audit_safe_settings=audit_safe,
        plugin_snapshot=snapshot,
    )

    rendered = json.dumps(
        {
            "run": run_config,
            "nodes": [runtime.graph.get_node_info(node_id).config for node_id in runtime.graph.topological_order()],
        },
        default=dict,
    )
    assert "tutorial" in rendered
    assert "openai/gpt-4o" not in rendered
    assert "probe-key" not in rendered


def test_audit_safe_projections_do_not_require_authored_matches_for_unprofiled_components(
    tmp_path: Path,
) -> None:
    settings, audit_safe, snapshot = _profiled_llm_audit_inputs(tmp_path)
    audit_safe["sources"] = {}
    audit_safe["sinks"] = {}

    runtime = build_validated_runtime_graph(
        settings,
        plugin_snapshot=snapshot,
        audit_safe_settings=audit_safe,
    )
    run_config = audit_safe_resolved_config(
        settings,
        audit_safe_settings=audit_safe,
        plugin_snapshot=snapshot,
    )

    rendered = json.dumps(
        {
            "run": run_config,
            "nodes": [runtime.graph.get_node_info(node_id).config for node_id in runtime.graph.topological_order()],
        },
        default=dict,
    )
    assert "tutorial" in rendered
    assert "openai/gpt-4o" not in rendered
    assert "probe-key" not in rendered


def test_audit_safe_projections_explicitly_synthesise_absent_optional_component_lists(
    tmp_path: Path,
) -> None:
    pipeline_yaml = _minimal_csv_pipeline_yaml(tmp_path)
    settings = load_settings_from_yaml_string(pipeline_yaml)
    audit_safe = load_bounded_pipeline_yaml(pipeline_yaml)
    assert type(audit_safe) is dict
    audit_safe["transforms"] = None
    audit_safe["aggregations"] = None
    snapshot = _snapshot_with_profiles((PluginId("source", "csv"), "source-profile"))

    build_validated_runtime_graph(
        settings,
        plugin_snapshot=snapshot,
        audit_safe_settings=audit_safe,
    )
    audit_safe_resolved_config(
        settings,
        audit_safe_settings=audit_safe,
        plugin_snapshot=snapshot,
    )


def test_audit_safe_plugin_configs_restores_earlier_substitutions_when_later_authored_config_is_corrupt(
    tmp_path: Path,
) -> None:
    settings, audit_safe, _snapshot = _profiled_llm_audit_inputs(tmp_path)
    snapshot = _snapshot_with_profiles(
        (PluginId("source", "csv"), "source-profile"),
        (PluginId("transform", "llm"), "tutorial"),
    )
    bundle = instantiate_runtime_plugins(settings, plugin_snapshot=snapshot)
    executable_source_config = bundle.sources["primary"].config
    del audit_safe["transforms"][0]["options"]

    with (
        pytest.raises(KeyError),
        execution_preflight._audit_safe_plugin_configs(
            bundle,
            audit_safe_settings=audit_safe,
            plugin_snapshot=snapshot,
        ),
    ):
        pass

    assert bundle.sources["primary"].config is executable_source_config


@pytest.mark.asyncio
async def test_preflight_context_isolated_between_concurrent_tasks() -> None:
    """I5 lock-in: ContextVar mutations in one asyncio task MUST NOT leak
    into a sibling task spawned via ``asyncio.create_task``.

    Python's ContextVar semantics give each task a copy of the parent
    context at task creation, so ``plugin_preflight_mode(True)`` inside
    Task A cannot be observed by Task B even when both run concurrently.
    Without this guarantee, two requests in flight at the same moment
    could see each other's preflight state — a tenant-isolation hazard
    in a multi-request server. The asyncio.Event barriers are the only
    way to assert ordering: without them, sequential ``await`` could
    mask an isolation bug by serialising the observations.
    """
    import asyncio

    a_set_mode = asyncio.Event()
    b_observed_state = asyncio.Event()
    observed_in_b: list[bool] = []
    observed_in_a_after_b: list[bool] = []

    async def task_a() -> None:
        with plugin_preflight_mode(True):
            assert plugin_preflight_mode_enabled() is True
            a_set_mode.set()
            await b_observed_state.wait()
            # Re-observe inside Task A — the state is still True here
            # because the context manager has not exited.
            observed_in_a_after_b.append(plugin_preflight_mode_enabled())

    async def task_b() -> None:
        await a_set_mode.wait()
        observed_in_b.append(plugin_preflight_mode_enabled())
        b_observed_state.set()

    await asyncio.gather(asyncio.create_task(task_a()), asyncio.create_task(task_b()))

    assert observed_in_b == [False], (
        "Sibling asyncio task observed Task A's preflight_mode=True — "
        "ContextVar isolation between create_task() siblings is broken. "
        f"Observed in Task B: {observed_in_b}"
    )
    assert observed_in_a_after_b == [True], (
        "Task A lost its own preflight_mode=True after Task B observed — "
        "context isolation is leaking the wrong direction. "
        f"Observed in Task A after B: {observed_in_a_after_b}"
    )
    # And the outer context is back to default — neither task's mutation
    # leaked out.
    assert plugin_preflight_mode_enabled() is False


def test_preflight_context_resets_when_body_raises() -> None:
    """I5 lock-in: ``plugin_preflight_mode`` uses try/finally + ContextVar.reset(token),
    so an exception inside the ``with`` block MUST still reset the
    ContextVar to its prior value.

    Without this guarantee, a plugin constructor that raises during
    runtime preflight would leave the ContextVar permanently True for
    the rest of the asyncio task, contaminating downstream plugin
    instantiations that should run in real-runtime mode.
    """

    class _ConstructorBoom(Exception):
        """Synthetic plugin-constructor failure to drive the test."""

    assert plugin_preflight_mode_enabled() is False  # baseline

    with pytest.raises(_ConstructorBoom), plugin_preflight_mode(True):
        assert plugin_preflight_mode_enabled() is True
        raise _ConstructorBoom("simulated plugin constructor failure")

    assert plugin_preflight_mode_enabled() is False, (
        "After an exception escaped plugin_preflight_mode(True), the "
        "ContextVar did not reset — try/finally with ContextVar.reset(token) "
        "should have restored the prior value regardless of the exception."
    )
