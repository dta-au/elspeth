"""Tests for static_llm_prompt_advisory — flags llm transform nodes whose
prompt_template interpolates no row data.

Scope boundary of elspeth-bea314a89b (closed): that fix rejects prompt
templates with unbound bare variables but deliberately ACCEPTS a fully
static template (``test_accepts_bound_or_static_templates`` in
tests/unit/web/composer/test_state.py pins
``"Static prompt with no interpolation at all."`` as valid). This advisory
is the non-blocking follow-up (elspeth-6bdb7e7736): a static template is
syntactically legal, but every row gets an identical prompt, which is
almost always an authoring mistake. It mirrors identity_node_advisory's
contract exactly: registered in VALIDATION_CHECK_NAMES, present in the
advisory list, absent from VALIDATION_BLOCKING_CHECK_NAMES and _ALL_CHECKS,
``passed=True`` always, emitted only on the happy path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, create_autospec, patch

from elspeth.core.dag.graph import ExecutionGraph
from elspeth.plugins.infrastructure.runtime_factory import PluginBundle
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.config import WebSettings
from elspeth.web.execution._validation_diagnostics import _find_static_llm_prompt_advisories
from elspeth.web.execution.schemas import (
    CHECK_STATIC_LLM_PROMPT_ADVISORY,
    VALIDATION_BLOCKING_CHECK_NAMES,
    VALIDATION_CHECK_NAMES,
)
from elspeth.web.execution.validation import (
    _ALL_CHECKS,
    validate_pipeline_for_trained_operator,
)

# ── Fixture builders ────────────────────────────────────────────────────


def _make_llm_node(
    node_id: str = "classify",
    prompt_template: str = "Static prompt with no interpolation at all.",
    on_success: str = "json_out",
    input_field: str = "page_in",
    options_overrides: dict[str, Any] | None = None,
) -> NodeSpec:
    # No "model" — a set model value auto-stages an llm_model_choice
    # interpretation review that would block validation before reaching this
    # advisory's happy-path block, for reasons unrelated to this advisory
    # (see TestValidatePipelineLlmBaseUrlPolicy._llm_options in
    # tests/unit/web/execution/test_validation.py for the same avoidance).
    options: dict[str, Any] = {"prompt_template": prompt_template}
    if options_overrides:
        options.update(options_overrides)
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="llm",
        input=input_field,
        on_success=on_success,
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _make_observed_sink(name: str = "json_out") -> OutputSpec:
    return OutputSpec(
        name=name,
        plugin="json",
        options={"path": "out.json", "schema": {"mode": "observed"}},
        on_write_failure="discard",
    )


def _make_state_with(
    nodes: tuple[NodeSpec, ...],
    outputs: tuple[OutputSpec, ...],
    source: SourceSpec | None = None,
) -> CompositionState:
    return CompositionState(
        source=source
        or SourceSpec(
            plugin="csv",
            on_success="page_in",
            options={"path": "in.csv"},
            on_validation_failure="discard",
        ),
        nodes=nodes,
        edges=(),
        outputs=outputs,
        metadata=PipelineMetadata(),
        version=1,
    )


# ── Constant + closed-vocabulary registration ───────────────────────────


def test_check_constant_value() -> None:
    """The check name string is the public contract — frontend and LLM both read it."""
    assert CHECK_STATIC_LLM_PROMPT_ADVISORY == "static_llm_prompt_advisory"


def test_check_registered_in_validation_check_names_but_not_blocking() -> None:
    """Closed-vocabulary parity: mirrors identity_node_advisory exactly —
    registered in VALIDATION_CHECK_NAMES, absent from
    VALIDATION_BLOCKING_CHECK_NAMES and the _ALL_CHECKS skip-propagation list."""
    assert CHECK_STATIC_LLM_PROMPT_ADVISORY in VALIDATION_CHECK_NAMES
    assert CHECK_STATIC_LLM_PROMPT_ADVISORY not in VALIDATION_BLOCKING_CHECK_NAMES
    assert CHECK_STATIC_LLM_PROMPT_ADVISORY not in _ALL_CHECKS


def test_helper_returns_empty_list_for_empty_state() -> None:
    state = CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    assert _find_static_llm_prompt_advisories(state) == []


# ── Detection — positive cases ───────────────────────────────────────────


def test_fully_static_template_is_flagged() -> None:
    """The pinned-acceptance shape: no interpolation at all."""
    state = _make_state_with(
        nodes=(_make_llm_node(prompt_template="Static prompt with no interpolation at all."),),
        outputs=(_make_observed_sink(),),
    )
    findings = _find_static_llm_prompt_advisories(state)
    assert len(findings) == 1
    assert findings[0].node_id == "classify"


def test_lookup_only_template_is_flagged() -> None:
    """lookup data is fixed at PromptTemplate construction from a static YAML
    file (see templates.py PromptTemplate.__init__ docstring: "Static lookup
    data from YAML file") and does not vary per row, so a template that reads
    only 'lookup.*' renders identically for every row — same failure mode as
    a fully static template."""
    state = _make_state_with(
        nodes=(_make_llm_node(prompt_template="Instructions: {{ lookup.instructions }}"),),
        outputs=(_make_observed_sink(),),
    )
    findings = _find_static_llm_prompt_advisories(state)
    assert len(findings) == 1


def test_env_global_only_template_is_flagged() -> None:
    """A template that only references environment globals (no row, no
    lookup) is likewise static across every row."""
    state = _make_state_with(
        nodes=(_make_llm_node(prompt_template="{{ range(3) | join(', ') }}"),),
        outputs=(_make_observed_sink(),),
    )
    findings = _find_static_llm_prompt_advisories(state)
    assert len(findings) == 1


def test_interpolation_placeholder_only_template_is_flagged() -> None:
    """{{interpretation:...}} placeholders are masked before parsing (they
    resolve to operator-accepted text upstream of rendering, same as the
    sibling unbound-variable guard) — a template using only those still
    interpolates no row data."""
    state = _make_state_with(
        nodes=(_make_llm_node(prompt_template="Rate how {{interpretation:cool}} this row is."),),
        outputs=(_make_observed_sink(),),
    )
    findings = _find_static_llm_prompt_advisories(state)
    assert len(findings) == 1


# ── Detection — negative cases ───────────────────────────────────────────


def test_row_field_reference_is_not_flagged() -> None:
    state = _make_state_with(
        nodes=(_make_llm_node(prompt_template="Classify: {{ row.text }}"),),
        outputs=(_make_observed_sink(),),
    )
    assert _find_static_llm_prompt_advisories(state) == []


def test_dynamic_row_indexing_is_not_flagged() -> None:
    """row[expr] does not populate the concrete field-usage set but still
    references 'row' as a top-level name — must not be misread as static."""
    state = _make_state_with(
        nodes=(_make_llm_node(prompt_template="{% for x in row %}{{ x }}{% endfor %}"),),
        outputs=(_make_observed_sink(),),
    )
    assert _find_static_llm_prompt_advisories(state) == []


def test_multi_query_node_is_not_flagged() -> None:
    """Multi-query nodes render each query's own per-query context
    (build_template_context in multi_query.py); the node-level
    prompt_template is out of scope here exactly as it is for the sibling
    unbound-variable guard (``queries is not None`` short-circuits)."""
    state = _make_state_with(
        nodes=(
            _make_llm_node(
                prompt_template="Static prompt with no interpolation at all.",
                options_overrides={
                    "queries": {
                        "q1": {"template": "{{ row.a }}", "input_fields": {"a": "field_a"}},
                    }
                },
            ),
        ),
        outputs=(_make_observed_sink(),),
    )
    assert _find_static_llm_prompt_advisories(state) == []


def test_non_llm_transform_is_not_flagged() -> None:
    node = NodeSpec(
        id="vt",
        node_type="transform",
        plugin="value_transform",
        input="page_in",
        on_success="json_out",
        on_error="discard",
        options={"operations": [{"target": "x", "expression": "1"}]},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    state = _make_state_with(nodes=(node,), outputs=(_make_observed_sink(),))
    assert _find_static_llm_prompt_advisories(state) == []


def test_missing_prompt_template_is_not_flagged() -> None:
    """Absence is a different, already-covered failure mode (missing prompt
    warning elsewhere) — this rule stays silent per the observation_boundary
    contract mirrored from the sibling guard."""
    node = _make_llm_node()
    options = dict(node.options)
    del options["prompt_template"]
    node = NodeSpec(
        id=node.id,
        node_type=node.node_type,
        plugin=node.plugin,
        input=node.input,
        on_success=node.on_success,
        on_error=node.on_error,
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    state = _make_state_with(nodes=(node,), outputs=(_make_observed_sink(),))
    assert _find_static_llm_prompt_advisories(state) == []


def test_unparseable_template_is_not_flagged() -> None:
    """Syntax errors are another layer's business — this rule stays silent."""
    state = _make_state_with(
        nodes=(_make_llm_node(prompt_template="Classify: {{ text"),),
        outputs=(_make_observed_sink(),),
    )
    assert _find_static_llm_prompt_advisories(state) == []


# ── Integration — wired into validate_pipeline_for_trained_operator() ────


def _make_settings(data_dir: str = "/tmp/test_data") -> WebSettings:
    return WebSettings(
        data_dir=Path(data_dir),
        composer_max_composition_turns=10,
        composer_max_discovery_turns=5,
        composer_timeout_seconds=30.0,
        composer_rate_limit_per_minute=60,
        shareable_link_signing_key=b"\x00" * 32,
    )


def _allowlisted_source(data_dir: str = "/tmp/test_data") -> SourceSpec:
    return SourceSpec(
        plugin="csv",
        on_success="page_in",
        options={"path": f"{data_dir}/blobs/test-session/in.csv"},
        on_validation_failure="discard",
    )


def _allowlisted_observed_sink(
    name: str = "json_out",
    data_dir: str = "/tmp/test_data",
) -> OutputSpec:
    return OutputSpec(
        name=name,
        plugin="json",
        options={"path": f"{data_dir}/outputs/test-session/out.json", "schema": {"mode": "observed"}},
        on_write_failure="discard",
    )


class _YamlGeneratorDouble:
    def generate_yaml(self, state: CompositionState) -> str:
        return "source:\n  plugin: csv_source\n  options: {}"


def _runtime_bundle_double() -> PluginBundle:
    return cast(PluginBundle, create_autospec(PluginBundle, instance=True))


def _runtime_graph_double() -> MagicMock:
    graph = cast(MagicMock, create_autospec(ExecutionGraph, instance=True))
    graph.validation_warnings = ()
    return graph


@patch("elspeth.web.execution.validation.assemble_and_validate_pipeline_config")
@patch("elspeth.web.execution.validation.load_settings_from_yaml_string")
@patch("elspeth.web.execution.validation.instantiate_runtime_plugins")
@patch("elspeth.web.execution.validation.build_runtime_graph")
def test_validate_pipeline_emits_advisory_and_does_not_block(
    mock_build_graph: MagicMock,
    mock_instantiate: MagicMock,
    mock_load: MagicMock,
    mock_assemble: MagicMock,
) -> None:
    """End-to-end: the advisory appears in ValidationResult.checks with
    passed=True and is_valid stays True — the critical non-blocking assertion."""
    yaml_gen = _YamlGeneratorDouble()
    mock_load.return_value = object()
    mock_instantiate.return_value = _runtime_bundle_double()
    mock_build_graph.return_value = _runtime_graph_double()
    mock_assemble.return_value = object()

    state = _make_state_with(
        source=_allowlisted_source(),
        nodes=(_make_llm_node(prompt_template="Static prompt with no interpolation at all."),),
        outputs=(_allowlisted_observed_sink(),),
    )
    # allow_pending_interpretation_placeholders masks the llm node's pending
    # llm_prompt_template/llm_model_choice reviews (same authoring-preflight
    # flag used by TestSequentialMultiQueryLlm in test_validation.py) so
    # validation reaches this advisory's happy-path block instead of
    # blocking on an unrelated, unaccepted interpretation review.
    result = validate_pipeline_for_trained_operator(
        state, _make_settings(), yaml_gen, session_id="test-session", allow_pending_interpretation_placeholders=True
    )

    assert result.is_valid is True, "Advisory must never block Run"
    advisories = [c for c in result.checks if c.name == CHECK_STATIC_LLM_PROMPT_ADVISORY]
    assert len(advisories) == 1
    advisory = advisories[0]
    assert advisory.passed is True, "Advisory entries are passed=True (informational)"
    assert advisory.affected_nodes == ("classify",)

    detail = advisory.detail
    # 1. Observation: names the node, states the consequence (unchanged by
    # the wording revision).
    assert "'classify'" in detail
    assert "identical prompt" in detail
    # 2. Redirect question, not an availability assertion — an LLM source
    # plugin is in development elsewhere (as of this wording) but does not
    # exist yet, so the advisory must ask rather than promise. Exact phrasing
    # from operator direction (2026-08-01 follow-up on elspeth-6bdb7e7736).
    assert "did you mean to use an LLM source instead?" in detail
    assert detail.rstrip().endswith("?"), "must end as a question, not an assertion of availability"
    # No specific plugin id is verifiable yet (no ticket, no branch, no
    # registration) — the advisory must not promise one. Guards against a
    # future edit re-introducing an unverified id like 'source:llm'.
    assert "source:llm" not in detail
    assert "llm_source" not in detail
    # Old assertion-style wording ("cannot express today") must be gone.
    assert "cannot express" not in detail
    # 3. Concrete remedy retained for the transform case.
    assert "row.*" in detail
    assert "per-row transformation" in detail


@patch("elspeth.web.execution.validation.assemble_and_validate_pipeline_config")
@patch("elspeth.web.execution.validation.load_settings_from_yaml_string")
@patch("elspeth.web.execution.validation.instantiate_runtime_plugins")
@patch("elspeth.web.execution.validation.build_runtime_graph")
def test_validate_pipeline_emits_no_advisory_for_row_bound_template(
    mock_build_graph: MagicMock,
    mock_instantiate: MagicMock,
    mock_load: MagicMock,
    mock_assemble: MagicMock,
) -> None:
    yaml_gen = _YamlGeneratorDouble()
    mock_load.return_value = object()
    mock_instantiate.return_value = _runtime_bundle_double()
    mock_build_graph.return_value = _runtime_graph_double()
    mock_assemble.return_value = object()

    state = _make_state_with(
        source=_allowlisted_source(),
        nodes=(_make_llm_node(prompt_template="Classify: {{ row.text }}"),),
        outputs=(_allowlisted_observed_sink(),),
    )
    result = validate_pipeline_for_trained_operator(
        state, _make_settings(), yaml_gen, session_id="test-session", allow_pending_interpretation_placeholders=True
    )

    assert result.is_valid is True
    advisories = [c for c in result.checks if c.name == CHECK_STATIC_LLM_PROMPT_ADVISORY]
    assert advisories == []


def test_validate_pipeline_suppresses_advisory_on_failure_path() -> None:
    """Structural failures suppress the advisory — happy-path only, same
    contract as identity_node_advisory."""
    state = _make_state_with(
        source=SourceSpec(
            plugin="csv",
            on_success="page_in",
            options={"path": "/etc/passwd"},  # blocked path
            on_validation_failure="discard",
        ),
        nodes=(_make_llm_node(prompt_template="Static prompt with no interpolation at all."),),
        outputs=(_make_observed_sink(),),
    )
    settings = _make_settings(data_dir="/tmp/test_data")
    result = validate_pipeline_for_trained_operator(state, settings, _YamlGeneratorDouble())

    assert result.is_valid is False, "path_allowlist must block this pipeline"
    advisories = [c for c in result.checks if c.name == CHECK_STATIC_LLM_PROMPT_ADVISORY]
    assert advisories == [], "Advisory must NOT emit on the failure path."
