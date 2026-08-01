"""Production-path integration proof for asynchronous Amazon Textract analysis."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.freeze import deep_thaw
from elspeth.core.config import load_settings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.row_data import CallDataState
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config

_RAW_NEXT_TOKEN = "provider-private-next-token"
_EXPECTED_OUTPUT_FIELDS = {
    "textract_text",
    "textract_metadata",
    "textract_native",
    "textract_tables",
    "textract_forms",
}


def _response_metadata(request_id: str) -> dict[str, object]:
    return {
        "RequestId": request_id,
        "RetryAttempts": 0,
        "HTTPStatusCode": 200,
        "HTTPHeaders": {"provider-private-header": "must-not-be-audited"},
    }


def _page_one_blocks() -> list[dict[str, object]]:
    return [
        {"BlockType": "PAGE", "Id": "page-1", "Page": 1},
        {"BlockType": "LINE", "Id": "line-1", "Page": 1, "Text": "Invoice", "Confidence": 99.0},
        {
            "BlockType": "TABLE",
            "Id": "table-1",
            "Page": 1,
            "Confidence": 98.0,
            "Relationships": [{"Type": "CHILD", "Ids": ["cell-1"]}],
        },
        {
            "BlockType": "CELL",
            "Id": "cell-1",
            "Page": 1,
            "RowIndex": 1,
            "ColumnIndex": 1,
            "RowSpan": 1,
            "ColumnSpan": 1,
            "Confidence": 98.0,
            "Relationships": [{"Type": "CHILD", "Ids": ["cell-word"]}],
        },
        {"BlockType": "WORD", "Id": "cell-word", "Page": 1, "Text": "Amount", "Confidence": 98.0},
        {
            "BlockType": "KEY_VALUE_SET",
            "Id": "key-1",
            "Page": 1,
            "EntityTypes": ["KEY"],
            "Confidence": 97.0,
            "Relationships": [
                {"Type": "CHILD", "Ids": ["key-word"]},
                {"Type": "VALUE", "Ids": ["value-1"]},
            ],
        },
        {"BlockType": "WORD", "Id": "key-word", "Page": 1, "Text": "Reference", "Confidence": 97.0},
        {
            "BlockType": "KEY_VALUE_SET",
            "Id": "value-1",
            "Page": 1,
            "EntityTypes": ["VALUE"],
            "Confidence": 96.0,
            "Relationships": [{"Type": "CHILD", "Ids": ["value-word"]}],
        },
        {"BlockType": "WORD", "Id": "value-word", "Page": 1, "Text": "INV-42", "Confidence": 96.0},
    ]


class _FakeTextractSDK:
    def __init__(self) -> None:
        self.start_requests: list[dict[str, Any]] = []
        self.get_requests: list[dict[str, Any]] = []
        self.closed = False
        self._responses: list[Mapping[str, object]] = [
            {
                "JobStatus": "IN_PROGRESS",
                "ResponseMetadata": _response_metadata("poll-1"),
            },
            {
                "JobStatus": "SUCCEEDED",
                "DocumentMetadata": {"Pages": 2},
                "AnalyzeDocumentModelVersion": "integration-model",
                "Blocks": _page_one_blocks(),
                "NextToken": _RAW_NEXT_TOKEN,
                "ResponseMetadata": _response_metadata("poll-2"),
            },
            {
                "JobStatus": "SUCCEEDED",
                "DocumentMetadata": {"Pages": 2},
                "AnalyzeDocumentModelVersion": "integration-model",
                "Blocks": [
                    {"BlockType": "PAGE", "Id": "page-2", "Page": 2},
                    {"BlockType": "LINE", "Id": "line-2", "Page": 2, "Text": "Paid", "Confidence": 95.0},
                ],
                "ResponseMetadata": _response_metadata("poll-3"),
            },
        ]

    def start_document_analysis(self, **kwargs: Any) -> Mapping[str, object]:
        self.start_requests.append(kwargs)
        return {"JobId": "job-integration", "ResponseMetadata": _response_metadata("start-1")}

    def get_document_analysis(self, **kwargs: Any) -> Mapping[str, object]:
        self.get_requests.append(kwargs)
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _settings_document(tmp_path: Path) -> tuple[Path, Path]:
    input_path = tmp_path / "documents.csv"
    output_path = tmp_path / "results.jsonl"
    input_path.write_text("document_bucket,document_key\ndocuments,invoices/invoice.pdf\n", encoding="utf-8")
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        yaml.safe_dump(
            {
                "sources": {
                    "primary": {
                        "plugin": "csv",
                        "on_success": "textract_input",
                        "options": {
                            "path": str(input_path),
                            "on_validation_failure": "discard",
                            "schema": {"mode": "observed"},
                        },
                    }
                },
                "queues": {"textract_input": {}},
                "transforms": [
                    {
                        "name": "analyze_document",
                        "plugin": "aws_textract_document_analysis",
                        "input": "textract_input",
                        "on_success": "output",
                        "on_error": "discard",
                        "options": {
                            "region": "ap-southeast-2",
                            "auth_mode": "default_chain",
                            "bucket_field": "document_bucket",
                            "key_field": "document_key",
                            "feature_types": ["TABLES", "FORMS"],
                            "text_field": "textract_text",
                            "metadata_field": "textract_metadata",
                            "result_field": "textract_native",
                            "extract": {
                                "tables": "textract_tables",
                                "forms": "textract_forms",
                            },
                            "poll_interval_seconds": 0.001,
                            "poll_max_interval_seconds": 0.001,
                            "poll_timeout_seconds": 5.0,
                            "batch_wait_timeout_seconds": 5.0,
                            "schema": {"mode": "observed"},
                        },
                    }
                ],
                "sinks": {
                    "output": {
                        "plugin": "json",
                        "on_write_failure": "discard",
                        "options": {
                            "path": str(output_path),
                            "format": "jsonl",
                            "schema": {"mode": "observed"},
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return settings_path, output_path


@pytest.mark.timeout(60)
def test_textract_pipeline_uses_real_runtime_and_durable_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path, output_path = _settings_document(tmp_path)
    sdk = _FakeTextractSDK()
    builder_arguments: dict[str, object] = {}

    def build_sdk(**kwargs: object) -> _FakeTextractSDK:
        builder_arguments.update(kwargs)
        return sdk

    monkeypatch.setattr(
        "elspeth.plugins.transforms.aws.textract_document_analysis.build_textract_sdk_client",
        build_sdk,
    )
    settings = load_settings(settings_path)
    bundle = instantiate_plugins_from_config(settings)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce),
        queues=settings.queues,
    )
    transform = bundle.transforms[0].plugin
    assert transform.declared_output_fields == _EXPECTED_OUTPUT_FIELDS
    assert transform._output_schema_config is not None
    assert transform._output_schema_config.get_effective_guaranteed_fields() >= _EXPECTED_OUTPUT_FIELDS

    pipeline = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
        sink_effect_modes=bundle.sink_effect_modes,
    )
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    store = FilesystemPayloadStore(tmp_path / "payloads")
    result = Orchestrator(db).run(pipeline, graph=graph, settings=settings, payload_store=store)

    assert result.rows_processed == 1
    assert result.rows_succeeded == 1
    assert builder_arguments == {
        "region": "ap-southeast-2",
        "aws_access_key_id": None,
        "aws_secret_access_key": None,
        "aws_session_token": None,
    }
    assert sdk.closed is True
    assert len(sdk.start_requests) == 1
    assert sdk.get_requests == [
        {"JobId": "job-integration", "MaxResults": 1000},
        {"JobId": "job-integration", "MaxResults": 1000},
        {"JobId": "job-integration", "MaxResults": 1000, "NextToken": _RAW_NEXT_TOKEN},
    ]

    output = json.loads(output_path.read_text(encoding="utf-8").strip())
    assert output["document_bucket"] == "documents"
    assert output["document_key"] == "invoices/invoice.pdf"
    assert output["textract_text"] == "Invoice\n\f\nPaid"
    assert output["textract_tables"][0]["rows"][0][0]["text"] == "Amount"
    assert output["textract_forms"][0]["key"] == "Reference"
    assert output["textract_forms"][0]["value"] == "INV-42"
    assert output["textract_metadata"] == {
        "job_id": "job-integration",
        "job_status": "SUCCEEDED",
        "page_count": 2,
        "block_count": 11,
        "model_version": "integration-model",
        "warnings": [],
        "feature_types": ["FORMS", "TABLES"],
        "s3_version": None,
    }
    assert output["textract_native"]["DocumentMetadata"] == {"Pages": 2}
    assert len(output["textract_native"]["Blocks"]) == 11

    read = RecorderFactory.read_only(db, payload_store=store)
    row = read.query.get_rows(result.run_id)[0]
    token = read.query.get_tokens(row.row_id)[0]
    transform_node_id = graph.get_transform_id_map()[0]
    state = next(state for state in read.query.get_node_states_for_token(token.token_id) if state.node_id == transform_node_id)
    calls = read.query.get_calls(state.state_id)
    assert [call.call_index for call in calls] == [0, 1, 2, 3]
    assert all(call.call_type is CallType.HTTP and call.status is CallStatus.SUCCESS for call in calls)

    retained_payloads: list[object] = []
    for call in calls:
        assert call.request_ref is not None
        retained_payloads.append(json.loads(store.retrieve(call.request_ref).decode("utf-8")))
        response = read.execution.get_call_response_data(call.call_id)
        assert response.state is CallDataState.AVAILABLE
        assert response.data is not None
        retained_payloads.append(deep_thaw(response.data))
    retained = json.dumps(retained_payloads, sort_keys=True)
    assert _RAW_NEXT_TOKEN not in retained
    assert "provider-private-header" not in retained
    assert "aws_access_key_id" not in retained
    assert "aws_secret_access_key" not in retained
    assert "aws_session_token" not in retained

    _input_contract, output_contract = read.data_flow.get_node_contracts(result.run_id, transform_node_id)
    assert output_contract is not None
    assert output_contract.locked is True
    assert {field.normalized_name for field in output_contract.fields} >= _EXPECTED_OUTPUT_FIELDS
