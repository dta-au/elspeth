# Composer Path-Quality Battery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `evals/composer-battery/` — a tracked, offline-scored battery that fires a fixed operator-voice corpus at the local Web Composer, scores each run against a pre-registered floor with a mechanical deviation taxonomy, and covers the planner surface with a paired probe and a tripwire.

**Architecture:** Three tracked library modules under `evals/lib/` (topology comparator, scenario contract, capture parser + scorer) are pure functions over captured JSON and are unit-tested hermetically. A standalone `requests`-based driver under `evals/composer-battery/` captures runs into `runs/<round>/<case>/<n>/` and never scores; `report.py` and `planner_probe.py` read captures only. The oracle per case is a validated `set_pipeline` `canonical_arguments` payload (parity-fixture format); floors are in tool-bearing `llm_call_audit` currency.

**Tech Stack:** Python 3.12, `requests` (already used by `scripts/acceptance_battery.py`), pytest (`tests/unit/evals/…`), the ELSPETH web composer modules for offline validation (`SetPipelineArgumentsModel`, `PolicyCatalogView.for_trained_operator`, `_execute_set_pipeline`, `classify_pipeline_mutation_intent`).

**Spec:** `docs/superpowers/specs/2026-08-13-composer-battery-design.md` (rev 4, `ab043791b`). Review + panels: `docs/superpowers/specs/2026-08-16-composer-battery-design-review.md`.

## Global Constraints

- Substrate is `https://elspeth.foundryside.dev` only; **login only, never register**; hard-fail on absent `access_token`; never cache an error body.
- Compose+validate only — the driver never calls `/execute`.
- Currency = `llm_call_audit` rows with `status == "success"` and non-null `tools_spec_hash` (Decision 8). Advisor bucket = `model_requested == identity.binding.advisor_model`.
- Corpus prompts must NOT classify `EXPLICIT_MUTATION` (`classify_pipeline_mutation_intent`), enforced by a unit test over `corpus.md`.
- Every rate is printed with `n` and exclusions beside it; surfaces are never pooled; `--compare` refuses on binding-identity mismatch and prints recorded deltas.
- Client timeout 620 s; capture the 422 `detail` body; paginate `GET /messages` on `offset` with an identical flag set until a page shorter than 500 (a full page always triggers the next fetch).
- Data path floor: `source.inline_blob` (Decision 10); `create_blob`→bind is `data_setup_detour`.
- All new Python is tracked; `runs/` is git-ignored; `.gitignore` re-include mirrors composer-parity's credential re-exclusions.
- Full `pytest tests/` before merge; commit by pathspec only (shared checkout).

---

## File structure

| Path | Responsibility |
| --- | --- |
| `evals/lib/battery_topology.py` | `Topology` model; `topology_from_pipeline`/`topology_from_arguments`; isomorphism `topologies_match` with `option_assertions` |
| `evals/lib/battery_scenario.py` | `scenario.json` contract: load/validate, canonical-payload validation (parity pattern), expected-topology derivation and round-trip |
| `evals/lib/battery_capture.py` | Parse a captured run dir into typed rows: `LlmCall`, `PlannerAttempt`, `ToolRow`, `AssistantTurn`, tool outcomes (durable-pair projection) |
| `evals/lib/battery_score.py` | Per-run scoring: buckets, floor delta, deviation classes, instrument_error sub-kinds, `surface_observed`; writes `score.json` |
| `evals/lib/battery_report.py` | Aggregation: pooled Σ/Σ, per-case, per-repeat, ledger, compare with binding/recorded identity, MDE |
| `evals/composer-battery/drive_battery.py` | Live driver: login, session, PATCH-title-before-POST, 422/progress capture, reviews loop, `validate?state_id`, paginated capture, `meta.json`, ledger/resume, order canary→tripwire→round-robin, abort rules |
| `evals/composer-battery/report.py` | CLI over `battery_report` → `report.json` + `report.md` |
| `evals/composer-battery/planner_probe.py` | §7 paired probe arms + information-class floor scoring; tripwire pass/fail |
| `evals/composer-battery/corpus.md` | Verbatim prompts (first unlabelled fence per case heading), `corpus_version` |
| `evals/composer-battery/scenarios/<case>/scenario.json` | Per-case contract |
| `evals/composer-battery/README.md` | Runbook: calibrate, freeze, fire, report |
| `tests/unit/evals/composer_battery/` | Unit tests for every module above |

---

### Task 1: Topology comparator (`evals/lib/battery_topology.py`)

**Files:**
- Create: `evals/lib/battery_topology.py`
- Create: `tests/unit/evals/composer_battery/__init__.py` (empty)
- Create: `tests/unit/evals/composer_battery/test_battery_topology.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) TNode(kind: str, plugin: str | None, extras: tuple[tuple[str, str], ...])` — `kind ∈ {"source","transform","gate","aggregation","coalesce","row_union","queue","output"}`; `extras` is a sorted tuple of (`policy`,`merge`,`fork_count`,`route_count`) as strings when present.
  - `@dataclass(frozen=True) Topology(nodes: tuple[TNode, ...], edges: tuple[tuple[int, int, str], ...])` — edges are `(from_index, to_index, edge_type)` with `edge_type ∈ {"on_success","on_error","route","fork"}`.
  - `topology_from_pipeline(doc: Mapping[str, Any]) -> Topology` — accepts EITHER a `set_pipeline` args dict (`source`, `nodes`, `outputs[].sink_name`) OR a `CompositionState.to_dict()` (`sources`, `nodes`, `outputs[].name`). Connections are derived from names, never from the `edges` list.
  - `OptionAssertion = tuple[str, str, Any]` — (`node_kind_or_plugin`, `option_key`, `expected_value`).
  - `topologies_match(expected: Topology, observed: Topology, *, option_values: Mapping[str, Mapping[str, Any]] | None = None, option_assertions: Sequence[OptionAssertion] = ()) -> MatchResult` where `@dataclass MatchResult(ok: bool, reason: str | None)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/evals/composer_battery/test_battery_topology.py
"""Isomorphism oracle — labels/ids ignored, structure/policy/merge/cardinality exact."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from evals.lib.battery_topology import topologies_match, topology_from_pipeline

FIXTURE = Path(__file__).resolve().parents[4] / "evals/composer-parity/fixtures/fork_coalesce.json"


def _fork_args() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())["canonical_arguments"]


def _as_state(args: dict[str, Any]) -> dict[str, Any]:
    """Project a set_pipeline args dict into the CompositionState.to_dict() shape."""
    src = dict(args["source"])
    return {
        "version": 1,
        "sources": {"source": src},
        "nodes": copy.deepcopy(args["nodes"]),
        "edges": [],
        "outputs": [{"name": o["sink_name"], "plugin": o["plugin"], "options": o.get("options", {})} for o in args["outputs"]],
    }


def test_args_and_state_projections_agree() -> None:
    args = _fork_args()
    assert topology_from_pipeline(args) == topology_from_pipeline(_as_state(args))


def test_renamed_ids_and_fork_labels_still_match() -> None:
    args = _fork_args()
    renamed = copy.deepcopy(args)
    for n in renamed["nodes"]:
        n["id"] = "x_" + n["id"]
    fork = next(n for n in renamed["nodes"] if n["node_type"] == "gate")
    fork["fork_to"] = ["b1", "b2"]
    coalesce = next(n for n in renamed["nodes"] if n["node_type"] == "coalesce")
    coalesce["branches"] = {"b1": "b1", "b2": "b2"}
    coalesce["input"] = "b1"
    result = topologies_match(topology_from_pipeline(args), topology_from_pipeline(renamed))
    assert result.ok, result.reason


def test_wrong_coalesce_merge_fails() -> None:
    args = _fork_args()
    bad = copy.deepcopy(args)
    next(n for n in bad["nodes"] if n["node_type"] == "coalesce")["merge"] = "first_wins"
    result = topologies_match(topology_from_pipeline(args), topology_from_pipeline(bad))
    assert not result.ok and "merge" in (result.reason or "")


def test_extra_passthrough_node_fails() -> None:
    args = _fork_args()
    bad = copy.deepcopy(args)
    fin = next(n for n in bad["nodes"] if n["id"] == "finalize")
    fin["on_success"] = "extra_in"
    bad["nodes"].append({"id": "extra", "node_type": "transform", "plugin": "passthrough", "input": "extra_in", "on_success": "merged", "on_error": "discard"})
    result = topologies_match(topology_from_pipeline(args), topology_from_pipeline(bad))
    assert not result.ok and "node" in (result.reason or "")


def test_sink_plugin_swap_fails() -> None:
    args = _fork_args()
    bad = copy.deepcopy(args)
    bad["outputs"][0]["plugin"] = "jsonl"
    assert not topologies_match(topology_from_pipeline(args), topology_from_pipeline(bad)).ok


def test_swapped_route_wiring_between_two_gates_fails() -> None:
    """Two same-typed gates; swapping which one feeds the sink is a different graph."""
    base = {
        "source": {"plugin": "csv", "on_success": "in", "options": {"path": "r.csv"}},
        "nodes": [
            {"id": "g1", "node_type": "gate", "input": "in", "condition": "True", "routes": {"true": "mid", "false": "rej"}},
            {"id": "g2", "node_type": "gate", "input": "mid", "condition": "True", "routes": {"true": "ok", "false": "rej"}},
        ],
        "outputs": [{"sink_name": "ok", "plugin": "json"}, {"sink_name": "rej", "plugin": "csv"}],
    }
    swapped = copy.deepcopy(base)
    swapped["nodes"][0]["routes"] = {"true": "ok", "false": "rej"}
    swapped["nodes"][1]["input"] = "ok"
    swapped["nodes"][1]["routes"] = {"true": "mid", "false": "rej"}
    swapped["outputs"][0]["sink_name"] = "mid"
    # g1 now feeds the sink directly and g2 hangs off it — different wiring, same multiset
    result = topologies_match(topology_from_pipeline(base), topology_from_pipeline(swapped))
    assert not result.ok


def test_option_assertion_pins_threshold_only_when_listed() -> None:
    args = {
        "source": {"plugin": "csv", "on_success": "in", "options": {"path": "r.csv"}},
        "nodes": [{"id": "g", "node_type": "gate", "input": "in", "condition": "row['amount'] > 100", "routes": {"true": "hi", "false": "lo"}, "options": {"threshold": 100}}],
        "outputs": [{"sink_name": "hi", "plugin": "json"}, {"sink_name": "lo", "plugin": "json"}],
    }
    other = copy.deepcopy(args)
    other["nodes"][0]["options"]["threshold"] = 250
    exp, obs = topology_from_pipeline(args), topology_from_pipeline(other)
    assert topologies_match(exp, obs).ok  # option values ignored by default
    observed_options = {"gate": {"threshold": 250}}
    result = topologies_match(exp, obs, option_values=observed_options, option_assertions=[("gate", "threshold", 100)])
    assert not result.ok and "threshold" in (result.reason or "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_battery_topology.py -q -n 0`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.lib.battery_topology'`

- [ ] **Step 3: Implement the comparator**

```python
# evals/lib/battery_topology.py
"""Topology oracle for the composer battery (spec §2, Decision 9).

Both a ``set_pipeline`` args dict and a ``CompositionState.to_dict()`` are
projected into the same ``Topology``: typed nodes plus edges derived from
connection NAMES (``source.on_success`` → ``node.input``; ``routes`` /
``fork_to`` / ``on_success`` → the node whose ``input`` matches, or the
output whose name matches). Node ids and fork labels are author-chosen
strings and are ignored; plugin, node type, edge type, coalesce ``policy``
and ``merge``, and node cardinality are exact. Option values are ignored
unless an ``option_assertion`` names them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import permutations
from typing import Any

OptionAssertion = tuple[str, str, Any]

_EXTRA_KEYS: tuple[str, ...] = ("policy", "merge")


@dataclass(frozen=True)
class TNode:
    kind: str
    plugin: str | None
    extras: tuple[tuple[str, str], ...]

    def signature(self) -> tuple[str, str | None, tuple[tuple[str, str], ...]]:
        return (self.kind, self.plugin, self.extras)


@dataclass(frozen=True)
class Topology:
    nodes: tuple[TNode, ...]
    edges: tuple[tuple[int, int, str], ...]


@dataclass(frozen=True)
class MatchResult:
    ok: bool
    reason: str | None = None


def _node_extras(node: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    extras: list[tuple[str, str]] = []
    for key in _EXTRA_KEYS:
        if node.get(key) is not None:
            extras.append((key, str(node[key])))
    if node.get("fork_to"):
        extras.append(("fork_count", str(len(node["fork_to"]))))
    if isinstance(node.get("routes"), Mapping):
        extras.append(("route_count", str(len(node["routes"]))))
    return tuple(sorted(extras))


def topology_from_pipeline(doc: Mapping[str, Any]) -> Topology:
    """Project args (``source``/``sink_name``) or state (``sources``/``name``) into a Topology."""
    if "sources" in doc and isinstance(doc["sources"], Mapping) and doc["sources"]:
        # CompositionState shape; the canonical single source is the first declared.
        source_spec = next(iter(doc["sources"].values()))
    else:
        source_spec = doc.get("source") or {}
    nodes_in = list(doc.get("nodes") or [])
    outputs_in = list(doc.get("outputs") or [])

    tnodes: list[TNode] = [TNode("source", source_spec.get("plugin"), ())]
    for n in nodes_in:
        tnodes.append(TNode(str(n.get("node_type")), n.get("plugin"), _node_extras(n)))
    output_index_by_name: dict[str, int] = {}
    for o in outputs_in:
        name = o.get("sink_name") if "sink_name" in o else o.get("name")
        output_index_by_name[str(name)] = len(tnodes)
        tnodes.append(TNode("output", o.get("plugin"), ()))

    # connection name → consuming node index (a node's ``input``); coalesce
    # branches are also consumers of the branch connection names.
    consumers: dict[str, list[int]] = {}
    for i, n in enumerate(nodes_in, start=1):
        if n.get("input") is not None:
            consumers.setdefault(str(n["input"]), []).append(i)
        branches = n.get("branches")
        if isinstance(branches, Mapping):
            for conn in branches.values():
                consumers.setdefault(str(conn), []).append(i)
        elif isinstance(branches, (list, tuple)):
            for conn in branches:
                consumers.setdefault(str(conn), []).append(i)

    def targets(conn: Any) -> list[int]:
        if conn is None or conn == "discard":
            return []
        c = str(conn)
        found = list(dict.fromkeys(consumers.get(c, [])))
        if c in output_index_by_name:
            found.append(output_index_by_name[c])
        return found

    edges: list[tuple[int, int, str]] = []
    for t in targets(source_spec.get("on_success")):
        edges.append((0, t, "on_success"))
    for i, n in enumerate(nodes_in, start=1):
        for t in targets(n.get("on_success")):
            edges.append((i, t, "on_success"))
        for t in targets(n.get("on_error")):
            edges.append((i, t, "on_error"))
        routes = n.get("routes")
        if isinstance(routes, Mapping):
            for conn in routes.values():
                if conn == "fork":
                    continue
                for t in targets(conn):
                    edges.append((i, t, "route"))
        for conn in n.get("fork_to") or []:
            for t in targets(conn):
                edges.append((i, t, "fork"))
    return Topology(tuple(tnodes), tuple(sorted(set(edges))))


def _edge_multiset(t: Topology, mapping: Sequence[int]) -> tuple[tuple[int, int, str], ...]:
    return tuple(sorted((mapping[a], mapping[b], k) for a, b, k in t.edges))


def topologies_match(
    expected: Topology,
    observed: Topology,
    *,
    option_values: Mapping[str, Mapping[str, Any]] | None = None,
    option_assertions: Sequence[OptionAssertion] = (),
) -> MatchResult:
    """Exact isomorphism on typed nodes + typed edges; option assertions checked afterwards."""
    if len(expected.nodes) != len(observed.nodes):
        return MatchResult(False, f"node count {len(observed.nodes)} != expected {len(expected.nodes)}")
    exp_sigs = sorted(n.signature() for n in expected.nodes)
    obs_sigs = sorted(n.signature() for n in observed.nodes)
    if exp_sigs != obs_sigs:
        for e, o in zip(exp_sigs, obs_sigs, strict=True):
            if e != o:
                return MatchResult(False, f"node signature mismatch: expected {e}, observed {o}")
    # candidate correspondences: observed index j may play expected index i iff signatures equal
    n = len(expected.nodes)
    candidates = [[j for j in range(n) if observed.nodes[j].signature() == expected.nodes[i].signature()] for i in range(n)]
    exp_edges = tuple(sorted(expected.edges))

    def search(i: int, used: set[int], mapping: list[int]) -> bool:
        if i == n:
            return _edge_multiset(observed, _invert(mapping)) == exp_edges
        for j in candidates[i]:
            if j in used:
                continue
            mapping.append(j)
            used.add(j)
            if search(i + 1, used, mapping):
                return True
            used.discard(j)
            mapping.pop()
        return False

    def _invert(mapping: Sequence[int]) -> list[int]:
        inv = [0] * n
        for i, j in enumerate(mapping):
            inv[j] = i
        return inv

    if not search(0, set(), []):
        return MatchResult(False, "edge structure differs (no node correspondence reproduces the expected edges)")
    for kind_or_plugin, key, expected_value in option_assertions:
        actual = (option_values or {}).get(kind_or_plugin, {}).get(key, _MISSING)
        if actual is _MISSING or actual != expected_value:
            return MatchResult(False, f"option assertion failed: {kind_or_plugin}.{key} expected {expected_value!r}, observed {actual!r}")
    return MatchResult(True, None)


_MISSING = object()


def observed_option_values(doc: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Collect ``options`` per node kind and per plugin name for option assertions."""
    out: dict[str, dict[str, Any]] = {}
    for n in doc.get("nodes") or []:
        opts = dict(n.get("options") or {})
        for key in (n.get("node_type"), n.get("plugin")):
            if key:
                out.setdefault(str(key), {}).update(opts)
    return out


__all__ = ["MatchResult", "OptionAssertion", "TNode", "Topology", "observed_option_values", "topologies_match", "topology_from_pipeline"]
```

Note: `permutations` is imported but unused — remove it before committing (ruff will flag it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_battery_topology.py -q -n 0`
Expected: 7 passed. If `test_swapped_route_wiring_between_two_gates_fails` passes trivially, print both topologies and confirm they differ in edges, not node multiset.

- [ ] **Step 5: Commit**

```bash
git add -- evals/lib/battery_topology.py tests/unit/evals/composer_battery/__init__.py tests/unit/evals/composer_battery/test_battery_topology.py
git commit -m "feat(evals): topology isomorphism oracle for the composer battery" -- evals/lib/battery_topology.py tests/unit/evals/composer_battery/__init__.py tests/unit/evals/composer_battery/test_battery_topology.py
```

---

### Task 2: Scenario contract (`evals/lib/battery_scenario.py`)

**Files:**
- Create: `evals/lib/battery_scenario.py`
- Create: `tests/unit/evals/composer_battery/test_battery_scenario.py`
- Create: `evals/composer-battery/scenarios/fork_coalesce/scenario.json` (first real scenario, reusing the parity payload)

**Interfaces:**
- Consumes: `topology_from_pipeline`, `Topology`, `TNode` from Task 1; `SetPipelineArgumentsModel` (`elspeth.web.composer.redaction`), `PolicyCatalogView.for_trained_operator`, `create_catalog_service`, `PluginAvailabilitySnapshot` (as in `tests/unit/evals/composer_parity/test_fixtures.py`); `extract_structural_target` (`evals.lib.scenario_from_example`).
- Produces:
  - `@dataclass Floor(tool_bearing_calls: int, components: dict[str, int], repairs: int, backtracks: int, derivation: list[str], pre_calibration: int, post_calibration: int | None)`
  - `@dataclass Scenario(case: str, example: str | None, variant: str | None, corpus_version: int, surface_required: str, classifier_decision: str, canonical_arguments: dict, expected_topology: dict, option_assertions: list[list], floor: Floor, red_criteria: dict, green_criteria: dict, path: Path)`
  - `load_scenario(path: Path) -> Scenario` (raises `ValueError` listing missing keys)
  - `topology_to_dict(t: Topology) -> dict` / `topology_from_dict(d: Mapping) -> Topology` (stored `expected_topology` form: `{"nodes":[{"kind","plugin","extras":{...}}], "edges":[[i,j,type]]}`)
  - `validate_canonical_arguments(args: Mapping) -> None` (raises on schema or plugin-availability failure)
  - `extractor_cross_check(scenario: Scenario, repo_root: Path) -> list[str]` (returns violations; `fork`→`gate` normalised)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/evals/composer_battery/test_battery_scenario.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.lib.battery_scenario import (
    extractor_cross_check,
    load_scenario,
    topology_from_dict,
    topology_to_dict,
    validate_canonical_arguments,
)
from evals.lib.battery_topology import topology_from_pipeline

REPO = Path(__file__).resolve().parents[4]
SCENARIOS = REPO / "evals/composer-battery/scenarios"
PARITY = REPO / "evals/composer-parity/fixtures"


def test_fork_coalesce_scenario_loads_and_round_trips() -> None:
    sc = load_scenario(SCENARIOS / "fork_coalesce/scenario.json")
    assert sc.case == "fork_coalesce"
    assert sc.floor.tool_bearing_calls == 2
    derived = topology_from_pipeline(sc.canonical_arguments)
    assert topology_to_dict(derived) == sc.expected_topology
    assert topology_from_dict(sc.expected_topology) == derived


def test_canonical_arguments_validate_like_parity_fixtures() -> None:
    args = json.loads((PARITY / "fork_coalesce.json").read_text())["canonical_arguments"]
    validate_canonical_arguments(args)  # must not raise


def test_canonical_arguments_reject_unknown_plugin() -> None:
    args = json.loads((PARITY / "linear_transform.json").read_text())["canonical_arguments"]
    args["nodes"][0]["plugin"] = "definitely_not_a_plugin"
    with pytest.raises(ValueError, match="plugin"):
        validate_canonical_arguments(args)


def test_missing_key_is_a_loud_error(tmp_path: Path) -> None:
    doc = json.loads((SCENARIOS / "fork_coalesce/scenario.json").read_text())
    del doc["floor"]
    p = tmp_path / "scenario.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="floor"):
        load_scenario(p)


def test_extractor_cross_check_passes_for_fork_coalesce() -> None:
    sc = load_scenario(SCENARIOS / "fork_coalesce/scenario.json")
    assert extractor_cross_check(sc, REPO) == []


def test_extractor_cross_check_flags_missing_kind() -> None:
    sc = load_scenario(SCENARIOS / "fork_coalesce/scenario.json")
    stripped = json.loads(json.dumps(sc.expected_topology))
    stripped["nodes"] = [n for n in stripped["nodes"] if n["kind"] != "coalesce"]
    sc.expected_topology = stripped
    assert any("coalesce" in v for v in extractor_cross_check(sc, REPO))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_battery_scenario.py -q -n 0`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the first scenario file**

Create `evals/composer-battery/scenarios/fork_coalesce/scenario.json` by copying `canonical_arguments` from `evals/composer-parity/fixtures/fork_coalesce.json` and filling the rest:

```json
{
  "case": "fork_coalesce",
  "example": "examples/fork_coalesce",
  "variant": null,
  "corpus_version": 0,
  "surface": { "required": "compose_loop", "classifier_decision": "AMBIGUOUS" },
  "canonical_arguments": "<<paste the parity fixture's canonical_arguments object here, verbatim>>",
  "expected_topology": {},
  "option_assertions": [],
  "floor": {
    "tool_bearing_calls": 2,
    "components": { "discovery": 1, "dependent_listing": 0, "mutation": 1 },
    "repairs": 0, "backtracks": 0,
    "derivation": [
      "discovery: schemas for csv, passthrough, coalesce, json — one batched call",
      "data: invented rows travel as source.inline_blob inside the mutation — 0 extra",
      "dependent_listing: none",
      "mutation: single set_pipeline authors the whole graph"
    ],
    "pre_calibration": 2, "post_calibration": null
  },
  "red_criteria": {
    "passivity_phrases": "rgr_default",
    "build_failure_sentinels": ["I cannot mark this pipeline complete", "runtime preflight failed"]
  },
  "green_criteria": {
    "topology_matches_expected": true,
    "option_assertions_hold": true,
    "must_discover_schema_before_first_mutation": true,
    "is_valid": true
  }
}
```

Then generate `expected_topology` once with the helper (after Step 4's module exists) and paste it back:

```bash
source .venv/bin/activate && python - <<'EOF'
import json
from pathlib import Path
from evals.lib.battery_scenario import topology_to_dict
from evals.lib.battery_topology import topology_from_pipeline
p = Path("evals/composer-battery/scenarios/fork_coalesce/scenario.json")
doc = json.loads(p.read_text())
doc["expected_topology"] = topology_to_dict(topology_from_pipeline(doc["canonical_arguments"]))
p.write_text(json.dumps(doc, indent=2) + "\n")
print(json.dumps(doc["expected_topology"], indent=1))
EOF
```

`corpus_version: 0` means "pre-freeze"; the freeze task bumps every scenario to 1.

- [ ] **Step 4: Implement the module**

```python
# evals/lib/battery_scenario.py
"""scenario.json contract for the composer battery (spec §2)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.lib.battery_topology import TNode, Topology, topology_from_pipeline
from evals.lib.scenario_from_example import extract_structural_target

_REQUIRED_KEYS = ("case", "example", "variant", "corpus_version", "surface", "canonical_arguments", "expected_topology", "option_assertions", "floor", "red_criteria", "green_criteria")
_FLOOR_KEYS = ("tool_bearing_calls", "components", "repairs", "backtracks", "derivation", "pre_calibration", "post_calibration")


@dataclass
class Floor:
    tool_bearing_calls: int
    components: dict[str, int]
    repairs: int
    backtracks: int
    derivation: list[str]
    pre_calibration: int
    post_calibration: int | None


@dataclass
class Scenario:
    case: str
    example: str | None
    variant: str | None
    corpus_version: int
    surface_required: str
    classifier_decision: str
    canonical_arguments: dict[str, Any]
    expected_topology: dict[str, Any]
    option_assertions: list[list[Any]]
    floor: Floor
    red_criteria: dict[str, Any]
    green_criteria: dict[str, Any]
    path: Path = field(default_factory=Path)


def load_scenario(path: Path) -> Scenario:
    doc = json.loads(Path(path).read_text())
    missing = [k for k in _REQUIRED_KEYS if k not in doc]
    if missing:
        raise ValueError(f"{path}: scenario missing keys {missing}")
    floor_doc = doc["floor"]
    fmissing = [k for k in _FLOOR_KEYS if k not in floor_doc]
    if fmissing:
        raise ValueError(f"{path}: floor missing keys {fmissing}")
    surface = doc["surface"]
    return Scenario(
        case=str(doc["case"]),
        example=doc["example"],
        variant=doc["variant"],
        corpus_version=int(doc["corpus_version"]),
        surface_required=str(surface["required"]),
        classifier_decision=str(surface["classifier_decision"]),
        canonical_arguments=dict(doc["canonical_arguments"]),
        expected_topology=dict(doc["expected_topology"]),
        option_assertions=[list(a) for a in doc["option_assertions"]],
        floor=Floor(**{k: floor_doc[k] for k in _FLOOR_KEYS}),
        red_criteria=dict(doc["red_criteria"]),
        green_criteria=dict(doc["green_criteria"]),
        path=Path(path),
    )


def topology_to_dict(t: Topology) -> dict[str, Any]:
    return {
        "nodes": [{"kind": n.kind, "plugin": n.plugin, "extras": dict(n.extras)} for n in t.nodes],
        "edges": [[a, b, k] for a, b, k in t.edges],
    }


def topology_from_dict(d: Mapping[str, Any]) -> Topology:
    nodes = tuple(TNode(str(n["kind"]), n.get("plugin"), tuple(sorted((str(k), str(v)) for k, v in dict(n.get("extras") or {}).items()))) for n in d["nodes"])
    edges = tuple(sorted((int(a), int(b), str(k)) for a, b, k in d["edges"]))
    return Topology(nodes, edges)


def validate_canonical_arguments(args: Mapping[str, Any]) -> None:
    """Same two checks the parity fixtures get: schema + trained-operator plugin availability."""
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.composer.redaction import SetPipelineArgumentsModel
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

    from elspeth.web.plugin_policy.models import PluginId

    model = SetPipelineArgumentsModel.model_validate(dict(args))
    if not model.nodes or not model.outputs:
        raise ValueError("canonical_arguments must declare at least one node and one output")
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    view = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    refs: set[PluginId] = set()
    if model.source is not None:
        refs.add(PluginId("source", model.source.plugin))
    if model.sources is not None:
        for named in model.sources.values():
            refs.add(PluginId("source", named.plugin))
    for node in model.nodes:
        if node.plugin is not None:
            refs.add(PluginId("transform", node.plugin))
    for output in model.outputs:
        refs.add(PluginId("sink", output.plugin))
    for plugin_id in sorted(refs, key=str):
        reason = view.unavailable_reason(plugin_id)
        if reason is not None:
            raise ValueError(f"plugin {plugin_id} unavailable to a trained operator ({reason})")


def extractor_cross_check(scenario: Scenario, repo_root: Path) -> list[str]:
    """The example extractor's node-kind multiset (fork→gate) must be ⊆ expected_topology's."""
    if not scenario.example:
        return []
    target = extract_structural_target(repo_root / scenario.example, scenario.variant)
    extracted: list[str] = ["gate" for _ in target["gates"]]
    extracted += ["coalesce" for _ in target["coalesce_nodes"]]
    extracted += ["aggregation" for _ in target["aggregations"]]
    extracted += ["transform" for _ in target["transforms"]]
    expected_kinds = [n["kind"] for n in scenario.expected_topology.get("nodes", [])]
    violations: list[str] = []
    for kind in set(extracted):
        if extracted.count(kind) > expected_kinds.count(kind):
            violations.append(f"extractor sees {extracted.count(kind)} × {kind}, expected_topology has {expected_kinds.count(kind)}")
    return violations


__all__ = ["Floor", "Scenario", "extractor_cross_check", "load_scenario", "topology_from_dict", "topology_to_dict", "validate_canonical_arguments"]
```

This mirrors `tests/unit/evals/composer_parity/test_fixtures.py:78-111` (`_referenced_plugins` + `PolicyCatalogView.unavailable_reason`) exactly — do not invent a different lookup.

- [ ] **Step 5: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_battery_scenario.py -q -n 0`
Expected: 6 passed. (`test_extractor_cross_check_passes_for_fork_coalesce` depends on the `examples/fork_coalesce/settings.yaml` extractor: 2 gates → 2 gate kinds expected; the parity payload has ONE gate. If it fails on gate count, the fork_coalesce scenario's `canonical_arguments` must mirror the example's second gate — read `examples/fork_coalesce/settings.yaml` and add the second gate + its route to the payload rather than weakening the check.)

- [ ] **Step 6: Commit**

```bash
git add -- evals/lib/battery_scenario.py tests/unit/evals/composer_battery/test_battery_scenario.py evals/composer-battery/scenarios/fork_coalesce/scenario.json
git commit -m "feat(evals): battery scenario contract with canonical-payload oracle" -- evals/lib/battery_scenario.py tests/unit/evals/composer_battery/test_battery_scenario.py evals/composer-battery/scenarios/fork_coalesce/scenario.json
```

If `git status` shows the scenario file ignored, do the `.gitignore` re-include from Task 3 Step 1 first.

---

### Task 3: Package scaffolding, corpus, and the classifier gate

**Files:**
- Modify: `.gitignore` (add the `evals/composer-battery/` re-include next to the composer-parity block, ~lines 53–79)
- Create: `evals/composer-battery/README.md`
- Create: `evals/composer-battery/corpus.md`
- Create: `evals/composer-battery/scenarios/<case>/scenario.json` for the remaining 18 cases (`canary`, `transform_pipeline`, `boolean_routing`, `explicit_routing`, `threshold_gate`, `deep_routing`, `error_routing`, `row_union_ab_experiment`, `batch_aggregation`, `statistical_batch_plugins`, `json_explode`, `deaggregation`, `template_lookups`, `multi_query_assessment`, `openrouter_sentiment`, `llm_source`, `schema_contracts_demo`, `report_assemble`)
- Create: `evals/lib/battery_corpus.py`
- Create: `tests/unit/evals/composer_battery/test_corpus.py`

**Interfaces:**
- Consumes: `load_scenario`, `validate_canonical_arguments`, `topology_to_dict`, `topology_from_dict`, `extractor_cross_check` (Task 2); `topology_from_pipeline` (Task 1); `classify_pipeline_mutation_intent`, `PipelineMutationIntentDecision` (`elspeth.web.composer.no_tool_policy`).
- Produces:
  - `evals/lib/battery_corpus.py`: `@dataclass CorpusCase(name: str, prompt: str)`; `parse_corpus(md: str) -> tuple[int, list[CorpusCase]]` (returns `corpus_version` and cases; **first unlabelled fenced block under each `## <case>` heading is the prompt, byte-verbatim**); `CORPUS_PATH`, `SCENARIOS_DIR` constants; `load_corpus() -> tuple[int, dict[str, CorpusCase]]`.

- [ ] **Step 1: `.gitignore` re-include**

Open `.gitignore`, find the composer-parity re-include block (search `composer-parity`). Immediately after it add, mirroring its shape:

```gitignore
# composer-battery: tracked instrument (spec docs/superpowers/specs/2026-08-13-composer-battery-design.md)
!/evals/composer-battery/
!/evals/composer-battery/**
/evals/composer-battery/runs/
/evals/composer-battery/**/jwt.txt
/evals/composer-battery/**/login.json
/evals/composer-battery/**/*.access_token
/evals/composer-battery/**/*.pem
/evals/composer-battery/**/credentials.json
```

Verify: `git check-ignore -v evals/composer-battery/corpus.md` prints nothing (not ignored) and `git check-ignore -v evals/composer-battery/runs/x` prints the runs rule.

- [ ] **Step 2: Write the failing corpus tests**

```python
# tests/unit/evals/composer_battery/test_corpus.py
"""Corpus integrity: every case has a prompt, a scenario, a valid payload, a stored topology
that round-trips, an extractor cross-check that holds, and a prompt that stays on the compose loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from elspeth.web.composer.no_tool_policy import PipelineMutationIntentDecision, classify_pipeline_mutation_intent
from evals.lib.battery_corpus import SCENARIOS_DIR, load_corpus, parse_corpus
from evals.lib.battery_scenario import extractor_cross_check, load_scenario, topology_from_dict, topology_to_dict, validate_canonical_arguments
from evals.lib.battery_topology import topology_from_pipeline

REPO = Path(__file__).resolve().parents[4]
EXPECTED_CASES = {
    "canary", "transform_pipeline", "boolean_routing", "explicit_routing", "threshold_gate", "deep_routing",
    "error_routing", "fork_coalesce", "row_union_ab_experiment", "batch_aggregation", "statistical_batch_plugins",
    "json_explode", "deaggregation", "template_lookups", "multi_query_assessment", "openrouter_sentiment",
    "llm_source", "schema_contracts_demo", "report_assemble",
}


def test_parse_corpus_takes_first_unlabelled_fence_verbatim() -> None:
    md = "corpus_version: 3\n\n## alpha\n\nIntro.\n\n```text\nlabelled\n```\n\n```\n  Verbatim  bytes\nline two\n```\n\n## beta\n\n```\nb\n```\n"
    version, cases = parse_corpus(md)
    assert version == 3
    assert [c.name for c in cases] == ["alpha", "beta"]
    assert cases[0].prompt == "  Verbatim  bytes\nline two"


def test_corpus_and_scenarios_cover_the_same_cases() -> None:
    _, cases = load_corpus()
    scenario_dirs = {p.name for p in SCENARIOS_DIR.iterdir() if (p / "scenario.json").exists()}
    assert set(cases) == EXPECTED_CASES == scenario_dirs


@pytest.mark.parametrize("case", sorted(EXPECTED_CASES))
def test_scenario_is_sound(case: str) -> None:
    sc = load_scenario(SCENARIOS_DIR / case / "scenario.json")
    validate_canonical_arguments(sc.canonical_arguments)
    derived = topology_from_pipeline(sc.canonical_arguments)
    assert topology_to_dict(derived) == sc.expected_topology, f"{case}: expected_topology is stale — regenerate"
    assert topology_from_dict(sc.expected_topology) == derived
    assert extractor_cross_check(sc, REPO) == []
    assert sc.floor.tool_bearing_calls == sum(sc.floor.components.values())
    assert sc.floor.repairs == 0 and sc.floor.backtracks == 0


@pytest.mark.parametrize("case", sorted(EXPECTED_CASES))
def test_prompt_stays_on_the_compose_loop(case: str) -> None:
    """Decision 7 surface gate, run in CI: a classifier-grammar edit must fail here, not silently re-route."""
    _, cases = load_corpus()
    sc = load_scenario(SCENARIOS_DIR / case / "scenario.json")
    decision = classify_pipeline_mutation_intent(cases[case].prompt)
    assert decision is not PipelineMutationIntentDecision.EXPLICIT_MUTATION, f"{case}: prompt would route to the planner"
    assert decision.name == sc.classifier_decision, f"{case}: recorded classifier_decision {sc.classifier_decision} != {decision.name}"


def test_corpus_version_matches_every_scenario() -> None:
    version, cases = load_corpus()
    for case in cases:
        assert load_scenario(SCENARIOS_DIR / case / "scenario.json").corpus_version == version


@pytest.fixture(scope="module")
def tool_context():
    """Real builtin catalog + trained-operator policy view — the same wiring tests/unit/web/composer/test_recipes.py uses."""
    from elspeth.plugins.infrastructure.manager import PluginManager
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.catalog.service import CatalogServiceImpl
    from elspeth.web.composer.tools._common import ToolContext
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

    pm = PluginManager()
    pm.register_builtin_plugins()
    catalog = CatalogServiceImpl(pm)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot), plugin_snapshot=snapshot)


@pytest.mark.parametrize("case", sorted(EXPECTED_CASES))
def test_canonical_payload_commits_to_the_expected_topology(case: str, tool_context) -> None:
    """Spec §2 second oracle test: the topology of the state the server's own args→state path builds
    for the canonical payload ≡ the stored expected_topology (hermetic for ``path`` sources)."""
    from elspeth.web.composer.state import CompositionState, PipelineMetadata
    from elspeth.web.composer.tools.sessions import build_set_pipeline_candidate

    sc = load_scenario(SCENARIOS_DIR / case / "scenario.json")
    if "inline_blob" in json.dumps(sc.canonical_arguments):
        pytest.skip("inline_blob payloads need a session engine; the args-projection test above covers them")
    empty = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    candidate = build_set_pipeline_candidate(sc.canonical_arguments, empty, tool_context)
    assert candidate.result.success, candidate.result.to_dict()
    committed = topology_from_pipeline(candidate.result.updated_state.to_dict())
    assert topology_to_dict(committed) == sc.expected_topology, f"{case}: server args→state projection differs from expected_topology"
```

- [ ] **Step 3: Run to verify failure**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_corpus.py -q -n 0`
Expected: FAIL — `evals.lib.battery_corpus` missing.

- [ ] **Step 4: Implement `battery_corpus.py`**

```python
# evals/lib/battery_corpus.py
"""corpus.md parsing — the first unlabelled fenced block under each ``## <case>`` heading is the
prompt, copied byte-for-byte to the wire (ops-local/acceptance/extract_intents.py discipline)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = REPO_ROOT / "evals/composer-battery/corpus.md"
SCENARIOS_DIR = REPO_ROOT / "evals/composer-battery/scenarios"

_HEADING = re.compile(r"^## (?P<name>[a-z0-9_]+)\s*$", re.MULTILINE)
_VERSION = re.compile(r"^corpus_version:\s*(?P<v>\d+)\s*$", re.MULTILINE)
_FENCE = re.compile(r"^```(?P<lang>[^\n]*)\n(?P<body>.*?)^```\s*$", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class CorpusCase:
    name: str
    prompt: str


def parse_corpus(md: str) -> tuple[int, list[CorpusCase]]:
    vm = _VERSION.search(md)
    if vm is None:
        raise ValueError("corpus.md has no `corpus_version: N` line")
    version = int(vm.group("v"))
    headings = list(_HEADING.finditer(md))
    cases: list[CorpusCase] = []
    for i, h in enumerate(headings):
        start = h.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(md)
        section = md[start:end]
        prompt: str | None = None
        for fm in _FENCE.finditer(section):
            if fm.group("lang").strip() == "":
                body = fm.group("body")
                prompt = body[:-1] if body.endswith("\n") else body
                break
        if prompt is None:
            raise ValueError(f"case {h.group('name')!r} has no unlabelled fenced prompt")
        cases.append(CorpusCase(h.group("name"), prompt))
    return version, cases


def load_corpus(path: Path = CORPUS_PATH) -> tuple[int, dict[str, CorpusCase]]:
    version, cases = parse_corpus(path.read_text())
    return version, {c.name: c for c in cases}


__all__ = ["CORPUS_PATH", "SCENARIOS_DIR", "CorpusCase", "load_corpus", "parse_corpus"]
```

- [ ] **Step 5: Author `corpus.md` — header, canary, fork_coalesce, transform_pipeline (fully specified)**

```markdown
# Composer battery corpus

corpus_version: 0

Rules (spec §1): operator voice; task, never implementation; tight enough that one
shape is the reasonable reading; invented data; every prompt must NOT classify
EXPLICIT_MUTATION (tests/unit/evals/composer_battery/test_corpus.py enforces it).
The first unlabelled fenced block under each heading is sent byte-for-byte.

## canary

```
I've got a tiny list of three colours with a name and a hex code — just make it up.
I only want it read in and written back out as JSON, nothing else done to it.
```

## fork_coalesce

```
Take a short list of products with a sku, a name and a price — make up three.
For every product I want two things worked out at the same time: a shortened name
that's cut down to ten characters, and the price with tax added at ten percent.
Bring both results back together into one row per product and write those rows
out as JSON.
```

## transform_pipeline

```
Make up a handful of orders — an order id, a quantity and a unit price, all as
plain text the way a spreadsheet export would give them. First turn the quantity
and unit price into proper numbers, then work out a line total for each order,
and write the finished orders out as CSV.
```
```

Before writing more, dry-run these three: `source .venv/bin/activate && python -c "from evals.lib.battery_corpus import load_corpus; from elspeth.web.composer.no_tool_policy import classify_pipeline_mutation_intent as c; v,cs=load_corpus(); [print(k, c(x.prompt).name) for k,x in cs.items()]"` — every line must print `AMBIGUOUS` or `CONVERSATIONAL`. Record each decision in the case's `scenario.json` `surface.classifier_decision`.

- [ ] **Step 6: Author the remaining 16 prompts, one per case, in the same voice**

For each case read `examples/<case>/settings.yaml` (variant `settings_top_k.yaml` for `statistical_batch_plugins`) and write a prompt that (a) names the shape's *task* in plain words, (b) pins the stratum-defining detail (e.g. `threshold_gate`: "anything over 100 goes to one file, the rest to another"; `statistical_batch_plugins`: "keep just the top three by score"; `deep_routing`: the five decisions in order; `error_routing`: "rows that fail go to a separate rejects file"; `row_union_ab_experiment`: "split into two groups, tag each, then put them back into one list"; `json_explode`: "each order has a list of items — I want one row per item"; `deaggregation`: "copy each row N times where N is in the row"; `template_lookups`: "classify each headline against these categories: …" (inline the categories); `multi_query_assessment`: inline the criteria as invented content; `openrouter_sentiment`: "sentiment of each review"; `llm_source`: "I don't have data — have the model make up five plausible support tickets, then …"; `schema_contracts_demo`: state the columns and types explicitly and "only rows over 1000 to the high-value file"; `report_assemble`: "collect the lines two at a time into a report"), (c) says to invent the data. Dry-run each with the one-liner above; iterate wording until non-EXPLICIT. Commit after every 4 prompts.

- [ ] **Step 7: Author the 18 remaining `scenario.json` files**

Recipe per case (identical steps; commit in batches of 4):
1. `mkdir -p evals/composer-battery/scenarios/<case>` and copy `fork_coalesce/scenario.json` as the template.
2. Write `canonical_arguments` as a **valid `set_pipeline` payload for the example's topology**, using a plain `path` source (`{"plugin": "<source plugin>", "on_success": "<conn>", "options": {"path": "rows.csv", "schema": {"mode": "observed"}}, "on_validation_failure": "discard"}`), nodes with `input`/`on_success`/`on_error` connection names, and outputs with `sink_name`/`plugin`/`options.path`. Reuse a parity fixture's payload where the topology matches (`row_union` for case 8, `aggregation` for 9, `conditional_gate` for 2/4, `linear_transform` for 1, `error_routing` for 6, `row_expansion` for 11/12, `structured_llm` for 15–17). For LLM transforms use `plugin: "llm"` with `options: {"provider": "openrouter", "model": "…", "response_field": "…", "api_key": {"secret_ref": "OPENROUTER_API_KEY"}}` — check `examples/<case>/settings.yaml` for the exact option keys and copy them.
3. Set `example`, `variant`, `floor.derivation` (list the schemas the discovery turn fetches), `option_assertions` for cases 4 (`["gate", "<threshold option key>", <value>]`) and 10 (`["batch_top_k", "k", 3]` — read the real option name from `examples/statistical_batch_plugins/settings_top_k.yaml`).
4. Regenerate `expected_topology` with the Task 2 Step 3 one-liner (parametrise the path).
5. Run `python -m pytest tests/unit/evals/composer_battery/test_corpus.py -q -n 0 -k "<case>"` until it passes; a `validate_canonical_arguments` failure means a wrong plugin name or option — fix the payload, never the test.
The canary's payload is source csv → one `passthrough` transform → json sink (a `set_pipeline` needs ≥1 node).

- [ ] **Step 8: Run the whole corpus test file**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/ -q -n 0`
Expected: all green, including 19× `test_scenario_is_sound` and 19× `test_prompt_stays_on_the_compose_loop`.

- [ ] **Step 9: README stub and commit**

`evals/composer-battery/README.md`: three lines — what this is (link to spec), how to run tests, "runbook: see Task 10 section once written". Commit:

```bash
git add -- .gitignore evals/lib/battery_corpus.py evals/composer-battery/README.md evals/composer-battery/corpus.md evals/composer-battery/scenarios tests/unit/evals/composer_battery/test_corpus.py
git commit -m "feat(evals): composer battery corpus, scenarios and CI surface gate" -- .gitignore evals/lib/battery_corpus.py evals/composer-battery/README.md evals/composer-battery/corpus.md evals/composer-battery/scenarios tests/unit/evals/composer_battery/test_corpus.py
```

---

### Task 4: Capture parser (`evals/lib/battery_capture.py`)

**Files:**
- Create: `evals/lib/battery_capture.py`
- Create: `tests/unit/evals/composer_battery/test_battery_capture.py`
- Create: `tests/unit/evals/composer_battery/fixtures/run_ideal/{messages.json,state.json,validate.json,reviews.json,meta.json}` (synthetic ideal run, hand-written in Step 3)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (all frozen dataclasses):
  - `LlmCall(sequence_no: int, model_requested: str, model_returned: str | None, status: str, tools_spec_hash: str | None, planner_call_ordinal: int | None, prompt_tokens: int | None, completion_tokens: int | None, cached_prompt_tokens: int | None, provider_cost: float | None, latency_ms: int, started_at: str, finished_at: str, error_class: str | None)`
  - `PlannerAttempt(sequence_no: int, ordinal: int, planner_call_ordinal: int | None, phase: str, outcome: str, planner_code: str | None, led_to: str, selected_tools: tuple[str, ...], requested_information: tuple[str, ...], new_information: tuple[str, ...], rejection_codes: tuple[str, ...], repeated_fingerprint: bool)` (envelope `{"_kind": "planner_attempt_audit", "attempt": {...}}`, keys per `contracts/composer_planner_audit.py:ComposerPlannerAttempt.to_dict`)
  - `ToolCall(id: str, name: str, arguments: dict, outcome: str | None)` (from an assistant row's `tool_calls`; `outcome` is the server stamp if present)
  - `AssistantTurn(sequence_no: int, message_id: str, content: str, raw_content: str | None, tool_calls: tuple[ToolCall, ...])`
  - `ToolRow(sequence_no: int, tool_call_id: str, content: dict | None, composition_state_id: str | None, envelope: dict | None, parent_assistant_id: str | None)`
  - `Capture(messages: list[dict], state: dict | None, validate: dict | None, reviews: list[dict], meta: dict, run_dir: Path | None = None)` with `load_capture(run_dir: Path) -> Capture` (missing `messages.json` or `meta.json` ⇒ `CaptureError`; the other three may be absent — recorded as `None`/`[]`).
  - `llm_calls(capture) -> list[LlmCall]`, `planner_attempts(capture) -> list[PlannerAttempt]`, `assistant_turns(capture) -> list[AssistantTurn]`, `tool_rows(capture) -> list[ToolRow]`, all ordered by `sequence_no`.
  - `tool_outcomes(capture) -> dict[str, str]` — the **durable-pair projection** (`applied|rejected|failed|cancelled|completed`) re-implemented from `src/elspeth/web/sessions/routes/_helpers.py:_tool_call_outcomes_by_call_id` over `messages.json` (tool row `composition_state_id`, envelope `version_before/version_after/status`, content `error_class`/`success`); the assistant stamp is NOT used here — the scorer compares the two.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/evals/composer_battery/test_battery_capture.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.lib.battery_capture import CaptureError, assistant_turns, llm_calls, load_capture, planner_attempts, tool_outcomes, tool_rows

FIXTURES = Path(__file__).parent / "fixtures"


def test_ideal_run_parses_into_typed_rows() -> None:
    cap = load_capture(FIXTURES / "run_ideal")
    calls = llm_calls(cap)
    assert [c.status for c in calls] == ["success", "success", "success"]
    assert [c.tools_spec_hash is not None for c in calls] == [True, False, True]  # tool, advisor, tool
    turns = assistant_turns(cap)
    assert [len(t.tool_calls) for t in turns] == [3, 1]  # batched discovery, then set_pipeline
    assert turns[1].tool_calls[0].name == "set_pipeline"
    assert planner_attempts(cap) == []
    rows = tool_rows(cap)
    assert len(rows) == 4 and all(r.parent_assistant_id for r in rows)


def test_outcomes_use_the_durable_pair_not_the_stamp() -> None:
    cap = load_capture(FIXTURES / "run_ideal")
    out = tool_outcomes(cap)
    assert out["call_sp"] == "applied"  # composition_state_id set on the tool row
    assert out["call_d1"] == "completed"
    # mutate: strip the state id and give a rejecting content → rejected, regardless of the assistant stamp
    doc = json.loads((FIXTURES / "run_ideal/messages.json").read_text())
    for m in doc:
        if m["role"] == "tool" and m["tool_call_id"] == "call_sp":
            m["composition_state_id"] = None
            m["content"] = json.dumps({"success": False, "error": "rejected"})
        if m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                if tc.get("id") == "call_sp":
                    tc["outcome"] = "applied"  # lying stamp
    cap2 = load_capture(FIXTURES / "run_ideal")
    cap2.messages = doc
    assert tool_outcomes(cap2)["call_sp"] == "rejected"


def test_envelope_cancelled_and_failed_statuses() -> None:
    cap = load_capture(FIXTURES / "run_ideal")
    doc = json.loads((FIXTURES / "run_ideal/messages.json").read_text())
    tool = next(m for m in doc if m["role"] == "tool" and m["tool_call_id"] == "call_sp")
    tool["composition_state_id"] = None
    tool["tool_calls"] = [{"_kind": "audit", "status": "cancelled", "version_before": 1, "version_after": 1}]
    cap.messages = doc
    assert tool_outcomes(cap)["call_sp"] == "cancelled"
    tool["tool_calls"] = [{"_kind": "audit", "status": "arg_error", "version_before": 1, "version_after": 1}]
    assert tool_outcomes(cap)["call_sp"] == "failed"
    tool["tool_calls"] = [{"_kind": "audit", "status": "ok", "version_before": 1, "version_after": 2}]
    assert tool_outcomes(cap)["call_sp"] == "applied"


def test_missing_messages_is_a_capture_error(tmp_path: Path) -> None:
    (tmp_path / "meta.json").write_text("{}")
    with pytest.raises(CaptureError):
        load_capture(tmp_path)
```

- [ ] **Step 2: Run to verify failure**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_battery_capture.py -q -n 0`
Expected: FAIL — module missing.

- [ ] **Step 3: Write the synthetic ideal fixture**

`tests/unit/evals/composer_battery/fixtures/run_ideal/messages.json` — a minimal but shape-faithful thread (fields per `ChatMessageResponse`: `id, session_id, role, content, raw_content, segments, tool_calls, created_at, composition_state_id, tool_call_id, parent_assistant_id, sequence_no`). Rows in `sequence_no` order:

```json
[
  {"id":"m1","session_id":"s","role":"user","content":"<prompt>","raw_content":null,"segments":[],"tool_calls":null,"created_at":"2026-08-17T00:00:00Z","composition_state_id":null,"tool_call_id":null,"parent_assistant_id":null,"sequence_no":1},
  {"id":"a1","session_id":"s","role":"audit","content":"","raw_content":null,"segments":[],"tool_calls":[{"_kind":"llm_call_audit","call":{"model_requested":"openrouter/anthropic/claude-sonnet-5","model_returned":"claude-sonnet-5","status":"success","finish_reason":"tool_calls","prompt_tokens":1000,"completion_tokens":100,"total_tokens":1100,"cached_prompt_tokens":null,"cache_creation_input_tokens":null,"cache_read_input_tokens":null,"reasoning_tokens":null,"latency_ms":1200,"provider_request_id":"r1","messages_hash":"mh1","tools_spec_hash":"tsh","declared_tool_names":["get_plugin_schema","set_pipeline"],"started_at":"2026-08-17T00:00:01Z","finished_at":"2026-08-17T00:00:02Z","error_class":null,"error_message":null,"temperature":null,"seed":null,"provider_cost":0.01,"provider_cost_source":"provider","max_completion_tokens_requested":null,"planner_policy_hash":null,"planner_call_ordinal":null}}],"created_at":"2026-08-17T00:00:02Z","composition_state_id":null,"tool_call_id":null,"parent_assistant_id":null,"sequence_no":2},
  {"id":"as1","session_id":"s","role":"assistant","content":"","raw_content":null,"segments":[],"tool_calls":[{"id":"call_d1","type":"function","function":{"name":"get_plugin_schema","arguments":"{\"plugin_type\":\"source\",\"plugin_name\":\"csv\"}"},"outcome":"completed","applied_state_version":null},{"id":"call_d2","type":"function","function":{"name":"get_plugin_schema","arguments":"{\"plugin_type\":\"transform\",\"plugin_name\":\"passthrough\"}"},"outcome":"completed","applied_state_version":null},{"id":"call_d3","type":"function","function":{"name":"get_plugin_schema","arguments":"{\"plugin_type\":\"sink\",\"plugin_name\":\"json\"}"},"outcome":"completed","applied_state_version":null}],"created_at":"2026-08-17T00:00:02Z","composition_state_id":null,"tool_call_id":null,"parent_assistant_id":null,"sequence_no":3},
  {"id":"t1","session_id":"s","role":"tool","content":"{\"success\": true, \"schema\": {}}","raw_content":null,"segments":[],"tool_calls":null,"created_at":"2026-08-17T00:00:03Z","composition_state_id":null,"tool_call_id":"call_d1","parent_assistant_id":"as1","sequence_no":4},
  {"id":"t2","session_id":"s","role":"tool","content":"{\"success\": true, \"schema\": {}}","raw_content":null,"segments":[],"tool_calls":null,"created_at":"2026-08-17T00:00:03Z","composition_state_id":null,"tool_call_id":"call_d2","parent_assistant_id":"as1","sequence_no":5},
  {"id":"t3","session_id":"s","role":"tool","content":"{\"success\": true, \"schema\": {}}","raw_content":null,"segments":[],"tool_calls":null,"created_at":"2026-08-17T00:00:03Z","composition_state_id":null,"tool_call_id":"call_d3","parent_assistant_id":"as1","sequence_no":6},
  {"id":"a2","session_id":"s","role":"audit","content":"","raw_content":null,"segments":[],"tool_calls":[{"_kind":"llm_call_audit","call":{"model_requested":"openrouter/anthropic/claude-opus-4-8","model_returned":"claude-opus-4-8","status":"success","finish_reason":"stop","prompt_tokens":500,"completion_tokens":50,"total_tokens":550,"cached_prompt_tokens":null,"cache_creation_input_tokens":null,"cache_read_input_tokens":null,"reasoning_tokens":null,"latency_ms":900,"provider_request_id":"r2","messages_hash":"mh2","tools_spec_hash":null,"declared_tool_names":[],"started_at":"2026-08-17T00:00:04Z","finished_at":"2026-08-17T00:00:05Z","error_class":null,"error_message":null,"temperature":null,"seed":null,"provider_cost":0.02,"provider_cost_source":"provider","max_completion_tokens_requested":null,"planner_policy_hash":null,"planner_call_ordinal":null}}],"created_at":"2026-08-17T00:00:05Z","composition_state_id":null,"tool_call_id":null,"parent_assistant_id":null,"sequence_no":7},
  {"id":"a3","session_id":"s","role":"audit","content":"","raw_content":null,"segments":[],"tool_calls":[{"_kind":"llm_call_audit","call":{"model_requested":"openrouter/anthropic/claude-sonnet-5","model_returned":"claude-sonnet-5","status":"success","finish_reason":"tool_calls","prompt_tokens":1500,"completion_tokens":300,"total_tokens":1800,"cached_prompt_tokens":900,"cache_creation_input_tokens":null,"cache_read_input_tokens":900,"reasoning_tokens":null,"latency_ms":2500,"provider_request_id":"r3","messages_hash":"mh3","tools_spec_hash":"tsh","declared_tool_names":["get_plugin_schema","set_pipeline"],"started_at":"2026-08-17T00:00:06Z","finished_at":"2026-08-17T00:00:09Z","error_class":null,"error_message":null,"temperature":null,"seed":null,"provider_cost":0.03,"provider_cost_source":"provider","max_completion_tokens_requested":null,"planner_policy_hash":null,"planner_call_ordinal":null}}],"created_at":"2026-08-17T00:00:09Z","composition_state_id":null,"tool_call_id":null,"parent_assistant_id":null,"sequence_no":8},
  {"id":"as2","session_id":"s","role":"assistant","content":"Done.","raw_content":null,"segments":[],"tool_calls":[{"id":"call_sp","type":"function","function":{"name":"set_pipeline","arguments":"{\"source\":{\"plugin\":\"csv\"},\"nodes\":[],\"outputs\":[]}"},"outcome":"applied","applied_state_version":2}],"created_at":"2026-08-17T00:00:09Z","composition_state_id":null,"tool_call_id":null,"parent_assistant_id":null,"sequence_no":9},
  {"id":"t4","session_id":"s","role":"tool","content":"{\"success\": true, \"summary\": \"pipeline set\"}","raw_content":null,"segments":[],"tool_calls":null,"created_at":"2026-08-17T00:00:10Z","composition_state_id":"state-2","tool_call_id":"call_sp","parent_assistant_id":"as2","sequence_no":10}
]
```

`state.json`: `{"state_id": "state-2", "version": 2, "sources": {"source": {"plugin": "csv", "on_success": "in", "options": {"path": "rows.csv"}}}, "nodes": [{"id": "p", "node_type": "transform", "plugin": "passthrough", "input": "in", "on_success": "out", "on_error": "discard", "options": {}}], "edges": [], "outputs": [{"name": "out", "plugin": "json", "options": {}}]}` · `validate.json`: `{"is_valid": true, "checks": [], "errors": [], "warnings": [], "readiness": "ready"}` · `reviews.json`: `[]` · `meta.json`: `{"round": "t", "case": "canary", "repeat": 1, "corpus_version": 0, "prompt_sha256": "x", "session_id": "s", "state_id": "state-2", "http": [{"step": "post_message", "status": 200, "elapsed_ms": 9000}], "server_terminal": {"budget_exhausted": null, "reason": null, "source": "none"}, "identity": {"binding": {"substrate": "https://elspeth.foundryside.dev", "composer_model": "openrouter/anthropic/claude-sonnet-5", "advisor_model": "openrouter/anthropic/claude-opus-4-8", "model_returned": "claude-sonnet-5", "composer_timeout_seconds": 600.0, "budgets": {"composition_turns": 30, "discovery_turns": 10}, "tools_spec_hash": "tsh", "temperature": null, "seed": null}, "recorded": {"composer_skill_hash": null, "first_call_messages_hash": "mh1", "server_version": "0.7.2", "frontend_build": "index-x.js"}}}`.

- [ ] **Step 4: Implement the parser**

```python
# evals/lib/battery_capture.py
"""Typed view over a captured battery run directory (spec §4/§5).

Everything the scorer reads comes from these accessors, so a taxonomy
revision never re-parses raw JSON. ``tool_outcomes`` re-implements the
server's durable-pair projection (routes/_helpers.py
``_tool_call_outcomes_by_call_id``) so offline scoring never trusts a tool
NAME or the assistant stamp alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CaptureError(RuntimeError):
    """A run directory is missing an artifact the scorer cannot do without."""


@dataclass
class Capture:
    messages: list[dict[str, Any]]
    state: dict[str, Any] | None
    validate: dict[str, Any] | None
    reviews: list[dict[str, Any]]
    meta: dict[str, Any]
    run_dir: Path | None = None


@dataclass(frozen=True)
class LlmCall:
    sequence_no: int
    model_requested: str
    model_returned: str | None
    status: str
    tools_spec_hash: str | None
    planner_call_ordinal: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_prompt_tokens: int | None
    provider_cost: float | None
    latency_ms: int
    started_at: str
    finished_at: str
    error_class: str | None


@dataclass(frozen=True)
class PlannerAttempt:
    sequence_no: int
    ordinal: int
    planner_call_ordinal: int | None
    phase: str
    outcome: str
    planner_code: str | None
    led_to: str
    selected_tools: tuple[str, ...]
    requested_information: tuple[str, ...]
    new_information: tuple[str, ...]
    rejection_codes: tuple[str, ...]
    repeated_fingerprint: bool


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    outcome: str | None


@dataclass(frozen=True)
class AssistantTurn:
    sequence_no: int
    message_id: str
    content: str
    raw_content: str | None
    tool_calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class ToolRow:
    sequence_no: int
    tool_call_id: str
    content: dict[str, Any] | None
    composition_state_id: str | None
    envelope: dict[str, Any] | None
    parent_assistant_id: str | None


def _read_json(path: Path, *, required: bool) -> Any:
    if not path.exists():
        if required:
            raise CaptureError(f"missing {path.name} in {path.parent}")
        return None
    try:
        return json.loads(path.read_text())
    except ValueError as exc:
        raise CaptureError(f"unparseable {path}: {exc}") from exc


def load_capture(run_dir: Path) -> Capture:
    run_dir = Path(run_dir)
    messages = _read_json(run_dir / "messages.json", required=True)
    meta = _read_json(run_dir / "meta.json", required=True)
    if not isinstance(messages, list) or not isinstance(meta, dict):
        raise CaptureError(f"{run_dir}: messages.json must be a list and meta.json an object")
    reviews = _read_json(run_dir / "reviews.json", required=False)
    return Capture(
        messages=sorted(messages, key=lambda m: (m.get("sequence_no") is None, m.get("sequence_no") or 0)),
        state=_read_json(run_dir / "state.json", required=False),
        validate=_read_json(run_dir / "validate.json", required=False),
        reviews=list(reviews) if isinstance(reviews, list) else [],
        meta=meta,
        run_dir=run_dir,
    )


def _seq(m: dict[str, Any]) -> int:
    return int(m.get("sequence_no") or 0)


def _audit_envelopes(capture: Capture, kind: str) -> list[tuple[int, dict[str, Any]]]:
    out: list[tuple[int, dict[str, Any]]] = []
    for m in capture.messages:
        if m.get("role") != "audit":
            continue
        for env in m.get("tool_calls") or []:
            if isinstance(env, dict) and env.get("_kind") == kind:
                out.append((_seq(m), env))
    return out


def llm_calls(capture: Capture) -> list[LlmCall]:
    calls: list[LlmCall] = []
    for seq, env in _audit_envelopes(capture, "llm_call_audit"):
        c = env.get("call") or {}
        calls.append(
            LlmCall(
                sequence_no=seq,
                model_requested=str(c.get("model_requested")),
                model_returned=c.get("model_returned"),
                status=str(c.get("status")),
                tools_spec_hash=c.get("tools_spec_hash"),
                planner_call_ordinal=c.get("planner_call_ordinal"),
                prompt_tokens=c.get("prompt_tokens"),
                completion_tokens=c.get("completion_tokens"),
                cached_prompt_tokens=c.get("cached_prompt_tokens"),
                provider_cost=c.get("provider_cost"),
                latency_ms=int(c.get("latency_ms") or 0),
                started_at=str(c.get("started_at")),
                finished_at=str(c.get("finished_at")),
                error_class=c.get("error_class"),
            )
        )
    return calls


def planner_attempts(capture: Capture) -> list[PlannerAttempt]:
    out: list[PlannerAttempt] = []
    for seq, env in _audit_envelopes(capture, "planner_attempt_audit"):
        a = env.get("attempt") or {}
        out.append(
            PlannerAttempt(
                sequence_no=seq,
                ordinal=int(a.get("ordinal") or 0),
                phase=str(a.get("phase")),
                outcome=str(a.get("outcome")),
                planner_call_ordinal=a.get("planner_call_ordinal"),
                planner_code=a.get("planner_code"),
                led_to=str(a.get("led_to")),
                selected_tools=tuple(str(t) for t in (a.get("selected_tools") or [])),
                requested_information=tuple(str(t) for t in (a.get("requested_information") or [])),
                new_information=tuple(str(t) for t in (a.get("new_information") or [])),
                rejection_codes=tuple(str(t) for t in (a.get("rejection_codes") or [])),
                repeated_fingerprint=bool(a.get("repeated_fingerprint")),
            )
        )
    return out


def assistant_turns(capture: Capture) -> list[AssistantTurn]:
    turns: list[AssistantTurn] = []
    for m in capture.messages:
        if m.get("role") != "assistant":
            continue
        calls: list[ToolCall] = []
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict) or "function" not in tc:
                continue
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
            except ValueError:
                args = {"_unparseable": True}
            calls.append(ToolCall(id=str(tc.get("id")), name=str(fn.get("name")), arguments=args if isinstance(args, dict) else {}, outcome=tc.get("outcome")))
        turns.append(AssistantTurn(_seq(m), str(m.get("id")), str(m.get("content") or ""), m.get("raw_content"), tuple(calls)))
    return turns


def tool_rows(capture: Capture) -> list[ToolRow]:
    rows: list[ToolRow] = []
    for m in capture.messages:
        if m.get("role") != "tool" or not m.get("tool_call_id"):
            continue
        content: dict[str, Any] | None
        try:
            parsed = json.loads(m.get("content") or "")
            content = parsed if isinstance(parsed, dict) else None
        except ValueError:
            content = None
        env = None
        if m.get("tool_calls"):
            first = m["tool_calls"][0]
            env = first if isinstance(first, dict) else None
        rows.append(ToolRow(_seq(m), str(m["tool_call_id"]), content, m.get("composition_state_id"), env, m.get("parent_assistant_id")))
    return rows


_FAILED_STATUSES = frozenset({"arg_error", "plugin_crash"})


def tool_outcomes(capture: Capture) -> dict[str, str]:
    """Durable-pair projection: applied | rejected | failed | cancelled | completed."""
    out: dict[str, str] = {}
    for row in tool_rows(capture):
        env = row.envelope
        if env is None and row.composition_state_id is not None:
            out[row.tool_call_id] = "applied"
            continue
        if env is not None:
            vb, va = env.get("version_before"), env.get("version_after")
            if isinstance(vb, int) and isinstance(va, int) and va > vb:
                out[row.tool_call_id] = "applied"
                continue
            status = env.get("status")
            if status == "cancelled":
                out[row.tool_call_id] = "cancelled"
                continue
            if status in _FAILED_STATUSES:
                out[row.tool_call_id] = "failed"
                continue
        content = row.content
        if isinstance(content, dict):
            if content.get("error_class"):
                out[row.tool_call_id] = "cancelled" if content.get("_redaction_status") == "cancelled" else "failed"
                continue
            if content.get("success") is False:
                out[row.tool_call_id] = "rejected"
                continue
        out[row.tool_call_id] = "completed"
    return out


__all__ = ["AssistantTurn", "Capture", "CaptureError", "LlmCall", "PlannerAttempt", "ToolCall", "ToolRow", "assistant_turns", "llm_calls", "load_capture", "planner_attempts", "tool_outcomes", "tool_rows"]
```

Check the real `ComposerToolStatus` values before committing: `git grep -n "class ComposerToolStatus" -A12 -- src/elspeth/contracts/` — `_FAILED_STATUSES` must equal `{ARG_ERROR.value, PLUGIN_CRASH.value}` and the cancelled literal must equal `CANCELLED.value`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_battery_capture.py -q -n 0`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add -- evals/lib/battery_capture.py tests/unit/evals/composer_battery/test_battery_capture.py tests/unit/evals/composer_battery/fixtures
git commit -m "feat(evals): battery capture parser with durable-pair tool outcomes" -- evals/lib/battery_capture.py tests/unit/evals/composer_battery/test_battery_capture.py tests/unit/evals/composer_battery/fixtures
```

---

### Task 5: Scorer (`evals/lib/battery_score.py`)

**Files:**
- Create: `evals/lib/battery_score.py`
- Create: `tests/unit/evals/composer_battery/threadgen.py` (test-only builder for synthetic captures — imported by Task 5, 6 and 8 tests)
- Create: `tests/unit/evals/composer_battery/test_battery_score.py`

**Interfaces:**
- Consumes: Task 4 `Capture`, `LlmCall`, `ToolCall`, `llm_calls`, `planner_attempts`, `assistant_turns`, `tool_rows`, `tool_outcomes`; Task 2 `Scenario`, `Floor`, `topology_from_dict`; Task 1 `topology_from_pipeline`, `topologies_match`, `observed_option_values`; `PASSIVITY_PHRASES`, `BUILD_FAILURE_SENTINELS` from `evals.lib.scenario_from_example`; `is_discovery_tool`, `is_mutation_tool` from `elspeth.web.composer.tools.discovery`.
- Produces:
  - `EXCLUSION_KINDS = ("no_calls","auth","http","read_integrity","truncated","surface","terminal_missing","transport","capture")`
  - `SEVERITY: dict[str, str]` — class → `"soft"|"hard"` (spec §3 table; `wrong_shape` hard; `unattributed_excess` soft; `retried_provider_error` soft)
  - `@dataclass(frozen=True) Deviation(cls: str, sequence_no: tuple[int, int], tool: str | None = None, args_digest: str | None = None, codes: tuple[str, ...] = (), audit_ordinal: int | None = None)` with `to_dict()` (key `"class"`, plus `"severity"`)
  - `@dataclass Score(...)` — every `score.json` field of spec §5 as attributes (`case, repeat, surface_observed, tool_bearing_calls, advisor_calls, other_text_calls, retried_calls, audited_provider_calls, floor, excess, deviations: list[Deviation], review_rounds, recipe_used, green, red, is_valid, wrong_shape, clean, optimal, excluded, tokens: dict, cost, wall_ms, red_reasons: list[str], green_reasons: list[str], exclusion_evidence: str | None`) with `to_dict()`
  - `score_run(capture: Capture, scenario: Scenario) -> Score`
  - `write_score(run_dir: Path, score: Score) -> Path` (writes `score.json`)
  - `score_from_disk(run_dir: Path, scenario: Scenario) -> Score` — `load_capture` + `score_run`; a `CaptureError` becomes `excluded="capture"` with the message as `exclusion_evidence` (never raises)
  - `surface_of(capture: Capture) -> str` — `"undetermined"` (zero `llm_call_audit` rows) / `"planner"` (any `planner_call_ordinal != null` or any `planner_attempt_audit` row) / `"compose_loop"`; the single surface rule, reused by Task 8
- **`meta.json` contract consumed here and produced by Task 7:** `meta["instrument"] = {"truncated": bool, "read_integrity": str | None, "http_unrecovered": str | None, "auth_failed": bool, "review_rounds_exhausted": bool}` and `meta["http"]` entries `{"step": str, "status": int | None, "elapsed_ms": int, "detail": object | None}` where `step ∈ {"create_session","patch_title","patch_preferences","post_message","composer_progress","settle","list_reviews","resolve_review","get_state","validate","get_messages","get_proposals","get_proposal_events"}` and `meta["server_terminal"] = {"budget_exhausted": str | None, "reason": str | None, "source": "422_detail|composer_progress|none"}`.

Precedence of exclusions (first match wins; the run is excluded from every rate): `capture` → `truncated` → `read_integrity` → `auth` → `http` (also when `review_rounds_exhausted`, evidence `"interpretation review rounds exhausted (5)"` — the capture never reached a settled state) → `surface` → `transport` → `no_calls` → `terminal_missing`.

- [ ] **Step 1: Write the synthetic-thread builder**

```python
# tests/unit/evals/composer_battery/threadgen.py
"""Builders for synthetic battery captures.

Every helper returns plain dicts in the exact wire shape ``GET /messages``
serialises (see tests/unit/evals/composer_battery/fixtures/run_ideal), so
a scorer test reads like the thread it describes.
"""

from __future__ import annotations

import json
from typing import Any

from evals.lib.battery_capture import Capture

COMPOSER = "openrouter/anthropic/claude-sonnet-5"
ADVISOR = "openrouter/anthropic/claude-opus-4-8"
TOOLS_HASH = "tsh"

_T0 = "2026-08-17T00:00:{:02d}Z"


def audit_row(seq: int, *, status: str = "success", tools: bool = True, model: str = COMPOSER, planner_ordinal: int | None = None, prompt_tokens: int | None = 100, completion_tokens: int | None = 10, cached: int | None = None, cost: float | None = 0.01, error_class: str | None = None) -> dict[str, Any]:
    call = {
        "model_requested": model, "model_returned": model.rsplit("/", 1)[-1], "status": status, "finish_reason": "tool_calls" if tools else "stop",
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
        "cached_prompt_tokens": cached, "cache_creation_input_tokens": None, "cache_read_input_tokens": cached, "reasoning_tokens": None,
        "latency_ms": 1000, "provider_request_id": f"r{seq}", "messages_hash": f"mh{seq}", "tools_spec_hash": TOOLS_HASH if tools else None,
        "declared_tool_names": ["get_plugin_schema", "set_pipeline"] if tools else [], "started_at": _T0.format(seq % 60), "finished_at": _T0.format((seq + 1) % 60),
        "error_class": error_class, "error_message": None, "temperature": None, "seed": None, "provider_cost": cost, "provider_cost_source": "provider",
        "max_completion_tokens_requested": None, "planner_policy_hash": None, "planner_call_ordinal": planner_ordinal,
    }
    return {"id": f"a{seq}", "session_id": "s", "role": "audit", "content": "", "raw_content": None, "segments": [], "tool_calls": [{"_kind": "llm_call_audit", "call": call}], "created_at": _T0.format(seq % 60), "composition_state_id": None, "tool_call_id": None, "parent_assistant_id": None, "sequence_no": seq}


def planner_attempt_row(seq: int, *, ordinal: int = 1, phase: str = "discovery", outcome: str = "accepted", planner_code: str | None = None, led_to: str = "done", new_information: tuple[str, ...] = ()) -> dict[str, Any]:
    attempt = {"ordinal": ordinal, "planner_call_ordinal": ordinal, "phase": phase, "outcome": outcome, "planner_code": planner_code, "led_to": led_to, "selected_tools": [], "requested_information": [], "new_information": list(new_information), "rejection_codes": [], "candidate_shape_hash": None, "repeated_fingerprint": False}
    return {"id": f"pa{seq}", "session_id": "s", "role": "audit", "content": "", "raw_content": None, "segments": [], "tool_calls": [{"_kind": "planner_attempt_audit", "attempt": attempt}], "created_at": _T0.format(seq % 60), "composition_state_id": None, "tool_call_id": None, "parent_assistant_id": None, "sequence_no": seq}


def call(cid: str, name: str, args: dict[str, Any] | None = None, *, stamp: str | None = None) -> dict[str, Any]:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}, "outcome": stamp, "applied_state_version": None}


def assistant_row(seq: int, calls: list[dict[str, Any]] | None = None, *, content: str = "") -> dict[str, Any]:
    return {"id": f"as{seq}", "session_id": "s", "role": "assistant", "content": content, "raw_content": None, "segments": [], "tool_calls": calls or None, "created_at": _T0.format(seq % 60), "composition_state_id": None, "tool_call_id": None, "parent_assistant_id": None, "sequence_no": seq}


def tool_row(seq: int, cid: str, parent: str, *, content: dict[str, Any] | None = None, state_id: str | None = None, envelope: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(content if content is not None else {"success": True})
    return {"id": f"t{seq}", "session_id": "s", "role": "tool", "content": body, "raw_content": None, "segments": [], "tool_calls": [envelope] if envelope else None, "created_at": _T0.format(seq % 60), "composition_state_id": state_id, "tool_call_id": cid, "parent_assistant_id": parent, "sequence_no": seq}


def user_row(seq: int, prompt: str = "prompt") -> dict[str, Any]:
    return {"id": f"u{seq}", "session_id": "s", "role": "user", "content": prompt, "raw_content": None, "segments": [], "tool_calls": None, "created_at": _T0.format(seq % 60), "composition_state_id": None, "tool_call_id": None, "parent_assistant_id": None, "sequence_no": seq}


def meta(*, case: str = "fork_coalesce", repeat: int = 1, post_status: int = 200, detail: dict[str, Any] | None = None, terminal: dict[str, Any] | None = None, instrument: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "round": "t", "case": case, "repeat": repeat, "corpus_version": 0, "prompt_sha256": "x", "session_id": "s", "state_id": "state-2",
        "http": [{"step": "post_message", "status": post_status, "elapsed_ms": 1, "detail": detail}],
        "server_terminal": terminal or {"budget_exhausted": None, "reason": None, "source": "none"},
        "instrument": instrument or {"truncated": False, "read_integrity": None, "http_unrecovered": None, "auth_failed": False, "review_rounds_exhausted": False},
        "identity": {"binding": {"substrate": "https://elspeth.foundryside.dev", "composer_model": COMPOSER, "advisor_model": ADVISOR, "model_returned": "claude-sonnet-5", "composer_timeout_seconds": 600.0, "budgets": {"composition_turns": 30, "discovery_turns": 10}, "tools_spec_hash": TOOLS_HASH, "temperature": None, "seed": None},
                     "recorded": {"composer_skill_hash": None, "first_call_messages_hash": "mh2", "server_version": "0.7.2", "frontend_build": None}},
    }


def capture(messages: list[dict[str, Any]], *, state: dict[str, Any] | None, is_valid: bool | None = True, reviews: list[dict[str, Any]] | None = None, meta_doc: dict[str, Any] | None = None) -> Capture:
    validate = None if is_valid is None else {"is_valid": is_valid, "checks": [], "errors": [], "warnings": [], "readiness": "ready" if is_valid else "blocked"}
    return Capture(messages=sorted(messages, key=lambda m: m["sequence_no"]), state=state, validate=validate, reviews=reviews or [], meta=meta_doc or meta(), run_dir=None)


def ideal_thread(args: dict[str, Any], *, schema_calls: int = 3) -> list[dict[str, Any]]:
    """Exactly the floor: user → audit(tool) → assistant[N×get_plugin_schema] → tool rows → audit(tool) → assistant[set_pipeline applied] → tool row."""
    rows: list[dict[str, Any]] = [user_row(1), audit_row(2)]
    disc = [call(f"d{i}", "get_plugin_schema", {"plugin_type": "transform", "plugin_name": f"p{i}"}, stamp="completed") for i in range(schema_calls)]
    rows.append(assistant_row(3, disc))
    seq = 4
    for i in range(schema_calls):
        rows.append(tool_row(seq, f"d{i}", "as3", content={"success": True, "schema": {}}))
        seq += 1
    rows.append(audit_row(seq))
    seq += 1
    sp_seq = seq
    rows.append(assistant_row(seq, [call("sp", "set_pipeline", args, stamp="applied")], content="Done."))
    seq += 1
    rows.append(tool_row(seq, "sp", f"as{sp_seq}", content={"success": True}, state_id="state-2"))
    return rows
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/evals/composer_battery/test_battery_score.py
"""Scorer both-ways, near-miss, cross-class negatives, terminal boundaries, instrument honesty (spec §6)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from elspeth.web.composer.tools.discovery import is_mutation_tool
from evals.lib.battery_capture import Capture
from evals.lib.battery_scenario import load_scenario
from evals.lib.battery_score import EXCLUSION_KINDS, SEVERITY, Deviation, Score, score_from_disk, score_run, write_score
from tests.unit.evals.composer_battery import threadgen as tg

REPO = Path(__file__).resolve().parents[4]
SC = load_scenario(REPO / "evals/composer-battery/scenarios/fork_coalesce/scenario.json")
ARGS = SC.canonical_arguments

SPEC_MUTATION_VOCAB = {"set_pipeline", "set_source", "set_source_from_blob", "set_source_from_blobs", "set_output", "upsert_node", "upsert_edge", "patch_source_options", "patch_node_options", "patch_output_options", "remove_node", "remove_edge", "remove_output", "clear_source", "splice_transform", "apply_pipeline_recipe", "set_metadata"}


def _classes(score: Score) -> list[str]:
    return [d.cls for d in score.deviations]


def _ideal() -> Capture:
    return tg.capture(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))


def test_spec_mutation_vocabulary_is_the_registry_mutation_set() -> None:
    assert all(is_mutation_tool(n) for n in SPEC_MUTATION_VOCAB), [n for n in SPEC_MUTATION_VOCAB if not is_mutation_tool(n)]


def test_ideal_thread_at_floor_is_clean_and_optimal() -> None:
    s = score_run(_ideal(), SC)
    assert s.surface_observed == "compose_loop"
    assert (s.tool_bearing_calls, s.advisor_calls, s.other_text_calls, s.audited_provider_calls) == (2, 0, 0, 2)
    assert s.floor == 2 and s.excess == 0
    assert s.deviations == [] and s.green and not s.red and s.is_valid and not s.wrong_shape
    assert s.clean and s.optimal and s.excluded is None
    assert s.tokens == {"prompt": 200, "completion": 20, "cached_prompt": 0, "unknown_calls": 0}
    assert s.cost == pytest.approx(0.02) and s.wall_ms == 6000  # audit rows :02→:03 and :07→:08
    assert s.review_rounds == 0 and s.recipe_used is False


def test_advisor_bucket_by_model_not_by_null_tools_hash() -> None:
    rows = tg.ideal_thread(ARGS)
    rows.append(tg.audit_row(30, tools=False, model=tg.ADVISOR))
    rows.append(tg.audit_row(31, tools=False, model=tg.COMPOSER))  # a text-only composer call is NOT advisor
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert (s.tool_bearing_calls, s.advisor_calls, s.other_text_calls, s.audited_provider_calls) == (2, 1, 1, 4)
    assert s.optimal  # text-only calls do not count against the floor


def test_repair_fires_on_durable_not_applied_then_retry_and_not_backtrack() -> None:
    rows = tg.ideal_thread(ARGS)
    rows[-2]["tool_calls"][0]["outcome"] = "applied"  # lying stamp on the first attempt
    rows[-1]["composition_state_id"] = None
    rows[-1]["content"] = json.dumps({"success": False, "error": "validation failed", "errors": [{"code": "E_MISSING_SINK"}]})
    rows.append(tg.audit_row(20))
    rows.append(tg.assistant_row(21, [tg.call("sp2", "set_pipeline", ARGS, stamp="applied")]))
    rows.append(tg.tool_row(22, "sp2", "as21", state_id="state-3"))
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["repair"]
    d = s.deviations[0]
    assert d.tool == "set_pipeline" and d.sequence_no == (9, 22) and "E_MISSING_SINK" in d.codes and d.audit_ordinal == 2
    assert s.tool_bearing_calls == 3 and s.excess == 1 and not s.clean and s.green


def test_near_miss_durable_applied_with_lying_rejected_stamp_does_not_repair() -> None:
    rows = tg.ideal_thread(ARGS)
    rows[-2]["tool_calls"][0]["outcome"] = "rejected"  # stamp lies; tool row has composition_state_id
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert s.deviations == [] and s.clean


def test_cancelled_outcome_is_not_applied_so_a_retry_is_a_repair() -> None:
    rows = tg.ideal_thread(ARGS)
    rows[-1]["composition_state_id"] = None
    rows[-1]["tool_calls"] = [{"_kind": "audit", "status": "cancelled", "version_before": 1, "version_after": 1}]
    rows.append(tg.audit_row(20))
    rows.append(tg.assistant_row(21, [tg.call("sp2", "set_pipeline", ARGS)]))
    rows.append(tg.tool_row(22, "sp2", "as21", state_id="state-3"))
    assert _classes(score_run(tg.capture(rows, state=ARGS), SC)) == ["repair"]


def test_backtrack_fires_on_wholesale_reset_and_not_repair() -> None:
    rows = tg.ideal_thread(ARGS)  # first set_pipeline applied
    rows.append(tg.audit_row(20))
    rows.append(tg.assistant_row(21, [tg.call("sp2", "set_pipeline", ARGS)]))
    rows.append(tg.tool_row(22, "sp2", "as21", state_id="state-3"))
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["backtrack"] and s.deviations[0].sequence_no == (9, 22)


def test_backtrack_fires_on_remove_after_apply() -> None:
    rows = tg.ideal_thread(ARGS)
    rows.append(tg.audit_row(20))
    rows.append(tg.assistant_row(21, [tg.call("rm", "remove_node", {"node_id": "fork"})]))
    rows.append(tg.tool_row(22, "rm", "as21", state_id="state-3"))
    assert _classes(score_run(tg.capture(rows, state=ARGS), SC)) == ["backtrack"]


def test_excess_discovery_both_ways() -> None:
    p0 = {"plugin_type": "transform", "plugin_name": "p0"}
    rows = [
        tg.user_row(1), tg.audit_row(2), tg.assistant_row(3, [tg.call("d0", "get_plugin_schema", p0)]), tg.tool_row(4, "d0", "as3"),
        tg.audit_row(5), tg.assistant_row(6, [tg.call("d0b", "get_plugin_schema", p0)]), tg.tool_row(7, "d0b", "as6"),  # second read, no mutation between
        tg.audit_row(8), tg.assistant_row(9, [tg.call("sp", "set_pipeline", ARGS)]), tg.tool_row(10, "sp", "as9", state_id="state-2"),
    ]
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["excess_discovery"] and s.deviations[0].tool == "get_plugin_schema" and s.deviations[0].sequence_no == (6, 7)
    # a re-read AFTER an applied mutation is not excess_discovery (state may have changed) — but the extra
    # tool-bearing call is still above floor, so it surfaces honestly as unattributed_excess
    rows2 = tg.ideal_thread(ARGS)
    rows2.append(tg.audit_row(30))
    rows2.append(tg.assistant_row(31, [tg.call("st", "get_pipeline_state", {})]))
    rows2.append(tg.tool_row(32, "st", "as31"))
    assert _classes(score_run(tg.capture(rows2, state=ARGS), SC)) == ["unattributed_excess"]


def test_schema_fumble_on_repeated_patch_of_same_target() -> None:
    rows = tg.ideal_thread(ARGS)
    for i, seq in enumerate((20, 23)):
        rows.append(tg.audit_row(seq))
        rows.append(tg.assistant_row(seq + 1, [tg.call(f"pn{i}", "patch_node_options", {"node_id": "fork", "options": {"x": i}})]))
        rows.append(tg.tool_row(seq + 2, f"pn{i}", f"as{seq + 1}", state_id=f"state-{3 + i}"))
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["schema_fumble"] and s.deviations[0].sequence_no == (24, 25)


def test_data_setup_detour_and_rework() -> None:
    rows = [tg.user_row(1), tg.audit_row(2), tg.assistant_row(3, [tg.call("cb", "create_blob", {"filename": "rows.csv", "content": "a,b\n1,2"})]), tg.tool_row(4, "cb", "as3", content={"success": True, "blob_id": "b1"})]
    rows += [tg.audit_row(5), tg.assistant_row(6, [tg.call("ub", "update_blob", {"blob_id": "b1", "content": "a,b\n1,3"})]), tg.tool_row(7, "ub", "as6", content={"success": True})]
    rows += [tg.audit_row(8), tg.assistant_row(9, [tg.call("sp", "set_pipeline", ARGS)]), tg.tool_row(10, "sp", "as9", state_id="state-2")]
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["data_setup_detour", "data_rework"]
    assert [SEVERITY[c] for c in _classes(s)] == ["soft", "soft"]


def test_retried_provider_error_counts_but_unrecovered_transport_excludes() -> None:
    rows = tg.ideal_thread(ARGS)
    rows.insert(1, tg.audit_row(0, status="api_error", prompt_tokens=None, completion_tokens=None, cost=None, error_class="APIConnectionError"))
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["retried_provider_error"] and s.retried_calls == 1 and s.excluded is None
    assert s.tokens["unknown_calls"] == 1 and s.audited_provider_calls == 3
    dead = [tg.user_row(1), tg.audit_row(2, status="timeout", prompt_tokens=None, completion_tokens=None, cost=None)]
    s2 = score_run(tg.capture(dead, state=None, is_valid=None, meta_doc=tg.meta(post_status=500)), SC)
    assert s2.excluded == "transport" and not s2.clean


def test_malformed_output_is_a_hard_deviation_not_an_exclusion() -> None:
    rows = tg.ideal_thread(ARGS)
    rows.insert(2, tg.audit_row(20, status="malformed_response"))
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["malformed_output"] and SEVERITY["malformed_output"] == "hard" and s.excluded is None


def test_surface_honesty_zero_audit_rows_and_planner_rows() -> None:
    empty = tg.capture([tg.user_row(1), tg.assistant_row(2, content="I built it.")], state=ARGS)
    s = score_run(empty, SC)
    assert s.surface_observed == "undetermined" and s.excluded == "surface"
    rows = tg.ideal_thread(ARGS)
    rows.append(tg.audit_row(30, planner_ordinal=1))
    assert score_run(tg.capture(rows, state=ARGS), SC).excluded == "surface"
    rows2 = tg.ideal_thread(ARGS)
    rows2.append(tg.planner_attempt_row(30))
    s3 = score_run(tg.capture(rows2, state=ARGS), SC)
    assert s3.surface_observed == "planner" and s3.excluded == "surface"


def test_truncated_and_read_integrity_and_auth_and_http_come_from_meta() -> None:
    base = tg.ideal_thread(ARGS)
    inst = {"truncated": True, "read_integrity": None, "http_unrecovered": None, "auth_failed": False, "review_rounds_exhausted": False}
    assert score_run(tg.capture(base, state=ARGS, meta_doc=tg.meta(instrument=inst)), SC).excluded == "truncated"
    inst = {**inst, "truncated": False, "read_integrity": "AuditIntegrityError: seq gap"}
    assert score_run(tg.capture(base, state=ARGS, meta_doc=tg.meta(instrument=inst)), SC).excluded == "read_integrity"
    inst = {**inst, "read_integrity": None, "auth_failed": True}
    assert score_run(tg.capture(base, state=ARGS, meta_doc=tg.meta(instrument=inst)), SC).excluded == "auth"
    inst = {**inst, "auth_failed": False, "http_unrecovered": "GET /messages 502"}
    assert score_run(tg.capture(base, state=ARGS, meta_doc=tg.meta(instrument=inst)), SC).excluded == "http"
    inst = {**inst, "http_unrecovered": None, "review_rounds_exhausted": True}
    s = score_run(tg.capture(base, state=ARGS, meta_doc=tg.meta(instrument=inst)), SC)
    assert s.excluded == "http" and "review rounds" in (s.exclusion_evidence or "")


def test_terminal_boundaries_read_the_captured_body_not_turn_counts() -> None:
    dead = [tg.user_row(1), tg.audit_row(2), tg.assistant_row(3, [tg.call("d0", "get_plugin_schema", {"plugin_type": "source", "plugin_name": "csv"})]), tg.tool_row(4, "d0", "as3")]
    comp = tg.meta(post_status=422, detail={"turns_used": 40, "budget_exhausted": "composition", "reason": "convergence_composition_budget"}, terminal={"budget_exhausted": "composition", "reason": "convergence_composition_budget", "source": "422_detail"})
    s = score_run(tg.capture(dead, state=None, is_valid=None, meta_doc=comp), SC)
    assert _classes(s) == ["turn_exhaustion"] and s.excluded is None and not s.green
    disc = tg.meta(post_status=422, detail={"turns_used": 39, "budget_exhausted": "discovery", "reason": "convergence_discovery_budget"}, terminal={"budget_exhausted": "discovery", "reason": "convergence_discovery_budget", "source": "422_detail"})
    assert _classes(score_run(tg.capture(dead, state=None, is_valid=None, meta_doc=disc), SC)) == ["turn_exhaustion"]
    wall = tg.meta(post_status=None, terminal={"budget_exhausted": "timeout", "reason": "convergence_wall_clock_timeout", "source": "composer_progress"})
    assert _classes(score_run(tg.capture(dead, state=None, is_valid=None, meta_doc=wall), SC)) == ["wall_timeout"]
    # a provider-call TIMEOUT status is NOT wall_timeout; with a recovered retry it is retried_provider_error
    prov = list(dead)
    prov.insert(1, tg.audit_row(0, status="timeout", prompt_tokens=None, completion_tokens=None, cost=None))
    s4 = score_run(tg.capture(prov, state=None, is_valid=None, meta_doc=comp), SC)
    assert "wall_timeout" not in _classes(s4) and "retried_provider_error" in _classes(s4)
    # non-200 with no terminal body ⇒ terminal_missing (excluded)
    missing = tg.meta(post_status=500)
    assert score_run(tg.capture(dead, state=None, is_valid=None, meta_doc=missing), SC).excluded == "terminal_missing"
    # a 200 with a valid state is never terminal_missing even though the terminal source is none
    assert score_run(_ideal(), SC).excluded is None


def test_decline_and_passivity_are_hard_and_not_terminal_missing() -> None:
    passive = [tg.user_row(1), tg.audit_row(2, tools=True), tg.assistant_row(3, content="I can build that. Would you like me to proceed?")]
    s = score_run(tg.capture(passive, state=None, is_valid=None), SC)
    assert _classes(s) == ["passivity"] and s.red and s.excluded is None
    prose = [tg.user_row(1), tg.audit_row(2, tools=True), tg.assistant_row(3, content="Here is how you would do it: use csv then json.")]
    s2 = score_run(tg.capture(prose, state=None, is_valid=None), SC)
    assert _classes(s2) == ["decline"] and s2.red


def test_wrong_shape_and_option_assertions() -> None:
    wrong = copy.deepcopy(ARGS)
    wrong["outputs"][0]["plugin"] = "jsonl"
    s = score_run(tg.capture(tg.ideal_thread(wrong), state=wrong), SC)
    assert s.wrong_shape and _classes(s) == ["wrong_shape"] and not s.green and not s.clean and s.is_valid
    # invalid final state: not wrong_shape (shape is only judged on is_valid), red
    s2 = score_run(tg.capture(tg.ideal_thread(ARGS), state=ARGS, is_valid=False), SC)
    assert not s2.wrong_shape and s2.red and not s2.green


def test_unattributed_excess_is_visible() -> None:
    rows = tg.ideal_thread(ARGS)
    rows.insert(2, tg.audit_row(20))  # an extra tool-bearing call that produced no tool call at all
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert s.excess == 1 and _classes(s) == ["unattributed_excess"] and not s.clean


def test_approval_pending_and_recipe_flag() -> None:
    rows = tg.ideal_thread(ARGS)
    rows[-2]["tool_calls"][0]["function"]["name"] = "apply_pipeline_recipe"
    rows[-1]["composition_state_id"] = None
    rows[-1]["content"] = json.dumps({"success": True, "status": "APPROVAL_REQUIRED", "proposal_id": "p1"})
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert s.recipe_used and "approval_pending" in _classes(s)


def test_no_calls_when_audit_rows_exist_but_none_are_tool_bearing() -> None:
    rows = [tg.user_row(1), tg.audit_row(2, tools=False, model=tg.ADVISOR), tg.assistant_row(3, content="ok")]
    assert score_run(tg.capture(rows, state=None, is_valid=None), SC).excluded == "no_calls"


def test_score_json_round_trip_and_capture_error(tmp_path: Path) -> None:
    s = score_run(_ideal(), SC)
    p = write_score(tmp_path, s)
    doc = json.loads(p.read_text())
    assert doc["clean"] is True and doc["deviations"] == [] and doc["excluded"] is None and set(doc["tokens"]) == {"prompt", "completion", "cached_prompt", "unknown_calls"}
    assert set(EXCLUSION_KINDS) == {"no_calls", "auth", "http", "read_integrity", "truncated", "surface", "terminal_missing", "transport", "capture"}
    (tmp_path / "meta.json").write_text("{}")  # no messages.json
    s2 = score_from_disk(tmp_path, SC)
    assert s2.excluded == "capture" and s2.exclusion_evidence
```

- [ ] **Step 3: Run to verify failure**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_battery_score.py -q -n 0`
Expected: FAIL — `evals.lib.battery_score` missing.

- [ ] **Step 4: Implement the scorer**

```python
# evals/lib/battery_score.py
"""Per-run scorer for the composer battery (spec §3, §5).

Reads only the typed views from ``battery_capture``; never the server. Every
excess above the floor lands in exactly one deviation class or in
``unattributed_excess`` — silence is not an option. Exclusions
(``instrument_error`` sub-kinds) remove the run from every rate and are
reported beside it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from elspeth.web.composer.tools.discovery import is_discovery_tool, is_mutation_tool
from evals.lib.battery_capture import Capture, CaptureError, ToolCall, assistant_turns, llm_calls, load_capture, planner_attempts, tool_outcomes, tool_rows
from evals.lib.battery_scenario import Scenario, topology_from_dict
from evals.lib.battery_topology import observed_option_values, topologies_match, topology_from_pipeline
from evals.lib.scenario_from_example import BUILD_FAILURE_SENTINELS, PASSIVITY_PHRASES

EXCLUSION_KINDS: tuple[str, ...] = ("no_calls", "auth", "http", "read_integrity", "truncated", "surface", "terminal_missing", "transport", "capture")

SEVERITY: dict[str, str] = {
    "excess_discovery": "soft", "schema_fumble": "soft", "data_setup_detour": "soft", "data_rework": "soft", "retried_provider_error": "soft",
    "repair": "hard", "backtrack": "hard", "malformed_output": "hard", "wrong_shape": "hard", "decline": "hard", "passivity": "hard",
    "turn_exhaustion": "hard", "wall_timeout": "hard", "approval_pending": "hard", "unattributed_excess": "soft",
}

_TRANSPORT_STATUSES = frozenset({"api_error", "timeout", "auth_error", "cancelled"})
_MALFORMED_STATUSES = frozenset({"malformed_response", "bad_request_error"})
_NOT_APPLIED = frozenset({"rejected", "failed", "cancelled"})
_REMOVAL_TOOLS = frozenset({"remove_node", "remove_edge", "remove_output", "clear_source"})
_PATCH_TOOLS = frozenset({"patch_source_options", "patch_node_options", "patch_output_options"})
_DATA_TOOLS = frozenset({"create_blob", "update_blob", "delete_blob"})


@dataclass(frozen=True)
class Deviation:
    cls: str
    sequence_no: tuple[int, int]
    tool: str | None = None
    args_digest: str | None = None
    codes: tuple[str, ...] = ()
    audit_ordinal: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"class": self.cls, "severity": SEVERITY[self.cls], "sequence_no": list(self.sequence_no), "tool": self.tool, "args_digest": self.args_digest, "codes": list(self.codes), "audit_ordinal": self.audit_ordinal}


@dataclass
class Score:
    case: str
    repeat: int
    surface_observed: str
    tool_bearing_calls: int
    advisor_calls: int
    other_text_calls: int
    retried_calls: int
    audited_provider_calls: int
    floor: int
    excess: int
    deviations: list[Deviation]
    review_rounds: int
    recipe_used: bool
    green: bool
    red: bool
    is_valid: bool | None
    wrong_shape: bool
    clean: bool
    optimal: bool
    excluded: str | None
    tokens: dict[str, int]
    cost: float | None
    wall_ms: int
    red_reasons: list[str] = field(default_factory=list)
    green_reasons: list[str] = field(default_factory=list)
    exclusion_evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "deviations"}
        d["deviations"] = [x.to_dict() for x in self.deviations]
        return d


def write_score(run_dir: Path, score: Score) -> Path:
    p = Path(run_dir) / "score.json"
    p.write_text(json.dumps(score.to_dict(), indent=2, sort_keys=True) + "\n")
    return p


def score_from_disk(run_dir: Path, scenario: Scenario) -> Score:
    try:
        cap = load_capture(Path(run_dir))
    except CaptureError as exc:
        return _excluded_stub(scenario, "capture", str(exc), repeat=_repeat_from_dir(Path(run_dir)))
    return score_run(cap, scenario)


def _repeat_from_dir(run_dir: Path) -> int:
    try:
        return int(run_dir.name)
    except ValueError:
        return 0


def _excluded_stub(scenario: Scenario, kind: str, evidence: str, *, repeat: int) -> Score:
    return Score(case=scenario.case, repeat=repeat, surface_observed="undetermined", tool_bearing_calls=0, advisor_calls=0, other_text_calls=0, retried_calls=0, audited_provider_calls=0, floor=scenario.floor.tool_bearing_calls, excess=0, deviations=[], review_rounds=0, recipe_used=False, green=False, red=False, is_valid=None, wrong_shape=False, clean=False, optimal=False, excluded=kind, tokens={"prompt": 0, "completion": 0, "cached_prompt": 0, "unknown_calls": 0}, cost=None, wall_ms=0, exclusion_evidence=evidence)


# ── helpers ────────────────────────────────────────────────────────────────

def _digest(args: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _discovery_key(call: ToolCall) -> tuple[Any, ...]:
    a = call.arguments
    if call.name == "get_plugin_schema":
        return ("schema", a.get("plugin_type"), a.get("plugin_name"))
    if call.name in {"get_pipeline_state", "get_audit_info", "get_expression_grammar", "list_recipes", "list_models", "list_sources", "list_transforms", "list_sinks"}:
        return ("tool", call.name)
    return ("tool", call.name, _digest(a))


def _patch_target(call: ToolCall) -> str:
    a = call.arguments
    return str(a.get("node_id") or a.get("sink_name") or a.get("output_name") or "source")


def _codes_from(content: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not content:
        return ()
    codes: list[str] = []
    for key in ("error_class", "code", "status"):
        v = content.get(key)
        if isinstance(v, str) and v:
            codes.append(v)
    for entry in content.get("errors") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("code"), str):
            codes.append(entry["code"])
    return tuple(dict.fromkeys(codes))


@dataclass(frozen=True)
class _Event:
    turn_seq: int
    row_seq: int
    call: ToolCall
    outcome: str
    content: Mapping[str, Any] | None
    audit_ordinal: int


def _events(capture: Capture, tool_bearing_seqs: list[int]) -> list[_Event]:
    outcomes = tool_outcomes(capture)
    rows_by_id = {r.tool_call_id: r for r in tool_rows(capture)}
    events: list[_Event] = []
    for turn in assistant_turns(capture):
        # audit ordinal = 1-based index of the last tool-bearing audit row at or before this turn
        ordinal = sum(1 for s in tool_bearing_seqs if s <= turn.sequence_no)
        for c in turn.tool_calls:
            row = rows_by_id.get(c.id)
            events.append(_Event(turn.sequence_no, row.sequence_no if row else turn.sequence_no, c, outcomes.get(c.id, "cancelled"), row.content if row else None, ordinal))
    return events


def surface_of(capture: Capture) -> str:
    calls = llm_calls(capture)
    if not calls:
        return "undetermined"
    if any(c.planner_call_ordinal is not None for c in calls) or planner_attempts(capture):
        return "planner"
    return "compose_loop"


# ── the scorer ─────────────────────────────────────────────────────────────

def score_run(capture: Capture, scenario: Scenario) -> Score:  # noqa: C901 — one linear pass, sectioned below
    meta = capture.meta
    binding = ((meta.get("identity") or {}).get("binding") or {})
    advisor_model = binding.get("advisor_model")
    instrument = meta.get("instrument") or {}
    http_steps = meta.get("http") or []
    terminal = meta.get("server_terminal") or {}
    repeat = int(meta.get("repeat") or 0)

    calls = llm_calls(capture)
    tool_bearing = [c for c in calls if c.status == "success" and c.tools_spec_hash]
    advisor = [c for c in calls if c.status == "success" and not c.tools_spec_hash and c.model_requested == advisor_model]
    other_text = [c for c in calls if c.status == "success" and not c.tools_spec_hash and c.model_requested != advisor_model]
    success_seqs = [c.sequence_no for c in calls if c.status == "success"]
    transport = [c for c in calls if c.status in _TRANSPORT_STATUSES]
    retried = [c for c in transport if any(s > c.sequence_no for s in success_seqs)]
    unrecovered = [c for c in transport if c not in retried]

    surface = surface_of(capture)

    tokens = {"prompt": 0, "completion": 0, "cached_prompt": 0, "unknown_calls": 0}
    cost_known = False
    cost = 0.0
    for c in calls:
        if c.prompt_tokens is None or c.completion_tokens is None:
            tokens["unknown_calls"] += 1
        else:
            tokens["prompt"] += c.prompt_tokens
            tokens["completion"] += c.completion_tokens
            tokens["cached_prompt"] += c.cached_prompt_tokens or 0
        if c.provider_cost is not None:
            cost_known = True
            cost += c.provider_cost
    starts = [t for t in (_parse_ts(c.started_at) for c in calls) if t]
    ends = [t for t in (_parse_ts(c.finished_at) for c in calls) if t]
    wall_ms = int((max(ends) - min(starts)).total_seconds() * 1000) if starts and ends else 0

    # ── state / validity / shape ──
    is_valid: bool | None = None
    if isinstance(capture.validate, Mapping) and "is_valid" in capture.validate:
        is_valid = bool(capture.validate["is_valid"])
    state = capture.state if isinstance(capture.state, Mapping) else None
    state_empty = state is None or (not (state.get("sources") or state.get("source")) and not state.get("nodes") and not state.get("outputs"))
    wrong_shape = False
    shape_reason: str | None = None
    if is_valid and state is not None:
        match = topologies_match(topology_from_dict(scenario.expected_topology), topology_from_pipeline(state), option_values=observed_option_values(state), option_assertions=[tuple(a) for a in scenario.option_assertions])
        wrong_shape, shape_reason = (not match.ok), match.reason

    # ── deviations from the tool-call timeline ──
    events = _events(capture, [c.sequence_no for c in tool_bearing])
    deviations: list[Deviation] = []
    seen_discovery: set[tuple[Any, ...]] = set()
    patched: set[tuple[str, str]] = set()
    pending_failed: _Event | None = None
    applied_any = False
    applied_set_pipeline = False
    blob_created = False
    recipe_used = False
    for ev in events:
        name, args = ev.call.name, ev.call.arguments
        digest = _digest(args)
        if name == "apply_pipeline_recipe":
            recipe_used = True
        if is_discovery_tool(name) and name not in _DATA_TOOLS:
            key = _discovery_key(ev.call)
            if key in seen_discovery:
                deviations.append(Deviation("excess_discovery", (ev.turn_seq, ev.row_seq), name, digest, (), ev.audit_ordinal))
            seen_discovery.add(key)
            continue
        if name == "create_blob":
            deviations.append(Deviation("data_rework" if blob_created else "data_setup_detour", (ev.turn_seq, ev.row_seq), name, digest, (), ev.audit_ordinal))
            blob_created = True
            continue
        if name in {"update_blob", "delete_blob"}:
            deviations.append(Deviation("data_rework", (ev.turn_seq, ev.row_seq), name, digest, (), ev.audit_ordinal))
            continue
        if not is_mutation_tool(name):
            continue
        # a mutation
        if pending_failed is not None:
            deviations.append(Deviation("repair", (pending_failed.row_seq, ev.row_seq), pending_failed.call.name, _digest(pending_failed.call.arguments), _codes_from(pending_failed.content), pending_failed.audit_ordinal))
            pending_failed = None
        if ev.outcome in _NOT_APPLIED:
            pending_failed = ev
            continue
        if isinstance(ev.content, Mapping) and ev.content.get("success") is True and ev.content.get("status") == "APPROVAL_REQUIRED":
            deviations.append(Deviation("approval_pending", (ev.turn_seq, ev.row_seq), name, digest, ("APPROVAL_REQUIRED",), ev.audit_ordinal))
            continue
        # applied
        if name in _REMOVAL_TOOLS and applied_any:
            deviations.append(Deviation("backtrack", (_first_applied_seq(events), ev.row_seq), name, digest, (), ev.audit_ordinal))
        elif name == "set_pipeline" and applied_set_pipeline:
            deviations.append(Deviation("backtrack", (_first_applied_seq(events), ev.row_seq), name, digest, (), ev.audit_ordinal))
        if name in _PATCH_TOOLS:
            tkey = (name, _patch_target(ev.call))
            if tkey in patched:
                deviations.append(Deviation("schema_fumble", (ev.turn_seq, ev.row_seq), name, digest, (), ev.audit_ordinal))
            patched.add(tkey)
        applied_any = True
        applied_set_pipeline = applied_set_pipeline or name in {"set_pipeline", "apply_pipeline_recipe"}
        seen_discovery.clear()  # a mutation licenses a fresh read

    # ── audit-row classes ──
    for c in retried:
        deviations.append(Deviation("retried_provider_error", (c.sequence_no, c.sequence_no), None, None, (c.status, c.error_class or ""), None))
    for c in calls:
        if c.status in _MALFORMED_STATUSES:
            deviations.append(Deviation("malformed_output", (c.sequence_no, c.sequence_no), None, None, (c.status,), None))

    # ── terminal classes (from the captured body only) ──
    post = next((h for h in http_steps if h.get("step") == "post_message"), {})
    post_status = post.get("status")
    budget = terminal.get("budget_exhausted")
    if not is_valid:
        if budget in {"composition", "discovery"}:
            deviations.append(Deviation("turn_exhaustion", (0, 0), None, None, (str(budget), str(terminal.get("reason"))), None))
        elif budget == "timeout":
            deviations.append(Deviation("wall_timeout", (0, 0), None, None, (str(terminal.get("reason")),), None))

    # ── red/green (RGR rules reused) ──
    final_text = next((t.content for t in reversed(assistant_turns(capture)) if t.content), "").lower()
    red_reasons: list[str] = []
    phrase_hits = [p for p in PASSIVITY_PHRASES if p in final_text]
    sentinel_hits = [p for p in BUILD_FAILURE_SENTINELS if p in final_text]
    if phrase_hits:
        red_reasons.append(f"forbidden passivity phrases in final message: {phrase_hits}")
    if sentinel_hits:
        red_reasons.append(f"build failure sentinels in final message: {sentinel_hits}")
    if state_empty:
        red_reasons.append("final composition state is null or structurally empty")
    if is_valid is False:
        red_reasons.append("final composition state has is_valid=false")
    if not applied_any and not any(d.cls in {"turn_exhaustion", "wall_timeout", "approval_pending"} for d in deviations):
        deviations.append(Deviation("passivity" if phrase_hits else "decline", (0, 0), None, None, tuple(phrase_hits), None))
    green_reasons: list[str] = []
    gc = scenario.green_criteria
    if gc.get("is_valid", True) and not is_valid:
        green_reasons.append("not is_valid")
    if gc.get("topology_matches_expected", True) and (not is_valid or wrong_shape):
        green_reasons.append(f"topology: {shape_reason or 'no valid state'}")
    if gc.get("must_discover_schema_before_first_mutation") and events:
        first_mut = next((e for e in events if is_mutation_tool(e.call.name) and e.outcome not in _NOT_APPLIED), None)
        if first_mut is not None and not any(e.call.name == "get_plugin_schema" and e.row_seq < first_mut.row_seq for e in events):
            green_reasons.append("no get_plugin_schema before the first applied mutation")
    if wrong_shape:
        deviations.append(Deviation("wrong_shape", (0, 0), None, None, (shape_reason or "",), None))
    green = not green_reasons and not red_reasons
    red = bool(red_reasons)

    # ── floor / excess / unattributed ──
    floor = scenario.floor.tool_bearing_calls
    excess = max(0, len(tool_bearing) - floor)
    path_classes = [d for d in deviations if d.cls != "wrong_shape"]
    if excess > 0 and not path_classes:
        deviations.append(Deviation("unattributed_excess", (0, 0), None, None, (f"excess={excess}",), None))
    deviations.sort(key=lambda d: (d.sequence_no, d.cls))

    # ── exclusions (precedence order) ──
    excluded: str | None = None
    evidence: str | None = None
    if instrument.get("truncated"):
        excluded, evidence = "truncated", "last messages page was full"
    elif instrument.get("read_integrity"):
        excluded, evidence = "read_integrity", str(instrument["read_integrity"])
    elif instrument.get("auth_failed") or post_status in {401, 403}:
        excluded, evidence = "auth", f"post_message status {post_status}"
    elif instrument.get("http_unrecovered"):
        excluded, evidence = "http", str(instrument["http_unrecovered"])
    elif instrument.get("review_rounds_exhausted"):
        excluded, evidence = "http", "interpretation review rounds exhausted (5)"
    elif surface != "compose_loop":
        excluded, evidence = "surface", f"surface_observed={surface}"
    elif unrecovered:
        excluded, evidence = "transport", f"{unrecovered[0].status} at sequence_no {unrecovered[0].sequence_no} with no later successful call"
    elif not tool_bearing:
        excluded, evidence = "no_calls", "zero tool-bearing calls"
    elif not is_valid and post_status != 200 and terminal.get("source", "none") == "none":
        excluded, evidence = "terminal_missing", f"post_message status {post_status} and no server terminal reason"

    clean = excluded is None and not deviations and green and bool(is_valid)
    optimal = clean and len(tool_bearing) == floor
    return Score(
        case=scenario.case, repeat=repeat, surface_observed=surface, tool_bearing_calls=len(tool_bearing), advisor_calls=len(advisor), other_text_calls=len(other_text),
        retried_calls=len(retried), audited_provider_calls=len(calls), floor=floor, excess=excess, deviations=deviations, review_rounds=len(capture.reviews),
        recipe_used=recipe_used, green=green, red=red, is_valid=is_valid, wrong_shape=wrong_shape, clean=clean, optimal=optimal, excluded=excluded,
        tokens=tokens, cost=(cost if cost_known else None), wall_ms=wall_ms, red_reasons=red_reasons, green_reasons=green_reasons, exclusion_evidence=evidence,
    )


def _first_applied_seq(events: list[_Event]) -> int:
    for e in events:
        if is_mutation_tool(e.call.name) and e.outcome not in _NOT_APPLIED:
            return e.row_seq
    return 0


__all__ = ["EXCLUSION_KINDS", "SEVERITY", "Deviation", "Score", "score_from_disk", "score_run", "surface_of", "write_score"]
```

Notes for the implementer:
- `wall_ms` for the ideal fixture is derived from the builder: audit rows at seq 2 (`:02`→`:03`) and seq `4 + schema_calls = 7` (`:07`→`:08`) → `max(finished_at) − min(started_at)` = 6000 ms. If you change `ideal_thread`, re-derive rather than "fix" the assertion.
- Verify at implementation time that `is_discovery_tool("get_pipeline_state")`, `is_discovery_tool("list_models")` and `is_mutation_tool("apply_pipeline_recipe")` are all `True` (`python -c 'from elspeth.web.composer.tools.discovery import *; ...'`); if any differ, the plan's vocabulary test (`test_spec_mutation_vocabulary_is_the_registry_mutation_set`) tells you which — do NOT hand-maintain a mutation set in the scorer.
- `Score.to_dict()` emits `red_reasons`/`green_reasons`/`exclusion_evidence` beyond spec §5's field list — additive, and the report prints them in the ledger.

- [ ] **Step 5: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_battery_score.py -q -n 0`
Expected: all pass (23 tests).

- [ ] **Step 6: Commit**

```bash
git add -- evals/lib/battery_score.py tests/unit/evals/composer_battery/threadgen.py tests/unit/evals/composer_battery/test_battery_score.py
git commit -m "feat(evals): battery scorer — buckets, deviation taxonomy, exclusions" -- evals/lib/battery_score.py tests/unit/evals/composer_battery/threadgen.py tests/unit/evals/composer_battery/test_battery_score.py
```

---

### Task 6: Round report (`evals/lib/battery_report.py` + `evals/composer-battery/report.py`)

**Files:**
- Create: `evals/lib/battery_report.py`
- Create: `evals/composer-battery/report.py` (thin CLI)
- Create: `tests/unit/evals/composer_battery/test_battery_report.py`

**Interfaces:**
- Consumes: Task 5 `Score`, `SEVERITY`, `score_from_disk`, `write_score`; Task 3 `load_corpus`, `SCENARIOS_DIR`; Task 2 `load_scenario`, `Scenario`.
- Run-directory layout (produced by Task 7/8, consumed here):
  - corpus runs `runs/<round>/<case>/<repeat>/` (repeat is a 1-based int dir name);
  - canary pre-flight `runs/<round>/canary/<1..10>/`;
  - tripwire `runs/<round>/_tripwire/<fixture>/1/` plus `runs/<round>/_tripwire/tripwire.json` (written by Task 8; list of `{fixture, pass, staged_variant, planner_calls, planner_codes, surface, reason}`);
  - probe `runs/<round>/_probe/<fixture>/{P,L}/` (never enters a rate; Task 8 reports it separately);
  - `runs/<round>/firing.json` (Task 7 driver ledger: `{"round","base","started_at","completed":[{"case","repeat","status"}],"aborted":bool,"abort_reason":str|null,"case_flags":{case:[reasons]}}`).
- Produces:
  - `class CompareRefused(RuntimeError)`
  - `collect_scores(round_dir: Path, scenarios: Mapping[str, Scenario]) -> tuple[list[Score], list[Score]]` — `(corpus_scores, canary_scores)`; writes `score.json` beside every run; unknown case dirs raise `ValueError`; `_tripwire`/`_probe` are skipped.
  - `ci_half_width_pp(n: int) -> int` — `round(196 * sqrt(0.25 / n))`, `0` when `n == 0`.
  - `build_report(round_dir: Path, *, scenarios, corpus_version: int, compare_to: Path | None = None) -> dict` — the `report.json` document (schema below); raises `CompareRefused` on binding/corpus mismatch.
  - `render_markdown(report: dict) -> str` — `report.md`; every rate line carries `n` and `excluded`; formula string beside pooled rates.
  - `write_report(round_dir: Path, report: dict) -> tuple[Path, Path]` — writes `report.json` and `report.md`.
- `report.json` schema (additive to spec §5): `round, corpus_version, identity{binding,recorded}, caveats[], degraded{flag,reasons[]}, canary{n,non_optimal,flag}, tripwire[], pooled{n,excluded,clean,optimal,hard,clean_rate,optimal_rate,hard_rate,formula,mde_pp}, by_repeat[{repeat,n,excluded,clean,optimal,cached_prompt_tokens_median}], by_case[{case,n,excluded,clean,optimal,histogram{},median_excess,median_review_rounds,per_case_ci_pp,exclusion_streak}], exclusions[{case,repeat,kind,evidence}], ledger[{case,class,severity,events[{repeat,sequence_no,tool,args_digest,codes,audit_ordinal}]}], compare: null | {prev_round, recorded_deltas{key:[prev,cur]}, pooled_delta{clean_pp,optimal_pp,hard_pp}, by_case_delta[{case,clean_pp,indicative:true}]}`.
- Rules: `hard` = runs with ≥1 deviation whose `SEVERITY == "hard"`; pooled counts exclude `excluded` runs from `n` (reported beside); rates are `Σsuccesses/Σn` over included runs; canary never enters `pooled`; `degraded.reasons` include `"canary: >1/10 non-optimal"`, `"exclusions above 15%"`, `"driver aborted: <reason>"`, `"binding identity drifted within round"`; `by_case[].exclusion_streak` is true when two consecutive repeats of that case are excluded.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/evals/composer_battery/test_battery_report.py
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evals.lib.battery_report import CompareRefused, build_report, ci_half_width_pp, collect_scores, render_markdown, write_report
from evals.lib.battery_scenario import load_scenario
from tests.unit.evals.composer_battery import threadgen as tg

REPO = Path(__file__).resolve().parents[4]
SC = load_scenario(REPO / "evals/composer-battery/scenarios/fork_coalesce/scenario.json")
CANARY = load_scenario(REPO / "evals/composer-battery/scenarios/canary/scenario.json")
SCENARIOS = {"fork_coalesce": SC, "canary": CANARY}


def _write_run(run_dir: Path, messages: list[dict], *, state: dict | None, meta: dict, is_valid: bool | None = True) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "messages.json").write_text(json.dumps(messages))
    if state is not None:
        (run_dir / "state.json").write_text(json.dumps(state))
    if is_valid is not None:
        (run_dir / "validate.json").write_text(json.dumps({"is_valid": is_valid, "checks": [], "errors": [], "warnings": [], "readiness": "ready"}))
    (run_dir / "reviews.json").write_text("[]")
    (run_dir / "meta.json").write_text(json.dumps(meta))


def _ideal(case: str, repeat: int, args: dict) -> tuple[list[dict], dict, dict]:
    return tg.ideal_thread(args), copy.deepcopy(args), tg.meta(case=case, repeat=repeat)


def _repair(args: dict) -> list[dict]:
    rows = tg.ideal_thread(args)
    rows[-1]["composition_state_id"] = None
    rows[-1]["content"] = json.dumps({"success": False, "errors": [{"code": "E1"}]})
    rows.append(tg.audit_row(20))
    rows.append(tg.assistant_row(21, [tg.call("sp2", "set_pipeline", args)]))
    rows.append(tg.tool_row(22, "sp2", "as21", state_id="state-3"))
    return rows


def _round(tmp_path: Path, name: str = "r1") -> Path:
    rd = tmp_path / "runs" / name
    fc = SC.canonical_arguments
    ca = CANARY.canonical_arguments
    for rep in (1, 2, 3):
        m, s, meta = _ideal("fork_coalesce", rep, fc)
        _write_run(rd / "fork_coalesce" / str(rep), m, state=s, meta=meta)
    # repeat 4: repair (hard); repeat 5: excluded (surface undetermined — no audit rows)
    _write_run(rd / "fork_coalesce" / "4", _repair(fc), state=copy.deepcopy(fc), meta=tg.meta(case="fork_coalesce", repeat=4))
    _write_run(rd / "fork_coalesce" / "5", [tg.user_row(1), tg.assistant_row(2, content="done")], state=copy.deepcopy(fc), meta=tg.meta(case="fork_coalesce", repeat=5))
    for rep in range(1, 11):
        m, s, meta = _ideal("canary", rep, ca)
        _write_run(rd / "canary" / str(rep), m, state=s, meta=meta)
    (rd / "_tripwire").mkdir()
    (rd / "_tripwire" / "tripwire.json").write_text(json.dumps([{"fixture": "fork_coalesce", "pass": True, "staged_variant": "PIPELINE_STAGED_AUTO_COMMIT", "planner_calls": 4, "planner_codes": {}, "surface": "planner", "reason": None}]))
    (rd / "firing.json").write_text(json.dumps({"round": name, "base": "https://elspeth.foundryside.dev", "started_at": "2026-08-17T00:00:00Z", "completed": [], "aborted": False, "abort_reason": None, "case_flags": {}}))
    return rd


def test_collect_scores_writes_score_json_and_splits_canary(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    corpus, canary = collect_scores(rd, SCENARIOS)
    assert len(corpus) == 5 and len(canary) == 10
    assert (rd / "fork_coalesce/4/score.json").exists() and json.loads((rd / "fork_coalesce/4/score.json").read_text())["deviations"][0]["class"] == "repair"


def test_pooled_uses_sum_over_sum_and_excludes_beside_n(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    rep = build_report(rd, scenarios=SCENARIOS, corpus_version=0)
    assert rep["pooled"] == {"n": 4, "excluded": 1, "clean": 3, "optimal": 3, "hard": 1, "clean_rate": 0.75, "optimal_rate": 0.75, "hard_rate": 0.25, "formula": "sum(successes)/sum(n)", "mde_pp": ci_half_width_pp(4)}
    assert rep["canary"] == {"n": 10, "non_optimal": 0, "flag": False}
    assert rep["exclusions"] == [{"case": "fork_coalesce", "repeat": 5, "kind": "surface", "evidence": "surface_observed=undetermined"}]
    assert rep["tripwire"][0]["fixture"] == "fork_coalesce" and rep["degraded"] == {"flag": False, "reasons": []}


def test_by_case_by_repeat_and_ledger(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    rep = build_report(rd, scenarios=SCENARIOS, corpus_version=0)
    case = next(c for c in rep["by_case"] if c["case"] == "fork_coalesce")
    assert case["n"] == 4 and case["excluded"] == 1 and case["clean"] == 3 and case["histogram"] == {"repair": 1} and case["per_case_ci_pp"] == ci_half_width_pp(4) and case["exclusion_streak"] is False
    assert [r["repeat"] for r in rep["by_repeat"]] == [1, 2, 3, 4, 5]
    assert rep["by_repeat"][4] == {"repeat": 5, "n": 0, "excluded": 1, "clean": 0, "optimal": 0, "cached_prompt_tokens_median": None}
    assert rep["ledger"] == [{"case": "fork_coalesce", "class": "repair", "severity": "hard", "events": [{"repeat": 4, "sequence_no": [9, 22], "tool": "set_pipeline", "args_digest": rep["ledger"][0]["events"][0]["args_digest"], "codes": ["E1"], "audit_ordinal": 2}]}]


def test_degraded_flags_canary_streak_and_exclusion_ratio(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    fc = SC.canonical_arguments
    # make canary 2/10 non-optimal (excess call) and fork_coalesce repeats 4 and 5 both excluded → streak; exclusions 2/5 = 40%
    for rep in (1, 2):
        rows = tg.ideal_thread(CANARY.canonical_arguments)
        rows.insert(2, tg.audit_row(20))
        _write_run(rd / "canary" / str(rep), rows, state=copy.deepcopy(CANARY.canonical_arguments), meta=tg.meta(case="canary", repeat=rep))
    _write_run(rd / "fork_coalesce" / "4", [tg.user_row(1)], state=copy.deepcopy(fc), meta=tg.meta(case="fork_coalesce", repeat=4))
    (rd / "firing.json").write_text(json.dumps({"round": "r1", "base": "x", "started_at": "t", "completed": [], "aborted": True, "abort_reason": "3 consecutive instrument_error", "case_flags": {"fork_coalesce": ["instrument_error streak"]}}))
    rep = build_report(rd, scenarios=SCENARIOS, corpus_version=0)
    assert rep["canary"] == {"n": 10, "non_optimal": 2, "flag": True}
    assert rep["degraded"]["flag"] is True
    assert set(rep["degraded"]["reasons"]) == {"canary: >1/10 non-optimal", "exclusions above 15%", "driver aborted: 3 consecutive instrument_error"}
    assert next(c for c in rep["by_case"] if c["case"] == "fork_coalesce")["exclusion_streak"] is True


def test_compare_refuses_on_binding_mismatch_and_prints_recorded_deltas(tmp_path: Path) -> None:
    prev = _round(tmp_path, "r0")
    write_report(prev, build_report(prev, scenarios=SCENARIOS, corpus_version=0))
    cur = _round(tmp_path, "r1")
    # recorded delta only (skill hash) → allowed and printed
    for meta_path in cur.rglob("meta.json"):
        doc = json.loads(meta_path.read_text())
        doc["identity"]["recorded"]["composer_skill_hash"] = "kit-v2"
        meta_path.write_text(json.dumps(doc))
    rep = build_report(cur, scenarios=SCENARIOS, corpus_version=0, compare_to=prev)
    assert rep["compare"]["prev_round"] == "r0"
    assert rep["compare"]["recorded_deltas"]["composer_skill_hash"] == [None, "kit-v2"]
    assert rep["compare"]["pooled_delta"] == {"clean_pp": 0.0, "optimal_pp": 0.0, "hard_pp": 0.0}
    assert rep["compare"]["by_case_delta"][0]["indicative"] is True
    md = render_markdown(rep)
    assert "composer_skill_hash" in md and "kit-v2" in md
    # binding delta → refused
    for meta_path in cur.rglob("meta.json"):
        doc = json.loads(meta_path.read_text())
        doc["identity"]["binding"]["composer_model"] = "openrouter/other/model"
        meta_path.write_text(json.dumps(doc))
    with pytest.raises(CompareRefused, match="composer_model"):
        build_report(cur, scenarios=SCENARIOS, corpus_version=0, compare_to=prev)
    # corpus_version mismatch → refused
    with pytest.raises(CompareRefused, match="corpus_version"):
        build_report(_round(tmp_path, "r2"), scenarios=SCENARIOS, corpus_version=1, compare_to=prev)


def test_markdown_carries_n_exclusions_and_formula_beside_every_rate(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    md = render_markdown(build_report(rd, scenarios=SCENARIOS, corpus_version=0))
    head = md.split("## Per-repeat")[0]
    assert "n=4" in head and "excluded=1" in head and "sum(successes)/sum(n)" in head
    assert "clean 75.0%" in head and "optimal 75.0%" in head and "hard 25.0%" in head
    assert "## Tripwire" in md and "PIPELINE_STAGED_AUTO_COMMIT" in md
    assert "## Deviation ledger" in md and "repair" in md and "E1" in md
    assert "compose-loop surface only" in md  # caveats header


def test_unknown_case_dir_is_loud(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    (rd / "not_a_case" / "1").mkdir(parents=True)
    with pytest.raises(ValueError, match="not_a_case"):
        collect_scores(rd, SCENARIOS)


def test_ci_half_width() -> None:
    assert ci_half_width_pp(0) == 0 and ci_half_width_pp(5) == 44 and ci_half_width_pp(90) == 10
```

- [ ] **Step 2: Run to verify failure**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_battery_report.py -q -n 0`
Expected: FAIL — module missing. (The `canary/scenario.json` from Task 3 must exist; if Task 3's remaining scenarios are not yet authored, author `canary` first — it is the linear passthrough csv→json payload.)

- [ ] **Step 3: Implement the report library**

```python
# evals/lib/battery_report.py
"""Round aggregation for the composer battery (spec §5). Offline over score.json."""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evals.lib.battery_scenario import Scenario
from evals.lib.battery_score import SEVERITY, Score, score_from_disk, write_score

RESERVED_DIRS = frozenset({"_tripwire", "_probe"})
CAVEATS = ["compose-loop surface only (planner covered by the tripwire table and the §7 probe, never pooled)", "operator-voice register only (prompts that classify EXPLICIT_MUTATION are excluded by construction)", "compose+validate only — no execute", "per-case rates are indicative at N=5 (±~44 pp); claims rest on the pooled aggregate"]
FORMULA = "sum(successes)/sum(n)"


class CompareRefused(RuntimeError):
    pass


def ci_half_width_pp(n: int) -> int:
    return 0 if n <= 0 else round(196 * math.sqrt(0.25 / n))


def _run_dirs(case_dir: Path) -> list[Path]:
    dirs = [d for d in case_dir.iterdir() if d.is_dir() and d.name.isdigit()]
    return sorted(dirs, key=lambda d: int(d.name))


def collect_scores(round_dir: Path, scenarios: Mapping[str, Scenario]) -> tuple[list[Score], list[Score]]:
    corpus: list[Score] = []
    canary: list[Score] = []
    for case_dir in sorted(p for p in Path(round_dir).iterdir() if p.is_dir()):
        if case_dir.name in RESERVED_DIRS:
            continue
        if case_dir.name not in scenarios:
            raise ValueError(f"{case_dir}: no scenario named {case_dir.name!r} — not a battery case")
        for run_dir in _run_dirs(case_dir):
            score = score_from_disk(run_dir, scenarios[case_dir.name])
            write_score(run_dir, score)
            (canary if case_dir.name == "canary" else corpus).append(score)
    return corpus, canary


def _identity(round_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """First run's identity; reasons if binding drifts across runs of the round."""
    first: dict[str, Any] | None = None
    drift: list[str] = []
    for meta_path in sorted(Path(round_dir).rglob("meta.json")):
        if RESERVED_DIRS & set(meta_path.parts):
            continue
        ident = (json.loads(meta_path.read_text()).get("identity") or {})
        if first is None:
            first = ident
        elif (ident.get("binding") or {}) != (first.get("binding") or {}):
            drift.append("binding identity drifted within round")
            break
    return first or {"binding": {}, "recorded": {}}, drift


def _rates(scores: list[Score]) -> dict[str, Any]:
    inc = [s for s in scores if s.excluded is None]
    n = len(inc)
    clean = sum(1 for s in inc if s.clean)
    optimal = sum(1 for s in inc if s.optimal)
    hard = sum(1 for s in inc if any(SEVERITY[d.cls] == "hard" for d in s.deviations))
    return {"n": n, "excluded": len(scores) - n, "clean": clean, "optimal": optimal, "hard": hard, "clean_rate": (clean / n) if n else None, "optimal_rate": (optimal / n) if n else None, "hard_rate": (hard / n) if n else None, "formula": FORMULA, "mde_pp": ci_half_width_pp(n)}


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def build_report(round_dir: Path, *, scenarios: Mapping[str, Scenario], corpus_version: int, compare_to: Path | None = None) -> dict[str, Any]:
    round_dir = Path(round_dir)
    corpus, canary = collect_scores(round_dir, scenarios)
    identity, degraded_reasons = _identity(round_dir)
    firing = json.loads((round_dir / "firing.json").read_text()) if (round_dir / "firing.json").exists() else {}
    tripwire_path = round_dir / "_tripwire" / "tripwire.json"
    tripwire = json.loads(tripwire_path.read_text()) if tripwire_path.exists() else []

    canary_inc = [s for s in canary if s.excluded is None]
    canary_non_optimal = sum(1 for s in canary_inc if not s.optimal) + (len(canary) - len(canary_inc))
    canary_block = {"n": len(canary), "non_optimal": canary_non_optimal, "flag": canary_non_optimal > 1}
    if canary_block["flag"]:
        degraded_reasons.append("canary: >1/10 non-optimal")

    pooled = _rates(corpus)
    if corpus and pooled["excluded"] / len(corpus) > 0.15:
        degraded_reasons.append("exclusions above 15%")
    if firing.get("aborted"):
        degraded_reasons.append(f"driver aborted: {firing.get('abort_reason')}")

    by_repeat: list[dict[str, Any]] = []
    for rep in sorted({s.repeat for s in corpus}):
        rs = [s for s in corpus if s.repeat == rep]
        r = _rates(rs)
        cached = [s.tokens.get("cached_prompt", 0) for s in rs if s.excluded is None]
        by_repeat.append({"repeat": rep, "n": r["n"], "excluded": r["excluded"], "clean": r["clean"], "optimal": r["optimal"], "cached_prompt_tokens_median": _median(cached)})

    by_case: list[dict[str, Any]] = []
    ledger_map: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    exclusions: list[dict[str, Any]] = []
    for case in sorted({s.case for s in corpus}):
        cs = sorted((s for s in corpus if s.case == case), key=lambda s: s.repeat)
        r = _rates(cs)
        hist: Counter[str] = Counter()
        for s in cs:
            if s.excluded is not None:
                exclusions.append({"case": case, "repeat": s.repeat, "kind": s.excluded, "evidence": s.exclusion_evidence})
                continue
            for d in s.deviations:
                hist[d.cls] += 1
                ledger_map[(case, d.cls)].append({"repeat": s.repeat, "sequence_no": list(d.sequence_no), "tool": d.tool, "args_digest": d.args_digest, "codes": list(d.codes), "audit_ordinal": d.audit_ordinal})
        streak = any(a.excluded is not None and b.excluded is not None and b.repeat == a.repeat + 1 for a, b in zip(cs, cs[1:], strict=False))
        inc = [s for s in cs if s.excluded is None]
        by_case.append({"case": case, "n": r["n"], "excluded": r["excluded"], "clean": r["clean"], "optimal": r["optimal"], "histogram": dict(sorted(hist.items())), "median_excess": _median([s.excess for s in inc]), "median_review_rounds": _median([s.review_rounds for s in inc]), "per_case_ci_pp": ci_half_width_pp(r["n"]), "exclusion_streak": streak})
    ledger = [{"case": c, "class": k, "severity": SEVERITY[k], "events": ev} for (c, k), ev in sorted(ledger_map.items())]

    report: dict[str, Any] = {
        "round": round_dir.name, "corpus_version": corpus_version, "identity": identity, "caveats": list(CAVEATS),
        "degraded": {"flag": bool(degraded_reasons), "reasons": degraded_reasons}, "canary": canary_block, "tripwire": tripwire,
        "pooled": pooled, "by_repeat": by_repeat, "by_case": by_case, "exclusions": exclusions, "ledger": ledger, "compare": None,
    }
    if compare_to is not None:
        report["compare"] = _compare(report, Path(compare_to))
    return report


def _compare(report: dict[str, Any], prev_dir: Path) -> dict[str, Any]:
    prev_path = prev_dir / "report.json"
    if not prev_path.exists():
        raise CompareRefused(f"{prev_dir}: no report.json — run report.py on the previous round first")
    prev = json.loads(prev_path.read_text())
    if prev.get("corpus_version") != report["corpus_version"]:
        raise CompareRefused(f"corpus_version differs: prev {prev.get('corpus_version')} vs current {report['corpus_version']}")
    pb, cb = prev.get("identity", {}).get("binding", {}), report["identity"].get("binding", {})
    mismatches = sorted(k for k in set(pb) | set(cb) if pb.get(k) != cb.get(k))
    if mismatches:
        raise CompareRefused("binding identity mismatch on: " + ", ".join(f"{k} ({pb.get(k)!r} → {cb.get(k)!r})" for k in mismatches))
    pr, cr = prev.get("identity", {}).get("recorded", {}), report["identity"].get("recorded", {})
    recorded_deltas = {k: [pr.get(k), cr.get(k)] for k in ["composer_skill_hash", *sorted((set(pr) | set(cr)) - {"composer_skill_hash"})] if pr.get(k) != cr.get(k)}

    def pp(cur: float | None, old: float | None) -> float | None:
        return None if cur is None or old is None else round((cur - old) * 100, 1)

    pooled_delta = {"clean_pp": pp(report["pooled"]["clean_rate"], prev["pooled"].get("clean_rate")), "optimal_pp": pp(report["pooled"]["optimal_rate"], prev["pooled"].get("optimal_rate")), "hard_pp": pp(report["pooled"]["hard_rate"], prev["pooled"].get("hard_rate"))}
    prev_cases = {c["case"]: c for c in prev.get("by_case", [])}
    by_case_delta = []
    for c in report["by_case"]:
        p = prev_cases.get(c["case"])
        cur_rate = c["clean"] / c["n"] if c["n"] else None
        old_rate = (p["clean"] / p["n"]) if p and p["n"] else None
        by_case_delta.append({"case": c["case"], "clean_pp": pp(cur_rate, old_rate), "indicative": True})
    return {"prev_round": prev.get("round"), "recorded_deltas": recorded_deltas, "pooled_delta": pooled_delta, "by_case_delta": by_case_delta}


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def render_markdown(report: dict[str, Any]) -> str:
    p = report["pooled"]
    out: list[str] = [f"# Composer battery — round `{report['round']}` (corpus v{report['corpus_version']})", ""]
    out += ["> Caveats: " + "; ".join(report["caveats"]), ""]
    if report["degraded"]["flag"]:
        out += ["**DEGRADED FIRING:** " + "; ".join(report["degraded"]["reasons"]), ""]
    b, r = report["identity"].get("binding", {}), report["identity"].get("recorded", {})
    out += ["## Identity", "", f"- binding: `{json.dumps(b, sort_keys=True)}`", f"- recorded: composer_skill_hash=`{r.get('composer_skill_hash')}` server_version=`{r.get('server_version')}` first_call_messages_hash=`{r.get('first_call_messages_hash')}`", ""]
    if report.get("compare"):
        c = report["compare"]
        out += [f"## Compare vs `{c['prev_round']}`", "", "Recorded deltas (skill hash first): " + (", ".join(f"{k}: {v[0]!r} → {v[1]!r}" for k, v in c["recorded_deltas"].items()) or "none"), f"Pooled Δ: clean {c['pooled_delta']['clean_pp']} pp, optimal {c['pooled_delta']['optimal_pp']} pp, hard {c['pooled_delta']['hard_pp']} pp", "Per-case Δ (indicative, ±~44 pp at N=5): " + ", ".join(f"{d['case']} {d['clean_pp']}" for d in c["by_case_delta"]), ""]
    out += ["## Headline", "", f"- clean {_pct(p['clean_rate'])} (n={p['n']}, excluded={p['excluded']}, formula {p['formula']}, MDE ±{p['mde_pp']} pp)", f"- optimal {_pct(p['optimal_rate'])} (n={p['n']}, excluded={p['excluded']}, formula {p['formula']})", f"- hard {_pct(p['hard_rate'])} (n={p['n']}, excluded={p['excluded']}, formula {p['formula']})", f"- canary: n={report['canary']['n']} non_optimal={report['canary']['non_optimal']} flag={report['canary']['flag']}", ""]
    out += ["## Tripwire", "", "| fixture | pass | staged_variant | surface | planner_calls | planner_codes | reason |", "| --- | --- | --- | --- | --- | --- | --- |"]
    out += [f"| {t['fixture']} | {t['pass']} | {t.get('staged_variant')} | {t.get('surface')} | {t.get('planner_calls')} | {json.dumps(t.get('planner_codes') or {})} | {t.get('reason')} |" for t in report["tripwire"]] or ["| (none) | | | | | | |"]
    out += ["", "## Per-repeat", "", "| repeat | n | excluded | clean | optimal | cached_prompt_tokens_median |", "| --- | --- | --- | --- | --- | --- |"]
    out += [f"| {x['repeat']} | {x['n']} | {x['excluded']} | {x['clean']} | {x['optimal']} | {x['cached_prompt_tokens_median']} |" for x in report["by_repeat"]]
    out += ["", "## Per-case (indicative)", "", "| case | n | excluded | clean | optimal | ±pp | median_excess | median_review_rounds | histogram | streak |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    out += [f"| {c['case']} | {c['n']} | {c['excluded']} | {c['clean']} | {c['optimal']} | {c['per_case_ci_pp']} | {c['median_excess']} | {c['median_review_rounds']} | {json.dumps(c['histogram'])} | {c['exclusion_streak']} |" for c in report["by_case"]]
    out += ["", "## Exclusions", ""] + ([f"- {e['case']}/{e['repeat']}: `{e['kind']}` — {e['evidence']}" for e in report["exclusions"]] or ["- none"])
    out += ["", "## Deviation ledger", ""]
    for entry in report["ledger"]:
        out.append(f"### {entry['case']} — `{entry['class']}` ({entry['severity']}, {len(entry['events'])} events)")
        out += [f"- repeat {e['repeat']}: seq {e['sequence_no']} tool={e['tool']} digest={e['args_digest']} codes={e['codes']} audit_ordinal={e['audit_ordinal']}" for e in entry["events"]]
        out.append("")
    if not report["ledger"]:
        out.append("- none")
    return "\n".join(out) + "\n"


def write_report(round_dir: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    j = Path(round_dir) / "report.json"
    m = Path(round_dir) / "report.md"
    j.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    m.write_text(render_markdown(report))
    return j, m


__all__ = ["CAVEATS", "CompareRefused", "build_report", "ci_half_width_pp", "collect_scores", "render_markdown", "write_report"]
```

```python
# evals/composer-battery/report.py
"""CLI: score every run of a round offline and write report.json + report.md."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from evals.lib.battery_corpus import SCENARIOS_DIR, load_corpus  # noqa: E402
from evals.lib.battery_report import CompareRefused, build_report, write_report  # noqa: E402
from evals.lib.battery_scenario import load_scenario  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--round", required=True, help="round name under evals/composer-battery/runs/")
    ap.add_argument("--compare", default=None, help="previous round name to diff against (refuses on binding mismatch)")
    ap.add_argument("--runs-dir", default=str(REPO / "evals/composer-battery/runs"))
    ns = ap.parse_args(argv)
    version, _cases = load_corpus()
    scenarios = {p.name: load_scenario(p / "scenario.json") for p in SCENARIOS_DIR.iterdir() if (p / "scenario.json").exists()}
    runs = Path(ns.runs_dir)
    try:
        report = build_report(runs / ns.round, scenarios=scenarios, corpus_version=version, compare_to=(runs / ns.compare) if ns.compare else None)
    except CompareRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    j, m = write_report(runs / ns.round, report)
    print(m.read_text())
    print(f"wrote {j} and {m}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_battery_report.py -q -n 0`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add -- evals/lib/battery_report.py evals/composer-battery/report.py tests/unit/evals/composer_battery/test_battery_report.py
git commit -m "feat(evals): battery round report — pooled Σ/Σ, per-case/repeat, ledger, guarded compare" -- evals/lib/battery_report.py evals/composer-battery/report.py tests/unit/evals/composer_battery/test_battery_report.py
```

---

### Task 7: Live driver (`evals/composer-battery/drive_battery.py`)

**Files:**
- Create: `evals/composer-battery/drive_battery.py`
- Create: `tests/unit/evals/composer_battery/test_drive_battery.py`
- Create: `tests/unit/evals/composer_battery/fake_http.py` (scripted fake client shared with Task 8 tests)

**Interfaces:**
- Consumes: Task 3 `load_corpus`, `SCENARIOS_DIR`, `CorpusCase`; Task 2 `load_scenario`; Task 5 `score_from_disk` (**only** its `.excluded` verdict, for the abort rules — the driver never writes `score.json`); Task 8 `run_tripwire` is injected as a callable (the CLI wires it; tests stub it).
- Produces:
  - `@dataclass HttpResponse(status_code: int, body: Any, text: str = "")`; `class HttpTimeout(Exception)`; `class HttpClient(Protocol)` with `request(method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, timeout: float | None = None) -> HttpResponse` and `set_token(token: str) -> None`.
  - `class RequestsClient(HttpClient)` — `requests.Session`, `Authorization: Bearer`, converts `requests.Timeout` → `HttpTimeout`.
  - `class BatteryAuthError(RuntimeError)`
  - `@dataclass Identity(binding: dict, recorded: dict)`; `read_env_budgets(env_file: Path) -> dict` (`advisor_model`, `composition_turns`, `discovery_turns` from `ELSPETH_WEB__COMPOSER_ADVISOR_MODEL` / `…_MAX_COMPOSITION_TURNS` / `…_MAX_DISCOVERY_TURNS`; missing key ⇒ `ValueError`).
  - `class Battery` with `__init__(client, *, base: str, round_name: str, runs_dir: Path, corpus_version: int, env_budgets: dict, repeats: int = 5, resume: bool = False, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic)`; methods `login(username, password)`, `system_status() -> dict`, `run_prompt(*, label: str, prompt: str, run_dir: Path, case: str, repeat: int, capture_proposals: bool = False) -> str | None` (returns the exclusion verdict or `None`), `fire(cases: Mapping[str, CorpusCase], scenarios: Mapping[str, Scenario], *, tripwire: Callable[[Battery], None] | None, only: set[str] | None = None) -> dict` (returns the `firing.json` doc), `cleanup() -> list[str]`.
  - Constants: `CLIENT_TIMEOUT_S = 620.0`, `PAGE = 500`, `MAX_PAGES = 40`, `MAX_REVIEW_ROUNDS = 5`, `SETTLE_POLLS = 12` (5 s apart), `CANARY_N = 10`.
- Firing order in `fire`: canary N=10 (dir `canary/<1..10>`, skipped if `only` excludes `canary`) → `tripwire(self)` (once; skipped when `only` is set and excludes it? — no: the tripwire always runs unless `--no-tripwire`; keep it simple: it runs whenever `tripwire is not None`) → round-robin: `for repeat in 1..repeats: for case in sorted(cases − {canary})`.
- Abort rules in `fire`: after each corpus run, if the last 3 verdicts are all non-`None` ⇒ `aborted=True, abort_reason="3 consecutive instrument_error"` and return; if a case's last two repeats are non-`None` ⇒ `case_flags[case] += ["instrument_error on two consecutive repeats"]` (continue); if excluded/total > 0.15 after ≥ 10 runs ⇒ `case_flags["_round"] = ["exclusions above 15%"]` (continue; the report renders it).
- Per run (`run_prompt`), in this exact order, every HTTP step appended to `meta.http` as `{"step","status","elapsed_ms","detail"}`:
  1. `POST /api/sessions` `{}` → 201 `{id}` (`create_session`);
  2. `PATCH /api/sessions/{id}` `{"title": label}` (`patch_title`) — **before** any message; then `PATCH /api/sessions/{id}/composer/preferences` `{"trust_mode": "auto_commit", "density_default": "high"}` (`patch_preferences`) — the product defaults, pinned explicitly so every run is comparable (spec §7 prerequisite; the values are recorded in `meta.preferences`);
  3. `POST /api/sessions/{id}/messages` `{"content": prompt}` timeout 620 s (`post_message`); on `HttpTimeout` → `GET /api/sessions/{id}/composer-progress` once (`composer_progress`) → `server_terminal = {budget_exhausted: "timeout" if reason == "convergence_wall_clock_timeout" else None, reason, source: "composer_progress"}`; on 422 → `detail = body["detail"]` if it is a dict (FastAPI wraps the route's dict under `detail`) → `server_terminal = {budget_exhausted: detail.get("budget_exhausted"), reason: detail.get("reason"), source: "422_detail"}`; on 401/403 → `instrument.auth_failed = True`; on any non-200 (incl. timeout) → **settle**: poll `GET …/messages?include_llm_audit=true&limit=500` (`settle`) every 5 s until the row count is equal on two consecutive reads (max `SETTLE_POLLS`);
  4. reviews: up to `MAX_REVIEW_ROUNDS` rounds of `GET /api/sessions/{id}/interpretations?status=pending` (`list_reviews`) → for each `events[i].id` `POST …/interpretations/{eid}/resolve` `{"choice": "accepted_as_drafted"}` (`resolve_review`), appending each payload with `round` to `reviews.json`; stop when a round returns zero pending; if the 5th round still returned pending ⇒ `instrument.review_rounds_exhausted = True`;
  5. `GET /api/sessions/{id}/state` (`get_state`) → `state.json` (404 ⇒ no state; skip validate); `POST /api/sessions/{id}/validate?state_id=<state.id>` (`validate`) → `validate.json`;
  6. paginated `GET /api/sessions/{id}/messages` with params `{include_tool_rows: "true", include_llm_audit: "true", include_raw_content: "true", limit: 500, offset: k*500}` (`get_messages`), identical flags every page, `k` from 0 **while the last page had exactly 500 rows** and `k < MAX_PAGES`; a non-200 page ⇒ `instrument.http_unrecovered = "GET /messages <status> at offset <k*500>"` and stop; a 500 whose body has `error_type == "audit_integrity_error"` ⇒ `instrument.read_integrity = "<detail>"`; if the loop stopped with a full last page (error or `MAX_PAGES`) ⇒ `instrument.truncated = True`; concatenate pages → `messages.json`;
  7. if `capture_proposals`: `GET …/proposals` and `GET …/proposal-events` → `proposals.json` `{"proposals": [...], "events": [...]}` (`get_proposals`, `get_proposal_events`);
  8. `meta.json` per the schema below; then return `score_from_disk(run_dir, scenario).excluded` when a scenario is known for the case (canary/corpus), else `None` (tripwire/probe compute their own verdicts).
- `meta.json` = `{"round","case","repeat","corpus_version","prompt_sha256","session_id","state_id","label","preferences":{"trust_mode","density_default"},"http":[…],"server_terminal":{…},"instrument":{"truncated","read_integrity","http_unrecovered","auth_failed","review_rounds_exhausted"},"identity":{"binding":{"substrate","composer_model","advisor_model","model_returned","composer_timeout_seconds","budgets":{"composition_turns","discovery_turns"},"tools_spec_hash","temperature","seed"},"recorded":{"composer_skill_hash","first_call_messages_hash","server_version","frontend_build"}}}` — `substrate/composer_model/composer_timeout_seconds/frontend_build` from `GET /api/system/status` (fetched once per firing), `advisor_model/budgets` from `read_env_budgets`, `model_returned/tools_spec_hash/temperature/seed` from the **first tool-bearing** audit row of *this* run's `messages.json`, `first_call_messages_hash` from the first audit row, `server_version` from `GET /api/system/status` if it carries `version` else from `GET /api/version` if present else `null`, `composer_skill_hash` = `null` in v1 (recorded field; see spec §4 — a review payload may expose it later).
- `--resume`: a run dir is complete iff `messages.json`, `meta.json`, `reviews.json` exist and parse (`state.json`/`validate.json` may be legitimately absent); complete dirs are skipped and never re-fetched.
- `--cleanup`: `GET /api/sessions` → for each session whose `title` starts with `battery/<round>/` **and** whose run dir is complete (title → `case/repeat` → dir) → `DELETE /api/sessions/{id}`; returns the deleted ids; never deletes anything else.
- `firing.json` written after every run: `{"round","base","started_at","completed":[{"case","repeat","label","session_id","excluded"}],"aborted","abort_reason","case_flags":{}}`.

- [ ] **Step 1: Write the fake HTTP client**

```python
# tests/unit/evals/composer_battery/fake_http.py
"""Scripted HttpClient for driver tests: routes (method, path-prefix) → responder; records every call in order."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from drive_battery import HttpResponse  # resolved via conftest.py sys.path insertion (below)
from tests.unit.evals.composer_battery import threadgen as tg


@dataclass
class Call:
    method: str
    path: str
    json: Any
    params: dict[str, Any] | None
    timeout: float | None


@dataclass
class FakeClient:
    """``responders`` maps ``"METHOD path-prefix"`` → callable(call) → HttpResponse (or raises)."""

    responders: dict[str, Callable[[Call], Any]]
    calls: list[Call] = field(default_factory=list)
    token: str | None = None

    def set_token(self, token: str) -> None:
        self.token = token

    def request(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, timeout: float | None = None) -> Any:
        call = Call(method, path, json, dict(params) if params else None, timeout)
        self.calls.append(call)
        for key, fn in self.responders.items():
            m, prefix = key.split(" ", 1)
            if m == method and path.startswith(prefix):
                return fn(call)
        raise AssertionError(f"unscripted request: {method} {path}")

    def steps(self) -> list[str]:
        return [f"{c.method} {c.path.split('?')[0]}" for c in self.calls]


def ok(body: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(status, body, json.dumps(body))


def happy_responders(messages: list[dict[str, Any]], *, state: dict[str, Any] | None, session_id: str = "s1") -> dict[str, Callable[[Call], Any]]:
    """A session that composes cleanly and returns ``messages`` on the paginated read."""

    def paged(call: Call):
        off = int((call.params or {}).get("offset", 0))
        lim = int((call.params or {}).get("limit", 500))
        return ok(messages[off : off + lim])

    def state_resp(call: Call):
        if state is None:
            return ok({"detail": "no state"}, 404)
        return ok({"id": "state-2", "session_id": session_id, "version": 2, "is_valid": True, "created_at": "2026-08-17T00:00:00Z", **state})

    return {
        "POST /api/auth/login": lambda c: ok({"access_token": "tok", "token_type": "bearer"}),
        "GET /api/system/status": lambda c: ok({"composer_available": True, "composer_model": tg.COMPOSER, "composer_provider": "openrouter", "composer_reason": None, "composer_missing_keys": [], "composer_timeout_seconds": 600.0, "frontend_build": "index-abc.js", "tutorial_ready": True, "tutorial_reason": None, "plugin_policy_readiness": {}}),
        "POST /api/sessions/": lambda c: _session_child(c, messages, session_id),
        "POST /api/sessions": lambda c: ok({"id": session_id, "user_id": "u", "title": "Session — 17 Aug 2026", "created_at": "t", "updated_at": "t", "archived": False}, 201),
        "PATCH /api/sessions/": lambda c: ok({"id": session_id, "user_id": "u", "title": (c.json or {}).get("title"), "created_at": "t", "updated_at": "t", "archived": False}),
        "GET /api/sessions/" + session_id + "/interpretations": lambda c: ok({"events": []}),
        "GET /api/sessions/" + session_id + "/state": state_resp,
        "GET /api/sessions/" + session_id + "/messages": paged,
        "GET /api/sessions/" + session_id + "/proposals": lambda c: ok([]),
        "GET /api/sessions/" + session_id + "/proposal-events": lambda c: ok([]),
        "GET /api/sessions/" + session_id + "/composer-progress": lambda c: ok({"phase": "failed", "headline": "x", "evidence": [], "likely_next": None, "reason": "convergence_wall_clock_timeout", "session_id": session_id, "request_id": None, "updated_at": "t", "inflight_requests": 0}),
        "GET /api/sessions": lambda c: ok([]),
        "DELETE /api/sessions/": lambda c: ok(None, 204),
    }


def _session_child(call: Call, messages: list[dict[str, Any]], session_id: str):
    if call.path.endswith("/messages"):
        return ok({"message": {}, "new_state": None})
    if call.path.endswith("/validate"):
        return ok({"is_valid": True, "checks": [], "errors": [], "warnings": [], "readiness": "ready"})
    if "/interpretations/" in call.path and call.path.endswith("/resolve"):
        return ok({"event": {"id": call.path.split("/")[-2], "status": "accepted_as_drafted"}, "new_state": None})
    raise AssertionError(f"unscripted POST {call.path}")
```

Import note: `drive_battery.py` lives in `evals/composer-battery/` (a hyphenated dir, not a package). Tests import it via `sys.path` insertion in a `conftest.py`:

```python
# tests/unit/evals/composer_battery/conftest.py
import sys
from pathlib import Path

_BATTERY_DIR = Path(__file__).resolve().parents[4] / "evals" / "composer-battery"
if str(_BATTERY_DIR) not in sys.path:
    sys.path.insert(0, str(_BATTERY_DIR))
```

Check first that `tests/unit/evals/composer_parity/` or `composer_rgr/` does not already do this in a way that conflicts (`grep -rn "sys.path" tests/unit/evals/`); if a shared pattern exists, follow it.

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/evals/composer_battery/test_drive_battery.py
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import drive_battery as db
from evals.lib.battery_corpus import CorpusCase
from evals.lib.battery_scenario import load_scenario
from tests.unit.evals.composer_battery import threadgen as tg
from tests.unit.evals.composer_battery.fake_http import FakeClient, happy_responders, ok

REPO = Path(__file__).resolve().parents[4]
SC = load_scenario(REPO / "evals/composer-battery/scenarios/fork_coalesce/scenario.json")
CANARY = load_scenario(REPO / "evals/composer-battery/scenarios/canary/scenario.json")
ARGS = SC.canonical_arguments
ENV = {"advisor_model": tg.ADVISOR, "composition_turns": 30, "discovery_turns": 10}


def _battery(tmp_path: Path, client: FakeClient, **kw) -> db.Battery:
    b = db.Battery(client, base="https://elspeth.foundryside.dev", round_name="r1", runs_dir=tmp_path / "runs", corpus_version=0, env_budgets=ENV, sleep=lambda s: None, **kw)
    b.login("battery_local", "pw")
    return b


def test_login_hard_fails_without_access_token_and_caches_nothing(tmp_path: Path) -> None:
    client = FakeClient({"POST /api/auth/login": lambda c: ok({"detail": "bad credentials"}, 401)})
    b = db.Battery(client, base="x", round_name="r1", runs_dir=tmp_path, corpus_version=0, env_budgets=ENV, sleep=lambda s: None)
    with pytest.raises(db.BatteryAuthError):
        b.login("battery_local", "pw")
    assert client.token is None and not list(tmp_path.rglob("*.json"))


def test_patch_title_precedes_post_message_and_label_format(tmp_path: Path) -> None:
    client = FakeClient(happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS)))
    b = _battery(tmp_path, client)
    verdict = b.run_prompt(label="battery/r1/fork_coalesce/1", prompt="p", run_dir=tmp_path / "runs/r1/fork_coalesce/1", case="fork_coalesce", repeat=1)
    steps = client.steps()
    assert steps.index("PATCH /api/sessions/s1") < steps.index("POST /api/sessions/s1/messages")
    assert client.calls[steps.index("PATCH /api/sessions/s1")].json == {"title": "battery/r1/fork_coalesce/1"}
    assert client.calls[steps.index("POST /api/sessions/s1/messages")].timeout == 620.0
    assert verdict is None
    meta = json.loads((tmp_path / "runs/r1/fork_coalesce/1/meta.json").read_text())
    assert [h["step"] for h in meta["http"]][:4] == ["create_session", "patch_title", "patch_preferences", "post_message"]
    assert meta["preferences"] == {"trust_mode": "auto_commit", "density_default": "high"}
    assert meta["identity"]["binding"]["tools_spec_hash"] == tg.TOOLS_HASH and meta["identity"]["binding"]["advisor_model"] == tg.ADVISOR
    assert meta["identity"]["binding"]["composer_timeout_seconds"] == 600.0 and meta["identity"]["recorded"]["frontend_build"] == "index-abc.js"
    assert meta["identity"]["recorded"]["first_call_messages_hash"] == "mh2"
    assert meta["state_id"] == "state-2" and meta["server_terminal"]["source"] == "none"
    validate_call = client.calls[steps.index("POST /api/sessions/s1/validate")]
    assert validate_call.params == {"state_id": "state-2"}


def test_422_detail_is_captured_as_the_terminal_reason(tmp_path: Path) -> None:
    detail = {"error_type": "convergence", "detail": "x", "turns_used": 40, "budget_exhausted": "composition", "reason": "convergence_composition_budget", "recovery_text": "y"}
    r = happy_responders([tg.user_row(1), tg.audit_row(2)], state=None)
    r["POST /api/sessions/"] = lambda c: ok({"detail": detail}, 422) if c.path.endswith("/messages") else ok({"is_valid": False, "checks": [], "errors": [], "warnings": [], "readiness": "blocked"})
    client = FakeClient(r)
    b = _battery(tmp_path, client)
    b.run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/fork_coalesce/1", case="fork_coalesce", repeat=1)
    meta = json.loads((tmp_path / "runs/r1/fork_coalesce/1/meta.json").read_text())
    post = next(h for h in meta["http"] if h["step"] == "post_message")
    assert post["status"] == 422 and post["detail"]["turns_used"] == 40
    assert meta["server_terminal"] == {"budget_exhausted": "composition", "reason": "convergence_composition_budget", "source": "422_detail"}
    # non-200 ⇒ settle: at least two audit-count reads before the capture read
    assert client.steps().count("GET /api/sessions/s1/messages") >= 3


def test_client_timeout_reads_composer_progress_once(tmp_path: Path) -> None:
    r = happy_responders([tg.user_row(1), tg.audit_row(2)], state=None)

    def timeout_then(c):
        if c.path.endswith("/messages"):
            raise db.HttpTimeout("620s")
        return ok({"is_valid": False, "checks": [], "errors": [], "warnings": [], "readiness": "blocked"})

    r["POST /api/sessions/"] = timeout_then
    client = FakeClient(r)
    b = _battery(tmp_path, client)
    b.run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/fork_coalesce/1", case="fork_coalesce", repeat=1)
    meta = json.loads((tmp_path / "runs/r1/fork_coalesce/1/meta.json").read_text())
    assert client.steps().count("GET /api/sessions/s1/composer-progress") == 1
    assert meta["server_terminal"] == {"budget_exhausted": "timeout", "reason": "convergence_wall_clock_timeout", "source": "composer_progress"}
    post = next(h for h in meta["http"] if h["step"] == "post_message")
    assert post["status"] is None


def test_pagination_full_page_triggers_next_fetch_and_truncation_is_flagged(tmp_path: Path) -> None:
    rows = [tg.user_row(i) for i in range(1, 1004)]  # 1003 rows → pages 500/500/3
    client = FakeClient(happy_responders(rows, state=None))
    b = _battery(tmp_path, client)
    b.run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/1", case="x", repeat=1)
    offsets = [c.params["offset"] for c in client.calls if c.path.endswith("/messages") and c.method == "GET" and c.params and c.params.get("include_tool_rows") == "true"]
    assert offsets == [0, 500, 1000]
    assert len(json.loads((tmp_path / "runs/r1/x/1/messages.json").read_text())) == 1003
    # exactly 500 rows: a full page ALWAYS triggers a follow-up read (which returns 0 rows)
    client2 = FakeClient(happy_responders(rows[:500], state=None))
    _battery(tmp_path, client2).run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/2", case="x", repeat=2)
    offsets2 = [c.params["offset"] for c in client2.calls if c.method == "GET" and c.path.endswith("/messages") and c.params and c.params.get("include_tool_rows") == "true"]
    assert offsets2 == [0, 500]
    assert json.loads((tmp_path / "runs/r1/x/2/meta.json").read_text())["instrument"]["truncated"] is False
    # a page error after a full page ⇒ http_unrecovered AND truncated
    r3 = happy_responders(rows, state=None)
    calls_seen = {"n": 0}

    def flaky(c):
        if c.params and c.params.get("include_tool_rows") == "true":
            calls_seen["n"] += 1
            if calls_seen["n"] == 2:
                return ok({"detail": "boom"}, 502)
        off = int(c.params.get("offset", 0)) if c.params else 0
        return ok(rows[off : off + 500])

    r3["GET /api/sessions/s1/messages"] = flaky
    client3 = FakeClient(r3)
    _battery(tmp_path, client3).run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/3", case="x", repeat=3)
    inst = json.loads((tmp_path / "runs/r1/x/3/meta.json").read_text())["instrument"]
    assert inst["truncated"] is True and inst["http_unrecovered"] == "GET /messages 502 at offset 500"


def test_read_integrity_error_is_recorded(tmp_path: Path) -> None:
    r = happy_responders([tg.user_row(1)], state=None)
    r["GET /api/sessions/s1/messages"] = lambda c: ok({"error_type": "audit_integrity_error", "detail": "ELSPETH stopped before replying because it could not verify this session's audit trail."}, 500)
    client = FakeClient(r)
    _battery(tmp_path, client).run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/1", case="x", repeat=1)
    inst = json.loads((tmp_path / "runs/r1/x/1/meta.json").read_text())["instrument"]
    assert inst["read_integrity"] and "audit trail" in inst["read_integrity"]


def test_review_loop_is_bounded_and_captured(tmp_path: Path) -> None:
    r = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    r["GET /api/sessions/s1/interpretations"] = lambda c: ok({"events": [{"id": "e1", "status": "pending"}]})  # never drains
    client = FakeClient(r)
    _battery(tmp_path, client).run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/1", case="x", repeat=1)
    assert client.steps().count("POST /api/sessions/s1/interpretations/e1/resolve") == 5
    reviews = json.loads((tmp_path / "runs/r1/x/1/reviews.json").read_text())
    assert [rv["round"] for rv in reviews] == [1, 2, 3, 4, 5]
    assert json.loads((tmp_path / "runs/r1/x/1/meta.json").read_text())["instrument"]["review_rounds_exhausted"] is True
    r2 = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    seen = {"n": 0}

    def drains(c):
        seen["n"] += 1
        return ok({"events": [{"id": "e1", "status": "pending"}]}) if seen["n"] == 1 else ok({"events": []})

    r2["GET /api/sessions/s1/interpretations"] = drains
    client2 = FakeClient(r2)
    _battery(tmp_path, client2).run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/2", case="x", repeat=2)
    assert client2.steps().count("POST /api/sessions/s1/interpretations/e1/resolve") == 1
    assert json.loads((tmp_path / "runs/r1/x/2/meta.json").read_text())["instrument"]["review_rounds_exhausted"] is False


def test_fire_order_abort_and_case_flags(tmp_path: Path) -> None:
    client = FakeClient(happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS)))
    b = _battery(tmp_path, client, repeats=2)
    order: list[str] = []
    real = b.run_prompt

    def spy(**kw):
        order.append(kw["label"])
        return real(**kw)

    b.run_prompt = spy  # type: ignore[method-assign]
    tripped: list[str] = []
    cases = {"canary": CorpusCase("canary", "c"), "fork_coalesce": CorpusCase("fork_coalesce", "f"), "boolean_routing": CorpusCase("boolean_routing", "b")}
    scenarios = {"canary": CANARY, "fork_coalesce": SC, "boolean_routing": SC}
    doc = b.fire(cases, scenarios, tripwire=lambda bt: tripped.append("tw"))
    assert order[:10] == [f"battery/r1/canary/{i}" for i in range(1, 11)]
    assert tripped == ["tw"]
    assert order[10:] == ["battery/r1/boolean_routing/1", "battery/r1/fork_coalesce/1", "battery/r1/boolean_routing/2", "battery/r1/fork_coalesce/2"]
    assert doc["aborted"] is False and len(doc["completed"]) == 14
    assert json.loads((tmp_path / "runs/r1/firing.json").read_text())["completed"][-1]["case"] == "fork_coalesce"


def test_fire_aborts_after_three_consecutive_instrument_errors(tmp_path: Path) -> None:
    dead = happy_responders([tg.user_row(1), tg.assistant_row(2, content="hi")], state=None)  # zero audit rows ⇒ surface undetermined
    b = _battery(tmp_path, FakeClient(dead), repeats=5)
    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f"), "boolean_routing": CorpusCase("boolean_routing", "b")}
    doc = b.fire(cases, {"fork_coalesce": SC, "boolean_routing": SC}, tripwire=None, only={"fork_coalesce", "boolean_routing"})
    assert doc["aborted"] is True and doc["abort_reason"] == "3 consecutive instrument_error" and len(doc["completed"]) == 3
    assert all(c["excluded"] == "surface" for c in doc["completed"])


def test_fire_flags_a_case_on_two_consecutive_repeats_but_continues(tmp_path: Path) -> None:
    good = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    dead_msgs = [tg.user_row(1), tg.assistant_row(2, content="hi")]
    counter = {"n": 0}

    def paged(c):
        # boolean_routing sessions are titled with that case; the fake keys on the PATCH title we saw last
        return ok(dead_msgs if b.last_label and "boolean_routing" in b.last_label else tg.ideal_thread(ARGS))

    good["GET /api/sessions/s1/messages"] = paged
    b = _battery(tmp_path, FakeClient(good), repeats=3)
    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f"), "boolean_routing": CorpusCase("boolean_routing", "b")}
    doc = b.fire(cases, {"fork_coalesce": SC, "boolean_routing": SC}, tripwire=None, only=set(cases))
    assert doc["aborted"] is False  # round-robin: never 3 consecutive
    assert doc["case_flags"]["boolean_routing"] == ["instrument_error on two consecutive repeats"]


def test_resume_skips_complete_runs_and_never_refetches(tmp_path: Path) -> None:
    client = FakeClient(happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS)))
    b = _battery(tmp_path, client, repeats=1)
    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f")}
    b.fire(cases, {"fork_coalesce": SC}, tripwire=None, only={"fork_coalesce"})
    n_calls = len(client.calls)
    stamp = (tmp_path / "runs/r1/fork_coalesce/1/messages.json").read_text()
    b2 = _battery(tmp_path, client, repeats=1, resume=True)
    b2.fire(cases, {"fork_coalesce": SC}, tripwire=None, only={"fork_coalesce"})
    assert len(client.calls) == n_calls + 1  # only the login of the second battery
    assert (tmp_path / "runs/r1/fork_coalesce/1/messages.json").read_text() == stamp


def test_cleanup_deletes_only_this_rounds_complete_sessions(tmp_path: Path) -> None:
    r = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    r["GET /api/sessions"] = lambda c: ok([
        {"id": "s1", "user_id": "u", "title": "battery/r1/fork_coalesce/1", "created_at": "t", "updated_at": "t", "archived": False},
        {"id": "s9", "user_id": "u", "title": "battery/r1/fork_coalesce/2", "created_at": "t", "updated_at": "t", "archived": False},  # no capture on disk
        {"id": "s8", "user_id": "u", "title": "battery/r0/fork_coalesce/1", "created_at": "t", "updated_at": "t", "archived": False},  # other round
        {"id": "s7", "user_id": "u", "title": "My real session", "created_at": "t", "updated_at": "t", "archived": False},
    ])
    client = FakeClient(r)
    b = _battery(tmp_path, client, repeats=1)
    b.fire({"fork_coalesce": CorpusCase("fork_coalesce", "f")}, {"fork_coalesce": SC}, tripwire=None, only={"fork_coalesce"})
    assert b.cleanup() == ["s1"]
    assert [c.path for c in client.calls if c.method == "DELETE"] == ["/api/sessions/s1"]


def test_read_env_budgets(tmp_path: Path) -> None:
    env = tmp_path / "web.env"
    env.write_text("# c\nELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS=30\nELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS=10\nELSPETH_WEB__COMPOSER_ADVISOR_MODEL=openrouter/anthropic/claude-opus-4-8\n")
    assert db.read_env_budgets(env) == {"advisor_model": "openrouter/anthropic/claude-opus-4-8", "composition_turns": 30, "discovery_turns": 10}
    env.write_text("ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS=10\n")
    with pytest.raises(ValueError, match="COMPOSER_ADVISOR_MODEL"):
        db.read_env_budgets(env)
```

`test_fire_flags_a_case_on_two_consecutive_repeats_but_continues` relies on `Battery.last_label` (set at the start of `run_prompt`) — include that attribute.

- [ ] **Step 3: Run to verify failure**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_drive_battery.py -q -n 0`
Expected: FAIL — `drive_battery` not importable.

- [ ] **Step 4: Implement the driver**

```python
# evals/composer-battery/drive_battery.py
"""Composer path-quality battery — live driver (spec §4).

Login only (never register). Captures runs into runs/<round>/<case>/<n>/;
never scores for measurement (report.py does) — it consults the scorer's
exclusion verdict only for the abort rules. Compose + validate only; never
/execute.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from evals.lib.battery_corpus import SCENARIOS_DIR, CorpusCase, load_corpus  # noqa: E402
from evals.lib.battery_scenario import Scenario, load_scenario  # noqa: E402
from evals.lib.battery_score import score_from_disk  # noqa: E402

CLIENT_TIMEOUT_S = 620.0
PAGE = 500
MAX_PAGES = 40
MAX_REVIEW_ROUNDS = 5
SETTLE_POLLS = 12
SETTLE_INTERVAL_S = 5.0
CANARY_N = 10
DEFAULT_BASE = "https://elspeth.foundryside.dev"
PINNED_PREFERENCES = {"trust_mode": "auto_commit", "density_default": "high"}  # the product defaults (sessions/models.py), pinned per session for comparability


# ── HTTP seam ──────────────────────────────────────────────────────────────

@dataclass
class HttpResponse:
    status_code: int
    body: Any
    text: str = ""


class HttpTimeout(Exception):
    pass


class HttpClient(Protocol):
    def request(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, timeout: float | None = None) -> HttpResponse: ...
    def set_token(self, token: str) -> None: ...


class RequestsClient:
    def __init__(self, base: str) -> None:
        import requests

        self._requests = requests
        self._base = base.rstrip("/")
        self._s = requests.Session()

    def set_token(self, token: str) -> None:
        self._s.headers["Authorization"] = f"Bearer {token}"

    def request(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, timeout: float | None = None) -> HttpResponse:
        try:
            r = self._s.request(method, self._base + path, json=json, params=dict(params or {}), timeout=timeout or 60.0)
        except self._requests.Timeout as exc:
            raise HttpTimeout(str(exc)) from exc
        try:
            body = r.json() if r.content else None
        except ValueError:
            body = None
        return HttpResponse(r.status_code, body, r.text)


class BatteryAuthError(RuntimeError):
    pass


def read_env_budgets(env_file: Path) -> dict[str, Any]:
    values: dict[str, str] = {}
    for line in Path(env_file).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip()
    out: dict[str, Any] = {}
    for key, name, cast in (("advisor_model", "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL", str), ("composition_turns", "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS", int), ("discovery_turns", "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS", int)):
        if name not in values:
            raise ValueError(f"{env_file}: missing {name} — binding identity would be incomplete")
        out[key] = cast(values[name])
    return out


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def run_dir_is_complete(run_dir: Path) -> bool:
    for name in ("messages.json", "meta.json", "reviews.json"):
        p = run_dir / name
        if not p.exists():
            return False
        try:
            json.loads(p.read_text())
        except ValueError:
            return False
    return True


# ── the driver ─────────────────────────────────────────────────────────────

class Battery:
    def __init__(self, client: HttpClient, *, base: str, round_name: str, runs_dir: Path, corpus_version: int, env_budgets: Mapping[str, Any], repeats: int = 5, resume: bool = False, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.client = client
        self.base = base
        self.round = round_name
        self.runs_dir = Path(runs_dir)
        self.round_dir = self.runs_dir / round_name
        self.corpus_version = corpus_version
        self.env = dict(env_budgets)
        self.repeats = repeats
        self.resume = resume
        self._sleep = sleep
        self._clock = clock
        self._status: dict[str, Any] | None = None
        self.last_label: str | None = None
        self._firing: dict[str, Any] = {"round": round_name, "base": base, "started_at": None, "completed": [], "aborted": False, "abort_reason": None, "case_flags": {}}

    # -- auth / identity --
    def login(self, username: str, password: str) -> None:
        r = self.client.request("POST", "/api/auth/login", json={"username": username, "password": password}, timeout=30)
        token = (r.body or {}).get("access_token") if isinstance(r.body, dict) else None
        if r.status_code != 200 or not token:
            raise BatteryAuthError(f"login failed (HTTP {r.status_code}); refusing to continue — never register from the battery")
        self.client.set_token(token)

    def system_status(self) -> dict[str, Any]:
        if self._status is None:
            r = self.client.request("GET", "/api/system/status", timeout=30)
            if r.status_code != 200 or not isinstance(r.body, dict):
                raise RuntimeError(f"/api/system/status returned {r.status_code}")
            self._status = r.body
        return self._status

    # -- one run --
    def run_prompt(self, *, label: str, prompt: str, run_dir: Path, case: str, repeat: int, capture_proposals: bool = False) -> str | None:
        self.last_label = label
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        http: list[dict[str, Any]] = []
        instrument = {"truncated": False, "read_integrity": None, "http_unrecovered": None, "auth_failed": False, "review_rounds_exhausted": False}
        terminal: dict[str, Any] = {"budget_exhausted": None, "reason": None, "source": "none"}

        def step(name: str, method: str, path: str, **kw: Any) -> HttpResponse | None:
            t0 = self._clock()
            try:
                r = self.client.request(method, path, **kw)
            except HttpTimeout:
                http.append({"step": name, "status": None, "elapsed_ms": int((self._clock() - t0) * 1000), "detail": "client timeout"})
                return None
            http.append({"step": name, "status": r.status_code, "elapsed_ms": int((self._clock() - t0) * 1000), "detail": None})
            return r

        # 1. session
        r = step("create_session", "POST", "/api/sessions", json={}, timeout=30)
        if r is None or r.status_code != 201:
            instrument["http_unrecovered"] = f"POST /api/sessions {r.status_code if r else 'timeout'}"
            self._write_meta(run_dir, case=case, repeat=repeat, label=label, prompt=prompt, session_id=None, state_id=None, http=http, terminal=terminal, instrument=instrument, messages=[])
            return self._verdict(run_dir, case)
        sid = str(r.body["id"])
        # 2. title BEFORE any message — suppresses the unaudited auto-title provider call
        step("patch_title", "PATCH", f"/api/sessions/{sid}", json={"title": label}, timeout=30)
        step("patch_preferences", "PATCH", f"/api/sessions/{sid}/composer/preferences", json=dict(PINNED_PREFERENCES), timeout=30)
        # 3. compose
        r = step("post_message", "POST", f"/api/sessions/{sid}/messages", json={"content": prompt}, timeout=CLIENT_TIMEOUT_S)
        if r is None:
            pr = step("composer_progress", "GET", f"/api/sessions/{sid}/composer-progress", timeout=30)
            reason = pr.body.get("reason") if pr is not None and isinstance(pr.body, dict) else None
            terminal = {"budget_exhausted": "timeout" if reason == "convergence_wall_clock_timeout" else None, "reason": reason, "source": "composer_progress"}
            self._settle(sid, http)
        elif r.status_code != 200:
            detail = r.body.get("detail") if isinstance(r.body, dict) else None
            http[-1]["detail"] = detail
            if r.status_code == 422 and isinstance(detail, dict):
                terminal = {"budget_exhausted": detail.get("budget_exhausted"), "reason": detail.get("reason"), "source": "422_detail"}
            if r.status_code in (401, 403):
                instrument["auth_failed"] = True
            self._settle(sid, http)
        # 4. reviews
        reviews: list[dict[str, Any]] = []
        exhausted = True
        for rnd in range(1, MAX_REVIEW_ROUNDS + 1):
            lr = step("list_reviews", "GET", f"/api/sessions/{sid}/interpretations", params={"status": "pending"}, timeout=30)
            events = (lr.body or {}).get("events", []) if lr is not None and isinstance(lr.body, dict) else []
            if not events:
                exhausted = False
                break
            for ev in events:
                reviews.append({"round": rnd, "event": ev})
                step("resolve_review", "POST", f"/api/sessions/{sid}/interpretations/{ev['id']}/resolve", json={"choice": "accepted_as_drafted"}, timeout=60)
        instrument["review_rounds_exhausted"] = exhausted
        (run_dir / "reviews.json").write_text(json.dumps(reviews, indent=2))
        # 5. state + validate (pinned to state_id)
        state_id: str | None = None
        sr = step("get_state", "GET", f"/api/sessions/{sid}/state", timeout=30)
        if sr is not None and sr.status_code == 200 and isinstance(sr.body, dict):
            (run_dir / "state.json").write_text(json.dumps(sr.body, indent=2))
            state_id = str(sr.body.get("id"))
            vr = step("validate", "POST", f"/api/sessions/{sid}/validate", params={"state_id": state_id}, timeout=120)
            if vr is not None and vr.status_code == 200:
                (run_dir / "validate.json").write_text(json.dumps(vr.body, indent=2))
        # 6. paginated thread capture
        messages: list[dict[str, Any]] = []
        last_full = False
        for k in range(MAX_PAGES):
            params = {"include_tool_rows": "true", "include_llm_audit": "true", "include_raw_content": "true", "limit": PAGE, "offset": k * PAGE}
            pr = step("get_messages", "GET", f"/api/sessions/{sid}/messages", params=params, timeout=120)
            if pr is None or pr.status_code != 200 or not isinstance(pr.body, list):
                status = pr.status_code if pr is not None else "timeout"
                if pr is not None and isinstance(pr.body, dict) and pr.body.get("error_type") == "audit_integrity_error":
                    instrument["read_integrity"] = str(pr.body.get("detail"))
                instrument["http_unrecovered"] = f"GET /messages {status} at offset {k * PAGE}"
                break
            messages.extend(pr.body)
            last_full = len(pr.body) == PAGE
            if not last_full:
                break
        else:
            last_full = True  # MAX_PAGES exhausted with full pages
        instrument["truncated"] = last_full
        (run_dir / "messages.json").write_text(json.dumps(messages, indent=2))
        # 7. proposals (tripwire/probe need them; harmless otherwise)
        if capture_proposals:
            p1 = step("get_proposals", "GET", f"/api/sessions/{sid}/proposals", timeout=30)
            p2 = step("get_proposal_events", "GET", f"/api/sessions/{sid}/proposal-events", timeout=30)
            (run_dir / "proposals.json").write_text(json.dumps({"proposals": p1.body if p1 and p1.status_code == 200 else None, "events": p2.body if p2 and p2.status_code == 200 else None}, indent=2))
        # 8. meta
        self._write_meta(run_dir, case=case, repeat=repeat, label=label, prompt=prompt, session_id=sid, state_id=state_id, http=http, terminal=terminal, instrument=instrument, messages=messages)
        return self._verdict(run_dir, case)

    def _settle(self, sid: str, http: list[dict[str, Any]]) -> None:
        """After a non-200 compose response server writes may still be in flight; wait for the audit-row count to hold across two reads."""
        prev = -1
        for _ in range(SETTLE_POLLS):
            r = self.client.request("GET", f"/api/sessions/{sid}/messages", params={"include_llm_audit": "true", "limit": PAGE}, timeout=60)
            http.append({"step": "settle", "status": r.status_code, "elapsed_ms": 0, "detail": None})
            n = len(r.body) if isinstance(r.body, list) else -2
            if n == prev:
                return
            prev = n
            self._sleep(SETTLE_INTERVAL_S)

    def _identity(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        st = self.system_status()
        first_call = None
        first_tool = None
        for m in messages:
            if m.get("role") != "audit":
                continue
            for env in m.get("tool_calls") or []:
                if isinstance(env, dict) and env.get("_kind") == "llm_call_audit":
                    c = env.get("call") or {}
                    first_call = first_call or c
                    if c.get("tools_spec_hash") and c.get("status") == "success" and first_tool is None:
                        first_tool = c
        ft = first_tool or {}
        return {
            "binding": {"substrate": self.base, "composer_model": st.get("composer_model"), "advisor_model": self.env["advisor_model"], "model_returned": ft.get("model_returned"), "composer_timeout_seconds": st.get("composer_timeout_seconds"), "budgets": {"composition_turns": self.env["composition_turns"], "discovery_turns": self.env["discovery_turns"]}, "tools_spec_hash": ft.get("tools_spec_hash"), "temperature": ft.get("temperature"), "seed": ft.get("seed")},
            "recorded": {"composer_skill_hash": None, "first_call_messages_hash": (first_call or {}).get("messages_hash"), "server_version": st.get("version"), "frontend_build": st.get("frontend_build")},
        }

    def _write_meta(self, run_dir: Path, *, case: str, repeat: int, label: str, prompt: str, session_id: str | None, state_id: str | None, http: list[dict[str, Any]], terminal: dict[str, Any], instrument: dict[str, Any], messages: list[dict[str, Any]]) -> None:
        meta = {"round": self.round, "case": case, "repeat": repeat, "corpus_version": self.corpus_version, "prompt_sha256": _sha(prompt), "session_id": session_id, "state_id": state_id, "label": label, "preferences": dict(PINNED_PREFERENCES), "http": http, "server_terminal": terminal, "instrument": instrument, "identity": self._identity(messages)}
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    def _verdict(self, run_dir: Path, case: str) -> str | None:
        sc_path = SCENARIOS_DIR / case / "scenario.json"
        if not sc_path.exists():
            return None
        return score_from_disk(run_dir, load_scenario(sc_path)).excluded

    # -- the firing --
    def _label(self, case: str, repeat: int) -> str:
        return f"battery/{self.round}/{case}/{repeat}"

    def _record(self, case: str, repeat: int, label: str, excluded: str | None) -> None:
        self._firing["completed"].append({"case": case, "repeat": repeat, "label": label, "session_id": None, "excluded": excluded})
        self.round_dir.mkdir(parents=True, exist_ok=True)
        (self.round_dir / "firing.json").write_text(json.dumps(self._firing, indent=2))

    def _run_or_resume(self, case: str, repeat: int, prompt: str) -> str | None:
        run_dir = self.round_dir / case / str(repeat)
        label = self._label(case, repeat)
        if self.resume and run_dir_is_complete(run_dir):
            return self._verdict(run_dir, case)
        return self.run_prompt(label=label, prompt=prompt, run_dir=run_dir, case=case, repeat=repeat)

    def fire(self, cases: Mapping[str, CorpusCase], scenarios: Mapping[str, Scenario], *, tripwire: Callable[[Battery], None] | None, only: set[str] | None = None) -> dict[str, Any]:
        self._firing["started_at"] = self._firing["started_at"] or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        selected = {n: c for n, c in cases.items() if only is None or n in only}
        if "canary" in selected:
            for rep in range(1, CANARY_N + 1):
                verdict = self._run_or_resume("canary", rep, selected["canary"].prompt)
                self._record("canary", rep, self._label("canary", rep), verdict)
        if tripwire is not None:
            tripwire(self)
        corpus_names = sorted(n for n in selected if n != "canary")
        streak: list[str | None] = []
        per_case: dict[str, list[str | None]] = {n: [] for n in corpus_names}
        total = excluded_n = 0
        for rep in range(1, self.repeats + 1):
            for name in corpus_names:
                verdict = self._run_or_resume(name, rep, selected[name].prompt)
                self._record(name, rep, self._label(name, rep), verdict)
                streak.append(verdict)
                per_case[name].append(verdict)
                total += 1
                excluded_n += verdict is not None
                if len(per_case[name]) >= 2 and per_case[name][-1] is not None and per_case[name][-2] is not None:
                    flags = self._firing["case_flags"].setdefault(name, [])
                    if "instrument_error on two consecutive repeats" not in flags:
                        flags.append("instrument_error on two consecutive repeats")
                if total >= 10 and excluded_n / total > 0.15:
                    self._firing["case_flags"]["_round"] = ["exclusions above 15%"]
                if len(streak) >= 3 and all(v is not None for v in streak[-3:]):
                    self._firing["aborted"] = True
                    self._firing["abort_reason"] = "3 consecutive instrument_error"
                    self._record_flush()
                    return self._firing
        self._record_flush()
        return self._firing

    def _record_flush(self) -> None:
        self.round_dir.mkdir(parents=True, exist_ok=True)
        (self.round_dir / "firing.json").write_text(json.dumps(self._firing, indent=2))

    def cleanup(self) -> list[str]:
        r = self.client.request("GET", "/api/sessions", timeout=60)
        deleted: list[str] = []
        prefix = f"battery/{self.round}/"
        for s in r.body or []:
            title = str(s.get("title") or "")
            if not title.startswith(prefix):
                continue
            rest = title[len(prefix):].split("/")
            if len(rest) != 2 or not run_dir_is_complete(self.round_dir / rest[0] / rest[1]):
                continue
            d = self.client.request("DELETE", f"/api/sessions/{s['id']}", timeout=30)
            if d.status_code in (200, 204):
                deleted.append(str(s["id"]))
        return deleted


# ── CLI ────────────────────────────────────────────────────────────────────

def _load_credentials(state_dir: Path) -> tuple[str, str]:
    p = state_dir / "credentials.json"
    if p.exists():
        if p.stat().st_mode & 0o077:
            raise SystemExit(f"{p}: must be mode 600")
        doc = json.loads(p.read_text())
        return str(doc["username"]), str(doc["password"])
    user = os.environ.get("BATTERY_USERNAME", "battery_local")
    pw = os.environ.get("BATTERY_PASSWORD") or getpass.getpass(f"password for {user}: ")
    return user, pw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--round", required=True)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--cases", default=None, help="comma-separated case names; omit for all")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--probe", action="store_true", help="run the §7 paired planner probe (calibration only)")
    ap.add_argument("--no-tripwire", action="store_true")
    ap.add_argument("--env-file", default=str(REPO / "deploy/elspeth-web.env"))
    ap.add_argument("--state-dir", default=str(Path.home() / ".elspeth-battery"))
    ap.add_argument("--runs-dir", default=str(REPO / "evals/composer-battery/runs"))
    ns = ap.parse_args(argv)

    from planner_probe import run_probe, run_tripwire  # local import: same directory

    version, cases = load_corpus()
    scenarios = {p.name: load_scenario(p / "scenario.json") for p in SCENARIOS_DIR.iterdir() if (p / "scenario.json").exists()}
    user, pw = _load_credentials(Path(ns.state_dir))
    battery = Battery(RequestsClient(ns.base), base=ns.base, round_name=ns.round, runs_dir=Path(ns.runs_dir), corpus_version=version, env_budgets=read_env_budgets(Path(ns.env_file)), repeats=ns.repeats, resume=ns.resume)
    battery.login(user, pw)
    print(json.dumps({k: battery.system_status().get(k) for k in ("composer_model", "composer_timeout_seconds", "frontend_build")}), file=sys.stderr)
    only = set(ns.cases.split(",")) if ns.cases else None
    if ns.probe:
        run_probe(battery)
        return 0
    doc = battery.fire(cases, scenarios, tripwire=None if ns.no_tripwire else run_tripwire, only=only)
    if ns.cleanup:
        print(f"cleanup deleted {len(battery.cleanup())} sessions", file=sys.stderr)
    print(json.dumps({"aborted": doc["aborted"], "abort_reason": doc["abort_reason"], "completed": len(doc["completed"]), "case_flags": doc["case_flags"]}))
    return 1 if doc["aborted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Implementer notes:
- The `_verdict` call re-reads the scenario per run (cheap; ~1 ms) so the driver keeps no scenario cache; it deliberately calls Task 5's `score_from_disk` and discards everything but `.excluded`.
- `_settle` uses the same `GET /messages` route the capture uses (with `include_llm_audit=true`), so the audit-row count is what settles; the `settle` steps land in `meta.http` before `get_messages`. In `test_422_detail_is_captured_as_the_terminal_reason` the count of `GET …/messages` includes 2 settle reads + 1 capture read = 3.
- `test_pagination_*` filters capture reads by `include_tool_rows == "true"` because settle reads share the path.
- Verify `POST /api/sessions` accepts `{}` (`CreateSessionRequest.title` is optional per `web/sessions/schemas.py:93`); if the deployed server requires a body field, pass `{"title": label}` at creation **and** still PATCH (the PATCH is the pinned contract).

- [ ] **Step 5: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_drive_battery.py -q -n 0`
Expected: 13 passed. (`test_fire_order_abort_and_case_flags` asserts 14 completed = 10 canary + 2 cases × 2 repeats.)

- [ ] **Step 6: Commit**

```bash
git add -- evals/composer-battery/drive_battery.py tests/unit/evals/composer_battery/conftest.py tests/unit/evals/composer_battery/fake_http.py tests/unit/evals/composer_battery/test_drive_battery.py
git commit -m "feat(evals): battery live driver — login-only, PATCH-before-POST, terminal capture, paginated read, abort rules" -- evals/composer-battery/drive_battery.py tests/unit/evals/composer_battery/conftest.py tests/unit/evals/composer_battery/fake_http.py tests/unit/evals/composer_battery/test_drive_battery.py
```

---

### Task 8: Planner probe and tripwire (`evals/composer-battery/planner_probe.py`)

**Files:**
- Create: `evals/composer-battery/planner_probe.py`
- Create: `tests/unit/evals/composer_battery/test_planner_probe.py`

**Interfaces:**
- Consumes: Task 7 `Battery.run_prompt(..., capture_proposals=True)`; Task 5 `score_run`, `surface_of`; Task 4 `load_capture`, `llm_calls`, `planner_attempts`, `assistant_turns`, `tool_outcomes`; Task 2 `Scenario`, `Floor`, `topology_to_dict`; Task 1 `topology_from_pipeline`, `topologies_match`; `classify_pipeline_mutation_intent`, `PipelineMutationIntent` (`elspeth.web.composer.no_tool_policy`); the five `PIPELINE_STAGED_*_MESSAGE` constants (`elspeth.web.composer.protocol`); `ComposerPlannerInformationClass` (`elspeth.contracts.composer_planner_audit`).
- Produces:
  - `PARITY_DIR`, `TRIPWIRE_FIXTURES = ("fork_coalesce", "error_routing", "linear_transform")`, `PROBE_FIXTURES` (all ten parity fixture names), `STAGED_VARIANTS: dict[str, str]` (message text → constant name).
  - `load_fixture(name) -> dict` (`intent`, `canonical_arguments`).
  - `class ProbeUnpaired(RuntimeError)`; `classifier_fingerprint() -> str` (sha256 of `no_tool_policy.py` bytes); `assert_pair_routes(intent) -> None` (arm P must be `EXPLICIT_MUTATION`, arm L `"Hi. " + intent` must not — else `ProbeUnpaired`).
  - `required_information(args) -> frozenset[str]` — the pre-registered floor: `{"plugin.schema"}` always, plus `"model.catalog"` when any node/source plugin name starts with `llm` or contains `_llm`; `catalog.selection` is recorded when seen but never required (the loop receives it in the prompt; the planner may skip it).
  - `LOOP_TOOL_TO_INFO: dict[str, str]` — `get_plugin_schema→plugin.schema`, `list_models→model.catalog`, `get_plugin_assistance→plugin.assistance`, `get_expression_grammar→expression.grammar`, `list_sources/list_transforms/list_sinks→catalog.selection`, `get_pipeline_state→pipeline.current`, `list_recipes→recipe.index`, `get_audit_info→audit.info`.
  - `PLANNER_CODE_TRIAGE: dict[str, str]` — `DISCOVERY_NO_GAIN|DISCOVERY_CYCLE→kit_misled_discovery`, `REPAIR_BLIND_REPEAT→error_message_not_actionable`, `*_EXHAUSTED→budget`, `MALFORMED_RESPONSE|PROSE_REPLY|RESPONSE_TRUNCATED→model`, everything else `other`.
  - `@dataclass ArmResult(fixture: str, arm: str, surface: str, surface_ok: bool, information_seen: list[str], floor_missing: list[str], accepted_terminal: bool, planner_calls: int, planner_codes: dict[str, int], triage: dict[str, int], deviations: list[str], staged_variant: str | None, staged_topology_ok: bool | None, clean: bool, tool_bearing_calls: int, reason: str | None)` with `to_dict()`.
  - `scenario_for_fixture(name: str, args: dict) -> Scenario` — a synthetic Scenario (floor 2, expected topology derived) so the loop arm reuses `score_run` unchanged.
  - `staged_topology(run_dir: Path, args: dict) -> tuple[bool | None, str | None]` — reads `state.json` (auto-commit) else the first `pending` proposal in `proposals.json` (`arguments_redacted_json`); `None` when neither exists.
  - `score_arm(run_dir: Path, fixture: str, arm: str) -> ArmResult`; `score_probe_dir(round_dir: Path) -> dict` (10×2 table → `_probe/probe.json` + `probe.md`); `score_tripwire_dir(round_dir: Path) -> list[dict]` (→ `_tripwire/tripwire.json`).
  - `run_probe(battery) -> Path`; `run_tripwire(battery) -> Path` (fires, then scores offline via the two functions above).
- Tripwire pass ⇔ `surface == "planner"` ∧ `staged_variant is not None` ∧ `staged_topology_ok is True`; `undetermined` ⇒ fail with reason `"surface undetermined"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/evals/composer_battery/test_planner_probe.py
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import planner_probe as pp
from elspeth.web.composer.protocol import PIPELINE_STAGED_AUTO_COMMIT_MESSAGE, PIPELINE_STAGED_REVIEW_MESSAGE
from tests.unit.evals.composer_battery import threadgen as tg

FC = pp.load_fixture("fork_coalesce")
ARGS = FC["canonical_arguments"]


def _write(run_dir: Path, messages: list[dict], *, state: dict | None, proposals: dict | None = None, is_valid: bool | None = True, meta: dict | None = None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "messages.json").write_text(json.dumps(messages))
    if state is not None:
        (run_dir / "state.json").write_text(json.dumps(state))
    if is_valid is not None:
        (run_dir / "validate.json").write_text(json.dumps({"is_valid": is_valid, "checks": [], "errors": [], "warnings": [], "readiness": "ready"}))
    if proposals is not None:
        (run_dir / "proposals.json").write_text(json.dumps(proposals))
    (run_dir / "reviews.json").write_text("[]")
    (run_dir / "meta.json").write_text(json.dumps(meta or tg.meta(case="fork_coalesce")))


def _planner_thread(*, accepted: bool = True, info: tuple[str, ...] = ("catalog.selection", "plugin.schema"), codes: tuple[str | None, ...] = (), staged: str = PIPELINE_STAGED_AUTO_COMMIT_MESSAGE) -> list[dict]:
    rows = [tg.user_row(1)]
    seq = 2
    for i, code in enumerate((*codes, None), start=1):
        rows.append(tg.audit_row(seq, planner_ordinal=i))
        seq += 1
        outcome = "accepted" if (code is None and accepted and i == len(codes) + 1) else ("discovery_executed" if code is None else "candidate_rejected")
        rows.append(tg.planner_attempt_row(seq, ordinal=i, phase="discovery" if i == 1 else "candidate", outcome=outcome, planner_code=code, led_to="done" if outcome == "accepted" else "continue", new_information=info if i == 1 else ()))
        seq += 1
    rows.append(tg.assistant_row(seq, content=staged if accepted else "I could not build this."))
    return rows


def test_pair_routing_precondition_and_fingerprint() -> None:
    for name in pp.PROBE_FIXTURES:
        pp.assert_pair_routes(pp.load_fixture(name)["intent"])  # both arms dry-run to their surface offline
    with pytest.raises(pp.ProbeUnpaired):
        pp.assert_pair_routes("I have some products and want to score them")  # never routes to the planner
    assert len(pp.classifier_fingerprint()) == 64
    assert set(pp.TRIPWIRE_FIXTURES) <= set(pp.PROBE_FIXTURES) and len(pp.PROBE_FIXTURES) == 10


def test_required_information_floor() -> None:
    assert pp.required_information(ARGS) == frozenset({"plugin.schema"})
    llm = pp.load_fixture("structured_llm")["canonical_arguments"]
    assert pp.required_information(llm) == frozenset({"plugin.schema", "model.catalog"})


def test_planner_arm_clean_and_triage(tmp_path: Path) -> None:
    _write(tmp_path / "P", _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
    r = pp.score_arm(tmp_path / "P", "fork_coalesce", "P")
    assert r.surface == "planner" and r.surface_ok and r.floor_missing == [] and r.accepted_terminal and r.planner_calls == 1
    assert r.staged_variant == "PIPELINE_STAGED_AUTO_COMMIT_MESSAGE" and r.staged_topology_ok is True and r.clean and r.reason is None
    _write(tmp_path / "P2", _planner_thread(codes=("DISCOVERY_NO_GAIN", "REPAIR_BLIND_REPEAT")), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
    r2 = pp.score_arm(tmp_path / "P2", "fork_coalesce", "P")
    assert r2.planner_calls == 3 and r2.planner_codes == {"DISCOVERY_NO_GAIN": 1, "REPAIR_BLIND_REPEAT": 1}
    assert r2.triage == {"kit_misled_discovery": 1, "error_message_not_actionable": 1} and not r2.clean and r2.accepted_terminal


def test_planner_arm_missing_floor_and_wrong_surface(tmp_path: Path) -> None:
    _write(tmp_path / "P", _planner_thread(info=("catalog.selection",)), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
    r = pp.score_arm(tmp_path / "P", "fork_coalesce", "P")
    assert r.floor_missing == ["plugin.schema"] and not r.clean
    _write(tmp_path / "L", _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
    r2 = pp.score_arm(tmp_path / "L", "fork_coalesce", "L")  # arm L was expected on the loop; the planner answered
    assert r2.surface == "planner" and not r2.surface_ok and not r2.clean and r2.reason == "surface planner != expected compose_loop"


def test_loop_arm_reuses_the_scorer(tmp_path: Path) -> None:
    _write(tmp_path / "L", tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    r = pp.score_arm(tmp_path / "L", "fork_coalesce", "L")
    assert r.surface == "compose_loop" and r.surface_ok and r.information_seen == ["plugin.schema"] and r.floor_missing == []
    assert r.accepted_terminal and r.tool_bearing_calls == 2 and r.deviations == [] and r.clean and r.planner_calls == 0
    _write(tmp_path / "L2", tg.ideal_thread(ARGS, schema_calls=0), state=copy.deepcopy(ARGS))
    r2 = pp.score_arm(tmp_path / "L2", "fork_coalesce", "L")
    assert r2.floor_missing == ["plugin.schema"] and not r2.clean


def test_staged_topology_from_proposal_when_state_absent(tmp_path: Path) -> None:
    proposals = {"proposals": [{"id": "p1", "status": "pending", "tool_name": "set_pipeline", "arguments_redacted_json": copy.deepcopy(ARGS)}], "events": []}
    _write(tmp_path / "P", _planner_thread(staged=PIPELINE_STAGED_REVIEW_MESSAGE), state=None, proposals=proposals, is_valid=None)
    r = pp.score_arm(tmp_path / "P", "fork_coalesce", "P")
    assert r.staged_variant == "PIPELINE_STAGED_REVIEW_MESSAGE" and r.staged_topology_ok is True
    wrong = copy.deepcopy(ARGS)
    wrong["outputs"][0]["plugin"] = "jsonl"
    _write(tmp_path / "P2", _planner_thread(staged=PIPELINE_STAGED_REVIEW_MESSAGE), state=None, proposals={"proposals": [{"id": "p1", "status": "pending", "tool_name": "set_pipeline", "arguments_redacted_json": wrong}], "events": []}, is_valid=None)
    assert pp.score_arm(tmp_path / "P2", "fork_coalesce", "P").staged_topology_ok is False
    _write(tmp_path / "P3", _planner_thread(accepted=False), state=None, proposals={"proposals": [], "events": []}, is_valid=None)
    r3 = pp.score_arm(tmp_path / "P3", "fork_coalesce", "P")
    assert r3.staged_variant is None and r3.staged_topology_ok is None and not r3.accepted_terminal


def test_tripwire_table_pass_fail_and_undetermined(tmp_path: Path) -> None:
    rd = tmp_path / "runs" / "r1"
    _write(rd / "_tripwire" / "fork_coalesce" / "1", _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
    er = pp.load_fixture("error_routing")["canonical_arguments"]
    wrong = copy.deepcopy(er)
    wrong["outputs"][0]["plugin"] = "jsonl"
    _write(rd / "_tripwire" / "error_routing" / "1", _planner_thread(), state={"id": "s", "version": 2, **wrong})
    _write(rd / "_tripwire" / "linear_transform" / "1", [tg.user_row(1), tg.assistant_row(2, content="hello")], state=None, is_valid=None)
    table = pp.score_tripwire_dir(rd)
    by = {t["fixture"]: t for t in table}
    assert by["fork_coalesce"]["pass"] is True and by["fork_coalesce"]["staged_variant"] == "PIPELINE_STAGED_AUTO_COMMIT_MESSAGE" and by["fork_coalesce"]["surface"] == "planner"
    assert by["error_routing"]["pass"] is False and "topology" in by["error_routing"]["reason"]
    assert by["linear_transform"]["pass"] is False and by["linear_transform"]["surface"] == "undetermined" and by["linear_transform"]["reason"] == "surface undetermined"
    assert json.loads((rd / "_tripwire" / "tripwire.json").read_text()) == table


def test_probe_dir_scores_ten_by_two_and_binds_fingerprint(tmp_path: Path) -> None:
    rd = tmp_path / "runs" / "r1"
    for name in pp.PROBE_FIXTURES:
        args = pp.load_fixture(name)["canonical_arguments"]
        _write(rd / "_probe" / name / "P", _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(args)})
        _write(rd / "_probe" / name / "L", tg.ideal_thread(args), state=copy.deepcopy(args))
    doc = pp.score_probe_dir(rd)
    assert doc["classifier_fingerprint"] == pp.classifier_fingerprint() and len(doc["arms"]) == 20
    assert all(a["surface_ok"] for a in doc["arms"])
    assert (rd / "_probe" / "probe.md").exists() and "| fixture | arm |" in (rd / "_probe" / "probe.md").read_text()


def test_run_tripwire_and_probe_use_the_battery_with_proposal_capture(tmp_path: Path) -> None:
    class StubBattery:
        round = "r1"
        round_dir = tmp_path / "runs" / "r1"
        runs_dir = tmp_path / "runs"
        calls: list[dict] = []

        def run_prompt(self, **kw):
            self.calls.append(kw)
            _write(kw["run_dir"], _planner_thread() if not kw["prompt"].startswith("Hi. ") else tg.ideal_thread(ARGS), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
            return None

    b = StubBattery()
    pp.run_tripwire(b)  # type: ignore[arg-type]
    assert [c["label"] for c in b.calls] == [f"battery/r1/_tripwire/{f}/1" for f in pp.TRIPWIRE_FIXTURES]
    assert all(c["capture_proposals"] for c in b.calls) and all(c["case"] == "_tripwire" for c in b.calls)
    assert (tmp_path / "runs/r1/_tripwire/tripwire.json").exists()
    b.calls.clear()
    pp.run_probe(b)  # type: ignore[arg-type]
    assert len(b.calls) == 20 and b.calls[0]["prompt"] == pp.load_fixture(pp.PROBE_FIXTURES[0])["intent"] and b.calls[1]["prompt"].startswith("Hi. ")
    assert (tmp_path / "runs/r1/_probe/probe.json").exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_planner_probe.py -q -n 0`
Expected: FAIL — `planner_probe` not importable.

- [ ] **Step 3: Implement**

```python
# evals/composer-battery/planner_probe.py
"""§7 planner probe (paired, calibration only) and tripwire (standing, every round).

Both reuse the parity fixtures verbatim; scoring is offline over the captured
run directories. Nothing here enters a pooled rate.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from elspeth.web.composer import no_tool_policy  # noqa: E402
from elspeth.web.composer.no_tool_policy import PipelineMutationIntent, classify_pipeline_mutation_intent  # noqa: E402
from elspeth.web.composer.protocol import (  # noqa: E402
    PIPELINE_STAGED_AUTO_COMMIT_MESSAGE,
    PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE,
    PIPELINE_STAGED_REVIEW_MESSAGE,
    PIPELINE_STAGED_REVIEW_PENDING_INTERPRETATION_MESSAGE,
    PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE,
)
from elspeth.web.composer.tools.discovery import is_mutation_tool  # noqa: E402
from evals.lib.battery_capture import assistant_turns, llm_calls, load_capture, planner_attempts, tool_outcomes  # noqa: E402
from evals.lib.battery_scenario import Floor, Scenario, topology_to_dict  # noqa: E402
from evals.lib.battery_score import score_run, surface_of  # noqa: E402
from evals.lib.battery_topology import topologies_match, topology_from_pipeline  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from drive_battery import Battery

PARITY_DIR = REPO / "evals" / "composer-parity" / "fixtures"
TRIPWIRE_FIXTURES: tuple[str, ...] = ("fork_coalesce", "error_routing", "linear_transform")
PROBE_FIXTURES: tuple[str, ...] = tuple(sorted(p.stem for p in PARITY_DIR.glob("*.json")))
STAGED_VARIANTS: dict[str, str] = {
    PIPELINE_STAGED_AUTO_COMMIT_MESSAGE: "PIPELINE_STAGED_AUTO_COMMIT_MESSAGE",
    PIPELINE_STAGED_REVIEW_MESSAGE: "PIPELINE_STAGED_REVIEW_MESSAGE",
    PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE: "PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE",
    PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE: "PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE",
    PIPELINE_STAGED_REVIEW_PENDING_INTERPRETATION_MESSAGE: "PIPELINE_STAGED_REVIEW_PENDING_INTERPRETATION_MESSAGE",
}
LOOP_TOOL_TO_INFO: dict[str, str] = {
    "get_plugin_schema": "plugin.schema", "list_models": "model.catalog", "get_plugin_assistance": "plugin.assistance", "get_expression_grammar": "expression.grammar",
    "list_sources": "catalog.selection", "list_transforms": "catalog.selection", "list_sinks": "catalog.selection", "get_pipeline_state": "pipeline.current",
    "list_recipes": "recipe.index", "get_audit_info": "audit.info",
}
LOOP_PREFIX = "Hi. "


class ProbeUnpaired(RuntimeError):
    pass


def load_fixture(name: str) -> dict[str, Any]:
    doc = json.loads((PARITY_DIR / f"{name}.json").read_text())
    return {"intent": doc["intent"], "canonical_arguments": doc["canonical_arguments"]}


def classifier_fingerprint() -> str:
    return hashlib.sha256(Path(inspect.getsourcefile(no_tool_policy) or "").read_bytes()).hexdigest()


def assert_pair_routes(intent: str) -> None:
    p = classify_pipeline_mutation_intent(intent)
    l_ = classify_pipeline_mutation_intent(LOOP_PREFIX + intent)
    if p is not PipelineMutationIntent.EXPLICIT_MUTATION or l_ is PipelineMutationIntent.EXPLICIT_MUTATION:
        raise ProbeUnpaired(f"pair does not route P→planner / L→loop: P={p.name} L={l_.name} for {intent[:60]!r}")


def required_information(args: dict[str, Any]) -> frozenset[str]:
    plugins = [str((args.get("source") or {}).get("plugin") or "")] + [str(n.get("plugin") or "") for n in args.get("nodes") or []]
    for src in (args.get("sources") or {}).values() if isinstance(args.get("sources"), dict) else []:
        plugins.append(str(src.get("plugin") or ""))
    need = {"plugin.schema"}
    if any(p.startswith("llm") or "_llm" in p for p in plugins):
        need.add("model.catalog")
    return frozenset(need)


def triage_code(code: str) -> str:
    if code in {"DISCOVERY_NO_GAIN", "DISCOVERY_CYCLE"}:
        return "kit_misled_discovery"
    if code == "REPAIR_BLIND_REPEAT":
        return "error_message_not_actionable"
    if code.endswith("_EXHAUSTED"):
        return "budget"
    if code in {"MALFORMED_RESPONSE", "PROSE_REPLY", "RESPONSE_TRUNCATED"}:
        return "model"
    return "other"


@dataclass
class ArmResult:
    fixture: str
    arm: str
    surface: str
    surface_ok: bool
    information_seen: list[str]
    floor_missing: list[str]
    accepted_terminal: bool
    planner_calls: int
    planner_codes: dict[str, int]
    triage: dict[str, int]
    deviations: list[str]
    staged_variant: str | None
    staged_topology_ok: bool | None
    clean: bool
    tool_bearing_calls: int
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def scenario_for_fixture(name: str, args: dict[str, Any]) -> Scenario:
    topo = topology_to_dict(topology_from_pipeline(args))
    return Scenario(case=name, example=None, variant=f"parity:{name}", corpus_version=0, surface_required="compose_loop", classifier_decision="AMBIGUOUS", canonical_arguments=args, expected_topology=topo, option_assertions=[], floor=Floor(tool_bearing_calls=2, components={"discovery": 1, "dependent_listing": 0, "mutation": 1}, repairs=0, backtracks=0, derivation=["parity fixture: one batched discovery + one set_pipeline"], pre_calibration=2, post_calibration=None), red_criteria={"passivity_phrases": "rgr_default"}, green_criteria={"topology_matches_expected": True, "option_assertions_hold": True, "must_discover_schema_before_first_mutation": True, "is_valid": True}, path=PARITY_DIR / f"{name}.json")


def staged_topology(run_dir: Path, args: dict[str, Any]) -> tuple[bool | None, str | None]:
    expected = topology_from_pipeline(args)
    state_p = run_dir / "state.json"
    if state_p.exists():
        state = json.loads(state_p.read_text())
        if isinstance(state, dict) and (state.get("sources") or state.get("source") or state.get("nodes")):
            m = topologies_match(expected, topology_from_pipeline(state))
            return m.ok, m.reason
    prop_p = run_dir / "proposals.json"
    if prop_p.exists():
        doc = json.loads(prop_p.read_text())
        for prop in (doc.get("proposals") or []) if isinstance(doc, dict) else []:
            if isinstance(prop, dict) and prop.get("status") == "pending" and isinstance(prop.get("arguments_redacted_json"), dict):
                m = topologies_match(expected, topology_from_pipeline(prop["arguments_redacted_json"]))
                return m.ok, m.reason
    return None, "no committed state and no pending proposal"


def _staged_variant(capture: Any) -> str | None:
    for turn in reversed(assistant_turns(capture)):
        for text, name in STAGED_VARIANTS.items():
            if text in (turn.content or ""):
                return name
    return None


def score_arm(run_dir: Path, fixture: str, arm: str) -> ArmResult:
    args = load_fixture(fixture)["canonical_arguments"]
    cap = load_capture(Path(run_dir))
    surface = surface_of(cap)
    expected_surface = "planner" if arm == "P" else "compose_loop"
    surface_ok = surface == expected_surface
    reason: str | None = None if surface_ok else (f"surface {surface} != expected {expected_surface}" if surface != "undetermined" else "surface undetermined")
    need = required_information(args)
    calls = llm_calls(cap)
    tool_bearing = sum(1 for c in calls if c.status == "success" and c.tools_spec_hash)
    variant = _staged_variant(cap)
    topo_ok, topo_reason = staged_topology(Path(run_dir), args)

    if surface == "planner":
        attempts = planner_attempts(cap)
        accepted_idx = next((i for i, a in enumerate(attempts) if a.outcome == "accepted" and a.led_to == "done"), None)
        before = attempts if accepted_idx is None else attempts[: accepted_idx + 1]
        seen = sorted({info for a in before for info in a.new_information})
        codes = Counter(a.planner_code for a in attempts if a.planner_code)
        if any(a.repeated_fingerprint for a in attempts):
            codes["repeated_fingerprint"] += 1
        triage = Counter(triage_code(c) for c in codes.elements())
        accepted = accepted_idx is not None
        deviations = sorted(codes)
        return ArmResult(fixture, arm, surface, surface_ok, seen, sorted(need - set(seen)), accepted, sum(1 for c in calls if c.planner_call_ordinal is not None), dict(codes), dict(triage), deviations, variant, topo_ok, surface_ok and not (need - set(seen)) and accepted and not deviations and topo_ok is True, tool_bearing, reason or (None if topo_ok else f"topology: {topo_reason}"))

    # compose loop (or undetermined): reuse the corpus scorer with a synthetic scenario
    score = score_run(cap, scenario_for_fixture(fixture, args))
    outcomes = tool_outcomes(cap)
    seen_set: set[str] = set()
    accepted = False
    for turn in assistant_turns(cap):
        for c in turn.tool_calls:
            if is_mutation_tool(c.name) and outcomes.get(c.id) == "applied":
                accepted = True
                break
            if c.name in LOOP_TOOL_TO_INFO:
                seen_set.add(LOOP_TOOL_TO_INFO[c.name])
        if accepted:
            break
    accepted = accepted and bool(score.is_valid)
    seen = sorted(seen_set)
    deviations = [d.cls for d in score.deviations]
    return ArmResult(fixture, arm, surface, surface_ok, seen, sorted(need - seen_set), accepted, 0, {}, {}, deviations, variant, topo_ok, surface_ok and not (need - seen_set) and accepted and not deviations and score.excluded is None, tool_bearing, reason or (score.excluded and f"excluded: {score.excluded}") or None)


def score_tripwire_dir(round_dir: Path) -> list[dict[str, Any]]:
    tw = Path(round_dir) / "_tripwire"
    table: list[dict[str, Any]] = []
    for fixture in TRIPWIRE_FIXTURES:
        run_dir = tw / fixture / "1"
        if not run_dir.exists():
            table.append({"fixture": fixture, "pass": False, "staged_variant": None, "planner_calls": 0, "planner_codes": {}, "surface": "undetermined", "reason": "not fired"})
            continue
        r = score_arm(run_dir, fixture, "P")
        passed = r.surface == "planner" and r.staged_variant is not None and r.staged_topology_ok is True
        reason = None if passed else (r.reason or ("no PIPELINE_STAGED_* message" if r.staged_variant is None else "topology mismatch"))
        table.append({"fixture": fixture, "pass": passed, "staged_variant": r.staged_variant, "planner_calls": r.planner_calls, "planner_codes": r.planner_codes, "surface": r.surface, "reason": reason})
    tw.mkdir(parents=True, exist_ok=True)
    (tw / "tripwire.json").write_text(json.dumps(table, indent=2))
    return table


def score_probe_dir(round_dir: Path) -> dict[str, Any]:
    pd = Path(round_dir) / "_probe"
    arms: list[dict[str, Any]] = []
    for fixture in PROBE_FIXTURES:
        for arm in ("P", "L"):
            run_dir = pd / fixture / arm
            if run_dir.exists():
                arms.append(score_arm(run_dir, fixture, arm).to_dict())
    doc = {"classifier_fingerprint": classifier_fingerprint(), "rule": "floor = required information classes seen before the accepting mutation + one accepted terminal; surface asserted per arm from artifacts", "arms": arms}
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "probe.json").write_text(json.dumps(doc, indent=2))
    lines = ["# Planner probe (calibration; enters no rate)", "", f"classifier fingerprint `{doc['classifier_fingerprint']}`", "", "| fixture | arm | surface_ok | clean | floor_missing | accepted | tool_bearing | planner_calls | triage | staged | topology_ok | reason |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {a['fixture']} | {a['arm']} | {a['surface_ok']} | {a['clean']} | {','.join(a['floor_missing']) or '-'} | {a['accepted_terminal']} | {a['tool_bearing_calls']} | {a['planner_calls']} | {json.dumps(a['triage'])} | {a['staged_variant']} | {a['staged_topology_ok']} | {a['reason'] or ''} |" for a in arms]
    (pd / "probe.md").write_text("\n".join(lines) + "\n")
    return doc


def run_tripwire(battery: Battery) -> Path:
    for fixture in TRIPWIRE_FIXTURES:
        intent = load_fixture(fixture)["intent"]
        assert_pair_routes(intent)
        battery.run_prompt(label=f"battery/{battery.round}/_tripwire/{fixture}/1", prompt=intent, run_dir=battery.round_dir / "_tripwire" / fixture / "1", case="_tripwire", repeat=1, capture_proposals=True)
    score_tripwire_dir(battery.round_dir)
    return battery.round_dir / "_tripwire" / "tripwire.json"


def run_probe(battery: Battery) -> Path:
    for fixture in PROBE_FIXTURES:
        intent = load_fixture(fixture)["intent"]
        assert_pair_routes(intent)
        for arm, prompt in (("P", intent), ("L", LOOP_PREFIX + intent)):
            battery.run_prompt(label=f"battery/{battery.round}/_probe/{fixture}/{arm}", prompt=prompt, run_dir=battery.round_dir / "_probe" / fixture / arm, case="_probe", repeat=1, capture_proposals=True)
    score_probe_dir(battery.round_dir)
    return battery.round_dir / "_probe" / "probe.json"


__all__ = ["PROBE_FIXTURES", "TRIPWIRE_FIXTURES", "ArmResult", "ProbeUnpaired", "assert_pair_routes", "classifier_fingerprint", "load_fixture", "required_information", "run_probe", "run_tripwire", "scenario_for_fixture", "score_arm", "score_probe_dir", "score_tripwire_dir", "staged_topology"]
```

Implementer notes:
- `Scenario(...)` construction above must match Task 2's dataclass field order/names exactly (`surface_required`, `classifier_decision`, `path`); if Task 2 landed with keyword-only differences, adapt here — do not change Task 2.
- `test_loop_arm_reuses_the_scorer`'s `ideal_thread(ARGS, schema_calls=0)` yields tool-bearing 2 (both audit rows), no discovery → `floor_missing == ["plugin.schema"]`; `score_run` also reports `must_discover_schema_before_first_mutation` in `green_reasons` — consistent.
- `multi_source_queue` has two sources; `topology_from_pipeline` projects the first declared source only, so the tripwire never uses it (it is not in `TRIPWIRE_FIXTURES`); in the probe its `staged_topology_ok` is informational.
- The 10 parity intents were dry-run on 2026-08-17: every `intent` → `EXPLICIT_MUTATION`, every `"Hi. " + intent` → `AMBIGUOUS` (`test_pair_routing_precondition_and_fingerprint` re-pins this against the live grammar).

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/unit/evals/composer_battery/test_planner_probe.py -q -n 0`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add -- evals/composer-battery/planner_probe.py tests/unit/evals/composer_battery/test_planner_probe.py
git commit -m "feat(evals): §7 planner probe (paired, offline-scored) and standing tripwire" -- evals/composer-battery/planner_probe.py tests/unit/evals/composer_battery/test_planner_probe.py
```

---

### Task 9: README runbook, calibration procedure, and freeze

**Files:**
- Modify: `evals/composer-battery/README.md` (replace the Task 3 stub with the full runbook)
- Modify: `evals/composer-battery/corpus.md` and every `scenarios/<case>/scenario.json` (`corpus_version` 0 → 1 **only after** calibration; the last step of this task)
- Create: `evals/composer-battery/calibration/README.md` (the calibration decision record — one line per case with pre/post floor and the decision)

**Interfaces:**
- Consumes: everything above. Produces: no code; the runbook and the calibration record. This task contains the only **live** steps in the plan (they hit `https://elspeth.foundryside.dev` with the `battery_local` account) — they are operator steps, run by John or with his explicit go-ahead; an agent executing this plan writes the README, prepares the commands, and stops before firing unless authorised (public host; project rule).

- [ ] **Step 1: Write the runbook**

```markdown
# Composer path-quality battery

Spec: `docs/superpowers/specs/2026-08-13-composer-battery-design.md` (rev 4).
Plan: `docs/superpowers/plans/2026-08-17-composer-battery.md`.

The battery fires a fixed operator-voice corpus (`corpus.md`, 18 stratified
cases + canary) at the local live composer, captures every run to
`runs/<round>/<case>/<n>/`, and scores **offline** against each case's
pre-registered floor (`scenarios/<case>/scenario.json`) with the §3
deviation taxonomy. Nothing here executes a pipeline; nothing here registers
an account.

## Prerequisites

- `source .venv/bin/activate` in the main checkout.
- `~/.elspeth-battery/credentials.json` (mode 600):
  `{"username": "battery_local", "password": "…"}` — the account must
  already exist on the substrate (`elspeth-web` local auth); the driver
  logs in and **never registers**. `BATTERY_USERNAME`/`BATTERY_PASSWORD`
  env vars work for one-off runs.
- `deploy/elspeth-web.env` present (advisor model and the two turn budgets
  are read from it into the binding identity).
- The substrate is healthy: `curl -s https://elspeth.foundryside.dev/api/system/status | jq .composer_available` → `true`.

## Commands

| Step | Command |
| --- | --- |
| Unit gate (offline) | `pytest tests/unit/evals/composer_battery -q` |
| Dry-run the corpus through the classifier | `python -c 'from evals.lib.battery_corpus import load_corpus; from elspeth.web.composer.no_tool_policy import classify_pipeline_mutation_intent as c; [print(n, c(k.prompt).name) for n,k in load_corpus()[1].items()]'` |
| Fire a round (canary N=10 → tripwire → round-robin) | `python evals/composer-battery/drive_battery.py --round 2026-08-20-baseline --repeats 5` |
| Fire a subset / resume after an interruption | `python evals/composer-battery/drive_battery.py --round <r> --cases fork_coalesce,error_routing --resume` |
| §7 planner probe (calibration only) | `python evals/composer-battery/drive_battery.py --round <r>-calib --probe` |
| Score + report | `python evals/composer-battery/report.py --round <r>` |
| Compare with a previous round | `python evals/composer-battery/report.py --round <r> --compare <prev>` (refuses on binding-identity mismatch; prints recorded deltas, skill hash first) |
| Delete this round's sessions (only complete captures) | `python evals/composer-battery/drive_battery.py --round <r> --cleanup --cases none` |

Exit codes: driver `0` completed, `1` aborted by the instrument rules
(three consecutive `instrument_error`); report `2` on a refused compare.

## Reading a report

`runs/<round>/report.md` — headline (clean / optimal / hard, each with `n`,
exclusions and the Σ/Σ formula), the canary block, the tripwire table (its
own table; never pooled), per-repeat bins, per-case rates (indicative at
N=5), exclusions, then the **deviation ledger** grouped case → class with
evidence (`sequence_no` range, tool, args digest, codes, audit ordinal).
Triage reads the ledger; kit defects become Filigree issues by hand.

Every `score.json` carries `red_reasons`, `green_reasons` and
`exclusion_evidence` so a single run can be read without the report.

## Calibration before freeze (spec §6) — operator procedure

Calibration runs are corpus QA. They enter no rate. Use a round name that
says so (`…-calib`).

1. **Canary at N=10**: `--cases canary` (the canary block runs at N=10 by
   design). Expect ≥ 9/10 optimal; otherwise the instrument, not the corpus,
   is wrong — stop and read the exclusions.
2. **Tripwire**: runs automatically at the start of every round; check
   `runs/<r>/_tripwire/tripwire.json` — all three `pass: true`.
3. **Paired planner probe**: `--probe`. Read `runs/<r>/_probe/probe.md`:
   every arm `surface_ok`; write the reading against the pre-registered rule
   into `calibration/README.md`.
4. **One N=1 pass over the 18 cases**: `--repeats 1 --cases <all but canary>`
   then `report.py`. Check, per case:
   - `surface_observed == compose_loop` (an `instrument_error: surface`
     means the prompt routes to the planner — reword, re-dry-run);
   - advisor rows are on the advisor model with null `tools_spec_hash`
     (`llm_calls` in the capture); `other_text_calls` should be 0;
   - `first_call_messages_hash` stable across two runs of one case
     (fire one case twice with `--repeats 2 --cases <case>`);
   - the floor is reachable: at least one run at floor across calibration,
     else the derivation is wrong — re-derive (structural reason only) and
     record pre/post in `calibration/README.md`;
   - the data path actually taken (`inline_blob` in the `set_pipeline` args
     vs a `create_blob` detour) — record per case; a corpus-wide detour is a
     kit finding, not a floor change;
   - passivity/decline rate as a corpus-QA signal — a prompt that reads as a
     question gets tightened.
5. **Freeze**: bump `corpus_version: 0 → 1` in `corpus.md` and in every
   `scenarios/*/scenario.json` (`floor.post_calibration` filled in), commit
   as one change: `git commit -m "feat(evals): freeze composer battery corpus v1" -- evals/composer-battery`.
   From here any prompt or floor edit is a version bump and a new baseline.

## Operational posture

- Serial; a full 19×5 round runs a few hours. Off-peak; the OpenRouter key
  and `sessions.db` are shared with real use — say so in the round name.
- The per-user composer rate limit is 10/min; the driver's serial cadence
  stays under it.
- Sessions are titled `battery/<round>/<case>/<n>` **before** the prompt is
  posted (suppresses the unaudited auto-title call). `--cleanup` deletes
  only this round's sessions whose capture is complete; default off.
- `runs/` is git-ignored; `report.md` for a round worth keeping is copied
  into `docs/` by hand.

## Layout

| Path | What |
| --- | --- |
| `corpus.md` | verbatim prompts, `corpus_version` |
| `scenarios/<case>/scenario.json` | oracle payload, expected topology, floor + derivation, criteria |
| `drive_battery.py` | live driver (capture only) |
| `planner_probe.py` | §7 probe + tripwire |
| `report.py` | offline scoring + report |
| `calibration/README.md` | calibration decisions (pre/post floors) |
| `../lib/battery_*.py` | tracked libraries (topology, scenario, capture, score, report) |
```

- [ ] **Step 2: Create `calibration/README.md`**

```markdown
# Calibration record

One row per case, appended during the §6 calibration firing. Pre/post
floors are the `floor.pre_calibration` / `floor.post_calibration` values
committed in `scenarios/<case>/scenario.json`.

| case | pre floor | post floor | surface observed | data path observed | decision |
| --- | --- | --- | --- | --- | --- |
| (fill during calibration) | | | | | |

Probe reading (§7): (fill after `--probe`; classifier fingerprint from
`runs/<r>/_probe/probe.json`).
```

- [ ] **Step 3: Commit the runbook (pre-calibration)**

```bash
git add -- evals/composer-battery/README.md evals/composer-battery/calibration/README.md
git commit -m "docs(evals): composer battery runbook and calibration record" -- evals/composer-battery/README.md evals/composer-battery/calibration/README.md
```

- [ ] **Step 4: Calibration firing (OPERATOR — live host)**

Run steps 1–4 of the runbook's calibration procedure. Record every decision in `calibration/README.md`. If a prompt is reworded, re-run the corpus unit tests (the classifier gate is `test_prompt_stays_on_the_compose_loop`).

- [ ] **Step 5: Freeze**

Set `corpus_version: 1` in `corpus.md` and every `scenarios/*/scenario.json`; fill `floor.post_calibration`; run `pytest tests/unit/evals/composer_battery -q` (the version-agreement test pins the bump); commit:

```bash
git add -- evals/composer-battery
git commit -m "feat(evals): freeze composer battery corpus v1 after calibration" -- evals/composer-battery
```

---

### Task 10: Whole-tree gates and merge readiness

**Files:** none new.

- [ ] **Step 1: Full unit suite** — `source .venv/bin/activate && python -m pytest tests/ -q -n 12` (whole-tree AST gates: dynamic-attribute sites, masquerade sites, wire-shape templates; a scoped run proves nothing about them). Expected: green; compare the count against the last known baseline (40,666 passed at `d37a13ead`) — the delta must equal the tests added by Tasks 1–8.
- [ ] **Step 2: Lint gates** — `ruff check evals/lib evals/composer-battery tests/unit/evals/composer_battery && ruff format --check evals/lib evals/composer-battery tests/unit/evals/composer_battery`; then `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth` — the battery adds nothing under `src/`, so the finding corpus must be **unchanged** (diff against a pre-branch run; the gate's non-zero exit is the known fail-closed state).
- [ ] **Step 3: Wardline** — `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only` (exit 0). The battery consumes only its own captured JSON; if a finding lands on `battery_capture.py`/`drive_battery.py`, fix at the boundary (the HTTP client) — never at the scorer.
- [ ] **Step 4: Recent-code-hints** — if any new whole-tree trap was hit while landing Tasks 1–8 (e.g. a `getattr` in the scorer tripping the dynamic-attribute gate), add it to `docs/agents/recent-code-hints.md` in the same commit that fixes it.
- [ ] **Step 5: Hand-off** — no push; report the suite count, the corpus-diff result, and the calibration status (fired / not fired) to John. Filigree: the battery has no ticket yet; if John wants one, file it after the fact against the spec commit rather than blocking on it.

---

## Self-review (writing-plans checklist, run 2026-08-17)

- **Spec coverage.** Decisions 1–10 → Tasks 1 (isomorphism, option assertions), 2 (canonical payload oracle, plural-source extractor cross-check), 3 (corpus, classifier gate, version), 4 (capture, durable pair), 5 (currency, buckets, taxonomy incl. instrument sub-kinds), 6 (Σ/Σ, per-repeat, per-case, ledger, compare refusal, MDE), 7 (login-only, PATCH-before-POST, 422/progress, settle, reviews ≤5, `validate?state_id`, pagination, identity, resume, cleanup, abort rules, firing order), 8 (§7 probe + tripwire + proposals capture + preference pin), 9 (§6 calibration + freeze), 10 (whole-tree gates). §2's second oracle test (server args→state anchor) is added to Task 3. §6 discriminator invariants: advisor `tools=None` and `composer_advisor_model ≠ composer_model` are **existing** server-side facts (`config.py:1020` validator; the advisor call path) — the plan does not add a characterization test for them; if the reviewer wants one it belongs in `tests/unit/web/composer/`, not in the battery package.
- **Deliberate deviations from spec text (flag for review):** (a) the canary's 10 pre-flight runs are reported in their own block and never enter `pooled` (spec §5's illustrative `n: 95` implies 19×5; pooling a trivially-easy canary would inflate the headline); (b) exhausted review rounds map to the `http` exclusion sub-kind with explicit evidence (spec says "instrument_error" without a sub-kind); (c) `auth_error`/`cancelled` audit statuses join `api_error`/`timeout` as transport statuses; (d) `excess_discovery` is reset by any applied mutation (spec wording "no intervening mutation") — a schema re-read after a mutation therefore surfaces as `unattributed_excess`, not silence; (e) `decline` vs `passivity` is split on the RGR phrase list (both hard); (f) preferences are pinned to the product defaults per session (spec §7 "pin the auto-commit preference").
- **Placeholder scan.** No TBD/TODO; every code step is full code; the two "implementer notes" are verification instructions with the exact commands, not deferred work.
- **Type consistency.** `Capture.run_dir` (Task 4) is used by Task 5 (`score_from_disk`) — Task 4's `Capture` must carry `run_dir: Path | None` (it does in the parser code; the interface line lists five fields — add `run_dir`). `Scenario` field names used in Task 8's `scenario_for_fixture` match Task 2's dataclass. `Deviation.cls` serialises as `"class"`. `surface_of` is defined in Task 5 and imported by Task 8. `PlannerAttempt` carries `planner_call_ordinal`/`requested_information`/`rejection_codes`/`repeated_fingerprint` (Task 4, consumed by Task 8). Task 7's `run_prompt` signature (`label, prompt, run_dir, case, repeat, capture_proposals`) matches Task 8's calls and the fake in tests.
