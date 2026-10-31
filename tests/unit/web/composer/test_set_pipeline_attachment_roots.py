"""Public attachment roots use current records through the strict dispatcher.

The received private-authority fixture supplies real session/blob/catalog
owners. None of these public calls receives private reviewed-source authority.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError as PydanticValidationError

from elspeth.contracts.credential_material import DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
from elspeth.contracts.freeze import deep_thaw
from elspeth.plugins.sources.blob_rows import BlobRowsSourceConfig
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.capability_skill import canonical_capability_fields, documented_capability_fields
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import SetPipelineArgumentsModel, SetSourceFromBlobsArgumentsModel
from elspeth.web.composer.skills import load_skill
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolResult, get_tool_definitions
from elspeth.web.composer.tools._common import _serialize_set_pipeline_arguments
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material_for_tool
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.helpers.composer_fences import fenced_tool_context
from tests.unit.web.composer import test_reviewed_plural_authority as custody
from tests.unit.web.composer.test_tools import execute_tool

authority_harness = custody.authority_harness

_CSV = b"department,total\nsales,7\n"
_INLINE = {"filename": "input.csv", "mime_type": "text/csv", "content": _CSV.decode()}


def _public_call(
    harness: custody._AuthorityHarness,
    tool: str,
    arguments: dict[str, Any],
    state: CompositionState,
) -> ToolResult:
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(harness.catalog)
    catalog = PolicyCatalogView.for_trained_operator(harness.catalog, snapshot)
    with fenced_tool_context(
        catalog=catalog,
        plugin_snapshot=snapshot,
        session_engine=harness.engine,
        session_id=str(harness.session_id),
    ) as context:
        return execute_tool(
            tool,
            arguments,
            state,
            catalog,
            plugin_snapshot=snapshot,
            session_engine=harness.engine,
            session_id=str(harness.session_id),
            session_operation_context=context.session_operation_context,
            session_operation_authority=context.session_operation_authority,
            user_id="alice",
            data_dir=str(harness.data_dir),
            validate_arguments=True,
            raise_schema_argument_errors=True,
            require_data_dir_for_paths=True,
        )


def _arguments(roots: dict[str, dict[str, Any]], *, default: bool = False) -> dict[str, Any]:
    # The existing public serializer supplies the current sink_name transport
    # for the rootless fixture; no private execution authority enters dispatch.
    arguments, error = _serialize_set_pipeline_arguments(custody._draft())
    assert error is None and arguments is not None
    assert arguments.pop("sources") == {}
    if default:
        assert set(roots) == {"source"}
        arguments["source"] = roots["source"]
    else:
        arguments["sources"] = roots
    return arguments


def _root(plugin: str, **selectors: Any) -> dict[str, Any]:
    return {
        "plugin": plugin,
        "on_success": "documents",
        "on_validation_failure": "discard",
        "description": "Provider-selected attachment root",
        "options": {"schema": {"mode": "observed"}, **({"column": "provider_text"} if plugin == "text" else {})},
        **selectors,
    }


def _csv_blob(harness: custody._AuthorityHarness) -> str:
    return custody._create_blob(harness, content=_CSV, filename="input.csv", mime_type="text/csv")


def _images(harness: custody._AuthorityHarness) -> tuple[str, str]:
    return (
        custody._create_blob(harness, content=custody._PNG, filename="first.png", mime_type="image/png"),
        custody._create_blob(harness, content=custody._JPEG, filename="second.jpg", mime_type="image/jpeg"),
    )


def _expected_entries(harness: custody._AuthorityHarness, blob_ids: list[str]) -> list[dict[str, Any]]:
    entries = []
    for blob_id in blob_ids:
        row = custody._blob_row(harness, blob_id)
        assert row is not None and row["session_id"] == str(harness.session_id) and row["status"] == "ready"
        entries.append(
            {
                "blob_id": row["id"],
                "payload_ref": row["content_hash"],
                "filename": row["filename"],
                "mime_type": row["mime_type"],
                "size_bytes": row["size_bytes"],
            }
        )
    return entries


def _assert_candidate_refusal(result: ToolResult, state: CompositionState, code: str, source_name: str) -> None:
    assert result.success is False, result.to_dict()
    assert result.updated_state is state
    assert result.updated_state.version == state.version
    assert result.affected_nodes == ()
    assert result.prior_validation is None
    component = "source" if source_name == "source" else f"source:{source_name}"
    assert [(entry.component, entry.error_code, entry.rejected_component) for entry in result.validation.errors] == [
        ("rejected_mutation", code, component)
    ]
    assert "validation_delta" not in result.to_dict()


def test_existing_default_scalar_and_incremental_plural_controls(authority_harness: custody._AuthorityHarness) -> None:
    """Real old-interface positives distinguish setup health from new grammar."""
    harness = authority_harness
    blob_id = _csv_blob(harness)
    state = custody._draft()
    arguments = _arguments({"source": _root("csv", blob_id=blob_id)}, default=True)
    before = deepcopy(arguments)
    result = _public_call(harness, "set_pipeline", arguments, state)
    assert result.success, result.to_dict()
    assert result.validation.is_valid, result.to_dict()
    assert result.updated_state.sources["source"].plugin == "csv"
    assert result.updated_state.sources["source"].options["blob_ref"] == blob_id
    assert result.updated_state.version == state.version + 1
    assert arguments == before

    images = list(_images(harness))
    incremental = _public_call(
        harness,
        "set_source_from_blobs",
        {"source_name": "scans", "blob_ids": images, "on_success": "documents", "on_validation_failure": "discard"},
        state,
    )
    assert incremental.success and incremental.validation.is_valid, incremental.to_dict()
    assert deep_thaw(incremental.updated_state.sources["scans"].options["blobs"]) == _expected_entries(harness, images)
    restored, _record = custody._persist(harness, incremental.updated_state)
    assert restored.sources == incremental.updated_state.sources


@pytest.mark.parametrize("authority", ["declaration", "model", "teaching"])
def test_attachment_selector_contract_agrees_across_declaration_model_and_teaching(authority: str) -> None:
    scalar = str(UUID(int=1, version=4))
    ordered = [str(UUID(int=2, version=4)), str(UUID(int=3, version=4))]
    arguments = _arguments({"scan": _root("blob_rows", blob_id=None, blob_ids=ordered)})
    if authority == "model":
        validated = SetPipelineArgumentsModel.model_validate(arguments, strict=True)
        assert validated.model_dump(exclude_unset=True) == arguments
        scalar_arguments = _arguments({"scan": _root("csv", blob_id=scalar, blob_ids=None)})
        assert SetPipelineArgumentsModel.model_validate(scalar_arguments, strict=True).model_dump(exclude_unset=True) == scalar_arguments
        default = _arguments({"source": _root("blob_rows", blob_ids=ordered)}, default=True)
        assert SetPipelineArgumentsModel.model_validate(default, strict=True).model_dump(exclude_unset=True) == default
        invalid = deepcopy(arguments)
        invalid["sources"]["scan"]["unexpected_attachment_selector"] = scalar
        with pytest.raises(PydanticValidationError):
            SetPipelineArgumentsModel.model_validate(invalid, strict=True)
        return
    schema = canonical_set_pipeline_schema()
    families = (
        canonical_capability_fields(schema)
        if authority == "declaration"
        else documented_capability_fields(load_skill("pipeline_capabilities"))
    )
    assert {"blob_id", "blob_ids", "inline_blob"} <= families["source"]
    assert {"blob_id", "blob_ids"} <= families["named_source"]
    assert "inline_blob" not in families["named_source"]
    if authority == "declaration":
        validator = Draft202012Validator(schema)
        assert list(validator.iter_errors(arguments)) == []
        invalid = deepcopy(arguments)
        invalid["sources"]["scan"]["unexpected_attachment_selector"] = scalar
        assert list(validator.iter_errors(invalid))


@pytest.mark.parametrize("scalar_plugin", ["csv", "text"])
def test_set_pipeline_authors_named_attachment_roots(authority_harness: custody._AuthorityHarness, scalar_plugin: str) -> None:
    harness = authority_harness
    csv = _csv_blob(harness)
    first, second = _images(harness)
    state = custody._draft()
    state_before = state.to_dict()
    arguments = _arguments({"table": _root(scalar_plugin, blob_id=csv), "scans": _root("blob_rows", blob_ids=[second, first])})
    before = deepcopy(arguments)

    result = _public_call(harness, "set_pipeline", arguments, state)

    assert result.success and result.validation.is_valid, result.to_dict()
    assert tuple(result.updated_state.sources) == ("table", "scans")
    assert result.updated_state.sources["table"].plugin == scalar_plugin
    assert result.updated_state.sources["table"].options["blob_ref"] == csv
    if scalar_plugin == "text":
        assert result.updated_state.sources["table"].options["column"] == "provider_text"
    assert result.updated_state.sources["scans"].plugin == "blob_rows"
    assert deep_thaw(result.updated_state.sources["scans"].options["blobs"]) == _expected_entries(harness, [second, first])
    for source in result.updated_state.sources.values():
        assert (source.on_success, source.on_validation_failure, source.description) == (
            "documents",
            "discard",
            "Provider-selected attachment root",
        )
    assert result.updated_state.version == state.version + 1
    assert result.updated_state.nodes == () and result.updated_state.edges == ()
    assert arguments == before and state.to_dict() == state_before


@pytest.mark.parametrize("count", [1, 2])
def test_default_source_binds_ordered_blob_ids(authority_harness: custody._AuthorityHarness, count: int) -> None:
    harness = authority_harness
    selected = list(reversed(_images(harness)))[:count]
    state = custody._draft()
    arguments = _arguments({"source": _root("blob_rows", blob_ids=selected)}, default=True)
    before = deepcopy(arguments)

    result = _public_call(harness, "set_pipeline", arguments, state)

    assert result.success and result.validation.is_valid, result.to_dict()
    source = result.updated_state.sources["source"]
    assert source.plugin == "blob_rows"
    assert deep_thaw(source.options["blobs"]) == _expected_entries(harness, selected)
    assert "path" not in source.options and "blob_ref" not in source.options
    assert result.updated_state.version == state.version + 1
    assert arguments == before


@pytest.mark.parametrize("source_name", ["source", "scans"])
@pytest.mark.parametrize(
    "case",
    ["null_plural", "null_scalar", "all_null", "ambiguous", "empty_id", "malformed_id", "malformed_member"],
)
def test_attachment_selector_null_ambiguity_and_shape_channels(
    authority_harness: custody._AuthorityHarness, source_name: str, case: str
) -> None:
    harness = authority_harness
    csv = _csv_blob(harness)
    images = list(_images(harness))
    root = _root("csv", blob_id=csv, blob_ids=None)
    if case == "null_scalar":
        root = _root("blob_rows", blob_id=None, blob_ids=images)
    elif case == "all_null":
        row = custody._blob_row(harness, csv)
        assert row is not None
        root = _root("csv", blob_id=None, blob_ids=None)
        root["options"]["path"] = row["storage_path"]
    elif case == "ambiguous":
        root = _root("blob_rows", blob_id=csv, blob_ids=images)
    elif case in {"empty_id", "malformed_id"}:
        root = _root("csv", blob_id="" if case == "empty_id" else "not-a-uuid", blob_ids=None)
    elif case == "malformed_member":
        root = _root("blob_rows", blob_ids=[images[0], "not-a-uuid"])
    if source_name == "source":
        root["inline_blob"] = None
    arguments = _arguments({source_name: root}, default=source_name == "source")
    before = deepcopy(arguments)
    state = custody._draft()
    state_before = state.to_dict()

    result = _public_call(harness, "set_pipeline", arguments, state)

    if case == "ambiguous":
        _assert_candidate_refusal(result, state, "source_binding_ambiguous", source_name)
    elif case in {"empty_id", "malformed_id", "malformed_member"}:
        _assert_candidate_refusal(result, state, "source_blob_identity_invalid", source_name)
    else:
        assert result.success and result.validation.is_valid, result.to_dict()
        if case == "null_plural":
            assert result.updated_state.sources[source_name].options["blob_ref"] == csv
        elif case == "null_scalar":
            assert deep_thaw(result.updated_state.sources[source_name].options["blobs"]) == _expected_entries(harness, images)
        else:
            assert "blob_ref" not in result.updated_state.sources[source_name].options
    assert arguments == before and state.to_dict() == state_before


@pytest.mark.parametrize(
    "selectors", [{"blob_id": "", "inline_blob": _INLINE}, {"blob_ids": [str(UUID(int=1, version=4))], "inline_blob": _INLINE}]
)
def test_default_inline_ambiguity_precedes_materialization(authority_harness: custody._AuthorityHarness, selectors: dict[str, Any]) -> None:
    harness = authority_harness
    arguments = _arguments({"source": _root("csv", **deepcopy(selectors))}, default=True)
    before = deepcopy(arguments)
    state = custody._draft()
    files_before = sorted(path.relative_to(harness.data_dir) for path in harness.data_dir.rglob("*") if path.is_file())

    result = _public_call(harness, "set_pipeline", arguments, state)

    _assert_candidate_refusal(result, state, "source_binding_ambiguous", "source")
    assert sorted(path.relative_to(harness.data_dir) for path in harness.data_dir.rglob("*") if path.is_file()) == files_before
    assert arguments == before


@pytest.mark.parametrize("source_name", ["source", "scans"])
@pytest.mark.parametrize(
    "selectors", [{"blob_id": 1}, {"blob_ids": []}, {"blob_ids": "not-a-list"}, {"blob_ids": [None]}, {"blob_ids": [1]}]
)
def test_attachment_selector_shape_errors_use_argument_channel(
    authority_harness: custody._AuthorityHarness, source_name: str, selectors: dict[str, Any]
) -> None:
    arguments = _arguments({source_name: _root("blob_rows", **deepcopy(selectors))}, default=source_name == "source")
    before = deepcopy(arguments)
    state = custody._draft()
    with pytest.raises(PydanticValidationError):
        SetPipelineArgumentsModel.model_validate(arguments, strict=True)
    with pytest.raises(ToolArgumentError) as caught:
        _public_call(authority_harness, "set_pipeline", arguments, state)
    assert caught.value.code == "SCHEMA_VALIDATION"
    assert arguments == before and state == custody._draft()


@pytest.mark.parametrize("inline", [None, _INLINE])
def test_named_inline_remains_undeclared(authority_harness: custody._AuthorityHarness, inline: object) -> None:
    arguments = _arguments({"scans": _root("csv", inline_blob=deepcopy(inline))})
    with pytest.raises(PydanticValidationError):
        SetPipelineArgumentsModel.model_validate(arguments, strict=True)
    with pytest.raises(ToolArgumentError) as caught:
        _public_call(authority_harness, "set_pipeline", arguments, custody._draft())
    assert caught.value.code == "SCHEMA_VALIDATION"


@pytest.mark.parametrize("source_name", ["source", "scans"])
def test_blob_ids_require_the_provider_to_author_blob_rows(authority_harness: custody._AuthorityHarness, source_name: str) -> None:
    harness = authority_harness
    csv = _csv_blob(harness)
    state = custody._draft()
    arguments = _arguments({source_name: _root("csv", blob_ids=[csv])}, default=source_name == "source")
    before = deepcopy(arguments)

    refused = _public_call(harness, "set_pipeline", arguments, state)

    _assert_candidate_refusal(refused, state, "source_blob_plugin_mismatch", source_name)
    assert arguments == before
    assert refused.validation_guidance is not None
    assert set(refused.validation_guidance["codes"]) == {"source_blob_plugin_mismatch"}
    assert "blob_rows" in refused.validation_guidance["codes"]["source_blob_plugin_mismatch"]["suggested_fix"]

    arguments = _arguments({source_name: _root("blob_rows", blob_ids=[csv])}, default=source_name == "source")
    accepted = _public_call(harness, "set_pipeline", arguments, state)
    assert accepted.success and accepted.validation.is_valid, accepted.to_dict()
    assert accepted.updated_state.sources[source_name].plugin == "blob_rows"
    assert deep_thaw(accepted.updated_state.sources[source_name].options["blobs"]) == _expected_entries(harness, [csv])


@pytest.mark.parametrize("defect", ["duplicate_id", "duplicate_content", "missing_later_root"])
def test_duplicate_scope_and_later_root_atomicity(authority_harness: custody._AuthorityHarness, defect: str) -> None:
    harness = authority_harness
    first, later = _images(harness)
    state = custody._draft()
    cross_root = _arguments({"first": _root("blob_rows", blob_ids=[first]), "later": _root("blob_rows", blob_ids=[first, later])})
    positive = _public_call(harness, "set_pipeline", cross_root, state)
    assert positive.success and positive.validation.is_valid, positive.to_dict()
    assert positive.updated_state.version == state.version + 1
    assert deep_thaw(positive.updated_state.sources["first"].options["blobs"]) == _expected_entries(harness, [first])
    assert deep_thaw(positive.updated_state.sources["later"].options["blobs"]) == _expected_entries(harness, [first, later])

    if defect == "duplicate_id":
        selected = [first, first]
    elif defect == "duplicate_content":
        copied = custody._create_blob(harness, content=custody._PNG, filename="same-content.png", mime_type="image/png")
        assert copied != first
        selected = [first, copied]
    else:
        selected = [first, str(UUID(int=999, version=4))]
    # Rootless saved state has standing graph errors; a full-replacement
    # refusal must contain only the candidate defect, not those old findings.
    assert state.validate().errors
    arguments = _arguments({"first": _root("blob_rows", blob_ids=[first]), "later": _root("blob_rows", blob_ids=selected)})
    before = deepcopy(arguments)
    state_before = state.to_dict()
    records_before = {blob_id: custody._blob_row(harness, blob_id) for blob_id in (first, later)}

    refused = _public_call(harness, "set_pipeline", arguments, state)

    code = "source_blob_unavailable" if defect == "missing_later_root" else "source_blob_duplicate"
    _assert_candidate_refusal(refused, state, code, "later")
    assert arguments == before and state.to_dict() == state_before
    assert {blob_id: custody._blob_row(harness, blob_id) for blob_id in (first, later)} == records_before


@pytest.mark.parametrize("boundary", ["model", "declaration", "detector"])
def test_public_attachment_width_matches_the_actual_credential_boundary(boundary: str) -> None:
    width = DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_width
    assert width == 512
    assert BlobRowsSourceConfig.model_json_schema()["properties"]["blobs"]["maxItems"] == 1000
    ids = [str(UUID(int=index + 1, version=4)) for index in range(width + 1)]
    arguments = {"blob_ids": ids[:width], "on_success": "documents", "on_validation_failure": "discard"}
    if boundary == "model":
        assert SetSourceFromBlobsArgumentsModel.model_validate(arguments, strict=True).blob_ids == ids[:width]
        with pytest.raises(PydanticValidationError):
            SetSourceFromBlobsArgumentsModel.model_validate({**arguments, "blob_ids": ids}, strict=True)
        for default in (True, False):
            name = "source" if default else "scans"
            admitted = _arguments({name: _root("blob_rows", blob_ids=ids[:width])}, default=default)
            SetPipelineArgumentsModel.model_validate(admitted, strict=True)
            rejected = _arguments({name: _root("blob_rows", blob_ids=ids)}, default=default)
            with pytest.raises(PydanticValidationError):
                SetPipelineArgumentsModel.model_validate(rejected, strict=True)
    elif boundary == "declaration":
        definition = next(item for item in get_tool_definitions() if item["name"] == "set_source_from_blobs")
        validator = Draft202012Validator(definition["parameters"])
        assert list(validator.iter_errors(arguments)) == []
        errors = list(validator.iter_errors({**arguments, "blob_ids": ids}))
        assert [(error.validator, tuple(error.path)) for error in errors] == [("maxItems", ("blob_ids",))]
        full = Draft202012Validator(canonical_set_pipeline_schema())
        for default in (True, False):
            name = "source" if default else "scans"
            assert list(full.iter_errors(_arguments({name: _root("blob_rows", blob_ids=ids[:width])}, default=default))) == []
            errors = list(full.iter_errors(_arguments({name: _root("blob_rows", blob_ids=ids)}, default=default)))
            assert len(errors) == 1 and errors[0].validator == "maxItems"
    else:
        require_no_credential_material_for_tool("set_source_from_blobs", arguments, surface="test_public_attachment_width")
        with pytest.raises(CredentialMaterialRefused) as caught:
            require_no_credential_material_for_tool(
                "set_source_from_blobs", {**arguments, "blob_ids": ids}, surface="test_public_attachment_width"
            )
        assert caught.value.to_payload()["failure_code"] == "credential_material_rejected"
