"""An undeclared row-multiplying transform inside a row_union branch is a build error.

row_union correlates on (row_union_name, row_id), so a branch containing an
expanding transform emits several children sharing one row_id and cannot
satisfy the barrier. Coalesce — which correlates identically — surfaces that
as a loud duplicate-arrival invariant error.

**Reclassified under ruling 28 (spec §7 rule 5, Task 8, WS2 controller ruling
2026-08-23; see `docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md`
§7 rule 5).** This module used to prove the RUNTIME-adjudicated posture:
before elspeth-a5b86149d4, the mid-branch continuation dropped the barrier
binding, so exploded children walked straight through the union node and the
group split silently; the fix (`token_traversal.py:254-273`, "the barrier
binding must survive expansion") made the union node adjudicate the surplus
children and fail them closed instead, with an audit signal. Ruling 28
supersedes that whole posture for BOUND regions: "a shape change inside a
group must itself be a group" — an undeclared multi-row transform inside a
row_union-bound branch is now rejected at BUILD time
(`GraphValidationError`, `core/dag/bound_regions.py::validate_openers_bound_in_region`),
never reaching a run at all. `test_expanding_transform_in_a_branch_does_not_bypass_the_barrier`
below pins that build-time rejection now, not the runtime adjudication.

The runtime seam this module used to exercise (`token_traversal.py:254-273`,
"the barrier binding must survive expansion") is NOT dead code — it is still
reachable through any post-ruling-28 topology this build-time check does not
yet see, and through pre-ruling-28 persisted/resumed runs — but it has lost
its only END-TO-END witness here, because there is no legal way to build a
RUNNABLE pipeline with a multi-row transform inside a bound row_union region
today: `scopes:`/`collectors:` (the ruling-28-legal replacement) are
buildable but not yet RUNNABLE — the collector executor is WS4 (spec §7
plan Decision 5). A direct processor-level plumbing witness for that seam
(hand-built `TokenInfo`/work-item context, no DAG builder involved) lives in
`tests/unit/engine/test_processor.py::TestProcessRowMultiRowOutput::
test_row_union_binding_survives_expansion_on_a_branch`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError


def _build(tmp_path: Path) -> ExecutionGraph:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "out.jsonl"
    # Each row carries a two-element array, so the treatment branch explodes
    # one source row into two tokens that share its row_id.
    input_path.write_text('{"id": 1, "items": [10, 20]}\n{"id": 2, "items": [30, 40]}\n')
    settings = load_settings_from_yaml_string(
        f"""
sources:
  rows:
    plugin: json
    on_success: routed
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
gates:
  - name: variant_fork
    input: routed
    condition: "True"
    routes:
      'true': fork
      'false': output
    fork_to: [control_branch, treatment_branch]
transforms:
  - name: tag_control
    plugin: passthrough
    input: control_branch
    on_success: control_scored
    on_error: discard
    options:
      schema:
        mode: observed
  - name: explode_treatment
    plugin: json_explode
    input: treatment_branch
    on_success: treatment_scored
    on_error: discard
    options:
      array_field: items
      output_field: item
      schema:
        mode: observed
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
row_unions:
  - name: variant_union
    branches:
      control_branch: control_scored
      treatment_branch: treatment_scored
    on_success: union_out
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema:
        mode: observed
"""
    )
    bundle = instantiate_plugins_from_config(settings)
    return ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        queues=settings.queues,
        row_union_settings=list(settings.row_unions),
    )


def test_expanding_transform_in_a_branch_does_not_bypass_the_barrier(tmp_path: Path) -> None:
    """An undeclared multi-row transform inside a row_union branch fails to BUILD (ruling 28).

    `explode_treatment` (creates_tokens=True, plugin json_explode) sits inside
    `variant_union`'s `treatment_branch` with no `scopes:` entry declaring it
    an opener. Spec §7 rule 5: every token-creating node inside a bound
    region must be a declared scope opener whose closer is ALSO inside that
    region — this topology has neither, so it is rejected flat, before a run
    is ever attempted.
    """
    with pytest.raises(GraphValidationError, match=r"Multi-row transform .* inside bound region"):
        _build(tmp_path)
