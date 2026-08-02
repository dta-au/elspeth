"""Queue-aware required-control coverage over authored pipeline streams."""

from __future__ import annotations

from dataclasses import replace

import pytest

from elspeth.contracts.plugin_capabilities import PluginCapability
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    _validate_prompt_template_variable_bindings,
)
from elspeth.web.plugin_policy.coverage import (
    _translate_protected_fields_through_mapper,
    build_output_stream_graph,
    control_coverage_findings,
)


def _node(
    node_id: str,
    plugin: str | None,
    input_stream: str,
    on_success: str | None,
    *,
    node_type: str = "transform",
    options: dict[str, object] | None = None,
    routes: dict[str, str] | None = None,
    fork_to: tuple[str, ...] | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type=node_type,  # type: ignore[arg-type]
        plugin=plugin,
        input=input_stream,
        on_success=on_success,
        on_error="discard" if node_type == "transform" else None,
        options=options or {},
        condition=None,
        routes=routes,
        fork_to=fork_to,
        branches=None,
        policy=None,
        merge=None,
    )


def _queue(queue_id: str) -> NodeSpec:
    return _node(queue_id, None, queue_id, None, node_type="queue")


def _row_union(
    *,
    branches: dict[str, str],
    on_success: str = "unioned_rows",
) -> NodeSpec:
    return replace(
        _node("variant_union", None, next(iter(branches.values())), on_success, node_type="row_union"),
        branches=branches,
    )


def _state(*nodes: NodeSpec, source_target: str = "llm_in", sinks: tuple[str, ...] = ("main",)) -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success=source_target,
            options={"path": "rows.csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=nodes,
        edges=(),
        outputs=tuple(
            OutputSpec(
                name=name,
                plugin="json",
                options={"path": f"{name}.jsonl", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
            for name in sinks
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _llm(
    input_stream: str = "llm_in",
    on_success: str = "main",
    *,
    prompt_fields: tuple[str, ...] = ("prompt",),
    declared_prompt_fields: tuple[str, ...] | None = None,
    response_field: str = "llm_response",
) -> NodeSpec:
    declared_fields = prompt_fields if declared_prompt_fields is None else declared_prompt_fields
    return _node(
        "judge",
        "llm",
        input_stream,
        on_success,
        options={
            "prompt_template": " ".join(f"{{{{ row.{field} }}}}" for field in prompt_fields),
            "required_input_fields": list(declared_fields),
            "response_field": response_field,
        },
    )


def _shield(
    node_id: str,
    input_stream: str,
    on_success: str,
    *,
    detect_only: bool = False,
    fields: tuple[str, ...] = ("prompt",),
    plugin: str = "azure_prompt_shield",
) -> NodeSpec:
    return _node(
        node_id,
        plugin,
        input_stream,
        on_success,
        options={"detect_only": detect_only, "fields": list(fields)},
    )


def _safety(
    node_id: str,
    input_stream: str,
    on_success: str,
    *,
    detect_only: bool = False,
    fields: tuple[str, ...] = ("llm_response",),
    plugin: str = "azure_content_safety",
) -> NodeSpec:
    return _node(
        node_id,
        plugin,
        input_stream,
        on_success,
        options={"detect_only": detect_only, "fields": list(fields)},
    )


def _mapper(
    node_id: str,
    input_stream: str,
    on_success: str,
    mapping: object,
    *,
    select_only: object = False,
) -> NodeSpec:
    return _node(
        node_id,
        "field_mapper",
        input_stream,
        on_success,
        options={"mapping": mapping, "select_only": select_only},
    )


@pytest.mark.parametrize(
    ("state", "covered"),
    [
        (_state(_llm()), False),
        (_state(_shield("shield", "raw", "llm_in"), _llm(), source_target="raw"), True),
        # Role mismatch: OUTPUT control cannot satisfy INPUT coverage.
        (_state(_safety("wrong_role", "raw", "llm_in"), _llm(), source_target="raw"), False),
        # A shield after the LLM is too late.
        (_state(_llm(on_success="shield_in"), _shield("shield", "shield_in", "main")), False),
        # Detect-only metadata cannot receive blocking credit.
        (_state(_shield("shield", "raw", "llm_in", detect_only=True), _llm(), source_target="raw"), False),
        # An external-call result after a shield reintroduces untrusted content.
        (
            _state(
                _shield("shield", "raw", "fetch_in"),
                _node("fetch", "web_scrape", "fetch_in", "llm_in"),
                _llm(),
                source_target="raw",
            ),
            False,
        ),
    ],
)
def test_prompt_shield_input_coverage(state: CompositionState, covered: bool) -> None:
    assert (control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()) is covered


@pytest.mark.parametrize(
    ("shield_fields", "covered"),
    [
        (("benign_label",), False),
        (("untrusted_prompt",), True),
    ],
)
def test_prompt_shield_input_coverage_requires_llm_field_scope(
    shield_fields: tuple[str, ...],
    covered: bool,
) -> None:
    state = _state(
        _shield("shield", "raw", "llm_in", fields=shield_fields),
        _llm(prompt_fields=("untrusted_prompt",)),
        source_target="raw",
    )

    assert (control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()) is covered


def test_prompt_shield_input_coverage_uses_actual_template_fields() -> None:
    state = _state(
        _shield("shield", "raw", "llm_in", fields=("benign_label",)),
        _llm(
            prompt_fields=("untrusted_prompt",),
            declared_prompt_fields=("benign_label",),
        ),
        source_target="raw",
    )

    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)


def _value_transform(
    node_id: str,
    input_stream: str,
    on_success: str,
    operations: object,
) -> NodeSpec:
    return _node(
        node_id,
        "value_transform",
        input_stream,
        on_success,
        options={"schema": {"mode": "observed"}, "operations": operations},
    )


def test_prompt_shield_input_coverage_rejects_value_transform_overwrite_below_shield() -> None:
    state = _state(
        _shield("shield", "raw", "rewrite_in"),
        _value_transform(
            "rewrite",
            "rewrite_in",
            "llm_in",
            [{"target": "prompt", "expression": "row['untrusted']"}],
        ),
        _llm(),
        source_target="raw",
    )

    findings = control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)

    assert [(finding.component_id, finding.reason) for finding in findings] == [
        ("judge", "input_not_dominated"),
    ]


def test_prompt_shield_input_coverage_allows_value_transform_writing_unshielded_fields() -> None:
    state = _state(
        _shield("shield", "raw", "rewrite_in"),
        _value_transform(
            "rewrite",
            "rewrite_in",
            "llm_in",
            [{"target": "derived_score", "expression": "1"}],
        ),
        _llm(),
        source_target="raw",
    )

    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()


@pytest.mark.parametrize(
    "operations",
    [
        "not a list",
        [{"expression": "1"}],
        [{"target": 7, "expression": "1"}],
        [{"target": "  ", "expression": "1"}],
        ["not a mapping"],
    ],
)
def test_prompt_shield_input_coverage_fails_closed_for_unprovable_value_transform(
    operations: object,
) -> None:
    state = _state(
        _shield("shield", "raw", "rewrite_in"),
        _value_transform("rewrite", "rewrite_in", "llm_in", operations),
        _llm(),
        source_target="raw",
    )

    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)


def test_prompt_shield_input_coverage_passes_through_passthrough() -> None:
    state = _state(
        _shield("shield", "raw", "pass_in"),
        _node("relay", "passthrough", "pass_in", "llm_in"),
        _llm(),
        source_target="raw",
    )

    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()


def test_prompt_shield_input_coverage_fails_closed_for_unknown_write_set_transform() -> None:
    # json_explode writes parsed fields into the row; coverage cannot prove the
    # shielded prompt survives, so the path must fail closed.
    state = _state(
        _shield("shield", "raw", "explode_in"),
        _node(
            "explode",
            "json_explode",
            "explode_in",
            "llm_in",
            options={"schema": {"mode": "observed"}, "source_field": "payload", "fields": ["prompt"]},
        ),
        _llm(),
        source_target="raw",
    )

    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)


def test_prompt_shield_input_coverage_follows_field_mapper_rename_upstream() -> None:
    state = _state(
        _shield("shield", "raw", "mapped_in", fields=("raw_prompt",)),
        _mapper("rename", "mapped_in", "llm_in", {"raw_prompt": "prompt"}),
        _llm(prompt_fields=("prompt",)),
        source_target="raw",
    )

    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()


@pytest.mark.parametrize(
    ("mapping", "prompt_template", "shield_fields"),
    [
        ({"meta.prompt": "prompt"}, "{{ row.prompt }}", ["meta.prompt"]),
        ({"meta.prompt": "meta.prompt"}, '{{ row["meta.prompt"] }}', ["meta.prompt"]),
        ({"meta.prompt": "prompt"}, "{{ row.prompt }}", "all"),
    ],
)
def test_prompt_shield_input_coverage_fails_closed_for_dotted_mapper_source(
    mapping: dict[str, str],
    prompt_template: str,
    shield_fields: list[str] | str,
) -> None:
    shield = replace(
        _shield("shield", "raw", "mapped_in"),
        options={"detect_only": False, "fields": shield_fields},
    )
    llm = _node(
        "judge",
        "llm",
        "llm_in",
        "main",
        options={
            "prompt_template": prompt_template,
            "required_input_fields": [],
            "response_field": "llm_response",
        },
    )
    state = _state(
        shield,
        _mapper("rename", "mapped_in", "llm_in", mapping),
        llm,
        source_target="raw",
    )

    findings = control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)

    assert [(finding.component_id, finding.reason) for finding in findings] == [
        ("judge", "input_not_dominated"),
    ]


def test_prompt_shield_input_coverage_preserves_fields_unrelated_to_dotted_mapping() -> None:
    state = _state(
        _shield("shield", "raw", "mapped_in", fields=("prompt",)),
        _mapper("rename", "mapped_in", "llm_in", {"meta.audit": "audit"}),
        _llm(prompt_fields=("prompt",)),
        source_target="raw",
    )

    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()


@pytest.mark.parametrize(("select_only", "covered"), [(False, True), (True, False)])
def test_prompt_shield_input_coverage_tracks_dotted_literal_passthrough(
    select_only: bool,
    covered: bool,
) -> None:
    llm = _node(
        "judge",
        "llm",
        "llm_in",
        "main",
        options={
            "prompt_template": '{{ row["meta.prompt"] }}',
            "required_input_fields": [],
            "response_field": "llm_response",
        },
    )
    state = _state(
        _shield("shield", "raw", "mapped_in", fields=("meta.prompt",)),
        _mapper(
            "extract_nested",
            "mapped_in",
            "llm_in",
            {"meta.prompt": "audit"},
            select_only=select_only,
        ),
        llm,
        source_target="raw",
    )

    assert (control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()) is covered


@pytest.mark.parametrize(
    ("mapping", "select_only", "translated"),
    [
        ({"meta.prompt": "audit"}, False, frozenset({"meta.prompt", "audit"})),
        ({"meta.prompt": "audit"}, True, frozenset({"audit"})),
        ({"meta.prompt": "meta.prompt"}, False, frozenset({"meta.prompt"})),
        ({"meta.prompt": "meta.prompt"}, True, frozenset({"meta.prompt"})),
    ],
)
def test_downstream_translation_tracks_dotted_literal_passthrough(
    mapping: dict[str, str],
    select_only: bool,
    translated: frozenset[str] | None,
) -> None:
    mapper = _mapper("mapper", "in", "out", mapping, select_only=select_only)

    assert (
        _translate_protected_fields_through_mapper(
            mapper,
            frozenset({"meta.prompt"}),
            direction="downstream",
        )
        == translated
    )


@pytest.mark.parametrize(
    ("mapping", "select_only", "safety_fields", "covered"),
    [
        ({"q.a_llm_response": "answer"}, False, ("q.a_llm_response",), False),
        ({"q.a_llm_response": "answer"}, False, ("q.a_llm_response", "answer"), True),
        ({"q.a_llm_response": "answer"}, True, ("answer",), True),
        ({"q.a_llm_response": "q.a_llm_response"}, False, ("q.a_llm_response",), True),
        ({"q.a_llm_response": "q.a_llm_response"}, True, ("q.a_llm_response",), True),
    ],
)
def test_content_safety_output_coverage_tracks_dotted_multi_query_response_mapping(
    mapping: dict[str, str],
    select_only: bool,
    safety_fields: tuple[str, ...],
    covered: bool,
) -> None:
    llm = _node(
        "judge",
        "llm",
        "llm_in",
        "mapped_in",
        options={
            "prompt_template": "{{ row.prompt }}",
            "required_input_fields": ["prompt"],
            "response_field": "llm_response",
            "queries": {"q.a": {"input_fields": {"prompt": "prompt"}}},
        },
    )
    state = _state(
        llm,
        _mapper("mapper", "mapped_in", "safe_in", mapping, select_only=select_only),
        _safety("safety", "safe_in", "main", fields=safety_fields),
    )

    assert (control_coverage_findings(state, PluginCapability.CONTENT_SAFETY) == ()) is covered


def test_prompt_shield_queue_fan_in_requires_every_path() -> None:
    state = _state(
        _node("left", "passthrough", "left_in", "inbound"),
        _shield("right_shield", "right_in", "inbound"),
        _queue("inbound"),
        _llm("inbound"),
        source_target="left_in",
    )
    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)


def test_prompt_shield_queue_fan_in_passes_when_every_path_is_shielded() -> None:
    state = CompositionState(
        sources={
            "left": SourceSpec("csv", "left_raw", {}, "discard"),
            "right": SourceSpec("json", "right_raw", {}, "discard"),
        },
        nodes=(
            _shield("left_shield", "left_raw", "inbound"),
            _shield("right_shield", "right_raw", "inbound"),
            _queue("inbound"),
            _llm("inbound"),
        ),
        edges=(),
        outputs=(OutputSpec("main", "json", {}, "discard"),),
        metadata=PipelineMetadata(),
        version=1,
    )
    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()


def _branch_llm(node_id: str, input_stream: str) -> NodeSpec:
    return _node(
        node_id,
        "llm",
        input_stream,
        "main",
        options={
            "prompt_template": "{{ row.prompt }}",
            "required_input_fields": ["prompt"],
            "response_field": f"{node_id}_response",
        },
    )


def _fork_gate(gate_id: str, input_stream: str, branches: tuple[str, ...]) -> NodeSpec:
    return _node(
        gate_id,
        None,
        input_stream,
        None,
        node_type="gate",
        routes={"true": "fork", "false": "fork"},
        fork_to=branches,
    )


def test_prompt_shield_dominates_every_branch_through_fork_gate() -> None:
    # Config gates route rows without modifying them, so one shield above the
    # fork dominates both LLM branches.
    state = _state(
        _shield("shield", "raw", "shielded"),
        _fork_gate("fan_out", "shielded", ("branch_a", "branch_b")),
        _branch_llm("llm_a", "branch_a"),
        _branch_llm("llm_b", "branch_b"),
        source_target="raw",
    )
    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()


def test_prompt_shield_dominates_through_routing_gate() -> None:
    state = _state(
        _shield("shield", "raw", "shielded"),
        _node("router", None, "shielded", None, node_type="gate", routes={"hot": "llm_in", "cold": "discard"}),
        _llm(),
        source_target="raw",
    )
    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()


def test_fork_gate_without_upstream_shield_reports_every_branch() -> None:
    state = _state(
        _fork_gate("fan_out", "raw", ("branch_a", "branch_b")),
        _branch_llm("llm_a", "branch_a"),
        _branch_llm("llm_b", "branch_b"),
        source_target="raw",
    )

    findings = control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)

    assert {finding.component_id for finding in findings} == {"llm_a", "llm_b"}


def test_fork_gate_pass_through_keeps_per_branch_shielding_exact() -> None:
    # A shield inside one branch covers that branch only; the gate pass-through
    # must not leak its credit to the sibling.
    state = _state(
        _fork_gate("fan_out", "raw", ("branch_a", "branch_b")),
        _shield("branch_shield", "branch_a", "shielded_a"),
        _branch_llm("llm_a", "shielded_a"),
        _branch_llm("llm_b", "branch_b"),
        source_target="raw",
    )

    findings = control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)

    assert {finding.component_id for finding in findings} == {"llm_b"}


def test_prompt_shield_cycle_fails_safe() -> None:
    state = _state(
        _node("cycle_a", "passthrough", "cycle_b_out", "cycle_a_out"),
        _node("cycle_b", "passthrough", "cycle_a_out", "cycle_b_out"),
        _llm("cycle_a_out"),
        source_target="unused",
    )
    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)


def test_extracted_graph_preserves_error_stream_producers() -> None:
    producer = replace(_node("producer", "passthrough", "raw", "success"), on_error="failure")
    graph = build_output_stream_graph((producer,))

    assert graph.producers_by_stream["failure"] == (producer,)


@pytest.mark.parametrize(
    ("state", "covered"),
    [
        (_state(_llm()), False),
        (_state(_llm(on_success="safe_in"), _safety("safety", "safe_in", "main")), True),
        # Role mismatch: INPUT control after the LLM cannot satisfy OUTPUT coverage.
        (_state(_llm(on_success="shield_in"), _shield("wrong_role", "shield_in", "main")), False),
        # A safety control before the LLM does not post-dominate its output.
        (_state(_safety("safety", "raw", "llm_in"), _llm(), source_target="raw"), False),
        (_state(_llm(on_success="safe_in"), _safety("safety", "safe_in", "main", detect_only=True)), False),
        # Multiple effective controls remain valid.
        (
            _state(
                _llm(on_success="safe_a_in"),
                _safety("safety_a", "safe_a_in", "safe_b_in"),
                _safety("safety_b", "safe_b_in", "main"),
            ),
            True,
        ),
    ],
)
def test_content_safety_output_coverage(state: CompositionState, covered: bool) -> None:
    assert (control_coverage_findings(state, PluginCapability.CONTENT_SAFETY) == ()) is covered


@pytest.mark.parametrize(
    ("safety_fields", "covered"),
    [
        (("benign_label",), False),
        (("model_answer",), True),
    ],
)
def test_content_safety_output_coverage_requires_llm_field_scope(
    safety_fields: tuple[str, ...],
    covered: bool,
) -> None:
    state = _state(
        _llm(on_success="safe_in", response_field="model_answer"),
        _safety("safety", "safe_in", "main", fields=safety_fields),
    )

    assert (control_coverage_findings(state, PluginCapability.CONTENT_SAFETY) == ()) is covered


def test_content_safety_output_coverage_follows_field_mapper_rename_downstream() -> None:
    state = _state(
        _llm(on_success="mapped_in"),
        _mapper("rename", "mapped_in", "safe_in", {"llm_response": "answer"}),
        _safety("safety", "safe_in", "main", fields=("answer",)),
    )

    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY) == ()


def test_content_safety_output_coverage_rejects_pre_rename_field_name() -> None:
    state = _state(
        _llm(on_success="mapped_in"),
        _mapper("rename", "mapped_in", "safe_in", {"llm_response": "answer"}),
        _safety("safety", "safe_in", "main", fields=("llm_response",)),
    )

    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY)


def test_content_safety_output_coverage_preserves_unmapped_fields() -> None:
    state = _state(
        _llm(on_success="mapped_in"),
        _mapper("rename", "mapped_in", "safe_in", {"metadata": "label"}),
        _safety("safety", "safe_in", "main", fields=("llm_response",)),
    )

    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY) == ()


@pytest.mark.parametrize(
    ("mapping", "select_only"),
    [
        ({"metadata": "label"}, True),
        (["not", "a", "mapping"], False),
        ({"llm_response": 7}, False),
        ({"llm_response": "answer", "other": "answer"}, False),
        ({"llm_response": "answer", "answer": "final"}, False),
        ({"llm_response": "answer"}, "false"),
    ],
)
def test_content_safety_output_coverage_fails_closed_for_unprovable_field_mapper(
    mapping: object,
    select_only: object,
) -> None:
    state = _state(
        _llm(on_success="mapped_in"),
        _mapper("mapper", "mapped_in", "safe_in", mapping, select_only=select_only),
        _safety("safety", "safe_in", "main", fields=("llm_response", "answer")),
    )

    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY)


def test_content_safety_fan_out_requires_every_sink_path() -> None:
    state = _state(
        _node("judge", "llm", "llm_in", None, fork_to=("safe_in", "unsafe_in")),
        _safety("safety", "safe_in", "safe"),
        _node("unsafe", "passthrough", "unsafe_in", "unsafe"),
        sinks=("safe", "unsafe"),
    )
    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY)


def test_content_safety_fan_out_passes_when_each_sink_path_is_controlled() -> None:
    state = _state(
        _node("judge", "llm", "llm_in", None, fork_to=("left_in", "right_in")),
        _safety("left_safety", "left_in", "left"),
        _safety("right_safety", "right_in", "right"),
        sinks=("left", "right"),
    )
    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY) == ()


def test_content_safety_error_route_to_sink_is_uncovered() -> None:
    ordinary = replace(
        _node("ordinary", "passthrough", "ordinary_in", "safe_in"),
        on_error="unsafe",
    )
    state = _state(
        _llm(on_success="ordinary_in"),
        ordinary,
        _safety("safety", "safe_in", "safe"),
        sinks=("safe", "unsafe"),
    )

    findings = control_coverage_findings(state, PluginCapability.CONTENT_SAFETY)

    assert [(finding.component_id, finding.reason) for finding in findings] == [
        ("judge", "output_not_post_dominated"),
    ]


def test_content_safety_post_dominates_valid_coalesce_chain() -> None:
    coalesce = replace(
        _node("join", None, "branches", None, node_type="coalesce"),
        branches={"left": "left_done", "right": "right_done"},
        policy="require_all",
        merge="nested",
    )
    state = _state(
        _llm(on_success="fanout_in"),
        _node(
            "fanout",
            None,
            "fanout_in",
            None,
            node_type="gate",
            routes={"all": "fork"},
            fork_to=("left", "right"),
        ),
        _node("left_path", "passthrough", "left", "left_done"),
        _node("right_path", "passthrough", "right", "right_done"),
        coalesce,
        _safety("safety", "join", "main"),
    )

    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY) == ()


def test_prompt_shield_must_dominate_every_row_union_branch() -> None:
    union = _row_union(branches={"left": "left_done", "right": "right_done"})
    state = _state(
        _shield("left_shield", "raw", "left_done"),
        _node("right_path", "passthrough", "raw", "right_done"),
        union,
        _llm(input_stream="unioned_rows"),
        source_target="raw",
    )

    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)


def test_content_safety_post_dominates_row_union_release() -> None:
    union = _row_union(branches={"left": "left_done", "right": "right_done"})
    state = _state(
        _llm(on_success="fanout_in"),
        _node(
            "fanout",
            None,
            "fanout_in",
            None,
            node_type="gate",
            routes={"all": "fork"},
            fork_to=("left", "right"),
        ),
        _node("left_path", "passthrough", "left", "left_done"),
        _node("right_path", "passthrough", "right", "right_done"),
        union,
        _safety("safety", "unioned_rows", "main"),
    )

    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY) == ()


def test_content_safety_unknown_downstream_fails_safe() -> None:
    assert control_coverage_findings(_state(_llm(on_success="missing")), PluginCapability.CONTENT_SAFETY)


def test_content_safety_no_op_thresholds_do_not_receive_blocking_coverage_credit() -> None:
    no_op = _safety("safety", "safe_in", "main")
    no_op = replace(
        no_op,
        options={
            "detect_only": False,
            "thresholds": {"hate": 6, "violence": 6, "sexual": 6, "self_harm": 6},
        },
    )

    assert control_coverage_findings(
        _state(_llm(on_success="safe_in"), no_op),
        PluginCapability.CONTENT_SAFETY,
    )


def test_bedrock_controls_dominate_llm_input_and_post_dominate_every_output() -> None:
    state = _state(
        _shield(
            "prompt_shield",
            "raw",
            "llm_in",
            plugin="aws_bedrock_prompt_shield",
        ),
        _llm(on_success="content_in"),
        _safety(
            "content_safety",
            "content_in",
            "main",
            plugin="aws_bedrock_content_safety",
        ),
        source_target="raw",
    )

    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()
    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY) == ()


def test_bedrock_content_safety_input_source_does_not_claim_output_coverage() -> None:
    state = _state(
        _llm(on_success="content_in"),
        _node(
            "content_safety",
            "aws_bedrock_content_safety",
            "content_in",
            "main",
            options={"source": "INPUT"},
        ),
    )

    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY)


def test_bedrock_required_coverage_fails_for_an_unshielded_input_or_output_path() -> None:
    unshielded_input = _state(
        _llm(on_success="content_in"),
        _safety("content_safety", "content_in", "main", plugin="aws_bedrock_content_safety"),
    )
    unshielded_output = _state(
        _shield("prompt_shield", "raw", "llm_in", plugin="aws_bedrock_prompt_shield"),
        _llm(),
        source_target="raw",
    )

    assert control_coverage_findings(unshielded_input, PluginCapability.PROMPT_SHIELD)
    assert control_coverage_findings(unshielded_output, PluginCapability.CONTENT_SAFETY)


# ── llm on_error vs a required output control ────────────────────────────────
#
# An llm node's on_error edge is an independent output path, so a quarantine
# sink on it correctly fails required_control_coverage. These tests pin the
# DIAGNOSIS (which reason and stream the finding carries) and — critically —
# the fact that the only authorable repair is on_error='discard'. A coverage
# assertion alone is not enough evidence here: the abstract stream graph
# happily accepts `llm --on_error--> connection --> control --> sink`, while
# the graph builder rejects that edge outright. Both halves are asserted.


def _authorable_state(*nodes: NodeSpec, sinks: tuple[str, ...] = ("main",)) -> CompositionState:
    """Like ``_state``, but with the llm node's prompt field guaranteed.

    ``_state``'s source guarantees no fields, which is fine for the tests that
    only call ``control_coverage_findings``. Declaring ``guaranteed_fields``
    here clears the ``schema_contract_violation`` an ``_llm`` node would
    otherwise raise, so asserting on ``validate().is_valid`` becomes meaningful.
    """
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="llm_in",
            options={"path": "rows.csv", "schema": {"mode": "observed", "guaranteed_fields": ["prompt"]}},
            on_validation_failure="discard",
        ),
        nodes=nodes,
        edges=(),
        outputs=tuple(
            OutputSpec(
                name=name,
                plugin="json",
                options={"path": f"{name}.jsonl", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
            for name in sinks
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def test_content_safety_llm_error_route_to_sink_names_the_offending_error_route() -> None:
    state = _authorable_state(
        replace(_llm(on_success="safe_in"), on_error="quarantine"),
        _safety("safety", "safe_in", "main"),
        sinks=("main", "quarantine"),
    )

    findings = control_coverage_findings(state, PluginCapability.CONTENT_SAFETY)

    assert [(finding.component_id, finding.reason, finding.uncovered_stream) for finding in findings] == [
        ("judge", "output_error_route_not_post_dominated", "quarantine"),
    ]


def test_content_safety_llm_error_route_discard_is_covered_and_authorable() -> None:
    """The one repair the rejection message offers must actually validate."""
    state = _authorable_state(
        replace(_llm(on_success="safe_in"), on_error="discard"),
        _safety("safety", "safe_in", "main"),
    )

    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY) == ()
    assert state.validate().is_valid


def test_llm_error_route_through_the_control_is_not_authorable() -> None:
    """Interposing a control on an error branch is NOT a repair — pin that.

    Stream-level coverage accepts this shape, which makes it look like a
    sanctioned pattern. It is not: on_error may only name a sink or 'discard'
    (core/dag/builder.py:1108, mirrored in composer state validation), so the
    graph rejects the edge. Any message or authoring aid that offers this
    shape sends the planner between two rejections forever.
    """
    state = _authorable_state(
        replace(_llm(on_success="safe_in"), on_error="quarantine_in"),
        _safety("safety", "safe_in", "main"),
        _safety("quarantine_safety", "quarantine_in", "quarantine"),
        sinks=("main", "quarantine"),
    )

    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY) == ()
    assert "transform_on_error_unknown_sink" in {error.error_code for error in state.validate().errors}


def test_llm_error_route_is_named_when_a_multi_hop_success_path_is_covered() -> None:
    """The discriminating case: on_success covered through hops, on_error not.

    This is the shape that must yield the error-route reason, and it is what
    keeps the narrowing honest. Widening the condition to "any uncovered stream
    that happens to be some node's on_error target" would still pass the
    trivially-broken cases below while breaking
    ``test_content_safety_error_route_to_sink_is_uncovered``, where the
    uncovered on_error belongs to a DOWNSTREAM node, not the llm node.
    """
    state = _authorable_state(
        replace(_llm(on_success="mid_in"), on_error="quarantine"),
        _node("mid", "passthrough", "mid_in", "safe_in"),
        _safety("safety", "safe_in", "main"),
        sinks=("main", "quarantine"),
    )

    findings = control_coverage_findings(state, PluginCapability.CONTENT_SAFETY)

    assert [(finding.reason, finding.uncovered_stream) for finding in findings] == [
        ("output_error_route_not_post_dominated", "quarantine"),
    ]


def test_llm_error_route_reason_stays_general_when_another_path_is_also_uncovered() -> None:
    """The error-route diagnosis claims the SOLE uncovered stream, or nothing.

    Here on_success writes straight to a sink, so both edges are uncovered and
    naming only the error route would send the author on a repair that leaves
    the pipeline rejected.
    """
    state = _authorable_state(
        replace(_llm(on_success="main"), on_error="quarantine"),
        sinks=("main", "quarantine"),
    )

    findings = control_coverage_findings(state, PluginCapability.CONTENT_SAFETY)

    assert [(finding.reason, finding.uncovered_stream) for finding in findings] == [
        ("output_not_post_dominated", None),
    ]


# ── prompt-field provability (R2-F17 compounding half, elspeth-5c0c09db31) ───


def _llm_with_template(template: str, *, input_stream: str = "llm_in", on_success: str = "main") -> NodeSpec:
    """An LLM node whose prompt template is authored verbatim.

    ``_llm`` synthesises ``{{ row.<field> }}`` accesses; these cases turn on
    templates that carry NO provable ``row.*`` access at all.

    The template must be one a real candidate could still carry, so the
    upstream binding guard is asserted here rather than in each case:
    ``_validate_prompt_template_variable_bindings`` (elspeth-bea314a89b) now
    rejects bare-variable templates like ``{{ text }}`` at
    ``CompositionState.validate`` — earlier in the funnel than coverage — so
    that shape can no longer reach a coverage decision at all. Dynamic
    ``row[<expr>]`` access is the surviving route to an empty provable field
    set. Without this assertion a later tightening of the binding guard would
    leave the provability cases silently vacuous: they call
    ``control_coverage_findings`` directly and would keep passing on a shape
    production forbids.
    """
    assert (
        _validate_prompt_template_variable_bindings(_node("probe", "llm", input_stream, on_success, options={"prompt_template": template}))
        is None
    ), f"fixture template is rejected before coverage runs: {template!r}"
    return _node(
        "judge",
        "llm",
        input_stream,
        on_success,
        options={"prompt_template": template, "response_field": "llm_response"},
    )


def _all_fields_shield(node_id: str, input_stream: str, on_success: str) -> NodeSpec:
    """A shield configured with the string scope ``fields: all``."""
    return _node(
        node_id,
        "azure_prompt_shield",
        input_stream,
        on_success,
        options={"detect_only": False, "fields": "all"},
    )


def test_all_fields_shield_is_credited_when_prompt_fields_are_unprovable() -> None:
    """AWS acceptance run 2 (R2-F17): a correctly placed shield was rejected.

    The incident's own template was ``Classify: {{ text }}``, which has no
    ``row.*`` access, so the provable prompt field set is empty — and the
    empty-set bail-out ran BEFORE the ``fields: all`` shortcut, so a control
    that scans every field was never credited and ``input_not_dominated``
    fired on a conforming pipeline. ``all`` is a superset of every protected
    set, provable or not.

    The fixture uses dynamic ``row[<expr>]`` access rather than the incident's
    literal template because ``{{ text }}`` is now rejected upstream at
    ``CompositionState.validate`` (elspeth-bea314a89b) and can no longer reach
    coverage. The regression this pins is the ordering of the ``fields: all``
    shortcut against the empty-set bail-out, which is unchanged by how the
    protected set came to be empty.
    """
    state = _state(
        _all_fields_shield("shield", "raw", "llm_in"),
        _llm_with_template("Classify: {{ row[lookup.field_name] }}"),
        source_target="raw",
    )

    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()


def test_all_fields_shield_does_not_cover_dynamic_prompt_after_downstream_rewrite() -> None:
    state = _state(
        _all_fields_shield("shield", "raw", "rewrite_in"),
        _value_transform(
            "rewrite",
            "rewrite_in",
            "llm_in",
            [{"target": "prompt", "expression": "row['untrusted']"}],
        ),
        _llm_with_template("Classify: {{ row[lookup.field_name] }}"),
        source_target="raw",
    )

    findings = control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)

    assert [(finding.component_id, finding.reason) for finding in findings] == [
        ("judge", "input_fields_unprovable"),
    ]
    assert findings[0].scanned_fields == ()


def test_all_fields_shield_covers_dynamic_prompt_after_downstream_field_mapping() -> None:
    """A mapper only relocates values already scanned by an all-field shield."""
    state = _state(
        _all_fields_shield("shield", "raw", "mapping_in"),
        _node(
            "rename",
            "field_mapper",
            "mapping_in",
            "llm_in",
            options={"mapping": {"untrusted": "prompt"}, "select_only": False},
        ),
        _llm_with_template("Classify: {{ row[lookup.field_name] }}"),
        source_target="raw",
    )

    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()


def test_unprovable_prompt_fields_are_a_distinct_diagnosis_naming_both_field_sets() -> None:
    """A field-scoped shield still fails closed — but says why, not "not covered".

    An empty extraction is not proof that no row field reaches the prompt, so
    a control scanning a specific list cannot be credited. The author needs
    the distinct diagnosis, not the generic domination message which points at
    a topology that is in fact correct.

    The fixture is the dynamic ``row[<expr>]`` access this rationale names:
    every row field it reads is chosen at render time, so the prompt provably
    DOES consume row data while the statically provable set stays empty — the
    fail-closed case in its purest form, and the shape that survives the
    upstream bare-variable guard (elspeth-bea314a89b).
    """
    state = _state(
        _shield("shield", "raw", "llm_in", fields=("prompt",)),
        _llm_with_template("Classify: {{ row[lookup.field_name] }}"),
        source_target="raw",
    )

    findings = control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)

    assert [(finding.component_id, finding.reason) for finding in findings] == [
        ("judge", "input_fields_unprovable"),
    ]
    assert findings[0].protected_fields == ()
    assert findings[0].scanned_fields == ("prompt",)


def test_missing_shield_preserves_the_unprovable_prompt_field_diagnosis() -> None:
    """Unprovable scope must block unsafe field-scoped auto-wiring."""
    state = _state(_llm_with_template("Classify: {{ row[lookup.field_name] }}"))

    findings = control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)

    assert [(finding.component_id, finding.reason) for finding in findings] == [
        ("judge", "input_fields_unprovable"),
    ]


def _multi_query_llm(
    *,
    prompt_template: str,
    queries: dict[str, object],
    input_stream: str = "llm_in",
    on_success: str = "main",
) -> NodeSpec:
    """A multi-query LLM whose shared template may be dead (all queries override)."""
    return _node(
        "judge",
        "llm",
        input_stream,
        on_success,
        options={
            "prompt_template": prompt_template,
            "required_input_fields": [],
            "response_field": "llm_response",
            "queries": queries,
        },
    )


def test_dead_shared_template_does_not_poison_prompt_field_provability() -> None:
    """A node template every query overrides never renders, so it must not decide provability.

    ``queries.*.template`` is a per-query override (``None`` = fall back to the
    node-level ``prompt_template`` — multi_query.py), and config validation
    deliberately skips a shared template no query falls back to
    (``LLMConfig._validate_template_variable_bindings``). Coverage must apply
    the same liveness rule: with every override static, the protected set is
    provably {prompt} and a shield scanning exactly that field is credited.
    """
    state = _state(
        _shield("shield", "raw", "llm_in", fields=("prompt",)),
        _multi_query_llm(
            prompt_template="Classify: {{ row[lookup.field_name] }}",
            queries={"q.a": {"input_fields": {"prompt": "prompt"}, "template": "Classify: {{ row.prompt }}"}},
        ),
        source_target="raw",
    )

    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()


def test_dead_shared_template_missing_shield_is_an_auto_wirable_topology_failure() -> None:
    """With the dead template ignored, a missing shield is the wirable diagnosis.

    Required-control auto-wiring acts only on ``input_not_dominated``
    (required_controls._AUTO_WIRE_ACTIONABLE_REASONS); reporting the dead
    template's dynamic access as ``input_fields_unprovable`` left the required
    shield unwired and failed the gate later.
    """
    state = _state(
        _multi_query_llm(
            prompt_template="Classify: {{ row[lookup.field_name] }}",
            queries={"q.a": {"input_fields": {"prompt": "prompt"}, "template": "Classify: {{ row.prompt }}"}},
        )
    )

    findings = control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)

    assert [(finding.component_id, finding.reason) for finding in findings] == [
        ("judge", "input_not_dominated"),
    ]
    assert findings[0].protected_fields == ("prompt",)


def test_query_fallback_keeps_node_template_in_provability() -> None:
    """A query WITHOUT an override still pulls the shared template into the set."""
    llm = _multi_query_llm(
        prompt_template="{{ row.shared_context }}",
        queries={
            "q.a": {"input_fields": {"prompt": "prompt"}},
            "q.b": {"input_fields": {"prompt": "prompt"}, "template": "Classify: {{ row.prompt }}"},
        },
    )

    partial = _state(_shield("shield", "raw", "llm_in", fields=("prompt",)), llm, source_target="raw")
    findings = control_coverage_findings(partial, PluginCapability.PROMPT_SHIELD)
    assert [(finding.component_id, finding.reason) for finding in findings] == [
        ("judge", "input_not_dominated"),
    ]
    assert findings[0].protected_fields == ("prompt", "shared_context")

    covered = _state(
        _shield("shield", "raw", "llm_in", fields=("prompt", "shared_context")),
        llm,
        source_target="raw",
    )
    assert control_coverage_findings(covered, PluginCapability.PROMPT_SHIELD) == ()


def test_dynamic_shared_template_with_a_falling_back_query_stays_unprovable() -> None:
    """Liveness is per-query: one fallback keeps the dynamic template authoritative."""
    state = _state(
        _shield("shield", "raw", "llm_in", fields=("prompt",)),
        _multi_query_llm(
            prompt_template="Classify: {{ row[lookup.field_name] }}",
            queries={"q.a": {"input_fields": {"prompt": "prompt"}}},
        ),
        source_target="raw",
    )

    findings = control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)

    assert [(finding.component_id, finding.reason) for finding in findings] == [
        ("judge", "input_fields_unprovable"),
    ]


def test_mismatched_shield_still_fires_and_carries_both_field_sets() -> None:
    """The real finding must survive the credit fix, with both sets named."""
    state = _state(
        _shield("shield", "raw", "llm_in", fields=("benign_label",)),
        _llm(prompt_fields=("untrusted_prompt",)),
        source_target="raw",
    )

    findings = control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)

    assert [(finding.component_id, finding.reason) for finding in findings] == [
        ("judge", "input_not_dominated"),
    ]
    assert findings[0].protected_fields == ("untrusted_prompt",)
    assert findings[0].scanned_fields == ("benign_label",)


def test_all_fields_control_is_credited_for_output_coverage_too() -> None:
    """The ``all`` shortcut is role-agnostic: it dominates any protected set."""
    state = _authorable_state(
        _llm(on_success="safe_in"),
        _node("safety", "azure_content_safety", "safe_in", "main", options={"detect_only": False, "fields": "all"}),
    )

    assert control_coverage_findings(state, PluginCapability.CONTENT_SAFETY) == ()


def test_external_call_below_the_shield_preserves_unprovable_field_diagnosis() -> None:
    """Unprovable scope remains authoritative over the topology diagnosis.

    A ``web_scrape`` between the shield and the LLM reintroduces unscanned
    content, so no upstream shield scope can cover this node. The diagnostic
    renderer uses the empty scanned-field set to avoid claiming the wiring is
    correct, while the unprovable reason prevents unsafe partial auto-wiring.
    """
    state = _state(
        _shield("shield", "raw", "fetch_in"),
        _node("fetch", "web_scrape", "fetch_in", "llm_in"),
        _llm_with_template("Classify: {{ row[lookup.field_name] }}"),
        source_target="raw",
    )

    findings = control_coverage_findings(state, PluginCapability.PROMPT_SHIELD)

    assert [(finding.component_id, finding.reason) for finding in findings] == [
        ("judge", "input_fields_unprovable"),
    ]
    # No field sets are asserted for a topology failure — naming a control
    # that does not dominate would be a second false statement.
    assert findings[0].scanned_fields == ()
