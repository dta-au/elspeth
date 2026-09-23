"""A batch transform reads, unguarded, only the fields it declares required.

The flush preflight (``batch_contract_validation.validate_batch_inputs``)
enforces ``schema_required_input_fields()`` on every buffered row: a row that
omits a declared field fails the batch as a routed contract violation before the
plugin runs (elspeth-5887fb7928 R1). That turns the plugin's own ``row[field]``
reads into safe accesses — but ONLY for fields in the declared set. A column the
plugin reads without declaring it is still a raw ``KeyError`` from inside
``process()`` that ends the run, which is exactly how ``batch_stats`` read
``value_field`` while declaring only ``group_by``.

So the obligation is a relation between two things the plugin owns, checked
from a live registry rather than a hand-written list:

* **reads ⊆ declared** — every field present on the plugin's own probe batch
  that it does NOT declare is removed, one at a time, and ``process()`` must not
  raise ``KeyError``. A plugin that reads such a field unguarded fails here, and
  the fix is its declaration, not a catch in the engine. A field read only when
  present (``batch_replicate.copies_field``, behind ``if field not in row``)
  passes, as it should.
* **declared ⇒ enforced** — every declared field, removed from one row, is
  rejected by the preflight with the row index and the field named, and with
  none of the row's values.

Anti-vacuity: the roster must be non-empty, must still contain the plugins the
defect was measured on, and every plugin's unmodified probe batch must run
without raising — a probe that already fails would make "no KeyError after
removal" meaningless.

KNOWN LIMIT: the probe batch comes from ``backward_invariant_probe_rows()``
under ``probe_config()``, one configuration. A read reachable only under another
configuration (``group_by`` set, say) is exercised only if that probe carries
it. Both ``group_by`` readers declare it on the same line that declares their
value column, so the limit does not currently hide a read.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from elspeth.contracts.errors import PluginContractViolation
from elspeth.contracts.node_state_context import AggregationBatchContext
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.engine.executors.batch_contract_validation import validate_batch_inputs
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context

# The probe row's own background column; every plugin forwards or ignores it.
_BASELINE_FIELD = "baseline"

# Carried on every row of the enforcement check; a reason must never contain it.
_SENTINEL_FIELD = "r1_sentinel_column"
_SENTINEL_VALUE = "SENTINEL-R1-value-5d0c2e"

# Plugins the missing-field abort was measured on end to end
# (understand-plugin-raises §6.3 x05; the batch_stats undeclared read).
_MEASURED_ANCHORS = frozenset({"batch_threshold_summary", "batch_stats"})

# Batch transforms whose every column is optional on the row, so they declare
# no required input. A NEW name here is a decision to review, not a skip.
_DECLARES_NO_REQUIRED_INPUT = frozenset({"batch_replicate"})


def _batch_transform_roster() -> list[Any]:
    manager = PluginManager()
    manager.register_builtin_plugins()
    roster = []
    for cls in manager.get_transforms():
        instance = cls(cls.probe_config())
        if instance.is_batch_aware:
            roster.append(instance)
    return roster


_ROSTER = _batch_transform_roster()


def _probe_batch(transform: Any) -> list[PipelineRow]:
    return list(transform.backward_invariant_probe_rows(make_pipeline_row({_BASELINE_FIELD: "kept"})))


def _flush_context(batch_size: int) -> PluginContext:
    """The context the engine hands a flush: ``aggregation_batch`` attached."""
    batch = AggregationBatchContext(
        trigger_type="count",
        batch_id="batch-r1",
        batch_size=batch_size,
        flush_index=1,
        rows_seen_total=batch_size,
        row_start=1,
        row_end=batch_size,
        is_end_of_source=False,
    )
    return dataclasses.replace(make_context(), aggregation_batch=batch)


def _without(rows: list[PipelineRow], field: str, *, only_index: int | None = None) -> list[PipelineRow]:
    rebuilt = []
    for index, row in enumerate(rows):
        payload = row.to_dict()
        if only_index is None or index == only_index:
            payload.pop(field, None)
        rebuilt.append(make_pipeline_row(payload))
    return rebuilt


def test_the_roster_is_live_and_still_holds_the_measured_plugins() -> None:
    names = {transform.name for transform in _ROSTER}
    assert names, "no batch-aware transform registered — the sweep would pass vacuously"
    assert names >= _MEASURED_ANCHORS, f"measured plugins left the roster: {sorted(_MEASURED_ANCHORS - names)}"


def test_only_the_named_plugins_declare_no_required_input() -> None:
    undeclared = {transform.name for transform in _ROSTER if not transform.schema_required_input_fields()}
    assert undeclared == _DECLARES_NO_REQUIRED_INPUT


@pytest.mark.parametrize("transform", _ROSTER, ids=lambda transform: transform.name)
def test_a_batch_transform_reads_no_undeclared_field_unguarded(transform: Any) -> None:
    rows = _probe_batch(transform)
    # Control: the unmodified probe runs, so a KeyError below is caused by the removal.
    transform.process(rows, _flush_context(len(rows)))

    present = {name for row in rows for name in row.to_dict()}
    undeclared = sorted(present - transform.schema_required_input_fields() - {_BASELINE_FIELD})
    reads: dict[str, str] = {}
    for field in undeclared:
        try:
            transform.process(_without(rows, field), _flush_context(len(rows)))
        except KeyError as exc:
            reads[field] = repr(exc)
    assert reads == {}, (
        f"{transform.name} reads field(s) it does not declare required, so the flush preflight cannot "
        f"protect them: {reads}. Fold them into its schema required_fields."
    )


@pytest.mark.parametrize("transform", [t for t in _ROSTER if t.schema_required_input_fields()], ids=lambda transform: transform.name)
def test_every_declared_field_is_enforced_by_the_flush_preflight(transform: Any) -> None:
    rows = [make_pipeline_row({**row.to_dict(), _SENTINEL_FIELD: _SENTINEL_VALUE}) for row in _probe_batch(transform)]
    validate_batch_inputs(transform, rows, node_kind="Aggregation")

    last = len(rows) - 1
    for field in sorted(transform.schema_required_input_fields()):
        with pytest.raises(PluginContractViolation) as excinfo:
            validate_batch_inputs(transform, _without(rows, field, only_index=last), node_kind="Aggregation")
        message = str(excinfo.value)
        assert f"buffered row {last}:" in message
        assert repr(field) in message
        assert _SENTINEL_VALUE not in message
        # The row's own keys are row-derived under an observed source: only the
        # configured field may be named, never what the row did carry.
        assert _SENTINEL_FIELD not in message
