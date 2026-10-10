"""Composer preview parity for pass-through producer guarantees."""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.plugins.infrastructure.templates import TemplateError
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from tests.unit.web.composer._probe_lifecycle_helpers import DelegatingPluginManagerDouble


class TestPassThroughComposerParity:
    """ADR-007 composer parity tests for known-pass-through plugins.

    The composer preview must mirror runtime propagation for pass-through
    plugins. Two behaviours are pinned:

    - Probe succeeds → the producer_guarantees on downstream edges include
      predecessor fields (not just the transform's own declared output).
    - Probe fails for a *known* pass-through plugin → fail-closed with
      high-severity warning, producer_guarantees=(), Stage 1 rejects the
      pipeline (mirroring runtime rejection).
    - Probe fails for a *non*-pass-through plugin → v2 behaviour preserved
      (medium-severity warning, return raw_guaranteed).
    """

    def _empty_state(self) -> CompositionState:
        return CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _make_source(self, on_success: str, plugin: str = "csv", options: dict[str, Any] | None = None) -> SourceSpec:
        opts = dict(options or {})
        if plugin == "csv":
            opts = {"path": "/data/input.csv", **opts}
        return SourceSpec(
            plugin=plugin,
            on_success=on_success,
            options=opts,
            on_validation_failure="discard",
        )

    def _make_transform(
        self,
        id: str,
        input: str,
        on_success: str,
        plugin: str,
        options: dict[str, Any] | None = None,
        on_error: str = "discard",
    ) -> NodeSpec:
        return NodeSpec(
            id=id,
            node_type="transform",
            plugin=plugin,
            input=input,
            on_success=on_success,
            on_error=on_error,
            options=dict(options or {}),
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _make_edge(self, id: str, from_id: str, to_id: str) -> EdgeSpec:
        return EdgeSpec(id=id, from_node=from_id, to_node=to_id, edge_type="on_success", label=None)

    def test_preview_inherits_upstream_guarantees_when_pass_through_has_no_output_schema_config(self) -> None:
        """Successful passthrough probes still propagate inherited guarantees.

        The built-in passthrough plugin does not populate
        ``_output_schema_config``. Composer preview must therefore treat its
        own declaration set as empty and still apply ADR-007 propagation,
        mirroring ``ExecutionGraph.get_effective_guaranteed_fields()``.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="source",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": ["id: str", "body: str"],
                        "guaranteed_fields": ["id", "body"],
                    }
                },
            )
        )
        state = state.with_node(
            self._make_transform(
                "pt_node",
                "source",
                "main",
                plugin="passthrough",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "pt_node"))
        state = state.with_edge(self._make_edge("e2", "pt_node", "main"))

        result = state.validate()

        assert result.is_valid
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert set(sink_contract.producer_guarantees) == {"id", "body"}
        assert sink_contract.consumer_requires == ("body",)
        assert sink_contract.satisfied is True

    def test_batch_replicate_propagates_upstream_guarantees_to_downstream_consumer(self) -> None:
        """Replicated rows retain the fields guaranteed by their source."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="replicate_in",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "observed",
                        "guaranteed_fields": ["color_name", "hex"],
                    }
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="replicate",
                node_type="aggregation",
                plugin="batch_replicate",
                input="replicate_in",
                on_success="score_in",
                on_error="discard",
                options={
                    "schema": {"mode": "observed"},
                    "include_copy_index": True,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                trigger={"count": 10},
                output_mode="transform",
            )
        )
        state = state.with_node(
            self._make_transform(
                "score_variants",
                "score_in",
                "main",
                plugin="passthrough",
                options={
                    "schema": {"mode": "observed"},
                    "required_input_fields": ["color_name", "hex"],
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "outputs/main.csv", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "replicate"))
        state = state.with_edge(self._make_edge("e2", "replicate", "score_variants"))
        state = state.with_edge(self._make_edge("e3", "score_variants", "main"))

        result = state.validate()

        assert result.is_valid, result.errors
        consumer_contract = next(ec for ec in result.edge_contracts if ec.to_id == "score_variants")
        assert set(consumer_contract.producer_guarantees) == {"color_name", "hex", "copy_index"}
        assert consumer_contract.consumer_requires == ("color_name", "hex")
        assert consumer_contract.satisfied is True

    def test_preview_inherits_upstream_guarantees_through_fork_gate_into_pass_through(self) -> None:
        """Pass-through preview must follow fork branches back to their producer.

        The fixture is whole-roster (fork_to == the coalesce's branches): a
        former third direct-to-sink "overflow" branch was removed when spec §7
        rule 2 (ruling 23) made mixed closure a build error — it was vehicle,
        not subject.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": ["id: str", "body: str"],
                    }
                },
            )
        )
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
            self._make_transform(
                "pt_node",
                "path_a",
                "pt_out",
                plugin="passthrough",
                options={"schema": {"mode": "observed"}},
            )
        )
        # A coalesce needs at least two branches at the runtime
        # (``CoalesceSettings.branches`` min_length=2; Stage 1 mirrors it as
        # ``coalesce_branches_invalid``, elspeth-2ed41f0a4a) — the second
        # pass-through arm keeps the fixture runnable without changing what
        # the test pins (guarantee inheritance through the fork/pass-through).
        state = state.with_node(
            self._make_transform(
                "pt_node_b",
                "path_b",
                "pt_b_out",
                plugin="passthrough",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="rejoin",
                node_type="coalesce",
                plugin=None,
                input="pt_out",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"path_a": "pt_out", "path_b": "pt_b_out"},
                policy="require_all",
                merge="union",
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "fork_gate"))
        state = state.with_edge(EdgeSpec(id="e2", from_node="fork_gate", to_node="pt_node", edge_type="fork", label="path_a"))

        result = state.validate()

        # Engine-legal fork chains route branches through a coalesce (or a
        # sink-named branch), and the guarantee walk now RESOLVES a union
        # coalesce instead of deferring to the runtime validator
        # (elspeth-ae83a6b60c): its merged guarantee is the same one the DAG
        # builder stamps, so the contract row is computed rather than
        # fabricated and the skip warning would now be a false "not yet
        # checked" signal. This test therefore pins the end-to-end inheritance
        # it is named for — source guarantees, through the fork gate, through
        # the pass-through, through the merge, to the sink's requirement —
        # which the previous no-contract-row assertion could not see.
        assert result.is_valid, result.errors
        assert not any("coalesce" in w.message.lower() and "skipped" in w.message.lower() for w in result.warnings)
        contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert contract.from_id == "rejoin"
        assert set(contract.producer_guarantees) == {"id", "body"}
        assert contract.consumer_requires == ("body",)
        assert contract.satisfied is True

    def test_preview_fails_closed_when_known_pass_through_constructor_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Probe failure on a known pass-through plugin → Stage 1 rejects pipeline.

        Composer preview must surface high-severity warning and return an
        empty producer_guarantees set, matching the runtime rejection that
        would occur if the transform were constructed at DAG build time.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="pt_node",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": ["id: str", "body: str"], "guaranteed_fields": ["id", "body"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "pt_node",
                "source",
                "main",
                plugin="passthrough",  # Known pass-through plugin
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "pt_node"))
        state = state.with_edge(self._make_edge("e2", "pt_node", "main"))

        # Stub the plugin manager: get_transforms returns a minimal shim with
        # passthrough annotated True; create_transform raises for passthrough.
        class _StubPassThrough:
            name = "passthrough"
            passes_through_input = True
            forwards_input_fields = False
            removed_input_fields = frozenset()
            is_batch_aware = False  # Required by _known_batch_aware_transform_plugins()

        class _StubPluginManager(DelegatingPluginManagerDouble):
            def get_transforms(self) -> list[type]:
                return [_StubPassThrough]

            def create_transform(self, plugin_name: str, options: dict[str, Any]) -> object:
                raise TemplateError("intentional probe failure")

        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: _StubPluginManager(),
        )

        result = state.validate()

        # Stage 1 rejects because producer guarantees are empty and sink requires 'body'.
        assert not result.is_valid
        high_warnings = [w for w in result.warnings if w.severity == "high"]
        probe_high = [w for w in high_warnings if "computed contract probe" in w.message.lower() and "pass-through" in w.message.lower()]
        assert probe_high, f"Expected a high-severity probe warning mentioning pass-through; got warnings={result.warnings!r}"
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.producer_guarantees == ()
        assert sink_contract.satisfied is False
