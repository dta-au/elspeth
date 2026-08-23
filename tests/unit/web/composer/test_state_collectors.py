"""Stage-1 mirrors for the collector node kind and its scope binding (spec §3/§7 rule 1/rule 8).

NEW FILE — test_state.py is under active maintainer edit; do not add to it.
Construction idiom copied from ``test_state_bound_regions.py`` (file-local
small builders, never imported from test_state.py).
"""

from __future__ import annotations

import pytest

from elspeth.core.config import load_settings_from_yaml_string
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    ValidationEntry,
)
from elspeth.web.composer.yaml_generator import PipelineLoweringError, generate_yaml
from elspeth.web.composer.yaml_importer import (
    RuntimeYamlImportError,
    composition_state_from_runtime_yaml,
)


def _make_source(on_success: str) -> SourceSpec:
    return SourceSpec(plugin="csv", on_success=on_success, options={}, on_validation_failure="discard")


def _make_output(name: str) -> OutputSpec:
    return OutputSpec(name=name, plugin="csv", options={}, on_write_failure="discard")


def _transform(node_id: str, input_name: str, on_success: str, *, on_error: str = "discard") -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="passthrough",
        input=input_name,
        on_success=on_success,
        on_error=on_error,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _collector(
    node_id: str = "page_stitcher",
    *,
    input_name: str = "pages",
    on_success: str = "out",
    on_error: str | None = None,
    plugin: str | None = "batch_stats",
    scope_name: str | None = "document_pages",
    scope_opener: str | None = "explode",
    scope_policy: str | None = "require_all",
    scope_on_group_failure: str | None = None,
    **overrides: object,
) -> NodeSpec:
    kwargs: dict[str, object] = {
        "id": node_id,
        "node_type": "collector",
        "plugin": plugin,
        "input": input_name,
        "on_success": on_success,
        "on_error": on_error,
        "options": {},
        "condition": None,
        "routes": None,
        "fork_to": None,
        "branches": None,
        "policy": None,
        "merge": None,
        "scope_name": scope_name,
        "scope_opener": scope_opener,
        "scope_policy": scope_policy,
        "scope_on_group_failure": scope_on_group_failure,
    }
    kwargs.update(overrides)
    return NodeSpec(**kwargs)  # type: ignore[arg-type]


def _state(*nodes: NodeSpec, outputs: tuple[OutputSpec, ...] = ()) -> CompositionState:
    return CompositionState(
        sources={"source": _make_source("rows")},
        nodes=nodes,
        edges=(),
        outputs=outputs or (_make_output("out"),),
        metadata=PipelineMetadata(),
        version=1,
    )


def _errors_for(state: CompositionState, code: str) -> list[ValidationEntry]:
    return [entry for entry in state.validate().errors if entry.error_code == code]


_COLLECTOR_FAMILY_CODES = (
    "collector_missing_scope",
    "collector_scope_policy_invalid",
    "scope_opener_unknown",
    "collector_has_trigger_invalid",
    "collector_missing_plugin",
    "collector_plugin_not_batch_aware",
    "collector_config_invalid",
    "collector_on_success_dangling",
    "collector_scope_on_group_failure_invalid",
    "scope_name_duplicate",
    "scope_opener_duplicate",
    "scope_name_invalid",
    "node_scope_fields_unsupported",
    "scope_escalate_at_outermost",
)


class TestCollectorIntrinsics:
    def test_bound_collector_raises_no_collector_family_code(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector())
        errors = state.validate().errors
        fired = sorted({entry.error_code for entry in errors if entry.error_code in _COLLECTOR_FAMILY_CODES})
        assert fired == [], [entry.message for entry in errors if entry.error_code in fired]

    def test_scope_on_group_failure_records_the_runtime_default(self) -> None:
        node = _collector()
        assert node.scope_on_group_failure == "quarantine"

    def test_scope_policy_is_never_defaulted(self) -> None:
        # ScopeSettings.policy is REQUIRED with no default (spec §3): Stage 1
        # must not invent one — absence is a rejection, not a normalisation.
        node = _collector(scope_policy=None)
        assert node.scope_policy is None
        state = _state(_transform("explode", "rows", "pages"), node)
        [entry] = _errors_for(state, "collector_missing_scope")
        assert "scope_policy" in entry.message

    @pytest.mark.parametrize(
        ("kwargs", "missing_field"),
        [
            pytest.param({"scope_name": None}, "scope_name", id="scope_name_absent"),
            pytest.param({"scope_opener": None}, "scope_opener", id="scope_opener_absent"),
            pytest.param({"scope_policy": None}, "scope_policy", id="scope_policy_absent"),
            pytest.param({"scope_name": "  "}, "scope_name", id="scope_name_blank"),
        ],
    )
    def test_incomplete_scope_binding_is_rejected(self, kwargs: dict[str, str | None], missing_field: str) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(**kwargs))
        [entry] = _errors_for(state, "collector_missing_scope")
        assert missing_field in entry.message

    def test_scope_policy_outside_vocabulary_is_rejected(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(scope_policy="quorum"))
        [entry] = _errors_for(state, "collector_scope_policy_invalid")
        assert "require_all" in entry.message and "best_effort" in entry.message

    def test_scope_opener_must_name_a_transform_node(self) -> None:
        state = _state(_collector(scope_opener="missing_step"))
        [entry] = _errors_for(state, "scope_opener_unknown")
        assert "missing_step" in entry.message

    def test_scope_opener_naming_a_non_transform_is_rejected(self) -> None:
        other = _collector("other_closer", scope_name="other_scope", scope_opener="explode", input_name="pages2", on_success="out")
        state = _state(
            _transform("explode", "rows", "pages"),
            _collector(scope_opener="other_closer"),
            other,
        )
        assert _errors_for(state, "scope_opener_unknown")

    def test_trigger_is_rejected(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(trigger={"count": 5}))
        [entry] = _errors_for(state, "collector_has_trigger_invalid")
        assert "end_of_group" in entry.message

    def test_missing_plugin_is_rejected(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(plugin=None))
        assert _errors_for(state, "collector_missing_plugin")

    def test_row_level_plugin_is_rejected(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(plugin="field_mapper"))
        [entry] = _errors_for(state, "collector_plugin_not_batch_aware")
        assert "is_batch_aware=False" in entry.message

    def test_unknown_plugin_is_not_this_rules_business(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(plugin="not_a_registered_plugin"))
        assert not _errors_for(state, "collector_plugin_not_batch_aware")

    def test_foreign_fields_are_rejected(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(condition="True"))
        [entry] = _errors_for(state, "collector_config_invalid")
        assert "condition" in entry.message

    def test_timeout_seconds_is_owned_by_the_shared_check(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(timeout_seconds=5.0))
        assert _errors_for(state, "node_timeout_unsupported")
        assert not _errors_for(state, "collector_config_invalid")

    def test_dangling_on_success_is_rejected(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(on_success="nowhere"))
        [entry] = _errors_for(state, "collector_on_success_dangling")
        assert "nowhere" in entry.message

    def test_on_success_to_a_downstream_connection_is_accepted(self) -> None:
        state = _state(
            _transform("explode", "rows", "pages"),
            _collector(on_success="assembled"),
            _transform("finisher", "assembled", "out"),
        )
        assert not _errors_for(state, "collector_on_success_dangling")

    def test_scope_on_group_failure_outside_vocabulary_is_rejected(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(scope_on_group_failure="explode_everything"))
        [entry] = _errors_for(state, "collector_scope_on_group_failure_invalid")
        assert "quarantine" in entry.message and "escalate" in entry.message

    def test_reserved_scope_name_is_rejected(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(scope_name="continue"))
        [entry] = _errors_for(state, "scope_name_invalid")
        assert "reserved" in entry.message

    def test_collector_labels_run_the_runtime_label_rules(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(on_error="continue"))
        assert any(entry.component == "node:page_stitcher" for entry in _errors_for(state, "connection_label_invalid"))

    def test_scope_fields_on_a_transform_are_rejected(self) -> None:
        bad = NodeSpec(
            id="step",
            node_type="transform",
            plugin="passthrough",
            input="rows",
            on_success="out",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            scope_name="document_pages",
        )
        state = _state(bad)
        [entry] = _errors_for(state, "node_scope_fields_unsupported")
        assert "scope_name" in entry.message

    def test_scope_fields_on_a_queue_are_rejected_by_the_queue_contract(self) -> None:
        queue = NodeSpec(
            id="buffer",
            node_type="queue",
            plugin=None,
            input="buffer",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            scope_name="document_pages",
        )
        state = _state(queue)
        [entry] = _errors_for(state, "queue_config_invalid")
        assert "scope_name" in entry.message
        assert not _errors_for(state, "node_scope_fields_unsupported")


class TestCollectorScopeTopology:
    def test_duplicate_scope_name_is_rejected(self) -> None:
        state = _state(
            _transform("explode", "rows", "pages"),
            _transform("explode2", "rows", "pages2"),
            _collector(),
            _collector("other_closer", input_name="pages2", scope_opener="explode2"),
        )
        [entry] = _errors_for(state, "scope_name_duplicate")
        assert "document_pages" in entry.message

    def test_duplicate_scope_opener_is_rejected(self) -> None:
        state = _state(
            _transform("explode", "rows", "pages"),
            _collector(),
            _collector("other_closer", input_name="pages", scope_name="other_scope"),
        )
        [entry] = _errors_for(state, "scope_opener_duplicate")
        assert "one scope per" in entry.message

    def test_two_transform_on_errors_naming_the_collector_do_not_collide_as_producers(self) -> None:
        # Rule-9 mechanism pin, collector analogue of the Task-11 coalesce
        # test in test_state_bound_regions.py: a closer-shaped on_error is a
        # DIVERT edge into an EXISTING node, not a claim to PRODUCE a
        # connection under the closer's name. Before the carve-out included
        # collectors, TWO in-scope claimants produced a false-positive
        # duplicate_connection_producer ("node 't1' on_error and node 't2'
        # on_error") on a shape the committed builder ACCEPTS with two DIVERT
        # edges — the banned composer-red/runtime-green drift.
        state = _state(
            _transform("explode", "rows", "pages"),
            _transform("t1", "pages", "mid", on_error="page_stitcher"),
            _transform("t2", "mid", "pages_done", on_error="page_stitcher"),
            _collector(input_name="pages_done"),
        )
        summary = state.validate()
        codes = {entry.error_code for entry in summary.errors}
        assert "duplicate_connection_producer" not in codes, summary.errors
        assert "transform_on_error_unknown_sink" not in codes, summary.errors

    def test_escalate_with_no_other_barrier_is_rejected(self) -> None:
        state = _state(
            _transform("explode", "rows", "pages"),
            _collector(scope_on_group_failure="escalate"),
        )
        [entry] = _errors_for(state, "scope_escalate_at_outermost")
        # Task 10 N1 message shape: name the collector AND its opener, never
        # "Scope '{collector}'".
        assert "Scope closed by collector 'page_stitcher' (opener 'explode')" in entry.message

    def test_escalate_with_another_barrier_present_is_accepted_for_stage_2(self) -> None:
        # Stage 1 does not compute bound regions: with another barrier in the
        # draft, membership needs the real builder — accepting is the safe
        # drift direction (Stage 2 rejects a genuinely outermost escalate with
        # the runtime message).
        coalesce = NodeSpec(
            id="join_rows",
            node_type="coalesce",
            plugin=None,
            input="left",
            on_success="out",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=("left", "right"),
            policy=None,
            merge=None,
        )
        state = _state(
            _transform("explode", "rows", "pages"),
            _collector(scope_on_group_failure="escalate"),
            coalesce,
        )
        assert not _errors_for(state, "scope_escalate_at_outermost")

    def test_quarantine_never_fires_the_escalate_rule(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector())
        assert not _errors_for(state, "scope_escalate_at_outermost")

    def test_transform_on_error_may_name_a_collector_closer(self) -> None:
        # Spec §7 rule 9 relax now covers collector closers: the builder's
        # closer_name_to_node holds all three closer kinds.
        inner = NodeSpec(
            id="page_step",
            node_type="transform",
            plugin="passthrough",
            input="pages",
            on_success="staged",
            on_error="page_stitcher",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = _state(
            _transform("explode", "rows", "pages"),
            inner,
            _collector(input_name="staged"),
        )
        assert not _errors_for(state, "transform_on_error_unknown_sink")

    def test_collector_id_collision_message_names_collectors(self) -> None:
        state = _state(
            _transform("explode", "rows", "pages"),
            _collector("out"),  # collides with the sink name
        )
        [entry] = _errors_for(state, "node_id_collides_with_source_or_sink")
        assert "collectors" in entry.message

    def test_collection_cap_counts_collectors(self) -> None:
        crowd = tuple(_collector(f"stitch_{index:03d}", scope_name=f"scope_{index:03d}", scope_opener="explode") for index in range(101))
        state = _state(_transform("explode", "rows", "pages"), *crowd)
        assert any("collectors" in entry.message for entry in _errors_for(state, "pipeline_collection_cap_exceeded"))


_ROUND_TRIP_YAML = """
sources:
  main:
    plugin: csv
    on_success: rows
    options:
      path: in.csv
      schema:
        mode: observed
transforms:
  - name: explode
    plugin: json_explode
    input: rows
    on_success: pages
    on_error: discard
    options:
      field: items
      schema:
        mode: observed
collectors:
  - name: page_stitcher
    plugin: batch_stats
    input: pages
    on_success: out
    on_error: discard
    options:
      schema:
        mode: observed
scopes:
  - name: document_pages
    opener: explode
    closer: page_stitcher
    policy: require_all
    on_group_failure: quarantine
sinks:
  out:
    plugin: json
    options:
      path: out.json
    on_write_failure: discard
"""


class TestImporterAndGenerator:
    def test_import_folds_scopes_onto_collector_nodes(self) -> None:
        state = composition_state_from_runtime_yaml(_ROUND_TRIP_YAML)
        [collector] = [node for node in state.nodes if node.node_type == "collector"]
        assert collector.id == "page_stitcher"
        assert collector.plugin == "batch_stats"
        assert collector.input == "pages"
        assert collector.on_success == "out"
        assert collector.on_error == "discard"
        assert collector.scope_name == "document_pages"
        assert collector.scope_opener == "explode"
        assert collector.scope_policy == "require_all"
        assert collector.scope_on_group_failure == "quarantine"

    def test_import_defaults_omitted_on_group_failure_to_quarantine(self) -> None:
        yaml_text = _ROUND_TRIP_YAML.replace("    on_group_failure: quarantine\n", "")
        state = composition_state_from_runtime_yaml(yaml_text)
        [collector] = [node for node in state.nodes if node.node_type == "collector"]
        assert collector.scope_on_group_failure == "quarantine"

    def test_import_rejects_scope_with_unknown_closer(self) -> None:
        yaml_text = _ROUND_TRIP_YAML.replace("closer: page_stitcher", "closer: nobody")
        with pytest.raises(RuntimeYamlImportError, match="collectors: entry"):
            composition_state_from_runtime_yaml(yaml_text)

    def test_import_rejects_second_scope_on_one_closer(self) -> None:
        yaml_text = _ROUND_TRIP_YAML.replace(
            "sinks:",
            "  - name: second_scope\n    opener: explode\n    closer: page_stitcher\n    policy: require_all\nsinks:",
        )
        with pytest.raises(RuntimeYamlImportError, match="one scope per closer"):
            composition_state_from_runtime_yaml(yaml_text)

    def test_import_rejects_unknown_collector_field(self) -> None:
        yaml_text = _ROUND_TRIP_YAML.replace("    input: pages\n", "    input: pages\n    trigger: {}\n")
        with pytest.raises(RuntimeYamlImportError, match="unknown or inapplicable"):
            composition_state_from_runtime_yaml(yaml_text)

    def test_full_round_trip_preserves_collectors_and_scopes(self) -> None:
        original = load_settings_from_yaml_string(_ROUND_TRIP_YAML)
        state = composition_state_from_runtime_yaml(_ROUND_TRIP_YAML)
        regenerated = generate_yaml(state)
        result = load_settings_from_yaml_string(regenerated)
        assert result.collectors == original.collectors
        assert result.scopes == original.scopes

    def test_generator_refuses_to_lower_a_collector_without_scope_policy(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(scope_policy=None))
        with pytest.raises(PipelineLoweringError, match="scope_policy"):
            generate_yaml(state)

    def test_generator_refuses_to_lower_a_pluginless_collector(self) -> None:
        state = _state(_transform("explode", "rows", "pages"), _collector(plugin=None))
        with pytest.raises(PipelineLoweringError, match="plugin"):
            generate_yaml(state)
