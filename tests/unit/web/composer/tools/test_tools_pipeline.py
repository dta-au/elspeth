"""Composer bulk pipeline authoring and schema contracts."""

from __future__ import annotations

import csv
import io
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.catalog.schemas import PluginSchemaInfo, PluginSummary
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolResult
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_TEMPLATE_PARTS_KEY,
    approved_prompt_artifact_hash_from_options,
)
from elspeth.web.sessions.models import blobs_table
from tests.unit.web.composer.test_tools import (
    _STUB_SHA256,
    _STUB_SHA256_ALT,
    _assert_credential_material_refusal,
    _blob_operation,
    _default_source,
    _empty_state,
    _llm_options_with_api_key,
    _llm_options_with_forged_resolved_reviews,
    _llm_options_with_user_supplied_runtime_hash,
    _mock_catalog,
    _session_engine_with_session,
    _valid_pipeline_args,
    _verbatim_blob_context,
    execute_tool,
)


def _assert_inline_source_refusal(result: ToolResult, state: CompositionState, code: str, explanation_fragment: str) -> None:
    assert result.updated_state is state
    assert result.affected_nodes == ()
    assert [(entry.error_code, entry.rejected_component) for entry in result.validation.errors] == [(code, "source")]
    guidance = result.to_dict()["validation_guidance"]["codes"][code]
    assert explanation_fragment in guidance["explanation"]
    assert guidance["suggested_fix"]


class TestSetPipelineSchemaShape:
    """Lock the ``set_pipeline`` schema shape so the elspeth-4e79436719 Bug A
    regression cannot return via a future schema edit.

    The walker treats nested ``required`` as conditional-on-presence; for that
    contract to hold, ``inline_blob`` must remain *outside* the outer
    ``source.required`` list. If a future commit adds ``inline_blob`` to the
    outer ``required`` list, every regular ``set_pipeline`` call (which
    correctly does not ship inline literal data) starts failing pre-dispatch.
    """

    def test_inline_blob_is_optional_at_source_level(self) -> None:
        from elspeth.web.composer.tools import get_tool_definitions

        defns = {d["name"]: d for d in get_tool_definitions()}
        source = defns["set_pipeline"]["parameters"]["properties"]["source"]
        assert "inline_blob" in source["properties"], "inline_blob property must exist on source"
        assert "inline_blob" not in source["required"], (
            "inline_blob must remain optional at source level — adding it to "
            "source.required reintroduces elspeth-4e79436719 Bug A by forcing "
            "every regular set_pipeline call to ship literal inline content."
        )

    def test_inline_blob_inner_required_is_well_formed(self) -> None:
        from elspeth.web.composer.tools import get_tool_definitions

        defns = {d["name"]: d for d in get_tool_definitions()}
        inline = defns["set_pipeline"]["parameters"]["properties"]["source"]["properties"]["inline_blob"]
        # The conditional-on-presence semantics rely on this list being non-empty
        # — if inline_blob is supplied, these are the fields the walker enforces.
        assert set(inline["required"]) == {"filename", "mime_type", "content"}

    def test_existing_blob_binding_is_optional_at_source_level(self) -> None:
        """Atomic set_pipeline must expose an existing-blob binding path."""
        from elspeth.web.composer.tools import get_tool_definitions

        defns = {d["name"]: d for d in get_tool_definitions()}
        source = defns["set_pipeline"]["parameters"]["properties"]["source"]
        assert "blob_id" in source["properties"], "source.blob_id must bind an already uploaded blob"
        assert "blob_id" not in source["required"]

    def test_options_are_optional_at_schema_boundary(self) -> None:
        """Missing options must reach the handler so it can emit repair guidance."""
        from elspeth.web.composer.tools import get_tool_definitions

        defns = {d["name"]: d for d in get_tool_definitions()}
        params = defns["set_pipeline"]["parameters"]
        source = params["properties"]["source"]
        output_item = params["properties"]["outputs"]["items"]

        assert "options" not in source["required"]
        assert "options" not in output_item["required"]

    def test_output_schema_examples_show_valid_file_sink_options(self) -> None:
        from elspeth.web.composer.tools import get_tool_definitions

        defns = {d["name"]: d for d in get_tool_definitions()}
        output_item = defns["set_pipeline"]["parameters"]["properties"]["outputs"]["items"]

        assert {
            "sink_name": "results",
            "plugin": "json",
            "options": {
                "path": "outputs/results.json",
                "schema": {"mode": "observed"},
                "mode": "write",
                "collision_policy": "auto_increment",
            },
            "on_write_failure": "discard",
        } in output_item["examples"]


class TestSetPipeline:
    def _catalog_with_json_sink(self) -> MagicMock:
        catalog = _mock_catalog()
        catalog.list_sinks.return_value = [
            *catalog.list_sinks.return_value,
            PluginSummary(
                name="json",
                description="JSON file sink",
                plugin_type="sink",
                config_fields=[],
            ),
        ]
        return catalog

    def _catalog_with_text_sink(self) -> MagicMock:
        catalog = _mock_catalog()
        catalog.list_sinks.return_value = [
            *catalog.list_sinks.return_value,
            PluginSummary(
                name="text",
                description="Line-oriented text file sink",
                plugin_type="sink",
                config_fields=[],
            ),
        ]
        return catalog

    def _gate_pipeline_args(self, *, on_error: str | None, include_on_error: bool) -> dict[str, Any]:
        args = _valid_pipeline_args()
        args["source"]["on_success"] = "rows"
        gate = {
            "id": "threshold",
            "node_type": "gate",
            "plugin": None,
            "input": "rows",
            "condition": "row['amount'] > 500",
            "routes": {"true": "main", "false": "main"},
        }
        if include_on_error:
            gate["on_error"] = on_error
        args["nodes"] = [gate]
        args["edges"] = [
            {
                "id": "e1",
                "from_node": "source",
                "to_node": "threshold",
                "edge_type": "on_success",
                "label": None,
            }
        ]
        if on_error == "gate_errors":
            args["outputs"].append(
                {
                    "sink_name": "gate_errors",
                    "plugin": "csv",
                    "options": {"path": "/data/gate-errors.csv", "schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            )
        return args

    def test_set_pipeline_rejects_a_plugin_on_a_gate_without_mutating_state(self) -> None:
        """The whole atomic replacement rolls back; the stray plugin never reaches the state."""
        state = _empty_state()
        args = self._gate_pipeline_args(on_error=None, include_on_error=False)
        args["nodes"][0]["plugin"] = "fork"

        result = execute_tool("set_pipeline", args, state, _mock_catalog())

        assert result.success is False
        assert result.data is None
        assert result.validation.errors[0].error_code == "structural_node_plugin_forbidden"
        assert state.nodes == ()
        assert result.updated_state.nodes == ()

    def test_set_pipeline_creates_valid_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("set_pipeline", _valid_pipeline_args(), state, catalog)
        assert result.success is True
        assert result.validation is not None
        assert result.validation.is_valid is True
        assert result.updated_state.version == 2  # incremented from 1

    def test_set_pipeline_gate_omitted_on_error_preserves_fail_fast_policy(self) -> None:
        result = execute_tool(
            "set_pipeline",
            self._gate_pipeline_args(on_error=None, include_on_error=False),
            _empty_state(),
            _mock_catalog(),
        )

        assert result.success is True
        assert result.updated_state.nodes[0].on_error is None

    @pytest.mark.parametrize("on_error", ["discard", "gate_errors"])
    def test_set_pipeline_gate_accepts_explicit_non_empty_on_error_policy(self, on_error: str) -> None:
        result = execute_tool(
            "set_pipeline",
            self._gate_pipeline_args(on_error=on_error, include_on_error=True),
            _empty_state(),
            _mock_catalog(),
        )

        assert result.success is True
        assert result.updated_state.nodes[0].on_error == on_error

    def test_set_pipeline_gate_rejects_empty_on_error_before_state_mutation(self) -> None:
        from elspeth.web.composer.protocol import ToolArgumentError

        state = _empty_state()
        with pytest.raises(ToolArgumentError) as exc_info:
            execute_tool(
                "set_pipeline",
                self._gate_pipeline_args(on_error="", include_on_error=True),
                state,
                _mock_catalog(),
            )

        assert isinstance(exc_info.value.__cause__, PydanticValidationError)
        assert state.nodes == ()

    def test_set_pipeline_accepts_named_sources_mapping(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        args.pop("source")
        args["sources"] = {
            "customers": {
                "plugin": "csv",
                "on_success": "customer_rows",
                "options": {"path": "/data/customers.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            "orders": {
                "plugin": "json",
                "on_success": "order_rows",
                "options": {"path": "/data/orders.json", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
        }
        args["nodes"] = []
        args["edges"] = []
        args["outputs"] = [
            {
                "sink_name": "customer_rows",
                "plugin": "csv",
                "options": {"path": "/data/customer_rows.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            {
                "sink_name": "order_rows",
                "plugin": "json",
                "options": {"path": "/data/order_rows.json", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
        ]

        result = execute_tool("set_pipeline", args, state, catalog)

        assert result.success is True
        assert tuple(result.updated_state.sources) == ("customers", "orders")
        assert result.updated_state.sources["customers"].plugin == "csv"
        assert "source:customers" in result.affected_nodes
        assert "source:orders" in result.affected_nodes

    def test_set_pipeline_source_requires_validation_failure_choice(self) -> None:
        """A full replacement requires an authored validation-failure choice."""
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        del args["source"]["on_validation_failure"]

        with pytest.raises(ToolArgumentError):
            execute_tool("set_pipeline", args, state, catalog)
        assert _default_source(state) is None

    def test_set_pipeline_missing_json_output_options_returns_exact_repair_hint(self) -> None:
        state = _empty_state()
        catalog = self._catalog_with_json_sink()
        args = _valid_pipeline_args()
        args["source"]["options"]["path"] = "/data/blobs/test-session/in.csv"
        del args["outputs"][0]["options"]
        args["outputs"][0]["plugin"] = "json"

        result = execute_tool("set_pipeline", args, state, catalog, data_dir="/data")

        assert result.success is False
        assert result.updated_state is state
        error = result.validation.errors[0].message
        assert error.startswith("Output 'main': Missing options")
        assert '"sink_name": "main"' in error
        assert '"plugin": "json"' in error
        assert '"path": "outputs/main.json"' in error
        assert '"schema": {"mode": "observed"}' in error
        assert '"collision_policy": "auto_increment"' in error
        assert '"on_write_failure": "discard"' in error

    def test_set_pipeline_missing_text_output_options_returns_runnable_repair_hint(self) -> None:
        state = _empty_state()
        catalog = self._catalog_with_text_sink()
        args = _valid_pipeline_args()
        args["source"]["options"]["path"] = "/data/blobs/test-session/in.csv"
        del args["outputs"][0]["options"]
        args["outputs"][0]["plugin"] = "text"

        result = execute_tool("set_pipeline", args, state, catalog, data_dir="/data")

        assert result.success is False
        error = result.validation.errors[0].message
        assert '"plugin": "text"' in error
        assert '"path": "outputs/main.txt"' in error
        assert '"field": "line_text"' in error
        assert '"mode": "write"' in error
        assert '"collision_policy": "auto_increment"' in error

    def test_every_file_sink_repair_suggestion_actually_validates(self) -> None:
        """A repair hint that cannot validate is the defect it exists to fix.

        Asserted against the two gates whose rejection PRODUCES this hint —
        the sink's own config model and the collision-policy validator — rather
        than against an expected key list, so a NEW single-field sink is covered
        the day it is registered instead of silently inheriting an invalid
        suggestion. That is exactly how ``document`` rotted: it shipped
        requiring ``field``, fell through to the generic branch that omitted it,
        and its suggestion could not validate. ``mode`` was missing for csv and
        json on the same branch.
        """
        import json as json_module

        from elspeth.contracts.sink import FILE_SINK_REPAIR_EXTENSIONS
        from elspeth.web.composer.tools._common import (
            _missing_output_options_repair_error,
            validate_composer_file_sink_collision_policy,
        )
        from elspeth.web.composer.tools.sessions import _prevalidate_sink

        assert "document" in FILE_SINK_REPAIR_EXTENSIONS, "the sink this test was written for must be in scope"

        for plugin_name in sorted(FILE_SINK_REPAIR_EXTENSIONS):
            message = _missing_output_options_repair_error(
                sink_name="main",
                plugin_name=plugin_name,
                on_write_failure="discard",
                validation_error=None,
            )
            suggested = json_module.loads(message[message.index("{") : message.rindex("}") + 1])
            options = suggested["options"]

            assert _prevalidate_sink(plugin_name, options) is None, (
                f"the suggested repair for sink '{plugin_name}' fails its own config model"
            )
            assert validate_composer_file_sink_collision_policy(plugin_name, options, require_explicit=True) is None, (
                f"the suggested repair for sink '{plugin_name}' fails the collision-policy gate"
            )

    def test_set_pipeline_missing_document_output_options_returns_runnable_repair_hint(self) -> None:
        """document requires `field` exactly as text does; the hint must carry it."""
        from elspeth.web.composer.tools._common import _missing_output_options_repair_error

        error = _missing_output_options_repair_error(
            sink_name="main",
            plugin_name="document",
            on_write_failure="discard",
            validation_error=None,
        )

        assert '"plugin": "document"' in error
        assert '"path": "outputs/main.txt"' in error
        assert '"field": "line_text"' in error
        assert '"mode": "write"' in error
        assert '"collision_policy": "auto_increment"' in error
        assert "Replace line_text with the actual selected string field." in error

    def test_set_pipeline_failure_leads_validation_with_rejection_reason(self) -> None:
        """Regression for composer session 58d7ede3 round 6.

        When ``set_pipeline`` rejects a mutation, ``validation.errors[0]``
        must carry the actionable rejection reason (component
        ``rejected_mutation``). In the live session, the LLM read the
        stale-state errors first and burned a full round retrying with only
        a cosmetic change. Since elspeth-e89e6bf47a those state-snapshot
        errors (``"No source configured."`` etc.) are withheld from the
        full-replacement rejection envelope entirely — they described the
        pre-mutation state the candidate was not editing and misrouted
        repair loops reading error codes on the raw-result surfaces.
        """
        state = _empty_state()
        catalog = self._catalog_with_json_sink()
        args = _valid_pipeline_args()
        args["source"]["options"]["path"] = "/data/blobs/test-session/in.csv"
        del args["outputs"][0]["options"]
        args["outputs"][0]["plugin"] = "json"

        result = execute_tool("set_pipeline", args, state, catalog, data_dir="/data")

        assert result.success is False
        first = result.validation.errors[0]
        assert first.component == "rejected_mutation"
        assert first.severity == "high"
        assert "missing options" in first.message.lower()
        assert result.data is None
        # The rejection entry is the WHOLE envelope: the unchanged state's
        # errors are withheld, not merely demoted.
        assert [e.component for e in result.validation.errors] == ["rejected_mutation"]

    def test_set_pipeline_accepts_two_json_sinks_with_explicit_file_options(self, tmp_path: Path) -> None:
        state = _empty_state()
        catalog = self._catalog_with_json_sink()
        args = _valid_pipeline_args()
        args["source"]["options"]["path"] = str(tmp_path / "blobs" / "test-session" / "input.csv")
        args["nodes"][0]["on_error"] = "failures"
        args["outputs"] = [
            {
                "sink_name": "main",
                "plugin": "json",
                "options": {
                    "path": str(tmp_path / "outputs" / "test-session" / "main.json"),
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
            {
                "sink_name": "failures",
                "plugin": "json",
                "options": {
                    "path": str(tmp_path / "outputs" / "test-session" / "failures.json"),
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
        ]

        result = execute_tool("set_pipeline", args, state, catalog, data_dir=str(tmp_path))

        assert result.success is True
        assert result.validation.is_valid is True
        assert [output.plugin for output in result.updated_state.outputs] == ["json", "json"]
        assert [output.name for output in result.updated_state.outputs] == ["main", "failures"]

    def test_set_pipeline_rejects_literal_credential_value_without_mutating_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        literal = "literal-api-key-for-test"
        args = _valid_pipeline_args()
        args["nodes"][0] = {
            "id": "code_themes",
            "node_type": "transform",
            "plugin": "llm",
            "input": "source_out",
            "on_success": "main",
            "on_error": "discard",
            "options": _llm_options_with_api_key(literal),
        }
        args["edges"][0]["to_node"] = "code_themes"

        with pytest.raises(CredentialMaterialRefused) as caught:
            execute_tool("set_pipeline", args, state, catalog)

        _assert_credential_material_refusal(caught.value, literal_value=literal)
        assert state.nodes == ()

    def test_set_pipeline_rejects_placeholder_database_table_without_mutating_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.list_sinks.return_value = [
            *catalog.list_sinks.return_value,
            PluginSummary(
                name="database",
                description="Database sink",
                plugin_type="sink",
                config_fields=[],
            ),
        ]
        args = _valid_pipeline_args()
        args["outputs"][0] = {
            "sink_name": "main",
            "plugin": "database",
            "options": {
                "url": {"secret_ref": "DATABASE_URL"},
                "table": "<OPERATOR_REQUIRED>",
                "schema": {"mode": "observed"},
            },
            "on_write_failure": "discard",
        }

        result = execute_tool("set_pipeline", args, state, catalog)

        assert result.success is False
        assert result.updated_state is state
        assert result.data is None
        error = result.validation.errors[0].message
        assert "Output 'main'" in error
        assert "table" in error
        assert "placeholder" in error

    def test_set_pipeline_accepts_wired_secret_ref_marker(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        args["nodes"][0] = {
            "id": "code_themes",
            "node_type": "transform",
            "plugin": "llm",
            "input": "source_out",
            "on_success": "main",
            "on_error": "discard",
            "options": _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"}),
        }
        args["edges"][0]["to_node"] = "code_themes"

        result = execute_tool("set_pipeline", args, state, catalog)

        assert result.success is True
        node = result.updated_state.nodes[0]
        assert node.options["api_key"] == {"secret_ref": "OPENROUTER_API_KEY"}

    def test_set_pipeline_accepts_textract_paired_secret_ref_markers(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="aws_textract_document_analysis",
                description="Asynchronous S3-backed document analysis",
                plugin_type="transform",
                config_fields=[],
            ),
        ]
        args = _valid_pipeline_args()
        args["nodes"][0] = {
            "id": "analyze_document",
            "node_type": "transform",
            "plugin": "aws_textract_document_analysis",
            "input": "source_out",
            "on_success": "main",
            "on_error": "discard",
            "options": {
                "region": "ap-southeast-2",
                "auth_mode": "secret_refs",
                "aws_access_key_id": {"secret_ref": "AWS_ACCESS_KEY_ID"},
                "aws_secret_access_key": {"secret_ref": "AWS_SECRET_ACCESS_KEY"},
                "bucket_field": "document_bucket",
                "key_field": "document_key",
                "feature_types": ["FORMS"],
                "text_field": "textract_text",
                "schema": {"mode": "observed"},
            },
        }
        args["edges"][0]["to_node"] = "analyze_document"

        result = execute_tool("set_pipeline", args, state, catalog)

        assert result.success is True
        node = result.updated_state.nodes[0]
        assert node.options["aws_access_key_id"] == {"secret_ref": "AWS_ACCESS_KEY_ID"}
        assert node.options["aws_secret_access_key"] == {"secret_ref": "AWS_SECRET_ACCESS_KEY"}

    def test_set_pipeline_rejects_user_supplied_llm_runtime_hash_without_mutating_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        args["nodes"][0] = {
            "id": "code_themes",
            "node_type": "transform",
            "plugin": "llm",
            "input": "source_out",
            "on_success": "main",
            "on_error": "discard",
            "options": _llm_options_with_user_supplied_runtime_hash({"secret_ref": "OPENROUTER_API_KEY"}),
        }
        args["edges"][0]["to_node"] = "code_themes"

        result = execute_tool("set_pipeline", args, state, catalog)

        assert result.success is False
        assert result.updated_state is state
        assert result.data is None
        assert "approved_prompt_artifact_hash" in result.validation.errors[0].message
        assert "runtime-owned" in result.validation.errors[0].message

    def test_set_pipeline_rejects_output_interpretation_requirements_without_mutating_state(self) -> None:
        state = _empty_state()
        args = _valid_pipeline_args()
        args["outputs"][0]["options"][INTERPRETATION_REQUIREMENTS_KEY] = []

        result = execute_tool("set_pipeline", args, state, _mock_catalog())

        assert result.success is False
        assert result.updated_state is state
        assert result.data is None
        assert INTERPRETATION_REQUIREMENTS_KEY in result.validation.errors[0].message

    def test_upsert_node_rejects_user_supplied_llm_runtime_hash_without_mutating_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()

        result = execute_tool(
            "upsert_node",
            {
                "id": "code_themes",
                "node_type": "transform",
                "plugin": "llm",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": _llm_options_with_user_supplied_runtime_hash({"secret_ref": "OPENROUTER_API_KEY"}),
            },
            state,
            catalog,
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.data is None
        assert "approved_prompt_artifact_hash" in result.validation.errors[0].message
        assert "runtime-owned" in result.validation.errors[0].message

    def test_patch_node_options_rejects_user_supplied_llm_runtime_hash_without_mutating_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        created = execute_tool(
            "upsert_node",
            {
                "id": "code_themes",
                "node_type": "transform",
                "plugin": "llm",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"}),
            },
            state,
            catalog,
        )
        assert created.success is True, created.data
        prompt_template = created.updated_state.nodes[0].options["prompt_template"]

        result = execute_tool(
            "patch_node_options",
            {
                "node_id": "code_themes",
                "patch": {"approved_prompt_artifact_hash": stable_hash(prompt_template)},
            },
            created.updated_state,
            catalog,
        )

        assert result.success is False
        assert result.updated_state is created.updated_state
        assert result.data is None
        assert "approved_prompt_artifact_hash" in result.validation.errors[0].message
        assert "runtime-owned" in result.validation.errors[0].message

    def test_set_pipeline_rejects_user_supplied_resolved_llm_reviews_without_mutating_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        args["nodes"][0] = {
            "id": "code_themes",
            "node_type": "transform",
            "plugin": "llm",
            "input": "source_out",
            "on_success": "main",
            "on_error": "discard",
            "options": _llm_options_with_forged_resolved_reviews({"secret_ref": "OPENROUTER_API_KEY"}),
        }
        args["edges"][0]["to_node"] = "code_themes"

        result = execute_tool("set_pipeline", args, state, catalog)

        assert result.success is False
        assert result.updated_state is state
        assert result.data is None
        assert INTERPRETATION_REQUIREMENTS_KEY in result.validation.errors[0].message
        assert "request_interpretation_review" in result.validation.errors[0].message
        assert "resolve_interpretation_event" not in result.validation.errors[0].message

    def test_upsert_node_rejects_user_supplied_resolved_llm_reviews_without_mutating_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()

        result = execute_tool(
            "upsert_node",
            {
                "id": "code_themes",
                "node_type": "transform",
                "plugin": "llm",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": _llm_options_with_forged_resolved_reviews({"secret_ref": "OPENROUTER_API_KEY"}),
            },
            state,
            catalog,
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.data is None
        assert INTERPRETATION_REQUIREMENTS_KEY in result.validation.errors[0].message
        assert "request_interpretation_review" in result.validation.errors[0].message
        assert "resolve_interpretation_event" not in result.validation.errors[0].message

    def test_patch_node_options_rejects_user_supplied_resolved_llm_reviews_without_mutating_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        created = execute_tool(
            "upsert_node",
            {
                "id": "code_themes",
                "node_type": "transform",
                "plugin": "llm",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"}),
            },
            state,
            catalog,
        )
        assert created.success is True, created.data
        forged = _llm_options_with_forged_resolved_reviews({"secret_ref": "OPENROUTER_API_KEY"})

        result = execute_tool(
            "patch_node_options",
            {
                "node_id": "code_themes",
                "patch": {INTERPRETATION_REQUIREMENTS_KEY: forged[INTERPRETATION_REQUIREMENTS_KEY]},
            },
            created.updated_state,
            catalog,
        )

        assert result.success is False
        assert result.updated_state is created.updated_state
        assert result.data is None
        assert INTERPRETATION_REQUIREMENTS_KEY in result.validation.errors[0].message
        assert "request_interpretation_review" in result.validation.errors[0].message
        assert "resolve_interpretation_event" not in result.validation.errors[0].message

    def test_patch_node_options_preserves_existing_resolved_llm_reviews_on_unrelated_patch(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        created = execute_tool(
            "upsert_node",
            {
                "id": "code_themes",
                "node_type": "transform",
                "plugin": "llm",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"}),
            },
            state,
            catalog,
        )
        assert created.success is True, created.data
        resolved_options = _llm_options_with_forged_resolved_reviews({"secret_ref": "OPENROUTER_API_KEY"})
        resolved_options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(resolved_options)
        resolved_node = replace(created.updated_state.nodes[0], options=resolved_options)
        resolved_state = created.updated_state.with_node(resolved_node)

        result = execute_tool(
            "patch_node_options",
            {"node_id": "code_themes", "patch": {"temperature": 0.1}},
            resolved_state,
            catalog,
        )

        assert result.success is True, result.data
        patched_options = result.updated_state.nodes[0].options
        assert patched_options["temperature"] == 0.1
        assert patched_options["approved_prompt_artifact_hash"] == approved_prompt_artifact_hash_from_options(patched_options)
        assert deep_thaw(patched_options[INTERPRETATION_REQUIREMENTS_KEY]) == resolved_options[INTERPRETATION_REQUIREMENTS_KEY]

    def test_set_pipeline_rejects_secret_ref_in_non_credential_field(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        args["nodes"][0] = {
            "id": "scrape_pages",
            "node_type": "transform",
            "plugin": "web_scrape",
            "input": "source_out",
            "on_success": "main",
            "on_error": "discard",
            "options": {
                "url_field": "url",
                "http": {
                    "abuse_contact": {"secret_ref": "ANY_SECRET"},
                    "scraping_reason": "research",
                    "allowed_hosts": ["example.com"],
                },
            },
        }
        args["edges"][0]["to_node"] = "scrape_pages"

        result = execute_tool("set_pipeline", args, state, catalog)

        assert result.success is False
        assert result.updated_state is state
        assert "web_scrape" in result.validation.errors[0].message
        assert "http.abuse_contact" in result.validation.errors[0].message
        assert "ANY_SECRET" in result.validation.errors[0].message
        assert "only credential-bearing fields" in result.validation.errors[0].message
        assert "api_key" in result.validation.errors[0].message

    def test_upsert_node_rejects_secret_ref_in_non_credential_field(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()

        result = execute_tool(
            "upsert_node",
            {
                "id": "scrape_pages",
                "node_type": "transform",
                "plugin": "web_scrape",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": {
                    "url_field": "url",
                    "http": {
                        "scraping_reason": {"secret_ref": "ANY_SECRET"},
                        "abuse_contact": "ops@example.com",
                        "allowed_hosts": ["example.com"],
                    },
                },
            },
            state,
            catalog,
        )

        assert result.success is False
        assert result.updated_state is state
        assert "web_scrape" in result.validation.errors[0].message
        assert "http.scraping_reason" in result.validation.errors[0].message
        assert "ANY_SECRET" in result.validation.errors[0].message
        assert "only credential-bearing fields" in result.validation.errors[0].message

    def test_upsert_node_rejects_literal_credential_value_without_mutating_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        literal = "literal-upsert-key-for-test"

        with pytest.raises(CredentialMaterialRefused) as caught:
            execute_tool(
                "upsert_node",
                {
                    "id": "code_themes",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "source_out",
                    "on_success": "main",
                    "on_error": "discard",
                    "options": _llm_options_with_api_key(literal),
                },
                state,
                catalog,
            )

        _assert_credential_material_refusal(caught.value, literal_value=literal)
        assert state.nodes == ()

    def test_upsert_node_llm_prevalidation_ignores_review_metadata(self) -> None:
        """LLM plugin validation strips web-only prompt review metadata but keeps it in state."""
        state = _empty_state()
        catalog = _mock_catalog()

        result = execute_tool(
            "upsert_node",
            {
                "id": "code_themes",
                "node_type": "transform",
                "plugin": "llm",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": {
                    "provider": "openrouter",
                    "model": "openai/gpt-4o",
                    "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                    "prompt_template": "Classify pending interpretation: {{ row.text }}",
                    "required_input_fields": ["text"],
                    "schema": {"mode": "observed"},
                    PROMPT_TEMPLATE_PARTS_KEY: [
                        {"kind": "text", "text": "Classify "},
                        {
                            "kind": "interpretation_ref",
                            "requirement_id": "llm_prompt_template:code_themes:code_themes",
                        },
                        {"kind": "text", "text": ": {{ row.text }}"},
                    ],
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "kind": "llm_prompt_template",
                            "user_term": "llm_prompt_template:code_themes",
                            "draft": "Classify pending interpretation: {{ row.text }}",
                        }
                    ],
                },
            },
            state,
            catalog,
        )

        assert result.success is True, result.data
        node = result.updated_state.nodes[0]
        assert PROMPT_TEMPLATE_PARTS_KEY in node.options
        assert INTERPRETATION_REQUIREMENTS_KEY in node.options

    def test_set_pipeline_rejects_manual_blob_ref_in_source_options(self) -> None:
        """Manual blob_ref must not bypass the blob-backed source binding tools."""
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        args["source"]["options"] = {
            "blob_ref": "manual-ref",
            "path": "/tmp/not_the_blob_storage.csv",
            "schema": {"mode": "observed"},
        }

        result = execute_tool("set_pipeline", args, state, catalog)

        assert result.success is False
        assert _default_source(result.updated_state) is None
        assert result.updated_state.version == 1
        assert "set_source_from_blob" in result.validation.errors[0].message
        assert "source.inline_blob" in result.validation.errors[0].message

    def test_set_pipeline_binds_existing_blob_instead_of_header_only_sibling(self, tmp_path: Path) -> None:
        """Complete-pipeline writes must preserve the user-selected uploaded blob."""
        from datetime import UTC, datetime

        state = _empty_state()
        catalog = _mock_catalog()
        engine, session_id = _session_engine_with_session()
        uploaded_id = str(uuid4())
        header_only_id = str(uuid4())
        storage_dir = tmp_path / "blobs" / session_id
        storage_dir.mkdir(parents=True)
        uploaded_path = storage_dir / f"{uploaded_id}_contact_form_submissions.csv"
        header_only_path = storage_dir / f"{header_only_id}_hubspot_export.csv"
        uploaded_content = (
            "submission_id,timestamp,name,email,company,message\n"
            "CF-501,2026-08-01T09:14Z,Sarah Patel,sarah@example.com,Northwind,Interested in pricing\n"
            "CF-502,2026-08-01T09:22Z,Bot Bot,spam@example.test,,CHEAP SEO SERVICES\n"
        )
        header_only_content = "submission_id,timestamp,name,email,company,message\n"
        uploaded_path.write_text(uploaded_content, encoding="utf-8")
        header_only_path.write_text(header_only_content, encoding="utf-8")
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert(),
                [
                    {
                        "id": uploaded_id,
                        "session_id": session_id,
                        "filename": "contact_form_submissions.csv",
                        "mime_type": "text/csv",
                        "size_bytes": len(uploaded_content.encode("utf-8")),
                        "content_hash": _STUB_SHA256,
                        "storage_path": str(uploaded_path),
                        "created_at": now,
                        "created_by": "user",
                        "source_description": "scenario uploaded contact form submissions",
                        "status": "ready",
                    },
                    {
                        "id": header_only_id,
                        "session_id": session_id,
                        "filename": "hubspot_export.csv",
                        "mime_type": "text/csv",
                        "size_bytes": len(header_only_content.encode("utf-8")),
                        "content_hash": _STUB_SHA256_ALT,
                        "storage_path": str(header_only_path),
                        "created_at": now,
                        "created_by": "assistant",
                        "source_description": "header-only inferred schema",
                        "status": "ready",
                    },
                ],
            )

        args = _valid_pipeline_args()
        args["source"] = {
            "plugin": "csv",
            "blob_id": uploaded_id,
            "on_success": "source_out",
            "options": {"schema": {"mode": "observed"}},
            "on_validation_failure": "quarantine",
        }
        args["outputs"][0]["options"]["path"] = str(tmp_path / "outputs" / session_id / "out.csv")
        args["outputs"][0]["options"]["mode"] = "write"
        args["outputs"][0]["options"]["collision_policy"] = "auto_increment"

        with _blob_operation(engine, session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "set_pipeline",
                args,
                state,
                catalog,
                data_dir=str(tmp_path),
                session_engine=engine,
                session_id=session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

        assert result.success is True
        assert _default_source(result.updated_state) is not None
        source_options = _default_source(result.updated_state).options
        assert source_options["blob_ref"] == uploaded_id
        assert source_options["path"] == str(uploaded_path)
        assert source_options["path"] != str(header_only_path)
        assert result.data["source_blob"]["blob_id"] == uploaded_id

    def test_set_pipeline_rejects_header_only_inline_csv_when_uploaded_csv_exists(self, tmp_path: Path) -> None:
        """Header-only inline CSV must not supersede an uploaded ready CSV."""
        from datetime import UTC, datetime

        state = _empty_state()
        catalog = _mock_catalog()
        engine, session_id = _session_engine_with_session()
        uploaded_id = str(uuid4())
        uploaded_content = "name,email\nAlice,alice@example.com\n"
        uploaded_path = tmp_path / "blobs" / session_id / f"{uploaded_id}_contacts.csv"
        uploaded_path.parent.mkdir(parents=True)
        uploaded_path.write_text(uploaded_content, encoding="utf-8")
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=uploaded_id,
                    session_id=session_id,
                    filename="contacts.csv",
                    mime_type="text/csv",
                    size_bytes=len(uploaded_content.encode("utf-8")),
                    content_hash=_STUB_SHA256,
                    storage_path=str(uploaded_path),
                    created_at=now,
                    created_by="user",
                    source_description="uploaded contact rows",
                    status="ready",
                )
            )

        args = _valid_pipeline_args()
        args["source"] = {
            "plugin": "csv",
            "on_success": "source_out",
            "options": {"schema": {"mode": "observed"}},
            "inline_blob": {
                "filename": "contacts.csv",
                "mime_type": "text/csv",
                "content": "name,email\n",
            },
            "on_validation_failure": "quarantine",
        }
        args["outputs"][0]["options"]["path"] = str(tmp_path / "outputs" / "out.csv")
        args["outputs"][0]["options"]["mode"] = "write"
        args["outputs"][0]["options"]["collision_policy"] = "auto_increment"

        blob_provenance = _verbatim_blob_context(engine, session_id, "name,email\n")
        with _blob_operation(engine, session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "set_pipeline",
                args,
                state,
                catalog,
                data_dir=str(tmp_path),
                session_engine=engine,
                session_id=session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )

        assert result.success is False
        assert _default_source(result.updated_state) is None
        assert "header-only inline CSV" in result.validation.errors[0].message
        assert uploaded_id in result.validation.errors[0].message

    def test_set_pipeline_header_only_inline_csv_check_does_not_read_full_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custody check should stream/prefix-read candidate headers, not use read_text()."""
        from datetime import UTC, datetime

        state = _empty_state()
        catalog = _mock_catalog()
        engine, session_id = _session_engine_with_session()
        uploaded_id = str(uuid4())
        uploaded_content = "name,email\n" + "\n".join(f"User {idx},user{idx}@example.com" for idx in range(2000))
        uploaded_path = tmp_path / "blobs" / session_id / f"{uploaded_id}_contacts.csv"
        uploaded_path.parent.mkdir(parents=True)
        uploaded_path.write_text(uploaded_content, encoding="utf-8")
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=uploaded_id,
                    session_id=session_id,
                    filename="contacts.csv",
                    mime_type="text/csv",
                    size_bytes=len(uploaded_content.encode("utf-8")),
                    content_hash=_STUB_SHA256,
                    storage_path=str(uploaded_path),
                    created_at=now,
                    created_by="user",
                    source_description="uploaded contact rows",
                    status="ready",
                )
            )

        original_read_text = Path.read_text

        def _forbid_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
            if path == uploaded_path:
                raise AssertionError("header-only custody check must not read candidate CSVs in full")
            return original_read_text(path, *args, **kwargs)

        args = _valid_pipeline_args()
        args["source"] = {
            "plugin": "csv",
            "on_success": "source_out",
            "options": {"schema": {"mode": "observed"}},
            "inline_blob": {
                "filename": "contacts.csv",
                "mime_type": "text/csv",
                "content": "name,email\n",
            },
            "on_validation_failure": "quarantine",
        }
        args["outputs"][0]["options"]["path"] = str(tmp_path / "outputs" / "out.csv")
        args["outputs"][0]["options"]["mode"] = "write"
        args["outputs"][0]["options"]["collision_policy"] = "auto_increment"

        blob_provenance = _verbatim_blob_context(engine, session_id, "name,email\n")
        with _blob_operation(engine, session_id) as (blob_authority, blob_operation_context):
            monkeypatch.setattr(Path, "read_text", _forbid_read_text)
            result = execute_tool(
                "set_pipeline",
                args,
                state,
                catalog,
                data_dir=str(tmp_path),
                session_engine=engine,
                session_id=session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )

        assert result.success is False
        assert "header-only inline CSV" in result.validation.errors[0].message
        assert uploaded_id in result.validation.errors[0].message

    def test_set_pipeline_header_only_inline_csv_decode_failure_escalates(self, tmp_path: Path) -> None:
        """Non-UTF-8 ready CSV candidates should raise audit integrity, not UnicodeDecodeError."""
        from datetime import UTC, datetime

        from elspeth.contracts.errors import AuditIntegrityError

        state = _empty_state()
        catalog = _mock_catalog()
        engine, session_id = _session_engine_with_session()
        uploaded_id = str(uuid4())
        uploaded_content = b"name,email\nAlice,\xff\n"
        uploaded_path = tmp_path / "blobs" / session_id / f"{uploaded_id}_contacts.csv"
        uploaded_path.parent.mkdir(parents=True)
        uploaded_path.write_bytes(uploaded_content)
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=uploaded_id,
                    session_id=session_id,
                    filename="contacts.csv",
                    mime_type="text/csv",
                    size_bytes=len(uploaded_content),
                    content_hash=_STUB_SHA256,
                    storage_path=str(uploaded_path),
                    created_at=now,
                    created_by="user",
                    source_description="uploaded contact rows",
                    status="ready",
                )
            )

        args = _valid_pipeline_args()
        args["source"] = {
            "plugin": "csv",
            "on_success": "source_out",
            "options": {"schema": {"mode": "observed"}},
            "inline_blob": {
                "filename": "contacts.csv",
                "mime_type": "text/csv",
                "content": "name,email\n",
            },
            "on_validation_failure": "quarantine",
        }
        args["outputs"][0]["options"]["path"] = str(tmp_path / "outputs" / "out.csv")
        args["outputs"][0]["options"]["mode"] = "write"
        args["outputs"][0]["options"]["collision_policy"] = "auto_increment"

        blob_provenance = _verbatim_blob_context(engine, session_id, "name,email\n")
        with (
            _blob_operation(engine, session_id) as (blob_authority, blob_operation_context),
            pytest.raises(AuditIntegrityError, match="could not be decoded"),
        ):
            execute_tool(
                "set_pipeline",
                args,
                state,
                catalog,
                data_dir=str(tmp_path),
                session_engine=engine,
                session_id=session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )

    def test_set_pipeline_oversized_inline_csv_field_returns_bounded_failure(self, tmp_path: Path) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        engine, session_id = _session_engine_with_session()
        oversized_content = f"{'x' * ((64 * 1024) + 1)}\n"
        args = _valid_pipeline_args()
        args["source"] = {
            "plugin": "csv",
            "on_success": "source_out",
            "options": {"schema": {"mode": "observed"}},
            "inline_blob": {
                "filename": "oversized.csv",
                "mime_type": "text/csv",
                "content": oversized_content,
            },
            "on_validation_failure": "quarantine",
        }
        args["outputs"][0]["options"]["path"] = str(tmp_path / "outputs" / session_id / "out.csv")
        args["outputs"][0]["options"]["mode"] = "write"
        args["outputs"][0]["options"]["collision_policy"] = "auto_increment"

        blob_provenance = _verbatim_blob_context(engine, session_id, oversized_content)
        with _blob_operation(engine, session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "set_pipeline",
                args,
                state,
                catalog,
                data_dir=str(tmp_path),
                session_engine=engine,
                session_id=session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )

        assert result.success is False
        assert (
            result.validation.errors[0].message == "Source 'source': Refusing inline CSV because it exceeds bounded CSV inspection limits."
        )
        assert "x" * 32 not in result.validation.errors[0].message
        _assert_inline_source_refusal(result, state, "inline_source_csv_inspection_failed", "No uploaded-file conflict was established")

    @pytest.mark.parametrize(
        "unsafe_content",
        [f"{'x' * ((64 * 1024) + 1)}\n", '"unterminated\n', "url\x00\n"],
        ids=["oversized_field", "malformed_quoting", "nul"],
    )
    def test_source_blob_review_classification_contains_unsafe_csv(self, unsafe_content: str) -> None:
        from elspeth.web.composer.tools.sources import _source_blob_review_user_term

        assert _source_blob_review_user_term(mime_type="text/csv", content=unsafe_content) == "inline_source_data"

    @pytest.mark.parametrize(
        "malformed_content",
        ['"unterminated\n', "name,em\x00ail\n"],
        ids=["malformed_quoting", "nul"],
    )
    def test_set_pipeline_malformed_inline_csv_returns_bounded_failure(
        self,
        tmp_path: Path,
        malformed_content: str,
    ) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        engine, session_id = _session_engine_with_session()
        args = _valid_pipeline_args()
        args["source"] = {
            "plugin": "csv",
            "on_success": "source_out",
            "options": {"schema": {"mode": "observed"}},
            "inline_blob": {
                "filename": "malformed.csv",
                "mime_type": "text/csv",
                "content": malformed_content,
            },
            "on_validation_failure": "quarantine",
        }
        args["outputs"][0]["options"].update(
            {
                "path": str(tmp_path / "outputs" / session_id / "out.csv"),
                "mode": "write",
                "collision_policy": "auto_increment",
            }
        )

        blob_provenance = _verbatim_blob_context(engine, session_id, malformed_content)
        with _blob_operation(engine, session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "set_pipeline",
                args,
                state,
                catalog,
                data_dir=str(tmp_path),
                session_engine=engine,
                session_id=session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )

        assert result.success is False
        assert (
            result.validation.errors[0].message == "Source 'source': Refusing inline CSV because it exceeds bounded CSV inspection limits."
        )
        assert malformed_content not in result.validation.errors[0].message
        _assert_inline_source_refusal(result, state, "inline_source_csv_inspection_failed", "No uploaded-file conflict was established")

    def test_set_pipeline_candidate_csv_parser_error_escalates_as_integrity_failure(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from elspeth.contracts.errors import AuditIntegrityError

        state = _empty_state()
        catalog = _mock_catalog()
        engine, session_id = _session_engine_with_session()
        uploaded_id = str(uuid4())
        uploaded_content = f"{'x' * 64}\nrow\n"
        uploaded_path = tmp_path / "blobs" / session_id / f"{uploaded_id}_oversized.csv"
        uploaded_path.parent.mkdir(parents=True)
        uploaded_path.write_text(uploaded_content, encoding="utf-8")
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=uploaded_id,
                    session_id=session_id,
                    filename="oversized.csv",
                    mime_type="text/csv",
                    size_bytes=len(uploaded_content.encode("utf-8")),
                    content_hash=_STUB_SHA256,
                    storage_path=str(uploaded_path),
                    created_at=datetime.now(UTC),
                    created_by="user",
                    source_description="uploaded rows",
                    status="ready",
                )
            )
        inline_content = "name,email\n"
        args = _valid_pipeline_args()
        args["source"] = {
            "plugin": "csv",
            "on_success": "source_out",
            "options": {"schema": {"mode": "observed"}},
            "inline_blob": {
                "filename": "contacts.csv",
                "mime_type": "text/csv",
                "content": inline_content,
            },
            "on_validation_failure": "quarantine",
        }
        args["outputs"][0]["options"]["path"] = str(tmp_path / "outputs" / "out.csv")
        args["outputs"][0]["options"]["mode"] = "write"
        args["outputs"][0]["options"]["collision_policy"] = "auto_increment"
        blob_provenance = _verbatim_blob_context(engine, session_id, inline_content)
        with _blob_operation(engine, session_id) as (blob_authority, blob_operation_context):
            previous_limit = csv.field_size_limit()
            csv.field_size_limit(32)
            try:
                with pytest.raises(AuditIntegrityError, match="bounded CSV inspection"):
                    execute_tool(
                        "set_pipeline",
                        args,
                        state,
                        catalog,
                        data_dir=str(tmp_path),
                        session_engine=engine,
                        session_id=session_id,
                        session_operation_authority=blob_authority,
                        session_operation_context=blob_operation_context,
                        **blob_provenance,
                    )
            finally:
                csv.field_size_limit(previous_limit)

    def test_first_nonempty_csv_row_from_path_returns_header_when_complete_in_window(self, tmp_path: Path) -> None:
        """A header row that terminates inside the window still parses normally."""
        from elspeth.web.composer.tools.sources import _first_nonempty_csv_row_from_path

        path = tmp_path / "small.csv"
        path.write_text("name,email\nAlice,alice@example.com\n", encoding="utf-8")

        assert _first_nonempty_csv_row_from_path(path) == ("name", "email")

    def test_first_nonempty_csv_row_from_path_tolerates_first_record_crossing_window(self, tmp_path: Path) -> None:
        """A well-formed CSV whose first record's quoted cell crosses the 64 KiB
        bounded-inspection window must not be reported as a parse failure: the
        file continues past the window (this is expected truncation, not
        corruption), so the header is legitimately undeterminable here rather
        than broken.
        """
        from elspeth.web.composer.tools.sources import _first_nonempty_csv_row_from_path

        cell_a = "A" * 40000
        cell_b = "B" * 40000
        header_row = f'"{cell_a}","{cell_b}"\n'
        assert len(header_row) > 64 * 1024
        assert len(cell_a) < 64 * 1024
        assert len(cell_b) < 64 * 1024
        path = tmp_path / "wide_header.csv"
        path.write_text(header_row + "trailer,row\n", encoding="utf-8")

        # The full file parses without error...
        full_rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8")), strict=True))
        assert full_rows[0] == [cell_a, cell_b]

        # ...but the bounded reader, which only ever inspects a fixed-size
        # prefix, cannot determine the header because the window closes
        # mid-quote. It must report "undeterminable" (None), never raise.
        assert _first_nonempty_csv_row_from_path(path) is None

    def test_first_nonempty_csv_row_from_path_still_raises_for_genuine_corruption(self, tmp_path: Path) -> None:
        """A small, fully-read file that is genuinely malformed CSV (not merely
        truncated by the window) must still surface as a boundary error so
        callers can escalate it.
        """
        from elspeth.web.composer.tools.sources import _CsvContentBoundaryError, _first_nonempty_csv_row_from_path

        path = tmp_path / "malformed.csv"
        path.write_text('"unterminated\n', encoding="utf-8")

        with pytest.raises(_CsvContentBoundaryError):
            _first_nonempty_csv_row_from_path(path)

    def test_first_nonempty_csv_row_from_path_tolerates_multibyte_char_straddling_window(self, tmp_path: Path) -> None:
        """A valid multi-byte UTF-8 character split across the window boundary
        is expected truncation, not corruption or a decoding failure — the
        file continues past the window, so a dangling partial byte sequence
        at the cut point must not raise ``UnicodeDecodeError``.
        """
        from elspeth.web.composer.tools.sources import _first_nonempty_csv_row_from_path

        # Pad so the multi-byte character's leading byte lands exactly at
        # the window's last byte, splitting it across the boundary.
        pad = "x" * ((64 * 1024) - 1)
        content = pad + "あ" + ",trailer\ndata,row\n"
        path = tmp_path / "straddled_char.csv"
        path.write_text(content, encoding="utf-8")

        assert path.read_bytes()[(64 * 1024) - 1 : (64 * 1024) + 2] == "あ".encode()

        result = _first_nonempty_csv_row_from_path(path)

        assert result is not None
        assert result[0].startswith("x")

    def test_set_pipeline_header_only_inline_csv_tolerates_candidate_first_record_crossing_window(self, tmp_path: Path) -> None:
        """A ready uploaded CSV whose first record merely crosses the bounded
        inspection window must not abort an unrelated header-only inline
        set_pipeline call with AuditIntegrityError. The candidate's header is
        legitimately undeterminable within the bounded window, so it is
        treated as a non-match rather than escalated as corruption.
        """
        from datetime import UTC, datetime

        state = _empty_state()
        catalog = _mock_catalog()
        engine, session_id = _session_engine_with_session()
        uploaded_id = str(uuid4())
        cell_a = "A" * 40000
        cell_b = "B" * 40000
        uploaded_content = f'"{cell_a}","{cell_b}"\n' + "trailer,row\n"
        assert len(uploaded_content.encode("utf-8")) > 64 * 1024
        uploaded_path = tmp_path / "blobs" / session_id / f"{uploaded_id}_wide_header.csv"
        uploaded_path.parent.mkdir(parents=True)
        uploaded_path.write_text(uploaded_content, encoding="utf-8")
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=uploaded_id,
                    session_id=session_id,
                    filename="wide_header.csv",
                    mime_type="text/csv",
                    size_bytes=len(uploaded_content.encode("utf-8")),
                    content_hash=_STUB_SHA256,
                    storage_path=str(uploaded_path),
                    created_at=now,
                    created_by="user",
                    source_description="uploaded rows with a wide first record",
                    status="ready",
                )
            )

        inline_content = "name,email\n"
        args = _valid_pipeline_args()
        args["source"] = {
            "plugin": "csv",
            "on_success": "source_out",
            "options": {"schema": {"mode": "observed"}},
            "inline_blob": {
                "filename": "contacts.csv",
                "mime_type": "text/csv",
                "content": inline_content,
            },
            "on_validation_failure": "quarantine",
        }
        args["outputs"][0]["options"]["path"] = str(tmp_path / "outputs" / session_id / "out.csv")
        args["outputs"][0]["options"]["mode"] = "write"
        args["outputs"][0]["options"]["collision_policy"] = "auto_increment"

        blob_provenance = _verbatim_blob_context(engine, session_id, inline_content)
        with _blob_operation(engine, session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "set_pipeline",
                args,
                state,
                catalog,
                data_dir=str(tmp_path),
                session_engine=engine,
                session_id=session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )

        assert result.success is True, result.data
        assert _default_source(result.updated_state) is not None
        assert not any(
            entry.error_code
            in {"inline_source_csv_inspection_failed", "inline_source_comparison_incomplete", "inline_source_content_conflict"}
            for entry in result.validation.errors
        )

    def test_set_pipeline_header_only_inline_csv_escalates_definitive_parse_error_before_window_end(self, tmp_path: Path) -> None:
        """A larger backing file must not hide syntax errors raised before the
        bounded reader reaches its prefix boundary.
        """
        from datetime import UTC, datetime

        from elspeth.contracts.errors import AuditIntegrityError

        state = _empty_state()
        catalog = _mock_catalog()
        engine, session_id = _session_engine_with_session()
        uploaded_id = str(uuid4())
        uploaded_content = '"a"x,b\n' + ("z" * (64 * 1024))
        uploaded_path = tmp_path / "blobs" / session_id / f"{uploaded_id}_malformed.csv"
        uploaded_path.parent.mkdir(parents=True)
        uploaded_path.write_text(uploaded_content, encoding="utf-8")
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=uploaded_id,
                    session_id=session_id,
                    filename="malformed.csv",
                    mime_type="text/csv",
                    size_bytes=len(uploaded_content.encode("utf-8")),
                    content_hash=_STUB_SHA256,
                    storage_path=str(uploaded_path),
                    created_at=now,
                    created_by="user",
                    source_description="uploaded rows with malformed CSV syntax",
                    status="ready",
                )
            )

        inline_content = "name,email\n"
        args = _valid_pipeline_args()
        args["source"] = {
            "plugin": "csv",
            "on_success": "source_out",
            "options": {"schema": {"mode": "observed"}},
            "inline_blob": {
                "filename": "contacts.csv",
                "mime_type": "text/csv",
                "content": inline_content,
            },
            "on_validation_failure": "quarantine",
        }
        args["outputs"][0]["options"]["path"] = str(tmp_path / "outputs" / session_id / "out.csv")
        args["outputs"][0]["options"]["mode"] = "write"
        args["outputs"][0]["options"]["collision_policy"] = "auto_increment"

        blob_provenance = _verbatim_blob_context(engine, session_id, inline_content)
        with (
            _blob_operation(engine, session_id) as (blob_authority, blob_operation_context),
            pytest.raises(AuditIntegrityError, match="bounded CSV inspection"),
        ):
            execute_tool(
                "set_pipeline",
                args,
                state,
                catalog,
                data_dir=str(tmp_path),
                session_engine=engine,
                session_id=session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )

    @pytest.mark.parametrize(
        ("candidate_count", "match_on_read", "expected_reads", "expected_error", "expected_code", "explanation_fragment"),
        [
            (50, 50, 50, "header-only inline CSV", "inline_source_content_conflict", "Its data rows were not verified"),
            (51, None, 0, "candidate scan limit", "inline_source_comparison_incomplete", "comparison did not finish"),
        ],
        ids=["match_at_budget", "over_budget"],
    )
    def test_set_pipeline_header_only_candidate_scan_has_fixed_file_read_budget(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        candidate_count: int,
        match_on_read: int | None,
        expected_reads: int,
        expected_error: str,
        expected_code: str,
        explanation_fragment: str,
    ) -> None:
        from datetime import UTC, datetime

        from elspeth.web.composer.tools import sources as sources_module

        state = _empty_state()
        catalog = _mock_catalog()
        engine, session_id = _session_engine_with_session()
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert(),
                [
                    {
                        "id": str(uuid4()),
                        "session_id": session_id,
                        "filename": f"candidate-{index}.csv",
                        "mime_type": "text/csv",
                        "size_bytes": 1024,
                        "content_hash": _STUB_SHA256,
                        "storage_path": str(tmp_path / f"missing-{index}.csv"),
                        "created_at": now,
                        "created_by": "user",
                        "source_description": "uploaded rows",
                        "status": "ready",
                    }
                    for index in range(candidate_count)
                ],
            )

        reads = 0

        def _record_read(_path: Path) -> tuple[str, ...] | None:
            nonlocal reads
            reads += 1
            return ("name", "email") if reads == match_on_read else None

        inline_content = "name,email\n"
        args = _valid_pipeline_args()
        args["source"] = {
            "plugin": "csv",
            "on_success": "source_out",
            "options": {"schema": {"mode": "observed"}},
            "inline_blob": {
                "filename": "contacts.csv",
                "mime_type": "text/csv",
                "content": inline_content,
            },
            "on_validation_failure": "quarantine",
        }
        args["outputs"][0]["options"]["path"] = str(tmp_path / "outputs" / session_id / "out.csv")
        args["outputs"][0]["options"]["mode"] = "write"
        args["outputs"][0]["options"]["collision_policy"] = "auto_increment"

        blob_provenance = _verbatim_blob_context(engine, session_id, inline_content)
        with _blob_operation(engine, session_id) as (blob_authority, blob_operation_context):
            monkeypatch.setattr(sources_module, "_first_nonempty_csv_row_from_path", _record_read)
            result = execute_tool(
                "set_pipeline",
                args,
                state,
                catalog,
                data_dir=str(tmp_path),
                session_engine=engine,
                session_id=session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )

        assert result.success is False
        assert expected_error in result.validation.errors[0].message
        assert reads == expected_reads
        _assert_inline_source_refusal(result, state, expected_code, explanation_fragment)
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(blobs_table)).scalar_one() == candidate_count

    def test_set_pipeline_unknown_source_plugin_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.get_schema.side_effect = ValueError("Unknown plugin: nonexistent")
        args = _valid_pipeline_args()
        args["source"]["plugin"] = "nonexistent"
        result = execute_tool("set_pipeline", args, state, catalog)
        assert result.success is False
        assert "source" in result.validation.errors[0].message.lower()

    def test_set_pipeline_unknown_node_plugin_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()

        from elspeth.web.catalog.schemas import PluginSchemaInfo

        def selective_schema(plugin_type: Literal["source", "transform", "sink"], name: str) -> PluginSchemaInfo:
            if plugin_type == "transform" and name == "badplugin":
                raise ValueError(f"Unknown plugin: {name}")
            return PluginSchemaInfo(
                name=name,
                plugin_type=plugin_type,
                description="",
                json_schema={},
                knob_schema={"fields": []},
            )

        catalog.get_schema.side_effect = selective_schema
        args = _valid_pipeline_args()
        args["nodes"][0]["plugin"] = "badplugin"
        result = execute_tool("set_pipeline", args, state, catalog)
        assert result.success is False
        assert "transform" in result.validation.errors[0].message.lower()

    def test_set_pipeline_unknown_sink_plugin_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()

        from elspeth.web.catalog.schemas import PluginSchemaInfo

        def selective_schema(plugin_type: Literal["source", "transform", "sink"], name: str) -> PluginSchemaInfo:
            if plugin_type == "sink" and name == "badsink":
                raise ValueError(f"Unknown plugin: {name}")
            return PluginSchemaInfo(
                name=name,
                plugin_type=plugin_type,
                description="",
                json_schema={},
                knob_schema={"fields": []},
            )

        catalog.get_schema.side_effect = selective_schema
        args = _valid_pipeline_args()
        args["outputs"][0]["plugin"] = "badsink"
        result = execute_tool("set_pipeline", args, state, catalog)
        assert result.success is False
        assert "sink" in result.validation.errors[0].message.lower()

    def test_set_pipeline_missing_required_field_fails(self) -> None:
        """Missing required field is now a Tier-3 ToolArgumentError.

        Post Task 14 (set_pipeline manifest promotion):
        :class:`SetPipelineArgumentsModel` / :class:`_SetPipelineSourceModel`
        require ``source.on_success``; absence raises a structured
        :class:`pydantic.ValidationError` that the handler re-raises as
        :class:`ToolArgumentError` BEFORE any handler-side spec
        construction.  Previously the omission fell through to the
        ``try: SourceSpec(...)``/``except KeyError`` branch and was
        reported via ``"Invalid pipeline spec"`` in ``result.validation.errors[0].message``;
        that branch was removed in Task 14 because Pydantic now catches
        the type errors upstream.

        The compose-loop ARG_ERROR routing at ``service.py:2480`` builds
        the LLM-facing error payload from the captured
        :class:`ToolArgumentError`.  This unit-level test reaches the
        handler directly via ``execute_tool``, so it observes the bare
        exception class — not the wrapped LLM payload.
        """
        from elspeth.web.composer.protocol import ToolArgumentError

        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        # Remove on_success from source — required field
        del args["source"]["on_success"]
        with pytest.raises(ToolArgumentError) as exc_info:
            execute_tool("set_pipeline", args, state, catalog)
        assert isinstance(exc_info.value.__cause__, PydanticValidationError)

    def test_set_pipeline_replaces_entire_state(self) -> None:
        # Build a state with 3 nodes first
        state = _empty_state()
        catalog = _mock_catalog()
        for i in range(3):
            r = execute_tool(
                "upsert_node",
                {
                    "id": f"t{i}",
                    "node_type": "transform",
                    "plugin": "passthrough",
                    "input": "in",
                    "on_success": "out",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}},
                },
                state,
                catalog,
            )
            state = r.updated_state
        assert len(state.nodes) == 3

        # set_pipeline with 1 node replaces all
        result = execute_tool("set_pipeline", _valid_pipeline_args(), state, catalog)
        assert result.success is True
        assert len(result.updated_state.nodes) == 1
        assert result.updated_state.nodes[0].id == "t1"

    def test_set_pipeline_version_increments(self) -> None:
        from elspeth.web.composer.state import PipelineMetadata

        state = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=5,
        )
        catalog = _mock_catalog()
        result = execute_tool("set_pipeline", _valid_pipeline_args(), state, catalog)
        assert result.success is True
        assert result.updated_state.version == 6

    def test_set_pipeline_validation_runs(self) -> None:
        """A pipeline with a disconnected node (unreachable input) should produce
        validation errors or is_valid=False."""
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        # Add a second node that has an input not connected to anything
        args["nodes"].append(
            {
                "id": "t2",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "orphan_channel",
                "on_success": "main",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        )
        result = execute_tool("set_pipeline", args, state, catalog)
        assert result.success is True
        assert result.validation is not None
        # The orphan node has no reachable input — validation should flag it
        assert result.validation.is_valid is False
        assert len(result.validation.errors) > 0

    def test_set_pipeline_surfaces_fixed_schema_edge_contract_mismatch(self) -> None:
        """LLM fixed-schema requirements must reject source rows before runtime."""
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="llm",
                description="LLM transform",
                plugin_type="transform",
                config_fields=[],
            ),
        ]
        args = {
            "source": {
                "plugin": "text",
                "on_success": "rate_in",
                "options": {
                    "path": "/data/colors.txt",
                    "column": "color",
                    "schema": {"mode": "fixed", "fields": ["color: str"]},
                },
                "on_validation_failure": "discard",
            },
            "nodes": [
                {
                    "id": "transform_rate_teal_pairing",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "rate_in",
                    "on_success": "results",
                    "on_error": "discard",
                    "options": {
                        "provider": "openrouter",
                        "model": "openai/gpt-4o",
                        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                        "prompt_template": "Rate {{ row.color }} for teal pairing.",
                        "required_input_fields": ["color"],
                        "schema": {
                            "mode": "fixed",
                            "fields": ["color: str", "teal_pairing_rating: str"],
                        },
                    },
                }
            ],
            "edges": [
                {
                    "id": "e1",
                    "from_node": "source",
                    "to_node": "transform_rate_teal_pairing",
                    "edge_type": "on_success",
                    "label": None,
                },
                {
                    "id": "e2",
                    "from_node": "transform_rate_teal_pairing",
                    "to_node": "results",
                    "edge_type": "on_success",
                    "label": None,
                },
            ],
            "outputs": [
                {
                    "sink_name": "results",
                    "plugin": "csv",
                    "options": {"path": "/data/results.csv", "schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            ],
        }

        result = execute_tool("set_pipeline", args, state, catalog)

        assert result.success is True
        assert result.validation is not None
        assert result.validation.is_valid is False
        assert any("teal_pairing_rating" in error.message for error in result.validation.errors)
        contract = next(ec for ec in result.validation.edge_contracts if ec.to_id == "transform_rate_teal_pairing")
        assert contract.from_id == "source"
        assert contract.consumer_requires == ("color", "teal_pairing_rating")
        assert contract.producer_guarantees == ("color",)
        assert contract.missing_fields == ("teal_pairing_rating",)
        assert contract.satisfied is False

    def test_set_pipeline_unknown_node_type_invalidates_state(self) -> None:
        """set_pipeline keeps enum parsing recoverable but Stage 1 must reject unknown node types."""
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        args["nodes"][0]["node_type"] = "bogus"

        result = execute_tool("set_pipeline", args, state, catalog)

        assert result.success is True
        assert result.validation is not None
        assert result.validation.is_valid is False
        assert any("unknown node_type 'bogus'" in error.message for error in result.validation.errors)

    def test_set_pipeline_gate_injection_rejected(self) -> None:
        """set_pipeline rejects gate nodes with injection in condition."""
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        args["nodes"].append(
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "source_out",
                "on_success": None,
                "on_error": None,
                "options": {},
                "condition": "__import__('os').system('whoami')",
                "routes": {"true": "main", "false": "main"},
            }
        )
        result = execute_tool("set_pipeline", args, state, catalog)
        assert result.success is False
        assert "Forbidden construct" in result.validation.errors[0].message
        assert result.updated_state.version == 1

    def test_set_pipeline_gate_malformed_condition_rejected(self) -> None:
        """set_pipeline rejects gate nodes with syntax errors in condition."""
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        args["nodes"].append(
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "source_out",
                "on_success": None,
                "on_error": None,
                "options": {},
                "condition": "row['x'] >>>= 5",
                "routes": {"true": "main", "false": "main"},
            }
        )
        result = execute_tool("set_pipeline", args, state, catalog)
        assert result.success is False
        assert "Invalid gate condition syntax" in result.validation.errors[0].message

    def test_value_transform_bad_expression_error_code_parity(self) -> None:
        """Both authoring routes code the same prevalidation failure (A6).

        The same _prevalidate_transform_for_context failure was returned
        UNCODED on upsert_node but coded plugin_options_invalid on
        set_pipeline, so explain_validation_error misdiagnosed the
        upsert_node path. Parity is pinned so it cannot drift back.
        """
        bad_operations = [{"target": "out", "expression": "title(row['text'])"}]

        catalog = _mock_catalog()
        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
                "on_validation_failure": "quarantine",
            },
            _empty_state(),
            catalog,
        )
        node_result = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "value_transform",
                "input": "t1",
                "on_success": "main",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}, "operations": bad_operations},
            },
            r1.updated_state,
            catalog,
        )
        assert node_result.success is False

        args = _valid_pipeline_args()
        args["nodes"].append(
            {
                "id": "t_bad",
                "node_type": "transform",
                "plugin": "value_transform",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}, "operations": bad_operations},
            }
        )
        pipeline_result = execute_tool("set_pipeline", args, _empty_state(), catalog)
        assert pipeline_result.success is False

        assert pipeline_result.validation.errors[0].error_code == "plugin_options_invalid"
        assert node_result.validation.errors[0].error_code == pipeline_result.validation.errors[0].error_code

    def test_set_pipeline_gate_valid_condition_accepted(self) -> None:
        """set_pipeline accepts gate nodes with valid conditions."""
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        args["nodes"].append(
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "source_out",
                "on_success": None,
                "on_error": None,
                "options": {},
                "condition": "row['score'] >= 0.5",
                "routes": {"true": "main", "false": "main"},
            }
        )
        result = execute_tool("set_pipeline", args, state, catalog)
        assert result.success is True
        gate_nodes = [n for n in result.updated_state.nodes if n.node_type == "gate"]
        assert len(gate_nodes) == 1
        assert gate_nodes[0].condition == "row['score'] >= 0.5"

    def test_set_pipeline_materializes_inline_blob_source(self, tmp_path: Path) -> None:
        """set_pipeline can atomically bind inline literal data as the source."""
        state = _empty_state()
        catalog = _mock_catalog()
        engine, session_id = _session_engine_with_session()
        output_path = tmp_path / "outputs" / session_id / "append.csv"
        args = {
            "source": {
                "plugin": "text",
                "on_success": "source_out",
                "options": {
                    "column": "text",
                    "schema": {"mode": "flexible", "fields": ["text: str"], "guaranteed_fields": ["text"]},
                },
                "inline_blob": {
                    "filename": "input.txt",
                    "mime_type": "text/plain",
                    "content": "hello",
                    "description": "literal input from the user prompt",
                },
                "on_validation_failure": "discard",
            },
            "nodes": [
                {
                    "id": "append_world",
                    "node_type": "transform",
                    "plugin": "value_transform",
                    "input": "source_out",
                    "on_success": "main",
                    "on_error": "discard",
                    "options": {
                        "schema": {
                            "mode": "observed",
                            "guaranteed_fields": ["text"],
                            "required_fields": ["text"],
                        },
                        "operations": [
                            {
                                "target": "text",
                                "expression": "row['text'] + ' world'",
                            }
                        ],
                    },
                }
            ],
            "edges": [
                {
                    "id": "source_to_append",
                    "from_node": "source",
                    "to_node": "append_world",
                    "edge_type": "on_success",
                },
                {
                    "id": "append_to_main",
                    "from_node": "append_world",
                    "to_node": "main",
                    "edge_type": "on_success",
                },
            ],
            "outputs": [
                {
                    "sink_name": "main",
                    "plugin": "csv",
                    "options": {
                        "path": str(output_path),
                        "schema": {"mode": "observed", "required_fields": ["text"]},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                }
            ],
            "metadata": {"name": "Append literal text"},
        }

        blob_provenance = _verbatim_blob_context(engine, session_id, "hello")
        with _blob_operation(engine, session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "set_pipeline",
                args,
                state,
                catalog,
                data_dir=str(tmp_path),
                session_engine=engine,
                session_id=session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                **blob_provenance,
            )

        assert result.success is True
        assert _default_source(result.updated_state) is not None
        source_options = _default_source(result.updated_state).options
        assert source_options["column"] == "text"
        assert source_options["blob_ref"] == result.data["inline_blob"]["blob_id"]
        # Self-authorship marker (elspeth-47eba5cced): the blob's bytes came
        # from this call's own inline_blob argument, and mid-turn custody
        # rewrites excise them from the live transcript — without the marker
        # a later get_blob_content of this blob reads as discovery.
        assert result.data["inline_blob"]["originated_in"] == "this_tool_call"
        assert "hello" not in str(result.to_dict())

        with engine.connect() as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == source_options["blob_ref"])).one()
        assert row.filename == "input.txt"
        assert row.mime_type == "text/plain"
        assert row.source_description == "literal input from the user prompt"
        assert Path(row.storage_path).read_text(encoding="utf-8") == "hello"

    def test_set_pipeline_non_gate_with_condition_skips_validation(self) -> None:
        """set_pipeline only validates conditions on gate nodes.

        Transform nodes with a stray condition field are not expression-validated
        (structural validation in CompositionState.validate() catches this).
        """
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        # Add a condition to the transform node — structurally wrong but not expression-validated
        args["nodes"][0]["condition"] = "this is garbage syntax!!!"
        result = execute_tool("set_pipeline", args, state, catalog)
        # Succeeds at tool level; validate() flags structural mismatch
        assert result.success is True
