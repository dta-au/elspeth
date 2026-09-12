"""Every argument knob the planner is shown is a field of the model the handler validates against, and vice versa.

SHIPPED comes from the live registry; MODEL comes from the manifest's ``argument_model`` or the
handler's validator call, by AST (``scripts/cicd/composer_wire_census.py``). Neither side is
hand-listed. Fences live in ``model_wire_fence.json`` with a reason each and are themselves gated.

SCOPE, MEASURED at 3ab58f336 (2026-09-08), stated so a green run is not over-read:

* 42 tools, all of them parametrised. 22 carry a model (12 from the manifest, 10 from a handler);
  20 do not, and 10 of those 20 ship knobs -- for those the MODEL wire is vacuously absent and
  EVERY knob is a gap, which is why the fence file has 20 rows over 10 tools rather than a
  handful. Skipping the no-model rows would have made the whole fence unenforced, so the plan's
  ``pytest.skip("no model on this wire")`` branch is deliberately NOT here.
* The gate pins membership only -- which names exist on each side. It says nothing about a
  field's TYPE, its requiredness, or its default. ``assert_upsert_node_schema_compatible`` and
  ``assert_set_pipeline_schema_compatible`` in the same module pin those, directionally, for the
  two tools that have them.
* READ (a knob no handler consumes), TAUGHT (prose naming a knob that does not ship) and the
  TypeScript decoder are other wires and other gates.

A fenced row retires itself: when a tool gains its model, its gap empties and
``test_every_fence_row_still_earns_its_place`` fails with "fenced but no longer a gap".
"""

import json
from pathlib import Path

import pytest
from scripts.cicd.composer_wire_census import _handler_models, census_model_wire

from elspeth.web.composer.tools.schema_contract import assert_model_wire_compatible

FENCE = json.loads((Path(__file__).parent / "model_wire_fence.json").read_text(encoding="utf-8"))["fenced"]
ROWS = census_model_wire()


@pytest.mark.parametrize("tool", sorted(ROWS))
def test_shipped_and_model_agree(tool: str) -> None:
    row = ROWS[tool]
    assert_model_wire_compatible(tool, shipped=row.shipped, model_fields=row.model_fields, fenced=frozenset(FENCE.get(tool, {})))


def test_every_fence_row_still_earns_its_place() -> None:
    for tool, keys in FENCE.items():
        row = ROWS[tool]
        for key, reason in keys.items():
            assert reason, f"{tool}.{key}: a fence without a reason"
            assert key in (row.shipped ^ row.model_fields), f"{tool}.{key}: fenced but no longer a gap — retire the fence"


def test_gate_is_not_vacuous() -> None:
    assert sum(1 for r in ROWS.values() if r.site != "none") >= 12


# --- probes: the census's two refusals, exercised against planted modules ------------------------
#
# Both refusals are unwitnessed by the live tree -- every validator call there passes a bare Name
# and every model_validate receiver is one -- so nothing in ``src`` would notice if either raise
# became a ``continue``, and the census would then report a validated tool as unvalidated while
# this gate passed over the gap. A refusal no test can fire is not a refusal (skill section 5,
# rule 7), which is why ``_handler_models`` takes its root as a parameter.

_PROBE_WELL_FORMED = """
def _execute_probe_tool(args, state, context):
    validated = _validate_mutation_arguments(_ProbeArgumentsModel, args, state)
    return validated
"""

_PROBE_NON_NAME_VALIDATOR_MODEL = """
def _execute_probe_tool(args, state, context):
    validated = _validate_mutation_arguments(_MODELS["probe"], args, state)
    return validated
"""

_PROBE_NON_NAME_MODEL_VALIDATE_RECEIVER = """
def _handle_probe_tool(args, state, context):
    return _models.ProbeArgumentsModel.model_validate(args)
"""


def _planted(tmp_path: Path, name: str, source: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "probe.py").write_text(source, encoding="utf-8")
    return root


def test_probe_root_reads_a_well_formed_handler(tmp_path: Path) -> None:
    """The positive control: without it, a walker that read nothing would pass both refusal probes."""
    found = _handler_models(_planted(tmp_path, "ok", _PROBE_WELL_FORMED))

    assert found == {"probe_tool": ("_ProbeArgumentsModel", "handler:probe._execute_probe_tool")}


def test_a_validator_called_without_a_bare_model_name_is_refused(tmp_path: Path) -> None:
    root = _planted(tmp_path, "validator", _PROBE_NON_NAME_VALIDATOR_MODEL)

    with pytest.raises(RuntimeError, match="called without a bare model Name"):
        _handler_models(root)


def test_model_validate_on_a_non_name_receiver_is_refused(tmp_path: Path) -> None:
    root = _planted(tmp_path, "receiver", _PROBE_NON_NAME_MODEL_VALIDATE_RECEIVER)

    with pytest.raises(RuntimeError, match="receiver that is not a bare model Name"):
        _handler_models(root)
