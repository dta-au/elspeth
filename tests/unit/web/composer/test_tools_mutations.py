"""Composer incremental mutation tools and admission policies."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from elspeth.contracts.enums import CreationModality
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.schemas import PluginSummary
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolContext, _apply_merge_patch, _credential_wiring_contract_failure
from elspeth.web.composer.tools.sources import _options_with_source_blob_review
from elspeth.web.config import WebSettings
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, PROMPT_SHIELD_USER_TERM, SOURCE_AUTHORING_KEY
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from tests.unit.web.composer import test_set_source_from_blobs as plural_blob_tests
from tests.unit.web.composer.test_set_source_from_blobs import _create_ready_blob
from tests.unit.web.composer.test_tools import (
    _AWS_S3_ENDPOINT_SENTINEL,
    _STUB_SHA256,
    _VALID_AWS_S3_OPTIONS,
    _VALID_AWS_S3_PROFILE_OPTIONS,
    _assert_aws_s3_endpoint_url_rejected,
    _assert_credential_material_refusal,
    _aws_s3_output_state,
    _aws_s3_source_state,
    _default_source,
    _empty_state,
    _llm_options_with_api_key,
    _mock_catalog,
    _profiled_s3_policy_pair,
    _restricted_policy_pair,
    _SecretServiceDouble,
    _trained_tool_context,
    _valid_pipeline_args,
    _wiring_policy,
    execute_tool,
)

harness = plural_blob_tests.harness


class TestAwsS3EndpointUrlComposerPolicy:
    def test_aws_s3_set_source_rejects_endpoint_url_without_mutating_state(self) -> None:
        state = _empty_state()

        result = execute_tool(
            "set_source",
            {
                "plugin": "aws_s3",
                "on_success": "main",
                "options": {"endpoint_url": _AWS_S3_ENDPOINT_SENTINEL},
                "on_validation_failure": "discard",
            },
            state,
            _mock_catalog(),
        )

        _assert_aws_s3_endpoint_url_rejected(result, state, rejected_component="source", rejection_operation="saved_graph")

    def test_aws_s3_patch_source_options_rejects_endpoint_url_without_mutating_state(self) -> None:
        state = _aws_s3_source_state()

        result = execute_tool(
            "patch_source_options",
            {"patch": {"endpoint_url": _AWS_S3_ENDPOINT_SENTINEL}},
            state,
            _mock_catalog(),
        )

        _assert_aws_s3_endpoint_url_rejected(result, state, rejected_component="source", rejection_operation="saved_graph")

    def test_aws_s3_set_output_rejects_endpoint_url_without_mutating_state(self) -> None:
        state = _empty_state()

        result = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "aws_s3",
                "options": {"endpoint_url": _AWS_S3_ENDPOINT_SENTINEL},
                "on_write_failure": "discard",
            },
            state,
            _mock_catalog(),
        )

        assert not result.success
        assert result.updated_state is state and result.updated_state.version == state.version
        assert result.data is None
        entry = result.validation.errors[0]
        assert (entry.error_code, entry.rejected_component) == ("aws_s3_endpoint_url_not_allowed", "output:main")
        assert "endpoint_url" in entry.message
        assert _AWS_S3_ENDPOINT_SENTINEL not in repr(result.validation)

    def test_aws_s3_patch_output_options_rejects_endpoint_url_without_mutating_state(self) -> None:
        state = _aws_s3_output_state()

        result = execute_tool(
            "patch_output_options",
            {"sink_name": "main", "patch": {"endpoint_url": _AWS_S3_ENDPOINT_SENTINEL}},
            state,
            _mock_catalog(),
        )

        assert not result.success
        assert result.updated_state is state and result.updated_state.version == state.version
        assert result.data is None
        entry = result.validation.errors[0]
        assert (entry.error_code, entry.rejected_component) == ("aws_s3_endpoint_url_not_allowed", "output:main")
        assert "endpoint_url" in entry.message
        assert _AWS_S3_ENDPOINT_SENTINEL not in repr(result.validation)

    def test_aws_s3_set_pipeline_named_source_rejects_endpoint_url_without_mutating_state(self) -> None:
        state = _empty_state()
        args = _valid_pipeline_args()
        args.pop("source")
        args["sources"] = {
            "archive": {
                "plugin": "aws_s3",
                "on_success": "main",
                "options": {"endpoint_url": _AWS_S3_ENDPOINT_SENTINEL},
                "on_validation_failure": "discard",
            }
        }
        args["nodes"] = []
        args["edges"] = []

        result = execute_tool("set_pipeline", args, state, _mock_catalog())

        _assert_aws_s3_endpoint_url_rejected(result, state, rejected_component="source:archive")

    def test_aws_s3_set_pipeline_legacy_source_rejects_endpoint_url_without_mutating_state(self) -> None:
        state = _empty_state()
        args = _valid_pipeline_args()
        args["source"]["plugin"] = "aws_s3"
        args["source"]["options"] = {"endpoint_url": _AWS_S3_ENDPOINT_SENTINEL}

        result = execute_tool("set_pipeline", args, state, _mock_catalog())

        _assert_aws_s3_endpoint_url_rejected(result, state, rejected_component="source")

    def test_aws_s3_set_pipeline_output_rejects_endpoint_url_without_mutating_state(self) -> None:
        state = _empty_state()
        args = _valid_pipeline_args()
        args["outputs"][0]["plugin"] = "aws_s3"
        args["outputs"][0]["options"] = {"endpoint_url": _AWS_S3_ENDPOINT_SENTINEL}

        result = execute_tool("set_pipeline", args, state, _mock_catalog())

        _assert_aws_s3_endpoint_url_rejected(result, state, rejected_component="output:main")

    def test_aws_s3_set_source_from_blob_rejects_raw_endpoint_url_before_lookup(self) -> None:
        state = _empty_state()

        with patch("elspeth.web.composer.tools.sources._resolve_source_blob") as mock_resolve:
            mock_resolve.side_effect = AssertionError("blob lookup must not run")
            result = execute_tool(
                "set_source_from_blob",
                {
                    "blob_id": str(uuid4()),
                    "plugin": "aws_s3",
                    "on_success": "main",
                    "options": {"endpoint_url": _AWS_S3_ENDPOINT_SENTINEL},
                },
                state,
                _mock_catalog(),
            )

        _assert_aws_s3_endpoint_url_rejected(result, state, rejected_component="source", rejection_operation="saved_graph")
        mock_resolve.assert_not_called()

    def test_aws_s3_set_source_from_blob_rejects_resolved_endpoint_url_without_mutating_state(self) -> None:
        from elspeth.web.composer.tools.sources import _execute_set_source_from_blob, _ResolvedSourceBlob

        state = _empty_state()
        resolved = _ResolvedSourceBlob(
            plugin="aws_s3",
            options={"endpoint_url": _AWS_S3_ENDPOINT_SENTINEL},
            payload={
                "blob_id": str(uuid4()),
                "filename": "input.jsonl",
                "mime_type": "application/jsonl",
                "size_bytes": 1,
                "content_hash": _STUB_SHA256,
            },
            creation_modality=CreationModality.VERBATIM,
        )

        with patch("elspeth.web.composer.tools.sources._resolve_source_blob", return_value=resolved):
            result = _execute_set_source_from_blob(
                {"blob_id": str(uuid4()), "on_success": "main", "on_validation_failure": "discard", "options": {}},
                state,
                _trained_tool_context(),
            )

        _assert_aws_s3_endpoint_url_rejected(result, state, rejected_component="source", rejection_operation="saved_graph")

    @pytest.mark.parametrize(
        ("target", "target_id", "state_factory"),
        [
            ("source", None, _aws_s3_source_state),
            ("output", "main", _aws_s3_output_state),
        ],
    )
    def test_aws_s3_wire_secret_ref_rejects_endpoint_url_without_mutating_state(
        self,
        target: str,
        target_id: str | None,
        state_factory: Callable[[], CompositionState],
    ) -> None:
        state = state_factory()
        secret_name = "ENDPOINT_URL_SECRET_CANARY"
        secret_service = _SecretServiceDouble()
        secret_service.has_ref.return_value = True
        arguments: dict[str, Any] = {
            "name": secret_name,
            "target": target,
            "option_key": "endpoint_url",
        }
        if target_id is not None:
            arguments["target_id"] = target_id

        result = execute_tool(
            "wire_secret_ref",
            arguments,
            state,
            _mock_catalog(),
            secret_service=secret_service,
            user_id="test-user",
            # Authorize the wiring so the endpoint_url policy is the gate
            # under test, not the secret-wiring allowlist (elspeth-f3c1aafd25).
            secret_wiring_policy=_wiring_policy(
                secret_name,
                "source" if target == "source" else "sink",
                "aws_s3",
                "endpoint_url",
            ),
        )

        _assert_aws_s3_endpoint_url_rejected(result, state, forbidden_value=secret_name)


class TestAwsS3SourceComposerPolicy:
    """Mutation-time profile enforcement must match authoritative validation."""

    def test_set_source_rejects_aws_s3_for_web_authored_session_without_mutating_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        view, snapshot = _restricted_policy_pair(
            catalog,
            PluginId("source", "aws_s3"),
            PluginUnavailableReason.PROFILE_UNAVAILABLE,
        )

        result = execute_tool(
            "set_source",
            {
                "plugin": "aws_s3",
                "on_success": "main",
                "options": _VALID_AWS_S3_OPTIONS,
                "on_validation_failure": "discard",
            },
            state,
            view,
            plugin_snapshot=snapshot,
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.updated_state.version == state.version
        assert result.data is None
        assert "operator profile" in result.validation.errors[0].message
        assert result.validation.errors[0].error_code == "profile_unavailable"
        # The planner reads this entry through _rejection_entries and
        # _allowlisted_candidate_feedback to derive rejection_codes.
        assert result.validation.errors[0].component == "rejected_mutation"

    def test_set_source_allows_aws_s3_for_trained_operator_session(self) -> None:
        """Trained-operator (local MCP) sessions remain exempt, matching validation.py."""
        state = _empty_state()

        result = execute_tool(
            "set_source",
            {
                "plugin": "aws_s3",
                "on_success": "main",
                "options": _VALID_AWS_S3_OPTIONS,
                "on_validation_failure": "discard",
            },
            state,
            _mock_catalog(),
        )

        assert result.success is True
        assert result.updated_state.sources["source"].plugin == "aws_s3"

    def test_set_source_accepts_operator_profile_and_persists_only_safe_options(self) -> None:
        state = _empty_state()
        view, snapshot = _profiled_s3_policy_pair()

        result = execute_tool(
            "set_source",
            {
                "plugin": "aws_s3",
                "on_success": "main",
                "options": dict(_VALID_AWS_S3_PROFILE_OPTIONS),
                "on_validation_failure": "discard",
            },
            state,
            view,
            plugin_snapshot=snapshot,
        )

        assert result.success is True, result.data
        assert deep_thaw(result.updated_state.sources["source"].options) == dict(_VALID_AWS_S3_PROFILE_OPTIONS)
        assert "bucket" not in repr(result.updated_state.sources["source"].options)

    def test_set_pipeline_rejects_aws_s3_named_source_for_web_authored_session_without_mutating_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        view, snapshot = _restricted_policy_pair(
            catalog,
            PluginId("source", "aws_s3"),
            PluginUnavailableReason.PROFILE_UNAVAILABLE,
        )
        args = _valid_pipeline_args()
        args.pop("source")
        args["sources"] = {
            "archive": {
                "plugin": "aws_s3",
                "on_success": "main",
                "options": _VALID_AWS_S3_OPTIONS,
                "on_validation_failure": "discard",
            }
        }
        args["nodes"] = []
        args["edges"] = []

        result = execute_tool("set_pipeline", args, state, view, plugin_snapshot=snapshot)

        assert result.success is False
        assert result.updated_state is state
        assert result.updated_state.version == state.version
        assert result.data is None
        assert "Source 'archive':" in result.validation.errors[0].message
        assert "operator profile" in result.validation.errors[0].message
        assert result.validation.errors[0].error_code == "profile_unavailable"

    def test_set_pipeline_rejects_aws_s3_legacy_source_for_web_authored_session_without_mutating_state(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        view, snapshot = _restricted_policy_pair(
            catalog,
            PluginId("source", "aws_s3"),
            PluginUnavailableReason.PROFILE_UNAVAILABLE,
        )
        args = _valid_pipeline_args()
        args["source"]["plugin"] = "aws_s3"
        args["source"]["options"] = dict(_VALID_AWS_S3_OPTIONS)

        result = execute_tool("set_pipeline", args, state, view, plugin_snapshot=snapshot)

        assert result.success is False
        assert result.updated_state is state
        assert result.updated_state.version == state.version
        assert result.data is None
        assert "operator profile" in result.validation.errors[0].message
        assert result.validation.errors[0].error_code == "profile_unavailable"

    def test_set_pipeline_allows_aws_s3_legacy_source_for_trained_operator_session(self) -> None:
        """Trained-operator (local MCP) sessions remain exempt, matching validation.py."""
        state = _empty_state()
        args = _valid_pipeline_args()
        args["source"]["plugin"] = "aws_s3"
        args["source"]["options"] = dict(_VALID_AWS_S3_OPTIONS)

        result = execute_tool("set_pipeline", args, state, _mock_catalog())

        assert result.success is True
        assert result.updated_state.sources["source"].plugin == "aws_s3"

    @pytest.mark.parametrize("source_shape", ["legacy", "named"])
    def test_set_pipeline_accepts_operator_profiled_s3_source(self, source_shape: str) -> None:
        state = _empty_state()
        view, snapshot = _profiled_s3_policy_pair()
        args = _valid_pipeline_args()
        source = {
            "plugin": "aws_s3",
            "on_success": "main",
            "options": dict(_VALID_AWS_S3_PROFILE_OPTIONS),
            "on_validation_failure": "discard",
        }
        if source_shape == "legacy":
            args["source"] = source
        else:
            args.pop("source")
            args["sources"] = {"archive": source}
        args["nodes"] = []
        args["edges"] = []

        result = execute_tool("set_pipeline", args, state, view, plugin_snapshot=snapshot)

        assert result.success is True, result.data
        persisted = next(iter(result.updated_state.sources.values()))
        assert deep_thaw(persisted.options) == dict(_VALID_AWS_S3_PROFILE_OPTIONS)

    def test_set_source_rejects_private_bucket_override_without_mutating_state(self) -> None:
        state = _empty_state()
        view, snapshot = _profiled_s3_policy_pair()

        result = execute_tool(
            "set_source",
            {
                "plugin": "aws_s3",
                "on_success": "main",
                "options": {**_VALID_AWS_S3_PROFILE_OPTIONS, "bucket": "attacker-bucket"},
                "on_validation_failure": "discard",
            },
            state,
            view,
            plugin_snapshot=snapshot,
        )

        assert result.success is False
        assert result.updated_state is state
        assert "attacker-bucket" not in repr(result.data)

    def test_set_source_from_blob_rejects_aws_s3_for_web_authored_session_without_mutating_state(self, harness) -> None:
        from elspeth.web.composer.tools.sources import _execute_set_source_from_blob

        state = _empty_state()
        engine, _, session_id = harness
        blob_id = _create_ready_blob(harness, content=b'{"a":1}\n', filename="input.jsonl", mime_type="application/jsonl")
        catalog = _mock_catalog()
        view, snapshot = _restricted_policy_pair(
            catalog,
            PluginId("source", "aws_s3"),
            PluginUnavailableReason.PROFILE_UNAVAILABLE,
        )

        result = _execute_set_source_from_blob(
            {
                "blob_id": blob_id,
                "plugin": "aws_s3",
                "on_success": "main",
                "on_validation_failure": "discard",
                "options": dict(_VALID_AWS_S3_OPTIONS),
            },
            state,
            ToolContext(catalog=view, plugin_snapshot=snapshot, session_engine=engine, session_id=session_id),
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.updated_state.version == state.version
        assert result.data is None
        assert "operator profile" in result.validation.errors[0].message
        assert result.validation.errors[0].error_code == "profile_unavailable"

    def test_set_source_from_blob_trained_operator_still_requires_supported_source_options(self, harness) -> None:
        """Policy admission does not make a local blob an AWS object binding."""
        from elspeth.web.composer.tools.sources import _execute_set_source_from_blob

        state = _empty_state()
        engine, _, session_id = harness
        blob_id = _create_ready_blob(harness, content=b'{"a":1}\n', filename="input.jsonl", mime_type="application/jsonl")

        result = _execute_set_source_from_blob(
            {
                "blob_id": blob_id,
                "plugin": "aws_s3",
                "on_success": "main",
                "on_validation_failure": "discard",
                "options": dict(_VALID_AWS_S3_OPTIONS),
            },
            state,
            _trained_tool_context(session_engine=engine, session_id=session_id),
        )

        assert not result.success and result.updated_state is state
        assert result.validation.errors[0].error_code == "plugin_options_invalid"
        assert result.validation.errors[0].plugin_identity == ("source", "aws_s3")
        assert "path" in result.validation.errors[0].message

    def test_snapshot_prohibited_source_selection_explains_the_generic_policy(self) -> None:
        """A snapshot-declared prohibition must name the policy, not a code.

        Once ``build_plugin_snapshot`` declines ``source:aws_s3`` with
        ``WEB_SURFACE_PROHIBITED``, the plugin-visibility gate is the FIRST one
        an authoring call meets (it precedes the aws_s3-specific gate below in
        both ``set_source`` and ``set_pipeline``). Its message therefore has to
        carry the policy cause; otherwise the honest decline degrades to a bare
        code and the author is told a plugin is "unavailable" with nothing to
        act on.
        """
        from elspeth.web.composer.tools._common import _validate_plugin_name

        catalog = _mock_catalog()
        view, snapshot = _restricted_policy_pair(
            catalog,
            PluginId("source", "aws_s3"),
            PluginUnavailableReason.WEB_SURFACE_PROHIBITED,
        )

        violation = _validate_plugin_name(ToolContext(catalog=view, plugin_snapshot=snapshot), "source", "aws_s3")

        assert violation is not None
        assert violation.error_code is PluginUnavailableReason.WEB_SURFACE_PROHIBITED
        assert "prohibited on the web authoring surface" in violation.message

        result = execute_tool(
            "set_source",
            {
                "plugin": "aws_s3",
                "on_success": "main",
                "options": _VALID_AWS_S3_OPTIONS,
                "on_validation_failure": "discard",
            },
            _empty_state(),
            view,
            plugin_snapshot=snapshot,
        )

        assert result.success is False
        assert result.validation.errors[0].error_code == "plugin_not_allowed_on_web"
        assert "prohibited on the web authoring surface" in result.validation.errors[0].message

    def test_prohibited_sink_selection_is_unaffected(self) -> None:
        """Only the SOURCE is prohibited; S3 writes stay authorable."""
        catalog = _mock_catalog()
        view, snapshot = _restricted_policy_pair(
            catalog,
            PluginId("source", "aws_s3"),
            PluginUnavailableReason.WEB_SURFACE_PROHIBITED,
        )

        result = execute_tool(
            "set_output",
            {
                "sink_name": "archive",
                "plugin": "aws_s3",
                "on_write_failure": "discard",
                "options": {"bucket": "test-bucket", "key": "out.jsonl", "schema": {"mode": "observed"}},
            },
            _empty_state(),
            view,
            plugin_snapshot=snapshot,
        )

        assert result.success is True, result.data
        assert [(output.name, output.plugin) for output in result.updated_state.outputs] == [("archive", "aws_s3")]


class TestSetSource:
    def test_sets_source(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert result.success is True
        assert _default_source(result.updated_state) is not None
        assert _default_source(result.updated_state).plugin == "csv"
        assert result.updated_state.version == 2
        assert "source" in result.affected_nodes

    def test_set_source_with_source_name_adds_named_source(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()

        first = execute_tool(
            "set_source",
            {
                "source_name": "customers",
                "plugin": "csv",
                "on_success": "customer_rows",
                "options": {"path": "/data/customers.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            state,
            catalog,
        )
        second = execute_tool(
            "set_source",
            {
                "source_name": "orders",
                "plugin": "json",
                "on_success": "order_rows",
                "options": {"path": "/data/orders.json", "schema": {"mode": "observed"}},
                "on_validation_failure": "bad_orders",
            },
            first.updated_state,
            catalog,
        )

        assert second.success is True
        assert tuple(second.updated_state.sources) == ("customers", "orders")
        assert second.updated_state.sources["orders"].plugin == "json"
        assert "source:orders" in second.affected_nodes

    @pytest.mark.parametrize("source_name", ("Orders", "on_success", " "))
    def test_set_source_invalid_source_name_raises_arg_error(self, source_name: str) -> None:
        from elspeth.web.composer.protocol import ToolArgumentError

        state = _empty_state()
        catalog = _mock_catalog()

        with pytest.raises(ToolArgumentError) as exc_info:
            execute_tool(
                "set_source",
                {
                    "source_name": source_name,
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"path": "/data/orders.csv", "schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                },
                state,
                catalog,
            )

        assert exc_info.value.argument == "source_name"
        assert isinstance(exc_info.value.__cause__, ValueError)

    def test_named_source_patch_and_clear_target_only_selected_source(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        customers = execute_tool(
            "set_source",
            {
                "source_name": "customers",
                "plugin": "csv",
                "on_success": "customer_rows",
                "options": {"path": "/data/customers.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            state,
            catalog,
        ).updated_state
        both = execute_tool(
            "set_source",
            {
                "source_name": "orders",
                "plugin": "json",
                "on_success": "order_rows",
                "options": {"path": "/data/orders.json", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            customers,
            catalog,
        ).updated_state

        patched = execute_tool(
            "patch_source_options",
            {"source_name": "orders", "patch": {"path": "/data/orders-patched.json"}},
            both,
            catalog,
        )
        cleared = execute_tool("clear_source", {"source_name": "customers"}, patched.updated_state, catalog)

        assert patched.success is True
        assert patched.updated_state.sources["orders"].options["path"] == "/data/orders-patched.json"
        assert patched.updated_state.sources["customers"].options["path"] == "/data/customers.csv"
        assert cleared.success is True
        assert tuple(cleared.updated_state.sources) == ("orders",)

    def test_on_validation_failure_accepts_sink_name(self) -> None:
        """on_validation_failure can be a sink name — not just 'discard'/'quarantine'.

        Regression guard: the tool schema must not constrain on_validation_failure
        to an enum. The runtime accepts any valid sink name for routing validation
        failures (e.g. 'bad_rows_sink'). If an enum constraint is re-added, the
        LLM cannot build source-level failsink routes.
        """
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "bad_rows_sink",
            },
            state,
            catalog,
        )
        assert result.success is True
        assert _default_source(result.updated_state) is not None
        assert _default_source(result.updated_state).on_validation_failure == "bad_rows_sink"

    def test_unknown_plugin_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.get_schema.side_effect = ValueError("Unknown plugin: foobar")
        result = execute_tool(
            "set_source",
            {
                "plugin": "foobar",
                "on_success": "t1",
                "options": {},
                "on_validation_failure": "discard",
            },
            state,
            catalog,
        )
        assert result.success is False
        assert _default_source(result.updated_state) is None  # unchanged
        assert result.updated_state.version == 1
        assert result.data is None
        assert result.validation.errors[0].error_code == "plugin_not_installed"

    def test_set_source_rejects_manual_blob_ref_in_options(self) -> None:
        """Reject manual blob_ref injection through set_source.

        set_source must not accept ``blob_ref`` in options because it
        cannot enforce that the supplied ``path`` equals the bound blob's
        canonical ``storage_path``.  The canonical write path for blob-
        backed sources is ``set_source_from_blob``, which forces the path
        to ``BlobRecord.storage_path``.  Without this rejection, a caller
        (the LLM) could persist a wrong-shape path alongside a real
        blob_ref, breaking runtime path resolution.

        Bug-verification protocol (cf.
        ``tests/integration/pipeline/test_composer_runtime_agreement.py``
        module docstring lines 76-88): manually revert the
        ``"blob_ref" in options`` check in ``_execute_set_source``
        (src/elspeth/web/composer/tools.py) and confirm this test fails
        with ``result.success is True``.  Then restore.  This guards
        against the test passing both pre-fix and post-fix — a class of
        test theatre otherwise undetectable until a future regression
        slips through.
        """
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {
                    "blob_ref": "abc123",
                    "path": "data/blobs/abc123/tickets.csv",
                    "schema": {"mode": "observed"},
                },
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert result.success is False
        assert _default_source(result.updated_state) is None
        assert "set_source_from_blob" in result.validation.errors[0].message

    def test_set_source_rejects_manual_blobs_list_in_options(self) -> None:
        """The plural blob_rows binding is resolver-owned (elspeth-0c6a343921
        review): only set_source_from_blobs may author a ``blobs`` list, with
        every entry field resolved from the session's blob records and
        LLM-authored blobs refused. A generic set_source carrying ``blobs``
        would skip that custody entirely, so the key is reserved."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {
                    "blobs": [
                        {
                            "blob_id": "0a274caf-6d51-44a4-b8ef-3d2f5f77a1f0",
                            "payload_ref": "c" * 64,
                            "filename": "doc.pdf",
                            "mime_type": "application/pdf",
                            "size_bytes": 10,
                        }
                    ],
                    "schema": {"mode": "observed"},
                },
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert result.success is False
        assert _default_source(result.updated_state) is None
        assert "set_source_from_blobs" in result.validation.errors[0].message

    def test_patch_source_options_rejects_manual_blobs_list(self) -> None:
        """Patching the authoritative blobs list directly would let the LLM
        rewrite resolver-owned custody; rebinding goes through
        set_source_from_blobs."""
        state = _empty_state()
        catalog = _mock_catalog()
        seeded = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert seeded.success is True

        result = execute_tool(
            "patch_source_options",
            {"patch": {"blobs": [{"blob_id": "0a274caf-6d51-44a4-b8ef-3d2f5f77a1f0"}]}},
            seeded.updated_state,
            catalog,
        )
        assert result.success is False
        assert "set_source_from_blobs" in result.validation.errors[0].message

    def test_set_pipeline_rejects_manual_blobs_list_in_source_options(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_pipeline",
            {
                "source": {
                    "plugin": "csv",
                    "on_success": "main",
                    "options": {
                        "blobs": [{"blob_id": "0a274caf-6d51-44a4-b8ef-3d2f5f77a1f0"}],
                        "schema": {"mode": "observed"},
                    },
                    "on_validation_failure": "quarantine",
                },
                "nodes": [],
                "edges": [],
                "outputs": [],
            },
            state,
            catalog,
        )
        assert result.success is False
        assert "set_source_from_blobs" in result.validation.errors[0].message

    def test_set_source_rejects_manual_source_authoring_in_options(self) -> None:
        """Caller-supplied source_authoring must not bypass blob provenance stamping."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {
                    "path": "/data/in.csv",
                    "schema": {"mode": "observed"},
                    SOURCE_AUTHORING_KEY: {
                        "modality": CreationModality.LLM_GENERATED.value,
                        "content_hash": "0" * 64,
                        "review_event_id": None,
                        "resolved_kind": None,
                    },
                },
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )

        assert result.success is False
        assert "source" not in result.updated_state.sources
        assert SOURCE_AUTHORING_KEY in result.validation.errors[0].message

    def test_set_source_rejects_literal_credential_value_without_mutating_state(self) -> None:
        """The outer credential boundary refuses before source mutation."""
        state = _empty_state()
        catalog = _mock_catalog()
        literal = "literal-source-key-for-test"

        with pytest.raises(CredentialMaterialRefused) as caught:
            execute_tool(
                "set_source",
                {
                    "plugin": "csv",
                    "on_success": "t1",
                    "options": {"path": "/data/in.csv", "api_key": literal},
                    "on_validation_failure": "quarantine",
                },
                state,
                catalog,
            )

        _assert_credential_material_refusal(caught.value, literal_value=literal)
        assert "source" not in state.sources


class TestVfDestinationAdvisory:
    """Structured guidance when on_validation_failure references an unknown output.

    The set_source tool schema accepts any string for on_validation_failure
    (not just 'discard'/'quarantine'). When the value doesn't match a
    configured output, validation carries the closed error once, without
    duplicating its repair target in a free-text data note.
    """

    def test_set_source_unknown_vf_sink_has_only_structured_error(self) -> None:
        """Unknown destinations retain their structured validation error."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "nonexistent",
            },
            state,
            catalog,
        )
        assert result.success is True
        assert result.data is None
        assert [entry.error_code for entry in result.validation.errors].count("quarantine_unknown_output") == 1

    def test_set_source_discard_vf_no_note(self) -> None:
        """'discard' is a built-in value — no advisory needed."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            state,
            catalog,
        )
        assert result.success is True
        assert result.data is None
        assert all(entry.error_code != "quarantine_unknown_output" for entry in result.validation.errors)

    def test_set_source_known_vf_sink_no_note(self) -> None:
        """When the named output exists, no advisory is needed."""
        state = _empty_state()
        catalog = _mock_catalog()
        # First create the output that on_validation_failure will reference.
        r1 = execute_tool(
            "set_output",
            {
                "sink_name": "quarantine",
                "plugin": "csv",
                "options": {"path": "/data/quarantine.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        assert r1.success is True
        # Now set source referencing the existing output.
        r2 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            r1.updated_state,
            catalog,
        )
        assert r2.success is True
        assert r2.data is None
        assert all(entry.error_code != "quarantine_unknown_output" for entry in r2.validation.errors)

    def test_set_pipeline_unknown_vf_sink_has_only_structured_error(self) -> None:
        """Full replacement reports the same validation error once."""
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        # Change on_validation_failure to a name that doesn't match any output.
        args["source"]["on_validation_failure"] = "typo_sink"
        result = execute_tool("set_pipeline", args, state, catalog)
        assert result.success is True
        assert result.data is None
        assert [entry.error_code for entry in result.validation.errors].count("quarantine_unknown_output") == 1

    def test_set_pipeline_vf_matches_output_no_note(self) -> None:
        """set_pipeline with on_validation_failure matching an output — no note."""
        state = _empty_state()
        catalog = _mock_catalog()
        args = _valid_pipeline_args()
        # Add a "quarantine" output and reference it from on_validation_failure.
        args["outputs"].append(
            {
                "sink_name": "quarantine",
                "plugin": "csv",
                "options": {"path": "/data/quarantine.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
        )
        args["source"]["on_validation_failure"] = "quarantine"
        result = execute_tool("set_pipeline", args, state, catalog)
        assert result.success is True
        assert result.data is None


class TestUpsertNode:
    def test_adds_new_node(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            state,
            catalog,
        )
        assert result.success is True
        assert len(result.updated_state.nodes) == 1
        assert "t1" in result.affected_nodes

    @pytest.mark.parametrize("node_type", ["gate", "coalesce"])
    def test_structural_node_with_plugin_is_rejected_atomically(self, node_type: str) -> None:
        """A plugin on a built-in structural node is a rejected mutation, not a persisted token or a crash.

        Before the guard, ``_execute_upsert_node`` accepted ``node_type='gate',
        plugin='fork'`` and the post-call hint lookup then raised an uncaught
        ``ValueError('plugin_not_enabled')`` from the catalog; ``set_pipeline``
        persisted the plugin outright with ``is_valid: True``.
        """
        state = _empty_state()
        arguments: dict[str, Any] = {"id": "structural", "node_type": node_type, "plugin": "authored_plugin_token", "input": "rows"}
        if node_type == "gate":
            arguments |= {"condition": "row['x'] > 1", "routes": {"true": "fork", "false": "main"}, "fork_to": ["main", "alt"]}
        else:
            arguments |= {"branches": ["path_a", "path_b"], "policy": "require_all", "merge": "union", "on_success": "main"}

        result = execute_tool("upsert_node", arguments, state, _mock_catalog())

        assert result.success is False
        assert result.data is None
        assert result.validation.errors[0].error_code == "structural_node_plugin_forbidden"
        assert "plugin=null" in result.validation.errors[0].message
        assert "authored_plugin_token" not in result.validation.errors[0].message, "the authored plugin token is not echoed"
        assert state.nodes == ()
        assert result.updated_state.nodes == ()

    def test_allows_llm_consuming_web_scrape_without_prompt_shield_as_advisory(self) -> None:
        state = CompositionState(
            source=None,
            nodes=(
                NodeSpec(
                    id="fetch_pages",
                    node_type="transform",
                    plugin="web_scrape",
                    input="rows",
                    on_success="scraped_rows",
                    on_error="discard",
                    options={
                        "schema": {"mode": "observed"},
                        "url_field": "url",
                        "content_field": "content",
                        "http": {
                            "abuse_contact": "ops@example.com",
                            "scraping_reason": "technical evaluation",
                            "allowed_hosts": ["example.com"],
                        },
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        catalog = _mock_catalog()

        result = execute_tool(
            "upsert_node",
            {
                "id": "summarise_pages",
                "node_type": "transform",
                "plugin": "llm",
                "input": "scraped_rows",
                "on_success": "summaries",
                "on_error": "discard",
                "options": {
                    "provider": "openrouter",
                    "model": "openai/gpt-4o",
                    "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                    "prompt_template": "Summarise {{ row.content }}.",
                    "required_input_fields": ["content"],
                    "schema": {"mode": "observed"},
                },
            },
            state,
            catalog,
        )

        # Advisory, not blocking: the missing prompt-shield surfaces as a
        # non-blocking validation warning rather than rejecting the upsert.
        assert result.success is True
        warning_text = " ".join(w.message for w in result.updated_state.validate().warnings)
        # The registry key rides the requirement's structured user_term field,
        # never the warning body (user register, elspeth-9665dcca32).
        assert PROMPT_SHIELD_USER_TERM not in warning_text
        assert "prompt-injection" in warning_text
        assert "continuing without it is allowed" in warning_text

    def test_replaces_existing_node(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result1 = execute_tool(
            "upsert_node",
            {
                "id": "t1",
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
        result2 = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "new_in",
                "on_success": "out",
                "options": {"schema": {"mode": "observed"}},
            },
            result1.updated_state,
            catalog,
        )
        assert result2.success is True
        assert len(result2.updated_state.nodes) == 1
        assert result2.updated_state.nodes[0].input == "new_in"
        assert deep_thaw(result2.updated_state.nodes[0].options) == {"schema": {"mode": "observed"}}

    def test_gate_node_no_plugin_validation(self) -> None:
        """Gates don't have plugins — should not validate against catalog."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "upsert_node",
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "in",
                "on_success": None,
                "options": {},
                "condition": "row['x'] > 0",
                "routes": {"true": "s1", "false": "s2"},
            },
            state,
            catalog,
        )
        assert result.success is True
        catalog.get_schema.assert_not_called()

    def test_gate_omitted_on_error_preserves_fail_fast_policy(self) -> None:
        result = execute_tool(
            "upsert_node",
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "rows",
                "condition": "row['amount'] > 500",
                "routes": {"true": "main", "false": "main"},
            },
            _empty_state(),
            _mock_catalog(),
        )

        assert result.success is True
        assert result.updated_state.nodes[0].on_error is None

    @pytest.mark.parametrize("on_error", ["discard", "gate_errors"])
    def test_gate_accepts_explicit_non_empty_on_error_policy(self, on_error: str) -> None:
        result = execute_tool(
            "upsert_node",
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "rows",
                "on_error": on_error,
                "condition": "row['amount'] > 500",
                "routes": {"true": "main", "false": "main"},
            },
            _empty_state(),
            _mock_catalog(),
        )

        assert result.success is True
        assert result.updated_state.nodes[0].on_error == on_error

    def test_upsert_node_unknown_transform_plugin_fails(self) -> None:
        """W-4B-1: LLM hallucinates a transform plugin name."""
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.get_schema.side_effect = ValueError("Unknown plugin: nonexistent_xyz")
        result = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "nonexistent_xyz",
                "input": "in",
                "on_success": "out",
                "on_error": "discard",
                "options": {},
            },
            state,
            catalog,
        )
        assert result.success is False
        assert result.updated_state.version == 1  # unchanged

    def test_gate_injection_condition_rejected(self) -> None:
        """upsert_node rejects gate with injection attempt in condition."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "upsert_node",
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "in",
                "on_success": None,
                "options": {},
                "condition": "__import__('os').system('rm -rf /')",
                "routes": {"true": "s1", "false": "s2"},
            },
            state,
            catalog,
        )
        assert result.success is False
        assert "Forbidden construct" in result.validation.errors[0].message
        assert result.updated_state.version == 1  # unchanged

    def test_gate_malformed_condition_rejected(self) -> None:
        """upsert_node rejects gate with syntactically invalid condition."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "upsert_node",
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "in",
                "on_success": None,
                "options": {},
                "condition": "row['x'] >== 5",
                "routes": {"true": "s1", "false": "s2"},
            },
            state,
            catalog,
        )
        assert result.success is False
        assert "Invalid gate condition syntax" in result.validation.errors[0].message
        assert result.updated_state.version == 1

    def test_gate_boolean_condition_custom_labels_rejected(self) -> None:
        """upsert_node rejects a boolean gate condition with non-true/false labels.

        Mirrors GateSettings.validate_boolean_routes at the tool boundary so the
        composer does not green-light a pipeline runtime config later rejects.
        Regression for elspeth-08e17b9253.
        """
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "upsert_node",
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "in",
                "on_success": None,
                "options": {},
                "condition": "row['x'] > 0",
                "routes": {"high": "s1", "low": "s2"},
            },
            state,
            catalog,
        )
        assert result.success is False
        assert "boolean condition" in result.validation.errors[0].message
        assert result.updated_state.version == 1

    def test_gate_numeric_condition_rejected(self) -> None:
        """upsert_node rejects a provably-numeric gate condition (never a route label).

        Regression for elspeth-08e17b9253.
        """
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "upsert_node",
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "in",
                "on_success": None,
                "options": {},
                "condition": "row['x'] + 1",
                "routes": {"a": "s1"},
            },
            state,
            catalog,
        )
        assert result.success is False
        assert "numeric value" in result.validation.errors[0].message
        assert result.updated_state.version == 1

    def test_gate_eval_call_rejected(self) -> None:
        """upsert_node rejects eval() in condition (forbidden function call)."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "upsert_node",
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "in",
                "on_success": None,
                "options": {},
                "condition": "eval('row[\"x\"]')",
                "routes": {"true": "s1", "false": "s2"},
            },
            state,
            catalog,
        )
        assert result.success is False
        assert "Forbidden construct" in result.validation.errors[0].message

    def test_gate_valid_condition_accepted(self) -> None:
        """upsert_node accepts gate with well-formed condition."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "upsert_node",
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "in",
                "on_success": None,
                "options": {},
                "condition": "row['score'] >= 0.85 and row.get('status') is not None",
                "routes": {"true": "s1", "false": "s2"},
            },
            state,
            catalog,
        )
        assert result.success is True
        assert len(result.updated_state.nodes) == 1

    def test_aggregation_end_of_source_condition_rejected(self) -> None:
        """upsert_node rejects end_of_source in the aggregation condition slot."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "upsert_node",
            {
                "id": "agg1",
                "node_type": "aggregation",
                "plugin": "batch_stats",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}, "value_field": "amount"},
                "trigger": {"condition": "end_of_source"},
            },
            state,
            catalog,
        )

        assert result.success is False
        assert "end_of_source" in result.validation.errors[0].message
        assert result.updated_state.version == 1

    def test_gate_none_condition_not_validated(self) -> None:
        """upsert_node with condition=None skips expression validation.

        Presence validation is the job of CompositionState.validate(), not
        the upsert_node handler. This test ensures we don't crash on None.
        """
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "upsert_node",
            {
                "id": "g1",
                "node_type": "gate",
                "plugin": None,
                "input": "in",
                "on_success": None,
                "options": {},
                "condition": None,
                "routes": {"true": "s1", "false": "s2"},
            },
            state,
            catalog,
        )
        # Succeeds at tool level; validate() will flag missing condition
        assert result.success is True

    def test_transform_with_condition_skips_expression_validation(self) -> None:
        """Non-gate nodes with a condition field don't trigger expression validation.

        Only gates have expressions; a transform with a stray condition field
        is a structural error caught by validate(), not an expression error.
        """
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "in",
                "on_success": "out",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
                "condition": "this is not python!!!",
            },
            state,
            catalog,
        )
        # The invalid syntax is irrelevant for a transform node — not validated
        assert result.success is True


class TestUpsertEdge:
    def _catalog_with_llm_and_json(self) -> MagicMock:
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

    def test_adds_edge(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "upsert_edge",
            {
                "id": "e1",
                "from_node": "source",
                "to_node": "t1",
                "edge_type": "on_success",
                "label": None,
            },
            state,
            catalog,
        )
        assert result.success is True
        assert len(result.updated_state.edges) == 1
        assert "source" in result.affected_nodes
        assert "t1" in result.affected_nodes

    def test_edge_to_output_syncs_node_on_success(self) -> None:
        """Edge from node to output updates node's on_success to output name."""
        state = _empty_state()
        catalog = _mock_catalog()
        # Add a node with on_success pointing elsewhere
        r1 = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "in",
                "on_success": "old_stream",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            state,
            catalog,
        )
        # Add an output
        r2 = execute_tool(
            "set_output",
            {
                "sink_name": "csv_out",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/output.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            r1.updated_state,
            catalog,
        )
        # Add edge from node to output with on_success type
        r3 = execute_tool(
            "upsert_edge",
            {"id": "e1", "from_node": "t1", "to_node": "csv_out", "edge_type": "on_success"},
            r2.updated_state,
            catalog,
        )
        assert r3.success is True
        node = next(n for n in r3.updated_state.nodes if n.id == "t1")
        assert node.on_success == "csv_out"

    def test_edge_to_output_syncs_node_on_error(self) -> None:
        """Edge from node to output with on_error updates node's on_error."""
        state = _empty_state()
        catalog = _mock_catalog()
        r1 = execute_tool(
            "upsert_node",
            {
                "id": "t1",
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
        r2 = execute_tool(
            "set_output",
            {
                "sink_name": "err_out",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/output.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            r1.updated_state,
            catalog,
        )
        r3 = execute_tool(
            "upsert_edge",
            {"id": "e1", "from_node": "t1", "to_node": "err_out", "edge_type": "on_error"},
            r2.updated_state,
            catalog,
        )
        assert r3.success is True
        node = next(n for n in r3.updated_state.nodes if n.id == "t1")
        assert node.on_error == "err_out"

    def test_gate_on_error_edge_is_rejected_with_node_level_repair(self) -> None:
        state = (
            _empty_state()
            .with_node(
                NodeSpec(
                    id="threshold",
                    node_type="gate",
                    plugin=None,
                    input="rows",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="row['amount'] > 500",
                    routes={"true": "main", "false": "main"},
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_output(OutputSpec(name="gate_errors", plugin="json", options={}, on_write_failure="discard"))
        )

        result = execute_tool(
            "upsert_edge",
            {
                "id": "e_gate_error",
                "from_node": "threshold",
                "to_node": "gate_errors",
                "edge_type": "on_error",
            },
            state,
            _mock_catalog(),
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.updated_state.nodes[0].on_error is None
        assert "upsert_node" in result.validation.errors[0].message
        assert "on_error" in result.validation.errors[0].message
        assert "route_true" not in result.validation.errors[0].message
        assert "fork" not in result.validation.errors[0].message

    def test_gate_on_error_edge_to_processing_node_is_also_rejected_atomically(self) -> None:
        gate = NodeSpec(
            id="threshold",
            node_type="gate",
            plugin=None,
            input="rows",
            on_success=None,
            on_error=None,
            options={},
            condition="row['amount'] > 500",
            routes={"true": "review_rows", "false": "review_rows"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        review = NodeSpec(
            id="review",
            node_type="transform",
            plugin="passthrough",
            input="review_rows",
            on_success="main",
            on_error="discard",
            options={"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = _empty_state().with_node(gate).with_node(review)

        result = execute_tool(
            "upsert_edge",
            {
                "id": "e_gate_error_visual",
                "from_node": "threshold",
                "to_node": "review",
                "edge_type": "on_error",
            },
            state,
            _mock_catalog(),
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.updated_state.edges == state.edges
        assert "upsert_node" in result.validation.errors[0].message
        assert "on_error" in result.validation.errors[0].message

    def test_upsert_edge_adds_llm_failure_sink_on_error_without_rebuilding_pipeline(self) -> None:
        """An existing LLM node can be routed to a failure sink via upsert_edge."""
        state = _empty_state()
        catalog = self._catalog_with_llm_and_json()
        with_node = execute_tool(
            "upsert_node",
            {
                "id": "judge_layers",
                "node_type": "transform",
                "plugin": "llm",
                "input": "source_out",
                "on_success": "judged_layers",
                "on_error": "discard",
                "options": _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"}),
            },
            state,
            catalog,
        )
        assert with_node.success is True
        with_failure_sink = execute_tool(
            "set_output",
            {
                "sink_name": "llm_failures",
                "plugin": "json",
                "options": {
                    "path": "/data/outputs/test-session/magic_comp_rules_layers_failures.json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
            with_node.updated_state,
            catalog,
            data_dir="/data",
        )
        assert with_failure_sink.success is True

        result = execute_tool(
            "upsert_edge",
            {
                "id": "e_judge_layers_error",
                "from_node": "judge_layers",
                "to_node": "llm_failures",
                "edge_type": "on_error",
                "label": "LLM failures",
            },
            with_failure_sink.updated_state,
            catalog,
        )

        assert result.success is True
        assert result.updated_state.nodes[0].on_error == "llm_failures"
        assert result.updated_state.nodes[0].options == with_failure_sink.updated_state.nodes[0].options
        assert result.updated_state.edges[-1].edge_type == "on_error"

    @pytest.mark.parametrize(
        ("edge_type", "expected_routes"),
        [
            ("route_true", {"true": "main", "false": "old_false"}),
            ("route_false", {"true": "old_true", "false": "main"}),
        ],
    )
    def test_gate_route_edge_to_output_syncs_gate_routes(
        self,
        edge_type: str,
        expected_routes: dict[str, str],
    ) -> None:
        """Gate route edges to sinks must update the gate's runtime routes."""
        state = _empty_state().with_node(
            NodeSpec(
                id="g1",
                node_type="gate",
                plugin=None,
                input="in",
                on_success=None,
                on_error=None,
                options={},
                condition="row['flag']",
                routes={"true": "old_true", "false": "old_false"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        catalog = _mock_catalog()
        with_output = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )

        result = execute_tool(
            "upsert_edge",
            {"id": "e1", "from_node": "g1", "to_node": "main", "edge_type": edge_type},
            with_output.updated_state,
            catalog,
        )

        assert result.success is True
        gate = next(n for n in result.updated_state.nodes if n.id == "g1")
        assert dict(gate.routes or {}) == expected_routes

    def test_gate_fork_edge_to_output_syncs_gate_fork_to(self) -> None:
        """Fork edges to sinks must update the gate's runtime fork destinations."""
        state = _empty_state().with_node(
            NodeSpec(
                id="g1",
                node_type="gate",
                plugin=None,
                input="in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("existing",),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        catalog = _mock_catalog()
        with_output = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )

        result = execute_tool(
            "upsert_edge",
            {"id": "e1", "from_node": "g1", "to_node": "main", "edge_type": "fork"},
            with_output.updated_state,
            catalog,
        )

        assert result.success is True
        gate = next(n for n in result.updated_state.nodes if n.id == "g1")
        assert gate.fork_to == ("existing", "main")

    def test_route_edge_from_transform_to_output_is_rejected(self) -> None:
        """Only gates can use route_true/route_false/fork sink edges."""
        state = _empty_state()
        catalog = _mock_catalog()
        with_node = execute_tool(
            "upsert_node",
            {
                "id": "t1",
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
        with_output = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            with_node.updated_state,
            catalog,
        )

        result = execute_tool(
            "upsert_edge",
            {"id": "e1", "from_node": "t1", "to_node": "main", "edge_type": "route_true"},
            with_output.updated_state,
            catalog,
        )

        assert result.success is False
        assert "gate" in result.validation.errors[0].message.lower()

    def test_edge_to_output_syncs_source_on_success(self) -> None:
        """Edge from source to output updates source's on_success.

        set_source is promoted to a Pydantic argument model with
        extra='forbid'; all four required fields must be supplied
        (Task 4).
        """
        state = _empty_state()
        catalog = _mock_catalog()
        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "old_stream",
                "options": {"path": "/data/blobs/test-session/input.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            state,
            catalog,
        )
        r2 = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/output.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            r1.updated_state,
            catalog,
        )
        r3 = execute_tool(
            "upsert_edge",
            {"id": "e1", "from_node": "source", "to_node": "main", "edge_type": "on_success"},
            r2.updated_state,
            catalog,
        )
        assert r3.success is True
        assert _default_source(r3.updated_state) is not None
        assert _default_source(r3.updated_state).on_success == "main"

    def test_edge_to_node_does_not_sync(self) -> None:
        """Edge from node to another node does NOT change connection fields."""
        state = _empty_state()
        catalog = _mock_catalog()
        r1 = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "in",
                "on_success": "stream_a",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            state,
            catalog,
        )
        r2 = execute_tool(
            "upsert_node",
            {
                "id": "t2",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "stream_a",
                "on_success": "out",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            r1.updated_state,
            catalog,
        )
        r3 = execute_tool(
            "upsert_edge",
            {"id": "e1", "from_node": "t1", "to_node": "t2", "edge_type": "on_success"},
            r2.updated_state,
            catalog,
        )
        assert r3.success is True
        node = next(n for n in r3.updated_state.nodes if n.id == "t1")
        assert node.on_success == "stream_a"  # unchanged

    def test_edge_to_output_already_matching_is_noop(self) -> None:
        """Edge to output where on_success already matches does not double-bump version."""
        state = _empty_state()
        catalog = _mock_catalog()
        r1 = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "in",
                "on_success": "csv_out",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            state,
            catalog,
        )
        r2 = execute_tool(
            "set_output",
            {
                "sink_name": "csv_out",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/output.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            r1.updated_state,
            catalog,
        )
        v_before = r2.updated_state.version
        r3 = execute_tool(
            "upsert_edge",
            {"id": "e1", "from_node": "t1", "to_node": "csv_out", "edge_type": "on_success"},
            r2.updated_state,
            catalog,
        )
        assert r3.success is True
        node = next(n for n in r3.updated_state.nodes if n.id == "t1")
        assert node.on_success == "csv_out"
        # with_edge bumps version once; with_node should NOT be called
        assert r3.updated_state.version == v_before + 1


class TestRemoveNode:
    def test_removes_node_and_edges(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        # Add a node and an edge to it
        r1 = execute_tool(
            "upsert_node",
            {
                "id": "t1",
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
        r2 = execute_tool(
            "upsert_edge",
            {
                "id": "e1",
                "from_node": "source",
                "to_node": "t1",
                "edge_type": "on_success",
                "label": None,
            },
            r1.updated_state,
            catalog,
        )
        # Remove the node — edge should also be removed
        r3 = execute_tool("remove_node", {"id": "t1"}, r2.updated_state, catalog)
        assert r3.success is True
        assert len(r3.updated_state.nodes) == 0
        assert len(r3.updated_state.edges) == 0

    def test_remove_nonexistent_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("remove_node", {"id": "nope"}, state, catalog)
        assert result.success is False
        assert result.updated_state.version == 1  # unchanged


class TestRemoveEdge:
    def test_removes_edge(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        r1 = execute_tool(
            "upsert_edge",
            {
                "id": "e1",
                "from_node": "source",
                "to_node": "t1",
                "edge_type": "on_success",
                "label": None,
            },
            state,
            catalog,
        )
        r2 = execute_tool("remove_edge", {"id": "e1"}, r1.updated_state, catalog)
        assert r2.success is True
        assert len(r2.updated_state.edges) == 0

    @pytest.mark.parametrize(
        ("edge_type", "route_of"),
        [
            pytest.param("on_success", lambda node: node.on_success, id="on_success"),
            pytest.param("on_error", lambda node: node.on_error, id="on_error"),
        ],
    )
    def test_remove_sink_edge_clears_node_runtime_route(
        self,
        edge_type: str,
        route_of: Callable[[NodeSpec], str | None],
    ) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        with_node = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "in",
                "on_success": "normal_rows",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            state,
            catalog,
        )
        with_output = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            with_node.updated_state,
            catalog,
        )
        routed = execute_tool(
            "upsert_edge",
            {"id": "e1", "from_node": "t1", "to_node": "main", "edge_type": edge_type},
            with_output.updated_state,
            catalog,
        )
        assert routed.success is True
        assert route_of(next(n for n in routed.updated_state.nodes if n.id == "t1")) == "main"

        result = execute_tool("remove_edge", {"id": "e1"}, routed.updated_state, catalog)

        assert result.success is True
        assert len(result.updated_state.edges) == 0
        assert route_of(next(n for n in result.updated_state.nodes if n.id == "t1")) is None

    def test_remove_sink_edge_clears_source_runtime_route(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        with_source = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"path": "/data/blobs/test-session/input.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            state,
            catalog,
        )
        with_output = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            with_source.updated_state,
            catalog,
        )
        routed = execute_tool(
            "upsert_edge",
            {"id": "e1", "from_node": "source", "to_node": "main", "edge_type": "on_success"},
            with_output.updated_state,
            catalog,
        )
        assert routed.success is True
        assert _default_source(routed.updated_state).on_success == "main"

        result = execute_tool("remove_edge", {"id": "e1"}, routed.updated_state, catalog)

        assert result.success is True
        assert len(result.updated_state.edges) == 0
        assert _default_source(result.updated_state).on_success == "discard"

    def test_remove_gate_sink_edges_clears_route_and_fork_runtime_routes(self) -> None:
        state = _empty_state().with_node(
            NodeSpec(
                id="g1",
                node_type="gate",
                plugin=None,
                input="in",
                on_success=None,
                on_error=None,
                options={},
                condition="row['flag']",
                routes={"true": "old_true", "false": "old_false"},
                fork_to=("existing",),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        catalog = _mock_catalog()
        with_output = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        with_route = execute_tool(
            "upsert_edge",
            {"id": "route_edge", "from_node": "g1", "to_node": "main", "edge_type": "route_true"},
            with_output.updated_state,
            catalog,
        )
        with_fork = execute_tool(
            "upsert_edge",
            {"id": "fork_edge", "from_node": "g1", "to_node": "main", "edge_type": "fork"},
            with_route.updated_state,
            catalog,
        )
        gate = next(n for n in with_fork.updated_state.nodes if n.id == "g1")
        assert dict(gate.routes or {}) == {"true": "main", "false": "old_false"}
        assert gate.fork_to == ("existing", "main")

        without_route = execute_tool("remove_edge", {"id": "route_edge"}, with_fork.updated_state, catalog)
        without_fork = execute_tool("remove_edge", {"id": "fork_edge"}, without_route.updated_state, catalog)

        assert without_fork.success is True
        assert len(without_fork.updated_state.edges) == 0
        gate = next(n for n in without_fork.updated_state.nodes if n.id == "g1")
        assert dict(gate.routes or {}) == {"false": "old_false"}
        assert gate.fork_to == ("existing",)

    def test_remove_nonexistent_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("remove_edge", {"id": "nope"}, state, catalog)
        assert result.success is False


class TestSetMetadata:
    def test_partial_update(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_metadata",
            {"patch": {"name": "My Pipeline"}},
            state,
            catalog,
        )
        assert result.success is True
        assert result.updated_state.metadata.name == "My Pipeline"
        assert result.updated_state.metadata.description == ""  # preserved
        assert result.affected_nodes == ()  # metadata doesn't affect nodes

    def test_missing_patch_key_raises(self) -> None:
        """LLM omits required 'patch' key — route as Tier-3 argument error."""
        from elspeth.web.composer.protocol import ToolArgumentError

        state = _empty_state()
        catalog = _mock_catalog()
        with pytest.raises(ToolArgumentError):
            execute_tool("set_metadata", {"name": "Oops"}, state, catalog)


class TestLegacyMutationArgumentGuards:
    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("upsert_node", {}),
            ("upsert_edge", {}),
            ("remove_node", {}),
            ("remove_edge", {}),
            ("set_output", {}),
            ("remove_output", {}),
        ],
    )
    def test_missing_required_fields_raise_tool_argument_error(self, tool_name: str, arguments: dict[str, Any]) -> None:
        from elspeth.web.composer.protocol import ToolArgumentError

        with pytest.raises(ToolArgumentError) as exc_info:
            execute_tool(tool_name, arguments, _empty_state(), _mock_catalog())

        assert isinstance(exc_info.value.__cause__, PydanticValidationError)

    def test_upsert_node_rejects_unknown_node_type_before_state_mutation(self) -> None:
        from elspeth.web.composer.protocol import ToolArgumentError

        with pytest.raises(ToolArgumentError) as exc_info:
            execute_tool(
                "upsert_node",
                {"id": "x", "node_type": "bogus", "input": "in"},
                _empty_state(),
                _mock_catalog(),
            )

        assert isinstance(exc_info.value.__cause__, PydanticValidationError)

    def test_upsert_node_rejects_non_object_options_as_argument_error(self) -> None:
        # Pins the control that makes NodeSpec.options' declared
        # ``Mapping[str, Any]`` a real guarantee rather than an annotation:
        # ``_UpsertNodeArgumentsModel.options`` is ``dict[str, Any]``, so a
        # non-object options payload is rejected at the Tier-3 argument
        # boundary and never reaches NodeSpec construction. Consumers of
        # ``NodeSpec.options`` (e.g. ``_backend_surface_args_for_site``)
        # therefore read it nominally, with no isinstance shape guard.
        from elspeth.web.composer.protocol import ToolArgumentError

        with pytest.raises(ToolArgumentError) as exc_info:
            execute_tool(
                "upsert_node",
                {"id": "x", "node_type": "transform", "input": "in", "options": ["not", "an", "object"]},
                _empty_state(),
                _mock_catalog(),
            )

        assert isinstance(exc_info.value.__cause__, PydanticValidationError)

    def test_upsert_gate_rejects_non_string_condition_as_argument_error(self) -> None:
        from elspeth.web.composer.protocol import ToolArgumentError

        with pytest.raises(ToolArgumentError) as exc_info:
            execute_tool(
                "upsert_node",
                {"id": "gate1", "node_type": "gate", "input": "rows", "condition": 42},
                _empty_state(),
                _mock_catalog(),
            )

        assert isinstance(exc_info.value.__cause__, PydanticValidationError)

    def test_upsert_gate_rejects_empty_on_error_before_state_mutation(self) -> None:
        from elspeth.web.composer.protocol import ToolArgumentError

        state = _empty_state()
        with pytest.raises(ToolArgumentError) as exc_info:
            execute_tool(
                "upsert_node",
                {
                    "id": "gate1",
                    "node_type": "gate",
                    "input": "rows",
                    "on_error": "",
                    "condition": "row['amount'] > 500",
                    "routes": {"true": "main", "false": "main"},
                },
                state,
                _mock_catalog(),
            )

        assert isinstance(exc_info.value.__cause__, PydanticValidationError)
        assert state.nodes == ()


class TestSetOutput:
    def test_adds_output(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        assert result.success is True
        assert len(result.updated_state.outputs) == 1
        assert result.updated_state.outputs[0].name == "main"
        assert result.updated_state.outputs[0].plugin == "csv"
        assert result.updated_state.version == 2
        assert "main" in result.affected_nodes

    def test_set_output_rejects_interpretation_requirements_atomically(self) -> None:
        state = _empty_state()
        result = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {
                    "path": "/data/out.csv",
                    "schema": {"mode": "observed"},
                    INTERPRETATION_REQUIREMENTS_KEY: [],
                },
                "on_write_failure": "discard",
            },
            state,
            _mock_catalog(),
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.data is None
        assert INTERPRETATION_REQUIREMENTS_KEY in result.validation.errors[0].message

    def test_data_dir_file_sink_requires_collision_policy(self) -> None:
        """Runnable web-composer file sinks must make output collision behavior explicit."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            state,
            catalog,
            data_dir="/data",
        )

        assert result.success is False
        assert "collision_policy" in result.validation.errors[0].message

    def test_data_dir_file_sink_accepts_explicit_collision_policy(self) -> None:
        """The composer accepts file sinks once the LLM chooses the collision policy."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {
                    "path": "/data/outputs/test-session/out.csv",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
            state,
            catalog,
            data_dir="/data",
        )

        assert result.success is True
        assert result.updated_state.outputs[0].options["collision_policy"] == "auto_increment"

    def test_replaces_existing_output(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        r1 = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        r2 = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/new.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "quarantine",
            },
            r1.updated_state,
            catalog,
        )
        assert r2.success is True
        assert len(r2.updated_state.outputs) == 1
        assert r2.updated_state.outputs[0].on_write_failure == "quarantine"

    def test_on_write_failure_accepts_sink_name_for_failsink_routing(self) -> None:
        """on_write_failure can be a sink name — not just 'discard'/'quarantine'.

        Regression guard: the tool schema must not constrain on_write_failure to
        an enum. The skill document instructs LLMs to set it to a sink name (e.g.
        'results_failures') to wire automatic failsink pipelines. If an enum
        constraint is re-added, the LLM cannot build failsink routes.
        """
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "results_failures",
            },
            state,
            catalog,
        )
        assert result.success is True
        assert result.updated_state.outputs[0].on_write_failure == "results_failures"

    def test_unknown_sink_plugin_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.get_schema.side_effect = ValueError("Unknown plugin: foobar")
        result = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "foobar",
                "options": {},
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        assert result.success is False
        assert result.updated_state.version == 1  # unchanged

    def test_set_output_rejects_secret_ref_in_non_credential_field(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()

        result = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {
                    "path": {"secret_ref": "ANY_SECRET"},
                    "schema": {"mode": "observed"},
                },
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )

        assert result.success is False
        assert result.updated_state is state
        assert "csv" in result.validation.errors[0].message
        assert "path" in result.validation.errors[0].message
        assert "ANY_SECRET" in result.validation.errors[0].message
        assert "only credential-bearing fields" in result.validation.errors[0].message

    def test_set_output_rejects_literal_database_url_credentials(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        literal_url = "postgresql://db.example.invalid/elspeth"

        result = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "database",
                "options": {
                    "url": literal_url,
                    "table": "events",
                    "schema": {"mode": "observed"},
                },
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.data is not None
        assert "main:url" in result.data["credential_fields"]
        assert literal_url not in repr(result.to_dict())
        assert "secret_ref" in result.validation.errors[0].message


class TestRemoveOutput:
    def test_removes_output(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        r1 = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/output.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        r2 = execute_tool("remove_output", {"sink_name": "main"}, r1.updated_state, catalog)
        assert r2.success is True
        assert len(r2.updated_state.outputs) == 0

    def test_remove_nonexistent_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("remove_output", {"sink_name": "nope"}, state, catalog)
        assert result.success is False
        assert result.updated_state.version == 1  # unchanged


class TestSetSourcePathSecurity:
    """S2: Source paths must stay inside the caller's blob subtree."""

    def test_path_under_blobs_succeeds(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/blobs/test-session/input.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is True

    def test_path_outside_blobs_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/etc/passwd"},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is False
        assert "path" in result.validation.errors[0].message.lower()

    def test_traversal_attack_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/blobs/../../etc/passwd"},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is False

    def test_file_key_also_validated(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"file": "/tmp/evil.csv"},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is False

    def test_file_key_traversal_via_blobs_prefix_fails(self) -> None:
        """W-4B-2: file key traversal starting from blobs prefix."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"file": "/data/blobs/../../etc/passwd"},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is False

    def test_no_path_key_skips_s2_but_fails_prevalidation(self) -> None:
        """Source options without path/file keys are not subject to S2 path security.

        S2 only checks path/file keys for directory traversal — absent keys are not
        rejected. However, pre-validation (Pydantic) correctly rejects the call because
        csv source requires 'path'. The failure comes from pre-validation, not S2.
        """
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
            data_dir="/data",
        )
        # Pydantic catches the missing required 'path' field
        assert result.success is False
        # Error is from pre-validation (path required), not S2 (traversal / allowed dir)
        assert "path" in result.validation.errors[0].message
        assert "traversal" not in result.validation.errors[0].message.lower()
        assert "allowed" not in result.validation.errors[0].message.lower()

    def test_relative_path_resolves_against_data_dir(self) -> None:
        """blobs/input.csv should resolve under {data_dir}/blobs/."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "blobs/test-session/input.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is True

    def test_relative_traversal_still_blocked(self) -> None:
        """../etc/passwd relative to data_dir must still be blocked."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "../etc/passwd"},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is False

    def test_other_session_blob_path_fails(self, tmp_path: Path) -> None:
        victim_path = tmp_path / "blobs" / "victim-session" / "input.csv"
        victim_path.parent.mkdir(parents=True)
        victim_path.write_text("value\nsecret\n", encoding="utf-8")

        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": str(victim_path), "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            _empty_state(),
            _mock_catalog(),
            data_dir=str(tmp_path),
            session_id="attacker-session",
        )

        assert result.success is False
        assert "path" in result.validation.errors[0].message.lower()

    def test_symlink_from_own_blob_subtree_to_other_session_fails(self, tmp_path: Path) -> None:
        own_root = tmp_path / "blobs" / "attacker-session"
        victim_root = tmp_path / "blobs" / "victim-session"
        own_root.mkdir(parents=True)
        victim_root.mkdir(parents=True)
        (victim_root / "input.csv").write_text("value\nsecret\n", encoding="utf-8")
        (own_root / "foreign-link").symlink_to(victim_root, target_is_directory=True)

        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": str(own_root / "foreign-link" / "input.csv"), "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            _empty_state(),
            _mock_catalog(),
            data_dir=str(tmp_path),
            session_id="attacker-session",
        )

        assert result.success is False


class TestMergePatch:
    def test_merge_patch_overwrites(self) -> None:
        result = _apply_merge_patch({"a": 1}, {"a": 2})
        assert result == {"a": 2}

    def test_merge_patch_adds(self) -> None:
        result = _apply_merge_patch({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_merge_patch_deletes_null(self) -> None:
        result = _apply_merge_patch({"a": 1, "b": 2}, {"b": None})
        assert result == {"a": 1}
        assert "b" not in result

    def test_merge_patch_null_for_absent_key_is_noop(self) -> None:
        # Delete-if-present semantics: setting an absent key to None must be a
        # silent no-op, never a KeyError. This pins the edge case that
        # distinguishes the delete-if-present idiom from an unguarded ``del``.
        result = _apply_merge_patch({"a": 1}, {"b": None})
        assert result == {"a": 1}
        assert "b" not in result

    def test_merge_patch_preserves_unmentioned(self) -> None:
        result = _apply_merge_patch({"a": 1, "b": 2}, {"a": 3})
        assert result == {"a": 3, "b": 2}

    def test_merge_patch_empty_patch(self) -> None:
        result = _apply_merge_patch({"a": 1}, {})
        assert result == {"a": 1}

    def test_merge_patch_does_not_mutate_target(self) -> None:
        proxy = MappingProxyType({"a": 1})
        result = _apply_merge_patch(proxy, {"a": 2})
        # Original proxy is unchanged
        assert proxy["a"] == 1
        assert result == {"a": 2}


class TestPatchSourceOptions:
    def _state_with_source(self, options: dict[str, Any]) -> CompositionState:
        state = _empty_state()
        catalog = _mock_catalog()
        merged = {"schema": {"mode": "observed"}, **options}
        r = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": merged,
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert r.success is True
        return r.updated_state

    def test_patch_source_options_updates_key(self) -> None:
        state = self._state_with_source({"path": "/a"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"path": "/b"}},
            state,
            catalog,
        )
        assert result.success is True
        assert _default_source(result.updated_state) is not None

        opts = deep_thaw(_default_source(result.updated_state).options)
        assert opts["path"] == "/b"

    def test_patch_source_options_adds_key(self) -> None:
        state = self._state_with_source({"path": "/a"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"encoding": "utf-8"}},
            state,
            catalog,
        )
        assert result.success is True

        assert _default_source(result.updated_state) is not None
        opts = deep_thaw(_default_source(result.updated_state).options)
        assert opts["path"] == "/a"
        assert opts["encoding"] == "utf-8"

    def test_patch_source_options_rejects_literal_credential_value_without_mutating_state(self) -> None:
        state = self._state_with_source({"path": "/a"})
        before = state.to_dict()
        catalog = _mock_catalog()
        literal = "literal-source-key-for-test"

        with pytest.raises(CredentialMaterialRefused) as caught:
            execute_tool(
                "patch_source_options",
                {"patch": {"api_key": literal}},
                state,
                catalog,
            )

        _assert_credential_material_refusal(caught.value, literal_value=literal)
        assert state.to_dict() == before

    def test_patch_source_options_deletes_key(self) -> None:
        state = self._state_with_source({"path": "/a", "encoding": "utf-8"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"encoding": None}},
            state,
            catalog,
        )
        assert result.success is True

        assert _default_source(result.updated_state) is not None
        opts = deep_thaw(_default_source(result.updated_state).options)
        assert opts["path"] == "/a"
        assert "encoding" not in opts

    def test_patch_source_options_no_source_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"path": "/b"}},
            state,
            catalog,
        )
        assert result.success is False
        assert "No source" in result.validation.errors[0].message
        assert result.updated_state.version == 1

    def _blob_backed_state(self) -> CompositionState:
        """Build a state with a blob-backed source directly.

        Bypasses ``set_source`` (which now rejects manual ``blob_ref``
        injection) by constructing the
        ``CompositionState`` and ``SourceSpec`` from the dataclass
        primitives.  This is the post-fix shape produced by
        ``set_source_from_blob`` at runtime.
        """
        return CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="t1",
                options={
                    "blob_ref": "abc123",
                    "path": "/canon/abc123_x.csv",
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="quarantine",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def test_patch_source_options_rejects_path_patch_on_blob_backed_source(self) -> None:
        """Reject path patches on a blob-backed source.

        Once a source is bound to a blob via ``set_source_from_blob``, the
        ``path`` is the blob's canonical ``storage_path`` and is not
        patchable.  Allowing a ``path`` patch lets the LLM persist a path
        that disagrees with the bound blob, breaking runtime path
        resolution and composer/runtime agreement.  Re-binding to a
        different blob requires ``set_source_from_blob`` (or
        ``clear_source`` first), not a path patch.

        Bug-verification protocol (cf.
        ``tests/integration/pipeline/test_composer_runtime_agreement.py``
        module docstring lines 76-88): manually revert the
        ``if "blob_ref" in state.source.options:`` block in
        ``_execute_patch_source_options`` (src/elspeth/web/composer/tools.py)
        and confirm this test fails with ``result.success is True``.
        Then restore.
        """
        state = self._blob_backed_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"path": "/canon/abc123_x.csv"}},
            state,
            catalog,
        )
        assert result.success is False
        assert "blob-backed source" in result.validation.errors[0].message

    def test_patch_source_options_rejects_blob_ref_patch_on_plain_source(self) -> None:
        """Manual blob identity injection is rejected even before a source is blob-backed."""
        state = self._state_with_source({"path": "/a.csv"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"blob_ref": "def456"}},
            state,
            catalog,
        )
        assert result.success is False
        assert "Cannot patch 'blob_ref'" in result.validation.errors[0].message
        assert _default_source(result.updated_state) is not None
        opts = deep_thaw(_default_source(result.updated_state).options)
        assert "blob_ref" not in opts
        assert opts["path"] == "/a.csv"

    def test_patch_source_options_rejects_blob_ref_patch_on_blob_backed_source(self) -> None:
        """Reject blob_ref re-binding through a patch.

        ``blob_ref`` is part of the immutable binding; replacing it via a
        patch would silently re-target the source to a different blob
        without re-deriving the canonical ``storage_path``.  Re-binding
        requires ``set_source_from_blob``.
        """
        state = self._blob_backed_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"blob_ref": "def456"}},
            state,
            catalog,
        )
        assert result.success is False
        assert "Cannot patch 'blob_ref'" in result.validation.errors[0].message

    def test_patch_source_options_allows_unrelated_keys_on_blob_backed_source(self) -> None:
        """Allow unrelated option patches on a blob-backed source.

        The (path, blob_ref) lock must not over-reach: patches that touch
        only schema/encoding/etc. on a blob-backed source remain valid,
        because they don't break the blob binding.  Without this the
        defense-in-depth would block legitimate plugin-option tuning.
        """
        state = self._blob_backed_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"encoding": "utf-8"}},
            state,
            catalog,
        )
        assert result.success is True
        assert _default_source(result.updated_state) is not None
        opts = deep_thaw(_default_source(result.updated_state).options)
        assert opts["encoding"] == "utf-8"
        assert opts["path"] == "/canon/abc123_x.csv"
        assert opts["blob_ref"] == "abc123"

    def _acknowledged_blob_backed_state(self) -> CompositionState:
        """Post-acknowledge shape: uploaded source carrying the user's stamp.

        Built from dataclass primitives because the authoring tools
        (correctly) refuse to write this stamp — only the
        source_data_contract resolve arm produces it.
        """
        return CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="t1",
                options={
                    "blob_ref": "abc123",
                    "path": "/canon/abc123_x.csv",
                    "schema": {"mode": "observed", "guaranteed_fields": ["colour"]},
                },
                on_validation_failure="quarantine",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def test_patch_source_options_rejects_planner_guarantee_stamp_on_uploaded_source(self) -> None:
        """The evidence-class ruling (2026-08-27): an uploaded source's header
        is a sample; its observed-mode ``guaranteed_fields`` stamp is the
        user's own recorded promise, written server-side when the
        source_data_contract review is acknowledged. A planner patch writing
        it directly silently extinguishes the ask-the-user demand
        (elspeth-1dddcfee3a) — and landing after
        ``request_interpretation_review`` it kills the review site under a
        persisted pending card (elspeth-d73139155a)."""
        state = self._blob_backed_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"schema": {"mode": "observed", "guaranteed_fields": ["colour", "extra"]}}},
            state,
            catalog,
        )
        assert result.success is False
        assert "guaranteed_fields" in result.validation.errors[0].message
        assert "request_interpretation_review" in result.validation.errors[0].message
        assert _default_source(result.updated_state) is not None
        opts = deep_thaw(_default_source(result.updated_state).options)
        assert "guaranteed_fields" not in opts["schema"]

    def test_patch_source_options_rejects_planner_guarantee_stamp_on_plain_source(self) -> None:
        """A plain path-bound source is rebindable content — same sample
        evidence class as an upload, same rejection."""
        state = self._state_with_source({"path": "/a.csv"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"schema": {"guaranteed_fields": ["colour"]}}},
            state,
            catalog,
        )
        assert result.success is False
        assert "guaranteed_fields" in result.validation.errors[0].message

    def test_patch_source_options_allows_echoed_guarantee_stamp(self) -> None:
        """A patch echoing the stored stamp verbatim asserts nothing new
        (read-modify-write over serialized state) and must pass."""
        state = self._acknowledged_blob_backed_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"schema": {"mode": "observed", "guaranteed_fields": ["colour"]}, "encoding": "utf-8"}},
            state,
            catalog,
        )
        assert result.success is True
        opts = deep_thaw(_default_source(result.updated_state).options)
        assert opts["schema"]["guaranteed_fields"] == ["colour"]
        assert opts["encoding"] == "utf-8"

    def test_patch_source_options_rejects_changed_guarantee_stamp_over_stored(self) -> None:
        """Widening the user's acknowledged stamp is authoring, not an echo."""
        state = self._acknowledged_blob_backed_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"schema": {"guaranteed_fields": ["colour", "extra"]}}},
            state,
            catalog,
        )
        assert result.success is False
        assert "guaranteed_fields" in result.validation.errors[0].message
        opts = deep_thaw(_default_source(result.updated_state).options)
        assert opts["schema"]["guaranteed_fields"] == ["colour"]

    def test_patch_source_options_allows_guarantee_stamp_on_llm_authored_source(self) -> None:
        """An LLM-authored blob's bytes are content-hash-bound: the author's
        schema claim stands (the same lane ``_options_with_derived_guarantees``
        serves), so the guard must not over-reach."""
        authored_csv = "colour\nred\n"
        authored_hash = sha256(authored_csv.encode("utf-8")).hexdigest()
        authored_options = _options_with_source_blob_review(
            {
                "blob_ref": "abc123",
                "path": "/canon/abc123_x.csv",
                "schema": {"mode": "observed"},
                SOURCE_AUTHORING_KEY: {
                    "modality": "llm_generated",
                    "content_hash": authored_hash,
                    "review_event_id": None,
                    "resolved_kind": None,
                },
            },
            mime_type="text/csv",
            content=authored_csv,
        )
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="t1",
                options=authored_options,
                on_validation_failure="quarantine",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_source_options",
            {"patch": {"schema": {"mode": "observed", "guaranteed_fields": ["colour"]}}},
            state,
            catalog,
        )
        assert result.success is True, result.validation.errors
        opts = deep_thaw(_default_source(result.updated_state).options)
        assert opts["schema"]["guaranteed_fields"] == ["colour"]
        assert opts[SOURCE_AUTHORING_KEY]["content_hash"] == authored_hash
        (review,) = opts[INTERPRETATION_REQUIREMENTS_KEY]
        assert review["status"] == "pending"
        assert review["event_id"] is None
        assert review["draft"] == authored_csv


class TestPatchNodeOptions:
    def _state_with_node(self, options: dict[str, Any]) -> CompositionState:
        state = _empty_state()
        catalog = _mock_catalog()
        r = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": options,
            },
            state,
            catalog,
        )
        assert r.success is True
        return r.updated_state

    def test_patch_node_options_updates_key(self) -> None:
        state = self._state_with_node({"schema": {"mode": "observed"}, "required_input_fields": ["old"]})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_node_options",
            {"node_id": "t1", "patch": {"required_input_fields": ["new"]}},
            state,
            catalog,
        )
        assert result.success is True
        node = result.updated_state.nodes[0]
        assert node.id == "t1"

        opts = deep_thaw(node.options)
        assert opts["required_input_fields"] == ["new"]
        # Other node fields preserved
        assert node.node_type == "transform"
        assert node.plugin == "passthrough"

    def test_patch_node_options_rejects_literal_credential_value_without_mutating_state(self) -> None:
        state = self._state_with_node({"schema": {"mode": "observed"}})
        before = state.to_dict()
        catalog = _mock_catalog()
        literal = "literal-node-key-for-test"

        with pytest.raises(CredentialMaterialRefused) as caught:
            execute_tool(
                "patch_node_options",
                {"node_id": "t1", "patch": {"api_key": literal}},
                state,
                catalog,
            )

        _assert_credential_material_refusal(caught.value, literal_value=literal)
        assert state.to_dict() == before

    def test_patch_node_options_rejects_on_error_with_routing_tool_guidance(self) -> None:
        """on_error is a node routing field, not a plugin option patch."""
        state = _empty_state()
        catalog = _mock_catalog()
        with_node = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            state,
            catalog,
        )
        assert with_node.success is True

        result = execute_tool(
            "patch_node_options",
            {"node_id": "t1", "patch": {"on_error": "llm_failures"}},
            with_node.updated_state,
            catalog,
        )

        assert result.success is False
        assert result.updated_state is with_node.updated_state
        assert result.updated_state.nodes[0].on_error == "discard"
        assert "on_error is a node-level routing field" in result.validation.errors[0].message
        assert "upsert_edge" in result.validation.errors[0].message
        assert "edge_type='on_error'" in result.validation.errors[0].message
        assert "Extra inputs are not permitted" not in result.validation.errors[0].message

    def test_patch_gate_options_rejects_on_error_with_node_level_guidance(self) -> None:
        state = _empty_state().with_node(
            NodeSpec(
                id="threshold",
                node_type="gate",
                plugin=None,
                input="rows",
                on_success=None,
                on_error=None,
                options={},
                condition="row['amount'] > 500",
                routes={"true": "main", "false": "main"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )

        result = execute_tool(
            "patch_node_options",
            {"node_id": "threshold", "patch": {"on_error": "gate_errors"}},
            state,
            _mock_catalog(),
        )

        assert result.success is False
        assert result.updated_state is state
        assert "upsert_node" in result.validation.errors[0].message
        assert "upsert_edge" not in result.validation.errors[0].message

    def test_patch_node_options_unknown_node_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_node_options",
            {"node_id": "nonexistent", "patch": {"field": "value"}},
            state,
            catalog,
        )
        assert result.success is False
        assert "nonexistent" in result.validation.errors[0].message


class TestPatchOutputOptions:
    def _state_with_output(self, options: dict[str, Any]) -> CompositionState:
        state = _empty_state()
        catalog = _mock_catalog()
        merged = {"schema": {"mode": "observed"}, **options}
        r = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": merged,
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        assert r.success is True
        return r.updated_state

    def test_patch_output_options_updates_key(self) -> None:
        state = self._state_with_output({"path": "/old.csv"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_output_options",
            {"sink_name": "main", "patch": {"path": "/new.csv"}},
            state,
            catalog,
        )
        assert result.success is True
        output = result.updated_state.outputs[0]

        opts = deep_thaw(output.options)
        assert opts["path"] == "/new.csv"

    def test_patch_output_options_rejects_interpretation_requirements_atomically(self) -> None:
        state = self._state_with_output({"path": "/old.csv"})
        result = execute_tool(
            "patch_output_options",
            {"sink_name": "main", "patch": {INTERPRETATION_REQUIREMENTS_KEY: []}},
            state,
            _mock_catalog(),
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.data is None
        assert INTERPRETATION_REQUIREMENTS_KEY in result.validation.errors[0].message

    def test_patch_output_options_rejects_literal_credential_value_without_mutating_state(self) -> None:
        state = self._state_with_output({"path": "/old.csv"})
        before = state.to_dict()
        catalog = _mock_catalog()
        literal = "literal-output-password-for-test"

        with pytest.raises(CredentialMaterialRefused) as caught:
            execute_tool(
                "patch_output_options",
                {"sink_name": "main", "patch": {"password": literal}},
                state,
                catalog,
            )

        _assert_credential_material_refusal(caught.value, literal_value=literal)
        assert state.to_dict() == before

    def test_patch_output_options_unknown_sink_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_output_options",
            {"sink_name": "nonexistent", "patch": {"path": "/x.csv"}},
            state,
            catalog,
        )
        assert result.success is False
        assert "nonexistent" in result.validation.errors[0].message


class TestPatchOutputPathSecurity:
    """S2: Sink path allowlist — patched output paths must be under allowed directories.

    Mirrors TestSetSourcePathSecurity but for the sink/output side.
    _validate_sink_path() must be called after merge-patching output options.
    """

    def _state_with_output(self, options: dict[str, Any]) -> CompositionState:
        state = _empty_state()
        catalog = _mock_catalog()
        merged = {"schema": {"mode": "observed"}, **options}
        r = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": merged,
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        assert r.success is True
        return r.updated_state

    def test_path_outside_allowlist_rejected(self) -> None:
        state = self._state_with_output({"path": "/data/outputs/test-session/ok.csv"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_output_options",
            {"sink_name": "main", "patch": {"path": "/etc/passwd"}},
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is False
        assert "path" in result.validation.errors[0].message.lower()

    def test_traversal_attack_rejected(self) -> None:
        state = self._state_with_output({"path": "/data/outputs/test-session/ok.csv"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_output_options",
            {"sink_name": "main", "patch": {"path": "/data/outputs/test-session/../../etc/passwd"}},
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is False

    def test_file_key_also_validated(self) -> None:
        state = self._state_with_output({"path": "/data/outputs/test-session/ok.csv"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_output_options",
            {"sink_name": "main", "patch": {"file": "/tmp/evil.csv"}},
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is False

    def test_persist_directory_key_also_validated(self) -> None:
        from elspeth.web.composer.tools._common import _validate_sink_path

        error = _validate_sink_path({"persist_directory": "/tmp/elspeth-outside"}, data_dir="/data", session_id=None)
        assert error is not None
        assert "persist_directory" in error

    def test_sink_path_in_own_session_blobs_allowed(self) -> None:
        """elspeth-bdc17cfdb1: a sink may write into its own session's blob subtree."""
        from elspeth.web.composer.tools._common import _validate_sink_path

        error = _validate_sink_path({"path": "/data/blobs/sess-a/out.json"}, data_dir="/data", session_id="sess-a")
        assert error is None

    def test_sink_path_in_other_session_blobs_rejected(self) -> None:
        """elspeth-bdc17cfdb1: cross-session blob writes are the defect this guard closes."""
        from elspeth.web.composer.tools._common import _validate_sink_path

        error = _validate_sink_path({"path": "/data/blobs/sess-b/out.json"}, data_dir="/data", session_id="sess-a")
        assert error is not None
        assert "path" in error

    def test_sink_path_in_blobs_without_session_rejected(self) -> None:
        """No session identity fails closed: no blob access at all."""
        from elspeth.web.composer.tools._common import _validate_sink_path

        error = _validate_sink_path({"path": "/data/blobs/sess-a/out.json"}, data_dir="/data", session_id=None)
        assert error is not None

    def test_append_sink_cannot_adopt_other_session_output(self, tmp_path: Path) -> None:
        victim_path = tmp_path / "outputs" / "victim-session" / "shared.csv"
        victim_path.parent.mkdir(parents=True)
        victim_path.write_text("value\nsecret\n", encoding="utf-8")

        result = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {
                    "path": str(victim_path),
                    "schema": {"mode": "observed"},
                    "mode": "append",
                    "collision_policy": "append_or_create",
                },
                "on_write_failure": "discard",
            },
            _empty_state(),
            _mock_catalog(),
            data_dir=str(tmp_path),
            session_id="attacker-session",
        )

        assert result.success is False
        assert "path" in result.validation.errors[0].message.lower()

    def test_sink_symlink_to_other_session_output_rejected(self, tmp_path: Path) -> None:
        from elspeth.web.composer.tools._common import _validate_sink_path

        own_root = tmp_path / "outputs" / "attacker-session"
        victim_root = tmp_path / "outputs" / "victim-session"
        own_root.mkdir(parents=True)
        victim_root.mkdir(parents=True)
        (own_root / "foreign-link").symlink_to(victim_root, target_is_directory=True)

        error = _validate_sink_path(
            {"path": str(own_root / "foreign-link" / "out.csv")},
            data_dir=str(tmp_path),
            session_id="attacker-session",
        )

        assert error is not None

    def test_file_key_traversal_rejected(self) -> None:
        state = self._state_with_output({"path": "/data/outputs/test-session/ok.csv"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_output_options",
            {"sink_name": "main", "patch": {"file": "/data/outputs/test-session/../../etc/shadow"}},
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is False

    def test_relative_path_under_outputs_accepted(self) -> None:
        state = self._state_with_output({"path": "/data/outputs/test-session/ok.csv"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_output_options",
            {
                "sink_name": "main",
                "patch": {
                    "path": "outputs/result.csv",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is True

    def test_absolute_path_under_allowed_dir_accepted(self) -> None:
        state = self._state_with_output({"path": "/data/outputs/test-session/ok.csv"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_output_options",
            {
                "sink_name": "main",
                "patch": {
                    "path": "/data/outputs/test-session/subdir/out.csv",
                    "mode": "write",
                    "collision_policy": "fail_if_exists",
                },
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is True

    def test_data_dir_none_skips_validation(self) -> None:
        """When data_dir is not configured, any path is accepted."""
        state = self._state_with_output({"path": "/anywhere/file.csv"})
        catalog = _mock_catalog()
        result = execute_tool(
            "patch_output_options",
            {"sink_name": "main", "patch": {"path": "/etc/passwd"}},
            state,
            catalog,
            data_dir=None,
        )
        assert result.success is True


class TestTransformProviderConfigPathSecurity:
    """S2: nested transform provider_config persist_directory must be confined.

    RAG retrieval transforms carry a local Chroma persist_directory under
    options.provider_config. Mirrors the sink-path guard: confined to the
    allowed output directories.
    """

    @staticmethod
    def _catalog_with_rag() -> MagicMock:
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="rag_retrieval",
                description="RAG retrieval transform",
                plugin_type="transform",
                config_fields=[],
            ),
        ]
        return catalog

    @staticmethod
    def _rag_options(persist_directory: str) -> dict[str, Any]:
        return {
            "provider": "chroma",
            "provider_config": {"persist_directory": persist_directory, "collection": "docs"},
            "schema": {"mode": "observed"},
            "output_prefix": "rag_",
            "query_field": "text",
        }

    def test_helper_rejects_persist_directory_outside_allowed(self) -> None:
        from elspeth.web.composer.tools._common import _validate_transform_provider_config_path

        error = _validate_transform_provider_config_path(
            {"provider": "chroma", "provider_config": {"persist_directory": "/tmp/elspeth-outside"}},
            data_dir="/data",
            session_id=None,
        )
        assert error is not None
        assert "persist_directory" in error

    def test_helper_allows_persist_directory_under_outputs(self) -> None:
        from elspeth.web.composer.tools._common import _validate_transform_provider_config_path

        error = _validate_transform_provider_config_path(
            {"provider": "chroma", "provider_config": {"persist_directory": "/data/outputs/test-session/chroma"}},
            data_dir="/data",
            session_id="test-session",
        )
        assert error is None

    def test_helper_allows_persist_directory_in_own_session_blobs(self) -> None:
        """elspeth-bdc17cfdb1: persist_directory may live in the caller's own blob subtree."""
        from elspeth.web.composer.tools._common import _validate_transform_provider_config_path

        error = _validate_transform_provider_config_path(
            {"provider": "chroma", "provider_config": {"persist_directory": "/data/blobs/sess-a/chroma"}},
            data_dir="/data",
            session_id="sess-a",
        )
        assert error is None

    def test_helper_rejects_persist_directory_in_other_session_blobs(self) -> None:
        """elspeth-bdc17cfdb1: persist_directory is a read/write target — pointing it
        at another session's blob subtree would disclose that session's data."""
        from elspeth.web.composer.tools._common import _validate_transform_provider_config_path

        error = _validate_transform_provider_config_path(
            {"provider": "chroma", "provider_config": {"persist_directory": "/data/blobs/sess-b/chroma"}},
            data_dir="/data",
            session_id="sess-a",
        )
        assert error is not None
        assert "persist_directory" in error

    def test_helper_rejects_persist_directory_in_other_session_outputs(self) -> None:
        from elspeth.web.composer.tools._common import _validate_transform_provider_config_path

        error = _validate_transform_provider_config_path(
            {
                "provider": "chroma",
                "provider_config": {"persist_directory": "/data/outputs/victim-session/chroma"},
            },
            data_dir="/data",
            session_id="attacker-session",
        )

        assert error is not None
        assert "persist_directory" in error

    def test_helper_skips_non_rag_transform(self) -> None:
        from elspeth.web.composer.tools._common import _validate_transform_provider_config_path

        error = _validate_transform_provider_config_path({"some_field": "value"}, data_dir="/data", session_id=None)
        assert error is None

    def test_helper_skips_null_persist_directory(self) -> None:
        # A null nested path value must be skipped cleanly (no TypeError from
        # Path(None)) — parity with the runtime siblings (service/validation),
        # which guard with ``value is not None`` before resolving.
        from elspeth.web.composer.tools._common import _validate_transform_provider_config_path

        error = _validate_transform_provider_config_path(
            {"provider": "chroma", "provider_config": {"persist_directory": None, "collection": "docs"}},
            data_dir="/data",
            session_id=None,
        )
        assert error is None

    @staticmethod
    def _azure_ai_search_managed_identity_node(*, input_name: str, on_success: str) -> dict[str, Any]:
        return {
            "id": "rag",
            "node_type": "transform",
            "plugin": "azure_ai_search",
            "input": input_name,
            "on_success": on_success,
            "on_error": "discard",
            "options": {
                "endpoint": "https://tenant-b.search.windows.net",
                "index": "payroll",
                "use_managed_identity": True,
                "schema": {"mode": "observed"},
                "output_prefix": "rag_",
                "query_field": "text",
            },
        }

    @staticmethod
    def _azure_ai_search_without_a_profile() -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]:
        # What build_plugin_snapshot records for an operator-profiled plugin with no usable alias.
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="azure_ai_search",
                description="Azure AI Search RAG retrieval transform",
                plugin_type="transform",
                config_fields=[],
            ),
        ]
        return _restricted_policy_pair(
            catalog,
            PluginId("transform", "azure_ai_search"),
            PluginUnavailableReason.PROFILE_UNAVAILABLE,
        )

    def test_upsert_node_rejects_raw_azure_ai_search_managed_identity(self) -> None:
        state = _empty_state()
        view, snapshot = self._azure_ai_search_without_a_profile()
        result = execute_tool(
            "upsert_node",
            self._azure_ai_search_managed_identity_node(input_name="rows", on_success="retrieved"),
            state,
            view,
            data_dir="/data",
            plugin_snapshot=snapshot,
        )
        assert result.success is False
        assert result.updated_state is state
        assert result.validation.errors[0].error_code == "profile_unavailable"

    def test_patch_node_options_cannot_repoint_rag_retrieval_at_azure_search(self) -> None:
        # rag_retrieval used to reach Azure through provider="azure_search"; that arm is gone,
        # so a patch can no longer move an admitted Chroma node onto the server's identity.
        state = _empty_state()
        catalog = self._catalog_with_rag()
        created = execute_tool(
            "upsert_node",
            {
                "id": "rag",
                "node_type": "transform",
                "plugin": "rag_retrieval",
                "input": "rows",
                "on_success": "retrieved",
                "on_error": "discard",
                "options": self._rag_options("/data/outputs/test-session/chroma"),
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert created.success is True

        result = execute_tool(
            "patch_node_options",
            {
                "node_id": "rag",
                "patch": {
                    "provider": "azure_search",
                    "provider_config": {
                        "endpoint": "https://tenant-b.search.windows.net",
                        "index": "payroll",
                        "use_managed_identity": True,
                    },
                },
            },
            created.updated_state,
            catalog,
            data_dir="/data",
        )

        assert result.success is False
        assert result.updated_state is created.updated_state
        assert "provider: Input should be 'chroma'" in result.validation.errors[0].message

    def test_set_pipeline_rejects_raw_azure_ai_search_managed_identity(self) -> None:
        state = _empty_state()
        view, snapshot = self._azure_ai_search_without_a_profile()
        args = {
            "source": {
                "plugin": "csv",
                "on_success": "source_out",
                "options": {"path": "/data/blobs/test-session/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            "nodes": [self._azure_ai_search_managed_identity_node(input_name="source_out", on_success="main")],
            "edges": [{"id": "e1", "from_node": "source", "to_node": "rag", "edge_type": "on_success", "label": None}],
            "outputs": [
                {
                    "sink_name": "main",
                    "plugin": "csv",
                    "options": {
                        "path": "/data/outputs/test-session/out.csv",
                        "schema": {"mode": "observed"},
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                }
            ],
        }
        result = execute_tool("set_pipeline", args, state, view, data_dir="/data", plugin_snapshot=snapshot)
        assert result.success is False
        assert result.updated_state is state
        assert result.validation.errors[0].error_code == "profile_unavailable"

    @staticmethod
    def _azure_ai_search_profiled_view() -> PolicyCatalogView:
        """A REAL policy view: production snapshot builder, real registry, one managed-identity profile."""
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from elspeth.web.plugin_policy.availability import build_plugin_snapshot
        from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy

        class _NoSecrets:
            def has_server_ref(self, name: str) -> bool:
                return False

            def has_user_ref(self, principal: str, name: str) -> bool:
                return False

            def has_ref(self, principal: str, name: str) -> bool:
                return False

            def server_generation(self, name: str) -> str | None:
                return None

            def user_generation(self, principal: str, name: str) -> str | None:
                return None

        settings = WebSettings.model_validate(
            {
                "composer_max_composition_turns": 4,
                "composer_max_discovery_turns": 4,
                "composer_timeout_seconds": 60,
                "composer_rate_limit_per_minute": 20,
                "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",
                "plugin_allowlist": ["source:csv", "sink:csv", "transform:azure_ai_search"],
                "azure_search_profiles": [
                    {
                        "alias": "policies",
                        "endpoint": "https://operator-private-marker.search.windows.net",
                        "auth": "managed_identity",
                        "indexes": ["approved-documents"],
                    }
                ],
            }
        )
        runtime = RuntimeWebPluginConfig.from_settings(settings)
        policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
        profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
        catalog = create_catalog_service()
        snapshot = build_plugin_snapshot(
            policy=policy,
            catalog=catalog,
            profiles=profiles,
            principal_scope="local:test-user",
            secret_inventory=_NoSecrets(),
            generation_key=b"azure-search-tool-layer-proof",
        )
        return PolicyCatalogView(catalog, snapshot, profiles)

    @staticmethod
    def _profiled_azure_ai_search_node(**extra: Any) -> dict[str, Any]:
        return {
            "id": "rag",
            "node_type": "transform",
            "plugin": "azure_ai_search",
            "input": "rows",
            "on_success": "retrieved",
            "on_error": "discard",
            "options": {
                "profile": "policies",
                "index": "approved-documents",
                "query_field": "text",
                "output_prefix": "rag",
                "schema": {"mode": "observed"},
                **extra,
            },
        }

    def test_upsert_node_admits_a_profiled_azure_ai_search_node(self) -> None:
        """Control for the refusals below: the same call without a private option succeeds."""
        view = self._azure_ai_search_profiled_view()
        result = execute_tool(
            "upsert_node", self._profiled_azure_ai_search_node(), _empty_state(), view, data_dir="/data", plugin_snapshot=view.snapshot
        )

        assert result.success is True
        stored = dict(result.updated_state.nodes[0].options)
        assert stored["profile"] == "policies"
        assert "endpoint" not in stored
        assert "use_managed_identity" not in stored

    @pytest.mark.parametrize(
        ("option", "value"),
        [
            ("endpoint", "https://evil.example.com"),
            ("use_managed_identity", True),
            ("client_id", "11111111-2222-3333-4444-555555555555"),
            ("api_version", "2020-06-30"),
        ],
    )
    def test_upsert_node_refuses_a_private_option_beside_a_profile(self, option: str, value: object) -> None:
        from elspeth.contracts.azure_ai_search import AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES

        assert option in AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES
        view = self._azure_ai_search_profiled_view()
        state = _empty_state()

        result = execute_tool(
            "upsert_node",
            self._profiled_azure_ai_search_node(**{option: value}),
            state,
            view,
            data_dir="/data",
            plugin_snapshot=view.snapshot,
        )

        assert result.success is False
        assert result.updated_state is state
        rendered = " ".join(entry.message for entry in result.validation.errors)
        assert option in rendered
        assert "evil.example.com" not in rendered
        assert "stolen" not in rendered

    def test_upsert_node_refuses_an_api_key_before_profile_private_option_validation(self) -> None:
        view = self._azure_ai_search_profiled_view()
        state = _empty_state()
        literal = "stolen"

        with pytest.raises(CredentialMaterialRefused) as caught:
            execute_tool(
                "upsert_node",
                self._profiled_azure_ai_search_node(api_key=literal),
                state,
                view,
                data_dir="/data",
                plugin_snapshot=view.snapshot,
            )

        _assert_credential_material_refusal(caught.value, literal_value=literal)
        assert state.nodes == ()

    def test_the_private_option_cases_above_cover_the_whole_private_set(self) -> None:
        from elspeth.contracts.azure_ai_search import AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES

        assert (
            frozenset({"endpoint", "use_managed_identity", "client_id", "api_key", "api_version"})
            == AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES
        )

    def test_upsert_node_refuses_a_raw_azure_ai_search_node_when_a_profile_exists(self) -> None:
        view = self._azure_ai_search_profiled_view()
        state = _empty_state()
        node = self._azure_ai_search_managed_identity_node(input_name="rows", on_success="retrieved")

        result = execute_tool("upsert_node", node, state, view, data_dir="/data", plugin_snapshot=view.snapshot)

        assert result.success is False
        assert result.updated_state is state

    def test_patch_node_options_cannot_add_a_private_option_to_a_profiled_node(self) -> None:
        view = self._azure_ai_search_profiled_view()
        created = execute_tool(
            "upsert_node", self._profiled_azure_ai_search_node(), _empty_state(), view, data_dir="/data", plugin_snapshot=view.snapshot
        )
        assert created.success is True

        result = execute_tool(
            "patch_node_options",
            {"node_id": "rag", "patch": {"endpoint": "https://evil.example.com"}},
            created.updated_state,
            view,
            data_dir="/data",
            plugin_snapshot=view.snapshot,
        )

        assert result.success is False
        assert result.updated_state is created.updated_state

    def test_upsert_node_rejects_persist_directory_outside_allowed(self) -> None:
        state = _empty_state()
        catalog = self._catalog_with_rag()
        result = execute_tool(
            "upsert_node",
            {
                "id": "rag",
                "node_type": "transform",
                "plugin": "rag_retrieval",
                "input": "rows",
                "on_success": "retrieved",
                "on_error": "discard",
                "options": self._rag_options("/etc/cron.d/backdoor"),
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is False
        assert "persist_directory" in result.validation.errors[0].message

    def test_upsert_node_accepts_persist_directory_under_outputs(self) -> None:
        state = _empty_state()
        catalog = self._catalog_with_rag()
        result = execute_tool(
            "upsert_node",
            {
                "id": "rag",
                "node_type": "transform",
                "plugin": "rag_retrieval",
                "input": "rows",
                "on_success": "retrieved",
                "on_error": "discard",
                "options": self._rag_options("/data/outputs/test-session/chroma"),
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert result.success is True

    def test_patch_node_options_rejects_persist_directory_outside_allowed(self) -> None:
        state = _empty_state()
        catalog = self._catalog_with_rag()
        created = execute_tool(
            "upsert_node",
            {
                "id": "rag",
                "node_type": "transform",
                "plugin": "rag_retrieval",
                "input": "rows",
                "on_success": "retrieved",
                "on_error": "discard",
                "options": self._rag_options("/data/outputs/test-session/chroma"),
            },
            state,
            catalog,
            data_dir="/data",
        )
        assert created.success is True
        result = execute_tool(
            "patch_node_options",
            {
                "node_id": "rag",
                "patch": {"provider_config": {"persist_directory": "/etc/passwd", "collection": "docs"}},
            },
            created.updated_state,
            catalog,
            data_dir="/data",
        )
        assert result.success is False
        assert "persist_directory" in result.validation.errors[0].message

    def test_set_pipeline_rejects_persist_directory_outside_allowed(self) -> None:
        """Parity: a bulk set_pipeline must reject an escaping transform path,
        not just escaping sink paths."""
        state = _empty_state()
        catalog = self._catalog_with_rag()
        args = {
            "source": {
                "plugin": "csv",
                "on_success": "source_out",
                "options": {"path": "/data/blobs/test-session/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            "nodes": [
                {
                    "id": "rag",
                    "node_type": "transform",
                    "plugin": "rag_retrieval",
                    "input": "source_out",
                    "on_success": "main",
                    "on_error": "discard",
                    "options": self._rag_options("/etc/cron.d/backdoor"),
                }
            ],
            "edges": [{"id": "e1", "from_node": "source", "to_node": "rag", "edge_type": "on_success", "label": None}],
            "outputs": [
                {
                    "sink_name": "main",
                    "plugin": "csv",
                    "options": {"path": "/data/outputs/test-session/out.csv", "schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            ],
        }
        result = execute_tool("set_pipeline", args, state, catalog, data_dir="/data")
        assert result.success is False
        assert "persist_directory" in result.validation.errors[0].message


class TestTransformLlmRetryBudgetPolicy:
    """Composer must not persist unsafe sequential multi-query LLM retry budgets."""

    @staticmethod
    def _catalog_with_llm() -> MagicMock:
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
        return catalog

    @staticmethod
    def _llm_multi_query_options(**overrides: Any) -> dict[str, Any]:
        options = _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"})
        options.update(
            {
                "prompt_template": "Classify {{ row.text }}.",
                "required_input_fields": [],
                "queries": [
                    {
                        "name": "classify",
                        "input_fields": {"text": "body"},
                    }
                ],
            }
        )
        options.update(overrides)
        return options

    def test_helper_rejects_default_sequential_multi_query_retry_budget(self) -> None:
        from elspeth.web.composer.tools._common import _validate_transform_provider_config_policy

        error = _validate_transform_provider_config_policy(
            self._llm_multi_query_options(),
            plugin="llm",
        )
        assert error is not None
        assert "sequential multi-query llm" in error.lower()

    def test_helper_allows_small_sequential_multi_query_retry_budget(self) -> None:
        from elspeth.web.composer.tools._common import _validate_transform_provider_config_policy

        error = _validate_transform_provider_config_policy(
            self._llm_multi_query_options(max_capacity_retry_seconds=30.0),
            plugin="llm",
        )
        assert error is None

    def test_helper_allows_pooled_multi_query_default_retry_budget(self) -> None:
        from elspeth.web.composer.tools._common import _validate_transform_provider_config_policy

        error = _validate_transform_provider_config_policy(
            self._llm_multi_query_options(pool_size="2.0"),
            plugin="llm",
        )
        assert error is None

    def test_helper_rejects_fractional_multi_query_pool_size(self) -> None:
        from elspeth.web.composer.tools._common import _validate_transform_provider_config_policy

        error = _validate_transform_provider_config_policy(
            self._llm_multi_query_options(pool_size="2.5"),
            plugin="llm",
        )
        assert error is not None
        assert "sequential multi-query llm" in error.lower()

    def test_upsert_node_rejects_default_sequential_multi_query_retry_budget(self) -> None:
        result = execute_tool(
            "upsert_node",
            {
                "id": "llm_review",
                "node_type": "transform",
                "plugin": "llm",
                "input": "rows",
                "on_success": "reviewed",
                "on_error": "discard",
                "options": self._llm_multi_query_options(),
            },
            _empty_state(),
            self._catalog_with_llm(),
            data_dir="/data",
        )
        assert result.success is False
        assert "sequential multi-query llm" in result.validation.errors[0].message.lower()

    def test_patch_node_options_rejects_oversized_sequential_multi_query_retry_budget(self) -> None:
        catalog = self._catalog_with_llm()
        created = execute_tool(
            "upsert_node",
            {
                "id": "llm_review",
                "node_type": "transform",
                "plugin": "llm",
                "input": "rows",
                "on_success": "reviewed",
                "on_error": "discard",
                "options": self._llm_multi_query_options(max_capacity_retry_seconds=30),
            },
            _empty_state(),
            catalog,
            data_dir="/data",
        )
        assert created.success is True

        result = execute_tool(
            "patch_node_options",
            {
                "node_id": "llm_review",
                "patch": {"max_capacity_retry_seconds": 31},
            },
            created.updated_state,
            catalog,
            data_dir="/data",
        )
        assert result.success is False
        assert "sequential multi-query llm" in result.validation.errors[0].message.lower()

    def test_set_pipeline_rejects_default_sequential_multi_query_retry_budget(self) -> None:
        args = {
            "source": {
                "plugin": "csv",
                "on_success": "source_out",
                "options": {"path": "/data/blobs/test-session/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            "nodes": [
                {
                    "id": "llm_review",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "source_out",
                    "on_success": "main",
                    "on_error": "discard",
                    "options": self._llm_multi_query_options(),
                }
            ],
            "edges": [{"id": "e1", "from_node": "source", "to_node": "llm_review", "edge_type": "on_success", "label": None}],
            "outputs": [
                {
                    "sink_name": "main",
                    "plugin": "csv",
                    "options": {
                        "path": "/data/outputs/test-session/out.csv",
                        "schema": {"mode": "observed"},
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                }
            ],
        }
        result = execute_tool("set_pipeline", args, _empty_state(), self._catalog_with_llm(), data_dir="/data")
        assert result.success is False
        assert "sequential multi-query llm" in result.validation.errors[0].message.lower()


class TestCredentialRejectionAdvertisesInlineForm:
    """Lock the credential-rejection error contract for elspeth-85ae8972b0.

    The cheap composer model (gpt-5.x-mini class) cannot create a new node
    that has a required credential field because:

    - ``set_pipeline`` is atomic, so a node with ``api_key`` missing fails
      pydantic validation and the whole mutation rolls back.
    - ``wire_secret_ref`` requires the node to already exist in state.
    - A literal credential value is rejected by this helper.

    The escape hatch is to pass ``{secret_ref: NAME}`` *inline* as the
    value of the credential field — supported by ``secrets.py`` and
    stripped before pydantic validation in tools.py — but the rejection
    message historically only advertised the ``wire_secret_ref`` tool
    sequence (which is unusable for new nodes). This locks in the
    inline-form-first messaging.
    """

    def test_error_message_contains_inline_form(self) -> None:
        state = _empty_state()
        result = _credential_wiring_contract_failure(
            state,
            component_id="my_llm",
            component_type="transform",
            options={"api_key": "literal-secret"},
        )
        assert result is not None
        assert result.success is False
        error = result.validation.errors[0].message
        # Inline form must appear (and appear before the post-hoc form).
        assert "secret_ref" in error
        assert "{secret_ref: NAME}" in error or "{secret_ref:" in error
        # Inline form must be the lead — operator should see it before
        # the post-hoc tool sequence.
        inline_idx = error.find("set_pipeline")
        post_hoc_idx = error.find("list_secret_refs -> validate_secret_ref -> wire_secret_ref")
        assert inline_idx != -1, "inline form must reference set_pipeline / upsert_node"
        assert post_hoc_idx != -1, "post-hoc form must remain documented"
        assert inline_idx < post_hoc_idx, (
            "Inline form must lead — model reads top-down and the wire_secret_ref path is unusable for new nodes."
        )

    def test_repair_payload_splits_inline_and_post_hoc(self) -> None:
        state = _empty_state()
        result = _credential_wiring_contract_failure(
            state,
            component_id="my_llm",
            component_type="transform",
            options={"api_key": "literal-secret"},
        )
        assert result is not None
        repair = result.data["repair"]
        assert set(result.to_dict()["data"]) == {"credential_fields", "components", "repair"}
        assert result.validation.errors[0].component == "rejected_mutation"
        # Old key must be gone — no compatibility shim (CONTRIBUTING.md §Code Standards).
        assert "required_tool_sequence" not in repair
        # New shape: two separately-keyed forms.
        assert "inline_form" in repair
        assert "post_hoc_form" in repair
        inline = repair["inline_form"]
        post_hoc = repair["post_hoc_form"]
        # Inline form carries an example the model can copy verbatim.
        assert "instruction" in inline
        assert "example_options" in inline
        assert inline["example_options"] == {"api_key": {"secret_ref": "<NAME>"}}
        # Post-hoc form carries the tool sequence.
        assert "instruction" in post_hoc
        assert tuple(post_hoc["tool_sequence"]) == (
            "list_secret_refs",
            "validate_secret_ref",
            "wire_secret_ref",
        )

    def test_post_hoc_repair_instruction_still_present(self) -> None:
        """The internal repair result still documents the post-hoc sequence."""
        state = _empty_state()
        result = _credential_wiring_contract_failure(
            state,
            component_id="my_llm",
            component_type="transform",
            options={"api_key": "literal-secret"},
        )
        assert result is not None
        assert "list_secret_refs -> validate_secret_ref -> wire_secret_ref" in result.validation.errors[0].message

    def test_multiple_fields_listed_with_single_inline_example_form(self) -> None:
        """When multiple credential fields are violated, the example_options
        payload should enumerate all of them so the model sees the inline
        form for each one."""
        state = _empty_state()
        result = _credential_wiring_contract_failure(
            state,
            component_id="db",
            component_type="sink",
            options={"api_key": "literal-1", "password": "literal-2"},
        )
        assert result is not None
        example = result.data["repair"]["inline_form"]["example_options"]
        assert set(example.keys()) == {"api_key", "password"}
        assert all(v == {"secret_ref": "<NAME>"} for v in example.values())


class TestFailedMutationVersionStable:
    """Failed mutations must not advance the version counter."""

    def test_failed_mutation_preserves_version(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        # remove_node with nonexistent ID fails
        result = execute_tool(
            "remove_node",
            {"id": "nonexistent_node"},
            state,
            catalog,
        )
        assert result.success is False
        # Version must not advance on failure
        assert result.updated_state.version == state.version


class TestServiceMissingArgToolArgumentError:
    """Tool raises ToolArgumentError on missing required argument.

    Post Task 4 (set_source manifest promotion), missing required
    arguments are caught at the Tier-3 boundary by
    :class:`SetSourceArgumentsModel`; the handler re-raises
    :class:`pydantic.ValidationError` as :class:`ToolArgumentError` so
    the compose loop's ARG_ERROR routing at ``service.py:2480`` receives
    the right exception class.  A bare ``KeyError`` (the prior shape) is
    a plugin-bug indicator and would be routed to ``PLUGIN_CRASH`` —
    that disposition is wrong for Tier-3 input.
    """

    def test_missing_required_arg_raises_tool_argument_error(self) -> None:
        from elspeth.web.composer.protocol import ToolArgumentError

        state = _empty_state()
        catalog = _mock_catalog()
        # set_source requires "plugin" — omitting it must raise
        # ToolArgumentError (no longer KeyError) per Task 4.
        with pytest.raises(ToolArgumentError):
            execute_tool("set_source", {}, state, catalog)


class TestClearSource:
    def test_clear_source_removes_source(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        # First set a source
        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/blobs/test-session/input.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert _default_source(r1.updated_state) is not None
        # Now clear it
        r2 = execute_tool("clear_source", {}, r1.updated_state, catalog)
        assert r2.success is True
        assert _default_source(r2.updated_state) is None
        assert r2.updated_state.version == 3

    def test_clear_source_no_source_fails(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("clear_source", {}, state, catalog)
        assert result.success is False
        assert "No source" in result.validation.errors[0].message
