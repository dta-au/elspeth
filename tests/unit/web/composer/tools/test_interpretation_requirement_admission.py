"""Authoring admission for interpretation requirements is plugin-agnostic.

The exhaustive shape matrix belongs at the centralized guard.  The production
writer matrix then proves that every source/node mutation reaches that guard
before plugin validation, blob lookup, merge-patching, or state publication.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.source_demand import build_source_data_contract_draft, source_data_contract_artifact_hash
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.composer.tools import _common as common_tools
from elspeth.web.composer.tools import execute_tool as _execute_tool
from elspeth.web.composer.tools._common import (
    _resolver_owned_interpretation_requirement_error,
    _runtime_owned_llm_option_error,
    _serialize_set_pipeline_arguments,
)
from elspeth.web.composer.tools.blobs import _sync_get_blob
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_TEMPLATE_PARTS_KEY,
    ServerStagedRequiredControlUserTerm,
    parse_interpretation_requirements,
    project_planner_context_interpretation_requirement,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer import test_set_source_from_blobs as plural_blob_tests
from tests.unit.web.composer.test_authoring_reconciliation import _resolved_source_contract_state
from tests.unit.web.composer.test_set_source_from_blobs import _create_ready_blob

harness = plural_blob_tests.harness


@pytest.mark.parametrize(
    "kind", ["vague_term", "invented_source", "source_data_contract", "llm_model_choice", "llm_prompt_template", "pipeline_decision"]
)
@pytest.mark.parametrize("status", ["pending", "resolved"])
def test_review_projection_separates_historical_draft_without_exposing_authority(kind: str, status: str) -> None:
    row = _canonical_pending_requirement(kind=kind) | {"status": status, "display_title": "Review title"}
    before = deepcopy(row)
    projected = project_planner_context_interpretation_requirement(row)
    assert projected == {
        "id": row["id"],
        "kind": kind,
        "user_term": row["user_term"],
        "historical_draft" if status == "resolved" else "draft": row["draft"],
        "status": status,
        "display_title": "Review title",
    }
    echoed, matched = common_tools._normalize_echoed_interpretation_requirements(
        {INTERPRETATION_REQUIREMENTS_KEY: [projected]}, stored_options={INTERPRETATION_REQUIREMENTS_KEY: [row]}
    )
    assert matched
    assert echoed[INTERPRETATION_REQUIREMENTS_KEY] == [
        {"kind": kind, "user_term": row["user_term"], "draft": row["draft"], "display_title": "Review title"}
    ]
    assert row == before


@pytest.mark.parametrize(
    "writer",
    [
        "set_source",
        "patch_source_options",
        "set_source_from_blob",
        "set_source_from_blobs",
        "set_pipeline_source",
        "set_pipeline_named_source",
    ],
)
@pytest.mark.parametrize("echo", ["current", "full", "stale-resolved", "tampered-history"])
def test_source_writers_admit_only_exact_current_or_full_review_echoes(
    writer: str,
    echo: str,
    harness,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, session_id = harness
    blob_id = _create_ready_blob(harness, content=b"colour\nred\n", filename="colours.csv", mime_type="text/csv")
    catalog = _catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    state = _resolved_source_contract_state(required_fields=["colour"])
    record = _sync_get_blob(engine, blob_id, session_id)
    assert record is not None and record["status"] == "ready"
    assert Path(record["storage_path"]).is_file()
    source = state.sources["source"]
    state = replace(
        state,
        sources={"source": replace(source, options={**source.options, "path": record["storage_path"]})},
        outputs=tuple(replace(output, options={**output.options, "path": f"outputs/{session_id}/out.jsonl"}) for output in state.outputs),
    )
    # This borrowed contract fixture originally had an unscoped display path.
    # Use the actual ready artifact and prove both real session path guards.
    assert common_tools._validate_source_path(state.sources["source"].options, str(tmp_path), session_id=session_id) is None
    assert all(
        common_tools._validate_sink_path(dict(output.options), str(tmp_path), session_id=session_id) is None for output in state.outputs
    )
    with fenced_operation_context(engine, session_id) as operation:
        context = {
            "plugin_snapshot": snapshot,
            "data_dir": str(tmp_path),
            "session_engine": engine,
            "session_id": session_id,
            "session_operation_context": operation,
            "session_operation_authority": SQLiteLocalSessionOperationAuthority(engine),
        }
        if writer in ("set_source_from_blob", "set_source_from_blobs"):
            bind_args = {"on_success": state.sources["source"].on_success, "on_validation_failure": "discard"}
            if writer == "set_source_from_blob":
                bind_args.update(blob_id=blob_id, options={"schema": {"mode": "observed"}})
            else:
                bind_args["blob_ids"] = [blob_id]
            bound = _execute_tool(writer, bind_args, _empty_state(), policy, **context)
            assert bound.success, bound.validation.errors
            source = bound.updated_state.sources["source"]
            if writer == "set_source_from_blobs":
                fields = list(source.options["schema"]["guaranteed_fields"])
                node = replace(state.nodes[0], options={"schema": {"mode": "observed"}, "required_input_fields": ["blob_id"]})
                state = replace(state, nodes=(node,))
            else:
                fields = ["colour"]
            card = build_source_data_contract_draft(fields, None)
            requirement = _canonical_pending_requirement(
                requirement_id="source-contract",
                kind="source_data_contract",
                user_term="source_data_contract",
                draft=card,
            ) | {
                "status": "resolved",
                "event_id": "source-contract-event",
                "accepted_value": card,
                "accepted_artifact_hash": source_data_contract_artifact_hash(fields),
            }
            source_options = deep_thaw(source.options)
            source_options[INTERPRETATION_REQUIREMENTS_KEY] = [requirement]
            if writer == "set_source_from_blob":
                source_options["schema"] = {"mode": "observed", "guaranteed_fields": fields}
            state = state.with_named_source("source", replace(source, options=source_options))
        if writer == "set_pipeline_named_source":
            state = replace(state, sources={"orders": state.sources["source"]})
        source_name = next(iter(state.sources))
        source = state.sources[source_name]
        rows = deep_thaw(source.options[INTERPRETATION_REQUIREMENTS_KEY])
        supplied = rows if echo == "full" else [project_planner_context_interpretation_requirement(row) for row in rows]
        assert supplied[0]["status"] == "resolved"
        if echo == "stale-resolved":
            supplied[0]["draft"] = supplied[0].pop("historical_draft")
        elif echo == "tampered-history":
            supplied[0]["historical_draft"] = _SENSITIVE_SENTINEL
        options = deep_thaw(source.options)
        options[INTERPRETATION_REQUIREMENTS_KEY] = supplied
        if writer == "patch_source_options":
            args = {"source_name": source_name, "patch": {INTERPRETATION_REQUIREMENTS_KEY: supplied}}
            tool = writer
        elif writer in ("set_source_from_blob", "set_source_from_blobs"):
            args = {
                "on_success": source.on_success,
                "on_validation_failure": "discard",
                "options": {"schema": options["schema"], INTERPRETATION_REQUIREMENTS_KEY: supplied},
            }
            args["blob_id" if writer == "set_source_from_blob" else "blob_ids"] = blob_id if writer == "set_source_from_blob" else [blob_id]
            tool = writer
        elif writer == "set_source":
            args = {"plugin": source.plugin, "on_success": source.on_success, "on_validation_failure": "discard", "options": options}
            tool = writer
        else:
            # The convenience exporter cannot reconstruct named blob paths.
            # Keep its exact node/output payload and exercise the public source
            # shape with the actual saved path/options, as the writer accepts.
            args, error = _serialize_set_pipeline_arguments(replace(state, sources={}))
            assert error is None and args is not None
            del args["sources"]
            source_args = {
                "plugin": source.plugin,
                "on_success": source.on_success,
                "on_validation_failure": source.on_validation_failure,
                "description": source.description,
                "options": options,
            }
            if writer == "set_pipeline_source":
                args["source"] = source_args
            else:
                args["sources"] = {source_name: source_args}
            tool = "set_pipeline"
        before = state.to_dict()
        calls = []
        original = common_tools._normalize_echoed_interpretation_requirements

        def observe(options, *, stored_options):
            value = original(options, stored_options=stored_options)
            if stored_options is source.options or stored_options == source.options:
                calls.append(value[1])
            return value

        # Actual writer imports bind the shared comparator at module scope.
        from elspeth.web.composer.tools import sessions, sources

        monkeypatch.setattr(sources, "_normalize_echoed_interpretation_requirements", observe)
        monkeypatch.setattr(sessions, "_normalize_echoed_interpretation_requirements", observe)
        result = _execute_tool(tool, args, state, policy, **context)
    assert calls and calls[0] is (echo in ("current", "full"))
    assert state.to_dict() == before
    if echo in ("current", "full"):
        assert result.success, result.validation.errors
        assert (
            result.updated_state.sources[source_name].options[INTERPRETATION_REQUIREMENTS_KEY]
            == source.options[INTERPRETATION_REQUIREMENTS_KEY]
        )
    else:
        assert not result.success and result.updated_state is state
        assert result.validation.errors[0].error_code == "interpretation_requirements_invalid"
        assert _SENSITIVE_SENTINEL not in repr(result.to_dict())


_SENSITIVE_SENTINEL: Final[str] = "sensitive-requirement-value"
_PLUGIN_NAMES: Final[tuple[str | None, ...]] = (
    "llm",
    "passthrough",
    "field_mapper",
    "web_scrape",
    None,
)
_RESOLVER_OWNED_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "status",
    "event_id",
    "accepted_value",
    "accepted_artifact_hash",
    "resolved_prompt_template_hash",
)
_IDENTITY_COLLISION_ERROR: Final[str] = "duplicate or colliding interpretation requirement identities"


def _valid_authored_requirement() -> dict[str, str]:
    return {
        "kind": "pipeline_decision",
        "user_term": "prompt_injection_shield_recommendation",
        "draft": "Recommend a prompt-injection shield.",
    }


def test_display_title_survives_authoring_projection_echo_and_serialization() -> None:
    authored = {**_valid_authored_requirement(), "display_title": "Prompt injection protection"}
    options = {INTERPRETATION_REQUIREMENTS_KEY: [authored]}
    assert common_tools._resolver_owned_interpretation_requirement_error(options, tool_name="upsert_node") is None
    canonical = common_tools._canonicalize_authored_interpretation_requirements(options, component_id="summarize")
    assert common_tools._canonical_interpretation_requirement_error(canonical, tool_name="upsert_node") is None
    parsed = parse_interpretation_requirements(canonical)
    assert parsed is not None
    assert parsed[0]["display_title"] == "Prompt injection protection"
    projected = project_planner_context_interpretation_requirement(parsed[0])
    assert projected["display_title"] == authored["display_title"]
    echoed, was_echoed = common_tools._normalize_echoed_interpretation_requirements(
        {INTERPRETATION_REQUIREMENTS_KEY: [projected]}, stored_options=canonical
    )
    assert was_echoed
    assert echoed[INTERPRETATION_REQUIREMENTS_KEY] == [authored]
    assert common_tools._serialize_authoring_options(canonical)[INTERPRETATION_REQUIREMENTS_KEY] == [authored]
    plain = common_tools._canonicalize_authored_interpretation_requirements(
        {INTERPRETATION_REQUIREMENTS_KEY: [_valid_authored_requirement()]}, component_id="summarize"
    )
    assert parsed[0]["id"] == plain[INTERPRETATION_REQUIREMENTS_KEY][0]["id"]
    assert parsed[0]["user_term"] == authored["user_term"]
    assert "display_title" not in plain[INTERPRETATION_REQUIREMENTS_KEY][0]


@pytest.mark.parametrize("title", [None, "", "  ", 7, True, {}, "x" * 201])
def test_malformed_display_title_is_rejected_at_authoring_and_persisted_boundaries(title: object) -> None:
    authored = {**_valid_authored_requirement(), "display_title": title}
    assert (
        common_tools._resolver_owned_interpretation_requirement_error(
            {INTERPRETATION_REQUIREMENTS_KEY: [authored]}, tool_name="upsert_node"
        )
        is not None
    )
    canonical = common_tools._canonicalize_authored_interpretation_requirements(
        {INTERPRETATION_REQUIREMENTS_KEY: [_valid_authored_requirement()]}, component_id="summarize"
    )
    canonical[INTERPRETATION_REQUIREMENTS_KEY][0]["display_title"] = title
    with pytest.raises(TypeError, match="display_title"):
        parse_interpretation_requirements(canonical)
    assert common_tools._canonical_interpretation_requirement_error(canonical, tool_name="upsert_node") is not None


def _canonical_pending_requirement(
    *,
    requirement_id: str = "alpha:existing",
    kind: str = "vague_term",
    user_term: str = "alpha",
    draft: str = "Alpha means the first category.",
) -> dict[str, Any]:
    return {
        "id": requirement_id,
        "kind": kind,
        "user_term": user_term,
        "draft": draft,
        "status": "pending",
        "event_id": None,
        "accepted_value": None,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": None,
    }


def test_authoring_serializer_fails_loudly_on_owned_requirement_contract_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owned serializer's impossible item shape must not be passed through."""

    def _malformed_owned_serialization(_options: Any) -> dict[str, Any]:
        return {INTERPRETATION_REQUIREMENTS_KEY: [None]}

    monkeypatch.setattr(
        common_tools,
        "serialize_authoring_review_options",
        _malformed_owned_serialization,
    )

    with pytest.raises(TypeError):
        common_tools._serialize_authoring_options({})


_MALFORMED_PRESENT_VALUES: Final[tuple[Any, ...]] = (
    None,
    {},
    _SENSITIVE_SENTINEL,
    7,
    True,
    (),
    [None],
    [{}],
    [[_valid_authored_requirement()]],
    [_SENSITIVE_SENTINEL],
    [7],
    [True],
    [{"user_term": "term", "draft": "draft"}],
    [{"kind": "pipeline_decision", "draft": "draft"}],
    [{"kind": "pipeline_decision", "user_term": "term"}],
    [{"kind": 7, "user_term": "term", "draft": "draft"}],
    [{"kind": "pipeline_decision", "user_term": 7, "draft": "draft"}],
    [{"kind": "pipeline_decision", "user_term": "term", "draft": 7}],
    [{"kind": "not-a-kind", "user_term": "term", "draft": "draft"}],
    [{"kind": "pipeline_decision", "user_term": "term", "draft": "draft", "unknown": _SENSITIVE_SENTINEL}],
)


@pytest.mark.parametrize("plugin_name", _PLUGIN_NAMES)
def test_runtime_guard_distinguishes_absent_requirements_from_present_values(plugin_name: str | None) -> None:
    assert _runtime_owned_llm_option_error(plugin_name, {}, tool_name="test_tool") is None


@pytest.mark.parametrize("plugin_name", _PLUGIN_NAMES)
def test_runtime_guard_allows_valid_unresolved_authoring_shell(plugin_name: str | None) -> None:
    options = {INTERPRETATION_REQUIREMENTS_KEY: [_valid_authored_requirement()]}

    assert _runtime_owned_llm_option_error(plugin_name, options, tool_name="test_tool") is None


@pytest.mark.parametrize("plugin_name", _PLUGIN_NAMES)
@pytest.mark.parametrize("requirements", _MALFORMED_PRESENT_VALUES)
def test_runtime_guard_rejects_every_malformed_present_shape_without_leaking(
    plugin_name: str | None,
    requirements: Any,
) -> None:
    options = {INTERPRETATION_REQUIREMENTS_KEY: deepcopy(requirements)}

    error = _runtime_owned_llm_option_error(plugin_name, options, tool_name="test_tool")

    assert error is not None
    assert "interpretation_requirements" in error
    assert _SENSITIVE_SENTINEL not in error


@pytest.mark.parametrize("plugin_name", _PLUGIN_NAMES)
@pytest.mark.parametrize("field_name", _RESOLVER_OWNED_FIELDS)
@pytest.mark.parametrize("field_value", (None, _SENSITIVE_SENTINEL))
def test_runtime_guard_rejects_every_present_resolver_owned_field_without_leaking(
    plugin_name: str | None,
    field_name: str,
    field_value: Any,
) -> None:
    requirement: dict[str, Any] = _valid_authored_requirement()
    requirement[field_name] = field_value

    error = _runtime_owned_llm_option_error(
        plugin_name,
        {INTERPRETATION_REQUIREMENTS_KEY: [requirement]},
        tool_name="test_tool",
    )

    assert error is not None
    assert field_name in error
    assert _SENSITIVE_SENTINEL not in error


def test_source_guard_uses_the_same_exhaustive_admission_contract() -> None:
    error = _resolver_owned_interpretation_requirement_error(
        {INTERPRETATION_REQUIREMENTS_KEY: None},
        tool_name="set_source",
    )

    assert error is not None
    assert "interpretation_requirements" in error


@pytest.mark.parametrize(
    "requirements",
    (
        [_canonical_pending_requirement() | {"unknown": _SENSITIVE_SENTINEL}],
        [_canonical_pending_requirement() | {"status": _SENSITIVE_SENTINEL}],
        [_canonical_pending_requirement() | {"accepted_value": _SENSITIVE_SENTINEL}],
        [
            _canonical_pending_requirement()
            | {
                "status": "resolved",
                "event_id": None,
                "accepted_value": "approved",
            }
        ],
        [
            _canonical_pending_requirement(requirement_id="duplicate"),
            _canonical_pending_requirement(
                requirement_id="duplicate",
                kind="pipeline_decision",
                user_term="beta",
            ),
        ],
        [
            _canonical_pending_requirement(requirement_id="first"),
            _canonical_pending_requirement(
                requirement_id="second",
                user_term="  alpha  ",
            ),
        ],
    ),
    ids=(
        "unknown-field",
        "invalid-status",
        "pending-coherence",
        "resolved-coherence",
        "duplicate-id",
        "duplicate-normalized-identity",
    ),
)
def test_canonical_invariant_rejects_shape_domain_and_identity_failures_without_leaking(
    requirements: list[dict[str, Any]],
) -> None:
    invariant = common_tools._canonical_interpretation_requirement_error

    assert callable(invariant), "central canonical invariant B is missing"
    error = invariant(
        {INTERPRETATION_REQUIREMENTS_KEY: deepcopy(requirements)},
        tool_name="test_tool",
    )

    assert error is not None
    assert "interpretation_requirements_invalid" in error
    assert _SENSITIVE_SENTINEL not in error


def test_canonical_invariant_accepts_exact_coherent_pending_rows() -> None:
    invariant = common_tools._canonical_interpretation_requirement_error

    assert callable(invariant), "central canonical invariant B is missing"
    assert (
        invariant(
            {INTERPRETATION_REQUIREMENTS_KEY: [_canonical_pending_requirement()]},
            tool_name="test_tool",
        )
        is None
    )


@pytest.mark.parametrize(
    ("kind", "user_term", "required_hash_field", "wrong_hash_field"),
    (
        (
            "vague_term",
            "ambiguous term",
            "resolved_prompt_template_hash",
            "accepted_artifact_hash",
        ),
        (
            "invented_source",
            "inline_source_data",
            "accepted_artifact_hash",
            "resolved_prompt_template_hash",
        ),
        (
            "llm_prompt_template",
            "llm_prompt_template:node",
            "resolved_prompt_template_hash",
            "accepted_artifact_hash",
        ),
        (
            "pipeline_decision",
            "prompt_injection_shield_recommendation",
            "accepted_artifact_hash",
            "resolved_prompt_template_hash",
        ),
        (
            "llm_model_choice",
            "llm_model_choice:node",
            "resolved_prompt_template_hash",
            "accepted_artifact_hash",
        ),
        (
            "source_data_contract",
            "source_data_contract",
            "accepted_artifact_hash",
            "resolved_prompt_template_hash",
        ),
    ),
)
def test_canonical_invariant_requires_kind_appropriate_resolved_evidence(
    kind: str,
    user_term: str,
    required_hash_field: str,
    wrong_hash_field: str,
) -> None:
    invariant = common_tools._canonical_interpretation_requirement_error
    assert callable(invariant), "central canonical invariant B is missing"
    resolved = _canonical_pending_requirement(
        requirement_id=f"resolved:{kind}",
        kind=kind,
        user_term=user_term,
    ) | {
        "status": "resolved",
        "event_id": "event-1",
        "accepted_value": "approved",
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": None,
    }

    missing_error = invariant(
        {INTERPRETATION_REQUIREMENTS_KEY: [resolved]},
        tool_name="test_tool",
    )
    wrong_error = invariant(
        {
            INTERPRETATION_REQUIREMENTS_KEY: [
                resolved | {wrong_hash_field: "wrong-domain-hash"},
            ]
        },
        tool_name="test_tool",
    )
    empty_error = invariant(
        {
            INTERPRETATION_REQUIREMENTS_KEY: [
                resolved | {required_hash_field: ""},
            ]
        },
        tool_name="test_tool",
    )
    accepted_error = invariant(
        {
            INTERPRETATION_REQUIREMENTS_KEY: [
                resolved | {required_hash_field: "kind-appropriate-hash"},
            ]
        },
        tool_name="test_tool",
    )

    assert missing_error is not None
    assert wrong_error is not None
    assert empty_error is not None
    assert accepted_error is None


def _catalog() -> CatalogServiceImpl:
    manager = PluginManager()
    manager.register_builtin_plugins()
    return CatalogServiceImpl(manager)


def _execute(
    tool_name: str,
    arguments: dict[str, Any],
    state: CompositionState,
    *,
    catalog: CatalogServiceImpl,
    interpretation_requirements_are_internal: bool = False,
) -> Any:
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return _execute_tool(
        tool_name,
        arguments,
        state,
        PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
        _interpretation_requirements_are_internal=interpretation_requirements_are_internal,
    )


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _state_with_source() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": "/tmp/input.csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _state_with_node(
    plugin_name: str,
    *,
    extra_options: dict[str, Any] | None = None,
) -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id="existing",
                node_type="transform",
                plugin=plugin_name,
                input="rows",
                on_success="out",
                on_error="discard",
                options={"schema": {"mode": "observed"}, **(extra_options or {})},
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


def _splice_state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="to_successor",
            options={"path": "/tmp/input.csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="successor",
                node_type="transform",
                plugin="passthrough",
                input="to_successor",
                on_success="out",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(
            EdgeSpec(
                id="source_to_successor",
                from_node="source",
                to_node="successor",
                edge_type="on_success",
                label=None,
            ),
        ),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _with_poisoned_output(state: CompositionState) -> CompositionState:
    return replace(
        state,
        outputs=(
            OutputSpec(
                name="poisoned",
                plugin="json",
                options={
                    "path": "/tmp/poisoned.jsonl",
                    "schema": {"mode": "observed"},
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "poison": _SENSITIVE_SENTINEL,
                        }
                    ],
                },
                on_write_failure="discard",
            ),
        ),
    )


def _source_writer_result(
    writer: str,
    requirements: Any,
    *,
    catalog: CatalogServiceImpl,
) -> tuple[Any, CompositionState]:
    options = {
        "path": "/tmp/input.csv",
        "schema": {"mode": "observed"},
        INTERPRETATION_REQUIREMENTS_KEY: deepcopy(requirements),
    }
    if writer == "set_source":
        state = _empty_state()
        arguments = {
            "plugin": "csv",
            "on_success": "rows",
            "options": options,
            "on_validation_failure": "discard",
        }
        return _execute(writer, arguments, state, catalog=catalog), state
    if writer == "set_source_from_blob":
        state = _empty_state()
        arguments = {
            "blob_id": "00000000-0000-0000-0000-000000000000",
            "on_success": "rows",
            "options": {INTERPRETATION_REQUIREMENTS_KEY: deepcopy(requirements)},
        }
        return _execute(writer, arguments, state, catalog=catalog), state
    if writer == "patch_source_options":
        state = _state_with_source()
        arguments = {"patch": {INTERPRETATION_REQUIREMENTS_KEY: deepcopy(requirements)}}
        return _execute(writer, arguments, state, catalog=catalog), state
    state = _empty_state()
    source = {
        "plugin": "csv",
        "on_success": "rows",
        "options": options,
        "on_validation_failure": "discard",
    }
    arguments = {
        "source": source,
        "nodes": [],
        "edges": [],
        "outputs": [],
    }
    if writer == "set_pipeline_named_source":
        arguments["sources"] = {"orders": source}
        del arguments["source"]
    return _execute("set_pipeline", arguments, state, catalog=catalog), state


def _node_writer_result(
    writer: str,
    plugin_name: str,
    requirements: Any,
    *,
    catalog: CatalogServiceImpl,
    extra_options: dict[str, Any] | None = None,
) -> tuple[Any, CompositionState]:
    options = {
        "schema": {"mode": "observed"},
        **(extra_options or {}),
        INTERPRETATION_REQUIREMENTS_KEY: deepcopy(requirements),
    }
    if writer == "upsert_node":
        state = _empty_state()
        arguments = {
            "id": "candidate",
            "node_type": "transform",
            "plugin": plugin_name,
            "input": "rows",
            "on_success": "out",
            "on_error": "discard",
            "options": options,
        }
        return _execute(writer, arguments, state, catalog=catalog), state
    if writer == "patch_node_options":
        state = _state_with_node(plugin_name, extra_options=extra_options)
        arguments = {
            "node_id": "existing",
            "patch": {INTERPRETATION_REQUIREMENTS_KEY: deepcopy(requirements)},
        }
        return _execute(writer, arguments, state, catalog=catalog), state
    if writer == "splice_transform":
        state = _splice_state()
        arguments = {
            "predecessor_id": "source",
            "successor_id": "successor",
            "node": {
                "id": "inserted",
                "plugin": plugin_name,
                "options": options,
                "on_error": "discard",
            },
        }
        return _execute(writer, arguments, state, catalog=catalog), state
    state = _empty_state()
    arguments = {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": "/tmp/input.csv", "schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        },
        "nodes": [
            {
                "id": "candidate",
                "node_type": "transform",
                "plugin": plugin_name,
                "input": "rows",
                "on_success": "out",
                "on_error": "discard",
                "options": options,
            }
        ],
        "edges": [],
        "outputs": [],
    }
    return _execute("set_pipeline", arguments, state, catalog=catalog), state


@pytest.mark.parametrize(
    "writer",
    (
        "set_source",
        "set_source_from_blob",
        "patch_source_options",
        "set_pipeline_legacy_source",
        "set_pipeline_named_source",
    ),
)
@pytest.mark.parametrize(
    "requirements",
    (
        None,
        [_valid_authored_requirement() | {"status": "resolved"}],
    ),
    ids=("malformed-null", "resolver-owned-status"),
)
def test_every_source_writer_rejects_malformed_or_resolver_owned_requirements(
    writer: str,
    requirements: Any,
) -> None:
    catalog = _catalog()

    result, state = _source_writer_result(writer, requirements, catalog=catalog)

    assert result.success is False, (writer, result.to_dict())
    assert result.updated_state is state
    assert "interpretation_requirements" in result.validation.errors[0].message


@pytest.mark.parametrize(
    "writer",
    (
        "upsert_node",
        "patch_node_options",
        "splice_transform",
        "set_pipeline",
    ),
)
@pytest.mark.parametrize("plugin_name", ("llm", "passthrough"))
@pytest.mark.parametrize(
    "requirements",
    (
        None,
        [_valid_authored_requirement() | {"status": "resolved"}],
    ),
    ids=("malformed-null", "resolver-owned-status"),
)
def test_every_node_writer_rejects_for_llm_and_non_llm_plugins(
    writer: str,
    plugin_name: str,
    requirements: Any,
) -> None:
    catalog = _catalog()

    result, state = _node_writer_result(writer, plugin_name, requirements, catalog=catalog)

    assert result.success is False, (writer, plugin_name, result.to_dict())
    assert result.updated_state is state
    assert "interpretation_requirements" in result.validation.errors[0].message


_COLLIDING_REQUIREMENT_LISTS: Final[tuple[list[dict[str, str]], ...]] = (
    [
        {
            "kind": "vague_term",
            "user_term": _SENSITIVE_SENTINEL,
            "draft": "first draft",
        },
        {
            "kind": "vague_term",
            "user_term": _SENSITIVE_SENTINEL,
            "draft": "second draft",
        },
    ],
    [
        {
            "kind": "vague_term",
            "user_term": f"  {_SENSITIVE_SENTINEL}  ",
            "draft": "first draft",
        },
        {
            "kind": "vague_term",
            "user_term": _SENSITIVE_SENTINEL,
            "draft": "second draft",
        },
    ],
    [
        {
            "kind": "vague_term",
            "user_term": f"  {_SENSITIVE_SENTINEL}  ",
            "draft": "first draft",
        },
        {
            "kind": "pipeline_decision",
            "user_term": _SENSITIVE_SENTINEL,
            "draft": "second draft",
        },
    ],
)


@pytest.mark.parametrize("plugin_name", _PLUGIN_NAMES)
@pytest.mark.parametrize(
    "requirements",
    _COLLIDING_REQUIREMENT_LISTS,
    ids=("identical-shells", "normalized-kind-term", "projected-id-across-kinds"),
)
def test_runtime_guard_rejects_duplicate_or_projected_identity_collisions_without_leaking(
    plugin_name: str | None,
    requirements: list[dict[str, str]],
) -> None:
    error = _runtime_owned_llm_option_error(
        plugin_name,
        {INTERPRETATION_REQUIREMENTS_KEY: deepcopy(requirements)},
        tool_name="test_tool",
    )

    assert error is not None
    assert _IDENTITY_COLLISION_ERROR in error
    assert _SENSITIVE_SENTINEL not in error


def test_runtime_guard_allows_multiple_distinct_authored_requirements() -> None:
    error = _runtime_owned_llm_option_error(
        "passthrough",
        {
            INTERPRETATION_REQUIREMENTS_KEY: [
                {"kind": "vague_term", "user_term": "alpha", "draft": "first"},
                {
                    "kind": "pipeline_decision",
                    "user_term": "prompt_injection_shield_recommendation",
                    "draft": "second",
                },
            ]
        },
        tool_name="test_tool",
    )

    assert error is None


def test_runtime_guard_rejects_unregistered_pipeline_decision_with_bounded_repair() -> None:
    error = _runtime_owned_llm_option_error(
        "passthrough",
        {
            INTERPRETATION_REQUIREMENTS_KEY: [
                {
                    "kind": "pipeline_decision",
                    "user_term": "drop_raw_extracted_fields",
                    "draft": "Drop extracted document fields.",
                }
            ]
        },
        tool_name="upsert_node",
    )

    assert error is not None
    assert "pipeline_decision user_term is not registered" in error
    assert "closest registered term: 'drop_raw_html_fields'" in error
    assert "prompt_injection_shield_recommendation" in error
    assert "web_scrape_http_identity" in error
    assert "required_control_auto_wired" not in error


def test_runtime_guard_keeps_non_pipeline_user_terms_open() -> None:
    error = _runtime_owned_llm_option_error(
        "llm",
        {
            INTERPRETATION_REQUIREMENTS_KEY: [
                {
                    "kind": "vague_term",
                    "user_term": "customer-specific risk band",
                    "draft": "Risk band means the operator's current policy bands.",
                }
            ]
        },
        tool_name="upsert_node",
    )

    assert error is None


def test_runtime_guard_preserves_nominal_server_staged_required_control_authority() -> None:
    error = _runtime_owned_llm_option_error(
        "passthrough",
        {
            INTERPRETATION_REQUIREMENTS_KEY: [
                {
                    "kind": "pipeline_decision",
                    "user_term": ServerStagedRequiredControlUserTerm("required_control_auto_wired"),
                    "draft": "The server inserted the required control.",
                }
            ]
        },
        tool_name="required_control_finalizer",
    )

    assert error is None


def test_runtime_guard_rejects_plain_required_control_term_as_server_owned() -> None:
    error = _runtime_owned_llm_option_error(
        "passthrough",
        {
            INTERPRETATION_REQUIREMENTS_KEY: [
                {
                    "kind": "pipeline_decision",
                    "user_term": "required_control_auto_wired",
                    "draft": "The model claims it inserted the required control.",
                }
            ]
        },
        tool_name="upsert_node",
    )

    assert error is not None
    assert "server-owned user_term 'required_control_auto_wired'" in error


@pytest.mark.parametrize(
    "writer",
    (
        "upsert_node",
        "patch_node_options",
        "splice_transform",
        "set_pipeline",
    ),
)
def test_every_node_writer_rejects_unregistered_pipeline_decision_before_publication(writer: str) -> None:
    catalog = _catalog()
    requirements = [
        {
            "kind": "pipeline_decision",
            "user_term": "drop_raw_extracted_fields",
            "draft": "Drop extracted document fields.",
        }
    ]

    result, state = _node_writer_result(writer, "passthrough", requirements, catalog=catalog)

    assert result.success is False, (writer, result.to_dict())
    assert result.updated_state is state
    assert "pipeline_decision user_term is not registered" in result.validation.errors[0].message
    assert "closest registered term: 'drop_raw_html_fields'" in result.validation.errors[0].message
    assert "required_control_auto_wired" not in result.validation.errors[0].message
    assert state.nodes == result.updated_state.nodes


@pytest.mark.parametrize(
    "writer",
    (
        "set_source",
        "set_source_from_blob",
        "patch_source_options",
        "set_pipeline_legacy_source",
        "set_pipeline_named_source",
    ),
)
@pytest.mark.parametrize(
    "requirements",
    _COLLIDING_REQUIREMENT_LISTS,
    ids=("identical-shells", "normalized-kind-term", "projected-id-across-kinds"),
)
def test_every_source_writer_rejects_identity_collisions_before_round_trip_poisoning(
    writer: str,
    requirements: list[dict[str, str]],
) -> None:
    catalog = _catalog()

    result, state = _source_writer_result(writer, requirements, catalog=catalog)

    assert result.success is False, (writer, result.to_dict())
    assert result.updated_state is state
    assert _IDENTITY_COLLISION_ERROR in result.validation.errors[0].message
    assert _SENSITIVE_SENTINEL not in result.validation.errors[0].message
    _payload, round_trip_error = _serialize_set_pipeline_arguments(result.updated_state)
    assert round_trip_error is None


@pytest.mark.parametrize(
    "writer",
    (
        "upsert_node",
        "patch_node_options",
        "splice_transform",
        "set_pipeline",
    ),
)
@pytest.mark.parametrize("plugin_name", ("llm", "passthrough"))
@pytest.mark.parametrize(
    "requirements",
    _COLLIDING_REQUIREMENT_LISTS,
    ids=("identical-shells", "normalized-kind-term", "projected-id-across-kinds"),
)
def test_every_node_writer_rejects_identity_collisions_before_round_trip_poisoning(
    writer: str,
    plugin_name: str,
    requirements: list[dict[str, str]],
) -> None:
    catalog = _catalog()

    result, state = _node_writer_result(writer, plugin_name, requirements, catalog=catalog)

    assert result.success is False, (writer, plugin_name, result.to_dict())
    assert result.updated_state is state
    assert _IDENTITY_COLLISION_ERROR in result.validation.errors[0].message
    assert _SENSITIVE_SENTINEL not in result.validation.errors[0].message
    _payload, round_trip_error = _serialize_set_pipeline_arguments(result.updated_state)
    assert round_trip_error is None


@pytest.mark.parametrize("writer", ("set_source", "patch_source_options"))
def test_direct_source_writers_preserve_trusted_requirement_id(writer: str) -> None:
    catalog = _catalog()
    trusted_id = "trusted-source-review-id"
    canonical = _canonical_pending_requirement(
        requirement_id=trusted_id,
        kind="invented_source",
        user_term="trusted_source_assumption",
        draft="Review the source assumption.",
    )
    base = _state_with_source()
    current = base.sources["source"]
    state = base.with_named_source(
        "source",
        replace(
            current,
            options={
                **current.options,
                INTERPRETATION_REQUIREMENTS_KEY: [canonical],
            },
        ),
    )
    shell = {field: canonical[field] for field in ("kind", "user_term", "draft")}
    if writer == "set_source":
        arguments = {
            "plugin": "csv",
            "on_success": "rows",
            "options": {
                "path": "/tmp/input.csv",
                "schema": {"mode": "observed"},
                INTERPRETATION_REQUIREMENTS_KEY: [shell],
            },
            "on_validation_failure": "discard",
        }
    else:
        arguments = {
            "patch": {
                INTERPRETATION_REQUIREMENTS_KEY: [shell],
            }
        }

    result = _execute(writer, arguments, state, catalog=catalog)

    assert result.success, result.to_dict()
    retained = result.updated_state.sources["source"].options[INTERPRETATION_REQUIREMENTS_KEY]
    assert retained[0]["id"] == trusted_id


@pytest.mark.parametrize("writer", ("upsert_node", "patch_node_options"))
@pytest.mark.parametrize("display_title", (None, "Prompt injection protection"))
def test_direct_node_writers_preserve_trusted_requirement_id(writer: str, display_title: str | None) -> None:
    catalog = _catalog()
    trusted_id = "trusted-node-review-id"
    canonical = _canonical_pending_requirement(
        requirement_id=trusted_id,
        kind="pipeline_decision",
        user_term="prompt_injection_shield_recommendation",
        draft="Review the node decision.",
    )
    state = _state_with_node(
        "passthrough",
        extra_options={
            INTERPRETATION_REQUIREMENTS_KEY: [canonical],
        },
    )
    shell = {field: canonical[field] for field in ("kind", "user_term", "draft")}
    if display_title is not None:
        shell["display_title"] = display_title
    if writer == "upsert_node":
        arguments = {
            "id": "existing",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "rows",
            "on_success": "out",
            "on_error": "discard",
            "options": {
                "schema": {"mode": "observed"},
                INTERPRETATION_REQUIREMENTS_KEY: [shell],
            },
        }
    else:
        arguments = {
            "node_id": "existing",
            "patch": {
                INTERPRETATION_REQUIREMENTS_KEY: [shell],
            },
        }

    result = _execute(writer, arguments, state, catalog=catalog)

    assert result.success, result.to_dict()
    retained = result.updated_state.nodes[0].options[INTERPRETATION_REQUIREMENTS_KEY]
    assert retained[0]["id"] == trusted_id
    if display_title is not None:
        assert retained[0]["display_title"] == display_title


@pytest.mark.parametrize("writer", ("upsert_node", "patch_node_options"))
def test_direct_llm_writers_preserve_trusted_auto_staged_requirement_ids(writer: str) -> None:
    catalog = _catalog()
    old_prompt = "Summarise {{ row.text }}."
    # Must be a real openrouter catalog entry: afe354ee4 stopped short-circuiting
    # value-source checks for deferred-secret configs, so this value is now verified.
    old_model = "openai/gpt-4o"
    prompt_id = "trusted-prompt-review-id"
    model_id = "trusted-model-review-id"
    state = _state_with_node(
        "llm",
        extra_options={
            "provider": "openrouter",
            "model": old_model,
            "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
            "prompt_template": old_prompt,
            "required_input_fields": ["text"],
            INTERPRETATION_REQUIREMENTS_KEY: [
                _canonical_pending_requirement(
                    requirement_id=prompt_id,
                    kind="llm_prompt_template",
                    user_term="llm_prompt_template:existing",
                    draft=old_prompt,
                ),
                _canonical_pending_requirement(
                    requirement_id=model_id,
                    kind="llm_model_choice",
                    user_term="llm_model_choice:existing",
                    draft=old_model,
                ),
            ],
        },
    )
    if writer == "upsert_node":
        arguments = {
            "id": "existing",
            "node_type": "transform",
            "plugin": "llm",
            "input": "rows",
            "on_success": "out",
            "on_error": "discard",
            "options": {
                "provider": "openrouter",
                "model": old_model,
                "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                "prompt_template": old_prompt,
                "required_input_fields": ["text"],
                "schema": {"mode": "observed"},
            },
        }
    else:
        arguments = {
            "node_id": "existing",
            "patch": {
                "model": "openai/gpt-4.1-mini",
                "prompt_template": "Classify {{ row.text }}.",
            },
        }

    result = _execute(writer, arguments, state, catalog=catalog)

    assert result.success, result.to_dict()
    requirements = result.updated_state.nodes[0].options[INTERPRETATION_REQUIREMENTS_KEY]
    ids_by_kind = {requirement["kind"]: requirement["id"] for requirement in requirements}
    assert ids_by_kind == {
        "llm_prompt_template": prompt_id,
        "llm_model_choice": model_id,
    }


@pytest.mark.parametrize(
    ("writer", "state_factory", "arguments"),
    (
        (
            "set_source",
            _empty_state,
            {
                "plugin": "csv",
                "on_success": "rows",
                "options": {
                    "path": "/tmp/input.csv",
                    "schema": {"mode": "observed"},
                },
                "on_validation_failure": "discard",
            },
        ),
        (
            "patch_source_options",
            _state_with_source,
            {
                "patch": {
                    "schema": {"mode": "observed"},
                },
            },
        ),
        (
            "upsert_node",
            _empty_state,
            {
                "id": "candidate",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "rows",
                "on_success": "out",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                },
            },
        ),
        (
            "patch_node_options",
            lambda: _state_with_node("passthrough"),
            {
                "node_id": "existing",
                "patch": {
                    "schema": {"mode": "observed"},
                },
            },
        ),
        (
            "set_metadata",
            _empty_state,
            {
                "patch": {
                    "name": "Still poisoned",
                },
            },
        ),
    ),
)
def test_public_composition_mutations_reject_preexisting_output_review_metadata(
    writer: str,
    state_factory: Callable[[], CompositionState],
    arguments: dict[str, Any],
) -> None:
    catalog = _catalog()
    state = _with_poisoned_output(state_factory())

    result = _execute(writer, arguments, state, catalog=catalog)

    assert result.success is False
    assert result.updated_state is state
    assert result.validation.errors[0].error_code == "interpretation_requirements_invalid"
    assert _SENSITIVE_SENTINEL not in result.validation.errors[0].message


def test_composition_gate_registry_covers_every_public_state_mutation() -> None:
    from elspeth.web.composer.redaction import SetPipelineArgumentsModel
    from elspeth.web.composer.tools._registry import (
        _MUTATION_TOOL_NAMES,
        _REGISTERED_TOOLS,
        _SECRET_MUTATION_TOOL_NAMES,
        resolve_tool_effects,
    )
    from elspeth.web.composer.tools.declarations import EffectDomain

    pipeline_arguments = {
        "source": {"plugin": "csv", "on_success": "rows", "on_validation_failure": "discard"},
        "nodes": [],
        "edges": [],
        "outputs": [],
    }
    SetPipelineArgumentsModel.model_validate(pipeline_arguments)
    covered = frozenset(
        decl.name
        for decl in _REGISTERED_TOOLS
        if EffectDomain.GRAPH in resolve_tool_effects(decl.name, pipeline_arguments if decl.name == "set_pipeline" else {}).domains
    )

    assert covered == (
        _MUTATION_TOOL_NAMES
        | _SECRET_MUTATION_TOOL_NAMES
        | frozenset(
            {
                "set_source_from_blob",
                "set_source_from_blobs",
                "wire_blob_inline_ref",
            }
        )
    )


@pytest.mark.parametrize("writer", ("upsert_node", "splice_transform", "set_pipeline"))
@pytest.mark.parametrize(
    ("extra_options", "colliding_user_term"),
    (
        ({"prompt_template": "Summarise {{ row.text }}"}, "prompt_template_review"),
        ({"model": "test/model"}, "model_choice_review"),
    ),
    ids=("prompt-auto-stage", "model-auto-stage"),
)
def test_node_writers_enforce_canonical_identity_after_automatic_review_staging(
    writer: str,
    extra_options: dict[str, Any],
    colliding_user_term: str,
) -> None:
    catalog = _catalog()
    requirements = [
        {
            "kind": "vague_term",
            "user_term": colliding_user_term,
            "draft": _SENSITIVE_SENTINEL,
        }
    ]

    result, state = _node_writer_result(
        writer,
        "llm",
        requirements,
        catalog=catalog,
        extra_options=extra_options,
    )

    assert result.success is False
    assert result.updated_state is state
    assert "interpretation_requirements_invalid" in result.validation.errors[0].message
    assert _SENSITIVE_SENTINEL not in result.validation.errors[0].message


@pytest.mark.parametrize(
    ("field_name", "trusted_id", "trusted_term", "patched_value"),
    (
        (
            "prompt_template",
            "prompt_template_review:existing",
            "trusted-prompt-review",
            "Rewrite {{ row.text }}",
        ),
        (
            "model",
            "model_choice_review:existing",
            "trusted-model-review",
            "test/other-model",
        ),
    ),
    ids=("prompt-review-id", "model-review-id"),
)
def test_patch_without_requirements_rejects_auto_stager_collision_with_trusted_row(
    field_name: str,
    trusted_id: str,
    trusted_term: str,
    patched_value: str,
) -> None:
    catalog = _catalog()
    state = _state_with_node(
        "llm",
        extra_options={
            field_name: "original value",
            INTERPRETATION_REQUIREMENTS_KEY: [
                _canonical_pending_requirement(
                    requirement_id=trusted_id,
                    user_term=trusted_term,
                    draft=_SENSITIVE_SENTINEL,
                )
            ],
        },
    )

    result = _execute(
        "patch_node_options",
        {"node_id": "existing", "patch": {field_name: patched_value}},
        state,
        catalog=catalog,
    )

    assert result.success is False, result.to_dict()
    assert result.updated_state is state
    assert "interpretation_requirements_invalid" in result.validation.errors[0].message
    assert _SENSITIVE_SENTINEL not in result.validation.errors[0].message
    _payload, round_trip_error = _serialize_set_pipeline_arguments(result.updated_state)
    assert round_trip_error is None


def test_patch_source_without_requirements_enforces_b_after_merge() -> None:
    catalog = _catalog()
    base = _state_with_source()
    current_source = base.sources["source"]
    state = base.with_named_source(
        "source",
        replace(
            current_source,
            options={
                **current_source.options,
                INTERPRETATION_REQUIREMENTS_KEY: [
                    _canonical_pending_requirement(requirement_id="duplicate", user_term="alpha"),
                    _canonical_pending_requirement(
                        requirement_id="duplicate",
                        kind="pipeline_decision",
                        user_term="beta",
                        draft=_SENSITIVE_SENTINEL,
                    ),
                ],
            },
        ),
    )

    result = _execute(
        "patch_source_options",
        {"patch": {"schema": {"mode": "observed"}}},
        state,
        catalog=catalog,
    )

    assert result.success is False
    assert result.updated_state is state
    assert "interpretation_requirements_invalid" in result.validation.errors[0].message
    assert _SENSITIVE_SENTINEL not in result.validation.errors[0].message


def test_splice_enforces_b_again_after_final_reconciliation() -> None:
    catalog = _catalog()
    base = _splice_state()
    successor = base.nodes[0]
    state = replace(
        base,
        nodes=(
            replace(
                successor,
                options={
                    **successor.options,
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        _canonical_pending_requirement(requirement_id="duplicate", user_term="alpha"),
                        _canonical_pending_requirement(
                            requirement_id="duplicate",
                            kind="pipeline_decision",
                            user_term="beta",
                            draft=_SENSITIVE_SENTINEL,
                        ),
                    ],
                },
            ),
        ),
    )

    result = _execute(
        "splice_transform",
        {
            "predecessor_id": "source",
            "successor_id": "successor",
            "node": {
                "id": "inserted",
                "plugin": "passthrough",
                "options": {"schema": {"mode": "observed"}},
                "on_error": "discard",
            },
        },
        state,
        catalog=catalog,
    )

    assert result.success is False
    assert result.updated_state is state
    assert "interpretation_requirements_invalid" in result.validation.errors[0].message
    assert _SENSITIVE_SENTINEL not in result.validation.errors[0].message


def test_internal_revalidation_skips_raw_admission_but_never_canonical_invariant() -> None:
    catalog = _catalog()
    state = _empty_state()
    malformed_canonical = _canonical_pending_requirement() | {"status": _SENSITIVE_SENTINEL}

    result = _execute(
        "set_pipeline",
        {
            "source": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"path": "/tmp/input.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            "nodes": [
                {
                    "id": "candidate",
                    "node_type": "transform",
                    "plugin": "passthrough",
                    "input": "rows",
                    "on_success": "out",
                    "on_error": "discard",
                    "options": {
                        "schema": {"mode": "observed"},
                        INTERPRETATION_REQUIREMENTS_KEY: [malformed_canonical],
                    },
                }
            ],
            "edges": [],
            "outputs": [],
        },
        state,
        catalog=catalog,
        interpretation_requirements_are_internal=True,
    )

    assert result.success is False
    assert result.updated_state is state
    assert "interpretation_requirements_invalid" in result.validation.errors[0].message
    assert _SENSITIVE_SENTINEL not in result.validation.errors[0].message


def test_internal_revalidation_normalizes_trusted_legacy_pending_row_once() -> None:
    catalog = _catalog()
    state = _empty_state()
    legacy_pending = {
        "id": "trusted-legacy-id",
        "kind": "pipeline_decision",
        "user_term": "trusted_legacy_term",
        "status": "pending",
        "draft": "Review the trusted legacy decision.",
    }

    result = _execute(
        "set_pipeline",
        {
            "source": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"path": "/tmp/input.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            "nodes": [
                {
                    "id": "candidate",
                    "node_type": "transform",
                    "plugin": "passthrough",
                    "input": "rows",
                    "on_success": "out",
                    "on_error": "discard",
                    "options": {
                        "schema": {"mode": "observed"},
                        INTERPRETATION_REQUIREMENTS_KEY: [legacy_pending],
                    },
                }
            ],
            "edges": [],
            "outputs": [],
        },
        state,
        catalog=catalog,
        interpretation_requirements_are_internal=True,
    )

    assert result.success is True, result.to_dict()
    requirement = result.updated_state.nodes[0].options[INTERPRETATION_REQUIREMENTS_KEY][0]
    assert set(requirement) == {
        "id",
        "kind",
        "user_term",
        "draft",
        "status",
        "event_id",
        "accepted_value",
        "accepted_artifact_hash",
        "resolved_prompt_template_hash",
    }
    assert requirement["id"] == "trusted-legacy-id"
    assert requirement["event_id"] is None


def test_canonical_id_projection_normalizes_user_term_once() -> None:
    catalog = _catalog()
    result, _state = _node_writer_result(
        "upsert_node",
        "passthrough",
        [
            {
                "kind": "vague_term",
                "user_term": "  alpha  ",
                "draft": "alpha means the first category",
            }
        ],
        catalog=catalog,
        # A staged pending vague_term must be wired or the review contract
        # rejects the mutation; the ref names the canonical id this test pins
        # (trimmed user_term + ":" + node id).
        extra_options={
            PROMPT_TEMPLATE_PARTS_KEY: [
                {"kind": "text", "text": "Classify as "},
                {"kind": "interpretation_ref", "requirement_id": "alpha:candidate"},
            ],
        },
    )

    assert result.success is True, result.to_dict()
    requirement = result.updated_state.nodes[0].options[INTERPRETATION_REQUIREMENTS_KEY][0]
    assert requirement["id"] == "alpha:candidate"


def test_runtime_hash_remains_llm_only() -> None:
    assert (
        _runtime_owned_llm_option_error(
            "passthrough",
            {"approved_prompt_artifact_hash": _SENSITIVE_SENTINEL},
            tool_name="upsert_node",
        )
        is None
    )
    error = _runtime_owned_llm_option_error(
        "llm",
        {"approved_prompt_artifact_hash": _SENSITIVE_SENTINEL},
        tool_name="upsert_node",
    )
    assert error is not None
    assert _SENSITIVE_SENTINEL not in error


@pytest.mark.parametrize(
    ("requirements", "expected_error"),
    (
        (None, "must be a list of review entry objects"),
        (
            [_valid_authored_requirement() | {"status": _SENSITIVE_SENTINEL}],
            "resolver-owned field(s): status",
        ),
    ),
    ids=("malformed-null", "resolver-owned-status"),
)
def test_queue_upsert_reaches_plugin_agnostic_review_admission_without_leaking(
    requirements: Any,
    expected_error: str,
) -> None:
    catalog = _catalog()
    state = _empty_state()
    result = _execute(
        "upsert_node",
        {
            "id": "inbound",
            "node_type": "queue",
            "plugin": None,
            "input": "inbound",
            "on_success": None,
            "on_error": None,
            "options": {INTERPRETATION_REQUIREMENTS_KEY: deepcopy(requirements)},
        },
        state,
        catalog=catalog,
    )

    assert result.success is False
    assert result.updated_state is state
    assert expected_error in result.validation.errors[0].message
    assert _SENSITIVE_SENTINEL not in result.validation.errors[0].message


def test_queue_upsert_preserves_canonical_unknown_option_error_after_review_admission() -> None:
    catalog = _catalog()
    state = _empty_state()
    result = _execute(
        "upsert_node",
        {
            "id": "inbound",
            "node_type": "queue",
            "plugin": None,
            "input": "inbound",
            "on_success": None,
            "on_error": None,
            "options": {"buffer": 10},
        },
        state,
        catalog=catalog,
    )

    assert result.success is False
    assert result.updated_state is state
    assert "unknown option" in result.validation.errors[0].message


# ---------------------------------------------------------------------------
# Prompt-template review auto-stager on a multi-query node (session 94f6f00c):
# the staged draft is the rendered prompt SURFACE, and a query-template or
# system-prompt edit re-stages the review while an identical mutation does not.
# ---------------------------------------------------------------------------

_MULTI_QUERY_OPTIONS: Final[dict[str, Any]] = {
    "prompt_template": "Answer the question about the colour {{ row.colour }} in one short reply.",
    "system_prompt": "Reply with only the value asked for and nothing else.",
    "queries": {
        "good_pair": {
            "input_fields": {"colour": "colour"},
            "template": "What is a good colour pair for {{ row.colour }}? Reply with the single colour name only.",
        },
    },
    "required_input_fields": ["colour"],
}


def _staged_prompt_review(options: Any) -> dict[str, Any]:
    requirements = options["interpretation_requirements"]
    matches = [dict(entry) for entry in requirements if entry["kind"] == "llm_prompt_template"]
    assert len(matches) == 1
    return matches[0]


def test_prompt_review_auto_stager_drafts_the_multi_query_prompt_surface() -> None:
    from elspeth.web.interpretation_state import prompt_review_draft_from_options

    staged = common_tools._options_with_default_prompt_template_review(
        node_id="colour_questions", plugin="llm", options=_MULTI_QUERY_OPTIONS
    )
    requirement = _staged_prompt_review(staged)

    assert requirement["status"] == "pending"
    assert requirement["draft"] == prompt_review_draft_from_options(_MULTI_QUERY_OPTIONS)
    assert "Query 'good_pair':" in requirement["draft"]
    assert "System prompt (sent with every query):" in requirement["draft"]
    assert "not used (every query supplies its own template)" in requirement["draft"]
    # Single-prompt nodes keep drafting the prompt_template itself.
    single = common_tools._options_with_default_prompt_template_review(
        node_id="rate", plugin="llm", options={"prompt_template": "Rate {{ row.text }}."}
    )
    assert _staged_prompt_review(single)["draft"] == "Rate {{ row.text }}."


def test_prompt_review_auto_stager_restages_on_a_query_template_edit_but_not_on_a_replay() -> None:
    first = common_tools._options_with_default_prompt_template_review(
        node_id="colour_questions", plugin="llm", options=_MULTI_QUERY_OPTIONS
    )
    first_requirement = _staged_prompt_review(first)

    replay = common_tools._options_with_default_prompt_template_review(
        node_id="colour_questions", plugin="llm", options=dict(first), existing_options=first
    )
    assert _staged_prompt_review(replay) == first_requirement

    edited = deepcopy(dict(first))
    edited["queries"]["good_pair"]["template"] = "Name a colour that pairs with {{ row.colour }}. One word."
    restaged = common_tools._options_with_default_prompt_template_review(
        node_id="colour_questions", plugin="llm", options=edited, existing_options=first
    )
    restaged_requirement = _staged_prompt_review(restaged)
    assert restaged_requirement["draft"] != first_requirement["draft"]
    assert "Name a colour that pairs with {{ row.colour }}. One word." in restaged_requirement["draft"]
    assert restaged_requirement["status"] == "pending"

    system_edit = deepcopy(dict(first))
    system_edit["system_prompt"] = "Be terse."
    assert (
        _staged_prompt_review(
            common_tools._options_with_default_prompt_template_review(
                node_id="colour_questions", plugin="llm", options=system_edit, existing_options=first
            )
        )["draft"]
        != first_requirement["draft"]
    )
