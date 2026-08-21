# WS2 — Config + Build-Time Validation Implementation Plan (Unified Lineage / Barrier Scopes)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the `collectors:`/`scopes:` config surface, the unified `group_bindings` registry, and ALL spec-§7 build-time validation rules (whole-roster fork closure, well-nestedness, bidirectional SESE, rule-5/r28 every-opener-bound, aggregator ban, roster authority, escalate-at-outermost, depth cap with derived fixpoint bound, on_error closer targets) — with runtime-rejection-parity adjudications, composer Stage-1 parity, the composer three-pin, and a canonical-hash pin corpus recorded BEFORE any change.

**Architecture:** Config models in `core/config.py` feed one binding registry (`core/dag/group_bindings.py`) built inside `build_execution_graph`; a new `core/dag/bound_regions.py` computes SESE regions over the built graph and hosts every §7 rejection as `GraphValidationError`. Derived views (`branch_to_coalesce`/`branch_to_row_union`) reproduce today's maps exactly so routing code is untouched. Composer Stage-1 mirrors land in the same commit as each runtime rule.

**Tech Stack:** Python 3.12+, pydantic v2 frozen models, SQLAlchemy-free (this workstream touches no schema), pytest, `scripts/cicd/runtime_rejection_parity.py`, `scripts/cicd/bootstrap_redaction_snapshot.py`.

**Spec:** docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md (rev 3.2 — rulings 1–28 final; §3 config, §7 validation, §6.3 depth cap are this plan's authority)

## Global Constraints

- **Standing procedures:** docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md §S1–§S5 govern fixture freezing, slice gates, casualty retirement, judge-bundle sequencing, and the WS1 STOP rule.
- **Shared checkout, stage by pathspec ONLY.** `git add <explicit paths>` — never `git add -A`, `-u`, or `.`. Commit only your own hunks; a sibling agent can sweep your staged files if you stage broadly.
- **Never bypass hooks** except under the documented `--no-verify`-with-end-of-slice-reconciliation grant; `git stash` is blocked by hook — use commits.
- **Full `pytest tests/` at every slice boundary** (end of Task 3, Task 11, Task 15). Whole-tree AST gates (attribute-contracts, masquerade, runtime-rejection-parity, serialisation-contract) miss scoped runs; a green scoped run proves nothing. Record `git rev-parse HEAD` before AND after the full run; if they differ, re-run rather than diagnose.
- **Trust-tier corpus diff before/after each slice, add NOTHING.** Baseline command (keyless form):
  `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth`
  The gate exits 1 with a large standing corpus by design (elspeth-13f0cc04fb). COUNT findings before and after (never `tail` them); the after-count must not exceed the before-count.
- **Wardline gate** before handing back any slice touching external input:
  `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only`
  (exit 0 = clean and non-inert; 1 = findings or inert gate; 2 = pack/config error).
- **No hand-edited judge signatures, ever.** Never edit a `judge_metadata_signature`, never hand-edit a `key:` in `config/cicd/runtime_rejection_parity.yaml`. New tier-model suppressions are staged key-free via `mcp__elspeth-judge__*`; the operator signs after the campaign settles — do not stage a bundle across this campaign's churn.
- **Depth cap and fixpoint bound rules (spec §6.3, verbatim obligations):** the supported guarantee is **5 layers of bound-region nesting**, builder-enforced fail-closed as `GraphValidationError`, config-overridable (`max_bound_region_depth`) for whoever knowingly accepts the churn; the escalation fixpoint's non-convergence bound is **derived at build from the actual depth (+ margin), never a constant** — today's `MAX_END_OF_INPUT_FLUSH_ITERATIONS = 1_000` must not remain the literal bound once an override-deep build is possible.
- **`src/elspeth/web/composer/state.py` is under active maintainer edit.** Every task that touches it (Tasks 7–11 mirrors, 12, 13) must: `git pull` / re-read the file at execution time, anchor edits by SYMBOL and section comment (never by line number), and put new composer tests in NEW test files — do not edit `tests/unit/web/composer/test_state.py`.
- **Canonical-hash stability is a pinned invariant** (spec §3): no pipeline that passes the new §7 validation may change its canonical hash. Task 1's pin corpus is recorded BEFORE any other task lands and re-asserted at every slice boundary. New optional composer spec fields serialize omitted-when-None (2026-08-20 serialisation-contract discipline).
- **Runtime-rejection-parity gate:** every task that adds a `raise` under `src/elspeth/core/dag/` or `src/elspeth/core/config.py` runs `.venv/bin/python scripts/cicd/runtime_rejection_parity.py --write` and adjudicates every seeded entry IN THAT TASK (the whole-tree gate `tests/unit/scripts/cicd/test_runtime_rejection_parity_gate.py` fails on ANY unadjudicated entry, so an unadjudicated commit turns the branch red for every sibling).
- **Interfaces consumed from WS1** (plan files `docs/superpowers/plans/2026-08-21-unified-lineage-ws1a-model-core.md` — its Task 1 contracts — and `docs/superpowers/plans/2026-08-21-unified-lineage-ws1b-flip-replay-checkpoint.md` — the representation flip): `class FrameKind(StrEnum): FORK = "fork"; EXPAND = "expand"` in `src/elspeth/contracts/enums.py`, and `LineageFrame(kind: FrameKind, group_id: str, member_key: str)` frozen slots dataclass in `contracts/identity.py`. WS2 consumes `FrameKind` and `LineageFrame` (Task 4's `binding_for`). **Entry gate (check before Task 4):** the WS1 checkpoint must be GREEN — `pytest tests/integration/core/dag/test_oracle_freeze.py` passes in compare mode (the frozen-oracle gate), and the protocols plan's **§S5 (WS1 checkpoint and STOP procedure)** is satisfied. If the oracle-freeze suite is red or absent, WS1 has not landed/checkpointed — STOP and surface to the coordinator rather than defining local copies of the contracts.

**Canonical contracts (copy-exact; do not rename):**

```python
LineageFrame(kind: FrameKind, group_id: str, member_key: str)   # frozen slots dataclass (WS1)
FrameKind.FORK | FrameKind.EXPAND                                # contracts/enums.py (WS1)
CloserKind.COALESCE | CloserKind.ROW_UNION | CloserKind.COLLECTOR  # StrEnum, THIS plan's group_bindings.py (Task 4); WS3 compares against the members
GroupLossSpec(closer_name, group_id, member_key, token_id, reason)  # authored WS1a Task 3, consumed by WS3 — named here only for vocabulary
```

**Decisions this plan pins (spec-derived, stated once so every task inherits them):**

1. **SESE walks cover success-path edges ONLY** — `RoutingMode.DIVERT` edges (transform/gate `on_error`, source `__quarantine__`, sink `__failsink__`) are excluded from both the forward and backward walks. Ratified as protocols RC-7. Rationale: every fork-coalesce loss fixture terminates tokens in-region via `on_error: discard`/routed errors, and that IS the settlement system's input; §7 rule 9 treats in-region `on_error` as legal and derivable. Reading rule 4's "before any sink/terminal" as covering DIVERT edges would build-reject all 8 loss fixtures and the loss machinery itself. Task 7 carries RC-7's commissioned build-acceptance test pinning the lost-branch fixtures buildable.
2. **Rule 2 is implemented as written — RATIFIED (spec rev 3.2 correction / protocols RC-2):** a fork with EVERY branch direct-to-sink is "fully unbound (pure fan-out)" and LEGAL. `fork-multiple-terminals-partial-failure` is pure fan-out, NOT a casualty, stays buildable and permanently FROZEN; `parallel-coalesces` is the actual r23 casualty and migrates in Task 6's commit (protocols RC-3).
3. **`CollectorSettings` carries a required `input` connection** — RATIFIED (2026-08-22 synthesis): the spec's §3 YAML omits it, but the DAG needs an in-edge and the aggregation precedent requires one. `on_error` is OPTIONAL (`str | None = None`; None = the route derives from structure per spec §7 rule 9); `on_success` stays required.
4. **Rule 9 scope:** a transform/gate INSIDE a bound region may name that region's closer (any enclosing region's closer, not just the innermost) as `on_error`; the builder wires a DIVERT edge into the closer node (the "route errors into a non-sink" topology `schema_validation.py:1181-1193` explicitly anticipates and defends). Runtime semantics of that edge land in WS3; WS2 builds and validates it. Gate/transform `on_error` omission semantics are UNCHANGED in WS2 — "omitted derives from structure" is realized by WS3's settle-member seam walking the lineage path, which needs no authored edge.
5. **Collector nodes are buildable but not yet runnable.** WS2 lands `NodeType.COLLECTOR` graph nodes; the collector executor is WS4. Until WS4, a run (not merely a build) of a collector-bearing pipeline fails loudly at orchestrator graph registration — acceptable and stated; no silent path exists.
6. **Depth counts BOUND regions only.** An unbound fork (pure fan-out) opens no region and adds no depth. Depth 1 = a bound region with no enclosing bound region.

---

### Task 1: Pre-WS2 canonical-hash pin corpus (record FIRST, before any other change)

**Files:**
- Create: `tests/unit/core/dag/test_canonical_hash_corpus.py`
- Create: `tests/unit/core/dag/canonical_hash_corpus.json` (recorded artifact, committed)

**Interfaces:**
- Consumes: `elspeth.core.config.load_settings(config_path: Path) -> ElspethSettings` (core/config.py:2867); `elspeth.plugins.infrastructure.runtime_factory.instantiate_plugins_from_config(config, *, preflight_mode: bool = False, ...) -> PluginBundle` (runtime_factory.py:78); `ExecutionGraph.from_plugin_instances(...)` (core/dag/graph.py:710); `elspeth.core.canonical.compute_full_topology_hash(graph) -> str` (core/canonical.py:228).
- Produces: `tests/unit/core/dag/canonical_hash_corpus.json` — `{"hashes": {relpath: sha}, "unbuildable": {relpath: exception_class_name}}`; the pin every later task re-asserts.

- [ ] **Step 1: Write the corpus test (record mode + assert mode)**

```python
"""Canonical-hash pin corpus (spec §3 / quality F7).

Records the full topology hash of every buildable ``examples/`` settings
file at pre-WS2 HEAD and asserts them byte-identical thereafter. Spec §3:
"No YAML churn for any pipeline that passes the new §7 validation;
canonical hash of such pipelines does not move."

Regenerate ONLY with an adjudicated reason (a deliberate canonicalization
change is a reviewed decision, never a drive-by re-pin):

    ELSPETH_CANONICAL_CORPUS_RECORD=1 pytest \
        tests/unit/core/dag/test_canonical_hash_corpus.py -x

The unbuildable roster is pinned too: a settings file that stops building
(or starts building) is a corpus change, not a silent skip.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from elspeth.core.canonical import compute_full_topology_hash
from elspeth.core.config import load_settings
from elspeth.core.dag import ExecutionGraph
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EXAMPLES = _REPO_ROOT / "examples"
_PINS_PATH = Path(__file__).parent / "canonical_hash_corpus.json"
_RECORD = os.environ.get("ELSPETH_CANONICAL_CORPUS_RECORD") == "1"


def _settings_files() -> list[Path]:
    return sorted(_EXAMPLES.glob("*/settings*.yaml"))


def _topology_hash(settings_path: Path) -> str:
    config = load_settings(settings_path)
    plugins = instantiate_plugins_from_config(config, preflight_mode=True)
    graph = ExecutionGraph.from_plugin_instances(
        sources=plugins.sources,
        source_settings_map=plugins.source_settings_map,
        transforms=plugins.transforms,
        sinks=plugins.sinks,
        aggregations=plugins.aggregations,
        gates=list(config.gates),
        coalesce_settings=list(config.coalesce) or None,
        queues=config.queues,
        row_union_settings=list(config.row_unions) or None,
    )
    return compute_full_topology_hash(graph)


def _build_corpus() -> dict[str, dict[str, str]]:
    hashes: dict[str, str] = {}
    unbuildable: dict[str, str] = {}
    for path in _settings_files():
        rel = str(path.relative_to(_REPO_ROOT))
        try:
            hashes[rel] = _topology_hash(path)
        except Exception as exc:  # noqa: BLE001 — roster records WHY, test pins the roster
            unbuildable[rel] = type(exc).__name__
    return {"hashes": hashes, "unbuildable": unbuildable}


def test_examples_canonical_hash_corpus_is_pinned() -> None:
    corpus = _build_corpus()
    if _RECORD:
        _PINS_PATH.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n")
        pytest.fail("Corpus recorded to canonical_hash_corpus.json — commit it and re-run without ELSPETH_CANONICAL_CORPUS_RECORD.")
    pinned = json.loads(_PINS_PATH.read_text())
    # Roster first: a moved roster with matching hashes is still a corpus change.
    assert sorted(corpus["unbuildable"]) == sorted(pinned["unbuildable"]), (
        "Unbuildable-example roster moved. A file that stops (or starts) building is a "
        "corpus change requiring adjudication, not a silent skip."
    )
    assert corpus["hashes"] == pinned["hashes"], (
        "Canonical topology hash moved for a pipeline that passes §7 validation — "
        "spec §3 pins these byte-identical across WS2. Diff the two dicts, find the "
        "node whose canonical config changed, and fix the serialization (likely a "
        "key that stopped being omitted-when-None) rather than re-pinning."
    )
```

- [ ] **Step 2: Record the corpus at CURRENT HEAD (before any WS2 code exists)**

Run: `ELSPETH_CANONICAL_CORPUS_RECORD=1 pytest tests/unit/core/dag/test_canonical_hash_corpus.py -x`
Expected: FAIL with "Corpus recorded" (that failure is the record confirmation). Inspect `canonical_hash_corpus.json`: expect the majority of the 65 `examples/*/settings*.yaml` under `hashes`; files needing unset env/credentials at plugin construction land under `unbuildable` with their exception class. Sanity-check that `examples/fork_coalesce/settings.yaml` and `examples/row_union_ab_experiment/settings.yaml` are in `hashes` (they must be — they are the §7-relevant shapes).

- [ ] **Step 3: Run in assert mode to verify green**

Run: `pytest tests/unit/core/dag/test_canonical_hash_corpus.py -v`
Expected: PASS (1 test).

- [ ] **Step 4: Baseline the composer hash pins (no edit — evidence only)**

Run: `pytest tests/unit/web/composer/test_state_serialisation_contract.py -q`
Expected: PASS. These pinned `composition_content_hash` values are the composer half of the §3 corpus; they must still pass untouched at every slice boundary.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/core/dag/test_canonical_hash_corpus.py tests/unit/core/dag/canonical_hash_corpus.json
git commit -m "test(dag): pin pre-WS2 canonical topology hashes for the examples corpus (spec §3 / F7)"
```

---

### Task 2: `CollectorSettings` + `ScopeSettings` config models and `ElspethSettings` fields

**Files:**
- Modify: `src/elspeth/core/config.py` — insert `CollectorSettings` and `ScopeSettings` after `RowUnionSettings` (after line ~1286, before `QueueSettings`); extend `ElspethSettings` (fields block after `row_unions` at :1963-1967; new model_validator after `_validate_default_llm_profile_alias`)
- Modify: `config/cicd/runtime_rejection_parity.yaml` — via `--write`, adjudicate seeded entries
- Test: `tests/unit/core/test_config_collectors_scopes.py` (new)

**Interfaces:**
- Consumes: house helpers already in `core/config.py`: `_validate_max_length`, `_validate_node_name_chars`, `_validate_connection_or_sink_name`, `_RESERVED_EDGE_LABELS`, `_MAX_NODE_NAME_LENGTH`.
- Produces:
  - `class CollectorSettings(BaseModel)` — fields `name: str`, `plugin: str`, `input: str`, `on_success: str`, `on_error: str | None = None` (None = derives from structure, spec §7 rule 9), `options: dict[str, Any]`. This is the ONE authored copy of the settings shape (2026-08-22 synthesis): WS4 Task 1 consumes-and-verifies it, never re-authors it.
  - `class ScopeSettings(BaseModel)` — fields `name: str`, `opener: str`, `closer: str`, `policy: Literal["require_all", "best_effort"]` (REQUIRED, no default), `on_group_failure: Literal["quarantine", "escalate"] = "quarantine"`.
  - `ElspethSettings.collectors: list[CollectorSettings]`, `ElspethSettings.scopes: list[ScopeSettings]`, `ElspethSettings.max_bound_region_depth: int = 5`.

- [ ] **Step 1: Write the failing tests**

```python
"""Config-surface tests for collectors:/scopes: (barrier-scopes spec §3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from elspeth.core.config import (
    CollectorSettings,
    ElspethSettings,
    ScopeSettings,
    load_settings_from_config_dict,
)

_MINIMAL = {
    "sources": {"main": {"plugin": "csv", "options": {"path": "in.csv", "schema": {"mode": "observed"}}, "on_success": "rows"}},
    "sinks": {"out": {"plugin": "json", "options": {"path": "out.json"}, "on_write_failure": "discard"}},
    "transforms": [
        {"name": "explode", "plugin": "json_explode", "input": "rows", "on_success": "pages", "on_error": "discard",
         "options": {"field": "items", "schema": {"mode": "observed"}}},
    ],
}


def _with_collector_scope(**scope_overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        **_MINIMAL,
        "collectors": [
            {"name": "page_stitcher", "plugin": "batch_stats", "input": "pages",
             "on_success": "out", "on_error": "discard", "options": {"schema": {"mode": "observed"}}},
        ],
        "scopes": [
            {"name": "document_pages", "opener": "explode", "closer": "page_stitcher",
             "policy": "require_all", **scope_overrides},
        ],
    }
    return doc


class TestCollectorSettings:
    def test_valid_collector_parses(self) -> None:
        c = CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="pages",
                              on_success="assembled_out", on_error="discard")
        assert c.name == "page_stitcher"
        assert c.options == {}

    def test_collector_has_no_trigger_field(self) -> None:
        # Closers flush on end_of_group ONLY (spec §5): a trigger key is extra=forbid rejected.
        with pytest.raises(ValidationError, match="trigger"):
            CollectorSettings(name="c", plugin="p", input="i", on_success="o",
                              on_error="discard", trigger={"count": 5})

    def test_collector_name_reserved_rejected(self) -> None:
        with pytest.raises(ValidationError, match="reserved"):
            CollectorSettings(name="continue", plugin="p", input="i", on_success="o", on_error="discard")

    def test_collector_on_error_defaults_to_none_derives_from_structure(self) -> None:
        # 2026-08-22 synthesis: on_error is optional; None = the route derives
        # from structure (spec §7 rule 9) — losses settle through the scope's
        # group machinery, realized by WS3/WS4.
        c = CollectorSettings(name="c", plugin="p", input="i", on_success="o")
        assert c.on_error is None

    def test_collector_on_error_must_be_sink_or_discard_shaped_when_given(self) -> None:
        with pytest.raises(ValidationError, match="on_error"):
            CollectorSettings(name="c", plugin="p", input="i", on_success="o", on_error="  ")


class TestScopeSettings:
    def test_policy_is_required_no_default(self) -> None:
        with pytest.raises(ValidationError, match="policy"):
            ScopeSettings(name="s", opener="explode", closer="stitch")  # type: ignore[call-arg]

    def test_policy_vocabulary_is_closed(self) -> None:
        # Collector policy v1 = require_all|best_effort (spec decision 15); quorum/first deferred.
        with pytest.raises(ValidationError):
            ScopeSettings(name="s", opener="explode", closer="stitch", policy="quorum")

    def test_on_group_failure_defaults_to_quarantine(self) -> None:
        s = ScopeSettings(name="s", opener="explode", closer="stitch", policy="require_all")
        assert s.on_group_failure == "quarantine"


class TestElspethSettingsCrossRefs:
    def test_valid_collector_scope_pipeline_parses(self) -> None:
        settings = load_settings_from_config_dict(_with_collector_scope())
        assert settings.collectors[0].name == "page_stitcher"
        assert settings.scopes[0].closer == "page_stitcher"
        assert settings.max_bound_region_depth == 5

    def test_scope_closer_must_name_a_collector(self) -> None:
        doc = _with_collector_scope()
        doc["scopes"][0]["closer"] = "not_a_collector"  # type: ignore[index]
        with pytest.raises(ValueError, match="must name a collectors: entry"):
            load_settings_from_config_dict(doc)

    def test_collector_without_scope_rejected(self) -> None:
        doc = _with_collector_scope()
        doc["scopes"] = []
        with pytest.raises(ValueError, match="no scopes: entry binds"):
            load_settings_from_config_dict(doc)

    def test_scope_opener_must_name_a_transform(self) -> None:
        doc = _with_collector_scope()
        doc["scopes"][0]["opener"] = "missing_transform"  # type: ignore[index]
        with pytest.raises(ValueError, match="must name a transforms: entry"):
            load_settings_from_config_dict(doc)

    def test_two_scopes_cannot_share_a_closer(self) -> None:
        doc = _with_collector_scope()
        doc["scopes"] = [doc["scopes"][0], {**doc["scopes"][0], "name": "second", "opener": "explode"}]  # type: ignore[index,list-item]
        with pytest.raises(ValueError, match="one scope per closer|already bound"):
            load_settings_from_config_dict(doc)

    def test_max_bound_region_depth_override(self) -> None:
        doc = {**_MINIMAL, "max_bound_region_depth": 8}
        settings = load_settings_from_config_dict(doc)
        assert settings.max_bound_region_depth == 8

    def test_max_bound_region_depth_floor(self) -> None:
        with pytest.raises(ValidationError):
            load_settings_from_config_dict({**_MINIMAL, "max_bound_region_depth": 0})
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/core/test_config_collectors_scopes.py -v`
Expected: FAIL — `ImportError: cannot import name 'CollectorSettings'`.

- [ ] **Step 3: Implement the models**

Insert after `RowUnionSettings.validate_on_success` (before `class QueueSettings`):

```python
class CollectorSettings(BaseModel):
    """Configuration for a collector — the EXPAND-group closer (barrier-scopes spec §2/§3).

    A collector is a barrier, not an aggregation: it buffers every member of
    ONE bound EXPAND group and flushes on end_of_group ONLY. It reuses the
    batch-transform plugin contract (the same plugins aggregations use) but
    deliberately has NO trigger config — count/timeout/condition are
    inexpressible on a closer (a timeout on a closer converts a liveness bug
    into a silently short group; spec §5). Flush order is the opener's
    expansion ordinal, never arrival order.

    Example YAML:
        collectors:
          - name: page_stitcher
            plugin: stitch_pages
            input: pages
            on_success: assembled_out
            # on_error is optional: omitted (None) derives the route from
            # structure (spec §7 rule 9) — losses settle through the scope's
            # group machinery.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    name: str = Field(description="Unique identifier for this collector (drives node IDs and audit records)")
    plugin: str = Field(description="Batch-transform plugin name (same plugin contract as aggregations)")
    input: str = Field(description="Named input connection the bound region's members arrive on")
    on_success: str = Field(description="Connection name or sink name for the flushed group output")
    on_error: str | None = Field(
        default=None,
        description=(
            "Sink name for rows that fail batch processing, 'discard', or omitted (None): "
            "the route derives from structure — losses settle through the scope's group "
            "machinery (spec §7 rule 9)"
        ),
    )
    options: dict[str, Any] = Field(default_factory=dict, description="Plugin-specific configuration options")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Validate collector name is not empty or reserved."""
        if not v or not v.strip():
            raise ValueError("Collector name must not be empty")
        value = v.strip()
        _validate_max_length(value, field_label="Collector name", max_length=_MAX_NODE_NAME_LENGTH)
        _validate_node_name_chars(value, field_label="Collector name")
        if value in _RESERVED_EDGE_LABELS:
            raise ValueError(f"Collector name '{value}' is reserved. Reserved: {sorted(_RESERVED_EDGE_LABELS)}")
        if value.startswith("__"):
            raise ValueError(f"Collector name '{value}' starts with '__', which is reserved for system edges")
        return value

    @field_validator("input")
    @classmethod
    def validate_input(cls, v: str) -> str:
        """Validate input connection name is not empty."""
        if not v or not v.strip():
            raise ValueError("Collector input connection must not be empty")
        value = v.strip()
        return _validate_connection_or_sink_name(value, field_label="Collector input connection name")

    @field_validator("on_success")
    @classmethod
    def validate_on_success(cls, v: str) -> str:
        """Ensure on_success is a valid connection or sink name."""
        if not v.strip():
            raise ValueError("Collector on_success must be a connection name or sink name")
        value = v.strip()
        return _validate_connection_or_sink_name(value, field_label="Collector on_success connection name")

    @field_validator("on_error")
    @classmethod
    def validate_on_error(cls, v: str | None) -> str | None:
        """on_error is optional: None derives the route from structure (spec §7 rule 9)."""
        if v is None:
            return None
        if not v.strip():
            raise ValueError("Collector on_error must be a sink name, 'discard', or omitted")
        value = v.strip()
        if value == "discard":
            return value
        return _validate_connection_or_sink_name(value, field_label="Collector on_error sink name")


class ScopeSettings(BaseModel):
    """A declared EXPAND-group binding: opener (multi-row transform) → closer (collector).

    The scope is the build-time closer binding for a multi-row expansion
    (barrier-scopes spec §2/§3). The opener must be a multi-row transform
    (creates_tokens=True — builder-enforced, since config time cannot see
    plugin attributes); the closer MUST be a collectors: entry. policy is
    REQUIRED with no default (spec §3): the author decides whether a lost
    member fails the group.

    Example YAML:
        scopes:
          - name: document_pages
            opener: pdf_explode
            closer: page_stitcher
            policy: require_all
            on_group_failure: quarantine
    """

    model_config = {"frozen": True, "extra": "forbid"}

    name: str = Field(description="Scope identifier (scope_id in audit vocabulary)")
    opener: str = Field(description="Multi-row transform (creates_tokens=True) that opens the group")
    closer: str = Field(description="Collector that closes the group; MUST name a collectors: entry")
    policy: Literal["require_all", "best_effort"] = Field(
        description="Group arrival policy. REQUIRED — no default (spec §3). quorum/first are deferred (spec decision 15).",
    )
    on_group_failure: Literal["quarantine", "escalate"] = Field(
        default="quarantine",
        description="require_all failure handling: quarantine the group's source row, or escalate one loss to the enclosing bound group (escalate requires one — §7 rule 8).",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Validate scope name is not empty or reserved."""
        if not v or not v.strip():
            raise ValueError("Scope name must not be empty")
        value = v.strip()
        _validate_max_length(value, field_label="Scope name", max_length=_MAX_NODE_NAME_LENGTH)
        _validate_node_name_chars(value, field_label="Scope name")
        if value in _RESERVED_EDGE_LABELS:
            raise ValueError(f"Scope name '{value}' is reserved. Reserved: {sorted(_RESERVED_EDGE_LABELS)}")
        return value

    @field_validator("opener", "closer")
    @classmethod
    def validate_endpoint_names(cls, v: str) -> str:
        """Scope endpoints must be non-empty node names."""
        if not v or not v.strip():
            raise ValueError("Scope opener/closer must not be empty")
        value = v.strip()
        _validate_node_name_chars(value, field_label="Scope endpoint name")
        return value
```

Extend `ElspethSettings` — after the `row_unions` field (:1963-1967) add:

```python
    # Optional - collectors (EXPAND-group closers; barrier-scopes spec §3)
    collectors: list[CollectorSettings] = Field(
        default_factory=list,
        max_length=100,
        description="Collector (EXPAND-group closer) configurations",
    )

    # Optional - scope bindings (opener multi-row transform → collector closer)
    scopes: list[ScopeSettings] = Field(
        default_factory=list,
        max_length=100,
        description="Declared scope bindings pairing a multi-row transform opener with its collector closer",
    )

    # Supported bound-region nesting depth (spec §6.3 maintainer ruling): the
    # builder rejects deeper bound nesting fail-closed. Raise this ONLY if you
    # knowingly accept the per-insert audit churn of deeper nesting — the
    # model stays correct at any depth; the SUPPORT guarantee is 5.
    max_bound_region_depth: int = Field(
        default=5,
        ge=1,
        le=64,
        description="Maximum supported bound-region nesting depth (builder-enforced, spec §6.3)",
    )
```

And after `_validate_default_llm_profile_alias`, the cross-reference validator:

```python
    @model_validator(mode="after")
    def _validate_scope_bindings(self) -> "ElspethSettings":
        """Cross-check collectors: and scopes: (barrier-scopes spec §7 rule 1, parse-time half).

        The builder re-verifies with plugin instances in hand (opener
        multi-row-ness is only visible there); these are the pure
        name-reference checks that need no instances.
        """
        collector_names = {c.name for c in self.collectors}
        transform_names = {t.name for t in self.transforms}
        seen_scope_names: set[str] = set()
        seen_openers: set[str] = set()
        seen_closers: set[str] = set()
        for scope in self.scopes:
            if scope.name in seen_scope_names:
                raise ValueError(f"Scope name '{scope.name}' is declared twice")
            seen_scope_names.add(scope.name)
            if scope.closer not in collector_names:
                raise ValueError(
                    f"Scope '{scope.name}' closer '{scope.closer}' must name a collectors: entry. "
                    f"Declared collectors: {sorted(collector_names) or '(none)'}"
                )
            if scope.opener not in transform_names:
                raise ValueError(
                    f"Scope '{scope.name}' opener '{scope.opener}' must name a transforms: entry. "
                    f"A scope opener is a multi-row transform declared in transforms:."
                )
            if scope.opener in seen_openers:
                raise ValueError(f"Transform '{scope.opener}' opens two scopes — one scope per opener")
            seen_openers.add(scope.opener)
            if scope.closer in seen_closers:
                raise ValueError(f"Collector '{scope.closer}' is already bound — one scope per closer")
            seen_closers.add(scope.closer)
        unbound = collector_names - seen_closers
        if unbound:
            raise ValueError(
                f"Collector(s) {sorted(unbound)}: no scopes: entry binds them. A collector is an "
                f"EXPAND-group closer and requires a scope (spec §7 rule 1); an unbound collector "
                f"has no group to close. Add a scopes: entry naming it as closer."
            )
        return self
```

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/core/test_config_collectors_scopes.py -v`
Expected: PASS (all tests). Note: `test_valid_collector_scope_pipeline_parses` needs the `batch_stats`/`json_explode` plugin names only as strings — config parsing does not instantiate plugins, so any string is fine at this layer.

- [ ] **Step 5: Adjudicate the runtime-rejection-parity entries**

Run: `.venv/bin/python scripts/cicd/runtime_rejection_parity.py --write`
Then open `config/cicd/runtime_rejection_parity.yaml` and adjudicate every newly seeded entry for `CollectorSettings.*`, `ScopeSettings.*`, `ElspethSettings._validate_scope_bindings`, and the declarative `Field` constraints (`max_length=100` on collectors/scopes, `ge=1`/`le=64` on `max_bound_region_depth`). Dispositions at this task: `not_authorable` (the composer cannot yet author collector/scope shapes — Task 12 changes that and RE-adjudicates these to `mirrored` with the new Stage-1 codes). Never hand-edit a `key`.
Run: `pytest tests/unit/scripts/cicd/test_runtime_rejection_parity_gate.py -q`
Expected: PASS.

- [ ] **Step 6: Verify Task 1 pins still green, then commit**

Run: `pytest tests/unit/core/dag/test_canonical_hash_corpus.py tests/unit/core/test_config_collectors_scopes.py -q`
Expected: PASS.

```bash
git add src/elspeth/core/config.py tests/unit/core/test_config_collectors_scopes.py config/cicd/runtime_rejection_parity.yaml
git commit -m "feat(config): CollectorSettings + ScopeSettings + max_bound_region_depth (spec §3)"
```

---

### Task 3: Collector plugin instantiation, `NodeType.COLLECTOR`, and the graph node (conditional canonical key)

**Files:**
- Modify: `src/elspeth/contracts/enums.py` — `NodeType` (:91) gains `COLLECTOR = "collector"`
- Modify: `src/elspeth/contracts/types.py` — add `CollectorName = NewType("CollectorName", str)` beside `AggregationName`
- Modify: `src/elspeth/plugins/infrastructure/runtime_factory.py` — `PluginBundle` gains `collectors`; `instantiate_plugins_from_config` instantiates them (:117-129 aggregation block is the precedent, including the `is_batch_aware` rejection)
- Modify: `src/elspeth/core/dag/graph.py` — `from_plugin_instances` (:710) gains `collectors=`, `scope_settings=`, `max_bound_region_depth=` kwargs; new `set_collector_id_map`
- Modify: `src/elspeth/core/dag/builder.py` — `build_execution_graph` (:170) same kwargs; collector node construction after the row_union block (:663); consumer/producer registration beside the aggregation registrations (:811-816 producer, :881-886 consumer)
- Modify: call sites that build from full settings: `src/elspeth/cli.py` (:755, :1558, :1793, :2276, :2289) and the web validation path (`git grep -n "from_plugin_instances" src/elspeth/web/execution/` — thread the same three kwargs from the loaded `ElspethSettings`)
- Test: `tests/unit/core/dag/test_builder_collectors.py` (new)

**Interfaces:**
- Consumes: Task 2's `CollectorSettings`/`ScopeSettings`/`ElspethSettings.max_bound_region_depth`.
- Produces:
  - `NodeType.COLLECTOR` (`"collector"`).
  - `from_plugin_instances(..., collectors: Mapping[str, tuple[TransformProtocol, CollectorSettings]] | None = None, scope_settings: Sequence[ScopeSettings] | None = None, max_bound_region_depth: int = 5) -> ExecutionGraph` — the signature WS4's executor tests build against.
  - Collector node canonical config: `{"options": ..., "input_schema": ..., "scope": {"name", "opener", "policy", "on_group_failure"}}` — the `"scope"` key appears ONLY on collector nodes (no existing node's canonical dict changes; Task 1's corpus pins that).
  - `graph.get_collector_id_map() -> dict[CollectorName, NodeID]`.

- [ ] **Step 1: Write the failing test**

```python
"""Builder tests for collector nodes and the scope binding node-config key."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from elspeth.contracts.enums import NodeType
from elspeth.contracts.types import CollectorName
from elspeth.core.config import CollectorSettings, ScopeSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.dag.wiring import WiredTransform


class _Source:
    name = "src"
    output_schema = None
    config: ClassVar[dict[str, Any]] = {"schema": {"mode": "observed"}}
    _on_validation_failure = "discard"
    on_success = "rows"
    _output_schema_config = None


class _Sink:
    name = "out"
    input_schema = None
    config: ClassVar[dict[str, Any]] = {}
    _on_write_failure = "discard"
    declared_required_fields: ClassVar[frozenset[str]] = frozenset()

    def _reset_diversion_log(self) -> None:
        pass


class _MultiRowTransform:
    """Stub multi-row transform (creates_tokens=True) — a scope opener."""

    input_schema = None
    output_schema = None
    creates_tokens = True
    is_batch_aware = False
    on_success: str | None = "pages"
    on_error: str | None = "discard"
    declared_output_fields: ClassVar[frozenset[str]] = frozenset()
    declared_input_fields: ClassVar[frozenset[str]] = frozenset()
    declared_string_input_fields: ClassVar[frozenset[str]] = frozenset()
    passes_through_input = False
    forwards_input_fields = False
    removed_input_fields = frozenset()

    def __init__(self) -> None:
        self.name = "explode"
        self.config = {"schema": {"mode": "observed"}}
        self._output_schema_config = None


class _BatchTransform:
    """Stub batch-aware transform — the collector plugin."""

    input_schema = None
    output_schema = None
    creates_tokens = False
    is_batch_aware = True
    on_success: str | None = None
    on_error: str | None = None
    declared_output_fields: ClassVar[frozenset[str]] = frozenset()
    declared_input_fields: ClassVar[frozenset[str]] = frozenset()
    declared_string_input_fields: ClassVar[frozenset[str]] = frozenset()
    passes_through_input = False
    forwards_input_fields = False
    removed_input_fields = frozenset()

    def __init__(self) -> None:
        self.name = "stitch"
        self.config = {"schema": {"mode": "observed"}}
        self._output_schema_config = None


def _source_settings() -> dict[str, SourceSettings]:
    return {"src": SourceSettings(plugin="csv", options={"path": "x.csv", "schema": {"mode": "observed"}}, on_success="rows")}


def _explode_settings() -> TransformSettings:
    return TransformSettings(name="explode", plugin="json_explode", input="rows", on_success="pages", on_error="discard")


def _collector_settings() -> CollectorSettings:
    # on_error deliberately omitted: None = derives-from-structure (spec §7 rule 9),
    # the canonical authored shape (2026-08-22 synthesis).
    return CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="pages", on_success="out")


def _scope_settings() -> ScopeSettings:
    return ScopeSettings(name="document_pages", opener="explode", closer="page_stitcher", policy="require_all")


def _build(**overrides: Any) -> ExecutionGraph:
    kwargs: dict[str, Any] = dict(
        sources={"src": _Source()},
        source_settings_map=_source_settings(),
        transforms=[WiredTransform(plugin=_MultiRowTransform(), settings=_explode_settings())],
        sinks={"out": _Sink()},
        collectors={"page_stitcher": (_BatchTransform(), _collector_settings())},
        scope_settings=[_scope_settings()],
    )
    kwargs.update(overrides)
    return ExecutionGraph.from_plugin_instances(**kwargs)


class TestCollectorNode:
    def test_collector_node_is_built_with_scope_binding_key(self) -> None:
        graph = _build()
        collector_ids = graph.get_collector_id_map()
        assert list(collector_ids) == [CollectorName("page_stitcher")]
        info = graph.get_node_info(collector_ids[CollectorName("page_stitcher")])
        assert info.node_type == NodeType.COLLECTOR
        assert info.config["scope"] == {
            "name": "document_pages",
            "opener": "explode",
            "policy": "require_all",
            "on_group_failure": "quarantine",
        }

    def test_collector_requires_batch_aware_plugin(self) -> None:
        non_batch = _MultiRowTransform()
        with pytest.raises(GraphValidationError, match="is_batch_aware"):
            _build(collectors={"page_stitcher": (non_batch, _collector_settings())})

    def test_scope_binding_key_is_never_present_on_non_collector_nodes(self) -> None:
        # Canonical-hash stability (spec §3): "scope" must not leak into any
        # other node's canonical config dict.
        graph = _build()
        for node_id in graph.get_all_node_ids():
            info = graph.get_node_info(node_id)
            if info.node_type is not NodeType.COLLECTOR:
                assert "scope" not in info.config
```

(If `graph.get_all_node_ids()` does not exist under that name, use the graph's existing node-iteration surface — `git grep -n "def get_all_node" src/elspeth/core/dag/graph.py` — and adjust the last test to it; do not add a new iteration method for a test.)

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/core/dag/test_builder_collectors.py -v`
Expected: FAIL — `from_plugin_instances() got an unexpected keyword argument 'collectors'`.

- [ ] **Step 3: Implement**

(a) `contracts/enums.py` `NodeType` (after `ROW_UNION = "row_union"` at :103): `COLLECTOR = "collector"`. Then sweep exhaustive matches: `git grep -n "NodeType\." src/elspeth | grep -v enums.py` and check every `match`/full-enum dispatch for the new member. Expected touch points: `core/dag/schema_validation.py` contract propagation (treat COLLECTOR like ROW_UNION/AGGREGATION observed-schema nodes) and orchestrator `graph_registration.py` (must RAISE loudly on COLLECTOR until WS4 — pinned decision 5; add an explicit `OrchestrationInvariantError("collector execution lands in WS4 …")` arm rather than falling through).

(b) `contracts/types.py`: `CollectorName = NewType("CollectorName", str)` beside `AggregationName`.

(c) `runtime_factory.py`: `PluginBundle` gains `collectors: Mapping[str, tuple[TransformProtocol, CollectorSettings]] = field(default_factory=dict)` (extend the `freeze_fields` call at :67 with `"collectors"`). In `instantiate_plugins_from_config`, mirror the aggregation block (:117-129):

```python
        collectors = {}
        for collector_config in config.collectors:
            transform_cls = manager.get_transform_by_name(collector_config.plugin)
            transform = transform_cls(dict(collector_config.options))
            transform.on_success = collector_config.on_success
            # May be None — the derives-from-structure default (spec §7 rule 9);
            # transforms already type on_error as str | None, and WS4's executor
            # realizes the structural route.
            transform.on_error = collector_config.on_error
            if not transform.is_batch_aware:
                raise ValueError(
                    f"Collector '{collector_config.name}' uses transform '{collector_config.plugin}' "
                    f"which has is_batch_aware=False. Collectors reuse the batch-transform plugin "
                    f"contract and require batch-aware plugins."
                )
            collectors[collector_config.name] = (transform, collector_config)
```

(d) `graph.py` `from_plugin_instances` and `builder.py` `build_execution_graph` gain:

```python
    collectors: Mapping[str, tuple[TransformProtocol, CollectorSettings]] | None = None,
    scope_settings: Sequence[ScopeSettings] | None = None,
    max_bound_region_depth: int = 5,
```

(e) `builder.py` — after the row_union block (:663), before fork-gate connection:

```python
    # ===== BUILD COLLECTORS (EXPAND-GROUP CLOSERS; barrier-scopes spec §3) =====
    # A collector is a barrier reusing the batch-transform plugin contract.
    # Its scope binding rides the node config as the "scope" key — present on
    # collector nodes ONLY, so no pre-existing node's canonical hash moves
    # (spec §3; Task-1 corpus pins it).
    collector_ids: dict[CollectorName, NodeID] = {}
    scopes_by_closer: dict[str, ScopeSettings] = {s.closer: s for s in (scope_settings or ())}
    if collectors:
        for collector_name, (transform, collector_config) in collectors.items():
            if not transform.is_batch_aware:
                raise GraphValidationError(
                    f"Collector '{collector_name}' plugin '{collector_config.plugin}' has "
                    f"is_batch_aware=False. Collectors reuse the batch-transform plugin contract.",
                    component_id=collector_name,
                    component_type="collector",
                )
            scope = scopes_by_closer.get(collector_config.name)
            if scope is None:
                raise GraphValidationError(
                    f"Collector '{collector_name}' has no scopes: entry binding it. A collector is an "
                    f"EXPAND-group closer and requires a scope (spec §7 rule 1).",
                    component_id=collector_name,
                    component_type="collector",
                )
            transform_config = transform.config
            collector_node_config = {
                "options": dict(collector_config.options),
                "input_schema": transform_config["schema"],
                "scope": {
                    "name": scope.name,
                    "opener": scope.opener,
                    "policy": scope.policy,
                    "on_group_failure": scope.on_group_failure,
                },
            }
            col_id = node_id("collector", collector_name, collector_node_config)
            collector_ids[CollectorName(collector_name)] = col_id
            collector_output_schema_config = transform._output_schema_config
            if collector_output_schema_config is None:
                collector_output_schema_config = _parse_contract_schema_config(
                    transform_config,
                    owner=f"collector:{collector_name}",
                    component_id=collector_name,
                    component_type="collector",
                )
            graph.add_node(
                col_id,
                node_type=NodeType.COLLECTOR,
                plugin_name=collector_config.plugin,
                config=collector_node_config,
                input_schema=transform.input_schema,
                output_schema=transform.output_schema,
                output_schema_config=collector_output_schema_config,
                passes_through_input=transform.passes_through_input,
                forwards_input_fields=transform.forwards_input_fields,
                removed_input_fields=transform.removed_input_fields,
            )
    graph.set_collector_id_map(collector_ids)
```

All fields of the `"scope"` dict are REQUIRED on collectors (a collector always has a scope), so there is no None to omit here; the omitted-when-None discipline binds the COMPOSER spec field (Task 12), where `scope` is None for every non-collector node.

(f) Producer/consumer registration: beside the aggregation registrations, add

```python
    for collector_name, (_transform, collector_config) in (collectors or {}).items():
        register_producer(collector_config.on_success, collector_ids[CollectorName(collector_name)], "continue", f"collector '{collector_config.name}'")
```
(at :811-816) and
```python
    for collector_name, (_transform, collector_config) in (collectors or {}).items():
        register_consumer(
            collector_config.input,
            collector_ids[CollectorName(collector_name)],
            f"collector '{collector_config.name}'",
        )
```
(at :881-886). Also add `collector_ids.values()` to `processing_node_ids` (:1208-1211) and mirror the aggregation on_success edge/sink-target handling (read the block at builder.py:1043-1060 — the aggregation on_success→sink wiring — and replicate for collectors with the same GraphValidationError shape when `on_success` names neither a sink nor a consumed connection).

(g) `graph.py`: `set_collector_id_map` / `get_collector_id_map` following the `set_aggregation_id_map` (:792) pattern; include the new map in `_freeze_build_metadata` (:852).

(h) Thread call sites. In `cli.py` at each of :755, :1558, :1793, :2276, :2289 add:

```python
        collectors=plugins.collectors,
        scope_settings=list(settings_config.scopes) if settings_config.scopes else None,
        max_bound_region_depth=settings_config.max_bound_region_depth,
```

(the local names differ per site — match each site's existing `coalesce_settings=`/`row_union_settings=` argument style; the variable holding `ElspethSettings` is `settings_config` at :2276/:2289 — confirm per site). Then `git grep -n "from_plugin_instances(" src/elspeth/web/execution/ src/elspeth/engine/` and thread the same three kwargs at every site that already threads `row_union_settings`; sites building synthetic graphs without settings keep the defaults.

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/core/dag/test_builder_collectors.py -v`
Expected: PASS.

- [ ] **Step 5: Adjudicate parity entries for the new builder raise sites**

Run: `.venv/bin/python scripts/cicd/runtime_rejection_parity.py --write`
Adjudicate the two new `build_execution_graph` sites (`is_batch_aware`, collector-without-scope): disposition `not_authorable` for now (composer cannot author collector nodes until Task 12; Task 12 re-adjudicates to `mirrored`).
Run: `pytest tests/unit/scripts/cicd/test_runtime_rejection_parity_gate.py -q` — Expected: PASS.

- [ ] **Step 6: SLICE BOUNDARY — full suite + gates**

```bash
git rev-parse HEAD   # record
pytest tests/ -n 12  # CI-equivalent; ~18 min
git rev-parse HEAD   # must match the recorded value or re-run
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth  # count findings; compare to pre-slice count
wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only
```
Expected: full suite green (Task 1 corpus test included — proof no existing hash moved); trust-tier finding COUNT unchanged; wardline exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/contracts/enums.py src/elspeth/contracts/types.py \
        src/elspeth/plugins/infrastructure/runtime_factory.py \
        src/elspeth/core/dag/graph.py src/elspeth/core/dag/builder.py \
        src/elspeth/core/dag/schema_validation.py src/elspeth/engine/orchestrator/graph_registration.py \
        src/elspeth/cli.py tests/unit/core/dag/test_builder_collectors.py \
        config/cicd/runtime_rejection_parity.yaml
git commit -m "feat(dag): collector nodes with scope binding key; NodeType.COLLECTOR; plugin instantiation (spec §3)"
```
(Add the web/execution files touched in step 3(h) to the pathspec.)

---

### Task 4: `GroupBindingRegistry` with derived branch views (rule 1 — binding exclusivity)

**Files:**
- Create: `src/elspeth/core/dag/group_bindings.py`
- Modify: `src/elspeth/core/dag/builder.py` — construct the registry after the row_union walk (insert before `graph.set_pipeline_nodes(pipeline_nodes)` at :1563)
- Modify: `src/elspeth/core/dag/graph.py` — `set_group_bindings`/`get_group_bindings`
- Test: `tests/unit/core/dag/test_group_bindings.py` (new)

**Interfaces:**
- Consumes: WS1's `FrameKind` and `LineageFrame` (`from elspeth.contracts.enums import FrameKind`, `from elspeth.contracts.identity import LineageFrame` — run the Global-Constraints entry gate first); Task 3's `collector_ids`/`scopes_by_closer`.
- Produces (WS3/WS4 build against these exact names — there is NO `CloserBinding` type and NO `declared_branches` field; the roster field is `member_roster`, 2026-08-22 synthesis):

```python
class CloserKind(StrEnum):          # string-compatible with every serialized surface
    COALESCE = "coalesce"
    ROW_UNION = "row_union"
    COLLECTOR = "collector"

@dataclass(frozen=True, slots=True)
class GroupBinding:
    kind: FrameKind                 # FORK | EXPAND
    opener_node_id: NodeID          # FORK: the fork gate node; EXPAND: the opener transform node
    opener_name: str                # gate name (FORK) / transform name (EXPAND)
    closer_node_id: NodeID
    closer_name: str
    closer_kind: CloserKind
    policy: str                     # coalesce/row_union: settings policy; collector: scope policy
    on_group_failure: str | None    # scopes only; None for FORK bindings
    member_roster: tuple[str, ...]  # FORK: declared branch names (== fork_to); EXPAND: () (runtime roster)

@dataclass(frozen=True)
class GroupBindingRegistry:
    bindings: tuple[GroupBinding, ...]
    def by_opener_node(self) -> dict[NodeID, GroupBinding]: ...
    def by_closer_node(self) -> dict[NodeID, GroupBinding]: ...
    def binding_for(self, frame: LineageFrame) -> GroupBinding | None: ...   # keyed lookup for the settle-member walk (None = inert frame)
    def register_expand_group(self, group_id: str, *, opener_name: str) -> GroupBinding | None: ...  # runtime EXPAND-group index feed (WS3 wires the mint-path call)
    def branch_to_coalesce(self) -> dict[BranchName, CoalesceName]: ...
    def branch_to_row_union(self) -> dict[BranchName, RowUnionName]: ...

def build_group_binding_registry(
    *,
    fork_rosters: Mapping[GateName, tuple[NodeID, tuple[str, ...]]],   # gate → (gate node, fork_to)
    coalesce_plans: Mapping[CoalesceName, "_CoalescePlan"],
    coalesce_settings_by_name: Mapping[CoalesceName, CoalesceSettings],
    coalesce_ids: Mapping[CoalesceName, NodeID],
    row_union_branch_specs: Mapping[BranchName, "_RowUnionBranchSpec"],
    row_union_settings_by_name: Mapping[RowUnionName, RowUnionSettings],
    row_union_ids: Mapping[RowUnionName, NodeID],
    scope_settings: Sequence[ScopeSettings],
    collector_ids: Mapping[CollectorName, NodeID],
    transform_ids_by_name: Mapping[str, NodeID],
) -> GroupBindingRegistry
```
  - `graph.get_group_bindings() -> GroupBindingRegistry` (empty registry when no bound group exists).

- [ ] **Step 1: Write the failing tests**

```python
"""GroupBindingRegistry — ONE registry, derived views, frame resolution (barrier-scopes spec §3)."""

from __future__ import annotations

import json

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.types import BranchName, CoalesceName, NodeID, RowUnionName
from elspeth.core.dag.group_bindings import CloserKind, GroupBinding, GroupBindingRegistry


def _fork_binding(
    *,
    closer_kind: CloserKind = CloserKind.COALESCE,
    opener: str = "g1",
    closer: str = "merge",
    member_roster: tuple[str, ...] = ("path_a", "path_b"),
) -> GroupBinding:
    return GroupBinding(
        kind=FrameKind.FORK,
        opener_node_id=NodeID(f"gate_{opener}_abc"),
        opener_name=opener,
        closer_node_id=NodeID(f"{closer_kind}_{closer}_def"),
        closer_name=closer,
        closer_kind=closer_kind,
        policy="require_all",
        on_group_failure=None,
        member_roster=member_roster,
    )


def _expand_binding(*, opener: str = "explode", closer: str = "page_stitcher") -> GroupBinding:
    return GroupBinding(
        kind=FrameKind.EXPAND,
        opener_node_id=NodeID(f"transform_{opener}_abc"),
        opener_name=opener,
        closer_node_id=NodeID(f"collector_{closer}_def"),
        closer_name=closer,
        closer_kind=CloserKind.COLLECTOR,
        policy="require_all",
        on_group_failure="quarantine",
        member_roster=(),
    )


class TestCloserKindWire:
    def test_members_are_their_strings(self) -> None:
        # StrEnum: every serialized surface (composer NodeSpec dicts, the
        # guidedDecoder wire shapes, audit JSON, GraphValidationError
        # component_type) keeps carrying plain strings unchanged.
        assert CloserKind.COALESCE == "coalesce"
        assert f"{CloserKind.ROW_UNION}" == "row_union"
        assert json.loads(json.dumps(CloserKind.COLLECTOR)) == "collector"


class TestDerivedViews:
    def test_branch_to_coalesce_view(self) -> None:
        reg = GroupBindingRegistry(bindings=(_fork_binding(),))
        assert reg.branch_to_coalesce() == {
            BranchName("path_a"): CoalesceName("merge"),
            BranchName("path_b"): CoalesceName("merge"),
        }
        assert reg.branch_to_row_union() == {}

    def test_branch_to_row_union_view(self) -> None:
        reg = GroupBindingRegistry(bindings=(_fork_binding(closer_kind=CloserKind.ROW_UNION, closer="union"),))
        assert reg.branch_to_row_union() == {
            BranchName("path_a"): RowUnionName("union"),
            BranchName("path_b"): RowUnionName("union"),
        }

    def test_expand_binding_contributes_no_branch_view(self) -> None:
        reg = GroupBindingRegistry(bindings=(_expand_binding(),))
        assert reg.branch_to_coalesce() == {}
        assert reg.branch_to_row_union() == {}


class TestExclusivity:
    def test_duplicate_opener_rejected(self) -> None:
        with pytest.raises(ValueError, match="binds at most one closer"):
            GroupBindingRegistry(bindings=(_fork_binding(), _fork_binding(closer="other")))

    def test_duplicate_closer_rejected(self) -> None:
        b1 = _fork_binding()
        b2 = _fork_binding(opener="g2")  # same closer node
        with pytest.raises(ValueError, match="closes at most one group"):
            GroupBindingRegistry(bindings=(b1, b2))

    def test_shared_roster_member_across_forks_rejected(self) -> None:
        # binding_for's FORK resolution keys on member_key (the branch name),
        # so roster membership must be a function. Branch names are
        # one-producer connections in the builder; the registry re-asserts it.
        b1 = _fork_binding()
        b2 = _fork_binding(opener="g2", closer="other", member_roster=("path_b", "path_c"))
        with pytest.raises(ValueError, match="appears in two bound forks"):
            GroupBindingRegistry(bindings=(b1, b2))


class TestBindingFor:
    """binding_for — the settle-member walk's frame resolver (spec §6.1; 2026-08-22 synthesis)."""

    def test_fork_frame_resolves_by_member_key(self) -> None:
        reg = GroupBindingRegistry(bindings=(_fork_binding(),))
        frame = LineageFrame(kind=FrameKind.FORK, group_id="fg_runtime_1", member_key="path_a")
        assert reg.binding_for(frame) is reg.bindings[0]

    def test_fork_frame_outside_any_roster_is_inert(self) -> None:
        reg = GroupBindingRegistry(bindings=(_fork_binding(),))
        frame = LineageFrame(kind=FrameKind.FORK, group_id="fg_runtime_1", member_key="unbound_branch")
        assert reg.binding_for(frame) is None

    def test_expand_frame_resolves_after_mint_registration(self) -> None:
        # EXPAND group ids are runtime-minted (generate_id()), so the opener's
        # mint path registers each group; before registration the frame is
        # inert (exactly what an UNDECLARED expand stays forever).
        reg = GroupBindingRegistry(bindings=(_expand_binding(),))
        frame = LineageFrame(kind=FrameKind.EXPAND, group_id="eg_run_1", member_key="tok_child_1")
        assert reg.binding_for(frame) is None
        assert reg.register_expand_group("eg_run_1", opener_name="explode") is reg.bindings[0]
        assert reg.binding_for(frame) is reg.bindings[0]

    def test_undeclared_opener_registration_is_a_noop(self) -> None:
        reg = GroupBindingRegistry(bindings=(_expand_binding(),))
        assert reg.register_expand_group("eg_run_2", opener_name="plain_batch_transform") is None
        frame = LineageFrame(kind=FrameKind.EXPAND, group_id="eg_run_2", member_key="tok_x")
        assert reg.binding_for(frame) is None

    def test_reregistering_group_to_a_different_opener_rejected(self) -> None:
        reg = GroupBindingRegistry(bindings=(_expand_binding(), _expand_binding(opener="explode2", closer="stitch2")))
        reg.register_expand_group("eg_run_3", opener_name="explode")
        assert reg.register_expand_group("eg_run_3", opener_name="explode") is reg.bindings[0]  # idempotent
        with pytest.raises(ValueError, match="already registered"):
            reg.register_expand_group("eg_run_3", opener_name="explode2")
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/core/dag/test_group_bindings.py -v`
Expected: FAIL — module `elspeth.core.dag.group_bindings` does not exist.

- [ ] **Step 3: Implement `group_bindings.py`**

```python
"""Unified group-binding registry (barrier-scopes spec §3).

ONE registry of bound groups. The branch→closer views the routing code
wants (`_branch_to_coalesce`, `_branch_to_row_union`) are DERIVED from it,
never assembled independently — a second assembly path is how the two-map
drift the spec retires would come back.

Exclusivity (spec §7 rule 1): each frame source binds at most one closer,
and each closer closes at most one group. The registry enforces both at
construction; the builder's per-branch duplicate checks remain as the
authoring-facing diagnostics (they fire first, with better messages).

``binding_for`` is the settle-member walk's frame resolver (spec §6.1):
FORK frames resolve statically — a FORK frame's ``member_key`` IS the
declared branch name (spec §4.1), and rosters are member-disjoint. EXPAND
group ids are runtime-minted (``generate_id()``), so the opener's mint path
registers each new group via ``register_expand_group`` (WS3 wires the
single TokenManager call site; on takeover/resume the index re-derives
from ``group_records``, which carries the opener). An unregistered frame
is inert — ``None``, nothing staged, no roster watching (spec §2).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.types import BranchName, CoalesceName, NodeID, RowUnionName


class CloserKind(StrEnum):
    """Closer taxonomy for bound groups.

    StrEnum (2026-08-22 synthesis): members ARE their string values
    ("coalesce"/"row_union"/"collector"), so every serialized surface —
    composer NodeSpec dicts, guidedDecoder wire shapes, audit JSON,
    ``GraphValidationError.component_type`` — keeps carrying plain strings
    with zero serialization change. WS3 compares against the MEMBERS
    (``binding.closer_kind is CloserKind.COLLECTOR``), never string
    literals.
    """

    COALESCE = "coalesce"
    ROW_UNION = "row_union"
    COLLECTOR = "collector"


@dataclass(frozen=True, slots=True)
class GroupBinding:
    """The build-time group→closer association for ONE bound group."""

    kind: FrameKind
    opener_node_id: NodeID
    opener_name: str
    closer_node_id: NodeID
    closer_name: str
    closer_kind: CloserKind
    policy: str
    on_group_failure: str | None
    member_roster: tuple[str, ...]


@dataclass(frozen=True)
class GroupBindingRegistry:
    """All bound groups of one build. Empty for pipelines with no bound group."""

    bindings: tuple[GroupBinding, ...]
    # Derived indices, built in __post_init__ (init=False keeps the public
    # constructor shape at exactly `bindings`; frozen= blocks rebinding, and
    # these dicts are mutated in place during __post_init__ only).
    _fork_binding_by_member: dict[str, GroupBinding] = field(default_factory=dict, init=False, repr=False, compare=False)
    _expand_binding_by_opener: dict[str, GroupBinding] = field(default_factory=dict, init=False, repr=False, compare=False)
    # Runtime EXPAND-group index: group ids are minted at runtime, so the
    # opener's mint path feeds this via register_expand_group. Mutable BY
    # DESIGN inside the frozen registry — it is bookkeeping, not identity
    # (excluded from eq/repr).
    _expand_groups: dict[str, GroupBinding] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        opener_counts = Counter(b.opener_node_id for b in self.bindings)
        dup_openers = sorted(str(n) for n, c in opener_counts.items() if c > 1)
        if dup_openers:
            raise ValueError(f"Group opener(s) {dup_openers} bound twice — each frame source binds at most one closer (spec §7 rule 1)")
        closer_counts = Counter(b.closer_node_id for b in self.bindings)
        dup_closers = sorted(str(n) for n, c in closer_counts.items() if c > 1)
        if dup_closers:
            raise ValueError(f"Closer(s) {dup_closers} bound twice — each closer closes at most one group (spec §7 rule 1)")
        for binding in self.bindings:
            if binding.kind is FrameKind.FORK:
                for member in binding.member_roster:
                    if member in self._fork_binding_by_member:
                        raise ValueError(
                            f"Branch '{member}' appears in two bound forks' rosters — branch names are "
                            f"one-producer connections, so roster membership must be a function "
                            f"(binding_for's FORK resolution keys on it)"
                        )
                    self._fork_binding_by_member[member] = binding
            else:
                self._expand_binding_by_opener[binding.opener_name] = binding

    def by_opener_node(self) -> dict[NodeID, GroupBinding]:
        return {b.opener_node_id: b for b in self.bindings}

    def by_closer_node(self) -> dict[NodeID, GroupBinding]:
        return {b.closer_node_id: b for b in self.bindings}

    def binding_for(self, frame: LineageFrame) -> GroupBinding | None:
        """Resolve one lineage frame to its bound closer (None = inert frame).

        The settle-member walk's keyed lookup (spec §6.1): FORK frames key on
        ``member_key`` (the declared branch name); EXPAND frames key on the
        runtime-registered ``group_id``. None means nobody waits — pure
        provenance, nothing staged (spec §2).
        """
        if frame.kind is FrameKind.FORK:
            return self._fork_binding_by_member.get(frame.member_key)
        return self._expand_groups.get(frame.group_id)

    def register_expand_group(self, group_id: str, *, opener_name: str) -> GroupBinding | None:
        """Associate a runtime-minted EXPAND group id with its scope binding.

        Called unconditionally from the opener's mint path (WS3 wires the
        TokenManager call site): a declared scope opener returns (and
        records) its binding; an undeclared expand returns None and records
        nothing — its frames stay inert forever. Idempotent per group id;
        re-registering one group under a DIFFERENT opener is an integrity
        violation (group ids are unique per mint).
        """
        binding = self._expand_binding_by_opener.get(opener_name)
        if binding is None:
            return None
        existing = self._expand_groups.get(group_id)
        if existing is not None:
            if existing is not binding:
                raise ValueError(
                    f"EXPAND group '{group_id}' already registered to opener '{existing.opener_name}'; "
                    f"refusing re-registration under '{opener_name}'"
                )
            return existing
        self._expand_groups[group_id] = binding
        return binding

    def branch_to_coalesce(self) -> dict[BranchName, CoalesceName]:
        return {
            BranchName(member): CoalesceName(b.closer_name)
            for b in self.bindings
            if b.kind is FrameKind.FORK and b.closer_kind is CloserKind.COALESCE
            for member in b.member_roster
        }

    def branch_to_row_union(self) -> dict[BranchName, RowUnionName]:
        return {
            BranchName(member): RowUnionName(b.closer_name)
            for b in self.bindings
            if b.kind is FrameKind.FORK and b.closer_kind is CloserKind.ROW_UNION
            for member in b.member_roster
        }
```

Freeze caveat: `graph.set_group_bindings` stores the registry in the graph's build
metadata. The `_expand_groups` runtime index must stay mutable after
`_freeze_build_metadata` — verify the freeze wraps the ID MAPS (MappingProxyType) but
stores this OBJECT as-is; if the freeze deep-copies or proxies arbitrary values, store
the registry reference outside that sweep. `_fork_binding_by_member` /
`_expand_binding_by_opener` are never mutated after construction.

Then `build_group_binding_registry(...)` in the same module with the Interfaces-block signature: FORK bindings — for each fork gate whose branches appear in `coalesce_branch_specs`/`row_union_branch_specs`, one binding per gate with `member_roster=tuple(fork_to)`, closer resolved through the branch specs (Task 6's rule 2 guarantees one closer per gate; until Task 6 lands, take the closer of the FIRST bound branch and let rule 2 tighten it); `closer_kind=CloserKind.COALESCE` / `CloserKind.ROW_UNION` per the resolving spec map; `policy` from `CoalesceSettings.policy` / `"require_all"` for row_union. EXPAND bindings — one per `ScopeSettings`, `opener_node_id=transform_ids_by_name[scope.opener]`, closer from `collector_ids`, `closer_kind=CloserKind.COLLECTOR`, `policy=scope.policy`, `on_group_failure=scope.on_group_failure`.

- [ ] **Step 4: Wire into the builder + differential test**

In `build_execution_graph`, before `graph.set_pipeline_nodes(pipeline_nodes)` (:1563):

```python
    # ===== UNIFIED GROUP-BINDING REGISTRY (barrier-scopes spec §3) =====
    registry = build_group_binding_registry(
        fork_rosters={
            GateName(gate_entry.name): (gate_entry.node_id, tuple(gate_entry.fork_to))
            for gate_entry in gate_entries
            if gate_entry.fork_to
        },
        coalesce_plans=coalesce_plans,
        coalesce_settings_by_name={CoalesceName(c.name): c for c in (coalesce_settings or [])},
        coalesce_ids=coalesce_ids,
        row_union_branch_specs=row_union_branch_specs,
        row_union_settings_by_name={RowUnionName(u.name): u for u in (row_union_settings or [])},
        row_union_ids=row_union_ids,
        scope_settings=tuple(scope_settings or ()),
        collector_ids=collector_ids,
        transform_ids_by_name=transform_ids_by_name,
    )
    graph.set_group_bindings(registry)
```

(`coalesce_plans`/`coalesce_ids`/`row_union_*`/`gate_entries`/`transform_ids_by_name` are all in scope at that point — verified against the live builder; `coalesce_ids`/`row_union_ids` are defined only inside their `if` blocks, so hoist their initializations (`coalesce_ids: dict[CoalesceName, NodeID] = {}` etc.) above the conditionals.)

Add to `tests/unit/core/dag/test_group_bindings.py` a differential test that builds a real fork→coalesce graph (reuse the stub-plugin idiom from `tests/unit/core/dag/test_builder_validation.py` — `_BuilderValidationMockSource`/`_BuilderValidationMockSink`/gate + `CoalesceSettings`) and asserts:

```python
def test_derived_views_reproduce_graph_maps_exactly() -> None:
    graph = _build_fork_coalesce_graph()  # helper in this file, modeled on test_builder_validation's graph builders
    registry = graph.get_group_bindings()
    assert registry.branch_to_coalesce() == graph.get_branch_to_coalesce_map()
    assert registry.branch_to_row_union() == graph.get_branch_to_row_union_map()
```

This is the no-second-assembly-path proof: the views and the legacy maps must be equal on every buildable topology. (WS3/WS4 may later re-point the graph maps AT the views; WS2 only proves equality.)

- [ ] **Step 5: Run to pass**

Run: `pytest tests/unit/core/dag/test_group_bindings.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/core/dag/group_bindings.py src/elspeth/core/dag/builder.py \
        src/elspeth/core/dag/graph.py tests/unit/core/dag/test_group_bindings.py
git commit -m "feat(dag): unified GroupBindingRegistry with derived branch views (spec §3, rule 1)"
```

---

### Task 5: Bound-region computation, well-nestedness (rule 3), depth cap, derived fixpoint bound

**Files:**
- Create: `src/elspeth/core/dag/bound_regions.py`
- Modify: `src/elspeth/core/dag/builder.py` — call `compute_bound_regions` right after the Task-4 registry construction
- Modify: `src/elspeth/core/dag/graph.py` — `set_max_bound_region_depth`/`get_max_bound_region_depth` (mirroring the `set_group_bindings`/`get_group_bindings` pair — 2026-08-22 synthesis; the value is the max OBSERVED bound-region depth of this build, 0 = no bound regions, NOT the configured cap) and `set_escalation_fixpoint_bound`/property `escalation_fixpoint_bound` (default `1_000`)
- Modify: `src/elspeth/engine/orchestrator/types.py` — `PipelineConfig` gains `escalation_fixpoint_bound: int = 1_000`
- Modify: `src/elspeth/engine/orchestrator/leader_drain.py` — `run_end_of_input_barrier_flush` (:420) reads the config bound instead of the module constant (:417, :464, :514-518)
- Modify: PipelineConfig construction sites that have a graph in hand: `src/elspeth/engine/orchestrator/preflight.py:651`, `src/elspeth/cli.py:1355`, `:3453`, `src/elspeth/engine/__init__.py:17` (pass `escalation_fixpoint_bound=graph.escalation_fixpoint_bound`; sites without a graph keep the default)
- Test: `tests/unit/core/dag/test_bound_regions.py` (new), `tests/unit/core/dag/test_builder_validation.py` (extend)

**Interfaces:**
- Consumes: Task 4's `GroupBindingRegistry`/`GroupBinding`.
- Produces:

```python
@dataclass(frozen=True)
class BoundRegion:
    binding: GroupBinding
    member_node_ids: frozenset[NodeID]   # strictly inside — opener and closer EXCLUDED
    depth: int                           # 1 = outermost bound region

def compute_bound_regions(
    graph: "ExecutionGraph",
    registry: GroupBindingRegistry,
    *,
    max_depth: int,
) -> tuple[BoundRegion, ...]           # raises GraphValidationError on partial overlap / depth breach

ESCALATION_ITERATIONS_PER_LEVEL = 8

def derive_escalation_fixpoint_bound(max_observed_depth: int) -> int:
    """1_000 + 8 * depth — derived from the ACTUAL depth, never a bare constant (spec §6.3).

    THE ONE fixpoint formula (2026-08-22 synthesis) — owned here. WS3's
    leader_drain helper `derive_end_of_input_flush_bound` aligns to exactly
    this formula and consumes `graph.get_max_bound_region_depth()`; any
    competing formula is deleted, never forked.
    """
```
  - `graph.get_max_bound_region_depth() -> int` — the max OBSERVED bound-region depth of the build (0 = none); the accessor WS3 feeds into this module's formula.
  - `graph.escalation_fixpoint_bound: int`; `PipelineConfig.escalation_fixpoint_bound: int` — the value `run_end_of_input_barrier_flush` iterates to (WS5 extends the same loop for collectors; it consumes THIS field).

- [ ] **Step 1: Write the failing tests**

```python
"""Bound-region computation: membership, well-nestedness, depth cap, fixpoint bound."""

from __future__ import annotations

import pytest

from elspeth.core.dag.bound_regions import derive_escalation_fixpoint_bound
from elspeth.core.dag.group_bindings import CloserKind
from elspeth.core.dag.models import GraphValidationError

# Graph-level cases use the shared stub-plugin builders defined in this file,
# modeled on tests/unit/core/dag/test_builder_validation.py (mock source/sink/
# transform classes) plus Task 3's _MultiRowTransform/_BatchTransform stubs.


class TestFixpointBound:
    def test_depth_zero_keeps_base(self) -> None:
        assert derive_escalation_fixpoint_bound(0) == 1_000

    def test_bound_grows_with_depth(self) -> None:
        # THE one formula (2026-08-22 synthesis): 1_000 + 8 * depth.
        assert derive_escalation_fixpoint_bound(5) == 1_040
        assert derive_escalation_fixpoint_bound(1_000) == 9_000  # override-deep builds outgrow the old constant


class TestRegionMembership:
    def test_fork_coalesce_region_members(self) -> None:
        graph = _build_fork_coalesce_with_branch_transforms()
        regions = graph.get_bound_regions()
        assert len(regions) == 1
        region = regions[0]
        # Branch transforms are members; gate and coalesce are NOT.
        member_names = _plugin_names(graph, region.member_node_ids)
        assert member_names == {"branch_a_transform", "branch_b_transform"}
        assert region.depth == 1
        assert graph.get_max_bound_region_depth() == 1


class TestWellNestedness:
    def test_partial_overlap_rejected(self) -> None:
        with pytest.raises(GraphValidationError, match="partially overlap"):
            _build_partially_overlapping_regions()


class TestDepthCap:
    def test_depth_beyond_cap_rejected(self) -> None:
        # Two nested bound regions with max_bound_region_depth=1.
        with pytest.raises(GraphValidationError, match="bound-region nesting depth"):
            _build_nested_fork_in_fork(max_bound_region_depth=1)

    def test_depth_within_cap_builds_and_derives_bound(self) -> None:
        graph = _build_nested_fork_in_fork(max_bound_region_depth=5)
        assert max(r.depth for r in graph.get_bound_regions()) == 2
        assert graph.get_max_bound_region_depth() == 2
        assert graph.escalation_fixpoint_bound == 1_000 + 8 * 2
```

Helper topologies to implement in the test file (real code, not sketches — each returns `ExecutionGraph.from_plugin_instances(...)` built from the stub classes):
- `_build_fork_coalesce_with_branch_transforms()` — source → gate `fork_to [path_a, path_b]` (routes `{"all": "fork"}`, condition `"'all'"`), per-branch `TransformSettings(name="branch_a_transform", input="path_a", on_success="path_a_out", on_error="discard")` (and `_b`), coalesce `branches: {path_a: path_a_out, path_b: path_b_out}`, `on_success: out`.
- `_build_nested_fork_in_fork(max_bound_region_depth)` — outer fork [left, right]; branch `left`'s chain contains an inner gate forking [la, lb] closed by inner coalesce whose merged output flows to the OUTER coalesce's `left` connection; branch `right` is a plain transform chain to the outer coalesce. Outer coalesce closes [left, right].
- `_build_partially_overlapping_regions()` — two forks whose coalesces cross (fork1's closer inside fork2's region while fork1's opener is outside it). Construct by wiring fork2 inside branch of fork1 but declaring fork2's coalesce to consume a connection produced OUTSIDE fork1's region. If the topology is unreachable through settings (the builder's existing guards may fire first), assert on whichever `GraphValidationError` fires and note WHICH in the test docstring — the region check must still exist for the `add_edge` surface.
- `_plugin_names(graph, ids)` — `{graph.get_node_info(n).plugin_name for n in ids}` normalized of prefixes.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/core/dag/test_bound_regions.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `bound_regions.py`**

```python
"""Bound-region (SESE) computation and structural validation (spec §7 rules 3, depth cap §6.3).

A bound region is the SESE span of one bound group: the nodes strictly
between its opener and its closer. Membership walks SUCCESS-PATH edges
only — RoutingMode.DIVERT edges (on_error, __quarantine__, __failsink__)
are failure semantics, not region topology (pinned decision 1 in the WS2
plan; §7 rule 9 treats in-region on_error as legal).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from elspeth.contracts.enums import RoutingMode
from elspeth.contracts.types import NodeID
from elspeth.core.dag.group_bindings import GroupBinding, GroupBindingRegistry
from elspeth.core.dag.models import GraphValidationError

if TYPE_CHECKING:
    from elspeth.core.dag.graph import ExecutionGraph

ESCALATION_ITERATIONS_PER_LEVEL = 8
_BASE_FLUSH_ITERATIONS = 1_000


def derive_escalation_fixpoint_bound(max_observed_depth: int) -> int:
    """Non-convergence bound for the EOF drain fixpoint, derived from build depth.

    Spec §6.3: "derived at build from the actual depth (+ margin), never a
    constant — today's MAX_END_OF_INPUT_FLUSH_ITERATIONS = 1_000 would
    collide with an override-deep unwind."

    THE one fixpoint formula (2026-08-22 synthesis): 1_000 + 8 * depth —
    each bound nesting level adds at most a handful of
    escalate-notify-reevaluate rounds, so depth-5 stays at 1_040 and an
    override-depth-1000 unwind gets 9_000. WS3's
    `derive_end_of_input_flush_bound` aligns to exactly this formula
    (consuming `graph.get_max_bound_region_depth()`); competing formulas are
    deleted, never forked.
    """
    return _BASE_FLUSH_ITERATIONS + ESCALATION_ITERATIONS_PER_LEVEL * max_observed_depth


@dataclass(frozen=True)
class BoundRegion:
    binding: GroupBinding
    member_node_ids: frozenset[NodeID]
    depth: int


def _forward_reach(graph: "ExecutionGraph", start: NodeID, stop: NodeID) -> set[NodeID]:
    """Nodes reachable from start via non-DIVERT edges, not expanding through stop."""
    seen: set[NodeID] = set()
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for edge in graph.get_outgoing_edges(current):
            if edge.mode is RoutingMode.DIVERT:
                continue
            nxt = NodeID(edge.to_node)
            if nxt in seen or nxt == stop:
                if nxt == stop:
                    seen.add(nxt)
                continue
            seen.add(nxt)
            frontier.append(nxt)
    return seen


def _backward_reach(graph: "ExecutionGraph", start: NodeID, stop: NodeID) -> set[NodeID]:
    seen: set[NodeID] = set()
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for edge in graph.get_incoming_edges(current):
            if edge.mode is RoutingMode.DIVERT:
                continue
            prev = NodeID(edge.from_node)
            if prev in seen or prev == stop:
                if prev == stop:
                    seen.add(prev)
                continue
            seen.add(prev)
            frontier.append(prev)
    return seen


def compute_bound_regions(
    graph: "ExecutionGraph",
    registry: GroupBindingRegistry,
    *,
    max_depth: int,
) -> tuple[BoundRegion, ...]:
    spans: list[tuple[GroupBinding, frozenset[NodeID]]] = []
    for binding in registry.bindings:
        forward = _forward_reach(graph, binding.opener_node_id, binding.closer_node_id)
        backward = _backward_reach(graph, binding.closer_node_id, binding.opener_node_id)
        members = frozenset((forward & backward) - {binding.opener_node_id, binding.closer_node_id})
        spans.append((binding, members))

    def _span(binding: GroupBinding, members: frozenset[NodeID]) -> frozenset[NodeID]:
        return members | {binding.opener_node_id, binding.closer_node_id}

    # Well-nestedness (spec §7 rule 3): regions fully contain or are disjoint.
    for i, (b1, m1) in enumerate(spans):
        for b2, m2 in spans[i + 1:]:
            s1, s2 = _span(b1, m1), _span(b2, m2)
            if s1.isdisjoint(s2):
                continue
            if s2 <= m1 or s1 <= m2:
                continue  # strictly nested (inner span entirely inside outer MEMBERS)
            raise GraphValidationError(
                f"Bound regions '{b1.closer_name}' (opener '{b1.opener_name}') and "
                f"'{b2.closer_name}' (opener '{b2.opener_name}') partially overlap. "
                f"Bound regions must fully contain one another or be disjoint (spec §7 rule 3): "
                f"close the inner group before the outer closer, or separate the regions.",
                component_id=b2.closer_name,
                component_type=b2.closer_kind,
            )

    regions: list[BoundRegion] = []
    for b1, m1 in spans:
        s1 = _span(b1, m1)
        depth = 1 + sum(1 for b2, m2 in spans if b2 is not b1 and s1 <= m2)
        regions.append(BoundRegion(binding=b1, member_node_ids=m1, depth=depth))

    too_deep = [r for r in regions if r.depth > max_depth]
    if too_deep:
        worst = max(too_deep, key=lambda r: r.depth)
        raise GraphValidationError(
            f"Bound-region nesting depth {worst.depth} exceeds the supported maximum {max_depth} "
            f"(innermost closer: '{worst.binding.closer_name}'). The supported guarantee is "
            f"{max_depth} layers (spec §6.3); deeper nesting is model-correct but unsupported — "
            f"per-token audit churn scales with depth. Set max_bound_region_depth in settings to "
            f"accept the churn knowingly.",
            component_id=worst.binding.closer_name,
            component_type=worst.binding.closer_kind,
        )
    return tuple(regions)
```

(Adjust `edge.to_node`/`edge.from_node`/`edge.mode` attribute names to the live `EdgeInfo` dataclass — `git grep -n "class EdgeInfo" src/elspeth/core/dag/` — the row_union walk at builder.py:1449-1461 shows the shape: `in_edge.from_node`.)

- [ ] **Step 4: Wire into the builder and plumb the bound**

In `build_execution_graph`, immediately after `graph.set_group_bindings(registry)`:

```python
    regions = compute_bound_regions(graph, registry, max_depth=max_bound_region_depth)
    graph.set_bound_regions(regions)
    max_observed_depth = max((r.depth for r in regions), default=0)
    graph.set_max_bound_region_depth(max_observed_depth)
    graph.set_escalation_fixpoint_bound(derive_escalation_fixpoint_bound(max_observed_depth))
```

`graph.py`: `set_bound_regions`/`get_bound_regions`, `set_max_bound_region_depth`/`get_max_bound_region_depth` (mirroring the `set_group_bindings`/`get_group_bindings` pair; docstring MUST state it returns the max OBSERVED bound-region nesting depth of this build — 0 when no bound regions — not the configured `max_bound_region_depth` cap, which shares the name by synthesis decision), and `set_escalation_fixpoint_bound`/`escalation_fixpoint_bound` (default `1_000`), following the `set_aggregation_id_map` pattern, frozen in `_freeze_build_metadata`.

`types.py` `PipelineConfig`: add

```python
    # Derived at graph build from the actual bound-region depth (+ margin);
    # never a bare constant (barrier-scopes spec §6.3). leader_drain iterates
    # the EOF barrier-flush fixpoint to exactly this bound.
    escalation_fixpoint_bound: int = 1_000
```

`leader_drain.py`: in `run_end_of_input_barrier_flush`, replace the two constant uses:

```python
    flush_iteration_bound = config.escalation_fixpoint_bound
    for _ in range(flush_iteration_bound):
        ...
    raise OrchestrationInvariantError(
        f"End-of-input barrier flush for run '{processor.run_id}' did not converge within "
        f"{flush_iteration_bound} intake/flush rounds; durable BLOCKED barrier holds remain. "
        "Possible barrier cycle or a flush that re-deposits its own inputs."
    )
```

Keep `MAX_END_OF_INPUT_FLUSH_ITERATIONS = 1_000` as the module-level default documentation anchor with a comment pointing at `derive_escalation_fixpoint_bound` (grep for external importers first: `git grep -n "MAX_END_OF_INPUT_FLUSH_ITERATIONS" src tests` — update any test that imports it to read `PipelineConfig.escalation_fixpoint_bound`'s default instead only if the test asserts the bound; otherwise leave).

Thread `escalation_fixpoint_bound=graph.escalation_fixpoint_bound` (or the equivalent local graph variable) at `preflight.py:651`, `cli.py:1355`, `cli.py:3453`, `engine/__init__.py:17` — at each site confirm a built graph is in scope; where none is (pure-unit constructions, `testing/__init__.py:602`), the default holds.

- [ ] **Step 5: Run to pass**

Run: `pytest tests/unit/core/dag/test_bound_regions.py tests/unit/core/dag/test_group_bindings.py tests/unit/engine/orchestrator/ -q`
Expected: PASS (the orchestrator selection catches leader_drain fallout).

- [ ] **Step 6: Adjudicate parity entries (well-nestedness + depth-cap raises), commit**

Run: `.venv/bin/python scripts/cicd/runtime_rejection_parity.py --write` — adjudicate the two new sites: `abstains` for both, note: "Stage 1's existing narrower guards (`row_union_nested_fork_invalid`, `row_union_downstream_group_invalid`) reject a subset; full region computation is runtime-only; deliberate — Stage 1 must not be stricter than the runtime and the general shapes require the built graph."
Run: `pytest tests/unit/scripts/cicd/test_runtime_rejection_parity_gate.py -q` — Expected: PASS.

```bash
git add src/elspeth/core/dag/bound_regions.py src/elspeth/core/dag/builder.py src/elspeth/core/dag/graph.py \
        src/elspeth/engine/orchestrator/types.py src/elspeth/engine/orchestrator/leader_drain.py \
        src/elspeth/engine/orchestrator/preflight.py src/elspeth/engine/__init__.py src/elspeth/cli.py \
        tests/unit/core/dag/test_bound_regions.py config/cicd/runtime_rejection_parity.yaml
git commit -m "feat(dag): bound-region computation, well-nestedness, depth cap, derived escalation fixpoint bound (spec §7 r3, §6.3)"
```

---

### Task 6: Whole-roster fork closure (rule 2 / ruling 23)

**Files:**
- Modify: `src/elspeth/core/dag/builder.py` — new check inside the fork-gate connection loop (after the per-branch wiring at :671-740, before the :742 "VALIDATE COALESCE BRANCHES" block)
- Modify: `src/elspeth/web/composer/state.py` — Stage-1 mirror (SYMBOL anchor: inside `validate()`, in the topology section immediately after the coalesce `coalesce_branch_alias_unreachable` block — re-read the file first; it is under maintainer edit)
- Modify: `src/elspeth/web/composer/tools/generation.py` — `_VALIDATION_ERROR_PATTERNS` entries
- Modify: `config/cicd/runtime_rejection_parity.yaml` — via `--write`
- Modify: `tests/fixtures/dag_scenario_corpus/v1/parallel-coalesces/two-parallel-require-all.yaml` — RC-3 casualty rewrite (same-slice migration, protocols RC-3)
- Modify: `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` + `tests/unit/architecture/test_dag_scenario_corpus_contract.py` — adjudicated rotation for the migrated scenario
- Move: `git mv tests/fixtures/dag_scenario_corpus/oracle_freeze/v1/parallel-coalesces tests/fixtures/dag_scenario_corpus/oracle_freeze/retired/parallel-coalesces` + new `MIGRATION.md` there (protocols §S3 retirement)
- Test: `tests/unit/core/dag/test_builder_validation.py` (extend), `tests/unit/web/composer/test_state_bound_regions.py` (NEW file — do not touch test_state.py)

**Interfaces:**
- Consumes: builder locals `gate_entries`, `coalesce_branch_specs: dict[BranchName, _CoalesceBranchSpec]`, `row_union_branch_specs: dict[BranchName, _RowUnionBranchSpec]`, `coalesce_plans`.
- Produces: `GraphValidationError` messages beginning `"Fork gate '{gate}' has mixed closure:"` and `"Fork gate '{gate}' closes at multiple barriers:"` and `"Coalesce '{name}' roster mismatch:"`; composer codes `fork_mixed_closure_invalid`, `fork_multiple_closers_invalid`, `fork_roster_mismatch`; the migrated `parallel-coalesces` corpus scenario (protocols RC-3 — the r23 casualty rides THIS commit, same-slice discipline).

- [ ] **Step 0: RC-5 casualty-grep pre-check (protocols RC-5 — BEFORE the rejection lands).** The zero-r25/zero-r28 casualty inventory was measured at HEAD `add597342`; re-verify at this slice's HEAD: `git grep -l "fork_to" examples/ tests/fixtures/dag_scenario_corpus/v1/` and inspect each hit for (i) aggregation nodes inside a fork/union branch (r25), (ii) multi-row transforms inside a bound branch (r28), (iii) branch subsets closing at a coalesce (r23). Expected casualties: `parallel-coalesces` (r23, migrated in this task) and `examples/row_union_ab_experiment/settings_screened.yaml` (rule 4, handled in Task 7) ONLY. Any NEW hit is a new casualty: STOP and add it to the protocols RC worklist with its own adjudication before landing this task.

- [ ] **Step 1: Write the failing builder tests** (in `test_builder_validation.py`, new class; reuse that file's `_BuilderValidationMockSource`/`_BuilderValidationMockSink` and gate/coalesce construction idioms)

```python
class TestWholeRosterForkClosure:
    """Spec §7 rule 2 (ruling 23): a fork closes entirely at ONE closer or not at all."""

    def test_mixed_fork_closure_rejected(self) -> None:
        # fork_to [failing, survivor]: 'failing' declared by a coalesce,
        # 'survivor' matches a sink name — buildable today, rejected now.
        with pytest.raises(GraphValidationError, match="mixed closure"):
            _build_gate_fork_graph(
                fork_to=["failing", "survivor"],
                coalesce=CoalesceSettings(name="merge", branches={"failing": "failing", "second": "second"}, on_success="out"),
                extra_sinks={"survivor": _BuilderValidationMockSink()},
            )

    def test_pure_fan_out_fork_stays_legal(self) -> None:
        # BOTH branches direct to sinks, no closer anywhere: "fully unbound
        # (pure fan-out)" — LEGAL under rule 2 as written (plan pinned decision 2).
        graph = _build_gate_fork_graph(
            fork_to=["left", "right"],
            coalesce=None,
            extra_sinks={"left": _BuilderValidationMockSink(), "right": _BuilderValidationMockSink()},
        )
        assert graph is not None

    def test_fork_split_across_two_closers_rejected(self) -> None:
        # The parallel-coalesces shape: ONE fork closing at TWO sibling coalesces.
        with pytest.raises(GraphValidationError, match="closes at multiple barriers"):
            _build_gate_fork_graph(
                fork_to=["left_a", "left_b", "right_a", "right_b"],
                coalesce=[
                    CoalesceSettings(name="merge_left", branches={"left_a": "left_a", "left_b": "left_b"}, on_success="out"),
                    CoalesceSettings(name="merge_right", branches={"right_a": "right_a", "right_b": "right_b"}, on_success="out"),
                ],
            )

    def test_closer_roster_must_equal_fork_roster(self) -> None:
        # Closer declares a strict SUPERSET of the fork's branches → mismatch.
        with pytest.raises(GraphValidationError, match="roster mismatch"):
            _build_gate_fork_graph(
                fork_to=["path_a", "path_b"],
                coalesce=CoalesceSettings(name="merge", branches={"path_a": "path_a", "path_b": "path_b", "path_c": "path_c"}, on_success="out"),
            )
```

`_build_gate_fork_graph` is a file-local helper: source + gate (`routes={"all": "fork"}`, `condition="'all'"`, `fork_to=...`) + optional coalesce settings (accept one or a list) + sinks, through `ExecutionGraph.from_plugin_instances`. Write it concretely against the existing gate-construction idiom in the same file (see `test_multi_route_gate_suppresses_continue_edge` at :352 for `GateSettings` usage).

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/core/dag/test_builder_validation.py::TestWholeRosterForkClosure -v`
Expected: `test_pure_fan_out_fork_stays_legal` PASSES (already-legal shape); the three rejection tests FAIL (shapes currently build or die on the pre-existing :746/:763 checks with different messages).

- [ ] **Step 3: Implement in the builder** — after the fork-branch wiring loop (post :740), before the :742 block:

```python
    # ===== WHOLE-ROSTER FORK CLOSURE (spec §7 rule 2, ruling 23) =====
    # A fork is fully bound (every branch flows to its ONE closer, rosters
    # equal) or fully unbound (pure fan-out to sinks). Mixed closure and
    # multi-closer splits are build errors; subset closure can be added
    # additively later — the reverse narrowing never could be.
    for gate_entry in gate_entries:
        if not gate_entry.fork_to:
            continue
        closers: dict[str, str] = {}  # branch -> closer name ("coalesce:X" / "row_union:Y")
        unbound: list[str] = []
        for branch_name in gate_entry.fork_to:
            branch_key = BranchName(branch_name)
            if branch_key in coalesce_branch_specs:
                closers[branch_name] = f"coalesce:{coalesce_branch_specs[branch_key].coalesce_name}"
            elif branch_key in row_union_branch_specs:
                closers[branch_name] = f"row_union:{row_union_branch_specs[branch_key].row_union_name}"
            else:
                unbound.append(branch_name)
        if closers and unbound:
            raise GraphValidationError(
                f"Fork gate '{gate_entry.name}' has mixed closure: branches {sorted(closers)} close at a "
                f"barrier while branches {unbound} go direct to sinks. A fork is either fully bound — "
                f"every declared branch flows to the fork's single closer — or fully unbound (pure "
                f"fan-out). Route every branch to the closer, or none (spec §7 rule 2).",
                component_id=gate_entry.name,
                component_type="gate",
            )
        distinct_closers = sorted(set(closers.values()))
        if len(distinct_closers) > 1:
            raise GraphValidationError(
                f"Fork gate '{gate_entry.name}' closes at multiple barriers: {distinct_closers}. "
                f"A fork closes entirely at ONE closer (spec §7 rule 2). Split into nested forks — an "
                f"outer pure fan-out whose branches each contain their own fork→closer pair.",
                component_id=gate_entry.name,
                component_type="gate",
            )
        if distinct_closers:
            closer_label = distinct_closers[0]
            kind, _, closer_name = closer_label.partition(":")
            declared = (
                {str(b.branch_name) for b in coalesce_plans[CoalesceName(closer_name)].branches}
                if kind == "coalesce"
                else {str(b) for b, spec in row_union_branch_specs.items() if str(spec.row_union_name) == closer_name}
            )
            if declared != set(gate_entry.fork_to):
                raise GraphValidationError(
                    f"{'Coalesce' if kind == 'coalesce' else 'row_union'} '{closer_name}' roster mismatch: "
                    f"closer declares {sorted(declared)} but fork gate '{gate_entry.name}' declares "
                    f"{sorted(gate_entry.fork_to)}. Whole-roster closure requires the closer's branches "
                    f"to EQUAL the fork's branch list (spec §7 rule 2).",
                    component_id=closer_name,
                    component_type=kind,
                )
```

Note: the roster-equality limb subsumes builder.py:743's subset-direction check for bound forks; leave :742-757 in place (it still covers a coalesce whose branches NO gate produces).

- [ ] **Step 4: Run builder tests to pass**

Run: `pytest tests/unit/core/dag/test_builder_validation.py -q`
Expected: PASS, including all pre-existing tests (if an existing test used a mixed-fork VEHICLE for another rule, re-point its topology to a whole-roster shape rather than deleting it — the 2026-08-20 vehicle discipline).

- [ ] **Step 5: Composer Stage-1 mirror (same commit).** Re-read `web/composer/state.py` at HEAD first. In `validate()`, in the gate/topology section (anchor: the loop that computes `gate_fork_branches_by_id` — the same data the coalesce alias check uses), add per-fork-gate checks emitting:
  - `fork_mixed_closure_invalid` — some fork_to names appear as coalesce/row_union branch aliases and others match sink names;
  - `fork_multiple_closers_invalid` — fork_to aliases split across two closer nodes;
  - `fork_roster_mismatch` — the bound closer's branch alias set != the gate's fork_to set.

The composer has all three inputs already: gate `fork_to`, coalesce/row_union `branches` keys, sink names. Message texts mirror the builder's ("mixed closure", "closes at multiple barriers", "roster mismatch" — same discriminating phrases so `_VALIDATION_ERROR_PATTERNS` and `validationHumaniser.ts` match on stable headlines). Write the failing composer test FIRST in the new file:

```python
"""Stage-1 mirrors for the bound-region build rules (barrier-scopes spec §7 rule 10).

NEW FILE — test_state.py is under active maintainer edit; do not add to it.
"""

from __future__ import annotations

from tests.unit.web.composer._state_test_helpers import make_state  # if no such helper exists, build
# the minimal ComposerState the way test_state.py's fork/coalesce cases do — copy the construction
# idiom (set_source + upsert_node calls or direct state construction), not the file.


def test_mixed_fork_closure_is_rejected_with_code() -> None:
    state = _state_with_fork(fork_to=["failing", "survivor"],
                             coalesce_branches={"failing": "failing", "second": "second"},
                             sinks=["survivor", "out"])
    summary = state.validate()
    codes = {e.error_code for e in summary.errors}
    assert "fork_mixed_closure_invalid" in codes


def test_pure_fan_out_fork_validates_green() -> None:
    state = _state_with_fork(fork_to=["left", "right"], coalesce_branches=None, sinks=["left", "right"])
    summary = state.validate()
    codes = {e.error_code for e in summary.errors}
    assert "fork_mixed_closure_invalid" not in codes
    assert "fork_multiple_closers_invalid" not in codes
```

(`_state_with_fork` is a file-local helper built by reading the CURRENT construction idiom in test_state.py at execution time; the error-summary attribute names (`error_code`, `errors`) must be confirmed against `ValidationSummary` in state.py — adjust attribute access to the live contract, keeping the CODE assertions exactly as written.)

- [ ] **Step 6: generation.py catalogue entries.** Append to `_VALIDATION_ERROR_PATTERNS` (:370), after the `fork_branch_multiple_barriers` entry — LIST ORDER is match order, specific-first:

```python
    (
        r"fork_mixed_closure_invalid",
        "A fork routes some branches to a barrier and others direct to sinks. A fork is either fully "
        "bound (every branch reaches its one closer) or fully unbound (pure fan-out).",
        "Route every fork branch to the same coalesce/row_union, or route every branch direct to sinks. "
        "For a partial merge, use nested forks: an outer fan-out whose branch contains its own fork and closer.",
    ),
    (
        r"fork_multiple_closers_invalid",
        "One fork's branches close at two different barriers, so no single roster can settle the group.",
        "Give each barrier its own fork: split into nested forks, each closing whole-roster at one barrier.",
    ),
    (
        r"fork_roster_mismatch",
        "The barrier's declared branches do not equal the fork's declared branch list — whole-roster "
        "closure requires exact equality.",
        "Make the coalesce/row_union branches keys exactly the gate's fork_to list — same names, none "
        "added, none missing.",
    ),
```

- [ ] **Step 7: Run composer tests to pass, adjudicate parity**

Run: `pytest tests/unit/web/composer/test_state_bound_regions.py tests/unit/web/composer/test_tools_generation.py -q` (adjust the second selection to whatever suite pins `_VALIDATION_ERROR_PATTERNS` — `git grep -ln "_VALIDATION_ERROR_PATTERNS" tests/`).
Run: `.venv/bin/python scripts/cicd/runtime_rejection_parity.py --write` — adjudicate the three new builder sites as `mirrored` with counterparts `fork_mixed_closure_invalid` / `fork_multiple_closers_invalid` / `fork_roster_mismatch`.
Run: `pytest tests/unit/scripts/cicd/test_runtime_rejection_parity_gate.py -q` — Expected: PASS.

- [ ] **Step 7a: Migrate the `parallel-coalesces` corpus casualty (protocols RC-3 — SAME COMMIT as the rejection it flees).** Run the corpus suite to enumerate casualties first: `pytest tests/integration/core/dag/test_dag_scenario_production_path.py -q`. Expected failures: `parallel-coalesces` (rule 2, "closes at multiple barriers") and NOTHING else (`fork-multiple-terminals-partial-failure` is pure fan-out — LEGAL and permanently FROZEN per RC-2; if it fails, STOP: the rule 2 implementation is wrong, not the fixture). Then rewrite `tests/fixtures/dag_scenario_corpus/v1/parallel-coalesces/two-parallel-require-all.yaml` as nested depth-2 forks per RC-3's ratified replacement: ONE outer fork bound whole-roster to ONE outer coalesce `merge_all` over `[left_path, right_path]`, where each branch contains an inner whole-roster fork (`left_path` → inner gate fork_to `[left_a, left_b]` → `merge_left`, mirrored for the right) closing in-region before the outer coalesce. Preserve the scenario's pedagogical point (two require_all merges) and its row data; the projection changes wholesale. This also gives WS1's §4.1a differential tests their first true nested corpus fixture.

- [ ] **Step 7b: Adjudicated rotation + §S3 retirement (same commit).** Run the scenario; the harness failure message prints expected vs observed `projection_sha256`. Hand-rotate `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` for this scenario ONLY; append the dated A/B note to the rotation ledger in `tests/unit/architecture/test_dag_scenario_corpus_contract.py` ("2026-0X-XX parallel-coalesces migrated for ruling 23 — old topology build-rejected ('closes at multiple barriers'); rewritten as nested depth-2; A/B: old manifest verified against pre-WS2 HEAD <sha>, new against <sha>"); update `EXPECTED_CASE_REGISTRY_SHA256`. Retire the old shape per protocols §S3: `git mv tests/fixtures/dag_scenario_corpus/oracle_freeze/v1/parallel-coalesces tests/fixtures/dag_scenario_corpus/oracle_freeze/retired/parallel-coalesces` and write its `MIGRATION.md` (ruling, date, replacement scenario id — §S3's required fields; `tests/unit/architecture/test_oracle_freeze_registry.py` must stay green, it fails any orphan snapshot). The ledger entry is the tamper-vs-migration discriminator — budget it as real work. Re-run the corpus suite — green.

- [ ] **Step 8: Commit**

```bash
git add src/elspeth/core/dag/builder.py src/elspeth/web/composer/state.py \
        src/elspeth/web/composer/tools/generation.py tests/unit/core/dag/test_builder_validation.py \
        tests/unit/web/composer/test_state_bound_regions.py config/cicd/runtime_rejection_parity.yaml \
        tests/fixtures/dag_scenario_corpus/v1/parallel-coalesces/ \
        docs/architecture/dag/scenario-corpus/v1/manifest.yaml \
        tests/unit/architecture/test_dag_scenario_corpus_contract.py
# The §S3 `git mv` already staged the snapshot move; stage only the new MIGRATION.md:
git add tests/fixtures/dag_scenario_corpus/oracle_freeze/retired/parallel-coalesces/MIGRATION.md
git commit -m "feat(dag): whole-roster fork closure (ruling 23) with Stage-1 mirror + parallel-coalesces migration in the same commit (RC-3)"
```

---

### Task 7: Bidirectional SESE walk (rule 4)

**Files:**
- Modify: `src/elspeth/core/dag/bound_regions.py` — `validate_sese_regions(graph, regions)` called from the builder after `compute_bound_regions`
- Modify: `src/elspeth/core/dag/builder.py` — the call
- Modify: `src/elspeth/web/composer/state.py` + `tools/generation.py` — mirror codes (SYMBOL anchors; re-read first)
- Modify: `tests/unit/core/dag/canonical_hash_corpus.json` — RC-4 reclassification re-record (`settings_screened.yaml` moves `hashes` → `unbuildable`; same-slice migration, protocols RC-4)
- Test: `tests/unit/core/dag/test_bound_regions.py` (extend), `tests/unit/web/composer/test_state_bound_regions.py` (extend), `tests/integration/core/dag/test_bound_region_build_acceptance.py` (NEW — the RC-7 commissioned build-acceptance suite)

**Interfaces:**
- Consumes: Task 5's `BoundRegion`, `_forward_reach`.
- Produces: `def validate_sese_regions(graph: "ExecutionGraph", regions: tuple[BoundRegion, ...]) -> None` raising `GraphValidationError`; composer codes `bound_region_sink_inside`, `bound_region_no_path_to_closer`, `bound_region_external_entry`.

- [ ] **Step 1: Write the failing tests** (extend `test_bound_regions.py`)

```python
class TestSESEWalk:
    def test_sink_inside_bound_region_rejected(self) -> None:
        # A branch transform's on_success names a sink while its branch is
        # coalesce-bound — a path from the opener reaches a sink before the
        # closer ("rejected flat", spec §7 rule 4).
        with pytest.raises(GraphValidationError, match="reaches sink .* before the region's closer"):
            _build_fork_coalesce_with_in_region_sink()

    def test_on_error_divert_inside_region_stays_legal(self) -> None:
        # PINNED DECISION 1: the walk covers success-path edges only. A branch
        # transform with on_error routed to an OUTSIDE sink must build — the
        # loss fixtures' shape and the settlement system's input.
        graph = _build_fork_coalesce_with_branch_on_error_sink()
        assert graph is not None

    def test_external_entry_into_region_rejected(self) -> None:
        # A coalesce declaring one branch fed from OUTSIDE the fork's region
        # (the backward-walk violation; row_union precedent builder.py:1462-1527).
        with pytest.raises(GraphValidationError, match="originates outside the bound region"):
            _build_coalesce_with_external_branch_feed()
```

Helper topologies (real construction in the file): `_build_fork_coalesce_with_in_region_sink` — branch `path_a`'s transform has `on_success="out"` (a sink) while `path_a` is declared on the coalesce, using a transform-chain branch (`branches={"path_a": "path_a_out", ...}` with the transform emitting to the sink instead); `_build_fork_coalesce_with_branch_on_error_sink` — the Task-5 fork-coalesce graph with the branch transform's `on_error="errors"` and an `errors` sink added; `_build_coalesce_with_external_branch_feed` — a second source/transform chain producing the connection a coalesce branch maps, without traversing the fork.

- [ ] **Step 2: Run to verify failure** — `pytest tests/unit/core/dag/test_bound_regions.py::TestSESEWalk -v` — the two rejection tests FAIL (build succeeds or fails with a different pre-existing message), the legality test may already PASS (keep it as the pinned control).

- [ ] **Step 3: Implement `validate_sese_regions`**

```python
def validate_sese_regions(graph: "ExecutionGraph", regions: tuple[BoundRegion, ...]) -> None:
    """§7 rule 4 — bidirectional SESE, success-path edges only (pinned decision 1).

    Forward: every non-DIVERT path from the opener reaches the closer before
    any sink. Backward: every non-DIVERT edge into a region member
    originates at the opener or another member.
    """
    from elspeth.contracts.enums import NodeType

    for region in regions:
        binding = region.binding
        in_region = region.member_node_ids | {binding.opener_node_id, binding.closer_node_id}
        for member in region.member_node_ids:
            info = graph.get_node_info(member)
            if info.node_type is NodeType.SINK:
                raise GraphValidationError(
                    f"Bound region '{binding.closer_name}' (opener '{binding.opener_name}') reaches sink "
                    f"'{member}' before the region's closer. No token may leave a bound region except "
                    f"through its closer — sinks inside a bound region are rejected flat (spec §7 rule 4). "
                    f"Move the sink after the closer, or unbind the group.",
                    component_id=binding.closer_name,
                    component_type=binding.closer_kind,
                )
            closer_reachable = binding.closer_node_id in _forward_reach(graph, member, binding.closer_node_id)
            if not closer_reachable:
                raise GraphValidationError(
                    f"Node '{member}' inside bound region '{binding.closer_name}' has no success path to "
                    f"the region's closer. Every path from the opener must reach the closer "
                    f"(spec §7 rule 4).",
                    component_id=binding.closer_name,
                    component_type=binding.closer_kind,
                )
        entry_targets = region.member_node_ids | {binding.closer_node_id}
        for member in entry_targets:
            for edge in graph.get_incoming_edges(member):
                if edge.mode is RoutingMode.DIVERT:
                    continue
                origin = NodeID(edge.from_node)
                if origin not in in_region:
                    raise GraphValidationError(
                        f"Edge into '{member}' (bound region '{binding.closer_name}') originates outside "
                        f"the bound region at '{origin}'. Every path into an in-region node must "
                        f"originate at the opener '{binding.opener_name}' (spec §7 rule 4, backward walk; "
                        f"row_union precedent). Feed that input from inside the region, or move the "
                        f"consumer outside it.",
                        component_id=binding.closer_name,
                        component_type=binding.closer_kind,
                    )
```

Caveat to verify while implementing: the forward-membership set from Task 5 already excludes sinks reached WITHOUT a return path to the closer (forward ∩ backward drops them). The forward violation must therefore be detected on the FORWARD-ONLY set: compute `forward_only = _forward_reach(graph, opener, closer)` per region and run the sink/no-path checks over `forward_only - {closer}`, not over `member_node_ids`. Adjust the implementation accordingly and add a regression test pinning that a fork branch to a sink plus a bound sibling branch (mixed shape) is caught by Task 6 FIRST — ordering: rule 2 fires before rule 4 for that shape.

Builder call, after `graph.set_bound_regions(regions)`:

```python
    validate_sese_regions(graph, regions)
```

- [ ] **Step 4: Run to pass** — `pytest tests/unit/core/dag/test_bound_regions.py -v` — PASS. Then `pytest tests/unit/core/dag/ tests/unit/core/test_dag.py tests/unit/core/test_dag_row_union.py -q` — PASS (the pre-existing row_union walks must keep firing first for their shapes; if a message changed which error a test matches, tighten the TEST's topology, never relax the walk).

- [ ] **Step 5: Composer mirror + parity.** Stage 1 mirrors the SINK-inside-region limb only (the composer's connection-lineage helpers `_runtime_connection_lineage` / `_runtime_nodes_downstream_of_connection` make the forward walk over NodeSpecs tractable); the backward walk and no-path limb are adjudicated `abstains`. Add composer code `bound_region_sink_inside` (a coalesce/row_union/collector-bound branch chain that reaches a sink before its closer) with tests in `test_state_bound_regions.py`; generation.py entry:

```python
    (
        r"bound_region_sink_inside",
        "A path inside a bound group (fork→coalesce/row_union, or scope opener→collector) reaches a sink "
        "before the group's closer, so a member could leave the group without settling.",
        "Route the in-region chain to the group's closer and move the sink after it; only the closer "
        "releases tokens out of a bound region.",
    ),
```

`--write` + adjudicate: sink-inside limb `mirrored` (counterpart `bound_region_sink_inside`); no-path limb and backward limb `abstains` (note the narrower existing codes `row_union_branch_not_downstream` / `row_union_branch_origin_invalid` cover the row_union subset).
Run the parity gate test — PASS.

- [ ] **Step 5a: RC-7 build-acceptance test — the lost-branch fixtures stay buildable (protocols RC-7 commission).** The DIVERT-exclusion decision exists exactly so the loss fixtures — the settlement system's input — keep building; pin that with a dedicated suite iterating the lost-branch corpus fixtures through `build_execution_graph` with ALL new validation active. Create `tests/integration/core/dag/test_bound_region_build_acceptance.py`:

```python
"""RC-7 build-acceptance pin: the lost-branch corpus fixtures build under §7 validation.

The §7 rule 4 forward walk covers success-path and gate-route edges ONLY
(protocols RC-7; this plan's pinned decision 1). Every fork-coalesce loss
fixture terminates tokens in-region via on_error routing — that is the
settlement system's INPUT. If any of these stops building, the SESE walk
has started covering DIVERT edges: fix the WALK, never the fixtures.
"""

from __future__ import annotations

import pytest

from tests.fixtures.dag_scenario_corpus.harness import build_scenario, render_settings
from tests.fixtures.dag_scenario_corpus.loader import iter_harness_cases, load_manifest

_MANIFEST = load_manifest()
_LOST_BRANCH_CASES = [
    pytest.param(scenario, case, id=f"{scenario.id}:{case.id}")
    for scenario, case in iter_harness_cases(_MANIFEST)
    if scenario.id == "fork-coalesce-policies" and "lost" in case.fixture
]


def test_lost_branch_case_roster_is_pinned() -> None:
    # Protocols RC-7 speaks of "the 8 existing lost-branch corpus fixtures";
    # the live glob is authoritative — the fork-coalesce-policies scenario
    # carries 10 *lost* fixtures at plan time (best-effort-all-lost,
    # best-effort-{nested,select,union}-lost-c, first-all-lost,
    # quorum-impossible-lost-c, quorum-{nested,select,union}-lost-c,
    # require-all-lost-c). A shrinking roster means a fixture was renamed or
    # dropped — adjudicate, never shrug.
    assert len(_LOST_BRANCH_CASES) >= 8


@pytest.mark.parametrize(("scenario", "case"), _LOST_BRANCH_CASES)
def test_lost_branch_fixture_builds_under_bound_region_validation(scenario, case, tmp_path) -> None:
    built = build_scenario(render_settings(case, tmp_path))
    assert built.graph is not None
```

(Confirm at execution time: the corpus plugin manager setup — `install_corpus_plugin_manager` in `tests/fixtures/dag_scenario_corpus/plugins.py` — mirror however `test_dag_scenario_production_path.py` activates it (fixture vs direct call), and the `BuiltScenario` attribute name for the graph. Both are two-line greps; keep the assertions as written.)

Run: `pytest tests/integration/core/dag/test_bound_region_build_acceptance.py -q` — Expected: PASS, every case builds.

- [ ] **Step 5b: RC-4 fold — reclassify `examples/row_union_ab_experiment/settings_screened.yaml` (same-slice migration, protocols RC-4).** Rule 4 rejects it (the `quality_screen` gate inside the control branch routes `'false'` to the `screened_out` sink — a path from opener to sink before the closer) and there is NO mechanical migration: its pedagogical point is the prohibited shape. Re-record Task 1's corpus via the documented env var (`ELSPETH_CANONICAL_CORPUS_RECORD=1 pytest tests/unit/core/dag/test_canonical_hash_corpus.py -x`, then assert-mode green). **The replacement is RULED (maintainer, 2026-08-22): TWO variants, implemented in THIS commit** — (i) rewrite `settings_screened.yaml` as screen-BEFORE-fork on the source-known `baseline_quality >= 60` predicate (the `quality_screen` gate moves above `experiment_fork`; `screened_out` sink kept, now outside any region; run goes SUCCESS/exit 0; its corpus entry stays in `hashes` with a NEW recorded hash); (ii) add NEW `settings_screened_at_settlement.yaml` — the in-branch screen keys on the computed `score` (post-`tag_control`, unknowable pre-fork) and routes the screened row to discard so the settle-member seam stages the member loss and the `require_all` union fails that ticket's pair closed; its README states the costs (sibling billed then `scope_group_failed`; PARTIAL/exit 1 by design; screened rows recovered from `group_losses`/landscape, not a sink); it enters the corpus under `hashes`. NOTE: the settlement variant exercises WS3's settle-member seam at runtime — it BUILDS in this slice (that is what the corpus records) but its documented run output is only achievable after WS3 lands; mark its README accordingly and wire its full-run check into the WS3+WS4 integration item. Rewrite both variants' README output-count prose (routed counts, disposition names — the comparison statistics section survives verbatim) and update `examples/AGENTS.md` run notes. Verify the re-recorded corpus JSON moves EXACTLY: `settings_screened.yaml` re-hashed + `settings_screened_at_settlement.yaml` added; any other mover is an unintended break — STOP and fix before committing.

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/core/dag/bound_regions.py src/elspeth/core/dag/builder.py \
        src/elspeth/web/composer/state.py src/elspeth/web/composer/tools/generation.py \
        tests/unit/core/dag/test_bound_regions.py tests/unit/web/composer/test_state_bound_regions.py \
        tests/integration/core/dag/test_bound_region_build_acceptance.py \
        tests/unit/core/dag/canonical_hash_corpus.json \
        config/cicd/runtime_rejection_parity.yaml
git commit -m "feat(dag): bidirectional SESE walk, success-path edges only (spec §7 rule 4); RC-4 reclassification + RC-7 build-acceptance pin"
```
(If the maintainer's RC-4 redesign choice landed in Step 5b, add the touched `examples/row_union_ab_experiment/` files and `examples/AGENTS.md` to the pathspec.)

---

### Task 8: Rule 5 (ruling 28) — every opener inside a bound region is bound and closes in-region

**Files:**
- Modify: `src/elspeth/core/dag/bound_regions.py` — `validate_openers_bound_in_region(graph, regions, registry, multi_row_node_ids)`
- Modify: `src/elspeth/core/dag/builder.py` — compute `multi_row_node_ids` from `WiredTransform.plugin.creates_tokens` and call the validator
- Modify: composer `state.py` + `generation.py` (mirror where knowable; else abstain)
- Test: `tests/unit/core/dag/test_bound_regions.py` (extend)

**Interfaces:**
- Consumes: Tasks 4/5 regions + registry; `TransformProtocol.creates_tokens` (contracts/plugin_protocols.py:393).
- Produces: `GraphValidationError` message beginning `"Multi-row transform '{name}' inside bound region"`; composer code `bound_region_undeclared_expand` (or `abstains` — see step 4).

- [ ] **Step 1: Write the failing tests**

```python
class TestRule5OpenersBoundInRegion:
    def test_undeclared_expand_inside_coalesce_branch_rejected(self) -> None:
        # A creates_tokens=True transform inside a fork→coalesce region with
        # no scope — legal today (binding-survives-expansion posture,
        # token_traversal.py:254-262), a GraphValidationError under ruling 28.
        with pytest.raises(GraphValidationError, match="Multi-row transform .* inside bound region"):
            _build_fork_coalesce_with_undeclared_expand_in_branch()

    def test_declared_scope_closing_in_region_is_legal(self) -> None:
        # Same shape but the expand is a declared scope whose collector closes
        # BEFORE the coalesce (batch-in-fork-line, legal under ruling 28).
        graph = _build_fork_coalesce_with_scoped_expand_in_branch()
        regions = graph.get_bound_regions()
        # Compare against the CloserKind MEMBERS (StrEnum) — the WS3 discipline.
        assert {r.binding.closer_kind for r in regions} == {CloserKind.COALESCE, CloserKind.COLLECTOR}
        assert max(r.depth for r in regions) == 2

    def test_scoped_expand_whose_collector_sits_outside_region_rejected(self) -> None:
        # Opener in the branch, collector AFTER the coalesce: rule 3/5 violation —
        # expect the targeted rule-5 message, not a bare overlap error.
        with pytest.raises(GraphValidationError, match="closes outside"):
            _build_fork_coalesce_with_scope_closing_outside()

    def test_top_level_undeclared_expand_stays_legal(self) -> None:
        # Inert expands OUTSIDE bound regions are the batch posture — untouched.
        graph = _build_plain_expand_pipeline()  # source → json_explode-style stub → sink, no scope
        assert graph.get_bound_regions() == ()
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/unit/core/dag/test_bound_regions.py::TestRule5OpenersBoundInRegion -v` — rejection tests FAIL (shapes currently build).

- [ ] **Step 3: Implement**

Builder — collect once, near the transform loop (transform node ids are in `transform_ids_by_name`):

```python
    multi_row_node_ids: dict[NodeID, str] = {
        transform_ids_by_name[wired.settings.name]: wired.settings.name
        for wired in transforms
        if wired.plugin.creates_tokens
    }
```

NOTE — direct attribute access ONLY: `creates_tokens` is a declared protocol field (plugin_protocols.py:393), so `wired.plugin.creates_tokens` is direct access on an owned contract. A `getattr(..., "creates_tokens", False)` "just to be safe" would trip the masquerade gate (whole-tree; tests included) — if a test stub lacks the attribute, fix the STUB to model the contract, never the production code.

`bound_regions.py`:

```python
def validate_openers_bound_in_region(
    graph: "ExecutionGraph",
    regions: tuple[BoundRegion, ...],
    registry: GroupBindingRegistry,
    multi_row_node_ids: Mapping[NodeID, str],
) -> None:
    """§7 rule 5 (ruling 28): a shape change inside a group must itself be a group.

    Every token-creating node inside a bound region must be a declared
    opener whose closer is ALSO inside that region. Undeclared expands stay
    legal outside bound regions (the batch posture).
    """
    bound_openers = registry.by_opener_node()
    for region in regions:
        binding = region.binding
        for node_id, transform_name in multi_row_node_ids.items():
            if node_id not in region.member_node_ids:
                continue
            inner = bound_openers.get(node_id)
            if inner is None:
                raise GraphValidationError(
                    f"Multi-row transform '{transform_name}' inside bound region '{binding.closer_name}' "
                    f"is not a declared scope opener. Inside a bound region, a shape change must itself "
                    f"be a group (spec §7 rule 5, ruling 28): wrap '{transform_name}' in a scopes: entry "
                    f"whose collector closes before '{binding.closer_name}'.",
                    component_id=transform_name,
                    component_type="transform",
                )
            if inner.closer_node_id not in region.member_node_ids:
                raise GraphValidationError(
                    f"Scope opener '{transform_name}' sits inside bound region '{binding.closer_name}' "
                    f"but its collector '{inner.closer_name}' closes outside it. An inner group must "
                    f"close before the enclosing region's closer (spec §7 rules 3/5).",
                    component_id=inner.closer_name,
                    component_type="collector",
                )
```

Builder call after `validate_sese_regions(graph, regions)`:

```python
    validate_openers_bound_in_region(graph, regions, registry, multi_row_node_ids)
```

(Fork gates inside regions need no arm here: an in-region fork either closes at an in-region coalesce (well-nested, legal), closes outside (rule 3 partial overlap), or fans out to sinks (rule 4 sink-inside). Add a comment saying exactly that so the next reader does not "complete" the rule.)

- [ ] **Step 4: Composer mirror decision + parity.** Stage 1 cannot see `creates_tokens` without a plugin probe; check whether `_semantic_validator._instantiate_consumer` (state.py) already constructs transform plugins during validate — it does (recent-code-hints 2026-08-21, "the composer's probes DO construct the node"). If the constructed instance is reachable where the topology checks run, mirror with code `bound_region_undeclared_expand`; if the probe result is not plumbed to the topology section, adjudicate `abstains` with note "requires plugin instantiation in the topology pass; Stage 2 preview_pipeline rejects via the runtime builder" and add the generation.py entry anyway (preview_pipeline surfaces the runtime message text, so the catalogue must translate it):

```python
    (
        r"bound_region_undeclared_expand|Multi-row transform '(.+)' inside bound region",
        "A row-expanding transform sits inside a bound group without being a declared scope, so the "
        "group's roster cannot account for its children.",
        "Add a scopes: entry: name the expanding transform as opener and bind a collector that closes "
        "before the enclosing group's closer (coalesce/row_union/collector).",
    ),
```

`--write` + adjudicate both new sites (`mirrored` or `abstains` per the probe finding). Parity gate test — PASS.

- [ ] **Step 5: Run to pass, commit**

Run: `pytest tests/unit/core/dag/test_bound_regions.py -q` — PASS.

```bash
git add src/elspeth/core/dag/bound_regions.py src/elspeth/core/dag/builder.py \
        src/elspeth/web/composer/state.py src/elspeth/web/composer/tools/generation.py \
        tests/unit/core/dag/test_bound_regions.py tests/unit/web/composer/test_state_bound_regions.py \
        config/cicd/runtime_rejection_parity.yaml
git commit -m "feat(dag): ruling 28 — every opener inside a bound region is bound and closes in-region"
```

---

### Task 9: Rule 6 (ruling 25) — aggregators banned inside ALL bound regions

**Files:**
- Modify: `src/elspeth/core/dag/bound_regions.py` — `validate_no_aggregations_in_regions(graph, regions, aggregation_node_ids)`
- Modify: `src/elspeth/core/dag/builder.py` — call with `aggregation_ids.values()`
- Modify: composer `state.py` + `generation.py` — mirror `bound_region_aggregation_invalid`
- Test: `tests/unit/core/dag/test_bound_regions.py` (extend), `tests/unit/web/composer/test_state_bound_regions.py` (extend)

**Interfaces:**
- Consumes: builder local `aggregation_ids: dict[AggregationName, NodeID]` (:414); Task 5 regions.
- Produces: `GraphValidationError` `"Aggregation '{name}' is inside bound region '{closer}'"`; composer code `bound_region_aggregation_invalid`.

- [ ] **Step 1: Failing tests**

```python
class TestRule6AggregatorBan:
    def test_aggregation_inside_coalesce_branch_rejected_both_modes(self) -> None:
        for output_mode in ("transform", "passthrough"):
            with pytest.raises(GraphValidationError, match="Aggregation .* inside bound region"):
                _build_fork_coalesce_with_branch_aggregation(output_mode=output_mode)

    def test_aggregation_outside_regions_stays_legal(self) -> None:
        # Top-level aggregation, and aggregation AFTER a closer's release —
        # both remain legal (ADR-020 posture; the corpus fixtures' shapes).
        assert _build_top_level_aggregation_pipeline() is not None
        assert _build_aggregation_after_coalesce_release() is not None
```

Note the discriminating strength: the existing row_union walk (builder.py:1504-1517) rejects only transform-mode aggregations in ROW_UNION branches; the new rule must catch passthrough mode and coalesce regions — the first test iterates both modes deliberately.

- [ ] **Step 2: Run to verify failure** — passthrough-mode and coalesce-region cases FAIL to raise today.

- [ ] **Step 3: Implement**

```python
def validate_no_aggregations_in_regions(
    graph: "ExecutionGraph",
    regions: tuple[BoundRegion, ...],
    aggregation_node_ids: Mapping[NodeID, str],
) -> None:
    """§7 rule 6 (ruling 25): aggregators are windows, not closers — banned in
    every bound region, BOTH output modes, every closer kind's region. Closes
    the BATCH_CONSUMED loss-blindness gap (a lost batch member invisible to a
    roster). Outside bound regions no roster is watching — unchanged."""
    for region in regions:
        for node_id, agg_name in aggregation_node_ids.items():
            if node_id in region.member_node_ids:
                raise GraphValidationError(
                    f"Aggregation '{agg_name}' is inside bound region '{region.binding.closer_name}' "
                    f"(opener '{region.binding.opener_name}'). Aggregators are banned inside all bound "
                    f"regions (spec §7 rule 6, ruling 25): a batch flush consumes members the roster "
                    f"must account for. Move the aggregation before the opener or after the closer; "
                    f"for an in-region N->M batch, use a scoped multi-row transform with a collector.",
                    component_id=agg_name,
                    component_type="aggregation",
                )
```

Builder call (pass `{aid: str(name) for name, aid in aggregation_ids.items()}` inverted appropriately) after the Task-8 call.

- [ ] **Step 4: Composer mirror.** The composer HAS aggregation nodes and branch lineage helpers — mirror as `bound_region_aggregation_invalid` for coalesce-bound branches (the row_union twin `row_union_branch_aggregation_invalid` already exists and stays; put the new check beside it, keyed to coalesce/collector regions). generation.py entry:

```python
    (
        r"bound_region_aggregation_invalid",
        "An aggregation sits inside a bound group (fork→coalesce or scope). A batch flush consumes "
        "members the group's roster must account for, so losses inside the batch would be invisible.",
        "Move the aggregation before the group's opener or after its closer; batch work inside a bound "
        "region belongs to a scoped multi-row transform closed by a collector.",
    ),
```

- [ ] **Step 5: Parity (`mirrored`, counterpart `bound_region_aggregation_invalid`), run all, commit**

Run: `pytest tests/unit/core/dag/test_bound_regions.py tests/unit/web/composer/test_state_bound_regions.py tests/unit/scripts/cicd/test_runtime_rejection_parity_gate.py -q` — PASS.

```bash
git add src/elspeth/core/dag/bound_regions.py src/elspeth/core/dag/builder.py \
        src/elspeth/web/composer/state.py src/elspeth/web/composer/tools/generation.py \
        tests/unit/core/dag/test_bound_regions.py tests/unit/web/composer/test_state_bound_regions.py \
        config/cicd/runtime_rejection_parity.yaml
git commit -m "feat(dag): ruling 25 — aggregators banned inside all bound regions"
```

---

### Task 10: Rules 7 + 8 — roster authority (structural) and escalate-at-outermost

**Files:**
- Modify: `src/elspeth/core/dag/bound_regions.py` — `validate_escalation_targets(regions)`
- Modify: `src/elspeth/core/dag/builder.py` — call
- Modify: composer `state.py` + `generation.py` — mirror `scope_escalate_at_outermost`
- Test: `tests/unit/core/dag/test_bound_regions.py` (extend)

**Interfaces:**
- Consumes: Task 5 regions (`depth`, `binding.on_group_failure`).
- Produces: `GraphValidationError` `"Scope '{name}' declares on_group_failure: escalate at an outermost bound group"`; composer code `scope_escalate_at_outermost`.

- [ ] **Step 1: Failing test**

```python
class TestEscalateAtOutermost:
    def test_escalate_on_outermost_scope_rejected(self) -> None:
        with pytest.raises(GraphValidationError, match="escalate at an outermost bound group"):
            _build_top_level_scope(on_group_failure="escalate")

    def test_escalate_on_nested_scope_is_legal(self) -> None:
        # Scope inside a fork→coalesce region: an enclosing bound frame exists.
        graph = _build_fork_coalesce_with_scoped_expand_in_branch(on_group_failure="escalate")
        assert graph is not None

    def test_quarantine_on_outermost_scope_is_legal(self) -> None:
        assert _build_top_level_scope(on_group_failure="quarantine") is not None
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: implement**

```python
def validate_escalation_targets(regions: tuple[BoundRegion, ...]) -> None:
    """§7 rule 8 (standing ruling 2): escalate requires an enclosing bound
    group; outermost closers declare terminal handling."""
    for region in regions:
        if region.binding.on_group_failure == "escalate" and region.depth == 1:
            raise GraphValidationError(
                f"Scope '{region.binding.closer_name}' declares on_group_failure: escalate at an "
                f"outermost bound group — there is no enclosing bound group to escalate to "
                f"(spec §7 rule 8). Use quarantine (terminal handling) at the outermost level, or "
                f"nest this scope inside another bound group.",
                component_id=region.binding.closer_name,
                component_type=region.binding.closer_kind,
            )
```

Rule 7 (roster authority) needs NO new raise: `ScopeSettings.policy` and `CoalesceSettings.policy` Literals already confine `require_all` to closers with a roster authority, and `AggregationSettings` has no policy field at all (stays policy-free). Record that as a comment beside the call site — "rule 7 is structural: the policy vocabularies are closed per closer kind (spec §2); do not add a runtime check that can never fire" — and pin it with a test asserting `"policy" not in AggregationSettings.model_fields`.

- [ ] **Step 4: Composer mirror** — once Task 12 lands the composer scope fields, Stage 1 can mirror this exactly (scope with escalate + no enclosing bound group). SEQUENCING NOTE: if Task 12 has not run yet, adjudicate `not_authorable` now and RE-adjudicate to `mirrored` in Task 12 (the composer cannot author scopes at all before then). generation.py entry lands now (preview_pipeline already surfaces the runtime text):

```python
    (
        r"scope_escalate_at_outermost|escalate at an outermost bound group",
        "The scope's failure policy escalates to an enclosing group, but the scope is outermost — "
        "nothing encloses it.",
        "Set on_group_failure: quarantine on the outermost scope, or nest it inside a fork→coalesce "
        "or another scope that will receive the escalation.",
    ),
```

- [ ] **Step 5: Parity + run + commit**

```bash
git add src/elspeth/core/dag/bound_regions.py src/elspeth/core/dag/builder.py \
        src/elspeth/web/composer/tools/generation.py tests/unit/core/dag/test_bound_regions.py \
        config/cicd/runtime_rejection_parity.yaml
git commit -m "feat(dag): escalate-at-outermost is a build error (spec §7 rule 8); rule 7 pinned structural"
```

---

### Task 11: Rule 9 — `on_error` may target the enclosing region's closer

**Files:**
- Modify: `src/elspeth/core/dag/builder.py` — the transform error-edge block (:1144-1162) and gate error-edge block (:1164-1185) defer closer-named targets; new resolution pass after region computation
- Modify: `src/elspeth/core/config.py` — docstring updates ONLY on `TransformSettings.on_error` (:1330-1332) and `GateSettings.validate_on_error` (:834-843): "…a sink name, 'discard', or — from inside a bound region — that region's closer" (the parse-time validators already accept closer-shaped names via `_validate_connection_or_sink_name`; no behavioural parse change)
- Modify: composer `state.py` — RELAX the Stage-1 on_error check to accept an enclosing closer name (drift direction warning below) + `generation.py` entry
- Test: `tests/unit/core/dag/test_builder_validation.py` (extend), `tests/unit/web/composer/test_state_bound_regions.py` (extend)

**Interfaces:**
- Consumes: Tasks 4/5 registry + regions; builder locals `coalesce_ids`, `row_union_ids`, `collector_ids`, `sink_ids`, `transform_ids_by_name`, `config_gate_ids`.
- Produces: DIVERT edges into closer nodes (label `error_edge_label(<node name>)`, the existing helper); `GraphValidationError` `"…on_error '{name}' names closer '{name}' but '{node}' is not inside that closer's bound region"`.

- [ ] **Step 1: Failing tests**

```python
class TestOnErrorCloserTargets:
    def test_in_region_transform_may_name_its_closer(self) -> None:
        graph = _build_fork_coalesce_with_branch_on_error_to_closer()
        # DIVERT edge into the coalesce node exists:
        closer_id = graph.get_coalesce_id_map()[CoalesceName("merge")]
        divert_labels = {e.label for e in graph.get_incoming_edges(closer_id) if e.mode is RoutingMode.DIVERT}
        assert any("branch_a_transform" in label for label in divert_labels)

    def test_out_of_region_transform_naming_closer_rejected(self) -> None:
        with pytest.raises(GraphValidationError, match="not inside that closer's bound region"):
            _build_out_of_region_on_error_to_closer()

    def test_unknown_on_error_sink_message_unchanged(self) -> None:
        # The pre-existing unknown-sink rejection (builder.py:1151) must keep
        # firing for names that are neither sinks nor closers.
        with pytest.raises(GraphValidationError, match="references unknown sink"):
            _build_transform_with_bogus_on_error()
```

- [ ] **Step 2: Run to verify failure** — first test FAILS today ("references unknown sink" fires for the closer name).

- [ ] **Step 3: Implement.** In the transform error-edge block (:1144-1162), before the unknown-sink raise:

```python
    deferred_error_closer_targets: list[tuple[NodeID, str, str, str]] = []  # (node_id, node_name, kind, target)
    closer_name_to_node: dict[str, NodeID] = {
        **{str(name): nid for name, nid in coalesce_ids.items()},
        **{str(name): nid for name, nid in row_union_ids.items()},
        **{str(name): nid for name, nid in collector_ids.items()},
    }
```
(hoist above the block), then in the loop:

```python
        if on_error != "discard":
            if SinkName(on_error) not in sink_ids:
                if on_error in closer_name_to_node:
                    deferred_error_closer_targets.append(
                        (transform_ids_by_name[wired.settings.name], wired.settings.name, "transform", on_error)
                    )
                    continue
                ...existing unknown-sink raise unchanged...
```

Same deferral in the gate block (:1164-1185). Then, in the validation section AFTER `compute_bound_regions` (Tasks 5–10 calls), resolve:

```python
    # ===== RULE 9: on_error → enclosing closer (spec §7 rule 9) =====
    regions_by_closer = {r.binding.closer_node_id: r for r in regions}
    for node_id_, node_name, kind, target in deferred_error_closer_targets:
        closer_node = closer_name_to_node[target]
        region = regions_by_closer.get(closer_node)
        if region is None or node_id_ not in region.member_node_ids:
            raise GraphValidationError(
                f"{kind.capitalize()} '{node_name}' on_error '{target}' names closer '{target}' but "
                f"'{node_name}' is not inside that closer's bound region. A closer is a legal on_error "
                f"target only from inside its own region (spec §7 rule 9). Use a sink, 'discard', or "
                f"move the node inside the region.",
                component_id=node_name,
                component_type=kind,
            )
        graph.add_edge(node_id_, closer_node, label=error_edge_label(node_name), mode=RoutingMode.DIVERT)
```

(The DIVERT-into-non-sink shape is exactly what `schema_validation.py:1181-1193` documents and defends for the public `add_edge` surface — cite that comment in the builder comment. Runtime semantics of the edge land in WS3; until then the edge is a structural audit marker, same as every DIVERT edge.)

- [ ] **Step 4: Composer side — RELAX, don't tighten.** Find the Stage-1 check that validates transform/gate `on_error` against sink names (grep `on_error` in state.py's validation section at execution time). Teach it: a closer name is acceptable when the node's chain lies inside that closer's region (reuse the Task-7 mirror's lineage machinery; where region membership is not computable in Stage 1, ACCEPT the closer name whenever it names any coalesce/row_union/collector node — accepting is safe: Stage 2 `preview_pipeline` runs the real builder and rejects out-of-region targets with the runtime message. Rejecting here while the runtime accepts would be composer-red/runtime-green, the drift direction that strands the authoring loop). Add `test_state_bound_regions.py` cases for both directions. generation.py entry:

```python
    (
        r"on_error_closer_out_of_region|names closer .* but .* is not inside that closer's bound region",
        "on_error names a barrier closer, but the failing node is not inside that closer's bound region, "
        "so the closer has no membership to settle for it.",
        "From inside a bound region, on_error may name the region's own closer; elsewhere use a sink "
        "name or 'discard'.",
    ),
```

- [ ] **Step 5: Parity** — the new builder raise: `mirrored` if the Stage-1 relax includes the membership check, else `abstains` with the accept-is-safe note. Gate test PASS.

- [ ] **Step 6: SLICE BOUNDARY — full suite + gates + hash corpus** (same commands as Task 3 Step 6). Expected: green AS SEQUENCED — the RC-3 corpus migration (Task 6's commit) and the RC-4 reclassification (Task 7's commit) already landed in their own slices, so the scenario corpus is green against the rotated manifest and Task 1's corpus JSON is green as committed, carrying exactly ONE adjudicated roster move (`settings_screened.yaml` → `unbuildable`); any FURTHER hash or roster mover is a defect. `test_state_serialisation_contract.py` untouched-green; trust-tier count unchanged; wardline 0.

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/core/dag/builder.py src/elspeth/core/config.py \
        src/elspeth/web/composer/state.py src/elspeth/web/composer/tools/generation.py \
        tests/unit/core/dag/test_builder_validation.py tests/unit/web/composer/test_state_bound_regions.py \
        config/cicd/runtime_rejection_parity.yaml
git commit -m "feat(dag): on_error may target the enclosing region's closer (spec §7 rule 9)"
```

---

### Task 12: Composer authoring surface for collectors/scopes (`NodeSpec` + importer + YAML generation + serialisation contract)

**Files:**
- Modify: `src/elspeth/web/composer/state.py` — `NodeType` Literal (:68) + `COMPOSER_NODE_TYPES` (:72) gain `"collector"`; `NodeSpec` gains `scope_name`, `scope_opener`, `scope_policy`, `scope_on_group_failure` (all `str | None = None`); `__post_init__` collector normalisation; `from_dict`/`to_dict` omitted-when-None; `_SECTION_LIMITS` (:389) + the section map (:397) gain `"collectors": 100` / `"collector": "collectors"`; intrinsic validation for collector nodes (codes below)
- Modify: `src/elspeth/web/composer/yaml_importer.py` — `collectors:`/`scopes:` sections fold into collector NodeSpecs
- Modify: the YAML generation path (`git grep -ln "def generate_yaml\|row_unions:" src/elspeth/web/composer/` — emit `collectors:` + `scopes:` sections from collector NodeSpecs)
- Modify: `src/elspeth/web/composer/tools/generation.py` — intrinsic-code entries
- Modify: `config/cicd/runtime_rejection_parity.yaml` — RE-adjudicate Task 2/3's `not_authorable` entries to `mirrored`
- Test: `tests/unit/web/composer/test_state_collectors.py` (new), `tests/unit/web/composer/test_state_serialisation_contract.py` (extend — additive only)

**Interfaces:**
- Consumes: Task 2 settings shapes (the YAML sections to import/emit).
- Produces: `NodeSpec(node_type="collector", plugin=..., input=..., on_success=..., on_error=..., scope_name=..., scope_opener=..., scope_policy=..., scope_on_group_failure=...)`; new intrinsic codes `collector_missing_scope`, `collector_scope_policy_invalid`, `scope_opener_unknown`, `collector_has_trigger_invalid`; serialized dict omits every `scope_*` key when None.

- [ ] **Step 1: Failing serialisation-contract additions FIRST** (this is the hash gate): in `test_state_serialisation_contract.py`, add (a) a round-trip case for a collector NodeSpec asserting `to_dict` omits absent `scope_*` keys and `from_dict(to_dict(spec)) == spec`; (b) a NEW pinned `composition_content_hash` for one representative collector-bearing composition; (c) assert every EXISTING pinned hash in the file is untouched (they already assert themselves — just do not edit them). Also extend the file's AST check expectations if it pins `dataclasses.fields()`-driven `from_dict` reads (it does — the new fields must appear in `from_dict` exactly as declared).
- [ ] **Step 2: Run** `pytest tests/unit/web/composer/test_state_serialisation_contract.py -q` — new cases FAIL (unknown field), existing PASS.
- [ ] **Step 3: Implement the NodeSpec extension.** Follow the coalesce-fields precedent exactly (flat optionals + `__post_init__` defaulting): in `__post_init__`, for `node_type == "collector"` default `scope_policy` — NO. Do NOT default `scope_policy`: `ScopeSettings.policy` is REQUIRED with no default (spec §3), and the 2026-08-20 hint warns a Stage-1 default must only RECORD a runtime default, never invent one. Default only `scope_on_group_failure` to `"quarantine"` (mirrors the runtime default). Intrinsic validation (beside the coalesce/row_union intrinsic checks): a collector missing any of `scope_name`/`scope_opener`/`scope_policy` → `collector_missing_scope`; `scope_policy` outside `{"require_all","best_effort"}` → `collector_scope_policy_invalid`; `scope_opener` not naming a transform node in the draft → `scope_opener_unknown`; a collector NodeSpec carrying `trigger` → `collector_has_trigger_invalid`. Serialize `scope_*` omitted-when-None in `to_dict`, read them in `from_dict` with `.get(..., None)`.
- [ ] **Step 4: yaml_importer + generate_yaml.** Importer: `collectors:` entries become collector NodeSpecs; `scopes:` entries locate their closer's NodeSpec and populate its `scope_*` fields (a scope whose closer matches no collector → the importer's existing unknown-reference error path). Generator: emit each collector NodeSpec as one `collectors:` entry (name/plugin/input/on_success/on_error/options) plus one `scopes:` entry (name/opener/closer/policy/on_group_failure). Round-trip test: settings-YAML → importer → state → generate_yaml → `load_settings_from_yaml_string` → assert `collectors`/`scopes` equal the originals.
- [ ] **Step 5: generation.py intrinsic entries** for the four new codes (same catalogue style as Task 6's; write all four with concrete remedy text naming the exact fields to set).
- [ ] **Step 6: RE-adjudicate parity.** `--write`, then flip Task 2/3's collector/scope entries from `not_authorable` to `mirrored` with the new intrinsic codes as counterparts (e.g. collector-without-scope ↔ `collector_missing_scope`; `ScopeSettings.policy` Literal ↔ `collector_scope_policy_invalid`); Task 10's `scope_escalate_at_outermost` becomes mirrorable — implement that Stage-1 check now (scope_on_group_failure == "escalate" on a collector whose chain sits in no enclosing bound group) and flip its entry to `mirrored`.
- [ ] **Step 7: Run** `pytest tests/unit/web/composer/ -q` — PASS (full composer selection; the capability-skill identity test WILL fail here — that is Task 13's subject; if it fails in this task's run, proceed to Task 13 before committing, or commit both tasks together if the gate is same-commit atomic — check `test_capability_skill_identity`'s failure message first).
- [ ] **Step 8: Commit** (pathspec: the files above + new tests).

```bash
git add src/elspeth/web/composer/state.py src/elspeth/web/composer/yaml_importer.py \
        src/elspeth/web/composer/tools/generation.py tests/unit/web/composer/test_state_collectors.py \
        tests/unit/web/composer/test_state_serialisation_contract.py config/cicd/runtime_rejection_parity.yaml
git commit -m "feat(composer): collector node kind with scope binding fields, importer + YAML round-trip"
```

---

### Task 13: The composer three-pin (capability inventory, redaction snapshot, frontend decoder)

**Files:**
- Modify: `src/elspeth/web/composer/skills/pipeline_capabilities.md` — canonical-field-inventory table rows for the four `scope_*` NodeSpec fields + the `collector` node kind
- Modify: `src/elspeth/web/composer/redaction.py` — declare the new serialized keys on the NodeSpec-bearing response models (non-Sensitive: scope fields are structural names, not payload)
- Regenerate: `tests/unit/web/composer/redaction_policy_snapshot.json` via `scripts/cicd/bootstrap_redaction_snapshot.py --write` — NEVER by hand
- Modify: `src/elspeth/web/frontend/src/api/guidedDecoder.ts` — `decodeCompositionState`'s `exactRecord` key lists gain the `scope_*` keys and the `collector` node kind
- Test: existing gates (`test_capability_skill_identity`, redaction snapshot tests, frontend decoder tests — `git grep -ln "exactRecord" src/elspeth/web/frontend/src/`)

**Interfaces:**
- Consumes: Task 12's field names exactly: `scope_name`, `scope_opener`, `scope_policy`, `scope_on_group_failure`.
- Produces: three-pin consistency; the guided planner lane inherits the schema via `canonical_set_pipeline_schema()` automatically once the `set_pipeline` JSON schema + redaction models carry the fields (2026-08-15 hint).

- [ ] **Step 1:** Run `pytest tests/unit/web/composer/ -q -k "capability or redaction"` — observe the exact diffs the gates report (they derive the real schema and diff the table/snapshot).
- [ ] **Step 2:** Update `pipeline_capabilities.md`'s canonical-field-inventory table with the four new rows + node-kind row, matching the gate's derived format exactly.
- [ ] **Step 3:** Declare the keys in `redaction.py` on the models the gate names, then `python scripts/cicd/bootstrap_redaction_snapshot.py --write`. REVIEW the regenerated snapshot: only hashes may move; `sensitive_path_count` must NOT move (scope fields are non-sensitive structural names). If `check_redaction_direction.py` verdicts `weaken` on a same-count hash move, that is disposition (c) of the 2026-08-18 hint — the PR needs the `policy-weaken-justified` label + exact-phrase rationale, not a code workaround.
- [ ] **Step 4:** `guidedDecoder.ts`: add the keys to the node-record `exactRecord` list (grep `exactRecord` in the file; the lists REJECT unenumerated keys at runtime — missing this is invisible to every backend suite and breaks every `/guided` response after deploy; elspeth-b48212113e). Also grep the frontend for sibling `exactRecord` lists naming NodeSpec keys and update all of them. Run the frontend test suite per the repo's frontend test command (`git grep -n "test" src/elspeth/web/frontend/package.json` for the exact script).
- [ ] **Step 5:** Run `pytest tests/unit/web/ -q` — PASS. Commit:

```bash
git add src/elspeth/web/composer/skills/pipeline_capabilities.md src/elspeth/web/composer/redaction.py \
        tests/unit/web/composer/redaction_policy_snapshot.json src/elspeth/web/frontend/src/api/guidedDecoder.ts
git commit -m "feat(composer): three-pin for collector/scope fields — capability inventory, redaction snapshot, guided decoder"
```
(Include any sibling frontend files from step 4 in the pathspec.)

---

### Task 14: RC-5 casualty-grep drift RE-check (verification only)

The ruling-casualty migrations themselves ride the slices that outlaw their shapes —
protocols RC-3/RC-4 same-slice discipline: the `parallel-coalesces` rewrite landed in
Task 6's commit, the `settings_screened.yaml` reclassification in Task 7's commit. What
remains here is the protocols RC-5 drift RE-check: the casualty inventory was verified
at Task 6 Step 0, and Tasks 7–13 have moved HEAD since — confirm no new casualty
crept in behind the rejections.

**Files:** none (verification only; a new casualty becomes its own adjudication, never a
quiet fix here).

**Interfaces:**
- Consumes: Tasks 6–9's landed rejections; the protocols RC worklist.
- Produces: a recorded clean re-check (or a STOP with a named new casualty).

- [ ] **Step 1:** Re-run the RC-5 grep at current HEAD: `git grep -l "fork_to" examples/ tests/fixtures/dag_scenario_corpus/v1/` and inspect each hit for (i) aggregation nodes inside a fork/union branch (r25), (ii) multi-row transforms inside a bound branch (r28), (iii) branch subsets closing at a coalesce (r23). Expected: the same inventory as Task 6 Step 0 — the migrated `parallel-coalesces` (now nested depth-2, legal) and the Task-7-reclassified `settings_screened.yaml` — and NOTHING new. Any new hit: STOP, add it to the protocols RC worklist with its own adjudication.
- [ ] **Step 2:** Confirm the folds held: `pytest tests/integration/core/dag/test_dag_scenario_production_path.py tests/integration/core/dag/test_bound_region_build_acceptance.py tests/unit/core/dag/test_canonical_hash_corpus.py -q` — PASS with zero re-records needed (the corpus JSON still differs from pre-WS2 in exactly the one Task-7 adjudicated entry).
- [ ] **Step 3:** No commit (nothing authored). Record the re-check outcome in the campaign log/handoff notes.

---

### Task 15: Closeout — full-suite slice boundary, gates, docs

**Files:**
- Modify: `docs/agents/recent-code-hints.md` — add the WS2 entry (same-commit rolling-doc rule)
- Verify-only: everything

- [ ] **Step 1:** Full suite with HEAD recorded before/after: `git rev-parse HEAD && pytest tests/ -n 12 && git rev-parse HEAD`. Green, HEAD unchanged.
- [ ] **Step 2:** Trust-tier corpus diff: run the keyless lint command (Global Constraints), COUNT findings, compare with the pre-Task-1 baseline count. Added findings = fix before done (the campaign adds nothing to the corpus).
- [ ] **Step 3:** Wardline gate (Global Constraints command) — exit 0.
- [ ] **Step 4:** Re-assert the two hash-pin surfaces one last time: `pytest tests/unit/core/dag/test_canonical_hash_corpus.py tests/unit/web/composer/test_state_serialisation_contract.py -q` — PASS.
- [ ] **Step 5:** Add the recent-code-hints entry (dated), covering: (a) the `"scope"` node-config key appears ONLY on collector nodes — putting it (or any new key) on an existing node type moves every canonical hash and the corpus pin will catch it; (b) SESE walks exclude DIVERT edges BY PINNED DECISION — "fixing" rule 4 to cover on_error edges rejects all 8 loss fixtures; (c) every new `raise` under core/dag//core/config.py needs a same-commit parity adjudication; (d) `escalation_fixpoint_bound` is derived at build — do not reintroduce a constant iteration bound in leader_drain.
- [ ] **Step 6: Commit**

```bash
git add docs/agents/recent-code-hints.md
git commit -m "docs(agents): WS2 conventions — scope config key locality, DIVERT-excluded SESE, derived fixpoint bound"
```

---

## Self-Review (performed while drafting)

- **Spec §3 coverage:** config models (T2), binding registry + views (T4), conditional canonical key + hash pin corpus EARLY (T1, T3), rejected-alternative note honored (no synthetic branches anywhere).
- **Spec §7 coverage:** rule 1 → T2/T3/T4; rule 2 → T6; rule 3 → T5; rule 4 → T7; rule 5 → T8; rule 6 → T9; rule 7 → T10 (structural, pinned); rule 8 → T10; rule 9 → T11; rule 10 (composer parity same-commit + three-pin) → folded into T6–T11 + T12/T13.
- **§6.3 depth cap + fixpoint bound** → T5 (builder-enforced, config-overridable, derived bound plumbed to `run_end_of_input_barrier_flush`).
- **Type consistency:** `CloserKind` (StrEnum)/`GroupBinding`/`GroupBindingRegistry` (with `binding_for`/`register_expand_group`)/`BoundRegion`/`compute_bound_regions`/`validate_sese_regions`/`validate_openers_bound_in_region`/`validate_no_aggregations_in_regions`/`validate_escalation_targets`/`derive_escalation_fixpoint_bound` (1_000 + 8·depth, the ONE formula)/`graph.get_max_bound_region_depth()` used with the same signatures throughout; composer fields `scope_name`/`scope_opener`/`scope_policy`/`scope_on_group_failure` consistent across T12/T13.
- **Casualty sequencing (protocols RC-3/RC-4/RC-5):** each ruling casualty migrates in the SAME commit as the rejection that outlaws it — `parallel-coalesces` in T6, `settings_screened.yaml` reclassification in T7 — so every slice boundary (T11 included) is honestly green as sequenced; T14 is the RC-5 drift RE-check only. RC-7's commissioned lost-branch build-acceptance suite rides T7.
- **Known soft spots stated inline rather than hidden:** EdgeInfo attribute names (T5 step 3), the graph node-iteration surface (T3 step 1), composer test-helper idiom (T6 step 5), and the T7 forward-only membership caveat — each carries the exact grep to resolve against the live tree at execution time, because state.py and test_state.py are moving under the maintainer right now.

## Open Questions

1. **RESOLVED (maintainer ruling, 2026-08-22): `settings_screened.yaml` splits into TWO variants** — screen-before-fork on the source-known `baseline_quality` predicate (rewritten in place; SUCCESS run; sink kept), plus new `settings_screened_at_settlement.yaml` demonstrating screen-as-loss on the computed `score` through the settlement channel (PARTIAL by design; costs stated in its README). Implemented in Task 7 Step 5b; full-run verification of the settlement variant rides the WS3+WS4 integration item. See protocols RC-4 for the full ruling text.

*(Resolved by the 2026-08-22 synthesis and removed from this list: `fork-multiple-terminals-partial-failure` is pure fan-out, LEGAL, permanently FROZEN — `parallel-coalesces` is the r23 casualty (RC-2/RC-3); `CollectorSettings.input` required is RATIFIED; the fixpoint formula is RATIFIED as `1_000 + 8 × depth`, owned by this plan's Task 5.)*
