"""Stage-1 structural and routing validation of composition state."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from elspeth.contracts.sink import FAILSINK_ELIGIBLE_SINK_PLUGINS
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    EdgeType,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)


class TestStage1Validation:
    def _empty_state(self) -> CompositionState:
        return CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _make_source(self, on_success: str = "t1", on_validation_failure: str = "discard") -> SourceSpec:
        return SourceSpec(
            plugin="csv",
            on_success=on_success,
            options={},
            on_validation_failure=on_validation_failure,
        )

    def _make_transform(
        self,
        id: str,
        input: str,
        on_success: str,
        on_error: str = "discard",
    ) -> NodeSpec:
        return NodeSpec(
            id=id,
            node_type="transform",
            plugin="passthrough",
            input=input,
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

    def _make_output(self, name: str = "main") -> OutputSpec:
        return OutputSpec(name=name, plugin="csv", options={}, on_write_failure="discard")

    def _make_edge(
        self,
        id: str,
        from_node: str,
        to_node: str,
        edge_type: EdgeType = "on_success",
    ) -> EdgeSpec:
        return EdgeSpec(id=id, from_node=from_node, to_node=to_node, edge_type=edge_type, label=None)

    def _coalesce_route_state(self, *, on_success: str | None) -> CompositionState:
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="gate_in"))
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("path_a", "path_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            NodeSpec(
                id="merge_point",
                node_type="coalesce",
                plugin=None,
                input="join",
                on_success=on_success,
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches=("path_a", "path_b"),
                policy="require_all",
                merge="nested",
            )
        )
        return state.with_output(self._make_output("main"))

    def test_empty_state_has_errors(self) -> None:
        result = self._empty_state().validate()
        assert not result.is_valid
        assert any(e.message == "No source configured." for e in result.errors)
        assert any(e.message == "No sinks configured." for e in result.errors)

    def test_minimal_valid_pipeline(self) -> None:
        """source -> transform -> sink, fully connected."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid, result.errors

    def _single_transform_state(self, node_id: str) -> CompositionState:
        """source -> <node_id> -> sink; the two-call pair differs ONLY in the node id."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t_in"))
        state = state.with_node(self._make_transform(node_id, "t_in", "main"))
        return state.with_output(self._make_output("main"))

    @pytest.mark.parametrize(
        ("node_id", "reason"),
        [
            pytest.param("t" * 39, "exceeds max length 38", id="too-long"),
            pytest.param("1abc", "start with a letter", id="leading-digit"),
            pytest.param("has space", "invalid characters", id="whitespace"),
            # The runtime's ``__`` branch is unreachable for node names (the
            # character class already demands a leading letter); the mirror
            # keeps the same order, so this trips the same rule the runtime does.
            pytest.param("__private", "invalid characters", id="dunder-prefix"),
            pytest.param("continue", "is reserved", id="reserved-edge-label"),
            pytest.param("fork", "is reserved", id="reserved-fork"),
        ],
    )
    def test_node_id_violating_runtime_name_rules_is_rejected(self, node_id: str, reason: str) -> None:
        """A node id the runtime settings model rejects must not validate green.

        elspeth-2ed41f0a4a (census 2026-08-17). Every runtime node kind
        (transform/gate/aggregation/coalesce/row_union/queue) validates its
        ``name`` through ``core/config.py::validate_runtime_node_name`` — max
        38, ``^[a-zA-Z][a-zA-Z0-9_-]*$``, not a reserved edge label, no ``__``
        prefix. Stage 1 mirrored those rules for SOURCE names only
        (``_composer_source_name_validation_message``); a 60-character
        transform id validated green and died at ``settings_load`` with
        "Transform name exceeds max length 38".
        """
        result = self._single_transform_state(node_id).validate()

        assert not result.is_valid
        [entry] = [e for e in result.errors if e.error_code == "node_id_invalid"]
        assert entry.component == f"node:{node_id}"
        assert reason in entry.message

    def test_node_id_at_runtime_limits_is_accepted(self) -> None:
        """Positive control: a 38-char id with every permitted character class validates green."""
        node_id = "T" + "a1_-" * 9 + "z"
        assert len(node_id) == 38
        result = self._single_transform_state(node_id).validate()

        assert result.is_valid, result.errors

    def test_queue_node_id_must_be_lowercase_like_the_runtime(self) -> None:
        """Queue names are lowercase-only at the runtime; transform names are not."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="Q_in"))
        queue = NodeSpec(
            id="Q_in",
            node_type="queue",
            plugin=None,
            input="Q_in",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(queue)
        state = state.with_node(self._make_transform("t1", "Q_in", "main"))
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        [entry] = [e for e in result.errors if e.error_code == "node_id_invalid"]
        assert entry.component == "node:Q_in"
        assert "lowercase" in entry.message

    def _label_probe_state(self, **overrides: Any) -> CompositionState:
        """source -> t1 -> main with one field overridden; the control passes no override."""
        source = SourceSpec(
            plugin="csv",
            on_success=overrides.get("source_on_success", "t_in"),
            options={},
            on_validation_failure="discard",
        )
        transform = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="passthrough",
            input=overrides.get("transform_input", "t_in"),
            on_success=overrides.get("transform_on_success", "main"),
            on_error=overrides.get("transform_on_error", "discard"),
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        output = OutputSpec(
            name=overrides.get("output_name", "main"),
            plugin="csv",
            options={},
            on_write_failure=overrides.get("on_write_failure", "discard"),
        )
        state = self._empty_state().with_source(source).with_node(transform)
        return state.with_output(output)

    @pytest.mark.parametrize(
        ("overrides", "code", "fragment"),
        [
            pytest.param(
                {"source_on_success": ""},
                "connection_label_invalid",
                "Source on_success must be a connection name or sink name",
                id="source-on_success-empty",
            ),
            pytest.param({"source_on_success": "__t"}, "connection_label_invalid", "starts with '__'", id="source-on_success-dunder"),
            pytest.param(
                {"transform_input": ""},
                "connection_label_invalid",
                "Transform input connection must not be empty",
                id="transform-input-empty",
            ),
            pytest.param({"transform_input": "continue"}, "connection_label_invalid", "is reserved", id="transform-input-reserved"),
            pytest.param({"transform_input": "t" * 65}, "connection_label_invalid", "exceeds max length 64", id="transform-input-too-long"),
            pytest.param(
                {"transform_on_success": "has space"}, "connection_label_invalid", "invalid characters", id="transform-on_success-chars"
            ),
            pytest.param(
                {"transform_on_error": ""},
                "connection_label_invalid",
                "on_error must be a sink name or 'discard'",
                id="transform-on_error-empty",
            ),
            pytest.param({"output_name": "Main"}, "output_name_invalid", "must be lowercase", id="output-name-uppercase"),
            pytest.param({"output_name": "__main"}, "output_name_invalid", "starts with '__'", id="output-name-dunder"),
            pytest.param(
                {"on_write_failure": ""},
                "connection_label_invalid",
                "on_write_failure must be a sink name or 'discard'",
                id="on_write_failure-empty",
            ),
        ],
    )
    def test_label_violating_runtime_rules_is_rejected(self, overrides: dict[str, Any], code: str, fragment: str) -> None:
        """Every routing label the runtime settings model validates must be validated by Stage 1 too.

        elspeth-2ed41f0a4a (census 2026-08-17). ``core/config.py`` runs
        ``_validate_connection_or_sink_name`` (non-empty, <=64, connection
        character class, not a reserved edge label, no ``__`` prefix) on every
        connection/route/branch label and ``validate_sink_name`` (plus
        lowercase, <=38) on every sink name. Stage 1 only noticed a bad label
        indirectly through the dangling-reference rules — and those go silent
        exactly when the bad label is CONSISTENT (a blank on_success feeding a
        blank input validated green), while ``_SetOutputArgumentsModel.sink_name``
        was an unconstrained string on the freeform path (elspeth-88a4db09f9).
        """
        result = self._label_probe_state(**overrides).validate()

        assert not result.is_valid
        matching = [e for e in result.errors if e.error_code == code]
        assert matching, [(e.error_code, e.message) for e in result.errors]
        assert any(fragment in e.message for e in matching), [e.message for e in matching]

    def test_label_probe_control_is_accepted(self) -> None:
        """Positive control for the label rules — the unmodified probe state validates green."""
        result = self._label_probe_state().validate()

        assert result.is_valid, result.errors

    def test_gate_route_and_fork_labels_follow_runtime_rules(self) -> None:
        """Route labels must be non-empty and valid; ``continue`` is a removed destination; fork branch names are labels too."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="g_in"))
        state = state.with_node(
            NodeSpec(
                id="g",
                node_type="gate",
                plugin=None,
                input="g_in",
                on_success=None,
                on_error=None,
                options={},
                condition="'x'",
                routes={"": "main", "b": "continue", "c": "fork"},
                fork_to=("", "__hidden"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        messages = [e.message for e in result.errors if e.error_code == "connection_label_invalid"]
        assert any("Route labels must not be empty" in m for m in messages), result.errors
        assert any("Route destination 'continue' has been removed" in m for m in messages), result.errors
        assert any("Fork branch names must not be empty" in m for m in messages), result.errors
        assert any("Fork branch name '__hidden' starts with '__'" in m for m in messages), result.errors

    @pytest.mark.parametrize(
        ("on_error", "fragment"),
        [
            pytest.param("   ", "on_error must be a sink name or 'discard'", id="blank"),
            pytest.param("__errors", "starts with '__'", id="dunder"),
            pytest.param("has space", "invalid characters", id="chars"),
        ],
    )
    def test_aggregation_on_error_follows_runtime_label_rules(self, on_error: str, fragment: str) -> None:
        """AggregationSettings.on_error has the same parse-time validator as a
        transform's (it is now a live __error_<name>__ edge target), so Stage 1
        mirrors it (elspeth-d2e3f29d10)."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg_in"))
        state = state.with_node(
            NodeSpec(
                id="stats",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg_in",
                on_success="main",
                on_error=on_error,
                options={"schema": {"mode": "observed"}, "value_field": "value"},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                trigger={"count": 2},
                output_mode="transform",
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        messages = [e.message for e in result.errors if e.error_code == "connection_label_invalid" and e.component == "node:stats"]
        assert any(fragment in m for m in messages), result.errors

    def test_coalesce_branch_labels_follow_runtime_rules(self) -> None:
        """Branch keys/values must be non-empty, valid, and not collide after trimming or repeat in list form."""

        def coalesce(node_id: str, branches: Any) -> NodeSpec:
            return NodeSpec(
                id=node_id,
                node_type="coalesce",
                plugin=None,
                input="cx",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches=branches,
                policy="require_all",
                merge="union",
            )

        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="g_in"))
        state = state.with_node(
            NodeSpec(
                id="g",
                node_type="gate",
                plugin=None,
                input="g_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "discard"},
                fork_to=("x", "x ", "p", "e"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(coalesce("c_trim", {"x": "cx", "x ": "cy"}))
        state = state.with_node(coalesce("c_empty", {"e": ""}))
        state = state.with_node(coalesce("c_list", ("p", "p")))
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        by_component: dict[str, list[str]] = {}
        for e in result.errors:
            if e.error_code == "connection_label_invalid":
                by_component.setdefault(e.component, []).append(e.message)
        assert any("collide after trimming whitespace" in m for m in by_component.get("node:c_trim", ())), result.errors
        assert any("input connection must not be empty" in m for m in by_component.get("node:c_empty", ())), result.errors
        assert by_component.get("node:c_list") == ["Duplicate branch names in list: ['p']"], result.errors

    def test_gate_empty_fork_to_is_rejected(self) -> None:
        """``fork_to=()`` is neither "no fork" nor a fork: the runtime rejects it, and used to crash on it."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="g_in"))
        state = state.with_node(
            NodeSpec(
                id="g",
                node_type="gate",
                plugin=None,
                input="g_in",
                on_success=None,
                on_error=None,
                options={},
                condition="row['x'] > 0",
                routes={"true": "main", "false": "discard"},
                fork_to=(),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        [entry] = [e for e in result.errors if e.error_code == "gate_fork_to_empty"]
        assert entry.component == "node:g"

    @pytest.mark.parametrize("collides_with", ["sink", "source"])
    def test_node_id_colliding_with_sink_or_source_name_is_rejected(self, collides_with: str) -> None:
        """One namespace across nodes, sources and sinks (``validate_globally_unique_node_names``)."""
        state = CompositionState(
            sources={"primary": self._make_source(on_success="t_in")},
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        node_id = "primary" if collides_with == "source" else "main"
        state = state.with_node(self._make_transform(node_id, "t_in", "main"))
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        [entry] = [e for e in result.errors if e.error_code == "node_id_collides_with_source_or_sink"]
        assert entry.component == f"node:{node_id}"
        assert f"used by both transform and {collides_with}" in entry.message

    def _fork_gate(self, node_id: str, input_name: str, fork_to: tuple[str, ...]) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="gate",
            plugin=None,
            input=input_name,
            on_success=None,
            on_error=None,
            options={},
            condition="True",
            routes={"true": "fork", "false": "fork"},
            fork_to=fork_to,
            branches=None,
            policy=None,
            merge=None,
        )

    def test_fork_branch_declared_by_two_gates_over_sink_names_is_rejected(self) -> None:
        """Fork branch names are globally unique across gates — even when they are also sink names.

        elspeth-2ed41f0a4a (census 2026-08-17). The duplicate-producer
        accounting deliberately skips branch names that are sink names, so
        two gates each forking to ['main', 'other'] validated green while
        the DAG build raised "Fork branch 'main' is declared by multiple
        gates". The control forks DISTINCT sink names from the second gate.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="g1_in"))
        state = state.with_node(self._fork_gate("g1", "g1_in", ("main", "other")))
        state = state.with_node(self._make_transform("t1", "other", "g2_in"))
        state = state.with_node(self._fork_gate("g2", "g2_in", ("main", "third")))
        for name in ("main", "other", "third"):
            state = state.with_output(self._make_output(name))

        result = state.validate()

        [entry] = [e for e in result.errors if e.error_code == "fork_branch_declared_by_multiple_gates"]
        assert entry.component == "node:g2"
        assert "'g1' and 'g2'" in entry.message

    def test_fork_branches_over_distinct_sink_names_are_accepted(self) -> None:
        """Positive control for the multi-gate fork-branch rule."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="g1_in"))
        state = state.with_node(self._fork_gate("g1", "g1_in", ("main", "other")))
        state = state.with_node(self._make_transform("t1", "other", "g2_in"))
        state = state.with_node(self._fork_gate("g2", "g2_in", ("third", "fourth")))
        for name in ("main", "other", "third", "fourth"):
            state = state.with_output(self._make_output(name))

        result = state.validate()

        assert not any(e.error_code == "fork_branch_declared_by_multiple_gates" for e in result.errors), result.errors

    def test_single_branch_coalesce_is_rejected(self) -> None:
        """``CoalesceSettings.branches`` is ``min_length=2`` — declarative, so no raise site names it."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="g_in"))
        state = state.with_node(self._fork_gate("g", "g_in", ("x",)))
        state = state.with_node(self._make_transform("tx", "x", "cx"))
        state = state.with_node(
            NodeSpec(
                id="c",
                node_type="coalesce",
                plugin=None,
                input="cx",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"x": "cx"},
                policy="require_all",
                merge="union",
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        [entry] = [e for e in result.errors if e.error_code == "coalesce_branches_invalid"]
        assert entry.component == "node:c"

    def test_coalesce_branch_alias_no_gate_forks_is_rejected(self) -> None:
        """A coalesce branch KEY must be some gate's fork_to entry (row_union already had this rule)."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="g_in"))
        state = state.with_node(self._fork_gate("g", "g_in", ("b2", "b3")))
        state = state.with_node(self._make_transform("t1", "b3", "t1_out"))
        state = state.with_node(
            NodeSpec(
                id="c",
                node_type="coalesce",
                plugin=None,
                input="b2",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"b1": "t1_out", "b2": "b2"},
                policy="require_all",
                merge="union",
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        [entry] = [e for e in result.errors if e.error_code == "coalesce_branch_alias_unreachable"]
        assert entry.component == "node:c"
        assert "['b1']" in entry.message

    def test_more_sinks_than_the_runtime_cap_is_rejected(self) -> None:
        """``ElspethSettings.sinks`` is ``Field(max_length=50)`` — declarative, no raise site; 51 sinks validated green."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="main0"))
        for index in range(51):
            state = state.with_output(self._make_output(f"main{index}"))

        result = state.validate()

        [entry] = [e for e in result.errors if e.error_code == "pipeline_collection_cap_exceeded"]
        assert "51 sinks" in entry.message and "at most 50" in entry.message

    def test_gate_with_more_than_32_fork_branches_is_rejected(self) -> None:
        """``GateSettings.fork_to`` is ``Field(max_length=32)``."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="g_in"))
        branches = tuple(f"b{i}" for i in range(33))
        state = state.with_node(self._fork_gate("g", "g_in", branches))
        for branch in branches:
            state = state.with_output(self._make_output(branch))

        result = state.validate()

        assert any(e.error_code == "pipeline_collection_cap_exceeded" and "33 fork branches" in e.message for e in result.errors), (
            result.errors
        )

    @pytest.mark.parametrize("provider", ["fake", {"name": "openrouter"}], ids=["unknown-string", "mapping"])
    def test_llm_node_with_bad_provider_returns_a_verdict_not_a_crash(self, provider: object) -> None:
        """``validate()`` must never raise on authorable options.

        An llm node whose ``provider`` was an unknown string, or a mapping,
        made the semantic validator's consumer probe raise a bare
        ``ValueError`` / ``TypeError`` out of ``validate()`` — a 500 for a
        ``upsert_node`` payload (elspeth-2ed41f0a4a census, 2026-08-17). Plugin
        OPTION validity is owned by the tool-layer prevalidation and Stage 2's
        plugin_instantiation; Stage 1 abstains on an un-constructable draft,
        so the verdict here is simply "no crash".
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t_in"))
        state = state.with_node(
            NodeSpec(
                id="t1",
                node_type="transform",
                plugin="llm",
                input="t_in",
                on_success="main",
                on_error="discard",
                options={"schema": {"mode": "observed"}, "provider": provider, "model": "m", "prompt": "p"},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()  # must not raise

        assert isinstance(result.is_valid, bool)

    def test_transform_cycle_is_rejected(self) -> None:
        """Two transforms feeding each other must not validate green.

        elspeth-2ed41f0a4a (census 2026-08-17). Stage 1 had NO cycle detection
        at all: ``t1.input=b, t1.on_success=c; t2.input=c, t2.on_success=b``
        satisfies the per-node "input has a producer" check on both sides, so
        the pipeline validated ``is_valid=True`` with no warning, while the
        DAG build raised ``GraphValidationError`` "Pipeline contains a cycle".
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="main"))
        state = state.with_node(self._make_transform("t1", "b", "c"))
        state = state.with_node(self._make_transform("t2", "c", "b"))
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert not result.is_valid
        [entry] = [e for e in result.errors if e.error_code == "pipeline_cycle"]
        assert "t1" in entry.message and "t2" in entry.message
        assert entry.component in ("node:t1", "node:t2")

    def _implicit_aggregation_cycle_state(self) -> CompositionState:
        """A clean main path, plus a 2-cycle closed through an implicit self-publisher.

        ``agg`` omits ``on_success``, so it publishes under its own id;
        ``t2`` consumes ``agg`` and routes back to ``agg``'s own input ``b``.
        Deliberately free of every other defect — one producer per connection,
        every input reachable — so the ONLY thing under test is the cycle.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="rows"))
        state = state.with_node(self._make_transform("t0", "rows", "main", on_error="main"))
        state = state.with_node(
            NodeSpec(
                id="agg",
                node_type="aggregation",
                plugin="batch_stats",
                input="b",
                on_success=None,
                on_error="main",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                trigger={"count": 2},
            )
        )
        state = state.with_node(self._make_transform("t2", "agg", "b", on_error="main"))
        return state.with_output(self._make_output("main"))

    def test_cycle_through_an_implicitly_publishing_aggregation_is_rejected(self) -> None:
        """The composer used to GREEN this and the engine killed it at build.

        A false accept, the worst polarity. ``_node_published_connections``
        restated the publishing rule by hand as "a coalesce with no
        ``on_success``", which omits the other implicit self-publishers. An
        aggregation that omits ``on_success`` publishes under its own id
        (``core/dag/builder.py`` registers ``agg_settings.name``), so the
        cycle detector saw no outbound edge from ``agg`` and found nothing.

        Measured on this exact shape before the fix:

            composer : is_valid=True, errors=[]
            engine   : GraphValidationError: Pipeline contains a cycle:
                       transform_t2_... -> aggregation_agg_...

        Deriving the success channel from ``published_success_connection``
        closes it. Do not restate the rule here again.
        """
        result = self._implicit_aggregation_cycle_state().validate()

        assert not result.is_valid
        cycle_errors = [e for e in result.errors if e.error_code == "pipeline_cycle"]
        assert cycle_errors, [e.to_dict() for e in result.errors]
        message = cycle_errors[0].message
        assert "agg" in message and "t2" in message, message

    def test_the_same_cycle_spelled_explicitly_was_always_rejected(self) -> None:
        """Isolates the blindness to the IMPLICIT arm, not to aggregations.

        Control contributed by the hunt-strategy lane's independent repro. The
        identical graph, with the aggregation naming ``on_success: "agg"``
        instead of omitting it, was rejected before this fix and after — the
        hand restatement's ``elif node.on_success is not None`` arm caught it.

        Without this control, "an aggregation cycle was missed" could equally
        mean the detector was blind to aggregations generally. It is not: it
        was blind to the one arm that had to ask the helper.
        """
        state = self._implicit_aggregation_cycle_state()
        explicit = tuple(replace(node, on_success="agg") if node.id == "agg" else node for node in state.nodes)
        state = replace(state, nodes=explicit)

        result = state.validate()

        assert not result.is_valid
        assert [e for e in result.errors if e.error_code == "pipeline_cycle"], [e.to_dict() for e in result.errors]

    def test_a_queue_pipeline_gains_no_spurious_cycle(self) -> None:
        """The guard the aggregation arm must not dissolve.

        A queue's ``input`` IS its own id, and it publishes under that id, so
        deriving the success channel makes ``published_success_connection``
        return a queue's own id here. That would be a self-edge — a cycle on
        EVERY pipeline containing a queue — were it not for
        ``_node_topology_cycle`` skipping queues in both its consumer and
        successor maps, which is why this function needs no carve-out.

        This pins that coupling behaviourally: remove either skip and this
        goes red rather than shipping a spurious rejection.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="rows"))
        state = state.with_node(
            NodeSpec(
                id="q",
                node_type="queue",
                plugin=None,
                input="q",
                on_success=None,
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(self._make_transform("feed", "rows", "q", on_error="main"))
        state = state.with_node(self._make_transform("drain", "q", "main", on_error="main"))
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert not any(e.error_code == "pipeline_cycle" for e in result.errors), [e.to_dict() for e in result.errors]
        assert result.is_valid, [e.to_dict() for e in result.errors]

    def test_gate_routing_back_upstream_is_rejected_as_a_cycle(self) -> None:
        """A gate whose route re-enters an ancestor's input connection is a cycle through the gate."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t_in"))
        state = state.with_node(self._make_transform("t1", "t_in", "g_in"))
        state = state.with_node(
            NodeSpec(
                id="g",
                node_type="gate",
                plugin=None,
                input="g_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "main", "false": "t_in"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        cycle_errors = [e for e in result.errors if e.error_code == "pipeline_cycle"]
        assert len(cycle_errors) == 1, result.errors
        assert "g" in cycle_errors[0].message and "t1" in cycle_errors[0].message

    def test_fork_and_coalesce_diamond_is_not_a_cycle(self) -> None:
        """Positive control: a fork/coalesce diamond revisits no node and must stay green."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="g_in"))
        state = state.with_node(
            NodeSpec(
                id="g",
                node_type="gate",
                plugin=None,
                input="g_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("a", "b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(self._make_transform("ta", "a", "a_done"))
        state = state.with_node(self._make_transform("tb", "b", "b_done"))
        state = state.with_node(
            NodeSpec(
                id="c",
                node_type="coalesce",
                plugin=None,
                input="a_done",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"a": "a_done", "b": "b_done"},
                policy="require_all",
                merge="union",
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert not any(e.error_code == "pipeline_cycle" for e in result.errors), result.errors
        assert result.is_valid, result.errors

    def test_connection_only_runtime_pipeline_is_valid_without_ui_edges(self) -> None:
        """Runtime connection fields, not UI edges, determine Stage 1 validity."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert result.is_valid, result.errors

    def test_connection_only_coalesce_pipeline_is_valid_without_ui_edges(self) -> None:
        """Coalesce terminal routes are valid when declared in runtime fields."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="gate_in"))
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("path_a", "path_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            NodeSpec(
                id="merge_point",
                node_type="coalesce",
                plugin=None,
                input="join",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches=("path_a", "path_b"),
                policy="require_all",
                merge="nested",
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert result.is_valid, result.errors

    def test_coalesce_timeout_survives_serialization_round_trip(self) -> None:
        state = self._coalesce_route_state(on_success="main")
        coalesce = next(node for node in state.nodes if node.node_type == "coalesce")
        state = state.with_node(replace(coalesce, timeout_seconds=5.0))

        restored = CompositionState.from_dict(state.to_dict())

        assert restored == state
        assert next(node for node in restored.nodes if node.node_type == "coalesce").timeout_seconds == 5.0

    @pytest.mark.parametrize(
        "timeout_seconds",
        # 10**400 and its negation are only reachable from a persisted session
        # payload (JSON has no integer ceiling and NodeSpec.from_dict does not
        # cross the Pydantic tool boundary). float() overflows on them, which
        # used to abort validate() with OverflowError instead of rejecting.
        [True, float("nan"), float("inf"), 0.0, -1.0, 10**400, -(10**400)],
    )
    def test_coalesce_rejects_invalid_timeout(self, timeout_seconds: object) -> None:
        state = self._coalesce_route_state(on_success="main")
        coalesce = next(node for node in state.nodes if node.node_type == "coalesce")
        state = state.with_node(replace(coalesce, timeout_seconds=timeout_seconds))

        result = state.validate()

        assert any(error.error_code == "coalesce_timeout_invalid" for error in result.errors)

    @pytest.mark.parametrize(
        ("node", "error_code"),
        [
            pytest.param(
                NodeSpec(
                    id="transform_1",
                    node_type="transform",
                    plugin="passthrough",
                    input="transform_1",
                    on_success="main",
                    on_error="discard",
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    timeout_seconds=5.0,
                ),
                "node_timeout_unsupported",
                id="transform",
            ),
            pytest.param(
                NodeSpec(
                    id="gate_1",
                    node_type="gate",
                    plugin=None,
                    input="gate_1",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="True",
                    routes={"true": "main", "false": "main"},
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    timeout_seconds=5.0,
                ),
                "node_timeout_unsupported",
                id="gate",
            ),
            pytest.param(
                NodeSpec(
                    id="aggregation_1",
                    node_type="aggregation",
                    plugin="batch_counter",
                    input="aggregation_1",
                    on_success="main",
                    on_error="discard",
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    trigger={"count": 100, "timeout_seconds": 5.0},
                    timeout_seconds=5.0,
                ),
                "node_timeout_unsupported",
                id="aggregation",
            ),
            pytest.param(
                NodeSpec(
                    id="queue_1",
                    node_type="queue",
                    plugin=None,
                    input="queue_1",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    timeout_seconds=5.0,
                ),
                "queue_config_invalid",
                id="queue",
            ),
        ],
    )
    def test_only_barrier_nodes_accept_top_level_timeout(self, node: NodeSpec, error_code: str) -> None:
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success=node.input))
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert any(error.error_code == error_code and "timeout_seconds" in error.message for error in result.errors), result.errors

    @pytest.mark.parametrize(
        "node",
        [
            pytest.param(
                NodeSpec(
                    id="gate_1",
                    node_type="gate",
                    plugin="fork",
                    input="gate_1",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="row['x'] > 1",
                    routes={"true": "fork", "false": "main"},
                    fork_to=("main", "alt"),
                    branches=None,
                    policy=None,
                    merge=None,
                ),
                id="gate",
            ),
            pytest.param(
                NodeSpec(
                    id="coalesce_1",
                    node_type="coalesce",
                    plugin="passthrough",
                    input="coalesce_1",
                    on_success="main",
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=("path_a", "path_b"),
                    policy="require_all",
                    merge="union",
                ),
                id="coalesce",
            ),
        ],
    )
    def test_structural_nodes_reject_a_plugin(self, node: NodeSpec) -> None:
        """gate and coalesce are built-in node_types wired with plugin=null.

        Queues (``queue_node_contract_error``) and row_unions (their
        forbidden-fields block) already reject a plugin; gate and coalesce
        silently persisted one, so a state could carry ``('g', 'gate', 'fork')``
        while ``is_valid`` — an authored token that neither generated YAML nor
        the runtime ever sees.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success=node.input))
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("alt"))

        result = state.validate()

        matching = [e for e in result.errors if e.error_code == "structural_node_plugin_forbidden"]
        assert len(matching) == 1, result.errors
        assert matching[0].component == f"node:{node.id}"
        assert "plugin" in matching[0].message and "plugin=null" in matching[0].message
        assert node.plugin not in matching[0].message, "the rejected plugin token is not echoed"

    def test_structural_node_without_plugin_is_not_flagged(self) -> None:
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="gate_1"))
        state = state.with_node(
            NodeSpec(
                id="gate_1",
                node_type="gate",
                plugin=None,
                input="gate_1",
                on_success=None,
                on_error=None,
                options={},
                condition="row['x'] > 1",
                routes={"true": "fork", "false": "main"},
                fork_to=("main", "alt"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("alt"))

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any(e.error_code == "structural_node_plugin_forbidden" for e in result.errors)

    def test_multiple_fork_gates_do_not_collide_on_fork_route_keyword(self) -> None:
        """Two gates routing to the reserved 'fork' keyword are not duplicate producers.

        Regression test for elspeth-b6940369a7: route target 'fork' is the
        fork-mode keyword, not a connection, so any number of gates may use it.
        Before the fix the web validator reported "Duplicate producer for
        connection 'fork'" and rejected multi-fork topologies the engine builds.

        Topology mirrors an engine-valid build (verified via
        from_plugin_instances): a boolean router gate splits into two fork
        gates whose branches terminate directly at sinks — no coalesce, so the
        only thing under test is the two gates both routing to 'fork'.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="router_in"))
        state = state.with_node(
            NodeSpec(
                id="router",
                node_type="gate",
                plugin=None,
                input="router_in",
                on_success=None,
                on_error=None,
                options={},
                condition="row['x'] > 0",
                routes={"true": "fa_in", "false": "fb_in"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            NodeSpec(
                id="gate_a",
                node_type="gate",
                plugin=None,
                input="fa_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("sink_a1", "sink_a2"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            NodeSpec(
                id="gate_b",
                node_type="gate",
                plugin=None,
                input="fb_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("sink_b1", "sink_b2"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        for sink_name in ("sink_a1", "sink_a2", "sink_b1", "sink_b2"):
            state = state.with_output(self._make_output(sink_name))

        result = state.validate()

        assert result.is_valid, result.errors

    def test_gate_route_to_discard_is_valid_without_output_named_discard(self) -> None:
        """Gate routes may target virtual 'discard' without declaring a sink."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="gate_in"))
        state = state.with_node(
            NodeSpec(
                id="quality_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="row['keep']",
                routes={"true": "main", "false": "discard"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert result.is_valid, result.errors

    def test_dangling_edge_from_node(self) -> None:
        state = self._empty_state()
        state = state.with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "nonexistent", "main"))
        result = state.validate()
        assert not result.is_valid
        assert any("nonexistent" in e.message and "from_node" in e.message for e in result.errors)

    def test_dangling_edge_to_node(self) -> None:
        state = self._empty_state()
        state = state.with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "nonexistent"))
        result = state.validate()
        assert not result.is_valid
        assert any("nonexistent" in e.message and "to_node" in e.message for e in result.errors)

    def test_duplicate_node_ids(self) -> None:
        """Two nodes with same id — caught by validation, not by with_node (which replaces)."""
        node = self._make_transform("dup", "in", "out")
        state = CompositionState(
            source=self._make_source(),
            nodes=(node, node),
            edges=(),
            outputs=(self._make_output(),),
            metadata=PipelineMetadata(),
            version=1,
        )
        result = state.validate()
        assert not result.is_valid
        assert any("Duplicate node ID" in e.message for e in result.errors)

    def test_duplicate_output_names(self) -> None:
        out = self._make_output("dup")
        state = CompositionState(
            source=self._make_source(),
            nodes=(),
            edges=(),
            outputs=(out, out),
            metadata=PipelineMetadata(),
            version=1,
        )
        result = state.validate()
        assert not result.is_valid
        assert any("Duplicate output name" in e.message for e in result.errors)

    def test_duplicate_edge_ids(self) -> None:
        edge = self._make_edge("dup", "source", "main")
        state = CompositionState(
            source=self._make_source(),
            nodes=(),
            edges=(edge, edge),
            outputs=(self._make_output(),),
            metadata=PipelineMetadata(),
            version=1,
        )
        result = state.validate()
        assert not result.is_valid
        assert any("Duplicate edge ID" in e.message for e in result.errors)

    def test_gate_missing_condition(self) -> None:
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes={"high": "s1"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("condition" in e.message for e in result.errors)

    def test_gate_malformed_condition_syntax_error(self) -> None:
        """validate() catches condition with invalid Python syntax."""
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="row['x'] >== 5",
            routes={"true": "s1", "false": "s2"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("Invalid gate condition syntax" in e.message for e in result.errors)

    def test_gate_injection_condition_security_error(self) -> None:
        """validate() catches injection attempts in conditions."""
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="__import__('os').system('rm -rf /')",
            routes={"true": "s1", "false": "s2"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("Forbidden construct in gate condition" in e.message for e in result.errors)

    def test_gate_forbidden_function_call_condition(self) -> None:
        """validate() catches forbidden function calls (eval, exec, etc.)."""
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="eval('1+1')",
            routes={"true": "s1", "false": "s2"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("Forbidden construct in gate condition" in e.message for e in result.errors)

    def test_gate_valid_condition_passes_validation(self) -> None:
        """validate() accepts well-formed gate conditions."""
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="row['score'] >= 0.85",
            routes={"true": "s1", "false": "s2"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        # Only structural errors may remain (connection completeness etc.),
        # but no expression-related errors
        expr_errors = [e for e in result.errors if "gate condition" in e.message.lower()]
        assert expr_errors == []

    def test_gate_lambda_condition_rejected(self) -> None:
        """validate() catches lambda expressions in conditions."""
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="lambda: True",
            routes={"true": "s1", "false": "s2"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("Forbidden construct in gate condition" in e.message for e in result.errors)

    def test_gate_comprehension_condition_rejected(self) -> None:
        """validate() catches list comprehensions in conditions."""
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="[x for x in range(10)]",
            routes={"true": "s1", "false": "s2"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("Forbidden construct in gate condition" in e.message for e in result.errors)

    def test_gate_missing_routes(self) -> None:
        gate = NodeSpec(
            id="g1",
            node_type="gate",
            plugin=None,
            input="in",
            on_success=None,
            on_error=None,
            options={},
            condition="row['x'] > 1",
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(gate)
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        result = state.validate()
        assert not result.is_valid
        assert any("routes" in e.message for e in result.errors)

    def test_transform_with_condition_is_error(self) -> None:
        node = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="passthrough",
            input="in",
            on_success="out",
            on_error=None,
            options={},
            condition="row['x'] > 1",
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(node)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("condition" in e.message for e in result.errors)

    def test_coalesce_missing_branches(self) -> None:
        node = NodeSpec(
            id="c1",
            node_type="coalesce",
            plugin=None,
            input="join",
            on_success="out",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy="require_all",
            merge="nested",
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(node)
        state = state.with_edge(self._make_edge("e1", "source", "c1"))
        result = state.validate()
        assert not result.is_valid
        assert any("branches" in e.message for e in result.errors)

    def test_aggregation_missing_plugin(self) -> None:
        node = NodeSpec(
            id="a1",
            node_type="aggregation",
            plugin=None,
            input="in",
            on_success="out",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._empty_state().with_source(self._make_source())
        state = state.with_output(self._make_output())
        state = state.with_node(node)
        state = state.with_edge(self._make_edge("e1", "source", "a1"))
        result = state.validate()
        assert not result.is_valid
        assert any("plugin" in e.message for e in result.errors)

    def test_unknown_node_type_is_invalid(self) -> None:
        """Stage 1 must reject node types outside the closed runtime set."""
        node = NodeSpec.from_dict(
            {
                "id": "mystery",
                "node_type": "bogus",
                "plugin": "passthrough",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": {},
                "condition": None,
                "routes": None,
                "fork_to": None,
                "branches": None,
                "policy": None,
                "merge": None,
            }
        )
        state = self._empty_state().with_source(self._make_source(on_success="source_out"))
        state = state.with_output(self._make_output())
        state = state.with_node(node)
        state = state.with_edge(self._make_edge("e1", "source", "mystery"))

        result = state.validate()

        assert not result.is_valid
        assert any("unknown node_type 'bogus'" in e.message for e in result.errors)

    def test_unreachable_node(self) -> None:
        """Node exists but no edge points to it and source.on_success doesn't match."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="other"))
        state = state.with_node(self._make_transform("t1", "somewhere", "main"))
        state = state.with_output(self._make_output())
        result = state.validate()
        assert not result.is_valid
        assert any("not reachable" in e.message for e in result.errors)

    def test_validate_after_from_dict_round_trip(self) -> None:
        """W-4A-2: validate() on reconstructed state matches original."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))

        restored = CompositionState.from_dict(state.to_dict())
        result = restored.validate()
        assert result.is_valid, result.errors

    def test_edge_only_pipeline_is_invalid_when_runtime_connections_do_not_match(self) -> None:
        """UI edges cannot rescue runtime wiring that generate_yaml() will not emit."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="wrong_connection"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))

        result = state.validate()

        assert not result.is_valid
        assert any("runtime connection" in e.message for e in result.errors)

    @pytest.mark.parametrize(
        ("case_name", "expected_message"),
        [
            (
                "source_on_success",
                "Source on_success 'dangling' is neither a sink nor a known connection",
            ),
            (
                "transform_on_success",
                "Transform 't1' on_success 'dangling' is neither a sink nor a known connection",
            ),
            (
                "aggregation_on_success",
                "Aggregation 'agg1' on_success 'dangling' is neither a sink nor a known connection",
            ),
            (
                "coalesce_unknown_sink",
                "Coalesce 'merge_point' on_success references unknown sink 'dangling'",
            ),
            (
                "coalesce_connection_target",
                "Coalesce 'merge_point' has on_success='next_step'. Coalesce on_success must point to a sink when configured.",
            ),
            (
                "transform_on_error",
                "Transform 't1' on_error 'missing_error_sink' references unknown sink",
            ),
        ],
    )
    def test_validate_rejects_runtime_unresolvable_route_destinations(
        self,
        case_name: str,
        expected_message: str,
    ) -> None:
        """Stage 1 rejects terminal routes that the runtime DAG builder rejects."""
        if case_name == "source_on_success":
            state = self._empty_state().with_source(self._make_source(on_success="dangling")).with_output(self._make_output("main"))
        elif case_name == "transform_on_success":
            state = self._empty_state()
            state = state.with_source(self._make_source(on_success="t1"))
            state = state.with_node(self._make_transform("t1", "t1", "dangling"))
            state = state.with_output(self._make_output("main"))
        elif case_name == "aggregation_on_success":
            state = self._empty_state()
            state = state.with_source(self._make_source(on_success="agg1"))
            state = state.with_node(
                NodeSpec(
                    id="agg1",
                    node_type="aggregation",
                    plugin="batch_counter",
                    input="agg1",
                    on_success="dangling",
                    on_error="discard",
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    trigger={"count": 1},
                )
            )
            state = state.with_output(self._make_output("main"))
        elif case_name == "coalesce_unknown_sink":
            state = self._coalesce_route_state(on_success="dangling")
        elif case_name == "coalesce_connection_target":
            state = self._coalesce_route_state(on_success="next_step")
            state = state.with_node(self._make_transform("after_merge", "next_step", "main"))
        elif case_name == "transform_on_error":
            state = self._empty_state()
            state = state.with_source(self._make_source(on_success="t1"))
            state = state.with_node(self._make_transform("t1", "t1", "main", on_error="missing_error_sink"))
            state = state.with_output(self._make_output("main"))
        else:
            raise AssertionError(f"Unhandled route validation case: {case_name}")

        result = state.validate()

        assert not result.is_valid, case_name
        assert any(expected_message in error.message for error in result.errors), (case_name, result.errors)

    # --- Warning rules (W1-W4) ---

    def test_validate_output_no_incoming_edge_warns(self) -> None:
        """W1: Output with no edge targeting it produces a warning."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("orphan"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid
        assert any("orphan" in w.message and "never receive data" in w.message for w in result.warnings)

    def test_validate_w3_warns_on_a_dead_ended_non_terminal_coalesce(self) -> None:
        """W3 must still fire when NOTHING consumes the coalesce.

        Regression pair for the two ways this warning has been wrong. It first
        asked ``node.on_success is not None``, which accused every CONNECTED
        non-terminal coalesce (a false positive the composer's own planner saw
        and talked itself past). Replacing that with "does it publish
        anything?" fixed the false positive and introduced a false NEGATIVE:
        an implicit self-publisher always publishes — under its own id — so a
        genuinely dead-ended coalesce went unreported.

        The question that is right in both directions is whether the published
        connection is CONSUMED. Both halves are pinned here and in
        ``test_validate_w3_silent_when_the_coalesce_is_consumed`` below; do not
        "simplify" either back into a presence check.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="rows"))
        state = state.with_node(
            NodeSpec(
                id="fan",
                node_type="gate",
                plugin=None,
                input="rows",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=["ba", "bb"],
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(self._make_transform("a", "ba", "a_out"))
        state = state.with_node(self._make_transform("b", "bb", "b_out"))
        state = state.with_node(
            NodeSpec(
                id="merge",
                node_type="coalesce",
                plugin=None,
                input="a_out",
                on_success=None,
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"ba": "a_out", "bb": "b_out"},
                policy="require_all",
                merge="union",
            )
        )
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert any("merge" in (w.component or "") and "no outgoing edges" in w.message for w in result.warnings), [
            w.to_dict() for w in result.warnings
        ]

    def test_validate_w3_silent_when_the_coalesce_is_consumed(self) -> None:
        """The other half: a CONSUMED non-terminal coalesce is not accused."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="rows"))
        state = state.with_node(
            NodeSpec(
                id="fan",
                node_type="gate",
                plugin=None,
                input="rows",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=["ba", "bb"],
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(self._make_transform("a", "ba", "a_out"))
        state = state.with_node(self._make_transform("b", "bb", "b_out"))
        state = state.with_node(
            NodeSpec(
                id="merge",
                node_type="coalesce",
                plugin=None,
                input="a_out",
                on_success=None,
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"ba": "a_out", "bb": "b_out"},
                policy="require_all",
                merge="union",
            )
        )
        # The consumer names the coalesce BY ID — the shape the runtime resolves.
        state = state.with_node(self._make_transform("tidy", "merge", "main"))
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not any("merge" in (w.component or "") and "no outgoing edges" in w.message for w in result.warnings), [
            w.to_dict() for w in result.warnings
        ]

    def _dangling_aggregation_state(self, *, on_error: str) -> CompositionState:
        """source -> t1 -> aggregation, with NOTHING consuming the aggregation."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="rows"))
        state = state.with_node(self._make_transform("t1", "rows", "agg_in", on_error="main"))
        state = state.with_node(
            NodeSpec(
                id="agg",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg_in",
                on_success=None,
                on_error=on_error,
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                trigger={"count": 2},
            )
        )
        return state.with_output(self._make_output("main"))

    def test_validate_w3_warns_when_the_only_outbound_is_on_error_discard(self) -> None:
        """``on_error: "discard"`` is not a connection out — it is a hole.

        The rows go nowhere and the author is never told. Counting discard as
        an outbound made W3 contradict its own message, which promises the
        node's output is "not connected to any downstream node or sink";
        discard is neither. ``_runtime_connection_targets`` in the same module
        already excluded it, so W3 was the inconsistent sibling.

        An aggregation is the archetypal case because ``on_error`` is REQUIRED
        for one (``aggregation_missing_on_error``) — so before this it could
        always satisfy the limb with ``discard`` and dead-end in silence.

        This test is the ENTIRE evidence base for the change: all 715 saved
        composition states are unaffected (5 warned before, 5 after), because
        ``on_error`` is only ever ``discard`` or None across the corpus's 1033
        nodes and every discard node already satisfies the consumed limb
        first. The corpus cannot exercise this in either direction.
        """
        result = self._dangling_aggregation_state(on_error="discard").validate()
        assert any("agg" in (w.component or "") and "no outgoing edges" in w.message for w in result.warnings), [
            w.to_dict() for w in result.warnings
        ]

    def test_validate_w3_silent_when_on_error_names_a_real_sink(self) -> None:
        """The other direction: a real ``on_error`` target IS a connection out.

        Guards the tightening from becoming a blanket "ignore on_error". Same
        dangling aggregation, but its errors reach a sink — the rows leave in
        good order, so there is nothing to warn about.
        """
        result = self._dangling_aggregation_state(on_error="main").validate()
        assert not any("agg" in (w.component or "") and "no outgoing edges" in w.message for w in result.warnings), [
            w.to_dict() for w in result.warnings
        ]

    def test_validate_w3_silent_when_the_aggregation_is_consumed(self) -> None:
        """A consumed aggregation is quiet even with ``on_error: discard``.

        The consumed limb is checked first and independently, so tightening
        the ``on_error`` limb must not accuse a correctly-wired node.
        """
        state = self._dangling_aggregation_state(on_error="discard")
        state = state.with_node(self._make_transform("t2", "agg", "main", on_error="main"))
        result = state.validate()
        assert not any("agg" in (w.component or "") and "no outgoing edges" in w.message for w in result.warnings), [
            w.to_dict() for w in result.warnings
        ]

    def test_validate_source_on_success_mismatch_warns(self) -> None:
        """W2: Source on_success doesn't match any node input."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="nonexistent"))
        state = state.with_node(self._make_transform("t1", "other_input", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any("nonexistent" in w.message and "does not match" in w.message for w in result.warnings)

    def test_validate_format_extension_mismatch_warns(self) -> None:
        """W4: Sink plugin/filename extension mismatch."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "results"))
        output = OutputSpec(
            name="results",
            plugin="csv",
            options={"path": "/output/data.json"},
            on_write_failure="discard",
        )
        state = state.with_output(output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "results"))
        result = state.validate()
        assert result.is_valid
        assert any("extension suggests a different format" in w.message for w in result.warnings)

    def test_validate_transform_missing_required_options_warns(self) -> None:
        """W5: Transform that requires config has empty options."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        # value_transform requires 'operations' key
        incomplete_transform = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="value_transform",
            input="t1",
            on_success="main",
            on_error="discard",
            options={},  # Empty - should trigger warning
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(incomplete_transform)
        output = OutputSpec(name="main", plugin="csv", options={"path": "out.csv"}, on_write_failure="discard")
        state = state.with_output(output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid  # Still structurally valid
        assert any("value_transform" in w.message and "incomplete" in w.message for w in result.warnings)

    def test_validate_transform_empty_operations_warns(self) -> None:
        """W5: Transform has the required key but it's empty."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        # value_transform with empty operations list
        empty_ops_transform = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="value_transform",
            input="t1",
            on_success="main",
            on_error="discard",
            options={"operations": []},  # Empty list - should trigger warning
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(empty_ops_transform)
        output = OutputSpec(name="main", plugin="csv", options={"path": "out.csv"}, on_write_failure="discard")
        state = state.with_output(output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid
        assert any("value_transform" in w.message and "empty" in w.message for w in result.warnings)

    def test_validate_file_sink_missing_path_warns(self) -> None:
        """W6: File sink without path configured."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        # CSV sink with no path
        no_path_output = OutputSpec(name="main", plugin="csv", options={}, on_write_failure="discard")
        state = state.with_output(no_path_output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid  # Structurally valid but won't run
        assert any("no path configured" in w.message for w in result.warnings)

    def test_validate_file_sink_empty_path_warns(self) -> None:
        """W6: File sink with empty string path."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        # JSON sink with empty path
        empty_path_output = OutputSpec(name="main", plugin="json", options={"path": ""}, on_write_failure="discard")
        state = state.with_output(empty_path_output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid
        assert any("empty path" in w.message for w in result.warnings)

    def test_validate_non_file_sink_no_path_ok(self) -> None:
        """Non-file sinks (like database) don't require path."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        # Database sink - path is not a required option
        db_output = OutputSpec(
            name="main", plugin="database", options={"url": "sqlite:///:memory:", "table": "out"}, on_write_failure="discard"
        )
        state = state.with_output(db_output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        # Should NOT warn about missing path for non-file sinks
        assert not any("no path configured" in w.message for w in result.warnings)

    # --- W7: on_write_failure reference validation ---

    def test_validate_on_write_failure_nonexistent_output_is_error(self) -> None:
        """A dangling failsink raises RouteValidationError at runtime init, so
        Stage 1 reports it as an error (elspeth-eb4127fb49)."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        bad_output = OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="nonexistent")
        state = state.with_output(bad_output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any(e.error_code == "failsink_unknown_output" for e in result.errors)

    def test_validate_on_write_failure_self_reference_is_error(self) -> None:
        """A self-referencing failsink is a deterministic runtime rejection."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        self_ref = OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="main")
        state = state.with_output(self_ref)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any(e.error_code == "failsink_self_reference" for e in result.errors)

    def test_validate_on_write_failure_ineligible_plugin_is_error(self) -> None:
        """A non-file failsink target is a deterministic runtime rejection."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        main_out = OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="backup")
        backup_out = OutputSpec(
            name="backup", plugin="database", options={"url": "sqlite:///:memory:", "table": "t"}, on_write_failure="discard"
        )
        state = state.with_output(main_out)
        state = state.with_output(backup_out)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any(e.error_code == "failsink_ineligible_plugin" for e in result.errors)

    @pytest.mark.parametrize("plugin_name", sorted(FAILSINK_ELIGIBLE_SINK_PLUGINS))
    def test_validate_on_write_failure_shared_policy_plugins_are_valid(self, plugin_name: str) -> None:
        """Every centrally failsink-capable plugin is accepted by Stage 1."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        main_out = OutputSpec(name="main", plugin="database", options={"table": "rows"}, on_write_failure="errors")
        errors_out = OutputSpec(name="errors", plugin=plugin_name, options={"path": f"/errors.{plugin_name}"}, on_write_failure="discard")
        state = state.with_output(main_out)
        state = state.with_output(errors_out)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))

        result = state.validate()

        assert not any("on_write_failure" in e.message for e in (*result.errors, *result.warnings))

    def test_validate_on_write_failure_chain_is_error(self) -> None:
        """A chained failsink is a deterministic runtime rejection."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        main_out = OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="errors")
        errors_out = OutputSpec(name="errors", plugin="csv", options={"path": "/errors.csv"}, on_write_failure="overflow")
        overflow_out = OutputSpec(name="overflow", plugin="csv", options={"path": "/overflow.csv"}, on_write_failure="discard")
        state = state.with_output(main_out)
        state = state.with_output(errors_out)
        state = state.with_output(overflow_out)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any(e.error_code == "failsink_chain" for e in result.errors)

    def test_validate_on_write_failure_valid_no_warning(self) -> None:
        """W7: Valid failsink reference produces no warning."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        main_out = OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="errors")
        errors_out = OutputSpec(name="errors", plugin="csv", options={"path": "/errors.csv"}, on_write_failure="discard")
        state = state.with_output(main_out)
        state = state.with_output(errors_out)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        # No on_write_failure findings at any severity
        assert not any("on_write_failure" in e.message for e in (*result.errors, *result.warnings))

    def test_validate_on_write_failure_discard_no_warning(self) -> None:
        """W7: on_write_failure='discard' is always valid, no warning."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="discard"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert not any("on_write_failure" in w.message for w in result.warnings)

    # --- W8: on_validation_failure reference validation ---

    def test_validate_on_validation_failure_nonexistent_output_is_error(self) -> None:
        """A dangling quarantine destination raises RouteValidationError at
        runtime init, so Stage 1 reports it as an error."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1", on_validation_failure="nonexistent_sink"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="discard"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any(e.error_code == "quarantine_unknown_output" for e in result.errors)

    def test_validate_on_validation_failure_discard_no_warning(self) -> None:
        """W8: on_validation_failure='discard' is always valid, no warning."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1", on_validation_failure="discard"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="discard"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert not any("on_validation_failure" in w.message for w in result.warnings)

    def test_validate_on_validation_failure_valid_output_no_warning(self) -> None:
        """W8: on_validation_failure references a valid output, no warning."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1", on_validation_failure="quarantine"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(OutputSpec(name="main", plugin="csv", options={"path": "/out.csv"}, on_write_failure="discard"))
        state = state.with_output(
            OutputSpec(name="quarantine", plugin="csv", options={"path": "/quarantine.csv"}, on_write_failure="discard")
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert not any("on_validation_failure" in w.message for w in result.warnings)

    # --- Suggestion rules (S1-S3) ---

    def test_validate_discard_only_routing_suggests_retention(self) -> None:
        """S1: on_error='discard' does NOT count as error routing
        (elspeth-0aace271b4 I5) — the tool layer default-fills it, so counting
        it made the one advisory nudge toward retention permanently
        unreachable. A discard-only pipeline gets the retention suggestion."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main", on_error="discard"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid
        assert any("error routing" in s.message for s in result.suggestions)

    def test_validate_named_sink_routing_suppresses_s1(self) -> None:
        """S1 stays quiet when a node routes failures to a real output —
        retaining routing is exactly what the suggestion asks for."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main", on_error="errors"))
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("errors"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid
        assert not any("error routing" in s.message for s in result.suggestions)

    def test_validate_single_output_suggests(self) -> None:
        """S2: Pipeline with single EXTERNAL output gets a backup suggestion.

        Local file sinks (csv, json) don't trigger this because if the
        filesystem fails, a backup file will fail too. External sinks
        (database, azure_blob) benefit from a local recovery file.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        # Use external sink (database) to trigger S2 suggestion
        external_output = OutputSpec(
            name="main",
            plugin="database",
            options={"url": "sqlite:///:memory:", "table": "output"},
            on_write_failure="discard",
        )
        state = state.with_output(external_output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any("local file output" in s.message for s in result.suggestions)

    def test_validate_single_file_output_no_suggestion(self) -> None:
        """S2: Pipeline with single LOCAL file output gets no backup suggestion.

        Local file sinks don't benefit from a backup file - if the filesystem
        is failing, the backup will fail too.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))  # csv = local file sink
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        # Should NOT suggest backup for local file sinks
        assert not any("local file output" in s.message for s in result.suggestions)

    def test_validate_no_schema_config_suggests(self) -> None:
        """S3: Source without schema_config in options gets a suggestion."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert any("no explicit schema" in s.message for s in result.suggestions)

    def test_validate_schema_alias_suppresses_suggestion(self) -> None:
        """S3: Source with composer-facing ``schema`` alias must not trigger the suggestion.

        The composer/runtime boundary uses ``schema`` (user-facing) while
        plugin config parsing normalizes to ``schema_config``. The detection
        helper must accept either alias so correctly configured sources do
        not draw a false advisory through the LLM prompt.
        """
        source = SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"schema": {"mode": "observed"}},
            on_validation_failure="quarantine",
        )
        state = self._empty_state()
        state = state.with_source(source)
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert not any("no explicit schema" in s.message for s in result.suggestions)

    def test_validate_schema_config_alias_suppresses_suggestion(self) -> None:
        """S3: Source with internal ``schema_config`` alias also suppresses the suggestion.

        Plugin config parsing may land internal shapes in composer state
        (e.g. after serialization round-trips). Both alias names must be
        recognized so internal and external shapes agree.
        """
        source = SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"schema_config": {"mode": "observed"}},
            on_validation_failure="quarantine",
        )
        state = self._empty_state()
        state = state.with_source(source)
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert not any("no explicit schema" in s.message for s in result.suggestions)

    # --- Interaction tests ---

    def test_validate_warnings_dont_block(self) -> None:
        """Warnings don't affect is_valid."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("orphan"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "main"))
        result = state.validate()
        assert result.is_valid is True
        assert len(result.warnings) > 0

    def test_validate_errors_and_warnings_coexist(self) -> None:
        """A state with both errors and warnings populates both."""
        state = self._empty_state()
        # No source = error, orphan output = warning
        state = state.with_output(self._make_output("orphan"))
        result = state.validate()
        assert result.is_valid is False
        assert len(result.errors) > 0
        assert any("never receive data" in w.message for w in result.warnings)

    # --- Mandatory field enforcement (section 7 positive checks) ---

    def test_validate_transform_missing_plugin_errors(self) -> None:
        """Transform with plugin=None must fail validation."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        node = NodeSpec(
            id="t1",
            node_type="transform",
            plugin=None,
            input="t1",
            on_success="main",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("plugin" in e.message.lower() and "t1" in e.message for e in result.errors)

    def test_validate_transform_missing_on_success_errors(self) -> None:
        """Transform with on_success=None must fail validation."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        node = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="passthrough",
            input="t1",
            on_success=None,
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("on_success" in e.message and "t1" in e.message for e in result.errors)

    def test_validate_transform_blank_on_success_errors(self) -> None:
        """Transform with on_success='' must fail validation (engine rejects blank)."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        node = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="passthrough",
            input="t1",
            on_success="",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("on_success" in e.message and "t1" in e.message for e in result.errors)

    def test_validate_transform_blank_on_error_errors(self) -> None:
        """Transform with on_error='  ' must fail validation (engine rejects blank)."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        node = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="passthrough",
            input="t1",
            on_success="main",
            on_error="  ",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("on_error" in e.message and "t1" in e.message for e in result.errors)

    def test_validate_transform_missing_on_error_errors(self) -> None:
        """Transform with on_error=None must fail validation."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="t1"))
        node = NodeSpec(
            id="t1",
            node_type="transform",
            plugin="passthrough",
            input="t1",
            on_success="main",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("on_error" in e.message and "t1" in e.message for e in result.errors)

    def test_validate_aggregation_missing_trigger_is_end_of_source_only(self) -> None:
        """Aggregation with trigger=None means end-of-source-only flush."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg1"))
        node = NodeSpec(
            id="agg1",
            node_type="aggregation",
            plugin="batch_counter",
            input="agg1",
            on_success="main",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert result.is_valid

    def test_validate_aggregation_empty_trigger_is_end_of_source_only(self) -> None:
        """Aggregation with trigger={} means end-of-source-only flush."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg1"))
        node = NodeSpec(
            id="agg1",
            node_type="aggregation",
            plugin="batch_counter",
            input="agg1",
            on_success="main",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={},
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert result.is_valid

    def test_validate_aggregation_end_of_source_condition_errors(self) -> None:
        """end_of_source must not be accepted in the boolean condition slot."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg1"))
        node = NodeSpec(
            id="agg1",
            node_type="aggregation",
            plugin="batch_counter",
            input="agg1",
            on_success="main",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={"condition": "end_of_source"},
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("end_of_source" in e.message and "agg1" in e.message for e in result.errors)

    def test_validate_aggregation_invalid_output_mode_errors(self) -> None:
        """Aggregation with invalid output_mode must fail validation."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg1"))
        node = NodeSpec(
            id="agg1",
            node_type="aggregation",
            plugin="batch_counter",
            input="agg1",
            on_success="main",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={"count": 10},
            output_mode="invalid_mode",
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("output_mode" in e.message and "agg1" in e.message for e in result.errors)

    def test_validate_aggregation_with_trigger_timeout_passes(self) -> None:
        """Aggregation keeps its nested trigger timeout when top-level timeout is forbidden."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg1"))
        node = NodeSpec(
            id="agg1",
            node_type="aggregation",
            plugin="batch_counter",
            input="agg1",
            on_success="main",
            on_error="discard",
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={"count": 100, "timeout_seconds": 5.0},
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "agg1"))
        state = state.with_edge(self._make_edge("e2", "agg1", "main"))
        result = state.validate()
        assert result.is_valid, result.errors

    def test_validate_aggregation_missing_on_error_errors(self) -> None:
        """Aggregation with on_error=None must fail validation."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="agg1"))
        node = NodeSpec(
            id="agg1",
            node_type="aggregation",
            plugin="batch_counter",
            input="agg1",
            on_success="main",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(node)
        state = state.with_output(self._make_output("main"))
        result = state.validate()
        assert not result.is_valid
        assert any("on_error" in e.message and "agg1" in e.message for e in result.errors)

    def test_validate_clean_pipeline_no_warnings(self) -> None:
        """Well-formed pipeline with gates, error routing, schema, and
        multiple outputs has empty warnings and suggestions."""
        state = self._empty_state()
        source = SourceSpec(
            plugin="csv",
            on_success="t1",
            # `mode` is REQUIRED by `get_raw_schema_config`, the parser BOTH
            # surfaces share (core/dag/builder.py:161, core/dag/graph.py:199).
            # This fixture previously omitted it and still asserted is_valid —
            # a pipeline the runtime would reject at build time, called clean.
            # Nothing parsed it because Stage 1's schema parse was lazy; the
            # eager syntax sweep (elspeth-33738eedb6) now reaches it.
            options={"path": "/in.csv", "schema_config": {"mode": "observed", "fields": []}},
            on_validation_failure="quarantine",
        )
        state = state.with_source(source)
        state = state.with_node(self._make_transform("t1", "t1", "gate_in"))
        gate = NodeSpec(
            id="gate_1",
            node_type="gate",
            plugin=None,
            input="gate_in",
            on_success=None,
            on_error=None,
            options={},
            condition="row['score'] >= 0.5",
            routes={"true": "main", "false": "errors"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(gate)
        # Use properly configured outputs with paths (W6 semantic completeness)
        main_output = OutputSpec(name="main", plugin="csv", options={"path": "outputs/main.csv"}, on_write_failure="discard")
        errors_output = OutputSpec(name="errors", plugin="csv", options={"path": "outputs/errors.csv"}, on_write_failure="discard")
        quarantine_output = OutputSpec(
            name="quarantine", plugin="csv", options={"path": "outputs/quarantine.csv"}, on_write_failure="discard"
        )
        state = state.with_output(main_output)
        state = state.with_output(errors_output)
        state = state.with_output(quarantine_output)
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "gate_1"))
        state = state.with_edge(self._make_edge("e3", "gate_1", "main", edge_type="route_true"))
        state = state.with_edge(self._make_edge("e4", "gate_1", "errors", edge_type="route_false"))
        result = state.validate()
        assert result.is_valid, result.errors
        assert result.warnings == ()
        assert result.suggestions == ()

    def _gate_pipeline(self, *, condition: str, routes: dict[str, str]) -> CompositionState:
        """Minimal source -> gate -> sink pipeline for route-parity checks."""
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="g1"))
        state = state.with_node(
            NodeSpec(
                id="g1",
                node_type="gate",
                plugin=None,
                input="g1",
                on_success=None,
                on_error=None,
                options={},
                condition=condition,
                routes=routes,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e0", "source", "g1"))
        return state

    def test_gate_boolean_condition_custom_labels_invalid(self) -> None:
        """Boolean gate condition with non-true/false labels is rejected (parity with GateSettings).

        Regression for elspeth-08e17b9253: composer validate() previously
        green-lit a shape runtime GateSettings.validate_boolean_routes rejects.
        """
        result = self._gate_pipeline(condition="row['x'] > 0", routes={"high": "main", "low": "main"}).validate()
        assert result.is_valid is False
        assert any("boolean condition" in e.message and e.severity == "high" for e in result.errors), [e.message for e in result.errors]

    def test_gate_numeric_condition_invalid(self) -> None:
        """Provably-numeric gate condition can never be a route label; rejected for any labels."""
        result = self._gate_pipeline(condition="row['x'] + 1", routes={"a": "main"}).validate()
        assert result.is_valid is False
        assert any("numeric value" in e.message and e.severity == "high" for e in result.errors), [e.message for e in result.errors]

    def test_gate_boolean_condition_true_false_labels_valid(self) -> None:
        """Boolean gate condition with exactly {true,false} labels stays valid."""
        result = self._gate_pipeline(condition="row['x'] > 0", routes={"true": "main", "false": "main"}).validate()
        assert result.is_valid is True, [e.message for e in result.errors]

    def test_gate_string_route_condition_custom_labels_valid(self) -> None:
        """POSITIVE CONTROL: a string-returning condition with custom labels is NOT over-rejected."""
        result = self._gate_pipeline(
            condition='"high" if row["x"] > 0 else "low"',
            routes={"high": "main", "low": "main"},
        ).validate()
        assert result.is_valid is True, [e.message for e in result.errors]
