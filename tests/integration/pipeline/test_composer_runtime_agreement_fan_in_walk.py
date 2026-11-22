"""Composer/runtime agreement at the producer walk-back's abstention arms.

``_check_schema_contracts`` (``web/composer/state.py``) resolves the producer a
consumer is checked against by walking back from the consumer's input. At a
fan-in (queue, row_union), a fork, or a routing loop the walk can abstain, and
an abstention skips the contract check for that edge. Each case here builds
one small pipeline, feeds the SAME YAML to both surfaces, and asserts that the
composer's verdict is the runtime build's verdict:

* the runtime build: settings -> plugin instances -> ``ExecutionGraph``, as
  ``elspeth validate`` builds it;
* the composer: ``composition_state_from_runtime_yaml`` -> ``validate()``.

Registry entry (see ``test_composer_runtime_agreement.py`` for the list):

* Shape 34 - a node consumer with EXPLICIT required fields directly behind a
  fan-in whose vote abstains (a queue or row_union with one observed arm),
  reached directly or through routing gates. The runtime's Phase-1 name check
  has no participation clause for node consumers: an abstaining producer
  guarantees nothing, so the build refuses the edge (``EdgeContractError``,
  "guarantees: (none - dynamic schema)"). The composer walk abstained with the
  "Contract check skipped" warning and reported no error (validate green /
  runtime red, measured 2026-11-22). Closed in the node loop of
  ``_check_schema_contracts`` for queue and row_union boundaries: the
  abstaining fan-in is reported with the ordinary ``schema_contract_violation``
  entry. The sink direction is unchanged on both surfaces (the runtime defers
  an abstaining upstream to per-row enforcement, and the composer stays
  valid), as is a consumer that only derives or implies its requirement.
  Pinned by ``TestFanInAbstentionRequiredFieldAgreement``.

The other classes cover walk-back arms that already agreed but had never been
executed by a test: the locked-input extras re-resolution at an abstaining
row_union, a fan-in arm whose schema cannot be parsed, and a routing loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph, GraphValidationError
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.web.composer.state import ValidationSummary
from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

_OBSERVED: dict[str, Any] = {"mode": "observed"}
_FIXED_ID: dict[str, Any] = {"mode": "fixed", "fields": ["id: int"]}


def _runtime_refusal(pipeline_yaml: str) -> str | None:
    """Build the runtime graph; return the refusal text, or None when it builds."""
    try:
        settings = load_settings_from_yaml_string(pipeline_yaml)
        plugins = instantiate_plugins_from_config(settings, preflight_mode=True)
        ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(settings.gates),
            coalesce_settings=list(settings.coalesce) or None,
            queues=settings.queues,
            row_union_settings=list(settings.row_unions) or None,
        )
    except (GraphValidationError, PluginConfigError) as exc:
        return str(exc)
    return None


def _composer(pipeline_yaml: str) -> ValidationSummary:
    return composition_state_from_runtime_yaml(pipeline_yaml).validate()


def _dump(document: dict[str, Any]) -> str:
    # The JSON round trip gives every use of a shared schema dict its own
    # object: a shared one dumps as a YAML alias, which the importer refuses.
    return yaml.safe_dump(json.loads(json.dumps(document)), sort_keys=False)


def _source(tmp_path: Path, name: str, on_success: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "plugin": "json",
        "on_success": on_success,
        "options": {
            "path": str(tmp_path / f"{name}.jsonl"),
            "format": "jsonl",
            "on_validation_failure": "discard",
            "schema": schema,
        },
    }


def _sink(tmp_path: Path, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "plugin": "json",
        "on_write_failure": "discard",
        "options": {"path": str(tmp_path / "out.jsonl"), "format": "jsonl", "schema": schema or _OBSERVED},
    }


def _passthrough(name: str, input_connection: str, on_success: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "plugin": "passthrough",
        "input": input_connection,
        "on_success": on_success,
        "on_error": "discard",
        "options": {"schema": schema},
    }


def _fork(input_connection: str = "routed") -> dict[str, Any]:
    return {
        "name": "variant_fork",
        "input": input_connection,
        "condition": "True",
        "routes": {"true": "fork", "false": "out"},
        "fork_to": ["a_branch", "b_branch"],
    }


def _queue_pipeline(
    tmp_path: Path,
    *,
    right_schema: dict[str, Any],
    consumer: dict[str, Any],
    via_gate: bool = False,
    sink_schema: dict[str, Any] | None = None,
) -> str:
    """Two sources -> queue ``inbound`` -> (optional routing gate) -> ``consumer`` -> sink.

    The left arm always declares ``id``; ``right_schema`` decides whether the
    queue's fan-in vote participates (typed) or abstains (observed).
    """
    document: dict[str, Any] = {
        "sources": {
            "left": _source(tmp_path, "left", "inbound", _FIXED_ID),
            "right": _source(tmp_path, "right", "inbound", right_schema),
        },
        "queues": {"inbound": {}},
        "transforms": [{**consumer, "input": "work" if via_gate else "inbound"}],
        "sinks": {"out": _sink(tmp_path, sink_schema)},
    }
    if via_gate:
        document["gates"] = [{"name": "route", "input": "inbound", "condition": "True", "routes": {"true": "work", "false": "out"}}]
    return _dump(document)


def _row_union_pipeline(
    tmp_path: Path,
    *,
    source_schema: dict[str, Any],
    arm_a: dict[str, Any],
    consumer_schema: dict[str, Any],
    transforms_first: tuple[dict[str, Any], ...] = (),
    arm_b_schema: dict[str, Any] | None = None,
) -> str:
    """source -> fork -> (arm_a | passthrough arm_b) -> row_union -> ``tail`` -> sink."""
    return _dump(
        {
            "sources": {"rows": _source(tmp_path, "rows", "routed", source_schema)},
            "gates": [_fork()],
            "transforms": [
                *transforms_first,
                _passthrough("tail", "unioned", "out", consumer_schema),
                {"name": "arm_a", "input": "a_branch", "on_success": "a_done", "on_error": "discard", **arm_a},
                _passthrough("arm_b", "b_branch", "b_done", arm_b_schema or _OBSERVED),
            ],
            "row_unions": [{"name": "variant_union", "branches": {"a_branch": "a_done", "b_branch": "b_done"}, "on_success": "unioned"}],
            "sinks": {"out": _sink(tmp_path)},
        }
    )


_ADDS_BONUS: dict[str, Any] = {
    "plugin": "value_transform",
    "options": {"schema": {"mode": "observed"}, "operations": [{"target": "bonus", "expression": "1"}]},
}
_PASSTHROUGH_ARM: dict[str, Any] = {"plugin": "passthrough", "options": {"schema": {"mode": "observed"}}}


def _requires_id(name: str = "tail") -> dict[str, Any]:
    return _passthrough(name, "inbound", "out", {"mode": "observed", "required_fields": ["id"]})


class TestFanInAbstentionRequiredFieldAgreement:
    """Shape 34 - explicit required fields behind an abstaining fan-in: both surfaces refuse.

    Bug verification protocol: with the ``fan_in_abstentions`` block removed
    from the node loop of ``_check_schema_contracts`` (the ``if
    consumer_required and fan_in_abstentions:`` arm), the three refusal cases
    fail on the composer side (``is_valid`` is True, no
    ``schema_contract_violation``) while the runtime half still refuses; the
    controls stay green. Red-on-base measured on an exported 06c9cd408 tree
    (lane logs ``item1-red-on-base``).
    """

    @pytest.mark.parametrize("via_gate", [False, True], ids=["direct", "through-routing-gate"])
    def test_both_refuse_required_field_behind_abstaining_queue(self, via_gate: bool, tmp_path: Path) -> None:
        pipeline_yaml = _queue_pipeline(tmp_path, right_schema=_OBSERVED, consumer=_requires_id(), via_gate=via_gate)

        runtime_refusal = _runtime_refusal(pipeline_yaml)
        assert runtime_refusal is not None
        assert "Missing fields: ['id']" in runtime_refusal

        composer = _composer(pipeline_yaml)
        assert not composer.is_valid
        [entry] = [error for error in composer.errors if error.error_code == "schema_contract_violation"]
        assert entry.component == "node:tail"
        assert entry.contract is not None
        assert entry.contract.producer == "inbound"
        assert entry.contract.consumer == "tail"
        assert entry.contract.missing_fields == ("id",)

    def test_both_refuse_required_field_behind_abstaining_row_union(self, tmp_path: Path) -> None:
        """The observed source makes the pass-through arm abstain, so the union's vote abstains."""
        pipeline_yaml = _row_union_pipeline(
            tmp_path,
            source_schema=_OBSERVED,
            arm_a=_ADDS_BONUS,
            consumer_schema={"mode": "observed", "required_fields": ["bonus"]},
        )

        runtime_refusal = _runtime_refusal(pipeline_yaml)
        assert runtime_refusal is not None
        assert "Missing fields: ['bonus']" in runtime_refusal

        composer = _composer(pipeline_yaml)
        assert not composer.is_valid
        [entry] = [error for error in composer.errors if error.error_code == "schema_contract_violation"]
        assert entry.component == "node:tail"
        assert entry.contract is not None
        assert entry.contract.producer == "variant_union"
        assert entry.contract.missing_fields == ("bonus",)

    def test_both_accept_required_field_every_queue_arm_guarantees(self, tmp_path: Path) -> None:
        """Control: a participating queue's intersection satisfies the requirement."""
        pipeline_yaml = _queue_pipeline(tmp_path, right_schema=_FIXED_ID, consumer=_requires_id())

        assert _runtime_refusal(pipeline_yaml) is None
        composer = _composer(pipeline_yaml)
        assert composer.is_valid, composer.errors

    def test_both_defer_sink_required_field_behind_abstaining_queue(self, tmp_path: Path) -> None:
        """Control: the SINK direction defers an abstaining upstream to per-row enforcement on both surfaces."""
        pipeline_yaml = _queue_pipeline(
            tmp_path,
            right_schema=_OBSERVED,
            consumer=_passthrough("tail", "inbound", "out", _OBSERVED),
            sink_schema={"mode": "observed", "required_fields": ["id"]},
        )

        assert _runtime_refusal(pipeline_yaml) is None
        composer = _composer(pipeline_yaml)
        assert composer.is_valid, composer.errors

    @pytest.mark.parametrize(
        "consumer",
        [
            pytest.param(
                _passthrough("tail", "inbound", "out", {"mode": "flexible", "fields": ["id: int"]}), id="implied-by-flexible-schema"
            ),
            pytest.param(
                {
                    "name": "tail",
                    "plugin": "type_coerce",
                    "input": "inbound",
                    "on_success": "out",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}, "conversions": [{"field": "id", "to": "int"}]},
                },
                id="derived-from-an-option",
            ),
        ],
    )
    def test_both_accept_unwritten_requirement_behind_abstaining_queue(self, consumer: dict[str, Any], tmp_path: Path) -> None:
        """Control: only a requirement the author wrote by hand fails closed against an abstaining producer.

        A requirement implied by a flexible schema, or derived from an ordinary
        option, is enforced per row at run time; both surfaces build.
        """
        pipeline_yaml = _queue_pipeline(tmp_path, right_schema=_OBSERVED, consumer=consumer)

        assert _runtime_refusal(pipeline_yaml) is None
        composer = _composer(pipeline_yaml)
        assert composer.is_valid, composer.errors


class TestRowUnionAbstentionLockedInputAgreement:
    """Locked-input extras at a row_union whose presence vote abstained (elspeth-9d13900064).

    The presence direction abstains when one arm is observed, but a field one
    arm definitely adds still arrives on that arm's rows, so a fixed-mode
    consumer that does not accept it is refused. The runtime build makes the
    same call on the row_union -> consumer edge.

    Bug verification protocol: replacing ``if consumer_locked_input is not
    None:`` with ``if False:`` in the abstention arm of the node loop makes
    ``test_both_refuse_an_arm_only_field_at_a_locked_consumer`` fail on the
    composer side (valid, no ``locked_input_extras``); the control stays green.
    """

    def test_both_refuse_an_arm_only_field_at_a_locked_consumer(self, tmp_path: Path) -> None:
        pipeline_yaml = _row_union_pipeline(tmp_path, source_schema=_OBSERVED, arm_a=_ADDS_BONUS, consumer_schema=_FIXED_ID)

        runtime_refusal = _runtime_refusal(pipeline_yaml)
        assert runtime_refusal is not None
        assert "Extra fields rejected by consumer input contract: ['bonus']" in runtime_refusal

        composer = _composer(pipeline_yaml)
        assert not composer.is_valid
        [entry] = [error for error in composer.errors if error.error_code == "locked_input_extras"]
        assert entry.component == "node:tail"
        assert entry.contract is not None
        assert entry.contract.producer == "variant_union"
        assert entry.contract.extra_fields == ("bonus",)

    def test_both_accept_a_locked_consumer_that_declares_the_arm_field(self, tmp_path: Path) -> None:
        pipeline_yaml = _row_union_pipeline(
            tmp_path,
            source_schema=_OBSERVED,
            arm_a=_ADDS_BONUS,
            consumer_schema={"mode": "fixed", "fields": ["id: int", "bonus: int?"]},
        )

        assert _runtime_refusal(pipeline_yaml) is None
        composer = _composer(pipeline_yaml)
        assert composer.is_valid, composer.errors


class TestUnparseableFanInArmAgreement:
    """A fan-in arm whose schema does not parse is reported, never raised.

    The consumer is listed BEFORE the arm, so the walk-back's fan-in vote is
    the first parse of the arm's schema block. The runtime refuses the same
    document when it constructs the arm's plugin.

    Bug verification protocol: replacing the ``except ValueError:`` arm of the
    queue (or row_union) vote in ``_walk_producer_entry_to_real_producer`` with
    a bare ``raise`` makes the matching case fail with the ``ValueError``
    escaping ``validate()``.
    """

    _UNPARSEABLE: ClassVar[dict[str, Any]] = {"mode": "fixed", "fields": ["id: nonsense_type"]}

    def test_queue_arm_listed_after_the_consumer(self, tmp_path: Path) -> None:
        pipeline_yaml = _dump(
            {
                "sources": {
                    "left": _source(tmp_path, "left", "inbound", _FIXED_ID),
                    "right": _source(tmp_path, "right", "staged", _FIXED_ID),
                },
                "queues": {"inbound": {}},
                "transforms": [_requires_id(), _passthrough("late_arm", "staged", "inbound", self._UNPARSEABLE)],
                "sinks": {"out": _sink(tmp_path)},
            }
        )

        runtime_refusal = _runtime_refusal(pipeline_yaml)
        assert runtime_refusal is not None
        assert "nonsense_type" in runtime_refusal

        composer = _composer(pipeline_yaml)
        assert not composer.is_valid
        assert any(error.error_code == "contract_config_invalid" and error.component == "node:late_arm" for error in composer.errors)

    def test_row_union_arm_listed_after_the_consumer(self, tmp_path: Path) -> None:
        pipeline_yaml = _row_union_pipeline(
            tmp_path,
            source_schema=_FIXED_ID,
            arm_a=_PASSTHROUGH_ARM,
            consumer_schema={"mode": "observed", "required_fields": ["id"]},
            arm_b_schema=self._UNPARSEABLE,
        )

        runtime_refusal = _runtime_refusal(pipeline_yaml)
        assert runtime_refusal is not None
        assert "nonsense_type" in runtime_refusal

        composer = _composer(pipeline_yaml)
        assert not composer.is_valid
        assert any(error.error_code == "contract_config_invalid" and error.component == "node:arm_b" for error in composer.errors)


class TestRoutingLoopWalkBackAgreement:
    """A consumer fed from a gate cycle: both surfaces refuse the cycle and neither hangs.

    The walk-back from ``tail`` follows gate inputs; its visited-connection
    guard is the only thing that ends it when two gates feed each other.

    Bug verification protocol: replacing the guard's ``return None`` with a
    ``raise AssertionError`` makes the test fail with that error escaping
    ``validate()``, which shows the guard is the arm that ends this walk.
    """

    def test_both_refuse_a_gate_cycle_feeding_a_consumer(self, tmp_path: Path) -> None:
        pipeline_yaml = _dump(
            {
                "sources": {"rows": _source(tmp_path, "rows", "routed", _FIXED_ID)},
                "gates": [
                    {"name": "first", "input": "loop_b", "condition": "True", "routes": {"true": "loop_a", "false": "out"}},
                    {"name": "second", "input": "loop_a", "condition": "True", "routes": {"true": "loop_b", "false": "work"}},
                ],
                "transforms": [
                    _passthrough("tail", "work", "out", {"mode": "observed", "required_fields": ["id"]}),
                    _passthrough("head", "routed", "out", _OBSERVED),
                ],
                "sinks": {"out": _sink(tmp_path)},
            }
        )

        runtime_refusal = _runtime_refusal(pipeline_yaml)
        assert runtime_refusal is not None
        assert "cycle" in runtime_refusal

        composer = _composer(pipeline_yaml)
        assert not composer.is_valid
        assert any(error.error_code == "pipeline_cycle" for error in composer.errors)


class TestDeclaredTypeBehindForwarderUnionMergeAgreement:
    """A type declared two hops up a branch reaches the union-coalesce conflict check on both surfaces.

    Branch ``path_a`` declares ``price`` on a pass-through and then forwards
    it through an observed pass-through; branch ``path_b`` carries the fixed
    source's ``price: int``. Both branch outputs are observed, so only the
    type walk can see the conflict. A declared ``any`` is a known type at a
    union merge and conflicts with ``int``; a declared ``int`` does not.

    Bug verification protocol: making the union-merge stamp arm of
    ``_resolved_producer_field_type`` skip a declared ``any`` (``if field_name
    in declarations and declarations[field_name].field_type != "any":``) makes
    the ``any`` case fail on the composer side (valid) while the build refuses.
    """

    @staticmethod
    def _yaml(tmp_path: Path, declared_type: str) -> str:
        return _dump(
            {
                "sources": {
                    "rows": {
                        "plugin": "csv",
                        "on_success": "routed",
                        "options": {
                            "path": str(tmp_path / "rows.csv"),
                            "on_validation_failure": "discard",
                            "schema": {"mode": "fixed", "fields": ["id: int", "price: int"]},
                        },
                    }
                },
                "gates": [_fork()],
                "transforms": [
                    _passthrough("declares", "a_branch", "a_mid", {"mode": "flexible", "fields": [f"price: {declared_type}"]}),
                    _passthrough("forwards", "a_mid", "a_done", _OBSERVED),
                    _passthrough("carries", "b_branch", "b_done", _OBSERVED),
                ],
                "coalesce": [
                    {
                        "name": "merge_results",
                        "branches": {"a_branch": "a_done", "b_branch": "b_done"},
                        "policy": "require_all",
                        "merge": "union",
                        "on_success": "out",
                    }
                ],
                "sinks": {"out": _sink(tmp_path)},
            }
        )

    @pytest.mark.parametrize("declared_type", ["any", "str"])
    def test_both_refuse_a_conflicting_declared_type(self, declared_type: str, tmp_path: Path) -> None:
        pipeline_yaml = self._yaml(tmp_path, declared_type)

        runtime_refusal = _runtime_refusal(pipeline_yaml)
        assert runtime_refusal is not None
        assert "'price'" in runtime_refusal

        composer = _composer(pipeline_yaml)
        assert not composer.is_valid
        [entry] = [error for error in composer.errors if error.error_code == "coalesce_union_type_incompatible"]
        assert entry.coalesce_union_type is not None
        assert entry.coalesce_union_type.field == "price"
        assert {entry.coalesce_union_type.type_a, entry.coalesce_union_type.type_b} == {declared_type, "int"}

    def test_both_accept_the_same_declared_type(self, tmp_path: Path) -> None:
        pipeline_yaml = self._yaml(tmp_path, "int")

        assert _runtime_refusal(pipeline_yaml) is None
        composer = _composer(pipeline_yaml)
        assert composer.is_valid, composer.errors
