"""Authoring admission for interpretation requirements is plugin-agnostic.

The exhaustive shape matrix belongs at the centralized guard.  The production
writer matrix then proves that every source/node mutation reaches that guard
before plugin validation, blob lookup, merge-patching, or state publication.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from typing import Any, Final

import pytest

from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.service import CatalogServiceImpl
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
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    ServerStagedRequiredControlUserTerm,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

_SENSITIVE_SENTINEL: Final[str] = "sk-sensitive-requirement-value"
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
    assert "interpretation_requirements" in result.data["error"]


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
    assert "interpretation_requirements" in result.data["error"]


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
    assert "pipeline_decision user_term is not registered" in result.data["error"]
    assert "closest registered term: 'drop_raw_html_fields'" in result.data["error"]
    assert "required_control_auto_wired" not in result.data["error"]
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
    assert _IDENTITY_COLLISION_ERROR in result.data["error"]
    assert _SENSITIVE_SENTINEL not in result.data["error"]
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
    assert _IDENTITY_COLLISION_ERROR in result.data["error"]
    assert _SENSITIVE_SENTINEL not in result.data["error"]
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
def test_direct_node_writers_preserve_trusted_requirement_id(writer: str) -> None:
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


@pytest.mark.parametrize("writer", ("upsert_node", "patch_node_options"))
def test_direct_llm_writers_preserve_trusted_auto_staged_requirement_ids(writer: str) -> None:
    catalog = _catalog()
    old_prompt = "Summarise {{ row.text }}."
    old_model = "openai/gpt-4o-mini"
    prompt_id = "trusted-prompt-review-id"
    model_id = "trusted-model-review-id"
    state = _state_with_node(
        "llm",
        extra_options={
            "provider": "openrouter",
            "model": old_model,
            "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
            "prompt_template": old_prompt,
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
    assert result.data["error_code"] == "interpretation_requirements_invalid"
    assert _SENSITIVE_SENTINEL not in result.data["error"]


def test_composition_gate_registry_covers_every_public_state_mutation() -> None:
    from elspeth.web.composer.tools._dispatch import _COMPOSITION_STATE_MUTATION_TOOL_NAMES
    from elspeth.web.composer.tools._registry import (
        _MUTATION_TOOL_NAMES,
        _SECRET_MUTATION_TOOL_NAMES,
    )

    covered = _COMPOSITION_STATE_MUTATION_TOOL_NAMES

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
    assert "interpretation_requirements_invalid" in result.data["error"]
    assert _SENSITIVE_SENTINEL not in result.data["error"]


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
    assert "interpretation_requirements_invalid" in result.data["error"]
    assert _SENSITIVE_SENTINEL not in result.data["error"]
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
    assert "interpretation_requirements_invalid" in result.data["error"]
    assert _SENSITIVE_SENTINEL not in result.data["error"]


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
    assert "interpretation_requirements_invalid" in result.data["error"]
    assert _SENSITIVE_SENTINEL not in result.data["error"]


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
    assert "interpretation_requirements_invalid" in result.data["error"]
    assert _SENSITIVE_SENTINEL not in result.data["error"]


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
    )

    assert result.success is True, result.to_dict()
    requirement = result.updated_state.nodes[0].options[INTERPRETATION_REQUIREMENTS_KEY][0]
    assert requirement["id"] == "alpha:candidate"


def test_runtime_hash_remains_llm_only() -> None:
    assert (
        _runtime_owned_llm_option_error(
            "passthrough",
            {"resolved_prompt_template_hash": _SENSITIVE_SENTINEL},
            tool_name="upsert_node",
        )
        is None
    )
    error = _runtime_owned_llm_option_error(
        "llm",
        {"resolved_prompt_template_hash": _SENSITIVE_SENTINEL},
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
    assert expected_error in result.data["error"]
    assert _SENSITIVE_SENTINEL not in result.data["error"]


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
    assert "unknown option" in result.data["error"]
