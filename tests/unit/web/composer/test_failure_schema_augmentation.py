"""Tests for inline ``plugin_schemas`` augmentation on failed option-shape mutations.

When a mutation tool (``set_pipeline``, ``upsert_node``, ``splice_transform``,
``set_source``, ``set_output``, ``patch_*_options``, ``set_source_from_blob``,
``set_source_from_blobs``) returns ``success=False`` with at least one
validation error whose producer stamped ``ValidationEntry.plugin_identity``,
the response must embed the full ``get_plugin_schema`` payload for every
stamped plugin under a top-level ``plugin_schemas`` field. Eliminates the
second LLM round-trip the model would otherwise burn calling
``get_plugin_schema`` separately after each rejection (composer session
47cfbb5e on staging: 13 tool calls / 18 LLM rounds for a 4-plugin pipeline
because the model never preloaded any schema).

The identity is read STRUCTURALLY from the entry, never parsed from the
message (elspeth-f60d638661): the messages quote model-authored option
values, option keys, and component names, and each was enough to plant a
plugin identity that no validator had admitted — ``catalog.get_schema`` on
such a name raised out of ``execute_tool`` as a model-triggerable 500.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.protocol import CatalogService, PluginKind
from elspeth.web.catalog.schemas import (
    ConfigFieldSummary,
    PluginSchemaInfo,
    PluginSecretRequirement,
    PluginSummary,
)
from elspeth.web.composer.state import (
    CompositionState,
    PipelineMetadata,
)
from elspeth.web.composer.tools import _common as common_tools
from elspeth.web.composer.tools import _execute_create_blob, _execute_set_source_from_blob
from elspeth.web.composer.tools import execute_tool as _strict_execute_tool
from elspeth.web.composer.tools import outputs as outputs_tools
from elspeth.web.composer.tools import sessions as sessions_tools
from elspeth.web.composer.tools import sources as sources_tools
from elspeth.web.composer.tools import transforms as transforms_tools
from elspeth.web.composer.tools._common import ToolContext, build_plugin_schemas_for_failure
from elspeth.web.composer.tools.sources import _execute_set_source_from_blobs
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import (
    PluginAvailability,
    PluginAvailabilitySnapshot,
    PluginId,
    PluginUnavailableReason,
)
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry

from .test_promote_set_source_from_blob import _session_engine_with_user_message
from .test_set_source_from_blobs import _PNG, _create_ready_blob


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    state: CompositionState,
    catalog: CatalogService,
    **kwargs: Any,
) -> Any:
    if kwargs.get("data_dir") is not None and "session_id" not in kwargs:
        kwargs["session_id"] = "test-session"
    supplied_snapshot = kwargs.pop("plugin_snapshot", None)
    if isinstance(catalog, PolicyCatalogView):
        if not isinstance(supplied_snapshot, PluginAvailabilitySnapshot):
            raise AssertionError("policy catalog requires matching snapshot")
        snapshot = supplied_snapshot
        view = catalog
    else:
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        view = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    return _strict_execute_tool(
        tool_name,
        arguments,
        state,
        view,
        plugin_snapshot=snapshot,
        **kwargs,
    )


class _CatalogWithSchemas:
    def __init__(
        self,
        *,
        source_schemas: dict[str, PluginSchemaInfo] | None = None,
        transform_schemas: dict[str, PluginSchemaInfo] | None = None,
        sink_schemas: dict[str, PluginSchemaInfo] | None = None,
        schema_error: Exception | None = None,
    ) -> None:
        self._source_schemas = source_schemas or {}
        self._transform_schemas = transform_schemas or {}
        self._sink_schemas = sink_schemas or {}
        self._schema_error = schema_error

    def list_sources(self) -> list[PluginSummary]:
        return [
            PluginSummary(
                name=name,
                description=f"{name} source",
                plugin_type="source",
                config_fields=[
                    ConfigFieldSummary(name="path", type="string", required=True, description="File path", default=None),
                ],
            )
            for name in (self._source_schemas or {"csv": None})
        ]

    def list_transforms(self) -> list[PluginSummary]:
        return [
            PluginSummary(
                name=name,
                description=f"{name} transform",
                plugin_type="transform",
                config_fields=[],
            )
            for name in (self._transform_schemas or {"passthrough": None})
        ]

    def list_sinks(self) -> list[PluginSummary]:
        return [
            PluginSummary(
                name=name,
                description=f"{name} sink",
                plugin_type="sink",
                config_fields=[],
            )
            for name in (self._sink_schemas or {"csv": None})
        ]

    def get_schema(self, plugin_type: PluginKind, name: str) -> PluginSchemaInfo:
        if self._schema_error is not None:
            raise self._schema_error
        bucket = self._schemas_for(plugin_type)
        if name not in bucket:
            return PluginSchemaInfo(
                name=name,
                plugin_type=plugin_type,
                description=f"{name} {plugin_type}",
                json_schema={"title": f"{name.title()}Config", "properties": {}},
                knob_schema={"fields": []},
            )
        return bucket[name]

    def post_call_hints(
        self,
        *,
        plugin_type: PluginKind,
        plugin_name: str,
        tool_name: str,
        config_snapshot: Mapping[str, object],
    ) -> tuple[str, ...]:
        return ()

    def _schemas_for(self, plugin_type: PluginKind) -> dict[str, PluginSchemaInfo]:
        if plugin_type == "source":
            return self._source_schemas
        if plugin_type == "transform":
            return self._transform_schemas
        return self._sink_schemas


def _make_catalog_with_schemas(
    source_schemas: dict[str, PluginSchemaInfo] | None = None,
    transform_schemas: dict[str, PluginSchemaInfo] | None = None,
    sink_schemas: dict[str, PluginSchemaInfo] | None = None,
    *,
    schema_error: Exception | None = None,
) -> CatalogService:
    """Build a catalog fake whose ``get_schema`` dispatches per (kind, name)."""
    return _CatalogWithSchemas(
        source_schemas=source_schemas,
        transform_schemas=transform_schemas,
        sink_schemas=sink_schemas,
        schema_error=schema_error,
    )


def _csv_schema() -> PluginSchemaInfo:
    return PluginSchemaInfo(
        name="csv",
        plugin_type="source",
        description="CSV source",
        json_schema={
            "title": "CsvSourceConfig",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        knob_schema={"fields": [{"name": "path", "type": "string", "required": True}]},
    )


def _json_sink_schema() -> PluginSchemaInfo:
    return PluginSchemaInfo(
        name="json",
        plugin_type="sink",
        description="JSON sink",
        json_schema={
            "title": "JsonSinkConfig",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        knob_schema={"fields": [{"name": "path", "type": "string", "required": True}]},
    )


def _passthrough_transform_schema() -> PluginSchemaInfo:
    return PluginSchemaInfo(
        name="passthrough",
        plugin_type="transform",
        description="Passthrough transform",
        json_schema={"title": "PassthroughConfig", "properties": {}},
        knob_schema={"fields": []},
    )


def _azure_prompt_shield_schema() -> PluginSchemaInfo:
    return PluginSchemaInfo(
        name="azure_prompt_shield",
        plugin_type="transform",
        description="Prompt injection shield",
        json_schema={
            "title": "AzurePromptShieldConfig",
            "properties": {
                "endpoint": {"type": "string"},
                "api_key": {"type": "string"},
                "fields": {"type": "string"},
                "schema": {"type": "object"},
            },
            "required": ["endpoint", "api_key", "fields", "schema"],
        },
        knob_schema={"fields": []},
        secret_requirements=(PluginSecretRequirement(field="api_key", candidates=("AZURE_CONTENT_SAFETY_KEY",)),),
    )


class TestFailureSchemaAugmentationSetPipeline:
    def test_failed_set_pipeline_includes_plugin_schemas_for_named_plugins(self) -> None:
        """set_pipeline rejection naming a plugin → that plugin's schema inline.

        set_pipeline stays atomic as a MUTATION — nothing is applied — but its
        validation now reports every defective component in one rejection
        (elspeth-4fad98a453), so a candidate whose source and sink are both
        misconfigured surfaces both option-shape errors and therefore both
        schemas. Keys are emitted in sorted ``kind/plugin`` order by
        ``build_plugin_schemas_for_failure``; prompts.py consumes that order.
        Deduplication and the synthetic multi-plugin iteration contract are
        covered separately by
        ``test_plugin_schemas_deduplicated_when_multiple_errors_name_same_plugin``
        and ``test_synthetic_multi_plugin_errors_produce_both_schemas``.
        """
        catalog = _make_catalog_with_schemas(
            source_schemas={"csv": _csv_schema()},
            sink_schemas={"json": _json_sink_schema()},
        )
        args = {
            "source": {
                "plugin": "csv",
                "on_success": "rows",
                # Missing required ``path`` field — triggers Invalid options for source 'csv'.
                "options": {"schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            "nodes": [],
            "edges": [],
            "outputs": [
                {
                    "sink_name": "main",
                    "plugin": "json",
                    # Missing required ``path`` field — triggers Invalid options for sink 'json'.
                    "options": {"schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            ],
        }

        result = execute_tool("set_pipeline", args, _empty_state(), catalog)
        payload = result.to_dict()

        assert result.success is False
        # The contract for plugin_schemas is "every plugin named in a
        # surfaced 'Invalid options for ...' error". Both defective
        # components are surfaced in the one rejection, so both schemas ride.
        assert "plugin_schemas" in payload
        # Build named_kinds as an ordered list (dict iteration is insertion
        # order in CPython 3.7+) so the assertion below is order-sensitive.
        named_kinds = [key.split("/", 1)[0] for key in payload["plugin_schemas"]]
        # Sorted kind/plugin order — the surface contract composer prompt
        # construction relies on (prompts.py consumes plugin_schemas in
        # iteration order). Reassess prompts.py before changing it.
        assert named_kinds == ["sink", "source"]
        assert [entry["component"] for entry in payload["validation"]["errors"]] == ["rejected_mutation", "rejected_mutation"]
        for key, schema in payload["plugin_schemas"].items():
            kind, plugin = key.split("/", 1)
            assert schema["name"] == plugin
            assert schema["plugin_type"] == kind
            assert "json_schema" in schema

    def test_set_pipeline_omitted_output_options_repair_hint_carries_the_sink_schema(self, tmp_path: Any) -> None:
        """The omitted-options repair hint embeds the sink's option-shape rejection.

        ``set_pipeline`` wraps that rejection in ``_missing_output_options_repair_error``
        rather than returning it bare, so the message is not a
        ``*prevalidation*`` local at the call site and the AST tripwire cannot
        see it. This is the wire pin for that one site: the sink was resolved
        before the hint was built, so the augmentation carries it exactly as
        the message-parsing consumer used to.
        """
        catalog = _make_catalog_with_schemas(
            source_schemas={"csv": _csv_schema()},
            sink_schemas={"json": _json_sink_schema()},
        )
        args = {
            "source": {
                "plugin": "csv",
                "on_success": "main",
                "options": {"path": str(tmp_path / "blobs" / "test-session" / "in.csv"), "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            "nodes": [],
            "edges": [],
            # No ``options`` key at all: the omitted-options branch.
            "outputs": [{"sink_name": "main", "plugin": "json", "on_write_failure": "discard"}],
        }

        result = execute_tool("set_pipeline", args, _empty_state(), catalog, data_dir=str(tmp_path))
        payload = result.to_dict()

        assert result.success is False, payload
        leading = payload["validation"]["errors"][0]
        assert leading["message"].startswith("Output 'main': Missing options."), leading
        assert "Invalid options for sink 'json'" in leading["message"], leading
        assert payload["plugin_schemas"] == {"sink/json": catalog.get_schema("sink", "json").model_dump(mode="json")}

    def test_successful_set_pipeline_omits_plugin_schemas(self, tmp_path: Any) -> None:
        """A successful mutation must NOT carry the optional plugin_schemas field."""
        catalog = _make_catalog_with_schemas(
            source_schemas={"csv": _csv_schema()},
            transform_schemas={"passthrough": _passthrough_transform_schema()},
            sink_schemas={
                "csv": PluginSchemaInfo(
                    name="csv",
                    plugin_type="sink",
                    description="CSV sink",
                    json_schema={"title": "CsvSinkConfig", "properties": {}},
                    knob_schema={"fields": []},
                )
            },
        )
        args = {
            "source": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {
                    "path": str(tmp_path / "blobs" / "test-session" / "in.csv"),
                    "schema": {"mode": "observed"},
                },
                "on_validation_failure": "discard",
            },
            "nodes": [
                {
                    "id": "t1",
                    "node_type": "transform",
                    "plugin": "passthrough",
                    "input": "rows",
                    "on_success": "main",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}},
                }
            ],
            "edges": [
                {
                    "id": "e1",
                    "from_node": "source",
                    "to_node": "t1",
                    "edge_type": "on_success",
                    "label": None,
                }
            ],
            "outputs": [
                {
                    "sink_name": "main",
                    "plugin": "csv",
                    "options": {
                        "path": str(tmp_path / "outputs" / "test-session" / "out.csv"),
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                }
            ],
        }

        result = execute_tool("set_pipeline", args, _empty_state(), catalog, data_dir=str(tmp_path))
        payload = result.to_dict()

        assert result.success is True, payload
        assert "plugin_schemas" not in payload


class TestFailureSchemaAugmentationMultiPluginErrors:
    """Cover the multi-plugin iteration contract directly.

    The augmentation hook iterates every error entry — ``set_pipeline``
    reports every defective component in one rejection, and a synthetic
    ToolResult can carry any number — so every distinct stamped
    ``(kind, plugin)`` pair must materialise in ``plugin_schemas``. These
    tests feed a hand-crafted ValidationSummary through
    ``build_plugin_schemas_for_failure`` to lock that contract independently
    of the per-tool execution paths.
    """

    def test_synthetic_multi_plugin_errors_produce_both_schemas(self) -> None:
        """Two entries stamped with different plugins → both schemas inline.

        The messages are deliberately blank: the identity rides on the
        carrier, and a message that says nothing must still attach the schema
        the producer stamped.
        """
        from elspeth.web.composer.state import ValidationEntry, ValidationSummary
        from elspeth.web.composer.tools._common import (
            ToolResult,
            build_plugin_schemas_for_failure,
        )

        validation = ValidationSummary(
            is_valid=False,
            errors=(
                ValidationEntry(
                    component="rejected_mutation",
                    message="",
                    severity="high",
                    plugin_identity=("source", "csv"),
                ),
                ValidationEntry(
                    component="rejected_mutation",
                    message="",
                    severity="high",
                    plugin_identity=("sink", "json"),
                ),
            ),
        )
        result = ToolResult(
            success=False,
            updated_state=_empty_state(),
            validation=validation,
            affected_nodes=(),
        )
        catalog = _make_catalog_with_schemas(
            source_schemas={"csv": _csv_schema()},
            sink_schemas={"json": _json_sink_schema()},
        )

        schemas = build_plugin_schemas_for_failure(result, catalog)

        assert schemas is not None
        assert list(schemas.keys()) == ["sink/json", "source/csv"]
        assert schemas["source/csv"]["plugin_type"] == "source"
        assert schemas["sink/json"]["plugin_type"] == "sink"


class TestFailureSchemaAugmentationDeduplication:
    def test_plugin_schemas_deduplicated_when_multiple_errors_name_same_plugin(self) -> None:
        """Two distinct errors naming the same (kind, plugin) → schema emitted once."""
        from elspeth.web.composer.state import ValidationEntry, ValidationSummary
        from elspeth.web.composer.tools._common import (
            ToolResult,
            build_plugin_schemas_for_failure,
        )

        # Build a synthetic ToolResult containing two errors that both
        # carry the source 'csv' identity.
        validation = ValidationSummary(
            is_valid=False,
            errors=(
                ValidationEntry(
                    component="rejected_mutation",
                    message="Invalid options for source 'csv': missing required field 'path'",
                    severity="high",
                    plugin_identity=("source", "csv"),
                ),
                ValidationEntry(
                    component="source",
                    message="Invalid options for source 'csv': trailing detail",
                    severity="high",
                    plugin_identity=("source", "csv"),
                ),
            ),
        )
        result = ToolResult(
            success=False,
            updated_state=_empty_state(),
            validation=validation,
            affected_nodes=(),
        )
        catalog = _make_catalog_with_schemas(source_schemas={"csv": _csv_schema()})

        schemas = build_plugin_schemas_for_failure(result, catalog)
        assert schemas is not None
        assert list(schemas.keys()) == ["source/csv"]


class _CatalogThatMustNotBeAsked:
    """A catalog whose ``get_schema`` is the failure: any lookup is a leak.

    Stands in wherever the test's claim is "no identity was harvested", so a
    consumer that parses the message reaches a raise instead of a schema.
    """

    def get_schema(self, plugin_type: PluginKind, name: str) -> PluginSchemaInfo:
        raise AssertionError(f"build_plugin_schemas_for_failure resolved {plugin_type}/{name} from an unstamped entry")


class TestFailureSchemaAugmentationFailsClosed:
    """The augmentation reads ``plugin_identity`` and nothing else (elspeth-f60d638661).

    Each case is a reproduced vector from the ticket, driven end-to-end
    through ``execute_tool`` so the assertion covers the dispatch wrapper
    that attaches the schemas, not only the builder.
    """

    def test_message_matching_the_retired_pattern_without_the_carrier_attaches_nothing(self) -> None:
        """Fail closed: the old regex's exact input, minus the carrier, yields ``None``."""
        from elspeth.web.composer.state import ValidationEntry, ValidationSummary
        from elspeth.web.composer.tools._common import (
            ToolResult,
            build_plugin_schemas_for_failure,
        )

        result = ToolResult(
            success=False,
            updated_state=_empty_state(),
            validation=ValidationSummary(
                is_valid=False,
                errors=(
                    ValidationEntry(
                        component="rejected_mutation",
                        message="Invalid options for source 'csv': path: Field required",
                        severity="high",
                        error_code="plugin_options_invalid",
                    ),
                ),
            ),
            affected_nodes=(),
        )

        assert build_plugin_schemas_for_failure(result, cast(CatalogService, _CatalogThatMustNotBeAsked())) is None

    def test_option_value_quoting_the_retired_pattern_plants_no_second_identity(self) -> None:
        """Ticket vector 1: a rejected option VALUE is quoted in the details tail.

        ``csv`` rejects the ``encoding`` value and quotes it verbatim; the
        value names ``sink 'json'``. Only the plugin the validator resolved
        (``source/csv``) is attached — the quoted sink is model text.
        """
        catalog = _make_catalog_with_schemas(
            source_schemas={"csv": _csv_schema()},
            sink_schemas={"json": _json_sink_schema()},
        )
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "rows",
                "options": {
                    "path": "/data/blobs/test-session/in.csv",
                    "schema": {"mode": "observed"},
                    "encoding": "Invalid options for sink 'json'",
                },
                "on_validation_failure": "discard",
            },
            _empty_state(),
            catalog,
            data_dir="/data",
        )
        payload = result.to_dict()

        assert result.success is False, payload
        assert "Invalid options for sink 'json'" in payload["validation"]["errors"][0]["message"]
        assert list(payload["plugin_schemas"]) == ["source/csv"]

    def test_option_key_quoting_the_retired_pattern_plants_no_second_identity(self) -> None:
        """Ticket vector 2's shape on the primary branch: a rejected option KEY is quoted."""
        catalog = _make_catalog_with_schemas(
            source_schemas={"csv": _csv_schema()},
            sink_schemas={"json": _json_sink_schema()},
        )
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "rows",
                "options": {
                    "path": "/data/blobs/test-session/in.csv",
                    "schema": {"mode": "observed"},
                    "Invalid options for sink 'json'": 1,
                },
                "on_validation_failure": "discard",
            },
            _empty_state(),
            catalog,
            data_dir="/data",
        )
        payload = result.to_dict()

        assert result.success is False, payload
        assert "Invalid options for sink 'json'" in payload["validation"]["errors"][0]["message"]
        assert list(payload["plugin_schemas"]) == ["source/csv"]

    def test_unrelated_failure_quoting_a_model_authored_name_attaches_nothing(self) -> None:
        """A not-found rejection quotes the model's ``sink_name`` verbatim; it carries no identity.

        Before the structural read this attached ``sink/json`` to a failure
        about a sink that does not exist — the benign variant of the ticket's
        abort (a name the catalog does not know raises instead).
        """
        catalog = _make_catalog_with_schemas(sink_schemas={"json": _json_sink_schema()})
        result = execute_tool(
            "patch_output_options",
            {"sink_name": "Invalid options for sink 'json'", "patch": {"path": None}},
            _empty_state(),
            catalog,
        )
        payload = result.to_dict()

        assert result.success is False, payload
        assert "Invalid options for sink 'json'" in payload["validation"]["errors"][0]["message"]
        assert "plugin_schemas" not in payload

    def test_secret_ref_placement_rejection_now_carries_the_resolved_plugin_schema(self) -> None:
        """Policy change, pinned on purpose (elspeth-e405ad7cd2 R8).

        ``_prevalidate_plugin_options``'s first branch rejects a ``secret_ref``
        marker on a non-credential field with a head the old regex never
        matched, so this rejection was never augmented. The producer was
        validating a resolved plugin, so the structural stamp now attaches
        its schema — which is where the model learns which fields MAY carry
        a marker. Same ``plugin_options_invalid`` code as every other option
        failure at that site.
        """
        catalog = _make_catalog_with_schemas(source_schemas={"csv": _csv_schema()})
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "rows",
                "options": {
                    "path": "/data/blobs/test-session/in.csv",
                    "schema": {"mode": "observed"},
                    "delimiter": {"secret_ref": "DELIM"},
                },
                "on_validation_failure": "discard",
            },
            _empty_state(),
            catalog,
            data_dir="/data",
        )
        payload = result.to_dict()

        assert result.success is False, payload
        leading = payload["validation"]["errors"][0]
        assert leading["message"].startswith("Invalid secret_ref placement for source 'csv'"), leading
        assert leading["error_code"] == "plugin_options_invalid"
        assert payload["data"]["error_code"] == "plugin_options_invalid"
        assert payload["plugin_schemas"] == {"source/csv": _csv_schema().model_dump(mode="json")}


def _assert_option_failure_augmented_exactly(
    payload: Mapping[str, Any],
    catalog: CatalogService,
    kind: PluginKind,
    plugin: str,
) -> None:
    """The wire parity every option-shape rejection must hold (elspeth-e405ad7cd2 R8).

    The leading rejection carries the ``plugin_options_invalid`` code (twinned
    onto ``data.error_code``), and ``plugin_schemas`` holds EXACTLY the one
    stamped plugin, byte-identical to a discrete ``get_plugin_schema`` call.
    Equality on the whole mapping is deliberate: a second key would mean a
    second identity was harvested from somewhere other than the carrier.
    """
    assert payload["validation"]["errors"][0]["component"] == "rejected_mutation", payload["validation"]["errors"][0]
    assert payload["validation"]["errors"][0]["error_code"] == "plugin_options_invalid", payload["validation"]["errors"][0]
    assert payload["data"]["error_code"] == "plugin_options_invalid", payload["data"]
    # ``to_dict`` is the JSON projection (tuples become lists), so the
    # discrete-call comparison is against the schema's JSON dump.
    assert payload["plugin_schemas"] == {f"{kind}/{plugin}": catalog.get_schema(kind, plugin).model_dump(mode="json")}


class TestFailureSchemaAugmentationPerToolCoverage:
    """Confirm the augmentation hook fires, identically, for every option-shape tool.

    One test per producer site that can emit a plugin-option rejection
    (``set_pipeline`` has its own per-producer pins in
    ``test_pipeline_planner``). Each asserts the exact wire parity in
    ``_assert_option_failure_augmented_exactly`` so a site that loses its
    ``plugin_identity`` stamp fails here by name, not only in the AST
    tripwire below.
    """

    def test_set_source_failure_carries_schema(self) -> None:
        catalog = _make_catalog_with_schemas(source_schemas={"csv": _csv_schema()})
        # Missing required ``path`` triggers Invalid options for source 'csv'.
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            _empty_state(),
            catalog,
        )
        payload = result.to_dict()
        assert result.success is False
        _assert_option_failure_augmented_exactly(payload, catalog, "source", "csv")

    def test_set_output_failure_carries_schema(self) -> None:
        catalog = _make_catalog_with_schemas(sink_schemas={"json": _json_sink_schema()})
        # Missing required ``path`` triggers Invalid options for sink 'json'.
        result = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "json",
                "options": {"schema": {"mode": "observed"}, "mode": "write", "collision_policy": "auto_increment"},
                "on_write_failure": "discard",
            },
            _empty_state(),
            catalog,
        )
        payload = result.to_dict()
        assert result.success is False
        _assert_option_failure_augmented_exactly(payload, catalog, "sink", "json")

    def test_patch_output_options_failure_carries_schema(self) -> None:
        catalog = _make_catalog_with_schemas(sink_schemas={"json": _json_sink_schema()})
        set_result = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "json",
                "options": {
                    "path": "/data/outputs/test-session/out.json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
            _empty_state(),
            catalog,
            data_dir="/data",
        )
        assert set_result.success is True, set_result.to_dict()

        # Nulling ``path`` drops the key (merge-patch semantics), so the
        # patched options fail the sink's config model.
        result = execute_tool(
            "patch_output_options",
            {"sink_name": "main", "patch": {"path": None}},
            set_result.updated_state,
            catalog,
        )
        payload = result.to_dict()
        assert result.success is False, payload
        assert "Invalid options for sink 'json'" in payload["validation"]["errors"][0]["message"]
        _assert_option_failure_augmented_exactly(payload, catalog, "sink", "json")

    def test_upsert_node_failure_carries_schema(self) -> None:
        """upsert_node for a transform with missing required ``schema`` triggers Invalid options."""
        # ``passthrough`` (via TransformDataConfig) requires a schema
        # field on options. Omitting it produces the option-shape
        # rejection the augmentation hook is built for.
        catalog = _make_catalog_with_schemas(
            transform_schemas={"passthrough": _passthrough_transform_schema()},
        )
        result = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "rows",
                "on_success": "main",
                "on_error": "discard",
                "options": {},  # missing required ``schema``
            },
            _empty_state(),
            catalog,
        )
        payload = result.to_dict()
        assert result.success is False, payload
        errors_text = " ".join(e["message"] for e in payload["validation"]["errors"])
        assert "Invalid options for transform 'passthrough'" in errors_text, errors_text
        _assert_option_failure_augmented_exactly(payload, catalog, "transform", "passthrough")

    def test_patch_node_options_failure_carries_schema(self) -> None:
        catalog = _make_catalog_with_schemas(transform_schemas={"passthrough": _passthrough_transform_schema()})
        upsert = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "rows",
                "on_success": "main",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            _empty_state(),
            catalog,
        )
        assert upsert.success is True, upsert.to_dict()

        result = execute_tool(
            "patch_node_options",
            {"node_id": "t1", "patch": {"schema": None}},
            upsert.updated_state,
            catalog,
        )
        payload = result.to_dict()
        assert result.success is False, payload
        assert "Invalid options for transform 'passthrough'" in payload["validation"]["errors"][0]["message"]
        _assert_option_failure_augmented_exactly(payload, catalog, "transform", "passthrough")

    def test_splice_transform_failure_carries_schema(self) -> None:
        catalog = _make_catalog_with_schemas(
            source_schemas={"csv": _csv_schema()},
            transform_schemas={"passthrough": _passthrough_transform_schema()},
        )
        state = _empty_state()
        for tool_name, arguments in (
            (
                "set_source",
                {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"path": "/data/blobs/test-session/in.csv", "schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                },
            ),
            (
                "upsert_node",
                {
                    "id": "t1",
                    "node_type": "transform",
                    "plugin": "passthrough",
                    "input": "rows",
                    "on_success": "main",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}},
                },
            ),
            ("upsert_edge", {"id": "e1", "from_node": "source", "to_node": "t1", "edge_type": "on_success"}),
        ):
            step = execute_tool(tool_name, arguments, state, catalog, data_dir="/data")
            assert step.success is True, (tool_name, step.to_dict())
            state = step.updated_state

        result = execute_tool(
            "splice_transform",
            {"predecessor_id": "source", "successor_id": "t1", "node": {"id": "t2", "plugin": "passthrough", "options": {}}},
            state,
            catalog,
        )
        payload = result.to_dict()
        assert result.success is False, payload
        assert "Invalid options for transform 'passthrough'" in payload["validation"]["errors"][0]["message"]
        _assert_option_failure_augmented_exactly(payload, catalog, "transform", "passthrough")

    def test_unavailable_secret_required_transform_schema_is_not_inlined(self) -> None:
        """Augmentation must not bypass get_plugin_schema's secret-availability gate."""
        catalog = _make_catalog_with_schemas(
            transform_schemas={"azure_prompt_shield": _azure_prompt_shield_schema()},
        )
        unrestricted = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        shield_id = PluginId("transform", "azure_prompt_shield")
        snapshot = PluginAvailabilitySnapshot.create(
            policy_hash="restricted",
            principal_scope="local:test-user",
            available=unrestricted.available - {shield_id},
            unavailable=(PluginAvailability(shield_id, PluginUnavailableReason.CREDENTIAL_MISSING),),
            selected=unrestricted.selected,
            usable_profile_aliases=(),
            selected_profile_aliases=(),
            binding_generation_fingerprint="restricted",
        )
        view = PolicyCatalogView(catalog, snapshot, MagicMock(spec=OperatorProfileRegistry))

        result = execute_tool(
            "upsert_node",
            {
                "id": "shield",
                "node_type": "transform",
                "plugin": "azure_prompt_shield",
                "input": "rows",
                "on_success": "llm",
                "on_error": "discard",
                "options": {},
            },
            _empty_state(),
            view,
            plugin_snapshot=snapshot,
        )
        payload = result.to_dict()

        assert result.success is False, payload
        errors_text = " ".join(e["message"] for e in payload["validation"]["errors"])
        assert "credential_unavailable" in errors_text
        assert "plugin_schemas" not in payload

    def test_patch_source_options_failure_carries_schema(self) -> None:
        """patch_source_options surfacing Invalid options for source 'csv' must carry the schema inline."""
        catalog = _make_catalog_with_schemas(
            source_schemas={"csv": _csv_schema()},
        )

        # Stage a valid source via set_source first.
        set_result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"path": "/data/blobs/test-session/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            _empty_state(),
            catalog,
            data_dir="/data",
        )
        assert set_result.success is True, set_result.to_dict()

        # patch_source_options that nulls out the required ``path`` field
        # — _apply_merge_patch drops None-valued keys, so the resulting
        # options dict loses ``path`` and _prevalidate_source rejects
        # via the same Invalid options path the augmentation hook
        # covers. The wrapper model names the key ``patch``.
        result = execute_tool(
            "patch_source_options",
            {
                "patch": {"path": None},
            },
            set_result.updated_state,
            catalog,
        )
        payload = result.to_dict()
        assert result.success is False, payload
        errors_text = " ".join(e["message"] for e in payload["validation"]["errors"])
        assert "Invalid options for source 'csv'" in errors_text, errors_text
        _assert_option_failure_augmented_exactly(payload, catalog, "source", "csv")


def _blob_bound_context(tmp_path: Path) -> tuple[ToolContext, PolicyCatalogView, tuple[Any, Any, str]]:
    """A production-shaped context for the blob-binding handlers.

    Real catalog (the blob paths infer ``text`` / ``blob_rows`` from the blob
    itself, so the fake catalog above cannot drive them), a session engine
    holding one user message (the create_blob custody precondition), and the
    ``(engine, blob_service, session_id)`` harness the plural binder's
    ready-blob helper takes.
    """
    content = "Use this exact text:\nhello"
    engine, session_id, user_message_id = _session_engine_with_user_message(content)
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    view = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    context = ToolContext(
        catalog=view,
        plugin_snapshot=snapshot,
        data_dir=str(tmp_path),
        session_engine=engine,
        session_id=session_id,
        user_message_id=user_message_id,
        user_message_content=content,
    )
    return context, view, (engine, BlobServiceImpl(engine, tmp_path), session_id)


class TestFailureSchemaAugmentationBlobBinders:
    """The two blob-binding producers, handler-direct with the builder applied by hand.

    These handlers resolve their plugin from the blob (``text`` for a
    text/plain blob, ``blob_rows`` for the plural binder) rather than from a
    model argument, so the stamp is the only way the augmentation can learn
    it. The builder is applied directly because the dispatch wrapper needs a
    live session service these tests do not stand up; the wire assertions
    above already cover the wrapper.
    """

    def test_set_source_from_blob_failure_carries_schema(self, tmp_path: Path) -> None:
        context, view, _harness = _blob_bound_context(tmp_path)
        created = _execute_create_blob(
            {"filename": "seed.txt", "mime_type": "text/plain", "content": "hello"},
            _empty_state(),
            context,
        )
        assert created.success is True, created.to_dict()

        result = _execute_set_source_from_blob(
            {
                "blob_id": created.data["blob_id"],
                "on_success": "out",
                # A rejected VALUE that names another plugin: the exact
                # vector-1 shape on the blob path.
                "options": {"column": "text", "schema": {"mode": "observed"}, "encoding": "Invalid options for sink 'csv'"},
            },
            _empty_state(),
            context,
        )

        assert result.success is False, result.to_dict()
        leading = result.validation.errors[0]
        assert leading.message.startswith("Invalid options for source 'text'"), leading.message
        assert leading.error_code == "plugin_options_invalid"
        assert leading.plugin_identity == ("source", "text")
        assert build_plugin_schemas_for_failure(result, view) == {"source/text": view.get_schema("source", "text").model_dump()}

    def test_set_source_from_blobs_failure_carries_schema(self, tmp_path: Path) -> None:
        context, view, harness = _blob_bound_context(tmp_path)
        blob_id = _create_ready_blob(harness, content=_PNG, filename="page.png", mime_type="image/png")

        result = _execute_set_source_from_blobs(
            {"blob_ids": [blob_id], "on_success": "docs", "options": {"bogus": "Invalid options for sink 'csv'"}},
            _empty_state(),
            context,
        )

        assert result.success is False, result.to_dict()
        leading = result.validation.errors[0]
        assert leading.message.startswith("Invalid options for source 'blob_rows'"), leading.message
        assert leading.error_code == "plugin_options_invalid"
        assert leading.plugin_identity == ("source", "blob_rows")
        assert build_plugin_schemas_for_failure(result, view) == {"source/blob_rows": view.get_schema("source", "blob_rows").model_dump()}


def _prevalidation_rejection_calls() -> list[tuple[str, ast.Call]]:
    """Every ``_failure_result`` call whose message is a ``*prevalidation*`` local.

    Walks the four tool modules that call a ``_prevalidate_*`` helper and
    returns ``(module, call)`` for each rejection built from its result. The
    local's name is the join point: every producer binds the helper's return
    to a name containing ``prevalidation`` before rejecting on it, so a
    rejection whose message is such a Name IS a plugin-option rejection.
    """
    calls: list[tuple[str, ast.Call]] = []
    for module in (sources_tools, outputs_tools, transforms_tools, sessions_tools):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_failure_result"):
                continue
            if len(node.args) < 2 or not isinstance(node.args[1], ast.Name) or "prevalidation" not in node.args[1].id:
                continue
            calls.append((module.__name__, node))
    return calls


def test_every_prevalidation_rejection_stamps_its_plugin_identity_and_code() -> None:
    """The structural tripwire behind the augmentation (elspeth-e405ad7cd2 R8, elspeth-f60d638661).

    The consumer fails closed: an entry without ``plugin_identity`` attaches
    nothing, silently — no error, just a rejection the model must repair
    without the contract. The per-tool tests above catch a lost stamp on the
    paths they drive; this pins the rule at the source so a NEW producer of
    a plugin-option rejection cannot land unstamped or codeless. The
    keyword must be a non-``None`` expression and the code the closed
    ``plugin_options_invalid`` literal.
    """
    calls = _prevalidation_rejection_calls()
    assert len(calls) >= 12, f"expected every prevalidation rejection site, found {len(calls)}"
    wrong: list[str] = []
    for module, call in calls:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg is not None}
        identity = keywords.get("plugin_identity")
        if identity is None or (isinstance(identity, ast.Constant) and identity.value is None):
            wrong.append(f"{module}:{call.lineno} rejects on a prevalidation result without plugin_identity=")
        code = keywords.get("error_code")
        if not (isinstance(code, ast.Constant) and code.value == "plugin_options_invalid"):
            wrong.append(f"{module}:{call.lineno} rejects on a prevalidation result without error_code='plugin_options_invalid'")
    assert not wrong, "\n".join(wrong)


def test_the_message_parser_is_gone() -> None:
    """Deleted, not re-anchored: three parsing fixes were each defeated in review.

    Pins the absence of the regex and its harvester by name so a "tighter"
    pattern cannot come back as a fallback for entries that lack the carrier.
    """
    retired = {"_INVALID_OPTIONS_PLUGIN_RE", "plugin_identities_in_option_failure"}
    tree = ast.parse(Path(common_tools.__file__).read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not (retired & (names | functions)), sorted(retired & (names | functions))


class TestFailureSchemaAugmentationNonAugmentedTools:
    def test_get_plugin_schema_does_not_carry_plugin_schemas_field(self) -> None:
        """Discovery tools — even on failure — must NOT trigger augmentation."""
        catalog = _make_catalog_with_schemas(schema_error=ValueError("Unknown plugin: nope"))
        result = execute_tool(
            "get_plugin_schema",
            {"plugin_type": "source", "name": "nope"},
            _empty_state(),
            catalog,
        )
        payload = result.to_dict()
        assert result.success is False
        assert "plugin_schemas" not in payload
